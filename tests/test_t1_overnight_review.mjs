import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/t1-overnight-review.js', import.meta.url), 'utf8');

function setup() {
  const elements = new Map();
  const element = selector => {
    if (elements.has(selector)) return elements.get(selector);
    const classes = new Set();
    const node = {
      value: '',
      disabled: false,
      textContent: '',
      innerHTML: '',
      dataset: {},
      classList: {
        add: (...values) => values.forEach(value => classes.add(value)),
        remove: (...values) => values.forEach(value => classes.delete(value)),
        contains: value => classes.has(value),
        toggle: (value, force) => force === undefined ? (classes.has(value) ? classes.delete(value) : classes.add(value)) : (force ? classes.add(value) : classes.delete(value)),
      },
      addEventListener() {},
      setAttribute() {},
      querySelectorAll: () => [],
    };
    elements.set(selector, node);
    return node;
  };
  const ok = body => ({ ok: true, json: async () => body });
  const context = vm.createContext({
    document: { querySelector: element },
    window: {
      location: { search: '' },
      history: { replaceState() {} },
      addEventListener() {},
    },
    fetch: async url => String(url).includes('shadow-analytics')
      ? ok({ roles: [], facts: [], generatedAt: '2026-10-13T11:00:00' })
      : ok({ runs: [], stats: {}, optimizationStats: {} }),
    URLSearchParams,
    console,
    setTimeout,
    clearTimeout,
  });
  vm.runInContext(source, context);
  return { context, element };
}

const report = {
  gateBlockedCandidateCount: 3,
  gateDiagnostics: [
    { gateKey: 'sector', label: '板块状态', configuredMode: 'shadow', hitCount: 2, evaluatedCount: 6, evidenceCoveragePct: 100 },
    { gateKey: 'tail_support', label: '1m 尾盘承接', configuredMode: 'shadow', hitCount: 1, evaluatedCount: 6, evidenceCoveragePct: 83.33 },
    { gateKey: 'overnight_risk', label: '隔夜风险', configuredMode: 'shadow', hitCount: 1, evaluatedCount: 6, evidenceCoveragePct: 100 },
    { gateKey: 'chase_risk', label: '追高风险', configuredMode: 'shadow', hitCount: 2, evaluatedCount: 6, evidenceCoveragePct: 100 },
    { gateKey: 'event_risk', label: '事件风险', configuredMode: 'off', hitCount: 0, evaluatedCount: 0, evidenceCoveragePct: null },
  ],
};

test('risk-gate role renders isolated gate diagnostics', () => {
  const app = setup();
  vm.runInContext('state.shadowFamily = "policy_experiment"; state.shadowRole = "policy_risk_gate";', app.context);
  app.context.renderShadowGateDiagnostics(report);
  const node = app.element('#shadowGateDiagnostics');
  assert.equal(node.classList.contains('hidden'), false);
  assert.match(node.innerHTML, /负向门禁诊断/);
  assert.match(node.innerHTML, /累计挡下 3 个候选/);
  assert.match(node.innerHTML, /1m 尾盘承接/);
  assert.match(node.innerHTML, /证据覆盖 83\.33%/);
  assert.match(node.innerHTML, /关闭 · 证据覆盖 --/);
});

test('other shadow roles do not show risk-gate diagnostics', () => {
  const app = setup();
  vm.runInContext('state.shadowFamily = "policy_experiment"; state.shadowRole = "policy_market_gate";', app.context);
  app.context.renderShadowGateDiagnostics(report);
  const node = app.element('#shadowGateDiagnostics');
  assert.equal(node.classList.contains('hidden'), true);
  assert.equal(node.innerHTML, '');
});

test('diversity role renders only second-slot and conflict diagnostics', () => {
  const app = setup();
  vm.runInContext('state.shadowFamily = "policy_experiment"; state.shadowRole = "policy_diversity";', app.context);
  app.context.renderShadowGateDiagnostics({
    diversityDiagnostics: {
      replacementCount: 2,
      blockedCandidateCount: 3,
      avgSecondSlotIncrementPct: 0.24,
      medianSecondSlotIncrementPct: 0.11,
      positiveSecondSlotRate: 60,
      secondSlotEffectiveDays: 5,
      sameDayFailureRate: 20,
      sameDayFailureSampleDays: 5,
      portfolioReturnCorrelation: null,
      correlationSampleDays: 4,
      conflictCounts: { sameIndustry: 1, sameDriverGroup: 2, industryIdentityUnavailable: 1, driverIdentityUnavailable: 0 },
    },
  });
  const node = app.element('#shadowGateDiagnostics');
  assert.equal(node.classList.contains('hidden'), false);
  assert.match(node.innerHTML, /组合独立性诊断/);
  assert.match(node.innerHTML, /第二槽增量/);
  assert.match(node.innerHTML, /样本 4 日，不足 5 日不计算/);
  assert.doesNotMatch(node.innerHTML, /第三槽增量诊断/);
});

