# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import MagicMock, patch

from app.services.kiz_pick import (
    KizService,
    PickVerifyService,
    extract_gtin,
    gtin_matches_skus,
    kiz_from_meta_row,
    kiz_status_from_decision,
    pending_wb_save_jobs,
    row_matches_modal_search,
    summarize_kiz_check_status,
)


class ModalSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.row = {
            "order_id": 266846035,
            "article": "ART-RED-42",
            "product_name": "Куртка зимняя красная",
            "brand": "BrandX",
            "nm_id": 123456,
            "sticker_part_a": "5806",
            "sticker_part_b": "1234",
            "sticker_number": "58061234",
            "sticker_barcode": "2001234567890",
            "pick_barcode": "4604060004010",
            "skus": ["4604060004010", "04604060004010"],
        }

    def test_matches_name_article_sku_order_sticker(self) -> None:
        self.assertTrue(row_matches_modal_search(self.row, "куртка"))
        self.assertTrue(row_matches_modal_search(self.row, "ART-RED"))
        self.assertTrue(row_matches_modal_search(self.row, "4604060004010"))
        self.assertTrue(row_matches_modal_search(self.row, "266846035"))
        self.assertTrue(row_matches_modal_search(self.row, "58061234"))
        self.assertTrue(row_matches_modal_search(self.row, "1234"))
        self.assertTrue(row_matches_modal_search(self.row, "2001234567890"))

    def test_parses_skus_json_string(self) -> None:
        row = {"order_id": 1, "skus": '["999888777"]'}
        self.assertTrue(row_matches_modal_search(row, "999888777"))

    def test_empty_query_matches_all(self) -> None:
        self.assertTrue(row_matches_modal_search(self.row, "  "))
        self.assertFalse(row_matches_modal_search(self.row, "неттакого"))


class GtinValidationTests(unittest.TestCase):
    def test_extract_gtin_from_cis(self):
        code = "0104604060004010215ABC123\u001d93dGVzdA=="
        self.assertEqual(extract_gtin(code), "04604060004010")

    def test_gtin_matches_sku_with_leading_zero(self):
        self.assertTrue(gtin_matches_skus("4604060004010", ["04604060004010"]))

    def test_validate_mark_rejects_mismatch(self):
        kiz = KizService(MagicMock())
        ok, err = kiz.validate_mark(
            "0104604060004010215ABC123",
            ["9999999999999"],
            skip_gtin=False,
        )
        self.assertFalse(ok)
        self.assertIn("GTIN", err)


class PendingWbSaveJobsTests(unittest.TestCase):
    def test_skips_already_synced(self):
        rows = [
            {
                "order_id": 1,
                "kiz_codes": ["0104604060004010215A"],
                "kiz_wb_synced": True,
                "kiz_status": "ok",
                "skus": [],
            },
            {
                "order_id": 2,
                "kiz_codes": ["0104604060004010215B"],
                "kiz_wb_synced": False,
                "kiz_status": "pending",
                "skus": ["1"],
            },
        ]
        jobs = pending_wb_save_jobs(rows)
        self.assertEqual([j["order_id"] for j in jobs], [2])

    def test_includes_synced_with_error_for_retry(self):
        rows = [
            {
                "order_id": 3,
                "kiz_codes": ["0104604060004010215C"],
                "kiz_wb_synced": True,
                "kiz_status": "ok",
                "skus": [],
            }
        ]
        jobs = pending_wb_save_jobs(rows, row_errors={3: "timeout"})
        self.assertEqual([j["order_id"] for j in jobs], [3])

    def test_only_order_ids_filters_retry_set(self):
        rows = [
            {
                "order_id": 10,
                "kiz_codes": ["a"],
                "kiz_wb_synced": False,
                "kiz_status": "error",
                "skus": [],
            },
            {
                "order_id": 11,
                "kiz_codes": ["b"],
                "kiz_wb_synced": False,
                "kiz_status": "pending",
                "skus": [],
            },
        ]
        jobs = pending_wb_save_jobs(rows, only_order_ids=[10])
        self.assertEqual([j["order_id"] for j in jobs], [10])


class PickRowsSplitTests(unittest.TestCase):
    @patch("app.services.kiz_pick.WbFbsClient")
    def test_pick_excludes_orders_with_local_kiz_codes(self, client_cls):
        db = MagicMock()
        conn = MagicMock()
        db.connect.return_value.__enter__.return_value = conn
        conn.execute.return_value.fetchall.return_value = [
            {
                "order_id": 1,
                "article": "a1",
                "skus_json": '["123"]',
                "kiz_codes_json": json.dumps(["0104604060004010215X"]),
                "pick_verified": 0,
                "pick_barcode": "",
                "pick_verified_at": None,
            },
            {
                "order_id": 2,
                "article": "a2",
                "skus_json": '["456"]',
                "kiz_codes_json": "[]",
                "pick_verified": 0,
                "pick_barcode": "",
                "pick_verified_at": None,
            },
        ]
        client_cls.return_value.get_orders_meta.return_value = [
            {"id": 1},
            {"id": 2},
        ]
        from app.db import Database

        with patch.object(Database, "rows_to_dicts", side_effect=lambda rows: list(rows)):
            pick = PickVerifyService(db)
            rows = pick.rows(1, "WB-GI-1", "key")
        self.assertEqual([r["order_id"] for r in rows], [2])


