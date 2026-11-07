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
from backend.app.t1_candidate_store import (
    REVIEW_SCHEMA_VERSION,
    TERMINAL_STAGE_LABELS,
    CandidateStore,
    encode,
    legacy_candidate_review,
    now_text,
    transaction,
    v3_candidate_review,
)

LOGGER = logging.getLogger(__name__)
METRIC_VERSION = "2026-10-09.proxy.v1"
LEADER_CLASSIFICATION_VERSION = "2026-11-07.v1"


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
        candidates = self._tracking_candidates(run_id)
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

    def _tracking_candidates(self, run_id):
        """Use the V3 full-market lightweight frame while preserving archived candidate facts."""
        combined = {}
        for item in self.store.v3_facts(run_id):
            combined[item["code"]] = item
        for item in self.store.all(run_id):
            combined[item["code"]] = {**combined.get(item["code"], {}), **item}
        return [combined[code] for code in sorted(combined)]

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
            candidates = {x["run_id"]: self._tracking_candidates(x["run_id"]) for x in claimed}
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
        v3_facts = self.store.v3_facts(run_id)
        job = self.store.job(run_id)
        if not archive and not v3_facts:
            return {"legacy": True, "legacyNotRecorded": True, "runId": run_id, "groups": [],
                    "reviewSchemaVersion": REVIEW_SCHEMA_VERSION}
        with transaction(self.database) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM t1_overnight_candidate_next_day_metrics WHERE run_id=? AND target_trade_date=?",
                (run_id, job["target_trade_date"] if job else ""),
            ).fetchall()
        metrics = {item["code"]: item for item in (json.loads(row[0]) for row in rows)}
        missing_metric = lambda code: {"code": code, "status": "missing", "aggregateEligible": False}
        all_items = [dict(item, metric=metrics.get(item["code"], missing_metric(item["code"]))) for item in archive]
        v3_items = [dict(item, metric=metrics.get(item["code"], missing_metric(item["code"]))) for item in v3_facts]

        def percentage(numerator, denominator):
            return numerator / denominator * 100 if denominator else None

        selectors = {
            "all500": lambda candidate: True,
            "requested120": lambda candidate: int(candidate.get("allocationRound") or 0) > 0,
            "available15m": lambda candidate: bool(candidate.get("minute15mAvailable")),
            "eligiblePool": lambda candidate: bool(candidate.get("qualityEligible")),
            "notRequested": lambda candidate: not int(candidate.get("allocationRound") or 0),
        }
        for group in ("core", "single", "boundary", "coverage", "explore"):
            selectors[group] = lambda candidate, name=group: candidate.get("primaryGroup") == name
        groups = []
        for name, predicate in selectors.items():
            selected = [candidate for candidate in all_items if predicate(candidate)]
            outcomes = [candidate["metric"] for candidate in selected]
            total = len(selected)
            planned = sum(int(candidate.get("allocationRound") or 0) > 0 for candidate in selected)
            requested = sum(bool(candidate.get("minute15mRequested")) for candidate in selected)
            available = sum(bool(candidate.get("minute15mAvailable")) for candidate in selected)
            eligible = sum(bool(candidate.get("qualityEligible")) for candidate in selected)
            setup = sum(bool(candidate.get("setupEligible")) for candidate in selected)
            semifinal = sum(bool(candidate.get("semifinalSelected")) for candidate in selected)
            official = sum(bool(candidate.get("officialSelected")) for candidate in selected)
            request_completion = percentage(requested, planned)
            data_quality = percentage(eligible, available)
            groups.append({
                "group": name,
                "total": total,
                "missing": sum(outcome.get("status") == "missing" for outcome in outcomes),
                "excluded": sum(not outcome.get("aggregateEligible") and outcome.get("status") != "missing" for outcome in outcomes),
                "planned": planned,
                "requested": requested,
                "available": available,
                "eligible": eligible,
                "setupEligible": setup,
                "semifinal": semifinal,
                "official": official,
                "allocationCoveragePct": percentage(planned, total),
                "requestCompletionPct": request_completion,
                "dataAvailabilityPct": percentage(available, requested),
                "dataQualityPassPct": data_quality,
                "setupPassPct": percentage(setup, eligible),
                "semifinalAdvancePct": percentage(semifinal, setup),
                "officialSelectionPct": percentage(official, semifinal),
                # Deprecated aliases remain for older clients; the new page does not display them.
                "requestRatePct": request_completion,
                "gateRatePct": data_quality,
                "open": distribution(outcomes, "openProxyReturnPct"),
                "close": distribution(outcomes, "closeProxyReturnPct"),
            })

        leader_items = v3_items if v3_items else all_items
        review_scope = "v3_full_market" if v3_items else "legacy_500"

        def is_allocated(candidate):
            return bool(candidate.get("v3ValidationRank")) if v3_items else int(candidate.get("allocationRound") or 0) > 0

        def is_data_eligible(candidate):
            return bool(candidate.get("dataEligible")) if v3_items else bool(candidate.get("qualityEligible"))

        top = {}
        for label, field in (("open", "openProxyReturnPct"), ("close", "closeProxyReturnPct")):
            leaders = sorted(
                [candidate for candidate in leader_items if candidate["metric"].get("aggregateEligible")
                 and candidate["metric"].get(field) is not None],
                key=lambda candidate: (-float(candidate["metric"][field]), candidate["code"]),
            )[:10]
            top[label] = {
                "count": len(leaders),
                "requestedCoveragePct": percentage(sum(is_allocated(candidate) for candidate in leaders), len(leaders)),
                "eligibleCoveragePct": percentage(sum(is_data_eligible(candidate) for candidate in leaders), len(leaders)),
                "sourcePool": "v3_full_market_facts" if v3_items else "legacy_candidate_archive",
            }

        unallocated_valid = [
            candidate for candidate in leader_items
            if not is_allocated(candidate) and candidate["metric"].get("aggregateEligible")
        ]
        open_ranked = sorted(
            [candidate for candidate in unallocated_valid if float(candidate["metric"].get("openProxyReturnPct") or 0) > 0],
            key=lambda candidate: (-float(candidate["metric"]["openProxyReturnPct"]), candidate["code"]),
        )
        close_ranked = sorted(
            [candidate for candidate in unallocated_valid if float(candidate["metric"].get("closeProxyReturnPct") or 0) > 0],
            key=lambda candidate: (-float(candidate["metric"]["closeProxyReturnPct"]), candidate["code"]),
        )
        open_ranks = {candidate["code"]: index for index, candidate in enumerate(open_ranked, 1)}
        close_ranks = {candidate["code"]: index for index, candidate in enumerate(close_ranked, 1)}

        def risk_flagged(candidate):
            score = (candidate.get("cheapFeatures") or {}).get("risk")
            if isinstance(score, (int, float)) and score >= 45:
                return True
            text = " ".join(str(value) for value in candidate.get("riskFlags") or [])
            return any(word in text for word in ("追高", "高风险", "过热", "事件风险", "接近涨停"))

        def hard_to_buy(candidate):
            tradeability = candidate.get("tradeability") or {}
            level = str(tradeability.get("level") if isinstance(tradeability, dict) else tradeability or "")
            explicit = "formalTradeabilityEligible" in candidate
            return ((explicit and candidate.get("formalTradeabilityEligible") is False)
                    or level in {"hard_to_buy", "not_buyable", "suspended"}
                    or str(candidate.get("limitStatus") or "") in {"one_word_limit_up", "suspended"})

        def classification(candidate):
            metric = candidate["metric"]
            open_value = metric.get("openProxyReturnPct")
            close_value = metric.get("closeProxyReturnPct")
            tags, reasons = [], []
            open_rank = open_ranks.get(candidate["code"])
            close_rank = close_ranks.get(candidate["code"])
            if open_rank and open_rank <= 10:
                tags.append("open_premium_leader")
                reasons.append(f"未分配候选中开盘机会代理排名第{open_rank}")
            improvement = (float(close_value) - float(open_value)) if close_value is not None and open_value is not None else None
            if close_rank and close_rank <= 10 and (open_value is None or float(open_value) <= 0 or (improvement is not None and improvement >= 1.0)):
                tags.append("intraday_close_leader")
                reasons.append(f"未分配候选中收盘机会代理排名第{close_rank}，且开盘后明显走强")
            tradeability = candidate.get("tradeability")
            risk_known = (bool(candidate.get("riskFlags"))
                          or (candidate.get("cheapFeatures") or {}).get("risk") is not None
                          or candidate.get("v3RiskStatus") is not None
                          or candidate.get("riskStatus") is not None)
            tradeability_known = (tradeability is not None
                                  or "formalTradeabilityEligible" in candidate)
            if hard_to_buy(candidate):
                conclusion = "hard_to_buy"
                reasons.append("推荐日冻结交易性证据显示难买或不可买")
            elif risk_flagged(candidate):
                conclusion = "high_risk"
                reasons.append("推荐日冻结风险达到高风险阈值或命中高风险规则")
            elif tradeability_known and risk_known:
                conclusion = "normal_tradeable_miss"
                reasons.append("推荐日可交易且未达到高风险阈值，但没有获得验证名额")
            else:
                conclusion = "tradeability_unknown"
                reasons.append("历史推荐日交易性或风险字段不足，不能归为正常可交易漏选")
            review = v3_candidate_review(candidate) if v3_items else legacy_candidate_review(candidate)
            return {
                **review,
                "leaderPerformanceTags": tags,
                "leaderBusinessConclusion": conclusion,
                "leaderClassificationReasons": reasons,
                "leaderClassificationVersion": LEADER_CLASSIFICATION_VERSION,
                "unallocatedOpenRank": open_rank,
                "unallocatedCloseRank": close_rank,
                "tradeabilityStatus": "hard_to_buy" if conclusion == "hard_to_buy" else "unknown" if conclusion == "tradeability_unknown" else "tradable",
                "riskStatus": "high_risk" if conclusion == "high_risk" else "unknown" if conclusion == "tradeability_unknown" else "normal",
            }

        positive_cases = [
            candidate for candidate in unallocated_valid
            if float(candidate["metric"].get("openProxyReturnPct") or 0) > 0
            or float(candidate["metric"].get("closeProxyReturnPct") or 0) > 0
        ]
        leader_cases = []
        positive_codes = {candidate["code"] for candidate in positive_cases}
        review_facts = []
        for candidate in leader_items:
            review = v3_candidate_review(candidate) if v3_items else legacy_candidate_review(candidate)
            if candidate["code"] in positive_codes:
                review.update(classification(candidate))
            else:
                review.update(leaderPerformanceTags=[], leaderBusinessConclusion="",
                              leaderClassificationReasons=[], leaderClassificationVersion=LEADER_CLASSIFICATION_VERSION,
                              unallocatedOpenRank=open_ranks.get(candidate["code"]),
                              unallocatedCloseRank=close_ranks.get(candidate["code"]))
            review_fact = {
                "code": candidate["code"],
                "name": candidate.get("name") or "",
                **review,
            }
            review_facts.append(review_fact)
            if candidate["code"] not in positive_codes:
                continue
            metric = candidate["metric"]
            leader_cases.append({
                **review_fact,
                "preliminaryRank": candidate.get("v3RecallRank") or candidate.get("preliminaryScoreRank"),
                "v3RecallRank": candidate.get("v3RecallRank"),
                "v3ValidationRank": candidate.get("v3ValidationRank"),
                "validationGroup": candidate.get("v3ValidationRole") or candidate.get("primaryGroup") or "",
                "validationRound": candidate.get("allocationRound") or 0,
                "riskFlags": candidate.get("riskFlags") or [],
                "exclusionReason": review.get("terminalReason") or candidate.get("exclusionReason") or "",
                "openProxyReturnPct": metric.get("openProxyReturnPct"),
                "closeProxyReturnPct": metric.get("closeProxyReturnPct"),
                "highOpportunityPct": metric.get("highOpportunityPct"),
                "targetTradeDate": metric.get("targetTradeDate"),
            })
        conclusion_order = {"normal_tradeable_miss": 0, "high_risk": 1, "hard_to_buy": 2, "tradeability_unknown": 3}
        leader_cases.sort(key=lambda item: (
            conclusion_order.get(item.get("leaderBusinessConclusion"), 9),
            -max(float(item.get("openProxyReturnPct") or -999), float(item.get("closeProxyReturnPct") or -999)),
            item["code"],
        ))
        self.store.replace_review_facts(run_id, review_facts, REVIEW_SCHEMA_VERSION)
        leader_counts = Counter(item["leaderBusinessConclusion"] for item in leader_cases)
        performance_counts = Counter(tag for item in leader_cases for tag in item.get("leaderPerformanceTags") or [])

        expected_count = int((job or {}).get("expected_count") or len(leader_items) or 0)
        coverage = min(100.0, percentage(int((job or {}).get("resolved_count") or 0), expected_count) or 0)
        v3_stages = {
            "hardFilterEligible": lambda candidate: candidate.get("hardFilterStatus") == "eligible",
            "recall500": lambda candidate: bool(candidate.get("v3RecallRank")),
            "validation120": lambda candidate: bool(candidate.get("v3ValidationRank")),
            "dataEligible": lambda candidate: bool(candidate.get("dataEligible")),
            "tailCalibration30": lambda candidate: bool(candidate.get("v3CalibrationSample")),
            "shortlist": lambda candidate: v3_candidate_review(candidate)["terminalStage"] in {"shortlist_not_portfolio", "v3_shadow_selected"},
            "selected": lambda candidate: v3_candidate_review(candidate)["terminalStage"] == "v3_shadow_selected",
        }
        v3_groups = []
        if v3_items:
            for name, predicate in v3_stages.items():
                selected = [item for item in v3_items if predicate(item)]
                outcomes = [item["metric"] for item in selected]
                v3_groups.append({
                    "stage": name,
                    "total": len(selected),
                    "missing": sum(item.get("status") == "missing" for item in outcomes),
                    "openProxy": distribution(outcomes, "openProxyReturnPct"),
                    "closeProxy": distribution(outcomes, "closeProxyReturnPct"),
                })
        return {
            "runId": run_id,
            "legacy": False,
            "version": METRIC_VERSION,
            "reviewSchemaVersion": REVIEW_SCHEMA_VERSION,
            "leaderClassificationVersion": LEADER_CLASSIFICATION_VERSION,
            "dataScope": {"reviewScope": review_scope, "v3Recorded": bool(v3_items),
                          "legacyArchiveRecorded": bool(all_items), "candidateCount": len(leader_items)},
            "job": job,
            "coveragePct": coverage,
            "coverageIncomplete": coverage < 95,
            "collection": (json.loads(job["calendar_json"]).get("collection") or {}) if job else {},
            "dailySources": dict(Counter(candidate["metric"].get("dailyBarSource") for candidate in leader_items
                                         if candidate["metric"].get("dailyBarSource"))),
            "note": "开盘、收盘和盘中最高均为未扣成本的机会代理，不能替代 open_15m 交易净收益；旧版Run只能复原500池内部。",
            "v3": {"recorded": bool(v3_items), "fullMarketFactCount": len(v3_items), "groups": v3_groups},
            "groups": groups,
            "top10Coverage": top,
            "leaderDefinition": "未进入当日120只验证层，且目标日开盘代理收益大于 0，或收盘代理收益大于 0；仅用于复盘，不是可交易净收益。",
            "leaderSummary": {"uniqueCount": len(leader_cases), "businessConclusionCounts": dict(leader_counts),
                              "performanceTagCounts": dict(performance_counts), "tagsMayOverlap": True},
            "leaderCases": leader_cases,
            # Backward-compatible top ten; new clients use leaderCases.
            "missedOpportunities": leader_cases[:10],
            "terminalStageLabels": TERMINAL_STAGE_LABELS,
        }

    def rebuild_review_facts(self, run_id):
        summary = self.summary(run_id)
        facts = self.store.review_facts(run_id, REVIEW_SCHEMA_VERSION)
        return {"runId": run_id, "reviewSchemaVersion": REVIEW_SCHEMA_VERSION,
                "factCount": len(facts), "leaderCaseCount": len(summary.get("leaderCases") or []),
                "reviewScope": (summary.get("dataScope") or {}).get("reviewScope", "unknown")}


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
