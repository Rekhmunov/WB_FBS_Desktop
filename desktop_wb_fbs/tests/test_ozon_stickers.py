# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.services.ozon_stickers import (
    OzonStickersService,
    normalize_sticker_scan,
)


class NormalizeScanTest(unittest.TestCase):
    def test_strips_bom_and_space(self) -> None:
        self.assertEqual(normalize_sticker_scan("  ABC\ufeff "), "ABC")

    def test_empty(self) -> None:
        self.assertEqual(normalize_sticker_scan(""), "")
        self.assertEqual(normalize_sticker_scan(None), "")


class FindByScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.svc = OzonStickersService(self.db)
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO ozon_fbs_postings(
                    source_id, posting_number, status, tab,
                    sticker_barcode, sticker_lower_barcode,
                    barcodes_json, raw_json, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "12345-0001-1",
                    "awaiting_deliver",
                    "awaiting_deliver",
                    "9876543210123",
                    "LOWERQR001",
                    '["9876543210123","LOWERQR001"]',
                    "{}",
                    "2026-01-01T00:00:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO ozon_fbs_postings(
                    source_id, posting_number, status, tab,
                    sticker_barcode, sticker_lower_barcode,
                    barcodes_json, raw_json, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "12345-0002-1",
                    "awaiting_packaging",
                    "awaiting_packaging",
                    "",
                    "",
                    "[]",
                    '{"barcodes":["ONLYRAW999888777"]}',
                    "2026-01-01T00:00:00Z",
                ),
            )
            conn.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_exact_upper(self) -> None:
        found = self.svc.find_by_scan(1, "9876543210123")
        self.assertIsNotNone(found["row"])
        self.assertEqual(found["row"]["posting_number"], "12345-0001-1")
        self.assertFalse(found["ambiguous"])

    def test_exact_lower_casefold(self) -> None:
        found = self.svc.find_by_scan(1, "lowerqr001")
        self.assertIsNotNone(found["row"])
        self.assertEqual(found["row"]["posting_number"], "12345-0001-1")

    def test_posting_number(self) -> None:
        found = self.svc.find_by_scan(1, "12345-0001-1")
        self.assertIsNotNone(found["row"])

    def test_raw_json_fallback(self) -> None:
        found = self.svc.find_by_scan(1, "ONLYRAW999888777")
        self.assertIsNotNone(found["row"])
        self.assertEqual(found["row"]["posting_number"], "12345-0002-1")

    def test_miss(self) -> None:
        found = self.svc.find_by_scan(1, "no-such-code")
        self.assertIsNone(found["row"])
        self.assertFalse(found["ambiguous"])


if __name__ == "__main__":
    unittest.main()
