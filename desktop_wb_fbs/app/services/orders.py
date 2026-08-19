# -*- coding: utf-8 -*-
"""Orders / supplies listing and local mutations for desktop."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from app.db import Database
from app.services.catalog import ProductService
from app.wb import (
    assembly_stage_label,
    cargo_type_label,
    format_price_rub,
    parse_json_list,
    supply_status_label,
    utc_now,
)
from app.wb.client import WbFbsClient
from app.wb.sync import upsert_supply


def _warehouse_label(row: Dict[str, Any]) -> str:
    offices = parse_json_list(row.get("offices_json"))
    names = [str(x).strip() for x in offices if str(x or "").strip()]
    if names:
        return ", ".join(names)
    wh = row.get("warehouse_id")
    return str(wh) if wh is not None else ""


def _date_short(iso: object) -> str:
    raw = str(iso or "").strip()
    if not raw:
        return ""
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        from datetime import datetime

        return datetime.fromisoformat(raw).strftime("%d.%m.%Y")
    except Exception:
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            return "{}.{}.{}".format(raw[8:10], raw[5:7], raw[0:4])
        return raw[:10]


class OrdersService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._product_title_cache = None  # type: ignore

    def _product_maps(self) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
        by_art = {}  # type: Dict[str, str]
        by_nm = {}  # type: Dict[str, str]
        photo_by = {}  # type: Dict[str, str]
        for p in ProductService(self.db).list_all():
            name = str(p.get("name") or "").strip()
            photo = str(p.get("photo_path") or "").strip()
            art = str(p.get("supplier_article") or "").strip().lower()
            nm = str(p.get("wb_nmid") or "").strip()
            if name:
                if art:
                    by_art[art] = name
                if nm:
                    by_nm[nm] = name
            if photo:
                if art:
                    photo_by[art] = photo
                if nm:
                    photo_by[nm] = photo
        return by_art, by_nm, photo_by

    def _enrich_order(
        self,
        it: Dict[str, Any],
        by_art: Dict[str, str],
        by_nm: Dict[str, str],
        photo_by: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        it["cargo_label"] = cargo_type_label(it.get("cargo_type"))
        it["price_label"] = format_price_rub(
            it.get("final_price"), it.get("currency_code")
        )
        it["skus"] = parse_json_list(it.get("skus_json"))
        it["is_b2b"] = bool(int(it.get("is_b2b") or 0))
        it["warehouse_label"] = _warehouse_label(it)
        art = str(it.get("article") or "").strip().lower()
        nm = str(it.get("nm_id") or "").strip()
        it["product_name"] = by_art.get(art) or by_nm.get(nm) or ""
        photos = photo_by or {}
        it["product_photo"] = photos.get(art) or photos.get(nm) or ""
        return it

    def tab_counts(self, source_id: int) -> Dict[str, int]:
        out = {"new": 0, "assembly": 0, "delivery": 0, "mgt_new": 0}
        with self.db.connect() as conn:
            n = conn.execute(
                """
                SELECT COUNT(*) AS c FROM wb_fbs_orders
                WHERE source_id = ? AND tab = 'new' AND is_archive = 0
                """,
                (source_id,),
            ).fetchone()
            mgt = conn.execute(
                """
                SELECT COUNT(*) AS c FROM wb_fbs_orders
                WHERE source_id = ? AND tab = 'new' AND is_archive = 0
                  AND cargo_type = 1
                """,
                (source_id,),
            ).fetchone()
            a = conn.execute(
                """
                SELECT COUNT(*) AS c FROM wb_fbs_supplies
                WHERE source_id = ? AND done = 0
                """,
                (source_id,),
            ).fetchone()
            d = conn.execute(
                """
                SELECT COUNT(*) AS c FROM wb_fbs_supplies
                WHERE source_id = ? AND done = 1
                """,
                (source_id,),
            ).fetchone()
        out["new"] = int(n["c"] if n else 0)
        out["mgt_new"] = int(mgt["c"] if mgt else 0)
        out["assembly"] = int(a["c"] if a else 0)
        out["delivery"] = int(d["c"] if d else 0)
        return out

    def list_order_ids(
        self, source_id: int, tab: str = "new", search: str = ""
    ) -> List[int]:
        cond = ["source_id = ?", "tab = ?", "is_archive = 0"]
        params = [source_id, tab]  # type: List[Any]
        q = str(search or "").strip()
        if q:
            like = "%{}%".format(q.lower())
            cond.append(
                "(LOWER(CAST(order_id AS TEXT)) LIKE ? OR LOWER(article) LIKE ?"
                " OR LOWER(supply_id) LIKE ? OR LOWER(skus_json) LIKE ?"
                " OR LOWER(COALESCE(offices_json,'')) LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        where = " AND ".join(cond)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT order_id FROM wb_fbs_orders WHERE " + where + " ORDER BY order_id",
                params,
            ).fetchall()
        return [int(r["order_id"]) for r in rows]

    def list_orders(
        self,
        source_id: int,
        tab: str = "new",
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        cond = ["source_id = ?", "tab = ?", "is_archive = 0"]
        params = [source_id, tab]  # type: List[Any]
        q = str(search or "").strip()
        if q:
            like = "%{}%".format(q.lower())
            cond.append(
                "(LOWER(CAST(order_id AS TEXT)) LIKE ? OR LOWER(article) LIKE ?"
                " OR LOWER(supply_id) LIKE ? OR LOWER(skus_json) LIKE ?"
                " OR LOWER(COALESCE(offices_json,'')) LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        where = " AND ".join(cond)
        with self.db.connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM wb_fbs_orders WHERE " + where, params
            ).fetchone()
            rows = conn.execute(
                """
                SELECT * FROM wb_fbs_orders
                WHERE {} ORDER BY created_at_wb DESC, order_id DESC
                LIMIT ? OFFSET ?
                """.format(
                    where
                ),
                params + [limit, offset],
            ).fetchall()
        by_art, by_nm, photo_by = self._product_maps()
        items = [
            self._enrich_order(dict(r), by_art, by_nm, photo_by) for r in rows
        ]
        return items, int(total["c"] if total else 0)

    def list_supplies(
        self,
        source_id: int,
        done: bool,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        cond = ["source_id = ?", "done = ?"]
        params = [source_id, 1 if done else 0]  # type: List[Any]
        q = str(search or "").strip()
        if q:
            like = "%{}%".format(q.lower())
            cond.append(
                "(LOWER(supply_id) LIKE ? OR LOWER(name) LIKE ?)"
            )
            params.extend([like, like])
        where = " AND ".join(cond)
        with self.db.connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM wb_fbs_supplies WHERE " + where, params
            ).fetchone()
            rows = conn.execute(
                """
                SELECT * FROM wb_fbs_supplies
                WHERE {} ORDER BY created_at_wb DESC
                LIMIT ? OFFSET ?
                """.format(
                    where
                ),
                params + [limit, offset],
            ).fetchall()
        items = Database.rows_to_dicts(rows)
        first_oids = []  # type: List[int]
        for it in items:
            oids = parse_json_list(it.get("order_ids_json"))
            boxes = parse_json_list(it.get("boxes_json"))
            it["order_ids"] = oids
            it["order_count"] = len(oids)
            it["boxes_count"] = len(boxes)
            it["cargo_label"] = cargo_type_label(it.get("cargo_type"))
            it["is_b2b"] = bool(int(it.get("is_b2b") or 0))
            it["done"] = bool(int(it.get("done") or 0))
            raw = {}
            try:
                raw = json.loads(it.get("raw_json") or "{}")
            except Exception:
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            it["pickup_allowed"] = bool(
                raw.get("isPickupPointShipmentAllowed") or raw.get("pickup_allowed")
            )
            name = str(it.get("name") or "").strip()
            created = str(it.get("created_at_wb") or "").strip()
            if not name and created:
                # Portal fallback: «Поставка от DD.MM.YYYY»
                short = _date_short(created)
                name = "Поставка от {}".format(short) if short else ""
            if not name:
                sid = str(it.get("supply_id") or "").strip()
                name = "Поставка {}".format(sid) if sid else "Поставка"
            it["name"] = name
            if it["done"]:
                it["status_label"] = supply_status_label(
                    done=True, scan_dt=it.get("scan_dt")
                )
                it["status_kind"] = "scanned" if it.get("scan_dt") else "ship"
            else:
                it["status_label"] = assembly_stage_label(
                    done=False, boxes_count=it["boxes_count"]
                )
                it["status_kind"] = "assembly"
            if oids:
                try:
                    first_oids.append(int(oids[0]))
                except (TypeError, ValueError):
                    pass

        wh_by_oid = {}  # type: Dict[int, Dict[str, Any]]
        if first_oids:
            placeholders = ",".join("?" for _ in first_oids)
            with self.db.connect() as conn:
                wh_rows = conn.execute(
                    """
                    SELECT order_id, warehouse_id, offices_json
                    FROM wb_fbs_orders
                    WHERE source_id = ? AND order_id IN ({})
                    """.format(
                        placeholders
                    ),
                    [source_id] + first_oids,
                ).fetchall()
            for wr in wh_rows:
                wh_by_oid[int(wr["order_id"])] = dict(wr)

        for it in items:
            oids = it.get("order_ids") or []
            wh_row = None
            if oids:
                try:
                    wh_row = wh_by_oid.get(int(oids[0]))
                except (TypeError, ValueError):
                    wh_row = None
            if wh_row:
                it["warehouse_label"] = _warehouse_label(wh_row)
                it["warehouse_id"] = wh_row.get("warehouse_id")
            else:
                dest = it.get("destination_office_id")
                it["warehouse_label"] = (
                    "Офис {}".format(dest) if dest is not None else "—"
                )
                it["warehouse_id"] = dest
        return items, int(total["c"] if total else 0)

    def get_supply(self, source_id: int, supply_id: str) -> Optional[Dict[str, Any]]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM wb_fbs_supplies
                WHERE source_id = ? AND supply_id = ?
                """,
                (source_id, supply_id),
            ).fetchone()
        d = Database.row_to_dict(row)
        if not d:
            return None
        d["order_ids"] = parse_json_list(d.get("order_ids_json"))
        d["boxes"] = parse_json_list(d.get("boxes_json"))
        d["done"] = bool(int(d.get("done") or 0))
        d["is_b2b"] = bool(int(d.get("is_b2b") or 0))
        return d

    def ensure_supply_order_ids(
        self, source_id: int, api_key: str, supply_id: str
    ) -> List[int]:
        """Refresh order ids from WB when local list is empty (done supplies)."""
        supply = self.get_supply(source_id, supply_id)
        oids = list((supply or {}).get("order_ids") or [])
        if oids:
            return [int(x) for x in oids]
        client = WbFbsClient(api_key)
        oids = [int(x) for x in client.get_supply_order_ids(supply_id)]
        time.sleep(0.21)
        boxes = client.get_supply_boxes(supply_id)
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE wb_fbs_supplies
                SET order_ids_json = ?, boxes_json = ?, synced_at = ?
                WHERE source_id = ? AND supply_id = ?
                """,
                (
                    json.dumps(oids, ensure_ascii=False),
                    json.dumps(boxes, ensure_ascii=False),
                    now,
                    source_id,
                    supply_id,
                ),
            )
            for oid in oids:
                conn.execute(
                    """
                    UPDATE wb_fbs_orders
                    SET supply_id = ?, synced_at = ?
                    WHERE source_id = ? AND order_id = ?
                    """,
                    (supply_id, now, source_id, int(oid)),
                )
            conn.commit()
        return oids

    def orders_in_supply(
        self, source_id: int, supply_id: str, api_key: str = ""
    ) -> List[Dict[str, Any]]:
        if api_key:
            try:
                self.ensure_supply_order_ids(source_id, api_key, supply_id)
            except Exception:
                pass
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM wb_fbs_orders
                WHERE source_id = ? AND supply_id = ?
                ORDER BY article COLLATE NOCASE, order_id
                """,
                (source_id, supply_id),
            ).fetchall()
        by_art, by_nm, photo_by = self._product_maps()
        items = [
            self._enrich_order(dict(r), by_art, by_nm, photo_by) for r in rows
        ]
        for it in items:
            it["kiz_codes"] = parse_json_list(it.get("kiz_codes_json"))
            it["pick_verified"] = bool(int(it.get("pick_verified") or 0))
        return items

    def create_supply_from_orders(
        self,
        source_id: int,
        api_key: str,
        order_ids: List[int],
        name: str,
    ) -> str:
        client = WbFbsClient(api_key)
        data = client.create_supply(name=name)
        sid = str(data.get("id") or "").strip()
        if not sid:
            raise RuntimeError("WB не вернул ID поставки")
        # chunk 100
        for i in range(0, len(order_ids), 100):
            if i:
                time.sleep(0.21)
            client.add_orders_to_supply(sid, order_ids[i : i + 100])
        time.sleep(0.21)
        supply = client.get_supply(sid)
        oids = client.get_supply_order_ids(sid)
        boxes = client.get_supply_boxes(sid)
        upsert_supply(self.db, source_id, supply or {"id": sid, "name": name}, oids, boxes)
        now = utc_now()
        with self.db.connect() as conn:
            for oid in order_ids:
                conn.execute(
                    """
                    UPDATE wb_fbs_orders
                    SET supply_id = ?, supplier_status = 'confirm', tab = 'assembly', synced_at = ?
                    WHERE source_id = ? AND order_id = ?
                    """,
                    (sid, now, source_id, oid),
                )
            conn.commit()
        return sid

    def add_orders_to_existing_supply(
        self,
        source_id: int,
        api_key: str,
        supply_id: str,
        order_ids: List[int],
    ) -> None:
        client = WbFbsClient(api_key)
        for i in range(0, len(order_ids), 100):
            if i:
                time.sleep(0.21)
            client.add_orders_to_supply(supply_id, order_ids[i : i + 100])
        time.sleep(0.21)
        supply = client.get_supply(supply_id)
        oids = client.get_supply_order_ids(supply_id)
        boxes = client.get_supply_boxes(supply_id)
        upsert_supply(self.db, source_id, supply or {"id": supply_id}, oids, boxes)
        now = utc_now()
        with self.db.connect() as conn:
            for oid in order_ids:
                conn.execute(
                    """
                    UPDATE wb_fbs_orders
                    SET supply_id = ?, supplier_status = 'confirm', tab = 'assembly', synced_at = ?
                    WHERE source_id = ? AND order_id = ?
                    """,
                    (supply_id, now, source_id, oid),
                )
            conn.commit()

    def open_compatible_supplies(
        self, source_id: int, cargo_type: int, is_b2b: bool, warehouse_id: Any
    ) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM wb_fbs_supplies
                WHERE source_id = ? AND done = 0
                ORDER BY created_at_wb DESC
                """,
                (source_id,),
            ).fetchall()
        out = []  # type: List[Dict[str, Any]]
        for r in Database.rows_to_dicts(rows):
            if int(r.get("cargo_type") or 0) != int(cargo_type or 0):
                continue
            if bool(int(r.get("is_b2b") or 0)) != bool(is_b2b):
                continue
            oids = parse_json_list(r.get("order_ids_json"))
            # empty supply always compatible; else check warehouse via first order
            if oids and warehouse_id is not None:
                with self.db.connect() as conn:
                    ow = conn.execute(
                        """
                        SELECT warehouse_id FROM wb_fbs_orders
                        WHERE source_id = ? AND order_id = ?
                        """,
                        (source_id, int(oids[0])),
                    ).fetchone()
                if ow and ow["warehouse_id"] is not None:
                    if int(ow["warehouse_id"]) != int(warehouse_id):
                        continue
            r["order_count"] = len(oids)
            out.append(r)
        return out

    def new_mgt_orders(self, source_id: int) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM wb_fbs_orders
                WHERE source_id = ? AND tab = 'new' AND is_archive = 0
                  AND cargo_type = 1
                ORDER BY warehouse_id, is_b2b, order_id
                """,
                (source_id,),
            ).fetchall()
        return Database.rows_to_dicts(rows)
