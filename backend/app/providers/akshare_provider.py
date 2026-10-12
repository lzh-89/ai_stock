from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional

from backend.app.providers.provider_common import is_strict_limit_down
from backend.app.schema import Catalyst, RawStock


class AKShareProvider:
    source = "akshare"

    def __init__(self) -> None:
        try:
            import akshare as ak  # type: ignore
        except ModuleNotFoundError:
            self.ak = None
        else:
            self.ak = ak

    def available(self) -> bool:
        return self.ak is not None

    def fetch_market(self, stocks: Optional[List[RawStock]] = None) -> Dict[str, Any]:
        if stocks is None:
            stocks = self.fetch_snapshot()
        up = len([item for item in stocks if item.change > 0])
        down = len([item for item in stocks if item.change < 0])
        limit_up = len([item for item in stocks if item.limit_status in {"limit_up", "one_word_limit_up"}])
        natural_limit_down = len([item for item in stocks if item.limit_status == "limit_down" and not item.st])
        st_limit_down = len([item for item in stocks if item.limit_status == "limit_down" and item.st])
        near_limit_down = len([item for item in stocks if item.limit_status == "near_limit_down"])
        total_limit_down = natural_limit_down + st_limit_down
        hot_boards = _hot_boards(stocks)
        avg_change = sum(item.change for item in stocks) / len(stocks) if stocks else 0
        turnover_amount = sum(max(0.0, float(item.amount or 0)) for item in stocks)
        main_equal_weighted = round(_average_change(stocks, _is_main_board), 2)
        chinext_equal_weighted = round(_average_change(stocks, _is_chinext), 2)
        indices, index_status = self._fetch_indices()

        return {
            "indices": indices,
            "equalWeightedIndices": [
                {"name": "主板等权", "change": main_equal_weighted},
                {"name": "创业板等权", "change": chinext_equal_weighted},
            ],
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
            "marketIndexStatus": index_status,
            "mode": "aggressive" if avg_change > 1.2 and up > down * 1.5 else "defensive" if avg_change < -0.8 or down > up else "balanced",
        }

    def fetch_snapshot(self) -> List[RawStock]:
        if not self.ak:
            raise RuntimeError("AKShare is not installed")
        dataframe = self.ak.stock_zh_a_spot_em()
        rows = list(_iter_records(dataframe))
        normalized = [_normalize_row(row, index, rows) for index, row in enumerate(rows, start=1)]
        return [item for item in normalized if item is not None]

    def _fetch_indices(self) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if not self.ak:
            return [], {"available": False, "source": "akshare", "warning": "AKShare 未安装，无法获取真实指数"}
        wanted = {
            "000001": "上证指数",
            "399001": "深证成指",
            "399006": "创业板指",
            "000300": "沪深300",
            "000852": "中证1000",
        }
        try:
            dataframe = self.ak.stock_zh_index_spot_em()
            rows = list(_iter_records(dataframe))
        except Exception as error:  # noqa: BLE001 - index data is optional enrichment.
            return [], {"available": False, "source": "akshare", "warning": f"AKShare 真实指数获取失败：{error}"}

        indices: List[Dict[str, Any]] = []
        for row in rows:
            digits = "".join(char for char in str(row.get("代码", "")) if char.isdigit())
            code = digits[-6:] if len(digits) >= 6 else digits.zfill(6)
            if code not in wanted:
                continue
            name = str(row.get("名称") or wanted[code])
            indices.append({"name": name, "code": code, "change": round(_num(row.get("涨跌幅")), 2), "source": "akshare"})

        if not indices:
            return [], {"available": False, "source": "akshare", "warning": "AKShare 真实指数返回为空"}
        warning = ""
        if len(indices) < len(wanted):
            warning = f"AKShare 真实指数只返回 {len(indices)}/{len(wanted)} 项"
        return indices, {"available": True, "source": "akshare", "warning": warning}


def _iter_records(dataframe: Any) -> Iterable[Dict[str, Any]]:
    if hasattr(dataframe, "to_dict"):
        return dataframe.to_dict("records")
    return []


