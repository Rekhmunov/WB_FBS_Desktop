# -*- coding: utf-8 -*-
"""Assign catalog driver (+ optional plate) to a WB/Ozon supply."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from app.db import Database
from app.services.supply_drivers import SupplyDriversService
from app.ui.dialog_utils import prepare_modal_dialog


class DriverAssignDialog(QDialog):
    def __init__(
        self,
        db: Database,
        *,
        platform: str,
        source_id: int,
        supply_id: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(DriverAssignDialog, self).__init__(parent)
        self.db = db
        self.svc = SupplyDriversService(db)
        self.platform = str(platform or "").strip().lower()
        self.source_id = int(source_id)
        self.supply_id = str(supply_id or "").strip()
        self.changed = False

        self.setWindowTitle("Водитель · {}".format(self.supply_id))
        prepare_modal_dialog(
            self, maximized=False, default_size=(480, 280), minimum_size=(400, 240)
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        hint = QLabel(
            "Справочник водителей — Настройки → Водители. "
            "Гос. номер нужен, если у водителя несколько машин."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        root.addWidget(hint)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        self.driver_combo = QComboBox()
        self.driver_combo.addItem("— не выбран —", 0)
        self._options = self.svc.options()  # type: List[Dict[str, Any]]
        for opt in self._options:
            self.driver_combo.addItem(str(opt.get("full_name") or ""), int(opt["id"]))
        self.driver_combo.currentIndexChanged.connect(self._reload_plates)
        self.plate_combo = QComboBox()
        form.addRow("Водитель", self.driver_combo)
        form.addRow("Гос. номер", self.plate_combo)
        root.addLayout(form)

        current = self.svc.get_assignment(
            self.platform, self.source_id, self.supply_id
        )
        if current.get("driver_id"):
            idx = self.driver_combo.findData(int(current["driver_id"]))
            if idx >= 0:
                self.driver_combo.setCurrentIndex(idx)
        self._reload_plates()
        if current.get("vehicle_number"):
            pidx = self.plate_combo.findData(str(current["vehicle_number"]))
            if pidx < 0:
                pidx = self.plate_combo.findText(str(current["vehicle_number"]))
            if pidx >= 0:
                self.plate_combo.setCurrentIndex(pidx)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save
            | QDialogButtonBox.Reset
            | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.Reset).setText("Снять")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.button(QDialogButtonBox.Reset).clicked.connect(self._clear)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _reload_plates(self) -> None:
        self.plate_combo.clear()
        did = int(self.driver_combo.currentData() or 0)
        opt = next((o for o in self._options if int(o.get("id") or 0) == did), None)
        plates = list((opt or {}).get("vehicles") or [])
        self.plate_combo.addItem("— не выбран —", "")
        for p in plates:
            num = str(p.get("number") or "").strip()
            if num:
                label = num
                typ = str(p.get("type") or "").strip()
                if typ:
                    label = "{} ({})".format(num, typ)
                self.plate_combo.addItem(label, num)

    def _save(self) -> None:
        did = int(self.driver_combo.currentData() or 0)
        plate = str(self.plate_combo.currentData() or "").strip()
        try:
            self.svc.set_assignment(
                self.platform,
                self.source_id,
                self.supply_id,
                did,
                vehicle_number=plate,
            )
            self.changed = True
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Водитель", str(exc))

    def _clear(self) -> None:
        self.svc.clear_assignment(self.platform, self.source_id, self.supply_id)
        self.changed = True
        self.accept()
