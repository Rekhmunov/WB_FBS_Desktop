# -*- coding: utf-8 -*-
"""Application-wide text selection and copy via context menu."""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QEvent, QObject, Qt
from PyQt5.QtGui import QClipboard
from PyQt5.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QTableWidget,
    QTextEdit,
    QWidget,
)

_SELECTABLE_FLAGS = Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard


def _copyable_label(label: QLabel) -> bool:
    if label.pixmap() is not None and not str(label.text() or "").strip():
        return False
    return bool(str(label.text() or "").strip())


def enable_label_copy(label: QLabel) -> None:
    """Allow mouse selection on labels that display copyable text."""
    if not _copyable_label(label):
        return
    flags = label.textInteractionFlags()
    if flags == Qt.NoTextInteraction:
        label.setTextInteractionFlags(_SELECTABLE_FLAGS)


def walk_copyable_labels(root: QWidget) -> None:
    for label in root.findChildren(QLabel):
        enable_label_copy(label)


def table_selection_text(table: QTableWidget) -> str:
    """Tab-separated values for the current table selection."""
    ranges = table.selectedRanges()
    if not ranges:
        return ""
    lines = []  # type: list[str]
    for area in ranges:
        for row in range(area.topRow(), area.bottomRow() + 1):
            cols = []  # type: list[str]
            for col in range(area.leftColumn(), area.rightColumn() + 1):
                widget = table.cellWidget(row, col)
                if widget is not None:
                    text = _widget_full_text(widget)
                else:
                    item = table.item(row, col)
                    text = item.text() if item is not None else ""
                cols.append(text)
            lines.append("\t".join(cols))
    return "\n".join(lines)


def _widget_full_text(widget: QWidget) -> str:
    if isinstance(widget, QLabel):
        return str(widget.text() or "")
    parts = []  # type: list[str]
    for label in widget.findChildren(QLabel):
        text = str(label.text() or "").strip()
        if text:
            parts.append(text)
    for edit in widget.findChildren(QLineEdit):
        text = str(edit.text() or "").strip()
        if text:
            parts.append(text)
    return " · ".join(parts)


def selected_text_from_widget(widget: QWidget) -> str:
    if isinstance(widget, QLabel):
        selected = str(widget.selectedText() or "").strip()
        if selected:
            return selected
        return str(widget.text() or "").strip() if _copyable_label(widget) else ""
    if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit)):
        selected = str(widget.selectedText() or "").strip()
        if selected:
            return selected
        return str(widget.text() or "").strip()
    if isinstance(widget, QTableWidget):
        return table_selection_text(widget)
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QTableWidget):
            cell_text = table_selection_text(parent)
            if cell_text:
                return cell_text
            break
        if isinstance(parent, (QLabel, QLineEdit, QTextEdit, QPlainTextEdit)):
            return selected_text_from_widget(parent)
        parent = parent.parentWidget()
    return ""


def _clipboard() -> QClipboard:
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication is required")
    return app.clipboard()


def copy_text(text: str) -> None:
    value = str(text or "")
    if value:
        _clipboard().setText(value)


class TextCopySupport(QObject):
    """Enable label selection and a universal «Копировать» context menu."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.ChildAdded:
            child = event.child()
            if isinstance(child, QLabel):
                enable_label_copy(child)
            return False

        if event.type() != QEvent.ContextMenu:
            return False
        if not isinstance(obj, QWidget):
            return False
        if obj.contextMenuPolicy() == Qt.CustomContextMenu:
            return False
        if isinstance(obj, QMenu):
            return False
        if isinstance(obj, (QLineEdit, QTextEdit, QPlainTextEdit)):
            return False

        text = selected_text_from_widget(obj)
        if not text:
            return False

        menu = QMenu(obj)
        from app.ui.dialog_utils import style_app_menu

        menu = style_app_menu(menu)
        action = menu.addAction("Копировать")
        chosen = menu.exec_(event.globalPos())
        if chosen is action:
            copy_text(text)
        return True


def install_text_copy_support(app: QApplication) -> TextCopySupport:
    helper = TextCopySupport(app)
    app.installEventFilter(helper)
    return helper
