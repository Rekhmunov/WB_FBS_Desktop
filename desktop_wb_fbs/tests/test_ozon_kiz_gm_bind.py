# -*- coding: utf-8 -*-
"""Ozon FBS KIZ/pick → грузоместо bind parity (archive ozon_fbs_container_bind)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QLabel

from app.db import Database
from app.ozon import utc_now
from app.services.catalog import ProductService
from app.services.ozon_mark_pick import OzonMarkService
from app.ui.format_helpers import build_ozon_posting_cell_widget
from app.ui.ozon_container_bind import (
    match_container,
    row_container_ok,
    row_has_known_sticker,
)


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class MatchContainerTests(unittest.TestCase):
    def test_match_by_id_and_barcode(self) -> None:
        rows = [
            {
                "container_id": 202174459906000,
                "container_barcode": "202174459906000",
                "container_number": "12",
            }
        ]
        self.assertEqual(
            match_container(rows, "202174459906000")["container_id"],
            202174459906000,
        )
        self.assertIsNone(match_container(rows, "999"))
        # Short numbers must not match accidentally.
        self.assertIsNone(match_container(rows, "12"))

    def test_match_long_number(self) -> None:
        rows = [{"container_id": 1, "container_number": "12345"}]
        self.assertEqual(match_container(rows, "12345")["container_id"], 1)


class RowHelpersTests(unittest.TestCase):
    def test_sticker_and_container_ok(self) -> None:
        self.assertTrue(
            row_has_known_sticker({"sticker_scanit": "iiABCDEF1234"})
        )
        self.assertFalse(row_has_known_sticker({}))
        self.assertTrue(row_container_ok({"container_barcode": "10"}))
        self.assertTrue(row_container_ok({"container_id": 10}))
        self.assertFalse(row_container_ok({}))


class MarkRowViewStickerGmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO supply_sources(
                    name, marketplace, client_id, api_key, is_enabled,
                    lookback_days, created_at
                ) VALUES ('Озон', 'ozon', '1', 'k', 1, 2, '2026-01-01')
                """
            )
            self.source_id = int(
                conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
            now = utc_now()
            conn.execute(
                """
                INSERT INTO ozon_fbs_postings(
                    source_id, posting_number, status, tab, offer_id,
                    sku, created_at_wb, sticker_scanit, sticker_barcode,
                    container_id, container_barcode, container_synced,
                    marks_json, synced_at
                ) VALUES (
                    ?, '555-0001-1', 'awaiting_packaging', 'packaging',
                    'ART-1', '100', ?, 'iiABCDEF1234', 'BC1',
                    42, '42', 1, '[]', ?
                )
                """,
                (self.source_id, now, now),
            )
            conn.commit()
        ProductService(self.db).save(
            None, "Товар КИЗ", "ART-1", "", None, "", False, requires_kiz=True
        )
        self.mark = OzonMarkService(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_row_view_exposes_scanit_and_container(self) -> None:
        row = self.mark.single_marking_row(self.source_id, "555-0001-1")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["sticker_scanit"], "iiABCDEF1234")
        self.assertEqual(row["sticker_barcode"], "BC1")
        self.assertEqual(row["container_id"], 42)
        self.assertEqual(row["container_barcode"], "42")
        self.assertTrue(row["container_synced"])
        self.assertTrue(row.get("created_date") or row.get("created_at_wb"))


class PostingCellScanitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _app()

    def test_cell_shows_ii_and_date(self) -> None:
        w = build_ozon_posting_cell_widget(
            {
                "posting_number": "555-0001-1",
                "sticker_scanit": "iiABCDEF1234",
                "created_date": "21.11",
            }
        )
        texts = [c.text() for c in w.findChildren(QLabel)]
        self.assertIn("555-0001-1", texts)
        self.assertTrue(any("iiABCDEF" in t or t == "1234" for t in texts))
        self.assertIn("от 21.11", texts)

    def test_cell_dash_date_when_empty(self) -> None:
        w = build_ozon_posting_cell_widget({"posting_number": "1-1-1"})
        texts = [c.text() for c in w.findChildren(QLabel)]
        self.assertIn("от —", texts)


if __name__ == "__main__":
    unittest.main()
