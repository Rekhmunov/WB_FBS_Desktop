# -*- coding: utf-8 -*-
"""Per-carriage preload session (Ozon FBS, parallel to supply_session)."""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.services.ozon_mark_pick import OzonMarkService
from app.services.ozon_orders import OzonOrdersService

_SESSION_TTL_SEC = 300.0
_lock = threading.Lock()
_sessions = {}  # type: Dict[Tuple[int, str], Tuple[float, "OzonCarriageSession"]]


def _key(source_id: int, carriage_id: str) -> Tuple[int, str]:
    return (int(source_id), str(carriage_id or "").strip())


def get_session(source_id: int, carriage_id: str) -> Optional["OzonCarriageSession"]:
    key = _key(source_id, carriage_id)
    with _lock:
        item = _sessions.get(key)
        if not item:
            return None
        ts, session = item
        if (time.monotonic() - ts) > _SESSION_TTL_SEC:
            _sessions.pop(key, None)
            return None
        return session


def put_session(session: "OzonCarriageSession") -> None:
    if not session or not session.carriage_id:
        return
    with _lock:
        _sessions[_key(session.source_id, session.carriage_id)] = (
            time.monotonic(),
            session,
        )


def invalidate(source_id: int, carriage_id: str) -> None:
    with _lock:
        _sessions.pop(_key(source_id, carriage_id), None)


class OzonCarriageSession:
    def __init__(self, source_id: int, carriage_id: str) -> None:
        self.source_id = int(source_id)
        self.carriage_id = str(carriage_id or "").strip()
        self.carriage = None  # type: Optional[Dict[str, Any]]
        self.rows = []  # type: List[Dict[str, Any]]
        self.mark_rows = []  # type: List[Dict[str, Any]]

    def load(
        self,
        db: Database,
        client: OzonFbsClient,
        *,
        refresh: bool = True,
    ) -> None:
        orders = OzonOrdersService(db)
        if refresh:
            orders.refresh_carriage(self.source_id, client, self.carriage_id)
        self.carriage = orders.get_carriage(self.source_id, self.carriage_id)
        self.rows = orders.postings_in_carriage(self.source_id, self.carriage_id)
        self.mark_rows = OzonMarkService(db).marking_rows(
            self.source_id, self.carriage_id, client=client
        )
