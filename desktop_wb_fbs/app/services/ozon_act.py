# -*- coding: utf-8 -*-
"""Ozon FBS act flow (create, check-status, get-postings, QR/PDF)."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from app.db import Database
from app.ozon import iso_z, lookback_window, utc_now
from app.ozon.client import OzonFbsClient


_ACT_READY = frozenset({"ready", "formed", "success", "completed"})
_ACT_PENDING = frozenset({"in_process", "pending", "new", "awaiting_retry"})


class OzonActService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get_act_id(self, source_id: int, carriage_id: str) -> str:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT act_id FROM ozon_fbs_carriages
                WHERE source_id = ? AND carriage_id = ?
                """,
                (source_id, str(carriage_id or "").strip()),
            ).fetchone()
        if not row:
            return ""
        return str(row["act_id"] or "").strip()

    def save_act_id(
        self,
        source_id: int,
        carriage_id: str,
        act_id: object,
        *,
        act_status: str = "",
    ) -> None:
        aid = str(act_id or "").strip()
        if not aid:
            return
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE ozon_fbs_carriages
                SET act_id = ?, act_status = ?, synced_at = ?
                WHERE source_id = ? AND carriage_id = ?
                """,
                (aid, str(act_status or ""), utc_now(), source_id, str(carriage_id)),
            )
            conn.commit()

    def resolve_act_id(
        self,
        client: OzonFbsClient,
        source_id: int,
        carriage_id: str,
        *,
        delivery_method_id: Optional[int] = None,
    ) -> str:
        stored = self.get_act_id(source_id, carriage_id)
        if stored:
            return stored
        since, to = lookback_window(7)
        try:
            acts = client.act_list(
                date_from=iso_z(since),
                date_to=iso_z(to),
                limit=50,
            )
        except Exception:
            acts = []
        dm = int(delivery_method_id or 0)
        for act in acts:
            if not isinstance(act, dict):
                continue
            if dm and int(act.get("delivery_method_id") or 0) != dm:
                continue
            aid = str(act.get("id") or "").strip()
            if aid:
                self.save_act_id(
                    source_id,
                    carriage_id,
                    aid,
                    act_status=str(act.get("status") or ""),
                )
                return aid
        return ""

    def create_act(
        self,
        client: OzonFbsClient,
        source_id: int,
        carriage_id: str,
        delivery_method_id: int,
        *,
        departure_date: str = "",
        containers_count: int = 0,
    ) -> int:
        act_id = client.act_create(
            int(delivery_method_id),
            departure_date=departure_date,
            containers_count=containers_count,
        )
        self.save_act_id(source_id, carriage_id, act_id, act_status="in_process")
        return act_id

    def wait_act_ready(
        self,
        client: OzonFbsClient,
        act_id: int,
        *,
        timeout_s: float = 90.0,
        poll_s: float = 2.0,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + max(5.0, float(timeout_s))
        last = {}  # type: Dict[str, Any]
        while time.monotonic() < deadline:
            last = client.act_check_status(int(act_id))
            status = str(last.get("status") or "").strip().lower()
            if status in _ACT_READY:
                return last
            if status in ("error", "cancelled"):
                raise RuntimeError(
                    "Формирование акта завершилось с ошибкой: {}".format(status)
                )
            time.sleep(max(0.5, float(poll_s)))
        raise RuntimeError(
            "Таймаут ожидания акта (последний статус: {})".format(
                last.get("status") or "—"
            )
        )

    def postings_for_carriage(
        self,
        client: OzonFbsClient,
        source_id: int,
        carriage_id: str,
        *,
        delivery_method_id: Optional[int] = None,
    ) -> List[str]:
        act_id = self.resolve_act_id(
            client, source_id, carriage_id, delivery_method_id=delivery_method_id
        )
        if act_id:
            try:
                pnums = client.act_get_postings(int(act_id))
                if pnums:
                    return pnums
            except Exception:
                pass
        return []

    def fetch_barcode(
        self,
        client: OzonFbsClient,
        source_id: int,
        carriage_id: str,
        *,
        delivery_method_id: Optional[int] = None,
        create_if_missing: bool = True,
    ) -> Tuple[bytes, str]:
        act_id = self.resolve_act_id(
            client, source_id, carriage_id, delivery_method_id=delivery_method_id
        )
        if not act_id and create_if_missing:
            if not delivery_method_id:
                raise RuntimeError("Укажите метод доставки для создания акта")
            act_id = str(
                self.create_act(
                    client,
                    source_id,
                    carriage_id,
                    int(delivery_method_id),
                )
            )
        if not act_id:
            raise RuntimeError("Нет ID акта отгрузки — сформируйте акт")
        self.wait_act_ready(client, int(act_id))
        content, name = client.act_get_barcode(int(act_id))
        self.save_act_id(source_id, carriage_id, act_id, act_status="ready")
        return content, name

    def fetch_act_pdf(
        self,
        client: OzonFbsClient,
        source_id: int,
        carriage_id: str,
        *,
        delivery_method_id: Optional[int] = None,
    ) -> Tuple[bytes, str]:
        act_id = self.resolve_act_id(
            client, source_id, carriage_id, delivery_method_id=delivery_method_id
        )
        if not act_id:
            raise RuntimeError("Нет ID акта — сначала сформируйте акт")
        self.wait_act_ready(client, int(act_id))
        content, name = client.act_get_pdf(int(act_id))
        return content, name
