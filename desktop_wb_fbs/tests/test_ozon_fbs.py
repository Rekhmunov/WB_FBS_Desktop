# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import unittest

from app.ozon import (
    TAB_ARBITRATION,
    TAB_ASSEMBLY,
    TAB_CANCELLED,
    TAB_FINISHED,
    TAB_NEW,
    carriage_is_done,
    compute_tab,
)


class OzonFbsHelpersTest(unittest.TestCase):
    def test_compute_tab_new_packaging(self) -> None:
        self.assertEqual(compute_tab(status="awaiting_packaging"), TAB_NEW)

    def test_compute_tab_assembly_deliver(self) -> None:
        self.assertEqual(compute_tab(status="awaiting_deliver"), TAB_ASSEMBLY)

    def test_compute_tab_carriage_ignored_like_web(self) -> None:
        # Web compute_tab is status-only; carriage does not force awaiting_deliver.
        self.assertEqual(
            compute_tab(status="awaiting_packaging", carriage_id="123"),
            TAB_NEW,
        )

    def test_compute_tab_cancelled(self) -> None:
        self.assertEqual(compute_tab(status="cancelled"), TAB_CANCELLED)

    def test_compute_tab_finished(self) -> None:
        self.assertEqual(compute_tab(status="delivered"), TAB_FINISHED)

    def test_compute_tab_client_arbitration(self) -> None:
        self.assertEqual(compute_tab(status="client_arbitration"), TAB_ARBITRATION)

    def test_carriage_is_done(self) -> None:
        self.assertTrue(carriage_is_done("approved"))
        self.assertTrue(carriage_is_done("cancelled"))
        self.assertFalse(carriage_is_done("new"))
        self.assertFalse(carriage_is_done("formed"))
        self.assertFalse(carriage_is_done("confirmed"))


class OzonCarriageClientParseTest(unittest.TestCase):
    def test_v2_delivery_list_shape(self) -> None:
        """v2/carriage/delivery/list returns methods[], not result[]."""
        payload = {
            "methods": [
                {
                    "delivery_method_id": 42,
                    "carriages": [{"id": 99, "status": "new", "postings_count": 3}],
                }
            ],
            "cursor": "next",
            "has_next": False,
        }
        methods = payload.get("methods") or []
        self.assertEqual(len(methods), 1)
        carriage = methods[0]["carriages"][0]
        self.assertEqual(str(carriage.get("id")), "99")
        self.assertEqual(carriage.get("postings_count"), 3)


