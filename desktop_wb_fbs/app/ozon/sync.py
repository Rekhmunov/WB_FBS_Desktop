# -*- coding: utf-8 -*-
"""Sync Ozon FBS postings → SQLite (isolated from WB tables)."""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from app.db import Database
from app.ozon import (
    SYNC_STATUSES,
    TAB_AWAITING_DELIVER,
    TAB_AWAITING_PACKAGING,
    carriage_is_done,
    iso_z,
    lookback_window,
    resolve_upsert_status,
    utc_now,
)
from app.ozon.client import OzonFbsClient
from app.ozon.sticker_fields import sticker_fields_from_posting

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


def _product_barcodes(posting: Dict[str, Any]) -> List[str]:
    """Product ШК from products[].barcodes — not package QR (web archive parity)."""
    out = []  # type: List[str]
    for prod in _products(posting):
        raw = prod.get("barcodes")
        if isinstance(raw, list):
            for item in raw:
                val = str(item or "").strip()
                if val and val not in out:
                    out.append(val)
        elif isinstance(raw, str):
            val = raw.strip()
            if val and val not in out:
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
    remote_status = str(posting.get("status") or "")
    cid = str(carriage_id or posting.get("carriage_id") or "").strip()
    offer_id = str(prod.get("offer_id") or "")
    sku = str(prod.get("sku") or "")
    name = str(prod.get("name") or "")
    # Sum all product quantities (web upsert parity) — first-product-only
    # under-counts multis when products_json is later missing.
    qty = 0
    for p in all_products:
        try:
            qty += int(p.get("quantity") or 0)
        except (TypeError, ValueError):
            qty += 1
    if qty <= 0:
        qty = 1

    wh = str(analytics.get("warehouse") or "")
    wh_id = analytics.get("warehouse_id")
    barcodes = _product_barcodes(posting)
    stickers = sticker_fields_from_posting(posting)
    sticker_upper = stickers.get("sticker_barcode") or ""
    sticker_lower = stickers.get("sticker_lower_barcode") or ""
    sticker_scanit = stickers.get("sticker_scanit") or ""
    cancel = posting.get("cancellation") if isinstance(posting.get("cancellation"), dict) else {}
    cancel_reason = str(cancel.get("cancel_reason") or "")
    now = utc_now()
    with db.connect() as conn:
        local = conn.execute(
            """
            SELECT status, tab, supply_id FROM ozon_fbs_postings
            WHERE source_id = ? AND posting_number = ?
            """,
            (source_id, pnum),
        ).fetchone()
        local_status = str(local["status"] or "") if local else ""
        local_tab = str(local["tab"] or "") if local else ""
        status, tab = resolve_upsert_status(
            local_status=local_status,
            local_tab=local_tab,
            remote_status=remote_status,
        )
        # Local supply membership means the row was collected — never show it
        # again under «Ожидают сборки» even if Ozon still lags on packaging.
        local_supply = str(local["supply_id"] or "").strip() if local else ""
        if local_supply and tab == TAB_AWAITING_PACKAGING:
            status, tab = TAB_AWAITING_DELIVER, TAB_AWAITING_DELIVER
        conn.execute(
            """
            INSERT INTO ozon_fbs_postings(
                source_id, posting_number, order_id, order_number, status, substatus,
                tab, carriage_id, offer_id, sku, product_name, quantity,
                warehouse_name, warehouse_id, barcodes_json, cancel_reason,
                shipment_date, in_process_at, created_at_wb, products_json,
                sticker_barcode, sticker_lower_barcode, sticker_scanit,
                raw_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, posting_number) DO UPDATE SET
                order_id = excluded.order_id,
                order_number = excluded.order_number,
                status = excluded.status,
                substatus = excluded.substatus,
                tab = excluded.tab,
                carriage_id = CASE WHEN excluded.carriage_id != ''
                    THEN excluded.carriage_id ELSE ozon_fbs_postings.carriage_id END,
                -- Never wipe product fields with an empty ship/force payload.
                offer_id = CASE WHEN excluded.offer_id != ''
                    THEN excluded.offer_id ELSE ozon_fbs_postings.offer_id END,
                sku = CASE WHEN excluded.sku != ''
                    THEN excluded.sku ELSE ozon_fbs_postings.sku END,
                product_name = CASE WHEN excluded.product_name != ''
                    THEN excluded.product_name ELSE ozon_fbs_postings.product_name END,
                quantity = CASE
                    WHEN excluded.products_json NOT IN ('', '[]', 'null')
                    THEN excluded.quantity ELSE ozon_fbs_postings.quantity END,
                warehouse_name = CASE WHEN excluded.warehouse_name != ''
                    THEN excluded.warehouse_name ELSE ozon_fbs_postings.warehouse_name END,
                warehouse_id = COALESCE(excluded.warehouse_id, ozon_fbs_postings.warehouse_id),
                barcodes_json = CASE
                    WHEN excluded.barcodes_json NOT IN ('', '[]', 'null')
                    THEN excluded.barcodes_json ELSE ozon_fbs_postings.barcodes_json END,
                cancel_reason = excluded.cancel_reason,
                shipment_date = excluded.shipment_date,
                in_process_at = excluded.in_process_at,
                created_at_wb = COALESCE(excluded.created_at_wb, ozon_fbs_postings.created_at_wb),
                products_json = CASE
                    WHEN excluded.products_json NOT IN ('', '[]', 'null')
                    THEN excluded.products_json ELSE ozon_fbs_postings.products_json END,
                sticker_barcode = CASE
                    WHEN excluded.sticker_barcode != '' THEN excluded.sticker_barcode
                    ELSE ozon_fbs_postings.sticker_barcode END,
                sticker_lower_barcode = CASE
                    WHEN excluded.sticker_lower_barcode != ''
                    THEN excluded.sticker_lower_barcode
                    ELSE ozon_fbs_postings.sticker_lower_barcode END,
                sticker_scanit = CASE
                    WHEN excluded.sticker_scanit != '' THEN excluded.sticker_scanit
                    ELSE ozon_fbs_postings.sticker_scanit END,
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
                sticker_upper,
                sticker_lower,
                sticker_scanit,
                json.dumps(posting, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()


def reconcile_packaging_in_local_supplies(db: Database, source_id: int) -> int:
    """Move packaging rows that already have a local supply off «Ожидают сборки».

    Sync races / missing anti-downgrade used to bounce collected postings back
    to packaging while keeping ``supply_id`` — inflating the tab badge vs split.
    """
    now = utc_now()
    with db.connect() as conn:
        cur = conn.execute(
            """
            UPDATE ozon_fbs_postings
            SET tab = ?,
                status = CASE
                    WHEN LOWER(COALESCE(status, '')) IN (
                        'awaiting_registration',
                        'acceptance_in_progress',
                        'awaiting_approve',
                        'awaiting_packaging',
                        ''
                    ) THEN ?
                    ELSE status
                END,
                synced_at = ?
            WHERE source_id = ?
              AND tab IN ('new', 'awaiting_packaging')
              AND COALESCE(supply_id, '') != ''
            """,
            (TAB_AWAITING_DELIVER, TAB_AWAITING_DELIVER, now, source_id),
        )
        conn.commit()
        return int(cur.rowcount or 0)


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
                SET carriage_id = ?, tab = ?, status = CASE
                    WHEN LOWER(COALESCE(status, '')) IN (
                        'awaiting_registration',
                        'acceptance_in_progress',
                        'awaiting_approve',
                        'awaiting_packaging',
                        '',
                        'new'
                    ) THEN ?
                    ELSE status
                END, synced_at = ?
                WHERE source_id = ? AND posting_number = ?
                """,
                (
                    cid,
                    TAB_AWAITING_DELIVER,
                    TAB_AWAITING_DELIVER,
                    now,
                    source_id,
                    pnum,
                ),
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

    def _finish(carriages_n: int = 0, *, was_stopped: bool = False) -> Dict[str, Any]:
        # Fix already-bounced packaging rows that still carry a local supply_id.
        try:
            fixed = reconcile_packaging_in_local_supplies(db, source_id)
            if fixed:
                _log.info(
                    "ozon sync source=%s reconciled %s packaging rows in local supplies",
                    source_id,
                    fixed,
                )
        except Exception as exc:
            errors.append("reconcile packaging: {}".format(exc))
        return {
            "postings": count,
            "carriages": carriages_n,
            "errors": errors,
            "stopped": was_stopped or stopped(),
        }

    if stopped():
        return _finish(was_stopped=True)

    since, to = lookback_window(lookback_days)
    _prog(progress, "Необработанные отправления…", count)
    try:
        cursor = ""
        while True:
            if stopped():
                return _finish(was_stopped=True)
            batch, _total, cursor, has_next = client.list_unfulfilled(
                limit=100,
                cursor=cursor,
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
            if not has_next or not cursor:
                break
    except Exception as exc:
        errors.append("unfulfilled: {}".format(exc))

    # Per-status list (web parity) — updates rows that left packaging so the
    # «Ожидают сборки» tab does not keep stale portal leftovers.
    _prog(progress, "Список отправлений…", count)
    for status in SYNC_STATUSES:
        if stopped():
            return _finish(was_stopped=True)
        label = status
        try:
            for page in client.iter_postings_window(lookback_days, status=status):
                if stopped():
                    return _finish(was_stopped=True)
                for posting in page:
                    if not isinstance(posting, dict):
                        continue
                    pnum = str(posting.get("posting_number") or "")
                    if not pnum:
                        continue
                    seen.add(pnum)
                    upsert_posting(db, source_id, posting)
                    count += 1
                _prog(
                    progress,
                    "{}… {}".format(label, count),
                    count,
                )
        except Exception as exc:
            errors.append("list {}: {}".format(label, exc))

    carriages_n = 0
    _prog(progress, "Отгрузки…", count)
    try:
        for page in client.iter_carriage_delivery_methods(limit=100):
            if stopped():
                return _finish(carriages_n, was_stopped=True)
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

    return _finish(carriages_n)
