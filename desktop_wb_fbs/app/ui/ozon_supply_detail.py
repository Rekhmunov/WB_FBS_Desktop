# -*- coding: utf-8 -*-
"""Ozon FBS local supply detail — web FeedPilot supply page for desktop."""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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
from app.services.ozon_labels import OzonLabelService
from app.services.ozon_mark_pick import OzonMarkService, OzonPickService
from app.services.ozon_supplies import OzonSuppliesService
from app.ui.dialog_utils import (
    fullscreen_parent,
    init_fullscreen_dialog,
    make_modal_search_box,
)
from app.ui.format_helpers import build_product_cell_widget


class _LabelPrintWorker(QThread):
    ready = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        labels: OzonLabelService,
        supplies: OzonSuppliesService,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_LabelPrintWorker, self).__init__(parent)
        self.labels = labels
        self.supplies = supplies
        self.client = client
        self.source_id = source_id
        self.posting_numbers = list(posting_numbers or [])

    def run(self) -> None:
        try:
            try:
                self.supplies.bind_stickers_from_postings(
                    self.client, self.source_id, self.posting_numbers
                )
            except Exception:
                pass
            path = self.labels.fetch_labels(
                self.client, self.source_id, self.posting_numbers
            )
            self.ready.emit(path)
        except Exception as exc:
            self.failed.emit(str(exc))


