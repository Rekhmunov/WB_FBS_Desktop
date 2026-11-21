# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.ozon import utc_now
from app.services.ozon_supplies import OzonSuppliesService


class OzonSupplyParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.svc = OzonSuppliesService(self.db)
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO supply_sources(
                    name, marketplace, client_id, api_key, is_enabled,
                    lookback_days, created_at
                ) VALUES ('Озон', 'ozon', '1', 'k', 1, 2, '2026-01-01')
                """
            )
            self.source_id = int(
                conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
            now = utc_now()
            for pn, art in (("A-1", "ART-A"), ("A-2", "ART-A"), ("B-1", "ART-B")):
                conn.execute(
                    """
                    INSERT INTO ozon_fbs_postings(
                        source_id, posting_number, status, tab, offer_id,
                        product_name, warehouse_id, warehouse_name, synced_at
                    ) VALUES (?, ?, 'awaiting_deliver', 'awaiting_deliver',
                              ?, ?, 10, 'WH', ?)
                    """,
                    (self.source_id, pn, art, "Товар {}".format(pn), now),
                )
            conn.commit()
        self.sid = self.svc.create_supply(
            self.source_id,
            name="Main",
            warehouse_id=10,
            warehouse_name="WH",
            posting_numbers=["A-1", "A-2", "B-1"],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sticker_groups_by_article(self) -> None:
        groups = self.svc.sticker_groups_by_article(self.source_id, self.sid)
        by_key = {g["group_key"]: g for g in groups}
        self.assertIn("ART-A", by_key)
        self.assertIn("ART-B", by_key)
        self.assertEqual(int(by_key["ART-A"]["qty"]), 2)
        self.assertEqual(len(by_key["ART-A"]["posting_numbers"]), 2)

    def test_local_move_create_and_move_back(self) -> None:
        created = self.svc.create_local_supply_from_postings(
            self.source_id, ["A-1"], name="Part"
        )
        new_id = str(created["supply_id"])
        self.assertEqual(int(created["moved"]), 1)
        detail = self.svc.get_detail(self.source_id, self.sid)
        pns = {o["posting_number"] for o in detail["orders"]}
        self.assertNotIn("A-1", pns)
        self.svc.mark_delivering(self.source_id, new_id)
        delivering = {
            str(s["supply_id"])
            for s in self.svc.list_delivering_supplies(self.source_id)
        }
        self.assertIn(new_id, delivering)
        back = self.svc.move_to_awaiting_deliver(self.source_id, new_id)
        self.assertTrue(back.get("ok"))
        supply = self.svc.get_supply(self.source_id, new_id)
        self.assertEqual(int(supply.get("delivering_listed") or 0), 0)


if __name__ == "__main__":
    unittest.main()
