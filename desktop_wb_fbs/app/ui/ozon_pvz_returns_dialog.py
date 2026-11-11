# -*- coding: utf-8 -*-
"""Ozon FBS PVZ returns (ReceivedBySeller) dialog."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.services.ozon_pvz_returns import OzonPvzReturnsService
from app.ui.dialog_utils import make_modal_search_box, prepare_modal_dialog


class _PvzSyncWorker(QThread):
    ready = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(
        self,
        svc: OzonPvzReturnsService,
        client: OzonFbsClient,
        source_id: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_PvzSyncWorker, self).__init__(parent)
        self.svc = svc
        self.client = client
        self.source_id = source_id

    def run(self) -> None:
        try:
            result = self.svc.sync_source(self.client, self.source_id)
            self.ready.emit(result if isinstance(result, dict) else {})
        except Exception as exc:
            self.failed.emit(str(exc))


class OzonPvzReturnsDialog(QDialog):
    def __init__(
        self,
        db: Database,
        source: Dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> None:
        super(OzonPvzReturnsDialog, self).__init__(parent)
        self.db = db
        self.source = source
        self.source_id = int(source["id"])
        self.client = OzonFbsClient(
            str(source.get("client_id") or ""), str(source.get("api_key") or "")
        )
        self.svc = OzonPvzReturnsService(db)
        self._worker = None  # type: Optional[_PvzSyncWorker]
        self._rows = []  # type: List[Dict[str, Any]]

        self.setWindowTitle(
            "Возвраты ПВЗ · {}".format(source.get("name") or self.source_id)
        )
        prepare_modal_dialog(
            self, maximized=True, default_size=(980, 680), minimum_size=(720, 480)
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        title = QLabel("Возвраты ПВЗ (ReceivedBySeller)")
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        lead = QLabel(
            "Синхронизация тянет 31 день статусов FBS с Ozon. "
            "Поиск фильтрует локальный кэш."
        )
        lead.setObjectName("hint")
        lead.setWordWrap(True)
        root.addWidget(lead)

        bar = QHBoxLayout()
        self.sync_btn = QPushButton("Синхронизировать")
        self.sync_btn.clicked.connect(self.run_sync)
        bar.addWidget(self.sync_btn)
        search_box, self.search_input = make_modal_search_box(
            placeholder="Поиск…",
            tooltip="Отправление, заказ, артикул, SKU, ШК",
            min_width=220,
            max_width=320,
        )
        self.search_input.textChanged.connect(self.reload_table)
        bar.addWidget(search_box)
        bar.addStretch(1)
        root.addLayout(bar)

        self.info = QLabel("")
        self.info.setObjectName("hint")
        root.addWidget(self.info)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Дата", "Отправление", "Товар", "Арт.", "Кол-во", "ШК"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        self.reload_table()

    def reload_table(self) -> None:
        result = self.svc.list_local(
            self.source_id, search=self.search_input.text()
        )
        self._rows = list(result.get("items") or [])
        self.table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            dt = str(row.get("completed_dt") or "")[:16].replace("T", " ")
            self.table.setItem(i, 0, QTableWidgetItem(dt or "—"))
            self.table.setItem(
                i, 1, QTableWidgetItem(str(row.get("posting_number") or "—"))
            )
            self.table.setItem(
                i, 2, QTableWidgetItem(str(row.get("product_name") or "—"))
            )
            self.table.setItem(
                i, 3, QTableWidgetItem(str(row.get("offer_id") or "—"))
            )
            self.table.setItem(
                i, 4, QTableWidgetItem(str(row.get("quantity") or 1))
            )
            self.table.setItem(
                i, 5, QTableWidgetItem(str(row.get("barcode") or "—"))
            )
        self.info.setText(
            "Записей: {} · {} — {}".format(
                len(self._rows),
                result.get("date_from") or "—",
                result.get("date_to") or "—",
            )
        )

    def run_sync(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self.sync_btn.setEnabled(False)
        self.info.setText("Синхронизация возвратов Ozon…")
        worker = _PvzSyncWorker(self.svc, self.client, self.source_id, self)
        self._worker = worker
        worker.ready.connect(self._on_sync_ready)
        worker.failed.connect(self._on_sync_failed)
        worker.finished.connect(lambda: self.sync_btn.setEnabled(True))
        worker.start()

    def _on_sync_ready(self, result: object) -> None:
        res = result if isinstance(result, dict) else {}
        self.info.setText(str(res.get("message") or "Готово"))
        self.reload_table()

    def _on_sync_failed(self, message: str) -> None:
        QMessageBox.warning(self, "Возвраты ПВЗ", str(message or "Ошибка"))
