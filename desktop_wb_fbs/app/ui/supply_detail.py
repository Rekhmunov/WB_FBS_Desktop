# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QCursor, QDesktopServices
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtCore import QUrl

from app.db import Database
from app.services.kiz_pick import KizService, PickVerifyService
from app.services.orders import OrdersService
from app.services import supply_detail_cache
from app.services import supply_session
from app.services.trbx_stickers import StickersService, TrbxService
from app.ui.dialog_utils import (
    apply_fullscreen_on_show,
    fullscreen_parent,
    init_fullscreen_dialog,
    init_maximized_window,
    prepare_modal_dialog,
)
from app.ui.dialogs_extra import show_png_list, show_supply_qr
from app.ui.format_helpers import (
    ago_label,
    format_date_short,
    make_badge,
    make_photo_label,
)
from app.ui.layout_utils import FlowLayout
from app.wb import cargo_type_label, parse_json_list

_RENDER_BATCH = 50
_WAIT_ORDERS_TIP = "Дождитесь загрузки заказов"
_LOAD_STEPS = (
    "Заказы",
    "Номера стикеров",
    "КИЗ и проверка ШК",
)


class _SupplyCoreLoadWorker(QThread):
    """Staged preload: orders → sticker numbers → KIZ/pick meta."""

    progress = pyqtSignal(int, int, str)  # step (1-based), total, detail
    core_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        db: Database,
        orders: OrdersService,
        source_id: int,
        supply_id: str,
        api_key: str,
        supply_pickup_allowed: bool,
        generation: int = 0,
        *,
        force_network: bool = False,
    ) -> None:
        super(_SupplyCoreLoadWorker, self).__init__()
        self.db = db
        self.orders = orders
        self.source_id = source_id
        self.supply_id = supply_id
        self.api_key = api_key
        self.supply_pickup_allowed = supply_pickup_allowed
        self.generation = int(generation)
        self.force_network = bool(force_network)

    def run(self) -> None:
        try:
            from app.diag_log import write as diag_write

            total = len(_LOAD_STEPS)
            diag_write(
                "supply.core_worker.begin",
                sync=True,
                supply_id=self.supply_id,
                source_id=self.source_id,
                generation=self.generation,
                force_network=self.force_network,
            )

            def _progress(step: int, detail: str = "") -> None:
                self.progress.emit(int(step), total, str(detail or ""))

            session = supply_session.preload_supply_core(
                self.db,
                self.orders,
                self.source_id,
                self.supply_id,
                self.api_key,
                supply_pickup_allowed=self.supply_pickup_allowed,
                progress=_progress,
                force_network=self.force_network,
            )
            diag_write(
                "supply.core_worker.snapshot_emit",
                sync=True,
                supply_id=self.supply_id,
                rows=len(session.rows or []),
                generation=self.generation,
            )
            self.core_ready.emit(
                {
                    "generation": self.generation,
                    "payload": supply_session.snapshot_for_ui(session),
                }
            )
            diag_write(
                "supply.core_worker.done",
                sync=True,
                supply_id=self.supply_id,
                generation=self.generation,
            )
        except Exception as exc:
            from app.diag_log import exception as diag_exception

            diag_exception(
                "supply.core_worker.error",
                exc,
                supply_id=self.supply_id,
                generation=self.generation,
            )
            self.failed.emit(str(exc))


