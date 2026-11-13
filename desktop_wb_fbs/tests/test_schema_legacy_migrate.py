# -*- coding: utf-8 -*-
"""Legacy SQLite DBs must upgrade without «no such column» on startup."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import Database


class LegacySchemaMigrateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "legacy.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _bootstrap_legacy(self, sql: str) -> None:
        conn = sqlite3.connect(str(self.path))
        try:
            conn.executescript(sql)
            conn.commit()
        finally:
            conn.close()

    def test_legacy_ozon_postings_without_supply_id(self) -> None:
        self._bootstrap_legacy(
            """
            CREATE TABLE app_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE supply_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                marketplace TEXT NOT NULL DEFAULT 'ozon',
                client_id TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                lookback_days INTEGER NOT NULL DEFAULT 2,
                created_at TEXT NOT NULL,
                last_synced_at TEXT
            );
            CREATE TABLE ozon_fbs_postings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                posting_number TEXT NOT NULL,
                order_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                tab TEXT NOT NULL DEFAULT 'new',
                carriage_id TEXT NOT NULL DEFAULT '',
                offer_id TEXT NOT NULL DEFAULT '',
                sku TEXT NOT NULL DEFAULT '',
                product_name TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 1,
                barcodes_json TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL DEFAULT '{}',
                synced_at TEXT NOT NULL,
                UNIQUE(source_id, posting_number)
            );
            INSERT INTO ozon_fbs_postings
            (source_id, posting_number, synced_at)
            VALUES (1, 'PN-1', '2024-01-01');
            """
        )
        db = Database(self.path)
        db.init_schema()  # must not raise
        with db.connect() as conn:
            cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(ozon_fbs_postings)")
            }
            self.assertIn("supply_id", cols)
            self.assertIn("sticker_barcode", cols)
            row = conn.execute(
                "SELECT supply_id FROM ozon_fbs_postings WHERE posting_number='PN-1'"
            ).fetchone()
        self.assertEqual(str(row["supply_id"] or ""), "")

    def test_legacy_wb_supplies_without_delivery_listed(self) -> None:
        self._bootstrap_legacy(
            """
            CREATE TABLE app_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE wb_fbs_supplies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                supply_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                done INTEGER NOT NULL DEFAULT 0,
                cargo_type INTEGER NOT NULL DEFAULT 0,
                is_b2b INTEGER NOT NULL DEFAULT 0,
                order_ids_json TEXT NOT NULL DEFAULT '[]',
                boxes_json TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL DEFAULT '{}',
                synced_at TEXT NOT NULL,
                UNIQUE (source_id, supply_id)
            );
            INSERT INTO wb_fbs_supplies
            (source_id, supply_id, name, done, synced_at)
            VALUES (1, 'WB-GI-1', 'x', 1, '2024-01-01');
            """
        )
        db = Database(self.path)
        db.init_schema()
        with db.connect() as conn:
            row = conn.execute(
                "SELECT delivery_listed FROM wb_fbs_supplies WHERE supply_id='WB-GI-1'"
            ).fetchone()
        self.assertEqual(int(row["delivery_listed"]), 1)


if __name__ == "__main__":
    unittest.main()
