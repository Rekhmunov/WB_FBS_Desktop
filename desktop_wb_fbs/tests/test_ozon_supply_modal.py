# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QLabel, QMessageBox, QTableWidgetItem

from app.db import Database
from app.ozon import utc_now
from app.services.catalog import ProductService
from app.services.ozon_supplies import OzonSuppliesService
from app.ui.format_helpers import build_ozon_posting_cell_widget


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class OzonSupplyPostingCellTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _app()

    def test_posting_cell_shows_number_once(self) -> None:
        w = build_ozon_posting_cell_widget(
            {
                "posting_number": "12345-0001-1",
                "sticker_scanit": "iiABCDEF1234",
                "created_date": "21.11",
            }
        )
        labels = [
            c.text()
            for c in w.findChildren(QLabel)
            if c.objectName() == "sdOrderId"
        ]
        self.assertEqual(labels, ["12345-0001-1"])

    def test_supply_table_item_text_empty_under_widget(self) -> None:
        """Ghost duplicate: non-empty QTableWidgetItem under cell widget."""
        from PyQt5.QtWidgets import QTableWidget

        table = QTableWidget(1, 1)
        pnum = "12345-0001-1"
        item = QTableWidgetItem("")
        item.setData(Qt.UserRole, pnum)
        table.setItem(0, 0, item)
        table.setCellWidget(
            0,
            0,
            build_ozon_posting_cell_widget(
                {
                    "posting_number": pnum,
                    "sticker_scanit": "iiABCDEF1234",
                }
            ),
        )
        stored = table.item(0, 0)
        self.assertEqual(stored.text(), "")
        self.assertEqual(stored.data(Qt.UserRole), pnum)
        self.assertIsNotNone(table.cellWidget(0, 0))


class OzonKizPickBtnCountsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _app()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.svc = OzonSuppliesService(self.db)
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
                    product_name, warehouse_id, warehouse_name,
                    marks_json, marks_synced, pick_verified, pick_barcode,
                    synced_at
                ) VALUES
                (?, 'KIZ-OK', 'awaiting_deliver', 'awaiting_deliver', 'A',
                 'KIZ', 10, 'WH', '["mark1"]', 1, 0, '', ?),
                (?, 'KIZ-EMPTY', 'awaiting_deliver', 'awaiting_deliver', 'B',
                 'KIZ2', 10, 'WH', '[]', 0, 0, '', ?),
                (?, 'PICK-OK', 'awaiting_deliver', 'awaiting_deliver', 'C',
                 'Plain', 10, 'WH', '[]', 0, 1, '4600000000000', ?),
                (?, 'PICK-EMPTY', 'awaiting_deliver', 'awaiting_deliver', 'D',
                 'Plain2', 10, 'WH', '[]', 0, 0, '', ?)
                """,
                (self.source_id, now, self.source_id, now,
                 self.source_id, now, self.source_id, now),
            )
            conn.commit()
        # Catalog «Требует КИЗ» drives Ozon partition (archive parity).
        products = ProductService(self.db)
        products.save(
            None, "KIZ", "A", "", None, "", False, requires_kiz=True
        )
        products.save(
            None, "KIZ2", "B", "", None, "", False, requires_kiz=True
        )
        products.save(
            None, "Plain", "C", "", None, "", False, requires_kiz=False
        )
        products.save(
            None, "Plain2", "D", "", None, "", False, requires_kiz=False
        )
        self.sid = self.svc.create_supply(
            self.source_id,
            name="Main",
            warehouse_id=10,
            warehouse_name="WH",
            posting_numbers=["KIZ-OK", "KIZ-EMPTY", "PICK-OK", "PICK-EMPTY"],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_btn_count_label_format(self) -> None:
        _app()
        from PyQt5.QtWidgets import QPushButton

        from app.ui.ozon_supply_detail import OzonSupplyDetailDialog

        btn = QPushButton()
        OzonSupplyDetailDialog._set_action_btn_count(btn, "Товары с КИЗ", 1, 4)
        self.assertEqual(btn.text(), "Товары с КИЗ (1/4)")
        OzonSupplyDetailDialog._set_action_btn_count(btn, "Товары без КИЗ", 0, 0)
        self.assertEqual(btn.text(), "Товары без КИЗ")

    def test_kiz_pick_progress_counts(self) -> None:
        from app.services.ozon_mark_pick import OzonMarkService, OzonPickService
        from app.ui.ozon_supply_detail import OzonSupplyDetailDialog

        _app()
        dlg = OzonSupplyDetailDialog.__new__(OzonSupplyDetailDialog)
        dlg.source_id = self.source_id
        dlg.supply_id = self.sid
        dlg.mark = OzonMarkService(self.db)
        dlg.pick = OzonPickService(self.db)
        dlg._detail = {"name": "Main"}
        dlg._rows = [
            {"posting_number": "KIZ-OK", "tab": "awaiting_deliver", "status": "awaiting_deliver"},
            {"posting_number": "KIZ-EMPTY", "tab": "awaiting_deliver", "status": "awaiting_deliver"},
            {"posting_number": "PICK-OK", "tab": "awaiting_deliver", "status": "awaiting_deliver"},
            {"posting_number": "PICK-EMPTY", "tab": "awaiting_deliver", "status": "awaiting_deliver"},
        ]
        dlg._posting_numbers = lambda: [
            "KIZ-OK", "KIZ-EMPTY", "PICK-OK", "PICK-EMPTY"
        ]
        prog = OzonSupplyDetailDialog._kiz_pick_btn_progress(dlg)
        # Mark modal: 2 catalog KIZ products; 1 filled with marks.
        self.assertEqual(prog["kiz"]["total"], 2)
        self.assertEqual(prog["kiz"]["done"], 1)
        # Pick modal: 2 without «Требует КИЗ»; 1 verified with barcode.
        self.assertEqual(prog["pick"]["total"], 2)
        self.assertEqual(prog["pick"]["done"], 1)


class OzonForceMoveDeliveringTest(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.svc = OzonSuppliesService(self.db)
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
                    product_name, warehouse_id, warehouse_name, synced_at
                ) VALUES (?, 'P-1', 'awaiting_deliver', 'awaiting_deliver',
                          'ART', 'Товар', 10, 'WH', ?)
                """,
                (self.source_id, now),
            )
            conn.commit()
        self.sid = self.svc.create_supply(
            self.source_id,
            name="Main",
            warehouse_id=10,
            warehouse_name="WH",
            posting_numbers=["P-1"],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_mark_delivering_works_without_gates(self) -> None:
        """Force path calls the same mark_delivering regardless of blockers."""
        self.svc.mark_delivering(self.source_id, self.sid)
        supply = self.svc.get_supply(self.source_id, self.sid)
        self.assertEqual(int(supply.get("delivering_listed") or 0), 1)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT tab, status FROM ozon_fbs_postings "
                "WHERE posting_number='P-1'"
            ).fetchone()
        self.assertEqual(row["tab"], "delivering")
        self.assertEqual(row["status"], "delivering")

    def test_move_dialog_offers_force_when_blocked(self) -> None:
        _app()
        from app.ui.ozon_supply_detail import OzonSupplyDetailDialog

        dlg = OzonSupplyDetailDialog.__new__(OzonSupplyDetailDialog)
        dlg.db = self.db
        dlg.source_id = self.source_id
        dlg.supply_id = self.sid
        dlg.client = MagicMock()
        dlg.supplies = self.svc
        dlg.mark = MagicMock()
        dlg.pick = MagicMock()
        dlg._detail = {"name": "Main"}
        dlg._force_read_only = False
        dlg.supply_mutated = False
        dlg._is_read_only = MagicMock(return_value=False)
        dlg._move_delivering_blockers = MagicMock(
            return_value=["Водитель не выбран"]
        )
        dlg.reload = MagicMock()

        box = MagicMock()
        force_btn = object()
        box.addButton = MagicMock(
            side_effect=[MagicMock(name="cancel"), force_btn]
        )
        box.clickedButton = MagicMock(return_value=force_btn)
        box.exec_ = MagicMock()

        with patch(
            "app.ui.ozon_supply_detail.QMessageBox", return_value=box
        ) as mb_cls:
            # Constructor path: QMessageBox(self) then methods on instance.
            mb_cls.side_effect = None
            mb_cls.return_value = box
            mb_cls.Warning = QMessageBox.Warning
            mb_cls.RejectRole = QMessageBox.RejectRole
            mb_cls.AcceptRole = QMessageBox.AcceptRole
            mb_cls.information = MagicMock()
            OzonSupplyDetailDialog.move_to_delivering(dlg)

        labels = [c.args[0] for c in box.addButton.call_args_list]
        self.assertIn("Всё равно перенести", labels)
        supply = self.svc.get_supply(self.source_id, self.sid)
        self.assertEqual(int(supply.get("delivering_listed") or 0), 1)


if __name__ == "__main__":
    unittest.main()
