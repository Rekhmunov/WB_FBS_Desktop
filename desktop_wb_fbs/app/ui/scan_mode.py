# -*- coding: utf-8 -*-
"""Keyboard / COM scan-mode toggle (web wb-fbs-scan-mode-toggle parity)."""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QPainter, QPen
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.db import Database
from app.services import com_scanner as com
from app.ui.dialog_utils import prepare_modal_dialog, standard_window_flags


class ScanModeToggle(QPushButton):
    """Switch: off = keyboard, on = COM. Layout: [kb] [track] COM."""

    toggled_com = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("scanModeToggle")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setToolTip(
            "Выкл — клавиатура (как сейчас). Вкл — COM-порт"
        )
        self.setAccessibleName("Режим сканера: клавиатура или COM")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(8)

        self._kb = QLabel()
        self._kb.setObjectName("scanModeKb")
        self._kb.setFixedSize(22, 18)
        self._kb.setAlignment(Qt.AlignCenter)
        self._kb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._kb)

        self._track = QWidget()
        self._track.setObjectName("scanModeTrack")
        self._track.setFixedSize(36, 20)
        self._track.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._track.setAttribute(Qt.WA_StyledBackground, True)
        self._track.setAutoFillBackground(True)
        self._thumb = QWidget(self._track)
        self._thumb.setObjectName("scanModeThumb")
        self._thumb.setFixedSize(16, 16)
        self._thumb.setAttribute(Qt.WA_StyledBackground, True)
        self._thumb.setAutoFillBackground(True)
        self._thumb.move(2, 2)
        lay.addWidget(self._track)

        self._com = QLabel("COM")
        self._com.setObjectName("scanModeCom")
        self._com.setAlignment(Qt.AlignCenter)
        self._com.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._com)

        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.toggled.connect(self._on_toggled)
        self._sync_chrome()

    def sizeHint(self) -> QSize:
        return QSize(108, 40)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        # Draw keyboard glyph into _kb area (no emoji; simple strokes).
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self._kb.geometry()
        pen = QPen(self.palette().buttonText().color())
        pen.setWidthF(1.6)
        painter.setPen(pen)
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        painter.drawRoundedRect(x + 2, y + 3, w - 4, h - 6, 2, 2)
        for cx in (x + 6, x + 11, x + 16):
            painter.drawPoint(cx, y + 8)
        painter.drawLine(x + 6, y + 12, x + 14, y + 12)

    def set_com_enabled(self, enabled: bool) -> None:
        blocked = self.blockSignals(True)
        self.setChecked(bool(enabled))
        self.blockSignals(blocked)
        self._sync_chrome()

    def _on_toggled(self, checked: bool) -> None:
        self._sync_chrome()
        self.toggled_com.emit(bool(checked))

    def _sync_chrome(self) -> None:
        on = self.isChecked()
        self.setProperty("comOn", "true" if on else "false")
        self._kb.setProperty("active", "false" if on else "true")
        self._com.setProperty("active", "true" if on else "false")
        self._track.setProperty("comOn", "true" if on else "false")
        # Move thumb.
        self._thumb.move(18 if on else 2, 2)
        for w in (self, self._kb, self._com, self._track):
            w.style().unpolish(w)
            w.style().polish(w)
        self.update()


class ScanModeStatus(QLabel):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("scanModeStatus")
        self.setWordWrap(False)
        self.hide()

    def set_status(self, text: str, tone: str = "") -> None:
        msg = str(text or "").strip()
        if not msg:
            self.hide()
            self.clear()
            self.setProperty("tone", "")
        else:
            self.setText(msg)
            self.setProperty("tone", str(tone or ""))
            self.show()
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class ComPortPickerDialog(QDialog):
    def __init__(
        self,
        ports: List[Tuple[str, str]],
        current: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("COM-порт сканера")
        self.setWindowFlags(standard_window_flags())
        prepare_modal_dialog(self, default_size=(420, 180), minimum_size=(360, 160))
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)
        root.addWidget(QLabel("Выберите COM-порт USB-сканера:"))
        self.combo = QComboBox()
        self._ports = list(ports)
        for name, label in self._ports:
            self.combo.addItem(label, name)
        if current:
            idx = self.combo.findData(current)
            if idx >= 0:
                self.combo.setCurrentIndex(idx)
        root.addWidget(self.combo)
        refresh = QPushButton("Обновить список")
        refresh.setObjectName("secondary")
        refresh.clicked.connect(self._refresh)
        root.addWidget(refresh)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _refresh(self) -> None:
        current = str(self.combo.currentData() or "")
        self.combo.clear()
        self._ports = com.list_com_ports()
        for name, label in self._ports:
            self.combo.addItem(label, name)
        if current:
            idx = self.combo.findData(current)
            if idx >= 0:
                self.combo.setCurrentIndex(idx)

    def selected_port(self) -> str:
        return str(self.combo.currentData() or "").strip()


