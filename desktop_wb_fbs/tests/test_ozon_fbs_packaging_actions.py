# -*- coding: utf-8 -*-
"""Ozon FBS toolbar: Sync → Split → Collect gate."""
from __future__ import annotations

import os
import tempfile
import unittest


def packaging_action_state(*, packaging_tab: bool, multi_count: int, busy: bool):
    """Mirror ``OzonFbsPage._refresh_packaging_actions`` visibility/enable rules."""
    multi = max(0, int(multi_count or 0)) if packaging_tab else 0
    show_split = packaging_tab and multi > 0
    show_ship = packaging_tab
    if busy:
        return {
            "show_split": show_split,
            "show_ship": show_ship,
            "split_enabled": False,
            "ship_enabled": False,
        }
    return {
        "show_split": show_split,
        "show_ship": show_ship,
        "split_enabled": show_split,
        "ship_enabled": packaging_tab and multi <= 0,
    }


class PackagingActionStateTests(unittest.TestCase):
    def test_hide_split_and_enable_collect_when_no_multis(self) -> None:
        state = packaging_action_state(
            packaging_tab=True, multi_count=0, busy=False
        )
        self.assertFalse(state["show_split"])
        self.assertTrue(state["show_ship"])
        self.assertTrue(state["ship_enabled"])

    def test_show_split_and_disable_collect_when_multis_remain(self) -> None:
        state = packaging_action_state(
            packaging_tab=True, multi_count=3, busy=False
        )
        self.assertTrue(state["show_split"])
        self.assertTrue(state["split_enabled"])
        self.assertTrue(state["show_ship"])
        self.assertFalse(state["ship_enabled"])

    def test_hide_split_and_ship_off_packaging_tab(self) -> None:
        state = packaging_action_state(
            packaging_tab=False, multi_count=5, busy=False
        )
        self.assertFalse(state["show_split"])
        self.assertFalse(state["show_ship"])

    def test_busy_disables_actions(self) -> None:
        state = packaging_action_state(
            packaging_tab=True, multi_count=0, busy=True
        )
        self.assertFalse(state["ship_enabled"])
        self.assertFalse(state["split_enabled"])


class PackagingMultiCountFromDbTests(unittest.TestCase):
    def test_preview_multi_drives_collect_gate(self) -> None:
        from app.db import Database
        from app.services.ozon_split import OzonSplitService

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            db = Database(path)
            db.init_schema()
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO supply_sources(
                        name, marketplace, client_id, api_key, is_enabled,
                        lookback_days, created_at
                    ) VALUES ('Озон', 'ozon', '1', 'k', 1, 2, '2026-01-01')
                    """
                )
                sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO ozon_fbs_postings(
                        source_id, posting_number, status, tab, quantity,
                        products_json, synced_at
                    ) VALUES
                    (?, 'S1', 'awaiting_packaging', 'awaiting_packaging', 1,
                     '[{"quantity":1}]', 't'),
                    (?, 'M1', 'awaiting_packaging', 'awaiting_packaging', 4,
                     '[{"quantity":4}]', 't')
                    """,
                    (sid, sid),
                )
                conn.commit()
            multi = int(
                OzonSplitService(db).preview_multi(sid).get("multi_posting_count") or 0
            )
            state = packaging_action_state(
                packaging_tab=True, multi_count=multi, busy=False
            )
            self.assertEqual(multi, 1)
            self.assertTrue(state["show_split"])
            self.assertFalse(state["ship_enabled"])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
