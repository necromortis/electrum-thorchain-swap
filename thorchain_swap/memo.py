# Copyright (C) 2026 necromortis
# Distributed under the MIT software license, see the accompanying LICENSE file.
"""THORChain swap memo construction and parsing.

Memo grammar (swap): ``=:ASSET:DESTADDR:LIMIT/INTERVAL/QUANTITY:AFFILIATE:FEE``
Everything after the destination address is optional. The memo has to fit
into a single Bitcoin OP_RETURN output, i.e. 80 bytes.
"""
from dataclasses import dataclass
from typing import Optional, Sequence, TYPE_CHECKING

from electrum.bitcoin import make_op_return

if TYPE_CHECKING:
    from .thornode import Pool


MAX_MEMO_BYTES = 80  # MaxOpReturnDataSize in THORNode
_SWAP_PREFIXES = ('=', 'swap', 's')
# shortest token-address suffix we are willing to use when abbreviating an asset
_MIN_ADDRESS_SUFFIX = 6


class MemoError(Exception):
    pass


@dataclass(frozen=True)
class SwapMemo:
    asset: str
    destination: str
    limit: int = 0
    interval: int = 0
    quantity: int = 0
    affiliate: str = ''
    affiliate_bps: int = 0

    @property
    def is_streaming(self) -> bool:
        return self.interval > 0


def parse_uint(s: str) -> int:
    """Parse an unsigned integer as THORNode does: plain digits or 'COEFFeEXP'."""
    s = s.strip().lower()
    if not s:
        return 0
    if 'e' in s:
        coeff, _, exp = s.partition('e')
        if not (coeff.isdigit() and exp.isdigit()):
            raise MemoError(f'cannot parse {s!r} as scientific notation')
        return int(coeff) * 10 ** int(exp)
    if not s.isdigit():
        raise MemoError(f'cannot parse {s!r} as unsigned integer')
    return int(s)


def format_uint(n: int) -> str:
    """Shortest THORNode-compatible encoding of an unsigned integer ('1200000' -> '12e5')."""
    if n < 0:
        raise ValueError('negative amount')
    plain = str(n)
    stripped = plain.rstrip('0')
    exp = len(plain) - len(stripped)
    if n == 0 or exp < 2:
        return plain
    sci = f'{stripped}e{exp}'
    return sci if len(sci) < len(plain) else plain


def parse_swap_memo(memo: str) -> SwapMemo:
    parts = memo.split(':')
    if len(parts) < 3 or parts[0].lower() not in _SWAP_PREFIXES:
        raise MemoError(f'not a swap memo: {memo!r}')
    asset, destination = parts[1], parts[2]
    if not asset or not destination:
        raise MemoError(f'swap memo without asset or destination: {memo!r}')
    limit = interval = quantity = 0
    if len(parts) > 3 and parts[3]:
        fields = parts[3].split('/')
        if len(fields) > 3:
            raise MemoError(f'too many limit fields: {parts[3]!r}')
        fields += [''] * (3 - len(fields))
        limit, interval, quantity = (parse_uint(f) for f in fields)
    affiliate = parts[4] if len(parts) > 4 else ''
    affiliate_bps = parse_uint(parts[5]) if len(parts) > 5 else 0
    return SwapMemo(asset=asset, destination=destination, limit=limit, interval=interval,
                    quantity=quantity, affiliate=affiliate, affiliate_bps=affiliate_bps)


def build_swap_memo(
        *,
        asset: str,
        destination: str,
        limit: int,
        interval: int = 0,
        quantity: int = 0,
        affiliate: str = '',
        affiliate_bps: int = 0,
) -> str:
    if not asset or ':' in asset or '/' in asset:
        raise MemoError(f'invalid asset: {asset!r}')
    if not destination or ':' in destination or '/' in destination:
        raise MemoError(f'invalid destination: {destination!r}')
    if limit <= 0:
        raise MemoError('a swap limit is required')
    if interval < 0 or quantity < 0:
        raise MemoError('negative streaming parameters')
    if not 0 <= affiliate_bps <= 1000:
        raise MemoError('affiliate fee must be between 0 and 1000 bps')
    if affiliate and (':' in affiliate or '/' in affiliate):
        raise MemoError(f'invalid affiliate: {affiliate!r}')
    memo = f'=:{asset}:{destination}:{format_uint(limit)}'
    # THORNode treats the swap as streaming when interval > 0 or quantity > 1;
    # quantity 0 lets the protocol pick the number of sub-swaps.
    if interval > 0 or quantity > 0:
        memo += f'/{interval}/{quantity}'
    # an affiliate with 0 bps attributes the swap to the plugin without charging anything
    if affiliate:
        memo += f':{affiliate}:{affiliate_bps}'
    return memo


def memo_bytes(memo: str) -> bytes:
    data = memo.encode('ascii')  # memos are ASCII; non-ASCII raises here
    if len(data) > MAX_MEMO_BYTES:
        raise MemoError(f'memo is {len(data)} bytes, OP_RETURN allows {MAX_MEMO_BYTES}')
    return data


def memo_script(memo: str) -> bytes:
    """scriptPubKey of the OP_RETURN output carrying the memo."""
    return make_op_return(memo_bytes(memo))


def compute_limit(expected_amount_out: int, slippage_bps: int) -> int:
    """Minimum acceptable output amount given the quoted amount and a tolerance."""
    if not (0 <= slippage_bps < 10_000):
        raise ValueError('slippage_bps must be in [0, 10000)')
    return expected_amount_out * (10_000 - slippage_bps) // 10_000


def _unique_suffix(symbol: str, siblings: Sequence[str]) -> Optional[str]:
    """Shortest suffix of ``symbol`` (ticker-address) that no sibling symbol also ends with.

    THORNode's fuzzy asset matching compares the lower-cased address part as a
    suffix of the pool's symbol, so a suffix unique among same-chain,
    same-ticker pools resolves unambiguously.
    """
    ticker, sep, address = symbol.partition('-')
    if not sep or not address:
        return None
    others = [s.lower() for s in siblings if s.lower() != symbol.lower()]
    for n in range(_MIN_ADDRESS_SUFFIX, len(address)):
        suffix = address[-n:]
        if not any(o.endswith(suffix.lower()) for o in others):
            return f'{ticker}-{suffix}'
    return None


def abbreviate_asset(asset: str, pools: Sequence['Pool'], max_len: int) -> str:
    """Return the shortest spelling of ``asset`` that fits ``max_len`` and THORNode resolves.

    Preference order: pool short code (gas assets), full name, ticker plus a
    unique suffix of the token address.
    """
    pool = next((p for p in pools if p.asset.upper() == asset.upper()), None)
    if pool is not None and pool.short_code:
        return pool.short_code
    if len(asset) <= max_len:
        return asset
    chain, _, symbol = asset.partition('.')
    ticker = symbol.partition('-')[0]
    siblings = [p.asset.partition('.')[2] for p in pools
                if p.asset.partition('.')[0].upper() == chain.upper()
                and p.asset.partition('.')[2].partition('-')[0].upper() == ticker.upper()]
    short_symbol = _unique_suffix(symbol, siblings)
    if short_symbol is None or len(chain) + 1 + len(short_symbol) > max_len:
        raise MemoError(f'asset {asset} cannot be abbreviated to {max_len} characters')
    return f'{chain}.{short_symbol}'
