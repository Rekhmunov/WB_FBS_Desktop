# -*- coding: utf-8 -*-
"""Ozon Seller API FBS helpers (desktop, isolated from WB)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typing import Optional, Tuple

TAB_NEW = "new"
TAB_ASSEMBLY = "assembly"
TAB_DELIVERY = "delivery"
TAB_FINISHED = "finished"
TAB_CANCELLED = "cancelled"

# Ozon posting.status (Seller API FBS).
STATUS_AWAITING_REGISTRATION = "awaiting_registration"
STATUS_ACCEPTANCE_IN_PROGRESS = "acceptance_in_progress"
STATUS_AWAITING_APPROVE = "awaiting_approve"
STATUS_AWAITING_PACKAGING = "awaiting_packaging"
STATUS_AWAITING_DELIVER = "awaiting_deliver"
STATUS_ARBITRATION = "arbitration"
STATUS_DELIVERING = "delivering"
STATUS_DELIVERED = "delivered"
STATUS_CANCELLED = "cancelled"
STATUS_NOT_ACCEPTED = "not_accepted"
STATUS_SENT_BY_SELLER = "sent_by_seller"
STATUS_CLIENT_ARBITRATION = "client_arbitration"
STATUS_DRIVER_PICKUP = "driver_pickup"

_NEW_STATUSES = {
    STATUS_AWAITING_REGISTRATION,
    STATUS_ACCEPTANCE_IN_PROGRESS,
    STATUS_AWAITING_APPROVE,
    STATUS_AWAITING_PACKAGING,
}
_ASSEMBLY_STATUSES = {
    STATUS_AWAITING_DELIVER,
    STATUS_ARBITRATION,
    STATUS_CLIENT_ARBITRATION,
    STATUS_SENT_BY_SELLER,
    STATUS_DRIVER_PICKUP,
}
_FINISHED_STATUSES = {STATUS_DELIVERED, STATUS_DELIVERING}
_CANCELLED_STATUSES = {STATUS_CANCELLED, STATUS_NOT_ACCEPTED}


def is_fbs_source_name(name: object) -> bool:
    text = str(name or "").casefold()
    return "фбс" in text or "fbs" in text


def normalize_client_id(value: object) -> str:
    return str(value or "").strip()


def normalize_api_key(value: object) -> str:
    return str(value or "").strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def lookback_window(days: int) -> Tuple[datetime, datetime]:
    """UTC window for Ozon list filters (API rejects very long periods)."""
    end = datetime.now(timezone.utc)
    span = max(1, min(int(days or 2), 30))
    start = end - timedelta(days=span)
    return start, end


def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def compute_tab(*, status: str, carriage_id: str = "") -> str:
    """Map Ozon posting.status (+ carriage) to desktop tabs (WB parity)."""
    st = str(status or "").strip().lower()
    cid = str(carriage_id or "").strip()
    if st in _CANCELLED_STATUSES:
        return TAB_CANCELLED
    if st in _FINISHED_STATUSES:
        return TAB_FINISHED
    if st in _ASSEMBLY_STATUSES or cid:
        return TAB_ASSEMBLY
    if st in _NEW_STATUSES or not st:
        return TAB_NEW
    return TAB_ASSEMBLY


def status_label(status: str) -> str:
    labels = {
        STATUS_AWAITING_REGISTRATION: "Ожидает регистрации",
        STATUS_ACCEPTANCE_IN_PROGRESS: "Приёмка",
        STATUS_AWAITING_APPROVE: "Ожидает подтверждения",
        STATUS_AWAITING_PACKAGING: "Ожидает упаковки",
        STATUS_AWAITING_DELIVER: "Готов к отгрузке",
        STATUS_ARBITRATION: "Арбитраж",
        STATUS_CLIENT_ARBITRATION: "Арбитраж (клиент)",
        STATUS_DELIVERING: "Доставляется",
        STATUS_DRIVER_PICKUP: "У водителя",
        STATUS_DELIVERED: "Доставлено",
        STATUS_CANCELLED: "Отменено",
        STATUS_NOT_ACCEPTED: "Не принято",
        STATUS_SENT_BY_SELLER: "Передано продавцом",
    }
    return labels.get(str(status or "").strip().lower(), str(status or "—"))


# Carriage statuses that no longer belong on «На сборке».
_CARRIAGE_DONE_STATUSES = frozenset(
    {
        "approved",
        "cancelled",
        "completed",
        "shipped",
        "closed",
    }
)


def carriage_is_done(status: str) -> bool:
    return str(status or "").strip().lower() in _CARRIAGE_DONE_STATUSES


def carriage_status_label(status: str) -> str:
    labels = {
        "new": "Сборка заказов",
        "formed": "Сформирована",
        "confirmed": "Подтверждена",
        "approved": "Отгрузите отгрузку",
        "cancelled": "Отменена",
    }
    return labels.get(str(status or "").strip().lower(), str(status or "—"))