def _normalize_row(row: Dict[str, Any], index: int, all_rows: List[Dict[str, Any]]) -> Optional[RawStock]:
    code = str(row.get("代码", "")).strip().zfill(6)
    name = str(row.get("名称", "")).strip()
    price = _num(row.get("最新价"))
    change = _num(row.get("涨跌幅"))
    amount = _num(row.get("成交额"))
    volume_ratio = _num(row.get("量比"), default=1.0)
    turnover = _num(row.get("换手率"))
    high = _num(row.get("最高"))
    low = _num(row.get("最低"))
    open_price = _num(row.get("今开"))
    prev_close = _num(row.get("昨收"))

    if not code or not name or price <= 0:
        return None

    market_amounts = [_num(item.get("成交额")) for item in all_rows]
    median_amount = _median([value for value in market_amounts if value > 0]) or 1
    amount_factor = max(0.5, min(3.2, amount / median_amount))
    rel_strength = _percentile_rank(change, [_num(item.get("涨跌幅")) for item in all_rows])
    board = _board_name(code)
    board_rank = _board_rank(board, change, all_rows)
    board_limit_ups = _board_limit_ups(board, all_rows)
    limit_status = _limit_status(code, name, price, high, low, open_price, prev_close, change)
    risk_flags = _risk_flags(name, change, turnover, volume_ratio, amount, limit_status)

    return RawStock(
        code=code,
        name=name,
        sector=board,
        theme="AKShare实时行情",
        price=price,
        change=change,
        turnover=turnover,
        volume_ratio=volume_ratio,
        amount_factor=amount_factor,
        sector_rank=board_rank,
        sector_limit_ups=board_limit_ups,
        rel_strength=rel_strength,
        news_score=55,
        risk_flags=risk_flags,
        catalysts=[Catalyst("行情", "实时", f"{name} {code}：AKShare行情，成交额 {amount / 100000000:.2f} 亿，涨跌幅 {change:.2f}%")],
        status_bias=_status_bias(change, volume_ratio, amount_factor),
        series_seed=(sum(ord(char) for char in code) % 17) + 1,
        suspended=price <= 0,
        st=("ST" in name.upper()),
        limit_status=limit_status,
        amount=amount,
        open=open_price or None, high=high or None, low=low or None, pre_close=prev_close or None,
    )


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def _percentile_rank(value: float, values: List[float]) -> float:
    usable = sorted(item for item in values if not math.isnan(item))
    if not usable:
        return 50.0
    below = len([item for item in usable if item <= value])
    return round(below / len(usable) * 100, 2)


def _board_name(code: str) -> str:
    if _is_chinext_code(code):
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


def _is_main_board(stock: RawStock) -> bool:
    return stock.sector in {"沪市主板", "深市主板"}


def _is_chinext(stock: RawStock) -> bool:
    return stock.sector == "创业板"


def _is_chinext_code(code: str) -> bool:
    return code.startswith(("300", "301"))


def _board_rank(board: str, change: float, rows: List[Dict[str, Any]]) -> int:
    board_changes = []
    for row in rows:
        code = str(row.get("代码", "")).strip().zfill(6)
        if _board_name(code) == board:
            board_changes.append(_num(row.get("涨跌幅")))
    averages = []
    for candidate_board in {"沪市主板", "深市主板", "创业板", "科创板", "北交所"}:
        values = [
            _num(row.get("涨跌幅"))
            for row in rows
            if _board_name(str(row.get("代码", "")).strip().zfill(6)) == candidate_board
        ]
        if values:
            averages.append((candidate_board, sum(values) / len(values)))
    ranked = sorted(averages, key=lambda item: item[1], reverse=True)
    board_positions = {name: position for position, (name, _) in enumerate(ranked, start=1)}
    relative_boost = 0 if change < 8 else -1
    return max(1, board_positions.get(board, 8) + relative_boost)


def _board_limit_ups(board: str, rows: List[Dict[str, Any]]) -> int:
    return len(
        [
            row
            for row in rows
            if _board_name(str(row.get("代码", "")).strip().zfill(6)) == board
            and _num(row.get("涨跌幅")) >= 9.8
        ]
    )


def _hot_boards(stocks: List[RawStock]) -> List[str]:
    boards: Dict[str, List[float]] = {}
    for stock in stocks:
        boards.setdefault(stock.sector, []).append(stock.change)
    ranked = sorted(
        ((board, sum(values) / len(values)) for board, values in boards.items() if values),
        key=lambda item: item[1],
        reverse=True,
    )
    return [name for name, _ in ranked[:5]]


def _average_change(stocks: List[RawStock], predicate: Any) -> float:
    values = [stock.change for stock in stocks if predicate(stock)]
    return sum(values) / len(values) if values else 0.0


def _limit_status(code: str, name: str, price: float, high: float, low: float, open_price: float, prev_close: float, change: float) -> str:
    limit = _limit_threshold(code, name)
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


def _limit_threshold(code: str, name: str = "") -> float:
    normalized = str(code or "").strip().zfill(6)
    if "ST" in name.upper() and normalized.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return 5.0
    if normalized.startswith(("300", "301", "688")):
        return 20.0
    if normalized.startswith(("4", "8", "9")):
        return 30.0
    return 10.0


def _risk_flags(name: str, change: float, turnover: float, volume_ratio: float, amount: float, limit_status: str) -> List[str]:
    flags: List[str] = []
    if "ST" in name.upper():
        flags.append("ST 或退市风险")
    if change >= 8:
        flags.append("涨幅超过 8%，追高风险降权")
    if limit_status == "limit_up":
        flags.append("接近涨停，买入排队和追高风险高")
    if limit_status == "one_word_limit_up":
        flags.append("一字涨停，通常无法有效买入")
    if limit_status == "limit_down":
        flags.append("跌停，卖出流动性风险高")
    if limit_status == "near_limit_down":
        flags.append("接近跌停，承接和流动性风险高")
    if turnover >= 12:
        flags.append("换手率偏高")
    if volume_ratio <= 0.8:
        flags.append("量比偏弱")
    if amount <= 50000000:
        flags.append("成交额偏低")
    return flags


def _status_bias(change: float, volume_ratio: float, amount_factor: float) -> str:
    if change >= 4 and volume_ratio >= 1.8 and amount_factor >= 1.4:
        return "triggered"
    if 1.5 <= change < 5 and volume_ratio >= 1.2:
        return "pullback"
    return "watch"
