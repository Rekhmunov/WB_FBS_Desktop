# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from app.ui.format_helpers import scan_has_ru_layout


class ScanRuLayoutTests(unittest.TestCase):
    def test_detects_cyrillic_letters(self):
        self.assertTrue(scan_has_ru_layout("5694580йцу"))

    def test_allows_latin_and_digits(self):
        self.assertFalse(scan_has_ru_layout("56945806632"))
        self.assertFalse(scan_has_ru_layout("0104604060004010215ABC"))

    def test_empty_is_allowed(self):
        self.assertFalse(scan_has_ru_layout(""))
        self.assertFalse(scan_has_ru_layout("   "))


class BlockRuLayoutScanTests(unittest.TestCase):
    @patch("app.ui.dialog_utils.RuLayoutWarningDialog")
    def test_uses_dedicated_dialog_not_messagebox(self, dlg_cls):
        from app.ui.dialog_utils import block_ru_layout_scan

        dlg_cls.return_value.exec_.return_value = 0
        parent = MagicMock()
        field = MagicMock()
        field.text.return_value = "йцу"
        self.assertTrue(block_ru_layout_scan(parent, field))
        dlg_cls.assert_called_once()
        field.clear.assert_called_once()


if __name__ == "__main__":
    unittest.main()
