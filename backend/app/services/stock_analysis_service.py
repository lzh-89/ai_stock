from __future__ import annotations

import http.client
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List

from backend.app.engines.daily_scoring_engine import score_daily_stock
from backend.app.engines.scoring_engine import score_stock
from backend.app.engines.timing_engine import WINDOWS, attach_plan
from backend.app.database import Database
from backend.app.market_context_store import (
    FULL_MARKET_CONTEXT_CACHE_PATH,
    FullMarketContextStore,
    market_context_meta,
    market_context_public_response,
)
from backend.app.providers.daily_kline_provider import DailyKlineProvider
from backend.app.providers.minute_kline_provider import MinuteKlineProvider
from backend.app.providers.provider_common import DEFAULT_UNIVERSE
from backend.app.providers.research_evidence_provider import ResearchEvidenceProvider
from backend.app.providers.tencent_provider import TencentProvider
from backend.app.retail_holder_store import RETAIL_HOLDER_CACHE_PATH, RETAIL_HOLDER_TOP_JSON_PATH
from backend.app.schema import RawStock
from backend.app.services.ai_narrator import _chat_completions_url, _extract_chat_content, _http_error_message
from backend.app.services.stock_analysis_feedback_service import StockAnalysisFeedbackService
from backend.app.settings import Settings
from backend.app.universe_store import load_universe_items


LOGGER = logging.getLogger("ai_stock.stock_analysis")

STOCK_ANALYST_ROLE = """你是一个规则锁定的 A 股个股投研分析主持人和最终裁决者。

你的任务是基于输入证据判断该股票当前是否适合买入，并区分短线、中线、长线。

必须遵守：
1. 只能使用输入 JSON 中已有的行情、K 线、基本面、估值、资金流、新闻公告、市场和板块数据。
2. 不得编造新闻、公告、财务数据、资金流、机构观点或未来涨跌。
3. 数据缺失时必须明确写入 missingData 或 risks，不得用猜测补齐。
4. 数据缺失不是自动否决，但会降低置信度；只有剩余关键证据足够强且关键风险可控时，才允许维持 buy。
5. 允许基于证据权重综合判断，不要因为单一缺失项机械降级，也不要因为单一强指标直接买入。
6. “当前能不能买”必须给出谨慎结论，只能使用 buy、watch、avoid 三类 stance。
7. 周期定义：短线=1-5个交易日，重点看 VWAP 承接、MACD 动能、量能、早盘高低点、板块热度和短期资金；中线=2-8周，重点看日 K 趋势、MA20/MA60 结构、均线斜率、量价配合、回撤与突破；长线=3-12个月，重点看基本面、估值、行业逻辑、长期资金流和长期趋势。
8. 禁止使用“必涨”“稳赚”“满仓”“无风险”等确定性表达。
9. 输出必须是严格 JSON，不要 Markdown，不要解释 JSON 之外的文字。"""


BATTLE_ROLE_PROMPT = """多空博弈是证据辩论层，最终 decision 是主持人综合裁决层。

请先组织角色化投研辩论，再由你作为投研主持人综合角色分歧，输出最终 decision。
不要把本地规则分数做机械加减；规则分、技术指标、资金流、基本面、估值和事件信息都只是证据，不是单独结论。

请至少使用这些金融交易角色：
1. 短线交易员：关注 15m、VWAP、MACD、量能、入场/失效。
2. 趋势交易员：关注日 K、MA20/MA60、20日涨跌幅、趋势延续或破位。
3. 风控经理：关注追高、回撤、流动性、估值过热、缺失数据和失效条件。
4. 基本面研究员：关注行业、营收/利润/ROE、估值 PE/PB。
5. 资金情绪交易员：关注主力流入流出、量比、成交额强度、换手、人气拥挤。
6. 事件驱动研究员：关注新闻公告标题、潜在利好/利空、公告风险。

battle 必须包含：
- source: 固定为 "deepseek-role-debate"。
- bias: bullish / bearish / neutral。
- label: 多方占优 / 空方占优 / 多空拉锯。
- bullScore、bearScore：基于角色观点综合判断的 0-100 分，不要照抄本地规则分。
- roleViews: 数组，每个角色包含 role、stance、strength、weight、horizon、horizonImpacts、riskImpacts、thesis、bullCase、bearCase、keyEvidence、risk、action。
- bullishReasons、bearishReasons：从角色辩论中抽取，必须引用具体数据，说明是否只是辅助背景。
- conclusion：综合角色分歧后的最终结论。

bullScore / bearScore 评分口径：
1. 这两个分数是“当前证据对多方/空方的支持强度”，不是最终买入概率，也不是必须保守贴近 50。
2. 只有多空关键证据确实接近、角色观点明显分裂、且没有一方形成主导证据时，才允许落在 45-55 区间。
3. 轻微占优：主导方 58-65，另一方 35-42；适用于 1-2 个关键证据略占优，但仍需要观察确认。
4. 明显占优：主导方 66-75，另一方 25-34；适用于多数核心角色或关键证据同向，例如趋势、资金、风控同时支持一方。
5. 强占优：主导方 76-88，另一方 12-24；适用于趋势、资金、风险和事件证据高度一致，反方只剩辅助或缺失数据风险。
6. 极端分数 90 以上或 10 以下只能在证据非常充分且风险/反证很少时使用，不能因为单一指标触发。
7. 分数必须和 bias、label、roleViews、bullishReasons、bearishReasons 一致；如果 label 是多方占优或空方占优，主导方与另一方差值通常不应小于 18。
8. decision=watch 不等于 50/50；如果证据明显偏多但缺少买点，可以是多方 62、空方 38；如果证据明显偏空但只是等待修复，可以是多方 35、空方 65。
9. 风控经理的强烈反对、趋势破位、资金持续流出、估值/公告重大风险，应显著提高 bearScore；短线量价、日线趋势、资金流和事件共振，应显著提高 bullScore。
10. 每个 roleViews 必须给出 strength，取值 weak / medium / strong；weight 使用对应角色在当前主分析周期下的建议权重。
11. horizonImpacts 必须分别给出 short、mid、long 对应的 bullish / bearish / neutral，用于说明该角色观点对短线、中线、长线的影响方向。
12. riskImpacts 如存在明显风险，必须分别给出 short、mid、long 对应的 low / medium / high，用于说明风控压制影响哪个周期。
市场热度、样本板块热度只能作为辅助背景；不能因为市场或样本板块偏强就直接给买入结论。"""


ROLE_HORIZON_WEIGHTS = {
    "short": {
        "短线交易员": 25,
        "趋势交易员": 18,
        "风控经理": 22,
        "基本面研究员": 6,
        "资金情绪交易员": 22,
        "事件驱动研究员": 7,
    },
    "mid": {
        "短线交易员": 10,
        "趋势交易员": 28,
        "风控经理": 22,
        "基本面研究员": 15,
        "资金情绪交易员": 17,
        "事件驱动研究员": 8,
    },
    "long": {
        "短线交易员": 5,
        "趋势交易员": 18,
        "风控经理": 24,
        "基本面研究员": 28,
        "资金情绪交易员": 8,
        "事件驱动研究员": 17,
    },
}

ROLE_ALIASES = {
    "短线": "短线交易员",
    "短线交易员": "短线交易员",
    "趋势": "趋势交易员",
    "趋势交易员": "趋势交易员",
    "风控": "风控经理",
    "风控经理": "风控经理",
    "基本面": "基本面研究员",
    "基本面研究员": "基本面研究员",
    "资金": "资金情绪交易员",
    "资金情绪": "资金情绪交易员",
    "资金情绪交易员": "资金情绪交易员",
    "事件": "事件驱动研究员",
    "事件驱动": "事件驱动研究员",
    "事件驱动研究员": "事件驱动研究员",
}

STRENGTH_FACTORS = {"weak": 0.55, "medium": 0.75, "strong": 1.0}
STOCK_ANALYSIS_AI_TIMEOUT_SECONDS = 55


