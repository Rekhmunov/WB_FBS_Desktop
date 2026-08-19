"""WB FBS orders module — isolated from FBW supplies.

Uses marketplace-api.wildberries.ru and credentials from supply_sources (marketplace=wb).
Sync + stickers + order metadata (КИЗ) + collect MGT orders into assembly supplies.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta, time as dt_time
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .repository import ReviewRepository

_MSK = ZoneInfo("Europe/Moscow")

_log = logging.getLogger(__name__)

WB_FBS_API = "https://marketplace-api.wildberries.ru"
# WB Marketplace: max trbxIds per /trbx/stickers (and /trbx DELETE) request.
TRBX_STICKERS_PER_REQUEST = 100
TRBX_DELETE_PER_REQUEST = 100

# UI tab <- supplierStatus / wbStatus / archive flag
TAB_NEW = "new"
TAB_ASSEMBLY = "assembly"       # confirm
TAB_DELIVERY = "delivery"       # complete
TAB_FINISHED = "finished"       # sold etc.
TAB_CANCELLED = "cancelled"
TAB_ARCHIVE = "archive"

# Hidden for everyone — not used in operator workflow; sync skips archive and
# does not re-poll finished/cancelled statuses. Alias kept for older imports.
HIDDEN_TABS = frozenset({TAB_FINISHED, TAB_CANCELLED, TAB_ARCHIVE})
OWNER_ONLY_TABS = HIDDEN_TABS


def is_hidden_tab(tab: object) -> bool:
    return str(tab or "").strip().lower() in HIDDEN_TABS


def is_owner_only_tab(tab: object) -> bool:
    """Back-compat: same as is_hidden_tab (tabs are hidden for all roles)."""
    return is_hidden_tab(tab)

_FINISHED_WB = {"sold"}
_CANCELLED_SUPPLIER = {"cancel", "cancel_carrier"}
_CANCELLED_WB = {
    "canceled",
    "canceled_by_client",
    "declined_by_client",
    "defect",
    "canceled_by_carrier",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def compute_tab(*, supplier_status: str, wb_status: str, is_archive: bool) -> str:
    if is_archive:
        return TAB_ARCHIVE
    ss = (supplier_status or "").strip().lower()
    ws = (wb_status or "").strip().lower()
    if ss in _CANCELLED_SUPPLIER or ws in _CANCELLED_WB:
        return TAB_CANCELLED
    if ws in _FINISHED_WB:
        return TAB_FINISHED
    if ss == "confirm":
        return TAB_ASSEMBLY
    if ss == "complete":
        return TAB_DELIVERY
    if ss == "new" or not ss:
        return TAB_NEW
    # Fallback by supplier status
    if ss == "new":
        return TAB_NEW
    return TAB_ASSEMBLY if ss else TAB_NEW


def format_price_rub(price_kopecks: object, currency_code: object = 643) -> str:
    try:
        kopecks = int(price_kopecks or 0)
    except (TypeError, ValueError):
        kopecks = 0
    rub = kopecks / 100.0
    if int(currency_code or 643) == 643:
        if rub == int(rub):
            return f"{int(rub)} ₽"
        return f"{rub:.2f} ₽".replace(".", ",")
    return f"{rub:.2f}"


def _as_int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_order_price(order: dict[str, Any]) -> tuple[int, int]:
    """Pick the seller-facing price in kopecks + currency.

    WB /orders often omits finalPrice/convertedFinalPrice and only has
    price + convertedPrice. Prefer converted* (seller country, usually RUB)
    so we never show sale-currency amount as ₽.
    """
    converted_final = _as_int_or_none(order.get("convertedFinalPrice"))
    converted = _as_int_or_none(order.get("convertedPrice"))
    final = _as_int_or_none(order.get("finalPrice"))
    price = _as_int_or_none(order.get("price"))
    converted_ccy = _as_int_or_none(order.get("convertedCurrencyCode")) or 643
    sale_ccy = _as_int_or_none(order.get("currencyCode")) or 643

    if converted_final is not None and converted_final > 0:
        return converted_final, converted_ccy
    if converted is not None and converted > 0:
        return converted, converted_ccy
    if final is not None and final > 0:
        return final, sale_ccy
    if price is not None and price > 0:
        return price, sale_ccy
    # Zero / missing — still prefer converted currency for display.
    if converted_final is not None:
        return converted_final, converted_ccy
    if converted is not None:
        return converted, converted_ccy
    if final is not None:
        return final, sale_ccy
    return int(price or 0), sale_ccy


def cargo_type_label(cargo_type: object) -> str:
    try:
        ct = int(cargo_type or 0)
    except (TypeError, ValueError):
        return ""
    return {1: "МГТ", 2: "СГТ", 3: "КГТ+"}.get(ct, "")


def _coalesce_b2b_flag(obj: dict[str, Any] | None) -> bool | None:
    """Read WB B2B flag. Accepts ``isB2b`` / ``isB2B`` / ``is_b2b``.

    Returns ``None`` when the key is absent or the value is JSON null.
    """
    if not isinstance(obj, dict):
        return None
    for key in ("isB2b", "isB2B", "is_b2b"):
        if key in obj and obj.get(key) is not None:
            return bool(obj.get(key))
    return None


def _order_b2b_flag(order: dict[str, Any] | None) -> bool | None:
    """B2B flag from order payload, or ``None`` if WB omitted it."""
    if not isinstance(order, dict):
        return None
    opts = order.get("options")
    if isinstance(opts, dict):
        flag = _coalesce_b2b_flag(opts)
        if flag is not None:
            return flag
    return _coalesce_b2b_flag(order)


def _order_is_b2b(order: dict[str, Any] | None) -> bool:
    """WB order B2B flag: ``options.isB2B`` / ``options.isB2b`` (bool, default False)."""
    return bool(_order_b2b_flag(order))


def _row_order_is_b2b(row: dict[str, Any]) -> bool:
    if row.get("is_b2b"):
        return True
    raw = _parse_json_obj(row.get("raw_json"))
    if raw:
        return _order_is_b2b(raw)
    return bool(row.get("is_b2b"))


def _is_cancelled_status(*, supplier_status: object = "", wb_status: object = "") -> bool:
    ss = str(supplier_status or "").strip().lower()
    ws = str(wb_status or "").strip().lower()
    return ss in _CANCELLED_SUPPLIER or ws in _CANCELLED_WB


def finished_status_label(*, supplier_status: object = "", wb_status: object = "") -> str:
    """Human label for «Завершённые» (wbStatus=sold)."""
    del supplier_status  # reserved for future supplier-side outcomes
    ws = str(wb_status or "").strip().lower()
    if ws == "sold":
        return "Заказ выкуплен"
    return ""


def order_portal_status_label(*, supplier_status: object = "", wb_status: object = "") -> str:
    """Compact Marketplace status for tables (Вывод КИЗ, etc.)."""
    ss = str(supplier_status or "").strip().lower()
    ws = str(wb_status or "").strip().lower()
    if ws == "sold":
        return "Выкуплен"
    cancel = cancel_reason_label(supplier_status=ss, wb_status=ws)
    if cancel:
        return cancel
    if _is_cancelled_status(supplier_status=ss, wb_status=ws):
        return "Отменён"
    if ss == "complete":
        return "В доставке"
    if ss == "confirm":
        return "На сборке"
    if ss == "new" or (not ss and not ws):
        return "Новый"
    if ss:
        return ss
    return ws or ""


def default_mgt_supply_name(*, is_b2b: bool, when: datetime | None = None) -> str:
    """Default editable title: ``Поставка от ДД.ММ.ГГГГ`` (+ `` B2B``)."""
    from zoneinfo import ZoneInfo

    dt = when or datetime.now(ZoneInfo("Europe/Moscow"))
    base = f"Поставка от {dt.strftime('%d.%m.%Y')}"
    return f"{base} B2B" if is_b2b else base


def supply_status_label(*, done: object = False, scan_dt: object = None) -> str:
    """Map WB supply flags to seller-portal labels («В доставке»).

    WB API has no status string — only done / scanDt / closedAt.
    Important: after PATCH /deliver the API sets done=true (supply closed for
    edits), but the portal still shows «Отгрузите поставку» until scanDt.
    - no scanDt → «Отгрузите поставку»
    - scanDt set → «Поставка в обработке»
    """
    del done  # not used for portal label; kept for call-site compatibility
    if scan_dt:
        return "Поставка в обработке"
    return "Отгрузите поставку"


def assembly_stage_label(*, done: object = False, boxes_count: int = 0) -> str:
    """Portal «Этап сборки» for open supplies on «На сборке».

    API has no stage enum; open (not delivered) supplies show «Сборка заказов».
    """
    del done, boxes_count
    return "Сборка заказов"


def _parse_json_list(raw: object) -> list[Any]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _parse_json_obj(raw: object) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def cancel_reason_label(*, supplier_status: object = "", wb_status: object = "") -> str:
    """Human-readable cancel reason from WB status codes (no free-text in API)."""
    ws = str(wb_status or "").strip().lower()
    ss = str(supplier_status or "").strip().lower()
    if ws == "declined_by_client":
        return "Покупатель в первый час"
    if ws == "canceled_by_client":
        # Seller portal: отказ при получении / на ПВЗ (not pre-assembly cancel).
        return "Отказ на ПВЗ"
    if ws == "defect":
        return "Найдены дефекты"
    if ws == "canceled_by_carrier" or ss == "cancel_carrier":
        return "Перевозчик"
    if ws == "canceled" or ss == "cancel":
        return "Отмена продавцом"
    return ""


SCOPE_ERROR_MESSAGE = "Нет ни одного источника с нужным API (Marketplace)."


def is_fbs_source_name(name: object) -> bool:
    """True when supply source name is meant for FBS (contains ФБС/FBS)."""
    text = str(name or "").casefold()
    return "фбс" in text or "fbs" in text


def is_marketplace_scope_error(exc: object) -> bool:
    """True when WB token has no Marketplace category for FBS API."""
    text = str(exc or "").lower()
    return (
        "token scope not allowed" in text
        or "s2s-api-auth-marketplace" in text
        or ("http 401" in text and "unauthorized" in text and "marketplace" in text)
        or ("http 403" in text and "scope" in text)
    )


def friendly_sync_error(prefix: str, exc: object) -> str:
    """Short UI-safe sync error — never include raw WB JSON bodies."""
    if is_marketplace_scope_error(exc):
        return SCOPE_ERROR_MESSAGE
    text = str(exc or "")
    lower = text.lower()
    if "incorrectparameter" in lower or "incorrect parameter" in lower:
        return f"{prefix}: некорректные параметры запроса к WB"
    if "http 429" in lower:
        return f"{prefix}: превышен лимит запросов WB, попробуйте позже"
    if "http 401" in lower or "http 403" in lower:
        return f"{prefix}: нет доступа к API WB"
    if "http 5" in lower:
        return f"{prefix}: временная ошибка WB API"
    m = re.search(r"HTTP\s+(\d+)", text, flags=re.IGNORECASE)
    if m:
        return f"{prefix}: ошибка WB API (HTTP {m.group(1)})"
    return f"{prefix}: не удалось загрузить данные"


class WbFbsClient:
    def __init__(self, api_key: str, *, timeout: int = 30) -> None:
        self.api_key = str(api_key or "").strip()
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        body: dict[str, object] | list[object] | None = None,
        raw: bool = False,
    ) -> Any:
        qs = ("?" + urlencode({k: v for k, v in (params or {}).items() if v is not None})) if params else ""
        url = f"{WB_FBS_API}{path}{qs}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": self.api_key,
            "Accept": "application/json",
            "User-Agent": "FeedPilot-WBFBS/1.0",
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
            raise RuntimeError(f"WB FBS HTTP {exc.code}: {err_body or exc.reason}") from exc

    def get_new_orders(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v3/orders/new")
        orders = data.get("orders") if isinstance(data, dict) else None
        return list(orders or []) if isinstance(orders, list) else []

    def get_orders_page(
        self,
        *,
        limit: int = 1000,
        next_token: int | None = 0,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        # WB requires limit + next (0 on first page). Omitting next -> IncorrectParameter.
        params: dict[str, object] = {
            "limit": max(1, min(int(limit), 1000)),
            "next": int(next_token or 0),
        }
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
        # WB uses 0 as end marker in some versions
        if next_val == 0:
            next_val = None
        return list(orders), next_val

    def get_statuses(self, order_ids: list[int]) -> list[dict[str, Any]]:
        if not order_ids:
            return []
        data = self._request("POST", "/api/v3/orders/status", body={"orders": order_ids})
        orders = data.get("orders") if isinstance(data, dict) else None
        return list(orders or []) if isinstance(orders, list) else []

    def get_supplies(self, *, limit: int = 1000, next_token: int = 0) -> tuple[list[dict[str, Any]], int]:
        data = self._request("GET", "/api/v3/supplies", params={"limit": limit, "next": next_token})
        if not isinstance(data, dict):
            return [], 0
        supplies = data.get("supplies") if isinstance(data.get("supplies"), list) else []
        try:
            nxt = int(data.get("next") or 0)
        except (TypeError, ValueError):
            nxt = 0
        return list(supplies), nxt

    def get_supply_order_ids(self, supply_id: str) -> list[int]:
        # Prefer marketplace path; fall back to legacy
        for path in (
            f"/api/marketplace/v3/supplies/{supply_id}/order-ids",
            f"/api/v3/supplies/{supply_id}/orders",
        ):
            try:
                data = self._request("GET", path)
                if isinstance(data, dict):
                    if isinstance(data.get("orderIds"), list):
                        return [int(x) for x in data["orderIds"]]
                    if isinstance(data.get("orders"), list):
                        ids: list[int] = []
                        for item in data["orders"]:
                            if isinstance(item, dict) and item.get("id") is not None:
                                ids.append(int(item["id"]))
                            elif isinstance(item, (int, str)):
                                ids.append(int(item))
                        return ids
                if isinstance(data, list):
                    return [int(x.get("id") if isinstance(x, dict) else x) for x in data]
            except Exception as exc:
                _log.debug("get_supply_order_ids %s failed: %s", path, exc)
        return []

    def get_archive_orders(self, *, limit: int = 1000, next_token: int = 0) -> tuple[list[dict[str, Any]], int]:
        for path in (
            "/api/marketplace/v3/fbs/orders/archive",
            "/api/v3/orders/archive",
        ):
            try:
                data = self._request("GET", path, params={"limit": limit, "next": next_token})
                if not isinstance(data, dict):
                    continue
                orders = data.get("orders") if isinstance(data.get("orders"), list) else []
                try:
                    nxt = int(data.get("next") or 0)
                except (TypeError, ValueError):
                    nxt = 0
                return list(orders), nxt
            except Exception as exc:
                _log.debug("archive %s failed: %s", path, exc)
        return [], 0

    def get_order_stickers(
        self,
        order_ids: list[int],
        *,
        sticker_type: str = "png",
        width: int = 58,
        height: int = 40,
    ) -> list[dict[str, Any]]:
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

    def get_orders_meta(self, order_ids: list[int]) -> list[dict[str, Any]]:
        """Batch metadata for assembly orders (КИЗ/sgtin, IMEI, UIN, …).

        Official: ``POST /api/marketplace/v3/orders/meta`` (≤100 ids/request).
        If ``sgtin`` is absent from the response for an order, that order does
        not accept Data Matrix / КИЗ.
        """
        ids = [int(x) for x in order_ids if x is not None]
        if not ids:
            return []
        out: list[dict[str, Any]] = []
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            if not chunk:
                continue
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

    def set_order_sgtin(self, order_id: int, sgtins: list[str]) -> None:
        """Attach Chestny ZNAK Data Matrix codes (КИЗ) to an assembly order.

        Official: ``PUT /api/v3/orders/{orderId}/meta/sgtin``.
        Order must be in confirm status; ``sgtin`` must be allowed for the order.
        """
        # Do not use str.strip() — it removes GS (\\u001D) as whitespace.
        codes = [_kiz_code_clean(x) for x in (sgtins or []) if _kiz_code_clean(x)]
        if not codes:
            raise ValueError("Укажите хотя бы один код КИЗ (sgtin)")
        self._request(
            "PUT",
            f"/api/v3/orders/{int(order_id)}/meta/sgtin",
            body={"sgtins": codes},
        )

    def delete_order_meta(self, order_id: int, key: str) -> None:
        """Remove metadata key from an assembly order (e.g. ``sgtin``)."""
        meta_key = str(key or "").strip()
        if not meta_key:
            raise ValueError("Укажите ключ метаданных")
        self._request(
            "DELETE",
            f"/api/v3/orders/{int(order_id)}/meta",
            params={"key": meta_key},
        )

    def create_supply(self, *, name: str) -> dict[str, Any]:
        """Create supply: ``POST /api/v3/supplies`` → ``{ id }``."""
        title = str(name or "").strip()
        if not title:
            raise ValueError("Укажите название поставки")
        data = self._request("POST", "/api/v3/supplies", body={"name": title})
        return data if isinstance(data, dict) else {}

    def add_orders_to_supply(self, supply_id: str, order_ids: list[int]) -> None:
        """Add up to 100 orders: ``PATCH /api/marketplace/v3/supplies/{id}/orders``."""
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
            f"/api/marketplace/v3/supplies/{sid}/orders",
            body={"orders": ids},
        )

    def get_supply(self, supply_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/api/v3/supplies/{supply_id}")
        return data if isinstance(data, dict) else {}

    def get_supply_barcode(self, supply_id: str, *, sticker_type: str = "png") -> bytes:
        payload, _headers, _status = self._request(
            "GET",
            f"/api/v3/supplies/{supply_id}/barcode",
            params={"type": sticker_type},
            raw=True,
        )
        # WB returns JSON: { barcode: "WB-GI-…", file: "<base64 sticker>" }.
        # Never treat `barcode` (supply id string) as image payload.
        try:
            parsed = json.loads(payload.decode("utf-8"))
            if isinstance(parsed, dict):
                b64 = parsed.get("file")
                if isinstance(b64, str) and b64.strip():
                    return base64.b64decode(b64)
        except Exception:
            pass
        raw = bytes(payload)
        if raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:5] == b"%PDF-" or raw.lstrip().startswith(b"<"):
            return raw
        raise RuntimeError(f"WB не вернул файл стикера поставки {supply_id}")

    def get_supply_boxes(self, supply_id: str) -> list[dict[str, Any]]:
        for path in (
            f"/api/v3/supplies/{supply_id}/trbx",
            f"/api/marketplace/v3/supplies/{supply_id}/trbx",
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

    def create_supply_boxes(self, supply_id: str, amount: int) -> list[str]:
        """Add cargo places: ``POST /api/v3/supplies/{id}/trbx`` → ``trbxIds``."""
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
            f"/api/v3/supplies/{sid}/trbx",
            body={"amount": n},
        )
        ids = data.get("trbxIds") if isinstance(data, dict) else None
        if not isinstance(ids, list):
            return []
        return [str(x).strip() for x in ids if str(x or "").strip()]

    def delete_supply_boxes(self, supply_id: str, box_ids: list[str]) -> None:
        """Remove cargo places: ``DELETE /api/v3/supplies/{id}/trbx`` + ``trbxIds``.

        Chunks by ``TRBX_DELETE_PER_REQUEST`` (WB accepts ≤100 ids per call).
        """
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
                f"/api/v3/supplies/{sid}/trbx",
                body={"trbxIds": ids[i : i + chunk]},
            )

    def get_box_stickers(
        self,
        supply_id: str,
        box_ids: list[str],
        *,
        sticker_type: str = "png",
    ) -> list[dict[str, Any]]:
        """Fetch QR stickers for cargo places.

        WB Marketplace accepts at most ``TRBX_STICKERS_PER_REQUEST`` (100)
        ``trbxIds`` per call — callers must chunk larger lists.
        """
        if not box_ids:
            return []
        ids = [str(x).strip() for x in box_ids if str(x or "").strip()]
        if not ids:
            return []
        if len(ids) > TRBX_STICKERS_PER_REQUEST:
            raise ValueError(
                f"Не больше {TRBX_STICKERS_PER_REQUEST} грузомест за один запрос стикеров"
            )
        data = self._request(
            "POST",
            f"/api/v3/supplies/{supply_id}/trbx/stickers",
            params={"type": sticker_type},
            body={"trbxIds": ids},
        )
        stickers = data.get("stickers") if isinstance(data, dict) else None
        return list(stickers or []) if isinstance(stickers, list) else []


def create_supply_trbx(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    api_key: str,
    supply_id: str,
    amount: int,
    fetch_stickers: bool = True,
    sticker_type: str = "png",
) -> dict[str, Any]:
    """Create N cargo places on WB, refresh local boxes cache, optional stickers."""
    from . import wb_fbs_detail as wb_detail

    ensure_wb_fbs_tables(repo)
    sid = str(supply_id or "").strip()
    if not sid:
        raise ValueError("Не указан ID поставки")
    try:
        n = int(amount)
    except (TypeError, ValueError) as exc:
        raise ValueError("Укажите количество коробов") from exc
    if n < 1 or n > 1000:
        raise ValueError("Количество коробов: от 1 до 1000")

    client = WbFbsClient(api_key)
    try:
        live = client.get_supply(sid)
    except Exception as exc:
        raise RuntimeError(f"Не удалось проверить поставку: {exc}") from exc
    if not isinstance(live, dict) or not live:
        live = {"id": sid}
    if bool(live.get("done")):
        raise ValueError("Поставка уже закрыта — грузоместа добавить нельзя")
    # Ensure upsert_supply can key the row (WB payload uses ``id``).
    if not str(live.get("id") or "").strip():
        live["id"] = sid

    # WB limit: total boxes ≤ items in supply + 1 (not per request).
    existing_boxes: list[dict[str, Any]] = []
    try:
        existing_boxes = client.get_supply_boxes(sid)
    except Exception as exc:
        _log.debug("list boxes before create %s: %s", sid, exc)
    order_ids: list[int] = []
    try:
        time.sleep(0.21)
        order_ids = client.get_supply_order_ids(sid)
    except Exception as exc:
        _log.debug("order ids before create %s: %s", sid, exc)
    if not order_ids:
        order_ids = _local_supply_order_ids(
            repo, user_id=user_id, source_id=source_id, supply_id=sid
        )
    max_total = max(1, len(order_ids) + 1)
    remaining = max_total - len(existing_boxes)
    if remaining <= 0:
        raise ValueError(
            f"Достигнут лимит грузомест для поставки (макс. {max_total} = заказы+1)"
        )
    if n > remaining:
        raise ValueError(
            f"Можно добавить ещё не больше {remaining} грузомест "
            f"(сейчас {len(existing_boxes)}, лимит WB {max_total})"
        )

    try:
        time.sleep(0.21)
        created_ids = client.create_supply_boxes(sid, n)
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
    if not created_ids:
        raise RuntimeError("WB не вернул ID созданных грузомест")

    time.sleep(0.21)
    boxes: list[dict[str, Any]] = []
    try:
        boxes = client.get_supply_boxes(sid)
    except Exception as exc:
        _log.debug("refresh boxes after create %s: %s", sid, exc)
        boxes = list(existing_boxes) + [{"id": bid} for bid in created_ids]

    upsert_supply(
        repo,
        user_id=user_id,
        source_id=source_id,
        supply=live,
        order_ids=order_ids or [],
        boxes=boxes,
    )
    wb_detail.invalidate_supply_detail_cache(
        user_id=user_id, source_id=source_id, supply_id=sid
    )

    stickers: list[dict[str, Any]] = []
    stickers_error = ""
    stype = str(sticker_type or "png").strip().lower()
    if stype not in {"png", "svg", "zplv", "zplh"}:
        stype = "png"
    if fetch_stickers:
        try:
            time.sleep(0.21)
            stickers = fetch_trbx_stickers(
                api_key=api_key,
                supply_id=sid,
                box_ids=created_ids,
                sticker_type=stype,
            )
            if not stickers:
                stickers_error = (
                    "Грузоместа созданы, но WB не вернул стикеры "
                    "(иногда стикеры доступны после распределения заказов по коробам)"
                )
        except Exception as exc:
            _log.warning("trbx stickers after create %s: %s", sid, exc)
            stickers_error = (
                "Грузоместа созданы, но стикеры не получены: "
                f"{friendly_sync_error('stickers', exc)}"
            )

    return {
        "ok": True,
        "supply_id": sid,
        "trbx_ids": created_ids,
        "boxes_count": len(boxes) if boxes else (len(existing_boxes) + len(created_ids)),
        "stickers": stickers,
        "stickers_error": stickers_error,
        "max_total": max_total,
        "remaining_after": max(0, max_total - (len(boxes) if boxes else len(existing_boxes) + len(created_ids))),
    }


def _trbx_box_id(box: object) -> str:
    if isinstance(box, dict):
        return str(box.get("id") or box.get("trbxId") or box.get("barcode") or "").strip()
    return str(box or "").strip()


def list_supply_trbx(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    api_key: str,
    supply_id: str,
) -> dict[str, Any]:
    """Load cargo places from WB, refresh local cache, return list + limits."""
    from . import wb_fbs_detail as wb_detail

    ensure_wb_fbs_tables(repo)
    sid = str(supply_id or "").strip()
    if not sid:
        raise ValueError("Не указан ID поставки")

    client = WbFbsClient(api_key)
    live: dict[str, Any] = {}
    live_ok = False
    try:
        live = client.get_supply(sid)
        live_ok = isinstance(live, dict) and bool(live)
    except Exception as exc:
        _log.debug("list_supply_trbx get_supply %s: %s", sid, exc)
        live = {}
    if live_ok and not str(live.get("id") or "").strip():
        live["id"] = sid

    try:
        time.sleep(0.21)
        boxes_raw = client.get_supply_boxes(sid)
    except Exception as exc:
        raise RuntimeError(f"Не удалось загрузить грузоместа: {exc}") from exc

    boxes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for b in boxes_raw or []:
        bid = _trbx_box_id(b)
        if not bid or bid in seen:
            continue
        seen.add(bid)
        if isinstance(b, dict):
            row = dict(b)
            row["id"] = bid
            boxes.append(row)
        else:
            boxes.append({"id": bid})

    order_ids: list[int] = []
    try:
        time.sleep(0.21)
        order_ids = client.get_supply_order_ids(sid)
    except Exception as exc:
        _log.debug("list_supply_trbx order ids %s: %s", sid, exc)
    if not order_ids:
        order_ids = _local_supply_order_ids(
            repo, user_id=user_id, source_id=source_id, supply_id=sid
        )

    # Never upsert a stub supply: failed get_supply would wipe name/done.
    if live_ok:
        upsert_supply(
            repo,
            user_id=user_id,
            source_id=source_id,
            supply=live,
            order_ids=order_ids or [],
            boxes=boxes,
        )
    else:
        _persist_supply_boxes(
            repo,
            user_id=user_id,
            source_id=source_id,
            supply_id=sid,
            boxes=boxes,
            allow_empty=True,
        )
    # upsert_supply keeps previous boxes_json when new list is []; force live truth.
    if live_ok and not boxes:
        _persist_supply_boxes(
            repo,
            user_id=user_id,
            source_id=source_id,
            supply_id=sid,
            boxes=[],
            allow_empty=True,
        )
    wb_detail.invalidate_supply_detail_cache(
        user_id=user_id, source_id=source_id, supply_id=sid
    )

    max_total = max(1, len(order_ids) + 1)
    remaining = max(0, max_total - len(boxes))
    return {
        "ok": True,
        "supply_id": sid,
        "done": bool(live.get("done")) if live_ok else False,
        "boxes": [{"id": str(b.get("id") or "")} for b in boxes if str(b.get("id") or "")],
        "boxes_count": len(boxes),
        "order_count": len(order_ids),
        "max_total": max_total,
        "remaining": remaining,
    }


def delete_supply_trbx(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    api_key: str,
    supply_id: str,
    box_ids: list[str],
) -> dict[str, Any]:
    """Delete cargo places on WB and return the refreshed live list."""
    sid = str(supply_id or "").strip()
    if not sid:
        raise ValueError("Не указан ID поставки")
    ids = [str(x).strip() for x in (box_ids or []) if str(x or "").strip()]
    if not ids:
        raise ValueError("Укажите ID грузомест для удаления")

    client = WbFbsClient(api_key)
    try:
        live = client.get_supply(sid)
    except Exception as exc:
        raise RuntimeError(f"Не удалось проверить поставку: {exc}") from exc
    if isinstance(live, dict) and bool(live.get("done")):
        raise ValueError("Поставка уже закрыта — грузоместа удалить нельзя")

    try:
        time.sleep(0.21)
        client.delete_supply_boxes(sid, ids)
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    # Always return fresh list after delete (also refreshes local cache).
    return list_supply_trbx(
        repo,
        user_id=user_id,
        source_id=source_id,
        api_key=api_key,
        supply_id=sid,
    )


def fetch_trbx_stickers(
    *,
    api_key: str,
    supply_id: str,
    box_ids: list[str] | None = None,
    sticker_type: str = "png",
) -> list[dict[str, Any]]:
    """Fetch trbx stickers from WB for given ids (or all boxes on the supply).

    Chunks requests by ``TRBX_STICKERS_PER_REQUEST`` (WB hard limit 100).
    """
    sid = str(supply_id or "").strip()
    if not sid:
        raise ValueError("Не указан ID поставки")
    stype = str(sticker_type or "png").strip().lower()
    if stype not in {"png", "svg", "zplv", "zplh"}:
        stype = "png"
    client = WbFbsClient(api_key)
    ids = [str(x).strip() for x in (box_ids or []) if str(x or "").strip()]
    if not ids:
        try:
            boxes = client.get_supply_boxes(sid)
        except Exception as exc:
            raise RuntimeError(f"Не удалось загрузить грузоместа: {exc}") from exc
        ids = []
        seen: set[str] = set()
        for b in boxes or []:
            bid = _trbx_box_id(b)
            if bid and bid not in seen:
                seen.add(bid)
                ids.append(bid)
    if not ids:
        raise ValueError("Грузоместа не найдены")
    stickers: list[dict[str, Any]] = []
    chunk = max(1, int(TRBX_STICKERS_PER_REQUEST))
    try:
        for i in range(0, len(ids), chunk):
            if i:
                time.sleep(0.21)
            part = client.get_box_stickers(
                sid, ids[i : i + chunk], sticker_type=stype
            )
            stickers.extend(part)
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
    if not stickers:
        raise RuntimeError(
            "WB не вернул стикеры грузомест "
            "(иногда они доступны после распределения заказов по коробам)"
        )
    return stickers


def ensure_wb_fbs_tables(repo: ReviewRepository) -> None:
    with repo._connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wb_fbs_orders (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                source_id BIGINT NOT NULL,
                order_id BIGINT NOT NULL,
                order_uid TEXT NOT NULL DEFAULT '',
                article TEXT NOT NULL DEFAULT '',
                nm_id BIGINT,
                chrt_id BIGINT,
                skus_json TEXT NOT NULL DEFAULT '[]',
                price BIGINT NOT NULL DEFAULT 0,
                final_price BIGINT NOT NULL DEFAULT 0,
                currency_code INTEGER NOT NULL DEFAULT 643,
                warehouse_id BIGINT,
                office_id BIGINT,
                offices_json TEXT NOT NULL DEFAULT '[]',
                cargo_type INTEGER NOT NULL DEFAULT 0,
                delivery_type TEXT NOT NULL DEFAULT '',
                supplier_status TEXT NOT NULL DEFAULT '',
                wb_status TEXT NOT NULL DEFAULT '',
                tab TEXT NOT NULL DEFAULT 'new',
                supply_id TEXT NOT NULL DEFAULT '',
                is_archive BOOLEAN NOT NULL DEFAULT FALSE,
                comment_text TEXT NOT NULL DEFAULT '',
                created_at_wb TIMESTAMPTZ,
                raw_json TEXT NOT NULL DEFAULT '{}',
                synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, source_id, order_id)
            )
            """
        )
        conn.execute(
            repo._sql(
                "CREATE INDEX IF NOT EXISTS idx_wb_fbs_orders_user_src_tab "
                "ON wb_fbs_orders(user_id, source_id, tab, created_at_wb DESC)"
            )
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wb_fbs_supplies (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                source_id BIGINT NOT NULL,
                supply_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                done BOOLEAN NOT NULL DEFAULT FALSE,
                cargo_type INTEGER NOT NULL DEFAULT 0,
                destination_office_id BIGINT,
                created_at_wb TIMESTAMPTZ,
                closed_at_wb TIMESTAMPTZ,
                scan_dt TIMESTAMPTZ,
                order_ids_json TEXT NOT NULL DEFAULT '[]',
                boxes_json TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL DEFAULT '{}',
                synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, source_id, supply_id)
            )
            """
        )
        conn.execute(
            repo._sql(
                "CREATE INDEX IF NOT EXISTS idx_wb_fbs_supplies_user_src "
                "ON wb_fbs_supplies(user_id, source_id, done, created_at_wb DESC)"
            )
        )
        # Local КИЗ draft/copy: kept even when WB API fails (retry on next Save).
        conn.execute(
            """
            ALTER TABLE wb_fbs_orders
            ADD COLUMN IF NOT EXISTS kiz_codes_json TEXT NOT NULL DEFAULT '[]'
            """
        )
        conn.execute(
            """
            ALTER TABLE wb_fbs_orders
            ADD COLUMN IF NOT EXISTS kiz_saved_at TIMESTAMPTZ
            """
        )
        conn.execute(
            """
            ALTER TABLE wb_fbs_orders
            ADD COLUMN IF NOT EXISTS kiz_wb_synced BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE wb_fbs_orders
            ADD COLUMN IF NOT EXISTS is_b2b BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE wb_fbs_supplies
            ADD COLUMN IF NOT EXISTS is_b2b BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        # Local pick-check for orders WITHOUT КИЗ (EAN-13 vs product ШК).
        # Never sent to Wildberries — FeedPilot-only verification.
        conn.execute(
            """
            ALTER TABLE wb_fbs_orders
            ADD COLUMN IF NOT EXISTS pick_verified BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE wb_fbs_orders
            ADD COLUMN IF NOT EXISTS pick_barcode TEXT NOT NULL DEFAULT ''
            """
        )
        conn.execute(
            """
            ALTER TABLE wb_fbs_orders
            ADD COLUMN IF NOT EXISTS pick_verified_at TIMESTAMPTZ
            """
        )
        # Marketplace ``rid`` (= analytics/report ``srid``) for joining to KIZ circulation.
        conn.execute(
            """
            ALTER TABLE wb_fbs_orders
            ADD COLUMN IF NOT EXISTS rid TEXT NOT NULL DEFAULT ''
            """
        )
        conn.execute(
            repo._sql(
                "CREATE INDEX IF NOT EXISTS idx_wb_fbs_orders_user_src_rid "
                "ON wb_fbs_orders(user_id, source_id, rid) "
                "WHERE rid <> ''"
            )
        )


