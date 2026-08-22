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


_EXEMPLAR_OK = frozenset({"ok", "passed", "success", "completed", "validated"})
_EXEMPLAR_PENDING = frozenset(
    {"pending", "in_process", "processing", "new", "awaiting", "in_progress"}
)
_EXEMPLAR_ERROR = frozenset({"error", "failed", "invalid", "rejected"})


def _build_exemplar_products_payload(
    exemplar_data: Dict[str, Any],
    cleaned: List[str],
    *,
    for_set: bool = False,
) -> List[Dict[str, Any]]:
    products_payload = []  # type: List[Dict[str, Any]]
    mark_idx = 0
    for prod in exemplar_data.get("products") or []:
        if not isinstance(prod, dict):
            continue
        product_id = prod.get("product_id") or prod.get("sku")
        exemplars = prod.get("exemplars") or []
        if not isinstance(exemplars, list) or not exemplars:
            exemplars = [{}]
        exemplars_out = []  # type: List[Dict[str, Any]]
        for ex in exemplars:
            if not isinstance(ex, dict):
                ex = {}
            mark_code = cleaned[mark_idx] if mark_idx < len(cleaned) else ""
            if for_set:
                item = {
                    "exemplar_id": ex.get("exemplar_id"),
                    "marks": [],
                }
            else:
                item = {"gtd": "", "rnpt": "", "marks": []}
            if mark_code:
                item["marks"] = [
                    {"mark": mark_code, "mark_type": "mandatory_mark"}
                ]
            exemplars_out.append(item)
            mark_idx += 1
        if product_id is not None:
            products_payload.append(
                {"product_id": int(product_id), "exemplars": exemplars_out}
            )
    return products_payload


