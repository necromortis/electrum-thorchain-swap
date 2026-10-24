import asyncio
import shutil
import tempfile
import unittest

from electrum import constants, keystore, util
from electrum.address_synchronizer import TX_HEIGHT_UNCONFIRMED
from electrum.bitcoin import opcodes
from electrum.fee_policy import FixedFeePolicy
from electrum.simple_config import SimpleConfig
from electrum.transaction import Transaction, PartialTxOutput
from electrum.util import NotEnoughFunds
from electrum.wallet import Standard_Wallet
from electrum.wallet_db import WalletDB

from thorchain_swap.memo import memo_script
from thorchain_swap.swap_tx import (
    MIN_SWAP_SATS, SwapParams, SwapTxError, build_swap_outputs, make_swap_tx, verify_swap_tx,
)

LOCAL_HEIGHT = 900_000

# testnet p2wpkh wallet and its funding tx, borrowed from Electrum's own test-suite
SEED = 'bitter grass shiver impose acquire brush forget axis eager alone wine silver'
FUNDING_TX = Transaction(
    '01000000014576dacce264c24d81887642b726f5d64aa7825b21b350c7b75a57f337da6845010000006b483045022100a3f8b6155c71a98ad9'
    '986edd6161b20d24fad99b6463c23b463856c0ee54826d02200f606017fd987696ebbe5200daedde922eee264325a184d5bbda965ba516082101'
    '2102e5c473c051dae31043c335266d0ef89c1daab2f34d885cc7706b267f3269c609ffffffff0240420f00000000001600148a28bddb7f61864b'
    'dcf58b2ad13d5aeb3abc3c42a2ddb90e000000001976a914c384950342cb6f8df55175b48586838b03130fad88ac00000000')
FUNDING_VALUE = 1_000_000
VAULT = 'tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx'  # BIP173 example address, stands in for an Asgard vault
MEMO = '=:e:0x742d35Cc6634C0532925a3b844Bc454e4438f44e:33775077'
STRANGER = 'tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7'


