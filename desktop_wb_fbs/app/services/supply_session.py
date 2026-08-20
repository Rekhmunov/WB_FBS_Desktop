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
        self.sticker_png_count = 0
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
                art = str(r.get("article") or "").strip().lower()
                nm = str(r.get("nm_id") or "").strip()
                local = by_art.get(art) or by_nm.get(nm) or {}
                pick_out.append(
                    {
                        "order_id": oid,
                        "article": r.get("article") or "",
                        "nm_id": r.get("nm_id"),
                        "product_name": str(
                            local.get("name") or r.get("product_name") or ""
                        ).strip(),
                        "product_photo": str(
                            local.get("photo_path") or r.get("product_photo") or ""
                        ).strip(),
                        "brand": "",
                        "created_date": r.get("created_date") or "—",
                        "skus": list(
                            r.get("skus") or parse_json_list(r.get("skus_json"))
                        ),
                        "pick_verified": bool(int(r.get("pick_verified") or 0)),
                        "pick_barcode": str(r.get("pick_barcode") or ""),
                        "pick_verified_at": r.get("pick_verified_at"),
                        "sticker_part_a": r.get("sticker_part_a") or "",
                        "sticker_part_b": r.get("sticker_part_b") or "",
                        "sticker_number": r.get("sticker_number") or "",
                        "supplier_status": r.get("supplier_status"),
                        "wb_status": r.get("wb_status"),
                        "cancel_reason_label": cancel_reason_label(
                            supplier_status=r.get("supplier_status"),
                            wb_status=r.get("wb_status"),
                        ),
                    }
                )
        self.kiz_rows = kiz_out
        self.pick_rows = pick_out


ProgressCb = Callable[[int, str], None]


def _progress_detail(done: int, total: int, *, fallback: str = "") -> str:
    if total > 0:
        return "{} из {}".format(int(done), int(total))
    return str(fallback or "")


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

    def _prog(step: int, detail: str = "") -> None:
        if progress:
            progress(step, detail)

    session = SupplySession(source_id, supply_id, api_key)
    _prog(1, "из локальной базы")
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
    order_total = len(ids)

    def _sticker_progress(done: int, total: int) -> None:
        _prog(2, _progress_detail(done, order_total or total))

    stickers = {}  # type: Dict[int, Dict[str, Any]]
    if ids and api_key:
        try:
            stickers = _fetch_picking_stickers(
                api_key, ids, progress=_sticker_progress
            )
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

    _prog(3, _progress_detail(order_total, order_total, fallback="метаданные"))
    if ids and api_key:
        try:
            session.meta_by_id = fetch_orders_meta(api_key, ids)
        except Exception:
            session.meta_by_id = {}
    session.build_kiz_and_pick_rows(db)
    session.core_ready = True
    session.png_ready = False
    session.sticker_png = {}
    session.sticker_png_count = 0
    put_session(session)
    return session


def preload_sticker_pngs(
    session: SupplySession,
    progress: Optional[ProgressCb] = None,
) -> None:
    """Fetch PNG stickers for print — resume from disk, tiny chunks, no base64 keep."""
    import gc

    from app.diag_log import exception as diag_exception
    from app.diag_log import write as diag_write
    from app.services.print_docs import (
        _cache_merge_stickers,
        _cache_sticker_count,
        _stickers_cache_key,
        fetch_stickers_map,
    )
    from app.services.sticker_file_cache import existing_sticker_paths

    ids = [int(r["order_id"]) for r in session.rows if r.get("order_id") is not None]
    order_total = len(ids)
    diag_write(
        "supply.png_preload.begin",
        sync=True,
        supply_id=session.supply_id,
        source_id=session.source_id,
        order_total=order_total,
    )

    def _png_progress(done: int, total: int) -> None:
        if progress:
            progress(4, _progress_detail(done, order_total or total))

    session.sticker_png = {}
    session.sticker_png_count = 0
    if progress:
        progress(4, _progress_detail(0, order_total))
    if not ids or not session.api_key:
        session.png_ready = True
        put_session(session)
        diag_write(
            "supply.png_preload.skip",
            sync=True,
            supply_id=session.supply_id,
            reason="no_ids_or_api_key",
        )
        return

    cache_key = _stickers_cache_key(session.api_key, ids, "png", True)
    try:
        on_disk = existing_sticker_paths(session.api_key, session.supply_id, ids)
        if on_disk:
            seeded = {}  # type: Dict[int, Dict[str, Any]]
            for oid, path in on_disk.items():
                nums = session.sticker_numbers.get(oid) or {}
                seeded[oid] = {
                    "partA": str(nums.get("partA") or ""),
                    "partB": str(nums.get("partB") or ""),
                    "file_b64": "",
                    "file_path": path,
                }
            _cache_merge_stickers(cache_key, seeded)
            diag_write(
                "supply.png_preload.resume_disk",
                sync=True,
                supply_id=session.supply_id,
                cached_on_disk=len(on_disk),
                order_total=order_total,
            )
            if progress:
                progress(4, _progress_detail(len(on_disk), order_total))

        missing = [oid for oid in ids if oid not in on_disk]
        diag_write(
            "supply.png_preload.fetch_begin",
            sync=True,
            supply_id=session.supply_id,
            order_total=order_total,
            missing=len(missing),
            chunk_size=5,
            isolated=True,
        )
        if missing:
            # Fetch only missing; already-on-disk stickers stay in cache.
            # Isolated child process per chunk — UI survives hard PNG crashes.
            def _missing_progress(done: int, total: int) -> None:
                _png_progress(len(on_disk) + done, order_total)

            fetch_stickers_map(
                session.api_key,
                missing,
                sticker_type="png",
                keep_files=True,
                progress=_missing_progress,
                chunk_size=5,
                cache_only=True,
                persist_supply_id=session.supply_id,
            )
            # Re-merge disk entries under the full-id cache key (fetch used missing-only key).
            on_disk2 = existing_sticker_paths(session.api_key, session.supply_id, ids)
            seeded2 = {}  # type: Dict[int, Dict[str, Any]]
            for oid, path in on_disk2.items():
                nums = session.sticker_numbers.get(oid) or {}
                seeded2[oid] = {
                    "partA": str(nums.get("partA") or ""),
                    "partB": str(nums.get("partB") or ""),
                    "file_b64": "",
                    "file_path": path,
                }
            _cache_merge_stickers(cache_key, seeded2)

        session.sticker_png_count = max(
            _cache_sticker_count(cache_key),
            len(existing_sticker_paths(session.api_key, session.supply_id, ids)),
        )
        diag_write(
            "supply.png_preload.fetch_done",
            sync=True,
            supply_id=session.supply_id,
            cached_count=session.sticker_png_count,
        )
    except MemoryError as exc:
        session.sticker_png_count = len(
            existing_sticker_paths(session.api_key, session.supply_id, ids)
        )
        diag_exception(
            "supply.png_preload.memory_error",
            exc,
            supply_id=session.supply_id,
        )
    except Exception as exc:
        session.sticker_png_count = len(
            existing_sticker_paths(session.api_key, session.supply_id, ids)
        )
        diag_exception(
            "supply.png_preload.error",
            exc,
            supply_id=session.supply_id,
        )
    finally:
        gc.collect()
    session.png_ready = True
    put_session(session)
    if progress:
        progress(4, _progress_detail(session.sticker_png_count, order_total))
    diag_write(
        "supply.png_preload.done",
        sync=True,
        supply_id=session.supply_id,
        png_ready=True,
        cached_count=session.sticker_png_count,
    )


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
