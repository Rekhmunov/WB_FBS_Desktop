# -*- coding: utf-8 -*-
"""Product photo changes must refresh order/Ozon catalog caches."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.db import Database
from app.paths import reset_migrate_state_for_tests, resolve_product_photo_path
from app.services.catalog import (
    ProductService,
    clear_catalog_listeners_for_tests,
)
from app.services.orders import OrdersService
from app.services.ozon_orders import OzonOrdersService


class CatalogPhotoCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        clear_catalog_listeners_for_tests()
        reset_migrate_state_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self._env = mock.patch.dict(
            os.environ,
            {
                "FEEDPILOT_DATA_DIR": str(self.data),
                "FEEDPILOT_PROGRAM_DIR": str(self.root),
            },
            clear=False,
        )
        self._env.start()
        self.db = Database(str(self.root / "t.sqlite3"))
        self.db.init_schema()
        self.products = ProductService(self.db)
        self.orders = OrdersService(self.db)
        self.ozon = OzonOrdersService(self.db)

    def tearDown(self) -> None:
        self._env.stop()
        clear_catalog_listeners_for_tests()
        reset_migrate_state_for_tests()
        self._tmp.cleanup()

    def test_save_photo_invalidates_orders_cache(self) -> None:
        img = Path(self._tmp.name) / "a.jpg"
        img.write_bytes(b"fake-jpeg")
        pid = self.products.save(
            None, "Товар", "ART-1", "111", None, "", False, str(img)
        )
        by_art, _by_nm, photo_by = self.orders._product_maps()
        self.assertIn("art-1", by_art)
        self.assertTrue(photo_by.get("art-1"))
        # Warm cache, then change photo
        img2 = Path(self._tmp.name) / "b.jpg"
        img2.write_bytes(b"fake-jpeg-2")
        self.products.save(
            pid, "Товар", "ART-1", "111", None, "", False, str(img2)
        )
        _a, _n, photo_by2 = self.orders._product_maps()
        self.assertNotEqual(photo_by.get("art-1"), photo_by2.get("art-1"))
        self.assertTrue(str(photo_by2.get("art-1") or "").endswith("b.jpg"))

    def test_save_invalidates_ozon_photo_map(self) -> None:
        img = Path(self._tmp.name) / "o.jpg"
        img.write_bytes(b"x")
        self.products.save(
            None,
            "Озон",
            "OFFER",
            "",
            None,
            "",
            False,
            str(img),
            ozon_sku="999",
        )
        first = self.ozon._photo_map()
        self.assertTrue(first.get("999") or first.get("offer"))
        # Mutate catalog → cache must rebuild
        self.products.save(
            None, "Другой", "OFFER2", "", None, "", False, str(img), ozon_sku="888"
        )
        second = self.ozon._photo_map()
        self.assertIn("888", second)

    def test_resolve_falls_back_to_photos_dir(self) -> None:
        from app.paths import photos_dir

        photos = photos_dir()
        name = "fallback_photo_test.jpg"
        dest = photos / name
        dest.write_bytes(b"img")
        missing = Path(self._tmp.name) / "gone" / name
        resolved = resolve_product_photo_path(str(missing))
        self.assertEqual(Path(resolved).resolve(), dest.resolve())
        self.assertEqual(resolve_product_photo_path(""), "")
        self.assertEqual(resolve_product_photo_path(None), "")


if __name__ == "__main__":
    unittest.main()
