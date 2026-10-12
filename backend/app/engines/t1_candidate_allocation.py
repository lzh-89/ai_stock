"""Deterministic, quote-only recall and two-round validation allocation.

This module never manufactures minute indicators and performs no I/O.
"""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Any

from backend.app.engines.filter_engine import tradeability

VERSION = "2026-10-09.stratified.v1"
GROUPS = ("core", "single", "boundary", "coverage", "explore")
ROUND_QUOTAS = ({"core": 45, "single": 15, "boundary": 10, "coverage": 7, "explore": 3},
                {"core": 10, "single": 5, "boundary": 10, "coverage": 8, "explore": 7})


def preliminary_candidate(stock: Any, identity: dict | None = None) -> dict:
    """Only provider quote fields; board labels are not industry identities."""
    identity = identity or {}
    industry = identity.get("industryName") or identity.get("industry") or ""
    if not isinstance(industry, str):
        industry = ""
    amount = max(0, stock.amount)
    liquidity = max(0, min(100, math.log10(max(1, amount)) * 12 - 25))
    momentum = max(0, min(100, 50 + stock.change * 5))
    activity = min(100, stock.volume_ratio * 20 + stock.turnover * 3)
    strength = max(0, min(100, stock.rel_strength))
    risk = min(100, len(stock.risk_flags) * 12 + max(0, stock.change - 6) * 8
               + max(0, stock.turnover - 12) * 3 + (30 if stock.limit_status in {"limit_up", "one_word_limit_up"} else 0))
    score = round(max(0, liquidity * .25 + momentum * .25 + activity * .2 + strength * .3 - risk * .2), 4)
    types = []
    if strength >= 80:
        types.append("relative_strength_leader")
    if stock.volume_ratio >= 1.6 or stock.amount_factor >= 1.55:
        types.append("high_volume_attention")
    if stock.change >= 7:
        types.append("limit_up_candidate")
    if 0 <= stock.change <= 4 and activity >= 40:
        types.append("low_reversal_intraday")
    if risk >= 45:
        types.append("risk_high_but_active")
    types = types or ["quote_momentum"]
    return {
        "code": stock.code, "name": stock.name, "price": stock.price, "amount": amount,
        "sector": stock.sector, "industry": industry, "theme": stock.theme,
        "change": stock.change, "turnover": stock.turnover, "volumeRatio": stock.volume_ratio,
        "amountFactor": stock.amount_factor, "relStrength": stock.rel_strength,
        "riskFlags": list(stock.risk_flags), "limitStatus": stock.limit_status,
        "tradeability": tradeability(stock), "score": score, "preliminaryScore": score,
        "candidateTypes": types, "candidateType": types[0], "primaryCandidateType": types[0],
        "recallReasons": types, "cheapFeatures": {"liquidity": liquidity, "momentum": momentum,
            "activity": activity, "relativeStrength": strength, "risk": risk},
        "klineSource": "unavailable", "klinePeriod": "15m", "series": [],
        "minuteIndicators": "unavailable", "status": "watch", "style": "待真实15m验证",
        "priceBasis": "unadjusted", "quoteAsOfTime": getattr(stock, "quote_time", ""),
        "quoteTradeDate": getattr(stock, "trade_date", ""),
        "allocationVersion": VERSION,
    }


def build_preliminary_pool(candidates: list[dict], limit: int = 500) -> list[dict]:
    if limit <= 0:
        return []
    ranked = sorted(candidates, key=lambda x: (-x["preliminaryScore"], -x.get("amount", 0), x["code"]))
    # Multiple quote-derived entries preserve broad recall without claiming VWAP evidence.
    buckets = [[x for x in ranked if kind in x["candidateTypes"]]
               for kind in sorted({kind for x in ranked for kind in x["candidateTypes"]})]
    buckets.append(ranked)
    selected, seen = [], set()
    for i in range(len(ranked)):
        for bucket in buckets:
            if i < len(bucket) and bucket[i]["code"] not in seen:
                seen.add(bucket[i]["code"])
                selected.append(dict(bucket[i], recallRank=len(selected) + 1))
                if len(selected) >= min(500, limit):
                    break
        if len(selected) >= min(500, limit):
            break
    selected.sort(key=lambda x: (-x["preliminaryScore"], -x.get("amount", 0), x["code"]))
    for i, item in enumerate(selected, 1):
        item["preliminaryScoreRank"] = i
    return selected


