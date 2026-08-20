# -*- coding: utf-8 -*-
import unittest

from app.services.print_docs import render_picking_list_html


class ExtendedPickingDupHighlightTests(unittest.TestCase):
    def test_duplicate_part_b_gets_rectangle_class(self) -> None:
        groups = [
            {
                "product_name": "Куртка",
                "article": "A1",
                "qty": 2,
                "orders": [
                    {"order_id": 1, "sticker_part_a": "1111", "sticker_part_b": "5731"},
                    {"order_id": 2, "sticker_part_a": "2222", "sticker_part_b": "9999"},
                ],
            },
            {
                "product_name": "Шапка",
                "article": "A2",
                "qty": 1,
                "orders": [
                    {"order_id": 3, "sticker_part_a": "3333", "sticker_part_b": "5731"},
                ],
            },
        ]
        html = render_picking_list_html("WB-1", "Test", groups, variant="extended")
        self.assertIn('class="partb partb-dup"', html)
        self.assertIn("5731", html)
        # Unique partB must not get the dup rectangle class.
        self.assertIn('class="partb"', html)
        # Exactly two dup occurrences for 5731.
        self.assertEqual(html.count("partb-dup"), 2)

    def test_summary_has_no_partb_dup(self) -> None:
        groups = [
            {
                "product_name": "Куртка",
                "qty": 2,
                "orders": [
                    {"order_id": 1, "sticker_part_a": "1", "sticker_part_b": "1111"},
                    {"order_id": 2, "sticker_part_a": "2", "sticker_part_b": "1111"},
                ],
            }
        ]
        html = render_picking_list_html("WB-1", "Test", groups, variant="summary")
        self.assertNotIn("partb-dup", html)


if __name__ == "__main__":
    unittest.main()