test('rank3 role renders cash comparison without diversity metrics', () => {
  const app = setup();
  vm.runInContext('state.shadowFamily = "policy_experiment"; state.shadowRole = "policy_rank3";', app.context);
  app.context.renderShadowGateDiagnostics({
    rank3Diagnostics: {
      effectiveDays: 8,
      avgIncrementPct: 0.3,
      medianIncrementPct: 0.2,
      positiveIncrementRate: 62.5,
      worstIncrementPct: -0.8,
      avgSlotReturnPct: 0.9,
      avgEtfExcessPct: 0.4,
      sameDayFailureRate: 12.5,
      sameDayFailureSampleDays: 8,
      baseReturnCorrelation: 0.36,
      correlationSampleDays: 8,
    },
  });
  const node = app.element('#shadowGateDiagnostics');
  assert.match(node.innerHTML, /第三槽增量诊断/);
  assert.match(node.innerHTML, /Rank1\+Rank2\+现金/);
  assert.match(node.innerHTML, /ETF 超额/);
  assert.doesNotMatch(node.innerHTML, /冲突构成/);
});

test('net-expectation role shows sample gates and isolated baselines', () => {
  const app = setup();
  vm.runInContext('state.shadowFamily = "policy_experiment"; state.shadowRole = "policy_net_expectation";', app.context);
  app.context.renderShadowGateDiagnostics({
    netExpectationDiagnostics: {
      rankingReady: false,
      trainingSampleCount: 42,
      trainingTradeDays: 7,
      minimumTotalSamples: 150,
      minimumBinSamples: 30,
      readyBinCount: 1,
      binCount: 8,
      calibrationSelectedCount: 40,
      calibrationProbabilityCount: 25,
      avgEligiblePoolWeightedReturnPct: 0.18,
      avgCurrentRuleReturnPct: -0.22,
      avgRandomBaselineReturnPct: -0.4,
      avgEtfReturnPct: 0.05,
      budgetLimitedDays: 5,
    },
  });
  const node = app.element('#shadowGateDiagnostics');
  assert.equal(node.classList.contains('hidden'), false);
  assert.match(node.innerHTML, /保守净期望模型/);
  assert.match(node.innerHTML, /仅观察，不生成可交易排序/);
  assert.match(node.innerHTML, /还差 108 笔/);
  assert.match(node.innerHTML, /120 池加权收益/);
  assert.match(node.innerHTML, /随机基线/);
});

test('adaptive-exit role renders only execution and exit-branch diagnostics', () => {
  const app = setup();
  vm.runInContext('state.shadowFamily = "policy_experiment"; state.shadowRole = "policy_adaptive_exit";', app.context);
  app.context.renderShadowGateDiagnostics({
    adaptiveExitDiagnostics: {
      completeExecutionRate: 75,
      completeExecutionCount: 3,
      holdingOutcomeCount: 4,
      incompleteExecutionCount: 1,
      avgMarkToMarketReturnPct: -0.2,
      executionStatusCounts: { executed: 3, partial_execution: 1 },
      branchCounts: { opening_risk_exit: 1, breakdown_exit: 1, strong_open_split_exit: 1 },
    },
  });
  const node = app.element('#shadowGateDiagnostics');
  assert.equal(node.classList.contains('hidden'), false);
  assert.match(node.innerHTML, /逐分钟动态退出诊断/);
  assert.match(node.innerHTML, /完整成交率/);
  assert.match(node.innerHTML, /低开风险退出/);
  assert.match(node.innerHTML, /强开分腿/);
  assert.doesNotMatch(node.innerHTML, /负向门禁诊断/);
  assert.doesNotMatch(node.innerHTML, /第三槽增量诊断/);
  assert.doesNotMatch(node.innerHTML, /保守净期望模型/);
});

