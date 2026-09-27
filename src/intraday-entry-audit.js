(function () {
  const STATUS_TONE = {
    block_entry: "bad",
    wait_confirm: "warn",
    pullback_only: "warn",
    small_test: "good",
    watch_priority: "neutral",
    t1_only: "good",
    manual_review: "warn"
  };
  const POSITION_LEVEL_LABELS = {
    none: "不参与",
    observe: "只观察",
    tiny_test: "极小仓试错",
    small: "小仓",
    light: "轻仓",
    hold_only: "仅持有不加仓",
    reduce: "减仓"
  };
  const POSITION_ACTION_LABELS = {
    no_new_position: "不新增仓位",
    new_small_test: "新开小仓试错",
    new_tiny_test: "新开极小仓试错",
    pullback_entry_only: "仅回踩低吸",
    hold_only: "持有但不加仓",
    reduce: "减仓处理",
    manual_review: "人工复核"
  };
  const STATUS_LABELS = {
    block_entry: "禁止进场",
    wait_confirm: "暂不进场，等待确认",
    pullback_only: "只允许回踩低吸",
    small_test: "可小仓试错",
    watch_priority: "重点观察",
    t1_only: "仅适合 T+1",
    manual_review: "需人工确认"
  };
  const RAW_TEXT_LABELS = {
    ai_full_market: "AI 全市场",
    aiFullMarket: "背景 AI 全市场",
    morningSectorRadar: "早盘主线雷达",
    stockAnalysis: "个股分析",
    modulePair: "审计链路",
    triggerModule: "触发模块",
    triggerSignal: "触发信号",
    stockAnalysisSignal: "个股分析信号",
    ai_full_market_plus_stock_analysis: "AI 全市场 + 个股分析",
    t1_plus_stock_analysis: "T+1 + 个股分析",
    watch_risk: "观察但风险偏高",
    positive: "正向支持",
    background_only: "仅作背景",
    matched: "已匹配",
    missing: "缺失",
    not_matched: "未匹配",
    deepseek_success: "DeepSeek 已完成",
    deepseek_failed: "DeepSeek 未完成",
    ai_not_configured: "AI 未配置",
    not_started: "未开始",
    no_new_position: "不新增仓位",
    new_small_test: "新开小仓试错"
  };

  let modal = null;
  let context = null;

  window.IntradayEntryAudit = {
    buttonHtml(item, options = {}) {
      const code = normalizeCode(item && item.code);
      const name = (item && item.name) || "";
      const source = options.triggerSource || "ai_full_market";
      const runId = options.runId || (item && item.runId) || "";
      const scanTime = options.scanTime || "";
      const signalDate = options.signalDate || "";
      const itemId = options.triggerItemId || code;
      const signalLabel = signalDate ? ` · 信号 ${signalDate.slice(5)}` : "";
      return `
        <button class="entry-audit-button" type="button"
          data-entry-audit-code="${escapeHtml(code)}"
          data-entry-audit-name="${escapeHtml(name)}"
          data-entry-audit-source="${escapeHtml(source)}"
          data-entry-audit-run-id="${escapeHtml(String(runId || ""))}"
          data-entry-audit-item-id="${escapeHtml(itemId)}"
          data-entry-audit-scan-time="${escapeHtml(scanTime)}"
          data-entry-audit-signal-date="${escapeHtml(signalDate)}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2 4 5v6c0 5.1 3.4 9.9 8 11 4.6-1.1 8-5.9 8-11V5l-8-3Zm0 2.2 6 2.2V11c0 4-2.5 7.8-6 8.8C8.5 18.8 6 15 6 11V6.4l6-2.2Zm-1 4.8h2v5h-2V9Zm0 6h2v2h-2v-2Z"/></svg>
          审计能否进场${escapeHtml(signalLabel)}
        </button>
      `;
    }
  };

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-entry-audit-code]");
    if (!button) return;
    event.preventDefault();
    openAuditModal({
      code: button.dataset.entryAuditCode,
      name: button.dataset.entryAuditName,
      triggerSource: button.dataset.entryAuditSource,
      triggerRunId: button.dataset.entryAuditRunId,
      triggerItemId: button.dataset.entryAuditItemId,
      scanTime: button.dataset.entryAuditScanTime,
      signalDate: button.dataset.entryAuditSignalDate
    });
  });

  function openAuditModal(nextContext) {
    context = nextContext;
    ensureModal();
    modal.querySelector("#entryAuditTitle").textContent = `${nextContext.name || "--"} ${nextContext.code || ""}`.trim();
    modal.querySelector("#entryAuditSource").textContent = nextContext.triggerSource === "t1" ? "T+1 + 个股分析" : "AI 全市场 + 个股分析";
    modal.querySelector("#entryAuditPositionState").value = "none";
    modal.querySelector("#entryAuditCost").value = "";
    modal.querySelector("#entryAuditPct").value = "";
    modal.querySelector("#entryAuditDays").value = "";
    modal.querySelector("#entryAuditHorizon").value = "short";
    modal.querySelector("#entryAuditResult").innerHTML = `<div class="entry-audit-empty">录入持仓状态后生成当天进场审计。</div>`;
    modal.querySelector("#entryAuditRecent").innerHTML = `<div class="entry-audit-empty compact">正在读取最近审计...</div>`;
    modal.querySelector("#entryAuditHistoryConfirm").checked = false;
    renderHistoryWarning();
    updatePositionFields();
    modal.hidden = false;
    document.body.classList.add("entry-audit-open");
    loadRecent();
  }

  function ensureModal() {
    if (modal) return;
    const wrap = document.createElement("section");
    wrap.className = "entry-audit-modal";
    wrap.hidden = true;
    wrap.innerHTML = `
      <div class="entry-audit-backdrop" data-entry-audit-close></div>
      <article class="entry-audit-dialog" role="dialog" aria-modal="true" aria-labelledby="entryAuditTitle">
        <header class="entry-audit-head">
          <div>
            <span id="entryAuditSource">--</span>
            <h2 id="entryAuditTitle">进场审计</h2>
          </div>
          <button class="entry-audit-icon-button" type="button" data-entry-audit-close aria-label="关闭">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6.4 5 5.6 5.6L17.6 5 19 6.4 13.4 12l5.6 5.6-1.4 1.4-5.6-5.6L6.4 19 5 17.6l5.6-5.6L5 6.4 6.4 5Z"/></svg>
          </button>
        </header>
        <section id="entryAuditHistoryWarning" class="entry-audit-history-warning" hidden>
          <strong>历史信号提醒</strong>
          <span id="entryAuditHistoryWarningText">--</span>
          <label>
            <input id="entryAuditHistoryConfirm" type="checkbox" />
            <span>我知道这是历史信号，仍按今天数据审计</span>
          </label>
        </section>
        <form id="entryAuditForm" class="entry-audit-form">
          <label>
            <span>持仓状态</span>
            <select id="entryAuditPositionState">
              <option value="none">未持仓</option>
              <option value="holding">已持仓</option>
              <option value="planned">计划买入</option>
            </select>
          </label>
          <label data-position-field>
            <span>成本价</span>
            <input id="entryAuditCost" type="number" min="0" step="0.01" placeholder="可选" />
          </label>
          <label data-position-field>
            <span>当前仓位%</span>
            <input id="entryAuditPct" type="number" min="0" max="100" step="1" placeholder="0" />
          </label>
          <label data-position-field>
            <span>持有天数</span>
            <input id="entryAuditDays" type="number" min="0" step="1" placeholder="0" />
          </label>
          <label>
            <span>计划周期</span>
            <select id="entryAuditHorizon">
              <option value="short">短线</option>
              <option value="mid">中线</option>
              <option value="long">长线</option>
            </select>
          </label>
          <button id="entryAuditRunButton" class="entry-audit-run-button" type="submit">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7L8 5Z"/></svg>
            生成审计
          </button>
        </form>
        <div id="entryAuditResult" class="entry-audit-result"></div>
        <section class="entry-audit-recent-panel">
          <div class="entry-audit-section-head">
            <h3>最近审计</h3>
            <span>同一股票的历史快照</span>
          </div>
          <div id="entryAuditRecent" class="entry-audit-recent"></div>
        </section>
      </article>
    `;
    document.body.appendChild(wrap);
    modal = wrap;
    modal.addEventListener("click", (event) => {
      if (event.target.closest("[data-entry-audit-close]")) closeModal();
    });
    modal.querySelector("#entryAuditPositionState").addEventListener("change", updatePositionFields);
    modal.querySelector("#entryAuditHistoryConfirm").addEventListener("change", updateRunButtonState);
    modal.querySelector("#entryAuditForm").addEventListener("submit", runAudit);
  }

  function renderHistoryWarning() {
    const warning = modal.querySelector("#entryAuditHistoryWarning");
    const text = modal.querySelector("#entryAuditHistoryWarningText");
    const signalDate = context?.signalDate || "";
    const today = todayText();
    const isHistorical = signalDate && signalDate !== today;
    warning.hidden = !isHistorical;
    text.textContent = isHistorical ? `触发信号日期是 ${signalDate}，今天是 ${today}。这是用今天数据审计历史候选，不是复盘当日结论。` : "";
    updateRunButtonState();
  }

  function updateRunButtonState() {
    if (!modal || !context) return;
    const signalDate = context.signalDate || "";
    const needsConfirm = signalDate && signalDate !== todayText();
    modal.querySelector("#entryAuditRunButton").disabled = Boolean(needsConfirm && !modal.querySelector("#entryAuditHistoryConfirm").checked);
  }

  function updatePositionFields() {
    if (!modal) return;
    const state = modal.querySelector("#entryAuditPositionState").value;
    modal.querySelectorAll("[data-position-field]").forEach((node) => {
      node.hidden = state !== "holding";
    });
  }

  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    document.body.classList.remove("entry-audit-open");
  }

  async function runAudit(event) {
    event.preventDefault();
    if (!context) return;
    const button = modal.querySelector("#entryAuditRunButton");
    const result = modal.querySelector("#entryAuditResult");
    button.disabled = true;
    result.innerHTML = `<div class="entry-audit-loading">正在生成个股分析和进场审计...</div>`;
    try {
      const response = await fetch("/api/intraday-entry-audit/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          code: context.code,
          name: context.name,
          triggerSource: context.triggerSource,
          triggerRunId: Number(context.triggerRunId || 0),
          triggerItemId: context.triggerItemId || context.code,
          scanTime: context.scanTime || "",
          refreshStockAnalysis: true,
          position: formPosition()
        })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
      result.innerHTML = renderAudit(payload);
      loadRecent();
    } catch (error) {
      result.innerHTML = `<div class="entry-audit-error">${escapeHtml(error.message || String(error))}</div>`;
    } finally {
      button.disabled = false;
    }
  }

  async function loadRecent() {
    if (!context || !modal) return;
    const recent = modal.querySelector("#entryAuditRecent");
    try {
      const params = new URLSearchParams({ code: context.code || "", limit: "8" });
      const response = await fetch(`/api/intraday-entry-audit/recent?${params.toString()}`);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
      recent.innerHTML = renderRecent(payload.items || []);
    } catch (error) {
      recent.innerHTML = `<div class="entry-audit-error compact">${escapeHtml(error.message || String(error))}</div>`;
    }
  }

  function formPosition() {
    return {
      state: modal.querySelector("#entryAuditPositionState").value || "none",
      costPrice: optionalNumber(modal.querySelector("#entryAuditCost").value),
      positionPct: optionalNumber(modal.querySelector("#entryAuditPct").value) || 0,
      holdingDays: optionalNumber(modal.querySelector("#entryAuditDays").value) || 0,
      plannedHorizon: modal.querySelector("#entryAuditHorizon").value || "short"
    };
  }

  function renderAudit(audit) {
    const tone = STATUS_TONE[audit.entryStatus] || "neutral";
    return `
      <section class="entry-audit-summary ${tone}">
        <div>
          <span>${escapeHtml(statusLabel(audit.entryStatus, audit.displayStatus))}</span>
          <h3>${escapeHtml(displayText(audit.auditConclusion || "--"))}</h3>
          <p>${escapeHtml(displayText(audit.actionSummary || ""))}</p>
        </div>
        <div class="entry-audit-position">
          <strong>${formatPct(audit.positionPct)}</strong>
          <small>${escapeHtml(positionLevelLabel(audit.positionLevel))} · ${escapeHtml(positionActionLabel(audit.positionAction))}</small>
        </div>
      </section>
      <div class="entry-audit-kpis">
        ${kpi("周期", listText(audit.applicableHorizon))}
        ${kpi("追高", audit.allowChasing ? "允许" : "不允许")}
        ${kpi("低吸", audit.allowPullbackEntry ? "允许" : "不允许")}
        ${kpi("AI 状态", aiStatusText(audit))}
      </div>
      <div class="entry-audit-sections">
        ${chainSection(audit.chainSummary || [])}
        ${listSection("进场条件", audit.triggerConditions)}
        ${listSection("失效条件", audit.invalidConditions)}
        ${listSection("机会证据", audit.opportunityEvidence)}
        ${listSection("风险证据", audit.riskEvidence)}
        ${textSection("冲突原因", audit.conflictExplanation)}
        ${timelineSection(audit.timeline || {}, audit.dataQuality || {})}
        ${listSection("板块/背景证据", audit.backgroundSignalsUsed)}
        ${listSection("核心缺失", (audit.dataQuality || {}).coreMissingEvidence)}
        ${listSection("可选证据缺失", (audit.dataQuality || {}).optionalMissingEvidence)}
        ${dataQualitySection(audit.dataQuality || {}, audit.normalizationWarnings || [])}
        ${evidenceDetailsSection(audit.evidenceDetails || {})}
      </div>
    `;
  }

  function kpi(label, value) {
    return `<article><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "--")}</strong></article>`;
  }

  function textSection(title, text) {
    return `<section><h4>${escapeHtml(title)}</h4><p>${escapeHtml(displayText(text || "--"))}</p></section>`;
  }

  function listSection(title, values) {
    const items = Array.isArray(values) ? values.filter(Boolean) : [];
    return `
      <section>
        <h4>${escapeHtml(title)}</h4>
        ${items.length ? `<ul>${items.map((item) => `<li>${escapeHtml(displayText(item))}</li>`).join("")}</ul>` : `<p>--</p>`}
      </section>
    `;
  }

  function dataQualitySection(quality, warnings) {
    const rows = [
      `个股分析：${quality.stockAnalysisStatus || (quality.stockAnalysisFresh ? "当天结果" : "缺失或非当天")}`,
      `触发信号：${quality.triggerSignalMatched ? "已匹配" : "未匹配"}`,
      `板块雷达：${quality.sectorRadarStatus || (quality.sectorRadarFresh ? "可用" : quality.sectorRadarStale ? "已过期" : "缺失")}`,
      `当前行情：${quality.hasCurrentQuote ? "已接入" : "缺失"}`
    ].concat(Array.isArray(warnings) ? warnings : []);
    return listSection("数据质量", rows);
  }

  function chainSection(items) {
    if (!Array.isArray(items) || !items.length) return "";
    return `
      <section class="entry-audit-wide-section">
        <h4>审计链路</h4>
        <div class="entry-audit-chain">
          ${items.map((item) => `
            <article>
              <strong>${escapeHtml(displayText(item.label || "--"))}</strong>
              <span>${escapeHtml(displayText(item.status || "--"))}</span>
              <small>${escapeHtml(displayText(item.detail || ""))}</small>
            </article>
          `).join("")}
        </div>
      </section>
    `;
  }

  function evidenceDetailsSection(details) {
    if (!details || !Object.keys(details).length) return "";
    return `
      <section class="entry-audit-wide-section">
        <h4>输入证据摘要</h4>
        <div class="entry-audit-details">
          ${detailBlock("触发模块原始证据", details.triggerModule, triggerEvidenceRows)}
          ${detailBlock("个股分析核心证据", details.stockAnalysis, stockEvidenceRows)}
          ${detailBlock("板块证据", details.morningSectorRadar, radarEvidenceRows)}
          ${backgroundDetails(details.backgroundSignals || {})}
        </div>
      </section>
    `;
  }

  function detailBlock(title, data, rowsBuilder) {
    const rows = rowsBuilder(data || {});
    return `
      <details>
        <summary>${escapeHtml(title)}</summary>
        <dl>${rows.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(displayText(value || "--"))}</dd></div>`).join("")}</dl>
      </details>
    `;
  }

  function triggerEvidenceRows(data) {
    return [
      ["来源", data.title],
      ["信号日期", `${data.signalDate || "--"} ${data.scanTime || ""}`.trim()],
      ["匹配状态", data.matched ? "已匹配" : "未匹配"],
      ["分数/置信", data.score],
      ["理由", data.reason],
      ["风险", data.risk],
      ["入手条件", data.entryCondition],
      ["失效条件", data.invalidCondition]
    ];
  }

  function stockEvidenceRows(data) {
    return [
      ["分析日期", `${data.analysisDate || "--"}${data.generatedByAudit ? " · 本次自动生成" : ""}`],
      ["结论", data.decision],
      ["周期", data.primaryHorizon],
      ["置信度", data.confidence],
      ["摘要", data.summary],
      ["风险", Array.isArray(data.risks) ? data.risks.join("；") : data.risks],
      ["缺失数据", Array.isArray(data.missingData) ? data.missingData.join("；") : data.missingData]
    ];
  }

  function radarEvidenceRows(data) {
    const sectors = Array.isArray(data.matchedSectors) ? data.matchedSectors : [];
    const sectorText = sectors.map((item) => item.sectorName || item.sectorCode || "").filter(Boolean).join("、");
    return [
      ["状态", data.status],
      ["时间", data.scanTime],
      ["匹配该股", data.matched ? "已匹配" : "未匹配"],
      ["匹配板块", sectorText]
    ];
  }

  function backgroundDetails(background) {
    const entries = Object.entries(background || {});
    if (!entries.length) return "";
    return entries.map(([key, value]) => detailBlock(`${triggerSourceLabel(value.title || key)}背景信号`, value, (data) => [
      ["来源", data.title || key],
      ["信号日期", `${data.signalDate || "--"} ${data.scanTime || ""}`.trim()],
      ["匹配状态", data.matched ? "已匹配" : "未匹配"],
      ["理由", data.reason]
    ])).join("");
  }

  function timelineSection(timeline, quality) {
    const rows = [
      `审计生成：${formatDateTime(timeline.auditGeneratedAt)}`,
      `审计交易日：${timeline.auditTradeDate || "--"}`,
      `触发信号：${triggerSourceLabel(timeline.triggerSource)} · ${timeline.triggerSignalDate || "--"} ${timeline.triggerScanTime || ""}`.trim(),
      `个股分析：${timeline.stockAnalysisDate || "--"}${timeline.stockAnalysisGeneratedByAudit ? " · 本次自动生成" : ""}`,
      `早盘雷达：${timeline.sectorRadarStatus || "--"}${timeline.sectorRadarDate ? ` · ${timeline.sectorRadarDate} ${timeline.sectorRadarTime || ""}` : ""}`
    ];
    if (quality.dateWarning) rows.unshift(quality.dateWarning);
    return listSection("时间口径", rows);
  }

  function renderRecent(items) {
    if (!items.length) return `<div class="entry-audit-empty compact">暂无最近审计。</div>`;
    return items.map((item) => `
      <article class="entry-audit-recent-item ${STATUS_TONE[item.entryStatus] || "neutral"}">
        <div>
          <strong>${escapeHtml(statusLabel(item.entryStatus, item.displayStatus))}</strong>
          <span>${escapeHtml(triggerSourceLabel(item.triggerSource))} · ${escapeHtml(formatDateTime(item.auditedAt))}</span>
          <p>${escapeHtml(displayText(item.auditConclusion || "--"))}</p>
        </div>
        <small>${formatPct(item.positionPct)} · ${escapeHtml(positionLevelLabel(item.positionLevel))}</small>
      </article>
    `).join("");
  }

  function aiStatusText(audit) {
    if (audit.aiStatus === "deepseek_success") return `${audit.aiProvider || "DeepSeek"} ${audit.aiModel || ""}`.trim();
    return audit.fallbackReason || "AI 未完成";
  }

  function triggerSourceLabel(value) {
    if (value === "ai_full_market") return "AI 全市场";
    if (value === "t1") return "T+1";
    return value || "--";
  }

  function statusLabel(status, displayStatus) {
    const rawDisplay = String(displayStatus || "").trim();
    if (rawDisplay && !STATUS_LABELS[rawDisplay]) return displayText(rawDisplay);
    return STATUS_LABELS[status] || STATUS_LABELS[rawDisplay] || displayText(status || rawDisplay || "--");
  }

  function positionLevelLabel(value) {
    return POSITION_LEVEL_LABELS[value] || value || "--";
  }

  function positionActionLabel(value) {
    return POSITION_ACTION_LABELS[value] || value || "--";
  }

  function listText(values) {
    return Array.isArray(values) && values.length ? displayText(values.join(" / ")) : "--";
  }

  function displayText(value) {
    let text = String(value ?? "");
    if (STATUS_LABELS[text]) return STATUS_LABELS[text];
    if (RAW_TEXT_LABELS[text]) return RAW_TEXT_LABELS[text];
    const replacements = { ...STATUS_LABELS, ...RAW_TEXT_LABELS };
    Object.keys(replacements)
      .sort((a, b) => b.length - a.length)
      .forEach((key) => {
        text = text.split(key).join(replacements[key]);
      });
    return text;
  }

  function formatPct(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "0%";
    return `${number.toFixed(number % 1 ? 1 : 0)}%`;
  }

  function formatDateTime(value) {
    const text = String(value || "").trim();
    if (!text) return "--";
    return text.replace("T", " ").slice(0, 16);
  }

  function todayText() {
    const now = new Date();
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, "0");
    const day = String(now.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function optionalNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function normalizeCode(value) {
    const digits = String(value || "").replace(/\D+/g, "");
    return digits ? digits.slice(-6).padStart(6, "0") : "";
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
})();
