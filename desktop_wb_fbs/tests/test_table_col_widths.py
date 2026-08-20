# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import MagicMock

from app.ui.table_col_widths import normalize_defaults, parse_saved_widths


class ParseSavedWidthsTests(unittest.TestCase):
    def test_valid_json(self) -> None:
        raw = json.dumps([40, 200, 420, 52])
        self.assertEqual(parse_saved_widths(raw, 4), [40, 200, 420, 52])

    def test_clamps_min_width(self) -> None:
        raw = json.dumps([10, 200, 5, 52])
        self.assertEqual(parse_saved_widths(raw, 4, min_width=32), [32, 200, 32, 52])

    def test_rejects_wrong_length(self) -> None:
        self.assertIsNone(parse_saved_widths("[1,2]", 4))

    def test_rejects_invalid(self) -> None:
        self.assertIsNone(parse_saved_widths("not-json", 2))
        self.assertIsNone(parse_saved_widths("", 2))

    def test_normalize_defaults_pads(self) -> None:
        self.assertEqual(normalize_defaults([40, 200], 4), [40, 200, 100, 100])


class PersistViaDbMockTests(unittest.TestCase):
    def test_roundtrip_settings_key(self) -> None:
        db = MagicMock()
        db.get_setting.return_value = json.dumps([80, 160, 300, 48])
        raw = db.get_setting("supply_detail_table_cols", "")
        widths = parse_saved_widths(raw, 4)
        self.assertEqual(widths, [80, 160, 300, 48])
        db.set_setting("supply_detail_table_cols", json.dumps(widths, separators=(",", ":")))
        db.set_setting.assert_called_with(
            "supply_detail_table_cols", "[80,160,300,48]"
        )


if __name__ == "__main__":
    unittest.main()
