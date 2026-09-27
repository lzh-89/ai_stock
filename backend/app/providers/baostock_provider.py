from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from backend.app.providers.provider_common import (
    baostock_code,
    board_name,
    board_rank,
    limit_status,
    market_from_stocks,
    median,
    num,
    percentile_rank,
    risk_flags,
    status_bias,
    volume_proxy,
)
from backend.app.schema import Catalyst, RawStock
from backend.app.universe_store import load_universe_items


class BaostockProvider:
    source = "baostock"

    def __init__(self) -> None:
        try:
            import baostock as bs  # type: ignore
        except ModuleNotFoundError:
            self.bs = None
        else:
            self.bs = bs

    def available(self) -> bool:
        return self.bs is not None

    def fetch_snapshot(self) -> List[RawStock]:
        if not self.bs:
            raise RuntimeError("BaoStock 未安装")
        login_result = self.bs.login()
        if getattr(login_result, "error_code", "0") != "0":
            raise RuntimeError(f"BaoStock 登录失败：{getattr(login_result, 'error_msg', '')}")
        try:
            rows = self._fetch_rows()
        finally:
            self.bs.logout()
        normalized = [_normalize_row(row, rows) for row in rows]
        return [item for item in normalized if item is not None]

    def fetch_market(self, stocks: Optional[List[RawStock]] = None) -> Dict[str, Any]:
        if stocks is None:
            stocks = self.fetch_snapshot()
        return market_from_stocks(stocks)

    def _fetch_rows(self) -> List[Dict[str, str]]:
        end = date.today()
        start = end - timedelta(days=14)
        rows: List[Dict[str, str]] = []
        fields = "date,code,open,high,low,close,preclose,volume,amount,pctChg,turn"
        for code, name in load_universe_items():
            result = self.bs.query_history_k_data_plus(
                baostock_code(code),
                fields,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag="3",
            )
            stock_rows: List[Dict[str, str]] = []
            while result.error_code == "0" and result.next():
                values = result.get_row_data()
                stock_rows.append(dict(zip(result.fields, values)))
            if stock_rows:
                row = stock_rows[-1]
                row["name"] = name
                rows.append(row)
        return rows


def _normalize_row(row: Dict[str, str], all_rows: List[Dict[str, str]]) -> Optional[RawStock]:
    code = row.get("code", "").split(".")[-1].zfill(6)
    name = row.get("name", code)
    price = num(row.get("close"))
    high = num(row.get("high"))
    low = num(row.get("low"))
    open_price = num(row.get("open"))
    prev_close = num(row.get("preclose"))
    change = num(row.get("pctChg"))
    turnover = num(row.get("turn"))
    amount = num(row.get("amount"))
    if not code or price <= 0:
        return None

    amounts = [num(item.get("amount")) for item in all_rows if num(item.get("amount")) > 0]
    amount_factor = max(0.5, min(3.2, amount / (median(amounts) or 1)))
    changes = [num(item.get("pctChg")) for item in all_rows]
    board = board_name(code)
    board_changes: Dict[str, List[float]] = {}
    for item in all_rows:
        item_code = item.get("code", "").split(".")[-1].zfill(6)
        board_changes.setdefault(board_name(item_code), []).append(num(item.get("pctChg")))
    stock_limit_status = limit_status(price, high, low, open_price, change, code=code, name=name, prev_close=prev_close)
    return RawStock(
        code=code,
        name=name,
        sector=board,
        theme="BaoStock日线行情",
        price=price,
        change=change,
        turnover=turnover,
        volume_ratio=volume_proxy(amount_factor, turnover),
        amount_factor=amount_factor,
        sector_rank=board_rank(board, change, board_changes),
        sector_limit_ups=len([item for item in all_rows if board_name(item.get("code", "").split(".")[-1].zfill(6)) == board and num(item.get("pctChg")) >= 9.8]),
        rel_strength=percentile_rank(change, changes),
        news_score=50,
        risk_flags=risk_flags(name, change, turnover, amount, stock_limit_status),
        catalysts=[Catalyst("日线", row.get("date", ""), f"{name} {code}：BaoStock日线，成交额 {amount / 100000000:.2f} 亿，涨跌幅 {change:.2f}%")],
        status_bias=status_bias(change, amount_factor, turnover),
        series_seed=(sum(ord(char) for char in code) % 17) + 1,
        st=("ST" in name.upper()),
        limit_status=stock_limit_status,
        amount=amount,
    )
