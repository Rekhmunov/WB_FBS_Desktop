# -*- coding: utf-8 -*-
"""Ozon FBS act flow (create, check-status, get-postings, QR/PDF)."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import json

from app.db import Database
from app.ozon import iso_z, lookback_window, utc_now
from app.ozon.client import OzonFbsClient


_ACT_READY = frozenset({"ready", "formed", "success", "completed"})
_ACT_PENDING = frozenset({"in_process", "pending", "new", "awaiting_retry"})


def _extract_act_id_from_carriage(info: object) -> str:
    if not isinstance(info, dict):
        return ""
    for key in ("act_id", "shipping_act_id", "document_id"):
        val = info.get(key)
        if val not in (None, "", 0):
            return str(val).strip()
    act = info.get("act")
    if isinstance(act, dict):
        return str(act.get("id") or "").strip()
    return ""


def _local_posting_numbers(db: Database, source_id: int, carriage_id: str) -> List[str]:
    cid = str(carriage_id or "").strip()
    if not cid:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT posting_number FROM ozon_fbs_postings
            WHERE source_id = ? AND carriage_id = ?
            """,
            (source_id, cid),
        ).fetchall()
    out = [str(r["posting_number"] or "").strip() for r in rows]
    out = [p for p in out if p]
    if out:
        return out
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT posting_numbers_json FROM ozon_fbs_carriages
            WHERE source_id = ? AND carriage_id = ?
            """,
            (source_id, cid),
        ).fetchone()
    if not row:
        return []
    try:
        data = json.loads(str(row["posting_numbers_json"] or "[]"))
        if isinstance(data, list):
            return [str(p).strip() for p in data if str(p).strip()]
    except Exception:
        pass
    return []


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

    def capture_act_from_carriage(
        self,
        client: OzonFbsClient,
        source_id: int,
        carriage_id: str,
        *,
        carriage_info: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Persist act id after approve / get-carriage when Ozon exposes it."""
        cid = str(carriage_id or "").strip()
        if not cid:
            return ""
        info = carriage_info
        if info is None:
            try:
                info = client.get_carriage(int(cid))
            except Exception:
                info = {}
        aid = _extract_act_id_from_carriage(info)
        if aid:
            self.save_act_id(
                source_id,
                cid,
                aid,
                act_status=str((info or {}).get("status") or ""),
            )
            return aid
        try:
            status = client.act_check_status(int(cid))
            if isinstance(status, dict) and status.get("status"):
                self.save_act_id(
                    source_id,
                    cid,
                    cid,
                    act_status=str(status.get("status") or ""),
                )
                return cid
        except Exception:
            pass
        return self.get_act_id(source_id, cid)

    def _try_act_id(
        self,
        client: OzonFbsClient,
        source_id: int,
        carriage_id: str,
        act_id: str,
    ) -> bool:
        aid = str(act_id or "").strip()
        if not aid:
            return False
        try:
            status = client.act_check_status(int(aid))
            if isinstance(status, dict) and status.get("status"):
                self.save_act_id(
                    source_id,
                    carriage_id,
                    aid,
                    act_status=str(status.get("status") or ""),
                )
                return True
        except Exception:
            pass
        try:
            client.act_get_postings(int(aid))
            self.save_act_id(source_id, carriage_id, aid)
            return True
        except Exception:
            return False

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
        cid = str(carriage_id or "").strip()
        if not cid:
            return ""
        if self._try_act_id(client, source_id, cid, cid):
            return self.get_act_id(source_id, cid)
        try:
            info = client.get_carriage(int(cid))
            aid = _extract_act_id_from_carriage(info)
            if aid and self._try_act_id(client, source_id, cid, aid):
                return aid
        except Exception:
            info = {}
        local_pnums = set(_local_posting_numbers(self.db, source_id, cid))
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
        best_aid = ""
        best_overlap = 0
        for act in acts:
            if not isinstance(act, dict):
                continue
            if dm and int(act.get("delivery_method_id") or 0) != dm:
                continue
            aid = str(act.get("id") or "").strip()
            if not aid:
                continue
            overlap = 0
            if local_pnums:
                try:
                    act_pnums = set(client.act_get_postings(int(aid)))
                    overlap = len(local_pnums & act_pnums)
                    if overlap == len(local_pnums):
                        self.save_act_id(
                            source_id,
                            cid,
                            aid,
                            act_status=str(act.get("status") or ""),
                        )
                        return aid
                except Exception:
                    overlap = 0
            if overlap > best_overlap:
                best_overlap = overlap
                best_aid = aid
        if best_aid:
            self.save_act_id(source_id, cid, best_aid)
            return best_aid
        for act in acts:
            if not isinstance(act, dict):
                continue
            if dm and int(act.get("delivery_method_id") or 0) != dm:
                continue
            aid = str(act.get("id") or "").strip()
            if aid:
                self.save_act_id(
                    source_id,
                    cid,
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
        cid = str(carriage_id or "").strip()
        if not cid:
            return []
        try:
            pnums = client.act_get_postings(int(cid))
            if pnums:
                self.save_act_id(source_id, cid, cid)
                return pnums
        except Exception:
            pass
        act_id = self.resolve_act_id(
            client, source_id, cid, delivery_method_id=delivery_method_id
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
