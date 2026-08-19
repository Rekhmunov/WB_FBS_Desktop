# -*- coding: utf-8 -*-
"""Lookup order by id locally or via WB (finished/cancelled escape hatch)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from app.db import Database
from app.wb import (
    SCOPE_ERROR_MESSAGE,
    friendly_sync_error,
    is_marketplace_scope_error,
)
from app.wb.client import WbFbsClient
from app.wb.sync import upsert_order

_log = logging.getLogger(__name__)


def _fetch_order_payload_from_wb(
    client: WbFbsClient, order_id: int
) -> Tuple[Optional[Dict[str, Any]], bool, Optional[Dict[str, str]]]:
    oid = int(order_id)
    status_row = None  # type: Optional[Dict[str, str]]
    statuses = client.get_statuses([oid])
    for st in statuses:
        if not isinstance(st, dict) or st.get("id") is None:
            continue
        try:
            if int(st["id"]) != oid:
                continue
        except (TypeError, ValueError):
            continue
        status_row = {
            "supplierStatus": str(st.get("supplierStatus") or ""),
            "wbStatus": str(st.get("wbStatus") or ""),
        }
        break
    time.sleep(0.21)
    if status_row is None:
        return None, False, None

    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(days=30)
    next_token = 0  # type: Optional[int]
    pages = 0
    while pages < 20:
        orders, next_token = client.get_orders_page(
            limit=1000,
            next_token=next_token if next_token is not None else 0,
            date_from=date_from,
            date_to=date_to,
        )
        for order in orders:
            if not isinstance(order, dict) or order.get("id") is None:
                continue
            try:
                if int(order["id"]) == oid:
                    return order, False, status_row
            except (TypeError, ValueError):
                continue
        pages += 1
        if next_token is None:
            break
        time.sleep(0.25)

    try:
        for arch_orders in client.iter_archive_pages(
            months_back=6, limit=1000, max_pages=5
        ):
            for order in arch_orders:
                if not isinstance(order, dict) or order.get("id") is None:
                    continue
                try:
                    if int(order["id"]) == oid:
                        return order, True, status_row
                except (TypeError, ValueError):
                    continue
    except Exception as exc:
        _log.warning("desktop lookup archive scan failed order=%s: %s", oid, exc)

    return {"id": oid}, False, status_row


def lookup_order_by_id(
    db: Database,
    source_id: int,
    order_id: int,
    api_key: str,
    allow_remote: bool = True,
) -> Dict[str, Any]:
    oid = int(order_id)
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM wb_fbs_orders
            WHERE source_id = ? AND order_id = ?
            """,
            (source_id, oid),
        ).fetchone()
    if row:
        item = dict(row)
        return {
            "found": True,
            "source": "local",
            "order_id": oid,
            "tab": str(item.get("tab") or ""),
            "item": item,
        }

    if not allow_remote:
        return {
            "found": False,
            "order_id": oid,
            "message": "Заказ не найден в локальной базе",
        }

    key = str(api_key or "").strip()
    if not key:
        return {
            "found": False,
            "order_id": oid,
            "message": "Нет API-ключа источника для поиска в WB",
        }

    client = WbFbsClient(key)
    try:
        order_payload, is_archive, status_row = _fetch_order_payload_from_wb(client, oid)
    except Exception as exc:
        if is_marketplace_scope_error(exc):
            return {
                "found": False,
                "order_id": oid,
                "scope_error": True,
                "message": SCOPE_ERROR_MESSAGE,
            }
        return {
            "found": False,
            "order_id": oid,
            "message": friendly_sync_error("поиск заказа", exc),
        }

    if order_payload is None or status_row is None:
        return {
            "found": False,
            "order_id": oid,
            "message": "Заказ не найден в WB API",
        }

    upsert_order(
        db,
        source_id,
        order_payload,
        supplier_status=status_row.get("supplierStatus") or "",
        wb_status=status_row.get("wbStatus") or "",
        is_archive=bool(is_archive),
    )
    with db.connect() as conn:
        stored = conn.execute(
            """
            SELECT * FROM wb_fbs_orders
            WHERE source_id = ? AND order_id = ?
            """,
            (source_id, oid),
        ).fetchone()
    if not stored:
        return {
            "found": False,
            "order_id": oid,
            "message": "Заказ найден в WB, но не удалось сохранить локально",
        }
    item = dict(stored)
    return {
        "found": True,
        "source": "remote",
        "order_id": oid,
        "tab": str(item.get("tab") or ""),
        "item": item,
        "is_archive": bool(is_archive),
    }
