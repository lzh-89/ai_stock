import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/t1-candidate-review.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../t1-overnight-review.html', import.meta.url), 'utf8');
const css = readFileSync(new URL('../src/t1-overnight-review.css', import.meta.url), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const archive = {total:500, archiveVersion:'v1', rounds:[{round:1, planned:80, requested:80, available:78, eligible:75}]};
const next = {job:{status:'complete', target_trade_date:'2026-10-12', resolved_count:500, expected_count:500, updated_at:'2026-10-12 15:10'}, coveragePct:100, groups:[]};
const ok = body => ({ok:true, json:async () => body});
const oldBackend = () => ({ok:false, json:async () => ({error:'invalid run id'})});

function setup(fetcher) {
  const elements = new Map(), events = new Map(), timers = new Map();
  let timerId = 0;
  const element = id => {
    if (!elements.has(id)) elements.set(id, {textContent:'', innerHTML:'', hidden:false, disabled:false, open:false, value:'', listeners:{}, addEventListener(type, fn) { this.listeners[type] = fn; }});
    return elements.get(id);
  };
  vm.runInNewContext(source, {
    document:{getElementById:element}, window:{addEventListener:(type, fn) => events.set(type, fn)},
    fetch:fetcher, AbortController, URLSearchParams, TypeError,
    setTimeout:(fn, ms) => { const id = ++timerId; timers.set(id, {fn, ms}); return id; },
    clearTimeout:id => timers.delete(id),
  });
  return {element, timers, events, select:runId => events.get('t1-review-run')({detail:{runId}})};
}

test('archive failure does not block next-day success', async () => {
  const app = setup(async url => url.endsWith('archive-summary') ? oldBackend() : ok(next));
  app.select(92); await flush();
  assert.match(app.element('archiveMeta').textContent, /请重启后端/);
  assert.equal(app.element('archiveReload').hidden, false);
  assert.equal(app.element('archiveDetails').hidden, true);
  assert.equal(app.element('nextDayMeta').textContent, '已完整');
});

test('next-day failure does not block archive success', async () => {
  const app = setup(async url => url.endsWith('archive-summary') ? ok(archive) : oldBackend());
  app.select(92); await flush();
  assert.match(app.element('archiveMeta').textContent, /500/);
  assert.match(app.element('archiveFunnel').innerHTML, /数据合格/);
  assert.match(app.element('nextDayMeta').textContent, /加载失败/);
  assert.equal(app.element('nextDayReload').hidden, false);
});

test('both errors leave loading state and independent retries recover', async () => {
  let failing = true;
  const calls = [];
  const app = setup(async url => { calls.push(url); return failing ? oldBackend() : ok(url.endsWith('archive-summary') ? archive : next); });
  app.select(88); await flush();
  assert.match(app.element('archiveMeta').textContent, /加载失败/);
  assert.match(app.element('nextDayMeta').textContent, /加载失败/);
  failing = false;
  await app.element('archiveReload').listeners.click(); await flush();
  assert.match(app.element('archiveMeta').textContent, /500/);
  assert.equal(calls.length, 3);
  await app.element('nextDayReload').listeners.click(); await flush();
  assert.equal(app.element('nextDayMeta').textContent, '已完整');
  assert.equal(app.element('nextDayReload').hidden, true);
});

test('legacy archives do not request candidates or fabricate next-day data', async () => {
  const calls = [];
  const app = setup(async url => { calls.push(url); return ok({legacy:true}); });
  app.element('archiveDetails').open = true;
  app.select(1); await flush();
  assert.equal(calls.length, 2);
  assert.equal(app.element('archiveDetails').hidden, true);
  assert.equal(app.element('nextDayMetrics').innerHTML, '');
  assert.equal(app.element('nextDayRetry').disabled, true);
});

test('slow old Run responses cannot overwrite a newer Run', async () => {
  const pending = [];
  const app = setup(url => url.includes('/1/') ? new Promise(resolve => pending.push(resolve)) : Promise.resolve(ok(url.endsWith('archive-summary') ? archive : next)));
  app.select(1); app.select(2); await flush();
  pending.forEach(resolve => resolve(ok({legacy:true}))); await flush();
  assert.match(app.element('archiveMeta').textContent, /500/);
  assert.equal(app.element('nextDayMeta').textContent, '已完整');
});

test('hanging requests time out in both sections', async () => {
  const app = setup((url, {signal}) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name:'AbortError'})))));
  app.select(92);
  [...app.timers.values()].filter(t => t.ms === 15000).forEach(t => t.fn()); await flush();
  assert.match(app.element('archiveMeta').textContent, /请求超时/);
  assert.match(app.element('nextDayMeta').textContent, /请求超时/);
  assert.equal(app.timers.size, 0);
});