class ScanModeController(object):
    """Owns toggle/status for one dialog and wires shared ComScannerService."""

    def __init__(
        self,
        *,
        toggle: ScanModeToggle,
        status: ScanModeStatus,
        deliver: Callable[[str], None],
        parent_widget: QWidget,
        db: Optional[Database] = None,
    ) -> None:
        self.toggle = toggle
        self.status = status
        self._deliver = deliver
        self._parent = parent_widget
        self._db = db or Database()
        self._svc = com.shared_scanner()
        self._active = False
        self.toggle.toggled_com.connect(self._on_toggle)

    def activate(self) -> None:
        """Call when the host dialog is shown."""
        self._active = True
        com.set_active_com_deliver(self._deliver)
        try:
            self._svc.statusChanged.disconnect(self._on_status)
        except (TypeError, RuntimeError):
            pass
        self._svc.statusChanged.connect(self._on_status)
        want = com.preferred_com_enabled(self._db)
        self.toggle.set_com_enabled(want)
        if want:
            self._connect_preferred(interactive=False)
        else:
            self.status.set_status("", "")

    def deactivate(self) -> None:
        """Call when the host dialog closes."""
        self._active = False
        com.clear_active_com_deliver(self._deliver)
        try:
            self._svc.statusChanged.disconnect(self._on_status)
        except (TypeError, RuntimeError):
            pass
        # Release port when leaving scan UI (preference kept) — web parity.
        self._svc.disconnect_port()
        self.status.set_status("", "")

    def _on_status(self, text: str, tone: str) -> None:
        if not self._active:
            return
        self.status.set_status(text, tone)

    def _on_toggle(self, enabled: bool) -> None:
        if not com.serial_available() and enabled:
            self.toggle.set_com_enabled(False)
            com.set_preferred_com_enabled(False, self._db)
            self.status.set_status("COM недоступен в этой сборке", "error")
            return
        com.set_preferred_com_enabled(enabled, self._db)
        if not enabled:
            self._svc.disconnect_port()
            self.status.set_status("", "")
            return
        # Ensure deliver is armed even if showEvent activate was skipped.
        self._active = True
        com.set_active_com_deliver(self._deliver)
        try:
            self._svc.statusChanged.disconnect(self._on_status)
        except (TypeError, RuntimeError):
            pass
        self._svc.statusChanged.connect(self._on_status)
        ok = self._connect_preferred(interactive=True)
        if not ok:
            # Stay preferred ON so operator can retry (web parity).
            self.toggle.set_com_enabled(True)

    def _connect_preferred(self, *, interactive: bool) -> bool:
        ports = com.list_com_ports()
        names = [p[0] for p in ports]
        saved = com.saved_com_port(self._db)
        port = ""
        if saved in names:
            port = saved
        elif len(names) == 1:
            port = names[0]
        elif interactive and names:
            dlg = ComPortPickerDialog(ports, current=saved, parent=self._parent)
            if dlg.exec_() != QDialog.Accepted:
                self.status.set_status("COM: выберите порт", "error")
                return False
            port = dlg.selected_port()
        if not port:
            if not interactive:
                self.status.set_status(
                    "COM: нажмите переключатель для порта", "error"
                )
            elif not names:
                self.status.set_status(
                    "COM: порт не найден. Подключите сканер и повторите.",
                    "error",
                )
            else:
                self.status.set_status("COM: выберите порт", "error")
            return False
        ok = self._svc.connect_port(port)
        if not ok and interactive and names:
            # Port busy / wrong — let operator pick another.
            dlg = ComPortPickerDialog(ports, current=port, parent=self._parent)
            if dlg.exec_() == QDialog.Accepted and dlg.selected_port():
                return self._svc.connect_port(dlg.selected_port())
        return ok


def mount_scan_mode_controls(
    scan_layout: QHBoxLayout,
    *,
    insert_at: int = 0,
    parent: QWidget,
    deliver: Callable[[str], None],
    db: Optional[Database] = None,
) -> Tuple[ScanModeToggle, ScanModeStatus, ScanModeController]:
    """Insert toggle + status into an existing scan bar layout."""
    toggle = ScanModeToggle(parent)
    status = ScanModeStatus(parent)
    scan_layout.insertWidget(insert_at, toggle, 0)
    scan_layout.insertWidget(insert_at + 1, status, 0)
    controller = ScanModeController(
        toggle=toggle,
        status=status,
        deliver=deliver,
        parent_widget=parent,
        db=db,
    )
    return toggle, status, controller
