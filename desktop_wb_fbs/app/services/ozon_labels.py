# -*- coding: utf-8 -*-
"""Ozon FBS package labels via async v3 create + v2 get (web archive parity).

Never calls obsolete sync POST /v2/posting/fbs/package-label.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from app.db import Database
from app.ozon import utc_now
from app.ozon.client import OzonFbsClient

# Archive new-label raster uses 200; Ozon create max is 1000.
_LABEL_BATCH = 200
_PACKAGE_LABEL_TASK_FAIL = frozenset(
    {"error", "failed", "fail", "cancelled", "canceled"}
)

ProgressCb = Optional[Callable[..., None]]


def emit_label_progress(
    progress: ProgressCb,
    message: str,
    *,
    done: int = 0,
    total: int = 0,
) -> None:
    """Notify UI: prefer ``(done, total, message)``, fall back to ``(message,)``."""
    if not progress:
        return
    text = str(message or "")
    try:
        progress(int(done), int(total), text)
        return
    except TypeError:
        pass
    try:
        progress(text)
    except Exception:
        pass


def format_label_error(exc: BaseException) -> str:
    """Operator-facing Russian text for package-label failures."""
    text = str(exc).strip() or "ошибка Ozon"
    low = text.casefold()
    if "obsolete" in low:
        return (
            "Метод печати этикеток устарел на стороне Ozon. "
            "Нужны /v3/package-label/create и /v2/package-label/get."
        )
    if "invalid_argument" in low or "invalid argument" in low:
        return (
            "Ozon отклонил часть отправлений (неверный статус). "
            "Этикетки доступны после сборки («Ожидает отгрузки»). "
            "Обновите поставку и повторите."
        )
    if "timeout" in low or "таймаут" in low:
        return (
            "Таймаут ожидания этикеток Ozon. "
            "Повторите печать через минуту (задание ещё формируется)."
        )
    if "не больше 1000" in low or "1000" in low and "отправлен" in low:
        return text
    return text


def parse_package_label_create_tasks(
    data: Optional[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Normalize v3 create response (tolerate wrapped/old shapes)."""
    if not isinstance(data, Mapping):
        return []
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list):
        nested = data.get("result")
        if isinstance(nested, Mapping):
            raw_tasks = nested.get("tasks")
            if not isinstance(raw_tasks, list):
                tid = nested.get("task_id") or nested.get("taskId")
                if tid is not None:
                    raw_tasks = [nested]
        elif isinstance(nested, list):
            raw_tasks = nested
    if not isinstance(raw_tasks, list):
        tid = data.get("task_id") or data.get("taskId")
        if tid is not None:
            raw_tasks = [data]
        else:
            return []
    out = []  # type: List[Dict[str, Any]]
    for item in raw_tasks:
        if not isinstance(item, Mapping):
            continue
        tid = item.get("task_id")
        if tid is None:
            tid = item.get("taskId")
        if tid is None:
            continue
        try:
            task_id = int(str(tid).strip())
        except (TypeError, ValueError):
            continue
        task_type = str(
            item.get("task_type") or item.get("taskType") or ""
        ).strip().lower()
        out.append({"task_id": task_id, "task_type": task_type})
    return out


