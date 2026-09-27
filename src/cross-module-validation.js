const state = {
  summary: null,
  conflicts: null,
  confluence: null,
  matrix: null,
  review: null,
  loading: false,
  reviewing: false
};

const els = {
  limitInput: document.querySelector("#sampleLimitInput"),
  refreshButton: document.querySelector("#refreshButton"),
  reviewButton: document.querySelector("#reviewButton"),
  notice: document.querySelector("#notice"),
  summaryGrid: document.querySelector("#summaryGrid"),
  summaryMeta: document.querySelector("#summaryMeta"),
  auditScopeText: document.querySelector("#auditScopeText"),
  auditWarningText: document.querySelector("#auditWarningText"),
  outcomeBasisMeta: document.querySelector("#outcomeBasisMeta"),
  outcomeBasisRows: document.querySelector("#outcomeBasisRows"),
  conflictMeta: document.querySelector("#conflictMeta"),
  conflictStatRows: document.querySelector("#conflictStatRows"),
  conflictRows: document.querySelector("#conflictRows"),
  confluenceMeta: document.querySelector("#confluenceMeta"),
  confluenceStatRows: document.querySelector("#confluenceStatRows"),
  confluenceRows: document.querySelector("#confluenceRows"),
  marketMeta: document.querySelector("#marketMeta"),
  marketRows: document.querySelector("#marketRows"),
  matrixMeta: document.querySelector("#matrixMeta"),
  matrixRows: document.querySelector("#matrixRows"),
  reviewMeta: document.querySelector("#reviewMeta"),
  reviewConclusion: document.querySelector("#reviewConclusion"),
  reviewConfidence: document.querySelector("#reviewConfidence"),
  reviewMissing: document.querySelector("#reviewMissing"),
  reviewRisk: document.querySelector("#reviewRisk"),
  findingRows: document.querySelector("#findingRows"),
  draftRows: document.querySelector("#draftRows"),
  reviewWarnings: document.querySelector("#reviewWarnings"),
  suggestionMeta: document.querySelector("#suggestionMeta"),
  suggestionRows: document.querySelector("#suggestionRows"),
  caseMeta: document.querySelector("#caseMeta"),
  caseRows: document.querySelector("#caseRows")
};

els.refreshButton.addEventListener("click", refreshAggregation);
els.reviewButton.addEventListener("click", runAiReview);
loadAll();

async function loadAll() {
  setLoading(true);
  const limit = readLimit();
  try {
    const [summary, conflicts, confluence, matrix] = await Promise.all([
      fetchJson(`/api/cross-module-validation/summary?limit=${limit}`),
      fetchJson(`/api/cross-module-validation/conflicts?limit=${limit}`),
      fetchJson(`/api/cross-module-validation/confluence?limit=${limit}`),
      fetchJson(`/api/cross-module-validation/module-matrix?limit=${limit}`)
    ]);
    state.summary = summary;
    state.conflicts = conflicts;
    state.confluence = confluence;
    state.matrix = matrix;
    renderAll();
  } catch (error) {
    showNotice(`跨模块复盘读取失败：${error.message || String(error)}`);
  } finally {
    setLoading(false);
  }
}

async function refreshAggregation() {
  setLoading(true);
  try {
    const payload = await postJson("/api/cross-module-validation/refresh", { limit: readLimit() });
    state.summary = payload;
    await loadAll();
    showNotice("已重新读取现有三套复盘结果并完成对齐。");
  } catch (error) {
    showNotice(`重新聚合失败：${error.message || String(error)}`);
  } finally {
    setLoading(false);
  }
}

