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
    """Interactive header widths saved under ``app_settings`` key.

    Call ``begin_update()`` / ``end_update()`` around bulk table rebuilds so
    cell widgets cannot overwrite the user's widths (and so we do not persist
    those transient resizes). ``end_update()`` re-applies the saved widths.
    """

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
        # Expected column count for the active layout — refuse to persist when
        # the header was already rebuilt for another tab (would corrupt the key).
        self._layout_count = len(self.defaults)
        self.min_width = int(min_width)
        self._guard = False
        self._bulk = 0
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(max(50, int(save_delay_ms)))
        self._save_timer.timeout.connect(self.persist)
        self._reapply_timer = QTimer(self)
        self._reapply_timer.setSingleShot(True)
        self._reapply_timer.setInterval(0)
        self._reapply_timer.timeout.connect(self._on_deferred_reapply)
        hdr = self.table.horizontalHeader()
        hdr.setMinimumSectionSize(self.min_width)
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.sectionResized.connect(self._on_section_resized)

    def begin_update(self) -> None:
        """Suppress persist while the table is being rebuilt."""
        self._bulk += 1
        self._guard = True
        self._save_timer.stop()
        self._reapply_timer.stop()

    def end_update(self, *, reapply: bool = True) -> None:
        """Finish a bulk rebuild; optionally restore saved widths."""
        if self._bulk > 0:
            self._bulk -= 1
        if self._bulk > 0:
            return
        self._save_timer.stop()
        if not reapply:
            self._guard = False
            return
        # Keep guard until deferred re-apply finishes — cell widgets / layout
        # can stretch sections after the first apply and would otherwise be saved.
        self._guard = True
        self.apply()
        self._reapply_timer.start()

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
        prev_guard = self._guard
        self._guard = True
        try:
            hdr.setStretchLastSection(False)
            for i in range(count):
                hdr.setSectionResizeMode(i, QHeaderView.Interactive)
                self.table.setColumnWidth(i, widths[i])
        finally:
            # Preserve caller's guard (bulk update / deferred re-apply).
            self._guard = prev_guard or self._bulk > 0

    def use_layout(
        self, settings_key: str, defaults: Sequence[int]
    ) -> None:
        """Switch layout key and re-apply saved widths for that layout.

        Callers must ``persist()`` while the *previous* layout is still visible
        (before ``setColumnCount``). Auto-persist here used to write the new
        header's column count under the old settings key — e.g. 4-col
        «Доставляются» overwrote the 3-col «Ожидают отгрузки» entry, so
        returning to that tab fell back to defaults.
        """
        new_key = str(settings_key or "").strip()
        self.settings_key = new_key
        self.defaults = list(defaults or [])
        self._layout_count = len(self.defaults)
        self.apply()
        if self._bulk <= 0:
            # Re-apply after deferred layout when switching tabs/layouts.
            self._guard = True
            self._reapply_timer.start()

    def persist(self) -> None:
        if self._guard or self._bulk > 0 or not self.settings_key:
            return
        hdr = self.table.horizontalHeader()
        count = hdr.count()
        if count <= 0:
            return
        # Header already rebuilt for another layout — do not corrupt this key.
        if self._layout_count > 0 and count != self._layout_count:
            return
        widths = [hdr.sectionSize(i) for i in range(count)]
        self.db.set_setting(
            self.settings_key, json.dumps(widths, separators=(",", ":"))
        )

    def _on_deferred_reapply(self) -> None:
        self._guard = True
        self.apply()
        if self._bulk <= 0:
            self._guard = False

    def _on_section_resized(
        self, _logical_index: int, _old_size: int, _new_size: int
    ) -> None:
        if self._guard or self._bulk > 0:
            return
        self._save_timer.start()
