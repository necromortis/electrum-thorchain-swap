# Copyright (C) 2026 necromortis
# Distributed under the MIT software license, see the accompanying LICENSE file.
"""The "Swap" tab shown in the main window next to History / Send / Receive.

Quotes are requested automatically once the amount, asset and destination are
filled in, refreshed every minute while the inputs stay the same, and can be
refreshed by hand. The offer shown is the only one that can be swapped: any
change of the inputs discards it, and the swap flow re-verifies the
transaction Electrum's confirmation dialog hands back.
"""
import datetime
import decimal
import math
import re
import textwrap
from decimal import Decimal
from functools import partial
from typing import Any, Callable, Optional, TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QProgressBar, QScrollArea, QSlider, QStyle, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from electrum.i18n import _
from electrum.logging import Logger
from electrum.network import TxBroadcastError, BestEffortRequestFailed
from electrum.util import NotEnoughFunds, NoDynamicFeeEstimates
from electrum.gui.qt.amountedit import BTCAmountEdit
from electrum.gui.qt.transaction_dialog import TxDialog
from electrum.gui.qt.util import (
    Buttons, CancelButton, CloseButton, ColorScheme, EnterButton, MessageBoxMixin, OkButton, WWLabel, WaitingDialog,
    WindowModalDialog, read_QIcon_from_bytes,
)

from . import DEFAULT_THORNODE_URL
from .swap_tx import SwapTxError, make_swap_tx, verify_swap_tx
from .thorchain_swap import SwapError, SwapOffer
from .thornode import asset_display_name

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow
    from .qt import Plugin


AUTO_REFRESH_MS = 60_000  # re-quote while the inputs stay the same
DEBOUNCE_MS = 700  # wait for the user to stop typing before quoting
PROBLEM_CHECK_MS = 5_000  # re-evaluate expiry / halts of the current offer
TRACK_INTERVAL_MS = 30_000  # poll THORChain for the status of swaps in progress

SLIPPAGE_PRESETS_PCT = [0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3, 3.5, 4, 4.5, 5, 6, 7, 8, 9, 10]
SUB_SWAP_PRESETS = [0, 1, 5, 10, 15, 20, 25, 30, 50, 100]  # 0 = automatic
INTERVAL_PRESETS = [0, 1, 2, 3]  # blocks between sub-swaps; 0 = several per block
TOOLTIP_WRAP_CHARS = 64


class ResultRelay(QObject):
    """Long-lived bridge from the network thread to the GUI thread.

    Coroutine results are emitted on this object, never on a tab: a tab can be
    deleted while a request is in flight, and emitting a signal of a deleted
    widget from another thread crashes the process. The relay is owned by the
    plugin and outlives every tab.
    """
    delivered = pyqtSignal(object, object, object)  # (callback, result, error)

    def __init__(self):
        QObject.__init__(self)
        self.delivered.connect(self._dispatch)

    @staticmethod
    def _dispatch(callback, result, error):
        callback(result, error)


def _icon_filename(asset: str) -> str:
    # must match contrib/fetch_icons.py
    return re.sub(r'[^a-z0-9.-]', '_', asset.lower()) + '.png'


class AssetIcons:
    """Icons bundled with the plugin (see contrib/fetch_icons.py), loaded lazily and cached."""

    def __init__(self, plugin: 'Plugin'):
        self.plugin = plugin
        self._cache = {}  # type: dict[str, QIcon]

    def get(self, asset: str) -> QIcon:
        key = asset.upper()
        if key not in self._cache:
            self._cache[key] = self._load(_icon_filename(asset)) or self._load('_unknown.png') or QIcon()
        return self._cache[key]

    def _load(self, filename: str) -> Optional[QIcon]:
        try:
            return read_QIcon_from_bytes(self.plugin.read_file('icons/' + filename))
        except Exception:  # noqa: BLE001 - a missing icon must never break the tab
            return None


def _decimals_for_price(price_usd: Optional[float]) -> int:
    """Decimals so that the last shown digit is worth roughly a cent.

    Stablecoins and cheap coins get 2, ETH-priced assets 5, BTC-priced 6.
    Unknown price falls back to 2.
    """
    if not price_usd or price_usd <= 0:
        return 2
    return max(2, min(8, math.ceil(math.log10(price_usd)) + 1))


def _format_asset_amount(amount_1e8: int, asset: str, price_usd: Optional[float] = None) -> str:
    ticker = asset.partition('.')[2].partition('-')[0] or asset
    decimals = _decimals_for_price(price_usd)
    with decimal.localcontext() as ctx:
        ctx.prec = 64  # server-provided magnitudes must not raise InvalidOperation
        step = Decimal(1).scaleb(-decimals)
        value = Decimal(int(amount_1e8)) / Decimal(10 ** 8)
        rounded = value.quantize(step)
    if value > 0 and rounded == 0:
        text = '<' + f'{step:f}'
    else:
        text = f'{rounded:f}'.rstrip('0').rstrip('.') or '0'
    return f'{text} {ticker}'


def _wrapped_tooltip(text: str) -> str:
    """Tooltip text broken into lines of a readable width.

    Qt shows plain-text tooltips on a single line (however long) and re-wraps
    rich text at a width of its own choosing, so the lines are broken here and
    the result stays plain text, which Qt renders verbatim.
    """
    return '\n\n'.join(
        textwrap.fill(paragraph.replace('\n', ' '), TOOLTIP_WRAP_CHARS)
        for paragraph in text.split('\n\n'))


def _info_icon(tooltip: str) -> QLabel:
    """Small information icon that shows its explanation on hover.

    Uses the style's standard icon, so it is visible on light and dark themes alike.
    """
    label = QLabel()
    icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
    label.setPixmap(icon.pixmap(16, 16))
    label.setToolTip(_wrapped_tooltip(tooltip))
    label.setCursor(Qt.CursorShape.WhatsThisCursor)
    return label


