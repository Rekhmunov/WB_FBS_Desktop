# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.ozon.sticker_fields import (
    format_scanit_tail_html,
    looks_like_ozon_scanit,
    normalize_ozon_package_barcode,
    normalize_ozon_scanit,
    sticker_fields_from_posting,
)
from app.ozon.sync import upsert_posting
from app.services.ozon_stickers import OzonStickersService


class ScanitHelpersTest(unittest.TestCase):
    def test_looks_like_scanit(self) -> None:
        self.assertTrue(looks_like_ozon_scanit("iiAb12Cd34"))
        self.assertTrue(looks_like_ozon_scanit("IIXYZ999"))
        self.assertFalse(looks_like_ozon_scanit("12345-0001-1"))
        self.assertFalse(looks_like_ozon_scanit("ii"))
        self.assertFalse(looks_like_ozon_scanit(""))

    def test_normalize_placeholders(self) -> None:
        self.assertEqual(normalize_ozon_package_barcode("0"), "")
        self.assertEqual(normalize_ozon_package_barcode("123"), "")
        self.assertEqual(normalize_ozon_package_barcode("123456789012"), "123456789012")
        self.assertEqual(normalize_ozon_scanit("0"), "")
        self.assertEqual(normalize_ozon_scanit("iiABCDE12"), "iiABCDE12")

    def test_fields_from_posting(self) -> None:
        fields = sticker_fields_from_posting(
            {
                "posting_number": "12345-0001-1",
                "scanit": "iiInstance99",
                "barcodes": {
                    "upper_barcode": "0",
                    "lower_barcode": "9876543210123",
                    "scanit": "ignored-when-top-set",
                },
            }
        )
        self.assertEqual(fields["sticker_barcode"], "")
        self.assertEqual(fields["sticker_lower_barcode"], "9876543210123")
        self.assertEqual(fields["sticker_scanit"], "iiInstance99")
        self.assertEqual(fields["sticker_part_a"], "12345")
        self.assertEqual(fields["sticker_part_b"], "0001-1")

    def test_format_tail_html(self) -> None:
        html = format_scanit_tail_html("iiABCDEF1234")
        self.assertIn("iiABCDEF", html)
        self.assertIn("1234", html)
        self.assertIn("22px", html)

    def test_scanit_widget_uses_plain_labels(self) -> None:
        """RichText scanit used to report a short height and overlap the date."""
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtWidgets import QApplication, QLabel

        app = QApplication.instance()
        if app is None:
            app = QApplication([])
        from app.ui.format_helpers import build_ozon_scanit_label

        self.assertIsNone(build_ozon_scanit_label(""))
        w = build_ozon_scanit_label("iiABCDEF1234")
        self.assertIsNotNone(w)
        parts = [c.text() for c in w.findChildren(QLabel)]
        self.assertEqual(parts, ["iiABCDEF", "1234"])
        self.assertGreaterEqual(int(w.minimumHeight()), 28)


class ScanitPersistTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_upsert_and_find_by_scanit(self) -> None:
        upsert_posting(
            self.db,
            1,
            {
                "posting_number": "555-0001-1",
                "status": "awaiting_deliver",
                "scanit": "iiLiveCode77",
                "barcodes": {
                    "upper_barcode": "1111222233334",
                    "lower_barcode": "4444555566667",
                },
                "products": [
                    {
                        "offer_id": "ART-1",
                        "sku": 99,
                        "name": "Товар",
                        "barcodes": ["4601234567890"],
                    }
                ],
            },
        )
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT sticker_scanit, sticker_barcode, barcodes_json "
                "FROM ozon_fbs_postings WHERE posting_number = ?",
                ("555-0001-1",),
            ).fetchone()
        self.assertEqual(row["sticker_scanit"], "iiLiveCode77")
        self.assertEqual(row["sticker_barcode"], "1111222233334")
        self.assertIn("4601234567890", row["barcodes_json"])
        self.assertNotIn("1111222233334", row["barcodes_json"])

        svc = OzonStickersService(self.db)
        found = svc.find_by_scan(1, "iiLiveCode77")
        self.assertIsNotNone(found["row"])
        self.assertEqual(found["row"]["posting_number"], "555-0001-1")


if __name__ == "__main__":
    unittest.main()