def _kiz_code_clean(value: object) -> str:
    """Normalize one sgtin: trim space/CR/LF only — never strip GS (\\u001D).

    Python's default ``str.strip()`` treats \\u001D as whitespace and would
    destroy Honest Sign separators at the ends of a code.
    """
    text = str(value or "")
    return text.strip(" \t\r\n")


def _normalize_kiz_saved_at(value: object) -> str:
    """Canonical UTC timestamp token for optimistic concurrency compares.

    PostgreSQL ``TIMESTAMPTZ`` round-trips often change the string form
    (e.g. ``+00:00`` written, ``+03:00`` / MSK read back). Comparing raw
    strings then falsely reports «another operator» for the same writer.
    """
    if value is None:
        return ""
    dt: datetime | None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.isoformat()


def update_order_kiz_codes(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_id: int,
    kiz_codes: list[str],
    wb_synced: bool = False,
    expected_saved_at: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Persist КИЗ codes locally; ``wb_synced`` marks successful WB API push.

    When ``expected_saved_at`` is set and ``force`` is false, refuses the write
    if another operator already saved different codes (optimistic concurrency).

    Returns ``{ok, conflict, missing, saved_at, codes}``.
    """
    ensure_wb_fbs_tables(repo)
    codes = [_kiz_code_clean(x) for x in (kiz_codes or []) if _kiz_code_clean(x)]
    payload = json.dumps(codes, ensure_ascii=False)
    saved_at = datetime.now(UTC)
    expected = _normalize_kiz_saved_at(expected_saved_at)
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql(
                """
                SELECT kiz_codes_json, kiz_saved_at FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ? AND order_id = ?
                """
            ),
            (int(user_id), int(source_id), int(order_id)),
        ).fetchone()
        if not row:
            return {
                "ok": False,
                "conflict": False,
                "missing": True,
                "saved_at": "",
                "codes": codes,
            }
        d = repo._row_to_dict(row)
        cur_saved = _normalize_kiz_saved_at(d.get("kiz_saved_at"))
        cur_codes: list[str] = []
        try:
            parsed = json.loads(d.get("kiz_codes_json") or "[]")
            if isinstance(parsed, list):
                cur_codes = [_kiz_code_clean(x) for x in parsed if _kiz_code_clean(x)]
        except Exception:
            cur_codes = []
        if (
            not force
            and expected
            and cur_saved
            and expected != cur_saved
            and cur_codes != codes
        ):
            _log.info(
                "kiz save conflict order_id=%s expected=%r current=%r",
                order_id,
                expected,
                cur_saved,
            )
            return {
                "ok": False,
                "conflict": True,
                "missing": False,
                "saved_at": cur_saved,
                "codes": cur_codes,
            }
        cur = conn.execute(
            repo._sql(
                """
                UPDATE wb_fbs_orders
                SET kiz_codes_json = ?, kiz_saved_at = ?, kiz_wb_synced = ?
                WHERE user_id = ? AND source_id = ? AND order_id = ?
                """
            ),
            (
                payload,
                saved_at,
                bool(wb_synced),
                int(user_id),
                int(source_id),
                int(order_id),
            ),
        )
        try:
            updated = int(cur.rowcount or 0)
        except Exception:
            updated = 0
    return {
        "ok": updated > 0,
        "conflict": False,
        "missing": updated <= 0,
        "saved_at": _normalize_kiz_saved_at(saved_at) if updated > 0 else cur_saved,
        "codes": codes if updated > 0 else cur_codes,
    }


def update_order_wb_statuses(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    statuses: dict[int, tuple[str, str]],
) -> int:
    """Update supplier/wb status only — keep tab and supply_id unchanged.

    Used when an order is canceled by the client but must stay visible inside
    an open supply / marking modal.
    """
    if not statuses:
        return 0
    ensure_wb_fbs_tables(repo)
    updated = 0
    with repo._connect() as conn:
        for oid, (ss, ws) in statuses.items():
            try:
                order_id = int(oid)
            except (TypeError, ValueError):
                continue
            cur = conn.execute(
                repo._sql(
                    """
                    UPDATE wb_fbs_orders
                    SET supplier_status = CASE
                            WHEN ? != '' THEN ?
                            ELSE supplier_status
                        END,
                        wb_status = CASE
                            WHEN ? != '' THEN ?
                            ELSE wb_status
                        END,
                        synced_at = ?
                    WHERE user_id = ? AND source_id = ? AND order_id = ?
                    """
                ),
                (
                    str(ss or ""),
                    str(ss or ""),
                    str(ws or ""),
                    str(ws or ""),
                    _utc_now(),
                    int(user_id),
                    int(source_id),
                    order_id,
                ),
            )
            try:
                updated += int(cur.rowcount or 0)
            except Exception:
                pass
    return updated


def _price_info_from_kop(kop: int, ccy: int) -> dict[str, Any] | None:
    if int(kop or 0) <= 0:
        return None
    code = int(ccy or 643)
    return {
        "price_rub": float(kop) / 100.0,
        "currency_name": "RUB" if code in (0, 643, 810) else "",
        "currency_code": code,
    }


def _price_info_from_order_payload(order: dict[str, Any]) -> dict[str, Any] | None:
    amount_kop, ccy = resolve_order_price(order)
    return _price_info_from_kop(int(amount_kop or 0), int(ccy or 643))


def load_order_price_map(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """Map order_id → price in rubles (from local ``wb_fbs_orders`` kopecks).

    ``price`` / ``final_price`` are stored in kopecks (see ``resolve_order_price``).
    Falls back to ``raw_json`` when columns are zero (legacy / incomplete upserts).
    """
    ids: list[int] = []
    for raw in order_ids or []:
        try:
            oid = int(raw)
        except (TypeError, ValueError):
            continue
        if oid != 0:
            ids.append(oid)
    if not ids:
        return {}
    ensure_wb_fbs_tables(repo)
    placeholders = ", ".join("?" for _ in ids)
    out: dict[int, dict[str, Any]] = {}
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT order_id, price, final_price, currency_code, raw_json
                FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ? AND order_id IN ({placeholders})
                """
            ),
            tuple([int(user_id), int(source_id), *ids]),
        ).fetchall()
    for row in rows:
        try:
            oid = int(row["order_id"])
        except (TypeError, ValueError, KeyError):
            continue
        try:
            final_i = int(row["final_price"] or 0)
        except (TypeError, ValueError):
            final_i = 0
        try:
            price_i = int(row["price"] or 0)
        except (TypeError, ValueError):
            price_i = 0
        try:
            ccy = int(row["currency_code"] or 643)
        except (TypeError, ValueError):
            ccy = 643
        kop = final_i if final_i > 0 else price_i
        info = _price_info_from_kop(kop, ccy)
        if info is None:
            raw = row["raw_json"] if "raw_json" in row.keys() else None
            if isinstance(raw, str) and raw.strip():
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    info = _price_info_from_order_payload(payload)
        if info is None:
            continue
        out[oid] = info
    return out


def fill_missing_order_prices(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_ids: list[int],
    api_key: str,
    lookback_days: int = 30,
    max_order_pages: int = 40,
    max_archive_pages: int = 8,
) -> dict[int, dict[str, Any]]:
    """Ensure local FBS prices for ``order_ids``; pull from Marketplace if missing.

    Used by CHZ prepare for OTHER (no fiscal) withdraws that need ``product_cost``.
    """
    ids = sorted({int(x) for x in (order_ids or []) if int(x or 0) != 0})
    price_map = load_order_price_map(
        repo, user_id=user_id, source_id=source_id, order_ids=ids
    )
    missing = [oid for oid in ids if oid not in price_map]
    key = str(api_key or "").strip()
    if not missing or not key:
        return price_map

    client = WbFbsClient(key)
    needed = set(missing)
    date_to = datetime.now(UTC)
    date_from = date_to - timedelta(days=max(1, min(int(lookback_days), 30)))

    def _absorb(orders: list[dict[str, Any]], *, is_archive: bool = False) -> None:
        for order in orders:
            if not isinstance(order, dict) or order.get("id") is None:
                continue
            try:
                oid = int(order["id"])
            except (TypeError, ValueError):
                continue
            if oid not in needed:
                continue
            upsert_order(
                repo,
                user_id=user_id,
                source_id=source_id,
                order=order,
                is_archive=is_archive,
                supplier_status=str(order.get("supplierStatus") or ""),
                wb_status=str(order.get("wbStatus") or ""),
            )
            if _price_info_from_order_payload(order) is not None:
                needed.discard(oid)

    try:
        next_token: int | None = 0
        pages = 0
        while pages < max(1, min(int(max_order_pages), 40)) and needed:
            orders, next_token = client.get_orders_page(
                limit=1000,
                next_token=next_token if next_token is not None else 0,
                date_from=date_from,
                date_to=date_to,
            )
            if not orders:
                break
            _absorb(orders, is_archive=False)
            pages += 1
            if next_token is None:
                break
            time.sleep(0.2)
    except Exception as exc:
        _log.warning("fill_missing_order_prices orders scan failed: %s", exc)

    if needed:
        try:
            arch_next: int | None = 0
            pages = 0
            max_pages = max(0, min(int(max_archive_pages), 30))
            while pages < max_pages and needed:
                arch_orders, arch_next = client.get_archive_orders(
                    limit=1000, next_token=arch_next
                )
                if not arch_orders:
                    break
                _absorb(arch_orders, is_archive=True)
                pages += 1
                if not arch_next:
                    break
                time.sleep(0.2)
        except Exception as exc:
            _log.warning("fill_missing_order_prices archive scan failed: %s", exc)

    return load_order_price_map(
        repo, user_id=user_id, source_id=source_id, order_ids=ids
    )


def load_order_status_map(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_ids: list[int],
) -> dict[int, dict[str, str]]:
    """Map order_id → supplier_status / wb_status / cancel_reason_label / order_status_label."""
    ids = []
    for raw in order_ids or []:
        try:
            oid = int(raw)
        except (TypeError, ValueError):
            continue
        if oid > 0:
            ids.append(oid)
    if not ids:
        return {}
    ensure_wb_fbs_tables(repo)
    placeholders = ", ".join("?" for _ in ids)
    out: dict[int, dict[str, str]] = {}
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT order_id, supplier_status, wb_status
                FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ? AND order_id IN ({placeholders})
                """
            ),
            tuple([int(user_id), int(source_id), *ids]),
        ).fetchall()
    for row in rows:
        try:
            oid = int(row["order_id"])
        except (TypeError, ValueError, KeyError):
            continue
        ss = str(row["supplier_status"] or "")
        ws = str(row["wb_status"] or "")
        out[oid] = {
            "supplier_status": ss,
            "wb_status": ws,
            "cancel_reason_label": cancel_reason_label(
                supplier_status=ss, wb_status=ws
            ),
            "order_status_label": order_portal_status_label(
                supplier_status=ss, wb_status=ws
            ),
        }
    return out


def load_order_kiz_map(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """Map order_id → {codes, wb_synced, saved_at} from local wb_fbs_orders."""
    ids = [int(x) for x in order_ids if x is not None]
    if not ids:
        return {}
    ensure_wb_fbs_tables(repo)
    placeholders = ", ".join("?" for _ in ids)
    out: dict[int, dict[str, Any]] = {}
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT order_id, kiz_codes_json, kiz_wb_synced, kiz_saved_at
                FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ? AND order_id IN ({placeholders})
                """
            ),
            tuple([int(user_id), int(source_id), *ids]),
        ).fetchall()
    for row in rows:
        try:
            oid = int(row["order_id"])
        except (TypeError, ValueError, KeyError):
            continue
        codes: list[str] = []
        try:
            parsed = json.loads(row["kiz_codes_json"] or "[]")
            if isinstance(parsed, list):
                codes = [_kiz_code_clean(x) for x in parsed if _kiz_code_clean(x)]
        except Exception:
            codes = []
        try:
            synced = bool(row["kiz_wb_synced"])
        except (KeyError, IndexError):
            synced = False
        try:
            saved_at = row["kiz_saved_at"]
        except (KeyError, IndexError):
            saved_at = None
        out[oid] = {
            "codes": codes,
            "wb_synced": synced,
            "saved_at": saved_at,
        }
    return out


