# -*- coding: utf-8 -*-
"""Crash-safe diagnostic log — every line is flushed to disk immediately."""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_lock = threading.Lock()
_log_path = None  # type: Optional[Path]
_file = None  # type: Optional[object]
_initialized = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def init() -> Path:
    """Open log file once at app startup. Safe to call multiple times."""
    global _initialized, _log_path, _file
    if _initialized and _log_path is not None:
        return _log_path
    from app.paths import logs_dir

    _log_path = logs_dir() / "feedpilot_fbs.log"
    _file = open(_log_path, "a", encoding="utf-8", buffering=1)
    try:
        import faulthandler

        faulthandler.enable(file=_file, all_threads=True)
    except Exception:
        pass
    _initialized = True
    write(
        "app.start",
        python=sys.version.split()[0],
        platform=sys.platform,
        pid=os.getpid(),
        log=str(_log_path),
    )
    return _log_path


def log_file_path() -> Optional[Path]:
    return _log_path


def write(event: str, *, sync: bool = False, **fields: Any) -> None:
    """Append one JSON line. ``sync=True`` fsyncs — use at chunk/worker boundaries."""
    if not _initialized:
        try:
            init()
        except Exception:
            return
    thread = threading.current_thread()
    payload = {
        "ts": _now_iso(),
        "event": str(event or ""),
        "thread": thread.name,
        "tid": thread.ident,
    }
    for key, value in fields.items():
        if value is not None:
            payload[str(key)] = value
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _lock:
        if _file is None:
            return
        try:
            _file.write(line + "\n")
            _file.flush()
            if sync:
                os.fsync(_file.fileno())
        except Exception:
            pass


def exception(event: str, exc: BaseException, **fields: Any) -> None:
    write(
        event,
        sync=True,
        error=str(exc),
        traceback="".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
        **fields,
    )
