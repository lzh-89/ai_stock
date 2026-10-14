from __future__ import annotations

import logging
import os
import socket
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.app.database import Database


LOGGER = logging.getLogger("ai_stock.t1_entry_execution_evidence")
ENTRY_EXECUTION_EVIDENCE_VERSION = "entry_execution_after_publish_v1"
ENTRY_EXECUTION_STATE_KEY = "t1_entry_execution_evidence_scheduler_state"
ENTRY_EXECUTION_CONFIG = {
    "enabled": True,
    "pollIntervalSeconds": 15,
    "retryIntervalSeconds": 30,
    "leaseSeconds": 90,
    "captureDelaySeconds": 125,
    "deadlineTime": "14:56:30",
    "period": "1m",
    "lookbackRunLimit": 100,
}


class T1EntryExecutionEvidenceService:
    def __init__(self, analysis_service: Any, database: Database, clock: Any = None):
        self.analysis_service = analysis_service
        self.database = database
        self.clock = clock or datetime.now
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start_scheduler(self) -> Dict[str, Any]:
        state = self.status()
        if not state["config"]["enabled"]:
            return state
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._thread = threading.Thread(
                target=self._scheduler_loop, name="t1-entry-execution-evidence", daemon=True
            )
            self._thread.start()
        return self.status()

    def status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now_dt = now or self.clock()
        state = dict(self.database.get_app_setting(ENTRY_EXECUTION_STATE_KEY, {}) or {})
        config = _normalize_config(state.get("config") or ENTRY_EXECUTION_CONFIG)
        return {
            **state,
            "version": ENTRY_EXECUTION_EVIDENCE_VERSION,
            "config": config,
            "schedulerAlive": bool(self._thread and self._thread.is_alive()),
            "todayJobs": self.database.list_t1_entry_execution_jobs(now_dt.date().isoformat()),
        }

    def run_due(self, now: Optional[datetime] = None, trigger_source: str = "scheduler") -> Dict[str, Any]:
        now_dt = now or self.clock()
        now_at = now_dt.isoformat(timespec="seconds")
        state = dict(self.database.get_app_setting(ENTRY_EXECUTION_STATE_KEY, {}) or {})
        config = _normalize_config(state.get("config") or ENTRY_EXECUTION_CONFIG)
        registered = self.register_today_jobs(now_dt, config)
        processed: List[Dict[str, Any]] = []
        if config["enabled"] and now_dt.weekday() < 5:
            for job in reversed(self.database.list_t1_entry_execution_jobs(now_dt.date().isoformat())):
                if job["status"] not in {"scheduled", "running"} or now_at < job["dueAt"]:
                    continue
                lease_expires = (now_dt + timedelta(seconds=config["leaseSeconds"])).isoformat(timespec="seconds")
                acquired = self.database.acquire_t1_entry_execution_job(
                    job["id"], self.worker_id, now_at, lease_expires
                )
                if not acquired:
                    continue
                processed.append(self._execute(acquired, config, now_dt, trigger_source))
        state.update(
            {
                "version": ENTRY_EXECUTION_EVIDENCE_VERSION,
                "config": config,
                "lastCheckAt": now_at,
                "lastStatus": processed[-1]["status"] if processed else "waiting",
                "lastProcessed": processed[-8:],
                "updatedAt": now_at,
            }
        )
        self.database.set_app_setting(ENTRY_EXECUTION_STATE_KEY, state)
        return {**state, "registered": registered, "processed": processed}

    def register_today_jobs(
        self, now: Optional[datetime] = None, config: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        now_dt = now or self.clock()
        config = _normalize_config(config or ENTRY_EXECUTION_CONFIG)
        today = now_dt.date().isoformat()
        runs = self.database.list_t1_overnight_selection_runs(
            limit=config["lookbackRunLimit"], status="done"
        )
        registered = []
        for run in runs:
            if str(run.get("recommendationDate") or "") != today or not _eligible_run(run):
                continue
            timing = (run.get("market") or {}).get("decisionTiming") or {}
            published = str(timing.get("signalPublishedAt") or "")
            try:
                published_dt = datetime.fromisoformat(published)
            except ValueError:
                continue
            due_at = (published_dt + timedelta(seconds=config["captureDelaySeconds"])).isoformat(timespec="seconds")
            deadline_at = f"{today}T{config['deadlineTime']}"
            if due_at > deadline_at:
                due_at = deadline_at
            job = self.database.register_t1_entry_execution_job(
                {
                    "runId": int(run.get("id") or 0),
                    "recommendationDate": today,
                    "evidenceVersion": ENTRY_EXECUTION_EVIDENCE_VERSION,
                    "signalPublishedAt": published,
                    "dueAt": due_at,
                    "deadlineAt": deadline_at,
                }
            )
            if job:
                registered.append(job)
        return registered

    def evidence_for_run(self, run_id: int) -> Dict[str, Any]:
        jobs = [
            item
            for item in self.database.list_t1_entry_execution_jobs(limit=500)
            if int(item.get("runId") or 0) == int(run_id)
        ]
        snapshots = self.database.load_t1_entry_execution_snapshots(run_id)
        rows = list(snapshots.values())
        eligible = len([row for row in rows if row.get("decisionEligible")])
        expected = len(rows)
        run = self.database.load_t1_overnight_selection_run(run_id) or {}
        if not expected:
            expected = int(run.get("finalCount") or len(run.get("finalPicks") or []))
        status = jobs[0].get("status") if jobs else "not_registered"
        if expected and eligible == expected:
            status = "available"
        elif rows:
            status = "partial"
        return {
            "version": ENTRY_EXECUTION_EVIDENCE_VERSION,
            "status": status,
            "expectedCount": expected,
            "eligibleCount": eligible,
            "complete": bool(expected and eligible == expected),
            "jobs": jobs,
            "snapshots": rows,
        }

    def _scheduler_loop(self) -> None:
        while True:
            try:
                self.run_due()
                interval = self.status()["config"]["pollIntervalSeconds"]
            except Exception as error:  # noqa: BLE001 - scheduler must survive transient providers.
                LOGGER.exception("t1 entry execution scheduler tick failed error=%s", error)
                interval = ENTRY_EXECUTION_CONFIG["pollIntervalSeconds"]
            time.sleep(interval)

    def _execute(
        self, job: Dict[str, Any], config: Dict[str, Any], now_dt: datetime, trigger_source: str
    ) -> Dict[str, Any]:
        run = self.database.load_t1_overnight_selection_run(job["runId"])
        if not run or not _eligible_run(run):
            self.database.finish_t1_entry_execution_job(job["id"], "not_applicable", "Run 不再符合正式执行证据条件。")
            return {"jobId": job["id"], "status": "not_applicable"}
        picks = list(run.get("finalPicks") or [])
        codes = [str(item.get("code") or "").zfill(6) for item in picks if item.get("code")]
        try:
            fetched = self.analysis_service.kline_provider.fetch_many(
                codes, period=config["period"], require_dated=True
            )
        except TypeError:
            try:
                fetched = self.analysis_service.kline_provider.fetch_many(codes, period=config["period"])
            except Exception as error:  # noqa: BLE001 - retry within the bounded execution window.
                return self._retry_or_finish(job, config, now_dt, codes, {}, trigger_source, str(error))
        except Exception as error:  # noqa: BLE001 - retry within the bounded execution window.
            return self._retry_or_finish(job, config, now_dt, codes, {}, trigger_source, str(error))
        return self._retry_or_finish(job, config, now_dt, codes, fetched or {}, trigger_source, "")

    def _retry_or_finish(
        self,
        job: Dict[str, Any],
        config: Dict[str, Any],
        now_dt: datetime,
        codes: List[str],
        fetched: Dict[str, List[Dict[str, Any]]],
        trigger_source: str,
        provider_error: str,
    ) -> Dict[str, Any]:
        signal_time = _time_text(job.get("signalPublishedAt"))
        rows_by_code: Dict[str, List[Dict[str, Any]]] = {}
        for code in codes:
            series = fetched.get(code) or fetched.get(str(code).lstrip("0")) or []
            rows_by_code[code] = [
                dict(item)
                for item in series
                if _minute_date(item) == job["recommendationDate"]
                and _minute_time(item) > signal_time
                and _valid_trade_point(item)
            ]
        missing = [code for code in codes if not rows_by_code.get(code)]
        now_at = now_dt.isoformat(timespec="seconds")
        if missing and now_at < job["deadlineAt"]:
            due_at = min(
                now_dt + timedelta(seconds=config["retryIntervalSeconds"]),
                datetime.fromisoformat(job["deadlineAt"]),
            ).isoformat(timespec="seconds")
            error = provider_error or f"等待信号发布后的完整 1 分钟成交：{','.join(missing)}"
            self.database.retry_t1_entry_execution_job(job["id"], due_at, error)
            return {"jobId": job["id"], "status": "retry_wait", "missingCodes": missing, "error": error}

        source_map = getattr(self.analysis_service.kline_provider, "last_sources_by_code", {}) or {}
        default_source = str(getattr(self.analysis_service.kline_provider, "source", "") or "minute_provider")
        late_backfill = now_at > job["deadlineAt"]
        snapshots = []
        for code in codes:
            series = rows_by_code.get(code) or []
            first = series[0] if series else {}
            entry_time = _minute_time(first)
            price = _number(first.get("open") or first.get("close"))
            source = str(source_map.get(code) or default_source)
            eligible = bool(series and entry_time > signal_time and price and price > 0)
            payload = {
                "version": ENTRY_EXECUTION_EVIDENCE_VERSION,
                "runId": job["runId"],
                "code": code,
                "recommendationDate": job["recommendationDate"],
                "signalPublishedAt": job["signalPublishedAt"],
                "signalTime": signal_time,
                "entryTime": entry_time,
                "price": price,
                "volume": _number(first.get("volume")),
                "period": config["period"],
                "source": source,
                "series": series,
                "triggerSource": trigger_source,
                "decisionEligible": eligible,
                "lateBackfill": late_backfill,
                "reason": "已冻结正式推荐发布后的分钟成交证据。" if eligible else provider_error or "截止时间前未取得发布后的分钟成交。",
            }
            snapshots.append(
                {
                    "runId": job["runId"],
                    "code": code,
                    "recommendationDate": job["recommendationDate"],
                    "evidenceVersion": ENTRY_EXECUTION_EVIDENCE_VERSION,
                    "signalPublishedAt": job["signalPublishedAt"],
                    "providerTimestamp": _minute_timestamp(first),
                    "receivedAt": now_at,
                    "status": "available" if eligible else "unavailable",
                    "decisionEligible": eligible,
                    "lateBackfill": late_backfill,
                    "source": source,
                    "payload": payload,
                }
            )
        self.database.append_t1_entry_execution_snapshots(snapshots)
        terminal = "done" if not missing else "partial"
        error = "" if not missing else provider_error or f"部分股票缺少发布后分钟成交：{','.join(missing)}"
        self.database.finish_t1_entry_execution_job(job["id"], terminal, error)
        return {"jobId": job["id"], "status": terminal, "missingCodes": missing}


def _eligible_run(run: Dict[str, Any]) -> bool:
    request = run.get("request") or {}
    market = run.get("market") or {}
    role = str(market.get("runRole") or request.get("runRole") or "")
    timing = market.get("decisionTiming") or {}
    signal_time = _time_text(timing.get("signalPublishedAt") or timing.get("signalTime"))
    return bool(
        run.get("status") == "done"
        and role == "official"
        and not bool(request.get("force"))
        and int(run.get("finalCount") or len(run.get("finalPicks") or [])) > 0
        and signal_time
        and signal_time <= "14:50"
    )


def _normalize_config(value: Dict[str, Any]) -> Dict[str, Any]:
    raw = {**ENTRY_EXECUTION_CONFIG, **dict(value or {})}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "pollIntervalSeconds": max(5, min(60, int(raw.get("pollIntervalSeconds") or 15))),
        "retryIntervalSeconds": max(10, min(120, int(raw.get("retryIntervalSeconds") or 30))),
        "leaseSeconds": max(30, min(300, int(raw.get("leaseSeconds") or 90))),
        "captureDelaySeconds": max(65, min(240, int(raw.get("captureDelaySeconds") or 125))),
        "deadlineTime": str(raw.get("deadlineTime") or "14:56:30"),
        "period": "1m",
        "lookbackRunLimit": max(20, min(1000, int(raw.get("lookbackRunLimit") or 100))),
    }


