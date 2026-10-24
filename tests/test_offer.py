import json
import os
import shutil
import tempfile
import time
import unittest

from electrum.simple_config import SimpleConfig

import thorchain_swap  # noqa: F401  (registers the plugin's ConfigVars)
from thorchain_swap.memo import parse_swap_memo
from thorchain_swap.swap_tx import MIN_SWAP_SATS
from thorchain_swap.thorchain_swap import QUOTE_SAFETY_MARGIN_SEC, QUOTE_TTL_SEC, SwapError, ThorchainSwapPlugin
from thorchain_swap.thornode import InboundAddress, Pool, SwapQuote

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')
ETH_DEST = '0x742d35Cc6634C0532925a3b844Bc454e4438f44e'
USDC = 'ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48'


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class FakeClient:
    """Serves the recorded fixtures; tests mutate attributes to simulate edge cases."""

    def __init__(self):
        self.inbound = {InboundAddress.from_json(d).chain: InboundAddress.from_json(d)
                        for d in load_fixture('inbound_addresses.json')}
        self.quote = load_fixture('quote_btc_eth.json')
        self.quote['inbound_address'] = self.inbound['BTC'].address
        self.quote['expiry'] = int(time.time()) + 900
        self.mimir = {k.upper(): int(v) for k, v in load_fixture('mimir.json').items()}
        self.height = next(i['thorchain'] for i in load_fixture('lastblock.json') if 'thorchain' in i)
        self.pools = [Pool.from_json(d) for d in load_fixture('pools.json')]
        self.quote_calls = []
        self.host = 'fake.thornode'

    async def get_inbound_address(self, chain):
        return self.inbound[chain]

    async def get_swap_quote(self, **kwargs):
        self.quote_calls.append(kwargs)
        quote = dict(self.quote)
        if kwargs.get('affiliate'):
            # THORNode echoes the affiliate into the memo it returns, keeping the limit slot
            # (possibly empty) in front: "=:e:<dest>::el:10"
            if isinstance(quote.get('memo'), str):
                parts = quote['memo'].split(':')
                parts += [''] * (4 - len(parts))
                quote['memo'] = ':'.join(parts[:4] + [kwargs['affiliate'], str(kwargs['affiliate_bps'])])
            try:
                fee = int(quote['expected_amount_out']) * kwargs['affiliate_bps'] // 10000
                quote['fees'] = dict(quote['fees'], affiliate=str(fee))
            except (TypeError, ValueError):
                pass  # malformed fixture on purpose; leave it to the parser
        return SwapQuote.from_json(quote)

    async def get_mimir(self):
        return self.mimir

    async def get_thorchain_height(self):
        return self.height

    async def get_pools(self):
        return self.pools

    async def get_pool(self, asset):
        return next(p for p in self.pools if p.asset == asset)



