# -*- coding: utf-8 -*-
"""Application data / photo paths (local only)."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        path = Path(base) / "FeedPilotFBS"
    else:
        path = Path.home() / ".local" / "share" / "FeedPilotFBS"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return app_data_dir() / "feedpilot_fbs.sqlite3"


def photos_dir() -> Path:
    path = app_data_dir() / "product_photos"
    path.mkdir(parents=True, exist_ok=True)
    return path
