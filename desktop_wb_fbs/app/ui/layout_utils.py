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
            hint = item.sizeHint()
            mins = item.minimumSize()
            item_size = QSize(
                max(hint.width(), mins.width()),
                max(hint.height(), mins.height()),
            )
            next_x = x + item_size.width() + space_x_here
            if next_x - space_x_here > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + space_y_here
                next_x = x + item_size.width() + space_x_here
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item_size))
            x = next_x
            line_height = max(line_height, item_size.height())

        return y + line_height - rect.y() + m.bottom()


def fit_tab_button(btn, h_pad: int = 56) -> None:
    """Reserve width so bold title + count pill are never clipped."""
    from PyQt5.QtGui import QFontMetrics
    from PyQt5.QtWidgets import QPushButton

    if not isinstance(btn, QPushButton):
        return

    # Prefer measuring nested title/count widgets (FbsTabButton).
    title_lab = getattr(btn, "_title_lab", None)
    count_lab = getattr(btn, "_count_lab", None)
    if title_lab is not None and count_lab is not None:
        title_font = title_lab.font()
        title_font.setBold(True)
        title_metrics = QFontMetrics(title_font)
        title = str(getattr(btn, "_title_text", None) or title_lab.text() or "")
        title_w = title_metrics.horizontalAdvance(title)

        count_font = count_lab.font()
        count_font.setBold(True)
        count_metrics = QFontMetrics(count_font)
        count = str(getattr(btn, "_count_text", None) or count_lab.text() or "0")
        # Match QLabel#tabCount: min-width 22 + horizontal padding ~12.
        count_w = max(28, count_metrics.horizontalAdvance(count) + 16)

        lay = btn.layout()
        if lay is not None:
            m = lay.contentsMargins()
            side = m.left() + m.right() + lay.spacing()
        else:
            side = 28
        # Extra room for bold active state and DPI rounding.
        btn.setMinimumWidth(title_w + count_w + side + max(12, h_pad - 36))
        btn.updateGeometry()
        return

    font = btn.font()
    font.setBold(True)
    metrics = QFontMetrics(font)
    text = btn.text()
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
        lay.setContentsMargins(16, 8, 16, 10)
        lay.setSpacing(10)
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
        fit_tab_button(self)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        lay = self.layout()
        if lay is not None:
            hint = lay.sizeHint()
            return QSize(max(hint.width(), self.minimumWidth()), max(hint.height(), 44))
        return QSize(max(self.minimumWidth(), 120), 44)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        return self.sizeHint()

    def set_count(self, n: int) -> None:
        self._count_text = str(int(n))
        self._count_lab.setText(self._count_text)
        fit_tab_button(self)

    def setChecked(self, checked: bool) -> None:  # type: ignore[override]
        super(FbsTabButton, self).setChecked(checked)
        for w in (self, self._title_lab, self._count_lab):
            w.style().unpolish(w)
            w.style().polish(w)
            w.update()
        fit_tab_button(self)
