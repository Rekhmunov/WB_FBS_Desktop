function esc(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function readCookie(name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = document.cookie.match(new RegExp(`(?:^|; )${escaped}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : "";
}

function csrfHeaders(extra = {}) {
  const token = readCookie("csrf_token");
  if (token) return { ...extra, "X-CSRF-Token": token };
  return { ...extra };
}

const roleLabels = {
  user: "пользователь",
  feedback_manager: "менеджер обратной связи",
  admin: "администратор",
};
const reviewStatusLabels = {
  queued_for_operator: "Ждет обработки",
  answered_auto: "Обработан автоматически",
  answered_manual: "Обработан оператором",
  ignored: "Игнор",
};
const actionTypeLabels = {
  sync_error: "Ошибка синхронизации",
  sync_review: "Синхронизация отзыва",
  sync_conversation: "Синхронизация диалога",
  queue_manual: "Перевод в ручную обработку",
  auto_reply: "Автоответ",
  manual_reply: "Ручной ответ",
  conversation_status: "Смена статуса диалога",
};
const detailKeyLabels = {
  category: "категория",
  status: "статус",
  source: "источник",
  account_id: "идентификатор кабинета",
  channel: "канал",
  error: "ошибка",
  scope: "область",
  kind: "тип",
  reply: "ответ",
  marketplace: "маркетплейс",
  reason: "причина",
  access_denied: "доступ запрещен",
};
const categoryLabels = {
  positive: "Позитив",
  product_dissatisfaction: "Недовольство товаром",
  delivery_problems: "Проблемы при доставке",
  wrong_size: "Неправильный размер",
  tagged_reviews: "Отзывы с тегами",
  textless_ratings: "Оценки без текста",
  negative_delivery: "Негатив: доставка",
  negative_product: "Негатив: товар",
  negative_other: "Негатив: прочее",
  positive_quality: "Позитив: качество",
  positive_product: "Позитив: товар",
  neutral_other: "Нейтральный: прочее",
};
const conversationKindLabels = {
  question: "вопрос",
  chat: "чат",
};
const conversationStatusLabels = {
  open: "открыт",
  waiting: "ожидает",
  closed: "закрыт",
};
const tenantRoleLabels = {
  admin: "администратор кабинета",
  feedback_manager: "менеджер обратной связи",
};
const ALL_ROLE_VALUES = ["user", "feedback_manager", "admin"];
const TENANT_ROLE_VALUES = ["feedback_manager", "admin"];

const adminState = {
  context: null,
  hasYandexApiKey: false,
  aiEditMode: false,
  aiSettingsSnapshot: null,
  aiTestReviewLoading: false,
  aiActualIdsLoading: false,
};
const defaultTemplatesState = {
  items: [],
  currentGroupId: null,
  currentGroupTitle: "",
  currentSubgroup: "",
  currentTemplates: [],
};
const defaultTemplateBulkState = {
  groupId: "",
  subgroup: "",
};
const templateVariablesState = {
  items: [],
  editKey: null,
};
const actionsState = {
  page: 1,
  pageSize: 50,
  hasMore: false,
  total: 0,
  action_type: "all",
  actor: "all",
  date_from: null,
  date_to: null,
  search: "",
};
let actionsSearchTimer = null;
const usersState = {
  items: [],
  search: "",
  page: 1,
  pageSize: 10,
};
const tariffEditorState = {
  mode: "create",
  originalCode: null,
};

function normalizeNumber(value, fallback = 0) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return parsed;
}

function tariffLimitsFromFields() {
  return {
    reviews_per_month: Math.max(0, Math.floor(normalizeNumber(document.getElementById("tariffLimitReviews")?.value, 0))),
    managers: Math.max(0, Math.floor(normalizeNumber(document.getElementById("tariffLimitManagers")?.value, 0))),
    sources: Math.max(0, Math.floor(normalizeNumber(document.getElementById("tariffLimitSources")?.value, 0))),
    ai_units: Math.max(0, Math.floor(normalizeNumber(document.getElementById("tariffLimitAiUnits")?.value, 0))),
  };
}

function openTariffForm(mode = "create", item = null) {
  const form = document.getElementById("tariffFormPanel");
  if (!form) return;
  const isEdit = mode === "edit" && item;
  tariffEditorState.mode = isEdit ? "edit" : "create";
  tariffEditorState.originalCode = isEdit ? String(item.code || "").trim().toLowerCase() : null;

  const title = document.getElementById("tariffFormTitle");
  const saveBtn = document.getElementById("tariffSaveButton");
  const cancelBtn = document.getElementById("tariffCancelButton");
  const codeInput = document.getElementById("tariffCode");
  const nameInput = document.getElementById("tariffTitle");
  const priceInput = document.getElementById("tariffPrice");
  const reviewsInput = document.getElementById("tariffLimitReviews");
  const managersInput = document.getElementById("tariffLimitManagers");
  const sourcesInput = document.getElementById("tariffLimitSources");
  const aiUnitsInput = document.getElementById("tariffLimitAiUnits");
  const activeInput = document.getElementById("tariffIsActive");

  if (title) title.textContent = isEdit ? "Изменить тариф" : "Добавить тариф";
  if (saveBtn) saveBtn.textContent = isEdit ? "Сохранить изменения" : "Создать тариф";
  if (cancelBtn) cancelBtn.classList.toggle("hidden", !isEdit);

  const limits = (item && item.limits) || {};
  if (codeInput) codeInput.value = isEdit ? String(item.code || "") : "";
  if (nameInput) nameInput.value = isEdit ? String(item.title || "") : "";
  if (priceInput) priceInput.value = isEdit ? String(item.monthly_price ?? 0) : "";
  if (reviewsInput) reviewsInput.value = isEdit ? String(limits.reviews_per_month ?? 0) : "";
  if (managersInput) managersInput.value = isEdit ? String(limits.managers ?? 0) : "";
  if (sourcesInput) sourcesInput.value = isEdit ? String(limits.sources ?? 0) : "";
  if (aiUnitsInput) aiUnitsInput.value = isEdit ? String(limits.ai_units ?? 0) : "";
  if (activeInput) activeInput.checked = isEdit ? Boolean(item.is_active) : true;

  form.classList.remove("hidden");
}

function openCreateTariffForm() {
  openTariffForm("create");
}

function closeTariffForm() {
  const form = document.getElementById("tariffFormPanel");
  if (!form) return;
  tariffEditorState.mode = "create";
  tariffEditorState.originalCode = null;
  form.classList.add("hidden");
}

function isSuperAdmin() {
  return Boolean(adminState.context && adminState.context.is_super_admin);
}

function isTenantOwner() {
  return Boolean(adminState.context && adminState.context.is_tenant_owner);
}

function labelFromMap(map, value) {
  const key = String(value || "");
  return map[key] || key || "-";
}

function formatActionDetails(details) {
  if (!details || typeof details !== "object") return "-";
  const parts = [];
  for (const [key, rawValue] of Object.entries(details)) {
    let value = rawValue;
    if (key === "category") value = labelFromMap(categoryLabels, rawValue);
    if (key === "status") {
      value = labelFromMap(reviewStatusLabels, rawValue);
      if (value === String(rawValue)) value = labelFromMap(conversationStatusLabels, rawValue);
    }
    if (key === "kind") value = labelFromMap(conversationKindLabels, rawValue);
    if (key === "access_denied") value = rawValue ? "да" : "нет";
    const label = detailKeyLabels[key] || "параметр";
    parts.push(`${label}: ${value}`);
  }
  return parts.join("; ");
}

async function loadAdminContext() {
  const res = await fetch("/api/admin/context");
  const data = await res.json();
  if (!res.ok) {
    setUsersInfo(data.detail || "Не удалось определить контекст администратора", true);
    adminState.context = null;
    return false;
  }
  adminState.context = data;

  const roleSelect = document.getElementById("newUserRole");
  if (roleSelect) {
    const allowedRoles = isSuperAdmin() ? ALL_ROLE_VALUES : TENANT_ROLE_VALUES;
    roleSelect.innerHTML = "";
    for (const role of allowedRoles) {
      const option = document.createElement("option");
      option.value = role;
      option.textContent = roleLabels[role] || role;
      roleSelect.appendChild(option);
    }
    roleSelect.value = allowedRoles.includes("feedback_manager") ? "feedback_manager" : allowedRoles[0];
  }
  const createRoleWrap = document.getElementById("newUserRoleWrap");
  if (createRoleWrap) {
    createRoleWrap.classList.toggle("hidden", isSuperAdmin());
  }
  const planSelect = document.getElementById("newUserPlan");
  if (planSelect) {
    planSelect.classList.toggle("hidden", !isSuperAdmin());
  }

  const superAiPanel = document.getElementById("superAdminAiPanel");
  const superSaasPanel = document.getElementById("superAdminSaasPanel");
  if (isSuperAdmin()) {
    if (superAiPanel) superAiPanel.classList.remove("hidden");
    if (superSaasPanel) superSaasPanel.classList.remove("hidden");
    document.getElementById("superAdminDefaultTemplatesPanel")?.classList.remove("hidden");
  } else {
    if (superAiPanel) superAiPanel.classList.add("hidden");
    if (superSaasPanel) superSaasPanel.classList.add("hidden");
    document.getElementById("superAdminDefaultTemplatesPanel")?.classList.add("hidden");
  }
  return true;
}

function setUsersInfo(message, isError = false) {
  const info = document.getElementById("usersInfo");
  if (!info) return;
  info.textContent = message || "";
  info.style.color = isError ? "#b91c1c" : "";
}

async function loadAiSettings() {
  const res = await fetch("/api/admin/ai-settings");
  const data = await res.json();
  if (!res.ok) {
    document.getElementById("aiInfo").textContent = data.detail || "Ошибка";
    return;
  }
  adminState.hasYandexApiKey = Boolean(data.has_yandex_api_key);
  adminState.aiSettingsSnapshot = data;
  adminState.aiEditMode = false;
  renderAiSettingsMode(data);
  const lookbackInput = document.getElementById("defaultSyncLookbackDays");
  if (lookbackInput) {
    const lookback = Number(data.default_sync_lookback_days || 7);
    lookbackInput.value = String(Number.isFinite(lookback) ? lookback : 7);
  }
  document.getElementById("aiInfo").textContent = "";
}

function renderAiSettingsMode(data = {}) {
  const apiKeyInput = document.getElementById("apiKey");
  const folderIdInput = document.getElementById("folderId");
  const editBtn = document.getElementById("aiEditBtn");
  const saveBtn = document.getElementById("aiSaveBtn");
  const cancelBtn = document.getElementById("aiCancelBtn");
  const testBtn = document.getElementById("aiTestBtn");
  const testReviewBtn = document.getElementById("aiTestReviewBtn");
  if (!apiKeyInput || !folderIdInput) return;

  const preview = String(data.yandex_api_key_preview || "");
  const folderId = String(data.yandex_folder_id || "");
  const inEditMode = Boolean(adminState.aiEditMode);

  if (inEditMode) {
    apiKeyInput.type = "password";
    apiKeyInput.readOnly = false;
    apiKeyInput.value = "";
    apiKeyInput.placeholder = adminState.hasYandexApiKey
      ? "Новый API-ключ (оставьте пустым, чтобы не менять)"
      : "API-ключ Yandex Cloud";
    folderIdInput.readOnly = false;
    folderIdInput.value = folderId;
    if (editBtn) editBtn.classList.add("hidden");
    if (saveBtn) saveBtn.classList.remove("hidden");
    if (cancelBtn) cancelBtn.classList.remove("hidden");
    if (testBtn) testBtn.disabled = false;
    if (testReviewBtn) testReviewBtn.disabled = false;
    return;
  }

  apiKeyInput.type = "password";
  apiKeyInput.readOnly = true;
  apiKeyInput.value = preview || "";
  apiKeyInput.placeholder = adminState.hasYandexApiKey ? "API-ключ сохранен" : "API-ключ Yandex Cloud";
  folderIdInput.readOnly = true;
  folderIdInput.value = folderId;
  if (editBtn) editBtn.classList.remove("hidden");
  if (saveBtn) saveBtn.classList.add("hidden");
  if (cancelBtn) cancelBtn.classList.add("hidden");
  if (testBtn) testBtn.disabled = !adminState.hasYandexApiKey || !folderId;
  if (testReviewBtn) testReviewBtn.disabled = !adminState.hasYandexApiKey || !folderId;
}

function editAiSettings() {
  adminState.aiEditMode = true;
  renderAiSettingsMode({
    yandex_folder_id: adminState.aiSettingsSnapshot?.yandex_folder_id || "",
  });
}

function cancelAiSettingsEdit() {
  adminState.aiEditMode = false;
  loadAiSettings();
}

function syncDateToggle() {
  return;
}

async function saveAiSettings() {
  if (!adminState.aiEditMode) {
    editAiSettings();
    return;
  }
  const aiInfo = document.getElementById("aiInfo");
  if (aiInfo) {
    aiInfo.textContent = "Сохраняем настройки...";
    aiInfo.style.color = "";
  }
  const lookbackRaw = Number(document.getElementById("defaultSyncLookbackDays")?.value || "7");
  const defaultSyncLookbackDays = Number.isFinite(lookbackRaw)
    ? Math.max(0, Math.min(365, Math.floor(lookbackRaw)))
    : 7;
  const apiKey = String(document.getElementById("apiKey")?.value || "").trim();
  const folderId = String(document.getElementById("folderId")?.value || "").trim();
  if (!folderId) {
    if (aiInfo) {
      aiInfo.textContent = "Укажите folderId для подключения Yandex GPT.";
      aiInfo.style.color = "#b91c1c";
    }
    return;
  }
  if (!apiKey && !adminState.hasYandexApiKey) {
    if (aiInfo) {
      aiInfo.textContent = "Укажите API-ключ для подключения Yandex GPT.";
      aiInfo.style.color = "#b91c1c";
    }
    return;
  }
  const payload = {
    provider: "yandex",
    yandex_api_key: apiKey || null,
    yandex_folder_id: folderId,
    yandex_model_uri: null,
    group_processors: null,
    use_sync_start_date: false,
    sync_start_date: null,
    default_sync_lookback_days: defaultSyncLookbackDays,
  };
  const res = await fetch("/api/admin/ai-settings", {
    method: "PUT",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    if (aiInfo) {
      aiInfo.textContent = "Ошибка: " + (data.detail || "не удалось сохранить");
      aiInfo.style.color = "#b91c1c";
    }
    return;
  }
  if (aiInfo) {
    aiInfo.textContent = "Настройки сохранены";
    aiInfo.style.color = "";
  }
  await loadAiSettings();
}

async function testAiSettingsConnection() {
  const aiInfo = document.getElementById("aiInfo");
  if (aiInfo) {
    aiInfo.textContent = "Проверяем подключение...";
    aiInfo.style.color = "";
  }
  const payload = {
    yandex_folder_id: String(document.getElementById("folderId")?.value || "").trim() || null,
    yandex_api_key: adminState.aiEditMode ? (String(document.getElementById("apiKey")?.value || "").trim() || null) : null,
  };
  const res = await fetch("/api/admin/ai-settings/check", {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    if (aiInfo) {
      aiInfo.textContent = "Ошибка проверки: " + (data.detail || data.error || "подключение не прошло");
      aiInfo.style.color = "#b91c1c";
    }
    return;
  }
  if (aiInfo) {
    aiInfo.textContent = data.message || "Подключение успешно";
    aiInfo.style.color = "#166534";
  }
}

async function openAiStatsModal() {
  const modal = document.getElementById("aiStatsModal");
  if (modal) modal.classList.remove("hidden");
  // Reset detail log state
  _aiReqLogLoaded = false;
  const det = document.getElementById("aiRequestLogDetails");
  if (det) det.removeAttribute("open");
  const arrow = document.getElementById("aiReqLogArrow");
  if (arrow) { arrow.style.transform = ""; arrow.textContent = "▶"; }
  const logContent = document.getElementById("aiRequestLogContent");
  if (logContent) logContent.innerHTML = '<p class="small" style="color:#9ca3af;padding:10px 14px">Нажмите, чтобы загрузить...</p>';
  const content = document.getElementById("aiStatsContent");
  if (!content) return;
  content.innerHTML = '<p class="small" style="color:#9ca3af">Загрузка...</p>';
  try {
    const res = await fetch("/api/admin/ai-usage-stats?days=30");
    if (!res.ok) { content.innerHTML = '<p class="small" style="color:#dc2626">Ошибка загрузки</p>'; return; }
    const data = await res.json();
    const rows = data.rows || [];
    const totals = data.totals || {};

    const totalRow = `
      <tr style="font-weight:600;background:#f0f9ff">
        <td>Итого (30 дней)</td>
        <td style="text-align:right">${(totals.requests||0).toLocaleString("ru-RU")}</td>
        <td style="text-align:right">${(totals.input_tokens||0).toLocaleString("ru-RU")}</td>
        <td style="text-align:right">${(totals.output_tokens||0).toLocaleString("ru-RU")}</td>
        <td style="text-align:right">${(totals.total_tokens||0).toLocaleString("ru-RU")}</td>
      </tr>`;

    const tableRows = rows.length ? rows.map(r => `
      <tr>
        <td>${esc(r.log_date || "")}</td>
        <td style="text-align:right">${(r.requests||0).toLocaleString("ru-RU")}</td>
        <td style="text-align:right">${(r.input_tokens||0).toLocaleString("ru-RU")}</td>
        <td style="text-align:right">${(r.output_tokens||0).toLocaleString("ru-RU")}</td>
        <td style="text-align:right">${((r.input_tokens||0)+(r.output_tokens||0)).toLocaleString("ru-RU")}</td>
      </tr>`).join("") : '<tr><td colspan="5" style="color:#9ca3af;text-align:center">Данных нет — статистика накапливается при синхронизации отзывов</td></tr>';

    content.innerHTML = `
      <div style="max-height:280px;overflow-y:auto">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead>
            <tr style="background:#f8fafc;color:#64748b;font-size:12px">
              <th style="padding:8px;text-align:left;border-bottom:1px solid #e2e8f0">Дата</th>
              <th style="padding:8px;text-align:right;border-bottom:1px solid #e2e8f0">Запросов</th>
              <th style="padding:8px;text-align:right;border-bottom:1px solid #e2e8f0">Вход. токены</th>
              <th style="padding:8px;text-align:right;border-bottom:1px solid #e2e8f0">Исх. токены</th>
              <th style="padding:8px;text-align:right;border-bottom:1px solid #e2e8f0">Всего токенов</th>
            </tr>
          </thead>
          <tbody>
            ${totalRow}
            ${tableRows}
          </tbody>
        </table>
      </div>`;
  } catch (_) {
    content.innerHTML = '<p class="small" style="color:#dc2626">Ошибка загрузки статистики</p>';
  }
}

function closeAiStatsModal() {
  const modal = document.getElementById("aiStatsModal");
  if (modal) modal.classList.add("hidden");
  // Collapse request log details
  const det = document.getElementById("aiRequestLogDetails");
  if (det) det.removeAttribute("open");
  const arrow = document.getElementById("aiReqLogArrow");
  if (arrow) { arrow.style.transform = ""; arrow.textContent = "▶"; }
}

let _aiReqLogLoaded = false;
async function loadAiRequestLog() {
  const det = document.getElementById("aiRequestLogDetails");
  const arrow = document.getElementById("aiReqLogArrow");
  const content = document.getElementById("aiRequestLogContent");
  if (!det || !content) return;
  // Toggle arrow based on open state (details not yet toggled at this point)
  const willBeOpen = !det.hasAttribute("open");
  if (arrow) { arrow.style.transform = willBeOpen ? "rotate(90deg)" : ""; arrow.textContent = "▶"; }
  if (!willBeOpen) return; // collapsing — no fetch needed
  if (_aiReqLogLoaded) return; // already loaded
  _aiReqLogLoaded = true;
  content.innerHTML = '<p class="small" style="color:#9ca3af;padding:10px 14px">Загрузка...</p>';
  try {
    const res = await fetch("/api/admin/ai-request-log?limit=200");
    if (!res.ok) { content.innerHTML = '<p class="small" style="color:#dc2626;padding:10px 14px">Ошибка загрузки</p>'; return; }
    const data = await res.json();
    const logs = data.logs || [];
    if (!logs.length) {
      content.innerHTML = '<p class="small" style="color:#9ca3af;padding:10px 14px">Нет данных за последние 24 часа. Запросы появятся после синхронизации отзывов.</p>';
      return;
    }
    const rows = logs.map((log, idx) => {
      const dt = _fmtLogDate(log.created_at);
      const rating = log.review_rating ? `★${log.review_rating}` : "";
      const tokens = `${log.input_tokens||0}↑ ${log.output_tokens||0}↓`;
      const rowId = `aiReqRow${idx}`;
      const detId = `aiReqDet${idx}`;
      return `<div class="ai-req-row" onclick="toggleAiReqDetail('${detId}', this)" id="${rowId}" style="cursor:pointer;padding:6px 12px;border-bottom:1px solid #e2e8f0;display:flex;gap:10px;align-items:center;font-size:12px;user-select:none">
        <span style="color:#94a3b8;min-width:130px">${esc(dt)}</span>
        <span style="color:#64748b;min-width:36px">${rating}</span>
        <span style="color:#94a3b8;font-size:11px">${tokens}</span>
        <span style="margin-left:auto;color:#cbd5e1;font-size:11px">▼</span>
      </div>
      <div id="${detId}" class="ai-req-detail hidden" style="background:#fff;border-bottom:1px solid #e2e8f0;padding:10px 14px;font-size:11.5px;font-family:monospace;white-space:pre-wrap;word-break:break-word">
<b style="color:#475569">— СИСТЕМНЫЙ ПРОМПТ —</b>
${esc(log.prompt_system || "(пусто)")}

<b style="color:#475569">— ТЕКСТ ОТЗЫВА —</b>
${esc(log.prompt_user || "(пусто)")}

<b style="color:#475569">— ОТВЕТ ЯНДЕКСА —</b>
${esc(log.response_text || "(пусто)")}</div>`;
    }).join("");
    content.innerHTML = rows;
  } catch (_) {
    content.innerHTML = '<p class="small" style="color:#dc2626;padding:10px 14px">Ошибка загрузки</p>';
  }
}

function toggleAiReqDetail(detId, rowEl) {
  const det = document.getElementById(detId);
  if (!det) return;
  const arrow = rowEl ? rowEl.querySelector("span:last-child") : null;
  const isHidden = det.classList.contains("hidden");
  det.classList.toggle("hidden", !isHidden);
  if (arrow) arrow.textContent = isHidden ? "▲" : "▼";
}

function _fmtLogDate(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) + " МСК";
  } catch (_) { return iso; }
}

function openAiTestReviewModal() {
  const modal = document.getElementById("aiTestReviewModal");
  if (!modal) return;
  const textInput = document.getElementById("aiTestReviewText");
  const ratingInput = document.getElementById("aiTestReviewRating");
  const resultEl = document.getElementById("aiTestReviewResult");
  if (textInput) textInput.value = "";
  if (ratingInput) ratingInput.value = "";
  if (resultEl) {
    resultEl.textContent = "";
    resultEl.style.color = "";
    resultEl.classList.add("hidden");
  }
  adminState.aiTestReviewLoading = false;
  renderAiTestReviewLoadingState();
  modal.classList.remove("hidden");
  textInput?.focus();
}

function closeAiTestReviewModal() {
  const modal = document.getElementById("aiTestReviewModal");
  if (!modal) return;
  modal.classList.add("hidden");
}

function renderAiTestReviewLoadingState() {
  const submitBtn = document.getElementById("aiTestReviewSendBtn");
  const closeBtn = document.getElementById("aiTestReviewCloseBtn");
  const idsBtn = document.getElementById("aiTestReviewActualIdsBtn");
  const loading = Boolean(adminState.aiTestReviewLoading);
  if (submitBtn) {
    submitBtn.disabled = loading;
    submitBtn.textContent = loading ? "Отправляем..." : "Отправить";
  }
  if (closeBtn) {
    closeBtn.disabled = loading;
  }
  if (idsBtn) {
    idsBtn.disabled = loading || Boolean(adminState.aiActualIdsLoading);
  }
}

function _formatAiTestReviewResult(data) {
  const lines = [];
  lines.push(`Группа: ${String(data.group_title || data.group_id || "-")}`);
  lines.push(`ID группы: ${String(data.group_id || "-")}`);
  lines.push(`ID подгруппы: ${String(data.subgroup_id || "-")}`);
  lines.push(`Подгруппа: ${String(data.subgroup || "-")}`);
  lines.push(`Модель: ${String(data.model_uri || "-")}`);
  lines.push("");
  lines.push("Ответ YandexGPT:");
  lines.push(String(data.raw_response || "(пустой ответ)"));
  return lines.join("\n");
}

function _formatAiTestReviewErrorDetails(data) {
  if (!data || typeof data !== "object") return "";
  const details = data.details && typeof data.details === "object" ? data.details : {};
  const lines = [];
  const parsedGroupId = String(details.parsed_group_id || "").trim();
  const parsedSubgroupId = String(details.parsed_subgroup_id || "").trim();
  const parsedSubgroup = String(details.parsed_subgroup || "").trim();
  const modelUri = String(details.model_uri || "").trim();
  const rawResponse = String(details.raw_response || "").trim();
  const promptPreview = String(details.prompt_preview || "").trim();
  if (parsedGroupId || parsedSubgroupId || parsedSubgroup) {
    lines.push("Распознанный результат:");
    lines.push(`- group_id: ${parsedGroupId || "-"}`);
    lines.push(`- subgroup_id: ${parsedSubgroupId || "-"}`);
    lines.push(`- subgroup: ${parsedSubgroup || "-"}`);
    lines.push("");
  }
  if (modelUri) {
    lines.push(`modelUri: ${modelUri}`);
    lines.push("");
  }
  if (rawResponse) {
    lines.push("Сырой ответ YandexGPT:");
    lines.push(rawResponse);
    lines.push("");
  }
  if (promptPreview) {
    lines.push("Фрагмент отправленного prompt:");
    lines.push(promptPreview);
  }
  return lines.join("\n").trim();
}

function renderAiActualIdsLoadingState() {
  const idsBtn = document.getElementById("aiCurrentIdsBtn");
  const closeBtn = document.getElementById("aiCurrentIdsCloseBtn");
  const loading = Boolean(adminState.aiActualIdsLoading);
  if (idsBtn) {
    idsBtn.disabled = loading || Boolean(adminState.aiTestReviewLoading);
    idsBtn.textContent = loading ? "Загрузка..." : "Актуальные ID";
  }
  if (closeBtn) {
    closeBtn.disabled = loading;
  }
}

function closeAiCurrentIdsModal() {
  const modal = document.getElementById("aiCurrentIdsModal");
  if (!modal) return;
  modal.classList.add("hidden");
}

function _formatAiActualIdsPayload(data) {
  if (!data || typeof data !== "object") return "Нет данных.";
  const items = Array.isArray(data.items) ? data.items : [];
  if (!items.length) return "Список групп/подгрупп пуст.";
  const lines = [];
  for (const group of items) {
    const groupId = String(group.group_id || "").trim() || "-";
    const groupTitle = String(group.group_title || "").trim() || groupId;
    lines.push(`${groupTitle} (${groupId})`);
    const subgroupItems = Array.isArray(group.subgroup_items) ? group.subgroup_items : [];
    if (!subgroupItems.length) {
      lines.push("  - (нет подгрупп)");
      lines.push("");
      continue;
    }
    for (const subgroup of subgroupItems) {
      const subgroupId = String(subgroup.subgroup_id || "").trim() || "-";
      const subgroupTitle = String(subgroup.subgroup || "").trim() || "-";
      lines.push(`  - ${subgroupId}: ${subgroupTitle}`);
    }
    lines.push("");
  }
  return lines.join("\n").trim();
}

async function openAiCurrentIdsModal() {
  if (adminState.aiActualIdsLoading) return;
  const modal = document.getElementById("aiCurrentIdsModal");
  const contentEl = document.getElementById("aiCurrentIdsResult");
  if (!modal || !contentEl) return;
  adminState.aiActualIdsLoading = true;
  renderAiActualIdsLoadingState();
  contentEl.textContent = "Загружаем актуальные ID...";
  contentEl.style.color = "";
  modal.classList.remove("hidden");
  try {
    const res = await fetch("/api/admin/ai-settings/active-ids");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      contentEl.textContent = "Ошибка загрузки: " + (data.detail || data.error || "не удалось загрузить список ID");
      contentEl.style.color = "#b91c1c";
      return;
    }
    contentEl.textContent = _formatAiActualIdsPayload(data);
    contentEl.style.color = "#0f172a";
  } catch (_error) {
    contentEl.textContent = "Сетевая ошибка при загрузке актуальных ID.";
    contentEl.style.color = "#b91c1c";
  } finally {
    adminState.aiActualIdsLoading = false;
    renderAiActualIdsLoadingState();
  }
}

async function submitAiTestReview() {
  if (adminState.aiTestReviewLoading) return;
  const resultEl = document.getElementById("aiTestReviewResult");
  const textValue = String(document.getElementById("aiTestReviewText")?.value || "").trim();
  const ratingRaw = String(document.getElementById("aiTestReviewRating")?.value || "").trim();
  const folderId = String(document.getElementById("folderId")?.value || "").trim();
  const apiKey = adminState.aiEditMode ? String(document.getElementById("apiKey")?.value || "").trim() : "";
  if (!textValue) {
    if (resultEl) {
      resultEl.textContent = "Введите текст тестового отзыва.";
      resultEl.style.color = "#b91c1c";
      resultEl.classList.remove("hidden");
    }
    return;
  }
  const ratingNum = Number(ratingRaw);
  if (ratingRaw && (!Number.isInteger(ratingNum) || ratingNum < 1 || ratingNum > 5)) {
    if (resultEl) {
      resultEl.textContent = "Оценка должна быть целым числом от 1 до 5.";
      resultEl.style.color = "#b91c1c";
      resultEl.classList.remove("hidden");
    }
    return;
  }
  const payload = {
    review_text: textValue,
    review_rating: ratingRaw ? ratingNum : null,
    yandex_folder_id: folderId || null,
    yandex_api_key: apiKey || null,
  };
  adminState.aiTestReviewLoading = true;
  renderAiTestReviewLoadingState();
  if (resultEl) {
    resultEl.textContent = "Отправляем тестовый отзыв в Yandex GPT...";
    resultEl.style.color = "";
    resultEl.classList.remove("hidden");
  }
  try {
    const res = await fetch("/api/admin/ai-settings/test-review", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      if (resultEl) {
        const baseError = "Ошибка: " + (data.detail || data.error || "не удалось выполнить тестовый запрос");
        const debugInfo = _formatAiTestReviewErrorDetails(data);
        resultEl.textContent = debugInfo ? `${baseError}\n\n${debugInfo}` : baseError;
        resultEl.style.color = "#b91c1c";
        resultEl.classList.remove("hidden");
      }
      return;
    }
    if (resultEl) {
      resultEl.textContent = _formatAiTestReviewResult(data);
      resultEl.style.color = "#0f172a";
    }
  } catch (_error) {
    if (resultEl) {
      resultEl.textContent = "Сетевая ошибка при отправке тестового отзыва.";
      resultEl.style.color = "#b91c1c";
    }
  } finally {
    adminState.aiTestReviewLoading = false;
    renderAiTestReviewLoadingState();
  }
}

function getFilteredUsers() {
  const query = usersState.search.trim().toLowerCase();
  const source = Array.isArray(usersState.items) ? usersState.items : [];
  if (!query) return source;
  return source.filter((user) => String(user.email || "").toLowerCase().includes(query));
}

function usersSearchToggle(forceOpen) {
  const wrap = document.getElementById("usersSearchWrap");
  const input = document.getElementById("usersSearchEmail");
  if (!wrap || !input) return;
  const open = typeof forceOpen === "boolean" ? forceOpen : wrap.classList.contains("collapsed");
  wrap.classList.toggle("collapsed", !open);
  if (open) {
    input.focus();
  } else if (!usersState.search) {
    input.value = "";
  }
}

function renderUsers() {
  const tbody = document.getElementById("usersTbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const filtered = getFilteredUsers();
  const total = filtered.length;
  const pageSize = Math.max(1, Number(usersState.pageSize || 10));
  const pages = Math.max(1, Math.ceil(total / pageSize));
  usersState.page = Math.min(Math.max(1, usersState.page), pages);
  const start = (usersState.page - 1) * pageSize;
  const pageItems = filtered.slice(start, start + pageSize);
  const tariffOptions = (window.__AVAILABLE_PLANS__ || [])
    .map((plan) => {
      const code = String(plan.code || "");
      const title = String(plan.title || code);
      return `<option value="${esc(code)}">${esc(title)} (${esc(code)})</option>`;
    })
    .join("");
  if (!pageItems.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="8">Пользователи не найдены</td>`;
    tbody.appendChild(tr);
  }
  for (const user of pageItems) {
    const tr = document.createElement("tr");
    const passwordInputId = `password-input-${user.id}`;
    const planSelectId = `plan-select-${user.id}`;
    const blocked = Boolean(user.is_blocked);
    const saveIconTitle = "Сохранить изменения в строке";
    const blockIconTitle = blocked ? "Разблокировать пользователя" : "Заблокировать пользователя";
    const blockedCell = blocked
      ? `<span class="small status-badge status-blocked">заблокирован</span>`
      : `<span class="small status-badge status-active">активен</span>`;
    const subscriptionStatus = String(user.subscription_status || "inactive").toLowerCase();
    const subscriptionLabelMap = {
      active: "Активна",
      grace: "Льготный период",
      suspended: "Приостановлена",
      cancelled: "Отключена",
      inactive: "Не активирована",
    };
    const paidUntilRaw = String(user.subscription_paid_until || "").trim();
    const paidUntil = paidUntilRaw ? paidUntilRaw.slice(0, 10) : "-";
    tr.innerHTML = `
      <td>${esc(user.id)}</td>
      <td>${esc(user.email)}</td>
      <td>
        <select id="${planSelectId}">
          ${tariffOptions}
        </select>
      </td>
      <td>${esc(subscriptionLabelMap[subscriptionStatus] || subscriptionStatus || "Не активирована")}</td>
      <td>${esc(paidUntil)}</td>
      <td>
        <input id="${passwordInputId}" type="password" placeholder="Новый пароль" />
      </td>
      <td>${blockedCell}</td>
      <td>
        <div class="users-actions-row">
          <button class="icon-btn secondary" title="${esc(saveIconTitle)}" onclick="saveUserRow(${user.id})">💾</button>
          <button class="icon-btn secondary" title="${esc(blockIconTitle)}" onclick="toggleUserBlock(${user.id}, ${blocked ? "false" : "true"})">${blocked ? "🔓" : "🔒"}</button>
          <button class="icon-btn danger" title="Удалить пользователя" onclick="deleteUser(${user.id})">🗑</button>
        </div>
      </td>
    `;
    tbody.appendChild(tr);
    const planSelect = document.getElementById(planSelectId);
    if (planSelect) {
      planSelect.value = String(user.plan_code || "");
      if (!planSelect.value) {
        planSelect.value = "starter";
      }
    }
  }
  const pageInfo = document.getElementById("usersPaginationInfo");
  if (pageInfo) {
    pageInfo.textContent = `Страница ${usersState.page} из ${pages}. Всего клиентов: ${total}`;
  }
  const prevBtn = document.getElementById("usersPrevPageButton");
  if (prevBtn) prevBtn.disabled = usersState.page <= 1;
  const nextBtn = document.getElementById("usersNextPageButton");
  if (nextBtn) nextBtn.disabled = usersState.page >= pages;
}

async function loadUsers() {
  const usersRes = await fetch("/api/admin/users");
  const usersData = await usersRes.json();
  if (!usersRes.ok) {
    setUsersInfo(usersData.detail || "Не удалось загрузить пользователей", true);
    return;
  }
  let tariffs = [];
  if (isSuperAdmin()) {
    const tariffsRes = await fetch("/api/super-admin/tariffs");
    const tariffsData = await tariffsRes.json();
    if (!tariffsRes.ok) {
      setUsersInfo(tariffsData.detail || "Не удалось загрузить тарифы для пользователей", true);
      return;
    }
    tariffs = tariffsData.items || [];
  }
  usersState.items = usersData.items || [];
  window.__AVAILABLE_PLANS__ = tariffs;
  const newUserPlan = document.getElementById("newUserPlan");
  if (newUserPlan) {
    newUserPlan.innerHTML = "";
    const plans = (window.__AVAILABLE_PLANS__ || []).filter((plan) => Boolean(plan && String(plan.code || "").trim()));
    if (!plans.length) {
      const fallback = document.createElement("option");
      fallback.value = "";
      fallback.textContent = "Нет тарифов";
      newUserPlan.appendChild(fallback);
      newUserPlan.disabled = true;
    } else {
      newUserPlan.disabled = false;
    }
    for (const plan of plans) {
      const option = document.createElement("option");
      const code = String(plan.code || "");
      const isActive = Boolean(plan.is_active !== false);
      option.value = code;
      option.textContent = `${String(plan.title || code)} (${code})${isActive ? "" : " · неактивен"}`;
      newUserPlan.appendChild(option);
    }
    if (plans.length && !newUserPlan.value) newUserPlan.value = String(plans[0].code || "");
  }
  renderUsers();
}

function onUsersSearchInput(value) {
  usersState.search = String(value || "");
  usersState.page = 1;
  renderUsers();
}

function clearUsersSearch() {
  usersState.search = "";
  const input = document.getElementById("usersSearchEmail");
  if (input) input.value = "";
  usersSearchToggle(false);
  usersState.page = 1;
  renderUsers();
}

async function prevUsersPage() {
  if (usersState.page <= 1) return;
  usersState.page -= 1;
  renderUsers();
}

async function nextUsersPage() {
  const total = getFilteredUsers().length;
  const pages = Math.max(1, Math.ceil(total / usersState.pageSize));
  if (usersState.page >= pages) return;
  usersState.page += 1;
  renderUsers();
}

async function saveUserRow(userId) {
  const planCode = String(document.getElementById(`plan-select-${userId}`)?.value || "").trim().toLowerCase();
  const password = String(document.getElementById(`password-input-${userId}`)?.value || "");

  const actions = [];
  if (planCode) actions.push(setUserPlan(userId, planCode, { silent: true }));
  if (password) actions.push(setUserPassword(userId, password, { silent: true }));

  const results = await Promise.all(actions);
  if (results.some((ok) => ok === false)) {
    setUsersInfo("Не все изменения удалось сохранить. Проверьте данные строки.", true);
    return;
  }
  const passwordInput = document.getElementById(`password-input-${userId}`);
  if (passwordInput) passwordInput.value = "";
  setUsersInfo("Изменения пользователя сохранены.");
  await loadUsers();
}

async function setUserPlan(userId, planCode, options = {}) {
  const { silent = false } = options;
  const normalized = String(planCode || "").trim().toLowerCase();
  if (!normalized) {
    if (!silent) setUsersInfo("Выберите тарифный план.", true);
    return false;
  }
  const res = await fetch(`/api/admin/users/${userId}/plan`, {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ plan_code: normalized }),
  });
  const data = await res.json();
  if (!res.ok) {
    if (!silent) setUsersInfo(data.detail || "Ошибка смены тарифа пользователя", true);
    return false;
  }
  if (!silent) {
    setUsersInfo("Тариф пользователя обновлен.");
    await loadUsers();
  }
  return true;
}

