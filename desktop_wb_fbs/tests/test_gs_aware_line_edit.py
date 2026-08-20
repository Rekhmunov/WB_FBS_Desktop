# -*- coding: utf-8 -*-
"""QLineEdit must keep GS1 Group Separator from КИЗ wedge scanners."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QApplication, QLineEdit

from app.ui.dialog_utils import GsAwareLineEdit


def _app() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing
    return QApplication([])


class GsAwareLineEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def test_default_qlineedit_drops_gs_key(self) -> None:
        edit = QLineEdit()
        press = QKeyEvent(QEvent.KeyPress, 0x1D, Qt.NoModifier, "\u001d")
        self.app.sendEvent(edit, press)
        self.assertNotIn("\u001d", edit.text())

    def test_gs_aware_keeps_gs_key(self) -> None:
        edit = GsAwareLineEdit()
        press = QKeyEvent(QEvent.KeyPress, 0x1D, Qt.NoModifier, "\u001d")
        self.app.sendEvent(edit, press)
        self.assertEqual(edit.text(), "\u001d")

    def test_gs_aware_ctrl_bracket_as_gs(self) -> None:
        edit = GsAwareLineEdit()
        press = QKeyEvent(
            QEvent.KeyPress, Qt.Key_BracketRight, Qt.ControlModifier, ""
        )
        self.app.sendEvent(edit, press)
        self.assertEqual(edit.text(), "\u001d")

    def test_gs_aware_keeps_gs_inside_full_kiz(self) -> None:
        edit = GsAwareLineEdit()
        payload = "0104604060004010215ABC123\u001d93dGVzdA=="
        for ch in payload:
            if ch == "\u001d":
                press = QKeyEvent(QEvent.KeyPress, 0x1D, Qt.NoModifier, ch)
            else:
                press = QKeyEvent(QEvent.KeyPress, 0, Qt.NoModifier, ch)
            self.app.sendEvent(edit, press)
        self.assertEqual(edit.text(), payload)
        self.assertIn("\u001d", edit.text())


if __name__ == "__main__":
    unittest.main()
