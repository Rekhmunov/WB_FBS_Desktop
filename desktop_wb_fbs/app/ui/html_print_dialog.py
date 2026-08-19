# -*- coding: utf-8 -*-
"""In-app HTML document preview and print (picking lists, stickers)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QEventLoop, QUrl, Qt
from PyQt5.QtPrintSupport import QPrintDialog, QPrintPreviewDialog, QPrinter
from PyQt5.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView

    _HAS_WEBENGINE = True
except ImportError:  # pragma: no cover - optional dependency
    QWebEngineView = None  # type: ignore[misc, assignment]
    _HAS_WEBENGINE = False


class HtmlPrintPreviewDialog(QDialog):
    """Render saved HTML locally, then print or export PDF without a browser."""

    def __init__(
        self,
        html_path: Path,
        *,
        title: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super(HtmlPrintPreviewDialog, self).__init__(parent)
        self._html_path = Path(html_path)
        self._loaded = False
        doc_title = str(title or self._html_path.stem)
        self.setWindowTitle(doc_title)
        self.resize(960, 720)
        self.setMinimumSize(720, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.btn_print = QPushButton("Печать…")
        self.btn_print.setObjectName("bottomPrimary")
        self.btn_print.setEnabled(False)
        self.btn_print.clicked.connect(self._print)
        self.btn_preview = QPushButton("Предпросмотр печати")
        self.btn_preview.setObjectName("secondary")
        self.btn_preview.setEnabled(False)
        self.btn_preview.clicked.connect(self._print_preview)
        self.btn_pdf = QPushButton("Сохранить PDF")
        self.btn_pdf.setObjectName("secondary")
        self.btn_pdf.setEnabled(False)
        self.btn_pdf.clicked.connect(self._save_pdf)
        self.btn_close = QPushButton("Закрыть")
        self.btn_close.setObjectName("secondary")
        self.btn_close.clicked.connect(self.reject)
        toolbar.addWidget(self.btn_print)
        toolbar.addWidget(self.btn_preview)
        toolbar.addWidget(self.btn_pdf)
        toolbar.addStretch(1)
        toolbar.addWidget(self.btn_close)
        root.addLayout(toolbar)

        self._view = QWebEngineView(self)
        root.addWidget(self._view, 1)
        self._view.loadFinished.connect(self._on_load_finished)
        self._view.load(QUrl.fromLocalFile(str(self._html_path.resolve())))

    def _on_load_finished(self, ok: bool) -> None:
        self._loaded = bool(ok)
        for btn in (self.btn_print, self.btn_preview, self.btn_pdf):
            btn.setEnabled(ok)
        if not ok:
            QMessageBox.warning(
                self,
                "Предпросмотр",
                "Не удалось загрузить документ для печати.",
            )

    def _print_to_printer(self, printer: QPrinter) -> bool:
        if not self._loaded:
            return False
        loop = QEventLoop()
        result = {"ok": False}

        def _done(success: bool) -> None:
            result["ok"] = bool(success)
            loop.quit()

        self._view.page().print(printer, _done)
        loop.exec_()
        return result["ok"]

    def _print(self) -> None:
        printer = QPrinter(QPrinter.HighResolution)
        dlg = QPrintDialog(printer, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        if self._print_to_printer(printer):
            QMessageBox.information(self, "Печать", "Документ отправлен на печать.")
        else:
            QMessageBox.warning(self, "Печать", "Не удалось отправить документ на печать.")

    def _print_preview(self) -> None:
        printer = QPrinter(QPrinter.HighResolution)
        preview = QPrintPreviewDialog(printer, self)
        preview.setWindowFlags(preview.windowFlags() | Qt.WindowMaximizeButtonHint)
        preview.setWindowTitle("Предпросмотр печати")
        preview.paintRequested.connect(self._print_to_printer)
        preview.exec_()

    def _save_pdf(self) -> None:
        default_name = self._html_path.with_suffix(".pdf").name
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить PDF",
            str(self._html_path.parent / default_name),
            "PDF (*.pdf)",
        )
        if not path:
            return
        if not str(path).lower().endswith(".pdf"):
            path = path + ".pdf"
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(path)
        if self._print_to_printer(printer):
            QMessageBox.information(self, "PDF", "Файл сохранён:\n{}".format(path))
        else:
            QMessageBox.warning(self, "PDF", "Не удалось сохранить PDF.")


def show_html_print_preview(
    html_path: Path,
    *,
    title: str = "",
    parent: Optional[QWidget] = None,
) -> bool:
    """Open modal preview. Returns True when WebEngine preview was shown."""
    if not _HAS_WEBENGINE:
        return False
    dlg = HtmlPrintPreviewDialog(html_path, title=title, parent=parent)
    dlg.exec_()
    return True
