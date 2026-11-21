# -*- coding: utf-8 -*-
"""Application data / photo paths (portable ``data/`` + legacy AppData).

Primary store is ``<program>/data/``. On first run (or when ``data`` is
deleted), existing files are **copied** from the legacy AppData location.
Legacy is never deleted so the user can recover or re-migrate.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Optional

_log = logging.getLogger("feedpilot.paths")

DB_FILENAME = "feedpilot_fbs.sqlite3"
PHOTOS_DIRNAME = "product_photos"
_LEGACY_APP_NAME = "FeedPilotFBS"

_migrate_lock = threading.Lock()
_migrated = False  # type: bool


def program_dir() -> Path:
    """Folder that contains the app (``run.py`` / frozen exe)."""
    override = str(os.environ.get("FEEDPILOT_PROGRAM_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # app/paths.py → desktop_wb_fbs/
    return Path(__file__).resolve().parent.parent


def legacy_app_data_dir() -> Path:
    """Historical location under AppData / XDG (never deleted by us)."""
    override = str(os.environ.get("FEEDPILOT_LEGACY_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / _LEGACY_APP_NAME
    return Path.home() / ".local" / "share" / _LEGACY_APP_NAME


def portable_data_dir() -> Path:
    """``<program>/data`` — preferred store for DB, photos, caches, logs."""
    override = str(os.environ.get("FEEDPILOT_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return program_dir() / "data"


def _db_file(root: Path) -> Path:
    return root / DB_FILENAME


def _sqlite_sidecar_paths(db_file: Path) -> list:
    # type: () -> list[Path]
    """WAL/SHM next to the main DB (copy if present; app should be closed)."""
    return [Path(str(db_file) + suffix) for suffix in ("-wal", "-shm", "-journal")]


def _copy_file_if_present(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))
    return True


def _copy_photos(src_root: Path, dst_root: Path) -> None:
    src = src_root / PHOTOS_DIRNAME
    if not src.is_dir():
        return
    dst = dst_root / PHOTOS_DIRNAME
    # Merge into destination; never wipe existing portable photos.
    if not dst.exists():
        shutil.copytree(str(src), str(dst))
        return
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        target = dst / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(path), str(target))


def migrate_legacy_to_portable(
    *,
    portable: Optional[Path] = None,
    legacy: Optional[Path] = None,
) -> bool:
    """Copy legacy DB (+ sidecars) and photos into portable ``data/``.

    Returns True when a copy was performed. Never deletes ``legacy``.
    Skips when portable DB already exists.
    """
    portable_root = Path(portable or portable_data_dir())
    legacy_root = Path(legacy or legacy_app_data_dir())
    dest_db = _db_file(portable_root)
    src_db = _db_file(legacy_root)

    if dest_db.is_file():
        return False
    if not src_db.is_file():
        portable_root.mkdir(parents=True, exist_ok=True)
        return False

    portable_root.mkdir(parents=True, exist_ok=True)
    _log.info(
        "Migrating local DB from %s → %s (legacy kept)",
        src_db,
        dest_db,
    )
    _copy_file_if_present(src_db, dest_db)
    for side in _sqlite_sidecar_paths(src_db):
        _copy_file_if_present(side, portable_root / side.name)
    _copy_photos(legacy_root, portable_root)
    return True


def ensure_portable_data() -> Path:
    """Ensure portable ``data/`` exists; migrate from legacy once if needed."""
    global _migrated
    root = portable_data_dir()
    with _migrate_lock:
        if not _migrated:
            try:
                migrate_legacy_to_portable(portable=root)
            except OSError as exc:
                _log.warning("Portable migrate skipped: %s", exc)
            _migrated = True
        root.mkdir(parents=True, exist_ok=True)
    return root


def reset_migrate_state_for_tests() -> None:
    """Test helper: allow migrate to run again in-process."""
    global _migrated
    with _migrate_lock:
        _migrated = False


def app_data_dir() -> Path:
    """Active data root (portable ``data/``, with legacy auto-copy)."""
    return ensure_portable_data()


def db_path() -> Path:
    return app_data_dir() / DB_FILENAME


def photos_dir() -> Path:
    path = app_data_dir() / PHOTOS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
