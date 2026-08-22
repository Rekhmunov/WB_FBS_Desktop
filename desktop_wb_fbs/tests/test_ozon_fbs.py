# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from app.ozon import (
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

    def test_compute_tab_carriage_forces_assembly(self) -> None:
        self.assertEqual(
            compute_tab(status="awaiting_packaging", carriage_id="123"),
            TAB_ASSEMBLY,
        )

    def test_compute_tab_cancelled(self) -> None:
        self.assertEqual(compute_tab(status="cancelled"), TAB_CANCELLED)

    def test_compute_tab_finished(self) -> None:
        self.assertEqual(compute_tab(status="delivered"), TAB_FINISHED)

    def test_compute_tab_client_arbitration(self) -> None:
        self.assertEqual(compute_tab(status="client_arbitration"), TAB_ASSEMBLY)

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
                    ) VALUES (?, 'PN-1', 'awaiting_deliver', 'assembly', '2026-01-01')
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


if __name__ == "__main__":
    unittest.main()