async function toggleUserBlock(userId, blocked) {
  const reason = blocked ? prompt("Причина блокировки (необязательно):", "") : "";
  const payload = {
    blocked: Boolean(blocked),
    reason: blocked ? (reason || "").trim() : null,
  };
  const res = await fetch(`/api/admin/users/${userId}/block`, {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    setUsersInfo(data.detail || "Ошибка изменения статуса блокировки", true);
    return;
  }
  setUsersInfo(payload.blocked ? "Пользователь заблокирован." : "Пользователь разблокирован.");
  await loadUsers();
}

async function deleteUser(userId) {
  const confirmed = window.confirm("Удалить пользователя? Действие необратимо.");
  if (!confirmed) return;
  const res = await fetch(`/api/admin/users/${userId}/delete`, {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ confirm: true }),
  });
  const data = await res.json();
  if (!res.ok) {
    setUsersInfo(data.detail || "Ошибка удаления пользователя", true);
    return;
  }
  setUsersInfo("Пользователь удален.");
  await loadUsers();
}

async function createUser() {
  const emailInput = document.getElementById("newUserEmail");
  const passwordInput = document.getElementById("newUserPassword");
  const roleInput = document.getElementById("newUserRole");
  const planInput = document.getElementById("newUserPlan");
  let selectedPlan = String(planInput?.value || "").trim().toLowerCase();
  if (!selectedPlan) {
    const plans = (window.__AVAILABLE_PLANS__ || []).filter((plan) => Boolean(plan && String(plan.code || "").trim()));
    if (plans.length) {
      selectedPlan = String(plans[0].code || "").trim().toLowerCase();
    } else {
      selectedPlan = "starter";
    }
  }
  const payload = {
    email: String(emailInput?.value || "").trim(),
    password: String(passwordInput?.value || ""),
    role: isSuperAdmin() ? "user" : String(roleInput?.value || "feedback_manager"),
    plan_code: selectedPlan,
  };
  if (!payload.email || !payload.password) {
    setUsersInfo("Заполните эл. почту и пароль нового пользователя.", true);
    return;
  }
  const res = await fetch("/api/admin/users", {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  let data = {};
  try {
    data = await res.json();
  } catch (error) {
    data = {};
  }
  if (!res.ok) {
    setUsersInfo(
      data.detail || `Ошибка создания пользователя (HTTP ${res.status}). Проверьте корректность тарифа и данных.`,
      true,
    );
    return;
  }
  if (emailInput) emailInput.value = "";
  if (passwordInput) passwordInput.value = "";
  if (roleInput) roleInput.value = "user";
  if (planInput && planInput.options.length > 0) planInput.selectedIndex = 0;
  setUsersInfo("Пользователь создан.");
  await loadUsers();
}

async function setUserPassword(userId, password, options = {}) {
  const { silent = false } = options;
  const cleanPassword = String(password || "");
  if (!cleanPassword) {
    if (!silent) setUsersInfo("Введите новый пароль для выбранного пользователя.", true);
    return false;
  }
  const res = await fetch(`/api/admin/users/${userId}/password`, {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ password: cleanPassword }),
  });
  const data = await res.json();
  if (!res.ok) {
    if (!silent) setUsersInfo(data.detail || "Ошибка смены пароля", true);
    return false;
  }
  if (!silent) {
    const input = document.getElementById(`password-input-${userId}`);
    if (input) input.value = "";
    setUsersInfo("Пароль пользователя обновлен.");
  }
  return true;
}