class TestFetchOffer(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='thorchain-swap-unittest-')
        self.config = SimpleConfig({'electrum_path': self.tmpdir})
        self.client = FakeClient()
        self.plugin = ThorchainSwapPlugin(None, self.config, 'thorchain_swap')
        self.plugin.get_client = lambda: self.client

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    async def fetch(self, **overrides):
        kwargs = dict(to_asset='ETH.ETH', destination=ETH_DEST, amount_sats=1_000_000, slippage_bps=100)
        kwargs.update(overrides)
        return await self.plugin.fetch_offer(**kwargs)

    async def test_good_offer(self):
        offer = await self.fetch()
        q = offer.quote
        self.assertEqual(offer.inbound.address, q.inbound_address)
        self.assertEqual(offer.limit, q.expected_amount_out * 99 // 100)
        memo = parse_swap_memo(offer.memo)
        self.assertEqual(memo.asset, 'ETH.ETH')  # the full name fits, so it is preferred over THORNode's 'e'
        self.assertEqual(memo.destination, ETH_DEST)
        self.assertEqual(memo.limit, offer.limit)
        self.assertLessEqual(len(offer.memo), 80)
        self.assertEqual(offer.halts, [])
        self.assertEqual(offer.blocking_problems(), [])
        self.assertEqual(offer.params.vault_address, offer.inbound.address)
        self.assertEqual(offer.params.amount_sats, 1_000_000)
        self.assertGreater(offer.min_amount_sats, 0)
        # tolerance is NOT forwarded: THORNode would refuse to quote when the price impact exceeds it
        self.assertNotIn('tolerance_bps', self.client.quote_calls[0])

    async def test_price_impact_above_protection_blocks(self):
        self.client.quote['fees']['slippage_bps'] = 150
        offer = await self.fetch(slippage_bps=100)
        self.assertTrue(any('Price impact' in p for p in offer.blocking_problems()))
        offer = await self.fetch(slippage_bps=200)
        self.assertEqual(offer.blocking_problems(), [])

    async def test_rejects_bad_inputs(self):
        for overrides in (dict(amount_sats=0), dict(destination=''), dict(destination='a:b'),
                          dict(to_asset='BTC.BTC')):
            with self.assertRaises(SwapError):
                await self.fetch(**overrides)

    async def test_vault_pubkey_mismatch(self):
        btc = self.client.inbound['BTC']
        self.client.inbound['BTC'] = InboundAddress(**{**btc.__dict__, 'address': 'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4'})
        self.client.quote['inbound_address'] = self.client.inbound['BTC'].address
        with self.assertRaises(SwapError):
            await self.fetch()

    async def test_quote_from_other_vault(self):
        self.client.quote['inbound_address'] = 'bc1q704ak5tqw547nql6m65rjyaqgryq6h54d6ks2s'
        with self.assertRaises(SwapError):
            await self.fetch()

    async def test_quote_destination_mismatch(self):
        self.client.quote['memo'] = '=:e:0x0000000000000000000000000000000000000000:1'
        with self.assertRaises(SwapError):
            await self.fetch()

    async def test_halts_are_reported_not_raised(self):
        self.client.mimir['HALTETHCHAIN'] = 1
        self.client.inbound['BTC'] = InboundAddress(**{**self.client.inbound['BTC'].__dict__, 'halted': True})
        offer = await self.fetch()
        self.assertEqual(len(offer.halts), 2)
        self.assertTrue(offer.blocking_problems())

    async def test_min_amount_and_expiry(self):
        # below the hard floor nothing is even quoted
        with self.assertRaises(SwapError):
            await self.fetch(amount_sats=5000)
        offer = await self.fetch()
        self.assertGreaterEqual(offer.min_amount_sats, MIN_SWAP_SATS)
        soon = offer.expiry - QUOTE_SAFETY_MARGIN_SEC + 1
        self.assertEqual(offer.blocking_problems(now=soon - 2), [])
        self.assertTrue(offer.blocking_problems(now=soon))
        # a server expiry far in the future is capped at THORNode's own 15 minute TTL
        self.client.quote['expiry'] = 10 ** 12
        offer = await self.fetch()
        self.assertLessEqual(offer.expiry, offer.created_at + QUOTE_TTL_SEC)
        self.assertTrue(offer.blocking_problems(now=offer.created_at + QUOTE_TTL_SEC))
        # an already expired quote is refused outright
        self.client.quote['expiry'] = int(time.time()) - 1
        with self.assertRaises(SwapError):
            await self.fetch()

    async def test_quoted_asset_must_match_selection(self):
        for memo_asset in ('ETH.USDT', 'b', 'DOGE.DOGE', 'ETH.ETH-0X1234'):
            self.client.quote['memo'] = f'=:{memo_asset}:{ETH_DEST}'
            with self.assertRaises(SwapError):
                await self.fetch()
        for memo_asset in ('e', 'ETH.ETH', 'eth.eth'):
            self.client.quote['memo'] = f'=:{memo_asset}:{ETH_DEST}'
            await self.fetch()

    async def test_thorname_destination_gets_clear_message(self):
        self.client.quote['memo'] = '=:e:thor1n6zn4mq9nrk235pg6phaw6pac2wcwpgy4xjlxa'
        with self.assertRaises(SwapError) as cm:
            await self.fetch(destination='el')
        self.assertIn('THORName', str(cm.exception))

    async def test_malformed_quotes_become_swap_errors(self):
        for bad in ({'fees': None}, {'memo': None}, {'expected_amount_out': 'x'}, {'expected_amount_out': '0'},
                    {'inbound_address': None}):
            self.client.quote = {**load_fixture('quote_btc_eth.json'), 'inbound_address': self.client.inbound['BTC'].address,
                                 'expiry': int(time.time()) + 900, **bad}
            with self.assertRaises(SwapError):
                await self.fetch()

    async def test_settings_are_clamped(self):
        self.config.THORCHAIN_SWAP_SLIPPAGE_BPS = 50000
        self.assertEqual(self.plugin.get_settings().slippage_bps, 9999)
        self.config.set_key('plugins.thorchain_swap.slippage_bps', 'abc')  # hand-edited config
        self.assertEqual(self.plugin.get_settings().slippage_bps, 100)
        self.config.set_key('plugins.thorchain_swap.affiliate_thorname', 'e:l')
        self.assertEqual(self.plugin.get_settings().affiliate, '')
        self.config.THORCHAIN_SWAP_AFFILIATE_BPS = 5000
        self.assertEqual(self.plugin.get_settings().affiliate_bps, 1000)

    async def test_thornode_url_validation(self):
        real_get_client = ThorchainSwapPlugin.get_client
        for url, ok in (('https://gateway.liquify.com/chain/thorchain_api', True), ('http://evil.example/api', False),
                        ('http://localhost:1317', True), ('http://abc.onion', True), ('not a url', False),
                        ('ftp://x.y', False), ('https://x.y/?q=1', False)):
            self.config.THORCHAIN_SWAP_THORNODE_URL = url
            if ok:
                real_get_client(self.plugin)
            else:
                with self.assertRaises(SwapError):
                    real_get_client(self.plugin)

    async def test_long_asset_name_is_abbreviated(self):
        # pretend THORNode spelled the token in full in its memo
        self.client.quote['memo'] = f'=:{USDC}:{ETH_DEST}:1'
        await self.plugin.fetch_assets()  # fills the pool cache used for abbreviation
        offer = await self.fetch(to_asset=USDC)
        self.assertLessEqual(len(offer.memo.encode()), 80)
        memo = parse_swap_memo(offer.memo)
        self.assertTrue(memo.asset.startswith('ETH.USDC-'))
        self.assertTrue(USDC.endswith(memo.asset.partition('-')[2]))

    async def test_streaming_settings(self):
        offer = await self.fetch(streaming_interval=2, streaming_quantity=10)
        self.assertEqual((offer.streaming_interval, offer.streaming_quantity), (2, 10))
        memo = parse_swap_memo(offer.memo)
        self.assertEqual((memo.limit, memo.interval, memo.quantity), (offer.limit, 2, 10))
        call = self.client.quote_calls[-1]
        self.assertEqual((call['streaming_interval'], call['streaming_quantity']), (2, 10))
        # defaults: nothing streaming-related is sent or written
        offer = await self.fetch()
        self.assertEqual(parse_swap_memo(offer.memo).interval, 0)
        call = self.client.quote_calls[-1]
        self.assertIsNone(call['streaming_interval'])
        self.assertIsNone(call['streaming_quantity'])
        # "rapid" mode: quantity without interval
        offer = await self.fetch(streaming_interval=0, streaming_quantity=5)
        self.assertEqual(parse_swap_memo(offer.memo).quantity, 5)
        self.assertIsNone(self.client.quote_calls[-1]['streaming_interval'])
        with self.assertRaises(SwapError):
            await self.fetch(streaming_quantity=-1)

    async def test_affiliate_fee(self):
        # defaults from the plugin config: THORName "el", 10 bps
        offer = await self.fetch()
        self.assertEqual((offer.affiliate, offer.affiliate_bps), ('el', 10))
        memo = parse_swap_memo(offer.memo)
        self.assertEqual((memo.affiliate, memo.affiliate_bps), ('el', 10))
        self.assertTrue(offer.memo.endswith(':el:10'))
        call = self.client.quote_calls[-1]
        self.assertEqual((call['affiliate'], call['affiliate_bps']), ('el', 10))
        self.assertGreater(offer.quote.fee_affiliate, 0)
        # an empty name disables the fee entirely
        self.config.THORCHAIN_SWAP_AFFILIATE_THORNAME = ''
        offer = await self.fetch()
        self.assertEqual((offer.affiliate, offer.affiliate_bps), ('', 0))
        self.assertEqual(parse_swap_memo(offer.memo).affiliate, '')
        self.assertIsNone(self.client.quote_calls[-1]['affiliate'])

    async def test_affiliate_waived_for_small_swaps(self):
        btc_price = next(p for p in self.client.pools if p.asset == 'BTC.BTC').asset_tor_price / 1e8
        self.assertGreater(btc_price, 10_000)  # sanity: fixture price is in USD
        sats_for_99_usd = int(99 / btc_price * 1e8)
        sats_for_101_usd = int(101 / btc_price * 1e8)
        small = await self.fetch(amount_sats=sats_for_99_usd)
        # still attributed to the plugin, but at 0 bps
        self.assertEqual((small.affiliate, small.affiliate_bps), ('el', 0))
        self.assertAlmostEqual(small.amount_usd, 99, delta=0.01)
        self.assertTrue(small.memo.endswith(':el:0'))
        self.assertEqual(parse_swap_memo(small.memo).affiliate_bps, 0)
        call = self.client.quote_calls[-1]
        self.assertEqual((call['affiliate'], call['affiliate_bps']), ('el', 0))
        big = await self.fetch(amount_sats=sats_for_101_usd)
        self.assertEqual((big.affiliate, big.affiliate_bps), ('el', 10))
        self.assertTrue(big.memo.endswith(':el:10'))
        # threshold is configurable; 0 charges every swap
        self.config.THORCHAIN_SWAP_AFFILIATE_MIN_USD = 0
        small = await self.fetch(amount_sats=sats_for_99_usd)
        self.assertEqual((small.affiliate, small.affiliate_bps), ('el', 10))
        # unknown price counts as small: the documented rule is that small swaps are free
        self.config.THORCHAIN_SWAP_AFFILIATE_MIN_USD = 100
        btc = next(p for p in self.client.pools if p.asset == 'BTC.BTC')
        self.client.pools[self.client.pools.index(btc)] = Pool(**{**btc.__dict__, 'asset_tor_price': 0})
        big = await self.fetch(amount_sats=sats_for_101_usd)
        self.assertIsNone(big.amount_usd)
        self.assertEqual((big.affiliate, big.affiliate_bps), ('el', 0))

    async def test_affiliate_mismatch_and_unregistered_name(self):
        from thorchain_swap.thornode import ThornodeRequestRejected
        real = self.client.get_swap_quote

        async def wrong_bps(**kwargs):
            kwargs['affiliate_bps'] = 50
            return await real(**kwargs)
        self.client.get_swap_quote = wrong_bps
        with self.assertRaises(SwapError):
            await self.fetch()

        async def unregistered(**kwargs):
            raise ThornodeRequestRejected(
                "failed to simulate swap: MEMO: =:e:x::el:10\nPARSE FAILURE(S): cannot parse 'el' as an Address",
                status=400)
        self.client.get_swap_quote = unregistered
        with self.assertRaises(SwapError) as cm:
            await self.fetch()
        self.assertIn('not registered', str(cm.exception))

    async def test_fetch_assets_filters(self):
        assets = await self.plugin.fetch_assets()
        names = [p.asset for p in assets]
        self.assertNotIn('BTC.BTC', names)
        self.assertEqual(names, sorted(names))
        self.assertTrue(all(p.is_available for p in assets))


if __name__ == '__main__':
    unittest.main()
