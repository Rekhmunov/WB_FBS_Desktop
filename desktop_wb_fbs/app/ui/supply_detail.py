# -*- coding: utf-8 -*-
from __future__ import annotations

from functools import partial
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QMenu,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
)

from app.db import Database
from app.services.kiz_pick import KizService, PickVerifyService
from app.services.orders import OrdersService
from app.services.trbx_stickers import StickersService, TrbxService
from app.ui.dialog_utils import (
    apply_fullscreen_on_show,
    fullscreen_parent,
    init_fullscreen_dialog,
)
from app.ui.dialogs_extra import show_png_list, show_supply_qr
from app.ui.layout_utils import FlowLayout
from app.wb import cargo_type_label, supply_status_label


class SupplyDetailDialog(QDialog):
    def __init__(
        self,
        db: Database,
        orders: OrdersService,
        source: Dict[str, Any],
        supply_id: str,
        parent: Optional[QWidget] = None,
        *,
        fullscreen: bool = False,
    ) -> None:
        super(SupplyDetailDialog, self).__init__(fullscreen_parent(parent, fullscreen))
        self.db = db
        self.orders = orders
        self.source = source
        self.supply_id = supply_id
        self.source_id = int(source["id"])
        self.api_key = str(source["api_key"])
        self.trbx = TrbxService(db)
        self.stickers = StickersService(db)
        self.kiz = KizService(db)
        self.pick = PickVerifyService(db)

        self.setWindowTitle("Поставка {}".format(supply_id))
        init_fullscreen_dialog(
            self,
            fullscreen=fullscreen,
            default_size=(1040, 720),
            minimum_size=(880, 600),
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header block mirrors web .wb-fbs-sd-header
        header = QFrame()
        header.setObjectName("sdHeader")
        hv = QVBoxLayout(header)
        hv.setContentsMargins(24, 20, 24, 16)
        hv.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        self.header = QLabel("")
        self.header.setObjectName("sdTitle")
        self.header.setWordWrap(True)
        title_row.addWidget(self.header, 1)
        close_x = QPushButton("✕")
        close_x.setObjectName("iconBtn")
        close_x.setToolTip("Закрыть")
        close_x.clicked.connect(self.accept)
        title_row.addWidget(close_x, 0, Qt.AlignTop)
        hv.addLayout(title_row)

        self.warehouse = QLabel("")
        self.warehouse.setObjectName("sdMeta")
        hv.addWidget(self.warehouse)

        self.meta_chips = FlowLayout(h_spacing=8, v_spacing=8)
        hv.addLayout(self.meta_chips)
        # Keep legacy meta label for picking_list status append
        self.meta = QLabel("")
        self.meta.setObjectName("sdMeta")
        self.meta.setWordWrap(True)
        hv.addWidget(self.meta)

        # Wrapping action row — no horizontal squeeze / fixed-height scroll
        actions = FlowLayout(h_spacing=8, v_spacing=8)

        def _sec(btn):
            btn.setObjectName("secondary")
            return btn

        pick_btn = _sec(QPushButton("Лист подбора"))
        pick_btn.clicked.connect(partial(self.picking_list, "summary"))
        pick_caret = QToolButton()
        pick_caret.setObjectName("secondary")
        pick_caret.setText("▾")
        pick_caret.setPopupMode(QToolButton.InstantPopup)
        pick_menu = QMenu(pick_caret)
        pick_menu.addAction(
            "Расширенный лист подбора", partial(self.picking_list, "extended")
        )
        pick_caret.setMenu(pick_menu)
        actions.addWidget(pick_btn)
        actions.addWidget(pick_caret)

        st_btn = _sec(QPushButton("Стикеры"))
        st_btn.clicked.connect(self.print_stickers)
        st_caret = QToolButton()
        st_caret.setObjectName("secondary")
        st_caret.setText("▾")
        st_caret.setPopupMode(QToolButton.InstantPopup)
        st_menu = QMenu(st_caret)
        st_menu.addAction("Печать по категориям", self.stickers_by_category)
        st_caret.setMenu(st_menu)
        actions.addWidget(st_btn)
        actions.addWidget(st_caret)

        kiz_btn = _sec(QPushButton("Маркировка"))
        kiz_btn.clicked.connect(self.open_kiz)
        actions.addWidget(kiz_btn)
        kiz_ref = _sec(QPushButton("↻"))
        kiz_ref.setMinimumWidth(40)
        kiz_ref.setToolTip("Проверить статусы КИЗ на ВБ")
        kiz_ref.clicked.connect(self.refresh_kiz_status)
        actions.addWidget(kiz_ref)

        for text, slot in (
            ("Проверка ШК", self.open_pick),
            ("Грузоместа", self.manage_trbx),
            ("Отменённые заказы", self.show_cancelled),
            ("QR поставки", self.print_qr),
            ("Портал ВБ", self.open_portal),
        ):
            btn = _sec(QPushButton(text))
            btn.clicked.connect(slot)
            actions.addWidget(btn)
        hv.addLayout(actions)
        root.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(24, 16, 24, 20)
        body.setSpacing(12)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(12)
        self.select_all_cb = QCheckBox("Выбрать все")
        self.select_all_cb.stateChanged.connect(self._on_select_all_changed)
        toolbar.addWidget(self.select_all_cb)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск: заказ, артикул, товар, штрихкод…")
        self.search_input.textChanged.connect(lambda _text: self._render_table())
        toolbar.addWidget(self.search_input, 1)
        body.addLayout(toolbar)

        self._all_rows = []  # type: List[Dict[str, Any]]
        self._selected = set()  # type: set

        self.table = QTableWidget(0, 8)
        self.table.setAlternatingRowColors(True)
        self.table.setHorizontalHeaderLabels(
            ["", "Заказ", "Артикул", "Тип", "Цена", "КИЗ", "Проверка", ""]
        )
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(7, 44)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setShowGrid(False)
        body.addWidget(self.table, 1)
        root.addLayout(body, 1)

        self.reload()

    def showEvent(self, event) -> None:
        super(SupplyDetailDialog, self).showEvent(event)
        apply_fullscreen_on_show(self)

    def _clear_chips(self) -> None:
        while self.meta_chips.count():
            item = self.meta_chips.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w:
                w.deleteLater()

    def _add_chip(self, text: str) -> None:
        lab = QLabel(text)
        lab.setObjectName("sdChip")
        lab.setMargin(0)
        self.meta_chips.addWidget(lab)

    def reload(self) -> None:
        supply = self.orders.get_supply(self.source_id, self.supply_id)
        if not supply:
            QMessageBox.warning(self, "Поставка", "Не найдена локально")
            return
        self.header.setText(str(supply.get("name") or self.supply_id))
        self._clear_chips()
        self._add_chip(cargo_type_label(supply.get("cargo_type")) or "—")
        self._add_chip("заказов {}".format(len(supply.get("order_ids") or [])))
        self._add_chip("коробов {}".format(len(supply.get("boxes") or [])))
        self._add_chip(
            supply_status_label(done=supply.get("done"), scan_dt=supply.get("scan_dt"))
        )
        self.meta.setText("ID {}".format(self.supply_id))
        self._all_rows = self.orders.orders_in_supply(
            self.source_id, self.supply_id, api_key=self.api_key
        )
        if self._all_rows:
            wh = str(
                self._all_rows[0].get("warehouse_label")
                or self._all_rows[0].get("warehouse_id")
                or ""
            )
            self.warehouse.setText(wh or "—")
        else:
            self.warehouse.setText("—")
        valid_ids = {int(r.get("order_id")) for r in self._all_rows}
        self._selected = {oid for oid in self._selected if oid in valid_ids}
        self._render_table()

    @staticmethod
    def _row_matches_search(row: Dict[str, Any], query: str) -> bool:
        q = str(query or "").strip().lower()
        if not q:
            return True
        hay = [
            row.get("order_id"),
            row.get("article"),
            row.get("product_name"),
        ]
        hay.extend(row.get("skus") or [])
        return any(q in str(v or "").strip().lower() for v in hay)

    def _visible_rows(self) -> List[Dict[str, Any]]:
        query = self.search_input.text()
        if not query.strip():
            return list(self._all_rows)
        return [r for r in self._all_rows if self._row_matches_search(r, query)]

    def _render_table(self) -> None:
        rows = self._visible_rows()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            oid = int(r.get("order_id"))
            cb = QCheckBox()
            cb.setChecked(oid in self._selected)
            cb.stateChanged.connect(partial(self._on_row_checked, oid))
            cb_wrap = QWidget()
            cb_lay = QHBoxLayout(cb_wrap)
            cb_lay.setContentsMargins(0, 0, 0, 0)
            cb_lay.addWidget(cb)
            cb_lay.setAlignment(Qt.AlignCenter)
            self.table.setCellWidget(i, 0, cb_wrap)
            oid_item = QTableWidgetItem(str(oid))
            f = oid_item.font()
            f.setBold(True)
            oid_item.setFont(f)
            self.table.setItem(i, 1, oid_item)
            name = str(r.get("product_name") or r.get("article") or "")
            self.table.setItem(i, 2, QTableWidgetItem(name))
            self.table.setItem(i, 3, QTableWidgetItem(str(r.get("cargo_label") or "")))
            self.table.setItem(i, 4, QTableWidgetItem(str(r.get("price_label") or "")))
            codes = [c for c in (r.get("kiz_codes") or []) if str(c).strip(" \t\r\n")]
            self.table.setItem(i, 5, QTableWidgetItem(str(len(codes)) if codes else "—"))
            self.table.setItem(
                i, 6, QTableWidgetItem("да" if r.get("pick_verified") else "—")
            )
            self.table.setCellWidget(i, 7, self._build_row_menu(oid))
        self._sync_select_all()

    def _build_row_menu(self, order_id: int) -> QWidget:
        btn = QToolButton()
        btn.setObjectName("secondary")
        btn.setText("⋮")
        btn.setToolTip("Действия")
        btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(btn)
        menu.addAction(
            "Напечатать стикер", partial(self.print_one_order_sticker, order_id)
        )
        btn.setMenu(menu)
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(btn)
        lay.setAlignment(Qt.AlignCenter)
        return wrap

    def print_one_order_sticker(self, order_id: int) -> None:
        try:
            items = self.stickers.order_stickers_png(self.api_key, [int(order_id)])
            pngs = [it["png"] for it in items if it.get("png")]
            if not pngs:
                raise RuntimeError("WB не вернул стикер для заказа {}".format(order_id))
            show_png_list(pngs, "Стикер заказа {}".format(order_id), self)
        except Exception as exc:
            QMessageBox.critical(self, "Стикер", str(exc))

    def _on_row_checked(self, order_id: int, state: int) -> None:
        if state:
            self._selected.add(order_id)
        else:
            self._selected.discard(order_id)
        self._sync_select_all()

    def _sync_select_all(self) -> None:
        ids = [int(r.get("order_id")) for r in self._visible_rows()]
        cb = self.select_all_cb
        cb.blockSignals(True)
        if not ids:
            cb.setChecked(False)
        else:
            cb.setChecked(all(oid in self._selected for oid in ids))
        cb.blockSignals(False)

    def _on_select_all_changed(self, state: int) -> None:
        ids = [int(r.get("order_id")) for r in self._visible_rows()]
        if state:
            self._selected.update(ids)
        else:
            self._selected.difference_update(ids)
        self._render_table()

    def picking_list(self, variant: str = "summary") -> None:
        from app.services.print_docs import print_picking_list

        from PyQt5.QtGui import QCursor
        from PyQt5.QtWidgets import QApplication

        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            path = print_picking_list(
                self.db,
                self.orders,
                self.source_id,
                self.api_key,
                self.supply_id,
                variant=variant,
            )
            self.meta.setText(self.meta.text() + " · открыт {}".format(path.name))
        except Exception as exc:
            QMessageBox.critical(self, "Лист подбора", str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def print_stickers(self) -> None:
        from app.services.print_docs import print_supply_stickers

        from PyQt5.QtGui import QCursor
        from PyQt5.QtWidgets import QApplication

        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            print_supply_stickers(
                self.db,
                self.orders,
                self.source_id,
                self.api_key,
                self.supply_id,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Стикеры", str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def print_qr(self) -> None:
        try:
            show_supply_qr(self.api_key, self.supply_id, self)
        except Exception as exc:
            QMessageBox.critical(self, "QR", str(exc))

    def open_portal(self) -> None:
        from PyQt5.QtCore import QUrl
        from PyQt5.QtGui import QDesktopServices

        url = (
            "https://seller.wildberries.ru/marketplace-orders-fbs/supply-detail/packaging"
            "?supplyID={}".format(self.supply_id)
        )
        QDesktopServices.openUrl(QUrl(url))

    def show_cancelled(self) -> None:
        from app.services.cancelled import list_cancelled_in_supply

        try:
            data = list_cancelled_in_supply(
                self.db, self.source_id, self.api_key, self.supply_id
            )
        except Exception as exc:
            QMessageBox.critical(self, "Отменённые", str(exc))
            return
        rows = data.get("rows") or []
        dlg = QDialog(self)
        dlg.setWindowTitle("Отменённые заказы · {}".format(self.supply_id))
        dlg.resize(720, 520)
        dlg.setMinimumSize(560, 400)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        title = QLabel("Отменённые заказы")
        title.setObjectName("dialogTitle")
        lay.addWidget(title)
        head_row = QHBoxLayout()
        head_row.setSpacing(12)
        lead = QLabel("Найдено отменённых в поставке: {}".format(len(rows)))
        lead.setObjectName("hint")
        head_row.addWidget(lead, 1)
        rerun_btn = QPushButton("Перезапустить проверку")
        rerun_btn.setObjectName("secondary")
        head_row.addWidget(rerun_btn, 0)
        lay.addLayout(head_row)
        table = QTableWidget(len(rows), 3)
        table.setAlternatingRowColors(True)
        table.setHorizontalHeaderLabels(["Заказ", "Артикул", "Причина"])
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(40)

        def _fill(items: List[Dict[str, Any]]) -> None:
            table.setRowCount(len(items))
            for i, r in enumerate(items):
                table.setItem(i, 0, QTableWidgetItem(str(r.get("order_id"))))
                table.setItem(i, 1, QTableWidgetItem(str(r.get("article") or "")))
                table.setItem(i, 2, QTableWidgetItem(str(r.get("cancel_reason") or "")))
            lead.setText("Найдено отменённых в поставке: {}".format(len(items)))

        def _rerun() -> None:
            rerun_btn.setEnabled(False)
            try:
                fresh = list_cancelled_in_supply(
                    self.db, self.source_id, self.api_key, self.supply_id
                )
                _fill(fresh.get("rows") or [])
            except Exception as exc:
                QMessageBox.critical(dlg, "Отменённые", str(exc))
            finally:
                rerun_btn.setEnabled(True)

        rerun_btn.clicked.connect(_rerun)
        _fill(rows)
        lay.addWidget(table, 1)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(dlg.reject)
        lay.addWidget(close)
        dlg.exec_()
        self.reload()

    def stickers_by_category(self) -> None:
        from app.services.print_docs import (
            print_supply_stickers,
            sticker_groups_for_category_print,
        )

        try:
            groups = sticker_groups_for_category_print(
                self.db, self.orders, self.source_id, self.api_key, self.supply_id
            )
        except Exception as exc:
            QMessageBox.critical(self, "Стикеры", str(exc))
            return
        if not groups:
            QMessageBox.information(self, "Стикеры", "Нет товаров для печати")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Стикеры по категориям")
        dlg.resize(680, 560)
        dlg.setMinimumSize(560, 440)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        title = QLabel("Печать по категориям")
        title.setObjectName("dialogTitle")
        lay.addWidget(title)
        hint = QLabel("Отметьте группы товаров для печати стикеров.")
        hint.setObjectName("hint")
        lay.addWidget(hint)
        table = QTableWidget(len(groups), 4)
        table.setAlternatingRowColors(True)
        table.setHorizontalHeaderLabels(["", "Категория", "Товар", "Шт"])
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(40)
        for i, g in enumerate(groups):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked)
            chk.setData(Qt.UserRole, list(g.get("order_ids") or []))
            table.setItem(i, 0, chk)
            table.setItem(i, 1, QTableWidgetItem(str(g.get("category") or "")))
            table.setItem(
                i,
                2,
                QTableWidgetItem(
                    "{} · {}".format(g.get("product_name") or "", g.get("article") or "")
                ),
            )
            table.setItem(i, 3, QTableWidgetItem(str(g.get("qty") or 0)))
        lay.addWidget(table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Печать выбранных")
        buttons.button(QDialogButtonBox.Cancel).setObjectName("secondary")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec_() != QDialog.Accepted:
            return
        order_ids = []
        for i in range(table.rowCount()):
            item = table.item(i, 0)
            if item and item.checkState() == Qt.Checked:
                order_ids.extend(item.data(Qt.UserRole) or [])
        if not order_ids:
            QMessageBox.information(self, "Стикеры", "Ничего не выбрано")
            return
        try:
            print_supply_stickers(
                self.db,
                self.orders,
                self.source_id,
                self.api_key,
                self.supply_id,
                order_ids=order_ids,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Стикеры", str(exc))

    def refresh_kiz_status(self) -> None:
        try:
            result = self.kiz.refresh_statuses(
                self.source_id, self.api_key, self.supply_id
            )
            QMessageBox.information(
                self,
                "Статусы",
                "Обновлено заказов: {} · отменённых: {}".format(
                    result.get("updated", 0), result.get("cancelled", 0)
                ),
            )
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Статусы", str(exc))

    def manage_trbx(self) -> None:
        supply = self.orders.get_supply(self.source_id, self.supply_id) or {}
        order_count = len(supply.get("order_ids") or [])
        dlg = TrbxDialog(
            self.trbx,
            self.source_id,
            self.api_key,
            self.supply_id,
            order_count=order_count,
            parent=self,
        )
        dlg.exec_()
        self.reload()

    def open_kiz(self) -> None:
        from app.ui.kiz_pick_dialogs import KizDialog

        dlg = KizDialog(
            self.kiz, self.source_id, self.api_key, self.supply_id, fullscreen=True
        )
        dlg.exec_()
        self.reload()

    def open_pick(self) -> None:
        from app.ui.kiz_pick_dialogs import PickDialog

        dlg = PickDialog(
            self.pick, self.source_id, self.api_key, self.supply_id, fullscreen=True
        )
        dlg.exec_()
        self.reload()


class TrbxDialog(QDialog):
    """Грузоместа (TRBX) — web parity: stepper, boxes table, QR / delete actions."""

    def __init__(
        self,
        trbx: TrbxService,
        source_id: int,
        api_key: str,
        supply_id: str,
        order_count: int = 0,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(TrbxDialog, self).__init__(parent)
        self.trbx = trbx
        self.source_id = source_id
        self.api_key = api_key
        self.supply_id = supply_id
        self.order_count = int(order_count or 0)
        self.boxes = []  # type: List[Dict[str, Any]]

        self.setWindowTitle("Грузоместа для поставки в ПВЗ")
        self.resize(720, 560)
        self.setMinimumSize(560, 440)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title = QLabel("Создайте грузоместа для поставки в ПВЗ")
        title.setObjectName("dialogTitle")
        title.setWordWrap(True)
        root.addWidget(title)

        lead = QLabel(
            "1 короб = 1 грузоместо. Затем распечатайте QR-коды грузомест и наклейте"
            " их на каждый короб. Сделайте это до передачи в доставку."
        )
        lead.setObjectName("hint")
        lead.setWordWrap(True)
        root.addWidget(lead)

        stepper_row = QHBoxLayout()
        stepper_row.setSpacing(8)
        minus_btn = QPushButton("−")
        minus_btn.setObjectName("secondary")
        minus_btn.setFixedWidth(44)
        minus_btn.clicked.connect(partial(self._step_amount, -1))
        self.amount = QSpinBox()
        self.amount.setRange(1, 1000)
        self.amount.setValue(1)
        self.amount.setMinimumWidth(88)
        plus_btn = QPushButton("+")
        plus_btn.setObjectName("secondary")
        plus_btn.setFixedWidth(44)
        plus_btn.clicked.connect(partial(self._step_amount, 1))
        create_btn = QPushButton("Создать")
        create_btn.clicked.connect(self.create_boxes)
        stepper_row.addWidget(minus_btn)
        stepper_row.addWidget(self.amount)
        stepper_row.addWidget(plus_btn)
        stepper_row.addSpacing(8)
        stepper_row.addWidget(create_btn)
        stepper_row.addStretch(1)
        root.addLayout(stepper_row)

        self.info = QLabel("")
        self.info.setObjectName("hint")
        self.info.setWordWrap(True)
        root.addWidget(self.info)

        self.table = QTableWidget(0, 2)
        self.table.setAlternatingRowColors(True)
        self.table.setHorizontalHeaderLabels(["Грузоместо", "Действия"])
        self.table.setColumnWidth(1, 96)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        root.addWidget(self.table, 1)

        action_bar = FlowLayout(h_spacing=8, v_spacing=8)
        self.print_all_btn = QPushButton("Распечатать QR-коды грузовых мест")
        self.print_all_btn.setObjectName("secondary")
        self.print_all_btn.clicked.connect(self.print_stickers)
        self.print_supply_qr_btn = QPushButton("Распечатать QR-код поставки")
        self.print_supply_qr_btn.setObjectName("secondary")
        self.print_supply_qr_btn.setToolTip("QR-код поставки (WB-GI)")
        self.print_supply_qr_btn.clicked.connect(self.print_supply_qr)
        self.delete_all_btn = QPushButton("Удалить все")
        self.delete_all_btn.setObjectName("danger")
        self.delete_all_btn.clicked.connect(self.delete_all)
        action_bar.addWidget(self.print_all_btn)
        action_bar.addWidget(self.print_supply_qr_btn)
        action_bar.addWidget(self.delete_all_btn)
        root.addLayout(action_bar)

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        root.addWidget(close)
        self.reload()

    def _remaining(self) -> Optional[int]:
        if self.order_count <= 0:
            return None
        return max(0, self.order_count + 1 - len(self.boxes))

    def _step_amount(self, delta: int) -> None:
        self.amount.setValue(self.amount.value() + delta)

    def _sync_amount_range(self) -> None:
        remaining = self._remaining()
        if remaining is None:
            self.amount.setMaximum(1000)
            return
        self.amount.setMaximum(max(1, remaining))
        if remaining < 1:
            self.info.setText(
                "Лимит грузомест достигнут (макс. {} = заказы + 1)".format(
                    self.order_count + 1
                )
            )
        else:
            self.info.setText("Можно добавить ещё: {}".format(remaining))

    @staticmethod
    def _box_id(box: Any) -> str:
        if isinstance(box, dict):
            return str(box.get("id") or box.get("trbxId") or "").strip()
        return str(box or "").strip()

    def reload(self) -> None:
        try:
            boxes = self.trbx.refresh(self.source_id, self.api_key, self.supply_id)
        except Exception:
            boxes = self.trbx.list_boxes(self.source_id, self.supply_id)
        self.boxes = [b for b in boxes if self._box_id(b)]
        self._render_boxes()
        self._sync_amount_range()

    def _render_boxes(self) -> None:
        self.table.setRowCount(len(self.boxes))
        for i, b in enumerate(self.boxes):
            bid = self._box_id(b)
            self.table.setItem(i, 0, QTableWidgetItem(bid))
            self.table.setCellWidget(i, 1, self._build_box_actions(bid))
        has_boxes = bool(self.boxes)
        self.print_all_btn.setEnabled(has_boxes)
        self.delete_all_btn.setEnabled(has_boxes)

    def _build_box_actions(self, box_id: str) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)
        print_btn = QToolButton()
        print_btn.setObjectName("secondary")
        print_btn.setText("QR")
        print_btn.setToolTip("Печать QR грузоместа {}".format(box_id))
        print_btn.clicked.connect(partial(self.print_one_box, box_id))
        delete_btn = QToolButton()
        delete_btn.setObjectName("dangerToolBtn")
        delete_btn.setText("✕")
        delete_btn.setToolTip("Удалить грузоместо {}".format(box_id))
        delete_btn.clicked.connect(partial(self.delete_one_box, box_id))
        lay.addWidget(print_btn)
        lay.addWidget(delete_btn)
        lay.addStretch(1)
        return wrap

    def create_boxes(self) -> None:
        try:
            self.trbx.create(
                self.source_id, self.api_key, self.supply_id, int(self.amount.value())
            )
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Грузоместа", str(exc))

    def delete_one_box(self, box_id: str) -> None:
        if (
            QMessageBox.question(
                self, "Грузоместа", "Удалить грузоместо {}?".format(box_id)
            )
            != QMessageBox.Yes
        ):
            return
        try:
            self.trbx.delete_one(self.source_id, self.api_key, self.supply_id, box_id)
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Грузоместа", str(exc))

    def delete_all(self) -> None:
        if not self.boxes:
            return
        if (
            QMessageBox.question(self, "Грузоместа", "Удалить все грузоместа?")
            != QMessageBox.Yes
        ):
            return
        try:
            self.trbx.delete_all(self.source_id, self.api_key, self.supply_id)
            self.reload()
        except Exception as exc:
            QMessageBox.critical(self, "Грузоместа", str(exc))

    def print_one_box(self, box_id: str) -> None:
        try:
            pngs = self.trbx.stickers_png(self.api_key, self.supply_id, [box_id])
            if not pngs:
                raise RuntimeError("WB не вернул QR для грузоместа {}".format(box_id))
            show_png_list(pngs, "QR грузоместа {}".format(box_id), self)
        except Exception as exc:
            QMessageBox.critical(self, "Грузоместа", str(exc))

    def print_stickers(self) -> None:
        ids = [self._box_id(b) for b in self.boxes if self._box_id(b)]
        if not ids:
            QMessageBox.information(self, "Грузоместа", "Нет грузомест")
            return
        try:
            pngs = self.trbx.stickers_png(self.api_key, self.supply_id, ids)
            show_png_list(pngs, "QR-коды грузовых мест", self)
        except Exception as exc:
            QMessageBox.critical(self, "Грузоместа", str(exc))

    def print_supply_qr(self) -> None:
        try:
            show_supply_qr(self.api_key, self.supply_id, self)
        except Exception as exc:
            QMessageBox.critical(self, "QR поставки", str(exc))
