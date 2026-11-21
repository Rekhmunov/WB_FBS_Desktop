# -*- coding: utf-8 -*-
"""Ozon FBS stickers print sheet — web archive parity (one HTML, article separators).

Fetches v3 package-label PDFs in batches, rasterizes pages, then builds a single
58×40 mm print HTML: cover → per-article separator → posting stickers.
"""
from __future__ import annotations

import base64
import html
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from app.ozon.client import OzonFbsClient
from app.runtime_deps import _can_import, pip_install
from app.services.ozon_labels import OzonLabelService, emit_label_progress

_log = logging.getLogger(__name__)

_LABEL_BATCH = 200
_FETCH_WORKERS = 2
ProgressCb = Optional[Callable[..., None]]
StopCb = Optional[Callable[[], bool]]
_pymupdf_ready = False


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _try_import_pymupdf() -> bool:
    return _can_import("pymupdf")


def ensure_pymupdf(progress: ProgressCb = None) -> None:
    """Import pymupdf, installing it once via pip if missing (desktop UX)."""
    global _pymupdf_ready
    if _pymupdf_ready:
        return
    if _try_import_pymupdf():
        _pymupdf_ready = True
        return
    emit_label_progress(
        progress,
        "Установка pymupdf (нужен для листа стикеров)…",
        done=0,
        total=1,
    )
    try:
        pip_install("pymupdf>=1.24.0")
    except Exception as exc:
        raise RuntimeError(
            "Для печати стикеров Ozon нужен пакет pymupdf.\n\n"
            "Откройте cmd в папке программы и выполните:\n"
            "python -m pip install pymupdf\n\n"
            "или: python -m pip install -r requirements.txt\n\n"
            "Ошибка установки: {}".format(exc)
        ) from exc
    if not _try_import_pymupdf():
        raise RuntimeError(
            "pymupdf установился, но не импортируется. "
            "Перезапустите FeedPilot FBS и повторите печать."
        )
    _pymupdf_ready = True


def _pdf_pages_to_png_b64(pdf_bytes: bytes) -> List[str]:
    ensure_pymupdf()
    import pymupdf

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = []  # type: List[str]
    try:
        for page in doc:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
            out.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
    finally:
        doc.close()
    return out


def _assign_pages(batch: Sequence[str], pages: Sequence[str]) -> Dict[str, List[str]]:
    result = {pn: [] for pn in batch}  # type: Dict[str, List[str]]
    if len(pages) == len(batch):
        for pn, page in zip(batch, pages):
            result[pn] = [page]
    return result


def _fetch_images_for_batch(
    labels: OzonLabelService,
    client: OzonFbsClient,
    batch: List[str],
    *,
    timeout_s: float = 180.0,
) -> Dict[str, List[str]]:
    clean = [str(p).strip() for p in batch if str(p).strip()]
    if not clean:
        return {}
    if len(clean) == 1:
        try:
            pdf = labels._fetch_async_pdf(client, clean, timeout_s=timeout_s)
            return {clean[0]: _pdf_pages_to_png_b64(pdf)}
        except Exception as exc:
            if "pymupdf" in str(exc).casefold() or isinstance(exc, ImportError):
                raise
            _log.warning("ozon sticker sheet single %s: %s", clean[0], exc)
            return {clean[0]: []}
    try:
        pdf = labels._fetch_async_pdf(client, clean, timeout_s=timeout_s)
        pages = _pdf_pages_to_png_b64(pdf)
        assigned = _assign_pages(clean, pages)
        if all(assigned.get(pn) for pn in clean):
            return assigned
        _log.warning(
            "ozon sticker sheet page mismatch batch=%s pages=%s — split",
            len(clean),
            len(pages),
        )
    except Exception as exc:
        if "pymupdf" in str(exc).casefold() or isinstance(exc, ImportError):
            raise
        _log.warning("ozon sticker sheet batch failed (%s): %s", len(clean), exc)
    mid = max(1, len(clean) // 2)
    left = _fetch_images_for_batch(
        labels, client, clean[:mid], timeout_s=timeout_s
    )
    right = _fetch_images_for_batch(
        labels, client, clean[mid:], timeout_s=timeout_s
    )
    out = dict(left)
    out.update(right)
    return out


def fetch_label_images(
    labels: OzonLabelService,
    client: OzonFbsClient,
    posting_numbers: Sequence[str],
    *,
    progress: ProgressCb = None,
    stop_requested: StopCb = None,
    timeout_s: float = 180.0,
) -> Dict[str, List[str]]:
    """Return ``posting_number → [png_b64, …]`` via v3 create + v2 get."""
    nums = [str(p).strip() for p in posting_numbers if str(p).strip()]
    result = {pn: [] for pn in nums}  # type: Dict[str, List[str]]
    if not nums:
        return result
    batches = [
        nums[i : i + _LABEL_BATCH] for i in range(0, len(nums), _LABEL_BATCH)
    ]
    done = 0
    workers = min(_FETCH_WORKERS, max(1, len(batches)))

    def _on_batch(batch: List[str], partial: Dict[str, List[str]]) -> None:
        nonlocal done
        result.update(partial)
        done += len(batch)
        loaded = sum(1 for pn in nums if result.get(pn))
        emit_label_progress(
            progress,
            "Этикетки {}/{}".format(loaded, len(nums)),
            done=min(done, len(nums)),
            total=len(nums),
        )

    if workers <= 1 or len(batches) == 1:
        for batch in batches:
            if stop_requested and stop_requested():
                raise RuntimeError("Формирование стикеров отменено")
            emit_label_progress(
                progress,
                "Стикеры v3 · партия…",
                done=done,
                total=len(nums),
            )
            _on_batch(
                batch,
                _fetch_images_for_batch(
                    labels, client, batch, timeout_s=timeout_s
                ),
            )
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    _fetch_images_for_batch,
                    labels,
                    client,
                    batch,
                    timeout_s=timeout_s,
                ): batch
                for batch in batches
            }
            for fut in as_completed(futs):
                if stop_requested and stop_requested():
                    raise RuntimeError("Формирование стикеров отменено")
                batch = futs[fut]
                _on_batch(batch, fut.result())
    return result


