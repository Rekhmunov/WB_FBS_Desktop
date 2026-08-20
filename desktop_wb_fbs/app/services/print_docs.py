# -*- coding: utf-8 -*-
"""Print HTML for picking lists and stickers — portal-like (no web server)."""
from __future__ import annotations

import copy
import hashlib
import html
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from PyQt5.QtWidgets import QWidget

from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QDesktopServices

from app.db import Database
from app.diag_log import write as diag_write
from app.services.catalog import ProductService
from app.services.orders import OrdersService
from app.services.sticker_file_cache import sticker_img_src
from app.wb import parse_json_list
from app.wb.client import WbFbsClient
from app.wb.content import WbContentClient


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


_CARD_META_CACHE_TTL_SEC = 1800.0
_card_meta_cache = {}  # type: Dict[tuple, tuple]
_card_meta_cache_lock = threading.Lock()

_STICKERS_CACHE_TTL_SEC = 120.0
_stickers_cache = {}  # type: Dict[tuple, tuple]
_stickers_cache_lock = threading.Lock()
# Match web `_fetch_stickers_map` (chunks of 100). Smaller chunks were only
# needed to limit blast radius during in-UI preload; print still isolates each
# chunk in a child process, so 100 is safe and much faster (fewer spawns/sleeps).
_PNG_STICKER_CHUNK = 100


def _api_key_fp(api_key: str) -> str:
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]


def _stickers_cache_key(
    api_key: str, order_ids: List[int], sticker_type: str, keep_files: bool
) -> tuple:
    ids = tuple(sorted({int(x) for x in order_ids if x is not None}))
    stype = str(sticker_type or "png").strip().lower() or "png"
    return (_api_key_fp(api_key), stype, bool(keep_files), ids)


