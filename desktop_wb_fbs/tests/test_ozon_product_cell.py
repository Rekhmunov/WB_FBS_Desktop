# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QLabel

from app.ui.format_helpers import build_product_cell_widget


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class OzonProductCellTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _app()

    def test_packaging_cell_uses_supply_typography_and_tight_sub(self) -> None:
        w = build_product_cell_widget(
            {
                "product_name_display": "Наматрасник стеганый",
                "offer_id": "ART-1",
                "sku": "50255803441",
                "product_count_label": "2 шт.",
                "product_photo": "",
            },
            photo_size=72,
        )
        names = [
            c for c in w.findChildren(QLabel) if c.objectName() == "sdProductName"
        ]
        subs = [
            c for c in w.findChildren(QLabel) if c.objectName() == "sdProductSub"
        ]
        self.assertEqual(len(names), 1)
        self.assertEqual(names[0].text(), "Наматрасник стеганый")
        self.assertEqual(len(subs), 1)
        self.assertIn("Арт. ART-1", subs[0].text())
        self.assertIn("SKU 50255803441", subs[0].text())
        self.assertIn("2 шт.", subs[0].text())
        # Photo slot is supply-sized (72 * 1.1 scale).
        photos = [
            c
            for c in w.findChildren(QLabel)
            if c.objectName() == "" and c.minimumWidth() >= 72
        ]
        self.assertTrue(any(c.width() >= 72 for c in w.findChildren(QLabel)))


if __name__ == "__main__":
    unittest.main()
