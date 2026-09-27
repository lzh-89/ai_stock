const state = {
  history: null,
  loading: false,
  refreshing: false
};

const els = {
  codeInput: document.querySelector("#codeInput"),
  entryStatusSelect: document.querySelector("#entryStatusSelect"),
  limitInput: document.querySelector("#limitInput"),
  loadButton: document.querySelector("#loadButton"),
  refreshOutcomesButton: document.querySelector("#refreshOutcomesButton"),
  manualAuditForm: document.querySelector("#manualAuditForm"),
  manualCodeInput: document.querySelector("#manualCodeInput"),
  manualNameInput: document.querySelector("#manualNameInput"),
  manualSourceSelect: document.querySelector("#manualSourceSelect"),
  manualPositionStateSelect: document.querySelector("#manualPositionStateSelect"),
  manualRunButton: document.querySelector("#manualRunButton"),
  batchAuditForm: document.querySelector("#batchAuditForm"),
  batchSourceSelect: document.querySelector("#batchSourceSelect"),
  batchLimitInput: document.querySelector("#batchLimitInput"),
  batchRunButton: document.querySelector("#batchRunButton"),
  actionMeta: document.querySelector("#actionMeta"),
  actionResult: document.querySelector("#actionResult"),
  notice: document.querySelector("#notice"),
  summaryGrid: document.querySelector("#summaryGrid"),
  historyMeta: document.querySelector("#historyMeta"),
  historyRows: document.querySelector("#historyRows")
};

const ENTRY_STATUS_LABELS = {
  block_entry: "禁止进场",
  wait_confirm: "等待确认",
  pullback_only: "只允许低吸",
  small_test: "小仓试错",
  watch_priority: "重点观察",
  t1_only: "仅适合 T+1",
  manual_review: "人工判断"
};

const SOURCE_LABELS = {
  ai_full_market: "AI 全市场",
  t1: "T+1"
};

const AI_STATUS_LABELS = {
  deepseek_success: "DeepSeek 已完成",
  ai_not_configured: "AI 未配置",
  fallback: "AI 未完成",
  skipped: "AI 未完成",
  disabled: "AI 未启用",
  failed: "AI 未完成"
};

const HORIZON_LABELS = {
  t1_next_day: "T+1",
  "1d": "1 日",
  "3d": "3 日",
  "5d": "5 日"
};

els.loadButton.addEventListener("click", loadHistory);
els.refreshOutcomesButton.addEventListener("click", refreshOutcomes);
els.manualAuditForm.addEventListener("submit", runManualAudit);
els.batchAuditForm.addEventListener("submit", runBatchAudit);
els.historyRows.addEventListener("change", handleFeedbackChange);
els.historyRows.addEventListener("click", handleFeedbackClick);
els.codeInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") loadHistory();
});
els.entryStatusSelect.addEventListener("change", loadHistory);
hydrateFromQuery();
loadHistory();

async function loadHistory() {
  setLoading(true);
  try {
    const params = new URLSearchParams();
    const code = els.codeInput.value.trim();
    const status = els.entryStatusSelect.value;
    const limit = readLimit();
    if (code) params.set("code", code);
    if (status) params.set("entryStatus", status);
    params.set("limit", String(limit));
    const payload = await fetchJson(`/api/intraday-entry-audit/history?${params.toString()}`);
    state.history = payload;
    renderHistory(payload);
    showNotice("");
  } catch (error) {
    showNotice(`审计历史读取失败：${error.message || String(error)}`);
  } finally {
    setLoading(false);
  }
}

async function refreshOutcomes() {
  setRefreshing(true);
  try {
    const payload = await postJson("/api/intraday-entry-audit/outcomes/refresh", {
      limit: readLimit(),
      horizons: ["t1_next_day", "1d", "3d", "5d"]
    });
    showNotice(
      [
        `已刷新 ${formatInteger(payload.snapshotCount)} 个审计快照`,
        `完成 ${formatInteger(payload.doneCount)}`,
        `待回填 ${formatInteger(payload.pendingCount)}`,
        `数据不足 ${formatInteger(payload.noDataCount)}`
      ].join(" · ")
    );
    await loadHistory();
  } catch (error) {
    showNotice(`参考验证刷新失败：${error.message || String(error)}`);
  } finally {
    setRefreshing(false);
  }
}

async function runManualAudit(event) {
  event.preventDefault();
  const code = els.manualCodeInput.value.trim();
  if (!code) {
    showActionResult("请输入股票代码。", true);
    return;
  }
  setManualRunning(true);
  try {
    const payload = await postJson("/api/intraday-entry-audit/run", {
      code,
      name: els.manualNameInput.value.trim(),
      triggerSource: els.manualSourceSelect.value,
      refreshStockAnalysis: true,
      position: { state: els.manualPositionStateSelect.value }
    });
    showActionResult(`已生成审计快照 #${payload.snapshotId}：${statusLabel(payload.entryStatus, payload.displayStatus)}。`);
    els.codeInput.value = normalizeCode(code);
    await loadHistory();
  } catch (error) {
    showActionResult(`单票审计失败：${error.message || String(error)}`, true);
  } finally {
    setManualRunning(false);
  }
}

