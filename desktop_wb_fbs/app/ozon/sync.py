# -*- coding: utf-8 -*-
"""Sync Ozon FBS postings → SQLite (isolated from WB tables)."""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from app.db import Database
from app.ozon import compute_tab, carriage_is_done, iso_z, lookback_window, utc_now
from app.ozon.client import OzonFbsClient

_log = logging.getLogger(__name__)
ProgressCb = Optional[Callable[[str, int], None]]


def _prog(cb: ProgressCb, msg: str, n: int) -> None:
    if cb:
        cb(str(msg or ""), int(n or 0))


def _products(posting: Dict[str, Any]) -> List[Dict[str, Any]]:
    products = posting.get("products")
    if isinstance(products, list):
        return [p for p in products if isinstance(p, dict)]
    return []


def _first_product(posting: Dict[str, Any]) -> Dict[str, Any]:
    products = _products(posting)
    return products[0] if products else {}


def _barcodes(posting: Dict[str, Any]) -> List[str]:
    bc = posting.get("barcodes")
    if not isinstance(bc, dict):
        return []
    out = []
    for key in ("upper_barcode", "lower_barcode"):
        val = str(bc.get(key) or "").strip()
        if val:
            out.append(val)
    return out


def upsert_posting(
    db: Database,
    source_id: int,
    posting: Dict[str, Any],
    *,
    carriage_id: str = "",
) -> None:
    pnum = str(posting.get("posting_number") or "").strip()
    if not pnum:
        return
    prod = _first_product(posting)
    all_products = _products(posting)
    analytics = posting.get("analytics_data") if isinstance(posting.get("analytics_data"), dict) else {}
    status = str(posting.get("status") or "")
    cid = str(carriage_id or posting.get("carriage_id") or "").strip()
    tab = compute_tab(status=status, carriage_id=cid)
    offer_id = str(prod.get("offer_id") or "")
    sku = str(prod.get("sku") or "")
    name = str(prod.get("name") or "")
    qty = int(prod.get("quantity") or 1)
    wh = str(analytics.get("warehouse") or "")
    wh_id = analytics.get("warehouse_id")
    barcodes = _barcodes(posting)
    cancel = posting.get("cancellation") if isinstance(posting.get("cancellation"), dict) else {}
    cancel_reason = str(cancel.get("cancel_reason") or "")
    now = utc_now()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO ozon_fbs_postings(
                source_id, posting_number, order_id, order_number, status, substatus,
                tab, carriage_id, offer_id, sku, product_name, quantity,
                warehouse_name, warehouse_id, barcodes_json, cancel_reason,
                shipment_date, in_process_at, created_at_wb, products_json,
                raw_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, posting_number) DO UPDATE SET
                order_id = excluded.order_id,
                order_number = excluded.order_number,
                status = excluded.status,
                substatus = excluded.substatus,
                tab = excluded.tab,
                carriage_id = CASE WHEN excluded.carriage_id != ''
                    THEN excluded.carriage_id ELSE ozon_fbs_postings.carriage_id END,
                offer_id = excluded.offer_id,
                sku = excluded.sku,
                product_name = excluded.product_name,
                quantity = excluded.quantity,
                warehouse_name = excluded.warehouse_name,
                warehouse_id = excluded.warehouse_id,
                barcodes_json = excluded.barcodes_json,
                cancel_reason = excluded.cancel_reason,
                shipment_date = excluded.shipment_date,
                in_process_at = excluded.in_process_at,
                created_at_wb = COALESCE(excluded.created_at_wb, ozon_fbs_postings.created_at_wb),
                products_json = excluded.products_json,
                raw_json = excluded.raw_json,
                synced_at = excluded.synced_at
            """,
            (
                source_id,
                pnum,
                str(posting.get("order_id") or ""),
                str(posting.get("order_number") or ""),
                status,
                str(posting.get("substatus") or ""),
                tab,
                cid,
                offer_id,
                sku,
                name,
                qty,
                wh,
                int(wh_id) if wh_id not in (None, "") else None,
                json.dumps(barcodes, ensure_ascii=False),
                cancel_reason,
                str(posting.get("shipment_date") or ""),
                str(posting.get("in_process_at") or ""),
                str(posting.get("in_process_at") or posting.get("shipment_date") or ""),
                json.dumps(all_products, ensure_ascii=False),
                json.dumps(posting, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()


def upsert_carriage(
    db: Database,
    source_id: int,
    carriage: Dict[str, Any],
    *,
    delivery_method_id: Optional[int] = None,
) -> None:
    cid = str(
        carriage.get("carriage_id") or carriage.get("id") or ""
    ).strip()
    if not cid:
        return
    status = str(carriage.get("status") or "")
    done = 1 if carriage_is_done(status) else 0
    postings = carriage.get("posting_numbers") or carriage.get("postings") or []
    if isinstance(postings, list):
        pnums = [str(x.get("posting_number") if isinstance(x, dict) else x) for x in postings]
        pnums = [p for p in pnums if p]
    else:
        pnums = []
    now = utc_now()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO ozon_fbs_carriages(
                source_id, carriage_id, status, done, delivery_method_id,
                posting_numbers_json, raw_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, carriage_id) DO UPDATE SET
                status = excluded.status,
                done = excluded.done,
                delivery_method_id = excluded.delivery_method_id,
                posting_numbers_json = excluded.posting_numbers_json,
                raw_json = excluded.raw_json,
                synced_at = excluded.synced_at
            """,
            (
                source_id,
                cid,
                status,
                done,
                int(carriage.get("delivery_method_id") or delivery_method_id or 0) or None,
                json.dumps(pnums, ensure_ascii=False),
                json.dumps(carriage, ensure_ascii=False),
                now,
            ),
        )
        for pnum in pnums:
            conn.execute(
                """
                UPDATE ozon_fbs_postings
                SET carriage_id = ?, tab = 'assembly', synced_at = ?
                WHERE source_id = ? AND posting_number = ?
                """,
                (cid, now, source_id, pnum),
            )
        conn.commit()


