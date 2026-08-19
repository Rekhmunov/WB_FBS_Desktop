"""WB FBS sync pallet summary (new + assembly only)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from review_processor.wb_fbs import (
    TAB_ASSEMBLY,
    TAB_NEW,
    compute_wb_fbs_pallet_summary,
    format_boxes_ru,
    format_pallets_ru,
)


class FormatPalletsRuTests(unittest.TestCase):
    def test_integers_and_fractions(self):
        self.assertEqual(format_pallets_ru(1), "1 паллета")
        self.assertEqual(format_pallets_ru(2), "2 паллеты")
        self.assertEqual(format_pallets_ru(5), "5 паллет")
        self.assertEqual(format_pallets_ru(1.25), "1,25 паллета")
        self.assertEqual(format_pallets_ru(10.0), "10 паллет")


class FormatBoxesRuTests(unittest.TestCase):
    def test_integers_and_fractions(self):
        self.assertEqual(format_boxes_ru(1), "1 короб")
        self.assertEqual(format_boxes_ru(2), "2 короба")
        self.assertEqual(format_boxes_ru(5), "5 коробов")
        self.assertEqual(format_boxes_ru(11), "11 коробов")
        self.assertEqual(format_boxes_ru(1.5), "1,5 короба")
        self.assertEqual(format_boxes_ru(10.0), "10 коробов")


class ComputePalletSummaryTests(unittest.TestCase):
    @patch("review_processor.wb_fbs.ensure_wb_fbs_tables")
    def test_example_one_full_pallet(self, _ensure):
        # 100 pcs, box_qty=10, boxes_per_pallet=10 → 10 boxes, 1.0 pallet
        repo = MagicMock()
        repo.list_product_photos.return_value = [
            {
                "supplier_article": "ART-1",
                "wb_nmid": "111",
                "box_qty": 10,
                "product_category": "Категория 1",
            }
        ]
        repo.list_product_categories.return_value = [
            {"name": "Категория 1", "boxes_per_pallet": 10}
        ]
        repo._sql = lambda sql: sql
        repo._row_to_dict = lambda row: dict(row)

        class _Result:
            def fetchall(self):
                return [
                    {"source_id": 7, "article": "ART-1", "nm_id": 111, "qty": 100},
                ]

        conn = MagicMock()
        conn.execute.return_value = _Result()
        repo._connect.return_value.__enter__.return_value = conn
        repo._connect.return_value.__exit__.return_value = False

        summary = compute_wb_fbs_pallet_summary(
            repo,
            user_id=1,
            sources=[{"source_id": 7, "name": "ФБС Склад 1"}],
        )
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["name"], "ФБС Склад 1")
        self.assertEqual(summary[0]["pallets"], 1.0)
        self.assertEqual(summary[0]["boxes"], 10.0)
        self.assertEqual(summary[0]["boxes_label"], "10 коробов")
        self.assertEqual(summary[0]["pallets_label"], "1 паллета (10 коробов)")

        # Query must restrict to new + assembly.
        sql = str(conn.execute.call_args[0][0])
        params = conn.execute.call_args[0][1]
        self.assertIn("tab IN", sql)
        self.assertIn(TAB_NEW, params)
        self.assertIn(TAB_ASSEMBLY, params)

    @patch("review_processor.wb_fbs.ensure_wb_fbs_tables")
    def test_fractional_rounding_to_hundredths(self, _ensure):
        # 25 pcs / 10 = 2.5 boxes; / 10 = 0.25 pallet
        repo = MagicMock()
        repo.list_product_photos.return_value = [
            {
                "supplier_article": "ART-1",
                "wb_nmid": "",
                "box_qty": 10,
                "product_category": "Категория 1",
            }
        ]
        repo.list_product_categories.return_value = [
            {"name": "Категория 1", "boxes_per_pallet": 10}
        ]
        repo._sql = lambda sql: sql
        repo._row_to_dict = lambda row: dict(row)

        class _Result:
            def fetchall(self):
                return [
                    {"source_id": 1, "article": "ART-1", "nm_id": None, "qty": 25},
                ]

        conn = MagicMock()
        conn.execute.return_value = _Result()
        repo._connect.return_value.__enter__.return_value = conn
        repo._connect.return_value.__exit__.return_value = False

        summary = compute_wb_fbs_pallet_summary(
            repo,
            user_id=1,
            sources=[{"source_id": 1, "name": "Источник А"}],
        )
        self.assertEqual(summary[0]["pallets"], 0.25)
        self.assertEqual(summary[0]["boxes"], 2.5)
        self.assertEqual(summary[0]["pallets_label"], "0,25 паллета (2,5 короба)")

    @patch("review_processor.wb_fbs.ensure_wb_fbs_tables")
    def test_box_qty_without_category_counts_boxes_only(self, _ensure):
        # Has box_qty but no boxes_per_pallet → boxes yes, pallets 0
        repo = MagicMock()
        repo.list_product_photos.return_value = [
            {
                "supplier_article": "ART-2",
                "wb_nmid": "",
                "box_qty": 5,
                "product_category": "",
            }
        ]
        repo.list_product_categories.return_value = []
        repo._sql = lambda sql: sql
        repo._row_to_dict = lambda row: dict(row)

        class _Result:
            def fetchall(self):
                return [
                    {"source_id": 3, "article": "ART-2", "nm_id": None, "qty": 20},
                ]

        conn = MagicMock()
        conn.execute.return_value = _Result()
        repo._connect.return_value.__enter__.return_value = conn
        repo._connect.return_value.__exit__.return_value = False

        summary = compute_wb_fbs_pallet_summary(
            repo,
            user_id=1,
            sources=[{"source_id": 3, "name": "ФБС Склад 2"}],
        )
        self.assertEqual(summary[0]["boxes"], 4.0)
        self.assertEqual(summary[0]["pallets"], 0.0)
        self.assertEqual(summary[0]["pallets_label"], "0 паллет (4 короба)")


if __name__ == "__main__":
    unittest.main()
