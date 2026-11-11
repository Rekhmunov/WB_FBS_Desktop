# -*- coding: utf-8 -*-
"""Ozon FBS split multi-unit postings (/v1/posting/fbs/split) — web parity."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.ozon.sync import upsert_posting
from app.services.ozon_ship import parse_products

_log = logging.getLogger(__name__)


def posting_unit_count(row: Dict[str, Any]) -> int:
    products = parse_products(row.get("products_json") if "products_json" in row else row.get("products"))
    if products:
        total = 0
        for p in products:
            try:
                total += max(1, int(p.get("quantity") or 1))
            except (TypeError, ValueError):
                total += 1
        return max(1, total)
    try:
        return max(1, int(row.get("quantity") or 1))
    except (TypeError, ValueError):
        return 1


def build_split_postings(posting: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One split entry per physical unit (qty exploded)."""
    products = parse_products(
        posting.get("products")
        if isinstance(posting.get("products"), list)
        else posting.get("products_json")
    )
    units = []  # type: List[Dict[str, Any]]
    for p in products:
        product_id = p.get("sku") if p.get("sku") is not None else p.get("product_id")
        try:
            pid = int(product_id)
        except (TypeError, ValueError):
            continue
        try:
            qty = int(p.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        for _ in range(max(qty, 1)):
            units.append({"products": [{"product_id": pid, "quantity": 1}]})
    if len(units) < 2:
        raise RuntimeError("Отправление не является мультизаказом")
    return units


def parse_split_response(
    result: object, *, fallback_posting_number: str
) -> List[str]:
    out = []  # type: List[str]
    seen = set()  # type: set

    def _add(value: object) -> None:
        pn = str(value or "").strip()
        if not pn or pn in seen:
            return
        seen.add(pn)
        out.append(pn)

    if isinstance(result, dict):
        raw = result.get("result", result)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    _add(item.get("posting_number") or item.get("postingNumber"))
                else:
                    _add(item)
        elif isinstance(raw, dict):
            nums = raw.get("posting_numbers") or raw.get("postings")
            if isinstance(nums, list):
                for item in nums:
                    if isinstance(item, dict):
                        _add(item.get("posting_number"))
                    else:
                        _add(item)
            else:
                _add(raw.get("posting_number"))
    if not out:
        _add(fallback_posting_number)
    return out


class OzonSplitService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def preview_multi(self, source_id: int) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_postings
                WHERE source_id = ?
                  AND tab IN ('new', 'awaiting_packaging')
                """,
                (source_id,),
            ).fetchall()
        items = [Database.row_to_dict(r) for r in rows]
        multi_rows = [r for r in items if posting_unit_count(r) > 1]
        units_total = sum(posting_unit_count(r) for r in items)
        return {
            "ok": True,
            "posting_count": len(items),
            "multi_posting_count": len(multi_rows),
            "result_posting_count": units_total,
            "extra_postings": max(units_total - len(items), 0),
            "posting_numbers": [
                str(r.get("posting_number") or "")
                for r in multi_rows
                if str(r.get("posting_number") or "").strip()
            ],
        }

    def split_posting(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_number: str,
    ) -> Dict[str, Any]:
        pnum = str(posting_number or "").strip()
        if not pnum:
            raise ValueError("posting_number required")
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM ozon_fbs_postings
                WHERE source_id = ? AND posting_number = ?
                """,
                (source_id, pnum),
            ).fetchone()
        local = Database.row_to_dict(row) if row else {}
        units = posting_unit_count(local) if local else 0
        if units < 2:
            return {
                "ok": True,
                "skipped": True,
                "posting_number": pnum,
                "posting_numbers": [pnum],
                "message": "Не мультизаказ",
            }
        remote = {}  # type: Dict[str, Any]
        try:
            remote = client.get_posting(pnum) or {}
        except Exception as exc:
            _log.warning("ozon split get_posting %s: %s", pnum, exc)
        base = remote if remote else local
        plan = build_split_postings(base)
        result = client.split_posting(pnum, plan)
        posting_numbers = parse_split_response(result, fallback_posting_number=pnum)
        for pn in posting_numbers:
            try:
                fresh = client.get_posting(pn)
                if fresh:
                    upsert_posting(self.db, source_id, fresh)
                    continue
            except Exception:
                pass
            # Fallback: keep awaiting_packaging locally.
            payload = dict(base) if isinstance(base, dict) else {}
            payload["posting_number"] = pn
            payload["status"] = "awaiting_packaging"
            if pn != pnum:
                # Sibling after split: single unit.
                products = parse_products(payload.get("products"))
                if products:
                    first = dict(products[0])
                    first["quantity"] = 1
                    payload["products"] = [first]
            upsert_posting(self.db, source_id, payload)
        return {
            "ok": True,
            "posting_number": posting_numbers[0] if posting_numbers else pnum,
            "posting_numbers": posting_numbers,
            "from_units": units,
            "to_count": len(posting_numbers),
        }

    def split_all_multi(
        self,
        client: OzonFbsClient,
        source_id: int,
    ) -> Dict[str, Any]:
        preview = self.preview_multi(source_id)
        nums = list(preview.get("posting_numbers") or [])
        if not nums:
            return {
                "ok": True,
                "split": 0,
                "failed": 0,
                "message": "Нет мультизаказов для разделения",
                "errors": [],
            }
        split_n = 0
        failed = 0
        errors = []  # type: List[str]
        for pn in nums:
            try:
                out = self.split_posting(client, source_id, pn)
                if not out.get("skipped"):
                    split_n += 1
            except Exception as exc:
                failed += 1
                errors.append("{}: {}".format(pn, exc))
                _log.warning("ozon split-all %s: %s", pn, exc)
        if split_n and not failed:
            message = "Разделено {} мультизаказов".format(split_n)
        elif split_n:
            message = "Разделено {}, ошибок {}".format(split_n, failed)
        else:
            message = "Не удалось разделить мультизаказы"
        return {
            "ok": split_n > 0 and failed == 0,
            "split": split_n,
            "failed": failed,
            "message": message,
            "errors": errors,
        }
