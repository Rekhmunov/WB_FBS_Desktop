# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.services.catalog import ProductService


class ProductImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "t.sqlite"))
        self.db.init_schema()
        self.svc = ProductService(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_schema_has_ozon_and_yandex(self) -> None:
        with self.db.connect() as conn:
            cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(product_photos)").fetchall()
            }
        self.assertIn("ozon_sku", cols)
        self.assertIn("yandex_offer_id", cols)

    def test_import_csv_creates_and_updates(self) -> None:
        csv_path = Path(self.tmp.name) / "products.csv"
        csv_path.write_text(
            "\n".join(
                [
                    "Наименование товара;Артикул продавца;Артикул WB (nmId);"
                    "SKU Ozon;Артикул Яндекс Маркет (offerId);"
                    "Кратность в коробе;Категория товара;Без проверки GTIN маркировки",
                    "Товар А;ART-1;111;OZ-1;YM-1;12;Одежда;да",
                    "Товар Б;ART-2;222;OZ-2;YM-2;3;Обувь;",
                ]
            ),
            encoding="utf-8-sig",
        )
        stats = self.svc.import_csv(str(csv_path))
        self.assertEqual(stats["created"], 2)
        rows = self.svc.list_all()
        self.assertEqual(len(rows), 2)
        a = next(r for r in rows if r["supplier_article"] == "ART-1")
        self.assertEqual(a["wb_nmid"], "111")
        self.assertEqual(a["ozon_sku"], "OZ-1")
        self.assertEqual(a["yandex_offer_id"], "YM-1")
        self.assertEqual(a["box_qty"], 12)
        self.assertEqual(a["product_category"], "Одежда")
        self.assertTrue(a["skip_kiz_gtin_check"])

        csv_path.write_text(
            "\n".join(
                [
                    "Наименование товара;Артикул продавца;Артикул WB (nmId);"
                    "SKU Ozon;Артикул Яндекс Маркет (offerId);"
                    "Кратность в коробе;Категория товара;Без проверки GTIN маркировки",
                    "Товар А обновлён;ART-1;111;OZ-9;YM-9;20;Одежда;",
                ]
            ),
            encoding="utf-8-sig",
        )
        stats2 = self.svc.import_csv(str(csv_path))
        self.assertEqual(stats2["updated"], 1)
        a2 = self.svc.get(int(a["id"]))
        assert a2 is not None
        self.assertEqual(a2["name"], "Товар А обновлён")
        self.assertEqual(a2["ozon_sku"], "OZ-9")
        self.assertEqual(a2["box_qty"], 20)
        self.assertFalse(a2["skip_kiz_gtin_check"])


if __name__ == "__main__":
    unittest.main()
