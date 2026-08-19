# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.db import Database
from app.services.orders import OrdersService
from app.services.trbx_stickers import StickersService
from app.wb import default_mgt_supply_name


def show_png_list(
    pngs: List[bytes], title: str, parent: Optional[QWidget] = None
) -> None:
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    # Near-square preview — not a tall strip
    dlg.resize(560, 640)
    dlg.setMinimumSize(440, 480)
    root = QVBoxLayout(dlg)
    root.setContentsMargins(24, 20, 24, 20)
    root.setSpacing(12)
    heading = QLabel(title)
    heading.setObjectName("dialogTitle")
    heading.setWordWrap(True)
    root.addWidget(heading)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(0)  # QFrame.NoFrame
    wrap = QWidget()
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 8, 0)
    lay.setSpacing(16)
    for raw in pngs:
        lab = QLabel()
        lab.setAlignment(Qt.AlignCenter)
        pix = QPixmap()
        pix.loadFromData(raw)
        lab.setPixmap(pix.scaledToWidth(420, Qt.SmoothTransformation))
        lay.addWidget(lab)
    lay.addStretch(1)
    scroll.setWidget(wrap)
    root.addWidget(scroll, 1)
    buttons = QDialogButtonBox(QDialogButtonBox.Close)
    buttons.rejected.connect(dlg.reject)
    root.addWidget(buttons)
    dlg.exec_()


def show_order_stickers(
    api_key: str, order_ids: List[int], parent: Optional[QWidget] = None
) -> None:
    svc = StickersService(Database())  # stickers don't need DB
    items = svc.order_stickers_png(api_key, order_ids)
    pngs = [it["png"] for it in items if it.get("png")]
    if not pngs:
        raise RuntimeError("WB не вернул стикеры")
    show_png_list(pngs, "Стикеры заказов ({})".format(len(pngs)), parent)


def show_supply_qr(
    api_key: str, supply_id: str, parent: Optional[QWidget] = None
) -> None:
    svc = StickersService(Database())
    png = svc.supply_qr_png(api_key, supply_id)
    show_png_list([png], "QR поставки {}".format(supply_id), parent)


class CollectMgtDialog(QDialog):
    def __init__(
        self,
        db: Database,
        orders: OrdersService,
        source: Dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> None:
        super(CollectMgtDialog, self).__init__(parent)
        from app.services.collect_mgt import CollectMgtService

        self.db = db
        self.orders = orders
        self.source = source
        self.svc = CollectMgtService(db, orders)
        self.setWindowTitle("Собрать все МГТ-заказы")
        self.resize(720, 560)
        self.setMinimumSize(560, 440)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        title = QLabel("Собрать все МГТ-заказы")
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        self.lead = QLabel("")
        self.lead.setObjectName("hint")
        self.lead.setWordWrap(True)
        root.addWidget(self.lead)

        self.scroll = QWidget()
        self.form = QVBoxLayout(self.scroll)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(self.scroll)
        root.addWidget(scroll_area, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = buttons.button(QDialogButtonBox.Ok)
        ok.setText("Собрать")
        ok.setObjectName("mgtBtn")
        cancel = buttons.button(QDialogButtonBox.Cancel)
        cancel.setText("Отмена")
        cancel.setObjectName("secondary")
        buttons.accepted.connect(self.do_collect)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._ok_btn = ok
        self._group_widgets = []  # type: List[Dict[str, Any]]
        self._load()

    def _load(self) -> None:
        preview = self.svc.preview(int(self.source["id"]))
        groups = list(preview.get("groups") or [])
        self.lead.setText(
            "МГТ в «Новых»: {} · групп: {}".format(
                preview.get("mgt_count", 0), len(groups)
            )
        )
        # clear form
        while self.form.count():
            item = self.form.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._group_widgets = []
        if not groups:
            self.form.addWidget(QLabel("Нет МГТ-заказов для сбора"))
            if self._ok_btn:
                self._ok_btn.setEnabled(False)
            return
        for g in groups:
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 8, 0, 8)
            title = QLabel(
                "<b>{}</b> — {} зак. · режим: {}".format(
                    g.get("label") or "",
                    g.get("order_count") or 0,
                    {
                        "create": "новая поставка",
                        "add_one": "добавить в существующую",
                        "choose": "выберите поставку",
                    }.get(str(g.get("mode")), str(g.get("mode"))),
                )
            )
            title.setTextFormat(Qt.RichText)
            lay.addWidget(title)
            name_edit = QLineEdit(str(g.get("suggested_name") or ""))
            combo = QComboBox()
            combo.addItem("— создать новую —", "")
            for s in g.get("compatible_supplies") or []:
                combo.addItem(
                    "{} · {} зак.{}".format(
                        s.get("name") or s.get("supply_id"),
                        s.get("orders_count") or 0,
                        " (пустая)" if s.get("is_empty") else "",
                    ),
                    str(s.get("supply_id") or ""),
                )
            mode = str(g.get("mode") or "create")
            if mode == "create":
                lay.addWidget(QLabel("Название новой поставки"))
                lay.addWidget(name_edit)
                combo.hide()
            elif mode == "add_one":
                name_edit.hide()
                default = str(g.get("default_supply_id") or "")
                idx = combo.findData(default)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                lay.addWidget(QLabel("Поставка"))
                lay.addWidget(combo)
            else:
                lay.addWidget(QLabel("Название (если создать новую)"))
                lay.addWidget(name_edit)
                lay.addWidget(QLabel("Или выбрать существующую"))
                lay.addWidget(combo)
            self.form.addWidget(box)
            self._group_widgets.append(
                {"group": g, "name_edit": name_edit, "combo": combo}
            )
        self.form.addStretch(1)

    def do_collect(self) -> None:
        decisions = []
        for item in self._group_widgets:
            g = item["group"]
            sid = str(item["combo"].currentData() or "").strip()
            name = item["name_edit"].text().strip()
            mode = str(g.get("mode") or "create")
            if mode == "choose":
                mode = "add" if sid else "create"
            elif mode == "add_one":
                mode = "add_one"
            else:
                mode = "create"
            decisions.append(
                {
                    "group_key": g.get("group_key"),
                    "mode": mode,
                    "supply_id": sid or g.get("default_supply_id") or "",
                    "name": name or g.get("suggested_name") or "",
                }
            )
        try:
            result = self.svc.execute(
                int(self.source["id"]),
                str(self.source["api_key"]),
                decisions,
            )
            msg = "Создано: {}, добавлено в существующие: {}".format(
                result.get("created", 0), result.get("added", 0)
            )
            errs = result.get("errors") or []
            if errs:
                msg += "\n\nОшибки:\n" + "\n".join(str(e) for e in errs[:5])
                QMessageBox.warning(self, "МГТ", msg)
            else:
                QMessageBox.information(self, "МГТ", msg)
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "МГТ", str(exc))


