const state = {
  profile: "daily",
  riskMode: "balanced",
  source: "auto",
  scanTime: "14:36",
  poolSize: 500,
  run: null,
  feedback: null,
  recentOutcomes: null,
  polling: null,
  loading: false
};

const CANDIDATE_TYPE_ORDER = [
  "strong_trend",
  "sector_leader",
  "volume_breakout",
  "pullback_support",
  "low_reversal",
  "oversold_repair",
  "high_attention_high_risk",
  "rule_disliked_but_active",
  "other"
];

const STRATEGY_BUCKETS = [
  {
    key: "strong_trend",
    title: "强趋势 Top5",
    subtitle: "趋势延续、板块前排、放量确认",
    types: ["strong_trend", "sector_leader"]
  },
  {
    key: "low_start",
    title: "低位启动 Top5",
    subtitle: "低位放量、拐点启动、首次转强",
    types: ["low_reversal", "volume_breakout"]
  },
  {
    key: "pullback_support",
    title: "回踩承接 Top5",
    subtitle: "趋势内回踩、缩量企稳、承接有效",
    types: ["pullback_support"]
  },
  {
    key: "oversold_repair",
    title: "超跌修复 Top5",
    subtitle: "急跌后企稳、反弹修复、风险收益比改善",
    types: ["oversold_repair"]
  },
  {
    key: "high_attention_high_risk",
    title: "高风险人气观察榜",
    subtitle: "人气强但波动大，只做观察和条件跟踪",
    types: ["high_attention_high_risk", "rule_disliked_but_active"]
  }
];

const els = {
  pageEyebrow: document.querySelector("#pageEyebrow"),
  pageSubtitle: document.querySelector("#pageSubtitle"),
  profileSelect: document.querySelector("#profileSelect"),
  advancedSummary: document.querySelector("#advancedSummary"),
  riskSelect: document.querySelector("#riskSelect"),
  sourceSelect: document.querySelector("#sourceSelect"),
  scanTimeLabel: document.querySelector("#scanTimeLabel"),
  scanTimeSelect: document.querySelector("#scanTimeSelect"),
  poolSizeInput: document.querySelector("#poolSizeInput"),
  latestButton: document.querySelector("#latestButton"),
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
  batchList: document.querySelector("#batchList"),
  marketQualityText: document.querySelector("#marketQualityText"),
  marketSummaryText: document.querySelector("#marketSummaryText"),
  marketBreadthText: document.querySelector("#marketBreadthText"),
  marketEmotionText: document.querySelector("#marketEmotionText"),
  marketLimitText: document.querySelector("#marketLimitText"),
  marketMainlineText: document.querySelector("#marketMainlineText"),
  marketBrokenText: document.querySelector("#marketBrokenText"),
  sectorRiskText: document.querySelector("#sectorRiskText"),
  sectorQualityText: document.querySelector("#sectorQualityText"),
  styleRegimeText: document.querySelector("#styleRegimeText"),
  styleGroupText: document.querySelector("#styleGroupText"),
  shadowPoolText: document.querySelector("#shadowPoolText"),
  shadowPoolDetailText: document.querySelector("#shadowPoolDetailText"),
  mainlineList: document.querySelector("#mainlineList"),
  diagnosticSummary: document.querySelector("#diagnosticSummary"),
  poolTypeBars: document.querySelector("#poolTypeBars"),
  semifinalTypeBars: document.querySelector("#semifinalTypeBars"),
  finalTypeBars: document.querySelector("#finalTypeBars"),
  aiCallText: document.querySelector("#aiCallText"),
  supplementText: document.querySelector("#supplementText"),
  refreshOutcomesButton: document.querySelector("#refreshOutcomesButton"),
  feedbackSampleText: document.querySelector("#feedbackSampleText"),
  feedbackOutcomeText: document.querySelector("#feedbackOutcomeText"),
  feedbackReturnText: document.querySelector("#feedbackReturnText"),
  feedbackErrorText: document.querySelector("#feedbackErrorText"),
  feedbackOptimizeText: document.querySelector("#feedbackOptimizeText"),
  feedbackConfigText: document.querySelector("#feedbackConfigText"),
  runLearningText: document.querySelector("#runLearningText"),
  runLearningDetailText: document.querySelector("#runLearningDetailText"),
  feedbackMessage: document.querySelector("#feedbackMessage"),
  feedbackRows: document.querySelector("#feedbackRows"),
  resultEyebrow: document.querySelector("#resultEyebrow"),
  pickGrid: document.querySelector("#pickGrid"),
  strategyBucketGrid: document.querySelector("#strategyBucketGrid"),
  rejectedSummaryText: document.querySelector("#rejectedSummaryText"),
  rejectedRows: document.querySelector("#rejectedRows"),
  detailRows: document.querySelector("#detailRows")
};

init();

function init() {
  const params = new URLSearchParams(window.location.search);
  state.profile = "daily";
  state.riskMode = params.get("risk") || params.get("riskMode") || "balanced";
  state.source = params.get("source") || "auto";
  state.scanTime = params.get("scanTime") || "14:36";
  state.poolSize = Number(params.get("poolSize") || 500);
  if (els.profileSelect) els.profileSelect.value = state.profile;
  els.riskSelect.value = state.riskMode;
  els.sourceSelect.value = state.source;
  els.scanTimeSelect.value = state.scanTime;
  els.poolSizeInput.value = String(state.poolSize);
  bindEvents();
  updateScanTimeVisibility();
  updateHeader();
  loadLatest();
}

function bindEvents() {
  if (els.profileSelect) {
    els.profileSelect.addEventListener("change", () => {
      state.profile = "daily";
      updateScanTimeVisibility();
      syncUrl();
    });
  }
  els.riskSelect.addEventListener("change", () => {
    state.riskMode = els.riskSelect.value || "balanced";
    updateAdvancedSummary();
    syncUrl();
  });
  els.sourceSelect.addEventListener("change", () => {
    state.source = els.sourceSelect.value || "auto";
    syncUrl();
  });
  els.scanTimeSelect.addEventListener("change", () => {
    state.scanTime = els.scanTimeSelect.value || "14:36";
    syncUrl();
  });
  els.poolSizeInput.addEventListener("change", () => {
    state.poolSize = clampNumber(els.poolSizeInput.value, 50, 800, 500);
    els.poolSizeInput.value = String(state.poolSize);
    updateAdvancedSummary();
    syncUrl();
  });
  els.latestButton.addEventListener("click", loadLatest);
  els.runButton.addEventListener("click", startRun);
  els.refreshOutcomesButton.addEventListener("click", refreshOutcomes);
}

