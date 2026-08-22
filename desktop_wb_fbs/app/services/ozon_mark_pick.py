# -*- coding: utf-8 -*-
"""Ozon FBS marking (exemplar) and pick verification — isolated from WB kiz_pick."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.services.catalog import ProductService
from app.services.kiz_pick import (
    PickVerifyService,
    extract_gtin,
    gtin_matches_skus,
    kiz_code_clean,
    order_sku_set,
)
from app.services.ozon_orders import OzonOrdersService
from app.wb import utc_now


def _parse_marks(raw: object) -> List[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    try:
        data = json.loads(str(raw or "[]"))
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return []


def _parse_barcodes(raw: object) -> List[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    try:
        data = json.loads(str(raw or "[]"))
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return []


def _catalog_product(db: Database, offer_id: str, sku: str) -> Dict[str, Any]:
    offer = str(offer_id or "").strip().lower()
    oz = str(sku or "").strip().lower()
    for p in ProductService(db).list_all():
        art = str(p.get("supplier_article") or "").strip().lower()
        if offer and art == offer:
            return p
        if oz and str(p.get("ozon_sku") or "").strip().lower() == oz:
            return p
    return {}


class OzonMarkService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.orders = OzonOrdersService(db)
        self._pick_helper = PickVerifyService(db)

    def marking_rows(
        self,
        source_id: int,
        carriage_id: str,
        *,
        client: Optional[OzonFbsClient] = None,
    ) -> List[Dict[str, Any]]:
        rows = self.orders.postings_in_carriage(source_id, carriage_id)
        out = []  # type: List[Dict[str, Any]]
        for r in rows:
            if str(r.get("status") or "").lower() == "cancelled":
                continue
            marks = _parse_marks(r.get("marks_json"))
            prod = _catalog_product(
                self.db, str(r.get("offer_id") or ""), str(r.get("sku") or "")
            )
            out.append(self._row_view(r, prod, marks))
        return out

    def pick_rows(self, source_id: int, carriage_id: str) -> List[Dict[str, Any]]:
        rows = self.orders.postings_in_carriage(source_id, carriage_id)
        out = []  # type: List[Dict[str, Any]]
        for r in rows:
            if str(r.get("status") or "").lower() == "cancelled":
                continue
            marks = _parse_marks(r.get("marks_json"))
            if marks:
                continue
            prod = _catalog_product(
                self.db, str(r.get("offer_id") or ""), str(r.get("sku") or "")
            )
            out.append(self._row_view(r, prod, marks))
        return out

    def _row_view(
        self, row: Dict[str, Any], prod: Dict[str, Any], marks: List[str]
    ) -> Dict[str, Any]:
        barcodes = _parse_barcodes(row.get("barcodes_json"))
        return {
            "posting_number": str(row.get("posting_number") or ""),
            "offer_id": str(row.get("offer_id") or ""),
            "sku": str(row.get("sku") or ""),
            "product_name": str(
                prod.get("name") or row.get("product_name_display") or row.get("product_name") or ""
            ),
            "product_name_display": str(
                prod.get("name") or row.get("product_name_display") or row.get("product_name") or ""
            ),
            "product_photo": str(
                prod.get("photo_path") or row.get("product_photo") or ""
            ),
            "barcodes": barcodes,
            "skus": barcodes,
            "marks": list(marks),
            "marks_synced": bool(int(row.get("marks_synced") or 0)),
            "pick_verified": bool(int(row.get("pick_verified") or 0)),
            "pick_barcode": str(row.get("pick_barcode") or ""),
            "status": str(row.get("status") or ""),
            "status_label": str(row.get("status_label") or ""),
            "skip_gtin_check": bool(prod.get("skip_kiz_gtin_check")),
        }

    def _posting_needs_mark(
        self, posting_number: str, client: Optional[OzonFbsClient]
    ) -> bool:
        if not client or not posting_number:
            return True
        try:
            status = client.exemplar_status(posting_number)
            products = status.get("products") if isinstance(status, dict) else []
            if isinstance(products, list):
                for prod in products:
                    if not isinstance(prod, dict):
                        continue
                    for ex in prod.get("exemplars") or []:
                        if not isinstance(ex, dict):
                            continue
                        if ex.get("is_mandatory_mark_needed") or ex.get("mandatory_mark"):
                            return True
                        for mk in ex.get("marks") or []:
                            if isinstance(mk, dict) and mk.get("mark_type") == "mandatory_mark":
                                return True
            data = client.exemplar_create_or_get(posting_number)
            for prod in data.get("products") or []:
                if not isinstance(prod, dict):
                    continue
                for ex in prod.get("exemplars") or []:
                    if isinstance(ex, dict) and (
                        ex.get("is_mandatory_mark_needed") or ex.get("mandatory_mark")
                    ):
                        return True
        except Exception:
            return True
        return False

    def validate_mark(
        self, code: str, skus: List[Any], skip_gtin: bool
    ) -> Tuple[bool, str]:
        cleaned = kiz_code_clean(code).replace("\u2194", "\u001d")
        if not cleaned:
            return False, "Пустой код"
        gtin = extract_gtin(cleaned)
        if not gtin:
            return False, (
                "Не удалось выделить GTIN из кода маркировки "
                "(ожидается префикс 01 и 14 цифр)."
            )
        if skip_gtin:
            return True, ""
        if not order_sku_set(skus):
            return False, "У отправления нет штрихкодов — нельзя сверить GTIN."
        if not gtin_matches_skus(gtin, skus):
            shown = gtin[1:] if gtin.startswith("0") and len(gtin) == 14 else gtin
            return False, "GTIN {} не совпадает со ШК товара".format(shown)
        return True, ""

    def save_local(
        self,
        source_id: int,
        posting_number: str,
        marks: List[str],
        *,
        synced: bool = False,
    ) -> None:
        cleaned = [kiz_code_clean(c).replace("\u2194", "\u001d") for c in marks if kiz_code_clean(c)]
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE ozon_fbs_postings
                SET marks_json = ?, marks_saved_at = ?, marks_synced = ?
                WHERE source_id = ? AND posting_number = ?
                """,
                (
                    json.dumps(cleaned, ensure_ascii=False),
                    now,
                    1 if synced else 0,
                    source_id,
                    str(posting_number or "").strip(),
                ),
            )
            conn.commit()

    def save_to_ozon(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_number: str,
        marks: List[str],
    ) -> None:
        pnum = str(posting_number or "").strip()
        cleaned = [kiz_code_clean(c).replace("\u2194", "\u001d") for c in marks if kiz_code_clean(c)]
        self.save_local(source_id, pnum, cleaned, synced=False)
        exemplar_data = client.exemplar_create_or_get(pnum)
        products_payload = []  # type: List[Dict[str, Any]]
        for prod in exemplar_data.get("products") or []:
            if not isinstance(prod, dict):
                continue
            product_id = prod.get("product_id") or prod.get("sku")
            exemplars_out = []  # type: List[Dict[str, Any]]
            exemplars = prod.get("exemplars") or []
            if not isinstance(exemplars, list) or not exemplars:
                exemplars = [{}]
            for i, ex in enumerate(exemplars):
                if not isinstance(ex, dict):
                    ex = {}
                mark_code = cleaned[i] if i < len(cleaned) else (cleaned[0] if cleaned else "")
                item = {
                    "exemplar_id": ex.get("exemplar_id"),
                    "marks": [],
                }
                if mark_code:
                    item["marks"] = [
                        {"mark": mark_code, "mark_type": "mandatory_mark"}
                    ]
                exemplars_out.append(item)
            if product_id is not None:
                products_payload.append(
                    {"product_id": int(product_id), "exemplars": exemplars_out}
                )
        if not products_payload:
            raise RuntimeError(
                "Ozon не вернул exemplar для отправления {} — проверьте статус.".format(pnum)
            )
        client.exemplar_set(pnum, products_payload)
        self.save_local(source_id, pnum, cleaned, synced=True)

    def check_carriage_status(
        self,
        source_id: int,
        carriage_id: str,
        client: OzonFbsClient,
    ) -> Dict[str, Any]:
        rows = self.marking_rows(source_id, carriage_id, client=client)
        total = len(rows)
        ok = 0
        pending = 0
        for r in rows:
            marks = r.get("marks") or []
            if marks and r.get("marks_synced"):
                ok += 1
            elif marks:
                pending += 1
        return {
            "total": total,
            "ok": ok,
            "pending": pending,
            "empty": max(0, total - ok - pending),
        }


class OzonPickService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.orders = OzonOrdersService(db)
        self._wb_pick = PickVerifyService(db)

    def rows(self, source_id: int, carriage_id: str) -> List[Dict[str, Any]]:
        return OzonMarkService(self.db).pick_rows(source_id, carriage_id)

    def validate_barcode(self, barcode: str, skus: List[Any]) -> Tuple[bool, str]:
        return self._wb_pick.validate_barcode(barcode, skus)

    def save(
        self,
        source_id: int,
        posting_number: str,
        verified: bool,
        barcode: str = "",
    ) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE ozon_fbs_postings
                SET pick_verified = ?, pick_barcode = ?,
                    pick_verified_at = ?
                WHERE source_id = ? AND posting_number = ?
                """,
                (
                    1 if verified else 0,
                    str(barcode or "").strip(),
                    now if verified else None,
                    source_id,
                    str(posting_number or "").strip(),
                ),
            )
            conn.commit()

    def check_carriage_status(
        self, source_id: int, carriage_id: str
    ) -> Dict[str, Any]:
        rows = self.rows(source_id, carriage_id)
        total = len(rows)
        ok = sum(1 for r in rows if r.get("pick_verified"))
        return {"total": total, "ok": ok, "empty": max(0, total - ok)}
