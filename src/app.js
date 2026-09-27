function resolveCurrentScanTime(now = new Date()) {
  const minutes = now.getHours() * 60 + now.getMinutes();
  if (minutes < 10 * 60 + 30) return "09:50";
  if (minutes < 13 * 60) return "10:42";
  if (minutes < 14 * 60 + 30) return "13:18";
  return "14:36";
}


const state = {
  scanTime: resolveCurrentScanTime(),
  riskMode: "balanced",
  requestedSource: "auto",
  viewMode: "daily",
  activeSection: "dashboard",
  defaultUniverse: null,
  retailHolders: null,
  retailQuery: "",
  retailLoading: false,
  aiReviewStats: null,
  aiReviewLoading: false,
  aiReviewOutcomeResult: null,
  aiReviewTradeOutcomeResult: null,
  stockAnalysis: null,
  stockAnalysisLoading: false,
  stockAnalysisMarketRefreshing: false,
  stockAnalysisSuggestions: [],
  stockAnalysisSuggestionActive: -1,
  stockAnalysisSuggestRequestId: 0
};

let retailSearchTimer = null;
let stockAnalysisSearchTimer = null;

const apiEnabled = window.location.protocol !== "file:";

const els = {
  workspace: document.querySelector(".workspace"),
  dashboardSections: document.querySelectorAll("[data-dashboard-section]"),
  workbenchGrid: document.querySelector("#workbenchGrid"),
  navButtons: document.querySelectorAll(".nav-button[data-section]"),
  dashboardButton: document.querySelector("#dashboardButton"),
  marketLine: document.querySelector("#marketLine"),
  metricsGrid: document.querySelector("#metricsGrid"),
  universeButton: document.querySelector("#universeButton"),
  universeModal: document.querySelector("#universeModal"),
  universeCloseButton: document.querySelector("#universeCloseButton"),
  universeRefreshButton: document.querySelector("#universeRefreshButton"),
  universeEyebrow: document.querySelector("#universeEyebrow"),
  universeTitle: document.querySelector("#universeTitle"),
  universeDescription: document.querySelector("#universeDescription"),
  universeMethod: document.querySelector("#universeMethod"),
  universeList: document.querySelector("#universeList"),
  stockAnalysisButton: document.querySelector("#stockAnalysisButton"),
  stockAnalysisModal: document.querySelector("#stockAnalysisModal"),
  stockAnalysisCloseButton: document.querySelector("#stockAnalysisCloseButton"),
  stockAnalysisMarketRefreshButton: document.querySelector("#stockAnalysisMarketRefreshButton"),
  stockAnalysisForm: document.querySelector("#stockAnalysisForm"),
  stockAnalysisCode: document.querySelector("#stockAnalysisCode"),
  stockAnalysisSuggestions: document.querySelector("#stockAnalysisSuggestions"),
  stockAnalysisHasPosition: document.querySelector("#stockAnalysisHasPosition"),
  stockAnalysisPositionFields: document.querySelector("#stockAnalysisPositionFields"),
  stockAnalysisCostPrice: document.querySelector("#stockAnalysisCostPrice"),
  stockAnalysisPositionPct: document.querySelector("#stockAnalysisPositionPct"),
  stockAnalysisHoldingDays: document.querySelector("#stockAnalysisHoldingDays"),
  stockAnalysisPlannedHorizon: document.querySelector("#stockAnalysisPlannedHorizon"),
  stockAnalysisRunButton: document.querySelector("#stockAnalysisRunButton"),
  stockAnalysisEyebrow: document.querySelector("#stockAnalysisEyebrow"),
  stockAnalysisTitle: document.querySelector("#stockAnalysisTitle"),
  stockAnalysisWarning: document.querySelector("#stockAnalysisWarning"),
  stockAnalysisResult: document.querySelector("#stockAnalysisResult"),
  aiFullMarketButton: document.querySelector("#aiFullMarketButton"),
  morningSectorButton: document.querySelector("#morningSectorButton"),
  t1OvernightButton: document.querySelector("#t1OvernightButton"),
  aiReviewButton: document.querySelector("#aiReviewButton"),
  crossValidationButton: document.querySelector("#crossValidationButton"),
  entryAuditHistoryButton: document.querySelector("#entryAuditHistoryButton"),
  aiReviewPage: document.querySelector("#aiReviewPage"),
  aiReviewCloseButton: document.querySelector("#aiReviewCloseButton"),
  aiReviewOutcomesButton: document.querySelector("#aiReviewOutcomesButton"),
  aiReviewHorizonSelect: document.querySelector("#aiReviewHorizonSelect"),
  aiReviewTradeOutcomesButton: document.querySelector("#aiReviewTradeOutcomesButton"),
  aiReviewGenerateButton: document.querySelector("#aiReviewGenerateButton"),
  aiReviewEyebrow: document.querySelector("#aiReviewEyebrow"),
  aiReviewTitle: document.querySelector("#aiReviewTitle"),
  aiReviewWarning: document.querySelector("#aiReviewWarning"),
  aiReviewContent: document.querySelector("#aiReviewContent"),
  retailHolderButton: document.querySelector("#retailHolderButton"),
  retailHolderModal: document.querySelector("#retailHolderModal"),
  retailHolderCloseButton: document.querySelector("#retailHolderCloseButton"),
  retailHolderRefreshButton: document.querySelector("#retailHolderRefreshButton"),
  retailHolderEyebrow: document.querySelector("#retailHolderEyebrow"),
  retailHolderTitle: document.querySelector("#retailHolderTitle"),
  retailHolderSearch: document.querySelector("#retailHolderSearch"),
  retailHolderSummary: document.querySelector("#retailHolderSummary"),
  retailHolderWarning: document.querySelector("#retailHolderWarning"),
  retailHolderList: document.querySelector("#retailHolderList")
};

const workspacePages = [
  { key: "universe", root: els.universeModal },
  { key: "stock", root: els.stockAnalysisModal },
  { key: "ai-review", root: els.aiReviewPage },
  { key: "retail", root: els.retailHolderModal }
];

const workspaceSectionKeys = new Set(["dashboard", ...workspacePages.map((page) => page.key)]);

function setupWorkspacePages() {
  workspacePages.forEach(({ key, root }) => {
    if (!root || !els.workspace) return;
    root.dataset.appSection = key;
    root.classList.remove("modal-backdrop");
    root.classList.add("workspace-section", "tool-page");
    root.hidden = true;
    const panel = root.querySelector(".modal");
    if (panel) {
      panel.classList.remove("modal");
      panel.classList.add("tool-panel");
      panel.removeAttribute("aria-modal");
      panel.setAttribute("role", "region");
    }
    const closeButton = root.querySelector(".modal-head .icon-button");
    if (closeButton) {
      closeButton.setAttribute("title", "返回总览");
      closeButton.setAttribute("aria-label", "返回总览");
      closeButton.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10.4 5 4.7 10.7 4 12l.7 1.3 5.7 5.7 1.4-1.4L7.2 13H20v-2H7.2l4.6-4.6L10.4 5Z"/></svg>`;
    }
    els.workspace.appendChild(root);
  });
  openWorkspaceSection(workspaceSectionFromHash(), { scroll: false, updateUrl: false });
}

function workspaceSectionFromHash() {
  const rawHash = String(window.location.hash || "").replace(/^#\/?/, "");
  let section = "";
  try {
    section = decodeURIComponent(rawHash).trim();
  } catch (error) {
    section = rawHash.trim();
  }
  return workspaceSectionKeys.has(section) ? section : "dashboard";
}

function syncWorkspaceLocation(section) {
  if (!workspaceSectionKeys.has(section)) return;
  const nextHash = section === "dashboard" ? "" : `#${encodeURIComponent(section)}`;
  if (window.location.hash === nextHash) return;
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${nextHash}`);
}

function openWorkspaceSection(section, options = {}) {
  if (section === "universe") {
    openUniverseModal(options);
  } else if (section === "stock") {
    openStockAnalysisModal(options);
  } else if (section === "ai-review") {
    openAiReviewPage(options);
  } else if (section === "retail") {
    openRetailHolderModal(options);
  } else {
    showWorkspaceSection("dashboard", options);
  }
}

function setActiveNavigation(section) {
  document.querySelectorAll(".nav-module[data-current]").forEach((module) => {
    delete module.dataset.current;
  });
  els.navButtons.forEach((button) => {
    const active = button.dataset.section === section;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
    if (active) {
      const parentModule = button.closest(".nav-module");
      if (parentModule) {
        parentModule.open = true;
        parentModule.dataset.current = "true";
      }
    }
  });
}

function showWorkspaceSection(section, { scroll = true, updateUrl = true } = {}) {
  state.activeSection = section;
  const dashboardVisible = section === "dashboard";
  els.dashboardSections.forEach((block) => {
    block.hidden = !dashboardVisible;
  });
  workspacePages.forEach(({ key, root }) => {
    if (root) root.hidden = key !== section;
  });
  setActiveNavigation(section);
  if (updateUrl) syncWorkspaceLocation(section);
  if (scroll) window.scrollTo({ top: 0, behavior: "smooth" });
  if (dashboardVisible) window.requestAnimationFrame(() => renderWorkbench());
}

function shouldCloseOnBackdrop(root, event) {
  return root?.classList.contains("modal-backdrop") && event.target === root;
}

function formatPct(value) {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function formatInteger(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function formatMoney(value) {
  const number = Number(value || 0);
  if (!number) return "--";
  if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(1)}亿`;
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)}万`;
  return number.toFixed(0);
}

function render() {
  renderWorkbench();
}

function renderWorkbench() {
  if (els.marketLine) {
    els.marketLine.textContent = "旧推荐总览已下线。这里仅保留独立工具入口，不再展示 15m/日线 Top20 推荐预览。";
  }
  if (els.metricsGrid) {
    const metrics = [
      { label: "推荐总览", value: "已下线", sub: "15m/日线 Top20 不再生成或展示" },
      { label: "个股分析", value: "保留", sub: "使用独立分析快照和复盘闭环" },
      { label: "AI全市场/T+1", value: "保留", sub: "使用独立运行表，不复用首页推荐表" },
      { label: "推荐数据表", value: "已清理", sub: "旧扫描、日线推荐、回测和Top20深度分析表已移除" }
    ];
    els.metricsGrid.innerHTML = metrics
      .map(
        (item) => `
          <article class="metric">
            <span>${item.label}</span>
            <strong>${item.value}</strong>
            <small>${item.sub}</small>
          </article>
        `
      )
      .join("");
  }
}

function sourceName(source) {
  return {
    auto: "自动(全市场)",
    akshare: "AKShare",
    sina: "新浪",
    tencent: "腾讯",
    tushare: "Tushare",
    baostock: "BaoStock",
    mock: "Mock",
    api: "API",
    deepseek: "DeepSeek",
    openai: "OpenAI",
    rules: "规则",
    multi_minute: "多源分钟线",
    tencent_minute: "腾讯分钟线",
    eastmoney_minute: "东方财富分钟线",
    akshare_eastmoney_minute: "AKShare东财分钟线"
  }[source] || source || "--";
}

function klineSourceLabel(source, period = "") {
  const label = {
    multi_minute: "多源分钟线",
    tencent_minute: "腾讯分钟线",
    eastmoney_minute: "东方财富分钟线",
    akshare_eastmoney_minute: "AKShare东财分钟线",
    tencent_daily: "腾讯日K",
    baostock_daily: "BaoStock日K",
    tushare_daily: "Tushare日K",
    mixed_daily: "日K混合源",
    cache_daily: "日K缓存",
    simulated: "模拟K线"
  }[source] || source || "模拟K线";
  return period ? `${label} ${period}` : label;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function loadDefaultUniverse() {
  if (state.defaultUniverse || !apiEnabled) return state.defaultUniverse;
  const response = await fetch("/api/universe/default");
  if (!response.ok) throw new Error(`API ${response.status}`);
  state.defaultUniverse = await response.json();
  return state.defaultUniverse;
}

function renderUniverseModal() {
  const universe = state.defaultUniverse;
  const typeText = universe?.type === "dynamic" ? "动态样本" : "静态兜底";
  const staleText = universe ? universe.stale ? "待更新" : "今日已更新" : "";
  const sourceText = universe?.source ? ` · ${sourceName(universe.source)}` : "";
  els.universeEyebrow.textContent = universe ? `${universe.count} 只 · ${typeText}${sourceText} · ${staleText}` : "默认样本";
  els.universeTitle.textContent = universe?.name || "动态重点池";
  els.universeDescription.textContent = universe?.description || "正在读取动态重点池。";
  const methodItems = [...(universe?.method || ["加载中"])];
  if (universe?.providerWarning) methodItems.unshift(universe.providerWarning);
  els.universeMethod.innerHTML = methodItems
    .map((item) => `<span>${escapeHtml(item)}</span>`)
    .join("");
  els.universeList.innerHTML = (universe?.items || [])
    .map(
      (item) => `
        <article class="universe-item" title="${escapeHtml(item.reason || "")}">
          <code>${escapeHtml(item.code)}</code>
          <strong>${escapeHtml(item.name)}</strong>
          <small>${escapeHtml(item.board)}</small>
        </article>
      `
    )
    .join("");
}

async function openUniverseModal(options = {}) {
  showWorkspaceSection("universe", options);
  renderUniverseModal();
  try {
    await loadDefaultUniverse();
    renderUniverseModal();
  } catch (error) {
    els.universeDescription.textContent = "动态重点池读取失败，请确认本地 API 服务正在运行。";
    els.universeMethod.innerHTML = `<span>${escapeHtml(error.message)}</span>`;
    els.universeList.innerHTML = "";
  }
}

function setUniverseRefreshLoading(loading) {
  if (!els.universeRefreshButton) return;
  els.universeRefreshButton.disabled = loading;
  els.universeRefreshButton.innerHTML = loading
    ? "更新中"
    : `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.7 6.3A8 8 0 1 0 20 12h-2a6 6 0 1 1-1.8-4.3L13 11h8V3l-3.3 3.3Z"/></svg>更新`;
}

async function refreshDefaultUniverse() {
  if (!apiEnabled) return;
  setUniverseRefreshLoading(true);
  try {
    const response = await fetch("/api/universe/default/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "auto", maxSize: 80 })
    });
    if (!response.ok) throw new Error(`API ${response.status}`);
    state.defaultUniverse = await response.json();
    renderUniverseModal();
  } catch (error) {
    state.defaultUniverse = {
      ...(state.defaultUniverse || {}),
      providerWarning: `重点池更新失败：${error.message}`
    };
    renderUniverseModal();
  } finally {
    setUniverseRefreshLoading(false);
  }
}

function closeUniverseModal() {
  showWorkspaceSection("dashboard");
}

function normalizeStockAnalysisInput(value) {
  return String(value || "").trim();
}

function stockSuggestionValue(item) {
  return `${item.name || ""} ${item.code || ""}`.trim();
}

function clearStockAnalysisSuggestions() {
  state.stockAnalysisSuggestions = [];
  state.stockAnalysisSuggestionActive = -1;
  renderStockAnalysisSuggestions();
}

function scheduleStockAnalysisSuggestions() {
  const query = normalizeStockAnalysisInput(els.stockAnalysisCode?.value);
  window.clearTimeout(stockAnalysisSearchTimer);
  if (!query || query.length < 2) {
    clearStockAnalysisSuggestions();
    return;
  }
  stockAnalysisSearchTimer = window.setTimeout(() => {
    loadStockAnalysisSuggestions(query);
  }, 180);
}

