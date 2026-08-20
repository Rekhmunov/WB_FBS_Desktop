# -*- coding: utf-8 -*-
"""Sticker scan lookup — web portal parity (`_wbFbsKizFindBySticker`)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def normalize_scan(value: object) -> str:
    return str(value or "").replace(" ", "").strip()


def scan_key(value: object) -> str:
    """Case-insensitive key (en-US lower) for sticker barcode / number match."""
    return normalize_scan(value).lower()


def find_row_by_sticker(
    rows: List[Dict[str, Any]],
    scan: object,
) -> Tuple[Optional[Dict[str, Any]], bool, List[Dict[str, Any]]]:
    """Match a scanned sticker to a row.

    Returns ``(row, ambiguous, matches)`` — same semantics as web:
    primary ``sticker_barcode``, then human-readable partA/partB / number.
    """
    raw = normalize_scan(scan)
    if not raw:
        return None, False, []
    raw_key = scan_key(raw)
    digits = "".join(ch for ch in raw if ch.isdigit())

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
        full_digits = "".join(ch for ch in full if ch.isdigit())
        combo_digits = "".join(
            ch for ch in "{}{}".format(part_a, part_b) if ch.isdigit()
        )
        part_b_digits = "".join(ch for ch in part_b if ch.isdigit())
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
            full_digits = "".join(ch for ch in full if ch.isdigit())
            if scan_key(full) == raw_key or (digits and full_digits == digits):
                return row, False, matches
        return None, True, matches
    return None, False, []
