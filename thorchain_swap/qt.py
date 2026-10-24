# Copyright (C) 2026 necromortis
# Distributed under the MIT software license, see the accompanying LICENSE file.
from typing import Dict, Optional, TYPE_CHECKING

from PyQt6.QtGui import QIcon

from electrum.i18n import _
from electrum.plugin import hook
from electrum.gui.qt.util import read_QIcon_from_bytes

from .swap_tab import ResultRelay, SwapTab
from .thorchain_swap import ThorchainSwapPlugin

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow
    from electrum.wallet import Abstract_Wallet


class Plugin(ThorchainSwapPlugin):
    """Adds a "Swap" tab to every wallet window."""

    def __init__(self, *args):
        ThorchainSwapPlugin.__init__(self, *args)
        self._tabs = {}  # type: Dict[ElectrumWindow, SwapTab]
        self._relay = None  # type: Optional[ResultRelay]

    @property
    def relay(self) -> ResultRelay:
        """Long-lived QObject that carries coroutine results to the GUI thread (see SwapTab._run)."""
        if self._relay is None:
            self._relay = ResultRelay()
        return self._relay

    @hook
    def load_wallet(self, wallet: 'Abstract_Wallet', window: 'ElectrumWindow'):
        self._add_tab(window)

    @hook
    def close_wallet(self, wallet: 'Abstract_Wallet'):
        for window in [w for w, t in self._tabs.items() if t.wallet is wallet]:
            self._remove_tab(window)

    @hook
    def on_close_window(self, window: 'ElectrumWindow'):
        self._remove_tab(window)

    def on_close(self):
        # plugin disabled from the Plugins dialog
        for window in list(self._tabs):
            self._remove_tab(window)

    def _add_tab(self, window: 'ElectrumWindow'):
        if window in self._tabs:
            return
        # read the icon first: a tab must not be created (and start quoting) if this fails
        try:
            icon = read_QIcon_from_bytes(self.read_file('thorchain.png'))
        except Exception:  # noqa: BLE001
            self.logger.warning('plugin icon missing, using a blank one')
            icon = QIcon()
        tab = SwapTab(self, window)
        self._tabs[window] = tab
        # Always the last tab. ElectrumWindow.toggle_tab() places optional tabs by
        # comparing tab_pos, so a large value keeps ours behind them.
        tab.tab_pos = 1000
        window.tabs.addTab(tab, icon, _('Swap'))

    def _remove_tab(self, window: 'ElectrumWindow'):
        tab = self._tabs.pop(window, None)
        if tab is None:
            return
        tab.clean_up()
        index = window.tabs.indexOf(tab)
        if index != -1:
            window.tabs.removeTab(index)
        tab.deleteLater()
