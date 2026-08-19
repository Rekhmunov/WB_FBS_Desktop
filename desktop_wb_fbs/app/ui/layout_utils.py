# -*- coding: utf-8 -*-
"""Flow layout — wraps widgets to the next row (natural modal/action bars)."""
from __future__ import annotations

from PyQt5.QtCore import QPoint, QRect, QSize, Qt
from PyQt5.QtWidgets import QLayout, QLayoutItem, QPushButton, QSizePolicy, QWidget


class FlowLayout(QLayout):
    def __init__(
        self,
        parent: QWidget = None,
        margin: int = -1,
        h_spacing: int = -1,
        v_spacing: int = -1,
    ) -> None:
        super(FlowLayout, self).__init__(parent)
        if margin >= 0:
            self.setContentsMargins(margin, margin, margin, margin)
        self._h = h_spacing
        self._v = v_spacing
        self._items = []  # type: list

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super(FlowLayout, self).setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _smart_spacing(self, pm) -> int:
        parent = self.parent()
        if parent is None:
            return -1
        if parent.isWidgetType():
            return parent.style().pixelMetric(pm, None, parent)
        return parent.spacing()

    def _horizontal_spacing(self) -> int:
        if self._h >= 0:
            return self._h
        from PyQt5.QtWidgets import QStyle

        return self._smart_spacing(QStyle.PM_LayoutHorizontalSpacing)

    def _vertical_spacing(self) -> int:
        if self._v >= 0:
            return self._v
        from PyQt5.QtWidgets import QStyle

        return self._smart_spacing(QStyle.PM_LayoutVerticalSpacing)

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        space_x = self._horizontal_spacing()
        space_y = self._vertical_spacing()
        if space_x < 0:
            space_x = 8
        if space_y < 0:
            space_y = 8

        for item in self._items:
            wid = item.widget()
            space_x_here = space_x
            space_y_here = space_y
            next_x = x + item.sizeHint().width() + space_x_here
            if next_x - space_x_here > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + space_y_here
                next_x = x + item.sizeHint().width() + space_x_here
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            x = next_x
            line_height = max(line_height, item.sizeHint().height())

        return y + line_height - rect.y() + m.bottom()


def fit_tab_button(btn, h_pad: int = 48) -> None:
    """Reserve width for bold label so the active tab state does not clip text."""
    from PyQt5.QtGui import QFontMetrics
    from PyQt5.QtWidgets import QPushButton

    if not isinstance(btn, QPushButton):
        return
    font = btn.font()
    font.setBold(True)
    metrics = QFontMetrics(font)
    text = btn.text()
    # Prefer measuring inner title+count when present.
    title = getattr(btn, "_title_text", None)
    count = getattr(btn, "_count_text", None)
    if title is not None:
        text = "{} {}".format(title, count or "0")
    btn.setMinimumWidth(metrics.horizontalAdvance(text) + h_pad)
    btn.updateGeometry()


class FbsTabButton(QPushButton):
    """Tab with label + count pill (web `.wb-fbs-tab` / `.wb-fbs-tab-count`)."""

    def __init__(self, title: str, parent: QWidget = None) -> None:
        from PyQt5.QtWidgets import QHBoxLayout, QLabel

        super(FbsTabButton, self).__init__(parent)
        self._title_text = title
        self._count_text = "0"
        self.setObjectName("tabBtn")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 10)
        lay.setSpacing(8)
        self._title_lab = QLabel(title)
        self._title_lab.setObjectName("tabBtnLabel")
        self._title_lab.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._count_lab = QLabel("0")
        self._count_lab.setObjectName("tabCount")
        self._count_lab.setAlignment(Qt.AlignCenter)
        self._count_lab.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._title_lab)
        lay.addWidget(self._count_lab)
        # Keep accessible name without duplicating "· N" in the button text.
        self.setText("")
        self.setAccessibleName(title)

    def set_count(self, n: int) -> None:
        self._count_text = str(int(n))
        self._count_lab.setText(self._count_text)
        fit_tab_button(self, h_pad=36)

    def setChecked(self, checked: bool) -> None:  # type: ignore[override]
        super(FbsTabButton, self).setChecked(checked)
        for w in (self, self._title_lab, self._count_lab):
            w.style().unpolish(w)
            w.style().polish(w)
            w.update()
