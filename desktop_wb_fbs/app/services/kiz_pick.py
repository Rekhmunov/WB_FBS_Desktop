# -*- coding: utf-8 -*-
"""Office KIZ mark (meta/sgtin) + local pick-verify — no CHZ."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.db import Database
from app.services.catalog import ProductService
from app.wb import (
    cancel_reason_label,
    compute_tab,
    is_cancelled_status,
    kiz_code_clean,
    parse_json_list,
    utc_now,
)
from app.wb.client import WbFbsClient

_GTIN_RE = re.compile(r"^01(\d{14})21")


def extract_gtin(cis: str) -> Optional[str]:
    text = kiz_code_clean(cis).replace("\u2194", "\u001d")
    m = _GTIN_RE.match(text)
    return m.group(1) if m else None


def gtin_matches_skus(gtin: str, skus: List[Any]) -> bool:
    g = str(gtin or "").strip()
    if not g:
        return False
    candidates = {g}
    if len(g) == 14 and g.startswith("0"):
        candidates.add(g[1:])
    if len(g) == 13:
        candidates.add("0" + g)
    for sku in skus or []:
        s = str(sku or "").strip()
        if s in candidates:
            return True
    return False


def _format_created(iso: object) -> str:
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


def _sticker_number(part_a: str, part_b: str) -> str:
    return "{}{}".format(str(part_a or "").strip(), str(part_b or "").strip())


class KizService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.products = ProductService(db)

    def marking_rows(self, source_id: int, supply_id: str, api_key: str) -> List[Dict[str, Any]]:
        """Orders in supply that require КИЗ (sgtin key present in WB meta)."""
        client = WbFbsClient(api_key)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM wb_fbs_orders
                WHERE source_id = ? AND supply_id = ?
                ORDER BY article COLLATE NOCASE, order_id
                """,
                (source_id, supply_id),
            ).fetchall()
        items = Database.rows_to_dicts(rows)
        if not items:
            return []
        ids = [int(r["order_id"]) for r in items]
        meta_list = client.get_orders_meta(ids)
        meta_by_id = {}  # type: Dict[int, Dict[str, Any]]
        for m in meta_list:
            try:
                meta_by_id[int(m.get("id"))] = m
            except (TypeError, ValueError):
                pass
        skip_map = self.products.skip_gtin_map()
        by_art = {}  # type: Dict[str, Dict[str, Any]]
        by_nm = {}  # type: Dict[str, Dict[str, Any]]
        for p in self.products.list_all():
            art = str(p.get("supplier_article") or "").strip().lower()
            nm = str(p.get("wb_nmid") or "").strip()
            if art:
                by_art[art] = p
            if nm:
                by_nm[nm] = p
        out = []  # type: List[Dict[str, Any]]
        for r in items:
            oid = int(r["order_id"])
            meta = meta_by_id.get(oid) or {}
            # WB: if sgtin key absent → order does not accept КИЗ
            if "sgtin" not in meta and "meta" in meta and isinstance(meta["meta"], dict):
                has_sgtin = "sgtin" in meta["meta"]
            else:
                has_sgtin = "sgtin" in meta
            # Also accept nested structure from some API versions
            if not has_sgtin:
                nested = meta.get("meta") if isinstance(meta.get("meta"), dict) else {}
                has_sgtin = "sgtin" in nested
            if not has_sgtin:
                # Fallback: if local already has codes, still show
                local_codes = parse_json_list(r.get("kiz_codes_json"))
                if not any(kiz_code_clean(c) for c in local_codes):
                    continue
            codes = parse_json_list(r.get("kiz_codes_json"))
            if not codes:
                # seed from WB meta if present
                wb_codes = meta.get("sgtin")
                if isinstance(wb_codes, list):
                    codes = [kiz_code_clean(x) for x in wb_codes if kiz_code_clean(x)]
                elif isinstance(meta.get("meta"), dict):
                    wb_codes = meta["meta"].get("sgtin")
                    if isinstance(wb_codes, list):
                        codes = [kiz_code_clean(x) for x in wb_codes if kiz_code_clean(x)]
            art = str(r.get("article") or "").strip().lower()
            nm = str(r.get("nm_id") or "").strip()
            skip = bool(skip_map.get(art) or skip_map.get(nm))
            local = by_art.get(art) or by_nm.get(nm) or {}
            product_name = str(local.get("name") or "").strip()
            product_photo = str(local.get("photo_path") or "").strip()
            has_codes = any(kiz_code_clean(c) for c in codes)
            kiz_status = "empty"
            if bool(int(r.get("kiz_wb_synced") or 0)) and has_codes:
                kiz_status = "ok"
            elif has_codes:
                kiz_status = "pending"
            out.append(
                {
                    "order_id": oid,
                    "article": r.get("article") or "",
                    "nm_id": r.get("nm_id"),
                    "product_name": product_name,
                    "product_photo": product_photo,
                    "brand": "",
                    "created_date": _format_created(r.get("created_at_wb")) or "—",
                    "sticker_part_a": "",
                    "sticker_part_b": "",
                    "sticker_number": "",
                    "skus": parse_json_list(r.get("skus_json")),
                    "kiz_codes": codes or [""],
                    "kiz_saved_at": r.get("kiz_saved_at"),
                    "kiz_wb_synced": bool(int(r.get("kiz_wb_synced") or 0)),
                    "kiz_status": kiz_status,
                    "kiz_decision": "",
                    "skip_kiz_gtin_check": skip,
                    "supplier_status": r.get("supplier_status"),
                    "wb_status": r.get("wb_status"),
                    "cancel_reason_label": cancel_reason_label(
                        supplier_status=r.get("supplier_status"),
                        wb_status=r.get("wb_status"),
                    ),
                }
            )
        return out

    def validate_mark(self, code: str, skus: List[Any], skip_gtin: bool) -> Tuple[bool, str]:
        cleaned = kiz_code_clean(code).replace("\u2194", "\u001d")
        if not cleaned:
            return False, "Пустой код"
        gtin = extract_gtin(cleaned)
        if not gtin:
            return False, "Не удалось выделить GTIN (AI 01) из кода КИЗ"
        if not skip_gtin and not gtin_matches_skus(gtin, skus):
            return False, "GTIN {} не совпадает со ШК заказа".format(gtin)
        return True, ""

    def save_local(
        self,
        source_id: int,
        order_id: int,
        codes: List[str],
        wb_synced: bool = False,
    ) -> None:
        cleaned = [kiz_code_clean(c) for c in codes if kiz_code_clean(c)]
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE wb_fbs_orders
                SET kiz_codes_json = ?, kiz_saved_at = ?, kiz_wb_synced = ?
                WHERE source_id = ? AND order_id = ?
                """,
                (
                    json.dumps(cleaned, ensure_ascii=False),
                    now,
                    1 if wb_synced else 0,
                    source_id,
                    order_id,
                ),
            )
            conn.commit()

    def save_to_wb(
        self,
        source_id: int,
        api_key: str,
        order_id: int,
        codes: List[str],
    ) -> None:
        cleaned = [kiz_code_clean(c).replace("\u2194", "\u001d") for c in codes if kiz_code_clean(c)]
        self.save_local(source_id, order_id, cleaned, wb_synced=False)
        client = WbFbsClient(api_key)
        if cleaned:
            client.set_order_sgtin(order_id, cleaned)
        else:
            client.delete_order_meta(order_id, "sgtin")
        self.save_local(source_id, order_id, cleaned, wb_synced=True)

    def refresh_statuses(
        self, source_id: int, api_key: str, supply_id: str
    ) -> Dict[str, Any]:
        """Refresh WB order statuses for supply + re-detect cancelled / KIZ needed."""
        client = WbFbsClient(api_key)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT order_id FROM wb_fbs_orders
                WHERE source_id = ? AND supply_id = ?
                """,
                (source_id, supply_id),
            ).fetchall()
        ids = [int(r["order_id"]) for r in rows]
        if not ids:
            return {"updated": 0, "cancelled": 0}
        updated = 0
        cancelled = 0
        now = utc_now()
        for i in range(0, len(ids), 1000):
            chunk = ids[i : i + 1000]
            statuses = client.get_statuses(chunk)
            with self.db.connect() as conn:
                for st in statuses:
                    if not isinstance(st, dict) or st.get("id") is None:
                        continue
                    try:
                        oid = int(st["id"])
                    except (TypeError, ValueError):
                        continue
                    ss = str(st.get("supplierStatus") or "")
                    ws = str(st.get("wbStatus") or "")
                    tab = compute_tab(supplier_status=ss, wb_status=ws, is_archive=False)
                    if is_cancelled_status(supplier_status=ss, wb_status=ws):
                        cancelled += 1
                    conn.execute(
                        """
                        UPDATE wb_fbs_orders
                        SET supplier_status = ?, wb_status = ?, tab = ?, synced_at = ?
                        WHERE source_id = ? AND order_id = ?
                        """,
                        (ss, ws, tab, now, source_id, oid),
                    )
                    updated += 1
                conn.commit()
            if i + 1000 < len(ids):
                time.sleep(0.21)
        return {"updated": updated, "cancelled": cancelled}


