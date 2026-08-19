"""Ozon FBO eTrN (эТрН) draft XML for Kontur.Logistics upload.

Builds formal title-1 XML (КНД 1110339, ON_TRNACLGROT, ВерсФорм 5.01)
from FeedPilot supply / legal-entity / driver / cargo data. Intended as a
draft the user uploads into Kontur.Logistics and completes/signs there.

Schema references:
- FNS order ЕД-7-26/1065@ (format 5.01)
- Diadoc GenerateTitleXml sample (АдресРФ vs АдрРФ, Подписант)
- Kontur.Logistics InfPol keys (ORDERS / Значение)
"""
from __future__ import annotations

import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from xml.dom import minidom

_log = logging.getLogger(__name__)

OZON_CONSIGNEE_NAME = 'ООО "ИНТЕРНЕТ РЕШЕНИЯ"'
OZON_CONSIGNEE_INN = "7704217370"
OZON_CONSIGNEE_KPP = "997750001"
OZON_CONSIGNEE_EDO_GUID = "2BM-7704217370-774301001-201407110916237240124"
# Legal address of Ozon (ООО «ИНТЕРНЕТ РЕШЕНИЯ») — always emit as АдрРФ (Russian).
OZON_CONSIGNEE_ADDRESS = "123112, г. Москва, Пресненская наб., д. 10"
# Structured fields — do not rely on free-text parse for the constant consignee.
OZON_CONSIGNEE_ADDR_FIELDS: dict[str, str] = {
    "Индекс": "123112",
    "КодРегион": "77",
    "Город": "Москва",
    "Улица": "Пресненская наб.",
    "Дом": "10",
    "raw": OZON_CONSIGNEE_ADDRESS,
}

_CARGO_WEIGHT_TONS = {"PALLET": 0.2, "BOX": 0.0125}

# Subject codes (ССРФ/КЛАДР). More specific patterns MUST come first
# (e.g. «Московская обл» before bare «Москва»).
_REGION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"московск(?:ая)?\s*(?:обл\.?|область)", "50"),
    (r"ленинградск(?:ая)?\s*(?:обл\.?|область)", "47"),
    (r"нижегородск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*нижний\s+новгород", "52"),
    (r"свердловск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*екатеринбург", "66"),
    (r"новосибирск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*новосибирск", "54"),
    (r"ростовск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*ростов(?:\s*-\s*на\s*-\s*дону)?", "61"),
    (r"самарск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*самара", "63"),
    (r"челябинск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*челябинск", "74"),
    (r"омск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*омск(?![а-яё])", "55"),
    (r"воронежск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*воронеж", "36"),
    (r"волгоградск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*волгоград", "34"),
    (r"саратовск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*саратов", "64"),
    (r"тюменск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*тюмень", "72"),
    (r"тульск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*тула(?![а-яё])", "71"),
    (r"рязанск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*рязань", "62"),
    (r"калининградск(?:ая)?\s*(?:обл\.?|область)|г\.?\s*калининград", "39"),
    (r"краснодарск(?:ий)?\s*край|г\.?\s*краснодар", "23"),
    (r"красноярск(?:ий)?\s*край|г\.?\s*красноярск", "24"),
    (r"пермск(?:ий)?\s*край|г\.?\s*пермь", "59"),
    (r"респ(?:ублика)?\s*татарстан|г\.?\s*казань", "16"),
    (r"респ(?:ублика)?\s*башкортостан|г\.?\s*уфа(?![а-яё])", "02"),
    (r"санкт[-\s]?петербург|г\.?\s*спб(?![а-яё])|(?<![а-яё])петербург(?![а-яё])", "78"),
    (r"(?<![а-яё])(?:г\.?\s*)?москва(?![а-яё])", "77"),
)

# First 3 digits of postal index → subject code. Index[:2] is NOT OKATO.
_POSTAL3_RANGES: tuple[tuple[int, int, str], ...] = (
    (101, 135, "77"),  # Москва
    (140, 144, "50"),  # Московская область
    (150, 153, "76"),  # Ярославская
    (160, 162, "35"),  # Вологодская
    (163, 164, "29"),  # Архангельская
    (170, 172, "69"),  # Тверская
    (180, 182, "60"),  # Псковская
    (183, 184, "51"),  # Мурманская
    (185, 186, "10"),  # Карелия
    (187, 188, "47"),  # Ленинградская
    (190, 199, "78"),  # Санкт-Петербург
    (214, 216, "67"),  # Смоленская
    (241, 243, "32"),  # Брянская
    (300, 301, "71"),  # Тульская
    (302, 303, "62"),  # Рязанская
    (305, 307, "46"),  # Курская
    (308, 309, "31"),  # Белгородская
    (344, 347, "61"),  # Ростовская
    (350, 354, "23"),  # Краснодарский
    (390, 392, "62"),  # Рязань overlap handled above; keep local capitals
    (394, 396, "36"),  # Воронежская
    (400, 404, "34"),  # Волгоградская
    (410, 413, "64"),  # Саратовская
    (420, 423, "16"),  # Татарстан / Казань
    (426, 427, "18"),  # Удмуртия
    (440, 443, "63"),  # Самарская
    (450, 453, "02"),  # Башкортостан / Уфа
    (454, 456, "74"),  # Челябинская
    (460, 462, "56"),  # Оренбургская
    (603, 607, "52"),  # Нижегородская
    (614, 619, "59"),  # Пермский
    (620, 624, "66"),  # Свердловская
    (625, 627, "72"),  # Тюменская
    (630, 633, "54"),  # Новосибирская
    (640, 644, "55"),  # Омская
    (650, 654, "42"),  # Кемеровская
    (660, 663, "24"),  # Красноярский
    (690, 692, "25"),  # Приморский
)


def _parse_inn_kpp(text: str) -> tuple[str, str]:
    raw = str(text or "")
    inn_m = re.search(r"ИНН\s*[:=]?\s*(\d{10}|\d{12})", raw, flags=re.I)
    kpp_m = re.search(r"КПП\s*[:=]?\s*(\d{9})", raw, flags=re.I)
    inn = inn_m.group(1) if inn_m else ""
    kpp = kpp_m.group(1) if kpp_m else ""
    if not inn:
        bare = re.search(r"\b(\d{10}|\d{12})\b", raw)
        if bare:
            inn = bare.group(1)
    if not kpp and inn:
        bare_kpp = re.search(r"\b(\d{9})\b", raw)
        if bare_kpp and bare_kpp.group(1) != inn[:9]:
            kpp = bare_kpp.group(1)
    return inn, kpp


def _parse_carrier(text: str) -> tuple[str, str, str]:
    """Return (name, inn, kpp) from free-text carrier field."""
    raw = str(text or "").strip()
    if not raw:
        return "", "", ""
    inn, kpp = _parse_inn_kpp(raw)
    name = raw
    name = re.sub(r"ИНН\s*[:=]?\s*\d{10,12}", "", name, flags=re.I)
    name = re.sub(r"КПП\s*[:=]?\s*\d{9}", "", name, flags=re.I)
    name = re.sub(r"[,/|;]+", " ", name).strip(" -")
    name = re.sub(r"\s{2,}", " ", name)
    return name, inn, kpp


def _split_fio(full_name: str) -> tuple[str, str, str]:
    parts = [p for p in str(full_name or "").strip().split() if p]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], parts[1], " ".join(parts[2:])


