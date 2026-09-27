from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from datetime import date, timedelta
from typing import Any, Dict, List

from backend.app.providers.provider_common import baostock_code, symbol_prefix, ts_code
from backend.app.settings import PROJECT_ROOT


LOGGER = logging.getLogger("ai_stock.provider.daily_kline")
DAILY_KLINE_CACHE_PATH = PROJECT_ROOT / "data" / "daily_kline_cache.json"
TENCENT_DAILY_TIMEOUT_SECONDS = 8
TENCENT_DAILY_MAX_WORKERS = 8
EASTMONEY_DAILY_TIMEOUT_SECONDS = 8
EASTMONEY_DAILY_MAX_WORKERS = 16


class DailyKlineProvider:
    source = "daily_kline"

    def __init__(self, tushare_token: str = "") -> None:
        self.tushare_token = tushare_token
        self.cache_ttl_seconds = 60 * 60 * 24
        self._cache: Dict[tuple[str, int], tuple[float, List[Dict[str, float]]]] = {}
        self._disk_loaded = False
        self.last_source = ""
        self.last_warning = ""
        self.last_data_date = ""
        try:
            import baostock as bs  # type: ignore
        except ModuleNotFoundError:
            self.bs = None
        else:
            self.bs = bs
        try:
            import tushare as ts  # type: ignore
        except ModuleNotFoundError:
            self.ts = None
        else:
            self.ts = ts

    def fetch_many(
        self,
        codes: List[str],
        days: int = 180,
        force: bool = False,
        cache_only: bool = False,
        fetcher_timeout_seconds: float | None = None,
        preferred_sources: List[str] | None = None,
    ) -> Dict[str, List[Dict[str, float]]]:
        self.last_source = ""
        self.last_warning = ""
        self.last_data_date = ""
        normalized = [code.strip().zfill(6) for code in codes if code.strip()]
        cached = {} if force else self._read_cache(normalized, days)
        missing = [code for code in normalized if code not in cached]
        if not missing:
            self.last_source = "cache_daily"
            self.last_data_date = _latest_date(cached)
            return cached
        if cache_only:
            self.last_source = "cache_daily"
            self.last_data_date = _latest_date(cached)
            self.last_warning = f"{len(missing)} 只候选未命中日 K 缓存，已跳过现场补历史"
            return cached

        fetched: Dict[str, List[Dict[str, float]]] = {}
        warnings: List[str] = []
        fetchers = {
            "tencent": self._fetch_tencent,
            "eastmoney": self._fetch_eastmoney,
            "baostock": self._fetch_baostock,
            "tushare": self._fetch_tushare,
        }
        source_order = preferred_sources or ["tencent", "eastmoney", "baostock", "tushare"]
        for source_name in source_order:
            fetcher = fetchers.get(str(source_name))
            if not fetcher:
                continue
            if not missing:
                break
            started = time.perf_counter()
            LOGGER.info(
                "daily kline provider start fetcher=%s requested=%s timeout_seconds=%s",
                fetcher.__name__,
                len(missing),
                fetcher_timeout_seconds or "",
            )
            try:
                batch = _call_fetcher(fetcher, missing, days, fetcher_timeout_seconds)
            except Exception as error:  # noqa: BLE001 - provider fallback is expected here.
                warnings.append(str(error))
                LOGGER.warning(
                    "daily kline provider failed fetcher=%s requested=%s elapsed_ms=%.0f error=%s",
                    fetcher.__name__,
                    len(missing),
                    (time.perf_counter() - started) * 1000,
                    error,
                )
                continue
            if batch:
                fetched.update(batch)
                missing = [code for code in missing if code not in fetched]
            LOGGER.info(
                "daily kline provider done fetcher=%s requested=%s ok=%s missing=%s elapsed_ms=%.0f",
                fetcher.__name__,
                len(codes),
                len(batch or {}),
                len(missing),
                (time.perf_counter() - started) * 1000,
            )

        for code, series in fetched.items():
            self._cache[(code, days)] = (time.time(), series)
        if fetched:
            self._write_disk_cache(fetched, days)
        result = {**cached, **fetched}
        self.last_data_date = _latest_date(result)
        if warnings and not result:
            self.last_warning = "；".join(warnings)
        elif warnings:
            self.last_warning = "；".join(warnings[:2])
        if not self.last_source and result:
            self.last_source = "mixed_daily"
        return result

    def _fetch_tencent(self, codes: List[str], days: int) -> Dict[str, List[Dict[str, float]]]:
        result: Dict[str, List[Dict[str, float]]] = {}
        errors: List[str] = []
        with ThreadPoolExecutor(max_workers=min(TENCENT_DAILY_MAX_WORKERS, max(1, len(codes)))) as executor:
            futures = {executor.submit(_fetch_tencent_daily_series, code, days): code for code in codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    series = future.result()
                except Exception as error:  # noqa: BLE001 - fallback providers can still fill the gap.
                    if len(errors) < 5:
                        errors.append(f"{code}: {error}")
                    continue
                if len(series) >= 40:
                    result[code] = series[-days:]
        if errors:
            LOGGER.warning(
                "tencent daily kline partial/failed requested=%s failed_sample=%s%s",
                len(codes),
                "；".join(errors),
                "；..." if len(result) + len(errors) < len(codes) else "",
            )
        if result:
            self.last_source = "tencent_daily"
        return result

    def _fetch_eastmoney(self, codes: List[str], days: int) -> Dict[str, List[Dict[str, float]]]:
        result: Dict[str, List[Dict[str, float]]] = {}
        errors: List[str] = []
        unique_codes = [code.strip().zfill(6) for code in codes if code.strip()]
        with ThreadPoolExecutor(max_workers=min(EASTMONEY_DAILY_MAX_WORKERS, max(1, len(unique_codes)))) as executor:
            futures = {executor.submit(_fetch_eastmoney_daily_series, code, days): code for code in unique_codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    series = future.result()
                except Exception as error:  # noqa: BLE001 - fallback providers can still fill the gap.
                    if len(errors) < 5:
                        errors.append(f"{code}: {error}")
                    continue
                if len(series) >= 40:
                    result[code] = series[-days:]
        if errors:
            LOGGER.warning(
                "eastmoney daily kline partial/failed requested=%s failed_sample=%s%s",
                len(unique_codes),
                "；".join(errors),
                "；..." if len(result) + len(errors) < len(unique_codes) else "",
            )
        if result:
            self.last_source = "eastmoney_daily"
        return result

    def _read_cache(self, codes: List[str], days: int) -> Dict[str, List[Dict[str, float]]]:
        self._load_disk_cache()
        now = time.time()
        result: Dict[str, List[Dict[str, float]]] = {}
        for code in codes:
            cached = self._cache.get((code, days))
            if cached and now - cached[0] <= self.cache_ttl_seconds:
                result[code] = cached[1]
        return result

    def _load_disk_cache(self) -> None:
        if self._disk_loaded:
            return
        self._disk_loaded = True
        if not DAILY_KLINE_CACHE_PATH.exists():
            return
        try:
            with DAILY_KLINE_CACHE_PATH.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            return
        items = payload.get("items", {}) if isinstance(payload, dict) else {}
        if not isinstance(items, dict):
            return
        now = time.time()
        for code, item in items.items():
            if not isinstance(item, dict):
                continue
            series = item.get("series")
            days = int(item.get("days", 180))
            adjustment = str(item.get("adjustment") or "")
            if adjustment != "none":
                continue
            if not isinstance(series, list) or len(series) < 40:
                continue
            updated_at = float(item.get("updatedAtEpoch", now))
            self._cache[(str(code).zfill(6), days)] = (updated_at, series)

    def _write_disk_cache(self, fetched: Dict[str, List[Dict[str, float]]], days: int) -> None:
        payload = _read_disk_payload()
        items = payload.setdefault("items", {})
        now = time.time()
        for code, series in fetched.items():
            items[code] = {
                "days": days,
                "source": self.last_source or "daily_kline",
                "adjustment": "none",
                "updatedAt": date.today().isoformat(),
                "updatedAtEpoch": now,
                "dataDate": str(series[-1].get("date", "")) if series else "",
                "series": series,
            }
        payload["updatedAt"] = date.today().isoformat()
        DAILY_KLINE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = DAILY_KLINE_CACHE_PATH.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
        temp_path.replace(DAILY_KLINE_CACHE_PATH)

    def _fetch_baostock(self, codes: List[str], days: int) -> Dict[str, List[Dict[str, float]]]:
        if not self.bs:
            raise RuntimeError("BaoStock 未安装，无法拉取日 K")
        login_result = self.bs.login()
        if getattr(login_result, "error_code", "0") != "0":
            raise RuntimeError(f"BaoStock 登录失败：{getattr(login_result, 'error_msg', '')}")
        try:
            start, end = _history_range(days)
            fields = "date,code,open,high,low,close,preclose,volume,amount,pctChg,turn"
            result: Dict[str, List[Dict[str, float]]] = {}
            for code in codes:
                query = self.bs.query_history_k_data_plus(
                    baostock_code(code),
                    fields,
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag="3",
                )
                rows: List[Dict[str, float]] = []
                while query.error_code == "0" and query.next():
                    row = dict(zip(query.fields, query.get_row_data()))
                    point = _baostock_point(row)
                    if point:
                        rows.append(point)
                if len(rows) >= 40:
                    result[code] = rows[-days:]
            if result:
                self.last_source = "baostock_daily"
            return result
        finally:
            self.bs.logout()

    def _fetch_tushare(self, codes: List[str], days: int) -> Dict[str, List[Dict[str, float]]]:
        if not self.ts or not self.tushare_token:
            raise RuntimeError("Tushare token or package is missing，无法拉取日 K")
        pro = self.ts.pro_api(self.tushare_token)
        start, end = _history_range(days)
        start_key = start.replace("-", "")
        end_key = end.replace("-", "")
        result: Dict[str, List[Dict[str, float]]] = {}
        for code in codes:
            dataframe = pro.daily(ts_code=ts_code(code), start_date=start_key, end_date=end_key)
            if dataframe is None or dataframe.empty:
                continue
            rows = dataframe.sort_values("trade_date").to_dict("records")
            points = [_tushare_point(row) for row in rows]
            series = [point for point in points if point]
            if len(series) >= 40:
                result[code] = series[-days:]
        if result:
            self.last_source = "tushare_daily"
        return result


def _call_fetcher(fetcher: Any, codes: List[str], days: int, timeout_seconds: float | None) -> Dict[str, List[Dict[str, float]]]:
    if not timeout_seconds or timeout_seconds <= 0:
        return fetcher(codes, days)
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fetcher, codes, days)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as error:
        future.cancel()
        raise TimeoutError(f"{fetcher.__name__} 超过 {timeout_seconds:.0f}s 未返回") from error
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _fetch_tencent_daily_series(code: str, days: int) -> List[Dict[str, float]]:
    normalized = code.strip().zfill(6)
    symbol = f"{symbol_prefix(normalized)}{normalized}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{days}"
    request = urllib.request.Request(url, headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=TENCENT_DAILY_TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8", errors="ignore")
    data = json.loads(body)
    item = data.get("data", {}).get(symbol, {}) if isinstance(data, dict) else {}
    rows = item.get("day") or []
    series = [_tencent_point(row) for row in rows if isinstance(row, list)]
    return [point for point in series if point]


def _fetch_eastmoney_daily_series(code: str, days: int) -> List[Dict[str, float]]:
    normalized = code.strip().zfill(6)
    params = {
        "secid": _eastmoney_secid(normalized),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": "0",
        "end": "20500101",
        "lmt": str(max(days + 20, 80)),
    }
    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Accept-Encoding": "identity",
            "Referer": "https://quote.eastmoney.com/",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(request, timeout=EASTMONEY_DAILY_TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8", errors="ignore")
    payload = json.loads(body)
    rows = ((payload.get("data") or {}).get("klines") or []) if isinstance(payload, dict) else []
    series = [_eastmoney_point(row) for row in rows if isinstance(row, str)]
    return [point for point in series if point]


def _tencent_point(row: List[Any]) -> Dict[str, float]:
    if len(row) < 6:
        return {}
    close = _num(row[2])
    if close <= 0:
        return {}
    volume_lot = _num(row[5])
    return {
        "date": str(row[0]),
        "open": _num(row[1]),
        "high": _num(row[3]),
        "low": _num(row[4]),
        "close": close,
        "preClose": 0.0,
        "volume": volume_lot,
        "amount": volume_lot * close * 100,
        "change": 0.0,
        "turnover": 0.0,
    }


def _eastmoney_point(row: str) -> Dict[str, float]:
    parts = row.split(",")
    if len(parts) < 11:
        return {}
    close = _num(parts[2])
    if close <= 0:
        return {}
    return {
        "date": parts[0],
        "open": _num(parts[1]),
        "close": close,
        "high": _num(parts[3]),
        "low": _num(parts[4]),
        "volume": _num(parts[5]),
        "amount": _num(parts[6]),
        "amplitude": _num(parts[7]),
        "change": _num(parts[8]),
        "changeAmount": _num(parts[9]),
        "turnover": _num(parts[10]),
    }


def _eastmoney_secid(code: str) -> str:
    normalized = str(code or "").strip().zfill(6)
    market = "1" if normalized.startswith(("5", "6", "9")) else "0"
    return f"{market}.{normalized}"


def _history_range(days: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=max(days * 2, 260))
    return start.isoformat(), end.isoformat()


def _read_disk_payload() -> Dict[str, Any]:
    if not DAILY_KLINE_CACHE_PATH.exists():
        return {"version": 1, "items": {}}
    try:
        with DAILY_KLINE_CACHE_PATH.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "items": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "items": {}}
    payload.setdefault("items", {})
    return payload


def _baostock_point(row: Dict[str, Any]) -> Dict[str, float]:
    close = _num(row.get("close"))
    if close <= 0:
        return {}
    return {
        "date": str(row.get("date", "")),
        "open": _num(row.get("open")),
        "high": _num(row.get("high")),
        "low": _num(row.get("low")),
        "close": close,
        "preClose": _num(row.get("preclose")),
        "volume": _num(row.get("volume")),
        "amount": _num(row.get("amount")),
        "change": _num(row.get("pctChg")),
        "turnover": _num(row.get("turn")),
    }


def _tushare_point(row: Dict[str, Any]) -> Dict[str, float]:
    close = _num(row.get("close"))
    if close <= 0:
        return {}
    amount = _num(row.get("amount")) * 1000
    return {
        "date": str(row.get("trade_date", "")),
        "open": _num(row.get("open")),
        "high": _num(row.get("high")),
        "low": _num(row.get("low")),
        "close": close,
        "preClose": _num(row.get("pre_close")),
        "volume": _num(row.get("vol")),
        "amount": amount,
        "change": _num(row.get("pct_chg")),
        "turnover": 0.0,
    }


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _latest_date(series_by_code: Dict[str, List[Dict[str, float]]]) -> str:
    dates = [str(series[-1].get("date", "")) for series in series_by_code.values() if series]
    return max(dates) if dates else ""