def update_order_pick_verify(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_id: int,
    verified: bool,
    barcode: str = "",
    expected_verified_at: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Persist local ШК pick-check for a non-КИЗ order. No WB API calls.

    When ``expected_verified_at`` is set and ``force`` is false, refuses the write
    if another operator already saved a different pick result (optimistic concurrency).

    Returns ``{ok, conflict, missing, verified_at, verified, barcode}``.
    """
    ensure_wb_fbs_tables(repo)
    code = str(barcode or "").strip()
    is_ok = bool(verified) and bool(code)
    saved_at = datetime.now(UTC)
    expected = _normalize_kiz_saved_at(expected_verified_at)
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql(
                """
                SELECT pick_verified, pick_barcode, pick_verified_at FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ? AND order_id = ?
                """
            ),
            (int(user_id), int(source_id), int(order_id)),
        ).fetchone()
        if not row:
            return {
                "ok": False,
                "conflict": False,
                "missing": True,
                "verified_at": "",
                "verified": False,
                "barcode": "",
            }
        d = repo._row_to_dict(row)
        try:
            cur_verified = bool(d.get("pick_verified")) and bool(
                str(d.get("pick_barcode") or "").strip()
            )
        except Exception:
            cur_verified = False
        cur_barcode = str(d.get("pick_barcode") or "").strip() if cur_verified else ""
        cur_saved = _normalize_kiz_saved_at(d.get("pick_verified_at"))
        new_verified = is_ok
        new_barcode = code if is_ok else ""
        if (
            not force
            and expected
            and cur_saved
            and expected != cur_saved
            and (cur_verified != new_verified or cur_barcode != new_barcode)
        ):
            _log.info(
                "pick-verify conflict order_id=%s expected=%r current=%r",
                order_id,
                expected,
                cur_saved,
            )
            return {
                "ok": False,
                "conflict": True,
                "missing": False,
                "verified_at": cur_saved,
                "verified": cur_verified,
                "barcode": cur_barcode,
            }
        cur = conn.execute(
            repo._sql(
                """
                UPDATE wb_fbs_orders
                SET pick_verified = ?,
                    pick_barcode = ?,
                    pick_verified_at = ?
                WHERE user_id = ? AND source_id = ? AND order_id = ?
                """
            ),
            (
                new_verified,
                new_barcode,
                saved_at,
                int(user_id),
                int(source_id),
                int(order_id),
            ),
        )
        try:
            updated = int(cur.rowcount or 0)
        except Exception:
            updated = 0
    return {
        "ok": updated > 0,
        "conflict": False,
        "missing": updated <= 0,
        "verified_at": _normalize_kiz_saved_at(saved_at) if updated > 0 else "",
        "verified": new_verified if updated > 0 else False,
        "barcode": new_barcode if updated > 0 else "",
    }


def load_order_pick_map(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """Map order_id → {verified, barcode, verified_at} from local wb_fbs_orders."""
    ids = [int(x) for x in order_ids if x is not None]
    if not ids:
        return {}
    ensure_wb_fbs_tables(repo)
    placeholders = ", ".join("?" for _ in ids)
    out: dict[int, dict[str, Any]] = {}
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT order_id, pick_verified, pick_barcode, pick_verified_at
                FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ? AND order_id IN ({placeholders})
                """
            ),
            tuple([int(user_id), int(source_id), *ids]),
        ).fetchall()
    for row in rows:
        try:
            oid = int(row["order_id"])
        except (TypeError, ValueError, KeyError):
            continue
        try:
            verified = bool(row["pick_verified"])
        except (KeyError, IndexError):
            verified = False
        try:
            barcode = str(row["pick_barcode"] or "").strip()
        except (KeyError, IndexError):
            barcode = ""
        try:
            verified_at = row["pick_verified_at"]
        except (KeyError, IndexError):
            verified_at = None
        out[oid] = {
            "verified": verified and bool(barcode),
            "barcode": barcode,
            "verified_at": verified_at,
        }
    return out


def load_order_barcodes_map(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_ids: list[int],
) -> dict[int, list[str]]:
    """Map order_id → product ШК list from local ``skus_json`` (trusted source)."""
    ids = [int(x) for x in order_ids if x is not None]
    if not ids:
        return {}
    ensure_wb_fbs_tables(repo)
    placeholders = ", ".join("?" for _ in ids)
    out: dict[int, list[str]] = {}
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT order_id, skus_json
                FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ? AND order_id IN ({placeholders})
                """
            ),
            tuple([int(user_id), int(source_id), *ids]),
        ).fetchall()
    for row in rows:
        try:
            oid = int(row["order_id"])
        except (TypeError, ValueError, KeyError):
            continue
        barcodes: list[str] = []
        try:
            parsed = json.loads(row["skus_json"] or "[]")
        except Exception:
            parsed = []
        if isinstance(parsed, list):
            for sku in parsed:
                text = str(sku or "").strip()
                if text and text not in barcodes:
                    barcodes.append(text)
        out[oid] = barcodes
    return out


def _parse_dt(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def upsert_order(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order: dict[str, Any],
    supplier_status: str | None = None,
    wb_status: str | None = None,
    is_archive: bool = False,
    supply_id: str | None = None,
) -> None:
    try:
        order_id = int(order.get("id"))
    except (TypeError, ValueError):
        return
    ss = str(supplier_status if supplier_status is not None else order.get("supplierStatus") or "").strip()
    ws = str(wb_status if wb_status is not None else order.get("wbStatus") or "").strip()
    sid = str(supply_id if supply_id is not None else order.get("supplyId") or "").strip()
    tab = compute_tab(supplier_status=ss, wb_status=ws, is_archive=is_archive)
    offices = order.get("offices") if isinstance(order.get("offices"), list) else []
    skus = order.get("skus") if isinstance(order.get("skus"), list) else []
    price_i, currency_i = resolve_order_price(order)
    # Keep raw sale-currency price separately when available for debugging.
    sale_price_i = _as_int_or_none(order.get("finalPrice"))
    if sale_price_i is None:
        sale_price_i = _as_int_or_none(order.get("price")) or 0
    final_i = price_i
    b2b_flag = _order_b2b_flag(order)
    # Insert default False when WB omitted the field; on update keep previous value.
    is_b2b = bool(b2b_flag) if b2b_flag is not None else False
    has_b2b_signal = b2b_flag is not None
    now = _utc_now()
    with repo._connect() as conn:
        conn.execute(
            repo._sql(
                """
                INSERT INTO wb_fbs_orders (
                    user_id, source_id, order_id, order_uid, rid, article, nm_id, chrt_id, skus_json,
                    price, final_price, currency_code, warehouse_id, office_id, offices_json,
                    cargo_type, delivery_type, supplier_status, wb_status, tab, supply_id,
                    is_archive, is_b2b, comment_text, created_at_wb, raw_json, synced_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT (user_id, source_id, order_id) DO UPDATE SET
                    order_uid = EXCLUDED.order_uid,
                    rid = CASE
                        WHEN EXCLUDED.rid != '' THEN EXCLUDED.rid
                        ELSE wb_fbs_orders.rid
                    END,
                    article = EXCLUDED.article,
                    nm_id = EXCLUDED.nm_id,
                    chrt_id = EXCLUDED.chrt_id,
                    skus_json = EXCLUDED.skus_json,
                    price = EXCLUDED.price,
                    final_price = EXCLUDED.final_price,
                    currency_code = EXCLUDED.currency_code,
                    warehouse_id = EXCLUDED.warehouse_id,
                    office_id = EXCLUDED.office_id,
                    offices_json = EXCLUDED.offices_json,
                    cargo_type = EXCLUDED.cargo_type,
                    delivery_type = EXCLUDED.delivery_type,
                    supplier_status = CASE
                        WHEN EXCLUDED.supplier_status != '' THEN EXCLUDED.supplier_status
                        ELSE wb_fbs_orders.supplier_status
                    END,
                    wb_status = CASE
                        WHEN EXCLUDED.wb_status != '' THEN EXCLUDED.wb_status
                        ELSE wb_fbs_orders.wb_status
                    END,
                    -- GET /orders often omits statuses; do not reset tab to "new".
                    tab = CASE
                        WHEN EXCLUDED.is_archive THEN 'archive'
                        WHEN EXCLUDED.supplier_status != '' OR EXCLUDED.wb_status != ''
                            THEN EXCLUDED.tab
                        ELSE wb_fbs_orders.tab
                    END,
                    supply_id = CASE
                        WHEN EXCLUDED.supply_id != '' THEN EXCLUDED.supply_id
                        ELSE wb_fbs_orders.supply_id
                    END,
                    is_archive = EXCLUDED.is_archive OR wb_fbs_orders.is_archive,
                    -- Period sync may omit options.isB2B — do not wipe a known True.
                    is_b2b = CASE
                        WHEN ? THEN EXCLUDED.is_b2b
                        ELSE wb_fbs_orders.is_b2b
                    END,
                    comment_text = EXCLUDED.comment_text,
                    created_at_wb = COALESCE(EXCLUDED.created_at_wb, wb_fbs_orders.created_at_wb),
                    raw_json = EXCLUDED.raw_json,
                    synced_at = EXCLUDED.synced_at
                """
            ),
            (
                user_id,
                source_id,
                order_id,
                str(order.get("orderUid") or ""),
                str(order.get("rid") or "").strip(),
                str(order.get("article") or ""),
                order.get("nmId"),
                order.get("chrtId"),
                json.dumps(skus, ensure_ascii=False),
                int(sale_price_i or 0),
                final_i,
                int(currency_i or 643),
                order.get("warehouseId"),
                order.get("officeId"),
                json.dumps(offices, ensure_ascii=False),
                int(order.get("cargoType") or 0),
                str(order.get("deliveryType") or ""),
                ss,
                ws,
                tab,
                sid,
                bool(is_archive),
                bool(is_b2b),
                str(order.get("comment") or ""),
                _parse_dt(order.get("createdAt")),
                json.dumps(order, ensure_ascii=False),
                now,
                bool(has_b2b_signal),
            ),
        )


def order_ids_by_srids(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    srids: list[str],
) -> dict[str, int]:
    """Map analytics ``srid`` → Marketplace FBS ``order_id``.

    Matching order:
    1. full rid/srid case-insensitive;
    2. mid-token (``SPLIT_PART(...,2)`` / ``orderUid``) — Analytics often uses
       trailing unit suffix ``.1.0`` while Marketplace keeps ``.0.0`` on the same
       order (``eI.i0a….1.0`` vs ``eI.i0a….0.0``);
    3. ``raw_json.rid`` fallback + rid backfill.
    """
    ensure_wb_fbs_tables(repo)
    raw_keys = [str(s or "").strip() for s in (srids or []) if str(s or "").strip()]
    if not raw_keys:
        return {}

    def _mid(value: str) -> str:
        bits = str(value or "").split(".")
        return bits[1].strip().casefold() if len(bits) >= 2 and bits[1].strip() else ""

    # Preserve first original casing for each folded full key / mid key.
    fold_to_origs: dict[str, list[str]] = {}
    mid_to_origs: dict[str, list[str]] = {}
    for k in raw_keys:
        fold_to_origs.setdefault(k.casefold(), []).append(k)
        m = _mid(k)
        if m:
            mid_to_origs.setdefault(m, []).append(k)

    out: dict[str, int] = {}

    def _assign(orig: str, oid: int) -> None:
        if orig and oid > 0 and orig not in out:
            out[orig] = oid

    folds = sorted(fold_to_origs.keys())
    with repo._connect() as conn:
        if folds:
            ph = ", ".join("?" for _ in folds)
            rows = conn.execute(
                repo._sql(
                    f"""
                    SELECT order_id, rid FROM wb_fbs_orders
                    WHERE user_id = ? AND source_id = ?
                      AND rid IS NOT NULL AND rid <> ''
                      AND LOWER(rid) IN ({ph})
                    """
                ),
                (user_id, source_id, *folds),
            ).fetchall()
            for r in rows:
                d = repo._row_to_dict(r)
                rid = str(d.get("rid") or "").strip()
                try:
                    oid = int(d.get("order_id") or 0)
                except (TypeError, ValueError):
                    oid = 0
                for orig in fold_to_origs.get(rid.casefold(), []):
                    _assign(orig, oid)

        missing = [k for k in raw_keys if k not in out]
        if not missing:
            return out

        missing_mids = sorted({_mid(k) for k in missing if _mid(k)})
        if missing_mids:
            ph = ", ".join("?" for _ in missing_mids)
            rows2 = conn.execute(
                repo._sql(
                    f"""
                    SELECT order_id, order_uid, rid, raw_json
                    FROM wb_fbs_orders
                    WHERE user_id = ? AND source_id = ?
                      AND (
                        (order_uid IS NOT NULL AND order_uid <> ''
                         AND LOWER(order_uid) IN ({ph}))
                        OR (
                          rid IS NOT NULL AND rid <> ''
                          AND LOWER(SPLIT_PART(rid, '.', 2)) IN ({ph})
                        )
                        OR (
                          raw_json IS NOT NULL AND raw_json <> '' AND raw_json <> '{{}}'
                          AND LOWER(SPLIT_PART(COALESCE(raw_json::jsonb->>'rid', ''), '.', 2))
                              IN ({ph})
                        )
                      )
                    """
                ),
                (user_id, source_id, *missing_mids, *missing_mids, *missing_mids),
            ).fetchall()
            for r in rows2:
                d = repo._row_to_dict(r)
                try:
                    oid = int(d.get("order_id") or 0)
                except (TypeError, ValueError):
                    oid = 0
                if oid <= 0:
                    continue
                rid_col = str(d.get("rid") or "").strip()
                uid = str(d.get("order_uid") or "").strip()
                rid_json = ""
                try:
                    raw = d.get("raw_json") or "{}"
                    payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    if isinstance(payload, dict):
                        rid_json = str(payload.get("rid") or "").strip()
                except (TypeError, ValueError, json.JSONDecodeError):
                    rid_json = ""
                token_candidates = {
                    uid.casefold(),
                    _mid(rid_col),
                    _mid(rid_json),
                }
                token_candidates.discard("")
                for tok in token_candidates:
                    for orig in mid_to_origs.get(tok, []):
                        _assign(orig, oid)
                if not rid_col and rid_json:
                    conn.execute(
                        repo._sql(
                            """
                            UPDATE wb_fbs_orders
                            SET rid = ?
                            WHERE user_id = ? AND source_id = ? AND order_id = ?
                              AND (rid IS NULL OR rid = '')
                            """
                        ),
                        (rid_json, user_id, source_id, oid),
                    )

        # Final fallback: full-key match via order_uid / raw rid for leftovers.
        missing = [k for k in raw_keys if k not in out]
        if missing:
            missing_folds = sorted({k.casefold() for k in missing})
            ph = ", ".join("?" for _ in missing_folds)
            rows3 = conn.execute(
                repo._sql(
                    f"""
                    SELECT order_id, order_uid, rid, raw_json
                    FROM wb_fbs_orders
                    WHERE user_id = ? AND source_id = ?
                      AND (
                        (order_uid IS NOT NULL AND order_uid <> ''
                         AND LOWER(order_uid) IN ({ph}))
                        OR (
                          raw_json IS NOT NULL AND raw_json <> '' AND raw_json <> '{{}}'
                          AND LOWER(raw_json::jsonb->>'rid') IN ({ph})
                        )
                      )
                    """
                ),
                (user_id, source_id, *missing_folds, *missing_folds),
            ).fetchall()
            for r in rows3:
                d = repo._row_to_dict(r)
                try:
                    oid = int(d.get("order_id") or 0)
                except (TypeError, ValueError):
                    oid = 0
                if oid <= 0:
                    continue
                rid_col = str(d.get("rid") or "").strip()
                uid = str(d.get("order_uid") or "").strip()
                rid_json = ""
                try:
                    raw = d.get("raw_json") or "{}"
                    payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    if isinstance(payload, dict):
                        rid_json = str(payload.get("rid") or "").strip()
                except (TypeError, ValueError, json.JSONDecodeError):
                    rid_json = ""
                for candidate in (rid_col, rid_json, uid):
                    cf = candidate.casefold()
                    for orig in fold_to_origs.get(cf, []):
                        _assign(orig, oid)
    return out

def refresh_order_statuses_light(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_ids: list[int],
    api_key: str,
) -> int:
    """Refresh Marketplace wbStatus/supplierStatus for known order ids only.

    Used by Вывод КИЗ list — no archive download, so it stays fast.
    """
    ids = sorted({int(x) for x in (order_ids or []) if int(x or 0) > 0})
    if not ids or not str(api_key or "").strip():
        return 0
    client = WbFbsClient(str(api_key).strip())
    updated = 0
    for i in range(0, len(ids), 1000):
        chunk = ids[i : i + 1000]
        statuses = client.get_statuses(chunk)
        by_id: dict[int, dict[str, Any]] = {}
        for st in statuses or []:
            try:
                oid = int(st.get("id") or st.get("orderId") or 0)
            except (TypeError, ValueError):
                oid = 0
            if oid > 0:
                by_id[oid] = st
        with repo._connect() as conn:
            for oid in chunk:
                st = by_id.get(oid) or {}
                ss = str(st.get("supplierStatus") or "").strip()
                ws = str(st.get("wbStatus") or "").strip()
                if not ss and not ws:
                    continue
                tab = compute_tab(
                    supplier_status=ss,
                    wb_status=ws,
                    is_archive=False,
                )
                conn.execute(
                    repo._sql(
                        """
                        UPDATE wb_fbs_orders
                        SET supplier_status = CASE WHEN ? <> '' THEN ? ELSE supplier_status END,
                            wb_status = CASE WHEN ? <> '' THEN ? ELSE wb_status END,
                            tab = ?,
                            is_archive = CASE WHEN lower(?) = 'sold' THEN TRUE ELSE is_archive END,
                            synced_at = ?
                        WHERE user_id = ? AND source_id = ? AND order_id = ?
                        """
                    ),
                    (
                        ss,
                        ss,
                        ws,
                        ws,
                        tab,
                        ws,
                        _utc_now(),
                        user_id,
                        source_id,
                        oid,
                    ),
                )
                updated += 1
    return updated


def hydrate_orders_for_kiz_srids(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    srids: list[str],
    api_key: str,
    archive_pages: int = 8,
    lookback_days: int = 30,
) -> dict[str, Any]:
    """Pull Marketplace orders (incl. sold/archive) so KIZ can join srid→order_id.

    Regular FBS sync skips finished/archive tabs — without this, Вывод КИЗ shows
    «—» for order/status and cannot verify «выкуплен» / «отказ».

    Always refreshes statuses for linked orders (even when srid already known),
    so stale «на сборке» / wrong archive defaults cannot pass CHZ gates.
    """
    ensure_wb_fbs_tables(repo)
    wanted = sorted({str(s or "").strip() for s in (srids or []) if str(s or "").strip()})
    if not wanted or not str(api_key or "").strip():
        return {"wanted": len(wanted), "found_before": 0, "found_after": 0, "fetched": 0}

    before = order_ids_by_srids(
        repo, user_id=user_id, source_id=source_id, srids=wanted
    )
    missing = [k for k in wanted if k not in before]

    client = WbFbsClient(str(api_key).strip())
    fetched = 0
    still_missing = set(missing)

    def _absorb(orders: list[dict[str, Any]], *, is_archive: bool = False) -> None:
        nonlocal fetched
        for order in orders:
            if is_archive:
                upsert_order(
                    repo,
                    user_id=user_id,
                    source_id=source_id,
                    order=order,
                    is_archive=True,
                    supplier_status=str(order.get("supplierStatus") or ""),
                    # Do NOT default to "sold" — archive also has cancellations.
                    wb_status=str(order.get("wbStatus") or ""),
                )
            else:
                upsert_order(repo, user_id=user_id, source_id=source_id, order=order)
            fetched += 1
        if still_missing:
            now = order_ids_by_srids(
                repo, user_id=user_id, source_id=source_id, srids=list(still_missing)
            )
            for k in list(still_missing):
                if k in now:
                    still_missing.discard(k)

    if missing:
        # 1) Recent open orders window (may include items that just became sold).
        try:
            date_to = datetime.now(UTC)
            date_from = date_to - timedelta(
                days=max(1, min(int(lookback_days), 90))
            )
            next_token: int | None = 0
            pages = 0
            while pages < 15 and still_missing:
                orders, next_token = client.get_orders_page(
                    limit=1000,
                    next_token=next_token if next_token is not None else 0,
                    date_from=date_from,
                    date_to=date_to,
                )
                if not orders:
                    break
                _absorb(orders, is_archive=False)
                pages += 1
                if next_token is None:
                    break
                time.sleep(0.2)
        except Exception as exc:
            _log.warning("kiz hydrate orders page failed: %s", exc)

        # 2) Archive / finished — sold AND cancelled (отказы). Never invent wbStatus.
        try:
            arch_next: int | None = 0
            pages = 0
            max_pages = max(0, min(int(archive_pages), 30))
            while pages < max_pages and still_missing:
                arch_orders, arch_next = client.get_archive_orders(
                    limit=1000, next_token=arch_next
                )
                if not arch_orders:
                    break
                _absorb(arch_orders, is_archive=True)
                pages += 1
                if not arch_next:
                    break
                time.sleep(0.2)
        except Exception as exc:
            _log.warning("kiz hydrate archive failed: %s", exc)

    after = order_ids_by_srids(
        repo, user_id=user_id, source_id=source_id, srids=wanted
    )
    # Always refresh statuses for linked orders (sold vs in-delivery vs cancel).
    linked_ids = sorted({int(v) for v in after.values() if int(v or 0) > 0})
    if linked_ids:
        try:
            refresh_order_statuses_light(
                repo,
                user_id=user_id,
                source_id=source_id,
                order_ids=linked_ids,
                api_key=str(api_key).strip(),
            )
        except Exception as exc:
            _log.warning("kiz hydrate status refresh failed: %s", exc)

    return {
        "wanted": len(wanted),
        "found_before": len(before),
        "found_after": len(after),
        "fetched": fetched,
        "still_missing": len([k for k in wanted if k not in after]),
    }


def upsert_supply(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    supply: dict[str, Any],
    order_ids: list[int] | None = None,
    boxes: list[dict[str, Any]] | None = None,
) -> None:
    supply_id = str(supply.get("id") or "").strip()
    if not supply_id:
        return
    now = _utc_now()
    supply_b2b = _coalesce_b2b_flag(supply)
    has_supply_b2b = supply_b2b is not None
    with repo._connect() as conn:
        conn.execute(
            repo._sql(
                """
                INSERT INTO wb_fbs_supplies (
                    user_id, source_id, supply_id, name, done, cargo_type, is_b2b, destination_office_id,
                    created_at_wb, closed_at_wb, scan_dt, order_ids_json, boxes_json, raw_json, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, source_id, supply_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    done = EXCLUDED.done,
                    cargo_type = EXCLUDED.cargo_type,
                    -- Empty WB supplies send isB2b=null; keep previous local flag.
                    is_b2b = CASE
                        WHEN ? THEN EXCLUDED.is_b2b
                        ELSE wb_fbs_supplies.is_b2b
                    END,
                    destination_office_id = EXCLUDED.destination_office_id,
                    created_at_wb = COALESCE(EXCLUDED.created_at_wb, wb_fbs_supplies.created_at_wb),
                    closed_at_wb = COALESCE(EXCLUDED.closed_at_wb, wb_fbs_supplies.closed_at_wb),
                    scan_dt = COALESCE(EXCLUDED.scan_dt, wb_fbs_supplies.scan_dt),
                    order_ids_json = CASE
                        WHEN EXCLUDED.order_ids_json != '[]' THEN EXCLUDED.order_ids_json
                        ELSE wb_fbs_supplies.order_ids_json
                    END,
                    boxes_json = CASE
                        WHEN EXCLUDED.boxes_json != '[]' THEN EXCLUDED.boxes_json
                        ELSE wb_fbs_supplies.boxes_json
                    END,
                    raw_json = EXCLUDED.raw_json,
                    synced_at = EXCLUDED.synced_at
                """
            ),
            (
                user_id,
                source_id,
                supply_id,
                str(supply.get("name") or ""),
                bool(supply.get("done")),
                int(supply.get("cargoType") or 0),
                bool(supply_b2b) if supply_b2b is not None else False,
                supply.get("destinationOfficeId"),
                _parse_dt(supply.get("createdAt")),
                _parse_dt(supply.get("closedAt")),
                _parse_dt(supply.get("scanDt")),
                json.dumps(order_ids or [], ensure_ascii=False),
                json.dumps(boxes or [], ensure_ascii=False),
                json.dumps(supply, ensure_ascii=False),
                now,
                bool(has_supply_b2b),
            ),
        )


def _orders_filter_sql(
    *,
    user_id: int,
    source_id: int | None,
    tab: str | None = None,
    search: str | None = None,
) -> tuple[str, list[Any]]:
    conditions = ["user_id = ?"]
    params: list[Any] = [user_id]
    if source_id:
        conditions.append("source_id = ?")
        params.append(int(source_id))
    if tab:
        conditions.append("tab = ?")
        params.append(tab)
    if search:
        q = f"%{str(search).strip()}%"
        conditions.append(
            "(CAST(order_id AS TEXT) ILIKE ? OR article ILIKE ? OR supply_id ILIKE ? OR skus_json ILIKE ?)"
        )
        params.extend([q, q, q, q])
    return " AND ".join(conditions), params


def list_order_ids(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int | None,
    tab: str | None = None,
    search: str | None = None,
    limit: int = 5000,
) -> dict[str, Any]:
    """Lightweight id list for «select all matching» (no product enrichment)."""
    ensure_wb_fbs_tables(repo)
    where, params = _orders_filter_sql(
        user_id=user_id, source_id=source_id, tab=tab, search=search
    )
    safe_limit = min(max(int(limit or 5000), 1), 10000)
    with repo._connect() as conn:
        total_row = conn.execute(
            repo._sql(f"SELECT COUNT(*) AS n FROM wb_fbs_orders WHERE {where}"),
            tuple(params),
        ).fetchone()
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT order_id, supply_id
                FROM wb_fbs_orders
                WHERE {where}
                ORDER BY created_at_wb DESC NULLS LAST, order_id DESC
                LIMIT ?
                """
            ),
            tuple(params + [safe_limit]),
        ).fetchall()
    total = int(total_row["n"]) if total_row else 0
    order_ids: list[int] = []
    meta: dict[str, dict[str, str]] = {}
    for row in rows:
        d = repo._row_to_dict(row)
        try:
            oid = int(d.get("order_id"))
        except (TypeError, ValueError):
            continue
        order_ids.append(oid)
        meta[str(oid)] = {"supply_id": str(d.get("supply_id") or "").strip()}
    return {
        "order_ids": order_ids,
        "total": total,
        "truncated": total > len(order_ids),
        "meta": meta,
    }