class SelectionSupplyDialog(QDialog):
    def __init__(
        self,
        orders: OrdersService,
        source: Dict[str, Any],
        order_ids: List[int],
        mode: str = "create",
        parent: Optional[QWidget] = None,
    ) -> None:
        super(SelectionSupplyDialog, self).__init__(parent)
        self.orders = orders
        self.source = source
        self.order_ids = order_ids
        self.mode = mode
        self.setWindowTitle(
            "Новая поставка" if mode == "create" else "Добавить к поставке"
        )
        self.resize(520, 400)
        self.setMinimumSize(440, 320)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        title = QLabel(
            "Новая поставка" if mode == "create" else "Добавить к существующей"
        )
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        count = QLabel("Заказов: {}".format(len(order_ids)))
        count.setObjectName("hint")
        root.addWidget(count)

        # Load order traits
        sid = int(source["id"])
        with orders.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM wb_fbs_orders
                WHERE source_id = ? AND order_id IN ({})
                """.format(
                    ",".join("?" for _ in order_ids)
                ),
                [sid] + list(order_ids),
            ).fetchall()
        items = [dict(r) for r in rows]
        cargos = {int(r.get("cargo_type") or 0) for r in items}
        b2bs = {bool(int(r.get("is_b2b") or 0)) for r in items}
        whs = {r.get("warehouse_id") for r in items}
        errors = []
        if len(cargos) > 1:
            errors.append("Нельзя смешивать типы груза (МГТ/СГТ/КГТ+)")
        if len(b2bs) > 1:
            errors.append("Нельзя смешивать B2B и розницу")
        if len(whs) > 1:
            errors.append("Нельзя смешивать склады")
        self.err = QLabel("\n".join(errors))
        self.err.setObjectName("hint")
        self.err.setStyleSheet("color:#b91c1c; font-size: 15px;")
        self.err.setWordWrap(True)
        root.addWidget(self.err)

        self.name_edit = QLineEdit(
            default_mgt_supply_name(is_b2b=bool(next(iter(b2bs), False)))
        )
        self.supply_combo = QComboBox()
        if mode == "create":
            name_lab = QLabel("Название поставки")
            name_lab.setObjectName("fieldLabel")
            root.addWidget(name_lab)
            root.addWidget(self.name_edit)
        else:
            name_lab = QLabel("Открытая совместимая поставка")
            name_lab.setObjectName("fieldLabel")
            root.addWidget(name_lab)
            root.addWidget(self.supply_combo)
            cargo = next(iter(cargos), 0)
            is_b2b = bool(next(iter(b2bs), False))
            wh = next(iter(whs), None)
            for s in orders.open_compatible_supplies(sid, cargo, is_b2b, wh):
                self.supply_combo.addItem(
                    "{} · {} зак.".format(s.get("name") or s.get("supply_id"), s.get("order_count")),
                    str(s.get("supply_id")),
                )

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = buttons.button(QDialogButtonBox.Ok)
        ok.setText("Создать" if mode == "create" else "Добавить")
        buttons.button(QDialogButtonBox.Cancel).setObjectName("secondary")
        buttons.accepted.connect(self.do_ok)
        buttons.rejected.connect(self.reject)
        if errors:
            ok.setEnabled(False)
        if mode == "add" and self.supply_combo.count() == 0:
            ok.setEnabled(False)
            self.err.setText((self.err.text() + "\nНет совместимых открытых поставок").strip())
        root.addStretch(1)
        root.addWidget(buttons)

    def do_ok(self) -> None:
        try:
            sid = int(self.source["id"])
            key = str(self.source["api_key"])
            if self.mode == "create":
                self.orders.create_supply_from_orders(
                    sid, key, self.order_ids, self.name_edit.text().strip()
                )
            else:
                supply_id = str(self.supply_combo.currentData() or "")
                if not supply_id:
                    raise ValueError("Выберите поставку")
                self.orders.add_orders_to_existing_supply(
                    sid, key, supply_id, self.order_ids
                )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Поставка", str(exc))
