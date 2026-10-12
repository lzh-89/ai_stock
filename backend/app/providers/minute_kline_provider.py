from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List

from backend.app.providers.tencent_kline_provider import TencentKlineProvider


LOGGER = logging.getLogger("ai_stock.provider.minute_kline")

EASTMONEY_MINUTE_TIMEOUT_SECONDS = 6
EASTMONEY_MINUTE_MAX_WORKERS = 10
AKSHARE_MINUTE_MAX_WORKERS = 4


class MinuteKlineProvider:
    source = "multi_minute"

    def __init__(self) -> None:
        self.providers = [
            TencentKlineProvider(),
            EastmoneyMinuteKlineProvider(),
            AKShareMinuteKlineProvider(),
        ]
        self.last_meta: Dict[str, Any] = {}
        self.last_sources_by_code: Dict[str, str] = {}
        self.last_warning = ""

    def fetch_many(self, codes: List[str], period: str = "15m", require_dated: bool = False) -> Dict[str, List[Dict[str, float]]]:
        unique_codes = _unique_codes(codes)
        results: Dict[str, List[Dict[str, float]]] = {}
        self.last_sources_by_code = {}
        provider_meta: List[Dict[str, Any]] = []
        missing = list(unique_codes)
        started = time.perf_counter()

        for provider in self.providers:
            if not missing:
                break
            provider_started = time.perf_counter()
            try:
                batch = provider.fetch_many(missing, period=period)
            except Exception as error:  # noqa: BLE001 - fallback providers are expected to continue.
                meta = {
                    "source": provider.source,
                    "requested": len(missing),
                    "ok": 0,
                    "failed": len(missing),
                    "period": period,
                    "elapsedMs": round((time.perf_counter() - provider_started) * 1000),
                    "warning": str(error),
                }
                provider_meta.append(meta)
                LOGGER.warning("minute kline provider failed source=%s requested=%s error=%s", provider.source, len(missing), error)
                continue

            accepted = 0
            for code, series in batch.items():
                normalized = str(code or "").strip().zfill(6)
                if not normalized or not series or normalized in results:
                    continue
                if require_dated and not _series_has_trade_date(series):
                    continue
                results[normalized] = series
                self.last_sources_by_code[normalized] = provider.source
                accepted += 1
            missing = [code for code in missing if code not in results]
            meta = dict(getattr(provider, "last_meta", {}) or {})
            meta.update(
                {
                    "source": provider.source,
                    "requested": meta.get("requested", len(batch) + len(missing)),
                    "ok": meta.get("ok", accepted),
                    "accepted": accepted,
                    "missingAfter": len(missing),
                    "period": period,
                    "elapsedMs": meta.get("elapsedMs", round((time.perf_counter() - provider_started) * 1000)),
                    "warning": meta.get("warning") or getattr(provider, "last_warning", ""),
                }
            )
            provider_meta.append(meta)
            LOGGER.info(
                "minute kline provider done source=%s accepted=%s missing=%s period=%s",
                provider.source,
                accepted,
                len(missing),
                period,
            )

        source_counts: Dict[str, int] = {}
        for source in self.last_sources_by_code.values():
            source_counts[source] = source_counts.get(source, 0) + 1
        warning = ""
        if missing:
            warning = f"{len(missing)} 只候选未取到真实分钟线"
        if not results:
            warnings = [str(item.get("warning") or "") for item in provider_meta if item.get("warning")]
            warning = "；".join(warnings[:3]) or warning or "所有分钟线源均未返回有效数据"

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        self.last_warning = warning
        self.last_meta = {
            "source": self.source,
            "requested": len(unique_codes),
            "ok": len(results),
            "failed": len(unique_codes) - len(results),
            "period": period,
            "elapsedMs": elapsed_ms,
            "sourceCounts": source_counts,
            "providers": provider_meta,
            "warning": warning,
        }
        LOGGER.info(
            "minute kline multi-source done requested=%s ok=%s failed=%s period=%s sources=%s elapsed_ms=%s",
            len(unique_codes),
            len(results),
            len(unique_codes) - len(results),
            period,
            source_counts,
            elapsed_ms,
        )
        return results

    def source_for_code(self, code: str) -> str:
        return self.last_sources_by_code.get(str(code or "").strip().zfill(6), self.source)


