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


def fullscreen_parent(
    parent: Optional[QWidget], fullscreen: bool
) -> Optional[QWidget]:
    return None if fullscreen else parent


def init_fullscreen_dialog(
    dialog: QDialog,
    *,
    fullscreen: bool,
    default_size: Optional[Tuple[int, int]] = None,
    minimum_size: Optional[Tuple[int, int]] = None,
) -> None:
    dialog._fullscreen = fullscreen  # type: ignore[attr-defined]
    dialog._fullscreen_applied = False  # type: ignore[attr-defined]
    if fullscreen:
        dialog.setWindowFlags(
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
        )
        dialog.setMinimumSize(640, 480)
        return
    if default_size:
        dialog.resize(*default_size)
    if minimum_size:
        dialog.setMinimumSize(*minimum_size)


def apply_fullscreen_on_show(dialog: QDialog) -> None:
    if not getattr(dialog, "_fullscreen", False):
        return
    if getattr(dialog, "_fullscreen_applied", False):
        return
    dialog._fullscreen_applied = True  # type: ignore[attr-defined]
    screen = QApplication.primaryScreen()
    if screen is not None:
        dialog.setGeometry(screen.availableGeometry())
    else:
        dialog.showMaximized()


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
