import json
import os
import unittest

from thorchain_swap.thornode import (
    InboundAddress, SwapQuote, Pool, ThornodeError, Vault, _clean_text, asset_chain, asset_display_name,
    vault_address_from_pubkey, halt_reasons, validate_thornode_url,
)

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class TestParsing(unittest.TestCase):

    def test_inbound_addresses(self):
        items = [InboundAddress.from_json(d) for d in load_fixture('inbound_addresses.json')]
        btc = [i for i in items if i.chain == 'BTC']
        self.assertEqual(len(btc), 1)
        btc = btc[0]
        self.assertTrue(btc.address.startswith('bc1q'))
        self.assertEqual(btc.gas_rate_units, 'satsperbyte')
        self.assertGreater(btc.gas_rate, 0)
        self.assertGreater(btc.dust_threshold, 0)
        self.assertIsNone(btc.router)
        eth = next(i for i in items if i.chain == 'ETH')
        self.assertTrue(eth.router.startswith('0x'))

    def test_quote(self):
        q = SwapQuote.from_json(load_fixture('quote_btc_eth.json'))
        self.assertTrue(q.inbound_address.startswith('bc1q'))
        self.assertTrue(q.memo.startswith('=:'))
        self.assertGreater(q.expected_amount_out, 0)
        self.assertGreater(q.expiry, 1_700_000_000)
        self.assertEqual(q.fee_total, q.fee_outbound + q.fee_liquidity + q.fee_affiliate)
        self.assertGreater(q.recommended_gas_rate, 0)

    def test_vault(self):
        v = Vault.from_json(load_fixture('vault.json'))
        self.assertTrue(v.accepts_inbound)
        self.assertEqual(v.status, 'ActiveVault')
        self.assertEqual(v.addresses['BTC'], 'bc1qrqrefwzvtv50r539hn7umufj84gm2xx9vz7ykw')
        # the vault's BTC address is the one its key controls, and the inbound fixture agrees
        self.assertEqual(vault_address_from_pubkey(v.pub_key), v.addresses['BTC'])
        inbound = next(d for d in load_fixture('inbound_addresses.json') if d['chain'] == 'BTC')
        self.assertEqual((inbound['pub_key'], inbound['address']), (v.pub_key, v.addresses['BTC']))
        for status in ('RetiringVault',):
            self.assertTrue(Vault(v.pub_key, 'AsgardVault', status, v.addresses).accepts_inbound)
        for status in ('InactiveVault', 'InitVault', '', 'active'):
            self.assertFalse(Vault(v.pub_key, 'AsgardVault', status, v.addresses).accepts_inbound)
        self.assertFalse(Vault(v.pub_key, '', 'ActiveVault', v.addresses).accepts_inbound)

    def test_pools(self):
        pools = [Pool.from_json(d) for d in load_fixture('pools.json')]
        available = {p.asset for p in pools if p.is_available}
        self.assertIn('ETH.ETH', available)
        self.assertIn('BTC.BTC', available)
        staged = [p for p in pools if p.status == 'Staged']
        self.assertTrue(all(not p.is_available for p in staged))


class TestUntrustedResponses(unittest.TestCase):

    def test_field_validation(self):
        good = load_fixture('quote_btc_eth.json')
        SwapQuote.from_json(good)
        for bad in ({'fees': None}, {'memo': 5}, {'expected_amount_out': '-1'}, {'expected_amount_out': True},
                    {'expiry': '1.5'}, {'inbound_address': ''}):
            with self.assertRaises(ThornodeError):
                SwapQuote.from_json({**good, **bad})
        with self.assertRaises(ThornodeError):
            SwapQuote.from_json(None)
        # booleans given as strings
        d = dict(load_fixture('inbound_addresses.json')[0], halted='true')
        self.assertTrue(InboundAddress.from_json(d).halted)
        d['halted'] = 'false'
        self.assertFalse(InboundAddress.from_json(d).halted)

    def test_vault_validation(self):
        good = load_fixture('vault.json')
        for bad in ({'pub_key': ''}, {'addresses': None}, {'addresses': [{'chain': 'BTC'}]},
                    {'addresses': [{'chain': 'BTC', 'address': 5}]}, {'addresses': ['bc1q...']}, {'status': 7}):
            with self.assertRaises(ThornodeError):
                Vault.from_json({**good, **bad})
        with self.assertRaises(ThornodeError):
            Vault.from_json([])
        # chains are keyed upper-case whatever the server sends
        v = Vault.from_json({**good, 'addresses': [{'chain': 'btc', 'address': 'x'}]})
        self.assertEqual(v.addresses, {'BTC': 'x'})

    def test_pool_asset_format(self):
        with self.assertRaises(ThornodeError):
            Pool.from_json({'asset': 'ETH'})
        with self.assertRaises(ThornodeError):
            Pool.from_json({'asset': '../x'})
        self.assertEqual(Pool.from_json({'asset': 'ETH.USDC-0XA0B8'}).asset, 'ETH.USDC-0XA0B8')
        self.assertIsNone(Pool.from_json({'asset': 'ETH.ETH'}).usd_value(1))

    def test_server_text_is_sanitised(self):
        text = _clean_text('<b>x</b>\x00\x1b[31m' + 'y' * 1000)
        self.assertNotIn('\x00', text)
        self.assertNotIn('\x1b', text)
        self.assertLessEqual(len(text), 310)

    def test_url_validation(self):
        self.assertEqual(validate_thornode_url(' https://a.b/c/ '), 'https://a.b/c')
        self.assertEqual(validate_thornode_url('http://localhost:1317'), 'http://localhost:1317')
        self.assertEqual(validate_thornode_url('http://x.onion'), 'http://x.onion')
        self.assertEqual(validate_thornode_url('https://[::1]:1317/x'), 'https://[::1]:1317/x')
        for bad in ('http://a.b', 'ftp://a.b', 'a.b', '', 'https://a.b/?x=1', 'https://a.b/#f', 'https://a.b?',
                    'https://user:pw@a.b', 'https://a.b/pa th', 'HTTPS://a b'):
            with self.assertRaises(ThornodeError):
                validate_thornode_url(bad)