class EastmoneyMinuteKlineProvider:
    source = "eastmoney_minute"

    def __init__(self) -> None:
        self._cache: Dict[tuple[str, str], tuple[float, List[Dict[str, float]]]] = {}
        self.cache_ttl_seconds = 60
        self.last_meta: Dict[str, Any] = {}
        self.last_errors: Dict[str, str] = {}
        self.last_warning = ""

    def fetch_many(self, codes: List[str], period: str = "15m") -> Dict[str, List[Dict[str, float]]]:
        unique_codes = _unique_codes(codes)
        self.last_meta = {}
        self.last_errors = {}
        self.last_warning = ""
        results: Dict[str, List[Dict[str, float]]] = {}
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=min(EASTMONEY_MINUTE_MAX_WORKERS, max(1, len(unique_codes)))) as executor:
            futures = {executor.submit(self.fetch_series, code, period): code for code in unique_codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    series = future.result()
                except Exception as error:  # noqa: BLE001 - multi-source provider will continue.
                    self.last_errors[code] = str(error)
                    continue
                if series:
                    results[code] = series
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if unique_codes and not results and self.last_errors:
            sample = "; ".join(list(self.last_errors.values())[:3])
            self.last_warning = f"东方财富分钟线全部失败：{sample}"
        elif self.last_errors:
            self.last_warning = f"东方财富分钟线部分失败：{len(self.last_errors)} 只"
        self.last_meta = {
            "source": self.source,
            "requested": len(unique_codes),
            "ok": len(results),
            "failed": len(unique_codes) - len(results),
            "period": period,
            "elapsedMs": elapsed_ms,
            "warning": self.last_warning,
            "errorSample": list(self.last_errors.values())[:5],
        }
        LOGGER.info(
            "eastmoney minute kline batch done requested=%s ok=%s period=%s elapsed_ms=%s",
            len(unique_codes),
            len(results),
            period,
            elapsed_ms,
        )
        return results

    def fetch_series(self, code: str, period: str = "15m") -> List[Dict[str, float]]:
        code = str(code or "").strip().zfill(6)
        cache_key = (code, period)
        cached = self._cache.get(cache_key)
        if cached and time.time() - cached[0] <= self.cache_ttl_seconds:
            return cached[1]
        params = {
            "secid": _eastmoney_secid(code),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": str(_period_minutes(period)),
            "fqt": "0",
            "end": "20500101",
            "lmt": str(_eastmoney_limit(period)),
        }
        url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=EASTMONEY_MINUTE_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="ignore")
        payload = json.loads(body)
        rows = ((payload.get("data") or {}).get("klines") or []) if isinstance(payload, dict) else []
        series = _eastmoney_rows_to_series(rows)
        if series:
            self._cache[cache_key] = (time.time(), series)
        return series


