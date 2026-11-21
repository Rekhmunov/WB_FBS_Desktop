# -*- coding: utf-8 -*-
"""Shared Ozon FBS KIZ/pick modal chrome (web archive ozon_fbs.js parity)."""
from __future__ import annotations

from functools import partial
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import QUrl, Qt
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.ui.dialog_utils import style_app_menu
from app.ui.format_helpers import (
    build_ozon_scanit_label,
    format_date_short,
    make_badge,
)
from app.ui.text_copy import copy_text


def row_is_cancelled(row: Dict[str, Any]) -> bool:
    if not row:
        return False
    if row.get("cancelled"):
        return True
    if str(row.get("cancel_reason_label") or row.get("cancel_reason") or "").strip():
        return True
    tab = str(row.get("tab") or "").strip().lower()
    status = str(row.get("status") or "").strip().lower()
    if tab == "cancelled":
        return True
    if status in ("cancelled", "cancelled_from_split_pending", "not_accepted"):
        return True
    return False


def set_filter_count(cb: QCheckBox, base: str, count: int) -> None:
    n = max(0, int(count or 0))
    cb.setText("{} ({})".format(base, n))
    cb.setToolTip("По текущему поиску: {}".format(n))


def exclusive_filled_empty(
    filled: QCheckBox, empty: QCheckBox, *, which: str
) -> None:
    """Archive: filled ⟷ empty are mutually exclusive."""
    if which == "filled" and filled.isChecked() and empty.isChecked():
        empty.blockSignals(True)
        empty.setChecked(False)
        empty.blockSignals(False)
    elif which == "empty" and empty.isChecked() and filled.isChecked():
        filled.blockSignals(True)
        filled.setChecked(False)
        filled.blockSignals(False)


def build_ozon_modal_posting_cell(
    row: Dict[str, Any],
    *,
    on_copy: Optional[Callable[[str], None]] = None,
    on_refresh: Optional[Callable[[str], None]] = None,
) -> QWidget:
    """Posting # + copy/refresh icons + ii… + date (archive ``_ozonFbsModalPostingColHtml``)."""
    from PyQt5.QtWidgets import QSizePolicy

    wrap = QWidget()
    wrap.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(8, 10, 8, 10)
    lay.setSpacing(6)
    lay.setAlignment(Qt.AlignTop)

    cancel_label = str(
        row.get("cancel_reason_label") or row.get("cancel_reason") or ""
    ).strip()
    if cancel_label or row_is_cancelled(row):
        badges = QHBoxLayout()
        badges.setContentsMargins(0, 0, 0, 0)
        badges.setSpacing(4)
        badges.addWidget(make_badge(cancel_label or "отменено", "danger"))
        badges.addStretch(1)
        lay.addLayout(badges)

    pn = str(row.get("posting_number") or "").strip() or "—"
    id_row = QHBoxLayout()
    id_row.setContentsMargins(0, 0, 0, 0)
    id_row.setSpacing(6)
    oid = QLabel(pn)
    oid.setObjectName("sdOrderId")
    oid.setWordWrap(False)
    oid.setTextInteractionFlags(Qt.TextSelectableByMouse)
    oid.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    id_row.addWidget(oid, 0, Qt.AlignVCenter)

    if pn and pn != "—":
        actions = QWidget()
        actions.setObjectName("ozonPostingActions")
        al = QHBoxLayout(actions)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(2)

        copy_btn = QToolButton()
        copy_btn.setObjectName("ozonPostingIconBtn")
        copy_btn.setText("⧉")
        copy_btn.setToolTip("Скопировать стикер")
        copy_btn.setFixedSize(28, 28)
        copy_btn.setCursor(Qt.PointingHandCursor)
        if on_copy:
            copy_btn.clicked.connect(lambda _=False, p=pn: on_copy(p))
        else:
            copy_btn.clicked.connect(lambda _=False, p=pn: copy_text(p))
        al.addWidget(copy_btn)

        refresh_btn = QToolButton()
        refresh_btn.setObjectName("ozonPostingIconBtn")
        refresh_btn.setText("↻")
        refresh_btn.setToolTip("Проверить статус на Ozon")
        refresh_btn.setFixedSize(28, 28)
        refresh_btn.setCursor(Qt.PointingHandCursor)
        if on_refresh:
            refresh_btn.clicked.connect(lambda _=False, p=pn: on_refresh(p))
        al.addWidget(refresh_btn)
        id_row.addWidget(actions, 0, Qt.AlignVCenter)

    id_row.addStretch(1)
    lay.addLayout(id_row)

    scanit_lab = build_ozon_scanit_label(row.get("sticker_scanit"))
    if scanit_lab is not None:
        lay.addWidget(scanit_lab)

    created = str(row.get("created_date") or "").strip()
    if not created:
        raw = str(
            row.get("in_process_at") or row.get("created_at_wb") or ""
        ).strip()
        created = format_date_short(raw) if raw else ""
    meta = QLabel("от {}".format(created or "—"))
    meta.setObjectName("sdOrderMeta")
    meta.setWordWrap(False)
    lay.addWidget(meta)
    return wrap


