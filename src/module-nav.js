(function () {
  const path = window.location.pathname.split("/").pop() || "index.html";
  const currentByPath = {
    "ai-full-market-selection.html": ["ai-full-market", "selection"],
    "ai-full-market-review.html": ["ai-full-market", "review"],
    "morning-sector-radar.html": ["morning-sector", "scan"],
    "cross-module-validation.html": ["audit", "cross"],
    "intraday-entry-audit-history.html": ["audit", "entry"]
  };
  const [activeModule, activeItem] = currentByPath[path] || ["dashboard", "overview"];

  const modules = [
    {
      key: "dashboard",
      label: "交易工作台",
      icon: '<path d="M12 3a9 9 0 1 0 9 9h-2a7 7 0 1 1-7-7V3Zm1 0v10h8A9 9 0 0 0 13 3Zm2 4.1A7.03 7.03 0 0 1 18.9 11H15V7.1Z"/>',
      items: [{ key: "overview", label: "总览", href: "./index.html" }]
    },
    {
      key: "stock",
      label: "个股分析",
      icon: '<path d="M4 5h16v2H4V5Zm0 6h10v2H4v-2Zm0 6h16v2H4v-2Zm13.5-7 1.5 1.5 3-3L23.4 10 19 14.4 16.1 11.5 17.5 10Z"/>',
      items: [
        { key: "analysis", label: "买点与周期判断", href: "./index.html#stock" },
        { key: "review", label: "分析复盘", href: "./index.html#ai-review" }
      ]
    },
    {
      key: "ai-full-market",
      label: "AI全市场观察",
      icon: '<path d="M12 2 3 6.5v10L12 22l9-5.5v-10L12 2Zm0 2.2 5.7 2.9L12 10 6.3 7.1 12 4.2ZM5 8.8l6 3.1v7.2l-6-3.7V8.8Zm8 10.3v-7.2l6-3.1v6.6l-6 3.7Z"/>',
      items: [
        { key: "selection", label: "重点观察", href: "./ai-full-market-selection.html" },
        { key: "review", label: "全市场复盘", href: "./ai-full-market-review.html" }
      ]
    },
    {
      key: "morning-sector",
      label: "早盘主线雷达",
      icon: '<path d="M4 18h16v2H4v-2Zm1-5 4-4 4 3 5-7 1.6 1.2-6.2 8.6-4.2-3.1L6.4 14.5 5 13Zm-1-8h7v2H4V5Zm0 4h4v2H4V9Z"/>',
      items: [{ key: "scan", label: "主线扫描", href: "./morning-sector-radar.html" }]
    },
    {
      key: "t1",
      label: "超短线 T+1",
      icon: '<path d="M13 2 4 13h6l-1 9 9-12h-6l1-8Zm-2.4 9H8.2l2.4-3-.5 4h3.5l-2.6 3.4.4-4.4Z"/>',
      items: [
        { key: "selection", label: "今日推荐", href: "./t1-overnight-selection.html" },
        { key: "review", label: "推荐复盘", href: "./t1-overnight-review.html" },
        { key: "config", label: "策略配置", href: "./t1-overnight-config.html" },
        { key: "diagnostics", label: "Run 诊断", href: "./t1-overnight-diagnostics.html" },
        { key: "maintenance", label: "数据维护", href: "./t1-overnight-maintenance.html" }
      ]
    },
    {
      key: "audit",
      label: "审计与验证",
      icon: '<path d="M4 4h7v7H4V4Zm9 0h7v7h-7V4ZM4 13h7v7H4v-7Zm9 0h7v7h-7v-7Z"/>',
      items: [
        { key: "cross", label: "跨模块复盘", href: "./cross-module-validation.html" },
        { key: "entry", label: "进场审计历史", href: "./intraday-entry-audit-history.html" }
      ]
    },
    {
      key: "data",
      label: "基础数据",
      icon: '<path d="M12 3C7.6 3 4 4.3 4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6c0-1.7-3.6-3-8-3Zm0 2c3.9 0 6 .9 6 1s-2.1 1-6 1-6-.9-6-1 2.1-1 6-1Zm0 14c-3.9 0-6-.9-6-1v-2.2c1.4.8 3.6 1.2 6 1.2s4.6-.4 6-1.2V18c0 .1-2.1 1-6 1Zm0-4c-3.9 0-6-.9-6-1v-2.2c1.4.8 3.6 1.2 6 1.2s4.6-.4 6-1.2V14c0 .1-2.1 1-6 1Zm0-4c-3.9 0-6-.9-6-1V7.8C7.4 8.6 9.6 9 12 9s4.6-.4 6-1.2V10c0 .1-2.1 1-6 1Z"/>',
      items: [
        { key: "retail", label: "散户榜", href: "./index.html#retail" },
        { key: "universe", label: "重点池", href: "./index.html#universe" }
      ]
    }
  ];

  function renderModule(module) {
    const isOpen = module.key === activeModule;
    const items = module.items
      .map((item) => {
        const active = isOpen && item.key === activeItem;
        return `<a class="module-nav-subitem${active ? " active" : ""}" href="${item.href}"${active ? ' aria-current="page"' : ""}>${item.label}</a>`;
      })
      .join("");
    return `
      <details class="module-nav-section"${isOpen ? " open" : ""}>
        <summary class="module-nav-trigger">
          <span class="module-nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true">${module.icon}</svg></span>
          <span>${module.label}</span>
        </summary>
        <div class="module-nav-sublist">${items}</div>
      </details>
    `;
  }

  function renderRail() {
    return `
      <aside class="module-rail" aria-label="主导航">
        <div class="module-brand">
          <span class="module-brand-mark">A</span>
          <div>
            <strong>中短线雷达</strong>
            <span>Rule-Locked AI</span>
          </div>
        </div>
        <nav class="module-nav" aria-label="主导航">${modules.map(renderModule).join("")}</nav>
      </aside>
    `;
  }

  const main = document.querySelector("body > main.shell");
  if (!main || document.querySelector(".module-app-shell")) return;
  const shell = document.createElement("div");
  shell.className = "module-app-shell";
  shell.innerHTML = renderRail();
  document.body.insertBefore(shell, main);
  shell.appendChild(main);
})();
