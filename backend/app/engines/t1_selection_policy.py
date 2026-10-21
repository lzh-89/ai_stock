"""Versioned, evidence based T+1 policy. No network, simulated bars or legacy board score."""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta

VERSION = "2026-10-16.formal.v1"
PARAMETERS = {
    "weights": {"liquidity": .20, "relativeStrength": .25, "structure": .40, "industry": .15, "tail": .20},
    "riskWeight": .20,
    "minBars": 8,
    "breakoutMarginPct": .10,
    "maxVwapExtensionPct": 3.0,
    "supportTolerancePct": .5,
    "volatilityThresholds": {"breakoutFactor": .25, "breakoutMinPct": .10, "breakoutMaxPct": .5,
        "extensionFactor": 3, "extensionMinPct": 1.5, "extensionMaxPct": 3,
        "supportFactor": .5, "supportMinPct": .2, "supportMaxPct": .5},
    "minTailPoints": 5,
    "maxTailAgeMinutes": 2,
    "tailDropPct": -.5,
    "tailSellVolumePct": 60,
    "maxTailDrawdownPct": -1.2,
}
SETUP_LABELS = {"relative_strength_leader": "相对强势延续", "vwap_support": "回踩承接",
                "breakout_hold": "突破维持", "low_reversal_intraday": "日内转强"}


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def clamp(value, lo=0, hi=100):
    return max(lo, min(hi, value))


def stamp(row):
    value = str(row.get("bucketEnd") or row.get("timestamp") or row.get("datetime") or row.get("time") or "")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None) if moment.tzinfo else moment
    except ValueError:
        return None


def provenance(stock, source=""):
    existing = getattr(stock, "feature_provenance", None) or {}
    if existing:
        return existing
    return {key: {"source": source or "provider_unspecified", "asOfTime": getattr(stock, "quote_time", ""),
                   "method": method, "isProxy": True, "status": "proxy"}
            for key, method in {"volumeRatio": "provider_unverified_activity",
                                "amountFactor": "provider_unverified_amount_factor",
                                "relStrength": "provider_unverified_relative_strength"}.items()}


def quote_score(stock):
    """Amount enters once; change rank enters once. No proxy volume/turnover rewards."""
    liquidity = clamp((math.log10(max(1, number(stock.amount))) - 6) * 22)
    strength = clamp(number(stock.rel_strength))
    risk = clamp(len(stock.risk_flags) * 12 + max(0, number(stock.change) - 6) * 8
                 + max(0, number(stock.turnover) - 12) * 3
                 + (30 if stock.limit_status in {"limit_up", "one_word_limit_up"} else 0))
    return round(clamp(liquidity * .45 + strength * .55 - risk * .2), 4), {
        "liquidity": liquidity, "momentum": clamp(50 + number(stock.change) * 5),
        "activity": liquidity, "relativeStrength": strength, "risk": risk}


