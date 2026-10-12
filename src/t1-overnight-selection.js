const DEFAULT_T1_POOL_SIZE = 500;
const DEFAULT_T1_BATCH_SIZE = 80;
const DEFAULT_T1_BATCH_PICK = 8;
const OFFICIAL_MIN_POOL_SIZE = 300;

const state = {
  pageMode: document.body?.dataset?.t1Page || "selection",
  riskMode: "balanced",
  source: "auto",
  scanTime: "14:40",
  poolSize: DEFAULT_T1_POOL_SIZE,
  run: null,
  preheat: null,
  polling: null,
  loading: false,
  actionLoading: false,
  criticConfig: null,
  executionConfig: null,
  stageConfig: null,
  stageStatus: null,
  criticStatus: null,
  strategyFeedback: null,
  autoRunStatus: null
};

const ZERO_PICK_SELECTION_MODES = ["market_risk_zero_pick", "data_quality_zero_pick", "guardrail_zero_pick", "invalid_non_trading_day"];

const els = {
  pageEyebrow: document.querySelector("#pageEyebrow"),
  sourceSelect: document.querySelector("#sourceSelect"),
  scanTimeInput: document.querySelector("#scanTimeInput"),
  poolSizeInput: document.querySelector("#poolSizeInput"),
  latestButton: document.querySelector("#latestButton"),
  preheatEvidenceButton: document.querySelector("#preheatEvidenceButton"),
  runButton: document.querySelector("#runButton"),
  forceCheck: document.querySelector("#forceCheck"),
  notice: document.querySelector("#notice"),
  statusText: document.querySelector("#statusText"),
  modeText: document.querySelector("#modeText"),
  poolText: document.querySelector("#poolText"),
  universeText: document.querySelector("#universeText"),
  batchText: document.querySelector("#batchText"),
  stageText: document.querySelector("#stageText"),
  finalText: document.querySelector("#finalText"),
  elapsedText: document.querySelector("#elapsedText"),
  progressBar: document.querySelector("#progressBar"),
  progressMeta: document.querySelector("#progressMeta"),
  actionProgress: document.querySelector("#actionProgress"),
  actionProgressTitle: document.querySelector("#actionProgressTitle"),
  actionProgressHint: document.querySelector("#actionProgressHint"),
  actionModal: document.querySelector("#actionModal"),
  actionModalTone: document.querySelector("#actionModalTone"),
  actionModalTitle: document.querySelector("#actionModalTitle"),
  actionModalMessage: document.querySelector("#actionModalMessage"),
  actionModalClose: document.querySelector("#actionModalClose"),
  actionModalOk: document.querySelector("#actionModalOk"),
  batchList: document.querySelector("#batchList"),
  marketContextPanel: document.querySelector("#marketContextPanel"),
  resultEyebrow: document.querySelector("#resultEyebrow"),
  pickGrid: document.querySelector("#pickGrid"),
  detailRows: document.querySelector("#detailRows"),
  criticModeSelect: document.querySelector("#criticModeSelect"),
  criticMinRunsInput: document.querySelector("#criticMinRunsInput"),
  criticMinOutcomesInput: document.querySelector("#criticMinOutcomesInput"),
  criticMinTradeableInput: document.querySelector("#criticMinTradeableInput"),
  criticMaxReplacementsInput: document.querySelector("#criticMaxReplacementsInput"),
  criticHistoryWindowInput: document.querySelector("#criticHistoryWindowInput"),
  criticRequireOutcomesCheck: document.querySelector("#criticRequireOutcomesCheck"),
  criticAllowReduceCheck: document.querySelector("#criticAllowReduceCheck"),
  criticAllowZeroCheck: document.querySelector("#criticAllowZeroCheck"),
  saveCriticConfigButton: document.querySelector("#saveCriticConfigButton"),
  criticStatusLine: document.querySelector("#criticStatusLine"),
  stageSectorShadowCheck: document.querySelector("#stageSectorShadowCheck"),
  stageSectorRetreatMode: document.querySelector("#stageSectorRetreatMode"),
  stageStyleShadowCheck: document.querySelector("#stageStyleShadowCheck"),
  stageStylePromptMode: document.querySelector("#stageStylePromptMode"),
  stage2OfficialDaysInput: document.querySelector("#stage2OfficialDaysInput"),
  stage2TradeableInput: document.querySelector("#stage2TradeableInput"),
  stage3OfficialDaysInput: document.querySelector("#stage3OfficialDaysInput"),
  stage3TradeableInput: document.querySelector("#stage3TradeableInput"),
  saveStageConfigButton: document.querySelector("#saveStageConfigButton"),
  stageStatusLine: document.querySelector("#stageStatusLine"),
  saveExecutionConfigButton: document.querySelector("#saveExecutionConfigButton"),
  buySlippageInput: document.querySelector("#buySlippageInput"),
  sellSlippageInput: document.querySelector("#sellSlippageInput"),
  commissionInput: document.querySelector("#commissionInput"),
  stampTaxInput: document.querySelector("#stampTaxInput"),
  buyTimesInput: document.querySelector("#buyTimesInput"),
  sellTimesInput: document.querySelector("#sellTimesInput"),
  verifyRunButton: document.querySelector("#verifyRunButton"),
  verifyPendingButton: document.querySelector("#verifyPendingButton"),
  historyDryRunButton: document.querySelector("#historyDryRunButton"),
  historySoftCleanupButton: document.querySelector("#historySoftCleanupButton"),
  baselineComparison: document.querySelector("#baselineComparison"),
  outcomeSummary: document.querySelector("#outcomeSummary"),
  outcomeRows: document.querySelector("#outcomeRows"),
  refreshFeedbackButton: document.querySelector("#refreshFeedbackButton"),
  feedbackStats: document.querySelector("#feedbackStats"),
  feedbackPatterns: document.querySelector("#feedbackPatterns"),
  feedbackSuggestions: document.querySelector("#feedbackSuggestions"),
  feedbackPrompt: document.querySelector("#feedbackPrompt"),
  setOfficialButton: document.querySelector("#setOfficialButton"),
  setResearchButton: document.querySelector("#setResearchButton")
};

init();

function init() {
  const params = new URLSearchParams(window.location.search);
  const requestedPoolSize = params.get("poolSize");
  state.riskMode = "balanced";
  state.source = params.get("source") || "auto";
  state.scanTime = params.get("scanTime") || "14:40";
  state.poolSize = normalizeInitialPoolSize(requestedPoolSize);
  setValue(els.sourceSelect, state.source);
  setValue(els.scanTimeInput, state.scanTime);
  setValue(els.poolSizeInput, String(state.poolSize));
  bindEvents();
  updateHeader();
  if (requestedPoolSize && Number(requestedPoolSize) !== state.poolSize) syncUrl();
  if (hasCriticForm()) loadCriticConfig();
  if (hasExecutionForm()) loadExecutionConfig();
  if (hasStageForm()) loadStageConfig();
  if (hasStrategyFeedbackPanel()) loadStrategyFeedback();
  loadAutoRunStatus();
  if (state.pageMode !== "config") loadLatest();
}

function bindEvents() {
  on(els.sourceSelect, "change", (event) => {
    state.source = event.currentTarget?.value || "auto";
    state.preheat = null;
    syncUrl();
  });
  on(els.scanTimeInput, "change", (event) => {
    state.scanTime = event.currentTarget?.value || "14:40";
    state.preheat = null;
    syncUrl();
    renderTimeNotice();
  });
  on(els.poolSizeInput, "change", (event) => {
    state.poolSize = clampNumber(event.currentTarget?.value, OFFICIAL_MIN_POOL_SIZE, 500, DEFAULT_T1_POOL_SIZE);
    if (event.currentTarget) event.currentTarget.value = String(state.poolSize);
    state.preheat = null;
    syncUrl();
  });
  on(els.latestButton, "click", loadLatest);
  on(els.preheatEvidenceButton, "click", preheatEvidence);
  on(els.actionModalClose, "click", hideActionModal);
  on(els.actionModalOk, "click", hideActionModal);
  on(els.actionModal, "click", (event) => {
    if (event.target === els.actionModal) hideActionModal();
  });
  on(els.runButton, "click", startRun);
  on(els.saveCriticConfigButton, "click", saveCriticConfig);
  on(els.saveStageConfigButton, "click", saveStageConfig);
  on(els.stageSectorShadowCheck, "change", renderStageForm);
  on(els.stageStyleShadowCheck, "change", renderStageForm);
  on(els.saveExecutionConfigButton, "click", saveExecutionConfig);
  on(els.verifyRunButton, "click", verifyCurrentRun);
  on(els.verifyPendingButton, "click", verifyPendingRuns);
  on(els.historyDryRunButton, "click", historyDryRun);
  on(els.historySoftCleanupButton, "click", historySoftCleanup);
  on(els.refreshFeedbackButton, "click", refreshStrategyFeedback);
  on(els.feedbackSuggestions, "click", handleSuggestionAction);
  on(els.setOfficialButton, "click", () => updateRunRole("official"));
  on(els.setResearchButton, "click", () => updateRunRole("research"));
}

function on(element, eventName, handler) {
  if (element) element.addEventListener(eventName, handler);
}

function setValue(element, value) {
  if (element) element.value = value;
}

function setText(element, value) {
  if (element) element.textContent = value;
}

function setHtml(element, value) {
  if (element) element.innerHTML = value;
}

function hasCriticForm() {
  return Boolean(
    els.criticModeSelect
    && els.criticMinRunsInput
    && els.criticMinOutcomesInput
    && els.criticMinTradeableInput
    && els.criticMaxReplacementsInput
    && els.criticHistoryWindowInput
    && els.criticRequireOutcomesCheck
    && els.criticAllowReduceCheck
    && els.criticAllowZeroCheck
    && els.saveCriticConfigButton
  );
}

function hasExecutionForm() {
  return Boolean(
    els.buySlippageInput
    && els.sellSlippageInput
    && els.commissionInput
    && els.stampTaxInput
    && els.buyTimesInput
    && els.sellTimesInput
    && els.saveExecutionConfigButton
  );
}

function hasStageForm() {
  return Boolean(
    els.stageSectorShadowCheck
    && els.stageSectorRetreatMode
    && els.stageStyleShadowCheck
    && els.stageStylePromptMode
    && els.stage2OfficialDaysInput
    && els.stage2TradeableInput
    && els.stage3OfficialDaysInput
    && els.stage3TradeableInput
    && els.saveStageConfigButton
  );
}

function hasStrategyFeedbackPanel() {
  return Boolean(els.feedbackStats && els.feedbackPatterns && els.feedbackSuggestions);
}

