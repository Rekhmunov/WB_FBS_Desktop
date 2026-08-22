# -*- coding: utf-8 -*-
"""Ozon FBS pick verify modal (ШК without exemplar)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

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
)

from app.services.ozon_mark_pick import OzonPickService
from app.ui.dialog_utils import (
    block_ru_layout_scan,
    fullscreen_parent,
    init_fullscreen_dialog,
    install_live_ru_layout_guard,
    make_modal_search_box,
)
from app.ui.format_helpers import build_product_cell_widget


class OzonPickDialog(QDialog):
    def __init__(
        self,
        pick: OzonPickService,
        source_id: int,
        carriage_id: str,
        parent=None,
        *,
        fullscreen: bool = True,
    ) -> None:
        super(OzonPickDialog, self).__init__(fullscreen_parent(parent, fullscreen))
        self.pick = pick
        self.source_id = source_id
        self.carriage_id = str(carriage_id or "")
        self.rows = []  # type: List[Dict[str, Any]]
        self._pending_pnum = None  # type: Optional[str]
        self.data_changed = False

        self.setObjectName("kizModal")
        self.setWindowTitle("Проверка ШК · отгрузка {}".format(carriage_id))
        init_fullscreen_dialog(
            self,
            fullscreen=fullscreen,
            default_size=(1100, 760),
            minimum_size=(900, 640),
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        header = QFrame()
        header.setObjectName("kizHeader")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(24, 20, 24, 12)
        title = QLabel("Проверка штрихкода товара")
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
            "ШК отправления (upper/lower), затем ШК товара"
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
        self.table.setHorizontalHeaderLabels(["Отправление", "Товар", "ШК"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)

        self.load_rows()

    def load_rows(self) -> None:
        self.rows = self.pick.rows(self.source_id, self.carriage_id)
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
        for i, r in enumerate(visible):
            self.table.setItem(
                i, 0, QTableWidgetItem(str(r.get("posting_number") or ""))
            )
            self.table.setCellWidget(i, 1, build_product_cell_widget(r, photo_size=72))
            self.table.setItem(
                i,
                2,
                QTableWidgetItem("✓" if r.get("pick_verified") else "—"),
            )
            self.table.setRowHeight(i, 96)

    def _update_counter(self) -> None:
        total = len(self.rows)
        ok = sum(1 for r in self.rows if r.get("pick_verified"))
        self.counter.setText("Проверено {} из {}".format(ok, total))

    def on_scan(self) -> None:
        if block_ru_layout_scan(self.scan_input):
            return
        raw = self.scan_input.text().strip()
        self.scan_input.clear()
        if not raw:
            return
        if self._pending_pnum:
            row = next(
                (
                    r
                    for r in self.rows
                    if r.get("posting_number") == self._pending_pnum
                ),
                None,
            )
            if not row:
                self._pending_pnum = None
                return
            ok, err = self.pick.validate_barcode(raw, row.get("skus") or [])
            if not ok:
                QMessageBox.warning(self, "Проверка ШК", err)
                return
            self.pick.save(
                self.source_id,
                str(row.get("posting_number") or ""),
                True,
                raw,
            )
            row["pick_verified"] = True
            row["pick_barcode"] = raw
            self._pending_pnum = None
            self.prompt.setText("")
            self.data_changed = True
            self._render_table()
            self._update_counter()
            return
        for r in self.rows:
            pnum = str(r.get("posting_number") or "")
            barcodes = r.get("barcodes") or r.get("skus") or []
            if raw == pnum or raw in barcodes:
                self._pending_pnum = pnum
                self.prompt.setText(
                    "Отправление {} — отсканируйте ШК товара".format(pnum)
                )
                return
        QMessageBox.information(
            self,
            "Проверка ШК",
            "Сначала отсканируйте ШК отправления (из этикетки Ozon).",
        )
