# -*- coding: utf-8 -*-
"""Ozon FBS carriage-containers (грузоместа) — list/create/fill + archive/reconcile."""
from __future__ import annotations

import base64
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from app.db import Database
from app.ozon.client import OzonFbsClient
from app.ozon import utc_now

_log = logging.getLogger(__name__)
_RECONCILE_WORKERS = 4

SORT_TYPES = frozenset({"sort", "non-sort"})
CARGO_TYPES = frozenset({"box", "pallet"})

_STATUS_LABELS = {
    "new": "Новое",
    "approved": "Подтверждено",
    "approve_failed": "Ошибка подтверждения",
    "acceptance_in_progress": "Принято на СЦ",
    "finished": "Завершено на СЦ",
    "formed": "Сформировано",
    "ready": "Готово",
    "in_progress": "В работе",
    "shipped": "Отгружено",
    "closed": "Закрыто",
    "cancelled": "Отменено",
    "canceled": "Отменено",
    "deleted": "Удалено",
}


def status_label(status: object) -> str:
    key = str(status or "").strip().lower()
    if not key:
        return "—"
    return _STATUS_LABELS.get(key, key)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def normalize_container(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    cid = _as_int(raw.get("container_id"))
    if cid <= 0:
        return None
    status = str(raw.get("status") or "").strip().lower()
    cargo_type = str(raw.get("cargo_type") or "").strip().lower()
    sort_type = str(raw.get("sort_type") or "").strip().lower()
    return {
        "container_id": cid,
        "container_number": _as_int(raw.get("container_number")),
        "status": status,
        "status_label": status_label(status),
        "sort_type": sort_type,
        "cargo_type": cargo_type,
        "order_count": max(0, _as_int(raw.get("count_of_postings"))),
        "warehouse_id": raw.get("warehouse_id"),
        "available_actions": list(raw.get("available_actions") or [])
        if isinstance(raw.get("available_actions"), list)
        else [],
        "created_at": str(raw.get("created_at") or "").strip(),
        "locally_archived": False,
    }


def _posting_numbers_from_container_payload(raw: Dict[str, Any]) -> List[str]:
    out = []  # type: List[str]
    seen = set()  # type: Set[str]

    def add(value: object) -> None:
        pn = str(value or "").strip()
        if pn and pn not in seen:
            seen.add(pn)
            out.append(pn)

    direct = raw.get("posting_numbers")
    if isinstance(direct, list):
        for item in direct:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(item.get("posting_number"))
    postings = raw.get("postings")
    if isinstance(postings, list):
        for item in postings:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(item.get("posting_number"))
    return out


def _container_get_raw_payload(data: object) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    nested = data.get("container") or data.get("result")
    if isinstance(nested, dict):
        return nested
    return data


class OzonContainersService:
    """Carriage-scoped wrappers around Ozon container Seller API methods."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self.db = db

    def resolve_warehouse_id(
        self,
        client: OzonFbsClient,
        carriage_id: object,
        *,
        source_id: Optional[int] = None,
    ) -> int:
        cid = _as_int(carriage_id)
        if cid <= 0:
            raise ValueError("Укажите carriage_id")
        try:
            info = client.get_carriage(cid)
        except Exception as exc:
            _log.warning("ozon carriage/get for warehouse cid=%s: %s", cid, exc)
            info = {}
        if isinstance(info, dict):
            wh = _as_int(info.get("warehouse_id"))
            if wh > 0:
                return wh
            nested = info.get("warehouse") if isinstance(info.get("warehouse"), dict) else {}
            wh = _as_int(nested.get("warehouse_id") or nested.get("id"))
            if wh > 0:
                return wh
        if self.db is not None and source_id is not None:
            with self.db.connect() as conn:
                row = conn.execute(
                    """
                    SELECT warehouse_id FROM ozon_fbs_postings
                    WHERE source_id = ? AND carriage_id = ?
                      AND warehouse_id IS NOT NULL
                    ORDER BY posting_number
                    LIMIT 1
                    """,
                    (int(source_id), str(cid)),
                ).fetchone()
            if row is not None:
                wh = _as_int(row["warehouse_id"] if hasattr(row, "keys") else row[0])
                if wh > 0:
                    return wh
        raise RuntimeError(
            "Не удалось определить склад отгрузки для грузомест (warehouse_id)"
        )

    def list_containers(
        self,
        client: OzonFbsClient,
        carriage_id: object,
        *,
        source_id: Optional[int] = None,
        lookback_days: int = 30,
        warehouse_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        wh = int(warehouse_id) if warehouse_id else 0
        if wh <= 0:
            wh = self.resolve_warehouse_id(
                client, carriage_id, source_id=source_id
            )
        days = max(1, min(int(lookback_days or 30), 90))
        now = _utc_now()
        created_from = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        created_to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        items: List[Dict[str, Any]] = []
        cursor = ""
        for _ in range(20):
            body: Dict[str, Any] = {
                "filter": {
                    "created_from": created_from,
                    "created_to": created_to,
                    "warehouse_id": wh,
                },
                "sort_dir": 1,
                "limit": 100,
            }
            if cursor:
                body["cursor"] = cursor
            data = client.carriage_container_list(body)
            rows = data.get("containers") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                rows = []
            for raw in rows:
                norm = normalize_container(raw if isinstance(raw, dict) else {})
                if norm:
                    items.append(norm)
            cursor = str((data or {}).get("cursor") or "").strip()
            if not cursor:
                break
        return {
            "ok": True,
            "carriage_id": _as_int(carriage_id),
            "warehouse_id": wh,
            "items": items,
            "total": len(items),
        }

    def create(
        self,
        client: OzonFbsClient,
        carriage_id: object,
        amount: int,
        container_type: Optional[str] = None,
        *,
        source_id: Optional[int] = None,
        sort_type: str = "sort",
        warehouse_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        try:
            count = int(amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("Некорректное число грузомест") from exc
        if count < 1 or count > 100:
            raise ValueError("Можно создать от 1 до 100 грузомест за раз")
        cargo = str(container_type or "pallet").strip().lower()
        if cargo not in CARGO_TYPES:
            raise ValueError("container_type: box или pallet")
        st = str(sort_type or "sort").strip().lower()
        if st not in SORT_TYPES:
            raise ValueError("sort_type: sort или non-sort")
        wh = int(warehouse_id) if warehouse_id else 0
        if wh <= 0:
            wh = self.resolve_warehouse_id(
                client, carriage_id, source_id=source_id
            )
        data = client.carriage_container_create(
            warehouse_id=wh,
            containers_count=count,
            sort_type=st,
            cargo_type=cargo,
        )
        ids_raw = data.get("container_ids") if isinstance(data, dict) else None
        ids: List[int] = []
        if isinstance(ids_raw, list):
            for x in ids_raw:
                n = _as_int(x)
                if n > 0:
                    ids.append(n)
        return {
            "ok": True,
            "carriage_id": _as_int(carriage_id),
            "warehouse_id": wh,
            "container_ids": ids,
            "created": len(ids),
            "cargo_type": cargo,
            "sort_type": st,
            "message": "Создано грузомест: {}".format(len(ids)),
        }

    def delete(
        self, client: OzonFbsClient, container_ids: List[int]
    ) -> Dict[str, Any]:
        return self.cancel(client, container_ids)

    def cancel(
        self, client: OzonFbsClient, container_ids: List[int]
    ) -> Dict[str, Any]:
        ids = [_as_int(x) for x in (container_ids or [])]
        ids = [x for x in ids if x > 0][:100]
        if not ids:
            raise ValueError("Укажите ID грузомест")
        data = client.carriage_container_cancel(container_ids=ids)
        errors = []
        if isinstance(data, dict):
            for err in data.get("error_containers") or []:
                if not isinstance(err, dict):
                    continue
                errors.append(
                    {
                        "container_id": err.get("container_id"),
                        "error": str(
                            err.get("error_message") or err.get("message") or ""
                        ).strip(),
                    }
                )
        ok_n = max(0, len(ids) - len(errors))
        return {
            "ok": not errors,
            "deleted": ok_n,
            "errors": errors,
            "task_id": (data or {}).get("task_id") if isinstance(data, dict) else None,
            "message": "Удалено: {}".format(ok_n)
            + (", ошибок {}".format(len(errors)) if errors else ""),
        }

    def list_archived_ids(self, source_id: int) -> Set[int]:
        if self.db is None or int(source_id or 0) <= 0:
            return set()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT container_id FROM ozon_fbs_container_local_archive
                WHERE source_id = ?
                """,
                (int(source_id),),
            ).fetchall()
        out = set()  # type: Set[int]
        for row in rows:
            cid = _as_int(row["container_id"] if hasattr(row, "keys") else row[0])
            if cid > 0:
                out.add(cid)
        return out

    def archive_locally(self, source_id: int, container_id: int) -> Dict[str, Any]:
        if self.db is None:
            raise RuntimeError("Нужна БД для локального архива ГМ")
        cid = _as_int(container_id)
        sid = int(source_id or 0)
        if cid <= 0 or sid <= 0:
            raise ValueError("Укажите container_id и source_id")
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO ozon_fbs_container_local_archive(
                    source_id, container_id, archived_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(source_id, container_id) DO NOTHING
                """,
                (sid, cid, utc_now()),
            )
            conn.commit()
        return {
            "ok": True,
            "container_id": cid,
            "locally_archived": True,
            "message": "ГМ {} скрыто в локальный архив".format(cid),
        }

    def restore_locally(self, source_id: int, container_id: int) -> Dict[str, Any]:
        if self.db is None:
            raise RuntimeError("Нужна БД для локального архива ГМ")
        cid = _as_int(container_id)
        sid = int(source_id or 0)
        if cid <= 0 or sid <= 0:
            raise ValueError("Укажите container_id и source_id")
        with self.db.connect() as conn:
            conn.execute(
                """
                DELETE FROM ozon_fbs_container_local_archive
                WHERE source_id = ? AND container_id = ?
                """,
                (sid, cid),
            )
            conn.commit()
        return {
            "ok": True,
            "container_id": cid,
            "locally_archived": False,
            "message": "ГМ {} возвращено из архива".format(cid),
        }

    def apply_local_archive_filter(
        self,
        source_id: int,
        items: List[Dict[str, Any]],
        *,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        archived = self.list_archived_ids(source_id)
        out = []  # type: List[Dict[str, Any]]
        for row in items or []:
            item = dict(row)
            cid = _as_int(item.get("container_id"))
            is_local = bool(cid > 0 and cid in archived)
            item["locally_archived"] = is_local
            if is_local and not include_archived:
                continue
            if is_local:
                label = str(item.get("status_label") or item.get("status") or "")
                item["status_label"] = (
                    "{} · архив".format(label) if label else "архив"
                )
            out.append(item)
        return out

    def set_local_bind(
        self,
        source_id: int,
        posting_number: str,
        *,
        container_id: Optional[int],
        synced: bool = False,
        sync_error: str = "",
    ) -> None:
        if self.db is None or int(source_id or 0) <= 0:
            return
        pn = str(posting_number or "").strip()
        if not pn:
            return
        cid = _as_int(container_id)
        barcode = str(cid) if cid > 0 else ""
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE ozon_fbs_postings
                SET container_id = ?,
                    container_barcode = ?,
                    container_synced = ?,
                    container_sync_error = ?
                WHERE source_id = ? AND posting_number = ?
                """,
                (
                    cid if cid > 0 else None,
                    barcode,
                    1 if synced and cid > 0 else 0,
                    str(sync_error or "").strip(),
                    int(source_id),
                    pn,
                ),
            )
            conn.commit()

    def load_bind_map(
        self, source_id: int, posting_numbers: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        out = {}  # type: Dict[str, Dict[str, Any]]
        if self.db is None or int(source_id or 0) <= 0:
            return out
        nums = [str(p).strip() for p in posting_numbers if str(p).strip()]
        if not nums:
            return out
        placeholders = ",".join("?" for _ in nums)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT posting_number, container_id, container_barcode,
                       container_synced, container_sync_error
                FROM ozon_fbs_postings
                WHERE source_id = ? AND posting_number IN ({})
                """.format(
                    placeholders
                ),
                (int(source_id), *nums),
            ).fetchall()
        for row in rows:
            d = Database.row_to_dict(row) or {}
            pn = str(d.get("posting_number") or "").strip()
            if not pn:
                continue
            cid = _as_int(d.get("container_id"))
            out[pn] = {
                "container_id": cid if cid > 0 else None,
                "container_barcode": str(d.get("container_barcode") or "").strip(),
                "container_synced": bool(d.get("container_synced")) and cid > 0,
                "container_sync_error": str(
                    d.get("container_sync_error") or ""
                ).strip(),
            }
        return out

    def fetch_container_postings(
        self, client: OzonFbsClient, container_id: int
    ) -> Tuple[Optional[Dict[str, Any]], List[str], bool]:
        try:
            data = client.carriage_container_get(container_id=int(container_id))
        except Exception as exc:
            text = str(exc or "").casefold()
            if "404" in text or "not found" in text or "not_found" in text:
                return None, [], False
            _log.warning("ozon container get cid=%s: %s", container_id, exc)
            return None, [], True
        raw = _container_get_raw_payload(data)
        if not raw:
            return None, [], False
        if not raw.get("container_id"):
            raw = dict(raw)
            raw["container_id"] = int(container_id)
        meta = normalize_container(raw)
        return meta, _posting_numbers_from_container_payload(raw), True

    def reconcile_binds(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
        *,
        warehouse_id: int = 0,
        carriage_id: object = None,
    ) -> Dict[str, Any]:
        """Align local posting→container binds with Ozon (portal wins)."""
        sid = int(source_id or 0)
        nums = [str(p).strip() for p in posting_numbers if str(p).strip()]
        supply_set = set(nums)
        if not supply_set or sid <= 0:
            return {
                "ok": True,
                "changes": [],
                "message": "Нет отправлений для сверки ГМ",
            }
        local_binds = self.load_bind_map(sid, nums)
        listed = self.list_containers(
            client,
            carriage_id or 0,
            source_id=sid,
            warehouse_id=warehouse_id or None,
        )
        alive_ids = set()  # type: Set[int]
        for item in listed.get("items") or []:
            cid = _as_int(item.get("container_id"))
            if cid > 0:
                alive_ids.add(cid)
        local_cids = set()  # type: Set[int]
        for row in local_binds.values():
            cid = _as_int(row.get("container_id"))
            if cid > 0:
                local_cids.add(cid)
        fetch_ids = alive_ids | local_cids
        ozon_map = {}  # type: Dict[str, int]
        posting_lists_available = False
        missing_ids = set()  # type: Set[int]

        def _one(cid: int):
            return cid, self.fetch_container_postings(client, cid)

        if fetch_ids:
            workers = min(_RECONCILE_WORKERS, len(fetch_ids))
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = [pool.submit(_one, cid) for cid in fetch_ids]
                for fut in as_completed(futures):
                    try:
                        cid, (meta, pn_list, exists) = fut.result()
                    except Exception as exc:
                        _log.warning("reconcile container: %s", exc)
                        continue
                    if not exists:
                        missing_ids.add(int(cid))
                        continue
                    if pn_list:
                        posting_lists_available = True
                        for pn in pn_list:
                            if pn in supply_set:
                                ozon_map[pn] = int(cid)

        changes = []  # type: List[Dict[str, Any]]

        def _apply(
            pn: str,
            *,
            container_id: Optional[int],
            synced: bool,
            action: str,
            reason: str,
        ) -> None:
            self.set_local_bind(
                sid, pn, container_id=container_id, synced=synced, sync_error=""
            )
            changes.append(
                {
                    "posting_number": pn,
                    "action": action,
                    "reason": reason,
                    "container_id": container_id,
                }
            )

        for pn in nums:
            local = local_binds.get(pn) or {}
            local_cid = _as_int(local.get("container_id"))
            local_synced = bool(local.get("container_synced")) and local_cid > 0
            local_err = str(local.get("container_sync_error") or "").strip()
            ozon_cid = ozon_map.get(pn)

            if local_cid > 0 and local_cid in missing_ids:
                if not local_synced and local_err:
                    continue
                _apply(
                    pn,
                    container_id=None,
                    synced=False,
                    action="cleared",
                    reason="Грузоместо удалено на портале Ozon",
                )
                continue
            if not posting_lists_available:
                continue
            if ozon_cid == local_cid:
                if ozon_cid and ozon_cid > 0 and not local_synced and not local_err:
                    _apply(
                        pn,
                        container_id=ozon_cid,
                        synced=True,
                        action="synced",
                        reason="Привязка подтверждена на портале Ozon",
                    )
                continue
            if ozon_cid is None or ozon_cid <= 0:
                if local_cid <= 0:
                    continue
                if not local_synced and local_err:
                    continue
                _apply(
                    pn,
                    container_id=None,
                    synced=False,
                    action="cleared",
                    reason="На портале Ozon заказ не в грузоместе",
                )
                continue
            action = "adopted" if local_cid <= 0 else "updated"
            _apply(
                pn,
                container_id=ozon_cid,
                synced=True,
                action=action,
                reason="Состав грузоместа синхронизирован с порталом Ozon",
            )

        return {
            "ok": True,
            "changes": changes,
            "posting_lists_available": posting_lists_available,
            "containers_checked": len(fetch_ids),
            "message": (
                "Синхронизировано: {}".format(len(changes))
                if changes
                else (
                    "Данные совпадают с порталом"
                    if posting_lists_available
                    else "Состав ГМ недоступен из API — проверены только удалённые"
                )
            ),
        }

    def bind_posting(
        self,
        client: OzonFbsClient,
        carriage_id: object,
        container_id: int,
        posting_number: str,
        *,
        source_id: int = 0,
    ) -> Dict[str, Any]:
        _ = carriage_id  # scoped by caller UI; fill API is container-level
        pn = str(posting_number or "").strip()
        cid = _as_int(container_id)
        if not pn or cid <= 0:
            raise ValueError("Укажите отправление и грузоместо")
        data = client.carriage_container_fill(
            container_id=cid, posting_numbers=[pn]
        )
        self.set_local_bind(
            int(source_id or 0),
            pn,
            container_id=cid,
            synced=False,
            sync_error="",
        )
        return {
            "ok": True,
            "container_id": cid,
            "posting_number": pn,
            "task_id": _as_int((data or {}).get("task_id")) or None,
            "raw": data if isinstance(data, dict) else {},
        }

    def unbind(
        self,
        client: OzonFbsClient,
        container_id: int,
        posting_number: str,
        *,
        carriage_id: object = None,
        source_id: int = 0,
    ) -> Dict[str, Any]:
        _ = carriage_id
        pn = str(posting_number or "").strip()
        cid = _as_int(container_id)
        if not pn or cid <= 0:
            raise ValueError("Укажите отправление и грузоместо")
        data = client.carriage_container_remove_postings(
            container_id=cid, posting_numbers=[pn]
        )
        self.set_local_bind(
            int(source_id or 0),
            pn,
            container_id=None,
            synced=False,
            sync_error="",
        )
        return {
            "ok": True,
            "container_id": cid,
            "posting_number": pn,
            "raw": data if isinstance(data, dict) else {},
        }

    def approve(
        self, client: OzonFbsClient, container_ids: List[int]
    ) -> Dict[str, Any]:
        ids = [_as_int(x) for x in (container_ids or [])]
        ids = [x for x in ids if x > 0][:100]
        if not ids:
            raise ValueError("Укажите ID грузомест")
        data = client.carriage_container_approve(container_ids=ids)
        errors = []
        if isinstance(data, dict):
            for err in data.get("error_containers") or []:
                if not isinstance(err, dict):
                    continue
                errors.append(
                    {
                        "container_id": err.get("container_id"),
                        "error": str(
                            err.get("error_message") or err.get("message") or ""
                        ).strip(),
                    }
                )
        ok_n = max(0, len(ids) - len(errors))
        return {
            "ok": not errors,
            "approved": ok_n,
            "errors": errors,
            "task_id": _as_int((data or {}).get("task_id")) or None,
            "message": "Подтверждено грузомест: {}".format(ok_n)
            if not errors
            else "Подтверждено: {}, ошибок {}".format(ok_n, len(errors)),
        }

    def labels_pdf(
        self, client: OzonFbsClient, container_ids: List[int]
    ) -> Tuple[bytes, str]:
        ids = [_as_int(x) for x in (container_ids or [])]
        ids = [x for x in ids if x > 0][:300]
        if not ids:
            raise ValueError("Укажите ID грузомест")
        data = client.carriage_container_label_get(container_ids=ids)
        content = data.get("content") if isinstance(data, dict) else None
        file_b64 = ""
        if isinstance(content, dict):
            file_b64 = str(content.get("file_content") or "").strip()
        if not file_b64 and isinstance(data, dict):
            file_b64 = str(data.get("file_content") or "").strip()
        if not file_b64:
            raise RuntimeError("Ozon не вернул PDF этикеток грузомест")
        try:
            payload = base64.b64decode(file_b64)
        except Exception as exc:
            raise RuntimeError("Не удалось декодировать PDF этикеток") from exc
        fd, path = tempfile.mkstemp(suffix=".pdf", prefix="ozon-gm-label-")
        import os

        os.close(fd)
        with open(path, "wb") as fh:
            fh.write(payload)
        return payload, path