function toggleUsersSearch(forceOpen) {
  usersSearchToggle(forceOpen);
}

function setSuperAdminInfo(message, isError = false) {
  const info = document.getElementById("tariffsInfo");
  if (!info) return;
  info.textContent = message || "";
  info.style.color = isError ? "#b91c1c" : "";
}

function setDefaultTemplatesInfo(message, isError = false) {
  const info = document.getElementById("defaultTemplatesInfo");
  if (!info) return;
  info.textContent = message || "";
  info.style.color = isError ? "#b91c1c" : "";
}

function setDefaultTemplateBulkInfo(message, isError = false) {
  const info = document.getElementById("defaultTemplateBulkInfo");
  if (!info) return;
  info.textContent = message || "";
  info.style.color = isError ? "#b91c1c" : "";
}

function _defaultTemplateGroupOptions() {
  return (defaultTemplatesState.items || [])
    .map((group) => ({
      id: String(group?.id || "").trim(),
      title: String(group?.title || "").trim(),
      subgroups: Array.isArray(group?.subgroups) ? group.subgroups : [],
    }))
    .filter((group) => group.id);
}

function _selectedBulkGroup() {
  return _defaultTemplateGroupOptions().find((group) => group.id === defaultTemplateBulkState.groupId) || null;
}

function _selectedBulkSubgroupExists(group, subgroupName) {
  if (!group || !Array.isArray(group.subgroups)) return false;
  const target = String(subgroupName || "").trim();
  if (!target) return false;
  return group.subgroups.some((item) => String(item?.name || "").trim() === target);
}

