from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import psycopg  # type: ignore
from psycopg import rows as psycopg_rows  # type: ignore

from .models import ProcessedReview, ReviewInput
from .security import decrypt_secret, encrypt_secret, mask_secret

DEFAULT_GROUP_PROCESSORS: dict[str, str] = {
    "positive": "yandex",
    "product_dissatisfaction": "yandex",
    "delivery_problems": "yandex",
    "wrong_size": "yandex",
    "tagged_reviews": "program",
    "textless_ratings": "program",
}

TEMPLATE_VARIABLE_KEY_RE = re.compile(r"^%[A-Z0-9_]{2,50}%$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_subgroup_name(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _build_subgroup_id(group_id: str, subgroup: str) -> str:
    clean_group = str(group_id or "").strip().lower().replace(" ", "_").replace("-", "_")
    normalized_subgroup = _normalize_subgroup_name(subgroup)
    digest = hashlib.sha1(f"{clean_group}|{normalized_subgroup}".encode("utf-8")).hexdigest()[:12]
    return f"{clean_group}__{digest}"


def _replace_qmark_placeholders(query: str) -> str:
    # Convert sqlite-style placeholders to psycopg placeholders.
    result: list[str] = []
    in_single_quote = False
    i = 0
    while i < len(query):
        ch = query[i]
        if ch == "'":
            if in_single_quote and i + 1 < len(query) and query[i + 1] == "'":
                result.append("''")
                i += 2
                continue
            in_single_quote = not in_single_quote
            result.append(ch)
            i += 1
            continue
        if ch == "?" and not in_single_quote:
            result.append("%s")
            i += 1
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _json_load(raw: object, default):
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _coerce_iso_for_storage(value: str | None, *, as_date: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if as_date and len(text) >= 10:
        return text[:10]
    if text.endswith("Z"):
        return text[:-1] + "+00:00"
    return text


def _date_from_created_at_with_lookback(created_at: object, lookback_days: int) -> str:
    lookback = max(int(lookback_days), 0)
    if isinstance(created_at, datetime):
        dt = created_at
    else:
        raw = str(created_at or "").strip()
        if not raw:
            dt = datetime.now(UTC)
        else:
            normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            try:
                dt = datetime.fromisoformat(normalized)
            except ValueError:
                dt = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    base_date = dt.astimezone(UTC).date()
    return (base_date - timedelta(days=lookback)).isoformat()


def _parse_datetime_utc(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class _PgCompatConnection:
    def __init__(self, conn) -> None:
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self._conn.close()

    def execute(self, query: str, params: tuple[Any, ...] = ()):
        cur = self._conn.cursor(row_factory=psycopg_rows.dict_row)  # type: ignore[union-attr]
        normalized_query = _replace_qmark_placeholders(query)
        # Pass query without params when there are none: this avoids psycopg
        # placeholder parsing for SQL literals like '%BRAND%' in seed scripts.
        if params:
            cur.execute(normalized_query, params)
        else:
            cur.execute(normalized_query)
        return cur


class ReviewRepository:
    """Repository for auth, settings, and marketplace reviews."""

    def __init__(self, db_url: str | None = None) -> None:
        self.db_url = str(db_url or "").strip() or None
        if not self.db_url:
            raise RuntimeError("APP_DB_URL is required (postgresql://...)")
        if not self.db_url.startswith("postgres"):
            raise RuntimeError("APP_DB_URL must be a PostgreSQL DSN (postgresql://...)")
        self._init_schema()

    def _connect(self):
        conn = psycopg.connect(self.db_url, row_factory=psycopg_rows.dict_row, autocommit=True)
        return _PgCompatConnection(conn)

    def _sql(self, query: str) -> str:
        return _replace_qmark_placeholders(query)

    def _bool_db(self, value: bool | None) -> bool | None:
        if value is None:
            return None
        return bool(value)

    def _bool_true_literal(self) -> str:
        return "TRUE"

    def _json_param(self, value: object) -> object:
        return json.dumps(value, ensure_ascii=False)

    def _default_sync_lookback_days(self) -> int:
        return 7

    def _coerce_lookback_days(self, value: object | None) -> int:
        try:
            parsed = int(value) if value is not None else self._default_sync_lookback_days()
        except (TypeError, ValueError):
            parsed = self._default_sync_lookback_days()
        return min(max(parsed, 0), 365)

    @staticmethod
    def _is_effective_paid_status(status: object) -> bool:
        normalized = str(status or "").strip().lower()
        return normalized in {"paid", "succeeded", "success", "completed"}

    def _init_schema(self) -> None:
        sql_path = Path(__file__).resolve().parent.parent / "deploy" / "postgres" / "schema_v1.sql"
        if not sql_path.exists():
            raise RuntimeError(f"PostgreSQL schema file not found: {sql_path}")
        schema_sql = sql_path.read_text(encoding="utf-8")
        # psycopg interprets "%" markers in query text as placeholders even
        # when executing raw SQL scripts. Escape percent literals such as
        # %BRAND%/%NAME% used in seed data to keep bootstrap stable.
        schema_sql = schema_sql.replace("%", "%%")
        with self._connect() as conn:
            conn.execute(schema_sql)
            self._migrate_schema(conn)
    def _migrate_schema(self, conn) -> None:
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS owner_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS is_super_admin BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS blocked_reason TEXT
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMPTZ
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS plan_code TEXT NOT NULL DEFAULT 'starter'
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS limits_override_json JSONB NOT NULL DEFAULT '{}'::jsonb
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS use_sync_start_date BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS sync_start_date DATE
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_auto_sync_enabled BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_auto_sync_interval_hours INTEGER NOT NULL DEFAULT 1
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_auto_sync_active_from TEXT NOT NULL DEFAULT '12:00'
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_auto_sync_active_to TEXT NOT NULL DEFAULT '06:00'
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_sync_lookback_days INTEGER NOT NULL DEFAULT 3
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_last_synced_at TIMESTAMPTZ
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_last_auto_synced_at TIMESTAMPTZ
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_auto_collect_mgt_enabled BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_auto_collect_mgt_interval_hours INTEGER NOT NULL DEFAULT 1
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_auto_collect_mgt_active_from TEXT NOT NULL DEFAULT '12:00'
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_auto_collect_mgt_active_to TEXT NOT NULL DEFAULT '06:00'
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_auto_collect_mgt_last_run_at TIMESTAMPTZ
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_auto_collect_mgt_last_status TEXT NOT NULL DEFAULT ''
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_auto_collect_mgt_last_detail TEXT NOT NULL DEFAULT ''
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS wb_fbs_last_collect_mgt_at TIMESTAMPTZ
            """
        )
        # Seed "any collect" from known auto-collect runs (manual history unavailable).
        conn.execute(
            """
            UPDATE users
            SET wb_fbs_last_collect_mgt_at = wb_fbs_auto_collect_mgt_last_run_at
            WHERE wb_fbs_last_collect_mgt_at IS NULL
              AND wb_fbs_auto_collect_mgt_last_run_at IS NOT NULL
            """
        )
        conn.execute(
            """
            UPDATE users
            SET owner_user_id = id
            WHERE owner_user_id IS NULL
            """
        )
        super_admin_row = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_super_admin = TRUE").fetchone()
        has_super_admin = int(super_admin_row["c"]) > 0 if super_admin_row else False
        if not has_super_admin:
            candidate = conn.execute(
                "SELECT id FROM users WHERE role = 'admin' AND is_deleted = FALSE ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if candidate is not None:
                conn.execute(
                    "UPDATE users SET is_super_admin = TRUE WHERE id = ?",
                    (int(candidate["id"]),),
                )

        conn.execute(
            """
            ALTER TABLE ai_settings
            ADD COLUMN IF NOT EXISTS group_processors_json JSONB NOT NULL DEFAULT '{}'::jsonb
            """
        )
        conn.execute(
            """
            ALTER TABLE ai_settings
            ADD COLUMN IF NOT EXISTS use_sync_start_date BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE ai_settings
            ADD COLUMN IF NOT EXISTS sync_start_date DATE
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_settings (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                payment_provider TEXT NOT NULL DEFAULT 'manual',
                payment_api_key_encrypted TEXT,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        conn.execute(
            """
            ALTER TABLE platform_settings
            ADD COLUMN IF NOT EXISTS default_sync_lookback_days INTEGER NOT NULL DEFAULT 7
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tariff_plans (
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                monthly_price NUMERIC(14,2) NOT NULL DEFAULT 0,
                limits_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_records (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                amount NUMERIC(14,2) NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                status TEXT NOT NULL,
                external_payment_id TEXT,
                details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                paid_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_subscriptions (
                owner_user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'inactive',
                active_from TIMESTAMPTZ,
                paid_until TIMESTAMPTZ,
                grace_until TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manager_permissions (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                manager_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                account_id BIGINT NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                can_reviews BOOLEAN NOT NULL DEFAULT FALSE,
                can_questions BOOLEAN NOT NULL DEFAULT FALSE,
                can_chats BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE(manager_user_id, account_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_manager_permissions_manager
            ON manager_permissions(manager_user_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manager_supply_permissions (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                manager_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                can_supply_settings BOOLEAN NOT NULL DEFAULT FALSE,
                can_supply_poa BOOLEAN NOT NULL DEFAULT FALSE,
                sources_json TEXT NOT NULL DEFAULT '{}',
                updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE(manager_user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS default_template_variants (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                group_id TEXT NOT NULL,
                subgroup TEXT NOT NULL,
                template_text TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE(group_id, subgroup, template_text)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS default_template_subgroups (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                group_id TEXT NOT NULL,
                subgroup_id TEXT,
                subgroup TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE(group_id, subgroup)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_default_template_variants_group_sub
            ON default_template_variants(group_id, subgroup, is_active)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_default_template_subgroups_group_sub
            ON default_template_subgroups(group_id, subgroup)
            """
        )
        subgroup_columns = self._table_columns(conn, "default_template_subgroups")
        if "subgroup_id" not in subgroup_columns:
            conn.execute("ALTER TABLE default_template_subgroups ADD COLUMN subgroup_id TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_default_template_subgroups_subgroup_id
            ON default_template_subgroups(subgroup_id)
            WHERE subgroup_id IS NOT NULL
            """
        )
        conn.execute(
            """
            INSERT INTO default_template_subgroups (group_id, subgroup, created_at, updated_at)
            SELECT DISTINCT group_id, subgroup, NOW(), NOW()
            FROM default_template_variants
            WHERE TRIM(group_id) <> '' AND TRIM(subgroup) <> ''
            ON CONFLICT (group_id, subgroup) DO NOTHING
            """
        )
        rows = conn.execute(
            """
            SELECT group_id, subgroup
            FROM default_template_subgroups
            WHERE subgroup_id IS NULL OR TRIM(subgroup_id) = ''
            ORDER BY group_id ASC, subgroup ASC
            """
        ).fetchall()
        for row in rows:
            subgroup_id = _build_subgroup_id(str(row["group_id"] or ""), str(row["subgroup"] or ""))
            conn.execute(
                """
                UPDATE default_template_subgroups
                SET subgroup_id = ?, updated_at = NOW()
                WHERE group_id = ? AND subgroup = ?
                """,
                (subgroup_id, str(row["group_id"] or ""), str(row["subgroup"] or "")),
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS template_variables (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                var_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT,
                is_user_editable BOOLEAN NOT NULL DEFAULT FALSE,
                source_type TEXT NOT NULL DEFAULT 'manual',
                source_path TEXT,
                default_value TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_template_variable_values (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                variable_id BIGINT NOT NULL REFERENCES template_variables(id) ON DELETE CASCADE,
                value TEXT,
                updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE(user_id, variable_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_template_variables_active
            ON template_variables(is_active, var_key)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_template_values_user
            ON user_template_variable_values(user_id, variable_id)
            """
        )
        conn.execute(
            """
            INSERT INTO platform_settings (id, payment_provider, payment_api_key_encrypted, updated_at)
            VALUES (1, 'manual', NULL, NOW())
            ON CONFLICT (id) DO NOTHING
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS use_sync_start_date BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS sync_start_date DATE
            """
        )
        conn.execute(
            """
            UPDATE users
            SET use_sync_start_date = TRUE
            WHERE use_sync_start_date IS DISTINCT FROM TRUE
            """
        )
        conn.execute(
            """
            UPDATE users
            SET sync_start_date = ((created_at AT TIME ZONE 'UTC')::date - COALESCE((SELECT default_sync_lookback_days FROM platform_settings WHERE id = 1), 7))
            WHERE sync_start_date IS NULL
            """
        )
        conn.execute(
            """
            UPDATE platform_settings
            SET default_sync_lookback_days = COALESCE(default_sync_lookback_days, 7)
            WHERE id = 1
            """
        )
        conn.execute(
            """
            INSERT INTO tenant_subscriptions (owner_user_id, status, active_from, paid_until, grace_until, updated_at)
            SELECT u.id, 'active', u.created_at, NULL, NULL, NOW()
            FROM users u
            WHERE u.owner_user_id = u.id
              AND u.is_super_admin = FALSE
              AND u.is_deleted = FALSE
            ON CONFLICT (owner_user_id) DO NOTHING
            """
        )
        conn.execute(
            """
            ALTER TABLE conversation_items
            ADD COLUMN IF NOT EXISTS send_error_code TEXT
            """
        )
        conn.execute(
            """
            ALTER TABLE conversation_items
            ADD COLUMN IF NOT EXISTS send_error_message TEXT
            """
        )
        conn.execute(
            """
            ALTER TABLE conversation_items
            ADD COLUMN IF NOT EXISTS send_attempts INTEGER NOT NULL DEFAULT 0
            """
        )
        conn.execute(
            """
            ALTER TABLE conversation_items
            ADD COLUMN IF NOT EXISTS last_send_attempt_at TIMESTAMPTZ
            """
        )
        conn.execute(
            """
            ALTER TABLE conversation_items
            ADD COLUMN IF NOT EXISTS last_sent_at TIMESTAMPTZ
            """
        )
        # Convert last_sent_at and last_send_attempt_at from TIMESTAMPTZ to
        # TEXT so they are stored as ISO-8601 strings, consistent with
        # last_message_at and all other timestamp columns.  This makes
        # lexicographic comparisons correct and removes implicit type
        # coercion surprises.
        conn.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'conversation_items'
                      AND column_name = 'last_sent_at'
                      AND data_type = 'timestamp with time zone'
                ) THEN
                    ALTER TABLE conversation_items
                        ALTER COLUMN last_sent_at TYPE TEXT
                        USING CASE
                            WHEN last_sent_at IS NULL THEN NULL
                            ELSE to_char(last_sent_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"')
                        END;
                END IF;
            END;
            $$
            """
        )
        conn.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'conversation_items'
                      AND column_name = 'last_send_attempt_at'
                      AND data_type = 'timestamp with time zone'
                ) THEN
                    ALTER TABLE conversation_items
                        ALTER COLUMN last_send_attempt_at TYPE TEXT
                        USING CASE
                            WHEN last_send_attempt_at IS NULL THEN NULL
                            ELSE to_char(last_send_attempt_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"')
                        END;
                END IF;
            END;
            $$
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                conversation_uid TEXT NOT NULL REFERENCES conversation_items(conversation_uid) ON DELETE CASCADE,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                direction TEXT NOT NULL,
                message_text TEXT NOT NULL,
                operator_name TEXT,
                send_status TEXT NOT NULL DEFAULT 'sent',
                send_error_code TEXT,
                send_error_message TEXT,
                idempotency_key TEXT,
                external_message_id TEXT,
                created_at TIMESTAMPTZ NOT NULL,
                UNIQUE (user_id, conversation_uid, idempotency_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_quick_templates (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                template_text TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_messages_conversation_created
            ON conversation_messages(conversation_uid, created_at ASC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_quick_templates_user
            ON chat_quick_templates(user_id, updated_at DESC)
            """
        )
        conn.execute(
            """
            ALTER TABLE chat_quick_templates
            ADD COLUMN IF NOT EXISTS template_name TEXT NOT NULL DEFAULT ''
            """
        )
        # Migrate textless_ratings subgroups
        self._migrate_textless_subgroups(conn)
        # AI tables + stock tables
        self._migrate_ai_request_log_table(conn)
        self._migrate_ai_usage_table(conn)
        self._migrate_stock_tables(conn)
        # Question quick templates
        self._migrate_question_quick_templates(conn)
        # Review send error tracking
        self._migrate_review_send_error_columns(conn)
        # Salary rates
        self._migrate_salary_tables(conn)
        # Tariff plans are not auto-created by migration to avoid restoring
        # plans that were intentionally removed by super-admin.
        return

        user_columns = self._table_columns(conn, "users")
        if "owner_user_id" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN owner_user_id INTEGER")
        if "is_super_admin" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_super_admin INTEGER NOT NULL DEFAULT 0")
        if "is_blocked" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0")
        if "blocked_reason" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN blocked_reason TEXT")
        if "blocked_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN blocked_at TEXT")
        if "is_deleted" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")
        if "deleted_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN deleted_at TEXT")
        if "plan_code" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN plan_code TEXT NOT NULL DEFAULT 'starter'")
        if "limits_override_json" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN limits_override_json TEXT NOT NULL DEFAULT '{}'")
        if "use_sync_start_date" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN use_sync_start_date INTEGER NOT NULL DEFAULT 0")
        if "sync_start_date" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN sync_start_date TEXT")
        conn.execute("UPDATE users SET owner_user_id = id WHERE owner_user_id IS NULL")
        super_admin_row = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_super_admin = TRUE").fetchone()
        has_super_admin = int(super_admin_row["c"]) > 0 if super_admin_row else False
        if not has_super_admin:
            candidate = conn.execute(
                "SELECT id FROM users WHERE role = 'admin' AND is_deleted = FALSE ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if candidate is not None:
                conn.execute(
                    "UPDATE users SET is_super_admin = TRUE WHERE id = ?",
                    (int(candidate["id"]),),
                )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payment_provider TEXT NOT NULL DEFAULT 'manual',
                payment_api_key_encrypted TEXT,
                default_sync_lookback_days INTEGER NOT NULL DEFAULT 7,
                updated_at TEXT NOT NULL
            )
            """
        )
        platform_columns = self._table_columns(conn, "platform_settings")
        if "default_sync_lookback_days" not in platform_columns:
            conn.execute("ALTER TABLE platform_settings ADD COLUMN default_sync_lookback_days INTEGER NOT NULL DEFAULT 7")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tariff_plans (
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                monthly_price REAL NOT NULL DEFAULT 0,
                limits_json TEXT NOT NULL DEFAULT '{}',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                status TEXT NOT NULL,
                external_payment_id TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                paid_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_subscriptions (
                owner_user_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'inactive',
                active_from TEXT,
                paid_until TEXT,
                grace_until TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manager_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_user_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                can_reviews INTEGER NOT NULL DEFAULT 0,
                can_questions INTEGER NOT NULL DEFAULT 0,
                can_chats INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(manager_user_id, account_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_manager_permissions_manager
            ON manager_permissions(manager_user_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manager_supply_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_user_id INTEGER NOT NULL,
                can_supply_settings INTEGER NOT NULL DEFAULT 0,
                can_supply_poa INTEGER NOT NULL DEFAULT 0,
                sources_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                UNIQUE(manager_user_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tenant_subscriptions (owner_user_id, status, active_from, paid_until, grace_until, updated_at)
            SELECT u.id, 'active', u.created_at, NULL, NULL, ?
            FROM users u
            WHERE u.owner_user_id = u.id
              AND u.is_super_admin = FALSE
              AND u.is_deleted = FALSE
            ON CONFLICT (owner_user_id) DO NOTHING
            """,
            (_utc_now(),),
        )
        conversation_columns = self._table_columns(conn, "conversation_items")
        if "send_error_code" not in conversation_columns:
            conn.execute("ALTER TABLE conversation_items ADD COLUMN send_error_code TEXT")
        if "send_error_message" not in conversation_columns:
            conn.execute("ALTER TABLE conversation_items ADD COLUMN send_error_message TEXT")
        if "send_attempts" not in conversation_columns:
            conn.execute("ALTER TABLE conversation_items ADD COLUMN send_attempts INTEGER NOT NULL DEFAULT 0")
        if "last_send_attempt_at" not in conversation_columns:
            conn.execute("ALTER TABLE conversation_items ADD COLUMN last_send_attempt_at TEXT")
        if "last_sent_at" not in conversation_columns:
            conn.execute("ALTER TABLE conversation_items ADD COLUMN last_sent_at TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_uid TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                message_text TEXT NOT NULL,
                operator_name TEXT,
                send_status TEXT NOT NULL DEFAULT 'sent',
                send_error_code TEXT,
                send_error_message TEXT,
                idempotency_key TEXT,
                external_message_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, conversation_uid, idempotency_key),
                FOREIGN KEY (conversation_uid) REFERENCES conversation_items(conversation_uid) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_quick_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                template_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_messages_conversation_created
            ON conversation_messages(conversation_uid, created_at ASC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_quick_templates_user
            ON chat_quick_templates(user_id, updated_at DESC)
            """
        )
        tpl_columns = self._table_columns(conn, "chat_quick_templates")
        if "template_name" not in tpl_columns:
            conn.execute(
                "ALTER TABLE chat_quick_templates ADD COLUMN template_name TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS default_template_variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                subgroup TEXT NOT NULL,
                template_text TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(group_id, subgroup, template_text)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS default_template_subgroups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                subgroup_id TEXT,
                subgroup TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(group_id, subgroup)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_default_template_variants_group_sub
            ON default_template_variants(group_id, subgroup, is_active)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_default_template_subgroups_group_sub
            ON default_template_subgroups(group_id, subgroup)
            """
        )
        subgroup_columns = self._table_columns(conn, "default_template_subgroups")
        if "subgroup_id" not in subgroup_columns:
            conn.execute("ALTER TABLE default_template_subgroups ADD COLUMN subgroup_id TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_default_template_subgroups_subgroup_id
            ON default_template_subgroups(subgroup_id)
            WHERE subgroup_id IS NOT NULL
            """
        )
        conn.execute(
            """
            INSERT INTO default_template_subgroups (group_id, subgroup, created_at, updated_at)
            SELECT DISTINCT group_id, subgroup, ?, ?
            FROM default_template_variants
            WHERE TRIM(group_id) <> '' AND TRIM(subgroup) <> ''
            ON CONFLICT (group_id, subgroup) DO NOTHING
            """,
            (_utc_now(), _utc_now()),
        )
        rows = conn.execute(
            """
            SELECT group_id, subgroup
            FROM default_template_subgroups
            WHERE subgroup_id IS NULL OR TRIM(subgroup_id) = ''
            ORDER BY group_id ASC, subgroup ASC
            """
        ).fetchall()
        now = _utc_now()
        for row in rows:
            subgroup_id = _build_subgroup_id(str(row["group_id"] or ""), str(row["subgroup"] or ""))
            conn.execute(
                """
                UPDATE default_template_subgroups
                SET subgroup_id = ?, updated_at = ?
                WHERE group_id = ? AND subgroup = ?
                """,
                (subgroup_id, now, str(row["group_id"] or ""), str(row["subgroup"] or "")),
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS template_variables (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                var_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT,
                is_user_editable INTEGER NOT NULL DEFAULT 0,
                source_type TEXT NOT NULL DEFAULT 'manual',
                source_path TEXT,
                default_value TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_template_variable_values (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                variable_id INTEGER NOT NULL,
                value TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, variable_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (variable_id) REFERENCES template_variables(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_template_variables_active
            ON template_variables(is_active, var_key)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_template_values_user
            ON user_template_variable_values(user_id, variable_id)
            """
        )
        conn.execute(
            """
            INSERT INTO platform_settings (id, payment_provider, payment_api_key_encrypted, default_sync_lookback_days, updated_at)
            VALUES (1, 'manual', NULL, 7, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            (_utc_now(),),
        )
        conn.execute(
            """
            UPDATE platform_settings
            SET default_sync_lookback_days = COALESCE(default_sync_lookback_days, 7)
            WHERE id = 1
            """
        )
        conn.execute(
            """
            UPDATE users
            SET use_sync_start_date = 1
            WHERE use_sync_start_date IS NULL OR use_sync_start_date = 0
            """
        )
        conn.execute(
            """
            UPDATE users
            SET sync_start_date = date(substr(created_at, 1, 10), '-' || COALESCE((SELECT default_sync_lookback_days FROM platform_settings WHERE id = 1), 7) || ' days')
            WHERE sync_start_date IS NULL
            """
        )

        # Remove tagged_reviews group — no longer supported.
        conn.execute("DELETE FROM default_template_variants WHERE group_id = 'tagged_reviews'")
        conn.execute("DELETE FROM default_template_subgroups WHERE group_id = 'tagged_reviews'")

        # Tariff plans are managed exclusively by super-admin and should not be
        # reseeded automatically during SQLite migrations.

        # Migrate textless_ratings subgroups from old 2-band structure to 5 per-star.
        self._migrate_textless_subgroups(conn)
        # AI request log table (1-day debug log)
        self._migrate_ai_request_log_table(conn)
        # AI usage statistics table
        self._migrate_ai_usage_table(conn)
        # Stock module tables
        self._migrate_stock_tables(conn)
        # Question quick templates
        self._migrate_question_quick_templates(conn)
        # Review quick templates (separate from question templates)
        self._migrate_review_quick_templates(conn)
        # Review send error tracking
        self._migrate_review_send_error_columns(conn)
        # Review quick templates
        self._migrate_review_quick_templates(conn)
        # Review contradiction rules
        self._migrate_review_contradiction_rules(conn)
        # Product photos catalog
        self._migrate_product_photos(conn)
        self._migrate_product_categories(conn)
        # manually_closed_at for chat conversations
        self._migrate_manually_closed_at(conn)
        # Supply module tables (FBW/FBS)
        self._migrate_supply_tables(conn)
        # WB FBS orders (marketplace-api) — isolated from FBW supplies
        try:
            from .wb_fbs import ensure_wb_fbs_tables

            ensure_wb_fbs_tables(self)
        except Exception:
            pass
        # One-time cleanup: reset stale can_salary / can_supplies flags on managers
        self._run_migration_once(conn, "cleanup_stale_manager_access_flags", self._cleanup_stale_manager_access_flags)
        # Remove tagged_reviews group — it is no longer supported.
        # pros/cons fields are now merged into review.text before classification.
        conn.execute(
            self._sql("DELETE FROM default_template_variants WHERE group_id = 'tagged_reviews'")
        )
        conn.execute(
            self._sql("DELETE FROM default_template_subgroups WHERE group_id = 'tagged_reviews'")
        )

    def _run_migration_once(self, conn, name: str, fn) -> None:
        """Run a migration function exactly once, tracked by name in applied_migrations."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applied_migrations (
                name TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        row = conn.execute(
            self._sql("SELECT 1 FROM applied_migrations WHERE name = ? LIMIT 1"),
            (name,),
        ).fetchone()
        if row is not None:
            return
        fn(conn)
        conn.execute(
            self._sql("INSERT INTO applied_migrations (name, applied_at) VALUES (?, NOW()) ON CONFLICT (name) DO NOTHING"),
            (name,),
        )

    def _cleanup_stale_manager_access_flags(self, conn) -> None:
        """One-time: reset can_salary and can_supplies for feedback_managers
        whose flags were set stale due to leftover pendingCanSalary/pendingCanSupplies
        state leaking between manager-creation sessions.

        - can_salary: reset for ALL feedback_managers (new feature; any true value
          is from the stale-state bug, not an intentional grant).
        - can_supplies: reset only for managers whose manager_supply_permissions
          row has no actual permissions (all false / no row at all).
        """
        import json as _json

        _mgr_role = "feedback_manager"

        # 1. Reset can_salary for all feedback_managers
        conn.execute(
            self._sql(
                "UPDATE users SET can_salary = FALSE WHERE role = ? AND can_salary = TRUE"
            ),
            (_mgr_role,),
        )

        # 2. Reset can_supplies for managers with no actual supply permissions
        rows = conn.execute(
            self._sql(
                "SELECT id FROM users WHERE role = ? AND can_supplies = TRUE"
            ),
            (_mgr_role,),
        ).fetchall()
        for row in rows:
            manager_id = int((row.get("id") if hasattr(row, "get") else row[0]))
            try:
                perm_row = conn.execute(
                    self._sql(
                        "SELECT can_supply_settings, can_supply_poa, sources_json "
                        "FROM manager_supply_permissions WHERE manager_user_id = ? LIMIT 1"
                    ),
                    (manager_id,),
                ).fetchone()
            except Exception:
                continue  # table or column not ready yet — skip this manager
            if perm_row is None:
                # No supply permissions row at all → stale flag
                conn.execute(
                    self._sql("UPDATE users SET can_supplies = FALSE WHERE id = ?"),
                    (manager_id,),
                )
                continue
            d = perm_row if not hasattr(perm_row, "get") else dict(perm_row)
            if d.get("can_supply_settings") or d.get("can_supply_poa"):
                continue  # has at least one non-source permission → legitimate
            try:
                sources = _json.loads(d.get("sources_json") or "{}")
            except Exception:
                sources = {}
            if any(
                (
                    isinstance(v, dict)
                    and (
                        v.get("wb")
                        or v.get("wb_fbs")
                        or v.get("wb_fbs_tsd")
                        or v.get("ozon")
                    )
                )
                for v in sources.values()
            ):
                continue  # has at least one source permission → legitimate
            # All supply permissions are false → stale flag
            conn.execute(
                self._sql("UPDATE users SET can_supplies = FALSE WHERE id = ?"),
                (manager_id,),
            )

    def _migrate_ai_request_log_table(self, conn) -> None:
        """Create ai_request_log table for Yandex GPT request/response debugging."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_request_log (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                created_at TEXT NOT NULL,
                prompt_system TEXT NOT NULL DEFAULT '',
                prompt_user TEXT NOT NULL DEFAULT '',
                response_text TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                model_uri TEXT NOT NULL DEFAULT '',
                review_rating INTEGER,
                classified_group TEXT NOT NULL DEFAULT '',
                classified_subgroup TEXT NOT NULL DEFAULT ''
            )
        """)

        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_ai_request_log_user_date ON ai_request_log(user_id, created_at DESC)"
        ))

    def _migrate_ai_usage_table(self, conn) -> None:
        """Create ai_usage_log table for Yandex token statistics."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_usage_log (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                log_date TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                requests INTEGER NOT NULL DEFAULT 1,
                model_uri TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)

        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_ai_usage_user_date ON ai_usage_log(user_id, log_date DESC)"
        ))

    def _migrate_stock_tables(self, conn) -> None:
        """Create stock module tables if they don't exist yet."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_sources (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                account_name TEXT NOT NULL,
                api_url TEXT NOT NULL DEFAULT '',
                api_key_encrypted TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT '{}',
                interval_hours INTEGER NOT NULL DEFAULT 24,
                retention_days INTEGER NOT NULL DEFAULT 30,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                last_synced_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_reports (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                source_id BIGINT NOT NULL REFERENCES stock_sources(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                downloaded_at TEXT NOT NULL,
                file_path TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                rows_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ok',
                error_message TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_data (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                source_id BIGINT NOT NULL REFERENCES stock_sources(id) ON DELETE CASCADE,
                report_id BIGINT REFERENCES stock_reports(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                report_date TEXT NOT NULL,
                wb_article TEXT NOT NULL DEFAULT '',
                seller_article TEXT NOT NULL DEFAULT '',
                barcode TEXT NOT NULL DEFAULT '',
                warehouse_name TEXT NOT NULL DEFAULT '',
                current_stock INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_stock_sources_user ON stock_sources(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_stock_reports_source ON stock_reports(source_id)",
            "CREATE INDEX IF NOT EXISTS idx_stock_data_source_date ON stock_data(source_id, report_date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_stock_data_article ON stock_data(source_id, wb_article, warehouse_name)",
        ]:
            conn.execute(self._sql(idx_sql))

        # Product catalog for name substitution in stock table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS product_catalog (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_name TEXT NOT NULL DEFAULT '',
                wb_article TEXT NOT NULL DEFAULT '',
                ozon_article TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, wb_article)
            )
        """)
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_product_catalog_user ON product_catalog(user_id)"
        ))
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_product_catalog_wb ON product_catalog(user_id, wb_article)"
        ))

    def _migrate_question_quick_templates(self, conn) -> None:
        """Create question_quick_templates table (separate from chat templates)."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS question_quick_templates (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                template_name TEXT NOT NULL DEFAULT '',
                template_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_question_quick_templates_user "
            "ON question_quick_templates(user_id, updated_at DESC)"
        ))

    def _migrate_review_send_error_columns(self, conn) -> None:
        """Add send_error_message and send_attempts columns to review_items."""
        conn.execute("""
            ALTER TABLE review_items
            ADD COLUMN IF NOT EXISTS send_error_message TEXT
        """)
        conn.execute("""
            ALTER TABLE review_items
            ADD COLUMN IF NOT EXISTS send_attempts INTEGER NOT NULL DEFAULT 0
        """)

    def _migrate_salary_tables(self, conn) -> None:
        """Create salary_rates table for per-tenant operator payout rates."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salary_rates (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                rate_review NUMERIC(10,2) NOT NULL DEFAULT 0,
                rate_question NUMERIC(10,2) NOT NULL DEFAULT 0,
                rate_chat NUMERIC(10,2) NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE (owner_user_id)
            )
        """)

    def _migrate_textless_subgroups(self, conn) -> None:
        """Replace legacy '1-3 звезды' / '4-5 звезд' subgroups with 5 per-star subgroups.

        Safe to run multiple times — checks for old subgroup existence first.
        Moves any templates from old subgroups to the appropriate new ones.
        """
        now = _utc_now()
        old_low = "1-3 звезды"
        old_high = "4-5 звезд"
        group_id = "textless_ratings"
        new_subgroups = ["1 звезда", "2 звезды", "3 звезды", "4 звезды", "5 звезд"]

        # Check if migration is needed (old subgroups still exist)
        old_rows = conn.execute(
            "SELECT subgroup FROM default_template_subgroups WHERE group_id = ? AND subgroup IN (?, ?)",
            (group_id, old_low, old_high),
        ).fetchall()
        if not old_rows:
            # Already migrated — just ensure new subgroups exist (upsert safely)
            for sg in new_subgroups:
                sg_id = _build_subgroup_id(group_id, sg)
                existing = conn.execute(
                    self._sql("SELECT id FROM default_template_subgroups WHERE group_id = ? AND subgroup = ?"),
                    (group_id, sg),
                ).fetchone()
                if not existing:
                    conn.execute(
                        self._sql("""
                        INSERT INTO default_template_subgroups (group_id, subgroup_id, subgroup, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """),
                        (group_id, sg_id, sg, now, now),
                    )
            return

        # Migrate templates from old low band (1-3) to stars 1, 2, 3
        low_templates = conn.execute(
            "SELECT template_text, is_active FROM default_template_variants WHERE group_id = ? AND subgroup = ?",
            (group_id, old_low),
        ).fetchall()
        # Migrate templates from old high band (4-5) to stars 4, 5
        high_templates = conn.execute(
            "SELECT template_text, is_active FROM default_template_variants WHERE group_id = ? AND subgroup = ?",
            (group_id, old_high),
        ).fetchall()

        # Create new subgroups and copy templates
        star_to_templates = {
            "1 звезда": low_templates,
            "2 звезды": low_templates,
            "3 звезды": low_templates,
            "4 звезды": high_templates,
            "5 звезд": high_templates,
        }
        for sg, templates in star_to_templates.items():
            sg_id = _build_subgroup_id(group_id, sg)
            # Delete first to avoid conflicts on both (group_id,subgroup) and subgroup_id indexes
            conn.execute(
                self._sql("DELETE FROM default_template_subgroups WHERE group_id = ? AND subgroup = ?"),
                (group_id, sg),
            )
            conn.execute(
                self._sql("DELETE FROM default_template_subgroups WHERE subgroup_id = ?"),
                (sg_id,),
            )
            conn.execute(
                self._sql("""
                INSERT INTO default_template_subgroups (group_id, subgroup_id, subgroup, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """),
                (group_id, sg_id, sg, now, now),
            )
            for tmpl_row in templates:
                text = str(tmpl_row["template_text"] if hasattr(tmpl_row, "__getitem__") else tmpl_row[0])
                is_active_val = self._bool_db(True)
                conn.execute(
                    self._sql("""
                    INSERT INTO default_template_variants (group_id, subgroup, template_text, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (group_id, subgroup, template_text) DO NOTHING
                    """),
                    (group_id, sg, text, is_active_val, now, now),
                )

        # Remove old subgroups and their templates
        for old_sg in (old_low, old_high):
            conn.execute(
                "DELETE FROM default_template_variants WHERE group_id = ? AND subgroup = ?",
                (group_id, old_sg),
            )
            conn.execute(
                "DELETE FROM default_template_subgroups WHERE group_id = ? AND subgroup = ?",
                (group_id, old_sg),
            )

    def _table_columns(self, conn, table: str) -> set[str]:
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            """,
            (table,),
        ).fetchall()
        result: set[str] = set()
        for row in rows:
            if isinstance(row, Mapping):
                result.add(str(row.get("column_name") or ""))
            else:
                result.add(str(row["column_name"]))
        return {item for item in result if item}

        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _insert_and_get_id(self, conn, query: str, params: tuple[Any, ...]) -> int:
        row = conn.execute(self._sql(query + " RETURNING id"), params).fetchone()
        if row is None:
            raise RuntimeError("Insert did not return id")
        return int(row["id"]) if isinstance(row, Mapping) else int(row[0])

        cursor = conn.execute(self._sql(query), params)
        return int(cursor.lastrowid)

    @staticmethod
    def _row_to_dict(row) -> dict[str, Any]:
        data = dict(row)
        if "id" in data:
            try:
                data["id"] = int(data["id"])
            except (TypeError, ValueError):
                pass
        if "owner_user_id" in data and data["owner_user_id"] is not None:
            try:
                data["owner_user_id"] = int(data["owner_user_id"])
            except (TypeError, ValueError):
                pass
        if "subscription_owner_user_id" in data and data["subscription_owner_user_id"] is not None:
            try:
                data["subscription_owner_user_id"] = int(data["subscription_owner_user_id"])
            except (TypeError, ValueError):
                pass
        if "is_spam" in data:
            data["is_spam"] = bool(data["is_spam"])
        if "is_toxic" in data:
            data["is_toxic"] = bool(data["is_toxic"])
        if "is_active" in data:
            data["is_active"] = bool(data["is_active"])
        if "is_enabled" in data:
            data["is_enabled"] = bool(data["is_enabled"])
        if "is_user_editable" in data:
            data["is_user_editable"] = bool(data["is_user_editable"])
        if "use_sync_start_date" in data:
            data["use_sync_start_date"] = bool(data["use_sync_start_date"])
        if "can_reviews" in data:
            data["can_reviews"] = bool(data["can_reviews"])
        if "can_questions" in data:
            data["can_questions"] = bool(data["can_questions"])
        if "can_chats" in data:
            data["can_chats"] = bool(data["can_chats"])
        if "auto_send" in data:
            data["auto_send"] = bool(data["auto_send"])
        if "is_super_admin" in data:
            data["is_super_admin"] = bool(data["is_super_admin"])
        if "is_blocked" in data:
            data["is_blocked"] = bool(data["is_blocked"])
        if "is_deleted" in data:
            data["is_deleted"] = bool(data["is_deleted"])
        if "tags_json" in data:
            data["tags"] = _json_load(data.pop("tags_json"), [])
        if "metadata_json" in data:
            data["metadata"] = _json_load(data.pop("metadata_json"), {})
        if "extra_json" in data:
            raw = data.pop("extra_json")
            data["extra"] = _json_load(raw, {})
        if "limits_override_json" in data:
            data["limits_override"] = _json_load(data.pop("limits_override_json"), {})
        if "limits_json" in data:
            data["limits"] = _json_load(data.pop("limits_json"), {})
        if "details_json" in data:
            data["details"] = _json_load(data.pop("details_json"), {})
        if "group_processors_json" in data:
            data["group_processors"] = _json_load(data.pop("group_processors_json"), {})
        return data

    def count_users(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"]) if row else 0

    def count_super_admins(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_super_admin = TRUE AND is_deleted = FALSE").fetchone()
        return int(row["c"]) if row else 0

    def create_user(
        self,
        email: str,
        password_hash: str,
        role: str,
        full_name: str | None = None,
        *,
        owner_user_id: int | None = None,
        is_super_admin: bool = False,
        plan_code: str = "starter",
        limits_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        normalized_email = email.lower()
        owner_value = owner_user_id
        if owner_value is None and role in {"admin", "user", "feedback_manager"} and not is_super_admin:
            # Backward-compatible default: existing single-user flow owns itself.
            owner_value = None
        with self._connect() as conn:
            # If a legacy soft-deleted user still has this email, free the unique key.
            deleted_row = conn.execute(
                "SELECT id FROM users WHERE email = ? AND is_deleted = TRUE ORDER BY id DESC LIMIT 1",
                (normalized_email,),
            ).fetchone()
            if deleted_row is not None:
                deleted_user_id = int(deleted_row["id"])
                conn.execute(
                    "UPDATE users SET email = ? WHERE id = ?",
                    (f"deleted-user-{deleted_user_id}@deleted.local", deleted_user_id),
                )
            user_id = self._insert_and_get_id(
                conn,
                """
                INSERT INTO users (
                    email, full_name, password_hash, role, owner_user_id, is_super_admin,
                    is_blocked, blocked_reason, blocked_at, is_deleted, deleted_at,
                    plan_code, limits_override_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, FALSE, NULL, NULL, FALSE, NULL, ?, ?, ?)
                """,
                (
                    normalized_email,
                    full_name,
                    password_hash,
                    role,
                    owner_value,
                    self._bool_db(is_super_admin),
                    plan_code,
                    self._json_param(limits_override or {}),
                    now,
                ),
            )
            if owner_value is None and not is_super_admin:
                # For owner accounts created via old flows, self-own to isolate tenant data.
                conn.execute("UPDATE users SET owner_user_id = ? WHERE id = ?", (user_id, user_id))
        if not is_super_admin:
            self.ensure_tenant_subscription(owner_user_id=int(owner_value or user_id))
        if not is_super_admin:
            self.copy_default_templates_to_user(user_id=user_id, only_if_empty=True)
            self.get_user_sync_settings(user_id=user_id)
        user = self.get_user_by_id(user_id)
        if user is None:
            raise RuntimeError("User creation failed")
        return user

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ? AND is_deleted = FALSE",
                (email.lower(),),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ? AND is_deleted = FALSE", (user_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def update_user_role(self, *, user_id: int, role: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE users
                SET role = ?
                WHERE id = ? AND is_deleted = FALSE
                """,
                (role, user_id),
            )
        return result.rowcount > 0

    def update_user_password(self, *, user_id: int, password_hash: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE users
                SET password_hash = ?
                WHERE id = ? AND is_deleted = FALSE
                """,
                (password_hash, user_id),
            )
        return result.rowcount > 0

    def update_user_profile(
        self,
        *,
        user_id: int,
        email: str,
        full_name: str | None,
        password_hash: str | None = None,
    ) -> bool:
        normalized_email = email.strip().lower()
        if password_hash is None:
            with self._connect() as conn:
                result = conn.execute(
                    """
                    UPDATE users
                    SET email = ?, full_name = ?
                    WHERE id = ? AND is_deleted = FALSE
                    """,
                    (normalized_email, full_name, user_id),
                )
            return result.rowcount > 0
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE users
                SET email = ?, full_name = ?, password_hash = ?
                WHERE id = ? AND is_deleted = FALSE
                """,
                (normalized_email, full_name, password_hash, user_id),
            )
        return result.rowcount > 0

    def create_session(self, *, token: str, user_id: int, expires_at: str) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (token, user_id, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(token) DO UPDATE SET
                    user_id = excluded.user_id,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at
                """,
                (token, user_id, _coerce_iso_for_storage(expires_at), now),
            )

    def get_session_user(self, token: str) -> dict[str, Any] | None:
        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT u.*
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = ?
                  AND s.expires_at > ?
                  AND u.is_deleted = FALSE
                  AND u.is_blocked = FALSE
                LIMIT 1
                """,
                (token, _coerce_iso_for_storage(now)),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete_session(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def cleanup_expired_sessions(self, now_iso: str) -> int:
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ?",
                (_coerce_iso_for_storage(now_iso),),
            )
        return int(result.rowcount)

    def get_ai_settings(self, *, include_secrets: bool = False) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("AI settings row is missing")
        data = self._row_to_dict(row)
        provider = str(data.get("provider") or "rules").strip().lower() or "rules"
        encrypted_key = str(data.get("yandex_api_key_encrypted") or "")
        yandex_api_key = decrypt_secret(encrypted_key) if encrypted_key else None
        raw_group_processors = data.get("group_processors")
        if not isinstance(raw_group_processors, dict):
            raw_group_processors = {}
        group_processors = dict(DEFAULT_GROUP_PROCESSORS)
        for key, value in raw_group_processors.items():
            group_id = str(key or "").strip()
            mode = str(value or "").strip().lower()
            if not group_id:
                continue
            if mode not in {"yandex", "program"}:
                continue
            group_processors[group_id] = mode
        result: dict[str, Any] = {
            "provider": provider,
            "yandex_folder_id": str(data.get("yandex_folder_id") or "") or None,
            "yandex_model_uri": str(data.get("yandex_model_uri") or "") or None,
            "group_processors": group_processors,
            "use_sync_start_date": bool(data.get("use_sync_start_date")),
            "sync_start_date": str(data.get("sync_start_date") or "") or None,
            "has_yandex_api_key": bool(yandex_api_key),
            "yandex_api_key_preview": mask_secret(yandex_api_key),
        }
        if include_secrets:
            result["yandex_api_key"] = yandex_api_key
        return result

    def update_ai_settings(
        self,
        *,
        provider: str,
        yandex_api_key: str | None,
        yandex_folder_id: str | None,
        yandex_model_uri: str | None,
        group_processors: dict[str, str] | None = None,
        use_sync_start_date: bool = False,
        sync_start_date: str | None = None,
    ) -> None:
        normalized_provider = provider.strip().lower() or "rules"
        normalized_folder = (yandex_folder_id or "").strip() or None
        normalized_model = (yandex_model_uri or "").strip() or None
        normalized_sync_date = _coerce_iso_for_storage(sync_start_date, as_date=True) if use_sync_start_date else None

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT yandex_api_key_encrypted, group_processors_json FROM ai_settings WHERE id = 1"
            ).fetchone()
            current_key_encrypted = (
                str(existing["yandex_api_key_encrypted"] or "")
                if existing is not None and "yandex_api_key_encrypted" in existing
                else ""
            )
            current_group_processors = _json_load(
                existing["group_processors_json"] if existing is not None and "group_processors_json" in existing else {},
                {},
            )

            encrypted_value: str | None
            if yandex_api_key is None:
                encrypted_value = current_key_encrypted or None
            else:
                clean_key = yandex_api_key.strip()
                encrypted_value = encrypt_secret(clean_key) if clean_key else None

            normalized_groups = dict(DEFAULT_GROUP_PROCESSORS)
            if isinstance(current_group_processors, dict):
                for key, value in current_group_processors.items():
                    group_id = str(key or "").strip()
                    mode = str(value or "").strip().lower()
                    if not group_id or mode not in {"yandex", "program"}:
                        continue
                    normalized_groups[group_id] = mode
            if isinstance(group_processors, dict):
                for key, value in group_processors.items():
                    group_id = str(key or "").strip()
                    mode = str(value or "").strip().lower()
                    if not group_id or mode not in {"yandex", "program"}:
                        continue
                    normalized_groups[group_id] = mode

            conn.execute(
                """
                UPDATE ai_settings
                SET provider = ?,
                    yandex_api_key_encrypted = ?,
                    yandex_folder_id = ?,
                    yandex_model_uri = ?,
                    group_processors_json = ?,
                    use_sync_start_date = ?,
                    sync_start_date = ?,
                    updated_at = ?
                WHERE id = 1
                """,
                (
                    normalized_provider,
                    encrypted_value,
                    normalized_folder,
                    normalized_model,
                    self._json_param(normalized_groups),
                    self._bool_db(use_sync_start_date),
                    normalized_sync_date,
                    _utc_now(),
                ),
            )

    def list_users(
        self,
        *,
        super_admin_only: bool = False,
        owner_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["u.is_deleted = FALSE"]
        if super_admin_only:
            clauses.append("u.is_super_admin = TRUE")
        if owner_only:
            # Include both tenant owners AND super-admins who have their own
            # marketplace accounts.  Super-admins are self-owned (owner_user_id
            # = id) so the owner_user_id check is enough.
            clauses.append("u.owner_user_id = u.id")
        where_sql = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    u.id,
                    u.email,
                    u.role,
                    u.owner_user_id,
                    u.is_super_admin,
                    u.is_blocked,
                    u.blocked_reason,
                    u.blocked_at,
                    u.plan_code,
                    u.limits_override_json,
                    u.created_at,
                    s.status AS subscription_status,
                    s.active_from AS subscription_active_from,
                    s.paid_until AS subscription_paid_until,
                    s.grace_until AS subscription_grace_until
                FROM users u
                LEFT JOIN tenant_subscriptions s ON s.owner_user_id = u.owner_user_id
                WHERE {where_sql}
                ORDER BY u.id ASC
                """
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_tenant_users(self, *, owner_user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    u.id,
                    u.email,
                    u.full_name,
                    u.role,
                    u.owner_user_id,
                    u.is_blocked,
                    u.blocked_reason,
                    u.blocked_at,
                    u.plan_code,
                    u.created_at,
                    u.can_supplies,
                    u.can_salary,
                    s.status AS subscription_status,
                    s.active_from AS subscription_active_from,
                    s.paid_until AS subscription_paid_until,
                    s.grace_until AS subscription_grace_until
                FROM users u
                LEFT JOIN tenant_subscriptions s ON s.owner_user_id = u.owner_user_id
                WHERE u.owner_user_id = ? AND u.is_deleted = FALSE AND u.is_super_admin = FALSE
                ORDER BY u.id ASC
                """,
                (owner_user_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_manager_permissions(self, *, manager_user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, manager_user_id, account_id, can_reviews, can_questions, can_chats, created_at, updated_at
                FROM manager_permissions
                WHERE manager_user_id = ?
                ORDER BY account_id ASC
                """,
                (manager_user_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def replace_manager_permissions(
        self,
        *,
        manager_user_id: int,
        permissions: list[dict[str, Any]],
    ) -> int:
        now = _utc_now()
        normalized_rows: list[dict[str, Any]] = []
        seen_accounts: set[int] = set()
        for raw in permissions:
            try:
                account_id = int(raw.get("account_id"))
            except (TypeError, ValueError):
                continue
            if account_id <= 0 or account_id in seen_accounts:
                continue
            seen_accounts.add(account_id)
            can_reviews = bool(raw.get("can_reviews"))
            can_questions = bool(raw.get("can_questions"))
            can_chats = bool(raw.get("can_chats"))
            if not (can_reviews or can_questions or can_chats):
                continue
            normalized_rows.append(
                {
                    "account_id": account_id,
                    "can_reviews": can_reviews,
                    "can_questions": can_questions,
                    "can_chats": can_chats,
                }
            )

        with self._connect() as conn:
            conn.execute("DELETE FROM manager_permissions WHERE manager_user_id = ?", (manager_user_id,))
            for row in normalized_rows:
                conn.execute(
                    """
                    INSERT INTO manager_permissions (
                        manager_user_id, account_id, can_reviews, can_questions, can_chats, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manager_user_id,
                        int(row["account_id"]),
                        self._bool_db(bool(row["can_reviews"])),
                        self._bool_db(bool(row["can_questions"])),
                        self._bool_db(bool(row["can_chats"])),
                        now,
                        now,
                    ),
                )
        return len(normalized_rows)

    @staticmethod
    def _add_days_iso(base_iso: str, *, days: int) -> str:
        raw = str(base_iso or "").strip()
        if not raw:
            base_dt = datetime.now(UTC)
        else:
            normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            try:
                base_dt = datetime.fromisoformat(normalized)
            except ValueError:
                base_dt = datetime.now(UTC)
        if base_dt.tzinfo is None:
            base_dt = base_dt.replace(tzinfo=UTC)
        return (base_dt.astimezone(UTC) + timedelta(days=max(int(days), 0))).isoformat()

    def get_tenant_subscription(self, *, owner_user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT owner_user_id, status, active_from, paid_until, grace_until, updated_at
                FROM tenant_subscriptions
                WHERE owner_user_id = ?
                """,
                (owner_user_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def ensure_tenant_subscription(self, *, owner_user_id: int) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tenant_subscriptions (owner_user_id, status, active_from, paid_until, grace_until, updated_at)
                VALUES (?, 'inactive', ?, NULL, NULL, ?)
                ON CONFLICT (owner_user_id) DO NOTHING
                """,
                (owner_user_id, now, now),
            )
        subscription = self.get_tenant_subscription(owner_user_id=owner_user_id)
        if subscription is None:
            raise RuntimeError("Subscription initialization failed")
        return subscription

    def extend_tenant_subscription_after_payment(
        self,
        *,
        owner_user_id: int,
        months: int = 1,
        grace_days: int = 3,
    ) -> dict[str, Any]:
        subscription = self.ensure_tenant_subscription(owner_user_id=owner_user_id)
        now_iso = _utc_now()
        now_dt = _parse_datetime_utc(now_iso) or datetime.now(UTC)
        paid_until_current = _parse_datetime_utc(subscription.get("paid_until"))
        base_dt = paid_until_current if paid_until_current and paid_until_current > now_dt else now_dt
        next_paid_until = (base_dt + timedelta(days=max(int(months), 1) * 30)).isoformat()
        grace_until = self._add_days_iso(next_paid_until, days=max(int(grace_days), 0))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tenant_subscriptions
                SET status = 'active', paid_until = ?, grace_until = ?, updated_at = ?
                WHERE owner_user_id = ?
                """,
                (next_paid_until, grace_until, now_iso, owner_user_id),
            )
        updated = self.get_tenant_subscription(owner_user_id=owner_user_id)
        if updated is None:
            raise RuntimeError("Subscription extension failed")
        return updated

    def create_tenant_user(
        self,
        *,
        owner_user_id: int,
        email: str,
        password_hash: str,
        role: str,
        full_name: str | None = None,
    ) -> dict[str, Any]:
        return self.create_user(
            email=email,
            password_hash=password_hash,
            role=role,
            full_name=full_name,
            owner_user_id=owner_user_id,
            is_super_admin=False,
        )

    def set_user_blocked(
        self,
        *,
        user_id: int,
        blocked: bool,
        reason: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE users
                SET is_blocked = ?, blocked_reason = ?, blocked_at = ?
                WHERE id = ? AND is_deleted = FALSE
                """,
                (
                    self._bool_db(blocked),
                    (reason or "").strip() or None if blocked else None,
                    _utc_now() if blocked else None,
                    user_id,
                ),
            )
        return result.rowcount > 0

    def soft_delete_user(self, *, user_id: int) -> bool:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT email FROM users WHERE id = ? AND is_deleted = FALSE",
                (user_id,),
            ).fetchone()
            if existing is None:
                return False
            # Keep unique constraint on users.email reusable for future accounts.
            deleted_email = f"deleted-user-{int(user_id)}@deleted.local"
            result = conn.execute(
                """
                UPDATE users
                SET email = ?, is_deleted = TRUE, deleted_at = ?, is_blocked = TRUE
                WHERE id = ? AND is_deleted = FALSE
                """,
                (deleted_email, _utc_now(), user_id),
            )
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return result.rowcount > 0

    def list_tariff_plans(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT code, title, monthly_price, limits_json, is_active, created_at, updated_at
                FROM tariff_plans
                ORDER BY monthly_price ASC, code ASC
                """
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def upsert_tariff_plan(
        self,
        *,
        code: str,
        title: str,
        monthly_price: float,
        limits: dict[str, Any],
        is_active: bool = True,
    ) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tariff_plans (code, title, monthly_price, limits_json, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (code) DO UPDATE SET
                    title = excluded.title,
                    monthly_price = excluded.monthly_price,
                    limits_json = excluded.limits_json,
                    is_active = excluded.is_active,
                    updated_at = excluded.updated_at
                """,
                (
                    code,
                    title,
                    monthly_price,
                    self._json_param(limits),
                    self._bool_db(is_active),
                    now,
                    now,
                ),
            )

    def delete_tariff_plan(self, *, code: str) -> tuple[bool, int]:
        normalized_code = (code or "").strip().lower()
        if not normalized_code:
            return False, 0
        with self._connect() as conn:
            in_use_row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM users
                WHERE plan_code = ?
                  AND is_deleted = FALSE
                  AND is_super_admin = FALSE
                """,
                (normalized_code,),
            ).fetchone()
            in_use_count = int(in_use_row["c"]) if in_use_row else 0
            if in_use_count > 0:
                return False, in_use_count
            result = conn.execute("DELETE FROM tariff_plans WHERE code = ?", (normalized_code,))
        return result.rowcount > 0, 0

    def set_tenant_plan(
        self,
        *,
        owner_user_id: int,
        plan_code: str,
        limits_override: dict[str, Any] | None = None,
    ) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE users
                SET plan_code = ?, limits_override_json = ?
                WHERE id = ? AND is_deleted = FALSE AND is_super_admin = FALSE
                """,
                (plan_code, self._json_param(limits_override or {}), owner_user_id),
            )
        return result.rowcount > 0

    def get_super_admin_settings(self) -> dict[str, Any]:
        ai = self.get_ai_settings()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM platform_settings WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("platform_settings row is missing")
        data = self._row_to_dict(row)
        encrypted_key = str(data.get("payment_api_key_encrypted") or "")
        payment_key = decrypt_secret(encrypted_key) if encrypted_key else None
        data["has_payment_api_key"] = bool(payment_key)
        data["payment_api_key_preview"] = mask_secret(payment_key)
        data["ai"] = ai
        data["default_sync_lookback_days"] = self._coerce_lookback_days(data.get("default_sync_lookback_days"))
        return data

    def get_default_sync_lookback_days(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT default_sync_lookback_days FROM platform_settings WHERE id = 1").fetchone()
        if row is None:
            return self._default_sync_lookback_days()
        return self._coerce_lookback_days(row["default_sync_lookback_days"])

    def set_default_sync_lookback_days(self, *, days: int) -> None:
        normalized_days = self._coerce_lookback_days(days)
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_settings (id, payment_provider, payment_api_key_encrypted, default_sync_lookback_days, updated_at)
                VALUES (1, 'manual', NULL, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    default_sync_lookback_days = excluded.default_sync_lookback_days,
                    updated_at = excluded.updated_at
                """,
                (normalized_days, now),
            )

    def save_super_admin_settings(
        self,
        *,
        payment_provider: str,
        payment_api_key: str | None,
        ai_provider: str,
        yandex_api_key: str | None,
        yandex_folder_id: str | None,
        yandex_model_uri: str | None,
        group_processors: dict[str, str] | None = None,
        use_sync_start_date: bool = False,
        sync_start_date: str | None = None,
        default_sync_lookback_days: int | None = None,
    ) -> None:
        self.update_ai_settings(
            provider=ai_provider,
            yandex_api_key=yandex_api_key,
            yandex_folder_id=yandex_folder_id,
            yandex_model_uri=yandex_model_uri,
            group_processors=group_processors,
            use_sync_start_date=use_sync_start_date,
            sync_start_date=sync_start_date,
        )
        now = _utc_now()
        lookback_days = self._coerce_lookback_days(default_sync_lookback_days)
        with self._connect() as conn:
            if payment_api_key is None:
                current = conn.execute(
                    "SELECT payment_api_key_encrypted FROM platform_settings WHERE id = 1"
                ).fetchone()
                encrypted_payment = (
                    str(current["payment_api_key_encrypted"] or "") if current else ""
                )
                encrypted_value = encrypted_payment or None
            else:
                encrypted_value = encrypt_secret(payment_api_key.strip())
            conn.execute(
                """
                UPDATE platform_settings
                SET payment_provider = ?, payment_api_key_encrypted = ?, default_sync_lookback_days = ?, updated_at = ?
                WHERE id = 1
                """,
                (payment_provider, encrypted_value, lookback_days, now),
            )

    def get_user_sync_settings(self, *, user_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT use_sync_start_date, sync_start_date, created_at
                FROM users
                WHERE id = ? AND is_deleted = ?
                """,
                (user_id, self._bool_db(False)),
            ).fetchone()
            if row is None:
                raise RuntimeError("User not found")
            lookback_row = conn.execute(
                "SELECT default_sync_lookback_days FROM platform_settings WHERE id = 1"
            ).fetchone()
        lookback_days = self._coerce_lookback_days(lookback_row["default_sync_lookback_days"] if lookback_row else None)
        use_sync_start_date = bool(row["use_sync_start_date"])
        sync_start_date = _coerce_iso_for_storage(str(row["sync_start_date"] or ""), as_date=True)
        if not sync_start_date:
            created_at = _coerce_iso_for_storage(str(row["created_at"] or ""))
            base = datetime.now(UTC)
            if created_at:
                try:
                    base = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except ValueError:
                    base = datetime.now(UTC)
            sync_start_date = (base - timedelta(days=lookback_days)).date().isoformat()
            use_sync_start_date = True
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE users
                    SET use_sync_start_date = ?, sync_start_date = ?
                    WHERE id = ? AND is_deleted = ?
                    """,
                    (self._bool_db(True), sync_start_date, user_id, self._bool_db(False)),
                )
        return {
            "use_sync_start_date": use_sync_start_date,
            "sync_start_date": sync_start_date,
            "default_sync_lookback_days": lookback_days,
        }

    def save_user_sync_settings(
        self,
        *,
        user_id: int,
        use_sync_start_date: bool,
        sync_start_date: str | None,
    ) -> bool:
        normalized_date = _coerce_iso_for_storage(sync_start_date, as_date=True) if use_sync_start_date else None
        if use_sync_start_date and not normalized_date:
            settings = self.get_user_sync_settings(user_id=user_id)
            normalized_date = str(settings.get("sync_start_date") or "")
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE users
                SET use_sync_start_date = ?, sync_start_date = ?
                WHERE id = ? AND is_deleted = FALSE
                """,
                (self._bool_db(use_sync_start_date), normalized_date, user_id),
            )
        return result.rowcount > 0

    # Auto-sync / auto-collect intervals are stored in the "*_interval_hours"
    # columns as minutes (legacy rows may still hold hours 1/2/3/6/12/24).
    _WB_FBS_AUTO_SYNC_INTERVAL_MINUTES = (10, 30, 60, 120, 180, 360, 720, 1440)
    _WB_FBS_AUTO_SYNC_LEGACY_HOURS = (1, 2, 3, 6, 12, 24)
    _WB_FBS_AUTO_COLLECT_INTERVAL_MINUTES = _WB_FBS_AUTO_SYNC_INTERVAL_MINUTES

    @classmethod
    def _normalize_wb_fbs_sync_interval_minutes(cls, value: object) -> int:
        try:
            raw = int(value or 60)
        except (TypeError, ValueError):
            return 60
        if raw in cls._WB_FBS_AUTO_SYNC_INTERVAL_MINUTES:
            return raw
        if raw in cls._WB_FBS_AUTO_SYNC_LEGACY_HOURS:
            return raw * 60
        return 60

    @staticmethod
    def _normalize_hhmm(value: object, *, default: str) -> str:
        """Normalize to HH:MM. Accepts HH:MM or HH:MM:SS (browser <input type=time>)."""
        raw = str(value or "").strip()
        if not raw:
            return default
        parts = raw.split(":")
        if len(parts) < 2:
            return default
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except (TypeError, ValueError):
            return default
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return default
        return f"{hour:02d}:{minute:02d}"

    @classmethod
    def _parse_hhmm_strict(cls, value: object, *, field: str, default: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return default
        # Browsers may send seconds (12:00:00) from <input type="time">.
        if not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", raw):
            raise ValueError(f"Некорректное время {field} (ЧЧ:ММ)")
        normalized = cls._normalize_hhmm(raw, default="")
        if not normalized:
            raise ValueError(f"Некорректное время {field} (ЧЧ:ММ)")
        return normalized

    @staticmethod
    def _iso_or_none(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            raw = value.strip()
            return raw or None
        try:
            return str(value.isoformat())
        except Exception:
            raw = str(value).strip()
            return raw or None

    _WB_FBS_SYNC_LOOKBACK_MIN = 1
    _WB_FBS_SYNC_LOOKBACK_MAX = 30
    _WB_FBS_SYNC_LOOKBACK_DEFAULT = 3

    def _normalize_wb_fbs_sync_lookback_days(self, value: object | None) -> int:
        try:
            days = int(value) if value is not None else self._WB_FBS_SYNC_LOOKBACK_DEFAULT
        except (TypeError, ValueError):
            days = self._WB_FBS_SYNC_LOOKBACK_DEFAULT
        return max(self._WB_FBS_SYNC_LOOKBACK_MIN, min(self._WB_FBS_SYNC_LOOKBACK_MAX, days))

    def get_wb_fbs_auto_sync_settings(self, *, user_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT wb_fbs_auto_sync_enabled, wb_fbs_auto_sync_interval_hours,
                       wb_fbs_auto_sync_active_from,
                       wb_fbs_auto_sync_active_to,
                       wb_fbs_sync_lookback_days,
                       wb_fbs_last_synced_at,
                       wb_fbs_last_auto_synced_at,
                       wb_fbs_auto_collect_mgt_enabled,
                       wb_fbs_auto_collect_mgt_interval_hours,
                       wb_fbs_auto_collect_mgt_active_from,
                       wb_fbs_auto_collect_mgt_active_to,
                       wb_fbs_last_collect_mgt_at,
                       wb_fbs_auto_collect_mgt_last_run_at,
                       wb_fbs_auto_collect_mgt_last_status,
                       wb_fbs_auto_collect_mgt_last_detail
                FROM users
                WHERE id = ? AND is_deleted = ?
                """,
                (user_id, self._bool_db(False)),
            ).fetchone()
        if row is None:
            raise RuntimeError("User not found")
        interval_minutes = self._normalize_wb_fbs_sync_interval_minutes(
            row["wb_fbs_auto_sync_interval_hours"]
        )
        collect_interval_minutes = self._normalize_wb_fbs_sync_interval_minutes(
            row["wb_fbs_auto_collect_mgt_interval_hours"]
        )
        lookback_days = self._normalize_wb_fbs_sync_lookback_days(
            row["wb_fbs_sync_lookback_days"]
        )
        last_synced = self._iso_or_none(row["wb_fbs_last_synced_at"])
        last_auto_synced = self._iso_or_none(row["wb_fbs_last_auto_synced_at"])
        last_collect_any = self._iso_or_none(row["wb_fbs_last_collect_mgt_at"])
        last_collect_auto = self._iso_or_none(row["wb_fbs_auto_collect_mgt_last_run_at"])
        detail_raw = str(row["wb_fbs_auto_collect_mgt_last_detail"] or "").strip()
        detail: dict[str, Any] | None = None
        if detail_raw:
            try:
                parsed = json.loads(detail_raw)
                if isinstance(parsed, dict):
                    detail = parsed
            except Exception:
                detail = None
        # Legacy fields: whole hours only. For 10/30 min return null — do not lie with 1.
        interval_hours = (
            interval_minutes // 60
            if interval_minutes % 60 == 0
            else None
        )
        collect_interval_hours = (
            collect_interval_minutes // 60
            if collect_interval_minutes % 60 == 0
            else None
        )
        return {
            "enabled": bool(row["wb_fbs_auto_sync_enabled"]),
            "interval_minutes": interval_minutes,
            "interval_hours": interval_hours,
            "allowed_intervals_minutes": list(self._WB_FBS_AUTO_SYNC_INTERVAL_MINUTES),
            "allowed_intervals": [
                m // 60 for m in self._WB_FBS_AUTO_SYNC_INTERVAL_MINUTES if m % 60 == 0
            ],
            "last_synced_at": last_synced,
            "last_auto_synced_at": last_auto_synced,
            "lookback_days": lookback_days,
            "lookback_days_min": self._WB_FBS_SYNC_LOOKBACK_MIN,
            "lookback_days_max": self._WB_FBS_SYNC_LOOKBACK_MAX,
            "active_from": self._normalize_hhmm(
                row["wb_fbs_auto_sync_active_from"], default="12:00"
            ),
            "active_to": self._normalize_hhmm(
                row["wb_fbs_auto_sync_active_to"], default="06:00"
            ),
            "collect_mgt_enabled": bool(row["wb_fbs_auto_collect_mgt_enabled"]),
            "collect_mgt_interval_minutes": collect_interval_minutes,
            "collect_mgt_interval_hours": collect_interval_hours,
            "allowed_collect_intervals_minutes": list(
                self._WB_FBS_AUTO_COLLECT_INTERVAL_MINUTES
            ),
            "collect_mgt_active_from": self._normalize_hhmm(
                row["wb_fbs_auto_collect_mgt_active_from"], default="12:00"
            ),
            "collect_mgt_active_to": self._normalize_hhmm(
                row["wb_fbs_auto_collect_mgt_active_to"], default="06:00"
            ),
            "last_collect_mgt_at": last_collect_any or last_collect_auto,
            "collect_mgt_last_run_at": last_collect_auto,
            "collect_mgt_last_status": str(
                row["wb_fbs_auto_collect_mgt_last_status"] or ""
            ).strip(),
            "collect_mgt_last_detail": detail,
        }

    def save_wb_fbs_auto_sync_settings(
        self,
        *,
        user_id: int,
        enabled: bool,
        interval_minutes: int | None = None,
        interval_hours: int | None = None,
        lookback_days: int | None = None,
        active_from: str = "12:00",
        active_to: str = "06:00",
        collect_mgt_enabled: bool = False,
        collect_mgt_interval_minutes: int | None = None,
        collect_mgt_interval_hours: int | None = None,
        collect_mgt_active_from: str = "12:00",
        collect_mgt_active_to: str = "06:00",
    ) -> bool:
        if interval_minutes is not None:
            try:
                interval = int(interval_minutes)
            except (TypeError, ValueError) as exc:
                raise ValueError("Недопустимый период") from exc
        elif interval_hours is not None:
            # Legacy callers still send hours.
            interval = self._normalize_wb_fbs_sync_interval_minutes(interval_hours)
        else:
            interval = 60
        if collect_mgt_interval_minutes is not None:
            try:
                collect_interval = int(collect_mgt_interval_minutes)
            except (TypeError, ValueError) as exc:
                raise ValueError("Недопустимый период") from exc
        elif collect_mgt_interval_hours is not None:
            collect_interval = self._normalize_wb_fbs_sync_interval_minutes(
                collect_mgt_interval_hours
            )
        else:
            collect_interval = 60
        if interval not in self._WB_FBS_AUTO_SYNC_INTERVAL_MINUTES:
            raise ValueError(
                "Период синхронизации должен быть одним из: "
                + ", ".join(str(v) for v in self._WB_FBS_AUTO_SYNC_INTERVAL_MINUTES)
                + " минут"
            )
        if collect_interval not in self._WB_FBS_AUTO_COLLECT_INTERVAL_MINUTES:
            raise ValueError(
                "Период автосбора МГТ должен быть одним из: "
                + ", ".join(str(v) for v in self._WB_FBS_AUTO_COLLECT_INTERVAL_MINUTES)
                + " минут"
            )
        try:
            lookback_raw = (
                int(lookback_days)
                if lookback_days is not None
                else self._WB_FBS_SYNC_LOOKBACK_DEFAULT
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Недопустимое число дней загрузки заказов") from exc
        if (
            lookback_raw < self._WB_FBS_SYNC_LOOKBACK_MIN
            or lookback_raw > self._WB_FBS_SYNC_LOOKBACK_MAX
        ):
            raise ValueError(
                "Глубина загрузки заказов должна быть от "
                f"{self._WB_FBS_SYNC_LOOKBACK_MIN} до {self._WB_FBS_SYNC_LOOKBACK_MAX} дней"
            )
        lookback = lookback_raw
        sync_active_from = self._parse_hhmm_strict(
            active_from, field="начала окна автосинхронизации", default="12:00"
        )
        sync_active_to = self._parse_hhmm_strict(
            active_to, field="конца окна автосинхронизации", default="06:00"
        )
        collect_active_from = self._parse_hhmm_strict(
            collect_mgt_active_from, field="начала окна автосбора МГТ", default="12:00"
        )
        collect_active_to = self._parse_hhmm_strict(
            collect_mgt_active_to, field="конца окна автосбора МГТ", default="06:00"
        )
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE users
                SET wb_fbs_auto_sync_enabled = ?,
                    wb_fbs_auto_sync_interval_hours = ?,
                    wb_fbs_auto_sync_active_from = ?,
                    wb_fbs_auto_sync_active_to = ?,
                    wb_fbs_sync_lookback_days = ?,
                    wb_fbs_auto_collect_mgt_enabled = ?,
                    wb_fbs_auto_collect_mgt_interval_hours = ?,
                    wb_fbs_auto_collect_mgt_active_from = ?,
                    wb_fbs_auto_collect_mgt_active_to = ?
                WHERE id = ? AND is_deleted = FALSE
                """,
                (
                    self._bool_db(bool(enabled)),
                    interval,
                    sync_active_from,
                    sync_active_to,
                    lookback,
                    self._bool_db(bool(collect_mgt_enabled)),
                    collect_interval,
                    collect_active_from,
                    collect_active_to,
                    user_id,
                ),
            )
        return result.rowcount > 0

    def mark_wb_fbs_synced(self, *, user_id: int, is_auto: bool = False) -> None:
        """Record last successful WB FBS orders sync (tenant-level, not FBW supplies).

        Always updates last sync. When ``is_auto`` also updates last auto-sync.
        """
        now = _utc_now()
        with self._connect() as conn:
            if is_auto:
                conn.execute(
                    """
                    UPDATE users
                    SET wb_fbs_last_synced_at = ?,
                        wb_fbs_last_auto_synced_at = ?
                    WHERE id = ? AND is_deleted = FALSE
                    """,
                    (now, now, user_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET wb_fbs_last_synced_at = ?
                    WHERE id = ? AND is_deleted = FALSE
                    """,
                    (now, user_id),
                )

    def mark_wb_fbs_collect_mgt_at(self, *, user_id: int) -> None:
        """Record last MGT collect event (manual or automatic)."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET wb_fbs_last_collect_mgt_at = ?
                WHERE id = ? AND is_deleted = FALSE
                """,
                (_utc_now(), user_id),
            )

    def mark_wb_fbs_auto_collect_mgt_run(
        self,
        *,
        user_id: int,
        status: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Record last auto-collect MGT attempt (success, skip, or partial)."""
        detail_json = ""
        if isinstance(detail, dict) and detail:
            try:
                detail_json = json.dumps(detail, ensure_ascii=False, default=str)
            except Exception:
                detail_json = ""
            if len(detail_json) > 20000:
                detail_json = detail_json[:19997] + "…"
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET wb_fbs_auto_collect_mgt_last_run_at = ?,
                    wb_fbs_auto_collect_mgt_last_status = ?,
                    wb_fbs_auto_collect_mgt_last_detail = ?,
                    wb_fbs_last_collect_mgt_at = ?
                WHERE id = ? AND is_deleted = FALSE
                """,
                (now, str(status or "")[:500], detail_json, now, user_id),
            )

    def save_payment_record(
        self,
        *,
        owner_user_id: int,
        amount: float,
        currency: str = "RUB",
        status: str = "pending",
        external_payment_id: str | None = None,
        details: dict[str, Any] | None = None,
        paid_at: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            payment_id = self._insert_and_get_id(
                conn,
                """
                INSERT INTO payment_records (
                    owner_user_id, amount, currency, status, external_payment_id, details_json, paid_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_user_id,
                    float(amount),
                    currency,
                    status,
                    external_payment_id,
                    self._json_param(details or {}),
                    paid_at,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM payment_records WHERE id = ?", (payment_id,)).fetchone()
        if row is None:
            raise RuntimeError("Payment record creation failed")
        return self._row_to_dict(row)

    def save_payment_record_with_subscription_update(
        self,
        *,
        owner_user_id: int,
        amount: float,
        currency: str = "RUB",
        status: str = "pending",
        external_payment_id: str | None = None,
        details: dict[str, Any] | None = None,
        paid_at: str | None = None,
        months: int = 1,
        grace_days: int = 3,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        normalized_status = str(status or "").strip().lower() or "pending"
        payment = self.save_payment_record(
            owner_user_id=owner_user_id,
            amount=amount,
            currency=currency,
            status=normalized_status,
            external_payment_id=external_payment_id,
            details=details,
            paid_at=paid_at,
        )
        subscription = self.ensure_tenant_subscription(owner_user_id=owner_user_id)
        if self._is_effective_paid_status(normalized_status):
            subscription = self.extend_tenant_subscription_after_payment(
                owner_user_id=owner_user_id,
                months=months,
                grace_days=grace_days,
            )
        return payment, subscription

    def list_billing_records(self, *, owner_user_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(owner_user_id)
        query = "SELECT * FROM payment_records"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def delete_payment_record(self, *, payment_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute("DELETE FROM payment_records WHERE id = ?", (payment_id,))
        return result.rowcount > 0

    def list_tenants_overview(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    u.id,
                    u.email,
                    u.full_name,
                    u.plan_code,
                    u.is_blocked,
                    u.created_at,
                    COALESCE(stats.reviews_count, 0) AS reviews_count,
                    COALESCE(stats.members_count, 0) AS members_count
                FROM users u
                LEFT JOIN (
                    SELECT
                        owner.id AS owner_id,
                        COUNT(DISTINCT ri.review_uid) AS reviews_count,
                        COUNT(DISTINCT member.id) AS members_count
                    FROM users owner
                    LEFT JOIN users member ON member.owner_user_id = owner.id AND member.is_deleted = FALSE
                    LEFT JOIN review_items ri ON ri.user_id = member.id
                    WHERE owner.owner_user_id = owner.id
                      AND owner.is_deleted = FALSE
                      AND owner.is_super_admin = FALSE
                    GROUP BY owner.id
                ) stats ON stats.owner_id = u.id
                WHERE u.owner_user_id = u.id
                  AND u.is_deleted = FALSE
                  AND u.is_super_admin = FALSE
                ORDER BY u.created_at DESC
                """
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]


    def create_marketplace_account(
        self,
        *,
        user_id: int,
        marketplace: str,
        account_name: str,
        api_url: str,
        api_key: str | None,
        extra: dict[str, Any] | None = None,
        is_active: bool = True,
    ) -> dict[str, Any]:
        now = _utc_now()
        encrypted_api_key = encrypt_secret(api_key)
        with self._connect() as conn:
            account_id = self._insert_and_get_id(
                conn,
                """
                INSERT INTO marketplace_accounts (
                    user_id, marketplace, account_name, api_url, api_key_encrypted, extra_json, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    marketplace,
                    account_name,
                    api_url,
                    encrypted_api_key,
                    self._json_param(extra or {}),
                    self._bool_db(is_active),
                    now,
                    now,
                ),
            )
        account = self.get_marketplace_account(user_id=user_id, account_id=account_id, include_secrets=False)
        if account is None:
            raise RuntimeError("Marketplace account creation failed")
        return account

    def list_marketplace_accounts(self, user_id: int, *, include_secrets: bool = False) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM marketplace_accounts
                WHERE user_id = ?
                ORDER BY id DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._account_row_to_dict(row, include_secrets=include_secrets) for row in rows]

    def get_marketplace_account(
        self,
        *,
        user_id: int,
        account_id: int,
        include_secrets: bool = False,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM marketplace_accounts
                WHERE user_id = ? AND id = ?
                """,
                (user_id, account_id),
            ).fetchone()
        if row is None:
            return None
        return self._account_row_to_dict(row, include_secrets=include_secrets)

    def _account_row_to_dict(self, row, *, include_secrets: bool) -> dict[str, Any]:
        data = self._row_to_dict(row)
        encrypted = str(data.pop("api_key_encrypted") or "") if "api_key_encrypted" in data else ""
        api_key = decrypt_secret(encrypted) if encrypted else None
        data["has_api_key"] = bool(api_key)
        data["api_key_preview"] = mask_secret(api_key)
        if include_secrets:
            data["api_key"] = api_key
        return data

    def update_marketplace_account_extra_field(
        self,
        *,
        user_id: int,
        account_id: int,
        key: str,
        value: Any,
    ) -> bool:
        """Update a single key inside the extra_json field of a marketplace account.

        Used to persist lightweight per-account sync state (e.g. last events
        cursor) without a full account update.
        """
        account = self.get_marketplace_account(
            user_id=user_id, account_id=account_id, include_secrets=False
        )
        if account is None:
            return False
        extra = dict(account.get("extra") or {})
        extra[str(key)] = value
        with self._connect() as conn:
            result = conn.execute(
                self._sql("""
                UPDATE marketplace_accounts
                SET extra_json = ?, updated_at = ?
                WHERE user_id = ? AND id = ?
                """),
                (self._json_param(extra), _utc_now(), user_id, account_id),
            )
        return result.rowcount > 0

    def update_marketplace_account_status(self, *, user_id: int, account_id: int, is_active: bool) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE marketplace_accounts
                SET is_active = ?, updated_at = ?
                WHERE user_id = ? AND id = ?
                """,
                (self._bool_db(is_active), _utc_now(), user_id, account_id),
            )
        return result.rowcount > 0

    def delete_marketplace_account(self, *, user_id: int, account_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                DELETE FROM marketplace_accounts
                WHERE user_id = ? AND id = ?
                """,
                (user_id, account_id),
            )
        return result.rowcount > 0

    def upsert_template(
        self,
        *,
        user_id: int,
        category: str,
        mode: str,
        template_text: str,
        is_enabled: bool | None = None,
    ) -> None:
        if is_enabled is None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO response_templates (user_id, category, mode, template_text, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, category) DO UPDATE SET
                        mode = excluded.mode,
                        template_text = excluded.template_text,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, category, mode, template_text, _utc_now()),
                )
            return

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO response_templates (user_id, category, mode, is_enabled, template_text, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, category) DO UPDATE SET
                    mode = excluded.mode,
                    is_enabled = excluded.is_enabled,
                    template_text = excluded.template_text,
                    updated_at = excluded.updated_at
                """,
                (user_id, category, mode, self._bool_db(bool(is_enabled)), template_text, _utc_now()),
            )

    def list_templates(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM response_templates
                WHERE user_id = ?
                ORDER BY category ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_template(self, *, user_id: int, category: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM response_templates
                WHERE user_id = ? AND category = ?
                """,
                (user_id, category),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete_template(self, *, user_id: int, category: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                DELETE FROM response_templates
                WHERE user_id = ? AND category = ?
                """,
                (user_id, category),
            )
        return result.rowcount > 0

    def count_default_template_variants(self, *, include_inactive: bool = False) -> int:
        query = "SELECT COUNT(*) AS c FROM default_template_variants"
        if not include_inactive:
            query += f" WHERE is_active = {self._bool_true_literal()}"
        with self._connect() as conn:
            row = conn.execute(query).fetchone()
        return int(row["c"]) if row else 0

    def count_default_template_subgroups(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM default_template_subgroups").fetchone()
        return int(row["c"]) if row else 0

    def list_default_template_subgroups(self, *, group_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id)
        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)
        query = f"""
            SELECT *
            FROM default_template_subgroups
            {where}
            ORDER BY group_id ASC, created_at ASC, subgroup ASC
        """
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_default_template_subgroup(self, *, group_id: str, subgroup: str) -> dict[str, Any] | None:
        clean_group = str(group_id or "").strip()
        clean_subgroup = str(subgroup or "").strip()
        if not clean_group or not clean_subgroup:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM default_template_subgroups
                WHERE group_id = ? AND subgroup = ?
                """,
                (clean_group, clean_subgroup),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def rename_default_template_subgroup(self, *, group_id: str, subgroup: str, new_subgroup: str) -> bool:
        clean_group = str(group_id or "").strip()
        clean_subgroup = str(subgroup or "").strip()
        clean_new_subgroup = str(new_subgroup or "").strip()
        if not clean_group or not clean_subgroup or not clean_new_subgroup:
            return False
        if clean_subgroup == clean_new_subgroup:
            return True
        now = _utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM default_template_subgroups
                WHERE group_id = ? AND subgroup = ?
                """,
                (clean_group, clean_new_subgroup),
            ).fetchone()
            if existing is not None:
                return False
            subgroup_row = conn.execute(
                """
                SELECT subgroup_id
                FROM default_template_subgroups
                WHERE group_id = ? AND subgroup = ?
                """,
                (clean_group, clean_subgroup),
            ).fetchone()
            if subgroup_row is None:
                return False
            subgroup_id = str(subgroup_row.get("subgroup_id") or "").strip() if isinstance(subgroup_row, Mapping) else ""
            if not subgroup_id:
                subgroup_id = _build_subgroup_id(clean_group, clean_subgroup)
            updated_subgroups = conn.execute(
                """
                UPDATE default_template_subgroups
                SET subgroup = ?, subgroup_id = ?, updated_at = ?
                WHERE group_id = ? AND subgroup = ?
                """,
                (clean_new_subgroup, subgroup_id, now, clean_group, clean_subgroup),
            )
            if int(updated_subgroups.rowcount or 0) <= 0:
                return False
            conn.execute(
                """
                UPDATE default_template_variants
                SET subgroup = ?, updated_at = ?
                WHERE group_id = ? AND subgroup = ?
                """,
                (clean_new_subgroup, now, clean_group, clean_subgroup),
            )
            conn.execute(
                """
                UPDATE response_template_variants
                SET subgroup = ?, updated_at = ?
                WHERE group_id = ? AND subgroup = ?
                """,
                (clean_new_subgroup, now, clean_group, clean_subgroup),
            )
        return True

    def ensure_default_template_subgroups(self, rows: list[dict[str, str]]) -> int:
        now = _utc_now()
        touched = 0
        with self._connect() as conn:
            for item in rows:
                group_id = str(item.get("group_id") or "").strip()
                subgroup = str(item.get("subgroup") or "").strip()
                if not group_id or not subgroup:
                    continue
                subgroup_id = _build_subgroup_id(group_id, subgroup)
                result = conn.execute(
                    """
                    INSERT INTO default_template_subgroups (group_id, subgroup_id, subgroup, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, subgroup) DO UPDATE SET
                        subgroup_id = COALESCE(default_template_subgroups.subgroup_id, excluded.subgroup_id),
                        updated_at = excluded.updated_at
                    """,
                    (group_id, subgroup_id, subgroup, now, now),
                )
                touched += int(result.rowcount or 0)
        return touched

    def sync_default_template_subgroups_from_variants(self) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT group_id, subgroup
                FROM default_template_variants
                WHERE TRIM(group_id) <> '' AND TRIM(subgroup) <> ''
                """
            ).fetchall()
        payload = [
            {
                "group_id": str(row["group_id"] or "").strip(),
                "subgroup": str(row["subgroup"] or "").strip(),
            }
            for row in rows
        ]
        return self.ensure_default_template_subgroups(payload)

    def add_default_template_subgroup(self, *, group_id: str, subgroup: str) -> dict[str, Any]:
        group_id = group_id.strip()
        subgroup = subgroup.strip()
        if not group_id or not subgroup:
            raise ValueError("group_id and subgroup are required")
        now = _utc_now()
        subgroup_id = _build_subgroup_id(group_id, subgroup)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO default_template_subgroups (group_id, subgroup_id, subgroup, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, subgroup) DO UPDATE SET
                    subgroup_id = COALESCE(default_template_subgroups.subgroup_id, excluded.subgroup_id),
                    updated_at = excluded.updated_at
                """,
                (group_id, subgroup_id, subgroup, now, now),
            )
            row = conn.execute(
                """
                SELECT *
                FROM default_template_subgroups
                WHERE group_id = ? AND subgroup = ?
                """,
                (group_id, subgroup),
            ).fetchone()
        if row is None:
            raise RuntimeError("Default template subgroup creation failed")
        return self._row_to_dict(row)

    def delete_default_template_subgroup(self, *, group_id: str, subgroup: str) -> bool:
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM default_template_variants
                WHERE group_id = ? AND subgroup = ?
                """,
                (group_id, subgroup),
            )
            result = conn.execute(
                """
                DELETE FROM default_template_subgroups
                WHERE group_id = ? AND subgroup = ?
                """,
                (group_id, subgroup),
            )
        return result.rowcount > 0

    def seed_default_templates_from_user_templates(self) -> int:
        with self._connect() as conn:
            source = conn.execute(
                """
                SELECT user_id, COUNT(*) AS c
                FROM response_template_variants
                GROUP BY user_id
                ORDER BY c DESC, user_id ASC
                LIMIT 1
                """
            ).fetchone()
            if source is None:
                return 0
            rows = conn.execute(
                """
                SELECT group_id, subgroup, template_text
                FROM response_template_variants
                WHERE user_id = ? AND is_active = ?
                ORDER BY group_id ASC, subgroup ASC, id ASC
                """,
                (int(source["user_id"]), self._bool_db(True)),
            ).fetchall()
            inserted = 0
            now = _utc_now()
            for row in rows:
                result = conn.execute(
                    """
                    INSERT INTO default_template_variants (
                        group_id, subgroup, template_text, is_active, created_at, updated_at
                    )
                    SELECT ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM default_template_variants
                        WHERE group_id = ? AND subgroup = ? AND template_text = ?
                    )
                    """,
                    (
                        str(row["group_id"] or ""),
                        str(row["subgroup"] or ""),
                        str(row["template_text"] or ""),
                        self._bool_db(True),
                        now,
                        now,
                        str(row["group_id"] or ""),
                        str(row["subgroup"] or ""),
                        str(row["template_text"] or ""),
                    ),
                )
                inserted += int(result.rowcount or 0)
        return inserted

    def seed_default_template_variants(self, rows: list[dict[str, str]]) -> int:
        now = _utc_now()
        inserted = 0
        with self._connect() as conn:
            for item in rows:
                group_id = str(item.get("group_id") or "").strip()
                subgroup = str(item.get("subgroup") or "").strip()
                text = str(item.get("template_text") or "").strip()
                if not group_id or not subgroup or not text:
                    continue
                result = conn.execute(
                    """
                    INSERT INTO default_template_variants (
                        group_id, subgroup, template_text, is_active, created_at, updated_at
                    )
                    SELECT ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM default_template_variants
                        WHERE group_id = ? AND subgroup = ? AND template_text = ?
                    )
                    """,
                    (
                        group_id,
                        subgroup,
                        text,
                        self._bool_db(True),
                        now,
                        now,
                        group_id,
                        subgroup,
                        text,
                    ),
                )
                inserted += int(result.rowcount or 0)
        return inserted

    def list_default_template_variants(
        self,
        *,
        group_id: str | None = None,
        subgroup: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id)
        if subgroup:
            clauses.append("subgroup = ?")
            params.append(subgroup)
        if not include_inactive:
            clauses.append(f"is_active = {self._bool_true_literal()}")
        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)
        query = f"""
            SELECT *
            FROM default_template_variants
            {where}
            ORDER BY group_id ASC, subgroup ASC, id ASC
        """
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def replace_default_subgroup_templates(self, *, group_id: str, subgroup: str, templates: list[str]) -> None:
        clean = [item.strip() for item in templates if item and item.strip()]
        now = _utc_now()
        self.ensure_default_template_subgroups([{"group_id": group_id, "subgroup": subgroup}])
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM default_template_variants
                WHERE group_id = ? AND subgroup = ?
                """,
                (group_id, subgroup),
            )
            for text in clean:
                conn.execute(
                    """
                    INSERT INTO default_template_variants (
                        group_id, subgroup, template_text, is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (group_id, subgroup, text, self._bool_db(True), now, now),
                )

    def add_default_template_variant(self, *, group_id: str, subgroup: str, template_text: str) -> dict[str, Any]:
        now = _utc_now()
        self.ensure_default_template_subgroups([{"group_id": group_id, "subgroup": subgroup}])
        with self._connect() as conn:
            row_id = self._insert_and_get_id(
                conn,
                """
                INSERT INTO default_template_variants (
                    group_id, subgroup, template_text, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (group_id, subgroup, template_text.strip(), self._bool_db(True), now, now),
            )
            row = conn.execute(
                "SELECT * FROM default_template_variants WHERE id = ?",
                (row_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Default template variant creation failed")
        return self._row_to_dict(row)

    def add_default_template_variants_bulk(self, *, group_id: str, subgroup: str, templates: list[str]) -> int:
        clean_unique: list[str] = []
        seen: set[str] = set()
        for item in templates:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            clean_unique.append(text)
        if not clean_unique:
            return 0
        now = _utc_now()
        inserted = 0
        self.ensure_default_template_subgroups([{"group_id": group_id, "subgroup": subgroup}])
        with self._connect() as conn:
            for text in clean_unique:
                result = conn.execute(
                    """
                    INSERT INTO default_template_variants (
                        group_id, subgroup, template_text, is_active, created_at, updated_at
                    )
                    SELECT ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM default_template_variants
                        WHERE group_id = ? AND subgroup = ? AND template_text = ?
                    )
                    """,
                    (
                        group_id,
                        subgroup,
                        text,
                        self._bool_db(True),
                        now,
                        now,
                        group_id,
                        subgroup,
                        text,
                    ),
                )
                inserted += int(result.rowcount or 0)
        return inserted

    def delete_default_template_variant(self, *, template_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                DELETE FROM default_template_variants
                WHERE id = ?
                """,
                (template_id,),
            )
        return result.rowcount > 0

    def reset_templates_to_defaults(self, *, user_id: int) -> int:
        """Delete all user templates and replace with current admin defaults.

        Used when admin clicks 'Reset to defaults' in Settings → Templates.
        All user-created variants are removed and replaced with the current
        default_template_variants from the admin panel.
        """
        with self._connect() as conn:
            # Delete all existing user templates
            conn.execute(
                "DELETE FROM response_template_variants WHERE user_id = ?",
                (user_id,),
            )
            # Copy fresh from defaults
            defaults = conn.execute(
                f"""
                SELECT group_id, subgroup, template_text
                FROM default_template_variants
                WHERE is_active = {self._bool_true_literal()}
                ORDER BY group_id ASC, subgroup ASC, id ASC
                """
            ).fetchall()
            if not defaults:
                return 0
            now = _utc_now()
            inserted = 0
            for row in defaults:
                conn.execute(
                    self._sql("""
                    INSERT INTO response_template_variants (
                        user_id, group_id, subgroup, template_text, is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """),
                    (
                        user_id,
                        str(row["group_id"] or ""),
                        str(row["subgroup"] or ""),
                        str(row["template_text"] or ""),
                        self._bool_db(True),
                        now,
                        now,
                    ),
                )
                inserted += 1
        return inserted

    def copy_default_templates_to_user(self, *, user_id: int, only_if_empty: bool = True) -> int:
        with self._connect() as conn:
            if only_if_empty:
                existing = conn.execute(
                    "SELECT COUNT(*) AS c FROM response_template_variants WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if existing and int(existing["c"]) > 0:
                    return 0

            defaults = conn.execute(
                f"""
                SELECT group_id, subgroup, template_text
                FROM default_template_variants
                WHERE is_active = {self._bool_true_literal()}
                ORDER BY group_id ASC, subgroup ASC, id ASC
                """
            ).fetchall()
            if not defaults:
                return 0

            now = _utc_now()
            inserted = 0
            for row in defaults:
                result = conn.execute(
                    """
                    INSERT INTO response_template_variants (
                        user_id, group_id, subgroup, template_text, is_active, created_at, updated_at
                    )
                    SELECT ?, ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM response_template_variants
                        WHERE user_id = ? AND group_id = ? AND subgroup = ? AND template_text = ?
                    )
                    """,
                    (
                        user_id,
                        str(row["group_id"] or ""),
                        str(row["subgroup"] or ""),
                        str(row["template_text"] or ""),
                        self._bool_db(True),
                        now,
                        now,
                        user_id,
                        str(row["group_id"] or ""),
                        str(row["subgroup"] or ""),
                        str(row["template_text"] or ""),
                    ),
                )
                inserted += int(result.rowcount or 0)
        return inserted

    def list_template_variants(
        self,
        *,
        user_id: int,
        group_id: str | None = None,
        subgroup: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id)
        if subgroup:
            clauses.append("subgroup = ?")
            params.append(subgroup)
        if not include_inactive:
            clauses.append(f"is_active = {self._bool_true_literal()}")
        query = f"""
            SELECT *
            FROM response_template_variants
            WHERE {' AND '.join(clauses)}
            ORDER BY subgroup ASC, id ASC
        """
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def replace_subgroup_templates(
        self,
        *,
        user_id: int,
        group_id: str,
        subgroup: str,
        templates: list[str],
    ) -> None:
        clean = [item.strip() for item in templates if item and item.strip()]
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM response_template_variants
                WHERE user_id = ? AND group_id = ? AND subgroup = ?
                """,
                (user_id, group_id, subgroup),
            )
            for text in clean:
                conn.execute(
                    """
                    INSERT INTO response_template_variants (
                        user_id, group_id, subgroup, template_text, is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, group_id, subgroup, text, self._bool_db(True), now, now),
                )

    def add_template_variant(
        self,
        *,
        user_id: int,
        group_id: str,
        subgroup: str,
        template_text: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            row_id = self._insert_and_get_id(
                conn,
                """
                INSERT INTO response_template_variants (
                    user_id, group_id, subgroup, template_text, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, group_id, subgroup, template_text.strip(), self._bool_db(True), now, now),
            )
            row = conn.execute(
                "SELECT * FROM response_template_variants WHERE id = ?",
                (row_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Template variant creation failed")
        return self._row_to_dict(row)

    def get_template_variant_by_id(self, *, user_id: int, template_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM response_template_variants WHERE user_id = ? AND id = ?",
                (user_id, template_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def delete_template_variant(self, *, user_id: int, template_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                DELETE FROM response_template_variants
                WHERE user_id = ? AND id = ?
                """,
                (user_id, template_id),
            )
        return result.rowcount > 0

    def get_template_pool_for_reviews(
        self,
        *,
        user_id: int,
        group_ids: list[str],
    ) -> dict[tuple[str, str], list[str]]:
        """Return ALL active template texts per (group_id, subgroup) for the given groups.

        Returns dict keyed by (group_id, subgroup_or_empty) → list of template texts.
        The caller must call random.choice() per review to ensure each review
        gets an independently random template (not one shared pick per group).
        """
        if not group_ids:
            return {}
        unique_groups = list(set(group_ids))
        placeholders = ", ".join("?" for _ in unique_groups)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT group_id, subgroup, template_text
                FROM response_template_variants
                WHERE user_id = ? AND group_id IN ({placeholders})
                  AND is_active = {self._bool_true_literal()}
                """,
                [user_id, *unique_groups],
            ).fetchall()
        from collections import defaultdict
        pool: dict[tuple[str, str], list[str]] = defaultdict(list)
        for r in rows:
            text = str(r["template_text"] or "").strip()
            if text:
                pool[(str(r["group_id"] or ""), str(r["subgroup"] or ""))].append(text)
        return pool

    def get_random_template_variant(
        self,
        *,
        user_id: int,
        group_id: str,
        subgroup: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["user_id = ?", "group_id = ?", f"is_active = {self._bool_true_literal()}"]
        params: list[Any] = [user_id, group_id]
        if subgroup:
            clauses.append("subgroup = ?")
            params.append(subgroup)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM response_template_variants
                WHERE {where}
                ORDER BY RANDOM()
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def upsert_processing_rule(
        self,
        *,
        user_id: int,
        group_id: str,
        action_mode: str,
        auto_send: bool,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO processing_rules (user_id, group_id, action_mode, auto_send, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, group_id) DO UPDATE SET
                    action_mode = excluded.action_mode,
                    auto_send = excluded.auto_send,
                    updated_at = excluded.updated_at
                """,
                (user_id, group_id, action_mode, self._bool_db(auto_send), _utc_now()),
            )

    def list_processing_rules(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM processing_rules
                WHERE user_id = ?
                ORDER BY group_id ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_processing_rule(self, *, user_id: int, group_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM processing_rules
                WHERE user_id = ? AND group_id = ?
                """,
                (user_id, group_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def replace_processing_rules(self, *, user_id: int, rules: list[dict[str, Any]]) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("DELETE FROM processing_rules WHERE user_id = ?", (user_id,))
            for item in rules:
                conn.execute(
                    """
                    INSERT INTO processing_rules (user_id, group_id, action_mode, auto_send, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        str(item.get("group_id") or ""),
                        str(item.get("action_mode") or "manual"),
                        self._bool_db(bool(item.get("auto_send"))),
                        now,
                    ),
                )

    def list_recommendations(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_article, target_article
                FROM product_recommendations
                WHERE user_id = ? AND is_active = ?
                ORDER BY source_article ASC, target_article ASC
                """,
                (user_id, self._bool_db(True)),
            ).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            source = str(row["source_article"] or "").strip()
            target = str(row["target_article"] or "").strip()
            if not source or not target:
                continue
            grouped.setdefault(source, []).append(target)
        items: list[dict[str, Any]] = []
        for source, targets in grouped.items():
            items.append(
                {
                    "source_article": source,
                    "target_articles": targets,
                    "targets_csv": ", ".join(targets),
                }
            )
        return items

    def replace_all_recommendations(self, *, user_id: int, rows: list[dict[str, Any]]) -> int:
        now = _utc_now()
        inserted = 0
        with self._connect() as conn:
            conn.execute("DELETE FROM product_recommendations WHERE user_id = ?", (user_id,))
            for row in rows:
                source_raw = str(row.get("source_article") or "").strip()
                if not source_raw:
                    continue
                targets_raw = row.get("target_articles")
                if not isinstance(targets_raw, list):
                    continue
                seen_targets: set[str] = set()
                for target_value in targets_raw:
                    target = str(target_value or "").strip()
                    if not target or target in seen_targets:
                        continue
                    seen_targets.add(target)
                    conn.execute(
                        """
                        INSERT INTO product_recommendations (
                            user_id, source_article, target_article, is_active, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (user_id, source_raw, target, self._bool_db(True), now, now),
                    )
                    inserted += 1
        return inserted

    def get_random_recommendation(self, *, user_id: int, source_article: str) -> str | None:
        source = source_article.strip()
        if not source:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT target_article
                FROM product_recommendations
                WHERE user_id = ? AND source_article = ? AND is_active = ?
                ORDER BY RANDOM()
                LIMIT 1
                """,
                (user_id, source, self._bool_db(True)),
            ).fetchone()
        if row is None:
            return None
        target = str(row["target_article"] or "").strip()
        return target or None

    def ensure_default_template_variables(self) -> int:
        # Backward-compatible no-op: template variables are not auto-seeded.
        return 0

    def list_template_variables(self, *, only_active: bool = False) -> list[dict[str, Any]]:
        clauses: list[str] = []
        if only_active:
            clauses.append(f"is_active = {self._bool_true_literal()}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM template_variables
                {where}
                ORDER BY var_key ASC
                """
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def upsert_template_variable(
        self,
        *,
        var_key: str,
        title: str,
        description: str | None = None,
        is_user_editable: bool,
        source_type: str,
        source_path: str | None = None,
        default_value: str | None = None,
        is_active: bool = True,
    ) -> dict[str, Any]:
        normalized_key = var_key.strip().upper()
        if not normalized_key:
            raise ValueError("var_key is required")
        if not TEMPLATE_VARIABLE_KEY_RE.fullmatch(normalized_key):
            raise ValueError("var_key must match ^%[A-Z0-9_]{2,50}%$")
        normalized_source_type = (source_type or "").strip().lower() or "manual"
        if normalized_source_type not in {"manual", "review_field", "system"}:
            raise ValueError("source_type must be one of: manual, review_field, system")
        normalized_source_path = str(source_path or "").strip()
        if normalized_source_type in {"review_field", "system"} and not normalized_source_path:
            raise ValueError("source_path is required for review_field/system")
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO template_variables (
                    var_key, title, description, is_user_editable, source_type, source_path, default_value, is_active,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (var_key) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    is_user_editable = excluded.is_user_editable,
                    source_type = excluded.source_type,
                    source_path = excluded.source_path,
                    default_value = excluded.default_value,
                    is_active = excluded.is_active,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_key,
                    title.strip() or normalized_key,
                    str(description or ""),
                    self._bool_db(is_user_editable),
                    normalized_source_type,
                    normalized_source_path,
                    str(default_value or ""),
                    self._bool_db(is_active),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT *
                FROM template_variables
                WHERE var_key = ?
                LIMIT 1
                """,
                (normalized_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Template variable upsert failed")
        return self._row_to_dict(row)

    def delete_template_variable(self, *, var_key: str) -> bool:
        normalized_key = var_key.strip().upper()
        if not normalized_key:
            return False
        with self._connect() as conn:
            result = conn.execute(
                """
                DELETE FROM template_variables
                WHERE var_key = ?
                """,
                (normalized_key,),
            )
        return result.rowcount > 0

    def list_user_template_variable_values(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    v.id AS variable_id,
                    v.var_key,
                    v.title,
                    v.description,
                    v.is_user_editable,
                    v.source_type,
                    v.source_path,
                    v.default_value,
                    v.is_active,
                    uv.value,
                    uv.updated_at AS value_updated_at
                FROM template_variables v
                LEFT JOIN user_template_variable_values uv
                  ON uv.variable_id = v.id
                 AND uv.user_id = ?
                WHERE v.is_active = {self._bool_true_literal()}
                ORDER BY v.var_key ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def save_user_template_variable_values(self, *, user_id: int, values: dict[str, str]) -> int:
        updates = 0
        now = _utc_now()
        normalized_values: dict[str, str] = {}
        for key, value in values.items():
            normalized_key = str(key or "").strip().upper()
            if not normalized_key:
                continue
            normalized_values[normalized_key] = str(value or "").strip()
        if not normalized_values:
            return 0
        with self._connect() as conn:
            variable_rows = conn.execute(
                """
                SELECT id, var_key, is_user_editable
                FROM template_variables
                WHERE var_key IN ({})
                """.format(",".join("?" for _ in normalized_values)),
                tuple(normalized_values.keys()),
            ).fetchall()
            for row in variable_rows:
                if not bool(row["is_user_editable"]):
                    continue
                var_key = str(row["var_key"] or "").strip().upper()
                if var_key not in normalized_values:
                    continue
                conn.execute(
                    """
                    INSERT INTO user_template_variable_values (user_id, variable_id, value, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (user_id, variable_id) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, int(row["id"]), normalized_values[var_key], now),
                )
                updates += 1
        return updates

    def build_template_variables_context(
        self,
        *,
        user_id: int | None,
        review_author: str | None,
        review_rating: int | str | None,
        review_category: str | None,
        review_sentiment: str | None,
        review_tags: str | list[str] | None,
        review_metadata: dict[str, Any] | None,
    ) -> dict[str, str]:
        metadata = review_metadata if isinstance(review_metadata, dict) else {}
        tags_text = ", ".join(review_tags) if isinstance(review_tags, list) else str(review_tags or "")
        # Backward-compatible defaults for historic placeholders used in templates/tests.
        default_brand = "VarFabric"
        default_name = str(review_author or "").strip()
        context: dict[str, str] = {
            "%AUTHOR%": str(review_author or "").strip() or "клиент",
            "%RATING%": str(review_rating if review_rating is not None else ""),
            "%CATEGORY%": str(review_category or ""),
            "%SENTIMENT%": str(review_sentiment or ""),
            "%TAGS%": tags_text.strip(),
            "%BRAND%": default_brand,
            "%NAME%": default_name,
        }
        if metadata:
            for key, value in metadata.items():
                key_name = str(key or "").strip().upper()
                if not key_name:
                    continue
                context[f"%META_{key_name}%"] = str(value or "").strip()

        variables = self.list_template_variables(only_active=True)
        user_values_map: dict[str, str] = {}
        if user_id is not None:
            for row in self.list_user_template_variable_values(user_id=user_id):
                value = str(row.get("value") or "").strip()
                if value:
                    user_values_map[str(row.get("var_key") or "").strip().upper()] = value

        for item in variables:
            key = str(item.get("var_key") or "").strip().upper()
            if not key:
                continue
            source_type = str(item.get("source_type") or "manual").strip().lower()
            # Backward compatibility for older rows saved as "review".
            if source_type == "review":
                source_type = "review_field"
            source_path = str(item.get("source_path") or "").strip()
            default_value = str(item.get("default_value") or "").strip()
            resolved = ""
            if source_type == "review_field":
                if source_path in {"author", "name", "author_name", "author_name_ozon"}:
                    resolved = str(review_author or "").strip()
                elif source_path in {"rating"}:
                    resolved = str(review_rating if review_rating is not None else "").strip()
                elif source_path in {"category"}:
                    resolved = str(review_category or "").strip()
                elif source_path in {"sentiment"}:
                    resolved = str(review_sentiment or "").strip()
                elif source_path in {"tags"}:
                    resolved = tags_text.strip()
                elif source_path.startswith("metadata."):
                    meta_key = source_path.split(".", 1)[1].strip()
                    resolved = str(metadata.get(meta_key) or "").strip()
            elif source_type == "system":
                if source_path in {"author_name", "review_author"}:
                    resolved = str(review_author or "").strip()
                elif source_path == "review_rating":
                    resolved = str(review_rating if review_rating is not None else "").strip()
                elif source_path == "review_category":
                    resolved = str(review_category or "").strip()
                elif source_path == "review_sentiment":
                    resolved = str(review_sentiment or "").strip()
                elif source_path == "review_tags":
                    resolved = tags_text.strip()
                elif source_path.startswith("metadata."):
                    meta_key = source_path.split(".", 1)[1].strip()
                    resolved = str(metadata.get(meta_key) or "").strip()
            if source_type == "manual":
                resolved = user_values_map.get(key) or default_value
            if not resolved:
                resolved = user_values_map.get(key) or default_value
            context[key] = str(resolved or "")
        return context

    @staticmethod
    def make_review_uid(user_id: int, source: str, account_id: int | None, external_review_id: str) -> str:
        account_part = str(account_id) if account_id is not None else "na"
        return f"{user_id}:{source}:{account_part}:{external_review_id}"

    @staticmethod
    def make_conversation_uid(
        user_id: int,
        source: str,
        account_id: int | None,
        kind: str,
        external_conversation_id: str,
    ) -> str:
        account_part = str(account_id) if account_id is not None else "na"
        return f"{user_id}:{source}:{account_part}:{kind}:{external_conversation_id}"

    def upsert_processed_review(
        self,
        *,
        user_id: int,
        source: str,
        account_id: int | None,
        review: ReviewInput,
        processed: ProcessedReview,
        category: str,
        processing_mode: str,
        status: str,
        auto_reply: str | None = None,
        _log_action: dict[str, object] | None = None,
    ) -> None:
        """Upsert a processed review.

        Pass ``_log_action`` to also write a review_action in the same
        DB transaction (avoids opening a second connection per review).
        """
        review_uid = self.make_review_uid(user_id, source, account_id, review.review_id)
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO review_items (
                    review_uid, user_id, external_review_id, source, account_id, text, author, rating, metadata_json,
                    normalized_text, sentiment_score, sentiment_label, is_spam, is_toxic,
                    priority, tags_json, recommended_action, category, processing_mode, status, auto_reply,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_uid) DO UPDATE SET
                    text = excluded.text,
                    author = excluded.author,
                    rating = excluded.rating,
                    metadata_json = CASE
                        WHEN review_items.metadata_json::jsonb->>'classified_group_id' IS NOT NULL
                         AND review_items.metadata_json::jsonb->>'classified_group_id' != ''
                         AND review_items.metadata_json::jsonb->>'classified_group_id' != 'ai_unclassified'
                        THEN excluded.metadata_json::jsonb || jsonb_build_object(
                            'classified_group_id', review_items.metadata_json::jsonb->>'classified_group_id',
                            'classified_subgroup', review_items.metadata_json::jsonb->>'classified_subgroup'
                        )
                        ELSE excluded.metadata_json
                    END,
                    normalized_text = excluded.normalized_text,
                    sentiment_score = excluded.sentiment_score,
                    sentiment_label = excluded.sentiment_label,
                    is_spam = excluded.is_spam,
                    is_toxic = excluded.is_toxic,
                    priority = excluded.priority,
                    tags_json = excluded.tags_json,
                    recommended_action = excluded.recommended_action,
                    category = CASE
                        WHEN review_items.category IS NOT NULL
                         AND review_items.category != ''
                         AND review_items.category != 'ai_unclassified'
                        THEN review_items.category
                        ELSE excluded.category
                    END,
                    processing_mode = excluded.processing_mode,
                    status = CASE
                        -- ALWAYS preserve manual operator replies — never overwrite human work
                        WHEN review_items.status = 'answered_manual' THEN review_items.status
                        -- For YM: reset only answered_AUTO (bot artefact from loop)
                        -- when marketplace still reports needReaction=true (unanswered on YM side)
                        WHEN review_items.status = 'answered_auto'
                         AND excluded.source = 'yandex'
                         AND excluded.metadata_json::jsonb->'raw'->>'needReaction' = 'true'
                        THEN excluded.status
                        -- All other cases: preserve answered status
                        WHEN review_items.status IN ('answered_auto') THEN review_items.status
                        ELSE excluded.status
                    END,
                    auto_reply = CASE
                        WHEN review_items.status = 'answered_manual' THEN review_items.auto_reply
                        WHEN review_items.status = 'answered_auto'
                         AND excluded.source = 'yandex'
                         AND excluded.metadata_json::jsonb->'raw'->>'needReaction' = 'true'
                        THEN excluded.auto_reply
                        WHEN review_items.status IN ('answered_auto') THEN review_items.auto_reply
                        ELSE excluded.auto_reply
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    review_uid,
                    user_id,
                    review.review_id,
                    source,
                    account_id,
                    review.text,
                    review.author,
                    review.rating,
                    self._json_param(review.metadata),
                    processed.normalized_text,
                    processed.sentiment_score,
                    processed.sentiment_label,
                    self._bool_db(bool(processed.is_spam)),
                    self._bool_db(bool(processed.is_toxic)),
                    processed.priority,
                    self._json_param(processed.tags),
                    processed.recommended_action,
                    category,
                    processing_mode,
                    status,
                    auto_reply,
                    now,
                    now,
                ),
            )
            # Optionally write the review_action in the same transaction
            if _log_action:
                action_type = str(_log_action.get("action_type") or "sync_review")
                details = _log_action.get("details") or {}
                conn.execute(
                    self._sql("""
                    INSERT INTO review_actions (
                        user_id, review_uid, action_type, actor, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """),
                    (user_id, review_uid, action_type, "system", self._json_param(details), now),
                )

    def list_reviews(
        self,
        *,
        user_id: int,
        source: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        statuses: list[str] | None = None,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "newest",
        limit: int = 200,
        account_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        page_data = self.list_reviews_paginated(
            user_id=user_id,
            source=source,
            priority=priority,
            status=status,
            statuses=statuses,
            category=category,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            page=1,
            page_size=limit,
            bucket="all",
            account_ids=account_ids,
        )
        return list(page_data["items"])

    def list_reviews_paginated(
        self,
        *,
        user_id: int,
        source: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        statuses: list[str] | None = None,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "newest",
        page: int = 1,
        page_size: int = 30,
        bucket: str = "all",
        account_ids: list[int] | None = None,
        product_search: str | None = None,
        has_contradiction: bool = False,
    ) -> dict[str, Any]:
        base_clauses: list[str] = ["user_id = ?"]
        base_params: list[Any] = [user_id]
        if source:
            base_clauses.append("source = ?")
            base_params.append(source)
        if priority:
            base_clauses.append("priority = ?")
            base_params.append(priority)
        if category:
            base_clauses.append("category = ?")
            base_params.append(category)
        if product_search:
            q = f"%{product_search.strip()}%"
            base_clauses.append(
                "(metadata_json::jsonb->'raw'->'productDetails'->>'productName' ILIKE ?"
                " OR metadata_json::jsonb->'raw'->'productDetails'->>'supplierArticle' ILIKE ?"
                " OR metadata_json::jsonb->'raw'->>'supplierArticle' ILIKE ?"
                " OR metadata_json::jsonb->'raw'->'productDetails'->>'nmId'::text ILIKE ?)"
            )
            base_params.extend([q, q, q, q])
        if has_contradiction:
            # Reviews where rating_contradiction flag set OR rating matches contradiction rules
            base_clauses.append(
                "(metadata_json::jsonb->'rating_contradiction' IS NOT NULL"
                " OR EXISTS ("
                "  SELECT 1 FROM review_contradiction_rules rcr"
                "  WHERE rcr.user_id = review_items.user_id"
                "  AND rcr.group_id = review_items.category"
                "  AND review_items.rating IS NOT NULL"
                "  AND rcr.ratings_json::jsonb @> to_jsonb(review_items.rating)"
                " ))"
            )
        normalized_account_ids = sorted(
            {
                int(value)
                for value in (account_ids or [])
                if isinstance(value, int) or (isinstance(value, str) and str(value).strip().isdigit())
            }
        )
        if normalized_account_ids:
            placeholders = ", ".join("?" for _ in normalized_account_ids)
            base_clauses.append(f"account_id IN ({placeholders})")
            base_params.extend(normalized_account_ids)
        if date_from:
            base_clauses.append("updated_at::date >= ?::date")

            base_params.append(date_from)
        if date_to:
            base_clauses.append("updated_at::date <= ?::date")

            base_params.append(date_to)

        view_clauses = list(base_clauses)
        view_params = list(base_params)
        status_values = [str(item).strip() for item in (statuses or []) if str(item).strip()]
        if status_values:
            placeholders = ", ".join("?" for _ in status_values)
            view_clauses.append(f"status IN ({placeholders})")
            view_params.extend(status_values)
        elif status:
            view_clauses.append("status = ?")
            view_params.append(status)
        elif bucket == "new":
            view_clauses.append("status NOT IN ('answered_auto', 'answered_manual', 'ignored')")
        elif bucket == "processed":
            view_clauses.append("status IN ('answered_auto', 'answered_manual', 'ignored')")

        safe_page = max(page, 1)
        safe_page_size = min(max(page_size, 1), 500)

        where_base = " AND ".join(base_clauses)
        where_view = " AND ".join(view_clauses)
        sort_key = sort.strip().lower()
        # Sort by actual review creation date (metadata_json.raw.createdDate) with fallback
        # to review_uid (always unique). updated_at = sync timestamp — all reviews from one
        # batch share the same value, making it useless as a sort key.
        _cd_expr = "COALESCE(metadata_json::jsonb->'raw'->>'createdDate', '')"

        order_by_map = {
            "newest": f"{_cd_expr} DESC, review_uid DESC",
            "oldest": f"{_cd_expr} ASC, review_uid ASC",
            "rating_asc": f"COALESCE(rating, 0) ASC, {_cd_expr} DESC, review_uid DESC",
            "rating_desc": f"COALESCE(rating, 0) DESC, {_cd_expr} DESC, review_uid DESC",
            "category": f"category ASC, {_cd_expr} DESC, review_uid DESC",
        }
        order_by = order_by_map.get(sort_key, order_by_map["newest"])

        with self._connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM review_items WHERE {where_view}",
                tuple(view_params),
            ).fetchone()
            total = int(total_row["c"]) if total_row else 0
            pages = max((total + safe_page_size - 1) // safe_page_size, 1)
            safe_page = min(safe_page, pages)
            offset = (safe_page - 1) * safe_page_size
            new_row = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM review_items
                WHERE {where_base}
                  AND status NOT IN ('answered_auto', 'answered_manual', 'ignored')
                """,
                tuple(base_params),
            ).fetchone()
            processed_row = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM review_items
                WHERE {where_base}
                  AND status IN ('answered_auto', 'answered_manual', 'ignored')
                """,
                tuple(base_params),
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT *
                FROM review_items
                WHERE {where_view}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                tuple([*view_params, safe_page_size, offset]),
            ).fetchall()
            items = [self._row_to_dict(row) for row in rows]
            review_uids = [str(item.get("review_uid") or "") for item in items if item.get("review_uid")]
            error_map: dict[str, str] = {}
            if review_uids:
                placeholders = ", ".join("?" for _ in review_uids)
                action_rows = conn.execute(
                    f"""
                    SELECT review_uid, details_json
                    FROM review_actions
                    WHERE user_id = ?
                      AND action_type = 'send_reply_error'
                      AND review_uid IN ({placeholders})
                    ORDER BY created_at DESC
                    """,
                    tuple([user_id, *review_uids]),
                ).fetchall()
                for action_row in action_rows:
                    uid = str(action_row["review_uid"] or "")
                    if not uid or uid in error_map:
                        continue
                    details_raw = str(action_row["details_json"] or "{}")
                    details = _json_load(details_raw, {})
                    reason = str(details.get("error") or "").strip()
                    if reason:
                        error_map[uid] = reason
            for item in items:
                uid = str(item.get("review_uid") or "")
                if not uid:
                    continue
                item["send_error_message"] = error_map.get(uid)

        return {
            "items": items,
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
            "pages": pages,
            "new_count": int(new_row["c"]) if new_row else 0,
            "processed_count": int(processed_row["c"]) if processed_row else 0,
        }

    def list_review_sources(self, *, user_id: int) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT source
                FROM review_items
                WHERE user_id = ?
                ORDER BY source ASC
                """,
                (user_id,),
            ).fetchall()
        return [str(row["source"]) for row in rows if row["source"] is not None and str(row["source"]).strip()]

    def clear_reviews(self, *, user_id: int) -> int:
        with self._connect() as conn:
            result = conn.execute("DELETE FROM review_items WHERE user_id = ?", (user_id,))
        return int(result.rowcount or 0)

    def list_unprocessed_reviews(self, *, user_id: int, limit: int = 5000) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM review_items
                WHERE user_id = ? AND status NOT IN ('answered_auto', 'answered_manual', 'ignored')
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_review_sync_states_for_account(
        self,
        *,
        user_id: int,
        source: str,
        account_id: int,
    ) -> dict[str, dict[str, Any]]:
        """Map external_review_id / review_uid -> sync state for one marketplace account.

        Used by Yandex auto-sync to skip comments API / no-op upserts and to reconcile
        portal-answered reviews without re-paging the full feedback catalog.
        """
        sql = self._sql(
            """
            SELECT review_uid,
                   external_review_id,
                   status,
                   auto_reply,
                   created_at,
                   metadata_json::jsonb->'raw'->>'createdDate' AS marketplace_created
            FROM review_items
            WHERE user_id = ?
              AND source = ?
              AND account_id = ?
            """
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (user_id, source, account_id)).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            d = self._row_to_dict(row)
            payload = {
                "review_uid": str(d.get("review_uid") or "").strip(),
                "external_review_id": str(d.get("external_review_id") or "").strip(),
                "status": str(d.get("status") or "").strip(),
                "auto_reply": str(d.get("auto_reply") or "").strip(),
                "created_at": d.get("created_at"),
                "marketplace_created": str(d.get("marketplace_created") or "").strip() or None,
            }
            ext = payload["external_review_id"]
            uid = payload["review_uid"]
            if ext:
                result[ext] = payload
            if uid:
                result[uid] = payload
        return result

    def get_question_sync_states_for_account(
        self,
        *,
        user_id: int,
        source: str,
        account_id: int,
    ) -> dict[str, dict[str, Any]]:
        """Map external_conversation_id -> question sync state for one account."""
        sql = self._sql(
            """
            SELECT conversation_uid,
                   external_conversation_id,
                   status,
                   metadata_json,
                   last_message_at,
                   created_at
            FROM conversation_items
            WHERE user_id = ?
              AND source = ?
              AND account_id = ?
              AND kind = 'question'
            """
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (user_id, source, account_id)).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            d = self._row_to_dict(row)
            meta_raw = d.get("metadata_json")
            metadata: dict[str, Any] = {}
            if isinstance(meta_raw, dict):
                metadata = meta_raw
            elif isinstance(meta_raw, str) and meta_raw.strip():
                try:
                    parsed = json.loads(meta_raw)
                    if isinstance(parsed, dict):
                        metadata = parsed
                except Exception:
                    metadata = {}
            ext = str(d.get("external_conversation_id") or "").strip()
            if not ext:
                continue
            result[ext] = {
                "conversation_uid": str(d.get("conversation_uid") or "").strip(),
                "external_conversation_id": ext,
                "status": str(d.get("status") or "").strip(),
                "metadata": metadata,
                "last_message_at": d.get("last_message_at"),
                "created_at": d.get("created_at"),
            }
        return result

    def update_review_processing_result(
        self,
        *,
        user_id: int,
        review_uid: str,
        status: str,
        auto_reply: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE review_items
                SET status = ?, auto_reply = ?, updated_at = ?
                WHERE user_id = ? AND review_uid = ?
                """,
                (status, auto_reply, _utc_now(), user_id, review_uid),
            )
        return result.rowcount > 0

    def upsert_conversation(
        self,
        *,
        user_id: int,
        source: str,
        account_id: int | None,
        external_conversation_id: str,
        kind: str,
        customer_name: str | None,
        message_text: str,
        status: str,
        unread_count: int,
        metadata: dict[str, Any] | None = None,
        last_message_at: str | None = None,
        seller_replied_at: str | None = None,
        buyer_has_unread: bool = False,
    ) -> str:
        """Upsert a conversation record.

        ``seller_replied_at`` should be set to the timestamp of the seller's
        last message (from the WB events endpoint).  When provided it is written
        to ``last_sent_at``, which drives the "answered" / "needs reply" bucket
        logic:  processed_by_operator = last_sent_at IS NOT NULL AND
        last_sent_at >= last_message_at.
        Only update last_sent_at when the incoming value is newer than the
        stored one so that a manual reply from our app is never overwritten.

        ``buyer_has_unread=True`` signals that the marketplace confirmed the
        buyer has written new messages the seller has not replied to yet.
        In this case last_sent_at is cleared so the chat moves to the "New"
        bucket immediately, regardless of stored timestamps.
        """
        conversation_uid = self.make_conversation_uid(
            user_id=user_id,
            source=source,
            account_id=account_id,
            kind=kind,
            external_conversation_id=external_conversation_id,
        )
        now = _utc_now()
        # Do NOT fallback to `now` when last_message_at is unknown.
        # Using `now` as fallback causes the manually_closed_at guard to fail:
        # now > manually_closed_at is always TRUE, incorrectly resetting
        # chats that were manually moved to "Answered".
        # When None, the SQL CASE preserves the existing stored value.
        last_message_ts = last_message_at
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_items (
                    conversation_uid, user_id, source, account_id, external_conversation_id,
                    kind, customer_name, message_text, status, unread_count, metadata_json,
                    send_error_code, send_error_message, send_attempts, last_send_attempt_at, last_sent_at,
                    last_message_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL, ?, ?, ?, ?)
                ON CONFLICT(conversation_uid) DO UPDATE SET
                    customer_name = excluded.customer_name,
                    -- Preserve previous message_text when the incoming value is empty.
                    -- WB omits lastMessage for photo-only messages, so message_text=""
                    -- would overwrite a meaningful previous text (e.g. "Ок").
                    message_text = CASE
                        WHEN TRIM(COALESCE(excluded.message_text, '')) != '' THEN excluded.message_text
                        ELSE conversation_items.message_text
                    END,
                    -- Don't downgrade from answered/ignored if manually closed
                    -- and no newer buyer message arrived since closure.
                    status = CASE
                        WHEN conversation_items.manually_closed_at IS NOT NULL
                             AND conversation_items.status IN ('answered_manual', 'answered_auto', 'ignored')
                             AND excluded.status NOT IN ('answered_manual', 'answered_auto', 'ignored')
                             AND (
                                 excluded.last_message_at IS NULL
                                 OR excluded.last_message_at::text <= conversation_items.manually_closed_at::text
                             )
                        THEN conversation_items.status
                        ELSE excluded.status
                    END,
                    unread_count = excluded.unread_count,
                    metadata_json = excluded.metadata_json,
                    send_error_code = CASE
                        WHEN excluded.last_message_at <> conversation_items.last_message_at THEN NULL
                        ELSE conversation_items.send_error_code
                    END,
                    send_error_message = CASE
                        WHEN excluded.last_message_at <> conversation_items.last_message_at THEN NULL
                        ELSE conversation_items.send_error_message
                    END,
                    send_attempts = CASE
                        WHEN excluded.last_message_at <> conversation_items.last_message_at THEN 0
                        ELSE conversation_items.send_attempts
                    END,
                    last_send_attempt_at = CASE
                        WHEN excluded.last_message_at <> conversation_items.last_message_at THEN NULL
                        ELSE conversation_items.last_send_attempt_at
                    END,
                    last_message_at = CASE
                        WHEN excluded.last_message_at IS NULL THEN conversation_items.last_message_at
                        WHEN conversation_items.last_message_at IS NULL THEN excluded.last_message_at
                        WHEN excluded.last_message_at > conversation_items.last_message_at THEN excluded.last_message_at
                        ELSE conversation_items.last_message_at
                    END,
                    last_sent_at = CASE
                        -- Keep existing if incoming is NULL (no new seller reply detected)
                        WHEN excluded.last_sent_at IS NULL THEN conversation_items.last_sent_at
                        -- current IS NULL means buyer wrote last and is waiting — do NOT
                        -- restore an old seller reply timestamp from events. NULL must
                        -- remain until seller explicitly replies (via mark_conversation_answered)
                        -- or a full sync re-inserts the row with a fresh seller timestamp.
                        WHEN conversation_items.last_sent_at IS NULL THEN NULL
                        -- Both set: advance only if incoming is newer
                        WHEN excluded.last_sent_at > conversation_items.last_sent_at THEN excluded.last_sent_at
                        ELSE conversation_items.last_sent_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_uid,
                    user_id,
                    source,
                    account_id,
                    external_conversation_id,
                    kind,
                    customer_name,
                    message_text,
                    status,
                    max(unread_count, 0),
                    self._json_param(metadata or {}),
                    seller_replied_at or None,
                    last_message_ts,
                    now,
                    now,
                ),
            )
            # When the marketplace confirms the buyer has unread messages
            # force the chat to "New" bucket — but ONLY if the buyer's message
            # is actually newer than the seller's last reply.
            # Without this guard, stale WB unread_count (API cache lag) after
            # seller replies would incorrectly move the chat back to "New".
            if buyer_has_unread:
                conn.execute(
                    """
                    UPDATE conversation_items
                    SET last_sent_at = NULL, manually_closed_at = NULL, updated_at = ?
                    WHERE user_id = ? AND conversation_uid = ?
                      AND (
                          last_sent_at IS NULL
                          OR (
                              last_message_at IS NOT NULL
                              AND last_message_at::text > last_sent_at::text
                          )
                      )
                      AND (
                          manually_closed_at IS NULL
                          OR (
                              last_message_at IS NOT NULL
                              AND last_message_at::text > manually_closed_at::text
                          )
                      )
                    """,
                    (now, user_id, conversation_uid),
                )
        return conversation_uid

    def list_conversations(
        self,
        *,
        user_id: int,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 200,
        account_permissions: dict[str, list[int]] | None = None,
    ) -> list[dict[str, Any]]:
        page_data = self.list_conversations_paginated(
            user_id=user_id,
            source=None,
            kind=kind,
            status=status,
            statuses=None,
            sort="newest",
            page=1,
            page_size=limit,
            bucket="all",
            account_permissions=account_permissions,
        )
        return list(page_data["items"])

    def list_conversations_paginated(
        self,
        *,
        user_id: int,
        source: str | None = None,
        account_id: int | None = None,
        kind: str | None = None,
        status: str | None = None,
        statuses: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "newest",
        page: int = 1,
        page_size: int = 30,
        bucket: str = "all",
        search: str | None = None,
        account_permissions: dict[str, list[int]] | None = None,
    ) -> dict[str, Any]:
        base_clauses: list[str] = ["user_id = ?"]
        base_params: list[Any] = [user_id]
        if account_id is not None:
            base_clauses.append("account_id = ?")
            base_params.append(account_id)
        elif source:
            base_clauses.append("source = ?")
            base_params.append(source)
        if kind:
            base_clauses.append("kind = ?")
            base_params.append(kind)
        if search:
            # Search by customer name (case-insensitive LIKE)
            base_clauses.append("LOWER(COALESCE(customer_name, '')) LIKE ?")
            base_params.append(f"%{search.strip().lower()}%")
        # Exclude completely empty chats with no activity at all.
        # WB buyer-chat list returns empty text for some chats (e.g. photo-only
        # messages — WB omits lastMessage entirely); Ozon v3/chat/list never
        # returns message text — so we must not filter on text alone.
        # A chat is shown if it has any text OR has unread messages OR has been
        # replied to OR has a recorded last_message_at (any real activity).
        # The last_message_at guard prevents photo-only chats from disappearing:
        # WB does not include the photo in lastMessage summary, so message_text
        # stays empty, but last_message_at is set from the events cursor.
        if kind == "chat":
            base_clauses.append(
                "(TRIM(COALESCE(message_text, '')) != '' OR unread_count > 0"
                " OR last_sent_at IS NOT NULL OR last_message_at IS NOT NULL)"
            )
        if date_from:
            base_clauses.append("updated_at::date >= ?::date")

            base_params.append(date_from)
        if date_to:
            base_clauses.append("updated_at::date <= ?::date")

            base_params.append(date_to)
        # Both last_sent_at and last_message_at are stored as ISO-8601 TEXT.
        # ISO-8601 strings with timezone sort correctly lexicographically, so
        # a plain TEXT comparison works on both SQLite and PostgreSQL.
        # We cast explicitly to TEXT in PostgreSQL to avoid implicit type
        # coercion (the column was altered to TIMESTAMPTZ in some migrations
        # but data is inserted as TEXT strings).
        # A conversation is "processed" if seller replied via our system,
        # OR (for questions only) the status was set to answered by sync.
        # The kind guard ensures questions never bleed into chat queries and vice versa.
        if kind == "question":
            processed_by_operator_clause = (
                "("
                "(last_sent_at IS NOT NULL "
                " AND (last_message_at IS NULL OR last_sent_at::text >= last_message_at::text))"
                " OR status IN ('answered_manual', 'answered_auto')"
                ")"
            )
        else:
            processed_by_operator_clause = (
                "(last_sent_at IS NOT NULL "
                "AND (last_message_at IS NULL OR last_sent_at::text >= last_message_at::text))"
            )

        if account_permissions:
            permission_clauses: list[str] = []
            permission_params: list[Any] = []
            for conversation_kind in ("question", "chat"):
                ids = account_permissions.get(conversation_kind) if isinstance(account_permissions, Mapping) else None
                normalized_ids = sorted(
                    {
                        int(value)
                        for value in (ids or [])
                        if isinstance(value, int) or (isinstance(value, str) and str(value).strip().isdigit())
                    }
                )
                if not normalized_ids:
                    continue
                placeholders = ", ".join("?" for _ in normalized_ids)
                permission_clauses.append(f"(kind = ? AND account_id IN ({placeholders}))")
                permission_params.append(conversation_kind)
                permission_params.extend(normalized_ids)
            if permission_clauses:
                base_clauses.append("(" + " OR ".join(permission_clauses) + ")")
                base_params.extend(permission_params)
            else:
                base_clauses.append("1 = 0")

        view_clauses = list(base_clauses)
        view_params = list(base_params)
        status_values = [str(item).strip() for item in (statuses or []) if str(item).strip()]
        if status_values:
            placeholders = ", ".join("?" for _ in status_values)
            view_clauses.append(f"status IN ({placeholders})")
            view_params.extend(status_values)
        elif status:
            view_clauses.append("status = ?")
            view_params.append(status)
        elif bucket == "new":
            view_clauses.append(f"NOT ({processed_by_operator_clause})")
        elif bucket == "processed":
            view_clauses.append(processed_by_operator_clause)

        safe_page = max(page, 1)
        safe_page_size = min(max(page_size, 1), 2000)
        where_base = " AND ".join(base_clauses)
        where_view = " AND ".join(view_clauses)
        # For questions, use createdDate from raw metadata for accurate sort.
        # Cast all to TEXT so COALESCE works regardless of column type (TEXT vs TIMESTAMPTZ).
        if kind == "question":
            _date_expr = (
                "COALESCE("
                "metadata_json::jsonb->'raw'->>'createdDate',"
                "last_message_at::text,"
                "updated_at::text"
                ")"
            )
        else:
            _date_expr = "COALESCE(last_message_at::text, updated_at::text)"
        order_by_map = {
            "newest": f"{_date_expr} DESC",
            "oldest": f"{_date_expr} ASC",
        }
        order_by = order_by_map.get(sort.strip().lower(), order_by_map["newest"])

        with self._connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM conversation_items WHERE {where_view}",
                tuple(view_params),
            ).fetchone()
            total = int(total_row["c"]) if total_row else 0
            pages = max((total + safe_page_size - 1) // safe_page_size, 1)
            safe_page = min(safe_page, pages)
            offset = (safe_page - 1) * safe_page_size
            new_row = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM conversation_items
                WHERE {where_base}
                  AND NOT ({processed_by_operator_clause})
                """,
                tuple(base_params),
            ).fetchone()
            processed_row = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM conversation_items
                WHERE {where_base}
                  AND {processed_by_operator_clause}
                """,
                tuple(base_params),
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT *
                FROM conversation_items
                WHERE {where_view}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                tuple([*view_params, safe_page_size, offset]),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            raw = data.pop("metadata_json", "{}")
            data["metadata"] = _json_load(raw, {})
            items.append(data)
        return {
            "items": items,
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
            "pages": pages,
            "new_count": int(new_row["c"]) if new_row else 0,
            "processed_count": int(processed_row["c"]) if processed_row else 0,
        }

    def list_conversation_sources(self, *, user_id: int) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT source
                FROM conversation_items
                WHERE user_id = ?
                ORDER BY source ASC
                """,
                (user_id,),
            ).fetchall()
        return [str(row["source"]) for row in rows if row["source"] is not None and str(row["source"]).strip()]

    def list_conversation_accounts(self, *, user_id: int, kind: str | None = None) -> list[dict[str, Any]]:
        """Return distinct (account_id, source, name) pairs that have conversations."""
        kind_clause = " AND ci.kind = ?" if kind else ""
        kind_params: list[Any] = [kind] if kind else []
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT ci.account_id, ci.source,
                       COALESCE(ma.account_name, ci.source) AS name
                FROM conversation_items ci
                LEFT JOIN marketplace_accounts ma
                       ON ma.id = ci.account_id AND ma.user_id = ci.user_id
                WHERE ci.user_id = ?{kind_clause}
                  AND ci.account_id IS NOT NULL
                ORDER BY name ASC
                """,
                (user_id, *kind_params),
            ).fetchall()
        return [
            {
                "account_id": int(row["account_id"]),
                "source": str(row["source"] or ""),
                "name": str(row["name"] or row["source"] or ""),
            }
            for row in rows
        ]

    def delete_conversations_before_date(
        self,
        *,
        user_id: int,
        account_id: int | None = None,
        kind: str | None = None,
        before_date: str,
    ) -> int:
        """Remove conversations whose last_message_at is before ``before_date``.

        Used to enforce the sync-start-date for WB chats: the WB chats list
        endpoint has no date filter, so we sync everything and then prune rows
        that are older than the configured cutoff.
        """
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if account_id is not None:
            clauses.append("account_id = ?")
            params.append(int(account_id))
        if kind:
            clauses.append("kind = ?")
            params.append(str(kind).strip().lower())
        # Compare ISO strings lexicographically - works correctly for both
        # full ISO datetimes and YYYY-MM-DD date strings.
        clauses.append("last_message_at IS NOT NULL")
        clauses.append("last_message_at < ?")
        params.append(str(before_date))
        where = " AND ".join(clauses)
        with self._connect() as conn:
            result = conn.execute(
                f"DELETE FROM conversation_items WHERE {where}", tuple(params)
            )
        return int(result.rowcount or 0)

    def clear_conversations(self, *, user_id: int, kind: str | None = None, source: str | None = None) -> int:
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind in {"question", "chat"}:
            clauses.append("kind = ?")
            params.append(normalized_kind)
        normalized_source = str(source or "").strip().lower()
        if normalized_source:
            clauses.append("source = ?")
            params.append(normalized_source)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            result = conn.execute(f"DELETE FROM conversation_items WHERE {where}", tuple(params))
        return int(result.rowcount or 0)

    def update_conversation_status(self, *, user_id: int, conversation_uid: str, status: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE conversation_items
                SET status = ?, unread_count = CASE WHEN ? = 'closed' THEN 0 ELSE unread_count END, updated_at = ?
                WHERE user_id = ? AND conversation_uid = ?
                """,
                (status, status, _utc_now(), user_id, conversation_uid),
            )
        return result.rowcount > 0

    def move_conversation_to_new(self, *, user_id: int, conversation_uid: str) -> bool:
        """Clear last_sent_at and manually_closed_at so the chat moves to 'New'."""
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE conversation_items
                SET last_sent_at = NULL, manually_closed_at = NULL, updated_at = ?
                WHERE user_id = ? AND conversation_uid = ?
                """,
                (_utc_now(), user_id, conversation_uid),
            )
        return result.rowcount > 0

    def repair_chat_answered_status(self, *, user_id: int) -> int:
        """Fix chats where metadata says last_sender=seller but last_sent_at is NULL.

        This happens when phase-2 events enrichment was interrupted.  We can
        recover without re-fetching events by reading the last_sender field
        already stored in metadata_json.
        """
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE conversation_items
                SET last_sent_at = COALESCE(last_message_at, updated_at),
                    updated_at   = updated_at
                WHERE user_id = ?
                  AND kind = 'chat'
                  AND last_sent_at IS NULL
                  AND (
                      metadata_json LIKE '%"last_sender": "seller"%'
                      OR metadata_json LIKE '%''last_sender'': ''seller''%'
                  )
                """,
                (user_id,),
            )
        return int(result.rowcount or 0)

    def update_conversation_customer_name(
        self,
        *,
        user_id: int,
        conversation_uid: str,
        customer_name: str,
    ) -> bool:
        """Update customer_name for a conversation (used when API enriches name later)."""
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE conversation_items
                SET customer_name = ?, updated_at = ?
                WHERE user_id = ? AND conversation_uid = ?
                  AND (customer_name IS NULL OR TRIM(customer_name) = '')
                """,
                (customer_name, _utc_now(), user_id, conversation_uid),
            )
        return result.rowcount > 0

    def mark_conversation_answered(self, *, user_id: int, conversation_uid: str) -> bool:
        """Set last_sent_at = now, manually_closed_at = now and status = 'answered_manual'.

        manually_closed_at prevents auto-sync from moving this chat back to
        'New' as long as no newer buyer message arrives after this timestamp.
        Setting status ensures the conversation moves to the 'Processed' bucket
        immediately (bucket filter: status IN ('answered_manual', ...)).
        """
        now = _utc_now()
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE conversation_items
                SET last_sent_at = ?, manually_closed_at = ?, status = 'answered_manual', updated_at = ?
                WHERE user_id = ? AND conversation_uid = ?
                """,
                (now, now, now, user_id, conversation_uid),
            )
        return result.rowcount > 0

    def list_chat_quick_templates(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, template_name, template_text, created_at, updated_at
                FROM chat_quick_templates
                WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def add_chat_quick_template(
        self, *, user_id: int, template_text: str, template_name: str
    ) -> dict[str, Any]:
        clean_text = str(template_text or "").strip()
        clean_name = str(template_name or "").strip()
        if not clean_text:
            raise ValueError("template_text is required")
        if not clean_name:
            raise ValueError("template_name is required")
        now = _utc_now()
        with self._connect() as conn:
            template_id = self._insert_and_get_id(
                conn,
                """
                INSERT INTO chat_quick_templates (user_id, template_name, template_text, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, clean_name, clean_text, now, now),
            )
            row = conn.execute(
                """
                SELECT id, user_id, template_name, template_text, created_at, updated_at
                FROM chat_quick_templates
                WHERE id = ? AND user_id = ?
                """,
                (template_id, user_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Chat quick template creation failed")
        return self._row_to_dict(row)

    def update_chat_quick_template(
        self,
        *,
        user_id: int,
        template_id: int,
        template_name: str,
        template_text: str,
    ) -> dict[str, Any] | None:
        clean_text = str(template_text or "").strip()
        clean_name = str(template_name or "").strip()
        if not clean_text:
            raise ValueError("template_text is required")
        if not clean_name:
            raise ValueError("template_name is required")
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_quick_templates
                SET template_name = ?, template_text = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (clean_name, clean_text, now, template_id, user_id),
            )
            row = conn.execute(
                """
                SELECT id, user_id, template_name, template_text, created_at, updated_at
                FROM chat_quick_templates
                WHERE id = ? AND user_id = ?
                """,
                (template_id, user_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def delete_chat_quick_template(self, *, user_id: int, template_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                DELETE FROM chat_quick_templates
                WHERE id = ? AND user_id = ?
                """,
                (template_id, user_id),
            )
        return result.rowcount > 0

    # ── Question quick templates ──────────────────────────────────────────────

    def list_question_quick_templates(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("""
                SELECT id, user_id, template_name, template_text, created_at, updated_at
                FROM question_quick_templates
                WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC
                """),
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def add_question_quick_template(
        self, *, user_id: int, template_text: str, template_name: str
    ) -> dict[str, Any]:
        clean_text = str(template_text or "").strip()
        clean_name = str(template_name or "").strip()
        if not clean_text:
            raise ValueError("template_text is required")
        if not clean_name:
            raise ValueError("template_name is required")
        now = _utc_now()
        with self._connect() as conn:
            template_id = self._insert_and_get_id(
                conn,
                self._sql("""
                INSERT INTO question_quick_templates (user_id, template_name, template_text, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """),
                (user_id, clean_name, clean_text, now, now),
            )
            row = conn.execute(
                self._sql("""
                SELECT id, user_id, template_name, template_text, created_at, updated_at
                FROM question_quick_templates WHERE id = ? AND user_id = ?
                """),
                (template_id, user_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Question quick template creation failed")
        return self._row_to_dict(row)

    def update_question_quick_template(
        self, *, user_id: int, template_id: int, template_name: str, template_text: str
    ) -> dict[str, Any] | None:
        clean_text = str(template_text or "").strip()
        clean_name = str(template_name or "").strip()
        if not clean_text:
            raise ValueError("template_text is required")
        if not clean_name:
            raise ValueError("template_name is required")
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                self._sql("""
                UPDATE question_quick_templates
                SET template_name = ?, template_text = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """),
                (clean_name, clean_text, now, template_id, user_id),
            )
            row = conn.execute(
                self._sql("""
                SELECT id, user_id, template_name, template_text, created_at, updated_at
                FROM question_quick_templates WHERE id = ? AND user_id = ?
                """),
                (template_id, user_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def delete_question_quick_template(self, *, user_id: int, template_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM question_quick_templates WHERE id = ? AND user_id = ?"),
                (template_id, user_id),
            )
        return result.rowcount > 0

    # ── Review quick templates (independent from question templates) ──────────

    def _migrate_review_quick_templates(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_quick_templates (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                template_name TEXT NOT NULL DEFAULT '',
                template_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_review_quick_templates_user "
            "ON review_quick_templates(user_id, updated_at DESC)"
        ))

    def list_review_quick_templates(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("""
                SELECT id, user_id, template_name, template_text, created_at, updated_at
                FROM review_quick_templates
                WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC
                """),
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def add_review_quick_template(
        self, *, user_id: int, template_text: str, template_name: str
    ) -> dict[str, Any]:
        clean_text = str(template_text or "").strip()
        clean_name = str(template_name or "").strip()
        if not clean_text:
            raise ValueError("template_text is required")
        if not clean_name:
            raise ValueError("template_name is required")
        now = _utc_now()
        with self._connect() as conn:
            template_id = self._insert_and_get_id(
                conn,
                self._sql("""
                INSERT INTO review_quick_templates (user_id, template_name, template_text, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """),
                (user_id, clean_name, clean_text, now, now),
            )
            row = conn.execute(
                self._sql("""
                SELECT id, user_id, template_name, template_text, created_at, updated_at
                FROM review_quick_templates WHERE id = ? AND user_id = ?
                """),
                (template_id, user_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Review quick template creation failed")
        return self._row_to_dict(row)

    def delete_review_quick_template(self, *, user_id: int, template_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM review_quick_templates WHERE id = ? AND user_id = ?"),
                (template_id, user_id),
            )
        return result.rowcount > 0

    def get_last_sent_text_for_conversations(
        self, *, user_id: int, conversation_uids: list[str]
    ) -> dict[str, str]:
        """Return {conversation_uid: last_sent_text} for answered questions/conversations."""
        if not conversation_uids:
            return {}
        placeholders = ",".join(["?" for _ in conversation_uids])
        with self._connect() as conn:
            rows = conn.execute(
                self._sql(f"""
                SELECT DISTINCT ON (conversation_uid) conversation_uid, message_text
                FROM conversation_messages
                WHERE user_id = ?
                  AND conversation_uid IN ({placeholders})
                  AND direction = 'outbound'
                  AND send_status = 'sent'
                ORDER BY conversation_uid, created_at DESC
                """),
                (user_id, *conversation_uids),
            ).fetchall()
        return {self._row_to_dict(r)["conversation_uid"]: self._row_to_dict(r)["message_text"] for r in rows}

    def list_conversation_messages(
        self,
        *,
        user_id: int,
        conversation_uid: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 1000)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM conversation_messages
                WHERE user_id = ? AND conversation_uid = ?
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (user_id, conversation_uid, safe_limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def update_conversation_message_idempotency_key(
        self,
        *,
        user_id: int,
        conversation_uid: str,
        old_key: str,
        new_key: str,
    ) -> bool:
        """Replace a temporary idempotency key with the WB eventID-based key.

        Used after sending a message to link our DB record to the WB event so
        that when we later download events the ON CONFLICT DO NOTHING prevents
        a duplicate entry.
        """
        clean_old = str(old_key or "").strip()
        clean_new = str(new_key or "").strip()
        if not clean_old or not clean_new or clean_old == clean_new:
            return False
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE conversation_messages
                SET idempotency_key = ?
                WHERE user_id = ? AND conversation_uid = ? AND idempotency_key = ?
                """,
                (clean_new, user_id, conversation_uid, clean_old),
            )
        return result.rowcount > 0

    def get_conversation_message_by_idempotency(
        self,
        *,
        user_id: int,
        conversation_uid: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        clean_key = str(idempotency_key or "").strip()
        if not clean_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM conversation_messages
                WHERE user_id = ? AND conversation_uid = ? AND idempotency_key = ?
                """,
                (user_id, conversation_uid, clean_key),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def fix_wb_internal_photo_urls(self, *, user_id: int, conversation_uid: str) -> int:
        """Convert WB internal K8s image URLs to wb-download:id tokens.

        Old format: [img:http://sellers-chat-inner.chat.k8s.cc-xs/internal/v1/file/{uuid}]
        New format: [img:wb-download:{uuid}]

        Returns number of rows updated.
        """
        sql = """
            UPDATE conversation_messages
            SET message_text = regexp_replace(
                message_text,
                '\\[img:http://sellers-chat-inner[^/]*/internal/v1/file/([^\\]]+)\\]',
                '[img:wb-download:\\1]',
                'g'
            )
            WHERE user_id = %s AND conversation_uid = %s
              AND message_text LIKE '%sellers-chat-inner%'
        """

        with self._connect() as conn:
            result = conn.execute(self._sql(sql), (user_id, conversation_uid))
        return int(result.rowcount or 0)

    def fix_ozon_photo_messages(self, *, user_id: int, conversation_uid: str) -> int:
        """Convert legacy Ozon photo messages stored as Markdown to [img:url] tokens.

        Old format (before fix): ``![](https://api-seller.ozon.ru/...)``
        New format: ``[img:https://api-seller.ozon.ru/...]``

        Returns number of rows updated.
        """
        # Markdown: ![](url) — skip first 4 chars '![](' and last 1 char ')'
        # Also fix previously broken '[img:(http...]' entries (from=4 bug)
        sql = """
            UPDATE conversation_messages
            SET message_text =
                CASE
                    WHEN message_text LIKE '![](%%)' THEN
                        '[img:' || substring(message_text from 5 for length(message_text)-5) || ']'
                    WHEN message_text LIKE '%%[img:(http%%' THEN
                        regexp_replace(message_text, '\\[img:\\(([^)]+)\\)', '[img:\\1]', 'g')
                    ELSE message_text
                END
            WHERE user_id = %s AND conversation_uid = %s
              AND (message_text LIKE '![](%%)' OR message_text LIKE '%%[img:(http%%')
        """

        with self._connect() as conn:
            result = conn.execute(self._sql(sql), (user_id, conversation_uid))
        return int(result.rowcount or 0)

    def bulk_insert_chat_history_messages(
        self,
        *,
        user_id: int,
        conversation_uid: str,
        messages: list[dict[str, Any]],
    ) -> int:
        """Insert historical messages from WB events into conversation_messages.

        Each item in ``messages`` must have:
          - direction: 'inbound' | 'outbound'
          - message_text: str
          - idempotency_key: str  (event_id from WB)
          - created_at: str (ISO timestamp of the WB event)
          - operator_name: str | None (clientName or 'Продавец')

        Rows with duplicate idempotency_key are silently skipped.
        Returns the number of newly inserted rows.
        """
        if not messages:
            return 0
        # Build the parameter list, skipping invalid rows
        params: list[tuple] = []
        for msg in messages:
            direction = str(msg.get("direction") or "inbound").strip()
            text = str(msg.get("message_text") or "").strip()
            idem_key = str(msg.get("idempotency_key") or "").strip()
            created = str(msg.get("created_at") or _utc_now()).strip()
            op_name = str(msg.get("operator_name") or "").strip() or None
            if not idem_key or not text:
                continue
            params.append((conversation_uid, user_id, direction, text, op_name, idem_key, created))
        if not params:
            return 0
        sql = self._sql("""
            INSERT INTO conversation_messages (
                conversation_uid, user_id, direction, message_text,
                operator_name, send_status, idempotency_key, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'sent', ?, ?)
            ON CONFLICT(user_id, conversation_uid, idempotency_key) DO NOTHING
        """)
        # All rows in one transaction — the main performance gain vs separate transactions
        inserted = 0
        with self._connect() as conn:
            for row_params in params:
                result = conn.execute(sql, row_params)
                inserted += int(result.rowcount or 0)
        return inserted

    def batch_move_chats_to_new_if_buyer_replied(self, *, user_id: int) -> int:
        """Batch version: fix ALL chats in 'Answered' bucket that have a newer
        inbound message in conversation_messages than last_sent_at.

        Runs once per sync cycle. Uses a single SQL statement — no extra WB API
        calls, no per-chat loop. Returns number of chats moved to 'New'.
        """
        now = _utc_now()
        with self._connect() as conn:
            result = conn.execute(
                self._sql("""
                UPDATE conversation_items ci
                SET last_sent_at = NULL, manually_closed_at = NULL, updated_at = ?
                WHERE ci.user_id = ?
                  AND ci.kind = 'chat'
                  AND ci.last_sent_at IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM conversation_messages cm
                      WHERE cm.user_id = ci.user_id
                        AND cm.conversation_uid = ci.conversation_uid
                        AND cm.direction = 'inbound'
                        AND cm.created_at IS NOT NULL
                        AND cm.created_at::text != ''
                        AND cm.created_at > ci.last_sent_at::timestamptz
                        AND (
                            ci.manually_closed_at IS NULL
                            OR cm.created_at > ci.manually_closed_at::timestamptz
                        )
                  )
                """),
                (now, user_id),
            )
        return int(result.rowcount or 0)

    def move_chat_to_new_if_buyer_replied(
        self,
        *,
        user_id: int,
        conversation_uid: str,
    ) -> bool:
        """If the newest inbound message in conversation_messages is more recent
        than last_sent_at, clear last_sent_at so the chat moves to 'New' bucket.

        Called after saving WB chat history messages to ensure the bucket
        reflects the true state: buyer replied after seller → needs response.
        Returns True if last_sent_at was cleared (chat moved to New).
        """
        now = _utc_now()
        with self._connect() as conn:
            # Find the newest inbound (buyer) message
            row = conn.execute(
                self._sql("""
                SELECT created_at FROM conversation_messages
                WHERE user_id = ? AND conversation_uid = ? AND direction = 'inbound'
                  AND created_at IS NOT NULL AND created_at != ''
                ORDER BY created_at DESC LIMIT 1
                """),
                (user_id, conversation_uid),
            ).fetchone()
            if row is None:
                return False
            newest_inbound = str(dict(row).get("created_at") or "").strip()
            if not newest_inbound:
                return False
            # Clear last_sent_at only if buyer's message is newer than our last reply
            result = conn.execute(
                self._sql("""
                UPDATE conversation_items
                SET last_sent_at = NULL, updated_at = ?
                WHERE user_id = ? AND conversation_uid = ?
                  AND last_sent_at IS NOT NULL
                  AND last_sent_at::text < ?
                """),
                (now, user_id, conversation_uid, newest_inbound),
            )
            return bool(result.rowcount)

    def upsert_conversation_outbound_message(
        self,
        *,
        user_id: int,
        conversation_uid: str,
        message_text: str,
        operator_name: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        clean_text = str(message_text or "").strip()
        clean_operator = str(operator_name or "").strip()
        clean_key = str(idempotency_key or "").strip()
        if not clean_text:
            raise ValueError("message_text is required")
        if not clean_key:
            raise ValueError("idempotency_key is required")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_messages (
                    conversation_uid, user_id, direction, message_text, operator_name,
                    send_status, idempotency_key, created_at
                )
                VALUES (?, ?, 'outbound', ?, ?, 'pending', ?, ?)
                ON CONFLICT(user_id, conversation_uid, idempotency_key) DO NOTHING
                """,
                (conversation_uid, user_id, clean_text, clean_operator, clean_key, now),
            )
            row = conn.execute(
                """
                SELECT *
                FROM conversation_messages
                WHERE user_id = ? AND conversation_uid = ? AND idempotency_key = ?
                """,
                (user_id, conversation_uid, clean_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("Conversation message upsert failed")
        return self._row_to_dict(row)

    def mark_conversation_message_send_success(
        self,
        *,
        user_id: int,
        conversation_uid: str,
        idempotency_key: str,
        external_message_id: str | None = None,
    ) -> bool:
        now = _utc_now()
        clean_key = str(idempotency_key or "").strip()
        if not clean_key:
            return False
        with self._connect() as conn:
            message_result = conn.execute(
                """
                UPDATE conversation_messages
                SET send_status = 'sent',
                    send_error_code = NULL,
                    send_error_message = NULL,
                    external_message_id = COALESCE(?, external_message_id)
                WHERE user_id = ? AND conversation_uid = ? AND idempotency_key = ?
                """,
                (external_message_id, user_id, conversation_uid, clean_key),
            )
            conn.execute(
                """
                UPDATE conversation_items
                SET status = 'waiting',
                    unread_count = 0,
                    send_error_code = NULL,
                    send_error_message = NULL,
                    send_attempts = 0,
                    last_send_attempt_at = NULL,
                    last_sent_at = ?,
                    last_message_at = ?,
                    updated_at = ?
                WHERE user_id = ? AND conversation_uid = ?
                """,
                (now, now, now, user_id, conversation_uid),
            )
        return bool(message_result.rowcount)

    def mark_conversation_message_send_failure(
        self,
        *,
        user_id: int,
        conversation_uid: str,
        idempotency_key: str,
        error_code: str | None,
        error_message: str | None,
    ) -> bool:
        now = _utc_now()
        clean_key = str(idempotency_key or "").strip()
        if not clean_key:
            return False
        code = str(error_code or "").strip() or None
        message = str(error_message or "").strip() or None
        with self._connect() as conn:
            message_result = conn.execute(
                """
                UPDATE conversation_messages
                SET send_status = 'failed',
                    send_error_code = ?,
                    send_error_message = ?
                WHERE user_id = ? AND conversation_uid = ? AND idempotency_key = ?
                """,
                (code, message, user_id, conversation_uid, clean_key),
            )
            conn.execute(
                """
                UPDATE conversation_items
                SET status = 'open',
                    send_error_code = ?,
                    send_error_message = ?,
                    send_attempts = COALESCE(send_attempts, 0) + 1,
                    last_send_attempt_at = ?,
                    updated_at = ?
                WHERE user_id = ? AND conversation_uid = ?
                """,
                (code, message, now, now, user_id, conversation_uid),
            )
        return bool(message_result.rowcount)

    def get_review(self, *, user_id: int, review_uid: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM review_items
                WHERE user_id = ? AND review_uid = ?
                """,
                (user_id, review_uid),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def mark_review_send_error(
        self,
        *,
        user_id: int,
        review_uid: str,
        error_message: str,
        auto_reply: str | None = None,
    ) -> None:
        """Record a failed auto-reply attempt. Keeps auto_reply so retry can use it."""
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                self._sql("""
                UPDATE review_items
                SET send_error_message = ?,
                    send_attempts = COALESCE(send_attempts, 0) + 1,
                    status = 'queued_for_operator',
                    auto_reply = CASE WHEN ? IS NOT NULL THEN ? ELSE auto_reply END,
                    updated_at = ?
                WHERE user_id = ? AND review_uid = ?
                """),
                (error_message, auto_reply, auto_reply, now, user_id, review_uid),
            )

    def clear_review_send_error(self, *, user_id: int, review_uid: str) -> None:
        """Clear error state after successful send."""
        with self._connect() as conn:
            conn.execute(
                self._sql("""
                UPDATE review_items
                SET send_error_message = NULL,
                    send_attempts = 0,
                    updated_at = ?
                WHERE user_id = ? AND review_uid = ?
                """),
                (_utc_now(), user_id, review_uid),
            )

    def get_pending_retry_reviews(
        self, *, user_id: int, max_attempts: int = 3
    ) -> list[dict[str, Any]]:
        """Return reviews that failed auto-reply and should be retried (attempts < max)."""
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("""
                SELECT *
                FROM review_items
                WHERE user_id = ?
                  AND status = 'queued_for_operator'
                  AND auto_reply IS NOT NULL
                  AND auto_reply != ''
                  AND COALESCE(send_attempts, 0) > 0
                  AND COALESCE(send_attempts, 0) < ?
                ORDER BY updated_at ASC
                LIMIT 50
                """),
                (user_id, max_attempts),
            ).fetchall()
        items = []
        for row in rows:
            data = dict(row)
            if "metadata_json" in data:
                data["metadata"] = _json_load(data.pop("metadata_json"), {})
            items.append(data)
        return items

    def get_existing_classifications(self, *, user_id: int) -> dict[str, tuple[str, str]]:
        """Return {review_uid: (category/group, classified_subgroup)} for all
        already-classified reviews of this user. Used to skip redundant Yandex calls.
        Uses the `category` column (always populated) and json_extract on metadata_json
        for the subgroup."""
        sub_expr = "metadata_json::jsonb->>'classified_subgroup'"

        sql = self._sql(f"""
            SELECT review_uid,
                   category AS grp,
                   {sub_expr} AS sub
            FROM review_items
            WHERE user_id = ?
              AND category IS NOT NULL
              AND category != ''
        """)
        with self._connect() as conn:
            rows = conn.execute(sql, (user_id,)).fetchall()
        result: dict[str, tuple[str, str]] = {}
        for row in rows:
            d = self._row_to_dict(row)
            uid = str(d.get("review_uid") or "").strip()
            grp = str(d.get("grp") or "").strip()
            sub = str(d.get("sub") or "").strip()
            if uid and grp:
                result[uid] = (grp, sub)
        return result

    # ── Product catalog methods ───────────────────────────────────────────────

    def list_product_catalog(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("SELECT * FROM product_catalog WHERE user_id = ? ORDER BY product_name ASC, wb_article ASC"),
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def upsert_product_catalog_item(
        self,
        *,
        user_id: int,
        wb_article: str,
        product_name: str = "",
        ozon_article: str = "",
    ) -> dict[str, Any]:
        now = _utc_now()
        wb_article = str(wb_article or "").strip()
        if not wb_article:
            raise ValueError("wb_article is required")
        with self._connect() as conn:
            conn.execute(
                self._sql("""
                INSERT INTO product_catalog (user_id, product_name, wb_article, ozon_article, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, wb_article) DO UPDATE SET
                    product_name = EXCLUDED.product_name,
                    ozon_article = EXCLUDED.ozon_article,
                    updated_at   = EXCLUDED.updated_at
                """),
                (user_id, str(product_name or "").strip(), wb_article,
                 str(ozon_article or "").strip(), now, now),
            )
            row = conn.execute(
                self._sql("SELECT * FROM product_catalog WHERE user_id = ? AND wb_article = ?"),
                (user_id, wb_article),
            ).fetchone()
        return self._row_to_dict(row) if row else {}

    def delete_product_catalog_item(self, *, user_id: int, item_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM product_catalog WHERE id = ? AND user_id = ?"),
                (item_id, user_id),
            )
        return bool(result.rowcount)

    def delete_all_product_catalog(self, *, user_id: int) -> int:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM product_catalog WHERE user_id = ?"),
                (user_id,),
            )
        return int(result.rowcount or 0)

    def get_product_catalog_map(self, *, user_id: int) -> dict[str, dict[str, str]]:
        """Return {wb_article: {product_name, ozon_article}} for fast lookup by WB article."""
        rows = self.list_product_catalog(user_id=user_id)
        return {
            r["wb_article"]: {"product_name": r["product_name"], "ozon_article": r["ozon_article"]}
            for r in rows
            if r.get("wb_article")
        }

    def get_product_catalog_map_ozon(self, *, user_id: int) -> dict[str, dict[str, str]]:
        """Return {ozon_article: {product_name, wb_article}} for fast lookup by Ozon article."""
        rows = self.list_product_catalog(user_id=user_id)
        return {
            r["ozon_article"]: {"product_name": r["product_name"], "wb_article": r["wb_article"]}
            for r in rows
            if r.get("ozon_article")
        }

    # ── Stock module repository methods ──────────────────────────────────────

    def create_stock_source(
        self,
        *,
        user_id: int,
        marketplace: str,
        account_name: str,
        api_url: str = "",
        api_key: str = "",
        extra: dict | None = None,
        interval_hours: int = 24,
        retention_days: int = 30,
    ) -> dict[str, Any]:
        now = _utc_now()
        encrypted = str(api_key or "")  # stored as-is (same as marketplace_accounts)
        with self._connect() as conn:
            source_id = self._insert_and_get_id(
                conn,
                self._sql("""
                INSERT INTO stock_sources
                (user_id, marketplace, account_name, api_url, api_key_encrypted,
                 extra_json, interval_hours, retention_days, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """),
                (user_id, marketplace, account_name, api_url, encrypted,
                 self._json_param(extra or {}), interval_hours, retention_days,
                 self._bool_db(True), now, now),
            )
            row = conn.execute("SELECT * FROM stock_sources WHERE id = ?", (source_id,)).fetchone()
        return self._stock_source_to_dict(row)

    def list_stock_sources(self, *, user_id: int, include_secrets: bool = False) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM stock_sources WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
        return [self._stock_source_to_dict(r, include_secrets=include_secrets) for r in rows]

    def get_stock_source(self, *, user_id: int, source_id: int, include_secrets: bool = False) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stock_sources WHERE user_id = ? AND id = ?",
                (user_id, source_id),
            ).fetchone()
        return self._stock_source_to_dict(row, include_secrets=include_secrets) if row else None

    def update_stock_source(
        self,
        *,
        user_id: int,
        source_id: int,
        account_name: str | None = None,
        api_key: str | None = None,
        interval_hours: int | None = None,
        retention_days: int | None = None,
        is_active: bool | None = None,
        last_synced_at: str | None = None,
        extra: dict | None = None,
    ) -> bool:
        now = _utc_now()
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]
        if account_name is not None:
            sets.append("account_name = ?"); params.append(account_name)
        if api_key is not None:
            sets.append("api_key_encrypted = ?"); params.append(str(api_key or ""))
        if interval_hours is not None:
            sets.append("interval_hours = ?"); params.append(interval_hours)
        if retention_days is not None:
            sets.append("retention_days = ?"); params.append(retention_days)
        if is_active is not None:
            sets.append(f"is_active = ?"); params.append(self._bool_db(is_active))
        if last_synced_at is not None:
            sets.append("last_synced_at = ?"); params.append(last_synced_at)
        if extra is not None:
            sets.append("extra_json = ?"); params.append(self._json_param(extra))
        params.extend([user_id, source_id])
        with self._connect() as conn:
            result = conn.execute(
                self._sql(f"UPDATE stock_sources SET {', '.join(sets)} WHERE user_id = ? AND id = ?"),
                tuple(params),
            )
        return result.rowcount > 0

    def delete_stock_source(self, *, user_id: int, source_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM stock_sources WHERE user_id = ? AND id = ?",
                (user_id, source_id),
            )
        return result.rowcount > 0

    def create_stock_report(
        self,
        *,
        source_id: int,
        user_id: int,
        downloaded_at: str,
        file_path: str = "",
        file_size: int = 0,
        rows_count: int = 0,
        status: str = "ok",
        error_message: str | None = None,
    ) -> int:
        now = _utc_now()
        with self._connect() as conn:
            report_id = self._insert_and_get_id(
                conn,
                self._sql("""
                INSERT INTO stock_reports
                (source_id, user_id, downloaded_at, file_path, file_size, rows_count, status, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """),
                (source_id, user_id, downloaded_at, file_path, file_size, rows_count, status, error_message, now),
            )
        return report_id

    def list_stock_reports(self, *, user_id: int, source_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if source_id:
            clauses.append("source_id = ?"); params.append(source_id)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM stock_reports WHERE {' AND '.join(clauses)} ORDER BY downloaded_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def bulk_insert_stock_data(
        self,
        *,
        source_id: int,
        report_id: int,
        user_id: int,
        report_date: str,
        rows: list[dict[str, Any]],
    ) -> int:
        if not rows:
            return 0
        now = _utc_now()
        with self._connect() as conn:
            inserted = 0
            for row in rows:
                conn.execute(
                    self._sql("""
                    INSERT INTO stock_data
                    (source_id, report_id, user_id, report_date, wb_article, seller_article,
                     barcode, warehouse_name, current_stock, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """),
                    (source_id, report_id, user_id, report_date,
                     str(row.get("wb_article") or ""),
                     str(row.get("seller_article") or ""),
                     str(row.get("barcode") or ""),
                     str(row.get("warehouse_name") or ""),
                     int(row.get("current_stock") or 0),
                     now),
                )
                inserted += 1
        return inserted

    def get_stock_data_dates(self, *, user_id: int, source_id: int) -> list[str]:
        """Return sorted list of distinct report_dates for this source."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT report_date FROM stock_data WHERE user_id = ? AND source_id = ? ORDER BY report_date ASC",
                (user_id, source_id),
            ).fetchall()
        return [str(r["report_date"] or "") for r in rows if r["report_date"]]

    def get_stock_data_pivot(self, *, user_id: int, source_id: int) -> list[dict[str, Any]]:
        """Return stock data grouped by (wb_article, seller_article, warehouse_name)
        with each report_date as a key in the result dict."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT wb_article, seller_article, barcode, warehouse_name, report_date, current_stock
                FROM stock_data
                WHERE user_id = ? AND source_id = ?
                ORDER BY wb_article ASC, warehouse_name ASC, report_date ASC
                """,
                (user_id, source_id),
            ).fetchall()
        pivot: dict[tuple, dict[str, Any]] = {}
        for r in rows:
            key = (str(r["wb_article"] or ""), str(r["seller_article"] or ""),
                   str(r["barcode"] or ""), str(r["warehouse_name"] or ""))
            if key not in pivot:
                pivot[key] = {
                    "wb_article": key[0],
                    "seller_article": key[1],
                    "barcode": key[2],
                    "warehouse_name": key[3],
                    "dates": {},
                }
            pivot[key]["dates"][str(r["report_date"])] = int(r["current_stock"] or 0)
        return list(pivot.values())

    def purge_old_stock_data(self, *, user_id: int, source_id: int, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = f"NOW() - INTERVAL '{retention_days} days'"
        sql = f"DELETE FROM stock_data WHERE user_id = %s AND source_id = %s AND created_at::timestamp < {cutoff}"

        with self._connect() as conn:
            result = conn.execute(self._sql(sql), (user_id, source_id))

        return int(result.rowcount or 0)

    def delete_all_stock_reports(self, *, user_id: int, source_id: int | None = None) -> int:
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if source_id:
            clauses.append("source_id = ?"); params.append(source_id)
        with self._connect() as conn:
            conn.execute(f"DELETE FROM stock_data WHERE {' AND '.join(clauses)}", tuple(params))
            result = conn.execute(f"DELETE FROM stock_reports WHERE {' AND '.join(clauses)}", tuple(params))
        return int(result.rowcount or 0)

    @staticmethod
    def _stock_source_to_dict(row, *, include_secrets: bool = False) -> dict[str, Any]:
        if row is None:
            return {}
        data = dict(row)
        if "extra_json" in data:
            data["extra"] = _json_load(data.pop("extra_json"), {})
        # api_key_encrypted stores the key (no actual encryption for now — same pattern as marketplace_accounts)
        if "api_key_encrypted" in data:
            raw = str(data["api_key_encrypted"] or "")
            if include_secrets:
                data["api_key"] = raw
            else:
                data["api_key"] = (raw[:2] + "****") if len(raw) > 4 else ("****" if raw else "")
            del data["api_key_encrypted"]
        if "is_active" in data:
            data["is_active"] = bool(data["is_active"])
        return data

    def update_review_manual_reply(
        self,
        *,
        user_id: int,
        review_uid: str,
        operator_name: str,
        reply_text: str,
    ) -> bool:
        now = _utc_now()
        with self._connect() as conn:
            result = conn.execute(
                self._sql("""
                UPDATE review_items
                SET manual_reply = ?, operator_name = ?, status = 'answered_manual', updated_at = ?
                WHERE user_id = ? AND review_uid = ?
                """),
                (reply_text, operator_name, now, user_id, review_uid),
            )
        return result.rowcount > 0

    def get_conversation(self, *, user_id: int, conversation_uid: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM conversation_items
                WHERE user_id = ? AND conversation_uid = ?
                """,
                (user_id, conversation_uid),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        raw = data.pop("metadata_json", "{}")
        data["metadata"] = _json_load(raw, {})
        return data

    def mark_manual_queue(self, *, user_id: int, review_uid: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE review_items
                SET status = 'queued_for_operator', updated_at = ?
                WHERE user_id = ? AND review_uid = ?
                """,
                (_utc_now(), user_id, review_uid),
            )
            return result.rowcount > 0

    def mark_auto_replied(self, *, user_id: int, review_uid: str, response_text: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE review_items
                SET status = 'answered_auto', auto_reply = ?, updated_at = ?
                WHERE user_id = ? AND review_uid = ?
                """,
                (response_text, _utc_now(), user_id, review_uid),
            )
            return result.rowcount > 0

    def mark_manual_replied(self, *, user_id: int, review_uid: str, operator_name: str, response_text: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE review_items
                SET status = 'answered_manual', manual_reply = ?, operator_name = ?, updated_at = ?
                WHERE user_id = ? AND review_uid = ?
                """,
                (response_text, operator_name, _utc_now(), user_id, review_uid),
            )
            return result.rowcount > 0

    def log_review_action(
        self,
        *,
        user_id: int,
        review_uid: str | None,
        action_type: str,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO review_actions (user_id, review_uid, action_type, actor, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, review_uid, action_type, actor, self._json_param(details or {}), _utc_now()),
            )

    def log_ai_request(
        self,
        *,
        user_id: int,
        prompt_system: str,
        prompt_user: str,
        response_text: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model_uri: str = "",
        review_rating: int | None = None,
        classified_group: str = "",
        classified_subgroup: str = "",
    ) -> None:
        """Log one Yandex GPT request for debug purposes. Kept for 1 day only."""
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                self._sql("""
                INSERT INTO ai_request_log
                (user_id, created_at, prompt_system, prompt_user, response_text,
                 input_tokens, output_tokens, model_uri, review_rating,
                 classified_group, classified_subgroup)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """),
                (user_id, now, prompt_system[:4000], prompt_user[:2000], response_text[:1000],
                 input_tokens, output_tokens, model_uri, review_rating,
                 classified_group, classified_subgroup),
            )

    def list_ai_request_logs(self, *, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("""
                SELECT id, created_at, prompt_system, prompt_user, response_text,
                       input_tokens, output_tokens, model_uri, review_rating,
                       classified_group, classified_subgroup
                FROM ai_request_log
                WHERE user_id = ?
                ORDER BY created_at DESC LIMIT ?
                """),
                (user_id, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def purge_old_ai_usage_logs(self, *, keep_days: int = 30) -> int:
        """Delete ai_usage_log entries older than keep_days days."""
        with self._connect() as conn:
            result = conn.execute(
                self._sql(
                    f"DELETE FROM ai_usage_log WHERE log_date < (NOW() - INTERVAL '{keep_days} days')::date::text"
                )
            )
        return int(result.rowcount or 0)

    def purge_old_ai_request_logs(self, *, user_id: Optional[int] = None) -> int:
        """Delete AI request logs older than 1 day."""
        clause = "created_at < NOW() - INTERVAL '1 day'"

        if user_id:
            where = f"WHERE user_id = %s AND {clause}"
            params: tuple = (user_id,)
        else:
            where = f"WHERE {clause}"
            params = ()
        with self._connect() as conn:
            result = conn.execute(self._sql(f"DELETE FROM ai_request_log {where}"), params)
        return int(result.rowcount or 0)

    def log_ai_usage(
        self,
        *,
        user_id: int,
        input_tokens: int,
        output_tokens: int,
        model_uri: str = "",
    ) -> None:
        """Record one Yandex GPT call with token counts."""
        now = _utc_now()
        log_date = now[:10]  # YYYY-MM-DD
        with self._connect() as conn:
            conn.execute(
                self._sql("""
                INSERT INTO ai_usage_log (user_id, log_date, input_tokens, output_tokens, requests, model_uri, created_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """),
                (user_id, log_date, input_tokens, output_tokens, model_uri, now),
            )

    def get_ai_usage_stats(self, *, user_id: int, days: int = 30) -> list[dict[str, Any]]:
        """Return daily AI usage aggregated over the last N days."""
        cutoff = f"(NOW() - INTERVAL '{days} days')::date"
        sql = f"""
            SELECT log_date, SUM(requests) AS requests,
                   SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                   MAX(model_uri) AS model_uri
            FROM ai_usage_log
            WHERE user_id = %s AND log_date >= {cutoff}::text
            GROUP BY log_date ORDER BY log_date DESC LIMIT 60
        """

        with self._connect() as conn:
            rows = conn.execute(self._sql(sql), (user_id,)).fetchall()

        return [self._row_to_dict(r) for r in rows]

    def purge_sync_action_logs(self) -> int:
        """Delete all bulk sync action log entries from review_actions.

        These have no operational value and should never grow the table.
        """
        with self._connect() as conn:
            result = conn.execute(
                self._sql(
                    "DELETE FROM review_actions WHERE action_type IN ('sync_review', 'sync_conversation', 'sync_conversation')"
                )
            )
        return int(result.rowcount or 0)

    def purge_old_review_actions(self, *, keep_days: int = 90) -> int:
        """Delete review_actions older than keep_days to prevent unbounded growth.

        Keeps the most recent 90 days of action history by default.
        Should be called periodically (e.g. on server startup) to avoid
        the table growing to millions of rows with 200k+ reviews/syncs.
        """
        sql = self._sql(
            "DELETE FROM review_actions WHERE created_at < NOW() - INTERVAL '? days'"
        ).replace("'? days'", f"'{keep_days} days'")
        with self._connect() as conn:
            result = conn.execute(sql)

        return int(result.rowcount or 0)

    def count_recent_actions(self, *, user_id: int | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        query = "SELECT COUNT(*) AS c FROM review_actions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._connect() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return int(row["c"]) if row else 0

    def list_recent_actions(
        self,
        *,
        user_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
        action_type: str | None = None,
        actor: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        search: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        filter_params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            filter_params.append(user_id)
        normalized_action_type = str(action_type or "").strip()
        if normalized_action_type:
            clauses.append("action_type = ?")
            filter_params.append(normalized_action_type)
        normalized_actor = str(actor or "").strip()
        if normalized_actor:
            clauses.append("LOWER(actor) LIKE ?")
            filter_params.append(f"%{normalized_actor.lower()}%")
        if date_from:
            clauses.append("created_at::date >= ?::date")

            filter_params.append(date_from)
        if date_to:
            clauses.append("created_at::date <= ?::date")

            filter_params.append(date_to)
        normalized_search = str(search or "").strip().lower()
        if normalized_search:
            details_expr = "COALESCE(details_json::text, '')"
            clauses.append(
                f"""(
                    LOWER(COALESCE(actor, '')) LIKE ?
                    OR LOWER(COALESCE(review_uid, '')) LIKE ?
                    OR LOWER(COALESCE(action_type, '')) LIKE ?
                    OR LOWER({details_expr}) LIKE ?
                )"""
            )
            search_value = f"%{normalized_search}%"
            filter_params.extend([search_value, search_value, search_value, search_value])
        query = "SELECT * FROM review_actions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        query_params = [*filter_params, max(limit, 1), max(offset, 0)]

        with self._connect() as conn:
            count_query = "SELECT COUNT(*) AS c FROM review_actions"
            if clauses:
                count_query += " WHERE " + " AND ".join(clauses)
            total_row = conn.execute(count_query, tuple(filter_params)).fetchone()
            rows = conn.execute(query, tuple(query_params)).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            data = self._row_to_dict(row)
            raw = data.pop("details_json", "{}")
            data["details"] = _json_load(raw, {})
            items.append(data)
        total = int(total_row["c"]) if total_row else 0
        return items, total

    def list_action_filter_options(self, *, user_id: int | None = None) -> dict[str, list[str]]:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        where_sql = ""
        if clauses:
            where_sql = " WHERE " + " AND ".join(clauses)
        with self._connect() as conn:
            action_type_rows = conn.execute(
                f"""
                SELECT DISTINCT action_type
                FROM review_actions
                {where_sql}
                ORDER BY action_type ASC
                """,
                tuple(params),
            ).fetchall()
            actor_rows = conn.execute(
                f"""
                SELECT DISTINCT actor
                FROM review_actions
                {where_sql}
                ORDER BY actor ASC
                """,
                tuple(params),
            ).fetchall()
        return {
            "action_types": [
                str(row["action_type"])
                for row in action_type_rows
                if row["action_type"] is not None and str(row["action_type"]).strip()
            ],
            "actors": [str(row["actor"]) for row in actor_rows if row["actor"] is not None and str(row["actor"]).strip()],
        }

    def get_sla_metrics(self, *, user_id: int | None = None) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        where_and = f"{where} AND" if where else "WHERE"
        avg_expr = "AVG(EXTRACT(EPOCH FROM (updated_at - created_at)) / 60.0)"
        overdue_expr = "EXTRACT(EPOCH FROM (NOW() - updated_at)) / 3600.0 > 24"

        with self._connect() as conn:
            total_row = conn.execute(f"SELECT COUNT(*) AS c FROM review_items {where}", tuple(params)).fetchone()
            statuses = conn.execute(
                f"""
                SELECT status, COUNT(*) AS c
                FROM review_items
                {where}
                GROUP BY status
                """,
                tuple(params),
            ).fetchall()
            avg_row = conn.execute(
                f"""
                SELECT {avg_expr} AS avg_minutes
                FROM review_items
                {where_and} status IN ('answered_auto', 'answered_manual')
                """,
                tuple(params),
            ).fetchone()
            overdue_row = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM review_items
                {where_and}
                    status = 'queued_for_operator'
                    AND {overdue_expr}
                """,
                tuple(params),
            ).fetchone()

        status_map = {str(row["status"]): int(row["c"]) for row in statuses}
        avg_minutes = float(avg_row["avg_minutes"]) if avg_row and avg_row["avg_minutes"] is not None else 0.0
        return {
            "total_reviews": int(total_row["c"]) if total_row else 0,
            "status_counts": status_map,
            "avg_first_response_minutes": round(avg_minutes, 2),
            "overdue_manual_queue_24h": int(overdue_row["c"]) if overdue_row else 0,
        }

    def get_user_analytics(
        self,
        *,
        user_id: int,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        source_clause = " AND source = ?" if source else ""
        source_params: list[Any] = [source] if source else []
        date_clause = ""
        date_params: list[Any] = []
        if date_from:
            date_clause += " AND created_at::date >= ?::date"
            date_params.append(date_from)
        if date_to:
            date_clause += " AND created_at::date <= ?::date"
            date_params.append(date_to)
        filter_clause = source_clause + date_clause
        filter_params = source_params + date_params

        with self._connect() as conn:
            totals = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status IN ('answered_auto', 'answered_manual', 'ignored') THEN 1 ELSE 0 END) AS processed,
                    SUM(CASE WHEN sentiment_label = 'positive' THEN 1 ELSE 0 END) AS positive_count,
                    SUM(CASE WHEN sentiment_label = 'negative' THEN 1 ELSE 0 END) AS negative_count,
                    SUM(CASE WHEN rating >= 4 THEN 1 ELSE 0 END) AS high_rating_count,
                    SUM(CASE WHEN rating <= 3 THEN 1 ELSE 0 END) AS low_rating_count
                FROM review_items
                WHERE user_id = ?{filter_clause}
                """,
                (user_id, *filter_params),
            ).fetchone()

            conversation_totals = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_items,
                    SUM(CASE WHEN kind = 'question' THEN 1 ELSE 0 END) AS questions_count,
                    SUM(CASE WHEN kind = 'chat' THEN 1 ELSE 0 END) AS chats_count
                FROM conversation_items
                WHERE user_id = ?{filter_clause}
                """,
                (user_id, *filter_params),
            ).fetchone()

            # Rating breakdown (1–5 stars)
            rating_rows = conn.execute(
                f"""
                SELECT rating, COUNT(*) AS cnt
                FROM review_items
                WHERE user_id = ?{filter_clause}
                  AND rating IS NOT NULL
                GROUP BY rating
                ORDER BY rating
                """,
                (user_id, *filter_params),
            ).fetchall()

            # Category breakdown
            cat_rows = conn.execute(
                f"""
                SELECT category, COUNT(*) AS cnt
                FROM review_items
                WHERE user_id = ?{filter_clause}
                  AND category IS NOT NULL AND TRIM(category) != ''
                GROUP BY category
                ORDER BY cnt DESC
                """,
                (user_id, *filter_params),
            ).fetchall()

            # Per-source breakdown (respects date filter, always covers all sources)
            source_rows = conn.execute(
                f"""
                SELECT source,
                    COUNT(*) AS total,
                    SUM(CASE WHEN status IN ('answered_auto', 'answered_manual', 'ignored') THEN 1 ELSE 0 END) AS processed,
                    SUM(CASE WHEN rating >= 4 THEN 1 ELSE 0 END) AS positive_count,
                    SUM(CASE WHEN rating <= 3 THEN 1 ELSE 0 END) AS negative_count
                FROM review_items
                WHERE user_id = ?{date_clause}
                GROUP BY source
                ORDER BY total DESC
                """,
                (user_id, *date_params),
            ).fetchall()

        total_reviews = int(totals["total"] or 0) if totals else 0
        processed_reviews = int(totals["processed"] or 0) if totals else 0
        positive_count = int(totals["positive_count"] or 0) if totals else 0
        negative_count = int(totals["negative_count"] or 0) if totals else 0
        high_rating = int(totals["high_rating_count"] or 0) if totals else 0
        low_rating = int(totals["low_rating_count"] or 0) if totals else 0

        positive_percent = round((positive_count / total_reviews) * 100, 1) if total_reviews else 0.0
        negative_percent = round((negative_count / total_reviews) * 100, 1) if total_reviews else 0.0
        processed_percent = round((processed_reviews / total_reviews) * 100, 1) if total_reviews else 0.0

        by_rating = {int(r["rating"]): int(r["cnt"] or 0) for r in rating_rows if r["rating"]}
        by_category = [
            {"category": str(r["category"] or ""), "count": int(r["cnt"] or 0)}
            for r in cat_rows
        ]
        by_source = [
            {
                "source": str(r["source"] or ""),
                "total": int(r["total"] or 0),
                "processed": int(r["processed"] or 0),
                "positive": int(r["positive_count"] or 0),
                "negative": int(r["negative_count"] or 0),
            }
            for r in source_rows
        ]

        return {
            "total_reviews": total_reviews,
            "processed_reviews": processed_reviews,
            "processed_percent": processed_percent,
            "positive_count": positive_count,
            "negative_count": negative_count,
            "positive_percent": positive_percent,
            "negative_percent": negative_percent,
            "high_rating_count": high_rating,
            "low_rating_count": low_rating,
            "conversation_total": int(conversation_totals["total_items"] or 0) if conversation_totals else 0,
            "questions_count": int(conversation_totals["questions_count"] or 0) if conversation_totals else 0,
            "chats_count": int(conversation_totals["chats_count"] or 0) if conversation_totals else 0,
            "by_rating": by_rating,
            "by_category": by_category,
            "by_source": by_source,
        }

    # ── Analytics: trend & export ────────────────────────────────────────────

    def get_analytics_trend(
        self,
        *,
        user_id: int,
        source: str | None = None,
        granularity: str = "week",
    ) -> list[dict[str, Any]]:
        """Return time-series aggregation for the trend chart.
        granularity: 'week' | 'month'
        Returns up to 52 data points ordered by period.
        """
        source_clause = " AND source = ?" if source else ""
        source_params: list[Any] = [source] if source else []

        # Repository is always PostgreSQL — use native date functions
        limit_days = 52 * 7 if granularity == "week" else 24 * 30
        cutoff_clause = f" AND created_at >= NOW() - INTERVAL '{limit_days} days'"

        query = f"""
            SELECT
                TO_CHAR(DATE_TRUNC('{granularity}', created_at::date), 'YYYY-MM-DD') AS period,
                COUNT(*) AS total,
                ROUND(
                    CAST(AVG(CASE WHEN rating IS NOT NULL THEN rating END) AS numeric), 2
                ) AS avg_rating,
                ROUND(
                    100.0 * SUM(CASE WHEN status IN ('answered_auto','answered_manual','ignored') THEN 1 ELSE 0 END)
                    / NULLIF(COUNT(*), 0)::numeric, 1
                ) AS processed_pct
            FROM review_items
            WHERE user_id = ?{source_clause}{cutoff_clause}
            GROUP BY DATE_TRUNC('{granularity}', created_at::date)
            ORDER BY DATE_TRUNC('{granularity}', created_at::date)
        """

        with self._connect() as conn:
            rows = conn.execute(
                self._sql(query),
                (user_id, *source_params),
            ).fetchall()

        return [
            {
                "period": str(r["period"] or ""),
                "total": int(r["total"] or 0),
                "avg_rating": float(r["avg_rating"]) if r["avg_rating"] is not None else None,
                "processed_pct": float(r["processed_pct"]) if r["processed_pct"] is not None else 0.0,
            }
            for r in rows
        ]

    def list_reviews_for_export(
        self,
        *,
        user_id: int,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return review rows for Excel export."""
        conditions = ["user_id = ?"]
        params: list[Any] = [user_id]
        if source:
            conditions.append("source = ?")
            params.append(source)
        if date_from:
            conditions.append("created_at::date >= ?::date")
            params.append(date_from)
        if date_to:
            conditions.append("created_at::date <= ?::date")
            params.append(date_to)
        where = " AND ".join(conditions)
        query = f"""
            SELECT created_at, source, rating, category, text,
                   COALESCE(manual_reply, auto_reply) AS reply_text,
                   COALESCE(
                       NULLIF(metadata_json->'raw'->'productDetails'->>'vendorCode', ''),
                       NULLIF(metadata_json->'raw'->'productDetails'->>'supplierArticle', ''),
                       NULLIF(metadata_json->'raw'->'productDetails'->>'offerId', ''),
                       NULLIF(metadata_json->'raw'->>'vendorCode', ''),
                       NULLIF(metadata_json->'raw'->>'vendor_code', ''),
                       NULLIF(metadata_json->'raw'->>'offerId', ''),
                       NULLIF(metadata_json->'raw'->>'nmId', ''),
                       ''
                   ) AS article
            FROM review_items
            WHERE {where}
            ORDER BY created_at ASC
        """
        with self._connect() as conn:
            rows = conn.execute(self._sql(query), tuple(params)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Contest analysis ──────────────────────────────────────────────────────

    def _ensure_contest_tables(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS contest_analysis_runs (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    date_from TEXT NOT NULL DEFAULT '',
                    date_to TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    total INTEGER NOT NULL DEFAULT 0,
                    processed INTEGER NOT NULL DEFAULT 0,
                    violations_found INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS contest_analysis_results (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    run_id BIGINT NOT NULL,
                    review_uid TEXT NOT NULL,
                    violations TEXT NOT NULL DEFAULT '[]',
                    can_contest BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)
            conn.execute(
                self._sql("CREATE INDEX IF NOT EXISTS idx_contest_runs_user ON contest_analysis_runs(user_id)")
            )
            conn.execute(
                self._sql("CREATE INDEX IF NOT EXISTS idx_contest_results_run ON contest_analysis_results(run_id)")
            )

    def find_cached_contest_run(
        self, *, user_id: int, source: str, date_from: str, date_to: str
    ) -> dict[str, Any] | None:
        """Return a completed run for the same params, if one exists."""
        self._ensure_contest_tables()
        with self._connect() as conn:
            row = conn.execute(
                self._sql(
                    "SELECT * FROM contest_analysis_runs "
                    "WHERE user_id=? AND source=? AND date_from=? AND date_to=? AND status='completed' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                (user_id, source or "", date_from or "", date_to or ""),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def create_contest_run(
        self, *, user_id: int, source: str, date_from: str, date_to: str, total: int
    ) -> int:
        self._ensure_contest_tables()
        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                self._sql(
                    "INSERT INTO contest_analysis_runs "
                    "(user_id, source, date_from, date_to, status, total, processed, violations_found, created_at) "
                    "VALUES (?,?,?,?,?,?,0,0,?) RETURNING id"
                ),
                (user_id, source or "", date_from or "", date_to or "", "running", total, now),
            ).fetchone()
        return int(self._row_to_dict(row)["id"]) if row else 0

    def get_contest_run(self, *, run_id: int, user_id: int) -> dict[str, Any] | None:
        self._ensure_contest_tables()
        with self._connect() as conn:
            row = conn.execute(
                self._sql("SELECT * FROM contest_analysis_runs WHERE id=? AND user_id=?"),
                (run_id, user_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def update_contest_run_progress(
        self, *, run_id: int, processed: int, violations_found: int, status: str = "running", error: str | None = None
    ) -> None:
        now = _utc_now()
        completed_at = now if status == "completed" else None
        with self._connect() as conn:
            conn.execute(
                self._sql(
                    "UPDATE contest_analysis_runs SET processed=?, violations_found=?, status=?, "
                    "error=?, completed_at=? WHERE id=?"
                ),
                (processed, violations_found, status, error, completed_at, run_id),
            )

    def save_contest_results(self, *, run_id: int, results: list[dict[str, Any]]) -> None:
        import json as _j
        with self._connect() as conn:
            for r in results:
                violations = r.get("violations") or []
                can_contest = bool(r.get("can_contest"))
                conn.execute(
                    self._sql(
                        "INSERT INTO contest_analysis_results (run_id, review_uid, violations, can_contest) "
                        "VALUES (?,?,?,?) ON CONFLICT DO NOTHING"
                    ),
                    (run_id, str(r.get("review_uid") or ""), _j.dumps(violations), can_contest),
                )

    def get_contest_results_with_reviews(self, *, run_id: int, user_id: int) -> list[dict[str, Any]]:
        """Return contest results joined with review data, only where can_contest=true."""
        import json as _j
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("""
                    SELECT ri.created_at, ri.source, ri.rating, ri.category, ri.text,
                           COALESCE(ri.manual_reply, ri.auto_reply) AS reply_text,
                           COALESCE(
                               NULLIF(ri.metadata_json->'raw'->'productDetails'->>'vendorCode',''),
                               NULLIF(ri.metadata_json->'raw'->'productDetails'->>'supplierArticle',''),
                               ''
                           ) AS article,
                           cr.violations, cr.review_uid
                    FROM contest_analysis_results cr
                    JOIN review_items ri ON ri.review_uid = cr.review_uid
                    WHERE cr.run_id = ? AND cr.can_contest = TRUE AND ri.user_id = ?
                    ORDER BY ri.rating ASC, ri.created_at DESC
                """),
                (run_id, user_id),
            ).fetchall()
        result = []
        for row in rows:
            d = self._row_to_dict(row)
            try:
                d["violations"] = _j.loads(d.get("violations") or "[]")
            except Exception:
                d["violations"] = []
            result.append(d)
        return result

    def get_contest_details(self, *, run_id: int, user_id: int) -> list[dict[str, Any]]:
        """Return ALL contest results (with and without violations) joined with review data."""
        import json as _j
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("""
                    SELECT ri.created_at, ri.source, ri.rating, ri.text,
                           COALESCE(
                               NULLIF(ri.metadata_json->'raw'->'productDetails'->>'vendorCode',''),
                               NULLIF(ri.metadata_json->'raw'->'productDetails'->>'supplierArticle',''),
                               ''
                           ) AS article,
                           cr.violations, cr.can_contest, cr.review_uid
                    FROM contest_analysis_results cr
                    JOIN review_items ri ON ri.review_uid = cr.review_uid
                    WHERE cr.run_id = ? AND ri.user_id = ?
                    ORDER BY cr.can_contest DESC, ri.rating ASC, ri.created_at DESC
                """),
                (run_id, user_id),
            ).fetchall()
        result = []
        for row in rows:
            d = self._row_to_dict(row)
            try:
                d["violations"] = _j.loads(d.get("violations") or "[]")
            except Exception:
                d["violations"] = []
            result.append(d)
        return result

    def list_reviews_for_contest(
        self, *, user_id: int, source: str | None = None, date_from: str | None = None, date_to: str | None = None
    ) -> list[dict[str, Any]]:
        """Return reviews (rating 1-3, has text) for contest analysis."""
        conditions = ["user_id = ?", "rating <= 3", "rating IS NOT NULL", "text != ''"]
        params: list[Any] = [user_id]
        if source:
            conditions.append("source = ?")
            params.append(source)
        if date_from:
            conditions.append("created_at::date >= ?::date")
            params.append(date_from)
        if date_to:
            conditions.append("created_at::date <= ?::date")
            params.append(date_to)
        where = " AND ".join(conditions)
        with self._connect() as conn:
            rows = conn.execute(
                self._sql(f"SELECT review_uid, text, rating FROM review_items WHERE {where} ORDER BY created_at ASC"),
                tuple(params),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Review contradiction rules ────────────────────────────────────────────

    def _migrate_review_contradiction_rules(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_contradiction_rules (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                ratings_json TEXT NOT NULL DEFAULT '[]',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(user_id, group_id)
            )
            """
        )

    # ── Product categories (Feedback → Settings → Products) ───────────────────

    DEFAULT_PRODUCT_CATEGORIES: tuple[str, ...] = (
        "Наматрасник непромокаемый (ИП Авдеева, без маркировки)",
        "Наматрасник стеганый (ИП Авдеева, без маркировки)",
        "Наматрасник стеганый непромокаемый (ВарФабрик, без маркировки)",
        "Наматрасник непромокаемый (ВарФабрик, с маркировкой)",
        "Постельное белье (ВарФабрик, с маркировкой)",
    )

    def _migrate_product_categories(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product_categories (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                boxes_per_pallet INTEGER,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            self._sql(
                "CREATE INDEX IF NOT EXISTS idx_product_categories_user "
                "ON product_categories(user_id, sort_order, id)"
            )
        )

    def list_product_categories(
        self, *, user_id: int, seed_defaults: bool = True
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._migrate_product_categories(conn)
            rows = conn.execute(
                self._sql(
                    """
                    SELECT * FROM product_categories
                    WHERE user_id = ?
                    ORDER BY sort_order ASC, id ASC
                    """
                ),
                (user_id,),
            ).fetchall()
            if rows or not seed_defaults:
                return [self._row_to_dict(r) for r in rows]
            now = _utc_now()
            for idx, name in enumerate(self.DEFAULT_PRODUCT_CATEGORIES):
                self._insert_and_get_id(
                    conn,
                    self._sql(
                        """
                        INSERT INTO product_categories (
                            user_id, name, boxes_per_pallet, sort_order, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (user_id, name, None, idx, now, now),
                )
            rows = conn.execute(
                self._sql(
                    """
                    SELECT * FROM product_categories
                    WHERE user_id = ?
                    ORDER BY sort_order ASC, id ASC
                    """
                ),
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def save_product_categories(
        self, *, user_id: int, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Replace user category list; rename/delete syncs product_photos.product_category."""
        cleaned: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                raise ValueError("Название категории не может быть пустым")
            key = name.casefold()
            if key in seen_names:
                raise ValueError(f"Дублируется категория: {name}")
            seen_names.add(key)
            boxes_raw = raw.get("boxes_per_pallet")
            boxes: int | None
            if boxes_raw is None or str(boxes_raw).strip() == "":
                boxes = None
            else:
                try:
                    boxes = int(boxes_raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "Кол-во коробов на паллете должно быть целым числом"
                    ) from exc
                if boxes < 0:
                    raise ValueError("Кол-во коробов на паллете не может быть отрицательным")
            cat_id = raw.get("id")
            try:
                parsed_id = int(cat_id) if cat_id is not None and str(cat_id).strip() != "" else None
            except (TypeError, ValueError):
                parsed_id = None
            cleaned.append(
                {
                    "id": parsed_id,
                    "name": name,
                    "boxes_per_pallet": boxes,
                }
            )

        now = _utc_now()
        with self._connect() as conn:
            self._migrate_product_categories(conn)
            self._migrate_product_photos(conn)
            existing_rows = conn.execute(
                self._sql(
                    "SELECT id, name FROM product_categories WHERE user_id = ?"
                ),
                (user_id,),
            ).fetchall()
            existing = {
                int(r["id"]): str(r["name"] or "")
                for r in existing_rows
            }
            keep_ids: set[int] = set()
            renames: list[tuple[str, str]] = []

            for idx, item in enumerate(cleaned):
                cat_id = item["id"]
                name = item["name"]
                boxes = item["boxes_per_pallet"]
                if cat_id is not None and cat_id in existing:
                    old_name = existing[cat_id]
                    conn.execute(
                        self._sql(
                            """
                            UPDATE product_categories
                            SET name = ?, boxes_per_pallet = ?, sort_order = ?, updated_at = ?
                            WHERE user_id = ? AND id = ?
                            """
                        ),
                        (name, boxes, idx, now, user_id, cat_id),
                    )
                    keep_ids.add(cat_id)
                    if old_name and old_name != name:
                        renames.append((old_name, name))
                else:
                    new_id = self._insert_and_get_id(
                        conn,
                        self._sql(
                            """
                            INSERT INTO product_categories (
                                user_id, name, boxes_per_pallet, sort_order, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """
                        ),
                        (user_id, name, boxes, idx, now, now),
                    )
                    keep_ids.add(int(new_id))

            deleted_names: list[str] = []
            for old_id, old_name in existing.items():
                if old_id in keep_ids:
                    continue
                conn.execute(
                    self._sql(
                        "DELETE FROM product_categories WHERE user_id = ? AND id = ?"
                    ),
                    (user_id, old_id),
                )
                if old_name:
                    deleted_names.append(old_name)

            for old_name, new_name in renames:
                conn.execute(
                    self._sql(
                        """
                        UPDATE product_photos
                        SET product_category = ?, updated_at = ?
                        WHERE user_id = ? AND product_category = ?
                        """
                    ),
                    (new_name, now, user_id, old_name),
                )
            for old_name in deleted_names:
                # Skip if renamed away already (same old name shouldn't appear).
                if any(old == old_name for old, _new in renames):
                    continue
                conn.execute(
                    self._sql(
                        """
                        UPDATE product_photos
                        SET product_category = '', updated_at = ?
                        WHERE user_id = ? AND product_category = ?
                        """
                    ),
                    (now, user_id, old_name),
                )

            rows = conn.execute(
                self._sql(
                    """
                    SELECT * FROM product_categories
                    WHERE user_id = ?
                    ORDER BY sort_order ASC, id ASC
                    """
                ),
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Product photos catalog ────────────────────────────────────────────────

    def _migrate_product_photos(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS product_photos (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
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
            )
        """)
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_product_photos_user "
            "ON product_photos(user_id)"
        ))
        conn.execute(
            "ALTER TABLE product_photos ADD COLUMN IF NOT EXISTS yandex_offer_id TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "ALTER TABLE product_photos ADD COLUMN IF NOT EXISTS box_qty INTEGER"
        )
        conn.execute(
            "ALTER TABLE product_photos ADD COLUMN IF NOT EXISTS product_category TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "ALTER TABLE product_photos ADD COLUMN IF NOT EXISTS skip_kiz_gtin_check INTEGER NOT NULL DEFAULT 0"
        )

    def list_product_photos(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("SELECT * FROM product_photos WHERE user_id = ? ORDER BY name ASC"),
                (user_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = self._row_to_dict(r)
            d["skip_kiz_gtin_check"] = bool(int(d.get("skip_kiz_gtin_check") or 0))
            out.append(d)
        return out

    def add_product_photo(
        self, *, user_id: int, name: str, supplier_article: str,
        wb_nmid: str, ozon_sku: str, photo_path: str | None,
        yandex_offer_id: str = "",
        box_qty: int | None = None,
        product_category: str = "",
        skip_kiz_gtin_check: bool = False,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            product_id = self._insert_and_get_id(
                conn,
                self._sql("""
                INSERT INTO product_photos (
                    user_id, name, supplier_article, wb_nmid, ozon_sku, yandex_offer_id,
                    box_qty, product_category, skip_kiz_gtin_check, photo_path, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """),
                (
                    user_id,
                    name.strip(),
                    supplier_article.strip(),
                    wb_nmid.strip(),
                    ozon_sku.strip(),
                    yandex_offer_id.strip(),
                    box_qty,
                    str(product_category or "").strip(),
                    1 if skip_kiz_gtin_check else 0,
                    photo_path,
                    now,
                    now,
                ),
            )
            row = conn.execute(self._sql("SELECT * FROM product_photos WHERE id = ?"), (product_id,)).fetchone()
        d = self._row_to_dict(row) if row else {}
        if d:
            d["skip_kiz_gtin_check"] = bool(int(d.get("skip_kiz_gtin_check") or 0))
        return d

    def update_product_photo(
        self, *, user_id: int, product_id: int, name: str, supplier_article: str,
        wb_nmid: str, ozon_sku: str, photo_path: str | None = None,
        yandex_offer_id: str = "",
        box_qty: int | None = None,
        product_category: str = "",
        skip_kiz_gtin_check: bool = False,
    ) -> bool:
        now = _utc_now()
        sets = [
            "name=?",
            "supplier_article=?",
            "wb_nmid=?",
            "ozon_sku=?",
            "yandex_offer_id=?",
            "box_qty=?",
            "product_category=?",
            "skip_kiz_gtin_check=?",
            "updated_at=?",
        ]
        params: list[Any] = [
            name.strip(),
            supplier_article.strip(),
            wb_nmid.strip(),
            ozon_sku.strip(),
            yandex_offer_id.strip(),
            box_qty,
            str(product_category or "").strip(),
            1 if skip_kiz_gtin_check else 0,
            now,
        ]
        if photo_path is not None:
            sets.append("photo_path=?")
            params.append(photo_path)
        params.extend([user_id, product_id])
        with self._connect() as conn:
            result = conn.execute(
                self._sql(f"UPDATE product_photos SET {', '.join(sets)} WHERE user_id=? AND id=?"),
                tuple(params),
            )
        return bool(result.rowcount)

    def delete_product_photo(self, *, user_id: int, product_id: int) -> dict[str, Any] | None:
        """Delete product and return the record (to delete the file)."""
        with self._connect() as conn:
            row = conn.execute(
                self._sql("SELECT * FROM product_photos WHERE user_id=? AND id=?"),
                (user_id, product_id),
            ).fetchone()
            if row:
                conn.execute(
                    self._sql("DELETE FROM product_photos WHERE user_id=? AND id=?"),
                    (user_id, product_id),
                )
                # Drop manual balance rows/visibility for this product if tables exist.
                try:
                    self._ensure_supply_balances_tables(conn)
                    conn.execute(
                        self._sql(
                            "DELETE FROM supply_balances "
                            "WHERE user_id = ? AND item_type = ? AND item_id = ?"
                        ),
                        (user_id, "product", int(product_id)),
                    )
                    conn.execute(
                        self._sql(
                            "DELETE FROM supply_stock_movements "
                            "WHERE user_id = ? AND item_type = ? AND item_id = ?"
                        ),
                        (user_id, "product", int(product_id)),
                    )
                    conn.execute(
                        self._sql(
                            "DELETE FROM supply_balance_visibility "
                            "WHERE user_id = ? AND item_type = ? AND item_id = ?"
                        ),
                        (user_id, "product", int(product_id)),
                    )
                except Exception:
                    pass
        return self._row_to_dict(row) if row else None

    def get_product_name_by_article(self, *, user_id: int) -> dict[str, str]:
        """Return name map from Feedback → Settings → Products (product_photos).

        Keys: supplier_article, wb_nmid, and casefold variants for resilient lookup.
        """
        rows = self.list_product_photos(user_id=user_id)
        result: dict[str, str] = {}
        for r in rows:
            name = str(r.get("name") or "").strip()
            if not name:
                continue
            for field in ("supplier_article", "wb_nmid"):
                key = str(r.get(field) or "").strip()
                if not key:
                    continue
                result[key] = name
                result[key.casefold()] = name
        return result

    def get_product_name_by_ozon_sku(self, *, user_id: int) -> dict[str, str]:
        """Return {ozon_sku: name} for fast lookup in OZON supply goods."""
        rows = self.list_product_photos(user_id=user_id)
        result: dict[str, str] = {}
        for r in rows:
            sku = str(r.get("ozon_sku") or "").strip()
            name = str(r.get("name") or "").strip()
            if sku and name:
                result[sku] = name
        return result

    def get_product_photo_map(self, *, user_id: int) -> dict[str, str]:
        """Return {key: photo_url} for fast lookup. Keys are supplier_article, wb_nmid, ozon_sku."""
        rows = self.list_product_photos(user_id=user_id)
        result: dict[str, str] = {}
        for r in rows:
            pid = str(r.get("id") or "")
            if not pid or not r.get("photo_path"):
                continue
            url = f"/api/products/photo/{pid}"
            for field in ("supplier_article", "wb_nmid", "ozon_sku", "yandex_offer_id"):
                val = str(r.get(field) or "").strip()
                if val:
                    result[val] = url
        return result

    def get_product_skip_kiz_gtin_check_map(self, *, user_id: int) -> dict[str, bool]:
        """Keys: supplier_article / wb_nmid (+ casefold) → skip GTIN↔ШК check in Маркировка."""
        rows = self.list_product_photos(user_id=user_id)
        result: dict[str, bool] = {}
        for r in rows:
            if not bool(r.get("skip_kiz_gtin_check")):
                continue
            for field in ("supplier_article", "wb_nmid"):
                key = str(r.get(field) or "").strip()
                if not key:
                    continue
                result[key] = True
                result[key.casefold()] = True
        return result

    def _migrate_manually_closed_at(self, conn) -> None:
        """Add manually_closed_at column to conversation_items."""
        cols = self._table_columns(conn, "conversation_items")
        if "manually_closed_at" not in cols:
            conn.execute(
                "ALTER TABLE conversation_items ADD COLUMN manually_closed_at TEXT"
            )

    def _ensure_contradiction_rules_table(self) -> None:
        """Create table on demand if migration didn't run yet."""
        with self._connect() as conn:
            self._migrate_review_contradiction_rules(conn)

    def list_review_contradiction_rules(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM review_contradiction_rules WHERE user_id = ? ORDER BY group_id ASC",
                (user_id,),
            ).fetchall()
        result = []
        for row in rows:
            d = self._row_to_dict(row)
            try:
                import json as _json
                d["ratings"] = _json.loads(str(d.get("ratings_json") or "[]"))
            except Exception:
                d["ratings"] = []
            result.append(d)
        return result

    def save_review_contradiction_rule(self, *, user_id: int, group_id: str, ratings: list[int]) -> None:
        import json as _json
        ratings_json = _json.dumps(sorted({int(r) for r in ratings if 1 <= int(r) <= 5}))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO review_contradiction_rules (user_id, group_id, ratings_json, created_at)
                VALUES (?, ?, ?, NOW())
                ON CONFLICT (user_id, group_id) DO UPDATE SET
                    ratings_json = excluded.ratings_json
                """,
                (user_id, group_id.strip(), ratings_json),
            )

    def delete_review_contradiction_rule(self, *, user_id: int, group_id: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM review_contradiction_rules WHERE user_id = ? AND group_id = ?",
                (user_id, group_id.strip()),
            )
        return bool(result.rowcount)

    def get_review_contradiction_map(self, *, user_id: int) -> dict[str, set[int]]:
        """Return {group_id: {rating_ints}} for fast lookup during sync."""
        rules = self.list_review_contradiction_rules(user_id=user_id)
        return {r["group_id"]: set(r["ratings"]) for r in rules if r.get("ratings")}

    def raw_fetch(self, query: str, params: tuple[Any, ...] = ()) -> list[Mapping[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ──────────────────────────────────────────────────────────────
    # Supply module (FBW/FBS) — fully isolated from feedback module
    # ──────────────────────────────────────────────────────────────

    def _migrate_supply_tables(self, conn) -> None:
        """Create supply module tables and add can_supplies column to users."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_sources (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                api_key_encrypted TEXT,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_synced_at TEXT
            )
            """
        )
        conn.execute(
            self._sql("CREATE INDEX IF NOT EXISTS idx_supply_sources_user ON supply_sources(user_id)")
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_items (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                source_id BIGINT NOT NULL REFERENCES supply_sources(id) ON DELETE CASCADE,
                supply_id BIGINT NOT NULL,
                preorder_id BIGINT,
                status_id INTEGER,
                box_type_id INTEGER,
                warehouse_id INTEGER,
                warehouse_name TEXT,
                create_date TEXT,
                supply_date TEXT,
                fact_date TEXT,
                quantity INTEGER NOT NULL DEFAULT 0,
                accepted_quantity INTEGER NOT NULL DEFAULT 0,
                ready_for_sale_quantity INTEGER NOT NULL DEFAULT 0,
                acceptance_cost TEXT,
                storage_coef TEXT,
                delivery_coef TEXT,
                supplier_name TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                synced_at TEXT NOT NULL,
                UNIQUE(source_id, supply_id)
            )
            """
        )
        conn.execute(
            self._sql("CREATE INDEX IF NOT EXISTS idx_supply_items_source ON supply_items(source_id)")
        )
        conn.execute(
            self._sql("CREATE INDEX IF NOT EXISTS idx_supply_items_supply ON supply_items(supply_date, source_id)")
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_goods (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                supply_item_id BIGINT NOT NULL REFERENCES supply_items(id) ON DELETE CASCADE,
                nm_id BIGINT,
                vendor_code TEXT,
                barcode TEXT,
                tech_size TEXT,
                color TEXT,
                quantity INTEGER NOT NULL DEFAULT 0,
                accepted_quantity INTEGER NOT NULL DEFAULT 0,
                tnved TEXT
            )
            """
        )
        conn.execute(
            self._sql("CREATE INDEX IF NOT EXISTS idx_supply_goods_item ON supply_goods(supply_item_id)")
        )
        # Add can_supplies flag to users for manager access control
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_supplies BOOLEAN NOT NULL DEFAULT FALSE"
        )
        # Add can_salary flag to users for salary section access control
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_salary BOOLEAN NOT NULL DEFAULT FALSE"
        )
        # Add can_salary_settings flag for salary settings access
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_salary_settings BOOLEAN NOT NULL DEFAULT FALSE"
        )
        # Add salary_productions for production-level access control (JSON array)
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS salary_productions TEXT NOT NULL DEFAULT '[]'"
        )
        # Add can_salary_report for расчёт начислений export permission
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_salary_report BOOLEAN NOT NULL DEFAULT FALSE"
        )
        # Add can_salary_zp_export for Экспорт ЗП permission
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_salary_zp_export BOOLEAN NOT NULL DEFAULT FALSE"
        )
        # Add can_supply_planning for Планирование поставок
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_supply_planning BOOLEAN NOT NULL DEFAULT FALSE"
        )
        # Manual balances (Остатки): access + allowed productions (JSON id list)
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_supply_stock BOOLEAN NOT NULL DEFAULT FALSE"
        )
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS stock_productions TEXT NOT NULL DEFAULT '[]'"
        )
        self._ensure_supply_balances_tables(conn)
        # Add transit/actual warehouse columns (idempotent)
        conn.execute(
            "ALTER TABLE supply_items ADD COLUMN IF NOT EXISTS transit_warehouse_name TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_items ADD COLUMN IF NOT EXISTS actual_warehouse_name TEXT"
        )
        # Manual-entry fields on supply_items (display columns, restored after sync)
        conn.execute(
            "ALTER TABLE supply_items ADD COLUMN IF NOT EXISTS pass_number TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_items ADD COLUMN IF NOT EXISTS pallets_count TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_items ADD COLUMN IF NOT EXISTS driver_name TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_items ADD COLUMN IF NOT EXISTS notes TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_items ADD COLUMN IF NOT EXISTS production TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_items ADD COLUMN IF NOT EXISTS drivers_json TEXT"
        )
        # Permanent store for user-entered supply data — survives clear_supply_items
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_manual_data (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                supply_id BIGINT NOT NULL,
                pass_number TEXT,
                pallets_count TEXT,
                driver_name TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, supply_id)
            )
            """
        )
        conn.execute(
            self._sql(
                "CREATE INDEX IF NOT EXISTS idx_supply_manual_data_user "
                "ON supply_manual_data(user_id)"
            )
        )
        conn.execute(
            "ALTER TABLE supply_manual_data ADD COLUMN IF NOT EXISTS notes TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_manual_data ADD COLUMN IF NOT EXISTS production TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_manual_data ADD COLUMN IF NOT EXISTS drivers_json TEXT"
        )
        # Drivers catalog for supply deliveries
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_drivers (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                full_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, full_name)
            )
            """
        )
        conn.execute(
            self._sql(
                "CREATE INDEX IF NOT EXISTS idx_supply_drivers_user "
                "ON supply_drivers(user_id)"
            )
        )
        conn.execute(
            "ALTER TABLE supply_drivers ADD COLUMN IF NOT EXISTS documents TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_drivers ADD COLUMN IF NOT EXISTS vehicles_json TEXT NOT NULL DEFAULT '[]'"
        )
        conn.execute(
            "ALTER TABLE supply_drivers ADD COLUMN IF NOT EXISTS carrier TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "ALTER TABLE supply_drivers ADD COLUMN IF NOT EXISTS in_person TEXT"
        )
        for _drv_carrier_col in (
            "carrier_name",
            "carrier_inn",
            "carrier_kpp",
            "carrier_phone",
            "carrier_fns_id",
            "carrier_addr_index",
            "carrier_addr_region_code",
            "carrier_addr_district",
            "carrier_addr_city",
            "carrier_addr_settlement",
            "carrier_addr_street",
            "carrier_addr_house",
            "carrier_addr_corpus",
            "carrier_addr_flat",
            "carrier_addr_fias",
        ):
            conn.execute(
                f"ALTER TABLE supply_drivers ADD COLUMN IF NOT EXISTS {_drv_carrier_col} TEXT NOT NULL DEFAULT ''"
            )
        # Driver documents for eTrN СвВодит: VU series/number/date or ИННФЛ.
        # doc_vu_issuer is catalog-only (not in СвВодит schema).
        for _drv_doc_col in (
            "doc_vu_series",
            "doc_vu_number",
            "doc_vu_issuer",
            "doc_vu_date",
            "doc_inn_fl",
        ):
            conn.execute(
                f"ALTER TABLE supply_drivers ADD COLUMN IF NOT EXISTS {_drv_doc_col} TEXT NOT NULL DEFAULT ''"
            )
        # Structured FIO for eTrN СвВодит/ФИО; full_name stays the one-line for docs/selects.
        for _drv_fio_col in ("last_name", "first_name", "middle_name"):
            conn.execute(
                f"ALTER TABLE supply_drivers ADD COLUMN IF NOT EXISTS {_drv_fio_col} TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "ALTER TABLE supply_drivers ADD COLUMN IF NOT EXISTS phone TEXT NOT NULL DEFAULT ''"
        )
        # ── OZON Supplies module (fully isolated from WB) ──────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ozon_supply_items (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                source_id BIGINT NOT NULL,
                supply_order_id BIGINT NOT NULL,
                supply_order_number TEXT,
                state TEXT,
                creation_date TEXT,
                supply_date TEXT,
                warehouse_id BIGINT,
                warehouse_name TEXT,
                transit_warehouse_name TEXT,
                is_crossdock INTEGER NOT NULL DEFAULT 0,
                total_quantity INTEGER NOT NULL DEFAULT 0,
                creation_flow TEXT,
                raw_json TEXT,
                synced_at TEXT,
                UNIQUE(source_id, supply_order_id)
            )
            """
        )
        conn.execute(
            self._sql("CREATE INDEX IF NOT EXISTS idx_ozon_supply_items_source ON ozon_supply_items(source_id)")
        )
        # Remove single-column unique on supply_order_id — only composite (source_id, supply_order_id) needed
        try:
            conn.execute(self._sql(
                "ALTER TABLE ozon_supply_items DROP CONSTRAINT IF EXISTS ozon_supply_items_supply_order_id_key"
            ))
        except Exception:
            pass
        # Add ON DELETE CASCADE FK so ozon_supply_items are cleaned up when source is deleted
        try:
            conn.execute(self._sql(
                "ALTER TABLE ozon_supply_items ADD CONSTRAINT fk_ozon_supply_items_source "
                "FOREIGN KEY (source_id) REFERENCES supply_sources(id) ON DELETE CASCADE"
            ))
        except Exception:
            pass  # constraint may already exist
        conn.execute(self._sql("ALTER TABLE ozon_supply_items ADD COLUMN IF NOT EXISTS transit_warehouse_name TEXT"))
        conn.execute(self._sql("ALTER TABLE ozon_supply_items ADD COLUMN IF NOT EXISTS is_crossdock INTEGER NOT NULL DEFAULT 0"))
        conn.execute(self._sql("ALTER TABLE ozon_supply_items ADD COLUMN IF NOT EXISTS vehicle_json TEXT"))
        conn.execute(self._sql("ALTER TABLE ozon_supply_items ADD COLUMN IF NOT EXISTS cargoes_json TEXT"))
        conn.execute(self._sql("ALTER TABLE ozon_supply_items ADD COLUMN IF NOT EXISTS supplier_name TEXT"))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ozon_supply_goods (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                supply_item_id BIGINT NOT NULL REFERENCES ozon_supply_items(id) ON DELETE CASCADE,
                sku BIGINT,
                name TEXT,
                quantity INTEGER NOT NULL DEFAULT 0,
                barcode TEXT,
                offer_id TEXT
            )
            """
        )
        conn.execute(
            self._sql("CREATE INDEX IF NOT EXISTS idx_ozon_supply_goods_item ON ozon_supply_goods(supply_item_id)")
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ozon_supply_manual_data (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                supply_order_id BIGINT NOT NULL,
                pallets_count TEXT,
                driver_name TEXT,
                notes TEXT,
                production TEXT,
                updated_at TEXT,
                UNIQUE(user_id, supply_order_id)
            )
            """
        )
        # ── End OZON Supplies ───────────────────────────────────────────────────
        # Warehouses catalog (name → address lookup)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_warehouses (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                warehouse_name TEXT NOT NULL,
                address TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(user_id, warehouse_name)
            )
            """
        )
        conn.execute(
            self._sql(
                "CREATE INDEX IF NOT EXISTS idx_supply_warehouses_user "
                "ON supply_warehouses(user_id)"
            )
        )
        for _wh_addr_col in (
            "addr_index",
            "addr_region_code",
            "addr_district",
            "addr_city",
            "addr_settlement",
            "addr_street",
            "addr_house",
            "addr_corpus",
            "addr_flat",
        ):
            conn.execute(
                f"ALTER TABLE supply_warehouses ADD COLUMN IF NOT EXISTS {_wh_addr_col} TEXT NOT NULL DEFAULT ''"
            )
        # Legal entities catalog (short name → full name lookup)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_legal_entities (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                short_name TEXT NOT NULL,
                full_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(user_id, short_name)
            )
            """
        )
        conn.execute(
            self._sql(
                "CREATE INDEX IF NOT EXISTS idx_supply_legal_entities_user "
                "ON supply_legal_entities(user_id)"
            )
        )
        conn.execute(
            "ALTER TABLE supply_legal_entities ADD COLUMN IF NOT EXISTS requisites TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_sources ADD COLUMN IF NOT EXISTS marketplace TEXT DEFAULT 'wb'"
        )
        conn.execute(
            "ALTER TABLE supply_sources ADD COLUMN IF NOT EXISTS client_id TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_legal_entities ADD COLUMN IF NOT EXISTS signatories TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_legal_entities ADD COLUMN IF NOT EXISTS in_person TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_legal_entities ADD COLUMN IF NOT EXISTS basis TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_legal_entities ADD COLUMN IF NOT EXISTS address TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_legal_entities ADD COLUMN IF NOT EXISTS signature_image TEXT"
        )
        conn.execute(
            "ALTER TABLE supply_legal_entities ADD COLUMN IF NOT EXISTS phone TEXT"
        )
        for _le_addr_col in (
            "addr_index",
            "addr_region_code",
            "addr_district",
            "addr_city",
            "addr_settlement",
            "addr_street",
            "addr_house",
            "addr_corpus",
            "addr_flat",
            "addr_fias",
        ):
            conn.execute(
                f"ALTER TABLE supply_legal_entities ADD COLUMN IF NOT EXISTS {_le_addr_col} TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_poa_records (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                legal_entity_id BIGINT NOT NULL,
                contractor_id BIGINT NOT NULL,
                driver_id BIGINT NOT NULL,
                poa_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            self._sql("CREATE INDEX IF NOT EXISTS idx_supply_poa_records_user ON supply_poa_records(user_id)")
        )
        conn.execute("ALTER TABLE supply_poa_records ADD COLUMN IF NOT EXISTS driver_manual_name TEXT")
        conn.execute("ALTER TABLE supply_poa_records ADD COLUMN IF NOT EXISTS driver_manual_docs TEXT")
        # Contour.Logistics / Diadoc EDO settings + sent document tracking (Ozon).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_edo_settings (
                user_id BIGINT PRIMARY KEY,
                api_url TEXT NOT NULL DEFAULT '',
                api_key_encrypted TEXT,
                diadoc_url TEXT NOT NULL DEFAULT '',
                diadoc_client_id TEXT NOT NULL DEFAULT '',
                diadoc_login TEXT NOT NULL DEFAULT '',
                diadoc_password_encrypted TEXT,
                diadoc_from_box_id TEXT NOT NULL DEFAULT '',
                diadoc_to_box_id TEXT NOT NULL DEFAULT '',
                cert_thumbprint TEXT NOT NULL DEFAULT '',
                is_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ozon_edo_documents (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                supply_order_id BIGINT NOT NULL,
                doc_type TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT '',
                transportation_id TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL DEFAULT '',
                entity_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                status_label TEXT NOT NULL DEFAULT '',
                mintrans_id TEXT NOT NULL DEFAULT '',
                mintrans_status TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '',
                sent_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE (user_id, supply_order_id, doc_type)
            )
            """
        )
        conn.execute(
            self._sql(
                "CREATE INDEX IF NOT EXISTS idx_ozon_edo_docs_user_supply "
                "ON ozon_edo_documents(user_id, supply_order_id)"
            )
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_certificates (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                legal_entity_short TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                number TEXT NOT NULL DEFAULT '',
                expiry_date TEXT NOT NULL DEFAULT '',
                verification_url TEXT NOT NULL DEFAULT '',
                image_data TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            self._sql("CREATE INDEX IF NOT EXISTS idx_supply_certificates_user ON supply_certificates(user_id)")
        )
        conn.execute(
            "ALTER TABLE supply_certificates ADD COLUMN IF NOT EXISTS doc_type TEXT NOT NULL DEFAULT 'Сертификат соответствия'"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_productions (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                head_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("ALTER TABLE supply_productions ADD COLUMN IF NOT EXISTS address TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE supply_productions ADD COLUMN IF NOT EXISTS load_contact TEXT NOT NULL DEFAULT ''")
        for _prod_addr_col in (
            "addr_index",
            "addr_region_code",
            "addr_district",
            "addr_city",
            "addr_settlement",
            "addr_street",
            "addr_house",
            "addr_corpus",
            "addr_flat",
        ):
            conn.execute(
                f"ALTER TABLE supply_productions ADD COLUMN IF NOT EXISTS {_prod_addr_col} TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_contractors (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                requisites TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            self._sql("CREATE INDEX IF NOT EXISTS idx_supply_contractors_user ON supply_contractors(user_id)")
        )
        conn.execute(
            self._sql("CREATE INDEX IF NOT EXISTS idx_supply_productions_user ON supply_productions(user_id)")
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ttn_counter (
                date TEXT PRIMARY KEY,
                n    INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Last Ozon giveout barcode-reset time (for 24h validity / 6h auto-refresh).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ozon_giveout_barcode_state (
                user_id BIGINT NOT NULL,
                source_id BIGINT NOT NULL,
                reset_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, source_id)
            )
            """
        )

    def _ensure_supply_tables(self) -> None:
        with self._connect() as conn:
            self._migrate_supply_tables(conn)

    # ── Supply Drivers CRUD ──

    @staticmethod
    def _normalize_carrier_fields(
        *,
        carrier_name: str = "",
        carrier_inn: str = "",
        carrier_kpp: str = "",
        carrier_phone: str = "",
        carrier_fns_id: str = "",
        carrier_addr_index: str = "",
        carrier_addr_region_code: str = "",
        carrier_addr_district: str = "",
        carrier_addr_city: str = "",
        carrier_addr_settlement: str = "",
        carrier_addr_street: str = "",
        carrier_addr_house: str = "",
        carrier_addr_corpus: str = "",
        carrier_addr_flat: str = "",
        carrier_addr_fias: str = "",
    ) -> dict[str, str]:
        inn = re.sub(r"\D", "", str(carrier_inn or ""))[:12]
        kpp = re.sub(r"\D", "", str(carrier_kpp or ""))[:9]
        region = re.sub(r"\D", "", str(carrier_addr_region_code or ""))[:2]
        index = re.sub(r"\D", "", str(carrier_addr_index or ""))[:6]
        phone = re.sub(r"\s+", " ", str(carrier_phone or "").strip())[:32]
        # Diadoc/FNS participant id, e.g. 2BM-7704217370-774301001-201407110916237240124
        fns_id = re.sub(r"\s+", "", str(carrier_fns_id or "").strip())[:80]
        fias = re.sub(r"[{}\s]", "", str(carrier_addr_fias or "").strip()).lower()
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", fias):
            fias = ""
        return {
            "carrier_name": str(carrier_name or "").strip(),
            "carrier_inn": inn,
            "carrier_kpp": kpp,
            "carrier_phone": phone,
            "carrier_fns_id": fns_id,
            "carrier_addr_index": index,
            "carrier_addr_region_code": region,
            "carrier_addr_district": str(carrier_addr_district or "").strip(),
            "carrier_addr_city": str(carrier_addr_city or "").strip(),
            "carrier_addr_settlement": str(carrier_addr_settlement or "").strip(),
            "carrier_addr_street": str(carrier_addr_street or "").strip(),
            "carrier_addr_house": str(carrier_addr_house or "").strip(),
            "carrier_addr_corpus": str(carrier_addr_corpus or "").strip(),
            "carrier_addr_flat": str(carrier_addr_flat or "").strip(),
            "carrier_addr_fias": fias,
        }

    @classmethod
    def compose_carrier_line(cls, fields: dict[str, Any] | None = None, **extra: str) -> str:
        """One-line carrier for заявки / Исполнитель: name + ИНН/КПП + address."""
        data = {**(fields or {}), **extra}
        name = str(data.get("carrier_name") or "").strip()
        inn = str(data.get("carrier_inn") or "").strip()
        kpp = str(data.get("carrier_kpp") or "").strip()
        addr = cls.compose_production_address_line(
            {
                "addr_index": data.get("carrier_addr_index") or "",
                "addr_district": data.get("carrier_addr_district") or "",
                "addr_city": data.get("carrier_addr_city") or "",
                "addr_settlement": data.get("carrier_addr_settlement") or "",
                "addr_street": data.get("carrier_addr_street") or "",
                "addr_house": data.get("carrier_addr_house") or "",
                "addr_corpus": data.get("carrier_addr_corpus") or "",
                "addr_flat": data.get("carrier_addr_flat") or "",
            }
        )
        head_parts: list[str] = []
        if name:
            head_parts.append(name)
        if inn:
            head_parts.append(f"ИНН {inn}")
        if kpp:
            head_parts.append(f"КПП {kpp}")
        head = " ".join(head_parts)
        if head and addr:
            return f"{head}, {addr}"
        if head:
            return head
        if addr:
            return addr
        return str(data.get("carrier") or "").strip()

    @classmethod
    def carrier_line(cls, driver: dict[str, Any] | None) -> str:
        if not driver:
            return ""
        composed = cls.compose_carrier_line(driver)
        if composed:
            return composed
        return str(driver.get("carrier") or "").strip()

    @staticmethod
    def _normalize_driver_doc_fields(
        *,
        doc_vu_series: str = "",
        doc_vu_number: str = "",
        doc_vu_issuer: str = "",
        doc_vu_date: str = "",
        doc_inn_fl: str = "",
    ) -> dict[str, str]:
        series = re.sub(r"\s+", "", str(doc_vu_series or "").strip())
        series = re.sub(r"[^\dA-Za-zА-Яа-я]", "", series)[:20]
        number = re.sub(r"\s+", "", str(doc_vu_number or "").strip())
        number = re.sub(r"[^\dA-Za-zА-Яа-я]", "", number)[:20]
        issuer = re.sub(r"\s+", " ", str(doc_vu_issuer or "").strip())[:255]
        date_raw = str(doc_vu_date or "").strip()
        date_val = ""
        if date_raw:
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", date_raw)
            if m:
                date_val = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
            else:
                iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", date_raw)
                if iso:
                    date_val = f"{iso.group(3)}.{iso.group(2)}.{iso.group(1)}"
        inn = re.sub(r"\D", "", str(doc_inn_fl or ""))[:12]
        return {
            "doc_vu_series": series,
            "doc_vu_number": number,
            "doc_vu_issuer": issuer,
            "doc_vu_date": date_val,
            "doc_inn_fl": inn,
        }

    @staticmethod
    def compose_driver_documents_line(fields: dict[str, Any] | None = None, **extra: str) -> str:
        """One-line driver docs for PoA / заявка / table display."""
        data = {**(fields or {}), **extra}
        parts: list[str] = []
        series = str(data.get("doc_vu_series") or "").strip()
        number = str(data.get("doc_vu_number") or "").strip()
        issuer = str(data.get("doc_vu_issuer") or "").strip()
        date_val = str(data.get("doc_vu_date") or "").strip()
        inn = str(data.get("doc_inn_fl") or "").strip()
        # Issuer/date are suffixes only — never compose a standalone «ВУ кем выд.»
        # line that would overwrite a richer legacy documents string on save.
        if series or number:
            vu = "ВУ"
            if series and number:
                # Common display: «ВУ 9900 123456»
                if len(series) == 4 and series.isdigit():
                    vu = f"ВУ {series[:2]} {series[2:]} {number}"
                else:
                    vu = f"ВУ {series} {number}"
            elif series:
                vu = f"ВУ серия {series}"
            else:
                vu = f"ВУ № {number}"
            if issuer:
                vu = f"{vu} кем выд. {issuer}"
            if date_val:
                vu = f"{vu} выд. {date_val}"
            parts.append(vu)
        if inn:
            parts.append(f"ИНН {inn}")
        return ", ".join(parts)

    @classmethod
    def driver_documents_line(cls, driver: dict[str, Any] | None) -> str:
        if not driver:
            return ""
        series = str(driver.get("doc_vu_series") or "").strip()
        number = str(driver.get("doc_vu_number") or "").strip()
        legacy = str(driver.get("documents") or "").strip()
        composed = cls.compose_driver_documents_line(driver)
        # Prefer structured VU line when series/number present; otherwise keep legacy
        # free-text (issuer/date/ИНН alone must not wipe passport/VU prose).
        if series or number:
            return composed or legacy
        if composed and not legacy:
            return composed
        return legacy or composed

    @staticmethod
    def compose_driver_full_name(fields: dict[str, Any] | None = None, **extra: str) -> str:
        """One-line ФИО for selects / TTN / заявка / PoA."""
        data = {**(fields or {}), **extra}
        parts = [
            str(data.get("last_name") or "").strip(),
            str(data.get("first_name") or "").strip(),
            str(data.get("middle_name") or "").strip(),
        ]
        composed = " ".join(p for p in parts if p)
        return composed or str(data.get("full_name") or "").strip()

    @classmethod
    def _normalize_driver_fio_fields(
        cls,
        *,
        last_name: str = "",
        first_name: str = "",
        middle_name: str = "",
        full_name: str = "",
    ) -> dict[str, str]:
        last = re.sub(r"\s+", " ", str(last_name or "").strip())[:100]
        first = re.sub(r"\s+", " ", str(first_name or "").strip())[:100]
        middle = re.sub(r"\s+", " ", str(middle_name or "").strip())[:100]
        legacy = re.sub(r"\s+", " ", str(full_name or "").strip())
        if not last and not first and not middle and legacy:
            parts = [p for p in legacy.split(" ") if p]
            if len(parts) == 1:
                last = parts[0]
            elif len(parts) == 2:
                last, first = parts[0], parts[1]
            elif parts:
                last, first = parts[0], parts[1]
                middle = " ".join(parts[2:])
        composed = cls.compose_driver_full_name(
            last_name=last, first_name=first, middle_name=middle, full_name=legacy
        )
        return {
            "last_name": last,
            "first_name": first,
            "middle_name": middle,
            "full_name": composed,
        }

    @classmethod
    def driver_full_name_line(cls, driver: dict[str, Any] | None) -> str:
        if not driver:
            return ""
        return cls.compose_driver_full_name(driver) or str(driver.get("full_name") or "").strip()

    @staticmethod
    def _normalize_driver_phone(phone: str = "") -> str:
        return re.sub(r"\s+", " ", str(phone or "").strip())[:32]

    @staticmethod
    def compose_vehicle_line(fields: dict[str, Any] | None = None, **extra: str) -> str:
        """One-line «марка номер» for selects / заявка / table tags."""
        data = {**(fields or {}), **extra}
        line = str(data.get("line") or "").strip()
        if line:
            return line
        model = str(data.get("model") or data.get("vehicle_model") or "").strip()
        number = str(data.get("number") or data.get("vehicle_number") or "").strip()
        return f"{model} {number}".strip()

    @classmethod
    def _normalize_vehicle(cls, raw: Any) -> dict[str, str] | None:
        """Normalize catalog vehicle to eTrN СвТС fields + display line."""
        if raw is None:
            return None
        if isinstance(raw, str):
            line = raw.strip()
            if not line:
                return None
            parts = [t for t in line.split() if t]
            model, number = line, ""
            if parts:
                maybe = parts[-1]
                if re.search(r"\d", maybe):
                    number = maybe[:9]
                    model = " ".join(parts[:-1]).strip()
                else:
                    model = line
            return {
                "model": model,
                "number": number,
                "type": "грузовой автомобиль",
                "ownership": "1",
                "capacity_t": "20",
                "volume_m3": "20",
                "line": cls.compose_vehicle_line(model=model, number=number) or line,
            }
        if not isinstance(raw, dict):
            return None
        model = str(raw.get("model") or raw.get("vehicle_model") or "").strip()
        number = re.sub(r"\s+", "", str(raw.get("number") or raw.get("vehicle_number") or "").strip())[:9]
        v_type = str(raw.get("type") or "").strip() or "грузовой автомобиль"
        ownership = str(raw.get("ownership") or "").strip()
        if ownership not in {"1", "2", "3", "4", "5"}:
            ownership = "1"
        capacity = str(raw.get("capacity_t") or "").strip().replace(",", ".")
        if not re.match(r"^\d{1,5}(?:\.\d{1,2})?$", capacity or ""):
            capacity = "20"
        volume = str(raw.get("volume_m3") or "").strip().replace(",", ".")
        if not re.match(r"^\d{1,4}(?:\.\d{1,2})?$", volume or ""):
            volume = "20"
        line = str(raw.get("line") or "").strip() or cls.compose_vehicle_line(model=model, number=number)
        if not model and not number and not line:
            return None
        if not line:
            line = model or number
        return {
            "model": model,
            "number": number,
            "type": v_type,
            "ownership": ownership,
            "capacity_t": capacity,
            "volume_m3": volume,
            "line": line,
        }

    @classmethod
    def _normalize_vehicles_list(cls, vehicles: list | None) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for raw in vehicles or []:
            item = cls._normalize_vehicle(raw)
            if item:
                out.append(item)
        return out

    def list_supply_drivers(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql(
                    "SELECT * FROM supply_drivers WHERE user_id = ? ORDER BY full_name ASC"
                ),
                (user_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            d = self._row_to_dict(row)
            fio = self._normalize_driver_fio_fields(
                last_name=str(d.get("last_name") or ""),
                first_name=str(d.get("first_name") or ""),
                middle_name=str(d.get("middle_name") or ""),
                full_name=str(d.get("full_name") or ""),
            )
            d.update(fio)
            d["carrier"] = self.carrier_line(d)
            d["documents"] = self.driver_documents_line(d)
            result.append(d)
        return result

    def driver_exists(self, *, user_id: int, full_name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                self._sql(
                    "SELECT id FROM supply_drivers WHERE user_id = ? AND LOWER(full_name) = LOWER(?)"
                ),
                (user_id, full_name.strip()),
            ).fetchone()
        return row is not None

    def create_supply_driver(
        self,
        *,
        user_id: int,
        full_name: str = "",
        last_name: str = "",
        first_name: str = "",
        middle_name: str = "",
        phone: str = "",
        documents: str = "",
        in_person: str = "",
        vehicles: list | None = None,
        carrier: str = "",
        carrier_name: str = "",
        carrier_inn: str = "",
        carrier_kpp: str = "",
        carrier_phone: str = "",
        carrier_fns_id: str = "",
        carrier_addr_index: str = "",
        carrier_addr_region_code: str = "",
        carrier_addr_district: str = "",
        carrier_addr_city: str = "",
        carrier_addr_settlement: str = "",
        carrier_addr_street: str = "",
        carrier_addr_house: str = "",
        carrier_addr_corpus: str = "",
        carrier_addr_flat: str = "",
        carrier_addr_fias: str = "",
        doc_vu_series: str = "",
        doc_vu_number: str = "",
        doc_vu_issuer: str = "",
        doc_vu_date: str = "",
        doc_inn_fl: str = "",
    ) -> dict[str, Any]:
        import json as _j
        now = _utc_now()
        fio = self._normalize_driver_fio_fields(
            last_name=last_name,
            first_name=first_name,
            middle_name=middle_name,
            full_name=full_name,
        )
        phone_val = self._normalize_driver_phone(phone)
        vj = _j.dumps(self._normalize_vehicles_list(vehicles), ensure_ascii=False)
        cf = self._normalize_carrier_fields(
            carrier_name=carrier_name,
            carrier_inn=carrier_inn,
            carrier_kpp=carrier_kpp,
            carrier_phone=carrier_phone,
            carrier_fns_id=carrier_fns_id,
            carrier_addr_index=carrier_addr_index,
            carrier_addr_region_code=carrier_addr_region_code,
            carrier_addr_district=carrier_addr_district,
            carrier_addr_city=carrier_addr_city,
            carrier_addr_settlement=carrier_addr_settlement,
            carrier_addr_street=carrier_addr_street,
            carrier_addr_house=carrier_addr_house,
            carrier_addr_corpus=carrier_addr_corpus,
            carrier_addr_flat=carrier_addr_flat,
            carrier_addr_fias=carrier_addr_fias,
        )
        df = self._normalize_driver_doc_fields(
            doc_vu_series=doc_vu_series,
            doc_vu_number=doc_vu_number,
            doc_vu_issuer=doc_vu_issuer,
            doc_vu_date=doc_vu_date,
            doc_inn_fl=doc_inn_fl,
        )
        carrier_val = self.compose_carrier_line(cf) or str(carrier or "").strip()
        docs_val = self.compose_driver_documents_line(df) or str(documents or "").strip() or None
        with self._connect() as conn:
            driver_id = self._insert_and_get_id(
                conn,
                "INSERT INTO supply_drivers ("
                "user_id, full_name, last_name, first_name, middle_name, phone, documents, in_person, vehicles_json, carrier, "
                "carrier_name, carrier_inn, carrier_kpp, carrier_phone, carrier_fns_id, "
                "carrier_addr_index, carrier_addr_region_code, carrier_addr_district, "
                "carrier_addr_city, carrier_addr_settlement, carrier_addr_street, "
                "carrier_addr_house, carrier_addr_corpus, carrier_addr_flat, carrier_addr_fias, "
                "doc_vu_series, doc_vu_number, doc_vu_issuer, doc_vu_date, doc_inn_fl, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    fio["full_name"],
                    fio["last_name"],
                    fio["first_name"],
                    fio["middle_name"],
                    phone_val,
                    docs_val,
                    (in_person or "").strip() or None,
                    vj,
                    carrier_val,
                    cf["carrier_name"],
                    cf["carrier_inn"],
                    cf["carrier_kpp"],
                    cf["carrier_phone"],
                    cf["carrier_fns_id"],
                    cf["carrier_addr_index"],
                    cf["carrier_addr_region_code"],
                    cf["carrier_addr_district"],
                    cf["carrier_addr_city"],
                    cf["carrier_addr_settlement"],
                    cf["carrier_addr_street"],
                    cf["carrier_addr_house"],
                    cf["carrier_addr_corpus"],
                    cf["carrier_addr_flat"],
                    cf["carrier_addr_fias"],
                    df["doc_vu_series"],
                    df["doc_vu_number"],
                    df["doc_vu_issuer"],
                    df["doc_vu_date"],
                    df["doc_inn_fl"],
                    now,
                ),
            )
            row = conn.execute(
                self._sql("SELECT * FROM supply_drivers WHERE id = ?"),
                (driver_id,),
            ).fetchone()
        if not row:
            return {
                "id": driver_id,
                "phone": phone_val,
                "carrier": carrier_val,
                "documents": docs_val or "",
                **fio,
                **cf,
                **df,
            }
        d = self._row_to_dict(row)
        d.update(fio)
        d["phone"] = phone_val
        d["carrier"] = self.carrier_line(d)
        d["documents"] = self.driver_documents_line(d)
        return d

    def update_supply_driver(
        self,
        *,
        user_id: int,
        driver_id: int,
        full_name: str = "",
        last_name: str = "",
        first_name: str = "",
        middle_name: str = "",
        phone: str = "",
        documents: str = "",
        in_person: str = "",
        vehicles: list | None = None,
        carrier: str = "",
        carrier_name: str = "",
        carrier_inn: str = "",
        carrier_kpp: str = "",
        carrier_phone: str = "",
        carrier_fns_id: str = "",
        carrier_addr_index: str = "",
        carrier_addr_region_code: str = "",
        carrier_addr_district: str = "",
        carrier_addr_city: str = "",
        carrier_addr_settlement: str = "",
        carrier_addr_street: str = "",
        carrier_addr_house: str = "",
        carrier_addr_corpus: str = "",
        carrier_addr_flat: str = "",
        carrier_addr_fias: str = "",
        doc_vu_series: str = "",
        doc_vu_number: str = "",
        doc_vu_issuer: str = "",
        doc_vu_date: str = "",
        doc_inn_fl: str = "",
    ) -> bool:
        import json as _j
        fio = self._normalize_driver_fio_fields(
            last_name=last_name,
            first_name=first_name,
            middle_name=middle_name,
            full_name=full_name,
        )
        phone_val = self._normalize_driver_phone(phone)
        vj = _j.dumps(self._normalize_vehicles_list(vehicles), ensure_ascii=False)
        cf = self._normalize_carrier_fields(
            carrier_name=carrier_name,
            carrier_inn=carrier_inn,
            carrier_kpp=carrier_kpp,
            carrier_phone=carrier_phone,
            carrier_fns_id=carrier_fns_id,
            carrier_addr_index=carrier_addr_index,
            carrier_addr_region_code=carrier_addr_region_code,
            carrier_addr_district=carrier_addr_district,
            carrier_addr_city=carrier_addr_city,
            carrier_addr_settlement=carrier_addr_settlement,
            carrier_addr_street=carrier_addr_street,
            carrier_addr_house=carrier_addr_house,
            carrier_addr_corpus=carrier_addr_corpus,
            carrier_addr_flat=carrier_addr_flat,
            carrier_addr_fias=carrier_addr_fias,
        )
        df = self._normalize_driver_doc_fields(
            doc_vu_series=doc_vu_series,
            doc_vu_number=doc_vu_number,
            doc_vu_issuer=doc_vu_issuer,
            doc_vu_date=doc_vu_date,
            doc_inn_fl=doc_inn_fl,
        )
        composed = self.compose_carrier_line(cf)
        carrier_val = composed or str(carrier or "").strip()
        docs_val = self.compose_driver_documents_line(df) or str(documents or "").strip() or None
        with self._connect() as conn:
            if not carrier_val:
                existing = conn.execute(
                    self._sql("SELECT carrier FROM supply_drivers WHERE user_id = ? AND id = ?"),
                    (user_id, driver_id),
                ).fetchone()
                if existing:
                    carrier_val = str(self._row_to_dict(existing).get("carrier") or "").strip()
            if not docs_val:
                existing_docs = conn.execute(
                    self._sql("SELECT documents FROM supply_drivers WHERE user_id = ? AND id = ?"),
                    (user_id, driver_id),
                ).fetchone()
                if existing_docs:
                    docs_val = str(self._row_to_dict(existing_docs).get("documents") or "").strip() or None
            result = conn.execute(
                self._sql(
                    "UPDATE supply_drivers SET full_name = ?, last_name = ?, first_name = ?, middle_name = ?, "
                    "phone = ?, documents = ?, in_person = ?, vehicles_json = ?, "
                    "carrier = ?, carrier_name = ?, carrier_inn = ?, carrier_kpp = ?, carrier_phone = ?, "
                    "carrier_fns_id = ?, "
                    "carrier_addr_index = ?, carrier_addr_region_code = ?, carrier_addr_district = ?, "
                    "carrier_addr_city = ?, carrier_addr_settlement = ?, carrier_addr_street = ?, "
                    "carrier_addr_house = ?, carrier_addr_corpus = ?, carrier_addr_flat = ?, "
                    "carrier_addr_fias = ?, "
                    "doc_vu_series = ?, doc_vu_number = ?, doc_vu_issuer = ?, doc_vu_date = ?, doc_inn_fl = ? "
                    "WHERE user_id = ? AND id = ?"
                ),
                (
                    fio["full_name"],
                    fio["last_name"],
                    fio["first_name"],
                    fio["middle_name"],
                    phone_val,
                    docs_val,
                    (in_person or "").strip() or None,
                    vj,
                    carrier_val,
                    cf["carrier_name"],
                    cf["carrier_inn"],
                    cf["carrier_kpp"],
                    cf["carrier_phone"],
                    cf["carrier_fns_id"],
                    cf["carrier_addr_index"],
                    cf["carrier_addr_region_code"],
                    cf["carrier_addr_district"],
                    cf["carrier_addr_city"],
                    cf["carrier_addr_settlement"],
                    cf["carrier_addr_street"],
                    cf["carrier_addr_house"],
                    cf["carrier_addr_corpus"],
                    cf["carrier_addr_flat"],
                    cf["carrier_addr_fias"],
                    df["doc_vu_series"],
                    df["doc_vu_number"],
                    df["doc_vu_issuer"],
                    df["doc_vu_date"],
                    df["doc_inn_fl"],
                    user_id,
                    driver_id,
                ),
            )
        return bool(result.rowcount)

    def delete_supply_driver(self, *, user_id: int, driver_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM supply_drivers WHERE user_id = ? AND id = ?"),
                (user_id, driver_id),
            )
        return bool(result.rowcount)

    # ── Supply Warehouses CRUD ──

    @classmethod
    def warehouse_address_line(cls, wh: dict[str, Any] | None) -> str:
        """One-line warehouse address for TTN / заявка / packing list."""
        return cls.production_address_line(wh)

    def list_supply_warehouses(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("SELECT * FROM supply_warehouses WHERE user_id = ? ORDER BY warehouse_name ASC"),
                (user_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            d = self._row_to_dict(row)
            d["address"] = self.warehouse_address_line(d)
            result.append(d)
        return result

    def create_supply_warehouse(
        self,
        *,
        user_id: int,
        warehouse_name: str,
        address: str = "",
        addr_index: str = "",
        addr_region_code: str = "",
        addr_district: str = "",
        addr_city: str = "",
        addr_settlement: str = "",
        addr_street: str = "",
        addr_house: str = "",
        addr_corpus: str = "",
        addr_flat: str = "",
    ) -> dict[str, Any]:
        now = _utc_now()
        addr = self._normalize_production_addr_fields(
            addr_index=addr_index,
            addr_region_code=addr_region_code,
            addr_district=addr_district,
            addr_city=addr_city,
            addr_settlement=addr_settlement,
            addr_street=addr_street,
            addr_house=addr_house,
            addr_corpus=addr_corpus,
            addr_flat=addr_flat,
        )
        composed = self.compose_production_address_line(addr)
        address_val = composed or str(address or "").strip()
        with self._connect() as conn:
            wid = self._insert_and_get_id(
                conn,
                "INSERT INTO supply_warehouses ("
                "user_id, warehouse_name, address, "
                "addr_index, addr_region_code, addr_district, addr_city, addr_settlement, "
                "addr_street, addr_house, addr_corpus, addr_flat, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    warehouse_name.strip(),
                    address_val,
                    addr["addr_index"],
                    addr["addr_region_code"],
                    addr["addr_district"],
                    addr["addr_city"],
                    addr["addr_settlement"],
                    addr["addr_street"],
                    addr["addr_house"],
                    addr["addr_corpus"],
                    addr["addr_flat"],
                    now,
                ),
            )
            row = conn.execute(self._sql("SELECT * FROM supply_warehouses WHERE id = ?"), (wid,)).fetchone()
        if not row:
            return {"id": wid, "address": address_val, **addr}
        d = self._row_to_dict(row)
        d["address"] = self.warehouse_address_line(d)
        return d

    def update_supply_warehouse(
        self,
        *,
        user_id: int,
        warehouse_id: int,
        warehouse_name: str,
        address: str = "",
        addr_index: str = "",
        addr_region_code: str = "",
        addr_district: str = "",
        addr_city: str = "",
        addr_settlement: str = "",
        addr_street: str = "",
        addr_house: str = "",
        addr_corpus: str = "",
        addr_flat: str = "",
    ) -> bool:
        addr = self._normalize_production_addr_fields(
            addr_index=addr_index,
            addr_region_code=addr_region_code,
            addr_district=addr_district,
            addr_city=addr_city,
            addr_settlement=addr_settlement,
            addr_street=addr_street,
            addr_house=addr_house,
            addr_corpus=addr_corpus,
            addr_flat=addr_flat,
        )
        composed = self.compose_production_address_line(addr)
        address_val = composed or str(address or "").strip()
        with self._connect() as conn:
            if not address_val:
                existing_addr = conn.execute(
                    self._sql("SELECT address FROM supply_warehouses WHERE user_id = ? AND id = ?"),
                    (user_id, warehouse_id),
                ).fetchone()
                if existing_addr:
                    address_val = str(self._row_to_dict(existing_addr).get("address") or "").strip()
            result = conn.execute(
                self._sql(
                    "UPDATE supply_warehouses SET warehouse_name = ?, address = ?, "
                    "addr_index = ?, addr_region_code = ?, addr_district = ?, addr_city = ?, "
                    "addr_settlement = ?, addr_street = ?, addr_house = ?, addr_corpus = ?, addr_flat = ? "
                    "WHERE user_id = ? AND id = ?"
                ),
                (
                    warehouse_name.strip(),
                    address_val,
                    addr["addr_index"],
                    addr["addr_region_code"],
                    addr["addr_district"],
                    addr["addr_city"],
                    addr["addr_settlement"],
                    addr["addr_street"],
                    addr["addr_house"],
                    addr["addr_corpus"],
                    addr["addr_flat"],
                    user_id,
                    warehouse_id,
                ),
            )
        return bool(result.rowcount)

    def delete_supply_warehouse(self, *, user_id: int, warehouse_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM supply_warehouses WHERE user_id = ? AND id = ?"),
                (user_id, warehouse_id),
            )
        return bool(result.rowcount)

    def get_warehouse_address_map(self, *, user_id: int) -> dict[str, str]:
        """Return {warehouse_name: address} for lookups."""
        rows = self.list_supply_warehouses(user_id=user_id)
        return {r["warehouse_name"]: r["address"] for r in rows if r.get("warehouse_name")}

    # ── Supply Legal Entities CRUD ──

    @classmethod
    def legal_entity_address_line(cls, le: dict[str, Any] | None) -> str:
        """One-line legal address for documents: structured fields or legacy address."""
        return cls.production_address_line(le)

    def list_supply_legal_entities(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql(
                    "SELECT id, user_id, short_name, full_name, requisites, signatories, "
                    "in_person, basis, address, phone, "
                    "addr_index, addr_region_code, addr_district, addr_city, addr_settlement, "
                    "addr_street, addr_house, addr_corpus, addr_flat, addr_fias, created_at, "
                    "(signature_image IS NOT NULL AND signature_image != '') AS has_signature "
                    "FROM supply_legal_entities WHERE user_id = ? ORDER BY short_name ASC"
                ),
                (user_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            d = self._row_to_dict(row)
            d["address"] = self.legal_entity_address_line(d)
            result.append(d)
        return result

    def get_legal_entity_signature(self, *, user_id: int, entity_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                self._sql("SELECT signature_image FROM supply_legal_entities WHERE user_id = ? AND id = ?"),
                (user_id, entity_id),
            ).fetchone()
        if not row:
            return None
        d = self._row_to_dict(row)
        val = d.get("signature_image")
        return str(val) if val else None

    def set_legal_entity_signature(self, *, user_id: int, entity_id: int, image_base64: str | None) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("UPDATE supply_legal_entities SET signature_image = ? WHERE user_id = ? AND id = ?"),
                (image_base64 or None, user_id, entity_id),
            )
        return bool(result.rowcount)

    def update_supply_legal_entity(
        self,
        *,
        user_id: int,
        entity_id: int,
        short_name: str,
        full_name: str,
        requisites: str = "",
        signatories: str = "",
        in_person: str = "",
        basis: str = "",
        address: str = "",
        phone: str = "",
        addr_index: str = "",
        addr_region_code: str = "",
        addr_district: str = "",
        addr_city: str = "",
        addr_settlement: str = "",
        addr_street: str = "",
        addr_house: str = "",
        addr_corpus: str = "",
        addr_flat: str = "",
        addr_fias: str = "",
        signature_image: str | None = None,
        clear_signature: bool = False,
    ) -> bool:
        addr = self._normalize_production_addr_fields(
            addr_index=addr_index,
            addr_region_code=addr_region_code,
            addr_district=addr_district,
            addr_city=addr_city,
            addr_settlement=addr_settlement,
            addr_street=addr_street,
            addr_house=addr_house,
            addr_corpus=addr_corpus,
            addr_flat=addr_flat,
            addr_fias=addr_fias,
        )
        composed = self.compose_production_address_line(addr)
        address_val = composed or str(address or "").strip() or None
        with self._connect() as conn:
            if clear_signature:
                sig_val = None
            elif signature_image:
                sig_val = signature_image
            else:
                # Keep existing signature unchanged
                existing = conn.execute(self._sql("SELECT signature_image FROM supply_legal_entities WHERE id = ?"), (entity_id,)).fetchone()
                sig_val = self._row_to_dict(existing).get("signature_image") if existing else None
            if not address_val:
                existing_addr = conn.execute(
                    self._sql("SELECT address FROM supply_legal_entities WHERE user_id = ? AND id = ?"),
                    (user_id, entity_id),
                ).fetchone()
                if existing_addr:
                    address_val = str(self._row_to_dict(existing_addr).get("address") or "").strip() or None
            result = conn.execute(
                self._sql(
                    "UPDATE supply_legal_entities SET short_name = ?, full_name = ?, requisites = ?, "
                    "signatories = ?, in_person = ?, basis = ?, address = ?, phone = ?, "
                    "addr_index = ?, addr_region_code = ?, addr_district = ?, addr_city = ?, "
                    "addr_settlement = ?, addr_street = ?, addr_house = ?, addr_corpus = ?, addr_flat = ?, "
                    "addr_fias = ?, signature_image = ? "
                    "WHERE user_id = ? AND id = ?"
                ),
                (
                    short_name.strip(),
                    full_name.strip(),
                    (requisites or "").strip() or None,
                    (signatories or "").strip() or None,
                    (in_person or "").strip() or None,
                    (basis or "").strip() or None,
                    address_val,
                    (phone or "").strip() or None,
                    addr["addr_index"],
                    addr["addr_region_code"],
                    addr["addr_district"],
                    addr["addr_city"],
                    addr["addr_settlement"],
                    addr["addr_street"],
                    addr["addr_house"],
                    addr["addr_corpus"],
                    addr["addr_flat"],
                    addr["addr_fias"],
                    sig_val,
                    user_id,
                    entity_id,
                ),
            )
        return bool(result.rowcount)

    def create_supply_legal_entity(
        self,
        *,
        user_id: int,
        short_name: str,
        full_name: str,
        requisites: str = "",
        signatories: str = "",
        in_person: str = "",
        basis: str = "",
        address: str = "",
        phone: str = "",
        addr_index: str = "",
        addr_region_code: str = "",
        addr_district: str = "",
        addr_city: str = "",
        addr_settlement: str = "",
        addr_street: str = "",
        addr_house: str = "",
        addr_corpus: str = "",
        addr_flat: str = "",
        addr_fias: str = "",
        signature_image: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        addr = self._normalize_production_addr_fields(
            addr_index=addr_index,
            addr_region_code=addr_region_code,
            addr_district=addr_district,
            addr_city=addr_city,
            addr_settlement=addr_settlement,
            addr_street=addr_street,
            addr_house=addr_house,
            addr_corpus=addr_corpus,
            addr_flat=addr_flat,
            addr_fias=addr_fias,
        )
        composed = self.compose_production_address_line(addr)
        address_val = composed or str(address or "").strip() or None
        with self._connect() as conn:
            eid = self._insert_and_get_id(
                conn,
                "INSERT INTO supply_legal_entities "
                "(user_id, short_name, full_name, requisites, signatories, in_person, basis, address, phone, "
                "addr_index, addr_region_code, addr_district, addr_city, addr_settlement, "
                "addr_street, addr_house, addr_corpus, addr_flat, addr_fias, signature_image, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    short_name.strip(),
                    full_name.strip(),
                    (requisites or "").strip() or None,
                    (signatories or "").strip() or None,
                    (in_person or "").strip() or None,
                    (basis or "").strip() or None,
                    address_val,
                    (phone or "").strip() or None,
                    addr["addr_index"],
                    addr["addr_region_code"],
                    addr["addr_district"],
                    addr["addr_city"],
                    addr["addr_settlement"],
                    addr["addr_street"],
                    addr["addr_house"],
                    addr["addr_corpus"],
                    addr["addr_flat"],
                    addr["addr_fias"],
                    signature_image or None,
                    now,
                ),
            )
            row = conn.execute(self._sql("SELECT * FROM supply_legal_entities WHERE id = ?"), (eid,)).fetchone()
        if not row:
            return {"id": eid, "address": address_val or "", **addr}
        d = self._row_to_dict(row)
        d["address"] = self.legal_entity_address_line(d)
        return d


    def delete_supply_legal_entity(self, *, user_id: int, entity_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM supply_legal_entities WHERE user_id = ? AND id = ?"),
                (user_id, entity_id),
            )
        return bool(result.rowcount)

    # ── Supply Productions CRUD ──

    @staticmethod
    def _normalize_production_addr_fields(
        *,
        addr_index: str = "",
        addr_region_code: str = "",
        addr_district: str = "",
        addr_city: str = "",
        addr_settlement: str = "",
        addr_street: str = "",
        addr_house: str = "",
        addr_corpus: str = "",
        addr_flat: str = "",
        addr_fias: str = "",
    ) -> dict[str, str]:
        region = re.sub(r"\D", "", str(addr_region_code or ""))[:2]
        index = re.sub(r"\D", "", str(addr_index or ""))[:6]
        fias = re.sub(r"[{}\s]", "", str(addr_fias or "").strip()).lower()
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", fias):
            fias = ""
        return {
            "addr_index": index,
            "addr_region_code": region,
            "addr_district": str(addr_district or "").strip(),
            "addr_city": str(addr_city or "").strip(),
            "addr_settlement": str(addr_settlement or "").strip(),
            "addr_street": str(addr_street or "").strip(),
            "addr_house": str(addr_house or "").strip(),
            "addr_corpus": str(addr_corpus or "").strip(),
            "addr_flat": str(addr_flat or "").strip(),
            "addr_fias": fias,
        }

    @staticmethod
    def compose_production_address_line(fields: dict[str, str] | None = None, **extra: str) -> str:
        """Build a single display line from structured production address fields."""
        data = {**(fields or {}), **extra}
        parts: list[str] = []
        idx = str(data.get("addr_index") or "").strip()
        if idx:
            parts.append(idx)
        for key, prefix in (
            ("addr_district", ""),
            ("addr_city", "г. "),
            ("addr_settlement", ""),
            ("addr_street", ""),
            ("addr_house", "д. "),
            ("addr_corpus", "к. "),
            ("addr_flat", "кв. "),
        ):
            val = str(data.get(key) or "").strip()
            if not val:
                continue
            if key == "addr_city" and not re.match(r"^(г\.|город)\b", val, flags=re.I):
                val = f"{prefix}{val}"
            elif key == "addr_house" and not re.match(r"^(д\.|дом)\b", val, flags=re.I):
                val = f"{prefix}{val}"
            elif key == "addr_corpus" and not re.match(r"^(к\.|корп\.|корпус)\b", val, flags=re.I):
                val = f"{prefix}{val}"
            elif key == "addr_flat" and not re.match(r"^(кв\.|квартира)\b", val, flags=re.I):
                val = f"{prefix}{val}"
            parts.append(val)
        return ", ".join(parts)

    @classmethod
    def production_address_line(cls, prod: dict[str, Any] | None) -> str:
        """Full one-line address for TTN / PoA / заявки: structured fields or legacy address."""
        if not prod:
            return ""
        composed = cls.compose_production_address_line(prod)
        if composed:
            return composed
        return str(prod.get("address") or "").strip()

    def list_supply_productions(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("SELECT * FROM supply_productions WHERE user_id = ? ORDER BY name ASC"),
                (user_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            d = self._row_to_dict(r)
            # Always expose a ready one-line address for documents that expect a single string.
            d["address"] = self.production_address_line(d)
            result.append(d)
        return result

    def create_supply_production(
        self,
        *,
        user_id: int,
        name: str,
        head_name: str = "",
        address: str = "",
        load_contact: str = "",
        addr_index: str = "",
        addr_region_code: str = "",
        addr_district: str = "",
        addr_city: str = "",
        addr_settlement: str = "",
        addr_street: str = "",
        addr_house: str = "",
        addr_corpus: str = "",
        addr_flat: str = "",
    ) -> dict[str, Any]:
        now = _utc_now()
        addr = self._normalize_production_addr_fields(
            addr_index=addr_index,
            addr_region_code=addr_region_code,
            addr_district=addr_district,
            addr_city=addr_city,
            addr_settlement=addr_settlement,
            addr_street=addr_street,
            addr_house=addr_house,
            addr_corpus=addr_corpus,
            addr_flat=addr_flat,
        )
        composed = self.compose_production_address_line(addr)
        address_val = composed or str(address or "").strip()
        with self._connect() as conn:
            pid = self._insert_and_get_id(
                conn,
                "INSERT INTO supply_productions ("
                "user_id, name, head_name, address, load_contact, "
                "addr_index, addr_region_code, addr_district, addr_city, addr_settlement, "
                "addr_street, addr_house, addr_corpus, addr_flat, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    name.strip(),
                    head_name.strip(),
                    address_val,
                    load_contact.strip(),
                    addr["addr_index"],
                    addr["addr_region_code"],
                    addr["addr_district"],
                    addr["addr_city"],
                    addr["addr_settlement"],
                    addr["addr_street"],
                    addr["addr_house"],
                    addr["addr_corpus"],
                    addr["addr_flat"],
                    now,
                ),
            )
            row = conn.execute(self._sql("SELECT * FROM supply_productions WHERE id = ?"), (pid,)).fetchone()
        if not row:
            return {"id": pid, "address": address_val, **addr}
        d = self._row_to_dict(row)
        d["address"] = self.production_address_line(d)
        return d

    def update_supply_production(
        self,
        *,
        user_id: int,
        production_id: int,
        name: str,
        head_name: str = "",
        address: str = "",
        load_contact: str = "",
        addr_index: str = "",
        addr_region_code: str = "",
        addr_district: str = "",
        addr_city: str = "",
        addr_settlement: str = "",
        addr_street: str = "",
        addr_house: str = "",
        addr_corpus: str = "",
        addr_flat: str = "",
    ) -> bool:
        addr = self._normalize_production_addr_fields(
            addr_index=addr_index,
            addr_region_code=addr_region_code,
            addr_district=addr_district,
            addr_city=addr_city,
            addr_settlement=addr_settlement,
            addr_street=addr_street,
            addr_house=addr_house,
            addr_corpus=addr_corpus,
            addr_flat=addr_flat,
        )
        composed = self.compose_production_address_line(addr)
        address_val = composed or str(address or "").strip()
        with self._connect() as conn:
            result = conn.execute(
                self._sql(
                    "UPDATE supply_productions SET name = ?, head_name = ?, address = ?, load_contact = ?, "
                    "addr_index = ?, addr_region_code = ?, addr_district = ?, addr_city = ?, "
                    "addr_settlement = ?, addr_street = ?, addr_house = ?, addr_corpus = ?, addr_flat = ? "
                    "WHERE user_id = ? AND id = ?"
                ),
                (
                    name.strip(),
                    head_name.strip(),
                    address_val,
                    load_contact.strip(),
                    addr["addr_index"],
                    addr["addr_region_code"],
                    addr["addr_district"],
                    addr["addr_city"],
                    addr["addr_settlement"],
                    addr["addr_street"],
                    addr["addr_house"],
                    addr["addr_corpus"],
                    addr["addr_flat"],
                    user_id,
                    production_id,
                ),
            )
        return bool(result.rowcount)

    def delete_supply_production(self, *, user_id: int, production_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM supply_productions WHERE user_id = ? AND id = ?"),
                (user_id, production_id),
            )
            # Drop stock ledger / legacy snapshot rows for this production.
            try:
                self._ensure_supply_balances_tables(conn)
                conn.execute(
                    self._sql(
                        "DELETE FROM supply_stock_movements "
                        "WHERE user_id = ? AND production_id = ?"
                    ),
                    (user_id, int(production_id)),
                )
                conn.execute(
                    self._sql(
                        "DELETE FROM supply_balances "
                        "WHERE user_id = ? AND production_id = ?"
                    ),
                    (user_id, int(production_id)),
                )
            except Exception:
                pass
        return bool(result.rowcount)

    # ── Supply Contractors CRUD ──

    def list_supply_contractors(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("SELECT * FROM supply_contractors WHERE user_id = ? ORDER BY name ASC"),
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def create_supply_contractor(self, *, user_id: int, name: str, requisites: str = "") -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            cid = self._insert_and_get_id(
                conn,
                "INSERT INTO supply_contractors (user_id, name, requisites, created_at) VALUES (?, ?, ?, ?)",
                (user_id, name.strip(), requisites.strip(), now),
            )
            row = conn.execute(self._sql("SELECT * FROM supply_contractors WHERE id = ?"), (cid,)).fetchone()
        return self._row_to_dict(row) if row else {"id": cid}

    def update_supply_contractor(self, *, user_id: int, contractor_id: int, name: str, requisites: str = "") -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("UPDATE supply_contractors SET name = ?, requisites = ? WHERE user_id = ? AND id = ?"),
                (name.strip(), requisites.strip(), user_id, contractor_id),
            )
        return bool(result.rowcount)

    def delete_supply_contractor(self, *, user_id: int, contractor_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM supply_contractors WHERE user_id = ? AND id = ?"),
                (user_id, contractor_id),
            )
        return bool(result.rowcount)

    # ── OZON Supply Items CRUD ──

    def list_ozon_supply_items(self, *, user_id: int, source_id: int | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if source_id:
                rows = conn.execute(
                    self._sql("""SELECT oi.*, om.pallets_count, om.driver_name, om.notes, om.production
                        FROM ozon_supply_items oi
                        JOIN supply_sources ss ON ss.id = oi.source_id
                        LEFT JOIN ozon_supply_manual_data om ON om.user_id = ss.user_id AND om.supply_order_id = oi.supply_order_id
                        WHERE ss.user_id = ? AND oi.source_id = ?
                        ORDER BY oi.supply_date DESC NULLS LAST, oi.creation_date DESC"""),
                    (user_id, source_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    self._sql("""SELECT oi.*, om.pallets_count, om.driver_name, om.notes, om.production
                        FROM ozon_supply_items oi
                        JOIN supply_sources ss ON ss.id = oi.source_id
                        LEFT JOIN ozon_supply_manual_data om ON om.user_id = ss.user_id AND om.supply_order_id = oi.supply_order_id
                        WHERE ss.user_id = ?
                        ORDER BY oi.supply_date DESC NULLS LAST, oi.creation_date DESC"""),
                    (user_id,),
                ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def upsert_ozon_supply_item(self, *, source_id: int, data: dict[str, Any]) -> int:
        import json as _json
        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                self._sql("""INSERT INTO ozon_supply_items
                    (source_id, supply_order_id, supply_order_number, state, creation_date, supply_date,
                     warehouse_id, warehouse_name, transit_warehouse_name, is_crossdock,
                     total_quantity, creation_flow, supplier_name, raw_json, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_id, supply_order_id) DO UPDATE SET
                        supply_order_number = excluded.supply_order_number,
                        state = excluded.state,
                        creation_date = COALESCE(excluded.creation_date, ozon_supply_items.creation_date),
                        supply_date = COALESCE(excluded.supply_date, ozon_supply_items.supply_date),
                        warehouse_id = COALESCE(excluded.warehouse_id, ozon_supply_items.warehouse_id),
                        warehouse_name = COALESCE(excluded.warehouse_name, ozon_supply_items.warehouse_name),
                        transit_warehouse_name = excluded.transit_warehouse_name,
                        is_crossdock = excluded.is_crossdock,
                        total_quantity = CASE WHEN excluded.total_quantity > 0 THEN excluded.total_quantity ELSE ozon_supply_items.total_quantity END,
                        creation_flow = excluded.creation_flow,
                        supplier_name = COALESCE(excluded.supplier_name, ozon_supply_items.supplier_name),
                        raw_json = excluded.raw_json,
                        synced_at = excluded.synced_at
                    RETURNING id"""),
                (
                    source_id,
                    int(data.get("supply_order_id") or 0),
                    str(data.get("supply_order_number") or ""),
                    str(data.get("state") or ""),
                    str(data.get("creation_date") or "") or None,
                    str(data.get("supply_date") or "") or None,
                    int(data.get("dropoff_warehouse_id") or 0) or None,
                    str(data.get("warehouse_name") or "") or None,
                    str(data.get("transit_warehouse_name") or "") or None,
                    1 if data.get("is_crossdock") else 0,
                    int(data.get("total_quantity") or 0),
                    str(data.get("creation_flow") or "") or None,
                    str(data.get("supplier_name") or "") or None,
                    _json.dumps(data, ensure_ascii=False)[:8000],
                    now,
                ),
            ).fetchone()
        return int(self._row_to_dict(row)["id"]) if row else 0

    def upsert_ozon_supply_goods(self, *, supply_item_id: int, goods: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute(self._sql("DELETE FROM ozon_supply_goods WHERE supply_item_id = ?"), (supply_item_id,))
            for g in goods:
                conn.execute(
                    self._sql("INSERT INTO ozon_supply_goods (supply_item_id, sku, name, quantity, barcode, offer_id) VALUES (?, ?, ?, ?, ?, ?)"),
                    (supply_item_id, int(g.get("sku") or 0) or None, str(g.get("name") or "") or None,
                     int(g.get("quantity") or 0), str(g.get("barcode") or "") or None, str(g.get("offer_id") or "") or None),
                )

    def get_ozon_supply_goods(self, *, user_id: int, supply_order_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("""SELECT sg.* FROM ozon_supply_goods sg
                    JOIN ozon_supply_items oi ON oi.id = sg.supply_item_id
                    JOIN supply_sources ss ON ss.id = oi.source_id
                    WHERE ss.user_id = ? AND oi.supply_order_id = ?
                    ORDER BY sg.sku ASC NULLS LAST"""),
                (user_id, supply_order_id),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_ozon_supply_item_row(self, *, user_id: int, supply_order_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                self._sql("""SELECT oi.*, om.pallets_count, om.driver_name, om.notes, om.production
                    FROM ozon_supply_items oi
                    JOIN supply_sources ss ON ss.id = oi.source_id
                    LEFT JOIN ozon_supply_manual_data om
                      ON om.user_id = ss.user_id AND om.supply_order_id = oi.supply_order_id
                    WHERE ss.user_id = ? AND oi.supply_order_id = ? LIMIT 1"""),
                (user_id, supply_order_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def update_ozon_supply_cargoes(self, *, supply_order_id: int, cargoes_json: str) -> None:
        with self._connect() as conn:
            conn.execute(
                self._sql("UPDATE ozon_supply_items SET cargoes_json = ? WHERE supply_order_id = ?"),
                (cargoes_json, supply_order_id),
            )

    def update_ozon_supply_vehicle(self, *, supply_order_id: int, vehicle_json: str) -> None:
        with self._connect() as conn:
            conn.execute(
                self._sql("UPDATE ozon_supply_items SET vehicle_json = ? WHERE supply_order_id = ?"),
                (vehicle_json, supply_order_id),
            )

    def update_ozon_supply_total_quantity(self, *, supply_order_id: int, total_quantity: int) -> None:
        with self._connect() as conn:
            conn.execute(
                self._sql("UPDATE ozon_supply_items SET total_quantity = ? WHERE supply_order_id = ?"),
                (total_quantity, supply_order_id),
            )

    def update_ozon_supply_manual_fields(self, *, user_id: int, supply_order_id: int,
                                          pallets_count: str = "", driver_name: str = "",
                                          notes: str = "", production: str = "") -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                self._sql("""INSERT INTO ozon_supply_manual_data
                    (user_id, supply_order_id, pallets_count, driver_name, notes, production, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id, supply_order_id) DO UPDATE SET
                        pallets_count = excluded.pallets_count,
                        driver_name = excluded.driver_name,
                        notes = excluded.notes,
                        production = excluded.production,
                        updated_at = excluded.updated_at"""),
                (user_id, supply_order_id, pallets_count or None, driver_name or None,
                 notes or None, production or None, now),
            )

    def clear_ozon_supply_items(self, *, user_id: int) -> int:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("""DELETE FROM ozon_supply_items WHERE source_id IN
                    (SELECT id FROM supply_sources WHERE user_id = ?)"""),
                (user_id,),
            )
        return result.rowcount

    def get_ozon_giveout_barcode_reset_at(self, *, user_id: int, source_id: int) -> str | None:
        """Return ISO timestamp of last giveout barcode-reset, or None."""
        self._ensure_supply_tables()
        with self._connect() as conn:
            row = conn.execute(
                self._sql(
                    "SELECT reset_at FROM ozon_giveout_barcode_state "
                    "WHERE user_id = ? AND source_id = ?"
                ),
                (user_id, source_id),
            ).fetchone()
        if row is None:
            return None
        d = self._row_to_dict(row)
        value = str(d.get("reset_at") or "").strip()
        return value or None

    def set_ozon_giveout_barcode_reset_at(
        self,
        *,
        user_id: int,
        source_id: int,
        reset_at: str | None = None,
    ) -> str:
        """Upsert last giveout barcode-reset time. Returns stored ISO timestamp."""
        self._ensure_supply_tables()
        now = _utc_now()
        value = str(reset_at or now).strip() or now
        with self._connect() as conn:
            conn.execute(
                self._sql(
                    """
                    INSERT INTO ozon_giveout_barcode_state (user_id, source_id, reset_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (user_id, source_id) DO UPDATE SET
                        reset_at = excluded.reset_at,
                        updated_at = excluded.updated_at
                    """
                ),
                (user_id, source_id, value, now),
            )
        return value

    def delete_ozon_supply_items_not_in(
        self,
        *,
        source_id: int,
        keep_order_ids: list[int],
        delete_all_if_empty: bool = False,
    ) -> int:
        """Delete ozon_supply_items for this source whose supply_order_id is NOT in keep_order_ids.

        Used after sync to purge cancelled/deleted supplies that Ozon no longer returns
        in the active list. When keep_order_ids is empty, deletes nothing unless
        delete_all_if_empty=True (list API succeeded with zero orders).
        """
        keep_ids = [int(x) for x in keep_order_ids if int(x) > 0]
        with self._connect() as conn:
            if not keep_ids:
                if not delete_all_if_empty:
                    return 0
                result = conn.execute(
                    self._sql("DELETE FROM ozon_supply_items WHERE source_id = ?"),
                    (source_id,),
                )
                return int(result.rowcount or 0)
            placeholders = ",".join(["?" for _ in keep_ids])
            result = conn.execute(
                self._sql(
                    f"DELETE FROM ozon_supply_items WHERE source_id = ? "
                    f"AND supply_order_id NOT IN ({placeholders})"
                ),
                [source_id, *keep_ids],
            )
        return int(result.rowcount or 0)

    # ── Supply PoA Records CRUD ──

    def list_supply_poa_records(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("""
                    SELECT p.id, p.user_id, p.poa_date, p.created_at,
                           p.legal_entity_id, le.short_name AS le_short, le.full_name AS le_full,
                           le.requisites AS le_req, le.in_person AS le_in_person,
                           le.basis AS le_basis, le.signatories AS le_signatories,
                           le.signature_image AS le_signature_image,
                           p.contractor_id, c.name AS c_name, c.requisites AS c_req,
                           p.driver_id, d.full_name AS d_full, d.documents AS d_docs, d.in_person AS d_in_person,
                           d.doc_vu_series AS d_vu_series, d.doc_vu_number AS d_vu_number,
                           d.doc_vu_issuer AS d_vu_issuer, d.doc_vu_date AS d_vu_date,
                           d.doc_inn_fl AS d_inn_fl,
                           p.driver_manual_name, p.driver_manual_docs
                    FROM supply_poa_records p
                    LEFT JOIN supply_legal_entities le ON le.id = p.legal_entity_id
                    LEFT JOIN supply_contractors c ON c.id = p.contractor_id
                    LEFT JOIN supply_drivers d ON d.id = p.driver_id AND p.driver_id > 0
                    WHERE p.user_id = ?
                    ORDER BY p.created_at DESC
                """),
                (user_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            d = self._row_to_dict(row)
            # Prefer structured VU fields when legacy documents column is empty.
            d["d_docs"] = self.driver_documents_line(
                {
                    "documents": d.get("d_docs"),
                    "doc_vu_series": d.get("d_vu_series"),
                    "doc_vu_number": d.get("d_vu_number"),
                    "doc_vu_issuer": d.get("d_vu_issuer"),
                    "doc_vu_date": d.get("d_vu_date"),
                    "doc_inn_fl": d.get("d_inn_fl"),
                }
            )
            result.append(d)
        return result

    def create_supply_poa_record(self, *, user_id: int, legal_entity_id: int, contractor_id: int, driver_id: int = 0, poa_date: str, driver_manual_name: str = "", driver_manual_docs: str = "") -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            rid = self._insert_and_get_id(
                conn,
                "INSERT INTO supply_poa_records (user_id, legal_entity_id, contractor_id, driver_id, poa_date, driver_manual_name, driver_manual_docs, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, legal_entity_id, contractor_id, driver_id or 0, poa_date, driver_manual_name.strip() or None, driver_manual_docs.strip() or None, now),
            )
        records = self.list_supply_poa_records(user_id=user_id)
        return next((r for r in records if r["id"] == rid), {"id": rid})

    def update_supply_poa_record(self, *, user_id: int, record_id: int, legal_entity_id: int, contractor_id: int, driver_id: int = 0, driver_manual_name: str = "", driver_manual_docs: str = "") -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("UPDATE supply_poa_records SET legal_entity_id = ?, contractor_id = ?, driver_id = ?, driver_manual_name = ?, driver_manual_docs = ? WHERE user_id = ? AND id = ?"),
                (legal_entity_id, contractor_id, driver_id or 0, driver_manual_name.strip() or None, driver_manual_docs.strip() or None, user_id, record_id),
            )
        return bool(result.rowcount)

    def delete_supply_poa_record(self, *, user_id: int, record_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM supply_poa_records WHERE user_id = ? AND id = ?"),
                (user_id, record_id),
            )
        return bool(result.rowcount)

    # ── Certificates CRUD ──

    def list_certificates(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("SELECT * FROM supply_certificates WHERE user_id = ? ORDER BY expiry_date ASC, id DESC"),
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def create_certificate(self, *, user_id: int, legal_entity_short: str, category: str,
                           number: str, expiry_date: str, verification_url: str,
                           image_data: str | None, doc_type: str = "Сертификат соответствия") -> int:
        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                self._sql(
                    "INSERT INTO supply_certificates "
                    "(user_id, legal_entity_short, category, number, expiry_date, verification_url, image_data, doc_type, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id"
                ),
                (user_id, legal_entity_short.strip(), category.strip(), number.strip(),
                 expiry_date.strip(), verification_url.strip(), image_data,
                 doc_type.strip() or "Сертификат соответствия", now),
            ).fetchone()
        return int(self._row_to_dict(row)["id"]) if row else 0

    def update_certificate(self, *, user_id: int, cert_id: int, legal_entity_short: str,
                           category: str, number: str, expiry_date: str,
                           verification_url: str, image_data: str | None,
                           doc_type: str = "Сертификат соответствия") -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql(
                    "UPDATE supply_certificates SET legal_entity_short=?, category=?, number=?, "
                    "expiry_date=?, verification_url=?, doc_type=?, "
                    "image_data=COALESCE(?, image_data) "
                    "WHERE user_id=? AND id=?"
                ),
                (legal_entity_short.strip(), category.strip(), number.strip(),
                 expiry_date.strip(), verification_url.strip(),
                 doc_type.strip() or "Сертификат соответствия",
                 image_data, user_id, cert_id),
            )
        return bool(result.rowcount)

    def delete_certificate(self, *, user_id: int, cert_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM supply_certificates WHERE user_id = ? AND id = ?"),
                (user_id, cert_id),
            )
        return bool(result.rowcount)

    def next_ttn_number(self) -> int:
        """Return next sequential TTN number for today; resets to 1 each new day."""
        today = _utc_now()[:10]  # YYYY-MM-DD
        with self._connect() as conn:
            conn.execute(
                self._sql(
                    "INSERT INTO ttn_counter (date, n) VALUES (?, 1) "
                    "ON CONFLICT(date) DO UPDATE SET n = ttn_counter.n + 1"
                ),
                (today,),
            )
            row = conn.execute(
                self._sql("SELECT n FROM ttn_counter WHERE date = ?"), (today,)
            ).fetchone()
        if not row:
            return 1
        try:
            return int(row["n"])
        except (KeyError, TypeError):
            return int(row[0]) if hasattr(row, "__getitem__") else 1

    def get_legal_entity_map(self, *, user_id: int) -> dict[str, str]:
        """Return {short_name: full_name} for lookups."""
        rows = self.list_supply_legal_entities(user_id=user_id)
        return {r["short_name"]: r["full_name"] for r in rows if r.get("short_name")}

    # ── Supply Sources CRUD ──

    def list_supply_sources(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                self._sql("SELECT * FROM supply_sources WHERE user_id = ? ORDER BY id ASC"),
                (user_id,),
            ).fetchall()
        result = []
        for row in rows:
            d = self._row_to_dict(row)
            encrypted = str(d.pop("api_key_encrypted") or "")
            key = decrypt_secret(encrypted) if encrypted else None
            d["api_key_preview"] = mask_secret(key)
            d["has_api_key"] = bool(key)
            d["is_enabled"] = bool(d.get("is_enabled"))
            result.append(d)
        return result

    def get_supply_source_with_key(self, *, user_id: int, source_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                self._sql("SELECT * FROM supply_sources WHERE user_id = ? AND id = ?"),
                (user_id, source_id),
            ).fetchone()
        if row is None:
            return None
        d = self._row_to_dict(row)
        encrypted = str(d.pop("api_key_encrypted") or "")
        d["api_key"] = decrypt_secret(encrypted) if encrypted else None
        return d

    def create_supply_source(self, *, user_id: int, name: str, api_key: str, marketplace: str = "wb", client_id: str = "") -> dict[str, Any]:
        now = _utc_now()
        mp = marketplace.strip().lower() if marketplace else "wb"
        with self._connect() as conn:
            source_id = self._insert_and_get_id(
                conn,
                """
                INSERT INTO supply_sources (user_id, name, api_key_encrypted, marketplace, client_id, is_enabled, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (user_id, name.strip(), encrypt_secret(api_key.strip()), mp, client_id.strip() or None, now),
            )
            row = conn.execute(
                self._sql("SELECT * FROM supply_sources WHERE id = ?"),
                (source_id,),
            ).fetchone()
        d = self._row_to_dict(row) if row else {"id": source_id}
        d.pop("api_key_encrypted", None)
        d["api_key_preview"] = mask_secret(api_key.strip())
        d["has_api_key"] = True
        return d

    def toggle_supply_source(self, *, user_id: int, source_id: int, is_enabled: bool) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("UPDATE supply_sources SET is_enabled = ? WHERE user_id = ? AND id = ?"),
                (1 if is_enabled else 0, user_id, source_id),
            )
        return bool(result.rowcount)

    def delete_supply_source(self, *, user_id: int, source_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                self._sql("DELETE FROM supply_sources WHERE user_id = ? AND id = ?"),
                (user_id, source_id),
            )
        return bool(result.rowcount)

    def mark_supply_source_synced(self, *, source_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                self._sql("UPDATE supply_sources SET last_synced_at = ? WHERE id = ?"),
                (_utc_now(), source_id),
            )

    # ── Contour EDO settings / Ozon EDO documents ──

    def get_supply_edo_settings(self, *, user_id: int, include_secrets: bool = False) -> dict[str, Any]:
        with self._connect() as conn:
            self._migrate_supply_tables(conn)
            row = conn.execute(
                self._sql("SELECT * FROM supply_edo_settings WHERE user_id = ?"),
                (user_id,),
            ).fetchone()
        if not row:
            return {
                "user_id": user_id,
                "api_url": "https://logist-api.kontur.ru/",
                "has_api_key": False,
                "api_key_preview": "",
                "diadoc_url": "https://diadoc-api.kontur.ru/",
                "diadoc_client_id": "",
                "diadoc_login": "",
                "has_diadoc_password": False,
                "diadoc_password_preview": "",
                "diadoc_from_box_id": "",
                "diadoc_to_box_id": "",
                "cert_thumbprint": "",
                "is_enabled": False,
            }
        d = self._row_to_dict(row)
        api_enc = str(d.pop("api_key_encrypted", None) or "")
        pwd_enc = str(d.pop("diadoc_password_encrypted", None) or "")
        api_key = decrypt_secret(api_enc) if api_enc else ""
        pwd = decrypt_secret(pwd_enc) if pwd_enc else ""
        out = {
            "user_id": user_id,
            "api_url": str(d.get("api_url") or "https://logist-api.kontur.ru/"),
            "has_api_key": bool(api_key),
            "api_key_preview": mask_secret(api_key) if api_key else "",
            "diadoc_url": str(d.get("diadoc_url") or "https://diadoc-api.kontur.ru/"),
            "diadoc_client_id": str(d.get("diadoc_client_id") or ""),
            "diadoc_login": str(d.get("diadoc_login") or ""),
            "has_diadoc_password": bool(pwd),
            "diadoc_password_preview": mask_secret(pwd) if pwd else "",
            "diadoc_from_box_id": str(d.get("diadoc_from_box_id") or ""),
            "diadoc_to_box_id": str(d.get("diadoc_to_box_id") or ""),
            "cert_thumbprint": str(d.get("cert_thumbprint") or ""),
            "is_enabled": bool(int(d.get("is_enabled") or 0)),
            "updated_at": str(d.get("updated_at") or ""),
        }
        if include_secrets:
            out["api_key"] = api_key or ""
            out["diadoc_password"] = pwd or ""
        return out

    def upsert_supply_edo_settings(
        self,
        *,
        user_id: int,
        api_url: str = "",
        api_key: str | None = None,
        diadoc_url: str = "",
        diadoc_client_id: str = "",
        diadoc_login: str = "",
        diadoc_password: str | None = None,
        diadoc_from_box_id: str = "",
        diadoc_to_box_id: str = "",
        cert_thumbprint: str = "",
        is_enabled: bool = True,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            self._migrate_supply_tables(conn)
            existing = conn.execute(
                self._sql("SELECT * FROM supply_edo_settings WHERE user_id = ?"),
                (user_id,),
            ).fetchone()
            prev = self._row_to_dict(existing) if existing else {}
            api_enc = str(prev.get("api_key_encrypted") or "")
            pwd_enc = str(prev.get("diadoc_password_encrypted") or "")
            if api_key is not None:
                clean = str(api_key).strip()
                api_enc = encrypt_secret(clean) if clean else None
            if diadoc_password is not None:
                clean_pwd = str(diadoc_password)
                pwd_enc = encrypt_secret(clean_pwd) if clean_pwd.strip() else None
            if existing:
                conn.execute(
                    self._sql(
                        "UPDATE supply_edo_settings SET api_url = ?, api_key_encrypted = ?, "
                        "diadoc_url = ?, diadoc_client_id = ?, diadoc_login = ?, diadoc_password_encrypted = ?, "
                        "diadoc_from_box_id = ?, diadoc_to_box_id = ?, cert_thumbprint = ?, "
                        "is_enabled = ?, updated_at = ? WHERE user_id = ?"
                    ),
                    (
                        (api_url or "").strip() or "https://logist-api.kontur.ru/",
                        api_enc,
                        (diadoc_url or "").strip() or "https://diadoc-api.kontur.ru/",
                        (diadoc_client_id or "").strip(),
                        (diadoc_login or "").strip(),
                        pwd_enc,
                        (diadoc_from_box_id or "").strip(),
                        (diadoc_to_box_id or "").strip(),
                        (cert_thumbprint or "").strip(),
                        1 if is_enabled else 0,
                        now,
                        user_id,
                    ),
                )
            else:
                conn.execute(
                    self._sql(
                        "INSERT INTO supply_edo_settings ("
                        "user_id, api_url, api_key_encrypted, diadoc_url, diadoc_client_id, diadoc_login, "
                        "diadoc_password_encrypted, diadoc_from_box_id, diadoc_to_box_id, cert_thumbprint, "
                        "is_enabled, updated_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        user_id,
                        (api_url or "").strip() or "https://logist-api.kontur.ru/",
                        api_enc,
                        (diadoc_url or "").strip() or "https://diadoc-api.kontur.ru/",
                        (diadoc_client_id or "").strip(),
                        (diadoc_login or "").strip(),
                        pwd_enc,
                        (diadoc_from_box_id or "").strip(),
                        (diadoc_to_box_id or "").strip(),
                        (cert_thumbprint or "").strip(),
                        1 if is_enabled else 0,
                        now,
                    ),
                )
        return self.get_supply_edo_settings(user_id=user_id)

    def list_ozon_edo_documents(self, *, user_id: int, supply_order_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._migrate_supply_tables(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT * FROM ozon_edo_documents WHERE user_id = ? AND supply_order_id = ? "
                    "ORDER BY doc_type ASC"
                ),
                (user_id, supply_order_id),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def upsert_ozon_edo_document(
        self,
        *,
        user_id: int,
        supply_order_id: int,
        doc_type: str,
        channel: str = "",
        transportation_id: str = "",
        message_id: str = "",
        entity_id: str = "",
        status: str = "",
        status_label: str = "",
        mintrans_id: str = "",
        mintrans_status: str = "",
        last_error: str = "",
        raw_json: str = "",
        mark_sent: bool = False,
    ) -> dict[str, Any]:
        now = _utc_now()
        doc_type = str(doc_type or "").strip().lower()
        with self._connect() as conn:
            self._migrate_supply_tables(conn)
            existing = conn.execute(
                self._sql(
                    "SELECT * FROM ozon_edo_documents "
                    "WHERE user_id = ? AND supply_order_id = ? AND doc_type = ?"
                ),
                (user_id, supply_order_id, doc_type),
            ).fetchone()
            if existing:
                prev = self._row_to_dict(existing)
                sent_at = str(prev.get("sent_at") or "")
                if mark_sent:
                    sent_at = now
                conn.execute(
                    self._sql(
                        "UPDATE ozon_edo_documents SET channel = ?, transportation_id = ?, message_id = ?, "
                        "entity_id = ?, status = ?, status_label = ?, mintrans_id = ?, mintrans_status = ?, "
                        "last_error = ?, raw_json = ?, sent_at = ?, updated_at = ? "
                        "WHERE user_id = ? AND supply_order_id = ? AND doc_type = ?"
                    ),
                    (
                        channel or str(prev.get("channel") or ""),
                        transportation_id or str(prev.get("transportation_id") or ""),
                        message_id or str(prev.get("message_id") or ""),
                        entity_id or str(prev.get("entity_id") or ""),
                        status,
                        status_label,
                        mintrans_id or str(prev.get("mintrans_id") or ""),
                        mintrans_status or str(prev.get("mintrans_status") or ""),
                        last_error,
                        raw_json or str(prev.get("raw_json") or ""),
                        sent_at,
                        now,
                        user_id,
                        supply_order_id,
                        doc_type,
                    ),
                )
            else:
                conn.execute(
                    self._sql(
                        "INSERT INTO ozon_edo_documents ("
                        "user_id, supply_order_id, doc_type, channel, transportation_id, message_id, entity_id, "
                        "status, status_label, mintrans_id, mintrans_status, last_error, raw_json, sent_at, updated_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        user_id,
                        supply_order_id,
                        doc_type,
                        channel,
                        transportation_id,
                        message_id,
                        entity_id,
                        status,
                        status_label,
                        mintrans_id,
                        mintrans_status,
                        last_error,
                        raw_json,
                        now if mark_sent else "",
                        now,
                    ),
                )
            row = conn.execute(
                self._sql(
                    "SELECT * FROM ozon_edo_documents "
                    "WHERE user_id = ? AND supply_order_id = ? AND doc_type = ?"
                ),
                (user_id, supply_order_id, doc_type),
            ).fetchone()
        return self._row_to_dict(row) if row else {}

    # ── Supply Items CRUD ──

    def upsert_supply_item(self, *, source_id: int, data: dict[str, Any]) -> int:
        """Insert or update a supply item. Returns the internal DB id."""
        import json as _json
        now = _utc_now()
        raw_json = _json.dumps(data)

        def _ts(val: str | None) -> str | None:
            if not val:
                return None
            try:
                from datetime import datetime as _dt
                raw = str(val).strip()
                if raw.endswith("Z"):
                    raw = raw[:-1] + "+00:00"
                _dt.fromisoformat(raw)
                return raw
            except Exception:
                return None

        with self._connect() as conn:
            row = conn.execute(
                self._sql(
                    """
                    INSERT INTO supply_items (
                        source_id, supply_id, preorder_id, status_id, box_type_id,
                        warehouse_id, warehouse_name, transit_warehouse_name, actual_warehouse_name,
                        create_date, supply_date, fact_date,
                        quantity, accepted_quantity, ready_for_sale_quantity,
                        acceptance_cost, storage_coef, delivery_coef, supplier_name,
                        raw_json, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_id, supply_id) DO UPDATE SET
                        status_id = excluded.status_id,
                        box_type_id = excluded.box_type_id,
                        warehouse_id = COALESCE(excluded.warehouse_id, supply_items.warehouse_id),
                        warehouse_name = COALESCE(excluded.warehouse_name, supply_items.warehouse_name),
                        transit_warehouse_name = COALESCE(excluded.transit_warehouse_name, supply_items.transit_warehouse_name),
                        actual_warehouse_name = COALESCE(excluded.actual_warehouse_name, supply_items.actual_warehouse_name),
                        supply_date = COALESCE(excluded.supply_date, supply_items.supply_date),
                        fact_date = COALESCE(excluded.fact_date, supply_items.fact_date),
                        quantity = CASE WHEN excluded.quantity > 0 THEN excluded.quantity ELSE supply_items.quantity END,
                        accepted_quantity = excluded.accepted_quantity,
                        ready_for_sale_quantity = excluded.ready_for_sale_quantity,
                        acceptance_cost = COALESCE(excluded.acceptance_cost, supply_items.acceptance_cost),
                        storage_coef = excluded.storage_coef,
                        delivery_coef = excluded.delivery_coef,
                        supplier_name = COALESCE(excluded.supplier_name, supply_items.supplier_name),
                        raw_json = excluded.raw_json,
                        synced_at = excluded.synced_at
                    RETURNING id
                    """
                ),
                (
                    source_id,
                    int(data.get("supplyID") or 0),
                    int(data.get("preorderID") or 0) or None,
                    int(data.get("statusID") or 0) or None,
                    int(data.get("boxTypeID") or 0) or None,
                    int(data.get("warehouseID") or 0) or None,
                    str(data.get("warehouseName") or "") or None,
                    str(data.get("transitWarehouseName") or "") or None,
                    str(data.get("actualWarehouseName") or "") or None,
                    _ts(data.get("createDate")),
                    _ts(data.get("supplyDate")),
                    _ts(data.get("factDate")),
                    int(data.get("quantity") or 0),
                    int(data.get("acceptedQuantity") or 0),
                    int(data.get("readyForSaleQuantity") or 0),
                    str(data.get("acceptanceCost")) if data.get("acceptanceCost") is not None else None,
                    str(data.get("storageCoef") or "") or None,
                    str(data.get("deliveryCoef") or "") or None,
                    str(data.get("supplierAssignName") or "") or None,
                    raw_json,
                    now,
                ),
            ).fetchone()
        return int(row["id"])

    def upsert_supply_goods(self, *, supply_item_id: int, goods: list[dict[str, Any]]) -> None:
        """Replace all goods for a supply item."""
        with self._connect() as conn:
            conn.execute(
                self._sql("DELETE FROM supply_goods WHERE supply_item_id = ?"),
                (supply_item_id,),
            )
            for g in goods:
                conn.execute(
                    self._sql(
                        """
                        INSERT INTO supply_goods
                            (supply_item_id, nm_id, vendor_code, barcode, tech_size, color,
                             quantity, accepted_quantity, tnved)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        supply_item_id,
                        int(g.get("nmID") or 0) or None,
                        str(g.get("vendorCode") or "") or None,
                        str(g.get("barcode") or "") or None,
                        str(g.get("techSize") or "") or None,
                        str(g.get("color") or "") or None,
                        int(g.get("quantity") or 0),
                        int(g.get("acceptedQuantity") or 0),
                        str(g.get("tnved") or "") or None,
                    ),
                )

    def list_supply_warehouse_names(self, *, user_id: int, source_id: int | None = None) -> list[str]:
        """Distinct destination warehouse names for WB supplies (filter dropdown)."""
        conditions = ["ss.user_id = ?", "COALESCE(TRIM(si.warehouse_name), '') <> ''"]
        params: list[Any] = [user_id]
        if source_id:
            conditions.append("si.source_id = ?")
            params.append(source_id)
        where = "WHERE " + " AND ".join(conditions)
        with self._connect() as conn:
            rows = conn.execute(
                self._sql(
                    f"""
                    SELECT DISTINCT TRIM(si.warehouse_name) AS warehouse_name
                    FROM supply_items si
                    JOIN supply_sources ss ON ss.id = si.source_id
                    {where}
                    ORDER BY 1 ASC
                    """
                ),
                tuple(params),
            ).fetchall()
        return [str(r["warehouse_name"]) for r in rows if r and r.get("warehouse_name")]

    def list_supply_items(
        self,
        *,
        user_id: int,
        source_id: int | None = None,
        status_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        production: str | None = None,
        warehouse: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        conditions = ["ss.user_id = ?"]
        params: list[Any] = [user_id]
        if source_id:
            conditions.append("si.source_id = ?")
            params.append(source_id)
        if status_id is not None:
            conditions.append("si.status_id = ?")
            params.append(status_id)
        if date_from:
            conditions.append("LEFT(si.supply_date, 10) >= ?")
            params.append(date_from[:10])
        if date_to:
            conditions.append("LEFT(si.supply_date, 10) <= ?")
            params.append(date_to[:10])
        if production:
            conditions.append("si.production = ?")
            params.append(production)
        if warehouse:
            conditions.append("TRIM(si.warehouse_name) = ?")
            params.append(warehouse.strip())
        if search:
            s = f"%{search.strip()}%"
            conditions.append(
                "(CAST(si.supply_id AS TEXT) LIKE ? OR LOWER(si.warehouse_name) LIKE LOWER(?)"
                " OR LOWER(si.supplier_name) LIKE LOWER(?))"
            )
            params.extend([s, s, s])
        where = "WHERE " + " AND ".join(conditions)
        offset = (max(1, page) - 1) * page_size
        with self._connect() as conn:
            total_row = conn.execute(
                self._sql(
                    f"""
                    SELECT COUNT(*) AS cnt
                    FROM supply_items si
                    JOIN supply_sources ss ON ss.id = si.source_id
                    {where}
                    """
                ),
                tuple(params),
            ).fetchone()
            total = int((total_row or {}).get("cnt") or 0)
            rows = conn.execute(
                self._sql(
                    f"""
                    SELECT si.*, ss.name AS source_name
                    FROM supply_items si
                    JOIN supply_sources ss ON ss.id = si.source_id
                    {where}
                    ORDER BY si.supply_date DESC, si.supply_id DESC
                    LIMIT ? OFFSET ?
                    """
                ),
                tuple(params) + (page_size, offset),
            ).fetchall()
        items = []
        for row in rows:
            d = self._row_to_dict(row)
            d.pop("raw_json", None)
            items.append(d)
        warehouses = self.list_supply_warehouse_names(user_id=user_id, source_id=source_id)
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "warehouses": warehouses,
        }

    def clear_supply_items(self, *, user_id: int) -> int:
        """Delete all supply items (and cascaded goods) for this user. Returns deleted count."""
        with self._connect() as conn:
            result = conn.execute(
                self._sql(
                    """
                    DELETE FROM supply_items
                    WHERE source_id IN (SELECT id FROM supply_sources WHERE user_id = ?)
                    """
                ),
                (user_id,),
            )
        return int(result.rowcount or 0)

    def delete_supply_items_not_in(self, *, source_id: int, keep_supply_ids: list[int]) -> int:
        """Delete supply_items for this source whose supply_wb_id is NOT in keep_supply_ids.
        Used after sync to purge cancelled / removed supplies. Returns deleted count."""
        if not keep_supply_ids:
            return 0
        placeholders = ",".join(["?" for _ in keep_supply_ids])
        with self._connect() as conn:
            result = conn.execute(
                self._sql(
                    f"DELETE FROM supply_items WHERE source_id = ? AND supply_id NOT IN ({placeholders})"
                ),
                [source_id, *keep_supply_ids],
            )
        return int(result.rowcount or 0)

    def update_supply_manual_fields(
        self,
        *,
        user_id: int,
        supply_id: int,
        pass_number: str | None,
        pallets_count: str | None,
        driver_name: str | None,
        notes: str | None = None,
        production: str | None = None,
        drivers_json: str | None = None,
    ) -> bool:
        """Update user-editable fields in supply_items AND persist to supply_manual_data."""
        import json as _j
        now = _utc_now()
        # drivers_json takes priority; derive legacy fields from first slot for back-compat
        if drivers_json:
            try:
                slots = _j.loads(drivers_json)
                if slots:
                    pn = (slots[0].get("pass_number") or "").strip() or None
                    pc = (slots[0].get("pallets_count") or "").strip() or None
                    dn = (slots[0].get("driver_name") or "").strip() or None
                else:
                    pn = pc = dn = None
            except Exception:
                pn = (pass_number or "").strip() or None
                pc = (pallets_count or "").strip() or None
                dn = (driver_name or "").strip() or None
        else:
            pn = (pass_number or "").strip() or None
            pc = (pallets_count or "").strip() or None
            dn = (driver_name or "").strip() or None
        nt = (notes or "").strip() or None
        pr = (production or "").strip() or None
        dj = drivers_json or None
        with self._connect() as conn:
            result = conn.execute(
                self._sql(
                    """
                    UPDATE supply_items
                    SET pass_number = ?, pallets_count = ?, driver_name = ?,
                        notes = ?, production = ?, drivers_json = ?
                    WHERE supply_id = ?
                      AND source_id IN (SELECT id FROM supply_sources WHERE user_id = ?)
                    """
                ),
                (pn, pc, dn, nt, pr, dj, supply_id, user_id),
            )
            conn.execute(
                self._sql(
                    """
                    INSERT INTO supply_manual_data
                        (user_id, supply_id, pass_number, pallets_count, driver_name,
                         notes, production, drivers_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id, supply_id) DO UPDATE SET
                        pass_number   = excluded.pass_number,
                        pallets_count = excluded.pallets_count,
                        driver_name   = excluded.driver_name,
                        notes         = excluded.notes,
                        production    = excluded.production,
                        drivers_json  = excluded.drivers_json,
                        updated_at    = excluded.updated_at
                    """
                ),
                (user_id, supply_id, pn, pc, dn, nt, pr, dj, now),
            )
        return bool(result.rowcount)

    def restore_supply_manual_fields(self, *, user_id: int) -> int:
        """After sync, copy saved manual data back to supply_items rows.
        Returns number of rows updated."""
        with self._connect() as conn:
            result = conn.execute(
                self._sql(
                    """
                    UPDATE supply_items si
                    SET
                        pass_number   = smd.pass_number,
                        pallets_count = smd.pallets_count,
                        driver_name   = smd.driver_name,
                        notes         = smd.notes,
                        production    = smd.production,
                        drivers_json  = smd.drivers_json
                    FROM supply_manual_data smd
                    WHERE smd.user_id   = ?
                      AND smd.supply_id = si.supply_id
                      AND si.source_id IN (
                          SELECT id FROM supply_sources WHERE user_id = ?
                      )
                      AND (
                          smd.pass_number   IS NOT NULL
                       OR smd.pallets_count IS NOT NULL
                       OR smd.driver_name   IS NOT NULL
                       OR smd.notes         IS NOT NULL
                       OR smd.production    IS NOT NULL
                       OR smd.drivers_json  IS NOT NULL
                      )
                    """
                ),
                (user_id, user_id),
            )
        return int(result.rowcount or 0)

    def get_supply_item_row(self, *, user_id: int, supply_id: int) -> dict[str, Any] | None:
        """Get supply_items row verifying ownership."""
        with self._connect() as conn:
            row = conn.execute(
                self._sql(
                    """
                    SELECT si.*
                    FROM supply_items si
                    JOIN supply_sources ss ON ss.id = si.source_id
                    WHERE ss.user_id = ? AND si.supply_id = ?
                    LIMIT 1
                    """
                ),
                (user_id, supply_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_supply_goods(self, *, user_id: int, supply_id: int) -> list[dict[str, Any]]:
        """Get goods for a supply, verifying ownership via source."""
        with self._connect() as conn:
            rows = conn.execute(
                self._sql(
                    """
                    SELECT sg.*
                    FROM supply_goods sg
                    JOIN supply_items si ON si.id = sg.supply_item_id
                    JOIN supply_sources ss ON ss.id = si.source_id
                    WHERE ss.user_id = ? AND si.supply_id = ?
                    ORDER BY sg.nm_id ASC NULLS LAST
                    """
                ),
                (user_id, supply_id),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ── Supply user permission ──

    def set_user_can_supplies(self, *, user_id: int, can_supplies: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                self._sql("UPDATE users SET can_supplies = ? WHERE id = ?"),
                (self._bool_db(can_supplies), user_id),
            )

    def get_user_can_supplies(self, *, user_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                self._sql("SELECT can_supplies FROM users WHERE id = ?"),
                (user_id,),
            ).fetchone()
        if row is None:
            return False
        return bool((row.get("can_supplies") if hasattr(row, "get") else row[0]))  # type: ignore[index]

    def set_user_can_salary(
        self, *, user_id: int, can_salary: bool,
        can_salary_settings: bool = False,
        can_salary_report: bool = False,
        can_salary_zp_export: bool = False,
        salary_productions: list[str] | None = None,
    ) -> None:
        import json as _j
        prods_json = _j.dumps(list(salary_productions or []), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                self._sql(
                    "UPDATE users SET can_salary = ?, can_salary_settings = ?, "
                    "can_salary_report = ?, can_salary_zp_export = ?, salary_productions = ? WHERE id = ?"
                ),
                (self._bool_db(can_salary), self._bool_db(can_salary_settings),
                 self._bool_db(can_salary_report), self._bool_db(can_salary_zp_export),
                 prods_json, user_id),
            )

    def get_user_can_salary(self, *, user_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                self._sql("SELECT can_salary FROM users WHERE id = ?"),
                (user_id,),
            ).fetchone()
        if row is None:
            return False
        return bool((row.get("can_salary") if hasattr(row, "get") else row[0]))  # type: ignore[index]

    def get_manager_supply_permissions(self, *, manager_user_id: int) -> dict[str, Any]:
        import json as _j
        with self._connect() as conn:
            row = conn.execute(
                self._sql("SELECT * FROM manager_supply_permissions WHERE manager_user_id = ?"),
                (manager_user_id,),
            ).fetchone()
        if row is None:
            return {"can_supply_settings": False, "can_supply_poa": False, "can_supply_certs": False, "sources": {}}
        d = self._row_to_dict(row)
        try:
            sources = _j.loads(d.get("sources_json") or "{}")
        except Exception:
            sources = {}
        return {
            "can_supply_settings": bool(d.get("can_supply_settings")),
            "can_supply_poa": bool(d.get("can_supply_poa")),
            "can_supply_certs": bool(d.get("can_supply_certs")),
            "sources": sources,
        }

    def set_manager_supply_permissions(self, *, manager_user_id: int,
                                        can_supply_settings: bool,
                                        can_supply_poa: bool,
                                        can_supply_certs: bool = False,
                                        sources: dict) -> None:
        import json as _j
        now = _utc_now()
        sources_json = _j.dumps(sources, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute("ALTER TABLE manager_supply_permissions ADD COLUMN IF NOT EXISTS can_supply_certs INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                self._sql("""INSERT INTO manager_supply_permissions
                    (manager_user_id, can_supply_settings, can_supply_poa, can_supply_certs, sources_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (manager_user_id) DO UPDATE SET
                        can_supply_settings = excluded.can_supply_settings,
                        can_supply_poa = excluded.can_supply_poa,
                        can_supply_certs = excluded.can_supply_certs,
                        sources_json = excluded.sources_json,
                        updated_at = excluded.updated_at"""),
                (manager_user_id,
                 self._bool_db(can_supply_settings),
                 self._bool_db(can_supply_poa),
                 1 if can_supply_certs else 0,  # column is INTEGER, not BOOLEAN
                 sources_json, now),
            )

    # ------------------------------------------------------------------
    # Salary
    # ------------------------------------------------------------------

    def get_salary_rates(self, *, owner_user_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                self._sql("SELECT * FROM salary_rates WHERE owner_user_id = ?"),
                (owner_user_id,),
            ).fetchone()
        if row is None:
            return {"rate_review": 0.0, "rate_question": 0.0, "rate_chat": 0.0}
        d = self._row_to_dict(row)
        return {
            "rate_review": float(d.get("rate_review") or 0),
            "rate_question": float(d.get("rate_question") or 0),
            "rate_chat": float(d.get("rate_chat") or 0),
        }

    def set_salary_rates(
        self,
        *,
        owner_user_id: int,
        rate_review: float,
        rate_question: float,
        rate_chat: float,
    ) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                self._sql("""
                INSERT INTO salary_rates (owner_user_id, rate_review, rate_question, rate_chat, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (owner_user_id) DO UPDATE SET
                    rate_review = excluded.rate_review,
                    rate_question = excluded.rate_question,
                    rate_chat = excluded.rate_chat,
                    updated_at = excluded.updated_at
                """),
                (owner_user_id, max(0.0, rate_review), max(0.0, rate_question), max(0.0, rate_chat), now),
            )

    def get_salary_report(
        self,
        *,
        owner_user_id: int,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [owner_user_id]
        date_clause = ""
        if date_from:
            date_clause += " AND created_at::date >= ?::date"
            params.append(date_from)
        if date_to:
            date_clause += " AND created_at::date <= ?::date"
            params.append(date_to)

        with self._connect() as conn:
            rows = conn.execute(
                self._sql(f"""
                SELECT
                    actor,
                    SUM(CASE WHEN action_type = 'manual_reply' THEN 1 ELSE 0 END) AS review_count,
                    SUM(CASE WHEN action_type IN ('conversation_send_success', 'conversation_mark_answered')
                             AND COALESCE(details_json->>'kind', '') = 'question' THEN 1 ELSE 0 END) AS question_count,
                    SUM(CASE WHEN action_type IN ('conversation_send_success', 'conversation_mark_answered')
                             AND COALESCE(details_json->>'kind', '') = 'chat' THEN 1 ELSE 0 END) AS chat_count,
                    SUM(CASE WHEN action_type IN ('conversation_send_success', 'conversation_mark_answered') THEN 1 ELSE 0 END) AS conversation_count
                FROM review_actions
                WHERE user_id = ?{date_clause}
                    AND action_type IN ('manual_reply', 'conversation_send_success', 'conversation_mark_answered')
                GROUP BY actor
                ORDER BY review_count DESC, actor ASC
                """),
                tuple(params),
            ).fetchall()

        rates = self.get_salary_rates(owner_user_id=owner_user_id)
        result: list[dict[str, Any]] = []
        for row in rows:
            d = self._row_to_dict(row)
            rc = int(d.get("review_count") or 0)
            qc = int(d.get("question_count") or 0)
            cc = int(d.get("chat_count") or 0)
            total = (
                rc * float(rates["rate_review"])
                + qc * float(rates["rate_question"])
                + cc * float(rates["rate_chat"])
            )
            result.append({
                "actor": str(d.get("actor") or ""),
                "review_count": rc,
                "question_count": qc,
                "chat_count": cc,
                "total_amount": round(total, 2),
            })
        return result

    def get_salary_stats_for_actor(
        self,
        *,
        owner_user_id: int,
        actor: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        params: list[Any] = [owner_user_id, actor]
        date_clause = ""
        if date_from:
            date_clause += " AND created_at::date >= ?::date"
            params.append(date_from)
        if date_to:
            date_clause += " AND created_at::date <= ?::date"
            params.append(date_to)

        with self._connect() as conn:
            row = conn.execute(
                self._sql(f"""
                SELECT
                    SUM(CASE WHEN action_type = 'manual_reply' THEN 1 ELSE 0 END) AS review_count,
                    SUM(CASE WHEN action_type IN ('conversation_send_success', 'conversation_mark_answered')
                             AND COALESCE(details_json->>'kind', '') = 'question' THEN 1 ELSE 0 END) AS question_count,
                    SUM(CASE WHEN action_type IN ('conversation_send_success', 'conversation_mark_answered')
                             AND COALESCE(details_json->>'kind', '') = 'chat' THEN 1 ELSE 0 END) AS chat_count
                FROM review_actions
                WHERE user_id = ? AND actor = ?{date_clause}
                    AND action_type IN ('manual_reply', 'conversation_send_success', 'conversation_mark_answered')
                """),
                tuple(params),
            ).fetchone()

        d = self._row_to_dict(row) if row else {}
        rates = self.get_salary_rates(owner_user_id=owner_user_id)
        rc = int(d.get("review_count") or 0)
        qc = int(d.get("question_count") or 0)
        cc = int(d.get("chat_count") or 0)
        total = (
            rc * float(rates["rate_review"])
            + qc * float(rates["rate_question"])
            + cc * float(rates["rate_chat"])
        )
        return {
            "actor": actor,
            "review_count": rc,
            "question_count": qc,
            "chat_count": cc,
            "total_amount": round(total, 2),
            "rate_review": float(rates["rate_review"]),
            "rate_question": float(rates["rate_question"]),
            "rate_chat": float(rates["rate_chat"]),
        }

    # ── Salary entries (payroll records) ────────────────────────────────────

    def _ensure_salary_totals_override_table(self, conn) -> None:
        """Stores bulk-imported totals that override product-based calculation."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salary_totals_override (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                worker_id BIGINT NOT NULL,
                entry_date DATE NOT NULL,
                total_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE(worker_id, entry_date)
            )
        """)
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_salary_totals_owner "
            "ON salary_totals_override(owner_user_id, entry_date)"
        ))

    def set_salary_total_override(
        self, *, owner_user_id: int, worker_id: int, entry_date: str, total_amount: float
    ) -> None:
        now = _utc_now()
        with self._connect() as conn:
            self._ensure_salary_totals_override_table(conn)
            if total_amount <= 0:
                conn.execute(
                    self._sql("DELETE FROM salary_totals_override WHERE worker_id = ? AND entry_date = ?"),
                    (worker_id, entry_date),
                )
            else:
                conn.execute(
                    self._sql("""INSERT INTO salary_totals_override
                        (owner_user_id, worker_id, entry_date, total_amount, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (worker_id, entry_date) DO UPDATE SET
                            total_amount = excluded.total_amount,
                            updated_at = excluded.updated_at"""),
                    (owner_user_id, worker_id, entry_date,
                     round(float(total_amount), 2), now, now),
                )

    def get_salary_totals(self, *, owner_user_id: int) -> list[dict[str, Any]]:
        """Return (worker_id, entry_date, total) — products + oklad + extras + linked workers,
        with manual overrides taking absolute precedence."""
        def _r(row, key, idx):
            return row.get(key) if hasattr(row, "get") else row[idx]

        with self._connect() as conn:
            self._ensure_salary_entries_table(conn)
            self._ensure_salary_extra_prod_table(conn)
            self._ensure_salary_oklad_table(conn)
            self._ensure_salary_entry_extras_table(conn)
            self._ensure_salary_worker_links_table(conn)
            self._ensure_salary_totals_override_table(conn)

            totals: dict[tuple[int, str], float] = {}

            # 1. Product entries
            for r in conn.execute(
                self._sql(
                    "SELECT worker_id, CAST(entry_date AS TEXT) AS entry_date, "
                    "SUM(quantity * price_snapshot) AS total "
                    "FROM salary_entries WHERE owner_user_id = ? "
                    "GROUP BY worker_id, entry_date"
                ),
                (owner_user_id,),
            ).fetchall():
                key = (int(_r(r, "worker_id", 0)), str(_r(r, "entry_date", 1)))
                totals[key] = totals.get(key, 0.0) + float(_r(r, "total", 2) or 0)

            # 1b. Extra-production entries
            for r in conn.execute(
                self._sql(
                    "SELECT worker_id, CAST(entry_date AS TEXT) AS entry_date, "
                    "SUM(quantity * price_snapshot) AS total "
                    "FROM salary_extra_prod_entries WHERE owner_user_id = ? "
                    "GROUP BY worker_id, entry_date"
                ),
                (owner_user_id,),
            ).fetchall():
                key = (int(_r(r, "worker_id", 0)), str(_r(r, "entry_date", 1)))
                totals[key] = totals.get(key, 0.0) + float(_r(r, "total", 2) or 0)

            # 2. Oklad entries
            for r in conn.execute(
                self._sql(
                    "SELECT worker_id, CAST(entry_date AS TEXT) AS entry_date, amount "
                    "FROM salary_oklad_entries WHERE owner_user_id = ?"
                ),
                (owner_user_id,),
            ).fetchall():
                amt = float(_r(r, "amount", 2) or 0)
                if amt > 0:
                    key = (int(_r(r, "worker_id", 0)), str(_r(r, "entry_date", 1)))
                    totals[key] = totals.get(key, 0.0) + amt

            # 3. Extra costs
            for r in conn.execute(
                self._sql(
                    "SELECT worker_id, CAST(entry_date AS TEXT) AS entry_date, SUM(amount) AS total "
                    "FROM salary_entry_extras WHERE owner_user_id = ? "
                    "GROUP BY worker_id, entry_date"
                ),
                (owner_user_id,),
            ).fetchall():
                amt = float(_r(r, "total", 2) or 0)
                if amt > 0:
                    key = (int(_r(r, "worker_id", 0)), str(_r(r, "entry_date", 1)))
                    totals[key] = totals.get(key, 0.0) + amt

            # 4. Linked workers — amounts are always taken dynamically from base_totals.
            #    We store only worker IDs in two places:
            #      a) salary_linked_snapshot  — historical record per date (written on Save)
            #      b) salary_worker_links      — currently active links
            #    Rule: if a snapshot exists for (worker, date) → use snapshot worker IDs.
            #          otherwise → use active link worker IDs.
            #    This ensures deleting a link never removes historical contributions.
            self._ensure_salary_linked_snapshot_table(conn)

            # Compute base totals first (without linked contributions)
            base_totals = dict(totals)

            # a) Snapshot worker IDs  →  (main_worker, date) → set of linked_worker_ids
            snap_links = {}
            for r in conn.execute(
                self._sql(
                    "SELECT worker_id, CAST(entry_date AS TEXT) AS entry_date, linked_worker_id "
                    "FROM salary_linked_snapshot WHERE owner_user_id = ?"
                ),
                (owner_user_id,),
            ).fetchall():
                key = (int(_r(r, "worker_id", 0)), str(_r(r, "entry_date", 1)))
                snap_links.setdefault(key, set()).add(int(_r(r, "linked_worker_id", 2)))

            # b) Active link worker IDs  →  main_worker_id → set of linked_worker_ids
            act_links = {}
            for r in conn.execute(
                self._sql(
                    "SELECT worker_id, linked_worker_id "
                    "FROM salary_worker_links WHERE owner_user_id = ?"
                ),
                (owner_user_id,),
            ).fetchall():
                act_links.setdefault(
                    int(_r(r, "worker_id", 0)), set()
                ).add(int(_r(r, "linked_worker_id", 1)))

            # Apply snapshot-based contributions (historical dates)
            for (wid, date), linked_ids in snap_links.items():
                for lid in linked_ids:
                    contrib = base_totals.get((lid, date), 0.0)
                    totals[(wid, date)] = totals.get((wid, date), 0.0) + contrib

            # Apply active-link contributions for dates WITHOUT a snapshot
            snapshot_keys = set(snap_links.keys())
            all_dates = {d for (_, d) in base_totals}
            for wid, linked_ids in act_links.items():
                # Also consider dates where any linked worker has data
                for lid in linked_ids:
                    all_dates |= {d for (w, d) in base_totals if w == lid}
                for date in all_dates:
                    if (wid, date) not in snapshot_keys:
                        for lid in linked_ids:
                            contrib = base_totals.get((lid, date), 0.0)
                            if contrib > 0:
                                totals[(wid, date)] = totals.get((wid, date), 0.0) + contrib

            # 5. Manual overrides take absolute precedence
            for ov in conn.execute(
                self._sql(
                    "SELECT worker_id, CAST(entry_date AS TEXT) AS entry_date, total_amount "
                    "FROM salary_totals_override WHERE owner_user_id = ?"
                ),
                (owner_user_id,),
            ).fetchall():
                wid = int(_r(ov, "worker_id", 0))
                ed = str(_r(ov, "entry_date", 1))
                totals[(wid, ed)] = float(_r(ov, "total_amount", 2) or 0)

        return [
            {"worker_id": wid, "entry_date": ed, "total": tot}
            for (wid, ed), tot in totals.items()
        ]

    def _ensure_salary_entries_table(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salary_entries (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                worker_id BIGINT NOT NULL,
                entry_date DATE NOT NULL,
                product_id BIGINT NOT NULL,
                quantity NUMERIC(10,2) NOT NULL DEFAULT 0,
                price_snapshot NUMERIC(12,2) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE(worker_id, entry_date, product_id)
            )
        """)
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_salary_entries_owner "
            "ON salary_entries(owner_user_id, entry_date)"
        ))
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_salary_entries_worker "
            "ON salary_entries(worker_id, entry_date)"
        ))

    def get_salary_entries(
        self, *, owner_user_id: int, worker_id: int, entry_date: str
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_salary_entries_table(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT id, worker_id, entry_date, product_id, quantity, price_snapshot "
                    "FROM salary_entries WHERE worker_id = ? AND entry_date = ? "
                    "AND owner_user_id = ?"
                ),
                (worker_id, entry_date, owner_user_id),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def upsert_salary_entries(
        self, *, owner_user_id: int, worker_id: int, entry_date: str,
        entries: list[dict[str, Any]],
    ) -> None:
        """Replace all entries for (worker_id, entry_date) with the given list."""
        now = _utc_now()
        with self._connect() as conn:
            self._ensure_salary_entries_table(conn)
            # Delete existing entries for this worker+date
            conn.execute(
                self._sql(
                    "DELETE FROM salary_entries WHERE worker_id = ? AND entry_date = ? "
                    "AND owner_user_id = ?"
                ),
                (worker_id, entry_date, owner_user_id),
            )
            for e in entries:
                qty = float(e.get("quantity") or 0)
                if qty <= 0:
                    continue
                conn.execute(
                    self._sql(
                        "INSERT INTO salary_entries "
                        "(owner_user_id, worker_id, entry_date, product_id, quantity, "
                        "price_snapshot, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (owner_user_id, worker_id, entry_date,
                     int(e["product_id"]), qty,
                     float(e.get("price_snapshot") or 0), now, now),
                )

    # ── Salary products (price list) ────────────────────────────────────────

    def _ensure_salary_products_table(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salary_products (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                order_num INTEGER NOT NULL DEFAULT 0,
                name TEXT NOT NULL,
                price_ivanovo NUMERIC(12,2) NOT NULL DEFAULT 0,
                price_kineshma NUMERIC(12,2) NOT NULL DEFAULT 0,
                price_nerl NUMERIC(12,2) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL
            )
        """)
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_salary_products_owner "
            "ON salary_products(owner_user_id, order_num ASC)"
        ))
        # Sub-price columns for Кинешма and Нерль (idempotent)
        for col in (
            "price_kineshma_poshiv", "price_kineshma_raskroi", "price_kineshma_upakovka",
            "price_nerl_poshiv",     "price_nerl_raskroi",     "price_nerl_upakovka",
        ):
            conn.execute(
                f"ALTER TABLE salary_products ADD COLUMN IF NOT EXISTS {col} NUMERIC(12,2) NOT NULL DEFAULT 0"
            )
        # Roles: comma-separated list of positions (Швея, Упаковщик, Закройщик)
        # Empty = applies to all three roles (backwards compat)
        conn.execute(
            "ALTER TABLE salary_products ADD COLUMN IF NOT EXISTS roles TEXT NOT NULL DEFAULT ''"
        )

    _SUB_PRICE_COLS = (
        "price_kineshma_poshiv", "price_kineshma_raskroi", "price_kineshma_upakovka",
        "price_nerl_poshiv",     "price_nerl_raskroi",     "price_nerl_upakovka",
    )

    def list_salary_products(self, *, owner_user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_salary_products_table(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT id, owner_user_id, order_num, name, roles, "
                    + ", ".join(self._SUB_PRICE_COLS)
                    + ", created_at "
                    "FROM salary_products WHERE owner_user_id = ? "
                    "ORDER BY order_num ASC, id ASC"
                ),
                (owner_user_id,),
            ).fetchall()
        results = [self._row_to_dict(row) for row in rows]
        for r in results:
            for col in self._SUB_PRICE_COLS:
                r[col] = float(r.get(col) or 0)
        return results

    def create_salary_product(
        self, *, owner_user_id: int, order_num: int, name: str,
        roles: str = "",
        price_ivanovo: float = 0,
        price_kineshma: float = 0, price_nerl: float = 0,
        price_kineshma_poshiv: float = 0, price_kineshma_raskroi: float = 0,
        price_kineshma_upakovka: float = 0,
        price_nerl_poshiv: float = 0, price_nerl_raskroi: float = 0,
        price_nerl_upakovka: float = 0,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            self._ensure_salary_products_table(conn)
            cur = conn.execute(
                self._sql(
                    "INSERT INTO salary_products "
                    "(owner_user_id, order_num, name, roles, price_ivanovo, price_kineshma, price_nerl, "
                    "price_kineshma_poshiv, price_kineshma_raskroi, price_kineshma_upakovka, "
                    "price_nerl_poshiv, price_nerl_raskroi, price_nerl_upakovka, created_at) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id"
                ),
                (owner_user_id, order_num, name.strip(), roles.strip(),
                 round(price_kineshma_poshiv + price_kineshma_raskroi + price_kineshma_upakovka, 2),
                 round(price_nerl_poshiv + price_nerl_raskroi + price_nerl_upakovka, 2),
                 round(float(price_kineshma_poshiv), 2), round(float(price_kineshma_raskroi), 2),
                 round(float(price_kineshma_upakovka), 2),
                 round(float(price_nerl_poshiv), 2), round(float(price_nerl_raskroi), 2),
                 round(float(price_nerl_upakovka), 2), now),
            )
            row = cur.fetchone()
        product_id = int(row[0] if not hasattr(row, "get") else row.get("id"))
        return {"id": product_id, "order_num": order_num, "name": name.strip(), "roles": roles.strip(),
                "price_kineshma_poshiv": price_kineshma_poshiv,
                "price_kineshma_raskroi": price_kineshma_raskroi,
                "price_kineshma_upakovka": price_kineshma_upakovka,
                "price_nerl_poshiv": price_nerl_poshiv,
                "price_nerl_raskroi": price_nerl_raskroi,
                "price_nerl_upakovka": price_nerl_upakovka, "created_at": now}

    def update_salary_product(
        self, *, owner_user_id: int, product_id: int, order_num: int,
        name: str, roles: str = "",
        price_ivanovo: float = 0, price_kineshma: float = 0, price_nerl: float = 0,
        price_kineshma_poshiv: float = 0, price_kineshma_raskroi: float = 0,
        price_kineshma_upakovka: float = 0,
        price_nerl_poshiv: float = 0, price_nerl_raskroi: float = 0,
        price_nerl_upakovka: float = 0,
    ) -> bool:
        kineshma_total = round(float(price_kineshma_poshiv) + float(price_kineshma_raskroi) + float(price_kineshma_upakovka), 2)
        nerl_total     = round(float(price_nerl_poshiv)     + float(price_nerl_raskroi)     + float(price_nerl_upakovka),     2)
        with self._connect() as conn:
            self._ensure_salary_products_table(conn)
            result = conn.execute(
                self._sql(
                    "UPDATE salary_products SET order_num=?, name=?, roles=?, price_ivanovo=0, "
                    "price_kineshma=?, price_nerl=?, "
                    "price_kineshma_poshiv=?, price_kineshma_raskroi=?, price_kineshma_upakovka=?, "
                    "price_nerl_poshiv=?, price_nerl_raskroi=?, price_nerl_upakovka=? "
                    "WHERE id=? AND owner_user_id=?"
                ),
                (order_num, name.strip(), roles.strip(), kineshma_total, nerl_total,
                 round(float(price_kineshma_poshiv), 2), round(float(price_kineshma_raskroi), 2),
                 round(float(price_kineshma_upakovka), 2),
                 round(float(price_nerl_poshiv), 2), round(float(price_nerl_raskroi), 2),
                 round(float(price_nerl_upakovka), 2), product_id, owner_user_id),
            )
        return result.rowcount > 0

    def reorder_salary_products(
        self, *, owner_user_id: int, order: list[dict[str, int]]
    ) -> None:
        """Update order_num for each product in the list."""
        with self._connect() as conn:
            self._ensure_salary_products_table(conn)
            for item in order:
                conn.execute(
                    self._sql(
                        "UPDATE salary_products SET order_num=? WHERE id=? AND owner_user_id=?"
                    ),
                    (int(item["order_num"]), int(item["id"]), owner_user_id),
                )

    def delete_salary_product(self, *, owner_user_id: int, product_id: int) -> bool:
        with self._connect() as conn:
            self._ensure_salary_products_table(conn)
            result = conn.execute(
                self._sql("DELETE FROM salary_products WHERE id = ? AND owner_user_id = ?"),
                (product_id, owner_user_id),
            )
        return result.rowcount > 0

    # ── Salary workers ──────────────────────────────────────────────────────

    def _ensure_salary_workers_table(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salary_workers (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                full_name TEXT NOT NULL,
                position TEXT NOT NULL DEFAULT '',
                birth_date TEXT NOT NULL DEFAULT '',
                legal_entity TEXT NOT NULL DEFAULT '',
                production TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL
            )
        """)
        conn.execute("ALTER TABLE salary_workers ADD COLUMN IF NOT EXISTS birth_date TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE salary_workers ADD COLUMN IF NOT EXISTS legal_entity TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE salary_workers ADD COLUMN IF NOT EXISTS position TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE salary_workers ADD COLUMN IF NOT EXISTS visible_for_accountant BOOLEAN NOT NULL DEFAULT TRUE")
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_salary_workers_owner "
            "ON salary_workers(owner_user_id)"
        ))

    def list_salary_workers(self, *, owner_user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_salary_workers_table(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT id, owner_user_id, full_name, position, birth_date, legal_entity, "
                    "production, visible_for_accountant, created_at "
                    "FROM salary_workers WHERE owner_user_id = ? ORDER BY full_name ASC"
                ),
                (owner_user_id,),
            ).fetchall()
        result = [self._row_to_dict(row) for row in rows]
        for r in result:
            r["visible_for_accountant"] = bool(r.get("visible_for_accountant") if r.get("visible_for_accountant") is not None else True)
        return result

    def create_salary_worker(
        self, *, owner_user_id: int, full_name: str, position: str,
        birth_date: str, legal_entity: str, production: str,
        visible_for_accountant: bool = True,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            self._ensure_salary_workers_table(conn)
            cur = conn.execute(
                self._sql(
                    "INSERT INTO salary_workers "
                    "(owner_user_id, full_name, position, birth_date, legal_entity, production, "
                    "visible_for_accountant, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id"
                ),
                (owner_user_id, full_name.strip(), position.strip(), birth_date.strip(),
                 legal_entity.strip(), production.strip(),
                 self._bool_db(visible_for_accountant), now),
            )
            row = cur.fetchone()
        worker_id = int(row[0] if not hasattr(row, "get") else row.get("id"))
        return {"id": worker_id, "owner_user_id": owner_user_id,
                "full_name": full_name.strip(), "position": position.strip(),
                "birth_date": birth_date.strip(), "legal_entity": legal_entity.strip(),
                "production": production.strip(),
                "visible_for_accountant": visible_for_accountant, "created_at": now}

    def update_salary_worker(
        self, *, owner_user_id: int, worker_id: int,
        full_name: str, position: str, birth_date: str,
        legal_entity: str, production: str, visible_for_accountant: bool = True,
    ) -> bool:
        with self._connect() as conn:
            self._ensure_salary_workers_table(conn)
            result = conn.execute(
                self._sql(
                    "UPDATE salary_workers SET full_name=?, position=?, birth_date=?, "
                    "legal_entity=?, production=?, visible_for_accountant=? "
                    "WHERE id=? AND owner_user_id=?"
                ),
                (full_name.strip(), position.strip(), birth_date.strip(),
                 legal_entity.strip(), production.strip(),
                 self._bool_db(visible_for_accountant), worker_id, owner_user_id),
            )
        return result.rowcount > 0

    def delete_salary_worker(self, *, owner_user_id: int, worker_id: int) -> bool:
        with self._connect() as conn:
            self._ensure_salary_workers_table(conn)
            result = conn.execute(
                self._sql(
                    "DELETE FROM salary_workers WHERE id = ? AND owner_user_id = ?"
                ),
                (worker_id, owner_user_id),
            )
        return result.rowcount > 0

    # ── Salary extra-production entries ─────────────────────────────────────

    def _ensure_salary_extra_prod_table(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salary_extra_prod_entries (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                worker_id BIGINT NOT NULL,
                entry_date DATE NOT NULL,
                prod_type TEXT NOT NULL,
                product_id BIGINT NOT NULL,
                quantity NUMERIC(10,2) NOT NULL DEFAULT 0,
                price_snapshot NUMERIC(12,2) NOT NULL DEFAULT 0
            )
        """)
        conn.execute(self._sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_salary_extra_prod_unique "
            "ON salary_extra_prod_entries(owner_user_id, worker_id, entry_date, prod_type, product_id)"
        ))
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_salary_extra_prod_worker "
            "ON salary_extra_prod_entries(owner_user_id, worker_id, entry_date)"
        ))

    def get_salary_extra_prods(
        self, *, owner_user_id: int, worker_id: int, entry_date: str
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_salary_extra_prod_table(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT prod_type, product_id, quantity, price_snapshot "
                    "FROM salary_extra_prod_entries "
                    "WHERE owner_user_id = ? AND worker_id = ? AND entry_date = ? "
                    "ORDER BY prod_type, product_id ASC"
                ),
                (owner_user_id, worker_id, entry_date),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def upsert_salary_extra_prods(
        self,
        *,
        owner_user_id: int,
        worker_id: int,
        entry_date: str,
        entries: list[dict[str, Any]],
    ) -> None:
        """Replace all extra-prod entries for (worker_id, entry_date)."""
        with self._connect() as conn:
            self._ensure_salary_extra_prod_table(conn)
            conn.execute(
                self._sql(
                    "DELETE FROM salary_extra_prod_entries "
                    "WHERE owner_user_id = ? AND worker_id = ? AND entry_date = ?"
                ),
                (owner_user_id, worker_id, entry_date),
            )
            for e in entries:
                qty = float(e.get("quantity") or 0)
                if qty <= 0:
                    continue
                conn.execute(
                    self._sql(
                        "INSERT INTO salary_extra_prod_entries "
                        "(owner_user_id, worker_id, entry_date, prod_type, product_id, "
                        "quantity, price_snapshot) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        owner_user_id, worker_id, entry_date,
                        str(e["prod_type"]),
                        int(e["product_id"]),
                        qty,
                        float(e.get("price_snapshot") or 0),
                    ),
                )

    # ── Salary data clear ────────────────────────────────────────────────────

    _SALARY_DATA_TABLES = (
        "salary_entries",
        "salary_extra_prod_entries",
        "salary_oklad_entries",
        "salary_entry_extras",
        "salary_vacation_entries",
        "salary_linked_snapshot",
        "salary_totals_override",
    )

    def clear_salary_data(
        self,
        *,
        owner_user_id: int,
        entry_date: str | None = None,
        worker_ids: list[int] | None = None,
    ) -> int:
        """Delete payroll data for the given scope.
        - entry_date=None, worker_ids=None  → clear ALL
        - entry_date set                    → clear that date only
        - worker_ids set                    → clear those workers (optionally + date)
        Returns total rows deleted.
        """
        total = 0
        with self._connect() as conn:
            for tbl in self._SALARY_DATA_TABLES:
                try:
                    if worker_ids is not None and entry_date:
                        placeholders = ", ".join(["?"] * len(worker_ids))
                        res = conn.execute(
                            self._sql(
                                f"DELETE FROM {tbl} WHERE owner_user_id = ? "
                                f"AND worker_id IN ({placeholders}) AND entry_date = ?"
                            ),
                            [owner_user_id, *worker_ids, entry_date],
                        )
                    elif worker_ids is not None:
                        placeholders = ", ".join(["?"] * len(worker_ids))
                        res = conn.execute(
                            self._sql(
                                f"DELETE FROM {tbl} WHERE owner_user_id = ? "
                                f"AND worker_id IN ({placeholders})"
                            ),
                            [owner_user_id, *worker_ids],
                        )
                    elif entry_date:
                        res = conn.execute(
                            self._sql(
                                f"DELETE FROM {tbl} WHERE owner_user_id = ? AND entry_date = ?"
                            ),
                            (owner_user_id, entry_date),
                        )
                    else:
                        res = conn.execute(
                            self._sql(f"DELETE FROM {tbl} WHERE owner_user_id = ?"),
                            (owner_user_id,),
                        )
                    total += res.rowcount or 0
                except Exception:
                    pass  # table may not exist yet
        return total

    # ── Salary vacation entries ──────────────────────────────────────────────

    def _ensure_salary_vacation_table(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salary_vacation_entries (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                worker_id BIGINT NOT NULL,
                entry_date DATE NOT NULL
            )
        """)
        conn.execute(self._sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_salary_vacation_unique "
            "ON salary_vacation_entries(owner_user_id, worker_id, entry_date)"
        ))

    def list_salary_vacations(self, *, owner_user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_salary_vacation_table(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT worker_id, CAST(entry_date AS TEXT) AS entry_date "
                    "FROM salary_vacation_entries WHERE owner_user_id = ?"
                ),
                (owner_user_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def set_salary_vacation(
        self, *, owner_user_id: int, worker_id: int, entry_date: str, on: bool
    ) -> None:
        with self._connect() as conn:
            self._ensure_salary_vacation_table(conn)
            conn.execute(
                self._sql(
                    "DELETE FROM salary_vacation_entries "
                    "WHERE owner_user_id = ? AND worker_id = ? AND entry_date = ?"
                ),
                (owner_user_id, worker_id, entry_date),
            )
            if on:
                conn.execute(
                    self._sql(
                        "INSERT INTO salary_vacation_entries "
                        "(owner_user_id, worker_id, entry_date) VALUES (?, ?, ?)"
                    ),
                    (owner_user_id, worker_id, entry_date),
                )

    # ── Salary oklad entries ─────────────────────────────────────────────────

    def _ensure_salary_oklad_table(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salary_oklad_entries (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                worker_id BIGINT NOT NULL,
                entry_date DATE NOT NULL,
                amount NUMERIC(12,2) NOT NULL DEFAULT 0
            )
        """)
        conn.execute(self._sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_salary_oklad_unique "
            "ON salary_oklad_entries(owner_user_id, worker_id, entry_date)"
        ))

    def get_salary_oklad(self, *, owner_user_id: int, worker_id: int, entry_date: str) -> float:
        with self._connect() as conn:
            self._ensure_salary_oklad_table(conn)
            row = conn.execute(
                self._sql(
                    "SELECT amount FROM salary_oklad_entries "
                    "WHERE owner_user_id = ? AND worker_id = ? AND entry_date = ?"
                ),
                (owner_user_id, worker_id, entry_date),
            ).fetchone()
        if row is None:
            return 0.0
        return float(row["amount"] if hasattr(row, "keys") else row[0] or 0)

    def upsert_salary_oklad(
        self, *, owner_user_id: int, worker_id: int, entry_date: str, amount: float
    ) -> None:
        with self._connect() as conn:
            self._ensure_salary_oklad_table(conn)
            conn.execute(
                self._sql(
                    "DELETE FROM salary_oklad_entries "
                    "WHERE owner_user_id = ? AND worker_id = ? AND entry_date = ?"
                ),
                (owner_user_id, worker_id, entry_date),
            )
            conn.execute(
                self._sql(
                    "INSERT INTO salary_oklad_entries "
                    "(owner_user_id, worker_id, entry_date, amount) VALUES (?, ?, ?, ?)"
                ),
                (owner_user_id, worker_id, entry_date, amount),
            )

    # ── Salary entry extras ──────────────────────────────────────────────────

    def _ensure_salary_entry_extras_table(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salary_entry_extras (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                worker_id BIGINT NOT NULL,
                entry_date DATE NOT NULL,
                amount NUMERIC(12,2) NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_salary_extras_worker "
            "ON salary_entry_extras(owner_user_id, worker_id, entry_date)"
        ))

    def list_salary_entry_extras(
        self, *, owner_user_id: int, worker_id: int, entry_date: str
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_salary_entry_extras_table(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT id, amount, note FROM salary_entry_extras "
                    "WHERE owner_user_id = ? AND worker_id = ? AND entry_date = ? ORDER BY id ASC"
                ),
                (owner_user_id, worker_id, entry_date),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def replace_salary_entry_extras(
        self,
        *,
        owner_user_id: int,
        worker_id: int,
        entry_date: str,
        extras: list[dict[str, Any]],
    ) -> None:
        with self._connect() as conn:
            self._ensure_salary_entry_extras_table(conn)
            conn.execute(
                self._sql(
                    "DELETE FROM salary_entry_extras "
                    "WHERE owner_user_id = ? AND worker_id = ? AND entry_date = ?"
                ),
                (owner_user_id, worker_id, entry_date),
            )
            for e in extras:
                amt = float(e.get("amount") or 0)
                note = str(e.get("note") or "")
                if amt <= 0 and not note:
                    continue
                conn.execute(
                    self._sql(
                        "INSERT INTO salary_entry_extras "
                        "(owner_user_id, worker_id, entry_date, amount, note) VALUES (?, ?, ?, ?, ?)"
                    ),
                    (owner_user_id, worker_id, entry_date, amt, note),
                )

    # ── Salary worker links (persistent) ────────────────────────────────────

    def _ensure_salary_worker_links_table(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salary_worker_links (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                worker_id BIGINT NOT NULL,
                linked_worker_id BIGINT NOT NULL
            )
        """)
        conn.execute(self._sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_salary_links_unique "
            "ON salary_worker_links(owner_user_id, worker_id, linked_worker_id)"
        ))

    def list_salary_worker_links(
        self, *, owner_user_id: int, worker_id: int
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_salary_worker_links_table(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT swl.id, swl.linked_worker_id, sw.full_name AS linked_worker_name "
                    "FROM salary_worker_links swl "
                    "LEFT JOIN salary_workers sw ON sw.id = swl.linked_worker_id "
                    "WHERE swl.owner_user_id = ? AND swl.worker_id = ? ORDER BY swl.id ASC"
                ),
                (owner_user_id, worker_id),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_all_salary_linked_ids(self, *, owner_user_id: int) -> set[int]:
        """Return the set of all linked_worker_id values used in any link for this tenant."""
        with self._connect() as conn:
            self._ensure_salary_worker_links_table(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT linked_worker_id FROM salary_worker_links WHERE owner_user_id = ?"
                ),
                (owner_user_id,),
            ).fetchall()
        return {
            int(r["linked_worker_id"] if hasattr(r, "get") else r[0])
            for r in rows
        }

    def add_salary_worker_link(
        self, *, owner_user_id: int, worker_id: int, linked_worker_id: int
    ) -> None:
        with self._connect() as conn:
            self._ensure_salary_worker_links_table(conn)
            try:
                conn.execute(
                    self._sql(
                        "INSERT INTO salary_worker_links "
                        "(owner_user_id, worker_id, linked_worker_id) VALUES (?, ?, ?)"
                    ),
                    (owner_user_id, worker_id, linked_worker_id),
                )
            except Exception:
                pass  # Already exists — ignore

    # ── Salary linked snapshot (historical) ─────────────────────────────────

    def _ensure_salary_linked_snapshot_table(self, conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS salary_linked_snapshot (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                worker_id BIGINT NOT NULL,
                entry_date DATE NOT NULL,
                linked_worker_id BIGINT NOT NULL,
                linked_worker_name TEXT NOT NULL DEFAULT '',
                amount NUMERIC(12,2) NOT NULL DEFAULT 0
            )
        """)
        conn.execute(self._sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_salary_linked_snap_unique "
            "ON salary_linked_snapshot(owner_user_id, worker_id, entry_date, linked_worker_id)"
        ))
        conn.execute(self._sql(
            "CREATE INDEX IF NOT EXISTS idx_salary_linked_snap_worker "
            "ON salary_linked_snapshot(owner_user_id, worker_id, entry_date)"
        ))

    def save_salary_linked_snapshot(
        self,
        *,
        owner_user_id: int,
        worker_id: int,
        entry_date: str,
        links: list[dict[str, Any]],
    ) -> None:
        """Replace the linked-worker snapshot for (worker_id, entry_date)."""
        with self._connect() as conn:
            self._ensure_salary_linked_snapshot_table(conn)
            conn.execute(
                self._sql(
                    "DELETE FROM salary_linked_snapshot "
                    "WHERE owner_user_id = ? AND worker_id = ? AND entry_date = ?"
                ),
                (owner_user_id, worker_id, entry_date),
            )
            for lnk in links:
                conn.execute(
                    self._sql(
                        "INSERT INTO salary_linked_snapshot "
                        "(owner_user_id, worker_id, entry_date, linked_worker_id, "
                        "linked_worker_name, amount) VALUES (?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        owner_user_id, worker_id, entry_date,
                        int(lnk["linked_worker_id"]),
                        str(lnk.get("linked_worker_name") or ""),
                        float(lnk.get("amount") or 0),
                    ),
                )

    def get_salary_linked_snapshot(
        self, *, owner_user_id: int, worker_id: int, entry_date: str
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_salary_linked_snapshot_table(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT linked_worker_id, linked_worker_name, amount "
                    "FROM salary_linked_snapshot "
                    "WHERE owner_user_id = ? AND worker_id = ? AND entry_date = ? "
                    "ORDER BY id ASC"
                ),
                (owner_user_id, worker_id, entry_date),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def delete_salary_worker_link(self, *, owner_user_id: int, link_id: int) -> bool:
        with self._connect() as conn:
            self._ensure_salary_worker_links_table(conn)
            result = conn.execute(
                self._sql(
                    "DELETE FROM salary_worker_links WHERE id = ? AND owner_user_id = ?"
                ),
                (link_id, owner_user_id),
            )
        return result.rowcount > 0

    # ── Manual supply balances (Остатки) ─────────────────────────────────────

    def _ensure_supply_balances_tables(self, conn) -> None:
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_supply_stock BOOLEAN NOT NULL DEFAULT FALSE"
        )
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS stock_productions TEXT NOT NULL DEFAULT '[]'"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_materials (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT 'шт',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            self._sql(
                "CREATE INDEX IF NOT EXISTS idx_feedback_materials_user "
                "ON feedback_materials(user_id)"
            )
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_balances (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                production_id BIGINT NOT NULL,
                item_type TEXT NOT NULL,
                item_id BIGINT NOT NULL,
                balance_date TEXT NOT NULL,
                quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                updated_by BIGINT,
                UNIQUE(user_id, production_id, item_type, item_id, balance_date)
            )
            """
        )
        conn.execute(
            self._sql(
                "CREATE INDEX IF NOT EXISTS idx_supply_balances_lookup "
                "ON supply_balances(user_id, production_id, balance_date)"
            )
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_balance_visibility (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                item_type TEXT NOT NULL,
                item_id BIGINT NOT NULL,
                visible BOOLEAN NOT NULL DEFAULT TRUE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, item_type, item_id)
            )
            """
        )
        conn.execute(
            "ALTER TABLE supply_balance_visibility "
            "ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0"
        )
        # Optional reorder threshold for Остатки (NULL = not set).
        conn.execute(
            "ALTER TABLE supply_balance_visibility "
            "ADD COLUMN IF NOT EXISTS min_qty DOUBLE PRECISION"
        )
        # Append-only stock ledger (Поставки → Остатки). Balance = SUM(qty).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_stock_movements (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                production_id BIGINT NOT NULL,
                item_type TEXT NOT NULL,
                item_id BIGINT NOT NULL,
                qty DOUBLE PRECISION NOT NULL,
                movement_date TEXT NOT NULL,
                kind TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                created_by BIGINT,
                UNIQUE(user_id, source_type, source_id)
            )
            """
        )
        conn.execute(
            self._sql(
                "CREATE INDEX IF NOT EXISTS idx_supply_stock_movements_lookup "
                "ON supply_stock_movements(user_id, production_id, movement_date)"
            )
        )
        conn.execute(
            self._sql(
                "CREATE INDEX IF NOT EXISTS idx_supply_stock_movements_item "
                "ON supply_stock_movements(user_id, production_id, item_type, item_id)"
            )
        )
        # Orders already reflected in an opening/adjustment — do not auto-ship later.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_stock_fbs_settled (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                order_id BIGINT NOT NULL,
                production_id BIGINT NOT NULL DEFAULT 0,
                settled_at TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                UNIQUE(user_id, order_id)
            )
            """
        )
        conn.execute(
            self._sql(
                "CREATE INDEX IF NOT EXISTS idx_supply_stock_fbs_settled_user "
                "ON supply_stock_fbs_settled(user_id)"
            )
        )

    def ensure_supply_balances_tables(self) -> None:
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)

    def set_user_can_supply_stock(
        self,
        *,
        user_id: int,
        can_supply_stock: bool,
        stock_productions: list[str] | None = None,
    ) -> None:
        import json as _j

        prods_json = _j.dumps(
            [str(x) for x in (stock_productions or [])],
            ensure_ascii=False,
        )
        with self._connect() as conn:
            conn.execute(
                self._sql(
                    "UPDATE users SET can_supply_stock = ?, stock_productions = ? WHERE id = ?"
                ),
                (self._bool_db(can_supply_stock), prods_json, user_id),
            )

    def list_feedback_materials(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT * FROM feedback_materials WHERE user_id = ? "
                    "ORDER BY sort_order ASC, name ASC, id ASC"
                ),
                (user_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def add_feedback_material(
        self, *, user_id: int, name: str, unit: str = "шт"
    ) -> dict[str, Any]:
        now = _utc_now()
        name_s = str(name or "").strip()
        unit_s = str(unit or "шт").strip() or "шт"
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            mid = self._insert_and_get_id(
                conn,
                "INSERT INTO feedback_materials "
                "(user_id, name, unit, sort_order, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (user_id, name_s, unit_s, now, now),
            )
            row = conn.execute(
                self._sql("SELECT * FROM feedback_materials WHERE id = ?"),
                (mid,),
            ).fetchone()
        return self._row_to_dict(row) if row else {"id": mid, "name": name_s, "unit": unit_s}

    def update_feedback_material(
        self, *, user_id: int, material_id: int, name: str, unit: str = "шт"
    ) -> bool:
        now = _utc_now()
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            cur = conn.execute(
                self._sql(
                    "UPDATE feedback_materials SET name = ?, unit = ?, updated_at = ? "
                    "WHERE user_id = ? AND id = ?"
                ),
                (
                    str(name or "").strip(),
                    str(unit or "шт").strip() or "шт",
                    now,
                    user_id,
                    int(material_id),
                ),
            )
        return int(cur.rowcount or 0) > 0

    def delete_feedback_material(self, *, user_id: int, material_id: int) -> bool:
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            cur = conn.execute(
                self._sql(
                    "DELETE FROM feedback_materials WHERE user_id = ? AND id = ?"
                ),
                (user_id, int(material_id)),
            )
            conn.execute(
                self._sql(
                    "DELETE FROM supply_balances WHERE user_id = ? AND item_type = ? AND item_id = ?"
                ),
                (user_id, "material", int(material_id)),
            )
            conn.execute(
                self._sql(
                    "DELETE FROM supply_stock_movements "
                    "WHERE user_id = ? AND item_type = ? AND item_id = ?"
                ),
                (user_id, "material", int(material_id)),
            )
            conn.execute(
                self._sql(
                    "DELETE FROM supply_balance_visibility "
                    "WHERE user_id = ? AND item_type = ? AND item_id = ?"
                ),
                (user_id, "material", int(material_id)),
            )
        return int(cur.rowcount or 0) > 0

    def list_supply_balance_dates(
        self, *, user_id: int, production_id: int
    ) -> list[str]:
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT DISTINCT balance_date FROM supply_balances "
                    "WHERE user_id = ? AND production_id = ? "
                    "ORDER BY balance_date ASC"
                ),
                (user_id, int(production_id)),
            ).fetchall()
        out: list[str] = []
        for r in rows:
            d = self._row_to_dict(r)
            val = str(d.get("balance_date") or "").strip()
            if val:
                out.append(val)
        return out

    def list_supply_balances(
        self, *, user_id: int, production_id: int, dates: list[str] | None = None
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            if dates:
                placeholders = ", ".join("?" for _ in dates)
                rows = conn.execute(
                    self._sql(
                        f"SELECT * FROM supply_balances "
                        f"WHERE user_id = ? AND production_id = ? "
                        f"AND balance_date IN ({placeholders})"
                    ),
                    (user_id, int(production_id), *[str(d) for d in dates]),
                ).fetchall()
            else:
                rows = conn.execute(
                    self._sql(
                        "SELECT * FROM supply_balances "
                        "WHERE user_id = ? AND production_id = ?"
                    ),
                    (user_id, int(production_id)),
                ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def upsert_supply_balances(
        self,
        *,
        user_id: int,
        production_id: int,
        balance_date: str,
        items: list[dict[str, Any]],
        updated_by: int | None = None,
    ) -> int:
        """Upsert quantity rows for one production/date. Returns saved count.

        ``quantity: null`` deletes the cell for that item/date (clear input).
        """
        now = _utc_now()
        date_s = str(balance_date or "").strip()
        if not date_s:
            return 0
        saved = 0
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            for item in items or []:
                item_type = str(item.get("item_type") or "").strip().lower()
                if item_type not in {"material", "product"}:
                    continue
                try:
                    item_id = int(item.get("item_id") or 0)
                except (TypeError, ValueError):
                    continue
                if item_id <= 0:
                    continue
                if item.get("quantity") is None:
                    conn.execute(
                        self._sql(
                            "DELETE FROM supply_balances WHERE user_id = ? AND production_id = ? "
                            "AND item_type = ? AND item_id = ? AND balance_date = ?"
                        ),
                        (user_id, int(production_id), item_type, item_id, date_s),
                    )
                    saved += 1
                    continue
                try:
                    qty = float(item.get("quantity"))
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    self._sql(
                        "INSERT INTO supply_balances "
                        "(user_id, production_id, item_type, item_id, balance_date, "
                        "quantity, updated_at, updated_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (user_id, production_id, item_type, item_id, balance_date) "
                        "DO UPDATE SET quantity = EXCLUDED.quantity, "
                        "updated_at = EXCLUDED.updated_at, "
                        "updated_by = EXCLUDED.updated_by"
                    ),
                    (
                        user_id,
                        int(production_id),
                        item_type,
                        item_id,
                        date_s,
                        qty,
                        now,
                        updated_by,
                    ),
                )
                saved += 1
        return saved

    @staticmethod
    def _parse_supply_balance_min_qty(raw: object) -> float | None:
        """Empty / null → no threshold; otherwise non-negative float."""
        if raw is None:
            return None
        if isinstance(raw, str) and not str(raw).strip():
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        if val < 0 or val != val:  # NaN
            return None
        return val

    def list_supply_balance_visibility(self, *, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT item_type, item_id, visible, sort_order, min_qty "
                    "FROM supply_balance_visibility WHERE user_id = ?"
                ),
                (user_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = self._row_to_dict(r)
            try:
                d["sort_order"] = int(d.get("sort_order") or 0)
            except (TypeError, ValueError):
                d["sort_order"] = 0
            d["visible"] = bool(d.get("visible", True))
            d["min_qty"] = self._parse_supply_balance_min_qty(d.get("min_qty"))
            out.append(d)
        return out

    def set_supply_balance_visibility(
        self, *, user_id: int, items: list[dict[str, Any]]
    ) -> int:
        saved = 0
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            for item in items or []:
                item_type = str(item.get("item_type") or "").strip().lower()
                if item_type not in {"material", "product"}:
                    continue
                try:
                    item_id = int(item.get("item_id") or 0)
                except (TypeError, ValueError):
                    continue
                if item_id <= 0:
                    continue
                visible = bool(item.get("visible", True))
                try:
                    sort_order = int(item.get("sort_order") or 0)
                except (TypeError, ValueError):
                    sort_order = 0
                min_qty = self._parse_supply_balance_min_qty(item.get("min_qty"))
                conn.execute(
                    self._sql(
                        "INSERT INTO supply_balance_visibility "
                        "(user_id, item_type, item_id, visible, sort_order, min_qty) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (user_id, item_type, item_id) "
                        "DO UPDATE SET visible = EXCLUDED.visible, "
                        "sort_order = EXCLUDED.sort_order, "
                        "min_qty = EXCLUDED.min_qty"
                    ),
                    (
                        user_id,
                        item_type,
                        item_id,
                        self._bool_db(visible),
                        sort_order,
                        min_qty,
                    ),
                )
                saved += 1
        return saved

    @staticmethod
    def supply_balance_item_sort_key(
        *,
        item_type: str,
        item_id: int,
        name: str,
        sort_map: dict[tuple[str, int], int],
    ) -> tuple:
        """Sort key for Остатки rows: explicit sort_order, then name, then id."""
        key = (str(item_type or "").strip().lower(), int(item_id or 0))
        if key in sort_map:
            order = int(sort_map[key])
        else:
            order = 10**9
        return (order, str(name or "").casefold(), key[1])

    # ── Stock ledger (movements) ─────────────────────────────────────────────

    _STOCK_KINDS = frozenset(
        {"opening", "receipt", "fbs_ship", "adjustment", "fbs_reverse"}
    )

    def get_product_id_by_article_map(self, *, user_id: int) -> dict[str, int]:
        """Map supplier_article / wb_nmid (+ casefold) → product_photos.id."""
        rows = self.list_product_photos(user_id=user_id)
        result: dict[str, int] = {}
        for r in rows:
            try:
                pid = int(r.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            for field in ("supplier_article", "wb_nmid"):
                key = str(r.get(field) or "").strip()
                if not key:
                    continue
                result[key] = pid
                result[key.casefold()] = pid
        return result

    def get_wb_fbs_barcodes_by_product_id(
        self, *, user_id: int
    ) -> dict[int, list[str]]:
        """Collect product ШК from local FBS orders, keyed by product_photos.id.

        Matches order ``article`` / ``nm_id`` to catalog supplier_article / wb_nmid.
        Used by Остатки product cards (same ШК source as поставки).
        """
        id_map = self.get_product_id_by_article_map(user_id=user_id)
        if not id_map:
            return {}
        out: dict[int, list[str]] = {}
        try:
            with self._connect() as conn:
                # wb_fbs_orders may be absent before first FBS sync.
                rows = conn.execute(
                    self._sql(
                        """
                        SELECT article, nm_id, skus_json
                        FROM wb_fbs_orders
                        WHERE user_id = ?
                          AND COALESCE(skus_json, '') NOT IN ('', '[]', 'null')
                        """
                    ),
                    (user_id,),
                ).fetchall()
        except Exception:
            return {}
        for row in rows:
            d = self._row_to_dict(row)
            try:
                parsed = json.loads(d.get("skus_json") or "[]")
            except Exception:
                parsed = []
            if not isinstance(parsed, list) or not parsed:
                continue
            barcodes: list[str] = []
            for sku in parsed:
                text = str(sku or "").strip()
                if text and text not in barcodes:
                    barcodes.append(text)
            if not barcodes:
                continue
            keys = [
                str(d.get("article") or "").strip(),
                str(d.get("nm_id") or "").strip(),
            ]
            product_ids: set[int] = set()
            for key in keys:
                if not key:
                    continue
                pid = id_map.get(key) or id_map.get(key.casefold())
                if pid:
                    product_ids.add(int(pid))
            for pid in product_ids:
                bucket = out.setdefault(pid, [])
                for b in barcodes:
                    if b not in bucket:
                        bucket.append(b)
        return out

    def count_supply_stock_movements(
        self, *, user_id: int, production_id: int
    ) -> int:
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            row = conn.execute(
                self._sql(
                    "SELECT COUNT(*) AS cnt FROM supply_stock_movements "
                    "WHERE user_id = ? AND production_id = ?"
                ),
                (user_id, int(production_id)),
            ).fetchone()
        d = self._row_to_dict(row) if row else {}
        try:
            return int(d.get("cnt") or 0)
        except (TypeError, ValueError):
            return 0

    def migrate_legacy_supply_balances_to_movements(
        self, *, user_id: int, production_id: int, created_by: int | None = None
    ) -> int:
        """Import latest legacy snapshot cells into opening movements.

        Idempotent: deterministic ``source_id`` + ON CONFLICT DO NOTHING.
        Safe to call on every GET; concurrent callers will not double-count.
        """
        dates = self.list_supply_balance_dates(
            user_id=user_id, production_id=production_id
        )
        if not dates:
            return 0
        latest = dates[-1]
        cells = self.list_supply_balances(
            user_id=user_id, production_id=production_id, dates=[latest]
        )
        items: list[dict[str, Any]] = []
        for cell in cells:
            item_type = str(cell.get("item_type") or "").strip().lower()
            if item_type not in {"material", "product"}:
                continue
            try:
                item_id = int(cell.get("item_id") or 0)
                qty = float(cell.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            if item_id <= 0 or qty == 0:
                continue
            items.append(
                {
                    "item_type": item_type,
                    "item_id": item_id,
                    "qty": qty,
                    # Deterministic — concurrent migrates cannot duplicate a cell.
                    "source_id": f"legacy:{latest}:{item_type}:{item_id}",
                }
            )
        if not items:
            return 0
        return self.add_supply_stock_movements(
            user_id=user_id,
            production_id=production_id,
            movement_date=latest,
            kind="opening",
            source_type="legacy_balance",
            items=items,
            comment="Импорт из прежних остатков",
            created_by=created_by,
        )

    def list_supply_stock_movements_for_item(
        self,
        *,
        user_id: int,
        production_id: int,
        item_type: str,
        item_id: int,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Recent ledger rows for one catalog item (newest first)."""
        itype = str(item_type or "").strip().lower()
        if itype not in {"material", "product"}:
            return []
        try:
            iid = int(item_id or 0)
            pid = int(production_id or 0)
            lim = int(limit or 0)
        except (TypeError, ValueError):
            return []
        if iid <= 0 or pid <= 0:
            return []
        lim = max(1, min(lim, 200))
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT id, item_type, item_id, qty, movement_date, kind, "
                    "source_type, source_id, comment, created_at, created_by "
                    "FROM supply_stock_movements "
                    "WHERE user_id = ? AND production_id = ? "
                    "AND item_type = ? AND item_id = ? "
                    "ORDER BY movement_date DESC, id DESC "
                    "LIMIT ?"
                ),
                (user_id, pid, itype, iid, lim),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = self._row_to_dict(r)
            try:
                d["id"] = int(d.get("id") or 0)
            except (TypeError, ValueError):
                d["id"] = 0
            try:
                d["item_id"] = int(d.get("item_id") or 0)
            except (TypeError, ValueError):
                d["item_id"] = 0
            try:
                d["qty"] = float(d.get("qty"))
            except (TypeError, ValueError):
                continue
            try:
                created_by = d.get("created_by")
                d["created_by"] = int(created_by) if created_by not in (None, "") else None
            except (TypeError, ValueError):
                d["created_by"] = None
            d["item_type"] = str(d.get("item_type") or itype)
            d["movement_date"] = str(d.get("movement_date") or "")
            d["kind"] = str(d.get("kind") or "")
            d["source_type"] = str(d.get("source_type") or "")
            d["source_id"] = str(d.get("source_id") or "")
            d["comment"] = str(d.get("comment") or "")
            d["created_at"] = str(d.get("created_at") or "")
            out.append(d)
        return out

    def list_supply_stock_movement_dates(
        self, *, user_id: int, production_id: int, as_of: str | None = None
    ) -> list[str]:
        as_of_s = str(as_of or "").strip()
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            if as_of_s:
                rows = conn.execute(
                    self._sql(
                        "SELECT DISTINCT movement_date FROM supply_stock_movements "
                        "WHERE user_id = ? AND production_id = ? AND movement_date <= ? "
                        "ORDER BY movement_date ASC"
                    ),
                    (user_id, int(production_id), as_of_s),
                ).fetchall()
            else:
                rows = conn.execute(
                    self._sql(
                        "SELECT DISTINCT movement_date FROM supply_stock_movements "
                        "WHERE user_id = ? AND production_id = ? "
                        "ORDER BY movement_date ASC"
                    ),
                    (user_id, int(production_id)),
                ).fetchall()
        out: list[str] = []
        for r in rows:
            d = self._row_to_dict(r)
            val = str(d.get("movement_date") or "").strip()
            if val:
                out.append(val)
        return out

    def sum_supply_stock_balances(
        self,
        *,
        user_id: int,
        production_id: int,
        as_of: str,
    ) -> dict[tuple[str, int], float]:
        """Cumulative balance per item with movement_date <= as_of."""
        as_of_s = str(as_of or "").strip()
        if not as_of_s:
            return {}
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            rows = conn.execute(
                self._sql(
                    "SELECT item_type, item_id, COALESCE(SUM(qty), 0) AS balance "
                    "FROM supply_stock_movements "
                    "WHERE user_id = ? AND production_id = ? AND movement_date <= ? "
                    "GROUP BY item_type, item_id"
                ),
                (user_id, int(production_id), as_of_s),
            ).fetchall()
        out: dict[tuple[str, int], float] = {}
        for r in rows:
            d = self._row_to_dict(r)
            item_type = str(d.get("item_type") or "").strip().lower()
            try:
                item_id = int(d.get("item_id") or 0)
                bal = float(d.get("balance") or 0)
            except (TypeError, ValueError):
                continue
            if item_type not in {"material", "product"} or item_id <= 0:
                continue
            out[(item_type, item_id)] = bal
        return out

    def add_supply_stock_movements(
        self,
        *,
        user_id: int,
        production_id: int,
        movement_date: str,
        kind: str,
        source_type: str,
        items: list[dict[str, Any]],
        comment: str = "",
        created_by: int | None = None,
    ) -> int:
        """Insert signed ledger rows. Each item needs qty and unique source_id."""
        import uuid as _uuid

        kind_s = str(kind or "").strip().lower()
        if kind_s not in self._STOCK_KINDS:
            return 0
        date_s = str(movement_date or "").strip()
        if not date_s:
            return 0
        src_type = str(source_type or "manual").strip() or "manual"
        comment_s = str(comment or "").strip()
        now = _utc_now()
        saved = 0
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            for item in items or []:
                item_type = str(item.get("item_type") or "").strip().lower()
                if item_type not in {"material", "product"}:
                    continue
                try:
                    item_id = int(item.get("item_id") or 0)
                    qty = float(item.get("qty"))
                except (TypeError, ValueError):
                    continue
                if item_id <= 0 or qty == 0:
                    continue
                source_id = str(item.get("source_id") or "").strip()
                if not source_id:
                    source_id = f"{src_type}:{kind_s}:{_uuid.uuid4().hex}"
                row_comment = str(item.get("comment") or comment_s or "").strip()
                cur = conn.execute(
                    self._sql(
                        "INSERT INTO supply_stock_movements "
                        "(user_id, production_id, item_type, item_id, qty, "
                        "movement_date, kind, source_type, source_id, comment, "
                        "created_at, created_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (user_id, source_type, source_id) DO NOTHING"
                    ),
                    (
                        user_id,
                        int(production_id),
                        item_type,
                        item_id,
                        qty,
                        date_s,
                        kind_s,
                        src_type,
                        source_id,
                        row_comment,
                        now,
                        created_by,
                    ),
                )
                if int(getattr(cur, "rowcount", 0) or 0) > 0:
                    saved += 1
        return saved

    def _fbs_stock_ship_counts(
        self, conn, *, user_id: int, order_id: int
    ) -> tuple[int, int]:
        """Return (ships, reverses) for a WB FBS order, including legacy source_ids."""
        oid = str(int(order_id))
        rows = conn.execute(
            self._sql(
                """
                SELECT kind, source_type, source_id FROM supply_stock_movements
                WHERE user_id = ?
                  AND (
                    (source_type = 'wb_fbs_order'
                     AND (source_id = ? OR source_id LIKE ?))
                    OR
                    (source_type = 'wb_fbs_order_reverse'
                     AND (source_id = ? OR source_id LIKE ?))
                  )
                """
            ),
            (user_id, oid, f"{oid}:s:%", oid, f"{oid}:r:%"),
        ).fetchall()
        ships = 0
        reverses = 0
        for r in rows:
            d = self._row_to_dict(r)
            kind = str(d.get("kind") or "").strip().lower()
            src_type = str(d.get("source_type") or "").strip().lower()
            if src_type == "wb_fbs_order_reverse" or kind == "fbs_reverse":
                reverses += 1
            elif src_type == "wb_fbs_order" or kind == "fbs_ship":
                ships += 1
        return ships, reverses

    def settle_open_wb_fbs_orders_for_stock(
        self,
        *,
        user_id: int,
        production_id: int,
        reason: str = "adjustment",
    ) -> int:
        """Mark current delivery/finished FBS orders as already in the stock figure.

        After opening/adjustment the physical count already includes goods that
        left with open deliveries — those order ids must not auto-ship later.
        New orders (not settled) still ship on enter delivery/finished.
        """
        now = _utc_now()
        reason_s = str(reason or "adjustment").strip() or "adjustment"
        settled = 0
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            try:
                rows = conn.execute(
                    self._sql(
                        """
                        SELECT order_id FROM wb_fbs_orders
                        WHERE user_id = ?
                          AND is_archive = FALSE
                          AND tab IN ('delivery', 'finished')
                        """
                    ),
                    (user_id,),
                ).fetchall()
            except Exception:
                # wb_fbs_orders may be absent in some environments/tests
                return 0
            for r in rows:
                d = self._row_to_dict(r)
                try:
                    oid = int(d.get("order_id") or 0)
                except (TypeError, ValueError):
                    continue
                if oid <= 0:
                    continue
                cur = conn.execute(
                    self._sql(
                        "INSERT INTO supply_stock_fbs_settled "
                        "(user_id, order_id, production_id, settled_at, reason) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (user_id, order_id) DO NOTHING"
                    ),
                    (user_id, oid, int(production_id), now, reason_s),
                )
                if int(getattr(cur, "rowcount", 0) or 0) > 0:
                    settled += 1
        return settled

    def is_wb_fbs_order_stock_settled(
        self, conn, *, user_id: int, order_id: int
    ) -> bool:
        row = conn.execute(
            self._sql(
                "SELECT 1 AS ok FROM supply_stock_fbs_settled "
                "WHERE user_id = ? AND order_id = ? LIMIT 1"
            ),
            (user_id, int(order_id)),
        ).fetchone()
        return bool(row)

    def reconcile_wb_fbs_stock_orders(
        self,
        *,
        user_id: int,
        production_id: int,
        orders: list[dict[str, Any]],
        movement_date: str,
    ) -> dict[str, int]:
        """Align ledger with current FBS tabs (idempotent, cycle-safe).

        Desired state:
        - ``delivery`` / ``finished`` → net shipped == 1 (goods left warehouse)
        - ``new`` / ``assembly`` → net shipped == 0 (still on warehouse / cancelled before ship)
        - ``cancelled`` and others → leave net as-is (do not auto-reverse after ship)
        - orders in ``supply_stock_fbs_settled`` (after stock adjustment) are ignored

        Uses sequenced source_ids (``{oid}:s:{n}`` / ``{oid}:r:{n}``) so
        assembly→delivery→assembly→delivery can ship again. Legacy
        ``source_id=oid`` rows still count toward net.
        """
        stats = {"shipped": 0, "reversed": 0, "skipped": 0, "ok": 0, "settled": 0}
        if not orders or int(production_id or 0) <= 0:
            return stats
        date_s = str(movement_date or "").strip()
        if not date_s:
            return stats
        id_map = self.get_product_id_by_article_map(user_id=user_id)
        now = _utc_now()
        with self._connect() as conn:
            self._ensure_supply_balances_tables(conn)
            for order in orders:
                try:
                    oid = int(order.get("order_id") or 0)
                except (TypeError, ValueError):
                    stats["skipped"] += 1
                    continue
                if oid <= 0:
                    stats["skipped"] += 1
                    continue
                if self.is_wb_fbs_order_stock_settled(
                    conn, user_id=user_id, order_id=oid
                ):
                    stats["settled"] += 1
                    continue
                tab = str(order.get("tab") or "").strip().lower()
                article = str(order.get("article") or "").strip()
                nm_id = str(order.get("nm_id") or "").strip()
                product_id = 0
                for key in (article, article.casefold(), nm_id, nm_id.casefold()):
                    if key and key in id_map:
                        product_id = int(id_map[key])
                        break
                if product_id <= 0:
                    stats["skipped"] += 1
                    continue

                ships, reverses = self._fbs_stock_ship_counts(
                    conn, user_id=user_id, order_id=oid
                )
                net = ships - reverses

                def _insert(kind: str, qty: float, source_type: str, source_id: str) -> bool:
                    cur = conn.execute(
                        self._sql(
                            "INSERT INTO supply_stock_movements "
                            "(user_id, production_id, item_type, item_id, qty, "
                            "movement_date, kind, source_type, source_id, comment, "
                            "created_at, created_by) "
                            "VALUES (?, ?, 'product', ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                            "ON CONFLICT (user_id, source_type, source_id) DO NOTHING"
                        ),
                        (
                            user_id,
                            int(production_id),
                            product_id,
                            qty,
                            date_s,
                            kind,
                            source_type,
                            source_id,
                            f"WB FBS заказ {oid}",
                            now,
                        ),
                    )
                    return int(getattr(cur, "rowcount", 0) or 0) > 0

                if tab in {"delivery", "finished"}:
                    if net >= 1:
                        stats["ok"] += 1
                        continue
                    # Need one ship (supports re-ship after a prior reverse).
                    seq = ships + 1
                    source_id = f"{oid}:s:{seq}"
                    if _insert("fbs_ship", -1.0, "wb_fbs_order", source_id):
                        stats["shipped"] += 1
                    else:
                        stats["skipped"] += 1
                elif tab in {"new", "assembly"}:
                    if net <= 0:
                        stats["ok"] += 1
                        continue
                    # Only reverse when a ship exists (net > 0).
                    seq = reverses + 1
                    source_id = f"{oid}:r:{seq}"
                    if _insert(
                        "fbs_reverse", 1.0, "wb_fbs_order_reverse", source_id
                    ):
                        stats["reversed"] += 1
                    else:
                        stats["skipped"] += 1
                else:
                    # cancelled / archive / unknown — do not auto-mutate
                    stats["ok"] += 1
        return stats

    def apply_wb_fbs_stock_tab_transitions(
        self,
        *,
        user_id: int,
        production_id: int,
        transitions: list[dict[str, Any]],
        movement_date: str,
    ) -> dict[str, int]:
        """Back-compat wrapper: map transitions to reconcile desired tabs."""
        orders: list[dict[str, Any]] = []
        for tr in transitions or []:
            orders.append(
                {
                    "order_id": tr.get("order_id"),
                    "tab": tr.get("new_tab"),
                    "article": tr.get("article"),
                    "nm_id": tr.get("nm_id"),
                }
            )
        return self.reconcile_wb_fbs_stock_orders(
            user_id=user_id,
            production_id=production_id,
            orders=orders,
            movement_date=movement_date,
        )
