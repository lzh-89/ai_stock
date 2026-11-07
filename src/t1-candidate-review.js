/* Lazy Run archives. Requests are scoped to a generation to prevent cross-Run stale rendering. */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const pct = (value) => value == null ? "--" : `${Number(value).toFixed(2)}%`;
  const returnValue = (value) => `<span class="${value == null || Number(value) === 0 ? "" : Number(value) > 0 ? "proxy-positive" : "proxy-negative"}">${pct(value)}</span>`;
  const metric = (label, value) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`;
  const names = { all500:"全部宽召回", requested120:"分配15m验证", available15m:"15m真实可用", eligiblePool:"数据合格", notRequested:"未分配15m", core:"核心", single:"单项突出", boundary:"边界分歧", coverage:"行业覆盖", explore:"探索", hardFilterEligible:"V3硬过滤后全量", recall500:"V3召回500", validation120:"V3验证120", dataEligible:"V3数据合格", tailCalibration30:"V3尾盘/校准30", shortlist:"V3短名单", selected:"V3最终组合" };
  const statuses = {not_planned:"未计划", planned:"已计划", requested:"已请求", available:"可用", partial:"部分可用", failed:"技术失败", quality_rejected:"数据质量不合格", stale:"过期", not_needed:"无需请求"};
  const taskNames = {pending_target:"等待目标日",waiting_close:"等待收盘",collecting:"采集中",retry_wait:"等待重试",partial:"部分缺失",complete:"已完整",terminal_missing:"补拉期结束",waiting_trade_date_confirmation:"等待交易日确认"};
  const stageNames = {"15m_round1_planned":"第一轮分配", "15m_round1_requested":"第一轮请求", "15m_round1_result":"第一轮结果", "15m_round2_planned":"第二轮分配", "15m_round2_requested":"第二轮请求", "15m_result":"15m数据质量与评分", setup_validation:"交易形态验证", formal_tail_validation:"正式尾盘资格", "ai_batch_requested":"AI批次请求", "ai_batch":"AI批次结果", "1m_planned":"尾盘证据分配", "1m_requested":"尾盘证据请求", "1m_result":"尾盘证据结果", decision:"最终去向"};
  const allocationReasons = {core:"低成本综合排序靠前",single:"量能或相对强度单项突出",boundary:"初筛边界或评分分歧",coverage:"补充行业覆盖",explore:"按冻结种子从不同排名区间探索"};
  const relaxNames = {"industry15%":"行业集中度15%上限", "candidateType30%":"类型集中度30%上限", "highRisk10%":"高风险占比10%上限",group_shortage:"分组候选不足"};
  const corporateNames = {unknown:"未知",none:"无",ex_right:"除权",ex_dividend:"除息",confirmed:"已确认",suspected:"疑似"};
  const sourceNames = {tencent:"腾讯行情",tencent_quote:"腾讯行情",eastmoney_daily:"东财日线",tencent_daily:"腾讯日线",eastmoney_daily_fqt0:"东财未复权日线",tencent_daily_unadjusted:"腾讯未复权日线"};
  const identityNames = {legacy_semifinal:"半决赛候选",official_pick:"正式推荐",rule_shadow:"规则影子",ai_shadow:"AI影子",v3_shadow_pick:"V3影子组合",tail_calibration_sample:"尾盘校准样本"};
  const leaderNames = {all:"全部领先案例",open_premium_leader:"开盘溢价领先",intraday_close_leader:"收盘／盘中拉升",hard_to_buy:"推荐日难买",high_risk:"高风险",normal_tradeable_miss:"正常可交易漏选",tradeability_unknown:"交易性未知"};
  const terminalPresentations = {
    hard_filter_rejected:{result:"未进入正式推荐",stop:"基础交易条件检查",badge:"基础条件未通过",tone:"warning",step:0,stepText:"未通过",summary:"推荐日的基础范围、价格、交易资格或其他硬条件未通过，因此没有进入候选池。这是业务筛选结果，不是系统故障。"},
    pool_not_recalled:{result:"未进入正式推荐",stop:"重点候选召回",badge:"未进入候选500",tone:"neutral",step:0,stepText:"未进入",summary:"基础条件合格，但综合排序没有进入重点观察的500只候选。这表示召回名额未覆盖，不等于系统故障。"},
    validation_not_allocated:{result:"未进入正式推荐",stop:"15分钟验证名额分配",badge:"验证名额未覆盖",tone:"neutral",step:1,stepText:"未分配",summary:"已经进入重点候选池，但没有获得本轮分钟验证名额。这表示验证预算未覆盖，不等于交易条件不合格。"},
    minute15m_technical_failure:{result:"本次无法完成筛选",stop:"15分钟行情获取",badge:"技术异常",tone:"danger",step:1,stepText:"技术异常",summary:"分钟行情请求或依赖链路失败，后续条件无法判断。它属于系统或数据链路问题，不能当成策略主动淘汰。"},
    minute15m_quality_rejected:{result:"未进入正式推荐",stop:"15分钟数据质量检查",badge:"数据不合格",tone:"danger",step:1,stepText:"数据不足",summary:"分钟行情已经返回，但完整性、日期、点数或截止时间不满足判断要求。它属于数据质量问题，不代表交易形态本身不合格。"},
    model_observation_only:{result:"暂不形成推荐结论",stop:"净期望模型准备",badge:"模型收集中",tone:"info",step:2,stepText:"仅观察",summary:"数据链路正常，但模型样本还不足以形成稳定排序。本次只收集证据，不把观察结果当成正式推荐。"},
    tail_not_allocated:{result:"未进入正式推荐",stop:"尾盘校准名额分配",badge:"尾盘名额未覆盖",tone:"neutral",step:3,stepText:"未分配",summary:"已经通过前序检查，但没有获得尾盘1分钟证据的校准名额，因此没有继续形成组合结论。"},
    minute1m_technical_failure:{result:"本次无法完成筛选",stop:"尾盘1分钟行情获取",badge:"技术异常",tone:"danger",step:3,stepText:"技术异常",summary:"尾盘1分钟行情请求或依赖失败，无法完成尾盘确认。它属于系统或数据链路问题，不能当成业务淘汰。"},
    tail_negative_rejected:{result:"未进入正式推荐",stop:"尾盘证据确认",badge:"尾盘走弱",tone:"warning",step:3,stepText:"未通过",summary:"尾盘真实价格或承接证据出现走弱信号，因此没有继续进入组合。这是业务筛选结果，不是系统故障。"},
    event_risk_rejected:{result:"未进入正式推荐",stop:"事件风险检查",badge:"事件风险阻断",tone:"warning",step:3,stepText:"风险阻断",summary:"决策截止前出现高风险事件，正式推荐被风险规则阻断。这是主动风险控制结果。"},
    non_positive_expectation:{result:"未进入正式推荐",stop:"保守净期望检查",badge:"预期收益不足",tone:"warning",step:3,stepText:"未通过",summary:"模型已经具备排序条件，但扣除风险和成本后的保守净期望不为正，因此没有继续进入组合。"},
    tradeability_rejected:{result:"未进入正式推荐",stop:"推荐日可交易性检查",badge:"当日难以执行",tone:"warning",step:3,stepText:"不可交易",summary:"推荐日的买入可执行性不满足要求，因此没有进入组合。这是交易执行约束，不是系统故障。"},
    shortlist_not_portfolio:{result:"未进入正式推荐",stop:"最终组合构建",badge:"未获组合席位",tone:"neutral",step:4,stepText:"未入组合",summary:"已经进入短名单，但受当日持仓上限、席位或组合分散约束，没有进入最终组合。"},
    v3_shadow_selected:{result:"已进入V3影子组合",stop:"V3影子组合",badge:"影子入选",tone:"info",step:4,stepText:"影子入选",final:true,summary:"该股票进入V3影子组合，用于前向验证；在策略正式获批前，它不等于正式推荐。"},
    legacy_validation_not_allocated:{result:"未进入正式推荐",stop:"15分钟验证名额分配",badge:"验证名额未覆盖",tone:"neutral",step:1,stepText:"未分配",summary:"已经进入当时的500只候选池，但没有获得本轮分钟验证名额。这表示验证预算未覆盖，不等于交易条件不合格。"},
    legacy_minute15m_technical_failure:{result:"本次无法完成筛选",stop:"15分钟行情获取",badge:"技术异常",tone:"danger",step:1,stepText:"技术异常",summary:"分钟行情请求失败，后续条件无法判断。它属于系统或数据链路问题，不能当成策略主动淘汰或主动空仓。"},
    legacy_minute15m_quality_rejected:{result:"未进入正式推荐",stop:"15分钟数据质量检查",badge:"数据不合格",tone:"danger",step:1,stepText:"数据不足",summary:"分钟行情已经返回，但质量不足以支持后续判断。它属于数据质量问题，不代表交易形态本身不合格。"},
    legacy_setup_rejected:{result:"未进入正式推荐",stop:"交易形态确认",badge:"业务筛选",tone:"warning",step:2,stepText:"形态未成立",summary:"系统拿到了可用的15分钟数据，但当时没有确认可用于继续晋级的交易形态。这是业务筛选结果，不是系统故障。"},
    legacy_ai_batch_rejected:{result:"未进入正式推荐",stop:"AI批次筛选",badge:"未晋级",tone:"warning",step:3,stepText:"未晋级",summary:"交易形态已经成立，但在同批候选比较中没有进入下一轮。这是候选排序结果，不是系统故障。"},
    legacy_tail_rejected:{result:"未进入正式推荐",stop:"尾盘证据确认",badge:"尾盘确认不足",tone:"warning",step:3,stepText:"未通过",summary:"已经进入半决赛候选，但尾盘真实证据没有满足正式推荐要求，因此停止晋级。"},
    legacy_final_not_selected:{result:"未进入正式推荐",stop:"AI最终选择",badge:"终选未入选",tone:"neutral",step:3,stepText:"未入选",summary:"已经通过尾盘资格，但AI最终选择没有把它纳入正式候选。"},
    legacy_guardrail_rejected:{result:"未进入正式推荐",stop:"最终风险护栏",badge:"护栏阻断",tone:"warning",step:4,stepText:"风险阻断",summary:"AI曾经选中该股票，但市场、数据、事件或隔夜风险护栏阻止了正式发布。这是主动风险控制结果。"},
    legacy_official_selected:{result:"已进入正式推荐",stop:"正式推荐",badge:"正式入选",tone:"success",step:4,stepText:"已入选",final:true,summary:"该股票通过了当时全部筛选与风险检查，并进入本 Run 的正式推荐。"},
    legacy_stage_unknown:{result:"暂时无法判断筛选结果",stop:"历史层级还原",badge:"历史信息不足",tone:"neutral",step:0,stepText:"无法还原",summary:"旧记录缺少还原筛选链路所需的字段，系统没有猜测它被哪一层淘汰。"},
    stage_unknown:{result:"暂时无法判断筛选结果",stop:"筛选层级识别",badge:"信息不足",tone:"neutral",step:0,stepText:"无法判断",summary:"当前记录缺少确认最终环节所需的字段，系统不会把信息缺失猜测成业务淘汰。"}
  };
  const reasonText = (c) => `${allocationReasons[c.primaryGroup] || "未分配验证名额"}${c.allocationReason?.includes("shortage_substitution") ? "；分组不足，按稳定排名补位" : c.allocationRound === 2 ? "；结合第一轮真实评分和门禁反馈" : ""}`;
  const fixed = (value, digits = 1) => value == null || value === "" || !Number.isFinite(Number(value)) ? "--" : Number(value).toFixed(digits);
  const price = (value) => value == null || value === "" || !Number.isFinite(Number(value)) ? "--" : Number(value).toFixed(2);
  const readableTime = (value) => value ? String(value).replace("T", " ").replace(/Z$/, "").slice(0, 16) : "--";
  const sourceLabel = (value) => sourceNames[value] || ({tencent_minute:"腾讯分钟行情",eastmoney_minute:"东财分钟行情"})[value] || value || "--";
  const presentationFor = (candidate) => terminalPresentations[candidate.terminalStage] || {
    result:(candidate.identityTags || []).includes("official_pick") ? "已进入正式推荐" : "未进入正式推荐",
    stop:candidate.terminalStageLabel || "筛选状态待确认", badge:"筛选结果", tone:"neutral", step:0, stepText:"已记录",
    summary:candidate.terminalReason || "已记录筛选结果，详细原因请查看下方审计信息。"
  };
  let runId = 0, generation = 0, tableVersion = 0, detailVersion = 0, page = 1, total = 0;
  let currentNext = null, leaderFilter = "all", leaderSort = "default", leaderPage = 1, leaderPageSize = 20;
  let detailTrigger = null;
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
  function openCandidateDetail(trigger) {
    detailTrigger = trigger || null;
    $("candidateDetailTitle").textContent = "正在加载候选详情";
    $("archiveEvidence").innerHTML = '<p class="archive-note">正在读取冻结依据与筛选过程…</p>';
    $("candidateDetailModal").hidden = false;
    document.body?.classList?.add("candidate-detail-open");
    $("candidateDetailClose").focus?.();
  }
  function closeCandidateDetail({restoreFocus = true, cancelRequest = true} = {}) {
    if (cancelRequest) ++detailVersion;
    $("candidateDetailModal").hidden = true;
    document.body?.classList?.remove("candidate-detail-open");
    if (restoreFocus) detailTrigger?.focus?.();
    detailTrigger = null;
  }
  async function loadArchive(g) {
    if (!runId || archiveLoading) return;
    archiveLoading = true;
    $("archiveReload").hidden = true;
    $("archiveMeta").textContent = "正在加载候选档案…";
    try {
      const archive = await api("archive-summary");
      if (!current(g)) return;
      archiveReady = !archive.legacy;
      $("archiveMeta").textContent = archive.legacy ? "旧版本未记录，不能倒推重建" : `${archive.total} 只 · 旧版500池内部复盘 · ${archive.archiveVersion}`;
      $("archiveDetails").hidden = archive.legacy;
      $("archiveFunnel").innerHTML = archive.legacy ? "" : archive.rounds.map((r) => `<div class="archive-round"><strong>15m · 第${esc(r.round)}轮</strong><dl class="archive-metrics">${metric("计划", r.planned)}${metric("请求", r.requested)}${metric("真实可用", r.available)}${metric("数据合格", r.eligible)}${archive.selectionPolicyVersion && archive.selectionPolicyVersion !== "legacy" ? metric("形态成立", r.setupEligible) + metric("正式证据通过", r.formalEligible) : ""}</dl></div>`).join("");
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
    currentNext = next;
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
    const v3Rows = (next.v3?.groups || []).map((r) => `<tr><td><strong>${esc(names[r.stage] || r.stage)}</strong><small>V3全链路</small></td><td>${r.total} / ${r.closeProxy.sampleCount}<small>开盘有效 ${r.openProxy.sampleCount ?? "--"}</small><small>缺失 ${r.missing}</small></td><td>该层唯一股票 ${r.total}<small>代理收益只用于漏斗诊断</small></td><td>${distribution(r.openProxy)}</td><td>${distribution(r.closeProxy)}</td><td>${returnValue(r.closeProxy.positiveProxyRatioPct)}</td></tr>`).join("");
    const legacyRows = (next.groups || []).map((r) => `<tr><td><strong>${esc(names[r.group] || r.group)}</strong></td><td>${r.total} / ${r.close.sampleCount}<small>开盘有效 ${r.open.sampleCount ?? "--"}</small><small>缺失 ${r.missing} · 排除 ${r.excluded}</small></td><td>计划 ${r.planned} · 请求 ${r.requested} · 可用 ${r.available} · 质量合格 ${r.eligible}<small>验证分配覆盖 ${pct(r.allocationCoveragePct)} · 请求执行完成 ${pct(r.requestCompletionPct)}</small><small>数据返回可用 ${pct(r.dataAvailabilityPct)} · 数据质量合格 ${pct(r.dataQualityPassPct)}</small><small>形态成立 ${pct(r.setupPassPct)} · AI晋级 ${pct(r.semifinalAdvancePct)} · 正式入选 ${pct(r.officialSelectionPct)}</small></td><td>${distribution(r.open)}</td><td>${distribution(r.close)}</td><td>${returnValue(r.close.positiveProxyRatioPct)}</td></tr>`).join("");
    $("nextDayV3Section").hidden = !(next.v3?.recorded);
    $("nextDayV3Scope").textContent = next.v3?.recorded ? `V3全市场轻量事实 ${next.v3.fullMarketFactCount} 只；各阶段收益只用于漏斗诊断。` : "该 Run 未记录 V3 全市场事实，不能按 V3 阶段统计。";
    $("nextDayV3Groups").innerHTML = v3Rows || '<tr><td colspan="6">等待有效V3阶段数据。</td></tr>';
    $("nextDayLegacySection").hidden = !(next.groups || []).length;
    $("nextDayLegacyGroups").innerHTML = legacyRows || '<tr><td colspan="6">该 Run 没有旧版候选档案。</td></tr>';
    renderLeaders();
  }
  function renderLeaders() {
    const next = currentNext || {};
    const rows = next.leaderCases || next.missedOpportunities || [];
    const counts = next.leaderSummary || {};
    const filterCount = (key) => key === "all" ? counts.uniqueCount ?? rows.length : ["open_premium_leader","intraday_close_leader"].includes(key) ? (counts.performanceTagCounts || {})[key] || 0 : (counts.businessConclusionCounts || {})[key] || 0;
    const visible = rows.filter((item) => leaderFilter === "all" || (item.leaderPerformanceTags || []).includes(leaderFilter) || item.leaderBusinessConclusion === leaderFilter).slice();
    const numeric = (item, key, fallback) => item[key] == null ? fallback : Number(item[key]);
    if (leaderSort === "open") visible.sort((a, b) => numeric(b, "openProxyReturnPct", -Infinity) - numeric(a, "openProxyReturnPct", -Infinity) || String(a.code).localeCompare(String(b.code)));
    if (leaderSort === "close") visible.sort((a, b) => numeric(b, "closeProxyReturnPct", -Infinity) - numeric(a, "closeProxyReturnPct", -Infinity) || String(a.code).localeCompare(String(b.code)));
    if (leaderSort === "high") visible.sort((a, b) => numeric(b, "highOpportunityPct", -Infinity) - numeric(a, "highOpportunityPct", -Infinity) || String(a.code).localeCompare(String(b.code)));
    if (leaderSort === "rank") visible.sort((a, b) => numeric(a, "preliminaryRank", Infinity) - numeric(b, "preliminaryRank", Infinity) || String(a.code).localeCompare(String(b.code)));
    const totalPages = Math.max(1, Math.ceil(visible.length / leaderPageSize));
    leaderPage = Math.min(Math.max(1, leaderPage), totalPages);
    const pageRows = visible.slice((leaderPage - 1) * leaderPageSize, leaderPage * leaderPageSize);
    const filters = ["all","open_premium_leader","intraday_close_leader","hard_to_buy","high_risk","normal_tradeable_miss","tradeability_unknown"];
    const buttons = `<div class="archive-actions leader-filter-actions">${filters.map((key) => `<button type="button" class="secondary-button${leaderFilter === key ? " active" : ""}" data-leader-filter="${key}">${esc(leaderNames[key])} ${filterCount(key)}</button>`).join("")}<label class="leader-sort-label">案例排序<select data-leader-sort aria-label="领先案例排序"><option value="default"${leaderSort === "default" ? " selected" : ""}>业务结论优先</option><option value="open"${leaderSort === "open" ? " selected" : ""}>开盘代理降序</option><option value="close"${leaderSort === "close" ? " selected" : ""}>收盘代理降序</option><option value="high"${leaderSort === "high" ? " selected" : ""}>盘中最高降序</option><option value="rank"${leaderSort === "rank" ? " selected" : ""}>当时排名升序</option></select></label><label class="leader-sort-label">每页显示<select data-leader-page-size aria-label="领先案例每页数量"><option value="20"${leaderPageSize === 20 ? " selected" : ""}>20只</option><option value="50"${leaderPageSize === 50 ? " selected" : ""}>50只</option><option value="100"${leaderPageSize === 100 ? " selected" : ""}>100只</option></select></label></div>`;
    const scope = (next.dataScope || {}).reviewScope === "v3_full_market" ? "V3全市场范围" : "旧版500池内部；无法判断全市场到500的召回漏损";
    const body = visible.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>股票 / 当时范围</th><th>表现标签</th><th>业务结论</th><th>被筛掉层级 / 原因</th><th>开盘 / 收盘代理</th><th>盘中最高机会</th></tr></thead><tbody>${pageRows.map((item) => `<tr><td><strong>${esc(item.name)}</strong><small>${esc(item.code)} · 当时排名 ${item.preliminaryRank ? `#${esc(item.preliminaryRank)}` : "未进召回500"}</small><small>${esc(item.reviewScope === "v3_full_market" ? "V3全市场" : "旧版500池")}</small></td><td>${(item.leaderPerformanceTags || []).map((tag) => `<span>${esc(leaderNames[tag] || tag)}</span>`).join(" / ") || "未进入开盘/收盘前10"}</td><td><strong>${esc(leaderNames[item.leaderBusinessConclusion] || item.leaderBusinessConclusion || "--")}</strong><small>${esc((item.leaderClassificationReasons || []).join("；") || "--")}</small></td><td><strong>${esc(item.terminalStageLabel || item.terminalStage || "层级未知")}</strong><small>${esc(item.terminalReason || item.exclusionReason || "未记录具体原因")}</small><small>规则 ${esc(item.terminalRuleVersion || "--")}</small></td><td>${returnValue(item.openProxyReturnPct)} / ${returnValue(item.closeProxyReturnPct)}</td><td>${returnValue(item.highOpportunityPct)}<small>事后最高价，非可实现收益</small></td></tr>`).join("")}</tbody></table></div>` : '<p class="archive-note">当前筛选下暂无符合条件的正机会代理案例。</p>';
    const pagination = `<nav class="leader-pagination" aria-label="领先案例分页"><button type="button" class="secondary-button" data-leader-page="prev"${leaderPage <= 1 ? " disabled" : ""}>上一页</button><span>当前筛选共 ${visible.length} 只 · 第 ${leaderPage}／${totalPages} 页</span><button type="button" class="secondary-button" data-leader-page="next"${leaderPage >= totalPages ? " disabled" : ""}>下一页</button></nav>`;
    $("nextDayMissed").innerHTML = `<h3>正机会代理但未分配验证的领先案例</h3><p class="archive-note">${esc(next.leaderDefinition || "等待完整次日数据。")}</p><p class="archive-note">范围：${esc(scope)}。开盘/盘中是表现标签，难买/高风险/正常可交易是业务结论，标签数量可能重叠。</p>${buttons}${body}${pagination}`;
  }
  async function loadPage() {
    if (!runId || !archiveReady || !$("archiveDetails").open) return;
    const g = generation, version = ++tableVersion;
    const params = new URLSearchParams({page:String(page),pageSize:"50",query:$("archiveQuery").value,group:$("archiveGroup").value,round:$("archiveRound").value,stage:$("archiveStage").value,identity:$("archiveIdentity").value,sort:$("archiveSort").value});
    $("archiveRows").innerHTML = '<tr><td colspan="8">正在加载候选…</td></tr>';
    try {
      const data = await api(`candidates?${params}`);
      if (!current(g) || version !== tableVersion) return;
      total = data.total;
      $("archiveRows").innerHTML = data.items.map((x) => {
        const identities = (x.identityTags || []).map((tag) => identityNames[tag] || tag).join(" / ") || "无额外身份";
        return `<tr><td><strong>${esc(x.name)}</strong><small>${esc(x.code)}</small></td><td>#${x.preliminaryScoreRank} / ${Number(x.preliminaryScore).toFixed(1)}</td><td>${esc(names[x.primaryGroup] || "未分配验证名额")}<small>${x.allocationRound ? `第${x.allocationRound}轮` : "未分配轮次"}</small></td><td>${esc(statuses[x.minute15mStatus] || x.minute15mStatus)} / ${esc(statuses[x.minute1mStatus] || x.minute1mStatus)}</td><td><strong>${esc(x.terminalStageLabel || "层级未知")}</strong><small>${esc(identities)}</small></td><td>${pct(x.nextDay?.openProxyReturnPct)}</td><td>${pct(x.nextDay?.closeProxyReturnPct)}</td><td><button type="button" class="secondary-button" data-candidate="${esc(x.code)}">查看</button></td></tr>`;
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
    currentNext = null;
    leaderFilter = "all";
    leaderSort = "default";
    leaderPage = 1;
    $("archiveRows").innerHTML = "";
    $("archiveFunnel").innerHTML = "";
    $("nextDayV3Groups").innerHTML = "";
    $("nextDayLegacyGroups").innerHTML = "";
    $("nextDayV3Section").hidden = true;
    $("nextDayLegacySection").hidden = true;
    $("nextDayCoverage").innerHTML = "";
    $("nextDayMetrics").innerHTML = "";
    $("nextDayCollectionDetails").hidden = true;
    $("nextDayNote").textContent = "";
    $("nextDayCollection").textContent = "";
    $("nextDayMissed").textContent = "";
    closeCandidateDetail({restoreFocus:false, cancelRequest:false});
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
    openCandidateDetail(button);
    try {
      const data = await api(`candidate/${encodeURIComponent(button.dataset.candidate)}`);
      if (!current(g) || version !== detailVersion) return;
      const c = data.candidate;
      $("candidateDetailTitle").textContent = `${c.name || "候选股票"} ${c.code || ""} · 候选详情`.trim();
      const metric = c.nextDay || {};
      const daily = metric.dailyBar || {};
      const presentation = presentationFor(c);
      const identities = (c.identityTags || []).map((tag) => identityNames[tag] || tag);
      const typeLabels = {relative_strength_leader:"相对强势延续",vwap_support:"回踩承接",breakout_hold:"突破维持",low_reversal_intraday:"日内转强"};
      const candidateTypes = (c.verifiedCandidateTypes || []).map((kind) => typeLabels[kind] || kind);
      const missingItems = [c.minute15mError, ...(c.dataQuality?.missing || [])].filter(Boolean);
      if (!missingItems.length && String(c.terminalStage || "").includes("quality_rejected") && c.terminalReason) missingItems.push(c.terminalReason);
      const dataGap = missingItems.join("；") || "无已知数据缺口";
      const shapeStatus = c.setupEligible === true ? `已确认${candidateTypes.length ? `：${candidateTypes.join("、")}` : "可继续晋级的交易形态"}` : c.setupEligible === false ? "未确认可继续晋级的交易形态" : "未进入或未记录形态检查";
      const formalReasons = c.formalExclusionReasons || [];
      const formalStatus = c.formalEligible === true ? "正式证据已通过" : c.setupEligible === false ? "未进入正式证据检查（前一环节已停止）" : formalReasons.length ? `正式证据未通过：${formalReasons.join("；")}` : "正式证据尚未检查或未单独记录";
      const tailStatus = c.setupEligible === false ? "未进入尾盘检查（前一环节已停止）" : c.tailSupportPack?.label || "尾盘证据未单独记录";
      const minuteStatus = `${statuses[c.minute15mStatus] || c.minute15mStatus || "未记录"}${c.minute15mPointCount == null ? "" : ` · ${c.minute15mPointCount}个有效点`}`;
      const pathLabels = ["进入候选池", "分钟数据验证", "交易形态确认", "AI／尾盘筛选", "正式推荐结果"];
      const path = pathLabels.map((label, index) => {
        const state = index < presentation.step || (presentation.final && index === presentation.step) ? "done" : index === presentation.step ? (presentation.tone === "danger" ? "error" : "current") : "pending";
        const stateText = state === "done" ? "已完成" : state === "pending" ? "未进入" : presentation.stepText;
        return `<li class="${state}"><span class="candidate-stage-dot">${index + 1}</span><div><strong>${esc(label)}</strong><small>${esc(stateText)}</small></div></li>`;
      }).join("");
      const scoreComponents = Object.entries(c.scoreComponents || {}).map(([key,value]) => `${({liquidity:"成交规模",relativeStrength:"日内相对强度",structure:"结构",industry:"真实行业支持",tail:"尾盘承接"})[key] || key}：${value == null ? "未知" : fixed(value)}`).join("；") || "未单独记录";
      const eventStatusNames = {setup_rejected:"形态未成立",ai_batch_rejected:"未晋级",tail_rejected:"尾盘未通过",final_not_selected:"终选未入选",guardrail_rejected:"护栏阻断",official_selected:"正式入选"};
      const eventRows = (data.events || []).map((item) => {
        const status = item.status === item.stage ? "已记录" : statuses[item.status] || eventStatusNames[item.status] || "已记录";
        return `<li><time>${esc(readableTime(item.createdAt))}</time><span>${esc(stageNames[item.stage] || item.stage || "过程记录")}</span><strong>${esc(status)}</strong></li>`;
      }).join("") || "<li><span>没有单独保存过程事件。</span></li>";
      const identityChips = identities.length ? identities.map((item) => `<span>${esc(item)}</span>`).join("") : "<span>无额外身份</span>";
      const proxyCard = (label, value, note) => `<div><span>${esc(label)}</span><strong>${returnValue(value)}</strong><small>${esc(note)}</small></div>`;
      $("archiveEvidence").innerHTML = `
        <section class="candidate-result-card ${esc(presentation.tone)}">
          <div class="candidate-result-kicker"><span>本次筛选结果</span><strong>${esc(presentation.badge)}</strong></div>
          <h3>${esc(presentation.result)}</h3>
          <div class="candidate-result-stop"><span>止步环节</span><strong>${esc(presentation.stop)}</strong></div>
          <p>${esc(presentation.summary)}</p>
          <div class="candidate-direct-reason"><span>当时记录的直接原因</span><strong>${esc(c.terminalReason || "未单独记录具体原因")}</strong></div>
        </section>
        <section class="candidate-detail-section">
          <div class="candidate-section-title"><div><span>从候选到正式结果</span><h3>筛选过程</h3></div><small>灰色步骤表示前一环节停止后未再进入</small></div>
          <ol class="candidate-stage-path">${path}</ol>
        </section>
        <div class="candidate-detail-grid">
          <section class="candidate-detail-panel">
            <div class="candidate-section-title"><div><span>推荐日冻结事实</span><h3>当时为什么被关注</h3></div></div>
            <dl class="candidate-fact-list">
              <div><dt>初筛位置</dt><dd>${c.preliminaryScoreRank ? `第 ${esc(c.preliminaryScoreRank)} 名 · ` : ""}${fixed(c.preliminaryScore)} 分</dd></div>
              <div><dt>行业</dt><dd>${esc(c.industry || "未记录")}</dd></div>
              <div><dt>验证安排</dt><dd>${esc(names[c.primaryGroup] || "未分配验证名额")}${c.allocationRound ? ` · 第${c.allocationRound}轮` : ""}</dd></div>
              <div><dt>分配原因</dt><dd>${esc(reasonText(c))}</dd></div>
              <div><dt>冻结参考价</dt><dd>${price(c.frozenScanPrice)} 元</dd></div>
              <div><dt>冻结时间</dt><dd>${esc(readableTime(c.minute15mCutoff))}</dd></div>
            </dl>
          </section>
          <section class="candidate-detail-panel">
            <div class="candidate-section-title"><div><span>当时可用证据</span><h3>本环节看到了什么</h3></div></div>
            <dl class="candidate-fact-list">
              <div><dt>分钟数据</dt><dd>${esc(minuteStatus)}</dd></div>
              <div><dt>交易形态</dt><dd>${esc(shapeStatus)}</dd></div>
              <div><dt>后续正式证据</dt><dd>${esc(formalStatus)}</dd></div>
              <div><dt>尾盘检查</dt><dd>${esc(tailStatus)}</dd></div>
              <div><dt>风险记录</dt><dd>${esc((c.riskFlags || []).join("；") || "未记录额外风险")}</dd></div>
              <div><dt>身份标签</dt><dd class="candidate-identity-chips">${identityChips}</dd></div>
            </dl>
          </section>
        </div>
        <section class="candidate-detail-section candidate-proxy-section">
          <div class="candidate-section-title"><div><span>${esc(metric.targetTradeDate || "次日数据待采集")}</span><h3>次日表现 · 仅用于事后复盘</h3></div><small>机会代理不等于真实可实现收益，也不参与当时推荐</small></div>
          <div class="candidate-proxy-grid">
            ${proxyCard("开盘机会代理", metric.openProxyReturnPct, "相对冻结参考价")}
            ${proxyCard("收盘机会代理", metric.closeProxyReturnPct, "相对冻结参考价")}
            ${proxyCard("盘中最高机会", metric.highOpportunityPct, "事后最高价观察")}
            ${proxyCard("盘中最低不利", metric.lowAdversePct, "事后最低价观察")}
          </div>
        </section>
        <details class="candidate-audit-details">
          <summary><span><strong>技术与审计信息</strong><small>版本、行情源、原始价格和过程记录</small></span><span class="candidate-audit-toggle">展开</span></summary>
          <div class="candidate-audit-grid">
            <dl class="candidate-fact-list">
              <div><dt>数据缺口</dt><dd>${esc(dataGap)}</dd></div>
              <div><dt>分钟行情来源</dt><dd>${esc(sourceLabel(c.minute15mSource))}</dd></div>
              <div><dt>验证评分／排名</dt><dd>${fixed(c.verifiedScore)} 分 ／ ${c.verifiedRank == null ? "--" : `第 ${esc(c.verifiedRank)} 名`}</dd></div>
              <div><dt>评分分项</dt><dd>${esc(scoreComponents)}</dd></div>
              <div><dt>筛选规则版本</dt><dd>${esc(c.selectionPolicyVersion || c.terminalRuleVersion || "--")}</dd></div>
              <div><dt>复盘版本／重建时间</dt><dd>${esc(c.reviewSchemaVersion || "读取时计算")} ／ ${esc(readableTime(c.reviewComputedAt))}</dd></div>
            </dl>
            <dl class="candidate-fact-list">
              <div><dt>次日日行情来源</dt><dd>${esc(sourceLabel(metric.dailyBarSource))}</dd></div>
              <div><dt>次日开／高／低／收</dt><dd>${[daily.open,daily.high,daily.low,daily.close].map((value) => price(value)).join(" ／ ")}</dd></div>
              <div><dt>除权除息状态</dt><dd>${esc(corporateNames[metric.corporateActionStatus] || "未知")}</dd></div>
              <div><dt>机会代理排除原因</dt><dd>${esc((metric.exclusionReasons || []).join("；") || "无")}</dd></div>
              <div><dt>隔夜缺口</dt><dd>${pct(metric.overnightGapPct)}</dd></div>
              <div><dt>约束放宽</dt><dd>${esc((c.relaxationReason || "").split(",").filter(Boolean).map((item) => relaxNames[item] || item).join("；") || "无")}</dd></div>
            </dl>
          </div>
          <h4>过程记录</h4>
          <ol class="candidate-event-list">${eventRows}</ol>
        </details>`;
    } catch (error) { if (current(g) && version === detailVersion) { $("candidateDetailTitle").textContent = "候选详情加载失败"; $("archiveEvidence").innerHTML = `<p class="archive-note">${esc(error.message)}，请关闭后重试。</p>`; } }
  });
  $("candidateDetailClose").addEventListener("click", () => closeCandidateDetail());
  $("candidateDetailModal").addEventListener("click", (event) => {
    if (event.target === $("candidateDetailModal")) closeCandidateDetail();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("candidateDetailModal").hidden) closeCandidateDetail();
  });
  $("nextDayMissed").addEventListener("click", (event) => {
    const pageButton = event.target.closest?.("[data-leader-page]");
    if (pageButton) {
      leaderPage += pageButton.dataset.leaderPage === "next" ? 1 : -1;
      renderLeaders();
      return;
    }
    const button = event.target.closest?.("[data-leader-filter]");
    if (!button) return;
    leaderFilter = button.dataset.leaderFilter || "all";
    leaderPage = 1;
    renderLeaders();
  });
  $("nextDayMissed").addEventListener("change", (event) => {
    const pageSize = event.target.closest?.("[data-leader-page-size]");
    if (pageSize) {
      const value = Number(pageSize.value);
      leaderPageSize = [20, 50, 100].includes(value) ? value : 20;
      leaderPage = 1;
      renderLeaders();
      return;
    }
    const select = event.target.closest?.("[data-leader-sort]");
    if (!select) return;
    leaderSort = select.value || "default";
    leaderPage = 1;
    renderLeaders();
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