def allocate_round(candidates: list[dict], round_number: int, seed: str,
                   previous: list[dict] | None = None, evidence: dict | None = None,
                   budget: int = 120) -> list[dict]:
    previous, evidence = previous or [], evidence or {}
    used = {x["code"] for x in previous}
    remaining = [x for x in candidates if x["code"] not in used and x.get("price", 0) > 0]
    quota = ROUND_QUOTAS[round_number - 1]
    max_count = max(0, min(sum(quota.values()), budget - len(used)))
    industries = Counter(x.get("industry") for x in previous if x.get("industry"))
    types = Counter(x.get("primaryCandidateType") for x in previous)
    risky = sum(x["cheapFeatures"]["risk"] >= 45 for x in previous)
    # Round two targets underrepresented industries and disagreement, using only round-one evidence.
    failed_industries = Counter(x.get("industry") for x in previous
        if not (evidence.get(x["code"]) or {}).get("qualityEligible", bool((evidence.get(x["code"]) or {}).get("series"))))
    industry_scores, disagreement = {}, Counter()
    for item in previous:
        observed = evidence.get(item["code"]) or {}
        if observed.get("qualityEligible"):
            industry_scores.setdefault(item.get("industry"), []).append(observed.get("verifiedScore", 0))
        disagreement[item.get("industry")] += abs(observed.get("scoreDisagreement", 0))
    industry_strength = {key: sum(values) / len(values) for key, values in industry_scores.items()}
    hash_key = lambda x: hashlib.sha256(f"{seed}:{x['code']}".encode()).hexdigest()
    queues = {
        "core": sorted(remaining, key=lambda x: (failed_industries[x.get("industry")] if round_number == 2 else 0,
                                                  -industry_strength.get(x.get("industry"), 0) if round_number == 2 else 0,
                                                  x["preliminaryScoreRank"])),
        "single": sorted(remaining, key=lambda x: (-max(x["cheapFeatures"][k] for k in
                           ("momentum", "activity", "relativeStrength")), x["preliminaryScoreRank"])),
        "boundary": sorted(remaining, key=lambda x: (-disagreement[x.get("industry")] if round_number == 2 else 0,
                                                      -abs(x["cheapFeatures"]["momentum"] - x["cheapFeatures"]["relativeStrength"]),
                                                      abs(x["preliminaryScoreRank"] - 120), x["code"])),
        "coverage": sorted(remaining, key=lambda x: (not bool(x.get("industry")), industries[x.get("industry")],
                                                       x["preliminaryScoreRank"])),
        "explore": [],
    }
    # Freeze ten deterministic exploration positions across four rank bands (4/3/2/1).
    exploration = []
    for lo, hi, count in ((121, 200, 4), (201, 300, 3), (301, 400, 2), (401, 500, 1)):
        exploration.extend(sorted([x for x in candidates if lo <= x["preliminaryScoreRank"] <= hi], key=hash_key)[:count])
    reserved = {x["code"] for x in exploration}
    queues["explore"] = [x for x in exploration if x["code"] not in used]
    for group in GROUPS[:-1]:
        queues[group] = [x for x in queues[group] if x["code"] not in reserved]
    allocated = []
    for group in GROUPS:
        queue = queues[group]
        if group == "coverage":
            queue = sorted(queue, key=lambda x: (not bool(x.get("industry")), industries[x.get("industry")], x["preliminaryScoreRank"]))
        target = min(quota[group], max_count - len(allocated))
        for relax in (False, True):
            for item in queue:
                if target <= 0:
                    break
                if item["code"] in used:
                    continue
                industry = item.get("industry")
                kind = item["primaryCandidateType"]
                high_risk = item["cheapFeatures"]["risk"] >= 45
                violations = []
                if industry and industries[industry] >= max(1, math.ceil(budget * .15)):
                    violations.append("industry15%")
                if types[kind] >= max(1, math.ceil(budget * .30)):
                    violations.append("candidateType30%")
                if high_risk and risky >= max(1, math.floor(budget * .10)):
                    violations.append("highRisk10%")
                if violations and not relax:
                    continue
                result = dict(item, primaryGroup=group, groupTags=[group], allocationRound=round_number,
                              allocationPriority=len(previous) + len(allocated) + 1,
                              allocationReason=f"{group}:round{round_number}" + (":real_score_gate_feedback" if round_number == 2 else ""),
                              relaxationReason=",".join(violations), minute15mStatus="planned")
                allocated.append(result)
                used.add(item["code"])
                if industry:
                    industries[industry] += 1
                types[kind] += 1
                risky += high_risk
                target -= 1
        # Small pools may have no exploration bands. Explicitly logged deterministic substitution.
        if target > 0:
            for item in remaining:
                if target <= 0:
                    break
                if item["code"] in used or (group != "explore" and item["code"] in reserved):
                    continue
                allocated.append(dict(item, primaryGroup=group, groupTags=[group], allocationRound=round_number,
                    allocationReason=f"{group}:shortage_substitution", relaxationReason="group_shortage",
                    allocationPriority=len(previous) + len(allocated) + 1, minute15mStatus="planned"))
                used.add(item["code"])
                if item.get("industry"):
                    industries[item["industry"]] += 1
                types[item["primaryCandidateType"]] += 1
                risky += item["cheapFeatures"]["risk"] >= 45
                target -= 1
    return allocated


