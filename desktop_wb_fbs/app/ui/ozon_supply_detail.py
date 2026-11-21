# -*- coding: utf-8 -*-
"""Ozon FBS local supply detail — web FeedPilot supply page for desktop."""
from __future__ import annotations

import json
import os
import tempfile
from functools import partial
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.services.ozon_labels import OzonLabelService
from app.services.ozon_mark_pick import OzonMarkService, OzonPickService
from app.services.ozon_stickers import OzonStickersService
from app.services.ozon_supplies import OzonSuppliesService
from app.ui.dialog_utils import (
    fullscreen_parent,
    init_fullscreen_dialog,
    make_modal_search_box,
)
from app.ui.format_helpers import (
    build_ozon_posting_cell_widget,
    build_product_cell_widget,
)
from app.ui.sticker_print_flow import StickerLoadDialog
from app.ui.table_col_widths import PersistentColumnWidths


class _LabelPrintWorker(QThread):
    """Build one archive-style stickers HTML, then bind QR in background.

    Cover + per-article separators + Ozon label images in a single file
    (web ``build_stickers_print`` / ``render_stickers_print_html`` parity).
    """

    progress = pyqtSignal(int, int, str)  # done, total, detail
    ready = pyqtSignal(list)
    failed = pyqtSignal(str)
    bind_done = pyqtSignal(int)

    def __init__(
        self,
        labels: OzonLabelService,
        supplies: OzonSuppliesService,
        stickers: Any,
        client: OzonFbsClient,
        source_id: int,
        supply_id: str,
        posting_numbers: List[str],
        *,
        source_name: str = "",
        include_cover_and_separators: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_LabelPrintWorker, self).__init__(parent)
        self.labels = labels
        self.supplies = supplies
        self.stickers = stickers
        self.client = client
        self.source_id = source_id
        self.supply_id = str(supply_id or "").strip()
        self.posting_numbers = list(posting_numbers or [])
        self.source_name = str(source_name or "").strip()
        self.include_cover_and_separators = bool(include_cover_and_separators)

    def _emit_progress(self, done: int, total: int, message: str) -> None:
        if self.isInterruptionRequested():
            return
        self.progress.emit(int(done or 0), int(total or 0), str(message or ""))

    def run(self) -> None:
        from app.services.ozon_labels import format_label_error
        from app.services.ozon_stickers_print import build_stickers_print_file

        nums = list(self.posting_numbers)
        n = len(nums)
        try:
            self._emit_progress(
                0, max(n, 1), "Стикеры Ozon v3 · {} отпр.…".format(n)
            )
            if self.isInterruptionRequested():
                return

            def _label_prog(*args) -> None:
                if self.isInterruptionRequested():
                    return
                if len(args) >= 3:
                    self._emit_progress(int(args[0]), int(args[1]), str(args[2]))
                elif args:
                    self._emit_progress(0, max(n, 1), str(args[0]))

            detail = self.supplies.get_detail(self.source_id, self.supply_id)
            # Web archive «Стикеры»: one HTML sheet (cover + артикул + labels).
            path = build_stickers_print_file(
                self.labels,
                self.client,
                detail,
                nums,
                source_id=self.source_id,
                source_name=self.source_name,
                include_cover_and_separators=self.include_cover_and_separators,
                progress=_label_prog,
                stop_requested=self.isInterruptionRequested,
            )
            if self.isInterruptionRequested():
                return
            self.ready.emit([path])
            try:
                self._emit_progress(0, max(n, 1), "Привязка QR после печати…")

                def _bind_prog(*args) -> None:
                    if self.isInterruptionRequested():
                        return
                    if len(args) >= 3:
                        self._emit_progress(
                            int(args[0]), int(args[1]), str(args[2])
                        )
                    elif args:
                        self._emit_progress(0, max(n, 1), str(args[0]))

                bound = self.stickers.bind_after_labels(
                    self.client,
                    self.source_id,
                    nums,
                    overwrite=True,
                    progress=_bind_prog,
                    workers=3,
                )
                if self.isInterruptionRequested():
                    return
                self.bind_done.emit(int(bound or 0))
            except Exception as bind_exc:
                self._emit_progress(
                    n,
                    max(n, 1),
                    "Стикеры открыты · привязка QR: {}".format(bind_exc),
                )
        except Exception as exc:
            if self.isInterruptionRequested():
                return
            msg = format_label_error(exc)
            if "отменено" in msg.casefold():
                return
            self.failed.emit(msg)


