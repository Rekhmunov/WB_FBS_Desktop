# -*- coding: utf-8 -*-
"""Collect MGT preview/plan parity with web."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.services.collect_mgt import (
    CollectMgtService,
    plan_mgt_group,
    supply_is_empty,
    unique_supply_name,
)
from app.wb import default_mgt_supply_name


class UniqueNameTests(unittest.TestCase):
    def test_leaves_free_title(self) -> None:
        existing = set()  # type: set
        name = unique_supply_name("Поставка от 07.08.2026", existing)
        self.assertEqual(name, "Поставка от 07.08.2026")
        self.assertEqual(existing, set())

    def test_suffixes_when_taken(self) -> None:
        existing = {"Поставка от 07.08.2026"}
        self.assertEqual(
            unique_supply_name("Поставка от 07.08.2026", existing),
            "Поставка от 07.08.2026 (2)",
        )


class SupplyEmptyTests(unittest.TestCase):
    def test_empty_only_cargo_zero(self) -> None:
        self.assertTrue(
            supply_is_empty({"cargo_type": 0, "order_ids": []})
        )
        self.assertFalse(
            supply_is_empty({"cargo_type": 1, "order_ids": []})
        )
        self.assertFalse(
            supply_is_empty({"cargo_type": 2, "order_ids": []})
        )
        self.assertFalse(
            supply_is_empty({"cargo_type": 0, "order_ids": [1]})
        )


class PlanGroupTests(unittest.TestCase):
    def test_create_reserves_name_no_warehouse_in_title(self) -> None:
        reserved = set()  # type: set
        group = plan_mgt_group(
            is_b2b=False,
            order_ids=[101, 102],
            mgt_matching=[],
            empties=[],
            existing_names=reserved,
            warehouse_id=1943422,
            cross_border_type=None,
        )
        self.assertEqual(group["mode"], "create")
        name = str(group["suggested_name"])
        self.assertNotIn("склад", name.lower())
        self.assertNotIn("1943422", name)
        self.assertTrue(name.startswith("Поставка от "))
        self.assertFalse(group["name_conflict"])
        self.assertIn(name, reserved)

    def test_second_bucket_gets_suffix(self) -> None:
        reserved = set()  # type: set
        g1 = plan_mgt_group(
            is_b2b=False,
            order_ids=[1],
            mgt_matching=[],
            empties=[],
            existing_names=reserved,
            warehouse_id=111,
        )
        g2 = plan_mgt_group(
            is_b2b=False,
            order_ids=[2],
            mgt_matching=[],
            empties=[],
            existing_names=reserved,
            warehouse_id=222,
        )
        self.assertNotEqual(g1["suggested_name"], g2["suggested_name"])
        self.assertFalse(g1["name_conflict"])
        self.assertFalse(g2["name_conflict"])

    def test_add_one_claims_empty(self) -> None:
        empties = [
            {"supply_id": "WB-E-1", "name": "Empty", "cargo_type": 0, "order_ids": []}
        ]
        group = plan_mgt_group(
            is_b2b=False,
            order_ids=[1],
            mgt_matching=[],
            empties=empties,
            existing_names=set(),
        )
        self.assertEqual(group["mode"], "add_one")
        self.assertEqual(group["default_supply_id"], "WB-E-1")
        self.assertEqual(empties, [])


class PreviewTests(unittest.TestCase):
    def test_preview_existing_names_exclude_suggested(self) -> None:
        db = MagicMock()
        orders = MagicMock()
        orders.new_mgt_orders.return_value = [
            {
                "order_id": 10,
                "is_b2b": 0,
                "warehouse_id": 1943422,
                "raw_json": "{}",
                "supplier_status": "new",
                "wb_status": "waiting",
            }
        ]
        orders.list_supplies.return_value = ([], 0)
        svc = CollectMgtService(db, orders)
        preview = svc.preview(1)
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["needs_modal"])
        self.assertEqual(preview["mgt_count"], 1)
        suggested = str(preview["groups"][0]["suggested_name"])
        existing = set(preview["existing_names"] or [])
        self.assertNotIn(suggested, existing)
        self.assertTrue(
            suggested == default_mgt_supply_name(is_b2b=False)
            or suggested.startswith("Поставка от ")
        )

    def test_preview_skips_cancelled_local(self) -> None:
        db = MagicMock()
        orders = MagicMock()
        orders.new_mgt_orders.return_value = [
            {
                "order_id": 1,
                "is_b2b": 0,
                "warehouse_id": 1,
                "raw_json": "{}",
                "supplier_status": "cancel",
                "wb_status": "canceled",
            },
            {
                "order_id": 2,
                "is_b2b": 0,
                "warehouse_id": 1,
                "raw_json": "{}",
                "supplier_status": "new",
                "wb_status": "waiting",
            },
        ]
        orders.list_supplies.return_value = ([], 0)
        preview = CollectMgtService(db, orders).preview(1)
        self.assertEqual(preview["mgt_count"], 1)
        self.assertEqual(preview["groups"][0]["order_ids"], [2])

    def test_matching_requires_cross_border(self) -> None:
        db = MagicMock()
        orders = MagicMock()
        orders.new_mgt_orders.return_value = [
            {
                "order_id": 5,
                "is_b2b": 0,
                "warehouse_id": 10,
                "raw_json": '{"crossBorderType": 1}',
                "supplier_status": "new",
                "wb_status": "waiting",
            }
        ]
        orders.list_supplies.return_value = (
            [
                {
                    "supply_id": "WB-S-1",
                    "name": "MGT",
                    "cargo_type": 1,
                    "is_b2b": 0,
                    "order_ids_json": "[1]",
                    "order_ids": [1],
                    "raw_json": '{"crossBorderType": 0}',
                }
            ],
            1,
        )
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchall.return_value = [{"warehouse_id": 10}]
        db.connect.return_value = conn
        preview = CollectMgtService(db, orders).preview(1)
        group = preview["groups"][0]
        # CB mismatch → no matching MGT supply → create mode
        self.assertEqual(group["mode"], "create")
        self.assertEqual(group["compatible_supplies"], [])


class ExecuteTests(unittest.TestCase):
    def test_execute_skips_non_new_and_reports(self) -> None:
        db = MagicMock()
        orders = MagicMock()
        orders.new_mgt_orders.return_value = [
            {
                "order_id": 11,
                "is_b2b": 0,
                "warehouse_id": 1,
                "raw_json": "{}",
                "supplier_status": "new",
                "wb_status": "waiting",
            }
        ]
        orders.list_supplies.return_value = ([], 0)

        client = MagicMock()
        client.get_statuses.return_value = [
            {"id": 11, "supplierStatus": "confirm", "wbStatus": "waiting"}
        ]
        with patch("app.services.collect_mgt.WbFbsClient", return_value=client):
            result = CollectMgtService(db, orders).execute(
                1,
                "key",
                [
                    {
                        "group_key": "non_wh1_cbna",
                        "action": "create",
                        "name": "Поставка тест",
                    }
                ],
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["skipped_cancelled"], [11])
        self.assertEqual(result["added"], 0)
        client.create_supply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