class OzonSupplyDetailDialog(QDialog):
    def __init__(
        self,
        db: Database,
        source: Dict[str, Any],
        supply_id: str,
        parent: Optional[QWidget] = None,
        *,
        fullscreen: bool = True,
        read_only: bool = False,
    ) -> None:
        super(OzonSupplyDetailDialog, self).__init__(
            fullscreen_parent(parent, fullscreen)
        )
        self.db = db
        self.source = source
        self.source_id = int(source["id"])
        self.supply_id = str(supply_id or "").strip()
        self.client = OzonFbsClient(
            str(source.get("client_id") or ""), str(source.get("api_key") or "")
        )
        self.supplies = OzonSuppliesService(db)
        self.mark = OzonMarkService(db)
        self.pick = OzonPickService(db)
        self.labels = OzonLabelService(db)
        self._detail = {}  # type: Dict[str, Any]
        self._rows = []  # type: List[Dict[str, Any]]
        self._label_worker = None  # type: Optional[_LabelPrintWorker]
        self.supply_mutated = False
        self._force_read_only = bool(read_only)

        self.setWindowTitle("Поставка Ozon {}".format(self.supply_id))
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
        header.setObjectName("sdHeader")
        hv = QVBoxLayout(header)
        hv.setContentsMargins(24, 12, 24, 10)
        hv.setSpacing(8)

        title_row = QHBoxLayout()
        self.title_label = QLabel("Поставка")
        self.title_label.setObjectName("sdTitle")
        title_row.addWidget(self.title_label)
        self.meta_chip = QLabel("")
        self.meta_chip.setObjectName("hint")
        title_row.addWidget(self.meta_chip)
        title_row.addStretch(1)
        hv.addLayout(title_row)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.btn_rename = QPushButton("Переименовать")
        self.btn_rename.setObjectName("secondary")
        self.btn_rename.clicked.connect(self.rename_supply)
        self.btn_mark = QPushButton("Товары с КИЗ")
        self.btn_mark.clicked.connect(self.open_mark)
        self.btn_pick = QPushButton("Товары без КИЗ")
        self.btn_pick.setObjectName("secondary")
        self.btn_pick.clicked.connect(self.open_pick)
        self.btn_labels = QPushButton("Этикетки")
        self.btn_labels.setObjectName("secondary")
        self.btn_labels.clicked.connect(self.print_labels)
        self.btn_stickers = QPushButton("Стикеры / QR")
        self.btn_stickers.setObjectName("secondary")
        self.btn_stickers.clicked.connect(self.print_stickers_sheet)
        self.btn_picking = QPushButton("Лист подбора")
        self.btn_picking.setObjectName("secondary")
        self.btn_picking.clicked.connect(self.print_picking_list)
        self.btn_containers = QPushButton("Грузоместа")
        self.btn_containers.setObjectName("secondary")
        self.btn_containers.clicked.connect(self.open_containers)
        self.btn_cancelled = QPushButton("Отменённые")
        self.btn_cancelled.setObjectName("secondary")
        self.btn_cancelled.clicked.connect(self.show_cancelled)
        self.btn_move = QPushButton("Перенести в доставку")
        self.btn_move.clicked.connect(self.move_to_delivering)
        self.btn_refresh = QPushButton("Обновить")
        self.btn_refresh.setObjectName("secondary")
        self.btn_refresh.clicked.connect(self.reload)
        for b in (
            self.btn_rename,
            self.btn_mark,
            self.btn_pick,
            self.btn_labels,
            self.btn_stickers,
            self.btn_picking,
            self.btn_containers,
            self.btn_cancelled,
            self.btn_move,
            self.btn_refresh,
        ):
            actions.addWidget(b)
        actions.addStretch(1)
        search_box, self.search_input = make_modal_search_box()
        self.search_input.textChanged.connect(self._apply_filter)
        actions.addWidget(search_box)
        hv.addLayout(actions)
        root.addWidget(header)

        self.load_info = QLabel("")
        self.load_info.setObjectName("syncInfoText")
        self.load_info.setContentsMargins(24, 8, 24, 8)
        root.addWidget(self.load_info)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Отправление", "Товар", "Маркировка", "ШК", "Стикер"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(self._open_selected_posting)
        root.addWidget(self.table, 1)

        self.reload()

    def _is_read_only(self) -> bool:
        return self._force_read_only or bool(self._detail.get("read_only"))

    def _set_actions_ready(self) -> None:
        ro = self._is_read_only()
        self.btn_rename.setEnabled(not ro)
        self.btn_mark.setEnabled(True)
        self.btn_pick.setEnabled(True)
        self.btn_labels.setEnabled(True)
        self.btn_stickers.setEnabled(True)
        self.btn_picking.setEnabled(True)
        self.btn_containers.setEnabled(True)
        self.btn_cancelled.setEnabled(True)
        self.btn_move.setEnabled(not ro)
        self.btn_move.setVisible(not ro)

    def reload(self) -> None:
        try:
            self._detail = self.supplies.get_detail(
                self.source_id,
                self.supply_id,
                read_only=self._force_read_only,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Поставка", str(exc))
            return
        self._rows = list(self._detail.get("orders") or [])
        name = str(self._detail.get("name") or self.supply_id)
        self.title_label.setText(name)
        self.setWindowTitle(name)
        self.meta_chip.setText(
            "{} · {} отпр. · {}".format(
                self.supply_id,
                int(self._detail.get("order_count") or 0),
                self._detail.get("warehouse_label") or "склад —",
            )
        )
        self._set_actions_ready()
        self._apply_filter()
        self.load_info.setText(
            "Только просмотр"
            if self._is_read_only()
            else "Готово · {} отправлений".format(len(self._rows))
        )

    def _apply_filter(self) -> None:
        q = self.search_input.text().strip().lower()
        rows = self._rows
        if q:
            rows = [
                r
                for r in self._rows
                if q in str(r.get("posting_number") or "").lower()
                or q in str(r.get("offer_id") or "").lower()
                or q in str(r.get("product_name") or "").lower()
                or q in str(r.get("sku") or "").lower()
            ]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            pnum = str(r.get("posting_number") or "")
            item = QTableWidgetItem(pnum)
            item.setData(Qt.UserRole, pnum)
            self.table.setItem(i, 0, item)
            self.table.setCellWidget(i, 1, build_product_cell_widget(r, photo_size=72))
            marks = []
            try:
                marks = json.loads(str(r.get("marks_json") or "[]"))
            except Exception:
                marks = []
            if not isinstance(marks, list):
                marks = []
            mark_text = (
                "✓"
                if marks and int(r.get("marks_synced") or 0)
                else ("…" if marks else "—")
            )
            if str(r.get("tab") or "") == "cancelled" or str(
                r.get("status") or ""
            ).lower() in ("cancelled", "not_accepted"):
                mark_text = "отм."
            self.table.setItem(i, 2, QTableWidgetItem(mark_text))
            pick_ok = bool(int(r.get("pick_verified") or 0))
            self.table.setItem(i, 3, QTableWidgetItem("✓" if pick_ok else "—"))
            sticker = str(
                r.get("sticker_barcode") or r.get("sticker_lower_barcode") or ""
            ).strip()
            if not sticker:
                try:
                    bcs = r.get("barcodes") or []
                    if isinstance(bcs, list) and bcs:
                        sticker = str(bcs[0] or "")
                except Exception:
                    sticker = ""
            self.table.setItem(
                i, 4, QTableWidgetItem(sticker[:18] + ("…" if len(sticker) > 18 else ""))
            )
            self.table.setRowHeight(i, 96)

    def _posting_numbers(self) -> List[str]:
        return [
            str(r.get("posting_number") or "").strip()
            for r in self._rows
            if str(r.get("posting_number") or "").strip()
            and str(r.get("tab") or "").lower() != "cancelled"
            and str(r.get("status") or "").lower()
            not in ("cancelled", "not_accepted")
        ]

    def _selected_posting_number(self) -> str:
        rows = self.table.selectionModel().selectedRows() if self.table else []
        if not rows:
            return ""
        item = self.table.item(rows[0].row(), 0)
        return str(item.data(Qt.UserRole) or item.text() if item else "").strip()

    def _open_selected_posting(self) -> None:
        from app.services.ozon_orders import OzonOrdersService
        from app.ui.ozon_posting_detail import OzonPostingDetailDialog

        pnum = self._selected_posting_number()
        if not pnum:
            return
        dlg = OzonPostingDetailDialog(
            self.db,
            OzonOrdersService(self.db),
            self.source,
            pnum,
            self,
            fullscreen=True,
        )
        dlg.exec_()
        if getattr(dlg, "posting_mutated", False):
            self.supply_mutated = True
            self.reload()

    def rename_supply(self) -> None:
        if self._is_read_only():
            return
        current = str(self._detail.get("name") or "")
        text, ok = QInputDialog.getText(
            self, "Переименовать", "Название поставки:", text=current
        )
        if not ok:
            return
        try:
            self.supplies.rename_supply(self.source_id, self.supply_id, text)
        except Exception as exc:
            QMessageBox.warning(self, "Поставка", str(exc))
            return
        self.supply_mutated = True
        self.reload()

    def open_mark(self) -> None:
        from app.ui.ozon_mark_dialog import OzonMarkDialog

        dlg = OzonMarkDialog(
            self.mark,
            self.client,
            self.source_id,
            "",
            self,
            fullscreen=True,
            posting_numbers=self._posting_numbers(),
            title_hint=str(self._detail.get("name") or self.supply_id),
        )
        dlg.exec_()
        if dlg.data_changed:
            self.supply_mutated = True
            self.reload()

    def open_pick(self) -> None:
        from app.ui.ozon_pick_dialog import OzonPickDialog

        dlg = OzonPickDialog(
            self.pick,
            self.source_id,
            "",
            self,
            fullscreen=True,
            posting_numbers=self._posting_numbers(),
            title_hint=str(self._detail.get("name") or self.supply_id),
        )
        dlg.exec_()
        if dlg.data_changed:
            self.supply_mutated = True
            self.reload()

    def print_labels(self) -> None:
        pnums = self._posting_numbers()
        if not pnums:
            QMessageBox.information(self, "Этикетки", "Нет отправлений.")
            return
        if self._label_worker and self._label_worker.isRunning():
            return
        self.btn_labels.setEnabled(False)
        self.load_info.setText("Формирование этикеток Ozon…")
        worker = _LabelPrintWorker(
            self.labels, self.supplies, self.client, self.source_id, pnums, self
        )
        self._label_worker = worker
        worker.ready.connect(self._on_labels_ready)
        worker.failed.connect(self._on_labels_failed)
        worker.finished.connect(lambda: self.btn_labels.setEnabled(True))
        worker.start()

    def _on_labels_ready(self, path: str) -> None:
        self.load_info.setText("Этикетки: {}".format(path))
        self.reload()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_labels_failed(self, message: str) -> None:
        QMessageBox.warning(self, "Этикетки", str(message or "Ошибка"))

    def print_picking_list(self) -> None:
        html = self.supplies.render_picking_list_html(self._detail)
        fd, path = tempfile.mkstemp(suffix=".html", prefix="ozon-picking-")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        self.load_info.setText("Лист подбора: {}".format(path))
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_containers(self) -> None:
        from app.ui.ozon_containers_dialog import OzonContainersDialog

        wh = 0
        try:
            wh = int(self._detail.get("warehouse_id") or 0)
        except (TypeError, ValueError):
            wh = 0
        if wh <= 0:
            for r in self._rows:
                try:
                    wh = int(r.get("warehouse_id") or 0)
                except (TypeError, ValueError):
                    wh = 0
                if wh > 0:
                    break
        if wh <= 0:
            QMessageBox.information(
                self,
                "Грузоместа",
                "Не удалось определить склад поставки. Синхронизируйте и повторите.",
            )
            return
        dlg = OzonContainersDialog(
            self.client,
            "supply:{}".format(self.supply_id),
            source_id=self.source_id,
            posting_numbers=self._posting_numbers(),
            selected_posting=self._selected_posting_number(),
            warehouse_id=wh,
            parent=self,
        )
        dlg.exec_()

    def print_stickers_sheet(self) -> None:
        pnums = self._posting_numbers()
        if not pnums:
            QMessageBox.information(self, "Стикеры", "Нет отправлений.")
            return
        self.load_info.setText("Привязка QR и подготовка листа стикеров…")
        try:
            self.supplies.bind_stickers_from_postings(
                self.client, self.source_id, pnums
            )
        except Exception:
            pass
        self.reload()
        html = self.supplies.render_stickers_sheet_html(self._detail)
        fd, path = tempfile.mkstemp(suffix=".html", prefix="ozon-stickers-")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        self.load_info.setText("Стикеры / QR: {}".format(path))
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def show_cancelled(self) -> None:
        from app.ui.dialog_utils import prepare_modal_dialog

        cancelled = self.supplies.list_cancelled_in_supply(
            self.source_id, self.supply_id
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("Отменённые · {}".format(self.supply_id))
        prepare_modal_dialog(
            dlg,
            maximized=True,
            default_size=(860, 600),
            minimum_size=(640, 440),
        )
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        title = QLabel("Отменённые отправления")
        title.setObjectName("dialogTitle")
        lay.addWidget(title)
        subtitle = QLabel(
            "Отправления отменены, но всё ещё привязаны к этой поставке. "
            "«Обновить» перечитает локальные статусы после синхронизации."
        )
        subtitle.setObjectName("hint")
        subtitle.setWordWrap(True)
        lay.addWidget(subtitle)
        head = QHBoxLayout()
        lead = QLabel("Найдено отменённых: {}".format(len(cancelled)))
        lead.setObjectName("hint")
        head.addWidget(lead, 1)
        refresh_btn = QPushButton("Обновить")
        refresh_btn.setObjectName("secondary")
        head.addWidget(refresh_btn)
        lay.addLayout(head)
        table = QTableWidget(0, 3)
        table.setObjectName("kizTable")
        table.setAlternatingRowColors(True)
        table.setHorizontalHeaderLabels(["Отправление", "Товар", "Причина / статус"])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setShowGrid(False)
        hdr = table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.verticalHeader().setVisible(False)

        def _fill(items: List[Dict[str, Any]]) -> None:
            lead.setText("Найдено отменённых: {}".format(len(items)))
            if not items:
                table.setRowCount(1)
                table.setSpan(0, 0, 1, 3)
                empty = QTableWidgetItem("Отменённых отправлений в поставке нет")
                empty.setTextAlignment(Qt.AlignCenter)
                empty.setFlags(Qt.ItemIsEnabled)
                table.setItem(0, 0, empty)
                return
            table.clearSpans()
            table.setRowCount(len(items))
            for i, r in enumerate(items):
                table.setItem(
                    i, 0, QTableWidgetItem(str(r.get("posting_number") or ""))
                )
                table.setCellWidget(
                    i, 1, build_product_cell_widget(r, photo_size=64)
                )
                reason = (
                    str(r.get("cancel_reason") or "").strip()
                    or str(r.get("status_label") or r.get("status") or "отменено")
                )
                table.setItem(i, 2, QTableWidgetItem(reason))
                table.setRowHeight(i, 88)

        def _refresh() -> None:
            self.reload()
            items = self.supplies.list_cancelled_in_supply(
                self.source_id, self.supply_id
            )
            _fill(items)

        refresh_btn.clicked.connect(_refresh)
        _fill(cancelled)
        lay.addWidget(table, 1)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(dlg.accept)
        lay.addWidget(close_btn, 0, Qt.AlignRight)
        dlg.exec_()

    def move_to_delivering(self) -> None:
        if self._is_read_only():
            return
        if (
            QMessageBox.question(
                self,
                "Доставка",
                "Перенести поставку «{}» во вкладку «Доставляются»?".format(
                    self._detail.get("name") or self.supply_id
                ),
            )
            != QMessageBox.Yes
        ):
            return
        try:
            self.supplies.mark_delivering(self.source_id, self.supply_id)
        except Exception as exc:
            QMessageBox.warning(self, "Доставка", str(exc))
            return
        self.supply_mutated = True
        QMessageBox.information(self, "Доставка", "Поставка перенесена в доставку.")
        self._force_read_only = True
        self.reload()
