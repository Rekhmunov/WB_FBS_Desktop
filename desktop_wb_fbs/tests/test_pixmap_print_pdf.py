# -*- coding: utf-8 -*-
"""Pixmap print preview must offer PDF download (pick list / stickers)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtGui import QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.ui.dialogs_extra import (  # noqa: E402
    _default_pdf_path,
    _download_pixmaps_pdf,
    _draw_pixmaps_to_printer,
    _safe_pdf_filename,
)


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class SafePdfNameTests(unittest.TestCase):
    def test_sanitizes_title(self) -> None:
        name = _safe_pdf_filename("Лист подбора / WB-GI-1")
        self.assertTrue(name.endswith(".pdf"))
        self.assertNotIn("/", name)

    def test_default_path_ends_with_pdf(self) -> None:
        path = _default_pdf_path("Стикеры")
        self.assertTrue(path.lower().endswith(".pdf"))


class PixmapPdfDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def _pixmap(self) -> QPixmap:
        pix = QPixmap(120, 80)
        pix.fill()
        return pix

    def test_draw_pixmaps_writes_pdf(self) -> None:
        from PyQt5.QtPrintSupport import QPrinter

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.pdf"
            printer = QPrinter(QPrinter.HighResolution)
            printer.setOutputFormat(QPrinter.PdfFormat)
            printer.setOutputFileName(str(out))
            ok = _draw_pixmaps_to_printer(printer, [self._pixmap(), self._pixmap()])
            self.assertTrue(ok)
            self.assertTrue(out.is_file())
            self.assertGreater(out.stat().st_size, 100)
            # PDF magic
            self.assertEqual(out.read_bytes()[:4], b"%PDF")

    def test_download_pixmaps_pdf_saves_via_dialog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "picking.pdf")
            with patch(
                "app.ui.dialogs_extra.QFileDialog.getSaveFileName",
                return_value=(out, "PDF (*.pdf)"),
            ), patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
                saved = _download_pixmaps_pdf(
                    None, [self._pixmap()], title="Лист подбора"
                )
            self.assertEqual(saved, out)
            self.assertTrue(Path(out).is_file())
            self.assertEqual(Path(out).read_bytes()[:4], b"%PDF")

    def test_download_appends_pdf_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "stickers")
            with patch(
                "app.ui.dialogs_extra.QFileDialog.getSaveFileName",
                return_value=(out, "PDF (*.pdf)"),
            ), patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
                saved = _download_pixmaps_pdf(None, [self._pixmap()], title="Стикеры")
            self.assertEqual(saved, out + ".pdf")
            self.assertTrue(Path(saved).is_file())

    def test_download_cancel_returns_none(self) -> None:
        with patch(
            "app.ui.dialogs_extra.QFileDialog.getSaveFileName",
            return_value=("", ""),
        ):
            saved = _download_pixmaps_pdf(None, [self._pixmap()], title="x")
        self.assertIsNone(saved)


if __name__ == "__main__":
    unittest.main()
