# Copyright (C) 2026 necromortis
# Distributed under the MIT software license, see the accompanying LICENSE file.
"""Minimal THORNode REST client and pure helpers around its responses.

Only the endpoints the plugin needs are wrapped. All network access goes
through Electrum's aiohttp session factory so the user's proxy settings
(e.g. Tor) are honoured. Responses are treated as untrusted input: every
field the plugin relies on is type- and range-checked before use.
"""
import asyncio
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, TYPE_CHECKING
from urllib.parse import urlparse

import aiohttp

from electrum import bitcoin, segwit_addr
from electrum.i18n import _
from electrum.logging import Logger
from electrum.network import Network
from electrum.util import make_aiohttp_session

if TYPE_CHECKING:
    from electrum.network import ProxySettings


BTC_CHAIN = 'BTC'
BTC_ASSET = 'BTC.BTC'
# Amino prefix of a secp256k1 pubkey as encoded in THORChain's bech32 "thorpub" strings.
_THORPUB_AMINO_PREFIX = bytes.fromhex('eb5ae98721')

HTTP_TIMEOUT_SEC = 20
_MAX_ATTEMPTS = 2  # one retry, only for transient failures
_RETRY_AFTER_CAP_SEC = 10
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # pools is ~40 KB; anything bigger is not THORNode
_MAX_ERROR_MESSAGE_CHARS = 300
# THORNode's symbol rule is [A-Za-z0-9-._]+ (common/symbol.go); '~' marks trade assets
_ASSET_RE = re.compile(r'^[A-Z0-9]+[.~][A-Z0-9._\-]+$', re.IGNORECASE)
# bech32 "thorpub" key as it may appear in a URL path: hrp, separator, bech32 charset
_THORPUB_RE = re.compile(r'^thorpub1[02-9ac-hj-np-z]{6,200}$')


class ThornodeError(Exception):
    """Transport or protocol failure talking to THORNode."""


class ThornodeRequestRejected(ThornodeError):
    """THORNode answered with an error message, e.g. a quote that cannot be served."""

    def __init__(self, message: str, *, status: int):
        ThornodeError.__init__(self, message)
        self.status = status


def _clean_text(text: Any, limit: int = _MAX_ERROR_MESSAGE_CHARS) -> str:
    """Server-provided text made safe for a GUI label: no control characters, bounded length."""
    text = ''.join(c if c.isprintable() or c == ' ' else ' ' for c in str(text))
    return text[:limit] + ('...' if len(text) > limit else '')


def _field(d: Mapping[str, Any], key: str, kind: str, *, what: str, default: Any = None, required: bool = False):
    """Fetch and convert one field of a THORNode JSON object, raising ThornodeError on bad data."""
    if not isinstance(d, Mapping):
        raise ThornodeError(f'malformed {what} response: expected an object')
    value = d.get(key)
    if value is None or value == '':
        if required:
            raise ThornodeError(f'malformed {what} response: missing {key}')
        return default
    try:
        if kind == 'int':
            if isinstance(value, bool):
                raise ValueError
            value = int(value)
            if value < 0:
                raise ValueError
            return value
        if kind == 'str':
            if not isinstance(value, str):
                raise ValueError
            return value
        if kind == 'bool':
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() == 'true'
            return bool(value)
    except (TypeError, ValueError):
        pass
    raise ThornodeError(f'malformed {what} response: bad {key}')


@dataclass(frozen=True)
class InboundAddress:
    chain: str
    pub_key: str
    address: str
    halted: bool
    global_trading_paused: bool
    chain_trading_paused: bool
    chain_lp_actions_paused: bool
    gas_rate: int
    gas_rate_units: str
    outbound_fee: int
    dust_threshold: int
    router: Optional[str] = None

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> 'InboundAddress':
        what = 'inbound_addresses'
        return cls(
            chain=_field(d, 'chain', 'str', what=what, required=True),
            pub_key=_field(d, 'pub_key', 'str', what=what, default=''),
            address=_field(d, 'address', 'str', what=what, default=''),
            halted=_field(d, 'halted', 'bool', what=what, default=False),
            global_trading_paused=_field(d, 'global_trading_paused', 'bool', what=what, default=False),
            chain_trading_paused=_field(d, 'chain_trading_paused', 'bool', what=what, default=False),
            chain_lp_actions_paused=_field(d, 'chain_lp_actions_paused', 'bool', what=what, default=False),
            gas_rate=_field(d, 'gas_rate', 'int', what=what, default=0),
            gas_rate_units=_field(d, 'gas_rate_units', 'str', what=what, default=''),
            outbound_fee=_field(d, 'outbound_fee', 'int', what=what, default=0),
            dust_threshold=_field(d, 'dust_threshold', 'int', what=what, default=0),
            router=_field(d, 'router', 'str', what=what, default=None),
        )


