# -*- coding: utf-8 -*-
"""Supply open must not hang on WB when local order links exist; refresh done."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.db import Database
from app.services.orders import OrdersService


class SupplyOpenDoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.orders = OrdersService(self.db)
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO supply_sources (id, name, api_key, created_at)
                VALUES (1, 't', 'k', '2024-01-01')
                """
            )
            # Stuck on «На сборке»: local done=0, empty order_ids_json, but orders linked.
            conn.execute(
                """
                INSERT INTO wb_fbs_supplies
                (source_id, supply_id, name, done, order_ids_json, boxes_json, synced_at)
                VALUES (1, 'WB-GI-1', 'stuck', 0, '[]', '[]', '2024-01-01')
                """
            )
            conn.execute(
                """
                INSERT INTO wb_fbs_orders
                (source_id, order_id, supply_id, article, skus_json, tab,
                 supplier_status, synced_at)
                VALUES (1, 101, 'WB-GI-1', 'A', '[]', 'assembly', 'confirm', '2024-01-01')
                """
            )
            conn.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ensure_uses_local_links_without_wb(self) -> None:
        client = MagicMock()
        with patch("app.services.orders.WbFbsClient", return_value=client):
            oids = self.orders.ensure_supply_order_ids(1, "key", "WB-GI-1")
        self.assertEqual(oids, [101])
        client.get_supply_order_ids.assert_not_called()
        client.get_supply_boxes.assert_not_called()

    def test_orders_in_supply_refreshes_done_and_lists_rows(self) -> None:
        client = MagicMock()
        client.get_supply.return_value = {
            "id": "WB-GI-1",
            "name": "delivered",
            "done": True,
            "cargoType": 1,
            "closedAt": "2024-01-02T00:00:00Z",
            "scanDt": None,
        }
        with patch("app.services.orders.WbFbsClient", return_value=client), patch(
            "app.services.orders.time.sleep"
        ):
            rows = self.orders.orders_in_supply(1, "WB-GI-1", api_key="key")
        self.assertEqual([int(r["order_id"]) for r in rows], [101])
        supply = self.orders.get_supply(1, "WB-GI-1")
        self.assertTrue(supply and supply.get("done"))
        self.assertEqual(supply.get("name"), "delivered")
        # Local links satisfied ensure — no order-ids storm on open.
        client.get_supply_order_ids.assert_not_called()
