"""Feedback product categories: seed defaults + rename sync."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from review_processor.repository import ReviewRepository


class ProductCategoriesRepoTests(unittest.TestCase):
    def test_default_category_names(self):
        names = ReviewRepository.DEFAULT_PRODUCT_CATEGORIES
        self.assertEqual(len(names), 5)
        self.assertTrue(any("Постельное белье" in n for n in names))

    def test_save_rejects_duplicate_names(self):
        repo = ReviewRepository.__new__(ReviewRepository)
        repo._sql = lambda sql: sql
        repo._connect = MagicMock()
        with self.assertRaises(ValueError):
            ReviewRepository.save_product_categories(
                repo,
                user_id=1,
                items=[
                    {"name": "A", "boxes_per_pallet": 1},
                    {"name": "a", "boxes_per_pallet": 2},
                ],
            )

    def test_save_rejects_negative_boxes(self):
        repo = ReviewRepository.__new__(ReviewRepository)
        repo._sql = lambda sql: sql
        with self.assertRaises(ValueError):
            ReviewRepository.save_product_categories(
                repo,
                user_id=1,
                items=[{"name": "A", "boxes_per_pallet": -1}],
            )


if __name__ == "__main__":
    unittest.main()