@dataclass(frozen=True)
class SwapQuote:
    inbound_address: str
    memo: str
    expected_amount_out: int  # in 1e-8 units of the destination asset
    expiry: int  # unix timestamp
    dust_threshold: int  # sats
    recommended_min_amount_in: int  # sats
    recommended_gas_rate: int  # sat/vB
    inbound_confirmation_blocks: int
    inbound_confirmation_seconds: int
    outbound_delay_seconds: int
    total_swap_seconds: int
    fee_total: int  # in 1e-8 units of the destination asset
    fee_outbound: int
    fee_liquidity: int
    fee_affiliate: int
    slippage_bps: int
    total_bps: int
    streaming_swap_blocks: int
    max_streaming_quantity: int
    notes: str
    warning: str

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> 'SwapQuote':
        what = 'quote'
        fees = d.get('fees') if isinstance(d, Mapping) else None
        if not isinstance(fees, Mapping):
            raise ThornodeError('malformed quote response: missing fees')
        return cls(
            inbound_address=_field(d, 'inbound_address', 'str', what=what, required=True),
            memo=_field(d, 'memo', 'str', what=what, required=True),
            expected_amount_out=_field(d, 'expected_amount_out', 'int', what=what, required=True),
            expiry=_field(d, 'expiry', 'int', what=what, required=True),
            dust_threshold=_field(d, 'dust_threshold', 'int', what=what, default=0),
            recommended_min_amount_in=_field(d, 'recommended_min_amount_in', 'int', what=what, default=0),
            recommended_gas_rate=_field(d, 'recommended_gas_rate', 'int', what=what, default=0),
            inbound_confirmation_blocks=_field(d, 'inbound_confirmation_blocks', 'int', what=what, default=0),
            inbound_confirmation_seconds=_field(d, 'inbound_confirmation_seconds', 'int', what=what, default=0),
            outbound_delay_seconds=_field(d, 'outbound_delay_seconds', 'int', what=what, default=0),
            total_swap_seconds=_field(d, 'total_swap_seconds', 'int', what=what, default=0),
            fee_total=_field(fees, 'total', 'int', what='quote fees', default=0),
            fee_outbound=_field(fees, 'outbound', 'int', what='quote fees', default=0),
            fee_liquidity=_field(fees, 'liquidity', 'int', what='quote fees', default=0),
            fee_affiliate=_field(fees, 'affiliate', 'int', what='quote fees', default=0),
            slippage_bps=_field(fees, 'slippage_bps', 'int', what='quote fees', default=0),
            total_bps=_field(fees, 'total_bps', 'int', what='quote fees', default=0),
            streaming_swap_blocks=_field(d, 'streaming_swap_blocks', 'int', what=what, default=0),
            max_streaming_quantity=_field(d, 'max_streaming_quantity', 'int', what=what, default=0),
            notes=_clean_text(_field(d, 'notes', 'str', what=what, default='')),
            warning=_clean_text(_field(d, 'warning', 'str', what=what, default='')),
        )


@dataclass(frozen=True)
class Pool:
    asset: str
    short_code: str
    status: str
    trading_halted: bool
    balance_asset: int
    balance_rune: int
    asset_tor_price: int = 0  # USD price of one unit of the asset, times 1e8

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> 'Pool':
        what = 'pools'
        asset = _field(d, 'asset', 'str', what=what, required=True)
        if not _ASSET_RE.match(asset):
            raise ThornodeError(f'malformed pools response: bad asset {asset!r}')
        return cls(
            asset=asset,
            short_code=_field(d, 'short_code', 'str', what=what, default=''),
            status=_field(d, 'status', 'str', what=what, default=''),
            trading_halted=_field(d, 'trading_halted', 'bool', what=what, default=False),
            balance_asset=_field(d, 'balance_asset', 'int', what=what, default=0),
            balance_rune=_field(d, 'balance_rune', 'int', what=what, default=0),
            asset_tor_price=_field(d, 'asset_tor_price', 'int', what=what, default=0),
        )

    @property
    def is_available(self) -> bool:
        return self.status == 'Available' and not self.trading_halted

    def usd_value(self, amount_1e8: int) -> Optional[float]:
        """USD value of an amount of this asset (in 1e-8 units), None if THORChain has no price."""
        if self.asset_tor_price <= 0:
            return None
        return amount_1e8 * self.asset_tor_price / 10 ** 16


