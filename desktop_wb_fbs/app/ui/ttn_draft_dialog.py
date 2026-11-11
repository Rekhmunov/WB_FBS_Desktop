# -*- coding: utf-8 -*-
"""Local TTN draft editor for WB/Ozon FBS supplies."""
from __future__ import annotations

from typing import Any, Dict, Optional

from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.db import Database
from app.services.ttn_draft import TtnDraftService
from app.ui.dialog_utils import prepare_modal_dialog


class TtnDraftDialog(QDialog):
    def __init__(
        self,
        db: Database,
        *,
        platform: str,
        source_id: int,
        supply_id: str,
        supply_name: str = "",
        warehouse_name: str = "",
        cargo_places: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super(TtnDraftDialog, self).__init__(parent)
        self.svc = TtnDraftService(db)
        self.platform = str(platform or "").strip().lower()
        self.source_id = int(source_id)
        self.supply_id = str(supply_id or "").strip()
        self.saved = False

        self.setWindowTitle("ТН · {}".format(self.supply_id))
        prepare_modal_dialog(
            self, maximized=False, default_size=(560, 640), minimum_size=(480, 520)
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        pre = self.svc.build_prefill(
            self.platform,
            self.source_id,
            self.supply_id,
            supply_name=supply_name,
            warehouse_name=warehouse_name,
            cargo_places=cargo_places,
        )
        draft = pre.get("draft") or {}
        warns = list(draft.get("warnings") or [])
        if warns:
            warn = QLabel("\n".join(warns))
            warn.setWordWrap(True)
            warn.setObjectName("hint")
            root.addWidget(warn)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        self.fields = {}  # type: Dict[str, QLineEdit]
        for key, label in (
            ("title", "Название"),
            ("ttn_date", "Дата ТН"),
            ("driver_name", "Водитель"),
            ("vehicle_number", "Гос. номер"),
            ("vehicle_type", "Тип ТС"),
            ("load_address", "Погрузка"),
            ("unload_address", "Разгрузка"),
            ("cargo_places", "Мест"),
            ("cargo_weight", "Вес"),
            ("cargo_description", "Груз"),
        ):
            edit = QLineEdit(str(draft.get(key) or ""))
            self.fields[key] = edit
            form.addRow(label, edit)
        root.addLayout(form)
        notes_lab = QLabel("Заметки")
        root.addWidget(notes_lab)
        self.notes = QTextEdit()
        self.notes.setPlainText(str(draft.get("notes") or ""))
        self.notes.setMinimumHeight(80)
        root.addWidget(self.notes, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Сохранить черновик")
        buttons.button(QDialogButtonBox.Cancel).setText("Закрыть")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _save(self) -> None:
        draft = {
            "platform": self.platform,
            "source_id": self.source_id,
            "supply_id": self.supply_id,
            "notes": self.notes.toPlainText().strip(),
        }  # type: Dict[str, Any]
        for key, edit in self.fields.items():
            draft[key] = edit.text().strip()
        try:
            result = self.svc.save(draft)
            self.saved = True
            QMessageBox.information(
                self, "ТН", str(result.get("message") or "Сохранено")
            )
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "ТН", str(exc))