def select_small_label_task(tasks: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Prefer 58×40 ``small_label`` when Ozon returns both sizes."""
    clean = [
        dict(t)
        for t in tasks
        if isinstance(t, Mapping) and t.get("task_id") is not None
    ]
    if not clean:
        raise RuntimeError("Ozon не вернул задание на формирование этикеток")
    for t in clean:
        tt = str(t.get("task_type") or "").strip().lower()
        if tt in ("small_label", "small"):
            return {"task_id": int(t["task_id"]), "task_type": tt}
    first = clean[0]
    return {
        "task_id": int(first["task_id"]),
        "task_type": str(first.get("task_type") or "").strip().lower(),
    }


def parse_package_label_get_result(
    data: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Normalize v2 get response; ``status`` may be a nested object with ``code``."""
    if not isinstance(data, Mapping):
        return {}
    src = data  # type: Mapping[str, Any]
    nested = data.get("result")
    if isinstance(nested, Mapping) and not (
        data.get("file_url") or data.get("fileUrl") or data.get("status")
    ):
        src = nested
    file_url = str(
        src.get("file_url") or src.get("fileUrl") or src.get("file") or ""
    ).strip()
    status_obj = src.get("status") if isinstance(src.get("status"), Mapping) else {}
    error_obj = src.get("error") if isinstance(src.get("error"), Mapping) else {}
    status_code = str(
        (status_obj.get("code") if isinstance(status_obj, Mapping) else "")
        or src.get("status_code")
        or (src.get("status") if isinstance(src.get("status"), str) else "")
        or ""
    ).strip().lower()
    return {
        "file_url": file_url,
        "status_code": status_code,
        "error_code": str(
            (error_obj.get("code") if isinstance(error_obj, Mapping) else "")
            or ""
        ).strip(),
        "error_message": str(
            (error_obj.get("message") if isinstance(error_obj, Mapping) else "")
            or src.get("error_message")
            or ""
        ).strip(),
        "raw": dict(data),
    }


def package_label_task_is_failed(parsed: Mapping[str, Any]) -> bool:
    if str(parsed.get("file_url") or "").strip():
        return False
    err = str(
        parsed.get("error_message") or parsed.get("error_code") or ""
    ).strip()
    if err:
        return True
    return str(parsed.get("status_code") or "").strip().lower() in _PACKAGE_LABEL_TASK_FAIL


def package_label_task_is_ready(parsed: Mapping[str, Any]) -> bool:
    return bool(str(parsed.get("file_url") or "").strip()) and not package_label_task_is_failed(
        parsed
    )


class OzonLabelService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _cache_key(self, posting_numbers: List[str]) -> str:
        nums = sorted(str(p).strip() for p in posting_numbers if str(p).strip())
        raw = ",".join(nums)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _cache_get(self, source_id: int, key: str) -> Optional[str]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT file_path FROM ozon_fbs_label_cache
                WHERE source_id = ? AND cache_key = ?
                """,
                (source_id, key),
            ).fetchone()
        if not row:
            return None
        path = str(row["file_path"] or "")
        if path and os.path.isfile(path):
            return path
        return None

    def _cache_get_parts(self, source_id: int, key: str) -> List[str]:
        """Return all cached PDF parts for a label job (key, key#2, …)."""
        paths = []  # type: List[str]
        first = self._cache_get(source_id, key)
        if not first:
            return []
        paths.append(first)
        idx = 2
        while True:
            part = self._cache_get(source_id, "{}#{}".format(key, idx))
            if not part:
                break
            paths.append(part)
            idx += 1
        return paths

    def _cache_put(self, source_id: int, key: str, path: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO ozon_fbs_label_cache(source_id, cache_key, file_path, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id, cache_key) DO UPDATE SET
                    file_path = excluded.file_path,
                    updated_at = excluded.updated_at
                """,
                (source_id, key, path, utc_now()),
            )
            conn.commit()

    def fetch_labels(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
        *,
        use_cache: bool = True,
        timeout_s: float = 180.0,
        progress: ProgressCb = None,
    ) -> str:
        """Download label PDF(s); returns path to the first (or only) file."""
        paths = self.fetch_label_paths(
            client,
            source_id,
            posting_numbers,
            use_cache=use_cache,
            timeout_s=timeout_s,
            progress=progress,
        )
        return paths[0]

    def fetch_label_paths(
        self,
        client: OzonFbsClient,
        source_id: int,
        posting_numbers: List[str],
        *,
        use_cache: bool = True,
        timeout_s: float = 180.0,
        progress: ProgressCb = None,
        stop_requested: Optional[Callable[[], bool]] = None,
    ) -> List[str]:
        nums = [str(p).strip() for p in posting_numbers if str(p).strip()]
        if not nums:
            raise ValueError("Нет отправлений для этикеток")
        key = self._cache_key(nums)
        expected_parts = (len(nums) + _LABEL_BATCH - 1) // _LABEL_BATCH
        if use_cache:
            cached = self._cache_get_parts(source_id, key)
            if cached and len(cached) >= expected_parts:
                emit_label_progress(
                    progress,
                    "Этикетки из кэша · {} файл(ов)".format(len(cached)),
                    done=expected_parts,
                    total=expected_parts,
                )
                return cached[:expected_parts]
        paths = []  # type: List[str]
        batches = [
            nums[i : i + _LABEL_BATCH]
            for i in range(0, len(nums), _LABEL_BATCH)
        ]
        batch_total = max(1, len(batches))
        for bi, batch in enumerate(batches, start=1):
            if stop_requested and stop_requested():
                raise RuntimeError("Формирование стикеров отменено")
            emit_label_progress(
                progress,
                "Стикеры v3 · партия {}/{} ({} отпр.)…".format(
                    bi, batch_total, len(batch)
                ),
                done=bi - 1,
                total=batch_total,
            )
            try:
                pdf = self._fetch_async_pdf(
                    client,
                    batch,
                    timeout_s=timeout_s,
                    progress=progress,
                    batch_label="{}/{}".format(bi, batch_total),
                    batch_done=bi - 1,
                    batch_total=batch_total,
                    stop_requested=stop_requested,
                )
            except Exception as exc:
                raise RuntimeError(format_label_error(exc)) from exc
            paths.append(self._write_bytes(pdf))
            emit_label_progress(
                progress,
                "Стикеры v3 · партия {}/{} готова".format(bi, batch_total),
                done=bi,
                total=batch_total,
            )
        if use_cache and paths:
            self._cache_put(source_id, key, paths[0])
            for idx, part in enumerate(paths[1:], start=2):
                self._cache_put(source_id, "{}#{}".format(key, idx), part)
        emit_label_progress(
            progress,
            "Стикеры готовы · {} файл(ов)".format(len(paths)),
            done=batch_total,
            total=batch_total,
        )
        return paths

    def _fetch_async_pdf(
        self,
        client: OzonFbsClient,
        posting_numbers: List[str],
        *,
        timeout_s: float,
        progress: ProgressCb = None,
        batch_label: str = "",
        batch_done: int = 0,
        batch_total: int = 0,
        stop_requested: Optional[Callable[[], bool]] = None,
    ) -> bytes:
        """v3 create → prefer small_label → poll v2 get → download PDF."""
        emit_label_progress(
            progress,
            "Создание задания этикеток{}…".format(
                " · {}".format(batch_label) if batch_label else ""
            ),
            done=batch_done,
            total=batch_total,
        )
        tasks = client.package_label_create_v3(posting_numbers)
        normalized = parse_package_label_create_tasks({"tasks": tasks})
        if not normalized and tasks:
            for item in tasks:
                if isinstance(item, dict) and item.get("task_id") is not None:
                    try:
                        normalized.append(
                            {
                                "task_id": int(item["task_id"]),
                                "task_type": str(
                                    item.get("task_type") or ""
                                )
                                .strip()
                                .lower(),
                            }
                        )
                    except (TypeError, ValueError):
                        continue
        task = select_small_label_task(normalized)
        return self._poll_task_pdf(
            client,
            int(task["task_id"]),
            timeout_s=timeout_s,
            progress=progress,
            batch_label=batch_label,
            batch_done=batch_done,
            batch_total=batch_total,
            stop_requested=stop_requested,
        )

    def _poll_task_pdf(
        self,
        client: OzonFbsClient,
        task_id: int,
        *,
        timeout_s: float,
        progress: ProgressCb = None,
        batch_label: str = "",
        batch_done: int = 0,
        batch_total: int = 0,
        stop_requested: Optional[Callable[[], bool]] = None,
    ) -> bytes:
        deadline = time.monotonic() + max(5.0, float(timeout_s))
        last_code = ""
        started = time.monotonic()
        while time.monotonic() < deadline:
            if stop_requested and stop_requested():
                raise RuntimeError("Формирование стикеров отменено")
            raw = client.package_label_get_v2(int(task_id))
            parsed = parse_package_label_get_result(raw)
            last_code = str(parsed.get("status_code") or "") or last_code
            if package_label_task_is_failed(parsed):
                err = str(
                    parsed.get("error_message")
                    or parsed.get("error_code")
                    or last_code
                    or "error"
                )
                raise RuntimeError(
                    "Ошибка формирования этикеток: {}".format(err)
                )
            if package_label_task_is_ready(parsed):
                url = str(parsed.get("file_url") or "").strip()
                emit_label_progress(
                    progress,
                    "Скачивание PDF{}…".format(
                        " · {}".format(batch_label) if batch_label else ""
                    ),
                    done=batch_done,
                    total=batch_total,
                )
                data = client.fetch_url_bytes(url)
                if not data:
                    raise RuntimeError("Ozon вернул пустой файл этикеток")
                if data[:4] != b"%PDF":
                    raise RuntimeError(
                        "Ozon вернул не PDF (начало ответа: {!r})".format(
                            data[:32]
                        )
                    )
                return data
            elapsed = int(time.monotonic() - started)
            emit_label_progress(
                progress,
                "Ожидание этикеток Ozon{} · {} с · статус: {}".format(
                    " · {}".format(batch_label) if batch_label else "",
                    elapsed,
                    last_code or "pending",
                ),
                done=batch_done,
                total=batch_total,
            )
            time.sleep(1.5)
        raise RuntimeError(
            "Таймаут ожидания этикеток (статус: {})".format(last_code or "—")
        )

    @staticmethod
    def _write_bytes(data: bytes) -> str:
        suffix = ".pdf" if data[:4] == b"%PDF" else ".bin"
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="ozon-label-")
        os.close(fd)
        with open(path, "wb") as fh:
            fh.write(data)
        return path
