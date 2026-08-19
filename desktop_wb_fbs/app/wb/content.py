# -*- coding: utf-8 -*-
"""WB Content API — card titles/colors for picking & stickers."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_log = logging.getLogger(__name__)
WB_CONTENT_API = "https://content-api.wildberries.ru"


class WbContentClient:
    def __init__(self, api_key: str, timeout: int = 30) -> None:
        self.api_key = str(api_key or "").strip()
        self.timeout = timeout

    def get_cards_by_nm_ids(self, nm_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Map nmId → {title, brand, colors…}. Chunks of 100."""
        ids = []  # type: List[int]
        for x in nm_ids:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                pass
        ids = sorted(set(ids))
        out = {}  # type: Dict[int, Dict[str, Any]]
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            body = {
                "settings": {
                    "cursor": {"limit": len(chunk)},
                    "filter": {"withPhoto": -1, "nmID": chunk},
                }
            }
            try:
                data = self._request(
                    "POST", "/content/v2/get/cards/list", body=body
                )
            except Exception as exc:
                _log.warning("content cards failed: %s", exc)
                continue
            cards = data.get("cards") if isinstance(data, dict) else None
            if not isinstance(cards, list):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                try:
                    nm = int(card.get("nmID"))
                except (TypeError, ValueError):
                    continue
                out[nm] = card
        return out

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        url = "{}{}".format(WB_CONTENT_API, path)
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "FeedPilot-Desktop-WBFBS/0.1",
        }
        req = Request(url, method=method.upper(), headers=headers, data=data)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
                if not payload:
                    return {}
                return json.loads(payload.decode("utf-8"))
        except HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            raise RuntimeError(
                "WB Content HTTP {}: {}".format(exc.code, err_body or exc.reason)
            ) from exc