@dataclass(frozen=True)
class Vault:
    """An Asgard vault as served by /thorchain/vault/{pubkey}; only the fields used for verification."""
    pub_key: str
    vault_type: str  # 'AsgardVault'
    status: str  # 'ActiveVault', 'RetiringVault', 'InactiveVault' or 'InitVault'
    addresses: Dict[str, str]  # chain -> inbound address of this vault

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> 'Vault':
        what = 'vault'
        pub_key = _field(d, 'pub_key', 'str', what=what, required=True)
        raw = d.get('addresses')
        if not isinstance(raw, list):
            raise ThornodeError('malformed vault response: missing addresses')
        addresses = {}
        for entry in raw:
            chain = _field(entry, 'chain', 'str', what=what, required=True).upper()
            addresses[chain] = _field(entry, 'address', 'str', what=what, required=True)
        return cls(
            pub_key=pub_key,
            vault_type=_field(d, 'type', 'str', what=what, default=''),
            status=_field(d, 'status', 'str', what=what, default=''),
            addresses=addresses,
        )

    @property
    def accepts_inbound(self) -> bool:
        """Funds sent to this vault get processed: it is active, or retiring (still observed,
        its funds migrate to the new vault)."""
        return self.vault_type == 'AsgardVault' and self.status in ('ActiveVault', 'RetiringVault')


def asset_chain(asset: str) -> str:
    """'ETH.USDC-0XA0B8...' -> 'ETH'"""
    chain, sep, _rest = asset.partition('.')
    if not sep or not chain:
        raise ValueError(f'malformed asset: {asset!r}')
    return chain.upper()


def asset_display_name(asset: str, pools: Sequence[Pool] = ()) -> str:
    """'ETH.USDC-0XA0B8...EB48' -> 'ETH.USDC'; a short address suffix is appended only
    when another pool has the same chain and ticker."""
    chain, _, symbol = asset.partition('.')
    ticker, _, address = symbol.partition('-')
    name = f'{chain}.{ticker}'
    if address:
        twins = [p for p in pools if p.asset.upper() != asset.upper()
                 and p.asset.upper().startswith(f'{name.upper()}-')]
        if twins:
            name += '-' + address[-6:]
    return name


def vault_address_from_pubkey(pub_key: str) -> str:
    """Derive the Bitcoin P2WPKH address of an Asgard vault from its bech32 'thorpub' key.

    Used as a consistency check of the inbound address served by THORNode: both
    come from the same response, so this catches corruption and API bugs, not a
    malicious node. Raises ValueError on anything that is not a thorpub key.
    """
    decoded = segwit_addr.bech32_decode(pub_key, ignore_long_length=True)
    if decoded.hrp != 'thorpub' or decoded.data is None:
        raise ValueError('not a thorpub bech32 string')
    data = segwit_addr.convertbits(decoded.data, 5, 8, False)
    if data is None:
        raise ValueError('bad thorpub payload')
    raw = bytes(data)
    if not raw.startswith(_THORPUB_AMINO_PREFIX) or len(raw) != len(_THORPUB_AMINO_PREFIX) + 33:
        raise ValueError('unexpected thorpub payload')
    return bitcoin.public_key_to_p2wpkh(raw[len(_THORPUB_AMINO_PREFIX):])


def _mimir_halt_active(mimir: Mapping[str, int], key: str, height: int) -> bool:
    # THORNode semantics: a halt mimir is active when 0 < value <= current block height
    # (a value above the current height is a halt scheduled for the future).
    value = mimir.get(key.upper())
    if not value:
        return False
    return 0 < int(value) <= height


def halt_reasons(mimir: Mapping[str, int], thorchain_height: int, dest_chain: str) -> List[str]:
    """Human readable reasons why a BTC -> dest_chain swap cannot be sent right now.

    The swap quote endpoint simulates the swap handler and therefore reports
    trading halts, but it does not check chain halts (observation/signing
    stopped), so those have to be read from mimir explicitly.
    """
    dest_chain = dest_chain.upper()
    checks = [
        ('HALTCHAINGLOBAL', _("all chains are halted")),
        ('HALTTRADING', _("trading is halted globally")),
        ('HALTSIGNING', _("signing is halted globally")),
        (f'HALT{BTC_CHAIN}CHAIN', _("the Bitcoin chain is halted")),
        (f'SOLVENCYHALT{BTC_CHAIN}CHAIN', _("the Bitcoin chain is halted (solvency)")),
        (f'HALT{BTC_CHAIN}TRADING', _("Bitcoin trading is halted")),
        (f'HALTSIGNING{BTC_CHAIN}', _("Bitcoin signing is halted")),
    ]
    if dest_chain != BTC_CHAIN:
        checks += [
            (f'HALT{dest_chain}CHAIN', _("the {} chain is halted").format(dest_chain)),
            (f'SOLVENCYHALT{dest_chain}CHAIN', _("the {} chain is halted (solvency)").format(dest_chain)),
            (f'HALT{dest_chain}TRADING', _("{} trading is halted").format(dest_chain)),
            (f'HALTSIGNING{dest_chain}', _("{} signing is halted").format(dest_chain)),
        ]
    return [reason for key, reason in checks if _mimir_halt_active(mimir, key, thorchain_height)]


