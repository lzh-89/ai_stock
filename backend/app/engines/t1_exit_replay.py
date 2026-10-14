from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional


ADAPTIVE_EXIT_VERSION = "adaptive_exit_shadow_v1"
EXECUTION_PROFILE_VERSION = "execution_profile_v2_participation"
MINUTE_SEMANTICS_VERSION = "minute_bar_semantics_v1"


def normalize_minute_bars(
    series: Iterable[Dict[str, Any]], trade_date: str, expected_period_minutes: int = 1
) -> Dict[str, Any]:
    rows = []
    errors = []
    for raw in series or []:
        item = dict(raw or {})
        period = int(item.get("_bucketPeriodMinutes") or expected_period_minutes or 1)
        if period != expected_period_minutes:
            errors.append("period_not_1m")
            continue
        bar_start = _parse_datetime(item.get("barStart"), trade_date)
        bar_end = _parse_datetime(item.get("barEnd"), trade_date)
        label = str(item.get("timeLabel") or "").strip().lower()
        raw_time = _parse_datetime(item.get("time") or item.get("datetime") or item.get("date"), trade_date)
        if bar_start and not bar_end:
            bar_end = bar_start + timedelta(minutes=period)
        elif bar_end and not bar_start:
            bar_start = bar_end - timedelta(minutes=period)
        elif not bar_start and not bar_end and raw_time:
            if label == "end":
                bar_end = raw_time
                bar_start = bar_end - timedelta(minutes=period)
            elif label == "start":
                bar_start = raw_time
                bar_end = bar_start + timedelta(minutes=period)
            else:
                errors.append("minute_semantics_unknown")
                continue
        if not bar_start or not bar_end or bar_end <= bar_start:
            errors.append("minute_semantics_unknown")
            continue
        if trade_date and bar_end.date().isoformat() != trade_date:
            continue
        item["barStart"] = bar_start.isoformat(timespec="seconds")
        item["barEnd"] = bar_end.isoformat(timespec="seconds")
        item["observedAt"] = str(item.get("observedAt") or item["barEnd"])
        item["minuteBarSemantics"] = MINUTE_SEMANTICS_VERSION
        rows.append(item)
    rows.sort(key=lambda row: row["barEnd"])
    status = "ok" if rows and not errors else "partial" if rows else "minute_semantics_unknown"
    return {
        "version": MINUTE_SEMANTICS_VERSION,
        "status": status,
        "bars": rows,
        "errors": sorted(set(errors)),
        "pointCount": len(rows),
    }


def simulate_entry_execution(
    *,
    series: Iterable[Dict[str, Any]],
    recommendation_date: str,
    signal_published_at: str,
    reference_price: Optional[float],
    profile: Dict[str, Any],
    board_lot: int = 100,
) -> Dict[str, Any]:
    normalized = normalize_minute_bars(series, recommendation_date)
    signal_at = _parse_datetime(signal_published_at, recommendation_date)
    if normalized["status"] != "ok" or not signal_at:
        return _execution_unavailable("minute_semantics_unknown", normalized, signal_published_at)
    submitted_at = signal_at + timedelta(seconds=int(profile.get("orderLatencySeconds") or 5))
    reference = _number(reference_price)
    if not reference or reference <= 0:
        return _execution_unavailable("reference_price_missing", normalized, signal_published_at)
    lot = max(1, int(board_lot or 100))
    notional = float(profile.get("slotNotionalCny") or 100000 / 3)
    target_quantity = math.floor(notional / reference / lot) * lot
    if target_quantity <= 0:
        return _execution_unavailable("notional_below_board_lot", normalized, signal_published_at)
    fills = _fill_order(
        normalized["bars"], submitted_at, target_quantity, "buy", profile, lot, deadline="15:00:00"
    )
    return _entry_result(
        fills=fills,
        target_quantity=target_quantity,
        profile=profile,
        signal_published_at=signal_published_at,
        submitted_at=submitted_at,
        normalized=normalized,
        reference_price=reference,
    )


