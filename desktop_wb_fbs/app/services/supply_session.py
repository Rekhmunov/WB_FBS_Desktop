# -*- coding: utf-8 -*-
"""Per-supply preload session: one slow open, fast secondary dialogs."""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.db import Database
from app.services.orders import OrdersService
from app.wb import kiz_code_clean, parse_json_list
from app.wb.client import WbFbsClient

_SESSION_TTL_SEC = 300.0
_lock = threading.Lock()
_sessions = {}  # type: Dict[Tuple[int, str], Tuple[float, "SupplySession"]]


def _key(source_id: int, supply_id: str) -> Tuple[int, str]:
    return (int(source_id), str(supply_id or "").strip())


def get_session(source_id: int, supply_id: str) -> Optional["SupplySession"]:
    key = _key(source_id, supply_id)
    with _lock:
        item = _sessions.get(key)
        if not item:
            return None
        ts, session = item
        if (time.monotonic() - ts) > _SESSION_TTL_SEC:
            _sessions.pop(key, None)
            return None
        return session


def put_session(session: "SupplySession") -> None:
    if not session or not session.supply_id:
        return
    with _lock:
        _sessions[_key(session.source_id, session.supply_id)] = (
            time.monotonic(),
            session,
        )


def invalidate(source_id: int, supply_id: str) -> None:
    with _lock:
        _sessions.pop(_key(source_id, supply_id), None)


def _has_sgtin(meta: Dict[str, Any]) -> bool:
    if "sgtin" in meta:
        return True
    nested = meta.get("meta") if isinstance(meta.get("meta"), dict) else {}
    return "sgtin" in nested


def _meta_sgtin_codes(meta: Dict[str, Any]) -> List[str]:
    wb_codes = meta.get("sgtin")
    if isinstance(wb_codes, list):
        return [kiz_code_clean(x) for x in wb_codes if kiz_code_clean(x)]
    nested = meta.get("meta") if isinstance(meta.get("meta"), dict) else {}
    wb_codes = nested.get("sgtin") if isinstance(nested, dict) else None
    if isinstance(wb_codes, list):
        return [kiz_code_clean(x) for x in wb_codes if kiz_code_clean(x)]
    return []


def fetch_orders_meta(
    api_key: str, order_ids: List[int]
) -> Dict[int, Dict[str, Any]]:
    ids = [int(x) for x in order_ids if x is not None]
    if not ids or not api_key:
        return {}
    client = WbFbsClient(api_key)
    out = {}  # type: Dict[int, Dict[str, Any]]
    try:
        meta_list = client.get_orders_meta(ids)
    except Exception:
        return {}
    for m in meta_list or []:
        if not isinstance(m, dict):
            continue
        try:
            oid = int(m.get("id"))
        except (TypeError, ValueError):
            continue
        out[oid] = m
    return out


