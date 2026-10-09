const state = {
  limit: 60,
  statusFilter: "reviewable",
  runId: 0,
  payload: null,
  loading: false,
  actionNotice: ""
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

init();

function init() {
  const params = new URLSearchParams(window.location.search);
  state.limit = clampNumber(params.get("limit"), 10, 200, 60);
  state.statusFilter = normalizeReviewFilter(params.get("reviewStatus") || params.get("status") || "reviewable");
  state.runId = clampNumber(params.get("runId"), 0, 999999, 0);
  els.limitInput.value = String(state.limit);
  els.statusSelect.value = state.statusFilter;
  bindEvents();
  loadReview();
}

function bindEvents() {
  els.refreshButton.addEventListener("click", () => {
    state.actionNotice = "";
    loadReview();
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
}

async function loadReview({ keepSelection = false } = {}) {
  setLoading(true);
  try {
    const params = new URLSearchParams({ limit: String(state.limit) });
    if (state.statusFilter) params.set("reviewStatus", state.statusFilter);
    if ((keepSelection || state.runId > 0) && state.runId > 0) params.set("runId", String(state.runId));
    const response = await fetch(`/api/t1-overnight-selection/review?${params.toString()}`);
    const payload = await response.json().catch(() => ({}));
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
    renderError(error);
  } finally {
    setLoading(false);
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
    metricCard("open_15m成功率", formatPct(stats.successRate, true), `强成功 ${formatPct(stats.strongSuccessRate, true)}`),
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
    metricCard("成功率", formatPct(primary.successRate, true), "open_15m 净收益大于 0 的比例")
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
        <td>${formatPct(row.successRate, true)}</td>
        <td>${formatPct(row.strongSuccessRate, true)}</td>
        <td>${formatPct(row.benchmarkWinRate, true)}</td>
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
    metricCard("验证进度", outcomeStatusLabel(run.outcomeStatus), outcomeStatusHint(run.outcomeStatus, run)),
    metricCard("市场状态", marketContext.label || "--", (run.market?.recommendationPolicy || {}).reason || run.error || "--")
  ].join("");
  renderUsageExplanation(run);
  renderPickSummary(run);
  renderShadow(run);
  renderOutcomes(run);
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
    els.pickSummary.innerHTML = `<span class="pick-pill">${escapeHtml(zeroPickReason(run) || "没有最终 Top3")}</span>`;
    return;
  }
  els.pickSummary.innerHTML = picks.map((pick, index) => `
    <a class="pick-pill" href="${escapeHtml(stockAnalysisHref(pick))}" title="打开个股分析">
      #${formatInteger(index + 1)} ${escapeHtml(pick.name || "--")} ${escapeHtml(pick.code || "")}
    </a>
  `).join("");
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
  if (!rulePicks.length && !aiPicks.length && !rows.length && !aiRows.length) {
    els.shadowPanel.innerHTML = `<section class="review-shadow-box"><strong>影子 Top3</strong><span>该 run 未记录影子推荐数据（旧 run 或生成时未启用影子链路）。</span></section>`;
    return;
  }
  const cards = [
    shadowReviewGroup("规则尾盘影子", rulePicks, rows),
    shadowReviewGroup("AI 尾盘影子", aiPicks, aiRows)
  ].filter(Boolean).join("");
  const finalReturn = outcome.final?.portfolioReturnPct;
  const shadowReturn = outcome.shadow?.portfolioReturnPct;
  const aiReturn = outcome.deepSeekTail?.portfolioReturnPct;
  els.shadowPanel.innerHTML = `
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
  `;
}

function shadowReturnCard(label, value, note, tone) {
  return `<div class="review-shadow-return ${tone}"><small>${escapeHtml(label)}</small><strong>${formatPct(value)}</strong><span>${escapeHtml(note)}</span></div>`;
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
    metricCard("最终均值", formatPct(final.avgNetReturnPct), `成功率 ${formatPct(final.successRate, true)}`),
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
  if (["usable", "official_usable", "research", "zero_pick"].includes(text)) return "reviewable";
  if (text === "learning") return "learnable";
  if (["pending_next_trade_day", "verifiable", "not_ready"].includes(text)) return "waiting";
  if (["problem", "data_insufficient"].includes(text)) return "partial";
  if (["legacy", "invalid", "unusable"].includes(text)) return "diagnostic";
  if (text === "rules_fallback") return "";
  if (["reviewable", "learnable", "partial", "waiting", "diagnostic", ""].includes(text)) return text;
  return "reviewable";
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
  window.history.replaceState(null, "", `?${params.toString()}`);
}

function clampNumber(value, min, max, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(min, Math.min(max, Math.trunc(number)));
}

function formatPct(value, alreadyPct = false) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  const normalized = alreadyPct ? number : number;
  return `${normalized > 0 ? "+" : ""}${normalized.toFixed(2).replace(/\.00$/, "")}%`;
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