async function loadStockAnalysisSuggestions(query) {
  if (!apiEnabled) return;
  const requestId = state.stockAnalysisSuggestRequestId + 1;
  state.stockAnalysisSuggestRequestId = requestId;
  try {
    const params = new URLSearchParams({ query, limit: "10" });
    const response = await fetch(`/api/stock/search?${params.toString()}`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    if (requestId !== state.stockAnalysisSuggestRequestId) return;
    const current = normalizeStockAnalysisInput(els.stockAnalysisCode?.value);
    if (current !== query) return;
    state.stockAnalysisSuggestions = payload.items || [];
    state.stockAnalysisSuggestionActive = state.stockAnalysisSuggestions.length ? 0 : -1;
    renderStockAnalysisSuggestions();
  } catch (error) {
    if (requestId !== state.stockAnalysisSuggestRequestId) return;
    state.stockAnalysisSuggestions = [];
    state.stockAnalysisSuggestionActive = -1;
    renderStockAnalysisSuggestions();
  }
}

function renderStockAnalysisSuggestions() {
  if (!els.stockAnalysisSuggestions) return;
  const rows = state.stockAnalysisSuggestions || [];
  if (!rows.length) {
    els.stockAnalysisSuggestions.hidden = true;
    els.stockAnalysisSuggestions.innerHTML = "";
    return;
  }
  els.stockAnalysisSuggestions.hidden = false;
  els.stockAnalysisSuggestions.innerHTML = rows
    .map((item, index) => {
      const active = index === state.stockAnalysisSuggestionActive ? " active" : "";
      return `
        <button class="stock-suggestion${active}" type="button" data-index="${index}">
          <strong>${escapeHtml(item.name || "--")}</strong>
          <span>${escapeHtml(item.code || "")}</span>
          <small>${escapeHtml(item.source || "")}</small>
        </button>
      `;
    })
    .join("");
}

function chooseStockAnalysisSuggestion(index, { run = false } = {}) {
  const item = state.stockAnalysisSuggestions?.[index];
  if (!item || !els.stockAnalysisCode) return;
  els.stockAnalysisCode.value = stockSuggestionValue(item);
  clearStockAnalysisSuggestions();
  if (run) analyzeStock();
}

function handleStockAnalysisSuggestionKey(event) {
  const rows = state.stockAnalysisSuggestions || [];
  if (!rows.length) return;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    state.stockAnalysisSuggestionActive = (state.stockAnalysisSuggestionActive + 1) % rows.length;
    renderStockAnalysisSuggestions();
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    state.stockAnalysisSuggestionActive = (state.stockAnalysisSuggestionActive - 1 + rows.length) % rows.length;
    renderStockAnalysisSuggestions();
  } else if (event.key === "Enter" && state.stockAnalysisSuggestionActive >= 0) {
    event.preventDefault();
    chooseStockAnalysisSuggestion(state.stockAnalysisSuggestionActive, { run: true });
  } else if (event.key === "Escape") {
    clearStockAnalysisSuggestions();
  }
}

function openStockAnalysisModal(options = {}) {
  showWorkspaceSection("stock", options);
  const params = new URLSearchParams(window.location.search);
  const defaultCode = options.query || options.code || params.get("stock") || params.get("code") || params.get("query") || "";
  if (els.stockAnalysisCode && !els.stockAnalysisCode.value) {
    els.stockAnalysisCode.value = defaultCode;
  }
  clearStockAnalysisSuggestions();
  renderStockAnalysisModal();
  const shouldRun = options.run || params.get("analyze") === "1" || params.get("run") === "1";
  if (shouldRun && normalizeStockAnalysisInput(els.stockAnalysisCode?.value)) {
    window.setTimeout(() => analyzeStock(), 0);
  }
}

function closeStockAnalysisModal() {
  showWorkspaceSection("dashboard");
  clearStockAnalysisSuggestions();
}

function openAiReviewPage(options = {}) {
  showWorkspaceSection("ai-review", options);
  loadAiReviewStats();
  loadAiReviewRecentOutcomes();
}

function openCrossModuleValidationPage() {
  const target = "./cross-module-validation.html";
  const targetUrl = new URL(target, window.location.href);
  if (window.location.href === targetUrl.href) {
    window.location.reload();
    return;
  }
  window.location.href = target;
}

function openIntradayEntryAuditHistoryPage() {
  const target = "./intraday-entry-audit-history.html";
  const targetUrl = new URL(target, window.location.href);
  if (window.location.href === targetUrl.href) {
    window.location.reload();
    return;
  }
  window.location.href = target;
}

function closeAiReviewPage() {
  showWorkspaceSection("dashboard");
}

function setStockAnalysisLoading(loading) {
  state.stockAnalysisLoading = loading;
  if (!els.stockAnalysisRunButton) return;
  els.stockAnalysisRunButton.disabled = loading;
  updateStockAnalysisPositionFields();
  els.stockAnalysisRunButton.innerHTML = loading
    ? `<span class="button-spinner"></span>分析中`
    : `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 4a6 6 0 1 0 3.7 10.7l3.6 3.6 1.4-1.4-3.6-3.6A6 6 0 0 0 10 4Zm0 2a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z"/></svg>分析`;
}

function updateStockAnalysisPositionFields() {
  const hasPosition = Boolean(els.stockAnalysisHasPosition?.checked);
  if (els.stockAnalysisPositionFields) {
    els.stockAnalysisPositionFields.hidden = !hasPosition;
  }
  [
    els.stockAnalysisCostPrice,
    els.stockAnalysisPositionPct,
    els.stockAnalysisHoldingDays,
    els.stockAnalysisPlannedHorizon
  ].forEach((control) => {
    if (control) control.disabled = !hasPosition || state.stockAnalysisLoading;
  });
}

function collectStockAnalysisPositionPayload() {
  if (!els.stockAnalysisHasPosition?.checked) {
    return { positionState: "none" };
  }
  const costPrice = readNumberInput(els.stockAnalysisCostPrice);
  const positionPct = readNumberInput(els.stockAnalysisPositionPct);
  const holdingDays = readNumberInput(els.stockAnalysisHoldingDays);
  const plannedHorizon = String(els.stockAnalysisPlannedHorizon?.value || "").trim();
  const errors = [];
  if (!Number.isFinite(costPrice) || costPrice <= 0) errors.push("成本价必须大于 0。");
  if (!Number.isFinite(positionPct) || positionPct <= 0 || positionPct > 100) errors.push("仓位比例必须在 0-100% 之间。");
  if (!Number.isFinite(holdingDays) || holdingDays < 0 || Math.floor(holdingDays) !== holdingDays) errors.push("持有天数必须是大于等于 0 的整数。");
  if (plannedHorizon && !["short", "mid", "long"].includes(plannedHorizon)) errors.push("计划周期只能是短线、中线或长线。");
  if (errors.length) {
    return { error: errors.join("") };
  }
  return {
    positionState: "existing",
    positionInfo: {
      costPrice,
      positionPct,
      holdingDays,
      ...(plannedHorizon ? { plannedHorizon } : {})
    }
  };
}

function readNumberInput(input) {
  const raw = String(input?.value ?? "").trim();
  if (!raw) return NaN;
  const value = Number(raw);
  return Number.isFinite(value) ? value : NaN;
}

function setStockAnalysisMarketRefreshing(loading) {
  state.stockAnalysisMarketRefreshing = loading;
  if (!els.stockAnalysisMarketRefreshButton) return;
  els.stockAnalysisMarketRefreshButton.disabled = loading;
  els.stockAnalysisMarketRefreshButton.innerHTML = loading
    ? `<span class="button-spinner"></span>更新中`
    : `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.7 6.3A8 8 0 1 0 20 12h-2a6 6 0 1 1-1.8-4.3L13 11h8V3l-3.3 3.3Z"/></svg>实时更新全市场`;
}

