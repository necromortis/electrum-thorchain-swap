# Copyright (C) 2026 necromortis
# Distributed under the MIT software license, see the accompanying LICENSE file.
"""GUI-independent core of the plugin: talks to THORNode and prepares swap offers."""
import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, TYPE_CHECKING

from electrum import constants
from electrum.i18n import _
from electrum.plugin import BasePlugin

from .memo import MemoError, build_swap_memo, compute_limit, memo_bytes, parse_swap_memo, abbreviate_asset, MAX_MEMO_BYTES
from .swap_tx import MIN_SWAP_SATS, SwapParams
from .thornode import (
    BTC_ASSET, BTC_CHAIN, InboundAddress, Pool, SwapQuote, ThornodeClient, ThornodeError, ThornodeRequestRejected,
    asset_chain, halt_reasons, validate_thornode_url, vault_address_from_pubkey,
)

if TYPE_CHECKING:
    from electrum.simple_config import SimpleConfig
    from electrum.wallet import Abstract_Wallet


# Do not start building a transaction when the quote is about to expire: signing
# (hardware wallet, password) and broadcasting take time too.
QUOTE_SAFETY_MARGIN_SEC = 60
# THORNode quotes are valid for 15 minutes; never trust a longer expiry from the server.
QUOTE_TTL_SEC = 15 * 60
# One deadline for the whole quote round-trip, whatever the client's retries do.
FETCH_DEADLINE_SEC = 40
# The verification node only confirms the vault; a slow answer must not hold up the quote.
VERIFY_TIMEOUT_SEC = 6
# A verification node whose view of the Bitcoin chain is this far behind the wallet's own
# chain tip has stopped syncing: it is treated as unavailable, not as contradicting the first node.
VERIFY_MAX_BTC_LAG_BLOCKS = 6

DEFAULT_SLIPPAGE_BPS = 100
DEFAULT_AFFILIATE_BPS = 10
DEFAULT_AFFILIATE_MIN_USD = 100


class SwapError(Exception):
    """User-facing error; str() is shown in the GUI."""


@dataclass(frozen=True)
class SwapSettings:
    """Plugin settings as read from the config, validated and clamped.

    The config file can be hand-edited, so every value is range-checked here
    instead of trusting ConfigVar's type conversion to never raise.
    """
    slippage_bps: int
    streaming_interval: int
    streaming_quantity: int
    affiliate: str
    affiliate_bps: int
    affiliate_min_usd: int


@dataclass(frozen=True)
class VaultCheck:
    """Outcome of asking a second, independent THORNode about the inbound vault.

    A contradiction (unknown vault, wrong status, different address) never ends up
    here: it is raised as a SwapError. This only records whether the confirmation
    could be obtained, and why not.
    """
    host: str  # verification node asked, '' when the check is disabled
    confirmed: bool
    warning: str = ''  # user-facing reason the vault is not confirmed, '' when it is or the check is off


@dataclass(frozen=True)
class SwapOffer:
    """A quote from THORNode together with everything derived from it.

    Immutable: a new offer is requested instead of mutating an old one.
    """
    to_asset: str
    destination: str
    amount_sats: int
    slippage_bps: int
    streaming_interval: int  # blocks between sub-swaps, 0 = several per block
    streaming_quantity: int  # number of sub-swaps, 0 = chosen by THORChain
    affiliate: str  # THORName collecting the plugin fee, '' = no fee
    affiliate_bps: int
    amount_usd: Optional[float]  # by THORChain's BTC price; None if unknown
    inbound: InboundAddress
    quote: SwapQuote
    memo: str
    limit: int  # minimum acceptable output, 1e-8 units of to_asset
    halts: List[str]
    created_at: float
    thornode_host: str
    vault_check: VaultCheck

    @property
    def params(self) -> SwapParams:
        return SwapParams(vault_address=self.inbound.address, amount_sats=self.amount_sats, memo=self.memo)

    @property
    def expiry(self) -> float:
        """Effective expiry: the server's, capped at THORNode's own quote TTL."""
        return min(self.quote.expiry, self.created_at + QUOTE_TTL_SEC)

    @property
    def min_amount_sats(self) -> int:
        return max(MIN_SWAP_SATS, self.inbound.dust_threshold, self.quote.dust_threshold,
                   self.quote.recommended_min_amount_in)

    def seconds_left(self, now: Optional[float] = None) -> int:
        return int(self.expiry - (now if now is not None else time.time()))

    def matches(self, *, to_asset: str, destination: str, amount_sats: int, slippage_bps: int,
                streaming_interval: int, streaming_quantity: int, **_ignored) -> bool:
        """True if the offer was produced for exactly these inputs and settings."""
        return (self.to_asset == to_asset and self.destination == destination and self.amount_sats == amount_sats
                and self.slippage_bps == slippage_bps and self.streaming_interval == streaming_interval
                and self.streaming_quantity == streaming_quantity)

    def blocking_problems(self, now: Optional[float] = None) -> List[str]:
        """Reasons the offer must not be used; empty means it is good to go."""
        problems = list(self.halts)
        if self.amount_sats < self.min_amount_sats:
            problems.append(_("Amount is below the THORChain minimum of {} sat").format(self.min_amount_sats))
        if self.quote.slippage_bps > self.slippage_bps:
            # same meaning as the price protection of swap.thorchain.org: it covers the price impact
            # of the swap itself, not only price movement after the quote
            problems.append(
                _("Price impact of this swap is {} bps, above your price protection of {} bps. "
                  "Reduce the amount, use more sub-swaps, or raise the protection in the settings.").format(
                    self.quote.slippage_bps, self.slippage_bps))
        if self.seconds_left(now) <= QUOTE_SAFETY_MARGIN_SEC:
            problems.append(_("Quote expired, request a new one"))
        return problems


