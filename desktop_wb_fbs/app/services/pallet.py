# -*- coding: utf-8 -*-
"""Pallet/box estimate after sync (Новые + На сборке)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.db import Database
from app.services.catalog import CategoryService, ProductService
from app.wb import TAB_ASSEMBLY, TAB_NEW


def _as_positive_int(value: object) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def format_pallets_ru(value: float) -> str:
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
        text = "{:.2f}".format(n).rstrip("0").rstrip(".").replace(".", ",")
        word = "паллета"
    return "{} {}".format(text, word)


def format_boxes_ru(value: float) -> str:
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
        text = "{:.2f}".format(n).rstrip("0").rstrip(".").replace(".", ",")
        word = "короба"
    return "{} {}".format(text, word)


def compute_pallet_summary(
    db: Database, sources: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not sources:
        return []
    products = ProductService(db).list_all()
    categories = CategoryService(db).list_all()
    cat_boxes = {}  # type: Dict[str, int]
    for cat in categories:
        name = str(cat.get("name") or "").strip()
        bpp = _as_positive_int(cat.get("boxes_per_pallet"))
        if name and bpp is not None:
            cat_boxes[name] = bpp

    product_meta = {}  # type: Dict[str, Tuple[int, Optional[int]]]
    for prod in products:
        box_qty = _as_positive_int(prod.get("box_qty"))
        if box_qty is None:
            continue
        cat_name = str(prod.get("product_category") or "").strip()
        bpp = cat_boxes.get(cat_name)
        meta = (box_qty, bpp)
        for raw_key in (prod.get("supplier_article"), prod.get("wb_nmid")):
            key = str(raw_key or "").strip()
            if not key:
                continue
            product_meta[key] = meta
            product_meta[key.casefold()] = meta

    source_ids = []  # type: List[int]
    source_names = {}  # type: Dict[int, str]
    for src in sources:
        try:
            sid = int(src.get("id"))
        except (TypeError, ValueError):
            continue
        source_ids.append(sid)
        source_names[sid] = str(src.get("name") or "Источник {}".format(sid))

    if not source_ids:
        return []

    placeholders = ", ".join("?" for _ in source_ids)
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT source_id, article, nm_id, COUNT(*) AS qty
            FROM wb_fbs_orders
            WHERE tab IN (?, ?)
              AND source_id IN ({})
            GROUP BY source_id, article, nm_id
            """.format(
                placeholders
            ),
            tuple([TAB_NEW, TAB_ASSEMBLY] + source_ids),
        ).fetchall()

    totals_pallets = {sid: 0.0 for sid in source_ids}  # type: Dict[int, float]
    totals_boxes = {sid: 0.0 for sid in source_ids}  # type: Dict[int, float]
    for row in rows:
        try:
            sid = int(row["source_id"])
            qty = int(row["qty"] or 0)
        except (TypeError, ValueError):
            continue
        if sid not in totals_pallets or qty <= 0:
            continue
        article = str(row["article"] or "").strip()
        nm_id = str(row["nm_id"] or "").strip()
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

    summary = []  # type: List[Dict[str, Any]]
    for sid in source_ids:
        pallets = round(float(totals_pallets.get(sid) or 0.0) + 1e-12, 2)
        boxes = round(float(totals_boxes.get(sid) or 0.0) + 1e-12, 2)
        boxes_label = format_boxes_ru(boxes)
        pallets_label = format_pallets_ru(pallets)
        summary.append(
            {
                "source_id": sid,
                "name": source_names.get(sid) or "Источник {}".format(sid),
                "pallets": pallets,
                "boxes": boxes,
                "boxes_label": boxes_label,
                "pallets_label": "{} ({})".format(pallets_label, boxes_label),
            }
        )
    return summary
