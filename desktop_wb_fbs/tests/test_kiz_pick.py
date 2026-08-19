# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import MagicMock, patch

from app.services.kiz_pick import (
    KizService,
    PickVerifyService,
    extract_gtin,
    gtin_matches_skus,
    pending_wb_save_jobs,
)


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


if __name__ == "__main__":
    unittest.main()