class PickVerifyService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def rows(self, source_id: int, supply_id: str, api_key: str) -> List[Dict[str, Any]]:
        """Orders WITHOUT КИЗ requirement."""
        kiz = KizService(self.db)
        # Reuse meta: those without sgtin key
        client = WbFbsClient(api_key)
        with self.db.connect() as conn:
            all_rows = conn.execute(
                """
                SELECT * FROM wb_fbs_orders
                WHERE source_id = ? AND supply_id = ?
                ORDER BY article COLLATE NOCASE, order_id
                """,
                (source_id, supply_id),
            ).fetchall()
        items = Database.rows_to_dicts(all_rows)
        if not items:
            return []
        ids = [int(r["order_id"]) for r in items]
        meta_list = client.get_orders_meta(ids)
        meta_by_id = {}  # type: Dict[int, Dict[str, Any]]
        for m in meta_list:
            try:
                meta_by_id[int(m.get("id"))] = m
            except (TypeError, ValueError):
                pass
        out = []  # type: List[Dict[str, Any]]
        for r in items:
            oid = int(r["order_id"])
            meta = meta_by_id.get(oid) or {}
            has_sgtin = "sgtin" in meta
            nested = meta.get("meta") if isinstance(meta.get("meta"), dict) else {}
            if not has_sgtin:
                has_sgtin = "sgtin" in nested
            if has_sgtin:
                continue
            out.append(
                {
                    "order_id": oid,
                    "article": r.get("article") or "",
                    "skus": parse_json_list(r.get("skus_json")),
                    "pick_verified": bool(int(r.get("pick_verified") or 0)),
                    "pick_barcode": str(r.get("pick_barcode") or ""),
                    "pick_verified_at": r.get("pick_verified_at"),
                }
            )
        return out

    def validate_barcode(self, barcode: str, skus: List[Any]) -> Tuple[bool, str]:
        code = str(barcode or "").strip()
        if not code:
            return False, "Пустой ШК"
        sku_set = {str(s).strip() for s in (skus or []) if str(s).strip()}
        if code in sku_set:
            return True, ""
        # 13↔14 leading zero
        if len(code) == 13 and ("0" + code) in sku_set:
            return True, ""
        if len(code) == 14 and code.startswith("0") and code[1:] in sku_set:
            return True, ""
        return False, "ШК не совпадает с заказом"

    def save(
        self,
        source_id: int,
        order_id: int,
        verified: bool,
        barcode: str = "",
    ) -> None:
        now = utc_now() if verified else None
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE wb_fbs_orders
                SET pick_verified = ?, pick_barcode = ?, pick_verified_at = ?
                WHERE source_id = ? AND order_id = ?
                """,
                (
                    1 if verified else 0,
                    str(barcode or "").strip() if verified else "",
                    now,
                    source_id,
                    order_id,
                ),
            )
            conn.commit()