def _driver_fio_from_fields(
    fields: dict[str, Any] | None,
    fallback_name: str = "",
) -> tuple[str, str, str]:
    """Prefer structured last/first/middle name for СвВодит/ФИО; fallback to split line."""
    f = fields or {}
    last = str(f.get("last_name") or "").strip()
    first = str(f.get("first_name") or "").strip()
    middle = str(f.get("middle_name") or "").strip()
    if last or first or middle:
        return last, first, middle
    return _split_fio(fallback_name or str(f.get("full_name") or ""))


def _region_from_postal_index(index: str) -> str:
    """Map postal index to subject code. Never use index[:2] — 390xxx is Ryazan (62), not Kaliningrad (39)."""
    idx = re.sub(r"\D", "", str(index or ""))
    if len(idx) < 3:
        return ""
    prefix = int(idx[:3])
    for start, end, code in _POSTAL3_RANGES:
        if start <= prefix <= end:
            return code
    return ""


def _region_code_from_text(address: str, index: str = "") -> str:
    """Resolve КодРегион from address text first, then postal ranges — never index[:2]."""
    low = str(address or "").lower().replace("ё", "е")
    for pattern, code in _REGION_PATTERNS:
        if re.search(pattern, low, flags=re.I):
            return code
    # Fall back to postal 3-digit ranges (still better than OKATO≠index[:2]).
    return _region_from_postal_index(index or "")


def _extract_address_from_requisites(requisites: str) -> str:
    """Pull legal address text from supply legal-entity requisites field only."""
    raw = str(requisites or "").strip()
    if not raw:
        return ""
    m = re.search(
        r"(?:юр\.?\s*адрес|юридический\s*адрес|фактический\s*адрес|адрес)\s*[:=]?\s*(.+)",
        raw,
        flags=re.I | re.S,
    )
    if m:
        candidate = m.group(1).strip()
        candidate = re.split(
            r"\n|(?:р/?с|к/?с|расчетн|бик|банк)\b",
            candidate,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" .;,")
        if candidate:
            return candidate[:500]

    cleaned = raw
    cleaned = re.sub(r"ИНН\s*[:=]?\s*\d{10,12}", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"КПП\s*[:=]?\s*\d{9}", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"ОГРН\s*[:=]?\s*\d+", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:р/?с|к/?с|бик|банк)[^\n]*", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.;")
    if not cleaned:
        return ""
    idx_m = re.search(r"\b\d{6}\b.{5,300}", cleaned)
    if idx_m:
        return idx_m.group(0).strip(" ,.;")[:500]
    if re.search(
        r"(?:\b\d{6}\b|г\.|город|ул\.|улица|д\.|дом|обл|край|район|пос|деревн|стр\.|строение)",
        cleaned,
        flags=re.I,
    ):
        return cleaned[:500]
    # Do not treat leftover org name / ИНН stubs as an address.
    return ""


def _empty_ru_address(raw: str = "") -> dict[str, str]:
    return {
        "Индекс": "",
        "КодРегион": "",
        "Район": "",
        "Город": "",
        "НаселПункт": "",
        "Улица": "",
        "Дом": "",
        "Корпус": "",
        "Кварт": "",
        "ФИАС": "",
        "raw": str(raw or "").strip(),
    }


def _normalize_fias_id(value: object) -> str:
    """Return lowercase FIAS GUID or empty string."""
    raw = re.sub(r"[{}\s]", "", str(value or "").strip()).lower()
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw):
        return raw
    return ""


def _addr_from_structured_fields(
    src: dict[str, Any] | None,
    *,
    mapping: tuple[tuple[str, str], ...],
    raw_fallback: str = "",
) -> dict[str, str]:
    """Map structured address columns to eTrN АдрРФ fields."""
    p = src or {}
    out = _empty_ru_address(raw_fallback)
    for src_key, dst in mapping:
        val = str(p.get(src_key) or "").strip()
        if val:
            out[dst] = val
    if out["Индекс"]:
        out["Индекс"] = re.sub(r"\D", "", out["Индекс"])[:6]
    if out["КодРегион"]:
        out["КодРегион"] = re.sub(r"\D", "", out["КодРегион"])[:2].zfill(2)
        if out["КодРегион"] == "00":
            out["КодРегион"] = ""
    if out.get("ФИАС"):
        out["ФИАС"] = _normalize_fias_id(out["ФИАС"])
    return out


_PRODUCTION_ADDR_MAP = (
    ("addr_index", "Индекс"),
    ("addr_region_code", "КодРегион"),
    ("addr_district", "Район"),
    ("addr_city", "Город"),
    ("addr_settlement", "НаселПункт"),
    ("addr_street", "Улица"),
    ("addr_house", "Дом"),
    ("addr_corpus", "Корпус"),
    ("addr_flat", "Кварт"),
    ("addr_fias", "ФИАС"),
)

_CARRIER_ADDR_MAP = (
    ("carrier_addr_index", "Индекс"),
    ("carrier_addr_region_code", "КодРегион"),
    ("carrier_addr_district", "Район"),
    ("carrier_addr_city", "Город"),
    ("carrier_addr_settlement", "НаселПункт"),
    ("carrier_addr_street", "Улица"),
    ("carrier_addr_house", "Дом"),
    ("carrier_addr_corpus", "Корпус"),
    ("carrier_addr_flat", "Кварт"),
    ("carrier_addr_fias", "ФИАС"),
)


def _addr_from_production_fields(prod: dict[str, Any] | None) -> dict[str, str]:
    """Map structured production address columns to eTrN АдрРФ fields."""
    p = prod or {}
    return _addr_from_structured_fields(
        p,
        mapping=_PRODUCTION_ADDR_MAP,
        raw_fallback=str(p.get("address") or ""),
    )


def _addr_from_warehouse_fields(wh: dict[str, Any] | None) -> dict[str, str]:
    """Map structured warehouse address columns to eTrN АдресРФ fields."""
    w = wh or {}
    return _addr_from_structured_fields(
        w,
        mapping=_PRODUCTION_ADDR_MAP,
        raw_fallback=str(w.get("address") or ""),
    )


def _addr_from_carrier_fields(driver: dict[str, Any] | None) -> dict[str, str]:
    """Map structured carrier address columns to eTrN АдрРФ fields."""
    d = driver or {}
    return _addr_from_structured_fields(
        d,
        mapping=_CARRIER_ADDR_MAP,
        raw_fallback=str(d.get("carrier") or ""),
    )


def _carrier_org_from_fields(fields: dict[str, Any] | None) -> tuple[str, str, str]:
    """Return (name, inn, kpp) from structured carrier fields."""
    f = fields or {}
    name = str(f.get("carrier_name") or "").strip()
    inn = re.sub(r"\D", "", str(f.get("carrier_inn") or ""))[:12]
    kpp = re.sub(r"\D", "", str(f.get("carrier_kpp") or ""))[:9]
    return name, inn, kpp


def _has_structured_address(addr: dict[str, str] | None) -> bool:
    if not addr:
        return False
    return any(
        str(addr.get(k) or "").strip()
        for k in ("Индекс", "КодРегион", "Район", "Город", "НаселПункт", "Улица", "Дом", "Корпус", "Кварт")
    )


