/* Lazy Run archives. Requests are scoped to a generation to prevent cross-Run stale rendering. */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const pct = (value) => value == null ? "--" : `${Number(value).toFixed(2)}%`;
  const returnValue = (value) => `<span class="${value == null || Number(value) === 0 ? "" : Number(value) > 0 ? "proxy-positive" : "proxy-negative"}">${pct(value)}</span>`;
  const metric = (label, value) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`;
  const names = { all500:"全部宽召回", requested120:"分配15m验证", available15m:"15m真实可用", eligiblePool:"通过门禁", notRequested:"未分配15m", core:"核心", single:"单项突出", boundary:"边界分歧", coverage:"行业覆盖", explore:"探索" };
  const statuses = {not_planned:"未计划", planned:"已计划", requested:"已请求", available:"可用", partial:"部分可用", failed:"失败", quality_rejected:"门禁淘汰", stale:"过期", not_needed:"无需请求"};
  const taskNames = {pending_target:"等待目标日",waiting_close:"等待收盘",collecting:"采集中",retry_wait:"等待重试",partial:"部分缺失",complete:"已完整",terminal_missing:"补拉期结束",waiting_trade_date_confirmation:"等待交易日确认"};
  const stageNames = {"15m_round1_planned":"第一轮分配", "15m_round1_requested":"第一轮请求", "15m_round1_result":"第一轮结果", "15m_round2_planned":"第二轮分配", "15m_round2_requested":"第二轮请求", "15m_result":"15m门禁与评分", "ai_batch_requested":"AI批次请求", "ai_batch":"AI批次结果", "1m_planned":"尾盘影子分配", "1m_requested":"尾盘影子请求", "1m_result":"尾盘影子结果", decision:"最终去向"};
  const allocationReasons = {core:"低成本综合排序靠前",single:"量能或相对强度单项突出",boundary:"初筛边界或评分分歧",coverage:"补充行业覆盖",explore:"按冻结种子从不同排名区间探索"};
  const relaxNames = {"industry15%":"行业集中度15%上限", "candidateType30%":"类型集中度30%上限", "highRisk10%":"高风险占比10%上限",group_shortage:"分组候选不足"};
  const corporateNames = {unknown:"未知",none:"无",ex_right:"除权",ex_dividend:"除息",confirmed:"已确认",suspected:"疑似"};
  const sourceNames = {tencent:"腾讯行情",tencent_quote:"腾讯行情",eastmoney_daily:"东财日线",tencent_daily:"腾讯日线",eastmoney_daily_fqt0:"东财未复权日线",tencent_daily_unadjusted:"腾讯未复权日线"};
  const reasonText = (c) => `${allocationReasons[c.primaryGroup] || "未分配验证名额"}${c.allocationReason?.includes("shortage_substitution") ? "；分组不足，按稳定排名补位" : c.allocationRound === 2 ? "；结合第一轮真实评分和门禁反馈" : ""}`;
  let runId = 0, generation = 0, tableVersion = 0, detailVersion = 0, page = 1, total = 0;
  let pollTimer, archiveReady = false, archiveLoading = false, nextLoading = false;
  const api = async (suffix, options) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/api/t1-overnight-selection/runs/${runId}/${suffix}`, {...options, signal:controller.signal});
      const body = await response.json();
      if (!response.ok) throw new Error(body.error === "invalid run id" ? "后端尚未加载候选追踪接口，请重启后端后重试。" : body.error || `接口请求失败（${response.status}）`);
      return body;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求超时，请稍后重试。");
      if (error instanceof TypeError) throw new Error("无法连接后端，请检查服务后重试。");
      throw error;
    } finally { clearTimeout(timeout); }
  };
  const current = (g) => generation === g;
  async function loadArchive(g) {
    if (!runId || archiveLoading) return;
    archiveLoading = true;
    $("archiveReload").hidden = true;
    $("archiveMeta").textContent = "正在加载候选档案…";
    try {
      const archive = await api("archive-summary");
      if (!current(g)) return;
      archiveReady = !archive.legacy;
      $("archiveMeta").textContent = archive.legacy ? "旧版本未记录，不能倒推重建" : `${archive.total} 只 · ${archive.archiveVersion}`;
      $("archiveDetails").hidden = archive.legacy;
      $("archiveFunnel").innerHTML = archive.legacy ? "" : archive.rounds.map((r) => `<div class="archive-round"><strong>15m · 第${esc(r.round)}轮</strong><dl class="archive-metrics">${metric("计划", r.planned)}${metric("请求", r.requested)}${metric("真实可用", r.available)}${metric("通过门禁", r.eligible)}</dl></div>`).join("");
      loadPage();
    } catch (error) {
      if (current(g)) {
        archiveReady = false;
        $("archiveDetails").hidden = true;
        $("archiveFunnel").innerHTML = "";
        $("archiveMeta").textContent = `档案加载失败：${error.message}`;
        $("archiveReload").hidden = false;
      }
    } finally { if (current(g)) archiveLoading = false; }
  }
  async function loadNext(g) {
    if (!runId || nextLoading) return;
    nextLoading = true;
    $("nextDayReload").hidden = true;
    try {
      const next = await api("next-day-summary");
      if (!current(g)) return;
      renderNext(next);
      clearTimeout(pollTimer);
      if (!next.legacy && next.job && !["complete", "terminal_missing"].includes(next.job.status)) {
        pollTimer = setTimeout(() => { if (current(g)) { loadNext(g); loadPage(); } }, 30000);
      }
    } catch (error) {
      if (current(g)) {
        $("nextDayMeta").textContent = `次日效果加载失败：${error.message}`;
        $("nextDayReload").hidden = false;
        $("nextDayRetry").disabled = true;
        clearTimeout(pollTimer);
      }
    } finally { if (current(g)) nextLoading = false; }
  }
  function renderNext(next) {
    const job = next.job;
    $("nextDayRetry").disabled = next.legacy || !job || ["complete", "collecting"].includes(job.status);
    $("nextDayMeta").textContent = next.legacy ? "旧版本未保存500候选，不能用今天的规则重建历史。" : !job ? "档案已保存，Run收口后建立追踪任务。" :
      `${taskNames[job.status] || job.status}${job.last_error ? ` · ${job.last_error}` : ""}`;
    $("nextDayMetrics").innerHTML = next.legacy || !job ? "" : metric("目标交易日", job.target_trade_date || "待确认") + metric("行情完整率", pct(next.coveragePct)) + metric("已解析候选", `${job.resolved_count} / ${job.expected_count}`) + metric("最近更新", job.updated_at);
    $("nextDayNote").textContent = next.note || "";
    const collection = next.collection || {};
    const sources = Object.entries(next.dailySources || {}).map(([key, value]) => `${sourceNames[key] || key} ${value}只`).join("；");
    const failures = Object.entries(collection.failuresBySource || {}).map(([key, value]) => `${sourceNames[key] || key} ${value}次`).join("；");
    $("nextDayCollection").textContent = next.legacy || !job ? "" : `最近采集：请求 ${collection.requestCount ?? "--"}次 · 重试 ${collection.retryCount ?? "--"}次 · 耗时 ${collection.elapsedMs == null ? "--" : `${(collection.elapsedMs / 1000).toFixed(1)}秒`} · 来源 ${sources || "待取得"} · 失败 ${failures || "无"}`;
    $("nextDayCollectionDetails").hidden = next.legacy || !job;
    const coverage = next.top10Coverage || {};
    $("nextDayCoverage").innerHTML = Object.entries(coverage).map(([key, value]) => `<div class="archive-round"><strong>${key === "open" ? "开盘" : "收盘"}代理前 ${esc(value.count)}</strong><dl class="archive-metrics coverage-metrics">${metric("分配验证覆盖", pct(value.requestedCoveragePct))}${metric("合格覆盖", pct(value.eligibleCoveragePct))}</dl></div>`).join("") + (next.coverageIncomplete ? '<p class="archive-note coverage-warning">行情完整率不足95%，继续补齐；不生成配额调整建议。</p>' : "");
    const distribution = (r) => `均值 ${returnValue(r.meanPct)}<small>中位 ${pct(r.medianPct)}</small><small>P25 / P75 ${pct(r.p25Pct)} / ${pct(r.p75Pct)}</small>`;
    $("nextDayGroups").innerHTML = (next.groups || []).map((r) => `<tr><td><strong>${esc(names[r.group] || r.group)}</strong></td><td>${r.total} / ${r.close.sampleCount}<small>开盘有效 ${r.open.sampleCount ?? "--"}</small><small>缺失 ${r.missing} · 排除 ${r.excluded}</small></td><td>计划 ${r.planned} · 请求 ${r.requested} · 合格 ${r.eligible}<small>请求率 ${pct(r.requestRatePct)} · 门禁率 ${pct(r.gateRatePct)}</small></td><td>${distribution(r.open)}</td><td>${distribution(r.close)}</td><td>${returnValue(r.close.positiveProxyRatioPct)}</td></tr>`).join("") || `<tr><td colspan="6">${next.legacy ? "旧版本未记录候选，暂无对比数据。" : "等待有效次日日行情。"}</td></tr>`;
    $("nextDayMissed").innerHTML = (next.missedOpportunities || []).length ? '<h3>未分配验证的领先案例</h3><ul class="archive-missed-list">' + next.missedOpportunities.map((x) => `<li><div><strong>${esc(x.name)}</strong><small>${esc(x.code)} · 初始 #${esc(x.preliminaryRank)}</small></div>${returnValue(x.closeProxyReturnPct)}</li>`).join("") + "</ul>" : next.legacy ? "" : "暂无有效漏选案例。";
  }
  async function loadPage() {
    if (!runId || !archiveReady || !$("archiveDetails").open) return;
    const g = generation, version = ++tableVersion;
    const params = new URLSearchParams({page:String(page),pageSize:"50",query:$("archiveQuery").value,group:$("archiveGroup").value,stage:$("archiveStage").value,sort:$("archiveSort").value});
    $("archiveRows").innerHTML = '<tr><td colspan="8">正在加载候选…</td></tr>';
    try {
      const data = await api(`candidates?${params}`);
      if (!current(g) || version !== tableVersion) return;
      total = data.total;
      $("archiveRows").innerHTML = data.items.map((x) => {
        const decision = [x.officialSelected && "正式", x.ruleShadowSelected && "规则影子", x.aiShadowSelected && "AI影子", x.semifinalSelected && "半决赛"].filter(Boolean).join(" / ") || (x.qualityEligible ? "合格待选" : "研究观察");
        return `<tr><td><strong>${esc(x.name)}</strong><small>${esc(x.code)}</small></td><td>#${x.preliminaryScoreRank} / ${Number(x.preliminaryScore).toFixed(1)}</td><td>${esc(names[x.primaryGroup] || "未分配")}<small>${x.allocationRound ? `第${x.allocationRound}轮` : "--"}</small></td><td>${esc(statuses[x.minute15mStatus] || x.minute15mStatus)} / ${esc(statuses[x.minute1mStatus] || x.minute1mStatus)}</td><td>${esc(decision)}</td><td>${pct(x.nextDay?.openProxyReturnPct)}</td><td>${pct(x.nextDay?.closeProxyReturnPct)}</td><td><button type="button" class="secondary-button" data-candidate="${esc(x.code)}">查看</button></td></tr>`;
      }).join("") || '<tr><td colspan="8">无符合条件的候选。</td></tr>';
      $("archivePageMeta").textContent = `第 ${page} / ${Math.max(1, Math.ceil(total / 50))} 页 · ${total} 只`;
      $("archivePrev").disabled = page <= 1;
      $("archiveNext").disabled = page * 50 >= total;
    } catch (error) {
      if (current(g) && version === tableVersion) $("archiveRows").innerHTML = `<tr><td colspan="8">${esc(error.message)}</td></tr>`;
    }
  }
  window.addEventListener("t1-review-run", (event) => {
    const id = Number(event.detail.runId);
    runId = Number.isSafeInteger(id) && id > 0 ? id : 0;
    const g = ++generation;
    ++tableVersion;
    ++detailVersion;
    clearTimeout(pollTimer);
    archiveReady = archiveLoading = nextLoading = false;
    $("archiveDetails").hidden = true;
    $("archiveReload").hidden = $("nextDayReload").hidden = true;
    page = 1;
    $("archiveRows").innerHTML = "";
    $("archiveFunnel").innerHTML = "";
    $("nextDayGroups").innerHTML = "";
    $("nextDayCoverage").innerHTML = "";
    $("nextDayMetrics").innerHTML = "";
    $("nextDayCollectionDetails").hidden = true;
    $("nextDayNote").textContent = "";
    $("nextDayCollection").textContent = "";
    $("nextDayMissed").textContent = "";
    $("archiveEvidence").hidden = true;
    $("nextDayRetry").disabled = true;
    $("archiveMeta").textContent = runId ? "加载中…" : "未选择 Run";
    $("nextDayMeta").textContent = runId ? "加载中…" : "未选择 Run";
    if (runId) { loadArchive(g); loadNext(g); }
  });
  $("archiveReload").addEventListener("click", () => loadArchive(generation));
  $("nextDayReload").addEventListener("click", () => { $("nextDayMeta").textContent = "正在加载次日效果…"; loadNext(generation); });
  $("archiveDetails").addEventListener("toggle", loadPage);
  $("archiveFilters").addEventListener("submit", (event) => { event.preventDefault(); page = 1; loadPage(); });
  $("archivePrev").addEventListener("click", () => { if (page > 1) { page--; loadPage(); } });
  $("archiveNext").addEventListener("click", () => { if (page * 50 < total) { page++; loadPage(); } });
  $("archiveRows").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-candidate]");
    if (!button) return;
    const g = generation, version = ++detailVersion;
    try {
      const data = await api(`candidate/${encodeURIComponent(button.dataset.candidate)}`);
      if (!current(g) || version !== detailVersion) return;
      const c = data.candidate;
      $("archiveEvidence").hidden = false;
      const metric = c.nextDay || {};
      const daily = metric.dailyBar || {};
      $("archiveEvidence").innerHTML = `<h3>${esc(c.name)} ${esc(c.code)} · 冻结依据</h3><p>初筛分 ${esc(c.preliminaryScore)} · 冻结价 ${esc(c.frozenScanPrice)} · 行业 ${esc(c.industry || "身份缺失")}</p><p>分配：${esc(names[c.primaryGroup] || "未分配验证名额")} ${c.allocationRound ? `第${c.allocationRound}轮` : ""} · 依据 ${esc(reasonText(c))} · 约束放宽：${esc((c.relaxationReason || "").split(",").filter(Boolean).map((x) => relaxNames[x] || x).join("；") || "无")}</p><p>15m来源 ${esc(c.minute15mSource || "--")} · 有效点数 ${esc(c.minute15mPointCount ?? "--")} · 截止 ${esc(c.minute15mCutoff || "--")} · 真实评分 ${esc(c.verifiedScore ?? "--")} / 排名 ${esc(c.verifiedRank ?? "--")}</p><p>数据缺口：${esc(c.minute15mError || (c.dataQuality?.missing || []).join("；") || c.exclusionReason || "无")}</p><p>风险：${esc((c.riskFlags || []).join("；") || "--")}</p><p>次日 ${esc(metric.targetTradeDate || "待采集")} · 来源 ${esc(metric.dailyBarSource || "--")} · 开/高/低/收 ${[daily.open, daily.high, daily.low, daily.close].map((x) => esc(x ?? "--")).join(" / ")} · 除权状态 ${esc(corporateNames[metric.corporateActionStatus] || "未知")}</p><p>开盘/收盘代理 ${pct(metric.openProxyReturnPct)} / ${pct(metric.closeProxyReturnPct)} · 排除原因 ${esc((metric.exclusionReasons || []).join("；") || "无")}</p><p>最高机会 / 最低不利：${pct(metric.highOpportunityPct)} / ${pct(metric.lowAdversePct)} · 隔夜缺口 ${pct(metric.overnightGapPct)}</p><ol>${data.events.map((x) => `<li>${esc(x.createdAt)} · ${esc(stageNames[x.stage] || x.stage)} · ${esc(statuses[x.status] || stageNames[x.status] || x.status)}</li>`).join("")}</ol>`;
    } catch (error) { if (current(g) && version === detailVersion) { $("archiveEvidence").hidden = false; $("archiveEvidence").textContent = error.message; } }
  });
  $("nextDayRetry").addEventListener("click", async () => {
    const g = generation;
    $("nextDayRetry").disabled = true;
    try {
      const result = await api("backfill-next-day", {method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
      if (current(g)) { $("nextDayMeta").textContent = result.message; clearTimeout(pollTimer); pollTimer = setTimeout(() => loadNext(g), 3000); }
    } catch (error) { if (current(g)) { $("nextDayMeta").textContent = error.message; $("nextDayRetry").disabled = false; } }
  });
})();