def list_orders(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int | None,
    tab: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    ensure_wb_fbs_tables(repo)
    where, params = _orders_filter_sql(
        user_id=user_id, source_id=source_id, tab=tab, search=search
    )
    safe_page = max(int(page), 1)
    safe_size = min(max(int(page_size), 1), 200)
    offset = (safe_page - 1) * safe_size
    with repo._connect() as conn:
        total = conn.execute(
            repo._sql(f"SELECT COUNT(*) AS n FROM wb_fbs_orders WHERE {where}"),
            tuple(params),
        ).fetchone()
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT * FROM wb_fbs_orders
                WHERE {where}
                ORDER BY created_at_wb DESC NULLS LAST, order_id DESC
                LIMIT ? OFFSET ?
                """
            ),
            tuple(params + [safe_size, offset]),
        ).fetchall()
    counts = _tab_counts(repo, user_id=user_id, source_id=source_id)
    name_map = repo.get_product_name_by_article(user_id=user_id)
    photo_map = repo.get_product_photo_map(user_id=user_id)
    # also map by wb_nmid
    items = []
    for row in rows:
        d = repo._row_to_dict(row)
        article = str(d.get("article") or "").strip()
        nm_id = str(d.get("nm_id") or "").strip()
        d["product_name"] = name_map.get(article) or name_map.get(nm_id) or article or "—"
        d["product_photo"] = photo_map.get(article) or photo_map.get(nm_id) or ""
        # Prefer live resolve from raw API payload — fixes rows synced before
        # convertedPrice was used for seller-facing RUB amounts.
        raw_order: dict[str, Any] = {}
        try:
            parsed_raw = json.loads(d.get("raw_json") or "{}")
            if isinstance(parsed_raw, dict):
                raw_order = parsed_raw
        except Exception:
            raw_order = {}
        if raw_order:
            amount, ccy = resolve_order_price(raw_order)
            d["final_price"] = amount
            d["currency_code"] = ccy
            d["price_display"] = format_price_rub(amount, ccy)
        else:
            d["price_display"] = format_price_rub(
                d.get("final_price") or d.get("price"), d.get("currency_code")
            )
        d["cargo_label"] = cargo_type_label(d.get("cargo_type"))
        d["cancel_reason_label"] = cancel_reason_label(
            supplier_status=d.get("supplier_status"),
            wb_status=d.get("wb_status"),
        )
        d["finished_status_label"] = finished_status_label(
            supplier_status=d.get("supplier_status"),
            wb_status=d.get("wb_status"),
        )
        try:
            offices = json.loads(d.get("offices_json") or "[]")
        except Exception:
            offices = []
        d["offices"] = offices if isinstance(offices, list) else []
        office_names = [str(x).strip() for x in d["offices"] if str(x or "").strip()]
        d["warehouse_label"] = ", ".join(office_names) or (
            f"Склад {d.get('warehouse_id')}" if d.get("warehouse_id") else "—"
        )
        # Delivery / office address from WB payload (often empty for pure FBS).
        warehouse_address = ""
        if isinstance(raw_order, dict):
            addr = raw_order.get("address")
            if isinstance(addr, dict):
                warehouse_address = str(addr.get("fullAddress") or "").strip()
            elif isinstance(addr, str):
                warehouse_address = addr.strip()
        d["warehouse_address"] = warehouse_address
        # WB order.skus = product barcodes (ШК)
        try:
            skus_raw = json.loads(d.get("skus_json") or "[]")
        except Exception:
            skus_raw = []
        barcodes: list[str] = []
        if isinstance(skus_raw, list):
            for sku in skus_raw:
                text = str(sku or "").strip()
                if text and text not in barcodes:
                    barcodes.append(text)
        d["barcodes"] = barcodes
        d["skus"] = barcodes
        items.append(d)
    return {
        "items": items,
        "total": int(total["n"]) if total else 0,
        "page": safe_page,
        "page_size": safe_size,
        "counts": counts,
    }


def parse_order_id_query(search: object) -> int | None:
    """Return order id when search is an exact numeric assembly-order number."""
    q = str(search or "").strip()
    # WB assembly order ids are long integers; require ≥6 digits to avoid
    # accidental remote lookups while typing short article/SKU fragments.
    if not re.fullmatch(r"\d{6,}", q):
        return None
    try:
        return int(q)
    except (TypeError, ValueError):
        return None


def _enrich_order_row(
    repo: ReviewRepository,
    *,
    user_id: int,
    row: dict[str, Any],
    name_map: dict[str, str] | None = None,
    photo_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Attach product/price/status labels used by the WB FBS orders table."""
    d = dict(row)
    names = name_map if name_map is not None else repo.get_product_name_by_article(user_id=user_id)
    photos = photo_map if photo_map is not None else repo.get_product_photo_map(user_id=user_id)
    article = str(d.get("article") or "").strip()
    nm_id = str(d.get("nm_id") or "").strip()
    d["product_name"] = names.get(article) or names.get(nm_id) or article or "—"
    d["product_photo"] = photos.get(article) or photos.get(nm_id) or ""
    raw_order: dict[str, Any] = {}
    try:
        parsed_raw = json.loads(d.get("raw_json") or "{}")
        if isinstance(parsed_raw, dict):
            raw_order = parsed_raw
    except Exception:
        raw_order = {}
    if raw_order:
        amount, ccy = resolve_order_price(raw_order)
        d["final_price"] = amount
        d["currency_code"] = ccy
        d["price_display"] = format_price_rub(amount, ccy)
    else:
        d["price_display"] = format_price_rub(
            d.get("final_price") or d.get("price"), d.get("currency_code")
        )
    d["cargo_label"] = cargo_type_label(d.get("cargo_type"))
    d["cancel_reason_label"] = cancel_reason_label(
        supplier_status=d.get("supplier_status"),
        wb_status=d.get("wb_status"),
    )
    d["finished_status_label"] = finished_status_label(
        supplier_status=d.get("supplier_status"),
        wb_status=d.get("wb_status"),
    )
    try:
        offices = json.loads(d.get("offices_json") or "[]")
    except Exception:
        offices = []
    d["offices"] = offices if isinstance(offices, list) else []
    office_names = [str(x).strip() for x in d["offices"] if str(x or "").strip()]
    d["warehouse_label"] = ", ".join(office_names) or (
        f"Склад {d.get('warehouse_id')}" if d.get("warehouse_id") else "—"
    )
    warehouse_address = ""
    if isinstance(raw_order, dict):
        addr = raw_order.get("address")
        if isinstance(addr, dict):
            warehouse_address = str(addr.get("fullAddress") or "").strip()
        elif isinstance(addr, str):
            warehouse_address = addr.strip()
    d["warehouse_address"] = warehouse_address
    try:
        skus_raw = json.loads(d.get("skus_json") or "[]")
    except Exception:
        skus_raw = []
    barcodes: list[str] = []
    if isinstance(skus_raw, list):
        for sku in skus_raw:
            text = str(sku or "").strip()
            if text and text not in barcodes:
                barcodes.append(text)
    d["barcodes"] = barcodes
    d["skus"] = barcodes
    return d


def get_order_by_id(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_id: int,
) -> dict[str, Any] | None:
    """Exact local lookup across all tabs (incl. finished/cancelled/archive)."""
    ensure_wb_fbs_tables(repo)
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql(
                """
                SELECT * FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ? AND order_id = ?
                LIMIT 1
                """
            ),
            (user_id, int(source_id), int(order_id)),
        ).fetchone()
    if not row:
        return None
    return repo._row_to_dict(row)


def _fetch_order_payload_from_wb(
    client: WbFbsClient,
    order_id: int,
    *,
    max_order_pages: int = 20,
    max_archive_pages: int = 5,
) -> tuple[dict[str, Any] | None, bool, dict[str, str] | None]:
    """Find full WB order payload + status.

    Returns ``(order_payload_or_none, is_archive, status_or_none)``.
    Status comes from ``POST /api/v3/orders/status`` (works for finished/cancelled).
    Full payload is scanned from ``GET /api/v3/orders`` (≤30 days) then archive.
    """
    oid = int(order_id)
    status_row: dict[str, str] | None = None
    try:
        statuses = client.get_statuses([oid])
        for st in statuses:
            if not isinstance(st, dict) or st.get("id") is None:
                continue
            try:
                if int(st["id"]) != oid:
                    continue
            except (TypeError, ValueError):
                continue
            status_row = {
                "supplierStatus": str(st.get("supplierStatus") or ""),
                "wbStatus": str(st.get("wbStatus") or ""),
            }
            break
        time.sleep(0.21)
    except Exception as exc:
        _log.warning("wb_fbs lookup status failed order=%s: %s", oid, exc)
        raise

    # No status → order is unknown for this marketplace token.
    if status_row is None:
        return None, False, None

    date_to = datetime.now(UTC)
    date_from = date_to - timedelta(days=30)
    next_token: int | None = 0
    pages = 0
    try:
        while pages < max(1, min(int(max_order_pages), 30)):
            orders, next_token = client.get_orders_page(
                limit=1000,
                next_token=next_token if next_token is not None else 0,
                date_from=date_from,
                date_to=date_to,
            )
            for order in orders:
                if not isinstance(order, dict) or order.get("id") is None:
                    continue
                try:
                    if int(order["id"]) == oid:
                        return order, False, status_row
                except (TypeError, ValueError):
                    continue
            pages += 1
            if next_token is None:
                break
            time.sleep(0.25)
    except Exception as exc:
        _log.warning("wb_fbs lookup orders scan failed order=%s: %s", oid, exc)

    arch_next = 0
    try:
        for _ in range(max(0, int(max_archive_pages))):
            arch_orders, arch_next = client.get_archive_orders(
                limit=1000, next_token=arch_next
            )
            for order in arch_orders:
                if not isinstance(order, dict) or order.get("id") is None:
                    continue
                try:
                    if int(order["id"]) == oid:
                        return order, True, status_row
                except (TypeError, ValueError):
                    continue
            if not arch_next:
                break
            time.sleep(0.25)
    except Exception as exc:
        _log.warning("wb_fbs lookup archive scan failed order=%s: %s", oid, exc)

    # Status known but payload outside scanned windows — still show the order.
    return {"id": oid}, False, status_row


def lookup_order_by_id(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_id: int,
    api_key: str | None = None,
    allow_remote: bool = True,
) -> dict[str, Any]:
    """Find one assembly order locally (any tab), else optionally via WB API.

    Sync intentionally skips finished/cancelled/archive; this path is the
    explicit escape hatch for search-by-order-number.
    """
    ensure_wb_fbs_tables(repo)
    oid = int(order_id)
    sid = int(source_id)
    counts = _tab_counts(repo, user_id=user_id, source_id=sid)

    local = get_order_by_id(repo, user_id=user_id, source_id=sid, order_id=oid)
    if local:
        item = _enrich_order_row(repo, user_id=user_id, row=local)
        return {
            "found": True,
            "source": "local",
            "order_id": oid,
            "tab": str(item.get("tab") or ""),
            "item": item,
            "counts": counts,
        }

    if not allow_remote:
        return {
            "found": False,
            "source": "none",
            "order_id": oid,
            "tab": "",
            "item": None,
            "counts": counts,
            "message": "Заказ не найден в локальной базе",
        }

    key = str(api_key or "").strip()
    if not key:
        return {
            "found": False,
            "source": "none",
            "order_id": oid,
            "tab": "",
            "item": None,
            "counts": counts,
            "message": "Нет API-ключа источника для поиска в WB",
        }

    client = WbFbsClient(key)
    try:
        order_payload, is_archive, status_row = _fetch_order_payload_from_wb(client, oid)
    except Exception as exc:
        if is_marketplace_scope_error(exc):
            return {
                "found": False,
                "source": "none",
                "order_id": oid,
                "tab": "",
                "item": None,
                "counts": counts,
                "scope_error": True,
                "message": SCOPE_ERROR_MESSAGE,
            }
        return {
            "found": False,
            "source": "none",
            "order_id": oid,
            "tab": "",
            "item": None,
            "counts": counts,
            "message": friendly_sync_error("поиск заказа", exc),
        }

    if order_payload is None or status_row is None:
        return {
            "found": False,
            "source": "none",
            "order_id": oid,
            "tab": "",
            "item": None,
            "counts": counts,
            "message": "Заказ не найден в WB API",
        }

    upsert_order(
        repo,
        user_id=user_id,
        source_id=sid,
        order=order_payload,
        supplier_status=status_row.get("supplierStatus") or "",
        wb_status=status_row.get("wbStatus") or "",
        is_archive=bool(is_archive),
    )
    stored = get_order_by_id(repo, user_id=user_id, source_id=sid, order_id=oid)
    if not stored:
        return {
            "found": False,
            "source": "none",
            "order_id": oid,
            "tab": "",
            "item": None,
            "counts": counts,
            "message": "Заказ найден в WB, но не удалось сохранить локально",
        }
    item = _enrich_order_row(repo, user_id=user_id, row=stored)
    counts = _tab_counts(repo, user_id=user_id, source_id=sid)
    return {
        "found": True,
        "source": "remote",
        "order_id": oid,
        "tab": str(item.get("tab") or ""),
        "item": item,
        "counts": counts,
        "is_archive": bool(is_archive),
    }


def list_supplies(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int | None,
    only_open: bool = False,
) -> list[dict[str, Any]]:
    ensure_wb_fbs_tables(repo)
    conditions = ["user_id = ?"]
    params: list[Any] = [user_id]
    if source_id:
        conditions.append("source_id = ?")
        params.append(int(source_id))
    if only_open:
        conditions.append("done = FALSE")
    where = " AND ".join(conditions)
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT * FROM wb_fbs_supplies
                WHERE {where}
                ORDER BY created_at_wb DESC NULLS LAST
                LIMIT 500
                """
            ),
            tuple(params),
        ).fetchall()
    result = []
    for row in rows:
        d = repo._row_to_dict(row)
        d["order_ids"] = _parse_json_list(d.get("order_ids_json"))
        d["boxes"] = _parse_json_list(d.get("boxes_json"))
        result.append(d)
    return result


def _tab_counts(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int | None,
) -> dict[str, int]:
    count_conditions = ["user_id = ?"]
    count_params: list[Any] = [user_id]
    if source_id:
        count_conditions.append("source_id = ?")
        count_params.append(int(source_id))
    count_where = " AND ".join(count_conditions)
    with repo._connect() as conn:
        count_rows = conn.execute(
            repo._sql(
                f"SELECT tab, COUNT(*) AS n FROM wb_fbs_orders WHERE {count_where} GROUP BY tab"
            ),
            tuple(count_params),
        ).fetchall()
    counts = {str(r["tab"]): int(r["n"]) for r in count_rows}
    mgt_new = 0
    with repo._connect() as conn:
        mgt_row = conn.execute(
            repo._sql(
                f"""
                SELECT COUNT(*) AS n FROM wb_fbs_orders
                WHERE {count_where}
                  AND tab = ?
                  AND cargo_type = 1
                  AND COALESCE(is_archive, FALSE) = FALSE
                """
            ),
            tuple(count_params + [TAB_NEW]),
        ).fetchone()
        mgt_new = int(mgt_row["n"]) if mgt_row else 0
    open_supplies = 0
    with repo._connect() as conn:
        open_row = conn.execute(
            repo._sql(
                f"""
                SELECT COUNT(*) AS n FROM wb_fbs_supplies
                WHERE {count_where} AND COALESCE(done, FALSE) = FALSE
                """
            ),
            tuple(count_params),
        ).fetchone()
        open_supplies = int(open_row["n"]) if open_row else 0
    return {
        TAB_NEW: counts.get(TAB_NEW, 0),
        TAB_ASSEMBLY: counts.get(TAB_ASSEMBLY, 0),
        TAB_DELIVERY: counts.get(TAB_DELIVERY, 0),
        TAB_FINISHED: counts.get(TAB_FINISHED, 0),
        TAB_CANCELLED: counts.get(TAB_CANCELLED, 0),
        TAB_ARCHIVE: counts.get(TAB_ARCHIVE, 0),
        "mgt_new": mgt_new,
        "open_supplies": open_supplies,
    }


def list_delivery_supplies(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int | None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Supplies (поставки) for the «В доставке» tab — one row per supply, not orders."""
    return _list_supplies_for_orders_tab(
        repo,
        user_id=user_id,
        source_id=source_id,
        tab=TAB_DELIVERY,
        search=search,
        page=page,
        page_size=page_size,
    )