class PresetSlider(QWidget):
    """Horizontal slider over a fixed list of presets, with the current value shown next to it
    and optional captions under the ends/ticks, like the sliders on swap.thorchain.org."""

    def __init__(self, presets: list, format_value, *, captions: Optional[list] = None):
        QWidget.__init__(self)
        self.presets = presets
        self.format_value = format_value
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(2)
        row = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, len(presets) - 1)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.setTickInterval(1)
        self.slider.setPageStep(1)
        self.slider.valueChanged.connect(self._on_change)
        row.addWidget(self.slider, 1)
        self.value_label = QLabel()
        self.value_label.setMinimumWidth(90)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.value_label)
        vbox.addLayout(row)
        if captions:
            cap = QHBoxLayout()
            cap.setContentsMargins(0, 0, 90, 0)
            for i, text in enumerate(captions):
                label = QLabel(text)
                label.setStyleSheet('QLabel { font-size: 10px; }')
                if i == 0:
                    label.setAlignment(Qt.AlignmentFlag.AlignLeft)
                elif i == len(captions) - 1:
                    label.setAlignment(Qt.AlignmentFlag.AlignRight)
                else:
                    label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                cap.addWidget(label, 1)
            vbox.addLayout(cap)
        self._on_change(self.slider.value())

    def _on_change(self, index: int):
        self.value_label.setText(self.format_value(self.presets[index]))

    def value(self):
        return self.presets[self.slider.value()]

    def set_value(self, value) -> None:
        """Select the preset; a value outside the presets snaps to the nearest one."""
        index = min(range(len(self.presets)), key=lambda i: abs(self.presets[i] - value))
        self.slider.setValue(index)
        self._on_change(index)


def _labeled(text: str, tooltip: str) -> QHBoxLayout:
    """Field label followed by an info icon; the tooltip is on both."""
    hbox = QHBoxLayout()
    hbox.setSpacing(4)
    label = QLabel(text)
    label.setToolTip(_wrapped_tooltip(tooltip))
    hbox.addWidget(label)
    hbox.addWidget(_info_icon(tooltip))
    hbox.addStretch(1)
    return hbox


PRICE_PROTECTION_HELP = _(
    'The most you are willing to spend on a swap. This includes the price impact of your swap '
    'and any slippage on the token prices before the swap is executed.')
TWAP_HELP = _(
    'TWAP trades on THORChain are called "streaming swaps" in the documents and code.\n\n'
    'To place an instant sell order, which will result in the worst price execution, enter 1 for '
    'the number of sub-swaps and 1 block for the time between sub-swaps.')
TWAP_DESCRIPTION = _(
    'The vast majority of users should use the default settings. Only expert traders should change these settings')
SUB_SWAPS_HELP = _(
    'Break up your order into smaller trades called sub-swaps. The more sub-swaps, the better price '
    'execution. When set to zero, the protocol will calculate the number of sub-swaps for you so that '
    'each one will have a slippage of 5 bps. The default value is zero.')
INTERVAL_HELP = _(
    'Time between each sub-swap measured in blocks. The more blocks between each sub-swap, the better '
    'the price execution. Choosing zero blocks will do multiple sub-swaps every block for faster '
    'execution. The default value is zero blocks.')


class SwapSettingsDialog(WindowModalDialog):
    """Price protection and streaming (sub-swap) settings, modelled on swap.thorchain.org."""

    def __init__(self, parent: QWidget, plugin: 'Plugin'):
        WindowModalDialog.__init__(self, parent, _('Swap settings'))
        self.plugin = plugin
        self.config = plugin.config
        vbox = QVBoxLayout(self)
        vbox.setSpacing(10)
        self.setMinimumWidth(460)

        # --- price protection
        vbox.addLayout(_labeled(_('Price protection'), PRICE_PROTECTION_HELP))
        self.slippage_slider = PresetSlider(
            [int(round(pct * 100)) for pct in SLIPPAGE_PRESETS_PCT],
            lambda bps: f'{bps / 100:g} %',
            captions=[f'{SLIPPAGE_PRESETS_PCT[0]:g} %', f'{SLIPPAGE_PRESETS_PCT[-1]:g} %'])
        vbox.addWidget(self.slippage_slider)

        # --- TWAP / streaming
        title = QHBoxLayout()
        title.setSpacing(4)
        title_label = QLabel(_('Time Weighted Average Price (TWAP)'))
        title_label.setStyleSheet('QLabel { font-weight: bold; }')
        title_label.setToolTip(_wrapped_tooltip(TWAP_HELP))
        title.addWidget(title_label)
        title.addWidget(_info_icon(TWAP_HELP))
        title.addStretch(1)
        vbox.addSpacing(6)
        vbox.addLayout(title)
        description = WWLabel(TWAP_DESCRIPTION)
        description.setStyleSheet('QLabel { font-style: italic; }')
        vbox.addWidget(description)

        vbox.addLayout(_labeled(_('Number of sub-swaps'), SUB_SWAPS_HELP))
        self.quantity_slider = PresetSlider(
            SUB_SWAP_PRESETS,
            lambda n: _('automatic') if n == 0 else str(n),
            captions=[_('automatic'), str(SUB_SWAP_PRESETS[-1])])
        vbox.addWidget(self.quantity_slider)

        vbox.addLayout(_labeled(_('Time between sub-swaps'), INTERVAL_HELP))
        self.interval_slider = PresetSlider(
            INTERVAL_PRESETS,
            lambda b: _('{} blocks').format(b) if b != 1 else _('1 block'),
            captions=[_('0 blocks') + '\n' + _('Rapid Swap')]
                     + [_('{} block(s)').format(b) + '\n' + _('~{} seconds').format(b * 6) for b in INTERVAL_PRESETS[1:]])
        vbox.addWidget(self.interval_slider)

        reset_button = EnterButton(_('Reset to defaults'), self.reset)
        vbox.addSpacing(6)
        vbox.addLayout(Buttons(reset_button, CancelButton(self), OkButton(self)))
        self.load()
        # the two-line slider captions get clipped at the layout's own size hint
        self.setMinimumHeight(self.sizeHint().height() + 10)

    def load(self) -> None:
        settings = self.plugin.get_settings()
        self.slippage_slider.set_value(settings.slippage_bps)
        self.quantity_slider.set_value(settings.streaming_quantity)
        self.interval_slider.set_value(settings.streaming_interval)

    def reset(self) -> None:
        self.slippage_slider.set_value(100)
        self.quantity_slider.set_value(0)
        self.interval_slider.set_value(0)

    def save(self) -> None:
        # what is displayed is what gets persisted
        self.config.THORCHAIN_SWAP_SLIPPAGE_BPS = self.slippage_slider.value()
        self.config.THORCHAIN_SWAP_STREAMING_QUANTITY = self.quantity_slider.value()
        self.config.THORCHAIN_SWAP_STREAMING_INTERVAL = self.interval_slider.value()


