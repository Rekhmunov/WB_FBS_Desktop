# -*- coding: utf-8 -*-
"""Ozon FBS carriage detail — parity with WB SupplyDetailDialog (phase 2)."""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtCore import QUrl
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
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
from app.ozon.client import OzonFbsClient
from app.ozon import carriage_status_label
from app.services.ozon_mark_pick import OzonMarkService, OzonPickService
from app.services.ozon_orders import OzonOrdersService
from app.ui.dialog_utils import (
    fullscreen_parent,
    init_fullscreen_dialog,
    make_modal_search_box,
)
from app.ui.format_helpers import build_product_cell_widget


class _CarriageLoadWorker(QThread):
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        orders: OzonOrdersService,
        source_id: int,
        client: OzonFbsClient,
        carriage_id: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_CarriageLoadWorker, self).__init__(parent)
        self.orders = orders
        self.source_id = source_id
        self.client = client
        self.carriage_id = carriage_id

    def run(self) -> None:
        try:
            self.orders.refresh_carriage(
                self.source_id, self.client, self.carriage_id
            )
            carriage = self.orders.get_carriage(self.source_id, self.carriage_id)
            rows = self.orders.postings_in_carriage(
                self.source_id, self.carriage_id
            )
            self.ready.emit({"carriage": carriage, "rows": rows})
        except Exception as exc:
            self.failed.emit(str(exc))


class _LabelPrintWorker(QThread):
    ready = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        client: OzonFbsClient,
        posting_numbers: List[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_LabelPrintWorker, self).__init__(parent)
        self.client = client
        self.posting_numbers = list(posting_numbers or [])

    def run(self) -> None:
        try:
            self.client.package_label_create(self.posting_numbers)
            data = self.client.package_label_fetch(self.posting_numbers)
            if not data:
                raise RuntimeError("Ozon не вернул файл этикеток")
            suffix = ".pdf" if data[:4] == b"%PDF" else ".bin"
            fd, path = tempfile.mkstemp(suffix=suffix, prefix="ozon-label-")
            os.close(fd)
            with open(path, "wb") as fh:
                fh.write(data)
            self.ready.emit(path)
        except Exception as exc:
            self.failed.emit(str(exc))