def structure(candidate):
    rows = sorted([r for r in candidate.get("series") or [] if stamp(r) and number(r.get("close")) > 0], key=stamp)
    if rows:
        latest_date = stamp(rows[-1]).date()
        rows = [r for r in rows if stamp(r).date() == latest_date and r.get("bucketComplete") is not False
                and (570 < stamp(r).hour * 60 + stamp(r).minute <= 690
                     or 780 < stamp(r).hour * 60 + stamp(r).minute <= 900)]
    result = {"version": VERSION, "status": "insufficient", "confirmedTypes": [], "reasons": [],
              "score": None, "vwap": None, "referenceHigh": None, "referenceLow": None}
    if len(rows) < PARAMETERS["minBars"] or candidate.get("klineSource") in {"simulated", "unavailable", ""}:
        result["reasons"] = ["真实带日期完整K线不足，不能确认交易形态"]
        return result
    last = number(rows[-1]["close"])
    reference = rows[:-2]  # two bars must hold the pre-existing level
    morning = [r for r in reference if stamp(r).hour < 12]
    reference = morning or reference
    ref_high = max(number(r.get("high"), number(r["close"])) for r in reference)
    ref_low = min(number(r.get("low"), number(r["close"])) for r in reference)
    true_ranges = [max(number(r.get("high"), number(r["close"])) - number(r.get("low"), number(r["close"])),
        abs(number(r.get("high"), number(r["close"])) - number(previous["close"])),
        abs(number(r.get("low"), number(r["close"])) - number(previous["close"])))
        / number(previous["close"]) * 100 for previous, r in zip(reference, reference[1:])]
    volatility = sum(true_ranges) / len(true_ranges) if true_ranges else 0
    thresholds = PARAMETERS["volatilityThresholds"]
    breakout_margin = clamp(volatility * thresholds["breakoutFactor"], thresholds["breakoutMinPct"], thresholds["breakoutMaxPct"])
    extension_limit = clamp(volatility * thresholds["extensionFactor"], thresholds["extensionMinPct"], thresholds["extensionMaxPct"])
    support_tolerance = clamp(volatility * thresholds["supportFactor"], thresholds["supportMinPct"], thresholds["supportMaxPct"])
    result.update(referenceVolatilityPct=round(volatility, 4), thresholds={"breakoutMarginPct": breakout_margin,
        "maxVwapExtensionPct": extension_limit, "supportTolerancePct": support_tolerance})
    volume = sum(max(0, number(r.get("volume"))) for r in rows)
    # Bar close*volume is an estimate, explicitly labelled, never order-flow evidence.
    vw = sum(number(r["close"]) * max(0, number(r.get("volume"))) for r in rows) / volume if volume else None
    result.update(vwap=round(vw, 4) if vw else None, vwapMethod="bar_close_volume_estimate" if vw else "unavailable",
                  referenceHigh=ref_high, referenceLow=ref_low, asOfTime=stamp(rows[-1]).isoformat(),
                  morningHigh=max((number(r.get("high"), number(r["close"])) for r in morning), default=None))
    if not vw:
        result["reasons"] = ["缺少真实成交量，不能确认成交均价承接"]
        return result
    extension = (last / vw - 1) * 100
    recent = rows[-3:]
    recent_low = min(number(r.get("low"), number(r["close"])) for r in recent)
    hold = min(number(r["close"]) for r in rows[-2:])
    types = []
    if hold > ref_high * (1 + breakout_margin / 100):
        types.append("breakout_hold")
    if (recent_low <= vw * (1 + support_tolerance / 100)
            and hold >= vw and number(rows[-1]["close"]) >= number(rows[-2]["close"])):
        types.append("vwap_support")
    earlier = rows[:-3]
    early_low = min(number(r.get("low"), number(r["close"])) for r in earlier)
    if (recent_low > early_low and number(earlier[0]["close"]) > min(number(r["close"]) for r in earlier)
            and hold >= vw and number(rows[-1]["close"]) > number(rows[-3]["close"])):
        types.append("low_reversal_intraday")
    if (number(candidate.get("relStrength")) >= 70 and number(candidate.get("change")) > 0
            and hold >= vw and number(rows[-1]["close"]) >= number(rows[-3]["close"]) * .998):
        types.append("relative_strength_leader")
    if extension > extension_limit:
        types = []
        result["reasons"].append("偏离成交均价过大，当前买点尚未确认")
    result.update(status="confirmed" if types else "invalid", confirmedTypes=types,
                  score=round(clamp(40 + (20 if hold >= vw else -20) + (15 if types else 0)
                                    + (10 if "breakout_hold" in types else 0) - max(0, extension - 2) * 5), 1),
                  vwapExtensionPct=round(extension, 2))
    result["detectors"] = {kind: {"confirmed": kind in types, "reference": "P1",
        "condition": condition, "invalidCondition": invalid}
        for kind, condition, invalid in (
            ("breakout_hold", "最近两根完整K线收盘维持在事前参考高点之上", "跌回事前参考高点以下"),
            ("vwap_support", "近期触及成交均价区域并收回，最近两根收盘不低于均价", "持续跌破均价且未收回"),
            ("low_reversal_intraday", "早段下探后低点抬高、收回均价且近期转强", "跌破转强后的支撑低点"),
            ("relative_strength_leader", "日内相对强度不低于70且结构保持、没有过度偏离", "强度衰减且跌破成交均价"))}
    evidence_strength = {
        "breakout_hold": clamp((hold / ref_high - 1) * 100 * 30 + 60),
        "vwap_support": clamp(80 - abs(recent_low / vw - 1) * 100 * 20),
        "low_reversal_intraday": clamp(60 + (recent_low / early_low - 1) * 100 * 15),
        "relative_strength_leader": clamp(number(candidate.get("relStrength")) - max(0, extension) * 5),
    }
    for kind, detector in result["detectors"].items():
        detector["evidenceStrength"] = round(evidence_strength[kind], 1) if kind in types else None
    result["confirmedTypes"] = sorted(types, key=lambda kind: (-evidence_strength[kind], kind))
    result["primarySelectionRule"] = "已成立形态按证据强度排序，不按标签声明顺序；不是收益概率"
    if not types and not result["reasons"]:
        result["reasons"] = ["未确认突破维持、回踩承接、日内转强或相对强势延续"]
    return result