test('run detail exposes opening evidence eligibility and coverage', () => {
  const app = setup();
  app.context.renderDetail({
    id: 108,
    recommendationDate: '2026-10-13',
    scanTime: '14:40',
    selectionMode: 'ai_t1_overnight',
    reviewStatus: 'reviewable',
    reviewStatusLabel: '可看结果',
    reviewStatusReason: '已验证',
    outcomes: [],
    finalPicks: [],
    market: {
      marketContext: { label: '震荡' },
      openingEvidence: {
        precheck: { status: 'ready', expectedCount: 5000 },
        snapshot: {
          snapshotId: 9,
          status: 'available',
          decisionEligible: true,
          marketCoveragePct: 96.2,
          mainIndexStatus: 'available',
        },
      },
    },
  });
  const node = app.element('#detailMetrics');
  assert.match(node.innerHTML, /09:29 数据预检/);
  assert.match(node.innerHTML, /09:31 早盘证据/);
  assert.match(node.innerHTML, /可用于动态退出/);
  assert.match(node.innerHTML, /全市场覆盖 96\.20%/);
  assert.match(node.innerHTML, /四指数完整/);
});

test('run detail renders same-day policy shadows before outcomes exist', () => {
  const app = setup();
  app.context.renderShadow({
    id: 95,
    outcomes: [],
    market: {
      eventRiskAudit: { requestedCount: 120, coveredCount: 120 },
      strategyPolicy: {
        strategyPolicyVersion: '2026-10-13收益优化.shadow.v5',
        affectsOfficialRecommendation: false,
        baseMarketState: 'mixed',
        maxOfficialPicksCandidate: 1,
        marketGateShadowPortfolio: [{ code: '002281', name: '光迅科技', originalAiRank: 1 }],
        riskGateTreatmentPortfolio: [],
        riskGateBlockedCandidates: [
          { code: '002281' },
          { code: '603661' },
          { code: '300814' },
        ],
        negativeGateAudit: {
          gates: {
            tail_support: { label: '1m 尾盘承接', hitCount: 3 },
          },
        },
        portfolioDiversityTreatmentPortfolio: [],
        portfolioDiversityAudit: { secondSlotOpen: false },
        rank3ShadowPortfolio: [
          { code: '002281', name: '光迅科技', originalAiRank: 1 },
          { code: '603661', name: '恒林股份', originalAiRank: 2 },
          { code: '300814', name: '中富电路', originalAiRank: 3 },
        ],
        rank3Audit: { available: true },
        netExpectationTreatmentPortfolio: [],
        netExpectationModel: { rankingReady: false, sampleCount: 10, minimumTotalSamples: 150 },
        netExpectationReason: '样本不足，仅冻结校准样本。',
        frozenDecisionRanking: [{ code: '002281', name: '光迅科技', originalAiRank: 1 }],
        executionCalibrationAudit: { selectedCount: 10 },
      },
    },
  });

  const node = app.element('#shadowPanel');
  assert.match(node.innerHTML, /本期策略实验 · 当日冻结结果/);
  assert.match(node.innerHTML, /风险门禁/);
  assert.match(node.innerHTML, /本次为什么是 0 只/);
  assert.match(node.innerHTML, /共检查 3 只，3 只命中风险项（1 分钟尾盘承接 3 只），通过 0 只/);
  assert.match(node.innerHTML, /风险门禁后没有剩余候选/);
  assert.match(node.innerHTML, /第三槽/);
  assert.match(node.innerHTML, /这里展示完整 Top3/);
  assert.match(node.innerHTML, /历史完整执行样本仅 10 笔/);
  assert.match(node.innerHTML, /并非新增推荐/);
  assert.match(node.innerHTML, /120 \/ 120/);
  assert.match(node.innerHTML, /10 只/);
  assert.match(node.innerHTML, /正式影响[\s\S]*无/);
  assert.match(node.innerHTML, /收益要到下一交易日验证后/);
});

test('legacy run without policy data keeps the old shadow fallback', () => {
  const app = setup();
  app.context.renderShadow({ id: 12, market: {} });
  const node = app.element('#shadowPanel');
  assert.match(node.innerHTML, /该 run 未记录影子推荐数据/);
  assert.doesNotMatch(node.innerHTML, /本期策略实验/);
});