async function runBatchAudit(event) {
  event.preventDefault();
  setBatchRunning(true);
  try {
    const payload = await postJson("/api/intraday-entry-audit/batch-run", {
      triggerSource: els.batchSourceSelect.value,
      limit: readBatchLimit(),
      selectedOnly: true,
      refreshStockAnalysis: true,
      position: { state: "planned" }
    });
    if (payload.skipReason) {
      showActionResult(payload.skipReason, true);
    } else {
      showActionResult(`批量审计完成：${formatInteger(payload.doneCount)}/${formatInteger(payload.requestedCount)} 条，run ${payload.runId || "--"}。`);
      await loadHistory();
    }
  } catch (error) {
    showActionResult(`批量审计失败：${error.message || String(error)}`, true);
  } finally {
    setBatchRunning(false);
  }
}

async function handleFeedbackChange(event) {
  const field = event.target.closest("[data-feedback-field]");
  if (!field) return;
  const row = event.target.closest("[data-snapshot-id]");
  if (!row) return;
  row.dataset.dirty = "1";
}

async function handleFeedbackClick(event) {
  const button = event.target.closest("[data-feedback-save]");
  if (!button) return;
  const row = event.target.closest("[data-snapshot-id]");
  if (!row) return;
  const snapshotId = Number(row.dataset.snapshotId || 0);
  const payload = {
    snapshotId,
    userAction: row.querySelector('[data-feedback-field="userAction"]')?.value || "",
    actualBuyPct: row.querySelector('[data-feedback-field="actualBuyPct"]')?.value || "",
    actualBuyPrice: row.querySelector('[data-feedback-field="actualBuyPrice"]')?.value || "",
    note: row.querySelector('[data-feedback-field="note"]')?.value || ""
  };
  button.disabled = true;
  button.textContent = "保存中";
  try {
    await postJson("/api/intraday-entry-audit/feedback", payload);
    row.dataset.dirty = "";
    button.textContent = "已保存";
    window.setTimeout(() => {
      button.textContent = "保存";
      button.disabled = false;
    }, 900);
  } catch (error) {
    button.textContent = "保存失败";
    showNotice(`反馈保存失败：${error.message || String(error)}`);
    window.setTimeout(() => {
      button.textContent = "保存";
      button.disabled = false;
    }, 1200);
  }
}

async function fetchJson(url) {
  const response = await fetch(url);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
  return payload;
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {})
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
  return payload;
}

function renderHistory(payload) {
  const rows = Array.isArray(payload.items) ? payload.items : [];
  const stats = summarize(rows);
  els.historyMeta.textContent = `${formatInteger(rows.length)} 条 · 上限 ${formatInteger(payload.limit)}`;
  els.summaryGrid.innerHTML = [
    ["审计快照", formatInteger(rows.length), `筛选状态：${statusLabel(payload.entryStatus) || "全部"}`],
    ["禁止/人工", formatInteger(stats.defensiveCount), "禁止进场 + 人工判断"],
    ["允许试错", formatInteger(stats.actionableCount), "小仓试错 + 低吸 + T+1"],
    ["验证完成", formatInteger(stats.doneOutcomeCount), `数据不足 ${formatInteger(stats.noDataCount)}`]
  ].map(([label, value, sub]) => `
    <article>
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(sub)}</small>
    </article>
  `).join("");

  els.historyRows.innerHTML = rows.length ? rows.map((row) => {
    const outcomeMap = outcomesByHorizon(row.outcomes);
    return `
      <tr>
        <td>${escapeHtml(formatDateTime(row.auditedAt))}<small>${escapeHtml(row.tradeDate || "--")}</small></td>
        <td>${stockCell(row)}</td>
        <td>${escapeHtml(sourceLabel(row.triggerSource))}<small>run ${escapeHtml(row.triggerRunId || "--")}</small></td>
        <td>
          <span class="entry-history-status ${statusClass(row.entryStatus)}">${escapeHtml(statusLabel(row.entryStatus, row.displayStatus))}</span>
          <small>${escapeHtml(row.auditConclusion || "--")}</small>
        </td>
        <td>${escapeHtml(formatPosition(row))}<small>${escapeHtml(actionLabel(row.positionAction))}</small></td>
        <td>${escapeHtml(aiStatusLabel(row.aiStatus))}<small>${escapeHtml(dataQualityText(row.dataQuality))}</small></td>
        <td>${feedbackCell(row)}</td>
        <td>${outcomeCell(outcomeMap.t1_next_day)}</td>
        <td>${outcomeCell(outcomeMap["1d"])}</td>
        <td>${outcomeCell(outcomeMap["3d"])}</td>
        <td>${outcomeCell(outcomeMap["5d"])}</td>
      </tr>
    `;
  }).join("") : `<tr><td colspan="11" class="empty">暂无进场审计快照</td></tr>`;
}

