# -*- coding: utf-8 -*-
"""Local Ozon FBS supplies (FeedPilot parity; no Ozon supply API).

After «Собрать все заказы» postings are shipped via /v4/posting/fbs/ship and
grouped into local supplies on «Ожидают отгрузки».
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from app.db import Database
from app.ozon import TAB_AWAITING_DELIVER, TAB_AWAITING_PACKAGING, utc_now
from app.ozon.client import OzonFbsClient
from app.services.ozon_ship import OzonShipService, posting_needs_ship

_log = logging.getLogger(__name__)

SUPPLY_ID_PREFIX = "OZ-FBS"
OZON_FBS_SHIP_ALL_PAUSE_SEC = 0.22
# Dedicated local bucket for delivering postings without a supply (web parity).
ORPHAN_DELIVERING_SUPPLY_NAME = "Без локальной поставки"
MSK = ZoneInfo("Europe/Moscow")

ProgressCb = Optional[Callable[[int, int, str], None]]
StopCb = Optional[Callable[[], bool]]


def default_supply_name(*, source_name: str, when: Optional[datetime] = None) -> str:
    """``Поставка Название источника от ДД.ММ.ГГГГ`` (MSK)."""
    dt = when or datetime.now(MSK)
    label = str(source_name or "").strip() or "—"
    return "Поставка {} от {}".format(label, dt.strftime("%d.%m.%Y"))


def unique_supply_name(base: str, existing_names: Set[str]) -> str:
    name = str(base or "").strip() or "Поставка"
    if name not in existing_names:
        return name
    for i in range(2, 100):
        candidate = "{} ({})".format(name, i)
        if candidate not in existing_names:
            return candidate
    return "{} · {}".format(name, int(time.time()))


def new_supply_id(source_id: int) -> str:
    stamp = datetime.now(MSK).strftime("%Y%m%d")
    return "{}-{}-{}-{}".format(
        SUPPLY_ID_PREFIX,
        int(source_id),
        stamp,
        secrets.token_hex(3).upper(),
    )


def _parse_json_list(raw: object) -> List[Any]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _esc(value: object) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class OzonSuppliesService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def source_display_name(self, source_id: int) -> str:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT name FROM supply_sources WHERE id = ?",
                (source_id,),
            ).fetchone()
        if not row:
            return "Источник {}".format(source_id)
        return str(row["name"] or "").strip() or "Источник {}".format(source_id)

    def list_open_supplies(self, source_id: int) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_supplies
                WHERE source_id = ? AND COALESCE(done, 0) = 0
                ORDER BY datetime(created_at) DESC
                """,
                (source_id,),
            ).fetchall()
        return [self._enrich_supply(Database.row_to_dict(r)) for r in rows]

    def list_orphan_awaiting_deliver_rows(
        self, source_id: int
    ) -> List[Dict[str, Any]]:
        """``awaiting_deliver`` postings with empty ``supply_id`` (loose / orphans)."""
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_postings
                WHERE source_id = ?
                  AND tab IN ('assembly', 'awaiting_deliver')
                  AND COALESCE(supply_id, '') = ''
                  AND (carriage_id IS NULL OR carriage_id = '')
                ORDER BY datetime(created_at_wb) DESC, posting_number DESC
                """,
                (source_id,),
            ).fetchall()
        return [Database.row_to_dict(r) for r in rows]

    def adopt_orphan_awaiting_deliver_postings(
        self, source_id: int
    ) -> Dict[str, Any]:
        """Put orphan awaiting_deliver postings into a *new* local supply.

        Never merges into an already open supply the operator created (so
        «сироты» do not mix into ОЗОН_ФБС_000 / current collect). Groups by
        warehouse; each bucket gets its own ``Поставка … · добор`` name.
        """
        rows = self.list_orphan_awaiting_deliver_rows(source_id)
        if not rows:
            return {
                "ok": True,
                "adopted": 0,
                "created_supplies": [],
                "group_lines": [],
            }
        buckets = {}  # type: Dict[Any, List[Dict[str, Any]]]
        for row in rows:
            wh = row.get("warehouse_id")
            buckets.setdefault(wh, []).append(row)

        open_names = {
            str(s.get("name") or "").strip()
            for s in self.list_open_supplies(source_id)
            if str(s.get("name") or "").strip()
        }
        source_name = self.source_display_name(source_id)
        created = []  # type: List[Dict[str, str]]
        group_lines = []  # type: List[str]
        adopted = 0
        for warehouse_id in sorted(buckets.keys(), key=lambda x: (x is None, str(x))):
            bucket = buckets[warehouse_id]
            posting_numbers = [
                str(r.get("posting_number") or "").strip()
                for r in bucket
                if str(r.get("posting_number") or "").strip()
            ]
            if not posting_numbers:
                continue
            wh_name = (
                str(bucket[0].get("warehouse_name") or "").strip() if bucket else ""
            )
            base = default_supply_name(source_name=source_name) + " · добор"
            name = unique_supply_name(base, open_names)
            sid = self.create_supply(
                source_id,
                name=name,
                warehouse_id=warehouse_id,
                warehouse_name=wh_name,
                posting_numbers=posting_numbers,
            )
            open_names.add(name)
            created.append({"supply_id": sid, "name": name})
            group_lines.append(
                "{}: {} отпр. (осиротевшие)".format(name, len(posting_numbers))
            )
            adopted += len(posting_numbers)
        return {
            "ok": True,
            "adopted": adopted,
            "created_supplies": created,
            "group_lines": group_lines,
        }

    def list_awaiting_deliver_supplies(
        self, source_id: int, *, adopt_orphans: bool = False
    ) -> List[Dict[str, Any]]:
        """Open local supplies that still have awaiting_deliver postings.

        Empty shells (stale ``posting_numbers_json``, no live rows) are omitted
        and pruned. Orphan adoption is opt-in — never a side effect of tab
        counts / refresh, otherwise mid-collect orphans spawn empty «· добор».
        """
        if adopt_orphans:
            try:
                self.adopt_orphan_awaiting_deliver_postings(source_id)
            except Exception as exc:
                _log.warning("ozon adopt orphans failed: %s", exc)
        open_supplies = self.list_open_supplies(source_id)
        counts = self._order_counts_by_tab(source_id, TAB_AWAITING_DELIVER)
        delivering_ids = self._supply_ids_with_tab(source_id, "delivering")
        out = []  # type: List[Dict[str, Any]]
        empty_ids = []  # type: List[str]
        for s in open_supplies:
            sid = str(s.get("supply_id") or "")
            if not sid:
                continue
            if int(s.get("delivering_listed") or 0):
                continue
            live = int(counts.get(sid, 0) or 0)
            if sid in delivering_ids and live == 0:
                continue
            if live <= 0:
                # Stale JSON must not keep empty «добор» visible.
                empty_ids.append(sid)
                continue
            item = dict(s)
            item["posting_count"] = live
            item["order_count"] = live
            item["status_label"] = "Сборка заказов"
            item["status_kind"] = "awaiting_deliver"
            out.append(item)
        if empty_ids:
            try:
                self._delete_empty_open_supplies(source_id, empty_ids)
            except Exception as exc:
                _log.warning("ozon prune empty supplies: %s", exc)
        return out

    def list_delivering_supplies(self, source_id: int) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_supplies
                WHERE source_id = ?
                  AND (
                    COALESCE(delivering_listed, 0) = 1
                    OR COALESCE(done, 0) = 1
                  )
                ORDER BY datetime(updated_at) DESC
                """,
                (source_id,),
            ).fetchall()
        counts = self._order_counts_by_tab(source_id, "delivering")
        out = []  # type: List[Dict[str, Any]]
        for r in rows:
            item = self._enrich_supply(Database.row_to_dict(r))
            sid = str(item.get("supply_id") or "")
            item["posting_count"] = int(counts.get(sid) or item.get("order_count") or 0)
            item["status_label"] = "Доставляется"
            item["status_kind"] = "delivering"
            out.append(item)
        # Also include open supplies that already have delivering postings.
        for s in self.list_open_supplies(source_id):
            sid = str(s.get("supply_id") or "")
            if not sid or any(x.get("supply_id") == sid for x in out):
                continue
            n = int(counts.get(sid) or 0)
            if n <= 0 and not int(s.get("delivering_listed") or 0):
                continue
            item = dict(s)
            item["posting_count"] = n
            item["status_label"] = "Доставляется"
            item["status_kind"] = "delivering"
            out.append(item)
        return self._attach_delivering_meta(source_id, out)

    def _attach_delivering_meta(
        self, source_id: int, supplies: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Archive delivering table: driver + TTN status chips."""
        from app.services.supply_drivers import SupplyDriversService
        from app.services.ttn_draft import TtnDraftService

        drivers = SupplyDriversService(self.db)
        ttn = TtnDraftService(self.db)
        out = []  # type: List[Dict[str, Any]]
        for raw in supplies:
            item = dict(raw)
            sid = str(item.get("supply_id") or "").strip()
            assigned = drivers.get_assignment("ozon", source_id, sid)
            item["driver_name"] = str(assigned.get("driver_name") or "").strip()
            item["has_driver"] = bool(assigned.get("has_driver"))
            item["vehicle_number"] = str(assigned.get("vehicle_number") or "").strip()
            draft = ttn.get("ozon", source_id, sid)
            has_ttn = bool(draft and draft.get("id"))
            item["has_ttn"] = has_ttn
            item["ttn_status_label"] = (
                "Сформирована" if has_ttn else "Несформирована"
            )
            # Soft row tone like archive enrich_ozon_supply_items_row_tones.
            if not item["has_driver"] and not has_ttn:
                item["row_tone"] = "warn"
            elif item["has_driver"] and has_ttn:
                item["row_tone"] = "ok"
            else:
                item["row_tone"] = ""
            out.append(item)
        return out

    def get_supply(
        self, source_id: int, supply_id: str
    ) -> Optional[Dict[str, Any]]:
        sid = str(supply_id or "").strip()
        if not sid:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM ozon_fbs_supplies
                WHERE source_id = ? AND supply_id = ?
                """,
                (source_id, sid),
            ).fetchone()
        if not row:
            return None
        return self._enrich_supply(Database.row_to_dict(row))

    def list_supply_postings(
        self, source_id: int, supply_id: str
    ) -> List[Dict[str, Any]]:
        sid = str(supply_id or "").strip()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_postings
                WHERE source_id = ? AND supply_id = ?
                ORDER BY datetime(created_at_wb) DESC, posting_number DESC
                """,
                (source_id, sid),
            ).fetchall()
        return [Database.row_to_dict(r) for r in rows]

    def create_supply(
        self,
        source_id: int,
        *,
        name: str,
        warehouse_id: Any = None,
        warehouse_name: str = "",
        posting_numbers: Optional[List[str]] = None,
        force_tab: str = TAB_AWAITING_DELIVER,
    ) -> str:
        nums = [str(x).strip() for x in (posting_numbers or []) if str(x).strip()]
        try:
            wh_id = int(warehouse_id) if warehouse_id not in (None, "") else None
        except (TypeError, ValueError):
            wh_id = None
        sid = new_supply_id(source_id)
        now = utc_now()
        supply_name = str(name or "").strip() or default_supply_name(
            source_name=self.source_display_name(source_id)
        )
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO ozon_fbs_supplies(
                    source_id, supply_id, name, warehouse_id, warehouse_name,
                    done, delivering_listed, posting_numbers_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    source_id,
                    sid,
                    supply_name,
                    wh_id,
                    str(warehouse_name or "").strip(),
                    json.dumps(nums, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            if nums:
                placeholders = ", ".join("?" for _ in nums)
                conn.execute(
                    """
                    UPDATE ozon_fbs_postings
                    SET supply_id = ?, tab = ?, status = ?, synced_at = ?
                    WHERE source_id = ? AND posting_number IN ({})
                    """.format(placeholders),
                    [sid, force_tab, force_tab, now, source_id] + nums,
                )
            conn.commit()
        if nums:
            self._detach_postings_from_other_supplies(source_id, sid, nums)
        return sid

    def add_postings(
        self,
        source_id: int,
        supply_id: str,
        posting_numbers: List[str],
    ) -> None:
        sid = str(supply_id or "").strip()
        nums = [str(x).strip() for x in posting_numbers if str(x).strip()]
        if not sid or not nums:
            return
        now = utc_now()
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT posting_numbers_json FROM ozon_fbs_supplies
                WHERE source_id = ? AND supply_id = ?
                """,
                (source_id, sid),
            ).fetchone()
            if not row:
                raise RuntimeError("Поставка {} не найдена".format(sid))
            existing = [
                str(x).strip()
                for x in _parse_json_list(row["posting_numbers_json"])
                if str(x).strip()
            ]
            merged = existing[:]
            for n in nums:
                if n not in merged:
                    merged.append(n)
            conn.execute(
                """
                UPDATE ozon_fbs_supplies
                SET posting_numbers_json = ?, updated_at = ?
                WHERE source_id = ? AND supply_id = ?
                """,
                (json.dumps(merged, ensure_ascii=False), now, source_id, sid),
            )
            placeholders = ", ".join("?" for _ in nums)
            conn.execute(
                """
                UPDATE ozon_fbs_postings
                SET supply_id = ?, tab = ?, status = ?, synced_at = ?
                WHERE source_id = ? AND posting_number IN ({})
                """.format(placeholders),
                [sid, TAB_AWAITING_DELIVER, TAB_AWAITING_DELIVER, now, source_id]
                + nums,
            )
            conn.commit()
        self._detach_postings_from_other_supplies(source_id, sid, nums)

    def _detach_postings_from_other_supplies(
        self,
        source_id: int,
        keep_supply_id: str,
        posting_numbers: List[str],
    ) -> None:
        """Remove postings from other open supplies' JSON; delete emptied shells."""
        sid = str(keep_supply_id or "").strip()
        nums = [str(x).strip() for x in posting_numbers if str(x).strip()]
        if not sid or not nums:
            return
        now = utc_now()
        num_set = set(nums)
        emptied = []  # type: List[str]
        with self.db.connect() as conn:
            others = conn.execute(
                """
                SELECT supply_id, posting_numbers_json FROM ozon_fbs_supplies
                WHERE source_id = ? AND supply_id != ? AND done = 0
                """,
                (source_id, sid),
            ).fetchall()
            for other in others:
                oid = str(other["supply_id"] or "").strip()
                if not oid:
                    continue
                prev = [
                    str(x).strip()
                    for x in _parse_json_list(other["posting_numbers_json"])
                    if str(x).strip()
                ]
                if not prev or not num_set.intersection(prev):
                    continue
                kept = [x for x in prev if x not in num_set]
                conn.execute(
                    """
                    UPDATE ozon_fbs_supplies
                    SET posting_numbers_json = ?, updated_at = ?
                    WHERE source_id = ? AND supply_id = ?
                    """,
                    (json.dumps(kept, ensure_ascii=False), now, source_id, oid),
                )
                if not kept:
                    emptied.append(oid)
            conn.commit()
        if emptied:
            self._delete_empty_open_supplies(source_id, emptied)

    def _delete_empty_open_supplies(
        self, source_id: int, supply_ids: List[str]
    ) -> None:
        """Delete open supplies that have no live postings and empty JSON."""
        ids = [str(x).strip() for x in supply_ids if str(x).strip()]
        if not ids:
            return
        with self.db.connect() as conn:
            for sid in ids:
                live = conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM ozon_fbs_postings
                    WHERE source_id = ? AND supply_id = ?
                    """,
                    (source_id, sid),
                ).fetchone()
                if int(live["c"] if live else 0) > 0:
                    continue
                row = conn.execute(
                    """
                    SELECT posting_numbers_json, done, delivering_listed
                    FROM ozon_fbs_supplies
                    WHERE source_id = ? AND supply_id = ?
                    """,
                    (source_id, sid),
                ).fetchone()
                if not row:
                    continue
                if int(row["done"] or 0) or int(row["delivering_listed"] or 0):
                    continue
                leftover = [
                    str(x).strip()
                    for x in _parse_json_list(row["posting_numbers_json"])
                    if str(x).strip()
                ]
                # Only delete when JSON is empty or every listed posting is gone.
                if leftover:
                    still = conn.execute(
                        """
                        SELECT COUNT(*) AS c FROM ozon_fbs_postings
                        WHERE source_id = ? AND posting_number IN ({})
                          AND supply_id = ?
                        """.format(", ".join("?" for _ in leftover)),
                        [source_id] + leftover + [sid],
                    ).fetchone()
                    if int(still["c"] if still else 0) > 0:
                        continue
                conn.execute(
                    """
                    DELETE FROM ozon_fbs_supplies
                    WHERE source_id = ? AND supply_id = ?
                    """,
                    (source_id, sid),
                )
            conn.commit()

    def mark_delivering(self, source_id: int, supply_id: str) -> None:
        sid = str(supply_id or "").strip()
        if not sid:
            return
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE ozon_fbs_supplies
                SET delivering_listed = 1, updated_at = ?
                WHERE source_id = ? AND supply_id = ?
                """,
                (now, source_id, sid),
            )
            conn.execute(
                """
                UPDATE ozon_fbs_postings
                SET tab = 'delivering', synced_at = ?
                WHERE source_id = ? AND supply_id = ?
                  AND tab = ?
                """,
                (now, source_id, sid, TAB_AWAITING_DELIVER),
            )
            conn.commit()

    def move_to_awaiting_deliver(self, source_id: int, supply_id: str) -> Dict[str, Any]:
        """Local reverse of mark_delivering — archive move_supply_to_awaiting_deliver."""
        sid = str(supply_id or "").strip()
        if not sid:
            raise ValueError("Укажите supply_id")
        if not self.get_supply(source_id, sid):
            raise RuntimeError("Поставка не найдена")
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE ozon_fbs_supplies
                SET delivering_listed = 0, done = 0, updated_at = ?
                WHERE source_id = ? AND supply_id = ?
                """,
                (now, source_id, sid),
            )
            cur = conn.execute(
                """
                UPDATE ozon_fbs_postings
                SET tab = ?, status = ?, synced_at = ?
                WHERE source_id = ? AND supply_id = ?
                  AND tab = 'delivering'
                """,
                (TAB_AWAITING_DELIVER, TAB_AWAITING_DELIVER, now, source_id, sid),
            )
            moved = int(cur.rowcount or 0)
            conn.commit()
        return {
            "ok": True,
            "supply_id": sid,
            "moved": moved,
            "message": "Поставка возвращена в «Ожидают отгрузки»",
        }

    def list_move_targets(
        self, source_id: int, *, exclude_supply_id: str = ""
    ) -> List[Dict[str, Any]]:
        """Awaiting-deliver supplies eligible for local posting move."""
        exclude = str(exclude_supply_id or "").strip()
        out = []  # type: List[Dict[str, Any]]
        for s in self.list_awaiting_deliver_supplies(source_id, adopt_orphans=False):
            sid = str(s.get("supply_id") or "").strip()
            if not sid or sid == exclude:
                continue
            if int(s.get("delivering_listed") or 0):
                continue
            out.append(s)
        return out

    def move_postings_to_local_supply(
        self,
        source_id: int,
        posting_numbers: List[str],
        supply_id: str,
        *,
        target_tab: str = TAB_AWAITING_DELIVER,
    ) -> Dict[str, Any]:
        """Locally reassign postings to an existing supply (no Ozon API)."""
        sid = str(supply_id or "").strip()
        nums = []  # type: List[str]
        seen = set()  # type: Set[str]
        for raw in posting_numbers or []:
            pn = str(raw or "").strip()
            if not pn or pn in seen:
                continue
            seen.add(pn)
            nums.append(pn)
        if not sid:
            raise ValueError("Укажите supply_id")
        if not nums:
            raise ValueError("Укажите posting_numbers")
        if not self.get_supply(source_id, sid):
            raise RuntimeError("Поставка не найдена")
        tab = str(target_tab or TAB_AWAITING_DELIVER).strip() or TAB_AWAITING_DELIVER
        now = utc_now()
        to_move = []  # type: List[str]
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT posting_numbers_json FROM ozon_fbs_supplies
                WHERE source_id = ? AND supply_id = ?
                """,
                (source_id, sid),
            ).fetchone()
            if not row:
                raise RuntimeError("Поставка не найдена")
            existing = [
                str(x).strip()
                for x in _parse_json_list(row["posting_numbers_json"])
                if str(x).strip()
            ]
            from_by_supply = {}  # type: Dict[str, List[str]]
            placeholders = ", ".join("?" for _ in nums)
            rows = conn.execute(
                """
                SELECT posting_number, supply_id
                FROM ozon_fbs_postings
                WHERE source_id = ? AND posting_number IN ({})
                """.format(placeholders),
                [source_id] + nums,
            ).fetchall()
            found = {str(r["posting_number"]): str(r["supply_id"] or "") for r in rows}
            for pn in nums:
                old = found.get(pn)
                if old is None:
                    continue
                if old == sid:
                    continue
                to_move.append(pn)
                if old:
                    from_by_supply.setdefault(old, []).append(pn)
            if to_move:
                ph = ", ".join("?" for _ in to_move)
                conn.execute(
                    """
                    UPDATE ozon_fbs_postings
                    SET supply_id = ?, tab = ?, status = ?, synced_at = ?
                    WHERE source_id = ? AND posting_number IN ({})
                    """.format(ph),
                    [sid, tab, tab, now, source_id] + to_move,
                )
                merged = existing[:]
                for pn in to_move:
                    if pn not in merged:
                        merged.append(pn)
                conn.execute(
                    """
                    UPDATE ozon_fbs_supplies
                    SET posting_numbers_json = ?, updated_at = ?
                    WHERE source_id = ? AND supply_id = ?
                    """,
                    (json.dumps(merged, ensure_ascii=False), now, source_id, sid),
                )
                for old_sid, leave_nums in from_by_supply.items():
                    old_row = conn.execute(
                        """
                        SELECT posting_numbers_json FROM ozon_fbs_supplies
                        WHERE source_id = ? AND supply_id = ?
                        """,
                        (source_id, old_sid),
                    ).fetchone()
                    if not old_row:
                        continue
                    leave = set(leave_nums)
                    kept = [
                        str(x).strip()
                        for x in _parse_json_list(old_row["posting_numbers_json"])
                        if str(x).strip() and str(x).strip() not in leave
                    ]
                    conn.execute(
                        """
                        UPDATE ozon_fbs_supplies
                        SET posting_numbers_json = ?, updated_at = ?
                        WHERE source_id = ? AND supply_id = ?
                        """,
                        (
                            json.dumps(kept, ensure_ascii=False),
                            now,
                            source_id,
                            old_sid,
                        ),
                    )
            conn.commit()
        return {
            "ok": True,
            "supply_id": sid,
            "moved": len(to_move),
            "message": "Перенесено в поставку: {}".format(len(to_move)),
        }

    def create_local_supply_from_postings(
        self,
        source_id: int,
        posting_numbers: List[str],
        *,
        name: str = "",
        target_tab: str = TAB_AWAITING_DELIVER,
    ) -> Dict[str, Any]:
        """Create a local supply and move selected postings into it (no ship)."""
        nums = [str(x).strip() for x in (posting_numbers or []) if str(x).strip()]
        if not nums:
            raise ValueError("Укажите posting_numbers")
        wh_id = None
        wh_name = ""
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT warehouse_id, warehouse_name FROM ozon_fbs_postings
                WHERE source_id = ? AND posting_number = ?
                """,
                (source_id, nums[0]),
            ).fetchone()
            if row:
                try:
                    wh_id = int(row["warehouse_id"] or 0) or None
                except (TypeError, ValueError):
                    wh_id = None
                wh_name = str(row["warehouse_name"] or "").strip()
        existing_names = {
            str(s.get("name") or "").strip()
            for s in self.list_open_supplies(source_id)
            if str(s.get("name") or "").strip()
        }
        base = str(name or "").strip() or default_supply_name(
            source_name=self.source_display_name(source_id)
        )
        supply_name = unique_supply_name(base, existing_names)
        # Create empty, then move (keeps JSON + from-supply cleanup consistent).
        sid = self.create_supply(
            source_id,
            name=supply_name,
            warehouse_id=wh_id,
            warehouse_name=wh_name,
            posting_numbers=[],
            force_tab=target_tab,
        )
        moved = self.move_postings_to_local_supply(
            source_id, nums, sid, target_tab=target_tab
        )
        return {
            "ok": True,
            "supply_id": sid,
            "name": supply_name,
            "moved": int(moved.get("moved") or 0),
            "message": "Создана поставка «{}» · перенесено {}".format(
                supply_name, int(moved.get("moved") or 0)
            ),
        }

    def sticker_groups_by_article(
        self, source_id: int, supply_id: str
    ) -> List[Dict[str, Any]]:
        """Archive sticker-groups: group supply postings by offer_id/article."""
        detail = self.get_detail(source_id, supply_id)
        groups = {}  # type: Dict[str, Dict[str, Any]]
        for order in detail.get("orders") or []:
            if str(order.get("status") or "").lower() in ("cancelled", "not_accepted"):
                continue
            if str(order.get("tab") or "").lower() == "cancelled":
                continue
            pn = str(order.get("posting_number") or "").strip()
            if not pn:
                continue
            article = str(
                order.get("offer_id") or order.get("article") or order.get("sku") or ""
            ).strip()
            name = str(
                order.get("product_name_display")
                or order.get("product_name")
                or article
                or "—"
            ).strip()
            key = article or name or pn
            bucket = groups.get(key)
            if not bucket:
                bucket = {
                    "group_key": key,
                    "article": article,
                    "product_name": name,
                    "qty": 0,
                    "posting_numbers": [],
                    "order_ids": [],
                }
                groups[key] = bucket
            bucket["qty"] = int(bucket["qty"] or 0) + 1
            bucket["posting_numbers"].append(pn)
            bucket["order_ids"].append(pn)
        out = list(groups.values())
        out.sort(
            key=lambda g: str(g.get("product_name") or "").casefold()
        )
        return out

    def rename_supply(self, source_id: int, supply_id: str, name: str) -> None:
        sid = str(supply_id or "").strip()
        new_name = str(name or "").strip()
        if not sid or not new_name:
            raise ValueError("Укажите название поставки")
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE ozon_fbs_supplies
                SET name = ?, updated_at = ?
                WHERE source_id = ? AND supply_id = ?
                """,
                (new_name, now, source_id, sid),
            )
            if cur.rowcount <= 0:
                raise RuntimeError("Поставка не найдена")
            conn.commit()

    def get_detail(
        self,
        source_id: int,
        supply_id: str,
        *,
        read_only: bool = False,
    ) -> Dict[str, Any]:
        from app.services.ozon_orders import OzonOrdersService

        supply = self.get_supply(source_id, supply_id)
        if not supply:
            raise RuntimeError("Поставка не найдена")
        rows = self.list_supply_postings(source_id, supply_id)
        orders_svc = OzonOrdersService(self.db)
        orders = [orders_svc._enrich_posting(dict(r)) for r in rows]
        active = [
            o
            for o in orders
            if str(o.get("tab") or "").lower() != "cancelled"
            and str(o.get("status") or "").lower()
            not in ("cancelled", "not_accepted")
        ]
        return {
            "supply_id": supply.get("supply_id"),
            "name": supply.get("name"),
            "warehouse_label": supply.get("warehouse_name") or "—",
            "warehouse_id": supply.get("warehouse_id"),
            "order_count": len(active),
            "orders": orders,
            "posting_numbers": [
                str(o.get("posting_number") or "")
                for o in orders
                if str(o.get("posting_number") or "").strip()
            ],
            "source_id": source_id,
            "read_only": bool(read_only)
            or bool(int(supply.get("delivering_listed") or 0)),
            "delivering_listed": int(supply.get("delivering_listed") or 0),
            "done": int(supply.get("done") or 0),
        }

    @staticmethod
    def posting_needs_ozon_product_refresh(row: Dict[str, Any]) -> bool:
        """True when local row has no usable product identity (name/SKU/offer)."""
        offer = str(row.get("offer_id") or "").strip()
        sku = str(row.get("sku") or "").strip()
        name = str(row.get("product_name") or "").strip()
        if offer or sku or (name and name != "—"):
            return False
        raw = row.get("products_json")
        if isinstance(raw, list):
            products = raw
        else:
            try:
                products = json.loads(str(raw or "[]"))
            except Exception:
                products = []
        if not isinstance(products, list):
            return True
        for p in products:
            if not isinstance(p, dict):
                continue
            if str(p.get("offer_id") or "").strip():
                return False
            if str(p.get("sku") or p.get("product_id") or "").strip():
                return False
            if str(p.get("name") or "").strip():
                return False
        return True

    def refresh_postings_from_ozon(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
        *,
        only_missing: bool = True,
        progress: Optional[Any] = None,
        stop_requested: Optional[Any] = None,
        sleep_s: float = 0.05,
    ) -> Dict[str, Any]:
        """Live ``/v3/posting/fbs/get`` hydrate (web ``refresh_supply_postings_from_ozon``).

        Used when opening a supply so empty product/SKU/barcode rows refill from Ozon.
        Keeps local ``supply_id`` (upsert does not clear it).
        """
        from app.ozon.sync import upsert_posting

        nums = [str(x).strip() for x in (posting_numbers or []) if str(x).strip()]
        if not nums:
            return {"ok": True, "refreshed": 0, "skipped": 0, "failed": 0, "total": 0}
        need = []  # type: List[str]
        if only_missing:
            with self.db.connect() as conn:
                for pn in nums:
                    if stop_requested and stop_requested():
                        break
                    row = conn.execute(
                        """
                        SELECT offer_id, sku, product_name, products_json
                        FROM ozon_fbs_postings
                        WHERE source_id = ? AND posting_number = ?
                        """,
                        (source_id, pn),
                    ).fetchone()
                    if row is None or self.posting_needs_ozon_product_refresh(
                        Database.row_to_dict(row)
                    ):
                        need.append(pn)
        else:
            need = list(nums)
        total = len(need)
        refreshed = 0
        failed = 0
        skipped = len(nums) - total
        if progress:
            progress(0, total, "Загрузка товаров из Ozon… 0/{}".format(total))
        for i, pn in enumerate(need):
            if stop_requested and stop_requested():
                break
            try:
                remote = client.get_posting(pn)
                if isinstance(remote, dict) and remote:
                    upsert_posting(self.db, source_id, remote)
                    refreshed += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                _log.warning("ozon supply detail refresh %s: %s", pn, exc)
            if progress:
                progress(
                    i + 1,
                    total,
                    "Загрузка товаров из Ozon… {}/{}".format(i + 1, total),
                )
            if sleep_s and i + 1 < total:
                time.sleep(float(sleep_s))
        return {
            "ok": failed == 0,
            "refreshed": refreshed,
            "skipped": skipped,
            "failed": failed,
            "total": total,
        }

    def bind_stickers_from_postings(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
    ) -> int:
        """Fill empty sticker_* from Ozon get barcodes (web safety-net)."""
        from app.ozon.sync import upsert_posting

        nums = [str(p).strip() for p in posting_numbers if str(p).strip()]
        updated = 0
        for pn in nums:
            with self.db.connect() as conn:
                row = conn.execute(
                    """
                    SELECT sticker_barcode, sticker_lower_barcode
                    FROM ozon_fbs_postings
                    WHERE source_id = ? AND posting_number = ?
                    """,
                    (source_id, pn),
                ).fetchone()
            if row and (
                str(row["sticker_barcode"] or "").strip()
                or str(row["sticker_lower_barcode"] or "").strip()
            ):
                continue
            try:
                fresh = client.get_posting(pn)
            except Exception as exc:
                _log.warning("ozon sticker bind get %s: %s", pn, exc)
                continue
            if not fresh:
                continue
            upsert_posting(self.db, source_id, fresh)
            updated += 1
        return updated

    def render_picking_list_html(self, detail: Dict[str, Any]) -> str:
        orders = [
            o
            for o in (detail.get("orders") or [])
            if str(o.get("tab") or "").lower() != "cancelled"
            and str(o.get("status") or "").lower()
            not in ("cancelled", "not_accepted")
        ]
        groups = {}  # type: Dict[str, int]
        for o in orders:
            name = str(
                o.get("product_name") or o.get("offer_id") or o.get("sku") or "—"
            ).strip()
            try:
                qty = max(1, int(o.get("quantity") or 1))
            except (TypeError, ValueError):
                qty = 1
            groups[name] = groups.get(name, 0) + qty
        total = sum(groups.values())
        sid = _esc(detail.get("supply_id"))
        name = _esc(detail.get("name") or sid)
        rows_html = []
        for pname, qty in sorted(groups.items(), key=lambda x: x[0].lower()):
            rows_html.append(
                '<tr class="summary-row"><td class="main">{} — {} шт.</td>'
                '<td class="check"><span class="box"></span></td></tr>'.format(
                    _esc(pname), qty
                )
            )
        body = (
            "\n".join(rows_html)
            if rows_html
            else '<tr><td colspan="2">Нет заказов</td></tr>'
        )
        return """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"/>
<title>Лист подбора {name}</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  body {{ font-family: Arial, sans-serif; color: #0f172a; font-size: 14px; }}
  h1 {{ font-size: 18px; margin: 0 0 8px; }}
  .meta {{ color: #64748b; margin-bottom: 16px; }}
  table.picking {{ width: 100%; border-collapse: collapse; }}
  table.picking th, table.picking td {{
    border-bottom: 1px solid #e2e8f0; padding: 8px 12px; text-align: left;
  }}
  table.picking .check {{ width: 88px; text-align: center; }}
  .box {{
    display: inline-block; width: 16px; height: 16px;
    border: 1.5px solid #94a3b8; border-radius: 3px;
  }}
  .totals-row th {{ background: #f8fafc; font-weight: 700; }}
  .toolbar {{ margin-bottom: 12px; }}
  @media print {{ .toolbar {{ display: none !important; }} }}
</style></head><body>
  <div class="toolbar"><button onclick="window.print()">Печать</button></div>
  <h1>Лист подбора — {name}</h1>
  <div class="meta">Поставка {sid} · склад {wh}</div>
  <table class="picking summary">
    <tbody>
      <tr class="totals-row"><th class="main">Всего {total} отпр.</th>
      <th class="check">Собрано</th></tr>
      {body}
    </tbody>
  </table>
  <script>window.addEventListener('load',function(){{
    setTimeout(function(){{ window.print(); }}, 200); }});</script>
</body></html>""".format(
            name=name,
            sid=sid,
            wh=_esc(detail.get("warehouse_label")),
            total=total,
            body=body,
        )

    def preview_selection_supply(
        self, source_id: int, posting_numbers: List[str]
    ) -> Dict[str, Any]:
        """Validate selected packaging postings + list compatible open supplies."""
        from app.services.ozon_split import posting_unit_count

        wanted = [str(x).strip() for x in posting_numbers if str(x).strip()]
        rows = []  # type: List[Dict[str, Any]]
        found = set()  # type: Set[str]
        for pn in wanted:
            with self.db.connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM ozon_fbs_postings
                    WHERE source_id = ? AND posting_number = ?
                    """,
                    (source_id, pn),
                ).fetchone()
            if row:
                d = Database.row_to_dict(row)
                rows.append(d)
                found.add(pn)
        errors = []  # type: List[str]
        missing = [n for n in wanted if n not in found]
        if missing:
            errors.append(
                "Не найдены отправления: "
                + ", ".join(missing[:10])
                + ("…" if len(missing) > 10 else "")
            )
        usable = []  # type: List[Dict[str, Any]]
        for r in rows:
            pn = str(r.get("posting_number") or "").strip()
            tab = str(r.get("tab") or "")
            if tab not in (TAB_AWAITING_PACKAGING, "new"):
                errors.append(
                    "Отправление {} не во вкладке «Ожидают сборки» (сейчас: {})".format(
                        pn, tab or "—"
                    )
                )
                continue
            if str(r.get("supply_id") or "").strip():
                errors.append("Отправление {} уже в локальной поставке".format(pn))
                continue
            usable.append(r)
        wh_ids = {r.get("warehouse_id") for r in usable}
        if len(wh_ids) > 1:
            errors.append("Выбраны отправления с разных складов")
        if not usable and not errors:
            errors.append("Нет подходящих отправлений")
        multi = sum(1 for r in usable if posting_unit_count(r) > 1)
        if multi > 0:
            errors.append(
                "Сначала разделите мультизаказы в выборе ({} шт.)".format(multi)
            )
        traits = {"warehouse_id": None, "warehouse_name": ""}  # type: Dict[str, Any]
        compatible = []  # type: List[Dict[str, Any]]
        if usable and len(wh_ids) <= 1 and multi == 0:
            first = usable[0]
            traits = {
                "warehouse_id": first.get("warehouse_id"),
                "warehouse_name": str(first.get("warehouse_name") or ""),
            }
            for s in self.list_awaiting_deliver_supplies(source_id):
                sw = s.get("warehouse_id")
                tw = traits.get("warehouse_id")
                if tw is None and sw is None:
                    compatible.append(s)
                elif tw is not None and sw is not None and int(sw) == int(tw):
                    compatible.append(s)
        open_names = sorted(
            {
                str(s.get("name") or "").strip()
                for s in self.list_open_supplies(source_id)
                if str(s.get("name") or "").strip()
            }
        )
        suggested = unique_supply_name(
            default_supply_name(source_name=self.source_display_name(source_id)),
            set(open_names),
        )
        # Deduplicate errors
        seen = set()  # type: Set[str]
        uniq = []  # type: List[str]
        for e in errors:
            if e in seen:
                continue
            seen.add(e)
            uniq.append(e)
        return {
            "ok": not uniq,
            "errors": uniq,
            "posting_numbers": [
                str(r.get("posting_number") or "").strip() for r in usable
            ],
            "order_count": len(usable),
            "multi_posting_count": multi,
            "block_collect": multi > 0,
            "traits": traits,
            "suggested_name": suggested,
            "existing_names": open_names,
            "compatible_supplies": [
                {
                    "supply_id": s.get("supply_id"),
                    "name": s.get("name") or s.get("supply_id"),
                    "orders_count": int(s.get("posting_count") or s.get("order_count") or 0),
                    "warehouse_name": s.get("warehouse_name") or "",
                }
                for s in compatible
            ],
            "has_open_supplies": bool(compatible),
        }

    def create_from_selection(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
        *,
        name: str = "",
    ) -> Dict[str, Any]:
        from app.services.ozon_ship import OzonShipService, posting_needs_ship

        preview = self.preview_selection_supply(source_id, posting_numbers)
        if not preview.get("ok"):
            return {
                "ok": False,
                "added": 0,
                "errors": list(preview.get("errors") or []),
                "message": "Нельзя создать поставку из выбранных отправлений",
                "goto_awaiting_deliver": False,
            }
        nums = list(preview.get("posting_numbers") or [])
        traits = preview.get("traits") or {}
        ship_svc = OzonShipService(self.db)
        shipped = []  # type: List[str]
        errors = []  # type: List[str]
        for pn in nums:
            try:
                with self.db.connect() as conn:
                    row = conn.execute(
                        """
                        SELECT * FROM ozon_fbs_postings
                        WHERE source_id = ? AND posting_number = ?
                        """,
                        (source_id, pn),
                    ).fetchone()
                local = Database.row_to_dict(row) if row else {}
                if posting_needs_ship(str(local.get("status") or "")):
                    ship_svc.ship_posting(client, source_id, pn, posting=local or None)
                shipped.append(pn)
            except Exception as exc:
                errors.append("{}: {}".format(pn, exc))
        if not shipped:
            return {
                "ok": False,
                "added": 0,
                "errors": errors,
                "message": "Не удалось собрать отправления",
                "goto_awaiting_deliver": False,
            }
        open_names = set(preview.get("existing_names") or [])
        supply_name = unique_supply_name(
            str(name or "").strip() or str(preview.get("suggested_name") or ""),
            open_names,
        )
        sid = self.create_supply(
            source_id,
            name=supply_name,
            warehouse_id=traits.get("warehouse_id"),
            warehouse_name=str(traits.get("warehouse_name") or ""),
            posting_numbers=shipped,
        )
        return {
            "ok": not errors,
            "supply_id": sid,
            "name": supply_name,
            "added": len(shipped),
            "errors": errors,
            "message": "Создана поставка «{}»: {} отпр.".format(
                supply_name, len(shipped)
            )
            + (" · ошибок {}".format(len(errors)) if errors else ""),
            "goto_awaiting_deliver": True,
        }

    def add_selection_to_supply(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
        supply_id: str,
    ) -> Dict[str, Any]:
        from app.services.ozon_ship import OzonShipService, posting_needs_ship

        preview = self.preview_selection_supply(source_id, posting_numbers)
        if not preview.get("ok"):
            return {
                "ok": False,
                "added": 0,
                "errors": list(preview.get("errors") or []),
                "message": "Нельзя добавить выбранные отправления в поставку",
                "goto_awaiting_deliver": False,
            }
        sid = str(supply_id or "").strip()
        compatible_ids = {
            str(s.get("supply_id") or "").strip()
            for s in (preview.get("compatible_supplies") or [])
        }
        if sid not in compatible_ids:
            return {
                "ok": False,
                "added": 0,
                "errors": ["Выбранная поставка не совместима со складом отправлений"],
                "message": "Поставка не подходит",
                "goto_awaiting_deliver": False,
            }
        nums = list(preview.get("posting_numbers") or [])
        ship_svc = OzonShipService(self.db)
        shipped = []  # type: List[str]
        errors = []  # type: List[str]
        for pn in nums:
            try:
                with self.db.connect() as conn:
                    row = conn.execute(
                        """
                        SELECT * FROM ozon_fbs_postings
                        WHERE source_id = ? AND posting_number = ?
                        """,
                        (source_id, pn),
                    ).fetchone()
                local = Database.row_to_dict(row) if row else {}
                if posting_needs_ship(str(local.get("status") or "")):
                    ship_svc.ship_posting(client, source_id, pn, posting=local or None)
                shipped.append(pn)
            except Exception as exc:
                errors.append("{}: {}".format(pn, exc))
        if not shipped:
            return {
                "ok": False,
                "added": 0,
                "errors": errors,
                "message": "Не удалось собрать отправления",
                "goto_awaiting_deliver": False,
            }
        self.add_postings(source_id, sid, shipped)
        sname = next(
            (
                str(s.get("name") or sid)
                for s in (preview.get("compatible_supplies") or [])
                if str(s.get("supply_id")) == sid
            ),
            sid,
        )
        return {
            "ok": not errors,
            "added": len(shipped),
            "supply_id": sid,
            "name": sname,
            "errors": errors,
            "message": "Добавлено в «{}»: {} отпр.".format(sname, len(shipped))
            + (" · ошибок {}".format(len(errors)) if errors else ""),
            "goto_awaiting_deliver": True,
        }

    def list_cancelled_in_supply(
        self, source_id: int, supply_id: str
    ) -> List[Dict[str, Any]]:
        from app.services.ozon_orders import OzonOrdersService

        rows = self.list_supply_postings(source_id, supply_id)
        orders_svc = OzonOrdersService(self.db)
        out = []  # type: List[Dict[str, Any]]
        for r in rows:
            item = orders_svc._enrich_posting(dict(r))
            tab = str(item.get("tab") or "").lower()
            status = str(item.get("status") or "").lower()
            cancel = str(item.get("cancel_reason") or "").strip()
            if (
                tab == "cancelled"
                or status in ("cancelled", "not_accepted")
                or cancel
            ):
                out.append(item)
        return out

    def render_stickers_sheet_html(self, detail: Dict[str, Any]) -> str:
        """Printable sticker barcode sheet (desktop stand-in for web image pages)."""
        orders = [
            o
            for o in (detail.get("orders") or [])
            if str(o.get("tab") or "").lower() != "cancelled"
            and str(o.get("status") or "").lower()
            not in ("cancelled", "not_accepted")
        ]
        cards = []
        for o in orders:
            pn = _esc(o.get("posting_number"))
            upper = str(o.get("sticker_barcode") or "").strip()
            lower = str(o.get("sticker_lower_barcode") or "").strip()
            if not upper and not lower:
                bcs = o.get("barcodes") or []
                if isinstance(bcs, list) and bcs:
                    upper = str(bcs[0] or "")
                    if len(bcs) > 1:
                        lower = str(bcs[1] or "")
            name = _esc(o.get("product_name") or o.get("offer_id") or "—")
            cards.append(
                """