function renderDefaultTemplateBulkSelectors() {
  const groupSelect = document.getElementById("defaultTemplateBulkGroup");
  const subgroupSelect = document.getElementById("defaultTemplateBulkSubgroup");
  if (!groupSelect || !subgroupSelect) return;

  const groups = _defaultTemplateGroupOptions();
  groupSelect.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Выберите группу";
  groupSelect.appendChild(placeholder);
  for (const group of groups) {
    const option = document.createElement("option");
    option.value = group.id;
    option.textContent = group.title || group.id;
    groupSelect.appendChild(option);
  }
  if (!groups.some((group) => group.id === defaultTemplateBulkState.groupId)) {
    defaultTemplateBulkState.groupId = "";
  }
  groupSelect.value = defaultTemplateBulkState.groupId;

  const selectedGroup = _selectedBulkGroup();
  const subgroupItems = selectedGroup ? selectedGroup.subgroups : [];
  if (!selectedGroup || !_selectedBulkSubgroupExists(selectedGroup, defaultTemplateBulkState.subgroup)) {
    defaultTemplateBulkState.subgroup = "";
  }

  subgroupSelect.innerHTML = "";
  const subgroupPlaceholder = document.createElement("option");
  subgroupPlaceholder.value = "";
  subgroupPlaceholder.textContent = selectedGroup ? "Выберите подгруппу" : "Сначала выберите группу";
  subgroupSelect.appendChild(subgroupPlaceholder);
  for (const item of subgroupItems) {
    const subgroupName = String(item?.name || "").trim();
    if (!subgroupName) continue;
    const option = document.createElement("option");
    option.value = subgroupName;
    option.textContent = subgroupName;
    subgroupSelect.appendChild(option);
  }
  subgroupSelect.disabled = !selectedGroup;
  subgroupSelect.value = defaultTemplateBulkState.subgroup;
}

