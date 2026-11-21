# -*- coding: utf-8 -*-
"""Ozon FBS «Товары с КИЗ» — archive / WB KIZ modal layout parity."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.ozon.client import OzonFbsClient
from app.services.ozon_mark_pick import OzonMarkService
from app.ui.dialog_utils import (
    apply_fullscreen_on_show,
    block_ru_layout_scan,
    fullscreen_parent,
    init_fullscreen_dialog,
    install_live_ru_layout_guard,
    make_modal_search_box,
)
from app.ui.format_helpers import (
    build_ozon_posting_cell_widget,
    build_product_cell_widget,
)
from app.ui.table_col_widths import PersistentColumnWidths


class _OzonMarkSaveWorker(QThread):
    done = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(
        self,
        mark: OzonMarkService,
        client: OzonFbsClient,
        source_id: int,
        jobs: List[Dict[str, Any]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_OzonMarkSaveWorker, self).__init__(parent)
        self.mark = mark
        self.client = client
        self.source_id = source_id
        self.jobs = list(jobs or [])

    def run(self) -> None:
        try:
            for job in self.jobs:
                self.mark.save_to_ozon(
                    self.client,
                    self.source_id,
                    str(job.get("posting_number") or ""),
                    list(job.get("marks") or []),
                )
            self.done.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class OzonMarkDialog(QDialog):
    def __init__(
        self,
        mark: OzonMarkService,
        client: OzonFbsClient,
        source_id: int,
        carriage_id: str = "",
        parent: Optional[QWidget] = None,
        *,
        fullscreen: bool = True,
        posting_number: str = "",
        posting_numbers: Optional[List[str]] = None,
        title_hint: str = "",
    ) -> None:
        super(OzonMarkDialog, self).__init__(fullscreen_parent(parent, fullscreen))
        self.mark = mark
        self.client = client
        self.source_id = source_id
        self.carriage_id = str(carriage_id or "")
        self.posting_number = str(posting_number or "").strip()
        self.posting_numbers = [
            str(p).strip() for p in (posting_numbers or []) if str(p).strip()
        ]
        self.rows = []  # type: List[Dict[str, Any]]
        self._pending_pnum = None  # type: Optional[str]
        self._inputs = {}  # type: Dict[str, QLineEdit]
        self.data_changed = False
        self._save_worker = None  # type: Optional[_OzonMarkSaveWorker]

        self.setObjectName("kizModal")
        scope = (
            self.posting_number
            or title_hint
            or (
                "отгрузка {}".format(carriage_id)
                if carriage_id
                else "поставка"
            )
        )
        self.setWindowTitle("Товары с КИЗ · {}".format(scope))
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
        hl = QVBoxLayout(header)
        hl.setContentsMargins(24, 20, 24, 16)
        hl.setSpacing(8)
        title_row = QHBoxLayout()
        title_row.setSpacing(16)
        title = QLabel("Товары с КИЗ")
        title.setObjectName("kizTitleOzon")
        title_row.addWidget(title, 0, Qt.AlignLeft | Qt.AlignTop)
        title_row.addStretch(1)
        refresh = QPushButton("↻ Статус Ozon")
        refresh.setObjectName("secondary")
        refresh.setFixedHeight(40)
        refresh.clicked.connect(self.refresh_live)
        title_row.addWidget(refresh, 0, Qt.AlignRight | Qt.AlignTop)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("bottomPrimary")
        self.save_btn.setFixedHeight(40)
        self.save_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.save_btn.clicked.connect(self.save_all)
        title_row.addWidget(self.save_btn, 0, Qt.AlignRight | Qt.AlignTop)
        hl.addLayout(title_row)
        sub = QLabel(
            "Сканируйте отправление Ozon, затем код маркировки «Честный знак»."
        )
        sub.setObjectName("kizSub")
        sub.setWordWrap(True)
        hl.addWidget(sub)
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
        for cb in (self.chk_filled, self.chk_empty, self.chk_errors):
            cb.setObjectName("kizFilterCheck")
            cb.toggled.connect(self._render_table)
            filters.addWidget(cb)
        tb.addLayout(filters, 0)
        tb.addStretch(1)
        search_box, self.search_input = make_modal_search_box()
        self.search_input.setPlaceholderText("Поиск по отправлению или товару…")
        self.search_input.textChanged.connect(self._render_table)
        tb.addWidget(search_box, 0)
        self.counter = QLabel("Просканировано 0 из 0 КИЗ")
        self.counter.setObjectName("kizScanCount")
        tb.addWidget(self.counter, 0)
        root.addWidget(toolbar)

        scan = QFrame()
        scan.setObjectName("kizScanBar")
        sl = QHBoxLayout(scan)
        sl.setContentsMargins(24, 12, 24, 12)
        sl.setSpacing(12)
        self.scan_input = QLineEdit()
        self.scan_input.setObjectName("kizScanInput")
        self.scan_input.setPlaceholderText(
            "Сканируйте этикетку Ozon (QR или ii…), затем КИЗ"
        )
        self.scan_input.returnPressed.connect(self.on_scan)
        install_live_ru_layout_guard(self.scan_input, self)
        from app.ui.scan_mode import mount_scan_mode_controls

        self._scan_mode_toggle, self._scan_mode_status, self._scan_mode = (
            mount_scan_mode_controls(
                sl,
                insert_at=0,
                parent=self,
                deliver=self.deliver_com_scan,
                db=self.mark.db,
            )
        )
        clear = QToolButton()
        clear.setObjectName("kizScanClear")
        clear.setText("✕")
        clear.setToolTip("Очистить")
        clear.clicked.connect(self.scan_input.clear)
        sl.addWidget(self.scan_input, 1)
        sl.addWidget(clear)
        root.addWidget(scan)

        self.scan_prompt = QFrame()
        self.scan_prompt.setObjectName("kizScanPrompt")
        self.scan_prompt.hide()
        pl = QVBoxLayout(self.scan_prompt)
        pl.setContentsMargins(24, 12, 24, 12)
        pl.setSpacing(4)
        pt = QLabel("Просканируйте маркировку")
        pt.setObjectName("kizPromptTitle")
        pl.addWidget(pt)
        self.prompt_meta = QLabel("")
        self.prompt_meta.setObjectName("kizPromptMeta")
        self.prompt_meta.setWordWrap(True)
        pl.addWidget(self.prompt_meta)
        root.addWidget(self.scan_prompt)

        self.info_banner = QFrame()
        self.info_banner.setObjectName("kizInfo")
        self.info_banner.hide()
        il = QHBoxLayout(self.info_banner)
        il.setContentsMargins(24, 8, 24, 8)
        self.info = QLabel("")
        self.info.setObjectName("kizInfoText")
        self.info.setWordWrap(True)
        il.addWidget(self.info)
        root.addWidget(self.info_banner)

        self.table = QTableWidget(0, 3)
        self.table.setObjectName("kizTable")
        self.table.setAlternatingRowColors(False)
        self.table.setHorizontalHeaderLabels(
            ["Отправление", "Товар", "Код маркировки"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self._col_widths = PersistentColumnWidths(
            self.mark.db,
            self.table,
            "ozon_mark_modal_table_cols_v2",
            [220, 420, 320],
            parent=self,
        )
        self._col_widths.apply()
        root.addWidget(self.table, 1)

        self.load_rows()

    def showEvent(self, event) -> None:  # noqa: N802
        super(OzonMarkDialog, self).showEvent(event)
        apply_fullscreen_on_show(self)
        self._scan_mode.activate()

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self._scan_mode.deactivate()
        except Exception:
            pass
        super(OzonMarkDialog, self).closeEvent(event)

    def deliver_com_scan(self, value: str) -> None:
        raw = str(value or "")
        if not raw.replace(" ", ""):
            return
        self.scan_input.setText(raw)
        self.on_scan()

    def _set_info(self, text: str, *, ok: bool = False) -> None:
        msg = str(text or "").strip()
        if not msg:
            self.info_banner.hide()
            self.info.setText("")
            return
        self.info.setText(msg)
        self.info_banner.setProperty("state", "ok" if ok else "")
        self.info_banner.style().unpolish(self.info_banner)
        self.info_banner.style().polish(self.info_banner)
        self.info_banner.show()

    def load_rows(self) -> None:
        if self.posting_number:
            row = self.mark.single_marking_row(
                self.source_id, self.posting_number, client=self.client
            )
            self.rows = [row] if row else []
        elif self.posting_numbers:
            self.rows = self.mark.marking_rows(
                self.source_id,
                self.carriage_id,
                client=self.client,
                posting_numbers=self.posting_numbers,
            )
        else:
            self.rows = self.mark.marking_rows(
                self.source_id, self.carriage_id, client=self.client
            )
        self._render_table()
        self._update_counter()

    def refresh_live(self) -> None:
        changed = False
        for r in self.rows:
            pnum = str(r.get("posting_number") or "")
            if not pnum:
                continue
            live = self.mark.refresh_mark_status(self.client, self.source_id, pnum)
            if live.get("marks"):
                r["marks"] = live.get("marks") or []
                r["marks_synced"] = bool(live.get("marks_synced"))
                changed = True
        if changed:
            self.data_changed = True
        self._render_table()
        self._update_counter()
        self._set_info("Статусы обновлены из Ozon", ok=True)

    def _row_filled(self, r: Dict[str, Any]) -> bool:
        return bool([m for m in (r.get("marks") or []) if str(m or "").strip()])

    def _row_error(self, r: Dict[str, Any]) -> bool:
        return bool(r.get("mark_error") or r.get("error"))

    def _render_table(self) -> None:
        q = self.search_input.text().strip().lower()
        want_filled = self.chk_filled.isChecked()
        want_empty = self.chk_empty.isChecked()
        want_errors = self.chk_errors.isChecked()
        visible = []  # type: List[Dict[str, Any]]
        for r in self.rows:
            filled = self._row_filled(r)
            err = self._row_error(r)
            if want_filled and not filled:
                continue
            if want_empty and filled:
                continue
            if want_errors and not err:
                continue
            if q:
                blob = " ".join(
                    [
                        str(r.get("posting_number") or ""),
                        str(r.get("product_name") or ""),
                        str(r.get("offer_id") or ""),
                        str(r.get("sticker_scanit") or ""),
                    ]
                ).lower()
                if q not in blob:
                    continue
            visible.append(r)

        self.table.setRowCount(len(visible))
        self._inputs = {}
        for i, r in enumerate(visible):
            pnum = str(r.get("posting_number") or "")
            self.table.setCellWidget(i, 0, build_ozon_posting_cell_widget(r))
            badge = None
            if self._row_filled(r):
                badge = QLabel("КИЗ")
                badge.setObjectName("sdKizBadge")
                badge.setProperty(
                    "kizState", "ok" if r.get("marks_synced") else "pending"
                )
                badge.style().unpolish(badge)
                badge.style().polish(badge)
            else:
                badge = QLabel("КИЗ")
                badge.setObjectName("sdKizBadge")
                badge.setProperty("kizState", "empty")
                badge.style().unpolish(badge)
                badge.style().polish(badge)
            self.table.setCellWidget(
                i,
                1,
                build_product_cell_widget(
                    r, photo_size=120, extra_widgets=[badge]
                ),
            )
            code_wrap = QWidget()
            code_lay = QVBoxLayout(code_wrap)
            code_lay.setContentsMargins(8, 10, 8, 10)
            code_lay.setSpacing(8)
            row_edit = QHBoxLayout()
            row_edit.setSpacing(8)
            edit = QLineEdit()
            edit.setObjectName("kizCodeInput")
            edit.setPlaceholderText("Код маркировки")
            edit.setText(
                (r.get("marks") or [""])[0] if r.get("marks") else ""
            )
            edit.textChanged.connect(
                lambda text, pn=pnum: self._on_edit(pn, text)
            )
            self._inputs[pnum] = edit
            row_edit.addWidget(edit, 1)
            clear_btn = QToolButton()
            clear_btn.setObjectName("kizCodeRemove")
            clear_btn.setText("×")
            clear_btn.setToolTip("Очистить")
            clear_btn.clicked.connect(
                lambda _=False, pn=pnum: self._clear_mark(pn)
            )
            row_edit.addWidget(clear_btn)
            code_lay.addLayout(row_edit)
            status = QLabel()
            status.setObjectName("kizCodeStatus")
            if self._row_error(r):
                status.setText(str(r.get("mark_error") or r.get("error") or "Ошибка"))
                status.setProperty("state", "error")
            elif r.get("marks_synced") and self._row_filled(r):
                status.setText("Сохранено в Ozon")
                status.setProperty("state", "ok")
            elif self._row_filled(r):
                status.setText("Заполнено · не сохранено")
                status.setProperty("state", "pending")
            else:
                status.setText("Не заполнено")
                status.setProperty("state", "pending")
            status.style().unpolish(status)
            status.style().polish(status)
            code_lay.addWidget(status)
            code_lay.addStretch(1)
            self.table.setCellWidget(i, 2, code_wrap)
            self.table.setRowHeight(i, max(148, self.table.sizeHintForRow(i)))
        self._update_counter()

    def _clear_mark(self, posting_number: str) -> None:
        for r in self.rows:
            if str(r.get("posting_number") or "") == posting_number:
                r["marks"] = []
                r["marks_synced"] = False
                break
        if posting_number in self._inputs:
            self._inputs[posting_number].setText("")
        self.data_changed = True
        self._render_table()

    def _on_edit(self, posting_number: str, text: str) -> None:
        for r in self.rows:
            if str(r.get("posting_number") or "") == posting_number:
                r["marks"] = [text.strip()] if text.strip() else []
                r["marks_synced"] = False
                break
        self.data_changed = True
        self._update_counter()

    def _update_counter(self) -> None:
        total = len(self.rows)
        filled = sum(1 for r in self.rows if self._row_filled(r))
        self.counter.setText(
            "Просканировано {} из {} КИЗ".format(filled, total)
        )

    def on_scan(self) -> None:
        if block_ru_layout_scan(self.scan_input):
            return
        raw = self.scan_input.text().strip()
        self.scan_input.clear()
        if not raw:
            return
        if self._pending_pnum:
            pnum = self._pending_pnum
            row = next(
                (r for r in self.rows if r.get("posting_number") == pnum), None
            )
            if not row:
                self._pending_pnum = None
                self.scan_prompt.hide()
                return
            ok, err = self.mark.validate_mark(
                raw, row.get("skus") or [], bool(row.get("skip_gtin_check"))
            )
            if not ok:
                self._set_info(err or "Неверный код маркировки")
                return
            row["marks"] = [raw]
            row["marks_synced"] = False
            if pnum in self._inputs:
                self._inputs[pnum].setText(raw)
            self._pending_pnum = None
            self.scan_prompt.hide()
            self.data_changed = True
            self._set_info("КИЗ принят для {}".format(pnum), ok=True)
            self._render_table()
            return

        from app.ozon.sticker_fields import normalize_ozon_scanit

        scanit = normalize_ozon_scanit(raw)
        for r in self.rows:
            pn = str(r.get("posting_number") or "")
            row_scanit = normalize_ozon_scanit(r.get("sticker_scanit"))
            stickers = [
                str(r.get("sticker_barcode") or "").strip(),
                str(r.get("sticker_lower_barcode") or "").strip(),
                row_scanit,
            ]
            if raw == pn or (scanit and scanit in stickers) or raw in stickers:
                self._pending_pnum = pn
                self.prompt_meta.setText(
                    "Отправление {} — отсканируйте код маркировки с того же изделия".format(
                        pn
                    )
                )
                self.scan_prompt.show()
                self._set_info("")
                self._render_table()
                return

        if len(raw) > 20:
            last_err = ""
            for r in self.rows:
                if self._row_filled(r):
                    continue
                ok, err = self.mark.validate_mark(
                    raw, r.get("skus") or [], bool(r.get("skip_gtin_check"))
                )
                if ok:
                    r["marks"] = [raw]
                    r["marks_synced"] = False
                    self.data_changed = True
                    self._set_info(
                        "КИЗ привязан к {}".format(r.get("posting_number")),
                        ok=True,
                    )
                    self._render_table()
                    return
                last_err = str(err or "")
            self._set_info(last_err or "Не найдено подходящее отправление")
            return
        self._set_info(
            "Сначала отсканируйте отправление Ozon, затем код маркировки."
        )

    def save_all(self) -> None:
        jobs = [
            {"posting_number": r.get("posting_number"), "marks": r.get("marks") or []}
            for r in self.rows
            if self._row_filled(r)
        ]
        if not jobs:
            QMessageBox.information(self, "Товары с КИЗ", "Нет кодов для сохранения.")
            return
        if self._save_worker and self._save_worker.isRunning():
            return
        self.save_btn.setEnabled(False)
        worker = _OzonMarkSaveWorker(
            self.mark, self.client, self.source_id, jobs, self
        )
        self._save_worker = worker
        worker.done.connect(self._on_saved)
        worker.failed.connect(self._on_save_failed)
        worker.start()

    def _on_save_failed(self, message: str) -> None:
        self.save_btn.setEnabled(True)
        self._set_info(str(message or "Ошибка сохранения"))

    def _on_saved(self) -> None:
        self.save_btn.setEnabled(True)
        self.data_changed = True
        self.load_rows()
        self._set_info("Коды отправлены в Ozon", ok=True)
