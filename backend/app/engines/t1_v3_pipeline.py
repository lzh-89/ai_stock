"""Deterministic T+1 V3 shadow pipeline.

The module is intentionally free of I/O.  It turns frozen quote/minute facts into
versioned recall, validation, tail-gate and portfolio decisions.  Callers decide
whether the result is shadow or official; the engine never promotes itself.
"""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import datetime
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional


PIPELINE_VERSION = "2026-11-06.t1_v3_shadow.v1"
RECALL_MODEL_VERSION = "t1_v3_recall_expectation_v1"
VALIDATION_ALLOCATION_VERSION = "t1_v3_validation_70_20_15_10_5_v1"
STRUCTURE_FEATURE_VERSION = "t1_v3_structure_features_v1"
TAIL_GATE_VERSION = "t1_v3_tail_negative_gate_v1"
PORTFOLIO_POLICY_VERSION = "t1_v3_portfolio_two_slots_v1"

RECALL_MIN_DAYS = 60
RECALL_MIN_SAMPLES = 3000
RECALL_MIN_BIN_SAMPLES = 50
RECALL_PRIOR_SAMPLES = 50
MAX_INVERSE_PROBABILITY_WEIGHT = 20.0

RECALL_QUOTAS = {"core": 350, "coverage": 100, "explore": 40, "audit": 10}
VALIDATION_QUOTAS = {"core": 70, "boundary": 20, "coverage": 15, "explore": 10, "audit": 5}
TAIL_QUOTAS = {"core": 20, "uncertainty": 5, "coverage": 3, "explore": 2}


