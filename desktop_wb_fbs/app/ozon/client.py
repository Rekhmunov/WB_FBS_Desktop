# -*- coding: utf-8 -*-
"""Ozon Seller API FBS client (desktop)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from urllib.error import HTTPError
from urllib.request import Request

from app.ozon import iso_z, lookback_window, normalize_api_key, normalize_client_id
from app.wb.http import urlopen_https

_log = logging.getLogger(__name__)

OZON_API = "https://api-seller.ozon.ru"


class OzonFbsClient:
    def __init__(self, client_id: str, api_key: str, timeout: int = 30) -> None:
        self.client_id = normalize_client_id(client_id)
        self.api_key = normalize_api_key(api_key)
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        if not self.client_id or not self.api_key:
            raise ValueError("Укажите Client-Id и Api-Key Ozon")
        url = "{}{}".format(OZON_API, path)
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "FeedPilot-Desktop-OzonFBS/0.1",
        }
        req = Request(url, method=method.upper(), headers=headers, data=data)
        try:
            with urlopen_https(req, timeout=self.timeout) as resp:
                payload = resp.read()
                if not payload:
                    return {}
                return json.loads(payload.decode("utf-8"))
        except HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError(
                "Ozon FBS HTTP {}: {}".format(exc.code, err_body or exc.reason)
            ) from exc

    def ping(self) -> Dict[str, Any]:
        """Lightweight check — list postings for last day."""
        since, to = lookback_window(1)
        data = self._request(
            "POST",
            "/v3/posting/fbs/list",
            {
                "dir": "DESC",
                "filter": {"since": iso_z(since), "to": iso_z(to)},
                "limit": 1,
                "offset": 0,
                "with": {
                    "analytics_data": False,
                    "barcodes": False,
                    "financial_data": False,
                    "translit": False,
                },
            },
        )
        return data if isinstance(data, dict) else {}

    def list_unfulfilled(
        self, *, limit: int = 100, offset: int = 0, cutoff_from: str, cutoff_to: str
    ) -> Tuple[List[Dict[str, Any]], int]:
        data = self._request(
            "POST",
            "/v3/posting/fbs/unfulfilled/list",
            {
                "dir": "ASC",
                "filter": {"cutoff_from": cutoff_from, "cutoff_to": cutoff_to},
                "limit": max(1, min(int(limit), 1000)),
                "offset": max(0, int(offset)),
                "with": {
                    "analytics_data": True,
                    "barcodes": True,
                    "financial_data": False,
                    "translit": False,
                },
            },
        )
        result = data.get("result") if isinstance(data, dict) else {}
        postings = result.get("postings") if isinstance(result, dict) else []
        count = int(result.get("count") or 0) if isinstance(result, dict) else 0
        return list(postings or []), count

    def list_postings(
        self,
        *,
        since: str,
        to: str,
        limit: int = 100,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        filt = {"since": since, "to": to}
        if status:
            filt["status"] = str(status)
        data = self._request(
            "POST",
            "/v3/posting/fbs/list",
            {
                "dir": "DESC",
                "filter": filt,
                "limit": max(1, min(int(limit), 1000)),
                "offset": max(0, int(offset)),
                "with": {
                    "analytics_data": True,
                    "barcodes": True,
                    "financial_data": False,
                    "translit": False,
                },
            },
        )
        result = data.get("result") if isinstance(data, dict) else {}
        postings = result.get("postings") if isinstance(result, dict) else []
        has_next = bool(result.get("has_next")) if isinstance(result, dict) else False
        return list(postings or []), has_next

    def get_posting(self, posting_number: str) -> Dict[str, Any]:
        data = self._request(
            "POST",
            "/v3/posting/fbs/get",
            {
                "posting_number": str(posting_number or "").strip(),
                "with": {
                    "analytics_data": True,
                    "barcodes": True,
                    "financial_data": False,
                    "translit": False,
                },
            },
        )
        result = data.get("result") if isinstance(data, dict) else {}
        return result if isinstance(result, dict) else {}

    def list_carriage_deliveries(self, *, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        data = self._request(
            "POST",
            "/v2/carriage/delivery/list",
            {"limit": max(1, min(int(limit), 999)), "offset": max(0, int(offset))},
        )
        result = data.get("result") if isinstance(data, dict) else []
        return list(result or []) if isinstance(result, list) else []

    def iter_postings_window(
        self, lookback_days: int, *, status: Optional[str] = None, sleep_s: float = 0.25
    ):
        """Yield posting pages; each API window ≤30 days (Ozon PERIOD_IS_TOO_LONG)."""
        end = lookback_window(1)[1]
        remaining = max(1, min(int(lookback_days or 2), 90))
        while remaining > 0:
            span = min(remaining, 30)
            start = end - __import__("datetime").timedelta(days=span)
            since, to = iso_z(start), iso_z(end)
            offset = 0
            while True:
                postings, has_next = self.list_postings(
                    since=since, to=to, limit=100, offset=offset, status=status
                )
                if postings:
                    yield postings
                if not has_next:
                    break
                offset += len(postings)
                time.sleep(sleep_s)
            end = start
            remaining -= span
            time.sleep(sleep_s)
