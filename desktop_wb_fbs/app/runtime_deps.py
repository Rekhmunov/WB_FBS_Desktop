# -*- coding: utf-8 -*-
"""One-shot optional dependency install at app open (skip if already present)."""
from __future__ import annotations

import importlib
import logging
import subprocess
import sys
from typing import Callable, List, Optional, Sequence, Tuple

_log = logging.getLogger(__name__)

StatusCb = Optional[Callable[[str], None]]

# (import_name, pip_spec) — soft deps used at runtime; PyQt* are hard (must exist).
_SOFT_DEPS = [
    ("pymupdf", "pymupdf>=1.24.0"),
    ("certifi", "certifi>=2024.2.2"),
    ("tzdata", "tzdata>=2024.1"),
]  # type: List[Tuple[str, str]]


def _can_import(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


def missing_soft_deps(
    deps: Sequence[Tuple[str, str]] = None,
) -> List[Tuple[str, str]]:
    items = list(deps if deps is not None else _SOFT_DEPS)
    if sys.version_info < (3, 9):
        items.append(("backports.zoneinfo", "backports.zoneinfo>=0.2.1"))
    out = []  # type: List[Tuple[str, str]]
    for import_name, spec in items:
        if not _can_import(import_name):
            out.append((import_name, spec))
    return out


def pip_install(spec: str) -> None:
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        str(spec),
    ]
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "universal_newlines": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    proc = subprocess.run(cmd, check=False, **kwargs)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(out or "pip exit code {}".format(proc.returncode))
    importlib.invalidate_caches()


def ensure_runtime_deps(status: StatusCb = None) -> List[str]:
    """Install missing soft packages once. Returns list of installed import names.

    Already-installed packages are skipped (no pip call).
    """
    missing = missing_soft_deps()
    if not missing:
        return []
    installed = []  # type: List[str]
    for import_name, spec in missing:
        if status:
            status("Установка {}…".format(import_name))
        _log.info("runtime_deps: installing %s", spec)
        try:
            pip_install(spec)
        except Exception as exc:
            _log.warning("runtime_deps: failed %s: %s", spec, exc)
            continue
        if _can_import(import_name):
            installed.append(import_name)
        else:
            _log.warning(
                "runtime_deps: %s installed but still not importable", import_name
            )
    return installed
