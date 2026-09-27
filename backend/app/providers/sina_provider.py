from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional

from backend.app.providers.provider_common import is_strict_limit_down
from backend.app.schema import Catalyst, RawStock


LOGGER = logging.getLogger("ai_stock.provider.sina")
SINA_SAMPLE_SORT_KEYS = ("changepercent", "turnoverratio", "amount")
SINA_SAMPLE_PAGES = range(1, 5)
SINA_FULL_SORT_KEY = "changepercent"
SINA_FULL_EXTRA_SORT_KEYS = ("turnoverratio", "amount")
SINA_FULL_PAGES = range(1, int(os.getenv("SINA_FULL_PAGES", "75")) + 1)
SINA_FULL_EXTRA_PAGES = range(1, 5)
SINA_FULL_MIN_ROWS = int(os.getenv("SINA_FULL_MIN_ROWS", "1800"))
SINA_MAX_WORKERS = int(os.getenv("SINA_MAX_WORKERS", "6"))
SINA_PAGE_TIMEOUT_SECONDS = int(os.getenv("SINA_PAGE_TIMEOUT_SECONDS", "6"))
SINA_FAILURE_COOLDOWN_SECONDS = int(os.getenv("SINA_FAILURE_COOLDOWN_SECONDS", "300"))
SINA_INDEX_TIMEOUT_SECONDS = int(os.getenv("SINA_INDEX_TIMEOUT_SECONDS", "4"))
SINA_INDEX_SYMBOLS = (
    ("s_sh000001", "上证指数"),
    ("s_sz399001", "深证成指"),
    ("s_sz399006", "创业板指"),
    ("s_sh000300", "沪深300"),
    ("s_sh000852", "中证1000"),
)