def group_orders_by_article(orders: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Archive ``_group_postings_by_article``: group by offer_id, sort by name."""
    groups = {}  # type: Dict[str, Dict[str, Any]]
    order_list = []  # type: List[str]
    for o in orders:
        if str(o.get("status") or "").lower() in ("cancelled", "not_accepted"):
            continue
        if str(o.get("tab") or "").lower() == "cancelled":
            continue
        pn = str(o.get("posting_number") or "").strip()
        if not pn:
            continue
        article = str(o.get("offer_id") or o.get("article") or "").strip() or "—"
        if article not in groups:
            order_list.append(article)
            groups[article] = {
                "article": article,
                "product_name": str(
                    o.get("product_name_display")
                    or o.get("product_name")
                    or article
                ).strip()
                or article,
                "nm_id": str(o.get("sku") or "—").strip() or "—",
                "barcodes": list(o.get("barcodes") or [])
                if isinstance(o.get("barcodes"), list)
                else [],
                "orders": [],
                "qty": 0,
            }
        g = groups[article]
        g["orders"].append(dict(o))
        g["qty"] = len(g["orders"])
        pname = str(
            o.get("product_name_display") or o.get("product_name") or ""
        ).strip()
        if pname and (
            not str(g.get("product_name") or "").strip()
            or str(g.get("product_name") or "").strip() == article
        ):
            g["product_name"] = pname
        if not g.get("barcodes") and isinstance(o.get("barcodes"), list):
            g["barcodes"] = list(o.get("barcodes") or [])
    out = [groups[k] for k in order_list]
    for g in out:
        g["orders"].sort(
            key=lambda row: str(row.get("posting_number") or "")
        )
    out.sort(
        key=lambda g: (
            str(g.get("product_name") or "").casefold(),
            str(g.get("article") or "").casefold(),
            str(g.get("nm_id") or ""),
        )
    )
    return out


def render_stickers_print_html(
    detail: Mapping[str, Any],
    label_images: Mapping[str, Sequence[str]],
    *,
    source_name: str = "",
    include_cover_and_separators: bool = True,
    order_ids_filter: Optional[Sequence[str]] = None,
) -> str:
    """One multi-page 58×40 HTML: cover + article separators + stickers."""
    orders = list(detail.get("orders") or [])
    if order_ids_filter is not None:
        allowed = {str(x).strip() for x in order_ids_filter if str(x).strip()}
        orders = [
            o
            for o in orders
            if str(o.get("posting_number") or "").strip() in allowed
        ]
    groups = group_orders_by_article(orders)
    supply_name = str(
        detail.get("name") or detail.get("supply_id") or "Поставка"
    ).strip()
    sticker_count = 0
    for g in groups:
        for o in g.get("orders") or []:
            pn = str(o.get("posting_number") or "").strip()
            imgs = label_images.get(pn) or []
            sticker_count += len(imgs) if imgs else 1

    pages = []  # type: List[str]
    page_no = 0
    if include_cover_and_separators and groups:
        page_no += 1
        src_line = (
            '<div class="cover-source">{}</div>'.format(_esc(source_name))
            if source_name
            else ""
        )
        pages.append(
            """
            <section class="label cover">
              <div class="cover-label">Поставка</div>
              <div class="cover-name">{name}</div>
              {src}
              <div class="cover-qty">{qty} стик.</div>
            </section>
            """.format(
                name=_esc(supply_name), src=src_line, qty=sticker_count
            )
        )
    for g in groups:
        qty = int(g.get("qty") or 0)
        article = str(g.get("article") or "")
        pname = str(g.get("product_name") or "").strip() or "—"
        barcodes = g.get("barcodes") or []
        barcode = str(barcodes[0] if barcodes else "")
        nm = g.get("nm_id") or "—"
        if include_cover_and_separators:
            page_no += 1
            pages.append(
                """
                <section class="label separator">
                  <div class="qty">{qty} шт.</div>
                  <div class="title">{title}</div>
                  <div class="line">SKU Ozon: {nm}</div>
                  <div class="line">Баркод: {bc}</div>
                  <div class="line">Артикул: {art}</div>
                  <div class="hint">
                    <span>Артикул для подбора · Не нужно клеить</span>
                    <span class="page">{page}</span>
                  </div>
                </section>
                """.format(
                    qty=qty,
                    title=_esc(pname),
                    nm=_esc(nm),
                    bc=_esc(barcode or "—"),
                    art=_esc(article),
                    page=page_no,
                )
            )
        for o in g.get("orders") or []:
            pn = str(o.get("posting_number") or "").strip()
            imgs = list(label_images.get(pn) or [])
            if not imgs:
                page_no += 1
                pages.append(
                    """
                    <section class="label missing">
                      <div>Нет этикетки</div>
                      <div>{}</div>
                    </section>
                    """.format(
                        _esc(pn)
                    )
                )
                continue
            for b64 in imgs:
                page_no += 1
                pages.append(
                    """
                    <section class="label sticker">
                      <img src="data:image/png;base64,{}" alt="sticker {}" />
                    </section>
                    """.format(
                        b64, _esc(pn)
                    )
                )
    return """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"/>
<title>Стикеры {title}</title>
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
  .label.cover {{
    padding: 3mm; background: #fff; border: 0.3mm solid #0f172a;
    display: flex; flex-direction: column; justify-content: center;
    align-items: center; text-align: center; gap: 1.5mm;
  }}
  .label.cover .cover-label {{
    font-size: 9px; font-weight: 700; color: #64748b; text-transform: uppercase;
  }}
  .label.cover .cover-name {{
    font-size: 14px; font-weight: 800; line-height: 1.2;
    max-height: 14mm; overflow: hidden; word-break: break-word;
  }}
  .label.cover .cover-source {{ font-size: 10px; font-weight: 700; color: #334155; }}
  .label.cover .cover-qty {{ margin-top: 1mm; font-size: 16px; font-weight: 800; }}
  .label.separator {{
    padding: 2.5mm 3mm; background: #fff; border: 0.3mm dashed #94a3b8;
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
    font-size: 10px; color: #b91c1c; gap: 4px;
  }}
  @media print {{ .no-print {{ display: none !important; }} }}
</style></head>
<body>
  <div class="no-print"><button onclick="window.print()">Печать</button></div>
  {pages}
  <script>
    window.addEventListener('load', function () {{
      setTimeout(function () {{ window.print(); }}, 250);
    }});
  </script>
</body></html>""".format(
        title=_esc(supply_name), pages="\n".join(pages)
    )


def _fallback_raw_pdf_path(
    labels: OzonLabelService,
    client: OzonFbsClient,
    source_id: int,
    posting_numbers: Sequence[str],
    *,
    progress: ProgressCb = None,
    stop_requested: StopCb = None,
) -> str:
    """Open Ozon PDF(s) without pymupdf (no cover/separators)."""
    emit_label_progress(
        progress,
        "pymupdf недоступен · открываю PDF этикеток Ozon…",
        done=0,
        total=max(1, len(posting_numbers)),
    )
    paths = labels.fetch_label_paths(
        client,
        int(source_id),
        list(posting_numbers),
        use_cache=True,
        progress=progress,
        stop_requested=stop_requested,
    )
    if not paths:
        raise RuntimeError("Ozon не вернул файл этикеток")
    return paths[0]


def build_stickers_print_file(
    labels: OzonLabelService,
    client: OzonFbsClient,
    detail: Mapping[str, Any],
    posting_numbers: Sequence[str],
    *,
    source_id: Optional[int] = None,
    source_name: str = "",
    include_cover_and_separators: bool = True,
    progress: ProgressCb = None,
    stop_requested: StopCb = None,
) -> str:
    """Fetch labels, render one HTML sheet, return local file path.

    If pymupdf cannot be imported/installed and ``source_id`` is set, falls
    back to the raw Ozon PDF (without cover/article separators).
    """
    nums = [str(p).strip() for p in posting_numbers if str(p).strip()]
    if not nums:
        raise ValueError("Нет отправлений для этикеток")
    try:
        ensure_pymupdf(progress=progress)
    except RuntimeError as exc:
        if source_id is None:
            raise
        _log.warning("ozon stickers: pymupdf missing, PDF fallback: %s", exc)
        return _fallback_raw_pdf_path(
            labels,
            client,
            int(source_id),
            nums,
            progress=progress,
            stop_requested=stop_requested,
        )
    emit_label_progress(
        progress,
        "Загрузка этикеток Ozon…",
        done=0,
        total=len(nums),
    )
    images = fetch_label_images(
        labels,
        client,
        nums,
        progress=progress,
        stop_requested=stop_requested,
    )
    if stop_requested and stop_requested():
        raise RuntimeError("Формирование стикеров отменено")
    emit_label_progress(
        progress,
        "Сборка листа печати…",
        done=len(nums),
        total=len(nums),
    )
    html_doc = render_stickers_print_html(
        detail,
        images,
        source_name=source_name,
        include_cover_and_separators=include_cover_and_separators,
        order_ids_filter=nums,
    )
    fd, path = tempfile.mkstemp(suffix=".html", prefix="ozon-stickers-")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_doc)
    return path