function summarize(rows) {
  const stats = { defensiveCount: 0, actionableCount: 0, doneOutcomeCount: 0, noDataCount: 0 };
  rows.forEach((row) => {
    if (["block_entry", "manual_review"].includes(row.entryStatus)) stats.defensiveCount += 1;
    if (["pullback_only", "small_test", "t1_only"].includes(row.entryStatus)) stats.actionableCount += 1;
    (row.outcomes || []).forEach((outcome) => {
      if (outcome.status === "done") stats.doneOutcomeCount += 1;
      if (outcome.status === "no_data") stats.noDataCount += 1;
    });
  });
  return stats;
}

function outcomesByHorizon(outcomes) {
  const result = {};
  (Array.isArray(outcomes) ? outcomes : []).forEach((item) => {
    result[item.horizon] = item;
  });
  return result;
}

function outcomeCell(outcome) {
  if (!outcome) return `<span class="muted">未刷新</span>`;
  const label = outcome.label || outcomeStatusLabel(outcome.status);
  const value = outcome.status === "done" ? formatMaybePct(outcome.returnPct) : outcomeStatusLabel(outcome.status);
  return `
    <span class="${outcomeClass(outcome)}">${escapeHtml(label)}</span>
    <small>${escapeHtml(value)}${outcome.reason ? ` · ${escapeHtml(outcome.reason)}` : ""}</small>
  `;
}

function stockCell(row) {
  const code = row.code || "";
  const name = row.name || code || "--";
  const params = new URLSearchParams({ stock: code, analyze: "1" });
  return `
    <a class="entry-history-stock-link" href="./index.html?${params.toString()}#stock">${escapeHtml(name)}</a>
    <small>${escapeHtml(code)}</small>
  `;
}

function feedbackCell(row) {
  const feedback = row.feedback || {};
  return `
    <div class="entry-history-feedback" data-snapshot-id="${escapeHtml(row.snapshotId)}">
      <select data-feedback-field="userAction" aria-label="反馈状态">
        ${feedbackOption("", "未反馈", feedback.userAction)}
        ${feedbackOption("watching", "继续观察", feedback.userAction)}
        ${feedbackOption("adopted", "采纳审计", feedback.userAction)}
        ${feedbackOption("not_adopted", "未采纳", feedback.userAction)}
        ${feedbackOption("bought", "已买入", feedback.userAction)}
        ${feedbackOption("abandoned", "放弃", feedback.userAction)}
      </select>
      <div class="entry-history-feedback-row">
        <input data-feedback-field="actualBuyPct" type="number" min="0" max="100" step="1" placeholder="仓位%" value="${escapeHtml(feedback.actualBuyPct ?? "")}" />
        <input data-feedback-field="actualBuyPrice" type="number" min="0" step="0.01" placeholder="买价" value="${escapeHtml(feedback.actualBuyPrice ?? "")}" />
      </div>
      <input data-feedback-field="note" type="text" maxlength="160" placeholder="备注" value="${escapeHtml(feedback.note || "")}" />
      <button data-feedback-save type="button" class="secondary-button">保存</button>
    </div>
  `;
}

function feedbackOption(value, label, selected) {
  return `<option value="${escapeHtml(value)}" ${String(selected || "") === value ? "selected" : ""}>${escapeHtml(label)}</option>`;
}

function readLimit() {
  const value = Number(els.limitInput.value || 100);
  return Math.max(20, Math.min(500, Number.isFinite(value) ? value : 100));
}

function readBatchLimit() {
  const value = Number(els.batchLimitInput.value || 3);
  return Math.max(1, Math.min(10, Number.isFinite(value) ? value : 3));
}

function setLoading(loading) {
  state.loading = loading;
  els.loadButton.disabled = loading;
  els.loadButton.innerHTML = loading ? buttonIcon("search") + "查询中" : buttonIcon("search") + "查询";
}

function setRefreshing(refreshing) {
  state.refreshing = refreshing;
  els.refreshOutcomesButton.disabled = refreshing;
  els.refreshOutcomesButton.innerHTML = refreshing ? buttonIcon("refresh") + "刷新中" : buttonIcon("refresh") + "刷新验证";
}

