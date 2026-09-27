const state = {
  sectorType: "all",
  source: "auto",
  marketSource: "auto",
  scanTime: "10:00",
  run: null,
  diagnostics: null,
  polling: null,
  loading: false
};

const els = {
  sectorTypeSelect: document.querySelector("#sectorTypeSelect"),
  sourceSelect: document.querySelector("#sourceSelect"),
  marketSourceSelect: document.querySelector("#marketSourceSelect"),
  scanTimeInput: document.querySelector("#scanTimeInput"),
  latestButton: document.querySelector("#latestButton"),
  runButton: document.querySelector("#runButton"),
  useAiCheck: document.querySelector("#useAiCheck"),
  forceCheck: document.querySelector("#forceCheck"),
  notice: document.querySelector("#notice"),
  statusText: document.querySelector("#statusText"),
  modeText: document.querySelector("#modeText"),
  sectorPoolText: document.querySelector("#sectorPoolText"),
  sourceText: document.querySelector("#sourceText"),
  aiText: document.querySelector("#aiText"),
  qualityText: document.querySelector("#qualityText"),
  finalText: document.querySelector("#finalText"),
  elapsedText: document.querySelector("#elapsedText"),
  progressBar: document.querySelector("#progressBar"),
  stageList: document.querySelector("#stageList"),
  resultSummary: document.querySelector("#resultSummary"),
  sectorGrid: document.querySelector("#sectorGrid"),
  ruleRows: document.querySelector("#ruleRows"),
  refreshOutcomesButton: document.querySelector("#refreshOutcomesButton"),
  diagnosticsPanel: document.querySelector("#diagnosticsPanel")
};

init();

function init() {
  const params = new URLSearchParams(window.location.search);
  state.sectorType = params.get("sectorType") || "all";
  state.source = params.get("source") || "auto";
  state.marketSource = params.get("marketSource") || "auto";
  state.scanTime = params.get("scanTime") || "10:00";
  els.sectorTypeSelect.value = state.sectorType;
  els.sourceSelect.value = state.source;
  els.marketSourceSelect.value = state.marketSource;
  els.scanTimeInput.value = state.scanTime;
  bindEvents();
  loadLatest();
  loadDiagnostics();
}

function bindEvents() {
  els.sectorTypeSelect.addEventListener("change", () => {
    state.sectorType = els.sectorTypeSelect.value || "all";
    syncUrl();
  });
  els.sourceSelect.addEventListener("change", () => {
    state.source = els.sourceSelect.value || "auto";
    syncUrl();
  });
  els.marketSourceSelect.addEventListener("change", () => {
    state.marketSource = els.marketSourceSelect.value || "auto";
    syncUrl();
  });
  els.scanTimeInput.addEventListener("change", () => {
    state.scanTime = els.scanTimeInput.value || "10:00";
    syncUrl();
  });
  els.latestButton.addEventListener("click", loadLatest);
  els.runButton.addEventListener("click", startRun);
  els.refreshOutcomesButton.addEventListener("click", refreshOutcomes);
  els.sectorGrid.addEventListener("click", (event) => {
    const button = event.target.closest("[data-stock-code]");
    if (!button) return;
    const code = button.dataset.stockCode || "";
    if (!code) return;
    window.location.href = `./index.html?stock=${encodeURIComponent(code)}&analyze=1#stock`;
  });
}