def _parse_ru_address(address: str) -> dict[str, str]:
    """Best-effort split of a free-form Russian address into template fields."""
    raw = str(address or "").strip()
    out = _empty_ru_address(raw)
    if not raw:
        return out
    idx_m = re.search(r"\b(\d{6})\b", raw)
    if idx_m:
        out["Индекс"] = idx_m.group(1)
    out["КодРегион"] = _region_code_from_text(raw, out["Индекс"])

    # Street markers: prefix («ул. Ленина») or suffix («Пресненская наб.»).
    # Bare «ш»/«д» must not match inside words.
    _street_type = (
        r"(?:ул\.|улица|пр-кт|проспект|пер\.|переулок|ш\.|шоссе|б-р|бульвар|"
        r"наб\.|набережная|пл\.|площадь|проезд|ал\.|аллея)"
    )
    street_m = re.search(
        rf"(?<![А-Яа-яA-Za-z]){_street_type}\s*([^,]+?)(?=,|\s+(?:д\.|дом)\s*\d|$)",
        raw,
        flags=re.I,
    )
    if not street_m:
        # Suffix form common for embankments / named streets: «Пресненская наб.»
        street_m = re.search(
            rf"([^,]+?)\s+{_street_type}\.?(?=,|\s+(?:д\.|дом)\s*\d|$)",
            raw,
            flags=re.I,
        )
    if street_m:
        out["Улица"] = street_m.group(0).strip().rstrip(",")

    # House: «д.» / «дом» / «стр.» / «строение» — never bare «д» (деревня).
    house_m = re.search(
        r"(?:^|[\s,])(?:д\.|дом|стр\.|строение)\s*([0-9A-Za-zА-Яа-я/-]+)",
        raw,
        flags=re.I,
    )
    if house_m:
        out["Дом"] = house_m.group(1)

    city_m = re.search(r"(?<![А-Яа-яA-Za-z])(?:г\.|город)\s*([^,]+)", raw, flags=re.I)
    if city_m:
        out["Город"] = city_m.group(1).strip()
    # Village / settlement name (деревня X / пос. X).
    sett_m = re.search(
        r"(?<![А-Яа-яA-Za-z])(?:деревня|село|посёлок|поселок|пос\.|с\.)\s*([^,]+)",
        raw,
        flags=re.I,
    )
    if sett_m:
        out["НаселПункт"] = sett_m.group(1).strip()
    district_m = re.search(
        r"([^,]+?)\s+(?:р-н|район)\b",
        raw,
        flags=re.I,
    )
    if district_m:
        out["Район"] = district_m.group(1).strip() + " р-н"
    return out


def _parse_driver_license(documents: str) -> dict[str, str]:
    """Extract VU series/number/date or INNFL from free-text driver documents."""
    raw = str(documents or "")
    out = {"СерВУ": "", "НомВУ": "", "ДатаВыдВУ": "", "ИННФЛ": ""}
    if not raw:
        return out
    inn_m = re.search(r"ИНН\s*[:=]?\s*(\d{12})", raw, flags=re.I)
    if inn_m:
        out["ИННФЛ"] = inn_m.group(1)
    date_m = re.search(
        r"(?:выд|выдач|от)\s*[.:]?\s*(\d{2}\.\d{2}\.\d{4})",
        raw,
        flags=re.I,
    )
    if date_m:
        out["ДатаВыдВУ"] = date_m.group(1)
    # «ВУ 99 00 123456» / «серия 99 00 номер 123456»
    vu_m = re.search(
        r"(?:ву|в/у|водительск\w*\s*уд\w*)\s*[:.]?\s*"
        r"(\d{2})\s*(\d{2})\s+(\d{6})",
        raw,
        flags=re.I,
    )
    if vu_m:
        out["СерВУ"] = f"{vu_m.group(1)}{vu_m.group(2)}"
        out["НомВУ"] = vu_m.group(3)
        return out
    ser_m = re.search(r"сери[яи]\s*[:=]?\s*(\d{2})\s*(\d{2})", raw, flags=re.I)
    num_m = re.search(r"(?:номер|№)\s*[:=]?\s*(\d{6})", raw, flags=re.I)
    if ser_m and num_m:
        out["СерВУ"] = f"{ser_m.group(1)}{ser_m.group(2)}"
        out["НомВУ"] = num_m.group(1)
    return out


def _driver_vu_from_fields(
    fields: dict[str, Any] | None,
    documents: str = "",
) -> dict[str, str]:
    """Prefer structured driver doc columns for СвВодит; fallback to free-text parse."""
    f = fields or {}
    series = re.sub(r"\s+", "", str(f.get("doc_vu_series") or "").strip())[:20]
    number = re.sub(r"\s+", "", str(f.get("doc_vu_number") or "").strip())[:20]
    date_val = str(f.get("doc_vu_date") or "").strip()
    if date_val and not re.match(r"^\d{2}\.\d{2}\.\d{4}$", date_val):
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", date_val)
        date_val = f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else ""
    inn = re.sub(r"\D", "", str(f.get("doc_inn_fl") or ""))[:12]
    out = {"СерВУ": series, "НомВУ": number, "ДатаВыдВУ": date_val, "ИННФЛ": inn}
    if out["СерВУ"] or out["НомВУ"] or out["ДатаВыдВУ"] or out["ИННФЛ"]:
        # Fill any gaps from legacy free-text documents.
        parsed = _parse_driver_license(documents or str(f.get("documents") or ""))
        for key in out:
            if not out[key] and parsed.get(key):
                out[key] = parsed[key]
        return out
    return _parse_driver_license(documents or str(f.get("documents") or ""))


def parse_cargoes_cache(cargoes_json: object) -> dict[str, Any]:
    """Normalize cached cargoes_json (legacy list or v2 object) into groups + transport."""
    groups: list[dict[str, Any]] = []
    transport: list[dict[str, Any]] = []
    parsed: object = cargoes_json
    if isinstance(cargoes_json, str) and cargoes_json.strip():
        try:
            parsed = json.loads(cargoes_json)
        except Exception:
            parsed = None
    if isinstance(parsed, list):
        groups = [g for g in parsed if isinstance(g, dict)]
    elif isinstance(parsed, dict):
        raw_groups = parsed.get("groups")
        if isinstance(raw_groups, list):
            groups = [g for g in raw_groups if isinstance(g, dict)]
        raw_tr = parsed.get("transport_cargoes")
        if isinstance(raw_tr, list):
            for tc in raw_tr:
                if not isinstance(tc, dict):
                    continue
                try:
                    box_count = int(tc.get("box_count") or 0)
                except (TypeError, ValueError):
                    box_count = 0
                transport.append(
                    {
                        "type": str(tc.get("type") or "PALLET").upper() or "PALLET",
                        "transport_cargo_id": str(tc.get("transport_cargo_id") or ""),
                        "box_count": max(0, box_count),
                    }
                )
    return {"groups": groups, "transport_cargoes": transport}


