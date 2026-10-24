import json
import os
import unittest

from electrum.bitcoin import opcodes

from thorchain_swap.memo import (
    MemoError, SwapMemo, parse_uint, format_uint, parse_swap_memo, build_swap_memo, memo_bytes, memo_script,
    compute_limit, abbreviate_asset, MAX_MEMO_BYTES,
)
from thorchain_swap.thornode import Pool

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')
ETH_DEST = '0x742d35Cc6634C0532925a3b844Bc454e4438f44e'


def load_pools():
    with open(os.path.join(FIXTURES, 'pools.json')) as f:
        return [Pool.from_json(d) for d in json.load(f)]


class TestUintEncoding(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(parse_uint(''), 0)
        self.assertEqual(parse_uint('0'), 0)
        self.assertEqual(parse_uint('33775077'), 33775077)
        self.assertEqual(parse_uint('12e5'), 1200000)
        self.assertEqual(parse_uint('1E8'), 100000000)
        for bad in ('1.5', '-1', 'e5', '1e', '1e-2', 'abc'):
            with self.assertRaises(MemoError):
                parse_uint(bad)

    def test_format_roundtrip(self):
        for n in (0, 1, 10, 100, 1000, 123456, 1200000, 100000000, 33775077, 10 ** 18):
            s = format_uint(n)
            self.assertEqual(parse_uint(s), n, s)
            self.assertLessEqual(len(s), len(str(n)))
        self.assertEqual(format_uint(1200000), '12e5')
        self.assertEqual(format_uint(1000), '1e3')
        self.assertEqual(format_uint(100), '100')  # '1e2' is not shorter
        self.assertEqual(format_uint(10), '10')
        self.assertEqual(format_uint(33775077), '33775077')


class TestSwapMemo(unittest.TestCase):

    def test_parse_quote_memo(self):
        m = parse_swap_memo(f'=:e:{ETH_DEST}:33775077')
        self.assertEqual(m, SwapMemo(asset='e', destination=ETH_DEST, limit=33775077))
        self.assertFalse(m.is_streaming)

    def test_parse_streaming_and_affiliate(self):
        m = parse_swap_memo(f'SWAP:ETH.ETH:{ETH_DEST}:1e6/3/0:dx:10')
        self.assertEqual(m.asset, 'ETH.ETH')
        self.assertEqual((m.limit, m.interval, m.quantity), (1000000, 3, 0))
        self.assertEqual((m.affiliate, m.affiliate_bps), ('dx', 10))
        self.assertTrue(m.is_streaming)
        m = parse_swap_memo(f'=:e:{ETH_DEST}:/1/0')
        self.assertEqual((m.limit, m.interval, m.quantity), (0, 1, 0))

    def test_parse_rejects(self):
        for bad in ('', 'ADD:BTC.BTC', '=:e', f'=::{ETH_DEST}', f'=:e:{ETH_DEST}:1/2/3/4', f'=:e:{ETH_DEST}:x'):
            with self.assertRaises(MemoError):
                parse_swap_memo(bad)

    def test_build(self):
        memo = build_swap_memo(asset='e', destination=ETH_DEST, limit=33775077)
        self.assertEqual(memo, f'=:e:{ETH_DEST}:33775077')
        self.assertEqual(parse_swap_memo(memo), SwapMemo(asset='e', destination=ETH_DEST, limit=33775077))
        memo = build_swap_memo(asset='e', destination=ETH_DEST, limit=1200000, interval=3, quantity=0)
        self.assertEqual(memo, f'=:e:{ETH_DEST}:12e5/3/0')
        # "rapid" streaming: several sub-swaps per block, interval 0
        memo = build_swap_memo(asset='e', destination=ETH_DEST, limit=1, interval=0, quantity=10)
        self.assertEqual(memo, f'=:e:{ETH_DEST}:1/0/10')
        self.assertEqual(parse_swap_memo(memo).quantity, 10)
        # defaults (0/0) leave the streaming fields out
        self.assertEqual(build_swap_memo(asset='e', destination=ETH_DEST, limit=1, interval=0, quantity=0),
                         f'=:e:{ETH_DEST}:1')
        with self.assertRaises(MemoError):
            build_swap_memo(asset='e', destination=ETH_DEST, limit=0)
        with self.assertRaises(MemoError):
            build_swap_memo(asset='e', destination=ETH_DEST, limit=1, interval=-1)

    def test_build_with_affiliate(self):
        memo = build_swap_memo(asset='e', destination=ETH_DEST, limit=33775077, affiliate='el', affiliate_bps=10)
        self.assertEqual(memo, f'=:e:{ETH_DEST}:33775077:el:10')
        parsed = parse_swap_memo(memo)
        self.assertEqual((parsed.affiliate, parsed.affiliate_bps, parsed.limit), ('el', 10, 33775077))
        # streaming fields keep their slot in front of the affiliate
        memo = build_swap_memo(asset='e', destination=ETH_DEST, limit=1, interval=1, quantity=5,
                               affiliate='el', affiliate_bps=10)
        self.assertEqual(memo, f'=:e:{ETH_DEST}:1/1/5:el:10')
        parsed = parse_swap_memo(memo)
        self.assertEqual((parsed.interval, parsed.quantity, parsed.affiliate, parsed.affiliate_bps), (1, 5, 'el', 10))
        # zero bps still names the affiliate (attribution without a fee); empty name: no affiliate fields
        self.assertEqual(build_swap_memo(asset='e', destination=ETH_DEST, limit=1, affiliate='el', affiliate_bps=0),
                         f'=:e:{ETH_DEST}:1:el:0')
        self.assertEqual(build_swap_memo(asset='e', destination=ETH_DEST, limit=1, affiliate='', affiliate_bps=10),
                         f'=:e:{ETH_DEST}:1')
        with self.assertRaises(MemoError):
            build_swap_memo(asset='e', destination=ETH_DEST, limit=1, affiliate='el', affiliate_bps=1001)
        with self.assertRaises(MemoError):
            build_swap_memo(asset='e', destination=ETH_DEST, limit=1, affiliate='e:l', affiliate_bps=10)
        with self.assertRaises(MemoError):
            build_swap_memo(asset='e:x', destination=ETH_DEST, limit=1)
        with self.assertRaises(MemoError):
            build_swap_memo(asset='e', destination='addr/with/slash', limit=1)

    def test_memo_bytes_and_script(self):
        memo = f'=:e:{ETH_DEST}:33775077'
        self.assertEqual(memo_bytes(memo), memo.encode())
        script = memo_script(memo)
        self.assertEqual(script[0], opcodes.OP_RETURN)
        self.assertEqual(script[1], len(memo))
        self.assertEqual(script[2:], memo.encode())
        with self.assertRaises(MemoError):
            memo_bytes('x' * (MAX_MEMO_BYTES + 1))
        with self.assertRaises(UnicodeEncodeError):
            memo_bytes('=:e:адрес:1')
        # 80 bytes exactly is fine
        memo_bytes('x' * MAX_MEMO_BYTES)

    def test_compute_limit(self):
        self.assertEqual(compute_limit(34029376, 100), 33689082)
        self.assertEqual(compute_limit(34029376, 0), 34029376)
        with self.assertRaises(ValueError):
            compute_limit(1, 10000)
        with self.assertRaises(ValueError):
            compute_limit(1, -1)


class TestAbbreviateAsset(unittest.TestCase):

    def setUp(self):
        self.pools = load_pools()

    def test_gas_assets_use_short_codes(self):
        self.assertEqual(abbreviate_asset('ETH.ETH', self.pools, 10), 'e')
        self.assertEqual(abbreviate_asset('btc.btc', self.pools, 10), 'b')
        self.assertEqual(abbreviate_asset('GAIA.ATOM', self.pools, 10), 'g')

    def test_full_name_when_it_fits(self):
        usdc = 'ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48'
        self.assertEqual(abbreviate_asset(usdc, self.pools, len(usdc)), usdc)

    def test_token_suffix(self):
        usdc = 'ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48'
        short = abbreviate_asset(usdc, self.pools, 20)
        self.assertTrue(short.startswith('ETH.USDC-'))
        self.assertLessEqual(len(short), 20)
        self.assertTrue(usdc.endswith(short.partition('-')[2]))
        # the suffix must not match any other pool on that chain with that ticker
        siblings = [p.asset for p in self.pools if p.asset.startswith('ETH.USDC-') and p.asset != usdc]
        self.assertFalse(any(s.lower().endswith(short.partition('-')[2].lower()) for s in siblings))

    def test_too_short_raises(self):
        with self.assertRaises(MemoError):
            abbreviate_asset('ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48', self.pools, 8)
        # a chain with no pools at all and an over-long name
        with self.assertRaises(MemoError):
            abbreviate_asset('XXX.TOKEN-0X' + 'A' * 40, self.pools, 12)

    def test_full_memo_fits_for_every_available_pool(self):
        # longest destination address per chain (bech32m / prefixed formats are the worst cases)
        dest_lens = {'ETH': 42, 'AVAX': 42, 'BSC': 42, 'BASE': 42, 'TRON': 34, 'THOR': 43, 'GAIA': 45,
                     'SOL': 44, 'XRP': 35, 'ZEC': 35, 'LTC': 62, 'DOGE': 34, 'BCH': 54}
        limit = 9_999_999_999
        for p in self.pools:
            if not p.is_available or p.asset == 'BTC.BTC':
                continue
            dest_len = dest_lens.get(p.asset.partition('.')[0], 62)
            budget = MAX_MEMO_BYTES - len('=:') - len(':') - dest_len - len(':') - len(str(limit))
            asset = abbreviate_asset(p.asset, self.pools, budget)
            memo = build_swap_memo(asset=asset, destination='x' * dest_len, limit=limit)
            self.assertLessEqual(len(memo_bytes(memo)), MAX_MEMO_BYTES, p.asset)


if __name__ == '__main__':
    unittest.main()
