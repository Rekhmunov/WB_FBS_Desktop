#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Entry point: FeedPilot Desktop — Поставки ВБ ФБС."""
from __future__ import annotations

import multiprocessing
import os
import sys
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


from app.main import run  # noqa: E402


if __name__ == "__main__":
    # Required on Windows when spawning sticker PNG worker processes.
    multiprocessing.freeze_support()
    _hide_windows_console()
    sys.exit(run())
