# -*- coding: utf-8 -*-
"""WB «В доставке» tab parity with web."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.db import Database
from app.services.orders import OrdersService
from app.wb import compute_tab, supply_status_label
from app.wb.sync import sync_source, upsert_supply


class ComputeTabDeliveryTests(unittest.TestCase):
    def test_complete_maps_to_delivery(self) -> None:
        self.assertEqual(
            compute_tab(supplier_status="complete", wb_status="", is_archive=False),
            "delivery",
        )

    def test_supply_status_label_web(self) -> None:
        self.assertEqual(supply_status_label(done=True, scan_dt=None), "Отгрузите поставку")
        self.assertEqual(
            supply_status_label(done=True, scan_dt="2026-01-02T00:00:00Z"),
            "Поставка в обработке",
        )


class DeliveryListedTests(unittest.TestCase):
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
            conn.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_migrate_pins_existing_done(self) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO wb_fbs_supplies
                (source_id, supply_id, name, done, order_ids_json, boxes_json, synced_at)
                VALUES (1, 'WB-GI-1', 'done', 1, '[]', '[]', '2024-01-01')
                """
            )
            conn.commit()
        # Re-run migrate path via init_schema
        self.db.init_schema()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT delivery_listed FROM wb_fbs_supplies WHERE supply_id='WB-GI-1'"
            ).fetchone()
        self.assertEqual(int(row["delivery_listed"]), 1)

    def test_upsert_done_sets_delivery_listed(self) -> None:
        upsert_supply(
            self.db,
            1,
            {
                "id": "WB-GI-2",
                "name": "deliv",
                "done": True,
                "cargoType": 1,
                "createdAt": "2024-01-01T00:00:00Z",
            },
            order_ids=[1],
            boxes=[],
        )
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT done, delivery_listed FROM wb_fbs_supplies WHERE supply_id='WB-GI-2'"
            ).fetchone()
        self.assertEqual(int(row["done"]), 1)
        self.assertEqual(int(row["delivery_listed"]), 1)

    def test_list_delivery_supplies(self) -> None:
        upsert_supply(
            self.db,
            1,
            {"id": "WB-GI-3", "name": "x", "done": True, "cargoType": 1},
            order_ids=[],
            boxes=[],
        )
        items, total = OrdersService(self.db).list_delivery_supplies(1)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["supply_id"], "WB-GI-3")

    def test_sync_pins_delivery_orders(self) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO wb_fbs_supplies
                (source_id, supply_id, name, done, delivery_listed,
                 order_ids_json, boxes_json, synced_at)
                VALUES (1, 'WB-GI-4', 'open-ish', 0, 0, '[101]', '[]', '2024-01-01')
                """
            )
            conn.execute(
                """
                INSERT INTO wb_fbs_orders
                (source_id, order_id, tab, supply_id, supplier_status, wb_status,
                 is_archive, synced_at)
                VALUES (1, 101, 'delivery', 'WB-GI-4', 'complete', 'waiting', 0, '2024-01-01')
                """
            )
            conn.commit()

        client = MagicMock()
        client.get_new_orders.return_value = []
        client.get_orders_page.return_value = ([], None)
        client.get_statuses.return_value = []
        client.get_supplies.return_value = ([], 0)

        with patch("app.wb.sync.WbFbsClient", return_value=client), patch(
            "app.wb.sync.time.sleep"
        ):
            sync_source(self.db, 1, "token")

        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT delivery_listed FROM wb_fbs_supplies WHERE supply_id='WB-GI-4'"
            ).fetchone()
        self.assertEqual(int(row["delivery_listed"]), 1)


if __name__ == "__main__":
    unittest.main()