def _exemplar_sync_state(
    status_data: Dict[str, Any],
    expected_marks: List[str],
) -> Tuple[bool, bool, str]:
    """Return (synced, pending, error_message)."""
    mark_statuses = []  # type: List[str]
    found_marks = []  # type: List[str]
    for prod in status_data.get("products") or []:
        if not isinstance(prod, dict):
            continue
        for ex in prod.get("exemplars") or []:
            if not isinstance(ex, dict):
                continue
            for mk in ex.get("marks") or []:
                if not isinstance(mk, dict):
                    continue
                cs = str(mk.get("check_status") or "").strip().lower()
                if cs:
                    mark_statuses.append(cs)
                code = str(mk.get("mark") or "").strip()
                if code:
                    found_marks.append(code)
    if any(s in _EXEMPLAR_ERROR for s in mark_statuses):
        return False, False, "Ozon отклонил код маркировки"
    if any(s in _EXEMPLAR_PENDING for s in mark_statuses):
        return False, True, ""
    if mark_statuses and all(s in _EXEMPLAR_OK for s in mark_statuses):
        return True, False, ""
    exp = [m for m in expected_marks if m]
    if exp and found_marks:
        if set(exp).issubset(set(found_marks)) and not any(
            s in _EXEMPLAR_ERROR for s in mark_statuses
        ):
            return True, False, ""
    return False, bool(mark_statuses), ""


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

    def single_marking_row(
        self,
        source_id: int,
        posting_number: str,
        *,
        client: Optional[OzonFbsClient] = None,
    ) -> Optional[Dict[str, Any]]:
        row = self.orders.get_posting(source_id, posting_number)
        if not row:
            return None
        if str(row.get("status") or "").lower() == "cancelled":
            return None
        marks = _parse_marks(row.get("marks_json"))
        prod = _catalog_product(
            self.db, str(row.get("offer_id") or ""), str(row.get("sku") or "")
        )
        return self._row_view(row, prod, marks)

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

    def refresh_mark_status(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_number: str,
    ) -> Dict[str, Any]:
        """Live ↻ from Ozon exemplar/status."""
        pnum = str(posting_number or "").strip()
        out = {
            "posting_number": pnum,
            "marks": [],
            "marks_synced": False,
            "needs_mark": False,
        }
        if not pnum:
            return out
        try:
            status = client.exemplar_status(pnum)
            marks = []  # type: List[str]
            needs = False
            for prod in status.get("products") or []:
                if not isinstance(prod, dict):
                    continue
                for ex in prod.get("exemplars") or []:
                    if not isinstance(ex, dict):
                        continue
                    if ex.get("is_mandatory_mark_needed"):
                        needs = True
                    for mk in ex.get("marks") or []:
                        if not isinstance(mk, dict):
                            continue
                        code = str(mk.get("mark") or "").strip()
                        if code:
                            marks.append(code)
                        if mk.get("mark_type") == "mandatory_mark":
                            needs = True
            synced, _, _err = _exemplar_sync_state(status, marks)
            if marks:
                self.save_local(source_id, pnum, marks, synced=synced)
            out["marks"] = marks
            out["marks_synced"] = synced
            out["needs_mark"] = needs and not marks
        except Exception:
            pass
        return out

    def validate_on_ozon(
        self,
        client: OzonFbsClient,
        posting_number: str,
        marks: List[str],
    ) -> Tuple[bool, str]:
        """POST /v5/fbs/posting/product/exemplar/validate before set."""
        pnum = str(posting_number or "").strip()
        cleaned = [
            kiz_code_clean(c).replace("\u2194", "\u001d")
            for c in marks
            if kiz_code_clean(c)
        ]
        if not cleaned:
            return True, ""
        exemplar_data = client.exemplar_create_or_get(pnum)
        products_payload = _build_exemplar_products_payload(
            exemplar_data, cleaned, for_set=False
        )
        if not products_payload:
            return False, "Ozon не вернул exemplar для валидации"
        result = client.exemplar_validate(pnum, products_payload)
        for prod in result.get("products") or []:
            if not isinstance(prod, dict):
                continue
            for ex in prod.get("exemplars") or []:
                if not isinstance(ex, dict):
                    continue
                for mk in ex.get("marks") or []:
                    if not isinstance(mk, dict):
                        continue
                    if mk.get("error_codes"):
                        return False, "Ozon отклонил код маркировки"
        return True, ""

    def wait_exemplar_synced(
        self,
        client: OzonFbsClient,
        posting_number: str,
        marks: List[str],
        *,
        timeout_s: float = 90.0,
        poll_s: float = 2.0,
    ) -> Dict[str, Any]:
        pnum = str(posting_number or "").strip()
        cleaned = [
            kiz_code_clean(c).replace("\u2194", "\u001d")
            for c in marks
            if kiz_code_clean(c)
        ]
        deadline = time.monotonic() + max(5.0, float(timeout_s))
        last = {}  # type: Dict[str, Any]
        while time.monotonic() < deadline:
            last = client.exemplar_status(pnum)
            synced, pending, err = _exemplar_sync_state(last, cleaned)
            if err:
                raise RuntimeError(err)
            if synced:
                return last
            if not pending and cleaned:
                break
            time.sleep(max(0.5, float(poll_s)))
        synced, _, err = _exemplar_sync_state(last, cleaned)
        if err:
            raise RuntimeError(err)
        if synced:
            return last
        raise RuntimeError("Таймаут ожидания проверки маркировки в Ozon")

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
        ok, err = self.validate_on_ozon(client, pnum, cleaned)
        if not ok:
            raise RuntimeError(err or "Валидация маркировки не пройдена")
        exemplar_data = client.exemplar_create_or_get(pnum)
        products_payload = _build_exemplar_products_payload(
            exemplar_data, cleaned, for_set=True
        )
        if not products_payload:
            raise RuntimeError(
                "Ozon не вернул exemplar для отправления {} — проверьте статус.".format(pnum)
            )
        client.exemplar_set(pnum, products_payload)
        self.wait_exemplar_synced(client, pnum, cleaned)
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
        if not str(carriage_id or "").strip():
            return []
        return OzonMarkService(self.db).pick_rows(source_id, carriage_id)

    def single_row(
        self, source_id: int, posting_number: str
    ) -> List[Dict[str, Any]]:
        row = self.orders.get_posting(source_id, posting_number)
        if not row:
            return []
        marks = _parse_marks(row.get("marks_json"))
        if marks:
            return []
        prod = _catalog_product(
            self.db, str(row.get("offer_id") or ""), str(row.get("sku") or "")
        )
        return [OzonMarkService(self.db)._row_view(row, prod, marks)]

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
