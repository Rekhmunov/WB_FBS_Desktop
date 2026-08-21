# -*- coding: utf-8 -*-
"""Shared helpers for modal / workflow dialogs."""
from __future__ import annotations

import time
from typing import Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.format_helpers import (
    RU_LAYOUT_SCAN_MESSAGE,
    RU_LAYOUT_SCAN_TITLE,
    scan_has_ru_layout,
)


def standard_window_flags() -> Qt.WindowFlags:
    """Native title bar with min/max/close; Dialog type stays under one app window."""
    return (
        Qt.Dialog
        | Qt.WindowTitleHint
        | Qt.WindowSystemMenuHint
        | Qt.WindowMinimizeButtonHint
        | Qt.WindowMaximizeButtonHint
        | Qt.WindowCloseButtonHint
    )


def fullscreen_parent(
    parent: Optional[QWidget], fullscreen: bool
) -> Optional[QWidget]:
    """Always keep ``parent`` so child dialogs share the main taskbar entry.

    Fullscreen/maximized is handled by window state, not by detaching the parent.
    """
    _ = fullscreen
    return parent


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


def make_modal_search_box(
    *,
    placeholder: str = "🔍 Поиск…",
    tooltip: str = "Поиск по заказу, стикеру, названию товара и ШК",
    height: int = 40,
    min_width: int = 200,
    max_width: int = 280,
) -> Tuple[QFrame, QLineEdit]:
    """Search field with a vertically centered clear ✕ (supply / КИЗ / pick)."""
    box = QFrame()
    box.setObjectName("sdSearchBox")
    box.setFixedHeight(height)
    box.setMinimumWidth(min_width)
    box.setMaximumWidth(max_width)
    lay = QHBoxLayout(box)
    lay.setContentsMargins(12, 0, 4, 0)
    lay.setSpacing(0)
    edit = QLineEdit()
    edit.setObjectName("sdSearch")
    edit.setPlaceholderText(placeholder)
    edit.setClearButtonEnabled(False)
    edit.setFrame(False)
    edit.setToolTip(tooltip)
    clear_btn = QToolButton()
    clear_btn.setObjectName("sdSearchClear")
    clear_btn.setText("✕")
    clear_btn.setCursor(Qt.PointingHandCursor)
    clear_btn.setFixedSize(28, 28)
    clear_btn.setToolTip("Очистить")
    clear_btn.hide()
    clear_btn.clicked.connect(edit.clear)

    def _on_text(text: str) -> None:
        clear_btn.setVisible(bool(str(text or "").strip()))

    edit.textChanged.connect(_on_text)
    lay.addWidget(edit, 1)
    lay.addWidget(clear_btn, 0, Qt.AlignVCenter)
    return box, edit


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


class GsAwareLineEdit(QLineEdit):
    """QLineEdit that keeps GS1 Group Separator (U+001D) from wedge scanners.

    Default QLineEdit rejects Other_Control characters, so a scanner that emits
    real GS drops it — WB then sees a КИЗ without separators. Scanners that map
    GS to ↔ still work via later normalize (↔ → GS); this widget fixes the
    correct-GS path. Ctrl+] is the common keyboard-wedge encoding of GS.
    """

    _GS = "\u001d"

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        text = event.text() or ""
        if self._GS in text:
            for ch in text:
                self.insert(ch)
            event.accept()
            return
        if event.key() in (0x1D, 29) or (
            event.key() == Qt.Key_BracketRight
            and bool(event.modifiers() & Qt.ControlModifier)
        ):
            self.insert(self._GS)
            event.accept()
            return
        super(GsAwareLineEdit, self).keyPressEvent(event)
