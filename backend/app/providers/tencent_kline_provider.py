from __future__ import annotations

import json
import logging
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

from backend.app.providers.provider_common import symbol_prefix


LOGGER = logging.getLogger("ai_stock.provider.tencent_kline")

TENCENT_MINUTE_TIMEOUT_SECONDS = 8
TENCENT_KLINE_MAX_WORKERS = 8


class TencentKlineProvider:
    source = "tencent_minute"

    def __init__(self) -> None:
        self._cache: Dict[tuple[str, str], tuple[float, List[Dict[str, float]]]] = {}
        self.cache_ttl_seconds = 60
        self.last_meta: Dict[str, Any] = {}
        self.last_errors: Dict[str, str] = {}
        self.last_warning = ""
        self._trade_dates: Dict[str, str] = {}

    def fetch_many(self, codes: List[str], period: str = "15m") -> Dict[str, List[Dict[str, float]]]:
        unique_codes = []
        seen = set()
        for code in codes:
            normalized = code.strip().zfill(6)
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_codes.append(normalized)

        self.last_meta = {}
        self.last_errors = {}
        self.last_warning = ""
        results: Dict[str, List[Dict[str, float]]] = {}
        for code in unique_codes:
            self._trade_dates.pop(code, None)
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=min(TENCENT_KLINE_MAX_WORKERS, max(1, len(unique_codes)))) as executor:
            futures = {executor.submit(self.fetch_series, code, period): code for code in unique_codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    series = future.result()
                except Exception as error:  # noqa: BLE001 - kline enrichment is best effort.
                    self.last_errors[code] = str(error)
                    LOGGER.warning("tencent minute kline failed code=%s error=%s", code, error)
                    continue
                if series:
                    results[code] = series

        elapsed_ms = (time.perf_counter() - started) * 1000
        if unique_codes and not results and self.last_errors:
            sample = "; ".join(list(self.last_errors.values())[:3])
            self.last_warning = f"腾讯分钟线全部失败：{sample}"
        elif self.last_errors:
            self.last_warning = f"腾讯分钟线部分失败：{len(self.last_errors)} 只"
        self.last_meta = {
            "source": self.source,
            "requested": len(unique_codes),
            "ok": len(results),
            "failed": len(unique_codes) - len(results),
            "period": period,
            "elapsedMs": round(elapsed_ms),
            "warning": self.last_warning,
            "errorSample": list(self.last_errors.values())[:5],
        }
        LOGGER.info(
            "tencent minute kline batch done requested=%s ok=%s period=%s elapsed_ms=%.0f",
            len(unique_codes),
            len(results),
            period,
            elapsed_ms,
        )
        return results

    def fetch_series(self, code: str, period: str = "15m") -> List[Dict[str, float]]:
        code = code.strip().zfill(6)
        cache_key = (code, period)
        cached = self._cache.get(cache_key)
        if cached and time.time() - cached[0] <= self.cache_ttl_seconds:
            return cached[1]

        raw = self._fetch_minute_rows(code)
        minute_series = _minute_rows_to_series(raw)
        trade_date = self._trade_dates.get(code, "")
        if trade_date:
            for point in minute_series:
                point["time"] = f"{trade_date} {point['time']}"
                point["timeLabel"] = "end"
        series = _aggregate_series(minute_series, _period_minutes(period))
        if series:
            self._cache[cache_key] = (time.time(), series)
        return series

    def _fetch_minute_rows(self, code: str) -> List[str]:
        symbol = f"{symbol_prefix(code)}{code}"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
        request = urllib.request.Request(url, headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=TENCENT_MINUTE_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="ignore")
        data = json.loads(body)
        container = data.get("data", {}).get(symbol, {}).get("data", {})
        raw_date = str(container.get("date") or "")
        if len(raw_date) == 8 and raw_date.isdigit():
            self._trade_dates[code] = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        elif len(raw_date) == 10:
            self._trade_dates[code] = raw_date
        rows = container.get("data", [])
        return rows if isinstance(rows, list) else []


def _minute_rows_to_series(rows: List[str]) -> List[Dict[str, float]]:
    series: List[Dict[str, float]] = []
    prev_price = 0.0
    prev_volume = 0.0
    prev_amount = 0.0

    for row in rows:
        parts = str(row).split()
        if len(parts) < 4:
            continue
        try:
            item_time = _normalize_tencent_minute_time(parts[0])
            price = float(parts[1])
            cumulative_volume = float(parts[2])
            cumulative_amount = float(parts[3])
        except ValueError:
            continue
        if price <= 0:
            continue

        open_price = prev_price or price
        volume = max(0.0, cumulative_volume - prev_volume)
        amount = max(0.0, cumulative_amount - prev_amount)
        series.append(
            {
                "time": item_time,
                "open": open_price,
                "high": max(open_price, price),
                "low": min(open_price, price),
                "close": price,
                "volume": volume,
                "amount": amount,
            }
        )
        prev_price = price
        prev_volume = cumulative_volume
        prev_amount = cumulative_amount

    return series


def _aggregate_series(series: List[Dict[str, float]], period_minutes: int) -> List[Dict[str, float]]:
    if not series:
        return []
    if period_minutes <= 1:
        return series

    aggregated: List[Dict[str, float]] = []
    for start in range(0, len(series), period_minutes):
        bucket = series[start : start + period_minutes]
        if not bucket:
            continue
        aggregated.append(
            {
                "open": bucket[0]["open"],
                "high": max(item["high"] for item in bucket),
                "low": min(item["low"] for item in bucket),
                "close": bucket[-1]["close"],
                "time": bucket[-1].get("time", ""),
                "timeLabel": "end",
                "bucketStart": bucket[0].get("time", ""),
                "bucketEnd": bucket[-1].get("time", ""),
                "bucketComplete": len(bucket) == period_minutes,
                "volume": sum(item["volume"] for item in bucket),
                "amount": sum(item.get("amount", 0.0) for item in bucket),
            }
        )
    return aggregated


def _normalize_tencent_minute_time(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        return f"{text[:2]}:{text[2:4]}"
    return text


def _period_minutes(period: str) -> int:
    normalized = str(period or "15m").strip().lower()
    if normalized.endswith("m"):
        try:
            return max(1, int(normalized[:-1]))
        except ValueError:
            return 15
    return 15