class SwapCard(QFrame):
    """One swap in progress: amounts on top, a progress bar with the stage text below."""

    def __init__(self, *, amount_in: str, asset: str, amount_out: str, status: str, progress: float,
                 tooltip: str, on_open: Callable[[], None]):
        QFrame.__init__(self)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setToolTip(tooltip + '\n' + _('Double-click to open the Bitcoin transaction.'))
        self._on_open = on_open
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setVerticalSpacing(4)
        left = QLabel(amount_in)
        left.setStyleSheet('QLabel { font-weight: bold; }')
        arrow = QLabel('→')
        right = QLabel(f'{amount_out}')
        right.setStyleSheet('QLabel { font-weight: bold; }')
        right.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for label in (left, arrow, right):
            label.setTextFormat(Qt.TextFormat.PlainText)
        grid.addWidget(left, 0, 0)
        grid.addWidget(arrow, 0, 1)
        grid.addWidget(QLabel(asset), 0, 2)
        grid.addWidget(right, 0, 3)
        grid.setColumnStretch(3, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(int(max(0.0, min(1.0, progress)) * 1000))
        self.bar.setTextVisible(True)
        self.bar.setFormat(status)
        self.bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bar.setMaximumHeight(18)
        grid.addWidget(self.bar, 1, 0, 1, 4)

    def mouseDoubleClickEvent(self, event):
        self._on_open()
        QFrame.mouseDoubleClickEvent(self, event)


class SwapHistoryDialog(WindowModalDialog):
    """All swaps sent from this wallet, newest first, with the last known THORChain status."""

    COLUMNS = (_('Date'), _('Amount'), _('To'), _('Destination'), _('Status'), _('Txid'))

    def __init__(self, tab: 'SwapTab', plugin: 'Plugin', wallet):
        WindowModalDialog.__init__(self, tab.window, _('Swap history'))
        self.tab = tab
        self.plugin = plugin
        self.wallet = wallet
        self.setMinimumSize(760, 360)
        vbox = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLUMNS))
        self.tree.setHeaderLabels(list(self.COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemDoubleClicked.connect(self._open_tx)
        vbox.addWidget(self.tree)
        self.hint = QLabel(_('Double-click a swap to open its Bitcoin transaction.'))
        vbox.addWidget(self.hint)
        refresh = EnterButton(_('Refresh status'), self._refresh)
        vbox.addLayout(Buttons(refresh, CloseButton(self)))
        self.reload()

    def reload(self):
        self.tree.clear()
        records = self.plugin.get_swap_records(self.wallet)
        rows = sorted(records.items(), key=lambda kv: kv[1].get('created_at', 0), reverse=True)
        for txid, r in rows:
            created = datetime.datetime.fromtimestamp(int(r.get('created_at', 0))).strftime('%Y-%m-%d %H:%M')
            amount = self.tab.window.format_amount_and_units(int(r.get('amount_sats', 0)))
            if r.get('done'):
                status = r.get('status_text') or r.get('result') or _('finished')
            else:
                status = self.tab._local_status(dict(r, txid=txid))
            item = QTreeWidgetItem([created, amount, asset_display_name(r.get('to_asset', '')),
                                    str(r.get('destination', '')), status, txid])
            item.setData(0, Qt.ItemDataRole.UserRole, txid)
            self.tree.addTopLevelItem(item)
        if not rows:
            self.hint.setText(_('No swaps have been sent from this wallet yet.'))

    def _refresh(self):
        self.tab.track_pending_swaps()
        self.hint.setText(_('Status update requested; reopen the dialog in a moment.'))

    def _open_tx(self, item, _column):
        txid = item.data(0, Qt.ItemDataRole.UserRole)
        tx = self.wallet.adb.get_transaction(txid)
        if tx is None:
            self.show_error(_('Transaction {} is not in this wallet.').format(txid))
            return
        self.tab.window.show_transaction(tx)


class SwapTab(QWidget, MessageBoxMixin, Logger):

    def __init__(self, plugin: 'Plugin', window: 'ElectrumWindow'):
        QWidget.__init__(self, window)
        Logger.__init__(self)
        self.plugin = plugin
        self.window = window
        self.wallet = window.wallet
        self.config = window.config
        self.icons = AssetIcons(plugin)
        self.offer = None  # type: Optional[SwapOffer]
        self._request_id = 0
        self._busy = False
        self._frozen = False  # True while the user confirms/signs a swap
        self._closed = False  # set by clean_up(); callbacks must not touch widgets afterwards
        self._assets_error = ''  # sticky until the asset list loads
        self._amount_sats = None  # type: Optional[int]  # last parsed amount, survives unit changes

        vbox = QVBoxLayout(self)
        grid = QGridLayout()
        grid.setSpacing(8)
        grid.setColumnStretch(4, 1)

        grid.addWidget(QLabel(_('Amount')), 0, 0)
        self.amount_e = BTCAmountEdit(self.window.get_decimal_point)
        self.amount_e.textChanged.connect(self._on_amount_changed)
        self.amount_e.shortcut.connect(self._on_max_shortcut)
        grid.addWidget(self.amount_e, 0, 1)

        grid.addWidget(QLabel(_('Receive')), 0, 2)
        self.asset_combo = QComboBox()
        self.asset_combo.setMinimumWidth(180)
        self.asset_combo.setPlaceholderText(_('choose an asset'))
        self.asset_combo.currentIndexChanged.connect(self._on_asset_changed)
        grid.addWidget(self.asset_combo, 0, 3)

        tools = QHBoxLayout()
        tools.setSpacing(2)
        self.settings_button = self._tool_button('⚙', _('Swap settings'), self.show_settings)
        self.refresh_button = self._tool_button('↻', _('Refresh quote'), self.refresh_quote)
        tools.addWidget(self.settings_button)
        tools.addWidget(self.refresh_button)
        self.countdown_label = QLabel('')
        self.countdown_label.setToolTip(_('Seconds until the quote refreshes automatically'))
        self.countdown_label.setMinimumWidth(28)
        tools.addWidget(self.countdown_label)
        tools.addStretch(1)
        grid.addLayout(tools, 0, 4)

        grid.addWidget(QLabel(_('Destination')), 1, 0)
        self.destination_e = QLineEdit()
        self.destination_e.setPlaceholderText(_('Address on the destination chain'))
        self.destination_e.setMaxLength(200)
        self.destination_e.textChanged.connect(self._schedule_quote)
        grid.addWidget(self.destination_e, 1, 1, 1, 4)
        vbox.addLayout(grid)

        self.receive_label = QLabel('')
        self.receive_label.setTextFormat(Qt.TextFormat.PlainText)
        self.receive_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.fees_label = QLabel('')
        self.fees_label.setTextFormat(Qt.TextFormat.PlainText)
        self.fees_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.fees_info = _info_icon('')
        self.fees_info.setVisible(False)
        fees_row = QHBoxLayout()
        fees_row.setSpacing(4)
        fees_row.addWidget(self.fees_label)
        fees_row.addWidget(self.fees_info)
        fees_row.addStretch(1)
        vbox.addWidget(self.receive_label)
        vbox.addLayout(fees_row)

        buttons = QHBoxLayout()
        self.swap_button = EnterButton(_('Swap'), self.do_swap)
        self.swap_button.setEnabled(False)
        buttons.addWidget(self.swap_button)
        self.history_button = EnterButton(_('History'), self.show_history)
        buttons.addWidget(self.history_button)
        buttons.addStretch(1)
        vbox.addLayout(buttons)

        # Messages go below the buttons: when one appears only the box underneath shrinks,
        # the inputs and the Swap button never move.
        self.status_label = WWLabel('')
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)  # server text is never rendered as HTML
        self.status_label.setVisible(False)
        vbox.addWidget(self.status_label)

        # swaps THORChain has not finished yet, one card each; takes all remaining height
        self.pending_box = QGroupBox(_('Swaps in progress'))
        pending_layout = QVBoxLayout(self.pending_box)
        pending_layout.setContentsMargins(4, 4, 4, 4)
        self.pending_scroll = QScrollArea()
        self.pending_scroll.setWidgetResizable(True)
        self.pending_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.pending_container = QWidget()
        self.pending_layout = QVBoxLayout(self.pending_container)
        self.pending_layout.setContentsMargins(0, 0, 0, 0)
        self.pending_layout.setSpacing(6)
        self.pending_layout.addStretch(1)
        self.pending_scroll.setWidget(self.pending_container)
        pending_layout.addWidget(self.pending_scroll)
        vbox.addWidget(self.pending_box, 1)
        self._tracking = False
        self.track_timer = QTimer(self)
        self.track_timer.timeout.connect(self.track_pending_swaps)
        self.track_timer.start(TRACK_INTERVAL_MS)
        self._render_pending(None)

        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.timeout.connect(self._on_inputs_settled)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._on_auto_refresh)
        self.problem_timer = QTimer(self)
        self.problem_timer.timeout.connect(self._update_problems)
        self.problem_timer.start(PROBLEM_CHECK_MS)
        self.countdown_timer = QTimer(self)
        self.countdown_timer.timeout.connect(self._update_countdown)
        self.countdown_timer.start(1000)

        # base-unit changes (BTC / mBTC / sat) re-render Electrum's own amount fields; mirror that
        self.window.app.refresh_amount_edits_signal.connect(self._on_base_unit_changed)

        if not self.window.network:
            self._set_status(_('Not connected to a network; swaps are unavailable.'), error=True)
            for w in (self.amount_e, self.asset_combo, self.destination_e, self.refresh_button, self.settings_button):
                w.setEnabled(False)
        else:
            self.refresh_assets()

    # ---- lifecycle

    def clean_up(self):
        self._closed = True
        self._request_id += 1  # in-flight responses are dropped
        for timer in (self.debounce_timer, self.refresh_timer, self.problem_timer, self.countdown_timer,
                      self.track_timer):
            timer.stop()
        try:
            self.window.app.refresh_amount_edits_signal.disconnect(self._on_base_unit_changed)
        except TypeError:
            pass

    # ---- helpers

    @staticmethod
    def _tool_button(glyph: str, tooltip: str, slot) -> QToolButton:
        """Round button with a Unicode glyph; scales with the font and follows the theme colours."""
        button = QToolButton()
        button.setText(glyph)
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        button.setStyleSheet(
            'QToolButton { border-radius: 14px; min-width: 28px; min-height: 28px; font-size: 18px; }'
            'QToolButton:hover { background-color: palette(mid); }')
        button.clicked.connect(slot)
        return button

    def _run(self, coro, callback: Callable[[Any, Optional[Exception]], None], *, name: str):
        """Run a coroutine on the network loop; callback(result, error) runs on the GUI thread.

        Results travel through the plugin's long-lived relay object, so a tab that
        was closed meanwhile is simply skipped (checked in the callback) instead of
        being touched after deletion.
        """
        relay = self.plugin.relay

        def guarded(result, error):
            if not self._closed:
                callback(result, error)

        async def wrapper():
            try:
                result = await coro
            except Exception as e:  # noqa: BLE001 - everything becomes a GUI message
                relay.delivered.emit(guarded, None, e)
            else:
                relay.delivered.emit(guarded, result, None)
        self.window.run_coroutine_from_thread(wrapper(), name)

    def _set_status(self, text: str, *, error: bool = False):
        self.status_label.setText(text)
        # scoped to QLabel: ColorScheme.as_stylesheet() targets QWidget and would also
        # colour the label's context menu
        color = ColorScheme.RED.as_color().name()
        self.status_label.setStyleSheet(f'QLabel {{ color: {color}; }}' if error else '')
        self.status_label.setVisible(bool(text))

    def _show_assets_error_or(self, text: str, *, error: bool = False):
        """The asset-list error stays visible until the list loads; other texts yield to it."""
        if self._assets_error and self.asset_combo.count() == 0:
            self._set_status(self._assets_error, error=True)
        else:
            self._set_status(text, error=error)

    def _inputs(self) -> Optional[dict]:
        """Current swap request if every field is filled in, else None."""
        amount = self._amount_sats
        asset = self.asset_combo.currentData()
        destination = self.destination_e.text().strip()
        if not amount or amount <= 0 or not asset or not destination:
            return None
        settings = self.plugin.get_settings()
        return dict(
            to_asset=asset,
            destination=destination,
            amount_sats=int(amount),
            slippage_bps=settings.slippage_bps,
            streaming_interval=settings.streaming_interval,
            streaming_quantity=settings.streaming_quantity,
        )

    def _spendable_sats(self) -> int:
        try:
            return sum(c.value_sats() for c in self.window.get_coins(nonlocal_only=True))
        except Exception:  # noqa: BLE001 - a balance hint must never break quoting
            return 0

    def _clear_offer(self):
        """Forget the current offer; also invalidates any request still in flight."""
        self.offer = None
        self._request_id += 1
        self._busy = False
        self.refresh_button.setEnabled(True)
        self.receive_label.setText('')
        self.fees_label.setText('')
        self.receive_label.setToolTip('')
        self.fees_info.setVisible(False)
        self.swap_button.setEnabled(False)
        self.refresh_timer.stop()
        self._update_countdown()

    def _offer_is_current(self) -> bool:
        inputs = self._inputs()
        return self.offer is not None and inputs is not None and self.offer.matches(**inputs)

    # ---- asset list

    def refresh_assets(self):
        if not self.window.network:
            return
        self.refresh_button.setEnabled(False)
        self._assets_error = ''
        self._set_status(_('Loading the asset list...'))
        self._run(self.plugin.fetch_assets(), self._on_assets, name='thorchain_swap.assets')

    def _on_assets(self, pools, error):
        self.refresh_button.setEnabled(not self._busy)
        if error is not None:
            self.logger.info(f'fetching pools failed: {error!r}')
            self._assets_error = _('Could not load the asset list from THORNode: {}').format(error) + '\n' \
                + _('Press the refresh button to try again.')
            self._show_assets_error_or('')
            return
        wanted = self.asset_combo.currentData() or self.config.THORCHAIN_SWAP_LAST_ASSET
        self.asset_combo.blockSignals(True)
        self.asset_combo.clear()
        for pool in pools:
            self.asset_combo.addItem(self.icons.get(pool.asset), asset_display_name(pool.asset, pools), pool.asset)
        index = self.asset_combo.findData(wanted) if wanted else -1
        self.asset_combo.setCurrentIndex(index)  # -1 shows the placeholder instead of a silent default
        self.asset_combo.blockSignals(False)
        if not pools:
            # sticky, like a failed fetch: the list is unusable until refreshed
            self._assets_error = _('THORChain has no open pools right now.') + '\n' \
                + _('Press the refresh button to try again.')
            self._show_assets_error_or('')
            return
        self._assets_error = ''
        if wanted and index < 0:
            self._set_status(_('The previously used asset {} is not available right now; choose an asset.').format(
                asset_display_name(wanted)), error=True)
        else:
            self._set_status('')
        if self._inputs() is not None:
            self._schedule_quote()

    # ---- quoting

    def _on_amount_changed(self, *_args):
        if self.amount_e.text().strip() == '!':
            # Electrum's "max" shortcut: _on_max_shortcut already explained; keep that message
            self._amount_sats = None
            self._clear_offer()
            return
        amount = self.amount_e.get_amount()
        sats = int(amount) if amount else None
        if sats == self._amount_sats and sats is not None:
            return  # same amount re-rendered (e.g. base-unit change): the offer still applies
        self._amount_sats = sats
        self._schedule_quote()

    def _on_max_shortcut(self):
        self._set_status(_('"Max" is not supported for swaps; enter the amount.'), error=True)

    def _on_base_unit_changed(self):
        # keep the meaning of the field: re-render the same satoshi amount in the new unit
        if self._amount_sats is not None:
            self.amount_e.setAmount(self._amount_sats)

    def _on_asset_changed(self, *_args):
        # remember the user's choice right away, not only after a completed swap
        # (signals are blocked while the list is being refilled, so this is a user action)
        asset = self.asset_combo.currentData()
        if asset:
            self.config.THORCHAIN_SWAP_LAST_ASSET = asset
        self._schedule_quote()

    def _schedule_quote(self, *_args):
        """Inputs changed: the current offer no longer describes them."""
        if self._frozen:
            return
        self._clear_offer()
        self.debounce_timer.start(DEBOUNCE_MS)

    def _on_inputs_settled(self):
        if self._inputs() is None:
            self._clear_offer()
            self._show_assets_error_or('')
            return
        self.request_quote()

    def _on_auto_refresh(self):
        if not self._frozen and not self._busy and self._inputs() is not None:
            self.request_quote()

    def refresh_quote(self):
        if self._frozen or not self.window.network:
            return
        self.debounce_timer.stop()
        if self.asset_combo.count() == 0:
            self.refresh_assets()
            return
        if self._inputs() is None:
            self._clear_offer()
            return
        self.request_quote()

    def request_quote(self):
        inputs = self._inputs()
        if inputs is None or not self.window.network:
            return
        # a new request always supersedes the previous one; its response will be dropped
        self._request_id += 1
        request_id = self._request_id
        self._busy = True
        self.swap_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self._set_status(_('Updating quote...'))
        self._run(self.plugin.fetch_offer(**inputs), partial(self._on_offer, request_id),
                  name='thorchain_swap.quote')

    def _on_offer(self, request_id: int, offer: Optional[SwapOffer], error):
        if request_id != self._request_id:
            return  # superseded by a newer request, or the inputs were cleared meanwhile
        self._busy = False
        self.refresh_button.setEnabled(True)
        if error is not None:
            self.logger.info(f'quote failed: {error!r}')
            self._clear_offer()
            self._set_status(str(error), error=True)
            return
        inputs = self._inputs()
        if inputs is None or not offer.matches(**inputs):
            # the fields or the settings moved on while the request was in flight: re-quote
            self._schedule_quote()
            return
        try:
            self._render_offer(offer)
        except Exception as e:  # noqa: BLE001 - never leave half-rendered state behind
            self.logger.exception('could not render the offer')
            self._clear_offer()
            self._set_status(_('THORNode returned an unusable quote: {}').format(e), error=True)
            return
        self.offer = offer
        self.refresh_timer.start(AUTO_REFRESH_MS)
        self._update_countdown()
        self._update_problems()

    def _render_offer(self, offer: SwapOffer):
        q = offer.quote
        price = self.plugin.asset_usd_price(offer.to_asset)
        fmt = lambda amount: _format_asset_amount(amount, offer.to_asset, price)
        receive = _('You receive about {}, at least {}').format(fmt(q.expected_amount_out), fmt(offer.limit))
        vault_note = _('Inbound vault: {}').format(offer.inbound.address)
        if offer.thornode_host and offer.thornode_host not in DEFAULT_THORNODE_URL:
            vault_note += ' (' + _('from {}').format(offer.thornode_host) + ')'
        streaming = ''
        if offer.streaming_interval or offer.streaming_quantity:
            streaming = ', ' + _('streaming over {} blocks').format(q.streaming_swap_blocks)
        fees = _('Fees {} ({} bps), about {} min{}').format(
            fmt(q.fee_total), q.total_bps, max(1, round(q.total_swap_seconds / 60)), streaming)
        if offer.affiliate_bps > 0:
            dev_fee = fmt(q.fee_affiliate)
        else:
            dev_fee = '0 (' + _('free for swaps under {}$').format(self.plugin.get_settings().affiliate_min_usd) + ')'
        breakdown = _('THORChain fee: {}').format(fmt(q.fee_outbound + q.fee_liquidity)) + '\n' \
            + _('Plugin dev fee: {}').format(dev_fee)
        # all computed; now touch the widgets
        self.receive_label.setText(receive)
        self.receive_label.setToolTip(vault_note + '\n' + _('Destination: {}').format(offer.destination)
                                      + '\n' + _('Memo: {}').format(offer.memo))
        self.fees_label.setText(fees)
        self.fees_info.setToolTip(breakdown)
        self.fees_info.setVisible(True)

    def _offer_problems(self, offer: SwapOffer) -> list:
        """THORChain-side blocking problems plus the wallet-side balance check."""
        problems = offer.blocking_problems()
        spendable = self._spendable_sats()
        if offer.amount_sats > spendable:
            # the quote stays visible (the user may be about to fund the wallet), the swap is not allowed
            if spendable <= 0:
                problems.append(_('The wallet has no spendable coins'))
            else:
                problems.append(_('Amount exceeds the spendable balance of {}').format(
                    self.window.format_amount_and_units(spendable)))
        return problems

    def _update_problems(self):
        if self._busy or self._frozen:
            return  # 'Updating quote...' / the frozen offer must stay as they are
        if not self.offer:
            return
        if not self._offer_is_current():
            self._schedule_quote()  # e.g. settings changed from another wallet window
            return
        problems = self._offer_problems(self.offer)
        if problems:
            self._set_status('\n'.join(problems), error=True)
            self.swap_button.setEnabled(False)
        else:
            self._show_assets_error_or('')
            self.swap_button.setEnabled(not self.debounce_timer.isActive())

    def _update_countdown(self):
        if self.refresh_timer.isActive() and not self._frozen:
            seconds = max(0, (self.refresh_timer.remainingTime() + 999) // 1000)
            self.countdown_label.setText(f'{seconds}s')
        else:
            self.countdown_label.setText('')

    # ---- settings

    def show_settings(self):
        d = SwapSettingsDialog(self, self.plugin)
        if d.exec():
            d.save()
            self.refresh_quote()

    # ---- swap

    def _freeze_quote(self, status: str = ''):
        """Stop refreshing while the user confirms and signs: the offer shown is the one used."""
        self.refresh_timer.stop()
        self.debounce_timer.stop()
        self._frozen = True
        self.swap_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        if status:
            self._set_status(status)
        self._update_countdown()

    def _unfreeze_quote(self):
        self._frozen = False
        if self._closed:
            return
        self.refresh_button.setEnabled(True)
        if self.offer is not None and self._offer_is_current():
            if self.offer.blocking_problems():
                self.request_quote()  # the freeze lasted long enough for the quote to expire
            else:
                self.refresh_timer.start(AUTO_REFRESH_MS)
                self._update_problems()
        else:
            self._schedule_quote()
        self._update_countdown()

    def do_swap(self):
        offer = self.offer
        if offer is None or self._busy or self._frozen or self.debounce_timer.isActive():
            return
        if not self._offer_is_current():
            self._schedule_quote()
            return
        if self._offer_problems(offer):
            self._update_problems()
            return
        if self.wallet.wallet_type == '2fa':
            self.show_error(_('Two-factor (TrustedCoin) wallets add a fee output to every transaction, '
                              'which THORChain does not accept. Use a different wallet.'))
            return
        if self.wallet.is_watching_only():
            self.show_error(_('This wallet cannot sign. Swaps need a wallet with private keys, '
                              'so that the transaction is broadcast while the quote is valid.'))
            return
        self._freeze_quote()
        try:
            self._do_swap(offer)
        except BaseException:
            self._unfreeze_quote()
            raise

    def _do_swap(self, offer: SwapOffer):
        params = offer.params
        last_error = []  # reason the most recent make_tx call failed, if it did

        def make_tx(fee_policy, *, confirmed_only=False, base_tx=None):
            # nonlocal_only: never build a swap on top of a transaction that was not broadcast
            coins = self.window.get_coins(nonlocal_only=True, confirmed_only=confirmed_only)
            try:
                tx = make_swap_tx(self.wallet, params=params, fee_policy=fee_policy, coins=coins, rbf=False)
            except (NotEnoughFunds, NoDynamicFeeEstimates):
                last_error.clear()
                raise
            except Exception as e:  # noqa: BLE001 - the dialog has no catch-all; a raise would repeat every 500 ms
                # the confirm dialog only knows about NotEnoughFunds; remember the real reason
                if not isinstance(e, SwapTxError):
                    self.logger.exception('building the swap transaction failed')
                last_error[:] = [str(e)]
                self._set_status(str(e), error=True)
                raise NotEnoughFunds() from e
            last_error.clear()
            return tx

        # note: no payee_outputs. ConfirmTxDialog uses them only to offer a "Submarine
        #       Payment" to Lightning wallets, which would pay the vault from an HTLC
        #       without the memo.
        tx, is_preview, did_swap = self.window.confirm_tx_dialog(make_tx, offer.amount_sats)
        if did_swap:
            # cannot happen without payee_outputs; if it ever does, nothing of ours was sent
            self.show_error(_('Unexpected submarine payment; the swap was not performed.'))
            self._unfreeze_quote()
            return
        if tx is None:
            if last_error:
                self.show_error(last_error[-1])
            self._unfreeze_quote()
            return
        # The dialog can edit the transaction after make_tx (locktime, RBF, change
        # settings), so verify what came back, exactly as it will be signed.
        tx.set_rbf(False)
        try:
            verify_swap_tx(self.wallet, tx, params, local_height=self.wallet.adb.get_local_height())
        except SwapTxError as e:
            self.show_error(_('The transaction was changed in a way THORChain does not accept:') + '\n' + str(e))
            self._unfreeze_quote()
            return
        if offer.blocking_problems():
            self.show_error(_('The quote expired while you were confirming. A new one is being requested.'))
            self._unfreeze_quote()
            return
        fee = tx.get_fee()
        if fee is not None:
            feerate = fee / tx.estimated_size()
            recommended = offer.quote.recommended_gas_rate
            if recommended and feerate < recommended:
                if not self.question(
                        _('The fee rate {:.1f} sat/vB is below the {} sat/vB THORChain recommends. '
                          'A slow confirmation risks arriving after the vault has rotated.').format(feerate, recommended)
                        + '\n\n' + _('Continue anyway?')):
                    self._unfreeze_quote()
                    return
        if is_preview:
            self._show_preview(tx)
            return

        # Ask for the password ourselves (instead of ElectrumWindow.sign_tx, which is
        # @protected) so that a cancelled password dialog is seen here and the
        # quote refresh can resume.
        password = None
        while self.wallet.has_keystore_encryption():
            password = self.wallet.get_unlocked_password() or self.window.password_dialog(
                parent=self.window.top_level_window(), msg=_('Enter your password to sign the swap transaction'))
            if password is None:
                self._unfreeze_quote()
                return
            try:
                self.wallet.check_password(password)
                break
            except Exception as e:
                self.show_error(str(e))
        if offer.blocking_problems():
            # the password dialog may have been left open for a long time
            self.show_error(_('The quote expired before signing. A new one is being requested.'))
            self._unfreeze_quote()
            return
        self.window.sign_tx_with_password(tx, callback=partial(self._on_signed, tx, offer),
                                          password=password, external_keypairs=None)

    def _show_preview(self, tx):
        """View-only transaction dialog: no sign, broadcast, save or export.

        Anything that lets the transaction leave this dialog would bypass the
        expiry re-check and the swap bookkeeping, so the only way to send it is
        the Swap button once the preview is closed.
        """
        self._freeze_quote(_('Transaction preview open; close it to continue.'))
        try:
            d = TxDialog(tx, parent=self.window, prompt_if_unsaved=False, prompt_if_complete_unsaved=False,
                         on_closed=lambda _tx: self._unfreeze_quote())
            for button in (d.sign_button, d.broadcast_button, d.save_button,
                           d.export_actions_button, d.partial_tx_actions_button):
                button.setVisible(False)
            d.show()
        except Exception as e:  # noqa: BLE001 - never leave the tab frozen
            self.logger.exception('could not open the preview')
            self.show_error(str(e))
            self._unfreeze_quote()

    def _on_signed(self, tx, offer: SwapOffer, success: bool):
        if self._closed:
            return
        try:
            if not success:
                self._unfreeze_quote()
                return
            if not tx.is_complete():
                # multisig with missing cosigners, hardware wallet without the device, ...
                self._unfreeze_quote()
                self.show_message(
                    _('The transaction needs more signatures and was not broadcast. Nothing was recorded.') + '\n\n'
                    + _('If you complete it elsewhere, broadcast it within the next {} minutes, '
                        'before the quote expires and the THORChain vault rotates.').format(
                        max(1, offer.seconds_left() // 60)))
                self.window.show_transaction(tx)
                return
            if offer.blocking_problems():
                self._unfreeze_quote()
                self.show_error(_('The quote expired during signing; the transaction was not broadcast.'))
                self.window.show_transaction(tx, show_broadcast_button=False)
                return
            self._broadcast(tx, offer)
        except Exception as e:  # noqa: BLE001 - the signed tx must never be lost silently
            self.logger.exception('swap bookkeeping failed')
            self._unfreeze_quote()
            self.show_error(str(e))
            self.window.show_transaction(tx)

    def _broadcast(self, tx, offer: SwapOffer):
        network = self.window.network
        txid = tx.txid()

        def broadcast_thread():
            try:
                network.run_from_another_thread(network.broadcast_transaction(tx))
            except TxBroadcastError as e:
                return False, e.get_message_for_gui()
            except BestEffortRequestFailed as e:
                return False, repr(e)
            return True, ''

        def record():
            # wallet-only bookkeeping: safe (and required) even if the tab was closed meanwhile
            try:
                self.plugin.add_swap_record(self.wallet, txid=txid, offer=offer)
                self.config.THORCHAIN_SWAP_LAST_ASSET = offer.to_asset
                self.wallet.set_label(txid, _('THORChain swap to {}').format(asset_display_name(offer.to_asset)))
            except Exception as e:  # noqa: BLE001 - bookkeeping must not hide a successful broadcast
                self.logger.exception('could not record the swap')
                return str(e)
            return ''

        def broadcast_done(result):
            success, message = result if result else (False, '')
            if success:
                record_error = record()
            if self._closed:
                return
            if not success:
                self._unfreeze_quote()
                self.show_error(_('Broadcast failed: {}').format(message or _('unknown error')) + '\n\n'
                                + _('Nothing was sent. Press Swap again to retry with a fresh quote.'))
                # view only: a later broadcast from the dialog would skip the expiry check and the record
                self.window.show_transaction(tx, show_sign_button=False, show_broadcast_button=False)
                return
            if record_error:
                self.show_error(_('The swap was sent but could not be recorded: {}').format(record_error))
            self.do_clear()
            self._set_status(_('Swap transaction {} sent. THORChain will process it after {} confirmation(s).').format(
                txid, offer.quote.inbound_confirmation_blocks))
            self._render_pending()
            self.track_pending_swaps()

        def broadcast_error(exc_info):
            if self._closed:
                return
            self._unfreeze_quote()
            self.window.on_error(exc_info)
            self.window.show_transaction(tx, show_sign_button=False, show_broadcast_button=False)

        WaitingDialog(self.window, _('Broadcasting swap transaction...'), broadcast_thread, broadcast_done, broadcast_error)

    def do_clear(self):
        self.debounce_timer.stop()
        self._frozen = False
        self._clear_offer()
        for w in (self.amount_e, self.destination_e):
            w.blockSignals(True)
            w.setText('')
            w.blockSignals(False)
        self._amount_sats = None
        self._show_assets_error_or('')

    # ---- swaps in progress / history

    def _describe_record(self, record: dict) -> str:
        amount = self.window.format_amount_and_units(int(record.get('amount_sats', 0)))
        asset = asset_display_name(record.get('to_asset', ''))
        return f'{amount} → {asset}'

    def _local_status(self, record: dict) -> str:
        """Status from the wallet's own point of view, used until THORChain reports anything."""
        if record.get('status_text') and not record['status_text'].startswith('waiting for THORChain'):
            return record['status_text']
        try:
            conf = self.wallet.adb.get_tx_height(record['txid']).conf
        except Exception:  # noqa: BLE001
            conf = 0
        if conf <= 0:
            return _('in the Bitcoin mempool, waiting for the first confirmation')
        return record.get('status_text') or _('{} confirmation(s), waiting for THORChain').format(conf)

    def _expected_text(self, record: dict) -> str:
        asset = record.get('to_asset', '')
        price = self.plugin.asset_usd_price(asset)
        if record.get('delivered'):
            return _format_asset_amount(int(record['delivered']), asset, price)
        return '≈ ' + _format_asset_amount(int(record.get('expected_amount_out', 0)), asset, price)

    def _render_pending(self, _records=None):
        if self._closed:
            return
        records = self.plugin.pending_swap_records(self.wallet)
        # rebuild the cards; there are only ever a handful
        while self.pending_layout.count() > 1:
            item = self.pending_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # detach immediately: deleteLater alone leaves the old widget painted until the
                # event loop gets around to it
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        if not records:
            empty = QLabel(_('No swaps in progress.'))
            empty.setEnabled(False)
            self.pending_layout.insertWidget(0, empty)
            return
        for i, r in enumerate(records):
            card = SwapCard(
                amount_in=self.window.format_amount_and_units(int(r.get('amount_sats', 0))),
                asset=asset_display_name(r.get('to_asset', '')),
                amount_out=self._expected_text(r),
                status=self._local_status(r),
                progress=float(r.get('progress') or 0.0),
                tooltip=_('Destination: {}').format(r.get('destination', '')) + '\n' + r['txid'],
                on_open=partial(self._open_pending_tx, r['txid']),
            )
            self.pending_layout.insertWidget(i, card)

    def _open_pending_tx(self, txid: str):
        tx = self.wallet.adb.get_transaction(txid)
        if tx is not None:
            self.window.show_transaction(tx)

    def track_pending_swaps(self):
        """Ask THORChain about every unfinished swap; runs on a timer and after a broadcast."""
        if self._closed or self._tracking or not self.window.network:
            return
        records = self.plugin.pending_swap_records(self.wallet)
        self._render_pending(records)
        if not records:
            return
        self._tracking = True
        self._run(self.plugin.fetch_swap_statuses(records), self._on_statuses, name='thorchain_swap.status')

    def _on_statuses(self, statuses, error):
        self._tracking = False
        if error is not None:
            self.logger.info(f'status tracking failed: {error!r}')
            return
        finished = []
        for txid, status in (statuses or {}).items():
            record = self.plugin.get_swap_records(self.wallet).get(txid)
            if record is None:
                continue
            self.plugin.update_swap_record(self.wallet, txid, status)
            if status.done:
                finished.append((record, status))
        self._render_pending()
        if finished and not self._busy and not self._frozen:
            self._set_status('\n'.join(
                _('Swap {}: {}').format(self._describe_record(r), s.text) for r, s in finished))

    def show_history(self):
        d = SwapHistoryDialog(self, self.plugin, self.wallet)
        d.exec()
