# -*- coding: utf-8 -*-
"""Local FBS TTN draft (desktop stand-in for web «Сформировать ТН»)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from app.db import Database
from app.ozon import utc_now
from app.services.supply_drivers import SupplyDriversService


class TtnDraftService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.drivers = SupplyDriversService(db)

    def get(
        self, platform: str, source_id: int, supply_id: str
    ) -> Optional[Dict[str, Any]]:
        sid = str(supply_id or "").strip()
        plat = str(platform or "").strip().lower()
        if not sid or int(source_id or 0) <= 0:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM fbs_ttn_drafts
                WHERE platform = ? AND source_id = ? AND supply_id = ?
                """,
                (plat, int(source_id), sid),
            ).fetchone()
        return Database.row_to_dict(row) if row else None

    def build_prefill(
        self,
        platform: str,
        source_id: int,
        supply_id: str,
        *,
        supply_name: str = "",
        warehouse_name: str = "",
        cargo_places: str = "",
    ) -> Dict[str, Any]:
        sid = str(supply_id or "").strip()
        plat = str(platform or "").strip().lower()
        assigned = self.drivers.get_assignment(plat, source_id, sid)
        existing = self.get(plat, source_id, sid)
        today = datetime.now().strftime("%Y-%m-%d")
        draft = {
            "platform": plat,
            "source_id": int(source_id),
            "supply_id": sid,
            "title": "ТН · {}".format(supply_name or sid),
            "ttn_date": today,
            "driver_name": str(assigned.get("driver_name") or ""),
            "vehicle_number": str(assigned.get("vehicle_number") or ""),
            "vehicle_type": "",
            "load_address": "",
            "unload_address": str(warehouse_name or "").strip(),
            "cargo_places": str(cargo_places or "").strip(),
            "cargo_weight": "",
            "cargo_description": "Товары FBS",
            "notes": "",
            "warnings": [],
        }  # type: Dict[str, Any]
        if not assigned.get("has_driver"):
            draft["warnings"].append(
                "Водитель не назначен. Назначьте в карточке поставки или "
                "заполните ФИО вручную."
            )
        if existing:
            for key in (
                "title",
                "ttn_date",
                "driver_name",
                "vehicle_number",
                "vehicle_type",
                "load_address",
                "unload_address",
                "cargo_places",
                "cargo_weight",
                "cargo_description",
                "notes",
            ):
                val = existing.get(key)
                if val not in (None, ""):
                    draft[key] = val
            draft["id"] = existing.get("id")
        return {"ok": True, "draft": draft, "assignment": assigned}

    def save(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        plat = str(draft.get("platform") or "").strip().lower()
        sid = str(draft.get("supply_id") or "").strip()
        source_id = int(draft.get("source_id") or 0)
        if plat not in ("wb", "ozon") or not sid or source_id <= 0:
            raise ValueError("Некорректный черновик ТН")
        payload = {
            "title": str(draft.get("title") or "").strip(),
            "ttn_date": str(draft.get("ttn_date") or "").strip(),
            "driver_name": str(draft.get("driver_name") or "").strip(),
            "vehicle_number": str(draft.get("vehicle_number") or "").strip(),
            "vehicle_type": str(draft.get("vehicle_type") or "").strip(),
            "load_address": str(draft.get("load_address") or "").strip(),
            "unload_address": str(draft.get("unload_address") or "").strip(),
            "cargo_places": str(draft.get("cargo_places") or "").strip(),
            "cargo_weight": str(draft.get("cargo_weight") or "").strip(),
            "cargo_description": str(draft.get("cargo_description") or "").strip(),
            "notes": str(draft.get("notes") or "").strip(),
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO fbs_ttn_drafts(
                    platform, source_id, supply_id, title, ttn_date,
                    driver_name, vehicle_number, vehicle_type,
                    load_address, unload_address, cargo_places, cargo_weight,
                    cargo_description, notes, draft_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, source_id, supply_id) DO UPDATE SET
                    title = excluded.title,
                    ttn_date = excluded.ttn_date,
                    driver_name = excluded.driver_name,
                    vehicle_number = excluded.vehicle_number,
                    vehicle_type = excluded.vehicle_type,
                    load_address = excluded.load_address,
                    unload_address = excluded.unload_address,
                    cargo_places = excluded.cargo_places,
                    cargo_weight = excluded.cargo_weight,
                    cargo_description = excluded.cargo_description,
                    notes = excluded.notes,
                    draft_json = excluded.draft_json,
                    updated_at = excluded.updated_at
                """,
                (
                    plat,
                    source_id,
                    sid,
                    payload["title"],
                    payload["ttn_date"],
                    payload["driver_name"],
                    payload["vehicle_number"],
                    payload["vehicle_type"],
                    payload["load_address"],
                    payload["unload_address"],
                    payload["cargo_places"],
                    payload["cargo_weight"],
                    payload["cargo_description"],
                    payload["notes"],
                    json.dumps(payload, ensure_ascii=False),
                    utc_now(),
                ),
            )
            conn.commit()
        saved = self.get(plat, source_id, sid) or {}
        return {"ok": True, "draft": saved, "message": "Черновик ТН сохранён"}
