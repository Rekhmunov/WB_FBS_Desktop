#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Entry point: FeedPilot Desktop — Поставки ВБ ФБС."""
from __future__ import annotations

import multiprocessing
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _hide_windows_console() -> None:
    """Hide the black cmd window when started via python.exe / .bat.

    Prefer ``pythonw`` / ``FeedPilot FBS.vbs`` for a true no-console launch.
    Set FEEDPILOT_CONSOLE=1 to keep the console (debug).
    """
    if sys.platform != "win32":
        return
    flag = str(os.environ.get("FEEDPILOT_CONSOLE") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def _crash_log_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        path = Path(base) / "FeedPilotFBS" / "logs"
    else:
        path = Path.home() / ".local" / "share" / "FeedPilotFBS" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path / "last_crash.txt"


def _write_crash(text: str) -> Path:
    path = _crash_log_path()
    path.write_text(text, encoding="utf-8")
    return path


def _show_fatal_dialog(message: str) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "FeedPilot FBS", 0x10)
            return
        except Exception:
            pass
    try:
        from PyQt5.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "FeedPilot FBS", message)
        app.processEvents()
    except Exception:
        pass


def _friendly_hint(exc: BaseException) -> str:
    text = str(exc or "")
    lower = text.lower()
    if isinstance(exc, ModuleNotFoundError) or "no module named" in lower:
        mod = getattr(exc, "name", "") or text
        return (
            "Не найден модуль Python: {}.\n\n"
            "Откройте cmd в папке программы и выполните:\n"
            "python -m pip install -r requirements.txt"
        ).format(mod or text)
    if "europe/moscow" in lower or "no time zone found" in lower:
        return (
            "Не найдена таймзона Europe/Moscow.\n\n"
            "Выполните: python -m pip install tzdata backports.zoneinfo"
        )
    return text


def main() -> int:
    try:
        from app.main import run

        code = run()
        return int(code or 0)
    except Exception as exc:
        tb = traceback.format_exc()
        body = "{}: {}\n\n{}".format(type(exc).__name__, exc, tb)
        crash_path = None
        try:
            crash_path = _write_crash(body)
        except Exception:
            pass
        try:
            from app.diag_log import exception

            exception("app.fatal", exc)
        except Exception:
            pass
        hint = _friendly_hint(exc)
        msg = "Не удалось запустить FeedPilot FBS.\n\n{}".format(hint)
        if crash_path is not None:
            msg += "\n\nПодробности:\n{}".format(crash_path)
        _show_fatal_dialog(msg)
        if sys.platform != "win32" or os.environ.get("FEEDPILOT_CONSOLE"):
            print(msg, file=sys.stderr)
            print(tb, file=sys.stderr)
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _hide_windows_console()
    sys.exit(main())
