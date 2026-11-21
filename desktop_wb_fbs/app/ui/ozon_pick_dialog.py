# -*- coding: utf-8 -*-
"""Ozon FBS «Товары без КИЗ» — archive / WB pick modal layout parity."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.ozon.client import OzonFbsClient
from app.services.ozon_mark_pick import OzonPickService
from app.ui.dialog_utils import (
    apply_fullscreen_on_show,
    block_ru_layout_scan,
    fullscreen_parent,
    init_fullscreen_dialog,
    install_live_ru_layout_guard,
    make_modal_search_box,
)
from app.ui.format_helpers import build_product_cell_widget
from app.ui.ozon_container_bind import OzonContainerBindController, row_container_ok
from app.ui.ozon_modal_chrome import (
    build_ozon_modal_posting_cell,
    build_ozon_modal_row_menu,
    exclusive_filled_empty,
    pick_other_container,
    print_one_posting_sticker,
    refresh_posting_status,
    row_is_cancelled,
    set_filter_count,
    supply_id_from_carriage,
)
from app.ui.table_col_widths import PersistentColumnWidths
from app.ui.text_copy import copy_text


class OzonPickDialog(QDialog):
    def __init__(
        self,
        pick: OzonPickService,
        source_id: int,
        carriage_id: str = "",
        parent=None,
        *,
        fullscreen: bool = True,
        posting_number: str = "",
        posting_numbers: Optional[List[str]] = None,
        title_hint: str = "",
        client: Optional[OzonFbsClient] = None,
        warehouse_id: int = 0,
    ) -> None:
        super(OzonPickDialog, self).__init__(fullscreen_parent(parent, fullscreen))
        self.pick = pick
        self.client = client
        self.source_id = source_id
        self.carriage_id = str(carriage_id or "")
        self.warehouse_id = int(warehouse_id or 0)
        self.posting_number = str(posting_number or "").strip()
        self.posting_numbers = [
            str(p).strip() for p in (posting_numbers or []) if str(p).strip()
        ]
        self.rows = []  # type: List[Dict[str, Any]]
        self._pending_pnum = None  # type: Optional[str]
        self.data_changed = False
        self._gm = None  # type: Optional[OzonContainerBindController]

        self.setObjectName("kizModal")
        scope = (
            self.posting_number
            or title_hint
            or ("отгрузка {}".format(carriage_id) if carriage_id else "поставка")
        )
        self.setWindowTitle("Товары без КИЗ · {}".format(scope))
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
        title = QLabel("Товары без КИЗ")
        title.setObjectName("kizTitleOzon")
        hl.addWidget(title)
        sub = QLabel(
            "Сканируйте этикетку отправления Ozon, затем штрихкод товара на упаковке."
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
        self._filter_base = {
            "filled": "Проверенные",
            "empty": "Непроверенные",
            "errors": "С ошибками",
            "cancelled": "Отмененные",
        }
        self.chk_filled = QCheckBox("Проверенные (0)")
        self.chk_empty = QCheckBox("Непроверенные (0)")
        self.chk_errors = QCheckBox("С ошибками (0)")
        self.chk_cancelled = QCheckBox("Отмененные (0)")
        for cb in (
            self.chk_filled,
            self.chk_empty,
            self.chk_errors,
            self.chk_cancelled,
        ):
            cb.setObjectName("kizFilterCheck")
        self.chk_filled.toggled.connect(self._on_filter_filled)
        self.chk_empty.toggled.connect(self._on_filter_empty)
        self.chk_errors.toggled.connect(self._render_table)
        self.chk_cancelled.toggled.connect(self._render_table)
        for cb in (
            self.chk_filled,
            self.chk_empty,
            self.chk_errors,
            self.chk_cancelled,
        ):
            filters.addWidget(cb)
        tb.addLayout(filters, 0)
        tb.addStretch(1)
        search_box, self.search_input = make_modal_search_box()
        self.search_input.setPlaceholderText("Поиск по отправлению или товару…")
        self.search_input.textChanged.connect(self._render_table)
        tb.addWidget(search_box, 0)
        self.counter = QLabel("Проверено 0 из 0")
        self.counter.setObjectName("kizScanCount")
        tb.addWidget(self.counter, 0)
        self._gm = OzonContainerBindController(
            db=self.pick.db,
            client=self.client,
            source_id=self.source_id,
            warehouse_id=self.warehouse_id,
            carriage_id=self.carriage_id
            or "supply:{}".format(title_hint or "0"),
            set_info=self._set_info,
            on_changed=self._on_gm_changed,
            parent=self,
        )
        self._gm.mount_toolbar_widgets(tb)
        if self._gm.chk_scan_gm is not None:
            self._gm.chk_scan_gm.toggled.connect(self._on_scan_gm_check)
        root.addWidget(toolbar)

        scan = QFrame()
        scan.setObjectName("kizScanBar")
        sl = QHBoxLayout(scan)
        sl.setContentsMargins(24, 12, 24, 12)
        sl.setSpacing(12)
        self.scan_input = QLineEdit()
        self.scan_input.setObjectName("kizScanInput")
        self.scan_input.setPlaceholderText(
            "Сканируйте этикетку Ozon (QR или ii…), затем ШК товара"
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
                db=self.pick.db,
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
        pt = QLabel("Просканируйте ШК товара")
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

        self.table = QTableWidget(0, 5)
        self.table.setObjectName("kizTable")
        self.table.setAlternatingRowColors(False)
        self.table.setHorizontalHeaderLabels(
            ["Отправление", "Товар", "Проверка ШК", "ШК грузоместа", ""]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self._col_widths = PersistentColumnWidths(
            self.pick.db,
            self.table,
            "ozon_pick_modal_table_cols_v4",
            [220, 420, 240, 160, 52],
            parent=self,
        )
        self._col_widths.apply()
        root.addWidget(self.table, 1)

        self._gm.prepare()
        self.load_rows()

    def showEvent(self, event) -> None:  # noqa: N802
        super(OzonPickDialog, self).showEvent(event)
        apply_fullscreen_on_show(self)
        self._scan_mode.activate()

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self._col_widths.persist()
        except Exception:
            pass
        try:
            self._scan_mode.deactivate()
        except Exception:
            pass
        super(OzonPickDialog, self).closeEvent(event)

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

    def _on_scan_gm_check(self, checked: bool) -> None:
        if checked:
            self.scan_input.setPlaceholderText("Сканируйте QR грузоместа")
        else:
            self.scan_input.setPlaceholderText(
                "Сканируйте этикетку Ozon (QR или ii…), затем ШК товара"
            )

    def _on_filter_filled(self, _checked: bool = False) -> None:
        exclusive_filled_empty(self.chk_filled, self.chk_empty, which="filled")
        self._render_table()

    def _on_filter_empty(self, _checked: bool = False) -> None:
        exclusive_filled_empty(self.chk_filled, self.chk_empty, which="empty")
        self._render_table()

    def _on_gm_changed(self) -> None:
        if self._gm is None:
            return
        self._gm.apply_pending_cell(self.rows)
        self._gm.apply_pending_clear(self.rows)
        self.data_changed = True
        self._render_table()

    def load_rows(self) -> None:
        if self.posting_number:
            self.rows = self.pick.single_row(self.source_id, self.posting_number)
        elif self.posting_numbers:
            self.rows = self.pick.rows(
                self.source_id,
                self.carriage_id,
                posting_numbers=self.posting_numbers,
            )
        else:
            self.rows = self.pick.rows(self.source_id, self.carriage_id)
        self._render_table()
        self._update_counter()

    def _build_status_widget(self, r: Dict[str, Any]) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(6)
        verified = bool(r.get("pick_verified"))
        barcode = str(r.get("pick_barcode") or "").strip()
        status = QLabel()
        status.setObjectName("kizCodeStatus")
        if verified and barcode:
            status.setText("✓ {}".format(barcode))
            status.setProperty("state", "ok")
        elif verified:
            status.setText("✓ Проверено")
            status.setProperty("state", "ok")
        else:
            status.setText("Не проверено")
            status.setProperty("state", "pending")
        status.style().unpolish(status)
        status.style().polish(status)
        status.setWordWrap(True)
        lay.addWidget(status)
        if verified and not row_is_cancelled(r):
            clear = QToolButton()
            clear.setObjectName("kizCodeRemove")
            clear.setText("×")
            clear.setToolTip("Сбросить проверку")
            pn = str(r.get("posting_number") or "")
            clear.clicked.connect(lambda _=False, p=pn: self._clear_pick(p))
            row = QHBoxLayout()
            row.addWidget(clear)
            row.addStretch(1)
            lay.addLayout(row)
        lay.addStretch(1)
        return wrap

    def _clear_pick(self, posting_number: str) -> None:
        for r in self.rows:
            if str(r.get("posting_number") or "") == posting_number:
                r["pick_verified"] = False
                r["pick_barcode"] = ""
                break
        try:
            self.pick.save(self.source_id, posting_number, False, "")
        except Exception:
            pass
        self.data_changed = True
        self._render_table()

    def _pick_ok(self, r: Dict[str, Any]) -> bool:
        return bool(r.get("pick_verified")) and bool(
            str(r.get("pick_barcode") or "").strip()
        )

    def _row_complete(self, r: Dict[str, Any]) -> bool:
        if not self._pick_ok(r):
            return False
        if self._gm is not None and self._gm.gm_required_in_filters(self.rows):
            return row_container_ok(r)
        return True

    def _row_error(self, r: Dict[str, Any]) -> bool:
        return bool(
            r.get("pick_error")
            or r.get("error")
            or r.get("container_sync_error")
        )

    def _row_matches_search(self, r: Dict[str, Any], q: str) -> bool:
        if not q:
            return True
        blob = " ".join(
            [
                str(r.get("posting_number") or ""),
                str(r.get("product_name") or ""),
                str(r.get("offer_id") or ""),
                str(r.get("sticker_scanit") or ""),
                str(r.get("pick_barcode") or ""),
                str(r.get("container_barcode") or ""),
            ]
        ).lower()
        return q in blob

    def _update_filter_counts(self) -> None:
        q = self.search_input.text().strip().lower()
        filled = empty = errors = cancelled = 0
        for r in self.rows:
            if not self._row_matches_search(r, q):
                continue
            if row_is_cancelled(r):
                cancelled += 1
                continue
            if self._row_complete(r):
                filled += 1
            else:
                empty += 1
            if self._row_error(r):
                errors += 1
        set_filter_count(self.chk_filled, self._filter_base["filled"], filled)
        set_filter_count(self.chk_empty, self._filter_base["empty"], empty)
        set_filter_count(self.chk_errors, self._filter_base["errors"], errors)
        set_filter_count(
            self.chk_cancelled, self._filter_base["cancelled"], cancelled
        )

    def _render_table(self) -> None:
        q = self.search_input.text().strip().lower()
        want_filled = self.chk_filled.isChecked()
        want_empty = self.chk_empty.isChecked()
        want_errors = self.chk_errors.isChecked()
        want_cancelled = self.chk_cancelled.isChecked()
        show_gm = self._gm is not None and self._gm.gm_ui_visible(self.rows)
        self.table.setColumnHidden(3, not show_gm)
        visible = []  # type: List[Dict[str, Any]]
        for r in self.rows:
            complete = self._row_complete(r)
            err = self._row_error(r)
            cancelled = row_is_cancelled(r)
            if want_filled and not complete:
                continue
            if want_empty and complete:
                continue
            if want_errors and not err:
                continue
            if want_cancelled and not cancelled:
                continue
            if not self._row_matches_search(r, q):
                continue
            visible.append(r)

        self._col_widths.begin_update()
        try:
            self.table.setRowCount(len(visible))
            for i, r in enumerate(visible):
                cancelled = row_is_cancelled(r)
                self.table.setCellWidget(
                    i,
                    0,
                    build_ozon_modal_posting_cell(
                        r,
                        on_copy=self._copy_posting,
                        on_refresh=self._refresh_posting_status,
                    ),
                )
                self.table.setCellWidget(
                    i, 1, build_product_cell_widget(r, photo_size=72)
                )
                self.table.setCellWidget(i, 2, self._build_status_widget(r))
                if show_gm and self._gm is not None:
                    self.table.setCellWidget(i, 3, self._gm.build_cell_widget(r))
                can_move = bool(
                    self._gm is not None
                    and self._gm.has_containers
                    and row_container_ok(r)
                )
                self.table.setCellWidget(
                    i,
                    4,
                    build_ozon_modal_row_menu(
                        r,
                        can_move_gm=can_move,
                        on_print=self._print_sticker,
                        on_move_gm=self._move_to_other_gm if can_move else None,
                        on_remove_cancelled=self._remove_cancelled
                        if cancelled
                        else None,
                    ),
                )
                self.table.setRowHeight(
                    i, max(148, self.table.sizeHintForRow(i))
                )
        finally:
            self._col_widths.end_update()
        self._update_counter()
        self._update_filter_counts()

    def _update_counter(self) -> None:
        active = [r for r in self.rows if not row_is_cancelled(r)]
        total = len(active)
        ok = sum(1 for r in active if self._pick_ok(r))
        self.counter.setText("Проверено {} из {}".format(ok, total))
        if self._gm is not None:
            self._gm.update_counter(self.rows)

    def _copy_posting(self, posting_number: str) -> None:
        copy_text(posting_number)
        self._set_info("Скопировано: {}".format(posting_number), ok=True)

    def _refresh_posting_status(self, posting_number: str) -> None:
        if self.client is None:
            QMessageBox.warning(self, "Статус заказа", "Нет клиента Ozon.")
            return
        try:
            refresh_posting_status(
                db=self.pick.db,
                client=self.client,
                source_id=self.source_id,
                posting_number=posting_number,
                rows=self.rows,
                parent=self,
            )
            self.data_changed = True
            self._render_table()
        except Exception as exc:
            QMessageBox.warning(self, "Статус заказа", str(exc))

    def _print_sticker(self, posting_number: str) -> None:
        if self.client is None:
            QMessageBox.warning(self, "Стикер", "Нет клиента Ozon.")
            return
        print_one_posting_sticker(
            db=self.pick.db,
            client=self.client,
            source_id=self.source_id,
            posting_number=posting_number,
            parent=self,
        )

    def _move_to_other_gm(self, posting_number: str) -> None:
        if self._gm is None:
            return
        row = next(
            (
                r
                for r in self.rows
                if str(r.get("posting_number") or "") == posting_number
            ),
            None,
        )
        if not row:
            return
        try:
            prev = int(row.get("container_id") or 0)
        except (TypeError, ValueError):
            prev = 0
        next_id = pick_other_container(
            self._gm.containers, exclude_id=prev, parent=self
        )
        if not next_id:
            return
        self._gm._pending_cell = (posting_number, str(next_id))
        self._gm.apply_pending_cell(self.rows)
        self.data_changed = True
        self._render_table()

    def _remove_cancelled(self, posting_number: str) -> None:
        sid = supply_id_from_carriage(self.carriage_id)
        if not sid:
            QMessageBox.information(
                self,
                "Поставка",
                "Удаление из поставки доступно только из модалки поставки.",
            )
            return
        if (
            QMessageBox.question(
                self,
                "Удалить из поставки",
                "Удалить отправление {} из поставки?\n\n"
                "Товар исчезнет из списков КИЗ / без КИЗ.".format(
                    posting_number
                ),
            )
            != QMessageBox.Yes
        ):
            return
        try:
            from app.services.ozon_supplies import OzonSuppliesService

            OzonSuppliesService(self.pick.db).remove_cancelled_posting_from_supply(
                self.source_id, sid, posting_number
            )
        except Exception as exc:
            QMessageBox.warning(self, "Поставка", str(exc))
            return
        self.rows = [
            r
            for r in self.rows
            if str(r.get("posting_number") or "") != posting_number
        ]
        self.data_changed = True
        self._render_table()
        self._set_info(
            "Отправление {} удалено из поставки".format(posting_number),
            ok=True,
        )

    def on_scan(self) -> None:
        if block_ru_layout_scan(self.scan_input):
            return
        raw = self.scan_input.text().strip()
        self.scan_input.clear()
        if not raw:
            return

        if self._gm is not None and self._gm.handle_scan(raw):
            self.scan_input.setPlaceholderText(
                "Сканируйте этикетку Ozon (QR или ii…), затем ШК товара"
            )
            return

        from app.ozon.sticker_fields import normalize_ozon_scanit

        if self._pending_pnum:
            pnum = self._pending_pnum
            row = next(
                (r for r in self.rows if r.get("posting_number") == pnum), None
            )
            if not row:
                self._pending_pnum = None
                self.scan_prompt.hide()
                return
            if row_is_cancelled(row):
                self._pending_pnum = None
                self.scan_prompt.hide()
                self._set_info(
                    "Отправление {} отменено — проверку ШК менять нельзя".format(
                        pnum
                    )
                )
                return
            barcodes = row.get("barcodes") if isinstance(row.get("barcodes"), list) else []
            codes = [str(b or "").strip() for b in barcodes if str(b or "").strip()]
            skus = row.get("skus") if isinstance(row.get("skus"), list) else []
            for s in skus:
                s2 = str(s or "").strip()
                if s2 and s2 not in codes:
                    codes.append(s2)
            offer = str(row.get("offer_id") or "").strip()
            if offer and offer not in codes:
                codes.append(offer)
            if codes and raw not in codes:
                self._set_info(
                    "ШК {} не совпадает с товаром отправления {}".format(raw, pnum)
                )
                return
            row["pick_verified"] = True
            row["pick_barcode"] = raw
            try:
                self.pick.save(self.source_id, pnum, True, raw)
            except Exception as exc:
                self._set_info(str(exc))
                return
            self._pending_pnum = None
            self.scan_prompt.hide()
            self.data_changed = True
            self._set_info("Проверено: {}".format(pnum), ok=True)
            if self._gm is not None:
                self._gm.maybe_bind_after_success(row)
            self._render_table()
            return

        if self._gm is not None and not self._gm.guard_order_scan(self.rows):
            return

        scanit = normalize_ozon_scanit(raw)
        for r in self.rows:
            pn = str(r.get("posting_number") or "")
            stickers = [
                str(r.get("sticker_barcode") or "").strip(),
                str(r.get("sticker_lower_barcode") or "").strip(),
                normalize_ozon_scanit(r.get("sticker_scanit")),
            ]
            if raw == pn or (scanit and scanit in stickers) or raw in stickers:
                if row_is_cancelled(r):
                    self._set_info(
                        "Отправление {} отменено — проверку ШК менять нельзя".format(
                            pn
                        )
                    )
                    return
                self._pending_pnum = pn
                self.prompt_meta.setText(
                    "Отправление {} — отсканируйте ШК товара на упаковке".format(pn)
                )
                self.scan_prompt.show()
                self._set_info("")
                self._render_table()
                return

        QMessageBox.information(
            self,
            "Товары без КИЗ",
            "Сначала отсканируйте этикетку отправления Ozon, затем ШК товара.",
        )
