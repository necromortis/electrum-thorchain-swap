import json
import os
import shutil
import tempfile
import unittest

from electrum.simple_config import SimpleConfig

import thorchain_swap  # noqa: F401  (registers the plugin's ConfigVars)
from thorchain_swap.thorchain_swap import SwapStatus, ThorchainSwapPlugin, interpret_tx_status
from thorchain_swap.thornode import ThornodeRequestRejected

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class TestInterpretTxStatus(unittest.TestCase):

    def test_unknown_txid(self):
        s = interpret_tx_status(load_fixture('tx_status_unknown.json'))
        self.assertFalse(s.done)
        self.assertIn('observe', s.text)
        self.assertEqual(interpret_tx_status({}).done, False)

    def test_swapped_waiting_for_outbound(self):
        body = load_fixture('tx_status_swap_completed.json')
        # the recorded swap had already been paid out; roll it back to "outbound pending"
        body['stages']['outbound_signed'] = {'completed': False, 'scheduled_outbound_height': 1}
        body['out_txs'] = [o for o in body['out_txs'] if o['chain'] == 'THOR']  # affiliate payout only
        s = interpret_tx_status(body, destination='0xF815288A6e8ccf5366e952181BEe29424df93fe7')
        self.assertFalse(s.done)
        self.assertIn('outbound', s.text)

    def test_completed(self):
        body = load_fixture('tx_status_swap_completed.json')
        eth_out = next(o for o in body['out_txs'] if o['chain'] == 'ETH')
        # destination comparison is case-insensitive (EVM addresses)
        s = interpret_tx_status(body, destination=eth_out['to_address'].lower())
        self.assertEqual((s.done, s.result, s.out_txid), (True, 'completed', eth_out['id']))
        # an OUT to someone else is not our delivery
        s = interpret_tx_status(body, destination='0x0000000000000000000000000000000000000000')
        self.assertEqual((s.done, s.result, s.out_txid), (True, 'completed', ''))

    def test_partial_refund_and_full_refund(self):
        body = load_fixture('tx_status_refund.json')
        s = interpret_tx_status(body, destination='0x0DC4a2e7960Cac8E643CBE722ebc7a1278F812DD')
        self.assertTrue(s.done)
        self.assertEqual(s.result, 'partially refunded')
        body['out_txs'] = [o for o in body['out_txs'] if o['memo'].startswith('REFUND')]
        s = interpret_tx_status(body, destination='0x0DC4a2e7960Cac8E643CBE722ebc7a1278F812DD')
        self.assertEqual((s.done, s.result), (True, 'refunded'))
        self.assertTrue(s.out_txid)

    def test_intermediate_stages(self):
        body = {'stages': {'inbound_observed': {'completed': True},
                           'inbound_confirmation_counted': {'completed': False, 'remaining_confirmation_seconds': 540}}}
        s = interpret_tx_status(body)
        self.assertFalse(s.done)
        self.assertIn('9 min', s.text)
        body['stages']['inbound_confirmation_counted'] = {'completed': True}
        body['stages']['inbound_finalised'] = {'completed': True}
        body['stages']['swap_status'] = {'pending': True, 'streaming': {'count': 3, 'quantity': 10, 'interval': 1}}
        s = interpret_tx_status(body)
        self.assertIn('3 / 10 sub-swaps', s.text)
        self.assertIn('1 min left', s.text)  # 7 sub-swaps x 1 block x 6 s
        self.assertTrue(0.3 < s.progress < 0.85)
        body['stages']['swap_status'] = {'pending': True, 'streaming': {'count': 100, 'quantity': 1797, 'interval': 0}}
        self.assertIn('h left', interpret_tx_status(body).text)  # 1697 x 6 s > 1 h
        body['stages']['swap_status'] = {'pending': False}
        body['stages']['swap_finalised'] = {'completed': True}
        body['planned_out_txs'] = [{'chain': 'BTC', 'refund': True}]
        self.assertIn('refund', interpret_tx_status(body).text)


class FakeStatusClient:
    def __init__(self, bodies):
        self.bodies = bodies

    async def get_tx_status(self, txid):
        body = self.bodies[txid]
        if isinstance(body, Exception):
            raise body
        return body


class TestFetchStatuses(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='thorchain-swap-unittest-')
        self.config = SimpleConfig({'electrum_path': self.tmpdir})
        self.plugin = ThorchainSwapPlugin(None, self.config, 'thorchain_swap')

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    async def test_errors_become_status_text(self):
        bodies = {
            'A' * 64: load_fixture('tx_status_unknown.json'),
            'B' * 64: ThornodeRequestRejected('boom', status=500),
            'C' * 64: None,  # malformed
        }
        self.plugin.get_client = lambda: FakeStatusClient(bodies)
        statuses = await self.plugin.fetch_swap_statuses([{'txid': t, 'destination': 'x'} for t in bodies])
        self.assertEqual(set(statuses), set(bodies))
        self.assertFalse(any(s.done for s in statuses.values()))
        self.assertIn('boom', statuses['B' * 64].text)
        self.assertTrue(all(isinstance(s, SwapStatus) for s in statuses.values()))


if __name__ == '__main__':
    unittest.main()