def list_assembly_supplies(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int | None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Supplies (поставки) for the «На сборке» tab — one row per supply, not orders."""
    return _list_supplies_for_orders_tab(
        repo,
        user_id=user_id,
        source_id=source_id,
        tab=TAB_ASSEMBLY,
        search=search,
        page=page,
        page_size=page_size,
    )


def _list_supplies_for_orders_tab(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int | None,
    tab: str,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Aggregate orders of a tab into supply rows (portal-like)."""
    ensure_wb_fbs_tables(repo)
    tab_key = str(tab or "").strip().lower() or TAB_DELIVERY
    conditions = ["o.user_id = ?", "o.tab = ?", "o.supply_id != ''"]
    params: list[Any] = [user_id, tab_key]
    if source_id:
        conditions.append("o.source_id = ?")
        params.append(int(source_id))
    q = str(search or "").strip()
    if q:
        like = f"%{q}%"
        conditions.append(
            "(o.supply_id ILIKE ? OR CAST(o.order_id AS TEXT) ILIKE ? OR o.article ILIKE ?"
            " OR COALESCE(s.name, '') ILIKE ? OR COALESCE(o.offices_json, '') ILIKE ?)"
        )
        params.extend([like, like, like, like, like])
    where = " AND ".join(conditions)
    safe_page = max(int(page), 1)
    safe_size = min(max(int(page_size), 1), 200)
    offset = (safe_page - 1) * safe_size

    with repo._connect() as conn:
        # One row per supply_id among tab orders; left-join supply metadata.
        total_row = conn.execute(
            repo._sql(
                f"""
                SELECT COUNT(*) AS n FROM (
                    SELECT o.supply_id
                    FROM wb_fbs_orders o
                    LEFT JOIN wb_fbs_supplies s
                      ON s.user_id = o.user_id
                     AND s.source_id = o.source_id
                     AND s.supply_id = o.supply_id
                    WHERE {where}
                    GROUP BY o.user_id, o.source_id, o.supply_id
                ) t
                """
            ),
            tuple(params),
        ).fetchone()
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT
                    o.supply_id AS supply_id,
                    o.source_id AS source_id,
                    COUNT(*) AS order_count,
                    ARRAY_AGG(o.order_id ORDER BY o.order_id) AS order_ids_agg,
                    MAX(o.warehouse_id) AS warehouse_id,
                    MAX(o.offices_json) AS offices_json,
                    MAX(o.cargo_type) AS order_cargo_type,
                    MAX(s.name) AS name,
                    MAX(CASE WHEN s.done THEN 1 ELSE 0 END) AS done_int,
                    MAX(s.cargo_type) AS cargo_type,
                    MAX(s.destination_office_id) AS destination_office_id,
                    MAX(COALESCE(s.created_at_wb, o.created_at_wb)) AS created_at_wb,
                    MAX(s.closed_at_wb) AS closed_at_wb,
                    MAX(s.scan_dt) AS scan_dt,
                    MAX(s.boxes_json) AS boxes_json,
                    MAX(s.raw_json) AS raw_json
                FROM wb_fbs_orders o
                LEFT JOIN wb_fbs_supplies s
                  ON s.user_id = o.user_id
                 AND s.source_id = o.source_id
                 AND s.supply_id = o.supply_id
                WHERE {where}
                GROUP BY o.user_id, o.source_id, o.supply_id
                ORDER BY MAX(COALESCE(s.created_at_wb, o.created_at_wb)) DESC NULLS LAST,
                         o.supply_id DESC
                LIMIT ? OFFSET ?
                """
            ),
            tuple(params + [safe_size, offset]),
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        d = repo._row_to_dict(row)
        supply_id = str(d.get("supply_id") or "").strip()
        raw = _parse_json_obj(d.get("raw_json"))
        order_ids_agg = d.get("order_ids_agg") or []
        order_ids: list[int] = []
        if isinstance(order_ids_agg, (list, tuple)):
            for oid in order_ids_agg:
                try:
                    order_ids.append(int(oid))
                except (TypeError, ValueError):
                    continue
        boxes = _parse_json_list(d.get("boxes_json"))
        offices = _parse_json_list(d.get("offices_json"))
        office_names = [str(x).strip() for x in offices if str(x or "").strip()]
        cargo_type = d.get("cargo_type") if d.get("cargo_type") not in (None, 0) else d.get("order_cargo_type")
        done = bool(int(d.get("done_int") or 0))
        scan_dt = d.get("scan_dt")
        name = str(d.get("name") or "").strip()
        if not name and d.get("created_at_wb"):
            # Fallback like portal: «Поставка от DD.MM.YYYY»
            try:
                created = datetime.fromisoformat(str(d["created_at_wb"]).replace("Z", "+00:00"))
                name = f"Поставка от {created.strftime('%d.%m.%Y')}"
            except Exception:
                name = f"Поставка {supply_id}"
        elif not name:
            name = f"Поставка {supply_id}" if supply_id else "Поставка"

        # Portal shows seller WH + destination office; API gives destination in offices[].
        warehouse_label = ", ".join(office_names) if office_names else (
            f"Склад {d.get('warehouse_id')}" if d.get("warehouse_id") else "—"
        )
        warehouse_sub = ""
        if d.get("destination_office_id") and not office_names:
            warehouse_sub = f"Офис {d.get('destination_office_id')}"

        pickup_allowed = bool(raw.get("isPickupPointShipmentAllowed"))
        order_count = int(d.get("order_count") or 0) or len(order_ids)
        boxes_count = len(boxes)
        if tab_key == TAB_ASSEMBLY:
            status_label = assembly_stage_label(done=done, boxes_count=boxes_count)
        else:
            status_label = supply_status_label(done=done, scan_dt=scan_dt)

        items.append(
            {
                "supply_id": supply_id,
                "source_id": d.get("source_id"),
                "name": name,
                "done": done,
                "cargo_type": cargo_type or 0,
                "cargo_label": cargo_type_label(cargo_type),
                "pickup_allowed": pickup_allowed,
                "created_at_wb": d.get("created_at_wb"),
                "closed_at_wb": d.get("closed_at_wb"),
                "scan_dt": scan_dt,
                "status_label": status_label,
                "order_count": order_count,
                "boxes_count": boxes_count,
                "order_ids": order_ids,
                "boxes": boxes,
                "warehouse_id": d.get("warehouse_id"),
                "warehouse_label": warehouse_label,
                "warehouse_sub": warehouse_sub,
                "destination_office_id": d.get("destination_office_id"),
            }
        )

    return {
        "items": items,
        "total": int(total_row["n"]) if total_row else 0,
        "page": safe_page,
        "page_size": safe_size,
        "counts": _tab_counts(repo, user_id=user_id, source_id=source_id),
    }


def _persist_supply_boxes(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    supply_id: str,
    boxes: list[dict[str, Any]],
    allow_empty: bool = False,
) -> None:
    if not supply_id:
        return
    if not boxes and not allow_empty:
        return
    with repo._connect() as conn:
        conn.execute(
            repo._sql(
                """
                UPDATE wb_fbs_supplies
                SET boxes_json = ?, synced_at = ?
                WHERE user_id = ? AND source_id = ? AND supply_id = ?
                """
            ),
            (json.dumps(boxes or [], ensure_ascii=False), _utc_now(), user_id, source_id, supply_id),
        )


def enrich_delivery_supplies_from_wb(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    api_key: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Refresh cargo boxes (trbx) + supply flags from WB when local cache is incomplete."""
    if not items or not api_key or not source_id:
        return items
    client = WbFbsClient(api_key)
    for item in items:
        sid = str(item.get("supply_id") or "").strip()
        if not sid:
            continue
        # Always normalize portal status from done/scanDt (API has no status string).
        item["status_label"] = supply_status_label(
            done=item.get("done"), scan_dt=item.get("scan_dt")
        )
        need_meta = not str(item.get("name") or "").strip()
        need_boxes = int(item.get("boxes_count") or 0) <= 0
        if not need_meta and not need_boxes:
            continue

        supply: dict[str, Any] = {}
        if need_meta:
            try:
                supply = client.get_supply(sid)
                time.sleep(0.21)
            except Exception as exc:
                _log.debug("enrich get_supply %s: %s", sid, exc)
                supply = {}
        if supply:
            done = bool(supply.get("done"))
            scan_dt = _parse_dt(supply.get("scanDt"))
            name = str(supply.get("name") or "").strip()
            item["done"] = done
            if scan_dt:
                item["scan_dt"] = scan_dt
            item["closed_at_wb"] = _parse_dt(supply.get("closedAt")) or item.get("closed_at_wb")
            if name:
                item["name"] = name
            if supply.get("cargoType") is not None:
                item["cargo_type"] = int(supply.get("cargoType") or 0)
                item["cargo_label"] = cargo_type_label(item["cargo_type"])
            item["pickup_allowed"] = bool(supply.get("isPickupPointShipmentAllowed"))
            item["destination_office_id"] = supply.get("destinationOfficeId")
            item["status_label"] = supply_status_label(done=done, scan_dt=item.get("scan_dt"))
            try:
                upsert_supply(
                    repo,
                    user_id=user_id,
                    source_id=source_id,
                    supply=supply,
                    order_ids=None,
                    boxes=None,
                )
            except Exception as exc:
                _log.debug("enrich upsert_supply %s: %s", sid, exc)

        if need_boxes:
            try:
                boxes = client.get_supply_boxes(sid)
                time.sleep(0.21)
            except Exception as exc:
                _log.debug("enrich get_supply_boxes %s: %s", sid, exc)
                boxes = []
            if boxes:
                item["boxes"] = boxes
                item["boxes_count"] = len(boxes)
                try:
                    _persist_supply_boxes(
                        repo,
                        user_id=user_id,
                        source_id=int(source_id),
                        supply_id=sid,
                        boxes=boxes,
                    )
                except Exception as exc:
                    _log.debug("enrich persist boxes %s: %s", sid, exc)
    return items


def clear_source_data(repo: ReviewRepository, *, user_id: int, source_id: int) -> dict[str, int]:
    ensure_wb_fbs_tables(repo)
    with repo._connect() as conn:
        o = conn.execute(
            repo._sql("DELETE FROM wb_fbs_orders WHERE user_id = ? AND source_id = ?"),
            (user_id, source_id),
        )
        s = conn.execute(
            repo._sql("DELETE FROM wb_fbs_supplies WHERE user_id = ? AND source_id = ?"),
            (user_id, source_id),
        )
    return {"orders": int(o.rowcount or 0), "supplies": int(s.rowcount or 0)}


def sync_wb_fbs_source(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    api_key: str,
    stop_requested: Callable[[], bool] | None = None,
    progress: Callable[[str, int], None] | None = None,
    # Supplies ship daily — a short window is enough for assembly/delivery
    # reconciliation via GET /api/v3/orders (WB max period is 30 days).
    lookback_days: int = 3,
    # Archive / finished / cancelled tabs are hidden in UI; do not spend sync
    # quota on archive pages (0 = skip). Status refresh below is limited to
    # operational tabs only (new / assembly / delivery).
    archive_pages: int = 0,
) -> dict[str, Any]:
    """Incremental sync for one WB supply source. Respects stop_requested between pages."""
    ensure_wb_fbs_tables(repo)
    client = WbFbsClient(api_key)
    stopped = False
    # Unique IDs — same order can appear in /new and /orders; do not double-count.
    seen_order_ids: set[int] = set()
    seen_supply_ids: set[str] = set()
    errors: list[str] = []

    def _stopped() -> bool:
        return bool(stop_requested and stop_requested())

    def _order_count() -> int:
        return len(seen_order_ids)

    def _supply_count() -> int:
        return len(seen_supply_ids)

    def _note_order(order: dict[str, Any]) -> None:
        oid = order.get("id")
        if oid is None:
            return
        try:
            seen_order_ids.add(int(oid))
        except (TypeError, ValueError):
            return

    def _prog(msg: str, n: int | None = None) -> None:
        if progress:
            progress(msg, _order_count() if n is None else n)

    # 1) New orders — also probes Marketplace token scope early.
    _prog("Новые заказы…")
    try:
        new_orders = client.get_new_orders()
        for order in new_orders:
            if _stopped():
                stopped = True
                break
            upsert_order(
                repo,
                user_id=user_id,
                source_id=source_id,
                order=order,
                supplier_status="new",
                is_archive=False,
            )
            _note_order(order)
        time.sleep(0.21)
    except Exception as exc:
        _log.warning("wb_fbs new orders failed: %s", exc)
        if is_marketplace_scope_error(exc):
            return {
                "orders": 0,
                "supplies": 0,
                "errors": [],
                "stopped": False,
                "scope_error": True,
                "message": SCOPE_ERROR_MESSAGE,
            }
        errors.append(friendly_sync_error("new", exc))

    if _stopped():
        return {
            "orders": _order_count(),
            "supplies": _supply_count(),
            "errors": errors,
            "stopped": True,
        }

    # 2) Recent orders pages
    _prog("Заказы за период…")
    date_to = datetime.now(UTC)
    date_from = date_to - timedelta(days=max(1, min(lookback_days, 30)))
    next_token: int | None = 0
    pages = 0
    try:
        while pages < 20:
            if _stopped():
                stopped = True
                break
            orders, next_token = client.get_orders_page(
                limit=1000,
                next_token=next_token if next_token is not None else 0,
                date_from=date_from,
                date_to=date_to,
            )
            if not orders:
                break
            for order in orders:
                upsert_order(repo, user_id=user_id, source_id=source_id, order=order)
                _note_order(order)
            pages += 1
            _prog(f"Заказы… стр. {pages}")
            if next_token is None:
                break
            time.sleep(0.25)
    except Exception as exc:
        _log.warning("wb_fbs orders page failed: %s", exc)
        if is_marketplace_scope_error(exc):
            return {
                "orders": _order_count(),
                "supplies": _supply_count(),
                "errors": [],
                "stopped": False,
                "scope_error": True,
                "message": SCOPE_ERROR_MESSAGE,
            }
        errors.append(friendly_sync_error("orders", exc))

    if _stopped():
        return {
            "orders": _order_count(),
            "supplies": _supply_count(),
            "errors": errors,
            "stopped": True,
        }

    # 3) Refresh statuses for operational tabs only (new / assembly / delivery).
    # Finished / cancelled / archive are hidden and not re-polled every sync.
    _prog("Статусы…")
    with repo._connect() as conn:
        id_rows = conn.execute(
            repo._sql(
                """
                SELECT order_id FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ?
                  AND is_archive = FALSE
                  AND tab IN ('new', 'assembly', 'delivery')
                ORDER BY synced_at DESC
                LIMIT 5000
                """
            ),
            (user_id, source_id),
        ).fetchall()
    all_ids = [int(r["order_id"]) for r in id_rows]
    # Stock ledger (Поставки → Остатки): remember final tab/article per order
    # after status refresh, then reconcile. Must never break FBS sync.
    stock_order_states: dict[int, dict[str, Any]] = {}
    for i in range(0, len(all_ids), 1000):
        if _stopped():
            stopped = True
            break
        chunk = all_ids[i : i + 1000]
        try:
            statuses = client.get_statuses(chunk)
            status_map = {
                int(s["id"]): s
                for s in statuses
                if isinstance(s, dict) and s.get("id") is not None
            }
            with repo._connect() as conn:
                prior_rows = conn.execute(
                    repo._sql(
                        f"""
                        SELECT order_id, tab, article, nm_id FROM wb_fbs_orders
                        WHERE user_id = ? AND source_id = ?
                          AND order_id IN ({", ".join("?" for _ in chunk)})
                        """
                    ),
                    (user_id, source_id, *chunk),
                ).fetchall()
                prior_map = {
                    int(r["order_id"]): {
                        "tab": str(r["tab"] or ""),
                        "article": str(r["article"] or ""),
                        "nm_id": str(r["nm_id"] or ""),
                    }
                    for r in prior_rows
                }
                for oid, st in status_map.items():
                    ss = str(st.get("supplierStatus") or "")
                    ws = str(st.get("wbStatus") or "")
                    tab = compute_tab(supplier_status=ss, wb_status=ws, is_archive=False)
                    prev = prior_map.get(oid) or {}
                    stock_order_states[oid] = {
                        "order_id": oid,
                        "tab": tab,
                        "article": str(prev.get("article") or ""),
                        "nm_id": str(prev.get("nm_id") or ""),
                    }
                    conn.execute(
                        repo._sql(
                            """
                            UPDATE wb_fbs_orders
                            SET supplier_status = ?, wb_status = ?, tab = ?, synced_at = ?
                            WHERE user_id = ? AND source_id = ? AND order_id = ?
                            """
                        ),
                        (ss, ws, tab, _utc_now(), user_id, source_id, oid),
                    )
            time.sleep(0.21)
        except Exception as exc:
            errors.append(friendly_sync_error("status", exc))
            break

    # Reconcile every sync (not only on tab change): repairs missed ships after
    # a previous stock failure and covers assembly→finished (skips delivery tab).
    if stock_order_states:
        try:
            prod_id = 0
            try:
                productions = repo.list_supply_productions(user_id=user_id)
                if productions:
                    prod_id = int(productions[0].get("id") or 0)
            except Exception:
                prod_id = 0
            if prod_id > 0:
                try:
                    move_date = datetime.now(_MSK).date().isoformat()
                except Exception:
                    move_date = datetime.now(UTC).date().isoformat()
                repo.reconcile_wb_fbs_stock_orders(
                    user_id=user_id,
                    production_id=prod_id,
                    orders=list(stock_order_states.values()),
                    movement_date=move_date,
                )
        except Exception as exc:
            _log.warning(
                "supply stock ledger hook failed user=%s source=%s: %s",
                user_id,
                source_id,
                exc,
            )

    if _stopped():
        return {
            "orders": _order_count(),
            "supplies": _supply_count(),
            "errors": errors,
            "stopped": True,
        }

    # 4) Supplies (+ order ids / boxes for open ones only)
    _prog("Поставки FBS…", _supply_count())
    next_sup = 0
    sup_pages = 0
    try:
        while sup_pages < 10:
            if _stopped():
                stopped = True
                break
            supplies, next_sup = client.get_supplies(limit=1000, next_token=next_sup)
            if not supplies:
                break
            for supply in supplies:
                if _stopped():
                    stopped = True
                    break
                sid = str(supply.get("id") or "")
                order_ids: list[int] = []
                boxes: list[dict[str, Any]] = []
                is_done = bool(supply.get("done"))
                if sid and not is_done:
                    try:
                        # Read-only WB calls (no deliver/cancel/add). ≥200 ms gap.
                        order_ids = client.get_supply_order_ids(sid)
                        time.sleep(0.21)
                        boxes = client.get_supply_boxes(sid)
                        time.sleep(0.21)
                    except Exception as exc:
                        errors.append(friendly_sync_error(f"supply {sid}", exc))
                elif sid and is_done:
                    # After PATCH /deliver WB sets done=true, but the supply stays
                    # on portal «В доставке» — still need trbx cargo places.
                    try:
                        boxes = client.get_supply_boxes(sid)
                        time.sleep(0.21)
                    except Exception as exc:
                        _log.debug("boxes for done supply %s: %s", sid, exc)
                upsert_supply(
                    repo,
                    user_id=user_id,
                    source_id=source_id,
                    supply=supply,
                    order_ids=order_ids,
                    boxes=boxes,
                )
                if sid:
                    seen_supply_ids.add(sid)
                # Link orders to supply + mark assembly if still new
                if order_ids:
                    with repo._connect() as conn:
                        for oid in order_ids:
                            conn.execute(
                                repo._sql(
                                    """
                                    UPDATE wb_fbs_orders
                                    SET supply_id = ?,
                                        supplier_status = CASE
                                            WHEN supplier_status = 'new' OR supplier_status = '' THEN 'confirm'
                                            ELSE supplier_status
                                        END,
                                        tab = CASE
                                            WHEN tab = 'new' THEN 'assembly'
                                            ELSE tab
                                        END,
                                        synced_at = ?
                                    WHERE user_id = ? AND source_id = ? AND order_id = ?
                                    """
                                ),
                                (sid, _utc_now(), user_id, source_id, oid),
                            )
            sup_pages += 1
            _prog(f"Поставки… стр. {sup_pages}", _supply_count())
            if not next_sup:
                break
            time.sleep(0.25)
    except Exception as exc:
        errors.append(friendly_sync_error("supplies", exc))

    if _stopped():
        return {
            "orders": _order_count(),
            "supplies": _supply_count(),
            "errors": errors,
            "stopped": True,
        }

    # 5) Archive — disabled by default (hidden tabs; save API quota).
    if archive_pages > 0:
        _prog("Архив…")
        arch_next = 0
        try:
            for _ in range(max(0, archive_pages)):
                if _stopped():
                    stopped = True
                    break
                arch_orders, arch_next = client.get_archive_orders(
                    limit=1000, next_token=arch_next
                )
                if not arch_orders:
                    break
                for order in arch_orders:
                    upsert_order(
                        repo,
                        user_id=user_id,
                        source_id=source_id,
                        order=order,
                        is_archive=True,
                        supplier_status=str(order.get("supplierStatus") or ""),
                        wb_status=str(order.get("wbStatus") or "sold"),
                    )
                    _note_order(order)
                if not arch_next:
                    break
                time.sleep(0.25)
        except Exception as exc:
            errors.append(friendly_sync_error("archive", exc))

    return {
        "orders": _order_count(),
        "supplies": _supply_count(),
        "errors": errors,
        "stopped": stopped,
    }


# Sync state for web layer
_wb_fbs_sync_state: dict[str, object] = {
    "in_progress": False,
    "synced": 0,
    "total": 0,
    "message": "",
    "errors": [],
    "cancel_requested": False,
    "source_id": None,
    "source_ids": [],
    "pallet_summary": [],
}
_wb_fbs_sync_lock = threading.Lock()


def _as_positive_int(value: object) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def format_pallets_ru(value: float) -> str:
    """Format pallet count with up to 2 decimals (comma) + Russian noun."""
    n = round(float(value or 0.0) + 1e-12, 2)
    if abs(n - int(n)) < 1e-9:
        whole = int(n)
        text = str(whole)
        abs_n = abs(whole) % 100
        last = abs_n % 10
        if 11 <= abs_n <= 14:
            word = "паллет"
        elif last == 1:
            word = "паллета"
        elif 2 <= last <= 4:
            word = "паллеты"
        else:
            word = "паллет"
    else:
        text = f"{n:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        word = "паллета"
    return f"{text} {word}"


def format_boxes_ru(value: float) -> str:
    """Format box count with up to 2 decimals (comma) + Russian noun."""
    n = round(float(value or 0.0) + 1e-12, 2)
    if abs(n - int(n)) < 1e-9:
        whole = int(n)
        text = str(whole)
        abs_n = abs(whole) % 100
        last = abs_n % 10
        if 11 <= abs_n <= 14:
            word = "коробов"
        elif last == 1:
            word = "короб"
        elif 2 <= last <= 4:
            word = "короба"
        else:
            word = "коробов"
    else:
        text = f"{n:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        word = "короба"
    return f"{text} {word}"


def compute_wb_fbs_pallet_summary(
    repo: ReviewRepository,
    *,
    user_id: int,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pallets + boxes per FBS source from tabs «Новые» + «На сборке».

    ``boxes = Σ (qty / box_qty)``
    ``pallets = Σ (qty / box_qty / boxes_per_pallet)``
    Both rounded to hundredths. Products without ``box_qty`` are skipped for
    both; without category ``boxes_per_pallet`` they still count toward boxes.
    """
    ensure_wb_fbs_tables(repo)
    if not sources:
        return []

    products = repo.list_product_photos(user_id=user_id)
    categories = repo.list_product_categories(user_id=user_id, seed_defaults=True)
    cat_boxes: dict[str, int] = {}
    for cat in categories:
        name = str(cat.get("name") or "").strip()
        bpp = _as_positive_int(cat.get("boxes_per_pallet"))
        if name and bpp is not None:
            cat_boxes[name] = bpp

    # article / nmId / casefold → (box_qty, boxes_per_pallet | None)
    product_meta: dict[str, tuple[int, int | None]] = {}
    for prod in products:
        box_qty = _as_positive_int(prod.get("box_qty"))
        if box_qty is None:
            continue
        cat_name = str(prod.get("product_category") or "").strip()
        bpp = cat_boxes.get(cat_name)
        meta = (box_qty, bpp)
        for raw_key in (
            prod.get("supplier_article"),
            prod.get("wb_nmid"),
            prod.get("ozon_sku"),
            prod.get("yandex_offer_id"),
        ):
            key = str(raw_key or "").strip()
            if not key:
                continue
            product_meta[key] = meta
            product_meta[key.casefold()] = meta

    source_names: dict[int, str] = {}
    source_ids: list[int] = []
    for src in sources:
        try:
            sid = int(src.get("source_id") if "source_id" in src else src.get("id"))
        except (TypeError, ValueError):
            continue
        source_ids.append(sid)
        source_names[sid] = str(
            src.get("name") or f"Источник {sid}"
        ).strip() or f"Источник {sid}"

    if not source_ids:
        return []

    placeholders = ", ".join("?" for _ in source_ids)
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT source_id, article, nm_id, COUNT(*) AS qty
                FROM wb_fbs_orders
                WHERE user_id = ?
                  AND tab IN (?, ?)
                  AND source_id IN ({placeholders})
                GROUP BY source_id, article, nm_id
                """
            ),
            tuple([user_id, TAB_NEW, TAB_ASSEMBLY, *source_ids]),
        ).fetchall()

    totals_pallets: dict[int, float] = {sid: 0.0 for sid in source_ids}
    totals_boxes: dict[int, float] = {sid: 0.0 for sid in source_ids}
    for row in rows:
        d = repo._row_to_dict(row)
        try:
            sid = int(d.get("source_id"))
            qty = int(d.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if sid not in totals_pallets or qty <= 0:
            continue
        article = str(d.get("article") or "").strip()
        nm_id = str(d.get("nm_id") or "").strip()
        meta = None
        for key in (article, nm_id, article.casefold(), nm_id.casefold()):
            if key and key in product_meta:
                meta = product_meta[key]
                break
        if not meta:
            continue
        box_qty, bpp = meta
        boxes = float(qty) / float(box_qty)
        totals_boxes[sid] += boxes
        if bpp is not None:
            totals_pallets[sid] += boxes / float(bpp)

    summary: list[dict[str, Any]] = []
    for sid in source_ids:
        pallets = round(float(totals_pallets.get(sid) or 0.0) + 1e-12, 2)
        boxes = round(float(totals_boxes.get(sid) or 0.0) + 1e-12, 2)
        pallets_label = format_pallets_ru(pallets)
        boxes_label = format_boxes_ru(boxes)
        summary.append(
            {
                "source_id": sid,
                "name": source_names.get(sid) or f"Источник {sid}",
                "pallets": pallets,
                "boxes": boxes,
                "boxes_label": boxes_label,
                "pallets_label": f"{pallets_label} ({boxes_label})",
            }
        )
    return summary


def get_sync_state() -> dict[str, object]:
    with _wb_fbs_sync_lock:
        return dict(_wb_fbs_sync_state)


def request_sync_stop() -> bool:
    with _wb_fbs_sync_lock:
        if _wb_fbs_sync_state.get("in_progress"):
            _wb_fbs_sync_state["cancel_requested"] = True
            return True
    return False


def start_sync_thread(
    *,
    repo: ReviewRepository,
    user_id: int,
    sources: list[dict[str, Any]],
    is_auto: bool = False,
    lookback_days: int | None = None,
) -> tuple[bool, str]:
    """Sync all provided FBS sources sequentially (same set as the UI picker)."""
    jobs: list[dict[str, Any]] = []
    for raw in sources:
        try:
            sid = int(raw.get("source_id") or raw.get("id"))
        except (TypeError, ValueError):
            continue
        api_key = str(raw.get("api_key") or "").strip()
        if not api_key:
            continue
        jobs.append(
            {
                "source_id": sid,
                "api_key": api_key,
                "name": str(raw.get("name") or f"Источник {sid}"),
            }
        )
    if not jobs:
        return False, "Нет источников с «ФБС» в названии для синхронизации"

    # Prefer caller override; else tenant setting; else code default (3).
    effective_lookback = 3
    if lookback_days is not None:
        try:
            effective_lookback = max(1, min(30, int(lookback_days)))
        except (TypeError, ValueError):
            effective_lookback = 3
    else:
        try:
            settings = repo.get_wb_fbs_auto_sync_settings(user_id=user_id)
            effective_lookback = max(1, min(30, int(settings.get("lookback_days") or 3)))
        except Exception:
            effective_lookback = 3

    with _wb_fbs_sync_lock:
        if _wb_fbs_sync_state.get("in_progress"):
            return False, "Синхронизация уже запущена"
        _wb_fbs_sync_state.update(
            {
                "in_progress": True,
                "synced": 0,
                "total": len(jobs),
                "message": f"Запуск… источников: {len(jobs)}",
                "errors": [],
                "cancel_requested": False,
                "source_id": jobs[0]["source_id"],
                "source_ids": [j["source_id"] for j in jobs],
                "pallet_summary": [],
            }
        )

    def _run() -> None:
        def stop_requested() -> bool:
            with _wb_fbs_sync_lock:
                return bool(_wb_fbs_sync_state.get("cancel_requested"))

        total_orders = 0
        total_supplies = 0
        all_errors: list[str] = []
        scope_failures = 0
        stopped = False
        synced_sources = 0

        try:
            for idx, job in enumerate(jobs, start=1):
                if stop_requested():
                    stopped = True
                    break
                sid = int(job["source_id"])
                label = str(job["name"])
                with _wb_fbs_sync_lock:
                    _wb_fbs_sync_state["source_id"] = sid
                    _wb_fbs_sync_state["message"] = (
                        f"Источник {idx}/{len(jobs)}: {label}"
                    )

                def progress(msg: str, n: int, _label: str = label, _idx: int = idx) -> None:
                    with _wb_fbs_sync_lock:
                        _wb_fbs_sync_state["message"] = (
                            f"[{_idx}/{len(jobs)}] {_label}: {msg}"
                        )
                        _wb_fbs_sync_state["synced"] = total_orders + int(n or 0)

                try:
                    result = sync_wb_fbs_source(
                        repo,
                        user_id=user_id,
                        source_id=sid,
                        api_key=str(job["api_key"]),
                        stop_requested=stop_requested,
                        progress=progress,
                        lookback_days=effective_lookback,
                    )
                except Exception as exc:
                    _log.exception("wb_fbs sync failed for source %s", sid)
                    if is_marketplace_scope_error(exc):
                        scope_failures += 1
                        all_errors.append(f"{label}: {SCOPE_ERROR_MESSAGE}")
                    else:
                        all_errors.append(f"{label}: {exc}")
                    continue

                if result.get("scope_error"):
                    scope_failures += 1
                    all_errors.append(
                        f"{label}: {result.get('message') or SCOPE_ERROR_MESSAGE}"
                    )
                    continue

                errs = list(result.get("errors") or [])
                safe_errs = [e for e in errs if not is_marketplace_scope_error(e)]
                if errs and not safe_errs:
                    scope_failures += 1
                    all_errors.append(f"{label}: {SCOPE_ERROR_MESSAGE}")
                    continue

                total_orders += int(result.get("orders") or 0)
                total_supplies += int(result.get("supplies") or 0)
                synced_sources += 1
                for err in safe_errs:
                    all_errors.append(f"{label}: {err}")
                if result.get("stopped"):
                    stopped = True
                    break

            if synced_sources > 0:
                # Tenant-level FBS timestamp — do NOT reuse supply_sources.last_synced_at
                # (that column is shared with FBW/Ozon supplies sync).
                try:
                    repo.mark_wb_fbs_synced(user_id=user_id, is_auto=bool(is_auto))
                except Exception:
                    _log.warning("wb_fbs: failed to update wb_fbs_last_synced_at for user %s", user_id)

            pallet_summary: list[dict[str, Any]] = []
            if synced_sources > 0:
                try:
                    pallet_summary = compute_wb_fbs_pallet_summary(
                        repo, user_id=user_id, sources=jobs
                    )
                except Exception:
                    _log.exception(
                        "wb_fbs pallet summary failed user=%s", user_id
                    )
                    pallet_summary = []

            with _wb_fbs_sync_lock:
                _wb_fbs_sync_state["synced"] = total_orders
                _wb_fbs_sync_state["pallet_summary"] = pallet_summary
                if synced_sources == 0 and scope_failures == len(jobs):
                    _wb_fbs_sync_state["errors"] = []
                    _wb_fbs_sync_state["message"] = SCOPE_ERROR_MESSAGE
                    _wb_fbs_sync_state["pallet_summary"] = []
                else:
                    _wb_fbs_sync_state["errors"] = all_errors[:8]
                    stats_part = (
                        f"Источников: {synced_sources}/{len(jobs)} | "
                        f"Заказов: {total_orders} | "
                        f"Поставок: {total_supplies}"
                    )
                    if stopped:
                        _wb_fbs_sync_state["message"] = f"Остановлено. {stats_part}"
                    elif all_errors:
                        _wb_fbs_sync_state["message"] = (
                            f"Готово с ошибками. {stats_part}"
                        )
                    else:
                        _wb_fbs_sync_state["message"] = f"Готово. {stats_part}"
        except Exception as exc:
            _log.exception("wb_fbs multi-source sync failed")
            with _wb_fbs_sync_lock:
                if is_marketplace_scope_error(exc):
                    _wb_fbs_sync_state["errors"] = []
                    _wb_fbs_sync_state["message"] = SCOPE_ERROR_MESSAGE
                else:
                    _wb_fbs_sync_state["errors"] = [str(exc)]
                    _wb_fbs_sync_state["message"] = f"Ошибка: {exc}"
                _wb_fbs_sync_state["pallet_summary"] = []
        finally:
            with _wb_fbs_sync_lock:
                _wb_fbs_sync_state["in_progress"] = False
                _wb_fbs_sync_state["cancel_requested"] = False

    threading.Thread(target=_run, daemon=True, name="wb-fbs-sync").start()
    return True, f"Синхронизация запущена ({len(jobs)} ист.)"


def list_fbs_sync_jobs(repo: ReviewRepository, *, user_id: int) -> list[dict[str, Any]]:
    """Build sync jobs for all enabled WB sources whose name contains ФБС/FBS."""
    sources = [
        s
        for s in repo.list_supply_sources(user_id=user_id)
        if (s.get("marketplace") or "wb").lower() == "wb"
        and s.get("is_enabled")
        and is_fbs_source_name(s.get("name"))
    ]
    jobs: list[dict[str, Any]] = []
    for s in sources:
        try:
            sid = int(s["id"])
        except (TypeError, ValueError):
            continue
        src_full = repo.get_supply_source_with_key(user_id=user_id, source_id=sid)
        if not src_full or not src_full.get("api_key"):
            continue
        jobs.append(
            {
                "source_id": sid,
                "api_key": str(src_full["api_key"]),
                "name": str(src_full.get("name") or s.get("name") or f"Источник {sid}"),
            }
        )
    return jobs


def _fbs_auto_sync_is_due(
    *,
    last_synced_at: str | None,
    interval_minutes: int | None = None,
    interval_hours: int | None = None,
) -> bool:
    """Due when FBS orders sync never ran or interval elapsed since last FBS sync."""
    if not last_synced_at:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last_synced_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=UTC)
    if interval_minutes is None:
        interval_minutes = int(interval_hours or 1) * 60
    minutes = max(1, int(interval_minutes or 1))
    return (datetime.now(UTC) - last_dt).total_seconds() >= float(minutes) * 60.0


