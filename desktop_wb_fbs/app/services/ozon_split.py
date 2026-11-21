# -*- coding: utf-8 -*-
"""Ozon FBS split multi-unit postings (/v1/posting/fbs/split) — web parity."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.db import Database
from app.ozon import TAB_AWAITING_PACKAGING
from app.ozon.client import OzonFbsClient
from app.ozon.sync import upsert_posting
from app.services.ozon_ship import parse_products

_log = logging.getLogger(__name__)


def posting_unit_count(row: Dict[str, Any]) -> int:
    products = parse_products(
        row.get("products_json") if "products_json" in row else row.get("products")
    )
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
    """One split entry per physical unit (qty exploded). First stays on parent PN."""
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


def _package_from_split_products(prods: object) -> Optional[Dict[str, Any]]:
    if not isinstance(prods, list):
        return None
    products = []  # type: List[Dict[str, Any]]
    for p in prods:
        if not isinstance(p, dict):
            continue
        raw = p.get("product_id") if p.get("product_id") is not None else p.get("sku")
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        try:
            qty = int(p.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        products.append({"product_id": pid, "quantity": max(1, qty)})
    return {"products": products} if products else None


def parse_split_parts(
    result: object,
    *,
    fallback_posting_number: str,
    split_plan: List[Dict[str, Any]],
) -> List[Tuple[str, Optional[Dict[str, Any]]]]:
    """Map split API response → [(posting_number, products_package), ...].

    Web parity: supports ``parent_posting`` + ``postings``, wrapped ``result``,
    and a plain list of posting numbers. Raises if siblings are missing.
    """
    parent_pn = str(fallback_posting_number).strip()
    parent_products = split_plan[0] if split_plan else None  # type: Optional[Dict[str, Any]]
    children = []  # type: List[Tuple[str, Optional[Dict[str, Any]]]]

    data = result if isinstance(result, dict) else {}
    wrapped = data.get("result") if isinstance(data, dict) else None

    # Plain list of numbers / objects under result.
    if isinstance(wrapped, list):
        nums = []  # type: List[Tuple[str, Optional[Dict[str, Any]]]]
        for item in wrapped:
            if isinstance(item, str):
                pn = item.strip()
                pkg = None  # type: Optional[Dict[str, Any]]
            elif isinstance(item, dict):
                pn = str(item.get("posting_number") or item.get("postingNumber") or "").strip()
                pkg = _package_from_split_products(item.get("products"))
            else:
                continue
            if pn:
                nums.append((pn, pkg))
        if nums:
            parent_pn = nums[0][0]
            if nums[0][1] is not None:
                parent_products = nums[0][1]
            children = nums[1:]
    else:
        if isinstance(wrapped, dict) and (
            "parent_posting" in wrapped or "postings" in wrapped
        ):
            data = wrapped
        parent = data.get("parent_posting") if isinstance(data, dict) else None
        if isinstance(parent, str) and parent.strip():
            parent_pn = parent.strip()
        elif isinstance(parent, dict):
            pn = str(parent.get("posting_number") or "").strip()
            if pn:
                parent_pn = pn
            pkg = _package_from_split_products(parent.get("products"))
            if pkg is not None:
                parent_products = pkg

        raw_children = data.get("postings") if isinstance(data, dict) else None
        sibling_plan_idx = 1
        if isinstance(raw_children, list):
            for item in raw_children:
                if isinstance(item, str):
                    pn = item.strip()
                    package = None  # type: Optional[Dict[str, Any]]
                elif isinstance(item, dict):
                    pn = str(item.get("posting_number") or "").strip()
                    package = _package_from_split_products(item.get("products"))
                else:
                    continue
                if not pn:
                    continue
                if pn == parent_pn:
                    if package is not None:
                        parent_products = package
                    continue
                if package is None and sibling_plan_idx < len(split_plan):
                    package = split_plan[sibling_plan_idx]
                sibling_plan_idx += 1
                children.append((pn, package))

    expected_extra = max(len(split_plan) - 1, 0)
    if expected_extra > 0 and len(children) < expected_extra:
        raise RuntimeError(
            "Ozon не вернул номера после разбиения — повторите или синхронизируйте"
        )

    out = [(parent_pn, parent_products)]  # type: List[Tuple[str, Optional[Dict[str, Any]]]]
    seen = {parent_pn}
    for pn, package in children:
        if pn in seen:
            continue
        seen.add(pn)
        out.append((pn, package))
    return out


def parse_split_response(
    result: object, *, fallback_posting_number: str
) -> List[str]:
    """Backward-compatible: list of posting numbers after split."""
    parts = parse_split_parts(
        result,
        fallback_posting_number=fallback_posting_number,
        split_plan=[{}],  # no sibling-count check for legacy callers
    )
    return [pn for pn, _ in parts] or [str(fallback_posting_number).strip()]


def _products_for_persist(
    package: Optional[Dict[str, Any]],
    base: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Build products list for local upsert from split package + parent meta."""
    meta_by_sku = {}  # type: Dict[int, Dict[str, Any]]
    for p in parse_products(
        base.get("products")
        if isinstance(base.get("products"), list)
        else base.get("products_json")
    ):
        raw = p.get("sku") if p.get("sku") is not None else p.get("product_id")
        try:
            sku = int(raw)
        except (TypeError, ValueError):
            continue
        meta_by_sku[sku] = p

    out = []  # type: List[Dict[str, Any]]
    for item in list((package or {}).get("products") or []):
        if not isinstance(item, dict):
            continue
        try:
            sku = int(
                item.get("product_id")
                if item.get("product_id") is not None
                else item.get("sku")
            )
        except (TypeError, ValueError):
            continue
        try:
            qty = int(item.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        meta = meta_by_sku.get(sku) or {}
        out.append(
            {
                "sku": sku,
                "product_id": sku,
                "quantity": max(1, qty),
                "offer_id": str(meta.get("offer_id") or "").strip(),
                "name": str(meta.get("name") or "").strip(),
            }
        )
    if out:
        return out
    # Fallback: single unit from parent first product.
    products = parse_products(
        base.get("products")
        if isinstance(base.get("products"), list)
        else base.get("products_json")
    )
    if products:
        first = dict(products[0])
        first["quantity"] = 1
        return [first]
    return []


def _clear_package_stickers(
    db: Database, source_id: int, posting_numbers: List[str]
) -> None:
    """Clear package QR so refresh can rebind (web after-split)."""
    nums = [str(x).strip() for x in posting_numbers if str(x).strip()]
    if not nums:
        return
    placeholders = ", ".join("?" for _ in nums)
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE ozon_fbs_postings
            SET sticker_barcode = '', sticker_lower_barcode = ''
            WHERE source_id = ? AND posting_number IN ({})
            """.format(placeholders),
            [source_id] + nums,
        )
        conn.commit()


def _refresh_stickers_from_ozon(
    db: Database,
    client: OzonFbsClient,
    source_id: int,
    posting_numbers: List[str],
) -> None:
    """Overwrite sticker_* from get_posting barcodes after split."""
    for pn in posting_numbers:
        pn = str(pn or "").strip()
        if not pn:
            continue
        try:
            fresh = client.get_posting(pn)
        except Exception as exc:
            _log.warning("ozon split sticker refresh %s: %s", pn, exc)
            continue
        if not isinstance(fresh, dict):
            continue
        # Force awaiting_packaging — split must not assemble.
        fresh = dict(fresh)
        fresh["status"] = TAB_AWAITING_PACKAGING
        upsert_posting(db, source_id, fresh)
        # Ensure stickers overwrite even if upsert kept empty (already cleared).
        bc = fresh.get("barcodes") if isinstance(fresh.get("barcodes"), dict) else {}
        upper = str((bc or {}).get("upper_barcode") or "").strip()
        lower = str((bc or {}).get("lower_barcode") or "").strip()
        if not upper and not lower:
            continue
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE ozon_fbs_postings
                SET sticker_barcode = ?, sticker_lower_barcode = ?
                WHERE source_id = ? AND posting_number = ?
                """,
                (upper, lower, source_id, pn),
            )
            conn.commit()


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
        """Split multi-unit posting. Status stays ``awaiting_packaging`` (no assemble)."""
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
        parts = parse_split_parts(
            result, fallback_posting_number=pnum, split_plan=plan
        )
        posting_numbers = [pn for pn, _ in parts]

        # Persist each part as awaiting_packaging with correct qty=1 products.
        for pn, package in parts:
            products = _products_for_persist(package, base if isinstance(base, dict) else {})
            payload = dict(base) if isinstance(base, dict) else {}
            payload["posting_number"] = pn
            payload["status"] = TAB_AWAITING_PACKAGING
            if products:
                payload["products"] = products
            # Drop pre-split barcodes — refresh will rebind.
            if isinstance(payload.get("barcodes"), dict):
                payload["barcodes"] = {}
            upsert_posting(self.db, source_id, payload)

        _clear_package_stickers(self.db, source_id, posting_numbers)
        _refresh_stickers_from_ozon(self.db, client, source_id, posting_numbers)

        # Hard-set status/tab packaging (get may lag).
        with self.db.connect() as conn:
            placeholders = ", ".join("?" for _ in posting_numbers)
            conn.execute(
                """
                UPDATE ozon_fbs_postings
                SET status = ?, tab = ?
                WHERE source_id = ? AND posting_number IN ({})
                """.format(placeholders),
                [TAB_AWAITING_PACKAGING, TAB_AWAITING_PACKAGING, source_id]
                + list(posting_numbers),
            )
            conn.commit()

        return {
            "ok": True,
            "posting_number": posting_numbers[0] if posting_numbers else pnum,
            "posting_numbers": posting_numbers,
            "from_units": units,
            "to_count": len(posting_numbers),
            "result": result,
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
            message = "Разделено {} мультизаказов → «Ожидают сборки»".format(split_n)
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