def annotate(candidate, industry_score=None):
    """Replace T+1 scores only; keep the common engine's fields for legacy diagnostics."""
    item = dict(candidate)
    setup = structure(item)
    amount = number(item.get("amount"))
    liquidity = clamp((math.log10(max(1, amount)) - 6) * 22)
    relative = clamp(number(item.get("relStrength")))
    risk = number((item.get("t1Dimensions") or {}).get("overnightRisk"), 30)
    parts = {"liquidity": liquidity, "relativeStrength": relative, "structure": setup["score"],
             "industry": industry_score}
    weights = PARAMETERS["weights"]
    known = {k: v for k, v in parts.items() if v is not None}
    score = sum(v * weights[k] for k, v in known.items()) / sum(weights[k] for k in known)
    item.update(selectionPolicyVersion=VERSION, selectionScoreVersion=VERSION, setupEvidence=setup,
                setupStatus=setup["status"], setupEligible=setup["status"] == "confirmed",
                scoreComponents=parts, t1Score=round(clamp(score - risk * PARAMETERS["riskWeight"]), 1),
                formalEligible=False, formalExclusionReasons=["等待正式尾盘证据确认"])
    item["t1Dimensions"] = {**(item.get("t1Dimensions") or {}), "fundStrength": round(liquidity, 1),
                            "fundStrengthLabel": "成交活跃度评分，非资金净流入", "sectorStrength": industry_score,
                            "intradayStructure": setup["score"], "premiumPotentialScore": None,
                            "tomorrowPremium": None, "premiumPotentialLabel": "未校准收益期望，不提供收益概率"}
    item["legacyRuleScore"] = item.get("score")
    item["score"] = item["t1Score"]
    item["lastVwap"] = setup["vwap"]
    item["morningHigh"] = setup.get("morningHigh")
    item["morningLow"] = setup.get("referenceLow")
    item["status"] = "buy-triggered" if item["setupEligible"] else "watch"
    item["style"] = SETUP_LABELS.get((setup["confirmedTypes"] or [""])[0], "等待形态确认")
    item["candidateTypes"] = setup["confirmedTypes"] or ["unconfirmed_setup"]
    item["candidateType"] = item["candidateTypes"][0]
    item["candidateTypeLabel"] = SETUP_LABELS.get(item["candidateType"], "形态未确认")
    item["primaryCandidateType"] = item["candidateType"]
    return item


