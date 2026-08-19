"""WB FBS → Chestny Znak circulation (вывод / возврат КИЗ).

«Ежедневный вывод» for a date range is driven by Marketplace **FBS** assembly
orders in that period with the statuses that require CHZ action:
- ``wbStatus=sold`` → вывод (op=1);
- ``wbStatus=canceled_by_client`` / ``defect`` → ввод (op=2).

КИЗ берётся из Marketplace ``meta.sgtin`` (и при наличии дополняется фискалом
из WB Analytics ``excise-report`` по mid-token ``srid``↔``rid``).
FBO / прочие статусы в очередь не пишем.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .chz_true_api import (
    DEMO_BASE,
    PROD_BASE,
    ChzTrueApiClient,
    ChzTrueApiError,
    build_lk_receipt_document,
    build_lp_return_document,
)
from .repository import ReviewRepository
from .security import decrypt_secret, encrypt_secret, mask_secret

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
WB_ANALYTICS_API = "https://seller-analytics-api.wildberries.ru"

# In-process cancel flags for async «Ежедневный вывод» (one uvicorn worker).
_SYNC_CANCEL_LOCK = threading.Lock()
_SYNC_CANCEL: dict[int, threading.Event] = {}


class SyncCancelled(Exception):
    """User requested stop of an in-flight excise sync."""


def _register_sync_cancel(run_id: int) -> threading.Event:
    """Attach (or reuse) a cancel Event for ``run_id``.

    Critical: never replace a flag that is already set — «Стоп» may race the
    worker's first ``_register_sync_cancel`` after ``create_excise_sync_run``.
    """
    rid = int(run_id)
    with _SYNC_CANCEL_LOCK:
        existing = _SYNC_CANCEL.get(rid)
        if existing is not None:
            return existing
        ev = threading.Event()
        _SYNC_CANCEL[rid] = ev
        return ev


def _clear_sync_cancel(run_id: int) -> None:
    with _SYNC_CANCEL_LOCK:
        _SYNC_CANCEL.pop(int(run_id), None)


def request_cancel_excise_sync(run_id: int) -> bool:
    """Signal a running sync to stop. Returns True if a cancel flag was set."""
    rid = int(run_id or 0)
    if rid <= 0:
        return False
    with _SYNC_CANCEL_LOCK:
        ev = _SYNC_CANCEL.get(rid)
        if ev is None:
            ev = threading.Event()
            _SYNC_CANCEL[rid] = ev
        ev.set()
        return True


def _sync_cancel_requested(run_id: int | None) -> bool:
    rid = int(run_id or 0)
    if rid <= 0:
        return False
    with _SYNC_CANCEL_LOCK:
        ev = _SYNC_CANCEL.get(rid)
    return bool(ev is not None and ev.is_set())


def _check_sync_cancelled(run_id: int | None) -> None:
    if _sync_cancel_requested(run_id):
        raise SyncCancelled("Синхронизация остановлена")

OP_WITHDRAW = 1
OP_RETURN = 2

# Marketplace wbStatus → circulation operation for «Ежедневный вывод».
WB_STATUS_WITHDRAW = frozenset({"sold"})
WB_STATUS_RETURN = frozenset({"canceled_by_client", "defect"})
WB_STATUS_CIRCULATION = WB_STATUS_WITHDRAW | WB_STATUS_RETURN

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_SKIPPED = "skipped"
STATUS_ACCEPTED = "accepted"
STATUS_ERROR = "error"
STATUS_SUBMITTED = "submitted"

# ASCII codes only in SQL — Cyrillic in ILIKE caused UnicodeDecodeError on PG.
SKIP_NO_FISCAL = "no_fiscal"
# Excise-report row has no matching Marketplace FBS order (likely FBO/other).
SKIP_NOT_FBS = "not_fbs"
# FBS order exists but wrong lifecycle for this operation.
SKIP_NOT_SOLD = "not_sold"  # withdraw only when wbStatus=sold
SKIP_NOT_RETURN = "not_return"  # return-to-circulation only when cancelled/отказ
# Prepare sticky closes — persist so oldest-first queue cannot HOL-block.
SKIP_EMPTY_CIS = "пустой КИЗ"
SKIP_BAD_CIS = "bad_cis"
SKIP_NO_PRODUCT_COST = "нет цены за единицу (product_cost)"
SKIP_NO_PLACE = "нет КПП/ФИАС у юр. лица"
SKIP_STALE_SUBMITTED = "stale_submitted"
# Primary document when WB excise-report has no fiscal receipt (True API OTHER).
NO_FISCAL_PRIMARY_DOC_TYPE = "OTHER"
NO_FISCAL_PRIMARY_DOC_NAME = "Без документа основания"
# Extra list→build rounds when sticky closes free slots under the 2000 head.
PREPARE_QUEUE_DRAIN_PASSES = 3
# Submitted with chz_doc_id but no terminal CHZ status for too long → sticky skip.
STALE_SUBMITTED_DAYS = 7
# Hydrate open-orders window for Analytics↔FBS match (archive pages cover sold).
KIZ_HYDRATE_LOOKBACK_MAX_DAYS = 90


def _is_no_fiscal_reason(reason: str) -> bool:
    raw = str(reason or "").strip().lower()
    if not raw:
        return False
    if raw == SKIP_NO_FISCAL or raw.startswith(f"{SKIP_NO_FISCAL}:"):
        return True
    # Legacy Russian reasons written before the ASCII-code fix.
    return ("нет номера" in raw) or ("нет чека" in raw) or ("нет чек" in raw)

# Oldest-first prepare batch; chunk products so CHZ docs stay within size limits.
PREPARE_EVENT_LIMIT = 2000
CHZ_PRODUCTS_PER_DOC = 100
# UKЭP signs one detached CAdES per document in the browser — keep rounds small.
# 1110 docs in one prepare made "Отправить в ЧЗ" unusable (hours of signing, no submit).
CHZ_DOCUMENTS_PER_PREPARE = 40
# Full event rows (UI/history) — ~6 months. Slim sent-CIS registry is kept forever.
EVENT_RETENTION_DAYS = 180
PURGE_BATCH_SIZE = 1000
# Keep soft-skipped not_fbs for a while so late FBS hydrate can requeue them.
# Immediate delete after sync persist would defeat repair_requeue_*.
NOT_FBS_PURGE_MIN_AGE_DAYS = 14
# Storage GC is not free — skip if ran recently (prepare can loop many rounds).
STORAGE_MAINTAIN_MIN_INTERVAL_HOURS = 12

CHZ_STATUS_SUCCESS = frozenset(
    {"ACCEPTED", "CHECKED_OK", "SUCCESS", "OK", "PROCESSED"}
)
CHZ_STATUS_FAILED = frozenset(
    {
        "CHECKED_NOT_OK",
        "PROCESSING_ERROR",
        "REJECTED",
        "ERROR",
        "FAILED",
        "CANCELLED",
        "CANCELED",
        "NOT_ACCEPTED",
        "PARSE_ERROR",
    }
)


def _moscow_today() -> str:
    return datetime.now(MSK).date().isoformat()


def _parse_date(s: str, *, default: str = "") -> str:
    raw = str(s or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    return default or _moscow_today()


def resolve_excise_period(
    *,
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    """Exact [date_from, date_to] from the modal — no watermark, no ceiling.

    Raises ValueError if either date is missing/invalid.
    """
    raw_from = str(date_from or "").strip()
    raw_to = str(date_to or "").strip()
    if not raw_from or not raw_to:
        raise ValueError("Укажите даты «С» и «По» в модалке «Вывод КИЗ»")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_from):
        raise ValueError(f"Некорректная дата «С»: {raw_from}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_to):
        raise ValueError(f"Некорректная дата «По»: {raw_to}")
    from_d = date.fromisoformat(raw_from)
    to_d = date.fromisoformat(raw_to)
    if from_d > to_d:
        from_d, to_d = to_d, from_d
    return {
        "date_from": from_d.isoformat(),
        "date_to": to_d.isoformat(),
        "days": (to_d - from_d).days + 1,
    }


def _event_key(
    *,
    srid: str,
    excise_short: str,
    operation_type: int,
    fiscal_doc_number: str,
    fiscal_dt: str,
) -> str:
    blob = "|".join(
        [
            str(srid or "").strip(),
            str(excise_short or "").strip(),
            str(int(operation_type or 0)),
            str(fiscal_doc_number or "").strip(),
            str(fiscal_dt or "").strip(),
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:40]


def _cis_identity(
    *,
    srid: str = "",
    rid: str = "",
    excise_short: str = "",
    operation_type: int = 0,
) -> tuple[str, str, int]:
    """Stable КИЗ identity ignoring fiscal receipt (srid/rid + cis + op).

    Primary anchor prefers ``srid`` then ``rid`` (raw). For matching across
    Marketplace/Analytics suffix variants use ``_cis_identity_keys``.
    """
    srid_s = str(srid or "").strip()
    rid_s = str(rid or "").strip()
    anchor = srid_s or rid_s
    return (anchor, str(excise_short or "").strip(), int(operation_type or 0))


def _cis_identity_keys(
    *,
    srid: str = "",
    rid: str = "",
    excise_short: str = "",
    operation_type: int = 0,
) -> set[tuple[str, str, int]]:
    """All fold-aware identity keys for one CIS (anti-dupe / related match)."""
    cis = str(excise_short or "").strip()
    op = int(operation_type or 0)
    if not cis:
        return set()
    keys: set[tuple[str, str, int]] = set()
    for raw in (srid, rid):
        for anchor in _rid_match_keys(raw):
            keys.add((anchor, cis, op))
    primary = _cis_identity(
        srid=srid, rid=rid, excise_short=cis, operation_type=op
    )
    if primary[0]:
        keys.add(primary)
    elif not keys:
        keys.add(("", cis, op))
    return keys


def _event_has_fiscal(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return bool(
        str(row.get("fiscal_doc_number") or "").strip()
        and str(row.get("fiscal_dt") or "").strip()
    )


def _fiscal_doc_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value).strip()
    return str(value).strip()


def ensure_kiz_circulation_tables(repo: ReviewRepository) -> None:
    with repo._connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supply_chz_settings (
                user_id BIGINT PRIMARY KEY,
                is_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                api_base TEXT NOT NULL DEFAULT '',
                participant_inn TEXT NOT NULL DEFAULT '',
                product_group TEXT NOT NULL DEFAULT '',
                kpp TEXT NOT NULL DEFAULT '',
                fias_id TEXT NOT NULL DEFAULT '',
                return_type TEXT NOT NULL DEFAULT 'REMOTE_SALE_RETURN',
                cert_thumbprint TEXT NOT NULL DEFAULT '',
                wb_analytics_api_key_encrypted TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        try:
            conn.execute(
                "ALTER TABLE supply_chz_settings "
                "ADD COLUMN IF NOT EXISTS wb_analytics_api_key_encrypted "
                "TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wb_kiz_circulation_cursor (
                user_id BIGINT NOT NULL,
                source_id BIGINT NOT NULL,
                last_date_to TEXT NOT NULL DEFAULT '',
                last_event_key TEXT NOT NULL DEFAULT '',
                last_fiscal_dt TEXT NOT NULL DEFAULT '',
                last_run_at TEXT NOT NULL DEFAULT '',
                last_run_id BIGINT,
                last_storage_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (user_id, source_id)
            )
            """
        )
        try:
            conn.execute(
                "ALTER TABLE wb_kiz_circulation_cursor "
                "ADD COLUMN IF NOT EXISTS last_storage_at TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wb_kiz_circulation_runs (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                source_id BIGINT NOT NULL,
                date_from TEXT NOT NULL DEFAULT '',
                date_to TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                fetched INTEGER NOT NULL DEFAULT 0,
                inserted INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                withdraw_count INTEGER NOT NULL DEFAULT 0,
                return_count INTEGER NOT NULL DEFAULT 0,
                error_text TEXT NOT NULL DEFAULT '',
                log_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            repo._sql(
                "CREATE INDEX IF NOT EXISTS idx_wb_kiz_circ_runs_user "
                "ON wb_kiz_circulation_runs(user_id, created_at DESC)"
            )
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wb_kiz_circulation_events (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                source_id BIGINT NOT NULL,
                event_key TEXT NOT NULL,
                operation_type INTEGER NOT NULL DEFAULT 0,
                srid TEXT NOT NULL DEFAULT '',
                rid TEXT NOT NULL DEFAULT '',
                nm_id BIGINT,
                barcode TEXT NOT NULL DEFAULT '',
                excise_short TEXT NOT NULL DEFAULT '',
                fiscal_doc_number TEXT NOT NULL DEFAULT '',
                fiscal_dt TEXT NOT NULL DEFAULT '',
                fiscal_drive_number TEXT NOT NULL DEFAULT '',
                price DOUBLE PRECISION,
                currency_name TEXT NOT NULL DEFAULT '',
                country_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                skip_reason TEXT NOT NULL DEFAULT '',
                chz_doc_id TEXT NOT NULL DEFAULT '',
                chz_status TEXT NOT NULL DEFAULT '',
                error_text TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                run_id BIGINT,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE (user_id, source_id, event_key)
            )
            """
        )
        try:
            conn.execute(
                "ALTER TABLE wb_kiz_circulation_events "
                "ADD COLUMN IF NOT EXISTS currency_name TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass
        # Live CIS card from True API /cises/info (not the document status).
        for col_sql in (
            "ADD COLUMN IF NOT EXISTS cis_status TEXT NOT NULL DEFAULT ''",
            "ADD COLUMN IF NOT EXISTS cis_owner_inn TEXT NOT NULL DEFAULT ''",
            "ADD COLUMN IF NOT EXISTS cis_status_error TEXT NOT NULL DEFAULT ''",
            "ADD COLUMN IF NOT EXISTS cis_checked_at TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(
                    f"ALTER TABLE wb_kiz_circulation_events {col_sql}"
                )
            except Exception:
                pass
        conn.execute(
            repo._sql(
                "CREATE INDEX IF NOT EXISTS idx_wb_kiz_circ_events_user_src "
                "ON wb_kiz_circulation_events(user_id, source_id, fiscal_dt ASC, id ASC)"
            )
        )
        conn.execute(
            repo._sql(
                "CREATE INDEX IF NOT EXISTS idx_wb_kiz_circ_events_status "
                "ON wb_kiz_circulation_events(user_id, source_id, status, operation_type)"
            )
        )
        conn.execute(
            repo._sql(
                "CREATE INDEX IF NOT EXISTS idx_wb_kiz_circ_events_purge "
                "ON wb_kiz_circulation_events(user_id, source_id, status, updated_at)"
            )
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wb_kiz_chz_documents (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                source_id BIGINT NOT NULL,
                run_id BIGINT,
                doc_type TEXT NOT NULL DEFAULT '',
                chz_doc_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                event_keys_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # Forever-kept compact anti-dupe + support trail (CIS → chz_doc_id).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wb_kiz_sent_cis (
                user_id BIGINT NOT NULL,
                source_id BIGINT NOT NULL,
                operation_type INTEGER NOT NULL DEFAULT 0,
                excise_short TEXT NOT NULL,
                anchor TEXT NOT NULL DEFAULT '',
                chz_doc_id TEXT NOT NULL DEFAULT '',
                event_key TEXT NOT NULL DEFAULT '',
                fiscal_doc_number TEXT NOT NULL DEFAULT '',
                fiscal_dt TEXT NOT NULL DEFAULT '',
                accepted_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (user_id, source_id, operation_type, excise_short, anchor)
            )
            """
        )
        conn.execute(
            repo._sql(
                "CREATE INDEX IF NOT EXISTS idx_wb_kiz_sent_cis_user_src "
                "ON wb_kiz_sent_cis(user_id, source_id, accepted_at DESC)"
            )
        )


def repair_stuck_return_events(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> int:
    """Re-queue returns that were wrongly marked error for missing fiscal."""
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    fixed = 0
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                """
                SELECT id, skip_reason FROM wb_kiz_circulation_events
                WHERE user_id = ? AND source_id = ?
                  AND operation_type = ? AND status = ?
                """
            ),
            (user_id, source_id, OP_RETURN, STATUS_ERROR),
        ).fetchall()
        for row in rows:
            d = repo._row_to_dict(row)
            if not _is_no_fiscal_reason(str(d.get("skip_reason") or "")):
                continue
            conn.execute(
                repo._sql(
                    """
                    UPDATE wb_kiz_circulation_events
                    SET status = ?, skip_reason = '', error_text = '', updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """
                ),
                (STATUS_PENDING, now, int(d["id"]), user_id),
            )
            fixed += 1
    return fixed


def repair_unhealable_withdraw_errors(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> int:
    """Move withdraw-without-fiscal from error → pending (OTHER primary doc path)."""
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    fixed = 0
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                """
                SELECT id, skip_reason FROM wb_kiz_circulation_events
                WHERE user_id = ? AND source_id = ?
                  AND operation_type = ? AND status = ?
                """
            ),
            (user_id, source_id, OP_WITHDRAW, STATUS_ERROR),
        ).fetchall()
        for row in rows:
            d = repo._row_to_dict(row)
            if not _is_no_fiscal_reason(str(d.get("skip_reason") or "")):
                continue
            conn.execute(
                repo._sql(
                    """
                    UPDATE wb_kiz_circulation_events
                    SET status = ?, skip_reason = ?, error_text = '', updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """
                ),
                (STATUS_PENDING, SKIP_NO_FISCAL, now, int(d["id"]), user_id),
            )
            fixed += 1
    return fixed


def repair_nofiscal_withdraw_to_pending(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> int:
    """Re-queue historical withdraw-without-fiscal from skipped → pending."""
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    fixed = 0
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                """
                SELECT id, skip_reason FROM wb_kiz_circulation_events
                WHERE user_id = ? AND source_id = ?
                  AND operation_type = ? AND status = ?
                """
            ),
            (user_id, source_id, OP_WITHDRAW, STATUS_SKIPPED),
        ).fetchall()
        for row in rows:
            d = repo._row_to_dict(row)
            if not _is_no_fiscal_reason(str(d.get("skip_reason") or "")):
                continue
            conn.execute(
                repo._sql(
                    """
                    UPDATE wb_kiz_circulation_events
                    SET status = ?, skip_reason = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """
                ),
                (STATUS_PENDING, SKIP_NO_FISCAL, now, int(d["id"]), user_id),
            )
            fixed += 1
    return fixed


def repair_orphan_submitted_events(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> int:
    """Re-queue submitted rows that never got a CHZ document id (local fault)."""
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    with repo._connect() as conn:
        cur = conn.execute(
            repo._sql(
                """
                UPDATE wb_kiz_circulation_events
                SET status = ?, chz_status = '', error_text = ?, updated_at = ?
                WHERE user_id = ? AND source_id = ?
                  AND status = ?
                  AND COALESCE(chz_doc_id, '') = ''
                """
            ),
            (
                STATUS_PENDING,
                "восстановлено: submitted без chz_doc_id",
                now,
                user_id,
                source_id,
                STATUS_SUBMITTED,
            ),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


def repair_stale_submitted_events(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> int:
    """Sticky-close in-flight submitted docs stuck without a terminal CHZ status.

    Does not auto-resubmit (risk of double send). Operator reconciles in CHZ UI.
    """
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, int(STALE_SUBMITTED_DAYS)))
    ).isoformat()
    terminal = sorted(CHZ_STATUS_SUCCESS | CHZ_STATUS_FAILED)
    ph = ", ".join("?" for _ in terminal) if terminal else "NULL"
    with repo._connect() as conn:
        cur = conn.execute(
            repo._sql(
                f"""
                UPDATE wb_kiz_circulation_events
                SET status = ?,
                    skip_reason = ?,
                    error_text = ?,
                    updated_at = ?
                WHERE user_id = ? AND source_id = ?
                  AND status = ?
                  AND COALESCE(chz_doc_id, '') <> ''
                  AND updated_at < ?
                  AND UPPER(COALESCE(chz_status, '')) NOT IN ({ph})
                """
            ),
            (
                STATUS_SKIPPED,
                SKIP_STALE_SUBMITTED,
                (
                    f"завис submitted без финального статуса ЧЗ "
                    f">(>{STALE_SUBMITTED_DAYS}д) — сверьте документ в ЛК ЧЗ"
                ),
                now,
                user_id,
                source_id,
                STATUS_SUBMITTED,
                cutoff,
                *terminal,
            ),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


# Skips that must stay closed (dedupe / already sent / eligibility) — never
# auto-requeue via repair_legacy_skipped_with_cis. Eligibility skips may reopen
# when local FBS status catches up (sold / cancelled).
_ELIGIBILITY_SKIP_REASONS = frozenset(
    {SKIP_NOT_FBS, SKIP_NOT_SOLD, SKIP_NOT_RETURN}
)
_TERMINAL_SKIP_REASONS = frozenset(
    {
        "already_sent",
        "duplicate",
        "duplicate_nofiscal",
        SKIP_EMPTY_CIS,
        SKIP_BAD_CIS,
        SKIP_NO_PRODUCT_COST,
        SKIP_NO_PLACE,
        SKIP_STALE_SUBMITTED,
        *_ELIGIBILITY_SKIP_REASONS,
    }
)


def load_local_fbs_order_index(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> dict[str, dict[str, Any]]:
    """Map Marketplace FBS identity keys → order_id + statuses.

    Indexed keys (casefolded):
    - full ``rid``
    - rid mid-token (``orderUid``-like segment between dots)
    - ``prefix.mid`` stem (ignores trailing ``.0.0`` / ``.1.0`` unit suffix)
    - ``order_uid``
    - ``raw_json.rid`` variants

    Analytics ``srid`` often differs from Marketplace ``rid`` only by the trailing
    unit suffix (``.1.0`` vs ``.0.0``) and/or letter case.
    """
    from . import wb_fbs as wb_fbs_mod

    wb_fbs_mod.ensure_wb_fbs_tables(repo)
    out: dict[str, dict[str, Any]] = {}
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                """
                SELECT order_id, rid, order_uid, wb_status, supplier_status, raw_json
                FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ?
                """
            ),
            (user_id, source_id),
        ).fetchall()
    for r in rows:
        d = repo._row_to_dict(r)
        try:
            oid = int(d.get("order_id") or 0)
        except (TypeError, ValueError):
            oid = 0
        if oid <= 0:
            continue
        info = {
            "order_id": oid,
            "wb_status": str(d.get("wb_status") or "").strip().lower(),
            "supplier_status": str(d.get("supplier_status") or "").strip().lower(),
        }
        for key in _rid_match_keys(d.get("rid")):
            out.setdefault(key, info)
        for key in _rid_match_keys(d.get("order_uid")):
            out.setdefault(key, info)
        try:
            raw = d.get("raw_json") or "{}"
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            if isinstance(payload, dict):
                for key in _rid_match_keys(payload.get("rid")):
                    out.setdefault(key, info)
                for key in _rid_match_keys(payload.get("orderUid") or payload.get("order_uid")):
                    out.setdefault(key, info)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return out


