# -*- coding: utf-8 -*-
"""Ozon FBS posting card — ship, mark, pick, labels."""
from __future__ import annotations

from typing import Any, Dict, Optional

from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.ozon import status_label
from app.services.ozon_labels import OzonLabelService
from app.services.ozon_mark_pick import OzonMarkService, OzonPickService
from app.services.ozon_orders import OzonOrdersService
from app.services.ozon_ship import OzonShipService, posting_needs_ship
from app.ui.dialog_utils import fullscreen_parent, init_fullscreen_dialog
from app.ui.format_helpers import build_product_cell_widget


class OzonPostingDetailDialog(QDialog):
    def __init__(
        self,
        db: Database,
        orders: OzonOrdersService,
        source: Dict[str, Any],
        posting_number: str,
        parent: Optional[QWidget] = None,
        *,
        fullscreen: bool = True,
    ) -> None:
        super(OzonPostingDetailDialog, self).__init__(
            fullscreen_parent(parent, fullscreen)
        )
        self.db = db
        self.orders = orders
        self.source = source
        self.posting_number = str(posting_number or "").strip()
        self.source_id = int(source["id"])
        self.client = OzonFbsClient(
            str(source.get("client_id") or ""), str(source.get("api_key") or "")
        )
        self.mark = OzonMarkService(db)
        self.pick = OzonPickService(db)
        self.ship = OzonShipService(db)
        self.labels = OzonLabelService(db)
        self.posting_mutated = False
        self._row = self.orders.get_posting(self.source_id, self.posting_number) or {}

        self.setWindowTitle("Отправление {}".format(self.posting_number))
        init_fullscreen_dialog(
            self,
            fullscreen=fullscreen,
            default_size=(720, 520),
            minimum_size=(640, 480),
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        header = QLabel("Отправление {}".format(self.posting_number))
        header.setObjectName("sdTitle")
        root.addWidget(header)

        status = QLabel(
            status_label(str(self._row.get("status") or ""))
        )
        status.setObjectName("hint")
        root.addWidget(status)

        card = QFrame()
        card.setObjectName("toolbarPanel")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(16, 16, 16, 16)
        cv.addWidget(build_product_cell_widget(self._row, photo_size=96))
        if self._row.get("product_count_label"):
            cv.addWidget(QLabel(str(self._row.get("product_count_label"))))
        root.addWidget(card)

        actions = QHBoxLayout()
        self.btn_refresh = QPushButton("↻ Обновить")
        self.btn_refresh.setObjectName("secondary")
        self.btn_refresh.clicked.connect(self.refresh_from_api)
        self.btn_ship = QPushButton("Собрать (ship)")
        self.btn_ship.clicked.connect(self.do_ship)
        self.btn_mark = QPushButton("Маркировка")
        self.btn_mark.setObjectName("secondary")
        self.btn_mark.clicked.connect(self.open_mark)
        self.btn_pick = QPushButton("Проверка ШК")
        self.btn_pick.setObjectName("secondary")
        self.btn_pick.clicked.connect(self.open_pick)
        self.btn_labels = QPushButton("Этикетка")
        self.btn_labels.setObjectName("secondary")
        self.btn_labels.clicked.connect(self.print_label)
        for b in (
            self.btn_refresh,
            self.btn_ship,
            self.btn_mark,
            self.btn_pick,
            self.btn_labels,
        ):
            actions.addWidget(b)
        actions.addStretch(1)
        root.addLayout(actions)

        self.info = QLabel("")
        self.info.setObjectName("syncInfoText")
        self.info.setWordWrap(True)
        root.addWidget(self.info)
        root.addStretch(1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        btn_close = QPushButton("Закрыть")
        btn_close.clicked.connect(self.accept)
        close_row.addWidget(btn_close)
        root.addLayout(close_row)

        self._update_ship_visibility()

    def _update_ship_visibility(self) -> None:
        st = str(self._row.get("status") or "")
        self.btn_ship.setVisible(posting_needs_ship(st))

    def refresh_from_api(self) -> None:
        try:
            fresh = self.client.get_posting(self.posting_number)
            if fresh:
                from app.ozon.sync import upsert_posting

                upsert_posting(self.db, self.source_id, fresh)
                self._row = self.orders.get_posting(
                    self.source_id, self.posting_number
                ) or {}
                self.posting_mutated = True
                self._update_ship_visibility()
                self.info.setText("Данные обновлены из Ozon API.")
        except Exception as exc:
            QMessageBox.warning(self, "Отправление", str(exc))

    def do_ship(self) -> None:
        try:
            self.ship.ship_posting(
                self.client, self.source_id, self.posting_number
            )
            self.posting_mutated = True
            self.refresh_from_api()
            QMessageBox.information(
                self, "Ship", "Отправление {} собрано.".format(self.posting_number)
            )
        except Exception as exc:
            QMessageBox.warning(self, "Ship", str(exc))

    def open_mark(self) -> None:
        from app.ui.ozon_mark_dialog import OzonMarkDialog

        dlg = OzonMarkDialog(
            self.mark,
            self.client,
            self.source_id,
            "",
            self,
            fullscreen=True,
            posting_number=self.posting_number,
        )
        dlg.exec_()
        if dlg.data_changed:
            self.posting_mutated = True
            self._row = self.orders.get_posting(
                self.source_id, self.posting_number
            ) or {}

    def open_pick(self) -> None:
        from app.ui.ozon_pick_dialog import OzonPickDialog

        dlg = OzonPickDialog(
            self.pick,
            self.source_id,
            "",
            self,
            fullscreen=True,
            posting_number=self.posting_number,
        )
        dlg.exec_()
        if dlg.data_changed:
            self.posting_mutated = True

    def print_label(self) -> None:
        try:
            path = self.labels.fetch_labels(
                self.client, self.source_id, [self.posting_number]
            )
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            QMessageBox.warning(self, "Этикетка", str(exc))