<div class="card">
  <div class="pn">{pn}</div>
  <div class="name">{name}</div>
  <div class="bc">{esc_u}</div>
  <div class="bc2">{esc_l}</div>
</div>""".format(
                    pn=pn,
                    name=name,
                    esc_u=_esc(upper) or "— нет QR —",
                    esc_l=_esc(lower) if lower else "",
                )
            )
        body = "\n".join(cards) if cards else "<p>Нет отправлений</p>"
        return """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"/>
<title>Стикеры {name}</title>
<style>
  @page {{ size: A4; margin: 8mm; }}
  body {{ font-family: Arial, sans-serif; color: #0f172a; }}
  h1 {{ font-size: 16px; margin: 0 0 8px; }}
  .meta {{ color: #64748b; margin-bottom: 12px; font-size: 13px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .card {{
    border: 1px solid #cbd5e1; border-radius: 8px; padding: 10px 12px;
    break-inside: avoid; min-height: 88px;
  }}
  .pn {{ font-weight: 700; font-size: 13px; }}
  .name {{ font-size: 12px; color: #334155; margin: 4px 0 8px; }}
  .bc {{ font-family: Consolas, monospace; font-size: 14px; letter-spacing: 0.04em; }}
  .bc2 {{ font-family: Consolas, monospace; font-size: 12px; color: #64748b; margin-top: 4px; }}
  .toolbar {{ margin-bottom: 10px; }}
  @media print {{ .toolbar {{ display: none !important; }} }}
</style></head><body>
  <div class="toolbar"><button onclick="window.print()">Печать</button></div>
  <h1>Стикеры / QR — {name}</h1>
  <div class="meta">Поставка {sid} · {count} отпр. · PDF этикеток Ozon печатайте отдельно кнопкой «Этикетки»</div>
  <div class="grid">{body}</div>
  <script>window.addEventListener('load',function(){{
    setTimeout(function(){{ window.print(); }}, 250); }});</script>
</body></html>""".format(
            name=_esc(detail.get("name") or detail.get("supply_id")),
            sid=_esc(detail.get("supply_id")),
            count=len(orders),
            body=body,
        )

    def preview_ship_all(self, source_id: int) -> Dict[str, Any]:
        from app.services.ozon_split import posting_unit_count

        rows = self._load_awaiting_packaging(source_id)
        multi = sum(1 for r in rows if posting_unit_count(r) > 1)
        # Web parity: multi-unit postings must be split before ship-all.
        if multi > 0:
            return {
                "ok": False,
                "block_collect": True,
                "posting_count": len(rows),
                "multi_posting_count": multi,
                "groups": [],
                "message": "Сначала разделите мультизаказы ({} шт.)".format(multi),
                "source_name": self.source_display_name(source_id),
            }

        buckets = {}  # type: Dict[Any, List[Dict[str, Any]]]
        for row in rows:
            wh = row.get("warehouse_id")
            buckets.setdefault(wh, []).append(row)

        open_supplies = self.list_open_supplies(source_id)
        existing_names = {
            str(s.get("name") or "").strip()
            for s in open_supplies
            if str(s.get("name") or "").strip()
        }
        reserved = set(existing_names)
        source_name = self.source_display_name(source_id)
        groups = []  # type: List[Dict[str, Any]]
        for warehouse_id in sorted(buckets.keys(), key=lambda x: (x is None, str(x))):
            bucket = buckets[warehouse_id]
            posting_numbers = [
                str(r.get("posting_number") or "").strip()
                for r in bucket
                if str(r.get("posting_number") or "").strip()
            ]
            wh_name = (
                str(bucket[0].get("warehouse_name") or "").strip() if bucket else ""
            )
            live_counts = self._order_counts_by_tab(
                source_id, TAB_AWAITING_DELIVER
            )
            matching = []
            for s in open_supplies:
                sid = str(s.get("supply_id") or "")
                # Prefer live rows over stale posting_numbers_json.
                if int(live_counts.get(sid, 0) or 0) <= 0:
                    continue
                if int(s.get("delivering_listed") or 0):
                    continue
                sw = s.get("warehouse_id")
                if warehouse_id is None and sw is None:
                    matching.append(s)
                elif (
                    warehouse_id is not None
                    and sw is not None
                    and int(sw) == int(warehouse_id)
                ):
                    matching.append(s)
            suggested = unique_supply_name(
                default_supply_name(source_name=source_name), reserved
            )
            reserved.add(suggested)
            group_key = "wh{}".format(
                warehouse_id if warehouse_id is not None else "none"
            )
            if matching:
                mode = "choose"
                default_supply_id = str(matching[0].get("supply_id") or "")
            else:
                mode = "create"
                default_supply_id = ""
            groups.append(
                {
                    "group_key": group_key,
                    "warehouse_id": warehouse_id,
                    "warehouse_name": wh_name,
                    "label": wh_name
                    or (
                        "склад {}".format(warehouse_id)
                        if warehouse_id is not None
                        else "склад —"
                    ),
                    "posting_numbers": posting_numbers,
                    "order_count": len(posting_numbers),
                    "suggested_name": suggested,
                    "mode": mode,
                    "default_supply_id": default_supply_id,
                    "compatible_supplies": [
                        {
                            "supply_id": s.get("supply_id"),
                            "name": s.get("name") or s.get("supply_id"),
                            "orders_count": int(s.get("order_count") or 0),
                        }
                        for s in matching
                    ],
                }
            )
        return {
            "ok": True,
            "block_collect": False,
            "posting_count": len(rows),
            "multi_posting_count": multi,
            "groups": groups,
            "needs_modal": bool(groups),
            "existing_names": sorted(existing_names),
            "source_name": source_name,
            "message": "",
        }

    def execute_ship_all(
        self,
        source_id: int,
        client: OzonFbsClient,
        *,
        decisions: Optional[List[Dict[str, Any]]] = None,
        progress: ProgressCb = None,
        stop_requested: StopCb = None,
        ship_pause_sec: float = OZON_FBS_SHIP_ALL_PAUSE_SEC,
    ) -> Dict[str, Any]:
        preview = self.preview_ship_all(source_id)
        if preview.get("block_collect"):
            msg = str(preview.get("message") or "Сначала разделите мультизаказы")
            if progress:
                progress(0, 0, msg)
            return {
                "ok": False,
                "total": 0,
                "shipped": 0,
                "failed": 0,
                "created_supplies": [],
                "errors": [{"posting_number": "—", "error": msg}],
                "message": msg,
                "goto_awaiting_deliver": False,
            }
        groups_by_key = {
            str(g.get("group_key")): g for g in (preview.get("groups") or [])
        }
        if not groups_by_key:
            msg = "Нет отправлений в «Ожидают сборки»"
            if progress:
                progress(0, 0, msg)
            return {
                "ok": True,
                "total": 0,
                "shipped": 0,
                "failed": 0,
                "created_supplies": [],
                "errors": [],
                "message": msg,
                "goto_awaiting_deliver": False,
            }

        if not decisions:
            decisions = []
            for g in preview.get("groups") or []:
                decisions.append(
                    {
                        "group_key": g.get("group_key"),
                        "action": "create",
                        "name": g.get("suggested_name"),
                    }
                )

        ship_svc = OzonShipService(self.db)
        open_names = {
            str(s.get("name") or "").strip()
            for s in self.list_open_supplies(source_id)
            if str(s.get("name") or "").strip()
        }
        work = []  # type: List[Any]
        for dec in decisions:
            if not isinstance(dec, dict):
                continue
            gkey = str(dec.get("group_key") or "").strip()
            group = groups_by_key.get(gkey)
            if not group:
                continue
            pns = [
                str(x).strip()
                for x in (group.get("posting_numbers") or [])
                if str(x).strip()
            ]
            if pns:
                work.append((dec, group, pns))
        total = sum(len(pns) for _, _, pns in work)
        done = 0
        shipped = []  # type: List[str]
        errors = []  # type: List[Dict[str, str]]
        created = []  # type: List[Dict[str, str]]
        if progress:
            progress(0, total, "Сборка… 0 из {}".format(total))

        for dec, group, posting_numbers in work:
            if stop_requested and stop_requested():
                break
            action = str(dec.get("action") or "create").strip().lower()
            if action == "add_one":
                action = "add"
            wh_id = group.get("warehouse_id")
            wh_name = str(group.get("warehouse_name") or "")
            # Resolve target supply BEFORE shipping so mid-collect orphans
            # never spawn empty «· добор» supplies on UI refresh.
            target_sid = ""
            target_name = ""
            try:
                if action == "add" and dec.get("supply_id"):
                    target_sid = str(dec.get("supply_id") or "").strip()
                    target_name = str(
                        dec.get("name") or dec.get("supply_id") or target_sid
                    )
                    if not target_sid:
                        raise RuntimeError("Не выбрана поставка для добавления")
                else:
                    target_name = unique_supply_name(
                        str(dec.get("name") or group.get("suggested_name") or ""),
                        open_names,
                    )
                    target_sid = self.create_supply(
                        source_id,
                        name=target_name,
                        warehouse_id=wh_id,
                        warehouse_name=wh_name,
                        posting_numbers=[],
                    )
                    open_names.add(target_name)
                created.append({"supply_id": target_sid, "name": target_name})
            except Exception as exc:
                errors.append(
                    {
                        "posting_number": posting_numbers[0] if posting_numbers else "—",
                        "error": "Поставка: {}".format(exc),
                    }
                )
                done += len(posting_numbers)
                if progress:
                    progress(
                        done,
                        total,
                        "Сборка… {} из {}".format(done, total),
                    )
                continue

            shipped_here = []  # type: List[str]
            for pn in posting_numbers:
                if stop_requested and stop_requested():
                    break
                try:
                    row = None
                    with self.db.connect() as conn:
                        prow = conn.execute(
                            """
                            SELECT * FROM ozon_fbs_postings
                            WHERE source_id = ? AND posting_number = ?
                            """,
                            (source_id, pn),
                        ).fetchone()
                        if prow:
                            row = Database.row_to_dict(prow)
                    status = str((row or {}).get("status") or "")
                    tab = str((row or {}).get("tab") or "")
                    if posting_needs_ship(status=status, tab=tab) or not (
                        status or tab
                    ):
                        # fast=True: web bulk collect — force awaiting_deliver locally.
                        ship_svc.ship_posting(
                            client, source_id, pn, posting=row, fast=True
                        )
                    # Attach immediately — no orphan window for adopt/добор.
                    self.add_postings(source_id, target_sid, [pn])
                    shipped.append(pn)
                    shipped_here.append(pn)
                except Exception as exc:
                    errors.append({"posting_number": pn, "error": str(exc)})
                    _log.warning("ozon ship-all %s failed: %s", pn, exc)
                done += 1
                if progress:
                    progress(
                        done,
                        total,
                        "Сборка… {} из {}".format(done, total)
                        + (
                            " · ошибок {}".format(len(errors)) if errors else ""
                        ),
                    )
                if ship_pause_sec and ship_pause_sec > 0:
                    time.sleep(float(ship_pause_sec))

            if not shipped_here and action != "add":
                # Created empty shell but nothing shipped — remove it.
                try:
                    self._delete_empty_open_supplies(source_id, [target_sid])
                except Exception:
                    pass
                created[:] = [
                    c for c in created if str(c.get("supply_id")) != target_sid
                ]

        shipped_n = len(shipped)
        failed_n = len(errors)
        ok = shipped_n > 0 and failed_n == 0
        if shipped_n and failed_n:
            message = "Собрано {}, ошибок {}".format(shipped_n, failed_n)
        elif shipped_n:
            message = "Собрано {} → «Ожидают отгрузки»".format(shipped_n)
        elif failed_n:
            message = "Не удалось собрать: ошибок {}".format(failed_n)
        else:
            message = "Нет отправлений для сборки"
        return {
            "ok": ok,
            "total": total,
            "shipped": shipped_n,
            "failed": failed_n,
            "created_supplies": created,
            "errors": errors,
            "message": message,
            "goto_awaiting_deliver": shipped_n > 0,
        }

    def tab_supply_counts(self, source_id: int) -> Dict[str, int]:
        # Never adopt orphans while counting — that created empty «добор» mid-collect.
        awaiting = len(
            self.list_awaiting_deliver_supplies(source_id, adopt_orphans=False)
        )
        delivering = len(self.list_delivering_supplies(source_id))
        return {
            "awaiting_deliver": awaiting,
            "delivering": delivering,
            "assembly": awaiting,
            "delivery": delivering,
        }

    def _enrich_supply(self, item: Dict[str, Any]) -> Dict[str, Any]:
        nums = [
            str(x).strip()
            for x in _parse_json_list(item.get("posting_numbers_json"))
            if str(x).strip()
        ]
        item["posting_numbers"] = nums
        item["order_count"] = len(nums)
        item["is_empty"] = len(nums) == 0
        return item

    def _load_awaiting_packaging(self, source_id: int) -> List[Dict[str, Any]]:
        """Rows for «Собрать все» — packaging tab and not yet in a local supply (web)."""
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_postings
                WHERE source_id = ?
                  AND tab IN ('new', 'awaiting_packaging')
                  AND LOWER(COALESCE(status, '')) = 'awaiting_packaging'
                  AND COALESCE(supply_id, '') = ''
                ORDER BY datetime(created_at_wb) DESC, posting_number DESC
                """,
                (source_id,),
            ).fetchall()
        return [Database.row_to_dict(r) for r in rows]

    def _order_counts_by_tab(
        self, source_id: int, tab: str
    ) -> Dict[str, int]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT supply_id, COUNT(*) AS order_count
                FROM ozon_fbs_postings
                WHERE source_id = ? AND tab = ?
                  AND COALESCE(supply_id, '') != ''
                  AND tab NOT IN ('cancelled')
                  AND LOWER(COALESCE(status, '')) NOT IN ('cancelled', 'not_accepted')
                GROUP BY supply_id
                """,
                (source_id, tab),
            ).fetchall()
        out = {}  # type: Dict[str, int]
        for row in rows:
            sid = str(row["supply_id"] or "").strip()
            if sid:
                out[sid] = int(row["order_count"] or 0)
        return out

    def _supply_ids_with_tab(self, source_id: int, tab: str) -> Set[str]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT supply_id FROM ozon_fbs_postings
                WHERE source_id = ? AND tab = ?
                  AND COALESCE(supply_id, '') != ''
                """,
                (source_id, tab),
            ).fetchall()
        return {
            str(r["supply_id"]).strip()
            for r in rows
            if str(r["supply_id"] or "").strip()
        }
