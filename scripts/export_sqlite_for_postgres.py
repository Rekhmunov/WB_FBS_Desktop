#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path
from typing import Any


TABLES = [
    "users",
    "sessions",
    "ai_settings",
    "marketplace_accounts",
    "response_templates",
    "response_template_variants",
    "processing_rules",
    "product_recommendations",
    "review_items",
    "review_actions",
    "conversation_items",
]


def _serialize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def export_table(conn: sqlite3.Connection, table: str, output_dir: Path) -> dict[str, Any]:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    columns = [item[1] for item in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    csv_path = output_dir / f"{table}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_serialize_value(row[idx]) for idx in range(len(columns))])
    return {
        "table": table,
        "rows": len(rows),
        "columns": columns,
        "file": csv_path.name,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export SQLite data to CSV files for PostgreSQL dry-run migration.")
    parser.add_argument("--db", required=True, help="Path to SQLite database file")
    parser.add_argument("--out", required=True, help="Output directory for CSV files")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    output_dir = Path(args.out).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        manifest: dict[str, Any] = {
            "source_db": str(db_path),
            "tables": [],
        }
        for table in TABLES:
            record = export_table(conn, table, output_dir)
            manifest["tables"].append(record)
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Export completed: {manifest_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