function openDefaultTemplateBulkModal() {
  const modal = document.getElementById("defaultTemplateBulkModal");
  if (!modal) return;
  setDefaultTemplateBulkInfo("");
  const textarea = document.getElementById("defaultTemplateBulkText");
  if (textarea) textarea.value = "";
  renderDefaultTemplateBulkSelectors();
  modal.classList.remove("hidden");
}

function closeDefaultTemplateBulkModal() {
  const modal = document.getElementById("defaultTemplateBulkModal");
  if (!modal) return;
  modal.classList.add("hidden");
}

function onDefaultTemplateBulkGroupChange(value) {
  defaultTemplateBulkState.groupId = String(value || "").trim();
  defaultTemplateBulkState.subgroup = "";
  renderDefaultTemplateBulkSelectors();
}

function onDefaultTemplateBulkSubgroupChange(value) {
  defaultTemplateBulkState.subgroup = String(value || "").trim();
}

function _parseBulkTemplates(rawText) {
  const text = String(rawText || "").replaceAll("\r\n", "\n").trim();
  if (!text) return [];
  const chunks = text
    .split(/\n\s*---\s*\n/g)
    .map((item) => item.trim())
    .filter(Boolean);
  const unique = [];
  const seen = new Set();
  for (const item of chunks) {
    if (seen.has(item)) continue;
    seen.add(item);
    unique.push(item);
  }
  return unique;
}

async function submitDefaultTemplateBulk() {
  const groupId = String(defaultTemplateBulkState.groupId || "").trim();
  const subgroup = String(defaultTemplateBulkState.subgroup || "").trim();
  const textarea = document.getElementById("defaultTemplateBulkText");
  if (!groupId) {
    setDefaultTemplateBulkInfo("Выберите группу.", true);
    return;
  }
  if (!subgroup) {
    setDefaultTemplateBulkInfo("Выберите подгруппу.", true);
    return;
  }
  const templates = _parseBulkTemplates(textarea?.value || "");
  if (!templates.length) {
    setDefaultTemplateBulkInfo("Введите хотя бы один шаблон. Разделяйте шаблоны строкой ---", true);
    return;
  }
  const res = await fetch("/api/super-admin/default-template-subgroup/bulk-import", {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ group_id: groupId, subgroup, templates }),
  });
  const data = await res.json();
  if (!res.ok) {
    setDefaultTemplateBulkInfo(data.detail || "Не удалось выполнить экспорт.", true);
    return;
  }
  setDefaultTemplateBulkInfo(`Экспорт завершен: добавлено ${Number(data.added || templates.length)} шаблон(ов).`);
  await loadDefaultTemplateGroups();
  if (
    defaultTemplatesState.currentGroupId === groupId &&
    defaultTemplatesState.currentSubgroup === subgroup
  ) {
    await openDefaultTemplateSubgroup(groupId, subgroup, defaultTemplatesState.currentGroupTitle || "");
  }
}

function setTemplateVariablesInfo(message, isError = false) {
  const info = document.getElementById("templateVariablesInfo");
  if (!info) return;
  info.textContent = message || "";
  info.style.color = isError ? "#b91c1c" : "";
}

function normalizeTemplateVariableKey(value) {
  const raw = String(value || "").trim().toUpperCase();
  if (!raw) return "";
  if (/^%[A-Z0-9_]{2,50}%$/.test(raw)) return raw;
  return "";
}

function onTemplateVarSourceChange(value) {
  // No-op: kept for compatibility
}

function _sourcePathToSelectValue(sourceType, sourcePath, defaultValue) {
  if (sourceType === "manual") return "manual:";
  if (sourceType === "review_field" || sourceType === "system") {
    const path = String(sourcePath || "").trim();
    if (path === "author_name" || path === "author" || path === "author_name_ozon") {
      return `review_field:${path}`;
    }
    return `review_field:${path}`;
  }
  return "";
}

function fillTemplateVariableForm(item = null) {
  const payload = item && typeof item === "object" ? item : {};
  const varKeyInput = document.getElementById("templateVarKey");
  if (varKeyInput) varKeyInput.value = String(payload.var_key || "");
  const titleInput = document.getElementById("templateVarTitle");
  if (titleInput) titleInput.value = String(payload.title || "");
  // hidden fields
  const descriptionInput = document.getElementById("templateVarDescription");
  if (descriptionInput) descriptionInput.value = String(payload.description || "");
  const sourceTypeInput = document.getElementById("templateVarSourceType");
  if (sourceTypeInput) sourceTypeInput.value = String(payload.source_type || "manual");
  const defaultValueInput = document.getElementById("templateVarDefaultValue");
  if (defaultValueInput) defaultValueInput.value = String(payload.default_value || "");
  const manualInput = document.getElementById("templateVarManualValue");
  const dVal = String(payload.default_value || "");
  if (manualInput) manualInput.value = dVal;
  const userEditableInput = document.getElementById("templateVarUserEditable");
  if (userEditableInput) userEditableInput.checked = Boolean(payload.is_user_editable);
  const activeInput = document.getElementById("templateVarIsActive");
  if (activeInput) activeInput.checked = payload.is_active !== false;
  templateVariablesState.editKey = payload.var_key ? String(payload.var_key).toUpperCase() : null;
}

function resetTemplateVariableForm() {
  fillTemplateVariableForm(null);
}

function renderTemplateVariablesList() {
  const tbody = document.getElementById("templateVariablesTbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const items = Array.isArray(templateVariablesState.items) ? templateVariablesState.items : [];
  if (!items.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="8" class="small">Переменные пока не настроены</td>`;
    tbody.appendChild(tr);
    return;
  }
  for (const item of items) {
    const varKey = String(item.var_key || "");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(varKey)}</td>
      <td>${esc(item.title || "")}</td>
      <td>${esc(item.default_value || "—")}</td>
      <td>${item.is_user_editable ? "да" : "нет"}</td>
      <td>${item.is_active ? "да" : "нет"}</td>
      <td>
        <div class="row">
          <button class="secondary" type="button" onclick="editTemplateVariableByKey('${esc(varKey)}')">Изменить</button>
          <button class="secondary danger" type="button" onclick="deleteTemplateVariable('${esc(varKey)}')">Удалить</button>
        </div>
      </td>
    `;
    tbody.appendChild(tr);
  }
}

function editTemplateVariableByKey(varKey) {
  const key = String(varKey || "").trim().toUpperCase();
  if (!key) return;
  const item = (templateVariablesState.items || []).find(
    (row) => String(row.var_key || "").trim().toUpperCase() === key,
  );
  if (!item) return;
  fillTemplateVariableForm(item);
  setTemplateVariablesInfo("");
}

async function loadTemplateVariables() {
  if (!isSuperAdmin()) return;
  const res = await fetch("/api/super-admin/template-variables");
  const data = await res.json();
  if (!res.ok) {
    setTemplateVariablesInfo(data.detail || "Не удалось загрузить переменные шаблонов", true);
    return;
  }
  templateVariablesState.items = data.items || [];
  renderTemplateVariablesList();
  setTemplateVariablesInfo("");
}

async function saveTemplateVariable() {
  if (!isSuperAdmin()) return;
  // All custom variables are manual type with a fixed value
  const sourceTypeValue = "manual";
  const sourcePathValue = "";
  const defaultValueValue = String(document.getElementById("templateVarManualValue")?.value || "").trim();
  const keyValue = normalizeTemplateVariableKey(document.getElementById("templateVarKey")?.value || "");
  const payload = {
    var_key: keyValue,
    title: String(document.getElementById("templateVarTitle")?.value || "").trim(),
    description: null,
    is_user_editable: Boolean(document.getElementById("templateVarUserEditable")?.checked),
    source_type: sourceTypeValue,
    source_path: sourcePathValue || null,
    default_value: defaultValueValue || null,
    is_active: Boolean(document.getElementById("templateVarIsActive")?.checked ?? true),
  };
  if (!payload.var_key) {
    setTemplateVariablesInfo("Ключ должен быть в формате %KEY% (только A-Z, 0-9, _; длина 2-50).", true);
    return;
  }
  if (!payload.title) {
    setTemplateVariablesInfo("Заполните название переменной.", true);
    return;
  }
  if (payload.source_type === "review_field" && !payload.source_path) {
    setTemplateVariablesInfo("Для источника «из отзыва» укажите поле source_path.", true);
    return;
  }
  if (payload.source_type === "system" && !payload.source_path) {
    setTemplateVariablesInfo("Для системного источника укажите source_path.", true);
    return;
  }
  const res = await fetch("/api/super-admin/template-variables", {
    method: "PUT",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    setTemplateVariablesInfo(data.detail || "Не удалось сохранить переменную", true);
    return;
  }
  setTemplateVariablesInfo("Переменная сохранена.");
  resetTemplateVariableForm();
  await loadTemplateVariables();
}

async function deleteTemplateVariable(varKey) {
  if (!isSuperAdmin()) return;
  const normalizedKey = String(varKey || "").trim().toUpperCase();
  if (!normalizedKey) return;
  if (!confirm(`Удалить переменную ${normalizedKey}?`)) return;
  const res = await fetch("/api/super-admin/template-variables", {
    method: "DELETE",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ var_key: normalizedKey }),
  });
  const data = await res.json();
  if (!res.ok) {
    setTemplateVariablesInfo(data.detail || "Не удалось удалить переменную", true);
    return;
  }
  setTemplateVariablesInfo("Переменная удалена.");
  await loadTemplateVariables();
}

