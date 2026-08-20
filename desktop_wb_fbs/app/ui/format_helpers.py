# -*- coding: utf-8 -*-
"""UI helpers shared by FBS list / KIZ / pick dialogs (web parity)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget

_pixmap_cache = {}  # type: Dict[tuple, QPixmap]

# Web `_WB_FBS_RU_LAYOUT_TO_EN`
_RU_LAYOUT_TO_EN = {
    "й": "q", "ц": "w", "у": "e", "к": "r", "е": "t", "н": "y", "г": "u",
    "ш": "i", "щ": "o", "з": "p", "х": "[", "ъ": "]", "ф": "a", "ы": "s",
    "в": "d", "а": "f", "п": "g", "р": "h", "о": "j", "л": "k", "д": "l",
    "ж": ";", "э": "'", "я": "z", "ч": "x", "с": "c", "м": "v", "и": "b",
    "т": "n", "ь": "m", "б": ",", "ю": ".", "ё": "`",
    "Й": "Q", "Ц": "W", "У": "E", "К": "R", "Е": "T", "Н": "Y", "Г": "U",
    "Ш": "I", "Щ": "O", "З": "P", "Х": "{", "Ъ": "}", "Ф": "A", "Ы": "S",
    "В": "D", "А": "F", "П": "G", "Р": "H", "О": "J", "Л": "K", "Д": "L",
    "Ж": ":", "Э": '"', "Я": "Z", "Ч": "X", "С": "C", "М": "V", "И": "B",
    "Т": "N", "Ь": "M", "Б": "<", "Ю": ">", "Ё": "~",
}

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def has_cyrillic(value: object) -> bool:
    return bool(_CYRILLIC_RE.search(str(value or "")))


def scan_has_ru_layout(value: object) -> bool:
    """True when scan text was typed/scanned with Russian keyboard layout."""
    text = str(value or "")
    if not text.strip():
        return False
    if has_cyrillic(text):
        return True
    return any(ch in _RU_LAYOUT_TO_EN for ch in text)


RU_LAYOUT_SCAN_TITLE = "Русская раскладка!"
RU_LAYOUT_SCAN_MESSAGE = (
    "Сейчас у вас установлена русская раскладка клавиатуры. "
    "Переключите раскладку на английскую (EN) и отсканируйте код снова."
)


def fix_ru_keyboard_layout(value: object) -> str:
    text = str(value or "")
    out = []
    for ch in text:
        out.append(_RU_LAYOUT_TO_EN.get(ch, ch))
    return "".join(out)


def ago_label(iso: object) -> str:
    """Web `_wbFbsAgo` — relative age from created_at_wb."""
    raw = str(iso or "").strip()
    if not raw:
        return ""
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        sec = max(0, int((now - dt.astimezone(timezone.utc)).total_seconds()))
    except Exception:
        return ""
    if sec < 60:
        return "{} сек назад".format(sec)
    mins = sec // 60
    if mins < 60:
        return "{} мин назад".format(mins)
    hours = mins // 60
    if hours < 48:
        rem = mins % 60
        if rem:
            return "{} ч {} мин назад".format(hours, rem)
        return "{} ч назад".format(hours)
    days = hours // 24
    return "{} дн назад".format(days)


def format_date_short(iso: object) -> str:
    """Web `_fmt_date` — DD.MM.YYYY from WB ISO timestamp."""
    raw = str(iso or "").strip()
    if not raw:
        return ""
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%d.%m.%Y")
    except Exception:
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            return "{}.{}.{}".format(raw[8:10], raw[5:7], raw[0:4])
        return raw[:10]


def make_badge(text: str, kind: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("fbsBadge")
    if kind == "time":
        lab.setProperty("kind", "time")
    elif kind == "cargo":
        lab.setProperty("kind", "cargo")
    elif kind == "pvz":
        lab.setProperty("kind", "pvz")
    lab.setStyleSheet(_badge_qss(kind))
    return lab


def _badge_qss(kind: str) -> str:
    base = (
        "QLabel { padding: 2px 6px; border-radius: 4px; font-size: 11px;"
        " font-weight: 600; }"
    )
    if kind == "time":
        return base + " QLabel { background:#dcfce7; color:#166534; }"
    if kind == "cargo":
        return base + " QLabel { background:#e0f2fe; color:#075985; }"
    if kind == "pvz":
        # Web `.wb-fbs-badge.pvz` — slate, not amber.
        return base + " QLabel { background:#f1f5f9; color:#334155; }"
    if kind == "danger":
        return base + " QLabel { background:#fee2e2; color:#991b1b; }"
    return base + " QLabel { background:#f1f5f9; color:#475569; }"


def make_status_pill(text: str, kind: str = "assembly") -> QLabel:
    """Portal supply status chip (Сборка заказов / Отгрузите поставку / …)."""
    lab = QLabel(text)
    lab.setObjectName("fbsStatusPill")
    lab.setProperty("kind", kind)
    colors = {
        "assembly": ("#eff6ff", "#1d4ed8", "#93c5fd"),
        "ship": ("#fff7ed", "#c2410c", "#fdba74"),
        "scanned": ("#eff6ff", "#1d4ed8", "#93c5fd"),
        "done": ("#f0fdf4", "#166534", "#86efac"),
    }
    bg, fg, border = colors.get(kind, colors["assembly"])
    lab.setStyleSheet(
        "QLabel {{ padding: 4px 10px; border-radius: 8px; font-size: 13px;"
        " font-weight: 600; background: {}; color: {}; border: 1px solid {}; }}".format(
            bg, fg, border
        )
    )
    return lab


def make_photo_label(path: Optional[str], size: int = 72) -> QLabel:
    lab = QLabel()
    lab.setFixedSize(size, size)
    lab.setAlignment(Qt.AlignCenter)
    lab.setStyleSheet(
        "QLabel { background:#f1f5f9; border:1px solid #e2e8f0; border-radius:8px; }"
    )
    p = str(path or "").strip()
    if p:
        cache_key = (p, int(size))
        pix = _pixmap_cache.get(cache_key)
        if pix is None or pix.isNull():
            loaded = QPixmap(p)
            if not loaded.isNull():
                pix = loaded.scaled(
                    size,
                    size,
                    Qt.KeepAspectRatioByExpanding,
                    Qt.FastTransformation,
                )
                _pixmap_cache[cache_key] = pix
        if pix is not None and not pix.isNull():
            lab.setPixmap(pix)
            return lab
    lab.setText("—")
    lab.setStyleSheet(
        lab.styleSheet() + " QLabel { color:#94a3b8; font-size:12px; }"
    )
    return lab


def make_badges_row(*badges: Optional[str], kinds: Optional[list] = None) -> QWidget:
    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    kinds = kinds or []
    for i, text in enumerate(badges):
        if not text:
            continue
        kind = kinds[i] if i < len(kinds) else ""
        lay.addWidget(make_badge(str(text), kind))
    lay.addStretch(1)
    return wrap