class SupplySession:
    """Cached payload for one open supply detail."""

    def __init__(
        self,
        source_id: int,
        supply_id: str,
        api_key: str,
    ) -> None:
        self.source_id = int(source_id)
        self.supply_id = str(supply_id)
        self.api_key = str(api_key or "")
        self.rows = []  # type: List[Dict[str, Any]]
        self.warehouse = ""
        self.sticker_numbers = {}  # type: Dict[int, Dict[str, Any]]
        self.sticker_png = {}  # type: Dict[int, Dict[str, Any]]
        self.meta_by_id = {}  # type: Dict[int, Dict[str, Any]]
        self.kiz_rows = []  # type: List[Dict[str, Any]]
        self.pick_rows = []  # type: List[Dict[str, Any]]
        self.core_ready = False
        self.png_ready = False
        self.error = ""

    def apply_sticker_numbers_to_rows(self) -> None:
        for r in self.rows:
            oid = int(r.get("order_id") or 0)
            st = self.sticker_numbers.get(oid) or {}
            part_a = str(st.get("partA") or "").strip()
            part_b = str(st.get("partB") or "").strip()
            r["sticker_part_a"] = part_a
            r["sticker_part_b"] = part_b
            r["sticker_number"] = "{}{}".format(part_a, part_b)

    def build_kiz_and_pick_rows(self, db: Database) -> None:
        from app.services.catalog import ProductService
        from app.wb import cancel_reason_label

        products = ProductService(db)
        skip_map = products.skip_gtin_map()
        by_art = {}  # type: Dict[str, Dict[str, Any]]
        by_nm = {}  # type: Dict[str, Dict[str, Any]]
        for p in products.list_all():
            art = str(p.get("supplier_article") or "").strip().lower()
            nm = str(p.get("wb_nmid") or "").strip()
            if art:
                by_art[art] = p
            if nm:
                by_nm[nm] = p

        kiz_out = []  # type: List[Dict[str, Any]]
        pick_out = []  # type: List[Dict[str, Any]]
        for r in self.rows:
            oid = int(r["order_id"])
            meta = self.meta_by_id.get(oid) or {}
            has_sgtin = _has_sgtin(meta)
            codes = parse_json_list(r.get("kiz_codes_json"))
            if not isinstance(r.get("kiz_codes"), list):
                codes = parse_json_list(r.get("kiz_codes_json"))
            else:
                codes = list(r.get("kiz_codes") or [])
            if not any(kiz_code_clean(c) for c in codes):
                seeded = _meta_sgtin_codes(meta)
                if seeded:
                    codes = seeded
            has_codes = any(kiz_code_clean(c) for c in codes)

            if has_sgtin or has_codes:
                art = str(r.get("article") or "").strip().lower()
                nm = str(r.get("nm_id") or "").strip()
                local = by_art.get(art) or by_nm.get(nm) or {}
                kiz_status = "empty"
                if bool(int(r.get("kiz_wb_synced") or 0)) and has_codes:
                    kiz_status = "ok"
                elif has_codes:
                    kiz_status = "pending"
                part_a = str(r.get("sticker_part_a") or "")
                part_b = str(r.get("sticker_part_b") or "")
                kiz_out.append(
                    {
                        "order_id": oid,
                        "article": r.get("article") or "",
                        "nm_id": r.get("nm_id"),
                        "product_name": str(local.get("name") or r.get("product_name") or "").strip(),
                        "product_photo": str(
                            local.get("photo_path") or r.get("product_photo") or ""
                        ).strip(),
                        "brand": "",
                        "created_date": r.get("created_date") or "—",
                        "sticker_part_a": part_a,
                        "sticker_part_b": part_b,
                        "sticker_number": r.get("sticker_number") or "",
                        "skus": list(r.get("skus") or parse_json_list(r.get("skus_json"))),
                        "kiz_codes": codes or [""],
                        "kiz_saved_at": r.get("kiz_saved_at"),
                        "kiz_wb_synced": bool(int(r.get("kiz_wb_synced") or 0)),
                        "kiz_status": kiz_status,
                        "kiz_decision": "",
                        "skip_kiz_gtin_check": bool(skip_map.get(art) or skip_map.get(nm)),
                        "supplier_status": r.get("supplier_status"),
                        "wb_status": r.get("wb_status"),
                        "cancel_reason_label": cancel_reason_label(
                            supplier_status=r.get("supplier_status"),
                            wb_status=r.get("wb_status"),
                        ),
                    }
                )
                # Keep detail-row kiz flags in sync with meta.
                r["kiz_required"] = True
                r["kiz_codes"] = codes
                r["kiz_status"] = kiz_status
            else:
                r["kiz_required"] = False
                r["kiz_status"] = "empty"
                pick_out.append(
                    {
                        "order_id": oid,
                        "article": r.get("article") or "",
                        "skus": list(r.get("skus") or parse_json_list(r.get("skus_json"))),
                        "pick_verified": bool(int(r.get("pick_verified") or 0)),
                        "pick_barcode": str(r.get("pick_barcode") or ""),
                        "pick_verified_at": r.get("pick_verified_at"),
                        "sticker_part_a": r.get("sticker_part_a") or "",
                        "sticker_part_b": r.get("sticker_part_b") or "",
                        "sticker_number": r.get("sticker_number") or "",
                    }
                )
        self.kiz_rows = kiz_out
        self.pick_rows = pick_out


