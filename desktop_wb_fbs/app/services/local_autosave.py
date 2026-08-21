# -*- coding: utf-8 -*-
"""Coalesced local SQLite autosave off the UI / scan path (web portal parity)."""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple


class LocalAutosaveQueue:
    """Fire-and-forget coalesced local saves — never blocks the scan path.

    Rapid scans overwrite pending payloads for the same order; a short debounce
    flushes to SQLite on a background thread (like web ``ScheduleLocalAutosave``).
    """

    def __init__(self, db: Any) -> None:
        self.db = db
        self._lock = threading.Lock()
        self._kiz = {}  # type: Dict[Tuple[int, int], List[str]]
        self._pick = {}  # type: Dict[Tuple[int, int], Tuple[bool, str]]
        self._flush_lock = threading.Lock()

    def schedule_kiz(self, source_id: int, order_id: int, codes: List[str]) -> None:
        key = (int(source_id), int(order_id))
        cleaned = [str(c).strip(" \t\r\n") for c in (codes or []) if str(c).strip(" \t\r\n")]
        with self._lock:
            self._kiz[key] = cleaned

    def schedule_pick(
        self, source_id: int, order_id: int, verified: bool, barcode: str = ""
    ) -> None:
        key = (int(source_id), int(order_id))
        with self._lock:
            self._pick[key] = (bool(verified), str(barcode or "").strip())

    def flush_async(self) -> None:
        threading.Thread(target=self._flush, name="local-autosave", daemon=True).start()

    def flush_sync(self) -> None:
        self._flush()

    def _take_pending(
        self,
    ) -> Tuple[
        Dict[Tuple[int, int], List[str]],
        Dict[Tuple[int, int], Tuple[bool, str]],
    ]:
        with self._lock:
            kiz = self._kiz
            pick = self._pick
            self._kiz = {}
            self._pick = {}
        return kiz, pick

    def _flush(self) -> None:
        # Serialize flushes so overlapping timers don't interleave writes oddly.
        with self._flush_lock:
            kiz, pick = self._take_pending()
            if not kiz and not pick:
                return
            try:
                from app.services.kiz_pick import KizService, PickVerifyService

                if kiz:
                    svc = KizService(self.db)
                    for (source_id, order_id), codes in kiz.items():
                        try:
                            svc.save_local(source_id, order_id, codes, wb_synced=False)
                        except Exception:
                            pass
                if pick:
                    svc = PickVerifyService(self.db)
                    for (source_id, order_id), (verified, barcode) in pick.items():
                        try:
                            svc.save(source_id, order_id, verified, barcode)
                        except Exception:
                            pass
            except Exception:
                pass