class KizStatusToneTests(unittest.TestCase):
    def test_status_from_decision(self) -> None:
        self.assertEqual(kiz_status_from_decision("filled", ["01…"]), "ok")
        self.assertEqual(kiz_status_from_decision("sgtinIntroduced", ["01…"]), "ok")
        self.assertEqual(kiz_status_from_decision("sgtinRetired", ["01…"]), "error")
        self.assertEqual(kiz_status_from_decision("required", ["01…"]), "pending")
        self.assertEqual(kiz_status_from_decision("required", []), "empty")

    def test_meta_row_and_summarize(self) -> None:
        parsed = kiz_from_meta_row(
            {
                "id": 1,
                "metaDetails": [
                    {"key": "sgtin", "value": ["010467…"], "decision": "filled"}
                ],
            }
        )
        self.assertTrue(parsed["kiz_required"])
        self.assertEqual(parsed["kiz_status"], "ok")
        self.assertEqual(summarize_kiz_check_status(["ok", "ok"]), "ok")
        self.assertEqual(summarize_kiz_check_status(["ok", "error"]), "error")
        self.assertEqual(summarize_kiz_check_status(["ok", "pending"]), "pending")
        self.assertEqual(summarize_kiz_check_status(["empty"]), "none")
        self.assertEqual(summarize_kiz_check_status([]), "none")

    def test_check_supply_status_persists_portal_codes(self) -> None:
        """Refresh next to Маркировка must write portal КИЗ into SQLite."""
        db = MagicMock()
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchall.return_value = []
        db.connect.return_value = conn

        local_row = {
            "order_id": 11,
            "kiz_codes_json": "[]",
            "kiz_wb_synced": 0,
            "supplier_status": "confirm",
            "wb_status": "waiting",
        }
        client = MagicMock()
        client.get_supply_order_ids.return_value = [11]
        client.get_statuses.return_value = [
            {"id": 11, "supplierStatus": "confirm", "wbStatus": "waiting"},
        ]
        client.get_orders_meta.return_value = [
            {
                "id": 11,
                "metaDetails": [
                    {
                        "key": "sgtin",
                        "value": ["0104604060004010215PORTAL"],
                        "decision": "filled",
                    }
                ],
            },
        ]
        with patch("app.services.kiz_pick.WbFbsClient", return_value=client):
            with patch(
                "app.services.kiz_pick.Database.rows_to_dicts",
                return_value=[local_row],
            ):
                with patch("app.services.order_open_cache.upsert_meta") as upsert:
                    payload = KizService(db).check_supply_status(1, "WB-1", "key")
        self.assertEqual(payload["status"], "ok")
        update_calls = [
            c
            for c in conn.execute.call_args_list
            if c.args and "kiz_codes_json" in str(c.args[0])
        ]
        self.assertTrue(update_calls)
        args = update_calls[0].args[1]
        self.assertIn("0104604060004010215PORTAL", args[0])
        self.assertEqual(args[2], 1)  # kiz_wb_synced
        self.assertEqual(args[3], 1)  # source_id
        self.assertEqual(args[4], 11)  # order_id
        upsert.assert_called_once()

    def test_check_supply_status_live_ok(self) -> None:
        db = MagicMock()
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchall.return_value = []
        db.connect.return_value = conn

        client = MagicMock()
        client.get_supply_order_ids.return_value = [11, 22]
        client.get_statuses.return_value = [
            {"id": 11, "supplierStatus": "confirm", "wbStatus": "waiting"},
            {"id": 22, "supplierStatus": "confirm", "wbStatus": "waiting"},
        ]
        client.get_orders_meta.return_value = [
            {
                "id": 11,
                "metaDetails": [
                    {"key": "sgtin", "value": ["010467…"], "decision": "filled"}
                ],
            },
            {"id": 22, "metaDetails": []},
        ]
        with patch("app.services.kiz_pick.WbFbsClient", return_value=client):
            with patch(
                "app.services.kiz_pick.Database.rows_to_dicts",
                return_value=[],
            ):
                payload = KizService(db).check_supply_status(1, "WB-1", "key")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["counts"]["ok"], 1)
        by_id = {r["order_id"]: r for r in payload["orders"]}
        self.assertEqual(by_id[11]["kiz_status"], "ok")
        self.assertFalse(by_id[22]["kiz_required"])

    def test_check_supply_status_error_and_cancelled_ignored(self) -> None:
        db = MagicMock()
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchall.return_value = []
        db.connect.return_value = conn

        client = MagicMock()
        client.get_supply_order_ids.return_value = [11, 33]
        client.get_statuses.return_value = [
            {"id": 11, "supplierStatus": "confirm", "wbStatus": "waiting"},
            {"id": 33, "supplierStatus": "cancel", "wbStatus": "canceled"},
        ]
        client.get_orders_meta.return_value = [
            {
                "id": 11,
                "metaDetails": [
                    {"key": "sgtin", "value": ["010467…"], "decision": "sgtinRetired"}
                ],
            },
        ]
        with patch("app.services.kiz_pick.WbFbsClient", return_value=client):
            with patch(
                "app.services.kiz_pick.Database.rows_to_dicts",
                return_value=[],
            ):
                payload = KizService(db).check_supply_status(1, "WB-1", "key")
        self.assertEqual(payload["status"], "error")
        # Cancelled order must not request meta / affect tone
        client.get_orders_meta.assert_called_once_with([11])
        by_id = {r["order_id"]: r for r in payload["orders"]}
        self.assertEqual(by_id[33]["kiz_status"], "empty")
        self.assertTrue(by_id[33]["cancelled"])


if __name__ == "__main__":
    unittest.main()
