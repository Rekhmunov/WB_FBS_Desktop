# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.db import Database
from app.services.ozon_act import OzonActService


class OzonWarehouseBarcodeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.svc = OzonActService(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fetch_warehouse_barcode_uses_warehouse_id(self) -> None:
        client = MagicMock()
        client.act_get_barcode.return_value = (b"\x89PNG\r\n", "wh.png")
        content, name = self.svc.fetch_warehouse_barcode(client, 1020005028015630)
        client.act_get_barcode.assert_called_once_with(1020005028015630)
        self.assertEqual(content[:4], b"\x89PNG")
        self.assertTrue(name)

    def test_fetch_warehouse_barcode_rejects_empty(self) -> None:
        client = MagicMock()
        with self.assertRaises(RuntimeError):
            self.svc.fetch_warehouse_barcode(client, 0)


if __name__ == "__main__":
    unittest.main()