class StockAnalysisService:
    def __init__(self, settings: Settings, database: Database | None = None, feedback_version: str = "v1"):
        self.settings = settings
        self.database = database
        self.tencent_provider = TencentProvider()
        self.kline_provider = MinuteKlineProvider()
        self.daily_kline_provider = DailyKlineProvider(settings.tushare_token)
        self.research_evidence_provider = ResearchEvidenceProvider()
        self.market_context_store = FullMarketContextStore(tencent_provider=self.tencent_provider)
        self.feedback_version = "v2" if str(feedback_version).lower() in {"2", "v2", "stock_analysis_v2"} else "v1"
        self.feedback_service = StockAnalysisFeedbackService(settings, database, version=self.feedback_version) if database else None

    def analyze(
        self,
        code: str,
        risk_mode: str = "balanced",
        scan_time: str = "10:42",
        use_ai: bool = True,
        market_source: str = "auto",
        force_market_context: bool = False,
        position_context: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        requested_input = str(code or "").strip()
        resolved = _resolve_stock_query(requested_input)
        code = resolved["code"]
        scan_time = scan_time if scan_time in WINDOWS else "10:42"
        risk_mode = risk_mode if risk_mode in {"balanced", "aggressive", "defensive"} else "balanced"
        normalized_position_context = _normalize_position_context(position_context)

        stocks, stock, market, market_context = self._load_analysis_market_context(
            code,
            market_source=market_source,
            force_market_context=force_market_context,
        )
        if not stock:
            raise ValueError(f"没有取到 {code} 的腾讯行情")

        sector_heat = _sector_heat(stock, stocks, scope_label=str(market.get("contextLabel") or "重点池样本"))
        intraday = self._build_intraday(stock, risk_mode, scan_time)
        daily = self._build_daily(stock, risk_mode)
        evidence = self.research_evidence_provider.fetch_many([code]).get(code, {})
        evidence_warning = self.research_evidence_provider.last_warning
        evidence_pack = _evidence_pack(stock, market, sector_heat, intraday, daily, evidence)
        if self.feedback_version == "v2":
            evidence_pack["positionContext"] = normalized_position_context
        case_memory_status = self.feedback_service.build_case_memory(evidence_pack) if self.feedback_service else _empty_case_memory_status()

        ai_decision = {}
        ai_warning = ""
        ai_mode = "rules"
        if use_ai and self.settings.enable_ai and self.settings.ai_api_key and self.settings.ai_base_url:
            ai_decision, ai_warning = self._analyze_with_ai(evidence_pack, case_memory_status)
            if ai_decision:
                ai_mode = self.settings.ai_provider

        rule_decision: Dict[str, Any] = {}
        rule_fallback_used = False
        fallback_reason = ""
        if ai_decision:
            decision = _normalize_ai_decision(ai_decision)
        else:
            rule_fallback_used = True
            fallback_reason = ai_warning or "AI 未返回有效个股分析结果，已使用本地规则兜底。"
            rule_decision = _rule_decision(intraday, daily, evidence, market, sector_heat)
            decision = dict(rule_decision)
            decision["warnings"] = _append_unique(decision.get("warnings", []), f"DeepSeek 未完成个股分析，当前为本地规则兜底：{fallback_reason}")
        if ai_warning and not rule_fallback_used:
            decision["warnings"] = _append_unique(decision.get("warnings", []), ai_warning)
        if evidence_warning:
            decision["warnings"] = _append_unique(decision.get("warnings", []), f"投研证据部分失败：{evidence_warning}")
        if normalized_position_context.get("warning"):
            decision["warnings"] = _append_unique(decision.get("warnings", []), str(normalized_position_context.get("warning")))
        if self.feedback_version == "v2":
            decision = _normalize_v2_position_plans(decision, normalized_position_context)
            decision = _apply_v2_trade_plan_validation(decision, evidence_pack)
        snapshot_id = None
        if self.feedback_service:
            try:
                snapshot_id = self.feedback_service.save_snapshot(
                    code=code,
                    name=stock.name,
                    risk_mode=risk_mode,
                    scan_time=scan_time,
                    market_source=market_source,
                    ai_mode=ai_mode,
                    ai_provider=self.settings.ai_provider,
                    ai_model=self.settings.ai_model if ai_mode != "rules" else "",
                    decision=decision,
                    evidence_pack=evidence_pack,
                    rule_fallback_used=rule_fallback_used,
                    case_memory_status=case_memory_status,
                )
            except Exception as error:  # noqa: BLE001 - analysis should remain usable if feedback persistence fails.
                LOGGER.warning("stock analysis feedback snapshot save failed code=%s error=%s", code, error)
                decision["warnings"] = _append_unique(decision.get("warnings", []), f"案例反馈保存失败：{error}")

        LOGGER.info(
            "stock analysis done code=%s ai_mode=%s decision=%s horizon=%s elapsed_ms=%.0f",
            code,
            ai_mode,
            decision.get("decision"),
            decision.get("primaryHorizon"),
            (time.perf_counter() - started) * 1000,
        )
        return {
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "requestedInput": requested_input,
            "resolvedBy": resolved.get("source", ""),
            "resolvedName": resolved.get("name", ""),
            "code": code,
            "name": stock.name,
            "riskMode": risk_mode,
            "scanTime": scan_time,
            "source": market_context.get("source") or "tencent",
            "marketContext": market_context,
            "aiMode": ai_mode,
            "analysisVersion": "stock_analysis_v2" if self.feedback_version == "v2" else "stock_analysis_v1",
            "feedbackVersion": self.feedback_version,
            "aiProvider": self.settings.ai_provider,
            "aiModel": self.settings.ai_model if ai_mode != "rules" else "",
            "decision": decision,
            "ruleFallbackUsed": rule_fallback_used,
            "fallbackReason": fallback_reason,
            "ruleDecision": rule_decision,
            "evidence": evidence_pack,
            "positionContext": normalized_position_context,
            "snapshotId": snapshot_id,
            "caseMemoryStatus": case_memory_status,
            "reviewMemoryStatus": case_memory_status.get("reviewMemoryStatus", {}),
        }

    def search(self, query: str, limit: int = 10) -> Dict[str, Any]:
        return search_stock_lookup(query, limit=limit)

    def market_context(self, source: str = "auto", force: bool = False) -> Dict[str, Any]:
        payload = self.market_context_store.latest_or_refresh(source=source, force=force)
        return market_context_public_response(payload)

    def _load_analysis_market_context(
        self,
        code: str,
        market_source: str,
        force_market_context: bool,
    ) -> tuple[List[RawStock], RawStock | None, Dict[str, Any], Dict[str, Any]]:
        context = (
            self.market_context_store.latest_or_refresh(source=market_source, force=True)
            if force_market_context
            else self.market_context_store.latest_cached()
        )
        stocks = context.get("stocks") or []
        if not stocks:
            LOGGER.warning("stock analysis market context cache missing code=%s source=%s", code, market_source)
            raise ValueError("全市场上下文缓存不存在，请先点击“刷新全市场上下文”后再分析。")

        stock = _find_stock(stocks, code)
        if not stock:
            LOGGER.warning(
                "stock analysis target missing in market context code=%s rows=%s generated=%s stale=%s",
                code,
                len(stocks),
                context.get("generatedAt", ""),
                context.get("stale", False),
            )
            raise ValueError(f"全市场缓存中未找到 {code}，请先刷新全市场上下文后再分析。")

        market = dict(context.get("market") or self.tencent_provider.fetch_market(stocks))
        meta = market_context_meta(context)
        meta["scopeLabel"] = "全市场缓存"
        if meta.get("stale"):
            warning = str(meta.get("warning") or "")
            stale_message = "全市场缓存已过期，市场强弱和板块排名可能不完整。"
            meta["warning"] = _append_warning_text(warning, stale_message)
        market.update(
            {
                "context": meta,
                "contextScope": "full_market",
                "contextLabel": "全市场缓存",
                "contextRows": len(stocks),
                "contextSource": meta.get("source", ""),
                "contextGeneratedAt": meta.get("generatedAt", ""),
                "contextCacheHit": meta.get("cacheHit", False),
                "contextStale": meta.get("stale", False),
                "contextWarning": meta.get("warning", ""),
            }
        )
        LOGGER.info(
            "stock analysis market context cache used code=%s rows=%s stale=%s generated=%s warning=%s",
            code,
            len(stocks),
            meta.get("stale", False),
            meta.get("generatedAt", ""),
            meta.get("warning", ""),
        )
        return stocks, stock, market, meta

    def _build_intraday(self, stock: RawStock, risk_mode: str, scan_time: str) -> Dict[str, Any]:
        series = self.kline_provider.fetch_many([stock.code], period=self.settings.kline_period).get(stock.code, [])
        if series:
            stock.kline_series = series
            source_for_code = getattr(self.kline_provider, "source_for_code", None)
            stock.kline_source = source_for_code(stock.code) if callable(source_for_code) else self.kline_provider.source
            stock.kline_period = self.settings.kline_period
        candidate = attach_plan(score_stock(stock, risk_mode=risk_mode), scan_time=scan_time)
        return candidate

    def _build_daily(self, stock: RawStock, risk_mode: str) -> Dict[str, Any]:
        series = self.daily_kline_provider.fetch_many(
            [stock.code],
            days=180,
            force=False,
            cache_only=False,
            fetcher_timeout_seconds=6,
        ).get(stock.code)
        if not series:
            return {
                "available": False,
                "warning": self.daily_kline_provider.last_warning or "未取到足够日 K",
                "klineSource": self.daily_kline_provider.last_source,
            }
        candidate = score_daily_stock(
            stock,
            series,
            risk_mode=risk_mode,
            kline_source=self.daily_kline_provider.last_source or "daily_kline",
        )
        candidate["available"] = True
        return candidate

    def _analyze_with_ai(self, evidence_pack: Dict[str, Any], case_memory_status: Dict[str, Any] | None = None) -> tuple[Dict[str, Any], str]:
        prompt_context: Dict[str, Any] = {"evidencePack": evidence_pack}
        if self.feedback_service and case_memory_status:
            prompt_context.update(self.feedback_service.prompt_payload(case_memory_status))
        case_prompt = ""
        if case_memory_status and case_memory_status.get("injected"):
            case_prompt = (
                "本次输入可能包含 caseMemory/reviewMemory 以及按周期隔离的 caseMemoryByHorizon/reviewMemoryByHorizon。"
                "历史案例和复盘经验只能用于校准判断，不能覆盖硬风控。"
                "必须严格按周期使用历史样本：horizons.short 和 newPositionPlan 只能参考 short 样本池；"
                "horizons.mid 只能参考 mid 样本池；horizons.long 只能参考 long 样本池。"
                "不得用 60/120/240 日长线成功案例证明短线可以买，也不得用 3/5/7/10 日短线案例否定长线逻辑。"
                "如果 primaryHorizon=mid 或 long，最终 caseCalibration 应说明对应周期样本池是否已注入、样本是否足够。"
                "如果当前命中硬风控，即使相似历史案例表现较好，也不得直接输出 buy。"
                "如果案例建议和风控结论冲突，必须输出 caseVsRiskConflict，并说明最终如何处理。"
                "普通风险可以通过权重综合，但需要说明权重依据和确认条件。"
            )
        position_plan_prompt = ""
        if self.feedback_version == "v2":
            position_plan_prompt = (
                "V2 版本根字段还必须包含 positionState、newPositionPlan、existingPositionPlan。"
                "positionState 只能是 none 或 existing，且必须与 evidencePack.positionContext.positionState 完全一致。"
                "positionContext 是用户本次分析临时录入的持仓上下文，不是永久持仓表；不得自行编造用户持仓。"
                "如果 evidencePack.positionContext.positionState=none，existingPositionPlan 只能说明未录入持仓，不能输出真实减仓、卖出、止损动作。"
                "newPositionPlan 是新开仓计划，必须包含 actionable、horizon、buyPriceLow、buyPriceHigh、stopLossPrice、takeProfitPrice、"
                "sellCondition、positionSuggestion、reason、riskReward、canSellToday、minHoldingDays、entryEvidence、nextDayStopPlan、invalidCondition。"
                "A 股新开仓必须按 T+1 约束，newPositionPlan.canSellToday 必须为 false，minHoldingDays 必须 >= 1；"
                "不得在 newPositionPlan 中写买入当天卖出、买入后 15-30 分钟止损、盘中止损、日内减仓等 T+0 执行动作。"
                "newPositionPlan.sellCondition 只能写 T+1 之后或后续交易日的退出条件。"
                "existingPositionPlan 是已有仓位处理，必须包含 action、canSellToday、intradayAction、stopLossPrice、takeProfitPrice、"
                "sellCondition、positionSuggestion、reason、riskLevel；它可以描述已有持仓的盘中减仓/止损动作，但不能作为新开仓买点。"
                "如果 positionContext.positionState=existing，existingPositionPlan 必须显式参考 positionInfo.costPrice、positionPct、holdingDays、plannedHorizon；"
                "止损价、止盈价和仓位处理理由要结合用户成本和当前价格的盈亏状态，不得只复制新开仓计划。"
                "tradePlan 作为兼容字段，必须与 newPositionPlan 的新开仓买入区间、止损、止盈和周期保持一致。"
            )
        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {"role": "system", "content": STOCK_ANALYST_ROLE},
                {
                    "role": "user",
                    "content": (
                        "请基于 evidencePack 输出 JSON。"
                        "根字段必须包含 decision、decisionText、primaryHorizon、confidence、horizons、tradePlan、battle、summary、supportingEvidence、risks、missingData、userExplanation。"
                        "horizons 必须包含 short、mid、long，每项包含 stance、label、reason、entry、invalid。"
                        "horizons.*.entry 的含义必须跟 stance 匹配：stance=buy 时才写可执行入场/买入条件；"
                        "stance=watch 时只能写观察确认条件，例如站回均线、放量确认、回踩不破；不得写成当前可以买的价格区间；"
                        "stance=avoid 时只能写重新评估条件，例如风险解除、重新站上关键均线、资金流改善；不得写买入区间。"
                        "horizons.*.invalid 的含义必须跟 stance 匹配：buy 写止损/失效条件；watch 写观察失败条件；avoid 写继续回避条件。"
                        "tradePlan 必须包含 actionable、horizon、buyPriceLow、buyPriceHigh、stopLossPrice、takeProfitPrice、sellCondition、positionSuggestion、reason、riskReward。"
                        "只有 decision=buy 或任一 horizons.*.stance=buy 时，actionable 才能为 true。"
                        "如果 decision=buy，必须给出数字型 buyPriceLow、buyPriceHigh、stopLossPrice；买入区间必须来自输入中的现价、VWAP、早盘高低点、MA20/MA60、支撑/压力等具体证据。"
                        "如果无法给出明确买入区间或止损价，不得输出 decision=buy，必须降级为 watch。"
                        "takeProfitPrice 如证据不足可为 null，但必须给 sellCondition。"
                        "battle 必须包含 source、bias、label、bullScore、bearScore、roleViews、bullishReasons、bearishReasons、conclusion；"
                        "多空理由必须引用输入中的具体数值，并说明该证据为什么支持看多或看空、是否只是辅助背景。"
                        "多空理由正文面向用户展示，不要直接写 evidencePack 字段路径或调试字段名，例如 intraday.score、daily.dailyMetrics、moneyFlow.mainNetInflow、market.breadth.down；"
                        "需要引用证据时请写成自然语言，例如“15分钟强度87分”“近5日主力净流出8990万”“全市场下跌4056只”。"
                        "bullishReasons 和 bearishReasons 可输出字符串；如输出对象，每项使用 title、detail、strength、evidenceType、caveat。"
                        "stance 只能是 buy/watch/avoid；primaryHorizon 只能是 short/mid/long/none。"
                        "supportingEvidence 必须引用输入中的具体数值或证据标题。"
                        "userExplanation 是给普通用户阅读的展示解释层，不参与 decision/tradePlan 裁决。"
                        "userExplanation 必须包含 plainDecision、why、considerCondition、avoidCondition。"
                        "plainDecision 用一句话说明当前能否操作，例如“先观察，不建议现在追买”。"
                        "why 必须是 2-4 条普通用户能理解的中文原因，避免裸写 VWAP、MACD、MA20、PE 等术语。"
                        "considerCondition 和 avoidCondition 必须包含 text、terms；text 写清触发/失效条件。"
                        "terms 是数组，每项包含 term、value、meaning、whyItMatters；如果 text 中出现 VWAP、MA20、MA60、早盘高点、早盘低点、支撑位、压力位等价格条件，"
                        "必须在 terms.value 中给出输入证据里对应的数字；如果证据包没有该数值，value 必须为 null，并在 meaning 或 whyItMatters 说明数据缺失，不能编造。"
                        "VWAP 的 meaning 固定解释为“当天平均成交成本线”；whyItMatters 必须说明它只用于短线承接判断，不能单独作为买入理由。"
                        "MACD 金叉/死叉、放量、量比、回踩不破、MA20/MA60、PE/PB、主力净流入等术语如出现，也必须进入 terms，并说明含义和作用。"
                        "如输入包含 caseMemory/reviewMemory/caseMemoryByHorizon/reviewMemoryByHorizon，必须输出 caseCalibration、reviewMemoryApplied；"
                        "caseCalibration 必须说明 short/mid/long 哪些样本池被使用、哪些因样本不足未使用，以及是否存在跨周期冲突。"
                        "如历史案例建议和风控冲突，必须输出 caseVsRiskConflict。"
                        f"{position_plan_prompt}"
                        f"{case_prompt}"
                        f"{BATTLE_ROLE_PROMPT}"
                        "不要输出 Markdown。\n"
                        f"{json.dumps(prompt_context, ensure_ascii=False)}"
                    ),
                },
            ],
            "temperature": 0.15,
            "max_tokens": 6000,
            "stream": False,
        }
        if self.settings.ai_provider in {"deepseek", "openai"}:
            payload["response_format"] = {"type": "json_object"}
        if self.settings.ai_provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        last_error = ""
        for attempt in range(1, 3):
            started = time.perf_counter()
            LOGGER.info(
                "stock analysis ai start provider=%s model=%s attempt=%s timeout_seconds=%s code=%s",
                self.settings.ai_provider,
                self.settings.ai_model,
                attempt,
                STOCK_ANALYSIS_AI_TIMEOUT_SECONDS,
                evidence_pack.get("snapshot", {}).get("code"),
            )
            request = urllib.request.Request(
                _chat_completions_url(self.settings.ai_base_url),
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.settings.ai_api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=STOCK_ANALYSIS_AI_TIMEOUT_SECONDS) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                return {}, _http_error_message(error)
            except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.IncompleteRead, json.JSONDecodeError) as error:
                last_error = f"{self.settings.ai_provider} 个股分析调用失败：{error}"
                LOGGER.warning(
                    "stock analysis ai failed provider=%s attempt=%s elapsed_ms=%.0f error=%s",
                    self.settings.ai_provider,
                    attempt,
                    (time.perf_counter() - started) * 1000,
                    error,
                )
                continue
            content = _extract_chat_content(data)
            decision = _extract_json_object(content)
            finish_reason = _extract_finish_reason(data)
            LOGGER.info(
                "stock analysis ai ok provider=%s attempt=%s elapsed_ms=%.0f valid=%s finish_reason=%s content_len=%s",
                self.settings.ai_provider,
                attempt,
                (time.perf_counter() - started) * 1000,
                bool(decision),
                finish_reason,
                len(content),
            )
            if decision:
                return decision, ""
            LOGGER.warning(
                "stock analysis ai invalid_json provider=%s attempt=%s finish_reason=%s content_len=%s content_preview=%s",
                self.settings.ai_provider,
                attempt,
                finish_reason,
                len(content),
                content[:1000].replace("\n", " "),
            )
            last_error = f"{self.settings.ai_provider} 返回内容不是有效 JSON"
        return {}, last_error