async function runAiReview() {
  setReviewing(true);
  try {
    const payload = await postJson("/api/cross-module-validation/review", { limit: readLimit() });
    state.review = payload;
    renderReview(payload);
  } catch (error) {
    showNotice(`DeepSeek 审查失败：${error.message || String(error)}`);
  } finally {
    setReviewing(false);
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

function renderAll() {
  const summary = state.summary || {};
  const stats = summary.summary || {};
  showNotice((summary.warnings || []).join(" "));
  els.summaryMeta.textContent = summary.generatedAt ? `生成 ${summary.generatedAt.replace("T", " ")}` : "--";
  const scopeRange = summary.scope?.dateFrom && summary.scope?.dateTo ? `${summary.scope.dateFrom} 至 ${summary.scope.dateTo}` : "暂无区间";
  els.auditScopeText.textContent = `${formatInteger(summary.scope?.sampleCount)} 条审计事件`;
  els.auditWarningText.textContent = [
    `严格同日 ${formatInteger(stats.strictSameDayStockCount)} 条`,
    `近邻上下文 ${formatInteger(stats.nearbyStockContextCount)} 条`,
    `可优化 ${formatInteger(stats.optimizationReadyCount)} 条`
  ].join(" · ");
  els.summaryGrid.innerHTML = [
    ["样本区间", scopeRange, `${formatInteger(stats.effectiveTradeDayCount)} 个有效交易日`],
    ["审计事件", formatInteger(stats.eventCount), "按交易日+股票对齐"],
    ["冲突样本", formatInteger(stats.conflictCount), "只统计严格可比冲突"],
    ["共振样本", formatInteger(stats.confluenceCount), "只统计严格可比共振"],
    ["后验完成", formatInteger(stats.doneOutcomeCount), `完成率 ${formatRatePct(stats.doneRatePct)}`],
    ["数据不足", formatInteger(stats.dataInsufficientCount), `无后验 ${formatInteger(stats.noOutcomeCount)} · 不可比 ${formatInteger(stats.notComparableCount)}`],
    ["AI全市场", formatInteger(stats.aiSignalCount), "机会发现样本"],
    ["T+1", formatInteger(stats.t1SignalCount), "尾盘隔夜样本"],
    ["个股分析", formatInteger(stats.stockSignalCount), "买点审查样本"],
    ["平均收益", formatMaybePct(stats.avgReturnPct), "基于完成后验"],
    ["平均回撤", formatMaybePct(stats.avgDrawdownPct), "基于完成后验"],
    ["只读边界", "不调参", "DeepSeek 只解释，不改标签"]
  ].map(([label, value, sub]) => `
    <article>
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(sub)}</small>
    </article>
  `).join("");
  renderStatsRows(els.marketRows, summary.marketStateStats || state.matrix?.marketStateStats || [], "暂无市场状态统计");
  renderStatsRows(els.matrixRows, state.matrix?.moduleMatrix || summary.moduleMatrix || [], "暂无模块矩阵统计");
  renderStatsRows(els.outcomeBasisRows, summary.outcomeBasisStats || [], "暂无收益口径统计");
  renderStatsRows(els.conflictStatRows, summary.conflictStats || state.conflicts?.conflictStats || [], "暂无冲突统计");
  renderStatsRows(els.confluenceStatRows, summary.confluenceStats || state.confluence?.confluenceStats || [], "暂无共振统计");
  renderSuggestionRows(summary.ruleSuggestions || []);
  els.marketMeta.textContent = `${formatInteger((summary.marketStateStats || []).length)} 类市场状态`;
  els.matrixMeta.textContent = `${formatInteger((state.matrix?.moduleMatrix || []).length)} 个组合`;
  els.outcomeBasisMeta.textContent = `${formatInteger((summary.outcomeBasisStats || []).length)} 种收益口径`;
  renderCaseRows(els.conflictRows, state.conflicts?.cases || [], "暂无冲突样本");
  renderCaseRows(els.confluenceRows, state.confluence?.cases || [], "暂无共振样本");
  els.conflictMeta.textContent = `${formatInteger(state.conflicts?.rowCount)} 条样本`;
  els.confluenceMeta.textContent = `${formatInteger(state.confluence?.rowCount)} 条样本`;
  const cases = [...(state.conflicts?.cases || []).slice(0, 20), ...(state.confluence?.cases || []).slice(0, 20)];
  renderCaseRows(els.caseRows, cases, "暂无代表样本");
  els.caseMeta.textContent = `${formatInteger(cases.length)} 条代表样本`;
}

function renderStatsRows(target, rows, emptyText) {
  target.innerHTML = rows.length ? rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.label || row.key || "--")}<small>${escapeHtml(row.qualityNote || "")}</small></td>
      <td>${formatInteger(row.sampleCount)}</td>
      <td>${formatInteger(row.doneCount)}<small>${formatRatePct(row.doneRatePct)}</small></td>
      <td>${formatInteger(row.comparableCount)}<small>${escapeHtml(row.metricNote || "")}</small></td>
      <td>${formatRatePct(row.positiveRatePct)}</td>
      <td class="${numberClass(row.avgReturnPct)}">${formatMaybePct(row.avgReturnPct)}</td>
      <td class="${numberClass(row.avgExcessReturnPct)}">${formatMaybePct(row.avgExcessReturnPct)}</td>
      <td class="${numberClass(row.avgDrawdownPct)}">${formatMaybePct(row.avgDrawdownPct)}</td>
    </tr>
  `).join("") : `<tr><td colspan="8" class="empty">${escapeHtml(emptyText)}</td></tr>`;
}

function renderSuggestionRows(rows) {
  const suggestions = Array.isArray(rows) ? rows : [];
  els.suggestionMeta.textContent = `${formatInteger(suggestions.length)} 条`;
  els.suggestionRows.innerHTML = suggestions.length ? suggestions.map((row) => `
    <tr>
      <td>${escapeHtml(sourceLabel(row.source || "rule_stats"))}</td>
      <td>${escapeHtml(moduleLabel(row.targetModule))}</td>
      <td>${escapeHtml(row.actionType || "--")}</td>
      <td>${escapeHtml(row.evidence || "--")}</td>
      <td>${escapeHtml(row.suggestion || "--")}</td>
    </tr>
  `).join("") : `<tr><td colspan="5" class="empty">暂无规则统计建议</td></tr>`;
}

function renderCaseRows(target, rows, emptyText) {
  target.innerHTML = rows.length ? rows.slice(0, 120).map((row) => `
    <tr>
      <td>${escapeHtml(row.tradeDate || "--")}</td>
      <td><strong>${stockLink(row)}</strong><small>${escapeHtml(row.marketState || "unknown")}</small></td>
      <td>${casePatternText(row)}</td>
      <td>${signalText(row.aiFullMarket, "ai")}</td>
      <td>${signalText(row.t1, "t1")}</td>
      <td>${signalText(row.stockAnalysis, "stock")}</td>
      <td>${outcomeText(row.primaryOutcome)}</td>
    </tr>
  `).join("") : `<tr><td colspan="7" class="empty">${escapeHtml(emptyText)}</td></tr>`;
}

function renderReview(payload) {
  const review = payload.review || {};
  els.reviewMeta.textContent = `${sourceLabel(review.aiMode || payload.source)} · ${payload.generatedAt ? payload.generatedAt.replace("T", " ") : "--"}`;
  els.reviewConclusion.textContent = review.reviewConclusion || "--";
  els.reviewConfidence.textContent = `置信度：${confidenceLabel(review.confidence)}`;
  els.reviewMissing.textContent = compactList(review.missingEvidence);
  els.reviewRisk.textContent = compactList(review.riskWarnings);
  const findings = Array.isArray(review.keyFindings) ? review.keyFindings : [];
  els.findingRows.innerHTML = findings.length ? findings.map((item) => `
    <tr>
      <td>${escapeHtml(item.finding || item.pattern || "--")}<small>${escapeHtml(item.pattern || "")}</small></td>
      <td>${escapeHtml(item.evidence || "--")}</td>
      <td>${escapeHtml((item.affectedModules || []).join(" / ") || "--")}</td>
      <td>${escapeHtml(item.risk || "--")}</td>
    </tr>
  `).join("") : `<tr><td colspan="4" class="empty">暂无 DeepSeek 结构化发现</td></tr>`;
  const drafts = Array.isArray(review.optimizationDrafts) ? review.optimizationDrafts : [];
  els.draftRows.innerHTML = drafts.length ? drafts.map((item) => `
    <tr>
      <td>${escapeHtml(moduleLabel(item.targetModule))}</td>
      <td>${escapeHtml(item.actionType || "--")}</td>
      <td>${escapeHtml(item.suggestion || "--")}<small>${escapeHtml(item.blockedAutoApplyReason || "")}</small></td>
      <td>${item.requiresUserConfirmation === false ? "否" : "是"}</td>
    </tr>
  `).join("") : `<tr><td colspan="4" class="empty">暂无待确认建议</td></tr>`;
  els.reviewWarnings.innerHTML = (review.riskWarnings || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("");
}

function signalText(signal, type) {
  if (!signal?.present) return `<span class="muted">未出现</span>`;
  if (type === "stock") {
    const decision = signal.decision || "watch";
    const align = alignmentText(signal.alignment);
    return `<strong>${escapeHtml(decisionLabel(decision))}</strong><small>置信 ${formatInteger(signal.confidence)} · ${escapeHtml(align || "未标记")}${align ? " · " : ""}${escapeHtml(signal.reason || "")}</small>`;
  }
  const selected = signal.selected ? "选中" : "未选";
  const score = signal.score == null ? "--" : formatNumber(signal.score);
  const reason = signal.reason || signal.risk || signal.buyPlan || "";
  return `<strong>${escapeHtml(selected)}</strong><small>${escapeHtml(signal.candidateType || "--")} · 分 ${score} · ${escapeHtml(reason)}</small>`;
}

function casePatternText(row) {
  const diagnosis = row.diagnosis || {};
  const quality = row.quality || diagnosis.comparability || {};
  const optimize = quality.canUseForOptimization ? "可用于优化观察" : "仅观察";
  return `
    <strong>${escapeHtml(row.patternLabel || row.pattern || "--")}</strong>
    <small>${escapeHtml(diagnosis.labelText || "")}${diagnosis.labelText ? " · " : ""}${escapeHtml(optimize)}</small>
    <small>${escapeHtml(diagnosis.whyClassified || diagnosis.summary || "")}</small>
  `;
}

function alignmentText(alignment) {
  if (!alignment || !alignment.mode) return "";
  if (alignment.mode === "exact_same_day") return "同日";
  const gap = Number(alignment.dateGapDays);
  const gapText = Number.isFinite(gap) ? `${gap > 0 ? "+" : ""}${gap}日` : "近邻";
  return `近邻分析${gapText}`;
}

function outcomeText(outcome) {
  if (!outcome || !Object.keys(outcome).length) return `<span class="muted">暂无后验</span>`;
  return `<strong class="${numberClass(outcome.returnPct)}">${formatMaybePct(outcome.returnPct)}</strong><small>${escapeHtml(outcome.basis || outcomeBasisLabel(outcome))} · ${escapeHtml(outcome.status || "--")} · ${escapeHtml(outcome.label || "")}</small>`;
}

function stockLink(row) {
  const code = row.code || "";
  const name = row.name || "--";
  const href = `./index.html?stock=${encodeURIComponent(code)}&analyze=1#stock`;
  return `<a class="stock-analysis-name-link" href="${href}">${escapeHtml(name)} ${escapeHtml(code)}</a>`;
}

