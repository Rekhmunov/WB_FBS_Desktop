# -*- coding: utf-8 -*-
"""Ozon Seller API FBS helpers (desktop, isolated from WB)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typing import Optional, Tuple

# UI tabs — portal-aligned (web FeedPilot Ozon FBS).
TAB_AWAITING_PACKAGING = "awaiting_packaging"
TAB_AWAITING_DELIVER = "awaiting_deliver"
TAB_DELIVERING = "delivering"
TAB_DELIVERED = "delivered"
TAB_CANCELLED = "cancelled"
TAB_ARBITRATION = "arbitration"

TAB_LABELS = {
    TAB_AWAITING_PACKAGING: "Ожидают сборки",
    TAB_AWAITING_DELIVER: "Ожидают отгрузки",
    TAB_DELIVERING: "Доставляются",
    TAB_DELIVERED: "Доставлены",
    TAB_CANCELLED: "Отменены",
    TAB_ARBITRATION: "Спорные",
}

# Backward-compatible aliases (older desktop used WB-style keys).
TAB_NEW = TAB_AWAITING_PACKAGING
TAB_ASSEMBLY = TAB_AWAITING_DELIVER
TAB_DELIVERY = TAB_DELIVERING
TAB_FINISHED = TAB_DELIVERED

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

_PACKAGING_STATUSES = {
    STATUS_AWAITING_REGISTRATION,
    STATUS_ACCEPTANCE_IN_PROGRESS,
    STATUS_AWAITING_APPROVE,
    STATUS_AWAITING_PACKAGING,
}
_AWAITING_DELIVER_STATUSES = {
    STATUS_AWAITING_DELIVER,
    STATUS_SENT_BY_SELLER,
    STATUS_DRIVER_PICKUP,
}
_ARBITRATION_STATUSES = {STATUS_ARBITRATION, STATUS_CLIENT_ARBITRATION}
_CANCELLED_STATUSES = {STATUS_CANCELLED, STATUS_NOT_ACCEPTED}

_LEGACY_TAB_MAP = {
    "new": TAB_AWAITING_PACKAGING,
    "assembly": TAB_AWAITING_DELIVER,
    "delivery": TAB_DELIVERING,
    "finished": TAB_DELIVERED,
}


def normalize_tab(tab: object) -> str:
    """Map legacy desktop tab keys to portal-aligned keys."""
    key = str(tab or "").strip().lower()
    return _LEGACY_TAB_MAP.get(key, key or TAB_AWAITING_PACKAGING)


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
    """Map Ozon posting.status to portal tabs (web parity).

    ``carriage_id`` is accepted for call-site compatibility but does not
    change the tab — web FeedPilot keys only off status.
    """
    del carriage_id  # unused; kept for API compatibility
    st = str(status or "").strip().lower()
    if st in _ARBITRATION_STATUSES:
        return TAB_ARBITRATION
    if st in _CANCELLED_STATUSES:
        return TAB_CANCELLED
    if st == STATUS_DELIVERED:
        return TAB_DELIVERED
    if st == STATUS_DELIVERING:
        return TAB_DELIVERING
    if st in _AWAITING_DELIVER_STATUSES:
        return TAB_AWAITING_DELIVER
    if st in _PACKAGING_STATUSES or not st:
        return TAB_AWAITING_PACKAGING
    # Unknown statuses: treat like packaging (web default).
    return TAB_AWAITING_PACKAGING


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


# Carriage statuses that no longer belong on «Ожидают отгрузки».
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