async function refreshStockAnalysisMarketContext() {
  if (!apiEnabled || state.stockAnalysisMarketRefreshing) return;
  setStockAnalysisMarketRefreshing(true);
  els.stockAnalysisWarning.hidden = false;
  els.stockAnalysisWarning.textContent = "正在实时更新全市场上下文缓存，完成后会自动重新分析当前个股。";
  try {
    const response = await fetch("/api/market-context/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: state.requestedSource || "auto" })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    const generatedAt = payload.generatedAt ? payload.generatedAt.replace("T", " ") : "--";
    els.stockAnalysisWarning.hidden = false;
    els.stockAnalysisWarning.textContent = payload.stale
      ? `全市场实时更新失败，继续使用旧缓存：${sourceName(payload.source)} · ${Number(payload.count || 0)} 只 · ${generatedAt}${payload.warning ? ` · ${payload.warning}` : ""}`
      : `全市场缓存已更新：${sourceName(payload.source)} · ${Number(payload.count || 0)} 只 · ${generatedAt}`;
    if (normalizeStockAnalysisInput(els.stockAnalysisCode?.value)) {
      await analyzeStock({ keepWarning: true });
    }
  } catch (error) {
    els.stockAnalysisWarning.hidden = false;
    els.stockAnalysisWarning.textContent = `全市场实时更新失败：${error.message}`;
  } finally {
    setStockAnalysisMarketRefreshing(false);
  }
}

async function analyzeStock({ keepWarning = false } = {}) {
  if (!apiEnabled || state.stockAnalysisLoading) return;
  const query = normalizeStockAnalysisInput(els.stockAnalysisCode?.value);
  if (!query) {
    els.stockAnalysisWarning.hidden = false;
    els.stockAnalysisWarning.textContent = "请输入股票代码或股票名称。";
    return;
  }
  const positionPayload = collectStockAnalysisPositionPayload();
  if (positionPayload.error) {
    els.stockAnalysisWarning.hidden = false;
    els.stockAnalysisWarning.textContent = `持仓信息无效：${positionPayload.error}`;
    return;
  }
  clearStockAnalysisSuggestions();
  if (!keepWarning) {
    els.stockAnalysisWarning.hidden = true;
  }
  setStockAnalysisLoading(true);
  try {
    const response = await fetch("/api/stock/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        version: "v2",
        riskMode: state.riskMode,
        scanTime: state.scanTime,
        useAI: true,
        marketSource: state.requestedSource || "auto",
        ...positionPayload
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.stockAnalysis = payload;
    renderStockAnalysisModal();
  } catch (error) {
    els.stockAnalysisWarning.hidden = false;
    els.stockAnalysisWarning.textContent = `个股分析失败：${error.message}`;
  } finally {
    setStockAnalysisLoading(false);
  }
}

function renderStockAnalysisModal() {
  const payload = state.stockAnalysis;
  if (!payload) {
    els.stockAnalysisEyebrow.textContent = "个股分析";
    els.stockAnalysisTitle.textContent = "买点与周期判断";
    els.stockAnalysisResult.innerHTML = `
      <div class="stock-empty">
        <strong>输入代码或名称后生成分析</strong>
        <span>结果会同时展示短线、中线、长线判断和对应证据。</span>
      </div>
    `;
    return;
  }
  const decision = payload.decision || {};
  const evidence = payload.evidence || {};
  const positionContext = decision.positionContext || evidence.positionContext || payload.positionContext || {};
  const generatedAt = payload.generatedAt ? payload.generatedAt.replace("T", " ") : "--";
  els.stockAnalysisEyebrow.textContent = `${sourceName(payload.aiMode)} · ${generatedAt}`;
  els.stockAnalysisTitle.textContent = `${payload.name || "--"} ${payload.code || ""}`;
  els.stockAnalysisResult.innerHTML = `
    ${renderDecisionOverview(payload, decision, evidence, positionContext)}
    ${renderStockAnalysisWarnings(payload)}
    ${renderPlainLanguageBrief(decision, evidence)}
    ${renderTradePlan(decision.tradePlan, decision.tradePlanValidation)}
    ${renderPositionPlans(decision, positionContext)}
    ${renderCaseMemoryStatus(payload.caseMemoryStatus, payload.reviewMemoryStatus)}
    ${renderHorizonDefinition()}
    <section class="stock-horizon-grid">
      ${["short", "mid", "long"].map((key) => renderHorizonCard(key, decision.horizons?.[key])).join("")}
    </section>
    ${renderBattle(decision.battle)}
    <section class="stock-section">
      <h3>关键数据</h3>
      <div class="stock-metric-grid">${renderStockMetrics(evidence)}</div>
    </section>
    <section class="stock-section">
      <h3>支撑证据</h3>
      ${renderTextList(decision.supportingEvidence, "暂无支撑证据")}
    </section>
    <section class="stock-section split">
      <div>
        <h3>风险</h3>
        ${renderTextList(decision.risks, "暂无强风险标签")}
      </div>
      <div>
        <h3>缺失数据</h3>
        ${renderTextList(decision.missingData, "当前证据包未标记缺失项")}
      </div>
    </section>
    <section class="stock-section">
      <h3>新闻公告</h3>
      <div class="news-list">${renderStockNews(evidence)}</div>
    </section>
  `;
}

function renderPlainLanguageBrief(decision = {}, evidence = {}) {
  const plan = decision.tradePlan || decision.newPositionPlan || {};
  const explanation = normalizedUserExplanation(decision.userExplanation);
  const action = explanation.plainDecision || plainDecisionAction(decision.decision, plan);
  const whyRows = explanation.why.length ? explanation.why : plainDecisionReasons(decision, evidence);
  const considerCondition = explanation.considerCondition.text
    ? explanation.considerCondition
    : { text: plainConsiderCondition(decision, plan), terms: [] };
  const avoidCondition = explanation.avoidCondition.text
    ? explanation.avoidCondition
    : { text: plainAvoidCondition(decision), terms: [] };
  const consider = considerCondition.text;
  const avoid = avoidCondition.text;
  const considerHints = renderTradingTermHints(consider, considerCondition.terms);
  const avoidHints = renderTradingTermHints(avoid, avoidCondition.terms);
  const terms = renderPlainTermChips(evidence);
  return `
    <section class="stock-plain-brief ${decisionClass(decision.decision)}">
      <div class="plain-brief-head">
        <span>用户解读</span>
        <strong>${escapeHtml(action)}</strong>
        <small>这块是把 AI 的交易证据翻译成普通操作语言，最终仍以买入区间、止损和仓位计划为准。</small>
      </div>
      <div class="plain-brief-grid">
        <article>
          <span>为什么</span>
          ${whyRows.map((line) => `<p>${escapeHtml(line)}</p>`).join("")}
        </article>
        <article>
          <span>什么情况下再考虑</span>
          <p>${escapeHtml(expandTradingTermsInline(consider, { evidence, decision, terms: considerCondition.terms }))}</p>
          ${considerHints}
        </article>
        <article>
          <span>什么情况下继续回避</span>
          <p>${escapeHtml(expandTradingTermsInline(avoid, { evidence, decision, terms: avoidCondition.terms }))}</p>
          ${avoidHints}
        </article>
      </div>
      ${terms}
    </section>
  `;
}

function normalizedUserExplanation(value = {}) {
  const source = value && typeof value === "object" ? value : {};
  return {
    plainDecision: String(source.plainDecision || "").trim(),
    why: Array.isArray(source.why) ? source.why.map((item) => String(item || "").trim()).filter(Boolean).slice(0, 4) : [],
    considerCondition: normalizedUserExplanationCondition(source.considerCondition),
    avoidCondition: normalizedUserExplanationCondition(source.avoidCondition)
  };
}

function normalizedUserExplanationCondition(value = {}) {
  if (typeof value === "string") {
    return { text: value.trim(), terms: [] };
  }
  const source = value && typeof value === "object" ? value : {};
  return {
    text: String(source.text || source.condition || "").trim(),
    terms: Array.isArray(source.terms)
      ? source.terms.map(normalizedUserExplanationTerm).filter((item) => item.term).slice(0, 8)
      : []
  };
}

function normalizedUserExplanationTerm(value = {}) {
  const source = value && typeof value === "object" ? value : {};
  const numericValue = Number(source.value);
  return {
    term: String(source.term || source.name || "").trim(),
    value: Number.isFinite(numericValue) ? numericValue : null,
    meaning: String(source.meaning || "").trim(),
    whyItMatters: String(source.whyItMatters || source.effect || source.reason || "").trim()
  };
}

function plainDecisionAction(decision, plan = {}) {
  if (decision === "buy" && plan.actionable) return "可以按计划小仓试错，不适合脱离止损追高。";
  if (decision === "buy") return "AI 偏买入，但买入条件不完整，先按观察处理。";
  if (decision === "avoid") return "当前不适合买入，先回避。";
  return "先观察，不建议现在追买。";
}

function plainDecisionReasons(decision = {}, evidence = {}) {
  const snapshot = evidence.snapshot || {};
  const daily = evidence.daily || {};
  const dailyMetrics = daily.dailyMetrics || {};
  const valuation = evidence.valuation || {};
  const fundamentals = evidence.fundamentals || {};
  const moneyFlow = evidence.moneyFlow || {};
  const rows = [];
  const change = Number(snapshot.change);
  const volumeRatio = Number(snapshot.volumeRatio);
  const distanceMa20 = Number(dailyMetrics.distanceMa20);
  const distanceMa60 = Number(dailyMetrics.distanceMa60);
  const pe = Number(valuation.peTtm);
  const pb = Number(valuation.pb);
  const profitYoY = Number(fundamentals.netProfitYoY);
  const mainFlow = Number(moneyFlow.mainNetInflow);
  const fiveDayFlow = Number(moneyFlow.fiveDayMainNetInflow);

  if (Number.isFinite(change) || Number.isFinite(volumeRatio)) {
    rows.push(`短线表现：${Number.isFinite(change) ? `今天涨跌幅 ${formatPct(change)}` : "涨跌幅未知"}${Number.isFinite(volumeRatio) ? `，成交活跃度约为平时 ${formatMetric(volumeRatio)} 倍` : ""}。`);
  }
  if (Number.isFinite(distanceMa20) || Number.isFinite(distanceMa60)) {
    rows.push(`趋势位置：${maDistancePlainText(distanceMa20, "20日")}${Number.isFinite(distanceMa60) ? `，${maDistancePlainText(distanceMa60, "60日")}` : ""}。`);
  } else if (daily.available === false) {
    rows.push(`趋势位置：日线数据缺失，不能确认中线趋势。`);
  }
  if (Number.isFinite(mainFlow) || Number.isFinite(fiveDayFlow)) {
    rows.push(`资金状态：${Number.isFinite(mainFlow) ? `当天大资金${mainFlow >= 0 ? "净买入" : "净卖出"} ${formatMoney(Math.abs(mainFlow))}` : "当天大资金未知"}${Number.isFinite(fiveDayFlow) ? `，近5日${fiveDayFlow >= 0 ? "偏流入" : "偏流出"}` : ""}。`);
  }
  if (Number.isFinite(pe) || Number.isFinite(pb) || Number.isFinite(profitYoY)) {
    rows.push(`基本面/估值：${valuationPlainText(pe, pb, profitYoY)}。`);
  }
  if (!rows.length) {
    rows.push(simplifyEvidenceText(decision.decisionText || "证据不够清晰，先按最终结论和风险提示处理。"));
  }
  return rows.slice(0, 4);
}

function maDistancePlainText(value, label) {
  if (!Number.isFinite(value)) return `${label}平均成本未知`;
  if (value >= 0) return `高于近${label}平均成本 ${formatMetric(value)}%`;
  return `低于近${label}平均成本 ${formatMetric(Math.abs(value))}%`;
}

function valuationPlainText(pe, pb, profitYoY) {
  const rows = [];
  if (Number.isFinite(pe)) rows.push(`市盈率 ${formatMetric(pe)}${pe >= 80 ? "，估值偏贵" : ""}`);
  if (Number.isFinite(pb)) rows.push(`市净率 ${formatMetric(pb)}${pb >= 8 ? "，资产估值偏高" : ""}`);
  if (Number.isFinite(profitYoY)) rows.push(`净利润同比${profitYoY >= 0 ? "增长" : "下降"} ${formatMetric(Math.abs(profitYoY))}%`);
  return rows.join("；") || "暂无足够基本面数据";
}

function plainConsiderCondition(decision = {}, plan = {}) {
  if (plan.actionable && displayTradeRange(plan.buyPriceLow, plan.buyPriceHigh) !== "AI 未给出") {
    return `只在 ${displayTradeRange(plan.buyPriceLow, plan.buyPriceHigh)} 附近并满足 AI 买点时考虑，且必须提前设置止损。`;
  }
  const horizons = decision.horizons || {};
  return simplifyEvidenceText(firstNonEmpty(
    horizons.short?.entry,
    horizons.mid?.entry,
    plan.reason,
    "等待价格回到关键均线附近、成交继续活跃、资金连续流入后再评估。"
  ));
}

function plainAvoidCondition(decision = {}) {
  const horizons = decision.horizons || {};
  const risks = Array.isArray(decision.risks) ? decision.risks : [];
  return simplifyEvidenceText(firstNonEmpty(
    horizons.short?.invalid,
    horizons.mid?.invalid,
    risks[0],
    "冲高回落、资金不能连续流入，或中线趋势继续走弱时继续回避。"
  ));
}

function renderPlainTermChips(evidence = {}) {
  const terms = plainTermChips(evidence);
  if (!terms.length) return "";
  return `
    <div class="plain-term-chips">
      ${terms.map((item) => `
        <span>
          <b>${escapeHtml(item.label)}</b>
          <small>${escapeHtml(item.text)}</small>
        </span>
      `).join("")}
    </div>
  `;
}

function plainTermChips(evidence = {}) {
  const snapshot = evidence.snapshot || {};
  const daily = evidence.daily || {};
  const dailyMetrics = daily.dailyMetrics || {};
  const valuation = evidence.valuation || {};
  const moneyFlow = evidence.moneyFlow || {};
  const rows = [];
  const volumeRatio = Number(snapshot.volumeRatio);
  const distanceMa20 = Number(dailyMetrics.distanceMa20);
  const pe = Number(valuation.peTtm);
  const mainFlow = Number(moneyFlow.mainNetInflow);
  if (Number.isFinite(volumeRatio)) {
    rows.push({ label: "量比", text: `当前成交活跃度约为平时 ${formatMetric(volumeRatio)} 倍，越高说明资金越活跃，也可能更容易短线过热。` });
  }
  if (Number.isFinite(distanceMa20)) {
    rows.push({ label: "MA20", text: `近20个交易日平均成本；当前${distanceMa20 >= 0 ? "在其上方" : "在其下方"}，用于判断中线是否修复。` });
  }
  if (Number.isFinite(mainFlow)) {
    rows.push({ label: "主力净流入", text: `大资金当天${mainFlow >= 0 ? "净买入" : "净卖出"}，但单日流入不能单独当成买入理由。` });
  }
  if (Number.isFinite(pe)) {
    rows.push({ label: "PE", text: `市盈率，粗略表示市场给利润的定价；数值过高时更怕业绩跟不上。` });
  }
  return rows.slice(0, 4);
}

function renderTradingTermHints(text, aiTerms = []) {
  const hints = tradingTermHintsFromText(text, aiTerms);
  if (!hints.length) return "";
  return `
    <div class="trading-term-hints">
      ${hints.map((item) => `
        <span>
          <b>${escapeHtml(item.label)}</b>
          <small>${escapeHtml(item.text)}</small>
        </span>
      `).join("")}
    </div>
  `;
}

function tradingTermHintsFromText(value, aiTerms = []) {
  const text = String(value || "");
  const aiHints = (aiTerms || [])
    .filter((item) => item && item.term)
    .map((item) => ({
      label: termLabelWithValue(item),
      text: [item.meaning, item.whyItMatters].filter(Boolean).join("；") || "AI 已标记该术语，但未给出完整解释。"
    }));
  const rules = [
    {
      test: /VWAP/i,
      label: "VWAP",
      text: "当天平均成交成本线。价格站上它，说明短线买盘愿意在平均成本上方承接。"
    },
    {
      test: /MACD|金叉|死叉/i,
      label: "MACD",
      text: "短线动能指标。金叉偏转强，死叉偏转弱，但必须结合成交量和趋势位置看。"
    },
    {
      test: /放量|量能|量比/,
      label: "放量/量比",
      text: "成交明显变活跃，说明资金关注增加；过高时也可能代表短线情绪过热。"
    },
    {
      test: /回踩|不破/,
      label: "回踩不破",
      text: "价格下跌测试支撑位但没有跌穿，表示下方可能有人承接。"
    },
    {
      test: /早盘高点|早盘低点|早盘/,
      label: "早盘高低点",
      text: "上午形成的关键价格。跌破低点常代表承接变弱，突破高点说明短线更强。"
    },
    {
      test: /MA20|MA60|均线/,
      label: "MA20/MA60",
      text: "近20/60个交易日平均成本。站回上方，通常代表中线趋势开始修复。"
    },
    {
      test: /止损|失效/,
      label: "止损/失效",
      text: "提前定义判断错了的位置；跌破后应按计划退出或停止观察。"
    },
    {
      test: /主力|资金|流入|流出/,
      label: "资金流",
      text: "观察大资金当天偏买还是偏卖；单日流入不能单独当成买入理由。"
    }
  ];
  const fallbackHints = rules.filter((item) => item.test.test(text));
  const merged = [...aiHints, ...fallbackHints].filter((item, index, rows) => (
    rows.findIndex((row) => String(row.label || "").split("：")[0] === String(item.label || "").split("：")[0]) === index
  ));
  return merged.slice(0, 5);
}

function termLabelWithValue(item = {}) {
  const value = Number(item.value);
  return Number.isFinite(value) ? `${item.term}：${formatMetric(value)}` : item.term;
}

function renderDecisionOverview(payload = {}, decision = {}, evidence = {}, positionContext = {}) {
  const plan = decision.tradePlan || decision.newPositionPlan || {};
  const validation = decision.tradePlanValidation || {};
  const confidence = Number(decision.confidence || 0);
  const primaryHorizon = decision.primaryHorizon || plan.horizon || "none";
  const summaryRows = renderDecisionSummaryRows(decision, plan);
  return `
    <section class="stock-decision-overview ${decisionClass(decision.decision)}">
      <div class="stock-decision-main">
        <span>当前判断</span>
        <strong>${escapeHtml(decision.decisionText || decisionLabel(decision.decision))}</strong>
        <small>${escapeHtml(horizonLabel(primaryHorizon))} · 置信 ${confidence}% · ${escapeHtml(positionScenarioLabel(positionContext, decision))}</small>
        ${renderAnalysisSourceBadges(payload)}
      </div>
      <div class="stock-decision-action-grid">
        ${renderDecisionActionTile("买入区间", displayTradeRange(plan.buyPriceLow, plan.buyPriceHigh), plan.actionable ? "可执行买点" : "等待触发")}
        ${renderDecisionActionTile("止损", displayTradePrice(plan.stopLossPrice), "风险底线")}
        ${renderDecisionActionTile("止盈", displayTradePrice(plan.takeProfitPrice), "目标区")}
        ${renderDecisionActionTile("后端校验", tradeValidationShortLabel(validation.grade), displayText(validation.reason || validation.summary, "交易计划校验"))}
      </div>
      ${summaryRows.length ? `<div class="stock-decision-summary">${summaryRows.map((line) => `<p>${escapeHtml(line)}</p>`).join("")}</div>` : ""}
      <div class="stock-decision-links">
        <a class="secondary-button" href="./intraday-entry-audit-history.html?stock=${encodeURIComponent(payload.code || "")}">进场审计</a>
      </div>
    </section>
  `;
}

function renderDecisionSummaryRows(decision = {}, plan = {}) {
  const summary = Array.isArray(decision.summary) ? decision.summary : [];
  return [
    ...summary,
    plan.reason,
    decision.battle?.conclusion
  ].filter(Boolean).map(String).filter((item, index, rows) => rows.indexOf(item) === index).slice(0, 3);
}

function renderAnalysisSourceBadges(payload = {}) {
  const sourceClass = payload.ruleFallbackUsed ? "fallback" : payload.aiMode === "deepseek" ? "ai" : "rules";
  const sourceText = payload.ruleFallbackUsed ? "本地规则兜底" : `${sourceName(payload.aiMode)} 分析`;
  const modelText = [payload.aiProvider, payload.aiModel].filter(Boolean).join(" · ");
  return `
    <div class="stock-source-badges">
      <span class="${sourceClass}">${escapeHtml(sourceText)}</span>
      ${modelText ? `<span>${escapeHtml(modelText)}</span>` : ""}
    </div>
  `;
}

function renderDecisionActionTile(label, value, sub) {
  return `
    <article>
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(sub || "--")}</small>
    </article>
  `;
}

function positionScenarioLabel(positionContext = {}, decision = {}) {
  const stateValue = positionContext.positionState || decision.positionState || "none";
  return stateValue === "existing" ? "已有仓位场景" : "新开仓场景";
}

function renderHorizonDefinition() {
  const rows = [
    { key: "short", title: "短线", period: "1-5个交易日", desc: "看 VWAP承接、MACD动能、量能、早盘高低点、板块热度和短期资金" },
    { key: "mid", title: "中线", period: "2-8周", desc: "看日K趋势、MA20/MA60结构、均线斜率、量价配合、回撤与突破" },
    { key: "long", title: "长线", period: "3-12个月", desc: "看基本面、估值、行业逻辑、长期资金流和长期趋势" }
  ];
  return `
    <section class="stock-horizon-definition">
      ${rows.map((item) => `
        <article>
          <span>${escapeHtml(item.title)}</span>
          <strong>${escapeHtml(item.period)}</strong>
          <p>${escapeHtml(item.desc)}</p>
        </article>
      `).join("")}
    </section>
  `;
}

function renderStockAnalysisWarnings(payload = {}) {
  const decision = payload.decision || {};
  const warnings = [...(Array.isArray(decision.warnings) ? decision.warnings : [])];
  if (payload.ruleFallbackUsed && payload.fallbackReason) {
    warnings.unshift(`DeepSeek 未完成个股分析，当前为本地规则兜底：${payload.fallbackReason}`);
  } else if (payload.ruleFallbackUsed) {
    warnings.unshift("DeepSeek 未完成个股分析，当前为本地规则兜底。");
  }
  const rows = [...new Set(warnings.filter(Boolean).map(String))];
  if (!rows.length) return "";
  return `
    <section class="stock-analysis-warning-card">
      ${rows.map((line) => `<p>${escapeHtml(line)}</p>`).join("")}
    </section>
  `;
}

function renderTradePlan(plan = {}, validation = {}) {
  const actionable = Boolean(plan.actionable);
  const stopLoss = displayTradePrice(plan.stopLossPrice);
  const takeProfit = displayTradePrice(plan.takeProfitPrice);
  const buyRange = displayTradeRange(plan.buyPriceLow, plan.buyPriceHigh);
  const validationGrade = validation.grade || "";
  const validationLabel = {
    pass: "后端校验通过",
    downgraded: "后端校验未通过，已降级观察",
    not_applicable: "当前不是买入计划"
  }[validationGrade] || "";
  const validationReasons = [
    ...(Array.isArray(validation.hardBlocks) ? validation.hardBlocks : []),
    ...(Array.isArray(validation.warnings) ? validation.warnings : [])
  ];
  return `
    <section class="trade-plan-card ${actionable ? "actionable" : "inactive"}">
      <div class="trade-plan-head">
        <span>新开仓执行计划</span>
        <strong>${actionable ? "可执行买入计划" : "暂无可执行买点"}</strong>
        <small>${escapeHtml(horizonLabel(plan.horizon || "none"))}${plan.positionSuggestion ? ` · ${escapeHtml(plan.positionSuggestion)}` : ""}</small>
      </div>
      ${validationLabel ? `<div class="trade-plan-validation ${validationGrade}"><strong>${escapeHtml(validationLabel)}</strong>${validationReasons.slice(0, 3).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
      <div class="trade-plan-grid">
        <article>
          <span>买入区间</span>
          <strong>${escapeHtml(buyRange)}</strong>
        </article>
        <article>
          <span>止损价</span>
          <strong>${escapeHtml(stopLoss)}</strong>
        </article>
        <article>
          <span>止盈价</span>
          <strong>${escapeHtml(takeProfit)}</strong>
        </article>
        <article>
          <span>风险收益</span>
          <strong>${escapeHtml(plan.riskReward || "--")}</strong>
        </article>
      </div>
      ${plan.sellCondition ? `<p><b>卖出条件：</b>${escapeHtml(plan.sellCondition)}</p>` : ""}
      ${plan.reason ? `<p>${escapeHtml(plan.reason)}</p>` : ""}
    </section>
  `;
}

function renderPositionPlans(decision = {}, positionContext = {}) {
  const newPlan = decision.newPositionPlan;
  const existingPlan = decision.existingPositionPlan;
  if (!newPlan && !existingPlan) return "";
  const positionState = positionContext.positionState || decision.positionState || "none";
  const positionInfo = positionContext.positionInfo || {};
  return `
    <section class="position-plan-section">
      <h3>仓位处理计划</h3>
      <div class="position-plan-grid">
        ${renderNewPositionPlan(newPlan || {})}
        ${renderExistingPositionPlan(existingPlan || {}, positionState, positionInfo)}
      </div>
    </section>
  `;
}

function renderNewPositionPlan(plan = {}) {
  const actionable = Boolean(plan.actionable);
  const buyRange = displayTradeRange(plan.buyPriceLow, plan.buyPriceHigh);
  const minHoldingDays = Number(plan.minHoldingDays || 1);
  const t1Label = plan.canSellToday ? "允许当日卖出" : `最短持有 ${minHoldingDays} 个交易日`;
  const evidenceRows = Array.isArray(plan.entryEvidence) ? plan.entryEvidence.filter(Boolean).slice(0, 3) : [];
  return `
    <article class="position-plan-card ${actionable ? "actionable" : "inactive"}">
      <header>
        <span>新开仓计划</span>
        <strong>${actionable ? "新买入可执行" : "新买入不可执行"}</strong>
        <small>${escapeHtml(horizonLabel(plan.horizon || "none"))} · ${escapeHtml(t1Label)}</small>
      </header>
      <div class="position-plan-metrics">
        <div><span>买入区间</span><strong>${escapeHtml(buyRange)}</strong></div>
        <div><span>止损</span><strong>${escapeHtml(displayTradePrice(plan.stopLossPrice))}</strong></div>
        <div><span>止盈</span><strong>${escapeHtml(displayTradePrice(plan.takeProfitPrice))}</strong></div>
        <div><span>风险收益</span><strong>${escapeHtml(displayText(plan.riskReward))}</strong></div>
      </div>
      ${plan.nextDayStopPlan ? `<p><b>T+1 退出：</b>${escapeHtml(plan.nextDayStopPlan)}</p>` : ""}
      ${plan.invalidCondition ? `<p><b>失效：</b>${escapeHtml(plan.invalidCondition)}</p>` : ""}
      ${plan.reason ? `<p>${escapeHtml(plan.reason)}</p>` : ""}
      ${evidenceRows.length ? `<div class="position-plan-tags">${evidenceRows.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
    </article>
  `;
}

function renderExistingPositionPlan(plan = {}, positionState = "none", positionInfo = {}) {
  if (positionState !== "existing") {
    return `
      <article class="position-plan-card existing inactive">
        <header>
          <span>已有仓位处理</span>
          <strong>未录入持仓</strong>
          <small>本次按新开仓场景分析</small>
        </header>
        <p>没有成本价、仓位比例和持有天数，系统不会生成真实持仓处理动作。</p>
      </article>
    `;
  }
  const action = existingPositionActionLabel(plan.action);
  const plannedHorizon = positionInfo.plannedHorizon ? horizonLabel(positionInfo.plannedHorizon) : "未指定";
  return `
    <article class="position-plan-card existing">
      <header>
        <span>已有仓位处理</span>
        <strong>${escapeHtml(action)}</strong>
        <small>已有仓位 · ${plan.canSellToday ? "可按持仓规则盘中处理" : "不安排当日卖出"}</small>
      </header>
      <div class="position-plan-metrics input">
        <div><span>成本价</span><strong>${escapeHtml(formatTradePrice(positionInfo.costPrice))}</strong></div>
        <div><span>仓位</span><strong>${escapeHtml(formatPositionPct(positionInfo.positionPct))}</strong></div>
        <div><span>持有天数</span><strong>${Number.isFinite(Number(positionInfo.holdingDays)) ? `${Number(positionInfo.holdingDays)} 天` : "--"}</strong></div>
        <div><span>计划周期</span><strong>${escapeHtml(plannedHorizon)}</strong></div>
      </div>
      <div class="position-plan-metrics">
        <div><span>止损</span><strong>${escapeHtml(displayTradePrice(plan.stopLossPrice))}</strong></div>
        <div><span>止盈</span><strong>${escapeHtml(displayTradePrice(plan.takeProfitPrice))}</strong></div>
        <div><span>风险等级</span><strong>${escapeHtml(existingPositionRiskLabel(plan.riskLevel))}</strong></div>
        <div><span>仓位建议</span><strong>${escapeHtml(displayText(plan.positionSuggestion))}</strong></div>
      </div>
      ${plan.intradayAction ? `<p><b>盘中动作：</b>${escapeHtml(plan.intradayAction)}</p>` : ""}
      ${plan.sellCondition ? `<p><b>卖出条件：</b>${escapeHtml(plan.sellCondition)}</p>` : ""}
      ${plan.reason ? `<p>${escapeHtml(plan.reason)}</p>` : ""}
    </article>
  `;
}

function displayTradeRange(low, high) {
  const buyLow = formatTradePrice(low);
  const buyHigh = formatTradePrice(high);
  return buyLow !== "--" && buyHigh !== "--" ? `${buyLow} - ${buyHigh}` : "AI 未给出";
}

function displayTradePrice(value) {
  const formatted = formatTradePrice(value);
  return formatted === "--" ? "AI 未给出" : formatted;
}

function displayText(value, emptyText = "AI 未给出") {
  const text = String(value ?? "").trim();
  return text || emptyText;
}

function tradeValidationShortLabel(grade) {
  return {
    pass: "通过",
    downgraded: "已降级",
    not_applicable: "非买入"
  }[grade] || "未校验";
}

function existingPositionActionLabel(value) {
  return {
    hold: "继续持有",
    watch: "观察处理",
    reduce: "减仓",
    sell: "卖出",
    stop_loss: "止损",
    take_profit: "止盈"
  }[value] || "观察处理";
}

function existingPositionRiskLabel(value) {
  return {
    low: "低",
    medium: "中",
    high: "高"
  }[value] || "--";
}

function renderCaseMemoryStatus(status = {}, reviewStatus = {}) {
  const mode = status.mode || "auto";
  const sampleCount = Number(status.effectiveSampleCount || 0);
  const minSamples = Number(status.minSamples || 800);
  const injected = Boolean(status.injected);
  const promptEffectLabel = injected ? "已影响本次 Prompt" : "未影响本次 Prompt";
  const label = caseMemoryLabel(status.status, injected);
  const similarCases = status.similarCases || [];
  const falseBuy = status.falseBuyGuard || {};
  const review = reviewStatus || status.reviewMemoryStatus || {};
  const byHorizon = status.byHorizon || {};
  return `
    <section class="case-memory-card ${injected ? "injected" : "preview"}">
      <div class="case-memory-head">
        <div>
          <span>案例学习</span>
          <strong>${escapeHtml(label)}</strong>
        </div>
        <small>${escapeHtml(mode)} · ${escapeHtml(promptEffectLabel)} · 样本 ${sampleCount}/${minSamples}${status.bestSimilarity ? ` · 相似度 ${Number(status.bestSimilarity).toFixed(2)}` : ""}</small>
      </div>
      <div class="case-memory-grid">
        <span>相似案例 ${Number(status.matchedCaseCount || similarCases.length)}</span>
        <span>误买率 ${formatNullablePct(falseBuy.recentFalseBuyRatePct)} / 基准 ${formatNullablePct(falseBuy.baselineFalseBuyRatePct)}</span>
        <span>复盘经验 ${Number(review.activeLessonCount || 0)} 条${review.injected ? " · 已注入" : ""}</span>
      </div>
      ${renderCaseMemoryPools(byHorizon, minSamples)}
      ${similarCases.length ? `
        <div class="case-memory-list">
          ${similarCases.slice(0, 3).map((item) => `
            <article>
              <strong>${escapeHtml(item.name || "--")} ${escapeHtml(item.code || "")}</strong>
              <span>${escapeHtml(caseLabelName(item.label))} · ${escapeHtml(item.analysisDate || "--")} · 相似 ${Number(item.similarity || 0).toFixed(2)}</span>
              <p>${escapeHtml(item.lesson || "--")}</p>
            </article>
          `).join("")}
        </div>
      ` : ""}
    </section>
  `;
}

function renderCaseMemoryPools(byHorizon = {}, minSamples = 800) {
  const rows = ["short", "mid", "long"].map((key) => byHorizon?.[key] ? { key, ...byHorizon[key] } : null).filter(Boolean);
  if (!rows.length) return "";
  return `
    <div class="case-memory-pools">
      ${rows.map((item) => `
        <span class="${item.injected ? "injected" : ""}">
          <b>${escapeHtml(horizonBucketName(item.key))}</b>
          <strong>${Number(item.effectiveSampleCount || 0)}/${Number(minSamples || 0)}</strong>
          <small>${escapeHtml(caseMemoryPoolStatusName(item.status))} · ${item.injected ? "已注入 Prompt" : "未注入 Prompt"}</small>
          <em>相似 ${Number(item.matchedCaseCount || 0)} · 经验 ${Number(item.reviewMemoryStatus?.activeLessonCount || 0)}</em>
        </span>
      `).join("")}
    </div>
  `;
}

function caseMemoryPoolStatusName(value) {
  return {
    disabled: "停用",
    collecting: "收集中",
    available: "可用未注入",
    insufficient_match: "缺少相似案例",
    injected: "已注入"
  }[value] || value || "--";
}

function caseMemoryLabel(status, injected) {
  if (injected) return "已参考历史样本";
  return {
    disabled: "未启用案例学习",
    collecting: "样本收集中，暂未影响结论",
    available: "已有相似案例，仅预览展示",
    insufficient_match: "样本足够但缺少相似案例",
    injected: "已参考历史样本"
  }[status] || "样本状态未知";
}

function formatNullablePct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number.toFixed(2)}%`;
}

function caseLabelName(value) {
  return {
    correct_buy: "正确买入",
    false_buy: "误买",
    missed_opportunity: "漏判机会",
    correct_avoid: "正确规避",
    early_watch: "偏早观察",
    risk_saved: "风险规避",
    neutral_buy: "买入待验证",
    neutral_watch: "观察待验证",
    mid_direction_positive: "中线方向正确",
    mid_direction_negative: "中线方向失败",
    mid_direction_neutral: "中线待验证",
    mid_direction_missed: "中线漏判",
    mid_direction_protected: "中线风险规避",
    long_direction_positive: "长线方向正确",
    long_direction_negative: "长线方向失败",
    long_direction_neutral: "长线待验证",
    long_direction_missed: "长线漏判",
    long_direction_protected: "长线风险规避"
  }[value] || value || "未标记";
}

function renderBattle(battle = {}) {
  const bias = battle.bias || "neutral";
  const bullScore = Number(battle.bullScore || 0);
  const bearScore = Number(battle.bearScore || 0);
  return `
    <section class="stock-battle ${battleClass(bias)}">
      <div class="battle-summary">
        <span>多空博弈</span>
        <strong>${escapeHtml(battle.label || "多空拉锯")}</strong>
        <small>多方 ${bullScore} · 空方 ${bearScore} · ${escapeHtml(battleScoreSourceLabel(battle))}</small>
        <div class="battle-bars" aria-hidden="true">
          <i class="bull" style="width:${Math.max(4, Math.min(100, bullScore))}%"></i>
          <i class="bear" style="width:${Math.max(4, Math.min(100, bearScore))}%"></i>
        </div>
        ${renderBattleScoreMeta(battle)}
      </div>
      ${renderBattleHorizonScores(battle.horizonScores)}
      ${renderBattleRoleViews(battle.roleViews)}
      ${renderBattleDimensions(battle.dimensions)}
      <div class="battle-list bull">
        <h3>看多理由</h3>
        ${renderBattleReasons(battle.bullishReasons, "暂无足够看多证据")}
      </div>
      <div class="battle-list bear">
        <h3>看空理由</h3>
        ${renderBattleReasons(battle.bearishReasons, "暂无明显看空证据")}
      </div>
      <div class="battle-conclusion">
        <span>结论</span>
        <p>${escapeHtml(battle.conclusion || "证据不足，等待更多数据确认。")}</p>
      </div>
    </section>
  `;
}

function renderBattleScoreMeta(battle = {}) {
  const meta = battle.scoreMeta || {};
  if (!meta.calibrated) return "";
  const rawBull = Number(meta.rawBullScore || 0);
  const rawBear = Number(meta.rawBearScore || 0);
  const horizon = horizonLabel(battle.scoreHorizon || meta.selectedHorizon || "mid");
  return `<em class="battle-score-note">按${escapeHtml(horizon)}角色权重计算 · AI原始 ${rawBull}/${rawBear}</em>`;
}

function renderBattleHorizonScores(scores = {}) {
  const rows = ["short", "mid", "long"].map((key) => scores && scores[key]).filter(Boolean);
  if (!rows.length) return "";
  return `
    <div class="battle-horizon-scores">
      ${rows.map((item) => {
        const bull = Number(item.bullScore || 0);
        const bear = Number(item.bearScore || 0);
        return `
          <article class="${battleClass(item.bias)}">
            <div>
              <span>${escapeHtml(horizonLabel(item.horizon))}</span>
              <strong>多 ${bull} / 空 ${bear}</strong>
            </div>
            <small>${escapeHtml(item.label || "多空拉锯")}</small>
            <div class="battle-mini-bars" aria-hidden="true">
              <i class="bull" style="width:${Math.max(4, Math.min(100, bull))}%"></i>
              <i class="bear" style="width:${Math.max(4, Math.min(100, bear))}%"></i>
            </div>
          </article>
        `;
      }).join("")}
    </div>
  `;
}

function renderBattleRoleViews(roleViews = []) {
  const rows = (roleViews || []).filter((item) => item && item.role);
  if (!rows.length) return "";
  return `
    <div class="battle-roles">
      ${rows.map((item) => {
        const summary = battleRoleUserSummary(item);
        const evidenceRows = (item.keyEvidence || []).filter(Boolean).slice(0, 4);
        return `
        <article class="${roleStanceClass(item.stance)} battle-role-card">
          <div>
            <span>${escapeHtml(item.role)}</span>
            <strong>${escapeHtml(roleStanceLabel(item.stance))}${summary.horizon ? ` · ${escapeHtml(summary.horizon)}` : ""}</strong>
          </div>
          <p class="battle-role-thesis">${escapeHtml(summary.thesis)}</p>
          <dl class="battle-role-summary">
            <div>
              <dt>用户理解</dt>
              <dd>${escapeHtml(summary.userMeaning)}</dd>
            </div>
            <div>
              <dt>适合操作</dt>
              <dd>${escapeHtml(summary.action)}</dd>
            </div>
            <div>
              <dt>关键条件</dt>
              <dd>${escapeHtml(summary.condition)}</dd>
            </div>
            <div>
              <dt>主要风险</dt>
              <dd>${escapeHtml(summary.risk)}</dd>
            </div>
          </dl>
          <details class="battle-role-details">
            <summary>专业证据</summary>
            ${item.bullCase ? `<small><b>看多：</b>${escapeHtml(item.bullCase)}</small>` : ""}
            ${item.bearCase ? `<small><b>看空：</b>${escapeHtml(item.bearCase)}</small>` : ""}
            ${evidenceRows.length ? evidenceRows.map((line) => `<em>${escapeHtml(line)}</em>`).join("") : `<em>暂无额外指标明细</em>`}
          </details>
        </article>
      `;
      }).join("")}
    </div>
  `;
}

function battleRoleUserSummary(item = {}) {
  const stance = roleStanceClass(item.stance);
  const horizon = horizonLabel(item.horizon);
  const thesis = firstNonEmpty(item.thesis, item.action, "该角色没有给出完整观点。");
  return {
    horizon,
    thesis: simplifyEvidenceText(thesis),
    userMeaning: battleRoleMeaning(stance, item.horizon),
    action: simplifyActionText(item.action, stance),
    condition: simplifyEvidenceText(firstNonEmpty(item.bullCase, item.thesis, item.action, "等待价格、量能和趋势继续确认。")),
    risk: simplifyEvidenceText(firstNonEmpty(item.risk, item.bearCase, "暂无明确风险，但仍需按最终交易计划控制仓位和止损。"))
  };
}

function battleRoleMeaning(stance, horizon) {
  const period = horizonPeriodLabel(horizon) || "对应周期";
  if (stance === "bullish") return `该角色认为 ${period} 机会大于风险，但不能单独当成买入结论。`;
  if (stance === "bearish") return `该角色认为 ${period} 风险偏高，需要先回避或等待修复。`;
  return `该角色认为 ${period} 证据还不够一致，适合先观察。`;
}

function simplifyActionText(value, stance) {
  const text = simplifyEvidenceText(value);
  if (text) return text;
  if (stance === "bullish") return "可以关注低风险买点，但必须以最终买入区间、止损和仓位计划为准。";
  if (stance === "bearish") return "不建议追买，等风险解除后再重新评估。";
  return "先观察，不急于下单。";
}

function expandTradingTermsInline(value, context = {}) {
  const text = String(value || "").trim();
  if (!text) return "";
  const terms = inferredTradingTermValues(context.evidence, context.decision, context.terms);
  return text
    .replace(/VWAP\s*[=：:]?\s*([0-9]+(?:\.[0-9]+)?)/gi, "VWAP（当天平均成交成本线，当前约 $1）")
    .replace(/VWAP(?!（)/gi, terms.vwap ? `VWAP（当天平均成交成本线，当前约 ${formatMetric(terms.vwap)}）` : "VWAP（当天平均成交成本线）")
    .replace(/MA20\s*[=：:]?\s*([0-9]+(?:\.[0-9]+)?)/g, "MA20（近20日平均成本线，当前约 $1）")
    .replace(/MA60\s*[=：:]?\s*([0-9]+(?:\.[0-9]+)?)/g, "MA60（近60日平均成本线，当前约 $1）")
    .replace(/MACD金叉(?!（)/g, "MACD金叉（短线动能转强信号）")
    .replace(/MACD死叉(?!（)/g, "MACD死叉（短线动能转弱信号）")
    .replace(/MACD走强(?!（)/g, "MACD走强（短线动能改善）")
    .replace(/MACD走弱(?!（)/g, "MACD走弱（短线动能转弱）")
    .replace(/MA20(?!（)/g, "MA20（近20日平均成本线）")
    .replace(/MA60(?!（)/g, "MA60（近60日平均成本线）")
    .replace(/量比(?!（)/g, "量比（成交活跃度）")
    .replace(/放量(?!（)/g, "放量（成交明显变活跃）")
    .replace(/回踩(?!（)/g, "回踩（下跌测试支撑）")
    .replace(/不破(?!（)/g, "不破（没有跌穿支撑）");
}

function inferredTradingTermValues(evidence = {}, decision = {}, aiTerms = []) {
  const intraday = evidence?.intraday || {};
  const rows = [];
  const aiVwap = (aiTerms || []).find((item) => /vwap/i.test(String(item?.term || "")) && Number.isFinite(Number(item?.value)));
  if (aiVwap) {
    return { vwap: Number(aiVwap.value) };
  }
  rows.push(intraday.vwap, intraday.VWAP);
  const battle = decision?.battle || {};
  (battle.roleViews || []).forEach((item) => {
    rows.push(item?.thesis, item?.bullCase, item?.bearCase, item?.risk, item?.action);
    (item?.keyEvidence || []).forEach((line) => rows.push(line));
  });
  rows.push(...(Array.isArray(decision.supportingEvidence) ? decision.supportingEvidence : []));
  const text = rows.filter(Boolean).map(String).join(" ");
  const vwapMatch = text.match(/VWAP\s*[=：:]?\s*([0-9]+(?:\.[0-9]+)?)/i);
  return {
    vwap: vwapMatch ? Number(vwapMatch[1]) : Number.isFinite(Number(intraday.vwap)) ? Number(intraday.vwap) : null
  };
}

function simplifyEvidenceText(value) {
  const text = expandTradingTermsInline(value);
  if (!text) return "";
  return text
    .replace(/\bshort\b/g, "短线")
    .replace(/\bmid\b/g, "中线")
    .replace(/\blong\b/g, "长线")
    .replace(/\ball\b/g, "全部周期")
    .replace(/\bintraday\.score\b/g, "15分钟强度")
    .replace(/\bintraday\.checks\b/g, "15分钟检查项")
    .replace(/\bdaily\.score\b/g, "日线趋势分")
    .replace(/\bdaily\.status\b/g, "日线状态")
    .replace(/\bdaily\.riskFlags\b/g, "日线风险")
    .replace(/\bdaily\.dailyMetrics\b/g, "日线数据")
    .replace(/\bdaily\.dailyTrend\b/g, "日线趋势")
    .replace(/\bvaluation\.peTtm\b/g, "市盈率")
    .replace(/\bvaluation\.pb\b/g, "市净率")
    .replace(/\bfundamentals\.netProfitYoY\b/g, "净利润同比")
    .replace(/\bfundamentals\.revenueYoY\b/g, "营收同比")
    .replace(/\bfundamentals\.roe\b/g, "净资产收益率")
    .replace(/\bmoneyFlow\.mainNetInflow\b/g, "主力净流入")
    .replace(/\bmoneyFlow\.fiveDayMainNetInflow\b/g, "近5日主力净流入")
    .replace(/\bprice\b/g, "现价")
    .replace(/\bvolumeRatio\b/g, "量比");
}

function firstNonEmpty(...values) {
  return values.map((value) => String(value || "").trim()).find(Boolean) || "";
}

function renderBattleDimensions(dimensions = []) {
  const rows = (dimensions || []).filter((item) => item && item.label);
  if (!rows.length) return "";
  return `
    <div class="battle-dimensions">
      ${rows.map((item) => `
        <article class="${battleClass(item.bias)}">
          <div>
            <span>${escapeHtml(item.label)}</span>
            <strong>多 ${Number(item.bullScore || 0)} / 空 ${Number(item.bearScore || 0)}</strong>
          </div>
          ${(item.evidence || []).slice(0, 2).map((line) => `<p>${escapeHtml(line)}</p>`).join("")}
        </article>
      `).join("")}
    </div>
  `;
}

function battleSourceLabel(value) {
  return value === "deepseek-role-debate" ? "DeepSeek 角色辩论" : "本地规则兜底";
}

function battleScoreSourceLabel(battle = {}) {
  if ((battle.scoreMeta || {}).calibrated) {
    return `${battleSourceLabel(battle.source)} · 角色权重`;
  }
  return battleSourceLabel(battle.source);
}

function roleStanceLabel(value) {
  return {
    bullish: "偏多",
    bearish: "偏空",
    neutral: "中性",
    buy: "偏多",
    watch: "中性",
    avoid: "偏空"
  }[value] || "中性";
}

function roleStanceClass(value) {
  if (value === "bullish" || value === "buy") return "bullish";
  if (value === "bearish" || value === "avoid") return "bearish";
  return "neutral";
}

function renderBattleReasons(items, emptyText) {
  const rows = (items || []).filter(Boolean);
  if (!rows.length) return `<p>${escapeHtml(emptyText)}</p>`;
  return rows.map(renderBattleReason).join("");
}

function renderBattleReason(item) {
  if (!item || typeof item !== "object") {
    return `<p>${escapeHtml(cleanUserFacingEvidenceText(String(item || "")))}</p>`;
  }
  const title = item.title || item.label || "理由";
  const detail = item.detail || item.reason || "";
  const strength = item.strength || "";
  const evidenceType = item.evidenceType || item.type || "";
  const caveat = item.caveat || "";
  const sourceRefs = extractEvidenceFieldRefs([title, detail, caveat, evidenceType].join(" "));
  return `
    <article class="battle-reason-card">
      <div>
        <strong>${escapeHtml(cleanUserFacingEvidenceText(title))}</strong>
        <span>${[strength, cleanUserFacingEvidenceText(evidenceType)].filter(Boolean).map(escapeHtml).join(" · ")}</span>
      </div>
      ${detail ? `<p>${escapeHtml(cleanUserFacingEvidenceText(detail))}</p>` : ""}
      ${caveat ? `<small>${escapeHtml(cleanUserFacingEvidenceText(caveat))}</small>` : ""}
      ${sourceRefs.length ? `
        <details class="battle-field-refs">
          <summary>字段来源</summary>
          <em>${escapeHtml(sourceRefs.join("、"))}</em>
        </details>
      ` : ""}
    </article>
  `;
}

function cleanUserFacingEvidenceText(value) {
  return simplifyEvidenceText(String(value || "")
    .replace(/（\s*[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+\s*）/g, "")
    .replace(/\(\s*[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+\s*\)/g, "")
    .replace(/[，,；;。]?\s*[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+\s*=\s*[-+]?\d+(?:\.\d+)?%?/g, "")
    .replace(/[，,；;。]?\s*[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+/g, "")
    .replace(/\s{2,}/g, " ")
    .replace(/（\s*）|\(\s*\)/g, "")
    .trim());
}

function extractEvidenceFieldRefs(value) {
  const matches = String(value || "").match(/[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+/g) || [];
  return [...new Set(matches)].slice(0, 5);
}

function battleClass(value) {
  return value === "bullish" ? "bullish" : value === "bearish" ? "bearish" : "neutral";
}

function renderHorizonCard(key, item = {}) {
  const stance = item.stance || "watch";
  const entryLabel = horizonEntryLabel(stance);
  const invalidLabel = horizonInvalidLabel(stance);
  return `
    <article class="stock-horizon ${stanceClass(stance)}">
      <span>${horizonLabel(key)} · ${horizonPeriodLabel(key)}</span>
      <strong>${escapeHtml(item.label || stanceLabel(stance))}</strong>
      <p>${escapeHtml(simplifyEvidenceText(item.reason || "--"))}</p>
      <small>${entryLabel}：${escapeHtml(simplifyEvidenceText(item.entry || "--"))}</small>
      <small>${invalidLabel}：${escapeHtml(simplifyEvidenceText(item.invalid || "--"))}</small>
    </article>
  `;
}

function horizonEntryLabel(stance) {
  if (stance === "buy") return "入场";
  if (stance === "avoid") return "重评";
  return "确认";
}

function horizonInvalidLabel(stance) {
  if (stance === "buy") return "失效";
  if (stance === "avoid") return "继续回避";
  return "观察失败";
}

function renderStockMetrics(evidence) {
  const snapshot = evidence.snapshot || {};
  const intraday = evidence.intraday || {};
  const daily = evidence.daily || {};
  const market = evidence.market || {};
  const sector = evidence.sectorHeat || {};
  const context = market.context || {};
  const contextLabel = market.contextLabel || context.scopeLabel || (context.scope === "full_market" ? "全市场缓存" : "重点池样本");
  const valuation = evidence.valuation || {};
  const fundamentals = evidence.fundamentals || {};
  const moneyFlow = evidence.moneyFlow || {};
  const dailyMetrics = daily.dailyMetrics || {};
  const volumeRatio = Number(snapshot.volumeRatio);
  const distanceMa20 = Number(dailyMetrics.distanceMa20);
  const pe = Number(valuation.peTtm);
  const pb = Number(valuation.pb);
  const rows = [
    { label: "现价/涨跌", value: `${formatMetric(snapshot.price)} / ${formatPct(Number(snapshot.change || 0))}`, sub: `今天价格表现；换手 ${formatMetric(snapshot.turnover)}% 表示成交活跃度` },
    { label: "成交活跃度", value: Number.isFinite(volumeRatio) ? `${formatMetric(volumeRatio)} 倍` : "--", sub: "量比：当前成交相对平时的放大程度，高了说明资金关注度上升" },
    { label: "短线动能", value: intraday.score ?? "--", sub: `${intraday.style || "--"}；用于判断几天内是否有人接力` },
    { label: "中线趋势", value: daily.score ?? "--", sub: daily.available === false ? daily.warning || "日 K 缺失" : `${maDistancePlainText(distanceMa20, "20日")}；20日表现 ${formatPct(Number(dailyMetrics.return20d || 0))}` },
    { label: "市场风向", value: market.mode || "--", sub: `${contextLabel}里 ${market.breadth?.up || 0} 只上涨、${market.breadth?.down || 0} 只下跌，涨停 ${market.limitUp || 0} 只` },
    { label: "市场数据", value: contextLabel, sub: `${sourceName(context.source || market.contextSource)} · 样本 ${Number(context.count || market.contextRows || 0)} 只 · ${formatContextAge(context.ageSeconds)}${context.stale ? " · 缓存偏旧" : ""}` },
    { label: "板块热度", value: sector.currentSector || "--", sub: `${sector.scopeLabel || contextLabel}排名第 ${sector.currentRank || "--"}；板块越靠前，短线情绪越强` },
    { label: "估值压力", value: Number.isFinite(pe) ? `PE ${formatMetric(pe)}` : "--", sub: `PB ${formatMetric(pb)}；PE/PB 越高，越需要业绩兑现支撑` },
    { label: "大资金", value: formatMoney(moneyFlow.mainNetInflow), sub: `主力净占比 ${formatMetric(moneyFlow.mainNetInflowRatio)}%；看资金当天是偏买入还是偏卖出` },
    { label: "证据状态", value: evidence.missingData?.length ? "有缺失" : "可用", sub: evidence.missingData?.slice(0, 2).join("、") || "基础证据已返回" }
  ];
  return rows
    .map(
      (item) => `
        <article>
          <span>${escapeHtml(item.label)}</span>
          <strong>${escapeHtml(item.value)}</strong>
          <small>${escapeHtml(item.sub)}</small>
        </article>
      `
    )
    .join("");
}

function formatContextAge(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "未知";
  if (seconds < 60) return `${Math.round(seconds)}秒前`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}分钟前`;
  return `${(seconds / 3600).toFixed(1)}小时前`;
}

function renderTextList(items, emptyText) {
  const rows = (items || []).filter(Boolean);
  if (!rows.length) return `<div class="stock-list-empty">${escapeHtml(emptyText)}</div>`;
  return `
    <div class="stock-text-list">
      ${rows.map((item) => `<p>${escapeHtml(item)}</p>`).join("")}
    </div>
  `;
}

function renderStockNews(evidence) {
  const items = [
    ...(evidence.news || []).map((item) => ({ ...item, type: item.type || "新闻" })),
    ...(evidence.announcements || []).map((item) => ({ ...item, type: item.type || "公告" }))
  ].slice(0, 6);
  if (!items.length) {
    return renderCatalyst({ type: "暂无", title: "当前没有可用新闻公告证据", time: "" });
  }
  return items.map(renderCatalyst).join("");
}

async function loadAiReviewStats({ keepWarning = false } = {}) {
  if (!apiEnabled) return;
  state.aiReviewLoading = true;
  renderAiReviewPage();
  try {
    const response = await fetch("/api/stock/case-memory/stats?version=v2");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.aiReviewStats = payload;
    if (els.aiReviewWarning && !keepWarning) els.aiReviewWarning.hidden = true;
  } catch (error) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = `AI复盘状态读取失败：${error.message}`;
    }
  } finally {
    state.aiReviewLoading = false;
    renderAiReviewPage();
  }
}

