# -*- coding: utf-8 -*-
"""Ozon FBS marking modal (exemplar / Честный знак)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
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
)

from app.ozon.client import OzonFbsClient
from app.services.ozon_mark_pick import OzonMarkService
from app.ui.dialog_utils import (
    block_ru_layout_scan,
    fullscreen_parent,
    init_fullscreen_dialog,
    install_live_ru_layout_guard,
    make_modal_search_box,
)
from app.ui.format_helpers import build_product_cell_widget


class _OzonMarkSaveWorker(QThread):
    done = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(
        self,
        mark: OzonMarkService,
        client: OzonFbsClient,
        source_id: int,
        jobs: List[Dict[str, Any]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_OzonMarkSaveWorker, self).__init__(parent)
        self.mark = mark
        self.client = client
        self.source_id = source_id
        self.jobs = list(jobs or [])

    def run(self) -> None:
        try:
            for job in self.jobs:
                self.mark.save_to_ozon(
                    self.client,
                    self.source_id,
                    str(job.get("posting_number") or ""),
                    list(job.get("marks") or []),
                )
            self.done.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class OzonMarkDialog(QDialog):
    def __init__(
        self,
        mark: OzonMarkService,
        client: OzonFbsClient,
        source_id: int,
        carriage_id: str,
        parent: Optional[QWidget] = None,
        *,
        fullscreen: bool = True,
        posting_number: str = "",
    ) -> None:
        super(OzonMarkDialog, self).__init__(fullscreen_parent(parent, fullscreen))
        self.mark = mark
        self.client = client
        self.source_id = source_id
        self.carriage_id = str(carriage_id or "")
        self.posting_number = str(posting_number or "").strip()
        self.rows = []  # type: List[Dict[str, Any]]
        self._pending_pnum = None  # type: Optional[str]
        self._inputs = {}  # type: Dict[str, QLineEdit]
        self.data_changed = False
        self._save_worker = None  # type: Optional[_OzonMarkSaveWorker]

        self.setObjectName("kizModal")
        self.setWindowTitle(
            "Маркировка · {}".format(
                self.posting_number or "отгрузка {}".format(carriage_id)
            )
        )
        init_fullscreen_dialog(
            self,
            fullscreen=fullscreen,
            default_size=(1100, 760),
            minimum_size=(900, 640),
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("kizHeader")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(24, 20, 24, 12)
        title = QLabel("Маркировка (Ozon exemplar)")
        title.setObjectName("kizTitle")
        hl.addWidget(title)
        root.addWidget(header)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(24, 8, 24, 8)
        self.counter = QLabel("0 / 0")
        self.counter.setObjectName("kizScanCount")
        toolbar.addWidget(self.counter)
        toolbar.addStretch(1)
        search_box, self.search_input = make_modal_search_box()
        self.search_input.textChanged.connect(self._render_table)
        toolbar.addWidget(search_box)
        refresh = QPushButton("↻ Статус Ozon")
        refresh.setObjectName("secondary")
        refresh.clicked.connect(self.refresh_live)
        toolbar.addWidget(refresh)
        save = QPushButton("Сохранить в Ozon")
        save.setObjectName("primaryButton")
        save.clicked.connect(self.save_all)
        toolbar.addWidget(save)
        root.addLayout(toolbar)

        scan = QFrame()
        scan.setObjectName("kizScanBar")
        sl = QHBoxLayout(scan)
        sl.setContentsMargins(24, 12, 24, 12)
        lab = QLabel("Сканирование")
        lab.setObjectName("kizScanLabel")
        self.scan_input = QLineEdit()
        self.scan_input.setObjectName("kizScanInput")
        self.scan_input.setPlaceholderText(
            "Сканируйте номер отправления или код маркировки"
        )
        self.scan_input.returnPressed.connect(self.on_scan)
        install_live_ru_layout_guard(self.scan_input, self)
        sl.addWidget(lab)
        sl.addWidget(self.scan_input, 1)
        root.addWidget(scan)

        self.prompt = QLabel("")
        self.prompt.setObjectName("kizScanPrompt")
        self.prompt.setContentsMargins(24, 0, 24, 8)
        root.addWidget(self.prompt)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Отправление", "Товар", "Код маркировки"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)

        self.load_rows()

    def load_rows(self) -> None:
        if self.posting_number:
            row = self.mark.single_marking_row(
                self.source_id, self.posting_number, client=self.client
            )
            self.rows = [row] if row else []
        else:
            self.rows = self.mark.marking_rows(
                self.source_id, self.carriage_id, client=self.client
            )
        self._render_table()
        self._update_counter()

    def refresh_live(self) -> None:
        changed = False
        for r in self.rows:
            pnum = str(r.get("posting_number") or "")
            if not pnum:
                continue
            live = self.mark.refresh_mark_status(self.client, self.source_id, pnum)
            if live.get("marks"):
                r["marks"] = live.get("marks") or []
                r["marks_synced"] = bool(live.get("marks_synced"))
                changed = True
        if changed:
            self.data_changed = True
        self._render_table()
        self._update_counter()

    def _render_table(self) -> None:
        q = self.search_input.text().strip().lower()
        visible = self.rows
        if q:
            visible = [
                r
                for r in self.rows
                if q in str(r.get("posting_number") or "").lower()
                or q in str(r.get("product_name") or "").lower()
            ]
        self.table.setRowCount(len(visible))
        self._inputs = {}
        for i, r in enumerate(visible):
            pnum = str(r.get("posting_number") or "")
            self.table.setItem(i, 0, QTableWidgetItem(pnum))
            self.table.setCellWidget(i, 1, build_product_cell_widget(r, photo_size=72))
            edit = QLineEdit()
            edit.setText((r.get("marks") or [""])[0] if r.get("marks") else "")
            edit.textChanged.connect(
                lambda text, pn=pnum: self._on_edit(pn, text)
            )
            self._inputs[pnum] = edit
            self.table.setCellWidget(i, 2, edit)
            self.table.setRowHeight(i, 96)

    def _on_edit(self, posting_number: str, text: str) -> None:
        for r in self.rows:
            if str(r.get("posting_number") or "") == posting_number:
                r["marks"] = [text.strip()] if text.strip() else []
                break
        self._update_counter()

    def _update_counter(self) -> None:
        total = len(self.rows)
        ok = sum(1 for r in self.rows if (r.get("marks") or []) and r.get("marks_synced"))
        filled = sum(1 for r in self.rows if r.get("marks"))
        self.counter.setText("Заполнено {} · синхр. {} из {}".format(filled, ok, total))

    def on_scan(self) -> None:
        if block_ru_layout_scan(self.scan_input):
            return
        raw = self.scan_input.text().strip()
        self.scan_input.clear()
        if not raw:
            return
        if self._pending_pnum:
            pnum = self._pending_pnum
            row = next(
                (r for r in self.rows if r.get("posting_number") == pnum), None
            )
            if not row:
                self._pending_pnum = None
                return
            ok, err = self.mark.validate_mark(
                raw, row.get("skus") or [], bool(row.get("skip_gtin_check"))
            )
            if not ok:
                QMessageBox.warning(self, "Маркировка", err)
                return
            row["marks"] = [raw]
            if pnum in self._inputs:
                self._inputs[pnum].setText(raw)
            self._pending_pnum = None
            self.prompt.setText("")
            self._update_counter()
            self.data_changed = True
            return
        for r in self.rows:
            if raw == str(r.get("posting_number") or ""):
                self._pending_pnum = raw
                self.prompt.setText(
                    "Отправление {} — отсканируйте код маркировки".format(raw)
                )
                return
        if len(raw) > 20:
            for r in self.rows:
                if not (r.get("marks") or []):
                    ok, err = self.mark.validate_mark(
                        raw, r.get("skus") or [], bool(r.get("skip_gtin_check"))
                    )
                    if ok:
                        r["marks"] = [raw]
                        pnum = str(r.get("posting_number") or "")
                        if pnum in self._inputs:
                            self._inputs[pnum].setText(raw)
                        self.data_changed = True
                        self._update_counter()
                        return
            QMessageBox.warning(self, "Маркировка", "Не найдено подходящее отправление")
            return
        QMessageBox.information(
            self,
            "Маркировка",
            "Сначала отсканируйте номер отправления, затем код маркировки.",
        )

    def save_all(self) -> None:
        jobs = [
            {"posting_number": r.get("posting_number"), "marks": r.get("marks") or []}
            for r in self.rows
            if r.get("marks")
        ]
        if not jobs:
            QMessageBox.information(self, "Маркировка", "Нет кодов для сохранения.")
            return
        if self._save_worker and self._save_worker.isRunning():
            return
        worker = _OzonMarkSaveWorker(
            self.mark, self.client, self.source_id, jobs, self
        )
        self._save_worker = worker
        worker.done.connect(self._on_saved)
        worker.failed.connect(lambda m: QMessageBox.warning(self, "Маркировка", m))
        worker.start()

    def _on_saved(self) -> None:
        self.data_changed = True
        self.load_rows()
        QMessageBox.information(self, "Маркировка", "Коды отправлены в Ozon.")
