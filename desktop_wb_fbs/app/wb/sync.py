# -*- coding: utf-8 -*-
"""Local sync: WB Marketplace → SQLite (no stock ledger / no CHZ)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set

from app.db import Database
from app.wb import (
    SCOPE_ERROR_MESSAGE,
    coalesce_b2b_flag,
    compute_tab,
    friendly_sync_error,
    is_marketplace_scope_error,
    lookback_window,
    normalize_api_key,
    order_b2b_flag,
    parse_dt,
    resolve_order_price,
    as_int_or_none,
    utc_now,
)
from app.wb.client import WbFbsClient

_log = logging.getLogger(__name__)


def upsert_order(
    db: Database,
    source_id: int,
    order: Dict[str, Any],
    supplier_status: Optional[str] = None,
    wb_status: Optional[str] = None,
    is_archive: bool = False,
    supply_id: Optional[str] = None,
) -> None:
    try:
        order_id = int(order.get("id"))
    except (TypeError, ValueError):
        return
    ss = str(
        supplier_status if supplier_status is not None else order.get("supplierStatus") or ""
    ).strip()
    ws = str(
        wb_status if wb_status is not None else order.get("wbStatus") or ""
    ).strip()
    sid = str(
        supply_id if supply_id is not None else order.get("supplyId") or ""
    ).strip()
    tab = compute_tab(supplier_status=ss, wb_status=ws, is_archive=is_archive)
    offices = order.get("offices") if isinstance(order.get("offices"), list) else []
    skus = order.get("skus") if isinstance(order.get("skus"), list) else []
    price_i, currency_i = resolve_order_price(order)
    sale_price_i = as_int_or_none(order.get("finalPrice"))
    if sale_price_i is None:
        sale_price_i = as_int_or_none(order.get("price")) or 0
    b2b_flag = order_b2b_flag(order)
    is_b2b = bool(b2b_flag) if b2b_flag is not None else False
    has_b2b_signal = b2b_flag is not None
    now = utc_now()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO wb_fbs_orders (
                source_id, order_id, order_uid, rid, article, nm_id, chrt_id, skus_json,
                price, final_price, currency_code, warehouse_id, office_id, offices_json,
                cargo_type, delivery_type, supplier_status, wb_status, tab, supply_id,
                is_archive, is_b2b, comment_text, created_at_wb, raw_json, synced_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(source_id, order_id) DO UPDATE SET
                order_uid = excluded.order_uid,
                rid = CASE WHEN excluded.rid != '' THEN excluded.rid ELSE wb_fbs_orders.rid END,
                article = excluded.article,
                nm_id = excluded.nm_id,
                chrt_id = excluded.chrt_id,
                skus_json = excluded.skus_json,
                price = excluded.price,
                final_price = excluded.final_price,
                currency_code = excluded.currency_code,
                warehouse_id = excluded.warehouse_id,
                office_id = excluded.office_id,
                offices_json = excluded.offices_json,
                cargo_type = excluded.cargo_type,
                delivery_type = excluded.delivery_type,
                supplier_status = CASE
                    WHEN excluded.supplier_status != '' THEN excluded.supplier_status
                    ELSE wb_fbs_orders.supplier_status END,
                wb_status = CASE
                    WHEN excluded.wb_status != '' THEN excluded.wb_status
                    ELSE wb_fbs_orders.wb_status END,
                tab = CASE
                    WHEN excluded.is_archive THEN 'archive'
                    WHEN excluded.supplier_status != '' OR excluded.wb_status != ''
                        THEN excluded.tab
                    ELSE wb_fbs_orders.tab END,
                supply_id = CASE
                    WHEN excluded.supply_id != '' THEN excluded.supply_id
                    ELSE wb_fbs_orders.supply_id END,
                is_archive = excluded.is_archive OR wb_fbs_orders.is_archive,
                is_b2b = CASE WHEN ? THEN excluded.is_b2b ELSE wb_fbs_orders.is_b2b END,
                comment_text = excluded.comment_text,
                created_at_wb = COALESCE(excluded.created_at_wb, wb_fbs_orders.created_at_wb),
                raw_json = excluded.raw_json,
                synced_at = excluded.synced_at
            """,
            (
                source_id,
                order_id,
                str(order.get("orderUid") or ""),
                str(order.get("rid") or "").strip(),
                str(order.get("article") or ""),
                order.get("nmId"),
                order.get("chrtId"),
                json.dumps(skus, ensure_ascii=False),
                int(sale_price_i or 0),
                int(price_i),
                int(currency_i or 643),
                order.get("warehouseId"),
                order.get("officeId"),
                json.dumps(offices, ensure_ascii=False),
                int(order.get("cargoType") or 0),
                str(order.get("deliveryType") or ""),
                ss,
                ws,
                tab,
                sid,
                1 if is_archive else 0,
                1 if is_b2b else 0,
                str(order.get("comment") or ""),
                parse_dt(order.get("createdAt")),
                json.dumps(order, ensure_ascii=False),
                now,
                1 if has_b2b_signal else 0,
            ),
        )
        conn.commit()


