# -*- coding: utf-8 -*-
"""Ozon FBS sticker bind + scan lookup (web ozon_fbs_stickers parity, desktop)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.ozon.sync import upsert_posting
from app.services.ozon_labels import OzonLabelService
from app.services.ozon_supplies import OzonSuppliesService

_log = logging.getLogger(__name__)
_MATCH_LIMIT = 50


def normalize_sticker_scan(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Drop zero-width / BOM noise from scanners.
    text = text.replace("\ufeff", "").replace("\u200b", "")
    return text.strip()


def _scan_key(value: object) -> str:
    return normalize_sticker_scan(value).casefold()


class OzonStickersService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.labels = OzonLabelService(db)
        self.supplies = OzonSuppliesService(db)

    def find_by_scan(
        self, source_id: int, scan: str
    ) -> Dict[str, Any]:
        raw = normalize_sticker_scan(scan)
        if not raw:
            return {"row": None, "ambiguous": False, "matches": []}
        raw_key = _scan_key(raw)
        matches = []  # type: List[Dict[str, Any]]
        with self.db.connect() as conn:
            # Exact sticker_barcode
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_postings
                WHERE source_id = ? AND sticker_barcode != ''
                  AND LOWER(sticker_barcode) = ?
                LIMIT ?
                """,
                (source_id, raw_key, _MATCH_LIMIT),
            ).fetchall()
            matches = [Database.row_to_dict(r) for r in rows]
            if not matches:
                rows = conn.execute(
                    """
                    SELECT * FROM ozon_fbs_postings
                    WHERE source_id = ? AND sticker_lower_barcode != ''
                      AND LOWER(sticker_lower_barcode) = ?
                    LIMIT ?
                    """,
                    (source_id, raw_key, _MATCH_LIMIT),
                ).fetchall()
                matches = [Database.row_to_dict(r) for r in rows]
            if not matches:
                # posting_number exact
                rows = conn.execute(
                    """
                    SELECT * FROM ozon_fbs_postings
                    WHERE source_id = ? AND LOWER(posting_number) = ?
                    LIMIT ?
                    """,
                    (source_id, raw.casefold(), _MATCH_LIMIT),
                ).fetchall()
                matches = [Database.row_to_dict(r) for r in rows]
            if not matches and len(raw) >= 8:
                # Fallback: barcodes_json / raw_json contain (web parity)
                like = "%{}%".format(raw)
                rows = conn.execute(
                    """
                    SELECT * FROM ozon_fbs_postings
                    WHERE source_id = ?
                      AND (barcodes_json LIKE ? OR raw_json LIKE ?)
                    LIMIT ?
                    """,
                    (source_id, like, like, _MATCH_LIMIT),
                ).fetchall()
                for r in rows:
                    d = Database.row_to_dict(r)
                    if self._row_matches(d, raw, raw_key):
                        matches.append(d)
        if len(matches) == 1:
            return {"row": matches[0], "ambiguous": False, "matches": matches}
        if len(matches) > 1:
            return {"row": None, "ambiguous": True, "matches": matches}
        return {"row": None, "ambiguous": False, "matches": []}

    def _row_matches(
        self, row: Dict[str, Any], raw: str, raw_key: str
    ) -> bool:
        for key in ("sticker_barcode", "sticker_lower_barcode"):
            val = str(row.get(key) or "").strip()
            if val and _scan_key(val) == raw_key:
                return True
        try:
            import json

            bcs = json.loads(str(row.get("barcodes_json") or "[]"))
        except Exception:
            bcs = []
        if isinstance(bcs, list):
            for b in bcs:
                if _scan_key(b) == raw_key:
                    return True
        pn = str(row.get("posting_number") or "").strip()
        if pn and pn.casefold() == raw.casefold():
            return True
        # raw_json may hold package barcodes before columns are backfilled
        blob = str(row.get("raw_json") or "")
        if raw and raw in blob:
            return True
        if raw_key and raw_key in blob.casefold():
            return True
        return False

    def bind_after_labels(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
        *,
        overwrite: bool = True,
    ) -> int:
        """Re-fetch get_posting barcodes after label print (web post-print bind)."""
        nums = [str(p).strip() for p in posting_numbers if str(p).strip()]
        updated = 0
        for pn in nums:
            try:
                fresh = client.get_posting(pn)
            except Exception as exc:
                _log.warning("ozon sticker bind get %s: %s", pn, exc)
                continue
            if not isinstance(fresh, dict):
                continue
            if not overwrite:
                with self.db.connect() as conn:
                    row = conn.execute(
                        """
                        SELECT sticker_barcode FROM ozon_fbs_postings
                        WHERE source_id = ? AND posting_number = ?
                        """,
                        (source_id, pn),
                    ).fetchone()
                if row and str(row["sticker_barcode"] or "").strip():
                    continue
            upsert_posting(self.db, source_id, fresh)
            updated += 1
        return updated

    def print_stickers(
        self,
        client: OzonFbsClient,
        source_id: int,
        supply_id: str,
        posting_numbers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Fetch Ozon label PDF, bind QR, build HTML sheet — desktop sticker job."""
        detail = self.supplies.get_detail(source_id, supply_id)
        orders = list(detail.get("orders") or [])
        if posting_numbers is not None:
            allow = {str(x).strip() for x in posting_numbers if str(x).strip()}
            orders = [
                o
                for o in orders
                if str(o.get("posting_number") or "").strip() in allow
            ]
        orders = [
            o
            for o in orders
            if str(o.get("tab") or "").lower()
            in ("awaiting_packaging", "awaiting_deliver", "delivering", "new", "assembly")
            and str(o.get("status") or "").lower()
            not in ("cancelled", "not_accepted")
        ]
        nums = [
            str(o.get("posting_number") or "").strip()
            for o in orders
            if str(o.get("posting_number") or "").strip()
        ]
        if not nums:
            raise RuntimeError(
                "Нет отправлений для печати этикеток. "
                "Этикетки доступны после сборки (ship)."
            )
        # Pre-enrich empty stickers
        try:
            self.supplies.bind_stickers_from_postings(client, source_id, nums)
        except Exception as exc:
            _log.warning("ozon stickers pre-enrich: %s", exc)
        pdf_path = self.labels.fetch_labels(
            client, source_id, nums, use_cache=False
        )
        # Post-print bind (overwrite) so scan matches PDF
        bound = self.bind_after_labels(
            client, source_id, nums, overwrite=True
        )
        detail = self.supplies.get_detail(source_id, supply_id)
        if posting_numbers is not None:
            allow = set(nums)
            detail = dict(detail)
            detail["orders"] = [
                o
                for o in (detail.get("orders") or [])
                if str(o.get("posting_number") or "").strip() in allow
            ]
        sheet_html = self.supplies.render_stickers_sheet_html(detail)
        # Embed PDF open hint into sheet
        sheet_html = sheet_html.replace(
            "PDF этикеток Ozon печатайте отдельно кнопкой «Этикетки»",
            "PDF этикеток Ozon открыт рядом · привязано QR: {}".format(bound),
        )
        return {
            "ok": True,
            "pdf_path": pdf_path,
            "sheet_html": sheet_html,
            "posting_count": len(nums),
            "bound": bound,
            "message": "Этикетки PDF: {} · QR привязано: {}".format(
                len(nums), bound
            ),
        }
