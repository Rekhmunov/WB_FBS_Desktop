#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <backup_file> <target_db_path>"
  exit 1
fi

BACKUP_FILE="$1"
TARGET_DB="$2"

if [ ! -f "$BACKUP_FILE" ]; then
  echo "Backup file not found: $BACKUP_FILE"
  exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required"
  exit 1
fi

mkdir -p "$(dirname "$TARGET_DB")"
TMP_DB="${TARGET_DB}.tmp-restore"

cp "$BACKUP_FILE" "$TMP_DB"
sqlite3 "$TMP_DB" "PRAGMA integrity_check;" | rg -q "^ok$" || {
  echo "Integrity check failed for restored DB"
  rm -f "$TMP_DB"
  exit 1
}

mv "$TMP_DB" "$TARGET_DB"
echo "Restore complete: $TARGET_DB"