class OzonCarriageDetailDialog(QDialog):
    def __init__(
        self,
        db: Database,
        orders: OzonOrdersService,
        source: Dict[str, Any],
        carriage_id: str,
        parent: Optional[QWidget] = None,
        *,
        fullscreen: bool = True,
    ) -> None:
        super(OzonCarriageDetailDialog, self).__init__(
            fullscreen_parent(parent, fullscreen)
        )
        self.db = db
        self.orders = orders
        self.source = source
        self.carriage_id = str(carriage_id or "").strip()
        self.source_id = int(source["id"])
        self.client_id = str(source.get("client_id") or "")
        self.api_key = str(source.get("api_key") or "")
        self.client = OzonFbsClient(self.client_id, self.api_key)
        self.mark = OzonMarkService(db)
        self.pick = OzonPickService(db)
        self._rows = []  # type: List[Dict[str, Any]]
        self._carriage = None  # type: Optional[Dict[str, Any]]
        self._load_worker = None  # type: Optional[_CarriageLoadWorker]
        self._label_worker = None  # type: Optional[_LabelPrintWorker]
        self.carriage_mutated = False

        self.setWindowTitle("Отгрузка Ozon {}".format(self.carriage_id))
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

        title_row = QHBoxLayout()
        self.title_label = QLabel("Отгрузка")
        self.title_label.setObjectName("sdTitle")
        title_row.addWidget(self.title_label)
        self.status_chip = QLabel("")
        self.status_chip.setObjectName("hint")
        title_row.addWidget(self.status_chip)
        title_row.addStretch(1)
        hv.addLayout(title_row)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.btn_mark = QPushButton("Маркировка")
        self.btn_mark.clicked.connect(self.open_mark)
        self.btn_pick = QPushButton("Проверка ШК")
        self.btn_pick.setObjectName("secondary")
        self.btn_pick.clicked.connect(self.open_pick)
        self.btn_labels = QPushButton("Этикетки")
        self.btn_labels.setObjectName("secondary")
        self.btn_labels.clicked.connect(self.print_labels)
        self.btn_approve = QPushButton("Подтвердить отгрузку")
        self.btn_approve.clicked.connect(self.approve_carriage)
        self.btn_refresh = QPushButton("Обновить")
        self.btn_refresh.setObjectName("secondary")
        self.btn_refresh.clicked.connect(self.reload)
        for b in (
            self.btn_mark,
            self.btn_pick,
            self.btn_labels,
            self.btn_approve,
            self.btn_refresh,
        ):
            actions.addWidget(b)
        actions.addStretch(1)
        search_box, self.search_input = make_modal_search_box()
        self.search_input.textChanged.connect(self._apply_filter)
        actions.addWidget(search_box)
        hv.addLayout(actions)
        root.addWidget(header)

        self.load_info = QLabel("Загрузка…")
        self.load_info.setObjectName("syncInfoText")
        self.load_info.setContentsMargins(24, 8, 24, 8)
        root.addWidget(self.load_info)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Отправление", "Товар", "Маркировка", "ШК"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        root.addWidget(self.table, 1)

        self._set_actions_ready(False)
        self._begin_load()

    def _set_actions_ready(self, ready: bool) -> None:
        for b in (
            self.btn_mark,
            self.btn_pick,
            self.btn_labels,
            self.btn_approve,
            self.btn_refresh,
        ):
            b.setEnabled(bool(ready))

    def _begin_load(self) -> None:
        if self._load_worker and self._load_worker.isRunning():
            return
        self.load_info.setText("Загрузка отгрузки из Ozon API…")
        worker = _CarriageLoadWorker(
            self.orders, self.source_id, self.client, self.carriage_id, self
        )
        self._load_worker = worker
        worker.ready.connect(self._on_loaded)
        worker.failed.connect(self._on_load_failed)
        worker.finished.connect(lambda: setattr(self, "_load_worker", None))
        worker.start()

    def reload(self) -> None:
        self._begin_load()

    def _on_load_failed(self, message: str) -> None:
        self.load_info.setText(str(message or "Ошибка загрузки"))
        self.load_info.setProperty("state", "error")
        self.load_info.style().unpolish(self.load_info)
        self.load_info.style().polish(self.load_info)

    def _on_loaded(self, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        self._carriage = data.get("carriage") if isinstance(data.get("carriage"), dict) else {}
        self._rows = list(data.get("rows") or [])
        self._render_header()
        self._apply_filter()
        self.load_info.setText(
            "Отправлений в отгрузке: {}.".format(len(self._rows))
        )
        self.load_info.setProperty("state", "ok")
        self.load_info.style().unpolish(self.load_info)
        self.load_info.style().polish(self.load_info)
        self._set_actions_ready(True)

    def _render_header(self) -> None:
        c = self._carriage or {}
        self.title_label.setText("Отгрузка {}".format(self.carriage_id))
        st = carriage_status_label(str(c.get("status") or ""))
        cnt = int(c.get("posting_count") or len(self._rows))
        self.status_chip.setText("{} · отправлений: {}".format(st, cnt))

    def _apply_filter(self) -> None:
        q = self.search_input.text().strip().lower()
        rows = self._rows
        if q:
            rows = [
                r
                for r in self._rows
                if q in str(r.get("posting_number") or "").lower()
                or q in str(r.get("offer_id") or "").lower()
                or q in str(r.get("sku") or "").lower()
                or q in str(r.get("product_name_display") or "").lower()
            ]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(
                i, 0, QTableWidgetItem(str(r.get("posting_number") or ""))
            )
            self.table.setCellWidget(
                i,
                1,
                build_product_cell_widget(r, photo_size=72),
            )
            marks = []
            try:
                marks = json.loads(str(r.get("marks_json") or "[]"))
            except Exception:
                marks = []
            mark_text = "✓" if marks and int(r.get("marks_synced") or 0) else (
                "…" if marks else "—"
            )
            self.table.setItem(i, 2, QTableWidgetItem(mark_text))
            pick_ok = bool(int(r.get("pick_verified") or 0))
            self.table.setItem(
                i, 3, QTableWidgetItem("✓" if pick_ok else "—")
            )
            self.table.setRowHeight(i, 96)

    def open_mark(self) -> None:
        from app.ui.ozon_mark_dialog import OzonMarkDialog

        dlg = OzonMarkDialog(
            self.mark,
            self.client,
            self.source_id,
            self.carriage_id,
            self,
            fullscreen=True,
        )
        dlg.exec_()
        if dlg.data_changed:
            self.carriage_mutated = True
            self.reload()

    def open_pick(self) -> None:
        from app.ui.ozon_pick_dialog import OzonPickDialog

        dlg = OzonPickDialog(
            self.pick,
            self.source_id,
            self.carriage_id,
            self,
            fullscreen=True,
        )
        dlg.exec_()
        if dlg.data_changed:
            self.carriage_mutated = True
            self.reload()

    def print_labels(self) -> None:
        pnums = [str(r.get("posting_number") or "") for r in self._rows]
        pnums = [p for p in pnums if p]
        if not pnums:
            QMessageBox.information(self, "Этикетки", "Нет отправлений в отгрузке.")
            return
        if self._label_worker and self._label_worker.isRunning():
            return
        self.btn_labels.setEnabled(False)
        self.load_info.setText("Формирование этикеток Ozon…")
        worker = _LabelPrintWorker(self.client, pnums, self)
        self._label_worker = worker
        worker.ready.connect(self._on_labels_ready)
        worker.failed.connect(self._on_labels_failed)
        worker.finished.connect(lambda: self.btn_labels.setEnabled(True))
        worker.start()

    def _on_labels_ready(self, path: str) -> None:
        self.load_info.setText("Этикетки сохранены: {}".format(path))
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_labels_failed(self, message: str) -> None:
        QMessageBox.warning(self, "Этикетки", str(message or "Ошибка"))

    def approve_carriage(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Подтвердить",
                "Подтвердить отгрузку {} в Ozon?".format(self.carriage_id),
            )
            != QMessageBox.Yes
        ):
            return
        try:
            self.orders.approve_carriage(
                self.source_id, self.client, self.carriage_id
            )
            self.carriage_mutated = True
            QMessageBox.information(self, "Отгрузка", "Отгрузка подтверждена.")
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Отгрузка", str(exc))
