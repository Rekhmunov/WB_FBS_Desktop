"""Product flag skip_kiz_gtin_check — skip GTIN↔ШК only for marked products."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class SkipKizGtinCheckMapTests(unittest.TestCase):
    @patch("review_processor.repository.ReviewRepository._connect")
    def test_map_keys_article_and_nmid(self, _connect_mock):
        from review_processor.repository import ReviewRepository

        repo = ReviewRepository.__new__(ReviewRepository)
        repo.list_product_photos = MagicMock(
            return_value=[
                {
                    "supplier_article": "ART-1",
                    "wb_nmid": "12345",
                    "skip_kiz_gtin_check": True,
                },
                {
                    "supplier_article": "ART-2",
                    "wb_nmid": "999",
                    "skip_kiz_gtin_check": False,
                },
            ]
        )
        m = ReviewRepository.get_product_skip_kiz_gtin_check_map(repo, user_id=1)
        self.assertTrue(m.get("ART-1"))
        self.assertTrue(m.get("art-1"))
        self.assertTrue(m.get("12345"))
        self.assertNotIn("ART-2", m)
        self.assertNotIn("999", m)

    @patch("review_processor.repository.ReviewRepository._connect")
    def test_add_product_photo_persists_flag(self, connect_mock):
        from review_processor.repository import ReviewRepository

        conn = MagicMock()
        connect_mock.return_value.__enter__.return_value = conn
        connect_mock.return_value.__exit__.return_value = False

        repo = ReviewRepository.__new__(ReviewRepository)
        repo._sql = lambda sql: sql
        repo._row_to_dict = lambda row: dict(row) if row else {}
        repo._insert_and_get_id = MagicMock(return_value=7)

        class _Row(dict):
            pass

        conn.execute.return_value.fetchone.return_value = _Row(
            id=7,
            name="Товар",
            skip_kiz_gtin_check=1,
        )

        item = ReviewRepository.add_product_photo(
            repo,
            user_id=1,
            name="Товар",
            supplier_article="A1",
            wb_nmid="1",
            ozon_sku="",
            photo_path=None,
            skip_kiz_gtin_check=True,
        )
        self.assertTrue(item.get("skip_kiz_gtin_check"))
        insert_sql = repo._insert_and_get_id.call_args[0][1]
        self.assertIn("skip_kiz_gtin_check", insert_sql)
        params = repo._insert_and_get_id.call_args[0][2]
        self.assertIn(1, params)


if __name__ == "__main__":
    unittest.main()
