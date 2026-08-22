# -*- coding: utf-8 -*-
"""Ozon FBS main page — UX parity with WB FBS (phase 1: sync + lists)."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

from app.db import Database
from app.services import SourceService, clamp_lookback_days
from app.services.ozon_orders import OzonOrdersService
from app.ui.format_helpers import make_badge, make_photo_label, make_status_pill
from app.ui.layout_utils import FbsTabButton, fit_tab_button
from app.ozon.sync import sync_source as ozon_sync_source


class OzonSyncWorker(QThread):
    progress = pyqtSignal(str, int)
    finished_ok = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(
        self,
        db: Database,
        sources: List[Dict[str, Any]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super(OzonSyncWorker, self).__init__(parent)
        self.db = db
        self.sources = list(sources or [])
        self._stop = False
        self._lock = threading.Lock()
        self._status = {}  # type: Dict[int, Dict[str, Any]]

    def request_stop(self) -> None:
        self._stop = True

    def _set_status(self, source_id: int, line: str, n: int) -> None:
        with self._lock:
            self._status[int(source_id)] = {"line": line, "n": int(n or 0)}
            lines = []
            total = 0
            for src in self.sources:
                info = self._status.get(int(src["id"]))
                if info:
                    lines.append(str(info.get("line") or ""))
                    total += int(info.get("n") or 0)
        self.progress.emit("\n".join(lines), total)

    def run(self) -> None:
        if not self.sources:
            self.finished_ok.emit(
                {"postings": 0, "carriages": 0, "errors": [], "stopped": False}
            )
            return
        totals = {"postings": 0, "carriages": 0, "errors": [], "stopped": False}

        def sync_one(src: Dict[str, Any]) -> Dict[str, Any]:
            sid = int(src["id"])
            name = str(src.get("name") or sid)
            lookback = clamp_lookback_days(src.get("lookback_days"))

            def prog(msg, n, _sid=sid, _name=name):
                self._set_status(_sid, "{} · {} · {}".format(_name, msg, n), n)

            self._set_status(sid, "{} · старт…".format(name), 0)
            return ozon_sync_source(
                self.db,
                sid,
                str(src.get("client_id") or ""),
                str(src.get("api_key") or ""),
                lookback,
                stop_requested=lambda: self._stop,
                progress=prog,
            )

        with ThreadPoolExecutor(max_workers=min(4, len(self.sources))) as pool:
            futures = {pool.submit(sync_one, s): s for s in self.sources}
            for fut in as_completed(futures):
                src = futures[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    totals["errors"].append(
                        "{}: {}".format(src.get("name") or src["id"], exc)
                    )
                    continue
                totals["postings"] += int(res.get("postings") or 0)
                totals["carriages"] += int(res.get("carriages") or 0)
                totals["errors"].extend(res.get("errors") or [])
                if res.get("stopped"):
                    totals["stopped"] = True
                SourceService(self.db).touch_synced(int(src["id"]))

        self.finished_ok.emit(totals)


class OzonFbsPage(QWidget):
    def __init__(
        self,
        db: Database,
        sources: SourceService,
        orders: OzonOrdersService,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(OzonFbsPage, self).__init__(parent)
        self.db = db
        self.sources = sources
        self.orders = orders
        self._tab = "new"
        self._page = 0
        self._sync_worker = None  # type: Optional[OzonSyncWorker]
        self._selected_postings = set()  # type: set

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        toolbar = QFrame()
        toolbar.setObjectName("toolbarPanel")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(16, 12, 16, 12)
        tb.setSpacing(10)
        self.sync_btn = QPushButton("Синхронизировать")
        self.sync_btn.clicked.connect(self.run_sync)
        self.stop_btn = QPushButton("Стоп")
        self.stop_btn.setObjectName("secondary")
        self.stop_btn.hide()
        self.stop_btn.clicked.connect(self.stop_sync)
        self.create_carriage_btn = QPushButton("Создать отгрузку")
        self.create_carriage_btn.setObjectName("secondary")
        self.create_carriage_btn.clicked.connect(self.create_carriage)
        self.open_carriage_btn = QPushButton("Открыть отгрузку")
        self.open_carriage_btn.clicked.connect(self.open_selected_carriage)
        tb.addWidget(self.sync_btn)
        tb.addWidget(self.stop_btn)
        tb.addWidget(self.create_carriage_btn)
        tb.addWidget(self.open_carriage_btn)
        tb.addStretch(1)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("sdSearch")
        self.search_input.setPlaceholderText("Поиск по отправлению, артикулу, SKU…")
        self.search_input.textChanged.connect(self.reload_table)
        self.search_input.setMinimumWidth(240)
        tb.addWidget(self.search_input)
        root.addWidget(toolbar)

        self.sync_info = QLabel("")
        self.sync_info.setObjectName("syncInfoText")
        self.sync_info.setWordWrap(True)
        self.sync_info.hide()
        root.addWidget(self.sync_info)

        tabs_row = QFrame()
        tabs_row.setObjectName("tabsRow")
        tabs_lay = QHBoxLayout(tabs_row)
        tabs_lay.setContentsMargins(8, 0, 8, 0)
        tabs_lay.setSpacing(4)
        self.tab_new = FbsTabButton("Новые")
        self.tab_assembly = FbsTabButton("На сборке")
        self.tab_delivery = FbsTabButton("В доставке")
        self.tab_finished = FbsTabButton("Завершённые")
        self.tab_new.clicked.connect(lambda: self.on_tab_change("new"))
        self.tab_assembly.clicked.connect(lambda: self.on_tab_change("assembly"))
        self.tab_delivery.clicked.connect(lambda: self.on_tab_change("delivery"))
        self.tab_finished.clicked.connect(lambda: self.on_tab_change("finished"))
        self.tab_new.setChecked(True)
        tabs_lay.addWidget(self.tab_new)
        tabs_lay.addWidget(self.tab_assembly)
        tabs_lay.addWidget(self.tab_delivery)
        tabs_lay.addWidget(self.tab_finished)
        tabs_lay.addStretch(1)
        root.addWidget(tabs_row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Отправление", "Товар", "Склад"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.doubleClicked.connect(self._on_row_double_click)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        root.addWidget(self.table, 1)

        self.bottom_bar = QFrame()
        self.bottom_bar.setObjectName("bottomBar")
        bottom_outer = QVBoxLayout(self.bottom_bar)
        bottom_outer.setContentsMargins(16, 12, 16, 12)
        bottom_outer.setSpacing(8)
        sel_row = QHBoxLayout()
        self.sel_label = QLabel("Выбрано 0 отправлений")
        self.sel_label.setObjectName("selectedLabel")
        sel_row.addWidget(self.sel_label)
        self.btn_select_page = QPushButton("Выбрать все на странице")
        self.btn_select_page.setObjectName("linkBtn")
        self.btn_select_page.clicked.connect(self.select_page)
        sel_row.addWidget(self.btn_select_page)
        sel_row.addStretch(1)
        bottom_outer.addLayout(sel_row)
        actions = QHBoxLayout()
        self.btn_add_carriage = QPushButton("Добавить в отгрузку")
        self.btn_add_carriage.clicked.connect(self.add_to_carriage)
        self.btn_ship_sel = QPushButton("Собрать (ship)")
        self.btn_ship_sel.setObjectName("secondary")
        self.btn_ship_sel.clicked.connect(self.ship_selected)
        actions.addWidget(self.btn_add_carriage)
        actions.addWidget(self.btn_ship_sel)
        actions.addStretch(1)
        bottom_outer.addLayout(actions)
        self.bottom_bar.hide()
        root.addWidget(self.bottom_bar)

        pager_frame = QFrame()
        pager_frame.setObjectName("pagerBar")
        pager = QHBoxLayout(pager_frame)
        pager.setContentsMargins(0, 4, 0, 0)
        pager.setSpacing(8)
        self.pager_total = QLabel("Записей: 0")
        self.pager_total.setObjectName("pagerTotal")
        pager.addWidget(self.pager_total)
        pager.addStretch(1)
        self.prev_btn = QPushButton("← Назад")
        self.prev_btn.setObjectName("pagerBtn")
        self.prev_btn.clicked.connect(self.prev_page)
        self.next_btn = QPushButton("Вперёд →")
        self.next_btn.setObjectName("pagerBtn")
        self.next_btn.clicked.connect(self.next_page)
        self.page_label = QLabel("1 / 1")
        self.page_label.setObjectName("pageMeta")
        pager.addWidget(self.prev_btn)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_btn)
        self.page_size = QComboBox()
        self.page_size.setObjectName("pageSizeCombo")
        for n in (30, 50, 100):
            self.page_size.addItem("{} / стр.".format(n), n)
        self.page_size.setCurrentIndex(1)
        self.page_size.currentIndexChanged.connect(self._on_page_size_change)
        pager.addWidget(self.page_size)
        root.addWidget(pager_frame)

        hint = QLabel(
            "Новые: выберите отправления и добавьте в отгрузку или соберите (ship). "
            "На сборке: двойной клик — карточка отгрузки или отправления."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.source_combo = QComboBox()
        self.source_combo.setObjectName("sourceCombo")
        self.source_combo.currentIndexChanged.connect(self.reload_table)
        self.reload_sources()

    def reload_sources(self) -> None:
        cur = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for s in self.sources.list_ozon_fbs_enabled():
            self.source_combo.addItem(str(s.get("name") or s["id"]), int(s["id"]))
        if cur is not None:
            idx = self.source_combo.findData(cur)
            if idx >= 0:
                self.source_combo.setCurrentIndex(idx)
        self.source_combo.blockSignals(False)
        self._update_tab_counts()
        self.reload_table()

    def current_source(self) -> Optional[Dict[str, Any]]:
        sid = self.source_combo.currentData()
        if sid is None:
            return None
        return self.sources.get(int(sid))

    def on_tab_change(self, tab: str) -> None:
        self._tab = tab
        self._page = 0
        self._selected_postings.clear()
        self.tab_new.setChecked(tab == "new")
        self.tab_assembly.setChecked(tab == "assembly")
        self.tab_delivery.setChecked(tab == "delivery")
        self.tab_finished.setChecked(tab == "finished")
        self.bottom_bar.setVisible(tab == "new")
        self.reload_table()

    def _on_page_size_change(self) -> None:
        self._page = 0
        self.reload_table()

    def prev_page(self) -> None:
        if self._page > 0:
            self._page -= 1
            self.reload_table()

    def next_page(self) -> None:
        self._page += 1
        self.reload_table()

    def select_page(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if not item:
                continue
            pnum = str(item.data(Qt.UserRole) or item.text() or "").strip()
            if pnum:
                self._selected_postings.add(pnum)
        self._on_selection_changed()
        self.table.selectAll()

    def _on_selection_changed(self) -> None:
        if self._tab != "new":
            return
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if item:
                pnum = str(item.data(Qt.UserRole) or item.text() or "").strip()
                if pnum:
                    self._selected_postings.add(pnum)
        self.sel_label.setText(
            "Выбрано {} отправлений".format(len(self._selected_postings))
        )

    def _selected_posting_numbers(self) -> List[str]:
        if self._tab == "new":
            out = set(self._selected_postings)
            for idx in self.table.selectionModel().selectedRows():
                item = self.table.item(idx.row(), 0)
                if item:
                    pnum = str(item.data(Qt.UserRole) or item.text() or "").strip()
                    if pnum:
                        out.add(pnum)
            return sorted(out)
        return []

    def _update_tab_counts(self) -> None:
        src = self.current_source()
        counts = {"new": 0, "assembly": 0, "delivery": 0, "finished": 0}
        if src:
            counts = self.orders.tab_counts(int(src["id"]))
        self.tab_new.set_count(int(counts.get("new") or 0))
        self.tab_assembly.set_count(int(counts.get("assembly") or 0))
        self.tab_delivery.set_count(int(counts.get("delivery") or 0))
        self.tab_finished.set_count(int(counts.get("finished") or 0))

    def reload_table(self) -> None:
        src = self.current_source()
        self._update_tab_counts()
        self.table.setRowCount(0)
        if not src:
            return
        sid = int(src["id"])
        search = self.search_input.text()
        limit = int(self.page_size.currentData() or 50)
        offset = self._page * limit

        if self._tab == "new":
            total = self.orders.count_new_postings(sid, search=search)
            rows = self.orders.list_new_postings(
                sid, search=search, limit=limit, offset=offset
            )
            self._fill_posting_rows(rows, ["Отправление", "Товар", "Склад"])
            self._update_pager(total, limit)
            return

        if self._tab == "finished":
            total = self.orders.count_finished_postings(sid, search=search)
            rows = self.orders.list_finished_postings(
                sid, search=search, limit=limit, offset=offset
            )
            self._fill_posting_rows(
                rows, ["Отправление", "Товар", "Статус"], status_col=True
            )
            self._update_pager(total, limit)
            return

        if self._tab == "delivery":
            carriages = self.orders.list_delivery_carriages(sid)
            self._fill_carriage_rows(carriages)
            self.pager_total.setText("Отгрузок: {}".format(len(carriages)))
            self.page_label.setText("1 / 1")
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            return

        carriages = self.orders.list_open_carriages(sid)
        orphans = self.orders.list_assembly_postings(sid, search=search)
        self.table.setHorizontalHeaderLabels(
            ["Отгрузка / отправление", "Статус / товар", "Отправлений"]
        )
        total_rows = len(carriages) + len(orphans)
        self.table.setRowCount(total_rows)
        row_i = 0
        for c in carriages:
            item = QTableWidgetItem(str(c.get("carriage_id") or ""))
            item.setData(Qt.UserRole, ("carriage", str(c.get("carriage_id") or "")))
            self.table.setItem(row_i, 0, item)
            pill = make_status_pill(
                str(c.get("status_label") or "—"),
                str(c.get("status_kind") or "assembly"),
            )
            wrap = QWidget()
            lay = QHBoxLayout(wrap)
            lay.setContentsMargins(8, 8, 8, 8)
            lay.addWidget(pill, 0, Qt.AlignLeft)
            lay.addStretch(1)
            self.table.setCellWidget(row_i, 1, wrap)
            self.table.setItem(
                row_i, 2, QTableWidgetItem(str(int(c.get("posting_count") or 0)))
            )
            self.table.setRowHeight(row_i, 56)
            row_i += 1
        for r in orphans:
            pnum = str(r.get("posting_number") or "")
            label = "· {}".format(pnum) if pnum else "· —"
            item = QTableWidgetItem(label)
            item.setData(Qt.UserRole, ("posting", pnum))
            self.table.setItem(row_i, 0, item)
            self.table.setCellWidget(row_i, 1, self._product_cell(r))
            self.table.setItem(
                row_i,
                2,
                QTableWidgetItem(str(r.get("status_label") or "—")),
            )
            self.table.setRowHeight(row_i, 120)
            row_i += 1
        self.pager_total.setText("На сборке: {}".format(total_rows))
        self.page_label.setText("1 / 1")
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)

    def _fill_posting_rows(
        self,
        rows: List[Dict[str, Any]],
        headers: List[str],
        *,
        status_col: bool = False,
    ) -> None:
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            pnum = str(r.get("posting_number") or "")
            item = QTableWidgetItem(pnum)
            item.setData(Qt.UserRole, pnum)
            self.table.setItem(i, 0, item)
            self.table.setCellWidget(i, 1, self._product_cell(r))
            if status_col:
                self.table.setItem(
                    i, 2, QTableWidgetItem(str(r.get("status_label") or "—"))
                )
            else:
                self.table.setItem(
                    i, 2, QTableWidgetItem(str(r.get("warehouse_name") or "—"))
                )
            self.table.setRowHeight(i, 120)

    def _fill_carriage_rows(self, carriages: List[Dict[str, Any]]) -> None:
        self.table.setHorizontalHeaderLabels(
            ["Отгрузка", "Статус", "Отправлений"]
        )
        self.table.setRowCount(len(carriages))
        for i, c in enumerate(carriages):
            item = QTableWidgetItem(str(c.get("carriage_id") or ""))
            item.setData(Qt.UserRole, ("carriage", str(c.get("carriage_id") or "")))
            self.table.setItem(i, 0, item)
            pill = make_status_pill(
                str(c.get("status_label") or "—"),
                str(c.get("status_kind") or "delivery"),
            )
            wrap = QWidget()
            lay = QHBoxLayout(wrap)
            lay.setContentsMargins(8, 8, 8, 8)
            lay.addWidget(pill, 0, Qt.AlignLeft)
            lay.addStretch(1)
            self.table.setCellWidget(i, 1, wrap)
            self.table.setItem(
                i, 2, QTableWidgetItem(str(int(c.get("posting_count") or 0)))
            )
            self.table.setRowHeight(i, 56)

    def _update_pager(self, total: int, limit: int) -> None:
        pages = max(1, (int(total) + limit - 1) // limit)
        if self._page >= pages:
            self._page = pages - 1
        self.pager_total.setText("Записей: {}".format(int(total)))
        self.page_label.setText("{} / {}".format(self._page + 1, pages))
        self.prev_btn.setEnabled(self._page > 0)
        self.next_btn.setEnabled(self._page + 1 < pages)

    def _on_row_double_click(self, _index) -> None:
        src = self.current_source()
        if not src:
            return
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        if not item:
            return
        data = item.data(Qt.UserRole)
        if self._tab == "new" or self._tab == "finished":
            pnum = str(data or item.text() or "").strip()
            if pnum:
                self._open_posting_by_id(src, pnum)
            return
        if self._tab == "delivery":
            if isinstance(data, (list, tuple)) and data[0] == "carriage":
                self._open_carriage_by_id(src, str(data[1]))
            return
        if isinstance(data, (list, tuple)):
            if data[0] == "carriage":
                self._open_carriage_by_id(src, str(data[1]))
            elif data[0] == "posting":
                self._open_posting_by_id(src, str(data[1]))
            return
        self.open_selected_carriage()

    def _open_posting_by_id(self, src: Dict[str, Any], posting_number: str) -> None:
        from app.ui.ozon_posting_detail import OzonPostingDetailDialog

        dlg = OzonPostingDetailDialog(
            self.db,
            self.orders,
            src,
            posting_number,
            self.window(),
            fullscreen=True,
        )
        dlg.exec_()
        if dlg.posting_mutated:
            self.reload_table()

    def _selected_carriage_id(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if not item:
            return None
        data = item.data(Qt.UserRole)
        if isinstance(data, (list, tuple)) and len(data) == 2 and data[0] == "carriage":
            return str(data[1] or "").strip() or None
        text = str(item.text() or "").strip()
        if text.startswith("·"):
            return None
        return text or None

    def open_selected_carriage(self) -> None:
        src = self.current_source()
        cid = self._selected_carriage_id()
        if not src or not cid:
            QMessageBox.information(
                self, "Ozon FBS", "Выберите отгрузку в списке «На сборке»."
            )
            return
        self._open_carriage_by_id(src, cid)

    def _open_carriage_by_id(self, src: Dict[str, Any], carriage_id: str) -> None:
        from app.ui.ozon_carriage_detail import OzonCarriageDetailDialog

        dlg = OzonCarriageDetailDialog(
            self.db,
            self.orders,
            src,
            carriage_id,
            self.window(),
            fullscreen=True,
        )
        dlg.exec_()
        if dlg.carriage_mutated:
            self.reload_table()

    def add_to_carriage(self) -> None:
        src = self.current_source()
        pnums = self._selected_posting_numbers()
        if not src or not pnums:
            QMessageBox.information(
                self, "Ozon FBS", "Выберите отправления на вкладке «Новые»."
            )
            return
        from app.ozon.client import OzonFbsClient
        from PyQt5.QtWidgets import QDialog, QDialogButtonBox, QFormLayout

        sid = int(src["id"])
        client = OzonFbsClient(
            str(src.get("client_id") or ""), str(src.get("api_key") or "")
        )
        carriages = self.orders.list_open_carriages(sid)
        if not carriages:
            QMessageBox.information(
                self,
                "Ozon FBS",
                "Нет открытых отгрузок. Сначала создайте отгрузку.",
            )
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Добавить в отгрузку")
        form = QFormLayout(dlg)
        combo = QComboBox()
        for c in carriages:
            cid = str(c.get("carriage_id") or "")
            combo.addItem(
                "Отгрузка {} ({} шт.)".format(
                    cid, int(c.get("posting_count") or 0)
                ),
                cid,
            )
        form.addRow("Отгрузка", combo)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec_() != QDialog.Accepted:
            return
        cid = str(combo.currentData() or "")
        try:
            self.orders.add_postings_to_carriage(sid, client, cid, pnums)
        except Exception as exc:
            QMessageBox.warning(self, "Ozon FBS", str(exc))
            return
        self._selected_postings.clear()
        QMessageBox.information(
            self,
            "Ozon FBS",
            "Добавлено {} отправлений в отгрузку {}.".format(len(pnums), cid),
        )
        self.on_tab_change("assembly")

    def ship_selected(self) -> None:
        src = self.current_source()
        pnums = self._selected_posting_numbers()
        if not src or not pnums:
            QMessageBox.information(self, "Ship", "Выберите отправления.")
            return
        from app.ozon.client import OzonFbsClient
        from app.services.ozon_ship import OzonShipService

        client = OzonFbsClient(
            str(src.get("client_id") or ""), str(src.get("api_key") or "")
        )
        try:
            res = OzonShipService(self.db).ship_postings(
                client, int(src["id"]), pnums
            )
            errs = res.get("errors") or []
            msg = "Собрано: {}.".format(len(res.get("ok") or []))
            if errs:
                msg += "\n" + "\n".join(str(e) for e in errs[:8])
            QMessageBox.information(self, "Ship", msg)
            self._selected_postings.clear()
            self.reload_table()
        except Exception as exc:
            QMessageBox.warning(self, "Ship", str(exc))

    def create_carriage(self) -> None:
        src = self.current_source()
        if not src:
            QMessageBox.information(self, "Ozon FBS", "Выберите источник Ozon FBS.")
            return
        from app.ozon.client import OzonFbsClient
        from PyQt5.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout

        client = OzonFbsClient(
            str(src.get("client_id") or ""), str(src.get("api_key") or "")
        )
        try:
            methods = self.orders.list_delivery_methods(client)
        except Exception as exc:
            QMessageBox.warning(self, "Ozon FBS", str(exc))
            return
        if not methods:
            QMessageBox.information(
                self,
                "Ozon FBS",
                "Нет доступных методов доставки для создания отгрузки.",
            )
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Создать отгрузку Ozon FBS")
        form = QFormLayout(dlg)
        combo = QComboBox()
        for m in methods:
            dm_id = int(m.get("delivery_method_id") or 0)
            if not dm_id:
                continue
            label = "{} (ID {})".format(m.get("name") or "Метод", dm_id)
            combo.addItem(label, dm_id)
        if combo.count() == 0:
            QMessageBox.information(self, "Ozon FBS", "Нет методов доставки.")
            return
        form.addRow("Метод доставки", combo)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec_() != QDialog.Accepted:
            return
        dm_id = int(combo.currentData())
        try:
            cid = self.orders.create_carriage(int(src["id"]), client, dm_id)
        except Exception as exc:
            QMessageBox.warning(self, "Ozon FBS", str(exc))
            return
        QMessageBox.information(self, "Ozon FBS", "Отгрузка создана: {}.".format(cid))
        self.on_tab_change("assembly")
        self.reload_table()
        self._open_carriage_by_id(src, cid)

    def _product_cell(self, row: Dict[str, Any]) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        lay.addWidget(make_photo_label(row.get("product_photo"), 72), 0, Qt.AlignTop)
        text = QVBoxLayout()
        name = QLabel(str(row.get("product_name_display") or "—"))
        name.setWordWrap(True)
        text.addWidget(name)
        sub = QLabel(
            "Арт. {} · SKU {}{}".format(
                row.get("offer_id") or "—",
                row.get("sku") or "—",
                " · {}".format(row["product_count_label"])
                if row.get("product_count_label")
                else "",
            )
        )
        sub.setObjectName("sdProductSub")
        text.addWidget(sub)
        if row.get("barcodes"):
            text.addWidget(make_badge(str(row["barcodes"][0]), "cargo"))
        lay.addLayout(text, 1)
        return wrap

    def run_sync(self) -> None:
        enabled = self.sources.list_ozon_fbs_enabled()
        if not enabled:
            QMessageBox.information(
                self,
                "Ozon FBS",
                "Нет включённых источников Ozon FBS. "
                "Добавьте в Настройки → Источники (кнопка «Ozon FBS»).",
            )
            return
        if self._sync_worker and self._sync_worker.isRunning():
            return
        self.sync_btn.setEnabled(False)
        self.stop_btn.show()
        self.sync_info.show()
        self.sync_info.setText("Синхронизация Ozon FBS…")
        worker = OzonSyncWorker(self.db, enabled, self)
        self._sync_worker = worker
        worker.progress.connect(self._on_sync_progress)
        worker.finished_ok.connect(self._on_sync_done)
        worker.failed.connect(self._on_sync_failed)
        worker.finished.connect(self._on_sync_finished)
        worker.start()

    def stop_sync(self) -> None:
        if self._sync_worker:
            self._sync_worker.request_stop()

    def _on_sync_progress(self, text: str, _n: int) -> None:
        self.sync_info.setText(text or "Синхронизация…")

    def _on_sync_done(self, result: object) -> None:
        res = result if isinstance(result, dict) else {}
        errs = [str(e) for e in (res.get("errors") or []) if str(e).strip()]
        msg = "Синхронизация Ozon: отправлений {}, отгрузок {}.".format(
            int(res.get("postings") or 0), int(res.get("carriages") or 0)
        )
        if errs:
            msg += " Предупреждения: {}.".format(len(errs))
        self.sync_info.setText(msg)
        self.sync_info.setProperty("state", "error" if errs else "ok")
        self.sync_info.style().unpolish(self.sync_info)
        self.sync_info.style().polish(self.sync_info)
        self.reload_sources()

    def _on_sync_failed(self, message: str) -> None:
        self.sync_info.setText(str(message or "Ошибка синхронизации"))
        self.sync_info.setProperty("state", "error")
        self.sync_info.style().unpolish(self.sync_info)
        self.sync_info.style().polish(self.sync_info)

    def _on_sync_finished(self) -> None:
        self.sync_btn.setEnabled(True)
        self.stop_btn.hide()
        self._sync_worker = None
