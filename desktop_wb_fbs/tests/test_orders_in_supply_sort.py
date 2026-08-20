# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import Database
from app.services.orders import OrdersService


class OrdersInSupplySortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.svc = OrdersService(self.db)
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO supply_sources (id, name, api_key, created_at)
                VALUES (1, 't', 'k', '2024-01-01')
                """
            )
            conn.execute(
                """
                INSERT INTO wb_fbs_supplies
                (source_id, supply_id, name, done, order_ids_json, boxes_json, synced_at)
                VALUES (1, 'WB-GI-1', 's', 0, ?, '[]', '2024-01-01')
                """,
                (json.dumps([30, 10, 20]),),
            )
            for oid, article in ((10, "zzz"), (20, "aaa"), (30, "mmm")):
                conn.execute(
                    """
                    INSERT INTO wb_fbs_orders
                    (source_id, order_id, supply_id, article, skus_json, synced_at)
                    VALUES (1, ?, 'WB-GI-1', ?, '[]', '2024-01-01')
                    """,
                    (oid, article),
                )
            conn.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_follows_wb_order_ids_sequence(self):
        with patch.object(self.svc, "_product_maps", return_value=({}, {}, {})):
            rows = self.svc.orders_in_supply(1, "WB-GI-1", api_key="")
        self.assertEqual([int(r["order_id"]) for r in rows], [30, 10, 20])


if __name__ == "__main__":
    unittest.main()
