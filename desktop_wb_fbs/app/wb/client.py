# -*- coding: utf-8 -*-
"""Wildberries Marketplace API client (desktop, no FeedPilot web)."""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.wb import kiz_code_clean

_log = logging.getLogger(__name__)

WB_FBS_API = "https://marketplace-api.wildberries.ru"
TRBX_STICKERS_PER_REQUEST = 100
TRBX_DELETE_PER_REQUEST = 100


class WbFbsClient:
    def __init__(self, api_key: str, timeout: int = 30) -> None:
        self.api_key = str(api_key or "").strip()
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, object]] = None,
        body: Any = None,
        raw: bool = False,
    ) -> Any:
        qs = ""
        if params:
            qs = "?" + urlencode({k: v for k, v in params.items() if v is not None})
        url = "{}{}{}".format(WB_FBS_API, path, qs)
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": self.api_key,
            "Accept": "application/json",
            "User-Agent": "FeedPilot-Desktop-WBFBS/0.1",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = Request(url, method=method.upper(), headers=headers, data=data)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
                if raw:
                    return payload, dict(resp.headers), resp.status
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
                "WB FBS HTTP {}: {}".format(exc.code, err_body or exc.reason)
            ) from exc

    def get_new_orders(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/api/v3/orders/new")
        orders = data.get("orders") if isinstance(data, dict) else None
        return list(orders or []) if isinstance(orders, list) else []

    def get_orders_page(
        self,
        limit: int = 1000,
        next_token: Optional[int] = 0,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        params = {
            "limit": max(1, min(int(limit), 1000)),
            "next": int(next_token or 0),
        }  # type: Dict[str, object]
        if date_from is not None:
            params["dateFrom"] = int(date_from.timestamp())
        if date_to is not None:
            params["dateTo"] = int(date_to.timestamp())
        data = self._request("GET", "/api/v3/orders", params=params)
        if not isinstance(data, dict):
            return [], None
        orders = data.get("orders") if isinstance(data.get("orders"), list) else []
        nxt = data.get("next")
        try:
            next_val = int(nxt) if nxt is not None else None
        except (TypeError, ValueError):
            next_val = None
        if next_val == 0:
            next_val = None
        return list(orders), next_val

    def get_statuses(self, order_ids: List[int]) -> List[Dict[str, Any]]:
        if not order_ids:
            return []
        data = self._request("POST", "/api/v3/orders/status", body={"orders": order_ids})
        orders = data.get("orders") if isinstance(data, dict) else None
        return list(orders or []) if isinstance(orders, list) else []

    def get_supplies(
        self, limit: int = 1000, next_token: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        data = self._request(
            "GET", "/api/v3/supplies", params={"limit": limit, "next": next_token}
        )
        if not isinstance(data, dict):
            return [], 0
        supplies = data.get("supplies") if isinstance(data.get("supplies"), list) else []
        try:
            nxt = int(data.get("next") or 0)
        except (TypeError, ValueError):
            nxt = 0
        return list(supplies), nxt

    def get_supply_order_ids(self, supply_id: str) -> List[int]:
        for path in (
            "/api/marketplace/v3/supplies/{}/order-ids".format(supply_id),
            "/api/v3/supplies/{}/orders".format(supply_id),
        ):
            try:
                data = self._request("GET", path)
                if isinstance(data, dict):
                    if isinstance(data.get("orderIds"), list):
                        return [int(x) for x in data["orderIds"]]
                    if isinstance(data.get("orders"), list):
                        ids = []  # type: List[int]
                        for item in data["orders"]:
                            if isinstance(item, dict) and item.get("id") is not None:
                                ids.append(int(item["id"]))
                            elif isinstance(item, (int, str)):
                                ids.append(int(item))
                        return ids
                if isinstance(data, list):
                    return [
                        int(x.get("id") if isinstance(x, dict) else x) for x in data
                    ]
            except Exception as exc:
                _log.debug("get_supply_order_ids %s failed: %s", path, exc)
        return []

    def get_order_stickers(
        self,
        order_ids: List[int],
        sticker_type: str = "png",
        width: int = 58,
        height: int = 40,
    ) -> List[Dict[str, Any]]:
        if not order_ids:
            return []
        data = self._request(
            "POST",
            "/api/v3/orders/stickers",
            params={"type": sticker_type, "width": width, "height": height},
            body={"orders": order_ids},
        )
        stickers = data.get("stickers") if isinstance(data, dict) else None
        return list(stickers or []) if isinstance(stickers, list) else []

    def get_orders_meta(self, order_ids: List[int]) -> List[Dict[str, Any]]:
        ids = [int(x) for x in order_ids if x is not None]
        if not ids:
            return []
        out = []  # type: List[Dict[str, Any]]
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            data = self._request(
                "POST",
                "/api/marketplace/v3/orders/meta",
                body={"orders": chunk},
            )
            orders = data.get("orders") if isinstance(data, dict) else None
            if isinstance(orders, list):
                out.extend(o for o in orders if isinstance(o, dict))
            if i + 100 < len(ids):
                time.sleep(0.21)
        return out

    def set_order_sgtin(self, order_id: int, sgtins: List[str]) -> None:
        codes = [kiz_code_clean(x) for x in (sgtins or []) if kiz_code_clean(x)]
        if not codes:
            raise ValueError("Укажите хотя бы один код КИЗ (sgtin)")
        self._request(
            "PUT",
            "/api/v3/orders/{}/meta/sgtin".format(int(order_id)),
            body={"sgtins": codes},
        )

    def delete_order_meta(self, order_id: int, key: str) -> None:
        meta_key = str(key or "").strip()
        if not meta_key:
            raise ValueError("Укажите ключ метаданных")
        self._request(
            "DELETE",
            "/api/v3/orders/{}/meta".format(int(order_id)),
            params={"key": meta_key},
        )

    def create_supply(self, name: str) -> Dict[str, Any]:
        title = str(name or "").strip()
        if not title:
            raise ValueError("Укажите название поставки")
        data = self._request("POST", "/api/v3/supplies", body={"name": title})
        return data if isinstance(data, dict) else {}

    def add_orders_to_supply(self, supply_id: str, order_ids: List[int]) -> None:
        sid = str(supply_id or "").strip()
        ids = [int(x) for x in order_ids if x is not None]
        if not sid:
            raise ValueError("Не указан ID поставки")
        if not ids:
            return
        if len(ids) > 100:
            raise ValueError("За один запрос можно добавить не более 100 заказов")
        self._request(
            "PATCH",
            "/api/marketplace/v3/supplies/{}/orders".format(sid),
            body={"orders": ids},
        )

    def get_supply(self, supply_id: str) -> Dict[str, Any]:
        data = self._request("GET", "/api/v3/supplies/{}".format(supply_id))
        return data if isinstance(data, dict) else {}

    def get_supply_barcode(self, supply_id: str, sticker_type: str = "png") -> bytes:
        payload, _headers, _status = self._request(
            "GET",
            "/api/v3/supplies/{}/barcode".format(supply_id),
            params={"type": sticker_type},
            raw=True,
        )
        try:
            parsed = json.loads(payload.decode("utf-8"))
            if isinstance(parsed, dict):
                b64 = parsed.get("file")
                if isinstance(b64, str) and b64.strip():
                    return base64.b64decode(b64)
        except Exception:
            pass
        raw = bytes(payload)
        if (
            raw[:8] == b"\x89PNG\r\n\x1a\n"
            or raw[:5] == b"%PDF-"
            or raw.lstrip().startswith(b"<")
        ):
            return raw
        raise RuntimeError("WB не вернул файл стикера поставки {}".format(supply_id))

    def get_supply_boxes(self, supply_id: str) -> List[Dict[str, Any]]:
        for path in (
            "/api/v3/supplies/{}/trbx".format(supply_id),
            "/api/marketplace/v3/supplies/{}/trbx".format(supply_id),
        ):
            try:
                data = self._request("GET", path)
                if isinstance(data, dict) and isinstance(data.get("trbxes"), list):
                    return list(data["trbxes"])
                if isinstance(data, dict) and isinstance(data.get("boxes"), list):
                    return list(data["boxes"])
                if isinstance(data, list):
                    return list(data)
            except Exception as exc:
                _log.debug("get_supply_boxes %s failed: %s", path, exc)
        return []

    def create_supply_boxes(self, supply_id: str, amount: int) -> List[str]:
        sid = str(supply_id or "").strip()
        if not sid:
            raise ValueError("Не указан ID поставки")
        try:
            n = int(amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("Укажите количество коробов") from exc
        if n < 1 or n > 1000:
            raise ValueError("Количество коробов: от 1 до 1000")
        data = self._request(
            "POST",
            "/api/v3/supplies/{}/trbx".format(sid),
            body={"amount": n},
        )
        ids = data.get("trbxIds") if isinstance(data, dict) else None
        if not isinstance(ids, list):
            return []
        return [str(x).strip() for x in ids if str(x or "").strip()]

    def delete_supply_boxes(self, supply_id: str, box_ids: List[str]) -> None:
        sid = str(supply_id or "").strip()
        if not sid:
            raise ValueError("Не указан ID поставки")
        ids = [str(x).strip() for x in (box_ids or []) if str(x or "").strip()]
        if not ids:
            raise ValueError("Укажите ID грузомест для удаления")
        chunk = max(1, int(TRBX_DELETE_PER_REQUEST))
        for i in range(0, len(ids), chunk):
            if i:
                time.sleep(0.21)
            self._request(
                "DELETE",
                "/api/v3/supplies/{}/trbx".format(sid),
                body={"trbxIds": ids[i : i + chunk]},
            )

    def get_box_stickers(
        self,
        supply_id: str,
        box_ids: List[str],
        sticker_type: str = "png",
    ) -> List[Dict[str, Any]]:
        if not box_ids:
            return []
        ids = [str(x).strip() for x in box_ids if str(x or "").strip()]
        if not ids:
            return []
        if len(ids) > TRBX_STICKERS_PER_REQUEST:
            raise ValueError(
                "Не больше {} грузомест за один запрос стикеров".format(
                    TRBX_STICKERS_PER_REQUEST
                )
            )
        data = self._request(
            "POST",
            "/api/v3/supplies/{}/trbx/stickers".format(supply_id),
            params={"type": sticker_type},
            body={"trbxIds": ids},
        )
        stickers = data.get("stickers") if isinstance(data, dict) else None
        return list(stickers or []) if isinstance(stickers, list) else []
