# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QComboBox,
)

from app.db import Database
from app.ui.dialog_utils import prepare_modal_dialog
from app.services import SourceService
from app.services.catalog import CategoryService, ProductService


class SettingsPage(QWidget):
    sources_changed = pyqtSignal()

    def __init__(
        self,
        db: Database,
        sources: SourceService,
        products: ProductService,
        categories: CategoryService,
    ) -> None:
        super(SettingsPage, self).__init__()
        self.db = db
        self.sources = sources
        self.products = products
        self.categories = categories

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)
        hint = QLabel(
            "Нужны для работы ВБ ФБС: источники (токен Marketplace), товары "
            "(фото, короба, пропуск GTIN), категории (коробов на палете)."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        layout.addWidget(hint)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.tabBar().setElideMode(Qt.ElideNone)
        tabs.tabBar().setUsesScrollButtons(True)
        tabs.tabBar().setExpanding(False)
        tabs.addTab(self._build_sources_tab(), "Источники")
        tabs.addTab(self._build_products_tab(), "Товары")
        tabs.addTab(self._build_categories_tab(), "Категории")
        layout.addWidget(tabs, 1)

    # --- Sources ---
    def _build_sources_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        add = QPushButton("Добавить")
        add.clicked.connect(self.add_source)
        edit = QPushButton("Изменить")
        edit.setObjectName("secondary")
        edit.clicked.connect(self.edit_source)
        delete = QPushButton("Удалить")
        delete.setObjectName("danger")
        delete.clicked.connect(self.delete_source)
        bar.addWidget(add)
        bar.addWidget(edit)
        bar.addWidget(delete)
        bar.addStretch(1)
        v.addLayout(bar)
        self.src_table = QTableWidget(0, 4)
        self.src_table.setHorizontalHeaderLabels(
            ["Название", "Включён", "Ключ", "Последняя синхр."]
        )
        self.src_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.src_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.src_table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.src_table, 1)
        note = QLabel(
            "В названии обязательно «ФБС» или FBS. Токен — категория Marketplace "
            "(и Контент для названий товаров)."
        )
        note.setWordWrap(True)
        note.setObjectName("hint")
        v.addWidget(note)
        self.reload_sources_table()
        return w

    def reload_sources_table(self) -> None:
        rows = self.sources.list_all()
        self.src_table.setRowCount(len(rows))
        for i, s in enumerate(rows):
            self.src_table.setItem(i, 0, QTableWidgetItem(str(s.get("name") or "")))
            self.src_table.item(i, 0).setData(Qt.UserRole, int(s["id"]))
            self.src_table.setItem(
                i, 1, QTableWidgetItem("да" if int(s.get("is_enabled") or 0) else "нет")
            )
            key = str(s.get("api_key") or "")
            masked = (key[:4] + "…" + key[-4:]) if len(key) > 10 else ("*" * len(key))
            self.src_table.setItem(i, 2, QTableWidgetItem(masked))
            self.src_table.setItem(
                i, 3, QTableWidgetItem(str(s.get("last_synced_at") or "—"))
            )

    def _selected_source_id(self) -> Optional[int]:
        row = self.src_table.currentRow()
        if row < 0:
            return None
        item = self.src_table.item(row, 0)
        if not item:
            return None
        return int(item.data(Qt.UserRole))

    def add_source(self) -> None:
        dlg = SourceEditDialog(self)
        if dlg.exec_():
            try:
                self.sources.create(dlg.name, dlg.api_key, dlg.enabled)
                self.reload_sources_table()
                self.sources_changed.emit()
            except Exception as exc:
                QMessageBox.warning(self, "Источник", str(exc))

    def edit_source(self) -> None:
        sid = self._selected_source_id()
        if sid is None:
            return
        src = self.sources.get(sid)
        if not src:
            return
        dlg = SourceEditDialog(self, src)
        if dlg.exec_():
            try:
                self.sources.update(sid, dlg.name, dlg.api_key, dlg.enabled)
                self.reload_sources_table()
                self.sources_changed.emit()
            except Exception as exc:
                QMessageBox.warning(self, "Источник", str(exc))

    def delete_source(self) -> None:
        sid = self._selected_source_id()
        if sid is None:
            return
        if (
            QMessageBox.question(
                self,
                "Удалить",
                "Удалить источник и локальные заказы/поставки?",
            )
            != QMessageBox.Yes
        ):
            return
        self.sources.delete(sid)
        self.reload_sources_table()
        self.sources_changed.emit()

    # --- Products ---
    def _build_products_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        add = QPushButton("Добавить")
        add.clicked.connect(self.add_product)
        edit = QPushButton("Изменить")
        edit.setObjectName("secondary")
        edit.clicked.connect(self.edit_product)
        delete = QPushButton("Удалить")
        delete.setObjectName("danger")
        delete.clicked.connect(self.delete_product)
        bar.addWidget(add)
        bar.addWidget(edit)
        bar.addWidget(delete)
        bar.addStretch(1)
        v.addLayout(bar)
        self.prod_table = QTableWidget(0, 6)
        self.prod_table.setHorizontalHeaderLabels(
            ["Название", "Артикул", "nmID", "В коробе", "Категория", "Без GTIN"]
        )
        self.prod_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.prod_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.prod_table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.prod_table, 1)
        self.reload_products_table()
        return w

    def reload_products_table(self) -> None:
        rows = self.products.list_all()
        self.prod_table.setRowCount(len(rows))
        for i, p in enumerate(rows):
            self.prod_table.setItem(i, 0, QTableWidgetItem(str(p.get("name") or "")))
            self.prod_table.item(i, 0).setData(Qt.UserRole, int(p["id"]))
            self.prod_table.setItem(
                i, 1, QTableWidgetItem(str(p.get("supplier_article") or ""))
            )
            self.prod_table.setItem(i, 2, QTableWidgetItem(str(p.get("wb_nmid") or "")))
            self.prod_table.setItem(
                i, 3, QTableWidgetItem(str(p.get("box_qty") or ""))
            )
            self.prod_table.setItem(
                i, 4, QTableWidgetItem(str(p.get("product_category") or ""))
            )
            self.prod_table.setItem(
                i,
                5,
                QTableWidgetItem("да" if p.get("skip_kiz_gtin_check") else ""),
            )

    def _selected_product_id(self) -> Optional[int]:
        row = self.prod_table.currentRow()
        if row < 0:
            return None
        item = self.prod_table.item(row, 0)
        return int(item.data(Qt.UserRole)) if item else None

    def add_product(self) -> None:
        dlg = ProductEditDialog(self.categories.names(), self)
        if dlg.exec_():
            try:
                self.products.save(
                    None,
                    dlg.name,
                    dlg.article,
                    dlg.nmid,
                    dlg.box_qty,
                    dlg.category,
                    dlg.skip_gtin,
                    dlg.photo_path,
                )
                self.reload_products_table()
            except Exception as exc:
                QMessageBox.warning(self, "Товар", str(exc))

    def edit_product(self) -> None:
        pid = self._selected_product_id()
        if pid is None:
            return
        p = self.products.get(pid)
        if not p:
            return
        dlg = ProductEditDialog(self.categories.names(), self, p)
        if dlg.exec_():
            try:
                self.products.save(
                    pid,
                    dlg.name,
                    dlg.article,
                    dlg.nmid,
                    dlg.box_qty,
                    dlg.category,
                    dlg.skip_gtin,
                    dlg.photo_path,
                )
                self.reload_products_table()
            except Exception as exc:
                QMessageBox.warning(self, "Товар", str(exc))

    def delete_product(self) -> None:
        pid = self._selected_product_id()
        if pid is None:
            return
        if QMessageBox.question(self, "Удалить", "Удалить товар?") != QMessageBox.Yes:
            return
        self.products.delete(pid)
        self.reload_products_table()

    # --- Categories ---
    def _build_categories_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        add = QPushButton("Добавить строку")
        add.clicked.connect(self._cat_add_row)
        save = QPushButton("Сохранить")
        save.clicked.connect(self._cat_save)
        bar.addWidget(add)
        bar.addWidget(save)
        bar.addStretch(1)
        v.addLayout(bar)
        self.cat_table = QTableWidget(0, 2)
        self.cat_table.setHorizontalHeaderLabels(["Название", "Коробов на палете"])
        self.cat_table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.cat_table, 1)
        note = QLabel(
            "Используется для оценки палет после синхронизации и печати стикеров по категориям."
        )
        note.setObjectName("hint")
        v.addWidget(note)
        self.reload_categories_table()
        return w

    def reload_categories_table(self) -> None:
        rows = self.categories.list_all()
        self.cat_table.setRowCount(len(rows))
        for i, c in enumerate(rows):
            self.cat_table.setItem(i, 0, QTableWidgetItem(str(c.get("name") or "")))
            self.cat_table.setItem(
                i, 1, QTableWidgetItem(str(c.get("boxes_per_pallet") or ""))
            )

    def _cat_add_row(self) -> None:
        r = self.cat_table.rowCount()
        self.cat_table.insertRow(r)
        self.cat_table.setItem(r, 0, QTableWidgetItem(""))
        self.cat_table.setItem(r, 1, QTableWidgetItem(""))

    def _cat_save(self) -> None:
        items = []  # type: List[Dict[str, Any]]
        for r in range(self.cat_table.rowCount()):
            name_item = self.cat_table.item(r, 0)
            boxes_item = self.cat_table.item(r, 1)
            name = name_item.text().strip() if name_item else ""
            boxes_raw = boxes_item.text().strip() if boxes_item else ""
            boxes = None
            if boxes_raw:
                try:
                    boxes = int(boxes_raw)
                except ValueError:
                    QMessageBox.warning(
                        self, "Категории", "Неверное число коробов в строке {}".format(r + 1)
                    )
                    return
            if name:
                items.append({"name": name, "boxes_per_pallet": boxes})
        self.categories.save_all(items)
        self.reload_categories_table()
        QMessageBox.information(self, "Категории", "Сохранено")


class SourceEditDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, src: Optional[Dict] = None) -> None:
        super(SourceEditDialog, self).__init__(parent)
        self.setWindowTitle("Источник WB FBS")
        prepare_modal_dialog(
            self,
            maximized=True,
            default_size=(480, 320),
            minimum_size=(420, 280),
        )
        self.name = ""
        self.api_key = ""
        self.enabled = True
        form = QFormLayout(self)
        form.setContentsMargins(24, 24, 24, 24)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(16)
        self.name_edit = QLineEdit(str((src or {}).get("name") or "Кабинет ФБС"))
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText(
            "Оставьте пустым, чтобы не менять" if src else "API-ключ Marketplace"
        )
        if src and src.get("api_key"):
            self.key_edit.setText("")  # don't show full key; require re-entry to change
        self.enabled_chk = QCheckBox("Включён")
        self.enabled_chk.setChecked(bool(int((src or {}).get("is_enabled", 1))))
        form.addRow("Название", self.name_edit)
        form.addRow("API-ключ", self.key_edit)
        form.addRow("", self.enabled_chk)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Cancel).setObjectName("secondary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._src = src

    def accept(self) -> None:
        self.name = self.name_edit.text().strip()
        self.api_key = self.key_edit.text().strip()
        if self._src and not self.api_key:
            self.api_key = str(self._src.get("api_key") or "")
        self.enabled = self.enabled_chk.isChecked()
        super(SourceEditDialog, self).accept()


class ProductEditDialog(QDialog):
    def __init__(
        self,
        categories: List[str],
        parent: Optional[QWidget] = None,
        product: Optional[Dict] = None,
    ) -> None:
        super(ProductEditDialog, self).__init__(parent)
        self.setWindowTitle("Товар")
        prepare_modal_dialog(
            self,
            maximized=True,
            default_size=(560, 480),
            minimum_size=(480, 400),
        )
        self.photo_path = None  # type: Optional[str]
        form = QFormLayout(self)
        form.setContentsMargins(24, 24, 24, 24)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        p = product or {}
        self.name_edit = QLineEdit(str(p.get("name") or ""))
        self.article_edit = QLineEdit(str(p.get("supplier_article") or ""))
        self.nmid_edit = QLineEdit(str(p.get("wb_nmid") or ""))
        self.box_spin = QSpinBox()
        self.box_spin.setRange(0, 100000)
        if p.get("box_qty"):
            self.box_spin.setValue(int(p["box_qty"]))
        self.cat_combo = QComboBox()
        self.cat_combo.setEditable(True)
        self.cat_combo.addItem("")
        for c in categories:
            self.cat_combo.addItem(c)
        idx = self.cat_combo.findText(str(p.get("product_category") or ""))
        if idx >= 0:
            self.cat_combo.setCurrentIndex(idx)
        else:
            self.cat_combo.setEditText(str(p.get("product_category") or ""))
        self.skip_chk = QCheckBox("Без проверки GTIN маркировки (не сверять с ШК)")
        self.skip_chk.setChecked(bool(p.get("skip_kiz_gtin_check")))
        photo_btn = QPushButton("Выбрать фото…")
        photo_btn.setObjectName("secondary")
        photo_btn.clicked.connect(self._pick_photo)
        self.photo_label = QLabel(str(p.get("photo_path") or "—"))
        form.addRow("Название", self.name_edit)
        form.addRow("Артикул продавца", self.article_edit)
        form.addRow("nmID WB", self.nmid_edit)
        form.addRow("Штук в коробе", self.box_spin)
        form.addRow("Категория", self.cat_combo)
        form.addRow("", self.skip_chk)
        form.addRow(photo_btn, self.photo_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Cancel).setObjectName("secondary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _pick_photo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Фото", "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.photo_path = path
            self.photo_label.setText(path)

    def accept(self) -> None:
        self.name = self.name_edit.text().strip()
        self.article = self.article_edit.text().strip()
        self.nmid = self.nmid_edit.text().strip()
        bq = int(self.box_spin.value())
        self.box_qty = bq if bq > 0 else None
        self.category = self.cat_combo.currentText().strip()
        self.skip_gtin = self.skip_chk.isChecked()
        super(ProductEditDialog, self).accept()
