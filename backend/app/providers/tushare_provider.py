from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from backend.app.providers.provider_common import (
    board_name,
    board_rank,
    limit_status,
    market_from_stocks,
    median,
    num,
    percentile_rank,
    risk_flags,
    status_bias,
    ts_code,
    volume_proxy,
)
from backend.app.schema import Catalyst, RawStock
from backend.app.universe_store import load_universe_items


class TushareProvider:
    source = "tushare"

    def __init__(self, token: str):
        self.token = token
        try:
            import tushare as ts  # type: ignore
        except ModuleNotFoundError:
            self.ts = None
        else:
            self.ts = ts

    def available(self) -> bool:
        return bool(self.token and self.ts)

    def client(self) -> Any:
        if not self.available():
            raise RuntimeError("Tushare token or package is missing")
        return self.ts.pro_api(self.token)

    def fetch_snapshot(self) -> List[RawStock]:
        pro = self.client()
        trade_date = self._latest_trade_date(pro)
        dataframe = pro.daily(trade_date=trade_date)
        if dataframe is None or dataframe.empty:
            raise RuntimeError("Tushare daily 未返回数据")
        names = self._stock_names(pro)
        wanted = {ts_code(code) for code, _ in load_universe_items()}
        rows = [row for row in dataframe.to_dict("records") if row.get("ts_code") in wanted]
        if not rows:
            rows = dataframe.head(80).to_dict("records")
        normalized = [_normalize_row(row, rows, names) for row in rows]
        return [item for item in normalized if item is not None]

    def fetch_market(self, stocks: Optional[List[RawStock]] = None) -> Dict[str, Any]:
        if stocks is None:
            stocks = self.fetch_snapshot()
        return market_from_stocks(stocks)

    def _latest_trade_date(self, pro: Any) -> str:
        end = date.today()
        start = end - timedelta(days=14)
        calendar = pro.trade_cal(exchange="", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), is_open="1")
        if calendar is not None and not calendar.empty:
            return str(calendar.sort_values("cal_date").iloc[-1]["cal_date"])
        return end.strftime("%Y%m%d")

    def _stock_names(self, pro: Any) -> Dict[str, str]:
        try:
            stock_basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
        except Exception:  # noqa: BLE001 - names are optional enrichment.
            return {ts_code(code): name for code, name in load_universe_items()}
        return {str(row["ts_code"]): str(row["name"]) for row in stock_basic.to_dict("records")}


def _normalize_row(row: Dict[str, Any], all_rows: List[Dict[str, Any]], names: Dict[str, str]) -> Optional[RawStock]:
    code = str(row.get("ts_code", "")).split(".")[0].zfill(6)
    name = names.get(str(row.get("ts_code", "")), code)
    price = num(row.get("close"))
    high = num(row.get("high"))
    low = num(row.get("low"))
    open_price = num(row.get("open"))
    prev_close = num(row.get("pre_close"))
    change = num(row.get("pct_chg"))
    amount = num(row.get("amount")) * 1000
    turnover = 0.0
    if not code or price <= 0:
        return None

    amounts = [num(item.get("amount")) * 1000 for item in all_rows if num(item.get("amount")) > 0]
    amount_factor = max(0.5, min(3.2, amount / (median(amounts) or 1)))
    changes = [num(item.get("pct_chg")) for item in all_rows]
    board = board_name(code)
    board_changes: Dict[str, List[float]] = {}
    for item in all_rows:
        item_code = str(item.get("ts_code", "")).split(".")[0].zfill(6)
        board_changes.setdefault(board_name(item_code), []).append(num(item.get("pct_chg")))
    stock_limit_status = limit_status(price, high, low, open_price, change, code=code, name=name, prev_close=prev_close)

    return RawStock(
        code=code,
        name=name,
        sector=board,
        theme="Tushare Pro日线行情",
        price=price,
        change=change,
        turnover=turnover,
        volume_ratio=volume_proxy(amount_factor, turnover),
        amount_factor=amount_factor,
        sector_rank=board_rank(board, change, board_changes),
        sector_limit_ups=len([item for item in all_rows if board_name(str(item.get("ts_code", "")).split(".")[0].zfill(6)) == board and num(item.get("pct_chg")) >= 9.8]),
        rel_strength=percentile_rank(change, changes),
        news_score=52,
        risk_flags=risk_flags(name, change, turnover, amount, stock_limit_status),
        catalysts=[Catalyst("日线", str(row.get("trade_date", "")), f"{name} {code}：Tushare日线，成交额 {amount / 100000000:.2f} 亿，涨跌幅 {change:.2f}%")],
        status_bias=status_bias(change, amount_factor, turnover),
        series_seed=(sum(ord(char) for char in code) % 17) + 1,
        st=("ST" in name.upper()),
        limit_status=stock_limit_status,
        amount=amount,
    )
