# -*- coding: utf-8 -*-
"""On-disk cache for WB order sticker PNGs (avoids huge in-memory base64 maps)."""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from app.paths import app_data_dir

_SAFE_SUPPLY_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _api_key_fp(api_key: str) -> str:
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]


def _safe_supply_id(supply_id: str) -> str:
    text = str(supply_id or "").strip() or "unknown"
    return _SAFE_SUPPLY_RE.sub("_", text)[:120]


def supply_sticker_dir(api_key: str, supply_id: str) -> Path:
    path = (
        app_data_dir()
        / "sticker_cache"
        / _api_key_fp(api_key)
        / _safe_supply_id(supply_id)
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_supply_sticker_dir(api_key: str, supply_id: str) -> None:
    root = supply_sticker_dir(api_key, supply_id)
    for child in root.glob("*.png"):
        try:
            child.unlink()
        except Exception:
            pass


def existing_sticker_paths(
    api_key: str,
    supply_id: str,
    order_ids: Iterable,
) -> Dict[int, str]:
    """Return ``{order_id: absolute_png_path}`` for stickers already on disk."""
    root = supply_sticker_dir(api_key, supply_id)
    out = {}  # type: Dict[int, str]
    for raw_oid in order_ids or []:
        try:
            oid = int(raw_oid)
        except (TypeError, ValueError):
            continue
        path = root / "{}.png".format(oid)
        try:
            if path.is_file() and path.stat().st_size > 0:
                out[oid] = str(path)
        except OSError:
            continue
    return out


def persist_sticker_png(
    api_key: str,
    supply_id: str,
    order_id: int,
    file_b64: str,
) -> str:
    """Decode WB base64 sticker PNG and store on disk. Returns absolute path."""
    raw_b64 = str(file_b64 or "").strip()
    if not raw_b64:
        return ""
    try:
        raw = base64.b64decode(raw_b64, validate=False)
    except Exception:
        return ""
    if not raw:
        return ""
    path = supply_sticker_dir(api_key, supply_id) / "{}.png".format(int(order_id))
    path.write_bytes(raw)
    return str(path)


def read_sticker_b64(meta: Optional[Dict[str, Any]]) -> str:
    """Resolve sticker PNG base64 from cache meta (inline or on-disk)."""
    if not meta:
        return ""
    inline = str(meta.get("file_b64") or "").strip()
    if inline:
        return inline
    file_path = str(meta.get("file_path") or "").strip()
    if not file_path:
        return ""
    path = Path(file_path)
    if not path.is_file():
        return ""
    try:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception:
        return ""


def sticker_img_src(
    meta: Optional[Dict[str, Any]],
    *,
    relative_to: Optional[Path] = None,
) -> str:
    """Prefer on-disk image path for print HTML (avoids huge base64 docs).

    When ``relative_to`` is the directory that will hold the HTML file and the
    PNG lives there, return just the filename so WebEngine can load it as a
    same-folder relative URL (more reliable than cross-path ``file://``).
    """
    if not meta:
        return ""
    file_path = str(meta.get("file_path") or "").strip()
    if file_path:
        path = Path(file_path)
        try:
            if path.is_file() and path.stat().st_size > 0:
                resolved = path.resolve()
                if relative_to is not None:
                    try:
                        if resolved.parent == Path(relative_to).resolve():
                            return resolved.name
                    except OSError:
                        pass
                return resolved.as_uri()
        except OSError:
            pass
    b64 = read_sticker_b64(meta)
    if not b64:
        return ""
    return "data:image/png;base64,{}".format(b64)