class AKShareMinuteKlineProvider:
    source = "akshare_eastmoney_minute"

    def __init__(self) -> None:
        self.last_meta: Dict[str, Any] = {}
        self.last_errors: Dict[str, str] = {}
        self.last_warning = ""
        try:
            import akshare as ak  # type: ignore
        except ModuleNotFoundError:
            self.ak = None
        else:
            self.ak = ak

    def fetch_many(self, codes: List[str], period: str = "15m") -> Dict[str, List[Dict[str, float]]]:
        unique_codes = _unique_codes(codes)
        self.last_meta = {}
        self.last_errors = {}
        self.last_warning = ""
        started = time.perf_counter()
        if self.ak is None:
            self.last_warning = "AKShare 未安装，无法获取东财分钟线"
            self.last_meta = {
                "source": self.source,
                "requested": len(unique_codes),
                "ok": 0,
                "failed": len(unique_codes),
                "period": period,
                "elapsedMs": 0,
                "warning": self.last_warning,
            }
            return {}

        results: Dict[str, List[Dict[str, float]]] = {}
        with ThreadPoolExecutor(max_workers=min(AKSHARE_MINUTE_MAX_WORKERS, max(1, len(unique_codes)))) as executor:
            futures = {executor.submit(self.fetch_series, code, period): code for code in unique_codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    series = future.result()
                except Exception as error:  # noqa: BLE001 - multi-source provider will continue.
                    self.last_errors[code] = str(error)
                    continue
                if series:
                    results[code] = series

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if unique_codes and not results and self.last_errors:
            sample = "; ".join(list(self.last_errors.values())[:3])
            self.last_warning = f"AKShare东财分钟线全部失败：{sample}"
        elif self.last_errors:
            self.last_warning = f"AKShare东财分钟线部分失败：{len(self.last_errors)} 只"
        self.last_meta = {
            "source": self.source,
            "requested": len(unique_codes),
            "ok": len(results),
            "failed": len(unique_codes) - len(results),
            "period": period,
            "elapsedMs": elapsed_ms,
            "warning": self.last_warning,
            "errorSample": list(self.last_errors.values())[:5],
        }
        LOGGER.info("akshare minute kline batch done requested=%s ok=%s period=%s elapsed_ms=%s", len(unique_codes), len(results), period, elapsed_ms)
        return results

    def fetch_series(self, code: str, period: str = "15m") -> List[Dict[str, float]]:
        if self.ak is None:
            return []
        normalized = str(code or "").strip().zfill(6)
        end_date = date.today().isoformat()
        start_date = (date.today() - timedelta(days=7)).isoformat()
        dataframe = self.ak.stock_zh_a_hist_min_em(
            symbol=normalized,
            start_date=start_date,
            end_date=end_date,
            period=str(_period_minutes(period)),
            adjust="",
        )
        return _dataframe_to_series(dataframe)


def _unique_codes(codes: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for code in codes:
        normalized = str(code or "").strip().zfill(6)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _series_has_trade_date(series: List[Dict[str, Any]]) -> bool:
    for item in series:
        text = str((item or {}).get("time") or (item or {}).get("datetime") or (item or {}).get("date") or "")
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return True
    return False


def _eastmoney_secid(code: str) -> str:
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    return f"{market}.{code}"


def _eastmoney_rows_to_series(rows: List[Any]) -> List[Dict[str, float]]:
    series: List[Dict[str, float]] = []
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < 7:
            continue
        try:
            item = {
                "time": parts[0],
                "timeLabel": "end",
                "open": _finite_float(parts[1]),
                "close": _finite_float(parts[2]),
                "high": _finite_float(parts[3]),
                "low": _finite_float(parts[4]),
                "volume": _finite_float(parts[5]),
                "amount": _finite_float(parts[6]),
            }
        except ValueError:
            continue
        if item["open"] <= 0 or item["close"] <= 0:
            continue
        series.append(item)
    return series


def _eastmoney_limit(period: str) -> int:
    minutes = _period_minutes(period)
    if minutes <= 1:
        return 1200
    if minutes <= 5:
        return 800
    return 256


def _dataframe_to_series(dataframe: Any) -> List[Dict[str, float]]:
    if not hasattr(dataframe, "to_dict"):
        return []
    rows = dataframe.to_dict("records")
    series: List[Dict[str, float]] = []
    for row in rows:
        item = _row_to_series_item(row)
        if item:
            series.append(item)
    return series


def _row_to_series_item(row: Dict[str, Any]) -> Dict[str, float]:
    try:
        item = {
            "time": str(row.get("时间") or row.get("datetime") or row.get("date") or ""),
            "open": _finite_float(row.get("开盘", row.get("open"))),
            "close": _finite_float(row.get("收盘", row.get("close"))),
            "high": _finite_float(row.get("最高", row.get("high"))),
            "low": _finite_float(row.get("最低", row.get("low"))),
            "volume": _finite_float(row.get("成交量", row.get("volume")), default=0.0),
            "amount": _finite_float(row.get("成交额", row.get("amount")), default=0.0),
        }
    except ValueError:
        return {}
    if item["open"] <= 0 or item["close"] <= 0:
        return {}
    return item


def _finite_float(value: Any, default: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise ValueError(f"invalid number: {value}")
    if math.isnan(number) or math.isinf(number):
        if default is not None:
            return default
        raise ValueError(f"invalid number: {value}")
    return number


def _period_minutes(period: str) -> int:
    normalized = str(period or "15m").strip().lower()
    if normalized.endswith("min"):
        normalized = normalized[:-3]
    elif normalized.endswith("m"):
        normalized = normalized[:-1]
    try:
        value = int(normalized)
    except ValueError:
        value = 15
    return value if value in {1, 5, 15, 30, 60} else 15