def replay_adaptive_exit(
    *,
    code: str,
    industry_group: str,
    next_trade_date: str,
    minute_series: Iterable[Dict[str, Any]],
    entry_execution: Dict[str, Any],
    opening_evidence: Optional[Dict[str, Any]],
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    base = {
        "code": code,
        "validationVersion": ADAPTIVE_EXIT_VERSION,
        "scenarioKey": ADAPTIVE_EXIT_VERSION,
        "scenarioLabel": "动态退出影子 v1",
        "stage": "shadow",
        "affectsOfficialRecommendation": False,
        "executionProfileVersion": profile.get("version") or EXECUTION_PROFILE_VERSION,
    }
    if not entry_execution.get("executable") or entry_execution.get("status") != "executed_from_minute":
        return {**base, "dataStatus": "entry_not_executable", "status": "unavailable", "netReturnPct": None,
                "executionStatus": "entry_not_executable", "reason": "买入未完整成交，动态退出不外推收益。"}
    normalized = normalize_minute_bars(minute_series, next_trade_date)
    if normalized["status"] != "ok":
        return {**base, "dataStatus": "minute_semantics_unknown", "status": "unavailable", "netReturnPct": None,
                "executionStatus": "data_missing", "minuteBarSemantics": normalized}
    bars = normalized["bars"]
    first_open = next((bar for bar in bars if _clock(bar["barEnd"]) <= "09:31:00" and _bar_tradeable(bar)), None)
    if not first_open:
        return {**base, "dataStatus": "next_open_unavailable", "status": "unavailable", "netReturnPct": None,
                "executionStatus": "data_hold", "minuteBarSemantics": normalized,
                "reason": "09:31 前没有可确认的首笔成交，不能事后回填开盘价。"}
    open_price, open_price_source = _bar_price(first_open)
    entry_price = _number(entry_execution.get("price"))
    if not open_price or not entry_price:
        return {**base, "dataStatus": "price_missing", "status": "unavailable", "netReturnPct": None,
                "executionStatus": "data_missing", "minuteBarSemantics": normalized}
    open_return = (open_price / entry_price - 1) * 100
    evidence = _opening_risk_evaluation(opening_evidence or {}, industry_group, next_trade_date)
    decision = _exit_decision(bars, open_return, evidence, next_trade_date)
    quantity = int(entry_execution.get("filledQuantity") or 0)
    if quantity <= 0:
        return {**base, "dataStatus": "entry_not_executable", "status": "unavailable", "netReturnPct": None,
                "executionStatus": "entry_not_executable"}

    legs = []
    remaining = quantity
    for index, instruction in enumerate(decision["legs"], start=1):
        target = remaining if index == len(decision["legs"]) else _round_lot(quantity * float(instruction["weight"]), int(entry_execution.get("boardLot") or 100))
        target = min(remaining, target)
        decision_at = datetime.fromisoformat(f"{next_trade_date}T{instruction['decisionTime']}:00")
        submitted_at = decision_at + timedelta(seconds=int(profile.get("orderLatencySeconds") or 5))
        fills = _fill_order(bars, submitted_at, target, "sell", profile, int(entry_execution.get("boardLot") or 100), deadline="11:30:00")
        filled = sum(int(fill["quantity"]) for fill in fills)
        remaining -= filled
        legs.append(_sell_leg(index, instruction, target, fills, profile))

    sell_proceeds = sum(float(leg.get("netProceedsCny") or 0) for leg in legs)
    buy_cost = float(entry_execution.get("totalCostCny") or 0)
    last_price = _last_price(bars)
    mark_value = remaining * last_price if last_price else 0.0
    mark_to_market = sell_proceeds + mark_value
    full = remaining == 0 and all(leg.get("executionStatus") == "executed" for leg in legs)
    execution_status = "executed" if full else "partial_execution" if sell_proceeds > 0 else "market_unexecutable"
    net_return = (sell_proceeds / buy_cost - 1) * 100 if full and buy_cost > 0 else None
    mark_return = (mark_to_market / buy_cost - 1) * 100 if buy_cost > 0 and last_price else None
    sell_notional = sum(float(leg.get("grossProceedsCny") or 0) for leg in legs)
    average_sell = sell_notional / (quantity - remaining) if quantity > remaining else None
    return {
        **base,
        "dataStatus": "ok" if full else execution_status,
        "status": "final" if full else "incomplete",
        "validationStatus": "final" if full else "incomplete",
        "executionStatus": execution_status,
        "branch": decision["branch"],
        "decisionReason": decision["reason"],
        "entryReturnAtOpenPct": round(open_return, 4),
        "nextOpenPrice": round(open_price, 4),
        "nextOpenObservedAt": first_open["observedAt"],
        "nextOpenPriceSource": open_price_source,
        "openingRiskEvidence": evidence,
        "minuteBarSemantics": normalized,
        "entryPrice": entry_price,
        "sellPrice": round(average_sell, 4) if average_sell else None,
        "sellTime": "+".join(str(leg.get("lastFillTime") or "") for leg in legs),
        "grossReturnPct": round((sell_notional / float(entry_execution.get("grossNotionalCny") or buy_cost) - 1) * 100, 4) if full and buy_cost else None,
        "netReturnPct": round(net_return, 4) if net_return is not None else None,
        "markToMarketReturnPct": round(mark_return, 4) if mark_return is not None else None,
        "targetQuantity": quantity,
        "filledQuantity": quantity - remaining,
        "remainingQuantity": remaining,
        "legs": legs,
        "costModel": _cost_model(profile),
    }


def _exit_decision(bars: List[Dict[str, Any]], open_return: float, evidence: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    if open_return <= -1 and evidence.get("anyRiskConditionTriggered"):
        return {"branch": "opening_risk_exit", "reason": "低开且 09:31 风险条件触发", "legs": [{"weight": 1.0, "decisionTime": "09:31"}]}
    if open_return >= 2:
        return {"branch": "strong_open_split_exit", "reason": "开盘收益达到 2%，分两腿止盈", "legs": [
            {"weight": 0.5, "decisionTime": "09:31"}, {"weight": 0.5, "decisionTime": "09:45"}
        ]}
    if -1 < open_return < 2:
        before = [bar for bar in bars if _clock(bar["barEnd"]) < "09:35:00"]
        decision_bar = next((bar for bar in bars if _clock(bar["barEnd"]) == "09:35:00"), None)
        lows = [_number(bar.get("low")) for bar in before]
        lows = [value for value in lows if value is not None]
        decision_price = _number((decision_bar or {}).get("close"))
        vwap = _window_vwap([bar for bar in bars if _clock(bar["barEnd"]) <= "09:35:00"])
        if decision_bar and lows and decision_price is not None and vwap is not None and decision_price < min(lows) and decision_price < vwap:
            return {"branch": "breakdown_exit", "reason": "09:35 跌破此前低点且低于 VWAP", "legs": [{"weight": 1.0, "decisionTime": "09:35"}],
                    "preDecisionLow": min(lows), "decisionPrice09_35": decision_price, "vwapThrough09_35": vwap}
    reason = "低开但早盘风险证据不可用，按 09:45 基准退出" if open_return <= -1 and not evidence.get("anyConditionAvailable") else "未触发提前退出"
    return {"branch": "fixed_0945_exit", "reason": reason, "legs": [{"weight": 1.0, "decisionTime": "09:45"}]}


def _opening_risk_evaluation(evidence: Dict[str, Any], industry_group: str, trade_date: str) -> Dict[str, Any]:
    published = _parse_datetime(evidence.get("snapshotPublishedAt"), trade_date)
    deadline = datetime.fromisoformat(f"{trade_date}T09:31:59") if trade_date else None
    eligible = bool(evidence.get("decisionEligible") and published and deadline and published <= deadline)
    sector = ((evidence.get("sectorReturnsByIndustry") or {}).get(industry_group) or {})
    conditions = [
        _risk_condition("market_breadth", evidence.get("downRatioPct"), 60, ">=", eligible and evidence.get("marketCoveragePct", 0) >= 90),
        _risk_condition("main_indices", evidence.get("mainIndexWorstChangePct"), -0.5, "<=", eligible and evidence.get("mainIndexStatus") == "available"),
        _risk_condition("industry", sector.get("returnPct"), -0.8, "<=", eligible and sector.get("coveragePct", 0) >= 90),
    ]
    return {
        "version": str(evidence.get("version") or "opening_risk_snapshot_v1"),
        "decisionEligible": eligible,
        "snapshotPublishedAt": evidence.get("snapshotPublishedAt") or "",
        "conditions": conditions,
        "anyConditionAvailable": any(item["available"] for item in conditions),
        "anyRiskConditionTriggered": any(item["available"] and item["triggered"] for item in conditions),
        "status": "available" if eligible and any(item["available"] for item in conditions) else "opening_risk_evidence_unavailable",
    }


def _risk_condition(key: str, value: Any, threshold: float, operator: str, available: bool) -> Dict[str, Any]:
    number = _number(value)
    available = bool(available and number is not None)
    triggered = available and (number >= threshold if operator == ">=" else number <= threshold)
    return {"key": key, "available": available, "value": number, "threshold": threshold, "operator": operator, "triggered": triggered}


def _fill_order(bars: List[Dict[str, Any]], submitted_at: datetime, quantity: int, side: str, profile: Dict[str, Any], lot: int, deadline: str) -> List[Dict[str, Any]]:
    remaining = quantity
    fills = []
    rate = float(profile.get("maxParticipationRate") or 0.01)
    for bar in bars:
        start = datetime.fromisoformat(bar["barStart"])
        if start < submitted_at or _clock(bar["barEnd"]) > deadline or not _bar_tradeable(bar):
            continue
        volume = _volume_in_shares(bar)
        if volume is None or volume <= 0:
            continue
        capacity = math.floor(volume * rate / lot) * lot
        fill_quantity = min(remaining, capacity)
        if fill_quantity <= 0:
            continue
        raw_price, source = _bar_price(bar)
        if not raw_price:
            continue
        slippage = float(profile.get("buySlippagePct") if side == "buy" else profile.get("sellSlippagePct") or 0) / 100
        price = raw_price * (1 + slippage if side == "buy" else 1 - slippage)
        fills.append({"quantity": fill_quantity, "price": round(price, 6), "rawPrice": round(raw_price, 6),
                      "priceSource": source, "barStart": bar["barStart"], "barEnd": bar["barEnd"]})
        remaining -= fill_quantity
        if remaining <= 0:
            break
    return fills


def _entry_result(*, fills: List[Dict[str, Any]], target_quantity: int, profile: Dict[str, Any], signal_published_at: str,
                  submitted_at: datetime, normalized: Dict[str, Any], reference_price: float) -> Dict[str, Any]:
    filled = sum(int(item["quantity"]) for item in fills)
    notional = sum(int(item["quantity"]) * float(item["price"]) for item in fills)
    commission = max(float(profile.get("minimumCommissionCny") or 5), notional * float(profile.get("commissionPct") or 0) / 100) if filled else 0
    transfer = notional * float(profile.get("transferFeePct") or 0) / 100
    complete = filled == target_quantity
    return {
        "version": EXECUTION_PROFILE_VERSION,
        "executable": complete,
        "status": "executed_from_minute" if complete else "partial_execution" if filled else "entry_not_executable",
        "label": "信号发布后完整成交" if complete else "信号发布后未完整成交",
        "signalPublishedAt": signal_published_at,
        "orderSubmittedAt": submitted_at.isoformat(timespec="seconds"),
        "entryTime": fills[-1]["barEnd"] if fills else "",
        "price": round(notional / filled, 6) if filled else None,
        "buyReferencePrice": reference_price,
        "targetQuantity": target_quantity,
        "filledQuantity": filled,
        "remainingQuantity": target_quantity - filled,
        "boardLot": int(profile.get("boardLot") or 100),
        "grossNotionalCny": round(notional, 4),
        "commissionCny": round(commission, 4),
        "transferFeeCny": round(transfer, 4),
        "totalCostCny": round(notional + commission + transfer, 4),
        "fills": fills,
        "source": "recommendation_minute_replay",
        "minuteBarSemantics": normalized,
    }


def _sell_leg(index: int, instruction: Dict[str, Any], target: int, fills: List[Dict[str, Any]], profile: Dict[str, Any]) -> Dict[str, Any]:
    filled = sum(int(item["quantity"]) for item in fills)
    gross = sum(int(item["quantity"]) * float(item["price"]) for item in fills)
    commission = max(float(profile.get("minimumCommissionCny") or 5), gross * float(profile.get("commissionPct") or 0) / 100) if filled else 0
    stamp = gross * float(profile.get("stampTaxPct") or 0) / 100
    transfer = gross * float(profile.get("transferFeePct") or 0) / 100
    return {"leg": index, "weight": instruction["weight"], "decisionTime": instruction["decisionTime"],
            "targetQuantity": target, "filledQuantity": filled, "remainingQuantity": target - filled,
            "executionStatus": "executed" if filled == target else "partial_execution" if filled else "market_unexecutable",
            "grossProceedsCny": round(gross, 4), "netProceedsCny": round(gross - commission - stamp - transfer, 4),
            "commissionCny": round(commission, 4), "stampTaxCny": round(stamp, 4), "transferFeeCny": round(transfer, 4),
            "lastFillTime": fills[-1]["barEnd"] if fills else "", "fills": fills}


def _execution_unavailable(status: str, normalized: Dict[str, Any], signal_published_at: str) -> Dict[str, Any]:
    return {"version": EXECUTION_PROFILE_VERSION, "executable": False, "status": status, "signalPublishedAt": signal_published_at,
            "price": None, "minuteBarSemantics": normalized, "reason": "信号后分钟成交证据不足。"}


def _bar_price(bar: Dict[str, Any]) -> tuple[Optional[float], str]:
    amount = _number(bar.get("amount"))
    volume = _volume_in_shares(bar)
    if amount and volume and amount > 0 and volume > 0:
        value = amount / volume
        close = _number(bar.get("close"))
        if close and 0.5 * close <= value <= 1.5 * close:
            return value, "minute_vwap"
    return _number(bar.get("close")), "minute_close_fallback"


def _window_vwap(bars: List[Dict[str, Any]]) -> Optional[float]:
    usable = [(_number(bar.get("amount")), _volume_in_shares(bar)) for bar in bars]
    amount = sum(float(item[0]) for item in usable if item[0] and item[1])
    volume = sum(float(item[1]) for item in usable if item[0] and item[1])
    return amount / volume if amount > 0 and volume > 0 else None


def _bar_tradeable(bar: Dict[str, Any]) -> bool:
    price, _ = _bar_price(bar)
    volume = _volume_in_shares(bar)
    return bool(price and price > 0 and volume and volume > 0 and not bar.get("suspended"))


def _volume_in_shares(bar: Dict[str, Any]) -> Optional[float]:
    volume = _number(bar.get("volume"))
    if volume is None or volume <= 0:
        return None
    unit = str(bar.get("volumeUnit") or "").strip().lower()
    if unit in {"lot", "lots", "hand", "hands", "手"}:
        return volume * 100
    if unit in {"share", "shares", "股"}:
        return volume
    amount = _number(bar.get("amount"))
    close = _number(bar.get("close"))
    if amount and close and close > 0:
        direct = amount / volume
        per_lot = amount / (volume * 100)
        if 0.5 * close <= per_lot <= 1.5 * close and not (0.5 * close <= direct <= 1.5 * close):
            return volume * 100
    return volume


def _last_price(bars: List[Dict[str, Any]]) -> Optional[float]:
    for bar in reversed(bars):
        price = _number(bar.get("close"))
        if price and price > 0:
            return price
    return None


def _cost_model(profile: Dict[str, Any]) -> Dict[str, Any]:
    return {key: profile.get(key) for key in ("buySlippagePct", "sellSlippagePct", "commissionPct", "minimumCommissionCny",
                                               "stampTaxPct", "transferFeePct", "maxParticipationRate", "slotNotionalCny", "orderLatencySeconds")}


def _round_lot(value: float, lot: int) -> int:
    return max(0, math.floor(value / max(1, lot)) * max(1, lot))


def _clock(value: str) -> str:
    text = str(value or "")
    return text[11:19] if len(text) >= 19 else text[:8]


def _parse_datetime(value: Any, fallback_date: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    if len(text) >= 5 and text[2] == ":" and fallback_date:
        try:
            return datetime.fromisoformat(f"{fallback_date}T{text}")
        except ValueError:
            return None
    return None


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