ProgressCb = Callable[[str], None]


def preload_supply_core(
    db: Database,
    orders: OrdersService,
    source_id: int,
    supply_id: str,
    api_key: str,
    supply_pickup_allowed: bool = False,
    progress: Optional[ProgressCb] = None,
) -> SupplySession:
    """Load orders + sticker numbers + meta. Safe to call off UI thread."""
    from app.ui.format_helpers import ago_label, format_date_short
    from app.services.print_docs import _fetch_picking_stickers

    def _prog(msg: str) -> None:
        if progress:
            progress(msg)

    session = SupplySession(source_id, supply_id, api_key)
    _prog("Загрузка заказов…")
    rows = orders.orders_in_supply(source_id, supply_id, api_key=api_key)
    for r in rows:
        r["created_date"] = format_date_short(r.get("created_at_wb"))
        r["created_ago"] = ago_label(r.get("created_at_wb"))
        r["pickup_allowed"] = bool(r.get("pickup_allowed") or supply_pickup_allowed)
        codes = r.get("kiz_codes")
        if not isinstance(codes, list):
            codes = parse_json_list(r.get("kiz_codes_json"))
        r["kiz_codes"] = codes
    session.rows = rows
    if rows:
        session.warehouse = str(
            rows[0].get("warehouse_label") or rows[0].get("warehouse_id") or ""
        )

    ids = [int(r["order_id"]) for r in rows if r.get("order_id") is not None]
    _prog("Загрузка номеров стикеров… ({})".format(len(ids)))
    stickers = {}  # type: Dict[int, Dict[str, Any]]
    if ids and api_key:
        try:
            stickers = _fetch_picking_stickers(api_key, ids)
        except Exception:
            stickers = {}
    session.sticker_numbers = {
        oid: {
            "partA": str((st or {}).get("partA") or ""),
            "partB": str((st or {}).get("partB") or ""),
            "file_b64": "",
        }
        for oid, st in stickers.items()
    }
    session.apply_sticker_numbers_to_rows()

    _prog("Определение КИЗ и проверки ШК… ({})".format(len(ids)))
    if ids and api_key:
        try:
            session.meta_by_id = fetch_orders_meta(api_key, ids)
        except Exception:
            session.meta_by_id = {}
    session.build_kiz_and_pick_rows(db)
    session.core_ready = True
    put_session(session)
    return session


def preload_sticker_pngs(
    session: SupplySession,
    progress: Optional[ProgressCb] = None,
) -> None:
    """Heavy PNG stickers for print — after core UI is ready."""
    from app.services.print_docs import fetch_stickers_map

    ids = [int(r["order_id"]) for r in session.rows if r.get("order_id") is not None]
    if not ids or not session.api_key:
        session.png_ready = True
        put_session(session)
        return
    if progress:
        progress("Подготовка стикеров для печати… ({})".format(len(ids)))
    try:
        session.sticker_png = fetch_stickers_map(
            session.api_key, ids, sticker_type="png", keep_files=True
        )
    except Exception:
        session.sticker_png = {}
    session.png_ready = True
    put_session(session)


def snapshot_for_ui(session: SupplySession) -> Dict[str, Any]:
    """Shallow-safe payload for UI thread."""
    return {
        "rows": copy.deepcopy(session.rows),
        "warehouse": session.warehouse,
        "kiz_rows": copy.deepcopy(session.kiz_rows),
        "pick_rows": copy.deepcopy(session.pick_rows),
        "sticker_numbers": copy.deepcopy(session.sticker_numbers),
        "core_ready": session.core_ready,
        "png_ready": session.png_ready,
    }