test('invalid Run selection makes no requests and clears state', async () => {
  let calls = 0;
  const app = setup(async () => { calls++; return ok({}); });
  app.select('invalid'); await flush();
  assert.equal(calls, 0);
  assert.equal(app.element('nextDayMeta').textContent, '未选择 Run');
});

test('T1-RV-001 page separates group round terminal stage and identity filters', () => {
  assert.match(html, /id="archiveGroup"/);
  assert.match(html, /id="archiveRound"/);
  assert.match(html, /id="archiveStage" aria-label="最终停留阶段"/);
  assert.match(html, /id="archiveIdentity"/);
  assert.match(html, /legacy_setup_rejected/);
});

test('T1-RV-001-D candidate detail opens immediately in a modal and restores the trigger on close', async () => {
  let resolveDetail;
  let restored = false;
  const detailResponse = new Promise(resolve => { resolveDetail = resolve; });
  const app = setup(async url => {
    if (url.endsWith('archive-summary')) return ok(archive);
    if (url.endsWith('next-day-summary')) return ok(next);
    if (url.includes('/candidate/')) return detailResponse;
    return ok({items:[],total:0});
  });
  app.select(112); await flush();
  const button = {dataset:{candidate:'000506'}, closest:() => button, focus:() => { restored = true; }};
  const request = app.element('archiveRows').listeners.click({target:button});
  assert.equal(app.element('candidateDetailModal').hidden, false);
  assert.match(app.element('candidateDetailTitle').textContent, /正在加载/);
  assert.match(app.element('archiveEvidence').innerHTML, /正在读取冻结依据/);
  resolveDetail(ok({candidate:{code:'000506',name:'招金黄金',preliminaryScore:82.6,frozenScanPrice:14.27,
    primaryGroup:'',allocationRound:0,identityTags:[],nextDay:{},scoreComponents:{}},events:[]}));
  await request; await flush();
  assert.match(app.element('candidateDetailTitle').textContent, /招金黄金 000506/);
  assert.match(app.element('archiveEvidence').innerHTML, /本次筛选结果/);
  app.element('candidateDetailClose').listeners.click();
  assert.equal(app.element('candidateDetailModal').hidden, true);
  assert.equal(restored, true);
});

test('T1-RV-001-D candidate modal stays hidden before the user opens it', () => {
  assert.match(html, /id="candidateDetailModal"[^>]*hidden/);
  assert.match(css, /\.candidate-detail-modal\[hidden\]\s*\{\s*display:\s*none;\s*\}/);
});

test('T1-RV-001-D candidate modal closes from the backdrop and Escape key', async () => {
  const app = setup(async url => {
    if (url.endsWith('archive-summary')) return ok(archive);
    if (url.endsWith('next-day-summary')) return ok(next);
    if (url.includes('/candidate/')) return ok({candidate:{code:'000506',name:'招金黄金',identityTags:[],nextDay:{},scoreComponents:{}},events:[]});
    return ok({items:[],total:0});
  });
  app.select(112); await flush();
  const button = {dataset:{candidate:'000506'}, closest:() => button, focus:() => {}};
  await app.element('archiveRows').listeners.click({target:button});
  const modal = app.element('candidateDetailModal');
  modal.listeners.click({target:modal});
  assert.equal(modal.hidden, true);
  await app.element('archiveRows').listeners.click({target:button});
  app.events.get('keydown')({key:'Escape'});
  assert.equal(modal.hidden, true);
});

