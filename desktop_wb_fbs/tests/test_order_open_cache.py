# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.db import Database
from app.services import order_open_cache, supply_session
from app.services.orders import OrdersService


class OrderOpenCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stickers_and_meta_roundtrip(self) -> None:
        order_open_cache.upsert_stickers(
            self.db,
            1,
            {10: {"partA": "AAA", "partB": "1234", "barcode": "BC"}},
        )
        order_open_cache.upsert_meta(
            self.db,
            1,
            {10: {"id": 10, "sgtin": ["x"]}, 11: {}},
            order_ids=[10, 11],
        )
        cached = order_open_cache.load_many(self.db, 1, [10, 11, 12])
        self.assertTrue(cached[10]["stickers_ready"])
        self.assertEqual(cached[10]["sticker_part_a"], "AAA")
        self.assertTrue(cached[10]["meta_ready"])
        self.assertEqual(cached[10]["meta"].get("sgtin"), ["x"])
        self.assertTrue(cached[11]["meta_ready"])
        self.assertFalse(cached[11].get("stickers_ready"))
        self.assertEqual(order_open_cache.missing_sticker_ids(cached, [10, 11]), [11])
        self.assertEqual(order_open_cache.missing_meta_ids(cached, [10, 11, 12]), [12])

    def test_clear_for_sources(self) -> None:
        order_open_cache.upsert_stickers(
            self.db, 1, {1: {"partA": "A", "partB": "1", "barcode": ""}}
        )
        order_open_cache.upsert_stickers(
            self.db, 2, {2: {"partA": "B", "partB": "2", "barcode": ""}}
        )
        order_open_cache.clear_for_sources(self.db, [1])
        self.assertEqual(order_open_cache.load_many(self.db, 1, [1]), {})
        self.assertTrue(order_open_cache.load_many(self.db, 2, [2])[2]["stickers_ready"])


class PreloadUsesDiskCacheTests(unittest.TestCase):
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
            conn.execute(
                """
                INSERT INTO wb_fbs_supplies
                (source_id, supply_id, name, done, order_ids_json, boxes_json, synced_at)
                VALUES (1, 'WB-1', 's', 0, ?, '[]', '2024-01-01')
                """,
                (json.dumps([10]),),
            )
            conn.execute(
                """
                INSERT INTO wb_fbs_orders
                (source_id, order_id, supply_id, article, skus_json, synced_at)
                VALUES (1, 10, 'WB-1', 'art', '[]', '2024-01-01')
                """
            )
            conn.commit()
        order_open_cache.upsert_stickers(
            self.db,
            1,
            {10: {"partA": "PA", "partB": "9999", "barcode": "BC10"}},
        )
        order_open_cache.upsert_meta(
            self.db, 1, {10: {"id": 10}}, order_ids=[10]
        )

    def tearDown(self) -> None:
        supply_session.clear_all_sessions()
        self.tmp.cleanup()

    def test_second_preload_skips_network(self) -> None:
        with patch.object(self.orders, "_product_maps", return_value=({}, {}, {})):
            with patch(
                "app.services.print_docs._fetch_picking_stickers"
            ) as fetch_st:
                with patch(
                    "app.services.supply_session.fetch_orders_meta"
                ) as fetch_meta:
                    session = supply_session.preload_supply_core(
                        self.db,
                        self.orders,
                        1,
                        "WB-1",
                        "key",
                    )
        fetch_st.assert_not_called()
        fetch_meta.assert_not_called()
        self.assertEqual(session.sticker_numbers[10]["partB"], "9999")
        self.assertEqual(session.meta_by_id[10].get("id"), 10)

    def test_force_network_refetches(self) -> None:
        with patch.object(self.orders, "_product_maps", return_value=({}, {}, {})):
            with patch(
                "app.services.print_docs._fetch_picking_stickers",
                return_value={
                    10: {"partA": "NX", "partB": "1111", "barcode": "NEW"},
                },
            ) as fetch_st:
                with patch(
                    "app.services.supply_session.fetch_orders_meta",
                    return_value={10: {"id": 10, "sgtin": []}},
                ) as fetch_meta:
                    session = supply_session.preload_supply_core(
                        self.db,
                        self.orders,
                        1,
                        "WB-1",
                        "key",
                        force_network=True,
                    )
        fetch_st.assert_called_once()
        fetch_meta.assert_called_once()
        self.assertEqual(session.sticker_numbers[10]["partB"], "1111")
        cached = order_open_cache.load_many(self.db, 1, [10])
        self.assertEqual(cached[10]["sticker_part_b"], "1111")


if __name__ == "__main__":
    unittest.main()
