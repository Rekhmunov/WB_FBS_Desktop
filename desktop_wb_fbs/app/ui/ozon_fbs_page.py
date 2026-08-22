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
        self._sync_worker = None  # type: Optional[OzonSyncWorker]

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
        tb.addWidget(self.sync_btn)
        tb.addWidget(self.stop_btn)
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
        self.tab_new = FbsTabButton("Новые", 0)
        self.tab_assembly = FbsTabButton("На сборке", 0)
        self.tab_new.clicked.connect(lambda: self.on_tab_change("new"))
        self.tab_assembly.clicked.connect(lambda: self.on_tab_change("assembly"))
        self.tab_new.setChecked(True)
        tabs_lay.addWidget(self.tab_new)
        tabs_lay.addWidget(self.tab_assembly)
        tabs_lay.addStretch(1)
        root.addWidget(tabs_row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Отправление", "Товар", "Склад"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        root.addWidget(self.table, 1)

        hint = QLabel(
            "Карточка отгрузки, маркировка, проверка ШК и печать — следующий этап. "
            "Синхронизация и списки уже работают на Ozon API."
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
        self.tab_new.setChecked(tab == "new")
        self.tab_assembly.setChecked(tab == "assembly")
        self.reload_table()

    def _update_tab_counts(self) -> None:
        src = self.current_source()
        counts = {"new": 0, "assembly": 0}
        if src:
            counts = self.orders.tab_counts(int(src["id"]))
        self.tab_new.set_count(int(counts.get("new") or 0))
        self.tab_assembly.set_count(int(counts.get("assembly") or 0))

    def reload_table(self) -> None:
        src = self.current_source()
        self._update_tab_counts()
        self.table.setRowCount(0)
        if not src:
            return
        sid = int(src["id"])
        search = self.search_input.text()
        if self._tab == "new":
            rows = self.orders.list_new_postings(sid, search=search)
            self.table.setHorizontalHeaderLabels(["Отправление", "Товар", "Склад"])
            self.table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                self.table.setItem(i, 0, QTableWidgetItem(str(r.get("posting_number") or "")))
                self.table.setCellWidget(i, 1, self._product_cell(r))
                self.table.setItem(i, 2, QTableWidgetItem(str(r.get("warehouse_name") or "—")))
                self.table.setRowHeight(i, 120)
            return

        carriages = self.orders.list_open_carriages(sid)
        orphans = self.orders.list_assembly_postings(sid, search=search)
        self.table.setHorizontalHeaderLabels(["Отгрузка / отправление", "Статус / товар", "Отправлений"])
        total_rows = len(carriages) + len(orphans)
        self.table.setRowCount(total_rows)
        row_i = 0
        for c in carriages:
            self.table.setItem(row_i, 0, QTableWidgetItem(str(c.get("carriage_id") or "")))
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
            label = str(r.get("posting_number") or "")
            if not label.startswith("·"):
                label = "· {}".format(label)
            self.table.setItem(row_i, 0, QTableWidgetItem(label))
            self.table.setCellWidget(row_i, 1, self._product_cell(r))
            self.table.setItem(
                row_i,
                2,
                QTableWidgetItem(str(r.get("status_label") or "—")),
            )
            self.table.setRowHeight(row_i, 120)
            row_i += 1

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
            "Арт. {} · SKU {}".format(
                row.get("offer_id") or "—", row.get("sku") or "—"
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
