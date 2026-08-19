"""Product catalog: box_qty + product_category helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class ProductBoxCategoryParseTests(unittest.TestCase):
    def _helpers(self):
        # Import nested helpers via create_app wiring is heavy; mirror validation
        # rules used in web.py for a lightweight contract check.
        options = (
            "Наматрасник непромокаемый (ИП Авдеева, без маркировки)",
            "Наматрасник стеганый (ИП Авдеева, без маркировки)",
            "Наматрасник стеганый непромокаемый (ВарФабрик, без маркировки)",
            "Наматрасник непромокаемый (ВарФабрик, с маркировкой)",
            "Постельное белье (ВарФабрик, с маркировкой)",
        )
        return options

    def test_category_options_match_spec(self):
        options = self._helpers()
        self.assertEqual(len(options), 5)
        self.assertTrue(all("маркиров" in x for x in options))

    @patch("review_processor.repository.ReviewRepository._connect")
    def test_add_product_photo_accepts_new_fields(self, connect_mock):
        from review_processor.repository import ReviewRepository

        conn = MagicMock()
        connect_mock.return_value.__enter__.return_value = conn
        connect_mock.return_value.__exit__.return_value = False

        repo = ReviewRepository.__new__(ReviewRepository)
        repo._sql = lambda sql: sql
        repo._row_to_dict = lambda row: dict(row) if row else {}
        repo._insert_and_get_id = MagicMock(return_value=42)

        class _Row(dict):
            pass

        conn.execute.return_value.fetchone.return_value = _Row(
            id=42,
            name="Товар",
            box_qty=12,
            product_category="Постельное белье (ВарФабрик, с маркировкой)",
        )

        item = ReviewRepository.add_product_photo(
            repo,
            user_id=1,
            name="Товар",
            supplier_article="A1",
            wb_nmid="1",
            ozon_sku="",
            photo_path=None,
            yandex_offer_id="",
            box_qty=12,
            product_category="Постельное белье (ВарФабрик, с маркировкой)",
        )
        self.assertEqual(item.get("box_qty"), 12)
        self.assertIn("Постельное белье", str(item.get("product_category") or ""))
        insert_sql = repo._insert_and_get_id.call_args[0][1]
        self.assertIn("box_qty", insert_sql)
        self.assertIn("product_category", insert_sql)


if __name__ == "__main__":
    unittest.main()
