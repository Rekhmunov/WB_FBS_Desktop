# -*- coding: utf-8 -*-
"""Ozon «Собрать все заказы» decisions dialog (create / add to existing)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class OzonShipAllDialog(QDialog):
    def __init__(
        self,
        preview: Dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> None:
        super(OzonShipAllDialog, self).__init__(parent)
        self.preview = preview or {}
        self._group_widgets = []  # type: List[Dict[str, Any]]
        self.setWindowTitle("Собрать все заказы")
        self.resize(560, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        title = QLabel("Собрать все заказы")
        title.setObjectName("dialogTitle")
        root.addWidget(title)

        lead = QLabel(
            "Будет собрано {} отправлений по {} складам. "
            "Выберите: создать новую поставку или добавить в существующую.".format(
                int(self.preview.get("posting_count") or 0),
                len(self.preview.get("groups") or []),
            )
        )
        lead.setObjectName("hint")
        lead.setWordWrap(True)
        root.addWidget(lead)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QVBoxLayout(body)
        form.setSpacing(16)
        for g in self.preview.get("groups") or []:
            form.addWidget(self._build_group_card(g))
        form.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Собрать")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_group_card(self, group: Dict[str, Any]) -> QWidget:
        card = QWidget()
        lay = QFormLayout(card)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        head = QLabel(
            "{} · {} отпр.".format(
                group.get("label") or "склад",
                int(group.get("order_count") or 0),
            )
        )
        head.setStyleSheet("font-weight: 600;")
        lay.addRow(head)

        action = QComboBox()
        action.addItem("Создать новую поставку", "create")
        for s in group.get("compatible_supplies") or []:
            action.addItem(
                "Добавить в «{}» ({} шт.)".format(
                    s.get("name") or s.get("supply_id"),
                    int(s.get("orders_count") or 0),
                ),
                "add:{}".format(s.get("supply_id") or ""),
            )
        if str(group.get("mode") or "") == "choose" and group.get("default_supply_id"):
            idx = action.findData(
                "add:{}".format(group.get("default_supply_id"))
            )
            if idx >= 0:
                action.setCurrentIndex(idx)
        lay.addRow("Действие", action)

        name_edit = QLineEdit(str(group.get("suggested_name") or ""))
        lay.addRow("Название новой", name_edit)

        def _toggle(_index: int = 0) -> None:
            name_edit.setEnabled(str(action.currentData() or "").startswith("create"))

        action.currentIndexChanged.connect(_toggle)
        _toggle()

        self._group_widgets.append(
            {"group": group, "action": action, "name": name_edit}
        )
        return card

    def decisions(self) -> List[Dict[str, Any]]:
        out = []  # type: List[Dict[str, Any]]
        for item in self._group_widgets:
            group = item["group"]
            raw = str(item["action"].currentData() or "create")
            if raw.startswith("add:"):
                sid = raw.split(":", 1)[1]
                out.append(
                    {
                        "group_key": group.get("group_key"),
                        "action": "add",
                        "supply_id": sid,
                        "name": item["name"].text().strip(),
                    }
                )
            else:
                out.append(
                    {
                        "group_key": group.get("group_key"),
                        "action": "create",
                        "name": item["name"].text().strip()
                        or group.get("suggested_name"),
                    }
                )
        return out