async function loadLatest() {
  setLoading(true);
  clearPolling();
  try {
    const params = new URLSearchParams({ profile: state.profile, riskMode: state.riskMode, source: state.source, scanTime: state.scanTime });
    const response = await fetch(`/api/ai-full-market-selection/latest?${params.toString()}`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.run = payload.status === "empty" ? null : payload;
    render();
    loadFeedback();
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
    const response = await fetch("/api/ai-full-market-selection/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        profile: state.profile,
        riskMode: state.riskMode,
        source: state.source,
        scanTime: state.scanTime,
        poolSize: state.poolSize,
        batchSize: 80,
        batchPick: 10,
        finalLimit: 10,
        force: els.forceCheck.checked
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.run = payload;
    render();
    loadFeedback();
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
      const response = await fetch(`/api/ai-full-market-selection/runs/${runId}`);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
      state.run = payload;
      render();
      if (!["pending", "running"].includes(payload.status)) {
        clearPolling();
        loadFeedback();
      }
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

async function loadFeedback() {
  try {
    const runId = state.run && state.run.id ? String(state.run.id) : "";
    const [statsResponse] = await Promise.all([
      fetch(`/api/ai-full-market-selection/feedback-stats${runId ? `?runId=${encodeURIComponent(runId)}` : ""}`)
    ]);
    const stats = await statsResponse.json().catch(() => ({}));
    if (!statsResponse.ok) throw new Error(stats.error || `API ${statsResponse.status}`);
    state.feedback = stats;
    state.recentOutcomes = null;
    renderFeedback();
  } catch (error) {
    renderFeedbackError(error);
  }
}

async function refreshOutcomes() {
  setFeedbackLoading(true);
  try {
    const body = {
      horizons: [1, 3, 5, 7],
      limit: 120,
      forceHistory: false
    };
    if (state.run && state.run.status === "done") body.runId = state.run.id;
    const response = await fetch("/api/ai-full-market-selection/outcomes/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    els.feedbackMessage.textContent = `已回填 ${formatInteger(payload.runCount)} 个任务 · T+1/T+3/T+5/T+7 共 ${formatInteger(payload.doneCount)} 条完成 · ${formatInteger(payload.pendingCount)} 条待更新`;
    await loadFeedback();
  } catch (error) {
    renderFeedbackError(error);
  } finally {
    setFeedbackLoading(false);
  }
}

function render() {
  updateHeader();
  const run = state.run;
  if (!run) {
    renderNotice("今日尚未生成 AI 全市场重点观察结果。点击“生成今日标准推荐”会新建独立任务，不会回退到历史数据；Top10 是 1-7 日候选，不是立即买入信号。");
    renderEmpty();
    return;
  }
  renderNotice(runNotice(run));
  els.statusText.textContent = statusLabel(run.status);
  els.modeText.innerHTML = `<span class="${modeClass(run.selectionMode)}">${selectionModeLabel(run.selectionMode)}</span> · ${escapeHtml(sampleRoleLabel(run.runSampleRole))}`;
  els.poolText.textContent = formatInteger(run.poolSize);
  els.universeText.textContent = `原始 ${formatInteger(run.totalCount)} · 硬过滤后 ${formatInteger(run.hardFilteredCount)}`;
  els.batchText.textContent = `${formatInteger(run.completedBatchCount)} / ${formatInteger(run.batchCount)}`;
  els.stageText.textContent = run.currentStage || "--";
  els.finalText.textContent = `${formatInteger(run.finalCount)} / ${formatInteger(run.finalLimit)}`;
  els.elapsedText.textContent = `${profileLabel(run.profile)} · ${riskLabel(run.riskMode)} · ${formatDuration(run.elapsedSeconds)}`;
  els.progressBar.style.width = `${Number(run.progressPct || 0)}%`;
  els.resultEyebrow.textContent = `${profileLabel(run.profile)} · ${selectionModeLabel(run.selectionMode)} · ${sourceName(run.actualSource || run.requestedSource)} · ${run.scanTime || "--"}`;
  renderBatchList(run.batchSummary || []);
  renderMarketEnvironment(run);
  renderRunLearning(run);
  renderDiagnostics(run);
  renderPicks(run.finalPicks || []);
  renderStrategyBuckets(run);
  renderRejectedRows(run);
  renderRows(run.items || []);
}

function renderEmpty() {
  els.statusText.textContent = "--";
  els.modeText.textContent = "--";
  els.poolText.textContent = "--";
  els.universeText.textContent = "--";
  els.batchText.textContent = "--";
  els.stageText.textContent = "--";
  els.finalText.textContent = "--";
  els.elapsedText.textContent = "--";
  els.progressBar.style.width = "0%";
  els.batchList.innerHTML = "";
  els.diagnosticSummary.textContent = "--";
  els.poolTypeBars.innerHTML = `<div class="empty compact">暂无候选池类型</div>`;
  els.semifinalTypeBars.innerHTML = `<div class="empty compact">暂无半决赛类型</div>`;
  els.finalTypeBars.innerHTML = `<div class="empty compact">暂无最终类型</div>`;
  els.aiCallText.textContent = "--";
  els.supplementText.textContent = "--";
  els.marketQualityText.textContent = "--";
  els.marketSummaryText.textContent = "--";
  els.marketBreadthText.textContent = "--";
  els.marketEmotionText.textContent = "--";
  els.marketLimitText.textContent = "--";
  els.marketMainlineText.textContent = "--";
  els.marketBrokenText.textContent = "--";
  els.sectorRiskText.textContent = "--";
  els.sectorQualityText.textContent = "--";
  els.styleRegimeText.textContent = "--";
  els.styleGroupText.textContent = "--";
  els.shadowPoolText.textContent = "--";
  els.shadowPoolDetailText.textContent = "--";
  els.mainlineList.innerHTML = `<div class="empty compact">暂无主线数据</div>`;
  els.runLearningText.textContent = "--";
  els.runLearningDetailText.textContent = "--";
  els.pickGrid.innerHTML = `<div class="empty">暂无最终 Top10</div>`;
  els.strategyBucketGrid.innerHTML = `<div class="empty">暂无策略分层榜</div>`;
  els.rejectedSummaryText.textContent = "--";
  els.rejectedRows.innerHTML = `<tr><td colspan="6" class="empty">暂无拒绝样本</td></tr>`;
  els.detailRows.innerHTML = `<tr><td colspan="9" class="empty">暂无明细</td></tr>`;
}

function renderFeedback() {
  const feedback = state.feedback || {};
  const historyStats = feedback.stats || {};
  const currentStats = feedback.currentRunStats || {};
  const hasCurrentStats = Boolean(currentStats.rowCount);
  const stats = hasCurrentStats ? currentStats : historyStats;
  const report = feedback.report || {};
  const config = feedback.config || {};
  els.feedbackSampleText.textContent = hasCurrentStats
    ? `${formatInteger(currentStats.sampleSelectedCount)}只Top10`
    : `${formatInteger(historyStats.selectedCount)} / ${formatInteger(historyStats.rejectedCount)}`;
  els.feedbackOutcomeText.textContent = hasCurrentStats
    ? `Top10验证：完成 ${formatInteger(currentStats.selectedDoneRowCount)} · 待更新 ${formatInteger(currentStats.selectedPendingRowCount)} · 无数据 ${formatInteger(currentStats.selectedNoDataRowCount)} · 对照 ${formatInteger(currentStats.sampleRejectedCount)}只`
    : `历史：完成 ${formatInteger(historyStats.doneCount)} · 7日完成 ${formatInteger(historyStats.horizon7Count)}`;
  const currentDoneSelected = Number(currentStats.selectedCount || 0);
  els.feedbackReturnText.textContent = hasCurrentStats && currentDoneSelected <= 0 ? "待验证" : `${formatSigned(stats.selectedAvgReturnPct)}%`;
  els.feedbackErrorText.textContent = hasCurrentStats && currentDoneSelected <= 0
    ? `当前Top10暂无完整 T+7 数据 · 全量候选待更新 ${formatInteger(currentStats.pendingCount)}`
    : `候选错买 ${formatInteger(stats.falsePositiveCount)} · 漏判 ${formatInteger(stats.missedOpportunityCount)} · 交易未验证 ${formatInteger(stats.tradeUnverifiableCount)}`;
  els.feedbackOptimizeText.textContent = report.eligible ? "样本已达标" : "等待样本";
  els.feedbackConfigText.textContent = `已完成历史 Top10 ${formatInteger(historyStats.selectedCount)} · 对照 ${formatInteger(historyStats.rejectedCount)}`;
  els.feedbackMessage.textContent = hasCurrentStats && currentStats.doneCount === 0
    ? "当前Top10还没有未来 T+1/T+3/T+5/T+7 完整行情，暂时不能评价表现；已完成的历史结果请进入历史复盘查看。"
    : `${learningStatusMessage(report, config)} ${stats.tradePerformanceNote || ""}`.trim();
}

function renderRunLearning(run) {
  const meta = run.learningMeta || {};
  const config = run.selectionConfig || {};
  const learnedTypes = meta.learnedTypes || [];
  const learnedSectorStates = meta.learnedSectorStates || [];
  const learnedText = learnedTypes.slice(0, 2).map((item) => `${item.label || candidateTypeLabel(item.candidateType)} ${formatNumber(item.scale)}x`).join(" · ");
  const learnedSectorText = learnedSectorStates.slice(0, 2).map((item) => `${item.label || sectorStateLabel(item.sectorState)} ${formatNumber(item.scale)}x`).join(" · ");
  const effective = run.effectiveLearningRun || {};
  const effectiveText = effective.id
    ? `学习采用 Run ${formatInteger(effective.id)}`
    : "暂无有效学习样本";
  els.runLearningText.textContent = meta.hasLearnedParams ? "使用学习参数" : "默认参数";
  els.runLearningDetailText.textContent = meta.hasLearnedParams
    ? `${learnedText || learnedSectorText || sampleRoleLabel(run.runSampleRole)} · 今日${formatInteger(run.dailyRunCount)}次 · ${effectiveText}`
    : `${sampleRoleLabel(run.runSampleRole)} · 今日${formatInteger(run.dailyRunCount)}次 · ${effectiveText} · Prompt ${config.promptVersion || run.promptVersion || "--"}`;
}

function renderFeedbackError(error) {
  els.feedbackMessage.textContent = error.message || String(error);
}

function setFeedbackLoading(loading) {
  els.refreshOutcomesButton.disabled = loading;
}

function learningStatusMessage(report, config) {
  const reason = report.reason || (report.suggestion || {}).reason || "";
  const promptVersion = config.promptVersion || "--";
  if (report.eligible) {
    return `历史样本已满足参数学习门槛；如需更新选股参数，请进入历史复盘页的“参数学习”。当前 Prompt ${promptVersion}`;
  }
  return `${reason || "等待更多已完成的历史样本"}。参数学习只使用 AI 全市场历史验证数据，不影响 T+1、个股分析、首页 Top20 和回测。`;
}

function renderBatchList(batches) {
  if (!batches.length) {
    els.batchList.innerHTML = `<span class="batch-pill">等待批次结果</span>`;
    return;
  }
  els.batchList.innerHTML = batches.map((batch) => `
    <span class="batch-pill ${batch.error ? "batch-error" : ""}" title="${escapeHtml(batch.error || "")}">
      批次 ${formatInteger(batch.batchIndex)} · ${formatInteger(batch.candidateCount)} 选 ${formatInteger(batch.targetPickCount || batch.pickCount)} · 入 ${formatInteger(batch.pickCount)}
      ${batch.outOfPoolPickCount ? ` · 池外${formatInteger(batch.outOfPoolPickCount)}` : ""}
    </span>
  `).join("");
}

function renderMarketEnvironment(run) {
  const market = run.market || {};
  const environment = run.marketEnvironment || market.environment || {};
  const mainlines = run.mainlines || market.mainlines || [];
  const sectorSummary = market.sectorRiskSummary || {};
  const sectorMeta = market.sectorEvidenceMeta || (market.sectorContext || {}).meta || {};
  const style = market.styleRegime || (market.sectorContext || {}).style || {};
  const shadow = market.shadowPoolSummary || {};
  const quality = environment.dataQuality || {};
  els.marketQualityText.textContent = quality.message || "--";
  els.marketSummaryText.textContent = environment.summary || "--";
  els.marketBreadthText.textContent = `${formatInteger(environment.up)}涨 / ${formatInteger(environment.down)}跌 · 上涨 ${formatNumber(environment.upRatioPct)}%`;
  els.marketEmotionText.textContent = environment.emotionPhase || "--";
  els.marketLimitText.textContent = `涨停 ${formatInteger(environment.limitUp)} · 跌停 ${formatInteger(environment.limitDown)} · 近涨停 ${formatInteger(environment.nearLimitUp)}`;
  els.marketMainlineText.textContent = `${environment.mainlineStatus || "--"} · ${formatInteger(environment.mainlineCount)}条`;
  els.marketBrokenText.textContent = `炸板率代理 ${formatNumber(environment.brokenLimitRateProxyPct)}% · ${environment.indexState || "--"}`;
  const riskSectors = sectorSummary.riskSectors || [];
  const favoredSectors = sectorSummary.favoredSectors || [];
  els.sectorRiskText.textContent = `风险 ${formatInteger(riskSectors.length)} · 优势 ${formatInteger(favoredSectors.length)}`;
  els.sectorQualityText.textContent = `${sectorMeta.dataQuality || sectorSummary.dataQuality || "--"} · 覆盖 ${formatMaybePct(sectorSummary.stockCoveragePct ?? sectorMeta.coveragePct)}`;
  els.styleRegimeText.textContent = styleRegimeLabel(style.styleRegime);
  els.styleGroupText.textContent = `风险 ${formatGroupList(style.riskGroups)} · 轮入 ${formatGroupList(style.favoredGroups)}`;
  els.shadowPoolText.textContent = shadow.enabled ? (shadow.formalUsesSectorExposure ? "已影响正式池" : "仅影子保存") : "未启用";
  els.shadowPoolDetailText.textContent = `Top50差异 ${formatInteger(shadow.baseTop50ChangedCount)} · ${sectorMeta.asOfTime || sectorMeta.tradeDate || "--"}`;
  els.mainlineList.innerHTML = mainlines.length ? mainlines.slice(0, 8).map((line) => `
    <div class="mainline-chip">
      <strong>${escapeHtml(line.name || "--")}</strong>
      <span>${formatInteger(line.count)}只 · 均涨 ${formatSigned(line.avgChange)}% · 涨停 ${formatInteger(line.limitUpCount)} · 额因子 ${formatNumber(line.avgAmountFactor)}</span>
      <small>${(line.leaders || []).map((leader) => `${escapeHtml(leader.name || "--")} ${formatSigned(leader.change)}%`).join(" · ") || "--"}</small>
    </div>
  `).join("") : `<div class="empty compact">暂无可推断主线</div>`;
}

function renderDiagnostics(run) {
  const calls = run.aiCallSummary || {};
  const supplement = run.semifinalSupplement || {};
  const finalCounts = run.finalTypeCounts || {};
  els.diagnosticSummary.textContent = `Prompt ${run.promptVersion || "--"} · 最终风格 ${formatTypeSummary(finalCounts)}`;
  els.poolTypeBars.innerHTML = renderTypeBars(run.poolTypeCounts || {});
  els.semifinalTypeBars.innerHTML = renderTypeBars(run.semifinalTypeCounts || {});
  els.finalTypeBars.innerHTML = renderTypeBars(finalCounts);
  els.aiCallText.textContent = `AI调用：批次 ${formatInteger(calls.batchCallCount)} · 终选 ${formatInteger(calls.finalCallCount)} · 失败 ${formatInteger(calls.failedCallCount)} · 空选 ${formatInteger(calls.emptyPickCount)} · 平均 ${formatInteger(calls.avgElapsedMs)}ms`;
  els.supplementText.textContent = `半决赛补位：${formatInteger(supplement.addedCount)} · ${formatTypeSummary(supplement.addedByType || {})}`;
}

function renderPicks(picks) {
  if (!picks.length) {
    els.pickGrid.innerHTML = `<div class="empty">任务完成后显示最终 Top10</div>`;
    return;
  }
  els.pickGrid.innerHTML = picks.map((pick) => `
    <article class="pick-card">
      <div class="pick-card-head">
        <div>
          <h3>#${formatInteger(pick.aiRank || pick.rank)} ${stockAnalysisNameLink(pick)}</h3>
          <small>候选池 #${formatInteger(pick.ruleRank)} · 规则分 ${formatNumber(pick.score)} · AI置信度 ${formatConfidence(pick.confidence)} · 采用 ${escapeHtml(candidateTypeLabel(pick.candidateTypeUsed || pick.aiCandidateTypeUsed || pick.candidateType))}</small>
        </div>
        <div class="pick-score">${formatNumber(pick.confidence || pick.aiSelectionScore || pick.score)}</div>
      </div>
      ${historicalSignalBadge(runSignalDate(state.run))}
      <div class="pick-card-actions">
        ${stockAnalysisButton(pick)}
        ${entryAuditButton(pick)}
      </div>
      <div class="tag-row">${renderCandidateTags(pick)}</div>
      <div class="evidence-line">${escapeHtml(compactEvidence(pick))}</div>
      <dl>
        <div><dt>候选假设</dt><dd>${escapeHtml(pick.strategyHypothesis || "--")}</dd></div>
        <div><dt>入选理由</dt><dd>${escapeHtml(pick.reason || pick.aiSelectionReason || "--")}</dd></div>
        <div><dt>同类比较</dt><dd>${escapeHtml(pick.whyBetterThanSimilar || pick.aiWhyBetterThanSimilar || "--")}</dd></div>
        <div><dt>支持证据</dt><dd>${escapeHtml(formatEvidenceList(pick.supportingEvidenceRefs))}</dd></div>
        <div><dt>反方证据</dt><dd>${escapeHtml(formatEvidenceList(pick.counterEvidenceRefs))}</dd></div>
        <div><dt>缺失证据</dt><dd>${escapeHtml(formatEvidenceList(pick.missingEvidence))}</dd></div>
        <div><dt>市场匹配</dt><dd>${escapeHtml(pick.marketFit || "--")}</dd></div>
        <div><dt>板块匹配</dt><dd>${escapeHtml(pick.sectorFit || sectorEvidenceText(pick))}</dd></div>
        <div><dt>板块状态</dt><dd>${escapeHtml(sectorStateText(pick))}</dd></div>
        <div><dt>例外理由</dt><dd>${escapeHtml(pick.sectorExceptionUsed ? (pick.sectorExceptionReason || pick.aiSectorExceptionReason || "--") : "未使用风险板块例外")}</dd></div>
        <div><dt>入手条件</dt><dd>${escapeHtml(pick.entryCondition || pick.aiEntryCondition || pick.buyCondition || "--")}</dd></div>
        <div><dt>失效条件</dt><dd>${escapeHtml(pick.invalidCondition || pick.aiInvalidCondition || "--")}</dd></div>
        <div><dt>主要风险</dt><dd>${escapeHtml(pick.risk || pick.aiSelectionRisk || "--")}</dd></div>
        <div><dt>确认点</dt><dd>${escapeHtml(pick.focus || pick.aiSelectionFocus || "--")}</dd></div>
        <div><dt>置信说明</dt><dd>${escapeHtml(pick.confidenceReason || pick.aiConfidenceReason || "--")}</dd></div>
      </dl>
    </article>
  `).join("");
}

function renderStrategyBuckets(run) {
  const buckets = Array.isArray(run.strategyBuckets) && run.strategyBuckets.length ? run.strategyBuckets : buildStrategyBuckets(run);
  els.strategyBucketGrid.innerHTML = buckets.map((bucket) => `
    <article class="strategy-bucket ${bucket.key === "high_attention_high_risk" ? "watch-bucket" : ""}">
      <div class="strategy-bucket-head">
        <div>
          <h3>${escapeHtml(bucket.title)}</h3>
          <small>${escapeHtml(bucket.subtitle)}</small>
        </div>
        <strong>${formatInteger(bucket.items.length)}</strong>
      </div>
      <div class="strategy-list">
        ${bucket.items.length ? bucket.items.map((item, index) => renderStrategyBucketRow(item, index)).join("") : `<div class="empty compact">暂无匹配候选</div>`}
      </div>
    </article>
  `).join("");
}

function renderRejectedRows(run) {
  const allRows = rejectedCandidates(run);
  const finalRejectedRows = allRows.filter((row) => row.stage === "final_rejected");
  const rows = finalRejectedRows.slice(0, 20);
  const stats = ((run.market || {}).rejectedStats) || {};
  const totalRejected = Number(stats.totalRejectedCount || 0) || allRows.length || Number(stats.rejectedCount || 0);
  const finalRejected = finalRejectedRows.length;
  const batchRejected = allRows.filter((row) => row.stage === "batch_rejected").length;
  els.rejectedSummaryText.textContent = `未入选 ${formatInteger(totalRejected)} · 终选拒绝 ${formatInteger(finalRejected)} · 批次未入选 ${formatInteger(batchRejected)} · 展开显示 ${formatInteger(rows.length)}`;
  if (!rows.length) {
    els.rejectedRows.innerHTML = `<tr><td colspan="6" class="empty">暂无拒绝样本</td></tr>`;
    return;
  }
  els.rejectedRows.innerHTML = rows.map((row) => `
    <tr>
      <td>#${formatInteger(row.ruleRank)}</td>
      <td>
        <strong>${stockAnalysisNameLink(row)}</strong>
        <small>${escapeHtml(row.sector || (row.item || {}).sector || "")}</small>
      </td>
      <td>${renderCandidateTags(row)}</td>
      <td>${stageLabel(row.stage)}</td>
      <td>${escapeHtml(row.reason || "--")}</td>
      <td>${escapeHtml(row.focus || row.risk || "--")}</td>
    </tr>
  `).join("");
}

function rejectedCandidates(run) {
  const finalCodes = new Set((run.finalPicks || []).map((pick) => normalizeCode(pick.code)));
  return (run.items || [])
    .filter((row) => !finalCodes.has(normalizeCode(row.code)) && row.stage !== "pool")
    .map((row) => ({ ...(row.item || {}), ...(row.pick || {}), ...row }))
    .sort((a, b) => rejectedStageWeight(b.stage) - rejectedStageWeight(a.stage) || Number(a.ruleRank || 9999) - Number(b.ruleRank || 9999));
}

function rejectedStageWeight(stage) {
  return { final_rejected: 3, batch_rejected: 2, rules_fallback: 1, semifinal: 1 }[stage] || 0;
}

function renderStrategyBucketRow(item, index) {
  const selected = Boolean(item.selected);
  const rankText = selected ? `Top${formatInteger(item.aiRank || item.rank)}` : stageLabel(item.stage);
  return `
    <div class="strategy-row ${selected ? "selected" : ""}">
      <span class="strategy-rank">${formatInteger(index + 1)}</span>
      <div class="strategy-main">
        <div class="strategy-title">
          <strong>${stockAnalysisNameLink(item)}</strong>
          <span class="${selected ? "strategy-badge selected" : "strategy-badge"}">${escapeHtml(rankText)}</span>
        </div>
        <div class="strategy-meta">
          候选#${formatInteger(item.ruleRank)} · 分数 ${formatNumber(item.score)} · ${escapeHtml(candidateTypeLabel(item.candidateTypeUsed || item.aiCandidateTypeUsed || item.candidateType))}
        </div>
        <div class="tag-row compact">${renderCandidateTags(item)}</div>
        <p>${escapeHtml(strategyReason(item))}</p>
      </div>
    </div>
  `;
}

function buildStrategyBuckets(run) {
  const candidates = normalizeStrategyCandidates(run);
  return STRATEGY_BUCKETS.map((bucket) => ({
    ...bucket,
    items: candidates
      .filter((item) => matchStrategyBucket(item, bucket))
      .sort((a, b) => strategyRankValue(b, bucket) - strategyRankValue(a, bucket))
      .slice(0, 5)
  }));
}

function normalizeStrategyCandidates(run) {
  const finalMap = new Map((run.finalPicks || []).map((pick) => [normalizeCode(pick.code), pick]));
  const seen = new Map();
  for (const row of run.items || []) {
    const raw = row.item || {};
    const pick = row.pick || {};
    const finalPick = finalMap.get(normalizeCode(raw.code || row.code || pick.code)) || {};
    const merged = {
      ...raw,
      ...pick,
      ...finalPick,
      stage: row.stage || finalPick.stage || pick.stage || "pool",
      selected: Boolean(row.selected || finalPick.code),
      aiRank: finalPick.aiRank || finalPick.rank || row.aiRank || pick.aiRank || pick.rank || 0,
      ruleRank: raw.ruleRank || row.ruleRank || pick.ruleRank || finalPick.ruleRank || 0,
      score: raw.score ?? row.score ?? pick.score ?? finalPick.score,
      reason: finalPick.reason || finalPick.aiSelectionReason || row.reason || pick.reason || "",
      risk: finalPick.risk || finalPick.aiSelectionRisk || row.risk || pick.risk || "",
      focus: finalPick.focus || finalPick.aiSelectionFocus || row.focus || pick.focus || "",
      whyBetterThanSimilar: finalPick.whyBetterThanSimilar || finalPick.aiWhyBetterThanSimilar || pick.whyBetterThanSimilar || "",
      candidateTypeUsed: finalPick.candidateTypeUsed || finalPick.aiCandidateTypeUsed || pick.candidateTypeUsed || row.candidateTypeUsed || raw.candidateType,
      candidateTypes: uniqueValues([...(raw.candidateTypes || []), ...(pick.candidateTypes || []), raw.candidateType, pick.candidateType, finalPick.candidateType, finalPick.candidateTypeUsed])
    };
    const code = normalizeCode(merged.code);
    if (!code) continue;
    const previous = seen.get(code);
    if (!previous || strategyRankValue(merged, STRATEGY_BUCKETS[0]) > strategyRankValue(previous, STRATEGY_BUCKETS[0])) {
      seen.set(code, merged);
    }
  }
  for (const pick of run.finalPicks || []) {
    const code = normalizeCode(pick.code);
    if (!code || seen.has(code)) continue;
    seen.set(code, { ...pick, selected: true, stage: "final", candidateTypes: uniqueValues([...(pick.candidateTypes || []), pick.candidateType, pick.candidateTypeUsed]) });
  }
  return [...seen.values()];
}

function matchStrategyBucket(item, bucket) {
  const types = candidateTypesOf(item);
  if (bucket.key === "low_start") {
    return types.includes("low_reversal") || (types.includes("volume_breakout") && isLowStartLike(item));
  }
  return bucket.types.some((type) => types.includes(type));
}

function isLowStartLike(item) {
  const metrics = item.dailyMetrics || {};
  const distanceMa20 = Number(metrics.distanceMa20);
  const return20d = Number(metrics.return20d);
  const amountRatio = Number(metrics.amountRatio5d || item.amountFactor || item.volumeRatio);
  const nearMa20 = Number.isFinite(distanceMa20) && distanceMa20 <= 8;
  const notExtended = !Number.isFinite(return20d) || return20d <= 12;
  const hasVolume = !Number.isFinite(amountRatio) || amountRatio >= 1.1;
  return nearMa20 && notExtended && hasVolume;
}

function strategyRankValue(item, bucket) {
  const selectedBonus = item.selected ? 100000 - Number(item.aiRank || item.rank || 99) * 1000 : 0;
  const stageBonus = {
    final: 50000,
    semifinal: 30000,
    final_rejected: 22000,
    batch_rejected: 12000,
    rules_fallback: 9000,
    pool: 0
  }[item.stage] || 0;
  const typeBonus = candidateTypesOf(item).some((type) => bucket.types.includes(type)) ? 3000 : 0;
  const score = Number(item.confidence || item.aiSelectionScore || item.score || 0);
  const ruleRank = Number(item.ruleRank || 9999);
  return selectedBonus + stageBonus + typeBonus + score - Math.min(ruleRank, 9999) / 10;
}

function candidateTypesOf(item) {
  return uniqueValues([
    ...(Array.isArray(item.candidateTypes) ? item.candidateTypes : []),
    item.candidateType,
    item.candidateTypeUsed,
    item.aiCandidateTypeUsed
  ]).filter(Boolean);
}

function strategyReason(item) {
  return item.whyBetterThanSimilar || item.reason || item.focus || compactEvidence(item);
}

function normalizeCode(value) {
  const code = String(value || "").trim();
  return code ? code.padStart(6, "0") : "";
}

function uniqueValues(values) {
  return [...new Set(values.map((value) => String(value || "").trim()).filter(Boolean))];
}

function renderRows(items) {
  const rows = items.filter((item) => item.selected || item.stage !== "pool").slice(0, 120);
  if (!rows.length) {
    els.detailRows.innerHTML = `<tr><td colspan="9" class="empty">暂无明细</td></tr>`;
    return;
  }
  els.detailRows.innerHTML = rows.map((item) => `
    <tr>
      <td>${item.selected ? `#${formatInteger(item.aiRank)}` : "--"}</td>
      <td>#${formatInteger(item.ruleRank)}</td>
      <td>
        <strong>${stockAnalysisNameLink(item)}</strong>
        <small>${escapeHtml((item.item || {}).sector || "")}</small>
      </td>
      <td>${renderCandidateTags(item.pick || item.item || item)}</td>
      <td>${formatNumber(item.score)}</td>
      <td>${stageLabel(item.stage)}<small>${item.batchIndex ? `批次 ${formatInteger(item.batchIndex)}` : ""}</small></td>
      <td>${escapeHtml(item.reason || "--")}</td>
      <td>${escapeHtml(item.risk || "--")}</td>
      <td>${escapeHtml(item.focus || "--")}</td>
    </tr>
  `).join("");
}

function updateHeader() {
  els.pageEyebrow.textContent = `${profileLabel(state.profile)} · ${riskLabel(state.riskMode)} · 候选池 ${formatInteger(state.poolSize)}`;
  els.pageSubtitle.textContent = `${state.scanTime} 数据截止 · 面向接下来 1-7 个交易日的重点观察候选 Top10`;
  updateAdvancedSummary();
}

function updateAdvancedSummary() {
  els.advancedSummary.textContent = `${riskLabel(state.riskMode)} · ${formatInteger(state.poolSize)}`;
}

function renderNotice(message) {
  els.notice.hidden = !message;
  els.notice.textContent = message || "";
}

function runNotice(run) {
  const message = String((run || {}).error || "").trim();
  if (!message) {
    if (isHistoricalSignalDate(runSignalDate(run))) {
      return `当前展示的是历史候选 · ${runSignalDate(run)}，不是今日 AI 全市场信号。可以切换观察时点，或点击“生成今日标准推荐”后再审计。`;
    }
    if (run.runSampleRole === "temporary_observation") {
      return `当前是${sampleRoleLabel(run.runSampleRole)}结果，可作为当天参考，但不参与参数学习；${run.standardScanTime || "14:36"} 后可生成今日标准推荐。`;
    }
    if (run.runSampleRole === "late_standard") {
      return `当前是${sampleRoleLabel(run.runSampleRole)}结果，会作为今日标准结果候选参与参数学习，但复盘时会保留后补标记。`;
    }
    if (run.effectiveLearningRun && run.effectiveLearningRun.id && !run.isEffectiveLearningRun) {
      return `当前展示 Run ${formatInteger(run.id)}；参数学习采用今日有效样本 Run ${formatInteger(run.effectiveLearningRun.id)}。`;
    }
    return "当前 Top10 是未来 1-7 个交易日重点观察候选，不是立即买入信号；实际入手需要等待入手条件触发且失效条件未触发。";
  }
  if (run.selectionMode === "rules_fallback") {
    return "AI 调用连接中断，当前展示规则兜底结果；建议点击“生成今日标准推荐”重新运行。";
  }
  if (message.includes("EOF occurred") || message.includes("IncompleteRead") || message.includes("Remote end closed")) {
    return "AI 调用连接不稳定，请稍后重新运行。";
  }
  return message;
}

function sampleRoleLabel(value) {
  return {
    temporary_observation: "临时观察",
    standard: "标准推荐",
    late_standard: "后补标准"
  }[value] || "未标记";
}

function renderError(error) {
  renderNotice(error.message || String(error));
}

function setLoading(loading) {
  state.loading = loading;
  els.runButton.disabled = loading;
  els.latestButton.disabled = loading;
}

function updateScanTimeVisibility() {
  els.scanTimeLabel.style.display = "grid";
}

function syncUrl() {
  state.profile = "daily";
  const params = new URLSearchParams({
    profile: state.profile,
    risk: state.riskMode,
    source: state.source,
    scanTime: state.scanTime,
    poolSize: String(state.poolSize)
  });
  window.history.replaceState(null, "", `?${params.toString()}`);
  updateHeader();
}

function normalizeProfile(value) {
  return "daily";
}

function profileLabel(value) {
  return "1-7日重点观察候选 Top10";
}

function riskLabel(value) {
  return { balanced: "均衡", aggressive: "进攻", defensive: "防守" }[value] || value || "--";
}

function sourceName(value) {
  return { auto: "自动", sina: "新浪", tencent: "腾讯", akshare: "AKShare/东财", mock: "Mock" }[value] || value || "--";
}

function statusLabel(value) {
  return { pending: "等待中", running: "选股中", done: "完成", failed: "失败", empty: "暂无" }[value] || value || "--";
}

function selectionModeLabel(value) {
  return {
    ai_full_market: "AI全市场",
    rules_fallback: "规则兜底",
    ai_failed: "AI失败",
    rules: "规则"
  }[value] || value || "--";
}

function modeClass(value) {
  if (value === "ai_full_market") return "mode-ai";
  if (value === "rules_fallback") return "mode-fallback";
  if (value === "ai_failed") return "error-text";
  return "";
}

function stageLabel(value) {
  return {
    pool: "候选池",
    semifinal: "半决赛",
    final: "最终入选",
    batch_rejected: "批次未入选",
    final_rejected: "终选未入选",
    rules_fallback: "规则未入选"
  }[value] || value || "--";
}

function outcomeLabel(value) {
  return {
    "": "--",
    true_positive: "正确入选",
    weak_positive: "方向偏弱",
    false_positive: "错买",
    not_triggered: "未触发",
    missed_opportunity: "漏判",
    correct_reject: "正确拒绝",
    neutral_reject: "中性拒绝",
    pending: "待更新",
    no_data: "无数据"
  }[value] || value || "--";
}

function outcomeStatusLabel(row) {
  const status = String((row || {}).status || "");
  if (status === "pending") return "待未来行情";
  if (status === "no_data") return "缺少K线";
  return outcomeLabel((row || {}).label);
}

function candidateTypeLabel(value) {
  return {
    strong_trend: "强趋势",
    sector_leader: "板块前排",
    volume_breakout: "放量启动",
    pullback_support: "回踩承接",
    low_reversal: "低位启动",
    oversold_repair: "超跌修复",
    high_attention_high_risk: "高人气高风险",
    rule_disliked_but_active: "规则不喜但异动",
    other: "其他"
  }[value] || value || "--";
}

function renderCandidateTags(item) {
  const raw = item || {};
  const types = Array.isArray(raw.candidateTypes) ? raw.candidateTypes : raw.candidateType ? [raw.candidateType] : [];
  const riskFlags = Array.isArray(raw.riskFlags) ? raw.riskFlags.slice(0, 2) : [];
  const tradeability = raw.tradeability || {};
  const sectorEvidenceStatus = raw.sectorEvidenceStatus || raw.sector_evidence_status;
  const sectorState = raw.sectorState || raw.sector_state;
  const tags = [
    ...types.map((type) => ({ label: candidateTypeLabel(type), className: type === "high_attention_high_risk" ? "tag warn" : "tag" })),
    ...(tradeability.label ? [{ label: tradeability.label, className: tradeability.level === "tradable" || tradeability.level === "daily" ? "tag muted" : "tag warn" }] : []),
    ...(sectorEvidenceStatus ? [{ label: sectorEvidenceLabel(sectorEvidenceStatus), className: sectorEvidenceStatus === "available" ? "tag muted" : "tag warn" }] : []),
    ...(sectorState ? [{ label: sectorStateLabel(sectorState), className: ["retreat", "distribution", "weakening", "overheated"].includes(sectorState) ? "tag warn" : "tag muted" }] : []),
    ...(raw.sectorExceptionUsed ? [{ label: "风险例外", className: "tag warn" }] : []),
    ...riskFlags.map((flag) => ({ label: flag, className: "tag warn" }))
  ];
  if (!tags.length) return `<span class="tag muted">--</span>`;
  return tags.map((tag) => `<span class="${tag.className}">${escapeHtml(tag.label)}</span>`).join("");
}

function renderTypeBars(counts) {
  const entries = orderedTypeEntries(counts);
  if (!entries.length) return `<div class="empty compact">暂无类型数据</div>`;
  const max = Math.max(...entries.map((entry) => entry.count), 1);
  return entries.map((entry) => `
    <div class="type-bar-row">
      <span>${escapeHtml(candidateTypeLabel(entry.type))}</span>
      <i><b style="width:${Math.max(4, Math.round(entry.count / max * 100))}%"></b></i>
      <strong>${formatInteger(entry.count)}</strong>
    </div>
  `).join("");
}

function orderedTypeEntries(counts) {
  const data = counts || {};
  const keys = new Set([...CANDIDATE_TYPE_ORDER, ...Object.keys(data)]);
  return [...keys]
    .map((type) => ({ type, count: Number(data[type] || 0) }))
    .filter((entry) => entry.count > 0)
    .sort((a, b) => {
      const order = typeOrder(a.type) - typeOrder(b.type);
      return order === 0 ? b.count - a.count : order;
    });
}

function typeOrder(type) {
  const index = CANDIDATE_TYPE_ORDER.indexOf(type);
  return index >= 0 ? index : CANDIDATE_TYPE_ORDER.length;
}

function formatTypeSummary(counts) {
  const entries = orderedTypeEntries(counts).slice(0, 3);
  if (!entries.length) return "--";
  return entries.map((entry) => `${candidateTypeLabel(entry.type)} ${formatInteger(entry.count)}`).join(" · ");
}

function compactEvidence(item) {
  const metrics = item.dailyMetrics || {};
  const tradeability = item.tradeability || {};
  const parts = [];
  if (Number.isFinite(Number(metrics.return20d))) parts.push(`20日 ${formatNumber(metrics.return20d)}%`);
  if (Number.isFinite(Number(metrics.distanceMa20))) parts.push(`距MA20 ${formatNumber(metrics.distanceMa20)}%`);
  if (Number.isFinite(Number(metrics.amountRatio5d))) parts.push(`5日量比 ${formatNumber(metrics.amountRatio5d)}`);
  if (Number.isFinite(Number(item.lastVwap))) parts.push(`VWAP ${formatNumber(item.lastVwap)}`);
  if (Number.isFinite(Number(item.morningHigh))) parts.push(`早高 ${formatNumber(item.morningHigh)}`);
  if (Number.isFinite(Number(item.volumeRatio))) parts.push(`量比 ${formatNumber(item.volumeRatio)}`);
  if (Number.isFinite(Number(item.amountFactor))) parts.push(`额因子 ${formatNumber(item.amountFactor)}`);
  if (Number.isFinite(Number(item.sectorRank))) parts.push(`板块#${formatInteger(item.sectorRank)}`);
  if (item.sectorState) parts.push(sectorStateLabel(item.sectorState));
  if (item.industrySector) parts.push(`行业 ${item.industrySector}`);
  if (Array.isArray(item.conceptSectors) && item.conceptSectors.length) parts.push(`概念 ${item.conceptSectors.slice(0, 2).join("/")}`);
  if (item.sectorEvidenceStatus && item.sectorEvidenceStatus !== "available") parts.push(sectorEvidenceLabel(item.sectorEvidenceStatus));
  if (tradeability.label) parts.push(tradeability.label);
  return parts.join(" · ") || "--";
}

function formatEvidenceList(value) {
  if (Array.isArray(value) && value.length) return value.join("；");
  const text = String(value || "").trim();
  return text || "--";
}

function sectorEvidenceLabel(value) {
  return {
    available: "板块证据可用",
    stale: "板块证据过期",
    proxy: "板块代理数据",
    unavailable: "板块证据缺失"
  }[value] || value || "--";
}

function sectorEvidenceText(item) {
  const status = item.sectorEvidenceStatus || item.sector_evidence_status || "";
  const note = item.sectorEvidenceNote || "";
  if (!status) return "--";
  return `${sectorEvidenceLabel(status)}${note ? `：${note}` : ""}`;
}

function sectorStateLabel(value) {
  return {
    rotation_in: "主动轮入",
    strong: "强势",
    overheated: "过热",
    neutral: "中性",
    weakening: "转弱",
    distribution: "派发",
    retreat: "退潮",
    oversold_repair: "超跌修复",
    data_insufficient: "板块不足"
  }[value] || value || "--";
}

function sectorStateText(item) {
  const parts = [];
  if (item.industrySector) parts.push(`行业 ${item.industrySector}`);
  if (item.conceptSectors?.length) parts.push(`概念 ${item.conceptSectors.slice(0, 2).join("/")}`);
  if (item.sectorState) parts.push(sectorStateLabel(item.sectorState));
  if (Number.isFinite(Number(item.sectorRiskScore))) parts.push(`风险 ${formatNumber(item.sectorRiskScore)}`);
  if (item.sectorFlowInterpretation) parts.push(flowLabel(item.sectorFlowInterpretation));
  return parts.join(" · ") || sectorEvidenceText(item);
}

function flowLabel(value) {
  return {
    active_inflow: "主动资金轮入",
    passive_defensive: "被动抗跌",
    outflow: "资金流出",
    unknown: "资金未知"
  }[value] || value || "--";
}

function styleRegimeLabel(value) {
  return {
    data_insufficient: "数据不足",
    sector_retreat: "局部退潮",
    sector_rotation: "风格轮动",
    rotation_in: "轮入增强",
    sector_strength: "板块走强",
    neutral: "中性"
  }[value] || value || "--";
}

function formatGroupList(values) {
  if (!Array.isArray(values) || !values.length) return "--";
  return values.slice(0, 3).join("/");
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
    triggerSource: "ai_full_market",
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
  const raw = run?.createdAt || run?.finishedAt || "";
  return String(raw).split("T")[0] || "";
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

function clampNumber(value, min, max, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(min, Math.min(max, Math.round(number)));
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

function formatSigned(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number > 0 ? "+" : ""}${formatNumber(number)}`;
}

function formatMaybePct(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${formatNumber(number)}%` : "--";
}

function numberClass(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "";
  return number > 0 ? "positive-text" : "negative-text";
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