def formal_tail(candidate, tail, scan_time):
    """Use measured tail facts, not the shadow score or the shadow's promotion mode."""
    item = candidate
    pack = {k: v for k, v in tail.items() if k not in {"stage", "score", "status", "label", "version"}}
    reasons = []
    if not item.get("dataEligible"):
        reasons.append("数据质量不满足正式推荐")
    if not item.get("setupEligible"):
        reasons.extend((item.get("setupEvidence") or {}).get("reasons") or ["交易形态未成立"])
    insufficient = (number(tail.get("pointCount")) < PARAMETERS["minTailPoints"]
            or number(tail.get("recent5MinuteCount")) < 5
            or number(tail.get("recent5VolumePointCount")) < 5
            or tail.get("status") == "data_insufficient"
            or any(tail.get(key) is None for key in ("vwapDeviationPct", "change5mPct", "sellPressureVolumeRatio", "tailMaxDrawdownPct")))
    if insufficient:
        reasons.append("缺少真实尾盘价格与成交承接证据")
    def minutes(value):
        try:
            text = str(value).replace("T", " ").split()[-1]
            h, m = text[:5].split(":")
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError, IndexError):
            return -10000
    actual = str(tail.get("actualLastTime") or "")
    actual_stamp = stamp({"time": actual})
    expected_date = str(tail.get("tradeDate") or item.get("quoteTradeDate") or "")[:10]
    if (not actual_stamp or not expected_date or actual_stamp.date().isoformat() != expected_date
            or (item.get("quoteTradeDate") and str(item["quoteTradeDate"])[:10] != expected_date)
            or tail.get("source") in {None, "", "unavailable", "simulated"}):
        reasons.append("尾盘证据日期或真实来源不匹配")
        insufficient = True
    if not actual or not (0 <= minutes(scan_time[:5]) - minutes(actual) <= PARAMETERS["maxTailAgeMinutes"]):
        reasons.append("尾盘分钟证据缺失或超过时效")
        insufficient = True
    drop = number(tail.get("change5mPct"))
    sell = number(tail.get("sellPressureVolumeRatio"))
    recovered = (tail.get("recoveredVwap") is True and number(tail.get("vwapDeviationPct")) >= 0
                 and drop >= 0)
    weak = (drop <= PARAMETERS["tailDropPct"] and (sell >= PARAMETERS["tailSellVolumePct"]
            or number(tail.get("longestSellPressureCount")) >= 3))
    weak = weak or (number(tail.get("tailMaxDrawdownPct")) <= PARAMETERS["maxTailDrawdownPct"]
                    and number(tail.get("vwapDeviationPct")) < 0)
    if weak and not recovered:
        reasons.append("尾盘持续卖压或回撤且未恢复，不满足隔夜承接")
    if (item.get("tradeability") or {}).get("level") in {"not-buyable", "hard-to-sell", "hard-to-buy"}:
        reasons.append("当前交易约束不支持正式买入")
    status = "insufficient" if insufficient else "weak" if weak and not recovered else "supported"
    pack.update(refId="T1", version=VERSION, stage="formal", status=status,
                label={"insufficient": "尾盘证据不足", "weak": "尾盘走弱", "supported": "尾盘价格与成交承接已验证"}[status],
                actualLastTime=tail.get("actualLastTime"), allowedUse="正式终选证据；不是资金净流入或收益概率")
    tail_score = None if insufficient else round(clamp(60 + number(tail.get("tailChangePct")) * 8
        + number(tail.get("vwapDeviationPct")) * 5 - max(0, sell-50) * .4
        + min(0, number(tail.get("tailMaxDrawdownPct"))) * 8), 1)
    pack["supportScore"] = tail_score
    parts = dict(item.get("scoreComponents") or {})
    parts["tail"] = tail_score
    known = {k: v for k, v in parts.items() if v is not None and k in PARAMETERS["weights"]}
    if known:
        score = sum(v * PARAMETERS["weights"][k] for k, v in known.items()) / sum(PARAMETERS["weights"][k] for k in known)
        score = round(clamp(score - number((item.get("t1Dimensions") or {}).get("overnightRisk"), 30) * PARAMETERS["riskWeight"]), 1)
        item.update(score=score, t1Score=score, scoreComponents=parts)
    item.update(tailSupportPackV2=pack, formalEligible=not reasons,
                formalExclusionReasons=list(dict.fromkeys(reasons)))
    refs = [r for r in item.get("evidenceRefs") or [] if r.get("id") != "T1"]
    refs.append({"id": "T1", "type": "formal_tail_support", "status": "available" if status != "insufficient" else "unavailable",
                 "source": tail.get("source") or "unavailable", "asOfTime": actual,
                 "scanCutoff": f"{tail.get('tradeDate')}T{scan_time[:5]}:00" if tail.get("tradeDate") else "",
                 "version": VERSION,
                 "summary": f"{pack['label']}；最近5分钟{tail.get('change5mPct')}%；下跌成交占比{tail.get('sellPressureVolumeRatio')}%；均价偏离{tail.get('vwapDeviationPct')}%；不是资金净流入。"})
    item["evidenceRefs"] = refs
    return item
