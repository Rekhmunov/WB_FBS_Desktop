# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import MagicMock

from app.ui.table_col_widths import normalize_defaults, parse_saved_widths


class ParseSavedWidthsTests(unittest.TestCase):
    def test_valid_json(self) -> None:
        raw = json.dumps([40, 200, 420, 52])
        self.assertEqual(parse_saved_widths(raw, 4), [40, 200, 420, 52])

    def test_clamps_min_width(self) -> None:
        raw = json.dumps([10, 200, 5, 52])
        self.assertEqual(parse_saved_widths(raw, 4, min_width=32), [32, 200, 32, 52])

    def test_rejects_wrong_length(self) -> None:
        self.assertIsNone(parse_saved_widths("[1,2]", 4))

    def test_rejects_invalid(self) -> None:
        self.assertIsNone(parse_saved_widths("not-json", 2))
        self.assertIsNone(parse_saved_widths("", 2))

    def test_normalize_defaults_pads(self) -> None:
        self.assertEqual(normalize_defaults([40, 200], 4), [40, 200, 100, 100])


class PersistViaDbMockTests(unittest.TestCase):
    def test_roundtrip_settings_key(self) -> None:
        db = MagicMock()
        db.get_setting.return_value = json.dumps([80, 160, 300, 48])
        raw = db.get_setting("supply_detail_table_cols", "")
        widths = parse_saved_widths(raw, 4)
        self.assertEqual(widths, [80, 160, 300, 48])
        db.set_setting("supply_detail_table_cols", json.dumps(widths, separators=(",", ":")))
        db.set_setting.assert_called_with(
            "supply_detail_table_cols", "[80,160,300,48]"
        )

    def test_use_layout_persists_previous_key(self) -> None:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtWidgets import QApplication, QTableWidget
        from app.ui.table_col_widths import PersistentColumnWidths

        app = QApplication.instance() or QApplication([])
        db = MagicMock()
        db.get_setting.return_value = ""
        table = QTableWidget(0, 3)
        helper = PersistentColumnWidths(
            db, table, "ozon_fbs_table_cols_postings", [200, 360, 160]
        )
        helper.apply()
        table.setColumnWidth(0, 240)
        helper.use_layout("ozon_fbs_table_cols_supplies", [280, 180, 100])
        saved = [
            c
            for c in db.set_setting.call_args_list
            if c[0][0] == "ozon_fbs_table_cols_postings"
        ]
        self.assertTrue(saved)
        self.assertEqual(helper.settings_key, "ozon_fbs_table_cols_supplies")
        del app

    def test_begin_update_blocks_persist_of_transient_resize(self) -> None:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtCore import QTimer
        from PyQt5.QtWidgets import QApplication, QTableWidget
        from app.ui.table_col_widths import PersistentColumnWidths

        app = QApplication.instance() or QApplication([])
        db = MagicMock()
        db.get_setting.return_value = json.dumps([80, 160, 120])
        table = QTableWidget(0, 3)
        helper = PersistentColumnWidths(
            db, table, "fbs_table_cols_new", [80, 160, 120], save_delay_ms=50
        )
        helper.apply()
        self.assertEqual([table.columnWidth(i) for i in range(3)], [80, 160, 120])

        helper.begin_update()
        table.setColumnWidth(0, 400)  # transient stretch during rebuild
        helper.persist()  # must no-op while bulk
        db.set_setting.assert_not_called()
        helper.end_update()
        # Process deferred re-apply so saved widths win over the stretch.
        QTimer.singleShot(0, app.quit)
        app.exec_()
        self.assertEqual([table.columnWidth(i) for i in range(3)], [80, 160, 120])
        # Transient 400 must not have been written.
        for call in db.set_setting.call_args_list:
            self.assertNotIn("400", str(call[0][1]))
        del app


if __name__ == "__main__":
    unittest.main()
