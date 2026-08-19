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
from app.ui.format_helpers import ago_label, make_badge, make_photo_label
from app.ui.layout_utils import FlowLayout
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
        self.tab_btns = {}  # type: Dict[str, QPushButton]
        for key, label in (
            ("new", "Новые"),
            ("assembly", "На сборке"),
            ("delivery", "В доставке"),
        ):
            btn = QPushButton("{} · 0".format(label))
            btn.setObjectName("tabBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
            self._tab_group.addButton(btn)
            self.tab_btns[key] = btn
            tabs_row.addWidget(btn)
            btn.clicked.connect(lambda _=False, k=key: self._set_tab(k))
        self.tab_btns["new"].setChecked(True)
        tabs_row.addStretch(1)

        self.collect_mgt_btn = QPushButton("Собрать все МГТ-заказы")
        self.collect_mgt_btn.setObjectName("mgtBtn")
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

        # Pagination — compact modern strip (combo instead of XP spinbox)
        pager_frame = QFrame()
        pager_frame.setObjectName("pagerBar")
        pager = QHBoxLayout(pager_frame)
        pager.setContentsMargins(0, 0, 0, 0)
        pager.setSpacing(8)
        pager.addStretch(1)
        pager_hint = QLabel("На стр.")
        pager_hint.setObjectName("hint")
        pager.addWidget(pager_hint)
        self.page_size = QComboBox()
        self.page_size.setObjectName("pageSizeCombo")
        for n in (30, 50, 100):
            self.page_size.addItem(str(n), n)
        self.page_size.setCurrentIndex(1)  # 50
        self.page_size.currentIndexChanged.connect(self.reload_table)
        pager.addWidget(self.page_size)
        self.prev_btn = QPushButton("←")
        self.prev_btn.setObjectName("pagerBtn")
        self.prev_btn.setCursor(Qt.PointingHandCursor)
        self.prev_btn.clicked.connect(self.prev_page)
        self.next_btn = QPushButton("→")
        self.next_btn.setObjectName("pagerBtn")
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.clicked.connect(self.next_page)
        self.page_label = QLabel("1/1 · 0")
        self.page_label.setObjectName("pageMeta")
        self.page_label.setAlignment(Qt.AlignCenter)
        pager.addWidget(self.prev_btn)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_btn)
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

        self.collect_mgt_btn.setVisible(is_new)
        self.btn_select_page.setVisible(is_new)
        self.btn_select_all_matching.setVisible(is_new)
        self.btn_clear_sel.setVisible(is_new and bool(self._selected_order_ids))

        self.btn_new_supply.setVisible(is_new)
        self.btn_add_supply.setVisible(is_new)
        self.btn_open_supply.setVisible(bool(sid) and (is_asm or is_del))
        self.btn_print_stickers.setVisible(
            (is_new and has_new_sel) or (is_asm and bool(sid))
        )
        self.btn_supply_sticker.setVisible(is_asm and bool(sid))
        self.btn_box_stickers.setVisible(is_asm and bool(sid))
        self.btn_supply_qr.setVisible(is_del and bool(sid))

        # Web parity: bottom bar hides entirely with no selection on the
        # "new" tab; on assembly/delivery it hides until a supply row is
        # picked (pagination below stays visible regardless).
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
            ("new", "Новые", counts.get("new", 0)),
            ("assembly", "На сборке", counts.get("assembly", 0)),
            ("delivery", "В доставке", counts.get("delivery", 0)),
        )
        for key, label, n in mapping:
            btn = self.tab_btns[key]
            btn.setText("{} · {}".format(label, n))
            btn.updateGeometry()

    def reload_table(self) -> None:
        self.update_bottom_visibility()
        src = self.current_source()
        if not src:
            return
        sid = int(src["id"])
        counts = self.orders.tab_counts(sid)
        self._update_tab_labels(counts)

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
        elif self._tab == "assembly":
            rows, total = self.orders.list_supplies(
                sid, done=False, search=search, limit=limit, offset=offset
            )
            self._last_total = total
            self._fill_supplies_table(rows)
            self.sel_label.setText("Поставки на сборке")
        else:
            rows, total = self.orders.list_supplies(
                sid, done=True, search=search, limit=limit, offset=offset
            )
            self._last_total = total
            self._fill_supplies_table(rows)
            self.sel_label.setText("Поставки в доставке")

        pages = max(1, (total + limit - 1) // limit)
        if self._page >= pages:
            self._page = pages - 1
        self.page_label.setText("{}/{} · {}".format(self._page + 1, pages, total))
        self.update_bottom_visibility()

    def _table_layout(self) -> str:
        return "new" if self._tab == "new" else "supplies"

    @staticmethod
    def _col_settings_key(layout: str) -> str:
        return "fbs_table_cols_{}".format(layout)

    @staticmethod
    def _default_col_widths(layout: str, count: int) -> List[int]:
        defaults = {
            "new": [40, 92, 120, 320, 100, 88, 140],
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
        """Rich rows mirror web: photo / bold order+age / product+badges."""
        try:
            self.table.itemChanged.disconnect(self._on_check_change)
        except Exception:
            pass
        cols = ["", "Фото", "Заказ", "Товар", "Склад", "Цена", "ШК"]
        self.table.blockSignals(True)
        self.table.clear()
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setRowCount(len(rows))
        self.table.verticalHeader().setDefaultSectionSize(88)
        for r, row in enumerate(rows):
            oid = int(row["order_id"])
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(
                Qt.Checked if oid in self._selected_order_ids else Qt.Unchecked
            )
            chk.setData(Qt.UserRole, oid)
            self.table.setItem(r, 0, chk)

            photo_wrap = QWidget()
            photo_lay = QVBoxLayout(photo_wrap)
            photo_lay.setContentsMargins(8, 8, 8, 8)
            photo_lay.setAlignment(Qt.AlignCenter)
            photo_lay.addWidget(make_photo_label(row.get("product_photo"), 72))
            self.table.setCellWidget(r, 1, photo_wrap)

            order_widget = QWidget()
            order_lay = QVBoxLayout(order_widget)
            order_lay.setContentsMargins(10, 10, 10, 10)
            order_lay.setSpacing(4)
            order_lay.addWidget(self._bold_label(str(oid)))
            ago = ago_label(row.get("created_at_wb"))
            if ago:
                order_lay.addWidget(make_badge(ago, "time"))
            order_lay.addStretch(1)
            self.table.setCellWidget(r, 2, order_widget)

            prod_widget = QWidget()
            prod_lay = QVBoxLayout(prod_widget)
            prod_lay.setContentsMargins(10, 10, 10, 10)
            prod_lay.setSpacing(4)
            article = str(row.get("article") or "")
            name = str(row.get("product_name") or article or "—")
            prod_lay.addWidget(self._bold_label(name, wrap=True))
            if article and row.get("product_name"):
                sub = QLabel("Арт. {}".format(article))
                sub.setObjectName("hint")
                prod_lay.addWidget(sub)
            badge_pairs = []
            cargo = row.get("cargo_label")
            if cargo:
                badge_pairs.append((cargo, "cargo"))
            if row.get("is_b2b"):
                badge_pairs.append(("B2B", ""))
            if badge_pairs:
                badges_row = QHBoxLayout()
                badges_row.setContentsMargins(0, 0, 0, 0)
                badges_row.setSpacing(4)
                for text, kind in badge_pairs:
                    badges_row.addWidget(make_badge(text, kind))
                badges_row.addStretch(1)
                prod_lay.addLayout(badges_row)
            prod_lay.addStretch(1)
            self.table.setCellWidget(r, 3, prod_widget)

            self.table.setItem(
                r,
                4,
                QTableWidgetItem(
                    str(row.get("warehouse_label") or row.get("warehouse_id") or "")
                ),
            )
            self.table.setItem(
                r, 5, QTableWidgetItem(str(row.get("price_label") or ""))
            )
            skus = row.get("skus") or []
            self.table.setItem(
                r, 6, QTableWidgetItem(", ".join(str(s) for s in skus[:3]))
            )
        self.table.blockSignals(False)
        self.table.itemChanged.connect(self._on_check_change)
        self._apply_table_col_widths("new")

    def _on_check_change(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        oid = item.data(Qt.UserRole)
        if oid is None:
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

    def _fill_supplies_table(self, rows: List[Dict[str, Any]]) -> None:
        try:
            self.table.itemChanged.disconnect(self._on_check_change)
        except Exception:
            pass
        cols = ["Поставка", "Название", "Заказов", "Коробов", "Тип", "Статус", "B2B", ""]
        self.table.blockSignals(True)
        self.table.clear()
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setRowCount(len(rows))
        self.table.verticalHeader().setDefaultSectionSize(52)
        for r, row in enumerate(rows):
            sid = str(row.get("supply_id") or "")
            item0 = self._table_text_item(sid, bold=True, tooltip=sid)
            item0.setData(Qt.UserRole, sid)
            self.table.setItem(r, 0, item0)

            name_text = str(row.get("name") or "")
            if row.get("pickup_allowed"):
                name_widget = QWidget()
                name_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
                name_lay = QVBoxLayout(name_widget)
                name_lay.setContentsMargins(12, 6, 12, 6)
                name_lay.setSpacing(4)
                name_lay.addWidget(self._bold_label(name_text, wrap=True))
                pvz_row = QHBoxLayout()
                pvz_row.setContentsMargins(0, 0, 0, 0)
                pvz_row.setSpacing(4)
                pvz_row.addWidget(make_badge("Можно в ПВЗ", "pvz"))
                pvz_row.addStretch(1)
                name_lay.addLayout(pvz_row)
                self.table.setCellWidget(r, 1, name_widget)
            else:
                self.table.setItem(
                    r, 1, self._table_text_item(name_text, bold=True, tooltip=name_text)
                )

            for col, key, tip in (
                (2, "order_count", ""),
                (3, "boxes_count", ""),
                (4, "cargo_label", "cargo_label"),
                (5, "status_label", "status_label"),
            ):
                val = str(row.get(key) or (0 if key.endswith("_count") else ""))
                item = self._table_text_item(val, tooltip=str(row.get(tip) or val))
                self.table.setItem(r, col, item)
            self.table.setItem(
                r,
                6,
                self._table_text_item("да" if row.get("is_b2b") else ""),
            )

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
            self.table.setRowHeight(r, max(self.table.rowHeight(r), 52))
        self.table.blockSignals(False)
        self._apply_table_col_widths("supplies")

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
        return str(item.data(Qt.UserRole) or item.text() or "")

    def open_selected_supply(self) -> None:
        from app.ui.supply_detail import SupplyDetailDialog

        src = self.current_source()
        sid = self._selected_supply_id()
        if not src or not sid:
            QMessageBox.information(self, "Поставка", "Выберите поставку")
            return
        fullscreen = self._tab == "assembly"
        dlg = SupplyDetailDialog(
            self.db,
            self.orders,
            src,
            sid,
            None if fullscreen else self,
            fullscreen=fullscreen,
        )
        dlg.exec_()
        self.reload_table()

    def on_row_double_click(self) -> None:
        if self._tab in ("assembly", "delivery"):
            self.open_selected_supply()

    def _print_stickers_for(self, sid: str) -> None:
        """Product stickers for one supply, used by row menu and bottom bar."""
        from app.services.print_docs import print_supply_stickers

        src = self.current_source()
        if not src or not sid:
            return
        try:
            print_supply_stickers(
                self.db, self.orders, int(src["id"]), str(src["api_key"]), sid
            )
        except Exception as exc:
            QMessageBox.critical(self, "Стикеры товаров", str(exc))

    def _supply_qr_for(self, sid: str) -> None:
        """Supply QR/sticker, used by row menu and bottom bar."""
        from app.ui.dialogs_extra import show_supply_qr

        src = self.current_source()
        if not src or not sid:
            return
        try:
            show_supply_qr(str(src["api_key"]), sid, self)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Стикер поставки",
                "{}\n\nQR доступен после передачи поставки в доставку на портале WB.".format(
                    exc
                ),
            )

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
        if self._tab in ("assembly", "delivery"):
            sid = self._selected_supply_id()
            if not sid:
                QMessageBox.information(self, "Стикеры товаров", "Выберите поставку")
                return
            self._print_stickers_for(sid)
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
