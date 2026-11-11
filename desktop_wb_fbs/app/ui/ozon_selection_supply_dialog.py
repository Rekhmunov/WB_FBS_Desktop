# -*- coding: utf-8 -*-
"""Ozon selection → create / add to local supply (web parity)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)


class OzonSelectionSupplyDialog(QDialog):
    def __init__(
        self,
        preview: Dict[str, Any],
        parent=None,
    ) -> None:
        super(OzonSelectionSupplyDialog, self).__init__(parent)
        self.preview = preview or {}
        self.setWindowTitle("Поставка из выбранных")
        self.resize(480, 280)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        title = QLabel("Поставка из выбранных")
        title.setObjectName("dialogTitle")
        root.addWidget(title)

        lead = QLabel(
            "Подходит отправлений: {}.".format(
                int(self.preview.get("order_count") or 0)
            )
        )
        lead.setObjectName("hint")
        lead.setWordWrap(True)
        root.addWidget(lead)

        form = QFormLayout()
        form.setSpacing(8)
        self.action = QComboBox()
        self.action.addItem("Создать новую поставку", "create")
        for s in self.preview.get("compatible_supplies") or []:
            self.action.addItem(
                "Добавить в «{}» ({} шт.)".format(
                    s.get("name") or s.get("supply_id"),
                    int(s.get("orders_count") or 0),
                ),
                "add:{}".format(s.get("supply_id") or ""),
            )
        form.addRow("Действие", self.action)

        self.name_edit = QLineEdit(str(self.preview.get("suggested_name") or ""))
        form.addRow("Название новой", self.name_edit)
        root.addLayout(form)

        def _toggle(_i: int = 0) -> None:
            self.name_edit.setEnabled(
                str(self.action.currentData() or "").startswith("create")
            )

        self.action.currentIndexChanged.connect(_toggle)
        _toggle()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Собрать")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def decision(self) -> Dict[str, str]:
        raw = str(self.action.currentData() or "create")
        if raw.startswith("add:"):
            return {
                "action": "add",
                "supply_id": raw.split(":", 1)[1],
                "name": self.name_edit.text().strip(),
            }
        return {
            "action": "create",
            "supply_id": "",
            "name": self.name_edit.text().strip()
            or str(self.preview.get("suggested_name") or ""),
        }


def run_selection_supply_flow(
    *,
    parent,
    svc,
    client,
    source_id: int,
    posting_numbers: List[str],
) -> Optional[Dict[str, Any]]:
    preview = svc.preview_selection_supply(source_id, posting_numbers)
    if not preview.get("ok"):
        errs = preview.get("errors") or ["Нельзя собрать выбранные отправления"]
        QMessageBox.warning(
            parent,
            "Поставка",
            "\n".join(str(e) for e in errs[:12]),
        )
        return None
    if int(preview.get("order_count") or 0) <= 0:
        QMessageBox.information(parent, "Поставка", "Нет подходящих отправлений.")
        return None
    dlg = OzonSelectionSupplyDialog(preview, parent)
    if dlg.exec_() != QDialog.Accepted:
        return None
    dec = dlg.decision()
    if dec.get("action") == "add":
        return svc.add_selection_to_supply(
            client, source_id, posting_numbers, str(dec.get("supply_id") or "")
        )
    return svc.create_from_selection(
        client, source_id, posting_numbers, name=str(dec.get("name") or "")
    )
