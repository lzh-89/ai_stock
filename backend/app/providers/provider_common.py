from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List

from backend.app.schema import RawStock


DEFAULT_UNIVERSE = [
    ("600519", "贵州茅台"),
    ("300750", "宁德时代"),
    ("002050", "三花智控"),
    ("300124", "汇川技术"),
    ("002371", "北方华创"),
    ("600584", "长电科技"),
    ("601138", "工业富联"),
    ("603019", "中科曙光"),
    ("000063", "中兴通讯"),
    ("300308", "中际旭创"),
    ("002230", "科大讯飞"),
    ("002085", "万丰奥威"),
    ("300760", "迈瑞医疗"),
    ("300274", "阳光电源"),
    ("000977", "浪潮信息"),
    ("000333", "美的集团"),
    ("000651", "格力电器"),
    ("600276", "恒瑞医药"),
    ("601318", "中国平安"),
    ("600036", "招商银行"),
    ("002594", "比亚迪"),
    ("300059", "东方财富"),
    ("600031", "三一重工"),
    ("601012", "隆基绿能"),
    ("002475", "立讯精密"),
    ("000858", "五粮液"),
]


def num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def median(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def percentile_rank(value: float, values: List[float]) -> float:
    usable = sorted(item for item in values if not math.isnan(item))
    if not usable:
        return 50.0
    below = len([item for item in usable if item <= value])
    return round(below / len(usable) * 100, 2)


def board_name(code: str) -> str:
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("600", "601", "603", "605")):
        return "沪市主板"
    if code.startswith(("000", "001", "002", "003")):
        return "深市主板"
    if code.startswith("688"):
        return "科创板"
    if code.startswith(("4", "8", "9")):
        return "北交所"
    return "其他"


def is_st_name(name: str) -> bool:
    return "ST" in str(name or "").upper()


def limit_threshold(code: str = "", name: str = "") -> float:
    raw_code = str(code or "").strip()
    normalized = raw_code.zfill(6)
    board = board_name(normalized)
    if is_st_name(name) and board in {"沪市主板", "深市主板"}:
        return 5.0
    if is_st_name(name) and not raw_code:
        return 5.0
    if normalized.startswith(("300", "301", "688")):
        return 20.0
    if normalized.startswith(("4", "8", "9")):
        return 30.0
    return 10.0