test('T1-RV-001-D candidate detail failure stays in the modal', async () => {
  const app = setup(async url => {
    if (url.endsWith('archive-summary')) return ok(archive);
    if (url.endsWith('next-day-summary')) return ok(next);
    if (url.includes('/candidate/')) return oldBackend();
    return ok({items:[],total:0});
  });
  app.select(112); await flush();
  const button = {dataset:{candidate:'000506'}, closest:() => button};
  await app.element('archiveRows').listeners.click({target:button}); await flush();
  assert.equal(app.element('candidateDetailModal').hidden, false);
  assert.match(app.element('candidateDetailTitle').textContent, /加载失败/);
  assert.match(app.element('archiveEvidence').innerHTML, /请关闭后重试/);
});

test('T1-RV-001-E candidate detail uses a wide layered layout with collapsed audit facts', () => {
  assert.match(css, /\.action-modal-card\.candidate-detail-card\s*\{[^}]*width:\s*min\(920px,/s);
  assert.match(css, /\.candidate-result-card\s*\{/);
  assert.match(css, /\.candidate-detail-grid\s*\{/);
  assert.match(css, /\.candidate-proxy-grid\s*\{/);
  assert.match(source, /<details class="candidate-audit-details">/);
});

test('T1-RV-006-D/E/F candidate detail explains the result in business language without mixing missing data and rejection', async () => {
  const candidate = {
    code:'300750',name:'宁德时代',preliminaryScore:90.5951,preliminaryScoreRank:12,frozenScanPrice:264.25,
    primaryGroup:'core',allocationRound:1,identityTags:[],industry:'电池',minute15mStatus:'available',minute15mPointCount:14,
    minute15mSource:'tencent_minute',minute15mCutoff:'2026-11-05T14:40',dataEligible:true,setupEligible:false,formalEligible:false,
    terminalStage:'legacy_setup_rejected',terminalStageLabel:'形态未成立',terminalReason:'未确认突破维持、回踩承接、日内转强或相对强势延续',
    terminalRuleVersion:'2026-10-16.formal.v1',selectionPolicyVersion:'2026-10-16.formal.v1',dataQuality:{missing:[]},
    nextDay:{targetTradeDate:'2026-11-06',openProxyReturnPct:-2.07,closeProxyReturnPct:-3.8,highOpportunityPct:2.13,lowAdversePct:-4.68},scoreComponents:{}
  };
  const app = setup(async url => {
    if (url.endsWith('archive-summary')) return ok(archive);
    if (url.endsWith('next-day-summary')) return ok(next);
    if (url.includes('/candidate/')) return ok({candidate,events:[]});
    return ok({items:[],total:0});
  });
  app.select(112); await flush();
  const button = {dataset:{candidate:'300750'}, closest:() => button, focus:() => {}};
  await app.element('archiveRows').listeners.click({target:button});
  const content = app.element('archiveEvidence').innerHTML;
  assert.match(content, /未进入正式推荐/);
  assert.match(content, /止步环节<\/span><strong>交易形态确认/);
  assert.match(content, /这是业务筛选结果，不是系统故障/);
  assert.match(content, /未进入正式证据检查（前一环节已停止）/);
  assert.match(content, /数据缺口<\/dt><dd>无已知数据缺口/);
  assert.match(content, /90\.6 分/);
  assert.match(content, /264\.25 元/);
  assert.match(content, /机会代理不等于真实可实现收益，也不参与当时推荐/);
  assert.match(content, /技术与审计信息/);
});

test('T1-RV-006-D candidate detail separates technical failure from a successful official selection', async () => {
  const candidates = {
    fail:{code:'000001',name:'技术失败示例',terminalStage:'legacy_minute15m_technical_failure',terminalReason:'分钟行情请求超时',identityTags:[],nextDay:{},scoreComponents:{}},
    selected:{code:'000002',name:'正式入选示例',terminalStage:'legacy_official_selected',terminalReason:'通过全部筛选与护栏',identityTags:['official_pick'],nextDay:{},scoreComponents:{}}
  };
  const app = setup(async url => {
    if (url.endsWith('archive-summary')) return ok(archive);
    if (url.endsWith('next-day-summary')) return ok(next);
    if (url.includes('/candidate/000001')) return ok({candidate:candidates.fail,events:[]});
    if (url.includes('/candidate/000002')) return ok({candidate:candidates.selected,events:[]});
    return ok({items:[],total:0});
  });
  app.select(112); await flush();
  const failButton = {dataset:{candidate:'000001'}, closest:() => failButton, focus:() => {}};
  await app.element('archiveRows').listeners.click({target:failButton});
  assert.match(app.element('archiveEvidence').innerHTML, /本次无法完成筛选/);
  assert.match(app.element('archiveEvidence').innerHTML, /系统或数据链路问题/);
  const selectedButton = {dataset:{candidate:'000002'}, closest:() => selectedButton, focus:() => {}};
  await app.element('archiveRows').listeners.click({target:selectedButton});
  assert.match(app.element('archiveEvidence').innerHTML, /已进入正式推荐/);
  assert.match(app.element('archiveEvidence').innerHTML, /正式入选/);
});

test('T1-RV-003 and T1-RV-004 render clear ratios and hide unrecorded V3 stages', async () => {
  const payload = {
    ...next,
    v3:{recorded:false,fullMarketFactCount:0,groups:[]},
    groups:[{group:'all500',total:500,missing:0,excluded:0,planned:120,requested:120,available:118,eligible:110,
      allocationCoveragePct:24,requestCompletionPct:100,dataAvailabilityPct:98.33,dataQualityPassPct:93.22,
      setupPassPct:34.17,semifinalAdvancePct:9.76,officialSelectionPct:75,
      open:{sampleCount:500,meanPct:1,medianPct:1,p25Pct:0,p75Pct:2,positiveProxyRatioPct:60},
      close:{sampleCount:500,meanPct:2,medianPct:1,p25Pct:0,p75Pct:3,positiveProxyRatioPct:65}}],
    leaderCases:[], leaderSummary:{uniqueCount:0,businessConclusionCounts:{},performanceTagCounts:{}}
  };
  const app = setup(async url => ok(url.endsWith('archive-summary') ? archive : payload));
  app.select(112); await flush();
  assert.equal(app.element('nextDayV3Section').hidden, true);
  assert.equal(app.element('nextDayLegacySection').hidden, false);
  assert.match(app.element('nextDayLegacyGroups').innerHTML, /验证分配覆盖 24\.00%/);
  assert.match(app.element('nextDayLegacyGroups').innerHTML, /请求执行完成 100\.00%/);
  assert.doesNotMatch(app.element('nextDayLegacyGroups').innerHTML, /门禁率/);
});

test('T1-RV-005 and T1-RV-006 render five leader filters with stage and reason', async () => {
  const payload = {
    ...next,
    dataScope:{reviewScope:'v3_full_market'},
    v3:{recorded:true,fullMarketFactCount:5425,groups:[]}, groups:[],
    leaderDefinition:'未进入验证且开盘或收盘机会代理为正',
    leaderSummary:{uniqueCount:1,businessConclusionCounts:{normal_tradeable_miss:1},performanceTagCounts:{open_premium_leader:1},tagsMayOverlap:true},
    leaderCases:[{code:'000001',name:'示例股票',preliminaryRank:88,reviewScope:'v3_full_market',
      leaderPerformanceTags:['open_premium_leader'],leaderBusinessConclusion:'normal_tradeable_miss',
      leaderClassificationReasons:['推荐日可交易且未达到高风险阈值'],terminalStage:'validation_not_allocated',
      terminalStageLabel:'未分配验证120',terminalReason:'已进入500但未获得120只验证名额',terminalRuleVersion:'v3',
      openProxyReturnPct:2.5,closeProxyReturnPct:1.2,highOpportunityPct:4.1}]
  };
  const app = setup(async url => ok(url.endsWith('archive-summary') ? archive : payload));
  app.select(113); await flush();
  const content = app.element('nextDayMissed').innerHTML;
  assert.match(content, /开盘溢价领先/);
  assert.match(content, /收盘／盘中拉升/);
  assert.match(content, /推荐日难买/);
  assert.match(content, /高风险/);
  assert.match(content, /正常可交易漏选/);
  assert.match(content, /未分配验证120/);
  assert.match(content, /已进入500但未获得120只验证名额/);
  assert.match(content, /领先案例排序/);
  assert.match(content, /业务结论优先/);
  assert.match(content, /开盘代理降序/);
  assert.match(content, /收盘代理降序/);
  assert.match(content, /盘中最高降序/);
  assert.match(content, /当时排名升序/);
  assert.equal(app.element('nextDayV3Section').hidden, false);
});

test('T1-RV-005-I/J leader cases paginate at 20 rows and reset paging when controls change', async () => {
  const cases = Array.from({length:45}, (_, index) => ({
    code:String(index + 1).padStart(6, '0'), name:`案例${index + 1}`, preliminaryRank:index + 1, reviewScope:'v3_full_market',
    leaderPerformanceTags:[], leaderBusinessConclusion:'normal_tradeable_miss', leaderClassificationReasons:['可交易'],
    terminalStage:'validation_not_allocated', terminalStageLabel:'未分配验证120', terminalReason:'未获得验证名额', terminalRuleVersion:'v3',
    openProxyReturnPct:1, closeProxyReturnPct:1, highOpportunityPct:2
  }));
  const payload = {
    ...next, dataScope:{reviewScope:'v3_full_market'}, groups:[], leaderCases:cases,
    leaderSummary:{uniqueCount:45,businessConclusionCounts:{normal_tradeable_miss:45},performanceTagCounts:{}}
  };
  const app = setup(async url => ok(url.endsWith('archive-summary') ? archive : payload));
  app.select(113); await flush();
  let content = app.element('nextDayMissed').innerHTML;
  assert.match(content, /每页显示/);
  assert.match(content, /当前筛选共 45 只 · 第 1／3 页/);
  assert.match(content, /案例20/);
  assert.doesNotMatch(content, /案例21/);
  assert.match(content, /data-leader-page="prev" disabled/);

  const nextButton = {dataset:{leaderPage:'next'}, closest:selector => selector === '[data-leader-page]' ? nextButton : null};
  app.element('nextDayMissed').listeners.click({target:nextButton});
  content = app.element('nextDayMissed').innerHTML;
  assert.match(content, /当前筛选共 45 只 · 第 2／3 页/);
  assert.match(content, /案例21/);
  assert.match(content, /案例40/);
  assert.doesNotMatch(content, /案例1<\/strong>/);

  app.element('nextDayMissed').listeners.click({target:nextButton});
  content = app.element('nextDayMissed').innerHTML;
  assert.match(content, /第 3／3 页/);
  assert.match(content, /data-leader-page="next" disabled/);

  const sizeSelect = {value:'50', closest:selector => selector === '[data-leader-page-size]' ? sizeSelect : null};
  app.element('nextDayMissed').listeners.change({target:sizeSelect});
  content = app.element('nextDayMissed').innerHTML;
  assert.match(content, /第 1／1 页/);
  assert.match(content, /案例45/);

  sizeSelect.value = '20';
  app.element('nextDayMissed').listeners.change({target:sizeSelect});
  app.element('nextDayMissed').listeners.click({target:nextButton});
  const sortSelect = {value:'rank', closest:selector => selector === '[data-leader-sort]' ? sortSelect : null};
  app.element('nextDayMissed').listeners.change({target:sortSelect});
  assert.match(app.element('nextDayMissed').innerHTML, /第 1／3 页/);

  app.element('nextDayMissed').listeners.click({target:nextButton});
  const highRiskFilter = {dataset:{leaderFilter:'high_risk'}, closest:selector => selector === '[data-leader-filter]' ? highRiskFilter : null};
  app.element('nextDayMissed').listeners.click({target:highRiskFilter});
  assert.match(app.element('nextDayMissed').innerHTML, /当前筛选共 0 只 · 第 1／1 页/);
  assert.match(css, /\.leader-pagination\s*\{/);
});