async function loadLatest() {
  setLoading(true);
  clearPolling();
  try {
    const params = new URLSearchParams({ riskMode: state.riskMode, source: state.source });
    const response = await fetch(`/api/t1-overnight-selection/latest?${params.toString()}`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.run = payload.status === "empty" ? null : payload;
    absorbRunMeta(state.run);
    render();
    if (state.run && ["pending", "running"].includes(state.run.status)) pollRun(state.run.id);
  } catch (error) {
    renderError(error);
  } finally {
    setLoading(false);
  }
}

async function loadAutoRunStatus() {
  try {
    const response = await fetch("/api/t1-overnight-selection/auto-run");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.autoRunStatus = payload;
    updateHeader();
    if (!state.run) renderTimeNotice();
  } catch (error) {
    state.autoRunStatus = null;
  }
}

async function startRun() {
  if (!els.runButton) return;
  setLoading(true);
  clearPolling();
  try {
    const response = await fetch("/api/t1-overnight-selection/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        riskMode: state.riskMode,
        source: state.source,
        scanTime: state.scanTime,
        poolSize: state.poolSize,
        batchSize: DEFAULT_T1_BATCH_SIZE,
        batchPick: DEFAULT_T1_BATCH_PICK,
        finalLimit: 3,
        force: Boolean(els.forceCheck?.checked)
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.run = payload;
    absorbRunMeta(state.run);
    render();
    if (!payload.reusedExisting && ["pending", "running"].includes(payload.status)) pollRun(payload.id);
  } catch (error) {
    renderError(error);
  } finally {
    setLoading(false);
  }
}

async function preheatEvidence() {
  if (state.actionLoading || state.loading) {
    renderNotice("证据预热已经在运行，请等待当前任务结束。");
    return;
  }
  startLongAction("正在预热今日证据", "正在拉取行情、分钟线和板块证据，完成前不能重复点击。");
  setActionLoading(true);
  try {
    const response = await fetch("/api/t1-overnight-selection/evidence/preheat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source: state.source,
        scanTime: state.scanTime,
        poolSize: state.poolSize
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.preheat = payload;
    render();
    const message = preheatSummary(payload);
    renderNotice(message);
    showActionModal({
      tone: payload?.evidence?.evidenceQuality === "insufficient" ? "warn" : "good",
      title: "预热今日证据完成",
      message
    });
  } catch (error) {
    const message = error?.message || String(error || "预热今日证据失败");
    renderError(error);
    showActionModal({
      tone: "bad",
      title: "预热今日证据失败",
      message
    });
  } finally {
    setActionLoading(false);
    finishLongAction();
  }
}

async function loadCriticConfig() {
  try {
    const response = await fetch("/api/t1-overnight-selection/critic-config");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.criticConfig = payload.config || {};
    state.criticStatus = payload.status || state.criticStatus;
    renderCriticForm();
    renderCriticStatusLine();
    if (state.run) renderProgressMeta(state.run);
  } catch (error) {
    renderError(error);
  }
}

async function loadExecutionConfig() {
  try {
    const response = await fetch("/api/t1-overnight-selection/execution-config");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.executionConfig = payload.config || {};
    renderExecutionForm();
  } catch (error) {
    renderError(error);
  }
}

async function loadStageConfig() {
  try {
    const response = await fetch("/api/t1-overnight-selection/stage-config");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.stageConfig = payload.config || {};
    state.stageStatus = payload.status || state.stageStatus;
    renderStageForm();
    renderStageStatusLine();
  } catch (error) {
    renderError(error);
  }
}

async function saveStageConfig() {
  if (!hasStageForm()) return;
  setActionLoading(true);
  try {
    const response = await fetch("/api/t1-overnight-selection/stage-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sectorStateShadowEnabled: els.stageSectorShadowCheck.checked,
        sectorRetreatDowngradeMode: els.stageSectorRetreatMode.value,
        styleRegimeShadowEnabled: els.stageStyleShadowCheck.checked,
        styleRegimePromptMode: els.stageStylePromptMode.value,
        minOfficialTradeDaysForStage2: clampNumber(els.stage2OfficialDaysInput.value, 1, 250, 30),
        minTradeableOutcomesForStage2: clampNumber(els.stage2TradeableInput.value, 1, 1000, 30),
        minOfficialTradeDaysForStage3: clampNumber(els.stage3OfficialDaysInput.value, 1, 250, 60),
        minTradeableOutcomesForStage3: clampNumber(els.stage3TradeableInput.value, 1, 1000, 60)
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.stageConfig = payload.config || {};
    state.stageStatus = payload.status || state.stageStatus;
    renderStageForm();
    renderStageStatusLine();
    renderNotice("T+1 策略阶段配置已保存；新 run 按当前配置冻结，已有 run 不改写。");
  } catch (error) {
    renderError(error);
  } finally {
    setActionLoading(false);
  }
}

async function saveExecutionConfig() {
  if (!hasExecutionForm()) return;
  setActionLoading(true);
  try {
    const response = await fetch("/api/t1-overnight-selection/execution-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        buySlippagePct: clampFloat(els.buySlippageInput.value, 0, 2, 0.1),
        sellSlippagePct: clampFloat(els.sellSlippageInput.value, 0, 2, 0.1),
        commissionPct: clampFloat(els.commissionInput.value, 0, 1, 0.025),
        stampTaxPct: clampFloat(els.stampTaxInput.value, 0, 1, 0.05),
        buyTimes: parseCsv(els.buyTimesInput.value),
        sellTimes: parseCsv(els.sellTimesInput.value)
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.executionConfig = payload.config || {};
    renderExecutionForm();
    renderNotice("专业复盘配置已保存，后续刷新验证时生效。");
  } catch (error) {
    renderError(error);
  } finally {
    setActionLoading(false);
  }
}

async function saveCriticConfig() {
  if (!hasCriticForm()) return;
  setActionLoading(true);
  try {
    const response = await fetch("/api/t1-overnight-selection/critic-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: els.criticModeSelect.value,
        minCompletedRuns: clampNumber(els.criticMinRunsInput.value, 0, 200, 10),
        minVerifiedOutcomes: clampNumber(els.criticMinOutcomesInput.value, 0, 600, 20),
        minTradeableValidationSamples: clampNumber(els.criticMinTradeableInput.value, 0, 600, 20),
        maxReplacements: clampNumber(els.criticMaxReplacementsInput.value, 0, 2, 1),
        historyWindowRuns: clampNumber(els.criticHistoryWindowInput.value, 5, 120, 20),
        requireOutcomes: els.criticRequireOutcomesCheck.checked,
        allowReducePickCount: els.criticAllowReduceCheck.checked,
        allowZeroPick: els.criticAllowZeroCheck.checked
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.criticConfig = payload.config || {};
    state.criticStatus = payload.status || state.criticStatus;
    renderCriticForm();
    render();
    renderNotice("AI 自检配置已保存。");
  } catch (error) {
    renderError(error);
  } finally {
    setActionLoading(false);
  }
}

async function loadStrategyFeedback() {
  try {
    const response = await fetch("/api/t1-overnight-selection/strategy-feedback");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.strategyFeedback = payload.feedback || null;
    state.criticStatus = payload.criticStatus || state.criticStatus;
    renderStrategyFeedback();
    renderCriticStatusLine();
  } catch (error) {
    renderError(error);
  }
}

async function verifyCurrentRun() {
  if (!state.run || !state.run.id) {
    const message = "当前没有可验证的 T+1 run。";
    renderNotice(message);
    showActionModal({ tone: "warn", title: "无法刷新验证", message });
    return;
  }
  if (isZeroPickRun(state.run)) {
    state.run.outcomeStatus = "zero_pick";
    render();
    const message = outcomeStatusHint("zero_pick", state.run);
    renderNotice(message);
    showActionModal({ tone: "warn", title: "无需刷新验证", message });
    return;
  }
  setActionLoading(true);
  try {
    const response = await fetch(`/api/t1-overnight-selection/runs/${state.run.id}/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.run.outcomes = payload.items || [];
    state.run.outcomeStatus = payload.status;
    state.run.outcomeSummary = payload.summary || {};
    state.run.tradeableSummary = payload.tradeableSummary || {};
    state.run.baselineComparison = payload.baselineComparison || {};
    state.strategyFeedback = payload.strategyFeedback || state.strategyFeedback;
    render();
    const message = payload.reason || outcomeStatusHint(payload.status, state.run);
    renderNotice(message);
    showActionModal({ tone: "good", title: "刷新当前 run 完成", message });
  } catch (error) {
    const message = error?.message || String(error || "刷新当前 run 失败");
    renderError(error);
    showActionModal({ tone: "bad", title: "刷新当前 run 失败", message });
  } finally {
    setActionLoading(false);
  }
}

async function verifyPendingRuns() {
  setActionLoading(true);
  try {
    const response = await fetch("/api/t1-overnight-selection/verify-pending", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: 20 })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.strategyFeedback = payload.strategyFeedback || state.strategyFeedback;
    if (state.run?.id) await refreshRun(state.run.id);
    render();
    const skipped = Number(payload.skippedZeroPick || 0);
    const message = `批量验证完成：处理 ${formatInteger(payload.count)} 个 run${skipped ? `，跳过空仓 run ${formatInteger(skipped)} 个` : ""}。`;
    renderNotice(message);
    showActionModal({ tone: "good", title: "批量验证完成", message });
  } catch (error) {
    const message = error?.message || String(error || "批量验证失败");
    renderError(error);
    showActionModal({ tone: "bad", title: "批量验证失败", message });
  } finally {
    setActionLoading(false);
  }
}

async function historyDryRun() {
  setActionLoading(true);
  try {
    const response = await fetch("/api/t1-overnight-selection/history-dry-run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    const message = historyDryRunSummary(payload);
    renderNotice(message);
    showActionModal({ tone: "good", title: "历史预演完成", message });
  } catch (error) {
    const message = error?.message || String(error || "历史预演失败");
    renderError(error);
    showActionModal({ tone: "bad", title: "历史预演失败", message });
  } finally {
    setActionLoading(false);
  }
}

async function historySoftCleanup() {
  const ok = window.confirm("执行历史软清理：旧 T+1 数据会保留展示，但标记为不进入严格复盘、DeepSeek 学习或 Critic 样本。不会删除数据。是否继续？");
  if (!ok) return;
  setActionLoading(true);
  try {
    const response = await fetch("/api/t1-overnight-selection/history-soft-cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.strategyFeedback = payload.feedback || state.strategyFeedback;
    if (state.run?.id) await refreshRun(state.run.id);
    render();
    const message = historySoftCleanupSummary(payload);
    renderNotice(message);
    showActionModal({ tone: "good", title: "软清理完成", message });
  } catch (error) {
    const message = error?.message || String(error || "软清理失败");
    renderError(error);
    showActionModal({ tone: "bad", title: "软清理失败", message });
  } finally {
    setActionLoading(false);
  }
}

async function updateRunRole(role) {
  if (!state.run?.id) {
    renderNotice("当前没有可设置的 T+1 run。");
    return;
  }
  const label = role === "official" ? "正式 run" : "研究 run";
  if (state.run.runRole === role) {
    const message = `当前 run 已经是${label}，不能重复设置。`;
    renderNotice(message);
    showActionModal({ tone: "warn", title: "无需重复设置", message });
    return;
  }
  if (state.run.runRole === "legacy" || state.run.runRole === "invalid") {
    const message = "历史/异常 run 只用于排查，不能改成正式或研究 run。";
    renderNotice(message);
    showActionModal({ tone: "warn", title: "不能修改 run 类型", message });
    return;
  }
  if (role === "official" && isQuickResearchRun(state.run)) {
    const message = `候选池低于 ${OFFICIAL_MIN_POOL_SIZE}，只能作为研究 run，不能设为正式 run。`;
    renderNotice(message);
    showActionModal({ tone: "warn", title: "不能设为正式 run", message });
    return;
  }
  const ok = window.confirm(`确认将当前 run 设为${label}？设为正式 run 时，同一推荐日期的其他非历史 run 会自动改为研究 run。`);
  if (!ok) return;
  setActionLoading(true);
  try {
    const response = await fetch("/api/t1-overnight-selection/run-role", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        runId: state.run.id,
        role,
        reviewerNote: `页面手动设为${label}`
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.run = payload.run || state.run;
    state.strategyFeedback = payload.feedback || state.strategyFeedback;
    state.criticStatus = payload.criticStatus || state.criticStatus;
    render();
    const message = runRoleChangeMessage(role);
    renderNotice(message);
    showActionModal({ tone: "good", title: `已设为${label}`, message });
  } catch (error) {
    const message = error?.message || String(error || "设置 run 类型失败");
    renderError(error);
    showActionModal({ tone: "bad", title: "设置 run 类型失败", message });
  } finally {
    setActionLoading(false);
  }
}

function runRoleChangeMessage(role) {
  if (role === "official") {
    return "当前 run 已设为正式 run：会进入严格复盘、策略统计和后续可优化样本；同一推荐日期的其他非历史 run 会自动降为研究 run。";
  }
  return "当前 run 已设为研究 run：页面仍可查看结果和排查问题，但不会进入严格学习、策略复盘统计或影响 DeepSeek 后续经验。";
}

async function refreshStrategyFeedback() {
  setActionLoading(true);
  try {
    const response = await fetch("/api/t1-overnight-selection/strategy-feedback/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ runId: state.run?.id || 0 })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.strategyFeedback = payload.feedback || state.strategyFeedback;
    await loadCriticConfig();
    render();
    const meta = (state.strategyFeedback || {}).refreshMeta || {};
    const idText = state.strategyFeedback?.id ? `记录 #${state.strategyFeedback.id}` : "最新记录";
    if (meta.reusedExistingFeedback) {
      renderNotice(`策略复盘已刷新，${idText} 已是最新；没有新增验证样本，指标无变化。`);
    } else if (meta.hasNewSamples === false) {
      renderNotice(`策略复盘已刷新，生成 ${idText}；没有新增验证样本，指标无变化。`);
    } else {
      renderNotice(`策略复盘已刷新，生成 ${idText}。`);
    }
  } catch (error) {
    renderError(error);
  } finally {
    setActionLoading(false);
  }
}

async function handleSuggestionAction(event) {
  const button = event.target.closest("[data-suggestion-action]");
  if (!button) return;
  const id = Number(button.dataset.suggestionId || 0);
  const status = button.dataset.suggestionAction || "";
  if (!id || !status) return;
  setActionLoading(true);
  try {
    const response = await fetch("/api/t1-overnight-selection/strategy-suggestions/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id,
        status,
        reviewerNote: suggestionReviewerNote(status)
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.strategyFeedback = payload.feedback || state.strategyFeedback;
    render();
    renderNotice(`策略建议已${suggestionStatusVerb(status)}。`);
  } catch (error) {
    renderError(error);
  } finally {
    setActionLoading(false);
  }
}

async function refreshRun(runId) {
  const response = await fetch(`/api/t1-overnight-selection/runs/${runId}`);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
  state.run = payload;
  absorbRunMeta(state.run);
}

function pollRun(runId) {
  clearPolling();
  state.polling = window.setInterval(async () => {
    try {
      const response = await fetch(`/api/t1-overnight-selection/runs/${runId}`);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
      state.run = payload;
      absorbRunMeta(state.run);
      render();
      if (!["pending", "running"].includes(payload.status)) clearPolling();
    } catch (error) {
      clearPolling();
      renderError(error);
    }
  }, 2000);
}

function clearPolling() {
  if (state.polling) window.clearInterval(state.polling);
  state.polling = null;
}

function absorbRunMeta(run) {
  if (!run) return;
  state.criticStatus = run.criticStatus || state.criticStatus;
  state.strategyFeedback = run.strategyFeedback || state.strategyFeedback;
}

function render() {
  updateHeader();
  renderCriticStatusLine();
  renderStrategyFeedback();
  const run = state.run;
  if (!run) {
    renderNotice(timeHint(state.scanTime));
    renderEmpty();
    return;
  }
  renderNotice(buildNotice(run));
  setText(els.statusText, statusLabel(run.status));
  setHtml(els.modeText, `<span class="${modeClass(run.selectionMode)}">${selectionModeLabel(run.selectionMode)}</span>`);
  setText(els.poolText, formatInteger(run.poolSize));
  setText(els.universeText, `原始 ${formatInteger(run.totalCount)} · 硬过滤后 ${formatInteger(run.hardFilteredCount)}`);
  setText(els.batchText, `${formatInteger(run.completedBatchCount)} / ${formatInteger(run.batchCount)}`);
  setText(els.stageText, stageLabel(run.currentStage));
  setText(els.finalText, `${formatInteger(run.finalCount)} / ${formatInteger(run.finalLimit)}`);
  setText(els.elapsedText, `${riskLabel(run.riskMode)} · ${run.scanTime || "--"} · ${formatDuration(run.elapsedSeconds)}`);
  if (els.progressBar) els.progressBar.style.width = `${Number(run.progressPct || 0)}%`;
  setText(els.resultEyebrow, `固定T+1策略 · 最多Top${formatInteger(run.finalLimit)} · ${selectionModeLabel(run.selectionMode)} · ${sourceLabel(run)}`);
  renderProgressMeta(run);
  renderMarketContext(run);
  renderBatchList(run.batchSummary || []);
  renderPicks(run.finalPicks || [], run);
  renderOutcomes(run);
  renderRows(run.items || []);
}

function renderEmpty() {
  const preheat = state.preheat || null;
  setText(els.statusText, "--");
  setText(els.modeText, "--");
  setText(els.poolText, preheat ? formatInteger(preheat.poolCount || preheat.poolSize || 0) : "--");
  setText(els.universeText, preheat ? `预热可交易 ${formatInteger(preheat.selectionPoolCount || 0)} · ${sourceName(preheat.actualSource || preheat.source)}` : "--");
  setText(els.batchText, "--");
  setText(els.stageText, "--");
  setText(els.finalText, "--");
  setText(els.elapsedText, preheat ? `证据预热 · ${state.scanTime || "--"} · ${formatDuration(preheat.elapsedSeconds)}` : "--");
  if (els.progressBar) els.progressBar.style.width = "0%";
  if (preheat) {
    renderPreheatPanels(preheat);
  } else {
    setHtml(els.progressMeta, "");
    setHtml(els.marketContextPanel, `<div class="empty">暂无市场环境。生成 T+1 run 后显示。</div>`);
  }
  setHtml(els.batchList, "");
  setHtml(els.baselineComparison, "");
  setHtml(els.pickGrid, `<div class="empty">暂无最终 Top3。点击“开始 T+1 筛选”手动生成。</div>`);
  setHtml(els.detailRows, `<tr><td colspan="8" class="empty">暂无明细</td></tr>`);
  renderOutcomes(null);
}

function renderBatchList(batches) {
  if (!els.batchList) return;
  if (!batches.length) {
    els.batchList.innerHTML = `<span class="batch-pill">等待批次结果</span>`;
    return;
  }
  els.batchList.innerHTML = batches.map((batch) => `
    <span class="batch-pill" title="${escapeHtml(batch.error || "")}">
      批次 ${formatInteger(batch.batchIndex)} · ${formatInteger(batch.candidateCount)} 选 ${formatInteger(batch.targetPickCount || batch.pickCount)}
    </span>
  `).join("");
}

function renderProgressMeta(run) {
  if (!els.progressMeta) return;
  const market = run.market || {};
  const source = market.sourceStatus || {};
  const ai = market.ai || {};
  const evidence = market.t1Evidence || {};
  const aiParticipation = market.aiParticipation || {};
  const criticStatus = market.criticStatus || run.criticStatus || state.criticStatus || {};
  const critic = market.critic || {};
  const cards = [
    {
      label: "当前阶段",
      value: stageLabel(run.currentStage || run.status),
      tone: run.status === "failed" ? "bad" : run.status === "done" ? "good" : "neutral",
      detail: progressDetail(run)
    },
    {
      label: "Run 类型",
      value: runRoleLabel(run.runRole),
      tone: run.runRole === "official" ? "good" : run.runRole === "legacy" || run.runRole === "invalid" ? "warn" : "neutral",
      detail: run.runRole === "official" ? `${runProfileLabel(run)} · 进入严格 official 复盘窗口` : run.runRole === "legacy" ? "旧口径仅展示，不进学习" : `${runProfileLabel(run)} · 研究/重跑结果不进入严格样本`
    },
    {
      label: "行情源",
      value: source.label || sourceName(run.actualSource || run.requestedSource),
      tone: source.isMock ? "bad" : source.isCache ? "warn" : "good",
      detail: sourceDetail(source, market)
    },
    {
      label: "T+1证据",
      value: evidence.evidenceRunId ? `#${formatInteger(evidence.evidenceRunId)} · ${evidenceQualityLabel(evidence)}` : "证据未生成",
      tone: evidenceTone(evidence),
      detail: evidenceDetail(evidence)
    },
    {
      label: "市场环境",
      value: marketContextLabel(market.marketContext),
      tone: marketContextTone(market.marketContext),
      detail: marketContextDetail(market.marketContext, market.recommendationPolicy)
    },
    {
      label: "AI 终选",
      value: aiParticipation.label || aiLabel(run, ai),
      tone: aiParticipationTone(aiParticipation, run, ai),
      detail: aiParticipation.reason || aiDetail(run, ai)
    },
    {
      label: "AI 自检",
      value: criticLabel(criticStatus, critic),
      tone: criticTone(criticStatus, critic),
      detail: criticDetail(criticStatus, critic)
    }
  ];
  els.progressMeta.innerHTML = cards.map((card) => `
    <div class="progress-card ${card.tone}">
      <span>${escapeHtml(card.label)}</span>
      <strong>${escapeHtml(card.value)}</strong>
      <small>${escapeHtml(card.detail || "--")}</small>
    </div>
  `).join("");
}

function renderPreheatPanels(preheat) {
  if (!els.progressMeta && !els.marketContextPanel) return;
  const evidence = preheat.evidence || {};
  const quality = preheat.dataQualityStats || {};
  const minuteKline = preheat.minuteKlineStatus || {};
  const coverage = evidence.evidenceCoverage || quality.sectorLayerCounts || {};
  setHtml(els.progressMeta, [
    {
      label: "证据预热",
      value: evidence.evidenceRunId ? `#${formatInteger(evidence.evidenceRunId)} · ${evidenceQualityLabel(evidence)}` : "证据未生成",
      tone: evidenceTone(evidence),
      detail: `${preheat.reusedExisting || evidence.reusedExisting ? "复用今天冻结证据" : "生成今天冻结证据"} · ${preheat.reuseReason || evidence.reuseReason || evidenceDetail(evidence)}`
    },
    {
      label: "候选池",
      value: `${formatInteger(preheat.poolCount || 0)} / ${formatInteger(preheat.poolSize || state.poolSize)}`,
      tone: Number(preheat.poolCount || 0) >= OFFICIAL_MIN_POOL_SIZE ? "good" : "warn",
      detail: `可交易候选 ${formatInteger(preheat.selectionPoolCount || 0)}`
    },
    {
      label: "分钟线",
      value: `${formatInteger(minuteKline.ok || minuteKline.attached || 0)} / ${formatInteger(minuteKline.requested || quality.total || 0)}`,
      tone: minuteKlineTone(minuteKline),
      detail: formatKlineSourceCounts(minuteKline.sourceCounts) || "等待真实分钟线"
    },
    {
      label: "板块证据",
      value: coverageHeadline(coverage),
      tone: coverageTone(coverage),
      detail: coverageDetail(coverage)
    }
  ].map((card) => `
    <div class="progress-card ${card.tone}">
      <span>${escapeHtml(card.label)}</span>
      <strong>${escapeHtml(card.value)}</strong>
      <small>${escapeHtml(card.detail || "--")}</small>
    </div>
  `).join(""));
  setHtml(els.marketContextPanel, `
    <div class="market-status-card ${evidenceTone(evidence)}">
      <span>T+1冻结证据</span>
      <strong>${evidence.evidenceRunId ? `#${formatInteger(evidence.evidenceRunId)} · ${escapeHtml(evidenceQualityLabel(evidence))}` : "证据未生成"}</strong>
      <small>${escapeHtml(evidenceDetail(evidence))}</small>
    </div>
    <div class="market-status-card ${coverageTone(coverage)}">
      <span>板块证据覆盖</span>
      <strong>${escapeHtml(coverageHeadline(coverage))}</strong>
      <small>${escapeHtml(coverageDetail(coverage))}</small>
    </div>
    <div class="market-status-card ${minuteKlineTone(minuteKline)}">
      <span>分钟线源</span>
      <strong>${formatInteger(minuteKline.ok || minuteKline.attached || 0)} / ${formatInteger(minuteKline.requested || quality.total || 0)}</strong>
      <small>${escapeHtml(formatKlineSourceCounts(minuteKline.sourceCounts) || "等待真实分钟线")}</small>
    </div>
    <div class="market-status-card neutral">
      <span>预热范围</span>
      <strong>${formatInteger(preheat.poolCount || 0)} 只候选</strong>
      <small>可交易候选 ${formatInteger(preheat.selectionPoolCount || 0)} · 耗时 ${formatDuration(preheat.elapsedSeconds)}</small>
    </div>
  `);
}

function renderMarketContext(run) {
  if (!els.marketContextPanel) return;
  const market = run?.market || {};
  const context = market.marketContext || {};
  const policy = market.recommendationPolicy || {};
  const metrics = context.metrics || {};
  const completeness = context.indicatorCompleteness || {};
  const quality = market.dataQualityStats || {};
  const counts = quality.counts || {};
  const sectorCounts = quality.sectorEvidenceCounts || {};
  const sectorLayerCounts = quality.sectorLayerCounts || {};
  const minuteKline = market.minuteKlineStatus || {};
  const evidence = market.t1Evidence || {};
  const evidenceCoverage = evidence.evidenceCoverage || sectorLayerCounts;
  const allocation = market.validationAllocation || {};
  const aiParticipation = market.aiParticipation || {};
  const indexView = marketIndexView(market, metrics);
  const realIndices = indexView.realIndices;
  const equalWeightedIndices = indexView.equalWeightedIndices;
  const indexStatus = market.marketIndexStatus || {};
  const indexWarning = indexView.warning || indexStatus.warning || "";
  const aiEvidenceStatus = market.aiEvidenceValidationStatus || "";
  const tradeDateStatus = market.tradeDateStatus || evidence.tradeDateStatus || context.tradeDateStatus || {};
  const sectorStateSummary = market.sectorStateShadowSummary || evidence.sectorStateShadowSummary || {};
  const styleRegime = market.styleRegimeShadow || evidence.styleRegimeShadow || {};
  const stageControls = market.stageControls || run?.stageStatus || {};
  const sectorRetreatStage = stageControls.sectorRetreatDowngrade || {};
  const stylePromptStage = stageControls.styleRegimePrompt || {};
  const lossTone = marketLossTone(indexView.lossEffectChangePct);
  const naturalLimitDown = Number(metrics.limitDown || 0);
  const stLimitDown = Number(metrics.stLimitDown || 0);
  const nearLimitDown = Number(metrics.nearLimitDown || 0);
  const totalLimitDown = Number(metrics.totalLimitDown || naturalLimitDown + stLimitDown);
  if (!context.state) {
    els.marketContextPanel.innerHTML = `<div class="empty">暂无市场环境。生成 T+1 run 后显示。</div>`;
    return;
  }
  els.marketContextPanel.innerHTML = `
    ${(allocation.rounds || []).map((round) => `<div class="market-status-card neutral"><span>15m 验证第 ${formatInteger(round.round)} 轮</span><strong>计划 ${formatInteger(round.planned)} · 合格 ${formatInteger(round.eligible)}</strong><small>请求 ${formatInteger(round.requested)} · 真实可用 ${formatInteger(round.available)}</small></div>`).join("")}
    ${allocation.version ? `<div class="market-status-card neutral"><span>尾盘 1m 影子验证</span><strong>计划 ${formatInteger(allocation.tail1m?.planned)} · 可用 ${formatInteger(allocation.tail1m?.available)}</strong><small>请求 ${formatInteger(allocation.tail1m?.requested)} · AI 批次后按需验证</small><a class="market-archive-link" href="./t1-overnight-review.html?runId=${Number(run.id || run.runId || 0)}&reviewStatus=all">查看筛选档案与次日效果</a></div>` : ""}
    <div class="market-status-card ${marketContextTone(context)}">
      <span>市场状态</span>
      <strong>${escapeHtml(context.label || context.state || "--")}</strong>
      <small>${escapeHtml((context.reasons || []).join("；") || policy.reason || "--")}</small>
    </div>
    <div class="market-status-card ${tradeDateTone(tradeDateStatus)}">
      <span>交易日校验</span>
      <strong>${escapeHtml(tradeDateStatusLabel(tradeDateStatus))}</strong>
      <small>推荐日 ${escapeHtml(tradeDateStatus.recommendationTradeDate || tradeDateStatus.recommendationDate || "--")} · 数据日 ${escapeHtml(tradeDateStatus.dataAsOfDate || "--")} · 时间 ${escapeHtml(tradeDateStatus.asOfTime || run.scanTime || "--")}</small>
      ${(tradeDateStatus.reasons || []).length ? `<small>${escapeHtml((tradeDateStatus.reasons || []).slice(0, 2).join("；"))}</small>` : ""}
    </div>
    <div class="market-status-card ${marketContextTone(context)}">
      <span>隔夜风险</span>
      <strong>${escapeHtml(riskLevelLabel(context.riskLevel))}</strong>
      <small>策略模式：${escapeHtml(context.strategyMode || "--")} · 仓位 ${escapeHtml(context.suggestedPosition || "--")}</small>
    </div>
    <div class="market-status-card ${Number(policy.maxPickCount || 0) <= 0 ? "bad" : Number(policy.maxPickCount || 0) < 3 ? "warn" : "good"}">
      <span>建议推荐数量</span>
      <strong>${formatInteger(policy.maxPickCount)} / ${formatInteger(policy.requestedLimit || run.finalLimit || 3)}</strong>
      <small>${escapeHtml(policy.reason || "正常最多 3 只")}</small>
    </div>
    <div class="market-status-card ${Number(quality.eligibleForRealTop || 0) > 0 ? "good" : "bad"}">
	      <span>数据质量</span>
	      <strong>${formatInteger(quality.eligibleForRealTop)} 只可交易</strong>
	      <small>完整 ${formatInteger(counts.A)} · 可用 ${formatInteger(counts.B)} · 有限 ${formatInteger(counts.C)} · 不足 ${formatInteger(counts.D)} · 模拟 ${formatInteger(quality.simulatedKeyData)}</small>
	      <small>${escapeHtml(coverageDetail(evidenceCoverage) || `板块证据 可用 ${formatInteger(sectorCounts.available)} · 部分 ${formatInteger(sectorCounts.partial)} · 过期 ${formatInteger(sectorCounts.stale)} · 不可用 ${formatInteger(sectorCounts.unavailable)}`)}</small>
	      <small>${escapeHtml(formatKlineSourceCounts(quality.klineSourceCounts))}</small>
	    </div>
	    <div class="market-status-card ${evidenceTone(evidence)}">
	      <span>T+1冻结证据</span>
	      <strong>${evidence.evidenceRunId ? `#${formatInteger(evidence.evidenceRunId)} · ${escapeHtml(evidenceQualityLabel(evidence))}` : "证据未生成"}</strong>
	      <small>板块 ${formatInteger(evidence.sectorCount)} · 成分股 ${formatInteger(evidence.constituentCount)} · 映射 ${formatInteger(evidence.stockMapCount)} · 分钟线 ${formatInteger(evidence.minuteCount)}</small>
	      <small>${escapeHtml(coverageDetail(evidenceCoverage))}</small>
	      <small>${escapeHtml(evidence.note || "T+1 使用独立证据，不混用早盘雷达。")}</small>
      ${evidence.missing?.length ? `<small class="warning-text">${escapeHtml(evidence.missing.slice(0, 2).join("；"))}</small>` : ""}
    </div>
    <div class="market-status-card ${sectorStateSummaryTone(sectorStateSummary)}">
      <span>板块状态影子</span>
      <strong>${escapeHtml(sectorStateSummaryHeadline(sectorStateSummary))}</strong>
      <small>${escapeHtml(sectorStateCountsText(sectorStateSummary))}</small>
      <small>${escapeHtml(sectorRetreatStage.effectiveEnabled ? "阶段2已生效：退潮普通后排会有限降权，逆势核心保留例外。" : sectorStateSummary.note || "当前只做影子观察，不影响正式 Top3。")}</small>
    </div>
    <div class="market-status-card ${styleRegimeTone(styleRegime)}">
      <span>风格轮动影子</span>
      <strong>${escapeHtml(styleRegime.label || "风格证据待确认")}</strong>
      <small>${escapeHtml(styleRegimeSummaryText(styleRegime))}</small>
      <small>${escapeHtml(stylePromptStage.effectiveEnabled ? "阶段3已生效：该风格证据已进入 DeepSeek 最终比较。" : "当前只做影子聚合，不进入 DeepSeek 正式判断。")}</small>
    </div>
    <div class="market-status-card ${aiParticipationTone(aiParticipation, run, market.ai || {})}">
      <span>AI参与模式</span>
      <strong>${escapeHtml(aiParticipation.label || "--")}</strong>
      <small>${escapeHtml(aiParticipation.reason || "--")}</small>
      ${aiParticipation.allowedClaims?.length ? `<small>允许依据：${escapeHtml(aiParticipation.allowedClaims.join("、"))}</small>` : ""}
    </div>
    <div class="market-status-card ${minuteKlineTone(minuteKline)}">
      <span>分钟线源</span>
      <strong>${formatInteger(minuteKline.ok || minuteKline.attached || 0)} / ${formatInteger(minuteKline.requested || quality.total || 0)}</strong>
      <small>${escapeHtml(formatKlineSourceCounts(minuteKline.sourceCounts) || "等待真实分钟线")}</small>
      ${minuteKline.warning ? `<small class="warning-text" title="${escapeHtml(minuteProviderDetails(minuteKline))}">${escapeHtml(minuteKline.warning)}</small>` : ""}
    </div>
    ${aiEvidenceStatus ? `
      <div class="market-status-card ${aiEvidenceStatus === "passed" || aiEvidenceStatus === "partial_passed" ? "good" : aiEvidenceStatus === "failed" ? "bad" : "warn"}">
        <span>AI证据校验</span>
        <strong>${escapeHtml(aiEvidenceValidationLabel(aiEvidenceStatus))}</strong>
        <small>${escapeHtml((market.aiEvidenceValidationErrors || []).slice(0, 2).join("；") || "DeepSeek 输出事实与候选证据一致")}</small>
      </div>
    ` : ""}
    <div class="market-status-card ${realIndices.length ? "neutral" : "warn"}">
      <span>真实指数环境</span>
      <strong>${formatPct(indexView.mainIndexChangePct, true)}</strong>
      <small>${escapeHtml(formatIndexList(realIndices, 3, "真实指数暂缺"))}</small>
      ${indexWarning ? `<small class="warning-text">${escapeHtml(indexWarning)}</small>` : ""}
    </div>
    <div class="market-status-card ${lossTone}">
      <span>小票亏钱效应</span>
      <strong>${formatPct(indexView.lossEffectChangePct, true)}</strong>
      <small>${escapeHtml(formatIndexList(equalWeightedIndices, 2, "等权数据暂缺"))} · 个股等权辅助指标</small>
    </div>
    <div class="market-status-card neutral">
      <span>市场广度</span>
      <strong>${formatInteger(metrics.up)} 涨 / ${formatInteger(metrics.down)} 跌</strong>
      <small>自然跌停 ${formatInteger(naturalLimitDown)} · ST ${formatInteger(stLimitDown)} · 接近跌停 ${formatInteger(nearLimitDown)}</small>
      <small>总跌停参考 ${formatInteger(totalLimitDown)} · 涨停 ${formatInteger(metrics.limitUp)} · 真实指数最弱 ${formatPct(indexView.mainIndexChangePct, true)}</small>
      <small>小票等权最弱 ${formatPct(indexView.lossEffectChangePct, true)}</small>
      <small>成交额 ${formatMoneyYi(metrics.turnoverAmount)} · 5日比 ${formatRatio(metrics.turnoverRatio5d)}</small>
      ${completeness.complete === false ? `<small class="warning-text">指标不完整：${escapeHtml(completeness.warning || "--")}</small>` : ""}
    </div>
    ${aiAuditHtml(market)}
  `;
}

function aiAuditHtml(market = {}) {
  const audits = Array.isArray(market.aiCallAudits) ? market.aiCallAudits : [];
  if (!audits.length) return "";
  const rows = audits.slice(-4).reverse().map((item) => `
    <div class="audit-row">
      <strong>${escapeHtml(item.stage || "--")} ${formatInteger(item.batchIndex || 0)}</strong>
      <small>${escapeHtml(item.provider || "--")} ${escapeHtml(item.model || "")} · temp ${formatNumber(item.temperature)} · ${item.cacheHit ? "缓存命中" : item.validResponse ? "有效响应" : "无效响应"}</small>
      <small>fp ${escapeHtml(shortHash(item.fingerprint))} · prompt ${escapeHtml(shortHash(item.promptHash))} · response ${escapeHtml(shortHash(item.rawResponseHash))}</small>
      ${item.error ? `<small class="warning-text">${escapeHtml(item.error)}</small>` : ""}
    </div>
  `).join("");
  return `
    <details class="market-status-card neutral audit-card">
      <summary>DeepSeek 审计 ${formatInteger(audits.length)}</summary>
      ${rows}
    </details>
  `;
}

function renderPicks(picks, run = null) {
  if (!els.pickGrid) return;
  if (!picks.length) {
    els.pickGrid.innerHTML = `<div class="empty">${escapeHtml(isZeroPickRun(run) ? zeroPickReason(run) : "任务完成后显示最终 Top3")}</div>${shadowTop3Html(run)}`;
    return;
  }
  const formalHtml = picks.map((pick) => `
    <article class="pick-card">
      <div class="pick-card-head">
        <div>
          <h3>#${formatInteger(pick.aiRank || pick.rank)} ${stockAnalysisNameLink(pick)}</h3>
          <small>候选池 #${formatInteger(pick.poolRank)} · ${escapeHtml(pick.candidateTypeLabel || candidateTypeLabel(pick.candidateType))} · 置信度 ${formatConfidence(pick.confidence)}</small>
        </div>
        <div class="pick-score">${formatNumber(pick.confidence || pick.t1Score || pick.score)}</div>
      </div>
      ${historicalSignalBadge(runSignalDate(run))}
      <div class="pick-card-actions">
        ${stockAnalysisButton(pick)}
        ${entryAuditButton(pick)}
      </div>
      ${riskBadgeHtml(pick)}
      ${dataQualityBadgeHtml(pick)}
      ${sectorEvidenceBadgeHtml(pick)}
      ${sectorStateBadgeHtml(pick)}
      ${aiEvidenceValidationHtml(pick)}
      ${t1ScoreBreakdownHtml(pick)}
      ${sectorSemanticsHtml(pick)}
      ${pick.criticReplacement ? `<div class="critic-note">Critic 替换：${escapeHtml(pick.criticReplacement.reason || "--")}</div>` : ""}
      <dl>
        ${pick.strategyHypothesis ? `<div><dt>策略假设</dt><dd>${explainTradingTermsHtml(pick.strategyHypothesis)}</dd></div>` : ""}
        <div><dt>入选理由</dt><dd>${explainTradingTermsHtml(pick.reason || "--")}</dd></div>
        ${evidenceRefsHtml(pick)}
        ${pick.missingEvidence?.length ? `<div><dt>需要注意</dt><dd>${explainTradingTermsHtml(pick.missingEvidence.join("；"))}</dd></div>` : ""}
        ${pick.marketFit ? `<div><dt>市场环境</dt><dd>${explainTradingTermsHtml(pick.marketFit)}</dd></div>` : ""}
        ${pick.sectorFit ? `<div><dt>板块情况</dt><dd>${explainTradingTermsHtml(pick.sectorFit)}</dd></div>` : ""}
        <div><dt>今日买入条件</dt><dd>${explainTradingTermsHtml(pick.buyPlan || "--")}</dd></div>
        <div><dt>明日卖出计划</dt><dd>${explainTradingTermsHtml(pick.sellPlan || "--")}</dd></div>
        <div><dt>隔夜风险</dt><dd>${explainTradingTermsHtml(pick.risk || "--")}</dd></div>
        ${pick.entryCondition ? `<div><dt>触发条件</dt><dd>${explainTradingTermsHtml(pick.entryCondition)}</dd></div>` : ""}
        <div><dt>失效条件</dt><dd>${explainTradingTermsHtml(pick.invalidCondition || "--")}</dd></div>
        ${pick.confidenceReason ? `<div><dt>为什么有把握</dt><dd>${explainTradingTermsHtml(pick.confidenceReason)}</dd></div>` : ""}
      </dl>
    </article>
  `).join("");
  els.pickGrid.innerHTML = `${formalHtml}${shadowTop3Html(run)}`;
}

function shadowTop3Html(run) {
  const market = run?.market || {};
  const shadow = market.shadowTop3 || {};
  const aiShadow = market.shadowDeepSeekTailTop3 || {};
  const comparison = market.shadowComparison || {};
  const picks = Array.isArray(shadow.picks) ? shadow.picks : [];
  const aiPicks = Array.isArray(aiShadow.picks) ? aiShadow.picks : [];
  if (!picks.length && !aiPicks.length && !aiShadow.error && !aiShadow.reason) return "";
  const finalCodes = new Set(Array.isArray(comparison.finalCodes) ? comparison.finalCodes : []);
  const changedCount = Number(comparison.changedCount || 0);
  const aiChangedCount = Number(comparison.deepSeekTailChangedCount || 0);
  return `
    <section class="shadow-top3-panel">
      <div class="shadow-top3-head">
        <div>
          <strong>影子 Top3 对比</strong>
          <small>只做观察，不影响正式推荐。用于验证尾盘承接、低开风险和 AI 尾盘证据是否有增量价值。</small>
        </div>
        <span class="shadow-badge">规则${changedCount > 0 ? `不同 ${formatInteger(changedCount)} 只` : "一致"} · AI${aiPicks.length ? (aiChangedCount > 0 ? `不同 ${formatInteger(aiChangedCount)} 只` : "一致") : "未生成"}</span>
      </div>
      ${shadowPickGroupHtml("规则尾盘影子", "系统假设更重视尾盘承接时的规则对照。", picks, finalCodes)}
      ${shadowPickGroupHtml("AI 尾盘影子", "DeepSeek 额外看到 T1 尾盘细节后重新终选，只做复盘对照。", aiPicks, finalCodes)}
      ${!aiPicks.length && aiShadow.reason ? `<p class="shadow-top3-note">${escapeHtml(aiShadow.reason)}</p>` : ""}
      ${aiShadow.error && !aiPicks.length ? `<p class="shadow-top3-note">${escapeHtml(aiShadow.error)}</p>` : ""}
      ${comparison.reason ? `<p class="shadow-top3-note">${escapeHtml(comparison.reason)}</p>` : ""}
    </section>
  `;
}

function shadowPickGroupHtml(title, note, picks, finalCodes) {
  if (!picks.length) return "";
  return `
    <div class="shadow-top3-group">
      <div class="shadow-group-title">
        <strong>${escapeHtml(title)}</strong>
        <small>${escapeHtml(note)}</small>
      </div>
      <div class="shadow-top3-grid">
        ${picks.map((pick) => {
          const code = String(pick.code || "").padStart(6, "0");
          const tail = pick.tailSupportShadow || pick.tailSupportPackV2 || {};
          const gap = pick.gapDownRiskShadow || pick.gapDownRiskPack || {};
          return `
            <article class="shadow-pick-card ${finalCodes.has(code) ? "same" : "diff"}">
              <div>
                <strong>#${formatInteger(pick.shadowRank || pick.aiRank || pick.rank)} ${stockAnalysisNameLink(pick)}</strong>
                <small>${finalCodes.has(code) ? "也在正式Top3" : "影子替代候选"} · 影子分 ${formatNumber(pick.shadowT1Score || pick.t1Score || pick.score)}</small>
              </div>
              <dl>
                <div><dt>尾盘承接</dt><dd>${escapeHtml(tail.label || "--")} ${formatNumber(tail.score)}</dd></div>
                <div><dt>低开风险</dt><dd>${escapeHtml(gap.label || "--")} ${formatNumber(gap.score)}</dd></div>
                ${tail.reasons?.length ? `<div><dt>观察原因</dt><dd>${escapeHtml(tail.reasons.slice(0, 2).join("；"))}</dd></div>` : ""}
              </dl>
            </article>
          `;
        }).join("")}
      </div>
    </div>
  `;
}

function renderOutcomes(run) {
  if (!els.outcomeSummary || !els.outcomeRows) return;
  const outcomes = run?.outcomes || [];
  const summary = run?.outcomeSummary || {};
  const tradeable = run?.tradeableSummary || {};
  const status = run?.outcomeStatus || (run ? "not_ready" : "empty");
  const hint = outcomeStatusHint(status, run);
  els.outcomeSummary.innerHTML = [
    metricCard("验证状态", outcomeStatusLabel(status), hint),
    metricCard("专业样本", `${formatInteger(tradeable.tradeableCount || 0)} / ${formatInteger(tradeable.count || outcomes.length)}`, tradeable.primaryScenario || "固定 open_15m"),
    metricCard("平均净收益", formatPct(tradeable.avgNetReturnPct), "扣滑点、佣金、印花税"),
    metricCard("规则卖出", formatPct(tradeable.avgRuleNetReturnPct), `样本 ${formatInteger(tradeable.ruleTradeableCount || 0)}`),
    metricCard("成功数量", formatInteger(summary.successCount || 0), "强成功 / 成功溢价 / 弱成功"),
    metricCard("分钟缺失", formatInteger(tradeable.minuteMissingCount || 0), "辅助分钟场景无法还原时标记缺失"),
    metricCard("盘中机会", formatPct(summary.avgHighReturnPct), "旧口径，仅作参考"),
    metricCard("平均回撤", formatPct(summary.avgMaxDrawdownPct), "次日最低相对买入参考")
  ].join("");
  renderBaselineComparison(run?.baselineComparison || {}, run?.market?.shadowComparison || {});
  if (!outcomes.length) {
    els.outcomeRows.innerHTML = `<tr><td colspan="15" class="empty">${escapeHtml(hint)}</td></tr>`;
    return;
  }
  els.outcomeRows.innerHTML = outcomes.map((item) => `
    <tr>
      <td>
        <strong>${stockAnalysisNameLink(item)}</strong>
        <small>${escapeHtml((item.outcome || {}).riskProfile?.label || "")}</small>
      </td>
      <td>${formatPrice(item.buyReferencePrice)}</td>
      <td>${escapeHtml(item.nextTradeDate || "--")}</td>
      <td>${scenarioNetHtml(primaryScenario(item))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "open"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "open_5m"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "open_15m"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "open_30m"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "midday_close"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "afternoon_open"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "two_pm"))}</td>
      <td>${scenarioNetHtml(scenarioByKey(item, "close"))}</td>
      <td>${scenarioNetHtml(ruleScenario(item))}</td>
      <td>${tradeQualityHtml(item)}</td>
      <td>
        <span class="label-chip ${outcomeTone(item.outcomeLabel)}">${escapeHtml(item.outcomeLabel || "--")}</span>
        <small>${escapeHtml(item.failureReason || item.dataSource || "")}</small>
        ${failureTagsHtml(item)}
        ${theoreticalDetailsHtml(item)}
      </td>
    </tr>
  `).join("");
}

function renderBaselineComparison(comparison, shadowComparison = {}) {
  if (!els.baselineComparison) return;
  const shadowHtml = shadowOutcomeComparisonHtml(shadowComparison?.outcomeComparison || {});
  const groups = [
    ["rules", "规则基线"],
    ["momentum", "涨幅基线"],
    ["tailSupportShadow", "尾盘影子"],
    ["deepSeekTailShadow", "AI尾盘影子"],
    ["random", "随机基线"],
    ["market", "全池等权"],
    ["sector", "板块同类基线"],
    ["aiPreCritic", "AI终选前"],
    ["final", "最终结果"],
    ["index", "指数基准"],
    ["portfolio", "组合口径"],
    ["criticPair", "Critic对照"]
  ];
  if (!groups.some(([key]) => comparison[key]) && !shadowHtml) {
    els.baselineComparison.innerHTML = "";
    return;
  }
  const cardsHtml = groups.map(([key, label]) => {
    const item = comparison[key] || {};
    const status = item.status === "ok" ? "good" : item.status === "unavailable" ? "warn" : "neutral";
    const mainValue = item.deltaPct !== undefined && item.deltaPct !== null
      ? item.deltaPct
      : item.portfolioReturnPct !== undefined && item.portfolioReturnPct !== null
        ? item.portfolioReturnPct
        : item.avgNetReturnPct;
    const detail = item.deltaPct !== undefined && item.deltaPct !== null
      ? `前 ${formatPct(item.prePortfolioReturnPct)} · 后 ${formatPct(item.postPortfolioReturnPct)}`
      : `样本 ${formatInteger(item.tradeableCount || item.count)} · 成功率 ${formatPct(item.successRate, true)}${item.cashSlots !== undefined ? ` · 现金槽 ${formatInteger(item.cashSlots)}` : ""}`;
    return `
      <div class="baseline-card ${status}">
        <span>${escapeHtml(label)}</span>
        <strong>${mainValue === undefined || mainValue === null ? "--" : formatPct(mainValue)}</strong>
        <small>${escapeHtml(detail)}</small>
        ${item.reason ? `<small>${escapeHtml(item.reason)}</small>` : ""}
      </div>
    `;
  }).join("");
  els.baselineComparison.innerHTML = `${cardsHtml}${shadowHtml}`;
}

function shadowOutcomeComparisonHtml(comparison = {}) {
  const rows = Array.isArray(comparison.rows) ? comparison.rows : [];
  const aiRows = Array.isArray(comparison.deepSeekTailRows) ? comparison.deepSeekTailRows : [];
  if (!rows.length && !aiRows.length) return "";
  const finalPortfolio = comparison.final?.portfolioReturnPct;
  const shadowPortfolio = comparison.shadow?.portfolioReturnPct;
  const aiShadowPortfolio = comparison.deepSeekTail?.portfolioReturnPct;
  const delta = comparison.deltaPortfolioReturnPct;
  const aiDelta = comparison.deepSeekTailDeltaPortfolioReturnPct;
  return `
    <section class="shadow-outcome-panel">
      <div class="shadow-outcome-head">
        <div>
          <strong>影子 Top3 次日表现</strong>
          <small>只做观察，不进入正式复盘学习</small>
        </div>
        <span class="shadow-badge">${delta === undefined || delta === null ? "等待完整数据" : `相对正式 ${formatPct(delta)}`}</span>
      </div>
      <div class="shadow-outcome-summary">
        <span>正式组合 open_15m：${formatPct(finalPortfolio)}</span>
        <span>规则影子 open_15m：${formatPct(shadowPortfolio)}${delta === undefined || delta === null ? "" : ` (${formatPct(delta)})`}</span>
        <span>AI尾盘影子 open_15m：${formatPct(aiShadowPortfolio)}${aiDelta === undefined || aiDelta === null ? "" : ` (${formatPct(aiDelta)})`}</span>
        <span>重合 ${formatInteger((comparison.overlapCodes || []).length)} 只 · 替换 ${formatInteger(comparison.changedCount || 0)} 只</span>
      </div>
      ${shadowOutcomeRowsHtml("规则尾盘影子", rows)}
      ${shadowOutcomeRowsHtml("AI 尾盘影子", aiRows)}
      ${comparison.reason ? `<p class="shadow-top3-note">${escapeHtml(comparison.reason)}</p>` : ""}
    </section>
  `;
}

function shadowOutcomeRowsHtml(title, rows) {
  if (!rows.length) return "";
  return `
    <div class="shadow-outcome-group">
      <strong>${escapeHtml(title)}</strong>
      <div class="shadow-outcome-table">
        ${rows.map((row) => `
          <div class="shadow-outcome-row">
            <strong>#${formatInteger(row.shadowRank)} ${stockAnalysisNameLink(row)}</strong>
            <span>${escapeHtml(row.selectionRole || (row.inFormalTop3 ? "重合" : "影子替代"))}</span>
            <span>开盘 ${formatPct(row.openReturnPct)}</span>
            <span>5分钟 ${formatPct(row.open5mReturnPct)}</span>
            <span>15分钟 ${formatPct(row.open15mReturnPct)}</span>
            <span>${escapeHtml(row.outcomeLabel || row.dataStatus || "--")}</span>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

function renderRows(items) {
  if (!els.detailRows) return;
  const rows = items.filter((item) => item.selected || item.stage !== "pool").slice(0, 160);
  if (!rows.length) {
    els.detailRows.innerHTML = `<tr><td colspan="8" class="empty">暂无明细</td></tr>`;
    return;
  }
  els.detailRows.innerHTML = rows.map((item) => {
    const raw = item.item || {};
    return `
      <tr>
        <td>${item.selected ? `#${formatInteger(item.aiRank)}` : "--"}</td>
        <td>#${formatInteger(item.poolRank)}</td>
        <td>
          <strong>${stockAnalysisNameLink(item)}</strong>
          <small>${escapeHtml(sectorDisplayText(raw))}</small>
        </td>
        <td>${formatNumber(item.score || raw.t1Score)}</td>
        <td>
          ${stageLabel(item.stage)}
          <small>${escapeHtml(raw.candidateTypeLabel || candidateTypeLabel(item.candidateType))}</small>
          ${riskBadgeHtml(item, raw)}
          ${dataQualityBadgeHtml(raw)}
          ${sectorEvidenceBadgeHtml(raw)}
          ${sectorStateBadgeHtml(raw)}
        </td>
        <td>${escapeHtml(item.buyPlan || "--")}</td>
        <td>${escapeHtml(item.sellPlan || "--")}</td>
        <td>${escapeHtml([item.risk, item.invalidCondition].filter(Boolean).join(" / ") || "--")}</td>
      </tr>
    `;
  }).join("");
}

function scenarioByKey(item, key) {
  const scenarios = ((item.outcome || {}).tradeable || {}).scenarios || [];
  return scenarios.find((scenario) => scenario.scenarioKey === key) || {};
}

function primaryScenario(item) {
  return ((item.outcome || {}).tradeable || {}).primaryScenario || {};
}

function ruleScenario(item) {
  const rule = ((item.outcome || {}).tradeable || {}).ruleExecution || {};
  if (rule.scenarioKey) return rule;
  return scenarioByKey(item, "rule_exit");
}

function scenarioNetHtml(scenario) {
  if (!scenario || !scenario.scenarioKey) return `<span class="muted-text">--</span>`;
  if (scenario.dataStatus !== "ok" || scenario.netReturnPct === null || scenario.netReturnPct === undefined) {
    const label = scenario.dataStatus === "minute_missing" ? "缺分钟" : scenario.dataStatus === "unavailable" ? "不可用" : "不可用";
    return `<span class="muted-text" title="${escapeHtml(scenario.reason || scenario.dataStatus || "")}">${label}</span>`;
  }
  const value = Number(scenario.netReturnPct);
  const tone = value >= 1 ? "good-text" : value < 0 ? "bad-text" : "warning-text";
  const status = scenario.validationStatus || scenario.status || "";
  const statusText = status && status !== "final" ? ` · ${validationStatusLabel(status)}` : "";
  return `
    <strong class="${tone}">${formatPct(value)}</strong>
    <small>毛 ${formatPct(scenario.grossReturnPct)} · ${escapeHtml(scenario.scenarioLabel || "")}${escapeHtml(statusText)}</small>
  `;
}

function tradeQualityHtml(item) {
  const outcome = item.outcome || {};
  const quality = ((item.outcome || {}).tradeable || {}).quality || {};
  const parts = [];
  if (quality.highOpenFade) parts.push("高开低走");
  if (quality.lowOpenRebound) parts.push("低开冲高");
  if (quality.intradayPullback) parts.push("冲高回落");
  const rule = ((item.outcome || {}).tradeable || {}).ruleExecution || {};
  const exclusion = Array.isArray(outcome.exclusionReasons) && outcome.exclusionReasons.length ? ` · ${outcome.exclusionReasons[0]}` : "";
  return `
    <span>${escapeHtml(parts.join(" / ") || "常规")}</span>
    <small>最高 ${formatPct(item.highReturnPct)} · 回撤 ${formatPct(item.maxDrawdownPct)}</small>
    <small>${escapeHtml(rule.label || "")}${rule.netReturnPct !== undefined && rule.netReturnPct !== null ? ` ${formatPct(rule.netReturnPct)}` : ""}</small>
    <small>${escapeHtml(validationStatusLabel(outcome.validationStatus))} · ${outcome.learningEligible ? "进入学习" : "不进学习"}${escapeHtml(exclusion)}</small>
  `;
}

function failureTagsHtml(item) {
  const tags = (item.outcome || {}).failureTags || ((item.outcome || {}).tradeable || {}).quality?.failureTags || [];
  if (!Array.isArray(tags) || !tags.length) return "";
  return `<small>${tags.map(failureTagLabel).join(" · ")}</small>`;
}

function failureTagLabel(tag) {
  return {
    primary_unavailable: "主KPI缺失",
    low_open: "低开",
    drawdown_invalid: "回撤失效",
    intraday_pullback: "冲高回落",
    premium_not_captured: "溢价未兑现",
    close_weak: "收盘弱",
    market_drag: "市场拖累",
    sector_fade: "板块退潮",
    data_quality: "数据质量",
    price_mismatch: "价格口径",
    corporate_action: "公司行为",
    liquidity: "流动性"
  }[tag] || tag;
}

function theoreticalDetailsHtml(item) {
  const data = (item.outcome || {}).theoretical || {};
  if (!data.openReturnPct && !data.highReturnPct && !data.closeReturnPct) return "";
  return `
    <details class="outcome-details">
      <summary>盘中机会参考</summary>
      <span>开 ${formatPct(data.openReturnPct)} · 高 ${formatPct(data.highReturnPct)} · 低 ${formatPct(data.lowReturnPct)} · 收 ${formatPct(data.closeReturnPct)}</span>
    </details>
  `;
}

function renderCriticForm() {
  if (!hasCriticForm()) return;
  const config = state.criticConfig || {};
  els.criticModeSelect.value = config.mode || "auto";
  els.criticMinRunsInput.value = String(config.minCompletedRuns ?? 10);
  els.criticMinOutcomesInput.value = String(config.minVerifiedOutcomes ?? 20);
  els.criticMinTradeableInput.value = String(config.minTradeableValidationSamples ?? 20);
  els.criticMaxReplacementsInput.value = String(config.maxReplacements ?? 1);
  els.criticHistoryWindowInput.value = String(config.historyWindowRuns ?? 20);
  els.criticRequireOutcomesCheck.checked = config.requireOutcomes !== false;
  els.criticAllowReduceCheck.checked = config.allowReducePickCount !== false;
  els.criticAllowZeroCheck.checked = config.allowZeroPick !== false;
}

function renderExecutionForm() {
  if (!hasExecutionForm()) return;
  const config = state.executionConfig || {};
  els.buySlippageInput.value = formatConfigNumber(config.buySlippagePct ?? 0.1);
  els.sellSlippageInput.value = formatConfigNumber(config.sellSlippagePct ?? 0.1);
  els.commissionInput.value = formatConfigNumber(config.commissionPct ?? 0.025);
  els.stampTaxInput.value = formatConfigNumber(config.stampTaxPct ?? 0.05);
  els.buyTimesInput.value = (config.buyTimes || ["14:30", "14:40", "14:50"]).join(",");
  els.sellTimesInput.value = (config.sellTimes || ["open", "open_5m", "open_15m", "open_30m", "midday_close", "afternoon_open", "two_pm", "close"]).join(",");
}

function renderStageForm() {
  if (!hasStageForm()) return;
  const config = state.stageConfig || {};
  els.stageSectorShadowCheck.checked = config.sectorStateShadowEnabled !== false;
  els.stageSectorRetreatMode.value = config.sectorRetreatDowngradeMode || "off";
  els.stageStyleShadowCheck.checked = config.styleRegimeShadowEnabled !== false;
  els.stageStylePromptMode.value = config.styleRegimePromptMode || "off";
  els.stage2OfficialDaysInput.value = String(config.minOfficialTradeDaysForStage2 ?? 30);
  els.stage2TradeableInput.value = String(config.minTradeableOutcomesForStage2 ?? 30);
  els.stage3OfficialDaysInput.value = String(config.minOfficialTradeDaysForStage3 ?? 60);
  els.stage3TradeableInput.value = String(config.minTradeableOutcomesForStage3 ?? 60);
  els.stageSectorRetreatMode.disabled = !els.stageSectorShadowCheck.checked;
  els.stageStylePromptMode.disabled = !els.stageStyleShadowCheck.checked;
}

function renderStageStatusLine() {
  if (!els.stageStatusLine) return;
  const status = state.stageStatus || {};
  const samples = status.sampleStatus || {};
  const stage2 = status.sectorRetreatDowngrade || {};
  const stage3 = status.styleRegimePrompt || {};
  els.stageStatusLine.innerHTML = `
    <span class="label-chip ${stage2Tone(stage2)}">${escapeHtml(stageModeStatusLabel(stage2))}</span>
    <span>板块退潮降权：${escapeHtml(stageModeStatusLabel(stage2))}</span>
    <span class="label-chip ${stage3Tone(stage3)}">${escapeHtml(stageModeStatusLabel(stage3))}</span>
    <span>风格轮动参与 AI：${escapeHtml(stageModeStatusLabel(stage3))}</span>
    <span>当前 official 交易日 ${formatInteger(samples.officialTradeDays)} · open_15m 有效样本 ${formatInteger(samples.tradeableOutcomes)}</span>
    <span>${escapeHtml(stage2.reason || stage3.reason || "等待阶段配置状态")}</span>
  `;
}

function stageModeStatusLabel(stage = {}) {
  if (stage.experimental) return "实验模式";
  if (stage.effectiveEnabled) return "已生效";
  if (stage.requested || stage.requestedMode === "auto") return "等待样本";
  return "已关闭";
}

function stage2Tone(stage = {}) {
  if (stage.experimental) return "warn";
  if (stage.effectiveEnabled) return "good";
  return stage.requested ? "warn" : "neutral";
}

function stage3Tone(stage = {}) {
  if (stage.experimental) return "warn";
  if (stage.effectiveEnabled) return "good";
  return stage.requested ? "warn" : "neutral";
}

function renderCriticStatusLine() {
  if (!els.criticStatusLine) return;
  const status = state.criticStatus || {};
  els.criticStatusLine.innerHTML = `
    <span class="label-chip ${status.enabled ? "good" : status.mode === "off" ? "neutral" : "warn"}">${escapeHtml(status.enabled ? "已满足介入条件" : "未介入")}</span>
    <span>${escapeHtml(criticModeLabel(status.mode))}</span>
    <span>推荐 ${formatInteger(status.completedRuns)} / ${formatInteger(status.minCompletedRuns)}</span>
    <span>验证 ${formatInteger(status.verifiedOutcomes)} / ${formatInteger(status.minVerifiedOutcomes)}</span>
    <span>专业验证 ${formatInteger(status.tradeableOutcomes)} / ${formatInteger(status.minTradeableValidationSamples)}</span>
    <span>最多替换 ${formatInteger(status.maxReplacements)}</span>
    <span>${status.allowReducePickCount ? "可减数量" : "不减数量"}</span>
    <span>${status.allowZeroPick ? "允许Top0" : "不允许Top0"}</span>
    <span>${escapeHtml(status.reason || "--")}</span>
  `;
}

function renderStrategyFeedback() {
  if (!hasStrategyFeedbackPanel()) return;
  const feedback = state.strategyFeedback || {};
  const audit = feedback.sampleAudit || {};
  const portfolio = feedback.portfolio || {};
  const exclusions = Array.isArray(audit.exclusionReasons) ? audit.exclusionReasons : [];
  els.feedbackStats.innerHTML = [
    metricCard("严格样本", `${formatInteger(feedback.verifiedOutcomes)} / ${formatInteger(audit.candidateOutcomes ?? feedback.verifiedOutcomes)}`, `最近 ${formatInteger(feedback.windowRuns)} 个 official 交易日`),
    metricCard("组合天数", `${formatInteger(portfolio.verifiedDays || 0)} / ${formatInteger(portfolio.officialDays || 0)}`, `Top0 ${formatInteger(portfolio.zeroPickDays || 0)} 天`),
    metricCard("组合收益", formatPct(portfolio.avgPortfolioReturnPct), `累计 ${formatPct(portfolio.cumulativePortfolioReturnPct)}`),
    metricCard("组合超额", formatPct(portfolio.avgExcessReturnPct), `指数基准 ${formatPct(portfolio.avgBenchmarkReturnPct)}`),
    metricCard("成功率", formatPct(feedback.successRate, true), "固定 15m 净收益 >= 0.2%"),
    metricCard("强成功率", formatPct(feedback.strongSuccessRate, true), "固定 15m 净收益 >= 3%"),
    metricCard("平均净收益", formatPct(feedback.avgNetReturnPct), "固定 15m，扣交易成本"),
    metricCard("平均回撤", formatPct(feedback.avgMaxDrawdownPct), "次日最低相对买入价"),
    metricCard("低开失败", formatPct(feedback.lowOpenFailRate, true), "failureTags: low_open"),
    metricCard("回撤失效", formatPct(feedback.drawdownInvalidRate, true), "failureTags: drawdown_invalid"),
    metricCard("排除样本", formatInteger(audit.excludedOutcomes || 0), exclusions.slice(0, 2).map((item) => `${item.reason} ${formatInteger(item.count)}`).join(" · ") || "暂无")
  ].join("");
  const best = feedback.bestPatterns || [];
  const worst = feedback.worstPatterns || [];
  els.feedbackPatterns.innerHTML = `
    <div class="pattern-column">
      <h3>表现较好</h3>
      ${renderPatternList(best, "good")}
    </div>
    <div class="pattern-column">
      <h3>表现较差</h3>
      ${renderPatternList(worst, "warn")}
    </div>
  `;
  renderStrategySuggestions(feedback);
  els.feedbackPrompt.textContent = feedback.promptSummary || "暂无策略复盘样本。";
}

function renderStrategySuggestions(feedback) {
  const suggestions = feedback.suggestions || [];
  const pending = suggestions.filter((item) => ["draft", "observing"].includes(item.status));
  const active = suggestions.filter((item) => item.status === "active");
  els.feedbackSuggestions.innerHTML = `
    <section class="suggestion-column">
      <div class="suggestion-head">
        <h3>待确认建议</h3>
        <span>${formatInteger(pending.length)}</span>
      </div>
      ${pending.length ? pending.slice(0, 6).map(renderSuggestionCard).join("") : `<div class="empty compact">暂无待确认策略建议</div>`}
    </section>
    <section class="suggestion-column">
      <div class="suggestion-head">
        <h3>已启用约束</h3>
        <span>${formatInteger(active.length)}</span>
      </div>
      ${active.length ? active.slice(0, 6).map(renderSuggestionCard).join("") : `<div class="empty compact">暂无已启用策略约束</div>`}
    </section>
  `;
}

function renderSuggestionCard(item) {
  const tone = suggestionTone(item);
  return `
    <article class="suggestion-card">
      <div class="suggestion-title">
        <span class="label-chip ${tone}">${escapeHtml(suggestionActionLabel(item.suggestionAction))}</span>
        <strong>${escapeHtml(item.patternLabel || item.patternKey || "--")}</strong>
      </div>
      <small>样本 ${formatInteger(item.sampleCount)} · 成功 ${formatPct(item.successRate, true)} · 强成功 ${formatPct(item.strongSuccessRate, true)} · 净收益 ${formatPct(item.avgNetReturnPct)} · 回撤 ${formatPct(item.avgMaxDrawdownPct)} · 回撤失效 ${formatPct(item.drawdownInvalidRate, true)}</small>
      <p>${escapeHtml(item.suggestionReason || "--")}</p>
      <div class="suggestion-actions">
        ${suggestionButtons(item)}
      </div>
    </article>
  `;
}

function suggestionButtons(item) {
  const id = Number(item.id || 0);
  if (!id) return "";
  if (item.status === "active") {
    return suggestionButton(id, "revoked", "撤销");
  }
  if (item.status === "draft") {
    return [
      suggestionButton(id, "active", "确认启用"),
      suggestionButton(id, "observing", "仅观察"),
      suggestionButton(id, "rejected", "忽略")
    ].join("");
  }
  if (item.status === "observing") {
    return [
      suggestionButton(id, "active", "确认启用"),
      suggestionButton(id, "rejected", "忽略")
    ].join("");
  }
  return "";
}

function suggestionButton(id, status, label) {
  return `<button class="mini-button" type="button" data-suggestion-id="${id}" data-suggestion-action="${escapeHtml(status)}">${escapeHtml(label)}</button>`;
}

function renderPatternList(patterns, tone) {
  if (!patterns.length) return `<div class="empty compact">暂无足够重复样本</div>`;
  return patterns.slice(0, 5).map((item) => `
    <div class="pattern-row">
      <span class="label-chip ${tone}">${escapeHtml(item.pattern || "--")}</span>
      <strong>${formatPct(item.successRate, true)}</strong>
      <small>样本 ${formatInteger(item.count)} · 净 ${formatPct(item.avgNetReturnPct)} · 低开 ${formatPct(item.lowOpenFailRate, true)} · 回撤失效 ${formatPct(item.drawdownInvalidRate, true)}</small>
    </div>
  `).join("");
}

function historyDryRunSummary(payload = {}) {
  const actions = payload.actions || {};
  const samples = Array.isArray(payload.samples) ? payload.samples : [];
  const sampleText = samples
    .filter((item) => item.suspectedPriceMismatch || item.code === "603629")
    .slice(0, 3)
    .map((item) => `${item.code} ${item.name || ""} ${item.exclusionReasons?.[0] || ""}`.trim())
    .join("；");
  return [
    "历史预演完成，未写入数据库。",
    `run ${formatInteger(payload.runs)} 个，outcome ${formatInteger(payload.outcomes)} 条，空仓 run ${formatInteger(payload.zeroPickRuns)} 个。`,
    `可学习 ${formatInteger(actions.learningEligible)} 条，主 KPI 不可用 ${formatInteger(actions.primaryUnavailable)} 条，疑似隔离 ${formatInteger(actions.markInvalidOrQuarantine)} 条。`,
    sampleText ? `重点样本：${sampleText}` : ""
  ].filter(Boolean).join(" ");
}

function historySoftCleanupSummary(payload = {}) {
  const actions = payload.actions || {};
  const audit = payload.feedback?.sampleAudit || {};
  return [
    payload.message || "历史软清理已完成。",
    `本次处理 run ${formatInteger(payload.appliedRuns || 0)} 个。`,
    `已软清理旧 run ${formatInteger(payload.alreadySoftCleanedRuns || 0)} 个。`,
    `旧样本仅展示 ${formatInteger(actions.keepDisplayOnly || 0)} 条，疑似价格异常 ${formatInteger(actions.markInvalidOrQuarantine || 0)} 条。`,
    `当前严格学习样本 ${formatInteger(payload.feedback?.verifiedOutcomes || 0)} 条，候选复盘样本 ${formatInteger(audit.candidateOutcomes || 0)} 条。`
  ].join(" ");
}

function buildNotice(run) {
  const messages = [];
  const autoNotice = autoRunNotice();
  if (autoNotice) messages.push(autoNotice);
  if (isHistoricalSignalDate(runSignalDate(run))) messages.push(`当前展示的是历史 T+1 候选 · ${runSignalDate(run)}，不是今日信号。建议运行今日 T+1 筛选后再审计。`);
  if (run.timeHint) messages.push(run.timeHint);
  if (run.reusedExisting) messages.push("已复用今天同参数的最近结果；勾选“强制重新生成”才会重新拉行情并调用 DeepSeek。");
  const source = (run.market || {}).sourceStatus || {};
  const freeze = (run.market || {}).marketSnapshotFreeze || {};
  const critic = (run.market || {}).critic || {};
  const context = (run.market || {}).marketContext || {};
  const policy = (run.market || {}).recommendationPolicy || {};
  const evidence = (run.market || {}).t1Evidence || {};
  const aiParticipation = (run.market || {}).aiParticipation || {};
  if (isQuickResearchRun(run)) messages.push(`当前为快速研究模式，候选池低于 ${OFFICIAL_MIN_POOL_SIZE}，不能作为正式复盘学习样本。`);
  if (source.isT1Frozen || freeze.reused) messages.push(`已复用同日全市场冻结快照，生成时间 ${source.freezeGeneratedAt || freeze.generatedAt || "--"}，避免连续重跑候选池跳动。`);
  if (evidence.evidenceRunId) messages.push(`当前使用 T+1 独立冻结证据 #${formatInteger(evidence.evidenceRunId)}，不混用早盘雷达。`);
  if (evidence.reusedExisting) messages.push(`本次复用同日证据，${evidence.reuseReason || "避免连续重跑证据抖动。"}`);
  if (aiParticipation.mode === "limited_ai_no_sector") messages.push("AI 当前为无板块有限参与：可比较个股量价和市场风险，但不得写正向行业/概念结论。");
  if (aiParticipation.mode === "research_ai") messages.push("关键行情证据不足，AI 只进入研究模式，不生成真实 Top。");
  if (source.isMock) messages.push("当前结果来自 Mock 模拟数据，不可用于真实推荐。");
  if (source.isCache) messages.push(`当前使用全市场缓存${source.cacheStale ? "，缓存已过期" : ""}。`);
  if (["panic", "heavy_down"].includes(context.state)) messages.push(`市场状态为${context.label}，系统允许少选或推荐 0 只。`);
  if (Number(policy.maxPickCount || run.finalLimit || 3) < Number(run.finalLimit || 3)) messages.push(policy.reason || "市场/数据质量限制了推荐数量。");
  if (run.selectionMode === "rules_fallback") messages.push("DeepSeek AI 未产出有效终选，当前为规则兜底结果。");
  if (run.selectionMode === "ai_semifinal_rules_final") {
    messages.push(String(run.error || "").includes("Top0")
      ? "DeepSeek 终选选择不选，当前保留 AI 初筛候选池，并在半决赛池内按本地 T+1 分给出观察结果。"
      : "DeepSeek 终选未通过证据校验，当前保留 AI 初筛候选池，并在半决赛池内按本地 T+1 分终选。");
  }
  if (isZeroPickRun(run)) messages.push(zeroPickReason(run));
  if (critic.reason && run.selectionMode === "ai_t1_overnight") messages.push(`AI 自检：${critic.reason}`);
  if (run.error) messages.push(run.error);
  return uniqueMessages(messages).join(" ");
}

function progressDetail(run) {
  if (String(run.currentStage || "").startsWith("batch:")) {
    return `DeepSeek 正在批次初筛，已完成 ${formatInteger(run.completedBatchCount)} / ${formatInteger(run.batchCount)}。`;
  }
  if (run.currentStage === "loading_market") return "正在尝试新浪/AKShare 真实全市场行情。";
  if (run.currentStage === "building_pool") return "正在冻结 T+1 独立证据，并计算资金、板块、分时和隔夜风险特征。";
  if (run.currentStage === "final") return `半决赛池 ${formatInteger(run.semifinalCount)} 只，正在终选 Top${formatInteger(run.finalLimit)}。`;
  if (run.currentStage === "critic") return "DeepSeek 终选后进入 AI 自检，按配置决定是否替换。";
  if (run.status === "pending") return "任务已创建，等待后台线程开始。";
  if (run.status === "done" && isZeroPickRun(run)) return "当前为空仓/避险决策，没有最终 Top3。";
  if (run.status === "done") return `已生成 ${formatInteger(run.finalCount)} 只最终结果。`;
  if (run.status === "failed") return run.error || "任务失败。";
  return "等待进度更新。";
}

function sourceDetail(source, market) {
  if (source.isMock) return source.fallbackReason || source.warning || "模拟数据只用于页面和接口验证。";
  if (source.isT1Frozen || (market.marketSnapshotFreeze || {}).reused) {
    const generatedAt = source.freezeGeneratedAt || (market.marketSnapshotFreeze || {}).generatedAt || "--";
    return `复用同日全市场冻结快照，生成时间 ${generatedAt}。`;
  }
  if (source.isCache) {
    const stale = source.cacheStale ? "已过期" : "未过期";
    return `生成时间 ${source.cacheGeneratedAt || "--"}，${stale}。`;
  }
  return source.warning || market.providerWarning || "真实全市场行情。";
}

function aiLabel(run, ai) {
  if (run.selectionMode === "rules_fallback") return "规则兜底";
  if (run.selectionMode === "ai_semifinal_rules_final") return String(run.error || "").includes("Top0") ? "AI初筛+规则观察" : "AI初筛+规则终选";
  if (!ai.configured) return "未配置";
  return `${ai.provider || "AI"} ${ai.model || ""}`.trim();
}

function runRoleLabel(value) {
  return {
    official: "正式",
    research: "研究",
    legacy: "历史",
    invalid: "异常"
  }[value] || value || "--";
}

function runProfile(run) {
  return (run?.market || {}).runProfile || (run?.request || {}).runProfile || {};
}

function runProfileLabel(run) {
  const profile = runProfile(run);
  const key = profile.key || "";
  if (profile.label) return profile.label;
  const poolSize = Number((run?.request || {}).poolSize || profile.poolSize || run?.poolSize || state.poolSize || 0);
  if (key === "production_500" || poolSize >= DEFAULT_T1_POOL_SIZE) return "生产模式";
  if (key === "standard_300" || poolSize >= OFFICIAL_MIN_POOL_SIZE) return "标准模式";
  return "快速研究";
}

function isQuickResearchRun(run) {
  if (!run) return false;
  const profile = runProfile(run);
  if (profile.officialEligible === false || profile.key === "quick_research") return true;
  const poolSize = Number((run.request || {}).poolSize || profile.poolSize || run.poolSize || 0);
  return poolSize > 0 && poolSize < OFFICIAL_MIN_POOL_SIZE;
}

function aiDetail(run, ai) {
  if (run.selectionMode === "rules_fallback") return run.error || "AI 未返回有效 T+1 JSON，已用本地规则排序兜底。";
  if (run.selectionMode === "ai_semifinal_rules_final") return run.error || "AI 终选未采纳，已在 AI 半决赛池内规则观察。";
  if (!ai.configured) return ai.fallback || "AI 未配置时会直接使用规则兜底。";
  if (run.currentStage === "final") return "正在从半决赛池做最终 Top3 判断，终选会参考历史复盘摘要。";
  if (String(run.currentStage || "").startsWith("batch:")) return "正在分批初筛候选池。";
  return "配置可用，候选池完成后进入 DeepSeek 批次初筛。";
}

function criticLabel(status, critic) {
  if (critic.applied) return critic.replacements?.length ? `替换 ${critic.replacements.length} 只` : "已审查";
  if (status.enabled) return "待介入";
  return "未介入";
}

function criticTone(status, critic) {
  if (critic.replacements?.length) return "warn";
  if (critic.applied || status.enabled) return "good";
  if (status.mode === "off") return "neutral";
  return "warn";
}

function criticDetail(status, critic) {
  const issueCount = (critic.issues || []).length;
  const replacementCount = (critic.replacements || []).length;
  if (critic.reason) return `${critic.reason} 问题 ${issueCount} · 替换 ${replacementCount}`;
  return `${criticModeLabel(status.mode)} · ${status.reason || "--"} · 问题 ${issueCount} · 替换 ${replacementCount}`;
}

function marketContextLabel(context = {}) {
  return context.label || marketStateLabel(context.state) || "--";
}

function marketContextTone(context = {}) {
  const state = context.state || "";
  if (["panic", "heavy_down"].includes(state)) return "bad";
  if (["mild_down", "euphoric", "unknown"].includes(state)) return "warn";
  if (["trend_up", "mild_up"].includes(state)) return "good";
  return "neutral";
}

function marketContextDetail(context = {}, policy = {}) {
  const reason = (context.reasons || []).join("；") || policy.reason || "";
  const limit = policy.maxPickCount ?? context.maxPickCount;
  return `${riskLevelLabel(context.riskLevel)} · 最多 ${formatInteger(limit)} 只${reason ? ` · ${reason}` : ""}`;
}

function formatIndexList(items, limit = 3, emptyText = "暂无数据") {
  if (!Array.isArray(items) || !items.length) return emptyText;
  return items
    .slice(0, limit)
    .map((item) => `${item.name || "--"} ${formatPct(item.change, true)}`)
    .join(" · ");
}

function marketIndexView(market = {}, metrics = {}) {
  const rawIndices = Array.isArray(market.indices) ? market.indices : [];
  const rawEqualWeighted = Array.isArray(market.equalWeightedIndices) ? market.equalWeightedIndices : [];
  const legacyEqualWeighted = !rawEqualWeighted.length && isLegacyEqualWeightedIndices(rawIndices);
  const equalWeightedIndices = rawEqualWeighted.length
    ? rawEqualWeighted
    : legacyEqualWeighted
      ? rawIndices.map((item) => ({
        ...item,
        name: item.name === "沪深主板" ? "主板等权" : "创业板等权"
      }))
      : [];
  return {
    realIndices: legacyEqualWeighted ? [] : rawIndices,
    equalWeightedIndices,
    mainIndexChangePct: legacyEqualWeighted ? null : metrics.mainIndexChangePct,
    lossEffectChangePct: metrics.lossEffectChangePct ?? weakestChange(equalWeightedIndices),
    warning: legacyEqualWeighted ? "旧 run 的沪深主板/创业板为个股等权口径，已按小票亏钱效应展示。" : ""
  };
}

function isLegacyEqualWeightedIndices(items) {
  if (!Array.isArray(items) || !items.length) return false;
  return items.every((item) => ["沪深主板", "创业板"].includes(String(item?.name || "")));
}

function weakestChange(items) {
  if (!Array.isArray(items) || !items.length) return null;
  const values = items.map((item) => Number(item?.change)).filter((value) => Number.isFinite(value));
  if (!values.length) return null;
  return Math.min(...values);
}

function marketLossTone(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "warn";
  if (number < -2) return "bad";
  if (number < 0) return "warn";
  return "good";
}

function marketStateLabel(value) {
  return {
    trend_up: "趋势上涨",
    mild_up: "温和上涨",
    mixed: "震荡分化",
    euphoric: "情绪高潮",
    mild_down: "温和下跌",
    heavy_down: "放量大跌",
    panic: "恐慌退潮",
    unknown: "数据不足"
  }[value] || value || "";
}

function riskLevelLabel(value) {
  return {
    extreme: "极高",
    high: "高",
    elevated: "偏高",
    medium_high: "中高",
    medium: "中等",
    low: "低",
    unknown: "未知"
  }[value] || value || "--";
}

function sourceLabel(run) {
  const source = ((run.market || {}).sourceStatus || {});
  return source.label || sourceName(run.actualSource || run.requestedSource);
}

function riskBadgeHtml(item, raw = null) {
  const source = item.riskProfile || (raw || {}).riskProfile || {};
  const key = source.key || item.riskProfileKey || (raw || {}).riskProfileKey || "";
  const label = source.label || item.riskProfileLabel || (raw || {}).riskProfileLabel || riskProfileLabel(key);
  const summary = source.summary || "";
  const tone = source.tone || riskProfileTone(key);
  if (!label) return "";
  return `
    <span class="risk-badge ${escapeHtml(tone)}" title="${escapeHtml(summary)}">
      ${escapeHtml(label)}
    </span>
  `;
}

function dataQualityBadgeHtml(item) {
  const quality = item?.dataQuality || {};
  const grade = String(quality.grade || "").toUpperCase();
  if (!grade) return "";
  const tone = ["A", "B"].includes(grade) ? "good" : grade === "C" ? "warn" : "bad";
  const sectorQuality = quality.sectorQuality || {};
  const parts = [
    `报价${dataQualityLabel(quality.quoteQuality)}`,
    `板块${sectorEvidenceStatusLabel(sectorQuality.status) || dataQualityLabel(sectorQuality.grade)}`,
    `市场${dataQualityLabel(quality.marketQuality)}`,
    `决策${decisionEligibilityLabel(quality.decisionEligibility)}`
  ];
  const reasons = [...(quality.reasons || []), ...parts].join("；");
  return `
    <span class="quality-badge ${tone}" title="${escapeHtml(reasons || quality.klineSource || "")}">
      ${escapeHtml(dataQualityBadgeLabel(grade))}
    </span>
  `;
}

function sectorEvidenceBadgeHtml(item) {
  const status = item?.sectorEvidenceStatus || item?.sectorEvidence?.status || item?.dataQuality?.sectorQuality?.status || "";
  if (!status) return "";
  const layers = item?.sectorEvidence?.evidenceLayers || item?.dataQuality?.sectorQuality?.layers || {};
  const identity = layerStatusLabel(layers.identity?.status);
  const snapshot = layerStatusLabel(layers.sectorSnapshot?.status);
  const rank = layerStatusLabel(layers.constituentRank?.status);
  const sourceLabel = sectorSnapshotSourceLabel(item);
  const tone = layers.sectorSnapshot?.status === "available" ? "good" : layers.identity?.status === "available" || status === "partial" || status === "stale" ? "warn" : "bad";
  const title = [
    `行业身份：${identity}`,
    `板块强度：${snapshot}`,
    `证据来源：${sourceLabel}`,
    `成分股排名：${rank}`,
    ...(sectorSnapshotSourceKind(item) === "candidate_pool_aggregate" ? ["自建聚合只反映本次候选池样本，不含外部资金流，不代表全市场板块统计。"] : []),
    ...(item?.sectorEvidence?.reasons || item?.dataQuality?.sectorQuality?.reasons || [])
  ].join("；");
  return `
    <span class="quality-badge ${tone}" title="${escapeHtml(title)}">
      板块 · ${escapeHtml(sourceLabel)} / 身份${escapeHtml(identity)} / 强度${escapeHtml(snapshot)} / 排名${escapeHtml(rank)}
    </span>
  `;
}

function sectorStateBadgeHtml(item) {
  const dimensions = item?.t1Dimensions || {};
  const state = item?.sectorStateShadow || item?.sectorEvidence?.sectorStateShadow || dimensions.sectorStateShadow || item?.dataQuality?.sectorQuality?.sectorStateShadow || "";
  if (!state) return "";
  const label = item?.sectorStateLabel || item?.sectorEvidence?.sectorStateLabel || dimensions.sectorStateLabel || sectorStateLabel(state);
  const confidence = item?.sectorStateConfidence ?? item?.sectorEvidence?.sectorStateConfidence ?? dimensions.sectorStateConfidence;
  const risk = item?.sectorRiskScore ?? item?.sectorEvidence?.sectorRiskScore ?? dimensions.sectorRiskScore;
  const reasons = item?.sectorStateReasons || item?.sectorEvidence?.sectorStateReasons || [];
  const affectsScore = Boolean(dimensions.sectorStateAffectsScore);
  const title = [
    affectsScore ? "阶段2已生效，该板块状态已对普通后排候选有限降权" : "当前为影子状态，不影响本次正式 Top3",
    `置信度 ${formatConfidence(confidence)}`,
    `板块风险 ${formatNumber(risk)}`,
    ...(reasons || []).slice(0, 2)
  ].filter(Boolean).join("；");
  return `
    <span class="quality-badge ${sectorStateTone(state)}" title="${escapeHtml(title)}">
      板块状态 · ${escapeHtml(label)}${affectsScore ? " · 已介入" : ""}
    </span>
  `;
}

function sectorStateTone(state) {
  if (["retreat", "distribution", "overheated"].includes(state)) return "bad";
  if (["weakening", "data_insufficient", "oversold_repair"].includes(state)) return "warn";
  if (["rotation_in", "strong"].includes(state)) return "good";
  return "neutral";
}

function aiEvidenceValidationHtml(item) {
  const validation = item?.aiEvidenceValidation || {};
  const status = validation.status || "";
  if (!status) return "";
  const tone = status === "passed" || status === "partial_passed" ? "good" : status === "not_ai_fallback" || status === "unknown" ? "warn" : "bad";
  const label = {
    passed: "AI证据通过",
    partial_passed: "AI部分通过",
    failed: "AI证据失败",
    not_ai_fallback: "规则兜底",
    unknown: "证据未标记"
  }[status] || status;
  return `
    <span class="quality-badge ${tone}" title="${escapeHtml((validation.errors || []).join("；"))}">
      ${escapeHtml(label)}
    </span>
  `;
}

function t1ScoreBreakdownHtml(item) {
  const dimensions = item?.t1Dimensions || {};
  if (!Object.keys(dimensions).length) return "";
  const premium = dimensions.premiumPotentialScore ?? dimensions.tomorrowPremium;
  const riskItems = (dimensions.overnightRiskBreakdown || []).slice(0, 4).map((row) => `${row.label}+${formatNumber(row.impact)}`);
  const shadowRisk = dimensions.sectorStateDowngrade
    ? t1MetricHtml("板块降权", `+${formatNumber(dimensions.sectorStateDowngrade)}`, "阶段2开启后，退潮、分歧或过热板块里的普通后排候选会被有限降权；逆势核心会保留例外。")
    : dimensions.overnightRiskWithSectorShadow !== undefined
      ? t1MetricHtml("影子风险", formatNumber(dimensions.overnightRiskWithSectorShadow), "观察用风险：在正式隔夜风险基础上，叠加板块状态影子风险。当前不影响正式 Top3，只用于后续验证。")
      : "";
  return `
    <div class="t1-mini-grid">
      ${t1MetricHtml("溢价潜力", formatNumber(premium), "次日开盘后是否有溢价机会的综合评分，主要看相对强度、涨幅位置、成交活跃度、日内结构、板块支持和隔夜风险。它不是上涨概率，也不是收益预测。")}
      ${t1MetricHtml("隔夜风险", formatNumber(dimensions.overnightRisk), "今天尾盘买入后持有到明天的风险评分，越低越好。会考虑市场状态、跌停扩散、板块证据缺失、可交易性、高位放量和数据完整度。")}
      ${shadowRisk}
      ${t1MetricHtml("板块强度", formatNumber(dimensions.sectorStrength), "行业/概念对这只股票的支持评分。外部全市场板块证据优先；如果只有候选池聚合，会限制分数并提示不能当作全市场排名。")}
      ${t1MetricHtml("成交活跃度", formatNumber(dimensions.fundStrength), "个股成交活跃度评分，主要看成交额强度、量比、涨幅和成交规模。它不是主力净流入，也不等于真实机构资金。")}
    </div>
    ${riskItems.length ? `<div class="t1-risk-breakdown">风险拆分：${escapeHtml(riskItems.join("；"))}</div>` : ""}
  `;
}

function t1MetricHtml(label, value, helpText) {
  const help = escapeHtml(helpText);
  return `
    <span class="t1-metric">
      <span class="t1-metric-label">
        ${escapeHtml(label)}
        <span class="metric-help term-help" title="${help}" data-tooltip="${help}" aria-label="${help}" tabindex="0">?</span>
      </span>
      <strong>${escapeHtml(value ?? "--")}</strong>
    </span>
  `;
}

function sectorSemanticsHtml(item) {
  const listing = item?.listingBoard || "";
  const industry = item?.industrySector || "";
  const concepts = item?.conceptSectors || [];
  if (!listing && !industry && !concepts.length) return "";
  const boardCount = item?.marketBoardLimitUpCount;
  const layers = item?.sectorEvidence?.evidenceLayers || {};
  const rankAvailable = layers.constituentRank?.status === "available";
  const stockRank = item?.stockRankInIndustry;
  const state = item?.sectorStateShadow || item?.sectorEvidence?.sectorStateShadow || "";
  const stateSource = item?.primarySectorStateSource || item?.industrySector || "";
  const industryEvidence = preferredSectorDisplayMatch(item, "industry", industry);
  const conceptEvidence = preferredConceptDisplayMatches(item, concepts).slice(0, 3);
  const industryLine = sectorEvidenceLine({
    prefix: "所属行业",
    name: industry || "--",
    evidence: industryEvidence,
    fallbackCount: item?.industryLimitUpCount,
    fallbackSampleCount: item?.industrySectorSampleCount,
    fallbackRank: item?.industryRank
  });
  const conceptLine = conceptEvidence.length
    ? `概念参考：${conceptEvidence.map((entry) => sectorEvidenceBrief(entry)).join("；")}`
    : `概念：${concepts.length ? concepts.slice(0, 3).join("、") : "--"}`;
  const sourceTip = sectorDisplaySourceTip(industryEvidence, conceptEvidence);
  return `
    <div class="sector-semantics">
      <span>上市板块：${escapeHtml(listing || "--")}${boardCount !== undefined && boardCount !== null ? ` · 涨停${formatInteger(boardCount)}` : ""}</span>
      <span title="${escapeHtml(sourceTip)}">${escapeHtml(industryLine)}</span>
      <span>行业内位置：${rankAvailable && stockRank ? `第${formatInteger(stockRank)}` : "未确认"}</span>
      <span>板块状态影子：${escapeHtml(state ? `${stateSource || "--"} ${sectorStateLabel(state)}` : "证据不足")}</span>
      <span title="${escapeHtml(sourceTip)}">${escapeHtml(conceptLine)}</span>
    </div>
  `;
}

function preferredSectorDisplayMatch(item = {}, sectorType = "", sectorName = "") {
  const matches = Array.isArray(item?.sectorEvidence?.matchedSectors) ? item.sectorEvidence.matchedSectors : [];
  const sameType = matches.filter((entry) => String(entry?.sectorType || "") === sectorType);
  const sameName = sameType.filter((entry) => !sectorName || String(entry?.sectorName || "") === sectorName);
  const candidates = sameName.length ? sameName : sameType;
  return candidates.sort((a, b) => sectorDisplayPriority(a) - sectorDisplayPriority(b))[0] || null;
}

function preferredConceptDisplayMatches(item = {}, concepts = []) {
  const names = new Set((concepts || []).map((name) => String(name || "")).filter(Boolean));
  const matches = Array.isArray(item?.sectorEvidence?.matchedSectors) ? item.sectorEvidence.matchedSectors : [];
  const conceptMatches = matches.filter((entry) => String(entry?.sectorType || "") === "concept" && (!names.size || names.has(String(entry?.sectorName || ""))));
  const byName = new Map();
  conceptMatches
    .sort((a, b) => sectorDisplayPriority(a) - sectorDisplayPriority(b))
    .forEach((entry) => {
      const name = String(entry?.sectorName || "");
      if (name && !byName.has(name)) byName.set(name, entry);
    });
  return Array.from(byName.values()).sort((a, b) => {
    const externalDelta = Number(isExternalSectorMatch(b)) - Number(isExternalSectorMatch(a));
    if (externalDelta) return externalDelta;
    return Number(a?.finalRank || a?.industryRank || 9999) - Number(b?.finalRank || b?.industryRank || 9999);
  });
}

function sectorDisplayPriority(entry = {}) {
  if (isExternalSectorMatch(entry)) return 0;
  if (isCandidatePoolSectorMatch(entry)) return 1;
  return 2;
}

function isExternalSectorMatch(entry = {}) {
  const scope = String(entry?.snapshotScope || "");
  const source = String(entry?.source || "");
  return scope === "full_market" || (!!source && !source.includes("t1_internal_candidate_aggregate"));
}

function isCandidatePoolSectorMatch(entry = {}) {
  const scope = String(entry?.snapshotScope || "");
  const source = String(entry?.source || "");
  return scope === "candidate_pool" || source.includes("t1_internal_candidate_aggregate");
}

function sectorEvidenceLine({ prefix, name, evidence, fallbackCount, fallbackSampleCount, fallbackRank }) {
  if (!evidence) return `${prefix}：${name} · 暂无当日强度证据`;
  const sourceLabel = isExternalSectorMatch(evidence) ? "全市场参考" : isCandidatePoolSectorMatch(evidence) ? "候选池参考" : "来源待确认";
  const count = evidence.limitUpCount ?? fallbackCount;
  const sampleCount = evidence.sampleCount ?? fallbackSampleCount;
  const rank = evidence.finalRank ?? evidence.industryRank ?? fallbackRank;
  const pieces = [`${prefix}：${name}`, sourceLabel];
  if (count !== undefined && count !== null && Number(count) > 0) pieces.push(`涨停${formatInteger(count)}`);
  if (sampleCount !== undefined && sampleCount !== null && Number(sampleCount) > 0) {
    pieces.push(`${isExternalSectorMatch(evidence) ? "覆盖" : "样本"}${formatInteger(sampleCount)}`);
  }
  if (rank !== undefined && rank !== null && Number(rank) > 0) {
    pieces.push(`${isExternalSectorMatch(evidence) ? "排名" : "池内排序"}${formatInteger(rank)}`);
  }
  return pieces.join(" · ");
}

function sectorEvidenceBrief(evidence = {}) {
  const name = evidence.sectorName || "--";
  return sectorEvidenceLine({ prefix: "", name, evidence }).replace(/^：/, "");
}

function sectorDisplaySourceTip(industryEvidence, conceptEvidence = []) {
  const hasCandidatePool = [industryEvidence, ...conceptEvidence].some((entry) => entry && isCandidatePoolSectorMatch(entry));
  const hasExternal = [industryEvidence, ...conceptEvidence].some((entry) => entry && isExternalSectorMatch(entry));
  const tips = [];
  if (hasExternal) tips.push("外部全市场参考来自当日板块快照，可用于观察板块强弱。");
  if (hasCandidatePool) tips.push("候选池参考只统计本次 T+1 候选池内样本，不代表全市场行业排名或主力资金。");
  if (!tips.length) tips.push("当前只确认行业/概念身份，未拿到可用的当日板块强度。");
  return tips.join(" ");
}

function evidenceRefsHtml(pick) {
  const support = pick.supportingEvidenceRefs || [];
  const counter = pick.counterEvidenceRefs || [];
  if (!support.length && !counter.length) return "";
  const refMap = new Map((pick.evidenceRefs || []).map((item) => [item.id, item]));
  const supportItems = support.map((id) => evidenceReasonItem(id, refMap, pick, "support")).filter(Boolean);
  const counterItems = counter.map((id) => evidenceReasonItem(id, refMap, pick, "counter")).filter(Boolean);
  return `
    <div>
      <dt>参考依据</dt>
      <dd>
        ${supportItems.length ? `<div class="evidence-reason-line"><strong>看好理由</strong>${supportItems.join("")}</div>` : ""}
        ${counterItems.length ? `<div class="evidence-reason-line"><strong>风险提醒</strong>${counterItems.join("")}</div>` : ""}
      </dd>
    </div>
  `;
}

function evidenceReasonItem(id, refMap, pick, mode) {
  const ref = refMap.get(id) || {};
  const label = evidenceReasonLabel(id);
  const text = plainEvidenceSummary(id, ref, pick, mode);
  if (!text) return "";
  return `<span class="evidence-reason ${mode === "counter" ? "counter" : ""}" title="${escapeHtml(ref.summary || text)}">${escapeHtml(label)}：${explainTradingTermsHtml(text)}</span>`;
}

function evidenceReasonLabel(id) {
  return {
    Q1: "价格成交",
    M1: "市场环境",
    S1: "板块情况",
    S2: "板块状态",
    R1: "隔夜风险",
    T1: "尾盘承接",
    G1: "低开风险"
  }[id] || id;
}

function plainEvidenceSummary(id, ref = {}, pick = {}, mode = "support") {
  const summary = String(ref.summary || "").trim();
  if (id === "Q1") {
    const change = formatPct(pick.change, true);
    const volumeRatio = formatRatio(pick.volumeRatio);
    const amount = formatMoneyYi(Number(pick.amount || 0) || Number(pick.turnover || 0) * 100000000);
    return `涨幅${change}，量比${volumeRatio}，成交额${amount}，说明尾盘有交易活跃度。`;
  }
  if (id === "M1") return pick.marketFit || cleanupEvidenceSummary(summary) || "市场环境对隔夜交易有一定参考。";
  if (id === "S1") return pick.sectorFit || plainSectorEvidenceFromPick(pick) || cleanupEvidenceSummary(summary);
  if (id === "S2") {
    const label = pick.sectorStateLabel || pick.sectorEvidence?.sectorStateLabel || "";
    const risk = pick.sectorRiskScore ?? pick.sectorEvidence?.sectorRiskScore;
    return label ? `${label}，板块状态仍是观察项${risk !== undefined ? `，风险参考 ${formatNumber(risk)}` : ""}。` : cleanupEvidenceSummary(summary);
  }
  if (id === "R1") return pick.risk || cleanupEvidenceSummary(summary) || (mode === "counter" ? "隔夜风险需要控制。" : "风险可控但仍需按计划处理。");
  if (id === "T1") return cleanupEvidenceSummary(summary) || "尾盘承接表现作为观察依据。";
  if (id === "G1") return cleanupEvidenceSummary(summary) || "低开风险作为观察依据。";
  return cleanupEvidenceSummary(summary);
}

function plainSectorEvidenceFromPick(pick = {}) {
  const industry = pick.industrySector || "";
  const concepts = Array.isArray(pick.conceptSectors) ? pick.conceptSectors.slice(0, 3).join("、") : "";
  if (!industry && !concepts) return "";
  const source = sectorSnapshotSourceLabel(pick);
  return `${industry ? `所属行业 ${industry}` : ""}${concepts ? `，相关概念 ${concepts}` : ""}；证据来源：${source}。`;
}

function cleanupEvidenceSummary(value) {
  const text = String(value || "")
    .replace(/；允许：.*$/u, "")
    .replace(/，涨停数需按 allowedClaims 区分/u, "")
    .replace(/allowedClaims/gu, "证据规则")
    .trim();
  if (!text) return "";
  return text.length > 80 ? `${text.slice(0, 80)}...` : text;
}

const TRADING_TERM_HELP = {
  VWAP: "VWAP 是成交量加权平均价，也可以理解成当天市场真实成交的平均成本线。T+1 里用它看尾盘价格有没有被资金接住；跌破后通常说明承接变弱。",
  均价线: "均价线是当天成交平均成本附近的位置。尾盘价格站在均价线上方，说明买盘承接相对更好；明显跌破则要警惕诱多或跳水。",
  量比: "量比表示当前成交量相对过去平均水平是否放大。量比高说明交易活跃，但不等于一定上涨，也不等于主力净流入。",
  放量: "放量表示成交明显变大。上涨放量可能代表资金关注，冲高不涨或下跌放量则可能是兑现压力。",
  承接: "承接指价格回落时有没有买盘接住。T+1 更看重尾盘承接，因为它关系到隔夜后明天是否还有溢价。"
};

function explainTradingTermsHtml(value) {
  const text = String(value || "--");
  const terms = Object.keys(TRADING_TERM_HELP).sort((a, b) => b.length - a.length);
  if (!terms.length) return escapeHtml(text);
  const pattern = new RegExp(terms.map(escapeRegExp).join("|"), "g");
  let html = "";
  let lastIndex = 0;
  for (const match of text.matchAll(pattern)) {
    const term = match[0];
    const index = match.index ?? 0;
    html += escapeHtml(text.slice(lastIndex, index));
    html += `${escapeHtml(term)}${termHelpHtml(TRADING_TERM_HELP[term])}`;
    lastIndex = index + term.length;
  }
  html += escapeHtml(text.slice(lastIndex));
  return html;
}

function termHelpHtml(helpText) {
  const text = escapeHtml(helpText);
  return `<span class="metric-help term-help" title="${text}" data-tooltip="${text}" aria-label="${text}" tabindex="0">?</span>`;
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function sectorDisplayText(item = {}) {
  const industry = item.industrySector || "";
  const listing = item.listingBoard || item.sector || "";
  const status = item.sectorEvidenceStatus || item.sectorEvidence?.status || "";
  if (industry) return `${industry} · ${listing || "--"} · ${sectorEvidenceStatusLabel(status)}`;
  return `${listing || "--"} · ${sectorEvidenceStatusLabel(status)}`;
}

function sectorEvidenceStatusLabel(status) {
  return {
    available: "可用",
    partial: "部分",
    stale: "过期",
    unavailable: "不可用"
  }[status] || status || "--";
}

function sectorSnapshotSourceKind(item = {}) {
  const value = item?.sectorSnapshotSourceKind
    || item?.sectorEvidence?.sectorSnapshotSourceKind
    || item?.dataQuality?.sectorQuality?.sectorSnapshotSourceKind
    || "";
  return String(value || "");
}

function sectorSnapshotSourceLabel(item = {}) {
  const kind = sectorSnapshotSourceKind(item);
  if (kind === "candidate_pool_aggregate") return "自建候选池聚合";
  if (kind === "external_snapshot") return "外部板块快照";
  if (kind === "mixed") return "外部快照+自建聚合";
  const layers = item?.sectorEvidence?.evidenceLayers || item?.dataQuality?.sectorQuality?.layers || {};
  if (layers.identity?.status === "available") return "仅行业身份";
  return "证据缺失";
}

function evidenceSourceLabel(evidence = {}) {
  const mode = evidence.sectorSnapshotMode || "";
  if (mode === "candidate_pool") return "自建候选池聚合";
  if (mode === "mixed") return "外部快照+自建聚合";
  if (mode === "external") return "外部板块快照";
  return evidence.sectorSourceLabel || "板块来源待确认";
}

function layerStatusLabel(status) {
  return {
    available: "可用",
    partial: "部分",
    stale: "过期",
    unavailable: "缺失"
  }[status] || status || "缺失";
}

function aiEvidenceValidationLabel(status) {
  return {
    passed: "已通过",
    partial_passed: "部分通过",
    retrying: "重试中",
    failed: "未通过",
    not_ai_fallback: "规则兜底",
    unknown: "未标记"
  }[status] || status || "--";
}

function tradeDateTone(status = {}) {
  const eligibility = status.eligibility || "";
  if (eligibility === "official_blocked" || status.isTradingDay === false) return "bad";
  if (eligibility === "research_only" || status.status === "mock_research_only") return "warn";
  return "good";
}

function tradeDateStatusLabel(status = {}) {
  if (!Object.keys(status || {}).length) return "待校验";
  if (status.eligibility === "official_blocked" || status.isTradingDay === false) return "不生成正式推荐";
  if (status.eligibility === "research_only") return "仅研究参考";
  return "可生成正式推荐";
}

function sectorStateSummaryTone(summary = {}) {
  const counts = summary.counts || {};
  if (!summary.enabled) return "warn";
  if (Number(counts.retreat || 0) + Number(counts.distribution || 0) > 0) return "warn";
  if (Number(counts.rotation_in || 0) + Number(counts.strong || 0) > 0) return "good";
  return "neutral";
}

function sectorStateSummaryHeadline(summary = {}) {
  if (!summary.enabled) return "未启用";
  const highlights = summary.highlights || [];
  if (!highlights.length) return "暂无明显状态";
  return highlights.slice(0, 2).map((item) => `${item.sectorName || "--"} ${sectorStateLabel(item.sectorState)}`).join(" / ");
}

function sectorStateCountsText(summary = {}) {
  const counts = summary.counts || {};
  const parts = [
    ["rotation_in", "轮入"],
    ["strong", "强势"],
    ["overheated", "过热"],
    ["weakening", "转弱"],
    ["distribution", "派发"],
    ["retreat", "退潮"],
    ["data_insufficient", "证据不足"]
  ].map(([key, label]) => `${label}${formatInteger(counts[key] || 0)}`);
  return parts.join(" · ");
}

function sectorStateLabel(value) {
  return {
    rotation_in: "轮入观察",
    strong: "强势延续",
    overheated: "高热兑现",
    neutral: "中性分化",
    weakening: "转弱",
    distribution: "派发分歧",
    retreat: "退潮",
    oversold_repair: "超跌修复",
    data_insufficient: "证据不足"
  }[value] || value || "证据不足";
}

function styleRegimeTone(regime = {}) {
  const state = regime.state || "";
  if (state === "broad_retreat" || state === "overheated_realization") return "warn";
  if (state === "rotation_or_strength") return "good";
  if (state === "data_insufficient") return "bad";
  return "neutral";
}

function styleRegimeSummaryText(regime = {}) {
  if (!regime.enabled) return "未启用";
  const reasons = regime.reasons || [];
  if (reasons.length) return reasons[0];
  const counts = regime.counts || {};
  return `轮入/强势 ${formatInteger((counts.rotation_in || 0) + (counts.strong || 0))} · 退潮/派发 ${formatInteger((counts.retreat || 0) + (counts.distribution || 0))}`;
}

function evidenceTone(evidence = {}) {
  const quality = String(evidence.qualityStatus || "").toUpperCase();
  if (quality === "A" || quality === "B") return "good";
  if (quality === "C" || evidence.status === "running") return "warn";
  if (quality === "D" || quality === "EMPTY" || evidence.status === "failed") return "bad";
  return evidence.evidenceRunId ? "neutral" : "warn";
}

function evidenceQualityLabel(evidence = {}) {
  if (!evidence.evidenceRunId) return "证据未生成";
  if (evidence.status === "running") return "正在生成";
  if (evidence.status === "failed") return "证据生成失败";
  return {
    A: "证据完整",
    B: "证据可用",
    C: "证据有限，仅研究参考",
    D: "证据不足",
    EMPTY: "证据不足"
  }[String(evidence.qualityStatus || "").toUpperCase()] || "证据状态待确认";
}

function evidenceDetail(evidence = {}) {
  if (!evidence.evidenceRunId) return "尚未生成 T+1 独立证据批次";
  const coverage = evidence.evidenceCoverage || {};
  const parts = [
    evidenceSourceLabel(evidence),
    `板块${formatInteger(evidence.sectorCount)}`,
    `成分股${formatInteger(evidence.constituentCount)}`,
    `映射${formatInteger(evidence.stockMapCount)}`,
    `分钟线${formatInteger(evidence.minuteCount)}`
  ];
  const coverageText = coverageDetail(coverage);
  if (coverageText) parts.push(coverageText);
  const missing = (evidence.missing || []).slice(0, 2).join("；");
  return `${parts.join(" · ")}${missing ? ` · 缺失：${missing}` : ""}`;
}

function layerAvailableCount(coverage = {}, key) {
  return Number((coverage[key] || {}).available || 0);
}

function layerTotalCount(coverage = {}, key) {
  const bucket = coverage[key] || {};
  const total = Number(coverage.total || 0);
  if (total) return total;
  return Number(bucket.available || 0) + Number(bucket.stale || 0) + Number(bucket.unavailable || 0);
}

function coverageHeadline(coverage = {}) {
  const total = layerTotalCount(coverage, "identity");
  if (!total) return "覆盖待确认";
  return `身份 ${formatInteger(layerAvailableCount(coverage, "identity"))} / ${formatInteger(total)}`;
}

function coverageDetail(coverage = {}) {
  const total = layerTotalCount(coverage, "identity");
  if (!total) return "";
  return [
    `行业身份 ${formatInteger(layerAvailableCount(coverage, "identity"))}/${formatInteger(total)}`,
    `板块强度 ${formatInteger(layerAvailableCount(coverage, "sectorSnapshot"))}/${formatInteger(total)}`,
    `成分股排名 ${formatInteger(layerAvailableCount(coverage, "constituentRank"))}/${formatInteger(total)}`
  ].join(" · ");
}

function coverageTone(coverage = {}) {
  const total = layerTotalCount(coverage, "identity");
  if (!total) return "warn";
  const identityRatio = layerAvailableCount(coverage, "identity") / total;
  const snapshotRatio = layerAvailableCount(coverage, "sectorSnapshot") / total;
  if (identityRatio >= 0.9 && snapshotRatio >= 0.55) return "good";
  if (identityRatio >= 0.55) return "warn";
  return "bad";
}

function preheatSummary(payload = {}) {
  const evidence = payload.evidence || {};
  const coverage = evidence.evidenceCoverage || (payload.dataQualityStats || {}).sectorLayerCounts || {};
  const warnings = (payload.warnings || []).slice(0, 2).join("；");
  const reuse = payload.reusedExisting || evidence.reusedExisting ? "已复用今天冻结证据" : "已生成今天冻结证据";
  const reuseReason = payload.reuseReason || evidence.reuseReason || "";
  return `T+1 证据预热完成：${reuse}，候选 ${formatInteger(payload.poolCount)}，可交易 ${formatInteger(payload.selectionPoolCount)}，${coverageDetail(coverage) || "覆盖待确认"}，耗时 ${formatDuration(payload.elapsedSeconds)}${reuseReason ? `；${reuseReason}` : ""}${warnings ? `；${warnings}` : ""}`;
}

function aiParticipationTone(participation = {}, run = {}, ai = {}) {
  const mode = participation.mode || "";
  if (mode === "full_ai") return "good";
  if (mode === "limited_ai_partial_sector" || mode === "limited_ai_no_sector") return "warn";
  if (mode === "research_ai" || mode === "ai_unavailable") return "bad";
  if (run.selectionMode === "rules_fallback" || !ai.configured) return "warn";
  if (run.selectionMode === "ai_semifinal_rules_final") return "warn";
  if (run.selectionMode === "ai_failed") return "bad";
  return "neutral";
}

function decisionEligibilityLabel(value) {
  return {
    strict: "严格",
    partial: "部分",
    restricted: "受限",
    research_only: "研究"
  }[value] || value || "--";
}

function dataQualityLabel(grade) {
  const key = String(grade || "").toUpperCase();
  return {
    A: "完整",
    B: "可用",
    C: "有限",
    D: "不足",
    E: "异常"
  }[key] || "--";
}

function dataQualityBadgeLabel(grade) {
  const key = String(grade || "").toUpperCase();
  return {
    A: "数据完整",
    B: "数据可用",
    C: "数据有限",
    D: "数据不足",
    E: "数据异常"
  }[key] || "数据待确认";
}

function minuteKlineTone(status = {}) {
  const requested = Number(status.requested || 0);
  const ok = Number(status.ok || status.attached || 0);
  if (!requested) return "neutral";
  if (ok <= 0) return "bad";
  if (ok < requested * 0.8) return "warn";
  return "good";
}

function formatKlineSourceCounts(counts = {}) {
  const entries = Object.entries(counts || {}).filter(([, value]) => Number(value || 0) > 0);
  if (!entries.length) return "";
  return entries
    .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
    .map(([source, count]) => `${sourceName(source)} ${formatInteger(count)}`)
    .join(" · ");
}

function minuteProviderDetails(status = {}) {
  const providers = Array.isArray(status.providers) ? status.providers : [];
  return providers.map((provider) => {
    const text = `${sourceName(provider.source)}：${formatInteger(provider.ok || provider.accepted || 0)}/${formatInteger(provider.requested || 0)}`;
    return provider.warning ? `${text}，${provider.warning}` : text;
  }).join("；");
}

function metricCard(label, value, detail) {
  return `
    <div class="metric-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value ?? "--")}</strong>
      <small>${escapeHtml(detail || "--")}</small>
    </div>
  `;
}

function riskProfileLabel(key) {
  return {
    aggressive: "进攻型",
    balanced: "均衡型",
    defensive_watch: "防守观察型",
    avoid_overnight: "不建议隔夜"
  }[key] || "";
}

function riskProfileTone(key) {
  return {
    aggressive: "warn",
    balanced: "good",
    defensive_watch: "neutral",
    avoid_overnight: "bad"
  }[key] || "neutral";
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
    empty: "暂无 run，生成推荐后再验证。",
    not_ready: "当前 run 尚未完成，完成后才能验证。",
    pending_next_trade_day: "推荐日期还没有下个交易日行情，暂时等待。",
    verifiable: "当前 run 可以手动刷新次日验证。",
    provisional: "已取得部分验证点，但收盘、高低点或规则复盘尚未到最终确认时间，不进入学习。",
    verified: "已写入次日验证结果。",
    legacy: "旧口径历史数据仅保留展示和问题排查，不进入严格复盘或学习。",
    invalid: "验证结果存在价格口径、行情源或数据质量异常，不能进入学习。",
    data_insufficient: "已经尝试验证，但行情源暂未提供足够次日数据。",
    zero_pick: zeroPickReason(run)
  }[value] || "等待验证状态。";
}

function validationStatusLabel(value) {
  return {
    pending: "等待",
    provisional: "临时",
    final: "最终",
    unavailable: "不可用",
    invalid: "异常",
    legacy: "历史参考"
  }[value] || value || "--";
}

function outcomeTone(label) {
  if (["强成功", "成功溢价", "弱成功"].includes(label)) return "good";
  if (["冲高回落", "数据不足"].includes(label)) return "warn";
  if (["触发失效", "低开失败", "无效", "价格异常"].includes(label)) return "bad";
  return "neutral";
}

function stockAnalysisHref(itemOrCode) {
  const code = typeof itemOrCode === "string" ? itemOrCode : itemOrCode?.code;
  const normalized = String(code || "").trim();
  if (!normalized) return "./index.html#stock";
  const stockCode = /^\d{1,6}$/.test(normalized) ? normalized.padStart(6, "0") : normalized;
  return `./index.html?stock=${encodeURIComponent(stockCode)}&analyze=1#stock`;
}

function stockAnalysisNameLink(item) {
  const name = item?.name || "--";
  const code = item?.code || "";
  const label = `${name} ${code}`.trim();
  return `<a class="stock-analysis-name-link" href="${escapeHtml(stockAnalysisHref(item))}" title="打开个股分析">${escapeHtml(label || "--")}</a>`;
}

function stockAnalysisButton(item) {
  return `
    <a class="analysis-link-button" href="${escapeHtml(stockAnalysisHref(item))}" title="打开个股分析">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3h7v7h-2V6.4l-8.3 8.3-1.4-1.4L17.6 5H14V3ZM5 5h6v2H5v12h12v-6h2v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z"/></svg>
      个股分析
    </a>
  `;
}

function entryAuditButton(item) {
  if (!window.IntradayEntryAudit) return "";
  return window.IntradayEntryAudit.buttonHtml(item, {
    triggerSource: "t1",
    runId: state.run?.id || item?.runId || "",
    triggerItemId: item?.code || "",
    scanTime: state.run?.scanTime || state.scanTime || "",
    signalDate: runSignalDate(state.run)
  });
}

function historicalSignalBadge(signalDate) {
  if (!isHistoricalSignalDate(signalDate)) return "";
  return `<div class="signal-date-warning">历史候选 · ${escapeHtml(signalDate)} · 不是今日信号</div>`;
}

function runSignalDate(run) {
  return String(run?.recommendationDate || run?.createdAt || "").split("T")[0] || "";
}

function isHistoricalSignalDate(signalDate) {
  return Boolean(signalDate && signalDate !== todayText());
}

function todayText() {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function suggestionActionLabel(action) {
  return {
    downweight: "待确认降权",
    upweight: "待确认加权",
    observe: "观察建议",
    disable: "待确认禁用",
    restore: "恢复建议"
  }[action] || action || "--";
}

function suggestionTone(item) {
  if (item.status === "active") return "good";
  if (item.suggestionAction === "downweight" || item.suggestionAction === "disable") return "warn";
  if (item.suggestionAction === "upweight") return "good";
  return "neutral";
}

function suggestionReviewerNote(status) {
  return {
    active: "用户确认启用，允许进入后续 DeepSeek / Critic 策略约束。",
    observing: "用户选择继续观察，暂不影响推荐。",
    rejected: "用户忽略该建议，不影响推荐。",
    revoked: "用户撤销已启用策略约束。"
  }[status] || "";
}

function suggestionStatusVerb(status) {
  return {
    active: "确认启用",
    observing: "设为观察",
    rejected: "忽略",
    revoked: "撤销"
  }[status] || "更新";
}

function uniqueMessages(messages) {
  const result = [];
  messages.forEach((message) => {
    const text = String(message || "").trim();
    if (text && !result.includes(text)) result.push(text);
  });
  return result;
}

function isZeroPickRun(run) {
  if (!run) return false;
  if (run.outcomeStatus === "zero_pick") return true;
  if (ZERO_PICK_SELECTION_MODES.includes(run.selectionMode)) return true;
  const finalPicks = Array.isArray(run.finalPicks) ? run.finalPicks : [];
  return run.status === "done" && Number(run.finalCount || 0) <= 0 && finalPicks.length === 0;
}

function zeroPickReason(run) {
  const market = run?.market || {};
  const policy = market.recommendationPolicy || {};
  const context = market.marketContext || {};
  return policy.reason || run?.error || (context.label ? `市场状态为${context.label}，当前按风控推荐 0 只。` : "当前 run 为空仓/避险决策，没有最终 Top3，不做个股验证。");
}

function updateHeader() {
  const auto = state.autoRunStatus || {};
  const config = auto.config || {};
  const autoText = config.enabled
    ? `自动 ${((config.scheduleTimes || []).join("/") || state.scanTime)}`
    : "手动触发";
  setText(els.pageEyebrow, `${autoText} · 固定T+1策略 · ${inputRunProfileLabel()} · 候选池 ${formatInteger(state.poolSize)}`);
}

function renderTimeNotice() {
  if (!state.run) {
    const autoNotice = autoRunNotice();
    renderNotice([autoNotice, timeHint(state.scanTime)].filter(Boolean).join(" "));
  }
}

function renderNotice(message) {
  setText(els.notice, message || "");
}

function renderError(error) {
  renderNotice(error.message || String(error));
}

function autoRunNotice() {
  const auto = state.autoRunStatus || {};
  const config = auto.config || {};
  if (!config.enabled) return "T+1 自动生成推荐未开启，需要手动点击开始筛选。";
  const schedule = (config.scheduleTimes || []).join(" / ") || "14:40";
  const alive = auto.schedulerAlive ? "调度器运行中" : "调度器未运行";
  const last = auto.lastMessage ? `最近状态：${auto.lastMessage}` : "最近状态：暂无自动生成记录。";
  return `后端会在 ${schedule} 自动生成 T+1 推荐，最晚 ${config.latestTriggerTime || "14:50"} 前触发；${alive}。${last}`;
}

function startLongAction(title, hint) {
  setText(els.actionProgressTitle, title || "正在处理");
  setText(els.actionProgressHint, hint || "请稍等");
  if (els.actionProgress) els.actionProgress.classList.remove("hidden");
}

function finishLongAction() {
  if (els.actionProgress) els.actionProgress.classList.add("hidden");
}

function showActionModal({ tone = "neutral", title = "操作完成", message = "" } = {}) {
  if (!els.actionModal) return;
  if (els.actionModalTone) {
    els.actionModalTone.className = `status-pill ${tone}`;
    els.actionModalTone.textContent = tone === "bad" ? "失败" : tone === "warn" ? "部分完成" : "完成";
  }
  setText(els.actionModalTitle, title);
  setText(els.actionModalMessage, message);
  els.actionModal.classList.remove("hidden");
}

function hideActionModal() {
  if (els.actionModal) els.actionModal.classList.add("hidden");
}

function setLoading(loading) {
  state.loading = loading;
  setDisabled(els.runButton, loading || state.actionLoading);
  setDisabled(els.latestButton, loading || state.actionLoading);
  setDisabled(els.preheatEvidenceButton, loading || state.actionLoading);
  setActionButtons();
}

function setActionLoading(loading) {
  state.actionLoading = loading;
  setDisabled(els.runButton, loading || state.loading);
  setDisabled(els.latestButton, loading || state.loading);
  setDisabled(els.preheatEvidenceButton, loading || state.loading);
  setActionButtons();
}

function setActionButtons() {
  const disabled = state.loading || state.actionLoading;
  setDisabled(els.preheatEvidenceButton, disabled);
  const zeroPick = isZeroPickRun(state.run);
  const quickResearch = isQuickResearchRun(state.run);
  setDisabled(els.saveCriticConfigButton, disabled);
  setDisabled(els.saveStageConfigButton, disabled);
  setDisabled(els.saveExecutionConfigButton, disabled);
  setDisabled(els.verifyRunButton, disabled || !state.run?.id || zeroPick);
  if (els.verifyRunButton) els.verifyRunButton.title = zeroPick ? "当前 run 为空仓/避险决策，没有最终 Top3，不能做个股验证。" : "";
  setDisabled(els.verifyPendingButton, disabled);
  setDisabled(els.historyDryRunButton, disabled);
  setDisabled(els.historySoftCleanupButton, disabled);
  setDisabled(els.refreshFeedbackButton, disabled);
  const role = state.run?.runRole || "";
  setDisabled(els.setOfficialButton, disabled || !state.run?.id || role === "legacy" || role === "official" || quickResearch);
  if (els.setOfficialButton) {
    els.setOfficialButton.title = role === "official"
      ? "当前已经是正式 run，不能重复设置。"
      : quickResearch
        ? `候选池低于 ${OFFICIAL_MIN_POOL_SIZE}，只能作为研究 run。`
        : "设为正式 run 后会进入严格复盘、策略统计和后续可优化样本。";
  }
  setDisabled(els.setResearchButton, disabled || !state.run?.id || role === "legacy" || role === "research");
  if (els.setResearchButton) {
    els.setResearchButton.title = role === "research"
      ? "当前已经是研究 run，不能重复设置。"
      : "改为研究 run 后仍可查看，但不进入严格学习和策略统计。";
  }
}

function setDisabled(element, disabled) {
  if (element) element.disabled = Boolean(disabled);
}

function inputRunProfileLabel() {
  if (Number(state.poolSize || 0) >= DEFAULT_T1_POOL_SIZE) return "生产模式";
  if (Number(state.poolSize || 0) >= OFFICIAL_MIN_POOL_SIZE) return "标准模式";
  return "快速研究";
}

function normalizeInitialPoolSize(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < OFFICIAL_MIN_POOL_SIZE) return DEFAULT_T1_POOL_SIZE;
  return clampNumber(number, OFFICIAL_MIN_POOL_SIZE, 500, DEFAULT_T1_POOL_SIZE);
}

function syncUrl() {
  const params = new URLSearchParams({
    source: state.source,
    scanTime: state.scanTime,
    poolSize: String(state.poolSize)
  });
  window.history.replaceState(null, "", `?${params.toString()}`);
  updateHeader();
}

function riskLabel(value) {
  return { balanced: "固定T+1策略", aggressive: "进攻", defensive: "防守" }[value] || value || "--";
}

function sourceName(value) {
  const text = String(value || "");
  if (text.startsWith("cache:")) return `全市场缓存(${text.slice(6) || "unknown"})`;
  return {
    auto: "自动",
    sina: "新浪",
    akshare: "AKShare/东财",
    mock: "Mock",
    multi_minute: "多源分钟线",
    tencent_minute: "腾讯分钟线",
    eastmoney_minute: "东方财富分钟线",
    akshare_eastmoney_minute: "AKShare东财分钟线",
    simulated: "模拟K线",
    unknown: "未知"
  }[text] || text || "--";
}

function statusLabel(value) {
  return { pending: "等待中", running: "筛选中", done: "完成", failed: "失败", empty: "暂无" }[value] || value || "--";
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

function modeClass(value) {
  if (value === "ai_t1_overnight") return "mode-ai";
  if (value === "ai_semifinal_rules_final") return "mode-fallback";
  if (value === "rules_fallback") return "mode-fallback";
  if (String(value || "").includes("zero_pick")) return "warning-text";
  if (value === "invalid_non_trading_day") return "warning-text";
  if (value === "ai_failed") return "error-text";
  return "";
}

function stageLabel(value) {
  if (String(value || "").startsWith("batch:")) return `AI批次 ${String(value).slice(6)}`;
  return {
    pending: "等待执行",
    loading_market: "拉取全市场行情",
    building_pool: "构建候选池",
    pool: "候选池",
    semifinal: "半决赛",
    final: "AI终选Top3",
    critic: "AI自检修正",
    done: "完成",
    failed: "失败"
  }[value] || value || "--";
}

function candidateTypeLabel(value) {
  return {
    late_money_inflow: "尾盘资金回流",
    sector_front_runner: "板块前排",
    vwap_support: "VWAP承接",
    breakout_hold: "突破维持",
    limit_up_candidate: "涨停候选",
    high_volume_attention: "放量高人气",
    low_reversal_intraday: "日内低位反转",
    risk_high_but_active: "高风险但资金活跃"
  }[value] || value || "--";
}

function criticModeLabel(value) {
  return {
    off: "关闭",
    rule_only: "仅规则审查",
    auto: "样本达标后自动",
    always: "总是介入"
  }[value] || value || "--";
}

function timeHint(value) {
  const text = String(value || "");
  if (text >= "09:30" && text <= "14:29") return "当前为预筛结果，不是最终 T+1 推荐。14:40 只是建议点击时间，系统不会自动执行。";
  if (text >= "14:30" && text <= "14:50") return "当前时间适合做 T+1 最终确认。任务只会在你点击按钮后运行。";
  if (text > "14:50") return "当前为尾盘后结果，需注意数据源是否已更新。任务只会在你点击按钮后运行。";
  return "建议在 14:30-14:50 手动点击做最终确认，系统不会自动执行。";
}

function clampNumber(value, min, max, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(min, Math.min(max, Math.round(number)));
}

function clampFloat(value, min, max, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(min, Math.min(max, number));
}

function parseCsv(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatConfigNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return String(number);
}

function formatInteger(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? String(Math.round(number)) : "--";
}

function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toFixed(1).replace(/\.0$/, "");
}

function shortHash(value) {
  const text = String(value || "");
  if (!text) return "--";
  return text.length <= 12 ? text : `${text.slice(0, 8)}...${text.slice(-4)}`;
}

function formatPct(value, alreadyPct = false) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  const normalized = alreadyPct ? number : number;
  return `${normalized > 0 ? "+" : ""}${normalized.toFixed(2).replace(/\.00$/, "")}%`;
}

function formatMoneyYi(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "--";
  return `${(number / 100000000).toFixed(0)}亿`;
}

function formatRatio(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "--";
  return `${number.toFixed(2)}x`;
}

function formatPrice(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "--";
  return number.toFixed(2);
}

function formatConfidence(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${formatNumber(Math.max(0, Math.min(100, number)))}/100`;
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  if (!Number.isFinite(value) || value <= 0) return "--";
  if (value < 60) return `${Math.round(value)}秒`;
  const minutes = Math.floor(value / 60);
  const remain = Math.round(value % 60);
  return remain ? `${minutes}分${remain}秒` : `${minutes}分`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
