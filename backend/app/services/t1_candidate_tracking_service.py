"""Background next-day proxy tracking, separate from official trade outcomes."""
from __future__ import annotations

import json
import logging
import math
import os
import socket
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import mean, median

from backend.app.providers.t1_daily_observation_provider import DailyObservationProvider, TradingCalendar, valid_bar
from backend.app.t1_candidate_store import CandidateStore, encode, now_text, transaction

LOGGER = logging.getLogger(__name__)
METRIC_VERSION = "2026-10-09.proxy.v1"


def candidate_metric(candidate, bar, target, recommendation_close=None):
    result = {"code": candidate["code"], "targetTradeDate": target, "version": METRIC_VERSION,
        "frozenScanPrice": candidate.get("frozenScanPrice"), "priceBasis": "unadjusted",
        "status": "missing", "exclusionReasons": [], "openProxyReturnPct": None,
        "closeProxyReturnPct": None, "highOpportunityPct": None, "lowAdversePct": None,
        "overnightGapPct": None, "aggregateEligible": False}
    if not bar or not valid_bar(bar, target):
        result["exclusionReasons"] = ["目标日未复权行情缺失或不合格"]
        return result
    result.update(dailyBar=bar, dailyBarSource=bar.get("source"), dailyBarTradeDate=bar["tradeDate"],
                  corporateActionStatus=bar.get("corporateActionStatus", "unknown"))
    if bar.get("tradingStatus") in {"suspended", "delisted", "no_trade"}:
        result.update(status="business_resolved", exclusionReasons=[bar["tradingStatus"]])
        return result
    price = candidate.get("frozenScanPrice")
    exclusions = []
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        exclusions.append("冻结扫描价缺失")
    if candidate.get("priceBasis") != bar["priceBasis"]:
        exclusions.append("价格口径不一致")
    if bar.get("corporateActionStatus") in {"ex_right", "ex_dividend", "confirmed", "suspected"}:
        exclusions.append("除权除息")
    if price and (not .5 <= bar["close"] / price <= 1.5 or
                  (bar.get("preClose") and abs(bar["preClose"] / price - 1) > .20)):
        exclusions.append("价格异常或疑似除权")
    if recommendation_close and bar.get("preClose") and abs(bar["preClose"] / recommendation_close - 1) > .01:
        exclusions.append("前收盘口径异常或疑似除权")
    if exclusions:
        result.update(status="excluded", exclusionReasons=exclusions)
        return result
    result.update(status="available", aggregateEligible=True)
    for key, field in (("openProxyReturnPct", "open"), ("closeProxyReturnPct", "close"),
                       ("highOpportunityPct", "high"), ("lowAdversePct", "low")):
        result[key] = round((bar[field] / price - 1) * 100, 6)
    if recommendation_close:
        result["overnightGapPct"] = round((bar["open"] / recommendation_close - 1) * 100, 6)
    # One-price limit bars describe opportunity but cannot be counted as executable wins.
    if bar["high"] == bar["low"] and bar.get("preClose") and abs(bar["close"] / bar["preClose"] - 1) > .045:
        result.update(aggregateEligible=False, exclusionReasons=["一字涨跌停：成交不可保证"])
    return result


def distribution(items, field):
    values = sorted(x[field] for x in items if x.get("aggregateEligible") and x.get(field) is not None)
    def quantile(q):
        if not values:
            return None
        pos = (len(values) - 1) * q
        lo, hi = math.floor(pos), math.ceil(pos)
        return values[lo] + (values[hi] - values[lo]) * (pos - lo)
    return {"sampleCount": len(values), "meanPct": mean(values) if values else None,
            "medianPct": median(values) if values else None, "p25Pct": quantile(.25), "p75Pct": quantile(.75),
            "positiveProxyRatioPct": sum(v > 0 for v in values) / len(values) * 100 if values else None}


