from __future__ import annotations

from typing import Any, Dict, List

from backend.app.engines.filter_engine import tradeability
from backend.app.engines.indicator_engine import generate_series, macd, moving_average, vwap
from backend.app.schema import RawStock


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def score_stock(stock: RawStock, risk_mode: str = "balanced") -> Dict[str, Any]:
    series = _usable_series(stock)
    kline_source = stock.kline_source or "simulated"
    kline_period = stock.kline_period or ""
    macd_series = macd(series)
    vwap_series = vwap(series)
    ma5 = moving_average(series, 5)
    ma10 = moving_average(series, 10)
    last = series[-1]
    last_macd = macd_series[-1]
    prev_macd = macd_series[-2]
    last_vwap = vwap_series[-1]
    morning_high = max(item["high"] for item in series[:16])
    morning_low = min(item["low"] for item in series[:16])

    above_vwap = last["close"] > last_vwap
    macd_up = last_macd["dif"] > last_macd["dea"] and last_macd["hist"] > prev_macd["hist"]
    ma_bull = ma5[-1] > ma10[-1] and last["close"] > ma5[-1]
    breakout = last["close"] > morning_high * 0.998
    near_vwap = last_vwap * 0.995 <= last["close"] <= last_vwap * 1.025
    pullback_ok = stock.status_bias == "pullback" and near_vwap and not breakout

    technical = clamp(
        46
        + (14 if above_vwap else -8)
        + (14 if macd_up else -5)
        + (12 if ma_bull else -6)
        + (10 if breakout else 0)
        + (8 if pullback_ok else 0)
        + stock.rel_strength * 0.08,
        0,
        100,
    )
    volume = clamp(32 + stock.volume_ratio * 11 + stock.amount_factor * 13 + stock.turnover * 1.3, 0, 100)
    sector = clamp(94 - stock.sector_rank * 5 + stock.sector_limit_ups * 2 + stock.rel_strength * 0.12, 0, 100)
    market = 86 if risk_mode == "aggressive" else 67 if risk_mode == "defensive" else 78
    news = clamp(stock.news_score, 0, 100)

    trade = tradeability(stock)
    trade_penalty = {
        "tradable": 0,
        "chase-risk": 8,
        "hard-to-buy": 12,
        "not-buyable": 18,
        "hard-to-sell": 22,
    }.get(trade["level"], 0)
    risk_penalty = len(stock.risk_flags) * 4 + (5 if stock.turnover > 8 else 0) + (7 if stock.sector_rank > 5 else 0) + trade_penalty
    threshold_shift = 2 if risk_mode == "aggressive" else -5 if risk_mode == "defensive" else 0
    score = clamp(technical * 0.3 + volume * 0.2 + sector * 0.2 + market * 0.15 + news * 0.1 - risk_penalty + threshold_shift, 0, 100)

    checks = [
        {"label": "时间窗允许", "ok": True},
        {"label": "价格站上 VWAP", "ok": above_vwap},
        {"label": "MACD 走强", "ok": macd_up},
        {"label": "均线结构偏多", "ok": ma_bull},
        {"label": "量能强于样本", "ok": stock.amount_factor >= 1.5 and stock.volume_ratio >= 1.6},
        {"label": "板块排名前五", "ok": stock.sector_rank <= 5},
        {"label": "风险惩罚可控", "ok": risk_penalty <= 14},
        {"label": "可买卖性已说明", "ok": trade["level"] in {"tradable", "chase-risk", "hard-to-buy"}},
    ]
    passed = len([item for item in checks if item["ok"]])
    status = resolve_status(score, passed, stock.status_bias, risk_mode, trade["level"], breakout, pullback_ok)
    style = "强势突破" if breakout else "回踩低吸" if pullback_ok else "趋势延续" if above_vwap and macd_up else "观察等待"

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
        "riskFlags": stock.risk_flags,
        "catalysts": [item.__dict__ for item in stock.catalysts],
        "limitStatus": stock.limit_status,
        "klineSource": kline_source,
        "klinePeriod": kline_period,
        "tradeability": trade,
        "series": series,
        "macdSeries": macd_series,
        "vwapSeries": vwap_series,
        "ma5": ma5,
        "ma10": ma10,
        "last": last,
        "lastVwap": last_vwap,
        "morningHigh": morning_high,
        "morningLow": morning_low,
        "score": round(score),
        "dimensions": {
            "technical": round(technical),
            "volume": round(volume),
            "sector": round(sector),
            "market": round(market),
            "news": round(news),
            "riskPenalty": risk_penalty,
        },
        "checks": checks,
        "passed": passed,
        "status": status,
        "style": style,
    }


def _usable_series(stock: RawStock) -> List[Dict[str, float]]:
    if len(stock.kline_series) >= 8:
        return stock.kline_series
    return generate_series(stock.series_seed, stock.price, stock.status_bias)


def resolve_status(score: float, passed: int, bias: str, risk_mode: str, trade_level: str, breakout: bool, pullback_ok: bool) -> str:
    if trade_level in {"not-buyable", "hard-to-sell"}:
        return "watch"
    if score < 65 or passed < 4:
        return "invalid"
    if risk_mode == "defensive" and score < 82:
        return "watch"
    if (bias == "triggered" or breakout) and score >= 78 and passed >= 6:
        return "buy-triggered"
    if pullback_ok and score >= 75 and passed >= 5:
        return "pullback"
    return "watch"