def limit_price(prev_close: float, limit: float, direction: str) -> float:
    if prev_close <= 0:
        return 0.0
    multiplier = Decimal("1") + (Decimal(str(limit)) / Decimal("100") if direction == "up" else -Decimal(str(limit)) / Decimal("100"))
    return float((Decimal(str(prev_close)) * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_strict_limit_down(price: float, prev_close: float, limit: float, change: float) -> bool:
    if prev_close <= 0 or price <= 0:
        return change <= -limit * 0.98
    return price <= limit_price(prev_close, limit, "down") + 0.001 and change <= -limit * 0.90


def symbol_prefix(code: str) -> str:
    return "sh" if code.startswith(("5", "6", "9")) else "sz"


def ts_code(code: str) -> str:
    suffix = "SH" if code.startswith(("5", "6", "9")) else "SZ"
    return f"{code}.{suffix}"


def baostock_code(code: str) -> str:
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}.{code}"


def board_rank(board: str, change: float, board_changes: Dict[str, List[float]]) -> int:
    averages = [(name, sum(values) / len(values)) for name, values in board_changes.items() if values]
    ranked = sorted(averages, key=lambda item: item[1], reverse=True)
    positions = {name: position for position, (name, _) in enumerate(ranked, start=1)}
    return max(1, positions.get(board, 8) - (1 if change >= 8 else 0))


def limit_status(
    price: float,
    high: float,
    low: float,
    open_price: float,
    change: float,
    code: str = "",
    name: str = "",
    prev_close: float = 0.0,
) -> str:
    limit = limit_threshold(code, name)
    near_limit = limit * 0.96
    hard_limit = limit * 0.98
    if is_strict_limit_down(price, prev_close, limit, change):
        return "limit_down"
    if change <= -near_limit:
        return "near_limit_down"
    if change >= hard_limit and abs(high - low) <= max(price * 0.002, 0.01) and abs(open_price - high) <= max(price * 0.002, 0.01):
        return "one_word_limit_up"
    if change >= near_limit:
        return "limit_up"
    if change >= limit * 0.8:
        return "near_limit_up"
    return "normal"


def risk_flags(name: str, change: float, turnover: float, amount: float, status: str) -> List[str]:
    flags: List[str] = []
    if "ST" in name.upper():
        flags.append("ST 或退市风险")
    if change >= 8:
        flags.append("涨幅超过 8%，追高风险降权")
    if status == "limit_up":
        flags.append("接近涨停，买入排队和追高风险高")
    if status == "one_word_limit_up":
        flags.append("一字涨停，通常无法有效买入")
    if status == "limit_down":
        flags.append("跌停，卖出流动性风险高")
    if status == "near_limit_down":
        flags.append("接近跌停，承接和流动性风险高")
    if turnover >= 12:
        flags.append("换手率偏高")
    if amount <= 50000000:
        flags.append("成交额偏低")
    return flags


def status_bias(change: float, amount_factor: float, turnover: float) -> str:
    if change >= 4 and amount_factor >= 1.2:
        return "triggered"
    if 1.5 <= change < 5 and turnover >= 1.0:
        return "pullback"
    return "watch"


def volume_proxy(amount_factor: float, turnover: float) -> float:
    return max(0.8, min(4.0, amount_factor * 0.9 + turnover / 12))


def market_from_stocks(stocks: List[RawStock]) -> Dict[str, Any]:
    up = len([item for item in stocks if item.change > 0])
    down = len([item for item in stocks if item.change < 0])
    limit_up = len([item for item in stocks if item.limit_status in {"limit_up", "one_word_limit_up"}])
    natural_limit_down = len([item for item in stocks if item.limit_status == "limit_down" and not item.st])
    st_limit_down = len([item for item in stocks if item.limit_status == "limit_down" and item.st])
    near_limit_down = len([item for item in stocks if item.limit_status == "near_limit_down"])
    total_limit_down = natural_limit_down + st_limit_down
    boards: Dict[str, List[float]] = {}
    for stock in stocks:
        boards.setdefault(stock.sector, []).append(stock.change)
    hot_boards = [
        name
        for name, _ in sorted(
            ((board, sum(values) / len(values)) for board, values in boards.items() if values),
            key=lambda item: item[1],
            reverse=True,
        )[:5]
    ]
    avg_change = sum(item.change for item in stocks) / len(stocks) if stocks else 0
    main_values = [item.change for item in stocks if item.sector in {"沪市主板", "深市主板"}]
    chinext_values = [item.change for item in stocks if item.sector == "创业板"]
    equal_weighted_indices = [
        {"name": "主板等权", "change": round(sum(main_values) / len(main_values), 2) if main_values else 0},
        {"name": "创业板等权", "change": round(sum(chinext_values) / len(chinext_values), 2) if chinext_values else 0},
    ]
    turnover_amount = sum(max(0.0, float(item.amount or 0)) for item in stocks)
    return {
        "indices": [],
        "equalWeightedIndices": equal_weighted_indices,
        "breadth": {"up": up, "down": down},
        "limitUp": limit_up,
        "limitDown": natural_limit_down,
        "stLimitDown": st_limit_down,
        "nearLimitDown": near_limit_down,
        "totalLimitDown": total_limit_down,
        "limitDownStats": {
            "natural": natural_limit_down,
            "st": st_limit_down,
            "near": near_limit_down,
            "total": total_limit_down,
        },
        "turnoverAmount": round(turnover_amount, 2) if turnover_amount > 0 else None,
        "hotSectors": hot_boards,
        "marketIndexStatus": {
            "available": False,
            "source": "",
            "warning": "当前行情源未提供真实指数，仅提供个股等权环境。",
        },
        "mode": "aggressive" if avg_change > 1.2 and up > down * 1.5 else "defensive" if avg_change < -0.8 or down > up else "balanced",
    }
