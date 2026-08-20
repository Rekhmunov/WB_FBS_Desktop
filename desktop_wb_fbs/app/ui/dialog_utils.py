# -*- coding: utf-8 -*-
"""Shared helpers for modal / workflow dialogs."""
from __future__ import annotations

import time
from typing import Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.format_helpers import (
    RU_LAYOUT_SCAN_MESSAGE,
    RU_LAYOUT_SCAN_TITLE,
    scan_has_ru_layout,
)


def standard_window_flags() -> Qt.WindowFlags:
    """Native Windows title bar with minimize, maximize, and close."""
    return (
        Qt.Window
        | Qt.WindowTitleHint
        | Qt.WindowSystemMenuHint
        | Qt.WindowMinimizeButtonHint
        | Qt.WindowMaximizeButtonHint
        | Qt.WindowCloseButtonHint
    )


def fullscreen_parent(
    parent: Optional[QWidget], fullscreen: bool
) -> Optional[QWidget]:
    return None if fullscreen else parent


def init_maximized_window(
    window: QWidget,
    *,
    maximized: bool = True,
    default_size: Optional[Tuple[int, int]] = None,
    minimum_size: Optional[Tuple[int, int]] = None,
) -> None:
    """Apply native window chrome; optionally start maximized on first show."""
    window.setWindowFlags(standard_window_flags())
    window._start_maximized = bool(maximized)  # type: ignore[attr-defined]
    window._maximized_applied = False  # type: ignore[attr-defined]
    if minimum_size:
        window.setMinimumSize(*minimum_size)
    elif maximized:
        window.setMinimumSize(640, 480)
    if default_size and not maximized:
        window.resize(*default_size)


def apply_maximized_on_show(window: QWidget) -> None:
    if not getattr(window, "_start_maximized", False):
        return
    if getattr(window, "_maximized_applied", False):
        return
    window._maximized_applied = True  # type: ignore[attr-defined]
    window.showMaximized()


def bind_maximized_on_show(window: QWidget) -> None:
    """Attach showEvent hook so the window maximizes on first display."""

    def showEvent(event) -> None:  # noqa: N802 — Qt override
        QWidget.showEvent(window, event)
        apply_maximized_on_show(window)

    window.showEvent = showEvent  # type: ignore[method-assign]


def init_fullscreen_dialog(
    dialog: QDialog,
    *,
    fullscreen: bool,
    default_size: Optional[Tuple[int, int]] = None,
    minimum_size: Optional[Tuple[int, int]] = None,
) -> None:
    dialog._fullscreen = fullscreen  # type: ignore[attr-defined]
    init_maximized_window(
        dialog,
        maximized=fullscreen,
        default_size=default_size,
        minimum_size=minimum_size,
    )
    if fullscreen:
        bind_maximized_on_show(dialog)


def apply_fullscreen_on_show(dialog: QDialog) -> None:
    apply_maximized_on_show(dialog)


def prepare_modal_dialog(
    dialog: QDialog,
    *,
    maximized: bool = True,
    default_size: Optional[Tuple[int, int]] = None,
    minimum_size: Optional[Tuple[int, int]] = None,
) -> QDialog:
    """Configure a dialog for native chrome and optional maximized start."""
    init_maximized_window(
        dialog,
        maximized=maximized,
        default_size=default_size,
        minimum_size=minimum_size,
    )
    bind_maximized_on_show(dialog)
    return dialog


class RuLayoutWarningDialog(QDialog):
    """Web-like RU layout modal — avoids QMessageBox eating scanner Enter."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super(RuLayoutWarningDialog, self).__init__(parent)
        self.setWindowTitle(RU_LAYOUT_SCAN_TITLE)
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._opened_at = time.monotonic()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)
        title = QLabel(RU_LAYOUT_SCAN_TITLE)
        title.setObjectName("dialogTitle")
        lay.addWidget(title)
        body = QLabel(RU_LAYOUT_SCAN_MESSAGE)
        body.setWordWrap(True)
        body.setObjectName("hint")
        lay.addWidget(body)
        row = QHBoxLayout()
        row.addStretch(1)
        ok = QPushButton("Понятно")
        ok.setDefault(True)
        ok.clicked.connect(self._try_accept)
        row.addWidget(ok)
        lay.addLayout(row)
        self.resize(420, 180)

    def _try_accept(self) -> None:
        # Swallow trailing scanner Enter for ~500ms (web parity).
        if time.monotonic() - self._opened_at < 0.5:
            return
        self.accept()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._try_accept()
            return
        super(RuLayoutWarningDialog, self).keyPressEvent(event)


def block_ru_layout_scan(
    parent: QWidget,
    field: Optional[QLineEdit] = None,
    *,
    text: Optional[str] = None,
) -> bool:
    """Warn and reject scan when Russian keyboard layout is detected. Returns True if blocked."""
    raw = str(text if text is not None else (field.text() if field is not None else ""))
    if not scan_has_ru_layout(raw):
        return False
    if field is not None:
        field.blockSignals(True)
        field.clear()
        field.blockSignals(False)
        field.setFocus()
    dlg = RuLayoutWarningDialog(parent)
    dlg.exec_()
    if field is not None:
        field.setFocus()
    return True


def install_live_ru_layout_guard(
    field: QLineEdit,
    parent: QWidget,
) -> None:
    """Clear + warn as soon as Cyrillic/RU layout chars appear (web oninput)."""

    def _on_text(text: str) -> None:
        if not scan_has_ru_layout(text):
            return
        block_ru_layout_scan(parent, field, text=text)

    field.textChanged.connect(_on_text)


def style_app_menu(menu: QMenu) -> QMenu:
    """Light SaaS popup menu — avoid Fusion/OS black plate under QMenu.

    Rounded menus on some platforms leave a dark window chrome behind the
    stylesheet fill; force a white palette + styled background.
    """
    menu.setObjectName("appMenu")
    menu.setAttribute(Qt.WA_StyledBackground, True)
    menu.setAutoFillBackground(True)
    pal = menu.palette()
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        pal.setColor(group, QPalette.Window, QColor("#ffffff"))
        pal.setColor(group, QPalette.Base, QColor("#ffffff"))
        pal.setColor(group, QPalette.AlternateBase, QColor("#f2f8ff"))
        pal.setColor(group, QPalette.Text, QColor("#0f1f33"))
        pal.setColor(group, QPalette.WindowText, QColor("#0f1f33"))
        pal.setColor(group, QPalette.Button, QColor("#ffffff"))
        pal.setColor(group, QPalette.ButtonText, QColor("#0f1f33"))
        pal.setColor(group, QPalette.Highlight, QColor("#ddf1ff"))
        pal.setColor(group, QPalette.HighlightedText, QColor("#0f1f33"))
    menu.setPalette(pal)
    return menu
