# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QCheckBox, QLabel, QToolButton

from app.db import Database
from app.ozon import utc_now
from app.services.ozon_supplies import OzonSuppliesService
from app.ui.ozon_modal_chrome import (
    build_ozon_modal_posting_cell,
    build_ozon_modal_row_menu,
    exclusive_filled_empty,
    row_is_cancelled,
    set_filter_count,
    supply_id_from_carriage,
)


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class OzonModalChromeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _app()

    def test_filter_count_label(self) -> None:
        cb = QCheckBox()
        set_filter_count(cb, "Заполненные", 3)
        self.assertEqual(cb.text(), "Заполненные (3)")

    def test_exclusive_filled_empty(self) -> None:
        filled = QCheckBox()
        empty = QCheckBox()
        filled.setChecked(True)
        empty.setChecked(True)
        exclusive_filled_empty(filled, empty, which="filled")
        self.assertTrue(filled.isChecked())
        self.assertFalse(empty.isChecked())

    def test_row_cancelled(self) -> None:
        self.assertTrue(row_is_cancelled({"tab": "cancelled"}))
        self.assertTrue(row_is_cancelled({"cancel_reason": "buyer"}))
        self.assertFalse(row_is_cancelled({"status": "awaiting_deliver"}))

    def test_posting_cell_has_icons(self) -> None:
        w = build_ozon_modal_posting_cell(
            {
                "posting_number": "1-1-1",
                "sticker_scanit": "iiABCDEF1234",
                "created_date": "21.11",
            },
            on_copy=lambda _pn: None,
            on_refresh=lambda _pn: None,
        )
        icons = w.findChildren(QToolButton, "ozonPostingIconBtn")
        self.assertEqual(len(icons), 2)
        texts = [c.text() for c in w.findChildren(QLabel)]
        self.assertIn("1-1-1", texts)
        self.assertIn("от 21.11", texts)

    def test_row_menu_print_and_remove(self) -> None:
        w = build_ozon_modal_row_menu(
            {"posting_number": "1-1-1", "cancelled": True},
            can_move_gm=True,
            on_print=lambda _pn: None,
            on_move_gm=lambda _pn: None,
            on_remove_cancelled=lambda _pn: None,
        )
        btn = w.findChild(QToolButton, "kizRowMenu")
        self.assertIsNotNone(btn)
        assert btn is not None
        menu = btn.menu()
        self.assertIsNotNone(menu)
        assert menu is not None
        titles = [a.text() for a in menu.actions()]
        self.assertIn("Переместить в другое ГМ", titles)
        self.assertIn("Напечатать стикер", titles)
        self.assertIn("Удалить товар из поставки", titles)

    def test_supply_id_from_carriage(self) -> None:
        self.assertEqual(supply_id_from_carriage("supply:OZ-1"), "OZ-1")
        self.assertEqual(supply_id_from_carriage("123"), "")


class RemoveCancelledFromSupplyTests(unittest.TestCase):
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
                INSERT INTO ozon_fbs_supplies(
                    source_id, supply_id, name, posting_numbers_json,
                    created_at, updated_at
                ) VALUES (?, 'OZ-1', 'Main', '["C-1"]', ?, ?)
                """,
                (self.source_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO ozon_fbs_postings(
                    source_id, posting_number, status, tab, supply_id,
                    cancel_reason, synced_at
                ) VALUES (?, 'C-1', 'cancelled', 'cancelled', 'OZ-1', 'buyer', ?)
                """,
                (self.source_id, now),
            )
            conn.commit()
        self.svc = OzonSuppliesService(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_remove_cancelled(self) -> None:
        out = self.svc.remove_cancelled_posting_from_supply(
            self.source_id, "OZ-1", "C-1"
        )
        self.assertTrue(out["ok"])
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT supply_id FROM ozon_fbs_postings WHERE posting_number='C-1'"
            ).fetchone()
            self.assertEqual(str(row["supply_id"] or ""), "")

    def test_remove_split_pending_cancelled(self) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE ozon_fbs_supplies
                SET posting_numbers_json = '["C-1","C-2"]'
                WHERE supply_id = 'OZ-1'
                """
            )
            conn.execute(
                """
                INSERT INTO ozon_fbs_postings(
                    source_id, posting_number, status, tab, supply_id,
                    cancel_reason, synced_at
                ) VALUES (?, 'C-2', 'cancelled_from_split_pending',
                          'cancelled', 'OZ-1', '', ?)
                """,
                (self.source_id, now),
            )
            conn.commit()
        out = self.svc.remove_cancelled_posting_from_supply(
            self.source_id, "OZ-1", "C-2"
        )
        self.assertTrue(out["ok"])


class CancelledScanGuardsTests(unittest.TestCase):
    def test_row_is_cancelled_split_pending(self) -> None:
        self.assertTrue(
            row_is_cancelled({"status": "cancelled_from_split_pending"})
        )


if __name__ == "__main__":
    unittest.main()
