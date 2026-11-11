# -*- coding: utf-8 -*-
"""Ozon FBS containers (грузоместа) dialog — TrbxDialog-style + archive/reconcile."""
from __future__ import annotations

from functools import partial
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.services.ozon_containers import OzonContainersService
from app.ui.dialog_utils import prepare_modal_dialog


class OzonContainersDialog(QDialog):
    """List / create / delete / approve / print / bind / archive / reconcile."""

    def __init__(
        self,
        client: OzonFbsClient,
        carriage_id: str,
        *,
        source_id: int = 0,
        posting_numbers: Optional[List[str]] = None,
        selected_posting: str = "",
        warehouse_id: int = 0,
        db: Optional[Database] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(OzonContainersDialog, self).__init__(parent)
        self.client = client
        self.db = db
        self.svc = OzonContainersService(db)
        self.carriage_id = str(carriage_id or "").strip()
        self.source_id = int(source_id or 0)
        self.warehouse_id = int(warehouse_id or 0)
        self.posting_numbers = [
            str(p).strip() for p in (posting_numbers or []) if str(p).strip()
        ]
        self.selected_posting = str(selected_posting or "").strip()
        self.containers = []  # type: List[Dict[str, Any]]
        self._include_archived = False

        scope = str(self.carriage_id or "")
        if scope.startswith("supply:"):
            self.setWindowTitle("Грузоместа поставки {}".format(scope[7:]))
        else:
            self.setWindowTitle("Грузоместа отгрузки {}".format(scope))
        prepare_modal_dialog(
            self,
            maximized=True,
            default_size=(720, 560),
            minimum_size=(560, 440),
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title = QLabel("Грузоместа (Ozon)")
        title.setObjectName("dialogTitle")
        title.setWordWrap(True)
        root.addWidget(title)

        lead = QLabel(
            "Создайте грузоместа, привяжите отправления, "
            "подтвердите состав и распечатайте этикетки. "
            "«Сверить с Ozon» подтягивает состав ГМ с портала; "
            "архив только локально скрывает залипшие ГМ."
        )
        lead.setObjectName("hint")
        lead.setWordWrap(True)
        root.addWidget(lead)

        stepper = QHBoxLayout()
        stepper.setSpacing(8)
        self.minus_btn = QPushButton("−")
        self.minus_btn.setObjectName("secondary")
        self.minus_btn.setFixedWidth(44)
        self.minus_btn.clicked.connect(partial(self._step_amount, -1))
        self.amount = QSpinBox()
        self.amount.setRange(1, 100)
        self.amount.setValue(1)
        self.amount.setMinimumWidth(88)
        self.plus_btn = QPushButton("+")
        self.plus_btn.setObjectName("secondary")
        self.plus_btn.setFixedWidth(44)
        self.plus_btn.clicked.connect(partial(self._step_amount, 1))
        self.type_combo = QComboBox()
        self.type_combo.addItem("Паллета", "pallet")
        self.type_combo.addItem("Короб", "box")
        self.create_btn = QPushButton("Создать")
        self.create_btn.clicked.connect(self.create_containers)
        stepper.addWidget(self.minus_btn)
        stepper.addWidget(self.amount)
        stepper.addWidget(self.plus_btn)
        stepper.addSpacing(8)
        stepper.addWidget(self.type_combo)
        stepper.addSpacing(8)
        stepper.addWidget(self.create_btn)
        stepper.addStretch(1)
        root.addLayout(stepper)

        filt = QHBoxLayout()
        filt.setSpacing(8)
        self.show_archived = QCheckBox("Показать локальный архив")
        self.show_archived.toggled.connect(self._on_show_archived)
        filt.addWidget(self.show_archived)
        filt.addStretch(1)
        root.addLayout(filt)

        self.info = QLabel("")
        self.info.setObjectName("hint")
        self.info.setWordWrap(True)
        root.addWidget(self.info)

        self.table = QTableWidget(0, 4)
        self.table.setObjectName("trbxTable")
        self.table.setHorizontalHeaderLabels(
            ["ID", "№", "Статус", "Заказов"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        root.addWidget(self.table, 1)

        bind_row = QHBoxLayout()
        bind_row.setSpacing(8)
        bind_row.addWidget(QLabel("Отправление:"))
        self.posting_combo = QComboBox()
        self.posting_combo.setMinimumWidth(220)
        self.posting_combo.addItem("— не выбрано —", "")
        for pn in self.posting_numbers:
            self.posting_combo.addItem(pn, pn)
        if self.selected_posting:
            idx = self.posting_combo.findData(self.selected_posting)
            if idx >= 0:
                self.posting_combo.setCurrentIndex(idx)
        self.bind_btn = QPushButton("Привязать к выбранному ГМ")
        self.bind_btn.setObjectName("secondary")
        self.bind_btn.clicked.connect(self.bind_selected)
        self.unbind_btn = QPushButton("Отвязать")
        self.unbind_btn.setObjectName("secondary")
        self.unbind_btn.clicked.connect(self.unbind_selected)
        bind_row.addWidget(self.posting_combo, 1)
        bind_row.addWidget(self.bind_btn)
        bind_row.addWidget(self.unbind_btn)
        root.addLayout(bind_row)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.setObjectName("secondary")
        self.refresh_btn.clicked.connect(self.reload)
        self.reconcile_btn = QPushButton("Сверить с Ozon")
        self.reconcile_btn.setObjectName("secondary")
        self.reconcile_btn.clicked.connect(self.reconcile)
        self.archive_btn = QPushButton("В архив")
        self.archive_btn.setObjectName("secondary")
        self.archive_btn.clicked.connect(self.archive_selected)
        self.restore_btn = QPushButton("Из архива")
        self.restore_btn.setObjectName("secondary")
        self.restore_btn.clicked.connect(self.restore_selected)
        self.approve_btn = QPushButton("Подтвердить")
        self.approve_btn.clicked.connect(self.approve_selected)
        self.labels_btn = QPushButton("Этикетки PDF")
        self.labels_btn.setObjectName("secondary")
        self.labels_btn.clicked.connect(self.print_labels)
        self.delete_btn = QPushButton("Удалить")
        self.delete_btn.setObjectName("danger")
        self.delete_btn.clicked.connect(self.delete_selected)
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.reconcile_btn)
        actions.addWidget(self.archive_btn)
        actions.addWidget(self.restore_btn)
        actions.addWidget(self.approve_btn)
        actions.addWidget(self.labels_btn)
        actions.addWidget(self.delete_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        self.reload()

    def _step_amount(self, delta: int) -> None:
        self.amount.setValue(max(1, min(100, self.amount.value() + int(delta))))

    def _on_show_archived(self, checked: bool) -> None:
        self._include_archived = bool(checked)
        self.reload()

    def _selected_ids(self) -> List[int]:
        ids = []  # type: List[int]
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if not item:
                continue
            try:
                cid = int(item.data(Qt.UserRole) or item.text() or 0)
            except (TypeError, ValueError):
                cid = 0
            if cid > 0:
                ids.append(cid)
        return ids

    def _selected_posting(self) -> str:
        return str(self.posting_combo.currentData() or "").strip()

    def reload(self) -> None:
        try:
            result = self.svc.list_containers(
                self.client,
                self.carriage_id,
                source_id=self.source_id or None,
                warehouse_id=self.warehouse_id or None,
            )
            self.warehouse_id = int(result.get("warehouse_id") or self.warehouse_id or 0)
            items = list(result.get("items") or [])
            if self.source_id > 0:
                items = self.svc.apply_local_archive_filter(
                    self.source_id,
                    items,
                    include_archived=self._include_archived,
                )
            self.containers = items
            self._render_table()
            archived_n = 0
            if self.source_id > 0:
                archived_n = len(self.svc.list_archived_ids(self.source_id))
            self.info.setText(
                "Грузомест: {} · склад {} · в локальном архиве {}".format(
                    len(self.containers),
                    self.warehouse_id or "—",
                    archived_n,
                )
            )
        except Exception as exc:
            self.info.setText(str(exc))
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def _render_table(self) -> None:
        self.table.setRowCount(len(self.containers))
        for i, row in enumerate(self.containers):
            cid = int(row.get("container_id") or 0)
            id_item = QTableWidgetItem(str(cid))
            id_item.setData(Qt.UserRole, cid)
            self.table.setItem(i, 0, id_item)
            self.table.setItem(
                i, 1, QTableWidgetItem(str(row.get("container_number") or "—"))
            )
            self.table.setItem(
                i,
                2,
                QTableWidgetItem(
                    str(row.get("status_label") or row.get("status") or "—")
                ),
            )
            self.table.setItem(
                i, 3, QTableWidgetItem(str(row.get("order_count") or 0))
            )

    def create_containers(self) -> None:
        cargo = str(self.type_combo.currentData() or "pallet")
        try:
            result = self.svc.create(
                self.client,
                self.carriage_id,
                self.amount.value(),
                container_type=cargo,
                source_id=self.source_id or None,
                warehouse_id=self.warehouse_id or None,
            )
            self.warehouse_id = int(
                result.get("warehouse_id") or self.warehouse_id or 0
            )
            QMessageBox.information(
                self, "Грузоместа", str(result.get("message") or "Создано")
            )
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def delete_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "Грузоместа", "Выберите грузоместа.")
            return
        if (
            QMessageBox.question(
                self,
                "Грузоместа",
                "Удалить выбранные грузоместа ({})?".format(len(ids)),
            )
            != QMessageBox.Yes
        ):
            return
        try:
            result = self.svc.delete(self.client, ids)
            QMessageBox.information(
                self, "Грузоместа", str(result.get("message") or "Готово")
            )
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def approve_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "Грузоместа", "Выберите грузоместа.")
            return
        if (
            QMessageBox.question(
                self,
                "Грузоместа",
                "Подтвердить состав выбранных грузомест ({})?".format(len(ids)),
            )
            != QMessageBox.Yes
        ):
            return
        try:
            result = self.svc.approve(self.client, ids)
            QMessageBox.information(
                self, "Грузоместа", str(result.get("message") or "Готово")
            )
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def print_labels(self) -> None:
        ids = self._selected_ids()
        if not ids:
            ids = [
                int(c.get("container_id") or 0)
                for c in self.containers
                if int(c.get("container_id") or 0) > 0
            ]
        if not ids:
            QMessageBox.information(self, "Грузоместа", "Нет грузомест для печати.")
            return
        try:
            _payload, path = self.svc.labels_pdf(self.client, ids)
            self.info.setText("Этикетки сохранены: {}".format(path))
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def archive_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "Грузоместа", "Выберите грузоместа.")
            return
        if self.source_id <= 0:
            QMessageBox.information(
                self, "Грузоместа", "Нужен источник для локального архива."
            )
            return
        try:
            for cid in ids:
                self.svc.archive_locally(self.source_id, cid)
            self.reload()
            QMessageBox.information(
                self,
                "Грузоместа",
                "В локальный архив: {}".format(len(ids)),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def restore_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "Грузоместа", "Выберите грузоместа.")
            return
        if self.source_id <= 0:
            return
        try:
            for cid in ids:
                self.svc.restore_locally(self.source_id, cid)
            self.reload()
            QMessageBox.information(
                self,
                "Грузоместа",
                "Восстановлено из архива: {}".format(len(ids)),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def reconcile(self) -> None:
        if self.source_id <= 0 or not self.posting_numbers:
            QMessageBox.information(
                self,
                "Грузоместа",
                "Нет отправлений поставки для сверки с Ozon.",
            )
            return
        try:
            result = self.svc.reconcile_binds(
                self.client,
                self.source_id,
                self.posting_numbers,
                warehouse_id=self.warehouse_id,
                carriage_id=self.carriage_id,
            )
            self.info.setText(str(result.get("message") or "Сверка выполнена"))
            QMessageBox.information(
                self, "Грузоместа", str(result.get("message") or "Готово")
            )
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def bind_selected(self) -> None:
        ids = self._selected_ids()
        pn = self._selected_posting()
        if len(ids) != 1:
            QMessageBox.information(
                self, "Грузоместа", "Выберите одно грузоместо для привязки."
            )
            return
        if not pn:
            QMessageBox.information(
                self, "Грузоместа", "Выберите отправление из списка."
            )
            return
        try:
            self.svc.bind_posting(
                self.client,
                self.carriage_id,
                ids[0],
                pn,
                source_id=self.source_id,
            )
            QMessageBox.information(
                self,
                "Грузоместа",
                "Отправление {} привязано к ГМ {}.".format(pn, ids[0]),
            )
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def unbind_selected(self) -> None:
        ids = self._selected_ids()
        pn = self._selected_posting()
        if len(ids) != 1 or not pn:
            QMessageBox.information(
                self,
                "Грузоместа",
                "Выберите одно грузоместо и отправление для отвязки.",
            )
            return
        try:
            self.svc.unbind(
                self.client,
                ids[0],
                pn,
                carriage_id=self.carriage_id,
                source_id=self.source_id,
            )
            QMessageBox.information(
                self,
                "Грузоместа",
                "Отправление {} отвязано от ГМ {}.".format(pn, ids[0]),
            )
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))
