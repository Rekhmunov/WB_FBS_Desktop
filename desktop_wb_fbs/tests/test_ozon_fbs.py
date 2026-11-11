# -*- coding: utf-8 -*-
from __future__ import annotations

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
