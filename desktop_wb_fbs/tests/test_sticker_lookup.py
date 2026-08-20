# -*- coding: utf-8 -*-
import unittest

from app.services.sticker_lookup import find_row_by_sticker, normalize_scan, scan_key


class StickerLookupTests(unittest.TestCase):
    def test_primary_barcode_case_insensitive(self):
        rows = [
            {
                "order_id": 1,
                "sticker_barcode": "!uKEtQZVx",
                "sticker_part_a": "111",
                "sticker_part_b": "2222",
                "sticker_number": "1112222",
            }
        ]
        row, amb, _ = find_row_by_sticker(rows, "!uketqzvx")
        self.assertFalse(amb)
        self.assertEqual(row["order_id"], 1)

    def test_ambiguous_barcode(self):
        rows = [
            {
                "order_id": 1,
                "sticker_barcode": "ABC",
                "sticker_number": "1",
                "sticker_part_a": "",
                "sticker_part_b": "",
            },
            {
                "order_id": 2,
                "sticker_barcode": "abc",
                "sticker_number": "2",
                "sticker_part_a": "",
                "sticker_part_b": "",
            },
        ]
        row, amb, matches = find_row_by_sticker(rows, "ABC")
        self.assertTrue(amb)
        self.assertIsNone(row)
        self.assertEqual(len(matches), 2)

    def test_fallback_part_b(self):
        rows = [
            {
                "order_id": 9,
                "sticker_barcode": "",
                "sticker_part_a": "5694",
                "sticker_part_b": "5806",
                "sticker_number": "56945806",
            }
        ]
        row, amb, _ = find_row_by_sticker(rows, "5806")
        self.assertFalse(amb)
        self.assertEqual(row["order_id"], 9)

    def test_normalize_and_key(self):
        self.assertEqual(normalize_scan(" a B "), "aB")
        self.assertEqual(scan_key("AbC"), "abc")


if __name__ == "__main__":
    unittest.main()