def validate_thornode_url(url: str) -> str:
    """Accept only https URLs (plain http just for localhost and .onion). Returns the cleaned URL."""
    url = (url or '').strip().rstrip('/')
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme not in ('http', 'https') or not host or '?' in url or '#' in url
            or parsed.username or parsed.password
            or not re.fullmatch(r'[A-Za-z0-9.\-\[\]:]+', parsed.netloc)
            or not re.fullmatch(r'[A-Za-z0-9._~\-/]*', parsed.path)):
        raise ThornodeError(_('THORNode URL must look like https://host/path'))
    if parsed.scheme == 'http' and not (host in ('localhost', '127.0.0.1', '::1') or host.endswith('.onion')):
        raise ThornodeError(_('THORNode URL must use https (plain http is only allowed for localhost and .onion)'))
    return url


class ThornodeClient(Logger):

    def __init__(self, base_url: str, *, client_id: str = 'electrum-thorchain-swap'):
        Logger.__init__(self)
        self.base_url = validate_thornode_url(base_url)
        # Passing our own headers replaces Electrum's defaults, so set the same
        # User-Agent the wallet uses for all its other HTTP traffic.
        self._headers = {'User-Agent': 'Electrum', 'x-client-id': client_id, 'Accept': 'application/json'}

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or self.base_url

    @staticmethod
    def _proxy() -> Optional['ProxySettings']:
        network = Network.get_instance()
        return network.proxy if network else None

    async def _get(self, path: str, params: Optional[Mapping[str, str]] = None) -> Any:
        url = f'{self.base_url}/thorchain/{path.lstrip("/")}'
        last_error = None  # type: Optional[Exception]
        for attempt in range(_MAX_ATTEMPTS):
            try:
                async with make_aiohttp_session(self._proxy(), headers=self._headers, timeout=HTTP_TIMEOUT_SEC) as session:
                    # never follow redirects: the vault address must come from the configured host
                    async with session.get(url, params=params, allow_redirects=False) as resp:
                        if 300 <= resp.status < 400:
                            raise ThornodeError(f'{self.host} redirected the request (HTTP {resp.status}); refusing')
                        if resp.content_length is not None and resp.content_length > _MAX_RESPONSE_BYTES:
                            raise ThornodeError(f'{self.host} response too large')
                        # read the whole body in chunks: StreamReader.read(n) may return a partial chunk
                        raw = bytearray()
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            raw += chunk
                            if len(raw) > _MAX_RESPONSE_BYTES:
                                raise ThornodeError(f'{self.host} response too large')
                        raw = bytes(raw)
                        if resp.status in (429, 503):
                            delay = self._retry_after(resp.headers.get('Retry-After'))
                            last_error = ThornodeError(f'{self.host} is busy (HTTP {resp.status})')
                            if attempt + 1 < _MAX_ATTEMPTS:
                                await asyncio.sleep(delay)
                            continue
                        try:
                            body = json.loads(raw)
                        except ValueError:
                            content_type = resp.headers.get('Content-Type', '')
                            self.logger.info(f'non-JSON reply from {url}: status={resp.status} '
                                             f'content-type={content_type!r} head={raw[:200]!r}')
                            last_error = ThornodeError(
                                f'{self.host} did not return JSON (HTTP {resp.status}, {_clean_text(content_type, 40) or "no content type"})')
                            if 200 <= resp.status < 300 and attempt + 1 < _MAX_ATTEMPTS:
                                await asyncio.sleep(2.0)  # a front proxy's error page is usually transient
                                continue
                            raise last_error
                        if resp.status != 200:
                            message = body.get('message', str(body)) if isinstance(body, dict) else str(body)
                            raise ThornodeRequestRejected(_clean_text(message), status=resp.status)
                        return body
            except asyncio.TimeoutError:
                last_error = ThornodeError(_('THORNode {} did not answer in time').format(self.host))
                self.logger.info(f'request timed out (attempt {attempt + 1}): {url}')
            except aiohttp.ClientError as e:
                # connection errors are transient; anything else is final
                self.logger.info(f'request failed (attempt {attempt + 1}): {e!r}')
                raise ThornodeError(_('Could not connect to THORNode {}: {}').format(self.host, _clean_text(e, 120)))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _retry_after(value: Optional[str]) -> float:
        try:
            seconds = float(value) if value else 2.0
        except ValueError:
            return 2.0
        if not math.isfinite(seconds):
            return 2.0
        return min(max(seconds, 1.0), _RETRY_AFTER_CAP_SEC)

    async def get_inbound_addresses(self) -> List[InboundAddress]:
        body = await self._get('inbound_addresses')
        if not isinstance(body, list):
            raise ThornodeError('malformed inbound_addresses response')
        return [InboundAddress.from_json(d) for d in body]

    async def get_inbound_address(self, chain: str = BTC_CHAIN) -> InboundAddress:
        """The inbound entry for one chain; entries of other chains are not even parsed,
        so a malformed field for some unrelated chain cannot break BTC swaps."""
        body = await self._get('inbound_addresses')
        if not isinstance(body, list):
            raise ThornodeError('malformed inbound_addresses response')
        for d in body:
            if isinstance(d, Mapping) and d.get('chain') == chain:
                return InboundAddress.from_json(d)
        raise ThornodeError(_('THORChain has no active {} vault right now').format(chain))

    async def get_mimir(self) -> Dict[str, int]:
        body = await self._get('mimir')
        if not isinstance(body, dict):
            raise ThornodeError('malformed mimir response')
        result = {}
        for k, v in body.items():
            try:
                result[str(k).upper()] = int(v)
            except (TypeError, ValueError):
                continue  # non-numeric mimir values are irrelevant to halts
        return result

    async def get_thorchain_height(self) -> int:
        body = await self._get('lastblock')
        if isinstance(body, list):
            for item in body:
                if isinstance(item, Mapping) and item.get('thorchain'):
                    height = _field(item, 'thorchain', 'int', what='lastblock', required=True)
                    if height > 0:
                        return height
        raise ThornodeError('malformed lastblock response')

    async def get_last_observed_height(self, chain: str = BTC_CHAIN) -> int:
        """Height of the newest block of an external chain that this node has observed."""
        body = await self._get('lastblock')
        if isinstance(body, list):
            for item in body:
                if isinstance(item, Mapping) and item.get('chain') == chain:
                    return _field(item, 'last_observed_in', 'int', what='lastblock', required=True)
        raise ThornodeError('malformed lastblock response')

    async def get_vault(self, pub_key: str) -> Vault:
        """The vault behind a 'thorpub' key. THORNode answers HTTP 400 for a key it does not know."""
        if not _THORPUB_RE.match(pub_key):
            raise ThornodeError('not a thorpub key')
        return Vault.from_json(await self._get(f'vault/{pub_key}'))

    async def get_pools(self) -> List[Pool]:
        body = await self._get('pools')
        if not isinstance(body, list):
            raise ThornodeError('malformed pools response')
        pools = []
        for d in body:
            try:
                pools.append(Pool.from_json(d))
            except ThornodeError as e:
                # one odd pool must not take the whole list (and every BTC swap) down
                self.logger.warning(f'skipping pool entry: {e}')
        if not pools:
            raise ThornodeError('malformed pools response')
        return pools

    async def get_pool(self, asset: str) -> Pool:
        return Pool.from_json(await self._get(f'pool/{asset}'))

    async def get_swap_quote(
            self,
            *,
            to_asset: str,
            amount_sats: int,
            destination: str,
            tolerance_bps: Optional[int] = None,
            streaming_interval: Optional[int] = None,
            streaming_quantity: Optional[int] = None,
            affiliate: Optional[str] = None,
            affiliate_bps: Optional[int] = None,
    ) -> SwapQuote:
        params = {
            'from_asset': BTC_ASSET,
            'to_asset': to_asset,
            'amount': str(amount_sats),
            'destination': destination,
        }
        if tolerance_bps is not None:
            params['tolerance_bps'] = str(tolerance_bps)
        if streaming_interval is not None:
            params['streaming_interval'] = str(streaming_interval)
        if streaming_quantity is not None:
            params['streaming_quantity'] = str(streaming_quantity)
        if affiliate:
            params['affiliate'] = affiliate
            params['affiliate_bps'] = str(affiliate_bps or 0)
        return SwapQuote.from_json(await self._get('quote/swap', params))

    async def get_tx_status(self, txid: str) -> Dict[str, Any]:
        body = await self._get(f'tx/status/{txid}')
        if not isinstance(body, dict):
            raise ThornodeError('malformed tx status response')
        return body
