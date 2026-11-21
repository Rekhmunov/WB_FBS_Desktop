# -*- coding: utf-8 -*-
"""Ozon FBS containers (грузоместа) — parity with web archive modal."""
from __future__ import annotations

from functools import partial
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
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
    """List / create / delete / approve / print — archive-parity layout."""

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
        self._include_sc_accepted = False
        self._only_this_supply = False
        self._reconciled_once = False

        self.setWindowTitle("Грузоместа")
        prepare_modal_dialog(
            self,
            maximized=True,
            default_size=(780, 600),
            minimum_size=(600, 460),
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        title = QLabel("Грузоместа")
        title.setObjectName("dialogTitle")
        title.setWordWrap(True)
        root.addWidget(title)

        # Create row: web stepper + sort type (pallet cargo + sort/non-sort).
        controls = QHBoxLayout()
        controls.setSpacing(12)

        stepper = QFrame()
        stepper.setObjectName("trbxStepper")
        step_lay = QHBoxLayout(stepper)
        step_lay.setContentsMargins(0, 0, 0, 0)
        step_lay.setSpacing(0)
        self.minus_btn = QPushButton("−")
        self.minus_btn.setObjectName("trbxStepBtn")
        self.minus_btn.setCursor(Qt.PointingHandCursor)
        self.minus_btn.clicked.connect(partial(self._step_amount, -1))
        self.amount = QSpinBox()
        self.amount.setObjectName("trbxAmount")
        self.amount.setRange(1, 100)
        self.amount.setValue(1)
        self.amount.setAlignment(Qt.AlignCenter)
        self.amount.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.plus_btn = QPushButton("+")
        self.plus_btn.setObjectName("trbxStepBtn")
        self.plus_btn.setCursor(Qt.PointingHandCursor)
        self.plus_btn.clicked.connect(partial(self._step_amount, 1))
        step_lay.addWidget(self.minus_btn)
        step_lay.addWidget(self.amount)
        step_lay.addWidget(self.plus_btn)

        type_wrap = QVBoxLayout()
        type_wrap.setContentsMargins(0, 0, 0, 0)
        type_wrap.setSpacing(4)
        type_lbl = QLabel("Тип")
        type_lbl.setObjectName("fieldLabel")
        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("formSelect")
        self.sort_combo.addItem("Сортируемое", "sort")
        self.sort_combo.addItem("Несортируемое", "non-sort")
        type_wrap.addWidget(type_lbl)
        type_wrap.addWidget(self.sort_combo)

        self.create_btn = QPushButton("Создать")
        self.create_btn.clicked.connect(self.create_containers)
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.setObjectName("secondary")
        self.refresh_btn.clicked.connect(self.reload)
        controls.addWidget(stepper, 0, Qt.AlignBottom)
        controls.addLayout(type_wrap)
        controls.addWidget(self.create_btn, 0, Qt.AlignBottom)
        controls.addWidget(self.refresh_btn, 0, Qt.AlignBottom)
        controls.addStretch(1)
        root.addLayout(controls)

        print_row = QHBoxLayout()
        print_row.setSpacing(8)
        self.print_supply_btn = QPushButton("Распечатать ШК поставки")
        self.print_supply_btn.setObjectName("secondary")
        self.print_supply_btn.clicked.connect(self.print_supply_barcode)
        print_row.addWidget(self.print_supply_btn)
        print_row.addStretch(1)
        root.addLayout(print_row)

        filt = QHBoxLayout()
        filt.setSpacing(16)
        self.show_sc = QCheckBox("Показать принятые / завершённые на СЦ")
        self.show_sc.toggled.connect(self._on_show_sc)
        self.only_supply = QCheckBox("ГМ у этой поставки")
        self.only_supply.toggled.connect(self._on_only_supply)
        self.show_archived = QCheckBox("Показать локальный архив")
        self.show_archived.toggled.connect(self._on_show_archived)
        filt.addWidget(self.show_sc)
        filt.addWidget(self.only_supply)
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
            ["Грузоместо", "Заказы", "Статус", "Действия"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(56)
        root.addWidget(self.table, 1)

        # Archive modal has no manual bind/unbind — scan/KIZ fills containers.
        self.reload(auto_reconcile=True)

    def _step_amount(self, delta: int) -> None:
        self.amount.setValue(max(1, min(100, self.amount.value() + int(delta))))

    def _on_show_archived(self, checked: bool) -> None:
        self._include_archived = bool(checked)
        self.reload()

    def _on_show_sc(self, checked: bool) -> None:
        self._include_sc_accepted = bool(checked)
        self.reload()

    def _on_only_supply(self, checked: bool) -> None:
        self._only_this_supply = bool(checked)
        self.reload()

    def _selected_ids(self) -> List[int]:
        ids = []  # type: List[int]
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if not item:
                continue
            try:
                cid = int(item.data(Qt.UserRole) or 0)
            except (TypeError, ValueError):
                cid = 0
            if cid > 0:
                ids.append(cid)
        return ids

    def reload(self, *, auto_reconcile: bool = False) -> None:
        try:
            if (
                auto_reconcile
                and not self._reconciled_once
                and self.source_id > 0
                and self.posting_numbers
            ):
                self._reconciled_once = True
                try:
                    self.svc.reconcile_binds(
                        self.client,
                        self.source_id,
                        self.posting_numbers,
                        warehouse_id=self.warehouse_id,
                        carriage_id=self.carriage_id,
                    )
                except Exception as exc:
                    self.info.setText("Сверка: {}".format(exc))

            result = self.svc.list_containers(
                self.client,
                self.carriage_id,
                source_id=self.source_id or None,
                warehouse_id=self.warehouse_id or None,
                include_sc_accepted=self._include_sc_accepted,
                only_active=True,
            )
            self.warehouse_id = int(result.get("warehouse_id") or self.warehouse_id or 0)
            items = list(result.get("items") or [])
            if self.source_id > 0:
                items = self.svc.apply_local_archive_filter(
                    self.source_id,
                    items,
                    include_archived=self._include_archived,
                )
                items = self.svc.enrich_for_supply(
                    items,
                    source_id=self.source_id,
                    posting_numbers=self.posting_numbers,
                    only_this_supply=self._only_this_supply,
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
        if not self.containers:
            self.table.setRowCount(1)
            empty = QTableWidgetItem("Нет активных грузомест")
            empty.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(0, 0, empty)
            self.table.setSpan(0, 0, 1, 4)
            return
        self.table.clearSpans()
        for i, row in enumerate(self.containers):
            cid = int(row.get("container_id") or 0)
            num = row.get("container_number") or "—"
            meta_bits = [
                str(row.get("sort_type_label") or "").strip(),
                str(row.get("cargo_type_label") or "").strip(),
            ]
            meta = " · ".join(b for b in meta_bits if b and b != "—")
            main = "{}\n№ {}{}".format(
                cid,
                num,
                (" · " + meta) if meta else "",
            )
            id_item = QTableWidgetItem(main)
            id_item.setData(Qt.UserRole, cid)
            self.table.setItem(i, 0, id_item)

            orders_ozon = int(
                row.get("order_count_ozon")
                if row.get("order_count_ozon") is not None
                else row.get("order_count")
                or 0
            )
            self.table.setItem(i, 1, QTableWidgetItem(str(orders_ozon)))

            status = str(row.get("status_label") or row.get("status") or "—")
            if row.get("locally_archived"):
                status = "{} · скрыто локально".format(status)
            self.table.setItem(i, 2, QTableWidgetItem(status))

            actions = QWidget()
            lay = QHBoxLayout(actions)
            lay.setContentsMargins(4, 2, 4, 2)
            lay.setSpacing(4)

            approve_btn = QPushButton("✓")
            approve_btn.setObjectName("trbxBoxBtn")
            approve_btn.setCursor(Qt.PointingHandCursor)
            approve_btn.setToolTip("Подтвердить состав грузоместа")
            approve_btn.setEnabled(bool(row.get("can_approve")))
            approve_btn.clicked.connect(partial(self._approve_one, cid))
            print_btn = QPushButton("⎙")
            print_btn.setObjectName("trbxBoxBtn")
            print_btn.setCursor(Qt.PointingHandCursor)
            print_btn.setToolTip("Печать этикетки")
            print_btn.setEnabled(row.get("can_print") is not False)
            print_btn.clicked.connect(partial(self._print_one, cid))
            delete_btn = QPushButton("✕")
            delete_btn.setObjectName("trbxBoxDeleteBtn")
            delete_btn.setCursor(Qt.PointingHandCursor)
            delete_btn.setToolTip("Удалить грузоместо")
            delete_btn.setEnabled(bool(row.get("can_delete")))
            delete_btn.clicked.connect(partial(self._delete_one, cid))
            lay.addWidget(approve_btn)
            lay.addWidget(print_btn)
            lay.addWidget(delete_btn)

            if row.get("locally_archived"):
                restore_btn = QPushButton("⤴")
                restore_btn.setObjectName("trbxBoxBtn")
                restore_btn.setCursor(Qt.PointingHandCursor)
                restore_btn.setToolTip("Вернуть из локального архива")
                restore_btn.clicked.connect(partial(self._restore_one, cid))
                lay.addWidget(restore_btn)
            elif not row.get("is_sc_accepted"):
                archive_btn = QPushButton("⤵")
                archive_btn.setObjectName("trbxBoxBtn")
                archive_btn.setCursor(Qt.PointingHandCursor)
                archive_btn.setToolTip("В локальный архив (без Ozon)")
                archive_btn.clicked.connect(partial(self._archive_one, cid))
                lay.addWidget(archive_btn)

            lay.addStretch(1)
            self.table.setCellWidget(i, 3, actions)

    def create_containers(self) -> None:
        sort_type = str(self.sort_combo.currentData() or "sort")
        try:
            result = self.svc.create(
                self.client,
                self.carriage_id,
                self.amount.value(),
                container_type="pallet",
                sort_type=sort_type,
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

    def _delete_one(self, container_id: int) -> None:
        if (
            QMessageBox.question(
                self,
                "Грузоместа",
                "Удалить грузоместо {}?".format(container_id),
            )
            != QMessageBox.Yes
        ):
            return
        try:
            result = self.svc.delete(self.client, [container_id])
            if result.get("errors"):
                err = result["errors"][0].get("error") or "Ошибка удаления"
                QMessageBox.warning(self, "Грузоместа", str(err))
            else:
                self.info.setText(str(result.get("message") or "Удалено"))
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def _approve_one(self, container_id: int) -> None:
        if self.source_id > 0 and self.posting_numbers:
            unbound = self.svc.unbound_supply_postings(
                self.source_id, self.posting_numbers
            )
            if unbound:
                preview = ", ".join(unbound[:5])
                more = "…" if len(unbound) > 5 else ""
                if (
                    QMessageBox.question(
                        self,
                        "Грузоместа",
                        "Не все отправления поставки в грузоместах "
                        "({}): {}{}.\nВсё равно подтвердить?".format(
                            len(unbound), preview, more
                        ),
                    )
                    != QMessageBox.Yes
                ):
                    return
        elif (
            QMessageBox.question(
                self,
                "Грузоместа",
                "Подтвердить состав грузоместа {}?".format(container_id),
            )
            != QMessageBox.Yes
        ):
            return
        try:
            result = self.svc.approve(self.client, [container_id], wait_task=True)
            if not result.get("ok"):
                err = ""
                errors = result.get("errors") or []
                if errors:
                    err = str(errors[0].get("error") or "")
                QMessageBox.warning(
                    self,
                    "Грузоместа",
                    err or str(result.get("message") or "Ошибка подтверждения"),
                )
            else:
                QMessageBox.information(
                    self, "Грузоместа", str(result.get("message") or "Готово")
                )
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def _print_one(self, container_id: int) -> None:
        try:
            _payload, path = self.svc.labels_pdf(self.client, [container_id])
            self.info.setText("Этикетка сохранена: {}".format(path))
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def _archive_one(self, container_id: int) -> None:
        if self.source_id <= 0:
            QMessageBox.information(
                self, "Грузоместа", "Нужен источник для локального архива."
            )
            return
        try:
            self.svc.archive_locally(self.source_id, container_id)
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def _restore_one(self, container_id: int) -> None:
        if self.source_id <= 0:
            return
        try:
            self.svc.restore_locally(self.source_id, container_id)
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Грузоместа", str(exc))

    def print_supply_barcode(self) -> None:
        """ШК поставки — same entry as supply detail button."""
        parent = self.parent()
        if parent is not None and hasattr(parent, "open_shipments_barcode"):
            try:
                parent.open_shipments_barcode()
                return
            except Exception as exc:
                QMessageBox.warning(self, "Грузоместа", str(exc))
                return
        QMessageBox.information(
            self,
            "Грузоместа",
            "Откройте печать ШК поставки из карточки поставки.",
        )
