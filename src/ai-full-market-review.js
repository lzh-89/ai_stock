const els = {
  refreshButton: document.querySelector("#reviewRefreshButton"),
  notice: document.querySelector("#reviewNotice"),
  sampleText: document.querySelector("#reviewSampleText"),
  runText: document.querySelector("#reviewRunText"),
  returnText: document.querySelector("#reviewReturnText"),
  errorText: document.querySelector("#reviewErrorText"),
  missText: document.querySelector("#reviewMissText"),
  promptText: document.querySelector("#reviewPromptText"),
  learningSummaryText: document.querySelector("#learningSummaryText"),
  learningStateText: document.querySelector("#learningStateText"),
  learningReasonText: document.querySelector("#learningReasonText"),
  learningMessage: document.querySelector("#learningMessage"),
  learningRunButton: document.querySelector("#learningRunButton"),
  learningRows: document.querySelector("#learningRows"),
  typeRows: document.querySelector("#reviewTypeRows"),
  sectorStateRows: document.querySelector("#reviewSectorStateRows"),
  sectorExceptionText: document.querySelector("#sectorExceptionText"),
  runRows: document.querySelector("#reviewRunRows"),
  detailRows: document.querySelector("#reviewDetailRows")
};

els.refreshButton.addEventListener("click", loadReview);
els.learningRunButton.addEventListener("click", runLearning);
loadReview();

async function loadReview() {
  setLoading(true);
  try {
    const response = await fetch("/api/ai-full-market-selection/outcomes/review?limit=2000");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    renderReview(payload);
  } catch (error) {
    renderNotice(error.message || String(error));
  } finally {
    setLoading(false);
  }
}

function renderReview(data) {
  renderNotice(data.rowCount ? (data.tradePerformanceNote || "") : "暂无已完成的新版历史验证结果。当前推荐需要等待未来 T+1/T+3/T+5/T+7 行情回填后才会出现在这里。");
  els.sampleText.textContent = `${formatInteger(data.selectedCount)} / ${formatInteger(data.rejectedCount)}`;
  els.runText.textContent = `Run ${formatInteger(data.runCount)} · 7日样本 ${formatInteger(data.horizon7Count)} · 总行 ${formatInteger(data.rowCount)}`;
  els.returnText.textContent = data.selectedAvgReturnPct == null ? "--" : `${formatSigned(data.selectedAvgReturnPct)}%`;
  els.errorText.textContent = `超额 ${formatSigned(data.selectedAvgExcessPct)}% · 候选错买 ${formatInteger(data.falsePositiveCount)} · 交易未验证`;
  els.missText.textContent = formatInteger(data.missedOpportunityCount);
  els.promptText.textContent = `Prompt ${data.promptVersion || "--"}`;
  renderLearningStatus(data);
  renderTypeRows(data.byCandidateType || []);
  renderSectorStateRows(data.bySectorState || [], data.sectorExceptionStats || {});
  renderRunRows(data.byRun || []);
  renderDetailRows(data.rows || []);
}