def build_ozon_modal_row_menu(
    row: Dict[str, Any],
    *,
    can_move_gm: bool,
    on_print: Callable[[str], None],
    on_move_gm: Optional[Callable[[str], None]] = None,
    on_remove_cancelled: Optional[Callable[[str], None]] = None,
) -> QWidget:
    """⋮ menu: move GM / print sticker / remove cancelled (archive)."""
    pn = str(row.get("posting_number") or "").strip()
    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(4, 8, 8, 8)
    lay.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
    if not pn:
        return wrap
    btn = QToolButton()
    btn.setObjectName("kizRowMenu")
    btn.setText("⋮")
    btn.setToolTip("Действия")
    btn.setPopupMode(QToolButton.InstantPopup)
    btn.setFixedSize(40, 40)
    menu = style_app_menu(QMenu(btn))
    if can_move_gm and on_move_gm is not None:
        menu.addAction(
            "Переместить в другое ГМ", partial(on_move_gm, pn)
        )
    menu.addAction("Напечатать стикер", partial(on_print, pn))
    if row_is_cancelled(row) and on_remove_cancelled is not None:
        menu.addAction(
            "Удалить товар из поставки", partial(on_remove_cancelled, pn)
        )
    btn.setMenu(menu)
    lay.addWidget(btn, 0, Qt.AlignTop)
    return wrap


def supply_id_from_carriage(carriage_id: str) -> str:
    raw = str(carriage_id or "").strip()
    if raw.startswith("supply:"):
        return raw.split(":", 1)[1].strip()
    return ""


def refresh_posting_status(
    *,
    db: Database,
    client: OzonFbsClient,
    source_id: int,
    posting_number: str,
    rows: List[Dict[str, Any]],
    parent: Optional[QWidget] = None,
) -> Dict[str, Any]:
    """Fetch live Ozon posting; update local DB + in-memory modal rows."""
    pn = str(posting_number or "").strip()
    if not pn:
        return {}
    fresh = client.get_posting(pn)
    if not fresh:
        raise RuntimeError("Отправление не найдено на Ozon")
    from app.ozon.sync import upsert_posting
    from app.ozon import status_label as ozon_status_label

    upsert_posting(db, source_id, fresh)
    status = str(fresh.get("status") or "").strip().lower()
    tab = str(fresh.get("tab") or "").strip().lower()
    cancel = ""
    cancellation = fresh.get("cancellation")
    if isinstance(cancellation, dict):
        cancel = str(
            cancellation.get("cancel_reason")
            or cancellation.get("reason")
            or ""
        ).strip()
    if not cancel:
        cancel = str(fresh.get("cancel_reason") or "").strip()
    cancelled = (
        tab == "cancelled"
        or status in ("cancelled", "cancelled_from_split_pending", "not_accepted")
        or bool(cancel)
    )
    label = ozon_status_label(status) if status else (tab or "—")
    for r in rows:
        if str(r.get("posting_number") or "").strip() != pn:
            continue
        r["status"] = status or r.get("status")
        r["tab"] = tab or r.get("tab")
        if cancelled:
            r["cancelled"] = True
            r["cancel_reason"] = cancel or "Отменено"
            r["cancel_reason_label"] = cancel or "Отменено"
            r["container_sync_error"] = ""
        else:
            r["cancelled"] = False
            r["cancel_reason"] = ""
            r["cancel_reason_label"] = ""
    if cancelled:
        QMessageBox.information(
            parent,
            "Заказ отменён",
            "Отправление {} — отменено{}".format(
                pn, " ({})".format(cancel) if cancel else ""
            ),
        )
    else:
        QMessageBox.information(
            parent,
            "Статус заказа",
            "Отправление {}\nТекущий статус: {}".format(pn, label),
        )
    return {"cancelled": cancelled, "status": status, "status_label": label}


def print_one_posting_sticker(
    *,
    db: Database,
    client: OzonFbsClient,
    source_id: int,
    posting_number: str,
    parent: Optional[QWidget] = None,
) -> None:
    pn = str(posting_number or "").strip()
    if not pn:
        return
    from app.services.ozon_labels import OzonLabelService

    try:
        path = OzonLabelService(db).fetch_labels(client, source_id, [pn])
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    except Exception as exc:
        QMessageBox.warning(parent, "Стикер", str(exc))


def pick_other_container(
    containers: List[Dict[str, Any]],
    *,
    exclude_id: int = 0,
    parent: Optional[QWidget] = None,
) -> Optional[int]:
    """Simple GM picker for «Переместить в другое ГМ»."""
    options = []  # type: List[str]
    ids = []  # type: List[int]
    for c in containers or []:
        try:
            cid = int(c.get("container_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid <= 0 or cid == exclude_id:
            continue
        from app.services.ozon_containers import container_accepts_fill

        if not container_accepts_fill(c):
            continue
        options.append(str(cid))
        ids.append(cid)
    if not options:
        QMessageBox.information(
            parent,
            "Грузоместа",
            "Нет других доступных грузомест для перемещения.",
        )
        return None
    choice, ok = QInputDialog.getItem(
        parent,
        "Переместить в другое ГМ",
        "Грузоместо:",
        options,
        0,
        False,
    )
    if not ok:
        return None
    try:
        return int(choice)
    except (TypeError, ValueError):
        return None
