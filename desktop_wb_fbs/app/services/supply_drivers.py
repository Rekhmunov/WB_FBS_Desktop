# -*- coding: utf-8 -*-
"""Shared supply drivers catalog + WB/Ozon supply assignment."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.db import Database
from app.ozon import utc_now as ozon_utc_now
from app.wb import utc_now as wb_utc_now


def _now() -> str:
    try:
        return ozon_utc_now()
    except Exception:
        return wb_utc_now()


def _norm_plate(value: object) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def empty_assignment() -> Dict[str, Any]:
    return {
        "driver_id": 0,
        "driver_name": "",
        "vehicle_number": "",
        "has_driver": False,
    }


class SupplyDriversService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list_drivers(self) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM supply_drivers
                ORDER BY full_name COLLATE NOCASE ASC
                """
            ).fetchall()
        out = []  # type: List[Dict[str, Any]]
        for row in rows:
            d = Database.row_to_dict(row) or {}
            vehicles = []
            try:
                raw = json.loads(str(d.get("vehicles_json") or "[]"))
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict):
                            num = str(item.get("number") or "").strip()
                            if num:
                                vehicles.append(
                                    {
                                        "number": num,
                                        "type": str(item.get("type") or "").strip(),
                                    }
                                )
                        elif str(item or "").strip():
                            vehicles.append({"number": str(item).strip(), "type": ""})
            except Exception:
                vehicles = []
            d["vehicles"] = vehicles
            d["id"] = int(d.get("id") or 0)
            out.append(d)
        return out

    def options(self) -> List[Dict[str, Any]]:
        out = []  # type: List[Dict[str, Any]]
        for row in self.list_drivers():
            did = int(row.get("id") or 0)
            name = str(row.get("full_name") or "").strip()
            if did <= 0 or not name:
                continue
            out.append(
                {
                    "id": did,
                    "full_name": name,
                    "phone": str(row.get("phone") or "").strip(),
                    "documents": str(row.get("documents") or "").strip(),
                    "carrier": str(row.get("carrier") or "").strip(),
                    "vehicles": list(row.get("vehicles") or []),
                }
            )
        return out

    def upsert(
        self,
        *,
        driver_id: int = 0,
        full_name: str,
        phone: str = "",
        documents: str = "",
        carrier: str = "",
        vehicles: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        name = str(full_name or "").strip()
        if not name:
            raise ValueError("Укажите ФИО водителя")
        vehicles_json = json.dumps(vehicles or [], ensure_ascii=False)
        now = _now()
        did = int(driver_id or 0)
        with self.db.connect() as conn:
            if did > 0:
                conn.execute(
                    """
                    UPDATE supply_drivers
                    SET full_name = ?, phone = ?, documents = ?, carrier = ?,
                        vehicles_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        str(phone or "").strip(),
                        str(documents or "").strip(),
                        str(carrier or "").strip(),
                        vehicles_json,
                        now,
                        did,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO supply_drivers(
                        full_name, phone, documents, carrier, vehicles_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        str(phone or "").strip(),
                        str(documents or "").strip(),
                        str(carrier or "").strip(),
                        vehicles_json,
                        now,
                        now,
                    ),
                )
                did = int(cur.lastrowid or 0)
            conn.commit()
        for row in self.list_drivers():
            if int(row.get("id") or 0) == did:
                return row
        return {"id": did, "full_name": name}

    def delete(self, driver_id: int) -> None:
        did = int(driver_id or 0)
        if did <= 0:
            raise ValueError("Укажите водителя")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM supply_drivers WHERE id = ?", (did,))
            conn.commit()

    def _table(self, platform: str) -> str:
        plat = str(platform or "").strip().lower()
        if plat == "wb":
            return "wb_fbs_supply_driver"
        if plat == "ozon":
            return "ozon_fbs_supply_driver"
        raise ValueError("platform: wb или ozon")

    def get_assignment(
        self, platform: str, source_id: int, supply_id: str
    ) -> Dict[str, Any]:
        sid = str(supply_id or "").strip()
        if not sid or int(source_id or 0) <= 0:
            return empty_assignment()
        table = self._table(platform)
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT driver_id, driver_name, vehicle_number
                FROM {}
                WHERE source_id = ? AND supply_id = ?
                """.format(
                    table
                ),
                (int(source_id), sid),
            ).fetchone()
        if not row:
            return empty_assignment()
        d = Database.row_to_dict(row) or {}
        did = int(d.get("driver_id") or 0)
        name = str(d.get("driver_name") or "").strip()
        return {
            "driver_id": did,
            "driver_name": name,
            "vehicle_number": str(d.get("vehicle_number") or "").strip(),
            "has_driver": did > 0 and bool(name),
        }

    def set_assignment(
        self,
        platform: str,
        source_id: int,
        supply_id: str,
        driver_id: int,
        vehicle_number: str = "",
    ) -> Dict[str, Any]:
        sid = str(supply_id or "").strip()
        if not sid:
            raise ValueError("Не указан ID поставки")
        did = int(driver_id or 0)
        if did <= 0:
            raise ValueError("Выберите водителя")
        opts = self.options()
        row = next((d for d in opts if int(d.get("id") or 0) == did), None)
        if not row:
            raise ValueError("Водитель не найден в справочнике")
        plates = list(row.get("vehicles") or [])
        want = _norm_plate(vehicle_number)
        chosen = ""
        if want:
            match = next(
                (
                    p
                    for p in plates
                    if _norm_plate(p.get("number")) == want
                ),
                None,
            )
            if not match and plates:
                raise ValueError("Гос. номер не относится к этому водителю")
            chosen = (
                str(match.get("number") or "").strip()
                if match
                else str(vehicle_number or "").strip()
            )
        elif len(plates) == 1:
            chosen = str(plates[0].get("number") or "").strip()
        elif len(plates) > 1:
            raise ValueError("Выберите гос. номер")
        name = str(row.get("full_name") or "").strip()
        table = self._table(platform)
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO {}(
                    source_id, supply_id, driver_id, driver_name,
                    vehicle_number, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, supply_id) DO UPDATE SET
                    driver_id = excluded.driver_id,
                    driver_name = excluded.driver_name,
                    vehicle_number = excluded.vehicle_number,
                    updated_at = excluded.updated_at
                """.format(
                    table
                ),
                (int(source_id), sid, did, name, chosen, _now()),
            )
            conn.commit()
        return self.get_assignment(platform, source_id, sid)

    def clear_assignment(
        self, platform: str, source_id: int, supply_id: str
    ) -> Dict[str, Any]:
        sid = str(supply_id or "").strip()
        table = self._table(platform)
        with self.db.connect() as conn:
            conn.execute(
                "DELETE FROM {} WHERE source_id = ? AND supply_id = ?".format(
                    table
                ),
                (int(source_id), sid),
            )
            conn.commit()
        return empty_assignment()
