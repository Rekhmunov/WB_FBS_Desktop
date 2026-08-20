# -*- coding: utf-8 -*-
"""TRBX + stickers helpers for desktop."""
from __future__ import annotations

import base64
import time
from typing import Any, Dict, List, Optional

from app.db import Database
from app.wb import parse_json_list, utc_now
from app.wb.client import TRBX_STICKERS_PER_REQUEST, WbFbsClient
from app.wb.sync import upsert_supply


class TrbxService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list_boxes(self, source_id: int, supply_id: str) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT boxes_json FROM wb_fbs_supplies
                WHERE source_id = ? AND supply_id = ?
                """,
                (source_id, supply_id),
            ).fetchone()
        return parse_json_list(row["boxes_json"] if row else "[]")

    def refresh(self, source_id: int, api_key: str, supply_id: str) -> List[Dict[str, Any]]:
        client = WbFbsClient(api_key)
        boxes = client.get_supply_boxes(supply_id)
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE wb_fbs_supplies
                SET boxes_json = ?, synced_at = ?
                WHERE source_id = ? AND supply_id = ?
                """,
                (
                    __import__("json").dumps(boxes, ensure_ascii=False),
                    utc_now(),
                    source_id,
                    supply_id,
                ),
            )
            conn.commit()
        return boxes

    def create(
        self,
        source_id: int,
        api_key: str,
        supply_id: str,
        amount: int,
        *,
        order_count: Optional[int] = None,
    ) -> List[str]:
        from app.services.orders import OrdersService

        supply = OrdersService(self.db).get_supply(source_id, supply_id) or {}
        if bool(supply.get("done")):
            raise ValueError(
                "Поставка уже закрыта — грузоместа добавить нельзя"
            )
        n = int(amount or 0)
        if n < 1:
            raise ValueError("Укажите количество грузомест")
        boxes = self.list_boxes(source_id, supply_id)
        qty = int(order_count if order_count is not None else 0)
        if qty <= 0:
            # Prefer live order ids count when UI did not pass it.
            oids = supply.get("order_ids") or []
            if isinstance(oids, list) and oids:
                qty = len(oids)
        if qty > 0:
            max_total = qty + 1
            remaining = max(0, max_total - len(boxes))
            if n > remaining:
                raise ValueError(
                    "Можно добавить не больше {} грузомест "
                    "(лимит = заказы + 1)".format(remaining)
                )
        client = WbFbsClient(api_key)
        ids = client.create_supply_boxes(supply_id, n)
        self.refresh(source_id, api_key, supply_id)
        return ids

    def delete_all(self, source_id: int, api_key: str, supply_id: str) -> None:
        from app.services.orders import OrdersService

        supply = OrdersService(self.db).get_supply(source_id, supply_id) or {}
        if bool(supply.get("done")):
            raise ValueError(
                "Поставка уже закрыта — грузоместа удалить нельзя"
            )
        boxes = self.list_boxes(source_id, supply_id)
        ids = []
        for b in boxes:
            if isinstance(b, dict):
                bid = str(b.get("id") or b.get("trbxId") or "").strip()
            else:
                bid = str(b or "").strip()
            if bid:
                ids.append(bid)
        if not ids:
            return
        client = WbFbsClient(api_key)
        client.delete_supply_boxes(supply_id, ids)
        self.refresh(source_id, api_key, supply_id)

    def delete_one(
        self, source_id: int, api_key: str, supply_id: str, box_id: str
    ) -> None:
        from app.services.orders import OrdersService

        supply = OrdersService(self.db).get_supply(source_id, supply_id) or {}
        if bool(supply.get("done")):
            raise ValueError(
                "Поставка уже закрыта — грузоместа удалить нельзя"
            )
        bid = str(box_id or "").strip()
        if not bid:
            raise ValueError("Укажите ID грузоместа")
        client = WbFbsClient(api_key)
        client.delete_supply_boxes(supply_id, [bid])
        self.refresh(source_id, api_key, supply_id)

    def stickers_png(
        self, api_key: str, supply_id: str, box_ids: Optional[List[str]] = None
    ) -> List[bytes]:
        client = WbFbsClient(api_key)
        if box_ids is None:
            # caller should pass ids
            box_ids = []
        out = []  # type: List[bytes]
        for i in range(0, len(box_ids), TRBX_STICKERS_PER_REQUEST):
            if i:
                time.sleep(0.21)
            chunk = box_ids[i : i + TRBX_STICKERS_PER_REQUEST]
            stickers = client.get_box_stickers(supply_id, chunk, sticker_type="png")
            for st in stickers:
                b64 = st.get("file") if isinstance(st, dict) else None
                if isinstance(b64, str) and b64.strip():
                    out.append(base64.b64decode(b64))
        return out


class StickersService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def order_stickers_png(
        self, api_key: str, order_ids: List[int]
    ) -> List[Dict[str, Any]]:
        client = WbFbsClient(api_key)
        out = []  # type: List[Dict[str, Any]]
        for i in range(0, len(order_ids), 100):
            if i:
                time.sleep(0.21)
            chunk = order_ids[i : i + 100]
            stickers = client.get_order_stickers(chunk, sticker_type="png")
            for st in stickers:
                if not isinstance(st, dict):
                    continue
                b64 = st.get("file")
                png = base64.b64decode(b64) if isinstance(b64, str) and b64.strip() else None
                out.append(
                    {
                        "order_id": st.get("orderId") or st.get("order_id"),
                        "partA": st.get("partA") or "",
                        "partB": st.get("partB") or "",
                        "barcode": st.get("barcode") or "",
                        "png": png,
                    }
                )
        return out

    def supply_qr_png(self, api_key: str, supply_id: str) -> bytes:
        client = WbFbsClient(api_key)
        return client.get_supply_barcode(supply_id, sticker_type="png")
