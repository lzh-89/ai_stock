import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../t1-overnight-selection.html', import.meta.url), 'utf8');
const source = readFileSync(new URL('../src/t1-overnight-selection.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('../src/t1-overnight-selection.css', import.meta.url), 'utf8');

test('重新生成使用弹窗而不是常驻强制复选框', () => {
  assert.match(html, /id="regenerateButton"/);
  assert.match(html, /id="regenerationModal"[^>]*role="dialog"/);
  assert.doesNotMatch(html, /id="forceCheck"/);
  assert.match(css, /\.regeneration-card/);
});

test('弹窗明确区分两种行情方式和两种结果用途', () => {
  assert.match(html, /value="recompute_frozen"/);
  assert.match(html, /value="refresh_real_then_recompute"/);
  assert.match(html, /value="official"/);
  assert.match(html, /value="research"/);
  assert.match(source, /requestedRunRole: role/);
  assert.match(source, /regenerationMode: mode/);
});

test('页面和请求共同防止重复创建', () => {
  assert.match(source, /randomUUID/);
  assert.match(source, /clientRequestId/);
  assert.match(source, /\["pending", "running"\]\.includes\(state\.run\?\.status\)/);
  assert.match(source, /当前 Run 尚未结束/);
});

test('正式选项显示时间交易日Mock候选池和实验配置的禁用原因', () => {
  assert.match(source, /14:30—14:50 正式信号窗口/);
  assert.match(source, /今天不是 A 股工作日/);
  assert.match(source, /交易日校验未通过/);
  assert.match(source, /Mock 模拟行情只能用于研究/);
  assert.match(source, /候选池少于/);
  assert.match(source, /实验阶段配置/);
});

test('页面展示行情准备状态和AI分层终选审计', () => {
  assert.match(source, /sourceHealth/);
  assert.match(source, /14:39 仍没有满足正式门槛/);
  assert.match(source, /预算化分层比较/);
  assert.match(source, /请求类型/);
  assert.match(source, /正式申请未生效/);
});

test('真实行情不可用不会显示成主动空仓', () => {
  assert.match(source, /market_data_unavailable_zero_pick/);
  assert.match(source, /没有形成买入或主动空仓判断/);
  assert.match(source, /真实行情不可用/);
});
