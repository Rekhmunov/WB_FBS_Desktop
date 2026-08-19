"""Local ШК pick-check for WB FBS orders without КИЗ."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from review_processor.wb_fbs_detail import (
    save_pick_verify,
    validate_ean_against_order_skus,
)


class ValidateEanTests(unittest.TestCase):
    def test_ean13_matches(self):
        ok, normalized, err = validate_ean_against_order_skus(
            "4670123456789", ["4670123456789"]
        )
        self.assertTrue(ok)
        self.assertEqual(normalized, "4670123456789")
        self.assertEqual(err, "")

    def test_gtin14_leading_zero_matches_ean13(self):
        ok, normalized, err = validate_ean_against_order_skus(
            "04670123456789", ["4670123456789"]
        )
        self.assertTrue(ok)
        self.assertEqual(normalized, "4670123456789")
        self.assertEqual(err, "")

    def test_mismatch(self):
        ok, _, err = validate_ean_against_order_skus(
            "4600000000000", ["4670123456789"]
        )
        self.assertFalse(ok)
        self.assertIn("не совпадает", err)

    def test_empty_order_skus(self):
        ok, _, err = validate_ean_against_order_skus("4670123456789", [])
        self.assertFalse(ok)
        self.assertIn("нет штрихкодов", err)


class SavePickVerifyTests(unittest.TestCase):
    def test_save_verified_uses_db_skus_not_client(self):
        repo = MagicMock()
        with patch(
            "review_processor.wb_fbs_detail.wb.load_order_barcodes_map",
            return_value={101: ["4670123456789"]},
        ), patch(
            "review_processor.wb_fbs_detail.wb.update_order_pick_verify"
        ) as upd:
            upd.return_value = {
                "ok": True,
                "conflict": False,
                "missing": False,
                "verified_at": "t1",
                "verified": True,
                "barcode": "4670123456789",
            }
            result = save_pick_verify(
                repo=repo,
                user_id=1,
                source_id=7,
                items=[
                    {
                        "order_id": 101,
                        "pick_verified": True,
                        "pick_barcode": "4670123456789",
                        # Forged client barcodes must be ignored.
                        "barcodes": ["9999999999999"],
                        "expected_verified_at": "t0",
                    }
                ],
                allowed_order_ids={101},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["saved"], 1)
        self.assertEqual(result["errors"], 0)
        upd.assert_called_once()
        kwargs = upd.call_args.kwargs
        self.assertTrue(kwargs["verified"])
        self.assertEqual(kwargs["barcode"], "4670123456789")
        self.assertEqual(kwargs["expected_verified_at"], "t0")
        self.assertFalse(kwargs["force"])
        self.assertEqual(result["results"][0]["pick_verified_at"], "t1")

    def test_rejects_when_scan_mismatches_db_skus(self):
        repo = MagicMock()
        with patch(
            "review_processor.wb_fbs_detail.wb.load_order_barcodes_map",
            return_value={101: ["4670123456789"]},
        ), patch(
            "review_processor.wb_fbs_detail.wb.update_order_pick_verify"
        ) as upd:
            result = save_pick_verify(
                repo=repo,
                user_id=1,
                source_id=7,
                items=[
                    {
                        "order_id": 101,
                        "pick_verified": True,
                        "pick_barcode": "4600000000000",
                        "barcodes": ["4600000000000"],  # client lies — ignored
                    }
                ],
                allowed_order_ids={101},
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"], 1)
        self.assertIn("не совпадает", result["results"][0]["error"])
        upd.assert_not_called()

    def test_rejects_order_not_allowed(self):
        repo = MagicMock()
        with patch(
            "review_processor.wb_fbs_detail.wb.load_order_barcodes_map",
            return_value={},
        ), patch(
            "review_processor.wb_fbs_detail.wb.update_order_pick_verify"
        ) as upd:
            result = save_pick_verify(
                repo=repo,
                user_id=1,
                source_id=7,
                items=[{"order_id": 101, "pick_verified": True, "pick_barcode": "1"}],
                allowed_order_ids={999},
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"], 1)
        upd.assert_not_called()

    def test_clear_resets(self):
        repo = MagicMock()
        with patch(
            "review_processor.wb_fbs_detail.wb.load_order_barcodes_map",
            return_value={101: ["4670123456789"]},
        ), patch(
            "review_processor.wb_fbs_detail.wb.update_order_pick_verify"
        ) as upd:
            upd.return_value = {
                "ok": True,
                "conflict": False,
                "missing": False,
                "verified_at": "t2",
                "verified": False,
                "barcode": "",
            }
            result = save_pick_verify(
                repo=repo,
                user_id=1,
                source_id=7,
                items=[{"order_id": 101, "clear": True}],
                allowed_order_ids={101},
            )
        self.assertTrue(result["ok"])
        kwargs = upd.call_args.kwargs
        self.assertFalse(kwargs["verified"])
        self.assertEqual(kwargs["barcode"], "")

    def test_save_conflict_when_another_operator_wrote(self):
        repo = MagicMock()
        with patch(
            "review_processor.wb_fbs_detail.wb.load_order_barcodes_map",
            return_value={101: ["4670123456789"]},
        ), patch(
            "review_processor.wb_fbs_detail.wb.update_order_pick_verify"
        ) as upd:
            upd.return_value = {
                "ok": False,
                "conflict": True,
                "missing": False,
                "verified_at": "2026-08-14T09:00:00+00:00",
                "verified": True,
                "barcode": "4670123456789",
            }
            result = save_pick_verify(
                repo=repo,
                user_id=1,
                source_id=7,
                items=[
                    {
                        "order_id": 101,
                        "pick_verified": True,
                        "pick_barcode": "4670123456789",
                        "expected_verified_at": "2026-08-14T08:00:00+00:00",
                    }
                ],
                allowed_order_ids={101},
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"], 1)
        row = result["results"][0]
        self.assertTrue(row["conflict"])
        self.assertIn("другим оператором", row["error"])
        self.assertEqual(row["pick_verified_at"], "2026-08-14T09:00:00+00:00")
        self.assertEqual(
            upd.call_args.kwargs["expected_verified_at"],
            "2026-08-14T08:00:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
