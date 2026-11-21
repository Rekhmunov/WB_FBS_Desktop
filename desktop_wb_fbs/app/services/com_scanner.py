# -*- coding: utf-8 -*-
"""COM / USB-UART barcode scanner (web Web Serial parity).

Keyboard wedge stays the default. When COM mode is on, complete scan frames
are delivered to the active dialog via the same handlers as returnPressed.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

try:
    from PyQt5.QtSerialPort import QSerialPort, QSerialPortInfo

    _SERIAL_OK = True
except Exception:  # pragma: no cover - optional Qt module
    QSerialPort = None  # type: ignore
    QSerialPortInfo = None  # type: ignore
    _SERIAL_OK = False

from app.db import Database

SETTING_MODE = "scan_input_mode_v1"
SETTING_PORT = "scan_com_port_v1"
MODE_COM = "com"
MODE_KEYBOARD = "keyboard"

COM_BAUD = 9600
COM_INTER_BYTE_MS = 50
COM_DEDUP_MS = 500
COM_RECONNECT_MS = 600


def serial_available() -> bool:
    return bool(_SERIAL_OK)


def list_com_ports() -> List[Tuple[str, str]]:
    """Return ``(port_name, display_label)`` pairs."""
    if not _SERIAL_OK:
        return []
    out = []  # type: List[Tuple[str, str]]
    for info in QSerialPortInfo.availablePorts():
        name = str(info.portName() or "").strip()
        if not name:
            continue
        desc = str(info.description() or "").strip()
        label = "{} — {}".format(name, desc) if desc else name
        out.append((name, label))
    out.sort(key=lambda x: x[0].lower())
    return out


def preferred_com_enabled(db: Optional[Database] = None) -> bool:
    store = db or Database()
    return str(store.get_setting(SETTING_MODE, MODE_KEYBOARD) or "").strip() == MODE_COM


def set_preferred_com_enabled(enabled: bool, db: Optional[Database] = None) -> None:
    store = db or Database()
    store.set_setting(SETTING_MODE, MODE_COM if enabled else MODE_KEYBOARD)


def saved_com_port(db: Optional[Database] = None) -> str:
    store = db or Database()
    return str(store.get_setting(SETTING_PORT, "") or "").strip()


def save_com_port(port_name: str, db: Optional[Database] = None) -> None:
    store = db or Database()
    store.set_setting(SETTING_PORT, str(port_name or "").strip())


class ComScannerService(QObject):
    """Background COM reader → ``scanReceived`` with complete frames."""

    scanReceived = pyqtSignal(str)
    statusChanged = pyqtSignal(str, str)  # text, tone: '' | 'ok' | 'error'

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._port = None  # type: Optional[QSerialPort]
        self._buffer = ""
        self._closing = False
        self._connect_in_flight = False
        self._last_raw = ""
        self._last_at = 0.0
        self._want_connected = False
        self._port_name = ""
        self._inter_byte = QTimer(self)
        self._inter_byte.setSingleShot(True)
        self._inter_byte.timeout.connect(self._flush_buffer)
        self._reconnect = QTimer(self)
        self._reconnect.setSingleShot(True)
        self._reconnect.timeout.connect(self._try_reconnect)

    @property
    def port_name(self) -> str:
        return self._port_name

    @property
    def is_open(self) -> bool:
        return bool(self._port is not None and self._port.isOpen())

    def set_status(self, text: str, tone: str = "") -> None:
        self.statusChanged.emit(str(text or ""), str(tone or ""))

    def connect_port(self, port_name: str, *, quiet: bool = False) -> bool:
        name = str(port_name or "").strip()
        if not name:
            if not quiet:
                self.set_status("COM: выберите порт", "error")
            return False
        if not _SERIAL_OK:
            if not quiet:
                self.set_status("COM недоступен в этой сборке", "error")
            return False
        if self._connect_in_flight:
            return False
        if self._closing:
            return False
        self._connect_in_flight = True
        self._want_connected = True
        self._reconnect.stop()
        try:
            if self.is_open and self._port_name == name:
                self.set_status("COM подключён", "ok")
                return True
            self._release_port()
            port = QSerialPort()
            port.setPortName(name)
            port.setBaudRate(COM_BAUD)
            port.setDataBits(QSerialPort.Data8)
            port.setParity(QSerialPort.NoParity)
            port.setStopBits(QSerialPort.OneStop)
            port.setFlowControl(QSerialPort.NoFlowControl)
            if not port.open(QSerialPort.ReadOnly):
                err = str(port.errorString() or "не удалось открыть")
                if not quiet:
                    busy = any(
                        token in err.lower()
                        for token in ("busy", "denied", "занят", "access", "in use")
                    )
                    self.set_status(
                        "COM: порт занят — закройте другие программы"
                        if busy
                        else "COM: не удалось открыть порт",
                        "error",
                    )
                return False
            self._port = port
            self._port_name = name
            port.readyRead.connect(self._on_ready_read)
            try:
                port.errorOccurred.connect(self._on_port_error)
            except Exception:
                pass
            save_com_port(name)
            self.set_status("COM подключён · {}".format(name), "ok")
            return True
        finally:
            self._connect_in_flight = False

    def disconnect_port(self) -> None:
        self._want_connected = False
        self._closing = True
        try:
            self._reconnect.stop()
            self._release_port()
            self.set_status("", "")
        finally:
            self._closing = False

    def _release_port(self) -> None:
        self._inter_byte.stop()
        self._buffer = ""
        port = self._port
        self._port = None
        self._port_name = ""
        if port is None:
            return
        try:
            port.readyRead.disconnect(self._on_ready_read)
        except Exception:
            pass
        try:
            if port.isOpen():
                port.close()
        except Exception:
            pass
        port.deleteLater()

    def _on_ready_read(self) -> None:
        port = self._port
        if port is None:
            return
        try:
            raw = bytes(port.readAll())
        except Exception:
            return
        if not raw:
            return
        # latin1 keeps 0x1D GS for КИЗ frames (web TextDecoder("latin1")).
        try:
            text = raw.decode("latin1", errors="ignore")
        except Exception:
            return
        self._on_chunk(text)

    def _on_chunk(self, text: str) -> None:
        if not text:
            return
        self._buffer += text
        parts = self._buffer.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self._buffer = parts.pop() if parts else ""
        for part in parts:
            if str(part or "").replace(" ", ""):
                self._deliver(part)
        if self._buffer:
            self._inter_byte.start(COM_INTER_BYTE_MS)
        else:
            self._inter_byte.stop()

    def _flush_buffer(self) -> None:
        raw = self._buffer
        self._buffer = ""
        if raw and str(raw).replace(" ", ""):
            self._deliver(raw)

    def _deliver(self, raw: str) -> None:
        value = str(raw or "").rstrip("\r\n")
        if not value.replace(" ", ""):
            return
        now = time.time() * 1000.0
        if value == self._last_raw and (now - self._last_at) < COM_DEDUP_MS:
            return
        self._last_raw = value
        self._last_at = now
        self.scanReceived.emit(value)

    def _on_port_error(self, *_args) -> None:
        if self._closing or not self._want_connected:
            return
        self.set_status("COM: связь потеряна — переподключение…", "error")
        name = self._port_name or saved_com_port()
        self._release_port()
        if name:
            self._port_name = name  # remember for reconnect
            self._reconnect.start(COM_RECONNECT_MS)

    def _try_reconnect(self) -> None:
        if not self._want_connected or self._closing:
            return
        name = self._port_name or saved_com_port()
        if not name:
            self.set_status("COM: выберите порт", "error")
            return
        ok = self.connect_port(name, quiet=True)
        if not ok:
            self.set_status("COM: переподключение…", "error")
            self._reconnect.start(COM_RECONNECT_MS)


# Shared process-wide reader (web has one wbFbsScanComState).
_service = None  # type: Optional[ComScannerService]
_active_deliver = None  # type: Optional[Callable[[str], None]]


def shared_scanner() -> ComScannerService:
    global _service
    if _service is None:
        _service = ComScannerService()
        _service.scanReceived.connect(_on_shared_scan)
    return _service


def set_active_com_deliver(callback: Optional[Callable[[str], None]]) -> None:
    """Register the frontmost scan dialog deliverer (or None on close)."""
    global _active_deliver
    _active_deliver = callback


def _on_shared_scan(value: str) -> None:
    cb = _active_deliver
    if cb is None:
        return
    try:
        cb(str(value or ""))
    except Exception:
        pass
