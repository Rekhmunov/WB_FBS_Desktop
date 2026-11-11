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

    def list_awaiting_deliver_supplies(self, source_id: int) -> List[Dict[str, Any]]:
        """Open local supplies with postings still on awaiting_deliver (or empty)."""
        open_supplies = self.list_open_supplies(source_id)
        counts = self._order_counts_by_tab(source_id, TAB_AWAITING_DELIVER)
        delivering_ids = self._supply_ids_with_tab(source_id, "delivering")
        out = []  # type: List[Dict[str, Any]]
        for s in open_supplies:
            sid = str(s.get("supply_id") or "")
            if int(s.get("delivering_listed") or 0):
                continue
            if sid in delivering_ids and counts.get(sid, 0) == 0:
                continue
            item = dict(s)
            item["posting_count"] = int(counts.get(sid) or item.get("order_count") or 0)
            item["status_label"] = "Сборка заказов"
            item["status_kind"] = "awaiting_deliver"
            out.append(item)
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
            matching = []
            for s in open_supplies:
                if int(s.get("order_count") or 0) <= 0:
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
                    if posting_needs_ship(status) or not status:
                        ship_svc.ship_posting(
                            client, source_id, pn, posting=row
                        )
                    else:
                        # Already shipped on Ozon — still attach to local supply.
                        pass
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

            if not shipped_here:
                continue
            wh_id = group.get("warehouse_id")
            wh_name = str(group.get("warehouse_name") or "")
            try:
                if action == "add" and dec.get("supply_id"):
                    self.add_postings(
                        source_id, str(dec.get("supply_id")), shipped_here
                    )
                    created.append(
                        {
                            "supply_id": str(dec.get("supply_id")),
                            "name": str(dec.get("name") or dec.get("supply_id")),
                        }
                    )
                else:
                    name = unique_supply_name(
                        str(dec.get("name") or group.get("suggested_name") or ""),
                        open_names,
                    )
                    sid = self.create_supply(
                        source_id,
                        name=name,
                        warehouse_id=wh_id,
                        warehouse_name=wh_name,
                        posting_numbers=shipped_here,
                    )
                    open_names.add(name)
                    created.append({"supply_id": sid, "name": name})
            except Exception as exc:
                errors.append(
                    {
                        "posting_number": shipped_here[0],
                        "error": "Поставка: {}".format(exc),
                    }
                )

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
        awaiting = len(self.list_awaiting_deliver_supplies(source_id))
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
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ozon_fbs_postings
                WHERE source_id = ?
                  AND tab IN ('new', 'awaiting_packaging')
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
