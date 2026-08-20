# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QHeaderView,
    QButtonGroup,
    QSizePolicy,
)

from app.db import Database
from app.services import SourceService
from app.services.orders import OrdersService
from app.ui.format_helpers import (
    ago_label,
    format_date_short,
    make_badge,
    make_photo_label,
    make_status_pill,
)
from app.ui.layout_utils import FlowLayout, FbsTabButton, fit_tab_button
from app.wb.sync import sync_source


class SyncWorker(QThread):
    progress = pyqtSignal(str, int)
    finished_ok = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(
        self,
        db: Database,
        sources: List[Dict[str, Any]],
        lookback_days: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(SyncWorker, self).__init__(parent)
        self.db = db
        self.sources = list(sources or [])
        self.lookback_days = lookback_days
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        from app.services.pallet import compute_pallet_summary

        try:
            total_orders = 0
            total_supplies = 0
            all_errors = []  # type: List[str]
            stopped = False
            scope_error = False
            scope_message = ""
            for src in self.sources:
                if self._stop:
                    stopped = True
                    break
                sid = int(src["id"])
                name = str(src.get("name") or sid)
                self.progress.emit("{}…".format(name), total_orders)

                def _prog(msg, n, _name=name):
                    self.progress.emit("{} · {}".format(_name, msg), n)

                result = sync_source(
                    self.db,
                    sid,
                    str(src.get("api_key") or ""),
                    lookback_days=self.lookback_days,
                    stop_requested=lambda: self._stop,
                    progress=_prog,
                )
                if result.get("scope_error"):
                    scope_error = True
                    scope_message = str(result.get("message") or "")
                    all_errors.append("{}: {}".format(name, scope_message))
                    continue
                total_orders += int(result.get("orders") or 0)
                total_supplies += int(result.get("supplies") or 0)
                for e in result.get("errors") or []:
                    all_errors.append("{}: {}".format(name, e))
                if result.get("stopped"):
                    stopped = True
                    break
            pallet_summary = []
            try:
                pallet_summary = compute_pallet_summary(self.db, self.sources)
            except Exception:
                pallet_summary = []
            self.finished_ok.emit(
                {
                    "orders": total_orders,
                    "supplies": total_supplies,
                    "errors": all_errors,
                    "stopped": stopped,
                    "scope_error": scope_error,
                    "message": scope_message,
                    "pallet_summary": pallet_summary,
                    "synced_sources": len(self.sources),
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class FbsPage(QWidget):
    """Layout mirrors web `#section-supplies-wb-fbs` (title → toolbar → tabs → table → bottom bar)."""

    def __init__(
        self, db: Database, sources: SourceService, orders: OrdersService
    ) -> None:
        super(FbsPage, self).__init__()
        self.db = db
        self.sources = sources
        self.orders = orders
        self._tab = "new"
        self._page = 0
        self._page_size = 50
        self._worker = None  # type: Optional[SyncWorker]
        self._selected_order_ids = set()  # type: set
        self._select_all_matching = False
        self._last_total = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)

        # Title row: section title + source select (web .wb-fbs-title-row)
        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        title = QLabel("Поставки — ВБ ФБС")
        title.setObjectName("sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.source_combo = QComboBox()
        self.source_combo.setObjectName("sourceCombo")
        self.source_combo.setMinimumWidth(180)
        self.source_combo.setMaximumWidth(280)
        self.source_combo.setToolTip("Источник")
        self.source_combo.currentIndexChanged.connect(self.on_source_change)
        title_row.addWidget(self.source_combo)
        root.addLayout(title_row)

        # Toolbar panel (web .wb-fbs-toolbar)
        toolbar_panel = QFrame()
        toolbar_panel.setObjectName("toolbarPanel")
        tb = QVBoxLayout(toolbar_panel)
        tb.setContentsMargins(16, 16, 16, 16)
        tb.setSpacing(12)

        top_controls = QHBoxLayout()
        top_controls.setSpacing(8)
        self.sync_btn = QPushButton("Синхронизировать")
        self.sync_btn.clicked.connect(self.start_sync)
        self.stop_btn = QPushButton("🛑")
        self.stop_btn.setObjectName("iconBtn")
        self.stop_btn.setToolTip("Остановить синхронизацию")
        self.stop_btn.clicked.connect(self.stop_sync)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setVisible(False)
        top_controls.addWidget(self.sync_btn)
        top_controls.addWidget(self.stop_btn)
        top_controls.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по заказу, артикулу, ШК…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(260)
        self.search.setMaximumWidth(360)
        self.search.setToolTip(
            "Номер заказа: если нет во вкладках, сервис запросит его в WB API"
        )
        self.search.returnPressed.connect(self.run_search)
        self.search.textChanged.connect(self._on_search_debounce)
        top_controls.addWidget(self.search)
        tb.addLayout(top_controls)

        # Sync info (web .wb-fbs-sync-info) — text + pallets + close
        self.sync_info_frame = QFrame()
        self.sync_info_frame.setObjectName("syncInfo")
        self.sync_info_frame.setProperty("state", "")
        sif = QHBoxLayout(self.sync_info_frame)
        sif.setContentsMargins(16, 12, 12, 12)
        sif.setSpacing(12)
        sync_main = QVBoxLayout()
        sync_main.setSpacing(8)
        self.sync_info = QLabel("")
        self.sync_info.setObjectName("syncInfoText")
        self.sync_info.setWordWrap(True)
        self.pallet_info = QLabel("")
        self.pallet_info.setObjectName("syncPallets")
        self.pallet_info.setWordWrap(True)
        self.pallet_info.hide()
        sync_main.addWidget(self.sync_info)
        sync_main.addWidget(self.pallet_info)
        sif.addLayout(sync_main, 1)
        self.sync_info_close = QPushButton("✕")
        self.sync_info_close.setObjectName("iconBtn")
        self.sync_info_close.setToolTip("Скрыть")
        self.sync_info_close.clicked.connect(self._close_sync_info)
        sif.addWidget(self.sync_info_close, 0, Qt.AlignTop)
        self.sync_info_frame.hide()
        tb.addWidget(self.sync_info_frame)
        root.addWidget(toolbar_panel)

        # Tabs row + collect MGT (web .wb-fbs-tabs-row)
        tabs_frame = QFrame()
        tabs_frame.setObjectName("tabsRow")
        tabs_row = QHBoxLayout(tabs_frame)
        tabs_row.setContentsMargins(0, 0, 4, 0)
        tabs_row.setSpacing(12)

        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self.tab_btns = {}  # type: Dict[str, FbsTabButton]
        for key, label in (
            ("new", "Новые"),
            ("assembly", "На сборке"),
            ("delivery", "В доставке"),
        ):
            btn = FbsTabButton(label)
            self._tab_group.addButton(btn)
            self.tab_btns[key] = btn
            tabs_row.addWidget(btn)
            btn.clicked.connect(lambda _=False, k=key: self._set_tab(k))
            fit_tab_button(btn, h_pad=64)
        self.tab_btns["new"].setChecked(True)
        tabs_row.addStretch(1)

        self.collect_mgt_btn = QPushButton("Собрать все МГТ-заказы")
        self.collect_mgt_btn.setObjectName("mgtBtn")
        self.collect_mgt_btn.setCursor(Qt.PointingHandCursor)
        self.collect_mgt_btn.setToolTip("Собрать все МГТ-заказы текущего кабинета")
        self.collect_mgt_btn.clicked.connect(self.collect_mgt)
        tabs_row.addWidget(self.collect_mgt_btn)
        root.addWidget(tabs_frame)

        # Table
        self.table = QTableWidget(0, 0)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table_header = self.table.horizontalHeader()
        table_header.setStretchLastSection(False)
        table_header.setSectionResizeMode(QHeaderView.Interactive)
        table_header.setMinimumSectionSize(32)
        table_header.sectionResized.connect(self._on_column_resized)
        self._col_widths_guard = False
        self._col_width_save_timer = QTimer(self)
        self._col_width_save_timer.setSingleShot(True)
        self._col_width_save_timer.setInterval(400)
        self._col_width_save_timer.timeout.connect(self._persist_table_col_widths)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setShowGrid(False)
        self.table.doubleClicked.connect(self.on_row_double_click)
        root.addWidget(self.table, 1)

        # Bottom selection bar — wrapping actions so buttons stay readable
        self.bottom_bar = QFrame()
        self.bottom_bar.setObjectName("bottomBar")
        bottom_outer = QVBoxLayout(self.bottom_bar)
        bottom_outer.setContentsMargins(16, 12, 16, 12)
        bottom_outer.setSpacing(8)

        top_sel = QHBoxLayout()
        top_sel.setSpacing(12)
        self.sel_label = QLabel("Выбрано 0 заказов")
        self.sel_label.setObjectName("selectedLabel")
        top_sel.addWidget(self.sel_label)
        self.btn_select_page = QPushButton("Выбрать все на странице")
        self.btn_select_page.setObjectName("linkBtn")
        self.btn_select_page.clicked.connect(self.select_page)
        self.btn_select_page.hide()
        top_sel.addWidget(self.btn_select_page)
        self.btn_select_all_matching = QPushButton("Выбрать все подходящие")
        self.btn_select_all_matching.setObjectName("linkBtn")
        self.btn_select_all_matching.clicked.connect(self.select_all_matching)
        self.btn_select_all_matching.hide()
        top_sel.addWidget(self.btn_select_all_matching)
        top_sel.addStretch(1)
        self.btn_clear_sel = QPushButton("✕")
        self.btn_clear_sel.setObjectName("iconBtn")
        self.btn_clear_sel.setToolTip("Сбросить выбор")
        self.btn_clear_sel.clicked.connect(self._clear_selection)
        top_sel.addWidget(self.btn_clear_sel)
        bottom_outer.addLayout(top_sel)

        bottom = FlowLayout(h_spacing=8, v_spacing=8)
        self.btn_new_supply = QPushButton("+  Новая поставка")
        self.btn_new_supply.setObjectName("bottomPrimary")
        self.btn_new_supply.clicked.connect(self.create_supply)
        self.btn_add_supply = QPushButton("Добавить к существующей")
        self.btn_add_supply.setObjectName("secondary")
        self.btn_add_supply.clicked.connect(self.add_to_supply)
        self.btn_open_supply = QPushButton("Открыть поставку")
        self.btn_open_supply.setObjectName("secondary")
        self.btn_open_supply.clicked.connect(self.open_selected_supply)
        self.btn_print_stickers = QPushButton("Стикеры товаров")
        self.btn_print_stickers.setObjectName("secondary")
        self.btn_print_stickers.clicked.connect(self.print_stickers)
        self.btn_supply_sticker = QPushButton("Стикер поставки")
        self.btn_supply_sticker.setObjectName("secondary")
        self.btn_supply_sticker.clicked.connect(self.print_supply_sticker)
        self.btn_box_stickers = QPushButton("Стикеры коробов")
        self.btn_box_stickers.setObjectName("secondary")
        self.btn_box_stickers.clicked.connect(self.print_box_stickers)
        self.btn_supply_qr = QPushButton("Напечатать QR-код поставки")
        self.btn_supply_qr.setObjectName("secondary")
        self.btn_supply_qr.clicked.connect(self.print_supply_qr)

        bottom.addWidget(self.btn_new_supply)
        bottom.addWidget(self.btn_add_supply)
        bottom.addWidget(self.btn_open_supply)
        bottom.addWidget(self.btn_print_stickers)
        bottom.addWidget(self.btn_supply_sticker)
        bottom.addWidget(self.btn_box_stickers)
        bottom.addWidget(self.btn_supply_qr)
        bottom_outer.addLayout(bottom)
        root.addWidget(self.bottom_bar)

        # Pagination — portal-like: total left, nav + page size right
        pager_frame = QFrame()
        pager_frame.setObjectName("pagerBar")
        pager = QHBoxLayout(pager_frame)
        pager.setContentsMargins(0, 4, 0, 0)
        pager.setSpacing(8)
        self.pager_total = QLabel("Заказов: 0")
        self.pager_total.setObjectName("pagerTotal")
        pager.addWidget(self.pager_total)
        pager.addStretch(1)
        self.prev_btn = QPushButton("← Назад")
        self.prev_btn.setObjectName("pagerBtn")
        self.prev_btn.setCursor(Qt.PointingHandCursor)
        self.prev_btn.clicked.connect(self.prev_page)
        self.next_btn = QPushButton("Вперёд →")
        self.next_btn.setObjectName("pagerBtn")
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.clicked.connect(self.next_page)
        self.page_label = QLabel("1 / 1")
        self.page_label.setObjectName("pageMeta")
        self.page_label.setAlignment(Qt.AlignCenter)
        pager.addWidget(self.prev_btn)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_btn)
        self.page_size = QComboBox()
        self.page_size.setObjectName("pageSizeCombo")
        for n in (30, 50, 100):
            self.page_size.addItem("{} / стр.".format(n), n)
        self.page_size.setCurrentIndex(1)  # 50
        self.page_size.currentIndexChanged.connect(self.reload_table)
        pager.addWidget(self.page_size)
        root.addWidget(pager_frame)

        # Web parity: live search waits 400ms after the last keystroke before
        # re-querying (Enter still searches immediately via returnPressed).
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(400)
        self._search_timer.timeout.connect(self.run_search)

        self.table.itemSelectionChanged.connect(self.update_bottom_visibility)

        self.reload_sources()

    def _set_tab(self, key: str) -> None:
        if key == self._tab and self.tab_btns[key].isChecked():
            self.on_tab_change(key)
            return
        self.on_tab_change(key)

    def _close_sync_info(self) -> None:
        self.sync_info_frame.hide()
        self.pallet_info.hide()

    def _set_sync_state(self, state: str) -> None:
        self.sync_info_frame.setProperty("state", state or "")
        self.sync_info_frame.style().unpolish(self.sync_info_frame)
        self.sync_info_frame.style().polish(self.sync_info_frame)

    def _show_sync_info(self, text: str, state: str = "") -> None:
        self.sync_info.setText(text)
        self._set_sync_state(state)
        self.sync_info_frame.show()

    def _clear_selection(self) -> None:
        self._selected_order_ids.clear()
        self._select_all_matching = False
        self.reload_table()

    def current_source(self) -> Optional[Dict[str, Any]]:
        idx = self.source_combo.currentIndex()
        if idx < 0:
            return None
        data = self.source_combo.itemData(idx)
        return data if isinstance(data, dict) else None

    def reload_sources(self) -> None:
        current_id = None
        cur = self.current_source()
        if cur:
            current_id = int(cur["id"])
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for s in self.sources.list_fbs_enabled():
            self.source_combo.addItem(str(s.get("name") or ""), s)
        self.source_combo.blockSignals(False)
        if self.source_combo.count() == 0:
            self._show_sync_info(
                "Нет источников FBS. Откройте Настройки → Источники "
                "и добавьте кабинет с «ФБС» в названии и ключом Marketplace.",
                "error",
            )
            self.pallet_info.hide()
            self.table.setRowCount(0)
            return
        if current_id is not None:
            for i in range(self.source_combo.count()):
                d = self.source_combo.itemData(i)
                if isinstance(d, dict) and int(d["id"]) == current_id:
                    self.source_combo.setCurrentIndex(i)
                    break
        self.reload_table()

    def on_source_change(self) -> None:
        self._page = 0
        self.reload_table()

    def on_tab_change(self, key: str) -> None:
        self._tab = key
        for k, btn in self.tab_btns.items():
            btn.setChecked(k == key)
        self._page = 0
        self._selected_order_ids.clear()
        self._select_all_matching = False
        self.reload_table()

    def _on_search_debounce(self, _text: str) -> None:
        """Web parity: debounce 400ms, then rerun the same search as Enter."""
        self._select_all_matching = False
        self._search_timer.start()

    def update_bottom_visibility(self) -> None:
        is_new = self._tab == "new"
        is_asm = self._tab == "assembly"
        is_del = self._tab == "delivery"
        sid = self._selected_supply_id() if (is_asm or is_del) else None
        has_new_sel = bool(self._selected_order_ids) or self._select_all_matching

        # Web: MGT button stays available on all operational tabs when mgt_new > 0.
        # Visibility is driven by reload_table via counts; keep enabled here.
        self.btn_select_page.setVisible(is_new)
        self.btn_select_all_matching.setVisible(is_new)
        self.btn_clear_sel.setVisible(is_new and bool(self._selected_order_ids))

        self.btn_new_supply.setVisible(is_new)
        self.btn_add_supply.setVisible(is_new)
        self.btn_open_supply.setVisible(bool(sid) and is_asm)
        self.btn_print_stickers.setVisible(
            (is_new and has_new_sel) or (is_asm and bool(sid))
        )
        self.btn_supply_sticker.setVisible(is_asm and bool(sid))
        self.btn_box_stickers.setVisible(is_asm and bool(sid))
        self.btn_supply_qr.setVisible(is_del and bool(sid))

        # Web parity: bottom bar hides entirely with no selection on the
        # "new" tab; on assembly/delivery it hides until a supply row is
        # picked (pagination below stays visible regardless).
        # Delivery: only QR of the supply — no open / order stickers.
        if is_new:
            visible = has_new_sel
        elif is_asm or is_del:
            visible = bool(sid)
        else:
            visible = False
        self.bottom_bar.setVisible(visible)

        if is_new:
            self.btn_new_supply.setEnabled(has_new_sel)
            self.btn_add_supply.setEnabled(has_new_sel)
        else:
            self.btn_new_supply.setEnabled(True)
            self.btn_add_supply.setEnabled(True)

        if (is_asm or is_del) and sid:
            self.sel_label.setText("Поставка {}".format(sid))

    def _update_tab_labels(self, counts: Dict[str, int]) -> None:
        mapping = (
            ("new", counts.get("new", 0)),
            ("assembly", counts.get("assembly", 0)),
            ("delivery", counts.get("delivery", 0)),
        )
        for key, n in mapping:
            btn = self.tab_btns[key]
            btn.set_count(int(n))
            btn.setChecked(key == self._tab)

        mgt_new = int(counts.get("mgt_new") or 0)
        show_mgt = mgt_new > 0 and self._tab in ("new", "assembly", "delivery")
        self.collect_mgt_btn.setVisible(show_mgt)

    def reload_table(self) -> None:
        self.update_bottom_visibility()
        src = self.current_source()
        if not src:
            return
        sid = int(src["id"])
        counts = self.orders.tab_counts(sid)
        self._update_tab_labels(counts)

        if self._tab == "new":
            self.search.setPlaceholderText("Поиск по заказу, артикулу, ШК…")
        elif self._tab == "assembly":
            self.search.setPlaceholderText("Поиск по поставке, заказу, складу…")
        else:
            self.search.setPlaceholderText("Поиск по поставке…")

        limit = int(self.page_size.currentData() or 50)
        offset = self._page * limit
        search = self.search.text().strip()

        if self._tab == "new":
            rows, total = self.orders.list_orders(
                sid, tab="new", search=search, limit=limit, offset=offset
            )
            self._last_total = total
            self._fill_orders_table(rows)
            page_count = len(rows)
            can_all = (
                page_count > 0
                and total > page_count
                and not self._select_all_matching
            )
            self.btn_select_all_matching.setEnabled(
                can_all or self._select_all_matching
            )
            if self._select_all_matching:
                self.btn_select_all_matching.setText(
                    "Выбраны все {} · сбросить".format(total)
                )
            else:
                self.btn_select_all_matching.setText(
                    "Выбрать все подходящие ({})".format(total)
                )
            n_sel = len(self._selected_order_ids)
            self.sel_label.setText(
                "Выбрано {} {}".format(
                    n_sel, "заказ" if n_sel == 1 else "заказов"
                )
            )
            self.pager_total.setText("Заказов: {}".format(total))
        elif self._tab == "assembly":
            rows, total = self.orders.list_supplies(
                sid, done=False, search=search, limit=limit, offset=offset
            )
            self._last_total = total
            self._fill_supplies_table(rows)
            self.sel_label.setText("Поставки на сборке")
            self.pager_total.setText("Поставок: {}".format(total))
        else:
            # Delivery: supply rows only — no order decrypt / detail open.
            rows, total = self.orders.list_supplies(
                sid,
                done=True,
                search=search,
                limit=limit,
                offset=offset,
                include_order_warehouse=False,
            )
            self._last_total = total
            self._fill_supplies_table(rows)
            self.sel_label.setText("Поставки в доставке")
            self.pager_total.setText("Поставок: {}".format(total))

        pages = max(1, (total + limit - 1) // limit)
        if self._page >= pages:
            self._page = pages - 1
        self.page_label.setText("{} / {}".format(self._page + 1, pages))
        self.prev_btn.setEnabled(self._page > 0)
        self.next_btn.setEnabled(self._page + 1 < pages)
        self.update_bottom_visibility()

    def _table_layout(self) -> str:
        if self._tab == "new":
            return "new"
        if self._tab == "assembly":
            return "supplies_assembly"
        return "supplies_delivery"

    @staticmethod
    def _col_settings_key(layout: str) -> str:
        return "fbs_table_cols_{}".format(layout)

    @staticmethod
    def _default_col_widths(layout: str, count: int) -> List[int]:
        defaults = {
            "new": [40, 180, 420, 200],
            "supplies_assembly": [40, 280, 180, 140, 160, 160],
            "supplies_delivery": [40, 220, 160, 140, 160, 140, 160, 48],
            # Legacy key kept for older installs that still have saved widths.
            "supplies": [168, 280, 72, 72, 88, 160, 48, 56],
        }
        base = list(defaults.get(layout, []))
        while len(base) < count:
            base.append(100)
        return base[:count]

    def _load_col_widths(self, layout: str, count: int) -> List[int]:
        raw = self.db.get_setting(self._col_settings_key(layout), "")
        if raw:
            try:
                widths = json.loads(raw)
                if isinstance(widths, list) and len(widths) == count:
                    return [max(32, int(w)) for w in widths]
            except (TypeError, ValueError):
                pass
        return self._default_col_widths(layout, count)

    def _save_col_widths(self, layout: str) -> None:
        hdr = self.table.horizontalHeader()
        widths = [hdr.sectionSize(i) for i in range(hdr.count())]
        self.db.set_setting(
            self._col_settings_key(layout), json.dumps(widths, separators=(",", ":"))
        )

    def _apply_table_col_widths(self, layout: str) -> None:
        hdr = self.table.horizontalHeader()
        count = hdr.count()
        if count <= 0:
            return
        widths = self._load_col_widths(layout, count)
        self._col_widths_guard = True
        try:
            hdr.setStretchLastSection(False)
            for i in range(count):
                hdr.setSectionResizeMode(i, QHeaderView.Interactive)
                self.table.setColumnWidth(i, widths[i])
        finally:
            self._col_widths_guard = False

    def _on_column_resized(
        self, _logical_index: int, _old_size: int, _new_size: int
    ) -> None:
        if self._col_widths_guard:
            return
        self._col_width_save_timer.start()

    def _persist_table_col_widths(self) -> None:
        if self._col_widths_guard or self.table.columnCount() <= 0:
            return
        self._save_col_widths(self._table_layout())

    @staticmethod
    def _bold_label(text: str, wrap: bool = False) -> QLabel:
        lab = QLabel(text)
        if wrap:
            lab.setWordWrap(True)
        f = lab.font()
        f.setBold(True)
        lab.setFont(f)
        return lab

    @staticmethod
    def _table_text_item(
        text: str, *, bold: bool = False, tooltip: str = ""
    ) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        if bold:
            f = item.font()
            f.setBold(True)
            item.setFont(f)
        tip = tooltip or text
        if tip:
            item.setToolTip(tip)
        return item

    def _fill_orders_table(self, rows: List[Dict[str, Any]]) -> None:
        """Web «Новые»: checkbox | Заказ | Товар (photo+text) | Склад."""
        try:
            self.table.itemChanged.disconnect(self._on_check_change)
        except Exception:
            pass
        cols = ["", "Заказ", "Товар", "Склад"]
        self.table.blockSignals(True)
        self.table.clear()
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setRowCount(len(rows))
        self.table.verticalHeader().setDefaultSectionSize(120)
        for r, row in enumerate(rows):
            oid = int(row["order_id"])
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(
                Qt.Checked if oid in self._selected_order_ids else Qt.Unchecked
            )
            chk.setData(Qt.UserRole, oid)
            self.table.setItem(r, 0, chk)

            order_widget = QWidget()
            order_lay = QVBoxLayout(order_widget)
            order_lay.setContentsMargins(10, 10, 10, 10)
            order_lay.setSpacing(4)
            oid_lab = QLabel(str(oid))
            oid_lab.setObjectName("orderIdLabel")
            order_lay.addWidget(oid_lab)
            created = format_date_short(row.get("created_at_wb"))
            if created:
                meta = QLabel("от {}".format(created))
                meta.setObjectName("supplyMeta")
                order_lay.addWidget(meta)
            badges_row = QHBoxLayout()
            badges_row.setContentsMargins(0, 0, 0, 0)
            badges_row.setSpacing(4)
            ago = ago_label(row.get("created_at_wb"))
            if ago:
                badges_row.addWidget(make_badge(ago, "time"))
            cargo = row.get("cargo_label")
            if cargo:
                badges_row.addWidget(make_badge(str(cargo), "cargo"))
            if row.get("is_b2b"):
                badges_row.addWidget(make_badge("B2B", ""))
            badges_row.addStretch(1)
            order_lay.addLayout(badges_row)
            order_lay.addStretch(1)
            self.table.setCellWidget(r, 1, order_widget)

            prod_widget = QWidget()
            prod_outer = QHBoxLayout(prod_widget)
            prod_outer.setContentsMargins(10, 10, 10, 10)
            prod_outer.setSpacing(12)
            prod_outer.setAlignment(Qt.AlignTop)
            prod_outer.addWidget(make_photo_label(row.get("product_photo"), 96))
            text_col = QVBoxLayout()
            text_col.setSpacing(4)
            article = str(row.get("article") or "")
            name = str(row.get("product_name") or article or "—")
            name_lab = QLabel(name)
            name_lab.setObjectName("productName")
            name_lab.setWordWrap(True)
            text_col.addWidget(name_lab)
            sub_parts = ["Арт. {}".format(article or "—")]
            if row.get("nm_id"):
                sub_parts.append("nmId {}".format(row.get("nm_id")))
            sub = QLabel(" · ".join(sub_parts))
            sub.setObjectName("productSub")
            text_col.addWidget(sub)
            skus = row.get("skus") or []
            for s in skus[:4]:
                bc = QLabel(str(s))
                bc.setObjectName("barcodeLine")
                text_col.addWidget(bc)
            text_col.addStretch(1)
            prod_outer.addLayout(text_col, 1)
            self.table.setCellWidget(r, 2, prod_widget)

            wh_widget = QWidget()
            wh_lay = QVBoxLayout(wh_widget)
            wh_lay.setContentsMargins(10, 10, 10, 10)
            wh_lay.setSpacing(4)
            wh_name = str(row.get("warehouse_label") or "—")
            wh_lab = QLabel(wh_name)
            wh_lab.setObjectName("whName")
            wh_lab.setWordWrap(True)
            wh_lay.addWidget(wh_lab)
            if row.get("warehouse_id") is not None:
                wh_id = QLabel("ID {}".format(row.get("warehouse_id")))
                wh_id.setObjectName("supplyMeta")
                wh_lay.addWidget(wh_id)
            wh_lay.addStretch(1)
            self.table.setCellWidget(r, 3, wh_widget)

            self.table.resizeRowToContents(r)
            self.table.setRowHeight(r, max(self.table.rowHeight(r), 120))
        self.table.blockSignals(False)
        self.table.itemChanged.connect(self._on_check_change)
        self._apply_table_col_widths("new")

    def _on_check_change(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        oid = item.data(Qt.UserRole)
        if oid is None:
            return
        # Supplies table also uses col-0 checkboxes with string supply ids.
        if self._tab != "new":
            self.update_bottom_visibility()
            return
        if item.checkState() == Qt.Checked:
            self._selected_order_ids.add(int(oid))
        else:
            self._selected_order_ids.discard(int(oid))
            self._select_all_matching = False
        n_sel = len(self._selected_order_ids)
        self.sel_label.setText(
            "Выбрано {} {}".format(n_sel, "заказ" if n_sel == 1 else "заказов")
        )
        self.update_bottom_visibility()

    def _supply_row_menu(self, sid: str) -> QMenu:
        """⋮ menu: web parity for delivery (QR) + preferred assembly actions."""
        menu = QMenu(self)
        if self._tab == "delivery":
            menu.addAction(
                "Напечатать QR-код поставки", lambda s=sid: self._supply_qr_for(s)
            )
        elif self._tab == "assembly":
            menu.addAction(
                "Стикеры товаров", lambda s=sid: self._print_stickers_for(s)
            )
            menu.addAction(
                "Стикер поставки", lambda s=sid: self._supply_qr_for(s)
            )
            menu.addAction(
                "Стикеры коробов", lambda s=sid: self._box_stickers_for(s)
            )
        return menu

    @staticmethod
    def _boxes_label(n: int) -> str:
        if n == 1:
            return "1 грузоместо"
        if 2 <= n <= 4:
            return "{} грузоместа".format(n)
        return "{} грузомест".format(n)

    def _fill_supplies_table(self, rows: List[Dict[str, Any]]) -> None:
        """Portal columns for «На сборке» / «В доставке»."""
        try:
            self.table.itemChanged.disconnect(self._on_check_change)
        except Exception:
            pass
        is_assembly = self._tab == "assembly"
        if is_assembly:
            cols = [
                "",
                "Поставка",
                "QR-код поставки",
                "Заказы и грузоместа",
                "Этап сборки",
                "Склад",
            ]
        else:
            cols = [
                "",
                "Поставка",
                "QR-код поставки",
                "Статус",
                "Время сканирования QR-кода поставки",
                "Заказы и грузоместа",
                "Склад",
                "",
            ]
        layout = "supplies_assembly" if is_assembly else "supplies_delivery"
        self.table.blockSignals(True)
        self.table.clear()
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setRowCount(len(rows))
        self.table.verticalHeader().setDefaultSectionSize(88)
        for r, row in enumerate(rows):
            sid = str(row.get("supply_id") or "")
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Unchecked)
            chk.setData(Qt.UserRole, sid)
            self.table.setItem(r, 0, chk)

            name_widget = QWidget()
            name_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            name_lay = QVBoxLayout(name_widget)
            name_lay.setContentsMargins(12, 8, 12, 8)
            name_lay.setSpacing(4)
            name_lab = QLabel(str(row.get("name") or ("Поставка " + sid)))
            name_lab.setObjectName("supplyLink" if is_assembly else "whName")
            name_lab.setWordWrap(True)
            if is_assembly:
                name_lab.setCursor(Qt.PointingHandCursor)
                name_lab.mousePressEvent = (  # type: ignore[method-assign]
                    lambda _ev, s=sid: self._open_supply_by_id(s)
                )
            name_lay.addWidget(name_lab)
            created = format_date_short(row.get("created_at_wb"))
            if created:
                meta = QLabel("от {}".format(created))
                meta.setObjectName("supplyMeta")
                name_lay.addWidget(meta)
            badges_row = QHBoxLayout()
            badges_row.setContentsMargins(0, 0, 0, 0)
            badges_row.setSpacing(4)
            cargo = row.get("cargo_label")
            if cargo:
                badges_row.addWidget(make_badge(str(cargo), "cargo"))
            if row.get("pickup_allowed"):
                badges_row.addWidget(make_badge("Можно в ПВЗ", "pvz"))
            badges_row.addStretch(1)
            name_lay.addLayout(badges_row)
            self.table.setCellWidget(r, 1, name_widget)

            qr_lab = QLabel(sid or "—")
            qr_lab.setObjectName("supplyQr")
            qr_lab.setWordWrap(True)
            qr_lab.setToolTip(sid)
            qr_wrap = QWidget()
            qr_lay = QVBoxLayout(qr_wrap)
            qr_lay.setContentsMargins(12, 8, 12, 8)
            qr_lay.addWidget(qr_lab)
            qr_lay.addStretch(1)
            self.table.setCellWidget(r, 2, qr_wrap)

            orders_count = int(row.get("order_count") or 0)
            boxes_count = int(row.get("boxes_count") or 0)
            orders_widget = QWidget()
            orders_lay = QVBoxLayout(orders_widget)
            orders_lay.setContentsMargins(12, 8, 12, 8)
            orders_lay.setSpacing(2)
            oc = QLabel(str(orders_count))
            oc.setObjectName("supplyOrders")
            orders_lay.addWidget(oc)
            boxes_meta = QLabel(self._boxes_label(boxes_count))
            boxes_meta.setObjectName("supplyMeta")
            orders_lay.addWidget(boxes_meta)
            orders_lay.addStretch(1)

            status_text = str(
                row.get("status_label")
                or ("Сборка заказов" if is_assembly else "Отгрузите поставку")
            )
            status_kind = str(row.get("status_kind") or ("assembly" if is_assembly else "ship"))
            status_wrap = QWidget()
            status_lay = QVBoxLayout(status_wrap)
            status_lay.setContentsMargins(12, 8, 12, 8)
            status_lay.addWidget(make_status_pill(status_text, status_kind))
            status_lay.addStretch(1)

            wh_widget = QWidget()
            wh_lay = QVBoxLayout(wh_widget)
            wh_lay.setContentsMargins(12, 8, 12, 8)
            wh_lay.setSpacing(2)
            wh_lab = QLabel(str(row.get("warehouse_label") or "—"))
            wh_lab.setObjectName("whName")
            wh_lab.setWordWrap(True)
            wh_lay.addWidget(wh_lab)
            wh_lay.addStretch(1)

            if is_assembly:
                self.table.setCellWidget(r, 3, orders_widget)
                self.table.setCellWidget(r, 4, status_wrap)
                self.table.setCellWidget(r, 5, wh_widget)
            else:
                self.table.setCellWidget(r, 3, status_wrap)
                scan_wrap = QWidget()
                scan_lay = QVBoxLayout(scan_wrap)
                scan_lay.setContentsMargins(12, 8, 12, 8)
                scan_lab = QLabel(format_date_short(row.get("scan_dt")) or "—")
                scan_lab.setObjectName("supplyMeta")
                scan_lay.addWidget(scan_lab)
                scan_lay.addStretch(1)
                self.table.setCellWidget(r, 4, scan_wrap)
                self.table.setCellWidget(r, 5, orders_widget)
                self.table.setCellWidget(r, 6, wh_widget)

                actions_cell = QWidget()
                actions_cell.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)
                actions_lay = QHBoxLayout(actions_cell)
                actions_lay.setContentsMargins(8, 6, 8, 6)
                actions_btn = QToolButton()
                actions_btn.setObjectName("iconBtn")
                actions_btn.setText("⋮")
                actions_btn.setToolTip("Действия")
                actions_btn.setPopupMode(QToolButton.InstantPopup)
                actions_btn.setMenu(self._supply_row_menu(sid))
                actions_lay.addWidget(actions_btn)
                actions_lay.addStretch(1)
                self.table.setCellWidget(r, 7, actions_cell)

            self.table.resizeRowToContents(r)
            min_h = 88 if row.get("pickup_allowed") or row.get("cargo_label") else 76
            self.table.setRowHeight(r, max(self.table.rowHeight(r), min_h))
        self.table.blockSignals(False)
        self.table.itemChanged.connect(self._on_check_change)
        self._apply_table_col_widths(layout)

    def _open_supply_by_id(self, sid: str) -> None:
        # Delivery tab is a read-only supply list — do not open / decrypt orders.
        if self._tab == "delivery":
            return
        from app.ui.supply_detail import SupplyDetailDialog

        src = self.current_source()
        if not src or not sid:
            return
        fullscreen = True
        dlg = SupplyDetailDialog(
            self.db,
            self.orders,
            src,
            sid,
            None,
            fullscreen=fullscreen,
        )
        dlg.exec_()
        if dlg.supply_mutated:
            self.reload_table()

    def prev_page(self) -> None:
        if self._page > 0:
            self._page -= 1
            self.reload_table()

    def next_page(self) -> None:
        self._page += 1
        self.reload_table()

    def start_sync(self) -> None:
        all_sources = self.sources.list_fbs_enabled()
        if not all_sources:
            QMessageBox.warning(self, "Синхронизация", "Нет включённых источников FBS")
            return
        if self._worker and self._worker.isRunning():
            return
        lookback = 3
        self._show_sync_info(
            "Синхронизация {} источник(ов)…".format(len(all_sources)), ""
        )
        self.pallet_info.hide()
        self.sync_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.stop_btn.setVisible(True)
        self._worker = SyncWorker(self.db, all_sources, lookback, self)
        self._worker.progress.connect(self._on_sync_progress)
        self._worker.finished_ok.connect(self._on_sync_done)
        self._worker.failed.connect(self._on_sync_fail)
        self._worker.start()

    def stop_sync(self) -> None:
        if self._worker:
            self._worker.request_stop()

    def _on_sync_progress(self, msg: str, n: int) -> None:
        self._show_sync_info("{} · заказов: {}".format(msg, n), "")

    def _on_sync_done(self, result: dict) -> None:
        self.sync_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setVisible(False)
        try:
            from app.services import order_open_cache, supply_session

            source_ids = [int(src["id"]) for src in self.sources.list_fbs_enabled()]
            order_open_cache.clear_for_sources(self.db, source_ids)
            supply_session.clear_all_sessions()
        except Exception:
            pass
        for src in self.sources.list_fbs_enabled():
            try:
                self.sources.touch_synced(int(src["id"]))
            except Exception:
                pass
        err = result.get("errors") or []
        msg = "Готово: заказов {}, поставок {} · источников {}".format(
            result.get("orders", 0),
            result.get("supplies", 0),
            result.get("synced_sources", 0),
        )
        if result.get("stopped"):
            msg += " (остановлено)"
        if err:
            msg += " · ошибки: " + "; ".join(str(e) for e in err[:3])
        state = "error" if (err or result.get("scope_error")) else "ok"
        if result.get("scope_error") and result.get("message"):
            msg = str(result.get("message"))
            state = "error"
        self._show_sync_info(msg, state)
        pallets = result.get("pallet_summary") or []
        if pallets:
            lines = []
            for p in pallets:
                lines.append(
                    "{}: {}".format(p.get("name") or "", p.get("pallets_label") or "")
                )
            self.pallet_info.setText("\n".join(lines))
            self.pallet_info.show()
        else:
            self.pallet_info.hide()
        self.reload_table()

    def _on_sync_fail(self, err: str) -> None:
        self.sync_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setVisible(False)
        self._show_sync_info("Ошибка: {}".format(err), "error")
        QMessageBox.critical(self, "Синхронизация", err)

    def select_all_matching(self) -> None:
        src = self.current_source()
        if not src or self._tab != "new":
            return
        if self._select_all_matching:
            self._select_all_matching = False
            self._selected_order_ids.clear()
            self.reload_table()
            return
        ids = self.orders.list_order_ids(
            int(src["id"]), tab="new", search=self.search.text().strip()
        )
        self._selected_order_ids = set(ids)
        self._select_all_matching = True
        self.reload_table()

    def select_page(self) -> None:
        """Web parity: header checkbox equivalent — check every visible row."""
        if self._tab != "new":
            return
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if not item:
                continue
            oid = item.data(Qt.UserRole)
            if oid is not None:
                self._selected_order_ids.add(int(oid))
        self.reload_table()

    def run_search(self) -> None:
        """Local filter + optional WB lookup by numeric order id."""
        src = self.current_source()
        q = self.search.text().strip()
        self._page = 0
        self._select_all_matching = False
        if src and q.isdigit() and self._tab == "new":
            from app.services.lookup import lookup_order_by_id

            try:
                result = lookup_order_by_id(
                    self.db, int(src["id"]), int(q), str(src["api_key"])
                )
            except Exception as exc:
                QMessageBox.warning(self, "Поиск", str(exc))
                self.reload_table()
                return
            if result.get("found"):
                tab = str(result.get("tab") or "")
                src_kind = result.get("source") or ""
                self._show_sync_info(
                    "Найден заказ {} ({}, вкладка «{}»)".format(
                        q,
                        "локально" if src_kind == "local" else "через WB",
                        tab or "—",
                    ),
                    "ok",
                )
                if tab in self.tab_btns:
                    self.on_tab_change(tab)
                    return
            elif result.get("message"):
                self._show_sync_info(str(result.get("message")), "")
        self.reload_table()

    def collect_mgt(self) -> None:
        from app.ui.dialogs_extra import CollectMgtDialog

        src = self.current_source()
        if not src:
            return
        dlg = CollectMgtDialog(self.db, self.orders, src, self)
        if dlg.exec_():
            self.reload_table()

    def create_supply(self) -> None:
        from app.ui.dialogs_extra import SelectionSupplyDialog

        src = self.current_source()
        if not src or not self._selected_order_ids:
            QMessageBox.information(self, "Поставка", "Выберите заказы")
            return
        dlg = SelectionSupplyDialog(
            self.orders, src, sorted(self._selected_order_ids), mode="create", parent=self
        )
        if dlg.exec_():
            self._selected_order_ids.clear()
            self.reload_table()

    def add_to_supply(self) -> None:
        from app.ui.dialogs_extra import SelectionSupplyDialog

        src = self.current_source()
        if not src or not self._selected_order_ids:
            QMessageBox.information(self, "Поставка", "Выберите заказы")
            return
        dlg = SelectionSupplyDialog(
            self.orders, src, sorted(self._selected_order_ids), mode="add", parent=self
        )
        if dlg.exec_():
            self._selected_order_ids.clear()
            self.reload_table()

    def _selected_supply_id(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if not item:
            return None
        return str(item.data(Qt.UserRole) or "").strip() or None

    def open_selected_supply(self) -> None:
        if self._tab == "delivery":
            return
        sid = self._selected_supply_id()
        if not sid:
            QMessageBox.information(self, "Поставка", "Выберите поставку")
            return
        self._open_supply_by_id(sid)

    def on_row_double_click(self) -> None:
        if self._tab == "assembly":
            self.open_selected_supply()

    def _print_stickers_for(self, sid: str) -> None:
        """Product stickers for one supply, used by row menu and bottom bar."""
        from app.services.print_docs import print_supply_stickers

        from PyQt5.QtGui import QCursor
        from PyQt5.QtWidgets import QApplication

        src = self.current_source()
        if not src or not sid:
            return
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            print_supply_stickers(
                self.db,
                self.orders,
                int(src["id"]),
                str(src["api_key"]),
                sid,
                parent=self,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Стикеры товаров", str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def _supply_qr_for(self, sid: str) -> None:
        """Supply QR/sticker, used by row menu and bottom bar."""
        from app.ui.dialogs_extra import show_supply_qr

        src = self.current_source()
        if not src or not sid:
            return
        supply = self.orders.get_supply(int(src["id"]), sid) or {}
        order_ids = supply.get("order_ids") or []
        city = str(
            supply.get("warehouse_label")
            or supply.get("warehouse_name")
            or ""
        ).strip()
        try:
            show_supply_qr(
                str(src["api_key"]),
                sid,
                self,
                order_count=len(order_ids) if isinstance(order_ids, list) else 0,
                city=city,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Стикер поставки", str(exc))

    def _box_stickers_for(self, sid: str) -> None:
        """Box (TRBX) stickers for one supply, used by row menu and bottom bar."""
        from app.services.trbx_stickers import TrbxService
        from app.ui.dialogs_extra import show_png_list

        src = self.current_source()
        if not src or not sid:
            return
        trbx = TrbxService(self.db)
        try:
            boxes = trbx.refresh(int(src["id"]), str(src["api_key"]), sid)
        except Exception:
            boxes = trbx.list_boxes(int(src["id"]), sid)
        ids = []
        for b in boxes:
            if isinstance(b, dict):
                bid = str(b.get("id") or b.get("trbxId") or "").strip()
            else:
                bid = str(b or "").strip()
            if bid:
                ids.append(bid)
        if not ids:
            QMessageBox.information(self, "Стикеры коробов", "Нет грузомест")
            return
        try:
            pngs = trbx.stickers_png(str(src["api_key"]), sid, ids)
            show_png_list(pngs, "Стикеры коробов · {}".format(sid), self)
        except Exception as exc:
            QMessageBox.critical(self, "Стикеры коробов", str(exc))

    def print_stickers(self) -> None:
        from app.ui.dialogs_extra import show_order_stickers

        src = self.current_source()
        if not src:
            return
        if self._tab == "assembly":
            sid = self._selected_supply_id()
            if not sid:
                QMessageBox.information(self, "Стикеры товаров", "Выберите поставку")
                return
            self._print_stickers_for(sid)
            return
        if self._tab == "delivery":
            return
        ids = sorted(self._selected_order_ids)
        if not ids:
            QMessageBox.information(self, "Стикеры", "Нет заказов для печати")
            return
        try:
            show_order_stickers(str(src["api_key"]), ids, self)
        except Exception as exc:
            QMessageBox.critical(self, "Стикеры", str(exc))

    def print_supply_sticker(self) -> None:
        sid = self._selected_supply_id()
        if not sid:
            QMessageBox.information(self, "Стикер поставки", "Выберите поставку")
            return
        self._supply_qr_for(sid)

    def print_box_stickers(self) -> None:
        sid = self._selected_supply_id()
        if not sid:
            QMessageBox.information(self, "Стикеры коробов", "Выберите поставку")
            return
        self._box_stickers_for(sid)

    def print_supply_qr(self) -> None:
        sid = self._selected_supply_id()
        if not sid:
            QMessageBox.information(self, "QR", "Выберите поставку")
            return
        self._supply_qr_for(sid)
