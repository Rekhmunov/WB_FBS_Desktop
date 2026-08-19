#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/feedpilot/app}"
DB_PATH="${APP_DB_PATH:-$APP_ROOT/reviews.db}"
BACKUP_DIR="${BACKUP_DIR:-/opt/feedpilot/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR"

timestamp="$(date +%F_%H-%M-%S)"
backup_file="$BACKUP_DIR/reviews_${timestamp}.db"

sqlite3 "$DB_PATH" ".backup '$backup_file'"
find "$BACKUP_DIR" -type f -name "reviews_*.db" -mtime "+$RETENTION_DAYS" -delete

echo "Backup created: $backup_file"
