"""Date-checked, unadjusted observations for the inexpensive 500-candidate audit."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from backend.app.providers.provider_common import symbol_prefix
from backend.app.providers.tencent_provider import TencentProvider

LOGGER = logging.getLogger(__name__)


def valid_bar(bar, target):
    if bar.get("tradeDate") != target or bar.get("priceBasis") != "unadjusted":
        return False
    if bar.get("tradingStatus") in {"suspended", "delisted", "no_trade"}:
        return bool(bar.get("businessStatusConfirmed"))
    try:
        if any(not isinstance(bar[k], (int, float)) or isinstance(bar[k], bool) for k in ("open", "high", "low", "close")):
            return False
        o, h, l, c = [float(bar[k]) for k in ("open", "high", "low", "close")]
        return 0 < l <= min(o, c) <= max(o, c) <= h and all(v < float("inf") for v in (o, h, l, c))
    except (KeyError, TypeError, ValueError):
        return False


def quote_bar(stock, target, source="tencent", version=""):
    if stock.trade_date != target or stock.quote_time < target + "T15:00":
        return None
    bar = {"code": stock.code, "tradeDate": target, "priceBasis": stock.price_basis,
        "open": stock.open, "high": stock.high, "low": stock.low, "close": stock.price,
        "preClose": stock.pre_close, "amount": stock.amount, "tradingStatus": "traded",
        "corporateActionStatus": "unknown", "limitStatus": stock.limit_status,
        "source": source, "sourceSnapshotVersion": version, "asOfTime": stock.quote_time}
    return bar if valid_bar(bar, target) else None


class DailyObservationProvider:
    def fetch(self, target, codes, deadline):
        result = {}
        started = time.monotonic()
        counts, failures = Counter(), Counter()
        metrics_lock = threading.Lock()
        def record(source, retry=False):
            with metrics_lock:
                counts[source] += 1
                counts["requests"] += 1
                counts["retries"] += int(retry)
        # Consume an already atomically published full-market snapshot first.
        from backend.app.market_context_store import FullMarketContextStore
        snapshot = FullMarketContextStore().latest_cached()
        if snapshot.get("status") in {"ready", "partial"} and snapshot.get("tradeDate") == target:
            for stock in snapshot.get("stocks") or []:
                bar = quote_bar(stock, target, snapshot.get("source", ""), snapshot.get("snapshotVersion", ""))
                if bar:
                    result[stock.code] = bar
        missing = [x for x in codes if x not in result]
        snapshot_count = sum(code in result for code in codes)
        requests = 0
        if target == date.today().isoformat() and missing:
            # Shared targeted quote blocks avoid publishing another full-market refresh.
            provider = TencentProvider(timeout_seconds=8)
            batches = [missing[i:i+60] for i in range(0, len(missing), 60)]
            def fetch_batch(batch):
                for attempt in range(2):
                    if time.monotonic() >= deadline - 9:
                        return []
                    try:
                        record("tencent_quote", retry=attempt > 0)
                        return provider.fetch_codes(batch)
                    except Exception as error:
                        with metrics_lock:
                            failures["tencent_quote"] += 1
                        LOGGER.warning("tracking quote block failed: %s", error)
                return []
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(fetch_batch, batch) for batch in batches]
                requests += len(batches)
                for future in as_completed(futures):
                    for stock in future.result():
                        bar = quote_bar(stock, target)
                        if bar:
                            result[stock.code] = bar
        missing = [x for x in codes if x not in result][:50]
        def historical(code):
            for attempt, source in enumerate(("eastmoney", "tencent")):
                if time.monotonic() >= deadline - 9:
                    break
                try:
                    record(source + "_daily", retry=attempt > 0)
                    bar = fetch_history(code, target, source)
                    if bar and valid_bar(bar, target):
                        return code, bar
                    with metrics_lock:
                        failures[source + "_daily"] += 1
                except Exception as error:
                    with metrics_lock:
                        failures[source + "_daily"] += 1
                    LOGGER.warning("tracking daily fallback code=%s source=%s error=%s", code, source, error)
            return code, None
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(historical, code) for code in missing]
            requests += len(missing)
            for future in as_completed(futures):
                code, bar = future.result()
                if bar:
                    result[code] = bar
        selected = {code: bar for code, bar in result.items() if code in codes}
        return selected, {"requestBlocks": requests, "historicalCodeBudget": len(missing),
            "requestCount": counts["requests"], "retryCount": counts["retries"],
            "requestsBySource": {key: value for key, value in counts.items() if key not in {"requests", "retries"}},
            "failuresBySource": dict(failures), "snapshotHitCount": snapshot_count,
            "sourceSnapshotVersion": snapshot.get("snapshotVersion", "") if snapshot_count else "",
            "availableCount": len(selected), "missingCount": len(codes) - len(selected),
            "elapsedMs": round((time.monotonic() - started) * 1000),
            "deadlineReached": time.monotonic() >= deadline - 9}


def fetch_history(code, target, source):
    if source == "eastmoney":
        params = urllib.parse.urlencode({"secid": f"{1 if code.startswith(('6','9')) else 0}.{code}",
            "klt": "101", "fqt": "0", "beg": target.replace("-", ""), "end": target.replace("-", ""),
            "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"})
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + params
    else:
        symbol = symbol_prefix(code) + code
        start = (date.fromisoformat(target) - timedelta(days=10)).isoformat()
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urllib.parse.urlencode({"param": f"{symbol},day,{start},{target},20"})
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read())
    if source == "eastmoney":
        rows = (payload.get("data") or {}).get("klines") or []
        for row in rows:
            p = row.split(",")
            if p[0] == target and len(p) >= 7:
                return {"code": code, "tradeDate": target, "priceBasis": "unadjusted",
                    "open": float(p[1]), "close": float(p[2]), "high": float(p[3]), "low": float(p[4]),
                    "amount": float(p[6]), "preClose": None, "source": "eastmoney_daily_fqt0",
                    "tradingStatus": "traded", "corporateActionStatus": "unknown", "asOfTime": target + "T15:00:00"}
    else:
        # Only the unadjusted 'day' key; never silently substitute qfqday/hfqday.
        rows = ((payload.get("data") or {}).get(symbol) or {}).get("day") or []
        for i, p in enumerate(rows):
            if p[0] == target:
                return {"code": code, "tradeDate": target, "priceBasis": "unadjusted",
                    "open": float(p[1]), "close": float(p[2]), "high": float(p[3]), "low": float(p[4]),
                    "amount": None, "preClose": float(rows[i-1][2]) if i else None,
                    "source": "tencent_daily_unadjusted", "tradingStatus": "traded",
                    "corporateActionStatus": "unknown", "asOfTime": target + "T15:00:00"}
    return None


class TradingCalendar:
    def __init__(self, database, token=""):
        self.database, self.token = database, token
        self._refresh_after = 0

    def next_date(self, recommendation_date, refresh=False):
        calendar = self.database.get_app_setting("t1_tracking_exchange_calendar", {}) or {}
        end = (date.fromisoformat(recommendation_date) + timedelta(days=60)).isoformat()
        if refresh and self.token and time.monotonic() >= self._refresh_after and not (calendar.get("start", "z") <= recommendation_date < calendar.get("end", "")):
            self._refresh_after = time.monotonic() + 600
            body = json.dumps({"api_name": "trade_cal", "token": self.token,
                "params": {"exchange": "SSE", "start_date": recommendation_date.replace("-", ""), "end_date": end.replace("-", "")},
                "fields": "cal_date,is_open"}).encode()
            try:
                req = urllib.request.Request("https://api.tushare.pro", data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=6) as response:
                    payload = json.loads(response.read())
                data = payload.get("data") or {}
                fields = data.get("fields") or []
                records = [dict(zip(fields, row)) for row in data.get("items") or []]
                if len(records) >= 30:
                    opens = [f"{x['cal_date'][:4]}-{x['cal_date'][4:6]}-{x['cal_date'][6:8]}" for x in records if str(x["is_open"]) == "1"]
                    calendar = {"start": recommendation_date, "end": end, "openDates": sorted(opens), "source": "SSE/tushare", "version": datetime.now().isoformat()}
                    self.database.set_app_setting("t1_tracking_exchange_calendar", calendar)
            except Exception as error:
                LOGGER.warning("tracking calendar unavailable: %s", error)
        dates = [x for x in calendar.get("openDates", []) if x > recommendation_date]
        if calendar.get("start", "z") <= recommendation_date < calendar.get("end", "") and dates:
            return min(dates), {"confidence": "authoritative", "source": calendar.get("source"), "version": calendar.get("version")}
        day = date.fromisoformat(recommendation_date) + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return day.isoformat(), {"confidence": "provisional", "source": "next_weekday", "version": "v1"}
