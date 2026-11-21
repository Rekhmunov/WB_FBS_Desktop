# -*- coding: utf-8 -*-
"""Ozon FBS ship (v4/posting/fbs/ship) — web parity (awaiting_packaging → awaiting_deliver)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from app.db import Database
from app.ozon import (
    TAB_AWAITING_DELIVER,
    TAB_AWAITING_PACKAGING,
    TAB_DELIVERED,
    TAB_DELIVERING,
    compute_tab,
    utc_now,
)
from app.ozon.client import OzonFbsClient
from app.ozon.sync import upsert_posting

_log = logging.getLogger(__name__)

_PAST_PACKAGING_TABS = frozenset(
    {TAB_AWAITING_DELIVER, TAB_DELIVERING, TAB_DELIVERED}
)


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
    """Single package with all products (default Ozon FBS assembly).

    Multi-unit orders must be split earlier via ``/v1/posting/fbs/split``.
    """
    pkg_products = []  # type: List[Dict[str, Any]]
    for prod in products:
        pid = prod.get("sku") if prod.get("sku") is not None else prod.get("product_id")
        if pid in (None, ""):
            continue
        try:
            qty = int(prod.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        pkg_products.append({"product_id": int(pid), "quantity": max(1, qty)})
    if not pkg_products:
        return []
    return [{"products": pkg_products}]


def posting_needs_ship(status: str = "", tab: str = "") -> bool:
    """True when posting is still on packaging / awaiting assembly."""
    tab_l = str(tab or "").strip().lower()
    if tab_l in _PAST_PACKAGING_TABS:
        return False
    if tab_l in ("new", TAB_AWAITING_PACKAGING):
        return True
    st = str(status or "").strip().lower()
    if st in _PAST_PACKAGING_TABS:
        return False
    return st in ("", TAB_AWAITING_PACKAGING) or compute_tab(status=st) == TAB_AWAITING_PACKAGING


def _ship_error_already_assembled(exc: BaseException) -> bool:
    text = str(exc or "").lower()
    needles = (
        "already",
        "уже собран",
        "awaiting_deliver",
        "has already been",
        "posting_already",
        "assembled",
    )
    return any(n in text for n in needles)


def _force_local_awaiting_deliver(
    db: Database,
    source_id: int,
    posting_number: str,
    *,
    posting: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist awaiting_deliver without relying on Ozon get eventual consistency.

    Prefer a full upsert when the payload has products; otherwise only flip
    status/tab so we never blank out name/SKU after «already assembled».
    """
    pn = str(posting_number or "").strip()
    if not pn:
        return
    payload = dict(posting or {}) if isinstance(posting, dict) else {}
    products = payload.get("products")
    has_products = isinstance(products, list) and any(
        isinstance(p, dict) for p in products
    )
    if has_products:
        payload["posting_number"] = pn
        payload["status"] = TAB_AWAITING_DELIVER
        try:
            upsert_posting(db, source_id, payload)
        except Exception:
            pass
    # Hard-set tab/status — always, even for product-less force paths.
    now = utc_now()
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE ozon_fbs_postings
            SET status = ?, tab = ?, synced_at = ?
            WHERE source_id = ? AND posting_number = ?
            """,
            (TAB_AWAITING_DELIVER, TAB_AWAITING_DELIVER, now, source_id, pn),
        )
        conn.commit()


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
        fast: bool = False,
    ) -> Dict[str, Any]:
        """Ship one posting; local status becomes ``awaiting_deliver`` (web parity).

        ``fast=True`` (bulk collect): prefer local products, skip post-ship get,
        treat «already assembled» as success.
        """
        pnum = str(posting_number or "").strip()
        if not pnum:
            raise ValueError("posting_number required")

        local = posting if isinstance(posting, dict) else None
        if local is None:
            local = self._local_row(source_id, pnum)

        local_tab = str((local or {}).get("tab") or "").strip().lower()
        if local_tab in _PAST_PACKAGING_TABS:
            return {
                "ok": True,
                "skipped": True,
                "already": True,
                "posting_numbers": [pnum],
            }

        remote = {}  # type: Dict[str, Any]
        products = parse_products((local or {}).get("products") if local else None)
        if not products and local:
            products = parse_products(local.get("products_json"))
        if not fast or not products:
            try:
                remote = client.get_posting(pnum) or {}
            except Exception as exc:
                _log.warning("ozon ship get_posting %s: %s", pnum, exc)
                remote = {}
            if remote:
                products = parse_products(remote.get("products")) or products
        if not products:
            products = parse_products(self._local_products(source_id, pnum))

        packages = build_ship_packages(products)
        if not packages:
            raise RuntimeError("Нет товаров для сборки отправления")

        try:
            result = client.ship_posting(pnum, packages=packages)
        except Exception as exc:
            if _ship_error_already_assembled(exc):
                _force_local_awaiting_deliver(
                    self.db, source_id, pnum, posting=remote or local
                )
                return {
                    "ok": True,
                    "already": True,
                    "skipped": True,
                    "posting_numbers": [pnum],
                    "error": str(exc),
                }
            err_l = str(exc).lower()
            if "429" in err_l or "http 5" in err_l or "network" in err_l:
                time.sleep(1.5)
                try:
                    result = client.ship_posting(pnum, packages=packages)
                except Exception as exc2:
                    if _ship_error_already_assembled(exc2):
                        _force_local_awaiting_deliver(
                            self.db, source_id, pnum, posting=remote or local
                        )
                        return {
                            "ok": True,
                            "already": True,
                            "skipped": True,
                            "posting_numbers": [pnum],
                            "error": str(exc2),
                        }
                    # Confirm remote may already be past packaging.
                    try:
                        got = client.get_posting(pnum) or {}
                    except Exception:
                        got = {}
                    got_tab = compute_tab(status=str((got or {}).get("status") or ""))
                    if got_tab in _PAST_PACKAGING_TABS:
                        _force_local_awaiting_deliver(
                            self.db, source_id, pnum, posting=got
                        )
                        return {
                            "ok": True,
                            "already": True,
                            "skipped": True,
                            "posting_numbers": [pnum],
                        }
                    raise
            else:
                try:
                    got = client.get_posting(pnum) or {}
                except Exception:
                    got = {}
                got_tab = compute_tab(status=str((got or {}).get("status") or ""))
                if got_tab in _PAST_PACKAGING_TABS:
                    _force_local_awaiting_deliver(
                        self.db, source_id, pnum, posting=got
                    )
                    return {
                        "ok": True,
                        "already": True,
                        "skipped": True,
                        "posting_numbers": [pnum],
                    }
                raise

        base = remote or local or {}
        if fast:
            _force_local_awaiting_deliver(
                self.db, source_id, pnum, posting=base if isinstance(base, dict) else None
            )
            return {"ok": True, "result": result, "posting_numbers": [pnum]}

        # Interactive: refresh from Ozon, but never leave packaging if get is stale.
        refreshed = {}  # type: Dict[str, Any]
        try:
            got = client.get_posting(pnum)
            if isinstance(got, dict):
                refreshed = dict(got)
        except Exception:
            refreshed = {}
        if not refreshed:
            refreshed = dict(base) if isinstance(base, dict) else {}
            refreshed["posting_number"] = pnum
        remote_tab = compute_tab(status=str(refreshed.get("status") or ""))
        if remote_tab not in _PAST_PACKAGING_TABS:
            refreshed["status"] = TAB_AWAITING_DELIVER
        upsert_posting(self.db, source_id, refreshed)
        _force_local_awaiting_deliver(
            self.db, source_id, pnum, posting=refreshed
        )
        return {"ok": True, "result": result, "posting_numbers": [pnum]}

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

    def _local_row(self, source_id: int, posting_number: str) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM ozon_fbs_postings
                WHERE source_id = ? AND posting_number = ?
                """,
                (source_id, posting_number),
            ).fetchone()
        return Database.row_to_dict(row) if row else {}

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