function setManualRunning(running) {
  els.manualRunButton.disabled = running;
  els.manualRunButton.innerHTML = running ? buttonIcon("play") + "审计中" : buttonIcon("play") + "审计单票";
}

function setBatchRunning(running) {
  els.batchRunButton.disabled = running;
  els.batchRunButton.innerHTML = running ? buttonIcon("batch") + "审计中" : buttonIcon("batch") + "批量审计";
}

function buttonIcon(type) {
  if (type === "refresh") {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.7 6.3A8 8 0 1 0 20 12h-2a6 6 0 1 1-1.8-4.3L13 11h8V3l-3.3 3.3Z"/></svg>';
  }
  if (type === "play") {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7L8 5Z"/></svg>';
  }
  if (type === "batch") {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h7v7H4V4Zm9 0h7v7h-7V4ZM4 13h7v7H4v-7Zm9 0h7v7h-7v-7Z"/></svg>';
  }
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 4a6 6 0 1 0 3.7 10.7l3.6 3.6 1.4-1.4-3.6-3.6A6 6 0 0 0 10 4Zm0 2a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z"/></svg>';
}

function showActionResult(message, isError = false) {
  const text = String(message || "").trim();
  els.actionResult.hidden = !text;
  els.actionResult.textContent = text;
  els.actionResult.classList.toggle("error", Boolean(isError));
  els.actionMeta.textContent = text ? "已更新" : "--";
}

function hydrateFromQuery() {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code") || params.get("stock") || "";
  if (code) {
    els.codeInput.value = normalizeCode(code);
    els.manualCodeInput.value = normalizeCode(code);
  }
  const source = params.get("triggerSource") || params.get("source") || "";
  if (["ai_full_market", "t1"].includes(source)) {
    els.manualSourceSelect.value = source;
  }
}

function showNotice(message) {
  const text = String(message || "").trim();
  els.notice.hidden = !text;
  els.notice.textContent = text;
}

function formatDateTime(value) {
  if (!value) return "--";
  return String(value).replace("T", " ");
}

function formatPosition(row) {
  const pct = Number(row.positionPct || 0);
  if (!Number.isFinite(pct) || pct <= 0) return "0%";
  return `${formatNumber(pct)}%`;
}

function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return Number.isInteger(number) ? String(number) : number.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function formatInteger(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? String(Math.trunc(number)) : "0";
}

function formatMaybePct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function statusLabel(value, fallback = "") {
  const status = String(value || "").trim();
  const fallbackText = String(fallback || "").trim();
  if (ENTRY_STATUS_LABELS[status]) return ENTRY_STATUS_LABELS[status];
  if (ENTRY_STATUS_LABELS[fallbackText]) return ENTRY_STATUS_LABELS[fallbackText];
  return fallbackText || status || "";
}

function sourceLabel(value) {
  return SOURCE_LABELS[String(value || "").trim()] || "--";
}

function aiStatusLabel(value) {
  return AI_STATUS_LABELS[String(value || "").trim()] || "AI 状态未知";
}

function normalizeCode(value) {
  const digits = String(value || "").replace(/\D+/g, "");
  return digits ? digits.slice(-6).padStart(6, "0") : "";
}

function actionLabel(value) {
  return {
    no_new_position: "不新开仓",
    new_small_test: "小仓试错",
    pullback_entry: "回踩低吸",
    hold_only: "仅持有处理",
    reduce_position: "降低仓位"
  }[String(value || "").trim()] || "按审计条件处理";
}

function outcomeStatusLabel(value) {
  return {
    done: "已完成",
    pending: "待回填",
    no_data: "数据不足"
  }[String(value || "").trim()] || "未刷新";
}

function dataQualityText(quality) {
  if (!quality || typeof quality !== "object") return "--";
  const parts = [];
  if (quality.dateWarning) parts.push("历史信号");
  if (quality.stockAnalysisFresh) parts.push("个股分析可用");
  if (quality.sectorRadarFresh) parts.push("板块证据可用");
  if (quality.sectorRadarStale) parts.push("板块证据过期");
  const coreMissing = Array.isArray(quality.coreMissingEvidence) ? quality.coreMissingEvidence.length : 0;
  if (coreMissing) parts.push(`核心缺失 ${coreMissing}`);
  return parts.join(" · ") || "--";
}

function statusClass(value) {
  const status = String(value || "");
  if (status === "block_entry") return "bad";
  if (["small_test", "t1_only"].includes(status)) return "good";
  if (["wait_confirm", "pullback_only", "manual_review"].includes(status)) return "warn";
  return "neutral";
}

function outcomeClass(outcome) {
  const label = String(outcome?.label || "");
  if (["审计有效", "规避有效", "观察合理"].includes(label)) return "positive";
  if (["审计失败", "可能错过", "观察错过"].includes(label)) return "negative";
  return "muted";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
