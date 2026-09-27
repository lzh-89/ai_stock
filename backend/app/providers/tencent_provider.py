from __future__ import annotations

import re
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


class TencentProvider:
    source = "tencent"

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
        return market_from_stocks(stocks)

    def _fetch_symbols(self, symbols: List[str]) -> List[Dict[str, Any]]:
        url = f"https://qt.gtimg.cn/q={','.join(symbols)}"
        request = urllib.request.Request(url, headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=12) as response:
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
    )
