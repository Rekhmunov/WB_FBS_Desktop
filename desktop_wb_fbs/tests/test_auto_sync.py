# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import Database
from app.services.auto_sync import (
    AutoSyncSettings,
    auto_sync_is_due,
    msk_time_in_active_window,
    normalize_hhmm,
    normalize_interval_minutes,
)


_MSK = timezone(timedelta(hours=3))


class HelpersTest(unittest.TestCase):
    def test_normalize_hhmm(self) -> None:
        self.assertEqual(normalize_hhmm("9:5"), "09:05")
        self.assertEqual(normalize_hhmm("bad", "12:00"), "12:00")

    def test_normalize_interval(self) -> None:
        self.assertEqual(normalize_interval_minutes(60), 60)
        self.assertEqual(normalize_interval_minutes(1), 60)  # legacy hour
        self.assertEqual(normalize_interval_minutes(15), 10)  # nearest

    def test_overnight_window(self) -> None:
        # 12:00 → 06:00 overnight
        evening = datetime(2026, 11, 11, 18, 0, tzinfo=_MSK)
        morning = datetime(2026, 11, 11, 3, 0, tzinfo=_MSK)
        midday = datetime(2026, 11, 11, 10, 0, tzinfo=_MSK)
        self.assertTrue(
            msk_time_in_active_window(
                active_from="12:00", active_to="06:00", now_msk=evening
            )
        )
        self.assertTrue(
            msk_time_in_active_window(
                active_from="12:00", active_to="06:00", now_msk=morning
            )
        )
        self.assertFalse(
            msk_time_in_active_window(
                active_from="12:00", active_to="06:00", now_msk=midday
            )
        )

    def test_due(self) -> None:
        self.assertTrue(auto_sync_is_due(last_synced_at=None, interval_minutes=60))
        recent = datetime.now(timezone.utc).isoformat()
        self.assertFalse(
            auto_sync_is_due(last_synced_at=recent, interval_minutes=60)
        )


class SettingsPersistTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.init_schema()
        self.svc = AutoSyncSettings(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_roundtrip(self) -> None:
        saved = self.svc.save(
            "ozon",
            enabled=True,
            interval_minutes=30,
            active_from="14:00",
            active_to="08:00",
        )
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["interval_minutes"], 30)
        self.assertEqual(saved["active_from"], "14:00")
        again = self.svc.get("ozon")
        self.assertEqual(again["interval_minutes"], 30)
        due, reason = self.svc.should_run_now(
            "ozon",
            now_msk=datetime(2026, 11, 11, 15, 0, tzinfo=_MSK),
        )
        self.assertTrue(due)
        self.assertEqual(reason, "due")
        self.svc.mark_synced("ozon")
        due2, reason2 = self.svc.should_run_now(
            "ozon",
            now_msk=datetime(2026, 11, 11, 15, 1, tzinfo=_MSK),
        )
        self.assertFalse(due2)
        self.assertEqual(reason2, "not_due")


if __name__ == "__main__":
    unittest.main()