def _parse_hhmm_to_time(value: object, *, default: str) -> dt_time:
    raw = ReviewRepository._normalize_hhmm(value, default=default)
    hour_s, minute_s = raw.split(":", 1)
    return dt_time(hour=int(hour_s), minute=int(minute_s))


def _msk_now() -> datetime:
    return datetime.now(_MSK)


def _msk_time_in_active_window(
    *,
    now_msk: datetime | None = None,
    active_from: object = "12:00",
    active_to: object = "06:00",
) -> bool:
    """True if MSK local time is inside [from, to] inclusive; supports overnight windows."""
    now = now_msk or _msk_now()
    start = _parse_hhmm_to_time(active_from, default="12:00")
    end = _parse_hhmm_to_time(active_to, default="06:00")
    current = now.astimezone(_MSK).time().replace(second=0, microsecond=0)
    if start == end:
        return True
    if start < end:
        return start <= current <= end
    # Overnight, e.g. 12:00 → 06:00 (active at/after noon or at/before 06:00).
    return current >= start or current <= end


def _open_supply_counts_for_auto_mgt(open_supplies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Supplies that block/participate in auto MGT: empty or cargo MGT.

    Non-empty non-MGT (e.g. SGT) are ignored for the «several supplies» gate.
    """
    out: list[dict[str, Any]] = []
    for s in open_supplies:
        if _supply_is_empty(s) or int(s.get("cargo_type") or 0) == 1:
            out.append(s)
    return out


def plan_auto_collect_mgt_decisions(
    preview: dict[str, Any],
    *,
    open_supplies: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Build safe auto decisions for «Собрать все МГТ» or return skip reason.

    Auto never opens a modal: any ambiguity / name conflict → skip.
    """
    groups = [g for g in (preview.get("groups") or []) if isinstance(g, dict)]
    if not groups or not int(preview.get("mgt_count") or 0):
        return None, "no_mgt"

    supplies = list(open_supplies or [])
    if not supplies:
        # Reconstruct minimal open supply list from preview existing names only
        # is not enough for cargo checks — caller should pass open supplies.
        supplies = []

    relevant = _open_supply_counts_for_auto_mgt(supplies)
    if len(relevant) > 1:
        return None, "several_open_supplies"

    existing_names = {
        str(x or "").strip()
        for x in (preview.get("existing_names") or [])
        if str(x or "").strip()
    }
    decisions: list[dict[str, Any]] = []
    claimed_create_names: set[str] = set()

    for g in groups:
        mode = str(g.get("mode") or "").strip()
        gkey = str(g.get("group_key") or "").strip()
        is_b2b = bool(g.get("is_b2b"))
        if not gkey:
            return None, "invalid_group"
        if mode == "choose":
            return None, "needs_choice"
        if mode == "add_one":
            sid = str(g.get("default_supply_id") or "").strip()
            if not sid:
                return None, "missing_supply"
            decisions.append(
                {
                    "group_key": gkey,
                    "is_b2b": is_b2b,
                    "action": "add",
                    "supply_id": sid,
                }
            )
            continue
        if mode == "create":
            # Exact template only — never auto-suffix «(2)».
            template = default_mgt_supply_name(is_b2b=is_b2b)
            if template in existing_names or template in claimed_create_names:
                return None, "name_conflict"
            claimed_create_names.add(template)
            decisions.append(
                {
                    "group_key": gkey,
                    "is_b2b": is_b2b,
                    "action": "create",
                    "name": template,
                }
            )
            continue
        return None, f"unsupported_mode:{mode or 'empty'}"

    if not decisions:
        return None, "no_decisions"
    return decisions, ""


_AUTO_COLLECT_REASON_RU: dict[str, str] = {
    "several_open_supplies": (
        "На сборке несколько поставок (МГТ или пустых) — доступен только ручной сбор"
    ),
    "needs_choice": "Нужен выбор поставки оператором",
    "name_conflict": "Поставка с названием шаблона уже есть — автосбор пропущен",
    "no_mgt": "Нет МГТ-заказов во вкладке «Новые»",
    "missing_supply": "Не найдена поставка для автоматического добавления",
    "invalid_group": "Некорректная группа заказов для автосбора",
    "no_decisions": "Нет безопасных действий для автосбора",
    "no_fbs_sources": "Нет источников ФБС для автосбора",
    "nothing_to_do": "Нечего выполнять",
    "disabled": "Автосбор МГТ выключен",
    "outside_window": "Вне окна активности (МСК)",
    "not_due": "Интервал автосбора ещё не прошёл",
}


def auto_collect_reason_ru(code: object) -> str:
    """Human-readable reason for auto-collect skip/error codes."""
    key = str(code or "").strip()
    if not key:
        return ""
    if key in _AUTO_COLLECT_REASON_RU:
        return _AUTO_COLLECT_REASON_RU[key]
    if key.startswith("list_jobs_error:"):
        return "Ошибка получения списка источников: " + key.split(":", 1)[-1].strip()
    if key.startswith("unsupported_mode:"):
        return "Неподдерживаемый режим сборки — нужен ручной сбор"
    return key


def _auto_collect_supply_name_map(open_supplies: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in open_supplies:
        sid = str(s.get("supply_id") or "").strip()
        if not sid:
            continue
        out[sid] = str(s.get("name") or sid).strip() or sid
    return out


def run_auto_collect_mgt_for_owner(
    repo: ReviewRepository,
    *,
    user_id: int,
) -> dict[str, Any]:
    """Run safe auto-collect MGT for all FBS sources of one owner.

    Records last_run even when skipped for ambiguity (avoids 60s retry spam).
    Does not record last_run when outside the MSK active window or interval.
    """
    try:
        settings = repo.get_wb_fbs_auto_sync_settings(user_id=user_id)
    except Exception as exc:
        return {"ok": False, "ran": False, "message": str(exc)}

    if not settings.get("collect_mgt_enabled"):
        return {"ok": True, "ran": False, "message": "disabled"}

    if not _msk_time_in_active_window(
        active_from=settings.get("collect_mgt_active_from"),
        active_to=settings.get("collect_mgt_active_to"),
    ):
        return {"ok": True, "ran": False, "message": "outside_window"}

    collect_minutes = settings.get("collect_mgt_interval_minutes")
    if collect_minutes is None:
        collect_minutes = int(settings.get("collect_mgt_interval_hours") or 1) * 60
    if not _fbs_auto_sync_is_due(
        last_synced_at=settings.get("collect_mgt_last_run_at"),
        interval_minutes=int(collect_minutes or 60),
    ):
        return {"ok": True, "ran": False, "message": "not_due"}

    ran_at = _utc_now()
    ran_at_msk = _msk_now().strftime("%d.%m.%Y %H:%M")

    def _persist(status: str, detail: dict[str, Any]) -> None:
        try:
            repo.mark_wb_fbs_auto_collect_mgt_run(
                user_id=user_id, status=status, detail=detail
            )
        except Exception as exc:
            _log.warning("mark auto collect run user=%s: %s", user_id, exc)

    try:
        jobs = list_fbs_sync_jobs(repo, user_id=user_id)
    except Exception as exc:
        code = f"list_jobs_error:{exc}"
        status = auto_collect_reason_ru(code)
        detail = {
            "ran_at": ran_at,
            "ran_at_msk": ran_at_msk,
            "outcome": "error",
            "summary": status,
            "added_total": 0,
            "sources": [],
            "errors": [str(exc)],
        }
        _persist(status, detail)
        return {"ok": False, "ran": True, "message": status, "detail": detail}

    if not jobs:
        status = auto_collect_reason_ru("no_fbs_sources")
        detail = {
            "ran_at": ran_at,
            "ran_at_msk": ran_at_msk,
            "outcome": "skipped",
            "summary": status,
            "added_total": 0,
            "sources": [],
        }
        _persist(status, detail)
        return {"ok": True, "ran": True, "message": status, "detail": detail}

    source_rows: list[dict[str, Any]] = []
    added_total = 0
    any_error = False
    any_skip = False
    any_success = False

    for job in jobs:
        sid = int(job.get("source_id") or 0)
        api_key = str(job.get("api_key") or "")
        name = str(job.get("name") or sid)
        if not sid or not api_key:
            continue
        row: dict[str, Any] = {
            "source_id": sid,
            "source_name": name,
            "outcome": "skipped",
            "reason_code": "",
            "reason": "",
            "mgt_found": 0,
            "added": 0,
            "planned": 0,
            "supplies": [],
            "errors": [],
        }
        try:
            preview = preview_collect_mgt(repo, user_id=user_id, source_id=sid)
            open_supplies = list_supplies(
                repo, user_id=user_id, source_id=sid, only_open=True
            )
            for s in open_supplies:
                s["order_ids"] = s.get("order_ids") or _parse_json_list(
                    s.get("order_ids_json")
                )
            name_map = _auto_collect_supply_name_map(open_supplies)
            mgt_found = int(preview.get("mgt_count") or 0)
            row["mgt_found"] = mgt_found
            decisions, skip_reason = plan_auto_collect_mgt_decisions(
                preview, open_supplies=open_supplies
            )
            if decisions is None:
                row["reason_code"] = skip_reason
                row["reason"] = auto_collect_reason_ru(skip_reason)
                if skip_reason == "no_mgt":
                    row["outcome"] = "empty"
                else:
                    row["outcome"] = "skipped"
                    any_skip = True
                source_rows.append(row)
                continue

            result = execute_collect_mgt(
                repo,
                user_id=user_id,
                source_id=sid,
                api_key=api_key,
                decisions=decisions,
            )
            added = int(result.get("added") or 0)
            planned = int(result.get("planned_live") or 0)
            row["added"] = added
            row["planned"] = planned
            added_total += added
            errors = [
                str(x) for x in (result.get("errors") or []) if str(x or "").strip()
            ]
            row["errors"] = errors
            created_by_id = {
                str(c.get("id") or c.get("supply_id") or "").strip(): str(
                    c.get("name") or ""
                ).strip()
                for c in (result.get("created_supplies") or [])
                if isinstance(c, dict)
            }
            supplies_out: list[dict[str, Any]] = []
            for gr in result.get("groups") or []:
                if not isinstance(gr, dict):
                    continue
                gsid = str(gr.get("supply_id") or "").strip()
                gadded = int(gr.get("added") or 0)
                if not gsid and not gadded:
                    continue
                gname = (
                    created_by_id.get(gsid)
                    or name_map.get(gsid)
                    or gsid
                )
                # Infer action from decisions for this group.
                gkey = str(gr.get("group_key") or "")
                action = "add"
                for d in decisions:
                    if str(d.get("group_key") or "") == gkey:
                        action = str(d.get("action") or "add")
                        if action == "create" and d.get("name"):
                            gname = str(d.get("name") or gname)
                        break
                supplies_out.append(
                    {
                        "action": action,
                        "supply_id": gsid,
                        "name": gname or gsid,
                        "added": gadded,
                    }
                )
            row["supplies"] = supplies_out
            if errors and added <= 0:
                row["outcome"] = "error"
                row["reason"] = str(result.get("message") or "Ошибка автосбора")
                any_error = True
            elif errors or (planned and added < planned):
                row["outcome"] = "partial"
                row["reason"] = str(result.get("message") or "Частично выполнено")
                any_success = True
                any_skip = True
            elif added > 0:
                row["outcome"] = "added"
                row["reason"] = str(
                    result.get("message") or f"Добавлено {added} заказов на сборку"
                )
                any_success = True
            else:
                row["outcome"] = "empty"
                row["reason"] = str(result.get("message") or "Нечего добавлять")
            source_rows.append(row)
        except Exception as exc:
            _log.warning(
                "auto collect MGT user=%s source=%s error: %s", user_id, sid, exc
            )
            row["outcome"] = "error"
            row["reason"] = f"Ошибка API/сети: {exc}"
            row["errors"] = [str(exc)]
            any_error = True
            source_rows.append(row)

    if any_error and any_success:
        outcome = "partial"
    elif any_error:
        outcome = "error"
    elif any_success and any_skip:
        outcome = "partial"
    elif any_success:
        outcome = "success"
    elif any_skip:
        outcome = "skipped"
    else:
        outcome = "empty"

    if added_total > 0:
        summary = f"Добавлено на сборку: {added_total} заказ(ов)"
        if any_skip or any_error:
            summary += " · есть пропуски/ошибки"
    elif any_error:
        summary = "Автосбор завершился с ошибкой"
    elif any_skip:
        reasons = sorted(
            {
                str(r.get("reason") or "").strip()
                for r in source_rows
                if r.get("outcome") == "skipped" and r.get("reason")
            }
        )
        summary = reasons[0] if len(reasons) == 1 else "Автосбор пропущен — нужна ручная обработка"
    else:
        summary = auto_collect_reason_ru("no_mgt")

    detail = {
        "ran_at": ran_at,
        "ran_at_msk": ran_at_msk,
        "outcome": outcome,
        "summary": summary,
        "added_total": added_total,
        "sources": source_rows,
    }
    _persist(summary, detail)
    return {
        "ok": not any_error or any_success,
        "ran": True,
        "added": added_total > 0,
        "message": summary,
        "detail": detail,
    }


class WbFbsScheduler:
    """Background hourly (configurable) sync + auto-collect MGT for enabled tenants."""

    def __init__(self, repository: ReviewRepository) -> None:
        self.repository = repository
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="feedpilot-wb-fbs-scheduler",
                daemon=True,
            )
            self._thread.start()
            _log.info("WbFbsScheduler: started")

    def stop(self) -> None:
        self._stop_event.set()

    def _loop(self) -> None:
        check_interval = 60
        while not self._stop_event.is_set():
            self._stop_event.wait(check_interval)
            if self._stop_event.is_set():
                break
            try:
                self._run_due_owners()
            except Exception as exc:
                _log.warning("WbFbsScheduler loop error: %s", exc)

    def _run_due_owners(self) -> None:
        if get_sync_state().get("in_progress"):
            return
        try:
            users = self.repository.list_users(owner_only=True)
        except Exception:
            return
        for user in users:
            if self._stop_event.is_set() or get_sync_state().get("in_progress"):
                return
            user_id = int(user.get("id") or 0)
            if not user_id:
                continue
            try:
                settings = self.repository.get_wb_fbs_auto_sync_settings(user_id=user_id)
            except Exception:
                continue

            # 1) Auto-sync orders — same MSK active window as auto-collect MGT.
            if settings.get("enabled") and _msk_time_in_active_window(
                active_from=settings.get("active_from"),
                active_to=settings.get("active_to"),
            ):
                interval_minutes = int(settings.get("interval_minutes") or 60)
                if _fbs_auto_sync_is_due(
                    last_synced_at=settings.get("last_synced_at"),
                    interval_minutes=interval_minutes,
                ):
                    try:
                        jobs = list_fbs_sync_jobs(self.repository, user_id=user_id)
                    except Exception as exc:
                        _log.warning(
                            "WbFbsScheduler user %s list jobs error: %s", user_id, exc
                        )
                        jobs = []
                    if jobs:
                        ok, message = start_sync_thread(
                            repo=self.repository,
                            user_id=user_id,
                            sources=jobs,
                            is_auto=True,
                        )
                        if ok:
                            _log.info(
                                "WbFbsScheduler: started auto-sync user=%s sources=%s interval=%sm",
                                user_id,
                                len(jobs),
                                interval_minutes,
                            )
                        else:
                            _log.info(
                                "WbFbsScheduler: skip sync user=%s (%s)",
                                user_id,
                                message,
                            )

            # 2) Auto-collect MGT — independent schedule; wait if sync is running.
            if self._stop_event.is_set():
                return
            if get_sync_state().get("in_progress"):
                continue
            if not settings.get("collect_mgt_enabled"):
                continue
            try:
                result = run_auto_collect_mgt_for_owner(
                    self.repository, user_id=user_id
                )
                if result.get("ran"):
                    _log.info(
                        "WbFbsScheduler: auto-collect MGT user=%s %s",
                        user_id,
                        result.get("message"),
                    )
            except Exception as exc:
                _log.warning(
                    "WbFbsScheduler auto-collect MGT user=%s error: %s",
                    user_id,
                    exc,
                )


def _load_new_mgt_orders(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
) -> list[dict[str, Any]]:
    ensure_wb_fbs_tables(repo)
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                """
                SELECT order_id, cargo_type, supplier_status, wb_status, tab, is_b2b,
                       warehouse_id, raw_json, supply_id
                FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ?
                  AND tab = ?
                  AND cargo_type = 1
                  AND COALESCE(is_archive, FALSE) = FALSE
                ORDER BY order_id ASC
                """
            ),
            (user_id, source_id, TAB_NEW),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = repo._row_to_dict(row)
        if _is_cancelled_status(
            supplier_status=d.get("supplier_status"),
            wb_status=d.get("wb_status"),
        ):
            continue
        d["is_b2b"] = _row_order_is_b2b(d)
        d["cross_border_type"] = _row_cross_border(d)
        try:
            d["warehouse_id"] = (
                int(d["warehouse_id"]) if d.get("warehouse_id") is not None else None
            )
        except (TypeError, ValueError):
            d["warehouse_id"] = None
        out.append(d)
    return out