class OzonPostingV4ClientTest(unittest.TestCase):
    """v3 list endpoints are gone — client must use v4 + cursor pages."""

    def test_postings_page_parses_flat_v4(self) -> None:
        from app.ozon.client import OzonFbsClient

        postings, has_next, cursor, count = OzonFbsClient._postings_page(
            {
                "postings": [{"posting_number": "PN-1"}],
                "has_next": True,
                "cursor": "cur-1",
                "count": 42,
            }
        )
        self.assertEqual(len(postings), 1)
        self.assertTrue(has_next)
        self.assertEqual(cursor, "cur-1")
        self.assertEqual(count, 42)

    def test_postings_page_legacy_result_wrap(self) -> None:
        from app.ozon.client import OzonFbsClient

        postings, has_next, cursor, count = OzonFbsClient._postings_page(
            {
                "result": {
                    "postings": [{"posting_number": "PN-2"}],
                    "has_next": False,
                    "cursor": "",
                    "count": 1,
                }
            }
        )
        self.assertEqual(postings[0]["posting_number"], "PN-2")
        self.assertFalse(has_next)
        self.assertEqual(cursor, "")
        self.assertEqual(count, 1)

    def test_list_postings_uses_v4_and_cursor(self) -> None:
        from unittest.mock import patch

        from app.ozon.client import OzonFbsClient

        client = OzonFbsClient("1", "k")
        calls = []

        def _fake(method, path, body=None):
            calls.append((method, path, dict(body or {})))
            if len(calls) == 1:
                return {
                    "postings": [{"posting_number": "A"}],
                    "has_next": True,
                    "cursor": "c1",
                }
            return {
                "postings": [{"posting_number": "B"}],
                "has_next": False,
                "cursor": "c2",
            }

        with patch.object(client, "_request", side_effect=_fake):
            page1, has_next, cursor = client.list_postings(
                since="2026-01-01T00:00:00Z",
                to="2026-01-02T00:00:00Z",
                limit=100,
            )
            page2, has_next2, cursor2 = client.list_postings(
                since="2026-01-01T00:00:00Z",
                to="2026-01-02T00:00:00Z",
                limit=100,
                cursor=cursor,
            )

        self.assertEqual(calls[0][1], "/v4/posting/fbs/list")
        self.assertNotIn("offset", calls[0][2])
        self.assertEqual([p["posting_number"] for p in page1], ["A"])
        self.assertTrue(has_next)
        self.assertEqual(cursor, "c1")
        self.assertEqual(calls[1][2].get("cursor"), "c1")
        self.assertEqual([p["posting_number"] for p in page2], ["B"])
        self.assertFalse(has_next2)
        self.assertEqual(cursor2, "c2")

    def test_list_unfulfilled_uses_v4_and_cursor(self) -> None:
        from unittest.mock import patch

        from app.ozon.client import OzonFbsClient

        client = OzonFbsClient("1", "k")
        calls = []

        def _fake(method, path, body=None):
            calls.append((path, dict(body or {})))
            return {
                "postings": [{"posting_number": "U1"}],
                "has_next": False,
                "cursor": "done",
                "count": 1,
            }

        with patch.object(client, "_request", side_effect=_fake):
            posts, count, cursor, has_next = client.list_unfulfilled(
                cutoff_from="2026-01-01T00:00:00Z",
                cutoff_to="2026-01-08T00:00:00Z",
                cursor="prev",
            )

        self.assertEqual(calls[0][0], "/v4/posting/fbs/unfulfilled/list")
        self.assertEqual(calls[0][1].get("cursor"), "prev")
        self.assertNotIn("offset", calls[0][1])
        self.assertEqual(posts[0]["posting_number"], "U1")
        self.assertEqual(count, 1)
        self.assertEqual(cursor, "done")
        self.assertFalse(has_next)

    def test_ping_uses_v4_list(self) -> None:
        from unittest.mock import patch

        from app.ozon.client import OzonFbsClient

        client = OzonFbsClient("1", "k")
        seen = []

        def _fake(method, path, body=None):
            seen.append(path)
            return {"postings": [], "has_next": False, "cursor": ""}

        with patch.object(client, "_request", side_effect=_fake):
            client.ping()
        self.assertEqual(seen, ["/v4/posting/fbs/list"])

    def test_sync_unfulfilled_walks_cursor(self) -> None:
        import os
        import tempfile
        from unittest.mock import MagicMock

        from app.db import Database
        from app.ozon.sync import sync_source

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            db = Database(path)
            db.init_schema()
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO supply_sources(
                        name, marketplace, client_id, api_key, is_enabled,
                        lookback_days, created_at
                    ) VALUES ('Озон', 'ozon', '1', 'k', 1, 2, '2026-01-01')
                    """
                )
                sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.commit()

            client = MagicMock()
            client.ping.return_value = {}
            client.list_unfulfilled.side_effect = [
                ([{"posting_number": "P1", "status": "awaiting_packaging",
                   "products": [{"offer_id": "o", "sku": "1", "name": "n", "quantity": 1}]}],
                 2, "c1", True),
                ([{"posting_number": "P2", "status": "awaiting_packaging",
                   "products": [{"offer_id": "o", "sku": "2", "name": "n", "quantity": 1}]}],
                 2, "c2", False),
            ]
            client.iter_postings_window.return_value = iter([])
            client.iter_carriage_delivery_methods.return_value = iter([])

            import app.ozon.sync as sync_mod

            original = sync_mod.OzonFbsClient
            sync_mod.OzonFbsClient = MagicMock(return_value=client)
            try:
                result = sync_source(db, sid, "1", "k", 2)
            finally:
                sync_mod.OzonFbsClient = original

            self.assertEqual(result.get("postings"), 2)
            self.assertEqual(result.get("errors"), [])
            self.assertEqual(client.list_unfulfilled.call_count, 2)
            first_kw = client.list_unfulfilled.call_args_list[0].kwargs
            second_kw = client.list_unfulfilled.call_args_list[1].kwargs
            self.assertEqual(first_kw.get("cursor"), "")
            self.assertEqual(second_kw.get("cursor"), "c1")
            with db.connect() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM ozon_fbs_postings WHERE source_id = ?",
                    (sid,),
                ).fetchone()["c"]
            self.assertEqual(int(n), 2)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

class OzonShipPackagesTest(unittest.TestCase):
    def test_build_ship_packages_multi(self) -> None:
        from app.services.ozon_ship import build_ship_packages

        products = [
            {"sku": 111, "quantity": 2},
            {"sku": 222, "quantity": 1},
        ]
        pkgs = build_ship_packages(products)
        self.assertEqual(len(pkgs), 1)
        self.assertEqual(len(pkgs[0]["products"]), 2)
        self.assertEqual(pkgs[0]["products"][0]["product_id"], 111)
        self.assertEqual(pkgs[0]["products"][0]["quantity"], 2)

    def test_act_get_postings_parses_objects(self) -> None:
        payload = {
            "result": [
                {"posting_number": "PN-1", "status": "awaiting_deliver"},
                {"posting_number": "PN-2", "status": "awaiting_deliver"},
            ]
        }
        result = payload.get("result") or []
        pnums = [
            str(x.get("posting_number"))
            for x in result
            if isinstance(x, dict) and x.get("posting_number")
        ]
        self.assertEqual(pnums, ["PN-1", "PN-2"])

    def test_posting_needs_ship(self) -> None:
        from app.services.ozon_ship import posting_needs_ship

        self.assertTrue(posting_needs_ship("awaiting_packaging"))
        self.assertFalse(posting_needs_ship("awaiting_deliver"))


class OzonPickServiceTest(unittest.TestCase):
    def test_save_pick_local(self) -> None:
        import os
        import tempfile

        from app.db import Database
        from app.services.ozon_mark_pick import OzonPickService

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            db = Database(path)
            db.init_schema()
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO supply_sources(
                        name, marketplace, client_id, api_key, is_enabled,
                        lookback_days, created_at
                    ) VALUES ('Ozon ФБС', 'ozon', '1', 'k', 1, 2, '2026-01-01')
                    """
                )
                sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO ozon_fbs_postings(
                        source_id, posting_number, status, tab, synced_at
                    ) VALUES (?, 'PN-1', 'awaiting_deliver', 'awaiting_deliver', '2026-01-01')
                    """,
                    (sid,),
                )
                conn.commit()
            svc = OzonPickService(db)
            svc.save(sid, "PN-1", True, "4601234567890")
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT pick_verified, pick_barcode FROM ozon_fbs_postings "
                    "WHERE source_id = ? AND posting_number = ?",
                    (sid, "PN-1"),
                ).fetchone()
            self.assertEqual(int(row["pick_verified"]), 1)
            self.assertEqual(str(row["pick_barcode"]), "4601234567890")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class OzonExemplarPayloadTest(unittest.TestCase):
    def test_build_exemplar_products_payload_multi_product(self) -> None:
        from app.services.ozon_mark_pick import _build_exemplar_products_payload

        exemplar_data = {
            "products": [
                {
                    "product_id": 111,
                    "exemplars": [{"exemplar_id": 1}, {"exemplar_id": 2}],
                },
                {
                    "product_id": 222,
                    "exemplars": [{"exemplar_id": 3}],
                },
            ]
        }
        payload = _build_exemplar_products_payload(
            exemplar_data, ["M1", "M2", "M3"], for_set=True
        )
        self.assertEqual(len(payload), 2)
        self.assertEqual(len(payload[0]["exemplars"]), 2)
        self.assertEqual(
            payload[0]["exemplars"][0]["marks"][0]["mark"],
            "M1",
        )
        self.assertEqual(
            payload[0]["exemplars"][1]["marks"][0]["mark"],
            "M2",
        )
        self.assertEqual(
            payload[1]["exemplars"][0]["marks"][0]["mark"],
            "M3",
        )

    def test_exemplar_sync_state_ok(self) -> None:
        from app.services.ozon_mark_pick import _exemplar_sync_state

        status = {
            "products": [
                {
                    "exemplars": [
                        {
                            "marks": [
                                {
                                    "mark": "CODE1",
                                    "check_status": "ok",
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        synced, pending, err = _exemplar_sync_state(status, ["CODE1"])
        self.assertTrue(synced)
        self.assertFalse(pending)
        self.assertEqual(err, "")


class OzonActResolveTest(unittest.TestCase):
    def test_extract_act_id_from_carriage(self) -> None:
        from app.services.ozon_act import _extract_act_id_from_carriage

        self.assertEqual(
            _extract_act_id_from_carriage({"act_id": 55}),
            "55",
        )
        self.assertEqual(
            _extract_act_id_from_carriage({"act": {"id": 77}}),
            "77",
        )
        self.assertEqual(_extract_act_id_from_carriage({"id": 99}), "")


class RunStartupGuardTest(unittest.TestCase):
    def test_friendly_hint_missing_module(self) -> None:
        import run as run_mod

        exc = ModuleNotFoundError("PyQt5")
        exc.name = "PyQt5"
        hint = run_mod._friendly_hint(exc)
        self.assertIn("PyQt5", hint)
        self.assertIn("pip install", hint)


if __name__ == "__main__":
    unittest.main()


class OzonLocalSuppliesTest(unittest.TestCase):
    def test_create_and_list_awaiting_deliver(self) -> None:
        import os
        import tempfile

        from app.db import Database
        from app.services.ozon_supplies import (
            OzonSuppliesService,
            default_supply_name,
        )

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            db = Database(path)
            db.init_schema()
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO supply_sources(
                        name, marketplace, client_id, api_key, is_enabled,
                        lookback_days, created_at
                    ) VALUES ('Озон ФБС', 'ozon', '1', 'k', 1, 2, '2026-01-01')
                    """
                )
                sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO ozon_fbs_postings(
                        source_id, posting_number, status, tab, warehouse_id,
                        warehouse_name, synced_at
                    ) VALUES
                    (?, 'PN-A', 'awaiting_packaging', 'awaiting_packaging',
                     10, 'WH-A', '2026-01-01'),
                    (?, 'PN-B', 'awaiting_packaging', 'awaiting_packaging',
                     10, 'WH-A', '2026-01-01')
                    """,
                    (sid, sid),
                )
                conn.commit()
            svc = OzonSuppliesService(db)
            preview = svc.preview_ship_all(sid)
            self.assertTrue(preview.get("ok"))
            self.assertEqual(int(preview.get("posting_count") or 0), 2)
            self.assertEqual(len(preview.get("groups") or []), 1)
            name = default_supply_name(source_name="Озон ФБС")
            self.assertIn("Озон ФБС", name)
            supply_id = svc.create_supply(
                sid,
                name=name,
                warehouse_id=10,
                warehouse_name="WH-A",
                posting_numbers=["PN-A", "PN-B"],
            )
            self.assertTrue(str(supply_id).startswith("OZ-FBS-"))
            listed = svc.list_awaiting_deliver_supplies(sid)
            self.assertEqual(len(listed), 1)
            self.assertEqual(int(listed[0].get("posting_count") or 0), 2)
            with db.connect() as conn:
                tabs = {
                    str(r["posting_number"]): str(r["tab"])
                    for r in conn.execute(
                        "SELECT posting_number, tab FROM ozon_fbs_postings "
                        "WHERE source_id = ?",
                        (sid,),
                    ).fetchall()
                }
            self.assertEqual(tabs["PN-A"], "awaiting_deliver")
            self.assertEqual(tabs["PN-B"], "awaiting_deliver")
            svc.mark_delivering(sid, supply_id)
            delivering = svc.list_delivering_supplies(sid)
            self.assertTrue(any(s.get("supply_id") == supply_id for s in delivering))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class OzonSplitHelpersTest(unittest.TestCase):
    def test_build_split_postings_qty(self) -> None:
        from app.services.ozon_split import build_split_postings, posting_unit_count

        row = {
            "products": [
                {"sku": 111, "quantity": 2},
                {"sku": 222, "quantity": 1},
            ]
        }
        self.assertEqual(posting_unit_count(row), 3)
        units = build_split_postings(row)
        self.assertEqual(len(units), 3)
        self.assertEqual(units[0]["products"][0]["quantity"], 1)

    def test_parse_split_response_list(self) -> None:
        from app.services.ozon_split import parse_split_response

        nums = parse_split_response(
            {"result": ["PN-1", "PN-1-1", "PN-1-2"]},
            fallback_posting_number="PN-1",
        )
        self.assertEqual(nums, ["PN-1", "PN-1-1", "PN-1-2"])

    def test_parse_split_parts_parent_postings(self) -> None:
        from app.services.ozon_split import build_split_postings, parse_split_parts

        plan = build_split_postings(
            {"products": [{"sku": 1, "quantity": 2}, {"sku": 2, "quantity": 1}]}
        )
        parts = parse_split_parts(
            {
                "result": {
                    "parent_posting": {"posting_number": "PN-1"},
                    "postings": [
                        {"posting_number": "PN-1"},
                        {"posting_number": "PN-1-1"},
                        {"posting_number": "PN-1-2"},
                    ],
                }
            },
            fallback_posting_number="PN-1",
            split_plan=plan,
        )
        self.assertEqual([pn for pn, _ in parts], ["PN-1", "PN-1-1", "PN-1-2"])

    def test_parse_split_parts_raises_when_siblings_missing(self) -> None:
        from app.services.ozon_split import build_split_postings, parse_split_parts

        plan = build_split_postings({"products": [{"sku": 1, "quantity": 3}]})
        with self.assertRaises(RuntimeError):
            parse_split_parts(
                {"result": {"parent_posting": "PN-1", "postings": []}},
                fallback_posting_number="PN-1",
                split_plan=plan,
            )


class OzonShipStatusParityTest(unittest.TestCase):
    def test_posting_needs_ship_by_tab(self) -> None:
        from app.services.ozon_ship import posting_needs_ship

        self.assertTrue(posting_needs_ship(status="awaiting_packaging"))
        self.assertTrue(posting_needs_ship(tab="awaiting_packaging"))
        self.assertFalse(posting_needs_ship(tab="awaiting_deliver"))
        self.assertFalse(posting_needs_ship(status="awaiting_deliver"))

    def test_ship_forces_awaiting_deliver_when_get_stale(self) -> None:
        import os
        import tempfile
        from unittest.mock import MagicMock

        from app.db import Database
        from app.services.ozon_ship import OzonShipService

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            db = Database(path)
            db.init_schema()
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO supply_sources(
                        name, marketplace, client_id, api_key, is_enabled,
                        lookback_days, created_at
                    ) VALUES ('Озон', 'ozon', '1', 'k', 1, 2, '2026-01-01')
                    """
                )
                sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO ozon_fbs_postings(
                        source_id, posting_number, status, tab, products_json, synced_at
                    ) VALUES (?, 'PN-1', 'awaiting_packaging', 'awaiting_packaging',
                              ?, '2026-01-01')
                    """,
                    (sid, json.dumps([{"sku": 111, "quantity": 1}])),
                )
                conn.commit()

            client = MagicMock()
            client.ship_posting.return_value = {"result": ["PN-1"]}
            # Stale get still returns packaging — must force awaiting_deliver.
            client.get_posting.return_value = {
                "posting_number": "PN-1",
                "status": "awaiting_packaging",
                "products": [{"sku": 111, "quantity": 1}],
            }
            svc = OzonShipService(db)
            out = svc.ship_posting(client, sid, "PN-1", fast=False)
            self.assertTrue(out.get("ok"))
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT status, tab FROM ozon_fbs_postings "
                    "WHERE source_id = ? AND posting_number = 'PN-1'",
                    (sid,),
                ).fetchone()
            self.assertEqual(row["status"], "awaiting_deliver")
            self.assertEqual(row["tab"], "awaiting_deliver")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_ship_already_assembled_counts_ok(self) -> None:
        import os
        import tempfile
        from unittest.mock import MagicMock

        from app.db import Database
        from app.services.ozon_ship import OzonShipService

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            db = Database(path)
            db.init_schema()
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO supply_sources(
                        name, marketplace, client_id, api_key, is_enabled,
                        lookback_days, created_at
                    ) VALUES ('Озон', 'ozon', '1', 'k', 1, 2, '2026-01-01')
                    """
                )
                sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO ozon_fbs_postings(
                        source_id, posting_number, status, tab, products_json, synced_at
                    ) VALUES (?, 'PN-2', 'awaiting_packaging', 'awaiting_packaging',
                              ?, '2026-01-01')
                    """,
                    (sid, json.dumps([{"sku": 1, "quantity": 1}])),
                )
                conn.commit()
            client = MagicMock()
            client.get_posting.return_value = {
                "posting_number": "PN-2",
                "status": "awaiting_packaging",
                "products": [{"sku": 1, "quantity": 1}],
            }
            client.ship_posting.side_effect = RuntimeError(
                "POSTING_ALREADY_SHIPPED awaiting_deliver"
            )
            out = OzonShipService(db).ship_posting(client, sid, "PN-2", fast=True)
            self.assertTrue(out.get("ok"))
            self.assertTrue(out.get("already"))
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT tab FROM ozon_fbs_postings WHERE posting_number='PN-2'"
                ).fetchone()
            self.assertEqual(row["tab"], "awaiting_deliver")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class OzonSelectionPreviewTest(unittest.TestCase):
    def test_preview_rejects_wrong_tab_and_existing_supply(self) -> None:
        import os
        import tempfile

        from app.db import Database
        from app.services.ozon_supplies import OzonSuppliesService

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            db = Database(path)
            db.init_schema()
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO supply_sources(
                        name, marketplace, client_id, api_key, is_enabled,
                        lookback_days, created_at
                    ) VALUES ('Озон ФБС', 'ozon', '1', 'k', 1, 2, '2026-01-01')
                    """
                )
                sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO ozon_fbs_postings(
                        source_id, posting_number, status, tab, warehouse_id,
                        synced_at
                    ) VALUES
                    (?, 'OK-1', 'awaiting_packaging', 'awaiting_packaging', 1, '2026-01-01'),
                    (?, 'BAD-TAB', 'awaiting_deliver', 'awaiting_deliver', 1, '2026-01-01'),
                    (?, 'IN-SUP', 'awaiting_packaging', 'awaiting_packaging', 1, '2026-01-01')
                    """,
                    (sid, sid, sid),
                )
                conn.execute(
                    "UPDATE ozon_fbs_postings SET supply_id = 'OZ-X' "
                    "WHERE posting_number = 'IN-SUP'"
                )
                conn.commit()
            svc = OzonSuppliesService(db)
            bad = svc.preview_selection_supply(
                sid, ["OK-1", "BAD-TAB", "IN-SUP", "MISSING"]
            )
            self.assertFalse(bad.get("ok"))
            joined = " | ".join(bad.get("errors") or [])
            self.assertIn("Ожидают сборки", joined)
            self.assertIn("локальной поставке", joined)
            self.assertIn("Не найдены", joined)
            good = svc.preview_selection_supply(sid, ["OK-1"])
            self.assertTrue(good.get("ok"))
            self.assertEqual(good.get("order_count"), 1)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
