# -*- coding: utf-8 -*-
"""Pick verify modal — KIZ modal layout parity (ШК instead of КИЗ)."""
from __future__ import annotations

from functools import partial
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QApplication,
)

from app.services.kiz_pick import PickVerifyService, row_matches_modal_search
from app.services import supply_session
from app.services.local_autosave import LocalAutosaveQueue
from app.services.print_docs import _fetch_picking_stickers
from app.services.sticker_lookup import (
    build_sticker_index,
    find_row_by_sticker,
    normalize_scan,
)
from app.services.trbx_stickers import StickersService
from app.ui.dialog_utils import (
    apply_fullscreen_on_show,
    block_ru_layout_scan,
    fullscreen_parent,
    init_fullscreen_dialog,
    install_live_ru_layout_guard,
    make_modal_search_box,
    style_app_menu,
)
from app.ui.dialogs_extra import show_png_list
from app.ui.format_helpers import (
    build_order_cell_widget,
    build_product_cell_widget,
)
from app.ui.table_col_widths import PersistentColumnWidths
from app.wb import cancel_reason_label, is_cancelled_status


def _sticker_number(part_a: str, part_b: str) -> str:
    return "{}{}".format(str(part_a or "").strip(), str(part_b or "").strip())


_RENDER_BATCH = 50
_FILTER_EMPTY_MSG = "Нет строк по выбранным фильтрам"
_LOAD_STEPS = (
    "Заказы",
    "Номера стикеров",
    "Отрисовка таблицы",
)


