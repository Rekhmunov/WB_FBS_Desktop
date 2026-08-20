# -*- coding: utf-8 -*-
"""Disk cache for supply-open sticker numbers + order meta (survives restart)."""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.db import Database
from app.wb import utc_now


def _norm_ids(order_ids: Iterable[Any]) -> List[int]:
    out = []  # type: List[int]
    seen = set()  # type: set
    for raw in order_ids:
        try:
            oid = int(raw)
        except (TypeError, ValueError):
            continue
        if oid in seen:
            continue
        seen.add(oid)
        out.append(oid)
    return out


def load_many(
    db: Database, source_id: int, order_ids: Sequence[Any]
) -> Dict[int, Dict[str, Any]]:
    """Return cached rows keyed by order_id (may be partial / empty)."""
    ids = _norm_ids(order_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    sql = (
        "SELECT order_id, sticker_part_a, sticker_part_b, sticker_barcode, "
        "stickers_ready, meta_json, meta_ready "
        "FROM wb_fbs_order_open_cache "
        "WHERE source_id = ? AND order_id IN ({})".format(placeholders)
    )
    with db.connect() as conn:
        rows = conn.execute(sql, [int(source_id)] + ids).fetchall()
    out = {}  # type: Dict[int, Dict[str, Any]]
    for row in rows:
        oid = int(row["order_id"])
        meta = {}  # type: Dict[str, Any]
        try:
            parsed = json.loads(row["meta_json"] or "{}")
            if isinstance(parsed, dict):
                meta = parsed
        except Exception:
            meta = {}
        out[oid] = {
            "order_id": oid,
            "sticker_part_a": str(row["sticker_part_a"] or ""),
            "sticker_part_b": str(row["sticker_part_b"] or ""),
            "sticker_barcode": str(row["sticker_barcode"] or ""),
            "stickers_ready": bool(int(row["stickers_ready"] or 0)),
            "meta": meta,
            "meta_ready": bool(int(row["meta_ready"] or 0)),
        }
    return out


def stickers_from_cache(
    cached: Dict[int, Dict[str, Any]], order_ids: Sequence[Any]
) -> Dict[int, Dict[str, Any]]:
    """Map ready sticker cache rows to the sticker_numbers shape."""
    out = {}  # type: Dict[int, Dict[str, Any]]
    for oid in _norm_ids(order_ids):
        row = cached.get(oid) or {}
        if not row.get("stickers_ready"):
            continue
        out[oid] = {
            "partA": str(row.get("sticker_part_a") or ""),
            "partB": str(row.get("sticker_part_b") or ""),
            "barcode": str(row.get("sticker_barcode") or ""),
            "file_b64": "",
        }
    return out


def meta_from_cache(
    cached: Dict[int, Dict[str, Any]], order_ids: Sequence[Any]
) -> Dict[int, Dict[str, Any]]:
    out = {}  # type: Dict[int, Dict[str, Any]]
    for oid in _norm_ids(order_ids):
        row = cached.get(oid) or {}
        if not row.get("meta_ready"):
            continue
        meta = row.get("meta")
        out[oid] = dict(meta) if isinstance(meta, dict) else {}
    return out


def missing_sticker_ids(
    cached: Dict[int, Dict[str, Any]], order_ids: Sequence[Any]
) -> List[int]:
    return [
        oid
        for oid in _norm_ids(order_ids)
        if not (cached.get(oid) or {}).get("stickers_ready")
    ]


def missing_meta_ids(
    cached: Dict[int, Dict[str, Any]], order_ids: Sequence[Any]
) -> List[int]:
    return [
        oid
        for oid in _norm_ids(order_ids)
        if not (cached.get(oid) or {}).get("meta_ready")
    ]


def upsert_stickers(
    db: Database,
    source_id: int,
    stickers: Dict[int, Dict[str, Any]],
) -> None:
    if not stickers:
        return
    now = utc_now()
    sid = int(source_id)
    with db.connect() as conn:
        for oid, st in stickers.items():
            try:
                order_id = int(oid)
            except (TypeError, ValueError):
                continue
            part_a = str((st or {}).get("partA") or "").strip()
            part_b = str((st or {}).get("partB") or "").strip()
            barcode = str((st or {}).get("barcode") or "").strip()
            conn.execute(
                """
                INSERT INTO wb_fbs_order_open_cache (
                    source_id, order_id,
                    sticker_part_a, sticker_part_b, sticker_barcode,
                    stickers_ready, meta_json, meta_ready, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, '{}', 0, ?)
                ON CONFLICT(source_id, order_id) DO UPDATE SET
                    sticker_part_a = excluded.sticker_part_a,
                    sticker_part_b = excluded.sticker_part_b,
                    sticker_barcode = excluded.sticker_barcode,
                    stickers_ready = 1,
                    updated_at = excluded.updated_at
                """,
                (sid, order_id, part_a, part_b, barcode, now),
            )
        conn.commit()


def upsert_meta(
    db: Database,
    source_id: int,
    meta_by_id: Dict[int, Dict[str, Any]],
    *,
    order_ids: Optional[Sequence[Any]] = None,
) -> None:
    """Persist meta. Orders in ``order_ids`` with no meta still marked ready."""
    ids = _norm_ids(order_ids if order_ids is not None else meta_by_id.keys())
    if not ids:
        return
    now = utc_now()
    sid = int(source_id)
    with db.connect() as conn:
        for oid in ids:
            meta = meta_by_id.get(oid) if isinstance(meta_by_id.get(oid), dict) else {}
            payload = json.dumps(meta or {}, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO wb_fbs_order_open_cache (
                    source_id, order_id,
                    sticker_part_a, sticker_part_b, sticker_barcode,
                    stickers_ready, meta_json, meta_ready, updated_at
                ) VALUES (?, ?, '', '', '', 0, ?, 1, ?)
                ON CONFLICT(source_id, order_id) DO UPDATE SET
                    meta_json = excluded.meta_json,
                    meta_ready = 1,
                    updated_at = excluded.updated_at
                """,
                (sid, oid, payload, now),
            )
        conn.commit()


def clear_for_sources(db: Database, source_ids: Sequence[Any]) -> None:
    ids = _norm_ids(source_ids)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM wb_fbs_order_open_cache WHERE source_id IN ({})".format(
                placeholders
            ),
            ids,
        )
        conn.commit()


def clear_for_orders(
    db: Database, source_id: int, order_ids: Sequence[Any]
) -> None:
    ids = _norm_ids(order_ids)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM wb_fbs_order_open_cache WHERE source_id = ? "
            "AND order_id IN ({})".format(placeholders),
            [int(source_id)] + ids,
        )
        conn.commit()
