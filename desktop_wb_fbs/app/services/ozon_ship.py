# -*- coding: utf-8 -*-
"""Ozon FBS ship (v4/posting/fbs/ship) — isolated from WB."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.ozon.sync import upsert_posting


def parse_products(raw: object) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    try:
        data = json.loads(str(raw or "[]"))
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict)]
    except Exception:
        pass
    return []


def build_ship_packages(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Single package with all products (default Ozon FBS assembly)."""
    pkg_products = []  # type: List[Dict[str, Any]]
    for prod in products:
        pid = prod.get("sku") or prod.get("product_id")
        if pid in (None, ""):
            continue
        qty = int(prod.get("quantity") or 1)
        pkg_products.append({"product_id": int(pid), "quantity": max(1, qty)})
    if not pkg_products:
        return []
    return [{"products": pkg_products}]


def posting_needs_ship(status: str) -> bool:
    return str(status or "").strip().lower() == "awaiting_packaging"


class OzonShipService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def ship_posting(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_number: str,
        *,
        posting: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        pnum = str(posting_number or "").strip()
        if not pnum:
            raise ValueError("posting_number required")
        data = posting
        if not data:
            data = client.get_posting(pnum)
        products = parse_products(data.get("products") if isinstance(data, dict) else [])
        if not products:
            products = parse_products(
                self._local_products(source_id, pnum)
            )
        packages = build_ship_packages(products)
        result = client.ship_posting(pnum, packages=packages or None)
        fresh = client.get_posting(pnum)
        if fresh:
            upsert_posting(self.db, source_id, fresh)
        return result

    def ship_postings(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
    ) -> Dict[str, Any]:
        ok = []  # type: List[str]
        errors = []  # type: List[str]
        for pnum in posting_numbers:
            pnum = str(pnum or "").strip()
            if not pnum:
                continue
            try:
                self.ship_posting(client, source_id, pnum)
                ok.append(pnum)
            except Exception as exc:
                errors.append("{}: {}".format(pnum, exc))
        return {"ok": ok, "errors": errors}

    def _local_products(self, source_id: int, posting_number: str) -> object:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT products_json FROM ozon_fbs_postings
                WHERE source_id = ? AND posting_number = ?
                """,
                (source_id, posting_number),
            ).fetchone()
        if not row:
            return []
        return row["products_json"]
