"""Stock module — fetches inventory reports from WB and Ozon on a schedule."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .repository import ReviewRepository

_log = logging.getLogger(__name__)

# Directory where downloaded report files are stored.
# Defaults to a subdirectory inside the app working directory.
# Can be overridden via STOCK_FILES_DIR env variable.
_default_files_dir = Path(__file__).resolve().parent.parent / "stock_reports"
STOCK_FILES_DIR = Path(os.getenv("STOCK_FILES_DIR", str(_default_files_dir)))


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _request_json(url: str, *, headers: dict, method: str = "GET", body: bytes | None = None, timeout: int = 30) -> Any:
    req = Request(url, method=method, headers=headers, data=body)
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except HTTPError as exc:
        msg = ""
        try:
            msg = exc.read().decode()[:200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {msg}") from exc


# ── WB Fetcher ────────────────────────────────────────────────────────────────

def fetch_wb_stocks(api_key: str) -> list[dict[str, Any]]:
    """Fetch current stocks from WB /api/v1/supplier/stocks endpoint.

    Returns list of dicts with keys:
      wb_article, seller_article, barcode, warehouse_name, current_stock
    """
    date_from = datetime.now(UTC).strftime("%Y-%m-%d")
    url = f"https://statistics-api.wildberries.ru/api/v1/supplier/stocks?dateFrom={date_from}"
    data = _request_json(url, headers={"Authorization": api_key})
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected WB stocks response type: {type(data)}")
    result = []
    for item in data:
        result.append({
            "wb_article": str(item.get("nmId") or ""),
            "seller_article": str(item.get("supplierArticle") or ""),
            "barcode": str(item.get("barcode") or ""),
            "warehouse_name": str(item.get("warehouseName") or ""),
            "current_stock": int(item.get("quantity") or 0),
        })
    return result


# ── Ozon Fetcher ──────────────────────────────────────────────────────────────

def fetch_ozon_stocks(client_id: str, api_key: str) -> list[dict[str, Any]]:
    """Fetch current stocks from Ozon /v2/analytics/stock_on_warehouses endpoint.

    Returns list of dicts with keys:
      wb_article (sku), seller_article (item_code), warehouse_name, current_stock
    """
    url = "https://api-seller.ozon.ru/v2/analytics/stock_on_warehouses"
    headers = {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }
    offset = 0
    limit = 1000
    all_rows: list[dict[str, Any]] = []
    while True:
        body = json.dumps({"limit": limit, "offset": offset, "warehouse_type": "ALL"}).encode()
        data = _request_json(url, headers=headers, method="POST", body=body)
        rows = (data.get("result") or {}).get("rows") or []
        if not rows:
            break
        for item in rows:
            stock = (
                int(item.get("free_to_sell_amount") or 0)
                + int(item.get("reserved_amount") or 0)
                + int(item.get("promised_amount") or 0)
            )
            all_rows.append({
                "wb_article": str(item.get("sku") or ""),
                "seller_article": str(item.get("item_code") or ""),
                "barcode": "",
                "warehouse_name": str(item.get("warehouse_name") or ""),
                "current_stock": stock,
            })
        if len(rows) < limit:
            break
        offset += limit
        time.sleep(0.12)
    return all_rows


# ── Sync orchestration ────────────────────────────────────────────────────────

def sync_stock_source(
    source: dict[str, Any],
    repository: ReviewRepository,
    *,
    files_dir: Path = STOCK_FILES_DIR,
) -> dict[str, Any]:
    """Download and store stocks for one source. Returns result summary."""
    source_id = int(source["id"])
    user_id = int(source["user_id"])
    marketplace = str(source.get("marketplace") or "").lower()
    api_key = str(source.get("api_key") or "")
    extra = source.get("extra") or {}
    retention_days = int(source.get("retention_days") or 30)
    now_iso = _utc_now_iso()
    report_date = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")

    try:
        if marketplace == "wb":
            rows = fetch_wb_stocks(api_key)
        elif marketplace == "ozon":
            client_id = str(extra.get("client_id") or "")
            rows = fetch_ozon_stocks(client_id, api_key)
        else:
            raise RuntimeError(f"Unknown marketplace: {marketplace}")

        # Save to file
        files_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"stock_{marketplace}_{source_id}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
        file_path = files_dir / file_name
        file_content = json.dumps({"source_id": source_id, "report_date": report_date, "rows": rows}, ensure_ascii=False)
        file_path.write_text(file_content, encoding="utf-8")

        # Save report metadata
        report_id = repository.create_stock_report(
            source_id=source_id,
            user_id=user_id,
            downloaded_at=now_iso,
            file_path=str(file_path),
            file_size=len(file_content.encode()),
            rows_count=len(rows),
            status="ok",
        )

        # Save stock data
        repository.bulk_insert_stock_data(
            source_id=source_id,
            report_id=report_id,
            user_id=user_id,
            report_date=report_date,
            rows=rows,
        )

        # Purge old data
        if retention_days > 0:
            repository.purge_old_stock_data(
                user_id=user_id,
                source_id=source_id,
                retention_days=retention_days,
            )

        # Update last_synced_at
        repository.update_stock_source(
            user_id=user_id,
            source_id=source_id,
            last_synced_at=now_iso,
        )

        _log.info("sync_stock_source: source_id=%d marketplace=%s rows=%d", source_id, marketplace, len(rows))
        return {"ok": True, "source_id": source_id, "rows": len(rows), "report_id": report_id}

    except Exception as exc:
        _log.warning("sync_stock_source: source_id=%d error=%s", source_id, exc)
        repository.create_stock_report(
            source_id=source_id,
            user_id=user_id,
            downloaded_at=now_iso,
            status="error",
            error_message=str(exc)[:400],
        )
        return {"ok": False, "source_id": source_id, "error": str(exc)}


# ── Background scheduler ──────────────────────────────────────────────────────

class StockScheduler:
    """Background worker that downloads stock reports on a per-source interval."""

    def __init__(self, repository: ReviewRepository) -> None:
        self.repository = repository
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="feedpilot-stock-scheduler",
                daemon=True,
            )
            self._thread.start()
            _log.info("StockScheduler: started")

    def stop(self) -> None:
        self._stop_event.set()

    def _loop(self) -> None:
        CHECK_INTERVAL = 60  # check every minute if any source is due
        while not self._stop_event.is_set():
            self._stop_event.wait(CHECK_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                self._run_due_sources()
            except Exception as exc:
                _log.warning("StockScheduler loop error: %s", exc)

    def _run_due_sources(self) -> None:
        """Check all active sources and sync those whose interval has elapsed."""
        try:
            # Find all users with active stock sources
            users = self.repository.list_users(owner_only=True)
        except Exception:
            return
        for user in users:
            user_id = int(user.get("id") or 0)
            if not user_id:
                continue
            try:
                sources = self.repository.list_stock_sources(user_id=user_id, include_secrets=True)
            except Exception:
                continue
            for source in sources:
                if not source.get("is_active"):
                    continue
                try:
                    interval_hours = int(source.get("interval_hours") or 24)
                    last_synced = source.get("last_synced_at")
                    if last_synced:
                        last_dt = datetime.fromisoformat(last_synced.replace("Z", "+00:00"))
                        elapsed_hours = (datetime.now(UTC) - last_dt).total_seconds() / 3600
                        if elapsed_hours < interval_hours:
                            continue
                    sync_stock_source(source, self.repository)
                except Exception as exc:
                    _log.warning("StockScheduler source %s error: %s", source.get("id"), exc)