def upsert_supply(
    db: Database,
    source_id: int,
    supply: Dict[str, Any],
    order_ids: Optional[List[int]] = None,
    boxes: Optional[List[Dict[str, Any]]] = None,
) -> None:
    supply_id = str(supply.get("id") or "").strip()
    if not supply_id:
        return
    now = utc_now()
    supply_b2b = coalesce_b2b_flag(supply)
    has_supply_b2b = supply_b2b is not None
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO wb_fbs_supplies (
                source_id, supply_id, name, done, cargo_type, is_b2b, destination_office_id,
                created_at_wb, closed_at_wb, scan_dt, order_ids_json, boxes_json, raw_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, supply_id) DO UPDATE SET
                name = excluded.name,
                done = excluded.done,
                cargo_type = excluded.cargo_type,
                is_b2b = CASE WHEN ? THEN excluded.is_b2b ELSE wb_fbs_supplies.is_b2b END,
                destination_office_id = excluded.destination_office_id,
                created_at_wb = COALESCE(excluded.created_at_wb, wb_fbs_supplies.created_at_wb),
                closed_at_wb = COALESCE(excluded.closed_at_wb, wb_fbs_supplies.closed_at_wb),
                scan_dt = COALESCE(excluded.scan_dt, wb_fbs_supplies.scan_dt),
                order_ids_json = CASE
                    WHEN excluded.order_ids_json != '[]' THEN excluded.order_ids_json
                    ELSE wb_fbs_supplies.order_ids_json END,
                boxes_json = CASE
                    WHEN excluded.boxes_json != '[]' THEN excluded.boxes_json
                    ELSE wb_fbs_supplies.boxes_json END,
                raw_json = excluded.raw_json,
                synced_at = excluded.synced_at
            """,
            (
                source_id,
                supply_id,
                str(supply.get("name") or ""),
                1 if supply.get("done") else 0,
                int(supply.get("cargoType") or 0),
                1 if (supply_b2b if supply_b2b is not None else False) else 0,
                supply.get("destinationOfficeId"),
                parse_dt(supply.get("createdAt")),
                parse_dt(supply.get("closedAt")),
                parse_dt(supply.get("scanDt")),
                json.dumps(order_ids or [], ensure_ascii=False),
                json.dumps(boxes or [], ensure_ascii=False),
                json.dumps(supply, ensure_ascii=False),
                now,
                1 if has_supply_b2b else 0,
            ),
        )
        conn.commit()


def sync_source(
    db: Database,
    source_id: int,
    api_key: str,
    lookback_days: int = 3,
    stop_requested: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    api_key = normalize_api_key(api_key)
    if not api_key:
        return {
            "orders": 0,
            "supplies": 0,
            "errors": ["Пустой API-ключ Marketplace"],
            "stopped": False,
        }
    client = WbFbsClient(api_key)
    stopped = False
    seen_order_ids = set()  # type: Set[int]
    seen_supply_ids = set()  # type: Set[str]
    errors = []  # type: List[str]

    def _stopped() -> bool:
        return bool(stop_requested and stop_requested())

    def _prog(msg: str, n: Optional[int] = None) -> None:
        if progress:
            progress(msg, len(seen_order_ids) if n is None else n)

    _prog("Проверка ключа…")
    try:
        client.ping()
        time.sleep(0.21)
    except Exception as exc:
        _log.warning("ping failed: %s", exc)
        if is_marketplace_scope_error(exc):
            return {
                "orders": 0,
                "supplies": 0,
                "errors": [],
                "stopped": False,
                "scope_error": True,
                "message": SCOPE_ERROR_MESSAGE,
            }
        return {
            "orders": 0,
            "supplies": 0,
            "errors": [friendly_sync_error("проверка ключа", exc)],
            "stopped": False,
        }

    _prog("Новые заказы…")
    try:
        for order in client.get_new_orders():
            if _stopped():
                stopped = True
                break
            upsert_order(
                db, source_id, order, supplier_status="new", is_archive=False
            )
            try:
                seen_order_ids.add(int(order.get("id")))
            except (TypeError, ValueError):
                pass
        time.sleep(0.21)
    except Exception as exc:
        _log.warning("new orders failed: %s", exc)
        if is_marketplace_scope_error(exc):
            return {
                "orders": 0,
                "supplies": 0,
                "errors": [],
                "stopped": False,
                "scope_error": True,
                "message": SCOPE_ERROR_MESSAGE,
            }
        errors.append(friendly_sync_error("new", exc))

    if _stopped():
        return {
            "orders": len(seen_order_ids),
            "supplies": len(seen_supply_ids),
            "errors": errors,
            "stopped": True,
        }

    _prog("Заказы за период…")
    date_from, date_to = lookback_window(lookback_days)
    next_token = 0  # type: Optional[int]
    pages = 0
    try:
        while pages < 20:
            if _stopped():
                stopped = True
                break
            orders, next_token = client.get_orders_page(
                limit=1000,
                next_token=next_token if next_token is not None else 0,
                date_from=date_from,
                date_to=date_to,
            )
            if not orders:
                break
            for order in orders:
                upsert_order(db, source_id, order)
                try:
                    seen_order_ids.add(int(order.get("id")))
                except (TypeError, ValueError):
                    pass
            pages += 1
            _prog("Заказы… стр. {}".format(pages))
            if next_token is None:
                break
            time.sleep(0.25)
    except Exception as exc:
        if is_marketplace_scope_error(exc):
            return {
                "orders": len(seen_order_ids),
                "supplies": len(seen_supply_ids),
                "errors": [],
                "stopped": False,
                "scope_error": True,
                "message": SCOPE_ERROR_MESSAGE,
            }
        errors.append(friendly_sync_error("orders", exc))

    if _stopped():
        return {
            "orders": len(seen_order_ids),
            "supplies": len(seen_supply_ids),
            "errors": errors,
            "stopped": True,
        }

    _prog("Статусы…")
    with db.connect() as conn:
        id_rows = conn.execute(
            """
            SELECT order_id FROM wb_fbs_orders
            WHERE source_id = ? AND is_archive = 0
              AND tab IN ('new', 'assembly')
            ORDER BY synced_at DESC
            LIMIT 5000
            """,
            (source_id,),
        ).fetchall()
    all_ids = [int(r["order_id"]) for r in id_rows]
    for i in range(0, len(all_ids), 1000):
        if _stopped():
            stopped = True
            break
        chunk = all_ids[i : i + 1000]
        try:
            statuses = client.get_statuses(chunk)
            status_map = {
                int(s["id"]): s
                for s in statuses
                if isinstance(s, dict) and s.get("id") is not None
            }
            now = utc_now()
            with db.connect() as conn:
                for oid, st in status_map.items():
                    ss = str(st.get("supplierStatus") or "")
                    ws = str(st.get("wbStatus") or "")
                    tab = compute_tab(
                        supplier_status=ss, wb_status=ws, is_archive=False
                    )
                    conn.execute(
                        """
                        UPDATE wb_fbs_orders
                        SET supplier_status = ?, wb_status = ?, tab = ?, synced_at = ?
                        WHERE source_id = ? AND order_id = ?
                        """,
                        (ss, ws, tab, now, source_id, oid),
                    )
                conn.commit()
            time.sleep(0.21)
        except Exception as exc:
            errors.append(friendly_sync_error("status", exc))
            break

    if _stopped():
        return {
            "orders": len(seen_order_ids),
            "supplies": len(seen_supply_ids),
            "errors": errors,
            "stopped": True,
        }

    _prog("Поставки FBS…", len(seen_supply_ids))
    next_sup = 0
    sup_pages = 0
    try:
        while sup_pages < 10:
            if _stopped():
                stopped = True
                break
            supplies, next_sup = client.get_supplies(limit=1000, next_token=next_sup)
            if not supplies:
                break
            for supply in supplies:
                if _stopped():
                    stopped = True
                    break
                sid = str(supply.get("id") or "")
                # Skip done supplies («В доставке») — not synced / not shown.
                if bool(supply.get("done")):
                    continue
                order_ids = []  # type: List[int]
                boxes = []  # type: List[Dict[str, Any]]
                if sid:
                    try:
                        order_ids = client.get_supply_order_ids(sid)
                        time.sleep(0.21)
                        boxes = client.get_supply_boxes(sid)
                        time.sleep(0.21)
                    except Exception as exc:
                        errors.append(friendly_sync_error("supply {}".format(sid), exc))
                upsert_supply(
                    db, source_id, supply, order_ids=order_ids, boxes=boxes
                )
                if sid:
                    seen_supply_ids.add(sid)
                if order_ids:
                    now = utc_now()
                    with db.connect() as conn:
                        for oid in order_ids:
                            conn.execute(
                                """
                                UPDATE wb_fbs_orders
                                SET supply_id = ?,
                                    supplier_status = CASE
                                        WHEN supplier_status = 'new' OR supplier_status = ''
                                        THEN 'confirm' ELSE supplier_status END,
                                    tab = CASE
                                        WHEN tab = 'new' THEN 'assembly' ELSE tab END,
                                    synced_at = ?
                                WHERE source_id = ? AND order_id = ?
                                """,
                                (sid, now, source_id, oid),
                            )
                        conn.commit()
            sup_pages += 1
            _prog("Поставки… стр. {}".format(sup_pages), len(seen_supply_ids))
            if not next_sup:
                break
            time.sleep(0.25)
    except Exception as exc:
        errors.append(friendly_sync_error("supplies", exc))

    return {
        "orders": len(seen_order_ids),
        "supplies": len(seen_supply_ids),
        "errors": errors,
        "stopped": stopped,
    }