@dataclass(frozen=True)
class SwapStatus:
    """Where a swap stands on THORChain, for a one-line text and a progress bar."""
    text: str
    done: bool
    progress: float = 0.0  # 0..1 across the whole swap
    result: str = ''  # 'completed' | 'refunded' | 'partially refunded' once done
    out_txid: str = ''
    delivered: int = 0  # amount actually paid out to the destination, 1e-8 units of the asset


# progress reached when a stage completes; streaming fills the gap between SWAPPING and SWAPPED
_P_OBSERVED, _P_CONFIRMED, _P_FINALISED, _P_SWAPPED, _P_DONE = 0.1, 0.25, 0.3, 0.85, 1.0


def _delivered_amount(out_tx: dict) -> int:
    try:
        return sum(int(c.get('amount', 0)) for c in out_tx.get('coins') or [])
    except (TypeError, ValueError):
        return 0


def interpret_tx_status(body: dict, *, destination: str = '') -> SwapStatus:
    """Turn THORNode's /thorchain/tx/status response into a SwapStatus."""
    if not isinstance(body, dict):
        raise ThornodeError('malformed tx status response')
    stages = body.get('stages') or {}
    observed = stages.get('inbound_observed') or {}
    if not observed.get('completed'):
        return SwapStatus(_("waiting for THORChain to observe the transaction"), done=False, progress=0.02)
    counted = stages.get('inbound_confirmation_counted') or {}
    if not counted.get('completed'):
        remaining = int(counted.get('remaining_confirmation_seconds') or 0)
        if remaining:
            return SwapStatus(_("waiting for Bitcoin confirmations, about {} min left").format(
                max(1, round(remaining / 60))), done=False, progress=_P_OBSERVED)
        return SwapStatus(_("waiting for Bitcoin confirmations"), done=False, progress=_P_OBSERVED)
    if not (stages.get('inbound_finalised') or {}).get('completed'):
        return SwapStatus(_("inbound being finalised by THORChain"), done=False, progress=_P_CONFIRMED)
    swap = stages.get('swap_status') or {}
    if swap.get('pending'):
        streaming = swap.get('streaming') or {}
        quantity = int(streaming.get('quantity') or 0)
        count = int(streaming.get('count') or 0)
        if quantity > 1:
            interval = int(streaming.get('interval') or 0)
            left_sec = max(0, quantity - count) * max(interval, 1) * 6
            if left_sec >= 3600:
                left = _("about {} h left").format(round(left_sec / 3600, 1))
            else:
                left = _("about {} min left").format(max(1, round(left_sec / 60)))
            text = _("{} / {} sub-swaps, {}").format(count, quantity, left)
            progress = _P_FINALISED + (_P_SWAPPED - _P_FINALISED) * min(1.0, count / quantity)
            return SwapStatus(text, done=False, progress=progress)
        return SwapStatus(_("swapping"), done=False, progress=(_P_FINALISED + _P_SWAPPED) / 2)
    outbound = stages.get('outbound_signed') or {}
    out_txs = body.get('out_txs') or []
    refunds = [o for o in out_txs if str(o.get('memo', '')).upper().startswith('REFUND')]
    delivered = [o for o in out_txs if str(o.get('memo', '')).upper().startswith('OUT')
                 and str(o.get('to_address', '')).lower() == destination.lower()]
    if not outbound.get('completed'):
        if any(p.get('refund') for p in body.get('planned_out_txs') or []):
            return SwapStatus(_("refund being sent"), done=False, progress=_P_SWAPPED)
        return SwapStatus(_("swapped, outbound being sent"), done=False, progress=_P_SWAPPED)
    if refunds and not delivered:
        return SwapStatus(_("refunded"), done=True, progress=_P_DONE, result='refunded',
                          out_txid=str(refunds[0].get('id', '')))
    if refunds and delivered:
        return SwapStatus(_("completed, part of the amount was refunded"), done=True, progress=_P_DONE,
                          result='partially refunded', out_txid=str(delivered[0].get('id', '')),
                          delivered=_delivered_amount(delivered[0]))
    if delivered:
        return SwapStatus(_("completed"), done=True, progress=_P_DONE, result='completed',
                          out_txid=str(delivered[0].get('id', '')), delivered=_delivered_amount(delivered[0]))
    return SwapStatus(_("completed"), done=True, progress=_P_DONE, result='completed')