def build_ozon_cargoes_cache(
    *,
    flat_cargoes: list[dict[str, Any]] | None = None,
    supplies_cargoes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build v2 cargoes cache from Ozon /v1/cargoes/get + /v1/cargoes/supplies/get."""
    groups_map: dict[tuple[str, str], int] = {}
    for c in flat_cargoes or []:
        if not isinstance(c, dict):
            continue
        key = (str(c.get("type") or ""), str(c.get("content_type") or ""))
        groups_map[key] = groups_map.get(key, 0) + 1
    groups = [
        {"type": k[0], "content_type": k[1], "count": v}
        for k, v in groups_map.items()
    ]
    transport: list[dict[str, Any]] = []
    for sc in supplies_cargoes or []:
        if not isinstance(sc, dict):
            continue
        for tc in sc.get("transport_cargoes") or []:
            if not isinstance(tc, dict):
                continue
            inner = [x for x in (tc.get("cargoes") or []) if isinstance(x, dict)]
            transport.append(
                {
                    "type": str(tc.get("type") or "PALLET").upper() or "PALLET",
                    "transport_cargo_id": str(tc.get("transport_cargo_id") or ""),
                    "box_count": len(inner),
                }
            )
    return {
        "version": 2,
        "groups": groups,
        "transport_cargoes": transport,
    }


def _cargo_stats(cargoes_json: object) -> dict[str, Any]:
    parsed = parse_cargoes_cache(cargoes_json)
    groups = parsed["groups"]
    transport = parsed["transport_cargoes"]

    pallets = 0
    boxes = 0
    other = 0
    tons = 0.0
    for g in groups:
        typ = str(g.get("type") or "").upper()
        try:
            count = int(g.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        tons += float(_CARGO_WEIGHT_TONS.get(typ, 0.0)) * count
        if typ == "PALLET":
            pallets += count
        elif typ == "BOX":
            boxes += count
        else:
            other += count

    # Transport cargoes from Ozon LK (паллеты с коробами внутри) override flat PALLET count.
    if transport:
        pallets = len(transport)
        boxes_from_tr = sum(int(tc.get("box_count") or 0) for tc in transport)
        if boxes_from_tr > 0:
            boxes = boxes_from_tr
            # Prefer box-based mass when transport hierarchy is known.
            tons = float(_CARGO_WEIGHT_TONS.get("BOX", 0.0)) * boxes
        else:
            tons = float(_CARGO_WEIGHT_TONS.get("PALLET", 0.0)) * pallets

    # eTrN КолМестГр = транспортные грузоместа (паллеты), иначе плоские места.
    total_places = pallets if pallets > 0 else (boxes + other)
    parts: list[str] = []
    if pallets:
        parts.append(f"{pallets} палет")
    if boxes:
        parts.append(f"{boxes} коробок")
    if other and not pallets:
        parts.append(f"{other} мест")
    places_label = ", ".join(parts) if parts else ""
    cargo_name = "Текстиль"
    if pallets and boxes:
        cargo_name = f"Текстиль. {pallets} палет ({boxes} коробок)"
    elif places_label:
        cargo_name = f"Текстиль. {places_label}"
    kg = int(round(tons * 1000)) if tons > 0 else 0
    # Required by schema: never emit empty place/mass fields.
    if total_places <= 0:
        total_places = 1
        places_label = places_label or "1 место (уточнить)"
        cargo_name = "Текстиль (количество мест уточнить)"
    if kg <= 0:
        kg = 1
    return {
        "pallets": pallets,
        "boxes": boxes,
        "other": other,
        "total_places": total_places,
        "tons": tons,
        "kg": kg,
        "places_label": places_label or "Отсутствует",
        "cargo_name": cargo_name,
        "transport_cargoes": transport,
    }


def _le_has_phone(entity: dict[str, Any] | None) -> bool:
    """True only when catalog phone field looks like a real number (not ИНН in requisites)."""
    if not entity:
        return False
    raw = str(entity.get("phone") or "").strip()
    if re.search(r"\d{10,}", raw):
        return True
    # Marked phone in requisites only (+7 or «тел.») — never bare ИНН/КПП.
    req = str(entity.get("requisites") or "")
    if re.search(r"\+7[\d\-()\s]{9,}\d", req):
        return True
    if re.search(r"(?:тел\.?|телефон|моб\.?)\s*[:№]?\s*(?:\+?7|8)[\d\-()\s]{8,}\d", req, flags=re.I):
        return True
    return False


def _find_legal_entity(entities: list[dict[str, Any]], supplier_name: str) -> dict[str, Any]:
    if not entities:
        return {}
    # Single catalog entry — always use it (phone lives there).
    if len(entities) == 1:
        return entities[0]

    supplier = str(supplier_name or "").strip().lower()
    exact: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    if supplier:
        for e in entities:
            short = str(e.get("short_name") or "").strip().lower()
            full = str(e.get("full_name") or "").strip().lower()
            if not short and not full:
                continue
            if supplier == short or supplier == full:
                exact.append(e)
            elif (short and (supplier in short or short in supplier)) or (
                full and (supplier in full or full in supplier)
            ):
                partial.append(e)
    matches = exact or partial
    if matches:
        for e in matches:
            if _le_has_phone(e):
                return e
        return matches[0]

    # Fallback: prefer ООО / any entry that actually has a phone.
    ooo = [e for e in entities if "ООО" in str(e.get("short_name") or "")]
    for pool in (ooo, entities):
        for e in pool:
            if _le_has_phone(e):
                return e
        if pool:
            return pool[0]
    return {}


def _find_driver(drivers: list[dict[str, Any]], driver_name: str) -> dict[str, Any]:
    target = str(driver_name or "").strip().lower()
    if not target:
        return {}
    for d in drivers:
        name = str(d.get("full_name") or "").strip().lower()
        if name == target:
            return d
    tokens = [t for t in target.split() if t]
    for d in drivers:
        name = str(d.get("full_name") or "").strip().lower()
        if tokens and all(t in name for t in tokens):
            return d
    return {}


def _vehicle_parts(vehicle_json: object, fallback_line: str = "") -> tuple[str, str]:
    params = _vehicle_params(vehicle_json=vehicle_json, fallback_line=fallback_line)
    return params["model"], params["number"]


def _parse_vehicles_json(raw: object) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            items = parsed if isinstance(parsed, list) else []
        except Exception:
            items = []
    else:
        items = []
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            line = item.strip()
            if not line:
                continue
            parts = [t for t in line.split() if t]
            model, number = line, ""
            if parts:
                maybe = parts[-1]
                if re.search(r"\d", maybe):
                    number = maybe[:9]
                    model = " ".join(parts[:-1]).strip()
            out.append(
                {
                    "model": model,
                    "number": number,
                    "type": "грузовой автомобиль",
                    "ownership": "1",
                    "capacity_t": "20",
                    "volume_m3": "20",
                    "line": f"{model} {number}".strip() or line,
                }
            )
        elif isinstance(item, dict):
            out.append(item)
    return out


def _match_driver_vehicle(
    driver_row: dict[str, Any] | None,
    vehicle_line: str = "",
) -> dict[str, Any]:
    """Pick structured catalog vehicle for the selected «марка номер» line."""
    if not driver_row:
        return {}
    vehicles = _parse_vehicles_json(driver_row.get("vehicles_json"))
    if not vehicles:
        return {}
    target = re.sub(r"\s+", " ", str(vehicle_line or "").strip().lower())
    if target:
        for v in vehicles:
            line = re.sub(
                r"\s+",
                " ",
                str(v.get("line") or f"{v.get('model') or ''} {v.get('number') or ''}").strip().lower(),
            )
            number = re.sub(r"\s+", "", str(v.get("number") or "").strip().lower())
            if line and line == target:
                return dict(v)
            if number and (target.endswith(number) or target == number):
                return dict(v)
    if len(vehicles) == 1:
        return dict(vehicles[0])
    return {}


def _vehicle_params(
    *,
    vehicle_json: object = None,
    fallback_line: str = "",
    vehicle_fields: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Resolve СвТС/ТС + ПарТС fields (catalog → Ozon JSON → free-text line)."""
    model, number = "", ""
    v_type = "грузовой автомобиль"
    ownership = "1"
    capacity = "20"
    volume = "20"

    fields = vehicle_fields or {}
    if fields:
        model = str(fields.get("model") or fields.get("vehicle_model") or "").strip()
        number = str(fields.get("number") or fields.get("vehicle_number") or "").strip()
        v_type = str(fields.get("type") or "").strip() or v_type
        own = str(fields.get("ownership") or "").strip()
        if own in {"1", "2", "3", "4", "5"}:
            ownership = own
        cap = str(fields.get("capacity_t") or "").strip().replace(",", ".")
        if re.match(r"^\d{1,5}(?:\.\d{1,2})?$", cap or ""):
            capacity = cap
        vol = str(fields.get("volume_m3") or "").strip().replace(",", ".")
        if re.match(r"^\d{1,4}(?:\.\d{1,2})?$", vol or ""):
            volume = vol
        if not model and not number:
            line = str(fields.get("line") or "").strip()
            if line:
                parts = line.split()
                if parts:
                    maybe_plate = parts[-1]
                    if re.search(r"\d", maybe_plate):
                        number = maybe_plate
                        model = " ".join(parts[:-1]).strip()
                    else:
                        model = line

    data: dict[str, Any] = {}
    if isinstance(vehicle_json, dict):
        data = vehicle_json
    elif isinstance(vehicle_json, str) and vehicle_json.strip() and vehicle_json.strip() != "{}":
        try:
            parsed = json.loads(vehicle_json)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    if not model:
        model = str(data.get("vehicle_model") or data.get("model") or "").strip()
    if not number:
        number = str(data.get("vehicle_number") or data.get("number") or "").strip()
    if data.get("type") and v_type == "грузовой автомобиль":
        alt_type = str(data.get("type") or "").strip()
        if alt_type:
            v_type = alt_type
    own2 = str(data.get("ownership") or "").strip()
    if own2 in {"1", "2", "3", "4", "5"} and ownership == "1" and not fields.get("ownership"):
        ownership = own2
    if capacity == "20":
        cap2 = str(data.get("capacity_t") or "").strip().replace(",", ".")
        if re.match(r"^\d{1,5}(?:\.\d{1,2})?$", cap2 or ""):
            capacity = cap2
    if volume == "20":
        vol2 = str(data.get("volume_m3") or "").strip().replace(",", ".")
        if re.match(r"^\d{1,4}(?:\.\d{1,2})?$", vol2 or ""):
            volume = vol2

    if not model and not number and fallback_line:
        parts = str(fallback_line).strip().split()
        if parts:
            maybe_plate = parts[-1]
            if re.search(r"\d", maybe_plate):
                number = maybe_plate
                model = " ".join(parts[:-1]).strip()
            else:
                model = str(fallback_line).strip()
    # РегНомер is T(1-9) in schema — keep first 9 chars if longer.
    if len(number) > 9:
        number = number[:9]
    return {
        "model": model,
        "number": number,
        "type": v_type,
        "ownership": ownership,
        "capacity_t": capacity,
        "volume_m3": volume,
    }


def _parse_supply_datetime(value: object, *, fallback: datetime | None = None) -> datetime:
    """Parse supply_date (ISO / date-only / DD.MM.YYYY) for eTrN timestamps."""
    raw = str(value or "").strip()
    if raw:
        try:
            # ISO with optional Z / timezone / time.
            iso = raw.replace("Z", "+00:00")
            if "T" in iso or " " in iso:
                return datetime.fromisoformat(iso.replace(" ", "T", 1))
            return datetime.fromisoformat(iso[:10])
        except Exception:
            pass
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                return datetime.strptime(raw[:19], fmt)
            except Exception:
                continue
    return fallback or datetime.now()


def _format_dt_vz(value: object, *, fallback: datetime | None = None) -> str:
    """Format ДатаВремяВЗТип: ДД.ММ.ГГГГТЧЧ:ММ:СС+03:00 (fixed MSK offset for drafts)."""
    dt = _parse_supply_datetime(value, fallback=fallback)
    # Date-only supply_date → midnight; keep time when timeslot provides it.
    if not str(value or "").strip() or (
        len(str(value).strip()) <= 10 and "T" not in str(value) and " " not in str(value).strip()
    ):
        return f"{dt.strftime('%d.%m.%Y')}T00:00:00+03:00"
    return f"{dt.strftime('%d.%m.%Y')}T{dt.strftime('%H:%M:%S')}+03:00"


def _el(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    clean_attrs = {k: str(v) for k, v in attrs.items() if v is not None and str(v) != ""}
    node = ET.SubElement(parent, tag, clean_attrs)
    if text is not None:
        node.text = str(text)
    return node


def _add_phone(parent: ET.Element, tag: str, phone: str) -> None:
    phone = str(phone or "").strip()
    if phone:
        _el(parent, tag, phone)


def _add_contact(parent: ET.Element, phone: str) -> None:
    phone = str(phone or "").strip()
    if not phone:
        return
    contact = _el(parent, "Контакт")
    _el(contact, "Тлф", phone)


def _add_adr_rf(parent: ET.Element, tag: str, addr: dict[str, str]) -> None:
    """Always emit АдрРФ / АдресРФ (never АдрИнф — Kontur shows that as foreign)."""
    raw = str(addr.get("raw") or "").strip()
    attrs = {
        k: addr[k]
        for k in ("Индекс", "КодРегион", "Район", "Город", "НаселПункт", "Улица", "Дом", "Корпус", "Кварт")
        if addr.get(k)
    }
    if not attrs.get("КодРегион"):
        code = _region_code_from_text(raw, attrs.get("Индекс", ""))
        if code:
            attrs["КодРегион"] = code
    # Keep Russian address type; if structured parse is weak, prefer full raw in Улица.
    if raw and not attrs.get("Улица") and not attrs.get("Индекс"):
        attrs["Улица"] = raw[:255]
    elif raw and not attrs.get("Улица") and not any(
        attrs.get(k) for k in ("Город", "НаселПункт", "Дом")
    ):
        attrs["Улица"] = raw[:255]
    if not attrs and not raw:
        return
    if not attrs:
        attrs = {"Улица": raw[:255]}
    # Schema marks КодРегион required for АдрРФТип — keep RF type visible in Kontur.
    if not attrs.get("КодРегион") and attrs.get("Индекс"):
        code = _region_from_postal_index(attrs["Индекс"])
        if code:
            attrs["КодРегион"] = code
    _el(parent, tag, **attrs)


def _ozon_supply_number(item: dict[str, Any] | None) -> str:
    """Номер поставки из основной таблицы Поставки → ОЗОН (`supply_order_number`).

    Это значение уходит в ``ИнфПол/ТекстИнф`` (``Идентиф=Orders`` / ``ORDERS``).
    Если колонка пуста — пробуем ``order_number`` из ``raw_json``, затем
    внутренний ``supply_order_id`` как крайний fallback.
    """
    row = item or {}
    num = str(row.get("supply_order_number") or "").strip()
    if num:
        return num
    raw = row.get("raw_json")
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                num = str(parsed.get("order_number") or "").strip()
                if num:
                    return num
        except Exception:
            pass
    return str(row.get("supply_order_id") or "").strip()


def build_ozon_etrn_xml(
    *,
    item: dict[str, Any],
    le: dict[str, Any] | None = None,
    driver_name: str = "",
    driver_phone: str = "",
    driver_documents: str = "",
    driver_fields: dict[str, Any] | None = None,
    vehicle_line: str = "",
    vehicle_json: object = None,
    vehicle_fields: dict[str, Any] | None = None,
    cargoes_json: object = None,
    load_address: str = "",
    load_addr_fields: dict[str, str] | None = None,
    delivery_address: str = "",
    delivery_addr_fields: dict[str, str] | None = None,
    carrier_text: str = "",
    carrier_fields: dict[str, Any] | None = None,
    loader_name: str = "",
    now: datetime | None = None,
) -> bytes:
    """Build formal eTrN title-1 XML draft bytes (UTF-8)."""
    now = now or datetime.now()
    le = le or {}
    supply_num = _ozon_supply_number(item)
    org_full = str(le.get("full_name") or le.get("short_name") or item.get("supplier_name") or "").strip()
    org_req = str(le.get("requisites") or "")
    inn, kpp = _parse_inn_kpp(org_req)
    if not inn:
        inn, kpp2 = _parse_inn_kpp(org_full)
        kpp = kpp or kpp2

    # Prefer structured production address fields; fall back to free-text parse.
    if _has_structured_address(load_addr_fields):
        load_addr = dict(load_addr_fields or {})
        if not load_addr.get("raw"):
            load_addr["raw"] = str(load_address or "").strip()
        if not load_addr.get("КодРегион"):
            load_addr["КодРегион"] = _region_code_from_text(
                str(load_addr.get("raw") or ""),
                str(load_addr.get("Индекс") or ""),
            )
    else:
        load_addr = _parse_ru_address(load_address)
    # Delivery (consignee unload) — structured warehouse fields preferred.
    if _has_structured_address(delivery_addr_fields):
        dest_addr = dict(delivery_addr_fields or {})
        if not dest_addr.get("raw"):
            dest_addr["raw"] = str(delivery_address or "").strip()
        if not dest_addr.get("КодРегион"):
            dest_addr["КодРегион"] = _region_code_from_text(
                str(dest_addr.get("raw") or ""),
                str(dest_addr.get("Индекс") or ""),
            )
    else:
        dest_addr = _parse_ru_address(delivery_address)
    # Shipper legal address from Поставки → Настройки → Юр.лица.
    # Prefer structured address fields; else one-line address; else requisites parse.
    # Never substitute production/warehouse (load/delivery) addresses here.
    shipper_addr = _addr_from_production_fields(le)
    if not _has_structured_address(shipper_addr):
        legal_addr_raw = str(le.get("address") or "").strip() or _extract_address_from_requisites(org_req)
        shipper_addr = _parse_ru_address(legal_addr_raw)
    elif not shipper_addr.get("КодРегион"):
        shipper_addr["КодРегион"] = _region_code_from_text(
            str(shipper_addr.get("raw") or le.get("address") or ""),
            str(shipper_addr.get("Индекс") or ""),
        )

    cargo = _cargo_stats(cargoes_json if cargoes_json is not None else item.get("cargoes_json"))
    fam, imya, otch = _driver_fio_from_fields(driver_fields, driver_name)
    if not fam:
        fam, imya = "Не", "указан"
    v_params = _vehicle_params(
        vehicle_json=vehicle_json if vehicle_json is not None else item.get("vehicle_json"),
        fallback_line=vehicle_line,
        vehicle_fields=vehicle_fields,
    )
    v_model, v_number = v_params["model"], v_params["number"]
    c_name, c_inn, c_kpp = _carrier_org_from_fields(carrier_fields)
    if c_name or c_inn or c_kpp:
        carrier_name, carrier_inn, carrier_kpp = c_name, c_inn, c_kpp
    else:
        carrier_name, carrier_inn, carrier_kpp = _parse_carrier(carrier_text)
    if not carrier_name:
        carrier_name = "Перевозчик (уточнить)"
    vu = _driver_vu_from_fields(driver_fields, driver_documents)

    signer_src = str(le.get("signatories") or le.get("in_person") or "").strip()
    s_fam, s_imya, s_otch = _split_fio(signer_src)
    if not s_fam:
        s_fam, s_imya = "Не", "указан"

    # Phone from Поставки → Настройки → Юр.лица; used for all eTrN contacts.
    # Fallbacks keep older drafts working when the field is still empty.
    contact_phone = str(le.get("phone") or "").strip()
    if not contact_phone:
        contact_phone = str(driver_phone or "").strip()
    if not contact_phone:
        phone_m = re.search(r"(?:\+7|8)\s*[\d\-()\s]{9,}", org_req)
        if phone_m:
            contact_phone = re.sub(r"\s+", "", phone_m.group(0))

    date_ru = now.strftime("%d.%m.%Y")
    time_ru = now.strftime("%H:%M:%S")
    date_file = now.strftime("%Y%m%d")
    file_guid = str(uuid.uuid4())

    # A=carrier FNSId (from Водители → Перевозчик), E=fixed Ozon GUID, O=shipper draft id.
    # Kontur rewrites ИдФайл on import when needed. Empty A → ON_TRNACLGROT__{E}_...
    shipper_edo = f"2BM-{inn}-{kpp or '000000000'}-DRAFT" if inn else "2BM-DRAFT-SHIPPER"
    carrier_edo = str((carrier_fields or {}).get("carrier_fns_id") or "").strip()
    id_file = (
        f"ON_TRNACLGROT_"
        f"{carrier_edo}_"
        f"{OZON_CONSIGNEE_EDO_GUID}_"
        f"{shipper_edo}_"
        f"0_"
        f"{date_file}_"
        f"{file_guid}"
    )

    root = ET.Element(
        "Файл",
        {
            "ИдФайл": id_file,
            "ВерсФорм": "5.01",
            "ВерсПрог": "FeedPilot 1.0",
        },
    )
    doc = _el(
        root,
        "Документ",
        КНД="1110339",
        ПоФактХЖ="Транспортная накладная, информация грузоотправителя",
        ДатИнфГО=date_ru,
        ВрИнфГО=time_ru,
    )
    sod = _el(
        doc,
        "СодИнфГО",
        СодОпер=(
            "Лицом, осуществляющим погрузку груза, при указанных обстоятельствах "
            "передан водителю груз с указанными характеристиками"
        ),
        НомерТрН=supply_num or "Без номера",
        ДатаТрН=date_ru,
        НомЗак=supply_num or "Без номера",
        ДатаЗак=date_ru,
    )

    # --- СвГО ---
    sv_go = _el(sod, "СвГО", ГОЭксп="0")
    rek_go = _el(sv_go, "РекИдентГО")
    id_go = _el(rek_go, "ИдСв")
    go_attrs = {"НаимОрг": org_full or "Грузоотправитель"}
    if inn:
        go_attrs["ИННЮЛ"] = inn
    if kpp:
        go_attrs["КПП"] = kpp
    _el(id_go, "СвЮЛУч", **go_attrs)
    if shipper_addr.get("raw") or shipper_addr.get("Индекс"):
        adr_go = _el(rek_go, "Адрес")
        _add_adr_rf(adr_go, "АдрРФ", shipper_addr)
    _add_contact(rek_go, contact_phone)

    # --- СвГП (Ozon) ---
    sv_gp = _el(sod, "СвГП")
    rek_gp = _el(sv_gp, "РекИдентГП")
    id_gp = _el(rek_gp, "ИдСв")
    _el(
        id_gp,
        "СвЮЛУч",
        НаимОрг=OZON_CONSIGNEE_NAME,
        ИННЮЛ=OZON_CONSIGNEE_INN,
        КПП=OZON_CONSIGNEE_KPP,
    )
    # Legal address of consignee — always Russian АдрРФ (structured constant).
    adr_gp = _el(rek_gp, "Адрес")
    _add_adr_rf(adr_gp, "АдрРФ", dict(OZON_CONSIGNEE_ADDR_FIELDS))
    _add_contact(rek_gp, contact_phone)
    # Delivery point — always АдресРФ, never АдресИнф.
    adr_dost = _el(sv_gp, "АдресДостГр")
    if not dest_addr.get("raw"):
        dest_addr = {
            **dest_addr,
            "raw": str(item.get("warehouse_name") or item.get("transit_warehouse_name") or "Склад Ozon"),
        }
    _add_adr_rf(adr_dost, "АдресРФ", dest_addr)

    # --- СвГруз ---
    # КолМестГр = число транспортных грузомест (паллет), если они есть в Ozon.
    sv_gruz = _el(sod, "СвГруз")
    op = _el(
        sv_gruz,
        "ОпГруз",
        НаимГруз=cargo["cargo_name"],
        СостГруз="Без повреждений",
        СпУпак="Паллеты" if int(cargo.get("pallets") or 0) > 0 else "Коробки",
        ВидТар="00",
        КолМестГр=str(cargo["total_places"]),
        УчГосСист="0",
    )
    _el(op, "Марк", "Отсутствует")
    _el(op, "ПлМасГруз", МасБрутЗнач=str(cargo["kg"]))

    # --- УказГО ---
    # Дата и время доставки груза ← основная таблица поставок: supply_date.
    delivery_dt_vz = _format_dt_vz(item.get("supply_date"), fallback=now)
    ukaz = _el(
        sod,
        "УказГО",
        УкНормПрвз="Отсутствуют",
        ЗапрПерегруз="0",
        ДатВрДостГр=delivery_dt_vz,
        НалКоорТочВрДост="1",
    )
    sv_pa = _el(
        ukaz,
        "СвПА",
        СпосПерУкПА="Электронное уведомление перевозчика о переадресовке",
        ЛицоПА="Грузоотправитель",
    )
    kont_pa = _el(sv_pa, "КонтПА")
    _el(kont_pa, "Тлф", contact_phone or "не указан")

    # --- СвПер ---
    sv_per = _el(sod, "СвПер")
    id_per = _el(sv_per, "ИдСв")
    per_attrs = {"НаимОрг": carrier_name}
    if carrier_inn:
        per_attrs["ИННЮЛ"] = carrier_inn
    if carrier_kpp:
        per_attrs["КПП"] = carrier_kpp
    _el(id_per, "СвЮЛУч", **per_attrs)
    # Prefer structured carrier address; else parse free-text; else shipper fallback.
    carrier_addr = _addr_from_carrier_fields(carrier_fields)
    if not _has_structured_address(carrier_addr):
        carrier_addr = _parse_ru_address(_extract_address_from_requisites(carrier_text))
    if not (carrier_addr.get("Индекс") or carrier_addr.get("КодРегион") or carrier_addr.get("Улица")):
        carrier_addr = shipper_addr if (shipper_addr.get("raw") or shipper_addr.get("Индекс")) else {}
    if carrier_addr and not carrier_addr.get("КодРегион"):
        carrier_addr["КодРегион"] = _region_code_from_text(
            str(carrier_addr.get("raw") or ""),
            str(carrier_addr.get("Индекс") or ""),
        )
    adr_per = _el(sv_per, "Адрес")
    if carrier_addr.get("raw") or carrier_addr.get("Индекс") or carrier_addr.get("КодРегион"):
        _add_adr_rf(adr_per, "АдрРФ", carrier_addr)
    else:
        inn_region = (
            carrier_inn[:2] if carrier_inn and len(carrier_inn) >= 2
            else (inn[:2] if inn and len(inn) >= 2 else "77")
        )
        _el(adr_per, "АдрРФ", КодРегион=inn_region)
    # Carrier phone from catalog; fallback to юр.лица / driver (contact_phone).
    carrier_phone = str((carrier_fields or {}).get("carrier_phone") or "").strip()
    if not carrier_phone:
        carrier_phone = contact_phone
    _add_contact(sv_per, carrier_phone)

    # --- СвВодит ---
    # Schema: VU trio (СерВУ+НомВУ+ДатаВыдВУ) OR ИННФЛ required; both allowed.
    vod_attrs: dict[str, str] = {}
    if vu.get("СерВУ"):
        vod_attrs["СерВУ"] = vu["СерВУ"]
    if vu.get("НомВУ"):
        vod_attrs["НомВУ"] = vu["НомВУ"]
    if vu.get("ДатаВыдВУ"):
        vod_attrs["ДатаВыдВУ"] = vu["ДатаВыдВУ"]
    if vu.get("ИННФЛ"):
        vod_attrs["ИННФЛ"] = vu["ИННФЛ"]
    has_vu = bool(vod_attrs.get("НомВУ") and vod_attrs.get("СерВУ") and vod_attrs.get("ДатаВыдВУ"))
    has_inn = bool(vod_attrs.get("ИННФЛ"))
    if not has_vu and not has_inn:
        vod_attrs.setdefault("СерВУ", "0000")
        vod_attrs.setdefault("НомВУ", "000000")
        vod_attrs.setdefault("ДатаВыдВУ", "01.01.2000")
    sv_vod = _el(sod, "СвВодит", **vod_attrs)
    # Driver phone from catalog first; fallback to юр.лица (contact_phone).
    vod_phone = str((driver_fields or {}).get("phone") or "").strip()
    if not vod_phone:
        vod_phone = contact_phone
    _el(sv_vod, "Тлф", vod_phone or "не указан")
    fio_attrs = {"Фамилия": fam, "Имя": imya or "не указано"}
    if otch:
        fio_attrs["Отчество"] = otch
    _el(sv_vod, "ФИО", **fio_attrs)

    # --- СвТС ---
    sv_ts = _el(sod, "СвТС")
    ts = _el(
        sv_ts,
        "ТС",
        РегНомер=v_number or "А000АА00",
        ТипВлад=v_params.get("ownership") or "1",
    )
    _el(
        ts,
        "ПарТС",
        Тип=v_params.get("type") or "грузовой автомобиль",
        Марка=v_model or "не указана",
        Грузопод=v_params.get("capacity_t") or "20",
        Вместим=v_params.get("volume_m3") or "20",
    )

    # --- СвПогруз ---
    sv_pogr = _el(
        sod,
        "СвПогруз",
        ЗаявПогр=f"{date_ru}T00:00:00+03:00",
        НалКоорТочВрЗаяв="1",
        ФДатВрПриб=f"{date_ru}T00:00:00+03:00",
        НалКоорТочВрФПогр="1",
        ФДатВрУбыт=f"{date_ru}T00:00:00+03:00",
        НалКоорТочВрФУбыт="1",
        МасБрутОтгр=str(cargo["kg"]),
        МетОпрМасс="03",
        КолМестПрием=str(cargo["total_places"]),
    )
    f_adr = _el(sv_pogr, "ФАдресПогр")
    if not load_addr.get("raw"):
        load_addr = {**load_addr, "raw": "Адрес погрузки уточнить"}
    _add_adr_rf(f_adr, "АдресРФ", load_addr)

    # Работник погрузки ← Поставки → Настройки → Производства → Начальник.
    lich = _el(sv_pogr, "СвЛицПогрГр", СовпГОП="1")
    loader_fio = str(loader_name or "").strip()
    if loader_fio:
        l_fam, l_imya, l_otch = _split_fio(loader_fio)
        if not l_fam:
            l_fam, l_imya = "Не", "указан"
        rab = _el(lich, "РабЛицПогрГр", Должность="начальник производства")
        _el(rab, "ОДолжОб", "Должностные обязанности")
        fio_loader = {"Фамилия": l_fam, "Имя": l_imya or "не указано"}
        if l_otch:
            fio_loader["Отчество"] = l_otch
        _el(rab, "ФИО", **fio_loader)
    ident = _el(lich, "ИдентРекГО")
    if inn:
        _el(ident, "ИННЮЛ", inn)
    vlad = _el(sv_pogr, "ВладИнфр", СовпГОВ="1")
    ident2 = _el(vlad, "ИдентРекГО")
    if inn:
        _el(ident2, "ИННЮЛ", inn)

    # ИнфПол must be last in СодИнфГО sequence (table 5.3).
    # Идентиф=Orders / ORDERS → Значение = номер поставки из основной таблицы ОЗОН.
    if supply_num:
        inf = _el(sod, "ИнфПол")
        _el(inf, "ТекстИнф", Идентиф="Orders", Значение=supply_num)
        _el(inf, "ТекстИнф", Идентиф="ORDERS", Значение=supply_num)

    # Подписант is required under Документ (table 5.2).
    signer = _el(doc, "Подписант", СтатПодп="1", Должн="Уполномоченное лицо")
    signer_fio = {"Фамилия": s_fam, "Имя": s_imya or "не указано"}
    if s_otch:
        signer_fio["Отчество"] = s_otch
    _el(signer, "ФИО", **signer_fio)

    rough = ET.tostring(root, encoding="utf-8")
    try:
        return minidom.parseString(rough).toprettyxml(indent="  ", encoding="utf-8")
    except Exception:
        _log.exception("ozon_etrn: pretty print failed")
        return b'<?xml version="1.0" encoding="utf-8"?>\n' + rough


def collect_ozon_etrn_context(
    *,
    repository: Any,
    owner_id: int,
    item: dict[str, Any],
    driver_name: str = "",
    driver_phone: str = "",
    vehicle_line: str = "",
) -> dict[str, Any]:
    """Resolve addresses / LE / carrier extras for XML build."""
    entities = repository.list_supply_legal_entities(user_id=owner_id)
    le = dict(_find_legal_entity(entities, str(item.get("supplier_name") or "")) or {})
    # Mirror carrier_phone: ensure le.phone is the catalog «Телефон» field when present.
    # Re-read from entities by id (source of truth), then borrow from another entry if needed.
    if le.get("id") is not None:
        for e in entities:
            if str(e.get("id")) == str(le.get("id")):
                if str(e.get("phone") or "").strip():
                    le["phone"] = e.get("phone")
                break
    if not _le_has_phone(le):
        with_phone = [e for e in entities if _le_has_phone(e)]
        if with_phone:
            donor = with_phone[0]
            le = dict(le) if le else {}
            le["phone"] = donor.get("phone") or le.get("phone")
            if not str(le.get("requisites") or "").strip():
                le["requisites"] = donor.get("requisites")
    shipper_phone = str(le.get("phone") or "").strip()
    drivers = repository.list_supply_drivers(user_id=owner_id)
    driver_row = _find_driver(drivers, driver_name)
    if hasattr(repository, "carrier_line"):
        carrier_text = str(repository.carrier_line(driver_row) or "").strip()
    else:
        carrier_text = str((driver_row or {}).get("carrier") or "")
    carrier_fields = dict(driver_row or {}) if driver_row else {}
    if hasattr(repository, "driver_documents_line"):
        driver_documents = str(repository.driver_documents_line(driver_row) or "").strip()
    else:
        driver_documents = str((driver_row or {}).get("documents") or "")
    driver_fields = dict(driver_row or {}) if driver_row else {}
    vehicle_fields = _match_driver_vehicle(driver_row, vehicle_line)

    production_name = str(item.get("production") or "").strip()
    load_address = ""
    loader_name = ""
    load_addr_fields: dict[str, str] = _empty_ru_address()
    if production_name:
        for p in repository.list_supply_productions(user_id=owner_id):
            if str(p.get("name") or "").strip() == production_name:
                # One-line address for any consumer that still expects a string;
                # structured fields go to XML АдресРФ directly.
                if hasattr(repository, "production_address_line"):
                    load_address = str(repository.production_address_line(p) or "").strip()
                else:
                    load_address = str(p.get("address") or "").strip()
                loader_name = str(p.get("head_name") or "").strip()
                structured = _addr_from_production_fields(p)
                if _has_structured_address(structured):
                    load_addr_fields = structured
                    if not structured.get("raw"):
                        structured["raw"] = load_address
                    load_addr_fields = structured
                break

    dest_wh = str(item.get("warehouse_name") or "").strip()
    transit_wh = str(item.get("transit_warehouse_name") or "").strip()
    pickup_wh = transit_wh or dest_wh
    delivery_address = ""
    delivery_addr_fields: dict[str, str] = _empty_ru_address()
    if pickup_wh:
        for w in repository.list_supply_warehouses(user_id=owner_id):
            if str(w.get("warehouse_name") or "").strip() == pickup_wh:
                if hasattr(repository, "warehouse_address_line"):
                    delivery_address = str(repository.warehouse_address_line(w) or "").strip()
                else:
                    delivery_address = str(w.get("address") or "").strip()
                structured = _addr_from_warehouse_fields(w)
                if _has_structured_address(structured):
                    if not structured.get("raw"):
                        structured["raw"] = delivery_address
                    delivery_addr_fields = structured
                break
    if not delivery_address:
        delivery_address = pickup_wh

    return {
        "le": le,
        "shipper_phone": shipper_phone,
        "legal_entities": entities,
        "carrier_text": carrier_text,
        "carrier_fields": carrier_fields,
        "load_address": load_address,
        "load_addr_fields": load_addr_fields,
        "delivery_address": delivery_address,
        "delivery_addr_fields": delivery_addr_fields,
        "driver_name": driver_name,
        "driver_phone": driver_phone,
        "driver_documents": driver_documents,
        "driver_fields": driver_fields,
        "vehicle_line": vehicle_line,
        "vehicle_fields": vehicle_fields,
        "loader_name": loader_name,
    }