class TestHelpers(unittest.TestCase):

    def test_asset_chain(self):
        self.assertEqual(asset_chain('ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48'), 'ETH')
        self.assertEqual(asset_chain('GAIA.ATOM'), 'GAIA')
        with self.assertRaises(ValueError):
            asset_chain('ETH')

    def test_asset_display_name(self):
        pools = [Pool.from_json(d) for d in load_fixture('pools.json')]
        self.assertEqual(asset_display_name('ETH.ETH', pools), 'ETH.ETH')
        self.assertEqual(asset_display_name('ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48', pools), 'ETH.USDC')
        self.assertEqual(asset_display_name('BSC.USDT-0X55D398326F99059FF775485246999027B3197955', pools), 'BSC.USDT')
        # without a pool list the address is simply dropped
        self.assertEqual(asset_display_name('BSC.USDT-0X55D398326F99059FF775485246999027B3197955'), 'BSC.USDT')
        # two pools sharing chain and ticker get a disambiguating suffix
        twins = [Pool('ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7', '', 'Available', False, 1, 1),
                 Pool('ETH.USDT-0X1111111111111111111111111111111111111111', '', 'Available', False, 1, 1)]
        self.assertEqual(asset_display_name(twins[0].asset, twins), 'ETH.USDT-831EC7')
        self.assertEqual(asset_display_name(twins[1].asset, twins), 'ETH.USDT-111111')
        # every available pool gets a unique display name
        names = [asset_display_name(p.asset, pools) for p in pools if p.is_available]
        self.assertEqual(len(names), len(set(names)))

    def test_vault_address_from_pubkey(self):
        for d in load_fixture('inbound_addresses.json'):
            if d['chain'] != 'BTC':
                continue
            self.assertEqual(vault_address_from_pubkey(d['pub_key']), d['address'])
        # a known pair from a different vault
        self.assertEqual(
            vault_address_from_pubkey('thorpub1addwnpepqva65k2jxjvafm6dufxh8zca2ghue4hsrwgx27tzjyj5e3uagqku7ql58h5'),
            'bc1q704ak5tqw547nql6m65rjyaqgryq6h54d6ks2s')
        with self.assertRaises(ValueError):
            vault_address_from_pubkey('bc1q704ak5tqw547nql6m65rjyaqgryq6h54d6ks2s')

    def test_halt_reasons_live_fixture(self):
        mimir = {k.upper(): int(v) for k, v in load_fixture('mimir.json').items()}
        height = next(i['thorchain'] for i in load_fixture('lastblock.json') if 'thorchain' in i)
        # at capture time BTC and ETH were fully open
        self.assertEqual(halt_reasons(mimir, height, 'ETH'), [])
        # SOL trading was halted at capture time
        self.assertTrue(any('SOL trading' in r for r in halt_reasons(mimir, height, 'SOL')))

    def test_halt_reasons_semantics(self):
        height = 1000
        self.assertEqual(halt_reasons({}, height, 'ETH'), [])
        self.assertEqual(halt_reasons({'HALTETHCHAIN': 0}, height, 'ETH'), [])
        # a halt scheduled in the future is not active yet
        self.assertEqual(halt_reasons({'HALTETHCHAIN': 5000}, height, 'ETH'), [])
        self.assertEqual(len(halt_reasons({'HALTETHCHAIN': 1}, height, 'ETH')), 1)
        self.assertEqual(len(halt_reasons({'HALTETHCHAIN': 1000}, height, 'ETH')), 1)
        # chain-specific keys are only looked at for the involved chains
        self.assertEqual(halt_reasons({'HALTLTCCHAIN': 1}, height, 'ETH'), [])
        self.assertEqual(len(halt_reasons({'HALTBTCCHAIN': 1, 'HALTSIGNINGETH': 1, 'HALTTRADING': 1}, height, 'ETH')), 3)
        # lower-case destination chain is accepted
        self.assertEqual(len(halt_reasons({'HALTETHTRADING': 1}, height, 'eth')), 1)


if __name__ == '__main__':
    unittest.main()
