from __future__ import annotations

import re
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    symbol_prefix,
    volume_proxy,
)
from backend.app.schema import Catalyst, RawStock
from backend.app.universe_store import load_universe_items


TENCENT_SNAPSHOT_TIMEOUT_SECONDS = float(os.getenv("TENCENT_SNAPSHOT_TIMEOUT_SECONDS", "8"))
TENCENT_MAIN_INDEX_SYMBOLS = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000852": "中证1000",
}


class TencentProvider:
    source = "tencent"

    def __init__(self, timeout_seconds: Optional[float] = None):
        self.timeout_seconds = TENCENT_SNAPSHOT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds

    def fetch_snapshot(self) -> List[RawStock]:
        return self.fetch_codes([code for code, _ in load_universe_items()])

    def fetch_codes(self, codes: List[str]) -> List[RawStock]:
        symbols = _symbols_from_codes(codes)
        rows: List[Dict[str, Any]] = []
        for start in range(0, len(symbols), 60):
            rows.extend(self._fetch_symbols(symbols[start : start + 60]))
        normalized = [_normalize_row(row, rows) for row in rows]
        return [item for item in normalized if item is not None]

    def fetch_codes_parallel(self, codes: List[str], max_workers: int = 8, batch_size: int = 60) -> List[RawStock]:
        symbols = _symbols_from_codes(codes)
        batches = [symbols[start : start + batch_size] for start in range(0, len(symbols), batch_size)]
        if not batches:
            return []
        rows: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(batches)))) as executor:
            futures = {executor.submit(self._fetch_symbols, batch): batch for batch in batches}
            for future in as_completed(futures):
                rows.extend(future.result())
        normalized = [_normalize_row(row, rows) for row in rows]
        return [item for item in normalized if item is not None]

    def fetch_market(self, stocks: Optional[List[RawStock]] = None) -> Dict[str, Any]:
        if stocks is None:
            stocks = self.fetch_snapshot()
        market = market_from_stocks(stocks)
        try:
            indices = self.fetch_indices()
        except Exception as error:  # noqa: BLE001 - stock breadth remains independently usable.
            market["indices"] = []
            market["marketIndexStatus"] = {"available": False, "source": self.source, "warning": str(error)}
        else:
            market["indices"] = indices
            market["marketIndexStatus"] = {
                "available": len(indices) == len(TENCENT_MAIN_INDEX_SYMBOLS),
                "source": self.source,
                "warning": "" if len(indices) == len(TENCENT_MAIN_INDEX_SYMBOLS) else f"腾讯主要指数只返回 {len(indices)}/4 项",
            }
        return market

    def fetch_indices(self) -> List[Dict[str, Any]]:
        rows = self._fetch_symbols(list(TENCENT_MAIN_INDEX_SYMBOLS))
        result = []
        for row in rows:
            fields = row.get("fields") or []
            if len(fields) <= 32:
                continue
            code = str(fields[2] or "").strip().zfill(6)
            symbol = next((item for item in TENCENT_MAIN_INDEX_SYMBOLS if item.endswith(code)), "")
            raw_time = str(fields[30] or "") if len(fields) > 30 else ""
            if not symbol or len(raw_time) < 14:
                continue
            trade_date = f"{raw_time[:4]}-{raw_time[4:6]}-{raw_time[6:8]}"
            quote_time = f"{trade_date}T{raw_time[8:10]}:{raw_time[10:12]}:{raw_time[12:14]}"
            result.append(
                {
                    "name": TENCENT_MAIN_INDEX_SYMBOLS[symbol],
                    "code": symbol,
                    "change": num(fields[32]),
                    "changePct": num(fields[32]),
                    "tradeDate": trade_date,
                    "quoteTime": quote_time,
                    "source": self.source,
                }
            )
        return result

    def _fetch_symbols(self, symbols: List[str]) -> List[Dict[str, Any]]:
        url = f"https://qt.gtimg.cn/q={','.join(symbols)}"
        request = urllib.request.Request(url, headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read().decode("gbk", errors="ignore")
        rows: List[Dict[str, Any]] = []
        for line in body.split(";"):
            if "~" not in line:
                continue
            match = re.search(r'="([^"]+)"', line)
            if match:
                fields = match.group(1).split("~")
                if len(fields) > 38:
                    rows.append({"fields": fields})
        return rows


def _symbols_from_codes(codes: List[str]) -> List[str]:
    symbols = []
    seen = set()
    for code in codes:
        normalized = str(code).strip().zfill(6)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        symbols.append(f"{symbol_prefix(normalized)}{normalized}")
    return symbols


def _normalize_row(row: Dict[str, Any], all_rows: List[Dict[str, Any]]) -> Optional[RawStock]:
    fields = row["fields"]
    name = fields[1].strip()
    code = fields[2].strip().zfill(6)
    price = num(fields[3])
    prev_close = num(fields[4])
    open_price = num(fields[5])
    high = num(fields[33])
    low = num(fields[34])
    change = num(fields[32])
    amount = num(fields[37]) * 10000
    turnover = num(fields[38])

    if not code or not name or price <= 0:
        return None

    amounts = [num(item["fields"][37]) * 10000 for item in all_rows if len(item["fields"]) > 38 and num(item["fields"][37]) > 0]
    median_amount = median(amounts) or 1
    amount_factor = max(0.5, min(3.2, amount / median_amount))
    changes = [num(item["fields"][32]) for item in all_rows if len(item["fields"]) > 32]
    rel_strength = percentile_rank(change, changes)
    board = board_name(code)
    board_changes: Dict[str, List[float]] = {}
    for item in all_rows:
        item_code = item["fields"][2].strip().zfill(6)
        board_changes.setdefault(board_name(item_code), []).append(num(item["fields"][32]))
    stock_limit_status = limit_status(price, high, low, open_price, change, code=code, name=name, prev_close=prev_close)
    flags = risk_flags(name, change, turnover, amount, stock_limit_status)

    return RawStock(
        code=code,
        name=name,
        sector=board,
        theme="腾讯实时行情",
        price=price,
        change=change,
        turnover=turnover,
        volume_ratio=volume_proxy(amount_factor, turnover),
        amount_factor=amount_factor,
        sector_rank=board_rank(board, change, board_changes),
        sector_limit_ups=len([item for item in all_rows if board_name(item["fields"][2].strip().zfill(6)) == board and num(item["fields"][32]) >= 9.8]),
        rel_strength=rel_strength,
        news_score=55,
        risk_flags=flags,
        catalysts=[Catalyst("行情", fields[30] if len(fields) > 30 else "实时", f"{name} {code}：腾讯行情，成交额 {amount / 100000000:.2f} 亿，涨跌幅 {change:.2f}%")],
        status_bias=status_bias(change, amount_factor, turnover),
        series_seed=(sum(ord(char) for char in code) % 17) + 1,
        st=("ST" in name.upper()),
        limit_status=stock_limit_status,
        amount=amount,
        open=open_price or None, high=high or None, low=low or None, pre_close=prev_close or None,
        trade_date=(f"{fields[30][:4]}-{fields[30][4:6]}-{fields[30][6:8]}" if len(fields[30]) >= 14 else ""),
        quote_time=(f"{fields[30][:4]}-{fields[30][4:6]}-{fields[30][6:8]}T{fields[30][8:10]}:{fields[30][10:12]}:{fields[30][12:14]}" if len(fields[30]) >= 14 else ""),
    )
