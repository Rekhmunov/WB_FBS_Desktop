"""Smoke tests for WB FBS ТСД page assets (no DB / web app boot required)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web_static"
TEMPLATES = ROOT / "web_templates"
WEB_PY = ROOT / "review_processor" / "web.py"


def test_tsd_static_assets_exist() -> None:
    assert (STATIC / "wb_fbs_tsd.css").is_file()
    assert (STATIC / "wb_fbs_tsd.js").is_file()
    assert (TEMPLATES / "wb_fbs_tsd.html").is_file()


def test_tsd_template_boot_placeholders() -> None:
    html = (TEMPLATES / "wb_fbs_tsd.html").read_text(encoding="utf-8")
    assert "{{CAN_VIEW_WB_FBS_TSD}}" in html
    assert "{{IS_TENANT_OWNER}}" in html
    assert "{{SAFE_EMAIL}}" in html
    assert "/static/wb_fbs_tsd.js" in html
    assert "/static/wb_fbs_tsd.css" in html


def test_app_html_has_tsd_button_and_permission() -> None:
    app_html = (TEMPLATES / "app.html").read_text(encoding="utf-8")
    assert 'id="wbFbsTsdBtn"' in app_html
    assert "can_view_wb_fbs_tsd: {{CAN_VIEW_WB_FBS_TSD}}" in app_html
    assert "<th>ТСД</th>" in app_html
    # Button sits next to Вывод КИЗ
    kiz = app_html.find("wbFbsKizCirculationBtn")
    tsd = app_html.find("wbFbsTsdBtn")
    assert kiz > 0 and tsd > kiz


def test_app_js_collects_wb_fbs_tsd_permission() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'data-col="wb_fbs_tsd"' in script
    assert "can_view_wb_fbs_tsd" in script
    assert "wb_fbs_tsd: false" in script or "wb_fbs_tsd: false," in script
    assert "s.wb_fbs_tsd" in script


def test_web_py_has_tsd_routes_and_builder() -> None:
    src = WEB_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "build_wb_fbs_tsd_html" in names
    assert '/wb-fbs/tsd' in src
    assert "/api/wb-fbs/tsd/sources" in src
    assert "/api/wb-fbs/tsd/supplies/{supply_id}/kiz" in src
    assert "/api/wb-fbs/tsd/supplies/{supply_id}/pick-verify" in src
    assert "/api/wb-fbs/tsd/supplies/{supply_id}/summary" in src
    assert "def _can_view_wb_fbs_tsd" in src
    assert re.search(r'CAN_VIEW_WB_FBS_TSD["\']:\s*"true" if can_view_wb_fbs_tsd', src)


def test_tsd_js_uses_dedicated_api_prefix() -> None:
    js = (STATIC / "wb_fbs_tsd.js").read_text(encoding="utf-8")
    assert "/api/wb-fbs/tsd/" in js
    assert "local_only: true" in js
    assert "sticker_barcode" in js
    assert "sticker_part_a" in js
    assert "expected_saved_at" in js
    assert "expected_verified_at" in js
    assert "forceSaveByOrder" in js
    assert "RU_LAYOUT_TO_EN" in js
    assert "fixRuKeyboardLayout" in js
    assert "syncSourceSelectVisibility" in js
    assert 'state.route.view === "list"' in js
    assert "Сохранить" in js
    assert "saveKizPushAll" in js
    assert "savePickLocalAll" in js
    assert "noteSessionScanned" in js
    assert "renderScannedListHtml" in js
    assert "clearKizCodes" in js
    assert 'data-action="clear-kiz-all"' in js
    assert 'data-action="clear-kiz-code"' not in js
    assert "orderBarcodesLabel" in js
    assert "tsd-scanned-barcodes" in js
    assert "tsd-product-barcodes" in js
    assert "formatBoldLastDigits" in js
    assert "tsd-sticker-tail" in js
    assert "КИЗ:" in js
    assert ">ШК:</span>" in js or "ШК:</span>" in js
    assert "tsd-scanned-kiz-line" in js or "tsd-scanned-kv" in js
    assert "tsd-scanned-top" in js
    assert "tsd-scanned-details" in js
    assert "Скан пишет КИЗ локально" not in js
    assert "Скан пишет ШК локально" not in js
    assert "Для 2-го КИЗ снова сканируйте стикер" in js
    assert "Этот КИЗ уже в этом заказе" in js
    assert "simple: true" in js
    assert 'title: "Товары с маркировкой"' in js
    assert 'title: "Товары без маркировки"' in js
    assert "Готовим сканирование…" not in js
    assert "Готово к сканированию" not in js
    assert "opts._retry" in js or "_retry: true" in js
    assert "pendingKizClear" in js
    assert "hasPendingKizPush" in js
    assert "rowNeedsKizWbClear" in js
    assert "removeSessionScanned" in js
    assert "убран из списка" in js
    assert "kizHubToneSupplyId" in js
    # First × on a filled KIZ must dismiss the row (not leave «—» via noteSessionScanned).
    clear_body = js.split("async function clearKizCodes", 1)[1].split("function syncSourceSelectVisibility", 1)[0]
    assert "removeSessionScanned(oid)" in clear_body
    assert "noteSessionScanned(oid)" not in clear_body
    assert "clear: true" in js
    assert "refreshHubKizStatus" in js
    assert "/kiz/status" in js
    assert "tsdKizRefreshBtn" in js
    assert "setKizHubTone" in js
    assert "tsdFilterBtn" in js
    assert "applyOrderFilters" in js
    assert "Заполненные" in (TEMPLATES / "wb_fbs_tsd.html").read_text(encoding="utf-8")
    html = (TEMPLATES / "wb_fbs_tsd.html").read_text(encoding="utf-8")
    assert 'id="tsdSearchBtn"' in html
    assert 'id="tsdFilterBtn"' in html
    assert 'id="tsdFilterErrors"' in html
    assert 'id="tsdOrderSearch"' in html
    assert 'id="tsdScrollTop"' in html
    assert "openOrderSearch" in js
    assert "openHeaderSearch" in js
    assert "applyListSearchFromHeader" in js
    assert "renderBrowseSheetHtml" in js
    assert "tsdBrowseSheet" in js
    assert "BROWSE_PAGE_SIZE" in js
    assert "Показать ещё" in js
    assert 'id="tsdSearch"' not in js
    assert "Поиск поставки…" in js
    assert 'view === "list"' in js or "view === \"list\"" in js
    assert "filterOrdersBySearch" in js
    assert "scrollToScanInput" in js
    assert "syncScrollTopFab" in js
    assert "pick-search-order" in js
    assert "orderSearch" in js
    assert "applyOrderSearchEnter" in js
    assert 'id="tsdScanClear"' in js
    assert "tsd-scan-clear" in js
    assert "normalizeKizMark" in js
    assert "normalizeKizCodesList" in js
    assert "\\u2194" in js
    assert "\\u001D" in js


def test_web_py_has_tsd_kiz_status_route() -> None:
    src = WEB_PY.read_text(encoding="utf-8")
    assert "/api/wb-fbs/tsd/supplies/{supply_id}/kiz/status" in src
    assert "def wb_fbs_tsd_kiz_status(" in src
    assert "check_supply_kiz_status" in src


def test_web_py_tsd_summary_matches_scan_without_full_payloads() -> None:
    src = WEB_PY.read_text(encoding="utf-8")
    # Isolate the TSD summary handler body between its decorator and next TSD kiz route.
    start = src.find("def wb_fbs_tsd_supply_summary(")
    end = src.find("def wb_fbs_tsd_kiz_list(")
    assert start > 0 and end > start
    body = src[start:end]
    assert "build_tsd_hub_progress" in body
    assert "wb_detail.build_kiz_marking_payload" not in body
    assert "wb_detail.build_pick_verify_payload" not in body
    # Must not regress to raw_json-only local counter.
    assert "build_tsd_hub_progress_from_local" not in body


def test_web_py_tsd_kiz_save_supports_local_and_wb() -> None:
    src = WEB_PY.read_text(encoding="utf-8")
    start = src.find("async def wb_fbs_tsd_kiz_save(")
    end = src.find("def wb_fbs_tsd_pick_verify_list(")
    assert start > 0 and end > start
    body = src[start:end]
    # Autosave keeps local_only from client; explicit Save can push to WB.
    assert 'row["local_only"] = bool(row.get("local_only"))' in body
    assert "only_local" in body
    assert "invalidate_supply_detail_cache" in body
    assert "nav-wb-fbs-tsd" in src
    assert "build_tsd_hub_progress" in src