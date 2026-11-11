# -*- coding: utf-8 -*-
"""Assert WbFbsClient / Content client use current Marketplace paths & bodies."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.wb.client import WbFbsClient
from app.wb.content import WbContentClient


class WbFbsClientEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = WbFbsClient("test.token.value")
        self.calls = []  # type: list

        def capture(method, path, params=None, body=None, raw=False):
            self.calls.append(
                {
                    "method": method,
                    "path": path,
                    "params": params,
                    "body": body,
                    "raw": raw,
                }
            )
            if path.endswith("/order-ids"):
                return {"orderIds": [11, 22]}
            if path.endswith("/orders") and method == "GET":
                return {"orders": [{"id": 11}]}
            if "trbx" in path and method == "GET":
                return {"trbxes": []}
            if path.endswith("/stickers"):
                return {"stickers": []}
            if path.endswith("/meta"):
                return {"orders": [{"id": 1, "metaDetails": []}]}
            if path == "/api/v3/orders/status":
                return {"orders": []}
            if path == "/api/v3/supplies" and method == "POST":
                return {"id": "WB-GI-1"}
            if path.startswith("/api/v3/supplies/") and method == "GET" and path.count("/") == 4:
                return {"id": "WB-GI-1", "done": False}
            return {"orders": [], "supplies": [], "next": 0}

        self.client._request = capture  # type: ignore[method-assign]

    def test_core_marketplace_paths(self) -> None:
        self.client.ping()
        self.client.get_new_orders()
        self.client.get_orders_page(limit=10, next_token=0)
        self.client.get_statuses([1, 2])
        self.client.get_supplies(limit=10, next_token=0)
        self.client.get_supply_order_ids("WB-GI-1")
        self.client.get_order_stickers([5], sticker_type="png", width=58, height=40)
        self.client.get_orders_meta([7, 8])
        self.client.set_order_sgtin(9, ["0104600"])
        self.client.delete_order_meta(9, "sgtin")
        self.client.create_supply("Test")
        self.client.add_orders_to_supply("WB-GI-1", [1, 2])
        self.client.get_supply("WB-GI-1")
        self.client.get_supply_boxes("WB-GI-1")
        self.client.create_supply_boxes("WB-GI-1", 1)
        self.client.delete_supply_boxes("WB-GI-1", ["TRBX-1"])
        self.client.get_box_stickers("WB-GI-1", ["TRBX-1"], sticker_type="png")
        self.client.get_archive_orders(2025, 6, limit=1, next_token=0)

        by_key = {
            (c["method"], c["path"]): c for c in self.calls
        }
        self.assertIn(("GET", "/ping"), by_key)
        self.assertIn(("GET", "/api/v3/orders/new"), by_key)
        self.assertIn(("GET", "/api/v3/orders"), by_key)
        self.assertEqual(by_key[("POST", "/api/v3/orders/status")]["body"], {"orders": [1, 2]})
        self.assertIn(("GET", "/api/v3/supplies"), by_key)
        self.assertIn(
            ("GET", "/api/marketplace/v3/supplies/WB-GI-1/order-ids"), by_key
        )
        stickers = by_key[("POST", "/api/v3/orders/stickers")]
        self.assertEqual(stickers["params"]["type"], "png")
        self.assertEqual(stickers["params"]["width"], 58)
        self.assertEqual(stickers["params"]["height"], 40)
        self.assertEqual(stickers["body"], {"orders": [5]})
        meta = by_key[("POST", "/api/marketplace/v3/orders/meta")]
        self.assertEqual(meta["body"], {"orders": [7, 8]})
        self.assertIn(("PUT", "/api/v3/orders/9/meta/sgtin"), by_key)
        self.assertEqual(
            by_key[("PUT", "/api/v3/orders/9/meta/sgtin")]["body"],
            {"sgtins": ["0104600"]},
        )
        self.assertEqual(
            by_key[("DELETE", "/api/v3/orders/9/meta")]["params"],
            {"key": "sgtin"},
        )
        self.assertEqual(
            by_key[("POST", "/api/v3/supplies")]["body"], {"name": "Test"}
        )
        self.assertEqual(
            by_key[("PATCH", "/api/marketplace/v3/supplies/WB-GI-1/orders")]["body"],
            {"orders": [1, 2]},
        )
        self.assertIn(("GET", "/api/v3/supplies/WB-GI-1"), by_key)
        self.assertIn(("GET", "/api/v3/supplies/WB-GI-1/trbx"), by_key)
        self.assertEqual(
            by_key[("POST", "/api/v3/supplies/WB-GI-1/trbx")]["body"],
            {"amount": 1},
        )
        self.assertEqual(
            by_key[("DELETE", "/api/v3/supplies/WB-GI-1/trbx")]["body"],
            {"trbxIds": ["TRBX-1"]},
        )
        box_st = by_key[("POST", "/api/v3/supplies/WB-GI-1/trbx/stickers")]
        self.assertEqual(box_st["params"], {"type": "png"})
        self.assertEqual(box_st["body"], {"trbxIds": ["TRBX-1"]})
        self.assertIn(
            ("GET", "/api/marketplace/v3/fbs/orders/archive"), by_key
        )

    def test_add_orders_rejects_over_100(self) -> None:
        with self.assertRaises(ValueError):
            self.client.add_orders_to_supply("WB-GI-1", list(range(101)))


class ContentClientFilterTests(unittest.TestCase):
    def test_uses_text_search_not_nmid_array(self) -> None:
        client = WbContentClient("tok")
        bodies = []  # type: list

        def fake_request(method, path, body=None):
            bodies.append(body)
            return {
                "cards": [
                    {"nmID": 111, "title": "A"},
                    {"nmID": 999, "title": "noise"},
                ]
            }

        client._request = fake_request  # type: ignore[method-assign]
        with patch("app.wb.content.time.sleep"):
            out = client.get_cards_by_nm_ids([111, 222])
        self.assertEqual(len(bodies), 2)
        for body in bodies:
            filt = body["settings"]["filter"]
            self.assertNotIn("nmID", filt)
            self.assertIn("textSearch", filt)
            self.assertEqual(body["settings"]["cursor"]["limit"], 1)
        self.assertEqual(out[111]["title"], "A")
        self.assertNotIn(222, out)


class OpenCompatibleCrossBorderTests(unittest.TestCase):
    def test_filters_mismatched_cross_border(self) -> None:
        import tempfile
        from pathlib import Path

        from app.db import Database
        from app.services.orders import OrdersService

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "t.db")
        db.init_schema()
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO supply_sources (id, name, api_key, created_at)
                VALUES (1, 'WB', 'k', '2026-01-01')
                """
            )
            conn.execute(
                """
                INSERT INTO wb_fbs_supplies (
                    source_id, supply_id, name, done, cargo_type, is_b2b,
                    order_ids_json, boxes_json, raw_json, synced_at
                ) VALUES
                (1, 'S-CB', 'cb', 0, 1, 0, '[10]', '[]',
                 '{"crossBorderType": 1}', '2026-01-01'),
                (1, 'S-LOCAL', 'local', 0, 1, 0, '[20]', '[]',
                 '{"crossBorderType": 0}', '2026-01-01'),
                (1, 'S-EMPTY', 'empty', 0, 1, 0, '[]', '[]', '{}', '2026-01-01')
                """
            )
            conn.execute(
                """
                INSERT INTO wb_fbs_orders (
                    source_id, order_id, warehouse_id, cargo_type, tab,
                    supply_id, is_b2b, raw_json, synced_at
                ) VALUES
                (1, 10, 100, 1, 'assembly', 'S-CB', 0,
                 '{"crossBorderType": 1}', '2026-01-01'),
                (1, 20, 100, 1, 'assembly', 'S-LOCAL', 0,
                 '{"crossBorderType": 0}', '2026-01-01')
                """
            )
            conn.commit()

        svc = OrdersService(db)
        ids = [
            s["supply_id"]
            for s in svc.open_compatible_supplies(
                1, cargo_type=1, is_b2b=False, warehouse_id=100, cross_border_type=1
            )
        ]
        self.assertIn("S-CB", ids)
        self.assertIn("S-EMPTY", ids)
        self.assertNotIn("S-LOCAL", ids)


if __name__ == "__main__":
    unittest.main()
