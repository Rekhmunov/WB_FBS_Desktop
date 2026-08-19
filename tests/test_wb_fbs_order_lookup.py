"""WB FBS search: lookup finished/cancelled orders by exact order id."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from review_processor.wb_fbs import (
    TAB_ASSEMBLY,
    TAB_CANCELLED,
    TAB_FINISHED,
    TAB_NEW,
    parse_order_id_query,
    lookup_order_by_id,
    upsert_order,
)


class ParseOrderIdQueryTests(unittest.TestCase):
    def test_accepts_long_numeric_ids(self):
        self.assertEqual(parse_order_id_query("13833711"), 13833711)
        self.assertEqual(parse_order_id_query(" 987654 "), 987654)

    def test_rejects_short_or_non_numeric(self):
        self.assertIsNone(parse_order_id_query("12345"))
        self.assertIsNone(parse_order_id_query("art-100"))
        self.assertIsNone(parse_order_id_query("13833711x"))
        self.assertIsNone(parse_order_id_query(""))
        self.assertIsNone(parse_order_id_query(None))


class LookupOrderByIdTests(unittest.TestCase):
    def _repo_with_order(self, *, tab: str, order_id: int = 13833711):
        repo = MagicMock()
        repo._connect.return_value.__enter__ = MagicMock(
            return_value=MagicMock(
                execute=MagicMock(
                    side_effect=self._conn_execute_factory(tab=tab, order_id=order_id)
                )
            )
        )
        repo._connect.return_value.__exit__ = MagicMock(return_value=False)
        repo._sql = lambda sql: sql
        repo._row_to_dict = lambda row: dict(row)
        repo.get_product_name_by_article = MagicMock(return_value={})
        repo.get_product_photo_map = MagicMock(return_value={})
        return repo

    def _conn_execute_factory(self, *, tab: str, order_id: int):
        row = {
            "user_id": 1,
            "source_id": 2,
            "order_id": order_id,
            "order_uid": "",
            "article": "sku-1",
            "nm_id": 11,
            "chrt_id": 22,
            "skus_json": '["460001"]',
            "price": 10000,
            "final_price": 10000,
            "currency_code": 643,
            "warehouse_id": 1,
            "office_id": 1,
            "offices_json": '["Склад"]',
            "cargo_type": 1,
            "delivery_type": "fbs",
            "supplier_status": "complete" if tab == TAB_FINISHED else "cancel",
            "wb_status": "sold" if tab == TAB_FINISHED else "canceled_by_client",
            "tab": tab,
            "supply_id": "",
            "is_archive": False,
            "is_b2b": False,
            "comment_text": "",
            "created_at_wb": "2026-08-01T10:00:00+00:00",
            "raw_json": "{}",
            "synced_at": "2026-08-01T10:00:00+00:00",
        }

        class _Result:
            def __init__(self, payload):
                self._payload = payload

            def fetchone(self):
                return self._payload

            def fetchall(self):
                if self._payload is None:
                    return []
                if isinstance(self._payload, list):
                    return self._payload
                return [self._payload]

        def _execute(sql, params=None):
            text = str(sql)
            if "COUNT(*)" in text and "GROUP BY tab" in text:
                return _Result([{"tab": tab, "n": 1}])
            if "COUNT(*)" in text and "cargo_type = 1" in text:
                return _Result({"n": 0})
            if "COUNT(*)" in text and "wb_fbs_supplies" in text:
                return _Result({"n": 0})
            if "FROM wb_fbs_orders" in text and "order_id = ?" in text:
                return _Result(row)
            if "INSERT INTO wb_fbs_orders" in text:
                return _Result(None)
            return _Result(None)

        return _execute

    @patch("review_processor.wb_fbs.ensure_wb_fbs_tables")
    def test_local_finished_order_without_remote(self, _ensure):
        repo = self._repo_with_order(tab=TAB_FINISHED)
        result = lookup_order_by_id(
            repo,
            user_id=1,
            source_id=2,
            order_id=13833711,
            api_key=None,
            allow_remote=False,
        )
        self.assertTrue(result["found"])
        self.assertEqual(result["source"], "local")
        self.assertEqual(result["tab"], TAB_FINISHED)
        self.assertEqual(result["item"]["order_id"], 13833711)
        self.assertEqual(result["item"]["finished_status_label"], "Заказ выкуплен")

    @patch("review_processor.wb_fbs.ensure_wb_fbs_tables")
    def test_local_cancelled_order(self, _ensure):
        repo = self._repo_with_order(tab=TAB_CANCELLED)
        result = lookup_order_by_id(
            repo,
            user_id=1,
            source_id=2,
            order_id=13833711,
            allow_remote=False,
        )
        self.assertTrue(result["found"])
        self.assertEqual(result["tab"], TAB_CANCELLED)
        self.assertEqual(result["item"]["cancel_reason_label"], "Отказ на ПВЗ")

    @patch("review_processor.wb_fbs.WbFbsClient")
    @patch("review_processor.wb_fbs.ensure_wb_fbs_tables")
    def test_remote_lookup_upserts_finished_order(self, _ensure, client_cls):
        # First get_order_by_id miss, then hit after upsert.
        stored = {
            "user_id": 1,
            "source_id": 2,
            "order_id": 55555555,
            "order_uid": "u",
            "article": "a1",
            "nm_id": 9,
            "chrt_id": 8,
            "skus_json": "[]",
            "price": 100,
            "final_price": 100,
            "currency_code": 643,
            "warehouse_id": None,
            "office_id": None,
            "offices_json": "[]",
            "cargo_type": 0,
            "delivery_type": "fbs",
            "supplier_status": "complete",
            "wb_status": "sold",
            "tab": TAB_FINISHED,
            "supply_id": "WB-GI-1",
            "is_archive": False,
            "is_b2b": False,
            "comment_text": "",
            "created_at_wb": "2026-08-10T12:00:00+00:00",
            "raw_json": "{}",
            "synced_at": "2026-08-10T12:00:00+00:00",
        }
        calls = {"n": 0}

        class _Result:
            def __init__(self, payload):
                self._payload = payload

            def fetchone(self):
                return self._payload

            def fetchall(self):
                return [] if self._payload is None else (
                    self._payload if isinstance(self._payload, list) else [self._payload]
                )

        def _execute(sql, params=None):
            text = str(sql)
            if "FROM wb_fbs_orders" in text and "order_id = ?" in text:
                calls["n"] += 1
                return _Result(None if calls["n"] == 1 else stored)
            if "COUNT(*)" in text and "GROUP BY tab" in text:
                return _Result([])
            if "COUNT(*)" in text:
                return _Result({"n": 0})
            return _Result(None)

        conn = MagicMock()
        conn.execute.side_effect = _execute
        repo = MagicMock()
        repo._connect.return_value.__enter__ = MagicMock(return_value=conn)
        repo._connect.return_value.__exit__ = MagicMock(return_value=False)
        repo._sql = lambda sql: sql
        repo._row_to_dict = lambda row: dict(row) if row else None
        repo.get_product_name_by_article = MagicMock(return_value={})
        repo.get_product_photo_map = MagicMock(return_value={})

        client = client_cls.return_value
        client.get_statuses.return_value = [
            {"id": 55555555, "supplierStatus": "complete", "wbStatus": "sold"}
        ]
        client.get_orders_page.return_value = (
            [
                {
                    "id": 55555555,
                    "article": "a1",
                    "nmId": 9,
                    "supplyId": "WB-GI-1",
                    "createdAt": "2026-08-10T12:00:00Z",
                    "skus": [],
                    "offices": [],
                }
            ],
            None,
        )
        client.get_archive_orders.return_value = ([], 0)

        with patch("review_processor.wb_fbs.upsert_order") as upsert:
            result = lookup_order_by_id(
                repo,
                user_id=1,
                source_id=2,
                order_id=55555555,
                api_key="token",
                allow_remote=True,
            )
            upsert.assert_called_once()
            kwargs = upsert.call_args.kwargs
            self.assertEqual(kwargs["supplier_status"], "complete")
            self.assertEqual(kwargs["wb_status"], "sold")
            self.assertFalse(kwargs["is_archive"])

        self.assertTrue(result["found"])
        self.assertEqual(result["source"], "remote")
        self.assertEqual(result["tab"], TAB_FINISHED)

    @patch("review_processor.wb_fbs.WbFbsClient")
    @patch("review_processor.wb_fbs.ensure_wb_fbs_tables")
    def test_remote_not_found_when_status_missing(self, _ensure, client_cls):
        conn = MagicMock()

        class _Result:
            def fetchone(self):
                return None

            def fetchall(self):
                return []

        conn.execute.return_value = _Result()
        repo = MagicMock()
        repo._connect.return_value.__enter__ = MagicMock(return_value=conn)
        repo._connect.return_value.__exit__ = MagicMock(return_value=False)
        repo._sql = lambda sql: sql
        repo._row_to_dict = lambda row: dict(row) if row else None
        repo.get_product_name_by_article = MagicMock(return_value={})
        repo.get_product_photo_map = MagicMock(return_value={})

        client = client_cls.return_value
        client.get_statuses.return_value = []

        result = lookup_order_by_id(
            repo,
            user_id=1,
            source_id=2,
            order_id=11111111,
            api_key="token",
        )
        self.assertFalse(result["found"])
        self.assertEqual(result["message"], "Заказ не найден в WB API")
        client.get_orders_page.assert_not_called()


class UpsertTabSmokeTests(unittest.TestCase):
    def test_compute_paths_used_by_lookup(self):
        # Keep smoke coverage that lookup tabs stay aligned with sync taxonomy.
        self.assertEqual(TAB_NEW, "new")
        self.assertEqual(TAB_ASSEMBLY, "assembly")
        self.assertTrue(callable(upsert_order))


if __name__ == "__main__":
    unittest.main()
