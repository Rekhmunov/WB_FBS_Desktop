# -*- coding: utf-8 -*-
"""Thin Ozon FBS carriage-containers (грузоместа) service for desktop."""
from __future__ import annotations

import base64
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.db import Database
from app.ozon.client import OzonFbsClient

_log = logging.getLogger(__name__)

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
    }


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

    def bind_posting(
        self,
        client: OzonFbsClient,
        carriage_id: object,
        container_id: int,
        posting_number: str,
    ) -> Dict[str, Any]:
        _ = carriage_id  # scoped by caller UI; fill API is container-level
        pn = str(posting_number or "").strip()
        cid = _as_int(container_id)
        if not pn or cid <= 0:
            raise ValueError("Укажите отправление и грузоместо")
        data = client.carriage_container_fill(
            container_id=cid, posting_numbers=[pn]
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
    ) -> Dict[str, Any]:
        _ = carriage_id
        pn = str(posting_number or "").strip()
        cid = _as_int(container_id)
        if not pn or cid <= 0:
            raise ValueError("Укажите отправление и грузоместо")
        data = client.carriage_container_remove_postings(
            container_id=cid, posting_numbers=[pn]
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
