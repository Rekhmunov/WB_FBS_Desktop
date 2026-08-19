# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.db import Database
from app.wb import is_fbs_source_name, utc_now


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

    def create(self, name: str, api_key: str, is_enabled: bool = True) -> int:
        name = str(name or "").strip()
        api_key = str(api_key or "").strip()
        if not name:
            raise ValueError("Укажите название источника")
        if not is_fbs_source_name(name):
            raise ValueError('В названии источника должно быть «ФБС» или «FBS»')
        if not api_key:
            raise ValueError("Укажите API-ключ Wildberries (Marketplace)")
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO supply_sources(name, marketplace, api_key, is_enabled, created_at)
                VALUES (?, 'wb', ?, ?, ?)
                """,
                (name, api_key, 1 if is_enabled else 0, utc_now()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def update(
        self,
        source_id: int,
        name: str,
        api_key: str,
        is_enabled: bool = True,
    ) -> None:
        name = str(name or "").strip()
        api_key = str(api_key or "").strip()
        if not name:
            raise ValueError("Укажите название источника")
        if not is_fbs_source_name(name):
            raise ValueError('В названии источника должно быть «ФБС» или «FBS»')
        with self.db.connect() as conn:
            if api_key:
                conn.execute(
                    """
                    UPDATE supply_sources
                    SET name = ?, api_key = ?, is_enabled = ?
                    WHERE id = ?
                    """,
                    (name, api_key, 1 if is_enabled else 0, source_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE supply_sources
                    SET name = ?, is_enabled = ?
                    WHERE id = ?
                    """,
                    (name, 1 if is_enabled else 0, source_id),
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
