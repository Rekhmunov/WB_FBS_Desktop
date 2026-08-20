# -*- coding: utf-8 -*-
"""Cancelled orders still listed in a supply (live WB status check)."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from app.db import Database
from app.wb import cancel_reason_label, compute_tab, is_cancelled_status, parse_json_list, utc_now
from app.wb.client import WbFbsClient


def rows_from_detail(
    detail_rows: List[Dict[str, Any]],
    *,
    sticker_numbers: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Build cancelled modal rows from already-loaded supply detail (no WB)."""
    stickers = sticker_numbers or {}
    out = []  # type: List[Dict[str, Any]]
    for r in detail_rows or []:
        label = str(r.get("cancel_reason_label") or r.get("cancel_reason") or "").strip()
        if not label and not is_cancelled_status(
            supplier_status=r.get("supplier_status"),
            wb_status=r.get("wb_status"),
        ):
            continue
        if not label:
            label = "Отменен"
        try:
            oid = int(r.get("order_id") or 0)
        except (TypeError, ValueError):
            continue
        if oid <= 0:
            continue
        st = stickers.get(oid) or {}
        part_a = str(
            st.get("partA") or r.get("sticker_part_a") or ""
        ).strip()
        part_b = str(
            st.get("partB") or r.get("sticker_part_b") or ""
        ).strip()
        barcode = str(
            st.get("barcode") or r.get("sticker_barcode") or ""
        ).strip()
        skus = r.get("skus") if isinstance(r.get("skus"), list) else parse_json_list(
            r.get("skus_json")
        )
        created = str(r.get("created_date") or r.get("created_at_wb") or "").strip()
        out.append(
            {
                "order_id": oid,
                "article": r.get("article") or "",
                "nm_id": r.get("nm_id"),
                "product_name": r.get("product_name") or "",
                "product_photo": r.get("product_photo") or "",
                "brand": r.get("brand") or "",
                "created_date": created,
                "skus": skus,
                "sticker_part_a": part_a,
                "sticker_part_b": part_b,
                "sticker_barcode": barcode,
                "sticker_number": "{}{}".format(part_a, part_b)
                or str(r.get("sticker_number") or ""),
                "cancel_reason": label,
                "cancel_reason_label": label,
                "supplier_status": r.get("supplier_status") or "",
                "wb_status": r.get("wb_status") or "",
            }
        )
    return out