async function runLearning() {
  setLearningLoading(true);
  try {
    const response = await fetch("/api/ai-full-market-selection/optimization/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ apply: true })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    const suggestion = payload.suggestion || {};
    const reason = suggestion.reason || "历史样本暂未给出新的参数建议";
    const selectedCount = payload.stats?.selectedCount ?? 0;
    const rejectedCount = payload.stats?.rejectedCount ?? 0;
    els.learningStateText.textContent = payload.applied ? "已更新" : "未更新";
    els.learningSummaryText.textContent = payload.applied ? "参数已更新" : "未达到更新条件";
    els.learningReasonText.textContent = reason;
    els.learningMessage.textContent = `${payload.applied ? "已根据历史样本更新 AI 全市场选股参数" : "本次没有更新参数"}：${reason}。样本 Top10 ${formatInteger(selectedCount)} · 对照 ${formatInteger(rejectedCount)}`;
    renderLearningRows(payload.suggestion || {}, payload.updatedConfig || payload.currentConfig || {});
  } catch (error) {
    els.learningStateText.textContent = "执行失败";
    els.learningReasonText.textContent = error.message || String(error);
    els.learningMessage.textContent = error.message || String(error);
  } finally {
    setLearningLoading(false);
  }
}

function renderLearningStatus(data) {
  const meta = data.learningMeta || {};
  const report = data.report || {};
  const suggestion = report.suggestion || {};
  const status = suggestion.sampleStatus || meta.sampleStatus || {};
  const thresholds = suggestion.thresholds || meta.thresholds || {};
  const horizon7Count = Number(data.horizon7Count || 0);
  const rowCount = Number(data.rowCount || 0);
  const completedRuns = Number(status.completedRuns || 0);
  const minRuns = Number(thresholds.minCompletedRuns || 12);
  if (!suggestion.allowApply) {
    els.learningSummaryText.textContent = `有效日 ${formatInteger(completedRuns)} / ${formatInteger(minRuns)}`;
    els.learningStateText.textContent = "等待样本";
    els.learningReasonText.textContent = suggestion.reason || meta.reason || "样本不足时不会更新参数。";
    els.learningMessage.textContent = `参数学习使用统计规则，不是 AI 自动改 Prompt。当前完成7日样本 ${formatInteger(horizon7Count)}，完成验证行 ${formatInteger(status.validatedOutcomes || rowCount)}。`;
  } else {
    els.learningSummaryText.textContent = `有效日 ${formatInteger(completedRuns)} / ${formatInteger(minRuns)}`;
    els.learningStateText.textContent = "可更新";
    els.learningReasonText.textContent = suggestion.reason || "发现可调整的策略权重。";
    els.learningMessage.textContent = "点击后只会在满足门槛时更新 AI 全市场选股的策略暴露权重；不会改 Prompt，也不会改其他功能。";
  }
  renderLearningRows(suggestion, data.config || {});
}

function renderLearningRows(suggestion, config) {
  const rows = (suggestion.parameterChanges && suggestion.parameterChanges.length)
    ? suggestion.parameterChanges
    : (suggestion.evaluatedTypes || []);
  const scaleMap = config.typeExposureScale || {};
  if (!rows.length) {
    const learned = Object.entries(scaleMap).filter(([, value]) => Math.abs(Number(value || 1) - 1) >= 0.001);
    els.learningRows.innerHTML = learned.length ? learned.map(([candidateType, scale]) => `
      <tr>
        <td>${escapeHtml(candidateTypeLabel(candidateType))}</td>
        <td>--</td>
        <td>--</td>
        <td>${formatNumber(scale)}x</td>
        <td>--</td>
        <td>当前已生效的历史学习权重；暂无新的可解释调整建议。</td>
      </tr>
    `).join("") : `<tr><td colspan="6" class="empty">暂无可更新参数。当前使用默认策略权重，AI 不参与自动调参。</td></tr>`;
    return;
  }
  els.learningRows.innerHTML = rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.label || candidateTypeLabel(row.candidateType))}<small>${escapeHtml(actionLabel(row.action))}</small></td>
      <td>${formatInteger(row.selectedCount)} 个Top10 7日样本</td>
      <td class="${numberClass(row.deltaPct)}">${formatSigned(row.deltaPct)}pct<small>本类 ${formatSigned(row.selectedAvgExcessPct)}% · 基线 ${formatSigned(row.baselineExcessPct)}%</small></td>
      <td>${formatNumber(row.oldScale)}x</td>
      <td>${formatNumber(row.newScale)}x</td>
      <td>${escapeHtml(row.reason || "--")}</td>
    </tr>
  `).join("");
}

function renderTypeRows(rows) {
  els.typeRows.innerHTML = rows.length ? rows.map((row) => `
    <tr>
      <td>${escapeHtml(candidateTypeLabel(row.candidateType))}</td>
      <td>${formatInteger(row.selectedCount)}</td>
      <td>${formatInteger(row.rejectedCount)}</td>
      <td class="${numberClass(row.selectedAvgReturnPct)}">${formatSigned(row.selectedAvgReturnPct)}%</td>
      <td class="${numberClass(row.selectedAvgExcessPct)}">${formatSigned(row.selectedAvgExcessPct)}%</td>
      <td>${formatInteger(row.falsePositiveCount)}<small>${formatMaybePct(row.falsePositiveRatePct)}</small></td>
      <td>${formatInteger(row.missedOpportunityCount)}<small>${formatMaybePct(row.missedRatePct)}</small></td>
    </tr>
  `).join("") : `<tr><td colspan="7" class="empty">暂无策略类型验证结果</td></tr>`;
}

function renderSectorStateRows(rows, exceptionStats) {
  els.sectorExceptionText.textContent = `风险例外 ${formatInteger(exceptionStats.sampleCount)} · 平均超额 ${formatSigned(exceptionStats.avgExcessPct)}% · 错买 ${formatInteger(exceptionStats.falsePositiveCount)}`;
  els.sectorStateRows.innerHTML = rows.length ? rows.map((row) => `
    <tr>
      <td>${escapeHtml(sectorStateLabel(row.key))}</td>
      <td>${formatInteger(row.selectedCount)}</td>
      <td>${formatInteger(row.rejectedCount)}</td>
      <td class="${numberClass(row.selectedAvgReturnPct)}">${formatSigned(row.selectedAvgReturnPct)}%</td>
      <td class="${numberClass(row.selectedAvgExcessPct)}">${formatSigned(row.selectedAvgExcessPct)}%</td>
      <td class="${numberClass(row.avgMaxDrawdownPct)}">${formatSigned(row.avgMaxDrawdownPct)}%</td>
      <td>${formatInteger(row.falsePositiveCount)}<small>${formatMaybePct(row.falsePositiveRatePct)}</small></td>
      <td>${formatInteger(row.missedOpportunityCount)}<small>${formatMaybePct(row.missedRatePct)}</small></td>
    </tr>
  `).join("") : `<tr><td colspan="8" class="empty">暂无板块状态验证结果</td></tr>`;
}

function renderRunRows(rows) {
  els.runRows.innerHTML = rows.length ? rows.map((row) => `
    <tr>
      <td>Run ${formatInteger(row.runId)}</td>
      <td>${escapeHtml(row.recommendationDate || "--")}</td>
      <td>${renderHorizon(row.horizons?.["1"])}</td>
      <td>${renderHorizon(row.horizons?.["3"])}</td>
      <td>${renderHorizon(row.horizons?.["5"])}</td>
      <td>${renderHorizon(row.horizons?.["7"])}</td>
      <td class="${numberClass(row.selectedAvgReturnPct)}">${formatSigned(row.selectedAvgReturnPct)}%</td>
      <td class="${numberClass(row.selectedAvgExcessPct)}">${formatSigned(row.selectedAvgExcessPct)}%</td>
    </tr>
  `).join("") : `<tr><td colspan="8" class="empty">暂无 Run 验证结果</td></tr>`;
}

function renderDetailRows(rows) {
  els.detailRows.innerHTML = rows.length ? rows.slice(0, 200).map((row) => `
    <tr>
      <td>${escapeHtml(row.recommendation_date || "--")}</td>
      <td><strong>${stockAnalysisNameLink(row)}</strong><small>Run ${formatInteger(row.run_id)} · AI#${formatInteger(row.ai_rank)}</small></td>
      <td>${escapeHtml(candidateTypeLabel(row.candidate_type || "other"))}</td>
      <td>${formatInteger(row.horizon)}日</td>
      <td>${Number(row.selected) ? "Top10" : "对照"}</td>
      <td>${escapeHtml(outcomeLabel(row.label))}</td>
      <td class="${numberClass(row.return_pct)}">${formatSigned(row.return_pct)}%</td>
      <td class="${numberClass(row.excess_return_pct)}">${formatSigned(row.excess_return_pct)}%</td>
    </tr>
  `).join("") : `<tr><td colspan="8" class="empty">暂无已完成验证明细</td></tr>`;
}

function renderHorizon(item) {
  if (!item) return "--";
  return `${formatInteger(item.selectedCount)}只 · ${formatSigned(item.selectedAvgReturnPct)}%`;
}

function setLoading(loading) {
  els.refreshButton.disabled = loading;
}

function setLearningLoading(loading) {
  els.learningRunButton.disabled = loading;
}

function renderNotice(message) {
  els.notice.hidden = !message;
  els.notice.textContent = message || "";
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

function outcomeLabel(value) {
  return {
    true_positive: "正确入选",
    weak_positive: "方向偏弱",
    false_positive: "错买",
    not_triggered: "未触发",
    missed_opportunity: "漏判",
    correct_reject: "正确拒绝",
    neutral_reject: "中性拒绝"
  }[value] || value || "--";
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
    data_insufficient: "板块不足",
    unknown: "未知"
  }[value] || value || "--";
}

function actionLabel(value) {
  return {
    increase: "建议提高",
    decrease: "建议降低",
    keep: "保持"
  }[value] || "评估";
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

function formatInteger(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? String(Math.round(number)) : "--";
}

function formatSigned(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number > 0 ? "+" : ""}${formatNumber(number)}`;
}

function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toFixed(1).replace(/\.0$/, "");
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

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