def _context_codes(code: str) -> List[str]:
    codes = [code]
    codes.extend(item_code for item_code, _ in load_universe_items())
    seen = set()
    result = []
    for item in codes:
        normalized = _normalize_code(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result[:120]


def _resolve_stock_query(query: Any) -> Dict[str, str]:
    text = str(query or "").strip()
    code = _normalize_code(text)
    if code:
        return {"code": code, "name": "", "source": "code"}
    if not text:
        raise ValueError("请输入股票代码或股票名称")

    lookup = _normalize_lookup_text(text)
    items = _stock_lookup_items()
    exact = _unique_lookup_items([item for item in items if _normalize_lookup_text(item.get("name", "")) == lookup])
    if exact:
        return _single_or_error(text, exact)

    fuzzy = _unique_lookup_items(
        [
            item
            for item in items
            if lookup
            and (
                lookup in _normalize_lookup_text(item.get("name", ""))
                or _normalize_lookup_text(item.get("name", "")) in lookup
            )
        ]
    )
    if len(fuzzy) == 1:
        return fuzzy[0]
    if fuzzy:
        candidates = "、".join(f"{item.get('name')}({item.get('code')})" for item in fuzzy[:8])
        raise ValueError(f"股票名称“{text}”匹配到多个股票：{candidates}。请补全名称或直接输入 6 位代码。")
    raise ValueError(f"没有找到股票名称“{text}”对应的代码。请换成 6 位股票代码，或先更新重点池/日线推荐缓存。")


def search_stock_lookup(query: Any, limit: int = 10) -> Dict[str, Any]:
    text = str(query or "").strip()
    limit = max(1, min(20, int(limit or 10)))
    if not text:
        return {"query": "", "count": 0, "items": []}

    lookup = _normalize_lookup_text(text)
    digits = re.sub(r"\D+", "", text)
    ranked: List[tuple[int, Dict[str, str]]] = []
    for item in _stock_lookup_items():
        code = item.get("code", "")
        name = item.get("name", "")
        normalized_name = _normalize_lookup_text(name)
        score = None
        if digits:
            if code == digits:
                score = 0
            elif code.startswith(digits):
                score = 8
            elif digits in code:
                score = 18
        if lookup:
            if normalized_name == lookup:
                score = min(score if score is not None else 100, 2)
            elif normalized_name.startswith(lookup):
                score = min(score if score is not None else 100, 10)
            elif lookup in normalized_name:
                score = min(score if score is not None else 100, 22)
            elif normalized_name and normalized_name in lookup:
                score = min(score if score is not None else 100, 28)
        if score is not None:
            ranked.append((score + _lookup_source_penalty(item.get("source", "")), item))

    ranked.sort(key=lambda row: (row[0], row[1].get("code", ""), row[1].get("name", "")))
    results = _unique_search_results([item for _, item in ranked])[:limit]
    return {"query": text, "count": len(results), "items": results}


def _stock_lookup_items() -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for code, name in load_universe_items():
        items.append(_lookup_item(code, name, "重点池"))
    for code, name in DEFAULT_UNIVERSE:
        items.append(_lookup_item(code, name, "内置重点池"))

    for path, source in [
        (FULL_MARKET_CONTEXT_CACHE_PATH, "全市场缓存"),
        (RETAIL_HOLDER_CACHE_PATH, "散户榜缓存"),
        (RETAIL_HOLDER_TOP_JSON_PATH, "散户榜Top100"),
    ]:
        items.extend(_json_lookup_items(path, source))
    return _unique_lookup_items(items)


def _json_lookup_items(path: Any, source: str) -> List[Dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError, AttributeError):
        return []
    return _collect_lookup_items(payload, source)


def _collect_lookup_items(value: Any, source: str) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    if isinstance(value, dict):
        item = _lookup_item(value.get("code"), value.get("name"), source)
        if item:
            result.append(item)
        for child in value.values():
            result.extend(_collect_lookup_items(child, source))
    elif isinstance(value, list):
        for child in value:
            result.extend(_collect_lookup_items(child, source))
    return result


def _lookup_item(code: Any, name: Any, source: str) -> Dict[str, str]:
    normalized_code = _normalize_code(code)
    normalized_name = str(name or "").strip()
    if not normalized_code or not normalized_name:
        return {}
    return {"code": normalized_code, "name": normalized_name, "source": source}


def _unique_lookup_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen = set()
    for item in items:
        code = item.get("code", "")
        name = item.get("name", "")
        key = (code, name)
        if not code or not name or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _unique_search_results(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen = set()
    for item in items:
        code = item.get("code", "")
        if not code or code in seen:
            continue
        seen.add(code)
        result.append({"code": code, "name": item.get("name", ""), "source": item.get("source", "")})
    return result


def _lookup_source_penalty(source: str) -> int:
    order = {
        "重点池": 0,
        "全市场缓存": 1,
        "日线推荐历史": 2,
        "日线分析缓存": 3,
        "散户榜Top100": 4,
        "散户榜缓存": 5,
        "内置重点池": 6,
    }
    return order.get(source, 9)


def _single_or_error(query: str, items: List[Dict[str, str]]) -> Dict[str, str]:
    codes = {item.get("code") for item in items}
    if len(codes) == 1:
        return items[0]
    candidates = "、".join(f"{item.get('name')}({item.get('code')})" for item in items[:8])
    raise ValueError(f"股票名称“{query}”匹配到多个股票：{candidates}。请直接输入 6 位代码。")


def _normalize_lookup_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _normalize_code(code: Any) -> str:
    match = re.search(r"\d{6}", str(code or ""))
    return match.group(0) if match else ""


def _find_stock(stocks: List[RawStock], code: str) -> RawStock | None:
    for stock in stocks:
        if stock.code == code:
            return stock
    return None


def _sector_heat(stock: RawStock, stocks: List[RawStock], scope_label: str = "样本") -> Dict[str, Any]:
    boards: Dict[str, List[RawStock]] = {}
    for item in stocks:
        boards.setdefault(item.sector, []).append(item)
    rows = []
    for sector, items in boards.items():
        if not items:
            continue
        rows.append(
            {
                "sector": sector,
                "avgChange": round(sum(item.change for item in items) / len(items), 2),
                "up": len([item for item in items if item.change > 0]),
                "down": len([item for item in items if item.change < 0]),
                "limitUp": len([item for item in items if item.limit_status in {"limit_up", "one_word_limit_up"}]),
                "count": len(items),
            }
        )
    ranked = sorted(rows, key=lambda item: item["avgChange"], reverse=True)
    rank_map = {item["sector"]: index for index, item in enumerate(ranked, start=1)}
    current = next((item for item in ranked if item["sector"] == stock.sector), {})
    return {
        "scopeLabel": scope_label,
        "currentSector": stock.sector,
        "currentRank": rank_map.get(stock.sector, 0),
        "current": current,
        "top": ranked[:5],
    }


def _normalize_position_context(value: Any) -> Dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    state = str(raw.get("positionState", raw.get("position_state", "none")) or "none").strip().lower()
    if state != "existing":
        return {
            "positionState": "none",
            "positionInfo": {},
            "canSellToday": False,
            "scope": "single_analysis_request",
            "source": str(raw.get("source") or "user_input"),
        }

    raw_info = raw.get("positionInfo", raw.get("position_info", {}))
    info = raw_info if isinstance(raw_info, dict) else {}
    cost_price = _safe_float(info.get("costPrice", info.get("cost_price")))
    position_pct = _safe_float(info.get("positionPct", info.get("position_pct")))
    holding_days = _safe_float(info.get("holdingDays", info.get("holding_days")))
    if cost_price is None or cost_price <= 0 or position_pct is None or position_pct <= 0:
        return {
            "positionState": "none",
            "positionInfo": {},
            "canSellToday": False,
            "scope": "single_analysis_request",
            "source": str(raw.get("source") or "user_input"),
            "warning": "已有持仓上下文缺少合法成本价或仓位比例，已按未持仓处理。",
        }
    planned_horizon = str(info.get("plannedHorizon", info.get("planned_horizon", "")) or "").strip().lower()
    position_info: Dict[str, Any] = {
        "costPrice": round(float(cost_price), 4),
        "positionPct": min(100.0, round(float(position_pct), 2)),
        "holdingDays": max(0, int(holding_days or 0)),
    }
    if planned_horizon in {"short", "mid", "long"}:
        position_info["plannedHorizon"] = planned_horizon
    return {
        "positionState": "existing",
        "positionInfo": position_info,
        "canSellToday": True,
        "scope": "single_analysis_request",
        "source": str(raw.get("source") or "user_input"),
    }


def _evidence_pack(
    stock: RawStock,
    market: Dict[str, Any],
    sector_heat: Dict[str, Any],
    intraday: Dict[str, Any],
    daily: Dict[str, Any],
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "market": {
            "breadth": market.get("breadth"),
            "limitUp": market.get("limitUp"),
            "limitDown": market.get("limitDown"),
            "hotSectors": market.get("hotSectors"),
            "mode": market.get("mode"),
            "context": market.get("context") or {},
            "contextScope": market.get("contextScope", ""),
            "contextLabel": market.get("contextLabel", ""),
            "contextRows": market.get("contextRows", 0),
            "contextSource": market.get("contextSource", ""),
            "contextGeneratedAt": market.get("contextGeneratedAt", ""),
            "contextCacheHit": market.get("contextCacheHit", False),
            "contextStale": market.get("contextStale", False),
            "contextWarning": market.get("contextWarning", ""),
        },
        "sectorHeat": sector_heat,
        "snapshot": {
            "code": stock.code,
            "name": stock.name,
            "sector": stock.sector,
            "theme": stock.theme,
            "price": stock.price,
            "change": stock.change,
            "turnover": stock.turnover,
            "volumeRatio": stock.volume_ratio,
            "amountFactor": stock.amount_factor,
            "sectorRank": stock.sector_rank,
            "sectorLimitUps": stock.sector_limit_ups,
            "relStrength": stock.rel_strength,
            "riskFlags": stock.risk_flags,
            "limitStatus": stock.limit_status,
        },
        "intraday": _compact_intraday(intraday),
        "daily": _compact_daily(daily),
        "fundamentals": evidence.get("fundamentals") or {},
        "valuation": evidence.get("valuation") or {},
        "moneyFlow": evidence.get("moneyFlow") or {},
        "news": (evidence.get("news") or [])[:5],
        "announcements": (evidence.get("announcements") or [])[:5],
        "evidenceStatus": evidence.get("evidenceStatus") or {},
        "missingData": evidence.get("missingData") or [],
        "evidenceWarnings": evidence.get("warnings") or [],
    }


def _compact_intraday(candidate: Dict[str, Any]) -> Dict[str, Any]:
    last = candidate.get("last") or {}
    return {
        "score": candidate.get("score"),
        "status": candidate.get("status"),
        "style": candidate.get("style"),
        "dimensions": candidate.get("dimensions"),
        "checks": candidate.get("checks"),
        "passed": candidate.get("passed"),
        "tradeability": candidate.get("tradeability"),
        "buyWindow": candidate.get("buyWindow"),
        "buyTrigger": candidate.get("buyTrigger"),
        "sellWindow": candidate.get("sellWindow"),
        "sellTrigger": candidate.get("sellTrigger"),
        "invalidCondition": candidate.get("invalidCondition"),
        "klineSource": candidate.get("klineSource"),
        "klinePeriod": candidate.get("klinePeriod"),
        "lastClose": last.get("close"),
        "lastVwap": candidate.get("lastVwap"),
        "morningHigh": candidate.get("morningHigh"),
        "morningLow": candidate.get("morningLow"),
    }


def _compact_daily(candidate: Dict[str, Any]) -> Dict[str, Any]:
    if not candidate or not candidate.get("available", True):
        return {
            "available": False,
            "warning": candidate.get("warning", "未取到足够日 K") if candidate else "未取到足够日 K",
            "klineSource": candidate.get("klineSource", "") if candidate else "",
        }
    return {
        "available": True,
        "score": candidate.get("score"),
        "status": candidate.get("status"),
        "style": candidate.get("style"),
        "dimensions": candidate.get("dimensions"),
        "checks": candidate.get("checks"),
        "dailyMetrics": candidate.get("dailyMetrics"),
        "dailyTrend": candidate.get("dailyTrend"),
        "riskFlags": candidate.get("riskFlags"),
        "buyTrigger": candidate.get("buyTrigger"),
        "sellTrigger": candidate.get("sellTrigger"),
        "invalidCondition": candidate.get("invalidCondition"),
        "klineSource": candidate.get("klineSource"),
    }


def _rule_decision(
    intraday: Dict[str, Any],
    daily: Dict[str, Any],
    evidence: Dict[str, Any],
    market: Dict[str, Any],
    sector_heat: Dict[str, Any],
) -> Dict[str, Any]:
    short = _short_horizon(intraday, daily, market, sector_heat)
    mid = _mid_horizon(daily, sector_heat)
    long = _long_horizon(daily, evidence)
    battle = _long_short_battle(intraday, daily, evidence, market, sector_heat)
    if short["stance"] == "buy":
        decision = "buy"
        decision_text = "当前只适合短线小仓试错"
        primary = "short"
    elif short["stance"] == "watch" or mid["stance"] == "buy":
        decision = "watch"
        decision_text = "当前以观察确认优先"
        primary = "mid" if mid["stance"] == "buy" else "short"
    elif long["stance"] == "buy":
        decision = "watch"
        decision_text = "长期证据可跟踪，但不等于当前买点"
        primary = "long"
    else:
        decision = "avoid"
        decision_text = "当前不建议买入"
        primary = "none"
    missing = list(evidence.get("missingData") or [])
    if not daily or not daily.get("available", True):
        missing.append("日 K 不足")
    context_label = str(market.get("contextLabel") or sector_heat.get("scopeLabel") or "样本")
    supporting = [
        f"15m 分数 {intraday.get('score', 0)}，状态 {intraday.get('status', '--')}，风格 {intraday.get('style', '--')}",
        f"{context_label}宽度 {market.get('breadth', {}).get('up', 0)}涨/{market.get('breadth', {}).get('down', 0)}跌，市场模式 {market.get('mode', '--')}",
        f"板块 {sector_heat.get('currentSector', '--')} {context_label}排名第 {sector_heat.get('currentRank') or '--'}",
    ]
    if daily and daily.get("available", True):
        metrics = daily.get("dailyMetrics") or {}
        supporting.append(
            f"日线分数 {daily.get('score', 0)}，20日涨跌幅 {metrics.get('return20d', '--')}%，距 MA20 {metrics.get('distanceMa20', '--')}%"
        )
    return {
        "decision": decision,
        "decisionText": decision_text,
        "primaryHorizon": primary,
        "confidence": _rule_confidence(short, mid, long, missing),
        "horizons": {"short": short, "mid": mid, "long": long},
        "battle": battle,
        "summary": [decision_text, short["reason"], mid["reason"], long["reason"]],
        "supportingEvidence": supporting,
        "risks": _risk_list(intraday, daily, evidence),
        "missingData": _unique(missing),
        "warnings": [],
    }


def _long_short_battle(
    intraday: Dict[str, Any],
    daily: Dict[str, Any],
    evidence: Dict[str, Any],
    market: Dict[str, Any],
    sector_heat: Dict[str, Any],
) -> Dict[str, Any]:
    bull_score = 0
    bear_score = 0
    bullish: List[Any] = []
    bearish: List[Any] = []

    def add_bull(
        points: int,
        title: str,
        detail: str = "",
        strength: str = "中",
        evidence_type: str = "直接证据",
        caveat: str = "",
    ) -> None:
        nonlocal bull_score
        if title:
            bull_score += points
            bullish.append(_battle_reason(title, detail, strength, evidence_type, caveat))

    def add_bear(
        points: int,
        title: str,
        detail: str = "",
        strength: str = "中",
        evidence_type: str = "直接证据",
        caveat: str = "",
    ) -> None:
        nonlocal bear_score
        if title:
            bear_score += points
            bearish.append(_battle_reason(title, detail, strength, evidence_type, caveat))

    intraday_score = int(intraday.get("score") or 0)
    intraday_status = str(intraday.get("status") or "")
    intraday_style = str(intraday.get("style") or "")
    close = _safe_float((intraday.get("last") or {}).get("close"))
    last_vwap = _safe_float(intraday.get("lastVwap"))
    amount_factor = _safe_float(intraday.get("amountFactor"))
    volume_ratio = _safe_float(intraday.get("volumeRatio"))
    if intraday_score >= 78 and intraday_status in {"buy-triggered", "pullback"}:
        add_bull(
            22,
            f"15m 出现{intraday_style or '买点'}信号",
            f"15m 综合分 {intraday_score} 已达到买点触发阈值 78，状态为 {intraday_status}。这说明当前分时量价、均价线、动能和可交易性至少同时满足了多项规则。",
            "强",
            "直接证据",
            "仍必须看失效位，跌回 VWAP、早盘低点或触发流动性风险时不能继续按买点处理。",
        )
    elif intraday_score >= 65:
        add_bull(
            10,
            "15m 处于可观察区间",
            f"15m 综合分 {intraday_score} 高于合格线 65，说明短线结构不是完全走弱；但状态仍是 {intraday_status or '--'}，还没有进入明确买点触发。",
            "弱",
            "直接证据",
            "这只能说明可以继续观察，不能直接等同于买入。",
        )
        add_bear(
            10,
            "15m 尚未触发买点阈值",
            f"当前 15m 分数 {intraday_score} 未达到 78，状态 {intraday_status or '--'}，说明信号还停留在候选/观察层。",
            "中",
            "直接证据",
            "如果后续量能、VWAP 或 MACD 走弱，应按短线未确认处理。",
        )
    else:
        add_bear(
            18,
            "15m 未过合格线",
            f"15m 综合分 {intraday_score} 低于 65，说明当前分时量价、动能或风险项没有形成足够交易条件。",
            "强",
            "直接证据",
            "短线没有先过合格线时，其他市场热度只能作为背景，不能抵消个股信号不足。",
        )

    for check in intraday.get("checks") or []:
        label = str(check.get("label") or "")
        ok = bool(check.get("ok"))
        if ok and label in {"价格站上 VWAP", "MACD 走强", "均线结构偏多", "量能强于样本"}:
            add_bull(5, f"15m 规则通过：{label}", _intraday_check_detail(label, True, intraday), "中", "直接证据", _intraday_check_caveat(label))
        elif not ok and label in {"价格站上 VWAP", "MACD 走强", "均线结构偏多", "量能强于样本"}:
            add_bear(5, f"15m 规则未通过：{label}", _intraday_check_detail(label, False, intraday), "中", "直接证据", _intraday_check_caveat(label))

    breadth = market.get("breadth") or {}
    up = int(breadth.get("up") or 0)
    down = int(breadth.get("down") or 0)
    mode = str(market.get("mode") or "--")
    context_label = str(market.get("contextLabel") or "本轮样本")
    if up > down:
        add_bull(
            10,
            "市场宽度偏多",
            f"{context_label}里 {up} 只上涨、{down} 只下跌，市场模式为 {mode}。这说明当前环境更容易容纳短线做多资金，个股信号的成功率背景更友好。",
            "弱",
            "辅助背景",
            "这不是个股买点，只能说明市场环境偏暖；如果个股自身量价或日线不成立，不能因为市场宽度好就买。",
        )
    elif down > up:
        add_bear(
            10,
            "市场宽度偏空",
            f"{context_label}里 {up} 只上涨、{down} 只下跌，市场模式为 {mode}。这说明短线环境偏弱，个股信号更容易受市场拖累。",
            "中",
            "辅助背景",
            "市场宽度偏空不代表个股一定下跌，但会降低追买容错率。",
        )

    sector_rank = int(sector_heat.get("currentRank") or 0)
    current_sector = sector_heat.get("current") or {}
    sector_name = sector_heat.get("currentSector") or "--"
    sector_scope = str(sector_heat.get("scopeLabel") or context_label or "样本")
    avg_change = current_sector.get("avgChange")
    if sector_rank and sector_rank <= 3:
        add_bull(
            10,
            f"{sector_scope}板块热度靠前",
            f"{sector_name} 在{sector_scope}板块里排名第 {sector_rank}，均涨跌 {avg_change if avg_change is not None else '--'}%。这说明该股所在板块有资金聚集或涨幅扩散迹象。",
            "弱",
            "辅助背景",
            "板块热度仍是辅助背景；只有和个股 VWAP、量能、日线趋势共振时才算有效加分。",
        )
    elif sector_rank:
        add_bear(
            6,
            f"{sector_scope}板块热度不强",
            f"{sector_name} 在{sector_scope}板块里排名第 {sector_rank}，说明板块合力没有排在前列。",
            "弱",
            "辅助背景",
            "板块弱不一定否定个股，但会降低短线资金接力意愿。",
        )

    if daily and daily.get("available", True):
        daily_score = int(daily.get("score") or 0)
        daily_status = str(daily.get("status") or "")
        trend = daily.get("dailyTrend") or {}
        metrics = daily.get("dailyMetrics") or {}
        return20 = metrics.get("return20d")
        distance_ma20 = metrics.get("distanceMa20")
        if daily_score >= 70 and trend.get("aboveMa20") and trend.get("aboveMa60"):
            add_bull(
                18,
                "日线趋势确认",
                f"日线分数 {daily_score}，价格同时站上 MA20 和 MA60，说明中线趋势没有明显破位，短线买点不会逆着主要均线方向。",
                "强",
                "直接证据",
                "日线趋势只能说明结构可做，不能替代短线入场点；连续大涨或高位滞涨仍要降级。",
            )
        elif daily_status == "daily-risk":
            add_bear(
                22,
                "日线风险未解除",
                f"日线分数 {daily_score}，状态 {daily_status}，MA20/MA60 站上情况为 {bool(trend.get('aboveMa20'))}/{bool(trend.get('aboveMa60'))}，说明中线结构仍有破位或趋势不足风险。",
                "强",
                "直接证据",
                "日线风险存在时，15m 反弹更容易只是弱修复，不宜当作确定买点。",
            )
        elif daily_score >= 55:
            add_bull(
                8,
                "日线未触发强风险",
                f"日线分数 {daily_score}，状态 {daily_status or '--'}，暂未进入 daily-risk，说明中线结构没有被规则直接淘汰。",
                "弱",
                "直接证据",
                "这只是观察条件，不代表日线已经确认强趋势。",
            )
        else:
            add_bear(
                14,
                "日线分数偏弱",
                f"日线分数 {daily_score}，状态 {daily_status or '--'}，说明中线趋势、量价结构或位置风险没有通过规则要求。",
                "中",
                "直接证据",
                "日线弱时，盘中信号需要更严格的量能和失效位确认。",
            )
        if _safe_float(return20) is not None:
            if _safe_float(return20) >= 0:
                add_bull(5, "20 日收益为正", f"20 日涨跌幅 {return20}%，说明近一个月价格表现仍为正收益。", "弱", "辅助证据", "连续大涨后的正收益也可能意味着追高风险，需要结合距 MA20 和量价滞涨判断。")
            else:
                add_bear(7, "20 日收益为负", f"20 日涨跌幅 {return20}%，说明近一个月价格表现偏弱。", "中", "辅助证据", "若后续重新站上 MA20 并放量，才可能从弱势修复转为机会。")
        if _safe_float(distance_ma20) is not None and _safe_float(distance_ma20) < 0:
            add_bear(6, "仍在 MA20 下方", f"距 MA20 {distance_ma20}%，说明价格还没有收复中期均线。", "中", "直接证据", "未站回 MA20 前，中线判断不能转为看多。")
    else:
        add_bear(8, "日 K 数据不足", "没有足够日 K 数据，无法确认 MA20、MA60、20 日涨跌幅和位置风险。", "中", "缺失证据", "数据缺失不等于看空，但会降低结论置信度。")

    valuation = evidence.get("valuation") or {}
    fundamentals = evidence.get("fundamentals") or {}
    pe_ttm = _safe_float(valuation.get("peTtm"))
    pb = _safe_float(valuation.get("pb"))
    revenue_yoy = _safe_float(fundamentals.get("revenueYoY"))
    profit_yoy = _safe_float(fundamentals.get("netProfitYoY"))
    if revenue_yoy is not None and revenue_yoy > 0:
        add_bull(5, "营收同比增长", f"营收同比 {round(revenue_yoy, 2)}%，说明收入端有增长证据。", "弱", "基本面证据", "个股短线能否买仍取决于量价和资金，基本面增长不能直接当盘中买点。")
    if profit_yoy is not None and profit_yoy > 0:
        add_bull(5, "净利润同比增长", f"净利润同比 {round(profit_yoy, 2)}%，说明盈利端有增长证据。", "中", "基本面证据", "需要结合估值水平判断增长是否已经被价格充分反映。")
    if pe_ttm is not None and pe_ttm > 80:
        add_bear(8, "PE 估值偏高", f"PE(TTM) {pe_ttm} 高于 80，估值安全边际不足。", "中", "估值证据", "高估值不等于马上下跌，但会放大利空和回撤风险。")
    if pb is not None and pb > 8:
        add_bear(8, "PB 估值偏高", f"PB {pb} 高于 8，权益估值压力较大。", "中", "估值证据", "高 PB 股票需要更强成长性和资金承接来支撑。")

    money_flow = evidence.get("moneyFlow") or {}
    main_flow = _safe_float(money_flow.get("mainNetInflow"))
    five_day_flow = _safe_float(money_flow.get("fiveDayMainNetInflow"))
    if main_flow is not None:
        if main_flow >= 0:
            add_bull(8, "当日主力净流入", f"当日主力净流入 {round(main_flow / 100000000, 2)} 亿，说明当前资金方向偏承接。", "中", "资金证据", "单日流入可能受大单口径影响，需要结合 5 日资金和价格位置。")
        else:
            add_bear(8, "当日主力净流出", f"当日主力净流出 {round(abs(main_flow) / 100000000, 2)} 亿，说明当前资金方向偏撤离。", "中", "资金证据", "如果价格仍上涨但主力流出，需要警惕拉高出货或分歧加大。")
    if five_day_flow is not None:
        if five_day_flow >= 0:
            add_bull(6, "5 日主力净流入", f"5 日主力净流入 {round(five_day_flow / 100000000, 2)} 亿，说明资金承接不是单日孤立现象。", "中", "资金证据", "仍需观察是否同步推升价格和成交额。")
        else:
            add_bear(6, "5 日主力净流出", f"5 日主力净流出 {round(abs(five_day_flow) / 100000000, 2)} 亿，说明资金趋势偏弱。", "中", "资金证据", "资金连续流出时，即便短线反弹也要降低仓位假设。")

    for item in _negative_news_items(evidence):
        add_bear(5, "新闻/公告风险", f"检索到风险标题：{item}", "中", "事件证据", "事件标题需要进一步打开来源核验，当前只作为风险提示。")

    risks = _risk_list(intraday, daily, evidence)
    for risk in risks[:3]:
        if risk and "暂无强风险" not in risk:
            add_bear(4, "风险标签", risk, "弱", "规则风险", "风险标签用于降低买入置信度，不等同于确定性卖出信号。")

    dimensions = _battle_dimensions(intraday, daily, evidence, market, sector_heat)
    for dimension in dimensions:
        label_text = dimension.get("label", "")
        evidence_rows = dimension.get("evidence", []) or []
        if not evidence_rows:
            continue
        summary = f"{label_text}：{evidence_rows[0]}"
        if dimension.get("bias") == "bullish":
            bullish.append(_battle_reason(f"{label_text}维度偏多", summary, "弱", "维度摘要", "上方维度卡片是汇总证据，不能单独替代交易结论。"))
        elif dimension.get("bias") == "bearish":
            bearish.append(_battle_reason(f"{label_text}维度偏空", summary, "弱", "维度摘要", "上方维度卡片是汇总证据，不能单独替代交易结论。"))

    bullish = _unique_reason_rows(bullish)[:8]
    bearish = _unique_reason_rows(bearish)[:8]
    diff = bull_score - bear_score
    if diff >= 15:
        bias = "bullish"
        label = "多方占优"
        conclusion = "多方证据更强，但仍需按入场条件确认，避免无条件追买。"
    elif diff <= -15:
        bias = "bearish"
        label = "空方占优"
        conclusion = "空方证据更强，当前不适合直接买入，优先等待趋势和资金面修复。"
    else:
        bias = "neutral"
        label = "多空拉锯"
        conclusion = "多空证据接近，当前以观察为主，等待价格、日线和资金流给出一致信号。"
    return {
        "source": "local-rule-fallback",
        "bias": bias,
        "label": label,
        "bullScore": max(0, min(100, bull_score)),
        "bearScore": max(0, min(100, bear_score)),
        "dimensions": dimensions,
        "bullishReasons": bullish or ["暂无足够看多证据"],
        "bearishReasons": bearish or ["暂无明显看空证据"],
        "conclusion": conclusion,
    }


def _battle_dimensions(
    intraday: Dict[str, Any],
    daily: Dict[str, Any],
    evidence: Dict[str, Any],
    market: Dict[str, Any],
    sector_heat: Dict[str, Any],
) -> List[Dict[str, Any]]:
    dimensions = [
        _technical_dimension(intraday),
        _daily_dimension(daily),
        _market_heat_dimension(market),
        _sector_theme_dimension(intraday, evidence, sector_heat),
        _money_flow_dimension(evidence),
        _popularity_dimension(intraday, evidence),
        _fundamental_valuation_dimension(evidence),
        _news_risk_dimension(evidence),
    ]
    return [item for item in dimensions if item.get("evidence")]


def _battle_dimension(key: str, label: str, bull: float, bear: float, evidence: List[str]) -> Dict[str, Any]:
    bull_score = _limited_score(bull)
    bear_score = _limited_score(bear)
    return {
        "key": key,
        "label": label,
        "bias": _dimension_bias(bull_score, bear_score),
        "bullScore": bull_score,
        "bearScore": bear_score,
        "evidence": _unique([item for item in evidence if item])[:5],
    }


def _battle_reason(title: str, detail: str = "", strength: str = "中", evidence_type: str = "直接证据", caveat: str = "") -> Dict[str, str]:
    return {
        "title": str(title or "").strip(),
        "detail": str(detail or "").strip(),
        "strength": str(strength or "中").strip(),
        "evidenceType": str(evidence_type or "直接证据").strip(),
        "caveat": str(caveat or "").strip(),
    }


def _intraday_check_detail(label: str, ok: bool, intraday: Dict[str, Any]) -> str:
    last = intraday.get("last") or {}
    close = _safe_float(last.get("close"))
    last_vwap = _safe_float(intraday.get("lastVwap"))
    ma5 = _last_numeric(intraday.get("ma5"))
    ma10 = _last_numeric(intraday.get("ma10"))
    amount_factor = _safe_float(intraday.get("amountFactor"))
    volume_ratio = _safe_float(intraday.get("volumeRatio"))
    macd_rows = intraday.get("macdSeries") or []
    last_macd = macd_rows[-1] if isinstance(macd_rows, list) and macd_rows else {}
    prev_macd = macd_rows[-2] if isinstance(macd_rows, list) and len(macd_rows) >= 2 else {}
    hist = _safe_float(last_macd.get("hist")) if isinstance(last_macd, dict) else None
    prev_hist = _safe_float(prev_macd.get("hist")) if isinstance(prev_macd, dict) else None

    if label == "价格站上 VWAP":
        if close is None or last_vwap is None:
            return "VWAP 或收盘价缺失，只能按规则结果判断，不能展开价差。"
        distance = (close / last_vwap - 1) * 100 if last_vwap else 0
        verb = "高于" if ok else "低于"
        meaning = "说明日内成交均价附近有承接，买方暂时愿意在均价上方接力" if ok else "说明价格没有站稳日内成交均价，承接不足"
        return f"最新价 {round(close, 2)} {verb} VWAP {round(last_vwap, 2)}，偏离 {round(distance, 2)}%。{meaning}。"
    if label == "MACD 走强":
        if hist is None:
            return "MACD 数据缺失，只能按规则结果判断。"
        compare = "高于" if prev_hist is not None and hist > prev_hist else "未高于"
        return f"15m MACD 柱值 {round(hist, 4)}，{compare}上一根 {round(prev_hist, 4) if prev_hist is not None else '--'}。{'动能正在扩张' if ok else '动能没有形成扩张'}。"
    if label == "均线结构偏多":
        if close is None or ma5 is None or ma10 is None:
            return "MA5/MA10 或最新价缺失，只能按规则结果判断。"
        return f"最新价 {round(close, 2)}，MA5 {round(ma5, 2)}，MA10 {round(ma10, 2)}。{'短均线多头且价格在 MA5 上方' if ok else '短均线或价格位置没有形成多头结构'}。"
    if label == "量能强于样本":
        return f"量比 {round(volume_ratio or 0, 2)}，成交额强度 {round(amount_factor or 0, 2)}x。{'量能可以支撑短线信号' if ok else '量能不足，价格信号容易失真'}。"
    return f"{label}{'通过' if ok else '未通过'}。"


def _intraday_check_caveat(label: str) -> str:
    caveats = {
        "价格站上 VWAP": "VWAP 是日内短线均价证据，只能说明盘中承接；跌回 VWAP 后该理由失效。",
        "MACD 走强": "MACD 是滞后动能指标，单独走强不能排除冲高回落，需要价格和量能同步。",
        "均线结构偏多": "15m 均线偏多属于短周期证据，不能替代日线趋势和风险判断。",
        "量能强于样本": "放量既可能是买盘，也可能是分歧放大，需要结合价格是否站稳关键位。",
    }
    return caveats.get(label, "该规则只是单项证据，需要和其他维度共振。")


def _last_numeric(values: Any) -> float | None:
    if isinstance(values, list) and values:
        return _safe_float(values[-1])
    return _safe_float(values)


def _unique_reason_rows(items: List[Any]) -> List[Any]:
    result: List[Any] = []
    seen = set()
    for item in items:
        normalized: Any
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("label") or "").strip()
            detail = str(item.get("detail") or item.get("reason") or "").strip()
            caveat = str(item.get("caveat") or "").strip()
            if not title and detail:
                title, detail = detail[:28], detail
            if not title:
                continue
            normalized = {
                "title": title,
                "detail": detail,
                "strength": str(item.get("strength") or "中").strip(),
                "evidenceType": str(item.get("evidenceType") or item.get("type") or "直接证据").strip(),
                "caveat": caveat,
            }
            key = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        else:
            text = str(item or "").strip()
            if not text:
                continue
            normalized = text
            key = text
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _dimension_bias(bull: int, bear: int) -> str:
    if bull - bear >= 8:
        return "bullish"
    if bear - bull >= 8:
        return "bearish"
    return "neutral"


def _limited_score(value: float) -> int:
    return max(0, min(100, int(round(value))))


def _technical_dimension(intraday: Dict[str, Any]) -> Dict[str, Any]:
    score = int(intraday.get("score") or 0)
    status = str(intraday.get("status") or "--")
    style = str(intraday.get("style") or "--")
    passed = int(intraday.get("passed") or 0)
    checks = intraday.get("checks") or []
    failed = [str(item.get("label")) for item in checks if not item.get("ok") and item.get("label")]
    bull = max(0, score * 0.45 + passed * 3)
    bear = max(0, (78 - min(score, 78)) * 0.8 + len(failed) * 4)
    evidence_rows = [
        f"15m 分数 {score}，状态 {status}，风格 {style}，规则通过 {passed}/{len(checks) or '--'}。",
    ]
    if failed:
        evidence_rows.append(f"未通过项：{'、'.join(failed[:4])}。")
    return _battle_dimension("technical", "技术面", bull, bear, evidence_rows)


def _daily_dimension(daily: Dict[str, Any]) -> Dict[str, Any]:
    if not daily or not daily.get("available", True):
        return _battle_dimension("daily", "日线趋势", 0, 35, ["日 K 数据不足，中线趋势无法确认。"])
    score = int(daily.get("score") or 0)
    status = str(daily.get("status") or "--")
    trend = daily.get("dailyTrend") or {}
    metrics = daily.get("dailyMetrics") or {}
    return20 = _safe_float(metrics.get("return20d"))
    distance_ma20 = _safe_float(metrics.get("distanceMa20"))
    bull = max(0, score * 0.45)
    bear = max(0, (70 - min(score, 70)) * 0.7)
    if trend.get("aboveMa20"):
        bull += 10
    else:
        bear += 10
    if trend.get("aboveMa60"):
        bull += 10
    else:
        bear += 10
    if return20 is not None and return20 < 0:
        bear += min(16, abs(return20) * 0.7)
    elif return20 is not None:
        bull += min(12, return20 * 0.6)
    if distance_ma20 is not None and distance_ma20 < 0:
        bear += min(12, abs(distance_ma20) * 0.8)
    evidence_rows = [
        f"日线分数 {score}，状态 {status}，MA20/MA60 站上 {bool(trend.get('aboveMa20'))}/{bool(trend.get('aboveMa60'))}。",
        f"20日涨跌幅 {metrics.get('return20d', '--')}%，距 MA20 {metrics.get('distanceMa20', '--')}%。",
    ]
    return _battle_dimension("daily", "日线趋势", bull, bear, evidence_rows)


def _market_heat_dimension(market: Dict[str, Any]) -> Dict[str, Any]:
    breadth = market.get("breadth") or {}
    up = int(breadth.get("up") or 0)
    down = int(breadth.get("down") or 0)
    limit_up = int(market.get("limitUp") or 0)
    limit_down = int(market.get("limitDown") or 0)
    mode = str(market.get("mode") or "--")
    hot = market.get("hotSectors") or []
    context_label = str(market.get("contextLabel") or "当前样本")
    total = max(1, up + down)
    bull = up / total * 40 + min(25, limit_up * 2)
    bear = down / total * 40 + min(25, limit_down * 3)
    if mode == "aggressive":
        bull += 15
    elif mode == "defensive":
        bear += 15
    evidence_rows = [
        f"{context_label}宽度 {up}涨/{down}跌，涨停 {limit_up}，跌停 {limit_down}，模式 {mode}。",
    ]
    if hot:
        evidence_rows.append(f"{context_label}热门方向：{'、'.join(map(str, hot[:5]))}。")
    return _battle_dimension("market_heat", "市场热度", bull, bear, evidence_rows)


def _sector_theme_dimension(intraday: Dict[str, Any], evidence: Dict[str, Any], sector_heat: Dict[str, Any]) -> Dict[str, Any]:
    current = sector_heat.get("current") or {}
    sector_name = str(sector_heat.get("currentSector") or intraday.get("sector") or "--")
    scope_label = str(sector_heat.get("scopeLabel") or "样本")
    sector_rank = int(sector_heat.get("currentRank") or 0)
    avg_change = _safe_float(current.get("avgChange"))
    up = int(current.get("up") or 0)
    down = int(current.get("down") or 0)
    count = int(current.get("count") or 0)
    limit_up = int(current.get("limitUp") or 0)
    fundamentals = evidence.get("fundamentals") or {}
    industry = str(fundamentals.get("industry") or intraday.get("theme") or "--")
    bull = 0
    bear = 0
    if sector_rank:
        bull += max(0, 28 - sector_rank * 5)
        bear += max(0, (sector_rank - 3) * 6)
    if avg_change is not None and avg_change > 0:
        bull += min(20, avg_change * 4)
    elif avg_change is not None and avg_change < 0:
        bear += min(20, abs(avg_change) * 4)
    bull += min(15, limit_up * 4)
    if down > up:
        bear += 10
    evidence_rows = [
        f"{scope_label}板块 {sector_name} 排名第 {sector_rank or '--'}，均涨跌 {avg_change if avg_change is not None else '--'}%，{up}涨/{down}跌，涨停 {limit_up}。",
        f"行业/主题线索：{industry}。",
    ]
    return _battle_dimension("sector_theme", "板块/风口", bull, bear, evidence_rows)


def _money_flow_dimension(evidence: Dict[str, Any]) -> Dict[str, Any]:
    money_flow = evidence.get("moneyFlow") or {}
    main_flow = _safe_float(money_flow.get("mainNetInflow"))
    main_ratio = _safe_float(money_flow.get("mainNetInflowRatio"))
    five_day_flow = _safe_float(money_flow.get("fiveDayMainNetInflow"))
    bull = 0
    bear = 0
    evidence_rows = []
    if main_flow is not None:
        amount_text = round(abs(main_flow) / 100000000, 2)
        if main_flow >= 0:
            bull += min(35, 12 + amount_text * 4)
            evidence_rows.append(f"当日主力净流入 {amount_text} 亿，主力净占比 {main_ratio if main_ratio is not None else '--'}%。")
        else:
            bear += min(35, 12 + amount_text * 4)
            evidence_rows.append(f"当日主力净流出 {amount_text} 亿，主力净占比 {main_ratio if main_ratio is not None else '--'}%。")
    if five_day_flow is not None:
        amount_text = round(abs(five_day_flow) / 100000000, 2)
        if five_day_flow >= 0:
            bull += min(25, 8 + amount_text)
            evidence_rows.append(f"5日主力净流入 {amount_text} 亿。")
        else:
            bear += min(25, 8 + amount_text)
            evidence_rows.append(f"5日主力净流出 {amount_text} 亿。")
    if not evidence_rows:
        evidence_rows.append("资金流数据缺失，无法确认主力方向。")
        bear += 8
    return _battle_dimension("money_flow", "资金流", bull, bear, evidence_rows)


def _popularity_dimension(intraday: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    turnover = _safe_float(intraday.get("turnover")) or 0
    volume_ratio = _safe_float(intraday.get("volumeRatio")) or 0
    amount_factor = _safe_float(intraday.get("amountFactor")) or 0
    rel_strength = _safe_float(intraday.get("relStrength")) or 0
    news_count = len(evidence.get("news") or [])
    holder = _retail_holder_item(str(intraday.get("code") or ""))
    bull = min(25, volume_ratio * 5) + min(20, amount_factor * 5) + min(20, rel_strength / 5)
    bear = 0
    if turnover >= 12:
        bear += 12
    if volume_ratio < 1.2 and amount_factor < 1.2:
        bear += 12
    if news_count:
        bull += min(12, news_count * 3)
    evidence_rows = [
        f"换手 {round(turnover, 2)}%，量比 {round(volume_ratio, 2)}，成交额强度 {round(amount_factor, 2)}x，相对强度 {round(rel_strength, 2)}。",
        f"近端新闻 {news_count} 条，公告 {len(evidence.get('announcements') or [])} 条。",
    ]
    if holder:
        rank = holder.get("rank") or "--"
        holder_count = holder.get("holderCount") or "--"
        evidence_rows.append(f"散户榜缓存：股东户数 {holder_count}，排名 {rank}；该数据代表披露口径的人气/拥挤度近似。")
        if _safe_float(rank) is not None and _safe_float(rank) <= 100:
            bull += 4
            bear += 4
    return _battle_dimension("popularity", "人气/活跃度", bull, bear, evidence_rows)


def _fundamental_valuation_dimension(evidence: Dict[str, Any]) -> Dict[str, Any]:
    fundamentals = evidence.get("fundamentals") or {}
    valuation = evidence.get("valuation") or {}
    revenue_yoy = _safe_float(fundamentals.get("revenueYoY"))
    profit_yoy = _safe_float(fundamentals.get("netProfitYoY"))
    roe = _safe_float(fundamentals.get("roe"))
    pe_ttm = _safe_float(valuation.get("peTtm"))
    pb = _safe_float(valuation.get("pb"))
    bull = 0
    bear = 0
    if revenue_yoy is not None and revenue_yoy > 0:
        bull += min(18, revenue_yoy * 2)
    elif revenue_yoy is not None:
        bear += min(18, abs(revenue_yoy) * 2)
    if profit_yoy is not None and profit_yoy > 0:
        bull += min(18, profit_yoy * 2)
    elif profit_yoy is not None:
        bear += min(18, abs(profit_yoy) * 2)
    if roe is not None and roe > 8:
        bull += 10
    if pe_ttm is not None and pe_ttm > 80:
        bear += 16
    elif pe_ttm is not None and 0 < pe_ttm <= 35:
        bull += 8
    if pb is not None and pb > 8:
        bear += 16
    elif pb is not None and 0 < pb <= 3:
        bull += 6
    evidence_rows = [
        f"行业 {fundamentals.get('industry') or '--'}，营收同比 {revenue_yoy if revenue_yoy is not None else '--'}%，净利润同比 {profit_yoy if profit_yoy is not None else '--'}%，ROE {roe if roe is not None else '--'}%。",
        f"PE(TTM) {pe_ttm if pe_ttm is not None else '--'}，PB {pb if pb is not None else '--'}。",
    ]
    return _battle_dimension("fundamental_valuation", "基本面/估值", bull, bear, evidence_rows)


def _news_risk_dimension(evidence: Dict[str, Any]) -> Dict[str, Any]:
    news = evidence.get("news") or []
    announcements = evidence.get("announcements") or []
    negative = _negative_news_items(evidence)
    bull = min(20, len(news) * 3 + len(announcements))
    bear = len(negative) * 12
    evidence_rows = [f"新闻 {len(news)} 条，公告 {len(announcements)} 条。"]
    if news:
        evidence_rows.append(f"最新新闻：{news[0].get('title', '--')}")
    if negative:
        evidence_rows.append(f"风险标题：{'；'.join(negative[:2])}")
    return _battle_dimension("news_risk", "新闻公告", bull, bear, evidence_rows)


def _short_horizon(intraday: Dict[str, Any], daily: Dict[str, Any], market: Dict[str, Any], sector_heat: Dict[str, Any]) -> Dict[str, str]:
    score = int(intraday.get("score") or 0)
    status = intraday.get("status", "")
    sector_rank = int(sector_heat.get("currentRank") or 9)
    market_mode = market.get("mode", "balanced")
    daily_bad = daily and daily.get("available", True) and daily.get("status") == "daily-risk"
    if score >= 78 and status in {"buy-triggered", "pullback"} and sector_rank <= 3 and market_mode != "defensive" and not daily_bad:
        stance = "buy"
        label = "短线可小仓"
    elif score >= 65 and not daily_bad:
        stance = "watch"
        label = "短线等确认"
    else:
        stance = "avoid"
        label = "短线不买"
    return {
        "stance": stance,
        "label": label,
        "reason": f"15m 分数 {score}，状态 {status or '--'}，板块样本排名第 {sector_rank}，市场 {market_mode}。",
        "entry": intraday.get("buyTrigger", "等待 15m 量价确认"),
        "invalid": intraday.get("invalidCondition", "跌破 VWAP 或早盘低点"),
    }


def _mid_horizon(daily: Dict[str, Any], sector_heat: Dict[str, Any]) -> Dict[str, str]:
    if not daily or not daily.get("available", True):
        return {"stance": "watch", "label": "中线缺日K", "reason": "日 K 数据不足，无法确认中线趋势。", "entry": "补齐日 K 后再判断", "invalid": "日 K 缺失"}
    score = int(daily.get("score") or 0)
    status = daily.get("status", "")
    trend = daily.get("dailyTrend") or {}
    if score >= 70 and status in {"daily-confirmed", "daily-watch", "daily-candidate"} and trend.get("aboveMa20") and trend.get("aboveMa60"):
        stance = "buy"
        label = "中线可跟踪"
    elif score >= 55 and status != "daily-risk":
        stance = "watch"
        label = "中线观察"
    else:
        stance = "avoid"
        label = "中线不买"
    return {
        "stance": stance,
        "label": label,
        "reason": f"日线分数 {score}，状态 {status or '--'}，MA20/MA60 站上情况 {bool(trend.get('aboveMa20'))}/{bool(trend.get('aboveMa60'))}。",
        "entry": daily.get("buyTrigger", "等待回踩 MA20 或突破后缩量确认"),
        "invalid": daily.get("invalidCondition", "跌破 MA20 或 MA60"),
    }


def _long_horizon(daily: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, str]:
    fundamentals = evidence.get("fundamentals") or {}
    valuation = evidence.get("valuation") or {}
    money_flow = evidence.get("moneyFlow") or {}
    missing = set(evidence.get("missingData") or [])
    pe_ttm = _safe_float(valuation.get("peTtm"))
    pb = _safe_float(valuation.get("pb"))
    has_basic = bool(fundamentals) and "基本面未接入" not in missing
    has_valuation = bool(valuation) and "估值未接入" not in missing
    daily_ok = bool(daily and daily.get("available", True) and daily.get("dailyTrend", {}).get("longTrendOk"))
    valuation_ok = (pe_ttm is None or 0 < pe_ttm <= 80) and (pb is None or 0 < pb <= 8)
    flow_ok = _safe_float(money_flow.get("mainNetInflow")) is None or _safe_float(money_flow.get("mainNetInflow")) >= 0
    if has_basic and has_valuation and daily_ok and valuation_ok and flow_ok:
        stance = "buy"
        label = "长线可跟踪"
    elif has_basic or daily_ok:
        stance = "watch"
        label = "长线证据不足"
    else:
        stance = "avoid"
        label = "长线不判断"
    return {
        "stance": stance,
        "label": label,
        "reason": f"基本面 {'有' if has_basic else '缺'}，估值 {'有' if has_valuation else '缺'}，PE(TTM) {pe_ttm if pe_ttm is not None else '--'}，PB {pb if pb is not None else '--'}，长期趋势 {'可用' if daily_ok else '未确认'}。",
        "entry": "长线不以盘中追买为入口，等待估值和趋势证据同步确认",
        "invalid": "基本面/估值/资金流证据缺失或日线长期趋势转弱",
    }


def _risk_list(intraday: Dict[str, Any], daily: Dict[str, Any], evidence: Dict[str, Any]) -> List[str]:
    risks = []
    risks.extend(intraday.get("riskFlags") or [])
    if daily and daily.get("riskFlags"):
        risks.extend(daily.get("riskFlags") or [])
    risks.extend(evidence.get("warnings") or [])
    if evidence.get("missingData"):
        risks.append(f"缺失证据：{'、'.join(evidence.get('missingData') or [])}")
    return _unique(risks) or ["暂无强风险标签，但仍需用价格失效条件控制风险"]


def _negative_news_items(evidence: Dict[str, Any]) -> List[str]:
    keywords = ("减持", "处罚", "立案", "问询", "诉讼", "亏损", "下滑", "风险", "退市", "冻结", "质押", "套现")
    rows = []
    for item in list(evidence.get("news") or []) + list(evidence.get("announcements") or []):
        title = str(item.get("title") or "").strip()
        if title and any(keyword in title for keyword in keywords):
            rows.append(title)
    return _unique(rows)[:3]


def _retail_holder_item(code: str) -> Dict[str, Any]:
    normalized = _normalize_code(code)
    if not normalized:
        return {}
    try:
        with RETAIL_HOLDER_CACHE_PATH.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return {}
    for item in items:
        if isinstance(item, dict) and _normalize_code(item.get("code")) == normalized:
            return item
    return {}


def _rule_confidence(short: Dict[str, str], mid: Dict[str, str], long: Dict[str, str], missing: List[str]) -> int:
    base = 62
    base += 8 if short["stance"] == "buy" else -6 if short["stance"] == "avoid" else 0
    base += 6 if mid["stance"] == "buy" else -4 if mid["stance"] == "avoid" else 0
    base += 4 if long["stance"] == "buy" else 0
    base -= min(20, len(missing) * 4)
    return max(30, min(88, base))


def _normalize_ai_decision(ai_decision: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(ai_decision, dict) or not ai_decision:
        return _empty_ai_decision()
    result = {key: value for key, value in ai_decision.items() if _present(value)}
    decision = result.get("decision")
    if decision not in {"buy", "watch", "avoid"}:
        result["decision"] = "watch"
    primary = result.get("primaryHorizon")
    if primary not in {"short", "mid", "long", "none"}:
        result["primaryHorizon"] = "none"
    result["decisionText"] = str(result.get("decisionText") or _ai_decision_text(result["decision"]))
    result["confidence"] = max(0, min(100, int(_safe_float(result.get("confidence")) or 50)))
    horizons = result.get("horizons") if isinstance(result.get("horizons"), dict) else {}
    normalized_horizons = {}
    for key in ("short", "mid", "long"):
        item = horizons.get(key) if isinstance(horizons.get(key), dict) else {}
        merged = _empty_ai_horizon(key)
        merged.update({field: item.get(field) for field in ("stance", "label", "reason", "entry", "invalid") if item.get(field)})
        if merged.get("stance") not in {"buy", "watch", "avoid"}:
            merged["stance"] = "watch"
        merged = _normalize_horizon_action_text(merged)
        normalized_horizons[key] = merged
    result["horizons"] = normalized_horizons
    raw_trade_plan = result.get("tradePlan")
    if not isinstance(raw_trade_plan, dict) and isinstance(result.get("newPositionPlan"), dict):
        raw_trade_plan = result.get("newPositionPlan")
    result["tradePlan"] = _normalize_trade_plan(raw_trade_plan, result["decision"], normalized_horizons)
    if result["decision"] == "buy" and not _trade_plan_has_buy_prices(result["tradePlan"]):
        result["decision"] = "watch"
        result["decisionText"] = "AI 给出买入倾向但缺少明确买入区间或止损价，已降级为观察"
        result["primaryHorizon"] = result["tradePlan"].get("horizon") if result["tradePlan"].get("horizon") in {"short", "mid", "long"} else "short"
        result["tradePlan"]["actionable"] = False
        result["tradePlan"]["reason"] = _append_text(result["tradePlan"].get("reason"), "缺少有效买入区间或止损价，不能作为买入信号。")
        warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
        result["warnings"] = _append_unique(warnings, "AI 买入计划缺少明确价格，已降级为观察。")
    result["battle"] = _normalize_battle(result.get("battle"), result.get("primaryHorizon"))
    result["userExplanation"] = _normalize_user_explanation(result.get("userExplanation"))
    for key in ("summary", "supportingEvidence", "risks", "missingData", "warnings"):
        value = result.get(key)
        if not isinstance(value, list):
            result[key] = []
    return result


def _empty_ai_decision() -> Dict[str, Any]:
    return {
        "decision": "watch",
        "decisionText": "等待更多证据确认",
        "primaryHorizon": "none",
        "confidence": 50,
        "horizons": {key: _empty_ai_horizon(key) for key in ("short", "mid", "long")},
        "tradePlan": _empty_trade_plan(),
        "battle": _empty_ai_battle(),
        "summary": [],
        "supportingEvidence": [],
        "risks": [],
        "missingData": [],
        "warnings": [],
        "userExplanation": _empty_user_explanation(),
    }


def _empty_user_explanation() -> Dict[str, Any]:
    return {
        "plainDecision": "",
        "why": [],
        "considerCondition": {"text": "", "terms": []},
        "avoidCondition": {"text": "", "terms": []},
    }


def _normalize_user_explanation(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return _empty_user_explanation()
    result = _empty_user_explanation()
    result["plainDecision"] = str(value.get("plainDecision") or value.get("decision") or "").strip()
    why = value.get("why")
    if not isinstance(why, list):
        why = value.get("reasons") if isinstance(value.get("reasons"), list) else []
    result["why"] = [str(item).strip() for item in why if str(item or "").strip()][:4]
    result["considerCondition"] = _normalize_user_explanation_condition(
        value.get("considerCondition") or value.get("whenToConsider") or value.get("entryCondition")
    )
    result["avoidCondition"] = _normalize_user_explanation_condition(
        value.get("avoidCondition") or value.get("whenToAvoid") or value.get("invalidCondition")
    )
    return result


def _normalize_user_explanation_condition(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        return {"text": value.strip(), "terms": []}
    if not isinstance(value, dict):
        return {"text": "", "terms": []}
    terms = value.get("terms")
    if not isinstance(terms, list):
        terms = []
    return {
        "text": str(value.get("text") or value.get("condition") or "").strip(),
        "terms": [_normalize_user_explanation_term(item) for item in terms if isinstance(item, dict)][:8],
    }


def _normalize_user_explanation_term(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "term": str(value.get("term") or value.get("name") or "").strip(),
        "value": _safe_float(value.get("value")) if value.get("value") is not None else None,
        "meaning": str(value.get("meaning") or "").strip(),
        "whyItMatters": str(value.get("whyItMatters") or value.get("effect") or value.get("reason") or "").strip(),
    }


def _empty_case_memory_status() -> Dict[str, Any]:
    return {
        "status": "disabled",
        "mode": "off",
        "effectiveSampleCount": 0,
        "minSamples": 800,
        "matchedCaseCount": 0,
        "bestSimilarity": None,
        "injected": False,
        "eligible": False,
        "similarCases": [],
        "reviewMemoryStatus": {"activeLessonCount": 0, "injected": False, "lessons": []},
    }


def _empty_ai_horizon(key: str) -> Dict[str, str]:
    labels = {"short": "短线待确认", "mid": "中线待确认", "long": "长线待确认"}
    return {
        "stance": "watch",
        "label": labels.get(key, "待确认"),
        "reason": "DeepSeek 未返回该周期的完整判断。",
        "entry": "等待证据进一步确认",
        "invalid": "证据不足时不形成买入判断",
    }


def _normalize_horizon_action_text(item: Dict[str, Any]) -> Dict[str, Any]:
    stance = item.get("stance")
    entry = str(item.get("entry") or "").strip()
    invalid = str(item.get("invalid") or "").strip()
    if stance == "watch":
        if _looks_like_price_range_only(entry):
            item["entry"] = f"等待价格重新确认该区间有效后再评估，不能按 {entry} 直接买入。"
        if invalid and invalid.startswith("无法"):
            item["invalid"] = invalid
    elif stance == "avoid":
        if _looks_like_price_range_only(entry):
            item["entry"] = f"风险解除并重新站上关键位后再评估，不能按 {entry} 直接买入。"
        if not invalid:
            item["invalid"] = "风险未解除前继续回避"
    return item


def _looks_like_price_range_only(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(re.fullmatch(r"\d+(?:\.\d+)?\s*[-~到至]\s*\d+(?:\.\d+)?", text))


def _empty_trade_plan() -> Dict[str, Any]:
    return {
        "actionable": False,
        "horizon": "none",
        "buyPriceLow": None,
        "buyPriceHigh": None,
        "stopLossPrice": None,
        "takeProfitPrice": None,
        "sellCondition": "",
        "positionSuggestion": "",
        "riskReward": "",
        "reason": "当前没有可执行买入计划。",
    }


def _empty_new_position_plan() -> Dict[str, Any]:
    plan = _empty_trade_plan()
    plan.update(
        {
            "planVersion": "new_position_plan_v2",
            "planSource": "empty",
            "positionType": "new_position",
            "canSellToday": False,
            "minHoldingDays": 1,
            "entryEvidence": [],
            "nextDayStopPlan": "",
            "invalidCondition": "",
        }
    )
    return plan


def _empty_existing_position_plan() -> Dict[str, Any]:
    return {
        "planVersion": "existing_position_plan_v2",
        "planSource": "empty",
        "positionType": "existing_position",
        "action": "watch",
        "canSellToday": True,
        "intradayAction": "",
        "stopLossPrice": None,
        "takeProfitPrice": None,
        "sellCondition": "",
        "positionSuggestion": "",
        "reason": "当前未录入已有仓位，不能把持仓处理计划当成新开仓买点。",
        "riskLevel": "medium",
    }


def _normalize_v2_position_plans(decision: Dict[str, Any], position_context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    result = dict(decision or {})
    normalized_context = _normalize_position_context(position_context)
    if result.get("decision") not in {"buy", "watch", "avoid"}:
        result["decision"] = "watch"
    horizons = result.get("horizons") if isinstance(result.get("horizons"), dict) else {}
    raw_trade_plan = result.get("tradePlan")
    raw_new_plan = result.get("newPositionPlan")
    trade_source = raw_trade_plan if isinstance(raw_trade_plan, dict) else raw_new_plan
    trade_plan = _normalize_trade_plan(trade_source, result["decision"], horizons)

    new_plan_source = raw_new_plan if isinstance(raw_new_plan, dict) else trade_plan
    source_label = "newPositionPlan" if isinstance(raw_new_plan, dict) else "legacy_tradePlan"
    new_plan = _normalize_new_position_plan(new_plan_source, result["decision"], horizons, source_label)
    raw_existing_plan = result.get("existingPositionPlan")
    existing_plan = _normalize_existing_position_plan(raw_existing_plan)

    position_state = str(normalized_context.get("positionState") or "none").strip().lower()
    position_info = normalized_context.get("positionInfo") if isinstance(normalized_context.get("positionInfo"), dict) else {}
    if position_state == "existing":
        existing_plan.update(
            {
                "planSource": "existingPositionPlan" if isinstance(raw_existing_plan, dict) else "empty_existing_position_plan",
                "positionCostPrice": position_info.get("costPrice"),
                "positionPct": position_info.get("positionPct"),
                "holdingDays": position_info.get("holdingDays"),
                "plannedHorizon": position_info.get("plannedHorizon", ""),
            }
        )
        if not isinstance(raw_existing_plan, dict):
            existing_plan["reason"] = "已录入本次持仓信息，但 AI 未返回已有仓位处理计划，不能视为明确持有、减仓或卖出建议。"
    else:
        existing_plan = _empty_existing_position_plan()

    result["positionState"] = position_state
    result["positionContext"] = normalized_context
    result["newPositionPlan"] = new_plan
    result["existingPositionPlan"] = existing_plan
    result["tradePlan"] = _trade_plan_from_new_position_plan(new_plan)
    return result


def _normalize_new_position_plan(value: Any, decision: str, horizons: Dict[str, Dict[str, Any]], source_label: str) -> Dict[str, Any]:
    base = _empty_new_position_plan()
    plan = _normalize_trade_plan(value, decision, horizons)
    base.update(plan)
    if isinstance(value, dict):
        base.update(
            {
                "canSellToday": _safe_bool(value.get("canSellToday"), default=False),
                "minHoldingDays": max(1, int(_safe_float(value.get("minHoldingDays")) or 1)),
                "entryEvidence": _normalize_text_array(value.get("entryEvidence")),
                "nextDayStopPlan": str(value.get("nextDayStopPlan") or "").strip(),
                "invalidCondition": str(value.get("invalidCondition") or value.get("invalid") or "").strip(),
            }
        )
    base["planVersion"] = "new_position_plan_v2"
    base["planSource"] = source_label
    base["positionType"] = "new_position"
    if not base.get("nextDayStopPlan") and base.get("sellCondition"):
        base["nextDayStopPlan"] = str(base.get("sellCondition") or "")
    if not base.get("invalidCondition") and base.get("stopLossPrice"):
        base["invalidCondition"] = f"T+1 后跌破止损价 {base['stopLossPrice']}，新开仓计划失效。"
    return base


def _normalize_existing_position_plan(value: Any) -> Dict[str, Any]:
    base = _empty_existing_position_plan()
    if not isinstance(value, dict):
        return base
    action = str(value.get("action") or value.get("stance") or base["action"]).strip()
    if action not in {"hold", "watch", "reduce", "sell", "stop_loss", "take_profit"}:
        action = "watch"
    risk_level = str(value.get("riskLevel") or value.get("risk") or base["riskLevel"]).strip()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "medium"
    base.update(
        {
            "planSource": "existingPositionPlan",
            "action": action,
            "canSellToday": _safe_bool(value.get("canSellToday"), default=True),
            "intradayAction": str(value.get("intradayAction") or value.get("actionDetail") or "").strip(),
            "stopLossPrice": _safe_float(value.get("stopLossPrice")),
            "takeProfitPrice": _safe_float(value.get("takeProfitPrice")),
            "sellCondition": str(value.get("sellCondition") or value.get("exitCondition") or "").strip(),
            "positionSuggestion": str(value.get("positionSuggestion") or value.get("position") or "").strip(),
            "reason": str(value.get("reason") or base["reason"]).strip(),
            "riskLevel": risk_level,
        }
    )
    return base


def _normalize_text_array(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _trade_plan_from_new_position_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    trade_plan = _empty_trade_plan()
    trade_plan.update(
        {
            "actionable": _safe_bool(plan.get("actionable")),
            "horizon": plan.get("horizon") or "none",
            "buyPriceLow": _safe_float(plan.get("buyPriceLow")),
            "buyPriceHigh": _safe_float(plan.get("buyPriceHigh")),
            "stopLossPrice": _safe_float(plan.get("stopLossPrice")),
            "takeProfitPrice": _safe_float(plan.get("takeProfitPrice")),
            "sellCondition": str(plan.get("sellCondition") or "").strip(),
            "positionSuggestion": str(plan.get("positionSuggestion") or "").strip(),
            "riskReward": str(plan.get("riskReward") or "").strip(),
            "reason": str(plan.get("reason") or "").strip(),
        }
    )
    return trade_plan


def _normalize_trade_plan(value: Any, decision: str, horizons: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    plan = _empty_trade_plan()
    if isinstance(value, dict):
        plan.update(
            {
                "actionable": _safe_bool(value.get("actionable")),
                "horizon": str(value.get("horizon") or value.get("primaryHorizon") or plan["horizon"]),
                "buyPriceLow": _safe_float(value.get("buyPriceLow")),
                "buyPriceHigh": _safe_float(value.get("buyPriceHigh")),
                "stopLossPrice": _safe_float(value.get("stopLossPrice")),
                "takeProfitPrice": _safe_float(value.get("takeProfitPrice")),
                "sellCondition": str(value.get("sellCondition") or value.get("exitCondition") or "").strip(),
                "positionSuggestion": str(value.get("positionSuggestion") or value.get("position") or "").strip(),
                "riskReward": str(value.get("riskReward") or "").strip(),
                "reason": str(value.get("reason") or "").strip(),
            }
        )
    if plan["horizon"] not in {"short", "mid", "long", "none"}:
        plan["horizon"] = "none"
    low = plan.get("buyPriceLow")
    high = plan.get("buyPriceHigh")
    if low is not None and high is not None and low > high:
        plan["buyPriceLow"], plan["buyPriceHigh"] = high, low
    has_buy_horizon = any((item or {}).get("stance") == "buy" for item in horizons.values())
    plan["actionable"] = bool(plan["actionable"] and decision == "buy" and has_buy_horizon and _trade_plan_has_buy_prices(plan))
    if not plan["actionable"] and not plan["reason"]:
        plan["reason"] = "当前没有同时满足买入区间、止损价和周期买入结论。"
    return plan


def _trade_plan_has_buy_prices(plan: Dict[str, Any]) -> bool:
    low = _safe_float(plan.get("buyPriceLow"))
    high = _safe_float(plan.get("buyPriceHigh"))
    stop = _safe_float(plan.get("stopLossPrice"))
    return bool(low and high and stop and low > 0 and high > 0 and stop > 0)


def _apply_v2_trade_plan_validation(decision: Dict[str, Any], evidence_pack: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(decision or {})
    validation = _validate_v2_trade_plan(result, evidence_pack)
    result["tradePlanValidation"] = validation
    if result.get("decision") != "buy" or validation["grade"] == "pass":
        return result

    original_horizon = str(result.get("primaryHorizon") or "short")
    result["decision"] = "watch"
    result["decisionText"] = "交易计划未通过后端校验，已降级为观察"
    result["primaryHorizon"] = original_horizon if original_horizon in {"short", "mid", "long"} else "short"
    trade_plan = dict(result.get("tradePlan") or {})
    trade_plan["actionable"] = False
    trade_plan["reason"] = _append_text(
        trade_plan.get("reason"),
        validation["downgradeReason"] or "V2 交易计划未通过后端校验，不能作为买入信号。",
    )
    result["tradePlan"] = trade_plan
    new_plan = dict(result.get("newPositionPlan") or {})
    if new_plan:
        new_plan["actionable"] = False
        new_plan["reason"] = _append_text(
            new_plan.get("reason"),
            validation["downgradeReason"] or "V2 新开仓计划未通过后端校验，不能作为买入信号。",
        )
        result["newPositionPlan"] = new_plan
    result["warnings"] = _append_unique(
        result.get("warnings") if isinstance(result.get("warnings"), list) else [],
        f"V2 后端交易计划校验未通过，已从 buy 降级为 watch：{validation['downgradeReason'] or '存在硬风险或计划结构问题'}",
    )
    horizons = result.get("horizons") if isinstance(result.get("horizons"), dict) else {}
    for key, item in horizons.items():
        if not isinstance(item, dict) or item.get("stance") != "buy":
            continue
        item["stance"] = "watch"
        item["label"] = "后端校验后观察"
        item["entry"] = "等待交易计划通过后再评估，当前不能直接买入"
        item["invalid"] = validation["downgradeReason"] or "交易计划校验未通过"
    validation["finalDecision"] = "watch"
    return result


def _validate_v2_trade_plan(decision: Dict[str, Any], evidence_pack: Dict[str, Any]) -> Dict[str, Any]:
    original_decision = str(decision.get("decision") or "watch")
    base = {
        "version": "trade_plan_v2",
        "originalDecision": original_decision,
        "finalDecision": original_decision,
        "grade": "not_applicable",
        "valid": original_decision != "buy",
        "actionable": False,
        "hardBlocks": [],
        "warnings": [],
        "checks": [],
        "executionConstraints": {
            "positionType": "new_position",
            "canSellToday": False,
            "minHoldingDays": 1,
            "note": "默认按新开仓评估，A 股新开仓当日不能卖出。",
        },
        "downgradeReason": "",
    }
    if original_decision != "buy":
        return base

    plan_source = "newPositionPlan" if isinstance(decision.get("newPositionPlan"), dict) else "tradePlan"
    plan = decision.get("newPositionPlan") if isinstance(decision.get("newPositionPlan"), dict) else {}
    if not plan:
        plan = decision.get("tradePlan") if isinstance(decision.get("tradePlan"), dict) else {}
    snapshot = evidence_pack.get("snapshot") if isinstance(evidence_pack.get("snapshot"), dict) else {}
    daily = evidence_pack.get("daily") if isinstance(evidence_pack.get("daily"), dict) else {}
    market = evidence_pack.get("market") if isinstance(evidence_pack.get("market"), dict) else {}
    hard_blocks: List[str] = []
    warnings: List[str] = []
    checks: List[Dict[str, Any]] = []

    def check(name: str, ok: bool, reason: str, hard: bool = False) -> None:
        checks.append({"name": name, "ok": bool(ok), "hard": hard, "reason": reason})
        if ok:
            return
        if hard:
            hard_blocks.append(reason)
        else:
            warnings.append(reason)

    buy_low = _safe_float(plan.get("buyPriceLow"))
    buy_high = _safe_float(plan.get("buyPriceHigh"))
    stop_loss = _safe_float(plan.get("stopLossPrice"))
    take_profit = _safe_float(plan.get("takeProfitPrice"))
    current_price = _safe_float(snapshot.get("price"))
    daily_status = str(daily.get("status") or "")
    limit_status = str(snapshot.get("limitStatus") or "")
    missing_data = {str(item) for item in evidence_pack.get("missingData") or []}
    sell_condition = str(plan.get("sellCondition") or "").strip()
    min_holding_days = int(_safe_float(plan.get("minHoldingDays")) or 0)

    check(
        "actionable",
        _safe_bool(plan.get("actionable")),
        f"{plan_source}.actionable=false，AI 新开仓计划没有声明为可执行。",
        hard=True,
    )
    check(
        "position_type",
        str(plan.get("positionType") or "new_position") == "new_position",
        "买入校验必须使用新开仓计划，不能用已有仓位处理计划替代。",
        hard=True,
    )
    check(
        "can_sell_today",
        not _safe_bool(plan.get("canSellToday"), default=False),
        "newPositionPlan.canSellToday 必须为 false，新开仓不能安排当日卖出。",
        hard=True,
    )
    check(
        "min_holding_days",
        min_holding_days >= 1,
        "newPositionPlan.minHoldingDays 必须 >= 1，符合 A 股 T+1。",
        hard=True,
    )
    check(
        "horizon",
        str(plan.get("horizon") or "") in {"short", "mid", "long"},
        "交易计划没有有效的 short/mid/long 周期。",
        hard=True,
    )
    check(
        "buy_range",
        bool(buy_low and buy_high and buy_low > 0 and buy_high > 0 and buy_low <= buy_high),
        "买入区间缺失或上下沿不合法。",
        hard=True,
    )
    check(
        "stop_loss",
        bool(stop_loss and stop_loss > 0 and buy_low and stop_loss < buy_low),
        "止损价必须低于买入区间下沿。",
        hard=True,
    )
    if take_profit is not None:
        check(
            "take_profit",
            bool(buy_high and take_profit > buy_high),
            "止盈价存在时必须高于买入区间上沿。",
            hard=True,
        )
    else:
        warnings.append("未提供数字型止盈价，后续只能依赖卖出条件退出。")

    if buy_low and buy_high and stop_loss and take_profit and stop_loss < buy_low:
        midpoint = (buy_low + buy_high) / 2
        risk = midpoint - stop_loss
        reward = take_profit - midpoint
        ratio = reward / risk if risk > 0 else 0
        check(
            "risk_reward",
            ratio >= 1.0,
            f"按买入区间中值计算风险收益比约 {ratio:.2f}，低于 1.0。",
            hard=True,
        )
    elif buy_low and buy_high and stop_loss:
        warnings.append("缺少止盈价，无法完整计算风险收益比。")

    check(
        "sell_condition",
        bool(sell_condition),
        "缺少卖出条件，交易计划无法定义退出规则。",
        hard=True,
    )
    new_position_exit_text = "\n".join(
        str(plan.get(key) or "") for key in ("sellCondition", "nextDayStopPlan", "invalidCondition", "reason")
    )
    if _contains_same_day_sell_text(new_position_exit_text):
        hard_blocks.append("新开仓交易计划包含当日卖出或盘中止损描述，不符合 A 股 T+1。")

    check(
        "limit_status",
        limit_status not in {"one_word_limit_up", "limit_down"},
        "当前股票处于一字涨停或跌停，不能作为正常新开仓买入。",
        hard=True,
    )
    if limit_status == "limit_up":
        warnings.append("股票接近或处于涨停，实际买入可能排队或无法成交。")

    check(
        "daily_risk",
        daily_status != "daily-risk",
        "日线状态为 daily-risk，短线买入也不能忽略日线破位风险。",
        hard=True,
    )
    primary_horizon = str(decision.get("primaryHorizon") or plan.get("horizon") or "short")
    if primary_horizon in {"mid", "long"}:
        check(
            "daily_available",
            bool(daily.get("available", True)),
            "中线或长线计划缺少可用日 K，不能确认对应周期趋势。",
            hard=True,
        )
    if primary_horizon == "long":
        check(
            "long_evidence",
            not ({"基本面未接入", "估值未接入"} & missing_data),
            "长线买入缺少基本面或估值关键证据。",
            hard=True,
        )

    if bool(market.get("contextStale")):
        hard_blocks.append("市场上下文缓存已过期，不能把当前结果当成正常买入信号。")
    elif market.get("contextWarning"):
        warnings.append(f"市场上下文存在警告：{market.get('contextWarning')}")

    if current_price and buy_low and buy_high:
        if buy_low > current_price * 1.12 or buy_high < current_price * 0.88:
            warnings.append("买入区间明显偏离当前价，需重新确认价格触发条件。")

    hard_blocks = _unique_texts(hard_blocks)
    warnings = _unique_texts(warnings)
    grade = "pass" if not hard_blocks else "downgraded"
    base.update(
        {
            "grade": grade,
            "valid": not hard_blocks,
            "actionable": not hard_blocks and _safe_bool(plan.get("actionable")),
            "hardBlocks": hard_blocks,
            "warnings": warnings,
            "checks": checks,
            "planSource": plan_source,
            "downgradeReason": "；".join(hard_blocks[:3]),
        }
    )
    if warnings:
        base["warningSummary"] = "；".join(warnings[:3])
    return base


def _contains_same_day_sell_text(value: str) -> bool:
    text = str(value or "")
    if not text:
        return False
    risky_patterns = (
        r"买入后\s*(?:15|30|60)\s*分钟",
        r"15\s*-\s*30\s*分钟",
        r"盘中止损",
        r"日内(?:卖出|止损|减仓|清仓|做T|做 T)",
        r"T\+0",
        r"(?:当天|当日)(?:卖出|止损|减仓|清仓)",
    )
    negation_pattern = re.compile(r"(不得|不能|不可|禁止|不允许|无法).{0,10}(?:当天|当日|日内|T\+0).{0,10}(?:卖出|止损|减仓|清仓|做T|做 T)")
    for segment in re.split(r"[。；;，,\n]", text):
        if not segment.strip():
            continue
        if negation_pattern.search(segment):
            continue
        if any(re.search(pattern, segment, flags=re.IGNORECASE) for pattern in risky_patterns):
            return True
    return False


def _unique_texts(values: List[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _append_text(text: Any, extra: str) -> str:
    current = str(text or "").strip()
    return f"{current} {extra}".strip() if current else extra


def _ai_decision_text(value: str) -> str:
    return {
        "buy": "DeepSeek 判断可小仓试错",
        "watch": "DeepSeek 判断等待确认",
        "avoid": "DeepSeek 判断当前不建议买入",
    }.get(value, "DeepSeek 判断等待确认")


def _empty_ai_battle() -> Dict[str, Any]:
    return {
        "source": "deepseek-role-debate",
        "bias": "neutral",
        "label": "多空拉锯",
        "conclusion": "DeepSeek 未返回完整多空博弈，等待更多证据确认。",
        "bullScore": 0,
        "bearScore": 0,
        "roleViews": [],
        "dimensions": [],
        "bullishReasons": ["DeepSeek 未返回足够看多证据"],
        "bearishReasons": ["DeepSeek 未返回足够看空证据"],
        "scoreHorizon": "mid",
        "horizonScores": {},
        "scoreMeta": {"method": "role_weighted_horizon", "calibrated": False},
    }


def _normalize_battle(value: Any, primary_horizon: str = "none") -> Dict[str, Any]:
    if not isinstance(value, dict):
        return _empty_ai_battle()
    result: Dict[str, Any] = {}
    result["source"] = str(value.get("source") or "deepseek-role-debate")
    if result.get("bias") not in {"bullish", "bearish", "neutral"}:
        result["bias"] = value.get("bias") if value.get("bias") in {"bullish", "bearish", "neutral"} else "neutral"
    result["label"] = str(value.get("label") or "多空拉锯")
    result["conclusion"] = str(value.get("conclusion") or "证据不足，等待更多数据确认。")
    result["bullScore"] = max(0, min(100, int(_safe_float(value.get("bullScore")) or 0)))
    result["bearScore"] = max(0, min(100, int(_safe_float(value.get("bearScore")) or 0)))
    result["roleViews"] = _normalize_battle_role_views(value.get("roleViews") or value.get("roles") or value.get("role_views"))
    result["dimensions"] = _normalize_battle_dimensions(value.get("dimensions"), [])
    for key, default_text in (("bullishReasons", "暂无足够看多证据"), ("bearishReasons", "暂无明显看空证据")):
        rows = _normalize_battle_reason_rows(value.get(key))
        result[key] = rows or [default_text]
    _apply_weighted_battle_scores(result, primary_horizon)
    return result


def _normalize_battle_role_views(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or item.get("name") or "").strip()
        if not role:
            continue
        stance = str(item.get("stance") or item.get("bias") or "neutral").strip()
        if stance not in {"bullish", "bearish", "neutral", "buy", "watch", "avoid"}:
            stance = "neutral"
        key_evidence = item.get("keyEvidence") or item.get("evidence") or []
        if not isinstance(key_evidence, list):
            key_evidence = [key_evidence]
        result.append(
            {
                "role": role,
                "stance": stance,
                "strength": _normalize_strength(item.get("strength")),
                "weight": _safe_float(item.get("weight")),
                "horizon": str(item.get("horizon") or "").strip(),
                "horizonImpacts": _normalize_horizon_impacts(item.get("horizonImpacts") or item.get("horizon_impacts")),
                "riskImpacts": _normalize_risk_impacts(item.get("riskImpacts") or item.get("risk_impacts")),
                "thesis": str(item.get("thesis") or item.get("summary") or "").strip(),
                "bullCase": str(item.get("bullCase") or item.get("bull") or "").strip(),
                "bearCase": str(item.get("bearCase") or item.get("bear") or "").strip(),
                "keyEvidence": [str(row).strip() for row in key_evidence if str(row or "").strip()][:4],
                "risk": str(item.get("risk") or "").strip(),
                "action": str(item.get("action") or item.get("suggestion") or "").strip(),
            }
        )
    return result[:8]


def _normalize_strength(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"weak", "medium", "strong"}:
        return text
    if text in {"弱", "偏弱"}:
        return "weak"
    if text in {"强", "强烈", "明显", "显著"}:
        return "strong"
    return "medium"


def _normalize_horizon_impacts(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, str] = {}
    for horizon in ("short", "mid", "long"):
        impact = _normalize_impact(value.get(horizon))
        if impact:
            result[horizon] = impact
    return result


def _normalize_risk_impacts(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, str] = {}
    for horizon in ("short", "mid", "long"):
        risk = str(value.get(horizon) or "").strip().lower()
        if risk in {"low", "medium", "high"}:
            result[horizon] = risk
        elif risk in {"低", "较低"}:
            result[horizon] = "low"
        elif risk in {"中", "中等"}:
            result[horizon] = "medium"
        elif risk in {"高", "较高", "强"}:
            result[horizon] = "high"
    return result


def _normalize_impact(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"bullish", "buy", "多", "偏多", "看多"}:
        return "bullish"
    if text in {"bearish", "avoid", "空", "偏空", "看空"}:
        return "bearish"
    if text in {"neutral", "watch", "中性", "观察", "拉锯"}:
        return "neutral"
    return ""


def _apply_weighted_battle_scores(result: Dict[str, Any], primary_horizon: str = "none") -> None:
    raw_bull = int(_safe_float(result.get("bullScore")) or 0)
    raw_bear = int(_safe_float(result.get("bearScore")) or 0)
    role_views = result.get("roleViews") if isinstance(result.get("roleViews"), list) else []
    if not role_views:
        result["scoreHorizon"] = "mid"
        result["horizonScores"] = {}
        result["scoreMeta"] = {
            "method": "ai_raw_no_roles",
            "calibrated": False,
            "rawBullScore": raw_bull,
            "rawBearScore": raw_bear,
        }
        return

    horizon_scores: Dict[str, Any] = {}
    for horizon in ("short", "mid", "long"):
        horizon_scores[horizon] = _weighted_horizon_score(role_views, horizon)

    score_horizon = primary_horizon if primary_horizon in {"short", "mid", "long"} else "mid"
    selected = horizon_scores.get(score_horizon) or horizon_scores["mid"]
    result["bullScore"] = selected["bullScore"]
    result["bearScore"] = selected["bearScore"]
    result["bias"] = selected["bias"]
    result["label"] = selected["label"]
    result["scoreHorizon"] = score_horizon
    result["horizonScores"] = horizon_scores
    result["scoreMeta"] = {
        "method": "role_weighted_horizon",
        "calibrated": True,
        "rawBullScore": raw_bull,
        "rawBearScore": raw_bear,
        "selectedHorizon": score_horizon,
        "scoreCap": "12-88",
    }
    LOGGER.info(
        "stock battle score calibrated raw=%s/%s horizon=%s calibrated=%s/%s label=%s role_count=%s",
        raw_bull,
        raw_bear,
        score_horizon,
        result["bullScore"],
        result["bearScore"],
        result["label"],
        len(role_views),
    )


def _weighted_horizon_score(role_views: List[Dict[str, Any]], horizon: str) -> Dict[str, Any]:
    bull = 0.0
    bear = 0.0
    contributions: List[Dict[str, Any]] = []
    for item in role_views:
        role = _canonical_role(item.get("role"))
        weight = float(ROLE_HORIZON_WEIGHTS[horizon].get(role, 10))
        strength = _infer_role_strength(item)
        relevance = _horizon_relevance(_normalize_horizon_key(item.get("horizon")), horizon)
        impact = _role_impact_for_horizon(item, horizon)
        contribution = weight * STRENGTH_FACTORS.get(strength, 0.75) * relevance
        if impact == "bullish":
            bull += contribution
        elif impact == "bearish":
            bear += contribution
        else:
            bull += contribution * 0.5
            bear += contribution * 0.5
        risk = (item.get("riskImpacts") or {}).get(horizon)
        if risk == "high":
            bear += weight * 0.18
        elif risk == "medium":
            bear += weight * 0.1
        contributions.append(
            {
                "role": item.get("role", ""),
                "weight": round(weight, 2),
                "strength": strength,
                "impact": impact,
                "relevance": round(relevance, 2),
            }
        )

    total = bull + bear
    if total <= 0:
        bull_score = 50
    else:
        bull_score = int(round((bull / total) * 100))
    bull_score = max(12, min(88, bull_score))
    bear_score = 100 - bull_score
    bias = _score_bias(bull_score, bear_score)
    return {
        "horizon": horizon,
        "bullScore": bull_score,
        "bearScore": bear_score,
        "bias": bias,
        "label": _score_label(bull_score, bear_score),
        "contributions": contributions,
    }


def _canonical_role(value: Any) -> str:
    text = str(value or "").strip()
    if text in ROLE_ALIASES:
        return ROLE_ALIASES[text]
    for key, role in ROLE_ALIASES.items():
        if key and key in text:
            return role
    return text


def _normalize_horizon_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "short": "short",
        "短线": "short",
        "mid": "mid",
        "middle": "mid",
        "中线": "mid",
        "long": "long",
        "长线": "long",
    }
    return mapping.get(text, "")


def _horizon_relevance(role_horizon: str, score_horizon: str) -> float:
    if not role_horizon or role_horizon == score_horizon:
        return 1.0
    distance = abs(("short", "mid", "long").index(role_horizon) - ("short", "mid", "long").index(score_horizon))
    return 0.68 if distance == 1 else 0.35


def _infer_role_strength(item: Dict[str, Any]) -> str:
    strength = _normalize_strength(item.get("strength"))
    text = " ".join(str(item.get(key) or "") for key in ("thesis", "risk", "bearCase", "bullCase"))
    if re.search(r"严重|极高|显著|强烈|明确|大幅|破位|暴跌|持续", text):
        return "strong"
    if re.search(r"略|尚可|可能|等待|观察|未确认|拉锯", text) and strength == "medium":
        return "weak"
    return strength


def _role_impact_for_horizon(item: Dict[str, Any], horizon: str) -> str:
    impacts = item.get("horizonImpacts") if isinstance(item.get("horizonImpacts"), dict) else {}
    if impacts.get(horizon):
        return impacts[horizon]
    stance = _normalize_impact(item.get("stance"))
    if stance in {"bullish", "bearish"}:
        return stance
    text = " ".join(str(item.get(key) or "") for key in ("thesis", "bullCase", "bearCase", "risk"))
    if re.search(r"偏多|看多|支撑|流入|放量|走强|站上|多头", text) and not re.search(r"偏空|看空|破位|流出|恶化|极高|减持|回避", text):
        return "bullish"
    if re.search(r"偏空|看空|破位|流出|恶化|极高|减持|回避|下滑", text) and not re.search(r"偏多|看多|支撑|流入|放量|走强|站上", text):
        return "bearish"
    return "neutral"


def _score_bias(bull: int, bear: int) -> str:
    if bull >= bear + 12:
        return "bullish"
    if bear >= bull + 12:
        return "bearish"
    return "neutral"


def _score_label(bull: int, bear: int) -> str:
    diff = abs(bull - bear)
    if diff < 12:
        return "多空拉锯"
    side = "多方" if bull > bear else "空方"
    if diff < 25:
        return f"{side}略占优"
    if diff < 40:
        return f"{side}占优"
    return f"{side}强占优"


def _normalize_battle_reason_rows(value: Any) -> List[Any]:
    if not isinstance(value, list):
        return []
    return _unique_reason_rows(value)


def _reason_rows_too_shallow(rows: List[Any]) -> bool:
    if not rows:
        return True
    if any(isinstance(item, dict) and str(item.get("detail") or "").strip() for item in rows):
        return False
    lengths = [len(str(item or "").strip()) for item in rows]
    return sum(lengths) / max(1, len(lengths)) < 48


def _normalize_battle_dimensions(value: Any, fallback: Any) -> List[Dict[str, Any]]:
    rows = value if isinstance(value, list) and value else fallback if isinstance(fallback, list) else []
    result: List[Dict[str, Any]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or "").strip()
        key = str(item.get("key") or f"dimension_{index}").strip()
        evidence_rows = item.get("evidence")
        if not isinstance(evidence_rows, list):
            evidence_rows = item.get("reasons") if isinstance(item.get("reasons"), list) else []
        evidence_rows = [str(row) for row in evidence_rows if str(row or "").strip()]
        if not label or not evidence_rows:
            continue
        bull = max(0, min(100, int(_safe_float(item.get("bullScore")) or 0)))
        bear = max(0, min(100, int(_safe_float(item.get("bearScore")) or 0)))
        bias = item.get("bias")
        if bias not in {"bullish", "bearish", "neutral"}:
            bias = _dimension_bias(bull, bear)
        result.append(
            {
                "key": key,
                "label": label,
                "bias": bias,
                "bullScore": bull,
                "bearScore": bear,
                "evidence": evidence_rows[:5],
            }
        )
    return result


def _extract_json_object(content: str) -> Dict[str, Any]:
    if not content:
        return {}
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def _extract_finish_reason(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason", ""))
    return ""


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _append_unique(items: List[str], value: str) -> List[str]:
    result = list(items or [])
    if value and value not in result:
        result.append(value)
    return result


def _append_warning_text(current: str, value: str) -> str:
    if not current:
        return value
    if not value or value in current:
        return current
    return f"{current}；{value}"


def _unique(items: List[Any]) -> List[str]:
    result = []
    seen = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on", "是", "允许", "可", "可以"}:
        return True
    if text in {"false", "0", "no", "n", "off", "否", "不", "不允许", "不可", "不能"}:
        return False
    return default
