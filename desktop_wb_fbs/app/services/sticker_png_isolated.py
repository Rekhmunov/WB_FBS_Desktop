# -*- coding: utf-8 -*-
"""Fetch PNG sticker chunks in an isolated child process.

STATUS_STACK_BUFFER_OVERRUN (-1073740791) during in-process PNG JSON decode
kills the whole app. Running each small chunk in a separate OS process keeps
the UI alive if a chunk hard-crashes; already-saved PNGs stay on disk.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _repo_root() -> Path:
    # app/services/sticker_png_isolated.py -> desktop_wb_fbs/
    return Path(__file__).resolve().parents[2]


def _persist_chunk_worker(
    api_key: str,
    supply_id: str,
    order_ids: List[int],
    result_path: str,
) -> None:
    """Child entrypoint: fetch stickers, write PNGs to disk, emit meta JSON."""
    payload = {"ok": False, "stickers": {}, "error": ""}  # type: Dict[str, Any]
    try:
        from app.services.sticker_file_cache import persist_sticker_png
        from app.wb.client import WbFbsClient

        client = WbFbsClient(api_key)
        raw = client.get_order_stickers(list(order_ids), sticker_type="png")
        out = {}  # type: Dict[str, Dict[str, Any]]
        for st in raw or []:
            if not isinstance(st, dict):
                continue
            try:
                oid = int(st.get("orderId") or st.get("order_id"))
            except (TypeError, ValueError):
                continue
            part_a = str(st.get("partA") or "")
            part_b = str(st.get("partB") or "")
            b64 = st.pop("file", None)
            b64_text = b64 if isinstance(b64, str) else ""
            file_path = ""
            if b64_text:
                file_path = persist_sticker_png(api_key, supply_id, oid, b64_text)
                b64_text = ""
                b64 = None
            out[str(oid)] = {
                "partA": part_a,
                "partB": part_b,
                "barcode": str(st.get("barcode") or "").strip(),
                "file_b64": "",
                "file_path": file_path,
            }
        payload["ok"] = True
        payload["stickers"] = out
        raw = None
    except Exception as exc:
        payload["error"] = str(exc)
    try:
        Path(result_path).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def run_job_file(job_path: str, result_path: str) -> None:
    """Load job JSON and persist chunk (used by child ``-c`` / ``-m`` entry)."""
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    api_key = str(job.get("api_key") or "")
    supply_id = str(job.get("supply_id") or "")
    order_ids = [int(x) for x in (job.get("order_ids") or [])]
    _persist_chunk_worker(api_key, supply_id, order_ids, result_path)


def _parse_result(result_path: Path) -> Dict[int, Dict[str, Any]]:
    if not result_path.is_file():
        return {}
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict) or not data.get("ok"):
        return {}
    out = {}  # type: Dict[int, Dict[str, Any]]
    for key, meta in (data.get("stickers") or {}).items():
        try:
            oid = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(meta, dict):
            out[oid] = {
                "partA": str(meta.get("partA") or ""),
                "partB": str(meta.get("partB") or ""),
                "barcode": str(meta.get("barcode") or ""),
                "file_b64": "",
                "file_path": str(meta.get("file_path") or ""),
            }
    return out


def fetch_png_chunk_isolated(
    api_key: str,
    supply_id: str,
    order_ids: List[int],
    *,
    timeout_sec: float = 180.0,
) -> Dict[int, Dict[str, Any]]:
    """Fetch one PNG chunk in a spawned OS process; return disk-backed meta."""
    ids = [int(x) for x in order_ids if x is not None]
    if not ids or not api_key or not str(supply_id or "").strip():
        return {}

    from app.diag_log import write as diag_write
    from app.paths import app_data_dir

    work = app_data_dir() / "sticker_jobs"
    work.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    job_path = work / "job_{}_{}.json".format(stamp, ids[0])
    result_path = work / "chunk_{}_{}.json".format(stamp, ids[0])
    for path in (job_path, result_path):
        if path.exists():
            try:
                path.unlink()
            except Exception:
                pass

    job_path.write_text(
        json.dumps(
            {
                "api_key": str(api_key),
                "supply_id": str(supply_id),
                "order_ids": ids,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    root = str(_repo_root())
    # Child imports only WB client + disk cache — no Qt, no main UI process.
    code = (
        "import sys; "
        "sys.path.insert(0, {root!r}); "
        "from app.services.sticker_png_isolated import run_job_file; "
        "run_job_file({job!r}, {out!r})"
    ).format(root=root, job=str(job_path), out=str(result_path))

    env = os.environ.copy()
    py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (os.pathsep + py_path if py_path else "")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))

    diag_write(
        "stickers.png_isolated.begin",
        sync=True,
        supply_id=supply_id,
        chunk_orders=len(ids),
        result=str(result_path),
    )

    exit_code = -1
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            env=env,
            timeout=timeout_sec,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        exit_code = int(completed.returncode)
    except subprocess.TimeoutExpired:
        diag_write(
            "stickers.png_isolated.timeout",
            sync=True,
            supply_id=supply_id,
            chunk_orders=len(ids),
        )
        return {}
    except Exception as exc:
        diag_write(
            "stickers.png_isolated.spawn_error",
            sync=True,
            supply_id=supply_id,
            error=str(exc),
        )
        return {}
    finally:
        try:
            job_path.unlink()
        except Exception:
            pass

    # Windows hard-kill often surfaces as 0xC0000409 (-1073740791).
    if exit_code not in (0,):
        diag_write(
            "stickers.png_isolated.child_crash",
            sync=True,
            supply_id=supply_id,
            chunk_orders=len(ids),
            exit_code=exit_code,
        )
        # Child may have saved some PNGs before dying — parent still lives.
        return _parse_result(result_path)

    out = _parse_result(result_path)
    try:
        result_path.unlink()
    except Exception:
        pass

    if not out:
        diag_write(
            "stickers.png_isolated.no_result",
            sync=True,
            supply_id=supply_id,
            chunk_orders=len(ids),
            exit_code=exit_code,
        )
        return {}

    diag_write(
        "stickers.png_isolated.done",
        sync=True,
        supply_id=supply_id,
        saved=len(out),
    )
    return out


def fetch_png_ids_isolated(
    api_key: str,
    supply_id: str,
    order_ids: List[int],
    *,
    chunk_size: int = 100,
    progress: Optional[Any] = None,
    timeout_sec: float = 180.0,
) -> Dict[int, Dict[str, Any]]:
    """Fetch many PNG stickers via isolated chunks; retry singles after crash."""
    ids = [int(x) for x in order_ids if x is not None]
    if not ids:
        return {}
    step = max(1, int(chunk_size or 100))
    out = {}  # type: Dict[int, Dict[str, Any]]
    total = len(ids)
    if progress:
        progress(0, total)

    for i in range(0, len(ids), step):
        if i:
            time.sleep(0.21)
        chunk = ids[i : i + step]
        part = fetch_png_chunk_isolated(
            api_key, supply_id, chunk, timeout_sec=timeout_sec
        )
        if not part and len(chunk) > 1:
            # Blast radius: retry one-by-one so one bad payload cannot skip many.
            for oid in chunk:
                single = fetch_png_chunk_isolated(
                    api_key, supply_id, [oid], timeout_sec=timeout_sec
                )
                out.update(single)
                if progress:
                    progress(min(len(out), total), total)
                time.sleep(0.05)
        else:
            out.update(part)
            if progress:
                progress(min(i + len(chunk), total), total)
    return out


def _cli(argv: Optional[List[str]] = None) -> int:
    """Manual debug: python -m app.services.sticker_png_isolated job.json out.json"""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2:
        run_job_file(args[0], args[1])
        return 0
    if len(args) < 4:
        sys.stderr.write(
            "usage: sticker_png_isolated <job.json> <out.json>\n"
            "   or: sticker_png_isolated <api_key> <supply_id> <out.json> <oid>...\n"
        )
        return 2
    api_key, supply_id, out_path = args[0], args[1], args[2]
    ids = [int(x) for x in args[3:]]
    _persist_chunk_worker(api_key, supply_id, ids, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