function readLimit() {
  const value = Number(els.limitInput.value || 600);
  if (!Number.isFinite(value)) return 600;
  return Math.max(50, Math.min(3000, Math.floor(value)));
}

function setLoading(loading) {
  state.loading = loading;
  els.refreshButton.disabled = loading;
  els.refreshButton.innerHTML = loading
    ? `<span class="button-spinner"></span>聚合中`
    : `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.7 6.3A8 8 0 1 0 20 12h-2a6 6 0 1 1-1.8-4.3L13 11h8V3l-3.3 3.3Z"/></svg>重新聚合`;
}

function setReviewing(reviewing) {
  state.reviewing = reviewing;
  els.reviewButton.disabled = reviewing;
  els.reviewButton.innerHTML = reviewing
    ? `<span class="button-spinner"></span>审查中`
    : `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 3 8l9 5 9-5-9-5Zm0 7.2L5.3 7 12 3.8 18.7 7 12 10.2ZM3 11v6l9 5 9-5v-6l-9 5-9-5Z"/></svg>生成 DeepSeek 审查`;
}

function showNotice(message) {
  els.notice.hidden = !message;
  els.notice.textContent = message || "";
}

function formatInteger(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toFixed(1);
}

function formatMaybePct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function formatRatePct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number.toFixed(1)}%`;
}

function numberClass(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "";
  return number > 0 ? "positive" : "negative";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function compactList(value) {
  const rows = Array.isArray(value) ? value : [];
  return rows.length ? rows.slice(0, 2).join("；") : "--";
}

function confidenceLabel(value) {
  return { high: "高", medium: "中", low: "低" }[value] || value || "--";
}

function sourceLabel(value) {
  return {
    deepseek: "DeepSeek",
    ai_unavailable: "AI 未配置",
    rules_fallback: "本地兜底",
    rule_stats: "规则统计"
  }[value] || value || "--";
}

function outcomeBasisLabel(outcome) {
  if (!outcome?.source) return "--";
  if (outcome.source === "t1") return "T+1次日收盘收益";
  if (outcome.source === "aiFullMarket") return `AI全市场${outcome.horizon || "--"}日收益`;
  if (outcome.source === "stockAnalysis") return `个股分析${outcome.horizon || "--"}日方向收益`;
  return outcome.source;
}

function moduleLabel(value) {
  return {
    ai_full_market: "AI 全市场",
    t1: "T+1",
    stock_analysis: "个股分析",
    cross_module: "跨模块"
  }[value] || value || "--";
}

function decisionLabel(value) {
  return { buy: "买入", watch: "观察", avoid: "回避" }[value] || value || "--";
}
