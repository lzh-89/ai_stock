const state = {
  limit: 60,
  statusFilter: "reviewable",
  runId: 0,
  payload: null,
  loading: false,
  loadGeneration: 0,
  actionNotice: "",
  shadowFamily: "legacy_tail_rule",
  shadowRole: "tail_rule",
  shadowWindow: "all",
  shadowPayload: null,
  shadowLoadGeneration: 0
};

const els = {
  statusSelect: document.querySelector("#statusSelect"),
  limitInput: document.querySelector("#limitInput"),
  refreshButton: document.querySelector("#refreshButton"),
  verifyRunButton: document.querySelector("#verifyRunButton"),
  backfillMinuteButton: document.querySelector("#backfillMinuteButton"),
  notice: document.querySelector("#notice"),
  summaryGrid: document.querySelector("#summaryGrid"),
  reviewNote: document.querySelector("#reviewNote"),
  shadowFamilyTabs: document.querySelector("#shadowFamilyTabs"),
  shadowRoleSelect: document.querySelector("#shadowRoleSelect"),
  shadowWindowSelect: document.querySelector("#shadowWindowSelect"),
  shadowAnalyticsNotice: document.querySelector("#shadowAnalyticsNotice"),
  shadowAnalyticsMetrics: document.querySelector("#shadowAnalyticsMetrics"),
  shadowAnalyticsProgress: document.querySelector("#shadowAnalyticsProgress"),
  shadowGateDiagnostics: document.querySelector("#shadowGateDiagnostics"),
  shadowAnalyticsRows: document.querySelector("#shadowAnalyticsRows"),
  optimizationStatsMeta: document.querySelector("#optimizationStatsMeta"),
  optimizationStatsGrid: document.querySelector("#optimizationStatsGrid"),
  exitTimingStatsRows: document.querySelector("#exitTimingStatsRows"),
  metricNotes: document.querySelector("#metricNotes"),
  runList: document.querySelector("#runList"),
  runListMeta: document.querySelector("#runListMeta"),
  detailEyebrow: document.querySelector("#detailEyebrow"),
  detailTitle: document.querySelector("#detailTitle"),
  actionProgress: document.querySelector("#actionProgress"),
  actionProgressTitle: document.querySelector("#actionProgressTitle"),
  actionProgressHint: document.querySelector("#actionProgressHint"),
  detailMetrics: document.querySelector("#detailMetrics"),
  usageExplanation: document.querySelector("#usageExplanation"),
  pickSummary: document.querySelector("#pickSummary"),
  shadowPanel: document.querySelector("#shadowPanel"),
  outcomeRows: document.querySelector("#outcomeRows"),
  outcomeMeta: document.querySelector("#outcomeMeta"),
  baselinePanel: document.querySelector("#baselinePanel"),
  actionModal: document.querySelector("#actionModal"),
  actionModalTone: document.querySelector("#actionModalTone"),
  actionModalTitle: document.querySelector("#actionModalTitle"),
  actionModalMessage: document.querySelector("#actionModalMessage"),
  actionModalClose: document.querySelector("#actionModalClose"),
  actionModalOk: document.querySelector("#actionModalOk")
};

function init() {
  const params = new URLSearchParams(window.location.search);
  state.limit = clampNumber(params.get("limit"), 10, 200, 60);
  state.statusFilter = normalizeReviewFilter(params.get("reviewStatus") || params.get("status") || "reviewable");
  state.runId = clampNumber(params.get("runId"), 0, 999999, 0);
  state.shadowFamily = normalizeShadowFamily(params.get("shadowFamily"));
  state.shadowRole = params.get("shadowRole") || defaultShadowRole(state.shadowFamily);
  state.shadowWindow = normalizeShadowWindow(params.get("shadowWindow"));
  els.limitInput.value = String(state.limit);
  els.statusSelect.value = state.statusFilter;
  if (els.shadowWindowSelect) els.shadowWindowSelect.value = state.shadowWindow;
  populateShadowRoles();
  bindEvents();
  loadReview();
  loadShadowAnalytics();
}

function bindEvents() {
  els.refreshButton.addEventListener("click", () => {
    state.actionNotice = "";
    loadReview();
    loadShadowAnalytics();
  });
  els.verifyRunButton.addEventListener("click", verifySelectedRun);
  els.backfillMinuteButton?.addEventListener("click", backfillSelectedRunMinutes);
  els.actionModalClose?.addEventListener("click", hideActionModal);
  els.actionModalOk?.addEventListener("click", hideActionModal);
  els.actionModal?.addEventListener("click", (event) => {
    if (event.target === els.actionModal) hideActionModal();
  });
  els.limitInput.addEventListener("change", () => {
    state.limit = clampNumber(els.limitInput.value, 10, 200, 60);
    els.limitInput.value = String(state.limit);
    state.actionNotice = "";
    loadReview({ keepSelection: true });
  });
  els.statusSelect.addEventListener("change", () => {
    state.statusFilter = normalizeReviewFilter(els.statusSelect.value || "");
    state.actionNotice = "";
    loadReview({ keepSelection: true });
  });
  els.runList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-run-id]");
    if (!button) return;
    state.runId = Number(button.dataset.runId || 0);
    loadReview({ keepSelection: true });
  });
  els.shadowFamilyTabs?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-shadow-family]");
    if (!button) return;
    state.shadowFamily = normalizeShadowFamily(button.dataset.shadowFamily);
    state.shadowRole = defaultShadowRole(state.shadowFamily);
    populateShadowRoles();
    loadShadowAnalytics();
  });
  els.shadowRoleSelect?.addEventListener("change", () => {
    state.shadowRole = els.shadowRoleSelect.value || defaultShadowRole(state.shadowFamily);
    loadShadowAnalytics();
  });
  els.shadowWindowSelect?.addEventListener("change", () => {
    state.shadowWindow = normalizeShadowWindow(els.shadowWindowSelect.value);
    loadShadowAnalytics();
  });
}

const SHADOW_ROLES = {
  legacy_tail_rule: [{ value: "tail_rule", label: "规则尾盘" }],
  legacy_tail_ai: [{ value: "tail_ai", label: "AI 尾盘" }],
  policy_experiment: [
    { value: "policy_market_gate", label: "市场门禁" },
    { value: "policy_risk_gate", label: "风险门禁" },
    { value: "policy_diversity", label: "组合去同质化" },
    { value: "policy_rank3", label: "第三槽" },
    { value: "policy_net_expectation", label: "净期望排序" },
    { value: "policy_adaptive_exit", label: "动态退出" }
  ]
};

init();

function populateShadowRoles() {
  if (!els.shadowRoleSelect) return;
  const roles = SHADOW_ROLES[state.shadowFamily] || [];
  if (!roles.some((item) => item.value === state.shadowRole)) state.shadowRole = roles[0]?.value || "";
  els.shadowRoleSelect.innerHTML = roles.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
  els.shadowRoleSelect.value = state.shadowRole;
}

async function loadShadowAnalytics() {
  const generation = ++state.shadowLoadGeneration;
  if (els.shadowAnalyticsNotice) els.shadowAnalyticsNotice.textContent = "正在重建并加载影子统计...";
  try {
    const params = new URLSearchParams({
      shadowFamily: state.shadowFamily,
      shadowRole: state.shadowRole,
      window: state.shadowWindow,
      outcomeScenario: "open_15m"
    });
    const response = await fetch(`/api/t1-overnight-selection/shadow-analytics/summary?${params.toString()}`);
    const payload = await response.json().catch(() => ({}));
    if (generation !== state.shadowLoadGeneration) return;
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.shadowPayload = payload;
    renderShadowAnalytics();
    syncUrl();
  } catch (error) {
    if (generation !== state.shadowLoadGeneration) return;
    state.shadowPayload = null;
    if (els.shadowAnalyticsNotice) els.shadowAnalyticsNotice.textContent = `影子统计加载失败：${error?.message || error}`;
    if (els.shadowAnalyticsMetrics) els.shadowAnalyticsMetrics.innerHTML = "";
    if (els.shadowGateDiagnostics) {
      els.shadowGateDiagnostics.innerHTML = "";
      els.shadowGateDiagnostics.classList.add("hidden");
    }
    if (els.shadowAnalyticsRows) els.shadowAnalyticsRows.innerHTML = `<tr><td colspan="7" class="empty">暂时无法加载影子统计。</td></tr>`;
  }
}