async function loadAiReviewRecentOutcomes() {
  if (!apiEnabled) return;
  try {
    const response = await fetch("/api/stock/outcomes/recent?version=v2&limit=50");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.aiReviewOutcomeResult = payload;
    renderAiReviewPage();
  } catch (error) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = `最近补算结果读取失败：${error.message}`;
    }
  }
  try {
    const response = await fetch("/api/stock/trade-outcomes/recent?version=v2&limit=50");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.aiReviewTradeOutcomeResult = payload;
    renderAiReviewPage();
  } catch (error) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = `最近策略收益读取失败：${error.message}`;
    }
  }
}

async function refreshAiReviewOutcomes() {
  if (!apiEnabled) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = "请通过本地服务打开页面后再补算个股分析收益。";
    }
    return;
  }
  if (state.aiReviewLoading) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = "当前正在读取或补算个股分析复盘数据，请稍后再试。";
    }
    return;
  }
  state.aiReviewLoading = true;
  setAiReviewButtonsLoading(true, "outcomes");
  const horizons = selectedAiReviewHorizons();
  const bucketLabel = aiReviewHorizonBucketLabel(horizons);
  if (els.aiReviewWarning) {
    els.aiReviewWarning.hidden = false;
    els.aiReviewWarning.textContent = `正在分批补算个股分析${bucketLabel} ${horizons} 个交易日方向收益。`;
  }
  try {
    const response = await fetch("/api/stock/outcomes/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: "v2", horizons, mode: "backfill", batchSize: 1000 })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.aiReviewOutcomeResult = { ...payload, resultTitle: "本次补算结果", source: "refresh" };
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = formatAiReviewOutcomeMessage(payload);
    }
    await loadAiReviewStats({ keepWarning: true });
  } catch (error) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = `补算失败：${error.message}`;
    }
  } finally {
    state.aiReviewLoading = false;
    setAiReviewButtonsLoading(false);
  }
}

