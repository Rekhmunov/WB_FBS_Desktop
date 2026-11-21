# -*- coding: utf-8 -*-
"""Pixmap print preview must offer compact, openable PDF download."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.ui.dialogs_extra import (  # noqa: E402
    STICKER_PAGE_MM,
    _configure_printer_page_size,
    _default_pdf_path,
    _download_pixmaps_pdf,
    _page_size_pdf_points,
    _safe_pdf_filename,
    _write_pixmaps_pdf,
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

    def _pixmap(self, w: int = 120, h: int = 80) -> QPixmap:
        pix = QPixmap(w, h)
        pix.fill(QColor("#ffffff"))
        return pix

    def _picking_page(self) -> QPixmap:
        pix = QPixmap(794, 1123)
        pix.fill(QColor("#ffffff"))
        painter = QPainter(pix)
        try:
            painter.setPen(QColor("#111111"))
            for y in range(40, 1100, 36):
                painter.drawText(
                    30, y, "Товар пример · артикул · 1234567890123"
                )
        finally:
            painter.end()
        return pix

    def test_write_pixmaps_pdf_is_valid_and_compact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.pdf"
            pages = [self._picking_page() for _ in range(3)]
            self.assertTrue(_write_pixmaps_pdf(str(out), pages))
            data = out.read_bytes()
            self.assertTrue(data.startswith(b"%PDF-1.4"))
            self.assertIn(b"%%EOF", data[-64:])
            self.assertIn(b"/Count 3", data)
            self.assertIn(b"/Filter /DCTDecode", data)
            # Old HighResolution path was multi-MB per page; keep compact.
            self.assertLess(out.stat().st_size, 600_000)

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
            data = Path(out).read_bytes()
            self.assertTrue(data.startswith(b"%PDF"))
            self.assertIn(b"%%EOF", data[-64:])

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

    def test_sticker_sheet_pdf_stays_small(self) -> None:
        stickers = []
        for i in range(20):
            pix = QPixmap(348, 240)
            pix.fill(QColor("#f1f5f9"))
            painter = QPainter(pix)
            try:
                painter.drawText(pix.rect(), Qt.AlignCenter, "STK{}".format(i))
            finally:
                painter.end()
            stickers.append(pix)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "stickers.pdf"
            self.assertTrue(_write_pixmaps_pdf(str(out), stickers))
            self.assertLess(out.stat().st_size, 250_000)

    def test_write_pixmaps_pdf_progress_and_cancel(self) -> None:
        pages = [self._pixmap(200, 120) for _ in range(5)]
        seen = []

        def _progress(done: int, total: int) -> bool:
            seen.append((done, total))
            return done < 2  # cancel mid-way

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cancel.pdf"
            self.assertFalse(
                _write_pixmaps_pdf(str(out), pages, progress=_progress)
            )
            self.assertFalse(out.exists())
        self.assertTrue(seen)
        self.assertEqual(seen[0][1], 5)

    def test_sticker_pdf_page_is_58x40_mm(self) -> None:
        page_w, page_h = _page_size_pdf_points(STICKER_PAGE_MM)
        self.assertAlmostEqual(page_w, 58 * 72 / 25.4, places=2)
        self.assertAlmostEqual(page_h, 40 * 72 / 25.4, places=2)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "sticker.pdf"
            self.assertTrue(
                _write_pixmaps_pdf(
                    str(out),
                    [self._pixmap(348, 240)],
                    page_size_mm=STICKER_PAGE_MM,
                )
            )
            data = out.read_bytes()
            self.assertIn(
                "/MediaBox [0 0 {:.2f} {:.2f}]".format(page_w, page_h).encode(
                    "ascii"
                ),
                data,
            )

    def test_configure_printer_page_size_58x40(self) -> None:
        from PyQt5.QtPrintSupport import QPrinter

        printer = QPrinter(QPrinter.HighResolution)
        _configure_printer_page_size(printer, STICKER_PAGE_MM)
        rect = printer.pageRect(QPrinter.Millimeter)
        self.assertAlmostEqual(float(rect.width()), 58.0, places=1)
        self.assertAlmostEqual(float(rect.height()), 40.0, places=1)


if __name__ == "__main__":
    unittest.main()