function renderDefaultTemplateGroups() {
  const container = document.getElementById("defaultTemplateGroupsAccordion");
  if (!container) return;
  container.innerHTML = "";
  for (const group of defaultTemplatesState.items || []) {
    const details = document.createElement("details");
    details.className = "template-group";
    details.open = false;
    const summary = document.createElement("summary");
    summary.textContent = String(group.title || "");
    details.appendChild(summary);
    const content = document.createElement("div");
    content.className = "template-subgroups-list";
    for (const subgroup of group.subgroups || []) {
      const row = document.createElement("div");
      row.className = "template-subgroup-row";
      const openButton = document.createElement("button");
      openButton.type = "button";
      openButton.className = "template-subgroup-open-btn";
      const nameSpan = document.createElement("span");
      nameSpan.textContent = String(subgroup.name || "");
      const countSpan = document.createElement("span");
      countSpan.className = "template-count-badge";
      countSpan.textContent = String(subgroup.count || 0);
      openButton.appendChild(nameSpan);
      openButton.appendChild(countSpan);
      openButton.addEventListener("click", () => {
        openDefaultTemplateSubgroup(String(group.id || ""), String(subgroup.name || ""), String(group.title || ""));
      });
      row.appendChild(openButton);
      const isProtectedGeneralSubgroup =
        ["positive", "product_dissatisfaction", "delivery_problems", "wrong_size", "tagged_reviews"].includes(
          String(group.id || "")
        ) && String(subgroup.name || "") === "Общий";
      const _PROTECTED_TEXTLESS = ["1 звезда", "2 звезды", "3 звезды", "4 звезды", "5 звезд", "1-3 звезды", "4-5 звезд"];
      const isProtectedTextlessSubgroup =
        String(group.id || "") === "textless_ratings" &&
        _PROTECTED_TEXTLESS.includes(String(subgroup.name || ""));
      const editButton = document.createElement("button");
      editButton.type = "button";
      editButton.className = "icon-btn modern-icon-btn template-subgroup-edit-btn";
      editButton.title = "Переименовать подгруппу";
      editButton.setAttribute("aria-label", "Переименовать подгруппу");
      editButton.innerHTML = `
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M4 20h4.3l9.95-9.95a1.5 1.5 0 0 0 0-2.12l-2.18-2.18a1.5 1.5 0 0 0-2.12 0L4 15.7V20Zm2-3.47 9.36-9.36 1.47 1.47L7.47 18H6v-1.47Z"/>
        </svg>
      `;
      if (!(isProtectedGeneralSubgroup || isProtectedTextlessSubgroup)) {
        editButton.addEventListener("click", async () => {
          await renameDefaultTemplateSubgroup(
            String(group.id || ""),
            String(subgroup.name || ""),
            String(group.title || "")
          );
        });
      }
      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "icon-btn danger modern-icon-btn template-subgroup-delete-btn";
      deleteButton.title = "Удалить группу";
      deleteButton.setAttribute("aria-label", "Удалить подгруппу");
      deleteButton.innerHTML = `
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M9 3h6a2 2 0 0 1 2 2v1h3v2h-1v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V8H4V6h3V5a2 2 0 0 1 2-2Zm1 3h4V5h-4v1Zm-3 2v11h10V8H7Zm3 2h2v7h-2v-7Zm4 0h2v7h-2v-7Z"/>
        </svg>
      `;
      if (!(isProtectedTextlessSubgroup || isProtectedGeneralSubgroup)) {
        deleteButton.addEventListener("click", async () => {
          await deleteDefaultTemplateSubgroup(String(group.id || ""), String(subgroup.name || ""), String(group.title || ""));
        });
      }
      const actions = document.createElement("div");
      actions.className = "template-subgroup-actions";
      if (!(isProtectedGeneralSubgroup || isProtectedTextlessSubgroup)) {
        actions.appendChild(editButton);
      }
      if (!(isProtectedTextlessSubgroup || isProtectedGeneralSubgroup)) {
        actions.appendChild(deleteButton);
      }
      row.appendChild(actions);
      content.appendChild(row);
    }
    const addRow = document.createElement("div");
    addRow.className = "template-subgroup-add-row";
    const addButton = document.createElement("button");
    addButton.type = "button";
    addButton.className = "secondary";
    addButton.textContent = "Добавить группу";
    addButton.addEventListener("click", async () => {
      await createDefaultTemplateSubgroup(String(group.id || ""), String(group.title || ""));
    });
    addRow.appendChild(addButton);
    content.appendChild(addRow);
    details.appendChild(content);
    container.appendChild(details);
  }
}

async function createDefaultTemplateSubgroup(groupId, groupTitle) {
  if (!groupId) return;
  const name = prompt(`Новая группа для категории "${groupTitle}"`);
  const subgroup = String(name || "").trim();
  if (!subgroup) return;
  const res = await fetch("/api/super-admin/default-template-subgroup", {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ group_id: groupId, subgroup }),
  });
  const data = await res.json();
  if (!res.ok) {
    setDefaultTemplatesInfo(data.detail || "Не удалось добавить группу", true);
    return;
  }
  setDefaultTemplatesInfo("Группа добавлена.");
  await loadDefaultTemplateGroups();
  await openDefaultTemplateSubgroup(groupId, subgroup, groupTitle);
}

async function deleteDefaultTemplateSubgroup(groupId, subgroup, groupTitle) {
  if (!groupId || !subgroup) return;
  const confirmed = confirm(`Удалить группу "${subgroup}" в категории "${groupTitle}"?`);
  if (!confirmed) return;
  const query = new URLSearchParams({ group_id: groupId, subgroup });
  const res = await fetch("/api/super-admin/default-template-subgroup?" + query.toString(), {
    method: "DELETE",
    headers: csrfHeaders(),
  });
  const data = await res.json();
  if (!res.ok) {
    setDefaultTemplatesInfo(data.detail || "Не удалось удалить группу", true);
    return;
  }
  setDefaultTemplatesInfo("Группа удалена.");
  if (
    defaultTemplatesState.currentGroupId === groupId &&
    defaultTemplatesState.currentSubgroup === subgroup
  ) {
    closeDefaultTemplateEditor();
  }
  await loadDefaultTemplateGroups();
}

async function renameDefaultTemplateSubgroup(groupId, subgroup, groupTitle) {
  if (!groupId || !subgroup) return;
  const nextNameRaw = prompt(`Новое название подгруппы "${subgroup}" в категории "${groupTitle}"`, subgroup);
  const nextName = String(nextNameRaw || "").trim();
  if (!nextName || nextName === subgroup) return;
  const res = await fetch("/api/super-admin/default-template-subgroup", {
    method: "PATCH",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ group_id: groupId, subgroup, new_subgroup: nextName }),
  });
  const data = await res.json();
  if (!res.ok) {
    setDefaultTemplatesInfo(data.detail || "Не удалось переименовать подгруппу", true);
    return;
  }
  setDefaultTemplatesInfo("Подгруппа переименована.");
  await loadDefaultTemplateGroups();
  if (
    defaultTemplatesState.currentGroupId === groupId &&
    defaultTemplatesState.currentSubgroup === subgroup
  ) {
    await openDefaultTemplateSubgroup(groupId, nextName, groupTitle);
  }
}

function renderDefaultTemplateEditorRows() {
  const container = document.getElementById("defaultTemplateEditorList");
  if (!container) return;
  container.innerHTML = "";
  if (!defaultTemplatesState.currentTemplates.length) {
    const empty = document.createElement("div");
    empty.className = "small";
    empty.textContent = "В этой подгруппе пока нет шаблонов.";
    container.appendChild(empty);
    return;
  }
  defaultTemplatesState.currentTemplates.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "template-editor-row";
    const textarea = document.createElement("textarea");
    textarea.className = "template-editor-input";
    textarea.value = item.text;
    textarea.placeholder = "Введите текст шаблона ответа";
    textarea.addEventListener("input", () => {
      defaultTemplatesState.currentTemplates[index].text = textarea.value;
    });
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "icon-btn danger";
    delBtn.title = "Удалить шаблон";
    delBtn.textContent = "🗑";
    delBtn.addEventListener("click", async () => {
      const itemId = defaultTemplatesState.currentTemplates[index]?.id;
      if (itemId) {
        await fetch(`/api/super-admin/default-template-subgroup/item/${itemId}`, {
          method: "DELETE",
          headers: csrfHeaders(),
        });
      }
      defaultTemplatesState.currentTemplates.splice(index, 1);
      renderDefaultTemplateEditorRows();
    });
    row.appendChild(textarea);
    row.appendChild(delBtn);
    container.appendChild(row);
  });
}

function closeDefaultTemplateEditor() {
  document.getElementById("defaultTemplateEditorView")?.classList.add("hidden");
  document.getElementById("defaultTemplateGroupsView")?.classList.remove("hidden");
  defaultTemplatesState.currentGroupId = null;
  defaultTemplatesState.currentGroupTitle = "";
  defaultTemplatesState.currentSubgroup = "";
  defaultTemplatesState.currentTemplates = [];
}

function addDefaultTemplateEditorRow() {
  defaultTemplatesState.currentTemplates.push({ id: null, text: "" });
  renderDefaultTemplateEditorRows();
}

async function loadDefaultTemplateGroups() {
  if (!isSuperAdmin()) return;
  const res = await fetch("/api/super-admin/default-template-groups");
  const data = await res.json();
  if (!res.ok) {
    setDefaultTemplatesInfo(data.detail || "Не удалось загрузить шаблоны по умолчанию", true);
    return;
  }
  defaultTemplatesState.items = data.items || [];
  renderDefaultTemplateGroups();
  renderDefaultTemplateBulkSelectors();
}

async function loadSuperAdminDefaultTemplateGroups() {
  closeDefaultTemplateEditor();
  await loadDefaultTemplateGroups();
}

async function openDefaultTemplateSubgroup(groupId, subgroup, groupTitle) {
  const query = new URLSearchParams({ group_id: groupId, subgroup });
  const res = await fetch("/api/super-admin/default-template-subgroup?" + query.toString());
  const data = await res.json();
  if (!res.ok) {
    setDefaultTemplatesInfo(data.detail || "Не удалось загрузить шаблоны подгруппы", true);
    return;
  }
  defaultTemplatesState.currentGroupId = groupId;
  defaultTemplatesState.currentGroupTitle = groupTitle;
  defaultTemplatesState.currentSubgroup = subgroup;
  defaultTemplatesState.currentTemplates = (data.items || []).map((item) => ({
    id: item.id || null,
    text: String(item.template_text || ""),
  }));
  document.getElementById("defaultTemplateGroupsView")?.classList.add("hidden");
  document.getElementById("defaultTemplateEditorView")?.classList.remove("hidden");
  const title = document.getElementById("defaultTemplateEditorTitle");
  if (title) title.textContent = `${groupTitle} / ${subgroup}`;
  setDefaultTemplatesInfo("");
  renderDefaultTemplateEditorRows();
}

