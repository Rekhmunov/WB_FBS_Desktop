# -*- coding: utf-8 -*-
"""Ozon package QR + scanit (``ii…``) helpers — web archive parity."""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Tuple

# New Ozon FBS instance label (scanit): unique id printed as ``ii…`` from 05.10.2026.
_OZON_SCANIT_RE = re.compile(r"^ii[A-Za-z0-9._-]{2,}$", re.IGNORECASE)
_PLACEHOLDERS = frozenset({"0", "null", "none", "nil", "-", "n/a"})


def sticker_parts_from_posting_number(posting_number: object) -> Tuple[str, str]:
    """Split Ozon posting_number into sticker-style parts (order segment + suffix)."""
    pn = str(posting_number or "").strip()
    if not pn or "-" not in pn:
        return pn, ""
    head, tail = pn.split("-", 1)
    return str(head or "").strip(), str(tail or "").strip()


def normalize_ozon_package_barcode(value: object) -> str:
    """Return a usable Ozon package barcode, or ``""`` for empty/placeholders."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.casefold() in _PLACEHOLDERS:
        return ""
    if raw.isdigit() and len(raw) < 12:
        return ""
    return raw


def looks_like_ozon_scanit(value: object) -> bool:
    """True for the new Ozon instance label (``ii`` + identifier)."""
    q = re.sub(r"\s+", "", str(value or "").strip())
    return bool(_OZON_SCANIT_RE.fullmatch(q))


def normalize_ozon_scanit(value: object) -> str:
    """Return a usable Ozon ``scanit`` instance id, or ``""``."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.casefold() in _PLACEHOLDERS:
        return ""
    if not looks_like_ozon_scanit(raw):
        return ""
    return raw


def scanit_from_posting(posting: Mapping[str, Any]) -> str:
    scanit = normalize_ozon_scanit(posting.get("scanit"))
    if scanit:
        return scanit
    barcodes = posting.get("barcodes")
    if isinstance(barcodes, Mapping):
        return normalize_ozon_scanit(barcodes.get("scanit"))
    return ""


def sticker_fields_from_posting(posting: Mapping[str, Any]) -> Dict[str, str]:
    """Derive sticker binding from Ozon posting (package QR + scanit + parts)."""
    pn = str(posting.get("posting_number") or "").strip()
    part_a, part_b = sticker_parts_from_posting_number(pn)
    upper = ""
    lower = ""
    barcodes = posting.get("barcodes")
    if isinstance(barcodes, Mapping):
        upper = normalize_ozon_package_barcode(barcodes.get("upper_barcode"))
        lower = normalize_ozon_package_barcode(barcodes.get("lower_barcode"))
    return {
        "sticker_barcode": upper,
        "sticker_lower_barcode": lower,
        "sticker_scanit": scanit_from_posting(posting),
        "sticker_part_a": part_a,
        "sticker_part_b": part_b,
    }


def format_scanit_tail_html(scanit: object) -> str:
    """HTML for scanit with last 4 chars enlarged (web ``formatOzonScanitHtml``)."""
    s = str(scanit or "").strip()
    if not s:
        return ""
    # Escape minimal HTML specials for QLabel RichText.
    def _esc(t: str) -> str:
        return (
            t.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    if len(s) > 4:
        head, tail = s[:-4], s[-4:]
        return (
            '<span style="font-size:14px;font-weight:700;color:#0f172a;">{}</span>'
            '<span style="font-size:22px;font-weight:700;color:#0f172a;">{}</span>'
        ).format(_esc(head), _esc(tail))
    return (
        '<span style="font-size:22px;font-weight:700;color:#0f172a;">{}</span>'
    ).format(_esc(s))
