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
from app.services.ozon_stickers import OzonStickersService
from app.services.ozon_supplies import OzonSuppliesService
from app.ui.dialog_utils import (
    fullscreen_parent,
    init_fullscreen_dialog,
    make_modal_search_box,
)
from app.ui.format_helpers import build_product_cell_widget


class _LabelPrintWorker(QThread):
    ready = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(
        self,
        labels: OzonLabelService,
        supplies: OzonSuppliesService,
        stickers: Any,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_LabelPrintWorker, self).__init__(parent)
        self.labels = labels
        self.supplies = supplies
        self.stickers = stickers
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
            # Web archive «Стикеры»: v3 create + v2 get only (no obsolete sync).
            paths = self.labels.fetch_label_paths(
                self.client, self.source_id, self.posting_numbers
            )
            # Web: overwrite sticker_* after package-label so scan matches PDF.
            try:
                self.stickers.bind_after_labels(
                    self.client,
                    self.source_id,
                    self.posting_numbers,
                    overwrite=True,
                )
            except Exception:
                pass
            self.ready.emit(list(paths or []))
        except Exception as exc:
            self.failed.emit(str(exc))


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
        # Archive: «Старые стикеры» (sync v2) hidden/removed — only «Стикеры»
        # via /v3/posting/fbs/package-label/create + /v2/.../get.
        self.btn_labels = QPushButton("Стикеры")
        self.btn_labels.setObjectName("secondary")
        self.btn_labels.setToolTip(
            "Этикетка отправления Ozon (v3 create + v2 get)"
        )
        self.btn_labels.clicked.connect(self.print_labels)
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

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Отправление", "Товар", "Маркировка", "ШК", "Стикер"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(self._open_selected_posting)
        root.addWidget(self.table, 1)

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
        if ready:
            self.btn_rename.setEnabled(not ro)
            self.btn_move.setEnabled(not ro)
        self.btn_move.setVisible(not ro)

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
                for key in ("sticker_barcode", "sticker_lower_barcode"):
                    if q in str(r.get(key) or "").lower():
                        return True
                return False

            rows = [r for r in self._rows if _match(r)]
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

    def print_labels(self) -> None:
        pnums = self._posting_numbers()
        if not pnums:
            QMessageBox.information(self, "Этикетки", "Нет отправлений.")
            return
        if self._label_worker and self._label_worker.isRunning():
            return
        self.btn_labels.setEnabled(False)
        self.load_info.setText("Формирование стикеров Ozon (v3)…")
        worker = _LabelPrintWorker(
            self.labels,
            self.supplies,
            self.stickers,
            self.client,
            self.source_id,
            pnums,
            self,
        )
        self._label_worker = worker
        worker.ready.connect(self._on_labels_ready)
        worker.failed.connect(self._on_labels_failed)
        worker.finished.connect(lambda: self.btn_labels.setEnabled(True))
        worker.start()

    def _on_labels_ready(self, paths: object) -> None:
        path_list = [str(p) for p in (paths or []) if str(p).strip()]
        if not path_list:
            QMessageBox.warning(self, "Стикеры", "Ozon не вернул файл этикеток")
            return
        self.load_info.setText(
            "Стикеры: {} файл(ов)".format(len(path_list))
            if len(path_list) > 1
            else "Стикеры: {}".format(path_list[0])
        )
        self.reload(hydrate=False)
        for path in path_list:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_labels_failed(self, message: str) -> None:
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