class SwapTxTestCase(unittest.IsolatedAsyncioTestCase):
    """Minimal stand-in for Electrum's ElectrumTestCase: testnet + event loop + temp config."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        constants.BitcoinTestnet.set_as_network()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        constants.BitcoinMainnet.set_as_network()

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp(prefix='thorchain-swap-unittest-')
        self.config = SimpleConfig({'electrum_path': self.tmpdir})

    async def asyncSetUp(self):
        await super().asyncSetUp()
        util._asyncio_event_loop = asyncio.get_running_loop()

    def tearDown(self):
        util.callback_mgr.clear_all_callbacks()
        shutil.rmtree(self.tmpdir)
        super().tearDown()
        util._asyncio_event_loop = None

    def create_wallet(self) -> Standard_Wallet:
        ks = keystore.from_seed(SEED, passphrase='', for_multisig=False)
        db = WalletDB('', storage=None, upgrade=True)
        db.put('keystore', ks.dump())
        db.put('gap_limit', 2)
        db.put('gap_limit_for_change', 2)
        w = Standard_Wallet(db, config=self.config)
        w.synchronize()
        w.adb.receive_tx_callback(FUNDING_TX, tx_height=TX_HEIGHT_UNCONFIRMED)
        # offline test wallet has no headers; the swap builder refuses an unknown height
        w.adb.get_local_height = lambda: LOCAL_HEIGHT
        return w

    def split_coins(self, wallet: Standard_Wallet, amounts):
        """Spend the single funding UTXO into several wallet UTXOs so later txs need multiple inputs."""
        outputs = [PartialTxOutput.from_address_and_value(addr, amt)
                   for addr, amt in zip(wallet.get_receiving_addresses(), amounts)]
        tx = wallet.make_unsigned_transaction(outputs=outputs, fee_policy=FixedFeePolicy(1000), rbf=False)
        wallet.sign_transaction(tx, password=None)
        self.assertTrue(tx.is_complete())
        wallet.adb.receive_tx_callback(tx, tx_height=TX_HEIGHT_UNCONFIRMED)
        return tx


class TestBuildOutputs(unittest.TestCase):

    def test_outputs(self):
        constants.BitcoinTestnet.set_as_network()
        try:
            params = SwapParams(vault_address=VAULT, amount_sats=250_000, memo=MEMO)
            outputs = build_swap_outputs(params)
            self.assertEqual(len(outputs), 2)
            self.assertEqual(outputs[0].address, VAULT)
            self.assertEqual(outputs[0].value, 250_000)
            self.assertEqual(outputs[1].value, 0)
            self.assertEqual(outputs[1].scriptpubkey, memo_script(MEMO))
            self.assertEqual(outputs[1].scriptpubkey[0], opcodes.OP_RETURN)
            with self.assertRaises(SwapTxError):
                build_swap_outputs(SwapParams(vault_address=VAULT, amount_sats=0, memo=MEMO))
        finally:
            constants.BitcoinMainnet.set_as_network()


class TestMakeSwapTx(SwapTxTestCase):

    async def test_single_input_with_change(self):
        wallet = self.create_wallet()
        params = SwapParams(vault_address=VAULT, amount_sats=250_000, memo=MEMO)
        tx = make_swap_tx(wallet, params=params, fee_policy=FixedFeePolicy(5000))
        self.assertEqual(len(tx.inputs()), 1)
        outputs = tx.outputs()
        self.assertEqual(len(outputs), 3)
        self.assertEqual(outputs[0].address, VAULT)
        self.assertEqual(outputs[0].value, 250_000)
        self.assertTrue(wallet.is_mine(outputs[1].address))
        self.assertEqual(outputs[1].value, FUNDING_VALUE - 250_000 - 5000)
        self.assertEqual(outputs[2].scriptpubkey, memo_script(MEMO))
        self.assertEqual(outputs[2].value, 0)
        self.assertEqual(tx.get_fee(), 5000)
        # rbf is off by default: Bifrost only observes RBF transactions once mined
        self.assertFalse(tx.is_rbf_enabled())
        # the result signs and serializes like any other Electrum transaction
        wallet.sign_transaction(tx, password=None)
        self.assertTrue(tx.is_complete())

    async def test_multiple_inputs_all_mine(self):
        wallet = self.create_wallet()
        self.split_coins(wallet, [300_000, 300_000])
        params = SwapParams(vault_address=VAULT, amount_sats=550_000, memo=MEMO)
        tx = make_swap_tx(wallet, params=params, fee_policy=FixedFeePolicy(2000))
        self.assertGreaterEqual(len(tx.inputs()), 2)
        for txin in tx.inputs():
            self.assertTrue(wallet.is_mine(txin.address))
        self.assertEqual(tx.outputs()[0].address, VAULT)
        self.assertEqual(tx.outputs()[-1].scriptpubkey, memo_script(MEMO))

    async def test_no_change_when_spending_everything(self):
        wallet = self.create_wallet()
        params = SwapParams(vault_address=VAULT, amount_sats=FUNDING_VALUE - 5000, memo=MEMO)
        tx = make_swap_tx(wallet, params=params, fee_policy=FixedFeePolicy(5000))
        self.assertEqual(len(tx.outputs()), 2)
        self.assertEqual(tx.outputs()[0].address, VAULT)
        self.assertEqual(tx.outputs()[1].scriptpubkey, memo_script(MEMO))

    async def test_not_enough_funds(self):
        wallet = self.create_wallet()
        params = SwapParams(vault_address=VAULT, amount_sats=FUNDING_VALUE, memo=MEMO)
        with self.assertRaises(NotEnoughFunds):
            make_swap_tx(wallet, params=params, fee_policy=FixedFeePolicy(5000))

    async def test_rbf_opt_in(self):
        wallet = self.create_wallet()
        params = SwapParams(vault_address=VAULT, amount_sats=100_000, memo=MEMO)
        tx = make_swap_tx(wallet, params=params, fee_policy=FixedFeePolicy(5000), rbf=True)
        self.assertTrue(tx.is_rbf_enabled())


class TestVerifySwapTx(SwapTxTestCase):

    def setUp(self):
        super().setUp()
        self.params = SwapParams(vault_address=VAULT, amount_sats=250_000, memo=MEMO)

    def good_tx(self, wallet):
        return make_swap_tx(wallet, params=self.params, fee_policy=FixedFeePolicy(5000))

    def assertRejected(self, wallet, tx, local_height=LOCAL_HEIGHT):
        with self.assertRaises(SwapTxError):
            verify_swap_tx(wallet, tx, self.params, local_height=local_height)

    async def test_good_tx_passes(self):
        wallet = self.create_wallet()
        verify_swap_tx(wallet, self.good_tx(wallet), self.params, local_height=LOCAL_HEIGHT)

    async def test_wrong_amount(self):
        wallet = self.create_wallet()
        tx = self.good_tx(wallet)
        tx.outputs()[0].value += 1
        self.assertRejected(wallet, tx)

    async def test_duplicate_vault_output(self):
        wallet = self.create_wallet()
        tx = self.good_tx(wallet)
        tx._outputs.insert(1, PartialTxOutput.from_address_and_value(VAULT, 1000))
        self.assertRejected(wallet, tx)

    async def test_missing_or_wrong_memo(self):
        wallet = self.create_wallet()
        tx = self.good_tx(wallet)
        tx._outputs = [o for o in tx.outputs() if o.value != 0]
        self.assertRejected(wallet, tx)
        tx = self.good_tx(wallet)
        tx._outputs[-1] = PartialTxOutput(scriptpubkey=memo_script(MEMO + '0'), value=0)
        self.assertRejected(wallet, tx)
        tx = self.good_tx(wallet)
        tx._outputs.append(PartialTxOutput(scriptpubkey=memo_script(MEMO), value=0))
        self.assertRejected(wallet, tx)

    async def test_wrong_order(self):
        wallet = self.create_wallet()
        tx = self.good_tx(wallet)
        tx._outputs = [tx._outputs[1], tx._outputs[0], tx._outputs[2]]
        self.assertRejected(wallet, tx)
        tx = self.good_tx(wallet)
        tx._outputs = [tx._outputs[0], tx._outputs[2], tx._outputs[1]]
        self.assertRejected(wallet, tx)

    async def test_foreign_change_output(self):
        wallet = self.create_wallet()
        tx = self.good_tx(wallet)
        tx._outputs[1] = PartialTxOutput.from_address_and_value(STRANGER, tx._outputs[1].value)
        self.assertRejected(wallet, tx)

    async def test_too_many_outputs(self):
        wallet = self.create_wallet()
        tx = self.good_tx(wallet)
        tx._outputs.insert(1, PartialTxOutput.from_address_and_value(wallet.get_receiving_addresses()[1], 1000))
        self.assertRejected(wallet, tx)

    async def test_foreign_input(self):
        wallet = self.create_wallet()
        tx = self.good_tx(wallet)
        # pretend vin 0 was not ours (name-mangled attribute of TxInput)
        tx.inputs()[0]._TxInput__address = STRANGER
        self.assertRejected(wallet, tx)

    async def test_locktime_in_the_future(self):
        wallet = self.create_wallet()
        tx = self.good_tx(wallet)
        tx.locktime = 900_000
        self.assertRejected(wallet, tx, local_height=899_999)
        # equal to the current height is fine
        verify_swap_tx(wallet, tx, self.params, local_height=900_000)
        # unknown height is not "anything goes": refuse until synced
        self.assertRejected(wallet, tx, local_height=0)

    async def test_timestamp_locktime_rejected(self):
        # Bifrost drops every tx with locktime > height; a timestamp always is (confirm dialog can set one)
        wallet = self.create_wallet()
        tx = self.good_tx(wallet)
        tx.locktime = 1_700_000_000
        self.assertRejected(wallet, tx, local_height=LOCAL_HEIGHT)
        self.assertRejected(wallet, tx, local_height=2_000_000_000)

    async def test_amount_below_minimum(self):
        wallet = self.create_wallet()
        params = SwapParams(vault_address=VAULT, amount_sats=MIN_SWAP_SATS - 1, memo=MEMO)
        with self.assertRaises(SwapTxError):
            make_swap_tx(wallet, params=params, fee_policy=FixedFeePolicy(5000))
        with self.assertRaises(SwapTxError):
            build_swap_outputs(params)
        params = SwapParams(vault_address=VAULT, amount_sats=MIN_SWAP_SATS, memo=MEMO)
        make_swap_tx(wallet, params=params, fee_policy=FixedFeePolicy(5000))

    async def test_multiple_change_setting_still_single_change(self):
        # the "use multiple change addresses" wallet setting would split the change into
        # several outputs; the swap builder pins a single change address instead
        wallet = self.create_wallet()
        wallet.multiple_change = True
        params = SwapParams(vault_address=VAULT, amount_sats=MIN_SWAP_SATS, memo=MEMO)
        tx = make_swap_tx(wallet, params=params, fee_policy=FixedFeePolicy(1000))
        self.assertEqual(len(tx.outputs()), 3)
        self.assertTrue(wallet.is_mine(tx.outputs()[1].address))


if __name__ == '__main__':
    unittest.main()
