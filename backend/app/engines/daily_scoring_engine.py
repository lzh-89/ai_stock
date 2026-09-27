from __future__ import annotations

from typing import Any, Dict, List

from backend.app.engines.indicator_engine import macd, moving_average
from backend.app.providers.provider_common import median
from backend.app.schema import Catalyst, RawStock


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def score_daily_stock(
    stock: RawStock,
    series: List[Dict[str, float]],
    risk_mode: str = "balanced",
    kline_source: str = "baostock_daily",
) -> Dict[str, Any]:
    usable = series[-120:]
    closes = [item["close"] for item in usable]
    ma5 = moving_average(usable, 5)
    ma10 = moving_average(usable, 10)
    ma20 = moving_average(usable, 20)
    ma60 = moving_average(usable, 60)
    macd_series = macd(usable)
    last = usable[-1]
    prev = usable[-2] if len(usable) >= 2 else usable[-1]
    last_ma5 = ma5[-1]
    last_ma10 = ma10[-1]
    last_ma20 = ma20[-1]
    last_ma60 = ma60[-1]
    prev_ma20 = ma20[-6] if len(ma20) >= 6 else ma20[0]
    prev_ma60 = ma60[-10] if len(ma60) >= 10 else ma60[0]
    last_macd = macd_series[-1]
    prev_macd = macd_series[-2] if len(macd_series) >= 2 else last_macd

    high_20 = max(item["high"] for item in usable[-21:-1]) if len(usable) >= 21 else max(item["high"] for item in usable)
    high_60 = max(item["high"] for item in usable[-61:-1]) if len(usable) >= 61 else max(item["high"] for item in usable)
    return_5d = _period_return(closes, 5)
    return_10d = _period_return(closes, 10)
    return_20d = _period_return(closes, 20)
    distance_ma20 = (last["close"] / last_ma20 - 1) * 100 if last_ma20 else 0
    distance_ma60 = (last["close"] / last_ma60 - 1) * 100 if last_ma60 else 0
    amount_ratio_5d = _last_ratio(usable, "amount", 5)
    amount_ratio_20d = _last_ratio(usable, "amount", 20)

    above_ma20 = last["close"] >= last_ma20
    above_ma60 = last["close"] >= last_ma60
    ma_bull = last_ma5 >= last_ma10 >= last_ma20
    long_trend_ok = last_ma20 >= last_ma60 and last_ma20 >= prev_ma20
    macd_up = last_macd["dif"] >= last_macd["dea"] and last_macd["hist"] >= prev_macd["hist"]
    breakout_20d = last["close"] >= high_20 * 0.995
    pullback_ma20 = above_ma60 and last_ma20 * 0.98 <= last["close"] <= last_ma20 * 1.05 and last_ma20 >= prev_ma20
    below_long_trend = last["close"] < last_ma60 and last_ma20 < prev_ma20
    bear_stack = last_ma5 < last_ma10 < last_ma20 and last["close"] < last_ma20
    high_stall = amount_ratio_5d >= 1.5 and abs(last.get("change", stock.change)) <= 1.2 and last["close"] >= high_60 * 0.94
    extended = distance_ma20 >= 18
    surge_high = return_20d >= 25 and distance_ma20 >= 12

    risk_flags = list(stock.risk_flags)
    if below_long_trend:
        risk_flags.append("日线跌破 MA60 且 MA20 走弱")
    if bear_stack:
        risk_flags.append("日线均线空头排列")
    if high_stall:
        risk_flags.append("前期高位放量滞涨")
    if extended:
        risk_flags.append("远离 MA20，追高风险")
    if surge_high:
        risk_flags.append("连续大涨后高位震荡风险")

    trend_score = clamp(
        (6 if above_ma20 else 0)
        + (5 if above_ma60 else 0)
        + (6 if ma_bull else 0)
        + (5 if long_trend_ok else 0)
        + (4 if macd_up else 0)
        + (4 if breakout_20d or pullback_ma20 else 0),
        0,
        30,
    )
    position_score = clamp(
        20
        - (8 if below_long_trend else 0)
        - (6 if bear_stack else 0)
        - (5 if extended else 0)
        - (5 if high_stall else 0)
        - (5 if surge_high else 0),
        0,
        20,
    )
    volume_score = clamp(
        (5 if amount_ratio_5d >= 1.15 else 2 if amount_ratio_5d >= 0.85 else 0)
        + (5 if amount_ratio_20d >= 1.0 else 2)
        + (5 if (breakout_20d and amount_ratio_5d >= 1.1) or (pullback_ma20 and amount_ratio_5d <= 1.8) else 1),
        0,
        15,
    )
    market_score = clamp(
        stock.rel_strength * 0.06
        + (4 if stock.sector_rank <= 5 else 1)
        + (3 if stock.amount_factor >= 1.2 else 1)
        + (2 if -2 <= stock.change <= 8 else 0),
        0,
        15,
    )
    fundamental_score = 3
    evidence_score = 3
    missing_data = ["基本面未接入", "估值未接入", "资金流未接入", "真实新闻公告未接入"]
    risk_penalty = min(18, len(risk_flags) * 3 + (4 if below_long_trend or bear_stack else 0))
    threshold_shift = 2 if risk_mode == "aggressive" else -4 if risk_mode == "defensive" else 0
    score = clamp(
        trend_score
        + position_score
        + volume_score
        + market_score
        + fundamental_score
        + evidence_score
        - risk_penalty
        + threshold_shift,
        0,
        100,
    )

    hard_excluded = below_long_trend or bear_stack
    if hard_excluded:
        status = "daily-risk"
        style = "日线破位"
    elif score >= 78 and (breakout_20d or pullback_ma20 or ma_bull):
        status = "daily-confirmed"
        style = "日线确认"
    elif score >= 65:
        status = "daily-watch"
        style = "趋势观察"
    elif score >= 55:
        status = "daily-candidate"
        style = "趋势备选"
    else:
        status = "daily-risk"
        style = "日线风险"

    checks = [
        {"label": "站上 MA20", "ok": above_ma20},
        {"label": "站上 MA60", "ok": above_ma60},
        {"label": "均线结构偏多", "ok": ma_bull},
        {"label": "MA20 走强", "ok": last_ma20 >= prev_ma20},
        {"label": "日线 MACD 走强", "ok": macd_up},
        {"label": "量价结构支持", "ok": amount_ratio_5d >= 0.85 and amount_ratio_20d >= 0.8},
        {"label": "20日新高或回踩MA20", "ok": breakout_20d or pullback_ma20},
        {"label": "日线风险可控", "ok": not hard_excluded and risk_penalty <= 10},
    ]

    return {
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
        "newsScore": stock.news_score,
        "riskFlags": risk_flags,
        "catalysts": [
            item.__dict__ for item in stock.catalysts
        ]
        + [
            Catalyst(
                "日线",
                str(last.get("date", "")),
                f"{stock.name} {stock.code}：日线数据截至 {last.get('date', '')}，20日涨跌幅 {return_20d:.2f}%，距 MA20 {distance_ma20:.1f}%",
            ).__dict__
        ],
        "limitStatus": stock.limit_status,
        "klineSource": kline_source,
        "klinePeriod": "日线",
        "tradeability": {"level": "daily", "label": "日线观察", "reason": "该视图只做日线层确认，不代表盘中即时买点。"},
        "series": usable,
        "macdSeries": macd_series,
        "vwapSeries": ma20,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "last": last,
        "lastVwap": last_ma20,
        "morningHigh": high_20,
        "morningLow": min(item["low"] for item in usable[-20:]),
        "score": round(score),
        "dimensions": {
            "technical": round(trend_score / 30 * 100),
            "volume": round(volume_score / 15 * 100),
            "sector": round(market_score / 15 * 100),
            "market": round(position_score / 20 * 100),
            "news": round(evidence_score / 10 * 100),
            "riskPenalty": risk_penalty,
            "dailyTrend": round(trend_score),
            "position": round(position_score),
            "volumePrice": round(volume_score),
            "marketStrength": round(market_score),
            "fundamental": fundamental_score,
            "evidence": evidence_score,
        },
        "dailyMetrics": {
            "dataDate": str(last.get("date", "")),
            "ma5": round(last_ma5, 3),
            "ma10": round(last_ma10, 3),
            "ma20": round(last_ma20, 3),
            "ma60": round(last_ma60, 3),
            "return5d": round(return_5d, 2),
            "return10d": round(return_10d, 2),
            "return20d": round(return_20d, 2),
            "distanceMa20": round(distance_ma20, 2),
            "distanceMa60": round(distance_ma60, 2),
            "amountRatio5d": round(amount_ratio_5d, 2),
            "amountRatio20d": round(amount_ratio_20d, 2),
            "breakout20d": breakout_20d,
            "pullbackMa20": pullback_ma20,
            "highStall": high_stall,
        },
        "dailyTrend": {
            "aboveMa20": above_ma20,
            "aboveMa60": above_ma60,
            "maBull": ma_bull,
            "longTrendOk": long_trend_ok,
            "macdUp": macd_up,
            "breakout20d": breakout_20d,
            "pullbackMa20": pullback_ma20,
            "status": style,
        },
        "missingData": missing_data,
        "evidenceStatus": {
            "dailyKline": "已接入",
            "fundamental": "待接入",
            "valuation": "待接入",
            "moneyFlow": "待接入",
            "news": "待接入",
        },
        "checks": checks,
        "passed": len([item for item in checks if item["ok"]]),
        "status": status,
        "style": style,
        "buyWindow": "日线确认",
        "buyTrigger": "站上 MA20/MA60，优先等待回踩 MA20 或突破后缩量确认。",
        "sellWindow": "日线风控",
        "sellTrigger": "跌破 MA20 且无法快速收回时降级，跌破 MA60 直接剔除。",
        "invalidCondition": f"跌破 MA20 {last_ma20:.2f} 或 MA60 {last_ma60:.2f}",
        "overnight": "daily-review",
        "viewMode": "daily",
    }


def _period_return(closes: List[float], period: int) -> float:
    if len(closes) <= period or closes[-period - 1] == 0:
        return 0.0
    return (closes[-1] / closes[-period - 1] - 1) * 100


def _last_ratio(series: List[Dict[str, float]], key: str, period: int) -> float:
    if len(series) <= period:
        return 1.0
    last_value = series[-1].get(key, 0.0)
    previous = [item.get(key, 0.0) for item in series[-period - 1 : -1] if item.get(key, 0.0) > 0]
    base = median(previous) or 1.0
    return last_value / base if base else 1.0
