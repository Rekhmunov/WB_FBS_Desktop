# -*- coding: utf-8 -*-
"""Collect all MGT — preview/execute like web WB FBS (manual only)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from app.db import Database
from app.services.orders import OrdersService
from app.wb import default_mgt_supply_name, parse_json_list, parse_json_obj


def _unique_supply_name(base: str, existing: Set[str]) -> str:
    name = str(base or "").strip() or "Поставка"
    if name not in existing:
        existing.add(name)
        return name
    n = 2
    while True:
        candidate = "{} ({})".format(name, n)
        if candidate not in existing:
            existing.add(candidate)
            return candidate
        n += 1


def _cross_border(row: Dict[str, Any]) -> Optional[int]:
    raw = parse_json_obj(row.get("raw_json"))
    for key in ("crossBorderType", "cross_border_type"):
        if key in raw and raw.get(key) is not None:
            try:
                return int(raw.get(key))
            except (TypeError, ValueError):
                return None
    return None


def _supply_is_empty(supply: Dict[str, Any]) -> bool:
    oids = supply.get("order_ids")
    if oids is None:
        oids = parse_json_list(supply.get("order_ids_json"))
    return len(oids or []) == 0


class CollectMgtService:
    def __init__(self, db: Database, orders: OrdersService) -> None:
        self.db = db
        self.orders = orders

    def preview(self, source_id: int) -> Dict[str, Any]:
        rows = self.orders.new_mgt_orders(source_id)
        buckets = {}  # type: Dict[Tuple[Any, ...], List[Dict[str, Any]]]
        for o in rows:
            key = (
                bool(int(o.get("is_b2b") or 0)),
                o.get("warehouse_id"),
                _cross_border(o),
            )
            buckets.setdefault(key, []).append(o)

        open_supplies, _total = self.orders.list_supplies(
            source_id, done=False, search="", limit=500, offset=0
        )
        for s in open_supplies:
            s["order_ids"] = parse_json_list(s.get("order_ids_json"))

        empties = [s for s in open_supplies if _supply_is_empty(s)]
        mgt_supplies = [
            s for s in open_supplies if int(s.get("cargo_type") or 0) == 1 and not _supply_is_empty(s)
        ]

        existing_names = {
            str(s.get("name") or "").strip()
            for s in open_supplies
            if str(s.get("name") or "").strip()
        }
        reserved = set(existing_names)

        groups = []  # type: List[Dict[str, Any]]
        ordered_keys = sorted(
            buckets.keys(), key=lambda k: (bool(k[0]), str(k[1]), str(k[2]))
        )
        for is_b2b, warehouse_id, cross_border in ordered_keys:
            bucket = buckets[(is_b2b, warehouse_id, cross_border)]
            order_ids = [int(o["order_id"]) for o in bucket]
            matching = []
            for s in mgt_supplies:
                if bool(int(s.get("is_b2b") or 0)) != bool(is_b2b):
                    continue
                # warehouse via first order
                oids = s.get("order_ids") or []
                if oids and warehouse_id is not None:
                    with self.db.connect() as conn:
                        ow = conn.execute(
                            """
                            SELECT warehouse_id FROM wb_fbs_orders
                            WHERE source_id = ? AND order_id = ?
                            """,
                            (source_id, int(oids[0])),
                        ).fetchone()
                    if ow and ow["warehouse_id"] is not None:
                        if int(ow["warehouse_id"]) != int(warehouse_id):
                            continue
                matching.append(s)

            candidates = list(matching) + list(empties)
            suggested = _unique_supply_name(
                default_mgt_supply_name(is_b2b=bool(is_b2b)), reserved
            )
            group = {
                "group_key": "{}|{}|{}".format(is_b2b, warehouse_id, cross_border),
                "is_b2b": bool(is_b2b),
                "warehouse_id": warehouse_id,
                "cross_border_type": cross_border,
                "label": "{} · склад {}{}".format(
                    "B2B" if is_b2b else "Розница",
                    warehouse_id if warehouse_id is not None else "—",
                    " · CB {}".format(cross_border) if cross_border is not None else "",
                ),
                "order_ids": order_ids,
                "order_count": len(order_ids),
                "suggested_name": suggested,
                "compatible_supplies": [
                    {
                        "supply_id": str(s.get("supply_id") or ""),
                        "name": str(s.get("name") or s.get("supply_id") or ""),
                        "is_empty": _supply_is_empty(s),
                        "orders_count": len(s.get("order_ids") or []),
                    }
                    for s in candidates
                    if str(s.get("supply_id") or "").strip()
                ],
                "mode": "create",
                "default_supply_id": "",
            }
            if not candidates:
                group["mode"] = "create"
            elif len(candidates) == 1:
                chosen = candidates[0]
                sid = str(chosen.get("supply_id") or "")
                group["mode"] = "add_one"
                group["default_supply_id"] = sid
                if _supply_is_empty(chosen):
                    empties[:] = [
                        s for s in empties if str(s.get("supply_id") or "") != sid
                    ]
            else:
                group["mode"] = "choose"
                claimed = {
                    str(s.get("supply_id") or "")
                    for s in empties
                    if str(s.get("supply_id") or "").strip()
                }
                if claimed:
                    empties[:] = [
                        s
                        for s in empties
                        if str(s.get("supply_id") or "") not in claimed
                    ]
            groups.append(group)

        return {
            "ok": True,
            "mgt_count": len(rows),
            "groups": groups,
            "existing_names": sorted(existing_names),
        }

    def execute(
        self,
        source_id: int,
        api_key: str,
        decisions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        preview = self.preview(source_id)
        by_key = {
            str(d.get("group_key") or ""): d
            for d in decisions
            if isinstance(d, dict) and str(d.get("group_key") or "").strip()
        }
        created = 0
        added = 0
        errors = []  # type: List[str]
        for g in preview.get("groups") or []:
            key = str(g.get("group_key") or "")
            decision = by_key.get(key) or {}
            mode = str(decision.get("mode") or g.get("mode") or "create")
            order_ids = list(g.get("order_ids") or [])
            if not order_ids:
                continue
            try:
                if mode in ("create",):
                    name = str(
                        decision.get("name") or g.get("suggested_name") or ""
                    ).strip()
                    self.orders.create_supply_from_orders(
                        source_id, api_key, order_ids, name
                    )
                    created += 1
                elif mode in ("add_one", "choose", "add"):
                    sid = str(
                        decision.get("supply_id")
                        or g.get("default_supply_id")
                        or ""
                    ).strip()
                    if not sid:
                        # fallback create
                        name = str(g.get("suggested_name") or "").strip()
                        self.orders.create_supply_from_orders(
                            source_id, api_key, order_ids, name
                        )
                        created += 1
                    else:
                        self.orders.add_orders_to_existing_supply(
                            source_id, api_key, sid, order_ids
                        )
                        added += 1
                else:
                    name = str(g.get("suggested_name") or "").strip()
                    self.orders.create_supply_from_orders(
                        source_id, api_key, order_ids, name
                    )
                    created += 1
            except Exception as exc:
                errors.append("{}: {}".format(g.get("label") or key, exc))
        return {
            "created": created,
            "added": added,
            "errors": errors,
        }
