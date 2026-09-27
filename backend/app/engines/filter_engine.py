from __future__ import annotations

from typing import Dict, Tuple

from backend.app.schema import RawStock


MAIN_BOARD_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")
CHINEXT_PREFIXES = ("300", "301")
STAR_PREFIXES = ("688",)
BSE_PREFIXES = ("4", "8", "9")


def in_confirmed_universe(code: str, include_bse: bool = False, include_star_market: bool = False) -> bool:
    if code.startswith(STAR_PREFIXES):
        return include_star_market
    if code.startswith(BSE_PREFIXES):
        return include_bse
    return code.startswith(MAIN_BOARD_PREFIXES) or code.startswith(CHINEXT_PREFIXES)


def tradeability(stock: RawStock) -> Dict[str, str]:
    if stock.limit_status == "limit_up":
        return {
            "level": "hard-to-buy",
            "label": "接近涨停",
            "reason": "接近涨停时买入排队和追高风险高，只能在回落后重新满足规则时观察。",
        }
    if stock.limit_status == "one_word_limit_up":
        return {
            "level": "not-buyable",
            "label": "一字涨停",
            "reason": "一字涨停通常无法有效买入，保留展示但不作为主动买入候选。",
        }
    if stock.limit_status == "limit_down":
        return {
            "level": "hard-to-sell",
            "label": "跌停",
            "reason": "跌停流动性和卖出风险极高，保留展示但必须大幅降权。",
        }
    if stock.change >= 8:
        return {
            "level": "chase-risk",
            "label": "高涨幅",
            "reason": "涨幅超过 8%，不剔除但追高风险降权，优先等待回踩确认。",
        }
    return {"level": "tradable", "label": "可观察", "reason": "未触发涨跌停流动性限制，按规则继续评估。"}


def hard_filter_reason(stock: RawStock, include_bse: bool = False, include_star_market: bool = False) -> Tuple[bool, str]:
    if not in_confirmed_universe(stock.code, include_bse=include_bse, include_star_market=include_star_market):
        return False, "不在第一阶段市场范围"
    if stock.st or "ST" in stock.name.upper():
        return False, "ST 或退市风险股票"
    if stock.suspended:
        return False, "停牌"
    if stock.amount_factor < 0.8 and stock.volume_ratio < 1.0:
        return False, "量能过低"
    return True, ""