class CandidateTrackingService:
    def __init__(self, database, provider=None, calendar=None, token=""):
        self.database = database
        self.store = CandidateStore(database)
        self.provider = provider or DailyObservationProvider()
        self.calendar = calendar or TradingCalendar(database, token)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    def ensure_job(self, run_id):
        existing = self.store.job(run_id)
        if existing:
            return existing
        candidates = self.store.all(run_id)
        run = self.database.load_t1_overnight_selection_run(run_id)
        if not candidates or not run:
            return None
        target, calendar = self.calendar.next_date(run["recommendationDate"])
        stamp = now_text()
        with transaction(self.database) as conn:
            conn.execute("""INSERT OR IGNORE INTO t1_overnight_next_day_tracking_jobs
              (run_id,target_trade_date,status,expected_count,calendar_json,created_at,updated_at)
              VALUES(?,?,'pending_target',?,?,?,?)""", (run_id, target, len(candidates), encode(calendar), stamp, stamp))
        return self.store.job(run_id)

    def discover(self):
        with transaction(self.database) as conn:
            ids = [row[0] for row in conn.execute("""SELECT DISTINCT a.run_id FROM t1_overnight_candidate_archives a
                JOIN t1_overnight_selection_runs r ON r.id=a.run_id
                LEFT JOIN t1_overnight_next_day_tracking_jobs j ON j.run_id=a.run_id
                WHERE j.run_id IS NULL AND r.status NOT IN ('pending','running')""").fetchall()]
        for run_id in ids:
            self.store.sync_decisions(run_id)
            self.ensure_job(run_id)

    def start_scheduler(self):
        if os.getenv("T1_CANDIDATE_TRACKING_ENABLED", "1").lower() in {"0", "false", "no"}:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="t1-candidate-next-day", daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                LOGGER.exception("candidate next-day scheduler failed")
            self._stop.wait(60)

    def retry(self, run_id):
        job = self.ensure_job(run_id)
        if not job:
            raise ValueError("旧版本 Run 未保存候选档案，无法补建筛选过程")
        if job["status"] == "complete":
            return {"accepted": False, "status": "complete", "message": "行情已完整，无需重复抓取"}
        with transaction(self.database) as conn:
            conn.execute("UPDATE t1_overnight_next_day_tracking_jobs SET next_retry_at='',status='pending_target' WHERE run_id=? AND lease_expires_at<?",
                         (run_id, now_text()))
        # Handler returns immediately. SQL leases coordinate with the scheduled worker.
        threading.Thread(target=self.tick, name="t1-candidate-manual-repair", daemon=True).start()
        return {"accepted": True, "message": "已提交补齐任务，仅补缺失的目标日日行情"}

    def tick(self, now=None):
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self._tick(now)
        finally:
            self._tick_lock.release()

    def _tick(self, now=None):
        now = now or datetime.now()
        stamp = now.isoformat(timespec="seconds")
        self.discover()
        with transaction(self.database) as conn:
            jobs = [dict(x) for x in conn.execute("""SELECT * FROM t1_overnight_next_day_tracking_jobs
                WHERE status NOT IN ('complete','terminal_missing') AND lease_expires_at<? AND next_retry_at<=?
                ORDER BY target_trade_date,run_id LIMIT 100""", (stamp, stamp)).fetchall()]
        groups = defaultdict(list)
        for job in jobs:
            run = self.database.load_t1_overnight_selection_run(job["run_id"])
            target, calendar = self.calendar.next_date(run["recommendationDate"], refresh=True)
            old_calendar = json.loads(job["calendar_json"])
            if old_calendar.get("confidence") in {"authoritative", "market_confirmed"} and calendar.get("confidence") != "authoritative":
                target, calendar = job["target_trade_date"], old_calendar
            if calendar.get("confidence") == "authoritative" and target != job["target_trade_date"]:
                calendar["revision"] = {"oldTarget": job["target_trade_date"], "reason": "交易所日历确认节假日", "at": stamp}
                with transaction(self.database) as conn:
                    conn.execute("UPDATE t1_overnight_next_day_tracking_jobs SET target_trade_date=?,calendar_json=?,updated_at=? WHERE run_id=? AND lease_expires_at<?",
                        (target, encode(calendar), stamp, job["run_id"], stamp))
                job["target_trade_date"] = target
            job["calendar_json"] = encode(calendar)
            if target > now.date().isoformat() or (target == now.date().isoformat() and now.strftime("%H:%M") < "15:10"):
                with transaction(self.database) as conn:
                    conn.execute("UPDATE t1_overnight_next_day_tracking_jobs SET status=?,calendar_json=?,updated_at=? WHERE run_id=? AND lease_expires_at<?",
                        ("pending_target" if target > now.date().isoformat() else "waiting_close", encode(calendar), stamp, job["run_id"], stamp))
                continue
            groups[target].append(job)
        for target, group in groups.items():
            self._collect(target, group, now)

    def _collect(self, target, jobs, now):
        stamp = now.isoformat(timespec="seconds")
        lease = (now + timedelta(minutes=5)).isoformat(timespec="seconds")
        # One collector per date even when different processes discover different Runs.
        with transaction(self.database) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT INTO t1_market_daily_collection_leases VALUES(?,?,?)
                ON CONFLICT(trade_date) DO UPDATE SET worker_id=excluded.worker_id,lease_expires_at=excluded.lease_expires_at
                WHERE t1_market_daily_collection_leases.lease_expires_at<?""", (target, self.worker_id, lease, stamp))
            owner = conn.execute("SELECT worker_id FROM t1_market_daily_collection_leases WHERE trade_date=?", (target,)).fetchone()[0]
            if owner != self.worker_id:
                return
            claimed = []
            for job in jobs:
                cur = conn.execute("""UPDATE t1_overnight_next_day_tracking_jobs SET status='collecting',worker_id=?,
                    lease_expires_at=?,attempt_count=attempt_count+1,calendar_json=?,updated_at=? WHERE run_id=? AND lease_expires_at<?""",
                    (self.worker_id, lease, job["calendar_json"], stamp, job["run_id"], stamp))
                if cur.rowcount:
                    claimed.append(job)
        try:
            candidates = {x["run_id"]: self.store.all(x["run_id"]) for x in claimed}
            codes = sorted({c["code"] for rows in candidates.values() for c in rows})
            bars = {code: bar for code, bar in self.store.bars(target).items() if valid_bar(bar, target)}
            missing = [code for code in codes if code not in bars]
            # Persist a fair cursor: permanently missing early codes cannot starve later batches.
            cursor_key = f"t1_daily_tracking_cursor:{target}"
            cursor = self.database.get_app_setting(cursor_key, "") or ""
            missing = [c for c in missing if c > cursor] + [c for c in missing if c <= cursor]
            error, meta = "", {}
            if missing:
                try:
                    fetched, meta = self.provider.fetch(target, missing, time.monotonic() + 120)
                    self.store.save_bars(target, {c: b for c, b in fetched.items() if valid_bar(b, target)})
                    bars.update({c: b for c, b in fetched.items() if valid_bar(b, target)})
                    if missing:
                        self.database.set_app_setting(cursor_key, missing[min(50, len(missing)) - 1])
                except Exception as exc:
                    error = str(exc)
                    LOGGER.warning("next-day collection date=%s error=%s", target, exc)
            # Market-wide confirmation; individual stale quotes can never advance the date.
            date_confirmed = sum(c in bars and bars[c].get("tradingStatus", "traded") == "traded" for c in codes) >= min(100, max(1, math.ceil(len(codes) * .5)))
            for job in claimed:
                run_id = job["run_id"]
                calendar = json.loads(job["calendar_json"])
                run = self.database.load_t1_overnight_selection_run(run_id)
                references = self.store.bars(run["recommendationDate"])
                metrics = [candidate_metric(c, bars.get(c["code"]), target,
                           (references.get(c["code"]) or {}).get("close")) for c in candidates[run_id]]
                resolved = sum(x["status"] != "missing" for x in metrics)
                authoritative = calendar.get("confidence") == "authoritative"
                confirmed = authoritative or calendar.get("confidence") == "market_confirmed" or date_confirmed
                if _trading_days_after(self.database, target, now.date().isoformat()) > 5 and (resolved < len(metrics) or not confirmed):
                    status = "terminal_missing"
                elif not authoritative and calendar.get("confidence") != "market_confirmed" and not date_confirmed:
                    status = "waiting_trade_date_confirmation"
                elif resolved == len(metrics):
                    status = "complete"
                elif now.date().isoformat() == target and now.strftime("%H:%M") < "18:00":
                    status = "retry_wait"
                else:
                    status = "partial"
                if date_confirmed and not authoritative:
                    calendar.update(confidence="market_confirmed", confirmedAt=stamp)
                retry_at = _next_retry(now, target)
                if not self.store.save_metrics(run_id, target, metrics, self.worker_id):
                    continue
                with transaction(self.database) as conn:
                    conn.execute("""UPDATE t1_overnight_next_day_tracking_jobs SET status=?,resolved_count=?,
                        next_retry_at=?,lease_expires_at='',worker_id='',calendar_json=?,last_error=?,updated_at=?
                        WHERE run_id=? AND worker_id=?""", (status, resolved, retry_at, encode(dict(calendar, collection=meta)),
                        error or (f"缺失 {len(metrics)-resolved} 只" if resolved < len(metrics) else ""),
                        now_text(), run_id, self.worker_id))
        finally:
            with transaction(self.database) as conn:
                conn.execute("DELETE FROM t1_market_daily_collection_leases WHERE trade_date=? AND worker_id=?", (target, self.worker_id))

    def summary(self, run_id):
        archive = self.store.all(run_id)
        job = self.store.job(run_id)
        if not archive:
            return {"legacy": True, "legacyNotRecorded": True, "runId": run_id, "groups": []}
        with transaction(self.database) as conn:
            rows = conn.execute("SELECT payload_json FROM t1_overnight_candidate_next_day_metrics WHERE run_id=? AND target_trade_date=?",
                                (run_id, job["target_trade_date"] if job else "")).fetchall()
        metrics = {x["code"]: x for x in (json.loads(r[0]) for r in rows)}
        all_items = [dict(c, metric=metrics.get(c["code"], {"code": c["code"], "status": "missing"})) for c in archive]
        selectors = {"all500": lambda c: True, "requested120": lambda c: c["allocationRound"] > 0,
            "available15m": lambda c: c.get("minute15mAvailable", False), "eligiblePool": lambda c: c["qualityEligible"],
            "notRequested": lambda c: not c["allocationRound"]}
        for group in ("core", "single", "boundary", "coverage", "explore"):
            selectors[group] = lambda c, g=group: c["primaryGroup"] == g
        groups = []
        for name, predicate in selectors.items():
            selected = [c for c in all_items if predicate(c)]
            outcomes = [c["metric"] for c in selected]
            planned = sum(c["allocationRound"] > 0 for c in selected)
            requested = sum(bool(c.get("minute15mRequested")) for c in selected)
            eligible = sum(c["qualityEligible"] for c in selected)
            groups.append({"group": name, "total": len(selected), "missing": sum(x["status"] == "missing" for x in outcomes),
                "excluded": sum(not x.get("aggregateEligible") and x["status"] != "missing" for x in outcomes),
                "planned": planned, "requested": requested, "eligible": eligible,
                "requestRatePct": requested / planned * 100 if planned else None,
                "gateRatePct": eligible / planned * 100 if planned else None,
                "open": distribution(outcomes, "openProxyReturnPct"), "close": distribution(outcomes, "closeProxyReturnPct")})
        top = {}
        for label, field in (("open", "openProxyReturnPct"), ("close", "closeProxyReturnPct")):
            leaders = sorted([c for c in all_items if c["metric"].get("aggregateEligible")],
                             key=lambda c: (-c["metric"][field], c["code"]))[:10]
            top[label] = {"count": len(leaders), "requestedCoveragePct": sum(c["allocationRound"] > 0 for c in leaders) / len(leaders) * 100 if leaders else None,
                "eligibleCoveragePct": sum(c["qualityEligible"] for c in leaders) / len(leaders) * 100 if leaders else None}
        missed = sorted([c for c in all_items if not c["allocationRound"] and c["metric"].get("aggregateEligible")],
                        key=lambda c: (-c["metric"]["closeProxyReturnPct"], c["code"]))[:10]
        coverage = (job["resolved_count"] / len(archive) * 100) if job else 0
        return {"runId": run_id, "legacy": False, "version": METRIC_VERSION, "job": job,
            "coveragePct": coverage, "coverageIncomplete": coverage < 95,
            "collection": (json.loads(job["calendar_json"]).get("collection") or {}) if job else {},
            "dailySources": dict(Counter(c["metric"].get("dailyBarSource") for c in all_items if c["metric"].get("dailyBarSource"))),
            "note": "开盘/收盘为未扣成本的机会代理，不能替代 open_15m 交易净收益；除权状态未知的样本仍需审慎观察。",
            "groups": groups, "top10Coverage": top,
            "missedOpportunities": [{"code": c["code"], "name": c["name"], "preliminaryRank": c["preliminaryScoreRank"],
                "riskFlags": c.get("riskFlags"), "exclusionReason": c.get("exclusionReason") or "未分配15m验证名额",
                "closeProxyReturnPct": c["metric"]["closeProxyReturnPct"]} for c in missed]}


def _business_days_after(start, end):
    day, finish, count = date.fromisoformat(start), date.fromisoformat(end), 0
    while day < finish:
        day += timedelta(days=1)
        count += day.weekday() < 5
    return count


def _trading_days_after(database, start, end):
    calendar = database.get_app_setting("t1_tracking_exchange_calendar", {}) or {}
    if calendar.get("start", "z") <= start and end <= calendar.get("end", ""):
        return sum(start < day <= end for day in calendar.get("openDates", []))
    return _business_days_after(start, end)


def _next_retry(now, target):
    if now.date().isoformat() == target:
        for slot in ("15:20", "15:40", "16:00", "17:00", "18:00"):
            value = datetime.fromisoformat(f"{target}T{slot}:00")
            if value > now:
                return value.isoformat(timespec="seconds")
    return (now + timedelta(minutes=10)).isoformat(timespec="seconds")