def _shallow_copy_stickers(
    data: Dict[int, Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """Copy sticker map without duplicating large base64 payloads."""
    return {int(oid): dict(meta) for oid, meta in data.items()}


def _cache_get_stickers(key: tuple) -> Optional[Dict[int, Dict[str, Any]]]:
    with _stickers_cache_lock:
        item = _stickers_cache.get(key)
        if not item:
            return None
        ts, data = item
        if time.monotonic() - ts > _STICKERS_CACHE_TTL_SEC:
            _stickers_cache.pop(key, None)
            return None
        return _shallow_copy_stickers(data)


def _cache_put_stickers(key: tuple, data: Dict[int, Dict[str, Any]]) -> None:
    if not data:
        return
    with _stickers_cache_lock:
        _stickers_cache[key] = (time.monotonic(), _shallow_copy_stickers(data))


def _cache_merge_stickers(key: tuple, chunk: Dict[int, Dict[str, Any]]) -> None:
    if not chunk:
        return
    with _stickers_cache_lock:
        item = _stickers_cache.get(key)
        if item:
            _, existing = item
            merged = dict(existing)
            merged.update(chunk)
        else:
            merged = dict(chunk)
        _stickers_cache[key] = (time.monotonic(), merged)


def _cache_sticker_count(key: tuple) -> int:
    with _stickers_cache_lock:
        item = _stickers_cache.get(key)
        if not item:
            return 0
        ts, data = item
        if time.monotonic() - ts > _STICKERS_CACHE_TTL_SEC:
            _stickers_cache.pop(key, None)
            return 0
        return len(data)


def get_cached_stickers_map(
    api_key: str,
    order_ids: List[int],
    *,
    sticker_type: str = "png",
    keep_files: bool = True,
) -> Optional[Dict[int, Dict[str, Any]]]:
    """Return cached WB stickers for the exact order-id set, if present."""
    ids = [int(x) for x in order_ids if x is not None]
    if not ids or not api_key:
        return None
    cache_key = _stickers_cache_key(api_key, ids, sticker_type, keep_files)
    return _cache_get_stickers(cache_key)


def _cache_get_card_meta(fp: str, nm_id: int) -> Optional[Dict[str, Any]]:
    with _card_meta_cache_lock:
        item = _card_meta_cache.get((fp, nm_id))
        if not item:
            return None
        ts, data = item
        if time.monotonic() - ts > _CARD_META_CACHE_TTL_SEC:
            _card_meta_cache.pop((fp, nm_id), None)
            return None
        return copy.deepcopy(data)


def _cache_put_card_meta(fp: str, nm_id: int, card: Dict[str, Any]) -> None:
    with _card_meta_cache_lock:
        _card_meta_cache[(fp, nm_id)] = (time.monotonic(), copy.deepcopy(card))


def _clear_override_cursor() -> None:
    """Drop wait-cursor before modal UI (print preview / message boxes)."""
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    while app.overrideCursor() is not None:
        app.restoreOverrideCursor()


def open_html(
    html_doc: str,
    basename: str,
    *,
    parent: Optional["QWidget"] = None,
    title: str = "",
    output_dir: Optional[Path] = None,
) -> Path:
    from PyQt5.QtWidgets import QMessageBox

    if output_dir is not None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / "{}.html".format(basename)
    else:
        path = Path(tempfile.gettempdir()) / "{}.html".format(basename)
    path.write_text(html_doc, encoding="utf-8")
    preview_error = ""
    webengine_ok = False
    try:
        from app.ui.html_print_dialog import show_html_print_preview, webengine_status

        webengine_ok, status = webengine_status()
        if webengine_ok:
            # Wait-cursor must not stay while the modal print dialog is open.
            _clear_override_cursor()
            # True ⇒ in-app preview was shown (even if loadFinished was flaky).
            if show_html_print_preview(path, title=title or basename, parent=parent):
                return path
            preview_error = "не удалось открыть встроенное окно предпросмотра"
        else:
            preview_error = status
    except Exception as exc:
        preview_error = str(exc) or exc.__class__.__name__
    _clear_override_cursor()
    # Fallback only when in-app preview could not be shown at all.
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    if parent is not None:
        if webengine_ok:
            detail = (
                "Документ открыт в браузере для печати.\n\n"
                "Встроенный предпросмотр не открылся."
            )
        else:
            detail = (
                "Документ открыт в браузере.\n\n"
                "Встроенный предпросмотр недоступен."
            )
        if preview_error:
            detail += "\n\nПричина: {}".format(preview_error)
        elif not webengine_ok:
            detail += (
                "\n\nУстановите пакет PyQtWebEngine "
                "(python -m pip install PyQtWebEngine) и перезапустите приложение."
            )
        QMessageBox.information(parent, "Печать", detail)
    return path


def _product_title(
    order: Dict[str, Any],
    cards: Dict[int, Dict[str, Any]],
    products_by_article: Dict[str, Dict[str, Any]],
    products_by_nm: Dict[str, Dict[str, Any]],
) -> Tuple[str, str, str]:
    """Return (title, brand, color)."""
    article = str(order.get("article") or "").strip()
    nm_raw = order.get("nm_id")
    try:
        nm = int(nm_raw) if nm_raw is not None else None
    except (TypeError, ValueError):
        nm = None
    card = cards.get(nm) if nm is not None else None
    title = ""
    brand = ""
    color = ""
    if isinstance(card, dict):
        title = str(card.get("title") or "").strip()
        brand = str(card.get("brand") or "").strip()
        colors = card.get("colors") or card.get("characteristics")
        if isinstance(card.get("colors"), list) and card["colors"]:
            c0 = card["colors"][0]
            if isinstance(c0, dict):
                color = str(c0.get("name") or "").strip()
            else:
                color = str(c0).strip()
    local = products_by_article.get(article.lower()) or (
        products_by_nm.get(str(nm)) if nm is not None else None
    )
    if local and not title:
        title = str(local.get("name") or "").strip()
    if not title:
        title = article or (str(nm) if nm else "—")
    return title, brand, color


def build_groups(
    orders: List[Dict[str, Any]],
    stickers_by_oid: Dict[int, Dict[str, Any]],
    cards: Dict[int, Dict[str, Any]],
    products: List[Dict[str, Any]],
    *,
    sticker_html_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    by_article = {
        str(p.get("supplier_article") or "").strip().lower(): p for p in products
    }
    by_nm = {str(p.get("wb_nmid") or "").strip(): p for p in products if p.get("wb_nmid")}
    grouped = OrderedDict()  # type: OrderedDict[str, Dict[str, Any]]
    for o in orders:
        article = str(o.get("article") or "").strip()
        title, brand, color = _product_title(o, cards, by_article, by_nm)
        local = by_article.get(article.lower()) or by_nm.get(str(o.get("nm_id") or ""))
        category = str((local or {}).get("product_category") or "").strip()
        photo = str((local or {}).get("photo_path") or "").strip()
        key = "{}|{}|{}|{}".format(article, title, color, category)
        if key not in grouped:
            grouped[key] = {
                "article": article,
                "product_name": title,
                "brand": brand,
                "color": color,
                "category": category,
                "product_photo": photo,
                "nm_id": o.get("nm_id"),
                "barcodes": [],
                "orders": [],
                "order_ids": [],
                "qty": 0,
                "group_key": key,
            }
        g = grouped[key]
        skus = o.get("skus") if isinstance(o.get("skus"), list) else parse_json_list(
            o.get("skus_json")
        )
        for s in skus:
            s = str(s).strip()
            if s and s not in g["barcodes"]:
                g["barcodes"].append(s)
        oid = int(o["order_id"])
        st = stickers_by_oid.get(oid) or {}
        g["orders"].append(
            {
                "order_id": oid,
                "sticker_part_a": st.get("partA") or "",
                "sticker_part_b": st.get("partB") or "",
                "sticker_src": sticker_img_src(st, relative_to=sticker_html_dir),
                "article": article,
            }
        )
        g["order_ids"].append(oid)
        g["qty"] = len(g["orders"])
    return list(grouped.values())


def sticker_groups_for_category_print(
    db: Database,
    orders_svc: OrdersService,
    source_id: int,
    api_key: str,
    supply_id: str,
) -> List[Dict[str, Any]]:
    rows = orders_svc.orders_in_supply(source_id, supply_id, api_key=api_key)
    products = ProductService(db).list_all()
    cards = fetch_cards(api_key, rows)
    groups = build_groups(rows, {}, cards, products)
    # Collapse by category for the modal: one row per product group
    out = []
    for g in groups:
        out.append(
            {
                "group_key": g.get("group_key") or g.get("article"),
                "product_name": g.get("product_name"),
                "article": g.get("article"),
                "category": g.get("category") or "Без категории",
                "qty": g.get("qty") or 0,
                "order_ids": list(g.get("order_ids") or []),
            }
        )
    return out


def fetch_stickers_map(
    api_key: str,
    order_ids: List[int],
    *,
    sticker_type: str = "png",
    keep_files: bool = True,
    progress: Optional[Callable[[int, int], None]] = None,
    chunk_size: Optional[int] = None,
    cache_only: bool = False,
    persist_supply_id: Optional[str] = None,
) -> Dict[int, Dict[str, Any]]:
    """Fetch WB order stickers. Picking list only needs partA/partB — use svg + keep_files=False."""
    ids = [int(x) for x in order_ids if x is not None]
    if not ids:
        if progress:
            progress(0, 0)
        return {}
    total = len(ids)
    if progress:
        progress(0, total)
    stype = str(sticker_type or "png").strip().lower() or "png"
    cache_key = None
    if api_key:
        cache_key = _stickers_cache_key(api_key, ids, stype, keep_files)
        cached = _cache_get_stickers(cache_key)
        if cached is not None:
            if progress:
                progress(total, total)
            return cached
    client = WbFbsClient(api_key)
    out = {}  # type: Dict[int, Dict[str, Any]]
    step = int(chunk_size or 0)
    if step <= 0:
        step = _PNG_STICKER_CHUNK if stype == "png" and keep_files else 100
    persist_disk = bool(
        keep_files
        and stype == "png"
        and str(persist_supply_id or "").strip()
        and api_key
    )
    diag_write(
        "stickers.fetch.begin",
        sync=True,
        supply_id=persist_supply_id or "",
        sticker_type=stype,
        order_total=total,
        chunk_size=step,
        cache_only=cache_only,
        persist_disk=persist_disk,
        isolated=persist_disk,
    )
    # PNG + disk persist: never decode huge base64 JSON in the UI process.
    if persist_disk:
        from app.services.sticker_png_isolated import fetch_png_ids_isolated

        isolated = fetch_png_ids_isolated(
            api_key,
            str(persist_supply_id),
            ids,
            chunk_size=step,
            progress=progress,
        )
        if cache_key is not None and isolated:
            _cache_merge_stickers(cache_key, isolated)
        if not cache_only:
            out.update(isolated)
        diag_write(
            "stickers.fetch.done",
            sync=True,
            supply_id=persist_supply_id or "",
            order_total=total,
            cache_only=cache_only,
            isolated=True,
            saved=len(isolated),
        )
        if cache_only:
            return {}
        return out

    for i in range(0, len(ids), step):
        if i:
            time.sleep(0.21)
        chunk = ids[i : i + step]
        chunk_no = (i // step) + 1
        chunk_total = (len(ids) + step - 1) // step
        diag_write(
            "stickers.fetch.chunk_begin",
            sync=True,
            supply_id=persist_supply_id or "",
            chunk=chunk_no,
            chunk_total=chunk_total,
            chunk_orders=len(chunk),
            progress_done=i,
            progress_total=total,
        )
        chunk_out = {}  # type: Dict[int, Dict[str, Any]]
        chunk_b64_bytes = 0
        stickers_raw = client.get_order_stickers(chunk, sticker_type=stype)
        diag_write(
            "stickers.fetch.api_done",
            sync=True,
            supply_id=persist_supply_id or "",
            chunk=chunk_no,
            stickers_returned=len(stickers_raw or []),
        )
        for st in stickers_raw:
            if not isinstance(st, dict):
                continue
            try:
                oid = int(st.get("orderId") or st.get("order_id"))
            except (TypeError, ValueError):
                continue
            part_a = str(st.get("partA") or "")
            part_b = str(st.get("partB") or "")
            barcode = str(st.get("barcode") or "").strip()
            if keep_files:
                b64 = st.pop("file", None)
                b64_text = b64 if isinstance(b64, str) else ""
                chunk_b64_bytes += len(b64_text)
                meta = {
                    "partA": part_a,
                    "partB": part_b,
                    "barcode": barcode,
                    "file_b64": b64_text,
                    "file_path": "",
                }
            else:
                st.pop("file", None)
                meta = {
                    "partA": part_a,
                    "partB": part_b,
                    "barcode": barcode,
                    "file_b64": "",
                    "file_path": "",
                }
            chunk_out[oid] = meta
            if not cache_only:
                out[oid] = meta
        if cache_key is not None and chunk_out:
            _cache_merge_stickers(cache_key, chunk_out)
        chunk_saved = len(chunk_out)
        chunk_out.clear()
        stickers_raw = None
        try:
            import gc

            gc.collect()
        except Exception:
            pass
        if progress:
            progress(min(i + len(chunk), total), total)
        diag_write(
            "stickers.fetch.chunk_done",
            sync=True,
            supply_id=persist_supply_id or "",
            chunk=chunk_no,
            chunk_total=chunk_total,
            saved=chunk_saved,
            b64_chars=chunk_b64_bytes,
            progress_done=min(i + len(chunk), total),
            progress_total=total,
        )
    diag_write(
        "stickers.fetch.done",
        sync=True,
        supply_id=persist_supply_id or "",
        order_total=total,
        cache_only=cache_only,
    )
    if cache_only:
        return {}
    return out


def _fetch_picking_stickers(
    api_key: str,
    order_ids: List[int],
    *,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[int, Dict[str, Any]]:
    """Lightweight sticker numbers for picking list (no PNG payloads)."""
    if not order_ids or not api_key:
        if progress:
            progress(0, 0)
        return {}
    total = len(order_ids)
    if progress:
        progress(0, total)
    stickers = fetch_stickers_map(
        api_key,
        order_ids,
        sticker_type="svg",
        keep_files=False,
        progress=progress,
    )
    missing = [
        int(oid)
        for oid in order_ids
        if not str((stickers.get(int(oid)) or {}).get("partB") or "").strip()
    ]
    if missing:
        base_done = total - len(missing)

        def _png_progress(done: int, _phase_total: int) -> None:
            if progress:
                progress(min(base_done + done, total), total)

        png_part = fetch_stickers_map(
            api_key,
            missing,
            sticker_type="png",
            keep_files=False,
            progress=_png_progress,
        )
        stickers.update(png_part)
    elif progress:
        progress(total, total)
    return stickers


def fetch_cards(
    api_key: str,
    orders: List[Dict[str, Any]],
    *,
    network: bool = True,
) -> Dict[int, Dict[str, Any]]:
    nm_ids = []  # type: List[int]
    seen = set()  # type: set
    for o in orders:
        try:
            nm = int(o.get("nm_id"))
        except (TypeError, ValueError):
            continue
        if nm in seen:
            continue
        seen.add(nm)
        nm_ids.append(nm)
    if not nm_ids:
        return {}

    fp = _api_key_fp(api_key) if api_key else ""
    out = {}  # type: Dict[int, Dict[str, Any]]
    missing = []  # type: List[int]
    for nm in nm_ids:
        cached = _cache_get_card_meta(fp, nm) if fp else None
        if cached is not None:
            out[nm] = cached
        else:
            missing.append(nm)

    if not network or not missing or not api_key:
        return out

    try:
        fetched = WbContentClient(api_key).get_cards_by_nm_ids(missing)
    except Exception:
        fetched = {}
    for nm, card in fetched.items():
        out[int(nm)] = card
        if fp:
            _cache_put_card_meta(fp, int(nm), card)
    return out


def render_picking_list_html(
    supply_id: str,
    supply_name: str,
    groups: List[Dict[str, Any]],
    variant: str = "summary",
) -> str:
    mode = "extended" if str(variant).lower() == "extended" else "summary"
    total = sum(int(g.get("qty") or 0) for g in groups)
    order_word = (
        "заказ"
        if total % 10 == 1 and total % 100 != 11
        else (
            "заказа"
            if 2 <= total % 10 <= 4 and not (12 <= total % 100 <= 14)
            else "заказов"
        )
    )
    box = '<span class="box" aria-hidden="true"></span>'
    title_prefix = "Лист подбора" if mode == "summary" else "Расширенный лист подбора"

    if mode == "summary":
        summary_qty = OrderedDict()  # type: OrderedDict[str, int]
        for g in groups:
            name = str(g.get("product_name") or "—")
            summary_qty[name] = summary_qty.get(name, 0) + int(g.get("qty") or 0)
        rows = []
        for name, qty in summary_qty.items():
            rows.append(
                '<tr class="summary-row"><td class="main">{} — {} шт.</td>'
                '<td class="check">{}</td></tr>'.format(_esc(name), qty, box)
            )
        body = """
        <section class="summary-page">
          <table class="picking summary">
            <tbody>
              <tr class="totals-row">
                <th class="main">Всего {total} {word}</th>
                <th class="check">Собрано</th>
              </tr>
              {rows}
            </tbody>
          </table>
        </section>
        """.format(
            total=total, word=order_word, rows="".join(rows) or "<tr><td>Нет заказов</td></tr>"
        )
    else:
        part_b_counts = {}  # type: Dict[str, int]
        for g in groups:
            for o in g.get("orders") or []:
                pb = str(o.get("sticker_part_b") or "").strip()
                if pb:
                    part_b_counts[pb] = part_b_counts.get(pb, 0) + 1
        dup = {pb for pb, n in part_b_counts.items() if n > 1}
        rows = []
        for g in groups:
            orders = list(g.get("orders") or [])
            qty = int(g.get("qty") or len(orders))
            meta = ['<div class="sku-title">{}</div>'.format(_esc(g.get("product_name")))]
            if g.get("brand"):
                meta.append('<div class="sku-meta">{}</div>'.format(_esc(g.get("brand"))))
            if g.get("article"):
                meta.append(
                    '<div class="sku-article">{}</div>'.format(_esc(g.get("article")))
                )
            for b in g.get("barcodes") or []:
                meta.append('<div class="sku-barcode">{}</div>'.format(_esc(b)))
            if g.get("color"):
                meta.append(
                    '<div class="sku-color">Цвет: {}</div>'.format(_esc(g.get("color")))
                )
            meta.append('<div class="sku-qty">{} шт</div>'.format(qty))
            rows.append(
                '<tr class="product-row"><td class="main" colspan="3">'
                '<div class="sku-text">{}</div></td></tr>'.format("".join(meta))
            )
            for o in orders:
                pb = str(o.get("sticker_part_b") or "").strip()
                pa = str(o.get("sticker_part_a") or "").strip()
                sticker = (pa + pb) if (pa or pb) else "—"
                cls = " dup" if pb in dup else ""
                rows.append(
                    '<tr class="order-row{cls}"><td class="oid">{oid}</td>'
                    '<td class="sticker">{st}</td>'
                    '<td class="check">{box}</td></tr>'.format(
                        cls=cls,
                        oid=_esc(o.get("order_id")),
                        st=_esc(sticker),
                        box=box,
                    )
                )
        body = """
        <table class="picking extended">
          <thead>
            <tr>
              <th>Заказ</th><th>Стикер</th><th>Собрано</th>
            </tr>
          </thead>
          <tbody>
            <tr class="totals-row">
              <th colspan="2">Всего {total} {word}</th>
              <th>Собрано</th>
            </tr>
            {rows}
          </tbody>
        </table>
        """.format(
            total=total, word=order_word, rows="".join(rows)
        )

    return """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"/>
<title>{title} · {sid}</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  body {{ font-family: Arial, sans-serif; color: #0f172a; font-size: 12px; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .sub {{ color: #64748b; margin-bottom: 16px; }}
  table.picking {{ width: 100%; border-collapse: collapse; }}
  table.picking th, table.picking td {{
    border-bottom: 1px solid #e2e8f0; padding: 6px 8px; vertical-align: top;
  }}
  .totals-row th {{ background: #f8fafc; text-align: left; }}
  .box {{
    display: inline-block; width: 14px; height: 14px;
    border: 1.5px solid #334155; border-radius: 2px;
  }}
  .sku-title {{ font-weight: 700; }}
  .sku-meta, .sku-article, .sku-barcode, .sku-color, .sku-qty {{ color: #475569; }}
  .order-row.dup .sticker {{ color: #b91c1c; font-weight: 700; }}
  .check {{ width: 70px; text-align: center; }}
  @media print {{ .no-print {{ display: none; }} }}
</style></head>
<body>
  <h1>{title}</h1>
  <div class="sub">{name} · ID {sid}</div>
  {body}
</body></html>
""".format(
        title=_esc(title_prefix),
        sid=_esc(supply_id),
        name=_esc(supply_name or supply_id),
        body=body,
    )


def render_stickers_print_html(supply_id: str, groups: List[Dict[str, Any]]) -> str:
    pages = []  # type: List[str]
    page_no = 0
    for g in groups:
        qty = int(g.get("qty") or 0)
        color = str(g.get("color") or "").strip()
        brand = str(g.get("brand") or "").strip()
        article = str(g.get("article") or "")
        name = str(g.get("product_name") or "").strip() or "—"
        barcodes = g.get("barcodes") or []
        barcode = str(barcodes[0] if barcodes else "")
        nm = g.get("nm_id") or "—"
        color_line = (
            '<div class="line">Цвет: {}</div>'.format(_esc(color)) if color else ""
        )
        brand_line = (
            '<div class="line">Бренд: {}</div>'.format(_esc(brand)) if brand else ""
        )
        page_no += 1
        pages.append(
            """
            <section class="label separator">
              <div class="qty">{qty} шт.</div>
              <div class="title">{title}</div>
              {brand}
              {color}
              <div class="line">Артикул WB: {nm}</div>
              <div class="line">Баркод: {bc}</div>
              <div class="line">Артикул: {art}</div>
              <div class="hint">
                <span>Артикул для подбора · Не нужно клеить</span>
                <span class="page">{page}</span>
              </div>
            </section>
            """.format(
                qty=qty,
                title=_esc(name),
                brand=brand_line,
                color=color_line,
                nm=_esc(nm),
                bc=_esc(barcode or "—"),
                art=_esc(article),
                page=page_no,
            )
        )
        for o in g.get("orders") or []:
            page_no += 1
            src = str(o.get("sticker_src") or o.get("sticker_file") or "").strip()
            # Relative filenames (same folder as HTML), file:// and data: must stay as-is.
            # Only wrap bare legacy base64 (no scheme / no path / no image extension).
            if src and not (
                src.startswith("data:")
                or src.startswith("file:")
                or "/" in src
                or "\\" in src
                or src.lower().endswith(
                    (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")
                )
            ):
                src = "data:image/png;base64,{}".format(src)
            if not src:
                pages.append(
                    """
                    <section class="label missing">
                      <div>Нет стикера</div>
                      <div>Заказ {}</div>
                    </section>
                    """.format(
                        _esc(o.get("order_id"))
                    )
                )
                continue
            pages.append(
                """
                <section class="label sticker">
                  <img src="{}" alt="sticker {}" />
                </section>
                """.format(
                    _esc(src), _esc(o.get("order_id"))
                )
            )
    return """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"/>
<title>Стикеры поставки {sid}</title>
<style>
  @page {{ size: 58mm 40mm; margin: 0; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{ font-family: Arial, sans-serif; color: #0f172a; }}
  .no-print {{ margin: 8px; }}
  .label {{
    width: 58mm; height: 40mm; page-break-after: always;
    overflow: hidden; position: relative;
  }}
  .label:last-child {{ page-break-after: auto; }}
  .label.separator {{
    padding: 2.5mm 3mm; background: #fff;
    border: 0.3mm dashed #94a3b8;
    display: flex; flex-direction: column; gap: 0.6mm;
  }}
  .label.separator .qty {{ font-size: 16px; font-weight: 800; }}
  .label.separator .title {{
    font-size: 12px; font-weight: 800; line-height: 1.2;
    max-height: 14mm; overflow: hidden;
  }}
  .label.separator .line {{ font-size: 8px; line-height: 1.25; }}
  .label.separator .hint {{
    margin-top: auto; font-size: 7px; color: #64748b; font-weight: 600;
    display: flex; justify-content: space-between; gap: 2mm;
  }}
  .label.separator .hint .page {{ font-size: 9px; font-weight: 800; color: #0f172a; }}
  .label.sticker {{ display: flex; align-items: center; justify-content: center; }}
  .label.sticker img {{ width: 58mm; height: 40mm; object-fit: contain; }}
  .label.missing {{
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    font-size: 10px; color: #b91c1c;
  }}
  @media print {{ .no-print {{ display: none; }} }}
</style></head>
<body>
  {pages}
</body></html>
""".format(
        sid=_esc(supply_id), pages="".join(pages)
    )


def print_picking_list(
    db: Database,
    orders_svc: OrdersService,
    source_id: int,
    api_key: str,
    supply_id: str,
    variant: str = "summary",
    preloaded_stickers: Optional[Dict[int, Dict[str, Any]]] = None,
    parent: Optional["QWidget"] = None,
) -> Path:
    supply = orders_svc.get_supply(source_id, supply_id) or {}
    rows = orders_svc.orders_in_supply(source_id, supply_id, api_key="")
    if not rows and api_key:
        rows = orders_svc.orders_in_supply(source_id, supply_id, api_key=api_key)
    ids = [int(r["order_id"]) for r in rows]
    products = ProductService(db).list_all()
    is_extended = str(variant).lower() == "extended"
    if is_extended:
        if preloaded_stickers is not None:
            stickers = {
                int(oid): dict(meta)
                for oid, meta in preloaded_stickers.items()
                if oid is not None
            }
            missing = [
                oid
                for oid in ids
                if not str((stickers.get(oid) or {}).get("partB") or "").strip()
            ]
            if missing and api_key:
                stickers.update(_fetch_picking_stickers(api_key, missing))
        else:
            stickers = _fetch_picking_stickers(api_key, ids)
        # Extended list uses local catalog names; Content API is optional (slow).
        cards = {}
    else:
        # Summary list only needs local product names and counts — no WB API.
        stickers = {}
        cards = {}
    groups = build_groups(rows, stickers, cards, products)
    html_doc = render_picking_list_html(
        supply_id,
        str(supply.get("name") or ""),
        groups,
        variant=variant,
    )
    title = (
        "Расширенный лист подбора"
        if str(variant).lower() == "extended"
        else "Лист подбора"
    )
    return open_html(
        html_doc,
        "feedpilot_picking_{}_{}".format(variant, supply_id),
        parent=parent,
        title=title,
    )


def print_supply_stickers(
    db: Database,
    orders_svc: OrdersService,
    source_id: int,
    api_key: str,
    supply_id: str,
    order_ids: Optional[List[int]] = None,
    parent: Optional["QWidget"] = None,
    preloaded_stickers: Optional[Dict[int, Dict[str, Any]]] = None,
) -> Path:
    """Print product stickers. PNG is loaded on demand (web parity), not on supply open."""
    from app.services.sticker_file_cache import existing_sticker_paths

    rows = orders_svc.orders_in_supply(source_id, supply_id, api_key="")
    if not rows and api_key:
        rows = orders_svc.orders_in_supply(source_id, supply_id, api_key=api_key)
    if order_ids is not None:
        want = set(int(x) for x in order_ids)
        rows = [r for r in rows if int(r["order_id"]) in want]
    ids = [int(r["order_id"]) for r in rows]
    stickers = {}  # type: Dict[int, Dict[str, Any]]
    if preloaded_stickers is not None:
        stickers = {
            int(oid): dict(meta)
            for oid, meta in preloaded_stickers.items()
            if oid is not None
        }
    # Resume PNGs already on disk from earlier prints (fast path).
    if ids and api_key and supply_id:
        on_disk = existing_sticker_paths(api_key, supply_id, ids)
        for oid, path in on_disk.items():
            if oid in stickers and str((stickers[oid] or {}).get("file_path") or "").strip():
                continue
            prev = stickers.get(oid) or {}
            stickers[oid] = {
                "partA": str(prev.get("partA") or ""),
                "partB": str(prev.get("partB") or ""),
                "file_b64": "",
                "file_path": path,
            }
    missing = [
        oid
        for oid in ids
        if not str((stickers.get(oid) or {}).get("file_path") or "").strip()
        and not str((stickers.get(oid) or {}).get("file_b64") or "").strip()
    ]
    if missing and api_key:
        # Isolated child-process fetch — same crash-safe path as preload used to use.
        stickers.update(
            fetch_stickers_map(
                api_key,
                missing,
                persist_supply_id=supply_id,
            )
        )
    cards = fetch_cards(api_key, rows)
    products = ProductService(db).list_all()
    html_dir = None  # type: Optional[Path]
    if api_key and supply_id:
        from app.services.sticker_file_cache import supply_sticker_dir

        html_dir = supply_sticker_dir(api_key, supply_id)
    groups = build_groups(
        rows, stickers, cards, products, sticker_html_dir=html_dir
    )
    html_doc = render_stickers_print_html(supply_id, groups)
    return open_html(
        html_doc,
        "feedpilot_stickers_{}".format(supply_id),
        parent=parent,
        title="Стикеры поставки",
        output_dir=html_dir,
    )
