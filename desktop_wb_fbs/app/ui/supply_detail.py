# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QRect, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QCursor, QDesktopServices, QPainter
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
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyleOptionToolButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

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
    make_modal_search_box,
    prepare_modal_dialog,
    style_app_menu,
)
from app.ui.dialogs_extra import show_png_list, show_supply_qr
from app.ui.format_helpers import (
    AdaptiveWrapLabel,
    ago_label,
    build_order_cell_widget,
    build_product_cell_widget,
    build_sticker_number_label,
    format_date_short,
    make_badge,
    make_photo_label,
)
from app.ui.layout_utils import FlowLayout
from app.ui.table_col_widths import PersistentColumnWidths
from app.wb import cargo_type_label, parse_json_list

_RENDER_BATCH = 50
_WAIT_ORDERS_TIP = "Дождитесь загрузки заказов"
_LOAD_STEPS = (
    "Заказы",
    "Номера стикеров",
    "КИЗ и проверка ШК",
)


class _SpinRefreshButton(QToolButton):
    """Refresh glyph that rotates while a live КИЗ check is in flight (web spin)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super(_SpinRefreshButton, self).__init__(parent)
        self._angle = 0
        self._spinning = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self.setText("↻")

    def set_spinning(self, on: bool) -> None:
        self._spinning = bool(on)
        if self._spinning:
            if not self._timer.isActive():
                self._timer.start(50)
        else:
            self._timer.stop()
            self._angle = 0
            self.update()

    def _on_tick(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._spinning:
            super(_SpinRefreshButton, self).paintEvent(event)
            return
        opt = QStyleOptionToolButton()
        self.initStyleOption(opt)
        opt.text = ""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self.style().drawComplexControl(QStyle.CC_ToolButton, opt, painter, self)
        painter.translate(self.rect().center())
        painter.rotate(self._angle)
        painter.setPen(self.palette().buttonText().color())
        painter.drawText(QRect(-12, -12, 24, 24), Qt.AlignCenter, "↻")
        painter.end()


class _KizStatusWorker(QThread):
    """Background live КИЗ status check (POST /orders/meta)."""

    ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        kiz: KizService,
        source_id: int,
        supply_id: str,
        api_key: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_KizStatusWorker, self).__init__(parent)
        self.kiz = kiz
        self.source_id = source_id
        self.supply_id = supply_id
        self.api_key = api_key

    def run(self) -> None:
        try:
            payload = self.kiz.check_supply_status(
                self.source_id, self.supply_id, self.api_key
            )
            self.ready.emit(payload)
        except Exception as exc:
            self.failed.emit(str(exc))


class _PickStatusWorker(QThread):
    """Background local pick ШК / sticker completeness check."""

    ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        pick: PickVerifyService,
        source_id: int,
        supply_id: str,
        api_key: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_PickStatusWorker, self).__init__(parent)
        self.pick = pick
        self.source_id = source_id
        self.supply_id = supply_id
        self.api_key = api_key

    def run(self) -> None:
        try:
            payload = self.pick.check_supply_status(
                self.source_id, self.supply_id, self.api_key
            )
            self.ready.emit(payload)
        except Exception as exc:
            self.failed.emit(str(exc))


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
        self._kiz_status_worker = None  # type: Optional[_KizStatusWorker]
        self._kiz_status_gen = 0
        self._kiz_status_refreshing = False
        self._alive_workers = []  # type: List[QThread]
        self._load_gen = 0
        self._loading = False
        self._load_step = 0
        self._load_detail = ""
        self._actions_ready = False
        self._action_widgets = []  # type: List[QWidget]
        self._saved_tooltips = {}  # type: Dict[QWidget, str]
        self.supply_mutated = False
        self.kiz_btn = None  # type: Optional[QPushButton]
        self.kiz_ref = None  # type: Optional[_SpinRefreshButton]
        self._kiz_split = None  # type: Optional[QWidget]
        self.pick_btn = None  # type: Optional[QPushButton]
        self.pick_ref = None  # type: Optional[_SpinRefreshButton]
        self._pick_split = None  # type: Optional[QWidget]
        self._pick_status_worker = None  # type: Optional[QThread]
        self._pick_status_gen = 0
        self._pick_status_refreshing = False

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
        hv.setContentsMargins(24, 12, 24, 10)
        hv.setSpacing(8)

        # One compact top row: title · warehouse · chips
        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        title_row.setAlignment(Qt.AlignVCenter)
        self.header = QLabel("Поставка")
        self.header.setObjectName("sdTitle")
        self.header.setWordWrap(False)
        self.header.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.header.setMaximumWidth(380)
        title_row.addWidget(self.header, 0, Qt.AlignVCenter)

        self.warehouse = QLabel("📍 —")
        self.warehouse.setObjectName("sdWarehouse")
        self.warehouse.setWordWrap(False)
        self.warehouse.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.warehouse.setMaximumWidth(220)
        title_row.addWidget(self.warehouse, 0, Qt.AlignVCenter)

        chips_wrap = QWidget()
        chips_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.meta_chips = FlowLayout(chips_wrap, h_spacing=8, v_spacing=4)
        title_row.addWidget(chips_wrap, 1, Qt.AlignVCenter)
        hv.addLayout(title_row)

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

        actions_row = QHBoxLayout()
        actions_row.setSpacing(12)
        actions_row.setAlignment(Qt.AlignVCenter)
        actions_wrap = QWidget()
        actions_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        actions = FlowLayout(actions_wrap, h_spacing=8, v_spacing=8)

        def _sec(btn):
            btn.setObjectName("secondary")
            btn.setMinimumHeight(40)
            btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            return btn

        pick_btn = _sec(QPushButton("Лист подбора"))
        pick_btn.clicked.connect(partial(self.picking_list, "summary"))
        pick_caret = QToolButton()
        pick_caret.setObjectName("splitCaret")
        pick_caret.setText("▾")
        pick_caret.setPopupMode(QToolButton.InstantPopup)
        pick_caret.setToolButtonStyle(Qt.ToolButtonTextOnly)
        pick_caret.setArrowType(Qt.NoArrow)
        pick_caret.setFixedSize(36, 40)
        pick_menu = style_app_menu(QMenu(pick_caret))
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
        st_caret.setToolButtonStyle(Qt.ToolButtonTextOnly)
        st_caret.setArrowType(Qt.NoArrow)
        st_caret.setFixedSize(36, 40)
        st_menu = style_app_menu(QMenu(st_caret))
        st_menu.addAction("Печать по категориям", self.stickers_by_category)
        st_caret.setMenu(st_menu)
        actions.addWidget(self._split_pair(st_btn, st_caret))

        kiz_btn = _sec(QPushButton("Товары с маркировкой"))
        kiz_btn.clicked.connect(self.open_kiz)
        kiz_ref = _sec(_SpinRefreshButton())
        kiz_ref.setToolTip("Проверить статусы КИЗ на ВБ")
        kiz_ref.clicked.connect(self.refresh_kiz_status)
        kiz_split = self._split_pair(kiz_btn, kiz_ref)
        kiz_split.setObjectName("kizSplitPair")
        self.kiz_btn = kiz_btn
        self.kiz_ref = kiz_ref
        self._kiz_split = kiz_split
        actions.addWidget(kiz_split)

        pick_verify_btn = _sec(QPushButton("Товары без маркировки"))
        pick_verify_btn.clicked.connect(self.open_pick)
        pick_ref = _sec(_SpinRefreshButton())
        pick_ref.setToolTip("Проверить заполнение стикеров и ШК")
        pick_ref.clicked.connect(self.refresh_pick_status)
        pick_split = self._split_pair(pick_verify_btn, pick_ref)
        pick_split.setObjectName("pickSplitPair")
        self.pick_btn = pick_verify_btn
        self.pick_ref = pick_ref
        self._pick_split = pick_split
        actions.addWidget(pick_split)

        extra_action_btns = []  # type: List[QPushButton]
        for text, slot in (
            ("Грузоместа", self.manage_trbx),
            ("Отмененные заказы", self.show_cancelled),
        ):
            btn = _sec(QPushButton(text))
            btn.clicked.connect(slot)
            actions.addWidget(btn)
            extra_action_btns.append(btn)

        portal_btn = QPushButton("Портал ВБ  →")
        portal_btn.setObjectName("portalBtn")
        portal_btn.setMinimumHeight(40)
        portal_btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        portal_btn.setToolTip("Открыть поставку на портале Wildberries")
        portal_btn.clicked.connect(self.open_portal)
        actions.addWidget(portal_btn)
        actions_row.addWidget(actions_wrap, 1)

        search_box, self.search_input = make_modal_search_box()
        self.search_input.textChanged.connect(
            lambda _t: self._update_search_visibility()
        )
        actions_row.addWidget(search_box, 0, Qt.AlignRight | Qt.AlignVCenter)
        hv.addLayout(actions_row)

        self._action_widgets = [
            pick_btn,
            pick_caret,
            st_btn,
            st_caret,
            kiz_btn,
            kiz_ref,
            pick_verify_btn,
            pick_ref,
            *extra_action_btns,
            portal_btn,
            self.search_input,
            search_box,
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
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(148)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        self._col_widths = PersistentColumnWidths(
            self.db,
            self.table,
            "supply_detail_table_cols",
            [40, 200, 420, 52],
            parent=self,
        )
        self._col_widths.apply()

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
        if ready and self._kiz_status_refreshing:
            self._set_kiz_refresh_busy(True)
        if ready and self._pick_status_refreshing:
            self._set_pick_refresh_busy(True)

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

    def _sync_kiz_session(self, status_rows: List[Dict[str, Any]]) -> None:
        """Merge live КИЗ status into session without wiping product fields.

        ``check_supply_status`` returns status-only rows (no name/article/photo).
        Replacing ``session.kiz_rows`` with those made Маркировка empty after refresh.
        """
        session = supply_session.get_session(self.source_id, self.supply_id)
        if not session:
            return
        status_by_oid = {
            int(r["order_id"]): r
            for r in status_rows
            if isinstance(r, dict) and r.get("order_id") is not None
        }
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
            if "kiz_decision" in src:
                r["kiz_decision"] = src.get("kiz_decision") or ""
            if "kiz_wb_synced" in src:
                r["kiz_wb_synced"] = src.get("kiz_wb_synced")
            if src.get("cancel_reason_label"):
                r["cancel_reason_label"] = src.get("cancel_reason_label")

        prev_by_oid = {
            int(r["order_id"]): dict(r)
            for r in (session.kiz_rows or [])
            if isinstance(r, dict) and r.get("order_id") is not None
        }
        detail_by_oid = {
            int(r["order_id"]): r
            for r in (session.rows or [])
            if isinstance(r, dict) and r.get("order_id") is not None
        }

        def _fill_product(kr: Dict[str, Any], detail: Dict[str, Any]) -> None:
            if not str(kr.get("article") or "").strip():
                kr["article"] = str(detail.get("article") or "").strip()
            if kr.get("nm_id") in (None, ""):
                kr["nm_id"] = detail.get("nm_id")
            if not str(kr.get("product_name") or "").strip():
                kr["product_name"] = str(
                    detail.get("product_name") or detail.get("name") or ""
                ).strip()
            if not str(kr.get("product_photo") or "").strip():
                kr["product_photo"] = str(
                    detail.get("product_photo")
                    or detail.get("photo_path")
                    or ""
                ).strip()
            if not str(kr.get("brand") or "").strip():
                kr["brand"] = str(detail.get("brand") or "").strip()
            if not kr.get("skus"):
                skus = detail.get("skus")
                if isinstance(skus, list):
                    kr["skus"] = list(skus)
                else:
                    kr["skus"] = parse_json_list(detail.get("skus_json"))
            if not str(kr.get("created_date") or "").strip():
                kr["created_date"] = str(detail.get("created_date") or "—")
            if not str(kr.get("created_ago") or "").strip():
                kr["created_ago"] = str(detail.get("created_ago") or "")
            if "skip_kiz_gtin_check" not in kr and "skip_kiz_gtin_check" in detail:
                kr["skip_kiz_gtin_check"] = bool(detail.get("skip_kiz_gtin_check"))
            if not str(kr.get("sticker_barcode") or "").strip():
                kr["sticker_barcode"] = str(detail.get("sticker_barcode") or "")

        kiz_rows = []  # type: List[Dict[str, Any]]
        # Prefer required orders from live status; if caller passed a mixed list,
        # keep only those that belong in Маркировка.
        for oid, src in status_by_oid.items():
            if not src.get("kiz_required"):
                continue
            detail = detail_by_oid.get(oid) or by_ui_oid.get(oid) or {}
            kr = prev_by_oid.get(oid) or {}
            kr = dict(kr)
            _fill_product(kr, detail)
            kr["order_id"] = oid
            kr["kiz_required"] = True
            kr["kiz_codes"] = list(src.get("kiz_codes") or kr.get("kiz_codes") or [""])
            kr["kiz_status"] = str(src.get("kiz_status") or kr.get("kiz_status") or "empty")
            kr["kiz_decision"] = str(src.get("kiz_decision") or "")
            if "kiz_wb_synced" in src:
                kr["kiz_wb_synced"] = bool(src.get("kiz_wb_synced"))
            elif kr.get("kiz_status") == "ok":
                kr["kiz_wb_synced"] = True
            st = session.sticker_numbers.get(oid) or {}
            part_a = str(
                st.get("partA") or kr.get("sticker_part_a") or detail.get("sticker_part_a") or ""
            ).strip()
            part_b = str(
                st.get("partB") or kr.get("sticker_part_b") or detail.get("sticker_part_b") or ""
            ).strip()
            kr["sticker_part_a"] = part_a
            kr["sticker_part_b"] = part_b
            kr["sticker_number"] = (
                "{}{}".format(part_a, part_b)
                if (part_a or part_b)
                else str(kr.get("sticker_number") or detail.get("sticker_number") or "")
            )
            if src.get("cancelled") or src.get("cancel_reason_label"):
                kr["cancel_reason_label"] = str(
                    src.get("cancel_reason_label")
                    or kr.get("cancel_reason_label")
                    or "Отменен"
                ).strip() or "Отменен"
            elif detail.get("cancel_reason_label"):
                kr["cancel_reason_label"] = detail.get("cancel_reason_label")
            if detail.get("supplier_status") is not None:
                kr.setdefault("supplier_status", detail.get("supplier_status"))
            if detail.get("wb_status") is not None:
                kr.setdefault("wb_status", detail.get("wb_status"))
            kiz_rows.append(kr)

        # If live status had no required rows but we still got a payload, keep
        # previous kiz_rows and only patch statuses when oids match.
        if not kiz_rows and prev_by_oid and status_by_oid:
            for oid, kr0 in prev_by_oid.items():
                kr = dict(kr0)
                src = status_by_oid.get(oid)
                if src:
                    kr["kiz_codes"] = list(
                        src.get("kiz_codes") or kr.get("kiz_codes") or [""]
                    )
                    kr["kiz_status"] = str(
                        src.get("kiz_status") or kr.get("kiz_status") or "empty"
                    )
                    kr["kiz_decision"] = str(src.get("kiz_decision") or "")
                    if "kiz_wb_synced" in src:
                        kr["kiz_wb_synced"] = bool(src.get("kiz_wb_synced"))
                kiz_rows.append(kr)

        session.kiz_rows = kiz_rows
        supply_session.put_session(session)
        supply_detail_cache.put(
            self.source_id,
            self.supply_id,
            supply_session.snapshot_for_ui(session),
        )

    def accept(self) -> None:
        self._stop_kiz_status_worker()
        self._stop_pick_status_worker()
        self._stop_load_worker()
        self._loading = False
        self._teardown_table()
        super(SupplyDetailDialog, self).accept()

    def reject(self) -> None:
        self._stop_kiz_status_worker()
        self._stop_pick_status_worker()
        self._stop_load_worker()
        self._loading = False
        self._teardown_table()
        super(SupplyDetailDialog, self).reject()

    def closeEvent(self, event) -> None:
        self._stop_kiz_status_worker()
        self._stop_pick_status_worker()
        self._stop_load_worker()
        self._loading = False
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
        # Do not clear ``_loading`` here — ``_begin_load`` sets it after stop;
        # clearing it dropped every progress update (statuses stayed frozen).

    def _stop_kiz_status_worker(self) -> None:
        self._kiz_status_gen += 1
        worker = self._kiz_status_worker
        self._kiz_status_worker = None
        if worker is not None:
            self._disconnect_worker(worker, "ready", "failed")
        self._set_kiz_refresh_busy(False)

    def _stop_pick_status_worker(self) -> None:
        self._pick_status_gen += 1
        worker = self._pick_status_worker
        self._pick_status_worker = None
        if worker is not None:
            self._disconnect_worker(worker, "ready", "failed")
        self._set_pick_refresh_busy(False)

    def _set_kiz_split_tone(self, tone: str) -> None:
        """Web ``_wbFbsKizSplitSetTone``: ok→green, error→red, else default."""
        wrap = self._kiz_split
        if wrap is None:
            return
        t = str(tone or "").strip().lower()
        if t == "ok":
            wrap.setProperty("kizTone", "ok")
        elif t == "error":
            wrap.setProperty("kizTone", "error")
        else:
            wrap.setProperty("kizTone", "")
        wrap.style().unpolish(wrap)
        wrap.style().polish(wrap)
        for child in wrap.findChildren(QWidget):
            child.style().unpolish(child)
            child.style().polish(child)
            child.update()
        wrap.update()

    def _set_pick_split_tone(self, tone: str) -> None:
        """Green only when every pick row has sticker + valid ШК."""
        wrap = self._pick_split
        if wrap is None:
            return
        t = str(tone or "").strip().lower()
        wrap.setProperty("pickTone", "ok" if t == "ok" else "")
        wrap.style().unpolish(wrap)
        wrap.style().polish(wrap)
        for child in wrap.findChildren(QWidget):
            child.style().unpolish(child)
            child.style().polish(child)
            child.update()
        wrap.update()

    def _set_kiz_refresh_busy(self, busy: bool) -> None:
        self._kiz_status_refreshing = bool(busy)
        ref = self.kiz_ref
        btn = self.kiz_btn
        if isinstance(ref, _SpinRefreshButton):
            ref.set_spinning(busy)
        if busy:
            if ref is not None:
                ref.setEnabled(False)
            if btn is not None:
                btn.setEnabled(False)
        elif self._actions_ready:
            if ref is not None:
                ref.setEnabled(True)
            if btn is not None:
                btn.setEnabled(True)

    def _set_pick_refresh_busy(self, busy: bool) -> None:
        self._pick_status_refreshing = bool(busy)
        ref = self.pick_ref
        btn = self.pick_btn
        if isinstance(ref, _SpinRefreshButton):
            ref.set_spinning(busy)
        if busy:
            if ref is not None:
                ref.setEnabled(False)
            if btn is not None:
                btn.setEnabled(False)
        elif self._actions_ready:
            if ref is not None:
                ref.setEnabled(True)
            if btn is not None:
                btn.setEnabled(True)

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
        """Main + caret/refresh: same height (web picking-split)."""
        wrap = QWidget()
        wrap.setObjectName("splitPair")
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        for w in (main, caret):
            w.setMinimumHeight(40)
            w.setMaximumHeight(40)
            w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        if str(caret.objectName() or "") == "splitCaret":
            caret.setFixedSize(36, 40)
        lay.addWidget(main)
        lay.addWidget(caret)
        wrap.setMinimumHeight(40)
        wrap.setMaximumHeight(40)
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
        chip_h = 32
        if qr:
            chip = QFrame()
            chip.setObjectName("sdChipQr")
            chip.setFixedHeight(chip_h)
            lay = QHBoxLayout(chip)
            lay.setContentsMargins(10, 0, 4, 0)
            lay.setSpacing(6)
            lab = QLabel(text)
            lab.setObjectName("sdChipQrText")
            lab.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            lay.addWidget(lab)
            btn = QToolButton()
            btn.setObjectName("sdQrPrint")
            btn.setText("🖨")
            btn.setFixedSize(28, 28)
            btn.setToolTip("Распечатать QR-код поставки")
            btn.clicked.connect(self.print_qr)
            lay.addWidget(btn, 0, Qt.AlignVCenter)
            self.meta_chips.addWidget(chip)
            return chip
        lab = QLabel(text)
        lab.setObjectName("sdChip")
        lab.setFixedHeight(chip_h)
        lab.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
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
            self.header.setToolTip(self.header.text())
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
        self.header.setToolTip(name)
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
        self._clear_loading_table()
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

    def _clear_loading_table(self) -> None:
        """Remove the loading overlay so it cannot linger over rendered rows.

        Qt can leave a spanned cell-widget painted on the viewport after
        ``clearSpans`` / ``setRowCount`` if we only replace widgets later —
        especially when ``processEvents`` runs mid-render on re-open.
        """
        self._loading_table_label = None
        if not hasattr(self, "table") or self.table is None:
            return
        self.table.blockSignals(True)
        try:
            cols = max(1, int(self.table.columnCount()))
            for col in range(cols):
                w = self.table.cellWidget(0, col)
                if w is None:
                    continue
                self.table.removeCellWidget(0, col)
                w.hide()
                w.setParent(None)
                w.deleteLater()
            self.table.clearSpans()
        finally:
            self.table.blockSignals(False)

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
        if not self._loading and int(self._load_step or 0) <= 0:
            # Initial placeholder before worker starts — still show header checklist.
            pass
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
        if not self._loading:
            # Stale progress after data is already on screen — ignore.
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
        if lab is not None and self._loading and step > 0:
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
                self._loading = False
                self._clear_loading_table()
                self._apply_loaded_payload(supply_session.snapshot_for_ui(session))
                self._set_load_status("")
                self._set_actions_ready(True)
                return

        self._set_actions_ready(False)
        self._stop_load_worker()
        self._loading = True
        self._load_step = 0
        self._load_detail = ""
        self._render_load_status()
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
        worker = self._load_worker
        if (
            not self._loading
            or worker is None
            or int(getattr(worker, "generation", -1)) != self._load_gen
        ):
            return
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
        # Drop loading overlay before paint; processEvents mid-render must not
        # revive the «Идёт загрузка…» label.
        self._loading = False
        self._load_worker = None
        self._load_step = 0
        self._load_detail = ""
        self._clear_loading_table()
        self._set_load_status("")
        self._apply_loaded_payload(data)
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
        self._clear_loading_table()
        self.table.blockSignals(True)
        self.table.setRowCount(1)
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
            self.warehouse.setToolTip(warehouse)
        elif not rows:
            supply = self.orders.get_supply(self.source_id, self.supply_id) or {}
            dest = supply.get("destination_office_id")
            text = "📍 {}".format(dest) if dest else "📍 —"
            self.warehouse.setText(text)
            self.warehouse.setToolTip(str(dest or ""))

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
        self._clear_loading_table()
        rows = list(self._all_rows)
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
        return build_order_cell_widget(row)

    @staticmethod
    def _build_sticker_label(row: Dict[str, Any]) -> QLabel:
        return build_sticker_number_label(row)

    def _build_product_cell(self, row: Dict[str, Any]) -> QWidget:
        extras = []
        if row.get("kiz_required"):
            extras.append(self._kiz_badge(row))
        return build_product_cell_widget(row, extra_widgets=extras)

    @staticmethod
    def _kiz_badge(row: Dict[str, Any]) -> QLabel:
        status = str(row.get("kiz_status") or "empty")
        lab = QLabel("КИЗ")
        lab.setObjectName("sdKizBadge")
        lab.setAlignment(Qt.AlignCenter)
        lab.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # Keep label text short («КИЗ») so the colored pill does not stick out;
        # status is conveyed by color + tooltip.
        if status == "pending":
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
        btn.setObjectName("kizRowMenu")
        btn.setText("⋮")
        btn.setToolTip("Действия")
        btn.setPopupMode(QToolButton.InstantPopup)
        btn.setFixedSize(32, 32)
        menu = style_app_menu(QMenu(btn))
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
        if str(variant).lower() == "extended":
            preloaded = {}
            if session and session.sticker_numbers:
                for oid, meta in session.sticker_numbers.items():
                    if oid is None:
                        continue
                    preloaded[int(oid)] = dict(meta)
            for row in self._all_rows or []:
                oid = row.get("order_id")
                if oid is None:
                    continue
                oid = int(oid)
                prev = preloaded.get(oid) or {}
                part_a = str(
                    prev.get("partA") or row.get("sticker_part_a") or ""
                ).strip()
                part_b = str(
                    prev.get("partB") or row.get("sticker_part_b") or ""
                ).strip()
                preloaded[oid] = {
                    "partA": part_a,
                    "partB": part_b,
                    "file_b64": str(prev.get("file_b64") or ""),
                }
        elif session and session.sticker_numbers:
            preloaded = {
                int(oid): dict(meta) for oid, meta in session.sticker_numbers.items()
            }

        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            print_picking_list(
                self.db,
                self.orders,
                self.source_id,
                self.api_key,
                self.supply_id,
                variant=variant,
                preloaded_stickers=preloaded,
                parent=self,
            )
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
        wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
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
        name_lab = AdaptiveWrapLabel(name)
        name_lab.setObjectName("sdProductName")
        text.addWidget(name_lab)
        article = str(row.get("article") or "—")
        brand = str(row.get("brand") or "").strip()
        sub = "Арт. {}".format(article)
        if brand:
            sub = "{} · {}".format(brand, sub)
        sub_lab = AdaptiveWrapLabel(sub)
        sub_lab.setObjectName("sdProductSub")
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
        lay.setSpacing(12)

        title = QLabel("Печать стикеров по категориям")
        title.setObjectName("dialogTitle")
        lay.addWidget(title)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        btn_all = QPushButton("Выделить все")
        btn_all.setObjectName("secondary")
        btn_clear = QPushButton("Очистить все")
        btn_clear.setObjectName("secondary")
        selected_lab = QLabel("Выбрано: 0 категорий, Заказов: 0 шт.")
        selected_lab.setStyleSheet("color:#64748b;")
        toolbar.addWidget(btn_all)
        toolbar.addWidget(btn_clear)
        toolbar.addStretch(1)
        toolbar.addWidget(selected_lab)
        lay.addLayout(toolbar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(0)
        wrap = QWidget()
        rows_lay = QVBoxLayout(wrap)
        rows_lay.setContentsMargins(0, 0, 8, 0)
        rows_lay.setSpacing(6)

        row_widgets = []  # type: List[Dict[str, Any]]

        def _cat_word(n: int) -> str:
            abs_n = abs(int(n)) % 100
            last = abs_n % 10
            if 10 < abs_n < 20:
                return "категорий"
            if last == 1:
                return "категория"
            if 2 <= last <= 4:
                return "категории"
            return "категорий"

        def _sync_ui() -> None:
            selected_count = 0
            orders_total = 0
            for row in row_widgets:
                checked = bool(row["cb"].isChecked())
                # Keep layout stable like web (visibility:hidden, not display:none).
                row["fill"].setEnabled(checked)
                row["fill"].setStyleSheet(
                    row["fill_style_on"] if checked else row["fill_style_off"]
                )
                row["frame"].setProperty("checkedRow", "true" if checked else "false")
                row["frame"].style().unpolish(row["frame"])
                row["frame"].style().polish(row["frame"])
                if checked:
                    selected_count += 1
                    orders_total += int(row["qty"] or 0)
            selected_lab.setText(
                "Выбрано: {} {}, Заказов: {} шт.".format(
                    selected_count, _cat_word(selected_count), orders_total
                )
            )
            print_btn.setEnabled(selected_count > 0)

        def _fill_down(start_idx: int) -> None:
            for i in range(start_idx, len(row_widgets)):
                row_widgets[i]["cb"].blockSignals(True)
                row_widgets[i]["cb"].setChecked(True)
                row_widgets[i]["cb"].blockSignals(False)
            _sync_ui()

        for idx, g in enumerate(groups):
            frame = QFrame()
            frame.setObjectName("stickersCatRow")
            frame.setStyleSheet(
                """
                QFrame#stickersCatRow {
                    background: #ffffff;
                    border: 1px solid #e2e8f0;
                    border-radius: 8px;
                }
                QFrame#stickersCatRow[checkedRow="true"] {
                    background: #f8fafc;
                    border-color: #cbd5e1;
                }
                """
            )
            row = QHBoxLayout(frame)
            row.setContentsMargins(12, 8, 8, 8)
            row.setSpacing(10)

            cb = QCheckBox()
            name = str(g.get("product_name") or "—")
            qty = int(g.get("qty") or 0)
            label = QLabel("{} — {} шт.".format(name, qty))
            label.setWordWrap(True)
            label.setStyleSheet("border:none;background:transparent;")

            fill = QToolButton()
            fill.setObjectName("stickersCatFill")
            fill.setText("▼")
            fill.setToolTip("Выделить все ниже")
            fill.setFixedSize(36, 36)
            fill_style_on = (
                "QToolButton#stickersCatFill {"
                "border: 1px solid #cbd5e1; border-radius: 8px;"
                "background: #fff; color: #0f172a; font-size: 12px; }"
                "QToolButton#stickersCatFill:hover { background: #f1f5f9; }"
            )
            fill_style_off = (
                "QToolButton#stickersCatFill {"
                "border: 1px solid transparent; border-radius: 8px;"
                "background: transparent; color: transparent; font-size: 12px; }"
            )
            fill.setStyleSheet(fill_style_off)
            fill.setEnabled(False)
            fill.clicked.connect(partial(_fill_down, idx))

            cb.stateChanged.connect(lambda _state: _sync_ui())

            row.addWidget(cb, 0, Qt.AlignVCenter)
            row.addWidget(label, 1)
            row.addWidget(fill, 0, Qt.AlignVCenter)
            rows_lay.addWidget(frame)
            row_widgets.append(
                {
                    "frame": frame,
                    "cb": cb,
                    "fill": fill,
                    "fill_style_on": fill_style_on,
                    "fill_style_off": fill_style_off,
                    "qty": qty,
                    "order_ids": list(g.get("order_ids") or []),
                    "group_key": str(g.get("group_key") or ""),
                }
            )

        rows_lay.addStretch(1)
        scroll.setWidget(wrap)
        lay.addWidget(scroll, 1)

        def _select_all() -> None:
            for row in row_widgets:
                row["cb"].blockSignals(True)
                row["cb"].setChecked(True)
                row["cb"].blockSignals(False)
            _sync_ui()

        def _clear_all() -> None:
            for row in row_widgets:
                row["cb"].blockSignals(True)
                row["cb"].setChecked(False)
                row["cb"].blockSignals(False)
            _sync_ui()

        btn_all.clicked.connect(_select_all)
        btn_clear.clicked.connect(_clear_all)

        def _print_selected() -> None:
            ids = []  # type: List[int]
            for row in row_widgets:
                if row["cb"].isChecked():
                    ids.extend(int(x) for x in (row["order_ids"] or []))
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
        print_btn = QPushButton("Печать")
        print_btn.setObjectName("bottomPrimary")
        print_btn.setEnabled(False)
        print_btn.clicked.connect(_print_selected)
        btns.addButton(print_btn, QDialogButtonBox.ActionRole)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        _sync_ui()
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
        if self._kiz_status_refreshing:
            return
        self._kiz_status_gen += 1
        gen = self._kiz_status_gen
        old = self._kiz_status_worker
        if old is not None:
            self._disconnect_worker(old, "ready", "failed")
            self._kiz_status_worker = None
        self._set_kiz_refresh_busy(True)
        worker = _KizStatusWorker(
            self.kiz, self.source_id, self.supply_id, self.api_key, self
        )
        self._kiz_status_worker = worker

        def _on_ready(payload: object, g: int = gen) -> None:
            if g != self._kiz_status_gen:
                return
            self._kiz_status_worker = None
            self._apply_kiz_status_payload(payload if isinstance(payload, dict) else {})
            self._set_kiz_refresh_busy(False)

        def _on_failed(message: str, g: int = gen) -> None:
            if g != self._kiz_status_gen:
                return
            self._kiz_status_worker = None
            self._set_kiz_refresh_busy(False)
            QMessageBox.critical(self, "КИЗ", str(message or "Ошибка проверки КИЗ"))

        worker.ready.connect(_on_ready)
        worker.failed.connect(_on_failed)
        worker.finished.connect(lambda w=worker: self._disconnect_worker(w))
        worker.start()

    def _apply_kiz_status_payload(self, payload: Dict[str, Any]) -> None:
        """Merge live check into table rows + tone (web refresh merge)."""
        orders = payload.get("orders") if isinstance(payload, dict) else None
        if not isinstance(orders, list):
            orders = []
        by_oid = {
            int(r["order_id"]): r
            for r in orders
            if isinstance(r, dict) and r.get("order_id") is not None
        }
        live_set = set(by_oid.keys())
        for r in self._all_rows:
            oid = int(r.get("order_id") or 0)
            src = by_oid.get(oid)
            if not src:
                if live_set:
                    r["kiz_required"] = False
                    r["kiz_bound"] = False
                    r["kiz_codes"] = []
                    r["kiz_decision"] = ""
                    r["kiz_status"] = "empty"
                continue
            r["kiz_required"] = bool(src.get("kiz_required"))
            r["kiz_bound"] = bool(src.get("kiz_bound"))
            r["kiz_codes"] = list(src.get("kiz_codes") or [])
            r["kiz_decision"] = str(src.get("kiz_decision") or "")
            r["kiz_status"] = str(src.get("kiz_status") or "empty")
            if "kiz_wb_synced" in src:
                r["kiz_wb_synced"] = bool(src.get("kiz_wb_synced"))
            if src.get("cancelled") or src.get("cancel_reason_label"):
                r["cancel_reason_label"] = str(
                    src.get("cancel_reason_label")
                    or r.get("cancel_reason_label")
                    or "Отменен"
                ).strip() or "Отменен"
        marking_rows = [
            r for r in orders if isinstance(r, dict) and r.get("kiz_required")
        ]
        # Always pass status rows (required + others) so sync can patch existing
        # kiz_rows; never replace product-bearing rows with status-only dicts.
        self._sync_kiz_session(list(by_oid.values()) if by_oid else marking_rows)
        self._render_table()
        # Tone after render — same order as web (render must not wipe color).
        self._set_kiz_split_tone(str(payload.get("status") or ""))
        filled, total = self._kiz_scan_counts()
        self._last_status_note = (
            "Статусы КИЗ обновлены, просканировано {} из {}".format(filled, total)
        )
        self.meta.setText(self._last_status_note)
        self.meta.show()

    def _kiz_scan_counts(self) -> Tuple[int, int]:
        """Same N/M as «Товары с маркировкой» counter (КИЗ slots filled / total)."""
        session = supply_session.get_session(self.source_id, self.supply_id)
        rows = list(session.kiz_rows) if session else []
        filled = 0
        total = 0
        for r in rows:
            codes = list(r.get("kiz_codes") or [""]) or [""]
            total += len(codes)
            filled += sum(1 for c in codes if str(c or "").strip())
        return filled, total

    def refresh_pick_status(self) -> None:
        if not self._require_actions_ready():
            return
        if self._pick_status_refreshing:
            return
        self._pick_status_gen += 1
        gen = self._pick_status_gen
        old = self._pick_status_worker
        if old is not None:
            self._disconnect_worker(old, "ready", "failed")
            self._pick_status_worker = None
        self._set_pick_refresh_busy(True)
        worker = _PickStatusWorker(
            self.pick, self.source_id, self.supply_id, self.api_key, self
        )
        self._pick_status_worker = worker

        def _on_ready(payload: object, g: int = gen) -> None:
            if g != self._pick_status_gen:
                return
            self._pick_status_worker = None
            data = payload if isinstance(payload, dict) else {}
            self._set_pick_split_tone(str(data.get("status") or ""))
            self._set_pick_refresh_busy(False)
            orders = [
                o
                for o in (data.get("orders") or [])
                if isinstance(o, dict) and not o.get("cancelled")
            ]
            filled = sum(
                1
                for o in orders
                if o.get("barcode_ok")
                or (
                    bool(o.get("pick_verified"))
                    and bool(str(o.get("pick_barcode") or "").strip())
                )
            )
            total = len(orders)
            self._last_status_note = (
                "Статусы ШК обновлены, просканировано {} из {}".format(
                    filled, total
                )
            )
            self.meta.setText(self._last_status_note)
            self.meta.show()

        def _on_failed(message: str, g: int = gen) -> None:
            if g != self._pick_status_gen:
                return
            self._pick_status_worker = None
            self._set_pick_refresh_busy(False)
            self._set_pick_split_tone("")
            QMessageBox.critical(
                self, "Проверка ШК", str(message or "Ошибка проверки ШК")
            )

        worker.ready.connect(_on_ready)
        worker.failed.connect(_on_failed)
        worker.finished.connect(lambda w=worker: self._disconnect_worker(w))
        worker.start()


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
        self.table.setObjectName("trbxTable")
        self.table.setAlternatingRowColors(False)
        self.table.setHorizontalHeaderLabels(["Грузоместо", "Действия"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.setColumnWidth(1, 120)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(52)
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
            self.table.setCellWidget(i, 0, self._build_box_id_cell(bid))
            self.table.setCellWidget(i, 1, self._build_box_actions(bid))
            self.table.setRowHeight(i, 52)
        has_boxes = bool(self.boxes)
        self.print_all_btn.setEnabled(has_boxes)
        self.delete_all_btn.setEnabled(has_boxes and not self.supply_done)

    @staticmethod
    def _build_box_id_cell(box_id: str) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(16, 8, 12, 8)
        lay.setSpacing(0)
        lab = QLabel(box_id or "—")
        lab.setObjectName("trbxBoxId")
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lab.setWordWrap(True)
        lab.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lay.addWidget(lab, 1)
        return wrap

    def _build_box_actions(self, box_id: str) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(8, 6, 16, 6)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        print_btn = QToolButton()
        print_btn.setObjectName("trbxBoxBtn")
        print_btn.setText("QR")
        print_btn.setToolTip("Печать QR грузоместа {}".format(box_id))
        print_btn.setFixedSize(40, 40)
        print_btn.clicked.connect(partial(self.print_one_box, box_id))
        lay.addWidget(print_btn)
        if not self.supply_done:
            delete_btn = QToolButton()
            delete_btn.setObjectName("trbxBoxDeleteBtn")
            delete_btn.setText("✕")
            delete_btn.setToolTip("Удалить грузоместо {}".format(box_id))
            delete_btn.setFixedSize(40, 40)
            delete_btn.clicked.connect(partial(self.delete_one_box, box_id))
            lay.addWidget(delete_btn)
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
            show_png_list(
                pngs,
                "QR грузоместа {}".format(box_id),
                self,
                sheet_labels=["Грузоместо {}".format(box_id)],
            )
        except Exception as exc:
            QMessageBox.critical(self, "Грузоместа", str(exc))

    def print_stickers(self) -> None:
        ids = [self._box_id(b) for b in self.boxes if self._box_id(b)]
        if not ids:
            QMessageBox.information(self, "Грузоместа", "Нет грузомест")
            return
        try:
            pngs = self.trbx.stickers_png(self.api_key, self.supply_id, ids)
            labels = [
                "Грузоместо {}".format(ids[i] if i < len(ids) else i + 1)
                for i in range(len(pngs))
            ]
            show_png_list(
                pngs,
                "QR-коды грузовых мест",
                self,
                sheet_labels=labels,
            )
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
