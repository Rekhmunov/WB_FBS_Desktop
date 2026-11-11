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