class _SupplyHydrateWorker(QThread):
    """Background get_posting for rows missing product/SKU/barcodes."""

    progress = pyqtSignal(int, int, str)
    finished_ok = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(
        self,
        supplies: OzonSuppliesService,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
        *,
        only_missing: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_SupplyHydrateWorker, self).__init__(parent)
        self.supplies = supplies
        self.client = client
        self.source_id = int(source_id)
        self.posting_numbers = list(posting_numbers or [])
        self.only_missing = bool(only_missing)
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            result = self.supplies.refresh_postings_from_ozon(
                self.client,
                self.source_id,
                self.posting_numbers,
                only_missing=self.only_missing,
                progress=lambda d, t, m: self.progress.emit(int(d), int(t), str(m or "")),
                stop_requested=lambda: self._stop,
            )
            self.finished_ok.emit(result if isinstance(result, dict) else {})
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
        self.stickers = OzonStickersService(db)
        self._detail = {}  # type: Dict[str, Any]
        self._rows = []  # type: List[Dict[str, Any]]
        self._label_worker = None  # type: Optional[_LabelPrintWorker]
        self._label_progress = None  # type: Optional[StickerLoadDialog]
        self._hydrate_worker = None  # type: Optional[_SupplyHydrateWorker]
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
        # Order matches web archive #ozonFbsSupplyDetailModal actions.
        self.btn_rename = QPushButton("Переименовать")
        self.btn_rename.setObjectName("secondary")
        self.btn_rename.setToolTip("Переименовать поставку")
        self.btn_rename.clicked.connect(self.rename_supply)
        self.btn_cancelled = QPushButton("Отмененные заказы")
        self.btn_cancelled.setObjectName("secondary")
        self.btn_cancelled.setToolTip("Проверить отменённые отправления в поставке")
        self.btn_cancelled.clicked.connect(self.show_cancelled)
        self.btn_picking = QPushButton("Лист подбора")
        self.btn_picking.setObjectName("secondary")
        self.btn_picking.clicked.connect(self.print_picking_list)
        # Archive: «Старые стикеры» removed — «Стикеры» + menu «По категориям».
        self.btn_labels = QPushButton("Стикеры")
        self.btn_labels.setObjectName("secondary")
        self.btn_labels.setToolTip(
            "Этикетка отправления Ozon (v3 create + v2 get)"
        )
        self.btn_labels.clicked.connect(self.print_labels)
        self.btn_labels_menu = QToolButton()
        self.btn_labels_menu.setObjectName("secondary")
        self.btn_labels_menu.setText("▾")
        self.btn_labels_menu.setToolTip("Другие варианты печати стикеров")
        self.btn_labels_menu.setPopupMode(QToolButton.InstantPopup)
        labels_menu = QMenu(self.btn_labels_menu)
        labels_menu.addAction(
            "Печать по категориям", self.stickers_by_category
        )
        self.btn_labels_menu.setMenu(labels_menu)
        self.btn_shipments = QPushButton("ШК поставки")
        self.btn_shipments.setObjectName("secondary")
        self.btn_shipments.setToolTip(
            "ШК / QR отгрузки Ozon (act/get-barcode) по складу поставки"
        )
        self.btn_shipments.clicked.connect(self.open_shipments_barcode)
        self.btn_containers = QPushButton("Грузоместа")
        self.btn_containers.setObjectName("secondary")
        self.btn_containers.setToolTip("Грузоместа Ozon FBS: паллеты, печать ШК")
        self.btn_containers.clicked.connect(self.open_containers)
        self.btn_mark = QPushButton("Товары с КИЗ")
        self.btn_mark.setToolTip("Товары с КИЗ — коды маркировки «Честный знак»")
        self.btn_mark.clicked.connect(self.open_mark)
        self.btn_pick = QPushButton("Товары без КИЗ")
        self.btn_pick.setObjectName("secondary")
        self.btn_pick.setToolTip("Товары без КИЗ — проверка штрихкодов")
        self.btn_pick.clicked.connect(self.open_pick)
        self.btn_driver = QPushButton("Водитель")
        self.btn_driver.setObjectName("secondary")
        self.btn_driver.setToolTip("Водитель поставки: выбрать из справочника и гос. номер ТС")
        self.btn_driver.clicked.connect(self.assign_driver)
        self.btn_ttn = QPushButton("ТН")
        self.btn_ttn.setObjectName("secondary")
        self.btn_ttn.clicked.connect(self.open_ttn_draft)
        self.btn_move = QPushButton("Перенести в доставку")
        self.btn_move.setToolTip(
            "Локально перенести поставку в «Доставляются» (без отправки в Ozon)"
        )
        self.btn_move.clicked.connect(self.move_to_delivering)
        self.btn_refresh = QPushButton("Обновить")
        self.btn_refresh.setObjectName("secondary")
        self.btn_refresh.setToolTip("Обновить товары/статусы из Ozon API")
        self.btn_refresh.clicked.connect(self.reload_from_ozon)
        for b in (
            self.btn_cancelled,
            self.btn_picking,
            self.btn_labels,
            self.btn_labels_menu,
            self.btn_shipments,
            self.btn_containers,
            self.btn_mark,
            self.btn_pick,
            self.btn_driver,
            self.btn_ttn,
            self.btn_move,
            self.btn_refresh,
            self.btn_rename,
        ):
            actions.addWidget(b)
        actions.addStretch(1)
        search_box, self.search_input = make_modal_search_box()
        self.search_input.setPlaceholderText("Поиск по отправлению, товару, ШК…")
        self.search_input.setToolTip("Поиск по отправлению, названию товара и ШК")
        self.search_input.textChanged.connect(self._apply_filter)
        actions.addWidget(search_box)
        hv.addLayout(actions)
        root.addWidget(header)

        self.load_info = QLabel("")
        self.load_info.setObjectName("syncInfoText")
        self.load_info.setContentsMargins(24, 8, 24, 8)
        root.addWidget(self.load_info)

        # Archive #ozonFbsSupplyDetailTable: Отправление | Товар (ii… under posting).
        self.table = QTableWidget(0, 2)
        self.table.setObjectName("sdTable")
        self.table.setHorizontalHeaderLabels(["Отправление", "Товар"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self._col_widths = PersistentColumnWidths(
            self.db,
            self.table,
            "ozon_supply_detail_table_cols_v2",
            [320, 640],
            parent=self,
        )
        self._col_widths.apply()
        self.table.doubleClicked.connect(self._open_selected_posting)
        self.table.itemSelectionChanged.connect(self._on_detail_selection_changed)
        root.addWidget(self.table, 1)

        # Archive #ozonFbsSupplyDetailBottomBar — local re-split of postings.
        self.detail_bottom = QFrame()
        self.detail_bottom.setObjectName("bottomBar")
        dbl = QHBoxLayout(self.detail_bottom)
        dbl.setContentsMargins(16, 10, 16, 10)
        dbl.setSpacing(8)
        self.detail_sel_label = QLabel("Выбрано 0 отправлений")
        self.detail_sel_label.setObjectName("selectedLabel")
        dbl.addWidget(self.detail_sel_label)
        dbl.addStretch(1)
        self.btn_detail_new_supply = QPushButton("Новая поставка")
        self.btn_detail_new_supply.clicked.connect(self.move_selected_to_new_supply)
        self.btn_detail_existing = QPushButton("В существующую")
        self.btn_detail_existing.setObjectName("secondary")
        self.btn_detail_existing.clicked.connect(
            self.move_selected_to_existing_supply
        )
        dbl.addWidget(self.btn_detail_new_supply)
        dbl.addWidget(self.btn_detail_existing)
        self.detail_bottom.hide()
        root.addWidget(self.detail_bottom)

        self.reload(hydrate=True)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._hydrate_worker and self._hydrate_worker.isRunning():
            self._hydrate_worker.request_stop()
        super(OzonSupplyDetailDialog, self).closeEvent(event)

    def _is_read_only(self) -> bool:
        return self._force_read_only or bool(self._detail.get("read_only"))

    def _action_buttons(self) -> List[QPushButton]:
        return [
            self.btn_cancelled,
            self.btn_picking,
            self.btn_labels,
            self.btn_shipments,
            self.btn_containers,
            self.btn_mark,
            self.btn_pick,
            self.btn_driver,
            self.btn_ttn,
            self.btn_move,
            self.btn_refresh,
            self.btn_rename,
        ]

    def _set_actions_ready(self, ready: bool = True) -> None:
        ro = self._is_read_only()
        for btn in self._action_buttons():
            btn.setEnabled(bool(ready))
        self.btn_labels_menu.setEnabled(bool(ready))
        if ready:
            self.btn_rename.setEnabled(not ro)
            self.btn_move.setEnabled(not ro)
            self.btn_detail_new_supply.setEnabled(not ro)
            self.btn_detail_existing.setEnabled(not ro)
        self.btn_move.setVisible(not ro)
        self.detail_bottom.setVisible(not ro)
        self._on_detail_selection_changed()

    def reload(self, *, hydrate: bool = True) -> None:
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
        self._set_actions_ready(True)
        self._apply_filter()
        missing = sum(
            1
            for r in self._rows
            if self.supplies.posting_needs_ozon_product_refresh(r)
        )
        if self._is_read_only():
            self.load_info.setText("Только просмотр · {} отпр.".format(len(self._rows)))
        elif missing and hydrate:
            self.load_info.setText(
                "Готово · {} отпр. · подгружаю товары из Ozon ({} без данных)…".format(
                    len(self._rows), missing
                )
            )
            self._start_hydrate(only_missing=True)
        else:
            self.load_info.setText(
                "Готово · {} отправлений".format(len(self._rows))
            )

    def reload_from_ozon(self) -> None:
        """Manual refresh — re-fetch all supply postings from Ozon API."""
        if self._hydrate_worker and self._hydrate_worker.isRunning():
            return
        self.reload(hydrate=False)
        self.load_info.setText("Обновление из Ozon…")
        self._start_hydrate(only_missing=False)

    def _start_hydrate(self, *, only_missing: bool) -> None:
        if self._is_read_only():
            return
        if self._hydrate_worker and self._hydrate_worker.isRunning():
            return
        pnums = [
            str(r.get("posting_number") or "").strip()
            for r in self._rows
            if str(r.get("posting_number") or "").strip()
        ]
        if not pnums:
            return
        if only_missing:
            pnums = [
                str(r.get("posting_number") or "").strip()
                for r in self._rows
                if self.supplies.posting_needs_ozon_product_refresh(r)
                and str(r.get("posting_number") or "").strip()
            ]
            if not pnums:
                return
        self._set_actions_ready(False)
        self.btn_refresh.setEnabled(False)
        worker = _SupplyHydrateWorker(
            self.supplies,
            self.client,
            self.source_id,
            pnums,
            only_missing=only_missing,
            parent=self,
        )
        self._hydrate_worker = worker
        worker.progress.connect(self._on_hydrate_progress)
        worker.finished_ok.connect(self._on_hydrate_done)
        worker.failed.connect(self._on_hydrate_failed)
        worker.finished.connect(self._on_hydrate_finished)
        worker.start()

    def _on_hydrate_progress(self, done: int, total: int, msg: str) -> None:
        self.load_info.setText(str(msg or "Загрузка из Ozon… {}/{}".format(done, total)))

    def _on_hydrate_done(self, result: object) -> None:
        res = result if isinstance(result, dict) else {}
        # Reload local enriched rows after upserts.
        self.reload(hydrate=False)
        refreshed = int(res.get("refreshed") or 0)
        failed = int(res.get("failed") or 0)
        skipped = int(res.get("skipped") or 0)
        parts = ["Готово · {} отпр.".format(len(self._rows))]
        if refreshed:
            parts.append("обновлено из Ozon: {}".format(refreshed))
        if skipped:
            parts.append("уже были данные: {}".format(skipped))
        if failed:
            parts.append("ошибок: {}".format(failed))
        self.load_info.setText(" · ".join(parts))

    def _on_hydrate_failed(self, message: str) -> None:
        self.load_info.setText(str(message or "Ошибка загрузки из Ozon"))
        self._set_actions_ready(True)

    def _on_hydrate_finished(self) -> None:
        self._hydrate_worker = None
        self._set_actions_ready(True)

    def _apply_filter(self) -> None:
        q = self.search_input.text().strip().lower()
        rows = self._rows
        if q:
            def _match(r: Dict[str, Any]) -> bool:
                if q in str(r.get("posting_number") or "").lower():
                    return True
                if q in str(r.get("offer_id") or "").lower():
                    return True
                if q in str(r.get("product_name") or r.get("product_name_display") or "").lower():
                    return True
                if q in str(r.get("sku") or "").lower():
                    return True
                for code in r.get("barcodes") or []:
                    if q in str(code or "").lower():
                        return True
                for key in (
                    "sticker_barcode",
                    "sticker_lower_barcode",
                    "sticker_scanit",
                ):
                    if q in str(r.get(key) or "").lower():
                        return True
                return False

            rows = [r for r in self._rows if _match(r)]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            pnum = str(r.get("posting_number") or "")
            # Keep a data item under the cell widget for selection / UserRole.
            item = QTableWidgetItem(pnum)
            item.setData(Qt.UserRole, pnum)
            self.table.setItem(i, 0, item)
            self.table.setCellWidget(i, 0, build_ozon_posting_cell_widget(r))
            self.table.setCellWidget(i, 1, build_product_cell_widget(r, photo_size=72))
            self.table.setRowHeight(i, 110)

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
            self.reload(hydrate=False)

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
        self.reload(hydrate=False)

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
            self.reload(hydrate=False)

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
            self.reload(hydrate=False)

    def print_labels(self, posting_numbers: Optional[List[str]] = None) -> None:
        pnums = [
            str(p).strip()
            for p in (posting_numbers or self._posting_numbers())
            if str(p).strip()
        ]
        if not pnums:
            QMessageBox.information(self, "Стикеры", "Нет отправлений.")
            return
        if self._label_worker and self._label_worker.isRunning():
            return
        self.btn_labels.setEnabled(False)
        self.btn_labels_menu.setEnabled(False)
        self.load_info.setText(
            "Стикеры Ozon v3 · {} отпр.…".format(len(pnums))
        )
        progress = StickerLoadDialog(
            self, title="Стикеры Ozon · {}".format(self.supply_id)
        )
        progress.title_lab.setText("Формирование стикеров Ozon")
        progress.detail.setText(
            "Создание задания этикеток · {} отпр.…".format(len(pnums))
        )
        progress.set_progress(0, max(len(pnums), 1), "")
        self._label_progress = progress
        source_name = str(
            self.source.get("name") or self.source.get("client_id") or ""
        ).strip()
        worker = _LabelPrintWorker(
            self.labels,
            self.supplies,
            self.stickers,
            self.client,
            self.source_id,
            self.supply_id,
            pnums,
            source_name=source_name,
            include_cover_and_separators=True,
            parent=self,
        )
        self._label_worker = worker
        worker.progress.connect(self._on_labels_progress)
        worker.ready.connect(self._on_labels_ready)
        worker.bind_done.connect(self._on_labels_bind_done)
        worker.failed.connect(self._on_labels_failed)
        worker.finished.connect(self._on_labels_worker_finished)
        progress.finished.connect(self._on_labels_progress_closed)
        worker.start()
        progress.show()

    def _on_labels_progress(self, done: int, total: int, message: str) -> None:
        text = str(message or "").strip()
        if text:
            self.load_info.setText(text)
        dlg = self._label_progress
        if dlg is not None and dlg.isVisible():
            dlg.set_progress(int(done or 0), int(total or 0), text)

    def _close_label_progress(self) -> None:
        dlg = self._label_progress
        self._label_progress = None
        if dlg is None:
            return
        try:
            dlg.finished.disconnect(self._on_labels_progress_closed)
        except Exception:
            pass
        if dlg.isVisible():
            dlg.accept()
        dlg.deleteLater()

    def _on_labels_progress_closed(self, _result: int = 0) -> None:
        dlg = self._label_progress
        if dlg is not None and getattr(dlg, "cancelled", False):
            worker = self._label_worker
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
            self.load_info.setText("Формирование стикеров отменено")
        self._label_progress = None

    def _on_labels_worker_finished(self) -> None:
        self.btn_labels.setEnabled(True)
        self.btn_labels_menu.setEnabled(True)
        self._close_label_progress()

    def _on_labels_bind_done(self, bound: int) -> None:
        text = "Стикеры готовы · QR привязано: {}".format(int(bound or 0))
        self.load_info.setText(text)
        dlg = self._label_progress
        if dlg is not None and dlg.isVisible():
            n = max(int(bound or 0), 1)
            dlg.set_progress(n, n, text)
        self.reload(hydrate=False)

    def stickers_by_category(self) -> None:
        try:
            groups = self.supplies.sticker_groups_by_article(
                self.source_id, self.supply_id
            )
        except Exception as exc:
            QMessageBox.warning(self, "Стикеры", str(exc))
            return
        if not groups:
            QMessageBox.information(self, "Стикеры", "Нет групп для печати.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Печать по категориям · {}".format(self.supply_id))
        dlg.resize(720, 560)
        lay = QVBoxLayout(dlg)
        title = QLabel("Печать стикеров по артикулам")
        title.setObjectName("dialogTitle")
        lay.addWidget(title)
        toolbar = QHBoxLayout()
        btn_all = QPushButton("Выделить все")
        btn_all.setObjectName("secondary")
        btn_clear = QPushButton("Очистить все")
        btn_clear.setObjectName("secondary")
        selected_lab = QLabel("Выбрано: 0")
        toolbar.addWidget(btn_all)
        toolbar.addWidget(btn_clear)
        toolbar.addStretch(1)
        toolbar.addWidget(selected_lab)
        lay.addLayout(toolbar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        wrap = QWidget()
        rows_lay = QVBoxLayout(wrap)
        row_widgets = []  # type: List[Dict[str, Any]]

        def _sync() -> None:
            n = sum(1 for r in row_widgets if r["cb"].isChecked())
            qty = sum(
                int(r["qty"]) for r in row_widgets if r["cb"].isChecked()
            )
            selected_lab.setText("Выбрано: {} · {} шт.".format(n, qty))
            print_btn.setEnabled(n > 0)

        for idx, g in enumerate(groups):
            frame = QFrame()
            row = QHBoxLayout(frame)
            cb = QCheckBox()
            name = str(g.get("product_name") or "—")
            qty = int(g.get("qty") or 0)
            label = QLabel("{} — {} шт.".format(name, qty))
            label.setWordWrap(True)
            fill = QToolButton()
            fill.setText("▼")
            fill.setToolTip("Выделить все ниже")

            def _fill_down(start: int = idx) -> None:
                for i in range(start, len(row_widgets)):
                    row_widgets[i]["cb"].setChecked(True)
                _sync()

            fill.clicked.connect(partial(_fill_down, idx))
            cb.stateChanged.connect(lambda _s: _sync())
            row.addWidget(cb)
            row.addWidget(label, 1)
            row.addWidget(fill)
            rows_lay.addWidget(frame)
            row_widgets.append(
                {
                    "cb": cb,
                    "qty": qty,
                    "posting_numbers": list(g.get("posting_numbers") or []),
                }
            )
        rows_lay.addStretch(1)
        scroll.setWidget(wrap)
        lay.addWidget(scroll, 1)

        def _select_all() -> None:
            for r in row_widgets:
                r["cb"].setChecked(True)
            _sync()

        def _clear_all() -> None:
            for r in row_widgets:
                r["cb"].setChecked(False)
            _sync()

        btn_all.clicked.connect(_select_all)
        btn_clear.clicked.connect(_clear_all)

        def _print_selected() -> None:
            nums = []  # type: List[str]
            for r in row_widgets:
                if r["cb"].isChecked():
                    nums.extend(str(x) for x in (r["posting_numbers"] or []) if str(x))
            if not nums:
                QMessageBox.information(dlg, "Стикеры", "Выберите категории")
                return
            dlg.accept()
            self.print_labels(nums)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        print_btn = QPushButton("Печать")
        print_btn.setEnabled(False)
        print_btn.clicked.connect(_print_selected)
        btns.addButton(print_btn, QDialogButtonBox.AcceptRole)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        _sync()
        dlg.exec_()

    def open_shipments_barcode(self) -> None:
        """Archive shipments modal lite: pick carriage for warehouse → act barcode."""
        from app.services.ozon_act import OzonActService
        from app.services.ozon_orders import OzonOrdersService

        wh = 0
        try:
            wh = int(self._detail.get("warehouse_id") or 0)
        except (TypeError, ValueError):
            wh = 0
        orders = OzonOrdersService(self.db)
        carriages = []  # type: List[Dict[str, Any]]
        try:
            for c in orders.list_open_carriages(self.source_id):
                try:
                    cwh = int(c.get("warehouse_id") or 0)
                except (TypeError, ValueError):
                    cwh = 0
                if wh > 0 and cwh > 0 and cwh != wh:
                    continue
                carriages.append(c)
            if not carriages:
                carriages = list(orders.list_delivery_carriages(self.source_id) or [])
        except Exception as exc:
            QMessageBox.warning(self, "ШК поставки", str(exc))
            return
        if not carriages:
            QMessageBox.information(
                self,
                "ШК поставки",
                "Нет отгрузок Ozon для склада этой поставки. "
                "Сформируйте отгрузку (акт) в Ozon, затем повторите.",
            )
            return
        labels = []
        for c in carriages:
            cid = str(c.get("carriage_id") or c.get("id") or "").strip()
            name = str(
                c.get("delivery_method_name")
                or c.get("name")
                or cid
                or "—"
            )
            labels.append("{} · {}".format(name, cid))
        choice, ok = QInputDialog.getItem(
            self,
            "ШК поставки",
            "Отгрузка (carriage) для печати ШК:",
            labels,
            0,
            False,
        )
        if not ok or not choice:
            return
        idx = labels.index(choice)
        carriage = carriages[idx]
        cid = str(
            carriage.get("carriage_id") or carriage.get("id") or ""
        ).strip()
        try:
            dm_id = int(
                carriage.get("delivery_method_id")
                or carriage.get("delivery_method")
                or 0
            )
        except (TypeError, ValueError):
            dm_id = 0
        if not cid:
            QMessageBox.warning(self, "ШК поставки", "Не удалось определить отгрузку")
            return
        self.load_info.setText("Загрузка ШК отгрузки…")
        try:
            content, name = OzonActService(self.db).fetch_barcode(
                self.client,
                self.source_id,
                cid,
                delivery_method_id=dm_id or None,
                create_if_missing=True,
            )
        except Exception as exc:
            QMessageBox.warning(self, "ШК поставки", str(exc))
            self.load_info.setText("")
            return
        suffix = ".png"
        if content[:4] == b"%PDF":
            suffix = ".pdf"
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="ozon-ship-barcode-")
        os.close(fd)
        with open(path, "wb") as fh:
            fh.write(content)
        self.load_info.setText("ШК: {}".format(name or path))
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_detail_selection_changed(self) -> None:
        n = len(self._selected_posting_numbers_multi())
        self.detail_sel_label.setText("Выбрано {} отправлений".format(n))
        enabled = n > 0 and not self._is_read_only()
        self.btn_detail_new_supply.setEnabled(enabled)
        self.btn_detail_existing.setEnabled(enabled)

    def _selected_posting_numbers_multi(self) -> List[str]:
        out = []  # type: List[str]
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if not item:
                continue
            pnum = str(item.data(Qt.UserRole) or item.text() or "").strip()
            if pnum:
                out.append(pnum)
        return out

    def move_selected_to_new_supply(self) -> None:
        if self._is_read_only():
            return
        pnums = self._selected_posting_numbers_multi()
        if not pnums:
            return
        name, ok = QInputDialog.getText(
            self,
            "Новая поставка",
            "Название новой поставки:",
            text=str(self._detail.get("name") or "") + " · часть",
        )
        if not ok:
            return
        try:
            result = self.supplies.create_local_supply_from_postings(
                self.source_id, pnums, name=str(name or "")
            )
        except Exception as exc:
            QMessageBox.warning(self, "Поставка", str(exc))
            return
        self.supply_mutated = True
        QMessageBox.information(
            self, "Поставка", str(result.get("message") or "Готово")
        )
        self.reload(hydrate=False)

    def move_selected_to_existing_supply(self) -> None:
        if self._is_read_only():
            return
        pnums = self._selected_posting_numbers_multi()
        if not pnums:
            return
        targets = self.supplies.list_move_targets(
            self.source_id, exclude_supply_id=self.supply_id
        )
        if not targets:
            QMessageBox.information(
                self,
                "Поставка",
                "Нет других поставок в «Ожидают отгрузки» для переноса.",
            )
            return
        labels = [
            "{} · {} отпр.".format(
                str(t.get("name") or t.get("supply_id") or ""),
                int(t.get("posting_count") or t.get("order_count") or 0),
            )
            for t in targets
        ]
        choice, ok = QInputDialog.getItem(
            self,
            "В существующую",
            "Куда перенести {} отправлений:".format(len(pnums)),
            labels,
            0,
            False,
        )
        if not ok or not choice:
            return
        target = targets[labels.index(choice)]
        try:
            result = self.supplies.move_postings_to_local_supply(
                self.source_id,
                pnums,
                str(target.get("supply_id") or ""),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Поставка", str(exc))
            return
        self.supply_mutated = True
        QMessageBox.information(
            self, "Поставка", str(result.get("message") or "Готово")
        )
        self.reload(hydrate=False)

    def _on_labels_ready(self, paths: object) -> None:
        path_list = [str(p) for p in (paths or []) if str(p).strip()]
        if not path_list:
            self._close_label_progress()
            QMessageBox.warning(self, "Стикеры", "Ozon не вернул файл этикеток")
            return
        text = "Лист стикеров открыт · привязка QR…"
        self.load_info.setText(text)
        dlg = self._label_progress
        if dlg is not None and dlg.isVisible():
            dlg.set_progress(0, max(len(self._posting_numbers()), 1), text)
        # One HTML sheet (cover + артикулы + этикетки), same as web archive.
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path_list[0])))

    def _on_labels_failed(self, message: str) -> None:
        self._close_label_progress()
        self.load_info.setText(str(message or "Ошибка стикеров"))
        QMessageBox.warning(self, "Стикеры", str(message or "Ошибка"))

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
            db=self.db,
            parent=self,
        )
        dlg.exec_()

    def assign_driver(self) -> None:
        from app.services.supply_drivers import SupplyDriversService
        from app.ui.driver_assign_dialog import DriverAssignDialog

        dlg = DriverAssignDialog(
            self.db,
            platform="ozon",
            source_id=self.source_id,
            supply_id=self.supply_id,
            parent=self,
        )
        dlg.exec_()
        if dlg.changed:
            assigned = SupplyDriversService(self.db).get_assignment(
                "ozon", self.source_id, self.supply_id
            )
            if assigned.get("has_driver"):
                self.load_info.setText(
                    "Водитель: {} · {}".format(
                        assigned.get("driver_name") or "—",
                        assigned.get("vehicle_number") or "без номера",
                    )
                )
            else:
                self.load_info.setText("Водитель снят")

    def open_ttn_draft(self) -> None:
        from app.ui.ttn_draft_dialog import TtnDraftDialog

        cargo = str(len(self._posting_numbers()) or "")
        dlg = TtnDraftDialog(
            self.db,
            platform="ozon",
            source_id=self.source_id,
            supply_id=self.supply_id,
            supply_name=str(self._detail.get("name") or self.supply_id),
            warehouse_name=str(self._detail.get("warehouse_name") or ""),
            cargo_places=cargo,
            parent=self,
        )
        dlg.exec_()

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
            self.reload(hydrate=False)
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

    def _move_delivering_blockers(self) -> List[str]:
        """Archive ``_ozonFbsCanMoveToDelivering``: driver + KIZ/pick when needed."""
        blockers = []  # type: List[str]
        from app.services.supply_drivers import SupplyDriversService

        assigned = SupplyDriversService(self.db).get_assignment(
            "ozon", self.source_id, self.supply_id
        )
        if not assigned.get("has_driver"):
            blockers.append("Водитель не выбран")
        pnums = self._posting_numbers()
        try:
            mark_rows = self.mark.marking_rows_for_postings(self.source_id, pnums)
        except Exception:
            mark_rows = []
        try:
            pick_rows = self.pick.pick_rows_for_postings(self.source_id, pnums)
        except Exception:
            pick_rows = []
        # KIZ rows (have marks) must be synced; plain rows must be pick_verified.
        kiz_rows = [r for r in mark_rows if r.get("marks")]
        if kiz_rows:
            pending = [r for r in kiz_rows if not r.get("marks_synced")]
            if pending:
                blockers.append(
                    "Не завершено сканирование товаров с КИЗ ({}/{})".format(
                        len(kiz_rows) - len(pending), len(kiz_rows)
                    )
                )
        if pick_rows:
            pending_pick = [r for r in pick_rows if not r.get("pick_verified")]
            if pending_pick:
                blockers.append(
                    "Не завершено сканирование товаров без КИЗ ({}/{})".format(
                        len(pick_rows) - len(pending_pick), len(pick_rows)
                    )
                )
        if not kiz_rows and not pick_rows:
            blockers.append("Нет товаров для проверки КИЗ / ШК")
        return blockers

    def move_to_delivering(self) -> None:
        if self._is_read_only():
            return
        blockers = self._move_delivering_blockers()
        if blockers:
            QMessageBox.warning(
                self,
                "Доставка",
                "Чтобы перенести в доставку:\n• "
                + "\n• ".join(blockers),
            )
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
        self.reload(hydrate=False)