def _time_text(value: Any) -> str:
    text = str(value or "")
    if len(text) >= 16 and text[10] in {"T", " "}:
        return text[11:16]
    return text[:5] if len(text) >= 5 and text[2] == ":" else ""


def _minute_date(item: Dict[str, Any]) -> str:
    text = str(item.get("date") or item.get("tradeDate") or item.get("datetime") or item.get("time") or "")
    return text[:10] if len(text) >= 10 and text[4:5] == "-" else ""


def _minute_time(item: Dict[str, Any]) -> str:
    text = str(item.get("time") or item.get("datetime") or item.get("timestamp") or "")
    if len(text) >= 16 and text[10] in {"T", " "}:
        return text[11:16]
    return text[:5] if len(text) >= 5 and text[2:3] == ":" else ""


def _minute_timestamp(item: Dict[str, Any]) -> str:
    value = str(item.get("datetime") or item.get("timestamp") or "")
    if value:
        return value
    trade_date = _minute_date(item)
    trade_time = _minute_time(item)
    return f"{trade_date}T{trade_time}:00" if trade_date and trade_time else ""


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _valid_trade_point(item: Dict[str, Any]) -> bool:
    price = _number(item.get("open") or item.get("close"))
    volume = _number(item.get("volume"))
    return bool(price and price > 0 and (volume is None or volume > 0))
