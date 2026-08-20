# -*- coding: utf-8 -*-
"""Collect all MGT — preview/execute parity with web WB FBS."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Set, Tuple

from app.db import Database
from app.services.orders import OrdersService
from app.wb import (
    coalesce_b2b_flag,
    default_mgt_supply_name,
    is_cancelled_status,
    order_b2b_flag,
    parse_json_list,
    parse_json_obj,
    utc_now,
)
from app.wb.client import WbFbsClient
from app.wb.sync import upsert_supply


def unique_supply_name(base: str, existing_names: Set[str]) -> str:
    """Return free title; does NOT mutate ``existing_names`` (web parity)."""
    name = str(base or "").strip() or "Поставка"
    if name not in existing_names:
        return name
    for i in range(2, 100):
        candidate = "{} ({})".format(name, i)
        if candidate not in existing_names:
            return candidate
    return "{} · {}".format(name, int(time.time()))


def cross_border(row: Dict[str, Any]) -> Optional[int]:
    """Order/supply ``crossBorderType``: 0 / 1 / None if unset."""
    raw = parse_json_obj(row.get("raw_json"))
    if "crossBorderType" in raw and raw.get("crossBorderType") is not None:
        try:
            return int(raw.get("crossBorderType"))
        except (TypeError, ValueError):
            return None
    if "cross_border_type" in row and row.get("cross_border_type") is not None:
        try:
            return int(row.get("cross_border_type"))
        except (TypeError, ValueError):
            return None
    for key in ("crossBorderType", "cross_border_type"):
        if key in raw and raw.get(key) is not None:
            try:
                return int(raw.get(key))
            except (TypeError, ValueError):
                return None
    return None


def row_order_is_b2b(row: Dict[str, Any]) -> bool:
    if row.get("is_b2b"):
        return True
    raw = parse_json_obj(row.get("raw_json"))
    if raw:
        flag = order_b2b_flag(raw)
        if flag is not None:
            return bool(flag)
    return bool(int(row.get("is_b2b") or 0))


def supply_is_empty(supply: Dict[str, Any]) -> bool:
    """True only for unset cargo (no goods yet). Never treat SGT/KGT as empty."""
    cargo = int(supply.get("cargo_type") or 0)
    if cargo != 0:
        return False
    order_ids = supply.get("order_ids")
    if not isinstance(order_ids, list):
        order_ids = parse_json_list(supply.get("order_ids_json"))
    return not order_ids


def mgt_group_key(
    *, is_b2b: bool, warehouse_id: object, cross_border_type: object
) -> str:
    wh = "na" if warehouse_id is None else str(int(warehouse_id))
    cb = "na" if cross_border_type is None else str(int(cross_border_type))
    return "{}_wh{}_cb{}".format("b2b" if is_b2b else "non", wh, cb)


def mgt_group_label(
    *, is_b2b: bool, warehouse_id: object, cross_border_type: object
) -> str:
    parts = ["B2B" if is_b2b else "не B2B"]
    if warehouse_id is not None:
        parts.append("склад {}".format(warehouse_id))
    if cross_border_type == 1:
        parts.append("кроссбордер")
    elif cross_border_type == 0:
        parts.append("не кроссбордер")
    return " · ".join(parts)


def plan_mgt_group(
    *,
    is_b2b: bool,
    order_ids: List[int],
    mgt_matching: List[Dict[str, Any]],
    empties: List[Dict[str, Any]],
    existing_names: Set[str],
    warehouse_id: object = None,
    cross_border_type: object = None,
) -> Dict[str, Any]:
    """Plan one MGT bucket. Mutates ``empties`` / ``existing_names`` like web."""
    group_key = mgt_group_key(
        is_b2b=is_b2b,
        warehouse_id=warehouse_id,
        cross_border_type=cross_border_type,
    )
    label = mgt_group_label(
        is_b2b=is_b2b,
        warehouse_id=warehouse_id,
        cross_border_type=cross_border_type,
    )
    base_name = default_mgt_supply_name(is_b2b=is_b2b)
    suggested = unique_supply_name(base_name, existing_names)
    candidates = list(mgt_matching) + list(empties)
    group = {
        "group_key": group_key,
        "is_b2b": bool(is_b2b),
        "warehouse_id": warehouse_id,
        "cross_border_type": cross_border_type,
        "label": label,
        "order_ids": order_ids,
        "order_count": len(order_ids),
        "suggested_name": suggested,
        "name_conflict": False,
        "compatible_supplies": [
            {
                "supply_id": str(s.get("supply_id") or ""),
                "name": str(s.get("name") or s.get("supply_id") or ""),
                "cargo_type": int(s.get("cargo_type") or 0),
                "is_b2b": bool(s.get("is_b2b")),
                "is_empty": supply_is_empty(s),
                "orders_count": len(
                    s.get("order_ids") or parse_json_list(s.get("order_ids_json"))
                ),
            }
            for s in candidates
            if str(s.get("supply_id") or "").strip()
        ],
        "mode": "create",
        "default_supply_id": "",
    }  # type: Dict[str, Any]
    if not order_ids:
        group["mode"] = "skip"
        return group
    if not candidates:
        group["mode"] = "create"
        existing_names.add(suggested)
        return group
    if len(candidates) == 1:
        chosen = candidates[0]
        sid = str(chosen.get("supply_id") or "")
        group["mode"] = "add_one"
        group["default_supply_id"] = sid
        if supply_is_empty(chosen):
            empties[:] = [
                s for s in empties if str(s.get("supply_id") or "") != sid
            ]
        return group
    group["mode"] = "choose"
    claimed_empty_ids = {
        str(s.get("supply_id") or "")
        for s in empties
        if str(s.get("supply_id") or "").strip()
    }
    if claimed_empty_ids:
        empties[:] = [
            s
            for s in empties
            if str(s.get("supply_id") or "") not in claimed_empty_ids
        ]
    return group


class CollectMgtService:
    def __init__(self, db: Database, orders: OrdersService) -> None:
        self.db = db
        self.orders = orders

    def _load_new_mgt_orders(self, source_id: int) -> List[Dict[str, Any]]:
        rows = self.orders.new_mgt_orders(source_id)
        out = []  # type: List[Dict[str, Any]]
        for row in rows:
            if is_cancelled_status(
                supplier_status=row.get("supplier_status"),
                wb_status=row.get("wb_status"),
            ):
                continue
            d = dict(row)
            d["is_b2b"] = row_order_is_b2b(d)
            d["cross_border_type"] = cross_border(d)
            try:
                d["warehouse_id"] = (
                    int(d["warehouse_id"]) if d.get("warehouse_id") is not None else None
                )
            except (TypeError, ValueError):
                d["warehouse_id"] = None
            out.append(d)
        return out

    def _supply_warehouse_id(
        self, source_id: int, supply: Dict[str, Any]
    ) -> Optional[int]:
        order_ids = supply.get("order_ids")
        if not isinstance(order_ids, list):
            order_ids = parse_json_list(supply.get("order_ids_json"))
        ids = [int(x) for x in order_ids if x is not None]
        if not ids:
            return None
        whs = set()  # type: Set[int]
        with self.db.connect() as conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                placeholders = ", ".join("?" for _ in chunk)
                rows = conn.execute(
                    """
                    SELECT DISTINCT warehouse_id FROM wb_fbs_orders
                    WHERE source_id = ?
                      AND order_id IN ({})
                      AND warehouse_id IS NOT NULL
                    """.format(
                        placeholders
                    ),
                    tuple([source_id] + chunk),
                ).fetchall()
                for row in rows:
                    try:
                        whs.add(int(row["warehouse_id"]))
                    except (TypeError, ValueError, KeyError):
                        continue
        if len(whs) == 1:
            return next(iter(whs))
        return None

    def _supply_matches_mgt_traits(
        self,
        source_id: int,
        supply: Dict[str, Any],
        *,
        is_b2b: bool,
        warehouse_id: object,
        cross_border_type: object,
    ) -> bool:
        if supply_is_empty(supply):
            return True
        cargo = int(supply.get("cargo_type") or 0)
        if cargo != 1:
            return False
        raw = parse_json_obj(supply.get("raw_json"))
        sb = coalesce_b2b_flag(raw)
        if sb is None and supply.get("is_b2b") is not None:
            sb = bool(supply.get("is_b2b"))
        if sb is not None and bool(sb) != bool(is_b2b):
            return False
        supply_wh = self._supply_warehouse_id(source_id, supply)
        if (
            warehouse_id is not None
            and supply_wh is not None
            and int(warehouse_id) != int(supply_wh)
        ):
            return False
        supply_cb = cross_border(supply)
        if (
            cross_border_type is not None
            and supply_cb is not None
            and int(cross_border_type) != int(supply_cb)
        ):
            return False
        return True

    def preview(self, source_id: int) -> Dict[str, Any]:
        orders = self._load_new_mgt_orders(source_id)
        buckets = {}  # type: Dict[Tuple[Any, ...], List[Dict[str, Any]]]
        for o in orders:
            key = (
                bool(o.get("is_b2b")),
                o.get("warehouse_id"),
                o.get("cross_border_type"),
            )
            buckets.setdefault(key, []).append(o)

        open_supplies, _total = self.orders.list_supplies(
            source_id, done=False, search="", limit=500, offset=0
        )
        for s in open_supplies:
            s["order_ids"] = s.get("order_ids") or parse_json_list(
                s.get("order_ids_json")
            )
            raw = parse_json_obj(s.get("raw_json"))
            inferred = coalesce_b2b_flag(raw)
            if inferred is not None:
                s["is_b2b"] = inferred

        empties = []  # type: List[Dict[str, Any]]
        mgt_supplies = []  # type: List[Dict[str, Any]]
        for s in open_supplies:
            if supply_is_empty(s):
                empties.append(s)
                continue
            if int(s.get("cargo_type") or 0) == 1:
                mgt_supplies.append(s)

        existing_names = {
            str(s.get("name") or "").strip()
            for s in open_supplies
            if str(s.get("name") or "").strip()
        }
        reserved_names = set(existing_names)

        ordered_keys = sorted(
            buckets.keys(), key=lambda k: (bool(k[0]), str(k[1]), str(k[2]))
        )
        groups = []  # type: List[Dict[str, Any]]
        for is_b2b, warehouse_id, cross_border_type in ordered_keys:
            bucket_orders = buckets[(is_b2b, warehouse_id, cross_border_type)]
            order_ids = [int(o["order_id"]) for o in bucket_orders]
            matching = [
                s
                for s in mgt_supplies
                if self._supply_matches_mgt_traits(
                    source_id,
                    s,
                    is_b2b=bool(is_b2b),
                    warehouse_id=warehouse_id,
                    cross_border_type=cross_border_type,
                )
            ]
            group = plan_mgt_group(
                is_b2b=bool(is_b2b),
                order_ids=order_ids,
                mgt_matching=matching,
                empties=empties,
                existing_names=reserved_names,
                warehouse_id=warehouse_id,
                cross_border_type=cross_border_type,
            )
            if group.get("mode") != "skip":
                groups.append(group)

        needs_modal = any(g.get("mode") in ("create", "choose") for g in groups)
        return {
            "ok": True,
            "mgt_count": len(orders),
            "groups": groups,
            "needs_modal": needs_modal,
            "existing_names": sorted(existing_names),
        }

    def execute(
        self,
        source_id: int,
        api_key: str,
        decisions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Execute collect plan — web ``execute_collect_mgt`` parity."""
        client = WbFbsClient(api_key)
        preview = self.preview(source_id)
        planned_groups = list(preview.get("groups") or [])
        existing_names = {
            str(x or "").strip()
            for x in (preview.get("existing_names") or [])
            if str(x or "").strip()
        }
        decisions_by_key = {
            str(d.get("group_key") or ""): d
            for d in decisions
            if isinstance(d, dict) and str(d.get("group_key") or "").strip()
        }

        all_ids = [
            int(x) for g in planned_groups for x in (g.get("order_ids") or [])
        ]
        status_map = {}  # type: Dict[int, Dict[str, Any]]
        for i in range(0, len(all_ids), 1000):
            chunk = all_ids[i : i + 1000]
            try:
                for st in client.get_statuses(chunk):
                    if isinstance(st, dict) and st.get("id") is not None:
                        status_map[int(st["id"])] = st
            except Exception as exc:
                return {
                    "ok": False,
                    "message": "Не удалось проверить статусы заказов: {}".format(exc),
                    "errors": [str(exc)],
                    "warnings": [],
                    "added": 0,
                    "created_supplies": [],
                    "skipped_cancelled": [],
                    "not_added": all_ids,
                    "remaining_in_new": all_ids,
                    "groups": [],
                    "goto_assembly": False,
                }
            if i + 1000 < len(all_ids):
                time.sleep(0.21)

        skipped_cancelled = []  # type: List[int]
        not_added = []  # type: List[int]
        errors = []  # type: List[str]
        warnings = []  # type: List[str]
        created_supplies = []  # type: List[Dict[str, Any]]
        added_total = 0
        added_ids = []  # type: List[int]
        group_results = []  # type: List[Dict[str, Any]]
        planned_live_total = 0

        for planned in planned_groups:
            group_key = str(planned.get("group_key") or "")
            decision = decisions_by_key.get(group_key) or {}
            is_b2b = bool(planned.get("is_b2b"))
            label = str(
                planned.get("label") or ("B2B" if is_b2b else "не B2B")
            )
            raw_ids = [int(x) for x in (planned.get("order_ids") or [])]
            live_ids = []  # type: List[int]
            for oid in raw_ids:
                st = status_map.get(oid) or {}
                ss = str(st.get("supplierStatus") or "").strip().lower()
                ws = str(st.get("wbStatus") or "").strip().lower()
                if is_cancelled_status(supplier_status=ss, wb_status=ws):
                    skipped_cancelled.append(oid)
                    continue
                if ss and ss != "new":
                    skipped_cancelled.append(oid)
                    continue
                live_ids.append(oid)
            planned_live_total += len(live_ids)
            if not live_ids:
                group_results.append(
                    {
                        "group_key": group_key,
                        "is_b2b": is_b2b,
                        "added": 0,
                        "supply_id": "",
                        "message": "{}: нет актуальных МГТ-заказов".format(label),
                        "not_added": [],
                    }
                )
                continue

            mode = str(planned.get("mode") or "create")
            explicit = str(
                decision.get("action") or decision.get("mode") or ""
            ).strip().lower()
            supply_id = str(
                decision.get("supply_id")
                or planned.get("default_supply_id")
                or ""
            ).strip()
            name = str(
                decision.get("name") or planned.get("suggested_name") or ""
            ).strip()

            if explicit == "create" or (not explicit and mode == "create"):
                action = "create"
            elif explicit in ("choose", "add", "add_one") or mode in (
                "choose",
                "add_one",
            ):
                action = "add"
                if mode == "choose" or explicit == "choose":
                    if not supply_id:
                        errors.append("{}: не выбрана поставка".format(label))
                        not_added.extend(live_ids)
                        group_results.append(
                            {
                                "group_key": group_key,
                                "is_b2b": is_b2b,
                                "added": 0,
                                "supply_id": "",
                                "message": "{}: не выбрана поставка".format(label),
                                "not_added": list(live_ids),
                            }
                        )
                        continue
                if mode == "add_one":
                    supply_id = supply_id or str(
                        planned.get("default_supply_id") or ""
                    )
            else:
                action = "create"

            if action == "create":
                if not name:
                    errors.append("{}: пустое название поставки".format(label))
                    not_added.extend(live_ids)
                    continue
                if name in existing_names:
                    errors.append(
                        "{}: поставка «{}» уже есть — измените название".format(
                            label, name
                        )
                    )
                    not_added.extend(live_ids)
                    continue
                try:
                    created = client.create_supply(name=name)
                    supply_id = str(created.get("id") or "").strip()
                    if not supply_id:
                        raise RuntimeError("WB не вернул id поставки")
                    upsert_supply(
                        self.db,
                        source_id,
                        {
                            "id": supply_id,
                            "name": name,
                            "done": False,
                            "cargoType": 0,
                            "isB2b": is_b2b,
                        },
                        order_ids=[],
                    )
                    existing_names.add(name)
                    created_supplies.append(
                        {
                            "supply_id": supply_id,
                            "name": name,
                            "is_b2b": is_b2b,
                            "group_key": group_key,
                        }
                    )
                except Exception as exc:
                    errors.append(
                        "{}: не удалось создать поставку — {}".format(label, exc)
                    )
                    not_added.extend(live_ids)
                    continue

            if not supply_id:
                errors.append("{}: не указана поставка".format(label))
                not_added.extend(live_ids)
                continue

            try:
                live_supply = client.get_supply(supply_id)
                if bool(live_supply.get("done")):
                    errors.append(
                        "{}: поставка {} уже закрыта на WB".format(label, supply_id)
                    )
                    not_added.extend(live_ids)
                    continue
                live_cargo = int(live_supply.get("cargoType") or 0)
                if live_cargo not in (0, 1):
                    errors.append(
                        "{}: поставка {} не МГТ (cargoType={})".format(
                            label, supply_id, live_cargo
                        )
                    )
                    not_added.extend(live_ids)
                    continue
                live_b2b = coalesce_b2b_flag(live_supply)
                if live_cargo == 1 and live_b2b is not None and live_b2b != is_b2b:
                    errors.append(
                        "{}: поставка {} другого типа B2B".format(label, supply_id)
                    )
                    not_added.extend(live_ids)
                    continue
                sel_wh = planned.get("warehouse_id")
                sel_cb = planned.get("cross_border_type")
                if live_cargo == 1 and sel_wh is not None:
                    try:
                        live_oids = client.get_supply_order_ids(supply_id)
                    except Exception:
                        live_oids = []
                    if not live_oids:
                        local = self.orders.get_supply(source_id, supply_id) or {}
                        live_oids = parse_json_list(local.get("order_ids_json"))
                    supply_wh = self._supply_warehouse_id(
                        source_id, {"order_ids": live_oids}
                    )
                    if supply_wh is not None and int(supply_wh) != int(sel_wh):
                        errors.append(
                            "{}: склад поставки {} ≠ склад заказов {}".format(
                                label, supply_wh, sel_wh
                            )
                        )
                        not_added.extend(live_ids)
                        continue
                live_cb = live_supply.get("crossBorderType")
                if (
                    live_cargo == 1
                    and live_cb is not None
                    and sel_cb is not None
                    and int(live_cb) != int(sel_cb)
                ):
                    errors.append(
                        "{}: поставка {} другого crossBorderType".format(
                            label, supply_id
                        )
                    )
                    not_added.extend(live_ids)
                    continue
            except Exception as exc:
                errors.append(
                    "{}: проверка поставки {} — {}".format(label, supply_id, exc)
                )
                not_added.extend(live_ids)
                continue

            group_added_ids = []  # type: List[int]
            now = utc_now()
            for i in range(0, len(live_ids), 100):
                chunk = live_ids[i : i + 100]
                try:
                    client.add_orders_to_supply(supply_id, chunk)
                    group_added_ids.extend(chunk)
                    with self.db.connect() as conn:
                        for oid in chunk:
                            conn.execute(
                                """
                                UPDATE wb_fbs_orders
                                SET supply_id = ?, tab = 'assembly',
                                    supplier_status = 'confirm',
                                    is_b2b = ?, synced_at = ?
                                WHERE source_id = ? AND order_id = ?
                                """,
                                (
                                    supply_id,
                                    1 if is_b2b else 0,
                                    now,
                                    source_id,
                                    oid,
                                ),
                            )
                        conn.commit()
                except Exception as exc:
                    errors.append(
                        "{}: не удалось добавить заказы {}… ({} шт.) в {} — {}".format(
                            label, chunk[0], len(chunk), supply_id, exc
                        )
                    )
                    not_added.extend(chunk)
                    rest = live_ids[i + 100 :]
                    if rest:
                        not_added.extend(rest)
                        errors.append(
                            "{}: оставшиеся {} заказ(ов) не отправлены "
                            "после ошибки чанка — они остались в «Новых»".format(
                                label, len(rest)
                            )
                        )
                    break
                if i + 100 < len(live_ids):
                    time.sleep(0.25)

            added = len(group_added_ids)
            if added:
                try:
                    oids = client.get_supply_order_ids(supply_id)
                    live_supply = client.get_supply(supply_id)
                    if oids:
                        refresh_ids = oids
                    else:
                        local = self.orders.get_supply(source_id, supply_id) or {}
                        prev = parse_json_list(local.get("order_ids_json"))
                        refresh_ids = sorted(set(prev) | set(group_added_ids))
                    upsert_supply(
                        self.db,
                        source_id,
                        live_supply
                        or {"id": supply_id, "name": name, "isB2b": is_b2b},
                        order_ids=refresh_ids,
                    )
                except Exception as exc:
                    warnings.append(
                        "{}: заказы добавлены на WB, локальный кэш поставки — {}".format(
                            label, exc
                        )
                    )

            added_total += added
            added_ids.extend(group_added_ids)
            group_not_added = [
                oid for oid in live_ids if oid not in set(group_added_ids)
            ]
            group_results.append(
                {
                    "group_key": group_key,
                    "is_b2b": is_b2b,
                    "added": added,
                    "supply_id": supply_id,
                    "message": "{}: добавлено {} из {}".format(
                        label, added, len(live_ids)
                    ),
                    "not_added": group_not_added,
                }
            )

        seen_na = set()  # type: Set[int]
        not_added_uniq = []  # type: List[int]
        for oid in not_added:
            if oid in seen_na:
                continue
            seen_na.add(oid)
            not_added_uniq.append(oid)
        not_added = not_added_uniq
        remaining_in_new = list(not_added)
        ok = (
            planned_live_total > 0
            and added_total == planned_live_total
            and not errors
            and not not_added
        )
        if ok:
            message = "Готово: добавлено все {} актуальных МГТ-заказов.".format(
                added_total
            )
        elif added_total > 0 and (errors or not_added):
            message = (
                "Частично: добавлено {} из {}. "
                "В «Новых» осталось {}.".format(
                    added_total, planned_live_total, len(remaining_in_new)
                )
            )
        elif errors:
            message = "Не удалось собрать МГТ-заказы."
        else:
            message = "Нечего добавлять."
        if skipped_cancelled:
            message += " Пропущено (отмена/не new): {}.".format(
                len(skipped_cancelled)
            )
        if warnings:
            message += " Предупреждений: {}.".format(len(warnings))
        return {
            "ok": ok,
            "message": message,
            "errors": errors,
            "warnings": warnings,
            "added": added_total,
            "added_ids": added_ids,
            "planned_live": planned_live_total,
            "created_supplies": created_supplies,
            "skipped_cancelled": skipped_cancelled,
            "not_added": not_added,
            "remaining_in_new": remaining_in_new,
            "groups": group_results,
            "goto_assembly": bool(ok),
        }
