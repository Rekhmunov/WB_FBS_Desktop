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

from app.services.kiz_pick import KizService, PickVerifyService
from app.services.trbx_stickers import StickersService
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


class KizDialog(QDialog):
    """Маркировка: скан стикера → скан КИЗ → сохранение в WB meta/sgtin."""

    def __init__(
        self,
        kiz: KizService,
        source_id: int,
        api_key: str,
        supply_id: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(KizDialog, self).__init__(parent)
        self.kiz = kiz
        self.source_id = source_id
        self.api_key = api_key
        self.supply_id = supply_id
        self.rows = []  # type: List[Dict[str, Any]]
        self.current = None  # type: Optional[Dict[str, Any]]
        self.row_errors = {}  # type: Dict[int, str]
        self._sticker_map = {}  # type: Dict[str, Dict[str, Any]]

        self.setWindowTitle("Маркировка · {}".format(supply_id))
        self.resize(960, 700)
        self.setMinimumSize(800, 560)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        title = QLabel("Маркировка")
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
        mark_lab = QLabel("КИЗ")
        mark_lab.setObjectName("fieldLabel")
        self.mark_input = QLineEdit()
        self.mark_input.setPlaceholderText("Сканирование КИЗ (Data Matrix)…")
        self.mark_input.returnPressed.connect(self.on_mark)
        self.mark_input.setEnabled(False)
        scan_row.addWidget(sticker_lab)
        scan_row.addWidget(self.sticker_input, 1)
        scan_row.addWidget(mark_lab)
        scan_row.addWidget(self.mark_input, 2)
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
        self.chip_errors = _make_chip("С ошибками")
        self.chip_cancelled = _make_chip("Отменённые")
        for chip in (self.chip_filled, self.chip_empty, self.chip_errors, self.chip_cancelled):
            chips_row.addWidget(chip)
        chips_row.addStretch(1)
        tb.addLayout(chips_row)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск: заказ, артикул, стикер…")
        tb.addWidget(self.search_input)
        root.addWidget(toolbar)

        self.chip_filled.toggled.connect(self._on_filled_toggled)
        self.chip_empty.toggled.connect(self._on_empty_toggled)
        self.chip_errors.toggled.connect(lambda _checked: self._render_table())
        self.chip_cancelled.toggled.connect(lambda _checked: self._render_table())
        self.search_input.textChanged.connect(lambda _text: self._render_table())

        self.info = QLabel("Загрузка…")
        self.info.setWordWrap(True)
        self.info.setObjectName("hint")
        root.addWidget(self.info)

        self.table = QTableWidget(0, 4)
        self.table.setAlternatingRowColors(True)
        self.table.setHorizontalHeaderLabels(["Заказ", "Товар / Артикул", "КИЗ", "Статус"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.table.itemSelectionChanged.connect(self.on_select_row)
        root.addWidget(self.table, 1)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        save = QPushButton("Сохранить в WB")
        save.clicked.connect(self.save_current)
        save_all = QPushButton("Сохранить все локальные → WB")
        save_all.setObjectName("secondary")
        save_all.clicked.connect(self.save_all)
        bar.addWidget(save)
        bar.addWidget(save_all)
        bar.addStretch(1)
        root.addLayout(bar)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.load_rows()
        self.sticker_input.setFocus()

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
    def _row_codes(row: Dict[str, Any]) -> List[str]:
        codes = row.get("kiz_codes") or [""]
        return list(codes) if codes else [""]

    @classmethod
    def _row_is_empty(cls, row: Dict[str, Any]) -> bool:
        return not any(str(c or "").strip() for c in cls._row_codes(row))

    def _row_is_cancelled(self, row: Dict[str, Any]) -> bool:
        return is_cancelled_status(
            supplier_status=row.get("supplier_status"),
            wb_status=row.get("wb_status"),
        )

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
            rows = [r for r in rows if not self._row_is_empty(r)]
        if self.chip_empty.isChecked():
            rows = [r for r in rows if self._row_is_empty(r)]
        if self.chip_errors.isChecked():
            rows = [r for r in rows if int(r["order_id"]) in self.row_errors]
        if self.chip_cancelled.isChecked():
            rows = [r for r in rows if self._row_is_cancelled(r)]
        query = self.search_input.text()
        if query.strip():
            rows = [r for r in rows if self._row_matches_search(r, query)]
        return rows

    def _update_counter(self) -> None:
        filled = 0
        total = 0
        for r in self.rows:
            codes = self._row_codes(r)
            total += len(codes)
            filled += sum(1 for c in codes if str(c or "").strip())
        self.counter.setText("Просканировано {} из {} КИЗ".format(filled, total))

    # -- RU keyboard layout guard -------------------------------------
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
        try:
            self.rows = self.kiz.marking_rows(
                self.source_id, self.supply_id, self.api_key
            )
        except Exception as exc:
            self.info.setText("Ошибка загрузки: {}".format(exc))
            self.rows = []
            self._render_table()
            return
        self.row_errors = {}
        # Enrich sticker numbers from WB stickers API
        try:
            stickers = StickersService(self.kiz.db).order_stickers_png(
                self.api_key, [int(r["order_id"]) for r in self.rows]
            )
            by_oid = {}
            for st in stickers:
                try:
                    oid = int(st.get("order_id"))
                except (TypeError, ValueError):
                    continue
                part_a = str(st.get("partA") or "")
                part_b = str(st.get("partB") or "")
                full = (part_a + part_b) if (part_a or part_b) else str(st.get("barcode") or "")
                by_oid[oid] = full
                if full:
                    self._sticker_map[full] = next(
                        (r for r in self.rows if int(r["order_id"]) == oid), None
                    )
                    if part_b:
                        self._sticker_map[part_b] = self._sticker_map[full]
            for r in self.rows:
                r["sticker_number"] = by_oid.get(int(r["order_id"]), "")
        except Exception:
            pass

        self._render_table()
        if not self.rows:
            self.info.setText("В поставке нет заказов, требующих маркировки КИЗ")
        else:
            self.info.setText("Сканируйте стикер, затем КИЗ.")

    def _build_codes_cell(self, row: Dict[str, Any]) -> QWidget:
        oid = int(row["order_id"])
        codes = self._row_codes(row)
        err = self.row_errors.get(oid, "")
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(4, 6, 4, 6)
        lay.setSpacing(6)
        for idx, code in enumerate(codes):
            line = QHBoxLayout()
            line.setSpacing(6)
            text = str(code or "").strip()
            lab = _ClickableLabel(text if text else "— пусто —")
            if not text:
                lab.setObjectName("hint")
            elif err:
                lab.setStyleSheet("color:#b91c1c;")
            lab.doubleClicked.connect(partial(self._clear_code, oid, idx))
            clear_btn = QToolButton()
            clear_btn.setText("✕")
            clear_btn.setObjectName("dangerToolBtn")
            clear_btn.setToolTip(
                "Удалить строку КИЗ" if len(codes) > 1 else "Очистить маркировку"
            )
            clear_btn.clicked.connect(partial(self._clear_code, oid, idx))
            line.addWidget(lab, 1)
            line.addWidget(clear_btn, 0)
            lay.addLayout(line)
        if err:
            err_lab = QLabel(err)
            err_lab.setWordWrap(True)
            err_lab.setStyleSheet("color:#b91c1c; font-size:12px;")
            lay.addWidget(err_lab)
        lay.addStretch(1)
        return wrap

    def _build_product_cell(self, row: Dict[str, Any]) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(4, 6, 4, 6)
        lay.setSpacing(2)
        lab = QLabel(str(row.get("article") or "—"))
        lay.addWidget(lab)
        if self._row_is_cancelled(row):
            reason = cancel_reason_label(
                supplier_status=row.get("supplier_status"),
                wb_status=row.get("wb_status"),
            ) or "Отменён"
            badge = QLabel(reason)
            badge.setObjectName("fbsBadge")
            badge.setStyleSheet(
                "QLabel#fbsBadge { background:#fee2e2; color:#b91c1c;"
                " padding:2px 6px; border-radius:4px; font-size:11px; font-weight:600; }"
            )
            lay.addWidget(badge)
        lay.addStretch(1)
        return wrap

    def _clear_code(self, order_id: int, idx: int) -> None:
        row = next((r for r in self.rows if int(r["order_id"]) == order_id), None)
        if not row:
            return
        codes = self._row_codes(row)
        if len(codes) <= 1:
            codes = [""]
        else:
            if 0 <= idx < len(codes):
                codes.pop(idx)
            if not codes:
                codes = [""]
        row["kiz_codes"] = codes
        self.row_errors.pop(order_id, None)
        cleaned = [c for c in codes if str(c).strip()]
        try:
            self.kiz.save_local(self.source_id, order_id, cleaned, wb_synced=False)
        except Exception:
            pass
        if self.current and int(self.current.get("order_id") or -1) == order_id:
            self.current = row
        self._render_table()

    def _render_table(self) -> None:
        self._update_counter()
        rows = self._visible_rows()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            oid = int(r["order_id"])
            self.table.setItem(i, 0, QTableWidgetItem(str(oid)))
            self.table.setCellWidget(i, 1, self._build_product_cell(r))
            self.table.setCellWidget(i, 2, self._build_codes_cell(r))
            codes = self._row_codes(r)
            has_codes = any(str(c or "").strip() for c in codes)
            if oid in self.row_errors:
                status = "Ошибка: {}".format(self.row_errors[oid])
            elif r.get("kiz_wb_synced"):
                status = "Сохранено в WB"
            elif has_codes:
                status = "Сохранено локально"
            else:
                status = "Не заполнено"
            status_item = QTableWidgetItem(status)
            if oid in self.row_errors:
                status_item.setForeground(Qt.red)
            self.table.setItem(i, 3, status_item)
        self.table.resizeRowsToContents()
        if not rows and self.rows:
            self.info.setText("Нет строк по выбранным фильтрам")

    def on_select_row(self) -> None:
        row = self.table.currentRow()
        visible = self._visible_rows()
        if row < 0 or row >= len(visible):
            return
        self.current = visible[row]
        self.mark_input.setEnabled(True)
        self.mark_input.setFocus()

    def on_sticker(self) -> None:
        if self._block_ru_layout(self.sticker_input):
            return
        raw = self.sticker_input.text().replace(" ", "").strip()
        self.sticker_input.clear()
        if not raw:
            return
        found = self._sticker_map.get(raw)
        if not found:
            # try last 4 digits
            tail = raw[-4:] if len(raw) >= 4 else raw
            matches = [
                r
                for r in self.rows
                if str(r.get("sticker_number") or "").endswith(tail)
            ]
            if len(matches) == 1:
                found = matches[0]
            elif len(matches) > 1:
                self.info.setText("Несколько заказов с хвостом {} — отсканируйте полный стикер".format(tail))
                return
        if not found:
            self.info.setText("Стикер не найден: {}".format(raw))
            return
        self.current = found
        self._select_order_row(int(found["order_id"]))
        self.info.setText(
            "Заказ {} · {} · сканируйте КИЗ".format(
                found["order_id"], found.get("article") or ""
            )
        )
        self.mark_input.setEnabled(True)
        self.mark_input.setFocus()

    def _select_order_row(self, order_id: int) -> None:
        visible = self._visible_rows()
        for i, r in enumerate(visible):
            if int(r["order_id"]) == order_id:
                self.table.selectRow(i)
                return

    def on_mark(self) -> None:
        if self._block_ru_layout(self.mark_input):
            return
        if not self.current:
            self.info.setText("Сначала отсканируйте стикер")
            return
        oid = int(self.current["order_id"])
        code = self.mark_input.text()
        self.mark_input.clear()
        ok, err = self.kiz.validate_mark(
            code,
            self.current.get("skus") or [],
            bool(self.current.get("skip_kiz_gtin_check")),
        )
        if not ok:
            self.row_errors[oid] = err
            self.info.setText(err)
            self._render_table()
            return
        self.row_errors.pop(oid, None)
        codes = [c for c in (self.current.get("kiz_codes") or []) if str(c).strip(" \t\r\n")]
        cleaned = code.strip(" \t\r\n").replace("\u2194", "\u001d")
        if has_cyrillic(cleaned):
            # Defensive normalization — scan already gated above, but loaded/pasted
            # values could still carry a stray RU-layout character.
            cleaned = fix_ru_keyboard_layout(cleaned)
        if cleaned in codes:
            self.info.setText("Этот КИЗ уже добавлен")
            return
        codes.append(cleaned)
        self.current["kiz_codes"] = codes
        self.kiz.save_local(self.source_id, oid, codes, wb_synced=False)
        self.info.setText(
            "КИЗ сохранён локально для заказа {} ({} шт.)".format(oid, len(codes))
        )
        self.load_rows()
        self._select_order_row(oid)
        self.sticker_input.setFocus()

    def save_current(self) -> None:
        if not self.current:
            return
        oid = int(self.current["order_id"])
        try:
            self.kiz.save_to_wb(
                self.source_id,
                self.api_key,
                oid,
                self.current.get("kiz_codes") or [],
            )
            self.row_errors.pop(oid, None)
            self.info.setText("Сохранено в WB: заказ {}".format(oid))
            self.load_rows()
        except Exception as exc:
            self.row_errors[oid] = str(exc)
            self._render_table()
            QMessageBox.critical(self, "Маркировка", str(exc))

    def save_all(self) -> None:
        errors = []
        for r in self.rows:
            codes = [c for c in (r.get("kiz_codes") or []) if str(c).strip(" \t\r\n")]
            if not codes:
                continue
            oid = int(r["order_id"])
            try:
                self.kiz.save_to_wb(self.source_id, self.api_key, oid, codes)
                self.row_errors.pop(oid, None)
            except Exception as exc:
                self.row_errors[oid] = str(exc)
                errors.append("{}: {}".format(oid, exc))
        self.load_rows()
        if errors:
            QMessageBox.warning(self, "Маркировка", "\n".join(errors[:8]))
        else:
            self.info.setText("Все локальные коды отправлены в WB")


class PickDialog(QDialog):
    def __init__(
        self,
        pick: PickVerifyService,
        source_id: int,
        api_key: str,
        supply_id: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(PickDialog, self).__init__(parent)
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
        self.resize(960, 700)
        self.setMinimumSize(800, 560)
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
        try:
            self.rows = self.pick.rows(self.source_id, self.supply_id, self.api_key)
        except Exception as exc:
            self.info.setText("Ошибка: {}".format(exc))
            return
        self.row_errors = {}
        try:
            stickers = StickersService(self.pick.db).order_stickers_png(
                self.api_key, [int(r["order_id"]) for r in self.rows]
            )
            for st in stickers:
                try:
                    oid = int(st.get("order_id"))
                except (TypeError, ValueError):
                    continue
                row = next((r for r in self.rows if int(r["order_id"]) == oid), None)
                if not row:
                    continue
                part_a = str(st.get("partA") or "")
                part_b = str(st.get("partB") or "")
                full = part_a + part_b
                row["sticker_number"] = full
                if full:
                    self._sticker_map[full] = row
                if part_b:
                    self._sticker_map[part_b] = row
        except Exception:
            pass
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
