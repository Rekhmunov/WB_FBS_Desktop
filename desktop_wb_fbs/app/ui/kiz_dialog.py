# -*- coding: utf-8 -*-
"""КИЗ marking modal — web portal layout parity."""
from __future__ import annotations

from functools import partial
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QApplication,
)

from app.services.kiz_pick import KizService
from app.services import supply_session
from app.services.print_docs import _fetch_picking_stickers
from app.services.trbx_stickers import StickersService
from app.ui.dialog_utils import (
    apply_fullscreen_on_show,
    block_ru_layout_scan,
    fullscreen_parent,
    init_fullscreen_dialog,
    init_maximized_window,
)
from app.ui.dialogs_extra import show_png_list
from app.ui.format_helpers import (
    make_badge,
    make_photo_label,
)
from app.wb import cancel_reason_label, is_cancelled_status


def _sticker_number(part_a: str, part_b: str) -> str:
    return "{}{}".format(str(part_a or "").strip(), str(part_b or "").strip())


_RENDER_BATCH = 50
_FILTER_EMPTY_MSG = "Нет строк по выбранным фильтрам"


class KizMarkScanDialog(QDialog):
    """Secondary prompt: scan Data Matrix after order sticker (web scan-prompt)."""

    def __init__(
        self,
        order_id: int,
        sticker_label: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(KizMarkScanDialog, self).__init__(parent)
        self.order_id = int(order_id)
        self.mark_code = ""  # type: str
        self.setWindowTitle("Просканируйте маркировку")
        self.setModal(True)
        init_maximized_window(
            self,
            maximized=False,
            default_size=(440, 220),
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(12)
        title = QLabel("Просканируйте маркировку")
        title.setObjectName("kizPromptTitle")
        lay.addWidget(title)
        meta = QLabel("Заказ {} · стикер {}".format(order_id, sticker_label or "—"))
        meta.setObjectName("kizPromptMeta")
        meta.setWordWrap(True)
        lay.addWidget(meta)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.mark_input = QLineEdit()
        self.mark_input.setObjectName("kizScanInput")
        self.mark_input.setPlaceholderText("Сканируйте КИЗ с того же изделия")
        self.mark_input.returnPressed.connect(self._accept_mark)
        clear_btn = QToolButton()
        clear_btn.setObjectName("kizScanClear")
        clear_btn.setText("✕")
        clear_btn.setToolTip("Очистить")
        clear_btn.clicked.connect(self.mark_input.clear)
        row.addWidget(self.mark_input, 1)
        row.addWidget(clear_btn)
        lay.addLayout(row)
        cancel = QPushButton("Отмена")
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self.reject)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(cancel)
        lay.addLayout(actions)

    def showEvent(self, event) -> None:
        super(KizMarkScanDialog, self).showEvent(event)
        self.mark_input.setFocus()

    def _accept_mark(self) -> None:
        if block_ru_layout_scan(self, self.mark_input):
            return
        self.mark_code = self.mark_input.text()
        self.accept()


class KizDialog(QDialog):
    """КИЗ marking modal — portal layout (header, filters, scan bar, table)."""

    def __init__(
        self,
        kiz: KizService,
        source_id: int,
        api_key: str,
        supply_id: str,
        parent: Optional[QWidget] = None,
        *,
        fullscreen: bool = True,
    ) -> None:
        super(KizDialog, self).__init__(fullscreen_parent(parent, fullscreen))
        self.kiz = kiz
        self.source_id = source_id
        self.api_key = api_key
        self.supply_id = supply_id
        self.rows = []  # type: List[Dict[str, Any]]
        self.row_errors = {}  # type: Dict[int, str]
        self._sticker_map = {}  # type: Dict[str, Dict[str, Any]]
        self._pending_order_id = None  # type: Optional[int]
        self._code_inputs = {}  # type: Dict[int, List[QLineEdit]]
        self._row_index_by_oid = {}  # type: Dict[int, int]
        self._row_by_oid = {}  # type: Dict[int, Dict[str, Any]]
        self._rows_ready = False
        self._saving = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_filters)

        self.setObjectName("kizModal")
        self.setWindowTitle("КИЗ · {}".format(supply_id))
        init_fullscreen_dialog(
            self,
            fullscreen=fullscreen,
            default_size=(1200, 820),
            minimum_size=(900, 640),
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header — title row (left) + actions (right), subtitle below
        header = QFrame()
        header.setObjectName("kizHeader")
        header_lay = QVBoxLayout(header)
        header_lay.setContentsMargins(24, 20, 24, 16)
        header_lay.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.setSpacing(16)
        title = QLabel("КИЗ")
        title.setObjectName("kizTitle")
        title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        title_row.addWidget(title, 0, Qt.AlignLeft | Qt.AlignTop)
        title_row.addStretch(1)

        head_actions = QHBoxLayout()
        head_actions.setSpacing(8)
        head_actions.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("bottomPrimary")
        self.save_btn.setFixedHeight(40)
        self.save_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.save_btn.clicked.connect(self.save_all)
        close_btn = QToolButton()
        close_btn.setObjectName("iconBtn")
        close_btn.setText("✕")
        close_btn.setToolTip("Закрыть")
        close_btn.clicked.connect(self.reject)
        head_actions.addWidget(self.save_btn)
        head_actions.addWidget(close_btn)
        title_row.addLayout(head_actions, 0)

        sub = QLabel(
            "Контрольный идентификационный знак, похожий на QR-код. "
            "Нужен для маркировки товаров в системе «Честный знак»"
        )
        sub.setObjectName("kizSub")
        sub.setWordWrap(True)
        sub.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        header_lay.addLayout(title_row)
        header_lay.addWidget(sub)
        root.addWidget(header)

        # Toolbar: filters + search + counter
        toolbar = QFrame()
        toolbar.setObjectName("kizToolbar")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(24, 12, 24, 12)
        tb.setSpacing(16)
        filters = QHBoxLayout()
        filters.setSpacing(16)
        self.chk_filled = QCheckBox("Заполненные")
        self.chk_empty = QCheckBox("Незаполненные")
        self.chk_errors = QCheckBox("С ошибками")
        self.chk_cancelled = QCheckBox("Отмененные")
        for cb in (self.chk_filled, self.chk_empty, self.chk_errors, self.chk_cancelled):
            cb.setObjectName("kizFilterCheck")
            filters.addWidget(cb)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("kizSearch")
        self.search_input.setPlaceholderText("🔍 Поиск...")
        self.search_input.setMinimumWidth(180)
        filters.addWidget(self.search_input)
        tb.addLayout(filters, 1)
        self.counter = QLabel("Просканировано 0 из 0 КИЗ")
        self.counter.setObjectName("kizScanCount")
        tb.addWidget(self.counter)
        root.addWidget(toolbar)

        self.chk_filled.toggled.connect(self._on_filled_toggled)
        self.chk_empty.toggled.connect(self._on_empty_toggled)
        self.chk_errors.toggled.connect(self._apply_filters)
        self.chk_cancelled.toggled.connect(self._apply_filters)
        self.search_input.textChanged.connect(self._schedule_filter)

        # Scan bar
        scan_bar = QFrame()
        scan_bar.setObjectName("kizScanBar")
        scan_lay = QHBoxLayout(scan_bar)
        scan_lay.setContentsMargins(24, 12, 24, 12)
        scan_lay.setSpacing(12)
        scan_lab = QLabel("Сканирование")
        scan_lab.setObjectName("kizScanLabel")
        self.sticker_input = QLineEdit()
        self.sticker_input.setObjectName("kizScanInput")
        self.sticker_input.setPlaceholderText("Сканируйте QR стикера заказа")
        self.sticker_input.returnPressed.connect(self.on_sticker)
        sticker_clear = QToolButton()
        sticker_clear.setObjectName("kizScanClear")
        sticker_clear.setText("✕")
        sticker_clear.clicked.connect(self.sticker_input.clear)
        scan_lay.addWidget(scan_lab)
        scan_lay.addWidget(self.sticker_input, 1)
        scan_lay.addWidget(sticker_clear)
        root.addWidget(scan_bar)

        # Info banner
        self.info_banner = QFrame()
        self.info_banner.setObjectName("kizInfo")
        self.info_banner.hide()
        info_lay = QHBoxLayout(self.info_banner)
        info_lay.setContentsMargins(24, 8, 24, 8)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setObjectName("kizInfoText")
        info_lay.addWidget(self.info)
        root.addWidget(self.info_banner)

        # Table
        self.table = QTableWidget(0, 4)
        self.table.setObjectName("kizTable")
        self.table.setAlternatingRowColors(True)
        self.table.setHorizontalHeaderLabels(
            ["Заказ/стикер", "Товар", "КИЗ", ""]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 170)
        self.table.setColumnWidth(2, 340)
        self.table.setColumnWidth(3, 52)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(96)
        root.addWidget(self.table, 1)

        self._set_filters_ready(False)
        self.load_rows()
        self.sticker_input.setFocus()

    def showEvent(self, event) -> None:
        super(KizDialog, self).showEvent(event)
        apply_fullscreen_on_show(self)

    def _set_filters_ready(self, ready: bool) -> None:
        self._rows_ready = bool(ready)
        for w in (
            self.chk_filled,
            self.chk_empty,
            self.chk_errors,
            self.chk_cancelled,
        ):
            w.setEnabled(ready)
        self.search_input.setReadOnly(not ready)
        self.sticker_input.setEnabled(ready)

    def _set_info(self, text: str = "", ok: bool = False) -> None:
        msg = str(text or "").strip()
        if not msg:
            self.info_banner.hide()
            self.info.setText("")
            return
        self.info.setText(msg)
        self.info_banner.setProperty("state", "ok" if ok else "error")
        self.info_banner.style().unpolish(self.info_banner)
        self.info_banner.style().polish(self.info_banner)
        self.info_banner.show()

    def _on_filled_toggled(self, checked: bool) -> None:
        if checked:
            self.chk_empty.setChecked(False)
        self._apply_filters()

    def _on_empty_toggled(self, checked: bool) -> None:
        if checked:
            self.chk_filled.setChecked(False)
        self._apply_filters()

    def _schedule_filter(self) -> None:
        self._search_timer.start()

    def _row_passes_filters(self, row: Dict[str, Any]) -> bool:
        if self.chk_filled.isChecked() and self._row_is_empty(row):
            return False
        if self.chk_empty.isChecked() and not self._row_is_empty(row):
            return False
        oid = int(row["order_id"])
        if self.chk_errors.isChecked():
            if oid not in self.row_errors and str(row.get("kiz_status") or "") != "error":
                return False
        if self.chk_cancelled.isChecked() and not self._row_is_cancelled(row):
            return False
        if not self._row_matches_search(row, self.search_input.text()):
            return False
        return True

    def _apply_filters(self) -> None:
        if not self._row_index_by_oid:
            return
        any_visible = False
        for oid, idx in self._row_index_by_oid.items():
            row = self._row_by_oid.get(oid)
            if not row:
                continue
            visible = self._row_passes_filters(row)
            self.table.setRowHidden(idx, not visible)
            if visible:
                any_visible = True
        if not any_visible and self.rows:
            self._set_info(_FILTER_EMPTY_MSG)
        elif any_visible and str(self.info.text() or "").strip() == _FILTER_EMPTY_MSG:
            self._set_info("")

    @staticmethod
    def _row_codes(row: Dict[str, Any]) -> List[str]:
        codes = row.get("kiz_codes") or [""]
        return list(codes) if codes else [""]

    @classmethod
    def _row_is_empty(cls, row: Dict[str, Any]) -> bool:
        return not any(str(c or "").strip() for c in cls._row_codes(row))

    def _row_is_cancelled(self, row: Dict[str, Any]) -> bool:
        if str(row.get("cancel_reason_label") or "").strip():
            return True
        return is_cancelled_status(
            supplier_status=row.get("supplier_status"),
            wb_status=row.get("wb_status"),
        )

    @staticmethod
    def _row_matches_search(row: Dict[str, Any], query: str) -> bool:
        q = str(query or "").strip().lower()
        if not q:
            return True
        skus = row.get("skus") or []
        hay = [
            row.get("order_id"),
            row.get("article"),
            row.get("sticker_number"),
            row.get("sticker_part_a"),
            row.get("sticker_part_b"),
            row.get("product_name"),
            row.get("brand"),
            row.get("nm_id"),
            *skus,
        ]
        return any(q in str(v or "").strip().lower() for v in hay)

    def _update_counter(self) -> None:
        filled = 0
        total = 0
        for r in self.rows:
            codes = self._row_codes(r)
            total += len(codes)
            filled += sum(1 for c in codes if str(c or "").strip())
        self.counter.setText("Просканировано {} из {} КИЗ".format(filled, total))

    def _restore_code_input(self, order_id: int, inp: QLineEdit) -> None:
        row = next((r for r in self.rows if int(r["order_id"]) == order_id), None)
        if not row:
            inp.clear()
            return
        inputs = self._code_inputs.get(order_id) or []
        try:
            idx = inputs.index(inp)
            codes = self._row_codes(row)
            inp.setText(str(codes[idx] if idx < len(codes) else ""))
        except ValueError:
            inp.clear()

    def _sync_codes_from_inputs(self) -> None:
        for oid, inputs in list(self._code_inputs.items()):
            row = next((r for r in self.rows if int(r["order_id"]) == oid), None)
            if not row:
                continue
            row["kiz_codes"] = [inp.text() for inp in inputs] or [""]

    def _clear_table(self) -> None:
        self.table.clearSpans()
        self.table.setRowCount(0)
        self.table.clearContents()
        self._row_index_by_oid = {}
        self._code_inputs = {}

    def _show_loading_row(self) -> None:
        self._clear_table()
        self.table.setRowCount(1)
        self.table.setSpan(0, 0, 1, self.table.columnCount())
        loading = QTableWidgetItem("Загрузка…")
        loading.setTextAlignment(Qt.AlignCenter)
        loading.setFlags(Qt.ItemIsEnabled)
        self.table.setItem(0, 0, loading)

    def load_rows(self) -> None:
        self._set_filters_ready(False)
        self.save_btn.setEnabled(False)
        self._show_loading_row()
        QApplication.processEvents()
        session = supply_session.get_session(self.source_id, self.supply_id)
        try:
            if session and session.core_ready and session.kiz_rows is not None:
                self.rows = [dict(r) for r in session.kiz_rows]
            else:
                self.rows = self.kiz.marking_rows(
                    self.source_id, self.supply_id, self.api_key
                )
        except Exception as exc:
            self.rows = []
            self._set_info(str(exc))
            self._render_table()
            self._set_filters_ready(True)
            self.save_btn.setEnabled(True)
            return
        self.row_errors = {}
        self._sticker_map = {}
        self._code_inputs = {}
        stickers = {}  # type: Dict[int, Dict[str, Any]]
        if session and session.sticker_numbers:
            stickers = session.sticker_numbers
        else:
            try:
                ids = [int(r["order_id"]) for r in self.rows]
                stickers = _fetch_picking_stickers(self.api_key, ids)
            except Exception:
                stickers = {}
        for r in self.rows:
            oid = int(r["order_id"])
            st = stickers.get(oid) or {}
            part_a = str(st.get("partA") or r.get("sticker_part_a") or "").strip()
            part_b = str(st.get("partB") or r.get("sticker_part_b") or "").strip()
            r["sticker_part_a"] = part_a
            r["sticker_part_b"] = part_b
            full = _sticker_number(part_a, part_b)
            r["sticker_number"] = full or str(r.get("sticker_number") or "")
            if full:
                self._sticker_map[full] = r
            if part_b:
                self._sticker_map[part_b] = r
        self._render_table()
        if not self.rows:
            self._set_info("В поставке нет заказов, требующих маркировки КИЗ")
        else:
            self._set_info("")
        self._set_filters_ready(True)
        self.save_btn.setEnabled(True)
        self.sticker_input.setFocus()

    @staticmethod
    def _wrap_cell(inner: QWidget, *, active: bool = False) -> QFrame:
        frame = QFrame()
        frame.setObjectName("kizRowCell")
        if active:
            frame.setProperty("state", "active")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(inner)
        frame.style().unpolish(frame)
        frame.style().polish(frame)
        return frame

    def _build_sticker_widget(self, row: Dict[str, Any]) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(12, 10, 8, 10)
        lay.setSpacing(4)
        oid_lab = QLabel(str(row.get("order_id") or ""))
        oid_lab.setObjectName("kizOrderId")
        lay.addWidget(oid_lab)
        sticker_row = QHBoxLayout()
        sticker_row.setSpacing(2)
        part_a = str(row.get("sticker_part_a") or "").strip()
        part_b = str(row.get("sticker_part_b") or "").strip()
        full = str(row.get("sticker_number") or "").strip()
        if not part_a and not part_b and full:
            if len(full) > 4:
                part_a, part_b = full[:-4], full[-4:]
            else:
                part_b = full
        if part_a or part_b:
            if part_a:
                head = QLabel(part_a)
                head.setObjectName("kizStickerHead")
                sticker_row.addWidget(head)
            if part_b:
                tail = QLabel(part_b)
                tail.setObjectName("kizStickerTail")
                sticker_row.addWidget(tail)
            sticker_row.addStretch(1)
            lay.addLayout(sticker_row)
        else:
            dash = QLabel("—")
            dash.setObjectName("kizOrderDate")
            lay.addWidget(dash)
        date = QLabel("от {}".format(row.get("created_date") or "—"))
        date.setObjectName("kizOrderDate")
        lay.addWidget(date)
        lay.addStretch(1)
        return wrap

    def _build_product_widget(self, row: Dict[str, Any]) -> QWidget:
        wrap = QWidget()
        outer = QHBoxLayout(wrap)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(12)
        outer.setAlignment(Qt.AlignTop)
        outer.addWidget(make_photo_label(row.get("product_photo"), 56))
        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        name = str(row.get("product_name") or row.get("article") or "—")
        name_lab = QLabel(name)
        name_lab.setObjectName("kizProductName")
        name_lab.setWordWrap(True)
        text_col.addWidget(name_lab)
        sub_parts = []
        if row.get("brand"):
            sub_parts.append(str(row.get("brand")))
        if row.get("article"):
            sub_parts.append("Арт. {}".format(row.get("article")))
        sub = QLabel(" · ".join(sub_parts) if sub_parts else "—")
        sub.setObjectName("kizProductSub")
        sub.setWordWrap(True)
        text_col.addWidget(sub)
        for sku in (row.get("skus") or [])[:4]:
            bc = QLabel(str(sku))
            bc.setObjectName("kizBarcode")
            text_col.addWidget(bc)
        cancel = str(row.get("cancel_reason_label") or "").strip()
        if cancel:
            badge = make_badge(cancel, "")
            badge.setStyleSheet(
                badge.styleSheet()
                + " QLabel { background:#fee2e2; color:#b91c1c; }"
            )
            text_col.addWidget(badge)
        text_col.addStretch(1)
        outer.addLayout(text_col, 1)
        return wrap

    def _code_status_label(self, row: Dict[str, Any], code: str, err: str) -> Optional[QLabel]:
        if not str(code or "").strip():
            return None
        status = str(row.get("kiz_status") or "empty")
        if err:
            status = "error"
        if status == "empty":
            return None
        lab = QLabel()
        lab.setObjectName("kizCodeStatus")
        if status == "ok":
            lab.setText("Проверка пройдена")
            lab.setProperty("state", "ok")
        elif status == "error":
            lab.setText(err or "Ошибка проверки")
            lab.setProperty("state", "error")
        else:
            lab.setText("На проверке")
            lab.setProperty("state", "pending")
        lab.style().unpolish(lab)
        lab.style().polish(lab)
        return lab

    def _build_codes_widget(self, row: Dict[str, Any]) -> QWidget:
        oid = int(row["order_id"])
        codes = self._row_codes(row)
        err = self.row_errors.get(oid, "")
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        inputs = []  # type: List[QLineEdit]
        can_remove = len(codes) > 1
        for idx, code in enumerate(codes):
            block = QVBoxLayout()
            block.setSpacing(4)
            line = QHBoxLayout()
            line.setSpacing(8)
            idx_lab = QLabel(str(idx + 1))
            idx_lab.setObjectName("kizCodeIdx")
            idx_lab.setFixedWidth(20)
            idx_lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            inp = QLineEdit(str(code or ""))
            inp.setObjectName("kizCodeInput")
            if err and str(code or "").strip():
                inp.setProperty("state", "error")
            inp.editingFinished.connect(partial(self._on_code_edited, oid))
            inp.returnPressed.connect(partial(self._on_code_edited, oid))
            clear_btn = QToolButton()
            clear_btn.setObjectName("kizCodeRemove")
            clear_btn.setText("×")
            clear_btn.setToolTip(
                "Удалить строку КИЗ" if can_remove else "Очистить маркировку"
            )
            clear_btn.clicked.connect(partial(self._clear_code, oid, idx))
            line.addWidget(idx_lab)
            line.addWidget(inp, 1)
            line.addWidget(clear_btn)
            block.addLayout(line)
            chip = self._code_status_label(row, code, err)
            if chip:
                block.addWidget(chip)
            lay.addLayout(block)
            inputs.append(inp)
        add_btn = QPushButton("+ Добавить КИЗ")
        add_btn.setObjectName("kizAddBtn")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(partial(self._add_code, oid))
        lay.addWidget(add_btn)
        if err and not any(str(c).strip() for c in codes):
            err_lab = QLabel(err)
            err_lab.setObjectName("kizRowError")
            err_lab.setWordWrap(True)
            lay.addWidget(err_lab)
        lay.addStretch(1)
        self._code_inputs[oid] = inputs
        return wrap

    def _build_actions_widget(self, row: Dict[str, Any]) -> QWidget:
        oid = int(row["order_id"])
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(4, 8, 8, 8)
        btn = QToolButton()
        btn.setObjectName("iconBtn")
        btn.setText("⋮")
        btn.setToolTip("Действия")
        btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(btn)
        menu.addAction(
            "Напечатать стикер", partial(self._print_sticker, oid)
        )
        btn.setMenu(menu)
        lay.addWidget(btn)
        return wrap

    def _on_code_edited(self, order_id: int) -> None:
        inp = self.sender()
        if not isinstance(inp, QLineEdit):
            return
        if getattr(inp, "_kiz_commit_lock", False):
            return
        inp._kiz_commit_lock = True
        try:
            if block_ru_layout_scan(self, inp):
                self._restore_code_input(order_id, inp)
                return
            self._sync_codes_from_inputs()
            row = next((r for r in self.rows if int(r["order_id"]) == order_id), None)
            if not row:
                return
            codes = [c for c in self._row_codes(row) if str(c).strip()]
            try:
                self.kiz.save_local(self.source_id, order_id, codes, wb_synced=False)
                row["kiz_wb_synced"] = False
                if codes:
                    row["kiz_status"] = "pending"
                self._sync_session_kiz_rows()
            except Exception:
                pass
            self._update_counter()
        finally:
            inp._kiz_commit_lock = False

    def _add_code(self, order_id: int) -> None:
        self._sync_codes_from_inputs()
        row = next((r for r in self.rows if int(r["order_id"]) == order_id), None)
        if not row:
            return
        codes = self._row_codes(row)
        codes.append("")
        row["kiz_codes"] = codes
        self._refresh_row(order_id)

    def _clear_code(self, order_id: int, idx: int) -> None:
        self._sync_codes_from_inputs()
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
            self._sync_session_kiz_rows()
        except Exception:
            pass
        self._refresh_row(order_id)
        self._update_counter()
        self._apply_filters()

    def _print_sticker(self, order_id: int) -> None:
        try:
            items = StickersService(self.kiz.db).order_stickers_png(
                self.api_key, [int(order_id)]
            )
            pngs = [it["png"] for it in items if it.get("png")]
            if not pngs:
                raise RuntimeError("WB не вернул стикер для заказа {}".format(order_id))
            show_png_list(pngs, "Стикер заказа {}".format(order_id), self)
        except Exception as exc:
            QMessageBox.critical(self, "Стикер", str(exc))

    def _set_row_widgets(
        self, table_idx: int, row: Dict[str, Any], *, active: bool = False
    ) -> None:
        oid = int(row["order_id"])
        if not active:
            active = self._pending_order_id == oid
        self.table.setCellWidget(
            table_idx, 0, self._wrap_cell(self._build_sticker_widget(row), active=active)
        )
        self.table.setCellWidget(
            table_idx, 1, self._wrap_cell(self._build_product_widget(row), active=active)
        )
        self.table.setCellWidget(
            table_idx, 2, self._wrap_cell(self._build_codes_widget(row), active=active)
        )
        self.table.setCellWidget(
            table_idx, 3, self._wrap_cell(self._build_actions_widget(row), active=active)
        )
        if active:
            self.table.selectRow(table_idx)

    def _resize_table_row(self, table_idx: int) -> None:
        self.table.resizeRowToContents(table_idx)
        self.table.setRowHeight(table_idx, max(self.table.rowHeight(table_idx), 96))

    def _refresh_row(self, order_id: int) -> None:
        idx = self._row_index_by_oid.get(int(order_id))
        row = self._row_by_oid.get(int(order_id))
        if idx is None or row is None:
            return
        self._set_row_widgets(idx, row)
        self._resize_table_row(idx)

    def _refresh_changed_rows(self, order_ids: List[int]) -> None:
        for oid in order_ids:
            self._refresh_row(oid)

    def _render_table(self) -> None:
        self._sync_codes_from_inputs()
        self._update_counter()
        self._clear_table()
        self._row_by_oid = {int(r["order_id"]): r for r in self.rows}
        row_count = len(self.rows)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(row_count)
        try:
            for i, r in enumerate(self.rows):
                oid = int(r["order_id"])
                self._row_index_by_oid[oid] = i
                self._set_row_widgets(i, r)
                if i and i % _RENDER_BATCH == 0:
                    QApplication.processEvents()
        finally:
            self.table.setUpdatesEnabled(True)
        for i in range(row_count):
            self._resize_table_row(i)
        self._apply_filters()

    def _find_by_sticker(self, raw: str) -> Optional[Dict[str, Any]]:
        scan = raw.replace(" ", "").strip()
        if not scan:
            return None
        found = self._sticker_map.get(scan)
        if found:
            return found
        tail = scan[-4:] if len(scan) >= 4 else scan
        matches = [
            r
            for r in self.rows
            if str(r.get("sticker_number") or "").endswith(tail)
            or str(r.get("sticker_part_b") or "") == tail
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def on_sticker(self) -> None:
        if not self._rows_ready:
            return
        if block_ru_layout_scan(self, self.sticker_input):
            return
        raw = self.sticker_input.text().replace(" ", "").strip()
        if not raw:
            return
        found = self._find_by_sticker(raw)
        if not found:
            self._set_info(
                "Заказ со стикером «{}» не найден среди товаров с маркировкой.".format(
                    raw
                )
            )
            self.sticker_input.selectAll()
            return
        self._set_info("")
        self.sticker_input.clear()
        pending_oid = int(found["order_id"])
        prev_pending = self._pending_order_id
        self._pending_order_id = pending_oid
        if prev_pending and prev_pending != pending_oid:
            self._refresh_row(prev_pending)
        self._refresh_row(pending_oid)
        dlg = KizMarkScanDialog(
            pending_oid,
            str(found.get("sticker_number") or "—"),
            self,
        )
        if dlg.exec_() != QDialog.Accepted:
            self._pending_order_id = None
            self._refresh_row(pending_oid)
            if prev_pending and prev_pending != pending_oid:
                self._refresh_row(prev_pending)
            self.sticker_input.setFocus()
            return
        self._apply_mark_scan(found, dlg.mark_code)
        self._pending_order_id = None
        self.sticker_input.setFocus()

    def _apply_mark_scan(self, row: Dict[str, Any], raw_mark: str) -> None:
        if block_ru_layout_scan(self, text=raw_mark):
            return
        oid = int(row["order_id"])
        code = raw_mark.strip(" \t\r\n").replace("\u2194", "\u001d")
        ok, err = self.kiz.validate_mark(
            code,
            row.get("skus") or [],
            bool(row.get("skip_kiz_gtin_check")),
        )
        if not ok:
            self.row_errors[oid] = err
            self._set_info(err)
            self._refresh_row(oid)
            self._apply_filters()
            return
        self.row_errors.pop(oid, None)
        codes = [c for c in self._row_codes(row) if str(c).strip()]
        if code in codes:
            self._set_info("Этот КИЗ уже добавлен")
            self._refresh_row(oid)
            return
        placed = False
        mutable = self._row_codes(row)
        for i, c in enumerate(mutable):
            if not str(c or "").strip():
                mutable[i] = code
                placed = True
                break
        if not placed:
            mutable.append(code)
        row["kiz_codes"] = mutable
        row["kiz_status"] = "pending"
        row["kiz_wb_synced"] = False
        self.kiz.save_local(self.source_id, oid, [c for c in mutable if str(c).strip()], wb_synced=False)
        self._sync_session_kiz_rows()
        self._set_info("", ok=True)
        self._update_counter()
        self._refresh_row(oid)
        self._apply_filters()

    def save_all(self) -> None:
        if self._saving:
            return
        self._sync_codes_from_inputs()
        self._saving = True
        self.save_btn.setEnabled(False)
        errors = []
        saved = 0
        touched = []  # type: List[int]
        try:
            for r in self.rows:
                codes = [c for c in self._row_codes(r) if str(c).strip(" \t\r\n")]
                if not codes:
                    continue
                oid = int(r["order_id"])
                for code in codes:
                    ok, err = self.kiz.validate_mark(
                        code,
                        r.get("skus") or [],
                        bool(r.get("skip_kiz_gtin_check")),
                    )
                    if not ok:
                        self.row_errors[oid] = err
                        errors.append("{}: {}".format(oid, err))
                        touched.append(oid)
                        break
                else:
                    try:
                        self.kiz.save_to_wb(
                            self.source_id, self.api_key, oid, codes
                        )
                        self.row_errors.pop(oid, None)
                        r["kiz_wb_synced"] = True
                        r["kiz_status"] = "ok"
                        saved += 1
                        touched.append(oid)
                    except Exception as exc:
                        self.row_errors[oid] = str(exc)
                        errors.append("{}: {}".format(oid, exc))
                        touched.append(oid)
            self._sync_session_kiz_rows()
            self._refresh_changed_rows(sorted(set(touched)))
            self._apply_filters()
            if errors:
                self._set_info("\n".join(errors[:3]))
                if len(errors) > 3:
                    QMessageBox.warning(
                        self, "КИЗ", "\n".join(errors[:12])
                    )
            elif saved:
                self._set_info("Сохранено в WB: {} заказ(ов)".format(saved), ok=True)
            else:
                self._set_info("Нет изменений для сохранения")
        finally:
            self._saving = False
            self.save_btn.setEnabled(True)

    def _sync_session_kiz_rows(self) -> None:
        session = supply_session.get_session(self.source_id, self.supply_id)
        if not session:
            return
        by_oid = {int(r["order_id"]): r for r in self.rows}
        updated = []
        for r in session.kiz_rows or []:
            oid = int(r.get("order_id") or 0)
            src = by_oid.get(oid)
            if src:
                updated.append(dict(src))
            else:
                updated.append(r)
        session.kiz_rows = updated
        for r in session.rows or []:
            oid = int(r.get("order_id") or 0)
            src = by_oid.get(oid)
            if not src:
                continue
            r["kiz_codes"] = list(src.get("kiz_codes") or [])
            r["kiz_wb_synced"] = bool(src.get("kiz_wb_synced"))
            r["kiz_status"] = src.get("kiz_status") or r.get("kiz_status")
            r["kiz_required"] = True
        supply_session.put_session(session)
