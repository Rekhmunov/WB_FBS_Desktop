# -*- coding: utf-8 -*-
"""Supply sync must mark WB-delivered (done) supplies locally."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.db import Database
from app.services.orders import OrdersService
from app.wb.sync import sync_source


class SyncDoneSupplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
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
                VALUES (1, 'WB-GI-1', 'open', 0, '[]', '[]', '2024-01-01')
                """
            )
            conn.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_done_supply_upserted_and_leaves_assembly(self) -> None:
        client = MagicMock()
        client.get_orders_page.return_value = ([], None)
        client.get_supplies.return_value = (
            [
                {
                    "id": "WB-GI-1",
                    "name": "delivered",
                    "done": True,
                    "cargoType": 1,
                    "createdAt": "2024-01-01T00:00:00Z",
                    "closedAt": "2024-01-02T00:00:00Z",
                    "scanDt": None,
                }
            ],
            0,
        )
        client.get_supply_boxes.return_value = [{"id": "TRBX1"}]
        client.get_supply_order_ids.side_effect = AssertionError(
            "order ids must not be fetched for done supplies"
        )

        with patch("app.wb.sync.WbFbsClient", return_value=client), patch(
            "app.wb.sync.time.sleep"
        ):
            result = sync_source(self.db, 1, "token-key")

        self.assertFalse(result.get("stopped"))
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT done, name FROM wb_fbs_supplies WHERE supply_id = ?",
                ("WB-GI-1",),
            ).fetchone()
        self.assertEqual(int(row["done"]), 1)
        self.assertEqual(row["name"], "delivered")
        counts = OrdersService(self.db).tab_counts(1)
        self.assertEqual(counts["assembly"], 0)
        self.assertEqual(counts["delivery"], 1)
        client.get_supply_boxes.assert_called_once_with("WB-GI-1")
        client.get_supply_order_ids.assert_not_called()