async function loadLatest() {
  setLoading(true);
  clearPolling();
  try {
    const params = new URLSearchParams({ sectorType: state.sectorType, source: state.source, scanTime: state.scanTime });
    const response = await fetch(`/api/morning-sector-radar/latest?${params.toString()}`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.run = payload.status === "empty" ? null : payload;
    render();
    if (payload.warning) showNotice(payload.warning);
    if (state.run && ["pending", "running"].includes(state.run.status)) pollRun(state.run.id);
  } catch (error) {
    renderError(error);
  } finally {
    setLoading(false);
  }
}

async function startRun() {
  setLoading(true);
  clearPolling();
  try {
    const response = await fetch("/api/morning-sector-radar/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sectorType: state.sectorType,
        source: state.source,
        marketSource: state.marketSource,
        scanTime: state.scanTime,
        useAI: els.useAiCheck.checked,
        force: els.forceCheck.checked,
        sectorLimit: 20,
        leaderLimit: 5
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.run = payload;
    render();
    pollRun(payload.id);
  } catch (error) {
    renderError(error);
  } finally {
    setLoading(false);
  }
}

function pollRun(runId) {
  clearPolling();
  state.polling = window.setInterval(async () => {
    try {
      const response = await fetch(`/api/morning-sector-radar/runs/${runId}`);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
      state.run = payload;
      render();
      if (!["pending", "running"].includes(payload.status)) {
        clearPolling();
        loadDiagnostics();
      }
    } catch (error) {
      clearPolling();
      renderError(error);
    }
  }, 1200);
}

async function loadDiagnostics() {
  try {
    const response = await fetch("/api/morning-sector-radar/diagnostics?windowRuns=20");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.diagnostics = payload;
    renderDiagnostics();
  } catch (error) {
    state.diagnostics = { error: error.message };
    renderDiagnostics();
  }
}

async function refreshOutcomes() {
  const runId = state.run?.id || 0;
  try {
    const response = await fetch("/api/morning-sector-radar/outcomes/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ runId, batchSize: 200 })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    showNotice(payload.message || "验证状态已刷新。");
    loadDiagnostics();
  } catch (error) {
    renderError(error);
  }
}

function render() {
  const run = state.run;
  if (!run) {
    els.statusText.textContent = "暂无结果";
    els.modeText.textContent = "可手动生成";
    els.sectorPoolText.textContent = "--";
    els.sourceText.textContent = sectorTypeLabel(state.sectorType);
    els.aiText.textContent = "--";
    els.qualityText.textContent = "--";
    els.finalText.textContent = "--";
    els.elapsedText.textContent = "--";
    els.progressBar.style.width = "0%";
    els.stageList.innerHTML = "";
    els.resultSummary.textContent = "--";
    els.sectorGrid.innerHTML = emptyBlock("暂无早盘主线结果");
    els.ruleRows.innerHTML = emptyTableRow(8, "暂无规则基线");
    return;
  }

  const selected = run.sectors || [];
  const rules = run.ruleSectors || [];
  const hasRules = rules.length > 0;
  const sourceMeta = run.sourceMeta || {};
  const sourceUnavailable = run.status === "failed" && run.dataQualityStatus === "unavailable" && !hasRules;
  const displaySectors = selected.length
    ? selected
    : rules.slice(0, 5).map((item) => ({ ...item, displayFallback: true }));
  const pct = Number(run.progressPct || 0);
  const fallback = hasRules && (run.ruleFallbackUsed || run.selectionMode !== "ai_reviewed");
  els.statusText.textContent = statusLabel(run.status);
  els.modeText.textContent = `${selectionModeLabel(run.selectionMode)} · ${stageLabel(run.currentStage)}`;
  els.sectorPoolText.textContent = `${Number(run.sectorCount || rules.length || 0)} 个`;
  els.sourceText.textContent = `${sourceLabel(run.actualSource || run.requestedSource)} · ${sectorTypeLabel(run.sectorType)}`;
  els.aiText.textContent = sourceUnavailable ? "未审查" : fallback ? "规则兜底" : "已审查";
  els.qualityText.textContent = `${dataQualityLabel(run.dataQualityStatus)}${run.aiError ? ` · ${run.aiError}` : ""}`;
  els.finalText.textContent = `${selected.length} 个`;
  els.elapsedText.textContent = `${Number(run.elapsedSeconds || 0)} 秒`;
  els.progressBar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  els.stageList.innerHTML = renderStages(run.currentStage, run.status);
  els.resultSummary.textContent = `${run.tradeDate || "--"} ${run.scanTime || "--"} · ${sampleRoleLabel(run.sampleRole)}`;
  els.sectorGrid.innerHTML = sourceUnavailable
    ? emptyBlock(sourceUnavailableText(run, sourceMeta))
    : displaySectors.length ? displaySectors.map(renderSectorCard).join("") : emptyBlock("没有进入最终观察的板块");
  els.ruleRows.innerHTML = rules.length ? rules.map(renderRuleRow).join("") : emptyTableRow(8, "暂无规则基线");

  const warnings = [];
  if (run.ruleFallbackUsed) warnings.push("DeepSeek 未生效或审查失败，当前展示规则兜底结果。");
  if (!selected.length && rules.length) warnings.push("DeepSeek 本轮给出 Top0，页面展示规则 Top5 作为未入选观察样本，便于查看前排/后排和复盘原因。");
  if (run.dataQualityStatus === "mock") warnings.push("当前为 Mock 测试数据，板块名称、股票和资金数据均不可用于真实交易判断。");
  if (run.dataQualityStatus === "stale") warnings.push("板块或市场数据使用旧缓存，本轮不能视为标准样本。");
  if (sourceUnavailable) warnings.push(sourceUnavailableText(run, sourceMeta));
  else if (run.error) warnings.push(`任务失败：${run.error}`);
  if (run.aiError && !run.ruleFallbackUsed) warnings.push(run.aiError);
  if (warnings.length) showNotice(warnings.join(" "));
  else hideNotice();
}

function renderSectorCard(sector) {
  const leaders = (sector.leaders || []).slice(0, 5);
  const followers = (sector.followers || []).slice(0, 3);
  const action = sector.displayFallback ? "未入选观察样本" : sector.aiAction ? `AI ${actionLabel(sector.aiAction)}` : "规则入选";
  const dimensions = sector.dimensions || {};
  return `
    <article class="sector-card">
      <div class="sector-card-head">
        <div>
          <span>${escapeHtml(sectorTypeLabel(sector.sectorType))} · #${escapeHtml(String(sector.finalRank || sector.ruleRank || "--"))}</span>
          <h3>${escapeHtml(sector.sectorName || "--")}</h3>
        </div>
        <strong>${formatNumber(sector.activityScore, 1)}</strong>
      </div>
      <div class="sector-metrics">
        <span>涨幅 ${formatPct(sector.changePct)}</span>
        <span>扩散 ${formatNumber(sector.breadthPct, 1)}%</span>
        <span>资金 ${formatMoney(sector.mainNetInflow)}</span>
        <span>涨停 ${Number(sector.limitUpCount || 0)}</span>
      </div>
      <div class="dimension-bars">
        ${dimensionBar("价格", dimensions.priceStrength, 15)}
        ${dimensionBar("扩散", dimensions.breadth, 15)}
        ${dimensionBar("成交", dimensions.turnoverActivity, 15)}
        ${dimensionBar("资金", dimensions.capitalFlow, 15)}
        ${dimensionBar("前排", dimensions.leaderQuality, 15)}
      </div>
      <div class="sector-reason">
        <b>${escapeHtml(action)} · ${escapeHtml(finalStatusLabel(sector.finalStatus))}</b>
        <p>${escapeHtml(sector.displayFallback ? fallbackSectorReason(sector) : sector.aiReason || (sector.reasons || []).join(" "))}</p>
      </div>
      <div class="constituent-section">
        <div class="constituent-title">
          <b>板块前排</b>
          <span>默认展示 5 只，点击打开个股分析</span>
        </div>
        <div class="leader-list">
          ${leaders.length ? leaders.map((item, index) => renderLeader(item, index)).join("") : `<span class="empty-inline">暂无前排成分股</span>`}
        </div>
        <div class="constituent-title backline-title">
          <b>后排观察</b>
          <span>只作扩散度参考，不等于可买标的</span>
        </div>
        <div class="leader-list follower-list">
          ${followers.length ? followers.map((item, index) => renderLeader(item, index + leaders.length, true)).join("") : `<span class="empty-inline">暂无后排样本</span>`}
        </div>
      </div>
    </article>
  `;
}

function renderLeader(item, index = 0, follower = false) {
  const code = String(item.code || "").padStart(6, "0");
  const role = follower ? "后排观察" : leaderRoleLabel(item.candidateRole, index);
  return `
    <button class="leader-pill ${follower ? "follower-pill" : ""}" type="button" data-stock-code="${escapeHtml(code)}" title="打开个股分析">
      <i>${escapeHtml(role)}</i>
      <span>${escapeHtml(item.name || "--")}</span>
      <b>${escapeHtml(code)}</b>
      <small>${formatPct(item.changePct)} · ${tradeabilityLabel(item.tradeability)}</small>
    </button>
  `;
}

function renderRuleRow(item) {
  return `
    <tr>
      <td>#${Number(item.ruleRank || 0)}</td>
      <td><strong>${escapeHtml(item.sectorName || "--")}</strong></td>
      <td>${escapeHtml(sectorTypeLabel(item.sectorType))}</td>
      <td>${formatNumber(item.activityScore, 1)}</td>
      <td>${formatPct(item.changePct)}</td>
      <td>${formatNumber(item.breadthPct, 1)}%</td>
      <td>${escapeHtml(item.fundFlowStatus === "ok" ? formatMoney(item.mainNetInflow) : "缺失")}</td>
      <td>${escapeHtml(finalStatusLabel(item.ruleStatus))}</td>
    </tr>
  `;
}

function fallbackSectorReason(sector) {
  const aiReason = String(sector.aiReason || "").trim();
  const ruleReason = (sector.reasons || []).join(" ");
  if (aiReason) return `该板块未进入 AI 最终观察，只作为规则基线样本展示。AI 理由：${aiReason}`;
  return `该板块未进入 AI 最终观察，只作为规则基线样本展示。规则证据：${ruleReason}`;
}

function sourceUnavailableText(run, sourceMeta = {}) {
  const cacheStatus = sourceMeta.cacheStatus ? `缓存状态：${sourceMeta.cacheStatus}。` : "";
  const error = run.error || sourceMeta.error || "外部板块源未返回数据";
  return `板块源刷新失败，且没有可用真实缓存。本次不展示板块观察，不使用 Mock 代替真实数据。${cacheStatus}失败原因：${error}`;
}

function renderStages(currentStage, status) {
  const stages = [
    ["loading_market", "市场"],
    ["loading_sectors", "板块"],
    ["loading_constituents", "前排"],
    ["ai_reviewing", "AI审查"],
    ["saving", "落库"],
    ["done", "完成"]
  ];
  const currentIndex = stages.findIndex(([key]) => key === currentStage);
  return stages
    .map(([key, label], index) => {
      const done = status === "done" || (currentIndex >= 0 && index <= currentIndex);
      return `<span class="batch-pill ${done ? "stage-done" : ""}">${label}</span>`;
    })
    .join("");
}

function renderDiagnostics() {
  const data = state.diagnostics;
  if (!data) {
    els.diagnosticsPanel.innerHTML = emptyBlock("诊断加载中");
    return;
  }
  if (data.error) {
    els.diagnosticsPanel.innerHTML = emptyBlock(`诊断失败：${escapeHtml(data.error)}`);
    return;
  }
  const groups = data.groups || [];
  const status = data.outcomeStatus || {};
  const runs = data.runBreakdown || [];
  els.diagnosticsPanel.innerHTML = `
    <article>
      <span>最近任务</span>
      <strong>${Number(data.runCount || 0)}</strong>
      <small>窗口 ${Number(data.windowRuns || 20)} 次</small>
    </article>
    <article>
      <span>样本行</span>
      <strong>${Number(status.rowCount || data.rowCount || 0)}</strong>
      <small>${escapeHtml(data.formulaText || "规则基线和最终结果")}</small>
    </article>
    <article>
      <span>已完成</span>
      <strong>${Number(status.doneCount || data.doneCount || 0)}</strong>
      <small>已接入后续表现</small>
    </article>
    <article>
      <span>待回填</span>
      <strong>${Number(status.pendingCount || data.pendingCount || 0)}</strong>
      <small>等待收盘或后续行情</small>
    </article>
    <article>
      <span>缺数据/已排除</span>
      <strong>${Number(status.noDataCount || data.noDataCount || 0)} / ${Number(status.excludedCount || data.excludedCount || 0)}</strong>
      <small>不进入正式收益统计</small>
    </article>
    <article class="diagnostic-wide">
      <span>结果分组</span>
      <div class="diagnostic-tags">
        ${groups.length ? groups.map(renderOutcomeGroupTag).join("") : "<b>暂无</b>"}
      </div>
    </article>
    <article class="diagnostic-wide">
      <span>最近任务明细</span>
      <div class="diagnostic-run-list">
        ${runs.length ? runs.map(renderRunBreakdown).join("") : "<b>暂无</b>"}
      </div>
    </article>
  `;
}

function renderOutcomeGroupTag(item) {
  const avg = item.status === "done" ? ` · 收盘 ${formatPct(item.avg_close_return_pct)}` : "";
  const excess = item.status === "done" ? ` · 超额 ${formatPct(item.avg_excess_close_return_pct)}` : "";
  return `<b>${escapeHtml(resultTypeLabel(item.result_type))} · ${escapeHtml(outcomeStatusLabel(item.status))} · ${Number(item.count || 0)}${avg}${excess}</b>`;
}

function renderRunBreakdown(run) {
  return `
    <b>
      #${Number(run.run_id || 0)} · ${escapeHtml(run.trade_date || "--")} ${escapeHtml(run.scan_time || "--")}
      · ${escapeHtml(sourceLabel(run.actual_source))}
      · ${escapeHtml(sampleRoleLabel(run.sample_role))}
      · 行 ${Number(run.row_count || 0)}
      · 完成 ${Number(run.done_count || 0)}
      · 待 ${Number(run.pending_count || 0)}
      · 缺 ${Number(run.no_data_count || 0)}
      · 排 ${Number(run.excluded_count || 0)}
    </b>
  `;
}

function dimensionBar(label, value, max) {
  if (value === null || value === undefined) {
    return `<span><b>${label}</b><i><em style="width:0%"></em></i><small>缺失</small></span>`;
  }
  const pct = Math.max(0, Math.min(100, Number(value || 0) / max * 100));
  return `<span><b>${label}</b><i><em style="width:${pct}%"></em></i><small>${formatNumber(value, 1)}</small></span>`;
}

function setLoading(loading) {
  state.loading = loading;
  els.latestButton.disabled = loading;
  els.runButton.disabled = loading;
}

function clearPolling() {
  if (state.polling) window.clearInterval(state.polling);
  state.polling = null;
}

function syncUrl() {
  const params = new URLSearchParams({
    sectorType: state.sectorType,
    source: state.source,
    marketSource: state.marketSource,
    scanTime: state.scanTime
  });
  window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
}

function renderError(error) {
  showNotice(error.message || String(error));
}

function showNotice(message) {
  els.notice.hidden = false;
  els.notice.textContent = message;
}

function hideNotice() {
  els.notice.hidden = true;
  els.notice.textContent = "";
}

function emptyBlock(text) {
  return `<div class="empty-state">${escapeHtml(text)}</div>`;
}

function emptyTableRow(colspan, text) {
  return `<tr><td colspan="${colspan}">${escapeHtml(text)}</td></tr>`;
}

function statusLabel(value) {
  return { pending: "等待中", running: "运行中", done: "已完成", failed: "失败", empty: "暂无" }[value] || value || "--";
}

function stageLabel(value) {
  return {
    pending: "等待",
    loading_market: "读取市场",
    loading_sectors: "读取板块",
    loading_constituents: "读取前排",
    ai_reviewing: "AI审查",
    saving: "保存",
    done: "完成",
    failed: "失败"
  }[value] || value || "--";
}

function selectionModeLabel(value) {
  return { ai_reviewed: "AI审查", rules_fallback: "规则兜底", source_unavailable: "数据源不可用" }[value] || "规则基线";
}

function sectorTypeLabel(value) {
  return { all: "行业+概念", industry: "行业", concept: "概念" }[value] || value || "--";
}

function sourceLabel(value) {
  if (String(value || "").includes("mock")) return "Mock";
  if (String(value || "").includes("ths")) return "同花顺";
  if (String(value || "").includes("akshare")) return "AKShare/东财";
  return value || "--";
}

function dataQualityLabel(value) {
  return { ok: "数据正常", stale: "旧缓存", mock: "Mock 测试数据", insufficient: "数据不足", unavailable: "数据源不可用" }[value] || "待确认";
}

function sampleRoleLabel(value) {
  return { standard: "标准样本", early: "早于标准时点", late: "晚于标准时点" }[value] || "--";
}

function finalStatusLabel(value) {
  return {
    continuing: "主线延续",
    emerging: "启动发散",
    diverging: "分歧观察",
    overheated: "过热谨慎",
    fading: "退潮",
    insufficient: "证据不足"
  }[value] || value || "--";
}

function actionLabel(value) {
  return { maintain: "维持", promote: "提升", downgrade: "降级", reject: "拒绝", rules_fallback: "兜底" }[value] || value || "--";
}

function tradeabilityLabel(value) {
  return { normal: "可观察", chase_risk: "追高风险", hard_to_buy: "买入受限" }[value] || value || "--";
}

function leaderRoleLabel(value, index) {
  if (value === "overheated") return "过热前排";
  if (index === 0 || value === "core_front") return "核心前排";
  return {
    front_runner: "前排跟随",
    back_runner: "后排观察",
    confirming: "跟随确认"
  }[value] || "前排观察";
}

function resultTypeLabel(value) {
  return { rule_baseline: "规则基线", ai_reviewed: "AI入选", rules_fallback: "规则兜底", ai_rejected: "AI拒绝" }[value] || value || "--";
}

function outcomeStatusLabel(value) {
  return { done: "完成", pending: "待回填", no_data: "缺数据", excluded: "已排除" }[value] || value || "--";
}

function formatPct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function formatNumber(value, digits = 0) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toFixed(digits);
}

function formatMoney(value) {
  const number = Number(value || 0);
  if (!number) return "--";
  if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(1)}亿`;
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)}万`;
  return number.toFixed(0);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