async function refreshAiReviewTradeOutcomes() {
  if (!apiEnabled) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = "请通过本地服务打开页面后再补算个股分析策略收益。";
    }
    return;
  }
  if (state.aiReviewLoading) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = "当前正在读取或补算个股分析复盘数据，请稍后再试。";
    }
    return;
  }
  state.aiReviewLoading = true;
  setAiReviewButtonsLoading(true, "trade");
  if (els.aiReviewWarning) {
    els.aiReviewWarning.hidden = false;
    els.aiReviewWarning.textContent = "正在按 AI 买点、T+1、止损止盈补算个股分析策略收益。";
  }
  try {
    const response = await fetch("/api/stock/trade-outcomes/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: "v2", mode: "backfill", batchSize: 1000 })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    state.aiReviewTradeOutcomeResult = { ...payload, resultTitle: "本次策略补算结果", source: "refresh" };
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = formatAiReviewTradeOutcomeMessage(payload);
    }
    await loadAiReviewStats({ keepWarning: true });
  } catch (error) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = `策略补算失败：${error.message}`;
    }
  } finally {
    state.aiReviewLoading = false;
    setAiReviewButtonsLoading(false);
  }
}

async function generateAiReviewReport() {
  if (!apiEnabled || state.aiReviewLoading) return;
  state.aiReviewLoading = true;
  setAiReviewButtonsLoading(true, "review");
  if (els.aiReviewWarning) {
    els.aiReviewWarning.hidden = false;
    els.aiReviewWarning.textContent = "正在生成误买/漏判复盘。";
  }
  try {
    const response = await fetch("/api/stock/review/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: "v2", limit: 120 })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = `复盘完成：${payload.caseCount || 0} 个案例，报告 ${payload.reportId || "--"}。`;
    }
    await loadAiReviewStats({ keepWarning: true });
  } catch (error) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = `复盘失败：${error.message}`;
    }
  } finally {
    state.aiReviewLoading = false;
    setAiReviewButtonsLoading(false);
  }
}