async function saveDefaultTemplateSubgroup() {
  if (!defaultTemplatesState.currentGroupId || !defaultTemplatesState.currentSubgroup) return;
  const payload = {
    templates: defaultTemplatesState.currentTemplates.map((item) => String(item.text || "")),
  };
  const query = new URLSearchParams({
    group_id: defaultTemplatesState.currentGroupId,
    subgroup: defaultTemplatesState.currentSubgroup,
  });
  const res = await fetch("/api/super-admin/default-template-subgroup?" + query.toString(), {
    method: "PUT",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    setDefaultTemplatesInfo(data.detail || "Не удалось сохранить шаблоны по умолчанию", true);
    return;
  }
  setDefaultTemplatesInfo("Шаблоны по умолчанию сохранены.");
  await loadDefaultTemplateGroups();
  await openDefaultTemplateSubgroup(
    defaultTemplatesState.currentGroupId,
    defaultTemplatesState.currentSubgroup,
    defaultTemplatesState.currentGroupTitle,
  );
}

function formatTariffLimits(limits) {
  const normalized = limits && typeof limits === "object" ? limits : {};
  return [
    `Отзывы/мес: ${Number(normalized.reviews_per_month || 0)}`,
    `Менеджеры: ${Number(normalized.managers || 0)}`,
    `Источники: ${Number(normalized.sources || 0)}`,
    `AI-единицы: ${Number(normalized.ai_units || 0)}`,
  ].join(" · ");
}

function renderTariffs(items) {
  const list = document.getElementById("tariffsList");
  if (!list) return;
  list.innerHTML = "";
  const normalizedItems = Array.isArray(items) ? items : [];
  if (!normalizedItems.length) {
    const row = document.createElement("div");
    row.className = "tariff-item";
    row.innerHTML = `<div class="small">Тарифы пока не добавлены</div>`;
    list.appendChild(row);
    return;
  }
  for (const item of normalizedItems) {
    const row = document.createElement("div");
    row.className = "tariff-item";
    const main = document.createElement("div");
    main.className = "tariff-item-main";
    main.innerHTML = `
      <div class="tariff-item-title">${esc(item.title)} <span class="small">(${esc(item.code)})</span></div>
      <div class="small">Цена в месяц: ${esc(item.monthly_price)} ₽</div>
      <div class="small">${esc(formatTariffLimits(item.limits || {}))}</div>
      <div class="small">Статус: ${item.is_active ? "активен" : "неактивен"}</div>
    `;
    const actions = document.createElement("div");
    actions.className = "tariff-item-actions";
    const editButton = document.createElement("button");
    editButton.className = "secondary";
    editButton.type = "button";
    editButton.textContent = "Изменить";
    editButton.addEventListener("click", () => openTariffForm("edit", item));
    const deleteButton = document.createElement("button");
    deleteButton.className = "secondary danger";
    deleteButton.type = "button";
    deleteButton.textContent = "Удалить";
    deleteButton.addEventListener("click", () => deleteTariffPlan(String(item.code || "")));
    actions.appendChild(editButton);
    actions.appendChild(deleteButton);
    row.appendChild(main);
    row.appendChild(actions);
    list.appendChild(row);
  }
}

function renderPayments(items) {
  const tbody = document.getElementById("paymentsTbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  for (const item of items || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(item.id)}</td>
      <td>${esc(item.owner_user_id)}</td>
      <td>${esc(item.amount)} ${esc(item.currency || "RUB")}</td>
      <td>${esc(item.status)}</td>
      <td>${esc(item.created_at || "")}</td>
      <td><button class="secondary danger" type="button" onclick="deletePaymentRecord(${Number(item.id || 0)})">Удалить</button></td>
    `;
    tbody.appendChild(tr);
  }
}

async function loadSuperAdminSection() {
  if (!isSuperAdmin()) return;
  const [tariffsRes, paymentsRes, variablesRes] = await Promise.all([
    fetch("/api/super-admin/tariffs"),
    fetch("/api/super-admin/payments?limit=100"),
    fetch("/api/super-admin/template-variables"),
  ]);
  const tariffsData = await tariffsRes.json();
  const paymentsData = await paymentsRes.json();
  const variablesData = await variablesRes.json();
  if (!tariffsRes.ok || !paymentsRes.ok || !variablesRes.ok) {
    setSuperAdminInfo(
      tariffsData.detail || paymentsData.detail || variablesData.detail || "Ошибка загрузки данных супер-админа",
      true,
    );
    return;
  }
  renderTariffs(tariffsData.items || []);
  renderPayments(paymentsData.items || []);
  templateVariablesState.items = variablesData.items || [];
  renderTemplateVariablesList();
  setTemplateVariablesInfo("");
  await loadDefaultTemplateGroups();
}

async function saveTariffPlan() {
  if (!isSuperAdmin()) return;
  const code = String(document.getElementById("tariffCode")?.value || "").trim().toLowerCase();
  const title = String(document.getElementById("tariffTitle")?.value || "").trim();
  const monthlyPrice = Number(document.getElementById("tariffPrice")?.value || "0");
  const limits = tariffLimitsFromFields();
  if (!code || !title) {
    setSuperAdminInfo("Заполните код и название тарифа.", true);
    return;
  }
  if (!Number.isFinite(monthlyPrice) || monthlyPrice < 0) {
    setSuperAdminInfo("Цена в месяц должна быть числом не меньше 0.", true);
    return;
  }
  const originalCode = tariffEditorState.originalCode;
  if (tariffEditorState.mode === "edit" && originalCode && originalCode !== code) {
    const deleted = await deleteTariffPlan(originalCode, false);
    if (!deleted) {
      setSuperAdminInfo("Не удалось изменить код тарифа: старый тариф не удален.", true);
      return;
    }
  }
  const payload = {
    code,
    title,
    monthly_price: monthlyPrice,
    limits,
    is_active: Boolean(document.getElementById("tariffIsActive")?.checked ?? true),
  };
  const res = await fetch("/api/super-admin/tariffs", {
    method: "PUT",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    setSuperAdminInfo(data.detail || "Ошибка сохранения тарифа", true);
    return;
  }
  setSuperAdminInfo(tariffEditorState.mode === "edit" ? "Тариф изменен." : "Тариф создан.");
  closeTariffForm();
  await loadSuperAdminSection();
}

async function deleteTariffPlan(code, showMessage = true) {
  if (!isSuperAdmin()) return;
  const normalizedCode = String(code || "").trim().toLowerCase();
  if (!normalizedCode) return false;
  if (showMessage) {
    const confirmed = confirm(`Удалить тариф "${normalizedCode}"?`);
    if (!confirmed) return false;
  }
  const res = await fetch("/api/super-admin/tariffs", {
    method: "DELETE",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ code: normalizedCode }),
  });
  const data = await res.json();
  if (!res.ok) {
    if (showMessage) setSuperAdminInfo(data.detail || "Не удалось удалить тариф", true);
    return false;
  }
  if (showMessage) setSuperAdminInfo("Тариф удален.");
  await loadSuperAdminSection();
  return true;
}

function loadTariffs() {
  if (!isSuperAdmin()) return;
  openCreateTariffForm();
  document.getElementById("tariffsBlock")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function loadPayments() {
  document.getElementById("paymentsBlock")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function addPaymentRecord() {
  if (!isSuperAdmin()) return;
  const ownerUserId = Number(document.getElementById("paymentOwnerId")?.value || "0");
  const amount = Number(document.getElementById("paymentAmount")?.value || "0");
  const status = String(document.getElementById("paymentStatus")?.value || "paid").trim().toLowerCase();
  if (!ownerUserId || !amount) {
    setSuperAdminInfo("Укажите ID владельца и сумму.", true);
    return;
  }
  const res = await fetch("/api/super-admin/payments", {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      owner_user_id: ownerUserId,
      amount,
      currency: "RUB",
      status,
      details: {},
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    setSuperAdminInfo(data.detail || "Ошибка добавления оплаты", true);
    return;
  }
  setSuperAdminInfo("Платеж добавлен.");
  await loadSuperAdminSection();
}

async function deletePaymentRecord(paymentId) {
  if (!isSuperAdmin()) return;
  const normalizedId = Number(paymentId || 0);
  if (!Number.isInteger(normalizedId) || normalizedId <= 0) {
    setSuperAdminInfo("Некорректный идентификатор платежа.", true);
    return;
  }
  const confirmed = confirm(`Удалить платеж #${normalizedId}?`);
  if (!confirmed) return;
  const res = await fetch("/api/super-admin/payments", {
    method: "DELETE",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ id: normalizedId }),
  });
  let data = {};
  try {
    data = await res.json();
  } catch (error) {
    data = {};
  }
  if (!res.ok) {
    setSuperAdminInfo(data.detail || "Не удалось удалить платеж.", true);
    return;
  }
  setSuperAdminInfo("Платеж удален.");
  await loadSuperAdminSection();
}

async function loadMetrics() {
  const res = await fetch("/api/admin/metrics");
  const data = await res.json();
  if (!res.ok) return;
  document.getElementById("mTotal").textContent = String(data.total_reviews || 0);
  document.getElementById("mAvg").textContent = String(data.avg_first_response_minutes || 0);
  document.getElementById("mOverdue").textContent = String(data.overdue_manual_queue_24h || 0);
  const statuses = data.status_counts || {};
  const parts = Object.entries(statuses).map(([k, v]) => `${labelFromMap(reviewStatusLabels, k)}: ${v}`);
  document.getElementById("mStatuses").textContent = parts.join(" | ");
}

async function purgeSyncActionLogs() {
  if (!confirm("Удалить из базы данных все записи sync_review и sync_conversation? Это освободит место и очистит ленту действий от лишних строк.")) return;
  try {
    const res = await fetch("/api/admin/actions-purge-sync", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
    });
    const data = await res.json();
    if (!res.ok) {
      alert("Ошибка: " + (data.detail || "не удалось очистить"));
      return;
    }
    alert(`Удалено ${(data.deleted || 0).toLocaleString("ru-RU")} записей из базы данных.`);
    await loadActions();
  } catch (err) {
    alert("Ошибка при очистке");
  }
}

