# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.db import Database
from app.wb import is_fbs_source_name, normalize_api_key, utc_now

_DEFAULT_LOOKBACK_DAYS = 2
_MIN_LOOKBACK_DAYS = 1
_MAX_LOOKBACK_DAYS = 30


def clamp_lookback_days(days: object) -> int:
    try:
        value = int(days)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = _DEFAULT_LOOKBACK_DAYS
    return max(_MIN_LOOKBACK_DAYS, min(value, _MAX_LOOKBACK_DAYS))


class SourceService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list_all(self) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM supply_sources ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return Database.rows_to_dicts(rows)

    def list_fbs_enabled(self) -> List[Dict[str, Any]]:
        return [
            s
            for s in self.list_all()
            if int(s.get("is_enabled") or 0)
            and str(s.get("marketplace") or "wb").lower() == "wb"
            and is_fbs_source_name(s.get("name"))
            and str(s.get("api_key") or "").strip()
        ]

    def get(self, source_id: int) -> Optional[Dict[str, Any]]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM supply_sources WHERE id = ?", (source_id,)
            ).fetchone()
        return Database.row_to_dict(row)

    def create(
        self,
        name: str,
        api_key: str,
        is_enabled: bool = True,
        lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    ) -> int:
        name = str(name or "").strip()
        api_key = normalize_api_key(api_key)
        lookback_days = clamp_lookback_days(lookback_days)
        if not name:
            raise ValueError("Укажите название источника")
        if not is_fbs_source_name(name):
            raise ValueError('В названии источника должно быть «ФБС» или «FBS»')
        if not api_key:
            raise ValueError("Укажите API-ключ Wildberries (Marketplace)")
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO supply_sources(
                    name, marketplace, api_key, is_enabled, lookback_days, created_at
                )
                VALUES (?, 'wb', ?, ?, ?, ?)
                """,
                (name, api_key, 1 if is_enabled else 0, lookback_days, utc_now()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def update(
        self,
        source_id: int,
        name: str,
        api_key: str,
        is_enabled: bool = True,
        lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    ) -> None:
        name = str(name or "").strip()
        api_key = normalize_api_key(api_key)
        lookback_days = clamp_lookback_days(lookback_days)
        if not name:
            raise ValueError("Укажите название источника")
        if not is_fbs_source_name(name):
            raise ValueError('В названии источника должно быть «ФБС» или «FBS»')
        with self.db.connect() as conn:
            if api_key:
                conn.execute(
                    """
                    UPDATE supply_sources
                    SET name = ?, api_key = ?, is_enabled = ?, lookback_days = ?
                    WHERE id = ?
                    """,
                    (name, api_key, 1 if is_enabled else 0, lookback_days, source_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE supply_sources
                    SET name = ?, is_enabled = ?, lookback_days = ?
                    WHERE id = ?
                    """,
                    (name, 1 if is_enabled else 0, lookback_days, source_id),
                )
            conn.commit()

    def delete(self, source_id: int) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM wb_fbs_orders WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM wb_fbs_supplies WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM supply_sources WHERE id = ?", (source_id,))
            conn.commit()

    def touch_synced(self, source_id: int) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE supply_sources SET last_synced_at = ? WHERE id = ?",
                (utc_now(), source_id),
            )
            conn.commit()
