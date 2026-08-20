# -*- coding: utf-8 -*-
"""Load bundled Inter / Manrope fonts (web archive parity)."""
from __future__ import annotations

from pathlib import Path
from typing import List

from PyQt5.QtGui import QFont, QFontDatabase

_FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_LOADED = False
_FAMILY_BODY = "Inter"
_FAMILY_DISPLAY = "Inter Display"


def _register(path: Path) -> str:
    if not path.is_file():
        return ""
    font_id = QFontDatabase.addApplicationFont(str(path))
    if font_id < 0:
        return ""
    families = QFontDatabase.applicationFontFamilies(font_id)
    return str(families[0]) if families else ""


def load_app_fonts() -> List[str]:
    """Register bundled TTFs once. Returns loaded family names."""
    global _LOADED, _FAMILY_BODY, _FAMILY_DISPLAY
    if _LOADED:
        return [_FAMILY_BODY, _FAMILY_DISPLAY]
    loaded = []  # type: List[str]
    for name in (
        "Inter-Regular.ttf",
        "Inter-Medium.ttf",
        "Inter-SemiBold.ttf",
        "Inter-Bold.ttf",
        "InterDisplay-SemiBold.ttf",
        "InterDisplay-Bold.ttf",
        "Manrope-Regular.ttf",
        "Manrope-SemiBold.ttf",
        "Manrope-Bold.ttf",
    ):
        fam = _register(_FONTS_DIR / name)
        if not fam:
            continue
        if fam not in loaded:
            loaded.append(fam)
        compact = fam.lower().replace(" ", "")
        if compact == "inter":
            _FAMILY_BODY = fam
        elif compact == "manrope":
            _FAMILY_DISPLAY = fam
        elif "interdisplay" in compact:
            if "manrope" not in _FAMILY_DISPLAY.lower():
                _FAMILY_DISPLAY = fam
    if not loaded:
        _FAMILY_BODY = "Segoe UI"
        _FAMILY_DISPLAY = "Segoe UI"
    _LOADED = True
    return loaded


def body_font(point_size: int = 14, weight: int = QFont.Normal) -> QFont:
    load_app_fonts()
    font = QFont(_FAMILY_BODY, point_size, weight)
    font.setStyleHint(QFont.SansSerif)
    font.setHintingPreference(QFont.PreferFullHinting)
    return font


def display_font(point_size: int = 15, weight: int = QFont.DemiBold) -> QFont:
    load_app_fonts()
    font = QFont(_FAMILY_DISPLAY, point_size, weight)
    font.setStyleHint(QFont.SansSerif)
    font.setHintingPreference(QFont.PreferFullHinting)
    return font


def font_css_stack() -> str:
    """QSS font-family stack after bundled fonts are registered."""
    load_app_fonts()
    return '"{}", "Segoe UI", "Segoe UI Variable", system-ui, sans-serif'.format(
        _FAMILY_BODY
    )


def display_css_stack() -> str:
    load_app_fonts()
    return '"{}", "{}", "Segoe UI", system-ui, sans-serif'.format(
        _FAMILY_DISPLAY, _FAMILY_BODY
    )
