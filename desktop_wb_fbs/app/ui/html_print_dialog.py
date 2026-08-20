# -*- coding: utf-8 -*-
"""In-app HTML document preview and print (picking lists, stickers)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QEventLoop, QTimer, QUrl, Qt
from PyQt5.QtGui import QCursor
from PyQt5.QtPrintSupport import QPrintDialog, QPrinter
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.dialog_utils import (
    apply_maximized_on_show,
    prepare_modal_dialog,
    standard_window_flags,
)

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView

    _HAS_WEBENGINE = True
    _WEBENGINE_ERROR = ""
except Exception as exc:  # pragma: no cover - optional dependency / DLL issues
    QWebEngineView = None  # type: ignore[misc, assignment]
    _HAS_WEBENGINE = False
    _WEBENGINE_ERROR = str(exc) or exc.__class__.__name__


def webengine_status() -> tuple:
    """Return (available, reason). Reason is empty when available."""
    if _HAS_WEBENGINE:
        return True, ""
    if _WEBENGINE_ERROR:
        return False, _WEBENGINE_ERROR
    return False, "модуль PyQt5.QtWebEngineWidgets не найден"


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
        self._presented = True
        self._load_started = False
        self._load_attempts = 0
        self._load_warned = False
        doc_title = str(title or self._html_path.stem)
        self.setWindowTitle(doc_title)
        # Full-screen preview at 100% zoom.
        # Do not start WebEngine load in __init__: maximizing during an in-flight
        # load aborts it (blank preview / false «load failed» → browser only).
        prepare_modal_dialog(
            self,
            maximized=True,
            default_size=(960, 720),
            minimum_size=(720, 520),
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.btn_print = QPushButton("Печать…")
        self.btn_print.setObjectName("bottomPrimary")
        self.btn_print.setEnabled(False)
        # Document is already on screen — open the system print dialog directly.
        self.btn_print.clicked.connect(self._print_now)
        self.btn_pdf = QPushButton("Сохранить PDF")
        self.btn_pdf.setObjectName("secondary")
        self.btn_pdf.setEnabled(False)
        self.btn_pdf.clicked.connect(self._save_pdf)
        toolbar.addWidget(self.btn_print)
        toolbar.addWidget(self.btn_pdf)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        self._view = QWebEngineView(self)
        try:
            from PyQt5.QtWebEngineWidgets import QWebEngineSettings

            settings = self._view.settings()
            settings.setAttribute(
                QWebEngineSettings.LocalContentCanAccessFileUrls, True
            )
            settings.setAttribute(
                QWebEngineSettings.LocalContentCanAccessRemoteUrls, True
            )
        except Exception:
            pass
        self._view.setZoomFactor(1.0)
        root.addWidget(self._view, 1)
        self._view.loadFinished.connect(self._on_load_finished)

        # Application-modal so preview is not hidden behind supply/fullscreen windows.
        self.setWindowModality(Qt.ApplicationModal)

        # Replace prepare_modal_dialog's showEvent so we maximize first, then load.
        def _show_event(event) -> None:  # noqa: N802
            QWidget.showEvent(self, event)
            apply_maximized_on_show(self)
            self.raise_()
            self.activateWindow()
            self._start_load()

        self.showEvent = _show_event  # type: ignore[method-assign]

    def _page_base_url(self) -> QUrl:
        # Trailing slash matters for resolving relative sticker assets (42.png).
        return QUrl.fromLocalFile(str(self._html_path.parent.resolve()) + "/")

    def _start_load(self) -> None:
        if self._load_started:
            return
        self._load_started = True
        # Let maximize layout settle one tick before navigating.
        QTimer.singleShot(0, self._load_html)

    def _load_html(self) -> None:
        self._load_attempts += 1
        try:
            self._view.setZoomFactor(1.0)
        except Exception:
            pass
        # Prefer setHtml + baseUrl so relative PNG stickers resolve next to the
        # temp HTML file. Fall back to load(file://) if the file cannot be read.
        try:
            html = self._html_path.read_text(encoding="utf-8")
        except Exception:
            self._view.load(QUrl.fromLocalFile(str(self._html_path.resolve())))
            return
        self._view.setHtml(html, self._page_base_url())

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            self._loaded = True
            try:
                self._view.setZoomFactor(1.0)
            except Exception:
                pass
            for btn in (self.btn_print, self.btn_pdf):
                btn.setEnabled(True)
            return

        # Ignore a late failure after a successful paint (WebEngine can emit this).
        if self._loaded:
            return

        if self._load_attempts < 2:
            QTimer.singleShot(120, self._load_html)
            return

        for btn in (self.btn_print, self.btn_pdf):
            btn.setEnabled(False)
        if not self._load_warned:
            self._load_warned = True
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

    def _print_now(self) -> None:
        """System print dialog — no second print-preview render pass."""
        printer = QPrinter(QPrinter.HighResolution)
        dialog = QPrintDialog(printer, self)
        dialog.setWindowFlags(standard_window_flags())
        dialog.setWindowTitle("Печать")
        if dialog.exec_() != QPrintDialog.Accepted:
            return
        app = QApplication.instance()
        if app is not None:
            app.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            ok = self._print_to_printer(printer)
        finally:
            if app is not None:
                while app.overrideCursor() is not None:
                    app.restoreOverrideCursor()
        if ok:
            QMessageBox.information(self, "Печать", "Документ отправлен на печать.")
        else:
            QMessageBox.warning(
                self, "Печать", "Не удалось отправить документ на печать."
            )

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
        app = QApplication.instance()
        if app is not None:
            app.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            ok = self._print_to_printer(printer)
        finally:
            if app is not None:
                while app.overrideCursor() is not None:
                    app.restoreOverrideCursor()
        if ok:
            QMessageBox.information(self, "PDF", "Файл сохранён:\n{}".format(path))
        else:
            QMessageBox.warning(self, "PDF", "Не удалось сохранить PDF.")


def show_html_print_preview(
    html_path: Path,
    *,
    title: str = "",
    parent: Optional[QWidget] = None,
) -> bool:
    """
    Open modal preview.

    Returns True when the in-app dialog was presented. Callers must not fall
    back to the system browser after a successful present — even if WebEngine
    reported a flaky loadFinished(False), the user already saw (or closed) the
    preview window.
    """
    if not _HAS_WEBENGINE:
        return False
    app = QApplication.instance()
    if app is not None:
        while app.overrideCursor() is not None:
            app.restoreOverrideCursor()
    dlg = HtmlPrintPreviewDialog(html_path, title=title, parent=parent)
    dlg.exec_()
    return bool(getattr(dlg, "_presented", False))
