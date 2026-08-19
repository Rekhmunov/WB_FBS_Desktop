# -*- coding: utf-8 -*-
from __future__ import annotations

from functools import partial
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
)

from app.ui.kiz_dialog import KizDialog  # noqa: F401 — re-export

from app.services.kiz_pick import PickVerifyService
from app.services import supply_session
from app.ui.dialog_utils import (
    apply_fullscreen_on_show,
    fullscreen_parent,
    init_fullscreen_dialog,
)
from app.ui.format_helpers import fix_ru_keyboard_layout, has_cyrillic
from app.wb import cancel_reason_label, is_cancelled_status


class _ClickableLabel(QLabel):
    """QLabel that emits doubleClicked — used for the quick clear gesture."""

    doubleClicked = pyqtSignal()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.doubleClicked.emit()
        super(_ClickableLabel, self).mouseDoubleClickEvent(event)


def _make_chip(text: str) -> QPushButton:
    chip = QPushButton(text)
    chip.setObjectName("filterChip")
    chip.setCheckable(True)
    chip.setCursor(Qt.PointingHandCursor)
    return chip


class PickDialog(QDialog):
    def __init__(
        self,
        pick: PickVerifyService,
        source_id: int,
        api_key: str,
        supply_id: str,
        parent: Optional[QWidget] = None,
        *,
        fullscreen: bool = True,
    ) -> None:
        super(PickDialog, self).__init__(fullscreen_parent(parent, fullscreen))
        self.pick = pick
        self.source_id = source_id
        self.api_key = api_key
        self.supply_id = supply_id
        self.rows = []  # type: List[Dict[str, Any]]
        self.current = None  # type: Optional[Dict[str, Any]]
        self.row_errors = {}  # type: Dict[int, str]
        self._sticker_map = {}  # type: Dict[str, Dict[str, Any]]
        self._cancelled_ids = set()  # type: set

        self.setWindowTitle("Проверка ШК · {}".format(supply_id))
        init_fullscreen_dialog(
            self,
            fullscreen=fullscreen,
            default_size=(960, 700),
            minimum_size=(800, 560),
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        title = QLabel("Проверка ШК")
        title.setObjectName("dialogTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.counter = QLabel("")
        self.counter.setObjectName("fieldLabel")
        title_row.addWidget(self.counter)
        root.addLayout(title_row)

        scan_row = QHBoxLayout()
        scan_row.setSpacing(12)
        sticker_lab = QLabel("Стикер")
        sticker_lab.setObjectName("fieldLabel")
        self.sticker_input = QLineEdit()
        self.sticker_input.setPlaceholderText("Сканирование стикера…")
        self.sticker_input.returnPressed.connect(self.on_sticker)
        sku_lab = QLabel("ШК")
        sku_lab.setObjectName("fieldLabel")
        self.sku_input = QLineEdit()
        self.sku_input.setPlaceholderText("Сканирование ШК товара…")
        self.sku_input.returnPressed.connect(self.on_sku)
        self.sku_input.setEnabled(False)
        scan_row.addWidget(sticker_lab)
        scan_row.addWidget(self.sticker_input, 1)
        scan_row.addWidget(sku_lab)
        scan_row.addWidget(self.sku_input, 1)
        root.addLayout(scan_row)

        toolbar = QFrame()
        toolbar.setObjectName("toolbarPanel")
        tb = QVBoxLayout(toolbar)
        tb.setContentsMargins(16, 12, 16, 12)
        tb.setSpacing(8)
        chips_row = QHBoxLayout()
        chips_row.setSpacing(8)
        self.chip_filled = _make_chip("Заполненные")
        self.chip_empty = _make_chip("Незаполненные")
        self.chip_cancelled = _make_chip("Отменённые")
        for chip in (self.chip_filled, self.chip_empty, self.chip_cancelled):
            chips_row.addWidget(chip)
        chips_row.addStretch(1)
        tb.addLayout(chips_row)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск: заказ, артикул, стикер…")
        tb.addWidget(self.search_input)
        root.addWidget(toolbar)

        self.chip_filled.toggled.connect(self._on_filled_toggled)
        self.chip_empty.toggled.connect(self._on_empty_toggled)
        self.chip_cancelled.toggled.connect(lambda _checked: self._render_table())
        self.search_input.textChanged.connect(lambda _text: self._render_table())

        self.info = QLabel("Загрузка…")
        self.info.setWordWrap(True)
        self.info.setObjectName("hint")
        root.addWidget(self.info)

        self.table = QTableWidget(0, 4)
        self.table.setAlternatingRowColors(True)
        self.table.setHorizontalHeaderLabels(["Заказ", "Артикул", "ШК заказа", "Проверка"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(48)
        root.addWidget(self.table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.load_rows()
        self.sticker_input.setFocus()

    def showEvent(self, event) -> None:
        super(PickDialog, self).showEvent(event)
        apply_fullscreen_on_show(self)

    # -- filters -----------------------------------------------------
    def _on_filled_toggled(self, checked: bool) -> None:
        if checked:
            self.chip_empty.setChecked(False)
        self._render_table()

    def _on_empty_toggled(self, checked: bool) -> None:
        if checked:
            self.chip_filled.setChecked(False)
        self._render_table()

    @staticmethod
    def _row_is_verified(row: Dict[str, Any]) -> bool:
        return bool(row.get("pick_verified")) and bool(str(row.get("pick_barcode") or "").strip())

    def _row_is_cancelled(self, row: Dict[str, Any]) -> bool:
        return int(row.get("order_id")) in self._cancelled_ids

    @staticmethod
    def _row_matches_search(row: Dict[str, Any], query: str) -> bool:
        q = str(query or "").strip().lower()
        if not q:
            return True
        hay = [
            row.get("order_id"),
            row.get("article"),
            row.get("sticker_number"),
        ]
        return any(q in str(v or "").strip().lower() for v in hay)

    def _visible_rows(self) -> List[Dict[str, Any]]:
        rows = list(self.rows)
        if self.chip_filled.isChecked():
            rows = [r for r in rows if self._row_is_verified(r)]
        if self.chip_empty.isChecked():
            rows = [r for r in rows if not self._row_is_verified(r)]
        if self.chip_cancelled.isChecked():
            rows = [r for r in rows if self._row_is_cancelled(r)]
        query = self.search_input.text()
        if query.strip():
            rows = [r for r in rows if self._row_matches_search(r, query)]
        return rows

    def _update_counter(self) -> None:
        filled = sum(1 for r in self.rows if self._row_is_verified(r))
        total = len(self.rows)
        self.counter.setText("Проверено {} из {}".format(filled, total))

    def _block_ru_layout(self, widget: QLineEdit) -> bool:
        text = widget.text()
        if not has_cyrillic(text):
            return False
        widget.clear()
        QMessageBox.warning(
            self,
            "Русская раскладка!",
            "Сканирование выполнено в русской раскладке клавиатуры.\n"
            "Переключите раскладку на английскую (EN) и отсканируйте код ещё раз.",
        )
        return True

    def load_rows(self) -> None:
        session = supply_session.get_session(self.source_id, self.supply_id)
        try:
            if session and session.core_ready and session.pick_rows is not None:
                self.rows = [dict(r) for r in session.pick_rows]
            else:
                self.rows = self.pick.rows(self.source_id, self.supply_id, self.api_key)
        except Exception as exc:
            self.info.setText("Ошибка: {}".format(exc))
            return
        self.row_errors = {}
        self._sticker_map = {}
        stickers = (session.sticker_numbers if session else {}) or {}
        for row in self.rows:
            oid = int(row["order_id"])
            st = stickers.get(oid) or {}
            part_a = str(st.get("partA") or row.get("sticker_part_a") or "")
            part_b = str(st.get("partB") or row.get("sticker_part_b") or "")
            full = (part_a + part_b) or str(row.get("sticker_number") or "")
            row["sticker_number"] = full
            row["sticker_part_a"] = part_a
            row["sticker_part_b"] = part_b
            if full:
                self._sticker_map[full] = row
            if part_b:
                self._sticker_map[part_b] = row
        try:
            from app.services.cancelled import list_cancelled_in_supply

            data = list_cancelled_in_supply(
                self.pick.db, self.source_id, self.api_key, self.supply_id
            )
            self._cancelled_ids = {
                int(r.get("order_id"))
                for r in (data.get("rows") or [])
                if str(r.get("order_id") or "").strip()
            }
        except Exception:
            self._cancelled_ids = set()
        self._render_table()
        if not self.rows:
            self.info.setText("Нет заказов для проверки ШК")
        else:
            self.info.setText("Сканируйте стикер, затем ШК.")

    def _build_status_cell(self, row: Dict[str, Any]) -> QWidget:
        oid = int(row["order_id"])
        err = self.row_errors.get(oid, "")
        verified = self._row_is_verified(row)
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)
        if err:
            lab = QLabel(err)
            lab.setStyleSheet("color:#b91c1c;")
        elif verified:
            lab = QLabel("✓ {}".format(row.get("pick_barcode") or ""))
            lab.setStyleSheet("color:#166534; font-weight:600;")
        else:
            lab = QLabel("Не проверено")
            lab.setObjectName("hint")
        lab.setWordWrap(True)
        lay.addWidget(lab, 1)
        if verified or err:
            clear_btn = QToolButton()
            clear_btn.setText("✕")
            clear_btn.setObjectName("dangerToolBtn")
            clear_btn.setToolTip("Сбросить проверку")
            clear_btn.clicked.connect(partial(self._clear_verify, oid))
            lay.addWidget(clear_btn, 0)
        return wrap

    def _clear_verify(self, order_id: int) -> None:
        row = next((r for r in self.rows if int(r["order_id"]) == order_id), None)
        if not row:
            return
        row["pick_verified"] = False
        row["pick_barcode"] = ""
        self.row_errors.pop(order_id, None)
        try:
            self.pick.save(self.source_id, order_id, False, "")
        except Exception:
            pass
        self.info.setText("Заказ {}: проверка сброшена".format(order_id))
        self._render_table()

    def _render_table(self) -> None:
        self._update_counter()
        rows = self._visible_rows()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(str(r["order_id"])))
            self.table.setItem(i, 1, QTableWidgetItem(str(r.get("article") or "")))
            self.table.setItem(
                i, 2, QTableWidgetItem(", ".join(str(s) for s in (r.get("skus") or [])[:4]))
            )
            self.table.setCellWidget(i, 3, self._build_status_cell(r))
        self.table.resizeRowsToContents()
        if not rows and self.rows:
            self.info.setText("Нет строк по выбранным фильтрам")

    def on_sticker(self) -> None:
        if self._block_ru_layout(self.sticker_input):
            return
        raw = self.sticker_input.text().replace(" ", "").strip()
        self.sticker_input.clear()
        found = self._sticker_map.get(raw)
        if not found and len(raw) >= 4:
            tail = raw[-4:]
            matches = [
                r
                for r in self.rows
                if str(r.get("sticker_number") or "").endswith(tail)
            ]
            if len(matches) == 1:
                found = matches[0]
        if not found:
            self.info.setText("Стикер не найден")
            return
        self.current = found
        self._select_order_row(int(found["order_id"]))
        self.info.setText(
            "Заказ {} · сканируйте ШК товара".format(found["order_id"])
        )
        self.sku_input.setEnabled(True)
        self.sku_input.setFocus()

    def _select_order_row(self, order_id: int) -> None:
        visible = self._visible_rows()
        for i, r in enumerate(visible):
            if int(r["order_id"]) == order_id:
                self.table.selectRow(i)
                return

    def on_sku(self) -> None:
        if self._block_ru_layout(self.sku_input):
            return
        if not self.current:
            return
        oid = int(self.current["order_id"])
        code = self.sku_input.text().strip()
        self.sku_input.clear()
        ok, err = self.pick.validate_barcode(code, self.current.get("skus") or [])
        if not ok:
            self.row_errors[oid] = err
            self.info.setText(err)
            self._render_table()
            return
        self.row_errors.pop(oid, None)
        self.pick.save(self.source_id, oid, True, code)
        self.info.setText("Проверено: заказ {}".format(oid))
        self.load_rows()
        self._select_order_row(oid)
        self.sticker_input.setFocus()
