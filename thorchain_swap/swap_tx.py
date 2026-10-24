# Copyright (C) 2026 necromortis
# Distributed under the MIT software license, see the accompanying LICENSE file.
"""Build and verify the Bitcoin transaction that funds a THORChain swap.

Layout of a correct swap transaction (as documented by THORChain):

    vout 0: inbound vault address, swap amount
    vout 1: change back to this wallet (optional)
    vout 2: OP_RETURN memo, zero value

Bifrost identifies the sender by vin 0, so every input must belong to this
wallet, otherwise a refund would go to a stranger. Bifrost also ignores
transactions whose locktime is above the current block height, which includes
every timestamp locktime, so those are rejected unconditionally.
"""
from dataclasses import dataclass
from typing import List, Optional, Sequence, TYPE_CHECKING

from electrum import bitcoin
from electrum.bitcoin import NLOCKTIME_BLOCKHEIGHT_MAX
from electrum.i18n import _
from electrum.transaction import PartialTransaction, PartialTxOutput

from .memo import memo_script

if TYPE_CHECKING:
    from electrum.fee_policy import FeePolicy
    from electrum.transaction import PartialTxInput
    from electrum.wallet import Abstract_Wallet


MAX_OUTPUTS = 3
# Bifrost drops vault outputs below the chain's dust threshold (1 000 sat for BTC at the
# time of writing; the documentation says 10 000). Nothing below this is ever built.
MIN_SWAP_SATS = 10_000


class SwapTxError(Exception):
    pass


@dataclass(frozen=True)
class SwapParams:
    vault_address: str
    amount_sats: int
    memo: str

    @property
    def vault_script(self) -> bytes:
        return bitcoin.address_to_script(self.vault_address)

    @property
    def memo_script(self) -> bytes:
        return memo_script(self.memo)


def build_swap_outputs(params: SwapParams) -> List[PartialTxOutput]:
    if params.amount_sats < MIN_SWAP_SATS:
        raise SwapTxError(_("Swap amount must be at least {} sat").format(MIN_SWAP_SATS))
    return [
        PartialTxOutput(scriptpubkey=params.vault_script, value=params.amount_sats),
        PartialTxOutput(scriptpubkey=params.memo_script, value=0),
    ]


def _reorder_outputs(tx: PartialTransaction, params: SwapParams) -> None:
    """Put the vault output first and the OP_RETURN last, change in between (in place)."""
    vault, memo, rest = [], [], []
    for o in tx.outputs():
        if o.scriptpubkey == params.vault_script:
            vault.append(o)
        elif o.scriptpubkey == params.memo_script:
            memo.append(o)
        else:
            rest.append(o)
    # note: same private access as PartialTransaction.BIP69_sort(); there is no public reorder API
    tx._outputs = vault + rest + memo
    tx.invalidate_ser_cache()


def make_swap_tx(
        wallet: 'Abstract_Wallet',
        *,
        params: SwapParams,
        fee_policy: 'FeePolicy',
        coins: Optional[Sequence['PartialTxInput']] = None,
        rbf: bool = False,
) -> PartialTransaction:
    """Create the unsigned swap transaction. Raises NotEnoughFunds or SwapTxError."""
    # A single change address: the "use multiple change addresses" wallet setting would
    # otherwise split the change and push the output count past what THORChain accepts.
    change_addrs = wallet.get_change_addresses_for_new_transaction()
    tx = wallet.make_unsigned_transaction(
        coins=coins,
        outputs=build_swap_outputs(params),
        fee_policy=fee_policy,
        change_addr=change_addrs[0] if change_addrs else None,
        rbf=rbf,
        BIP69_sort=False,
        merge_duplicate_outputs=False,
        send_change_to_lightning=False,
    )
    _reorder_outputs(tx, params)
    verify_swap_tx(wallet, tx, params, local_height=wallet.adb.get_local_height())
    return tx


def verify_swap_tx(
        wallet: 'Abstract_Wallet',
        tx: PartialTransaction,
        params: SwapParams,
        *,
        local_height: int,
) -> None:
    """Check every invariant THORChain relies on. Raises SwapTxError on the first violation.

    This is deliberately strict: a transaction that fails here must never be
    signed, because the failure modes on the THORChain side are silent loss.
    It is run once when the transaction is built and again on whatever comes
    back from Electrum's confirmation dialog, which can edit the transaction.
    """
    if params.amount_sats < MIN_SWAP_SATS:
        raise SwapTxError(_("Swap amount must be at least {} sat").format(MIN_SWAP_SATS))
    outputs = tx.outputs()
    if not 2 <= len(outputs) <= MAX_OUTPUTS:
        raise SwapTxError(_("Swap transaction must have 2 or 3 outputs, got {}").format(len(outputs)))
    vault_outputs = [o for o in outputs if o.scriptpubkey == params.vault_script]
    if len(vault_outputs) != 1:
        raise SwapTxError(_("Swap transaction must pay the vault exactly once, found {} outputs").format(len(vault_outputs)))
    if vault_outputs[0].value != params.amount_sats:
        raise SwapTxError(_("Vault output pays {} sat instead of {} sat").format(vault_outputs[0].value, params.amount_sats))
    memo_outputs = [o for o in outputs if o.scriptpubkey.startswith(bytes([bitcoin.opcodes.OP_RETURN]))]
    if len(memo_outputs) != 1 or memo_outputs[0].scriptpubkey != params.memo_script:
        raise SwapTxError(_("Swap transaction must carry exactly one OP_RETURN output with the swap memo"))
    if memo_outputs[0].value != 0:
        raise SwapTxError(_("OP_RETURN output must have zero value"))
    if outputs[0] is not vault_outputs[0]:
        raise SwapTxError(_("Vault output must be the first output"))
    if outputs[-1] is not memo_outputs[0]:
        raise SwapTxError(_("OP_RETURN output must be the last output"))
    for o in outputs:
        if o is vault_outputs[0] or o is memo_outputs[0]:
            continue
        if o.address is None or not wallet.is_mine(o.address):
            raise SwapTxError(_("Unexpected output to {}").format(o.address or o.scriptpubkey.hex()))
    inputs = tx.inputs()
    if not inputs:
        raise SwapTxError(_("Swap transaction has no inputs"))
    for txin in inputs:
        if txin.address is None or not wallet.is_mine(txin.address):
            raise SwapTxError(_("Input {} does not belong to this wallet; THORChain would refund to it").format(txin.prevout.to_str()))
    if tx.locktime > NLOCKTIME_BLOCKHEIGHT_MAX:
        raise SwapTxError(_("Timestamp locktimes are not accepted by THORChain"))
    if local_height <= 0:
        raise SwapTxError(_("Block height unknown; wait until the wallet is synchronized"))
    if tx.locktime > local_height:
        raise SwapTxError(_("Locktime {} is above the current height {}; THORChain would ignore the transaction").format(tx.locktime, local_height))
