# -*- coding: utf-8 -*-
"""Ozon FBS package labels with task polling and disk cache."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import List, Optional, Tuple

from app.db import Database
from app.ozon import utc_now
from app.ozon.client import OzonFbsClient


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
        timeout_s: float = 90.0,
    ) -> str:
        nums = [str(p).strip() for p in posting_numbers if str(p).strip()]
        if not nums:
            raise ValueError("Нет отправлений для этикеток")
        key = self._cache_key(nums)
        if use_cache:
            cached = self._cache_get(source_id, key)
            if cached:
                return cached
        tasks = client.package_label_create(nums)
        task_ids = []
        for task in tasks:
            if isinstance(task, dict) and task.get("task_id") is not None:
                task_ids.append(int(task["task_id"]))
        file_path = ""
        if task_ids:
            file_path = self._poll_tasks(client, task_ids, timeout_s=timeout_s)
        if not file_path:
            data = client.package_label_fetch(nums)
            if not data:
                raise RuntimeError("Ozon не вернул файл этикеток")
            file_path = self._write_bytes(data)
        if use_cache and file_path:
            self._cache_put(source_id, key, file_path)
        return file_path

    def _poll_tasks(
        self,
        client: OzonFbsClient,
        task_ids: List[int],
        *,
        timeout_s: float,
    ) -> str:
        deadline = time.monotonic() + max(5.0, float(timeout_s))
        last_status = ""
        while time.monotonic() < deadline:
            for tid in task_ids:
                status = client.package_label_task_status(int(tid))
                st = str(status.get("status") or "").strip().lower()
                last_status = st or last_status
                if st == "completed":
                    url = str(status.get("file_url") or "").strip()
                    if url:
                        data = client.fetch_url_bytes(url)
                        if data:
                            return self._write_bytes(data)
                if st == "error":
                    err = str(status.get("error") or "error")
                    raise RuntimeError("Ошибка формирования этикеток: {}".format(err))
            time.sleep(2.0)
        raise RuntimeError(
            "Таймаут ожидания этикеток (статус: {})".format(last_status or "—")
        )

    @staticmethod
    def _write_bytes(data: bytes) -> str:
        suffix = ".pdf" if data[:4] == b"%PDF" else ".bin"
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="ozon-label-")
        os.close(fd)
        with open(path, "wb") as fh:
            fh.write(data)
        return path