def _supply_is_empty(supply: dict[str, Any]) -> bool:
    """True only for unset cargo (no goods yet). Never treat SGT/KGT as empty."""
    cargo = int(supply.get("cargo_type") or 0)
    if cargo != 0:
        return False
    order_ids = supply.get("order_ids")
    if not isinstance(order_ids, list):
        order_ids = _parse_json_list(supply.get("order_ids_json"))
    return not order_ids


def _mgt_group_key(*, is_b2b: bool, warehouse_id: object, cross_border_type: object) -> str:
    wh = "na" if warehouse_id is None else str(int(warehouse_id))
    cb = "na" if cross_border_type is None else str(int(cross_border_type))
    return f"{'b2b' if is_b2b else 'non'}_wh{wh}_cb{cb}"


def _mgt_group_label(*, is_b2b: bool, warehouse_id: object, cross_border_type: object) -> str:
    parts = ["B2B" if is_b2b else "не B2B"]
    if warehouse_id is not None:
        parts.append(f"склад {warehouse_id}")
    if cross_border_type == 1:
        parts.append("кроссбордер")
    elif cross_border_type == 0:
        parts.append("не кроссбордер")
    return " · ".join(parts)


def _unique_supply_name(base: str, existing_names: set[str]) -> str:
    name = str(base or "").strip() or "Поставка"
    if name not in existing_names:
        return name
    for i in range(2, 100):
        candidate = f"{name} ({i})"
        if candidate not in existing_names:
            return candidate
    return f"{name} · {int(time.time())}"


def _supply_matches_mgt_traits(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    supply: dict[str, Any],
    is_b2b: bool,
    warehouse_id: object,
    cross_border_type: object,
) -> bool:
    """True if open supply can accept this MGT bucket (or is empty)."""
    if _supply_is_empty(supply):
        return True
    cargo = int(supply.get("cargo_type") or 0)
    if cargo != 1:
        return False
    raw = _parse_json_obj(supply.get("raw_json"))
    sb = _coalesce_b2b_flag(raw)
    if sb is None and supply.get("is_b2b") is not None:
        sb = bool(supply.get("is_b2b"))
    if sb is not None and bool(sb) != bool(is_b2b):
        return False
    supply_wh = _supply_warehouse_id(
        repo, user_id=user_id, source_id=source_id, supply=supply
    )
    if warehouse_id is not None and supply_wh is not None and int(warehouse_id) != int(supply_wh):
        return False
    supply_cb = _row_cross_border(supply)
    if (
        cross_border_type is not None
        and supply_cb is not None
        and int(cross_border_type) != int(supply_cb)
    ):
        return False
    return True


def _plan_mgt_group(
    *,
    is_b2b: bool,
    order_ids: list[int],
    mgt_matching: list[dict[str, Any]],
    empties: list[dict[str, Any]],
    existing_names: set[str],
    warehouse_id: object = None,
    cross_border_type: object = None,
) -> dict[str, Any]:
    """Plan one MGT bucket. Mutates ``empties`` when claiming an empty supply."""
    group_key = _mgt_group_key(
        is_b2b=is_b2b,
        warehouse_id=warehouse_id,
        cross_border_type=cross_border_type,
    )
    label = _mgt_group_label(
        is_b2b=is_b2b,
        warehouse_id=warehouse_id,
        cross_border_type=cross_border_type,
    )
    # Title is date (+ B2B) only — warehouse stays in group label, not in supply name.
    base_name = default_mgt_supply_name(is_b2b=is_b2b)
    suggested = _unique_supply_name(base_name, existing_names)
    candidates = list(mgt_matching) + list(empties)
    group: dict[str, Any] = {
        "group_key": group_key,
        "is_b2b": bool(is_b2b),
        "warehouse_id": warehouse_id,
        "cross_border_type": cross_border_type,
        "label": label,
        "order_ids": order_ids,
        "order_count": len(order_ids),
        "suggested_name": suggested,
        # suggested is always unique vs existing_names (_unique_supply_name).
        "name_conflict": False,
        "compatible_supplies": [
            {
                "supply_id": str(s.get("supply_id") or ""),
                "name": str(s.get("name") or s.get("supply_id") or ""),
                "cargo_type": int(s.get("cargo_type") or 0),
                "is_b2b": bool(s.get("is_b2b")),
                "is_empty": _supply_is_empty(s),
                "orders_count": len(s.get("order_ids") or _parse_json_list(s.get("order_ids_json"))),
            }
            for s in candidates
            if str(s.get("supply_id") or "").strip()
        ],
        "mode": "create",
        "default_supply_id": "",
    }
    if not order_ids:
        group["mode"] = "skip"
        return group
    if not candidates:
        group["mode"] = "create"
        existing_names.add(suggested)
        return group
    if len(candidates) == 1:
        chosen = candidates[0]
        sid = str(chosen.get("supply_id") or "")
        group["mode"] = "add_one"
        group["default_supply_id"] = sid
        if _supply_is_empty(chosen):
            empties[:] = [s for s in empties if str(s.get("supply_id") or "") != sid]
        return group
    group["mode"] = "choose"
    # Reserve empties offered in choose so another bucket cannot claim the same ones.
    claimed_empty_ids = {
        str(s.get("supply_id") or "")
        for s in empties
        if str(s.get("supply_id") or "").strip()
    }
    if claimed_empty_ids:
        empties[:] = [
            s for s in empties if str(s.get("supply_id") or "") not in claimed_empty_ids
        ]
    return group


def preview_collect_mgt(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
) -> dict[str, Any]:
    """Build collect plan for New-tab MGT orders of one FBS source.

    Splits by B2B × warehouse × crossBorder so WB will not 409-mix buckets.
    """
    ensure_wb_fbs_tables(repo)
    orders = _load_new_mgt_orders(repo, user_id=user_id, source_id=source_id)

    # Bucket orders — never mix warehouse/crossBorder in one supply.
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for o in orders:
        key = (
            bool(o.get("is_b2b")),
            o.get("warehouse_id"),
            o.get("cross_border_type"),
        )
        buckets.setdefault(key, []).append(o)

    open_supplies = list_supplies(repo, user_id=user_id, source_id=source_id, only_open=True)
    for s in open_supplies:
        s["order_ids"] = s.get("order_ids") or _parse_json_list(s.get("order_ids_json"))
        raw = _parse_json_obj(s.get("raw_json"))
        inferred = _coalesce_b2b_flag(raw)
        if inferred is not None:
            s["is_b2b"] = inferred

    empties: list[dict[str, Any]] = []
    mgt_supplies: list[dict[str, Any]] = []
    for s in open_supplies:
        if _supply_is_empty(s):
            empties.append(s)
            continue
        if int(s.get("cargo_type") or 0) == 1:
            mgt_supplies.append(s)

    # Real open-supply titles only (returned to FE for conflict checks).
    existing_names = {
        str(s.get("name") or "").strip()
        for s in open_supplies
        if str(s.get("name") or "").strip()
    }
    # Working set also reserves suggested titles across buckets so groups
    # don't collide — must NOT be leaked into existing_names (that caused a
    # false «поставка уже есть» on the suggested name itself).
    reserved_names = set(existing_names)

    # Non-B2B buckets first — they claim empty supplies before B2B.
    ordered_keys = sorted(buckets.keys(), key=lambda k: (bool(k[0]), str(k[1]), str(k[2])))
    groups: list[dict[str, Any]] = []
    for is_b2b, warehouse_id, cross_border_type in ordered_keys:
        bucket_orders = buckets[(is_b2b, warehouse_id, cross_border_type)]
        order_ids = [int(o["order_id"]) for o in bucket_orders]
        matching = [
            s
            for s in mgt_supplies
            if _supply_matches_mgt_traits(
                repo,
                user_id=user_id,
                source_id=source_id,
                supply=s,
                is_b2b=bool(is_b2b),
                warehouse_id=warehouse_id,
                cross_border_type=cross_border_type,
            )
        ]
        # Empties pool: only for this bucket's planning; non-B2B clears claimed ones.
        group = _plan_mgt_group(
            is_b2b=bool(is_b2b),
            order_ids=order_ids,
            mgt_matching=matching,
            empties=empties,
            existing_names=reserved_names,
            warehouse_id=warehouse_id,
            cross_border_type=cross_border_type,
        )
        if group.get("mode") != "skip":
            groups.append(group)

    needs_modal = any(g.get("mode") in ("create", "choose") for g in groups)
    return {
        "ok": True,
        "mgt_count": len(orders),
        "groups": groups,
        "needs_modal": needs_modal,
        "existing_names": sorted(existing_names),
    }