async function updateAiReviewLessonStatus(id, status) {
  if (!apiEnabled || !id) return;
  try {
    const response = await fetch("/api/stock/review/lesson-status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: "v2", id, status })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `API ${response.status}`);
    await loadAiReviewStats();
  } catch (error) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = `经验状态更新失败：${error.message}`;
    }
  }
}

async function saveAiReviewConfig() {
  if (!apiEnabled || state.aiReviewLoading || !els.aiReviewContent) return;
  const form = els.aiReviewContent.querySelector("#aiReviewConfigForm");
  if (!form) return;
  const formData = new FormData(form);
  const payload = {
    version: "v2",
    mode: String(formData.get("mode") || "auto"),
    minSamples: Number(formData.get("minSamples")),
    minSimilarity: Number(formData.get("minSimilarity")),
    minMatchedCases: Number(formData.get("minMatchedCases")),
    recentFalseBuyWindow: Number(formData.get("recentFalseBuyWindow")),
    falseBuyAllowedDegradePoints: Number(formData.get("falseBuyAllowedDegradePoints")),
    falseBuyAbsoluteLimitPct: Number(formData.get("falseBuyAbsoluteLimitPct")),
    success3dReturnPct: Number(formData.get("success3dReturnPct")),
    success10dReturnPct: Number(formData.get("success10dReturnPct")),
    falseBuyReturnPct: Number(formData.get("falseBuyReturnPct")),
    falseBuyDrawdownPct: Number(formData.get("falseBuyDrawdownPct")),
    missed5dReturnPct: Number(formData.get("missed5dReturnPct")),
    caseDedupeWindowDays: Number(formData.get("caseDedupeWindowDays"))
  };
  state.aiReviewLoading = true;
  try {
    const response = await fetch("/api/stock/case-memory/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `API ${response.status}`);
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = "案例学习配置已保存，后续个股分析立即生效。";
    }
    await loadAiReviewStats();
  } catch (error) {
    if (els.aiReviewWarning) {
      els.aiReviewWarning.hidden = false;
      els.aiReviewWarning.textContent = `配置保存失败：${error.message}`;
    }
  } finally {
    state.aiReviewLoading = false;
  }
}

function setAiReviewButtonsLoading(loading, target = "") {
  [
    [els.aiReviewOutcomesButton, "补算收益", "outcomes"],
    [els.aiReviewTradeOutcomesButton, "补算策略", "trade"],
    [els.aiReviewGenerateButton, "生成复盘", "review"]
  ].forEach(([button, label, key]) => {
    if (!button) return;
    if (!button.dataset.originalHtml) {
      button.dataset.originalHtml = button.innerHTML;
    }
    button.disabled = loading;
    button.innerHTML = loading && target === key
      ? `<span class="button-spinner"></span>${label}中`
      : button.dataset.originalHtml;
  });
}

function renderAiReviewConfigForm(config = {}) {
  return `
    <form id="aiReviewConfigForm" class="ai-review-config-form">
      <label>
        <span>模式</span>
        <select name="mode">
          ${["auto", "preview", "on", "off"].map((value) => `<option value="${value}" ${String(config.mode || "auto") === value ? "selected" : ""}>${caseConfigModeName(value)}</option>`).join("")}
        </select>
      </label>
      ${renderConfigNumber("样本门槛", "minSamples", config.minSamples, 1, 1)}
      ${renderConfigNumber("最低相似度", "minSimilarity", config.minSimilarity, 0.01, 0, 1)}
      ${renderConfigNumber("匹配案例数", "minMatchedCases", config.minMatchedCases, 1, 0)}
      ${renderConfigNumber("误买窗口", "recentFalseBuyWindow", config.recentFalseBuyWindow, 1, 1)}
      ${renderConfigNumber("误买恶化pct", "falseBuyAllowedDegradePoints", config.falseBuyAllowedDegradePoints, 0.1, 0)}
      ${renderConfigNumber("误买上限%", "falseBuyAbsoluteLimitPct", config.falseBuyAbsoluteLimitPct, 0.1, 0)}
      ${renderConfigNumber("3日成功%", "success3dReturnPct", config.success3dReturnPct, 0.1)}
      ${renderConfigNumber("10日成功%", "success10dReturnPct", config.success10dReturnPct, 0.1)}
      ${renderConfigNumber("误买收益%", "falseBuyReturnPct", config.falseBuyReturnPct, 0.1)}
      ${renderConfigNumber("误买回撤%", "falseBuyDrawdownPct", config.falseBuyDrawdownPct, 0.1)}
      ${renderConfigNumber("漏判5日%", "missed5dReturnPct", config.missed5dReturnPct, 0.1)}
      ${renderConfigNumber("案例去相关天数", "caseDedupeWindowDays", config.caseDedupeWindowDays, 1, 0)}
      <div class="ai-review-config-actions">
        <button class="primary-button" type="submit">保存配置</button>
      </div>
    </form>
  `;
}

function renderConfigNumber(label, name, value, step, min = null, max = null) {
  const attrs = [
    `type="number"`,
    `name="${name}"`,
    `value="${Number(value ?? 0)}"`,
    `step="${step}"`
  ];
  if (min !== null) attrs.push(`min="${min}"`);
  if (max !== null) attrs.push(`max="${max}"`);
  return `
    <label>
      <span>${escapeHtml(label)}</span>
      <input ${attrs.join(" ")} />
    </label>
  `;
}

function caseConfigModeName(value) {
  return {
    auto: "自动",
    preview: "仅预览",
    on: "强制注入",
    off: "关闭"
  }[value] || value;
}

function formatAiReviewOutcomeMessage(payload = {}) {
  const processed = Number(payload.processedSnapshotCount ?? payload.snapshotCount ?? 0);
  const rowCount = Number(payload.rowCount || 0);
  const done = Number(payload.doneCount || 0);
  const pending = Number(payload.pendingCount || 0);
  const noData = Number(payload.noDataCount || 0);
  const remaining = Number(payload.remainingSnapshotCount || 0);
  const remainingText = remaining > 0
    ? `仍有 ${formatInteger(remaining)} 个快照待补算或等待交易日，可继续点击补算收益处理下一批。`
    : "当前没有待补算快照。";
  const warning = payload.warning ? ` 数据提示：${payload.warning}` : "";
  return `补算完成：本批 ${formatInteger(processed)} 个快照，生成 ${formatInteger(rowCount)} 条收益结果，${formatInteger(done)} 条完成，${formatInteger(pending)} 条等待交易日，${formatInteger(noData)} 条缺数据。${remainingText}${warning}`;
}

function selectedAiReviewHorizons() {
  return String(els.aiReviewHorizonSelect?.value || "3,5,7,10");
}

function aiReviewHorizonBucketLabel(horizons) {
  return {
    "3,5,7,10": "短线",
    "20,40": "中线",
    "60,120,240": "长线"
  }[String(horizons || "")] || "";
}

function formatAiReviewTradeOutcomeMessage(payload = {}) {
  const processed = Number(payload.processedSnapshotCount ?? payload.snapshotCount ?? 0);
  const rowCount = Number(payload.rowCount || 0);
  const done = Number(payload.doneCount || 0);
  const pending = Number(payload.pendingCount || 0);
  const noTrigger = Number(payload.noTriggerCount || 0);
  const noData = Number(payload.noDataCount || 0);
  const remaining = Number(payload.remainingSnapshotCount || 0);
  const remainingText = remaining > 0
    ? `仍有 ${formatInteger(remaining)} 个买入快照待策略补算，可继续点击补算策略处理下一批。`
    : "当前没有待策略补算的买入快照。";
  const warning = payload.warning ? ` 数据提示：${payload.warning}` : "";
  return `策略补算完成：本批 ${formatInteger(processed)} 个快照，生成 ${formatInteger(rowCount)} 条策略结果，${formatInteger(done)} 条完成，${formatInteger(noTrigger)} 条未触发买点，${formatInteger(pending)} 条等待交易日，${formatInteger(noData)} 条不可校验。${remainingText}${warning}`;
}

function renderAiReviewOutcomeResult(result) {
  if (!result) return "";
  const rows = Array.isArray(result.rows) ? result.rows : [];
  const groups = groupAiReviewOutcomeRows(rows);
  const horizons = Array.isArray(result.horizons) && result.horizons.length
    ? result.horizons.map(Number).filter(Number.isFinite)
    : outcomeHorizonsFromRows(rows);
  const remaining = Number(result.remainingSnapshotCount || 0);
  const hasMore = Boolean(result.hasMore);
  const stored = result.source === "stored";
  const title = result.resultTitle || (stored ? "最近已补算结果" : "最近补算结果");
  const updatedText = result.lastUpdatedAt ? `最近更新 ${String(result.lastUpdatedAt).replace("T", " ")}` : "";
  return `
    <section class="stock-section">
      <h3>${escapeHtml(title)}</h3>
      <div class="ai-review-metrics compact">
        ${renderAiReviewMetric(stored ? "最近快照" : "本批快照", result.processedSnapshotCount ?? result.snapshotCount, updatedText)}
        ${renderAiReviewMetric(stored ? "已保存结果" : "收益结果", result.rowCount)}
        ${renderAiReviewMetric("已完成", result.doneCount)}
        ${renderAiReviewMetric("等待交易日", result.pendingCount)}
        ${renderAiReviewMetric("缺数据", result.noDataCount)}
        ${stored ? "" : renderAiReviewMetric("待继续处理", remaining, hasMore ? "可继续点击补算" : "已处理完")}
      </div>
      ${result.warning ? `<div class="stock-list-empty">数据提示：${escapeHtml(result.warning)}</div>` : ""}
      ${hasMore ? `<p class="ai-review-outcome-note">仍有 ${formatInteger(remaining)} 个有效快照缺少收益结果或仍在等待交易日，下一次点击会继续处理下一批。</p>` : ""}
      ${rows.length ? `<p class="ai-review-outcome-note">方向收益按统一基准价评估，不代表系统真实买入；同一股票不同分析日会分别保留，当前周期结果合在同一行。</p>${renderAiReviewOutcomeTable(groups, horizons)}` : `<div class="stock-list-empty">本批没有可展示的补算结果</div>`}
    </section>
  `;
}

function renderAiReviewTradeOutcomeResult(result) {
  if (!result) return "";
  const rows = Array.isArray(result.rows) ? result.rows : [];
  const remaining = Number(result.remainingSnapshotCount || 0);
  const hasMore = Boolean(result.hasMore);
  const stored = result.source === "stored";
  const title = result.resultTitle || (stored ? "V2 最近策略执行收益" : "策略执行收益");
  const updatedText = result.lastUpdatedAt ? `最近更新 ${String(result.lastUpdatedAt).replace("T", " ")}` : "";
  return `
    <section class="stock-section">
      <h3>${escapeHtml(title)}</h3>
      <div class="ai-review-metrics compact">
        ${renderAiReviewMetric(stored ? "最近快照" : "本批快照", result.processedSnapshotCount ?? result.snapshotCount, updatedText)}
        ${renderAiReviewMetric(stored ? "已保存策略" : "策略结果", result.rowCount)}
        ${renderAiReviewMetric("已完成", result.doneCount)}
        ${renderAiReviewMetric("未触发买点", result.noTriggerCount)}
        ${renderAiReviewMetric("等待交易日", result.pendingCount)}
        ${renderAiReviewMetric("不可校验", result.noDataCount)}
        ${stored ? "" : renderAiReviewMetric("待继续处理", remaining, hasMore ? "可继续点击补算" : "已处理完")}
      </div>
      ${result.warning ? `<div class="stock-list-empty">数据提示：${escapeHtml(result.warning)}</div>` : ""}
      ${hasMore ? `<p class="ai-review-outcome-note">仍有 ${formatInteger(remaining)} 个买入快照缺少策略结果或仍在等待交易日，下一次点击会继续处理下一批。</p>` : ""}
      ${rows.length ? `<p class="ai-review-outcome-note">策略收益按 AI 买入区间、T+1、止损止盈和周期退出执行；未触发买点标记为 no_trigger，不计为失败买入。</p>${renderAiReviewTradeOutcomeTable(rows)}` : `<div class="stock-list-empty">暂无可展示的策略收益结果</div>`}
    </section>
  `;
}

