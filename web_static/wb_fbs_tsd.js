/* WB FBS TSD — standalone warehouse page (does not depend on app.js) */
(function () {
  "use strict";

  const boot = window.TSD_BOOT || {};
  const state = {
    sourceId: null,
    sources: [],
    supplies: [],
    supply: null,
    route: { view: "list", supplyId: "", mode: "" },
    kizRows: [],
    pickRows: [],
    pendingOrderId: null,
    step: "sticker", // sticker | mark | sku
    banner: null,
    search: "",
    orderSearch: "",
    searchOpen: false,
    filterOpen: false,
    browseOpen: false,
    browseLimit: 40,
    filters: {
      filled: false,
      empty: false,
      errors: false,
      cancelled: false,
    },
    rowErrors: {},
    pendingKizClear: {},
    kizHubTone: "",
    kizHubToneSupplyId: "",
    kizStatusRefreshing: false,
    loadSeq: 0,
    forceSaveByOrder: {},
    sessionScannedIds: [],
    saving: false,
    clearing: false,
    loadUi: {
      token: 0,
      hintTimer: null,
      elapsedTimer: null,
      rotateTimer: null,
      startedAt: 0,
    },
  };

  const LS_SOURCE = "wb_fbs_tsd_source_id";

  // Wedge scanners type as keyboard; RU layout turns Latin sticker barcodes into Cyrillic.
  const RU_LAYOUT_TO_EN = {
    й: "q", ц: "w", у: "e", к: "r", е: "t", н: "y", г: "u", ш: "i",
    щ: "o", з: "p", х: "[", ъ: "]",
    ф: "a", ы: "s", в: "d", а: "f", п: "g", р: "h", о: "j", л: "k",
    д: "l", ж: ";", э: "'",
    я: "z", ч: "x", с: "c", м: "v", и: "b", т: "n", ь: "m", б: ",",
    ю: ".", ё: "`",
    Й: "Q", Ц: "W", У: "E", К: "R", Е: "T", Н: "Y", Г: "U", Ш: "I",
    Щ: "O", З: "P", Х: "{", Ъ: "}",
    Ф: "A", Ы: "S", В: "D", А: "F", П: "G", Р: "H", О: "J", Л: "K",
    Д: "L", Ж: ":", Э: '"',
    Я: "Z", Ч: "X", С: "C", М: "V", И: "B", Т: "N", Ь: "M", Б: "<",
    Ю: ">", Ё: "~",
  };

  function esc(v) {
    return String(v || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function getCsrfToken() {
    const key = "csrf_token=";
    for (const part of String(document.cookie || "").split(";")) {
      const value = part.trim();
      if (value.startsWith(key)) return decodeURIComponent(value.slice(key.length));
    }
    return "";
  }

  function jsonHeaders() {
    const headers = { "Content-Type": "application/json", Accept: "application/json" };
    const csrf = getCsrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
    return headers;
  }

  async function api(url, opts) {
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data && data.detail;
      throw new Error(typeof detail === "string" ? detail : `Ошибка ${res.status}`);
    }
    return data;
  }

  function boxesLabel(n) {
    const c = Number(n || 0);
    if (c === 1) return "1 грузоместо";
    if (c > 1 && c < 5) return `${c} грузоместа`;
    return `${c} грузомест`;
  }

  function ordersBoxesText(s) {
    const orders = Number(s.order_count || 0);
    const boxes = Number(s.boxes_count || 0);
    return `${orders} заказ. · ${boxesLabel(boxes)}`;
  }

  function beep(ok) {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx();
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "sine";
      o.frequency.value = ok ? 880 : 220;
      g.gain.value = 0.04;
      o.connect(g);
      g.connect(ctx.destination);
      o.start();
      setTimeout(() => {
        o.stop();
        ctx.close();
      }, ok ? 90 : 220);
    } catch (_e) {
      /* ignore */
    }
  }

  function toast(msg) {
    const el = document.getElementById("tsdToast");
    if (!el) return;
    el.textContent = String(msg || "");
    el.hidden = !msg;
    if (!msg) return;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      el.hidden = true;
    }, 2400);
  }

  function stopLoadingUi() {
    const ui = state.loadUi;
    if (ui.hintTimer) clearTimeout(ui.hintTimer);
    if (ui.elapsedTimer) clearInterval(ui.elapsedTimer);
    if (ui.rotateTimer) clearInterval(ui.rotateTimer);
    ui.hintTimer = null;
    ui.elapsedTimer = null;
    ui.rotateTimer = null;
    ui.token += 1;
  }

  function supplyNameHint(supplyId) {
    const sid = String(supplyId || "");
    if (state.supply && String(state.supply.supply_id || "") === sid) {
      const n = String(state.supply.name || "").trim();
      if (n) return n;
    }
    const fromList = (state.supplies || []).find((s) => String(s.supply_id || "") === sid);
    if (fromList) {
      const n = String(fromList.name || "").trim();
      if (n) return n;
    }
    return sid || "поставку";
  }

  function setLoadingStatus(text, stageIdx) {
    const statusEl = document.getElementById("tsdLoadStatus");
    if (statusEl) statusEl.textContent = String(text || "");
    if (stageIdx === undefined || stageIdx === null) return;
    const stages = document.querySelectorAll("#tsdLoadStages .tsd-load-stage");
    stages.forEach((el, i) => {
      el.classList.toggle("is-done", i < stageIdx);
      el.classList.toggle("is-active", i === stageIdx);
      el.classList.toggle("is-todo", i > stageIdx);
    });
  }

  function showLoadingScreen(opts) {
    stopLoadingUi();
    const token = state.loadUi.token;
    const title = String((opts && opts.title) || "Загрузка");
    const simple = !!(opts && opts.simple);
    const status = simple ? "" : String((opts && opts.status) || "Подождите…");
    const stages = simple ? [] : Array.isArray(opts && opts.stages) ? opts.stages : [];
    const main = document.getElementById("tsdMain");
    if (!main) return token;
    const stagesHtml = stages.length
      ? `<ol class="tsd-load-stages" id="tsdLoadStages" aria-hidden="true">
          ${stages
            .map(
              (label, i) =>
                `<li class="tsd-load-stage ${i === 0 ? "is-active" : "is-todo"}">${esc(label)}</li>`
            )
            .join("")}
        </ol>`
      : "";
    const detailsHtml = simple
      ? ""
      : `<div class="tsd-load-status" id="tsdLoadStatus">${esc(status)}</div>
        ${stagesHtml}
        <div class="tsd-load-elapsed" id="tsdLoadElapsed" hidden></div>
        <div class="tsd-load-hint" id="tsdLoadHint" hidden>Ещё загружаем, не уходите</div>`;
    main.innerHTML = `
      <div class="tsd-loading-screen${simple ? " is-simple" : ""}" role="status" aria-live="polite">
        <div class="tsd-load-spinner" aria-hidden="true"></div>
        <div class="tsd-load-title">${esc(title)}</div>
        ${detailsHtml}
      </div>`;
    if (simple) return token;
    state.loadUi.startedAt = Date.now();
    state.loadUi.hintTimer = setTimeout(() => {
      if (token !== state.loadUi.token) return;
      const hint = document.getElementById("tsdLoadHint");
      if (hint) hint.hidden = false;
    }, 9000);
    state.loadUi.elapsedTimer = setInterval(() => {
      if (token !== state.loadUi.token) return;
      const el = document.getElementById("tsdLoadElapsed");
      if (!el) return;
      const sec = Math.floor((Date.now() - state.loadUi.startedAt) / 1000);
      if (sec < 3) return;
      el.hidden = false;
      el.textContent = `Уже ${sec} сек`;
    }, 1000);
    return token;
  }

  function startLoadingRotate(steps, intervalMs) {
    const list = Array.isArray(steps) ? steps.filter(Boolean) : [];
    if (!list.length) return () => {};
    const token = state.loadUi.token;
    let idx = 0;
    const first = list[0];
    setLoadingStatus(first.status || first, first.stage);
    if (list.length === 1) return () => {};
    const ms = Math.max(1200, Number(intervalMs) || 2200);
    state.loadUi.rotateTimer = setInterval(() => {
      if (token !== state.loadUi.token) {
        clearInterval(state.loadUi.rotateTimer);
        state.loadUi.rotateTimer = null;
        return;
      }
      idx = (idx + 1) % list.length;
      const step = list[idx];
      setLoadingStatus(step.status || step, step.stage);
    }, ms);
    return () => {
      if (state.loadUi.rotateTimer) {
        clearInterval(state.loadUi.rotateTimer);
        state.loadUi.rotateTimer = null;
      }
    };
  }

  function setBanner(text, kind) {
    state.banner = text ? { text: String(text), kind: kind || "info" } : null;
  }

  function parseHash() {
    const raw = String(location.hash || "").replace(/^#\/?/, "");
    const parts = raw.split("/").filter(Boolean);
    if (!parts.length) return { view: "list", supplyId: "", mode: "" };
    if (parts[0] === "s" && parts[1]) {
      const mode = parts[2] === "kiz" || parts[2] === "pick" ? parts[2] : "";
      return { view: mode ? "scan" : "hub", supplyId: parts[1], mode };
    }
    return { view: "list", supplyId: "", mode: "" };
  }

  function navigate(hash) {
    const next = String(hash || "#/");
    if (location.hash === next) {
      onRoute();
      return;
    }
    location.hash = next;
  }

  function normalizeScan(raw) {
    return String(raw || "").replace(/\s+/g, "").trim();
  }

  /** Parity with desktop `_wbFbsKizNormalizeMark` (WB push / Save). */
  function normalizeKizMark(value) {
    // Scanners often emit ↔ instead of GS (\\u001D). Do not use \\s strip —
    // it must not destroy GS separators in Honest Sign / sgtin payloads.
    return fixRuKeyboardLayout(
      String(value || "")
        .replace(/\u2194/g, "\u001D")
        .replace(/\r?\n/g, "")
    ).trim();
  }

  /** Parity with desktop `_wbFbsKizNormalizeCodesList`. */
  function normalizeKizCodesList(codes) {
    const seen = new Set();
    const out = [];
    for (const c of Array.isArray(codes) ? codes : []) {
      const n = normalizeKizMark(c);
      if (!n || seen.has(n)) continue;
      seen.add(n);
      out.push(n);
    }
    return out;
  }

  function hasCyrillic(s) {
    return /[А-Яа-яЁё]/.test(String(s || ""));
  }

  function fixRuKeyboardLayout(value) {
    const text = String(value || "");
    if (!/[а-яёА-ЯЁ]/.test(text)) return text;
    let out = "";
    for (const ch of text) {
      out += Object.prototype.hasOwnProperty.call(RU_LAYOUT_TO_EN, ch)
        ? RU_LAYOUT_TO_EN[ch]
        : ch;
    }
    return out;
  }

  function scanKey(s) {
    return normalizeScan(s).toLocaleLowerCase("en-US");
  }

  function digitsOnly(s) {
    return String(s || "").replace(/\D+/g, "");
  }

  function findBySticker(rows, raw) {
    // Parity with desktop: primary sticker_barcode (QR/1D), then partA+partB / number.
    const scan = normalizeScan(raw);
    if (!scan) return { row: null, ambiguous: false };
    const rawKey = scanKey(scan);
    const byBarcode = [];
    for (const row of rows || []) {
      const bc = normalizeScan(row.sticker_barcode);
      if (bc && scanKey(bc) === rawKey) byBarcode.push(row);
    }
    if (byBarcode.length === 1) return { row: byBarcode[0], ambiguous: false };
    if (byBarcode.length > 1) {
      return { row: null, ambiguous: true, matches: byBarcode };
    }

    const digits = digitsOnly(scan);
    const matches = [];
    for (const row of rows || []) {
      const full = normalizeScan(row.sticker_number || row.sticker || "");
      const partA = normalizeScan(row.sticker_part_a);
      const partB = normalizeScan(row.sticker_part_b);
      if (
        (full && (rawKey === scanKey(full) || (digits && digits === digitsOnly(full)))) ||
        (partA && partB && digits && digits === digitsOnly(`${partA}${partB}`)) ||
        (partB && (rawKey === scanKey(partB) || (digits && digits === digitsOnly(partB))))
      ) {
        matches.push(row);
      }
    }
    if (matches.length === 1) return { row: matches[0], ambiguous: false };
    if (matches.length > 1) {
      const exact = matches.find((r) => {
        const full = normalizeScan(r.sticker_number);
        return scanKey(full) === rawKey || digitsOnly(full) === digits;
      });
      if (exact) return { row: exact, ambiguous: false };
      return { row: null, ambiguous: true, matches };
    }
    return { row: null, ambiguous: false };
  }

  function rowKizFilled(r) {
    const codes = Array.isArray(r.kiz_codes) ? r.kiz_codes : [];
    return codes.some((c) => String(c || "").trim());
  }

  function rowPickFilled(r) {
    return !!(r.pick_verified && String(r.pick_barcode || "").trim());
  }

  function gtinFromMark(mark) {
    // Parity with desktop `_wbFbsKizExtractGtin14`.
    const raw = normalizeKizMark(mark);
    if (!raw) return "";
    const m = raw.match(/^01(\d{14})/);
    if (m) return m[1];
    const m2 = raw.match(/(?:^|[\u001D])01(\d{14})/);
    return m2 ? m2[1] : "";
  }

  function orderSkuSet(row) {
    const set = new Set();
    const barcodes = Array.isArray(row.barcodes) ? row.barcodes : [];
    const skus = Array.isArray(row.skus) ? row.skus : [];
    for (const x of barcodes.concat(skus)) {
      const raw = String(x || "").trim();
      if (raw) set.add(raw);
      const d = digitsOnly(raw);
      if (d) set.add(d);
    }
    return set;
  }

  function markMatchesOrder(mark, row) {
    const gtin = gtinFromMark(mark);
    if (!gtin) {
      return {
        ok: false,
        error: "Не удалось выделить GTIN из кода маркировки (ожидается префикс 01 и 14 цифр).",
      };
    }
    // Product catalog flag: skip GTIN↔ШК match only (still require a parseable GTIN).
    if (row && row.skip_kiz_gtin_check) {
      return { ok: true };
    }
    const candidates = [gtin];
    if (gtin.startsWith("0")) candidates.push(gtin.slice(1));
    const orderSkus = orderSkuSet(row);
    if (!orderSkus.size) {
      return {
        ok: false,
        error: "У заказа нет штрихкодов товара — нельзя сверить GTIN маркировки.",
      };
    }
    if (!candidates.some((c) => orderSkus.has(c))) {
      const shown = gtin.startsWith("0") ? gtin.slice(1) : gtin;
      return { ok: false, error: `GTIN ${shown} не совпадает ни с одним ШК товара в заказе` };
    }
    return { ok: true };
  }

  function eanMatchesOrder(raw, row) {
    const dig = digitsOnly(raw);
    if (!(dig.length === 8 || dig.length === 12 || dig.length === 13 || dig.length === 14)) {
      return { ok: false, error: "Ожидается штрихкод EAN (8/13 цифр)" };
    }
    const orderSkus = orderSkuSet(row);
    if (!orderSkus.size) {
      return { ok: false, error: "У заказа нет штрихкодов товара — нельзя сверить ШК" };
    }
    const ok =
      orderSkus.has(dig) ||
      [...orderSkus].some((b) => digitsOnly(b) === dig || String(b).endsWith(dig) || dig.endsWith(digitsOnly(b)));
    if (!ok) return { ok: false, error: "ШК не подходит к товару в заказе" };
    return { ok: true };
  }

  function countProgress(rows, filledFn) {
    const total = (rows || []).length;
    const done = (rows || []).filter(filledFn).length;
    return { total, done, left: Math.max(0, total - done) };
  }

  async function loadSources() {
    const data = await api("/api/wb-fbs/tsd/sources");
    state.sources = Array.isArray(data) ? data : [];
    const sel = document.getElementById("tsdSourceSelect");
    if (!sel) return;
    const prev = localStorage.getItem(LS_SOURCE) || state.sourceId;
    sel.innerHTML = state.sources.length
      ? state.sources
          .map(
            (s) =>
              `<option value="${esc(s.id)}">${esc(s.name || "Источник " + s.id)}</option>`
          )
          .join("")
      : `<option value="">Нет кабинетов</option>`;
    if (prev && state.sources.some((s) => String(s.id) === String(prev))) {
      sel.value = String(prev);
      state.sourceId = Number(prev);
    } else if (state.sources.length) {
      state.sourceId = Number(state.sources[0].id);
      sel.value = String(state.sourceId);
    } else {
      state.sourceId = null;
    }
  }

  async function loadSupplies() {
    if (!state.sourceId) {
      state.supplies = [];
      return;
    }
    const params = new URLSearchParams({
      source_id: String(state.sourceId),
      page: "1",
      page_size: "100",
    });
    if (state.search) params.set("search", state.search);
    const data = await api(`/api/wb-fbs/tsd/supplies?${params}`);
    state.supplies = Array.isArray(data.items) ? data.items : [];
  }

  async function loadSummary(supplyId) {
    const params = new URLSearchParams({ source_id: String(state.sourceId) });
    const data = await api(
      `/api/wb-fbs/tsd/supplies/${encodeURIComponent(supplyId)}/summary?${params}`
    );
    state.supply = data;
    return data;
  }

  async function loadKiz(supplyId) {
    const params = new URLSearchParams({ source_id: String(state.sourceId) });
    const data = await api(
      `/api/wb-fbs/tsd/supplies/${encodeURIComponent(supplyId)}/kiz?${params}`
    );
    state.kizRows = Array.isArray(data.rows) ? data.rows.map((r) => ({ ...r })) : [];
    state.pendingKizClear = {};
    state.rowErrors = {};
  }

  async function loadPick(supplyId) {
    const params = new URLSearchParams({ source_id: String(state.sourceId) });
    const data = await api(
      `/api/wb-fbs/tsd/supplies/${encodeURIComponent(supplyId)}/pick-verify?${params}`
    );
    state.pickRows = Array.isArray(data.rows) ? data.rows.map((r) => ({ ...r })) : [];
  }

  async function saveKizLocal(row, opts) {
    const params = new URLSearchParams({ source_id: String(state.sourceId) });
    const oid = Number(row.order_id);
    const codes = normalizeKizCodesList(row.kiz_codes);
    row.kiz_codes = codes.length ? codes.slice() : [""];
    const retrying = !!(opts && opts._retry);
    const data = await api(
      `/api/wb-fbs/tsd/supplies/${encodeURIComponent(state.route.supplyId)}/kiz?${params}`,
      {
        method: "PUT",
        headers: jsonHeaders(),
        body: JSON.stringify({
          items: [
            {
              order_id: oid,
              kiz_codes: codes,
              clear: !codes.length,
              local_only: true,
              expected_saved_at: String(row.kiz_saved_at || ""),
              force: !!state.forceSaveByOrder[oid] || retrying,
            },
          ],
        }),
        keepalive: true,
      }
    );
    const result = (data.results || []).find((r) => Number(r.order_id) === oid) || null;
    if (!result) throw new Error("Сервер не вернул результат сохранения КИЗ");
    if (result.conflict) {
      row.kiz_saved_at = String(result.kiz_saved_at || row.kiz_saved_at || "");
      state.forceSaveByOrder[oid] = true;
      // Keep scanned codes and retry once — same operator / timezone false conflicts.
      row.kiz_codes = codes.length ? codes.slice() : [""];
      if (!retrying) return saveKizLocal(row, { _retry: true });
      throw new Error(
        result.error ||
          "Заказ уже сохранён другим оператором — проверьте КИЗ и повторите"
      );
    }
    if (!result.ok && !result.local_ok) {
      throw new Error(result.error || "Не удалось сохранить КИЗ локально");
    }
    if (result.kiz_saved_at) row.kiz_saved_at = String(result.kiz_saved_at);
    delete state.forceSaveByOrder[oid];
    return result;
  }

  async function savePickLocal(row, opts) {
    const params = new URLSearchParams({ source_id: String(state.sourceId) });
    const oid = Number(row.order_id);
    const retrying = !!(opts && opts._retry);
    const intendedVerified = !!row.pick_verified;
    const intendedBarcode = String(row.pick_barcode || "").trim();
    const data = await api(
      `/api/wb-fbs/tsd/supplies/${encodeURIComponent(state.route.supplyId)}/pick-verify?${params}`,
      {
        method: "PUT",
        headers: jsonHeaders(),
        body: JSON.stringify({
          items: [
            {
              order_id: oid,
              pick_verified: intendedVerified,
              pick_barcode: intendedBarcode,
              local_only: true,
              expected_verified_at: String(row.pick_verified_at || ""),
              force: !!state.forceSaveByOrder[`pick:${oid}`] || retrying,
            },
          ],
        }),
        keepalive: true,
      }
    );
    const result = (data.results || []).find((r) => Number(r.order_id) === oid) || null;
    if (!result) throw new Error("Сервер не вернул результат сохранения ШК");
    if (result.conflict) {
      row.pick_verified_at = String(result.pick_verified_at || row.pick_verified_at || "");
      state.forceSaveByOrder[`pick:${oid}`] = true;
      row.pick_verified = intendedVerified;
      row.pick_barcode = intendedBarcode;
      if (!retrying) return savePickLocal(row, { _retry: true });
      throw new Error(
        result.error ||
          "Заказ уже сохранён другим оператором — проверьте ШК и повторите"
      );
    }
    if (!result.ok) {
      throw new Error(result.error || "Не удалось сохранить проверку ШК");
    }
    if (result.pick_verified_at) row.pick_verified_at = String(result.pick_verified_at);
    delete state.forceSaveByOrder[`pick:${oid}`];
    return result;
  }

  /** Explicit «Сохранить»: local + push КИЗ to Wildberries (like desktop modal). */
  async function saveKizPushAll() {
    if (state.saving) return;
    const rows = state.kizRows || [];
    const items = [];
    for (const row of rows) {
      const oid = Number(row.order_id);
      if (!Number.isFinite(oid)) continue;
      const codes = normalizeKizCodesList(row.kiz_codes);
      if (!codes.length) {
        if (!rowNeedsKizWbClear(row)) continue;
        items.push({
          order_id: oid,
          kiz_codes: [],
          clear: true,
          expected_saved_at: String(row.kiz_saved_at || ""),
          force: !!state.forceSaveByOrder[oid],
        });
        continue;
      }
      row.kiz_codes = codes.slice();
      items.push({
        order_id: oid,
        kiz_codes: codes,
        clear: false,
        expected_saved_at: String(row.kiz_saved_at || ""),
        force: !!state.forceSaveByOrder[oid],
      });
    }
    if (!items.length) {
      setBanner("Нет КИЗ для отправки в WB", "warn");
      renderScan();
      return;
    }
    state.saving = true;
    setBanner(`Сохранение ${items.length} в WB…`, "info");
    renderScan();
    try {
      const params = new URLSearchParams({ source_id: String(state.sourceId) });
      const data = await api(
        `/api/wb-fbs/tsd/supplies/${encodeURIComponent(state.route.supplyId)}/kiz?${params}`,
        {
          method: "PUT",
          headers: jsonHeaders(),
          body: JSON.stringify({ items }),
        }
      );
      let okN = 0;
      let errN = 0;
      let conflictN = 0;
      for (const r of data.results || []) {
        const oid = Number(r.order_id);
        const row = rows.find((x) => Number(x.order_id) === oid);
        if (!row) continue;
        if (r.conflict) {
          conflictN += 1;
          row.kiz_saved_at = String(r.kiz_saved_at || row.kiz_saved_at || "");
          if (Array.isArray(r.kiz_codes)) row.kiz_codes = r.kiz_codes.slice();
          state.forceSaveByOrder[oid] = true;
          continue;
        }
        if (r.kiz_saved_at) row.kiz_saved_at = String(r.kiz_saved_at);
        if (r.kiz_wb_synced != null) row.kiz_wb_synced = !!r.kiz_wb_synced;
        if (r.ok || r.wb_ok) {
          okN += 1;
          delete state.forceSaveByOrder[oid];
          delete state.rowErrors[oid];
          const pushedCodes = normalizeKizCodesList(
            Array.isArray(r.kiz_codes) ? r.kiz_codes : row.kiz_codes
          );
          if (!pushedCodes.length) {
            delete state.pendingKizClear[oid];
            row.kiz_bound = false;
            row.kiz_local = false;
            row.kiz_wb_synced = true;
            row.kiz_status = "empty";
            row.kiz_codes = [""];
            state.sessionScannedIds = (state.sessionScannedIds || []).filter(
              (x) => Number(x) !== oid
            );
          } else {
            delete state.pendingKizClear[oid];
            row.kiz_bound = true;
            row.kiz_local = true;
            row.kiz_codes = pushedCodes.slice();
            if (row.kiz_status === "empty") row.kiz_status = "pending";
          }
        } else if (r.local_ok) {
          errN += 1;
          if (r.error) state.rowErrors[oid] = String(r.error);
        } else {
          errN += 1;
          if (r.error) state.rowErrors[oid] = String(r.error);
        }
      }
      if (conflictN) {
        setBanner(
          `Конфликт у ${conflictN} заказ(ов) — проверьте и сохраните ещё раз`,
          "err"
        );
      } else if (errN && okN) {
        setBanner(`Отправлено ${okN}, ошибок ${errN} — повторите «Сохранить»`, "warn");
      } else if (errN) {
        setBanner(`Не удалось отправить в WB (${errN})`, "err");
      } else {
        setBanner(`Сохранено в WB: ${okN}`, "ok");
      }
    } catch (e) {
      setBanner(e.message || String(e), "err");
    } finally {
      state.saving = false;
      renderScan();
    }
  }

  /** Explicit «Сохранить» for pick: local-only batch (like desktop modal). */
  async function savePickLocalAll() {
    if (state.saving) return;
    const rows = state.pickRows || [];
    const items = [];
    for (const row of rows) {
      const oid = Number(row.order_id);
      if (!Number.isFinite(oid)) continue;
      if (!rowPickFilled(row)) continue;
      items.push({
        order_id: oid,
        pick_verified: true,
        pick_barcode: String(row.pick_barcode || "").trim(),
        expected_verified_at: String(row.pick_verified_at || ""),
        force: !!state.forceSaveByOrder[`pick:${oid}`],
      });
    }
    if (!items.length) {
      setBanner("Нет подтверждённых ШК для сохранения", "warn");
      renderScan();
      return;
    }
    state.saving = true;
    setBanner(`Сохранение ${items.length}…`, "info");
    renderScan();
    try {
      const params = new URLSearchParams({ source_id: String(state.sourceId) });
      const data = await api(
        `/api/wb-fbs/tsd/supplies/${encodeURIComponent(state.route.supplyId)}/pick-verify?${params}`,
        {
          method: "PUT",
          headers: jsonHeaders(),
          body: JSON.stringify({ items }),
        }
      );
      let okN = 0;
      let errN = 0;
      let conflictN = 0;
      for (const r of data.results || []) {
        const oid = Number(r.order_id);
        const row = rows.find((x) => Number(x.order_id) === oid);
        if (!row) continue;
        if (r.conflict) {
          conflictN += 1;
          row.pick_verified_at = String(r.pick_verified_at || row.pick_verified_at || "");
          state.forceSaveByOrder[`pick:${oid}`] = true;
          continue;
        }
        if (r.ok) {
          okN += 1;
          if (r.pick_verified_at) row.pick_verified_at = String(r.pick_verified_at);
          delete state.forceSaveByOrder[`pick:${oid}`];
        } else {
          errN += 1;
        }
      }
      if (conflictN) {
        setBanner(`Конфликт у ${conflictN} заказ(ов)`, "err");
      } else if (errN) {
        setBanner(`Сохранено ${okN}, ошибок ${errN}`, "warn");
      } else {
        setBanner(`Сохранено локально: ${okN}`, "ok");
      }
    } catch (e) {
      setBanner(e.message || String(e), "err");
    } finally {
      state.saving = false;
      renderScan();
    }
  }

  function noteSessionScanned(orderId) {
    const oid = Number(orderId);
    if (!Number.isFinite(oid) || oid <= 0) return;
    state.sessionScannedIds = (state.sessionScannedIds || []).filter(
      (x) => Number(x) !== oid
    );
    state.sessionScannedIds.push(oid);
  }

  function rowNeedsKizWbClear(row) {
    const oid = Number(row && row.order_id);
    if (!Number.isFinite(oid)) return false;
    if (rowKizFilled(row)) return false;
    if (state.pendingKizClear[oid]) return true;
    // WB still has a mark, or local empty draft is not synced yet.
    if (row.kiz_bound) return true;
    if (row.kiz_local && row.kiz_wb_synced === false) return true;
    return false;
  }

  function hasPendingKizPush() {
    return (state.kizRows || []).some((row) => {
      const oid = Number(row.order_id);
      if (!Number.isFinite(oid)) return false;
      if (rowNeedsKizWbClear(row)) return true;
      return rowKizFilled(row);
    });
  }

  function removeSessionScanned(orderId) {
    const oid = Number(orderId);
    if (!Number.isFinite(oid)) return;
    state.sessionScannedIds = (state.sessionScannedIds || []).filter(
      (x) => Number(x) !== oid
    );
  }

  function orderedScannedRows(mode) {
    const rows = mode === "kiz" ? state.kizRows : state.pickRows;
    // KIZ: show filled codes, or empty only after this-session clear (pending + session).
    // Do NOT pull in stale empty local drafts (КИЗ «—») just because kiz_local/kiz_bound.
    const fn =
      mode === "kiz"
        ? (r) => {
            if (rowKizFilled(r)) return true;
            const oid = Number(r.order_id);
            return (
              !!state.pendingKizClear[oid] &&
              (state.sessionScannedIds || []).some((x) => Number(x) === oid)
            );
          }
        : rowPickFilled;
    const filled = (rows || []).filter(fn);
    const byId = new Map(filled.map((r) => [Number(r.order_id), r]));
    const out = [];
    const seen = new Set();
    for (let i = (state.sessionScannedIds || []).length - 1; i >= 0; i -= 1) {
      const id = Number(state.sessionScannedIds[i]);
      const row = byId.get(id);
      if (row && !seen.has(id)) {
        out.push(row);
        seen.add(id);
      }
    }
    for (const row of filled) {
      const id = Number(row.order_id);
      if (!seen.has(id)) {
        out.push(row);
        seen.add(id);
      }
    }
    return out;
  }

  function shortKizDisplay(code) {
    const c = String(code || "").trim();
    // Keep most of the mark visible on a full TSD row before ellipsis.
    if (c.length > 56) return `${c.slice(0, 40)}…${c.slice(-12)}`;
    return c;
  }

  function formatBoldLastDigits(text, n) {
    const s = String(text || "").trim();
    const count = Math.max(1, Number(n) || 4);
    if (!s || s === "—") return esc(s || "—");
    let seen = 0;
    let cut = -1;
    for (let i = s.length - 1; i >= 0; i -= 1) {
      if (/\d/.test(s[i])) {
        seen += 1;
        if (seen === count) {
          cut = i;
          break;
        }
      }
    }
    if (cut < 0) {
      if (s.length <= count) {
        return `<strong class="tsd-sticker-tail">${esc(s)}</strong>`;
      }
      return `${esc(s.slice(0, -count))}<strong class="tsd-sticker-tail">${esc(
        s.slice(-count)
      )}</strong>`;
    }
    return `${esc(s.slice(0, cut))}<strong class="tsd-sticker-tail">${esc(
      s.slice(cut)
    )}</strong>`;
  }

  function filledKizEntries(row) {
    return (Array.isArray(row.kiz_codes) ? row.kiz_codes : [])
      .map((c, idx) => ({ code: String(c || "").trim(), idx }))
      .filter((x) => x.code);
  }

  function orderBarcodesLabel(row) {
    const seen = new Set();
    const out = [];
    const lists = [row && row.barcodes, row && row.skus];
    for (const list of lists) {
      if (!Array.isArray(list)) continue;
      for (const raw of list) {
        const b = String(raw || "").trim();
        if (!b || seen.has(b)) continue;
        seen.add(b);
        out.push(b);
      }
    }
    return out.join(", ");
  }

  function renderScannedListHtml(mode) {
    const scanned = orderedScannedRows(mode);
    if (!scanned.length) {
      return `
        <section class="tsd-scanned" aria-label="Просканированные заказы">
          <h2 class="tsd-scanned-title">Просканировано</h2>
          <div class="tsd-scanned-empty">Пока пусто — сканируйте стикер и ${
            mode === "kiz" ? "КИЗ" : "ШК"
          }</div>
        </section>`;
    }
    const items = scanned
      .map((r) => {
        const photo = r.product_photo
          ? `<img src="${esc(r.product_photo)}" alt="" width="48" height="48" />`
          : `<span class="tsd-scanned-ph" aria-hidden="true"></span>`;
        const oid = esc(String(r.order_id));
        const stickerHtml = formatBoldLastDigits(r.sticker_number || "—", 4);
        const barcodes = orderBarcodesLabel(r);
        const barcodesHtml = barcodes
          ? `<div class="tsd-scanned-kv">
              <span class="tsd-scanned-label">ШК:</span>
              <span class="tsd-scanned-kv-val">${esc(barcodes)}</span>
            </div>`
          : "";
        let detailHtml;
        let clearBtn = "";
        if (mode === "kiz") {
          const entries = filledKizEntries(r);
          detailHtml = entries.length
            ? `<div class="tsd-scanned-kizs">${entries
                .map(
                  (e) => `
              <div class="tsd-scanned-kv">
                <span class="tsd-scanned-label">КИЗ:</span>
                <span class="tsd-scanned-kv-val">${esc(shortKizDisplay(e.code))}</span>
              </div>`
                )
                .join("")}</div>`
            : `<div class="tsd-scanned-kv"><span class="tsd-scanned-label">КИЗ:</span><span class="tsd-scanned-kv-val">—</span></div>`;
          clearBtn = `
            <button type="button" class="tsd-scanned-clear"
              data-action="clear-kiz-all" data-order-id="${oid}"
              aria-label="Очистить КИЗ" title="Очистить КИЗ">×</button>`;
        } else {
          const verified = String(r.pick_barcode || "").trim();
          detailHtml =
            !barcodes && verified
              ? `<div class="tsd-scanned-kv">
                  <span class="tsd-scanned-label">ШК:</span>
                  <span class="tsd-scanned-kv-val">${esc(verified)}</span>
                </div>`
              : "";
        }
        return `
          <div class="tsd-scanned-item">
            <div class="tsd-scanned-top">
              ${photo}
              <div class="tsd-scanned-text">
                <div class="tsd-scanned-order">Заказ ${oid} · ${stickerHtml}</div>
                <div class="tsd-scanned-name">${esc(r.product_name || r.article || "—")}</div>
              </div>
              ${clearBtn}
            </div>
            ${
              barcodesHtml || detailHtml
                ? `<div class="tsd-scanned-details">${barcodesHtml}${detailHtml}</div>`
                : ""
            }
          </div>`;
      })
      .join("");
    return `
      <section class="tsd-scanned" aria-label="Просканированные заказы">
        <h2 class="tsd-scanned-title">Просканировано · ${scanned.length}</h2>
        <div class="tsd-scanned-list" id="tsdScannedList">${items}</div>
      </section>`;
  }

  async function clearKizCodes(orderId) {
    if (state.saving || state.clearing) return;
    const oid = Number(orderId);
    const row = (state.kizRows || []).find((r) => Number(r.order_id) === oid);
    if (!row) return;
    if (!Array.isArray(row.kiz_codes)) row.kiz_codes = [""];
    const hadCodes = rowKizFilled(row);
    const wasBound = !!row.kiz_bound;
    const hadLocal = !!row.kiz_local || hadCodes;
    const needsWbClear =
      wasBound || (hadLocal && row.kiz_wb_synced === false) || !!state.pendingKizClear[oid];

    // Already empty (КИЗ «—»): just dismiss from «Просканировано».
    if (!hadCodes) {
      removeSessionScanned(oid);
      if (needsWbClear) {
        state.pendingKizClear[oid] = true;
        row.kiz_bound = wasBound || !!row.kiz_bound;
        row.kiz_local = hadLocal || !!row.kiz_local;
        setBanner(
          `Заказ ${oid} убран из списка — нажмите «Сохранить», чтобы очистить КИЗ на WB`,
          "ok"
        );
      } else {
        delete state.pendingKizClear[oid];
        setBanner(`Заказ ${oid} убран из просканированных`, "ok");
      }
      renderScan();
      return;
    }

    state.clearing = true;
    try {
      row.kiz_codes = [""];
      if (wasBound || hadLocal || needsWbClear) {
        state.pendingKizClear[oid] = true;
        // Keep flags until WB clear succeeds — mirrors desktop wasBound/hadLocal.
        row.kiz_bound = wasBound;
        row.kiz_local = hadLocal;
      } else {
        delete state.pendingKizClear[oid];
      }
      // Remove from «Просканировано» immediately — do not leave a «—» ghost row.
      removeSessionScanned(oid);
      await saveKizLocal(row);
      if (state.rowErrors[oid]) delete state.rowErrors[oid];
      if (String(row.kiz_status || "") === "error") row.kiz_status = "empty";
      setBanner(
        state.pendingKizClear[oid]
          ? `КИЗ очищен · заказ ${oid} убран из списка — нажмите «Сохранить», чтобы очистить на WB`
          : `КИЗ очищен · заказ ${oid}`,
        "ok"
      );
    } catch (e) {
      setBanner(e.message || String(e), "err");
    } finally {
      state.clearing = false;
      renderScan();
    }
  }

  function syncSourceSelectVisibility() {
    const sel = document.getElementById("tsdSourceSelect");
    if (!sel) return;
    // Source picker only on the assembly supplies list — not inside a supply / scan.
    const show = !!boot.can_view_wb_fbs_tsd && state.route.view === "list";
    sel.hidden = !show;
    sel.setAttribute("aria-hidden", show ? "false" : "true");
  }

  function syncSearchChrome() {
    const btn = document.getElementById("tsdSearchBtn");
    const panel = document.getElementById("tsdSearchPanel");
    const input = document.getElementById("tsdOrderSearch");
    const filterWrap = document.getElementById("tsdFilterWrap");
    const filterBtn = document.getElementById("tsdFilterBtn");
    const filterMenu = document.getElementById("tsdFilterMenu");
    const errorsLabel = document.getElementById("tsdFilterErrorsLabel");
    const view = state.route.view;
    const onList = !!boot.can_view_wb_fbs_tsd && view === "list";
    const onScan = !!boot.can_view_wb_fbs_tsd && view === "scan";
    const searchOk = onList || onScan;
    const mode = state.route.mode;

    if (btn) {
      btn.hidden = !searchOk;
      btn.setAttribute("aria-expanded", state.searchOpen && searchOk ? "true" : "false");
      btn.classList.toggle("is-active", !!(state.searchOpen && searchOk));
      btn.setAttribute(
        "aria-label",
        onList ? "Поиск поставок" : "Поиск заказов"
      );
      btn.title = onList ? "Поиск поставок" : "Поиск";
    }
    if (filterWrap) filterWrap.hidden = !onScan;

    const closeBtn = document.getElementById("tsdCloseBtn");
    if (closeBtn) closeBtn.hidden = !onScan;

    if (!searchOk) {
      state.searchOpen = false;
      if (panel) panel.hidden = true;
    } else if (panel) {
      panel.hidden = !state.searchOpen;
    }

    if (!onScan) {
      state.filterOpen = false;
      state.orderSearch = "";
      state.filters = { filled: false, empty: false, errors: false, cancelled: false };
      state.browseOpen = false;
      state.browseLimit = BROWSE_PAGE_SIZE;
      const sheet = document.getElementById("tsdBrowseSheet");
      if (sheet) sheet.remove();
      if (filterMenu) filterMenu.hidden = true;
      if (filterBtn) {
        filterBtn.setAttribute("aria-expanded", "false");
        filterBtn.classList.remove("is-active");
      }
      syncFilterInputsFromState();
    }

    if (!onList) {
      state.search = "";
    }

    if (input) {
      if (onList) {
        input.placeholder = "Поиск поставки…";
        if (state.searchOpen) {
          const want = state.search || "";
          if (String(input.value || "") !== want) input.value = want;
        } else {
          input.value = "";
        }
      } else if (onScan) {
        input.placeholder = "Стикер, заказ, ШК, артикул, название…";
        if (state.searchOpen) {
          const want = state.orderSearch || "";
          if (String(input.value || "") !== want) input.value = want;
        } else {
          input.value = "";
        }
      } else {
        input.value = "";
      }
    }

    // Full-screen browse sheet has its own search field — hide header search panel.
    if (panel && onScan && state.searchOpen && shouldShowBrowseSheet()) {
      panel.hidden = true;
    }

    if (onScan) {
      if (errorsLabel) errorsLabel.hidden = mode !== "kiz";
      if (mode !== "kiz" && state.filters.errors) state.filters.errors = false;
      if (filterBtn) {
        filterBtn.setAttribute("aria-expanded", state.filterOpen ? "true" : "false");
        filterBtn.classList.toggle("is-active", state.filterOpen || hasActiveFilters());
      }
      if (filterMenu) filterMenu.hidden = !state.filterOpen;
      syncFilterInputsFromState();
    }

    const app = document.getElementById("tsdApp");
    if (app) {
      app.classList.toggle("is-scan", onScan);
      app.classList.toggle("is-filter-menu-open", !!(onScan && state.filterOpen));
      app.classList.toggle("is-browse-open", !!(onScan && shouldShowBrowseSheet()));
    }
  }

  function syncFilterInputsFromState() {
    const filled = document.getElementById("tsdFilterFilled");
    const empty = document.getElementById("tsdFilterEmpty");
    const errors = document.getElementById("tsdFilterErrors");
    const cancelled = document.getElementById("tsdFilterCancelled");
    if (filled) filled.checked = !!state.filters.filled;
    if (empty) empty.checked = !!state.filters.empty;
    if (errors) errors.checked = !!state.filters.errors;
    if (cancelled) cancelled.checked = !!state.filters.cancelled;
  }

  const BROWSE_PAGE_SIZE = 40;

  function hasActiveFilters() {
    const f = state.filters || {};
    return !!(f.filled || f.empty || f.errors || f.cancelled);
  }

  function shouldShowBrowseSheet() {
    if (state.route.view !== "scan") return false;
    if (!state.browseOpen) return false;
    return hasActiveFilters() || state.searchOpen;
  }

  function openBrowseSheet(opts) {
    const resetLimit = !(opts && opts.keepLimit);
    if (resetLimit) state.browseLimit = BROWSE_PAGE_SIZE;
    state.browseOpen = true;
  }

  function closeBrowseSheet() {
    state.browseOpen = false;
  }

  function clearScanFiltersAndBrowse() {
    state.filters = { filled: false, empty: false, errors: false, cancelled: false };
    state.filterOpen = false;
    closeBrowseSheet();
    syncSearchChrome();
  }

  function matchedBrowseRows(mode) {
    const q = String(state.orderSearch || "").trim();
    const filtersOn = hasActiveFilters();
    const rows = mode === "kiz" ? state.kizRows : state.pickRows;
    let matched = rows || [];
    if (q) matched = filterOrdersBySearch(matched, q);
    else if (!filtersOn) matched = [];
    matched = applyOrderFilters(matched, mode);
    return matched;
  }

  function filterSummaryLabel() {
    const f = state.filters || {};
    const parts = [];
    if (f.filled) parts.push("заполненные");
    if (f.empty) parts.push("незаполненные");
    if (f.errors) parts.push("с ошибками");
    if (f.cancelled) parts.push("отменённые");
    return parts.join(", ");
  }

  function renderBrowseSheetHtml(mode) {
    if (!shouldShowBrowseSheet()) return "";
    const q = String(state.orderSearch || "").trim();
    const filtersOn = hasActiveFilters();
    const matched = matchedBrowseRows(mode);
    const limit = Math.max(BROWSE_PAGE_SIZE, Number(state.browseLimit) || BROWSE_PAGE_SIZE);
    const shown = matched.slice(0, limit);
    const hasMore = matched.length > shown.length;
    const title = q
      ? `Найдено · ${matched.length}`
      : filtersOn
        ? `Фильтр · ${matched.length}`
        : "Поиск";
    const sub = filtersOn && !q ? filterSummaryLabel() : "";
    let body;
    if (!q && !filtersOn) {
      body = `<div class="tsd-search-empty">Введите или отсканируйте стикер, номер заказа, ШК, артикул или название</div>`;
    } else if (!matched.length) {
      body = `<div class="tsd-search-empty">${
        filtersOn && !q ? "Нет заказов по выбранным фильтрам" : "Ничего не найдено"
      }</div>`;
    } else {
      const items = shown
        .map((r) => {
          const photo = r.product_photo
            ? `<img src="${esc(r.product_photo)}" alt="" width="48" height="48" />`
            : `<span class="tsd-scanned-ph" aria-hidden="true"></span>`;
          const barcodes = orderBarcodesLabel(r);
          const stickerHtml = formatBoldLastDigits(r.sticker_number || "—", 4);
          const cancelHtml = rowIsCancelled(r)
            ? ` · <span class="tsd-meta-cancelled">Отменён</span>`
            : "";
          const err = mode === "kiz" && rowHasKizError(r) ? " · Ошибка" : "";
          const status =
            mode === "kiz"
              ? rowKizFilled(r)
                ? "КИЗ есть"
                : "Нет КИЗ"
              : rowPickFilled(r)
                ? "ШК проверен"
                : "Не проверен";
          return `
            <button type="button" class="tsd-search-item" data-action="pick-search-order"
              data-order-id="${esc(String(r.order_id))}">
              ${photo}
              <div class="tsd-scanned-text">
                <div class="tsd-scanned-order">Заказ ${esc(r.order_id)} · ${stickerHtml}</div>
                <div class="tsd-scanned-name">${esc(r.product_name || r.article || "—")}</div>
                ${
                  barcodes
                    ? `<div class="tsd-scanned-barcodes">${esc(barcodes)}</div>`
                    : ""
                }
                <div class="tsd-scanned-meta">${esc(status)}${cancelHtml}${esc(err)}</div>
              </div>
            </button>`;
        })
        .join("");
      body = `
        <div class="tsd-search-list" id="tsdSearchList">${items}</div>
        ${
          hasMore
            ? `<button type="button" class="tsd-btn tsd-btn-secondary tsd-btn-block" id="tsdBrowseMore">
                Показать ещё · ${shown.length} из ${matched.length}
              </button>`
            : matched.length > BROWSE_PAGE_SIZE
              ? `<div class="tsd-browse-end">Показаны все ${matched.length}</div>`
              : ""
        }`;
    }
    return `
      <div class="tsd-browse-sheet" id="tsdBrowseSheet" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="tsd-browse-head">
          <div class="tsd-browse-head-text">
            <div class="tsd-browse-title">${esc(title)}</div>
            ${sub ? `<div class="tsd-browse-sub">${esc(sub)}</div>` : ""}
          </div>
          <div class="tsd-browse-actions">
            <button type="button" class="tsd-icon-btn tsd-browse-close" id="tsdBrowseClose"
              aria-label="Закрыть" title="Закрыть">×</button>
          </div>
        </div>
        ${
          state.searchOpen
            ? `<div class="tsd-browse-search">
                <input class="tsd-search-input" id="tsdBrowseSearchInput" type="search"
                  placeholder="Стикер, заказ, ШК, артикул, название…"
                  autocomplete="off" enterkeyhint="search"
                  value="${esc(String(state.orderSearch || ""))}" />
              </div>`
            : ""
        }
        <div class="tsd-browse-body">${body}</div>
      </div>`;
  }

  function syncBrowseSheetPosition() {
    const sheet = document.getElementById("tsdBrowseSheet");
    if (!sheet) return;
    // Full-viewport overlay — flush to the top edge over «Маркировка».
    sheet.style.top = "0px";
    sheet.style.bottom = "0px";
  }

  function scheduleBrowseSheetPositionSync() {
    syncBrowseSheetPosition();
    requestAnimationFrame(() => {
      syncBrowseSheetPosition();
      requestAnimationFrame(syncBrowseSheetPosition);
    });
  }

  function dismissBrowseSheetToScan() {
    // × replaces «Сбросить» — clear filters and close the overlay.
    state.filters = { filled: false, empty: false, errors: false, cancelled: false };
    state.filterOpen = false;
    closeBrowseSheet();
    if (state.searchOpen) {
      state.searchOpen = false;
      state.orderSearch = "";
      const input = document.getElementById("tsdOrderSearch");
      if (input) input.value = "";
    }
    syncSearchChrome();
    renderScan();
  }

  function wireBrowseSheet() {
    const sheet = document.getElementById("tsdBrowseSheet");
    if (!sheet) return;
    scheduleBrowseSheetPositionSync();
    const closeBtn = document.getElementById("tsdBrowseClose");
    if (closeBtn) {
      closeBtn.addEventListener("click", () => dismissBrowseSheetToScan());
    }
    const more = document.getElementById("tsdBrowseMore");
    if (more) {
      more.addEventListener("click", () => {
        state.browseLimit = (Number(state.browseLimit) || BROWSE_PAGE_SIZE) + BROWSE_PAGE_SIZE;
        openBrowseSheet({ keepLimit: true });
        renderScan({ keepSearchFocus: true });
      });
    }
    const searchList = document.getElementById("tsdSearchList");
    if (searchList) {
      searchList.addEventListener("click", (ev) => {
        const btn = ev.target && ev.target.closest
          ? ev.target.closest("[data-action='pick-search-order']")
          : null;
        if (!btn) return;
        ev.preventDefault();
        selectOrderFromSearch(btn.getAttribute("data-order-id"));
      });
    }
    const browseSearch = document.getElementById("tsdBrowseSearchInput");
    if (browseSearch) {
      browseSearch.addEventListener("input", () => {
        state.orderSearch = String(browseSearch.value || "");
        const headerInput = document.getElementById("tsdOrderSearch");
        if (headerInput && String(headerInput.value || "") !== state.orderSearch) {
          headerInput.value = state.orderSearch;
        }
        refreshSearchResultsOnly();
      });
      browseSearch.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape") {
          ev.preventDefault();
          dismissBrowseSheetToScan();
          return;
        }
        if (ev.key === "Enter") {
          ev.preventDefault();
          applyOrderSearchEnter();
        }
      });
      if (state.searchOpen) {
        setTimeout(() => {
          const el = document.getElementById("tsdBrowseSearchInput");
          if (el) {
            el.focus();
            el.select();
          }
        }, 40);
      }
    }
  }

  function renderSearchResultsHtml(_mode) {
    // Results live in the fixed browse sheet — never inline above the scan field.
    return "";
  }

  function refreshSearchResultsOnly() {
    if (state.route.view !== "scan") return;
    if (state.searchOpen || hasActiveFilters()) openBrowseSheet({ keepLimit: true });
    renderScan({ keepSearchFocus: true });
  }

  function rowHasKizError(row) {
    const oid = Number(row && row.order_id);
    if (oid && state.rowErrors[oid]) return true;
    return String((row && row.kiz_status) || "") === "error";
  }

  function rowIsCancelled(row) {
    return !!String((row && row.cancel_reason_label) || "").trim();
  }

  function applyOrderFilters(rows, mode) {
    let out = Array.isArray(rows) ? rows.slice() : [];
    const f = state.filters || {};
    if (f.filled) {
      out = out.filter((r) => (mode === "kiz" ? rowKizFilled(r) : rowPickFilled(r)));
    }
    if (f.empty) {
      out = out.filter((r) => (mode === "kiz" ? !rowKizFilled(r) : !rowPickFilled(r)));
    }
    if (f.errors && mode === "kiz") {
      out = out.filter((r) => rowHasKizError(r));
    }
    if (f.cancelled) {
      out = out.filter((r) => rowIsCancelled(r));
    }
    return out;
  }

  function resetScanFilters() {
    state.filterOpen = false;
    state.filters = { filled: false, empty: false, errors: false, cancelled: false };
    closeBrowseSheet();
  }

  function openHeaderSearch() {
    const view = state.route.view;
    if (view !== "list" && view !== "scan") return;
    state.searchOpen = true;
    if (view === "scan") openBrowseSheet();
    syncSearchChrome();
    if (view === "list") {
      const input = document.getElementById("tsdOrderSearch");
      if (input) {
        setTimeout(() => {
          input.focus();
          input.select();
        }, 40);
      }
    }
    if (view === "scan") {
      renderScan({ keepSearchFocus: true });
      scheduleBrowseSheetPositionSync();
    }
  }

  function closeHeaderSearch() {
    const view = state.route.view;
    const hadListSearch = view === "list" && !!String(state.search || "").trim();
    state.searchOpen = false;
    if (view === "list") state.search = "";
    if (view === "scan") {
      state.orderSearch = "";
      if (!hasActiveFilters()) closeBrowseSheet();
    }
    syncSearchChrome();
    const input = document.getElementById("tsdOrderSearch");
    if (input) input.value = "";
    if (view === "scan") {
      renderScan();
      return;
    }
    if (view === "list" && hadListSearch) {
      loadSupplies()
        .then(() => renderList())
        .catch((e) => toast(e.message || e));
    }
  }

  function openOrderSearch() {
    openHeaderSearch();
  }

  function closeOrderSearch() {
    closeHeaderSearch();
  }

  function closeFilterMenu() {
    if (!state.filterOpen) return;
    state.filterOpen = false;
    syncSearchChrome();
  }

  function toggleFilterMenu() {
    if (state.route.view !== "scan") return;
    // If filters already active and sheet closed — reopen results (not the dropdown).
    if (hasActiveFilters() && !state.browseOpen && !state.filterOpen) {
      openBrowseSheet();
      state.filterOpen = false;
      syncSearchChrome();
      renderScan({ keepSearchFocus: true });
      scheduleBrowseSheetPositionSync();
      return;
    }
    state.filterOpen = !state.filterOpen;
    syncSearchChrome();
    scheduleBrowseSheetPositionSync();
  }

  function onFilterChange(kind) {
    const filled = document.getElementById("tsdFilterFilled");
    const empty = document.getElementById("tsdFilterEmpty");
    const errors = document.getElementById("tsdFilterErrors");
    const cancelled = document.getElementById("tsdFilterCancelled");
    if (kind === "filled" && filled?.checked && empty) empty.checked = false;
    if (kind === "empty" && empty?.checked && filled) filled.checked = false;
    state.filters = {
      filled: !!filled?.checked,
      empty: !!empty?.checked,
      errors: state.route.mode === "kiz" ? !!errors?.checked : false,
      cancelled: !!cancelled?.checked,
    };
    if (hasActiveFilters()) {
      openBrowseSheet();
      // Close dropdown so the full-screen filter sheet is not trapped under the header.
      state.filterOpen = false;
    } else if (!state.searchOpen) {
      closeBrowseSheet();
    }
    syncSearchChrome();
    if (state.route.view === "scan") {
      renderScan({ keepSearchFocus: true });
      scheduleBrowseSheetPositionSync();
    }
  }

  function orderSearchHaystack(row) {
    const parts = [
      row.order_id,
      row.sticker_number,
      row.sticker_barcode,
      row.sticker_part_a,
      row.sticker_part_b,
      row.product_name,
      row.article,
      row.brand,
      row.pick_barcode,
      row.nm_id,
    ];
    const barcodes = Array.isArray(row.barcodes) ? row.barcodes : [];
    const skus = Array.isArray(row.skus) ? row.skus : [];
    const kiz = Array.isArray(row.kiz_codes) ? row.kiz_codes : [];
    for (const x of barcodes.concat(skus).concat(kiz)) parts.push(x);
    return parts
      .map((x) => String(x || "").trim().toLocaleLowerCase("ru-RU"))
      .filter(Boolean)
      .join("\n");
  }

  function filterOrdersBySearch(rows, query) {
    let q = String(query || "").trim();
    if (!q) return Array.isArray(rows) ? rows.slice() : [];
    if (hasCyrillic(q)) {
      const mapped = fixRuKeyboardLayout(q);
      if (!hasCyrillic(mapped)) q = mapped;
    }
    const needle = q.toLocaleLowerCase("ru-RU");
    const digits = digitsOnly(q);
    return (rows || []).filter((row) => {
      const hay = orderSearchHaystack(row);
      if (hay.includes(needle)) return true;
      if (digits && digits.length >= 3) {
        if (String(row.order_id || "").includes(digits)) return true;
        if (hay.replace(/\D+/g, "").includes(digits)) return true;
      }
      return false;
    });
  }

  function selectOrderFromSearch(orderId) {
    const mode = state.route.mode;
    const rows = mode === "kiz" ? state.kizRows : state.pickRows;
    const row = (rows || []).find((r) => Number(r.order_id) === Number(orderId));
    if (!row) {
      setBanner("Заказ не найден", "err");
      return;
    }
    state.pendingOrderId = Number(row.order_id);
    state.step = mode === "kiz" ? "mark" : "sku";
    state.searchOpen = false;
    state.orderSearch = "";
    closeBrowseSheet();
    closeFilterMenu();
    syncSearchChrome();
    const input = document.getElementById("tsdOrderSearch");
    if (input) input.value = "";
    setBanner(null);
    beep(true);
    renderScan();
    scrollToScanInput();
  }

  function applyOrderSearchEnter() {
    if (state.route.view !== "scan" || !state.searchOpen) return;
    const mode = state.route.mode;
    const rows = mode === "kiz" ? state.kizRows : state.pickRows;
    let raw = String(state.orderSearch || "").trim();
    if (!raw) return;
    if (hasCyrillic(raw)) {
      const mapped = fixRuKeyboardLayout(raw);
      if (hasCyrillic(mapped)) {
        setBanner("Русская раскладка — переключите на EN", "warn");
        beep(false);
        return;
      }
      raw = mapped;
      state.orderSearch = mapped;
      const input = document.getElementById("tsdOrderSearch");
      if (input) input.value = mapped;
    }
    const found = findBySticker(rows, raw);
    if (found.ambiguous) {
      setBanner("Стикер совпал у нескольких заказов — уточните поиск", "err");
      beep(false);
      refreshSearchResultsOnly();
      return;
    }
    if (found.row) {
      selectOrderFromSearch(found.row.order_id);
      return;
    }
    const matched = filterOrdersBySearch(rows, raw);
    if (matched.length === 1) {
      selectOrderFromSearch(matched[0].order_id);
      return;
    }
    if (!matched.length) {
      setBanner("Ничего не найдено", "err");
      beep(false);
      refreshSearchResultsOnly();
      return;
    }
    // Several matches — keep the list for a tap.
    setBanner(`Найдено ${matched.length} — выберите заказ`, "info");
    beep(true);
    refreshSearchResultsOnly();
  }

  function scrollToScanInput() {
    const target =
      document.getElementById("tsdScanInput") ||
      document.querySelector(".tsd-scan-card") ||
      document.getElementById("tsdMain");
    if (!target) {
      window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }
    const top = Math.max(0, target.getBoundingClientRect().top + window.scrollY - 72);
    window.scrollTo({ top, behavior: "smooth" });
    const input = document.getElementById("tsdScanInput");
    if (input) setTimeout(() => input.focus(), 280);
  }

  function syncScrollTopFab() {
    const fab = document.getElementById("tsdScrollTop");
    if (!fab) return;
    const onScan = state.route.view === "scan";
    const show = onScan && window.scrollY > 160;
    fab.hidden = !show;
  }

  function renderDenied() {
    const main = document.getElementById("tsdMain");
    syncSourceSelectVisibility();
    syncSearchChrome();
    syncScrollTopFab();
    const back = document.getElementById("tsdBackBtn");
    if (back) {
      back.hidden = false;
      back.href = "/app";
      back.textContent = "←";
    }
    document.getElementById("tsdTitle").textContent = "ТСД";
    main.innerHTML = `
      <div class="tsd-denied">
        <h1>Нет доступа</h1>
        <p>Раздел ТСД не разрешён для вашей учётной записи. Попросите владельца включить право «ТСД» в Команде.</p>
        <a class="tsd-btn tsd-btn-primary" href="/app">В кабинет</a>
      </div>`;
  }

  function renderList() {
    const main = document.getElementById("tsdMain");
    const back = document.getElementById("tsdBackBtn");
    const title = document.getElementById("tsdTitle");
    const prog = document.getElementById("tsdProgressBar");
    syncSourceSelectVisibility();
    syncSearchChrome();
    syncScrollTopFab();
    if (prog) prog.hidden = true;
    if (back) {
      back.hidden = false;
      back.href = "/app";
      back.onclick = null;
      back.textContent = "←";
    }
    title.textContent = "ТСД";

    if (!state.sources.length) {
      main.innerHTML = `<div class="tsd-empty">Нет доступных кабинетов ВБ ФБС для ТСД</div>`;
      return;
    }
    if (!state.supplies.length) {
      main.innerHTML = `<div class="tsd-empty">${
        state.search ? "Ничего не найдено" : "Нет поставок на сборке"
      }</div>`;
      return;
    }
    main.innerHTML = `
      <div class="tsd-list">
        ${state.supplies
          .map((s) => {
            const sid = String(s.supply_id || "");
            return `
            <button type="button" class="tsd-card" data-open-supply="${esc(sid)}">
              <div class="tsd-card-name">${esc(s.name || sid)}</div>
              <div class="tsd-card-meta">
                <div>QR: <strong>${esc(sid)}</strong></div>
                <div>${esc(ordersBoxesText(s))}</div>
                <div>Склад: <strong>${esc(s.warehouse_label || "—")}</strong></div>
              </div>
            </button>`;
          })
          .join("")}
      </div>`;
    main.querySelectorAll("[data-open-supply]").forEach((btn) => {
      btn.addEventListener("click", () => {
        navigate(`#/s/${btn.getAttribute("data-open-supply")}`);
      });
    });
  }

  let listSearchTimer = null;

  async function applyListSearchFromHeader() {
    if (state.route.view !== "list") return;
    try {
      await loadSupplies();
      if (state.route.view !== "list") return;
      renderList();
      const input = document.getElementById("tsdOrderSearch");
      if (input && state.searchOpen) {
        input.focus();
        const v = input.value;
        input.setSelectionRange(v.length, v.length);
      }
    } catch (e) {
      toast(e.message || e);
    }
  }

  function setKizHubTone(tone) {
    const t = String(tone || "").trim().toLowerCase();
    state.kizHubTone = t === "ok" || t === "error" ? t : "";
    const split = document.getElementById("tsdKizSplit");
    if (!split) return;
    split.classList.remove("is-ok", "is-error");
    if (state.kizHubTone === "ok") split.classList.add("is-ok");
    else if (state.kizHubTone === "error") split.classList.add("is-error");
  }

  async function refreshHubKizStatus(event) {
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }
    const sid = String(state.route.supplyId || (state.supply && state.supply.supply_id) || "").trim();
    if (!sid || !state.sourceId || state.kizStatusRefreshing) return;
    const refreshBtn = document.getElementById("tsdKizRefreshBtn");
    const kizBtn = document.getElementById("tsdTileKiz");
    state.kizStatusRefreshing = true;
    if (refreshBtn) {
      refreshBtn.disabled = true;
      refreshBtn.classList.add("is-spinning");
    }
    if (kizBtn) kizBtn.disabled = true;
    try {
      const params = new URLSearchParams({ source_id: String(state.sourceId) });
      const data = await api(
        `/api/wb-fbs/tsd/supplies/${encodeURIComponent(sid)}/kiz/status?${params}`
      );
      if (String(state.route.supplyId || "") !== sid || state.route.view !== "hub") return;
      state.kizHubToneSupplyId = sid;
      setKizHubTone(data.status);
    } catch (e) {
      if (String(state.route.supplyId || "") === sid && state.route.view === "hub") {
        toast(e.message || String(e));
      }
    } finally {
      state.kizStatusRefreshing = false;
      if (refreshBtn) {
        refreshBtn.disabled = false;
        refreshBtn.classList.remove("is-spinning");
      }
      if (kizBtn) {
        const s = state.supply || {};
        const kiz = s.kiz || { total: 0 };
        const kizError = String(s.kiz_error || "").trim();
        const kizDisabled = !kiz.total && !kizError;
        kizBtn.disabled = kizDisabled || state.kizStatusRefreshing;
      }
    }
  }

  function renderHub() {
    const s = state.supply || {};
    const sid = String(s.supply_id || state.route.supplyId || "");
    const main = document.getElementById("tsdMain");
    const back = document.getElementById("tsdBackBtn");
    const title = document.getElementById("tsdTitle");
    const prog = document.getElementById("tsdProgressBar");
    syncSourceSelectVisibility();
    syncSearchChrome();
    syncScrollTopFab();
    if (prog) prog.hidden = true;
    if (back) {
      back.hidden = false;
      back.href = "#/";
      back.onclick = (ev) => {
        ev.preventDefault();
        state.kizHubTone = "";
        state.kizHubToneSupplyId = "";
        navigate("#/");
      };
      back.textContent = "←";
    }
    title.textContent = "Поставка";

    const kiz = s.kiz || { done: 0, total: 0 };
    const pick = s.pick || { done: 0, total: 0 };
    const kizError = String(s.kiz_error || "").trim();
    const pickError = String(s.pick_error || "").trim();
    const kizDisabled = !kiz.total && !kizError;
    const pickDisabled = !pick.total && !pickError;

    main.innerHTML = `
      <h1 class="tsd-hub-name">${esc(s.name || sid)}</h1>
      <div class="tsd-hub-meta">
        <div>QR: <strong>${esc(sid)}</strong></div>
        <div>${esc(ordersBoxesText(s))}</div>
        <div>Склад: <strong>${esc(s.warehouse_label || "—")}</strong></div>
      </div>
      ${
        kizError || pickError
          ? `<div class="tsd-banner is-err">${esc(
              [kizError && `КИЗ: ${kizError}`, pickError && `ШК: ${pickError}`]
                .filter(Boolean)
                .join(" · ")
            )}</div>`
          : ""
      }
      <div class="tsd-tiles">
        <div class="tsd-tile-split" id="tsdKizSplit">
          <button type="button" class="tsd-tile tsd-tile-main" id="tsdTileKiz" ${
            kizDisabled ? "disabled" : ""
          }>
            <span class="tsd-tile-title">Товары с маркировкой</span>
            <span class="tsd-tile-prog">${
              kizError
                ? "Ошибка загрузки"
                : kizDisabled
                  ? "Нет заказов"
                  : `${kiz.done} / ${kiz.total}`
            }</span>
          </button>
          <button type="button" class="tsd-tile-refresh" id="tsdKizRefreshBtn"
            ${kizDisabled ? "disabled" : ""}
            aria-label="Проверить статусы КИЗ на Wildberries"
            title="Проверить статусы КИЗ на ВБ">
            <svg class="tsd-tile-refresh-ico" width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"
                    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M3 3v5h5"
                    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"
                    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M16 16h5v5"
                    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </button>
        </div>
        <button type="button" class="tsd-tile" id="tsdTilePick" ${pickDisabled ? "disabled" : ""}>
          <span class="tsd-tile-title">Товары без маркировки</span>
          <span class="tsd-tile-prog">${
            pickError
              ? "Ошибка загрузки"
              : pickDisabled
                ? "Нет заказов"
                : `${pick.done} / ${pick.total}`
          }</span>
        </button>
      </div>`;

    setKizHubTone(state.kizHubTone);

    const kizBtn = document.getElementById("tsdTileKiz");
    const pickBtn = document.getElementById("tsdTilePick");
    const refreshBtn = document.getElementById("tsdKizRefreshBtn");
    if (kizBtn && !kizDisabled) {
      kizBtn.addEventListener("click", () => navigate(`#/s/${sid}/kiz`));
    }
    if (pickBtn && !pickDisabled) {
      pickBtn.addEventListener("click", () => navigate(`#/s/${sid}/pick`));
    }
    if (refreshBtn && !kizDisabled) {
      refreshBtn.addEventListener("click", (ev) => refreshHubKizStatus(ev));
    }
  }

  function remainingRows(mode) {
    if (mode === "kiz") return state.kizRows.filter((r) => !rowKizFilled(r));
    return state.pickRows.filter((r) => !rowPickFilled(r));
  }

  function updateProgressBar(mode) {
    const prog = document.getElementById("tsdProgressBar");
    const fill = document.getElementById("tsdProgressFill");
    if (!prog || !fill) return;
    const rows = mode === "kiz" ? state.kizRows : state.pickRows;
    const fn = mode === "kiz" ? rowKizFilled : rowPickFilled;
    const { total, done } = countProgress(rows, fn);
    // Keep hidden: the 4px --tsd-line track looked like a leftover divider above
    // Готово/Осталось. Progress is already shown in .tsd-stats.
    prog.hidden = true;
    fill.style.width = total ? `${Math.round((100 * done) / total)}%` : "0%";
  }

  function hasUnsavedScanWork(mode) {
    const m = mode || state.route.mode;
    // Mid-scan step (sticker matched, waiting for КИЗ/ШК).
    if (state.pendingOrderId) return true;
    const session = state.sessionScannedIds || [];
    if (!session.length) {
      if (m === "kiz" && Object.keys(state.pendingKizClear || {}).length) return true;
      return false;
    }
    const sessionSet = new Set(session.map((x) => Number(x)));
    if (m === "kiz") {
      return (state.kizRows || []).some((row) => {
        const oid = Number(row.order_id);
        if (!sessionSet.has(oid)) return false;
        return rowNeedsKizWbClear(row) || rowKizFilled(row);
      });
    }
    return (state.pickRows || []).some(
      (row) => sessionSet.has(Number(row.order_id)) && rowPickFilled(row)
    );
  }

  function leaveScanScreen() {
    if (state.route.view !== "scan") return;
    const sid = state.route.supplyId;
    if (hasUnsavedScanWork(state.route.mode)) {
      if (!confirm("Есть несохранённые изменения. Закрыть без сохранения?")) {
        return;
      }
    }
    state.pendingOrderId = null;
    state.step = "sticker";
    state.searchOpen = false;
    state.orderSearch = "";
    state.sessionScannedIds = [];
    resetScanFilters();
    setBanner(null);
    navigate(`#/s/${sid}`);
  }

  function renderScan(opts) {
    const keepSearchFocus = !!(opts && opts.keepSearchFocus);
    const mode = state.route.mode;
    const sid = state.route.supplyId;
    const main = document.getElementById("tsdMain");
    const back = document.getElementById("tsdBackBtn");
    const title = document.getElementById("tsdTitle");
    syncSourceSelectVisibility();
    syncSearchChrome();
    if (back) {
      back.hidden = false;
      back.href = `#/s/${sid}`;
      back.onclick = (ev) => {
        ev.preventDefault();
        leaveScanScreen();
      };
      back.textContent = "←";
    }
    title.textContent = mode === "kiz" ? "С маркировкой" : "Без маркировки";
    updateProgressBar(mode);

    const rows = mode === "kiz" ? state.kizRows : state.pickRows;
    const fn = mode === "kiz" ? rowKizFilled : rowPickFilled;
    const { total, done, left } = countProgress(rows, fn);
    const pending = rows.find((r) => Number(r.order_id) === Number(state.pendingOrderId));
    const step = state.step;
    const banner = state.banner;

    let body = "";
    if (!total) {
      body = `<div class="tsd-empty">Нет заказов в этом режиме</div>`;
    } else if (step === "sticker" || !pending) {
      body = `
        <div class="tsd-scan-card" id="tsdScanCard">
          <div class="tsd-scan-step">Шаг 1</div>
          <p class="tsd-scan-prompt">Сканируйте стикер заказа</p>
          <div class="tsd-scan-field">
            <input class="tsd-scan-input" id="tsdScanInput" type="text" autocomplete="off" inputmode="none" />
            <button type="button" class="tsd-scan-clear" id="tsdScanClear" hidden
              aria-label="Очистить поле" title="Очистить">×</button>
          </div>
        </div>`;
    } else {
      const photo = pending.product_photo
        ? `<img src="${esc(pending.product_photo)}" alt="" width="64" height="64" />`
        : "";
      const existingKizN =
        mode === "kiz" ? filledKizEntries(pending).length : 0;
      const prompt =
        mode === "kiz"
          ? existingKizN
            ? `Сканируйте КИЗ ${existingKizN + 1}`
            : "Сканируйте КИЗ"
          : "Сканируйте штрихкод товара";
      const multiHint =
        mode === "kiz" && existingKizN
          ? `<p class="tsd-scan-subhint">У заказа уже ${existingKizN} КИЗ — новый код добавится к заказу</p>`
          : "";
      const pendingBarcodes = orderBarcodesLabel(pending);
      const pendingBarcodesHtml = pendingBarcodes
        ? `<div class="tsd-product-barcodes">${esc(pendingBarcodes)}</div>`
        : "";
      body = `
        <div class="tsd-scan-card" id="tsdScanCard">
          <div class="tsd-scan-step">Шаг 2</div>
          <p class="tsd-scan-prompt">${prompt}</p>
          ${multiHint}
          <div class="tsd-scan-context">Заказ ${esc(pending.order_id)} · стикер ${esc(pending.sticker_number || "—")}</div>
          <div class="tsd-scan-field">
            <input class="tsd-scan-input" id="tsdScanInput" type="text" autocomplete="off" inputmode="none" />
            <button type="button" class="tsd-scan-clear" id="tsdScanClear" hidden
              aria-label="Очистить поле" title="Очистить">×</button>
          </div>
          <div class="tsd-product">${photo}<div>
            <div class="tsd-product-name">${esc(pending.product_name || pending.article || "—")}</div>
            <div class="tsd-product-sub">${esc([pending.brand, pending.article].filter(Boolean).join(" · "))}</div>
            ${pendingBarcodesHtml}
          </div></div>
          <div class="tsd-scan-actions">
            <button type="button" class="tsd-btn tsd-btn-ghost tsd-btn-block" id="tsdCancelStep">Отмена шага</button>
          </div>
        </div>`;
    }

    const saveLabel = "Сохранить";
    const saveDisabled =
      state.saving ||
      state.clearing ||
      (mode === "kiz" ? !hasPendingKizPush() : !orderedScannedRows(mode).length);

    main.innerHTML = `
      <div class="tsd-scan-shell">
        <div class="tsd-stats">
          <span>Готово ${done} / ${total}</span>
          <span>Осталось ${left}</span>
        </div>
        ${
          banner
            ? `<div class="tsd-banner is-${esc(banner.kind)}">${esc(banner.text)}</div>`
            : ""
        }
        ${body}
        <div class="tsd-scan-footer">
          <button type="button" class="tsd-btn tsd-btn-primary tsd-btn-block" id="tsdSaveBtn"
            ${saveDisabled ? "disabled" : ""}>${esc(
              state.saving ? "Сохранение…" : saveLabel
            )}</button>
        </div>
        ${renderScannedListHtml(mode)}
      </div>`;

    // Fixed sheet under header — does not push the scan field down.
    const existingSheet = document.getElementById("tsdBrowseSheet");
    if (existingSheet) existingSheet.remove();
    const browseHtml = renderBrowseSheetHtml(mode).trim();
    if (browseHtml) {
      const app = document.getElementById("tsdApp") || document.body;
      const wrap = document.createElement("div");
      wrap.innerHTML = browseHtml;
      const sheet = wrap.firstElementChild;
      if (sheet) {
        const scrollTop = document.getElementById("tsdScrollTop");
        if (scrollTop && scrollTop.parentNode === app) app.insertBefore(sheet, scrollTop);
        else app.appendChild(sheet);
      }
      wireBrowseSheet();
    }

    const input = document.getElementById("tsdScanInput");
    const clearBtn = document.getElementById("tsdScanClear");
    const syncScanClearBtn = () => {
      if (!clearBtn || !input) return;
      clearBtn.hidden = !String(input.value || "").length;
    };
    if (input && !keepSearchFocus && !state.searchOpen && !shouldShowBrowseSheet()) {
      setTimeout(() => input.focus(), 40);
    }
    if (input) {
      syncScanClearBtn();
      input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") {
          ev.preventDefault();
          onScanEnter(input);
        }
      });
      // Do not remount on Cyrillic mid-scan — only banner hint; Enter applies layout map.
      input.addEventListener("input", () => {
        syncScanClearBtn();
        if (hasCyrillic(input.value)) {
          const el = document.querySelector(".tsd-banner");
          if (!el) {
            const shell = document.querySelector(".tsd-scan-shell");
            if (shell) {
              const ban = document.createElement("div");
              ban.className = "tsd-banner is-warn";
              ban.textContent = "Русская раскладка — переключите на EN (или сканируйте ещё раз)";
              shell.insertBefore(ban, shell.children[1] || null);
            }
          }
        }
      });
    }
    if (clearBtn && input) {
      clearBtn.addEventListener("click", () => {
        input.value = "";
        syncScanClearBtn();
        input.focus();
      });
    }
    const cancel = document.getElementById("tsdCancelStep");
    if (cancel) {
      cancel.addEventListener("click", () => {
        state.pendingOrderId = null;
        state.step = "sticker";
        setBanner(null);
        renderScan();
      });
    }
    const saveBtn = document.getElementById("tsdSaveBtn");
    if (saveBtn) {
      saveBtn.addEventListener("click", () => {
        if (mode === "kiz") saveKizPushAll();
        else savePickLocalAll();
      });
    }
    const scannedList = document.getElementById("tsdScannedList");
    if (scannedList && mode === "kiz") {
      scannedList.addEventListener("click", (ev) => {
        const btn = ev.target && ev.target.closest
          ? ev.target.closest("[data-action]")
          : null;
        if (!btn) return;
        const action = btn.getAttribute("data-action");
        const oid = btn.getAttribute("data-order-id");
        if (action === "clear-kiz-all") {
          ev.preventDefault();
          clearKizCodes(oid);
        }
      });
    }
    syncScrollTopFab();
  }

  async function onScanEnter(input) {
    const mode = state.route.mode;
    let raw = String(input.value || "");
    if (!normalizeScan(raw)) return;
    if (hasCyrillic(raw)) {
      const mapped = fixRuKeyboardLayout(raw);
      if (hasCyrillic(mapped)) {
        setBanner("Русская раскладка — переключите на EN", "warn");
        beep(false);
        input.value = "";
        input.focus();
        return;
      }
      raw = mapped;
      input.value = mapped;
    }

    if (state.step === "sticker" || !state.pendingOrderId) {
      const rows = mode === "kiz" ? state.kizRows : state.pickRows;
      const found = findBySticker(rows, raw);
      if (found.ambiguous) {
        setBanner("Стикер совпал у нескольких заказов — сканируйте QR ещё раз", "err");
        beep(false);
        input.select();
        renderScan();
        return;
      }
      if (!found.row) {
        setBanner(
          mode === "kiz"
            ? "Стикер не найден среди заказов с КИЗ"
            : "Стикер не найден среди заказов без КИЗ",
          "err"
        );
        beep(false);
        input.select();
        renderScan();
        return;
      }
      state.pendingOrderId = Number(found.row.order_id);
      state.step = mode === "kiz" ? "mark" : "sku";
      setBanner(null);
      beep(true);
      renderScan();
      return;
    }

    const rows = mode === "kiz" ? state.kizRows : state.pickRows;
    const row = rows.find((r) => Number(r.order_id) === Number(state.pendingOrderId));
    if (!row) {
      state.pendingOrderId = null;
      state.step = "sticker";
      renderScan();
      return;
    }

    try {
      if (mode === "kiz") {
        const mark = normalizeKizMark(raw);
        const check = markMatchesOrder(mark, row);
        if (!check.ok) {
          setBanner(check.error || "КИЗ не подходит", "err");
          state.rowErrors[Number(row.order_id)] = check.error || "КИЗ не подходит";
          beep(false);
          input.select();
          renderScan();
          return;
        }
        delete state.rowErrors[Number(row.order_id)];
        delete state.pendingKizClear[Number(row.order_id)];
        const ownDup = (Array.isArray(row.kiz_codes) ? row.kiz_codes : []).some(
          (c) => normalizeKizMark(c) === mark
        );
        if (ownDup) {
          setBanner("Этот КИЗ уже в этом заказе", "err");
          beep(false);
          input.select();
          renderScan();
          return;
        }
        const dup = state.kizRows.find((r) =>
          Number(r.order_id) !== Number(row.order_id) &&
          (Array.isArray(r.kiz_codes) ? r.kiz_codes : []).some(
            (c) => normalizeKizMark(c) === mark
          )
        );
        if (dup) {
          setBanner(`Этот КИЗ уже в заказе ${dup.order_id}`, "err");
          beep(false);
          input.select();
          renderScan();
          return;
        }
        if (!Array.isArray(row.kiz_codes) || !row.kiz_codes.length) row.kiz_codes = [""];
        let placed = false;
        for (let i = 0; i < row.kiz_codes.length; i += 1) {
          if (!String(row.kiz_codes[i] || "").trim()) {
            row.kiz_codes[i] = mark;
            placed = true;
            break;
          }
        }
        if (!placed) row.kiz_codes.push(mark);
        await saveKizLocal(row);
        row.kiz_local = true;
        noteSessionScanned(row.order_id);
        const kizN = filledKizEntries(row).length;
        setBanner(
          kizN <= 1
            ? `КИЗ записан · заказ ${row.order_id}. Для 2-го КИЗ снова сканируйте стикер`
            : `КИЗ ${kizN} записан · заказ ${row.order_id}`,
          "ok"
        );
      } else {
        const check = eanMatchesOrder(raw, row);
        if (!check.ok) {
          setBanner(check.error || "ШК не подходит", "err");
          beep(false);
          input.select();
          renderScan();
          return;
        }
        row.pick_verified = true;
        row.pick_barcode = digitsOnly(raw);
        await savePickLocal(row);
        noteSessionScanned(row.order_id);
        setBanner(`ШК подтверждён · заказ ${row.order_id}`, "ok");
      }
      beep(true);
      state.pendingOrderId = null;
      state.step = "sticker";
      renderScan();
    } catch (e) {
      setBanner(e.message || String(e), "err");
      beep(false);
      renderScan();
    }
  }

  async function onRoute() {
    if (!boot.can_view_wb_fbs_tsd) {
      renderDenied();
      return;
    }
    state.route = parseHash();
    syncSourceSelectVisibility();
    const seq = ++state.loadSeq;
    stopLoadingUi();

    // Show destination loader immediately so the previous screen never lingers.
    if (state.route.view === "hub") {
      showLoadingScreen({
        title: `Открываем ${supplyNameHint(state.route.supplyId)}`,
        status: "Ищем поставку…",
        stages: ["Открытие", "С маркировкой", "Без маркировки"],
      });
    } else if (state.route.view === "scan") {
      // Switch chrome immediately so the hub title/strip never lingers under load.
      syncSourceSelectVisibility();
      syncSearchChrome();
      const titleEl = document.getElementById("tsdTitle");
      if (titleEl) {
        titleEl.textContent =
          state.route.mode === "kiz" ? "С маркировкой" : "Без маркировки";
      }
      const backEl = document.getElementById("tsdBackBtn");
      if (backEl) {
        backEl.hidden = false;
        backEl.href = `#/s/${state.route.supplyId}`;
        backEl.textContent = "←";
        backEl.onclick = (ev) => {
          ev.preventDefault();
          leaveScanScreen();
        };
      }
      if (state.route.mode === "kiz") {
        showLoadingScreen({
          title: "Товары с маркировкой",
          simple: true,
        });
      } else {
        showLoadingScreen({
          title: "Товары без маркировки",
          simple: true,
        });
      }
    } else {
      showLoadingScreen({
        title: "Поставки на сборке",
        status: "Загружаем список поставок…",
        stages: ["Список поставок"],
      });
    }

    try {
      if (!state.sources.length) {
        setLoadingStatus("Загружаем кабинеты…", 0);
        await loadSources();
        if (seq !== state.loadSeq) return;
        if (state.route.view === "list") {
          setLoadingStatus("Загружаем список поставок…", 0);
        } else if (state.route.view === "hub") {
          setLoadingStatus("Ищем поставку…", 0);
        }
      }

      if (state.route.view === "list") {
        state.pendingOrderId = null;
        state.step = "sticker";
        state.banner = null;
        setLoadingStatus("Загружаем список поставок…", 0);
        await loadSupplies();
        if (seq !== state.loadSeq) return;
        stopLoadingUi();
        renderList();
        return;
      }

      if (!state.sourceId) {
        stopLoadingUi();
        const main = document.getElementById("tsdMain");
        if (main) main.innerHTML = `<div class="tsd-empty">Выберите кабинет</div>`;
        return;
      }

      if (state.route.view === "hub") {
        state.pendingOrderId = null;
        state.step = "sticker";
        state.banner = null;
        const sid = state.route.supplyId;
        if (String(state.kizHubToneSupplyId || "") !== String(sid || "")) {
          state.kizHubTone = "";
          state.kizHubToneSupplyId = String(sid || "");
        }
        const stopRotate = startLoadingRotate(
          [
            { status: "Открываем поставку…", stage: 0 },
            { status: "Считаем прогресс…", stage: 1 },
          ],
          1800
        );
        try {
          await loadSummary(sid);
        } finally {
          stopRotate();
        }
        if (seq !== state.loadSeq) return;
        stopLoadingUi();
        renderHub();
        return;
      }

      if (state.route.view === "scan") {
        state.pendingOrderId = null;
        state.step = "sticker";
        state.sessionScannedIds = [];
        state.searchOpen = false;
        state.orderSearch = "";
        resetScanFilters();
        if (state.route.mode === "kiz") {
          await loadKiz(state.route.supplyId);
        } else {
          await loadPick(state.route.supplyId);
        }
        if (seq !== state.loadSeq) return;
        stopLoadingUi();
        if (!state.step) state.step = "sticker";
        renderScan();
      }
    } catch (e) {
      if (seq !== state.loadSeq) return;
      stopLoadingUi();
      const main = document.getElementById("tsdMain");
      if (main) {
        main.innerHTML = `<div class="tsd-empty" style="color:#b91c1c">${esc(e.message || e)}</div>`;
      }
    }
  }

  function bindChrome() {
    const sel = document.getElementById("tsdSourceSelect");
    if (sel) {
      sel.addEventListener("change", async () => {
        state.sourceId = sel.value ? Number(sel.value) : null;
        if (state.sourceId) localStorage.setItem(LS_SOURCE, String(state.sourceId));
        if (state.route.view !== "list") navigate("#/");
        else {
          const seq = ++state.loadSeq;
          try {
            showLoadingScreen({
              title: "Поставки на сборке",
              status: "Обновляем список для кабинета…",
              stages: ["Список поставок"],
            });
            await loadSupplies();
            if (seq !== state.loadSeq) return;
            stopLoadingUi();
            renderList();
          } catch (e) {
            if (seq !== state.loadSeq) return;
            stopLoadingUi();
            toast(e.message || e);
            try {
              renderList();
            } catch (_err) {
              /* ignore */
            }
          }
        }
      });
    }
    const searchBtn = document.getElementById("tsdSearchBtn");
    if (searchBtn) {
      searchBtn.addEventListener("click", () => {
        if (state.searchOpen) closeHeaderSearch();
        else openHeaderSearch();
      });
    }
    const closeBtn = document.getElementById("tsdCloseBtn");
    if (closeBtn) {
      closeBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        leaveScanScreen();
      });
    }
    const filterBtn = document.getElementById("tsdFilterBtn");
    if (filterBtn) {
      filterBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        toggleFilterMenu();
      });
    }
    const filterFilled = document.getElementById("tsdFilterFilled");
    const filterEmpty = document.getElementById("tsdFilterEmpty");
    const filterErrors = document.getElementById("tsdFilterErrors");
    const filterCancelled = document.getElementById("tsdFilterCancelled");
    if (filterFilled) {
      filterFilled.addEventListener("change", () => onFilterChange("filled"));
    }
    if (filterEmpty) {
      filterEmpty.addEventListener("change", () => onFilterChange("empty"));
    }
    if (filterErrors) {
      filterErrors.addEventListener("change", () => onFilterChange("errors"));
    }
    if (filterCancelled) {
      filterCancelled.addEventListener("change", () => onFilterChange("cancelled"));
    }
    document.addEventListener("click", (ev) => {
      if (!state.filterOpen) return;
      const wrap = document.getElementById("tsdFilterWrap");
      if (wrap && wrap.contains(ev.target)) return;
      closeFilterMenu();
    });
    const searchClose = document.getElementById("tsdSearchClose");
    if (searchClose) {
      searchClose.addEventListener("click", () => closeHeaderSearch());
    }
    const orderSearch = document.getElementById("tsdOrderSearch");
    if (orderSearch) {
      orderSearch.addEventListener("input", () => {
        if (state.route.view === "list") {
          state.search = String(orderSearch.value || "").trim();
          clearTimeout(listSearchTimer);
          listSearchTimer = setTimeout(() => applyListSearchFromHeader(), 280);
          return;
        }
        state.orderSearch = String(orderSearch.value || "");
        refreshSearchResultsOnly();
      });
      orderSearch.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape") {
          ev.preventDefault();
          closeHeaderSearch();
          return;
        }
        if (ev.key === "Enter") {
          ev.preventDefault();
          if (state.route.view === "list") {
            clearTimeout(listSearchTimer);
            state.search = String(orderSearch.value || "").trim();
            applyListSearchFromHeader();
            return;
          }
          applyOrderSearchEnter();
        }
      });
    }
    const scrollTop = document.getElementById("tsdScrollTop");
    if (scrollTop) {
      scrollTop.addEventListener("click", () => scrollToScanInput());
    }
    window.addEventListener(
      "scroll",
      () => {
        syncScrollTopFab();
        if (document.getElementById("tsdBrowseSheet")) syncBrowseSheetPosition();
      },
      { passive: true }
    );
    window.addEventListener("resize", () => {
      if (document.getElementById("tsdBrowseSheet")) scheduleBrowseSheetPositionSync();
    });
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", () => {
        if (document.getElementById("tsdBrowseSheet")) scheduleBrowseSheetPositionSync();
      });
    }
    window.addEventListener("hashchange", onRoute);
  }

  async function bootApp() {
    bindChrome();
    if (!boot.can_view_wb_fbs_tsd) {
      renderDenied();
      return;
    }
    await onRoute();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootApp);
  } else {
    bootApp();
  }
})();