def list_cancelled_in_supply(
    db: Database,
    source_id: int,
    api_key: str,
    supply_id: str,
    *,
    sticker_numbers: Optional[Dict[int, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    sid = str(supply_id or "").strip()
    if not sid:
        raise ValueError("Укажите supply_id")
    if not api_key:
        raise ValueError("Нет API-ключа источника")

    with db.connect() as conn:
        supply = conn.execute(
            """
            SELECT * FROM wb_fbs_supplies
            WHERE source_id = ? AND supply_id = ?
            """,
            (source_id, sid),
        ).fetchone()
    order_ids = parse_json_list(supply["order_ids_json"] if supply else "[]")
    order_ids = [int(x) for x in order_ids if str(x).strip().isdigit() or isinstance(x, int)]

    # If empty (common for done supplies), refresh from WB once.
    client = WbFbsClient(api_key)
    if not order_ids:
        order_ids = client.get_supply_order_ids(sid)
        time.sleep(0.21)
        if order_ids and supply:
            with db.connect() as conn:
                conn.execute(
                    """
                    UPDATE wb_fbs_supplies
                    SET order_ids_json = ?, synced_at = ?
                    WHERE source_id = ? AND supply_id = ?
                    """,
                    (json.dumps(order_ids), utc_now(), source_id, sid),
                )
                conn.commit()

    cancel_labels = {}  # type: Dict[int, str]
    persist = {}  # type: Dict[int, Tuple[str, str]]
    status_by_id = {}  # type: Dict[int, Tuple[str, str]]
    if order_ids:
        for i in range(0, len(order_ids), 1000):
            chunk = order_ids[i : i + 1000]
            chunk_rows = client.get_statuses(chunk)
            if not isinstance(chunk_rows, list):
                raise RuntimeError("Некорректный ответ Wildberries при проверке статусов")
            for st in chunk_rows:
                if not isinstance(st, dict):
                    continue
                try:
                    oid = int(st.get("id") or st.get("orderId") or 0)
                except (TypeError, ValueError):
                    continue
                if oid <= 0:
                    continue
                ss = str(st.get("supplierStatus") or "").strip()
                ws = str(st.get("wbStatus") or "").strip()
                status_by_id[oid] = (ss, ws)
                label = cancel_reason_label(supplier_status=ss, wb_status=ws)
                if label or is_cancelled_status(supplier_status=ss, wb_status=ws):
                    cancel_labels[oid] = label or "Отменен"
                    if ss or ws:
                        persist[oid] = (ss, ws)
            if i + 1000 < len(order_ids):
                time.sleep(0.21)
        if not status_by_id:
            raise RuntimeError("Wildberries не вернул статусы заказов")
        missing = [oid for oid in order_ids if oid not in status_by_id]
        if missing:
            raise RuntimeError(
                "Wildberries не вернул статусы для {} из {} заказов".format(
                    len(missing), len(order_ids)
                )
            )

    if persist:
        now = utc_now()
        with db.connect() as conn:
            for oid, (ss, ws) in persist.items():
                tab = compute_tab(supplier_status=ss, wb_status=ws, is_archive=False)
                conn.execute(
                    """
                    UPDATE wb_fbs_orders
                    SET supplier_status = ?, wb_status = ?, tab = ?, synced_at = ?
                    WHERE source_id = ? AND order_id = ?
                    """,
                    (ss, ws, tab, now, source_id, oid),
                )
            conn.commit()

    cancelled_ids = [oid for oid in order_ids if oid in cancel_labels]
    local_by_id = {}  # type: Dict[int, Dict[str, Any]]
    if cancelled_ids:
        placeholders = ", ".join("?" for _ in cancelled_ids)
        with db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM wb_fbs_orders
                WHERE source_id = ? AND order_id IN ({})
                """.format(
                    placeholders
                ),
                tuple([source_id] + cancelled_ids),
            ).fetchall()
        for r in rows:
            local_by_id[int(r["order_id"])] = dict(r)

    # Sticker numbers for cancelled rows (web enriches with svg stickers map).
    stickers = {}  # type: Dict[int, Dict[str, Any]]
    if cancelled_ids and sticker_numbers is not None:
        stickers = {
            int(oid): (st or {})
            for oid, st in (sticker_numbers or {}).items()
            if oid in set(cancelled_ids)
        }
        # Fill gaps from explicit map keys that may use int/str mix.
        for oid in cancelled_ids:
            if oid not in stickers and sticker_numbers.get(oid):
                stickers[oid] = sticker_numbers.get(oid) or {}
    elif cancelled_ids and api_key:
        try:
            from app.services.print_docs import _fetch_picking_stickers

            stickers = _fetch_picking_stickers(api_key, cancelled_ids)
        except Exception:
            stickers = {}

    out_rows = []  # type: List[Dict[str, Any]]
    for oid in cancelled_ids:
        local = local_by_id.get(oid) or {}
        st = stickers.get(oid) or {}
        part_a = str(st.get("partA") or local.get("sticker_part_a") or "").strip()
        part_b = str(st.get("partB") or local.get("sticker_part_b") or "").strip()
        barcode = str(st.get("barcode") or local.get("sticker_barcode") or "").strip()
        skus = local.get("skus") if isinstance(local.get("skus"), list) else parse_json_list(
            local.get("skus_json")
        )
        out_rows.append(
            {
                "order_id": oid,
                "article": local.get("article") or "",
                "nm_id": local.get("nm_id"),
                "product_name": local.get("product_name") or "",
                "product_photo": local.get("product_photo") or "",
                "brand": local.get("brand") or "",
                "created_date": local.get("created_date")
                or local.get("created_at_wb")
                or "",
                "skus": skus,
                "sticker_part_a": part_a,
                "sticker_part_b": part_b,
                "sticker_barcode": barcode,
                "sticker_number": "{}{}".format(part_a, part_b),
                "cancel_reason": cancel_labels.get(oid) or "Отменен",
                "cancel_reason_label": cancel_labels.get(oid) or "Отменен",
                "supplier_status": (status_by_id.get(oid) or ("", ""))[0],
                "wb_status": (status_by_id.get(oid) or ("", ""))[1],
            }
        )
    return {"rows": out_rows, "cancelled_count": len(out_rows)}
