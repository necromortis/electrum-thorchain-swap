# Copyright (C) 2026 necromortis
# Distributed under the MIT software license, see the accompanying LICENSE file.
#
# This module is loaded before any GUI module of the plugin, so it only
# declares configuration variables and never imports Qt.
from electrum.simple_config import ConfigVar, SimpleConfig

plugin_name = "thorchain_swap"

DEFAULT_THORNODE_URL = "https://gateway.liquify.com/chain/thorchain_api"
# operated by a different party than the default THORNode above; see ThorchainSwapPlugin._verify_vault
DEFAULT_VERIFY_URL = "https://public-thornode.nativeswap.io"

# note: external plugins are imported as "electrum_external_plugins.<name>",
#       which contains a dot, so ConfigVar gets the literal plugin name
#       instead of __name__.
SimpleConfig.THORCHAIN_SWAP_THORNODE_URL = ConfigVar(
    'plugins.thorchain_swap.thornode_url', default=DEFAULT_THORNODE_URL, type_=str, plugin=plugin_name)
# second, independently run THORNode that must confirm the inbound vault address;
# it only receives the vault's public key. An empty value disables the check.
SimpleConfig.THORCHAIN_SWAP_VERIFY_URL = ConfigVar(
    'plugins.thorchain_swap.verify_url', default=DEFAULT_VERIFY_URL, type_=str, plugin=plugin_name)
# maximum accepted slippage against the quoted amount, in basis points
SimpleConfig.THORCHAIN_SWAP_SLIPPAGE_BPS = ConfigVar(
    'plugins.thorchain_swap.slippage_bps', default=100, type_=int, plugin=plugin_name)
# streaming swap: blocks between sub-swaps (0 = several sub-swaps per block)
SimpleConfig.THORCHAIN_SWAP_STREAMING_INTERVAL = ConfigVar(
    'plugins.thorchain_swap.streaming_interval', default=0, type_=int, plugin=plugin_name)
# streaming swap: number of sub-swaps (0 = let THORChain decide)
SimpleConfig.THORCHAIN_SWAP_STREAMING_QUANTITY = ConfigVar(
    'plugins.thorchain_swap.streaming_quantity', default=0, type_=int, plugin=plugin_name)
# asset of the last swap sent; preselected in the asset list
SimpleConfig.THORCHAIN_SWAP_LAST_ASSET = ConfigVar(
    'plugins.thorchain_swap.last_asset', default='', type_=str, plugin=plugin_name)
# plugin fee, collected by THORChain for the affiliate THORName. Not exposed in the GUI;
# an empty name disables the fee (useful while the name is not registered yet).
SimpleConfig.THORCHAIN_SWAP_AFFILIATE_THORNAME = ConfigVar(
    'plugins.thorchain_swap.affiliate_thorname', default='el', type_=str, plugin=plugin_name)
SimpleConfig.THORCHAIN_SWAP_AFFILIATE_BPS = ConfigVar(
    'plugins.thorchain_swap.affiliate_bps', default=10, type_=int, plugin=plugin_name)
# swaps worth less than this (USD, by THORChain's own BTC price) are not charged the plugin fee
SimpleConfig.THORCHAIN_SWAP_AFFILIATE_MIN_USD = ConfigVar(
    'plugins.thorchain_swap.affiliate_min_usd', default=100, type_=int, plugin=plugin_name)