class PickDialog(QDialog):
    """Товары без маркировки — portal layout parity with KIZ modal."""

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
        self.row_errors = {}  # type: Dict[int, str]
        self._sticker_index = build_sticker_index([])  # type: Dict[str, Any]
        self._pending_order_id = None  # type: Optional[int]
        self._pending_row = None  # type: Optional[Dict[str, Any]]
        self._sku_inputs = {}  # type: Dict[int, QLineEdit]
        self._row_index_by_oid = {}  # type: Dict[int, int]
        self._row_by_oid = {}  # type: Dict[int, Dict[str, Any]]
        self._rows_ready = False
        self._closing = False
        self._load_gen = 0
        self._load_step = 0
        self._load_detail = ""
        self._loading_table_label = None  # type: Optional[QLabel]
        self.data_changed = False
        self._autosave = LocalAutosaveQueue(self.pick.db)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(0)
        self._autosave_timer.timeout.connect(self._flush_autosave_async)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_filters)
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.timeout.connect(self.load_rows)

        self.setObjectName("kizModal")
        self.setWindowTitle("Товары без маркировки · {}".format(supply_id))
        init_fullscreen_dialog(
            self,
            fullscreen=fullscreen,
            default_size=(1200, 820),
            minimum_size=(900, 640),
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("kizHeader")
        header_lay = QVBoxLayout(header)
        header_lay.setContentsMargins(24, 20, 24, 16)
        header_lay.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.setSpacing(16)
        title = QLabel("Товары без маркировки")
        title.setObjectName("kizTitle")
        title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        title_row.addWidget(title, 0, Qt.AlignLeft | Qt.AlignTop)
        title_row.addStretch(1)

        header_lay.addLayout(title_row)
        root.addWidget(header)

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
        tb.addLayout(filters, 0)
        tb.addStretch(1)
        search_box, self.search_input = make_modal_search_box()
        tb.addWidget(search_box, 0, Qt.AlignRight | Qt.AlignVCenter)
        self.counter = QLabel("Проверено 0 из 0")
        self.counter.setObjectName("kizScanCount")
        tb.addWidget(self.counter, 0, Qt.AlignRight | Qt.AlignVCenter)
        root.addWidget(toolbar)

        self.chk_filled.toggled.connect(self._on_filled_toggled)
        self.chk_empty.toggled.connect(self._on_empty_toggled)
        self.chk_errors.toggled.connect(self._apply_filters)
        self.chk_cancelled.toggled.connect(self._apply_filters)
        self.search_input.textChanged.connect(self._schedule_filter)

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
        install_live_ru_layout_guard(self.sticker_input, self)
        sticker_clear = QToolButton()
        sticker_clear.setObjectName("kizScanClear")
        sticker_clear.setText("✕")
        sticker_clear.clicked.connect(self.sticker_input.clear)
        scan_lay.addWidget(scan_lab)
        scan_lay.addWidget(self.sticker_input, 1)
        scan_lay.addWidget(sticker_clear)
        root.addWidget(scan_bar)

        # Inline SKU prompt (web #wbFbsPickScanPrompt)
        self.scan_prompt = QFrame()
        self.scan_prompt.setObjectName("kizScanPrompt")
        self.scan_prompt.hide()
        prompt_lay = QVBoxLayout(self.scan_prompt)
        prompt_lay.setContentsMargins(24, 12, 24, 12)
        prompt_lay.setSpacing(8)
        prompt_title = QLabel("Просканируйте ШК")
        prompt_title.setObjectName("kizPromptTitle")
        prompt_lay.addWidget(prompt_title)
        self.scan_prompt_meta = QLabel("")
        self.scan_prompt_meta.setObjectName("kizPromptMeta")
        self.scan_prompt_meta.setWordWrap(True)
        prompt_lay.addWidget(self.scan_prompt_meta)
        prompt_row = QHBoxLayout()
        prompt_row.setSpacing(8)
        self.sku_prompt_input = QLineEdit()
        self.sku_prompt_input.setObjectName("kizScanInput")
        self.sku_prompt_input.setPlaceholderText("Сканируйте ШК с того же изделия")
        self.sku_prompt_input.returnPressed.connect(self._on_sku_prompt_enter)
        install_live_ru_layout_guard(self.sku_prompt_input, self)
        sku_clear = QToolButton()
        sku_clear.setObjectName("kizScanClear")
        sku_clear.setText("✕")
        sku_clear.setToolTip("Очистить")
        sku_clear.clicked.connect(self.sku_prompt_input.clear)
        prompt_cancel = QPushButton("Отмена")
        prompt_cancel.setObjectName("secondary")
        prompt_cancel.clicked.connect(lambda: self._hide_sku_prompt())
        prompt_row.addWidget(self.sku_prompt_input, 1)
        prompt_row.addWidget(sku_clear)
        prompt_row.addWidget(prompt_cancel)
        prompt_lay.addLayout(prompt_row)
        root.addWidget(self.scan_prompt)

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

        self.table = QTableWidget(0, 4)
        self.table.setObjectName("kizTable")
        self.table.setAlternatingRowColors(False)
        self.table.setHorizontalHeaderLabels(["Заказ", "Товар", "ШК", ""])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(148)
        self._col_widths = PersistentColumnWidths(
            self.pick.db,
            self.table,
            "pick_modal_table_cols",
            [200, 420, 340, 52],
            parent=self,
        )
        self._col_widths.apply()
        root.addWidget(self.table, 1)

        self._set_filters_ready(False)
        self._show_loading_row()
        # Shell-first open (web parity): paint modal, then fill from session.
        # Owned QTimer + generation so a fast close cannot paint a dead dialog.
        self._load_gen += 1
        self._load_timer.start(0)

    def showEvent(self, event) -> None:
        super(PickDialog, self).showEvent(event)
        apply_fullscreen_on_show(self)
        if self._rows_ready:
            self.sticker_input.setFocus()

    def _abort_deferred_load(self) -> None:
        self._closing = True
        self._load_gen += 1
        self._load_timer.stop()

    def _load_aborted(self, gen: int) -> bool:
        return self._closing or gen != self._load_gen

    def reject(self) -> None:
        self._abort_deferred_load()
        self._hide_sku_prompt(refocus=False)
        self._flush_autosave_sync()
        super(PickDialog, self).reject()

    def accept(self) -> None:
        self._abort_deferred_load()
        self._hide_sku_prompt(refocus=False)
        self._flush_autosave_sync()
        super(PickDialog, self).accept()

    def closeEvent(self, event) -> None:
        self._abort_deferred_load()
        self._hide_sku_prompt(refocus=False)
        self._flush_autosave_sync()
        super(PickDialog, self).closeEvent(event)

    def _schedule_pick_autosave(
        self, order_id: int, verified: bool, barcode: str = ""
    ) -> None:
        self._autosave.schedule_pick(
            self.source_id, int(order_id), verified, barcode
        )
        self._autosave_timer.start()

    def _flush_autosave_async(self) -> None:
        self._autosave.flush_async()

    def _flush_autosave_sync(self) -> None:
        self._autosave_timer.stop()
        self._autosave.flush_sync()

    def _rebuild_sticker_index(self) -> None:
        self._sticker_index = build_sticker_index(self.rows)

    def _show_sku_prompt(self, row: Dict[str, Any]) -> None:
        oid = int(row["order_id"])
        sticker = str(row.get("sticker_number") or "—")
        self._pending_order_id = oid
        self._pending_row = row
        self.scan_prompt_meta.setText(
            "Заказ {} · стикер {}".format(oid, sticker)
        )
        self.sku_prompt_input.clear()
        self.scan_prompt.show()
        self.sticker_input.setEnabled(False)
        QTimer.singleShot(0, self.sku_prompt_input.setFocus)

    def _hide_sku_prompt(self, *, refocus: bool = True) -> None:
        was_pending = self._pending_order_id
        self.scan_prompt.hide()
        self.sku_prompt_input.clear()
        self._pending_row = None
        self.sticker_input.setEnabled(True)
        if was_pending is not None:
            self._pending_order_id = None
            self._patch_sku_cell(was_pending)
        if refocus and self._rows_ready:
            self.sticker_input.setFocus()

    def _on_sku_prompt_enter(self) -> None:
        if block_ru_layout_scan(self, self.sku_prompt_input):
            return
        row = self._pending_row
        if not row:
            self._hide_sku_prompt()
            return
        code = self.sku_prompt_input.text()
        self.scan_prompt.hide()
        self.sku_prompt_input.clear()
        self.sticker_input.setEnabled(True)
        self._pending_row = None
        self._pending_order_id = None
        self._apply_sku_scan(row, code)
        self.sticker_input.setFocus()

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
        self.sticker_input.setReadOnly(not ready)
        self.sticker_input.setToolTip(
            "" if ready else "Дождитесь загрузки строк"
        )

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

    @staticmethod
    def _row_is_verified(row: Dict[str, Any]) -> bool:
        return bool(row.get("pick_verified")) and bool(
            str(row.get("pick_barcode") or "").strip()
        )

    def _row_is_empty(self, row: Dict[str, Any]) -> bool:
        return not self._row_is_verified(row)

    def _row_is_cancelled(self, row: Dict[str, Any]) -> bool:
        if str(row.get("cancel_reason_label") or "").strip():
            return True
        return is_cancelled_status(
            supplier_status=row.get("supplier_status"),
            wb_status=row.get("wb_status"),
        )

    def _row_passes_filters(self, row: Dict[str, Any]) -> bool:
        if self.chk_filled.isChecked() and self._row_is_empty(row):
            return False
        if self.chk_empty.isChecked() and not self._row_is_empty(row):
            return False
        oid = int(row["order_id"])
        if self.chk_errors.isChecked() and oid not in self.row_errors:
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
    def _row_matches_search(row: Dict[str, Any], query: str) -> bool:
        return row_matches_modal_search(row, query)

    def _update_counter(self) -> None:
        filled = sum(1 for r in self.rows if self._row_is_verified(r))
        total = len(self.rows)
        self.counter.setText("Проверено {} из {}".format(filled, total))

    def _clear_table(self) -> None:
        self.table.clearSpans()
        self.table.setRowCount(0)
        self.table.clearContents()
        self._row_index_by_oid = {}
        self._sku_inputs = {}
        self._loading_table_label = None

    def _show_loading_row(self) -> None:
        self._clear_table()
        self.table.setRowCount(1)
        self.table.setSpan(0, 0, 1, self.table.columnCount())
        loading = QLabel("")
        loading.setObjectName("hint")
        loading.setAlignment(Qt.AlignCenter)
        loading.setWordWrap(True)
        loading.setContentsMargins(24, 32, 24, 32)
        self._loading_table_label = loading
        self._load_step = 0
        self._load_detail = ""
        self._render_load_status()
        self.table.setCellWidget(0, 0, loading)

    def _render_load_status(self) -> None:
        lab = self._loading_table_label
        if lab is None:
            return
        step = int(self._load_step or 0)
        total = len(_LOAD_STEPS)
        if step <= 0:
            lines = ["<b>Подготовка проверки ШК…</b>"]
            lines.extend("○ {}".format(name) for name in _LOAD_STEPS)
            lab.setTextFormat(Qt.RichText)
            lab.setText("<br>".join(lines))
            return
        lines = [
            "<b>Загрузка · шаг {} из {}</b>".format(min(step, total), total)
        ]
        for i, name in enumerate(_LOAD_STEPS, start=1):
            if i < step:
                mark, style = "✓", "color:#166534;"
            elif i == step:
                mark, style = "→", "color:#1d4ed8;font-weight:700;"
            else:
                mark, style = "○", "color:#64748b;"
            detail = ""
            if i == step and self._load_detail:
                detail = " <span style='color:#64748b;font-weight:500;'>({})</span>".format(
                    self._load_detail
                )
            lines.append(
                "<span style='{}'>{} {}{}</span>".format(style, mark, name, detail)
            )
        lab.setTextFormat(Qt.RichText)
        lab.setText("<br>".join(lines))

    def _set_load_step(self, step: int, detail: str = "", *, pump: bool = True) -> None:
        self._load_step = int(step or 0)
        self._load_detail = str(detail or "").strip()
        self._render_load_status()
        if pump:
            QApplication.processEvents()

    def load_rows(self) -> None:
        gen = self._load_gen
        if self._load_aborted(gen):
            return
        self._set_filters_ready(False)
        session = supply_session.get_session(self.source_id, self.supply_id)
        fast = bool(
            session
            and session.core_ready
            and session.pick_rows is not None
        )
        if not fast:
            self._show_loading_row()
            self._set_load_step(1, "из локальной базы")
        if self._load_aborted(gen):
            return
        try:
            if fast:
                self.rows = [dict(r) for r in session.pick_rows]
            else:
                self.rows = self.pick.rows(self.source_id, self.supply_id, self.api_key)
        except Exception as exc:
            if self._load_aborted(gen):
                return
            self.rows = []
            self._set_info(str(exc))
            self._render_table(fast=False)
            if self._load_aborted(gen):
                return
            self._set_filters_ready(True)
            return
        if self._load_aborted(gen):
            return
        self.row_errors = {}
        self._sticker_index = build_sticker_index([])
        stickers = {}  # type: Dict[int, Dict[str, Any]]
        order_n = len(self.rows)
        need_sticker_fill = any(
            not str(r.get("sticker_number") or "").strip()
            and not str(r.get("sticker_part_b") or "").strip()
            for r in self.rows
        )
        if session and session.sticker_numbers:
            if not fast:
                self._set_load_step(2, "из сессии · {} шт.".format(order_n))
            stickers = session.sticker_numbers
        elif need_sticker_fill:
            if not fast:
                self._set_load_step(2, "0 из {}".format(order_n) if order_n else "")
            try:
                ids = [int(r["order_id"]) for r in self.rows]
                stickers = _fetch_picking_stickers(self.api_key, ids)
            except Exception:
                stickers = {}
            if not fast and not self._load_aborted(gen):
                self._set_load_step(
                    2, "{} из {}".format(len(stickers), order_n) if order_n else ""
                )
        if self._load_aborted(gen):
            return
        if stickers:
            for r in self.rows:
                oid = int(r["order_id"])
                st = stickers.get(oid) or {}
                if not st:
                    continue
                part_a = str(st.get("partA") or r.get("sticker_part_a") or "").strip()
                part_b = str(st.get("partB") or r.get("sticker_part_b") or "").strip()
                barcode = str(
                    st.get("barcode") or r.get("sticker_barcode") or ""
                ).strip()
                r["sticker_part_a"] = part_a
                r["sticker_part_b"] = part_b
                r["sticker_barcode"] = barcode
                full = _sticker_number(part_a, part_b)
                r["sticker_number"] = full or str(r.get("sticker_number") or "")
        if not fast:
            self._set_load_step(3, "{} строк".format(order_n) if order_n else "")
        if self._load_aborted(gen):
            return
        self._render_table(fast=fast)
        if self._load_aborted(gen):
            return
        if not self.rows:
            self._set_info("В поставке нет заказов для проверки ШК")
        else:
            self._set_info("")
        self._set_filters_ready(True)
        self.sticker_input.setFocus()

    @staticmethod
    def _wrap_cell(inner: QWidget, *, active: bool = False) -> QFrame:
        frame = QFrame()
        frame.setObjectName("kizRowCell")
        if active:
            frame.setProperty("state", "active")
            frame.style().unpolish(frame)
            frame.style().polish(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(inner)
        return frame

    def _build_sticker_widget(self, row: Dict[str, Any]) -> QWidget:
        return build_order_cell_widget(row)

    def _build_product_widget(self, row: Dict[str, Any]) -> QWidget:
        return build_product_cell_widget(row)

    def _sku_status_label(self, row: Dict[str, Any], err: str) -> Optional[QLabel]:
        if err:
            lab = QLabel(err)
            lab.setObjectName("kizCodeStatus")
            lab.setProperty("state", "error")
        elif self._row_is_verified(row):
            lab = QLabel("Проверка пройдена")
            lab.setObjectName("kizCodeStatus")
            lab.setProperty("state", "ok")
        else:
            return None
        lab.setWordWrap(True)
        lab.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lab.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lab.style().unpolish(lab)
        lab.style().polish(lab)
        return lab

    def _build_sku_widget(self, row: Dict[str, Any]) -> QWidget:
        oid = int(row["order_id"])
        err = self.row_errors.get(oid, "")
        barcode = str(row.get("pick_barcode") or "")
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        line = QHBoxLayout()
        line.setSpacing(8)
        inp = QLineEdit(barcode)
        inp.setObjectName("kizCodeInput")
        if err:
            inp.setProperty("state", "error")
        inp.returnPressed.connect(partial(self._on_sku_edited, oid))
        inp.editingFinished.connect(partial(self._on_sku_edited, oid))
        clear_btn = QToolButton()
        clear_btn.setObjectName("kizCodeRemove")
        clear_btn.setText("×")
        clear_btn.setToolTip("Сбросить проверку")
        clear_btn.setFixedSize(36, 36)
        clear_btn.clicked.connect(partial(self._clear_sku, oid))
        mid = QVBoxLayout()
        mid.setSpacing(4)
        mid.setContentsMargins(0, 0, 0, 0)
        mid.addWidget(inp)
        chip = self._sku_status_label(row, err)
        if chip:
            mid.addWidget(chip)
        line.addLayout(mid, 1)
        line.addWidget(clear_btn, 0, Qt.AlignTop)
        lay.addLayout(line)
        lay.addStretch(1)
        self._sku_inputs[oid] = inp
        return wrap

    def _build_actions_widget(self, row: Dict[str, Any]) -> QWidget:
        oid = int(row["order_id"])
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(4, 8, 8, 8)
        lay.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        btn = QToolButton()
        btn.setObjectName("kizRowMenu")
        btn.setText("⋮")
        btn.setToolTip("Действия")
        btn.setPopupMode(QToolButton.InstantPopup)
        btn.setFixedSize(36, 36)
        menu = style_app_menu(QMenu(btn))
        menu.addAction("Напечатать стикер", partial(self._print_sticker, oid))
        btn.setMenu(menu)
        lay.addWidget(btn, 0, Qt.AlignTop)
        return wrap

    def _restore_sku_input(self, order_id: int) -> None:
        inp = self._sku_inputs.get(int(order_id))
        row = self._row_by_oid.get(int(order_id))
        if inp is None or row is None:
            return
        inp.setText(str(row.get("pick_barcode") or ""))

    def _on_sku_edited(self, order_id: int) -> None:
        inp = self.sender()
        if not isinstance(inp, QLineEdit):
            inp = self._sku_inputs.get(int(order_id))
        if inp is None:
            return
        if getattr(inp, "_pick_commit_lock", False):
            return
        inp._pick_commit_lock = True
        try:
            if block_ru_layout_scan(self, inp):
                self._restore_sku_input(order_id)
                return
            code = inp.text().strip()
            if not code:
                return
            row = self._row_by_oid.get(int(order_id))
            if not row:
                return
            if self._row_is_verified(row) and code == str(row.get("pick_barcode") or ""):
                return
            self._apply_sku(row, code)
        finally:
            inp._pick_commit_lock = False

    def _clear_sku(self, order_id: int) -> None:
        row = self._row_by_oid.get(int(order_id))
        if not row:
            return
        row["pick_verified"] = False
        row["pick_barcode"] = ""
        self.row_errors.pop(order_id, None)
        self.data_changed = True
        self._schedule_pick_autosave(order_id, False, "")
        self._sync_session_pick_rows()
        self._update_counter()
        self._patch_sku_cell(order_id)
        self._apply_filters()

    def _apply_sku(self, row: Dict[str, Any], code: str) -> None:
        oid = int(row["order_id"])
        ok, err = self.pick.validate_barcode(code, row.get("skus") or [])
        if not ok:
            self.row_errors[oid] = err
            self._set_info(err)
            self._refresh_row(oid)
            self._apply_filters()
            return
        self.row_errors.pop(oid, None)
        self.data_changed = True
        row["pick_verified"] = True
        row["pick_barcode"] = code
        for r in self.rows:
            if int(r["order_id"]) == oid:
                r["pick_verified"] = True
                r["pick_barcode"] = code
                break
        self._schedule_pick_autosave(oid, True, code)
        self._sync_session_pick_rows()
        self._set_info("", ok=True)
        self._update_counter()
        # Full row refresh clears pending highlight on order/product columns.
        self._refresh_row(oid)
        self._apply_filters()

    def _print_sticker(self, order_id: int) -> None:
        try:
            items = StickersService(self.pick.db).order_stickers_png(
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
            table_idx, 2, self._wrap_cell(self._build_sku_widget(row), active=active)
        )
        self.table.setCellWidget(
            table_idx, 3, self._wrap_cell(self._build_actions_widget(row), active=active)
        )
        if active:
            self.table.selectRow(table_idx)

    def _resize_table_row(self, table_idx: int) -> None:
        self.table.resizeRowToContents(table_idx)
        self.table.setRowHeight(table_idx, max(self.table.rowHeight(table_idx), 148))

    def _refresh_row(self, order_id: int) -> None:
        idx = self._row_index_by_oid.get(int(order_id))
        row = self._row_by_oid.get(int(order_id))
        if idx is None or row is None:
            return
        self._set_row_widgets(idx, row)
        self._resize_table_row(idx)

    def _patch_sku_cell(self, order_id: int) -> None:
        oid = int(order_id)
        idx = self._row_index_by_oid.get(oid)
        row = self._row_by_oid.get(oid)
        if idx is None or row is None:
            return
        active = self._pending_order_id == oid
        self.table.setCellWidget(
            idx, 2, self._wrap_cell(self._build_sku_widget(row), active=active)
        )
        if active:
            self.table.selectRow(idx)
        self._resize_table_row(idx)

    def _render_table(self, *, fast: bool = False) -> None:
        self._update_counter()
        self._clear_table()
        self._row_by_oid = {int(r["order_id"]): r for r in self.rows}
        self._rebuild_sticker_index()
        row_count = len(self.rows)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(row_count)
        try:
            for i, r in enumerate(self.rows):
                oid = int(r["order_id"])
                self._row_index_by_oid[oid] = i
                self._set_row_widgets(i, r)
                if fast:
                    self.table.setRowHeight(i, 148)
                elif i and i % _RENDER_BATCH == 0:
                    QApplication.processEvents()
        finally:
            self.table.setUpdatesEnabled(True)
        if not fast:
            for i in range(row_count):
                self._resize_table_row(i)
        self._apply_filters()

    def _set_ambiguous_sticker_info(self, matches: List[Dict[str, Any]]) -> None:
        ids = ", ".join(str(r.get("order_id") or "") for r in matches[:5])
        more = "…" if len(matches) > 5 else ""
        self._set_info(
            "Код стикера совпадает у нескольких заказов ({}{}). "
            "Отсканируйте QR стикера ещё раз.".format(ids, more)
        )

    def on_sticker(self) -> None:
        if not self._rows_ready or self.sticker_input.isReadOnly():
            return
        if self.scan_prompt.isVisible():
            return
        if block_ru_layout_scan(self, self.sticker_input):
            return
        raw = normalize_scan(self.sticker_input.text())
        if not raw:
            return
        found, ambiguous, matches = find_row_by_sticker(
            self.rows, raw, index=self._sticker_index
        )
        if ambiguous:
            self._set_ambiguous_sticker_info(matches)
            self.sticker_input.selectAll()
            return
        if not found:
            self._set_info(
                "Заказ со стикером «{}» не найден среди товаров без маркировки.".format(
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
        self._show_sku_prompt(found)

    def _apply_sku_scan(self, row: Dict[str, Any], raw_barcode: str) -> None:
        if block_ru_layout_scan(self, text=raw_barcode):
            return
        code = str(raw_barcode or "").strip()
        if not code:
            return
        self._apply_sku(row, code)

    def _sync_session_pick_rows(self) -> None:
        session = supply_session.get_session(self.source_id, self.supply_id)
        if not session:
            return
        by_oid = {int(r["order_id"]): r for r in self.rows}
        updated = []
        for r in session.pick_rows or []:
            oid = int(r.get("order_id") or 0)
            src = by_oid.get(oid)
            if src:
                updated.append(dict(src))
            else:
                updated.append(r)
        session.pick_rows = updated
        for r in session.rows or []:
            oid = int(r.get("order_id") or 0)
            src = by_oid.get(oid)
            if not src:
                continue
            r["pick_verified"] = bool(src.get("pick_verified"))
            r["pick_barcode"] = str(src.get("pick_barcode") or "")
        supply_session.put_session(session)
