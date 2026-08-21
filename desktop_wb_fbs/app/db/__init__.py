# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from app.paths import db_path

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS supply_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    marketplace TEXT NOT NULL DEFAULT 'wb',
    api_key TEXT NOT NULL DEFAULT '',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    lookback_days INTEGER NOT NULL DEFAULT 2,
    created_at TEXT NOT NULL,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS product_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    boxes_per_pallet INTEGER,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    supplier_article TEXT NOT NULL DEFAULT '',
    wb_nmid TEXT NOT NULL DEFAULT '',
    ozon_sku TEXT NOT NULL DEFAULT '',
    yandex_offer_id TEXT NOT NULL DEFAULT '',
    box_qty INTEGER,
    product_category TEXT NOT NULL DEFAULT '',
    skip_kiz_gtin_check INTEGER NOT NULL DEFAULT 0,
    photo_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_fbs_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    order_uid TEXT NOT NULL DEFAULT '',
    rid TEXT NOT NULL DEFAULT '',
    article TEXT NOT NULL DEFAULT '',
    nm_id INTEGER,
    chrt_id INTEGER,
    skus_json TEXT NOT NULL DEFAULT '[]',
    price INTEGER NOT NULL DEFAULT 0,
    final_price INTEGER NOT NULL DEFAULT 0,
    currency_code INTEGER NOT NULL DEFAULT 643,
    warehouse_id INTEGER,
    office_id INTEGER,
    offices_json TEXT NOT NULL DEFAULT '[]',
    cargo_type INTEGER NOT NULL DEFAULT 0,
    delivery_type TEXT NOT NULL DEFAULT '',
    supplier_status TEXT NOT NULL DEFAULT '',
    wb_status TEXT NOT NULL DEFAULT '',
    tab TEXT NOT NULL DEFAULT 'new',
    supply_id TEXT NOT NULL DEFAULT '',
    is_archive INTEGER NOT NULL DEFAULT 0,
    is_b2b INTEGER NOT NULL DEFAULT 0,
    comment_text TEXT NOT NULL DEFAULT '',
    created_at_wb TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    synced_at TEXT NOT NULL,
    kiz_codes_json TEXT NOT NULL DEFAULT '[]',
    kiz_saved_at TEXT,
    kiz_wb_synced INTEGER NOT NULL DEFAULT 0,
    pick_verified INTEGER NOT NULL DEFAULT 0,
    pick_barcode TEXT NOT NULL DEFAULT '',
    pick_verified_at TEXT,
    UNIQUE (source_id, order_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_src_tab
    ON wb_fbs_orders(source_id, tab, created_at_wb DESC);

CREATE TABLE IF NOT EXISTS wb_fbs_supplies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    supply_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    cargo_type INTEGER NOT NULL DEFAULT 0,
    is_b2b INTEGER NOT NULL DEFAULT 0,
    destination_office_id INTEGER,
    created_at_wb TEXT,
    closed_at_wb TEXT,
    scan_dt TEXT,
    order_ids_json TEXT NOT NULL DEFAULT '[]',
    boxes_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '{}',
    synced_at TEXT NOT NULL,
    UNIQUE (source_id, supply_id)
);

CREATE INDEX IF NOT EXISTS idx_supplies_src
    ON wb_fbs_supplies(source_id, done, created_at_wb DESC);

-- Persists sticker numbers + order meta across app restarts so supply open
-- can skip WB API when Sync/reload has not invalidated the cache.
CREATE TABLE IF NOT EXISTS wb_fbs_order_open_cache (
    source_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    sticker_part_a TEXT NOT NULL DEFAULT '',
    sticker_part_b TEXT NOT NULL DEFAULT '',
    sticker_barcode TEXT NOT NULL DEFAULT '',
    stickers_ready INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT NOT NULL DEFAULT '{}',
    meta_ready INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_id, order_id)
);

CREATE INDEX IF NOT EXISTS idx_order_open_cache_src
    ON wb_fbs_order_open_cache(source_id);
"""


class Database:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = str(path or db_path())

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            self._migrate_product_photos(conn)
            self._migrate_supply_sources(conn)
            conn.commit()

    @staticmethod
    def _migrate_product_photos(conn: sqlite3.Connection) -> None:
        """Add newer product_photos columns on existing SQLite databases."""
        cols = {
            str(r[1])
            for r in conn.execute("PRAGMA table_info(product_photos)").fetchall()
        }
        for name, decl in (
            ("ozon_sku", "TEXT NOT NULL DEFAULT ''"),
            ("yandex_offer_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in cols:
                conn.execute(
                    "ALTER TABLE product_photos ADD COLUMN {} {}".format(name, decl)
                )

    @staticmethod
    def _migrate_supply_sources(conn: sqlite3.Connection) -> None:
        """Add newer supply_sources columns on existing SQLite databases."""
        cols = {
            str(r[1])
            for r in conn.execute("PRAGMA table_info(supply_sources)").fetchall()
        }
        if "lookback_days" not in cols:
            conn.execute(
                "ALTER TABLE supply_sources ADD COLUMN lookback_days "
                "INTEGER NOT NULL DEFAULT 2"
            )

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )
            conn.commit()

    def all_settings(self) -> Dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {str(r["key"]): str(r["value"]) for r in rows}

    @staticmethod
    def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return dict(row)

    @staticmethod
    def rows_to_dicts(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
        return [dict(r) for r in rows]