async function loadActions() {
  const page = Number(actionsState.page || 1);
  const pageSize = Number(actionsState.pageSize || 50);
  const query = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  const actionType = String(actionsState.action_type || "all");
  const actor = String(actionsState.actor || "all");
  const search = String(actionsState.search || "").trim();
  if (actionType && actionType !== "all") query.set("action_type", actionType);
  if (actor && actor !== "all") query.set("actor", actor);
  if (actionsState.date_from) query.set("date_from", String(actionsState.date_from));
  if (actionsState.date_to) query.set("date_to", String(actionsState.date_to));
  if (search) query.set("search", search);
  const res = await fetch("/api/admin/actions?" + query.toString());
  const data = await res.json();
  const tbody = document.getElementById("actionsTbody");
  tbody.innerHTML = "";
  if (!res.ok) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="5">${esc(data.detail || "Не удалось загрузить ленту действий")}</td>`;
    tbody.appendChild(tr);
    return;
  }
  const filterOptions = data.filter_options || {};
  const actionTypeSelect = document.getElementById("actionsFilterActionType");
  if (actionTypeSelect) {
    const current = String(actionsState.action_type || "all");
    actionTypeSelect.innerHTML = "";
    const defaultOption = document.createElement("option");
    defaultOption.value = "all";
    defaultOption.textContent = "Действие: все";
    actionTypeSelect.appendChild(defaultOption);
    for (const item of filterOptions.action_types || []) {
      const value = String(item || "").trim();
      if (!value) continue;
      const option = document.createElement("option");
      option.value = value;
      option.textContent = labelFromMap(actionTypeLabels, value);
      actionTypeSelect.appendChild(option);
    }
    actionTypeSelect.value = current;
    if (!Array.from(actionTypeSelect.options).some((opt) => opt.value === current)) {
      actionTypeSelect.value = "all";
      actionsState.action_type = "all";
    }
  }
  const actorSelect = document.getElementById("actionsFilterActor");
  if (actorSelect) {
    const current = String(actionsState.actor || "all");
    actorSelect.innerHTML = "";
    const defaultOption = document.createElement("option");
    defaultOption.value = "all";
    defaultOption.textContent = "Пользователь: все";
    actorSelect.appendChild(defaultOption);
    for (const item of filterOptions.actors || []) {
      const value = String(item || "").trim();
      if (!value) continue;
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      actorSelect.appendChild(option);
    }
    actorSelect.value = current;
    if (!Array.from(actorSelect.options).some((opt) => opt.value === current)) {
      actorSelect.value = "all";
      actionsState.actor = "all";
    }
  }
  const dateFromInput = document.getElementById("actionsDateFrom");
  if (dateFromInput) dateFromInput.value = actionsState.date_from || "";
  const dateToInput = document.getElementById("actionsDateTo");
  if (dateToInput) dateToInput.value = actionsState.date_to || "";
  const searchInput = document.getElementById("actionsSearch");
  if (searchInput && searchInput.value !== actionsState.search) searchInput.value = actionsState.search || "";
  const items = data.items || [];
  for (const item of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(item.created_at)}</td>
      <td>${esc(item.actor)}</td>
      <td>${esc(item.review_uid || "-")}</td>
      <td>${esc(labelFromMap(actionTypeLabels, item.action_type))}</td>
      <td>${esc(formatActionDetails(item.details || {}))}</td>
    `;
    tbody.appendChild(tr);
  }
  if (!items.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="5">Действий пока нет</td>`;
    tbody.appendChild(tr);
  }
  actionsState.total = Number(data.total || 0);
  actionsState.hasMore = Boolean(data.has_more);
  const pageInfo = document.getElementById("actionsPaginationInfo");
  if (pageInfo) {
    pageInfo.textContent = `Страница ${page} · записей: ${actionsState.total}${actionsState.hasMore ? " (есть следующая)" : ""}`;
  }
  const prevBtn = document.getElementById("actionsPrevButton");
  if (prevBtn) prevBtn.disabled = page <= 1;
  const nextBtn = document.getElementById("actionsNextButton");
  if (nextBtn) nextBtn.disabled = !actionsState.hasMore;
}

async function changeActionsPage(delta) {
  const nextPage = Math.max(1, Number(actionsState.page || 1) + Number(delta || 0));
  if (nextPage === actionsState.page) return;
  actionsState.page = nextPage;
  await loadActions();
}

async function updateActionsPageSize() {
  const value = document.getElementById("actionsPageSize")?.value || "50";
  const size = Number(value || 50);
  if (!Number.isFinite(size) || size < 10 || size > 200) return;
  actionsState.pageSize = size;
  actionsState.page = 1;
  await loadActions();
}

async function prevActionsPage() {
  await changeActionsPage(-1);
}

async function nextActionsPage() {
  await changeActionsPage(1);
}

function onActionsSearchInput(value) {
  actionsState.search = String(value || "");
  actionsState.page = 1;
  if (actionsSearchTimer) clearTimeout(actionsSearchTimer);
  actionsSearchTimer = setTimeout(() => {
    loadActions();
  }, 300);
}

async function applyActionsFilters() {
  const actionType = String(document.getElementById("actionsFilterActionType")?.value || "all");
  const actor = String(document.getElementById("actionsFilterActor")?.value || "all");
  const dateFrom = String(document.getElementById("actionsDateFrom")?.value || "").trim();
  const dateTo = String(document.getElementById("actionsDateTo")?.value || "").trim();
  if (dateFrom && dateTo && dateFrom > dateTo) {
    alert("Дата начала не может быть позже даты окончания");
    return;
  }
  actionsState.action_type = actionType;
  actionsState.actor = actor;
  actionsState.date_from = dateFrom || null;
  actionsState.date_to = dateTo || null;
  actionsState.search = String(document.getElementById("actionsSearch")?.value || "").trim();
  actionsState.page = 1;
  await loadActions();
}

async function resetActionsFilters() {
  actionsState.action_type = "all";
  actionsState.actor = "all";
  actionsState.date_from = null;
  actionsState.date_to = null;
  actionsState.search = "";
  const actionTypeSelect = document.getElementById("actionsFilterActionType");
  if (actionTypeSelect) actionTypeSelect.value = "all";
  const actorSelect = document.getElementById("actionsFilterActor");
  if (actorSelect) actorSelect.value = "all";
  const dateFromInput = document.getElementById("actionsDateFrom");
  if (dateFromInput) dateFromInput.value = "";
  const dateToInput = document.getElementById("actionsDateTo");
  if (dateToInput) dateToInput.value = "";
  const searchInput = document.getElementById("actionsSearch");
  if (searchInput) searchInput.value = "";
  actionsState.page = 1;
  await loadActions();
}

function exportActions(format) {
  const exportFormat = String(format || "csv").toLowerCase();
  if (!["csv", "xlsx"].includes(exportFormat)) return;
  const query = new URLSearchParams();
  query.set("format", exportFormat);
  if (actionsState.action_type && actionsState.action_type !== "all") query.set("action_type", actionsState.action_type);
  if (actionsState.actor && actionsState.actor !== "all") query.set("actor", actionsState.actor);
  if (actionsState.date_from) query.set("date_from", String(actionsState.date_from));
  if (actionsState.date_to) query.set("date_to", String(actionsState.date_to));
  const search = String(actionsState.search || "").trim();
  if (search) query.set("search", search);
  window.location.href = "/api/admin/actions/export?" + query.toString();
}


document.addEventListener("DOMContentLoaded", () => {
  loadAdminContext().then((ok) => {
    if (!ok) return;
    loadAiSettings();
    loadUsers();
    loadMetrics();
    loadActions();
    loadSuperAdminSection();
    loadSalaryRates();
  });
});

// -----------------------------------------------------------------------
// Salary
// -----------------------------------------------------------------------

const salaryState = {
  dateFrom: null,
  dateTo: null,
};

async function loadSalaryRates() {
  const infoEl = document.getElementById("salaryRatesInfo");
  try {
    const resp = await fetch("/api/admin/salary/rates", { headers: csrfHeaders() });
    if (!resp.ok) return;
    const data = await resp.json();
    const rates = data.rates || {};
    const rr = document.getElementById("salaryRateReview");
    const rq = document.getElementById("salaryRateQuestion");
    const rc = document.getElementById("salaryRateChat");
    if (rr) rr.value = String(rates.rate_review ?? 0);
    if (rq) rq.value = String(rates.rate_question ?? 0);
    if (rc) rc.value = String(rates.rate_chat ?? 0);
  } catch (e) {
    if (infoEl) infoEl.textContent = "Ошибка загрузки ставок";
  }
}

async function saveSalaryRates() {
  const infoEl = document.getElementById("salaryRatesInfo");
  const rr = parseFloat(document.getElementById("salaryRateReview")?.value || "0") || 0;
  const rq = parseFloat(document.getElementById("salaryRateQuestion")?.value || "0") || 0;
  const rc = parseFloat(document.getElementById("salaryRateChat")?.value || "0") || 0;
  if (rr < 0 || rq < 0 || rc < 0) {
    if (infoEl) infoEl.textContent = "Ставки не могут быть отрицательными";
    return;
  }
  if (infoEl) infoEl.textContent = "Сохранение...";
  try {
    const resp = await fetch("/api/admin/salary/rates", {
      method: "PUT",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ rate_review: rr, rate_question: rq, rate_chat: rc }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      if (infoEl) infoEl.textContent = data.detail || "Ошибка сохранения";
      return;
    }
    if (infoEl) {
      infoEl.textContent = "Ставки сохранены";
      setTimeout(() => { if (infoEl) infoEl.textContent = ""; }, 3000);
    }
  } catch (e) {
    if (infoEl) infoEl.textContent = "Ошибка сохранения ставок";
  }
}

async function loadSalaryReport() {
  const infoEl = document.getElementById("salaryReportInfo");
  const tbodyEl = document.getElementById("salaryReportTbody");
  const totalEl = document.getElementById("salaryReportTotal");
  const dateFrom = document.getElementById("salaryDateFrom")?.value || null;
  const dateTo = document.getElementById("salaryDateTo")?.value || null;
  salaryState.dateFrom = dateFrom || null;
  salaryState.dateTo = dateTo || null;

  if (tbodyEl) tbodyEl.innerHTML = '<tr><td colspan="5" class="small" style="color:#9ca3af;text-align:center">Загрузка...</td></tr>';
  if (infoEl) infoEl.textContent = "";
  if (totalEl) totalEl.classList.add("hidden");

  try {
    const query = new URLSearchParams();
    if (dateFrom) query.set("date_from", dateFrom);
    if (dateTo) query.set("date_to", dateTo);
    const resp = await fetch("/api/admin/salary/report?" + query.toString(), { headers: csrfHeaders() });
    const data = await resp.json();
    if (!resp.ok) {
      if (infoEl) infoEl.textContent = data.detail || "Ошибка загрузки";
      if (tbodyEl) tbodyEl.innerHTML = '<tr><td colspan="5" class="small" style="color:#ef4444;text-align:center">Ошибка загрузки</td></tr>';
      return;
    }
    const rows = data.rows || [];
    if (rows.length === 0) {
      if (tbodyEl) tbodyEl.innerHTML = '<tr><td colspan="5" class="small" style="color:#9ca3af;text-align:center">Нет данных за выбранный период</td></tr>';
      return;
    }
    let grandTotal = 0;
    const html = rows.map((row) => {
      grandTotal += Number(row.total_amount || 0);
      return `<tr>
        <td>${esc(row.actor || "—")}</td>
        <td>${row.review_count ?? 0}</td>
        <td>${row.question_count ?? 0}</td>
        <td>${row.chat_count ?? 0}</td>
        <td><strong>${formatAmount(row.total_amount)}</strong></td>
      </tr>`;
    }).join("");
    if (tbodyEl) tbodyEl.innerHTML = html;
    if (totalEl) {
      totalEl.textContent = `Итого: ${formatAmount(grandTotal)} ₽`;
      totalEl.classList.remove("hidden");
    }
  } catch (e) {
    if (infoEl) infoEl.textContent = "Ошибка загрузки отчёта";
    if (tbodyEl) tbodyEl.innerHTML = '<tr><td colspan="5" class="small" style="color:#ef4444;text-align:center">Ошибка загрузки</td></tr>';
  }
}

function resetSalaryDates() {
  const df = document.getElementById("salaryDateFrom");
  const dt = document.getElementById("salaryDateTo");
  if (df) df.value = "";
  if (dt) dt.value = "";
  salaryState.dateFrom = null;
  salaryState.dateTo = null;
  loadSalaryReport();
}

function exportSalaryReport(format) {
  const exportFormat = String(format || "csv").toLowerCase();
  if (!["csv", "xlsx"].includes(exportFormat)) return;
  const query = new URLSearchParams();
  query.set("format", exportFormat);
  if (salaryState.dateFrom) query.set("date_from", salaryState.dateFrom);
  if (salaryState.dateTo) query.set("date_to", salaryState.dateTo);
  window.location.href = "/api/admin/salary/report/export?" + query.toString();
}

function formatAmount(value) {
  const num = Number(value || 0);
  return num.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