def _quoted_asset_matches(to_asset: str, quoted: str, pools: Sequence[Pool]) -> bool:
    """Does THORNode's spelling of the asset in its quote memo denote the asset the user chose?

    Accepted: identical name, the pool's short code, or same chain and ticker with the
    quoted address part being a suffix of the real one (THORNode's own fuzzy rule).
    """
    to_asset, quoted = to_asset.upper(), quoted.upper()
    if quoted == to_asset:
        return True
    for pool in pools:
        if pool.asset.upper() == to_asset and pool.short_code and pool.short_code.upper() == quoted:
            return True
    chain, _, symbol = to_asset.partition('.')
    ticker, _, address = symbol.partition('-')
    q_chain, _, q_symbol = quoted.partition('.')
    q_ticker, _, q_address = q_symbol.partition('-')
    return q_chain == chain and q_ticker == ticker and (not q_address or address.endswith(q_address))


class ThorchainSwapPlugin(BasePlugin):

    def __init__(self, parent, config: 'SimpleConfig', name: str):
        BasePlugin.__init__(self, parent, config, name)
        self._pools = []  # type: List[Pool]

    def is_available(self) -> bool:
        # THORChain only serves Bitcoin mainnet; there is no testnet deployment.
        return constants.net is constants.BitcoinMainnet

    # ---- settings

    def _config_int(self, name: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(getattr(self.config, name))
        except (ValueError, TypeError, AssertionError):
            self.logger.warning(f'invalid value for {name} in config, using default {default}')
            return default
        clamped = max(lo, min(hi, value))
        if clamped != value:
            self.logger.warning(f'{name}={value} is outside [{lo}, {hi}], using {clamped}')
        return clamped

    def get_settings(self) -> SwapSettings:
        try:
            affiliate = str(self.config.THORCHAIN_SWAP_AFFILIATE_THORNAME or '').strip()
        except (ValueError, TypeError, AssertionError):
            affiliate = ''
        # THORNames are [A-Za-z0-9+_-]{1,30}
        if not affiliate.isascii() or len(affiliate) > 30 \
                or not affiliate.replace('-', '').replace('_', '').replace('+', '').isalnum():
            if affiliate:
                self.logger.warning('invalid affiliate THORName in config, fee disabled')
            affiliate = ''
        return SwapSettings(
            slippage_bps=self._config_int('THORCHAIN_SWAP_SLIPPAGE_BPS', DEFAULT_SLIPPAGE_BPS, 1, 9999),
            # THORChain caps the streaming length at 14400 blocks (StreamingSwapMaxLength)
            streaming_interval=self._config_int('THORCHAIN_SWAP_STREAMING_INTERVAL', 0, 0, 14400),
            streaming_quantity=self._config_int('THORCHAIN_SWAP_STREAMING_QUANTITY', 0, 0, 14400),
            affiliate=affiliate,
            affiliate_bps=self._config_int('THORCHAIN_SWAP_AFFILIATE_BPS', DEFAULT_AFFILIATE_BPS, 0, 1000),
            affiliate_min_usd=self._config_int('THORCHAIN_SWAP_AFFILIATE_MIN_USD', DEFAULT_AFFILIATE_MIN_USD, 0, 10 ** 9),
        )

    def thornode_url(self) -> str:
        try:
            return validate_thornode_url(str(self.config.THORCHAIN_SWAP_THORNODE_URL or ''))
        except (ThornodeError, ValueError, TypeError, AssertionError) as e:
            raise SwapError(_("Invalid THORNode URL in the plugin settings: {}").format(e))

    def get_client(self) -> ThornodeClient:
        return ThornodeClient(self.thornode_url())

    def verify_url(self) -> str:
        """URL of the vault verification node; '' when the check is disabled in the config."""
        try:
            url = str(self.config.THORCHAIN_SWAP_VERIFY_URL or '').strip()
        except (ValueError, TypeError, AssertionError):
            url = ''
        if not url:
            return ''
        try:
            return validate_thornode_url(url)
        except ThornodeError as e:
            raise SwapError(_("Invalid vault verification URL in the plugin settings: {}").format(e))

    def get_verify_client(self) -> Optional[ThornodeClient]:
        url = self.verify_url()
        return ThornodeClient(url) if url else None

    def asset_usd_price(self, asset: str) -> Optional[float]:
        """USD price of one unit of the asset from the cached pool list, None if unknown."""
        for pool in self._pools:
            if pool.asset.upper() == asset.upper() and pool.asset_tor_price > 0:
                return pool.asset_tor_price / 10 ** 8
        return None

    # ---- THORNode

    async def fetch_assets(self) -> List[Pool]:
        """Assets that can currently be bought with BTC, sorted by name. Raises SwapError only."""
        try:
            pools = await asyncio.wait_for(self.get_client().get_pools(), FETCH_DEADLINE_SEC)
        except SwapError:
            raise
        except asyncio.TimeoutError:
            raise SwapError(_("THORNode did not answer within {} seconds").format(FETCH_DEADLINE_SEC))
        except ThornodeError as e:
            raise SwapError(str(e))
        self._pools = pools
        return sorted((p for p in pools if p.is_available and p.asset != BTC_ASSET), key=lambda p: p.asset)

    async def fetch_offer(
            self, *,
            to_asset: str,
            destination: str,
            amount_sats: int,
            slippage_bps: int,
            streaming_interval: int = 0,
            streaming_quantity: int = 0,
            local_btc_height: int = 0,
    ) -> SwapOffer:
        """Ask THORNode for a quote and validate everything we are going to rely on.

        local_btc_height is the wallet's own Bitcoin chain tip (0 = unknown), used to
        tell a verification node that stopped syncing from one that disagrees.
        Raises SwapError with a message meant for the user; nothing else escapes.
        """
        try:
            return await asyncio.wait_for(
                self._fetch_offer(to_asset=to_asset, destination=destination, amount_sats=amount_sats,
                                  slippage_bps=slippage_bps, streaming_interval=streaming_interval,
                                  streaming_quantity=streaming_quantity, local_btc_height=local_btc_height),
                FETCH_DEADLINE_SEC)
        except SwapError:
            raise
        except asyncio.TimeoutError:
            raise SwapError(_("THORNode did not answer within {} seconds").format(FETCH_DEADLINE_SEC))
        except ThornodeRequestRejected as e:
            raise SwapError(_("THORNode rejected the request: {}").format(e))
        except ThornodeError as e:
            raise SwapError(str(e))
        except (MemoError, ValueError, UnicodeEncodeError) as e:
            raise SwapError(_("Could not build the swap memo: {}").format(e))

    async def _fetch_offer(self, *, to_asset: str, destination: str, amount_sats: int, slippage_bps: int,
                           streaming_interval: int, streaming_quantity: int, local_btc_height: int) -> SwapOffer:
        if amount_sats <= 0:
            raise SwapError(_("Enter a positive amount"))
        if amount_sats < MIN_SWAP_SATS:
            raise SwapError(_("Amount is below the minimum of {} sat").format(MIN_SWAP_SATS))
        destination = destination.strip()
        if not destination:
            raise SwapError(_("Enter a destination address"))
        if any(c in destination for c in ':/'):
            raise SwapError(_('The address must not contain ":" or "/". Paste it without a "bitcoincash:" '
                              'or "ethereum:" prefix.'))
        if not destination.isascii() or not destination.isprintable() or any(c.isspace() for c in destination):
            raise SwapError(_("The address contains characters that cannot be used in a swap memo"))
        if to_asset.upper() == BTC_ASSET or not asset_chain(to_asset):
            raise SwapError(_("Choose an asset other than BTC"))
        if not (0 < slippage_bps < 10_000):
            raise SwapError(_("Price protection must be between 0.01% and 99.99%"))
        if streaming_interval < 0 or streaming_quantity < 0:
            raise SwapError(_("Invalid streaming settings"))
        settings = self.get_settings()
        client = self.get_client()

        # The THORName is always attached so the swap is attributed to the plugin;
        # below the USD threshold the fee is 0 bps. The USD value comes from the pool list.
        affiliate = settings.affiliate
        affiliate_bps = settings.affiliate_bps if affiliate else 0
        pools = await client.get_pools()
        self._pools = pools
        btc_pool = next((p for p in pools if p.asset == BTC_ASSET), None)
        amount_usd = btc_pool.usd_value(amount_sats) if btc_pool else None
        # unknown USD value counts as "small": the documented rule is that small swaps are free
        if settings.affiliate_min_usd > 0 and (amount_usd is None or amount_usd < settings.affiliate_min_usd):
            affiliate_bps = 0
        if not any(p.asset.upper() == to_asset.upper() and p.is_available for p in pools):
            raise SwapError(_("{} is not available for swaps right now").format(to_asset))

        # note: tolerance_bps is deliberately not sent. THORNode would derive the limit from
        #       the fee-less spot price and refuse to quote at all when the price impact alone
        #       exceeds it. We compute the limit from the quoted output ourselves and report a
        #       too-large price impact as a blocking problem instead.
        tasks = [asyncio.ensure_future(c) for c in (
            client.get_inbound_address(BTC_CHAIN),
            client.get_swap_quote(to_asset=to_asset, amount_sats=amount_sats, destination=destination,
                                  streaming_interval=streaming_interval or None,
                                  streaming_quantity=streaming_quantity or None,
                                  affiliate=affiliate or None, affiliate_bps=affiliate_bps),
            client.get_mimir(),
            client.get_thorchain_height(),
        )]
        try:
            inbound, quote, mimir, height = await asyncio.gather(*tasks)
        except BaseException as e:
            # one request failed (or we were cancelled): do not leave the siblings running
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(e, ThornodeRequestRejected) and affiliate \
                    and f"cannot parse '{affiliate}' as an Address" in str(e):
                raise SwapError(_("The fee THORName '{}' is not registered on THORChain").format(affiliate)) from e
            raise

        # The address must be the one the vault key controls. Both fields come from the
        # same response, so this catches corruption and API bugs rather than an attacker,
        # but it is cheap and the cost of being wrong is total loss.
        try:
            derived = vault_address_from_pubkey(inbound.pub_key)
        except (ValueError, TypeError):
            raise SwapError(_("THORChain has no usable Bitcoin vault right now"))
        if derived != inbound.address:
            raise SwapError(_("Inbound address does not match the vault public key. Refusing to continue."))
        if quote.inbound_address != inbound.address:
            # both are active vaults, but we only have the pubkey check for one of them
            raise SwapError(_("THORChain rotated its inbound vault while quoting. Please request a new quote."))
        vault_check = await self._verify_vault(inbound, primary_host=client.host, local_btc_height=local_btc_height)
        if quote.expected_amount_out <= 0:
            raise SwapError(_("Amount too small: THORChain would deliver nothing after fees"))
        now = time.time()
        if quote.expiry <= now:
            raise SwapError(_("THORNode returned an already expired quote; check your clock"))

        quote_memo = parse_swap_memo(quote.memo)
        if quote_memo.destination != destination:
            if quote_memo.destination and destination.lower() not in quote_memo.destination.lower():
                raise SwapError(_("THORNames are not supported as destination; enter the address itself"))
            raise SwapError(_("Quoted destination does not match the requested one"))
        if not _quoted_asset_matches(to_asset, quote_memo.asset, pools):
            raise SwapError(_("THORNode quoted a different asset ({}) than requested").format(quote_memo.asset))
        if affiliate and (quote_memo.affiliate != affiliate or quote_memo.affiliate_bps != affiliate_bps):
            raise SwapError(_("THORNode quoted a different fee than requested"))

        halts = halt_reasons(mimir, height, asset_chain(to_asset))
        if inbound.halted or inbound.chain_trading_paused or inbound.global_trading_paused:
            halts.append(_("Bitcoin inbound is paused on THORChain"))
        halts = list(dict.fromkeys(halts))  # dedupe, keep order
        limit = compute_limit(quote.expected_amount_out, slippage_bps)
        if limit <= 0:
            raise SwapError(_("Quoted output is too small to protect with a price limit"))
        memo = self._build_memo(to_asset=to_asset, quoted_asset=quote_memo.asset, destination=destination,
                                limit=limit, interval=streaming_interval, quantity=streaming_quantity,
                                affiliate=affiliate, affiliate_bps=affiliate_bps, pools=pools)
        return SwapOffer(
            to_asset=to_asset,
            destination=destination,
            amount_sats=amount_sats,
            slippage_bps=slippage_bps,
            streaming_interval=streaming_interval,
            streaming_quantity=streaming_quantity,
            affiliate=affiliate,
            affiliate_bps=affiliate_bps,
            amount_usd=amount_usd,
            inbound=inbound,
            quote=quote,
            memo=memo,
            limit=limit,
            halts=halts,
            created_at=now,
            thornode_host=client.host,
            vault_check=vault_check,
        )

    async def _verify_vault(self, inbound: InboundAddress, *, primary_host: str, local_btc_height: int) -> VaultCheck:
        """Confirm the inbound vault with a second, independently operated THORNode.

        The first node is trusted for everything else, but the vault address is where
        a lie costs the whole amount, so a second opinion is asked for that alone. The
        verification node only ever sees the vault's public key, nothing about the swap.

        Node unreachable or not in sync: the offer goes ahead, with a warning. Node in
        sync and contradicting the first one: SwapError, the swap is refused.
        """
        verifier = self.get_verify_client()
        if verifier is None:
            return VaultCheck(host='', confirmed=False)
        host = verifier.host
        if host == primary_host:
            return self._unconfirmed(host, _("it is the same host as the THORNode, so it cannot be a second opinion"))
        try:
            vault = await asyncio.wait_for(verifier.get_vault(inbound.pub_key), VERIFY_TIMEOUT_SEC)
        except ThornodeRequestRejected as e:
            if e.status not in (400, 404):
                return self._unconfirmed(host, str(e))
            # "vault not found" from a node that is up to date is a contradiction; from a
            # node that stopped syncing it means nothing. Its own view of the Bitcoin chain,
            # compared with the wallet's, tells the two apart without trusting the first node.
            if not await self._verifier_in_sync(verifier, local_btc_height):
                return self._unconfirmed(host, _("the node is out of sync"))
            raise SwapError(self._contradiction(primary_host, host, _("it does not know this vault")))
        except asyncio.TimeoutError:
            return self._unconfirmed(host, _("no answer within {} seconds").format(VERIFY_TIMEOUT_SEC))
        except ThornodeError as e:
            return self._unconfirmed(host, str(e))
        if not vault.accepts_inbound:
            raise SwapError(self._contradiction(
                primary_host, host, _("there the vault's status is {}").format(vault.status or _("unknown"))))
        if vault.addresses.get(BTC_CHAIN) != inbound.address:
            raise SwapError(self._contradiction(primary_host, host, _("it reports a different Bitcoin address for this vault")))
        return VaultCheck(host=host, confirmed=True)

    @staticmethod
    async def _verifier_in_sync(verifier: ThornodeClient, local_btc_height: int) -> bool:
        if local_btc_height <= 0:
            return True  # nothing to compare with; assume the worse case, a real contradiction
        try:
            observed = await asyncio.wait_for(verifier.get_last_observed_height(BTC_CHAIN), VERIFY_TIMEOUT_SEC)
        except (ThornodeError, asyncio.TimeoutError):
            return False
        return observed >= local_btc_height - VERIFY_MAX_BTC_LAG_BLOCKS

    @staticmethod
    def _unconfirmed(host: str, reason: str) -> VaultCheck:
        return VaultCheck(host=host, confirmed=False, warning=_("Vault not confirmed by {}: {}").format(host, reason))

    @staticmethod
    def _contradiction(primary_host: str, host: str, detail: str) -> str:
        return (_("The inbound vault reported by {} is contradicted by {}: {}.").format(primary_host, host, detail)
                + ' ' + _("Refusing to swap. Try again in a minute; if this persists, one of the two nodes is wrong."))

    def _build_memo(self, *, to_asset: str, quoted_asset: str, destination: str, limit: int,
                    interval: int, quantity: int, affiliate: str, affiliate_bps: int,
                    pools: Sequence[Pool]) -> str:
        """Spell the asset as safely as the 80-byte memo allows.

        Preference: the full asset name (unambiguous), then the plugin's own
        abbreviation (pool short code, or ticker plus a suffix of the contract
        address that is unique among the current pools), and only then THORNode's
        maximally shortened spelling, which on-chain fuzzy matching resolves
        against the deepest pool at execution time.
        """
        def build(asset: str) -> str:
            return build_swap_memo(asset=asset, destination=destination, limit=limit,
                                   interval=interval, quantity=quantity,
                                   affiliate=affiliate, affiliate_bps=affiliate_bps)

        candidates = [to_asset]
        budget = MAX_MEMO_BYTES - len(build('X').encode('ascii')) + 1
        try:
            candidates.append(abbreviate_asset(to_asset, pools, budget))
        except MemoError:
            pass
        candidates.append(quoted_asset)
        for asset in candidates:
            memo = build(asset)
            if len(memo.encode('ascii')) <= MAX_MEMO_BYTES:
                memo_bytes(memo)
                return memo
        raise SwapError(_("Swap memo does not fit into OP_RETURN for this asset and destination"))

    # ---- swap progress

    async def fetch_swap_statuses(self, swaps: Sequence[dict]) -> Dict[str, 'SwapStatus']:
        """THORChain's view of each swap, keyed by txid. Never raises: failures become a status text."""
        client = self.get_client()
        result = {}
        for record in swaps:
            txid = record['txid']
            try:
                body = await asyncio.wait_for(client.get_tx_status(txid), FETCH_DEADLINE_SEC)
                result[txid] = interpret_tx_status(body, destination=record.get('destination', ''))
            except asyncio.TimeoutError:
                result[txid] = SwapStatus(_("status unavailable: THORNode did not answer"), done=False)
            except (ThornodeError, ValueError, TypeError, KeyError) as e:
                result[txid] = SwapStatus(_("status unavailable: {}").format(e), done=False)
        return result

    # ---- persistent per-wallet records of swaps we broadcast

    def get_swap_records(self, wallet: 'Abstract_Wallet') -> Dict[str, dict]:
        return self.get_storage(wallet).setdefault('swaps', {})

    def pending_swap_records(self, wallet: 'Abstract_Wallet') -> List[dict]:
        """Records of swaps that THORChain has not finished yet, newest first."""
        records = [dict(r, txid=txid) for txid, r in self.get_swap_records(wallet).items() if not r.get('done')]
        return sorted(records, key=lambda r: r.get('created_at', 0), reverse=True)

    def update_swap_record(self, wallet: 'Abstract_Wallet', txid: str, status: 'SwapStatus') -> None:
        record = self.get_swap_records(wallet).get(txid)
        if record is None:
            return
        record['status_text'] = status.text
        record['progress'] = status.progress
        record['updated_at'] = int(time.time())
        if status.done:
            record['done'] = True
            record['result'] = status.result
            record['out_txid'] = status.out_txid
            record['delivered'] = status.delivered
        wallet.save_db()

    def add_swap_record(self, wallet: 'Abstract_Wallet', *, txid: str, offer: SwapOffer) -> None:
        if not isinstance(txid, str) or len(txid) != 64:
            raise ValueError(f'refusing to record a swap without a txid: {txid!r}')
        self.get_swap_records(wallet)[txid] = {
            'created_at': int(time.time()),
            'to_asset': offer.to_asset,
            'destination': offer.destination,
            'amount_sats': offer.amount_sats,
            'expected_amount_out': offer.quote.expected_amount_out,
            'limit': offer.limit,
            'streaming_interval': offer.streaming_interval,
            'streaming_quantity': offer.streaming_quantity,
            'affiliate': offer.affiliate,
            'affiliate_bps': offer.affiliate_bps,
            'vault_address': offer.inbound.address,
            'vault_confirmed_by': offer.vault_check.host if offer.vault_check.confirmed else '',
            'memo': offer.memo,
            'thornode_host': offer.thornode_host,
        }
        wallet.save_db()
