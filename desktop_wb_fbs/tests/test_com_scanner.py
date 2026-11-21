# -*- coding: utf-8 -*-
"""COM scanner service + scan-mode toggle (web parity)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QHBoxLayout, QWidget  # noqa: E402

from app.db import Database  # noqa: E402
from app.services import com_scanner as com  # noqa: E402
from app.ui.scan_mode import (  # noqa: E402
    ScanModeToggle,
    mount_scan_mode_controls,
)


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class ComScannerSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_preferred_mode_roundtrip(self) -> None:
        self.assertFalse(com.preferred_com_enabled(self.db))
        com.set_preferred_com_enabled(True, self.db)
        self.assertTrue(com.preferred_com_enabled(self.db))
        com.set_preferred_com_enabled(False, self.db)
        self.assertFalse(com.preferred_com_enabled(self.db))

    def test_port_roundtrip(self) -> None:
        com.save_com_port("COM7", self.db)
        self.assertEqual(com.saved_com_port(self.db), "COM7")


class ComScannerFrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def test_chunk_splits_on_crlf_and_dedups(self) -> None:
        svc = com.ComScannerService()
        got = []
        svc.scanReceived.connect(got.append)
        svc._on_chunk("ABC\r\n")
        svc._on_chunk("ABC\r\n")  # dedup within window
        self.assertEqual(got, ["ABC"])
        svc._last_at = 0  # expire dedup
        svc._on_chunk("DEF\n")
        self.assertEqual(got, ["ABC", "DEF"])

    def test_inter_byte_flush(self) -> None:
        svc = com.ComScannerService()
        got = []
        svc.scanReceived.connect(got.append)
        svc._on_chunk("XYZ")  # no terminator yet
        self.assertEqual(got, [])
        svc._flush_buffer()
        self.assertEqual(got, ["XYZ"])


class ShowEventChainTests(unittest.TestCase):
    """fullscreen bind must not shadow dialog showEvent (COM activate)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def test_bind_maximized_chains_existing_show_event(self) -> None:
        from PyQt5.QtGui import QShowEvent
        from PyQt5.QtWidgets import QDialog

        from app.ui.dialog_utils import (
            apply_maximized_on_show,
            bind_maximized_on_show,
            init_maximized_window,
        )

        seen = []

        class _Dlg(QDialog):
            def showEvent(self, event) -> None:  # noqa: N802
                super(_Dlg, self).showEvent(event)
                seen.append("class")

        dlg = _Dlg()
        init_maximized_window(dlg, maximized=True, default_size=(400, 300))
        bind_maximized_on_show(dlg)
        dlg.showEvent(QShowEvent())
        self.assertIn("class", seen)
        self.assertTrue(getattr(dlg, "_maximized_applied", False))

    def test_kiz_fullscreen_show_activates_com_deliver(self) -> None:
        from PyQt5.QtGui import QShowEvent

        from app.services.kiz_pick import KizService
        from app.ui.kiz_dialog import KizDialog

        tmp = tempfile.TemporaryDirectory()
        db = Database(Path(tmp.name) / "kiz.db")
        db.init_schema()
        dlg = KizDialog(KizService(db), 1, "key", "SUP", fullscreen=True)
        self.assertIsNone(com.active_com_deliver())
        dlg.showEvent(QShowEvent())
        cb = com.active_com_deliver()
        self.assertIsNotNone(cb)
        self.assertIs(cb.__self__, dlg)  # type: ignore[union-attr]
        self.assertEqual(cb.__name__, "deliver_com_scan")  # type: ignore[union-attr]
        dlg._teardown_scan_mode()
        self.assertIsNone(com.active_com_deliver())
        tmp.cleanup()


class ScanModeToggleUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def test_toggle_emits_and_mounts(self) -> None:
        host = QWidget()
        lay = QHBoxLayout(host)
        seen = []
        tmp = tempfile.TemporaryDirectory()
        db = Database(Path(tmp.name) / "ui.db")
        db.init_schema()

        def _deliver(v: str) -> None:
            seen.append(v)

        toggle, status, controller = mount_scan_mode_controls(
            lay, parent=host, deliver=_deliver, db=db
        )
        self.assertIsInstance(toggle, ScanModeToggle)
        self.assertFalse(toggle.isChecked())
        fired = []
        toggle.toggled_com.connect(fired.append)
        with patch.object(controller, "_connect_preferred", return_value=False):
            toggle.click()
        self.assertTrue(toggle.isChecked())
        self.assertEqual(fired, [True])
        self.assertTrue(com.preferred_com_enabled(db))
        com.set_active_com_deliver(_deliver)
        com._on_shared_scan("SCAN1")
        self.assertEqual(seen, ["SCAN1"])
        com.set_active_com_deliver(None)
        controller.deactivate()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