function groupAiReviewOutcomeRows(rows) {
  const map = new Map();
  rows.forEach((row) => {
    const key = row.snapshotId || `${row.code || ""}|${row.recommendationDate || ""}|${row.riskMode || ""}|${row.decision || ""}`;
    if (!map.has(key)) {
      map.set(key, {
        snapshotId: row.snapshotId,
        code: row.code || "",
        name: row.name || "",
        recommendationDate: row.recommendationDate || "",
        riskMode: row.riskMode || "",
        decision: row.decision || "",
        buyDate: row.buyDate || "",
        buyPrice: row.buyPrice,
        outcomes: {}
      });
    }
    const group = map.get(key);
    group.outcomes[String(row.horizon)] = row;
    if (!group.buyDate && row.buyDate) group.buyDate = row.buyDate;
    if (!group.buyPrice && row.buyPrice) group.buyPrice = row.buyPrice;
  });
  return Array.from(map.values());
}

function outcomeHorizonsFromRows(rows = []) {
  const values = rows.map((row) => Number(row.horizon)).filter(Number.isFinite);
  return [...new Set(values)].sort((left, right) => left - right);
}

function renderAiReviewOutcomeTable(groups, horizons = [3, 5, 7, 10]) {
  return `
    <div class="ai-review-outcome-table-wrap">
      <table class="ai-review-outcome-table">
        <thead>
          <tr>
            <th>股票</th>
            <th>分析日 / 模式</th>
            <th>结论</th>
            <th>买入</th>
            ${horizons.map((horizon) => `<th>${Number(horizon)}日</th>`).join("")}
            <th>标签 / 明细</th>
          </tr>
        </thead>
        <tbody>
          ${groups.map((group) => renderAiReviewOutcomeGroupRow(group, horizons)).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderAiReviewOutcomeGroupRow(group = {}, horizons = [3, 5, 7, 10]) {
  return `
    <tr>
      <td><strong>${escapeHtml(group.code || "--")}</strong><small>${escapeHtml(group.name || "")}</small></td>
      <td><strong>${escapeHtml(group.recommendationDate || "--")}</strong><small>${escapeHtml(riskModeName(group.riskMode))}</small></td>
      <td>${escapeHtml(decisionLabel(group.decision))}</td>
      <td><strong>${escapeHtml(group.buyDate || "--")}</strong><small>${escapeHtml(formatOutcomePrice(group.buyPrice))}</small></td>
      ${horizons.map((horizon) => `<td>${renderOutcomeHorizonBadge(group.outcomes[String(horizon)])}</td>`).join("")}
      <td>${renderOutcomeGroupDetails(group)}</td>
    </tr>
  `;
}

function renderAiReviewTradeOutcomeTable(rows = []) {
  return `
    <div class="ai-review-outcome-table-wrap">
      <table class="ai-review-outcome-table">
        <thead>
          <tr>
            <th>股票</th>
            <th>分析日 / 模式</th>
            <th>周期</th>
            <th>入场</th>
            <th>出场</th>
            <th>收益 / 回撤</th>
            <th>标签 / 原因</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(renderAiReviewTradeOutcomeRow).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderAiReviewTradeOutcomeRow(row = {}) {
  const label = row.label ? tradeOutcomeLabel(row.label) : tradeOutcomeStatusName(row.status);
  return `
    <tr>
      <td><strong>${escapeHtml(row.code || "--")}</strong><small>${escapeHtml(row.name || "")}</small></td>
      <td><strong>${escapeHtml(row.recommendationDate || "--")}</strong><small>${escapeHtml(riskModeName(row.riskMode))}</small></td>
      <td><strong>${escapeHtml(horizonLabel(row.horizonType || "none"))}</strong><small>${escapeHtml(row.planVersion || "--")}</small></td>
      <td><strong>${escapeHtml(row.entryDate || "--")}</strong><small>${escapeHtml(row.entryTriggered ? formatOutcomePrice(row.entryPrice) : tradeOutcomeStatusName(row.status))}</small></td>
      <td><strong>${escapeHtml(row.exitDate || "--")}</strong><small>${escapeHtml(row.exitReason ? `${tradeExitReasonName(row.exitReason)} / ${formatOutcomePrice(row.exitPrice)}` : formatOutcomePrice(row.exitPrice))}</small></td>
      <td>
        <span class="outcome-badge ${outcomeNumberClass(row.returnPct)} ${row.status || ""}">
          <strong>${escapeHtml(row.status === "done" ? formatSignedPct(row.returnPct) : tradeOutcomeStatusName(row.status))}</strong>
          <small>回撤 ${escapeHtml(formatSignedPct(row.maxDrawdownPct))} / 冲高 ${escapeHtml(formatSignedPct(row.maxRunupPct))}</small>
        </span>
      </td>
      <td>
        <details class="outcome-details">
          <summary>${escapeHtml(label)}</summary>
          <div>
            ${row.entryReason ? `<p><strong>入场</strong><span>${escapeHtml(row.entryReason)}</span></p>` : ""}
            ${row.reason ? `<p><strong>结果</strong><span>${escapeHtml(row.reason)}</span></p>` : ""}
          </div>
        </details>
      </td>
    </tr>
  `;
}

function renderOutcomeHorizonBadge(row) {
  if (!row) return `<span class="outcome-badge empty">--</span>`;
  const label = row.label ? caseLabelName(row.label) : outcomeStatusName(row.status);
  const value = row.status === "done" ? formatSignedPct(row.returnPct) : outcomeStatusName(row.status);
  return `
    <span class="outcome-badge ${outcomeNumberClass(row.returnPct)} ${row.status || ""}">
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(label)}</small>
    </span>
  `;
}

function renderOutcomeGroupDetails(group = {}) {
  const rows = Object.values(group.outcomes || {}).sort((left, right) => Number(left.horizon || 0) - Number(right.horizon || 0));
  const summary = outcomeGroupSummary(rows);
  return `
    <details class="outcome-details">
      <summary>${escapeHtml(summary)}</summary>
      <div>
        ${rows.map((row) => {
          const label = row.label ? caseLabelName(row.label) : outcomeStatusName(row.status);
          const reason = row.reason ? ` · ${row.reason}` : "";
          return `
            <p>
              <strong>${escapeHtml(row.horizon ? `${row.horizon}日` : "--")}</strong>
              <span>${escapeHtml(outcomeStatusName(row.status))} · 收益 ${escapeHtml(formatSignedPct(row.returnPct))} · 回撤 ${escapeHtml(formatSignedPct(row.maxDrawdownPct))} · 冲高 ${escapeHtml(formatSignedPct(row.maxRunupPct))}</span>
              <small>退出 ${escapeHtml(row.exitDate || "--")} / ${escapeHtml(formatOutcomePrice(row.exitPrice))} · ${escapeHtml(label)}${escapeHtml(reason)}</small>
            </p>
          `;
        }).join("")}
      </div>
    </details>
  `;
}

function outcomeGroupSummary(rows) {
  const labels = [...new Set(rows.map((row) => row.label).filter(Boolean).map(caseLabelName))];
  if (labels.length) return labels.slice(0, 2).join(" / ");
  const pending = rows.filter((row) => row.status === "pending").length;
  if (pending) return `${pending} 个周期等待`;
  return "查看明细";
}

function outcomeStatusName(value) {
  return {
    done: "已完成",
    pending: "等待交易日",
    no_data: "缺数据",
    no_trigger: "未触发买点"
  }[value] || value || "--";
}

function tradeOutcomeStatusName(value) {
  return {
    done: "已完成",
    pending: "等待交易日",
    no_data: "不可校验",
    no_trigger: "未触发买点"
  }[value] || value || "--";
}

function tradeOutcomeLabel(value) {
  return {
    plan_success: "策略成功",
    plan_failed: "策略失败",
    no_trigger: "未触发买点",
    plan_unverifiable: "计划不可校验",
    legacy_plan_unverifiable: "旧计划不可校验"
  }[value] || value || "--";
}

function tradeExitReasonName(value) {
  return {
    stop_loss: "止损退出",
    take_profit: "止盈退出",
    timeout: "周期退出",
    invalidated: "计划失效"
  }[value] || value || "--";
}

function renderAiReviewOutcomeBuckets(buckets = {}) {
  const rows = ["short", "mid", "long"].map((key) => buckets?.[key] ? { key, ...buckets[key] } : null).filter(Boolean);
  if (!rows.length) return "";
  return `
    <section class="stock-section">
      <h3>方向收益分桶</h3>
      <div class="ai-review-tags">
        ${rows.map((item) => `
          <span>${escapeHtml(horizonBucketName(item.key))} ${escapeHtml((item.horizons || []).join("/"))} · 完成 ${Number(item.doneCount || 0)} · 等待 ${Number(item.pendingCount || 0)}</span>
        `).join("")}
      </div>
    </section>
  `;
}

function renderAiReviewSamplePools(pools = {}) {
  const rows = ["short", "mid", "long"].map((key) => pools?.[key] ? { key, ...pools[key] } : null).filter(Boolean);
  if (!rows.length) return "";
  return `
    <section class="stock-section">
      <h3>有效样本池</h3>
      <div class="ai-review-metrics compact">
        ${rows.map((pool) => {
          const labelRows = Object.entries(pool.labelCounts || {})
            .sort((left, right) => Number(right[1] || 0) - Number(left[1] || 0))
            .slice(0, 3);
          return `
            <article>
              <span>${escapeHtml(horizonBucketName(pool.key))} ${escapeHtml((pool.horizons || []).join("/"))}</span>
              <strong>${Number(pool.effectiveSampleCount || 0)}</strong>
              <small>完成 ${Number(pool.doneOutcomeCount || 0)} · 等待 ${Number(pool.pendingOutcomeCount || 0)}</small>
              <small>正向 ${Number(pool.positiveCount || 0)} · 负向 ${Number(pool.negativeCount || 0)} · 漏判 ${Number(pool.missedOpportunityCount || 0)}</small>
              ${labelRows.length ? `<div class="ai-review-tags">${labelRows.map(([key, value]) => `<span>${escapeHtml(caseLabelName(key))} ${Number(value || 0)}</span>`).join("")}</div>` : ""}
            </article>
          `;
        }).join("")}
      </div>
    </section>
  `;
}

function poolSampleCount(pool = {}) {
  return Number(pool?.effectiveSampleCount || 0);
}

function horizonBucketName(value) {
  return {
    short: "短线",
    mid: "中线",
    long: "长线"
  }[value] || value || "--";
}

function formatOutcomePrice(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number.toFixed(2) : "--";
}

function formatSignedPct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function outcomeNumberClass(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "";
  return number > 0 ? "up" : "down";
}

function riskModeName(value) {
  return {
    balanced: "均衡",
    aggressive: "进攻",
    defensive: "防守"
  }[value] || value || "--";
}

function renderAiReviewPage() {
  if (!els.aiReviewContent) return;
  const stats = state.aiReviewStats;
  if (state.aiReviewLoading && !stats) {
    els.aiReviewContent.innerHTML = `<div class="stock-empty"><strong>加载个股分析案例学习状态</strong><span>正在读取样本、误买率和复盘经验。</span></div>`;
    return;
  }
  if (!stats) {
    els.aiReviewContent.innerHTML = `<div class="stock-empty"><strong>暂无案例学习数据</strong><span>完成几次个股分析后会开始积累样本。</span></div>`;
    return;
  }
  const guard = stats.falseBuyGuard || {};
  const config = stats.promptConfig || {};
  const reports = stats.latestReports || [];
  const lessons = stats.lessons || [];
  const samplePools = stats.samplePools || {};
  els.aiReviewEyebrow.textContent = `短线 ${poolSampleCount(samplePools.short)} · 中线 ${poolSampleCount(samplePools.mid)} · 长线 ${poolSampleCount(samplePools.long)}`;
  els.aiReviewContent.innerHTML = `
    <section class="ai-review-metrics">
      ${renderAiReviewMetric("原始快照", stats.rawSnapshotCount ?? stats.snapshotCount)}
      ${renderAiReviewMetric("去重快照", stats.dedupedSnapshotCount)}
      ${renderAiReviewMetric("短线有效样本", poolSampleCount(samplePools.short), `门槛 ${config.minSamples || 800}`)}
      ${renderAiReviewMetric("误买率", formatNullablePct(guard.recentFalseBuyRatePct), `基准 ${formatNullablePct(guard.baselineFalseBuyRatePct)}`)}
      ${renderAiReviewMetric("Prompt状态", guard.ok ? "允许" : "暂停", `${config.mode || "auto"} · 门槛 ${config.minSamples || 800}`)}
      ${renderAiReviewMetric("Prompt样本", "按周期分池", "短/中/长分别校准，不交叉使用")}
    </section>
    ${renderAiReviewSamplePools(samplePools)}
    ${renderAiReviewOutcomeBuckets(stats.outcomeBuckets)}
    ${renderAiReviewOutcomeResult(state.aiReviewOutcomeResult)}
    ${renderAiReviewTradeOutcomeResult(state.aiReviewTradeOutcomeResult)}
    <section class="stock-section">
      <h3>标签分布</h3>
      <div class="ai-review-tags">
        ${Object.entries(stats.labelCounts || {}).map(([key, value]) => `<span>${escapeHtml(caseLabelName(key))} ${Number(value || 0)}</span>`).join("") || "<span>暂无已完成标签</span>"}
      </div>
    </section>
    <section class="stock-section">
      <h3>当前配置</h3>
      ${renderAiReviewConfigForm(config)}
    </section>
    <section class="stock-section">
      <h3>最近复盘报告</h3>
      <div class="ai-review-list">
        ${reports.length ? reports.map((item) => `
          <article>
            <strong>${escapeHtml(item.reviewDate || "--")} · 样本 ${Number(item.sampleCount || 0)}</strong>
            <span>误买 ${Number(item.falseBuyCount || 0)} · 漏判 ${Number(item.missedOpportunityCount || 0)}</span>
            <p>${escapeHtml(item.summary || "暂无摘要")}</p>
          </article>
        `).join("") : `<div class="stock-list-empty">暂无复盘报告</div>`}
      </div>
    </section>
    <section class="stock-section">
      <h3>复盘经验库</h3>
      <div class="ai-review-list lessons">
        ${lessons.length ? lessons.map((item) => `
          <article>
            <div>
              <strong>${escapeHtml(item.title || item.lesson_key || "--")}</strong>
              <span>${escapeHtml(lessonStatusName(item.status || "draft"))} · ${escapeHtml(item.updated_at || "--")}</span>
            </div>
            ${renderLessonQuality(item)}
            <p>${escapeHtml(item.prompt_instruction || "暂无提示词规则")}</p>
            <div class="lesson-actions">
              ${["active", "paused", "rejected"].map((status) => renderLessonStatusButton(item, status)).join("")}
            </div>
          </article>
        `).join("") : `<div class="stock-list-empty">暂无复盘经验</div>`}
      </div>
    </section>
  `;
}

function renderLessonQuality(item = {}) {
  const quality = item.quality || {};
  const safe = Boolean(quality.safeToEnable);
  const status = quality.status || "empty";
  return `
    <div class="lesson-quality ${safe ? "safe" : "warn"}">
      <span>${escapeHtml(lessonQualityName(status))}</span>
      <span>条件 ${Number(quality.conditionCount || 0)} · 证据 ${Number(quality.evidenceCount ?? item.evidenceCount ?? 0)}</span>
      <small>${escapeHtml(item.patternSummary || quality.warning || "缺少结构化匹配条件")}</small>
      ${quality.warning ? `<small>${escapeHtml(quality.warning)}</small>` : ""}
    </div>
  `;
}

function renderLessonStatusButton(item = {}, status) {
  const quality = item.quality || {};
  const disabled = status === "active" && !quality.safeToEnable;
  const title = disabled ? `title="${escapeHtml(quality.warning || "结构不完整，不建议启用")}"` : "";
  return `<button type="button" data-lesson-id="${Number(item.id)}" data-lesson-status="${status}" ${title} ${disabled ? "disabled" : ""}>${lessonStatusName(status)}</button>`;
}

function renderAiReviewMetric(label, value, sub = "") {
  return `
    <article>
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value ?? "--")}</strong>
      ${sub ? `<small>${escapeHtml(sub)}</small>` : ""}
    </article>
  `;
}

function lessonStatusName(value) {
  return {
    draft: "草稿",
    active: "启用",
    paused: "暂停",
    rejected: "拒绝"
  }[value] || value;
}

function lessonQualityName(value) {
  return {
    complete: "结构完整",
    partial: "结构偏弱",
    generic: "匹配过宽",
    empty: "结构缺失"
  }[value] || "结构未知";
}

function decisionClass(value) {
  return value === "buy" ? "buy" : value === "avoid" ? "avoid" : "watch";
}

function decisionLabel(value) {
  return {
    buy: "可小仓试错",
    watch: "等待确认",
    avoid: "不建议当前买入"
  }[value] || "等待确认";
}

function horizonLabel(value) {
  return {
    short: "短线",
    mid: "中线",
    long: "长线",
    none: "无主周期"
  }[value] || value || "--";
}

function horizonPeriodLabel(value) {
  return {
    short: "1-5个交易日",
    mid: "2-8周",
    long: "3-12个月"
  }[value] || "";
}

function stanceLabel(value) {
  return {
    buy: "可买",
    watch: "观察",
    avoid: "不买"
  }[value] || "观察";
}

function stanceClass(value) {
  return value === "buy" ? "buy" : value === "avoid" ? "avoid" : "watch";
}

function openAiFullMarketPage() {
  const params = new URLSearchParams({
    source: state.requestedSource || "auto",
    risk: state.riskMode || "balanced",
    profile: "intraday",
    scanTime: state.scanTime || "14:36"
  });
  const target = `./ai-full-market-selection.html?${params.toString()}`;
  const targetUrl = new URL(target, window.location.href);
  if (window.location.href === targetUrl.href) {
    window.location.reload();
    return;
  }
  window.location.href = target;
}

function openMorningSectorRadarPage() {
  const params = new URLSearchParams({
    source: state.requestedSource || "auto",
    marketSource: state.requestedSource || "auto",
    sectorType: "all",
    scanTime: "10:00"
  });
  const target = `./morning-sector-radar.html?${params.toString()}`;
  const targetUrl = new URL(target, window.location.href);
  if (window.location.href === targetUrl.href) {
    window.location.reload();
    return;
  }
  window.location.href = target;
}

function openT1OvernightPage() {
  const params = new URLSearchParams({
    source: state.requestedSource || "auto",
    risk: state.riskMode || "balanced",
    scanTime: "14:40"
  });
  const target = `./t1-overnight-selection.html?${params.toString()}`;
  const targetUrl = new URL(target, window.location.href);
  if (window.location.href === targetUrl.href) {
    window.location.reload();
    return;
  }
  window.location.href = target;
}

function openT1ReviewPage() {
  const target = "./t1-overnight-review.html";
  const targetUrl = new URL(target, window.location.href);
  if (window.location.href === targetUrl.href) {
    window.location.reload();
    return;
  }
  window.location.href = target;
}

function openT1ConfigPage() {
  const target = "./t1-overnight-config.html";
  const targetUrl = new URL(target, window.location.href);
  if (window.location.href === targetUrl.href) {
    window.location.reload();
    return;
  }
  window.location.href = target;
}

function openT1DiagnosticsPage() {
  const params = new URLSearchParams({
    source: state.requestedSource || "auto",
    scanTime: "14:40"
  });
  const target = `./t1-overnight-diagnostics.html?${params.toString()}`;
  const targetUrl = new URL(target, window.location.href);
  if (window.location.href === targetUrl.href) {
    window.location.reload();
    return;
  }
  window.location.href = target;
}

function openT1MaintenancePage() {
  const target = "./t1-overnight-maintenance.html";
  const targetUrl = new URL(target, window.location.href);
  if (window.location.href === targetUrl.href) {
    window.location.reload();
    return;
  }
  window.location.href = target;
}

function selectionModeLabel(value) {
  return value === "ai" ? "AI" : value === "rules" ? "规则" : value || "--";
}

async function loadRetailHolders({ force = false } = {}) {
  if (!apiEnabled) return null;
  const query = state.retailQuery || "";
  const url = `/api/retail-holders/latest?limit=100&query=${encodeURIComponent(query)}`;
  const response = force
    ? await fetch("/api/retail-holders/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit: 100, query })
      })
    : await fetch(url);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `API ${response.status}`);
  }
  state.retailHolders = await response.json();
  return state.retailHolders;
}