def build_recall_model(samples: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Fit the quote-only next-open proxy model using prior-date samples."""
    rows = []
    for raw in samples or []:
        value = _number(raw.get("openProxyReturnPct"))
        stratum = str(raw.get("recallStratum") or "")
        trade_date = str(raw.get("recommendationDate") or "")[:10]
        if value is None or not stratum or not trade_date:
            continue
        row = dict(raw)
        row["openProxyReturnPct"] = value
        row["recommendationDate"] = trade_date
        rows.append(row)

    dates = sorted({row["recommendationDate"] for row in rows})
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["recallStratum"])].append(row)
    global_mean = _weighted_mean(rows, "openProxyReturnPct")
    bins: Dict[str, Dict[str, Any]] = {}
    for stratum, values in grouped.items():
        weighted = _weighted_mean(values, "openProxyReturnPct")
        returns = [float(value["openProxyReturnPct"]) for value in values]
        sample_count = len(returns)
        shrunk = (
            (weighted * sample_count + global_mean * RECALL_PRIOR_SAMPLES)
            / (sample_count + RECALL_PRIOR_SAMPLES)
            if sample_count
            else global_mean
        )
        uncertainty = _stddev(returns) / math.sqrt(sample_count) if sample_count else None
        worst_count = max(1, math.ceil(sample_count * 0.2)) if sample_count else 0
        worst_mean = mean(sorted(returns)[:worst_count]) if worst_count else None
        left_tail = 0.25 * abs(worst_mean) if worst_mean is not None else None
        eligible = sample_count >= RECALL_MIN_BIN_SAMPLES
        score = shrunk - uncertainty - left_tail if eligible and uncertainty is not None and left_tail is not None else None
        bins[stratum] = {
            "sampleCount": sample_count,
            "weightedMeanPct": _round(weighted),
            "shrunkMeanPct": _round(shrunk),
            "uncertaintyPenaltyPct": _round(uncertainty),
            "leftTailPenaltyPct": _round(left_tail),
            "recallConservativeScorePct": _round(score),
            "rankingEligible": eligible,
        }
    ready = len(dates) >= RECALL_MIN_DAYS and len(rows) >= RECALL_MIN_SAMPLES and any(
        item.get("rankingEligible") for item in bins.values()
    )
    return {
        "version": RECALL_MODEL_VERSION,
        "status": "ready" if ready else "collecting",
        "ready": ready,
        "tradeDayCount": len(dates),
        "sampleCount": len(rows),
        "readyBinCount": sum(bool(item.get("rankingEligible")) for item in bins.values()),
        "globalWeightedMeanPct": _round(global_mean) if rows else None,
        "requirements": {
            "minimumTradeDays": RECALL_MIN_DAYS,
            "minimumSamples": RECALL_MIN_SAMPLES,
            "minimumBinSamples": RECALL_MIN_BIN_SAMPLES,
        },
        "bins": bins,
    }


def assign_recall_scores(
    candidates: Iterable[Dict[str, Any]],
    model: Dict[str, Any],
    market_state: str,
) -> List[Dict[str, Any]]:
    """Attach reproducible recall strata and scores to frozen quote candidates."""
    rows = _unique_candidates(candidates)
    amount_percentiles = _percentiles({row["code"]: math.log1p(max(0.0, _number(row.get("amount")) or 0.0)) for row in rows})
    for row in rows:
        amount_percentile = amount_percentiles.get(row["code"], 0.0)
        row["liquidityPercentile"] = _round(amount_percentile)
        row["recallStratum"] = recall_stratum(row, market_state, amount_percentile)
        estimate = dict((model.get("bins") or {}).get(row["recallStratum"]) or {})
        row["recallEstimate"] = estimate
        row["recallRankingEligible"] = bool(model.get("ready") and estimate.get("rankingEligible"))
        row["recallConservativeScore"] = (
            _number(estimate.get("recallConservativeScorePct")) if row["recallRankingEligible"] else None
        )
    return rows


def recall_stratum(candidate: Dict[str, Any], market_state: str, amount_percentile: Optional[float] = None) -> str:
    change = _number(candidate.get("change")) or 0.0
    strength = _number(candidate.get("relStrength"))
    if strength is None:
        strength = _number(((candidate.get("cheapFeatures") or {}).get("relativeStrength"))) or 0.0
    liquidity = amount_percentile if amount_percentile is not None else (_number(candidate.get("liquidityPercentile")) or 0.0)
    tradeability = _tradeability_bucket(candidate)
    return "|".join(
        (
            str(market_state or "unknown"),
            _range_bucket(change, ((0, "lt0"), (2, "0_2"), (5, "2_5"), (7, "5_7"), (8, "7_8")), "gte8"),
            _range_bucket(strength, ((50, "lt50"), (70, "50_69"), (85, "70_84"), (95, "85_94")), "gte95"),
            f"q{min(5, max(1, int(liquidity // 20) + 1))}",
            tradeability,
        )
    )


def build_recall_pool(
    candidates: Iterable[Dict[str, Any]],
    model: Dict[str, Any],
    market_state: str,
    seed: str,
    fallback_codes: Optional[Iterable[str]] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    scored = assign_recall_scores(candidates, model, market_state)
    by_code = {row["code"]: row for row in scored}
    limit = max(0, min(int(limit or 0), len(scored)))
    selected: List[Dict[str, Any]] = []
    roles: Dict[str, str] = {}

    if not model.get("ready"):
        ordered_codes = [_code(code) for code in fallback_codes or []]
        ordered = [by_code[code] for code in ordered_codes if code in by_code]
        remaining = sorted(scored, key=_fallback_rank_key)
        _take_unique(selected, roles, ordered + remaining, limit, "collection_existing_pool")
    else:
        eligible = [row for row in scored if row.get("formalTradeabilityEligible", True) and row.get("recallRankingEligible")]
        eligible.sort(key=_recall_rank_key)
        _take_unique(selected, roles, eligible, min(RECALL_QUOTAS["core"], limit), "core")

        coverage_source = [row for row in scored if row.get("formalTradeabilityEligible", True)]
        coverage = _round_robin_coverage(coverage_source, selected, RECALL_QUOTAS["coverage"])
        _take_unique(selected, roles, coverage, min(limit, len(selected) + RECALL_QUOTAS["coverage"]), "coverage")

        exploration = _decile_exploration(eligible, seed, RECALL_QUOTAS["explore"])
        _take_unique(selected, roles, exploration, min(limit, len(selected) + RECALL_QUOTAS["explore"]), "explore")

        audits = [
            row for row in scored
            if not row.get("formalTradeabilityEligible", True) or not row.get("recallRankingEligible")
        ]
        audits.sort(key=lambda row: (_digest(f"{seed}:audit:{row['code']}"), row["code"]))
        _take_unique(selected, roles, audits, min(limit, len(selected) + RECALL_QUOTAS["audit"]), "audit")
        _take_unique(selected, roles, eligible, limit, "core_fill")
        fallback = sorted(
            [row for row in scored if row.get("formalTradeabilityEligible", True)],
            key=_fallback_rank_key,
        )
        _take_unique(selected, roles, fallback, limit, "eligible_fill")

    for index, row in enumerate(selected, start=1):
        row["v3RecallRank"] = index
        row["v3RecallRole"] = roles.get(row["code"], "")
    return {
        "version": PIPELINE_VERSION,
        "modelVersion": model.get("version") or RECALL_MODEL_VERSION,
        "modelStatus": model.get("status") or "collecting",
        "ready": bool(model.get("ready")),
        "candidateCount": len(scored),
        "selectedCount": len(selected),
        "roleCounts": _counts(roles.values()),
        "pool": selected,
    }


def allocate_validation(candidates: Iterable[Dict[str, Any]], seed: str, limit: int = 120) -> Dict[str, Any]:
    rows = _unique_candidates(candidates)
    limit = max(0, min(int(limit or 0), len(rows)))
    ready = any(row.get("recallRankingEligible") for row in rows)
    ranked = sorted(rows, key=_recall_rank_key if ready else _fallback_rank_key)
    selected: List[Dict[str, Any]] = []
    roles: Dict[str, str] = {}

    formal = [row for row in ranked if row.get("formalTradeabilityEligible", True)]
    _take_unique(selected, roles, formal, min(VALIDATION_QUOTAS["core"], limit), "core" if ready else "collection_core")

    cutoff = _number(selected[-1].get("recallConservativeScore")) if ready and selected else None
    if cutoff is not None:
        boundary = sorted(
            [row for row in formal if _number(row.get("recallConservativeScore")) is not None],
            key=lambda row: (
                abs(float(row["recallConservativeScore"]) - cutoff) > 0.25,
                -(_number((row.get("recallEstimate") or {}).get("uncertaintyPenaltyPct")) or 0.0),
                abs(float(row["recallConservativeScore"]) - cutoff),
                row["code"],
            ),
        )
    else:
        boundary = sorted(formal, key=lambda row: (abs(int(row.get("v3RecallRank") or 500) - 70), row["code"]))
    _take_unique(selected, roles, boundary, min(limit, len(selected) + VALIDATION_QUOTAS["boundary"]), "boundary")

    coverage = _driver_industry_coverage(formal, selected, VALIDATION_QUOTAS["coverage"])
    _take_unique(selected, roles, coverage, min(limit, len(selected) + VALIDATION_QUOTAS["coverage"]), "coverage")

    explore = _rank_band_exploration(formal, seed, 500, 5, 2)
    _take_unique(selected, roles, explore, min(limit, len(selected) + VALIDATION_QUOTAS["explore"]), "explore")

    audits = [row for row in ranked if not row.get("formalTradeabilityEligible", True)]
    audits += sorted(
        formal,
        key=lambda row: (-(_number((row.get("recallEstimate") or {}).get("uncertaintyPenaltyPct")) or 0.0), row["code"]),
    )
    _take_unique(selected, roles, audits, min(limit, len(selected) + VALIDATION_QUOTAS["audit"]), "audit")
    _take_unique(selected, roles, formal, limit, "eligible_fill")

    for index, row in enumerate(selected, start=1):
        role = roles.get(row["code"], "")
        row["v3ValidationRank"] = index
        row["v3ValidationRole"] = role
        row["researchOnly"] = role == "audit" or not row.get("formalTradeabilityEligible", True)
    return {
        "version": VALIDATION_ALLOCATION_VERSION,
        "modelReady": ready,
        "selectedCount": len(selected),
        "roleCounts": _counts(roles.values()),
        "candidates": selected,
    }


def validate_15m_data(
    candidate: Dict[str, Any],
    series: Iterable[Dict[str, Any]],
    *,
    recommendation_date: str,
    decision_cutoff: str,
    source: str = "",
    quality_status: str = "",
) -> Dict[str, Any]:
    points = [dict(point) for point in series or []]
    reasons: List[str] = []
    normalized_source = str(source or "").lower()
    if normalized_source in {"", "mock", "simulated", "unavailable"}:
        reasons.append("15分钟数据来源不合格")
    if str(quality_status or "").lower() in {"stale", "unavailable", "mock", "simulated"}:
        reasons.append("15分钟数据质量状态不合格")
    if len(points) < 8:
        reasons.append("有效15分钟K线少于8根")
    cutoff = _parse_moment(decision_cutoff, recommendation_date)
    valid_points = []
    for point in points:
        moment = _point_moment(point, recommendation_date)
        price = _number(point.get("close"))
        if not moment or moment.date().isoformat() != recommendation_date:
            reasons.append("15分钟K线日期与推荐日不一致")
            continue
        if cutoff and moment > cutoff:
            reasons.append("15分钟K线晚于决策截止时间")
            continue
        if price is None or price <= 0:
            reasons.append("15分钟K线价格无效")
            continue
        valid_points.append(point)
    quote_date = str(candidate.get("quoteTradeDate") or "")[:10]
    if quote_date and quote_date != recommendation_date:
        reasons.append("报价交易日与推荐日不一致")
    reasons = _unique_strings(reasons)
    eligible = not reasons and len(valid_points) >= 8
    return {
        "version": STRUCTURE_FEATURE_VERSION,
        "dataEligible": eligible,
        "pointCount": len(valid_points),
        "source": source,
        "qualityStatus": quality_status or ("available" if points else "unavailable"),
        "reasons": reasons,
        "series": valid_points,
    }


def calculate_structure_features(series: Iterable[Dict[str, Any]], relative_strength: Optional[float] = None, change: Optional[float] = None) -> Dict[str, Any]:
    points = [dict(point) for point in series or []]
    if len(points) < 8:
        return {
            "version": STRUCTURE_FEATURE_VERSION,
            "status": "unavailable",
            "reasons": ["有效15分钟K线少于8根"],
        }
    closes = [_number(point.get("close")) for point in points]
    highs = [_number(point.get("high")) or closes[index] for index, point in enumerate(points)]
    lows = [_number(point.get("low")) or closes[index] for index, point in enumerate(points)]
    volumes = [max(0.0, _number(point.get("volume")) or 0.0) for point in points]
    if any(value is None or value <= 0 for value in closes):
        return {"version": STRUCTURE_FEATURE_VERSION, "status": "unavailable", "reasons": ["15分钟收盘价无效"]}
    close_values = [float(value) for value in closes if value is not None]
    total_volume = sum(volumes)
    vwap = sum(close_values[index] * volumes[index] for index in range(len(points))) / total_volume if total_volume > 0 else mean(close_values)
    reference_high = max(float(value) for value in highs[:-2] if value is not None)
    reference_low = min(float(value) for value in lows[:-2] if value is not None)
    true_ranges = []
    for index in range(1, len(points)):
        previous = close_values[index - 1]
        high = float(highs[index])
        low = float(lows[index])
        true_range = max(high - low, abs(high - previous), abs(low - previous))
        true_ranges.append(true_range / previous * 100 if previous > 0 else 0.0)
    volatility = mean(true_ranges) if true_ranges else 0.0
    breakout_margin = _clamp(volatility * 0.25, 0.10, 0.50)
    max_extension = _clamp(volatility * 3.0, 1.50, 3.00)
    support_tolerance = _clamp(volatility * 0.50, 0.20, 0.50)
    extension = (close_values[-1] / vwap - 1) * 100 if vwap > 0 else None
    breakout_hold = all(value >= reference_high * (1 + breakout_margin / 100) for value in close_values[-2:])
    recent_lows = [float(value) for value in lows[-3:]]
    vwap_support = (
        min(recent_lows) <= vwap * (1 + support_tolerance / 100)
        and all(value >= vwap for value in close_values[-2:])
        and close_values[-1] >= close_values[-2]
    )
    early_low = min(float(value) for value in lows[: max(2, len(points) // 2)])
    low_reversal = min(recent_lows[-2:]) > early_low and close_values[-1] >= vwap and close_values[-1] > close_values[-4]
    strength = _number(relative_strength) or 0.0
    day_change = _number(change) or 0.0
    relative_strength_hold = strength >= 70 and day_change > 0 and all(value >= vwap for value in close_values[-2:]) and close_values[-1] >= close_values[-4] * 0.998
    flags = {
        "breakoutHold": breakout_hold,
        "vwapSupport": vwap_support,
        "lowReversal": low_reversal,
        "relativeStrengthHold": relative_strength_hold,
    }
    structure_score = sum(25 for value in flags.values() if value)
    counter = []
    if extension is not None and extension > max_extension:
        counter.append("最新价相对成交均价偏离过大")
    return {
        "version": STRUCTURE_FEATURE_VERSION,
        "status": "available",
        "vwapEstimate": _round(vwap, 6),
        "vwapExtensionPct": _round(extension),
        "referenceHigh": _round(reference_high, 6),
        "referenceLow": _round(reference_low, 6),
        "referenceVolatilityPct": _round(volatility),
        "breakoutMarginPct": _round(breakout_margin),
        "maxVwapExtensionPct": _round(max_extension),
        "supportTolerancePct": _round(support_tolerance),
        **flags,
        "evidenceStrength": {key: 100 if value else 0 for key, value in flags.items()},
        "structureScore": structure_score,
        "structureCounterEvidence": counter,
    }


def allocate_tail_pool(candidates: Iterable[Dict[str, Any]], seed: str, limit: int = 30) -> Dict[str, Any]:
    rows = [row for row in _unique_candidates(candidates) if row.get("dataEligible")]
    limit = max(0, min(int(limit or 0), len(rows)))
    selected: List[Dict[str, Any]] = []
    roles: Dict[str, str] = {}
    eligible = [row for row in rows if _ranking_eligible(row)]
    eligible.sort(key=_net_expectation_rank_key)
    collection_mode = not bool(eligible)
    calibration_source = eligible if eligible else sorted(rows, key=_fallback_rank_key)
    _take_unique(
        selected,
        roles,
        calibration_source,
        min(TAIL_QUOTAS["core"], limit),
        "core" if eligible else "collection_core",
    )
    cutoff = _conservative_score(selected[-1]) if selected else None
    uncertainty = sorted(
        calibration_source,
        key=lambda row: (
            abs((_conservative_score(row) or -999.0) - cutoff) if cutoff is not None else 999.0,
            -(_uncertainty_penalty(row) or 0.0),
            row["code"],
        ),
    )
    _take_unique(selected, roles, uncertainty, min(limit, len(selected) + TAIL_QUOTAS["uncertainty"]), "uncertainty")
    coverage = _driver_industry_coverage(calibration_source, selected, TAIL_QUOTAS["coverage"])
    _take_unique(selected, roles, coverage, min(limit, len(selected) + TAIL_QUOTAS["coverage"]), "coverage" if eligible else "collection_coverage")
    remaining = sorted(calibration_source, key=lambda row: (_digest(f"{seed}:tail:{row['code']}"), row["code"]))
    _take_unique(selected, roles, remaining, min(limit, len(selected) + TAIL_QUOTAS["explore"]), "explore" if eligible else "collection_explore")
    _take_unique(selected, roles, calibration_source, limit, "eligible_fill" if eligible else "collection_fill")
    inclusion_probability = min(1.0, len(selected) / len(rows)) if rows else 0.0
    for index, row in enumerate(selected, start=1):
        row["v3TailRank"] = index
        row["v3TailRole"] = roles.get(row["code"], "")
        row["v3CalibrationInclusionProbability"] = inclusion_probability
        row["v3CalibrationSample"] = True
    return {
        "version": PIPELINE_VERSION,
        "mode": "collection_calibration" if collection_mode else "ranking_ready",
        "selectedCount": len(selected),
        "inclusionProbability": _round(inclusion_probability, 6),
        "roleCounts": _counts(roles.values()),
        "candidates": selected,
    }


def evaluate_tail_gate(
    series: Iterable[Dict[str, Any]],
    *,
    recommendation_date: str,
    decision_cutoff: str,
    source: str = "",
    tradeability_eligible: bool = True,
) -> Dict[str, Any]:
    points = [dict(point) for point in series or []]
    reasons: List[str] = []
    if str(source or "").lower() in {"", "mock", "simulated", "unavailable"}:
        reasons.append("1分钟数据来源不合格")
    cutoff = _parse_moment(decision_cutoff, recommendation_date)
    parsed = [(point, _point_moment(point, recommendation_date)) for point in points]
    parsed = [(point, moment) for point, moment in parsed if moment and moment.date().isoformat() == recommendation_date and (not cutoff or moment <= cutoff)]
    parsed.sort(key=lambda item: item[1])
    if len(parsed) < 5:
        reasons.append("尾盘有效1分钟点少于5个")
    if parsed and cutoff and (cutoff - parsed[-1][1]).total_seconds() > 120:
        reasons.append("最近尾盘分钟距离决策截止超过2分钟")
    if not tradeability_eligible:
        reasons.append("推荐时点已不具备正式交易资格")
    last = [point for point, _ in parsed[-5:]]
    closes = [_number(point.get("close")) for point in last]
    volumes = [max(0.0, _number(point.get("volume")) or 0.0) for point in last]
    if len(closes) < 5 or any(value is None or value <= 0 for value in closes):
        reasons.append("最近5分钟价格点无效")
        closes = []
    all_points = [point for point, _ in parsed]
    all_closes = [_number(point.get("close")) for point in all_points]
    all_volumes = [max(0.0, _number(point.get("volume")) or 0.0) for point in all_points]
    valid_all = len(all_closes) >= 5 and all(value is not None and value > 0 for value in all_closes)
    change5 = (float(closes[-1]) / float(closes[0]) - 1) * 100 if closes else None
    sell_volume = 0.0
    consecutive_sell = 0
    max_consecutive_sell = 0
    if closes:
        for index in range(1, len(closes)):
            if float(closes[index]) < float(closes[index - 1]):
                sell_volume += volumes[index]
                consecutive_sell += 1
                max_consecutive_sell = max(max_consecutive_sell, consecutive_sell)
            else:
                consecutive_sell = 0
    sell_ratio = sell_volume / sum(volumes) * 100 if sum(volumes) > 0 else 0.0
    max_drawdown = None
    vwap = None
    deviation = None
    if valid_all:
        peak = float(all_closes[0])
        drawdowns = []
        for value in all_closes:
            price = float(value)
            peak = max(peak, price)
            drawdowns.append((price / peak - 1) * 100)
        max_drawdown = min(drawdowns)
        total_volume = sum(all_volumes)
        vwap = sum(float(all_closes[index]) * all_volumes[index] for index in range(len(all_closes))) / total_volume if total_volume > 0 else mean(float(value) for value in all_closes)
        deviation = (float(all_closes[-1]) / vwap - 1) * 100 if vwap > 0 else None
    recovered = bool(deviation is not None and deviation >= 0 and change5 is not None and change5 >= 0)
    price_negative = bool(
        (change5 is not None and change5 <= -0.5 and (sell_ratio >= 60 or max_consecutive_sell >= 3))
        or (max_drawdown is not None and max_drawdown <= -1.2 and (deviation is None or deviation < 0))
    )
    if price_negative and not recovered:
        reasons.append("尾盘持续卖压或最大回撤命中负面门槛")
    reasons = _unique_strings(reasons)
    return {
        "version": TAIL_GATE_VERSION,
        "tailNegative": bool(reasons),
        "recoveredVwap": recovered,
        "change5mPct": _round(change5),
        "sellPressureVolumeRatio": _round(sell_ratio),
        "consecutiveSellMinutes": max_consecutive_sell,
        "tailMaxDrawdownPct": _round(max_drawdown),
        "vwapEstimate": _round(vwap, 6),
        "vwapDeviationPct": _round(deviation),
        "pointCount": len(parsed),
        "reasons": reasons,
    }


def finalize_shadow_portfolio(
    candidates: Iterable[Dict[str, Any]],
    market_state: str,
    *,
    shortlist_limit: int = 8,
    score_tolerance_pct: float = 0.20,
) -> Dict[str, Any]:
    rows = _unique_candidates(candidates)
    terminal_counts: Dict[str, int] = defaultdict(int)
    shortlist = []
    for row in rows:
        stage = terminal_stage(row)
        row["terminalStage"] = stage
        terminal_counts[stage] += 1
        if stage == "shortlist_eligible":
            shortlist.append(row)
    shortlist.sort(key=_net_expectation_rank_key)
    shortlist = shortlist[: max(0, int(shortlist_limit or 0))]
    slot_limits = {
        "panic": 0,
        "heavy_down": 0,
        "unknown": 0,
        "mild_down": 1,
        "mixed": 1,
        "mild_up": 2,
        "trend_up": 2,
        "euphoric": 1,
    }
    slot_limit = slot_limits.get(str(market_state or "unknown"), 0)
    portfolio: List[Dict[str, Any]] = []
    for row in shortlist:
        if len(portfolio) >= slot_limit:
            break
        conflicts = [
            current for current in portfolio
            if _identity(row, "industryGroup", "industry") == _identity(current, "industryGroup", "industry")
            or _identity(row, "driverGroup", "primaryDriver") == _identity(current, "driverGroup", "primaryDriver")
        ]
        if conflicts:
            alternatives = [candidate for candidate in shortlist if candidate not in portfolio and candidate is not row]
            independent = [candidate for candidate in alternatives if all(
                _identity(candidate, "industryGroup", "industry") != _identity(current, "industryGroup", "industry")
                and _identity(candidate, "driverGroup", "primaryDriver") != _identity(current, "driverGroup", "primaryDriver")
                for current in portfolio
            )]
            replacement = next(
                (
                    candidate for candidate in independent
                    if (_conservative_score(row) or 0.0) - (_conservative_score(candidate) or -999.0) <= score_tolerance_pct
                ),
                None,
            )
            if replacement is not None:
                row = replacement
        if row not in portfolio:
            portfolio.append(row)
    selected_codes = {row["code"] for row in portfolio}
    for row in shortlist:
        row["terminalStage"] = "v3_shadow_selected" if row["code"] in selected_codes else "shortlist_not_portfolio"
    return {
        "version": PIPELINE_VERSION,
        "portfolioPolicyVersion": PORTFOLIO_POLICY_VERSION,
        "stage": "shadow",
        "affectsOfficialRecommendation": False,
        "marketState": market_state or "unknown",
        "shortlistCount": len(shortlist),
        "slotLimit": slot_limit,
        "holdingCount": len(portfolio),
        "cashSlotCount": max(0, 3 - len(portfolio)),
        "shortlist": shortlist,
        "picks": portfolio,
        "terminalStageCounts": dict(terminal_counts),
    }


def terminal_stage(candidate: Dict[str, Any]) -> str:
    if candidate.get("hardFilterStatus") == "rejected":
        return "hard_filter_rejected"
    if not candidate.get("v3RecallRank"):
        return "pool_not_recalled"
    if not candidate.get("v3ValidationRank"):
        return "validation_not_allocated"
    if str(candidate.get("minute15mStatus") or "") in {"failed", "quality_rejected", "unavailable"} or not candidate.get("dataEligible"):
        return "minute15m_failed"
    if not _ranking_eligible(candidate):
        return "model_observation_only"
    if not candidate.get("v3TailRank"):
        return "tail_not_allocated"
    tail = candidate.get("v3TailGate") or {}
    if str(candidate.get("minute1mStatus") or "") in {"failed", "unavailable"}:
        return "minute1m_failed"
    if tail.get("tailNegative"):
        return "tail_negative_rejected"
    if candidate.get("eventRiskHardBlocked"):
        return "event_risk_rejected"
    if (_conservative_score(candidate) or -999.0) <= 0:
        return "non_positive_expectation"
    if not candidate.get("formalTradeabilityEligible", True):
        return "tradeability_rejected"
    return "shortlist_eligible"


def _ranking_eligible(candidate: Dict[str, Any]) -> bool:
    estimate = candidate.get("netExpectationEstimate") or {}
    return bool(estimate.get("rankingEligible"))


def _conservative_score(candidate: Dict[str, Any]) -> Optional[float]:
    return _number((candidate.get("netExpectationEstimate") or {}).get("conservativeScorePct"))


def _uncertainty_penalty(candidate: Dict[str, Any]) -> Optional[float]:
    return _number((candidate.get("netExpectationEstimate") or {}).get("uncertaintyPenaltyPct"))


def _net_expectation_rank_key(candidate: Dict[str, Any]) -> tuple:
    score = _conservative_score(candidate)
    return (-(score if score is not None else -999999.0), -(_number(candidate.get("t1Score")) or 0.0), candidate["code"])


def _recall_rank_key(candidate: Dict[str, Any]) -> tuple:
    score = _number(candidate.get("recallConservativeScore"))
    return (-(score if score is not None else -999999.0), int(candidate.get("preliminaryScoreRank") or 999999), candidate["code"])


def _fallback_rank_key(candidate: Dict[str, Any]) -> tuple:
    return (int(candidate.get("preliminaryScoreRank") or candidate.get("recallRank") or 999999), -(_number(candidate.get("preliminaryScore")) or 0.0), candidate["code"])


def _tradeability_bucket(candidate: Dict[str, Any]) -> str:
    value = candidate.get("tradeability") or {}
    level = str(value.get("level") if isinstance(value, dict) else value or "tradable").replace("-", "_")
    return {
        "hard_to_buy": "hard_to_buy",
        "not_buyable": "not_buyable",
        "chase_risk": "chase_risk",
    }.get(level, "tradable")


def _range_bucket(value: float, thresholds: tuple, fallback: str) -> str:
    for upper, label in thresholds:
        if value < upper:
            return label
    return fallback


def _round_robin_coverage(candidates: List[Dict[str, Any]], selected: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    existing = {row["code"] for row in selected}
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        if row["code"] in existing:
            continue
        stratum = str(row.get("recallStratum") or "unknown|unknown|unknown|unknown|unknown").split("|")
        labels = [
            f"change:{stratum[1] if len(stratum) > 1 else 'unknown'}",
            f"strength:{stratum[2] if len(stratum) > 2 else 'unknown'}",
            f"liquidity:{stratum[3] if len(stratum) > 3 else 'unknown'}",
            f"industry:{_identity(row, 'industryGroup', 'industry')}",
            f"driver:{_identity(row, 'primaryDriver', 'primaryCandidateType')}",
        ]
        for label in labels:
            buckets[label].append(row)
    for values in buckets.values():
        values.sort(key=_recall_rank_key)
    result: List[Dict[str, Any]] = []
    seen = set(existing)
    labels = sorted(buckets)
    cursor = 0
    while len(result) < limit and labels:
        label = labels[cursor % len(labels)]
        queue = buckets[label]
        row = next((item for item in queue if item["code"] not in seen), None)
        if row is not None:
            result.append(row)
            seen.add(row["code"])
        elif all(all(item["code"] in seen for item in buckets[key]) for key in labels):
            break
        cursor += 1
    return result


def _driver_industry_coverage(candidates: List[Dict[str, Any]], selected: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    existing = {row["code"] for row in selected}
    selected_counts: Dict[str, int] = defaultdict(int)
    for row in selected:
        selected_counts[f"{_identity(row, 'primaryDriver', 'primaryCandidateType')}|{_identity(row, 'industryGroup', 'industry')}"] += 1
    remaining = [row for row in candidates if row["code"] not in existing]
    result = []
    while remaining and len(result) < limit:
        remaining.sort(
            key=lambda row: (
                selected_counts[f"{_identity(row, 'primaryDriver', 'primaryCandidateType')}|{_identity(row, 'industryGroup', 'industry')}"] ,
                _recall_rank_key(row),
            )
        )
        row = remaining.pop(0)
        result.append(row)
        selected_counts[f"{_identity(row, 'primaryDriver', 'primaryCandidateType')}|{_identity(row, 'industryGroup', 'industry')}"] += 1
    return result


def _decile_exploration(candidates: List[Dict[str, Any]], seed: str, limit: int) -> List[Dict[str, Any]]:
    if not candidates or limit <= 0:
        return []
    result = []
    for decile in range(10):
        lo = math.floor(len(candidates) * decile / 10)
        hi = math.floor(len(candidates) * (decile + 1) / 10)
        rows = sorted(candidates[lo:hi], key=lambda row: (_digest(f"{seed}:decile:{decile}:{row['code']}"), row["code"]))
        result.extend(rows[: max(1, limit // 10)])
    return result[:limit]


def _rank_band_exploration(candidates: List[Dict[str, Any]], seed: str, maximum_rank: int, bands: int, per_band: int) -> List[Dict[str, Any]]:
    result = []
    width = max(1, maximum_rank // bands)
    for index in range(bands):
        lo = index * width + 1
        hi = maximum_rank if index == bands - 1 else (index + 1) * width
        rows = [row for row in candidates if lo <= int(row.get("v3RecallRank") or 999999) <= hi]
        rows.sort(key=lambda row: (_digest(f"{seed}:band:{index}:{row['code']}"), row["code"]))
        result.extend(rows[:per_band])
    return result


def _take_unique(selected: List[Dict[str, Any]], roles: Dict[str, str], candidates: Iterable[Dict[str, Any]], target_total: int, role: str) -> None:
    seen = {row["code"] for row in selected}
    for row in candidates:
        if len(selected) >= target_total:
            break
        code = row["code"]
        if code in seen:
            continue
        selected.append(dict(row))
        roles[code] = role
        seen.add(code)


def _unique_candidates(candidates: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for raw in candidates or []:
        code = _code(raw.get("code"))
        if not code or code in seen:
            continue
        row = dict(raw)
        row["code"] = code
        result.append(row)
        seen.add(code)
    return result


def _weighted_mean(rows: List[Dict[str, Any]], field: str) -> float:
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        value = _number(row.get(field))
        if value is None:
            continue
        probability = _number(row.get("inclusionProbability"))
        if probability is None:
            probability = _number(row.get("populationInclusionProbability"))
        probability = probability if probability and probability > 0 else 1.0
        weight = min(MAX_INVERSE_PROBABILITY_WEIGHT, 1.0 / probability)
        numerator += value * weight
        denominator += weight
    return numerator / denominator if denominator else 0.0


def _stddev(values: List[float]) -> float:
    if len(values) <= 1:
        return 0.0
    center = mean(values)
    return math.sqrt(sum((value - center) ** 2 for value in values) / (len(values) - 1))


def _percentiles(values: Dict[str, float]) -> Dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    if count <= 1:
        return {code: 0.0 for code, _ in ordered}
    result = {}
    index = 0
    while index < count:
        end = index + 1
        while end < count and ordered[end][1] == ordered[index][1]:
            end += 1
        average_rank = ((index + 1) + end) / 2
        percentile = 100 * (average_rank - 1) / max(1, count - 1)
        for position in range(index, end):
            result[ordered[position][0]] = percentile
        index = end
    return result


def _point_moment(point: Dict[str, Any], recommendation_date: str) -> Optional[datetime]:
    text = str(point.get("bucketEnd") or point.get("timestamp") or point.get("datetime") or point.get("time") or point.get("date") or "")
    if text and len(text) <= 8 and ":" in text:
        text = f"{recommendation_date}T{text}"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _parse_moment(value: str, recommendation_date: str) -> Optional[datetime]:
    text = str(value or "")
    if len(text) <= 8 and ":" in text:
        text = f"{recommendation_date}T{text}"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _identity(row: Dict[str, Any], primary: str, fallback: str) -> str:
    return str(row.get(primary) or row.get(fallback) or "unknown")


def _counts(values: Iterable[str]) -> Dict[str, int]:
    result: Dict[str, int] = defaultdict(int)
    for value in values:
        result[str(value or "unknown")] += 1
    return dict(result)


def _unique_strings(values: Iterable[str]) -> List[str]:
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    return round(float(value), digits) if value is not None and math.isfinite(float(value)) else None


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _code(value: Any) -> str:
    text = str(value or "").strip()
    return text.zfill(6) if text.isdigit() else text
