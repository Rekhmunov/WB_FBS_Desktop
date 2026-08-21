# -*- coding: utf-8 -*-
"""Sticker scan lookup — web portal parity (`_wbFbsKizFindBySticker`)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def normalize_scan(value: object) -> str:
    return str(value or "").replace(" ", "").strip()


def scan_key(value: object) -> str:
    """Case-insensitive key (en-US lower) for sticker barcode / number match."""
    return normalize_scan(value).lower()


def _digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def build_sticker_index(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Map scan keys → row lists for O(1) primary lookup (web linear, desktop indexed)."""
    by_barcode = {}  # type: Dict[str, List[Dict[str, Any]]]
    by_full_key = {}  # type: Dict[str, List[Dict[str, Any]]]
    by_full_digits = {}  # type: Dict[str, List[Dict[str, Any]]]
    by_combo_digits = {}  # type: Dict[str, List[Dict[str, Any]]]
    by_part_b_key = {}  # type: Dict[str, List[Dict[str, Any]]]
    by_part_b_digits = {}  # type: Dict[str, List[Dict[str, Any]]]

    def _add(bucket: Dict[str, List[Dict[str, Any]]], key: str, row: Dict[str, Any]) -> None:
        if not key:
            return
        bucket.setdefault(key, []).append(row)

    for row in rows or []:
        bc = normalize_scan(row.get("sticker_barcode"))
        if bc:
            _add(by_barcode, scan_key(bc), row)
        full = normalize_scan(row.get("sticker_number"))
        part_a = normalize_scan(row.get("sticker_part_a"))
        part_b = normalize_scan(row.get("sticker_part_b"))
        if full:
            _add(by_full_key, scan_key(full), row)
            _add(by_full_digits, _digits(full), row)
        if part_a and part_b:
            _add(by_combo_digits, _digits(part_a + part_b), row)
        if part_b:
            _add(by_part_b_key, scan_key(part_b), row)
            _add(by_part_b_digits, _digits(part_b), row)

    return {
        "barcode": by_barcode,
        "full_key": by_full_key,
        "full_digits": by_full_digits,
        "combo_digits": by_combo_digits,
        "part_b_key": by_part_b_key,
        "part_b_digits": by_part_b_digits,
    }


def _unique_or_ambiguous(
    matches: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], bool, List[Dict[str, Any]]]:
    if len(matches) == 1:
        return matches[0], False, matches
    if len(matches) > 1:
        return None, True, matches
    return None, False, []


def find_row_by_sticker(
    rows: List[Dict[str, Any]],
    scan: object,
    index: Optional[Dict[str, Dict[str, List[Dict[str, Any]]]]] = None,
) -> Tuple[Optional[Dict[str, Any]], bool, List[Dict[str, Any]]]:
    """Match a scanned sticker to a row.

    Returns ``(row, ambiguous, matches)`` — same semantics as web:
    primary ``sticker_barcode``, then human-readable partA/partB / number.
    """
    raw = normalize_scan(scan)
    if not raw:
        return None, False, []
    raw_key = scan_key(raw)
    digits = _digits(raw)

    if index is not None:
        by_barcode = index.get("barcode") or {}
        by_bc = list(by_barcode.get(raw_key) or [])
        found, ambiguous, matches = _unique_or_ambiguous(by_bc)
        if found or ambiguous:
            return found, ambiguous, matches

        matches = []  # type: List[Dict[str, Any]]
        seen = set()  # type: set

        def _extend(items: List[Dict[str, Any]]) -> None:
            for row in items:
                oid = int(row.get("order_id") or id(row))
                if oid in seen:
                    continue
                seen.add(oid)
                matches.append(row)

        if raw_key:
            _extend(list((index.get("full_key") or {}).get(raw_key) or []))
            _extend(list((index.get("part_b_key") or {}).get(raw_key) or []))
        if digits:
            _extend(list((index.get("full_digits") or {}).get(digits) or []))
            _extend(list((index.get("combo_digits") or {}).get(digits) or []))
            _extend(list((index.get("part_b_digits") or {}).get(digits) or []))

        if len(matches) == 1:
            return matches[0], False, matches
        if len(matches) > 1:
            for row in matches:
                full = normalize_scan(row.get("sticker_number"))
                full_digits = _digits(full)
                if scan_key(full) == raw_key or (digits and full_digits == digits):
                    return row, False, matches
            return None, True, matches
        return None, False, []

    by_barcode = []  # type: List[Dict[str, Any]]
    for row in rows or []:
        bc = normalize_scan(row.get("sticker_barcode"))
        if bc and scan_key(bc) == raw_key:
            by_barcode.append(row)
    if len(by_barcode) == 1:
        return by_barcode[0], False, by_barcode
    if len(by_barcode) > 1:
        return None, True, by_barcode

    matches = []  # type: List[Dict[str, Any]]
    for row in rows or []:
        full = normalize_scan(row.get("sticker_number"))
        part_a = normalize_scan(row.get("sticker_part_a"))
        part_b = normalize_scan(row.get("sticker_part_b"))
        full_digits = _digits(full)
        combo_digits = _digits("{}{}".format(part_a, part_b))
        part_b_digits = _digits(part_b)
        if (
            (full and (raw_key == scan_key(full) or (digits and digits == full_digits)))
            or (part_a and part_b and digits and digits == combo_digits)
            or (
                part_b
                and (
                    raw_key == scan_key(part_b)
                    or (digits and digits == part_b_digits)
                )
            )
        ):
            matches.append(row)

    if len(matches) == 1:
        return matches[0], False, matches
    if len(matches) > 1:
        for row in matches:
            full = normalize_scan(row.get("sticker_number"))
            full_digits = _digits(full)
            if scan_key(full) == raw_key or (digits and full_digits == digits):
                return row, False, matches
        return None, True, matches
    return None, False, []
