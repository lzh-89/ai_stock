from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from backend.app.database import Database
from backend.app.providers.sector_provider import get_sector_provider


LOGGER = logging.getLogger("ai_stock.t1_opening_evidence")
OPENING_EVIDENCE_VERSION = "opening_risk_snapshot_v1"
OPENING_STRATEGY_POLICY_VERSION = "2026-10-13收益优化.shadow.v5"
OPENING_PRECHECK = "opening_risk_precheck"
OPENING_SNAPSHOT = "opening_risk_snapshot"
OPENING_SCOPE = "opening_scope_registration"
OPENING_EVIDENCE_STATE_KEY = "t1_opening_evidence_scheduler_state"
OPENING_EVIDENCE_CONFIG = {
    "enabled": True,
    "pollIntervalSeconds": 15,
    "source": "tencent",
    "precheckTime": "09:29:00",
    "precheckDeadline": "09:30:59",
    "snapshotTime": "09:31:00",
    "snapshotDeadline": "09:31:59",
    "leaseSeconds": 90,
    "lookbackRunLimit": 500,
}
MAIN_INDEX_CODES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000852": "中证1000",
}


class T1OpeningEvidenceService:
    def __init__(
        self, analysis_service: Any, database: Database, sector_provider: Any = None, clock: Any = None
    ):
        self.analysis_service = analysis_service
        self.database = database
        self.sector_provider = sector_provider or get_sector_provider("auto")
        self.clock = clock or datetime.now
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start_scheduler(self) -> Dict[str, Any]:
        state = self.status()
        config = state["config"]
        if not config["enabled"]:
            return state
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._thread = threading.Thread(target=self._scheduler_loop, name="t1-opening-evidence", daemon=True)
            self._thread.start()
        return self.status()

    def status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now_dt = now or self.clock()
        state = dict(self.database.get_app_setting(OPENING_EVIDENCE_STATE_KEY, {}) or {})
        config = _normalize_config(state.get("config") or OPENING_EVIDENCE_CONFIG)
        eligible_run_ids = {
            int(run.get("id") or 0)
            for run in _canonical_official_runs(
                self.database.list_t1_overnight_selection_runs(limit=config["lookbackRunLimit"], status="done")
            )
        }
        return {
            **state,
            "version": OPENING_EVIDENCE_VERSION,
            "config": config,
            "schedulerAlive": bool(self._thread and self._thread.is_alive()),
            "todayJobs": [
                job for job in self.database.list_t1_opening_evidence_jobs(now_dt.date().isoformat())
                if int(job.get("runId") or 0) in eligible_run_ids
            ],
        }

    def run_due(self, now: Optional[datetime] = None, trigger_source: str = "scheduler") -> Dict[str, Any]:
        now_dt = now or self.clock()
        now_at = now_dt.isoformat(timespec="seconds")
        state = dict(self.database.get_app_setting(OPENING_EVIDENCE_STATE_KEY, {}) or {})
        config = _normalize_config(state.get("config") or OPENING_EVIDENCE_CONFIG)
        registered = self.register_upcoming_jobs(now_dt, config)
        processed: List[Dict[str, Any]] = []
        if config["enabled"] and now_dt.weekday() < 5:
            for job in self.database.list_t1_opening_evidence_jobs(now_dt.date().isoformat()):
                if job["status"] not in {"scheduled", "running"}:
                    continue
                if now_at > job["deadlineAt"]:
                    self._record_terminal_failure(job, "missed_deadline", "任务未在硬截止前发布，禁止事后补造。")
                    processed.append({"jobId": job["id"], "status": "missed_deadline"})
                    continue
                if now_at < job["dueAt"]:
                    continue
                lease_expires = (now_dt + timedelta(seconds=config["leaseSeconds"])).isoformat(timespec="seconds")
                acquired = self.database.acquire_t1_opening_evidence_job(
                    job["id"], self.worker_id, now_at, lease_expires
                )
                if not acquired:
                    continue
                processed.append(self._execute(acquired, config, trigger_source))
        state.update(
            {
                "version": OPENING_EVIDENCE_VERSION,
                "config": config,
                "lastCheckAt": now_at,
                "lastStatus": processed[-1]["status"] if processed else "waiting",
                "lastProcessed": processed[-8:],
                "updatedAt": now_at,
            }
        )
        self.database.set_app_setting(OPENING_EVIDENCE_STATE_KEY, state)
        return {**state, "registered": registered, "processed": processed}

    def register_upcoming_jobs(self, now: datetime, config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        config = _normalize_config(config or OPENING_EVIDENCE_CONFIG)
        runs = self.database.list_t1_overnight_selection_runs(
            limit=config["lookbackRunLimit"], status="done"
        )
        canonical = _canonical_official_runs(runs)
        registered = []
        for run in canonical:
            target = _next_weekday(str(run.get("recommendationDate") or ""))
            if not target or target < now.date().isoformat():
                continue
            if date.fromisoformat(target) > now.date() + timedelta(days=7):
                continue
            for evidence_type, due_key, deadline_key in (
                (OPENING_PRECHECK, "precheckTime", "precheckDeadline"),
                (OPENING_SNAPSHOT, "snapshotTime", "snapshotDeadline"),
            ):
                job = self.database.register_t1_opening_evidence_job(
                    {
                        "runId": int(run.get("id") or 0),
                        "targetTradeDate": target,
                        "evidenceType": evidence_type,
                        "evidenceVersion": OPENING_EVIDENCE_VERSION,
                        "dueAt": f"{target}T{config[due_key]}",
                        "deadlineAt": f"{target}T{config[deadline_key]}",
                    }
                )
                if job:
                    registered.append(job)
            if target > now.date().isoformat():
                self._ensure_scope_snapshot(run, target)
        return registered

    def evidence_for_run(self, run_id: int) -> Dict[str, Any]:
        return {
            "precheck": self.database.load_t1_opening_evidence(run_id, OPENING_PRECHECK),
            "snapshot": self.database.load_t1_opening_evidence(run_id, OPENING_SNAPSHOT),
            "jobs": [
                item for item in self.database.list_t1_opening_evidence_jobs(limit=500)
                if int(item.get("runId") or 0) == int(run_id)
            ],
        }

    def _scheduler_loop(self) -> None:
        while True:
            try:
                self.run_due()
                interval = self.status()["config"]["pollIntervalSeconds"]
            except Exception as error:  # noqa: BLE001 - scheduler must survive provider and database failures.
                LOGGER.exception("t1 opening evidence scheduler tick failed error=%s", error)
                interval = OPENING_EVIDENCE_CONFIG["pollIntervalSeconds"]
            time.sleep(interval)

    def _execute(self, job: Dict[str, Any], config: Dict[str, Any], trigger_source: str) -> Dict[str, Any]:
        try:
            if job["evidenceType"] == OPENING_PRECHECK:
                snapshot = self._collect_precheck(job, trigger_source)
            else:
                snapshot = self._collect_opening_snapshot(job, config, trigger_source)
            snapshot_id = self.database.append_t1_opening_evidence_snapshot(snapshot, [job["runId"]])
            terminal = "done" if snapshot["status"] in {"available", "partial", "ready"} else snapshot["status"]
            self.database.finish_t1_opening_evidence_job(job["id"], terminal)
            return {"jobId": job["id"], "snapshotId": snapshot_id, "status": terminal}
        except Exception as error:  # noqa: BLE001 - preserve a terminal failure instead of retrying past the slot.
            LOGGER.exception("t1 opening evidence job failed job=%s error=%s", job["id"], error)
            self._record_terminal_failure(job, "provider_failed", str(error))
            return {"jobId": job["id"], "status": "provider_failed", "error": str(error)}

    def _collect_precheck(self, job: Dict[str, Any], trigger_source: str) -> Dict[str, Any]:
        received_at = self.clock().isoformat(timespec="seconds")
        scope = self.database.load_t1_opening_evidence(job["runId"], OPENING_SCOPE)
        mappings = scope.get("sectorConstituentsByIndustry") or {}
        warnings = list(scope.get("warnings") or [])
        if not scope:
            warnings.append("目标交易日前未冻结行业成分映射，行业条件不可用。")
        cached = self.analysis_service.full_market_context_store.latest_cached()
        expected = int((cached.get("coverage") or {}).get("expectedRows") or cached.get("count") or 0)
        payload = {
            "version": OPENING_EVIDENCE_VERSION,
            "tradeDate": job["targetTradeDate"],
            "asOfTime": "09:29:00",
            "receivedAt": received_at,
            "triggerSource": trigger_source,
            "providerStatus": {
                "stock": "ready" if expected > 0 else "universe_missing",
                "index": "ready" if callable(getattr(self.analysis_service.full_market_context_store.tencent_provider, "fetch_indices", None)) else "unsupported",
                "sector": "ready" if mappings and any(item.get("expectedCount", 0) > 0 for item in mappings.values()) else "unavailable",
            },
            "expectedCount": expected,
            "sectorConstituentsByIndustry": mappings,
            "scopeSnapshotId": int(scope.get("snapshotId") or 0),
            "scopeFrozenAt": scope.get("snapshotPublishedAt") or "",
            "warnings": warnings,
        }
        published = self.clock().isoformat(timespec="seconds")
        status = "ready" if expected > 0 else "provider_failed"
        payload["decisionEligible"] = False
        return _snapshot_record(job, payload, received_at, published, status, False)

    def _ensure_scope_snapshot(self, run: Dict[str, Any], target_trade_date: str) -> Dict[str, Any]:
        run_id = int(run.get("id") or 0)
        existing = self.database.load_t1_opening_evidence(run_id, OPENING_SCOPE)
        if existing:
            return existing
        received_at = self.clock().isoformat(timespec="seconds")
        mappings: Dict[str, Any] = {}
        warnings = []
        for industry in _candidate_industries(run):
            try:
                rows = self.sector_provider.fetch_constituents(industry, "industry", limit=10_000)
                codes = sorted({str(item.get("code") or "").zfill(6) for item in rows if item.get("code")})
                mapping_hash = hashlib.sha256("|".join(codes).encode("utf-8")).hexdigest()
                mappings[industry] = {
                    "status": "available" if codes else "unavailable",
                    "expectedCount": len(codes),
                    "codes": codes,
                    "mappingVersion": mapping_hash[:16],
                }
            except Exception as error:  # noqa: BLE001 - each industry remains independently available.
                warnings.append(f"{industry}: {error}")
                mappings[industry] = {"status": "provider_failed", "expectedCount": 0, "codes": []}
        payload = {
            "version": OPENING_EVIDENCE_VERSION,
            "tradeDate": target_trade_date,
            "registeredFromRecommendationDate": run.get("recommendationDate") or "",
            "sectorConstituentsByIndustry": mappings,
            "warnings": warnings,
            "decisionEligible": False,
        }
        synthetic_job = {
            "targetTradeDate": target_trade_date,
            "evidenceType": OPENING_SCOPE,
            "evidenceVersion": OPENING_EVIDENCE_VERSION,
        }
        record = _snapshot_record(synthetic_job, payload, received_at, received_at, "ready", False)
        snapshot_id = self.database.append_t1_opening_evidence_snapshot(record, [run_id])
        return {**payload, "snapshotId": snapshot_id, "snapshotPublishedAt": received_at, "status": "ready"}

    def _collect_opening_snapshot(
        self, job: Dict[str, Any], config: Dict[str, Any], trigger_source: str
    ) -> Dict[str, Any]:
        received_at = self.clock().isoformat(timespec="seconds")
        full = self.analysis_service.full_market_context_store.refresh(source=config["source"])
        published = self.clock().isoformat(timespec="seconds")
        deadline = job["deadlineAt"]
        stocks = list(full.get("stocks") or [])
        eligible_stocks = [
            stock for stock in stocks
            if _quote_is_on_time(stock.trade_date, stock.quote_time, job["targetTradeDate"], deadline)
            and not stock.suspended
            and float(stock.price or 0) > 0
            and float(stock.amount or 0) > 0
        ]
        expected = int((full.get("coverage") or {}).get("expectedRows") or len(stocks))
        covered = len(eligible_stocks)
        coverage = round(covered / expected * 100, 2) if expected else 0.0
        down = len([stock for stock in eligible_stocks if float(stock.change or 0) < 0])
        down_ratio = round(down / covered * 100, 2) if covered else None
        market_status = "available" if coverage >= 90 else "unavailable"

        market = full.get("market") or {}
        index_rows = list(market.get("indices") or [])
        indices = _normalize_main_indices(index_rows, job["targetTradeDate"], deadline)
        index_available = len(indices) == len(MAIN_INDEX_CODES)
        worst_index = min(float(item["changePct"]) for item in indices) if index_available else None

        precheck = self.database.load_t1_opening_evidence(job["runId"], OPENING_PRECHECK)
        sector_mappings = precheck.get("sectorConstituentsByIndustry") or {}
        by_code = {str(stock.code).zfill(6): stock for stock in eligible_stocks}
        sector_returns = {}
        for industry, mapping in sector_mappings.items():
            codes = list(mapping.get("codes") or [])
            values = [float(by_code[code].change) for code in codes if code in by_code]
            sector_coverage = round(len(values) / len(codes) * 100, 2) if codes else 0.0
            sector_returns[industry] = {
                "status": "available" if sector_coverage >= 90 else "unavailable",
                "returnPct": round(sum(values) / len(values), 4) if values else None,
                "expectedCount": len(codes),
                "coveredCount": len(values),
                "coveragePct": sector_coverage,
                "mappingVersion": mapping.get("mappingVersion") or "",
            }

        on_time = published <= deadline
        any_condition = market_status == "available" or index_available or any(
            item["status"] == "available" for item in sector_returns.values()
        )
        decision_eligible = bool(on_time and any_condition and config["source"] != "mock")
        status = "available" if decision_eligible else "late_snapshot" if not on_time else "unavailable"
        provider_times = sorted(stock.quote_time for stock in eligible_stocks if stock.quote_time)
        payload = {
            "version": OPENING_EVIDENCE_VERSION,
            "tradeDate": job["targetTradeDate"],
            "asOfTime": "09:31:00",
            "providerTimestamp": provider_times[-1] if provider_times else "",
            "receivedAt": received_at,
            "snapshotPublishedAt": published,
            "triggerSource": trigger_source,
            "source": full.get("source") or config["source"],
            "expectedCount": expected,
            "coveredCount": covered,
            "coveragePct": coverage,
            "marketCoveragePct": coverage,
            "marketBreadthStatus": market_status,
            "downCount": down,
            "downRatioPct": down_ratio,
            "mainIndices": indices,
            "mainIndexStatus": "available" if index_available else "unavailable",
            "mainIndexWorstChangePct": round(worst_index, 4) if worst_index is not None else None,
            "sectorReturnsByIndustry": sector_returns,
            "decisionEligible": decision_eligible,
            "lateBackfill": not on_time,
        }
        return _snapshot_record(job, payload, received_at, published, status, decision_eligible)

    def _record_terminal_failure(self, job: Dict[str, Any], status: str, error: str) -> None:
        now = self.clock().isoformat(timespec="seconds")
        payload = {
            "version": OPENING_EVIDENCE_VERSION,
            "tradeDate": job["targetTradeDate"],
            "decisionEligible": False,
            "lateBackfill": status in {"missed_deadline", "late_snapshot"},
            "error": error,
        }
        snapshot = _snapshot_record(job, payload, now, now, status, False)
        self.database.append_t1_opening_evidence_snapshot(snapshot, [job["runId"]])
        self.database.finish_t1_opening_evidence_job(job["id"], status, error)


def _snapshot_record(
    job: Dict[str, Any], payload: Dict[str, Any], received_at: str, published_at: str,
    status: str, decision_eligible: bool,
) -> Dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "targetTradeDate": job["targetTradeDate"],
        "evidenceType": job["evidenceType"],
        "evidenceVersion": job["evidenceVersion"],
        "providerTimestamp": payload.get("providerTimestamp") or "",
        "receivedAt": received_at,
        "snapshotPublishedAt": published_at,
        "status": status,
        "decisionEligible": decision_eligible,
        "lateBackfill": bool(payload.get("lateBackfill")),
        "payload": payload,
        "payloadHash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "scope": job["evidenceType"],
    }


