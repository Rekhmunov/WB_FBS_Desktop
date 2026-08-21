# -*- coding: utf-8 -*-
"""КИЗ marking modal — web portal layout parity."""
from __future__ import annotations

from functools import partial
from typing import Any, Dict, List, Optional, Set

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
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

from app.db import Database
from app.services.kiz_pick import (
    KizService,
    find_existing_mark,
    pending_wb_save_jobs,
    row_matches_modal_search,
)
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
    GsAwareLineEdit,
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


class _KizSaveWorker(QThread):
    """Upload pending KIZ codes to WB without blocking the UI."""

    progress = pyqtSignal(int, int, int, bool, str)  # done, total, order_id, ok, error
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        db: Database,
        source_id: int,
        api_key: str,
        jobs: List[Dict[str, Any]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_KizSaveWorker, self).__init__(parent)
        self.db = db
        self.source_id = int(source_id)
        self.api_key = str(api_key or "")
        self.jobs = list(jobs or [])
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            kiz = KizService(self.db)
            total = len(self.jobs)
            saved = 0
            errors = []  # type: List[Dict[str, Any]]
            for index, job in enumerate(self.jobs, start=1):
                if self._stop:
                    break
                oid = int(job.get("order_id") or 0)
                codes = list(job.get("codes") or [])
                err = ""
                ok = False
                for code in codes:
                    valid, msg = kiz.validate_mark(
                        code,
                        job.get("skus") or [],
                        bool(job.get("skip_kiz_gtin_check")),
                    )
                    if not valid:
                        err = msg
                        break
                else:
                    try:
                        kiz.save_to_wb(self.source_id, self.api_key, oid, codes)
                        ok = True
                        saved += 1
                    except Exception as exc:
                        err = str(exc)
                if not ok:
                    errors.append({"order_id": oid, "error": err or "Ошибка сохранения"})
                self.progress.emit(index, total, oid, ok, err)
            self.finished_ok.emit(
                {
                    "saved": saved,
                    "errors": errors,
                    "stopped": self._stop,
                    "total": total,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))


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
        self._sticker_index = build_sticker_index([])  # type: Dict[str, Any]
        self._pending_order_id = None  # type: Optional[int]
        self._pending_row = None  # type: Optional[Dict[str, Any]]
        self._code_inputs = {}  # type: Dict[int, List[GsAwareLineEdit]]
        self._row_index_by_oid = {}  # type: Dict[int, int]
        self._row_by_oid = {}  # type: Dict[int, Dict[str, Any]]
        self._rows_ready = False
        self._closing = False
        self._load_gen = 0
        self._load_step = 0
        self._load_detail = ""
        self._loading_table_label = None  # type: Optional[QLabel]
        self.data_changed = False
        self._saving = False
        self._save_worker = None  # type: Optional[_KizSaveWorker]
        self._alive_workers = []  # type: List[QThread]
        self._save_failed_oids = set()  # type: Set[int]
        self._save_retry_mode = False
        self._autosave = LocalAutosaveQueue(self.kiz.db)
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
        self.setWindowTitle("Товары с маркировкой · {}".format(supply_id))
        init_fullscreen_dialog(
            self,
            fullscreen=fullscreen,
            default_size=(1200, 820),
            minimum_size=(900, 640),
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header — title row (left) + Save (right)
        header = QFrame()
        header.setObjectName("kizHeader")
        header_lay = QVBoxLayout(header)
        header_lay.setContentsMargins(24, 20, 24, 16)
        header_lay.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.setSpacing(16)
        title = QLabel("Товары с маркировкой")
        title.setObjectName("kizTitle")
        title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        title_row.addWidget(title, 0, Qt.AlignLeft | Qt.AlignTop)
        title_row.addStretch(1)

        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("bottomPrimary")
        self.save_btn.setFixedHeight(40)
        self.save_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.save_btn.clicked.connect(self.save_all)
        title_row.addWidget(self.save_btn, 0, Qt.AlignRight | Qt.AlignTop)

        header_lay.addLayout(title_row)
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
        tb.addLayout(filters, 0)
        tb.addStretch(1)
        search_box, self.search_input = make_modal_search_box()
        tb.addWidget(search_box, 0, Qt.AlignRight | Qt.AlignVCenter)
        self.counter = QLabel("Просканировано 0 из 0 КИЗ")
        self.counter.setObjectName("kizScanCount")
        tb.addWidget(self.counter, 0, Qt.AlignRight | Qt.AlignVCenter)
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
        install_live_ru_layout_guard(self.sticker_input, self)
        sticker_clear = QToolButton()
        sticker_clear.setObjectName("kizScanClear")
        sticker_clear.setText("✕")
        sticker_clear.clicked.connect(self.sticker_input.clear)
        scan_lay.addWidget(scan_lab)
        scan_lay.addWidget(self.sticker_input, 1)
        scan_lay.addWidget(sticker_clear)
        root.addWidget(scan_bar)

        # Inline mark prompt (web #wbFbsKizScanPrompt) — no nested QDialog.exec_
        self.scan_prompt = QFrame()
        self.scan_prompt.setObjectName("kizScanPrompt")
        self.scan_prompt.hide()
        prompt_lay = QVBoxLayout(self.scan_prompt)
        prompt_lay.setContentsMargins(24, 12, 24, 12)
        prompt_lay.setSpacing(8)
        prompt_title = QLabel("Просканируйте маркировку")
        prompt_title.setObjectName("kizPromptTitle")
        prompt_lay.addWidget(prompt_title)
        self.scan_prompt_meta = QLabel("")
        self.scan_prompt_meta.setObjectName("kizPromptMeta")
        self.scan_prompt_meta.setWordWrap(True)
        prompt_lay.addWidget(self.scan_prompt_meta)
        prompt_row = QHBoxLayout()
        prompt_row.setSpacing(8)
        self.mark_input = GsAwareLineEdit()
        self.mark_input.setObjectName("kizScanInput")
        self.mark_input.setPlaceholderText("Сканируйте КИЗ с того же изделия")
        self.mark_input.returnPressed.connect(self._on_mark_prompt_enter)
        install_live_ru_layout_guard(self.mark_input, self)
        mark_clear = QToolButton()
        mark_clear.setObjectName("kizScanClear")
        mark_clear.setText("✕")
        mark_clear.setToolTip("Очистить")
        mark_clear.clicked.connect(self.mark_input.clear)
        prompt_cancel = QPushButton("Отмена")
        prompt_cancel.setObjectName("secondary")
        prompt_cancel.clicked.connect(lambda: self._hide_mark_prompt())
        prompt_row.addWidget(self.mark_input, 1)
        prompt_row.addWidget(mark_clear)
        prompt_row.addWidget(prompt_cancel)
        prompt_lay.addLayout(prompt_row)
        root.addWidget(self.scan_prompt)

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
        self.table.setAlternatingRowColors(False)
        self.table.setHorizontalHeaderLabels(
            ["Заказ", "Товар", "КИЗ", ""]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(148)
        self._col_widths = PersistentColumnWidths(
            self.kiz.db,
            self.table,
            "kiz_modal_table_cols",
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
        super(KizDialog, self).showEvent(event)
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
        self._hide_mark_prompt(refocus=False)
        self._flush_autosave_sync()
        self._stop_save_worker()
        super(KizDialog, self).reject()

    def accept(self) -> None:
        self._abort_deferred_load()
        self._hide_mark_prompt(refocus=False)
        self._flush_autosave_sync()
        self._stop_save_worker()
        super(KizDialog, self).accept()

    def closeEvent(self, event) -> None:
        self._abort_deferred_load()
        self._hide_mark_prompt(refocus=False)
        self._flush_autosave_sync()
        self._stop_save_worker()
        super(KizDialog, self).closeEvent(event)

    def _schedule_kiz_autosave(self, order_id: int, codes: List[str]) -> None:
        self._autosave.schedule_kiz(self.source_id, int(order_id), codes)
        self._autosave_timer.start()

    def _flush_autosave_async(self) -> None:
        self._autosave.flush_async()

    def _flush_autosave_sync(self) -> None:
        self._autosave_timer.stop()
        self._autosave.flush_sync()

    def _rebuild_sticker_index(self) -> None:
        self._sticker_index = build_sticker_index(self.rows)

    def _show_mark_prompt(self, row: Dict[str, Any]) -> None:
        oid = int(row["order_id"])
        sticker = str(row.get("sticker_number") or "—")
        self._pending_order_id = oid
        self._pending_row = row
        self.scan_prompt_meta.setText(
            "Заказ {} · стикер {}".format(oid, sticker)
        )
        self.mark_input.clear()
        self.scan_prompt.show()
        self.sticker_input.setEnabled(False)
        QTimer.singleShot(0, self.mark_input.setFocus)

    def _hide_mark_prompt(self, *, refocus: bool = True) -> None:
        was_pending = self._pending_order_id
        self.scan_prompt.hide()
        self.mark_input.clear()
        self._pending_row = None
        self.sticker_input.setEnabled(True)
        if was_pending is not None:
            self._pending_order_id = None
            self._patch_codes_cell(was_pending)
        if refocus and self._rows_ready:
            self.sticker_input.setFocus()

    def _on_mark_prompt_enter(self) -> None:
        if block_ru_layout_scan(self, self.mark_input):
            return
        row = self._pending_row
        if not row:
            self._hide_mark_prompt()
            return
        mark = self.mark_input.text()
        self.scan_prompt.hide()
        self.mark_input.clear()
        self.sticker_input.setEnabled(True)
        self._pending_row = None
        self._pending_order_id = None
        self._apply_mark_scan(row, mark)
        self.sticker_input.setFocus()

    def _disconnect_worker(self, worker: QThread, *signal_names: str) -> None:
        for name in signal_names:
            signal = getattr(worker, name, None)
            if signal is None:
                continue
            try:
                signal.disconnect()
            except Exception:
                pass
        if worker not in self._alive_workers:
            self._alive_workers.append(worker)

        def _cleanup(w=worker) -> None:
            if w in self._alive_workers:
                self._alive_workers.remove(w)
            w.deleteLater()

        if worker.isRunning():
            worker.finished.connect(_cleanup)
        else:
            _cleanup()

    def _stop_save_worker(self) -> None:
        worker = self._save_worker
        self._save_worker = None
        if worker is None:
            return
        worker.request_stop()
        self._disconnect_worker(worker, "progress", "finished_ok", "failed")

    def _update_save_button(self) -> None:
        if self._saving:
            return
        pending = pending_wb_save_jobs(self.rows, row_errors=self.row_errors)
        pending_ids = {int(j["order_id"]) for j in pending}
        failed_pending = self._save_failed_oids & pending_ids
        if failed_pending and pending_ids <= self._save_failed_oids:
            self._save_retry_mode = True
            self.save_btn.setText("Повторить ошибки ({})".format(len(failed_pending)))
        else:
            self._save_retry_mode = False
            self.save_btn.setText("Сохранить")
        self.save_btn.setEnabled(self._rows_ready)

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
        # Keep enabled for tooltips; readonly until rows are ready (web wait-rows).
        self.sticker_input.setReadOnly(not ready)
        self.sticker_input.setToolTip(
            "" if ready else "Дождитесь загрузки строк"
        )
        if not self._saving:
            self._update_save_button()

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
        return row_matches_modal_search(row, query)

    def _update_counter(self) -> None:
        filled = 0
        total = 0
        for r in self.rows:
            codes = self._row_codes(r)
            total += len(codes)
            filled += sum(1 for c in codes if str(c or "").strip())
        self.counter.setText("Просканировано {} из {} КИЗ".format(filled, total))

    def _restore_code_input(self, order_id: int, inp: GsAwareLineEdit) -> None:
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
            lines = ["<b>Подготовка маркировки…</b>"]
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
        self.save_btn.setEnabled(False)
        session = supply_session.get_session(self.source_id, self.supply_id)
        fast = bool(
            session
            and session.core_ready
            and session.kiz_rows is not None
        )
        if not fast:
            self._show_loading_row()
            self._set_load_step(1, "из локальной базы")
        if self._load_aborted(gen):
            return
        try:
            if fast:
                self.rows = [dict(r) for r in session.kiz_rows]
            else:
                self.rows = self.kiz.marking_rows(
                    self.source_id, self.supply_id, self.api_key
                )
        except Exception as exc:
            if self._load_aborted(gen):
                return
            self.rows = []
            self._set_info(str(exc))
            self._render_table(fast=False)
            if self._load_aborted(gen):
                return
            self._set_filters_ready(True)
            self.save_btn.setEnabled(True)
            return
        if self._load_aborted(gen):
            return
        self.row_errors = {}
        self._sticker_index = build_sticker_index([])
        self._code_inputs = {}
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
        lab.setWordWrap(True)
        lab.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lab.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
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
        inputs = []  # type: List[GsAwareLineEdit]
        can_remove = len(codes) > 1
        for idx, code in enumerate(codes):
            line = QHBoxLayout()
            line.setSpacing(8)
            inp = GsAwareLineEdit(str(code or ""))
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
            # Status under the input only — same width when column is resized.
            mid = QVBoxLayout()
            mid.setSpacing(4)
            mid.setContentsMargins(0, 0, 0, 0)
            mid.addWidget(inp)
            chip = self._code_status_label(row, code, err)
            if chip:
                mid.addWidget(chip)
            line.addLayout(mid, 1)
            line.addWidget(clear_btn, 0, Qt.AlignTop)
            lay.addLayout(line)
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
        lay.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        btn = QToolButton()
        btn.setObjectName("kizRowMenu")
        btn.setText("⋮")
        btn.setToolTip("Действия")
        btn.setPopupMode(QToolButton.InstantPopup)
        btn.setFixedSize(32, 32)
        menu = style_app_menu(QMenu(btn))
        menu.addAction(
            "Напечатать стикер", partial(self._print_sticker, oid)
        )
        btn.setMenu(menu)
        lay.addWidget(btn, 0, Qt.AlignTop)
        return wrap

    def _on_code_edited(self, order_id: int) -> None:
        inp = self.sender()
        if not isinstance(inp, GsAwareLineEdit):
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
                self._schedule_kiz_autosave(order_id, codes)
                row["kiz_wb_synced"] = False
                if codes:
                    row["kiz_status"] = "pending"
                self._sync_session_kiz_rows()
            except Exception:
                pass
            self._update_counter()
            self._update_save_button()
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
        self._patch_codes_cell(order_id)

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
            self._schedule_kiz_autosave(order_id, cleaned)
            self._sync_session_kiz_rows()
        except Exception:
            pass
        self._patch_codes_cell(order_id)
        self._update_counter()
        self._apply_filters()
        self._update_save_button()

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
        self.table.setRowHeight(table_idx, max(self.table.rowHeight(table_idx), 148))

    def _refresh_row(self, order_id: int) -> None:
        idx = self._row_index_by_oid.get(int(order_id))
        row = self._row_by_oid.get(int(order_id))
        if idx is None or row is None:
            return
        self._set_row_widgets(idx, row)
        self._resize_table_row(idx)

    def _patch_codes_cell(self, order_id: int) -> None:
        """Update only the КИЗ column (web cell patch) — avoid full row rebuild."""
        oid = int(order_id)
        idx = self._row_index_by_oid.get(oid)
        row = self._row_by_oid.get(oid)
        if idx is None or row is None:
            return
        active = self._pending_order_id == oid
        self.table.setCellWidget(
            idx, 2, self._wrap_cell(self._build_codes_widget(row), active=active)
        )
        # Keep active highlight on order/product columns without rebuilding them
        # when only codes changed; sticker/product highlight updated via _refresh_row.
        if active:
            self.table.selectRow(idx)
        self._resize_table_row(idx)

    def _refresh_changed_rows(self, order_ids: List[int]) -> None:
        for oid in order_ids:
            self._refresh_row(oid)

    def _render_table(self, *, fast: bool = False) -> None:
        self._sync_codes_from_inputs()
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
        self._show_mark_prompt(found)

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
            self._patch_codes_cell(oid)
            self._apply_filters()
            return
        dup = find_existing_mark(self.rows, code)
        if dup:
            self._set_info(
                "Этот КИЗ уже привязан к заказу {}".format(dup.get("order_id"))
            )
            self._patch_codes_cell(oid)
            return
        self.row_errors.pop(oid, None)
        codes = [c for c in self._row_codes(row) if str(c).strip()]
        if code in codes:
            self._set_info("Этот КИЗ уже добавлен")
            self._patch_codes_cell(oid)
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
        cleaned = [c for c in mutable if str(c).strip()]
        self._schedule_kiz_autosave(oid, cleaned)
        self._sync_session_kiz_rows()
        self.data_changed = True
        self._set_info("", ok=True)
        self._update_counter()
        self._patch_codes_cell(oid)
        self._apply_filters()
        self._update_save_button()

    def save_all(self) -> None:
        if self._saving:
            return
        self._sync_codes_from_inputs()
        self._flush_autosave_sync()
        only_ids = (
            sorted(self._save_failed_oids)
            if self._save_retry_mode and self._save_failed_oids
            else None
        )
        jobs = pending_wb_save_jobs(
            self.rows,
            row_errors=self.row_errors,
            only_order_ids=only_ids,
        )
        if not jobs:
            self._save_retry_mode = False
            self._save_failed_oids.clear()
            self._update_save_button()
            self._set_info("Нет изменений для сохранения")
            return

        self._saving = True
        self.save_btn.setEnabled(False)
        total = len(jobs)
        self.save_btn.setText("0 из {}".format(total))
        self._set_info("Сохранение в WB: 0 из {}".format(total), ok=True)

        worker = _KizSaveWorker(
            self.kiz.db,
            self.source_id,
            self.api_key,
            jobs,
        )
        self._save_worker = worker
        if worker not in self._alive_workers:
            self._alive_workers.append(worker)
        worker.progress.connect(self._on_save_progress)
        worker.finished_ok.connect(self._on_save_finished)
        worker.failed.connect(self._on_save_failed)
        worker.start()

    def _on_save_progress(
        self, done: int, total: int, order_id: int, ok: bool, error: str
    ) -> None:
        self.save_btn.setText("{} из {}".format(done, total))
        self._set_info(
            "Сохранение в WB: {} из {}".format(done, total),
            ok=True,
        )
        oid = int(order_id)
        row = self._row_by_oid.get(oid)
        if row is None:
            return
        if ok:
            self.row_errors.pop(oid, None)
            self._save_failed_oids.discard(oid)
            row["kiz_wb_synced"] = True
            row["kiz_status"] = "ok"
        else:
            msg = str(error or "Ошибка сохранения")
            self.row_errors[oid] = msg
            self._save_failed_oids.add(oid)
            row["kiz_wb_synced"] = False
            row["kiz_status"] = "error"
        self._refresh_row(oid)

    def _on_save_finished(self, result: object) -> None:
        payload = result if isinstance(result, dict) else {}
        worker = self._save_worker
        self._save_worker = None
        if worker is not None:
            self._disconnect_worker(worker, "progress", "finished_ok", "failed")

        self._saving = False
        saved = int(payload.get("saved") or 0)
        errors = list(payload.get("errors") or [])
        stopped = bool(payload.get("stopped"))
        if saved or errors:
            self.data_changed = True
        failed_oids = {int(e.get("order_id") or 0) for e in errors if e.get("order_id")}
        self._save_failed_oids = {oid for oid in failed_oids if oid}
        self._save_retry_mode = bool(self._save_failed_oids)
        self._sync_session_kiz_rows()
        self._apply_filters()
        self._update_save_button()

        if stopped:
            self._set_info(
                "Сохранение остановлено. Успешно: {}, ошибок: {}".format(
                    saved, len(self._save_failed_oids)
                )
            )
            return
        if self._save_failed_oids:
            lines = [
                "{}: {}".format(e.get("order_id"), e.get("error"))
                for e in errors[:3]
            ]
            self._set_info(
                "Сохранено: {}. Ошибки ({}):\n{}".format(
                    saved, len(self._save_failed_oids), "\n".join(lines)
                )
            )
            if len(errors) > 3:
                QMessageBox.warning(
                    self,
                    "КИЗ",
                    "\n".join(
                        "{}: {}".format(e.get("order_id"), e.get("error"))
                        for e in errors[:12]
                    ),
                )
            return
        if saved:
            self._set_info("Сохранено в WB: {} заказ(ов)".format(saved), ok=True)
        else:
            self._set_info("Нет изменений для сохранения")

    def _on_save_failed(self, message: str) -> None:
        worker = self._save_worker
        self._save_worker = None
        if worker is not None:
            self._disconnect_worker(worker, "progress", "finished_ok", "failed")
        self._saving = False
        self._update_save_button()
        self._set_info(str(message or "Ошибка сохранения"))

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