function renderRetailHolderModal() {
  const payload = state.retailHolders;
  const query = state.retailQuery || "";
  const updatedAt = payload?.updatedAt ? payload.updatedAt.replace("T", " ") : "--";
  const staleText = payload?.stale ? "缓存待更新" : "缓存有效";
  const matchedText = query ? `匹配 ${payload?.matchedRows || 0} / ${payload?.totalRows || 0}` : `全量 ${payload?.totalRows || 0}`;
  els.retailHolderEyebrow.textContent = payload
    ? `${matchedText} · ${staleText} · ${updatedAt}`
    : "股东户数";
  els.retailHolderTitle.textContent = query ? "散户榜搜索结果" : "散户榜 Top 100";
  els.retailHolderSummary.textContent = payload
    ? `按“股东户数-本次”降序；数据来自东方财富披露口径，不是实时散户人数。`
    : "正在读取股东户数缓存。";
  const warning = payload?.warning || "";
  els.retailHolderWarning.hidden = !warning;
  els.retailHolderWarning.textContent = warning;
  const rows = payload?.items || [];
  if (!rows.length) {
    els.retailHolderList.innerHTML = `
      <tr>
        <td colspan="7" class="empty-row">${query ? "没有匹配的股票" : "暂无股东户数数据"}</td>
      </tr>
    `;
    return;
  }
  els.retailHolderList.innerHTML = rows
    .map((item, index) => {
      const rank = item.rank || index + 1;
      const change = Number(item.holderChange || 0);
      const changeClass = change > 0 ? "up" : change < 0 ? "down" : "";
      return `
        <tr>
          <td><span class="table-rank">#${rank}</span></td>
          <td>
            <strong>${escapeHtml(item.name)}</strong>
            <small>${escapeHtml(item.code)}</small>
          </td>
          <td>${formatInteger(item.holderCount)}</td>
          <td class="${changeClass}">
            ${change > 0 ? "+" : ""}${formatInteger(change)}
            <small>${Number(item.holderChangeRatio || 0).toFixed(2)}%</small>
          </td>
          <td>${escapeHtml(item.statDate || "--")}</td>
          <td>${escapeHtml(item.noticeDate || "--")}</td>
          <td>${formatMoney(item.avgHoldingMarketValue)}</td>
        </tr>
      `;
    })
    .join("");
}

async function openRetailHolderModal(options = {}) {
  showWorkspaceSection("retail", options);
  if (els.retailHolderSearch) els.retailHolderSearch.value = state.retailQuery;
  renderRetailHolderModal();
  try {
    await loadRetailHolders();
    renderRetailHolderModal();
  } catch (error) {
    els.retailHolderEyebrow.textContent = "读取失败";
    els.retailHolderSummary.textContent = "股东户数读取失败，请确认本地 API 服务正在运行，或稍后再更新缓存。";
    els.retailHolderWarning.hidden = false;
    els.retailHolderWarning.textContent = error.message;
    els.retailHolderList.innerHTML = "";
  }
}

function setRetailRefreshLoading(loading) {
  state.retailLoading = loading;
  if (!els.retailHolderRefreshButton) return;
  els.retailHolderRefreshButton.disabled = loading;
  els.retailHolderRefreshButton.innerHTML = loading
    ? "更新中"
    : `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.7 6.3A8 8 0 1 0 20 12h-2a6 6 0 1 1-1.8-4.3L13 11h8V3l-3.3 3.3Z"/></svg>更新`;
}

async function refreshRetailHolders(force = false) {
  if (!apiEnabled || state.retailLoading) return;
  setRetailRefreshLoading(true);
  try {
    await loadRetailHolders({ force });
    renderRetailHolderModal();
  } catch (error) {
    els.retailHolderWarning.hidden = false;
    els.retailHolderWarning.textContent = `散户榜更新失败：${error.message}`;
  } finally {
    setRetailRefreshLoading(false);
  }
}

function closeRetailHolderModal() {
  showWorkspaceSection("dashboard");
}

function formatMetric(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : "--";
}

function formatTradePrice(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number.toFixed(2) : "--";
}

function formatPositionPct(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? `${number.toFixed(number % 1 === 0 ? 0 : 1)}%` : "--";
}

function formatMarketCap(fundamentals, valuation) {
  if (fundamentals?.totalMarketCap) return formatMoney(fundamentals.totalMarketCap);
  const marketCapYi = Number(valuation?.marketCapYi);
  return Number.isFinite(marketCapYi) ? `${marketCapYi.toFixed(2)}亿` : "--";
}

function renderCatalyst(news) {
  const safeUrl = safeExternalUrl(news?.url);
  const title = escapeHtml(news?.title || "--");
  const titleNode = safeUrl
    ? `<a class="news-title" href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${title}</a>`
    : `<strong class="news-title">${title}</strong>`;
  return `
    <article class="${safeUrl ? "has-link" : "no-link"}">
      <span>${escapeHtml(news?.type || "线索")}</span>
      ${titleNode}
      <small>${escapeHtml(news?.time || "")}</small>
    </article>
  `;
}

function safeExternalUrl(value) {
  const url = String(value || "").trim();
  return /^https?:\/\//i.test(url) ? url : "";
}

function bindClick(element, handler) {
  if (element) element.addEventListener("click", handler);
}

bindClick(els.dashboardButton, () => {
  showWorkspaceSection("dashboard");
});

bindClick(els.universeButton, () => {
  openUniverseModal();
});

bindClick(els.stockAnalysisButton, () => {
  openStockAnalysisModal();
});

bindClick(els.aiFullMarketButton, () => {
  openAiFullMarketPage();
});

bindClick(els.morningSectorButton, () => {
  openMorningSectorRadarPage();
});

bindClick(els.t1OvernightButton, () => {
  openT1OvernightPage();
});

bindClick(els.aiReviewButton, () => {
  openAiReviewPage();
});

bindClick(els.crossValidationButton, () => {
  openCrossModuleValidationPage();
});

bindClick(els.entryAuditHistoryButton, () => {
  openIntradayEntryAuditHistoryPage();
});

bindClick(els.retailHolderButton, () => {
  openRetailHolderModal();
});

bindClick(els.universeCloseButton, () => {
  closeUniverseModal();
});

bindClick(els.universeRefreshButton, () => {
  refreshDefaultUniverse();
});

if (els.workbenchGrid) {
  els.workbenchGrid.addEventListener("click", (event) => {
    const button = event.target.closest("[data-workbench-open]");
    if (!button) return;
    const target = button.dataset.workbenchOpen;
    if (target === "stock") openStockAnalysisModal();
    if (target === "ai-full-market") openAiFullMarketPage();
    if (target === "morning-sector") openMorningSectorRadarPage();
    if (target === "t1-overnight") openT1OvernightPage();
    if (target === "t1-review") openT1ReviewPage();
    if (target === "t1-config") openT1ConfigPage();
    if (target === "t1-diagnostics") openT1DiagnosticsPage();
    if (target === "t1-maintenance") openT1MaintenancePage();
    if (target === "ai-review") openAiReviewPage();
    if (target === "cross-validation") openCrossModuleValidationPage();
    if (target === "entry-audit-history") openIntradayEntryAuditHistoryPage();
    if (target === "retail") openRetailHolderModal();
    if (target === "universe") openUniverseModal();
  });
}

els.stockAnalysisForm.addEventListener("submit", (event) => {
  event.preventDefault();
  analyzeStock();
});

els.stockAnalysisMarketRefreshButton.addEventListener("click", () => {
  refreshStockAnalysisMarketContext();
});

if (els.stockAnalysisHasPosition) {
  els.stockAnalysisHasPosition.addEventListener("change", () => {
    updateStockAnalysisPositionFields();
  });
  updateStockAnalysisPositionFields();
}

els.stockAnalysisCode.addEventListener("input", () => {
  scheduleStockAnalysisSuggestions();
});

els.stockAnalysisCode.addEventListener("focus", () => {
  scheduleStockAnalysisSuggestions();
});

els.stockAnalysisCode.addEventListener("keydown", (event) => {
  handleStockAnalysisSuggestionKey(event);
});

els.stockAnalysisCode.addEventListener("blur", () => {
  window.setTimeout(() => clearStockAnalysisSuggestions(), 160);
});

els.stockAnalysisSuggestions.addEventListener("click", (event) => {
  const button = event.target.closest("[data-index]");
  if (!button) return;
  chooseStockAnalysisSuggestion(Number(button.dataset.index), { run: false });
  els.stockAnalysisCode.focus();
});

els.stockAnalysisCloseButton.addEventListener("click", () => {
  closeStockAnalysisModal();
});

els.aiReviewCloseButton.addEventListener("click", () => {
  closeAiReviewPage();
});

els.aiReviewOutcomesButton.addEventListener("click", () => {
  refreshAiReviewOutcomes();
});

els.aiReviewTradeOutcomesButton.addEventListener("click", () => {
  refreshAiReviewTradeOutcomes();
});

els.aiReviewGenerateButton.addEventListener("click", () => {
  generateAiReviewReport();
});

els.aiReviewContent.addEventListener("click", (event) => {
  const button = event.target.closest("[data-lesson-id][data-lesson-status]");
  if (!button) return;
  updateAiReviewLessonStatus(Number(button.dataset.lessonId), button.dataset.lessonStatus);
});

els.aiReviewContent.addEventListener("submit", (event) => {
  if (!event.target.matches("#aiReviewConfigForm")) return;
  event.preventDefault();
  saveAiReviewConfig();
});

els.retailHolderRefreshButton.addEventListener("click", () => {
  refreshRetailHolders(true);
});

els.retailHolderSearch.addEventListener("input", (event) => {
  state.retailQuery = event.target.value.trim();
  window.clearTimeout(retailSearchTimer);
  retailSearchTimer = window.setTimeout(() => {
    refreshRetailHolders(false);
  }, 220);
});

els.universeModal.addEventListener("click", (event) => {
  if (shouldCloseOnBackdrop(els.universeModal, event)) {
    closeUniverseModal();
  }
});

els.retailHolderCloseButton.addEventListener("click", () => {
  closeRetailHolderModal();
});

els.retailHolderModal.addEventListener("click", (event) => {
  if (shouldCloseOnBackdrop(els.retailHolderModal, event)) {
    closeRetailHolderModal();
  }
});

els.stockAnalysisModal.addEventListener("click", (event) => {
  if (shouldCloseOnBackdrop(els.stockAnalysisModal, event)) {
    closeStockAnalysisModal();
  }
});

els.aiReviewPage.addEventListener("click", (event) => {
  if (shouldCloseOnBackdrop(els.aiReviewPage, event)) {
    closeAiReviewPage();
  }
});

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !els.universeModal.hidden) {
    closeUniverseModal();
  }
  if (event.key === "Escape" && !els.stockAnalysisModal.hidden) {
    closeStockAnalysisModal();
  }
  if (event.key === "Escape" && !els.aiReviewPage.hidden) {
    closeAiReviewPage();
  }
  if (event.key === "Escape" && !els.retailHolderModal.hidden) {
    closeRetailHolderModal();
  }
});


window.addEventListener("hashchange", () => {
  openWorkspaceSection(workspaceSectionFromHash(), { scroll: false, updateUrl: false });
});

setupWorkspacePages();
render();