def sync_source(
    db: Database,
    source_id: int,
    client_id: str,
    api_key: str,
    lookback_days: int,
    *,
    stop_requested: Optional[Callable[[], bool]] = None,
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    client = OzonFbsClient(client_id, api_key)
    errors = []  # type: List[str]
    seen = set()  # type: set
    count = 0

    def stopped() -> bool:
        return bool(stop_requested and stop_requested())

    try:
        _prog(progress, "Проверка API…", count)
        client.ping()
    except Exception as exc:
        return {
            "postings": 0,
            "carriages": 0,
            "errors": [str(exc)],
            "stopped": False,
        }

    if stopped():
        return {"postings": count, "carriages": 0, "errors": errors, "stopped": True}

    since, to = lookback_window(lookback_days)
    _prog(progress, "Необработанные отправления…", count)
    try:
        offset = 0
        while True:
            if stopped():
                return {"postings": count, "carriages": 0, "errors": errors, "stopped": True}
            batch, total = client.list_unfulfilled(
                limit=100,
                offset=offset,
                cutoff_from=iso_z(since),
                cutoff_to=iso_z(to),
            )
            if not batch:
                break
            for posting in batch:
                if not isinstance(posting, dict):
                    continue
                pnum = str(posting.get("posting_number") or "")
                if not pnum or pnum in seen:
                    continue
                seen.add(pnum)
                upsert_posting(db, source_id, posting)
                count += 1
            _prog(progress, "Необработанные… {}".format(count), count)
            offset += len(batch)
            if offset >= total or len(batch) < 100:
                break
    except Exception as exc:
        errors.append("unfulfilled: {}".format(exc))

    _prog(progress, "Список отправлений…", count)
    try:
        for page in client.iter_postings_window(lookback_days):
            if stopped():
                return {"postings": count, "carriages": 0, "errors": errors, "stopped": True}
            for posting in page:
                if not isinstance(posting, dict):
                    continue
                pnum = str(posting.get("posting_number") or "")
                if not pnum:
                    continue
                seen.add(pnum)
                upsert_posting(db, source_id, posting)
                count += 1
            _prog(progress, "Отправления… {}".format(count), count)
    except Exception as exc:
        errors.append("list: {}".format(exc))

    carriages_n = 0
    _prog(progress, "Отгрузки…", count)
    try:
        for page in client.iter_carriage_delivery_methods(limit=100):
            if stopped():
                return {"postings": count, "carriages": 0, "errors": errors, "stopped": True}
            for block in page:
                if not isinstance(block, dict):
                    continue
                dm_id = block.get("delivery_method_id")
                for carriage in block.get("carriages") or []:
                    if not isinstance(carriage, dict):
                        continue
                    upsert_carriage(
                        db,
                        source_id,
                        carriage,
                        delivery_method_id=int(dm_id) if dm_id not in (None, "") else None,
                    )
                    carriages_n += 1
        _prog(progress, "Готово · отправлений {}".format(count), count)
    except Exception as exc:
        errors.append("carriage: {}".format(exc))

    return {
        "postings": count,
        "carriages": carriages_n,
        "errors": errors,
        "stopped": stopped(),
    }