function renderShadowAnalytics() {
  const payload = state.shadowPayload || {};
  const report = (payload.roles || [])[0] || {};
  const facts = [...(payload.facts || [])]
    .filter((fact) => !report.strategyPolicyVersion || fact.strategyPolicyVersion === report.strategyPolicyVersion)
    .reverse();
  els.shadowFamilyTabs?.querySelectorAll("[data-shadow-family]").forEach((button) => {
    const active = button.dataset.shadowFamily === state.shadowFamily;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  const source = payload.sourceType === "historical_backfill" ? "历史数据已重建" : "从新策略版本开始前向累计";
  const coverage = report.coveragePct == null ? "暂无覆盖数据" : `覆盖率 ${formatRate(report.coveragePct)}`;
  if (els.shadowAnalyticsNotice) {
    els.shadowAnalyticsNotice.textContent = `${payload.description || "各影子角色独立统计。"} ${source}；${coverage}。最后计算 ${payload.generatedAt || "--"}。`;
  }
  if (els.shadowAnalyticsMetrics) {
    els.shadowAnalyticsMetrics.innerHTML = [
      metricCard("有效交易日", formatInteger(report.effectiveDays || 0), `目标日 ${formatInteger(report.targetTradeDays || 0)} · 命中 ${formatInteger(report.hitDays || 0)}`),
      metricCard("累计净收益", formatPct(report.cumulativeReturnPct), "三槽组合复利，空槽按现金"),
      metricCard("日均净收益", formatPct(report.avgDailyNetReturnPct), `中位数 ${formatPct(report.medianDailyNetReturnPct)}`),
      metricCard("持仓日正收益率", formatRate(report.positiveHoldingDayRate), `盈亏比 ${formatNumber(report.profitLossRatio, 2)}`),
      metricCard("最大回撤", formatPct(report.maxDrawdownPct), `最差 10% ${formatPct(report.worst10Pct)}`),
      metricCard("同日配对改善", formatPct(report.avgPairedDeltaPct), report.comparisonLabel || "诊断角色不解释为因果增益"),
      metricCard("避免亏损", formatPct(report.avoidedLossPct), `错过盈利 ${formatPct(report.missedProfitPct)}`),
      metricCard("评审状态", report.reviewStatusLabel || "收集中", `版本 ${report.strategyPolicyVersion || "--"}`)
    ].join("");
  }
  const progress = report.progress || {};
  if (els.shadowAnalyticsProgress) {
    const target = Number(progress.nextTargetDays || 20);
    const current = Number(progress.effectiveDays || 0);
    const width = Math.max(0, Math.min(100, target ? current / target * 100 : 0));
    els.shadowAnalyticsProgress.innerHTML = `
      <div><strong>${escapeHtml(report.reviewStatusLabel || "收集中")}</strong><span>${formatInteger(current)} / ${formatInteger(target)} 个有效交易日，还差 ${formatInteger(progress.remainingDays || 0)} 日</span></div>
      <span class="shadow-progress-track"><i style="width:${width.toFixed(1)}%"></i></span>
    `;
  }
  renderShadowGateDiagnostics(report);
  if (els.shadowAnalyticsRows) {
    els.shadowAnalyticsRows.innerHTML = facts.length
      ? facts.slice(0, 120).map((fact) => `
        <tr>
          <td>${escapeHtml(fact.recommendationDate || "--")}</td>
          <td><a href="?runId=${formatIntegerRaw(fact.canonicalRunId)}">#${formatInteger(fact.canonicalRunId)}</a></td>
          <td>${formatInteger(fact.holdingCount || 0)} / ${formatInteger(fact.cashSlotCount || 0)}</td>
          <td class="${returnTone(fact.treatmentNetReturnPct)}">${formatPct(fact.treatmentNetReturnPct)}</td>
          <td>${formatPct(fact.controlNetReturnPct)}</td>
          <td class="${returnTone(fact.pairedDailyDeltaPct)}">${formatPct(fact.pairedDailyDeltaPct)}</td>
          <td><span class="status-pill ${fact.status === "ok" ? "good" : "warn"}">${fact.status === "ok" ? "完整" : "数据不足"}</span><small>${fact.coveragePct == null ? "" : formatRate(fact.coveragePct)}</small></td>
        </tr>
      `).join("")
      : `<tr><td colspan="7" class="empty">当前角色还没有可用数据，将从符合条件的 Run 自动累计。</td></tr>`;
  }
}

function renderShadowGateDiagnostics(report) {
  if (!els.shadowGateDiagnostics) return;
  const diagnostics = Array.isArray(report.gateDiagnostics) ? report.gateDiagnostics : [];
  const isPolicy = state.shadowFamily === "policy_experiment";
  const isRiskGate = isPolicy && state.shadowRole === "policy_risk_gate" && diagnostics.length > 0;
  const isDiversity = isPolicy && state.shadowRole === "policy_diversity";
  const isRank3 = isPolicy && state.shadowRole === "policy_rank3";
  const isNetExpectation = isPolicy && state.shadowRole === "policy_net_expectation";
  const isAdaptiveExit = isPolicy && state.shadowRole === "policy_adaptive_exit";
  const visible = isRiskGate || isDiversity || isRank3 || isNetExpectation || isAdaptiveExit;
  els.shadowGateDiagnostics.classList.toggle("hidden", !visible);
  if (!visible) {
    els.shadowGateDiagnostics.innerHTML = "";
    return;
  }
  if (isDiversity) {
    const detail = report.diversityDiagnostics || {};
    const conflicts = detail.conflictCounts || {};
    els.shadowGateDiagnostics.innerHTML = `
      <div class="shadow-gate-diagnostics-head">
        <strong>组合独立性诊断</strong>
        <span>仅比较 P2 影子组合，不改变正式推荐</span>
      </div>
      <div class="shadow-gate-diagnostic-list role-metrics">
        ${shadowDiagnosticMetric("组合替换", formatInteger(detail.replacementCount || 0), `冲突挡下 ${formatInteger(detail.blockedCandidateCount || 0)} 只`)}
        ${shadowDiagnosticMetric("第二槽增量", formatPct(detail.avgSecondSlotIncrementPct), `中位数 ${formatPct(detail.medianSecondSlotIncrementPct)}`)}
        ${shadowDiagnosticMetric("第二槽胜率", formatRate(detail.positiveSecondSlotRate), `${formatInteger(detail.secondSlotEffectiveDays || 0)} 个有效日`)}
        ${shadowDiagnosticMetric("同日失败率", formatRate(detail.sameDayFailureRate), `${formatInteger(detail.sameDayFailureSampleDays || 0)} 个可比日`)}
        ${shadowDiagnosticMetric("组合内相关性", formatCorrelation(detail.portfolioReturnCorrelation), `样本 ${formatInteger(detail.correlationSampleDays || 0)} 日${detail.portfolioReturnCorrelation == null ? "，不足 5 日不计算" : ""}`)}
        ${shadowDiagnosticMetric("冲突构成", formatInteger((conflicts.sameIndustry || 0) + (conflicts.sameDriverGroup || 0)), `同行业 ${formatInteger(conflicts.sameIndustry || 0)} · 同驱动 ${formatInteger(conflicts.sameDriverGroup || 0)}`)}
        ${shadowDiagnosticMetric("身份缺失", formatInteger((conflicts.industryIdentityUnavailable || 0) + (conflicts.driverIdentityUnavailable || 0)), `行业 ${formatInteger(conflicts.industryIdentityUnavailable || 0)} · 驱动 ${formatInteger(conflicts.driverIdentityUnavailable || 0)}`)}
      </div>
    `;
    return;
  }
  if (isRank3) {
    const detail = report.rank3Diagnostics || {};
    els.shadowGateDiagnostics.innerHTML = `
      <div class="shadow-gate-diagnostics-head">
        <strong>第三槽增量诊断</strong>
        <span>Rank1+Rank2+现金 对照 Rank1+Rank2+Rank3</span>
      </div>
      <div class="shadow-gate-diagnostic-list role-metrics">
        ${shadowDiagnosticMetric("有效交易日", formatInteger(detail.effectiveDays || 0), "只认冻结排序的原始 Rank 3")}
        ${shadowDiagnosticMetric("平均增量", formatPct(detail.avgIncrementPct), `中位数 ${formatPct(detail.medianIncrementPct)}`)}
        ${shadowDiagnosticMetric("增量胜率", formatRate(detail.positiveIncrementRate), `最差增量 ${formatPct(detail.worstIncrementPct)}`)}
        ${shadowDiagnosticMetric("Rank 3 单股", formatPct(detail.avgSlotReturnPct), `ETF 超额 ${formatPct(detail.avgEtfExcessPct)}`)}
        ${shadowDiagnosticMetric("三只同跌", formatRate(detail.sameDayFailureRate), `${formatInteger(detail.sameDayFailureSampleDays || 0)} 个可比日`)}
        ${shadowDiagnosticMetric("与前两槽相关性", formatCorrelation(detail.baseReturnCorrelation), `样本 ${formatInteger(detail.correlationSampleDays || 0)} 日${detail.baseReturnCorrelation == null ? "，不足 5 日不计算" : ""}`)}
      </div>
    `;
    return;
  }
  if (isNetExpectation) {
    const detail = report.netExpectationDiagnostics || {};
    const remaining = Math.max(0, Number(detail.minimumTotalSamples || 150) - Number(detail.trainingSampleCount || 0));
    els.shadowGateDiagnostics.innerHTML = `
      <div class="shadow-gate-diagnostics-head">
        <strong>保守净期望模型</strong>
        <span>${detail.rankingReady ? "已达到影子排序门槛，仍不影响正式推荐" : "样本积累中，仅观察，不生成可交易排序"}</span>
      </div>
      <div class="shadow-gate-diagnostic-list role-metrics">
        ${shadowDiagnosticMetric("执行训练样本", formatInteger(detail.trainingSampleCount || 0), `${formatInteger(detail.trainingTradeDays || 0)} 个交易日 · 还差 ${formatInteger(remaining)} 笔`)}
        ${shadowDiagnosticMetric("稳定分箱", `${formatInteger(detail.readyBinCount || 0)} / ${formatInteger(detail.binCount || 0)}`, `每箱至少 ${formatInteger(detail.minimumBinSamples || 30)} 笔`)}
        ${shadowDiagnosticMetric("校准入样", formatInteger(detail.calibrationSelectedCount || 0), `其中概率样本 ${formatInteger(detail.calibrationProbabilityCount || 0)} 笔`)}
        ${shadowDiagnosticMetric("120 池加权收益", formatPct(detail.avgEligiblePoolWeightedReturnPct), "按冻结入样概率估计")}
        ${shadowDiagnosticMetric("当前策略", formatPct(detail.avgCurrentRuleReturnPct), `随机基线 ${formatPct(detail.avgRandomBaselineReturnPct)}`)}
        ${shadowDiagnosticMetric("ETF 基准", formatPct(detail.avgEtfReturnPct), `预算未覆盖 ${formatInteger(detail.budgetLimitedDays || 0)} 日`)}
      </div>
    `;
    return;
  }
  if (isAdaptiveExit) {
    const detail = report.adaptiveExitDiagnostics || {};
    const statuses = detail.executionStatusCounts || {};
    const branches = detail.branchCounts || {};
    els.shadowGateDiagnostics.innerHTML = `
      <div class="shadow-gate-diagnostics-head">
        <strong>逐分钟动态退出诊断</strong>
        <span>只改变卖出路径，固定 open_15m 继续作为主 KPI</span>
      </div>
      <div class="shadow-gate-diagnostic-list role-metrics">
        ${shadowDiagnosticMetric("完整成交率", formatRate(detail.completeExecutionRate), `${formatInteger(detail.completeExecutionCount || 0)} / ${formatInteger(detail.holdingOutcomeCount || 0)} 笔`)}
        ${shadowDiagnosticMetric("未完整执行", formatInteger(detail.incompleteExecutionCount || 0), `部分 ${formatInteger(statuses.partial_execution || 0)} · 不可成交 ${formatInteger(statuses.market_unexecutable || 0)}`)}
        ${shadowDiagnosticMetric("低开风险退出", formatInteger(branches.opening_risk_exit || 0), "依赖合格的 09:31 冻结证据")}
        ${shadowDiagnosticMetric("破位退出", formatInteger(branches.breakdown_exit || 0), "09:35 完成决策，最早下一分钟成交")}
        ${shadowDiagnosticMetric("强开分腿", formatInteger(branches.strong_open_split_exit || 0), "50% 早退 · 50% 09:45 后")}
        ${shadowDiagnosticMetric("盯市收益", formatPct(detail.avgMarkToMarketReturnPct), "未成交余量不从样本中删除")}
      </div>
    `;
    return;
  }
  const modeLabels = { off: "关闭", shadow: "影子", enforced: "正式" };
  els.shadowGateDiagnostics.innerHTML = `
    <div class="shadow-gate-diagnostics-head">
      <strong>负向门禁诊断</strong>
      <span>累计挡下 ${formatInteger(report.gateBlockedCandidateCount || 0)} 个候选，仅影响影子处理组</span>
    </div>
    <div class="shadow-gate-diagnostic-list">
      ${diagnostics.map((item) => `
        <div class="shadow-gate-diagnostic-item">
          <span>${escapeHtml(item.label || item.gateKey || "--")}</span>
          <strong>${formatInteger(item.hitCount || 0)} / ${formatInteger(item.evaluatedCount || 0)}</strong>
          <small>${escapeHtml(modeLabels[item.configuredMode] || item.configuredMode || "--")} · 证据覆盖 ${formatRate(item.evidenceCoveragePct)}</small>
        </div>
      `).join("")}
    </div>
  `;
}

function shadowDiagnosticMetric(label, value, note) {
  return `
    <div class="shadow-gate-diagnostic-item">
      <span>${escapeHtml(label || "--")}</span>
      <strong>${escapeHtml(value ?? "--")}</strong>
      <small>${escapeHtml(note || "--")}</small>
    </div>
  `;
}

function formatCorrelation(value) {
  if (value == null || value === "") return "--";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : "--";
}

async function loadReview({ keepSelection = false } = {}) {
  const generation = ++state.loadGeneration;
  setLoading(true);
  try {
    const params = new URLSearchParams({ limit: String(state.limit) });
    if (state.statusFilter) params.set("reviewStatus", state.statusFilter);
    if ((keepSelection || state.runId > 0) && state.runId > 0) params.set("runId", String(state.runId));
    const response = await fetch(`/api/t1-overnight-selection/review?${params.toString()}`);
    const payload = await response.json().catch(() => ({}));
    if (generation !== state.loadGeneration) return;
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.payload = payload;
    state.runId = Number(payload.selectedRunId || payload.selectedRun?.id || state.runId || 0);
    const targetRun = firstMatchingRun(payload.runs || [], payload.selectedRun);
    if (targetRun && Number(targetRun.id) !== state.runId) {
      state.runId = Number(targetRun.id || 0);
      return loadReview({ keepSelection: true });
    }
    render();
    syncUrl();
  } catch (error) {
    if (generation === state.loadGeneration) renderError(error);
  } finally {
    if (generation === state.loadGeneration) setLoading(false);
  }
}

async function verifySelectedRun() {
  const runId = Number(state.payload?.selectedRun?.id || state.runId || 0);
  if (!runId) {
    renderNotice("请先选择一个 run，再刷新验证。");
    return;
  }
  startLongAction("正在刷新该 run 验证", "正在重新计算次日15分钟结果，可能需要几十秒。");
  try {
    const response = await fetch(`/api/t1-overnight-selection/runs/${runId}/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.actionNotice = payload.reason || outcomeStatusHint(payload.status, state.payload?.selectedRun);
    await loadReview({ keepSelection: true });
    showActionModal({
      tone: "good",
      title: "刷新验证完成",
      message: state.actionNotice || "该 run 的验证结果已刷新。"
    });
  } catch (error) {
    const message = error?.message || String(error || "刷新验证失败");
    state.actionNotice = message;
    renderError(error);
    showActionModal({ tone: "bad", title: "刷新验证失败", message });
  } finally {
    finishLongAction();
  }
}

async function backfillSelectedRunMinutes() {
  const runId = Number(state.payload?.selectedRun?.id || state.runId || 0);
  if (!runId) {
    renderNotice("请先选择一个 run，再补拉验证分钟线。");
    return;
  }
  startLongAction("正在补拉验证分钟线", "正在拉取目标验证日分钟线并重新计算标签，可能需要一段时间。");
  try {
    const response = await fetch(`/api/t1-overnight-selection/runs/${runId}/backfill-validation-minutes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: "final", force: false, period: "1m" })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.actionNotice = formatBackfillResultMessage(payload);
    await loadReview({ keepSelection: true });
    showActionModal({
      tone: payload.failed ? "warn" : "good",
      title: payload.failed ? "补拉完成，但有失败" : "补拉完成",
      message: state.actionNotice
    });
  } catch (error) {
    const message = error?.message || String(error || "补拉验证分钟线失败");
    state.actionNotice = message;
    renderError(error);
    showActionModal({ tone: "bad", title: "补拉失败", message });
  } finally {
    finishLongAction();
  }
}

function render() {
  const payload = state.payload || {};
  const runs = payload.runs || [];
  const filteredRuns = runs.filter(matchesFilter);
  const selected = payload.selectedRun || null;
  renderSummary(runs, selected);
  renderReviewNote(payload.reviewStats || {}, payload.validationAutoFreeze || {});
  renderOptimizationStats(payload.optimizationStats || {});
  renderRunList(filteredRuns, runs.length, selected);
  renderDetail(selected);
  renderBaseline(selected);
  window.dispatchEvent(new CustomEvent("t1-review-run", { detail: { runId: Number(selected?.id || 0) } }));
  let notice = "";
  if (!runs.length) notice = "暂无 T+1 run。先在 T+1 筛选页生成推荐后，这里会显示复盘记录。";
  else if (!filteredRuns.length) notice = "当前筛选条件下没有 run；可以切到“全部”查看其它记录。";
  else if (selected?.reviewStatus === "waiting") notice = selected.reviewStatusReason || "当前选中的 run 还在等待验证。";
  else if (selected?.reviewStatus === "diagnostic" || selected?.reviewStatus === "unusable") notice = selected.reviewStatusReason || "当前 run 只用于排查问题，不拿来看结果或优化策略。";
  renderNotice(state.actionNotice || notice);
}

function renderSummary(runs, selected) {
  const stats = state.payload?.reviewStats || {};
  els.summaryGrid.innerHTML = [
    metricCard("可用于优化", formatInteger(stats.displayLearnableRuns || 0), `可用股票 ${formatInteger(stats.learningEligibleOutcomes || 0)} 个`),
    metricCard("可看结果", formatInteger(stats.displayReviewableRuns || 0), "能看次日15分钟表现"),
    metricCard("部分缺数据", formatInteger(stats.displayPartialRuns || 0), "有些股票缺15分钟价格"),
    metricCard("等待数据", formatInteger(stats.displayWaitingRuns || 0), "还没到验证时间或需刷新"),
    metricCard("仅排查", formatInteger(stats.displayDiagnosticRuns ?? stats.displayUnusableRuns ?? 0), "周末/异常/旧规则记录"),
    metricCard("open_15m均值", formatPct(stats.avgNetReturnPct), "只看可用于优化样本"),
    metricCard("open_15m成功率", formatRate(stats.successRate), `强成功 ${formatRate(stats.strongSuccessRate)}`),
    metricCard("选中 run", selected ? `#${formatInteger(selected.id)}` : "--", selected ? `${selected.recommendationDate || "--"} ${selected.scanTime || ""}` : "未选择"),
    metricCard("选中状态", selected ? reviewStatusLabel(selected) : "--", selected?.reviewStatusReason || "左侧选择 run 后显示")
  ].join("");
}

function renderReviewNote(stats, autoFreeze = {}) {
  const config = autoFreeze.config || {};
  const nextSlots = autoFreeze.nextSlots || [];
  const autoText = config.enabled
    ? `验证日分钟线自动冻结已开启${autoFreeze.schedulerAlive ? "" : "，调度器未运行"}；时间点 ${((config.scheduleTimes || []).join(" / ") || "--")}；下一次 ${nextSlots[0] || "今日已处理或未到交易日"}。`
    : "验证日分钟线自动冻结未开启。";
  const lastText = autoFreeze.lastMessage ? `最近状态：${autoFreeze.lastMessage}` : "最近状态：暂无自动冻结记录。";
  els.reviewNote.innerHTML = `
    <strong>复盘口径</strong>
    <span>${escapeHtml(stats.note || "页面按能不能使用来分：可用于优化、可看结果、部分缺数据、等待数据、仅排查。")}</span>
    <strong>验证数据</strong>
    <span>${escapeHtml(autoText)} ${escapeHtml(lastText)}</span>
  `;
}

function renderOptimizationStats(stats) {
  if (!els.optimizationStatsGrid || !els.exitTimingStatsRows) return;
  const primary = stats.primary || {};
  els.optimizationStatsMeta.textContent = `${formatInteger(stats.runCount || 0)} 个 run · ${formatInteger(stats.outcomeCount || 0)} 只股票`;
  els.optimizationStatsGrid.innerHTML = [
    metricCard("统计范围", stats.scopeLabel || "可用于优化", "不受下方筛选和列表加载数量影响"),
    metricCard("主KPI", stats.primaryScenarioLabel || "开盘后15分钟", "固定主口径，缺失不 fallback"),
    metricCard("平均净收益", formatPct(primary.avgNetReturnPct), "可用于优化样本的 open_15m 均值"),
    metricCard("成功率", formatRate(primary.successRate), "open_15m 净收益大于 0 的比例")
  ].join("");
  const rows = stats.exitTimingStats || [];
  els.exitTimingStatsRows.innerHTML = rows.length
    ? rows.map((row) => `
      <tr class="${row.isPrimary ? "primary-row" : ""}">
        <td>
          <strong>${escapeHtml(row.scenarioLabel || "--")}</strong>
          <small>${escapeHtml(row.sellTime || "")}${row.isPrimary ? " · 主KPI" : ""}</small>
        </td>
        <td>${timingPct(row.avgNetReturnPct)}</td>
        <td>${timingPct(row.medianNetReturnPct)}</td>
        <td>${formatRate(row.successRate)}</td>
        <td>${formatRate(row.strongSuccessRate)}</td>
        <td>${formatRate(row.benchmarkWinRate)}</td>
        <td>${formatInteger(row.sampleCount || 0)} / ${formatInteger(row.missingCount || 0)}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="7" class="empty">暂无可用于优化样本。</td></tr>`;
  const notes = stats.metricNotes || {};
  const noteRows = [
    notes.avgNetReturnPct,
    notes.medianNetReturnPct,
    notes.successRate,
    notes.strongSuccessRate,
    notes.benchmarkWinRate,
    notes.missingCount,
    stats.note
  ].filter(Boolean);
  els.metricNotes.innerHTML = noteRows.map((note) => `<span>${escapeHtml(note)}</span>`).join("");
}

function renderRunList(runs, totalCount, selected) {
  els.runListMeta.textContent = `${formatInteger(runs.length)} / ${formatInteger(totalCount)}`;
  if (!runs.length) {
    els.runList.innerHTML = `<div class="empty">没有符合筛选条件的 run。</div>`;
    return;
  }
  els.runList.innerHTML = runs.map((run) => {
    const active = Number(selected?.id || 0) === Number(run.id);
    const picks = (run.finalPicks || []).slice(0, 3).map((item) => `${item.name || "--"} ${item.code || ""}`.trim()).join(" / ");
    return `
      <button class="run-card ${active ? "active" : ""}" type="button" data-run-id="${formatIntegerRaw(run.id)}">
        <span class="run-card-head">
          <strong>run ${formatInteger(run.id)}</strong>
          <span>
            ${statusPill(reviewStatusLabel(run), reviewStatusTone(run))}
          </span>
        </span>
        <span class="run-card-meta">
          <small>${escapeHtml(run.recommendationDate || "--")} ${escapeHtml(run.scanTime || "")}</small>
          <small>${escapeHtml(selectionModeLabel(run.selectionMode))}</small>
          <small>${escapeHtml(sourceName(run.actualSource))}</small>
        </span>
        <span class="run-card-picks">${escapeHtml(picks || zeroPickReason(run) || "暂无最终推荐")}</span>
        <span class="run-card-foot">
          <small>Top${formatInteger(run.finalCount || 0)}</small>
          <small>open_15m ${formatPct(run.avgPrimaryNetReturnPct)}</small>
          <small>成功 ${formatInteger(run.successCount || 0)} / ${formatInteger(run.tradeableCount || 0)}</small>
        </span>
      </button>
    `;
  }).join("");
}

function renderDetail(run) {
  els.verifyRunButton.disabled = !run || state.loading;
  if (!run) {
    els.detailEyebrow.textContent = "请选择 run";
    els.detailTitle.textContent = "复盘明细";
    els.detailMetrics.innerHTML = "";
    els.usageExplanation.innerHTML = "";
    els.pickSummary.innerHTML = "";
    els.shadowPanel.innerHTML = "";
    els.outcomeRows.innerHTML = `<tr><td colspan="16" class="empty">暂无数据。</td></tr>`;
    els.outcomeMeta.textContent = "--";
    return;
  }
  const marketContext = run.market?.marketContext || {};
  const openingEvidence = run.market?.openingEvidence || {};
  const openingPrecheck = openingEvidence.precheck || {};
  const openingSnapshot = openingEvidence.snapshot || {};
  const openingLegacy = openingEvidence.status === "not_applicable_old_version";
  const entryEvidence = run.entryExecutionEvidence || run.market?.entryExecutionEvidence || {};
  const tradeable = run.tradeableSummary || {};
  const summary = run.outcomeSummary || {};
  els.detailEyebrow.textContent = `${run.recommendationDate || "--"} ${run.scanTime || ""} · ${escapeHtml(reviewStatusLabel(run))}`;
  els.detailTitle.textContent = `run ${formatInteger(run.id)} · ${selectionModeLabel(run.selectionMode)}`;
  els.detailMetrics.innerHTML = [
    metricCard("复盘状态", reviewStatusLabel(run), run.reviewStatusReason || "--"),
    metricCard("AI终选", aiDecisionLabel(run), run.aiDecisionReason || "--"),
    metricCard("学习范围", learningScopeLabel(run), run.learningScopeReason || "--"),
    metricCard("主 KPI 均值", formatPct(tradeable.avgNetReturnPct), "固定 open_15m，缺失不 fallback"),
    metricCard("成功数量", `${formatInteger(summary.successCount || 0)} / ${formatInteger(summary.count || 0)}`, "强成功 / 成功溢价 / 弱成功"),
    metricCard("用于优化股票", formatInteger(run.learningEligibleCount || 0), learningScopeLabel(run)),
    metricCard(
      "发布后买入证据",
      entryEvidence.complete ? `${formatInteger(entryEvidence.eligibleCount || 0)} / ${formatInteger(entryEvidence.expectedCount || 0)} 已冻结` : entryExecutionReviewLabel(entryEvidence.status),
      entryExecutionReviewDetail(entryEvidence)
    ),
    metricCard("验证进度", outcomeStatusLabel(run.outcomeStatus), outcomeStatusHint(run.outcomeStatus, run)),
    metricCard("市场状态", marketContext.label || "--", (run.market?.recommendationPolicy || {}).reason || run.error || "--"),
    metricCard(
      "09:29 数据预检",
      openingLegacy ? "旧版本未记录" : openingPrecheck.status === "ready" ? "已就绪" : openingPrecheck.status ? openingEvidenceStatusLabel(openingPrecheck.status) : "等待采集",
      openingLegacy ? openingEvidence.reason : openingPrecheck.expectedCount ? `预期全市场 ${formatInteger(openingPrecheck.expectedCount)} 只` : "只检查数据源，不作为开盘交易证据"
    ),
    metricCard(
      "09:31 早盘证据",
      openingLegacy ? "旧版本未记录" : openingSnapshot.decisionEligible ? "可用于动态退出" : openingSnapshot.status ? openingEvidenceStatusLabel(openingSnapshot.status) : "等待采集",
      openingLegacy ? "不使用当前规则倒推历史 Run" : openingSnapshot.snapshotId
        ? `全市场覆盖 ${formatRate(openingSnapshot.marketCoveragePct)} · 四指数${openingSnapshot.mainIndexStatus === "available" ? "完整" : "不完整"}`
        : "缺失或迟到时，低开风险分支不会触发"
    )
  ].join("");
  renderUsageExplanation(run);
  renderPickSummary(run);
  renderShadow(run);
  renderOutcomes(run);
}

function entryExecutionReviewLabel(status) {
  return {
    available: "可用于成交回放",
    scheduled: "等待采集",
    running: "正在采集",
    retry_wait: "等待重试",
    partial: "部分不可用",
    provider_failed: "行情源失败",
    missed_deadline: "错过采集窗口",
    legacy_not_recorded: "旧 Run 未记录",
    not_registered: "任务待登记",
  }[String(status || "not_registered")] || String(status || "--");
}

function entryExecutionReviewDetail(evidence = {}) {
  const ready = (evidence.snapshots || []).filter((item) => item.decisionEligible);
  if (ready.length) return ready.map((item) => `${item.code || "--"} ${item.entryTime || "--"} ${formatNumber(item.price)}`).join("；");
  if (evidence.job?.error) return evidence.job.error;
  if (evidence.status === "legacy_not_recorded") return "当时未独立冻结正式推荐发布后的分钟成交，不能倒推为可优化样本。";
  return "只认信号发布后的真实分钟成交；提前冻结选股价格不能替代。";
}

function openingEvidenceStatusLabel(status) {
  return {
    available: "可用",
    partial: "部分可用",
    ready: "已就绪",
    unavailable: "证据不足",
    missed_deadline: "已错过截止",
    late_snapshot: "采集迟到",
    provider_failed: "数据源失败",
  }[String(status || "")] || String(status || "--");
}

function renderUsageExplanation(run) {
  const usage = run.usageExplanation || {};
  const reasons = usage.reasons || [];
  const nextSteps = usage.nextSteps || [];
  if (!usage.summary && !reasons.length && !nextSteps.length) {
    els.usageExplanation.innerHTML = "";
    return;
  }
  const tone = usage.canOptimize ? "good" : reviewStatusTone(run);
  els.usageExplanation.innerHTML = `
    <section class="usage-box ${escapeHtml(tone)}">
      <div class="usage-box-head">
        <span>${escapeHtml(usage.title || "这条记录能不能用于优化？")}</span>
        <span class="usage-statuses">
          ${statusPill(reviewStatusLabel(run), reviewStatusTone(run))}
          ${statusPill(aiDecisionLabel(run), aiDecisionTone(run))}
          ${statusPill(learningScopeLabel(run), learningScopeTone(run))}
        </span>
      </div>
      <p>${escapeHtml(usage.summary || run.reviewStatusReason || "")}</p>
      ${reasons.length ? `
        <div class="usage-list">
          <strong>原因</strong>
          <ol>${reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ol>
        </div>
      ` : ""}
      ${nextSteps.length ? `
        <div class="usage-list">
          <strong>下一步</strong>
          <ol>${nextSteps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol>
        </div>
      ` : ""}
    </section>
  `;
}

function renderPickSummary(run) {
  const picks = run.finalPicks || [];
  if (!picks.length) {
    els.pickSummary.innerHTML = `<section class="review-official-section is-empty"><div class="review-official-head"><div><strong>正式推荐</strong><small>系统最终交易组合</small></div><span class="review-official-count">空仓</span></div><p>${escapeHtml(zeroPickReason(run) || "本次没有正式推荐。")}</p></section>`;
    return;
  }
  const outcomeByCode = new Map((run.outcomes || []).map((item) => [normalizedStockCode(item.code), item]));
  const rows = picks.map((pick, index) => {
    const result = outcomeByCode.get(normalizedStockCode(pick.code));
    const primary = result ? primaryScenario(result) : {};
    const returnValue = primary.netReturnPct;
    const status = result?.outcomeLabel || outcomeStatusLabel(run.outcomeStatus);
    return `<article class="review-official-pick">
      <span class="review-official-rank">#${formatInteger(pick.aiRank || index + 1)}</span>
      <a class="review-official-stock" href="${escapeHtml(stockAnalysisHref(pick))}" title="打开个股分析"><strong>${escapeHtml(pick.name || "--")}</strong><small>${escapeHtml(normalizedStockCode(pick.code))}</small></a>
      <span class="review-official-fields">
        <span class="review-official-field"><small>AI 终选</small><strong>#${formatInteger(pick.aiRank || index + 1)}</strong></span>
        <span class="review-official-field"><small>候选池</small><strong>${pick.poolRank == null ? "--" : `#${formatInteger(pick.poolRank)}`}</strong></span>
        <span class="review-official-field"><small>open_15m</small><strong class="${returnTone(returnValue)}">${returnValue == null ? "待验证" : formatPct(returnValue)}</strong></span>
      </span>
      <span class="review-official-status ${escapeHtml(outcomeStatusTone(run.outcomeStatus))}">${escapeHtml(status)}</span>
    </article>`;
  }).join("");
  els.pickSummary.innerHTML = `<section class="review-official-section"><div class="review-official-head"><div><strong>正式推荐</strong><small>系统最终交易组合，进入次日验证与策略复盘</small></div><span class="review-official-count">正式组合 · ${formatInteger(picks.length)} 只</span></div><div class="review-official-list">${rows}</div></section>`;
}

function renderShadow(run) {
  if (!els.shadowPanel) return;
  const market = run?.market || {};
  const rule = market.shadowTop3 || {};
  const ai = market.shadowDeepSeekTailTop3 || {};
  const comparison = market.shadowComparison || {};
  const outcome = comparison.outcomeComparison || {};
  const rulePicks = Array.isArray(rule.picks) ? rule.picks : [];
  const aiPicks = Array.isArray(ai.picks) ? ai.picks : [];
  const rows = Array.isArray(outcome.rows) ? outcome.rows : [];
  const aiRows = Array.isArray(outcome.deepSeekTailRows) ? outcome.deepSeekTailRows : [];
  const legacyAvailable = rulePicks.length || aiPicks.length || rows.length || aiRows.length;
  const policyHtml = policyExperimentRunSection(run);
  if (!legacyAvailable && !policyHtml) {
    els.shadowPanel.innerHTML = `<section class="review-shadow-box"><strong>影子 Top3</strong><span>该 run 未记录影子推荐数据（旧 run 或生成时未启用影子链路）。</span></section>`;
    return;
  }
  const cards = legacyAvailable ? [
    shadowReviewGroup("规则尾盘影子", rulePicks, rows),
    shadowReviewGroup("AI 尾盘影子", aiPicks, aiRows)
  ].filter(Boolean).join("") : "";
  const finalReturn = outcome.final?.portfolioReturnPct ?? run.baselineComparison?.portfolio?.portfolioReturnPct ?? run.tradeableSummary?.avgNetReturnPct;
  const shadowReturn = outcome.shadow?.portfolioReturnPct;
  const aiReturn = outcome.deepSeekTail?.portfolioReturnPct;
  const legacyHtml = legacyAvailable ? `
    <section class="review-shadow-box">
      <div class="review-shadow-head">
        <div><strong>影子 Top3 对比</strong><small>旁路验证尾盘承接和低开风险，不影响正式推荐或策略学习</small></div>
        <span class="review-shadow-badge">观察口径</span>
      </div>
      <div class="review-shadow-returns">
        ${shadowReturnCard("正式 Top3", finalReturn, "正式推荐组合", "formal")}
        ${shadowReturnCard("规则尾盘影子", shadowReturn, "尾盘承接加权", "rule")}
        ${shadowReturnCard("AI 尾盘影子", aiReturn, "DeepSeek 额外读取 T1", "ai")}
      </div>
      ${cards}
      ${outcome.reason ? `<small class="review-shadow-note">${escapeHtml(outcome.reason)}</small>` : ""}
    </section>
  ` : "";
  els.shadowPanel.innerHTML = `${legacyHtml}${policyHtml}`;
}

function policyExperimentRunSection(run) {
  const market = run?.market || {};
  const policy = market.strategyPolicy || {};
  if (!policy.strategyPolicyVersion) return "";

  const gateAudit = policy.negativeGateAudit || {};
  const eventAudit = market.eventRiskAudit || {};
  const calibration = policy.executionCalibrationAudit || {};
  const gateHits = Object.values(gateAudit.gates || {})
    .filter((gate) => Number(gate.hitCount || 0) > 0)
    .map((gate) => `${gate.label === "1m 尾盘承接" ? "1 分钟尾盘承接" : (gate.label || gate.gateKey)} ${formatInteger(gate.hitCount)} 只`);

  const roles = [
    policyRunRole(
      "市场门禁",
      policy.marketGateShadowPortfolio,
      policyRunResultReason("市场门禁", policy, market),
      "市场风险预算后的影子组合"
    ),
    policyRunRole(
      "风险门禁",
      policy.riskGateTreatmentPortfolio,
      policyRunResultReason("风险门禁", policy, market, gateHits),
      "板块、尾盘、隔夜、追高和事件风险"
    ),
    policyRunRole(
      "组合去同质化",
      policy.portfolioDiversityTreatmentPortfolio,
      policyRunResultReason("组合去同质化", policy, market),
      "行业与主驱动独立性"
    ),
    policyRunRole(
      "第三槽",
      policy.rank3ShadowPortfolio,
      policyRunResultReason("第三槽", policy, market),
      "第三槽影子，不强补"
    ),
    policyRunRole(
      "净期望排序",
      policy.netExpectationTreatmentPortfolio,
      policyRunResultReason("净期望排序", policy, market),
      "只读取此前日期的执行样本"
    ),
    policyRunRole(
      "动态退出",
      policy.frozenDecisionRanking,
      policyRunResultReason("动态退出", policy, market),
      "同一持仓对比固定 open_15m"
    )
  ].join("");

  const requestedEvents = Number(eventAudit.requestedCount || 0);
  const coveredEvents = Number(eventAudit.coveredCount || 0);
  const calibrationCount = Number(calibration.selectedCount || calibration.selectedCandidateCount || calibration.sampleCount || 0);
  return `
    <section class="review-shadow-box policy-run-shadow">
      <div class="review-shadow-head">
        <div>
          <strong>本期策略实验 · 当日冻结结果</strong>
          <small>推荐日即可查看名单和空组合原因；收益要到下一交易日验证后才进入跨 Run 统计</small>
        </div>
        <span class="review-shadow-badge">仅影子</span>
      </div>
      <div class="policy-run-meta">
        <span><small>策略版本</small><strong>${escapeHtml(policy.strategyPolicyVersion)}</strong></span>
        <span><small>事件风险</small><strong>${requestedEvents ? `${formatInteger(coveredEvents)} / ${formatInteger(requestedEvents)}` : "未采集"}</strong></span>
        <span><small>执行校准</small><strong>${formatInteger(calibrationCount)} 只</strong></span>
        <span><small>正式影响</small><strong>${policy.affectsOfficialRecommendation ? "已启用" : "无"}</strong></span>
      </div>
      <div class="policy-run-role-list">${roles}</div>
      <small class="review-shadow-note">空影子组合是可审计结果，不等于链路没有运行。顶部跨 Run 收益统计需等待次日结果。</small>
    </section>
  `;
}

function policyRunRole(title, rawPicks, status, note) {
  const picks = Array.isArray(rawPicks) ? rawPicks : [];
  const chips = picks.length
    ? picks.map((pick, index) => `<a href="${escapeHtml(stockAnalysisHref(pick))}" title="打开个股分析"><b>#${formatInteger(pick.originalAiRank || pick.aiRank || index + 1)}</b>${escapeHtml(pick.name || normalizedStockCode(pick.code))}<small>${escapeHtml(normalizedStockCode(pick.code))}</small></a>`).join("")
    : `<span class="policy-run-empty">Top0</span>`;
  return `
    <article class="policy-run-role">
      <div class="policy-run-role-head"><strong>${escapeHtml(title)}</strong><span>${formatInteger(picks.length)} 只</span></div>
      <small class="policy-run-meaning"><b>作用</b>${escapeHtml(policyRunMeaning(title))}</small>
      <div class="policy-run-result"><small>本次为什么是 ${formatInteger(picks.length)} 只</small><p>${escapeHtml(status || "等待数据")}</p></div>
      <small>${escapeHtml(note || "")}</small>
      <div class="policy-run-picks">${chips}</div>
    </article>
  `;
}

function policyRunResultReason(title, policy = {}, market = {}, gateHits = []) {
  const frozenCount = Array.isArray(policy.frozenDecisionRanking) ? policy.frozenDecisionRanking.length : 0;
  const auditCandidateCount = Number((policy.negativeGateAudit || {}).candidateCount || 0);
  const commonCount = Math.max(
    Array.isArray(policy.existingGuardrailSequence) ? policy.existingGuardrailSequence.length : 0,
    auditCandidateCount,
    frozenCount
  );
  if (title === "市场门禁") {
    const limit = Number(policy.maxOfficialPicksCandidate || 0);
    const resultCount = Array.isArray(policy.marketGateShadowPortfolio) ? policy.marketGateShadowPortfolio.length : 0;
    const state = marketStateLabel(policy.baseMarketState);
    if (!limit) return `当日市场为${state}，市场风险预算不允许持仓，因此结果为 0 只。`;
    return `当日市场为${state}，风险预算最多 ${formatInteger(limit)} 只；${formatInteger(commonCount)} 只候选中按 AI 原排序保留 ${formatInteger(resultCount)} 只。`;
  }
  if (title === "风险门禁") {
    const audit = policy.negativeGateAudit || {};
    const checked = Math.max(Number(audit.candidateCount || 0), (policy.riskGateBlockedCandidates || []).length, commonCount);
    const blockedCount = Number(audit.blockedCandidateCount ?? (policy.riskGateBlockedCandidates || []).length);
    const passed = Array.isArray(policy.riskGateTreatmentPortfolio) ? policy.riskGateTreatmentPortfolio.length : Math.max(0, checked - blockedCount);
    if (blockedCount) return `共检查 ${formatInteger(checked)} 只，${formatInteger(blockedCount)} 只命中风险项（${gateHits.join("；") || "详见门禁证据"}），通过 ${formatInteger(passed)} 只。`;
    return `共检查 ${formatInteger(checked)} 只，没有候选命中已启用的负向风险项，因此通过 ${formatInteger(passed)} 只。`;
  }
  if (title === "组合去同质化") {
    const upstream = Array.isArray(policy.riskGateTreatmentSequence) ? policy.riskGateTreatmentSequence.length : 0;
    const audit = policy.portfolioDiversityAudit || {};
    const resultCount = Array.isArray(policy.portfolioDiversityTreatmentPortfolio) ? policy.portfolioDiversityTreatmentPortfolio.length : 0;
    if (!upstream) return "风险门禁后没有剩余候选，本项没有股票可做行业和驱动分散，因此为 0 只。";
    if (resultCount < 2) return `风险门禁后有 ${formatInteger(upstream)} 只；第二只因同类集中或身份信息不足未开放，最终保留 ${formatInteger(resultCount)} 只。`;
    return `风险门禁后有 ${formatInteger(upstream)} 只，通过行业和上涨驱动检查后保留 ${formatInteger(resultCount)} 只${Number(audit.replacementCount || 0) ? `，替换 ${formatInteger(audit.replacementCount)} 只` : ""}。`;
  }
  if (title === "第三槽") {
    const rankedCount = Math.max(frozenCount, Array.isArray(policy.rank3ShadowPortfolio) ? policy.rank3ShadowPortfolio.length : 0);
    return (policy.rank3Audit || {}).available
      ? `AI 冻结排序共有 ${formatInteger(rankedCount)} 只；这里展示完整 Top3，用来比较“持有第 3 只”和“第三槽留现金”，因此为 3 只。`
      : `AI 冻结排序只有 ${formatInteger(rankedCount)} 只，不足 3 只，不能形成第三槽对照，因此为 0 只。`;
  }
  if (title === "净期望排序") {
    const model = policy.netExpectationModel || {};
    const samples = Number(model.sampleCount || 0);
    const minimum = Number(model.minimumTotalSamples || 150);
    if (!model.rankingReady) return `历史完整执行样本仅 ${formatInteger(samples)} 笔，尚未达到至少 ${formatInteger(minimum)} 笔的启用门槛，所以不生成排序，结果为 0 只。`;
    const count = Array.isArray(policy.netExpectationTreatmentPortfolio) ? policy.netExpectationTreatmentPortfolio.length : 0;
    return `历史样本已达到启用门槛，按扣除成本后的保守期望收益排序，并经风险与分散检查后保留 ${formatInteger(count)} 只。`;
  }
  if (title === "动态退出") {
    const eligible = Boolean(((market.openingEvidence || {}).snapshot || {}).decisionEligible);
    return eligible
      ? `这 ${formatInteger(frozenCount)} 只是今日冻结持仓的退出跟踪名单；次日 09:31 证据已具备，等待分钟回放得出退出结果。`
      : `这 ${formatInteger(frozenCount)} 只是今日冻结持仓的退出跟踪名单，并非新增推荐；需等下一交易日 09:29/09:31 数据后才能判断如何退出。`;
  }
  return "等待本次冻结数据。";
}

function policyRunMeaning(title) {
  return {
    "市场门禁": "根据大盘和市场广度控制整体风险预算，不直接判断单只股票。",
    "风险门禁": "检查尾盘承接、隔夜、追高和事件风险，命中时作为回避参考。",
    "组合去同质化": "避免组合集中在同一行业或同一上涨逻辑，降低同跌风险。",
    "第三槽": "单独观察 AI 排名第 3 只与现金仓位的差异，不强行补足推荐数量。",
    "净期望排序": "用历史真实执行结果估算扣成本后的期望收益，样本不足时只观察。",
    "动态退出": "用次日开盘后的分钟数据决定是否提前退出，不改变今日买入名单。"
  }[title] || "用于观察策略效果，不改变正式推荐。";
}

function marketStateLabel(value) {
  return {
    panic: "恐慌退潮",
    heavy_down: "重跌",
    mild_down: "偏弱",
    mixed: "震荡分化",
    mild_up: "温和上涨",
    trend_up: "趋势上涨",
    euphoric: "情绪高潮",
    unknown: "数据不足"
  }[String(value || "")] || String(value || "--");
}

function shadowReturnCard(label, value, note, tone) {
  return `<div class="review-shadow-return ${tone}"><small>${escapeHtml(label)}</small><strong class="${returnTone(value)}">${value == null ? "待验证" : formatPct(value)}</strong><span>${escapeHtml(note)}</span></div>`;
}

function shadowReviewGroup(title, picks, outcomeRows) {
  if (!picks.length && !outcomeRows.length) return "";
  const outcomeByCode = new Map(outcomeRows.map((row) => [String(row.code || "").padStart(6, "0"), row]));
  const rows = picks.map((pick) => {
    const code = String(pick.code || "").padStart(6, "0");
    const tail = pick.tailSupportShadow || {};
    const gap = pick.gapDownRiskShadow || {};
    const result = outcomeByCode.get(code) || {};
    return `<article class="review-shadow-pick">
      <div class="review-shadow-pick-head">
        <a class="stock-link" href="${escapeHtml(stockAnalysisHref(pick))}"><span class="review-shadow-rank">#${formatInteger(pick.shadowRank)}</span><strong>${escapeHtml(pick.name || code)}</strong><small>${escapeHtml(code)}</small></a>
        <span class="review-shadow-role">${escapeHtml(result.selectionRole || "未验证")}</span>
      </div>
      <div class="review-shadow-score"><span>影子分</span><strong>${formatNumber(pick.shadowT1Score)}</strong></div>
      <div class="review-shadow-metrics">
        <span><small>尾盘承接</small><b>${escapeHtml(tail.label || "--")}</b><em>${formatNumber(tail.score)}</em></span>
        <span><small>低开风险</small><b>${escapeHtml(gap.label || "--")}</b><em>${formatNumber(gap.score)}</em></span>
        <span><small>次日 open_15m</small><b class="${Number(result.open15mReturnPct) >= 0 ? "positive" : "negative"}">${formatPct(result.open15mReturnPct)}</b></span>
      </div>
    </article>`;
  }).join("");
  return `<div class="review-shadow-group"><div class="review-shadow-group-head"><strong>${escapeHtml(title)}</strong><small>${picks.length ? `${formatInteger(picks.length)} 个候选` : "暂无候选"}</small></div><div class="review-shadow-pick-grid">${rows || `<span class="review-shadow-empty">已生成但暂无候选明细</span>`}</div></div>`;
}

function renderOutcomes(run) {
  const outcomes = run.outcomes || [];
  els.outcomeMeta.textContent = `${outcomeStatusLabel(run.outcomeStatus)} · ${formatInteger(outcomes.length)} 条`;
  if (!outcomes.length) {
    els.outcomeRows.innerHTML = `<tr><td colspan="16" class="empty">${escapeHtml(outcomeStatusHint(run.outcomeStatus, run))}</td></tr>`;
    return;
  }
  els.outcomeRows.innerHTML = outcomes.map((item) => `
    <tr>
      <td>
        <a class="stock-link" href="${escapeHtml(stockAnalysisHref(item))}" title="打开个股分析">${escapeHtml(item.name || "--")}</a>
        <small>${escapeHtml(item.code || "")}</small>
      </td>
      <td>
        <span class="buy-price-cell">
          <strong>${formatPrice(item.buyReferencePrice)}</strong>
          <small>${escapeHtml(buyReferenceHint(item))}</small>
        </span>
      </td>
      <td>${escapeHtml(item.nextTradeDate || "--")}</td>
      <td>${scenarioNetHtml(primaryScenario(item))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "open"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "open_5m"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "open_15m"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "open_30m"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "ten_thirty"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "midday_close"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "afternoon_open"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "one_thirty"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "two_pm"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "two_thirty"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "close"))}</td>
      <td>
        <span class="label-chip ${outcomeTone(item.outcomeLabel)}">${escapeHtml(item.outcomeLabel || "--")}</span>
        <small>${escapeHtml(item.failureReason || item.dataSource || "")}</small>
      </td>
    </tr>
  `).join("");
}

function renderBaseline(run) {
  const baseline = run?.baselineComparison || {};
  const portfolio = baseline.portfolio || {};
  const final = baseline.final || {};
  const index = baseline.index || {};
  const criticPair = baseline.criticPair || {};
  els.baselinePanel.innerHTML = [
    metricCard("组合收益", formatPct(portfolio.portfolioReturnPct), `现金槽 ${formatInteger(portfolio.cashSlots || 0)} / ${formatInteger(portfolio.slotCount || 3)}`),
    metricCard("最终均值", formatPct(final.avgNetReturnPct), `成功率 ${formatRate(final.successRate)}`),
    metricCard("指数基准", formatPct(index.avgNetReturnPct ?? index.avgBenchmarkReturnPct), index.reason || "同口径指数/ETF参考"),
    metricCard("AI自检对照", formatPct(criticPair.deltaPct ?? criticPair.returnDeltaPct), criticPair.reason || "没有对照数据")
  ].join("");
}

function matchesFilter(run) {
  const filter = state.statusFilter;
  if (!filter) return true;
  return reviewStatusKey(run) === filter;
}

function firstMatchingRun(runs, selected) {
  if (selected && matchesFilter(selected)) return selected;
  const filtered = runs.filter(matchesFilter);
  return filtered[0] || selected || null;
}

function scenarioByKey(item, key) {
  const scenarios = ((item.outcome || {}).tradeable || {}).scenarios || [];
  return scenarios.find((scenario) => scenario.scenarioKey === key) || {};
}

function primaryScenario(item) {
  return ((item.outcome || {}).tradeable || {}).primaryScenario || {};
}

function buyReferenceHint(item) {
  const consistency = (item.outcome || {}).priceConsistency || {};
  const source = consistency.referenceSource || "";
  const date = consistency.referenceDate || item.recommendationDate || "";
  if (consistency.status === "reference_unavailable") {
    return "推荐冻结价，买入价未完成校验";
  }
  if (source === "recommendation_minute_snapshot") {
    return `推荐冻结价，分钟快照校验 ${date || "--"}`;
  }
  if (source === "recommendation_daily_ohlc") {
    return `推荐冻结价，日线校验 ${date || "--"}`;
  }
  if (consistency.ok === false) {
    return "推荐冻结价，价格异常";
  }
  return "推荐冻结价";
}

function scenarioNetHtml(scenario) {
  if (!scenario || !scenario.scenarioKey) return `<span class="muted-text">--</span>`;
  if (scenario.dataStatus !== "ok" || scenario.netReturnPct === null || scenario.netReturnPct === undefined) {
    const label = scenario.dataStatus === "minute_missing" ? "缺分钟" : scenario.dataStatus === "snapshot_missing" ? "缺快照" : "不可用";
    return `<span class="muted-text" title="${escapeHtml(scenario.reason || scenario.dataStatus || "")}">${label}</span>`;
  }
  const value = Number(scenario.netReturnPct);
  const tone = value >= 1 ? "good-text" : value < 0 ? "bad-text" : "warning-text";
  return `
    <span class="scenario-cell">
      <strong class="${tone}">${formatPct(value)}</strong>
      <small>${escapeHtml(scenario.sellTime || scenario.scenarioLabel || "")}</small>
    </span>
  `;
}

function metricCard(label, value, sub) {
  return `
    <article class="metric-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value ?? "--")}</strong>
      <small>${escapeHtml(sub || "--")}</small>
    </article>
  `;
}

function timingPct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return `<span class="muted-text">--</span>`;
  const tone = number >= 1 ? "good-text" : number < 0 ? "bad-text" : "warning-text";
  return `<strong class="${tone}">${escapeHtml(formatPct(number))}</strong>`;
}

function formatBackfillResultMessage(payload) {
  const base = payload?.message || payload?.reason || `已补拉 ${formatInteger(payload?.stored || 0)} 条，失败 ${formatInteger(payload?.failed || 0)} 条。`;
  const summary = payload?.roleSummary || {};
  const roleLines = [];
  if (summary.pick) {
    roleLines.push(`最终推荐：成功 ${formatInteger(summary.pick.stored || 0)} 只，已有 ${formatInteger(summary.pick.skipped || 0)} 只，失败 ${formatInteger(summary.pick.failed || 0)} 只`);
  }
  if (summary.shadow) {
    roleLines.push(`影子对照：成功 ${formatInteger(summary.shadow.stored || 0)} 只，已有 ${formatInteger(summary.shadow.skipped || 0)} 只，失败 ${formatInteger(summary.shadow.failed || 0)} 只`);
  }
  if (summary.benchmark) {
    roleLines.push(`市场基准：成功 ${formatInteger(summary.benchmark.stored || 0)} 只，已有 ${formatInteger(summary.benchmark.skipped || 0)} 只，失败 ${formatInteger(summary.benchmark.failed || 0)} 只`);
  }
  const failed = (payload?.items || []).filter((item) => item?.status === "failed");
  const prefix = roleLines.length ? `${base}\n${roleLines.join("\n")}` : base;
  if (!failed.length) return prefix;
  const details = failed.slice(0, 6).map((item) => {
    const role = item.roleLabel || (item.role === "benchmark" ? "市场基准" : item.role === "shadow" ? "影子对照" : "最终推荐");
    return `${role} ${item.code || "--"}：${item.error || item.reason || "补拉失败"}`;
  });
  const suffix = failed.length > details.length ? `；另有 ${failed.length - details.length} 条失败` : "";
  return `${prefix}\n失败明细：${details.join("；")}${suffix}`;
}

function statusPill(label, tone = "neutral") {
  return `<span class="status-pill ${escapeHtml(tone)}">${escapeHtml(label)}</span>`;
}

function normalizeReviewFilter(value) {
  const text = String(value || "");
  if (text === "all") return "";
  if (["usable", "official_usable", "research", "zero_pick"].includes(text)) return "reviewable";
  if (text === "learning") return "learnable";
  if (["pending_next_trade_day", "verifiable", "not_ready"].includes(text)) return "waiting";
  if (["problem", "data_insufficient"].includes(text)) return "partial";
  if (["legacy", "invalid", "unusable"].includes(text)) return "diagnostic";
  if (text === "rules_fallback") return "";
  if (["reviewable", "learnable", "partial", "waiting", "diagnostic", ""].includes(text)) return text;
  return "reviewable";
}

function normalizeShadowFamily(value) {
  const family = String(value || "");
  return Object.hasOwn(SHADOW_ROLES, family) ? family : "legacy_tail_rule";
}

function defaultShadowRole(family) {
  return (SHADOW_ROLES[normalizeShadowFamily(family)] || [])[0]?.value || "tail_rule";
}

function normalizeShadowWindow(value) {
  const windowKey = String(value || "all");
  return ["all", "20d", "discovery60", "confirmation60"].includes(windowKey) ? windowKey : "all";
}

function reviewStatusKey(run) {
  if (run?.reviewStatus) return run.reviewStatus;
  if (run?.sampleClass === "learning") return "learnable";
  if (["partial", "data_insufficient"].includes(run?.sampleClass)) return "partial";
  if (["usable", "zero_pick"].includes(run?.sampleClass)) return "reviewable";
  if (["waiting", "verifiable", "not_ready"].includes(run?.sampleClass)) return "waiting";
  return "diagnostic";
}

function reviewStatusLabel(run) {
  return run?.reviewStatusLabel || {
    reviewable: "可看结果",
    learnable: "可用于优化",
    partial: "部分缺数据",
    waiting: "等待数据",
    diagnostic: "仅排查",
    unusable: "仅排查"
  }[reviewStatusKey(run)] || "--";
}

function reviewStatusTone(run) {
  return run?.reviewStatusTone || {
    reviewable: "neutral",
    learnable: "good",
    partial: "warn",
    waiting: "warn",
    diagnostic: "bad",
    unusable: "bad"
  }[reviewStatusKey(run)] || "neutral";
}

function aiDecisionLabel(run) {
  return run?.aiDecisionLabel || selectionModeLabel(run?.selectionMode);
}

function aiDecisionTone(run) {
  if (run?.aiDecisionTone) return run.aiDecisionTone;
  if (run?.selectionMode === "ai_t1_overnight") return "good";
  if (run?.selectionMode === "ai_failed" || run?.selectionMode === "invalid_non_trading_day") return "bad";
  if (["ai_semifinal_rules_final", "rules_fallback"].includes(run?.selectionMode)) return "warn";
  return "neutral";
}

function learningScopeLabel(run) {
  if (run?.learningScopeLabel) return run.learningScopeLabel;
  return reviewStatusKey(run) === "learnable" ? "策略优化" : "不进入优化";
}

function learningScopeTone(run) {
  if (run?.learningScope === "full_ai_strategy") return "good";
  if (["candidate_rules_only", "rules_fallback_only", "data_only"].includes(run?.learningScope)) return "warn";
  return reviewStatusKey(run) === "learnable" ? "good" : "neutral";
}

function outcomeStatusLabel(value) {
  return {
    empty: "暂无 run",
    not_ready: "未完成",
    pending_next_trade_day: "等待次日",
    verifiable: "可验证",
    provisional: "盘中临时",
    verified: "已验证",
    legacy: "历史参考",
    invalid: "验证异常",
    data_insufficient: "数据不足",
    zero_pick: "空仓决策"
  }[value] || value || "--";
}

function outcomeStatusHint(value, run = null) {
  return {
    empty: "暂无 run。",
    not_ready: "当前 run 尚未完成。",
    pending_next_trade_day: "推荐日期还没有下个交易日行情，暂时等待。",
    verifiable: "当前 run 可以刷新次日验证。",
    provisional: "已取得 open_15m 等盘中验证点，但尚未进入最终状态。",
    verified: "已写入次日验证结果。",
    legacy: "旧口径历史数据仅保留展示和问题排查。",
    invalid: "验证结果存在价格口径、行情源或数据质量异常。",
    data_insufficient: "已经尝试验证，但行情源暂未提供足够次日数据。",
    zero_pick: zeroPickReason(run)
  }[value] || "等待验证状态。";
}

function outcomeStatusTone(value) {
  if (["verified", "provisional"].includes(value)) return "good";
  if (["pending_next_trade_day", "verifiable", "data_insufficient", "zero_pick", "legacy"].includes(value)) return "warn";
  if (["invalid", "not_ready"].includes(value)) return "bad";
  return "neutral";
}

function outcomeTone(label) {
  if (["强成功", "成功溢价", "弱成功"].includes(label)) return "good";
  if (["冲高回落", "数据不足"].includes(label)) return "warn";
  if (["触发失效", "低开失败", "无效", "价格异常"].includes(label)) return "bad";
  return "neutral";
}

function selectionModeLabel(value) {
  return {
    ai_t1_overnight: "AI T+1",
    ai_semifinal_rules_final: "AI初筛+规则终选",
    rules_fallback: "规则兜底",
    ai_failed: "AI失败",
    market_risk_zero_pick: "市场风险Top0",
    data_quality_zero_pick: "数据质量Top0",
    guardrail_zero_pick: "守门Top0",
    invalid_non_trading_day: "非交易日"
  }[value] || value || "--";
}

function sourceName(value) {
  return {
    auto: "自动",
    sina: "新浪",
    akshare: "AKShare/东财",
    mock: "Mock",
    eastmoney_daily: "东财日K",
    baostock_daily: "BaoStock",
    eastmoney_minute: "东财分钟线",
    multi_minute: "多源分钟线"
  }[value] || value || "--";
}

function zeroPickReason(run) {
  const policy = run?.market?.recommendationPolicy || {};
  const context = run?.market?.marketContext || {};
  return policy.reason || run?.error || (context.label ? `市场状态为${context.label}，按风控推荐 0 只。` : "");
}

function stockAnalysisHref(itemOrCode) {
  const code = typeof itemOrCode === "string" ? itemOrCode : itemOrCode?.code;
  const normalized = String(code || "").trim();
  if (!normalized) return "./index.html#stock";
  const stockCode = /^\d{1,6}$/.test(normalized) ? normalized.padStart(6, "0") : normalized;
  return `./index.html?stock=${encodeURIComponent(stockCode)}&analyze=1#stock`;
}

function normalizedStockCode(value) {
  const code = String(value || "").trim();
  return /^\d{1,6}$/.test(code) ? code.padStart(6, "0") : code;
}

function returnTone(value) {
  if (value == null || Number(value) === 0) return "neutral";
  return Number(value) > 0 ? "positive" : "negative";
}

function renderNotice(message) {
  els.notice.textContent = message || "";
}

function renderError(error) {
  renderNotice(error?.message || String(error || "操作失败"));
}

function startLongAction(title, hint) {
  setLoading(true);
  renderNotice(hint || title || "正在处理，请稍等。");
  if (els.actionProgress) {
    els.actionProgress.classList.remove("hidden");
    els.actionProgressTitle.textContent = title || "正在处理";
    els.actionProgressHint.textContent = hint || "请稍等";
  }
}

function finishLongAction() {
  if (els.actionProgress) {
    els.actionProgress.classList.add("hidden");
  }
  setLoading(false);
}

function showActionModal({ tone = "neutral", title = "操作完成", message = "" } = {}) {
  if (!els.actionModal) return;
  const normalizedTone = ["good", "warn", "bad"].includes(tone) ? tone : "neutral";
  els.actionModalTone.className = `status-pill ${normalizedTone}`;
  els.actionModalTone.textContent = {
    good: "成功",
    warn: "需注意",
    bad: "失败",
    neutral: "结果"
  }[normalizedTone] || "结果";
  els.actionModalTitle.textContent = title || "操作完成";
  els.actionModalMessage.textContent = message || "--";
  els.actionModal.classList.remove("hidden");
}

function hideActionModal() {
  els.actionModal?.classList.add("hidden");
}

function setLoading(loading) {
  state.loading = loading;
  els.refreshButton.disabled = loading;
  els.verifyRunButton.disabled = loading || !state.payload?.selectedRun;
  if (els.backfillMinuteButton) els.backfillMinuteButton.disabled = loading || !state.payload?.selectedRun;
}

function syncUrl() {
  const params = new URLSearchParams({
    limit: String(state.limit)
  });
  if (state.statusFilter && state.statusFilter !== "reviewable") params.set("reviewStatus", state.statusFilter);
  if (state.runId > 0) params.set("runId", String(state.runId));
  params.set("shadowFamily", state.shadowFamily);
  params.set("shadowRole", state.shadowRole);
  if (state.shadowWindow !== "all") params.set("shadowWindow", state.shadowWindow);
  window.history.replaceState(null, "", `?${params.toString()}`);
}

function clampNumber(value, min, max, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(min, Math.min(max, Math.trunc(number)));
}

function formatPct(value) {
  if (value == null || value === "") return "--";
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number > 0 ? "+" : ""}${number.toFixed(2).replace(/\.00$/, "")}%`;
}

function formatRate(value) {
  if (value == null || value === "") return "--";
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number.toFixed(2).replace(/\.00$/, "")}%`;
}

function formatPrice(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "--";
  return number.toFixed(2);
}

function formatInteger(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function formatNumber(value, digits = 1) {
  if (value == null || value === "") return "--";
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toFixed(digits).replace(/\.0+$/, "");
}

function formatIntegerRaw(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? String(Math.trunc(number)) : "0";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
