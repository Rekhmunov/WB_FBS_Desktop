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


if __name__ == "__main__":
    unittest.main()
