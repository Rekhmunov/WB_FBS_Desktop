# -*- coding: utf-8 -*-
"""Persist QTableWidget column widths across app restarts (app_settings)."""
from __future__ import annotations

import json
from typing import List, Optional, Sequence

from PyQt5.QtCore import QObject, QTimer
from PyQt5.QtWidgets import QHeaderView, QTableWidget

from app.db import Database


def parse_saved_widths(raw: str, count: int, *, min_width: int = 32) -> Optional[List[int]]:
    """Return validated widths from JSON, or None when missing/invalid."""
    if not raw or count <= 0:
        return None
    try:
        widths = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(widths, list) or len(widths) != count:
        return None
    out = []  # type: List[int]
    for w in widths:
        try:
            out.append(max(min_width, int(w)))
        except (TypeError, ValueError):
            return None
    return out


def normalize_defaults(
    defaults: Sequence[int], count: int, *, min_width: int = 32, fill: int = 100
) -> List[int]:
    base = [max(min_width, int(w)) for w in defaults]
    while len(base) < count:
        base.append(max(min_width, fill))
    return base[:count]


class PersistentColumnWidths(QObject):
    """Interactive header widths saved under ``app_settings`` key."""

    def __init__(
        self,
        db: Database,
        table: QTableWidget,
        settings_key: str,
        defaults: Sequence[int],
        parent: Optional[QObject] = None,
        *,
        min_width: int = 32,
        save_delay_ms: int = 400,
    ) -> None:
        super(PersistentColumnWidths, self).__init__(parent)
        self.db = db
        self.table = table
        self.settings_key = str(settings_key or "").strip()
        self.defaults = list(defaults or [])
        self.min_width = int(min_width)
        self._guard = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(max(50, int(save_delay_ms)))
        self._save_timer.timeout.connect(self.persist)
        hdr = self.table.horizontalHeader()
        hdr.setMinimumSectionSize(self.min_width)
        hdr.sectionResized.connect(self._on_section_resized)

    def load_widths(self, count: int) -> List[int]:
        raw = self.db.get_setting(self.settings_key, "") if self.settings_key else ""
        parsed = parse_saved_widths(raw, count, min_width=self.min_width)
        if parsed is not None:
            return parsed
        return normalize_defaults(
            self.defaults, count, min_width=self.min_width
        )

    def apply(self) -> None:
        hdr = self.table.horizontalHeader()
        count = hdr.count()
        if count <= 0:
            return
        widths = self.load_widths(count)
        self._guard = True
        try:
            hdr.setStretchLastSection(False)
            for i in range(count):
                hdr.setSectionResizeMode(i, QHeaderView.Interactive)
                self.table.setColumnWidth(i, widths[i])
        finally:
            self._guard = False

    def persist(self) -> None:
        if self._guard or not self.settings_key:
            return
        hdr = self.table.horizontalHeader()
        count = hdr.count()
        if count <= 0:
            return
        widths = [hdr.sectionSize(i) for i in range(count)]
        self.db.set_setting(
            self.settings_key, json.dumps(widths, separators=(",", ":"))
        )

    def _on_section_resized(
        self, _logical_index: int, _old_size: int, _new_size: int
    ) -> None:
        if self._guard:
            return
        self._save_timer.start()
