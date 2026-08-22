# -*- coding: utf-8 -*-
"""Ozon FBS postings / carriages listing (parallel to OrdersService, WB untouched)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.db import Database
from app.ozon import carriage_is_done, carriage_status_label, status_label, utc_now
from app.ozon.client import OzonFbsClient
from app.ozon.sync import upsert_carriage, upsert_posting
from app.services.catalog import ProductService
from app.services.ozon_act import OzonActService


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
        self._catalog_by_offer = None  # type: Optional[Dict[str, Dict[str, Any]]]

    def _catalog_maps(self) -> Dict[str, Dict[str, Any]]:
        if self._catalog_by_offer is not None:
            return self._catalog_by_offer
        out = {}  # type: Dict[str, Dict[str, Any]]
        for p in ProductService(self.db).list_all():
            art = str(p.get("supplier_article") or "").strip().lower()
            oz = str(p.get("ozon_sku") or "").strip().lower()
            if art:
                out[art] = p
            if oz and oz not in out:
                out[oz] = p
        self._catalog_by_offer = out
        return out

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

    def list_assembly_postings(
        self, source_id: int, *, search: str = "", limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Postings on assembly without carriage (awaiting_deliver, etc.)."""
        cond = [
            "source_id = ?",
            "tab = 'assembly'",
            "(carriage_id IS NULL OR carriage_id = '')",
        ]
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

    def tab_counts(self, source_id: int) -> Dict[str, int]:
        out = {"new": 0, "assembly": 0, "delivery": 0, "finished": 0}
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
            orphan = conn.execute(
                """
                SELECT COUNT(*) AS c FROM ozon_fbs_postings
                WHERE source_id = ? AND tab = 'assembly'
                  AND (carriage_id IS NULL OR carriage_id = '')
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
            f = conn.execute(
                """
                SELECT COUNT(*) AS c FROM ozon_fbs_postings
                WHERE source_id = ? AND tab = 'finished'
                """,
                (source_id,),
            ).fetchone()
        out["new"] = int(n["c"] if n else 0)
        out["assembly"] = int(a["c"] if a else 0) + int(orphan["c"] if orphan else 0)
        out["delivery"] = int(d["c"] if d else 0)
        out["finished"] = int(f["c"] if f else 0)
        return out

    def count_new_postings(self, source_id: int, *, search: str = "") -> int:
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
        sql = "SELECT COUNT(*) AS c FROM ozon_fbs_postings WHERE {}".format(
            " AND ".join(cond)
        )
        with self.db.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"] if row else 0)

    def list_new_postings(
        self,
        source_id: int,
        *,
        search: str = "",
        limit: int = 500,
        offset: int = 0,
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
            LIMIT ? OFFSET ?
        """.format(" AND ".join(cond))
        params.append(max(1, int(limit)))
        params.append(max(0, int(offset)))
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._enrich_posting(Database.row_to_dict(r)) for r in rows]

    def list_finished_postings(
        self,
        source_id: int,
        *,
        search: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        cond = ["source_id = ?", "tab = 'finished'"]
        params = [source_id]  # type: List[Any]
        q = str(search or "").strip().lower()
        if q:
            like = "%{}%".format(q)
            cond.append(
                "(LOWER(posting_number) LIKE ? OR LOWER(offer_id) LIKE ?"
                " OR LOWER(sku) LIKE ? OR LOWER(product_name) LIKE ?)"
            )
            params.extend([like, like, like, like])
        sql = """
            SELECT * FROM ozon_fbs_postings
            WHERE {}
            ORDER BY datetime(created_at_wb) DESC, posting_number DESC
            LIMIT ? OFFSET ?
        """.format(" AND ".join(cond))
        params.extend([max(1, int(limit)), max(0, int(offset))])
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._enrich_posting(Database.row_to_dict(r)) for r in rows]

    def count_finished_postings(self, source_id: int, *, search: str = "") -> int:
        cond = ["source_id = ?", "tab = 'finished'"]
        params = [source_id]  # type: List[Any]
        q = str(search or "").strip().lower()
        if q:
            like = "%{}%".format(q)
            cond.append(
                "(LOWER(posting_number) LIKE ? OR LOWER(offer_id) LIKE ?"
                " OR LOWER(sku) LIKE ? OR LOWER(product_name) LIKE ?)"
            )
            params.extend([like, like, like, like])
        sql = "SELECT COUNT(*) AS c FROM ozon_fbs_postings WHERE {}".format(
            " AND ".join(cond)
        )
        with self.db.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"] if row else 0)

    def list_delivery_carriages(self, source_id: int) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_carriages
                WHERE source_id = ? AND done = 1
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
            item["status_kind"] = "delivery"
            out.append(item)
        return out

    def get_posting(
        self, source_id: int, posting_number: str
    ) -> Optional[Dict[str, Any]]:
        pnum = str(posting_number or "").strip()
        if not pnum:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM ozon_fbs_postings
                WHERE source_id = ? AND posting_number = ?
                """,
                (source_id, pnum),
            ).fetchone()
        if not row:
            return None
        return self._enrich_posting(Database.row_to_dict(row))

    def add_postings_to_carriage(
        self,
        source_id: int,
        client: OzonFbsClient,
        carriage_id: str,
        posting_numbers: List[str],
    ) -> None:
        from app.services.ozon_ship import OzonShipService, posting_needs_ship

        cid = str(carriage_id or "").strip()
        nums = [str(p).strip() for p in posting_numbers if str(p).strip()]
        if not cid or not nums:
            raise ValueError("Укажите отгрузку и отправления")
        ship_svc = OzonShipService(self.db)
        for pnum in nums:
            row = self.get_posting(source_id, pnum)
            status = str((row or {}).get("status") or "")
            if posting_needs_ship(status):
                ship_svc.ship_posting(
                    client,
                    source_id,
                    pnum,
                    posting=row,
                )
        if not client.set_carriage_postings(int(cid), nums):
            raise RuntimeError("Ozon не принял отправления в отгрузку {}".format(cid))
        now = utc_now()
        with self.db.connect() as conn:
            for pnum in nums:
                conn.execute(
                    """
                    UPDATE ozon_fbs_postings
                    SET carriage_id = ?, tab = 'assembly', synced_at = ?
                    WHERE source_id = ? AND posting_number = ?
                    """,
                    (cid, now, source_id, pnum),
                )
            row = conn.execute(
                """
                SELECT posting_numbers_json FROM ozon_fbs_carriages
                WHERE source_id = ? AND carriage_id = ?
                """,
                (source_id, cid),
            ).fetchone()
            existing = []
            if row:
                try:
                    existing = json.loads(str(row["posting_numbers_json"] or "[]"))
                except Exception:
                    existing = []
            merged = list(dict.fromkeys(list(existing or []) + nums))
            conn.execute(
                """
                UPDATE ozon_fbs_carriages
                SET posting_numbers_json = ?, synced_at = ?
                WHERE source_id = ? AND carriage_id = ?
                """,
                (json.dumps(merged, ensure_ascii=False), now, source_id, cid),
            )
            conn.commit()
        self.refresh_carriage(source_id, client, cid)

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

    def get_carriage(self, source_id: int, carriage_id: str) -> Optional[Dict[str, Any]]:
        cid = str(carriage_id or "").strip()
        if not cid:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM ozon_fbs_carriages
                WHERE source_id = ? AND carriage_id = ?
                """,
                (source_id, cid),
            ).fetchone()
        if not row:
            return None
        item = Database.row_to_dict(row)
        pnums = json.loads(str(item.get("posting_numbers_json") or "[]"))
        item["posting_count"] = len(pnums) if isinstance(pnums, list) else 0
        item["status_label"] = carriage_status_label(str(item.get("status") or ""))
        return item

    def postings_in_carriage(
        self,
        source_id: int,
        carriage_id: str,
        *,
        search: str = "",
    ) -> List[Dict[str, Any]]:
        cid = str(carriage_id or "").strip()
        cond = ["source_id = ?", "carriage_id = ?"]
        params = [source_id, cid]  # type: List[Any]
        q = str(search or "").strip().lower()
        if q:
            like = "%{}%".format(q)
            cond.append(
                "(LOWER(posting_number) LIKE ? OR LOWER(offer_id) LIKE ?"
                " OR LOWER(sku) LIKE ? OR LOWER(product_name) LIKE ?)"
            )
            params.extend([like, like, like, like])
        sql = """
            SELECT * FROM ozon_fbs_postings
            WHERE {}
            ORDER BY datetime(created_at_wb) DESC, posting_number DESC
        """.format(" AND ".join(cond))
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        if rows:
            return [self._enrich_posting(Database.row_to_dict(r)) for r in rows]
        carriage = self.get_carriage(source_id, cid)
        if not carriage:
            return []
        pnums = json.loads(str(carriage.get("posting_numbers_json") or "[]"))
        if not isinstance(pnums, list) or not pnums:
            return []
        placeholders = ",".join("?" for _ in pnums)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_postings
                WHERE source_id = ? AND posting_number IN ({})
                ORDER BY datetime(created_at_wb) DESC, posting_number DESC
                """.format(placeholders),
                [source_id] + [str(p) for p in pnums],
            ).fetchall()
        return [self._enrich_posting(Database.row_to_dict(r)) for r in rows]

    def list_delivery_methods(self, client: OzonFbsClient) -> List[Dict[str, Any]]:
        out = []  # type: List[Dict[str, Any]]
        for page in client.iter_carriage_delivery_methods(limit=100):
            for method in page:
                if not isinstance(method, dict):
                    continue
                dm_id = method.get("delivery_method_id")
                name = str(
                    method.get("delivery_method_name")
                    or method.get("warehouse_name")
                    or dm_id
                    or ""
                )
                out.append(
                    {
                        "delivery_method_id": int(dm_id) if dm_id not in (None, "") else 0,
                        "name": name,
                        "warehouse_name": str(method.get("warehouse_name") or ""),
                        "departure_date": str(method.get("departure_date") or ""),
                    }
                )
        return out

    def create_carriage(
        self,
        source_id: int,
        client: OzonFbsClient,
        delivery_method_id: int,
    ) -> str:
        result = client.create_carriage(int(delivery_method_id))
        cid = str(
            result.get("carriage_id") or result.get("id") or ""
        ).strip()
        if not cid:
            raise RuntimeError("Ozon не вернул ID отгрузки")
        upsert_carriage(
            self.db,
            source_id,
            {
                "carriage_id": cid,
                "id": cid,
                "status": str(result.get("status") or "new"),
                "delivery_method_id": int(delivery_method_id),
                "posting_numbers": [],
            },
            delivery_method_id=int(delivery_method_id),
        )
        self.refresh_carriage(source_id, client, cid)
        return cid

    def refresh_carriage(
        self,
        source_id: int,
        client: OzonFbsClient,
        carriage_id: str,
    ) -> int:
        cid = str(carriage_id or "").strip()
        if not cid:
            return 0
        count = 0
        try:
            info = client.get_carriage(int(cid))
            if info:
                upsert_carriage(
                    self.db,
                    source_id,
                    {
                        "carriage_id": cid,
                        "id": cid,
                        "status": str(info.get("status") or ""),
                        "delivery_method_id": info.get("delivery_method_id"),
                    },
                )
                OzonActService(self.db).capture_act_from_carriage(
                    client,
                    source_id,
                    cid,
                    carriage_info=info,
                )
        except Exception:
            info = {}
        dm_id = info.get("delivery_method_id") if isinstance(info, dict) else None
        carriage_row = self.get_carriage(source_id, cid) or {}
        if dm_id in (None, "") and carriage_row.get("delivery_method_id"):
            dm_id = carriage_row.get("delivery_method_id")
        act_svc = OzonActService(self.db)
        pnums = act_svc.postings_for_carriage(
            client,
            source_id,
            cid,
            delivery_method_id=int(dm_id) if dm_id not in (None, "") else None,
        )
        if pnums:
            with self.db.connect() as conn:
                conn.execute(
                    """
                    UPDATE ozon_fbs_carriages
                    SET posting_numbers_json = ?, synced_at = ?
                    WHERE source_id = ? AND carriage_id = ?
                    """,
                    (
                        json.dumps(pnums, ensure_ascii=False),
                        utc_now(),
                        source_id,
                        cid,
                    ),
                )
                conn.commit()
        for pnum in pnums:
            try:
                posting = client.get_posting(pnum)
                if posting:
                    upsert_posting(self.db, source_id, posting, carriage_id=cid)
                    count += 1
            except Exception:
                pass
        if not pnums:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT posting_number FROM ozon_fbs_postings
                    WHERE source_id = ? AND carriage_id = ?
                    """,
                    (source_id, cid),
                ).fetchall()
            for r in rows:
                pnum = str(r["posting_number"] or "")
                if not pnum:
                    continue
                try:
                    posting = client.get_posting(pnum)
                    if posting:
                        upsert_posting(self.db, source_id, posting, carriage_id=cid)
                        count += 1
                except Exception:
                    pass
        return count

    def approve_carriage(
        self, source_id: int, client: OzonFbsClient, carriage_id: str
    ) -> None:
        cid = str(carriage_id or "").strip()
        if not client.approve_carriage(int(cid)):
            raise RuntimeError("Не удалось подтвердить отгрузку {}".format(cid))
        carriage_info = {}
        try:
            carriage_info = client.get_carriage(int(cid))
        except Exception:
            carriage_info = {}
        OzonActService(self.db).capture_act_from_carriage(
            client,
            source_id,
            cid,
            carriage_info=carriage_info,
        )
        self.refresh_carriage(source_id, client, cid)
        carriage = self.get_carriage(source_id, cid) or {}
        status = str(carriage.get("status") or "formed")
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE ozon_fbs_carriages
                SET status = ?, done = ?, synced_at = ?
                WHERE source_id = ? AND carriage_id = ?
                """,
                (
                    status,
                    1 if carriage_is_done(status) else 0,
                    utc_now(),
                    source_id,
                    cid,
                ),
            )
            conn.commit()

    def _enrich_posting(self, row: Dict[str, Any]) -> Dict[str, Any]:
        photos = self._photo_map()
        catalog = self._catalog_maps()
        offer = str(row.get("offer_id") or "").strip().lower()
        sku = str(row.get("sku") or "").strip().lower()
        prod = catalog.get(offer) or catalog.get(sku)
        catalog_name = str(prod.get("name") or "") if prod else ""
        row["product_name_display"] = (
            catalog_name or str(row.get("product_name") or "") or offer or sku or "—"
        )
        row["product_name"] = row["product_name_display"]
        row["product_photo"] = photos.get(offer) or photos.get(sku) or ""
        row["status_label"] = status_label(str(row.get("status") or ""))
        row["created_date"] = _date_short(row.get("created_at_wb"))
        try:
            row["barcodes"] = json.loads(str(row.get("barcodes_json") or "[]"))
        except Exception:
            row["barcodes"] = []
        try:
            products = json.loads(str(row.get("products_json") or "[]"))
            row["products"] = products if isinstance(products, list) else []
        except Exception:
            row["products"] = []
        if row.get("products") and len(row["products"]) > 1:
            row["product_count_label"] = "{} тов.".format(len(row["products"]))
        else:
            row["product_count_label"] = ""
        return row