class SinaProvider:
    source = "sina"

    def __init__(self) -> None:
        self.last_meta: Dict[str, Any] = {}
        self.unavailable_until = 0.0
        self.unavailable_reason = ""

    def fetch_snapshot(self) -> List[RawStock]:
        now = time.time()
        if now < self.unavailable_until:
            remaining = int(self.unavailable_until - now)
            raise RuntimeError(f"新浪接口冷却中，{remaining} 秒后重试：{self.unavailable_reason}")

        started = time.perf_counter()
        full_scan = os.getenv("SINA_FULL_SCAN", "true").lower() not in {"0", "false", "no"}
        rows: List[Dict[str, Any]] = []
        stats: Dict[str, Any] = {}

        if full_scan:
            rows, stats = self._fetch_rows(_full_scan_tasks(), "full")
            if len(rows) < SINA_FULL_MIN_ROWS:
                LOGGER.warning(
                    "sina full snapshot insufficient rows=%s min_rows=%s pages_ok=%s pages_failed=%s fallback=sample",
                    len(rows),
                    SINA_FULL_MIN_ROWS,
                    stats.get("pagesOk", 0),
                    stats.get("pagesFailed", 0),
                )
                rows = []

        if not rows:
            rows, stats = self._fetch_rows(_sample_scan_tasks(), "sample")

        if not rows:
            detail = (stats.get("failures") or ["empty response"])[0]
            if _should_cooldown(stats):
                self.unavailable_until = time.time() + SINA_FAILURE_COOLDOWN_SECONDS
                self.unavailable_reason = detail
            raise RuntimeError(f"新浪接口全部页面失败：{detail}")

        LOGGER.info(
            "sina snapshot done mode=%s pages_ok=%s pages_failed=%s unique_rows=%s elapsed_ms=%.0f",
            stats.get("mode", "unknown"),
            stats.get("pagesOk", 0),
            stats.get("pagesFailed", 0),
            len(rows),
            (time.perf_counter() - started) * 1000,
        )
        self.last_meta = {
            "mode": stats.get("mode", "unknown"),
            "pagesOk": stats.get("pagesOk", 0),
            "pagesFailed": stats.get("pagesFailed", 0),
            "rows": len(rows),
            "minRows": SINA_FULL_MIN_ROWS if stats.get("mode") == "full" else 0,
        }
        context = _build_market_context(rows)
        normalized = [_normalize_row(row, index, context) for index, row in enumerate(rows, start=1)]
        return [item for item in normalized if item is not None]

    def _fetch_rows(self, tasks: List[tuple[str, int]], mode: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen = set()
        page_results: Dict[tuple[str, int], List[Dict[str, Any]]] = {}
        failures: List[str] = []

        with ThreadPoolExecutor(max_workers=min(SINA_MAX_WORKERS, len(tasks))) as executor:
            futures = {executor.submit(self._fetch_page, sort_key, page): (sort_key, page) for sort_key, page in tasks}
            for future in as_completed(futures):
                sort_key, page = futures[future]
                try:
                    page_results[(sort_key, page)] = future.result()
                except Exception as error:  # noqa: BLE001 - keep partial Sina results when possible.
                    LOGGER.warning("sina page failed mode=%s sort=%s page=%s error=%s", mode, sort_key, page, error)
                    failures.append(f"{sort_key}/{page}: {error}")

        for sort_key, page in tasks:
            for row in page_results.get((sort_key, page), []):
                code = str(row.get("code", "")).zfill(6)
                if code and code not in seen:
                    seen.add(code)
                    rows.append(row)

        return rows, {"mode": mode, "pagesOk": len(page_results), "pagesFailed": len(failures), "failures": failures}

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
        equal_weighted_indices = [
            {"name": "主板等权", "change": round(_average_change(stocks, lambda item: item.sector in {"沪市主板", "深市主板"}), 2)},
            {"name": "创业板等权", "change": round(_average_change(stocks, lambda item: item.sector == "创业板"), 2)},
        ]
        indices, index_status = self._fetch_indices()
        return {
            "indices": indices,
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
            "marketIndexStatus": index_status,
            "mode": "aggressive" if avg_change > 1.2 and up > down * 1.5 else "defensive" if avg_change < -0.8 or down > up else "balanced",
        }

    def _fetch_indices(self) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        symbols = ",".join(symbol for symbol, _ in SINA_INDEX_SYMBOLS)
        expected_names = {symbol: name for symbol, name in SINA_INDEX_SYMBOLS}
        url = f"https://hq.sinajs.cn/list={symbols}"
        request = urllib.request.Request(
            url,
            headers={
                "Referer": "https://finance.sina.com.cn",
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=SINA_INDEX_TIMEOUT_SECONDS) as response:
                body = response.read().decode("gbk", errors="ignore")
        except Exception as error:  # noqa: BLE001 - index data is useful, but stock snapshot remains usable.
            LOGGER.warning("sina index fetch failed error=%s", error)
            return [], {"available": False, "source": "sina", "warning": f"新浪真实指数获取失败：{error}"}

        rows: List[Dict[str, Any]] = []
        for line in body.splitlines():
            if "hq_str_" not in line or "=" not in line:
                continue
            key, payload = line.split("=", 1)
            symbol = key.split("hq_str_", 1)[-1].strip()
            fields = payload.strip().strip(";").strip('"').split(",")
            if len(fields) < 4:
                continue
            name = fields[0] or expected_names.get(symbol, symbol)
            change = _num(fields[3])
            rows.append({"name": name, "code": symbol.replace("s_", ""), "change": round(change, 2), "source": "sina"})

        if not rows:
            return [], {"available": False, "source": "sina", "warning": "新浪真实指数返回为空"}

        warning = ""
        if len(rows) < len(SINA_INDEX_SYMBOLS):
            warning = f"新浪真实指数只返回 {len(rows)}/{len(SINA_INDEX_SYMBOLS)} 项"
        return rows, {"available": True, "source": "sina", "warning": warning}

    def _fetch_page(self, sort_key: str, page: int) -> List[Dict[str, Any]]:
        params = {
            "page": str(page),
            "num": "80",
            "sort": sort_key,
            "asc": "0",
            "node": "hs_a",
            "symbol": "",
            "_s_r_a": "init",
        }
        url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={
                "Referer": "https://finance.sina.com.cn",
                "User-Agent": "Mozilla/5.0",
            },
        )
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=SINA_PAGE_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
        data = json.loads(body)
        rows = data if isinstance(data, list) else []
        LOGGER.debug(
            "sina page ok sort=%s page=%s rows=%s elapsed_ms=%.0f",
            sort_key,
            page,
            len(rows),
            (time.perf_counter() - started) * 1000,
        )
        return rows


def _full_scan_tasks() -> List[tuple[str, int]]:
    tasks = [(SINA_FULL_SORT_KEY, page) for page in SINA_FULL_PAGES]
    tasks.extend((sort_key, page) for sort_key in SINA_FULL_EXTRA_SORT_KEYS for page in SINA_FULL_EXTRA_PAGES)
    return tasks


def _sample_scan_tasks() -> List[tuple[str, int]]:
    return [(sort_key, page) for sort_key in SINA_SAMPLE_SORT_KEYS for page in SINA_SAMPLE_PAGES]


def _should_cooldown(stats: Dict[str, Any]) -> bool:
    failures = stats.get("failures") or []
    if len(failures) < 8:
        return False
    blocked = [item for item in failures if "HTTP Error 456" in item]
    return len(blocked) >= max(4, len(failures) // 2)


def _build_market_context(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    amounts = [_num(row.get("amount")) for row in rows if _num(row.get("amount")) > 0]
    changes = [_num(row.get("changepercent")) for row in rows]
    boards: Dict[str, List[float]] = {}
    board_limit_ups: Dict[str, int] = {}

    for row in rows:
        code = str(row.get("code", "")).strip().zfill(6)
        board = _board_name(code)
        boards.setdefault(board, []).append(_num(row.get("changepercent")))
        if _is_limit_up_row(row):
            board_limit_ups[board] = board_limit_ups.get(board, 0) + 1

    ranked_boards = sorted(
        ((board, sum(values) / len(values)) for board, values in boards.items() if values),
        key=lambda item: item[1],
        reverse=True,
    )
    return {
        "medianAmount": _median(amounts) or 1,
        "sortedChanges": sorted(changes),
        "boardPositions": {name: position for position, (name, _) in enumerate(ranked_boards, start=1)},
        "boardLimitUps": board_limit_ups,
    }


def _normalize_row(row: Dict[str, Any], index: int, context: Dict[str, Any]) -> Optional[RawStock]:
    code = str(row.get("code", "")).strip().zfill(6)
    name = str(row.get("name", "")).strip()
    price = _num(row.get("trade"))
    change = _num(row.get("changepercent"))
    turnover = _num(row.get("turnoverratio"))
    amount = _num(row.get("amount"))
    high = _num(row.get("high"))
    low = _num(row.get("low"))
    open_price = _num(row.get("open"))
    prev_close = _num(row.get("settlement"))

    if not code or not name or price <= 0:
        return None

    median_amount = context["medianAmount"]
    amount_factor = max(0.5, min(3.2, amount / median_amount))
    rel_strength = _percentile_rank_sorted(change, context["sortedChanges"])
    board = _board_name(code)
    board_rank = max(1, context["boardPositions"].get(board, 8) - (1 if change >= 8 else 0))
    board_limit_ups = context["boardLimitUps"].get(board, 0)
    limit_status = _limit_status(code, name, price, high, low, open_price, prev_close, change)
    risk_flags = _risk_flags(name, change, turnover, amount, limit_status)

    return RawStock(
        code=code,
        name=name,
        sector=board,
        theme="新浪实时行情",
        price=price,
        change=change,
        turnover=turnover,
        volume_ratio=_volume_proxy(amount_factor, turnover),
        amount_factor=amount_factor,
        sector_rank=board_rank,
        sector_limit_ups=board_limit_ups,
        rel_strength=rel_strength,
        news_score=55,
        risk_flags=risk_flags,
        catalysts=[Catalyst("行情", str(row.get("ticktime", "实时")), f"{name} {code}：新浪行情，成交额 {amount / 100000000:.2f} 亿，涨跌幅 {change:.2f}%")],
        status_bias=_status_bias(change, amount_factor, turnover),
        series_seed=(sum(ord(char) for char in code) % 17) + 1,
        st=("ST" in name.upper()),
        limit_status=limit_status,
        amount=amount,
    )


def _num(value: Any, default: float = 0.0) -> float:
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


def _percentile_rank_sorted(value: float, sorted_values: List[float]) -> float:
    if not sorted_values:
        return 50.0
    below = bisect_right(sorted_values, value)
    return round(below / len(sorted_values) * 100, 2)


def _board_name(code: str) -> str:
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


def _board_rank(board: str, change: float, rows: List[Dict[str, Any]]) -> int:
    averages = []
    for candidate_board in {"沪市主板", "深市主板", "创业板", "科创板", "北交所"}:
        values = [
            _num(row.get("changepercent"))
            for row in rows
            if _board_name(str(row.get("code", "")).strip().zfill(6)) == candidate_board
        ]
        if values:
            averages.append((candidate_board, sum(values) / len(values)))
    ranked = sorted(averages, key=lambda item: item[1], reverse=True)
    positions = {name: position for position, (name, _) in enumerate(ranked, start=1)}
    return max(1, positions.get(board, 8) - (1 if change >= 8 else 0))


def _board_limit_ups(board: str, rows: List[Dict[str, Any]]) -> int:
    return len(
        [
            row
            for row in rows
            if _board_name(str(row.get("code", "")).strip().zfill(6)) == board
            and _is_limit_up_row(row)
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


def _is_limit_up_row(row: Dict[str, Any]) -> bool:
    code = str(row.get("code", "")).strip().zfill(6)
    name = str(row.get("name", "")).strip()
    return _num(row.get("changepercent")) >= _limit_threshold(code, name) * 0.98


def _limit_threshold(code: str, name: str = "") -> float:
    normalized = str(code or "").strip().zfill(6)
    if "ST" in name.upper() and normalized.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return 5.0
    if normalized.startswith(("300", "301", "688")):
        return 20.0
    if normalized.startswith(("4", "8", "9")):
        return 30.0
    return 10.0


def _risk_flags(name: str, change: float, turnover: float, amount: float, limit_status: str) -> List[str]:
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
    if amount <= 50000000:
        flags.append("成交额偏低")
    return flags


def _status_bias(change: float, amount_factor: float, turnover: float) -> str:
    if change >= 4 and amount_factor >= 1.2:
        return "triggered"
    if 1.5 <= change < 5 and turnover >= 1.0:
        return "pullback"
    return "watch"


def _volume_proxy(amount_factor: float, turnover: float) -> float:
    return max(0.8, min(4.0, amount_factor * 0.9 + turnover / 12))