class SupplyDetailDialog(QDialog):
    """Web parity for `.wb-fbs-supply-detail-modal`."""

    def __init__(
        self,
        db: Database,
        orders: OrdersService,
        source: Dict[str, Any],
        supply_id: str,
        parent: Optional[QWidget] = None,
        *,
        fullscreen: bool = True,
    ) -> None:
        super(SupplyDetailDialog, self).__init__(fullscreen_parent(parent, fullscreen))
        self.db = db
        self.orders = orders
        self.source = source
        self.supply_id = supply_id
        self.source_id = int(source["id"])
        self.api_key = str(source["api_key"])
        self.trbx = TrbxService(db)
        self.stickers = StickersService(db)
        self.kiz = KizService(db)
        self.pick = PickVerifyService(db)
        self._all_rows = []  # type: List[Dict[str, Any]]
        self._row_by_oid = {}  # type: Dict[int, Dict[str, Any]]
        self._row_order_ids = []  # type: List[int]
        self._selected = set()  # type: set
        self._supply_pickup_allowed = False
        self._last_status_note = ""
        self._load_worker = None  # type: Optional[_SupplyCoreLoadWorker]
        self._alive_workers = []  # type: List[QThread]
        self._load_gen = 0
        self._loading = False
        self._load_step = 0
        self._load_detail = ""
        self._actions_ready = False
        self._action_widgets = []  # type: List[QWidget]
        self._saved_tooltips = {}  # type: Dict[QWidget, str]
        self.supply_mutated = False

        self.setWindowTitle("Поставка {}".format(supply_id))
        init_fullscreen_dialog(
            self,
            fullscreen=fullscreen,
            default_size=(1040, 720),
            minimum_size=(880, 600),
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("sdHeader")
        hv = QVBoxLayout(header)
        hv.setContentsMargins(24, 20, 24, 16)
        hv.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.header = QLabel("Поставка")
        self.header.setObjectName("sdTitle")
        self.header.setWordWrap(True)
        title_row.addWidget(self.header, 1)
        close_x = QPushButton("✕")
        close_x.setObjectName("iconBtn")
        close_x.setToolTip("Закрыть")
        close_x.clicked.connect(self.accept)
        title_row.addWidget(close_x, 0, Qt.AlignTop)
        hv.addLayout(title_row)

        self.warehouse = QLabel("📍 —")
        self.warehouse.setObjectName("sdWarehouse")
        hv.addWidget(self.warehouse)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(12)
        chips_wrap = QWidget()
        self.meta_chips = FlowLayout(chips_wrap, h_spacing=8, v_spacing=8)
        meta_row.addWidget(chips_wrap, 1)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("sdSearch")
        self.search_input.setPlaceholderText("🔍 Поиск…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumWidth(200)
        self.search_input.setMaximumWidth(280)
        self.search_input.setToolTip("Поиск по заказу, стикеру, названию товара и ШК")
        self.search_input.textChanged.connect(lambda _t: self._update_search_visibility())
        meta_row.addWidget(self.search_input, 0, Qt.AlignTop)
        hv.addLayout(meta_row)

        self.meta = QLabel("")
        self.meta.setObjectName("sdMeta")
        self.meta.setWordWrap(True)
        self.meta.hide()
        hv.addWidget(self.meta)

        self.load_status = QLabel("")
        self.load_status.setObjectName("sdLoadStatus")
        self.load_status.setWordWrap(True)
        self.load_status.hide()
        hv.addWidget(self.load_status)

        actions = FlowLayout(h_spacing=8, v_spacing=8)

        def _sec(btn):
            btn.setObjectName("secondary")
            return btn

        pick_btn = _sec(QPushButton("Лист подбора"))
        pick_btn.clicked.connect(partial(self.picking_list, "summary"))
        pick_caret = QToolButton()
        pick_caret.setObjectName("splitCaret")
        pick_caret.setText("▾")
        pick_caret.setPopupMode(QToolButton.InstantPopup)
        pick_menu = QMenu(pick_caret)
        pick_menu.addAction(
            "Расширенный лист подбора", partial(self.picking_list, "extended")
        )
        pick_caret.setMenu(pick_menu)
        actions.addWidget(self._split_pair(pick_btn, pick_caret))

        st_btn = _sec(QPushButton("Стикеры"))
        st_btn.clicked.connect(self.print_stickers)
        st_caret = QToolButton()
        st_caret.setObjectName("splitCaret")
        st_caret.setText("▾")
        st_caret.setPopupMode(QToolButton.InstantPopup)
        st_menu = QMenu(st_caret)
        st_menu.addAction("Печать по категориям", self.stickers_by_category)
        st_caret.setMenu(st_menu)
        actions.addWidget(self._split_pair(st_btn, st_caret))

        kiz_btn = _sec(QPushButton("Маркировка"))
        kiz_btn.clicked.connect(self.open_kiz)
        kiz_ref = _sec(QToolButton())
        kiz_ref.setText("↻")
        kiz_ref.setToolTip("Проверить статусы КИЗ на ВБ")
        kiz_ref.clicked.connect(self.refresh_kiz_status)
        actions.addWidget(self._split_pair(kiz_btn, kiz_ref))

        extra_action_btns = []  # type: List[QPushButton]
        for text, slot in (
            ("Проверка ШК", self.open_pick),
            ("Грузоместа", self.manage_trbx),
            ("Отмененные заказы", self.show_cancelled),
        ):
            btn = _sec(QPushButton(text))
            btn.clicked.connect(slot)
            actions.addWidget(btn)
            extra_action_btns.append(btn)

        portal_btn = QPushButton("Портал ВБ  →")
        portal_btn.setObjectName("portalBtn")
        portal_btn.setToolTip("Открыть поставку на портале Wildberries")
        portal_btn.clicked.connect(self.open_portal)
        actions.addWidget(portal_btn)
        hv.addLayout(actions)

        self._action_widgets = [
            pick_btn,
            pick_caret,
            st_btn,
            st_caret,
            kiz_btn,
            kiz_ref,
            *extra_action_btns,
            portal_btn,
            self.search_input,
        ]
        root.addWidget(header)

        body = QFrame()
        body.setObjectName("sdBody")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        self.table = QTableWidget(0, 4)
        self.table.setObjectName("sdTable")
        self.table.setAlternatingRowColors(False)
        self.table.setHorizontalHeaderLabels(["", "Заказ", "Товар", ""])
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(3, 52)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(148)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.NoFocus)

        # Select-all checkbox overlaid on header column 0 (web thead parity).
        self.select_all_cb = QCheckBox()
        self.select_all_cb.setToolTip("Выбрать все")
        self.select_all_cb.stateChanged.connect(self._on_select_all_changed)
        self.table.horizontalHeader().setMinimumHeight(36)
        body_lay.addWidget(self.table, 1)
        root.addWidget(body, 1)

        self._header_select_host = QWidget(self.table.horizontalHeader())
        host_lay = QHBoxLayout(self._header_select_host)
        host_lay.setContentsMargins(12, 0, 0, 0)
        host_lay.addWidget(self.select_all_cb)
        host_lay.addStretch(1)
        self.table.horizontalHeader().sectionResized.connect(self._place_header_select)
        try:
            self.table.horizontalHeader().geometriesChanged.connect(
                self._place_header_select
            )
        except Exception:
            pass

        self._action_widgets.append(self.select_all_cb)

        self._apply_supply_header(self.orders.get_supply(self.source_id, self.supply_id))
        self._show_loading_table()
        self._set_actions_ready(False)
        self._begin_load()

    def _set_actions_ready(self, ready: bool) -> None:
        """Disable toolbar actions until orders load (web `_wbFbsSupplyDetailSetActionsReady`)."""
        self._actions_ready = bool(ready)
        for w in self._action_widgets:
            if not ready:
                if w not in self._saved_tooltips:
                    self._saved_tooltips[w] = w.toolTip()
                w.setToolTip(_WAIT_ORDERS_TIP)
                w.setProperty("waitOrders", True)
                if isinstance(w, QLineEdit):
                    w.setReadOnly(True)
                else:
                    w.setEnabled(False)
                    w.setAttribute(Qt.WA_AlwaysShowToolTips, True)
            else:
                if w in self._saved_tooltips:
                    w.setToolTip(self._saved_tooltips.pop(w))
                w.setProperty("waitOrders", False)
                if isinstance(w, QLineEdit):
                    w.setReadOnly(False)
                else:
                    w.setEnabled(True)
            w.style().unpolish(w)
            w.style().polish(w)

    def _require_actions_ready(self) -> bool:
        return bool(self._actions_ready)

    def _sticker_png_for_print(self) -> Tuple[Optional[Dict[int, Dict[str, Any]]], bool]:
        """Return ``(cached_or_none, abort)`` — never blocks open; print fetches missing."""
        return self._cached_sticker_png(), False

    def _cached_sticker_png(self) -> Optional[Dict[int, Dict[str, Any]]]:
        """Best-effort PNG meta from in-memory/disk cache (may be incomplete)."""
        from app.services.print_docs import get_cached_stickers_map

        session = supply_session.get_session(self.source_id, self.supply_id)
        ids = [
            int(r["order_id"])
            for r in ((session.rows if session else None) or self._all_rows)
            if r.get("order_id") is not None
        ]
        if not ids:
            return None
        api_key = (session.api_key if session else None) or self.api_key
        cached = get_cached_stickers_map(
            api_key,
            ids,
            sticker_type="png",
            keep_files=True,
        )
        return cached if cached else None

    def _sync_kiz_session(self, marking_rows: List[Dict[str, Any]]) -> None:
        """Keep supply session / cache aligned after KIZ status refresh."""
        session = supply_session.get_session(self.source_id, self.supply_id)
        if not session:
            return
        by_ui_oid = {
            int(r["order_id"]): r
            for r in self._all_rows
            if r.get("order_id") is not None
        }
        for r in session.rows:
            oid = int(r.get("order_id") or 0)
            src = by_ui_oid.get(oid)
            if not src:
                continue
            r["kiz_required"] = bool(src.get("kiz_required"))
            r["kiz_codes"] = list(src.get("kiz_codes") or [])
            r["kiz_status"] = src.get("kiz_status") or "empty"
            if "kiz_wb_synced" in src:
                r["kiz_wb_synced"] = src.get("kiz_wb_synced")
        kiz_rows = []  # type: List[Dict[str, Any]]
        for src in marking_rows:
            kr = dict(src)
            oid = int(kr["order_id"])
            st = session.sticker_numbers.get(oid) or {}
            part_a = str(st.get("partA") or kr.get("sticker_part_a") or "").strip()
            part_b = str(st.get("partB") or kr.get("sticker_part_b") or "").strip()
            kr["sticker_part_a"] = part_a
            kr["sticker_part_b"] = part_b
            kr["sticker_number"] = "{}{}".format(part_a, part_b)
            kiz_rows.append(kr)
        session.kiz_rows = kiz_rows
        supply_session.put_session(session)
        supply_detail_cache.put(
            self.source_id,
            self.supply_id,
            supply_session.snapshot_for_ui(session),
        )

    def accept(self) -> None:
        self._stop_load_worker()
        self._teardown_table()
        super(SupplyDetailDialog, self).accept()

    def reject(self) -> None:
        self._stop_load_worker()
        self._teardown_table()
        super(SupplyDetailDialog, self).reject()

    def closeEvent(self, event) -> None:
        self._stop_load_worker()
        self._teardown_table()
        super(SupplyDetailDialog, self).closeEvent(event)

    def _disconnect_worker(self, worker: QThread, *signal_names: str) -> None:
        for name in signal_names:
            signal = getattr(worker, name, None)
            if signal is None:
                continue
            try:
                signal.disconnect()
            except Exception:
                pass
        if worker not in self._alive_workers:
            self._alive_workers.append(worker)

        def _cleanup(w=worker) -> None:
            if w in self._alive_workers:
                self._alive_workers.remove(w)
            w.deleteLater()

        if worker.isRunning():
            worker.finished.connect(_cleanup)
        else:
            _cleanup()

    def _stop_load_worker(self) -> None:
        self._load_gen += 1  # ignore further signals from old workers
        core = self._load_worker
        self._load_worker = None
        if core is not None:
            self._disconnect_worker(core, "progress", "core_ready", "failed")
        self._loading = False

    def _teardown_table(self) -> None:
        self.table.blockSignals(True)
        self.select_all_cb.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        self.select_all_cb.blockSignals(False)
        self._row_order_ids = []
        self._row_by_oid = {}

    @staticmethod
    def _split_pair(main: QWidget, caret: QWidget) -> QWidget:
        """Main + caret/refresh: same height (web `.wb-fbs-picking-split` stretch)."""
        wrap = QWidget()
        wrap.setObjectName("splitPair")
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.setAlignment(Qt.AlignVCenter)
        # Match web min-height 40px for supply-detail action buttons.
        height = 40
        for w in (main, caret):
            w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            w.setFixedHeight(height)
            w.setMinimumHeight(height)
            w.setMaximumHeight(height)
        lay.addWidget(main)
        lay.addWidget(caret)
        wrap.setFixedHeight(height)
        return wrap

    def _place_header_select(self, *_args) -> None:
        hdr = self.table.horizontalHeader()
        self._header_select_host.setGeometry(0, 0, max(40, hdr.sectionSize(0)), hdr.height())
        self._header_select_host.show()
        self._header_select_host.raise_()

    def showEvent(self, event) -> None:
        super(SupplyDetailDialog, self).showEvent(event)
        apply_fullscreen_on_show(self)
        self._place_header_select()

    def _clear_chips(self) -> None:
        while self.meta_chips.count():
            item = self.meta_chips.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w:
                w.deleteLater()

    def _add_chip(self, text: str, *, qr: bool = False) -> QWidget:
        if qr:
            chip = QFrame()
            chip.setObjectName("sdChipQr")
            lay = QHBoxLayout(chip)
            lay.setContentsMargins(10, 2, 4, 2)
            lay.setSpacing(6)
            lab = QLabel(text)
            lab.setObjectName("sdChipQrText")
            lay.addWidget(lab)
            btn = QToolButton()
            btn.setObjectName("sdQrPrint")
            btn.setText("🖨")
            btn.setToolTip("Распечатать QR-код поставки")
            btn.clicked.connect(self.print_qr)
            lay.addWidget(btn)
            self.meta_chips.addWidget(chip)
            return chip
        lab = QLabel(text)
        lab.setObjectName("sdChip")
        self.meta_chips.addWidget(lab)
        return lab

    def reload(self) -> None:
        from app.services import order_open_cache

        supply_detail_cache.invalidate(self.source_id, self.supply_id)
        supply_session.invalidate(self.source_id, self.supply_id)
        try:
            order_ids = [
                int(r["order_id"])
                for r in (self._all_rows or [])
                if r.get("order_id") is not None
            ]
            if order_ids:
                order_open_cache.clear_for_orders(self.db, self.source_id, order_ids)
        except Exception:
            pass
        self._apply_supply_header(self.orders.get_supply(self.source_id, self.supply_id))
        self._show_loading_table()
        self._set_actions_ready(False)
        self._begin_load(force=True)

    def _apply_supply_header(self, supply: Optional[Dict[str, Any]]) -> None:
        if not supply:
            self.header.setText("Поставка {}".format(self.supply_id))
            self.setWindowTitle("Поставка {}".format(self.supply_id))
            return

        created = format_date_short(supply.get("created_at_wb"))
        name = str(supply.get("name") or "").strip()
        if not name:
            name = (
                "Поставка от {}".format(created)
                if created
                else "Поставка {}".format(self.supply_id)
            )
        self.header.setText(name)
        self.setWindowTitle(name)

        raw = {}
        try:
            raw = json.loads(supply.get("raw_json") or "{}")
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self._supply_pickup_allowed = bool(
            raw.get("isPickupPointShipmentAllowed") or raw.get("pickup_allowed")
        )

        order_ids = list(supply.get("order_ids") or [])
        boxes = list(supply.get("boxes") or [])
        cargo = cargo_type_label(supply.get("cargo_type")) or ""

        self._clear_chips()
        if cargo:
            self._add_chip(cargo)
        self._add_chip("Заказы {}".format(len(order_ids)))
        self._add_chip("Грузоместа {}".format(len(boxes)))
        self._add_chip("Создана {}".format(created or "—"))
        self._add_chip("QR поставки {}".format(self.supply_id), qr=True)

    def _show_loading_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(1)
        loading = QLabel("Идёт загрузка данных поставки…")
        loading.setObjectName("hint")
        loading.setAlignment(Qt.AlignCenter)
        loading.setWordWrap(True)
        self._loading_table_label = loading
        self.table.setSpan(0, 0, 1, 4)
        self.table.setCellWidget(0, 0, loading)
        self.table.blockSignals(False)
        self._load_step = 0
        self._load_detail = ""
        self._render_load_status()

    def _set_load_status(self, text: str = "") -> None:
        """Legacy single-line status (cache / wait messages)."""
        msg = str(text or "").strip()
        if not msg:
            self.load_status.hide()
            self.load_status.setText("")
            return
        self.load_status.setTextFormat(Qt.PlainText)
        self.load_status.setText(msg)
        self.load_status.show()

    def _render_load_status(self) -> None:
        step = int(self._load_step or 0)
        total = len(_LOAD_STEPS)
        if step <= 0:
            self.load_status.setTextFormat(Qt.RichText)
            self.load_status.setText(
                "<b>Подготовка данных поставки…</b><br>"
                + "<br>".join(
                    "○ {}".format(name) for name in _LOAD_STEPS
                )
            )
            self.load_status.show()
            return
        lines = [
            "<b>Загрузка данных поставки · шаг {} из {}</b>".format(
                min(step, total), total
            )
        ]
        for i, name in enumerate(_LOAD_STEPS, start=1):
            if i < step:
                mark = "✓"
                style = "color:#166534;"
            elif i == step:
                mark = "→"
                style = "color:#1d4ed8;font-weight:700;"
            else:
                mark = "○"
                style = "color:#64748b;"
            detail = ""
            if i == step and self._load_detail:
                detail = " <span style='color:#64748b;font-weight:500;'>({})</span>".format(
                    self._load_detail
                )
            lines.append(
                "<span style='{}'>{} {}{}</span>".format(style, mark, name, detail)
            )
        self.load_status.setTextFormat(Qt.RichText)
        self.load_status.setText("<br>".join(lines))
        self.load_status.show()
        lab = getattr(self, "_loading_table_label", None)
        if lab is not None and step > 0:
            current = _LOAD_STEPS[min(step, total) - 1] if step else "данные"
            lab.setText(
                "Идёт загрузка: {}…{}".format(
                    current,
                    " ({})".format(self._load_detail) if self._load_detail else "",
                )
            )

    def _begin_load(self, *, force: bool = False) -> None:
        if self._loading and not force:
            return

        if not force:
            session = supply_session.get_session(self.source_id, self.supply_id)
            if session and session.core_ready:
                self._apply_loaded_payload(supply_session.snapshot_for_ui(session))
                self._set_load_status("")
                self._set_actions_ready(True)
                return

        self._loading = True
        self._set_actions_ready(False)
        self._stop_load_worker()
        gen = self._load_gen
        worker = _SupplyCoreLoadWorker(
            self.db,
            self.orders,
            self.source_id,
            self.supply_id,
            self.api_key,
            self._supply_pickup_allowed,
            generation=gen,
            force_network=bool(force),
        )
        worker.progress.connect(self._on_load_progress)
        worker.core_ready.connect(self._on_core_ready)
        worker.failed.connect(self._on_load_failed)
        self._load_worker = worker
        if worker not in self._alive_workers:
            self._alive_workers.append(worker)
        worker.start()

    def _on_load_progress(self, step: int, total: int, detail: str) -> None:
        self._load_step = int(step or 0)
        self._load_detail = str(detail or "").strip()
        self._render_load_status()

    def _on_core_ready(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if int(payload.get("generation") or 0) != self._load_gen:
            return
        data = payload.get("payload")
        if not isinstance(data, dict):
            return
        from app.diag_log import write as diag_write

        order_count = len((data.get("rows") or []))
        diag_write(
            "supply.ui.core_ready",
            sync=True,
            supply_id=self.supply_id,
            generation=self._load_gen,
            rows=order_count,
        )
        supply_detail_cache.put(self.source_id, self.supply_id, data)
        self._apply_loaded_payload(data)
        self._loading = False
        self._load_worker = None
        self._load_step = 0
        self._load_detail = ""
        self._set_load_status("")
        self._set_actions_ready(True)
        diag_write(
            "supply.ui.actions_ready",
            sync=True,
            supply_id=self.supply_id,
            generation=self._load_gen,
            rows=order_count,
            png_deferred=True,
        )
    def _on_load_failed(self, message: str) -> None:
        self._loading = False
        self._load_worker = None
        self._load_step = 0
        self._load_detail = ""
        self._set_load_status("")
        self.table.blockSignals(True)
        self.table.setRowCount(1)
        self.table.clearSpans()
        err = QLabel(str(message or "Ошибка загрузки"))
        err.setObjectName("hint")
        err.setWordWrap(True)
        err.setStyleSheet("color:#b91c1c;")
        self.table.setSpan(0, 0, 1, 4)
        self.table.setCellWidget(0, 0, err)
        self.table.blockSignals(False)
        self._set_actions_ready(True)

    def _apply_loaded_payload(self, payload: Dict[str, Any]) -> None:
        rows = list(payload.get("rows") or [])
        self._all_rows = rows
        self._row_by_oid = {
            int(r["order_id"]): r for r in rows if r.get("order_id") is not None
        }
        warehouse = str(payload.get("warehouse") or "").strip()
        if warehouse:
            self.warehouse.setText("📍 {}".format(warehouse))
        elif not rows:
            supply = self.orders.get_supply(self.source_id, self.supply_id) or {}
            dest = supply.get("destination_office_id")
            self.warehouse.setText(
                "📍 {}".format(dest) if dest else "📍 —"
            )

        valid_ids = set(self._row_by_oid.keys())
        self._selected = {oid for oid in self._selected if oid in valid_ids}
        if self._last_status_note:
            self.meta.setText(self._last_status_note)
            self.meta.show()
        self._render_table()

    @staticmethod
    def _row_matches_search(row: Dict[str, Any], query: str) -> bool:
        q = str(query or "").strip().lower()
        if not q:
            return True
        hay = [
            row.get("order_id"),
            row.get("article"),
            row.get("product_name"),
            row.get("brand"),
            row.get("nm_id"),
            row.get("sticker_number"),
            row.get("sticker_part_a"),
            row.get("sticker_part_b"),
            row.get("sticker_barcode"),
            row.get("cancel_reason_label"),
        ]
        hay.extend(row.get("skus") or [])
        return any(q in str(v or "").strip().lower() for v in hay)

    def _visible_rows(self) -> List[Dict[str, Any]]:
        query = self.search_input.text()
        if not query.strip():
            return list(self._all_rows)
        return [r for r in self._all_rows if self._row_matches_search(r, query)]

    def _update_search_visibility(self) -> None:
        if not self._row_order_ids:
            return
        query = self.search_input.text()
        for i, oid in enumerate(self._row_order_ids):
            row = self._row_by_oid.get(oid)
            visible = self._row_matches_search(row, query) if row else True
            self.table.setRowHidden(i, not visible)
        self._sync_select_all()

    def _render_table(self) -> None:
        rows = list(self._all_rows)
        self.table.clearSpans()
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        self._row_order_ids = []
        for i, r in enumerate(rows):
            oid = int(r.get("order_id"))
            self._row_order_ids.append(oid)
            cb = QCheckBox()
            cb.setChecked(oid in self._selected)
            cb.stateChanged.connect(partial(self._on_row_checked, oid))
            cb_wrap = QWidget()
            cb_lay = QHBoxLayout(cb_wrap)
            cb_lay.setContentsMargins(8, 8, 0, 8)
            cb_lay.addWidget(cb)
            cb_lay.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self.table.setCellWidget(i, 0, cb_wrap)

            self.table.setCellWidget(i, 1, self._build_order_cell(r))
            self.table.setCellWidget(i, 2, self._build_product_cell(r))
            self.table.setCellWidget(i, 3, self._build_row_menu(oid))
            self.table.resizeRowToContents(i)
            self.table.setRowHeight(i, max(self.table.rowHeight(i), 148))
            if i and i % _RENDER_BATCH == 0:
                QApplication.processEvents()
        self.table.blockSignals(False)
        self._update_search_visibility()
        self._place_header_select()

    def _sync_row_checkboxes(self) -> None:
        for i, oid in enumerate(self._row_order_ids):
            wrap = self.table.cellWidget(i, 0)
            if wrap is None:
                continue
            cb = wrap.findChild(QCheckBox)
            if cb is None:
                continue
            cb.blockSignals(True)
            cb.setChecked(oid in self._selected)
            cb.blockSignals(False)

    def _build_order_cell(self, row: Dict[str, Any]) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(4)

        oid = QLabel(str(row.get("order_id") or ""))
        oid.setObjectName("sdOrderId")
        lay.addWidget(oid)

        sticker = self._build_sticker_label(row)
        lay.addWidget(sticker)

        created = str(row.get("created_date") or "").strip()
        meta = QLabel("от {}".format(created or "—"))
        meta.setObjectName("sdOrderMeta")
        lay.addWidget(meta)

        badges = QHBoxLayout()
        badges.setContentsMargins(0, 0, 0, 0)
        badges.setSpacing(4)
        ago = str(row.get("created_ago") or "").strip()
        if ago:
            badges.addWidget(make_badge(ago, "time"))
        if row.get("pickup_allowed"):
            badges.addWidget(make_badge("Можно в ПВЗ", "pvz"))
        badges.addStretch(1)
        lay.addLayout(badges)
        lay.addStretch(1)
        return wrap

    @staticmethod
    def _build_sticker_label(row: Dict[str, Any]) -> QLabel:
        part_a = str(row.get("sticker_part_a") or "").strip()
        part_b = str(row.get("sticker_part_b") or "").strip()
        full = str(row.get("sticker_number") or "").strip()
        if (not part_a or not part_b) and full:
            if len(full) > 4:
                part_a, part_b = full[:-4], full[-4:]
            else:
                part_a, part_b = "", full
        lab = QLabel()
        lab.setObjectName("sdSticker")
        lab.setTextFormat(Qt.RichText)
        if not part_a and not part_b:
            lab.setText("—")
        elif not part_b:
            lab.setText(part_a)
        else:
            lab.setText(
                '<span style="font-size:14px;font-weight:700;color:#0f172a;">{}</span>'
                '<span style="font-size:20px;font-weight:700;color:#0f172a;">{}</span>'.format(
                    part_a, part_b
                )
            )
        return lab

    def _build_product_cell(self, row: Dict[str, Any]) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(12)
        lay.setAlignment(Qt.AlignTop)

        photo = make_photo_label(row.get("product_photo"), 120)
        lay.addWidget(photo, 0, Qt.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(4)
        text.setContentsMargins(0, 0, 0, 0)

        name = str(row.get("product_name") or row.get("article") or "—")
        name_lab = QLabel(name)
        name_lab.setObjectName("sdProductName")
        name_lab.setWordWrap(True)
        name_lab.setToolTip(name)
        text.addWidget(name_lab)

        article = str(row.get("article") or "—")
        nm = row.get("nm_id")
        sub = "Арт. {}".format(article)
        if nm not in (None, ""):
            sub += " · nmId {}".format(nm)
        sub_lab = QLabel(sub)
        sub_lab.setObjectName("sdProductSub")
        sub_lab.setWordWrap(True)
        text.addWidget(sub_lab)

        skus = row.get("skus") if isinstance(row.get("skus"), list) else []
        for sku in skus:
            s = str(sku or "").strip()
            if not s:
                continue
            bc = QLabel(s)
            bc.setObjectName("sdBarcode")
            text.addWidget(bc)

        # Web order: cancel badge, then KIZ.
        cancel_label = str(row.get("cancel_reason_label") or "").strip()
        if cancel_label:
            text.addWidget(make_badge(cancel_label, "danger"))
        if row.get("kiz_required"):
            text.addWidget(self._kiz_badge(row))

        text.addStretch(1)
        lay.addLayout(text, 1)
        return wrap

    @staticmethod
    def _kiz_badge(row: Dict[str, Any]) -> QLabel:
        status = str(row.get("kiz_status") or "empty")
        lab = QLabel("КИЗ")
        lab.setObjectName("sdKizBadge")
        if status == "pending":
            lab.setText("На проверке")
            lab.setProperty("kizState", "pending")
            lab.setToolTip("КИЗ отправлен, ожидается проверка Wildberries")
        elif status == "ok":
            lab.setProperty("kizState", "ok")
            lab.setToolTip("Проверка КИЗ пройдена")
        elif status == "error":
            lab.setProperty("kizState", "error")
            lab.setToolTip("Проверка КИЗ не пройдена")
        else:
            lab.setProperty("kizState", "empty")
            lab.setToolTip("Требуется маркировка (КИЗ)")
        # Force style refresh for dynamic property
        lab.style().unpolish(lab)
        lab.style().polish(lab)
        return lab

    def _build_row_menu(self, order_id: int) -> QWidget:
        btn = QToolButton()
        btn.setObjectName("iconBtn")
        btn.setText("⋮")
        btn.setToolTip("Действия")
        btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(btn)
        menu.addAction(
            "Напечатать стикер", partial(self.print_one_order_sticker, order_id)
        )
        btn.setMenu(menu)
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(4, 10, 12, 4)
        lay.addWidget(btn)
        lay.setAlignment(Qt.AlignTop | Qt.AlignRight)
        return wrap

    def print_one_order_sticker(self, order_id: int) -> None:
        try:
            items = self.stickers.order_stickers_png(self.api_key, [int(order_id)])
            pngs = [it["png"] for it in items if it.get("png")]
            if not pngs:
                raise RuntimeError("WB не вернул стикер для заказа {}".format(order_id))
            show_png_list(pngs, "Стикер заказа {}".format(order_id), self)
        except Exception as exc:
            QMessageBox.critical(self, "Стикер", str(exc))

    def _on_row_checked(self, order_id: int, state: int) -> None:
        if state:
            self._selected.add(order_id)
        else:
            self._selected.discard(order_id)
        self._sync_select_all()

    def _sync_select_all(self) -> None:
        ids = [int(r.get("order_id")) for r in self._visible_rows()]
        cb = self.select_all_cb
        cb.blockSignals(True)
        if not ids:
            cb.setChecked(False)
        else:
            cb.setChecked(all(oid in self._selected for oid in ids))
        cb.blockSignals(False)

    def _on_select_all_changed(self, state: int) -> None:
        ids = [int(r.get("order_id")) for r in self._visible_rows()]
        if state:
            self._selected.update(ids)
        else:
            self._selected.difference_update(ids)
        self._sync_row_checkboxes()

    def picking_list(self, variant: str = "summary") -> None:
        if not self._require_actions_ready():
            return
        from app.services.print_docs import print_picking_list

        preloaded = None
        session = supply_session.get_session(self.source_id, self.supply_id)
        if session and session.sticker_numbers:
            preloaded = {
                int(oid): dict(meta) for oid, meta in session.sticker_numbers.items()
            }
        elif str(variant).lower() == "extended" and self._all_rows:
            preloaded = {}
            for row in self._all_rows:
                oid = row.get("order_id")
                if oid is None:
                    continue
                preloaded[int(oid)] = {
                    "partA": row.get("sticker_part_a") or "",
                    "partB": row.get("sticker_part_b") or "",
                    "file_b64": "",
                }

        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            path = print_picking_list(
                self.db,
                self.orders,
                self.source_id,
                self.api_key,
                self.supply_id,
                variant=variant,
                preloaded_stickers=preloaded,
                parent=self,
            )
            self._last_status_note = "открыт {}".format(path.name)
            self.meta.setText(self._last_status_note)
            self.meta.show()
        except Exception as exc:
            QMessageBox.critical(self, "Лист подбора", str(exc))
        finally:
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

    def print_stickers(self) -> None:
        if not self._require_actions_ready():
            return
        from app.services.print_docs import print_supply_stickers

        preloaded, abort = self._sticker_png_for_print()
        if abort:
            return

        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            print_supply_stickers(
                self.db,
                self.orders,
                self.source_id,
                self.api_key,
                self.supply_id,
                order_ids=sorted(self._selected) if self._selected else None,
                parent=self,
                preloaded_stickers=preloaded,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Стикеры", str(exc))
        finally:
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

    def print_qr(self) -> None:
        try:
            city = self.warehouse.text().replace("📍", "").strip()
            if city == "—":
                city = ""
            show_supply_qr(
                self.api_key,
                self.supply_id,
                self,
                order_count=len(self._all_rows),
                city=city,
            )
        except Exception as exc:
            QMessageBox.critical(self, "QR", str(exc))

    def open_portal(self) -> None:
        url = (
            "https://seller.wildberries.ru/marketplace-orders-fbs/supply-detail/packaging"
            "?supplyID={}".format(self.supply_id)
        )
        QDesktopServices.openUrl(QUrl(url))

    def show_cancelled(self) -> None:
        if not self._require_actions_ready():
            return
        from app.services.cancelled import list_cancelled_in_supply, rows_from_detail

        session = supply_session.get_session(self.source_id, self.supply_id)
        stickers = (session.sticker_numbers if session else None) or {}
        if session is not None and session.cancelled_rows is not None:
            rows = [dict(r) for r in session.cancelled_rows]
        else:
            rows = rows_from_detail(self._all_rows, sticker_numbers=stickers)
            if session is not None:
                session.cancelled_rows = [dict(r) for r in rows]
                supply_session.put_session(session)

        dlg = QDialog(self)
        dlg.setWindowTitle("Отменённые заказы · {}".format(self.supply_id))
        prepare_modal_dialog(
            dlg,
            maximized=True,
            default_size=(860, 600),
            minimum_size=(640, 440),
        )
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        title = QLabel("Отменённые заказы")
        title.setObjectName("dialogTitle")
        lay.addWidget(title)
        subtitle = QLabel(
            "Заказы отменены, но всё ещё находятся в этой поставке. "
            "Актуальные статусы — через «Перезапустить проверку»."
        )
        subtitle.setObjectName("hint")
        subtitle.setWordWrap(True)
        lay.addWidget(subtitle)
        head_row = QHBoxLayout()
        head_row.setSpacing(12)
        lead = QLabel("Найдено отменённых в поставке: {}".format(len(rows)))
        lead.setObjectName("hint")
        head_row.addWidget(lead, 1)
        rerun_btn = QPushButton("Перезапустить проверку")
        rerun_btn.setObjectName("secondary")
        head_row.addWidget(rerun_btn, 0)
        lay.addLayout(head_row)
        table = QTableWidget(0, 2)
        table.setObjectName("kizTable")
        table.setAlternatingRowColors(True)
        table.setHorizontalHeaderLabels(["Заказ / стикер", "Товар"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setShowGrid(False)
        hdr = table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        table.setColumnWidth(0, 200)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

        def _fill(items: List[Dict[str, Any]]) -> None:
            table.clearSpans()
            table.setRowCount(len(items))
            for i, r in enumerate(items):
                table.setCellWidget(i, 0, self._build_cancelled_order_cell(r))
                table.setCellWidget(i, 1, self._build_cancelled_product_cell(r))
                table.resizeRowToContents(i)
                table.setRowHeight(i, max(int(table.rowHeight(i)), 120))
            lead.setText("Найдено отменённых в поставке: {}".format(len(items)))
            if not items:
                table.setRowCount(1)
                table.setSpan(0, 0, 1, 2)
                empty = QTableWidgetItem("Отменённых заказов в поставке нет")
                empty.setTextAlignment(Qt.AlignCenter)
                empty.setFlags(Qt.ItemIsEnabled)
                table.setItem(0, 0, empty)

        _fill(rows)

        def _rerun() -> None:
            rerun_btn.setEnabled(False)
            lead.setText("Проверяем статусы…")
            QApplication.processEvents()
            try:
                data2 = list_cancelled_in_supply(
                    self.db,
                    self.source_id,
                    self.api_key,
                    self.supply_id,
                    sticker_numbers=stickers or None,
                )
                items = data2.get("rows") or []
                sess = supply_session.get_session(self.source_id, self.supply_id)
                if sess is not None:
                    sess.cancelled_rows = [dict(r) for r in items]
                    supply_session.put_session(sess)
                self._merge_cancelled_into_detail(items)
                _fill(items)
            except Exception as exc:
                QMessageBox.critical(dlg, "Отменённые", str(exc))
            finally:
                rerun_btn.setEnabled(True)

        rerun_btn.clicked.connect(_rerun)
        lay.addWidget(table, 1)
        dlg.exec_()

    def open_kiz(self) -> None:
        if not self._require_actions_ready():
            return
        from app.ui.kiz_pick_dialogs import KizDialog

        dlg = KizDialog(
            self.kiz, self.source_id, self.api_key, self.supply_id, fullscreen=True
        )
        dlg.exec_()
        if getattr(dlg, "data_changed", False):
            self._refresh_local_row_meta()

    def open_pick(self) -> None:
        if not self._require_actions_ready():
            return
        from app.ui.kiz_pick_dialogs import PickDialog

        dlg = PickDialog(
            self.pick, self.source_id, self.api_key, self.supply_id, fullscreen=True
        )
        dlg.exec_()
        if getattr(dlg, "data_changed", False):
            self._refresh_local_row_meta()

    def _merge_cancelled_into_detail(self, cancelled_rows: List[Dict[str, Any]]) -> None:
        """Sync cancel badges into supply detail rows (web `_wbFbsCancelledMergeIntoDetail`)."""
        by_oid = {
            int(r["order_id"]): str(r.get("cancel_reason_label") or r.get("cancel_reason") or "")
            for r in cancelled_rows
            if r.get("order_id") is not None
        }
        if not by_oid and not self._all_rows:
            return
        changed = False
        for row in self._all_rows:
            oid = int(row.get("order_id") or 0)
            label = by_oid.get(oid) or ""
            prev = str(row.get("cancel_reason_label") or "")
            if label and label != prev:
                row["cancel_reason_label"] = label
                changed = True
            elif not label and prev:
                row.pop("cancel_reason_label", None)
                changed = True
        if changed:
            self._render_table()

    def _build_cancelled_order_cell(self, row: Dict[str, Any]) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(12, 10, 8, 10)
        lay.setSpacing(4)
        oid = QLabel(str(row.get("order_id") or ""))
        oid.setObjectName("sdOrderId")
        lay.addWidget(oid)
        lay.addWidget(self._build_sticker_label(row))
        created = str(row.get("created_date") or "").strip()
        meta = QLabel("от {}".format(created or "—"))
        meta.setObjectName("sdOrderMeta")
        lay.addWidget(meta)
        lay.addStretch(1)
        return wrap

    def _build_cancelled_product_cell(self, row: Dict[str, Any]) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(8, 10, 12, 10)
        lay.setSpacing(12)
        lay.setAlignment(Qt.AlignTop)
        photo = make_photo_label(row.get("product_photo"), size=56)
        lay.addWidget(photo, 0, Qt.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(6)
        text.setContentsMargins(0, 0, 0, 0)
        name = str(row.get("product_name") or row.get("article") or "—")
        name_lab = QLabel(name)
        name_lab.setObjectName("sdProductName")
        name_lab.setWordWrap(True)
        text.addWidget(name_lab)
        article = str(row.get("article") or "—")
        brand = str(row.get("brand") or "").strip()
        sub = "Арт. {}".format(article)
        if brand:
            sub = "{} · {}".format(brand, sub)
        sub_lab = QLabel(sub)
        sub_lab.setObjectName("sdProductSub")
        sub_lab.setWordWrap(True)
        text.addWidget(sub_lab)
        skus = row.get("skus") if isinstance(row.get("skus"), list) else []
        for sku in skus:
            s = str(sku or "").strip()
            if not s:
                continue
            bc = QLabel(s)
            bc.setObjectName("sdBarcode")
            bc.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            bc.setMinimumHeight(28)
            text.addWidget(bc)
        reason = str(
            row.get("cancel_reason_label") or row.get("cancel_reason") or "Отменен"
        )
        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 2, 0, 0)
        badge_row.setSpacing(0)
        badge_row.addWidget(make_badge(reason, "danger"), 0, Qt.AlignLeft)
        badge_row.addStretch(1)
        text.addLayout(badge_row)
        text.addStretch(1)
        lay.addLayout(text, 1)
        wrap.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        return wrap
    def stickers_by_category(self) -> None:
        if not self._require_actions_ready():
            return
        from app.services.print_docs import (
            print_supply_stickers,
            sticker_groups_for_category_print,
        )

        try:
            groups = sticker_groups_for_category_print(
                self.db,
                self.orders,
                self.source_id,
                self.api_key,
                self.supply_id,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Стикеры", str(exc))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Печать по категориям · {}".format(self.supply_id))
        prepare_modal_dialog(
            dlg,
            maximized=True,
            default_size=(720, 560),
            minimum_size=(560, 440),
        )
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Печать стикеров по категориям")
        title.setObjectName("dialogTitle")
        lay.addWidget(title)
        table = QTableWidget(len(groups), 4)
        table.setHorizontalHeaderLabels(["", "Категория", "Товар", "Кол-во"])
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(40)
        for i, g in enumerate(groups):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked)
            chk.setData(Qt.UserRole, list(g.get("order_ids") or []))
            table.setItem(i, 0, chk)
            table.setItem(i, 1, QTableWidgetItem(str(g.get("category") or "")))
            table.setItem(
                i,
                2,
                QTableWidgetItem(
                    "{} · {}".format(g.get("product_name") or "", g.get("article") or "")
                ),
            )
            table.setItem(i, 3, QTableWidgetItem(str(g.get("qty") or 0)))
        lay.addWidget(table, 1)

        def _print_selected() -> None:
            ids = []  # type: List[int]
            for i in range(table.rowCount()):
                item = table.item(i, 0)
                if item and item.checkState() == Qt.Checked:
                    ids.extend(int(x) for x in (item.data(Qt.UserRole) or []))
            if not ids:
                QMessageBox.information(dlg, "Стикеры", "Выберите категории")
                return
            preloaded, abort = self._sticker_png_for_print()
            if abort:
                return
            try:
                print_supply_stickers(
                    self.db,
                    self.orders,
                    self.source_id,
                    self.api_key,
                    self.supply_id,
                    order_ids=ids,
                    parent=self,
                    preloaded_stickers=preloaded,
                )
            except Exception as exc:
                QMessageBox.critical(dlg, "Стикеры", str(exc))

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        print_btn = QPushButton("Печать выбранных")
        print_btn.clicked.connect(_print_selected)
        btns.addButton(print_btn, QDialogButtonBox.ActionRole)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        dlg.exec_()

    def _refresh_local_row_meta(self) -> None:
        """Refresh DB-backed fields without re-fetching WB stickers."""
        rows = self.orders.orders_in_supply(
            self.source_id, self.supply_id, api_key=""
        )
        sticker_keys = (
            "sticker_part_a",
            "sticker_part_b",
            "sticker_number",
            "created_date",
            "created_ago",
            "pickup_allowed",
        )
        for r in rows:
            oid = int(r.get("order_id") or 0)
            old = self._row_by_oid.get(oid) or {}
            for key in sticker_keys:
                if old.get(key):
                    r[key] = old[key]
            if not r.get("created_date"):
                r["created_date"] = format_date_short(r.get("created_at_wb"))
            if not r.get("created_ago"):
                r["created_ago"] = ago_label(r.get("created_at_wb"))
            r["pickup_allowed"] = bool(
                r.get("pickup_allowed") or self._supply_pickup_allowed
            )
            codes = parse_json_list(r.get("kiz_codes_json"))
            r["kiz_codes"] = codes
            has_codes = any(str(c).strip() for c in codes)
            if has_codes:
                r["kiz_required"] = True
                r["kiz_status"] = (
                    "ok" if int(r.get("kiz_wb_synced") or 0) else "pending"
                )
            elif old.get("kiz_required"):
                r["kiz_required"] = old.get("kiz_required")
                r["kiz_status"] = old.get("kiz_status") or "empty"
            else:
                r["kiz_required"] = False
                r["kiz_status"] = "empty"
        payload = {
            "rows": rows,
            "warehouse": str(
                (rows[0].get("warehouse_label") or rows[0].get("warehouse_id") or "")
                if rows
                else ""
            ),
        }
        session = supply_session.get_session(self.source_id, self.supply_id)
        if session:
            session.rows = rows
            session.apply_sticker_numbers_to_rows()
            session.build_kiz_and_pick_rows(self.db)
            supply_session.put_session(session)
            payload = supply_session.snapshot_for_ui(session)
        supply_detail_cache.put(self.source_id, self.supply_id, payload)
        self._apply_loaded_payload(payload)

    def manage_trbx(self) -> None:
        if not self._require_actions_ready():
            return
        supply = self.orders.get_supply(self.source_id, self.supply_id) or {}
        dlg = TrbxDialog(
            self.trbx,
            self.source_id,
            self.api_key,
            self.supply_id,
            order_count=len(self._all_rows),
            supply_done=bool(supply.get("done")),
            warehouse=str(
                self.warehouse.text().replace("📍", "").strip()
            ).replace("—", ""),
            parent=self,
        )
        dlg.exec_()
        self.supply_mutated = True
        self._apply_supply_header(
            self.orders.get_supply(self.source_id, self.supply_id)
        )
        self._refresh_local_row_meta()

    def refresh_kiz_status(self) -> None:
        if not self._require_actions_ready():
            return
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            rows = self.kiz.marking_rows(self.source_id, self.supply_id, self.api_key)
            by_oid = {int(r["order_id"]): r for r in rows if r.get("order_id") is not None}
            for r in self._all_rows:
                oid = int(r.get("order_id") or 0)
                src = by_oid.get(oid)
                if not src:
                    r["kiz_required"] = False
                    continue
                r["kiz_required"] = True
                r["kiz_codes"] = list(src.get("kiz_codes") or [])
                if src.get("kiz_error"):
                    r["kiz_status"] = "error"
                elif any(str(c).strip() for c in (src.get("kiz_codes") or [])):
                    r["kiz_status"] = (
                        "ok" if src.get("kiz_wb_synced") else "pending"
                    )
                else:
                    r["kiz_status"] = "empty"
            self._sync_kiz_session(rows)
            self._render_table()
            self._last_status_note = "Статусы КИЗ обновлены"
            self.meta.setText(self._last_status_note)
            self.meta.show()
        except Exception as exc:
            QMessageBox.critical(self, "КИЗ", str(exc))
        finally:
            QApplication.restoreOverrideCursor()


class TrbxDialog(QDialog):
    """Грузоместа (TRBX) — web parity: stepper, boxes table, QR / delete actions."""

    def __init__(
        self,
        trbx: TrbxService,
        source_id: int,
        api_key: str,
        supply_id: str,
        order_count: int = 0,
        supply_done: bool = False,
        warehouse: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super(TrbxDialog, self).__init__(parent)
        self.trbx = trbx
        self.source_id = source_id
        self.api_key = api_key
        self.supply_id = supply_id
        self.order_count = int(order_count or 0)
        self.supply_done = bool(supply_done)
        self.warehouse = str(warehouse or "").strip()
        self.boxes = []  # type: List[Dict[str, Any]]

        self.setWindowTitle("Грузоместа для поставки в ПВЗ")
        prepare_modal_dialog(
            self,
            maximized=True,
            default_size=(720, 560),
            minimum_size=(560, 440),
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title = QLabel("Создайте грузоместа для поставки в ПВЗ")
        title.setObjectName("dialogTitle")
        title.setWordWrap(True)
        root.addWidget(title)

        lead = QLabel(
            "1 короб = 1 грузоместо. Затем распечатайте QR-коды грузомест и наклейте"
            " их на каждый короб. Сделайте это до передачи в доставку."
        )
        lead.setObjectName("hint")
        lead.setWordWrap(True)
        root.addWidget(lead)

        stepper_row = QHBoxLayout()
        stepper_row.setSpacing(8)
        self.minus_btn = QPushButton("−")
        self.minus_btn.setObjectName("secondary")
        self.minus_btn.setFixedWidth(44)
        self.minus_btn.clicked.connect(partial(self._step_amount, -1))
        self.amount = QSpinBox()
        self.amount.setRange(1, 1000)
        self.amount.setValue(1)
        self.amount.setMinimumWidth(88)
        self.plus_btn = QPushButton("+")
        self.plus_btn.setObjectName("secondary")
        self.plus_btn.setFixedWidth(44)
        self.plus_btn.clicked.connect(partial(self._step_amount, 1))
        self.create_btn = QPushButton("Создать")
        self.create_btn.clicked.connect(self.create_boxes)
        stepper_row.addWidget(self.minus_btn)
        stepper_row.addWidget(self.amount)
        stepper_row.addWidget(self.plus_btn)
        stepper_row.addSpacing(8)
        stepper_row.addWidget(self.create_btn)
        stepper_row.addStretch(1)
        root.addLayout(stepper_row)

        self.info = QLabel("")
        self.info.setObjectName("hint")
        self.info.setWordWrap(True)
        root.addWidget(self.info)

        self.table = QTableWidget(0, 2)
        self.table.setAlternatingRowColors(True)
        self.table.setHorizontalHeaderLabels(["Грузоместо", "Действия"])
        self.table.setColumnWidth(1, 96)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        root.addWidget(self.table, 1)

        action_bar = FlowLayout(h_spacing=8, v_spacing=8)
        self.print_all_btn = QPushButton("Распечатать QR-коды грузовых мест")
        self.print_all_btn.setObjectName("secondary")
        self.print_all_btn.clicked.connect(self.print_stickers)
        self.print_supply_qr_btn = QPushButton("Распечатать QR-код поставки")
        self.print_supply_qr_btn.setObjectName("secondary")
        self.print_supply_qr_btn.setToolTip("QR-код поставки (WB-GI)")
        self.print_supply_qr_btn.clicked.connect(self.print_supply_qr)
        self.delete_all_btn = QPushButton("Удалить все")
        self.delete_all_btn.setObjectName("danger")
        self.delete_all_btn.clicked.connect(self.delete_all)
        action_bar.addWidget(self.print_all_btn)
        action_bar.addWidget(self.print_supply_qr_btn)
        action_bar.addWidget(self.delete_all_btn)
        root.addLayout(action_bar)

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        root.addWidget(close)
        self.reload()
        self._apply_done_state()

    def _apply_done_state(self) -> None:
        if not self.supply_done:
            return
        self.create_btn.setEnabled(False)
        self.minus_btn.setEnabled(False)
        self.plus_btn.setEnabled(False)
        self.amount.setEnabled(False)
        self.delete_all_btn.setEnabled(False)
        self.info.setText(
            "Поставка уже закрыта — создать грузоместа нельзя. "
            "Можно распечатать QR существующих."
        )

    def _remaining(self) -> Optional[int]:
        if self.order_count <= 0:
            return None
        return max(0, self.order_count + 1 - len(self.boxes))

    def _step_amount(self, delta: int) -> None:
        self.amount.setValue(self.amount.value() + delta)

    def _sync_amount_range(self) -> None:
        if self.supply_done:
            return
        remaining = self._remaining()
        if remaining is None:
            self.amount.setMaximum(1000)
            self.create_btn.setEnabled(True)
            return
        self.amount.setMaximum(max(1, remaining))
        if remaining < 1:
            self.create_btn.setEnabled(False)
            self.info.setText(
                "Лимит грузомест достигнут (макс. {} = заказы + 1)".format(
                    self.order_count + 1
                )
            )
        else:
            self.create_btn.setEnabled(True)
            self.info.setText("Можно добавить ещё: {}".format(remaining))

    @staticmethod
    def _box_id(box: Any) -> str:
        if isinstance(box, dict):
            return str(box.get("id") or box.get("trbxId") or "").strip()
        return str(box or "").strip()

    def reload(self) -> None:
        try:
            boxes = self.trbx.refresh(self.source_id, self.api_key, self.supply_id)
        except Exception:
            boxes = self.trbx.list_boxes(self.source_id, self.supply_id)
        self.boxes = [b for b in boxes if self._box_id(b)]
        self._render_boxes()
        self._sync_amount_range()
        self._apply_done_state()

    def _render_boxes(self) -> None:
        self.table.setRowCount(len(self.boxes))
        for i, b in enumerate(self.boxes):
            bid = self._box_id(b)
            self.table.setItem(i, 0, QTableWidgetItem(bid))
            self.table.setCellWidget(i, 1, self._build_box_actions(bid))
        has_boxes = bool(self.boxes)
        self.print_all_btn.setEnabled(has_boxes)
        self.delete_all_btn.setEnabled(has_boxes and not self.supply_done)

    def _build_box_actions(self, box_id: str) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)
        print_btn = QToolButton()
        print_btn.setObjectName("secondary")
        print_btn.setText("QR")
        print_btn.setToolTip("Печать QR грузоместа {}".format(box_id))
        print_btn.clicked.connect(partial(self.print_one_box, box_id))
        lay.addWidget(print_btn)
        if not self.supply_done:
            delete_btn = QToolButton()
            delete_btn.setObjectName("dangerToolBtn")
            delete_btn.setText("✕")
            delete_btn.setToolTip("Удалить грузоместо {}".format(box_id))
            delete_btn.clicked.connect(partial(self.delete_one_box, box_id))
            lay.addWidget(delete_btn)
        lay.addStretch(1)
        return wrap

    def create_boxes(self) -> None:
        if self.supply_done:
            QMessageBox.information(
                self,
                "Грузоместа",
                "Поставка уже закрыта — создать грузоместа нельзя.",
            )
            return
        try:
            self.trbx.create(
                self.source_id,
                self.api_key,
                self.supply_id,
                int(self.amount.value()),
                order_count=self.order_count,
            )
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Грузоместа", str(exc))

    def delete_one_box(self, box_id: str) -> None:
        if self.supply_done:
            return
        if (
            QMessageBox.question(
                self, "Грузоместа", "Удалить грузоместо {}?".format(box_id)
            )
            != QMessageBox.Yes
        ):
            return
        try:
            self.trbx.delete_one(self.source_id, self.api_key, self.supply_id, box_id)
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Грузоместа", str(exc))

    def delete_all(self) -> None:
        if self.supply_done or not self.boxes:
            return
        if (
            QMessageBox.question(self, "Грузоместа", "Удалить все грузоместа?")
            != QMessageBox.Yes
        ):
            return
        try:
            self.trbx.delete_all(self.source_id, self.api_key, self.supply_id)
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Грузоместа", str(exc))

    def print_one_box(self, box_id: str) -> None:
        try:
            pngs = self.trbx.stickers_png(self.api_key, self.supply_id, [box_id])
            if not pngs:
                raise RuntimeError("WB не вернул QR для грузоместа {}".format(box_id))
            show_png_list(pngs, "QR грузоместа {}".format(box_id), self)
        except Exception as exc:
            QMessageBox.critical(self, "Грузоместа", str(exc))

    def print_stickers(self) -> None:
        ids = [self._box_id(b) for b in self.boxes if self._box_id(b)]
        if not ids:
            QMessageBox.information(self, "Грузоместа", "Нет грузомест")
            return
        try:
            pngs = self.trbx.stickers_png(self.api_key, self.supply_id, ids)
            show_png_list(pngs, "QR-коды грузовых мест", self)
        except Exception as exc:
            QMessageBox.critical(self, "Грузоместа", str(exc))

    def print_supply_qr(self) -> None:
        try:
            show_supply_qr(
                self.api_key,
                self.supply_id,
                self,
                order_count=self.order_count,
                city=self.warehouse,
            )
        except Exception as exc:
            QMessageBox.critical(self, "QR поставки", str(exc))
