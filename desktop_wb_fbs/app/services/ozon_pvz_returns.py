# -*- coding: utf-8 -*-
"""Ozon FBS «Возвраты ПВЗ» — ReceivedBySeller local cache."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.db import Database
from app.ozon import utc_now
from app.ozon.client import OzonFbsClient

_log = logging.getLogger(__name__)
_MSK = timezone(timedelta(hours=3))
ISSUED_STATUS = "receivedbyseller"
LOOKBACK_DAYS = 30
RETURNS_PAGE_LIMIT = 500


def sync_display_range(*, today: Optional[date] = None) -> Tuple[date, date]:
    end = today or datetime.now(_MSK).date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    return start, end


def _msk_day_bounds_utc(start: date, end: date) -> Tuple[str, str]:
    time_from = datetime.combine(start, dt_time.min, tzinfo=_MSK).astimezone(
        timezone.utc
    )
    time_to = datetime.combine(end, dt_time.max, tzinfo=_MSK).astimezone(
        timezone.utc
    )
    return (
        time_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
        time_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _normalize_completed_dt(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_MSK)
        else:
            dt = dt.astimezone(_MSK)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        if len(text) >= 10:
            return text[:19].replace(" ", "T")
        return ""


def extract_return_fields(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    try:
        return_id = int(item.get("id") or item.get("return_id") or 0)
    except (TypeError, ValueError):
        return_id = 0
    if return_id <= 0:
        return None
    visual = item.get("visual") if isinstance(item.get("visual"), dict) else {}
    status_obj = visual.get("status") if isinstance(visual.get("status"), dict) else {}
    visual_status = str(
        status_obj.get("sys_name")
        or item.get("visual_status_name")
        or item.get("status")
        or ""
    ).strip()
    completed = _normalize_completed_dt(
        visual.get("change_moment")
        or item.get("visual_status_change_moment")
        or item.get("updated_at")
        or ""
    )
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    logistic = item.get("logistic") if isinstance(item.get("logistic"), dict) else {}
    try:
        quantity = max(1, int(product.get("quantity") or item.get("quantity") or 1))
    except (TypeError, ValueError):
        quantity = 1
    return {
        "return_id": return_id,
        "posting_number": str(
            item.get("posting_number") or item.get("postingNumber") or ""
        ).strip(),
        "order_id": str(item.get("order_id") or "").strip(),
        "order_number": str(item.get("order_number") or "").strip(),
        "visual_status": visual_status,
        "completed_dt": completed,
        "offer_id": str(product.get("offer_id") or item.get("offer_id") or "").strip(),
        "sku": str(product.get("sku") or item.get("sku") or "").strip(),
        "product_name": str(
            product.get("name") or item.get("product_name") or ""
        ).strip(),
        "quantity": quantity,
        "barcode": str(
            logistic.get("barcode") or item.get("barcode") or ""
        ).strip(),
        "raw_json": json.dumps(item, ensure_ascii=False),
    }


class OzonPvzReturnsService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert_rows(self, source_id: int, items: List[Dict[str, Any]]) -> int:
        saved = 0
        now = utc_now()
        with self.db.connect() as conn:
            for item in items:
                fields = extract_return_fields(item if isinstance(item, dict) else {})
                if not fields:
                    continue
                status = str(fields.get("visual_status") or "").strip().casefold()
                if status and status != ISSUED_STATUS:
                    continue
                if not fields.get("completed_dt"):
                    continue
                conn.execute(
                    """
                    INSERT INTO ozon_fbs_pvz_returns(
                        source_id, return_id, posting_number, order_id,
                        order_number, visual_status, completed_dt, offer_id, sku,
                        product_name, quantity, barcode, raw_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, return_id) DO UPDATE SET
                        posting_number = excluded.posting_number,
                        order_id = excluded.order_id,
                        order_number = excluded.order_number,
                        visual_status = excluded.visual_status,
                        completed_dt = excluded.completed_dt,
                        offer_id = excluded.offer_id,
                        sku = excluded.sku,
                        product_name = excluded.product_name,
                        quantity = excluded.quantity,
                        barcode = excluded.barcode,
                        raw_json = excluded.raw_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        int(source_id),
                        int(fields["return_id"]),
                        fields["posting_number"],
                        fields["order_id"],
                        fields["order_number"],
                        fields["visual_status"] or "ReceivedBySeller",
                        fields["completed_dt"],
                        fields["offer_id"],
                        fields["sku"],
                        fields["product_name"],
                        int(fields["quantity"]),
                        fields["barcode"],
                        fields["raw_json"],
                        now,
                    ),
                )
                saved += 1
            conn.commit()
        return saved

    def sync_source(
        self,
        client: OzonFbsClient,
        source_id: int,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        start, end = sync_display_range()
        if date_from and date_to:
            try:
                start = date.fromisoformat(str(date_from)[:10])
                end = date.fromisoformat(str(date_to)[:10])
            except ValueError:
                pass
        if end < start:
            start, end = end, start
        time_from, time_to = _msk_day_bounds_utc(start, end)
        last_id = 0
        total = 0
        pages = 0
        while pages < 40:
            pages += 1
            data = client.returns_list(
                time_from=time_from,
                time_to=time_to,
                last_id=last_id,
                limit=RETURNS_PAGE_LIMIT,
            )
            rows = data.get("returns") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                rows = data.get("result") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                rows = []
            if not rows:
                break
            total += self.upsert_rows(source_id, rows)
            try:
                next_id = int((data or {}).get("last_id") or 0)
            except (TypeError, ValueError):
                next_id = 0
            if next_id <= 0 or next_id == last_id:
                # Fallback: max id from page
                ids = []
                for r in rows:
                    try:
                        ids.append(int(r.get("id") or r.get("return_id") or 0))
                    except (TypeError, ValueError):
                        pass
                next_id = max(ids) if ids else 0
            if next_id <= last_id or len(rows) < RETURNS_PAGE_LIMIT:
                break
            last_id = next_id
        return {
            "ok": True,
            "saved": total,
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "message": "Загружено возвратов: {}".format(total),
        }

    def list_local(
        self,
        source_id: int,
        *,
        date_from: str = "",
        date_to: str = "",
        search: str = "",
        limit: int = 500,
    ) -> Dict[str, Any]:
        start, end = sync_display_range()
        if date_from:
            try:
                start = date.fromisoformat(str(date_from)[:10])
            except ValueError:
                pass
        if date_to:
            try:
                end = date.fromisoformat(str(date_to)[:10])
            except ValueError:
                pass
        q = str(search or "").strip().lower()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_pvz_returns
                WHERE source_id = ?
                  AND completed_dt >= ?
                  AND completed_dt <= ?
                ORDER BY completed_dt DESC, return_id DESC
                LIMIT ?
                """,
                (
                    int(source_id),
                    start.isoformat(),
                    end.isoformat() + "T23:59:59",
                    max(1, min(int(limit), 2000)),
                ),
            ).fetchall()
        items = []  # type: List[Dict[str, Any]]
        for row in rows:
            d = Database.row_to_dict(row) or {}
            if q:
                blob = " ".join(
                    str(d.get(k) or "")
                    for k in (
                        "posting_number",
                        "order_number",
                        "offer_id",
                        "sku",
                        "product_name",
                        "barcode",
                    )
                ).lower()
                if q not in blob:
                    continue
            items.append(d)
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
        }