def load_local_fbs_rid_keys(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> set[str]:
    """All Marketplace FBS ``rid`` keys known locally for the source."""
    return set(
        load_local_fbs_order_index(repo, user_id=user_id, source_id=source_id).keys()
    )


def _rid_fold(value: object) -> str:
    """Casefold rid/srid for joins (WB mixes ``ebX`` / ``ebx``)."""
    return str(value or "").strip().casefold()


def _rid_mid_token(value: object) -> str:
    """Middle dotted segment (= Marketplace ``orderUid`` shape).

    ``eI.i0a39f75….1.0`` → ``i0a39f75…``
    """
    text = str(value or "").strip()
    if not text:
        return ""
    bits = text.split(".")
    if len(bits) >= 2 and bits[1].strip():
        return bits[1].strip().casefold()
    return ""


def _rid_stem(value: object) -> str:
    """``prefix.mid`` without trailing unit counters (``.0.0`` / ``.1.0``)."""
    text = str(value or "").strip()
    if not text:
        return ""
    bits = text.split(".")
    if len(bits) >= 2 and bits[0].strip() and bits[1].strip():
        return f"{bits[0].strip()}.{bits[1].strip()}".casefold()
    return _rid_fold(text)


def _rid_match_keys(value: object) -> list[str]:
    """All join keys derived from a rid/srid/orderUid value."""
    keys: list[str] = []
    full = _rid_fold(value)
    if full:
        keys.append(full)
    stem = _rid_stem(value)
    if stem and stem not in keys:
        keys.append(stem)
    mid = _rid_mid_token(value)
    if mid and mid not in keys:
        keys.append(mid)
    return keys


def _lookup_fbs_order(
    norm: dict[str, Any], index: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    if not index:
        return None
    for raw in (norm.get("srid"), norm.get("rid")):
        for key in _rid_match_keys(raw):
            hit = index.get(key)
            if hit:
                return hit
    return None


def _norm_matches_fbs(norm: dict[str, Any], fbs_keys: set[str]) -> bool:
    if not fbs_keys:
        return False
    folded: set[str] = set()
    for k in fbs_keys:
        folded.update(_rid_match_keys(k))
    for raw in (norm.get("srid"), norm.get("rid")):
        for key in _rid_match_keys(raw):
            if key in folded:
                return True
    return False


def _norm_eligibility_skip(
    norm: dict[str, Any], index: dict[str, dict[str, Any]]
) -> str:
    """Empty if row may enter Вывод КИЗ; else ASCII skip code.

    - withdraw (1): FBS + wbStatus=sold → вывод из оборота
    - return (2): FBS match → ввод в оборот (excise op=2 = возврат/отказ на ПВЗ /
      найдены дефекты; not pre-delivery cancel)
    """
    info = _lookup_fbs_order(norm, index)
    if not info:
        return SKIP_NOT_FBS
    op = int(norm.get("operation_type") or 0)
    ws = str(info.get("wb_status") or "").strip().lower()
    if op == OP_WITHDRAW:
        if ws == "sold":
            return ""
        return SKIP_NOT_SOLD
    if op == OP_RETURN:
        # Trust Analytics return rows linked to an FBS assembly order.
        return ""
    return SKIP_NOT_FBS


def build_excise_fbs_match_index(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    norms: list[dict[str, Any]],
    marketplace_api_key: str = "",
    log: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Resolve analytics srid/rid → local FBS order + status (hydrate archive if needed).

    Returns ``(index, meta)`` where meta has hydrate_ok / sold_orders / order_count.
    """
    from . import wb_fbs as wb_fbs_mod

    keys: list[str] = []
    for norm in norms:
        srid = str(norm.get("srid") or "").strip()
        rid = str(norm.get("rid") or "").strip()
        if srid:
            keys.append(srid)
        if rid and rid != srid:
            keys.append(rid)
    uniq = sorted({k for k in keys if k})
    if log is not None:
        _append_log(log, f"уникальных srid/rid в отчёте: {len(uniq)}")

    mkey = str(marketplace_api_key or "").strip()
    hydrate_ok = True
    if mkey and uniq:
        try:
            hyd = wb_fbs_mod.hydrate_orders_for_kiz_srids(
                repo,
                user_id=user_id,
                source_id=source_id,
                srids=uniq,
                api_key=mkey,
                lookback_days=KIZ_HYDRATE_LOOKBACK_MAX_DAYS,
                archive_pages=40,
            )
            if log is not None:
                _append_log(
                    log,
                    "hydrate Marketplace FBS: "
                    f"wanted={hyd.get('wanted')}, "
                    f"было={hyd.get('found_before')}, "
                    f"стало={hyd.get('found_after')}, "
                    f"скачано={hyd.get('fetched')}, "
                    f"без rid={hyd.get('still_missing')}",
                )
        except Exception as exc:
            hydrate_ok = False
            logger.warning("hydrate for kiz sync failed: %s", exc)
            if log is not None:
                _append_log(log, f"hydrate Marketplace не удался: {exc}")
    elif not mkey:
        hydrate_ok = False
        if log is not None:
            _append_log(
                log,
                "нет Marketplace FBS токена — hydrate архива пропущен, "
                "сопоставление только по уже сохранённым rid",
            )

    by_key: dict[str, int] = {}
    chunk = 500
    for i in range(0, len(uniq), chunk):
        part = uniq[i : i + chunk]
        got = wb_fbs_mod.order_ids_by_srids(
            repo, user_id=user_id, source_id=source_id, srids=part
        )
        by_key.update(got)

    order_ids = sorted({int(v) for v in by_key.values() if int(v or 0) > 0})
    status_map = wb_fbs_mod.load_order_status_map(
        repo, user_id=user_id, source_id=source_id, order_ids=order_ids
    )
    out: dict[str, dict[str, Any]] = {}
    for key, oid in by_key.items():
        try:
            oid_i = int(oid)
        except (TypeError, ValueError):
            continue
        if oid_i <= 0:
            continue
        st = status_map.get(oid_i) or {}
        info = {
            "order_id": oid_i,
            "wb_status": str(st.get("wb_status") or "").strip().lower(),
            "supplier_status": str(st.get("supplier_status") or "").strip().lower(),
        }
        folded = _rid_fold(key)
        if folded:
            out[folded] = info
        # Keep original casing too (defensive for callers not using _lookup).
        raw = str(key or "").strip()
        if raw:
            out[raw] = info
        for mk in _rid_match_keys(key):
            out[mk] = info
    sold_orders = {
        int(v["order_id"])
        for v in out.values()
        if v.get("wb_status") == "sold" and int(v.get("order_id") or 0) > 0
    }
    meta = {
        "hydrate_ok": hydrate_ok,
        "order_count": len(order_ids),
        "sold_orders": len(sold_orders),
        "key_count": len({_rid_fold(k) for k in by_key if _rid_fold(k)}),
        "report_keys": len(uniq),
    }
    if log is not None:
        _append_log(
            log,
            f"сопоставлено с FBS по rid: ключей={meta['key_count']}, "
            f"уникальных заказов={len(order_ids)}, sold={len(sold_orders)}"
            + ("" if hydrate_ok else " (hydrate был с ошибкой/пропущен)"),
        )
        if uniq and not order_ids:
            dotted = [k for k in uniq if "." in k]
            sample_rep = ", ".join((dotted or uniq)[:3])
            local = load_local_fbs_order_index(
                repo, user_id=user_id, source_id=source_id
            )
            sample_loc = ", ".join(
                [k for k in local.keys() if "." in k][:3] or list(local.keys())[:3]
            ) or "—"
            fold_rep: set[str] = set()
            for k in uniq:
                fold_rep.update(_rid_match_keys(k))
            overlap = len(fold_rep & set(local.keys()))
            _append_log(
                log,
                f"диагностика: пример srid из отчёта [{sample_rep}]; "
                f"пример local rid [{sample_loc}]; "
                f"локальных ключей={len(local)}; "
                f"пересечение (full/mid/stem)={overlap}",
            )
    return out, meta


def _fbs_order_join_sql(alias_e: str = "e", alias_o: str = "o") -> str:
    # Analytics srid vs Marketplace rid: case may differ; trailing .N.M unit
    # suffix often differs (.1.0 vs .0.0). Join on full / mid / order_uid.
    return f"""
        {alias_o}.user_id = {alias_e}.user_id
        AND {alias_o}.source_id = {alias_e}.source_id
        AND (
          (
            COALESCE({alias_e}.srid, '') <> ''
            AND (
              LOWER({alias_o}.rid) = LOWER({alias_e}.srid)
              OR LOWER({alias_o}.order_uid) = LOWER({alias_e}.srid)
              OR (
                POSITION('.' IN {alias_e}.srid) > 0
                AND LOWER(SPLIT_PART({alias_o}.rid, '.', 2))
                    = LOWER(SPLIT_PART({alias_e}.srid, '.', 2))
                AND SPLIT_PART({alias_e}.srid, '.', 2) <> ''
              )
              OR (
                POSITION('.' IN {alias_e}.srid) > 0
                AND LOWER({alias_o}.order_uid)
                    = LOWER(SPLIT_PART({alias_e}.srid, '.', 2))
                AND SPLIT_PART({alias_e}.srid, '.', 2) <> ''
              )
              OR (
                {alias_o}.raw_json IS NOT NULL
                AND {alias_o}.raw_json <> ''
                AND {alias_o}.raw_json <> '{{}}'
                AND LOWER({alias_o}.raw_json::jsonb->>'rid') = LOWER({alias_e}.srid)
              )
            )
          )
          OR (
            COALESCE({alias_e}.rid, '') <> ''
            AND (
              LOWER({alias_o}.rid) = LOWER({alias_e}.rid)
              OR LOWER({alias_o}.order_uid) = LOWER({alias_e}.rid)
              OR (
                POSITION('.' IN {alias_e}.rid) > 0
                AND LOWER(SPLIT_PART({alias_o}.rid, '.', 2))
                    = LOWER(SPLIT_PART({alias_e}.rid, '.', 2))
                AND SPLIT_PART({alias_e}.rid, '.', 2) <> ''
              )
              OR (
                POSITION('.' IN {alias_e}.rid) > 0
                AND LOWER({alias_o}.order_uid)
                    = LOWER(SPLIT_PART({alias_e}.rid, '.', 2))
                AND SPLIT_PART({alias_e}.rid, '.', 2) <> ''
              )
              OR (
                {alias_o}.raw_json IS NOT NULL
                AND {alias_o}.raw_json <> ''
                AND {alias_o}.raw_json <> '{{}}'
                AND LOWER({alias_o}.raw_json::jsonb->>'rid') = LOWER({alias_e}.rid)
              )
            )
          )
        )
    """


def repair_skip_non_fbs_events(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> int:
    """Mark open queue rows without a local FBS order as skipped ``not_fbs``.

    No-op when the source has zero FBS orders (avoid wiping the queue by mistake).
    """
    ensure_kiz_circulation_tables(repo)
    from . import wb_fbs as wb_fbs_mod

    wb_fbs_mod.ensure_wb_fbs_tables(repo)
    fbs_n = 0
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql(
                """
                SELECT COUNT(*) AS n FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ?
                """
            ),
            (user_id, source_id),
        ).fetchone()
        if row:
            fbs_n = int(repo._row_to_dict(row).get("n") or 0)
    if fbs_n <= 0:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    join_sql = _fbs_order_join_sql()
    with repo._connect() as conn:
        cur = conn.execute(
            repo._sql(
                f"""
                UPDATE wb_kiz_circulation_events AS e
                SET status = ?,
                    skip_reason = ?,
                    error_text = '',
                    updated_at = ?
                WHERE e.user_id = ? AND e.source_id = ?
                  AND e.status IN (?, ?, ?)
                  AND NOT EXISTS (
                    SELECT 1 FROM wb_fbs_orders AS o
                    WHERE {join_sql}
                  )
                """
            ),
            (
                STATUS_SKIPPED,
                SKIP_NOT_FBS,
                now,
                user_id,
                source_id,
                STATUS_PENDING,
                STATUS_READY,
                STATUS_ERROR,
            ),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


def purge_non_fbs_circulation_events(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    run_id: int | None = None,
    on_batch: Callable[[int, int], None] | None = None,
) -> int:
    """Hard-delete confirmed non-FBS (e.g. FBO) skips from the circulation table.

    Only removes rows already marked ``skipped`` + ``not_fbs`` that still have
    no local FBS match and are older than ``NOT_FBS_PURGE_MIN_AGE_DAYS``.
    Never deletes open ``pending`` / ``ready`` / ``error`` via a bare
    ``NOT EXISTS`` — an incomplete Marketplace sync must not wipe the queue.
    Age gate preserves freshly persisted eligibility skips so
    ``repair_requeue_*`` can reopen them after a late FBS hydrate.
    Keeps ``submitted`` / ``accepted`` (CHZ audit). Requires at least one local
    FBS order so an empty Marketplace table cannot wipe anything.
    """
    ensure_kiz_circulation_tables(repo)
    from . import wb_fbs as wb_fbs_mod

    wb_fbs_mod.ensure_wb_fbs_tables(repo)
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql(
                """
                SELECT COUNT(*) AS n FROM wb_fbs_orders
                WHERE user_id = ? AND source_id = ?
                """
            ),
            (user_id, source_id),
        ).fetchone()
        fbs_n = int(repo._row_to_dict(row).get("n") or 0) if row else 0
    if fbs_n <= 0:
        return 0
    join_sql = _fbs_order_join_sql()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, int(NOT_FBS_PURGE_MIN_AGE_DAYS)))
    ).isoformat()
    deleted = 0
    for _ in range(100):
        _check_sync_cancelled(run_id)
        with repo._connect() as conn:
            cur = conn.execute(
                repo._sql(
                    f"""
                    DELETE FROM wb_kiz_circulation_events
                    WHERE id IN (
                      SELECT e.id FROM wb_kiz_circulation_events AS e
                      WHERE e.user_id = ? AND e.source_id = ?
                        AND e.status = ?
                        AND e.skip_reason = ?
                        AND e.updated_at < ?
                        AND NOT EXISTS (
                          SELECT 1 FROM wb_fbs_orders AS o
                          WHERE {join_sql}
                        )
                      ORDER BY e.id ASC
                      LIMIT ?
                    )
                    """
                ),
                (
                    user_id,
                    source_id,
                    STATUS_SKIPPED,
                    SKIP_NOT_FBS,
                    cutoff,
                    PURGE_BATCH_SIZE,
                ),
            )
            n = int(getattr(cur, "rowcount", 0) or 0)
        deleted += n
        if on_batch is not None and n:
            try:
                on_batch(n, deleted)
            except Exception:
                logger.exception("purge_non_fbs on_batch failed")
        if n < PURGE_BATCH_SIZE:
            break
    return deleted


def repair_skip_wrong_fbs_status_events(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> dict[str, int]:
    """Skip open withdraw rows whose FBS order is not sold.

    Returns (op=2) are not gated on cancel: Analytics return rows are PVZ/returns.
    """
    ensure_kiz_circulation_tables(repo)
    from . import wb_fbs as wb_fbs_mod

    wb_fbs_mod.ensure_wb_fbs_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    join_sql = _fbs_order_join_sql()
    with repo._connect() as conn:
        cur_w = conn.execute(
            repo._sql(
                f"""
                UPDATE wb_kiz_circulation_events AS e
                SET status = ?,
                    skip_reason = ?,
                    error_text = '',
                    updated_at = ?
                WHERE e.user_id = ? AND e.source_id = ?
                  AND e.operation_type = ?
                  AND e.status IN (?, ?, ?)
                  AND EXISTS (
                    SELECT 1 FROM wb_fbs_orders AS o
                    WHERE {join_sql}
                      AND LOWER(COALESCE(o.wb_status, '')) <> 'sold'
                  )
                """
            ),
            (
                STATUS_SKIPPED,
                SKIP_NOT_SOLD,
                now,
                user_id,
                source_id,
                OP_WITHDRAW,
                STATUS_PENDING,
                STATUS_READY,
                STATUS_ERROR,
            ),
        )
        not_sold = int(getattr(cur_w, "rowcount", 0) or 0)
        # Re-open legacy not_return only for Marketplace отказ/дефект.
        cur_r = conn.execute(
            repo._sql(
                f"""
                UPDATE wb_kiz_circulation_events AS e
                SET status = ?,
                    skip_reason = '',
                    error_text = '',
                    updated_at = ?
                WHERE e.user_id = ? AND e.source_id = ?
                  AND e.operation_type = ?
                  AND e.status = ?
                  AND e.skip_reason = ?
                  AND EXISTS (
                    SELECT 1 FROM wb_fbs_orders AS o
                    WHERE {join_sql}
                      AND LOWER(COALESCE(o.wb_status, '')) IN (?, ?)
                  )
                """
            ),
            (
                STATUS_PENDING,
                now,
                user_id,
                source_id,
                OP_RETURN,
                STATUS_SKIPPED,
                SKIP_NOT_RETURN,
                "canceled_by_client",
                "defect",
            ),
        )
        return_reopened = int(getattr(cur_r, "rowcount", 0) or 0)
    return {
        "not_sold_skipped": not_sold,
        "not_return_skipped": 0,
        "not_return_reopened": return_reopened,
        "skipped": not_sold,
    }


def repair_requeue_fbs_matched_not_fbs(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> int:
    """Re-open eligibility skips when FBS order is now sold / cancelled as required."""
    return int(
        repair_requeue_eligible_fbs_events(
            repo, user_id=user_id, source_id=source_id
        ).get("requeued")
        or 0
    )


def repair_requeue_eligible_fbs_events(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> dict[str, int]:
    """Re-open eligibility skips when FBS order becomes eligible.

    Withdraw: sold. Return: any linked FBS order (Analytics op=2 = PVZ/return).
    """
    ensure_kiz_circulation_tables(repo)
    from . import wb_fbs as wb_fbs_mod

    wb_fbs_mod.ensure_wb_fbs_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    join_sql = _fbs_order_join_sql()
    with repo._connect() as conn:
        cur_w = conn.execute(
            repo._sql(
                f"""
                UPDATE wb_kiz_circulation_events AS e
                SET status = ?,
                    skip_reason = CASE
                      WHEN COALESCE(e.fiscal_doc_number, '') = ''
                        AND COALESCE(e.fiscal_dt, '') = ''
                      THEN ?
                      ELSE ''
                    END,
                    error_text = '',
                    updated_at = ?
                WHERE e.user_id = ? AND e.source_id = ?
                  AND e.operation_type = ?
                  AND e.status = ?
                  AND e.skip_reason IN (?, ?)
                  AND EXISTS (
                    SELECT 1 FROM wb_fbs_orders AS o
                    WHERE {join_sql}
                      AND LOWER(COALESCE(o.wb_status, '')) = 'sold'
                  )
                """
            ),
            (
                STATUS_PENDING,
                SKIP_NO_FISCAL,
                now,
                user_id,
                source_id,
                OP_WITHDRAW,
                STATUS_SKIPPED,
                SKIP_NOT_FBS,
                SKIP_NOT_SOLD,
            ),
        )
        withdraw_n = int(getattr(cur_w, "rowcount", 0) or 0)
        # Analytics op=2 + any FBS link → reopen not_fbs (PVZ/return trust).
        cur_r_fbs = conn.execute(
            repo._sql(
                f"""
                UPDATE wb_kiz_circulation_events AS e
                SET status = ?,
                    skip_reason = '',
                    error_text = '',
                    updated_at = ?
                WHERE e.user_id = ? AND e.source_id = ?
                  AND e.operation_type = ?
                  AND e.status = ?
                  AND e.skip_reason = ?
                  AND EXISTS (
                    SELECT 1 FROM wb_fbs_orders AS o
                    WHERE {join_sql}
                  )
                """
            ),
            (
                STATUS_PENDING,
                now,
                user_id,
                source_id,
                OP_RETURN,
                STATUS_SKIPPED,
                SKIP_NOT_FBS,
            ),
        )
        # Legacy not_return only when Marketplace status is отказ/дефект.
        cur_r_status = conn.execute(
            repo._sql(
                f"""
                UPDATE wb_kiz_circulation_events AS e
                SET status = ?,
                    skip_reason = '',
                    error_text = '',
                    updated_at = ?
                WHERE e.user_id = ? AND e.source_id = ?
                  AND e.operation_type = ?
                  AND e.status = ?
                  AND e.skip_reason = ?
                  AND EXISTS (
                    SELECT 1 FROM wb_fbs_orders AS o
                    WHERE {join_sql}
                      AND LOWER(COALESCE(o.wb_status, '')) IN (?, ?)
                  )
                """
            ),
            (
                STATUS_PENDING,
                now,
                user_id,
                source_id,
                OP_RETURN,
                STATUS_SKIPPED,
                SKIP_NOT_RETURN,
                "canceled_by_client",
                "defect",
            ),
        )
        return_n = int(getattr(cur_r_fbs, "rowcount", 0) or 0) + int(
            getattr(cur_r_status, "rowcount", 0) or 0
        )
    return {
        "withdraw_requeued": withdraw_n,
        "return_requeued": return_n,
        "requeued": withdraw_n + return_n,
    }


def repair_requeue_skipped_with_product_cost(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> int:
    """Re-open prepare sticky no-price skips after price was backfilled."""
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    with repo._connect() as conn:
        cur = conn.execute(
            repo._sql(
                """
                UPDATE wb_kiz_circulation_events
                SET status = ?,
                    skip_reason = CASE
                      WHEN operation_type = 1
                        AND COALESCE(fiscal_doc_number, '') = ''
                        AND COALESCE(fiscal_dt, '') = ''
                      THEN ?
                      ELSE ''
                    END,
                    error_text = '',
                    updated_at = ?
                WHERE user_id = ? AND source_id = ?
                  AND status = ?
                  AND skip_reason = ?
                  AND price IS NOT NULL
                """
            ),
            (
                STATUS_PENDING,
                SKIP_NO_FISCAL,
                now,
                user_id,
                source_id,
                STATUS_SKIPPED,
                SKIP_NO_PRODUCT_COST,
            ),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


def repair_legacy_skipped_with_cis(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> int:
    """Re-queue legacy skipped rows that still have a CIS (do not lose codes).

    Does not reopen terminal dedupe skips (already_sent / duplicate*).
    """
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    terminal = sorted(_TERMINAL_SKIP_REASONS)
    ph = ", ".join("?" for _ in terminal)
    with repo._connect() as conn:
        cur = conn.execute(
            repo._sql(
                f"""
                UPDATE wb_kiz_circulation_events
                SET status = ?,
                    skip_reason = CASE
                      WHEN operation_type = 1
                        AND COALESCE(fiscal_doc_number, '') = ''
                        AND COALESCE(fiscal_dt, '') = ''
                      THEN ?
                      ELSE ''
                    END,
                    updated_at = ?
                WHERE user_id = ? AND source_id = ?
                  AND status = ?
                  AND COALESCE(excise_short, '') <> ''
                  AND COALESCE(skip_reason, '') NOT IN ({ph})
                """
            ),
            (
                STATUS_PENDING,
                SKIP_NO_FISCAL,
                now,
                user_id,
                source_id,
                STATUS_SKIPPED,
                *terminal,
            ),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


def _retention_cutoff_iso(*, days: int = EVENT_RETENTION_DAYS) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()


def _cis_anchor(*, srid: str = "", rid: str = "") -> str:
    return str(srid or "").strip() or str(rid or "").strip()


def upsert_sent_cis_rows(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    rows: list[dict[str, Any]],
    accepted_at: str = "",
) -> int:
    """Upsert compact forever registry rows (anti-dupe after event purge)."""
    if not rows:
        return 0
    ensure_kiz_circulation_tables(repo)
    now = accepted_at or datetime.now(timezone.utc).isoformat()
    written = 0
    with repo._connect() as conn:
        for row in rows:
            cis = str(row.get("excise_short") or "").strip()
            op = int(row.get("operation_type") or 0)
            if not cis or op not in {OP_WITHDRAW, OP_RETURN}:
                continue
            anchors = _cis_identity_keys(
                srid=str(row.get("srid") or ""),
                rid=str(row.get("rid") or ""),
                excise_short=cis,
                operation_type=op,
            )
            # Persist every fold key so later Analytics/Marketplace variants match.
            anchor_values = sorted({a for a, _, _ in anchors if a}) or [
                _cis_anchor(
                    srid=str(row.get("srid") or ""),
                    rid=str(row.get("rid") or ""),
                )
            ]
            for anchor in anchor_values:
                conn.execute(
                    repo._sql(
                        """
                        INSERT INTO wb_kiz_sent_cis (
                            user_id, source_id, operation_type, excise_short, anchor,
                            chz_doc_id, event_key, fiscal_doc_number, fiscal_dt, accepted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (user_id, source_id, operation_type, excise_short, anchor)
                        DO UPDATE SET
                            chz_doc_id = CASE
                                WHEN EXCLUDED.chz_doc_id <> '' THEN EXCLUDED.chz_doc_id
                                ELSE wb_kiz_sent_cis.chz_doc_id
                            END,
                            event_key = CASE
                                WHEN EXCLUDED.event_key <> '' THEN EXCLUDED.event_key
                                ELSE wb_kiz_sent_cis.event_key
                            END,
                            fiscal_doc_number = CASE
                                WHEN EXCLUDED.fiscal_doc_number <> '' THEN EXCLUDED.fiscal_doc_number
                                ELSE wb_kiz_sent_cis.fiscal_doc_number
                            END,
                            fiscal_dt = CASE
                                WHEN EXCLUDED.fiscal_dt <> '' THEN EXCLUDED.fiscal_dt
                                ELSE wb_kiz_sent_cis.fiscal_dt
                            END,
                            accepted_at = CASE
                                WHEN wb_kiz_sent_cis.accepted_at = ''
                                  OR EXCLUDED.accepted_at > wb_kiz_sent_cis.accepted_at
                                THEN EXCLUDED.accepted_at
                                ELSE wb_kiz_sent_cis.accepted_at
                            END
                        """
                    ),
                    (
                        user_id,
                        source_id,
                        op,
                        cis,
                        anchor,
                        str(row.get("chz_doc_id") or "").strip(),
                        str(row.get("event_key") or "").strip(),
                        str(row.get("fiscal_doc_number") or "").strip(),
                        str(row.get("fiscal_dt") or "").strip(),
                        now,
                    ),
                )
                written += 1
    return written


def register_sent_cis_for_event_keys(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    event_keys: list[str],
    accepted_at: str = "",
) -> int:
    keys = [str(k).strip() for k in event_keys if str(k).strip()]
    if not keys:
        return 0
    ensure_kiz_circulation_tables(repo)
    rows: list[dict[str, Any]] = []
    with repo._connect() as conn:
        for chunk in _chunked(keys, 200):
            ph = ", ".join("?" for _ in chunk)
            found = conn.execute(
                repo._sql(
                    f"""
                    SELECT srid, rid, excise_short, operation_type, chz_doc_id,
                           event_key, fiscal_doc_number, fiscal_dt
                    FROM wb_kiz_circulation_events
                    WHERE user_id = ? AND source_id = ?
                      AND event_key IN ({ph})
                    """
                ),
                (user_id, source_id, *chunk),
            ).fetchall()
            rows.extend(repo._row_to_dict(r) for r in found)
    return upsert_sent_cis_rows(
        repo,
        user_id=user_id,
        source_id=source_id,
        rows=rows,
        accepted_at=accepted_at,
    )


def clear_accepted_raw_json(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> int:
    """Drop bulky WB payload once CHZ accepted — registry keeps the trail."""
    ensure_kiz_circulation_tables(repo)
    cleared = 0
    for _ in range(20):
        with repo._connect() as conn:
            cur = conn.execute(
                repo._sql(
                    """
                    UPDATE wb_kiz_circulation_events
                    SET raw_json = '{}'
                    WHERE id IN (
                      SELECT id FROM wb_kiz_circulation_events
                      WHERE user_id = ? AND source_id = ?
                        AND status = ?
                        AND COALESCE(raw_json, '') <> ''
                        AND raw_json <> '{}'
                      ORDER BY id ASC
                      LIMIT ?
                    )
                    """
                ),
                (user_id, source_id, STATUS_ACCEPTED, PURGE_BATCH_SIZE),
            )
            n = int(getattr(cur, "rowcount", 0) or 0)
        cleared += n
        if n < PURGE_BATCH_SIZE:
            break
    return cleared


def _mark_storage_maintained(
    repo: ReviewRepository, *, user_id: int, source_id: int, when: str = ""
) -> None:
    ensure_kiz_circulation_tables(repo)
    now = when or datetime.now(timezone.utc).isoformat()
    with repo._connect() as conn:
        conn.execute(
            repo._sql(
                """
                INSERT INTO wb_kiz_circulation_cursor (
                    user_id, source_id, last_storage_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (user_id, source_id) DO UPDATE SET
                    last_storage_at = EXCLUDED.last_storage_at,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            (user_id, source_id, now, now),
        )


def maintain_kiz_circulation_storage(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    force: bool = False,
    min_interval_hours: int = STORAGE_MAINTAIN_MIN_INTERVAL_HOURS,
) -> dict[str, int]:
    """Clear bulky payloads + purge old terminal history; keep slim sent registry.

    Throttled by default so CHZ prepare multi-round loops do not re-scan the table.
    """
    empty = {
        "raw_json_cleared": 0,
        "events_purged": 0,
        "runs_purged": 0,
        "docs_purged": 0,
        "skipped": 0,
    }
    if not force and min_interval_hours > 0:
        try:
            cur = get_cursor(repo, user_id=user_id, source_id=source_id)
            last = str(cur.get("last_storage_at") or "").strip()
            if last:
                threshold = (
                    datetime.now(timezone.utc)
                    - timedelta(hours=max(1, int(min_interval_hours)))
                ).isoformat()
                if last >= threshold:
                    empty["skipped"] = 1
                    return empty
        except Exception as exc:
            logger.exception("storage maintain throttle check failed: %s", exc)

    cleared = 0
    purged_events = 0
    meta = {"runs": 0, "docs": 0}
    try:
        cleared = clear_accepted_raw_json(repo, user_id=user_id, source_id=source_id)
    except Exception as exc:
        logger.exception("clear_accepted_raw_json failed: %s", exc)
    try:
        purged_events = purge_old_kiz_circulation_events(
            repo, user_id=user_id, source_id=source_id
        )
    except Exception as exc:
        logger.exception("purge_old_kiz_circulation_events failed: %s", exc)
    try:
        meta = purge_old_kiz_runs_and_docs(repo, user_id=user_id, source_id=source_id)
    except Exception as exc:
        logger.exception("purge_old_kiz_runs_and_docs failed: %s", exc)
    try:
        _mark_storage_maintained(repo, user_id=user_id, source_id=source_id)
    except Exception as exc:
        logger.exception("mark storage maintained failed: %s", exc)
    return {
        "raw_json_cleared": cleared,
        "events_purged": purged_events,
        "runs_purged": int(meta.get("runs") or 0),
        "docs_purged": int(meta.get("docs") or 0),
        "skipped": 0,
    }


def repair_circulation_queue(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    run_id: int | None = None,
    on_log: Callable[[str], None] | None = None,
) -> dict[str, int]:
    def _log(msg: str) -> None:
        if on_log is not None:
            try:
                on_log(msg)
            except Exception:
                logger.exception("repair_circulation_queue on_log failed")

    try:
        _check_sync_cancelled(run_id)
        returns_fixed = repair_stuck_return_events(
            repo, user_id=user_id, source_id=source_id
        )
    except SyncCancelled:
        raise
    except Exception as exc:
        logger.exception("repair_stuck_return_events failed: %s", exc)
        returns_fixed = 0
    try:
        _check_sync_cancelled(run_id)
        withdraw_from_error = repair_unhealable_withdraw_errors(
            repo, user_id=user_id, source_id=source_id
        )
    except SyncCancelled:
        raise
    except Exception as exc:
        logger.exception("repair_unhealable_withdraw_errors failed: %s", exc)
        withdraw_from_error = 0
    try:
        _check_sync_cancelled(run_id)
        withdraw_requeued = repair_nofiscal_withdraw_to_pending(
            repo, user_id=user_id, source_id=source_id
        )
    except SyncCancelled:
        raise
    except Exception as exc:
        logger.exception("repair_nofiscal_withdraw_to_pending failed: %s", exc)
        withdraw_requeued = 0
    try:
        _check_sync_cancelled(run_id)
        orphan_submitted = repair_orphan_submitted_events(
            repo, user_id=user_id, source_id=source_id
        )
    except SyncCancelled:
        raise
    except Exception as exc:
        logger.exception("repair_orphan_submitted_events failed: %s", exc)
        orphan_submitted = 0
    try:
        _check_sync_cancelled(run_id)
        stale_submitted = repair_stale_submitted_events(
            repo, user_id=user_id, source_id=source_id
        )
    except SyncCancelled:
        raise
    except Exception as exc:
        logger.exception("repair_stale_submitted_events failed: %s", exc)
        stale_submitted = 0
    try:
        _check_sync_cancelled(run_id)
        legacy_skipped = repair_legacy_skipped_with_cis(
            repo, user_id=user_id, source_id=source_id
        )
    except SyncCancelled:
        raise
    except Exception as exc:
        logger.exception("repair_legacy_skipped_with_cis failed: %s", exc)
        legacy_skipped = 0
    try:
        _check_sync_cancelled(run_id)
        fbs_requeued = repair_requeue_fbs_matched_not_fbs(
            repo, user_id=user_id, source_id=source_id
        )
    except SyncCancelled:
        raise
    except Exception as exc:
        logger.exception("repair_requeue_fbs_matched_not_fbs failed: %s", exc)
        fbs_requeued = 0
    try:
        _check_sync_cancelled(run_id)
        price_requeued = repair_requeue_skipped_with_product_cost(
            repo, user_id=user_id, source_id=source_id
        )
    except SyncCancelled:
        raise
    except Exception as exc:
        logger.exception("repair_requeue_skipped_with_product_cost failed: %s", exc)
        price_requeued = 0
    try:
        # After legacy requeue — drop open rows that are not Marketplace FBS.
        _check_sync_cancelled(run_id)
        not_fbs_skipped = repair_skip_non_fbs_events(
            repo, user_id=user_id, source_id=source_id
        )
    except SyncCancelled:
        raise
    except Exception as exc:
        logger.exception("repair_skip_non_fbs_events failed: %s", exc)
        not_fbs_skipped = 0
    try:
        _check_sync_cancelled(run_id)
        _log("очистка не-FBS (FBO) из таблицы…")

        def _purge_progress(batch_n: int, total_n: int) -> None:
            _check_sync_cancelled(run_id)
            _log(f"удалено не-FBS: +{batch_n} (всего {total_n})")

        not_fbs_purged = purge_non_fbs_circulation_events(
            repo,
            user_id=user_id,
            source_id=source_id,
            run_id=run_id,
            on_batch=_purge_progress,
        )
    except SyncCancelled:
        raise
    except Exception as exc:
        logger.exception("purge_non_fbs_circulation_events failed: %s", exc)
        not_fbs_purged = 0
    try:
        _check_sync_cancelled(run_id)
        status_skip = repair_skip_wrong_fbs_status_events(
            repo, user_id=user_id, source_id=source_id
        )
    except SyncCancelled:
        raise
    except Exception as exc:
        logger.exception("repair_skip_wrong_fbs_status_events failed: %s", exc)
        status_skip = {"not_sold_skipped": 0, "not_return_skipped": 0, "skipped": 0}
    return {
        "returns_fixed": returns_fixed,
        "withdraw_skipped": withdraw_from_error,
        "withdraw_requeued": withdraw_requeued,
        "orphan_submitted": orphan_submitted,
        "stale_submitted": stale_submitted,
        "legacy_skipped": legacy_skipped,
        "fbs_requeued": fbs_requeued,
        "price_requeued": price_requeued,
        "not_fbs_skipped": not_fbs_skipped,
        "not_fbs_purged": not_fbs_purged,
        "not_sold_skipped": int(status_skip.get("not_sold_skipped") or 0),
        "not_return_skipped": int(status_skip.get("not_return_skipped") or 0),
    }


def purge_old_kiz_circulation_events(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    retention_days: int = EVENT_RETENTION_DAYS,
) -> int:
    """Delete terminal event rows older than retention; register accepted first.

    Never touches pending/ready/error/submitted (open queue / in-flight CHZ).
    """
    ensure_kiz_circulation_tables(repo)
    cutoff = _retention_cutoff_iso(days=retention_days)
    terminal = sorted(_TERMINAL_SKIP_REASONS)
    skip_ph = ", ".join("?" for _ in terminal)
    deleted = 0
    for _ in range(50):
        with repo._connect() as conn:
            accepted = conn.execute(
                repo._sql(
                    f"""
                    SELECT id, srid, rid, excise_short, operation_type, chz_doc_id,
                           event_key, fiscal_doc_number, fiscal_dt
                    FROM wb_kiz_circulation_events
                    WHERE user_id = ? AND source_id = ?
                      AND status = ?
                      AND updated_at < ?
                    ORDER BY id ASC
                    LIMIT ?
                    """
                ),
                (
                    user_id,
                    source_id,
                    STATUS_ACCEPTED,
                    cutoff,
                    PURGE_BATCH_SIZE,
                ),
            ).fetchall()
            accepted_rows = [repo._row_to_dict(r) for r in accepted]
        if accepted_rows:
            upsert_sent_cis_rows(
                repo,
                user_id=user_id,
                source_id=source_id,
                rows=accepted_rows,
            )
            ids = [int(r["id"]) for r in accepted_rows if int(r.get("id") or 0) > 0]
            with repo._connect() as conn:
                for chunk in _chunked(ids, 200):
                    ph = ", ".join("?" for _ in chunk)
                    cur = conn.execute(
                        repo._sql(
                            f"""
                            DELETE FROM wb_kiz_circulation_events
                            WHERE user_id = ? AND source_id = ?
                              AND id IN ({ph})
                            """
                        ),
                        (user_id, source_id, *chunk),
                    )
                    deleted += int(getattr(cur, "rowcount", 0) or 0)

        with repo._connect() as conn:
            cur = conn.execute(
                repo._sql(
                    f"""
                    DELETE FROM wb_kiz_circulation_events
                    WHERE id IN (
                      SELECT id FROM wb_kiz_circulation_events
                      WHERE user_id = ? AND source_id = ?
                        AND status = ?
                        AND COALESCE(skip_reason, '') IN ({skip_ph})
                        AND updated_at < ?
                      ORDER BY id ASC
                      LIMIT ?
                    )
                    """
                ),
                (
                    user_id,
                    source_id,
                    STATUS_SKIPPED,
                    *terminal,
                    cutoff,
                    PURGE_BATCH_SIZE,
                ),
            )
            n_skip = int(getattr(cur, "rowcount", 0) or 0)
            deleted += n_skip
        if not accepted_rows and n_skip == 0:
            break
    return deleted


def purge_old_kiz_runs_and_docs(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    retention_days: int = EVENT_RETENTION_DAYS,
) -> dict[str, int]:
    ensure_kiz_circulation_tables(repo)
    cutoff = _retention_cutoff_iso(days=retention_days)
    with repo._connect() as conn:
        cur_runs = conn.execute(
            repo._sql(
                """
                DELETE FROM wb_kiz_circulation_runs
                WHERE user_id = ? AND source_id = ?
                  AND COALESCE(finished_at, created_at, '') < ?
                  AND COALESCE(finished_at, created_at, '') <> ''
                """
            ),
            (user_id, source_id, cutoff),
        )
        cur_docs = conn.execute(
            repo._sql(
                """
                DELETE FROM wb_kiz_chz_documents
                WHERE user_id = ? AND source_id = ?
                  AND COALESCE(created_at, '') < ?
                  AND COALESCE(created_at, '') <> ''
                """
            ),
            (user_id, source_id, cutoff),
        )
    return {
        "runs": int(getattr(cur_runs, "rowcount", 0) or 0),
        "docs": int(getattr(cur_docs, "rowcount", 0) or 0),
    }


def _decrypt_wb_analytics_key(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    enc = str(row.get("wb_analytics_api_key_encrypted") or "").strip()
    if not enc:
        return ""
    return str(decrypt_secret(enc) or "").strip()


def get_wb_analytics_api_key(repo: ReviewRepository, *, user_id: int) -> str:
    """WB token for seller-analytics excise-report (not Marketplace FBS)."""
    ensure_kiz_circulation_tables(repo)
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql("SELECT wb_analytics_api_key_encrypted FROM supply_chz_settings WHERE user_id = ?"),
            (user_id,),
        ).fetchone()
    if not row:
        return ""
    return _decrypt_wb_analytics_key(repo._row_to_dict(row))


def get_chz_settings(
    repo: ReviewRepository, *, user_id: int, include_secrets: bool = False
) -> dict[str, Any]:
    ensure_kiz_circulation_tables(repo)
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql("SELECT * FROM supply_chz_settings WHERE user_id = ?"),
            (user_id,),
        ).fetchone()
    if not row:
        out = {
            "user_id": user_id,
            "is_enabled": False,
            "api_base": "prod",
            "api_base_url": PROD_BASE,
            "participant_inn": "",
            "product_group": "",
            "kpp": "",
            "fias_id": "",
            "return_type": "REMOTE_SALE_RETURN",
            "cert_thumbprint": "",
            "has_wb_analytics_api_key": False,
            "wb_analytics_api_key_preview": "",
        }
        if include_secrets:
            out["wb_analytics_api_key"] = ""
        return out
    d = repo._row_to_dict(row)
    api_base = str(d.get("api_base") or "prod").strip() or "prod"
    wb_key = _decrypt_wb_analytics_key(d)
    out = {
        "user_id": user_id,
        "is_enabled": bool(d.get("is_enabled")),
        "api_base": api_base if api_base in {"prod", "demo"} else "prod",
        "api_base_url": DEMO_BASE if api_base == "demo" else PROD_BASE,
        "participant_inn": str(d.get("participant_inn") or ""),
        "product_group": str(d.get("product_group") or ""),
        "kpp": str(d.get("kpp") or ""),
        "fias_id": str(d.get("fias_id") or ""),
        "return_type": str(d.get("return_type") or "REMOTE_SALE_RETURN"),
        "cert_thumbprint": str(d.get("cert_thumbprint") or ""),
        "has_wb_analytics_api_key": bool(wb_key),
        "wb_analytics_api_key_preview": mask_secret(wb_key) if wb_key else "",
        "updated_at": str(d.get("updated_at") or ""),
    }
    if include_secrets:
        out["wb_analytics_api_key"] = wb_key
    return out


def upsert_chz_settings(
    repo: ReviewRepository,
    *,
    user_id: int,
    is_enabled: bool = False,
    participant_inn: str = "",
    product_group: str = "",
    api_base: str | None = None,
    kpp: str | None = None,
    fias_id: str | None = None,
    return_type: str | None = None,
    cert_thumbprint: str | None = None,
    wb_analytics_api_key: str | None = None,
) -> dict[str, Any]:
    """Save minimal connection fields; omitted optional args keep previous values."""
    ensure_kiz_circulation_tables(repo)
    prev = get_chz_settings(repo, user_id=user_id, include_secrets=True)
    now = datetime.now(timezone.utc).isoformat()
    if api_base is None:
        base = "demo" if str(prev.get("api_base") or "") == "demo" else "prod"
    else:
        base = "demo" if str(api_base or "").strip().lower() == "demo" else "prod"
    pg = str(product_group or "").strip()
    # Reject pure-numeric placeholders that are not True API pg codes.
    if pg.isdigit():
        raise ValueError(
            "Товарная группа — код True API (например lp, shoes, clothes), не число"
        )
    kpp_s = str(prev.get("kpp") or "") if kpp is None else str(kpp or "").strip()
    fias_s = str(prev.get("fias_id") or "") if fias_id is None else str(fias_id or "").strip()
    ret_s = (
        str(prev.get("return_type") or "REMOTE_SALE_RETURN")
        if return_type is None
        else (str(return_type or "").strip() or "REMOTE_SALE_RETURN")
    )
    cert_s = (
        str(prev.get("cert_thumbprint") or "")
        if cert_thumbprint is None
        else str(cert_thumbprint or "").strip()
    )
    if wb_analytics_api_key is None:
        wb_enc = ""
        with repo._connect() as conn:
            row = conn.execute(
                repo._sql(
                    "SELECT wb_analytics_api_key_encrypted FROM supply_chz_settings "
                    "WHERE user_id = ?"
                ),
                (user_id,),
            ).fetchone()
            if row:
                wb_enc = str(repo._row_to_dict(row).get("wb_analytics_api_key_encrypted") or "")
    else:
        clean = str(wb_analytics_api_key or "").strip()
        wb_enc = encrypt_secret(clean) if clean else ""
        wb_enc = str(wb_enc or "")
    with repo._connect() as conn:
        conn.execute(
            repo._sql(
                """
                INSERT INTO supply_chz_settings (
                    user_id, is_enabled, api_base, participant_inn, product_group,
                    kpp, fias_id, return_type, cert_thumbprint,
                    wb_analytics_api_key_encrypted, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id) DO UPDATE SET
                    is_enabled = EXCLUDED.is_enabled,
                    api_base = EXCLUDED.api_base,
                    participant_inn = EXCLUDED.participant_inn,
                    product_group = EXCLUDED.product_group,
                    kpp = EXCLUDED.kpp,
                    fias_id = EXCLUDED.fias_id,
                    return_type = EXCLUDED.return_type,
                    cert_thumbprint = EXCLUDED.cert_thumbprint,
                    wb_analytics_api_key_encrypted = EXCLUDED.wb_analytics_api_key_encrypted,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            (
                user_id,
                bool(is_enabled),
                base,
                str(participant_inn or "").strip(),
                pg,
                kpp_s,
                fias_s,
                ret_s,
                cert_s,
                wb_enc,
                now,
            ),
        )
    return get_chz_settings(repo, user_id=user_id)


def _parse_inn_kpp_from_text(text: str) -> tuple[str, str]:
    raw = str(text or "")
    inn_m = re.search(r"ИНН\s*[:№]?\s*(\d{10}|\d{12})", raw, flags=re.IGNORECASE)
    kpp_m = re.search(r"КПП\s*[:№]?\s*(\d{9})", raw, flags=re.IGNORECASE)
    inn = inn_m.group(1) if inn_m else ""
    kpp = kpp_m.group(1) if kpp_m else ""
    if not inn:
        digits = re.findall(r"\b(\d{10}|\d{12})\b", raw)
        if digits:
            inn = digits[0]
    return inn, kpp


def resolve_chz_place_details(
    repo: ReviewRepository, *, user_id: int, participant_inn: str
) -> dict[str, str]:
    """Resolve KPP/FIAS for DISTANCE from legal entities matching participant INN."""
    inn = re.sub(r"\D", "", str(participant_inn or ""))
    out = {"kpp": "", "fias_id": ""}
    if not inn:
        return out
    try:
        entities = repo.list_supply_legal_entities(user_id=user_id)
    except Exception:
        return out
    for le in entities or []:
        if not isinstance(le, dict):
            continue
        req = str(le.get("requisites") or "")
        le_inn, le_kpp = _parse_inn_kpp_from_text(req)
        if le_inn and le_inn != inn:
            continue
        if not le_inn and inn not in re.sub(r"\D", "", req + str(le.get("short_name") or "")):
            # No INN in requisites — still allow if only one entity and FIAS present
            if len(entities) != 1:
                continue
        fias = str(le.get("addr_fias") or "").strip()
        if le_kpp and not out["kpp"]:
            out["kpp"] = le_kpp
        if fias and not out["fias_id"]:
            out["fias_id"] = fias
        if out["kpp"] and out["fias_id"]:
            break
    return out


def get_cursor(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> dict[str, Any]:
    ensure_kiz_circulation_tables(repo)
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql(
                "SELECT * FROM wb_kiz_circulation_cursor WHERE user_id = ? AND source_id = ?"
            ),
            (user_id, source_id),
        ).fetchone()
    if not row:
        return {
            "user_id": user_id,
            "source_id": source_id,
            "last_date_to": "",
            "last_event_key": "",
            "last_fiscal_dt": "",
            "last_run_at": "",
            "last_run_id": None,
            "last_storage_at": "",
        }
    return repo._row_to_dict(row)


def _append_log(parts: list[str], line: str) -> None:
    ts = datetime.now(MSK).strftime("%H:%M:%S")
    parts.append(f"[{ts}] {line}")


def _header_get(headers: Any, *names: str) -> str:
    if headers is None:
        return ""
    for name in names:
        try:
            val = headers.get(name)
        except Exception:
            val = None
        if val is None and hasattr(headers, "get"):
            try:
                # email.message.Message is case-insensitive; dict may need lower.
                val = headers.get(name.lower())
            except Exception:
                val = None
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def format_wb_excise_http_error(
    *, code: int, body: str = "", retry_after: str = "", reason: str = ""
) -> RuntimeError:
    """Human-readable WB Analytics excise-report errors (esp. 429 rate limit)."""
    if int(code) == 429:
        msg = (
            "Лимит WB на отчёт по маркировке: не больше 10 запросов за 5 часов "
            "(пауза между запросами около 30 минут). "
            "Не нажимайте «Ежедневный вывод» повторно — каждый клик тратит лимит. "
            "Один токен Аналитики общий для всех FBS-источников."
        )
        retry = str(retry_after or "").strip()
        if retry.isdigit():
            secs = int(retry)
            if secs >= 60:
                msg += f" Повторите примерно через {(secs + 59) // 60} мин."
            elif secs > 0:
                msg += f" Повторите примерно через {secs} сек."
        elif retry:
            msg += f" Повторите после: {retry}."
        return RuntimeError(msg)
    detail = (body or reason or "").strip()
    if detail:
        return RuntimeError(f"WB excise-report HTTP {code}: {detail[:500]}")
    return RuntimeError(f"WB excise-report HTTP {code}")


def _analytics_fetch_is_soft_failure(exc: BaseException) -> bool:
    """True when Analytics outage can fall back to Marketplace-only queue.

    Auth / 429 must stay hard failures (wrong token or quota). Timeouts and
    gateway 5xx are soft when Marketplace FBS can still build the queue.
    """
    msg = str(exc or "").lower()
    if any(
        token in msg
        for token in (
            "429",
            "rate limit",
            "слишком много",
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "авториз",
        )
    ):
        return False
    if any(
        token in msg
        for token in (
            "504",
            "502",
            "503",
            "500",
            "timeout",
            "timed out",
            "stream timeout",
            "сеть",
        )
    ):
        return True
    return False


def fetch_wb_excise_report(
    *,
    api_key: str,
    date_from: str,
    date_to: str,
    countries: list[str] | None = None,
    timeout: int = 60,
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    params = urlencode({"dateFrom": date_from, "dateTo": date_to})
    url = f"{WB_ANALYTICS_API}/api/v1/analytics/excise-report?{params}"
    body_obj: dict[str, object] = {}
    if countries:
        body_obj["countries"] = countries
    data = json.dumps(body_obj).encode("utf-8")
    last_exc: Exception | None = None
    parsed: Any = None
    for attempt in range(max(1, int(max_retries))):
        req = urllib.request.Request(
            url,
            method="POST",
            data=data,
            headers={
                "Authorization": str(api_key or "").strip(),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "FeedPilot-KizCirculation/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                if not payload:
                    return []
                parsed = json.loads(payload.decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            err = ""
            try:
                err = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            retry_after = _header_get(
                getattr(exc, "headers", None),
                "X-RateLimit-Retry",
                "x-ratelimit-retry",
                "Retry-After",
                "retry-after",
            )
            last_exc = format_wb_excise_http_error(
                code=int(exc.code),
                body=err,
                retry_after=retry_after,
                reason=str(exc.reason or ""),
            )
            # 429 must NOT be retried: each attempt burns the 10/5h quota.
            if int(exc.code) == 429:
                raise last_exc from exc
            if attempt + 1 < max_retries and int(exc.code) in {500, 502, 503, 504}:
                time.sleep(2 * (attempt + 1))
                continue
            raise last_exc from exc
        except urllib.error.URLError as exc:
            last_exc = RuntimeError(f"WB excise-report сеть: {exc.reason}")
            if attempt + 1 < max_retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise last_exc from exc
    else:
        if last_exc:
            raise last_exc
        return []

    rows: list[Any] = []
    if isinstance(parsed, dict):
        response = parsed.get("response")
        if isinstance(response, dict) and isinstance(response.get("data"), list):
            rows = response["data"]
        elif isinstance(parsed.get("data"), list):
            rows = parsed["data"]
    elif isinstance(parsed, list):
        rows = parsed
    return [r for r in rows if isinstance(r, dict)]


def _normalize_row(row: dict[str, Any]) -> dict[str, Any] | None:
    op_raw = row.get("operation_type_id")
    if op_raw is None:
        op_raw = row.get("operationTypeId")
    try:
        op = int(op_raw or 0)
    except (TypeError, ValueError):
        op = 0
    if op not in {OP_WITHDRAW, OP_RETURN}:
        return None
    # Avoid str.strip(): in CPython ``\\x1d``.isspace() is True and would
    # eat GS separators at the ends of a full Data Matrix string.
    excise = str(
        row.get("excise_short") or row.get("exciseShort") or row.get("kiz") or ""
    ).strip(" \t\r\n")
    if not excise:
        return None
    srid = str(row.get("srid") or "").strip()
    fiscal_no = _fiscal_doc_str(
        row.get("fiscal_doc_number")
        if row.get("fiscal_doc_number") is not None
        else row.get("fiscalDocNumber")
    )
    fiscal_dt = str(row.get("fiscal_dt") or row.get("fiscalDt") or "").strip()
    if fiscal_dt and "T" in fiscal_dt:
        fiscal_dt = fiscal_dt.split("T", 1)[0]
    rid = str(row.get("rid") or "").strip()
    nm_raw = row.get("nm_id") if row.get("nm_id") is not None else row.get("nmId")
    try:
        nm_id = int(nm_raw) if nm_raw is not None and str(nm_raw).strip() != "" else None
    except (TypeError, ValueError):
        nm_id = None
    price_raw = row.get("price")
    try:
        price = float(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        price = None
    currency = str(
        row.get("currency_name_short")
        or row.get("currencyNameShort")
        or row.get("currency")
        or ""
    ).strip().upper()
    key = _event_key(
        srid=srid,
        excise_short=excise,
        operation_type=op,
        fiscal_doc_number=fiscal_no,
        fiscal_dt=fiscal_dt,
    )
    return {
        "event_key": key,
        "operation_type": op,
        "srid": srid,
        "rid": rid,
        "nm_id": nm_id,
        "barcode": str(row.get("barcode") or "").strip(),
        "excise_short": excise,
        "fiscal_doc_number": fiscal_no,
        "fiscal_dt": fiscal_dt,
        "fiscal_drive_number": str(
            row.get("fiscal_drive_number") or row.get("fiscalDriveNumber") or ""
        ).strip(),
        "price": price,
        "currency_name": currency,
        "country_name": str(row.get("name") or row.get("countryName") or "").strip(),
        "raw_json": json.dumps(row, ensure_ascii=False),
    }


def _msk_day_bounds(date_from: str, date_to: str) -> tuple[datetime, datetime]:
    """Inclusive Moscow calendar days → timezone-aware datetimes."""
    d0 = date.fromisoformat(_parse_date(date_from))
    d1 = date.fromisoformat(_parse_date(date_to))
    if d1 < d0:
        d0, d1 = d1, d0
    start = datetime(d0.year, d0.month, d0.day, 0, 0, 0, tzinfo=MSK)
    end = datetime(d1.year, d1.month, d1.day, 23, 59, 59, tzinfo=MSK)
    return start, end


def _msk_day_windows(
    date_from: str,
    date_to: str,
    *,
    max_days: int = 30,
) -> list[tuple[datetime, datetime]]:
    """Split inclusive MSK date range into WB-safe windows (max 30 calendar days).

    ``GET /api/v3/orders`` returns HTTP 400 IncorrectParameter when the span
    exceeds 30 calendar days.
    """
    d0 = date.fromisoformat(_parse_date(date_from))
    d1 = date.fromisoformat(_parse_date(date_to))
    if d1 < d0:
        d0, d1 = d1, d0
    span = max(1, int(max_days))
    windows: list[tuple[datetime, datetime]] = []
    cur = d0
    while cur <= d1:
        chunk_end = min(cur + timedelta(days=span - 1), d1)
        start = datetime(cur.year, cur.month, cur.day, 0, 0, 0, tzinfo=MSK)
        end = datetime(
            chunk_end.year, chunk_end.month, chunk_end.day, 23, 59, 59, tzinfo=MSK
        )
        windows.append((start, end))
        cur = chunk_end + timedelta(days=1)
    return windows


def _sgtin_codes_from_meta_row(row: dict[str, Any]) -> list[str]:
    """Extract КИЗ list from ``POST /orders/meta`` item."""
    from . import wb_fbs as wb_fbs_mod

    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    sgtin_wrap = meta.get("sgtin") if isinstance(meta, dict) else None
    raw: object = None
    if isinstance(sgtin_wrap, dict):
        raw = sgtin_wrap.get("value")
    elif sgtin_wrap is not None:
        raw = sgtin_wrap
    if raw is None:
        # Some payloads put the current value on metaDetails.
        for item in row.get("metaDetails") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("key") or "").strip().lower() != "sgtin":
                continue
            raw = item.get("value")
            break
    codes: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            c = wb_fbs_mod._kiz_code_clean(item)
            if c:
                codes.append(c)
    else:
        c = wb_fbs_mod._kiz_code_clean(raw)
        if c:
            codes.append(c)
    # Deduplicate preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for c in codes:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def _analytics_by_mid(
    norms: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for norm in norms:
        for raw in (norm.get("srid"), norm.get("rid")):
            mid = _rid_mid_token(raw)
            if mid:
                out.setdefault(mid, []).append(norm)
    return out


def _enrich_norm_from_analytics(
    norm: dict[str, Any], analytics_mids: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Copy fiscal / price from Analytics when Marketplace row lacks them."""
    if not analytics_mids:
        return norm
    mid = _rid_mid_token(norm.get("srid") or norm.get("rid"))
    if not mid:
        return norm
    op = int(norm.get("operation_type") or 0)
    siblings = [
        a
        for a in analytics_mids.get(mid, [])
        if int(a.get("operation_type") or 0) == op
        or not a.get("operation_type")
    ] or list(analytics_mids.get(mid) or [])
    if not siblings:
        return norm
    # Prefer Analytics row with same CIS, else first with fiscal.
    cis = str(norm.get("excise_short") or "").strip()
    pick = None
    for a in siblings:
        if cis and str(a.get("excise_short") or "").strip() == cis:
            pick = a
            break
    if pick is None:
        pick = next(
            (
                a
                for a in siblings
                if a.get("fiscal_doc_number") and a.get("fiscal_dt")
            ),
            siblings[0],
        )
    enriched = dict(norm)
    # Prefer Analytics fiscal receipt when present (Marketplace usually has none).
    if pick.get("fiscal_doc_number") and pick.get("fiscal_dt"):
        enriched["fiscal_doc_number"] = pick.get("fiscal_doc_number")
        enriched["fiscal_dt"] = pick.get("fiscal_dt")
        if pick.get("fiscal_drive_number"):
            enriched["fiscal_drive_number"] = pick.get("fiscal_drive_number")
    for key in (
        "fiscal_drive_number",
        "price",
        "currency_name",
        "barcode",
        "nm_id",
        "country_name",
    ):
        if not enriched.get(key) and pick.get(key) not in (None, ""):
            enriched[key] = pick.get(key)
    # Rebuild event_key after fiscal attach.
    enriched["event_key"] = _event_key(
        srid=str(enriched.get("srid") or ""),
        excise_short=str(enriched.get("excise_short") or ""),
        operation_type=int(enriched.get("operation_type") or 0),
        fiscal_doc_number=str(enriched.get("fiscal_doc_number") or ""),
        fiscal_dt=str(enriched.get("fiscal_dt") or ""),
    )
    return enriched


def build_marketplace_period_norms(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    api_key: str,
    date_from: str,
    date_to: str,
    log: list[str] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Marketplace FBS orders in ``[date_from, date_to]`` → circulation norms.

    Returns ``(norms, fbs_index, meta)``.
    """
    from . import wb_fbs as wb_fbs_mod

    mkey = str(api_key or "").strip()
    if not mkey:
        return [], {}, {"ok": False, "error": "no_marketplace_token"}

    windows = _msk_day_windows(date_from, date_to, max_days=30)
    client = wb_fbs_mod.WbFbsClient(mkey, timeout=60)
    orders: list[dict[str, Any]] = []
    seen_oids: set[int] = set()
    pages = 0
    if log is not None and len(windows) > 1:
        _append_log(
            log,
            f"Marketplace: период разбит на {len(windows)} окон по ≤30 дн. (лимит WB)",
        )
    for w_i, (start, end) in enumerate(windows, start=1):
        next_token: int | None = 0
        while pages < 120:
            if cancel_check is not None:
                cancel_check()
            try:
                batch, next_token = client.get_orders_page(
                    limit=1000,
                    next_token=next_token if next_token is not None else 0,
                    date_from=start,
                    date_to=end,
                )
            except RuntimeError as exc:
                msg = str(exc)
                if "IncorrectParameter" in msg:
                    raise RuntimeError(
                        "WB Marketplace: некорректный период заказов "
                        f"({start.date()}…{end.date()}). "
                        "Лимит API — не больше 30 календарных дней за запрос. "
                        f"Исходная ошибка: {msg}"
                    ) from exc
                raise
            pages += 1
            if not batch:
                break
            for row in batch:
                try:
                    oid = int(row.get("id") or 0)
                except (TypeError, ValueError):
                    oid = 0
                if oid > 0:
                    if oid in seen_oids:
                        continue
                    seen_oids.add(oid)
                orders.append(row)
            if log is not None and pages % 3 == 0:
                _append_log(log, f"Marketplace заказы: загружено {len(orders)}…")
            if next_token is None:
                break
            time.sleep(0.2)
        if log is not None and len(windows) > 1:
            _append_log(
                log,
                f"Marketplace окно {w_i}/{len(windows)} "
                f"{start.date()}…{end.date()}: накоплено {len(orders)} заказов",
            )

    if log is not None:
        _append_log(
            log,
            f"Marketplace FBS за период: заказов={len(orders)} (стр. {pages})",
        )

    ids = sorted(
        {
            int(o.get("id") or 0)
            for o in orders
            if int(o.get("id") or 0) > 0
        }
    )
    status_map: dict[int, dict[str, Any]] = {}
    for i in range(0, len(ids), 1000):
        if cancel_check is not None:
            cancel_check()
        chunk = ids[i : i + 1000]
        for st in client.get_statuses(chunk) or []:
            try:
                oid = int(st.get("id") or st.get("orderId") or 0)
            except (TypeError, ValueError):
                oid = 0
            if oid > 0:
                status_map[oid] = st
        time.sleep(0.2)

    needed: list[tuple[dict[str, Any], str, str]] = []
    status_counts: dict[str, int] = {}
    for order in orders:
        try:
            oid = int(order.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if oid <= 0:
            continue
        st = status_map.get(oid) or {}
        ws = str(st.get("wbStatus") or order.get("wbStatus") or "").strip().lower()
        ss = str(
            st.get("supplierStatus") or order.get("supplierStatus") or ""
        ).strip().lower()
        status_counts[ws or "(empty)"] = status_counts.get(ws or "(empty)", 0) + 1
        if ws not in WB_STATUS_CIRCULATION:
            continue
        needed.append((order, ws, ss))
        # Keep local FBS row fresh for UI joins.
        try:
            wb_fbs_mod.upsert_order(
                repo,
                user_id=user_id,
                source_id=source_id,
                order=order,
                is_archive=ws in WB_STATUS_CIRCULATION,
                supplier_status=ss,
                wb_status=ws,
            )
        except Exception as exc:
            logger.debug("upsert period order %s failed: %s", oid, exc)

    if log is not None:
        _append_log(
            log,
            "Marketplace статусы для КИЗ: "
            f"sold={sum(1 for _, w, _ in needed if w in WB_STATUS_WITHDRAW)}, "
            f"отказ/дефект={sum(1 for _, w, _ in needed if w in WB_STATUS_RETURN)} "
            f"(из {len(orders)})",
        )

    meta_by_id: dict[int, dict[str, Any]] = {}
    need_ids = [int(o.get("id") or 0) for o, _, _ in needed]
    for i in range(0, len(need_ids), 100):
        if cancel_check is not None:
            cancel_check()
        chunk = [x for x in need_ids[i : i + 100] if x > 0]
        if not chunk:
            continue
        try:
            for row in client.get_orders_meta(chunk) or []:
                try:
                    oid = int(row.get("id") or 0)
                except (TypeError, ValueError):
                    oid = 0
                if oid > 0:
                    meta_by_id[oid] = row
        except Exception as exc:
            logger.warning("orders meta chunk failed: %s", exc)
            if log is not None:
                _append_log(log, f"meta КИЗ: ошибка пакета — {exc}")
        time.sleep(0.22)

    norms: list[dict[str, Any]] = []
    fbs_index: dict[str, dict[str, Any]] = {}
    with_kiz = 0
    without_kiz = 0
    for order, ws, ss in needed:
        try:
            oid = int(order.get("id") or 0)
        except (TypeError, ValueError):
            continue
        rid = str(order.get("rid") or "").strip()
        uid = str(order.get("orderUid") or order.get("order_uid") or "").strip()
        info = {
            "order_id": oid,
            "wb_status": ws,
            "supplier_status": ss,
        }
        for key in _rid_match_keys(rid):
            fbs_index.setdefault(key, info)
        for key in _rid_match_keys(uid):
            fbs_index.setdefault(key, info)

        codes = _sgtin_codes_from_meta_row(meta_by_id.get(oid) or {})
        # Fall back to locally saved КИЗ from сборка.
        if not codes:
            try:
                local_map = wb_fbs_mod.load_order_kiz_map(
                    repo, user_id=user_id, source_id=source_id, order_ids=[oid]
                )
                codes = list((local_map.get(oid) or {}).get("codes") or [])
            except Exception:
                codes = []
        if not codes:
            without_kiz += 1
            continue
        with_kiz += 1
        op = OP_WITHDRAW if ws in WB_STATUS_WITHDRAW else OP_RETURN
        try:
            nm_id = int(order.get("nmId") or order.get("nm_id") or 0) or None
        except (TypeError, ValueError):
            nm_id = None
        skus = order.get("skus") if isinstance(order.get("skus"), list) else []
        barcode = str(skus[0] if skus else "").strip()
        created = str(order.get("createdAt") or "").strip()
        fiscal_dt = ""
        if created:
            try:
                fiscal_dt = (
                    datetime.fromisoformat(created.replace("Z", "+00:00"))
                    .astimezone(MSK)
                    .date()
                    .isoformat()
                )
            except Exception:
                fiscal_dt = created[:10] if len(created) >= 10 else ""
        # Marketplace /orders exposes convertedPrice/price in kopecks — required for
        # CHZ product_cost on OTHER (no fiscal) withdraws. Analytics may fill later.
        price_rub: float | None = None
        currency_name = ""
        try:
            from .wb_fbs import resolve_order_price

            amount_kop, ccy = resolve_order_price(order)
            if amount_kop and int(amount_kop) > 0:
                price_rub = float(amount_kop) / 100.0
                # 643 = RUB; empty currency lets _price_for_chz accept the amount.
                if int(ccy or 0) in (0, 643, 810):
                    currency_name = "RUB"
        except Exception:
            price_rub = None
            currency_name = ""
        for cis in codes:
            key = _event_key(
                srid=rid,
                excise_short=cis,
                operation_type=op,
                fiscal_doc_number="",
                fiscal_dt=fiscal_dt,
            )
            norms.append(
                {
                    "event_key": key,
                    "operation_type": op,
                    "srid": rid,
                    "rid": rid,
                    "nm_id": nm_id,
                    "barcode": barcode,
                    "excise_short": cis,
                    "fiscal_doc_number": "",
                    "fiscal_dt": fiscal_dt,
                    "fiscal_drive_number": "",
                    "price": price_rub,
                    "currency_name": currency_name,
                    "country_name": "",
                    "raw_json": "",
                    "order_id": oid,
                    "wb_status": ws,
                }
            )

    meta = {
        "ok": True,
        "orders": len(orders),
        "needed": len(needed),
        "with_kiz": with_kiz,
        "without_kiz": without_kiz,
        "norms": len(norms),
        "status_counts": status_counts,
        "sold": sum(1 for _, w, _ in needed if w in WB_STATUS_WITHDRAW),
        "returns": sum(1 for _, w, _ in needed if w in WB_STATUS_RETURN),
    }
    if log is not None:
        _append_log(
            log,
            f"Marketplace→очередь: с КИЗ={with_kiz}, без КИЗ={without_kiz}, "
            f"событий={len(norms)}",
        )
    return norms, fbs_index, meta


def _initial_status(norm: dict[str, Any]) -> tuple[str, str]:
    """Queue withdraw without fiscal for CHZ via OTHER primary document.

    Returns may omit fiscal (WB: «если есть»). Keep ``no_fiscal`` reason so
    prepare uses document_type=OTHER instead of RECEIPT.
    """
    op = int(norm.get("operation_type") or 0)
    has_fiscal = bool(norm.get("fiscal_doc_number") and norm.get("fiscal_dt"))
    if op == OP_WITHDRAW and not has_fiscal:
        return STATUS_PENDING, SKIP_NO_FISCAL
    return STATUS_PENDING, ""


def _find_related_events(
    conn: Any,
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    norm: dict[str, Any],
) -> list[dict[str, Any]]:
    """Find local events for the same CIS identity (any fiscal variant)."""
    excise = str(norm.get("excise_short") or "").strip()
    op = int(norm.get("operation_type") or 0)
    if not excise or op not in {OP_WITHDRAW, OP_RETURN}:
        return []
    srid = str(norm.get("srid") or "").strip()
    rid = str(norm.get("rid") or "").strip()
    anchors = sorted({a for a in (srid, rid) if a})
    if anchors:
        ph = ", ".join("?" for _ in anchors)
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT * FROM wb_kiz_circulation_events
                WHERE user_id = ? AND source_id = ?
                  AND operation_type = ? AND excise_short = ?
                  AND (
                    (COALESCE(srid, '') <> '' AND srid IN ({ph}))
                    OR (COALESCE(rid, '') <> '' AND rid IN ({ph}))
                  )
                """
            ),
            (user_id, source_id, op, excise, *anchors, *anchors),
        ).fetchall()
    else:
        rows = conn.execute(
            repo._sql(
                """
                SELECT * FROM wb_kiz_circulation_events
                WHERE user_id = ? AND source_id = ?
                  AND operation_type = ? AND excise_short = ?
                  AND COALESCE(srid, '') = '' AND COALESCE(rid, '') = ''
                """
            ),
            (user_id, source_id, op, excise),
        ).fetchall()
    return [repo._row_to_dict(r) for r in rows]


def _upgrade_event_fiscal(
    conn: Any,
    repo: ReviewRepository,
    *,
    target: dict[str, Any],
    norm: dict[str, Any],
    now: str,
) -> None:
    """Attach late fiscal (or refresh) onto an existing open event — keep event_key."""
    oid = int(target.get("id") or 0)
    if oid <= 0:
        return
    fiscal_no = str(norm.get("fiscal_doc_number") or "").strip() or str(
        target.get("fiscal_doc_number") or ""
    ).strip()
    fiscal_dt = str(norm.get("fiscal_dt") or "").strip() or str(
        target.get("fiscal_dt") or ""
    ).strip()
    drive = str(norm.get("fiscal_drive_number") or "").strip() or str(
        target.get("fiscal_drive_number") or ""
    ).strip()
    has_fiscal = bool(fiscal_no and fiscal_dt)
    skip = (
        SKIP_NO_FISCAL
        if int(norm.get("operation_type") or 0) == OP_WITHDRAW and not has_fiscal
        else ""
    )
    st = str(target.get("status") or "")
    new_status = (
        STATUS_PENDING
        if st in {STATUS_SKIPPED, STATUS_ERROR, STATUS_READY, STATUS_PENDING}
        else st
    )
    conn.execute(
        repo._sql(
            """
            UPDATE wb_kiz_circulation_events
            SET fiscal_doc_number = ?,
                fiscal_dt = ?,
                fiscal_drive_number = ?,
                price = COALESCE(?, price),
                currency_name = CASE
                    WHEN COALESCE(?, '') <> '' THEN ? ELSE currency_name END,
                country_name = CASE
                    WHEN COALESCE(?, '') <> '' THEN ? ELSE country_name END,
                raw_json = ?,
                status = ?,
                skip_reason = ?,
                error_text = CASE WHEN ? = ? THEN '' ELSE error_text END,
                updated_at = ?
            WHERE id = ?
            """
        ),
        (
            fiscal_no,
            fiscal_dt,
            drive,
            norm.get("price"),
            str(norm.get("currency_name") or ""),
            str(norm.get("currency_name") or ""),
            str(norm.get("country_name") or ""),
            str(norm.get("country_name") or ""),
            str(norm.get("raw_json") or target.get("raw_json") or ""),
            new_status,
            skip,
            new_status,
            STATUS_PENDING,
            now,
            oid,
        ),
    )


def _resolve_sync_action(
    related: list[dict[str, Any]],
    *,
    norm: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Decide insert / upsert / upgrade / suppress for a normalized WB row.

    Returns ``(action, target_row)`` where action is one of:
    insert | upsert | upgrade | suppress
    """
    key = str(norm.get("event_key") or "")
    same_key = next((r for r in related if str(r.get("event_key") or "") == key), None)
    if same_key:
        return "upsert", same_key

    new_has_fiscal = _event_has_fiscal(norm)
    terminal = [
        r
        for r in related
        if str(r.get("status") or "") in {STATUS_SUBMITTED, STATUS_ACCEPTED}
    ]
    if terminal:
        # Already sent under another fiscal variant — do not create a duplicate.
        return "suppress", terminal[0]

    open_rows = [
        r
        for r in related
        if str(r.get("status") or "")
        in {STATUS_PENDING, STATUS_READY, STATUS_ERROR, STATUS_SKIPPED}
    ]
    if new_has_fiscal:
        # Prefer upgrading an open no-fiscal sibling instead of a second event_key.
        nofiscal_open = [r for r in open_rows if not _event_has_fiscal(r)]
        if nofiscal_open:
            return "upgrade", nofiscal_open[0]
        if open_rows:
            # Same CIS already queued under another fiscal key — keep one row.
            return "upgrade", open_rows[0]
    else:
        # Incoming no-fiscal while a fiscal open row already exists — keep fiscal one.
        fiscal_open = [r for r in open_rows if _event_has_fiscal(r)]
        if fiscal_open:
            return "suppress", fiscal_open[0]
        if open_rows:
            # Another no-fiscal open row with different key (legacy) — upgrade first.
            return "upgrade", open_rows[0]

    return "insert", None


def sync_excise_report(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    api_key: str,
    date_from: str = "",
    date_to: str = "",
    run_id: int | None = None,
    marketplace_api_key: str = "",
) -> dict[str, Any]:
    ensure_kiz_circulation_tables(repo)
    period = resolve_excise_period(date_from=date_from, date_to=date_to)
    date_from_s = str(period["date_from"])
    date_to_s = str(period["date_to"])

    now = datetime.now(timezone.utc).isoformat()
    log: list[str] = []
    run_id = int(run_id or 0)
    if run_id > 0:
        _register_sync_cancel(run_id)

    def _flush(status: str = "running", **counts: Any) -> None:
        if run_id <= 0:
            return
        _finish_run(
            repo,
            run_id=run_id,
            status=status,
            log=log,
            fetched=int(counts.get("fetched") or 0),
            inserted=int(counts.get("inserted") or 0),
            skipped=int(counts.get("skipped") or 0),
            withdraw_count=int(counts.get("withdraw_count") or 0),
            return_count=int(counts.get("return_count") or 0),
            error_text=str(counts.get("error_text") or ""),
        )

    def _progress(line: str, **counts: Any) -> None:
        _append_log(log, line)
        _flush(**counts)

    try:
        _progress(
            f"WB: выгрузка за выбранные даты {date_from_s}…{date_to_s} "
            f"({period['days']} дн., фоновый режим)"
        )
        if int(period["days"] or 0) > 31:
            _progress(
                "совет: для периода >31 дня запрос к WB и запись в БД могут быть долгими; "
                "лучше брать ≤31 день порциями"
            )

        _check_sync_cancelled(run_id)
        _progress("подготовка очереди (FBS-фильтр, очистка FBO)…")

        repaired = repair_circulation_queue(
            repo,
            user_id=user_id,
            source_id=source_id,
            run_id=run_id if run_id > 0 else None,
            on_log=lambda msg: _progress(msg),
        )
        try:
            # Throttled — force=True on large syncs caused GC to burn the HTTP budget.
            _check_sync_cancelled(run_id)
            storage = maintain_kiz_circulation_storage(
                repo, user_id=user_id, source_id=source_id, force=False
            )
            repaired.update(storage)
        except SyncCancelled:
            raise
        except Exception as exc:
            logger.exception("maintain_kiz_circulation_storage failed: %s", exc)

        if repaired.get("returns_fixed"):
            _progress(f"восстановлено возвратов без чека: {repaired['returns_fixed']}")
        if repaired.get("withdraw_skipped"):
            _progress(
                f"выводы без чека из error → очередь OTHER: {repaired['withdraw_skipped']}"
            )
        if repaired.get("withdraw_requeued"):
            _progress(
                f"выводы без чека из skipped → очередь OTHER: {repaired['withdraw_requeued']}"
            )
        if repaired.get("orphan_submitted"):
            _progress(
                f"восстановлено submitted без chz_doc_id: {repaired['orphan_submitted']}"
            )
        if repaired.get("legacy_skipped"):
            _progress(f"возвращено из skipped в очередь: {repaired['legacy_skipped']}")
        if repaired.get("fbs_requeued"):
            _progress(f"снова в очереди (нашлись в FBS): {repaired['fbs_requeued']}")
        if repaired.get("not_fbs_skipped"):
            _progress(
                f"убрано из очереди (нет в заказах FBS, вероятно FBO): "
                f"{repaired['not_fbs_skipped']}"
            )
        if repaired.get("not_fbs_purged"):
            _progress(f"удалено из таблицы (не FBS / FBO): {repaired['not_fbs_purged']}")
        if repaired.get("not_sold_skipped"):
            _progress(
                f"убрано (вывод только для выкупленных sold): "
                f"{repaired['not_sold_skipped']}"
            )
        if repaired.get("not_return_skipped"):
            _progress(
                f"убрано (возврат в оборот только для отказных): "
                f"{repaired['not_return_skipped']}"
            )
        if repaired.get("raw_json_cleared") or repaired.get("events_purged"):
            _progress(
                "очистка хранения: "
                f"raw_json={repaired.get('raw_json_cleared') or 0}, "
                f"событий>{EVENT_RETENTION_DAYS}д={repaired.get('events_purged') or 0}, "
                f"runs={repaired.get('runs_purged') or 0}, "
                f"docs={repaired.get('docs_purged') or 0}"
            )

        if run_id <= 0:
            with repo._connect() as conn:
                row = conn.execute(
                    repo._sql(
                        """
                        INSERT INTO wb_kiz_circulation_runs (
                            user_id, source_id, date_from, date_to, stage, status,
                            created_at, log_text
                        ) VALUES (?, ?, ?, ?, 'wb_sync', 'running', ?, ?)
                        RETURNING id
                        """
                    ),
                    (
                        user_id,
                        source_id,
                        date_from_s,
                        date_to_s,
                        now,
                        "\n".join(log)[:50000],
                    ),
                ).fetchone()
                run_id = int(repo._row_to_dict(row).get("id") or 0) if row else 0
            if run_id > 0:
                _register_sync_cancel(run_id)
        else:
            with repo._connect() as conn:
                conn.execute(
                    repo._sql(
                        """
                        UPDATE wb_kiz_circulation_runs SET
                            date_from = ?, date_to = ?, status = 'running',
                            log_text = ?, error_text = ''
                        WHERE id = ? AND user_id = ?
                        """
                    ),
                    (date_from_s, date_to_s, "\n".join(log)[:50000], run_id, user_id),
                )

        _check_sync_cancelled(run_id)
        api_key_s = str(api_key or "").strip()
        rows: list[Any] = []
        if api_key_s:
            _progress(
                "запрос к WB Analytics excise-report… "
                "(стоп применится после ответа WB; лимит 10 запросов / 5 ч)"
            )

            fetch_holder: dict[str, Any] = {"rows": None, "exc": None}

            def _wb_worker() -> None:
                try:
                    fetch_holder["rows"] = fetch_wb_excise_report(
                        api_key=api_key_s,
                        date_from=date_from_s,
                        date_to=date_to_s,
                        timeout=120,
                    )
                except Exception as exc:  # noqa: BLE001 — surface to parent thread
                    fetch_holder["exc"] = exc

            wb_thread = threading.Thread(
                target=_wb_worker, name=f"kiz-wb-fetch-{run_id}", daemon=True
            )
            wb_thread.start()
            last_beat = time.monotonic()
            while wb_thread.is_alive():
                if _sync_cancel_requested(run_id):
                    _progress(
                        "стоп запрошен — жду завершения текущего запроса WB "
                        "(уже отправленный запрос нельзя отменить)"
                    )
                wb_thread.join(timeout=5.0)
                now_m = time.monotonic()
                if wb_thread.is_alive() and now_m - last_beat >= 15:
                    _progress("ожидание ответа WB Analytics…")
                    last_beat = now_m
            if fetch_holder["exc"] is not None:
                # Prefer user cancel over a late WB error if Стоп was pressed mid-fetch.
                _check_sync_cancelled(run_id)
                analytics_exc = fetch_holder["exc"]
                mkey_soft = str(marketplace_api_key or "").strip()
                if mkey_soft and _analytics_fetch_is_soft_failure(analytics_exc):
                    _progress(
                        "Analytics недоступен "
                        f"({analytics_exc}) — продолжаю только Marketplace FBS "
                        "(sold / отказ / дефект с КИЗ); фискальные поля "
                        "из Analytics не подтянутся"
                    )
                    rows = []
                else:
                    raise analytics_exc
            else:
                rows = list(fetch_holder["rows"] or [])
            _check_sync_cancelled(run_id)

            if rows or fetch_holder["exc"] is None:
                _progress(
                    f"получено {len(rows)} строк из Analytics excise-report",
                    fetched=len(rows),
                )
        else:
            _progress(
                "Analytics токен не задан — очередь только из Marketplace FBS "
                "(sold / отказ / дефект с КИЗ)"
            )
        analytics_norms: list[dict[str, Any]] = []
        skipped_bad = 0
        for raw in rows:
            _check_sync_cancelled(run_id)
            norm = _normalize_row(raw)
            if not norm:
                skipped_bad += 1
                continue
            analytics_norms.append(norm)
        _progress(
            f"Analytics нормализовано {len(analytics_norms)}"
            + (f", битых {skipped_bad}" if skipped_bad else ""),
            fetched=len(rows),
        )
        _check_sync_cancelled(run_id)

        mkey = str(marketplace_api_key or "").strip()
        mp_norms: list[dict[str, Any]] = []
        fbs_index: dict[str, dict[str, Any]] = {}
        mp_meta: dict[str, Any] = {"ok": False}
        if mkey:
            _progress(
                "Marketplace FBS за выбранные даты (sold / отказ ПВЗ / дефект)…",
                fetched=len(rows),
            )
            mp_norms, fbs_index, mp_meta = build_marketplace_period_norms(
                repo,
                user_id=user_id,
                source_id=source_id,
                api_key=mkey,
                date_from=date_from_s,
                date_to=date_to_s,
                log=log,
                cancel_check=(
                    (lambda: _check_sync_cancelled(run_id)) if run_id > 0 else None
                ),
            )
            _flush(fetched=len(rows))
        else:
            _progress(
                "нет Marketplace FBS токена — очередь только из Analytics∩локальных rid",
                fetched=len(rows),
            )

        # Enrich Marketplace rows with Analytics fiscal; keep Analytics-only
        # matches as a fallback for CIS that meta did not return.
        analytics_mids = _analytics_by_mid(analytics_norms)
        if mp_norms and analytics_mids:
            mp_norms = [
                _enrich_norm_from_analytics(n, analytics_mids) for n in mp_norms
            ]
            _progress(
                f"фискальные поля Analytics наложены на {len(mp_norms)} Marketplace строк",
                fetched=len(rows),
            )

        _progress(
            "доп. сопоставление Analytics srid↔Marketplace rid…",
            fetched=len(rows),
        )
        an_index, match_meta = build_excise_fbs_match_index(
            repo,
            user_id=user_id,
            source_id=source_id,
            norms=analytics_norms,
            marketplace_api_key=mkey,
            log=log,
        )
        for k, v in an_index.items():
            fbs_index.setdefault(k, v)
        _flush(fetched=len(rows))
        _check_sync_cancelled(run_id)

        local_rid_n = len(
            load_local_fbs_order_index(repo, user_id=user_id, source_id=source_id)
        )
        if not fbs_index and not mp_norms:
            if not match_meta.get("hydrate_ok") and local_rid_n > 0 and not mkey:
                msg = (
                    "не удалось сопоставить отчёт с FBS (нет Marketplace токена). "
                    f"Локальных rid={local_rid_n}."
                )
                _append_log(log, f"ОШИБКА: {msg}")
                _finish_run(
                    repo,
                    run_id=run_id,
                    status="error",
                    log=log,
                    fetched=len(rows),
                    inserted=0,
                    skipped=len(rows),
                    error_text=msg,
                )
                return {
                    "ok": False,
                    "run_id": run_id,
                    "date_from": date_from_s,
                    "date_to": date_to_s,
                    "fetched": len(rows),
                    "inserted": 0,
                    "updated": 0,
                    "skipped": len(rows),
                    "skipped_not_fbs": len(analytics_norms),
                    "error": msg,
                    "withdraw_count": 0,
                    "return_count": 0,
                    "log": "\n".join(log),
                    "cursor": get_cursor(repo, user_id=user_id, source_id=source_id),
                }
            _progress(
                "внимание: Marketplace не вернул sold/отказ/дефект с КИЗ и "
                f"Analytics∩FBS пуст (локальных ключей={local_rid_n}).",
                fetched=len(rows),
            )

        sold_n = int(mp_meta.get("sold") or match_meta.get("sold_orders") or 0)
        _progress(
            f"источник очереди: Marketplace за период "
            f"(sold={mp_meta.get('sold', 0)}, отказ/дефект={mp_meta.get('returns', 0)}, "
            f"с КИЗ={mp_meta.get('with_kiz', 0)}); "
            f"Analytics строк={len(analytics_norms)}; "
            "FBO не пишем",
            fetched=len(rows),
        )

        inserted = 0
        updated = 0
        skipped = skipped_bad + int(mp_meta.get("without_kiz") or 0)
        skipped_not_fbs = 0
        skipped_not_sold = 0
        skipped_not_return = 0
        suppressed = 0
        withdraw_n = 0
        return_n = 0
        insert_errors = 0
        last_key = ""
        last_fiscal = ""
        sent_identities = _load_sent_cis_identities(
            repo, user_id=user_id, source_id=source_id
        )

        # Marketplace-first candidates (already status-filtered).
        candidates: list[dict[str, Any]] = list(mp_norms)
        seen_cis_op: set[tuple[int, str]] = set()
        for norm in candidates:
            cis = _normalize_cis_for_chz(str(norm.get("excise_short") or ""))
            if cis:
                seen_cis_op.add((int(norm.get("operation_type") or 0), cis))

        # Analytics extras that match FBS and are not already covered by meta.
        # Ineligible rows are still persisted as skipped so repair_requeue_* can
        # reopen them when local FBS / status catches up (no silent discard).
        elig_skipped_norms: list[tuple[dict[str, Any], str]] = []
        for norm in analytics_norms:
            elig = _norm_eligibility_skip(norm, fbs_index)
            if elig:
                skipped += 1
                if elig == SKIP_NOT_FBS:
                    skipped_not_fbs += 1
                elif elig == SKIP_NOT_SOLD:
                    skipped_not_sold += 1
                elif elig == SKIP_NOT_RETURN:
                    skipped_not_return += 1
                elig_skipped_norms.append((norm, elig))
                continue
            cis = _normalize_cis_for_chz(str(norm.get("excise_short") or ""))
            key = (int(norm.get("operation_type") or 0), cis)
            if cis and key in seen_cis_op:
                continue
            if cis:
                seen_cis_op.add(key)
            candidates.append(norm)

        _progress(
            f"к записи в очередь: {len(candidates)} "
            f"(Marketplace {len(mp_norms)} + Analytics доп. "
            f"{len(candidates) - len(mp_norms)}; "
            f"пропуск: без КИЗ {mp_meta.get('without_kiz', 0)}, "
            f"не FBS {skipped_not_fbs}, не sold {skipped_not_sold})",
            fetched=len(rows),
            skipped=skipped,
        )
        _check_sync_cancelled(run_id)

        with repo._connect() as conn:
            related_index = _prefetch_related_events_index(
                conn, repo, user_id=user_id, source_id=source_id
            )
            for i, norm in enumerate(candidates, start=1):
                if i % 200 == 0:
                    _check_sync_cancelled(run_id)
                    time.sleep(0)
                norm["raw_json"] = ""
                status, skip_reason = _initial_status(norm)
                try:
                    related = _related_from_index(related_index, norm=norm)
                    action, target = _resolve_sync_action(related, norm=norm)
                    if action == "insert":
                        idents = _cis_identity_keys(
                            srid=str(norm.get("srid") or ""),
                            rid=str(norm.get("rid") or ""),
                            excise_short=str(norm.get("excise_short") or ""),
                            operation_type=int(norm.get("operation_type") or 0),
                        )
                        if idents and (idents & sent_identities):
                            action = "suppress"
                            target = None
                    if action == "suppress":
                        suppressed += 1
                        skipped += 1
                        continue
                    if int(norm["operation_type"]) == OP_WITHDRAW:
                        withdraw_n += 1
                    else:
                        return_n += 1
                    if action == "upgrade" and target:
                        _upgrade_event_fiscal(
                            conn, repo, target=target, norm=norm, now=now
                        )
                        updated += 1
                        last_key = str(target.get("event_key") or norm["event_key"])
                        last_fiscal = (
                            str(norm.get("fiscal_dt") or target.get("fiscal_dt") or "")
                            or last_fiscal
                        )
                        continue

                    cur = conn.execute(
                        repo._sql(
                            """
                            INSERT INTO wb_kiz_circulation_events (
                                user_id, source_id, event_key, operation_type, srid, rid,
                                nm_id, barcode, excise_short, fiscal_doc_number, fiscal_dt,
                                fiscal_drive_number, price, currency_name, country_name,
                                status, skip_reason, raw_json, run_id, created_at, updated_at
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                            )
                            ON CONFLICT (user_id, source_id, event_key) DO UPDATE SET
                                price = COALESCE(EXCLUDED.price, wb_kiz_circulation_events.price),
                                currency_name = CASE
                                    WHEN EXCLUDED.currency_name <> '' THEN EXCLUDED.currency_name
                                    ELSE wb_kiz_circulation_events.currency_name
                                END,
                                country_name = CASE
                                    WHEN EXCLUDED.country_name <> '' THEN EXCLUDED.country_name
                                    ELSE wb_kiz_circulation_events.country_name
                                END,
                                fiscal_doc_number = CASE
                                    WHEN EXCLUDED.fiscal_doc_number <> '' THEN EXCLUDED.fiscal_doc_number
                                    ELSE wb_kiz_circulation_events.fiscal_doc_number
                                END,
                                fiscal_dt = CASE
                                    WHEN EXCLUDED.fiscal_dt <> '' THEN EXCLUDED.fiscal_dt
                                    ELSE wb_kiz_circulation_events.fiscal_dt
                                END,
                                fiscal_drive_number = CASE
                                    WHEN EXCLUDED.fiscal_drive_number <> '' THEN EXCLUDED.fiscal_drive_number
                                    ELSE wb_kiz_circulation_events.fiscal_drive_number
                                END,
                                updated_at = EXCLUDED.updated_at,
                                status = CASE
                                    WHEN wb_kiz_circulation_events.status IN ('submitted', 'accepted')
                                        THEN wb_kiz_circulation_events.status
                                    WHEN EXCLUDED.status = 'pending' THEN 'pending'
                                    ELSE wb_kiz_circulation_events.status
                                END,
                                skip_reason = CASE
                                    WHEN wb_kiz_circulation_events.status IN ('submitted', 'accepted')
                                        THEN wb_kiz_circulation_events.skip_reason
                                    WHEN EXCLUDED.status = 'pending' THEN EXCLUDED.skip_reason
                                    ELSE wb_kiz_circulation_events.skip_reason
                                END,
                                error_text = CASE
                                    WHEN wb_kiz_circulation_events.status IN ('submitted', 'accepted')
                                        THEN wb_kiz_circulation_events.error_text
                                    WHEN EXCLUDED.status = 'pending' THEN ''
                                    ELSE wb_kiz_circulation_events.error_text
                                END
                            RETURNING (xmax = 0) AS was_inserted
                            """
                        ),
                        (
                            user_id,
                            source_id,
                            norm["event_key"],
                            int(norm["operation_type"]),
                            norm["srid"],
                            norm["rid"],
                            norm["nm_id"],
                            norm["barcode"],
                            norm["excise_short"],
                            norm["fiscal_doc_number"],
                            norm["fiscal_dt"],
                            norm["fiscal_drive_number"],
                            norm["price"],
                            norm["currency_name"],
                            norm["country_name"],
                            status,
                            skip_reason,
                            norm["raw_json"],
                            run_id,
                            now,
                            now,
                        ),
                    )
                    ret = cur.fetchone()
                    was_inserted = True
                    if ret is not None:
                        rd = repo._row_to_dict(ret)
                        was_inserted = bool(rd.get("was_inserted"))
                    if was_inserted:
                        inserted += 1
                    else:
                        updated += 1
                    last_key = norm["event_key"]
                    last_fiscal = norm["fiscal_dt"] or last_fiscal
                    _index_related_event(related_index, {**norm, "status": status, "id": 0})
                except SyncCancelled:
                    raise
                except Exception as exc:
                    insert_errors += 1
                    skipped += 1
                    logger.exception(
                        "wb_kiz_circulation insert failed key=%s: %s",
                        norm.get("event_key"),
                        exc,
                    )
                    _append_log(
                        log, f"ошибка INSERT {norm.get('event_key', '')[:12]}…: {exc}"
                    )

                if i % 500 == 0 or i == len(candidates):
                    _progress(
                        f"запись {i}/{len(candidates)}: в очередь {inserted + updated} "
                        f"(вывод {withdraw_n}, возврат {return_n}), "
                        f"пропуск {skipped} (FBO/не FBS {skipped_not_fbs}, "
                        f"не sold {skipped_not_sold})",
                        fetched=len(rows),
                        inserted=inserted,
                        skipped=skipped,
                        withdraw_count=withdraw_n,
                        return_count=return_n,
                    )

            # Persist Analytics eligibility skips (not_fbs / not_sold / …).
            # ON CONFLICT DO NOTHING — never clobber an existing open/sent row.
            elig_persisted = 0
            for j, (norm, elig) in enumerate(elig_skipped_norms, start=1):
                if j % 200 == 0:
                    _check_sync_cancelled(run_id)
                    time.sleep(0)
                try:
                    cur = conn.execute(
                        repo._sql(
                            """
                            INSERT INTO wb_kiz_circulation_events (
                                user_id, source_id, event_key, operation_type, srid, rid,
                                nm_id, barcode, excise_short, fiscal_doc_number, fiscal_dt,
                                fiscal_drive_number, price, currency_name, country_name,
                                status, skip_reason, raw_json, run_id, created_at, updated_at
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                            )
                            ON CONFLICT (user_id, source_id, event_key) DO NOTHING
                            RETURNING (xmax = 0) AS was_inserted
                            """
                        ),
                        (
                            user_id,
                            source_id,
                            norm["event_key"],
                            int(norm["operation_type"]),
                            norm["srid"],
                            norm["rid"],
                            norm["nm_id"],
                            norm["barcode"],
                            norm["excise_short"],
                            norm["fiscal_doc_number"],
                            norm["fiscal_dt"],
                            norm["fiscal_drive_number"],
                            norm["price"],
                            norm["currency_name"],
                            norm["country_name"],
                            STATUS_SKIPPED,
                            elig,
                            "",
                            run_id,
                            now,
                            now,
                        ),
                    )
                    ret = cur.fetchone()
                    if ret is not None:
                        rd = repo._row_to_dict(ret)
                        if bool(rd.get("was_inserted")):
                            elig_persisted += 1
                except SyncCancelled:
                    raise
                except Exception as exc:
                    insert_errors += 1
                    logger.exception(
                        "wb_kiz_circulation elig-skip insert failed key=%s: %s",
                        norm.get("event_key"),
                        exc,
                    )
            if elig_persisted:
                _append_log(
                    log,
                    f"сохранено пропусков eligibility (not_fbs/not_sold): {elig_persisted}",
                )

            _check_sync_cancelled(run_id)
            conn.execute(
                repo._sql(
                    """
                    INSERT INTO wb_kiz_circulation_cursor (
                        user_id, source_id, last_date_to, last_event_key, last_fiscal_dt,
                        last_run_at, last_run_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id, source_id) DO UPDATE SET
                        last_date_to = EXCLUDED.last_date_to,
                        last_event_key = COALESCE(NULLIF(EXCLUDED.last_event_key, ''), wb_kiz_circulation_cursor.last_event_key),
                        last_fiscal_dt = COALESCE(NULLIF(EXCLUDED.last_fiscal_dt, ''), wb_kiz_circulation_cursor.last_fiscal_dt),
                        last_run_at = EXCLUDED.last_run_at,
                        last_run_id = EXCLUDED.last_run_id,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                (
                    user_id,
                    source_id,
                    date_to_s,
                    last_key,
                    last_fiscal,
                    now,
                    run_id,
                    now,
                ),
            )

        _check_sync_cancelled(run_id)
        _append_log(
            log,
            f"новых: {inserted}, обновлено: {updated}, пропуск: {skipped}"
            + (
                f" (не FBS: {skipped_not_fbs}, не sold: {skipped_not_sold})"
                if (skipped_not_fbs or skipped_not_sold)
                else ""
            )
            + (f", без дублей: {suppressed}" if suppressed else "")
            + (f", ошибок INSERT: {insert_errors}" if insert_errors else "")
            + f", вывод sold: {withdraw_n}, возврат ПВЗ: {return_n}",
        )
        _append_log(
            log,
            f"период сохранён → {date_to_s}"
            + (f" / {last_key[:12]}…" if last_key else ""),
        )
        _finish_run(
            repo,
            run_id=run_id,
            status="ok",
            log=log,
            fetched=len(rows),
            inserted=inserted,
            skipped=skipped,
            withdraw_count=withdraw_n,
            return_count=return_n,
        )
        return {
            "ok": True,
            "run_id": run_id,
            "date_from": date_from_s,
            "date_to": date_to_s,
            "fetched": len(rows),
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "skipped_not_fbs": skipped_not_fbs,
            "skipped_not_sold": skipped_not_sold,
            "skipped_not_return": skipped_not_return,
            "insert_errors": insert_errors,
            "withdraw_count": withdraw_n,
            "return_count": return_n,
            "log": "\n".join(log),
            "cursor": get_cursor(repo, user_id=user_id, source_id=source_id),
        }
    except SyncCancelled as exc:
        _append_log(log, str(exc) or "Синхронизация остановлена")
        _finish_run(
            repo,
            run_id=run_id,
            status="cancelled",
            log=log,
            error_text="Остановлено пользователем",
        )
        return {
            "ok": False,
            "cancelled": True,
            "run_id": run_id,
            "date_from": date_from_s,
            "date_to": date_to_s,
            "log": "\n".join(log),
            "error": "Остановлено пользователем",
        }
    except Exception as exc:
        # Preserve prior WB-specific finish when already written by callers.
        if run_id > 0:
            _append_log(log, f"Ошибка: {exc}")
            try:
                _finish_run(
                    repo,
                    run_id=run_id,
                    status="error",
                    log=log,
                    error_text=str(exc),
                )
            except Exception:
                logger.exception("finish error run after sync failure failed")
        raise
    finally:
        if run_id > 0:
            _clear_sync_cancel(run_id)

def _finish_run(
    repo: ReviewRepository,
    *,
    run_id: int,
    status: str,
    log: list[str],
    fetched: int = 0,
    inserted: int = 0,
    skipped: int = 0,
    withdraw_count: int = 0,
    return_count: int = 0,
    error_text: str = "",
) -> None:
    if run_id <= 0:
        return
    now = datetime.now(timezone.utc).isoformat()
    finished_at = None if str(status) == "running" else now
    with repo._connect() as conn:
        conn.execute(
            repo._sql(
                """
                UPDATE wb_kiz_circulation_runs SET
                    status = ?, fetched = ?, inserted = ?, skipped = ?,
                    withdraw_count = ?, return_count = ?, error_text = ?,
                    log_text = ?, finished_at = COALESCE(?, finished_at)
                WHERE id = ?
                """
            ),
            (
                status,
                fetched,
                inserted,
                skipped,
                withdraw_count,
                return_count,
                str(error_text or "")[:2000],
                "\n".join(log)[:50000],
                finished_at,
                run_id,
            ),
        )


def _prefetch_related_events_index(
    conn: Any,
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
) -> dict[tuple[int, str, str], list[dict[str, Any]]]:
    """One-shot load of open events for sync (avoids N+1 SELECTs per row)."""
    # Skip bulk eligibility rejects (FBO / not sold) — they bloat the index after
    # large Analytics pulls and are never upgrade targets for new FBS rows.
    elig = sorted(_ELIGIBILITY_SKIP_REASONS)
    elig_ph = ", ".join("?" for _ in elig)
    rows = conn.execute(
        repo._sql(
            f"""
            SELECT id, event_key, status, srid, rid, excise_short, operation_type,
                   fiscal_doc_number, fiscal_dt, fiscal_drive_number,
                   price, currency_name, country_name, skip_reason
            FROM wb_kiz_circulation_events
            WHERE user_id = ? AND source_id = ?
              AND status IN ('pending', 'ready', 'error', 'skipped')
              AND COALESCE(excise_short, '') <> ''
              AND NOT (status = 'skipped' AND skip_reason IN ({elig_ph}))
            """
        ),
        (user_id, source_id, *elig),
    ).fetchall()
    index: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
    for r in rows:
        d = repo._row_to_dict(r)
        _index_related_event(index, d)
    return index


def _index_related_event(
    index: dict[tuple[int, str, str], list[dict[str, Any]]],
    ev: dict[str, Any],
) -> None:
    op = int(ev.get("operation_type") or 0)
    excise = str(ev.get("excise_short") or "").strip()
    if not excise or op not in {OP_WITHDRAW, OP_RETURN}:
        return
    anchors: set[str] = set()
    for raw in (ev.get("srid"), ev.get("rid")):
        anchors.update(_rid_match_keys(raw))
        raw_s = str(raw or "").strip()
        if raw_s:
            anchors.add(raw_s)
    if not anchors:
        anchors = {""}
    for anchor in anchors:
        key = (op, excise, anchor)
        bucket = index.setdefault(key, [])
        # Prefer keeping real DB rows; skip duplicates by event_key.
        ek = str(ev.get("event_key") or "")
        if ek and any(str(x.get("event_key") or "") == ek for x in bucket):
            continue
        bucket.append(ev)


def _related_from_index(
    index: dict[tuple[int, str, str], list[dict[str, Any]]],
    *,
    norm: dict[str, Any],
) -> list[dict[str, Any]]:
    op = int(norm.get("operation_type") or 0)
    excise = str(norm.get("excise_short") or "").strip()
    if not excise or op not in {OP_WITHDRAW, OP_RETURN}:
        return []
    anchors: set[str] = set()
    for raw in (norm.get("srid"), norm.get("rid")):
        anchors.update(_rid_match_keys(raw))
        raw_s = str(raw or "").strip()
        if raw_s:
            anchors.add(raw_s)
    if not anchors:
        anchors = {""}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in anchors:
        for ev in index.get((op, excise, anchor), []):
            ek = str(ev.get("event_key") or "") or str(ev.get("id") or "")
            if ek in seen:
                continue
            seen.add(ek)
            out.append(ev)
    return out


def _sync_has_live_worker(run_id: int) -> bool:
    """True when this process still holds a cancel Event for the run."""
    rid = int(run_id or 0)
    if rid <= 0:
        return False
    with _SYNC_CANCEL_LOCK:
        return rid in _SYNC_CANCEL


def abandon_orphan_excise_sync_runs(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int | None = None,
    grace_seconds: int = 20,
) -> list[int]:
    """Close DB ``running`` rows with no live worker (restart / hung sync).

    After uvicorn restart in-memory cancel flags are gone, but rows may stay
    ``running`` forever and block «Ежедневный вывод».
    """
    ensure_kiz_circulation_tables(repo)
    params: list[Any] = [user_id]
    source_sql = ""
    if source_id is not None and int(source_id) > 0:
        source_sql = " AND source_id = ?"
        params.append(int(source_id))
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT id, source_id, created_at, log_text
                FROM wb_kiz_circulation_runs
                WHERE user_id = ? AND status = 'running'{source_sql}
                ORDER BY id ASC
                """
            ),
            tuple(params),
        ).fetchall()
    abandoned: list[int] = []
    now = datetime.now(timezone.utc)
    for raw in rows:
        row = repo._row_to_dict(raw)
        rid = int(row.get("id") or 0)
        if rid <= 0:
            continue
        if _sync_has_live_worker(rid):
            continue
        created_raw = str(row.get("created_at") or "").strip()
        age_ok = True
        if created_raw:
            try:
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_ok = (now - created).total_seconds() >= max(0, int(grace_seconds))
            except Exception:
                age_ok = True
        if not age_ok:
            # Fresh row: worker may still be attaching the cancel Event.
            continue
        log = str(row.get("log_text") or "")
        line = (
            f"[{datetime.now(MSK).strftime('%H:%M:%S')}] "
            "прогон закрыт автоматически: процесс выгрузки уже не работает "
            "(рестарт сервера или зависание). Можно запускать снова."
        )
        if line not in log:
            log = (log + ("\n" if log else "") + line)[:50000]
        _finish_run(
            repo,
            run_id=rid,
            status="cancelled",
            log=log.split("\n") if log else [line],
            error_text="Прервано (нет активного процесса)",
        )
        abandoned.append(rid)
    return abandoned


def find_active_excise_sync_run(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> dict[str, Any] | None:
    """Return the latest live running sync for this source, if any."""
    ensure_kiz_circulation_tables(repo)
    abandon_orphan_excise_sync_runs(
        repo, user_id=user_id, source_id=source_id
    )
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql(
                """
                SELECT id, status, date_from, date_to, log_text, created_at
                FROM wb_kiz_circulation_runs
                WHERE user_id = ? AND source_id = ? AND status = 'running'
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            (user_id, source_id),
        ).fetchone()
    if not row:
        return None
    data = repo._row_to_dict(row)
    rid = int(data.get("id") or 0)
    # Prefer treating DB-only leftovers as inactive even inside grace window
    # when this process has never seen the run (typical after deploy restart).
    if rid > 0 and not _sync_has_live_worker(rid):
        created_raw = str(data.get("created_at") or "").strip()
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            # Older than grace → should have been abandoned; force-close.
            if (datetime.now(timezone.utc) - created).total_seconds() >= 20:
                abandon_orphan_excise_sync_runs(
                    repo, user_id=user_id, source_id=source_id, grace_seconds=0
                )
                return None
        except Exception:
            abandon_orphan_excise_sync_runs(
                repo, user_id=user_id, source_id=source_id, grace_seconds=0
            )
            return None
    return data


def cancel_excise_sync_run(
    repo: ReviewRepository, *, user_id: int, run_id: int
) -> dict[str, Any]:
    """Request stop of a running sync; immediately close zombie runs."""
    ensure_kiz_circulation_tables(repo)
    rid = int(run_id or 0)
    if rid <= 0:
        raise ValueError("Укажите run_id")
    run = get_run(repo, user_id=user_id, run_id=rid)
    if not run:
        raise ValueError("Прогон не найден")
    status = str(run.get("status") or "")
    if status in {"ok", "error", "cancelled"}:
        return {
            "ok": True,
            "run_id": rid,
            "status": status,
            "already_finished": True,
            "message": f"Прогон уже завершён ({status})",
        }
    live = _sync_has_live_worker(rid)
    log = str(run.get("log_text") or "")
    if live:
        request_cancel_excise_sync(rid)
        line = (
            f"[{datetime.now(MSK).strftime('%H:%M:%S')}] "
            "стоп: запрошена остановка пользователем"
        )
        if line not in log:
            log = (log + ("\n" if log else "") + line)[:50000]
        with repo._connect() as conn:
            conn.execute(
                repo._sql(
                    """
                    UPDATE wb_kiz_circulation_runs SET
                        log_text = ?,
                        error_text = CASE
                            WHEN status = 'running' THEN 'Остановка…'
                            ELSE error_text
                        END
                    WHERE id = ? AND user_id = ? AND status = 'running'
                    """
                ),
                (log, rid, user_id),
            )
        return {
            "ok": True,
            "run_id": rid,
            "status": "cancelling",
            "already_finished": False,
            "message": "Остановка запрошена",
        }

    line = (
        f"[{datetime.now(MSK).strftime('%H:%M:%S')}] "
        "стоп: процесс выгрузки уже не работал — прогон закрыт"
    )
    if line not in log:
        log = (log + ("\n" if log else "") + line)[:50000]
    _finish_run(
        repo,
        run_id=rid,
        status="cancelled",
        log=log.split("\n") if log else [line],
        error_text="Остановлено (процесс уже не работал)",
    )
    return {
        "ok": True,
        "run_id": rid,
        "status": "cancelled",
        "already_finished": True,
        "message": "Зависший прогон закрыт — можно запускать снова",
    }


def create_excise_sync_run(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    """Create a running sync row so the UI can poll before WB returns."""
    ensure_kiz_circulation_tables(repo)
    abandon_orphan_excise_sync_runs(
        repo, user_id=user_id, source_id=source_id, grace_seconds=0
    )
    active = find_active_excise_sync_run(
        repo, user_id=user_id, source_id=source_id
    )
    if active and _sync_has_live_worker(int(active.get("id") or 0)):
        # Re-attach instead of hard-failing: UI can keep polling the live run.
        rid = int(active.get("id") or 0)
        period = resolve_excise_period(date_from=date_from, date_to=date_to)
        return {
            "ok": True,
            "async": True,
            "status": "running",
            "run_id": rid,
            "already_running": True,
            "date_from": str(period["date_from"]),
            "date_to": str(period["date_to"]),
            "days": int(period["days"] or 0),
            "log": str(active.get("log_text") or "").strip()
            or (
                f"WB: выгрузка #{rid} уже идёт — подключаюсь к логу…"
            ),
        }
    if active:
        # Defensive: DB row without live worker — close and continue.
        abandon_orphan_excise_sync_runs(
            repo, user_id=user_id, source_id=source_id, grace_seconds=0
        )
    period = resolve_excise_period(date_from=date_from, date_to=date_to)
    date_from_s = str(period["date_from"])
    date_to_s = str(period["date_to"])
    now = datetime.now(timezone.utc).isoformat()
    log = [
        f"WB: выгрузка за выбранные даты {date_from_s}…{date_to_s} "
        f"({period['days']} дн., фоновый режим)"
    ]
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql(
                """
                INSERT INTO wb_kiz_circulation_runs (
                    user_id, source_id, date_from, date_to, stage, status,
                    created_at, log_text
                ) VALUES (?, ?, ?, ?, 'wb_sync', 'running', ?, ?)
                RETURNING id
                """
            ),
            (
                user_id,
                source_id,
                date_from_s,
                date_to_s,
                now,
                "\n".join(log)[:50000],
            ),
        ).fetchone()
        run_id = int(repo._row_to_dict(row).get("id") or 0) if row else 0
    if run_id > 0:
        _register_sync_cancel(run_id)
    return {
        "ok": True,
        "async": True,
        "status": "running",
        "run_id": run_id,
        "date_from": date_from_s,
        "date_to": date_to_s,
        "days": int(period["days"] or 0),
        "log": "\n".join(log),
    }


def list_events(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    status: str = "",
    operation_type: int | None = None,
    limit: int = 200,
    order: str = "desc",
    api_key: str = "",
    hydrate_orders: bool = False,
    refresh_statuses: bool = False,
) -> list[dict[str, Any]]:
    ensure_kiz_circulation_tables(repo)
    lim = max(1, min(int(limit or 200), 5000))
    clauses = ["user_id = ?", "source_id = ?"]
    params: list[Any] = [user_id, source_id]
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    else:
        # Hide bulk eligibility skips (FBO / not sold / not cancelled) by default.
        ph = ", ".join("?" for _ in sorted(_ELIGIBILITY_SKIP_REASONS))
        clauses.append(f"NOT (status = ? AND skip_reason IN ({ph}))")
        params.append(STATUS_SKIPPED)
        params.extend(sorted(_ELIGIBILITY_SKIP_REASONS))
    if operation_type in {OP_WITHDRAW, OP_RETURN}:
        clauses.append("operation_type = ?")
        params.append(int(operation_type))
    params.append(lim)
    order_sql = (
        "ORDER BY fiscal_dt ASC NULLS FIRST, id ASC"
        if str(order or "").lower() == "asc"
        else "ORDER BY fiscal_dt DESC NULLS LAST, id DESC"
    )
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                f"SELECT * FROM wb_kiz_circulation_events WHERE {' AND '.join(clauses)} "
                f"{order_sql} LIMIT ?"
            ),
            tuple(params),
        ).fetchall()
    out = []
    participant_inn = ""
    try:
        participant_inn = str(
            get_chz_settings(repo, user_id=user_id).get("participant_inn") or ""
        ).strip()
    except Exception:
        participant_inn = ""
    for r in rows:
        d = repo._row_to_dict(r)
        d.pop("raw_json", None)
        cis_st = str(d.get("cis_status") or "").strip()
        owner = str(d.get("cis_owner_inn") or "").strip()
        label, kind = cis_display_for_row(
            status=cis_st,
            owner_inn=owner,
            participant_inn=participant_inn,
        )
        d["cis_status_label"] = label
        d["cis_status_kind"] = kind
        d["cis_transferred"] = bool(
            cis_owner_is_foreign(owner_inn=owner, participant_inn=participant_inn)
        )
        out.append(d)
    _attach_order_ids_to_events(
        repo,
        user_id=user_id,
        source_id=source_id,
        events=out,
        api_key=api_key,
        hydrate=bool(hydrate_orders and api_key),
        refresh_statuses=bool(refresh_statuses and api_key),
    )
    return out


def _attach_order_ids_to_events(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    events: list[dict[str, Any]],
    api_key: str = "",
    hydrate: bool = False,
    refresh_statuses: bool = False,
) -> None:
    """Attach numeric FBS ``order_id`` + Marketplace status via srid/rid join."""
    if not events:
        return
    from . import wb_fbs as wb_fbs_mod

    keys: list[str] = []
    for ev in events:
        srid = str(ev.get("srid") or "").strip()
        rid = str(ev.get("rid") or "").strip()
        if srid:
            keys.append(srid)
        if rid and rid != srid:
            keys.append(rid)

    do_refresh = bool(refresh_statuses)
    if hydrate and api_key and keys:
        # Prefer light join first; full Marketplace/archive download only for
        # still-unresolved srids (prepare used to always hydrate → nginx 504).
        try:
            linked = wb_fbs_mod.order_ids_by_srids(
                repo, user_id=user_id, source_id=source_id, srids=keys
            )
            missing = [k for k in keys if k not in linked]
            if missing:
                wb_fbs_mod.hydrate_orders_for_kiz_srids(
                    repo,
                    user_id=user_id,
                    source_id=source_id,
                    srids=missing,
                    api_key=api_key,
                )
            else:
                # Already linked — refresh wbStatus only (sold / отказ).
                do_refresh = True
        except Exception as exc:
            logger.warning("hydrate_orders_for_kiz_srids failed: %s", exc)

    by_srid = wb_fbs_mod.order_ids_by_srids(
        repo, user_id=user_id, source_id=source_id, srids=keys
    )
    order_ids: list[int] = []
    for ev in events:
        srid = str(ev.get("srid") or "").strip()
        rid = str(ev.get("rid") or "").strip()
        oid = by_srid.get(srid) or by_srid.get(rid) or None
        if oid:
            try:
                oid_i = int(oid)
            except (TypeError, ValueError):
                oid_i = 0
            # WB order ids are normally >0; keep any non-zero id (never treat as missing).
            ev["order_id"] = oid_i if oid_i != 0 else None
            if oid_i != 0:
                order_ids.append(oid_i)
        else:
            ev["order_id"] = None
        ev["order_status_label"] = ""
        ev["order_wb_status"] = ""
        ev["order_supplier_status"] = ""
        ev["order_cancel_reason"] = ""

    # Fast path: refresh Marketplace statuses for already-linked orders only
    # (no archive download — safe for list/events).
    if do_refresh and api_key and order_ids:
        try:
            wb_fbs_mod.refresh_order_statuses_light(
                repo,
                user_id=user_id,
                source_id=source_id,
                order_ids=order_ids,
                api_key=api_key,
            )
        except Exception as exc:
            logger.warning("refresh_order_statuses_light failed: %s", exc)

    status_map = wb_fbs_mod.load_order_status_map(
        repo, user_id=user_id, source_id=source_id, order_ids=order_ids
    )
    for ev in events:
        oid = ev.get("order_id")
        if not oid:
            continue
        st = status_map.get(int(oid)) or {}
        ev["order_status_label"] = str(st.get("order_status_label") or "").strip()
        ev["order_wb_status"] = str(st.get("wb_status") or "").strip()
        ev["order_supplier_status"] = str(st.get("supplier_status") or "").strip()
        ev["order_cancel_reason"] = str(st.get("cancel_reason_label") or "").strip()

    # Backfill price for OTHER / missing Analytics price from local FBS orders
    # (and Marketplace API when columns/raw_json still empty).
    need_price = [
        int(ev["order_id"])
        for ev in events
        if ev.get("order_id") not in (None, "", 0) and ev.get("price") is None
    ]
    if need_price:
        try:
            if str(api_key or "").strip():
                price_map = wb_fbs_mod.fill_missing_order_prices(
                    repo,
                    user_id=user_id,
                    source_id=source_id,
                    order_ids=need_price,
                    api_key=api_key,
                )
            else:
                price_map = wb_fbs_mod.load_order_price_map(
                    repo,
                    user_id=user_id,
                    source_id=source_id,
                    order_ids=need_price,
                )
        except Exception as exc:
            logger.warning("order price backfill failed: %s", exc)
            price_map = {}
        filled_keys: list[tuple[str, float, str]] = []
        for ev in events:
            if ev.get("price") is not None:
                continue
            try:
                oid_i = int(ev.get("order_id") or 0)
            except (TypeError, ValueError):
                continue
            info = price_map.get(oid_i) or {}
            if info.get("price_rub") is None:
                continue
            ev["price"] = info["price_rub"]
            if not str(ev.get("currency_name") or "").strip():
                ev["currency_name"] = str(info.get("currency_name") or "RUB")
            ek = str(ev.get("event_key") or "").strip()
            if ek:
                filled_keys.append(
                    (
                        ek,
                        float(ev["price"]),
                        str(ev.get("currency_name") or "RUB"),
                    )
                )
        if filled_keys:
            try:
                _persist_event_prices(
                    repo,
                    user_id=user_id,
                    source_id=source_id,
                    filled=filled_keys,
                )
            except Exception as exc:
                logger.warning("persist event prices failed: %s", exc)


def _event_is_sold_for_chz(ev: dict[str, Any]) -> bool:
    """Withdraw to CHZ only when Marketplace wbStatus is sold («выкуплен»)."""
    return str(ev.get("order_wb_status") or "").strip().lower() == "sold"


def _event_is_cancelled_for_chz(ev: dict[str, Any]) -> bool:
    """Return-to-circulation only for отказные / cancelled Marketplace statuses."""
    from . import wb_fbs as wb_fbs_mod

    return bool(
        wb_fbs_mod._is_cancelled_status(
            supplier_status=ev.get("order_supplier_status") or "",
            wb_status=ev.get("order_wb_status") or "",
        )
    )


def _withdraw_not_sold_reason(ev: dict[str, Any]) -> str:
    """Empty if withdraw is allowed; otherwise sticky skip code (fail closed)."""
    if int(ev.get("operation_type") or 0) != OP_WITHDRAW:
        return ""
    oid = ev.get("order_id")
    try:
        oid_i = int(oid) if oid is not None else 0
    except (TypeError, ValueError):
        oid_i = 0
    if oid_i <= 0:
        return SKIP_NOT_FBS
    if _event_is_sold_for_chz(ev):
        return ""
    return SKIP_NOT_SOLD


def _return_not_cancelled_reason(ev: dict[str, Any]) -> str:
    """Empty if return-to-circulation is allowed.

    Analytics op=2 already means return / PVZ refuse — only require FBS link.
    Pre-delivery cancels are not op=2 and never reach prepare as returns.
    """
    if int(ev.get("operation_type") or 0) != OP_RETURN:
        return ""
    oid = ev.get("order_id")
    try:
        oid_i = int(oid) if oid is not None else 0
    except (TypeError, ValueError):
        oid_i = 0
    if oid_i <= 0:
        return SKIP_NOT_FBS
    return ""


def list_events_for_chz(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    limit: int = PREPARE_EVENT_LIMIT,
    event_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Events eligible for CHZ submit: pending + recoverable error + failed submitted.

    Includes withdraw-without-fiscal (``no_fiscal``) as pending/skipped-legacy —
    they go out as LK_RECEIPT with primary document OTHER.

    When ``event_keys`` is set, only those keys are considered (still must be eligible).
    """
    ensure_kiz_circulation_tables(repo)
    key_filter = [
        str(k).strip()
        for k in (event_keys or [])
        if str(k or "").strip()
    ]
    # Cap IN-list size; UI selection is practical well below this.
    if key_filter:
        key_filter = key_filter[:5000]
    lim = max(1, min(int(limit or PREPARE_EVENT_LIMIT), 5000))
    if key_filter:
        lim = min(max(lim, len(key_filter)), 5000)
    fail_list = sorted(CHZ_STATUS_FAILED)
    fail_placeholders = ", ".join(["?"] * len(fail_list)) if fail_list else "NULL"
    params: list[Any] = [
        user_id,
        source_id,
        SKIP_NO_FISCAL,
        STATUS_SKIPPED,
        OP_WITHDRAW,
        SKIP_NO_FISCAL,
        STATUS_SUBMITTED,
        *fail_list,
    ]
    key_sql = ""
    if key_filter:
        key_placeholders = ", ".join(["?"] * len(key_filter))
        key_sql = f" AND event_key IN ({key_placeholders}) "
        params.extend(key_filter)
    params.append(lim)
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                f"""
                SELECT * FROM wb_kiz_circulation_events
                WHERE user_id = ? AND source_id = ?
                  AND COALESCE(excise_short, '') <> ''
                  AND (
                    status IN ('pending', 'ready')
                    OR (
                      status = 'error'
                      AND NOT (operation_type = 1 AND skip_reason = ?)
                    )
                    OR (
                      status = ?
                      AND operation_type = ?
                      AND skip_reason = ?
                    )
                    OR (
                      status = ?
                      AND COALESCE(chz_doc_id, '') = ''
                    )
                    OR (
                      status = 'submitted'
                      AND UPPER(COALESCE(chz_status, '')) IN ({fail_placeholders})
                    )
                  )
                  {key_sql}
                ORDER BY
                  CASE WHEN fiscal_dt IS NULL OR fiscal_dt = '' THEN 1 ELSE 0 END,
                  fiscal_dt ASC,
                  id ASC
                LIMIT ?
                """
            ),
            tuple(params),
        ).fetchall()
    out = []
    for r in rows:
        d = repo._row_to_dict(r)
        d.pop("raw_json", None)
        # Drop legacy Russian no-fiscal withdraw errors (not matched by ASCII code).
        if (
            int(d.get("operation_type") or 0) == OP_WITHDRAW
            and str(d.get("status") or "") == STATUS_ERROR
            and _is_no_fiscal_reason(str(d.get("skip_reason") or ""))
        ):
            continue
        out.append(d)
    return out


def _load_sent_cis_identities(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> set[tuple[str, str, int]]:
    """Identities already in CHZ — must not be sent again.

    Sources:
    - live events: accepted, or submitted with ``chz_doc_id``
    - forever slim registry (survives 6-month event purge)

    Keys are fold-expanded (mid-token / stem) so Marketplace ``.0.0`` and
    Analytics ``.1.0`` variants of the same order match.
    """
    ensure_kiz_circulation_tables(repo)
    out: set[tuple[str, str, int]] = set()
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                """
                SELECT srid, rid, excise_short, operation_type
                FROM wb_kiz_circulation_events
                WHERE user_id = ? AND source_id = ?
                  AND COALESCE(excise_short, '') <> ''
                  AND (
                    status = ?
                    OR (
                      status = ?
                      AND COALESCE(chz_doc_id, '') <> ''
                    )
                  )
                """
            ),
            (user_id, source_id, STATUS_ACCEPTED, STATUS_SUBMITTED),
        ).fetchall()
        for r in rows:
            d = repo._row_to_dict(r)
            out |= _cis_identity_keys(
                srid=str(d.get("srid") or ""),
                rid=str(d.get("rid") or ""),
                excise_short=str(d.get("excise_short") or ""),
                operation_type=int(d.get("operation_type") or 0),
            )
        reg = conn.execute(
            repo._sql(
                """
                SELECT anchor, excise_short, operation_type
                FROM wb_kiz_sent_cis
                WHERE user_id = ? AND source_id = ?
                  AND COALESCE(excise_short, '') <> ''
                """
            ),
            (user_id, source_id),
        ).fetchall()
        for r in reg:
            d = repo._row_to_dict(r)
            cis = str(d.get("excise_short") or "").strip()
            op = int(d.get("operation_type") or 0)
            anchor = str(d.get("anchor") or "").strip()
            if not cis:
                continue
            if anchor:
                out.add((anchor, cis, op))
                for fold in _rid_match_keys(anchor):
                    out.add((fold, cis, op))
            else:
                out.add(("", cis, op))
    return out


def _close_deduped_prepare_events(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    skipped: list[dict[str, Any]],
) -> int:
    """Persist terminal prepare skips so the oldest-first queue does not clog."""
    by_reason: dict[str, list[str]] = {}
    for row in skipped:
        reason = str(row.get("skip_reason") or "").strip()
        if reason not in _TERMINAL_SKIP_REASONS:
            continue
        key = str(row.get("event_key") or "").strip()
        if not key:
            continue
        by_reason.setdefault(reason, []).append(key)
    if not by_reason:
        return 0
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    closed = 0
    with repo._connect() as conn:
        for reason, keys in by_reason.items():
            uniq = sorted(set(keys))
            for chunk in _chunked(uniq, 200):
                ph = ", ".join("?" for _ in chunk)
                cur = conn.execute(
                    repo._sql(
                        f"""
                        UPDATE wb_kiz_circulation_events
                        SET status = ?, skip_reason = ?, updated_at = ?
                        WHERE user_id = ? AND source_id = ?
                          AND event_key IN ({ph})
                          AND status NOT IN (?, ?)
                        """
                    ),
                    (
                        STATUS_SKIPPED,
                        reason,
                        now,
                        user_id,
                        source_id,
                        *chunk,
                        STATUS_SUBMITTED,
                        STATUS_ACCEPTED,
                    ),
                )
                closed += int(getattr(cur, "rowcount", 0) or 0)
    return closed


def preclose_empty_cis_events(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> int:
    """Sticky-close open rows with empty CIS so they cannot HOL-block prepare."""
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    with repo._connect() as conn:
        cur = conn.execute(
            repo._sql(
                """
                UPDATE wb_kiz_circulation_events
                SET status = ?,
                    skip_reason = ?,
                    updated_at = ?
                WHERE user_id = ? AND source_id = ?
                  AND status IN (?, ?, ?)
                  AND COALESCE(excise_short, '') = ''
                """
            ),
            (
                STATUS_SKIPPED,
                SKIP_EMPTY_CIS,
                now,
                user_id,
                source_id,
                STATUS_PENDING,
                STATUS_READY,
                STATUS_ERROR,
            ),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


def _dedupe_events_for_prepare(
    events: list[dict[str, Any]],
    *,
    sent_identities: set[tuple[str, str, int]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop already-sent CIS and collapse fiscal/no-fiscal duplicates in one batch."""
    kept: list[dict[str, Any]] = []
    kept_keys: list[set[tuple[str, str, int]]] = []
    skipped: list[dict[str, Any]] = []
    for ev in events:
        keys = _cis_identity_keys(
            srid=str(ev.get("srid") or ""),
            rid=str(ev.get("rid") or ""),
            excise_short=str(ev.get("excise_short") or ""),
            operation_type=int(ev.get("operation_type") or 0),
        )
        if not any(k[1] for k in keys):
            skipped.append({**ev, "skip_reason": SKIP_EMPTY_CIS})
            continue
        if keys & sent_identities:
            skipped.append({**ev, "skip_reason": "already_sent"})
            continue
        match_i: int | None = None
        for i, prev_keys in enumerate(kept_keys):
            if keys & prev_keys:
                match_i = i
                break
        if match_i is None:
            kept.append(ev)
            kept_keys.append(keys)
            continue
        prev = kept[match_i]
        # Prefer the variant with fiscal receipt.
        if _event_has_fiscal(ev) and not _event_has_fiscal(prev):
            skipped.append({**prev, "skip_reason": "duplicate_nofiscal"})
            kept[match_i] = ev
            kept_keys[match_i] = keys | prev_keys
        else:
            skipped.append({**ev, "skip_reason": "duplicate"})
            kept_keys[match_i] = keys | prev_keys
    return kept, skipped


def reconcile_submitted_with_chz(
    repo: ReviewRepository,
    client: ChzTrueApiClient,
    *,
    user_id: int,
    source_id: int,
    limit: int = 500,
) -> dict[str, int]:
    """Poll CHZ for in-flight submitted docs and update local statuses."""
    ensure_kiz_circulation_tables(repo)
    # First heal rows where chz_status is already terminal but local status lagged.
    healed = heal_submitted_terminal_statuses(
        repo, user_id=user_id, source_id=source_id
    )
    lim = max(1, min(int(limit or 500), 2000))
    with repo._connect() as conn:
        rows = conn.execute(
            repo._sql(
                """
                SELECT event_key, chz_doc_id, chz_status, status
                FROM wb_kiz_circulation_events
                WHERE user_id = ? AND source_id = ?
                  AND status = ?
                  AND COALESCE(chz_doc_id, '') <> ''
                ORDER BY updated_at ASC NULLS FIRST, id ASC
                LIMIT ?
                """
            ),
            (user_id, source_id, STATUS_SUBMITTED, lim),
        ).fetchall()
    by_doc: dict[str, list[str]] = {}
    for r in rows:
        d = repo._row_to_dict(r)
        doc_id = str(d.get("chz_doc_id") or "").strip()
        key = str(d.get("event_key") or "").strip()
        if not doc_id or not key:
            continue
        st = str(d.get("chz_status") or "").strip().upper()
        # Already know terminal outcome locally — apply without another CHZ call.
        if st in CHZ_STATUS_FAILED:
            apply_chz_doc_status(
                repo,
                user_id=user_id,
                source_id=source_id,
                event_keys=[key],
                chz_doc_id=doc_id,
                chz_status=st,
                error_text=str(d.get("error_text") or "") or f"ЧЗ: {st}",
            )
            continue
        if st in CHZ_STATUS_SUCCESS:
            apply_chz_doc_status(
                repo,
                user_id=user_id,
                source_id=source_id,
                event_keys=[key],
                chz_doc_id=doc_id,
                chz_status=st,
            )
            continue
        by_doc.setdefault(doc_id, []).append(key)

    checked = 0
    accepted = 0
    failed = 0
    api_errors = 0
    api_error_samples: list[str] = []
    for doc_id, keys in by_doc.items():
        checked += 1
        try:
            info = client.document_info(doc_id)
            err = extract_chz_doc_errors(info)
            chz_status = extract_chz_doc_status(info) or ""
            # Array/unwrap misses used to leave status empty → stuck on submitted
            # even when CHZ already returned commonErrors / CHECKED_NOT_OK.
            if not chz_status and err:
                chz_status = "CHECKED_NOT_OK"
            elif not chz_status:
                chz_status = "submitted"
            final = apply_chz_doc_status(
                repo,
                user_id=user_id,
                source_id=source_id,
                event_keys=keys,
                chz_doc_id=doc_id,
                chz_status=chz_status,
                error_text=err,
            )
            if final == STATUS_ACCEPTED:
                accepted += 1
            elif final == STATUS_ERROR:
                failed += 1
                logger.warning(
                    "CHZ reconcile doc %s → %s (%s events): %s",
                    doc_id,
                    chz_status,
                    len(keys),
                    (err or chz_status)[:500],
                )
            else:
                logger.info(
                    "CHZ reconcile doc %s still in progress: %s",
                    doc_id,
                    chz_status or "submitted",
                )
        except Exception as exc:
            api_errors += 1
            sample = f"{doc_id}: {exc}"
            if len(api_error_samples) < 3:
                api_error_samples.append(sample[:240])
            logger.warning("CHZ reconcile doc %s failed: %s", doc_id, exc)
    return {
        "docs_checked": checked,
        "accepted": accepted,
        "failed": failed,
        "api_errors": api_errors,
        "api_error_samples": api_error_samples,
        "healed": int(healed.get("healed") or 0),
        "events": sum(len(v) for v in by_doc.values()),
    }


# True API CIS status → short RU label for the Вывод КИЗ table.
_CIS_STATUS_RU: dict[str, str] = {
    "INTRODUCED": "В обороте",
    "RETIRED": "Выведен",
    "WITHDRAWN": "Выведен",
    "WRITTEN_OFF": "Списан",
    "APPLIED": "Нанесён",
    "EMITTED": "Эмитирован",
    "DISAGGREGATION": "Расформирован",
    "DISAGGREGATED": "Расформирован",
    "APPLIED_NOT_PAID": "Нанесён (не оплачен)",
}

# Buckets for CSS / filters (document status is separate).
_CIS_STATUS_KIND_IN = frozenset({"INTRODUCED"})
_CIS_STATUS_KIND_OUT = frozenset({"RETIRED", "WITHDRAWN", "WRITTEN_OFF"})
_CIS_STATUS_KIND_PRE = frozenset({"EMITTED", "APPLIED", "APPLIED_NOT_PAID"})

# True API /cises/info batch size (docs allow up to ~1000; keep smaller).
CIS_INFO_CHUNK = 100


def cis_status_label(status: str) -> str:
    raw = str(status or "").strip()
    if not raw:
        return ""
    return _CIS_STATUS_RU.get(raw.upper(), raw)


def classify_cis_status(status: str) -> str:
    """Map True API CIS status → kind: in_circulation / withdrawn / pre / other / unknown."""
    s = str(status or "").strip().upper()
    if not s:
        return "unknown"
    if s in _CIS_STATUS_KIND_IN:
        return "in_circulation"
    if s in _CIS_STATUS_KIND_OUT:
        return "withdrawn"
    if s in _CIS_STATUS_KIND_PRE:
        return "pre"
    return "other"


def _normalize_inn(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def cis_owner_is_foreign(*, owner_inn: str = "", participant_inn: str = "") -> bool:
    """True when CHZ ownerInn is present and differs from our participant INN."""
    ours = _normalize_inn(participant_inn)
    owner = _normalize_inn(owner_inn)
    return bool(ours and owner and owner != ours)


def cis_display_for_row(
    *,
    status: str = "",
    owner_inn: str = "",
    participant_inn: str = "",
) -> tuple[str, str]:
    """Label + CSS kind for the «Код в ЧЗ» column.

    If the code belongs to another INN (e.g. already transferred to WB/РВБ),
    show gray «Передан» with the same kind as «Выведен».
    """
    if cis_owner_is_foreign(owner_inn=owner_inn, participant_inn=participant_inn):
        return "Передан", "withdrawn"
    return cis_status_label(status), classify_cis_status(status)


def parse_cises_info_item(item: dict[str, Any] | None) -> dict[str, str]:
    """Normalize one ``/cises/info`` row into cis / status / owner_inn / error."""
    if not isinstance(item, dict):
        return {"cis": "", "status": "", "owner_inn": "", "error": "", "error_code": ""}
    info = item.get("cisInfo") or item.get("cis_info")
    if not isinstance(info, dict):
        info = item if "status" in item or "cis" in item else {}
    if not isinstance(info, dict):
        info = {}
    cis = str(
        info.get("requestedCis")
        or info.get("cis")
        or item.get("requestedCis")
        or item.get("cis")
        or ""
    ).strip()
    status = str(info.get("status") or item.get("status") or "").strip()
    owner = str(
        info.get("ownerInn")
        or info.get("owner_inn")
        or info.get("inn")
        or item.get("ownerInn")
        or ""
    ).strip()
    err = str(
        item.get("errorMessage")
        or item.get("error_message")
        or item.get("error")
        or info.get("errorMessage")
        or ""
    ).strip()
    err_code = str(
        item.get("errorCode") or item.get("error_code") or info.get("errorCode") or ""
    ).strip()
    if err_code and err and err_code not in err:
        err = f"{err_code}: {err}"
    elif err_code and not err:
        err = err_code
    return {
        "cis": cis,
        "status": status,
        "owner_inn": owner,
        "error": err,
        "error_code": err_code,
    }


def _cis_lookup_keys(raw: str) -> list[str]:
    """Stable match keys for short WB excise vs full True API CIS."""
    n = _normalize_cis_for_chz(raw)
    if not n:
        return []
    keys: list[str] = [n]
    # Drop common crypto tails pasted after GS (AI 91/93 often starts mid-string).
    for sep in ("\x1d", ";"):
        if sep in str(raw or ""):
            head = _normalize_cis_for_chz(str(raw).split(sep, 1)[0])
            if head and head not in keys:
                keys.append(head)
    # Prefix variants: GTIN+serial without crypto (typical WB excise_short length).
    if len(n) > 25:
        for cut in (31, 25, 38):
            if len(n) > cut:
                frag = n[:cut]
                if frag not in keys:
                    keys.append(frag)
    return keys


def _index_cises_info_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for item in rows:
        parsed = parse_cises_info_item(item if isinstance(item, dict) else None)
        for key in _cis_lookup_keys(parsed.get("cis") or ""):
            if key and key not in indexed:
                indexed[key] = parsed
    return indexed


def _match_cis_info(
    indexed: dict[str, dict[str, str]], excise_short: str
) -> dict[str, str] | None:
    keys = _cis_lookup_keys(excise_short)
    for k in keys:
        hit = indexed.get(k)
        if hit:
            return hit
    # Prefix fallback when one side is longer (full CIS vs short).
    for k in keys:
        for idx_key, parsed in indexed.items():
            if idx_key.startswith(k) or k.startswith(idx_key):
                return parsed
    return None


def refresh_cis_statuses(
    repo: ReviewRepository,
    client: ChzTrueApiClient,
    *,
    user_id: int,
    source_id: int,
    event_keys: list[str] | None = None,
    product_group: str = "",
    limit: int = 2000,
) -> dict[str, Any]:
    """Call True API ``/cises/info`` and store CIS status on circulation events.

    This is the **code** status (в обороте / выведен), not the LK_RECEIPT document
    status shown in the «ЧЗ» column.
    """
    ensure_kiz_circulation_tables(repo)
    wanted = [
        str(k).strip()
        for k in (event_keys or [])
        if str(k or "").strip()
    ]
    # Cap one HTTP call; UI batches larger selections.
    lim = max(1, min(int(limit or 2000), 5000))
    if wanted:
        wanted = wanted[: min(len(wanted), lim)]
        lim = max(len(wanted), 1)
    with repo._connect() as conn:
        if wanted:
            ph = ", ".join("?" for _ in wanted)
            rows = conn.execute(
                repo._sql(
                    f"""
                    SELECT id, event_key, excise_short, cis_status, cis_owner_inn,
                           cis_status_error, cis_checked_at
                    FROM wb_kiz_circulation_events
                    WHERE user_id = ? AND source_id = ?
                      AND event_key IN ({ph})
                    ORDER BY id ASC
                    LIMIT ?
                    """
                ),
                (user_id, source_id, *wanted, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                repo._sql(
                    """
                    SELECT id, event_key, excise_short, cis_status, cis_owner_inn,
                           cis_status_error, cis_checked_at
                    FROM wb_kiz_circulation_events
                    WHERE user_id = ? AND source_id = ?
                      AND COALESCE(excise_short, '') <> ''
                    ORDER BY updated_at DESC NULLS LAST, id DESC
                    LIMIT ?
                    """
                ),
                (user_id, source_id, lim),
            ).fetchall()
    events = [repo._row_to_dict(r) for r in rows]
    if not events:
        return {
            "ok": True,
            "requested": 0,
            "updated": 0,
            "found": 0,
            "missing": 0,
            "errors": 0,
            "chunks": 0,
        }

    # Unique CIS values for the API (preserve first event per code).
    codes: list[str] = []
    seen_codes: set[str] = set()
    for ev in events:
        cis = _normalize_cis_for_chz(str(ev.get("excise_short") or ""))
        if not cis or cis in seen_codes:
            continue
        seen_codes.add(cis)
        codes.append(cis)

    pg = str(product_group or "").strip()
    if not pg:
        settings = get_chz_settings(repo, user_id=user_id)
        pg = str(settings.get("product_group") or "").strip()

    indexed: dict[str, dict[str, str]] = {}
    api_errors = 0
    api_error_samples: list[str] = []
    chunks = 0

    def _ingest(rows_api: list[dict[str, Any]], requested: list[str]) -> None:
        # Prefer positional pairing — True API returns one row per request CIS.
        for i, item in enumerate(rows_api):
            parsed = parse_cises_info_item(item if isinstance(item, dict) else None)
            req = requested[i] if i < len(requested) else ""
            if not parsed.get("cis") and req:
                parsed["cis"] = req
            keys = _cis_lookup_keys(parsed.get("cis") or "")
            if req:
                for k in _cis_lookup_keys(req):
                    if k not in keys:
                        keys.append(k)
            for key in keys:
                if key and key not in indexed:
                    indexed[key] = parsed
            if req and req not in indexed:
                indexed[req] = parsed

    def _fetch_chunk(part: list[str], *, pg_value: str) -> list[dict[str, Any]]:
        return client.cises_info(part, product_group=pg_value)

    for part in _chunked(codes, CIS_INFO_CHUNK):
        chunks += 1
        try:
            rows_api = _fetch_chunk(part, pg_value=pg)
            # Docs: wrong pg → «КИ не найден» even if the code exists elsewhere.
            not_found_n = 0
            for item in rows_api:
                parsed = parse_cises_info_item(item if isinstance(item, dict) else None)
                err_l = (parsed.get("error") or "").lower()
                if "не найден" in err_l or parsed.get("error_code") == "404":
                    not_found_n += 1
            if (
                pg
                and rows_api
                and not_found_n == len(rows_api)
                and len(part) == len(rows_api)
            ):
                logger.warning(
                    "CHZ cises/info all not-found with pg=%s — retry without pg "
                    "(sample %s)",
                    pg,
                    part[0][:40],
                )
                rows_api = _fetch_chunk(part, pg_value="")
            _ingest(rows_api, part)
            nf_samples: list[str] = []
            not_found_n = 0
            for i, item in enumerate(rows_api):
                parsed = parse_cises_info_item(
                    item if isinstance(item, dict) else None
                )
                err_l = (parsed.get("error") or "").lower()
                if "не найден" in err_l or parsed.get("error_code") == "404":
                    not_found_n += 1
                    req = part[i] if i < len(part) else ""
                    if req and len(nf_samples) < 5:
                        ser = req[18:] if len(req) > 18 else req
                        nf_samples.append(f"{req[:48]}(ser_len={len(ser)})")
            logger.info(
                "CHZ cises/info chunk ok pg=%s codes=%s not_found=%s/%s "
                "sample=%s nf=%s",
                pg or "(none)",
                len(part),
                not_found_n,
                len(rows_api),
                part[0][:48] if part else "",
                nf_samples,
            )
        except Exception as exc:
            api_errors += 1
            sample = str(exc)[:240]
            if len(api_error_samples) < 3:
                api_error_samples.append(sample)
            logger.warning("CHZ cises/info chunk failed: %s", exc)

    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    found = 0
    missing = 0
    err_n = 0
    with repo._connect() as conn:
        for ev in events:
            key = str(ev.get("event_key") or "").strip()
            excise = str(ev.get("excise_short") or "").strip()
            if not key or not excise:
                continue
            hit = _match_cis_info(indexed, excise)
            if hit is None:
                missing += 1
                status = ""
                owner = ""
                err = "нет в ответе ЧЗ" if not api_errors else "ошибка запроса ЧЗ"
                err_n += 1
            else:
                status = str(hit.get("status") or "").strip()
                owner = str(hit.get("owner_inn") or "").strip()
                err = str(hit.get("error") or "").strip()
                if status:
                    found += 1
                if err and not status:
                    err_n += 1
            conn.execute(
                repo._sql(
                    """
                    UPDATE wb_kiz_circulation_events
                    SET cis_status = ?, cis_owner_inn = ?, cis_status_error = ?,
                        cis_checked_at = ?, updated_at = ?
                    WHERE user_id = ? AND source_id = ? AND event_key = ?
                    """
                ),
                (
                    status,
                    owner,
                    err[:500],
                    now,
                    now,
                    user_id,
                    source_id,
                    key,
                ),
            )
            updated += 1

    return {
        "ok": True,
        "requested": len(events),
        "codes": len(codes),
        "updated": updated,
        "found": found,
        "missing": missing,
        "errors": err_n,
        "api_errors": api_errors,
        "api_error_samples": api_error_samples,
        "chunks": chunks,
    }


def heal_submitted_terminal_statuses(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
) -> dict[str, int]:
    """Flip submitted → error/accepted when chz_status already terminal.

    Fixes rows stuck on «отправлен» after CHZ already returned CHECKED_NOT_OK
    (or success) but local status was never updated — e.g. refresh without token.
    """
    ensure_kiz_circulation_tables(repo)
    fail_list = sorted(CHZ_STATUS_FAILED)
    ok_list = sorted(CHZ_STATUS_SUCCESS)
    if not fail_list and not ok_list:
        return {"healed": 0, "to_error": 0, "to_accepted": 0}
    now = datetime.now(timezone.utc).isoformat()
    to_error = 0
    to_accepted = 0
    with repo._connect() as conn:
        if fail_list:
            ph = ", ".join(["?"] * len(fail_list))
            cur = conn.execute(
                repo._sql(
                    f"""
                    UPDATE wb_kiz_circulation_events
                    SET status = ?,
                        error_text = CASE
                          WHEN COALESCE(error_text, '') = ''
                            THEN 'ЧЗ: ' || COALESCE(chz_status, 'ERROR')
                          ELSE error_text
                        END,
                        updated_at = ?
                    WHERE user_id = ? AND source_id = ?
                      AND status = ?
                      AND UPPER(COALESCE(chz_status, '')) IN ({ph})
                    """
                ),
                (STATUS_ERROR, now, user_id, source_id, STATUS_SUBMITTED, *fail_list),
            )
            to_error = int(getattr(cur, "rowcount", 0) or 0)
        if ok_list:
            ph = ", ".join(["?"] * len(ok_list))
            cur = conn.execute(
                repo._sql(
                    f"""
                    UPDATE wb_kiz_circulation_events
                    SET status = ?, raw_json = '{{}}', updated_at = ?
                    WHERE user_id = ? AND source_id = ?
                      AND status = ?
                      AND UPPER(COALESCE(chz_status, '')) IN ({ph})
                    """
                ),
                (
                    STATUS_ACCEPTED,
                    now,
                    user_id,
                    source_id,
                    STATUS_SUBMITTED,
                    *ok_list,
                ),
            )
            to_accepted = int(getattr(cur, "rowcount", 0) or 0)
    return {
        "healed": to_error + to_accepted,
        "to_error": to_error,
        "to_accepted": to_accepted,
    }


def get_overview(
    repo: ReviewRepository, *, user_id: int, source_id: int
) -> dict[str, Any]:
    ensure_kiz_circulation_tables(repo)
    cursor = get_cursor(repo, user_id=user_id, source_id=source_id)
    with repo._connect() as conn:
        counts = conn.execute(
            repo._sql(
                """
                SELECT status, operation_type, skip_reason, COUNT(*) AS cnt
                FROM wb_kiz_circulation_events
                WHERE user_id = ? AND source_id = ?
                GROUP BY status, operation_type, skip_reason
                """
            ),
            (user_id, source_id),
        ).fetchall()
        last_run = conn.execute(
            repo._sql(
                "SELECT * FROM wb_kiz_circulation_runs WHERE user_id = ? AND source_id = ? "
                "ORDER BY id DESC LIMIT 1"
            ),
            (user_id, source_id),
        ).fetchone()
    by_status: dict[str, int] = {}
    pending_withdraw = 0
    pending_return = 0
    eligibility_skipped = 0
    for r in counts:
        d = repo._row_to_dict(r)
        st = str(d.get("status") or "")
        op = int(d.get("operation_type") or 0)
        cnt = int(d.get("cnt") or 0)
        reason = str(d.get("skip_reason") or "")
        by_status[st] = by_status.get(st, 0) + cnt
        if st == STATUS_SKIPPED and reason in _ELIGIBILITY_SKIP_REASONS:
            eligibility_skipped += cnt
        if st in {STATUS_PENDING, STATUS_READY, STATUS_ERROR} and op == OP_WITHDRAW:
            pending_withdraw += cnt
        if st in {STATUS_PENDING, STATUS_READY, STATUS_ERROR} and op == OP_RETURN:
            pending_return += cnt
    run = repo._row_to_dict(last_run) if last_run else None
    total_all = sum(by_status.values())
    return {
        "cursor": cursor,
        "counts": by_status,
        "pending_withdraw": pending_withdraw,
        "pending_return": pending_return,
        "not_fbs_skipped": eligibility_skipped,
        "eligibility_skipped": eligibility_skipped,
        "total_fbs": max(0, total_all - eligibility_skipped),
        "last_run": run,
        "chz": get_chz_settings(repo, user_id=user_id),
    }


def get_run(repo: ReviewRepository, *, user_id: int, run_id: int) -> dict[str, Any] | None:
    ensure_kiz_circulation_tables(repo)
    with repo._connect() as conn:
        row = conn.execute(
            repo._sql(
                "SELECT * FROM wb_kiz_circulation_runs WHERE id = ? AND user_id = ?"
            ),
            (run_id, user_id),
        ).fetchone()
    return repo._row_to_dict(row) if row else None


def _persist_event_prices(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    filled: list[tuple[str, float, str]],
) -> int:
    """Write backfilled prices onto circulation events (only when still NULL)."""
    if not filled:
        return 0
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    with repo._connect() as conn:
        for event_key, price_rub, currency_name in filled:
            cur = conn.execute(
                repo._sql(
                    """
                    UPDATE wb_kiz_circulation_events
                    SET price = ?,
                        currency_name = CASE
                            WHEN COALESCE(?, '') <> '' THEN ?
                            ELSE currency_name
                        END,
                        updated_at = ?
                    WHERE user_id = ? AND source_id = ? AND event_key = ?
                      AND price IS NULL
                    """
                ),
                (
                    float(price_rub),
                    str(currency_name or ""),
                    str(currency_name or ""),
                    now,
                    int(user_id),
                    int(source_id),
                    str(event_key),
                ),
            )
            try:
                n += int(cur.rowcount or 0)
            except Exception:
                n += 1
    return n


def _price_for_chz(ev: dict[str, Any]) -> int | None:
    """True API ``product_cost`` is in kopecks (incl. VAT when applicable).

    WB Analytics excise-report ``price`` is in major currency units (rubles).
    """
    if ev.get("price") is None:
        return None
    cur = str(ev.get("currency_name") or "").strip().upper().replace(".", "")
    rub_aliases = {"RUB", "RUR", "₽", "РУБ", "РУБЛЬ", "РУБЛИ", "643", "810"}
    if cur and cur not in rub_aliases:
        return None
    try:
        rub = float(ev["price"])
    except (TypeError, ValueError):
        return None
    if rub <= 0:
        return None
    return int(round(rub * 100))


def _normalize_cis_for_chz(raw: str) -> str:
    """Canonical short CIS (no crypto) for matching / dedup.

    Crypto tails and GS are stripped so the same mark matches whether WB sent
    a short sgtin or a full Data Matrix string.
    """
    unit, _, _, _ = _split_cis_unit_crypto(raw)
    if unit:
        return unit
    s = _clean_cis_raw(raw)
    s = s.replace(_GS, "")
    # ``\\x1d``.isspace() is True in CPython — never use bare str.strip().
    return s.strip(" \t\r\n")


_GS = "\x1d"
# AI 91 (4-char key) + AI 92 (crypto tail). Lengths vary by product group
# (лёгпром/обувь: 91=4, 92=44 или 88; укороченный КМ: AI 93).
_CIS_CRYPTO_91_92_RE = re.compile(
    r"91([0-9A-Za-z+/]{4})(?:\x1d)?92([0-9A-Za-z+/=]{20,120})"
)
_CIS_CRYPTO_93_RE = re.compile(r"93([0-9A-Za-z+/=]{4,88})")
# GS1 General Specifications Figure 7.11-1 — AI encodable character set 82.
# ЧЗ КМ = GS1 DataMatrix; AI 21 (сериал) использует именно CSET 82
# (см. lint_cset82 / GS1 GenSpecs). Не путать с CSET 39 (# - /).
# Official string from GS1:
#   !"%&'()*+,-./0123456789:;<=>?ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz
_GS1_CSET82 = frozenset(
    "!\"%&'()*+,-./0123456789:;<=>?"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
    "abcdefghijklmnopqrstuvwxyz"
)
_CIS_SERIAL_CHARS = _GS1_CSET82


def _clean_cis_raw(raw: str) -> str:
    """Normalize a scanned/pasted КМ string per CHZ + GS1 rules.

    Keep:
    - GS (ASCII 29) as the only field separator before AI 91/92/93
    - all GS1 CSET 82 characters inside AI 21 (incl. ``'`` ``"`` ``,`` ``_``)

    Strip only transport junk that is not part of the KM:
    - whole-cell CSV wrapping quotes
    - Excel artifacts glued immediately before crypto AIs (``,i"91…``)
    - non-printable / non-ASCII (except GS)
    - trailing ``.`` after base64 ``=`` on crypto
    """
    s = str(raw or "")
    if not s:
        return ""
    # Do not use str.strip() — in Python ``\\x1d``.isspace() is True.
    s = s.strip(" \t\r\n")
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip(" \t\r\n")
    for sep in ("\u001d", "\x1e", "\x1f", "\x1c", "\u001e"):
        s = s.replace(sep, _GS)
    # CSV/Excel garbage only at the crypto boundary. Comma/semicolon/apostrophe
    # are valid mid-serial (CSET 82); do not strip them elsewhere.
    s = re.sub(r'[,;]+[A-Za-z]?["\']*(?=91|92|93)', _GS, s)
    s = re.sub(r'["\']+(?=91|92|93)', "", s)
    # Drop controls/Unicode outside GS + printable ASCII; serial filter is CSET 82.
    s = re.sub(r"[^\x1d\x21-\x7e]", "", s)
    if s.endswith("=."):
        s = s[:-1]
    return s


def _take_gs1_ai21_serial(serial_raw: str) -> str:
    """Read AI 21 serial (GS1 CSET 82).

    Per marking rules the variable-length serial is terminated by GS (ASCII 29)
    before the next AI (91/92/93). A bare GS without those AIs is treated as a
    spurious separator (common scanner/CSV artifact), not end of serial.
    """
    out: list[str] = []
    i = 0
    n = len(serial_raw)
    while i < n:
        ch = serial_raw[i]
        if ch == _GS:
            nxt = serial_raw[i + 1 : i + 3]
            if nxt in ("91", "92", "93"):
                break
            i += 1
            continue
        if ch not in _CIS_SERIAL_CHARS:
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _split_cis_unit_crypto(raw: str) -> tuple[str, str, str, str]:
    """Parse CIS → (short_unit, ai91_key, ai92_crypto, ai93).

    ``short_unit`` is ``01``+GTIN14+``21``+serial (КИ without crypto). Empty
    parts when missing. Tolerates missing GS between unit and ``91``/``92``.
    """
    s = _clean_cis_raw(raw)
    if not s:
        return "", "", "", ""

    key91 = ""
    crypto92 = ""
    crypto93 = ""
    head = s

    m92 = _CIS_CRYPTO_91_92_RE.search(s)
    if m92:
        key91 = m92.group(1)
        crypto92 = m92.group(2).rstrip(".")
        head = s[: m92.start()]
    else:
        m93 = _CIS_CRYPTO_93_RE.search(s)
        if m93:
            crypto93 = m93.group(1).rstrip(".")
            head = s[: m93.start()]

    head = head.rstrip(_GS)
    # Require classic unit prefix; otherwise keep cleaned head as best-effort.
    if not (head.startswith("01") and len(head) >= 18 and head[16:18] == "21"):
        unit = "".join(ch for ch in head if ch != _GS)
        return unit, key91, crypto92, crypto93

    prefix = head[:18]
    serial = _take_gs1_ai21_serial(head[18:])
    if not serial:
        unit = "".join(ch for ch in head if ch != _GS)
        return unit, key91, crypto92, crypto93
    return prefix + serial, key91, crypto92, crypto93


def _format_cis_for_chz_document(raw: str) -> str:
    """CIS string for True API document ``products[].cis``.

    Rebuilds a well-formed code with GS before AI 91/92 when the crypto tail is
    recoverable. Falls back to the short unit code when crypto is missing or
    corrupted — never sends quotes/commas/trailing dots (ЧЗ: «недопустимое
    количество символов»).
    """
    unit, key91, crypto92, crypto93 = _split_cis_unit_crypto(raw)
    if not unit:
        return ""
    if key91 and crypto92 and 20 <= len(crypto92) <= 88:
        return f"{unit}{_GS}91{key91}{_GS}92{crypto92}"
    if crypto93 and 4 <= len(crypto93) <= 88:
        return f"{unit}{_GS}93{crypto93}"
    return unit


def _chz_product_from_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Build True API product row with sanitized CIS; None if code unusable."""
    cis = _format_cis_for_chz_document(str(ev.get("excise_short") or ""))
    if not cis:
        return None
    product: dict[str, Any] = {"cis": cis}
    cost = _price_for_chz(ev)
    if cost is not None:
        product["product_cost"] = cost
    return product


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    n = max(1, int(size))
    return [items[i : i + n] for i in range(0, len(items), n)]


def extract_chz_doc_status(info: dict[str, Any] | None) -> str:
    """Best-effort document status string from True API ``/doc/{id}/info``."""
    if isinstance(info, list):
        for item in info:
            if isinstance(item, dict):
                got = extract_chz_doc_status(item)
                if got:
                    return got
        return ""
    if not isinstance(info, dict):
        return ""
    # Unwrap accidental {"raw": [ {...} ]} from older clients.
    if "status" not in info and "docStatus" not in info:
        raw = info.get("raw")
        if isinstance(raw, (list, dict)):
            got = extract_chz_doc_status(raw)  # type: ignore[arg-type]
            if got:
                return got
    for key in (
        "status",
        "docStatus",
        "doc_status",
        "state",
        "documentStatus",
        "document_status",
    ):
        val = info.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    body = info.get("body")
    if isinstance(body, dict):
        for key in ("status", "docStatus", "state"):
            val = body.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def extract_chz_doc_errors(info: dict[str, Any] | None) -> str:
    """Best-effort human text from True API document_info payload."""
    if isinstance(info, list):
        parts = [
            extract_chz_doc_errors(item)
            for item in info
            if isinstance(item, dict)
        ]
        return "; ".join(p for p in parts if p)[:1800]
    if not isinstance(info, dict):
        return ""
    if "errors" not in info and "commonErrors" not in info:
        raw = info.get("raw")
        if isinstance(raw, (list, dict)):
            got = extract_chz_doc_errors(raw)  # type: ignore[arg-type]
            if got:
                return got
    chunks: list[str] = []
    for key in (
        "errors",
        "commonErrors",
        "common_errors",
        "error_messages",
        "errorMessages",
        "rejectionReason",
        "rejection_reason",
    ):
        val = info.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    msg = (
                        item.get("errorMessage")
                        or item.get("message")
                        or item.get("error")
                        or item.get("description")
                        or item.get("text")
                        or ""
                    )
                    code = (
                        item.get("errorCode")
                        or item.get("code")
                        or ""
                    )
                    line = " ".join(str(x) for x in (code, msg) if x).strip()
                    if line:
                        chunks.append(line)
                elif item:
                    chunks.append(str(item))
        elif isinstance(val, str) and val.strip():
            chunks.append(val.strip())
        elif isinstance(val, dict):
            msg = (
                val.get("errorMessage")
                or val.get("message")
                or val.get("description")
                or ""
            )
            if msg:
                chunks.append(str(msg))
    for key in ("description", "error", "error_message", "body"):
        val = info.get(key)
        if isinstance(val, str) and val.strip() and val.strip() not in chunks:
            chunks.append(val.strip())
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for c in chunks:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return "; ".join(out)[:1800]


def prepare_chz_batches(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    limit: int = PREPARE_EVENT_LIMIT,
    event_keys: list[str] | None = None,
    api_key: str = "",
) -> dict[str, Any]:
    """Build unsigned CHZ document payloads grouped by operation + receipt.

    Withdraw (вывод) is allowed only when Marketplace ``wbStatus=sold``.
    ``api_key`` is the FBS Marketplace token (hydrate sold/archive for srid join).
    """
    settings = get_chz_settings(repo, user_id=user_id)
    if not settings.get("is_enabled"):
        raise ValueError("ЧЗ выключен в Настройки → ЧЗ")
    inn = str(settings.get("participant_inn") or "").strip()
    if not inn:
        raise ValueError("Укажите ИНН участника в Настройки → ЧЗ")
    pg = str(settings.get("product_group") or "").strip()
    if not pg:
        raise ValueError("Укажите товарную группу (pg) в Настройки → ЧЗ")
    if pg.isdigit():
        raise ValueError(
            "Товарная группа (pg) — код True API (например lp, shoes), не число"
        )

    wanted_keys = [
        str(k).strip()
        for k in (event_keys or [])
        if str(k or "").strip()
    ] or None
    queue_repair: dict[str, Any] = {}
    # Full queue repair + storage GC are expensive on large histories and push
    # prepare past gateway timeouts. Skip when the client sends an explicit
    # selection (typical «Передать в ЧЗ» path).
    if not wanted_keys:
        queue_repair = repair_circulation_queue(
            repo, user_id=user_id, source_id=source_id
        )
        try:
            storage = maintain_kiz_circulation_storage(
                repo, user_id=user_id, source_id=source_id
            )
            queue_repair.update(storage)
        except Exception as exc:
            logger.exception("maintain_kiz_circulation_storage failed: %s", exc)
        try:
            empty_closed = preclose_empty_cis_events(
                repo, user_id=user_id, source_id=source_id
            )
            if empty_closed:
                queue_repair["empty_cis_closed"] = empty_closed
        except Exception as exc:
            logger.exception("preclose_empty_cis_events failed: %s", exc)
    lim = max(1, min(int(limit or PREPARE_EVENT_LIMIT), 5000))

    documents: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []
    not_sold_n = 0
    not_cancelled_n = 0
    bad_cis_n = 0
    no_price_n = 0
    eligible_loaded = 0
    eligible_after_dedupe = 0
    docs_built_total = 0
    sticky_closed_total = 0
    hit_event_limit = False
    kpp = str(settings.get("kpp") or "").strip()
    fias_id = str(settings.get("fias_id") or "").strip()
    if not kpp or not fias_id:
        place = resolve_chz_place_details(
            repo, user_id=user_id, participant_inn=inn
        )
        if not kpp:
            kpp = str(place.get("kpp") or "").strip()
        if not fias_id:
            fias_id = str(place.get("fias_id") or "").strip()
    # ИП (12 цифр): КПП в LK_RECEIPT не передаётся.
    if len(re.sub(r"\D", "", inn)) == 12:
        kpp = ""

    doc_cap = max(1, int(CHZ_DOCUMENTS_PER_PREPARE))
    drain_passes = max(1, int(PREPARE_QUEUE_DRAIN_PASSES))
    if wanted_keys:
        drain_passes = 1
    sent_identities = _load_sent_cis_identities(
        repo, user_id=user_id, source_id=source_id
    )
    other_doc_date = _moscow_today()
    used_event_keys: set[str] = set()
    place_warned = False
    mod_warned = False

    for _drain in range(drain_passes):
        if len(documents) >= doc_cap:
            break
        events_raw = list_events_for_chz(
            repo,
            user_id=user_id,
            source_id=source_id,
            limit=lim,
            event_keys=wanted_keys,
        )
        if used_event_keys:
            events_raw = [
                e
                for e in events_raw
                if str(e.get("event_key") or "") not in used_event_keys
            ]
        if not events_raw:
            break
        eligible_loaded += len(events_raw)
        hit_event_limit = hit_event_limit or (len(events_raw) >= lim and not wanted_keys)
        # Join Marketplace order + status. Hydrate only unresolved srids (see attach).
        _attach_order_ids_to_events(
            repo,
            user_id=user_id,
            source_id=source_id,
            events=events_raw,
            api_key=api_key,
            hydrate=bool(str(api_key or "").strip()),
            refresh_statuses=bool(str(api_key or "").strip()),
        )
        events, pre_skipped = _dedupe_events_for_prepare(
            events_raw, sent_identities=sent_identities
        )
        eligible_after_dedupe += len(events)
        skipped.extend(pre_skipped)
        pass_skipped: list[dict[str, Any]] = list(pre_skipped)

        withdraw_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        withdraw_other_groups: dict[str, list[dict[str, Any]]] = {}
        return_items: list[dict[str, Any]] = []
        pass_not_sold = 0
        pass_not_cancelled = 0

        for ev in events:
            op = int(ev.get("operation_type") or 0)
            fiscal_no = str(ev.get("fiscal_doc_number") or "").strip()
            fiscal_dt = str(ev.get("fiscal_dt") or "").strip()
            cis = _normalize_cis_for_chz(str(ev.get("excise_short") or ""))
            if not cis:
                row = {**ev, "skip_reason": SKIP_EMPTY_CIS}
                skipped.append(row)
                pass_skipped.append(row)
                continue
            ev = {**ev, "excise_short": cis}
            if op == OP_WITHDRAW:
                not_sold = _withdraw_not_sold_reason(ev)
                if not_sold:
                    pass_not_sold += 1
                    row = {**ev, "skip_reason": not_sold}
                    skipped.append(row)
                    if not_sold in _TERMINAL_SKIP_REASONS:
                        pass_skipped.append(row)
                    continue
                if fiscal_no and fiscal_dt:
                    withdraw_groups.setdefault((fiscal_no, fiscal_dt), []).append(ev)
                else:
                    day = fiscal_dt or other_doc_date
                    withdraw_other_groups.setdefault(day, []).append(ev)
            elif op == OP_RETURN:
                not_cancelled = _return_not_cancelled_reason(ev)
                if not_cancelled:
                    pass_not_cancelled += 1
                    row = {**ev, "skip_reason": not_cancelled}
                    skipped.append(row)
                    if not_cancelled in _TERMINAL_SKIP_REASONS:
                        pass_skipped.append(row)
                    continue
                return_items.append(ev)
            else:
                skipped.append({**ev, "skip_reason": "неизвестный тип"})

        not_sold_n += pass_not_sold
        not_cancelled_n += pass_not_cancelled

        if (withdraw_groups or withdraw_other_groups) and (
            not fias_id or (len(re.sub(r"\D", "", inn)) == 10 and not kpp)
        ):
            if not place_warned:
                warnings.append(
                    "Вывод DISTANCE пропущен: укажите КПП (для ООО) и ФИАС МОД в Настройки → ЧЗ "
                    "— те же, что в профиле Честного знака (вкладка МОД), "
                    "либо у юр. лица с этим ИНН"
                )
                place_warned = True
            for group in withdraw_groups.values():
                for ev in group:
                    row = {**ev, "skip_reason": SKIP_NO_PLACE}
                    skipped.append(row)
                    pass_skipped.append(row)
            for group in withdraw_other_groups.values():
                for ev in group:
                    row = {**ev, "skip_reason": SKIP_NO_PLACE}
                    skipped.append(row)
                    pass_skipped.append(row)
            withdraw_groups = {}
            withdraw_other_groups = {}
        elif (withdraw_groups or withdraw_other_groups) and not mod_warned:
            mod_parts = [f"ИНН {inn}"]
            if kpp:
                mod_parts.append(f"КПП {kpp}")
            mod_parts.append(f"ФИАС {fias_id}")
            warnings.append(
                "МОД в документах: "
                + ", ".join(mod_parts)
                + " — должен совпадать с действующим МОД в профиле ЧЗ"
            )
            mod_warned = True

        pass_docs: list[dict[str, Any]] = []
        for (fiscal_no, fiscal_dt), group in withdraw_groups.items():
            for part_idx, part in enumerate(
                _chunked(group, CHZ_PRODUCTS_PER_DOC), start=1
            ):
                products = []
                keys_ok: list[str] = []
                for ev in part:
                    product = _chz_product_from_event(ev)
                    if not product:
                        bad_cis_n += 1
                        row = {**ev, "skip_reason": SKIP_BAD_CIS}
                        skipped.append(row)
                        pass_skipped.append(row)
                        continue
                    products.append(product)
                    keys_ok.append(str(ev.get("event_key") or ""))
                if not products:
                    continue
                doc_body = build_lk_receipt_document(
                    inn=inn,
                    document_number=fiscal_no,
                    document_date=fiscal_dt,
                    products=products,
                    kpp=kpp,
                    fias_id=fias_id,
                )
                suffix = (
                    f" · часть {part_idx}" if len(group) > CHZ_PRODUCTS_PER_DOC else ""
                )
                pass_docs.append(
                    {
                        "doc_type": "LK_RECEIPT",
                        "product_group": pg,
                        "title": f"Вывод · чек {fiscal_no} · {fiscal_dt}{suffix}",
                        "event_keys": [k for k in keys_ok if k],
                        "product_document": doc_body,
                        "sign_payload_b64": _b64_json(doc_body),
                    }
                )

        for doc_date, group in withdraw_other_groups.items():
            for part_idx, part in enumerate(
                _chunked(group, CHZ_PRODUCTS_PER_DOC), start=1
            ):
                products = []
                keys_ok: list[str] = []
                for ev in part:
                    product = _chz_product_from_event(ev)
                    if not product:
                        bad_cis_n += 1
                        row = {**ev, "skip_reason": SKIP_BAD_CIS}
                        skipped.append(row)
                        pass_skipped.append(row)
                        continue
                    if product.get("product_cost") is None:
                        no_price_n += 1
                        row = {**ev, "skip_reason": SKIP_NO_PRODUCT_COST}
                        skipped.append(row)
                        pass_skipped.append(row)
                        continue
                    products.append(product)
                    keys_ok.append(str(ev.get("event_key") or ""))
                if not products:
                    continue
                doc_number = f"WB-NOFISCAL-{doc_date}"
                if len(group) > CHZ_PRODUCTS_PER_DOC:
                    doc_number = f"{doc_number}-{part_idx}"
                doc_body = build_lk_receipt_document(
                    inn=inn,
                    document_number=doc_number,
                    document_date=doc_date,
                    primary_document_type=NO_FISCAL_PRIMARY_DOC_TYPE,
                    primary_document_custom_name=NO_FISCAL_PRIMARY_DOC_NAME,
                    products=products,
                    kpp=kpp,
                    fias_id=fias_id,
                )
                suffix = (
                    f" · часть {part_idx}" if len(group) > CHZ_PRODUCTS_PER_DOC else ""
                )
                pass_docs.append(
                    {
                        "doc_type": "LK_RECEIPT",
                        "product_group": pg,
                        "title": (
                            f"Вывод · без чека (OTHER) · {doc_date}{suffix}"
                        ),
                        "event_keys": [k for k in keys_ok if k],
                        "product_document": doc_body,
                        "sign_payload_b64": _b64_json(doc_body),
                    }
                )

        for part_idx, part in enumerate(
            _chunked(return_items, CHZ_PRODUCTS_PER_DOC), start=1
        ):
            if not part:
                continue
            products = []
            keys_ok: list[str] = []
            for e in part:
                product = _chz_product_from_event(e)
                if not product:
                    bad_cis_n += 1
                    row = {**e, "skip_reason": SKIP_BAD_CIS}
                    skipped.append(row)
                    pass_skipped.append(row)
                    continue
                products.append({"ki": product["cis"]})
                keys_ok.append(str(e.get("event_key") or ""))
            if not products:
                continue
            doc_body = build_lp_return_document(
                inn=inn,
                return_type=str(settings.get("return_type") or "REMOTE_SALE_RETURN"),
                products=products,
                paid=False,
            )
            suffix = (
                f" · часть {part_idx}"
                if len(return_items) > CHZ_PRODUCTS_PER_DOC
                else ""
            )
            pass_docs.append(
                {
                    "doc_type": "LP_RETURN",
                    "product_group": pg,
                    "title": f"Возврат в оборот · {len(products)} КИЗ{suffix}",
                    "event_keys": [k for k in keys_ok if k],
                    "product_document": doc_body,
                    "sign_payload_b64": _b64_json(doc_body),
                }
            )

        room = doc_cap - len(documents)
        accepted = pass_docs[: max(0, room)]
        for d in accepted:
            for k in d.get("event_keys") or []:
                if k:
                    used_event_keys.add(str(k))
        documents.extend(accepted)
        docs_built_total += len(pass_docs)

        sticky_closed = 0
        try:
            sticky_closed = _close_deduped_prepare_events(
                repo, user_id=user_id, source_id=source_id, skipped=pass_skipped
            )
        except Exception as exc:
            logger.exception("close prepare sticky skips failed: %s", exc)
        sticky_closed_total += sticky_closed

        # Further drain only when sticky closes freed the oldest head.
        if sticky_closed <= 0:
            break
        if len(documents) >= doc_cap:
            break

    if not_sold_n:
        warnings.append(
            f"Пропущено выводов без статуса «выкуплен»: {not_sold_n} "
            "(в ЧЗ уходят только заказы с wbStatus=sold; "
            "на сборке / в доставке — нельзя)"
        )
    if not_cancelled_n:
        warnings.append(
            f"Пропущено возвратов без статуса отказа: {not_cancelled_n} "
            "(в оборот возвращаются только отказные / отменённые)"
        )
    if no_price_n:
        warnings.append(
            f"Пропущено выводов без цены за единицу: {no_price_n} "
            "(ЧЗ OTHER требует product_cost; пересинхронизируйте заказы FBS "
            "или проверьте convertedPrice в Marketplace)"
        )
    if bad_cis_n:
        warnings.append(
            f"Пропущено КИЗ с битым кодом (кавычки/хвост): {bad_cis_n}"
        )

    truncated_by_docs = docs_built_total > doc_cap
    if len(documents) > doc_cap:
        documents = documents[:doc_cap]

    withdraw_n = sum(
        1
        for d in documents
        if d.get("doc_type") == "LK_RECEIPT"
        for _ in (d.get("event_keys") or [])
    )
    return_n = sum(
        1
        for d in documents
        if d.get("doc_type") == "LP_RETURN"
        for _ in (d.get("event_keys") or [])
    )
    has_more = (
        truncated_by_docs
        or hit_event_limit
        or (not documents and sticky_closed_total > 0)
    )
    return {
        "ok": True,
        "settings": {
            "api_base": settings.get("api_base"),
            "api_base_url": settings.get("api_base_url"),
            "participant_inn": inn,
            "product_group": pg,
            "cert_thumbprint": settings.get("cert_thumbprint") or "",
            "kpp": kpp,
            "fias_id": fias_id,
        },
        "documents": documents,
        "warnings": warnings,
        "queue_repair": queue_repair,
        "skipped": [
            {
                "event_key": s.get("event_key"),
                "excise_short": s.get("excise_short"),
                "skip_reason": s.get("skip_reason"),
            }
            for s in skipped
        ],
        "counts": {
            "documents": len(documents),
            "documents_built": docs_built_total,
            "documents_cap": doc_cap,
            "withdraw_events": withdraw_n,
            "return_events": return_n,
            "skipped": len(skipped),
            "withdraw_not_sold": not_sold_n,
            "return_not_cancelled": not_cancelled_n,
            "eligible_loaded": eligible_loaded,
            "eligible_after_dedupe": eligible_after_dedupe,
        },
        "has_more": has_more,
    }


def _b64_json(obj: dict[str, Any]) -> str:
    import base64

    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def classify_chz_doc_status(chz_status: str) -> str:
    """Return accepted | error | submitted for a True API document status."""
    st = str(chz_status or "").strip().upper()
    if not st:
        return STATUS_SUBMITTED
    if st in CHZ_STATUS_SUCCESS:
        return STATUS_ACCEPTED
    if st in CHZ_STATUS_FAILED:
        return STATUS_ERROR
    # Resilient to new CRPT codes: …_NOT_OK / …_ERROR are terminal failures.
    if "NOT_OK" in st or st.endswith("_ERROR") or st.endswith("_FAILED"):
        return STATUS_ERROR
    return STATUS_SUBMITTED


def mark_events_submitted(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    event_keys: list[str],
    chz_doc_id: str,
    doc_type: str,
    run_id: int | None = None,
) -> None:
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    keys = [str(k) for k in event_keys if str(k).strip()]
    if not keys:
        return
    with repo._connect() as conn:
        for key in keys:
            conn.execute(
                repo._sql(
                    """
                    UPDATE wb_kiz_circulation_events
                    SET status = ?, chz_doc_id = ?, chz_status = 'submitted',
                        updated_at = ?, error_text = ''
                    WHERE user_id = ? AND source_id = ? AND event_key = ?
                    """
                ),
                (
                    STATUS_SUBMITTED,
                    str(chz_doc_id or ""),
                    now,
                    user_id,
                    source_id,
                    key,
                ),
            )
        conn.execute(
            repo._sql(
                """
                INSERT INTO wb_kiz_chz_documents (
                    user_id, source_id, run_id, doc_type, chz_doc_id, status,
                    event_keys_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, ?)
                """
            ),
            (
                user_id,
                source_id,
                run_id,
                str(doc_type or ""),
                str(chz_doc_id or ""),
                json.dumps(keys, ensure_ascii=False),
                now,
                now,
            ),
        )


def apply_chz_doc_status(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    event_keys: list[str],
    chz_doc_id: str,
    chz_status: str,
    error_text: str = "",
) -> str:
    """Update events from CHZ document_info. Returns final local status class."""
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    st = str(chz_status or "").strip()
    final = classify_chz_doc_status(st)
    keys = [str(k) for k in event_keys if str(k).strip()]
    err = str(error_text or "").strip()
    if final == STATUS_ERROR and not err:
        err = f"ЧЗ: {st}"
    with repo._connect() as conn:
        for key in keys:
            if final == STATUS_ERROR:
                conn.execute(
                    repo._sql(
                        """
                        UPDATE wb_kiz_circulation_events
                        SET status = ?, chz_doc_id = ?, chz_status = ?,
                            error_text = ?, updated_at = ?
                        WHERE user_id = ? AND source_id = ? AND event_key = ?
                        """
                    ),
                    (
                        STATUS_ERROR,
                        str(chz_doc_id or ""),
                        st,
                        err[:2000],
                        now,
                        user_id,
                        source_id,
                        key,
                    ),
                )
            elif final == STATUS_ACCEPTED:
                conn.execute(
                    repo._sql(
                        """
                        UPDATE wb_kiz_circulation_events
                        SET status = ?, chz_doc_id = ?, chz_status = ?,
                            raw_json = '{}', updated_at = ?
                        WHERE user_id = ? AND source_id = ? AND event_key = ?
                        """
                    ),
                    (
                        STATUS_ACCEPTED,
                        str(chz_doc_id or ""),
                        st or final,
                        now,
                        user_id,
                        source_id,
                        key,
                    ),
                )
            else:
                conn.execute(
                    repo._sql(
                        """
                        UPDATE wb_kiz_circulation_events
                        SET status = ?, chz_doc_id = ?, chz_status = ?, updated_at = ?
                        WHERE user_id = ? AND source_id = ? AND event_key = ?
                        """
                    ),
                    (
                        final,
                        str(chz_doc_id or ""),
                        st or final,
                        now,
                        user_id,
                        source_id,
                        key,
                    ),
                )
    if final == STATUS_ACCEPTED and keys:
        try:
            register_sent_cis_for_event_keys(
                repo,
                user_id=user_id,
                source_id=source_id,
                event_keys=keys,
                accepted_at=now,
            )
        except Exception as exc:
            logger.exception("register_sent_cis_for_event_keys failed: %s", exc)
    return final


def mark_events_accepted(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    event_keys: list[str],
    chz_doc_id: str,
    chz_status: str,
) -> None:
    """Backward-compatible wrapper — uses classify_chz_doc_status."""
    apply_chz_doc_status(
        repo,
        user_id=user_id,
        source_id=source_id,
        event_keys=event_keys,
        chz_doc_id=chz_doc_id,
        chz_status=chz_status,
    )


def mark_events_error(
    repo: ReviewRepository,
    *,
    user_id: int,
    source_id: int,
    event_keys: list[str],
    error_text: str,
) -> None:
    ensure_kiz_circulation_tables(repo)
    now = datetime.now(timezone.utc).isoformat()
    with repo._connect() as conn:
        for key in event_keys:
            conn.execute(
                repo._sql(
                    """
                    UPDATE wb_kiz_circulation_events
                    SET status = ?, error_text = ?, updated_at = ?
                    WHERE user_id = ? AND source_id = ? AND event_key = ?
                    """
                ),
                (
                    STATUS_ERROR,
                    str(error_text or "")[:2000],
                    now,
                    user_id,
                    source_id,
                    key,
                ),
            )


def chz_client_from_settings(settings: dict[str, Any]) -> ChzTrueApiClient:
    return ChzTrueApiClient(base_url=str(settings.get("api_base_url") or PROD_BASE))


# Re-export for web layer convenience
__all__ = [
    "ChzTrueApiClient",
    "ChzTrueApiError",
    "ensure_kiz_circulation_tables",
    "get_chz_settings",
    "get_wb_analytics_api_key",
    "upsert_chz_settings",
    "get_cursor",
    "get_overview",
    "get_run",
    "list_events",
    "list_events_for_chz",
    "resolve_excise_period",
    "create_excise_sync_run",
    "find_active_excise_sync_run",
    "cancel_excise_sync_run",
    "abandon_orphan_excise_sync_runs",
    "request_cancel_excise_sync",
    "SyncCancelled",
    "sync_excise_report",
    "heal_submitted_terminal_statuses",
    "reconcile_submitted_with_chz",
    "refresh_cis_statuses",
    "cis_status_label",
    "classify_cis_status",
    "cis_display_for_row",
    "cis_owner_is_foreign",
    "parse_cises_info_item",
    "prepare_chz_batches",
    "mark_events_submitted",
    "mark_events_accepted",
    "apply_chz_doc_status",
    "classify_chz_doc_status",
    "mark_events_error",
    "repair_stuck_return_events",
    "repair_unhealable_withdraw_errors",
    "repair_nofiscal_withdraw_to_pending",
    "repair_orphan_submitted_events",
    "repair_stale_submitted_events",
    "repair_legacy_skipped_with_cis",
    "repair_skip_non_fbs_events",
    "purge_non_fbs_circulation_events",
    "repair_skip_wrong_fbs_status_events",
    "repair_requeue_fbs_matched_not_fbs",
    "repair_requeue_eligible_fbs_events",
    "repair_requeue_skipped_with_product_cost",
    "preclose_empty_cis_events",
    "repair_circulation_queue",
    "load_local_fbs_rid_keys",
    "load_local_fbs_order_index",
    "maintain_kiz_circulation_storage",
    "upsert_sent_cis_rows",
    "reconcile_submitted_with_chz",
    "chz_client_from_settings",
    "mask_secret",
    "encrypt_secret",
    "decrypt_secret",
]
