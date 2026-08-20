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


def row_matches_modal_search(row: Dict[str, Any], query: str) -> bool:
    """KIZ / pick modal search — same fields as supply detail search.

    Matches order id, sticker parts/number/barcode, product name, article,
    brand, nm_id, product SKUs/barcodes, pick barcode, and cancel label.
    """
    q = str(query or "").strip().lower()
    if not q:
        return True
    skus = row.get("skus")
    if skus is None:
        skus = row.get("barcodes")
    if isinstance(skus, str):
        skus = parse_json_list(skus)
    elif not isinstance(skus, (list, tuple)):
        skus = []
    hay = [
        row.get("order_id"),
        row.get("article"),
        row.get("product_name"),
        row.get("title"),
        row.get("name"),
        row.get("brand"),
        row.get("nm_id"),
        row.get("sticker_number"),
        row.get("sticker_part_a"),
        row.get("sticker_part_b"),
        row.get("sticker_barcode"),
        row.get("pick_barcode"),
        row.get("cancel_reason_label"),
        *list(skus),
    ]
    return any(q in str(v or "").strip().lower() for v in hay)

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


def pending_wb_save_jobs(
    rows: List[Dict[str, Any]],
    *,
    row_errors: Optional[Dict[int, str]] = None,
    only_order_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """Build WB save jobs for orders that still need upload.

    Skips already synced rows. When ``only_order_ids`` is set, only those
    orders are considered (retry-failed flow).
    """
    errors = row_errors or {}
    only = set(int(x) for x in only_order_ids) if only_order_ids is not None else None
    jobs = []  # type: List[Dict[str, Any]]
    for row in rows or []:
        oid = int(row.get("order_id") or 0)
        if not oid:
            continue
        if only is not None and oid not in only:
            continue
        codes = [
            str(c).strip(" \t\r\n")
            for c in (row.get("kiz_codes") or [])
            if str(c).strip(" \t\r\n")
        ]
        if not codes:
            continue
        synced = bool(row.get("kiz_wb_synced"))
        status = str(row.get("kiz_status") or "")
        has_error = oid in errors or status == "error"
        if synced and not has_error and status != "pending":
            continue
        jobs.append(
            {
                "order_id": oid,
                "codes": codes,
                "skus": list(row.get("skus") or []),
                "skip_kiz_gtin_check": bool(row.get("skip_kiz_gtin_check")),
            }
        )
    return jobs


def _int_or_zero(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _kiz_codes_from_value(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return _kiz_codes_from_value(value.get("value"))
    if isinstance(value, list):
        return [kiz_code_clean(x) for x in value if kiz_code_clean(x)]
    text = kiz_code_clean(value)
    return [text] if text else []


def _kiz_decision_raw(item: Dict[str, Any]) -> str:
    """Read WB validation flag from metaDetails (field names vary slightly)."""
    for key in ("decision", "status", "validationStatus", "state"):
        val = item.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return ""


def kiz_status_from_decision(decision: str, codes: List[str]) -> str:
    """UI status: empty | pending | ok | error — web ``_kiz_status_from_decision``."""
    dec = str(decision or "").strip().lower().replace("-", "_")
    if not dec and not codes:
        return "empty"
    error_exact = {
        "invalid",
        "sgtininvalid",
        "sgtin_invalid",
        "sgtininvalidformat",
        "sgtin_invalid_format",
        "sgtinnotfound",
        "sgtin_not_found",
        "notfound",
        "sgtinretired",
        "sgtin_retired",
        "sgtinwithdrawn",
        "sgtin_withdrawn",
        "sgtinwrittenoff",
        "sgtin_written_off",
        "sgtinemitted",
        "sgtin_emitted",
        "sgtinapplied",
        "sgtin_applied",
        "sgtindisaggregated",
        "sgtin_disaggregated",
        "error",
        "failed",
        "fail",
        "rejected",
        "reject",
        "ошибка",
    }
    if (
        dec in error_exact
        or "invalid" in dec
        or "notfound" in dec
        or "not_found" in dec
        or "retired" in dec
        or "withdrawn" in dec
        or "writtenoff" in dec
        or "written_off" in dec
        or "disaggregat" in dec
        or ("error" in dec and "sgtin" in dec)
        or "fail" in dec
    ):
        return "error"
    if dec in {
        "filled",
        "sgtinintroduced",
        "sgtin_introduced",
        "introduced",
        "ok",
        "valid",
        "success",
        "passed",
        "approved",
    } or "introduced" in dec:
        return "ok"
    if dec in {"optional", "required"} and not codes:
        return "empty"
    if dec in {"pending", "deadlineexceeded", "deadline_exceeded"}:
        return "pending" if codes else "empty"
    if codes:
        if dec.startswith("sgtin"):
            return "error"
        return "pending"
    return "empty"


def kiz_from_meta_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Parse POST /orders/meta row for sgtin slot + verification decision."""
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    details = row.get("metaDetails") if isinstance(row.get("metaDetails"), list) else []
    required = False
    codes = []  # type: List[str]
    decision = ""
    for item in details:
        if not isinstance(item, dict):
            continue
        if str(item.get("key") or "").strip().lower() != "sgtin":
            continue
        required = True
        codes = _kiz_codes_from_value(item.get("value"))
        decision = _kiz_decision_raw(item)
        break
    if not required and "sgtin" in meta:
        required = True
        codes = _kiz_codes_from_value(meta.get("sgtin"))
    status = kiz_status_from_decision(decision, codes) if required else "empty"
    return {
        "kiz_required": required,
        "kiz_bound": bool(codes),
        "kiz_codes": codes,
        "kiz_decision": decision,
        "kiz_status": status,
    }


def summarize_kiz_check_status(statuses: List[str]) -> str:
    """Aggregate tone for supply-detail «Маркировка» refresh (web parity).

    Returns:
      ``ok`` — every filled code approved → green;
      ``error`` — any filled code failed → red;
      ``pending`` — mix / still checking without errors → default;
      ``none`` — no filled КИЗ to check → default.
    """
    cleaned = [
        str(s or "").strip().lower()
        for s in (statuses or [])
        if str(s or "").strip()
    ]
    cleaned = [s for s in cleaned if s != "empty"]
    if not cleaned:
        return "none"
    if any(s == "error" for s in cleaned):
        return "error"
    if all(s == "ok" for s in cleaned):
        return "ok"
    return "pending"
    return jobs


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


def _created_ago(iso: object) -> str:
    from app.ui.format_helpers import ago_label

    return ago_label(iso)


def _sticker_number(part_a: str, part_b: str) -> str:
    return "{}{}".format(str(part_a or "").strip(), str(part_b or "").strip())


class KizService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.products = ProductService(db)

    def check_supply_status(
        self, source_id: int, supply_id: str, api_key: str
    ) -> Dict[str, Any]:
        """Live meta check for КИЗ next to «Маркировка» — web ``check_supply_kiz_status``.

        Tone uses only filled codes on non-cancelled orders. Meta failures raise
        (do not paint a false green/default from local fallbacks).
        """
        sid = str(supply_id or "").strip()
        if not sid:
            raise ValueError("Укажите supply_id")
        if not api_key:
            raise ValueError("Нет API-ключа источника")

        client = WbFbsClient(api_key)
        order_ids = []  # type: List[int]
        try:
            for item in client.get_supply_order_ids(sid) or []:
                oid = _int_or_zero(item)
                if oid > 0:
                    order_ids.append(oid)
        except Exception:
            order_ids = []

        local_by_id = {}  # type: Dict[int, Dict[str, Any]]
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM wb_fbs_orders
                WHERE source_id = ? AND supply_id = ?
                ORDER BY article COLLATE NOCASE, order_id
                """,
                (source_id, sid),
            ).fetchall()
        for r in Database.rows_to_dicts(rows):
            oid = _int_or_zero(r.get("order_id"))
            if oid:
                local_by_id[oid] = r

        if not order_ids:
            order_ids = list(local_by_id.keys())

        seen = set()  # type: set
        unique_ids = []  # type: List[int]
        for oid in order_ids:
            if oid in seen:
                continue
            seen.add(oid)
            unique_ids.append(oid)
        order_ids = unique_ids

        cancelled_ids = set()  # type: set
        cancel_labels = {}  # type: Dict[int, str]
        for oid, r in local_by_id.items():
            label = cancel_reason_label(
                supplier_status=r.get("supplier_status"),
                wb_status=r.get("wb_status"),
            )
            if label or is_cancelled_status(
                supplier_status=r.get("supplier_status"),
                wb_status=r.get("wb_status"),
            ):
                cancelled_ids.add(oid)
                cancel_labels[oid] = label or "Отменен"

        persist_cancel = {}  # type: Dict[int, Tuple[str, str]]
        if order_ids:
            try:
                live_statuses = client.get_statuses(order_ids)
            except Exception:
                live_statuses = []
            for st in live_statuses:
                if not isinstance(st, dict):
                    continue
                oid = _int_or_zero(st.get("id") or st.get("orderId"))
                if oid <= 0:
                    continue
                ss = str(st.get("supplierStatus") or "").strip()
                ws = str(st.get("wbStatus") or "").strip()
                label = cancel_reason_label(supplier_status=ss, wb_status=ws)
                if label or is_cancelled_status(supplier_status=ss, wb_status=ws):
                    cancelled_ids.add(oid)
                    cancel_labels[oid] = label or cancel_labels.get(oid) or "Отменен"
                    if ss or ws:
                        persist_cancel[oid] = (ss, ws)
        if persist_cancel:
            now = utc_now()
            with self.db.connect() as conn:
                for oid, (ss, ws) in persist_cancel.items():
                    tab = compute_tab(
                        supplier_status=ss, wb_status=ws, is_archive=False
                    )
                    conn.execute(
                        """
                        UPDATE wb_fbs_orders
                        SET supplier_status = ?, wb_status = ?, tab = ?,
                            updated_at = ?
                        WHERE source_id = ? AND order_id = ?
                        """,
                        (ss, ws, tab, now, source_id, oid),
                    )
                conn.commit()

        kiz_map = {
            oid: {
                "kiz_required": False,
                "kiz_bound": False,
                "kiz_codes": [],
                "kiz_decision": "",
                "kiz_status": "empty",
            }
            for oid in order_ids
        }  # type: Dict[int, Dict[str, Any]]
        meta_by_id_live = {}  # type: Dict[int, Dict[str, Any]]
        active_ids = [oid for oid in order_ids if oid not in cancelled_ids]
        if active_ids:
            try:
                meta_rows = client.get_orders_meta(active_ids)
            except Exception as exc:
                raise RuntimeError(
                    "Не удалось проверить КИЗ на Wildberries: {}".format(exc)
                ) from exc
            if not isinstance(meta_rows, list):
                raise RuntimeError("Некорректный ответ Wildberries при проверке КИЗ")
            seen_meta = set()  # type: set
            for row in meta_rows:
                if not isinstance(row, dict):
                    continue
                oid = _int_or_zero(row.get("id") or row.get("orderId"))
                if oid <= 0 or oid not in kiz_map or oid in cancelled_ids:
                    continue
                kiz_map[oid] = kiz_from_meta_row(row)
                meta_by_id_live[oid] = row
                seen_meta.add(oid)
            if not seen_meta:
                raise RuntimeError("Wildberries не вернул статусы КИЗ")
            missing = [oid for oid in active_ids if oid not in seen_meta]
            if missing:
                raise RuntimeError(
                    "Wildberries не вернул статусы КИЗ для {} заказ(ов)".format(
                        len(missing)
                    )
                )

        out_rows = []  # type: List[Dict[str, Any]]
        checked_statuses = []  # type: List[str]
        persist_codes = []  # type: List[Tuple[int, List[str], bool]]
        for oid in order_ids:
            kiz = kiz_map.get(oid) or {}
            is_cancelled = oid in cancelled_ids
            meta_codes = [
                kiz_code_clean(x)
                for x in (kiz.get("kiz_codes") or [])
                if kiz_code_clean(x)
            ]
            local = local_by_id.get(oid) or {}
            local_codes = [
                kiz_code_clean(x)
                for x in parse_json_list(local.get("kiz_codes_json"))
                if kiz_code_clean(x)
            ]
            codes = meta_codes or local_codes
            has_filled = bool(codes)
            status = str(kiz.get("kiz_status") or "empty")
            if is_cancelled:
                status = "empty"
                kiz_required = False
            else:
                kiz_required = bool(kiz.get("kiz_required"))
                if not has_filled:
                    status = "empty"
            # Portal-filled codes must survive reopen: write them to SQLite.
            # Only persist when live meta actually returned codes (source of truth).
            if meta_codes and not is_cancelled:
                local_synced = bool(int(local.get("kiz_wb_synced") or 0))
                wb_synced = status == "ok" or local_synced
                if codes != local_codes or bool(wb_synced) != local_synced:
                    persist_codes.append((oid, list(meta_codes), wb_synced))
            out_rows.append(
                {
                    "order_id": oid,
                    "kiz_required": kiz_required,
                    "kiz_bound": has_filled,
                    "kiz_codes": codes,
                    "kiz_decision": str(kiz.get("kiz_decision") or ""),
                    "kiz_status": status,
                    "cancelled": is_cancelled,
                    "cancel_reason_label": cancel_labels.get(oid, ""),
                    "kiz_wb_synced": status == "ok",
                    "kiz_error": status == "error",
                }
            )
            if has_filled and not is_cancelled:
                checked_statuses.append(status)

        if persist_codes:
            now = utc_now()
            with self.db.connect() as conn:
                for oid, codes, wb_synced in persist_codes:
                    conn.execute(
                        """
                        UPDATE wb_fbs_orders
                        SET kiz_codes_json = ?, kiz_saved_at = ?, kiz_wb_synced = ?
                        WHERE source_id = ? AND order_id = ?
                        """,
                        (
                            json.dumps(codes, ensure_ascii=False),
                            now,
                            1 if wb_synced else 0,
                            source_id,
                            oid,
                        ),
                    )
                conn.commit()

        # Refresh open-cache meta so reopen does not keep a stale empty sgtin.
        if meta_by_id_live:
            try:
                from app.services import order_open_cache

                order_open_cache.upsert_meta(
                    self.db,
                    source_id,
                    meta_by_id_live,
                    order_ids=list(meta_by_id_live.keys()),
                )
            except Exception:
                pass

        tone = summarize_kiz_check_status(checked_statuses)
        counts = {
            "checked": len(checked_statuses),
            "required": sum(1 for r in out_rows if r.get("kiz_required")),
            "ok": sum(1 for s in checked_statuses if s == "ok"),
            "error": sum(1 for s in checked_statuses if s == "error"),
            "pending": sum(1 for s in checked_statuses if s == "pending"),
            "empty": sum(1 for r in out_rows if not r.get("kiz_bound")),
            "cancelled": len(cancelled_ids),
            "cancelled_with_kiz": sum(
                1 for r in out_rows if r.get("cancelled") and r.get("kiz_bound")
            ),
        }
        return {
            "ok": True,
            "supply_id": sid,
            "source_id": int(source_id),
            "status": tone,
            "counts": counts,
            "orders": out_rows,
        }

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
                    "created_ago": _created_ago(r.get("created_at_wb")),
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
        products = ProductService(self.db)
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
        by_art = {}  # type: Dict[str, Dict[str, Any]]
        by_nm = {}  # type: Dict[str, Dict[str, Any]]
        for p in products.list_all():
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
            has_sgtin = "sgtin" in meta
            nested = meta.get("meta") if isinstance(meta.get("meta"), dict) else {}
            if not has_sgtin:
                has_sgtin = "sgtin" in nested
            local_codes = parse_json_list(r.get("kiz_codes_json"))
            if has_sgtin or any(kiz_code_clean(c) for c in local_codes):
                continue
            art = str(r.get("article") or "").strip().lower()
            nm = str(r.get("nm_id") or "").strip()
            local = by_art.get(art) or by_nm.get(nm) or {}
            out.append(
                {
                    "order_id": oid,
                    "article": r.get("article") or "",
                    "nm_id": r.get("nm_id"),
                    "product_name": str(local.get("name") or "").strip(),
                    "product_photo": str(local.get("photo_path") or "").strip(),
                    "brand": "",
                    "created_date": _format_created(r.get("created_at_wb")) or "—",
                    "created_ago": _created_ago(r.get("created_at_wb")),
                    "sticker_part_a": "",
                    "sticker_part_b": "",
                    "sticker_number": "",
                    "skus": parse_json_list(r.get("skus_json")),
                    "pick_verified": bool(int(r.get("pick_verified") or 0)),
                    "pick_barcode": str(r.get("pick_barcode") or ""),
                    "pick_verified_at": r.get("pick_verified_at"),
                    "supplier_status": r.get("supplier_status"),
                    "wb_status": r.get("wb_status"),
                    "cancel_reason_label": cancel_reason_label(
                        supplier_status=r.get("supplier_status"),
                        wb_status=r.get("wb_status"),
                    ),
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
