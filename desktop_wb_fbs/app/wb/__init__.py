# -*- coding: utf-8 -*-
"""Shared helpers ported for desktop (no Postgres / no web)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8
    from backports.zoneinfo import ZoneInfo  # type: ignore

MSK = ZoneInfo("Europe/Moscow")

TAB_NEW = "new"
TAB_ASSEMBLY = "assembly"
TAB_DELIVERY = "delivery"
TAB_FINISHED = "finished"
TAB_CANCELLED = "cancelled"
TAB_ARCHIVE = "archive"

_FINISHED_WB = {"sold"}
_CANCELLED_SUPPLIER = {"cancel", "cancel_carrier"}
_CANCELLED_WB = {
    "canceled",
    "canceled_by_client",
    "declined_by_client",
    "defect",
    "canceled_by_carrier",
}

SCOPE_ERROR_MESSAGE = "Нет ни одного источника с нужным API (Marketplace)."


def normalize_api_key(value: object) -> str:
    """Strip wrappers users often paste from docs/UI (Bearer, quotes, spaces)."""
    key = str(value or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1].strip()
    return key


def _wb_error_detail(exc: object) -> str:
    text = str(exc or "")
    start = text.find("{")
    if start < 0:
        return ""
    try:
        data = json.loads(text[start:])
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    detail = data.get("detail") or data.get("title") or data.get("message")
    return str(detail or "").strip()[:220]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    return TAB_ASSEMBLY if ss else TAB_NEW


def is_cancelled_status(*, supplier_status: object = "", wb_status: object = "") -> bool:
    ss = str(supplier_status or "").strip().lower()
    ws = str(wb_status or "").strip().lower()
    return ss in _CANCELLED_SUPPLIER or ws in _CANCELLED_WB


def cancel_reason_label(*, supplier_status: object = "", wb_status: object = "") -> str:
    ws = str(wb_status or "").strip().lower()
    ss = str(supplier_status or "").strip().lower()
    if ws == "declined_by_client":
        return "Покупатель в первый час"
    if ws == "canceled_by_client":
        return "Отказ на ПВЗ"
    if ws == "defect":
        return "Найдены дефекты"
    if ws == "canceled_by_carrier" or ss == "cancel_carrier":
        return "Перевозчик"
    if ws == "canceled" or ss == "cancel":
        return "Отмена продавцом"
    return ""


def as_int_or_none(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def resolve_order_price(order: Dict[str, Any]) -> Tuple[int, int]:
    converted_final = as_int_or_none(order.get("convertedFinalPrice"))
    converted = as_int_or_none(order.get("convertedPrice"))
    final = as_int_or_none(order.get("finalPrice"))
    price = as_int_or_none(order.get("price"))
    converted_ccy = as_int_or_none(order.get("convertedCurrencyCode")) or 643
    sale_ccy = as_int_or_none(order.get("currencyCode")) or 643

    if converted_final is not None and converted_final > 0:
        return converted_final, converted_ccy
    if converted is not None and converted > 0:
        return converted, converted_ccy
    if final is not None and final > 0:
        return final, sale_ccy
    if price is not None and price > 0:
        return price, sale_ccy
    if converted_final is not None:
        return converted_final, converted_ccy
    if converted is not None:
        return converted, converted_ccy
    if final is not None:
        return final, sale_ccy
    return int(price or 0), sale_ccy


def format_price_rub(price_kopecks: object, currency_code: object = 643) -> str:
    try:
        kopecks = int(price_kopecks or 0)
    except (TypeError, ValueError):
        kopecks = 0
    rub = kopecks / 100.0
    if int(currency_code or 643) == 643:
        if rub == int(rub):
            return "{} ₽".format(int(rub))
        return "{:.2f} ₽".format(rub).replace(".", ",")
    return "{:.2f}".format(rub)


def cargo_type_label(cargo_type: object) -> str:
    try:
        ct = int(cargo_type or 0)
    except (TypeError, ValueError):
        return ""
    return {1: "МГТ", 2: "СГТ", 3: "КГТ+"}.get(ct, "")


def coalesce_b2b_flag(obj: Optional[Dict[str, Any]]) -> Optional[bool]:
    if not isinstance(obj, dict):
        return None
    for key in ("isB2b", "isB2B", "is_b2b"):
        if key in obj and obj.get(key) is not None:
            return bool(obj.get(key))
    return None


def order_b2b_flag(order: Optional[Dict[str, Any]]) -> Optional[bool]:
    if not isinstance(order, dict):
        return None
    opts = order.get("options")
    if isinstance(opts, dict):
        flag = coalesce_b2b_flag(opts)
        if flag is not None:
            return flag
    return coalesce_b2b_flag(order)


def is_fbs_source_name(name: object) -> bool:
    text = str(name or "").casefold()
    return "фбс" in text or "fbs" in text


def is_marketplace_scope_error(exc: object) -> bool:
    text = str(exc or "").lower()
    detail = _wb_error_detail(exc).lower()
    if "token scope not allowed" in text or "token scope not allowed" in detail:
        return True
    if "category" in detail and "token" in detail:
        return True
    if "http 403" in text and ("scope" in text or "access denied" in detail):
        return True
    return False


def friendly_sync_error(prefix: str, exc: object) -> str:
    if is_marketplace_scope_error(exc):
        return SCOPE_ERROR_MESSAGE
    text = str(exc or "")
    lower = text.lower()
    detail = _wb_error_detail(exc)
    if (
        "certificate verify failed" in lower
        or "ssl при подключении" in lower
        or "ssl: " in lower
        or "sslc" in lower
    ):
        return (
            "{}: ошибка SSL при подключении к WB API. "
            "Установите сертификаты: pip install -U certifi "
            "(или отключите HTTPS-проверку в антивирусе/прокси)."
        ).format(prefix)
    if "timed out" in lower or "timeout" in lower:
        return "{}: таймаут подключения к WB API".format(prefix)
    if (
        "connection refused" in lower
        or "network is unreachable" in lower
        or "getaddrinfo failed" in lower
        or "name or service not known" in lower
    ):
        return "{}: нет связи с marketplace-api.wildberries.ru".format(prefix)
    if "incorrectparameter" in lower or "incorrect parameter" in lower:
        return "{}: некорректные параметры запроса к WB".format(prefix)
    if "http 429" in lower:
        return "{}: превышен лимит запросов WB, попробуйте позже".format(prefix)
    if "http 401" in lower or "http 403" in lower:
        if detail:
            return "{}: ключ API отклонён — {}".format(prefix, detail)
        return "{}: неверный или просроченный ключ API (категория Marketplace)".format(
            prefix
        )
    if "http 5" in lower:
        return "{}: временная ошибка WB API".format(prefix)
    m = re.search(r"HTTP\s+(\d+)", text, flags=re.IGNORECASE)
    if m:
        code = m.group(1)
        if detail:
            return "{}: ошибка WB API (HTTP {}) — {}".format(prefix, code, detail)
        return "{}: ошибка WB API (HTTP {})".format(prefix, code)
    if detail:
        return "{}: {}".format(prefix, detail)
    if text.strip():
        return "{}: {}".format(prefix, text.strip()[:220])
    return "{}: не удалось загрузить данные".format(prefix)


def kiz_code_clean(value: object) -> str:
    """Trim space/CR/LF only — never strip GS (\\u001D)."""
    return str(value or "").strip(" \t\r\n")


def parse_json_list(raw: object) -> List[Any]:
    if isinstance(raw, list):
        return list(raw)
    try:
        data = json.loads(str(raw or "[]"))
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def parse_json_obj(raw: object) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(str(raw or "{}"))
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_dt(value: object) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    text = str(value).strip()
    return text or None


def default_mgt_supply_name(*, is_b2b: bool, when: Optional[datetime] = None) -> str:
    dt = when or datetime.now(MSK)
    name = "Поставка от {:02d}.{:02d}.{}".format(dt.day, dt.month, dt.year)
    if is_b2b:
        name += " B2B"
    return name


def supply_status_label(*, done: object = False, scan_dt: object = None) -> str:
    if scan_dt:
        return "На приёмке"
    if done:
        return "Отгрузите поставку"
    return "На сборке"


def lookback_window(days: int) -> Tuple[datetime, datetime]:
    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(days=max(1, min(int(days), 30)))
    return date_from, date_to
