# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from app.services.ozon_stickers_print import (
    group_orders_by_article,
    render_stickers_print_html,
)


class GroupByArticleTest(unittest.TestCase):
    def test_groups_and_sorts(self) -> None:
        orders = [
            {
                "posting_number": "2-1",
                "offer_id": "B",
                "product_name": "Бета",
                "sku": "20",
                "barcodes": ["222"],
                "status": "awaiting_deliver",
                "tab": "awaiting_deliver",
            },
            {
                "posting_number": "1-1",
                "offer_id": "A",
                "product_name": "Альфа",
                "sku": "10",
                "barcodes": ["111"],
                "status": "awaiting_deliver",
                "tab": "awaiting_deliver",
            },
            {
                "posting_number": "1-2",
                "offer_id": "A",
                "product_name": "Альфа",
                "sku": "10",
                "barcodes": ["111"],
                "status": "awaiting_deliver",
                "tab": "awaiting_deliver",
            },
            {
                "posting_number": "X-1",
                "offer_id": "Z",
                "product_name": "Отмена",
                "status": "cancelled",
                "tab": "cancelled",
            },
        ]
        groups = group_orders_by_article(orders)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["article"], "A")
        self.assertEqual(groups[0]["qty"], 2)
        self.assertEqual(
            [o["posting_number"] for o in groups[0]["orders"]], ["1-1", "1-2"]
        )
        self.assertEqual(groups[1]["article"], "B")


class RenderHtmlTest(unittest.TestCase):
    def test_cover_separator_and_sticker(self) -> None:
        detail = {
            "supply_id": "OZ-1",
            "name": "Поставка тест",
            "orders": [
                {
                    "posting_number": "PN-1",
                    "offer_id": "ART-1",
                    "product_name": "Товар один",
                    "sku": "99",
                    "barcodes": ["460123"],
                    "status": "awaiting_deliver",
                    "tab": "awaiting_deliver",
                },
                {
                    "posting_number": "PN-2",
                    "offer_id": "ART-1",
                    "product_name": "Товар один",
                    "sku": "99",
                    "barcodes": ["460123"],
                    "status": "awaiting_deliver",
                    "tab": "awaiting_deliver",
                },
            ],
        }
        # Minimal 1x1 PNG
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        images = {"PN-1": [png_b64], "PN-2": [png_b64]}
        html = render_stickers_print_html(
            detail,
            images,
            source_name="Магазин",
            include_cover_and_separators=True,
        )
        self.assertIn('class="label cover"', html)
        self.assertIn("Поставка тест", html)
        self.assertIn('class="label separator"', html)
        self.assertIn("Артикул: ART-1", html)
        self.assertIn("2 шт.", html)
        self.assertEqual(html.count('class="label sticker"'), 2)
        self.assertIn("data:image/png;base64,", html)
        self.assertNotIn('class="label missing"', html)

    def test_missing_label_placeholder(self) -> None:
        detail = {
            "name": "S",
            "orders": [
                {
                    "posting_number": "PN-9",
                    "offer_id": "X",
                    "product_name": "Y",
                    "status": "awaiting_deliver",
                    "tab": "awaiting_deliver",
                }
            ],
        }
        html = render_stickers_print_html(
            detail, {}, include_cover_and_separators=True
        )
        self.assertIn("Нет этикетки", html)
        self.assertIn("PN-9", html)


if __name__ == "__main__":
    unittest.main()