def _canonical_official_runs(runs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    failure_modes = {"rules_fallback", "ai_technical_failure_zero_pick", "ai_unavailable_zero_pick", "ai_failed"}
    for run in runs:
        request = run.get("request") or {}
        market = run.get("market") or {}
        role = str(market.get("runRole") or request.get("runRole") or "")
        if run.get("status") != "done" or bool(request.get("force")) or role in {"research", "legacy", "invalid"}:
            continue
        if str(run.get("selectionMode") or "") in failure_modes:
            continue
        if str((market.get("strategyPolicy") or {}).get("strategyPolicyVersion") or "") != OPENING_STRATEGY_POLICY_VERSION:
            continue
        recommendation_date = str(run.get("recommendationDate") or "")
        if recommendation_date:
            by_date.setdefault(recommendation_date, []).append(run)
    selected = []
    for recommendation_date in sorted(by_date):
        candidates = sorted(
            by_date[recommendation_date],
            key=lambda row: (str(row.get("scanTime") or "") != "14:40", -int(row.get("id") or 0)),
        )
        selected.append(candidates[0])
    return selected


def _candidate_industries(run: Dict[str, Any]) -> List[str]:
    result = []
    for pick in run.get("finalPicks") or []:
        industry = str(pick.get("industrySector") or (pick.get("item") or {}).get("industrySector") or "").strip()
        if not industry:
            matched = ((pick.get("sectorEvidence") or {}).get("matchedSectors") or [])
            industry = next(
                (str(item.get("sectorName") or "").strip() for item in matched if item.get("sectorType") == "industry"),
                "",
            )
        if industry and industry not in result:
            result.append(industry)
    return result


def _normalize_main_indices(rows: List[Dict[str, Any]], trade_date: str, deadline: str) -> List[Dict[str, Any]]:
    result = []
    for row in rows:
        code = str(row.get("code") or "").replace("s_", "")
        if code not in MAIN_INDEX_CODES:
            continue
        quote_time = str(row.get("quoteTime") or row.get("providerTimestamp") or "")
        row_trade_date = str(row.get("tradeDate") or quote_time[:10])
        change = _number(row.get("changePct", row.get("change")))
        if change is None or not _quote_is_on_time(row_trade_date, quote_time, trade_date, deadline):
            continue
        result.append(
            {"code": code, "name": MAIN_INDEX_CODES[code], "changePct": change, "quoteTime": quote_time}
        )
    by_code = {item["code"]: item for item in result}
    return [by_code[code] for code in MAIN_INDEX_CODES if code in by_code]


def _quote_is_on_time(row_date: str, quote_time: str, trade_date: str, deadline: str) -> bool:
    return bool(row_date == trade_date and quote_time and quote_time <= deadline)


def _normalize_config(value: Dict[str, Any]) -> Dict[str, Any]:
    raw = {**OPENING_EVIDENCE_CONFIG, **dict(value or {})}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "pollIntervalSeconds": max(5, min(60, int(raw.get("pollIntervalSeconds") or 15))),
        "source": str(raw.get("source") or "tencent"),
        "precheckTime": str(raw.get("precheckTime") or "09:29:00"),
        "precheckDeadline": str(raw.get("precheckDeadline") or "09:30:59"),
        "snapshotTime": str(raw.get("snapshotTime") or "09:31:00"),
        "snapshotDeadline": str(raw.get("snapshotDeadline") or "09:31:59"),
        "leaseSeconds": max(30, min(300, int(raw.get("leaseSeconds") or 90))),
        "lookbackRunLimit": max(50, min(2000, int(raw.get("lookbackRunLimit") or 500))),
    }


def _next_weekday(value: str) -> str:
    try:
        current = date.fromisoformat(value[:10]) + timedelta(days=1)
    except ValueError:
        return ""
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current.isoformat()


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