def execute_collect_mgt(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    api_key: str,
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Execute collect plan: create supplies / add orders. Always reports leftovers."""
    ensure_wb_fbs_tables(repo)
    client = WbFbsClient(api_key)
    preview = preview_collect_mgt(repo, user_id=user_id, source_id=source_id)
    planned_groups = list(preview.get("groups") or [])
    # Only real open-supply names (preview no longer includes suggested titles).
    existing_names = {
        str(x or "").strip()
        for x in (preview.get("existing_names") or [])
        if str(x or "").strip()
    }
    decisions_by_key = {
        str(d.get("group_key") or ""): d
        for d in decisions
        if isinstance(d, dict) and str(d.get("group_key") or "").strip()
    }
    # Back-compat: old FE sent only is_b2b without group_key.
    if not decisions_by_key:
        for d in decisions:
            if not isinstance(d, dict):
                continue
            flag = bool(d.get("is_b2b"))
            for g in planned_groups:
                if bool(g.get("is_b2b")) == flag and str(g.get("group_key") or "") not in decisions_by_key:
                    decisions_by_key[str(g.get("group_key") or "")] = d
                    break

    all_ids = [int(x) for g in planned_groups for x in (g.get("order_ids") or [])]
    status_map: dict[int, dict[str, Any]] = {}
    for i in range(0, len(all_ids), 1000):
        chunk = all_ids[i : i + 1000]
        try:
            for st in client.get_statuses(chunk):
                if isinstance(st, dict) and st.get("id") is not None:
                    status_map[int(st["id"])] = st
        except Exception as exc:
            return {
                "ok": False,
                "message": f"Не удалось проверить статусы заказов: {exc}",
                "errors": [str(exc)],
                "added": 0,
                "created_supplies": [],
                "skipped_cancelled": [],
                "not_added": all_ids,
                "remaining_in_new": all_ids,
                "goto_assembly": False,
            }
        if i + 1000 < len(all_ids):
            time.sleep(0.21)

    skipped_cancelled: list[int] = []
    not_added: list[int] = []
    errors: list[str] = []
    warnings: list[str] = []
    created_supplies: list[dict[str, Any]] = []
    added_total = 0
    added_ids: list[int] = []
    group_results: list[dict[str, Any]] = []
    planned_live_total = 0

    for planned in planned_groups:
        group_key = str(planned.get("group_key") or "")
        decision = decisions_by_key.get(group_key) or {}
        is_b2b = bool(planned.get("is_b2b"))
        label = str(planned.get("label") or ("B2B" if is_b2b else "не B2B"))
        raw_ids = [int(x) for x in (planned.get("order_ids") or [])]
        live_ids: list[int] = []
        for oid in raw_ids:
            st = status_map.get(oid) or {}
            ss = str(st.get("supplierStatus") or "").strip().lower()
            ws = str(st.get("wbStatus") or "").strip().lower()
            if _is_cancelled_status(supplier_status=ss, wb_status=ws):
                skipped_cancelled.append(oid)
                continue
            if ss and ss != "new":
                skipped_cancelled.append(oid)
                continue
            live_ids.append(oid)
        planned_live_total += len(live_ids)
        if not live_ids:
            group_results.append({
                "group_key": group_key,
                "is_b2b": is_b2b,
                "added": 0,
                "supply_id": "",
                "message": f"{label}: нет актуальных МГТ-заказов",
                "not_added": [],
            })
            continue

        mode = str(planned.get("mode") or "create")
        explicit = str(decision.get("action") or "").strip().lower()
        supply_id = str(decision.get("supply_id") or planned.get("default_supply_id") or "").strip()
        name = str(decision.get("name") or planned.get("suggested_name") or "").strip()

        if explicit == "create" or (not explicit and mode == "create"):
            action = "create"
        elif explicit in ("choose", "add") or mode in ("choose", "add_one"):
            action = "add"
            if mode == "choose" or explicit == "choose":
                if not supply_id:
                    errors.append(f"{label}: не выбрана поставка")
                    not_added.extend(live_ids)
                    group_results.append({
                        "group_key": group_key,
                        "is_b2b": is_b2b,
                        "added": 0,
                        "supply_id": "",
                        "message": f"{label}: не выбрана поставка",
                        "not_added": list(live_ids),
                    })
                    continue
            if mode == "add_one":
                supply_id = supply_id or str(planned.get("default_supply_id") or "")
        else:
            action = "create"

        if action == "create":
            if not name:
                errors.append(f"{label}: пустое название поставки")
                not_added.extend(live_ids)
                continue
            if name in existing_names:
                errors.append(
                    f"{label}: поставка «{name}» уже есть — измените название"
                )
                not_added.extend(live_ids)
                continue
            try:
                created = client.create_supply(name=name)
                supply_id = str(created.get("id") or "").strip()
                if not supply_id:
                    raise RuntimeError("WB не вернул id поставки")
                upsert_supply(
                    repo,
                    user_id=user_id,
                    source_id=source_id,
                    supply={
                        "id": supply_id,
                        "name": name,
                        "done": False,
                        "cargoType": 0,
                        "isB2b": is_b2b,
                    },
                    order_ids=[],
                )
                existing_names.add(name)
                created_supplies.append({
                    "supply_id": supply_id,
                    "name": name,
                    "is_b2b": is_b2b,
                    "group_key": group_key,
                })
            except Exception as exc:
                errors.append(f"{label}: не удалось создать поставку — {exc}")
                not_added.extend(live_ids)
                continue

        if not supply_id:
            errors.append(f"{label}: не указана поставка")
            not_added.extend(live_ids)
            continue

        try:
            live_supply = client.get_supply(supply_id)
            if bool(live_supply.get("done")):
                errors.append(f"{label}: поставка {supply_id} уже закрыта на WB")
                not_added.extend(live_ids)
                continue
            live_cargo = int(live_supply.get("cargoType") or 0)
            if live_cargo not in (0, 1):
                errors.append(
                    f"{label}: поставка {supply_id} не МГТ (cargoType={live_cargo})"
                )
                not_added.extend(live_ids)
                continue
            live_b2b = _coalesce_b2b_flag(live_supply)
            if live_cargo == 1 and live_b2b is not None and live_b2b != is_b2b:
                errors.append(f"{label}: поставка {supply_id} другого типа B2B")
                not_added.extend(live_ids)
                continue
            sel_wh = planned.get("warehouse_id")
            sel_cb = planned.get("cross_border_type")
            if live_cargo == 1 and sel_wh is not None:
                try:
                    live_oids = client.get_supply_order_ids(supply_id)
                except Exception:
                    live_oids = []
                wh_ids = live_oids or _local_supply_order_ids(
                    repo, user_id=user_id, source_id=source_id, supply_id=supply_id
                )
                supply_wh = _supply_warehouse_id(
                    repo,
                    user_id=user_id,
                    source_id=source_id,
                    supply={"order_ids": wh_ids},
                )
                if supply_wh is not None and int(supply_wh) != int(sel_wh):
                    errors.append(
                        f"{label}: склад поставки {supply_wh} ≠ склад заказов {sel_wh}"
                    )
                    not_added.extend(live_ids)
                    continue
            live_cb = live_supply.get("crossBorderType")
            if (
                live_cargo == 1
                and live_cb is not None
                and sel_cb is not None
                and int(live_cb) != int(sel_cb)
            ):
                errors.append(f"{label}: поставка {supply_id} другого crossBorderType")
                not_added.extend(live_ids)
                continue
        except Exception as exc:
            errors.append(f"{label}: проверка поставки {supply_id} — {exc}")
            not_added.extend(live_ids)
            continue

        group_added_ids: list[int] = []
        for i in range(0, len(live_ids), 100):
            chunk = live_ids[i : i + 100]
            try:
                client.add_orders_to_supply(supply_id, chunk)
                group_added_ids.extend(chunk)
                with repo._connect() as conn:
                    for oid in chunk:
                        conn.execute(
                            repo._sql(
                                """
                                UPDATE wb_fbs_orders
                                SET supply_id = ?, tab = ?, supplier_status = ?,
                                    is_b2b = ?, synced_at = ?
                                WHERE user_id = ? AND source_id = ? AND order_id = ?
                                """
                            ),
                            (
                                supply_id,
                                TAB_ASSEMBLY,
                                "confirm",
                                is_b2b,
                                _utc_now(),
                                user_id,
                                source_id,
                                oid,
                            ),
                        )
            except Exception as exc:
                errors.append(
                    f"{label}: не удалось добавить заказы {chunk[0]}… "
                    f"({len(chunk)} шт.) в {supply_id} — {exc}"
                )
                not_added.extend(chunk)
                # Remaining chunks of this group also stay out.
                rest = live_ids[i + 100 :]
                if rest:
                    not_added.extend(rest)
                    errors.append(
                        f"{label}: оставшиеся {len(rest)} заказ(ов) не отправлены "
                        "после ошибки чанка — они остались в «Новых»"
                    )
                break
            if i + 100 < len(live_ids):
                time.sleep(0.25)

        added = len(group_added_ids)
        if added:
            try:
                oids = client.get_supply_order_ids(supply_id)
                live_supply = client.get_supply(supply_id)
                if oids:
                    refresh_ids = oids
                else:
                    prev = _local_supply_order_ids(
                        repo,
                        user_id=user_id,
                        source_id=source_id,
                        supply_id=supply_id,
                    )
                    refresh_ids = sorted(set(prev) | set(group_added_ids))
                upsert_supply(
                    repo,
                    user_id=user_id,
                    source_id=source_id,
                    supply=live_supply or {"id": supply_id, "name": name, "isB2b": is_b2b},
                    order_ids=refresh_ids,
                )
            except Exception as exc:
                warnings.append(
                    f"{label}: заказы добавлены на WB, локальный кэш поставки — {exc}"
                )

        added_total += added
        added_ids.extend(group_added_ids)
        group_not_added = [oid for oid in live_ids if oid not in set(group_added_ids)]
        group_results.append({
            "group_key": group_key,
            "is_b2b": is_b2b,
            "added": added,
            "supply_id": supply_id,
            "message": f"{label}: добавлено {added} из {len(live_ids)}",
            "not_added": group_not_added,
        })

    # Deduplicate not_added while preserving order.
    seen_na: set[int] = set()
    not_added_uniq: list[int] = []
    for oid in not_added:
        if oid in seen_na:
            continue
        seen_na.add(oid)
        not_added_uniq.append(oid)
    not_added = not_added_uniq

    remaining_in_new = list(not_added)
    ok = (
        planned_live_total > 0
        and added_total == planned_live_total
        and not errors
        and not not_added
    )
    if ok:
        message = f"Готово: добавлено все {added_total} актуальных МГТ-заказов."
    elif added_total > 0 and (errors or not_added):
        message = (
            f"Частично: добавлено {added_total} из {planned_live_total}. "
            f"В «Новых» осталось {len(remaining_in_new)}."
        )
    elif errors:
        message = "Не удалось собрать МГТ-заказы."
    else:
        message = "Нечего добавлять."
    if skipped_cancelled:
        message += f" Пропущено (отмена/не new): {len(skipped_cancelled)}."
    if warnings:
        message += f" Предупреждений: {len(warnings)}."
    return {
        "ok": ok,
        "message": message,
        "errors": errors,
        "warnings": warnings,
        "added": added_total,
        "added_ids": added_ids,
        "planned_live": planned_live_total,
        "created_supplies": created_supplies,
        "skipped_cancelled": skipped_cancelled,
        "not_added": not_added,
        "remaining_in_new": remaining_in_new,
        "groups": group_results,
        "goto_assembly": bool(ok),
    }


def _row_cross_border(row: dict[str, Any]) -> int | None:
    """Order/supply ``crossBorderType``: 0 / 1 / None if unset."""
    raw = _parse_json_obj(row.get("raw_json"))
    if "crossBorderType" in raw and raw.get("crossBorderType") is not None:
        try:
            return int(raw.get("crossBorderType"))
        except (TypeError, ValueError):
            return None
    if "cross_border_type" in row and row.get("cross_border_type") is not None:
        try:
            return int(row.get("cross_border_type"))
        except (TypeError, ValueError):
            return None
    return None


def _load_orders_by_ids(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_ids: list[int],
) -> list[dict[str, Any]]:
    ensure_wb_fbs_tables(repo)
    ids = sorted({int(x) for x in order_ids if x is not None})
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    # Chunk IN-lists to stay under parameter limits.
    with repo._connect() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                repo._sql(
                    f"""
                    SELECT order_id, cargo_type, supplier_status, wb_status, tab, is_b2b,
                           warehouse_id, supply_id, raw_json, is_archive
                    FROM wb_fbs_orders
                    WHERE user_id = ? AND source_id = ?
                      AND order_id IN ({placeholders})
                    """
                ),
                tuple([user_id, source_id, *chunk]),
            ).fetchall()
            for row in rows:
                d = repo._row_to_dict(row)
                d["is_b2b"] = _row_order_is_b2b(d)
                d["cross_border_type"] = _row_cross_border(d)
                out.append(d)
    out.sort(key=lambda x: int(x.get("order_id") or 0))
    return out


def _selection_mix_errors(orders: list[dict[str, Any]]) -> list[str]:
    """WB-compatible homogeneity checks for a selected set of orders."""
    errors: list[str] = []
    if not orders:
        return ["Не выбраны заказы"]

    cargo_labels: dict[int, str] = {}
    for o in orders:
        try:
            ct = int(o.get("cargo_type") or 0)
        except (TypeError, ValueError):
            ct = 0
        label = cargo_type_label(ct) or ("неизвестно" if ct == 0 else str(ct))
        cargo_labels[ct] = label
    known_cargos = {c for c in cargo_labels if c in (1, 2, 3)}
    if len(known_cargos) > 1:
        pretty = ", ".join(cargo_labels[c] for c in sorted(known_cargos))
        errors.append(
            f"В выборе смешаны типы габарита: {pretty}. "
            "По API WB в одной поставке могут быть только заказы одного типа "
            "(МГТ / СГТ / КГТ+)."
        )
    elif 0 in cargo_labels and known_cargos:
        errors.append(
            "У части выбранных заказов не определён тип габарита (cargoType). "
            "Снимите их с выбора или дождитесь синхронизации."
        )
    elif 0 in cargo_labels and not known_cargos:
        errors.append(
            "Не удалось определить тип габарита (МГТ/СГТ/КГТ+) у выбранных заказов. "
            "Обновите синхронизацию и попробуйте снова."
        )

    b2b_flags = {_row_order_is_b2b(o) for o in orders}
    if len(b2b_flags) > 1:
        errors.append(
            "В выборе есть и B2B, и обычные заказы. "
            "С 19.03.2026 WB не позволяет смешивать их в одной поставке."
        )

    warehouses: set[object] = set()
    for o in orders:
        wh = o.get("warehouse_id")
        if wh is None or str(wh).strip() == "":
            warehouses.add(None)
            continue
        try:
            warehouses.add(int(wh))
        except (TypeError, ValueError):
            warehouses.add(None)
    if None in warehouses and len(warehouses) > 1:
        errors.append(
            "У части заказов не указан склад WB (warehouseId), а у других указан. "
            "В одну поставку можно добавить только заказы одного склада."
        )
    elif len(warehouses) > 1:
        errors.append(
            "Выбраны заказы с разных складов WB. "
            "По API WB заказы с разных warehouseId нельзя объединить в одну поставку."
        )

    borders = {_row_cross_border(o) for o in orders}
    known_borders = {b for b in borders if b is not None}
    if len(known_borders) > 1:
        errors.append(
            "В выборе смешаны кроссбордер и обычные заказы (crossBorderType). "
            "WB разрешает в одной поставке только один тип."
        )
    elif None in borders and known_borders:
        errors.append(
            "У части выбранных заказов не определён crossBorderType. "
            "Снимите их с выбора или дождитесь синхронизации."
        )
    return errors


def _selection_traits(orders: list[dict[str, Any]]) -> dict[str, Any]:
    cargo = 0
    for o in orders:
        try:
            ct = int(o.get("cargo_type") or 0)
        except (TypeError, ValueError):
            ct = 0
        if ct in (1, 2, 3):
            cargo = ct
            break
    is_b2b = _row_order_is_b2b(orders[0]) if orders else False
    warehouse_id = None
    if orders and orders[0].get("warehouse_id") is not None:
        try:
            warehouse_id = int(orders[0].get("warehouse_id"))
        except (TypeError, ValueError):
            warehouse_id = None
    cross_border = _row_cross_border(orders[0]) if orders else None
    return {
        "cargo_type": cargo,
        "cargo_label": cargo_type_label(cargo) or "",
        "is_b2b": bool(is_b2b),
        "warehouse_id": warehouse_id,
        "cross_border_type": cross_border,
    }


def _supply_warehouse_id(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    supply: dict[str, Any],
) -> int | None:
    order_ids = supply.get("order_ids")
    if not isinstance(order_ids, list):
        order_ids = _parse_json_list(supply.get("order_ids_json"))
    ids = [int(x) for x in order_ids if x is not None]
    if not ids:
        return None
    whs: set[int] = set()
    with repo._connect() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                repo._sql(
                    f"""
                    SELECT DISTINCT warehouse_id FROM wb_fbs_orders
                    WHERE user_id = ? AND source_id = ?
                      AND order_id IN ({placeholders})
                      AND warehouse_id IS NOT NULL
                    """
                ),
                tuple([user_id, source_id, *chunk]),
            ).fetchall()
            for row in rows:
                try:
                    whs.add(int(row["warehouse_id"]))
                except (TypeError, ValueError, KeyError):
                    continue
    if len(whs) == 1:
        return next(iter(whs))
    return None


def _compatible_supplies_for_traits(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    traits: dict[str, Any],
) -> list[dict[str, Any]]:
    cargo = int(traits.get("cargo_type") or 0)
    is_b2b = bool(traits.get("is_b2b"))
    warehouse_id = traits.get("warehouse_id")
    cross_border = traits.get("cross_border_type")
    open_supplies = list_supplies(repo, user_id=user_id, source_id=source_id, only_open=True)
    out: list[dict[str, Any]] = []
    for s in open_supplies:
        s["order_ids"] = s.get("order_ids") or _parse_json_list(s.get("order_ids_json"))
        raw = _parse_json_obj(s.get("raw_json"))
        inferred_b2b = _coalesce_b2b_flag(raw)
        if inferred_b2b is not None:
            s["is_b2b"] = inferred_b2b
        empty = _supply_is_empty(s)
        scargo = int(s.get("cargo_type") or 0)
        supply_wh = None
        if not empty:
            if scargo not in (0, cargo):
                continue
            sb = _coalesce_b2b_flag(raw)
            if sb is None and s.get("is_b2b") is not None:
                sb = bool(s.get("is_b2b"))
            # Non-empty supply must match B2B even if local cargo_type is still 0.
            if sb is not None and sb != is_b2b:
                continue
            supply_wh = _supply_warehouse_id(
                repo, user_id=user_id, source_id=source_id, supply=s
            )
            if (
                warehouse_id is not None
                and supply_wh is not None
                and int(warehouse_id) != int(supply_wh)
            ):
                continue
            supply_border = _row_cross_border(s)
            if (
                cross_border is not None
                and supply_border is not None
                and int(cross_border) != int(supply_border)
            ):
                continue
        out.append(
            {
                "supply_id": str(s.get("supply_id") or ""),
                "name": str(s.get("name") or s.get("supply_id") or ""),
                "cargo_type": scargo,
                "cargo_label": cargo_type_label(scargo) or ("пустая" if empty else ""),
                "is_b2b": bool(s.get("is_b2b")) if not empty else None,
                "is_empty": empty,
                "orders_count": len(s.get("order_ids") or []),
                "warehouse_id": supply_wh,
            }
        )
    out = [x for x in out if x.get("supply_id")]
    out.sort(key=lambda x: (not x.get("is_empty"), str(x.get("name") or "").lower()))
    return out


def preview_selection_supply(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    order_ids: list[int],
) -> dict[str, Any]:
    """Validate selected New-tab orders and list compatible open supplies."""
    ensure_wb_fbs_tables(repo)
    wanted = sorted({int(x) for x in order_ids if x is not None})
    orders = _load_orders_by_ids(
        repo, user_id=user_id, source_id=source_id, order_ids=wanted
    )
    found = {int(o["order_id"]) for o in orders}
    missing = [oid for oid in wanted if oid not in found]
    errors: list[str] = []
    if missing:
        errors.append(
            f"Не найдены заказы в текущем кабинете: {', '.join(map(str, missing[:10]))}"
            + ("…" if len(missing) > 10 else "")
        )

    usable: list[dict[str, Any]] = []
    for o in orders:
        if bool(o.get("is_archive")):
            errors.append(f"Заказ {o.get('order_id')} в архиве")
            continue
        if str(o.get("tab") or "") != TAB_NEW:
            errors.append(
                f"Заказ {o.get('order_id')} не во вкладке «Новые» "
                f"(сейчас: {o.get('tab') or '—'})"
            )
            continue
        if _is_cancelled_status(
            supplier_status=o.get("supplier_status"),
            wb_status=o.get("wb_status"),
        ):
            errors.append(f"Заказ {o.get('order_id')} отменён")
            continue
        usable.append(o)

    mix_errors = _selection_mix_errors(usable) if usable else (["Нет подходящих заказов"] if not errors else [])
    errors.extend(mix_errors)
    # Deduplicate while preserving order
    seen: set[str] = set()
    uniq_errors: list[str] = []
    for e in errors:
        if e in seen:
            continue
        seen.add(e)
        uniq_errors.append(e)

    traits = _selection_traits(usable) if usable and not mix_errors else {
        "cargo_type": 0,
        "cargo_label": "",
        "is_b2b": False,
        "warehouse_id": None,
        "cross_border_type": None,
    }
    open_supplies = list_supplies(
        repo, user_id=user_id, source_id=source_id, only_open=True
    )
    existing_names = sorted(
        {
            str(s.get("name") or "").strip()
            for s in open_supplies
            if str(s.get("name") or "").strip()
        }
    )
    compatible: list[dict[str, Any]] = []
    if usable and not mix_errors:
        compatible = _compatible_supplies_for_traits(
            repo, user_id=user_id, source_id=source_id, traits=traits
        )

    suggested = default_mgt_supply_name(is_b2b=bool(traits.get("is_b2b")))
    return {
        "ok": not uniq_errors,
        "errors": uniq_errors,
        "order_ids": [int(o["order_id"]) for o in usable],
        "order_count": len(usable),
        "traits": traits,
        "suggested_name": suggested,
        "name_conflict": suggested in set(existing_names),
        "existing_names": existing_names,
        "compatible_supplies": compatible,
        "has_open_supplies": bool(open_supplies),
    }


def _filter_live_new_order_ids(
    client: WbFbsClient,
    order_ids: list[int],
) -> tuple[list[int], list[int], list[str]]:
    """Refresh statuses; return (live_new_ids, skipped, fatal_errors)."""
    skipped: list[int] = []
    errors: list[str] = []
    status_map: dict[int, dict[str, Any]] = {}
    ids = [int(x) for x in order_ids]
    for i in range(0, len(ids), 1000):
        chunk = ids[i : i + 1000]
        try:
            for st in client.get_statuses(chunk):
                if isinstance(st, dict) and st.get("id") is not None:
                    status_map[int(st["id"])] = st
        except Exception as exc:
            return [], [], [f"Не удалось проверить статусы заказов: {exc}"]
        if i + 1000 < len(ids):
            time.sleep(0.21)
    live: list[int] = []
    for oid in ids:
        st = status_map.get(oid) or {}
        ss = str(st.get("supplierStatus") or "").strip().lower()
        ws = str(st.get("wbStatus") or "").strip().lower()
        if _is_cancelled_status(supplier_status=ss, wb_status=ws):
            skipped.append(oid)
            continue
        if ss and ss != "new":
            skipped.append(oid)
            continue
        live.append(oid)
    return live, skipped, errors


def _local_supply_order_ids(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    supply_id: str,
) -> list[int]:
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql(
                """
                SELECT order_ids_json FROM wb_fbs_supplies
                WHERE user_id = ? AND source_id = ? AND supply_id = ?
                """
            ),
            (user_id, source_id, str(supply_id)),
        ).fetchone()
    if not row:
        return []
    return [int(x) for x in _parse_json_list(row["order_ids_json"]) if x is not None]


def _add_orders_to_supply_local(
    repo: ReviewRepository,
    client: WbFbsClient,
    *,
    user_id: int,
    source_id: int,
    supply_id: str,
    order_ids: list[int],
    is_b2b: bool,
    name: str = "",
) -> tuple[int, list[str], list[str]]:
    """PATCH orders to WB supply in chunks; update local cache. Returns added, errors, warnings."""
    errors: list[str] = []
    warnings: list[str] = []
    added = 0
    prev_ids = _local_supply_order_ids(
        repo, user_id=user_id, source_id=source_id, supply_id=supply_id
    )
    for i in range(0, len(order_ids), 100):
        chunk = order_ids[i : i + 100]
        try:
            client.add_orders_to_supply(supply_id, chunk)
            added += len(chunk)
            with repo._connect() as conn:
                for oid in chunk:
                    conn.execute(
                        repo._sql(
                            """
                            UPDATE wb_fbs_orders
                            SET supply_id = ?, tab = ?, supplier_status = ?,
                                is_b2b = ?, synced_at = ?
                            WHERE user_id = ? AND source_id = ? AND order_id = ?
                            """
                        ),
                        (
                            supply_id,
                            TAB_ASSEMBLY,
                            "confirm",
                            is_b2b,
                            _utc_now(),
                            user_id,
                            source_id,
                            oid,
                        ),
                    )
        except Exception as exc:
            errors.append(
                f"Не удалось добавить заказы {chunk[0]}… ({len(chunk)} шт.) "
                f"в {supply_id}: {exc}"
            )
        if i + 100 < len(order_ids):
            time.sleep(0.25)
    if added:
        try:
            oids = client.get_supply_order_ids(supply_id)
            live_supply = client.get_supply(supply_id)
            # Never replace an existing supply with only the newly added chunk:
            # empty/failed order-ids response would wipe previous members locally.
            if oids:
                refresh_ids = oids
            else:
                refresh_ids = sorted(set(prev_ids) | set(order_ids[:added]))
            upsert_supply(
                repo,
                user_id=user_id,
                source_id=source_id,
                supply=live_supply
                or {"id": supply_id, "name": name, "isB2b": is_b2b},
                order_ids=refresh_ids,
            )
        except Exception as exc:
            warnings.append(f"Заказы добавлены на WB, локальный кэш поставки: {exc}")
    return added, errors, warnings


def create_supply_from_selection(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    api_key: str,
    order_ids: list[int],
    name: str,
) -> dict[str, Any]:
    """Create a new WB supply and add the selected New-tab orders."""
    preview = preview_selection_supply(
        repo, user_id=user_id, source_id=source_id, order_ids=order_ids
    )
    if not preview.get("ok"):
        return {
            "ok": False,
            "message": "Нельзя создать поставку из выбранных заказов.",
            "errors": preview.get("errors") or [],
            "added": 0,
            "goto_assembly": False,
        }
    title = str(name or preview.get("suggested_name") or "").strip()
    if not title:
        return {
            "ok": False,
            "message": "Укажите название поставки.",
            "errors": ["Пустое название поставки"],
            "added": 0,
            "goto_assembly": False,
        }
    existing = set(preview.get("existing_names") or [])
    if title in existing:
        return {
            "ok": False,
            "message": "Поставка с таким названием уже есть.",
            "errors": [f"Поставка «{title}» уже есть — измените название"],
            "added": 0,
            "goto_assembly": False,
        }

    client = WbFbsClient(api_key)
    live_ids, skipped, fatal = _filter_live_new_order_ids(
        client, list(preview.get("order_ids") or [])
    )
    if fatal:
        return {
            "ok": False,
            "message": fatal[0],
            "errors": fatal,
            "added": 0,
            "skipped_cancelled": skipped,
            "goto_assembly": False,
        }
    if not live_ids:
        return {
            "ok": False,
            "message": "Нет актуальных заказов со статусом «new».",
            "errors": ["Все выбранные заказы уже не в «Новых» или отменены"],
            "added": 0,
            "skipped_cancelled": skipped,
            "goto_assembly": False,
        }

    traits = preview.get("traits") or {}
    is_b2b = bool(traits.get("is_b2b"))
    try:
        created = client.create_supply(name=title)
        supply_id = str(created.get("id") or "").strip()
        if not supply_id:
            raise RuntimeError("WB не вернул id поставки")
        upsert_supply(
            repo,
            user_id=user_id,
            source_id=source_id,
            supply={
                "id": supply_id,
                "name": title,
                "done": False,
                "cargoType": 0,
                "isB2b": is_b2b,
            },
            order_ids=[],
        )
    except Exception as exc:
        return {
            "ok": False,
            "message": "Не удалось создать поставку на WB.",
            "errors": [str(exc)],
            "added": 0,
            "skipped_cancelled": skipped,
            "goto_assembly": False,
        }

    added, errors, warnings = _add_orders_to_supply_local(
        repo,
        client,
        user_id=user_id,
        source_id=source_id,
        supply_id=supply_id,
        order_ids=live_ids,
        is_b2b=is_b2b,
        name=title,
    )
    ok = added > 0 and not errors
    if ok:
        message = (
            f"Готово: создана поставка «{title}», добавлено {added} из {len(live_ids)}."
        )
    elif added:
        message = f"Частично: добавлено {added}. Есть ошибки."
    else:
        message = (
            f"Поставка «{title}» ({supply_id}) создана, но заказы не добавились."
        )
    if skipped:
        message += f" Пропущено: {len(skipped)}."
    return {
        "ok": ok,
        "message": message,
        "errors": errors,
        "warnings": warnings,
        "added": added,
        "supply_id": supply_id,
        "supply_name": title,
        "skipped_cancelled": skipped,
        "goto_assembly": bool(added > 0 and not errors),
    }


def add_selection_to_supply(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    api_key: str,
    order_ids: list[int],
    supply_id: str,
) -> dict[str, Any]:
    """Add selected New-tab orders to an existing open WB supply."""
    sid = str(supply_id or "").strip()
    preview = preview_selection_supply(
        repo, user_id=user_id, source_id=source_id, order_ids=order_ids
    )
    if not preview.get("ok"):
        return {
            "ok": False,
            "message": "Нельзя добавить выбранные заказы в поставку.",
            "errors": preview.get("errors") or [],
            "added": 0,
            "goto_assembly": False,
        }
    if not sid:
        return {
            "ok": False,
            "message": "Не выбрана поставка.",
            "errors": ["Укажите supply_id"],
            "added": 0,
            "goto_assembly": False,
        }
    compatible_ids = {
        str(s.get("supply_id") or "")
        for s in (preview.get("compatible_supplies") or [])
    }
    if sid not in compatible_ids:
        return {
            "ok": False,
            "message": "Выбранная поставка не совместима с заказами.",
            "errors": [
                "Поставка другого типа габарита/B2B/склада или уже закрыта. "
                "Выберите совместимую поставку из списка."
            ],
            "added": 0,
            "goto_assembly": False,
        }

    client = WbFbsClient(api_key)
    traits = preview.get("traits") or {}
    is_b2b = bool(traits.get("is_b2b"))
    cargo = int(traits.get("cargo_type") or 0)
    try:
        live_supply = client.get_supply(sid)
        if bool(live_supply.get("done")):
            return {
                "ok": False,
                "message": "Поставка уже закрыта на WB.",
                "errors": [f"Поставка {sid} закрыта (done=true)"],
                "added": 0,
                "goto_assembly": False,
            }
        live_cargo = int(live_supply.get("cargoType") or 0)
        if live_cargo not in (0, cargo):
            return {
                "ok": False,
                "message": "Тип габарита поставки не совпадает.",
                "errors": [
                    f"Поставка {sid}: cargoType={live_cargo}, "
                    f"у заказов {cargo_type_label(cargo) or cargo}."
                ],
                "added": 0,
                "goto_assembly": False,
            }
        live_b2b = _coalesce_b2b_flag(live_supply)
        if live_cargo != 0 and live_b2b is not None and live_b2b != is_b2b:
            return {
                "ok": False,
                "message": "Тип B2B поставки не совпадает.",
                "errors": [f"Поставка {sid} другого типа B2B"],
                "added": 0,
                "goto_assembly": False,
            }
        live_border = live_supply.get("crossBorderType")
        sel_border = traits.get("cross_border_type")
        if (
            live_cargo != 0
            and live_border is not None
            and sel_border is not None
            and int(live_border) != int(sel_border)
        ):
            return {
                "ok": False,
                "message": "Тип cross-border поставки не совпадает.",
                "errors": [f"Поставка {sid}: другой crossBorderType"],
                "added": 0,
                "goto_assembly": False,
            }
        sel_wh = traits.get("warehouse_id")
        if live_cargo != 0 and sel_wh is not None:
            try:
                live_oids = client.get_supply_order_ids(sid)
            except Exception:
                live_oids = []
            wh_ids = live_oids or _local_supply_order_ids(
                repo, user_id=user_id, source_id=source_id, supply_id=sid
            )
            supply_wh = _supply_warehouse_id(
                repo,
                user_id=user_id,
                source_id=source_id,
                supply={"order_ids": wh_ids},
            )
            if supply_wh is not None and int(supply_wh) != int(sel_wh):
                return {
                    "ok": False,
                    "message": "Склад поставки не совпадает с выбранными заказами.",
                    "errors": [
                        f"Поставка {sid}: склад {supply_wh}, "
                        f"у заказов склад {sel_wh}."
                    ],
                    "added": 0,
                    "goto_assembly": False,
                }
    except Exception as exc:
        return {
            "ok": False,
            "message": "Не удалось проверить поставку на WB.",
            "errors": [str(exc)],
            "added": 0,
            "goto_assembly": False,
        }

    live_ids, skipped, fatal = _filter_live_new_order_ids(
        client, list(preview.get("order_ids") or [])
    )
    if fatal:
        return {
            "ok": False,
            "message": fatal[0],
            "errors": fatal,
            "added": 0,
            "skipped_cancelled": skipped,
            "goto_assembly": False,
        }
    if not live_ids:
        return {
            "ok": False,
            "message": "Нет актуальных заказов со статусом «new».",
            "errors": ["Все выбранные заказы уже не в «Новых» или отменены"],
            "added": 0,
            "skipped_cancelled": skipped,
            "goto_assembly": False,
        }

    supply_name = str(live_supply.get("name") or sid)
    added, errors, warnings = _add_orders_to_supply_local(
        repo,
        client,
        user_id=user_id,
        source_id=source_id,
        supply_id=sid,
        order_ids=live_ids,
        is_b2b=is_b2b,
        name=supply_name,
    )
    ok = added > 0 and not errors
    message = (
        f"Готово: в «{supply_name}» добавлено {added} из {len(live_ids)}."
        if ok
        else (
            f"Частично: добавлено {added}. Есть ошибки."
            if added
            else "Не удалось добавить заказы в поставку."
        )
    )
    if skipped:
        message += f" Пропущено: {len(skipped)}."
    return {
        "ok": ok,
        "message": message,
        "errors": errors,
        "warnings": warnings,
        "added": added,
        "supply_id": sid,
        "supply_name": supply_name,
        "skipped_cancelled": skipped,
        "goto_assembly": bool(added > 0 and not errors),
    }
