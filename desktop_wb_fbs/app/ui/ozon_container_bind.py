# -*- coding: utf-8 -*-
"""Ozon FBS KIZ/pick → грузоместо bind (web archive ozon_fbs_container_bind.js)."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QToolButton,
    QWidget,
)

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.services.ozon_containers import (
    OzonContainersService,
    container_accepts_fill,
)


def _norm_scan(value: object) -> str:
    return str(value or "").strip()


def match_container(
    containers: List[Dict[str, Any]], raw_scan: str
) -> Optional[Dict[str, Any]]:
    """Match scanned GM QR / id to a listed container (archive matchContainer)."""
    raw = _norm_scan(raw_scan)
    if not raw:
        return None
    raw_l = raw.lower()
    for c in containers or []:
        cid = str(c.get("container_id") or "").strip()
        if cid and (raw == cid or raw_l == cid.lower()):
            return c
        for key in ("container_barcode", "barcode"):
            bc = str(c.get(key) or "").strip()
            if bc and (raw == bc or raw_l == bc.lower()):
                return c
        num = str(c.get("container_number") or "").strip()
        # Long numbers only — avoid accidental short matches.
        if num and len(num) >= 4 and (raw == num or raw_l == num.lower()):
            return c
    return None


def row_has_known_sticker(row: Dict[str, Any]) -> bool:
    if str(row.get("sticker_scanit") or "").strip():
        return True
    if str(row.get("sticker_barcode") or "").strip():
        return True
    if str(row.get("sticker_lower_barcode") or "").strip():
        return True
    return False


def row_container_ok(row: Dict[str, Any]) -> bool:
    return bool(str(row.get("container_barcode") or "").strip()) or (
        int(row.get("container_id") or 0) > 0
    )


class OzonContainerBindController:
    """Shared GM scan/bind state for mark + pick modals."""

    def __init__(
        self,
        *,
        db: Database,
        client: Optional[OzonFbsClient],
        source_id: int,
        warehouse_id: int = 0,
        carriage_id: str = "",
        set_info: Optional[Callable[..., None]] = None,
        on_changed: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        self.db = db
        self.client = client
        self.source_id = int(source_id or 0)
        self.warehouse_id = int(warehouse_id or 0)
        self.carriage_id = str(carriage_id or "").strip() or "supply:0"
        self._set_info = set_info
        self._on_changed = on_changed
        self._parent = parent
        self.svc = OzonContainersService(db)
        self.containers = []  # type: List[Dict[str, Any]]
        self.active_id = 0  # type: int
        self.active_barcode = ""
        self.scan_gm_mode = False
        self.has_containers = False

        # UI widgets (mounted by host dialog).
        self.chk_scan_gm = None  # type: Optional[QCheckBox]
        self.active_chip = None  # type: Optional[QWidget]
        self.active_label = None  # type: Optional[QLabel]
        self.gm_counter = None  # type: Optional[QLabel]
        self._cell_edits = {}  # type: Dict[str, QLineEdit]

    def info(self, text: str, *, ok: bool = False) -> None:
        if self._set_info:
            self._set_info(text, ok=ok)

    def notify(self) -> None:
        if self._on_changed:
            self._on_changed()

    def mount_toolbar_widgets(self, layout: QHBoxLayout) -> None:
        """Add «Просканировать ШК грузоместа» + active chip + counter."""
        self.chk_scan_gm = QCheckBox("Просканировать ШК грузоместа")
        self.chk_scan_gm.setObjectName("kizFilterCheck")
        self.chk_scan_gm.setVisible(False)
        self.chk_scan_gm.toggled.connect(self._on_scan_gm_toggled)
        layout.addWidget(self.chk_scan_gm)

        self.active_chip = QWidget()
        chip_lay = QHBoxLayout(self.active_chip)
        chip_lay.setContentsMargins(8, 0, 4, 0)
        chip_lay.setSpacing(6)
        prefix = QLabel("ГМ")
        prefix.setObjectName("hint")
        self.active_label = QLabel("")
        self.active_label.setObjectName("kizScanCount")
        clear_btn = QToolButton()
        clear_btn.setObjectName("kizScanClear")
        clear_btn.setText("✕")
        clear_btn.setToolTip("Прекратить сканирование в это грузоместо")
        clear_btn.clicked.connect(self.clear_active)
        chip_lay.addWidget(prefix)
        chip_lay.addWidget(self.active_label)
        chip_lay.addWidget(clear_btn)
        self.active_chip.hide()
        layout.addWidget(self.active_chip)

        self.gm_counter = QLabel("")
        self.gm_counter.setObjectName("hint")
        layout.addWidget(self.gm_counter)
        self._refresh_active_ui()

    def prepare(self) -> None:
        """Load warehouse containers for the open modal."""
        if self.client is None or self.warehouse_id <= 0:
            self.containers = []
            self.has_containers = False
            self._refresh_active_ui()
            return
        try:
            result = self.svc.list_containers(
                self.client,
                self.carriage_id,
                source_id=self.source_id or None,
                warehouse_id=self.warehouse_id,
                include_sc_accepted=False,
                only_active=True,
            )
            items = list(result.get("items") or [])
            self.containers = items
            self.has_containers = bool(items)
            if self.warehouse_id <= 0:
                self.warehouse_id = int(result.get("warehouse_id") or 0)
        except Exception as exc:
            self.containers = []
            self.has_containers = False
            self.info("Грузоместа: {}".format(exc))
        # Keep active if still fillable.
        if self.active_id > 0:
            cur = self._by_id(self.active_id)
            if not cur or not container_accepts_fill(cur):
                self.active_id = 0
                self.active_barcode = ""
        self._refresh_active_ui()

    def gm_ui_visible(self, rows: List[Dict[str, Any]]) -> bool:
        if self.has_containers:
            return True
        for r in rows or []:
            if row_container_ok(r):
                return True
        return False

    def gm_required_in_filters(self, rows: List[Dict[str, Any]]) -> bool:
        return self.gm_ui_visible(rows)

    def _by_id(self, container_id: int) -> Optional[Dict[str, Any]]:
        for c in self.containers:
            try:
                if int(c.get("container_id") or 0) == int(container_id):
                    return c
            except (TypeError, ValueError):
                continue
        return None

    def _on_scan_gm_toggled(self, checked: bool) -> None:
        self.scan_gm_mode = bool(checked)
        if checked:
            if self.active_id > 0:
                self.info(
                    "Активное грузоместо {}. Отсканируйте QR следующего или снимите галку.".format(
                        self.active_barcode or self.active_id
                    ),
                    ok=True,
                )
            else:
                self.info("Отсканируйте QR грузоместа", ok=True)

    def clear_active(self) -> None:
        if self.active_id <= 0 and not self.scan_gm_mode:
            return
        self.active_id = 0
        self.active_barcode = ""
        self.scan_gm_mode = False
        if self.chk_scan_gm is not None:
            self.chk_scan_gm.blockSignals(True)
            self.chk_scan_gm.setChecked(False)
            self.chk_scan_gm.blockSignals(False)
        self._refresh_active_ui()
        self.info(
            "Сканирование в грузоместо остановлено. "
            "Заказы больше не привязываются автоматически.",
            ok=True,
        )

    def _refresh_active_ui(self) -> None:
        show = self.has_containers or self.active_id > 0
        if self.chk_scan_gm is not None:
            self.chk_scan_gm.setVisible(show)
        if self.active_chip is not None:
            if self.active_id > 0:
                self.active_label.setText(self.active_barcode or str(self.active_id))
                self.active_chip.show()
            else:
                self.active_chip.hide()
        if self.gm_counter is not None:
            self.gm_counter.setVisible(show)

    def update_counter(self, rows: List[Dict[str, Any]]) -> None:
        if self.gm_counter is None:
            return
        if not self.gm_ui_visible(rows):
            self.gm_counter.setText("")
            self.gm_counter.hide()
            if self.chk_scan_gm is not None:
                self.chk_scan_gm.hide()
            return
        self.gm_counter.show()
        if self.chk_scan_gm is not None:
            self.chk_scan_gm.show()
        total = len(rows or [])
        synced = 0
        for r in rows or []:
            if not row_container_ok(r):
                continue
            if r.get("container_synced") and not str(
                r.get("container_sync_error") or ""
            ).strip():
                synced += 1
        self.gm_counter.setText(
            "Прикреплено к грузоместам {} из {}".format(synced, total)
        )
        self._refresh_active_ui()

    def handle_scan(self, raw: str) -> bool:
        """If GM scan mode: resolve container and set active. Return True if handled."""
        if not self.scan_gm_mode:
            return False
        found = match_container(self.containers, raw)
        if not found:
            self.info("Грузоместо «{}» не найдено".format(raw))
            return True
        if not container_accepts_fill(found):
            self.info(
                "Грузоместо {} уже подтверждено — в него нельзя сканировать заказы".format(
                    found.get("container_id")
                )
            )
            return True
        try:
            self.active_id = int(found.get("container_id") or 0)
        except (TypeError, ValueError):
            self.active_id = 0
        self.active_barcode = str(self.active_id or "")
        self.scan_gm_mode = False
        if self.chk_scan_gm is not None:
            self.chk_scan_gm.blockSignals(True)
            self.chk_scan_gm.setChecked(False)
            self.chk_scan_gm.blockSignals(False)
        self._refresh_active_ui()
        self.info(
            "Грузоместо {} выбрано. Сканируйте заказы для него.".format(
                self.active_barcode
            ),
            ok=True,
        )
        return True

    def guard_order_scan(self, rows: List[Dict[str, Any]]) -> bool:
        """True if order scan is allowed (archive guardOrderScanRequiresActiveGm)."""
        if not self.gm_ui_visible(rows):
            return True
        if self.active_id > 0:
            return True
        any_bound = any(row_container_ok(r) for r in rows or [])
        if not any_bound:
            return True
        self.info("Вы пытаетесь просканировать заказ без грузоместа.")
        QMessageBox.information(
            self._parent,
            "Нет грузоместа",
            "Сначала просканируйте ШК грузоместа "
            "(галочка «Просканировать ШК грузоместа»), "
            "затем сканируйте отправления.",
        )
        return False

    def maybe_bind_after_success(self, row: Dict[str, Any]) -> None:
        """After KIZ/pick success — bind to active GM (archive maybeBind)."""
        if self.active_id <= 0 or self.client is None:
            return
        if not self.has_containers and not self.containers:
            return
        if not row_has_known_sticker(row):
            return
        pn = str(row.get("posting_number") or "").strip()
        if not pn:
            return
        active = self._by_id(self.active_id)
        if active and not container_accepts_fill(active):
            self.clear_active()
            self.info(
                "Грузоместо {} уже подтверждено — выберите другое".format(
                    self.active_id
                )
            )
            return
        try:
            prev_id = int(row.get("container_id") or 0)
        except (TypeError, ValueError):
            prev_id = 0
        next_bc = self.active_barcode or str(self.active_id)
        if prev_id and prev_id != self.active_id:
            if (
                QMessageBox.question(
                    self._parent,
                    "Смена грузоместа",
                    "Отправление {} уже в ГМ {}.\nПереместить в {}?".format(
                        pn, prev_id, self.active_id
                    ),
                )
                != QMessageBox.Yes
            ):
                return
        elif prev_id == self.active_id and not str(
            row.get("container_sync_error") or ""
        ).strip():
            return
        row["container_id"] = self.active_id
        row["container_barcode"] = next_bc
        row["container_synced"] = False
        row["container_sync_error"] = ""
        try:
            self.svc.bind_posting(
                self.client,
                self.carriage_id,
                self.active_id,
                pn,
                source_id=self.source_id,
            )
            # Mark local as pending sync; reconcile later confirms.
            self.svc.set_local_bind(
                self.source_id,
                pn,
                container_id=self.active_id,
                synced=True,
                sync_error="",
            )
            row["container_synced"] = True
            self.info(
                "Отправление {} привязано к ГМ {}".format(pn, self.active_id),
                ok=True,
            )
        except Exception as exc:
            row["container_sync_error"] = str(exc)
            self.svc.set_local_bind(
                self.source_id,
                pn,
                container_id=self.active_id,
                synced=False,
                sync_error=str(exc),
            )
            self.info("Привязка к ГМ: {}".format(exc))
        self.notify()

    def build_cell_widget(self, row: Dict[str, Any]) -> QWidget:
        """Editable «ШК грузоместа» cell with clear button."""
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        pn = str(row.get("posting_number") or "").strip()
        edit = QLineEdit()
        edit.setObjectName("kizCodeInput")
        edit.setPlaceholderText("ШК грузоместа")
        current = str(row.get("container_barcode") or "").strip()
        if not current and row.get("container_id"):
            current = str(row.get("container_id"))
        edit.setText(current)
        if not current:
            edit.setPlaceholderText("—")
        edit.editingFinished.connect(
            lambda pn=pn, e=edit: self._commit_cell(pn, e.text())
        )
        clear = QToolButton()
        clear.setObjectName("kizCodeRemove")
        clear.setText("✕")
        clear.setToolTip("Отвязать от грузоместа")
        clear.clicked.connect(lambda _=False, pn=pn: self.clear_bind(pn))
        lay.addWidget(edit, 1)
        lay.addWidget(clear)
        if pn:
            self._cell_edits[pn] = edit
        err = str(row.get("container_sync_error") or "").strip()
        if err:
            edit.setToolTip(err)
            edit.setProperty("state", "error")
            edit.style().unpolish(edit)
            edit.style().polish(edit)
        return wrap

    def _find_row(
        self, rows: List[Dict[str, Any]], posting_number: str
    ) -> Optional[Dict[str, Any]]:
        pn = str(posting_number or "").strip()
        for r in rows:
            if str(r.get("posting_number") or "").strip() == pn:
                return r
        return None

    def _commit_cell(self, posting_number: str, raw: str) -> None:
        # Host must call commit with rows via set_rows_provider — use notify path.
        raw = _norm_scan(raw)
        self._pending_cell = (posting_number, raw)
        self.notify()

    def apply_pending_cell(self, rows: List[Dict[str, Any]]) -> None:
        pending = getattr(self, "_pending_cell", None)
        if not pending:
            return
        self._pending_cell = None
        pn, raw = pending
        row = self._find_row(rows, pn)
        if not row:
            return
        if not raw:
            self._do_unbind(row)
            return
        found = match_container(self.containers, raw)
        if not found:
            # Allow typing bare container id even if not in active list.
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                cid = 0
            if cid <= 0:
                self.info("Грузоместо «{}» не найдено".format(raw))
                return
            found = {"container_id": cid, "status": "new", "available_actions": ["fill"]}
        if not container_accepts_fill(found) and found.get("status") not in (
            "new",
            "approve_failed",
            "",
        ):
            self.info(
                "Грузоместо {} нельзя заполнять".format(found.get("container_id"))
            )
            return
        try:
            cid = int(found.get("container_id") or 0)
        except (TypeError, ValueError):
            return
        if cid <= 0 or self.client is None:
            return
        prev = int(row.get("container_id") or 0)
        if prev == cid and row.get("container_synced"):
            return
        row["container_id"] = cid
        row["container_barcode"] = str(cid)
        row["container_synced"] = False
        row["container_sync_error"] = ""
        try:
            self.svc.bind_posting(
                self.client,
                self.carriage_id,
                cid,
                pn,
                source_id=self.source_id,
            )
            self.svc.set_local_bind(
                self.source_id, pn, container_id=cid, synced=True, sync_error=""
            )
            row["container_synced"] = True
            self.info("Отправление {} → ГМ {}".format(pn, cid), ok=True)
        except Exception as exc:
            row["container_sync_error"] = str(exc)
            self.info(str(exc))

    def clear_bind(self, posting_number: str) -> None:
        self._pending_clear = posting_number
        self.notify()

    def apply_pending_clear(self, rows: List[Dict[str, Any]]) -> None:
        pn = getattr(self, "_pending_clear", None)
        if not pn:
            return
        self._pending_clear = None
        row = self._find_row(rows, pn)
        if row:
            self._do_unbind(row)

    def _do_unbind(self, row: Dict[str, Any]) -> None:
        pn = str(row.get("posting_number") or "").strip()
        try:
            prev = int(row.get("container_id") or 0)
        except (TypeError, ValueError):
            prev = 0
        if prev > 0 and self.client is not None:
            try:
                self.svc.unbind(
                    self.client,
                    prev,
                    pn,
                    carriage_id=self.carriage_id,
                    source_id=self.source_id,
                )
            except Exception as exc:
                row["container_sync_error"] = str(exc)
                self.info(str(exc))
                return
        self.svc.set_local_bind(
            self.source_id, pn, container_id=None, synced=False, sync_error=""
        )
        row["container_id"] = None
        row["container_barcode"] = ""
        row["container_synced"] = False
        row["container_sync_error"] = ""
        self.info("Отправление {} отвязано от ГМ".format(pn), ok=True)
