# -*- coding: utf-8 -*-
"""Cancelled orders still listed in a supply (live WB status check)."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple

from app.db import Database
from app.wb import cancel_reason_label, compute_tab, is_cancelled_status, parse_json_list, utc_now
from app.wb.client import WbFbsClient


def list_cancelled_in_supply(
    db: Database,
    source_id: int,
    api_key: str,
    supply_id: str,
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

    out_rows = []  # type: List[Dict[str, Any]]
    for oid in cancelled_ids:
        local = local_by_id.get(oid) or {}
        out_rows.append(
            {
                "order_id": oid,
                "article": local.get("article") or "",
                "cancel_reason": cancel_labels.get(oid) or "Отменен",
                "supplier_status": (status_by_id.get(oid) or ("", ""))[0],
                "wb_status": (status_by_id.get(oid) or ("", ""))[1],
                "skus": parse_json_list(local.get("skus_json")),
            }
        )
    return {"rows": out_rows, "cancelled_count": len(out_rows)}
