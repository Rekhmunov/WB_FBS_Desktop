# -*- coding: utf-8 -*-
"""In-memory cache for supply detail modal (web `_detail_cache` parity)."""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_DETAIL_TTL_SEC = 120.0
_cache = {}  # type: Dict[Tuple[int, str], Tuple[float, Dict[str, Any]]]
_lock = threading.Lock()


def get(source_id: int, supply_id: str) -> Optional[Dict[str, Any]]:
    key = (int(source_id), str(supply_id or "").strip())
    if not key[1]:
        return None
    with _lock:
        item = _cache.get(key)
        if not item:
            return None
        ts, payload = item
        if (time.monotonic() - ts) > _DETAIL_TTL_SEC:
            _cache.pop(key, None)
            return None
        return copy.deepcopy(payload)


def put(source_id: int, supply_id: str, payload: Dict[str, Any]) -> None:
    sid = str(supply_id or "").strip()
    if not sid or not payload:
        return
    with _lock:
        _cache[(int(source_id), sid)] = (time.monotonic(), copy.deepcopy(payload))


def invalidate(source_id: int, supply_id: str) -> None:
    sid = str(supply_id or "").strip()
    if not sid:
        return
    with _lock:
        _cache.pop((int(source_id), sid), None)
