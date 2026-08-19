# -*- coding: utf-8 -*-
import unittest

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


if __name__ == "__main__":
    unittest.main()
