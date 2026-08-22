# -*- coding: utf-8 -*-
"""Ozon FBS postings / carriages listing (parallel to OrdersService, WB untouched)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.db import Database
from app.ozon import carriage_status_label, status_label
from app.services.catalog import ProductService


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


class OzonOrdersService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._photo_by_offer = None  # type: Optional[Dict[str, str]]

    def _photo_map(self) -> Dict[str, str]:
        if self._photo_by_offer is not None:
            return self._photo_by_offer
        out = {}  # type: Dict[str, str]
        for p in ProductService(self.db).list_all():
            photo = str(p.get("photo_path") or "").strip()
            if not photo:
                continue
            oz = str(p.get("ozon_sku") or "").strip().lower()
            art = str(p.get("supplier_article") or "").strip().lower()
            if oz:
                out[oz] = photo
            if art:
                out[art] = photo
        self._photo_by_offer = out
        return out

    def tab_counts(self, source_id: int) -> Dict[str, int]:
        out = {"new": 0, "assembly": 0, "delivery": 0}
        with self.db.connect() as conn:
            n = conn.execute(
                """
                SELECT COUNT(*) AS c FROM ozon_fbs_postings
                WHERE source_id = ? AND tab = 'new'
                """,
                (source_id,),
            ).fetchone()
            a = conn.execute(
                """
                SELECT COUNT(*) AS c FROM ozon_fbs_carriages
                WHERE source_id = ? AND done = 0
                """,
                (source_id,),
            ).fetchone()
            d = conn.execute(
                """
                SELECT COUNT(*) AS c FROM ozon_fbs_carriages
                WHERE source_id = ? AND done = 1
                """,
                (source_id,),
            ).fetchone()
        out["new"] = int(n["c"] if n else 0)
        out["assembly"] = int(a["c"] if a else 0)
        out["delivery"] = int(d["c"] if d else 0)
        return out

    def list_new_postings(
        self, source_id: int, *, search: str = "", limit: int = 500
    ) -> List[Dict[str, Any]]:
        cond = ["source_id = ?", "tab = 'new'"]
        params = [source_id]  # type: List[Any]
        q = str(search or "").strip().lower()
        if q:
            like = "%{}%".format(q)
            cond.append(
                "(LOWER(posting_number) LIKE ? OR LOWER(offer_id) LIKE ?"
                " OR LOWER(sku) LIKE ? OR LOWER(product_name) LIKE ?"
                " OR LOWER(barcodes_json) LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        sql = """
            SELECT * FROM ozon_fbs_postings
            WHERE {}
            ORDER BY datetime(created_at_wb) DESC, posting_number DESC
            LIMIT ?
        """.format(" AND ".join(cond))
        params.append(max(1, int(limit)))
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._enrich_posting(Database.row_to_dict(r)) for r in rows]

    def list_open_carriages(self, source_id: int) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_carriages
                WHERE source_id = ? AND done = 0
                ORDER BY datetime(synced_at) DESC, carriage_id DESC
                """,
                (source_id,),
            ).fetchall()
        out = []
        for r in rows:
            item = Database.row_to_dict(r)
            pnums = json.loads(str(item.get("posting_numbers_json") or "[]"))
            item["posting_count"] = len(pnums) if isinstance(pnums, list) else 0
            item["status_label"] = carriage_status_label(str(item.get("status") or ""))
            item["status_kind"] = "assembly"
            out.append(item)
        return out

    def _enrich_posting(self, row: Dict[str, Any]) -> Dict[str, Any]:
        photos = self._photo_map()
        offer = str(row.get("offer_id") or "").strip().lower()
        sku = str(row.get("sku") or "").strip().lower()
        catalog_name = ""
        for p in ProductService(self.db).list_all():
            if offer and str(p.get("supplier_article") or "").strip().lower() == offer:
                catalog_name = str(p.get("name") or "")
                break
            if sku and str(p.get("ozon_sku") or "").strip().lower() == sku:
                catalog_name = str(p.get("name") or "")
                break
        row["product_name_display"] = (
            catalog_name or str(row.get("product_name") or "") or offer or sku or "—"
        )
        row["product_photo"] = photos.get(offer) or photos.get(sku) or ""
        row["status_label"] = status_label(str(row.get("status") or ""))
        row["created_date"] = _date_short(row.get("created_at_wb"))
        try:
            row["barcodes"] = json.loads(str(row.get("barcodes_json") or "[]"))
        except Exception:
            row["barcodes"] = []
        return row
