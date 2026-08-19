# -*- coding: utf-8 -*-
"""Shared helpers for modal / workflow dialogs."""
from __future__ import annotations

from typing import Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QDialog, QLineEdit, QMessageBox, QWidget

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
        field.clear()
        field.setFocus()
    QMessageBox.warning(parent, RU_LAYOUT_SCAN_TITLE, RU_LAYOUT_SCAN_MESSAGE)
    return True