def clip_minute_series(series: list[dict], trade_date: str, scan_time: str, period: str) -> list[dict]:
    """Retain only dated, completed buckets; unknown labels use conservative start semantics."""
    from datetime import datetime, timedelta
    cutoff = datetime.fromisoformat(f"{trade_date}T{scan_time[:5]}:00")
    minutes = int(period.removesuffix("m"))
    result = []
    for point in series:
        if point.get("bucketComplete") is False:
            continue
        stamp = str(point.get("timestamp") or point.get("datetime") or point.get("time") or point.get("date") or "")
        try:
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).replace(tzinfo=None)
            end_text = point.get("bucketEnd")
            end = datetime.fromisoformat(end_text) if end_text else moment if point.get("timeLabel") == "end" else moment + timedelta(minutes=minutes)
            completed = moment.date().isoformat() == trade_date and end <= cutoff
        except ValueError:
            continue
        if completed:
            result.append(point)
    return result


def select_tail_codes(pool: list[dict], semifinal: list[dict], limit: int = 30) -> list[str]:
    if limit <= 0:
        return []
    ranked = sorted(pool, key=lambda x: (-float(x.get("t1Score") or 0), x["code"]))
    semif = {x["code"] for x in semifinal}
    queues = [([x for x in ranked if x["code"] in semif] + ranked, 15),
              (sorted(ranked, key=lambda x: (-abs((x.get("preliminaryScoreRank") or 500) - (x.get("verifiedRank") or 500)), x["code"])), 5),
              (sorted(ranked, key=lambda x: (abs(((x.get("t1Dimensions") or {}).get("intradayStructure") or 0) - 50), x["code"])), 5),
              (sorted(ranked, key=lambda x: (x.get("industry", ""), x["code"])), 3),
              ([x for x in ranked if x.get("primaryGroup") == "explore"], 2)]
    result = []
    for queue, quota in queues:
        count = 0
        for item in queue:
            if item["code"] in result:
                continue
            result.append(item["code"])
            count += 1
            if count >= quota or len(result) >= limit:
                break
        if len(result) >= limit:
            break
    return result
