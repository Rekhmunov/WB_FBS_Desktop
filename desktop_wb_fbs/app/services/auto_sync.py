# -*- coding: utf-8 -*-
"""WB / Ozon FBS auto-sync settings (web gear parity, desktop SQLite)."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

_MSK = timezone(timedelta(hours=3))
_INTERVALS = (10, 30, 60, 120, 180, 360, 720, 1440)

_KEYS = {
    "wb": {
        "enabled": "wb_fbs_auto_sync_enabled",
        "interval": "wb_fbs_auto_sync_interval_minutes",
        "from": "wb_fbs_auto_sync_active_from",
        "to": "wb_fbs_auto_sync_active_to",
        "last": "wb_fbs_last_auto_synced_at",
    },
    "ozon": {
        "enabled": "ozon_fbs_auto_sync_enabled",
        "interval": "ozon_fbs_auto_sync_interval_minutes",
        "from": "ozon_fbs_auto_sync_active_from",
        "to": "ozon_fbs_auto_sync_active_to",
        "last": "ozon_fbs_last_auto_synced_at",
    },
}


def allowed_intervals_minutes() -> List[int]:
    return list(_INTERVALS)


def normalize_hhmm(value: object, default: str = "12:00") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    parts = text.split(":")
    try:
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError, IndexError):
        return default
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return default
    return "{:02d}:{:02d}".format(hh, mm)


def normalize_interval_minutes(value: object, default: int = 60) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = int(default)
    if minutes in _INTERVALS:
        return minutes
    # Legacy hours (1..24) → minutes
    if minutes in (1, 2, 3, 6, 12, 24):
        return minutes * 60
    # Nearest allowed
    return min(_INTERVALS, key=lambda x: abs(x - minutes))


def _parse_hhmm(value: str, default: str = "12:00") -> time:
    norm = normalize_hhmm(value, default=default)
    hh, mm = norm.split(":")
    return time(hour=int(hh), minute=int(mm))


def msk_now() -> datetime:
    return datetime.now(_MSK)


def msk_time_in_active_window(
    *,
    active_from: str,
    active_to: str,
    now_msk: Optional[datetime] = None,
) -> bool:
    """Web overnight window: from 12:00 to 06:00 means evening→morning."""
    start = _parse_hhmm(active_from, "12:00")
    end = _parse_hhmm(active_to, "06:00")
    now = (now_msk or msk_now()).astimezone(_MSK)
    current = now.time().replace(second=0, microsecond=0)
    if start == end:
        return True
    if start < end:
        return start <= current <= end
    return current >= start or current <= end


def auto_sync_is_due(
    *,
    last_synced_at: Optional[str],
    interval_minutes: int,
) -> bool:
    if not last_synced_at:
        return True
    try:
        last_dt = datetime.fromisoformat(
            str(last_synced_at).replace("Z", "+00:00")
        )
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    minutes = max(1, int(interval_minutes or 1))
    return (datetime.now(timezone.utc) - last_dt).total_seconds() >= float(
        minutes
    ) * 60.0


class AutoSyncSettings:
    def __init__(self, db: Any) -> None:
        self.db = db

    def get(self, channel: str) -> Dict[str, Any]:
        keys = _KEYS[channel]
        enabled = self.db.get_setting(keys["enabled"], "0") in ("1", "true", "True")
        interval = normalize_interval_minutes(
            self.db.get_setting(keys["interval"], "60"), 60
        )
        active_from = normalize_hhmm(
            self.db.get_setting(keys["from"], "12:00"), "12:00"
        )
        active_to = normalize_hhmm(
            self.db.get_setting(keys["to"], "06:00"), "06:00"
        )
        last = self.db.get_setting(keys["last"], "") or None
        return {
            "channel": channel,
            "enabled": enabled,
            "interval_minutes": interval,
            "allowed_intervals_minutes": allowed_intervals_minutes(),
            "active_from": active_from,
            "active_to": active_to,
            "last_auto_synced_at": last,
        }

    def save(
        self,
        channel: str,
        *,
        enabled: bool,
        interval_minutes: int,
        active_from: str,
        active_to: str,
    ) -> Dict[str, Any]:
        keys = _KEYS[channel]
        interval = normalize_interval_minutes(interval_minutes, 60)
        frm = normalize_hhmm(active_from, "12:00")
        to = normalize_hhmm(active_to, "06:00")
        self.db.set_setting(keys["enabled"], "1" if enabled else "0")
        self.db.set_setting(keys["interval"], str(interval))
        self.db.set_setting(keys["from"], frm)
        self.db.set_setting(keys["to"], to)
        return self.get(channel)

    def mark_synced(self, channel: str) -> None:
        keys = _KEYS[channel]
        self.db.set_setting(
            keys["last"], datetime.now(timezone.utc).isoformat()
        )

    def should_run_now(
        self, channel: str, *, now_msk: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        cfg = self.get(channel)
        if not cfg["enabled"]:
            return False, "disabled"
        if not msk_time_in_active_window(
            active_from=cfg["active_from"],
            active_to=cfg["active_to"],
            now_msk=now_msk,
        ):
            return False, "outside_window"
        if not auto_sync_is_due(
            last_synced_at=cfg["last_auto_synced_at"],
            interval_minutes=int(cfg["interval_minutes"]),
        ):
            return False, "not_due"
        return True, "due"
