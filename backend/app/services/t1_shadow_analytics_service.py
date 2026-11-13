from __future__ import annotations

import hashlib
import logging
import math
import os
import socket
import threading
import time
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional

from backend.app.database import Database
from backend.app.engines.t1_v3_pipeline import PIPELINE_VERSION as T1_V3_PIPELINE_VERSION
from backend.app.services.t1_overnight_selection_service import T1_STRATEGY_POLICY_VERSION


CALCULATION_VERSION = "t1_shadow_analytics_v6"
LEGACY_POLICY_VERSION = "legacy_shadow_open15m_v1"
LEGACY_EXECUTION_PROFILE_VERSION = "legacy_shadow_diagnostic_mixed"
DEFAULT_OUTCOME_SCENARIO = "open_15m"
SHADOW_ANALYTICS_STATE_KEY = "t1_shadow_analytics_worker_state"
SHADOW_ANALYTICS_CONFIG = {
    "enabled": True,
    "pollIntervalSeconds": 60,
    "leaseSeconds": 120,
    "retryBaseSeconds": 30,
    "maxAttempts": 5,
    "lookbackRunLimit": 5000,
    "batchSize": 50,
}
REPORT_CHECKPOINTS = (20, 60, 120)
LOGGER = logging.getLogger("ai_stock.t1_shadow_analytics")

FAMILY_META = {
    "legacy_tail_rule": {
        "label": "规则尾盘影子",
        "roles": ["tail_rule"],
        "description": "诊断规则尾盘选股表现，不作为因果实验自动晋级。",
    },
    "legacy_tail_ai": {
        "label": "AI 尾盘影子",
        "roles": ["tail_ai"],
        "description": "诊断 DeepSeek 尾盘旁路表现，不作为因果实验自动晋级。",
    },
    "policy_experiment": {
        "label": "本期策略实验",
        "roles": ["policy_market_gate", "policy_risk_gate", "policy_diversity", "policy_rank3", "policy_net_expectation", "policy_adaptive_exit"],
        "description": "使用同一冻结排序的控制组与处理组做前向配对验证。",
    },
    "v3_main_experiment": {
        "label": "V3 全链路主实验",
        "roles": ["v3_main"],
        "description": "V3 固定版本与同日当前正式策略的真实 open_15m 扣费收益配对实验。",
    },
}

ROLE_META = {
    "tail_rule": ("规则尾盘", "角色诊断"),
    "tail_ai": ("AI 尾盘", "角色诊断"),
    "policy_market_gate": ("市场门禁", "市场风险上限处理组减控制组"),
    "policy_risk_gate": ("风险门禁", "个股负向门禁处理组减控制组"),
    "policy_diversity": ("组合去同质化", "行业和主驱动独立处理组减控制组"),
    "policy_rank3": ("第三槽", "Rank 3 相对现金槽的增量"),
    "policy_net_expectation": ("净期望排序", "保守净期望影子排序减当前选择策略"),
    "policy_adaptive_exit": ("动态退出", "动态退出相对固定 open_15m 的增量"),
    "v3_main": ("V3 主策略", "V3 组合相对同日当前正式策略"),
}


class T1ShadowAnalyticsService:
    def __init__(self, database: Database, clock: Any = None):
        self.database = database
        self.clock = clock or datetime.now
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        self._tick_lock = threading.Lock()

    def start_scheduler(self) -> Dict[str, Any]:
        state = self.status()
        if not state["config"]["enabled"]:
            return state
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._thread = threading.Thread(target=self._scheduler_loop, name="t1-shadow-analytics", daemon=True)
            self._thread.start()
        return self.status()

    def status(self) -> Dict[str, Any]:
        state = dict(self.database.get_app_setting(SHADOW_ANALYTICS_STATE_KEY, {}) or {})
        config = _normalize_worker_config(state.get("config") or SHADOW_ANALYTICS_CONFIG)
        jobs = self.database.list_t1_shadow_aggregate_jobs(limit=500)
        status_counts: Dict[str, int] = {}
        for job in jobs:
            key = str(job.get("status") or "unknown")
            status_counts[key] = status_counts.get(key, 0) + 1
        reports = self.database.list_t1_shadow_evaluation_reports(outcome_scenario="")
        return {
            **state,
            "config": config,
            "calculationVersion": CALCULATION_VERSION,
            "schedulerAlive": bool(self._thread and self._thread.is_alive()),
            "jobStatusCounts": status_counts,
            "recentJobs": jobs[:20],
            "frozenReportCount": len(reports),
        }

    def run_due(self, trigger_source: str = "scheduler") -> Dict[str, Any]:
        if not self._tick_lock.acquire(blocking=False):
            return {**self.status(), "lastStatus": "busy", "processed": []}
        try:
            now = self.clock()
            now_at = now.isoformat(timespec="seconds")
            state = dict(self.database.get_app_setting(SHADOW_ANALYTICS_STATE_KEY, {}) or {})
            config = _normalize_worker_config(state.get("config") or SHADOW_ANALYTICS_CONFIG)
            registered = self._consistency_scan(config)
            canonical_run_ids = {int(job.get("canonicalRunId") or 0) for job in registered}
            processed: List[Dict[str, Any]] = []
            if config["enabled"]:
                jobs = sorted(self.database.list_t1_shadow_aggregate_jobs(limit=5000), key=lambda item: int(item["id"]))
                for job in jobs:
                    if len(processed) >= config["batchSize"]:
                        break
                    if job.get("status") not in {"pending", "retry_wait", "running"}:
                        continue
                    if job.get("nextRetryAt") and str(job["nextRetryAt"]) > now_at:
                        continue
                    lease_expires = (now + timedelta(seconds=config["leaseSeconds"])).isoformat(timespec="seconds")
                    acquired = self.database.acquire_t1_shadow_aggregate_job(
                        int(job["id"]), self.worker_id, now_at, lease_expires
                    )
                    if not acquired:
                        continue
                    try:
                        run = self.database.load_t1_overnight_selection_run(int(acquired["canonicalRunId"]))
                        if not run:
                            raise ValueError("聚合来源 Run 不存在")
                        if int(run.get("id") or 0) not in canonical_run_ids:
                            self.database.complete_t1_shadow_aggregate_job(
                                int(acquired["id"]), self.worker_id, self.clock().isoformat(timespec="seconds")
                            )
                            processed.append({"jobId": acquired["id"], "runId": run["id"], "status": "skipped_noncanonical", "factCount": 0})
                            continue
                        fact_count = self._rebuild_run(run)
                        self.database.complete_t1_shadow_aggregate_job(
                            int(acquired["id"]), self.worker_id, self.clock().isoformat(timespec="seconds")
                        )
                        processed.append({"jobId": acquired["id"], "runId": run["id"], "status": "done", "factCount": fact_count})
                    except Exception as error:  # noqa: BLE001 - failed jobs must retain retry state.
                        attempt = int(acquired.get("attemptCount") or 1)
                        terminal = attempt >= config["maxAttempts"]
                        retry_at = self.clock() + timedelta(seconds=config["retryBaseSeconds"] * (2 ** max(0, attempt - 1)))
                        self.database.fail_t1_shadow_aggregate_job(
                            int(acquired["id"]), self.worker_id,
                            self.clock().isoformat(timespec="seconds"), retry_at.isoformat(timespec="seconds"),
                            str(error), terminal=terminal,
                        )
                        processed.append({"jobId": acquired["id"], "runId": acquired["canonicalRunId"], "status": "failed" if terminal else "retry_wait", "error": str(error)})
                        LOGGER.exception("t1 shadow aggregate job failed id=%s error=%s", acquired["id"], error)
            report_result = self.freeze_due_reports()
            state.update(
                {
                    "config": config,
                    "lastCheckAt": now_at,
                    "lastStatus": processed[-1]["status"] if processed else "waiting",
                    "lastProcessed": processed[-10:],
                    "lastRegisteredCount": len(registered),
                    "lastReportResult": report_result,
                    "triggerSource": trigger_source,
                    "updatedAt": now_at,
                }
            )
            self.database.set_app_setting(SHADOW_ANALYTICS_STATE_KEY, state)
            return {**state, "registered": registered, "processed": processed}
        finally:
            self._tick_lock.release()

    def _scheduler_loop(self) -> None:
        while True:
            try:
                self.run_due()
                interval = self.status()["config"]["pollIntervalSeconds"]
            except Exception as error:  # noqa: BLE001 - scheduler must recover on the next tick.
                LOGGER.exception("t1 shadow analytics scheduler tick failed error=%s", error)
                interval = SHADOW_ANALYTICS_CONFIG["pollIntervalSeconds"]
            time.sleep(interval)

    def _consistency_scan(self, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        runs = self.database.list_t1_overnight_selection_runs(limit=config["lookbackRunLimit"], status="done")
        canonical = _canonical_runs(runs)
        canonical_ids = [int(run.get("id") or 0) for run in canonical]
        self.database.delete_t1_shadow_daily_facts_outside_runs(canonical_ids)
        registered = []
        for run in canonical:
            registered.append(
                self.database.register_t1_shadow_aggregate_job(
                    int(run.get("id") or 0), _run_source_revision(run), CALCULATION_VERSION
                )
            )
        return registered

    def rebuild(self) -> Dict[str, Any]:
        runs = self.database.list_t1_overnight_selection_runs(limit=5000, status="done")
        canonical = _canonical_runs(runs)
        self.database.delete_t1_shadow_daily_facts_outside_runs([int(run.get("id") or 0) for run in canonical])
        migrated = 0
        for run in canonical:
            migrated += self._rebuild_run(run)
        reports = self.freeze_due_reports()
        return {
            "status": "done",
            "canonicalRunCount": len(canonical),
            "factCount": migrated,
            "reportResult": reports,
            "generatedAt": _now(),
            "calculationVersion": CALCULATION_VERSION,
        }

    def summary(
        self,
        shadow_family: str = "",
        shadow_role: str = "",
        scope: str = "",
        strategy_policy_version: str = "",
        outcome_scenario: str = DEFAULT_OUTCOME_SCENARIO,
        execution_profile_version: str = "",
        window: str = "all",
        date_from: str = "",
        date_to: str = "",
    ) -> Dict[str, Any]:
        if not shadow_family:
            families = [
                self.summary(
                    shadow_family=family,
                    shadow_role="",
                    scope=scope,
                    strategy_policy_version=strategy_policy_version,
                    outcome_scenario=outcome_scenario,
                    execution_profile_version=execution_profile_version,
                    window=window,
                    date_from=date_from,
                    date_to=date_to,
                )
                for family in FAMILY_META
            ]
            return {
                "families": families,
                "generatedAt": _now(),
                "sourceWatermark": _source_watermark([row for family in families for row in family.get("facts", [])]),
                "calculationVersion": CALCULATION_VERSION,
                "comparabilityStatus": "families_isolated",
                "note": "三个影子家族分别统计，不生成影子总收益。",
            }
        if shadow_family not in FAMILY_META:
            raise ValueError("未知影子家族")
        allowed_roles = FAMILY_META[shadow_family]["roles"]
        if shadow_role and shadow_role not in allowed_roles:
            raise ValueError("影子角色不属于当前家族")
        facts = self.database.list_t1_shadow_strategy_daily_facts(
            shadow_family=shadow_family,
            shadow_role=shadow_role,
            scope=scope,
            strategy_policy_version=strategy_policy_version,
            outcome_scenario=outcome_scenario,
            execution_profile_version=execution_profile_version,
            date_from=date_from,
            date_to=date_to,
        )
        roles = [shadow_role] if shadow_role else allowed_roles
        role_summaries = []
        selected_facts: List[Dict[str, Any]] = []
        for role in roles:
            role_facts = [fact for fact in facts if fact.get("shadowRole") == role]
            versions = sorted({str(fact.get("strategyPolicyVersion") or "") for fact in role_facts}, reverse=True)
            if strategy_policy_version:
                versions = [strategy_policy_version]
            if not versions:
                if shadow_family.startswith("legacy_"):
                    versions = [LEGACY_POLICY_VERSION]
                elif shadow_family == "v3_main_experiment":
                    versions = [T1_V3_PIPELINE_VERSION]
                else:
                    versions = [T1_STRATEGY_POLICY_VERSION]
            for version in versions:
                version_facts = [fact for fact in role_facts if fact.get("strategyPolicyVersion") == version]
                execution_versions = sorted(
                    {str(fact.get("executionProfileVersion") or "") for fact in version_facts}, reverse=True
                )
                if execution_profile_version:
                    execution_versions = [execution_profile_version]
                if not execution_versions:
                    execution_versions = [execution_profile_version]
                for execution_version in execution_versions:
                    isolated_facts = [
                        fact for fact in version_facts
                        if str(fact.get("executionProfileVersion") or "") == execution_version
                    ]
                    window_facts = _select_window(isolated_facts, window)
                    selected_facts.extend(window_facts)
                    aggregated = _aggregate(role, version, window_facts, shadow_family)
                    aggregated["executionProfileVersion"] = execution_version
                    role_summaries.append(aggregated)
        role_summaries.sort(
            key=lambda item: (
                str(item.get("lastUsableDate") or ""),
                str(item.get("strategyPolicyVersion") or ""),
                str(item.get("executionProfileVersion") or ""),
            ),
            reverse=True,
        )
        # An explicitly selected window may legitimately be empty (for example,
        # the 61-120 confirmation window before 61 effective days exist).  Do not
        # fall back to the unfiltered facts, otherwise the page silently shows a
        # different window from the one selected by the user.
        facts = list({int(fact.get("id") or 0): fact for fact in selected_facts}.values())
        facts.sort(key=lambda fact: (str(fact.get("recommendationDate") or ""), int(fact.get("canonicalRunId") or 0)))
        usable_dates = [fact.get("recommendationDate") for fact in facts if _fact_is_effective(fact)]
        return {
            "shadowFamily": shadow_family,
            "familyLabel": FAMILY_META[shadow_family]["label"],
            "description": FAMILY_META[shadow_family]["description"],
            "roles": role_summaries,
            "facts": [_public_fact(fact) for fact in facts],
            "generatedAt": _now(),
            "sourceWatermark": _source_watermark(facts),
            "calculationVersion": CALCULATION_VERSION,
            "comparabilityStatus": "role_and_version_isolated",
            "coverageIncomplete": any(bool(role.get("coverageIncomplete")) for role in role_summaries),
            "sourceType": "historical_backfill" if shadow_family.startswith("legacy_") else "forward_policy_experiment",
            "historyBackfillStatus": "rebuilt" if shadow_family.startswith("legacy_") else "not_applicable",
            "firstUsableDate": min(usable_dates) if usable_dates else "",
            "migratedRunCount": len({int(fact.get("canonicalRunId") or 0) for fact in facts}),
            "missingRunCount": len([
                fact for fact in facts
                if fact.get("status") in {"data_insufficient", "technical_failure"}
            ]),
            "window": window,
        }

    def series(self, **filters: Any) -> Dict[str, Any]:
        result = self.summary(**filters)
        if result.get("families") is not None:
            return result
        return {
            key: result.get(key)
            for key in (
                "shadowFamily", "familyLabel", "facts", "generatedAt", "sourceWatermark",
                "calculationVersion", "comparabilityStatus", "coverageIncomplete",
            )
        }

    def evaluation_report(self, **filters: Any) -> Dict[str, Any]:
        shadow_family = str(filters.get("shadow_family") or "")
        shadow_role = str(filters.get("shadow_role") or "")
        scope = str(filters.get("scope") or "")
        strategy_policy_version = str(filters.get("strategy_policy_version") or "")
        outcome_scenario = str(filters.get("outcome_scenario") or DEFAULT_OUTCOME_SCENARIO)
        execution_profile_version = str(filters.get("execution_profile_version") or "")
        window = str(filters.get("window") or "all")
        window_type = {
            "20d": "20d",
            "recent20": "20d",
            "discovery60": "60d",
            "60d": "60d",
            "confirmation60": "120d",
            "61-120": "120d",
        }.get(window, "")
        reports = self.database.list_t1_shadow_evaluation_reports(
            shadow_family=shadow_family,
            shadow_role=shadow_role,
            scope=scope,
            strategy_policy_version=strategy_policy_version,
            outcome_scenario=outcome_scenario,
            execution_profile_version=execution_profile_version,
            window_type=window_type,
        )
        live = self.summary(**filters)
        return {
            "shadowFamily": live.get("shadowFamily"),
            "familyLabel": live.get("familyLabel"),
            "reports": reports,
            "livePreview": live.get("roles") or live.get("families") or [],
            "generatedAt": _now(),
            "sourceWatermark": live.get("sourceWatermark"),
            "calculationVersion": CALCULATION_VERSION,
            "coverageIncomplete": live.get("coverageIncomplete"),
            "appendOnly": True,
        }

    def freeze_due_reports(self) -> Dict[str, Any]:
        facts = self.database.list_t1_shadow_strategy_daily_facts(outcome_scenario="")
        groups: Dict[tuple[str, str, str, str, str], List[Dict[str, Any]]] = {}
        for fact in facts:
            key = (
                str(fact.get("shadowFamily") or ""), str(fact.get("shadowRole") or ""),
                str(fact.get("strategyPolicyVersion") or ""), str(fact.get("outcomeScenario") or ""),
                str(fact.get("executionProfileVersion") or ""),
            )
            groups.setdefault(key, []).append(fact)
        frozen = []
        for (family, role, version, scenario, execution_version), rows in groups.items():
            ordered = sorted(rows, key=lambda item: (str(item.get("recommendationDate") or ""), int(item.get("canonicalRunId") or 0)))
            effective = [row for row in ordered if _fact_is_effective(row)]
            for checkpoint in REPORT_CHECKPOINTS:
                if len(effective) < checkpoint:
                    continue
                checkpoint_rows = effective[-checkpoint:] if checkpoint == 120 else effective[:checkpoint]
                start_date = str(checkpoint_rows[0].get("recommendationDate") or "")
                end_date = str(checkpoint_rows[-1].get("recommendationDate") or "")
                report_rows = [
                    row for row in ordered
                    if start_date <= str(row.get("recommendationDate") or "") <= end_date
                ]
                metrics = _aggregate(role, version, report_rows, family)
                coverage = metrics.get("coveragePct")
                blockers = []
                if coverage is None or float(coverage) < 95:
                    blockers.append("覆盖率低于 95%，不得形成晋级结论。")
                if family.startswith("legacy_"):
                    blockers.append("既有尾盘影子仅作历史诊断，不属于本期因果实验。")
                if family == "v3_main_experiment" and checkpoint == 120:
                    blockers.extend(_v3_admission_blockers(metrics, report_rows, version))
                report = self.database.append_t1_shadow_evaluation_report(
                    {
                        "shadowFamily": family,
                        "shadowRole": role,
                        "scope": str(report_rows[-1].get("scope") or "") if report_rows else "",
                        "strategyPolicyVersion": version,
                        "outcomeScenario": scenario,
                        "executionProfileVersion": execution_version,
                        "windowType": f"{checkpoint}d",
                        "windowStartDate": start_date,
                        "windowEndDate": end_date,
                        "effectiveDayCount": int(metrics.get("effectiveDays") or 0),
                        "hitDayCount": int(metrics.get("hitDays") or 0),
                        "outcomeCount": int(metrics.get("stockOutcomeCount") or 0),
                        "coveragePct": coverage,
                        "metrics": metrics,
                        "admission": {
                            "checkpointDays": checkpoint,
                            "minimumCoveragePct": 95,
                            "coverageSatisfied": not blockers or all("覆盖率" not in item for item in blockers),
                            "automaticPromotion": False,
                            "manualPromotionReviewAllowed": bool(
                                family == "v3_main_experiment" and checkpoint == 120 and not blockers
                            ),
                            "blockers": blockers,
                        },
                        "reviewStatus": metrics.get("reviewStatus") or "collecting",
                        # A corrected calculation contract must create a new
                        # immutable report revision even when the Run facts did
                        # not change.
                        "sourceWatermark": _report_source_watermark(report_rows),
                    }
                )
                frozen.append(report)
        return {
            "eligibleGroupCount": len(groups),
            "eligibleReportCount": len(frozen),
            "createdReportCount": len([report for report in frozen if report.get("created")]),
            "latestReportIds": [int(report.get("id") or 0) for report in frozen[-20:]],
        }

    def _rebuild_run(self, run: Dict[str, Any]) -> int:
        self.database.delete_t1_shadow_daily_facts_for_run(int(run.get("id") or 0))
        market = run.get("market") or {}
        comparison = ((market.get("shadowComparison") or {}).get("outcomeComparison") or {})
        count = 0
        count += self._write_legacy_fact(run, "legacy_tail_rule", "tail_rule", comparison.get("shadow") or {}, comparison.get("rows") or [])
        count += self._write_legacy_fact(run, "legacy_tail_ai", "tail_ai", comparison.get("deepSeekTail") or {}, comparison.get("deepSeekTailRows") or [])
        baseline = market.get("baselineComparison") or {}
        strategy = market.get("strategyPolicy") or {}
        policy_version = str(strategy.get("strategyPolicyVersion") or "")
        if policy_version:
            mappings = (
                ("policy_market_gate", "marketGateTreatment", "selectionPolicyControl", "selection_policy_vs_market_gate"),
                ("policy_risk_gate", "riskGateTreatment", "riskGateControl", "risk_gate_control_vs_treatment"),
                ("policy_diversity", "portfolioDiversityTreatment", "riskGateTreatment", "risk_gate_vs_diversity"),
                ("policy_rank3", "rank3", "rank3CashControl", "rank3_vs_cash"),
                ("policy_net_expectation", "netExpectationTreatment", "netExpectationControl", "selection_policy_vs_net_expectation"),
                ("policy_adaptive_exit", "selectionPolicyTreatmentAdaptiveExit", "selectionPolicyTreatment", "open15m_vs_adaptive_exit"),
            )
            for role, treatment_key, control_key, comparison_key in mappings:
                treatment = baseline.get(treatment_key) or {}
                control = baseline.get(control_key) or {}
                if role == "policy_rank3" and not bool((strategy.get("rank3Audit") or {}).get("available")):
                    continue
                if treatment or control:
                    fact = _policy_fact(run, role, policy_version, treatment, control, comparison_key)
                    self.database.upsert_t1_shadow_strategy_daily_fact(fact)
                    count += 1
        v3 = market.get("v3Strategy") or {}
        if v3:
            fact = _v3_main_fact(run, v3, baseline)
            self.database.upsert_t1_shadow_strategy_daily_fact(fact)
            count += 1
        return count

    def _write_legacy_fact(
        self,
        run: Dict[str, Any],
        family: str,
        role: str,
        summary: Dict[str, Any],
        rows: List[Dict[str, Any]],
    ) -> int:
        market = run.get("market") or {}
        picks_key = "shadowTop3" if role == "tail_rule" else "shadowDeepSeekTailTop3"
        shadow_payload = market.get(picks_key) or {}
        picks = (shadow_payload.get("picks") or [])
        status, status_reason = _legacy_fact_status(run, role, shadow_payload, summary, picks, rows)
        tradeable = int(summary.get("tradeableCount") or 0)
        holding = int(summary.get("count") or len(picks) or len(rows) or 0)
        slot_count = max(1, int(summary.get("slotCount") or 3))
        missing = max(0, holding - tradeable)
        target_date = next((str(row.get("nextTradeDate") or row.get("targetTradeDate") or "") for row in rows if isinstance(row, dict)), "")
        treatment_return = (
            0.0 if status == "active_cash"
            else _number(summary.get("portfolioReturnPct")) if status == "ok"
            else None
        )
        fact = {
            "recommendationDate": run.get("recommendationDate") or "",
            "targetTradeDate": target_date,
            "canonicalRunId": int(run.get("id") or 0),
            "shadowFamily": family,
            "shadowRole": role,
            "scope": "role_diagnostic",
            # Legacy tail algorithms have their own identity.  The current formal
            # selector version must never split an unchanged historical strategy.
            "strategyPolicyVersion": LEGACY_POLICY_VERSION,
            "outcomeScenario": DEFAULT_OUTCOME_SCENARIO,
            # Historical diagnostics span more than one old execution profile.
            # Keep them in one explicitly diagnostic bucket instead of silently
            # presenting the newest profile slice as the whole history.
            "executionProfileVersion": LEGACY_EXECUTION_PROFILE_VERSION,
            "comparisonKey": f"{role}_descriptive",
            "controlRole": "official_descriptive_only",
            "portfolioSlotCount": slot_count,
            "holdingCount": holding,
            # Historical summaries sometimes stored missing outcome rows as
            # ``cashSlots``.  Those are unknown results, not a strategy choice
            # to hold cash.  Derive cash only from the planned portfolio slots.
            "cashSlotCount": max(0, slot_count - holding),
            "stockOutcomeCount": tradeable,
            "missingCount": missing,
            "coveragePct": round(tradeable / holding * 100, 2) if holding else None,
            "treatmentNetReturnPct": treatment_return,
            "status": status,
            "generationStatus": "failed" if status == "technical_failure" else "done",
            "sourceType": "historical_shadow_comparison",
            "sourceRevision": _run_source_revision(run),
            "calculationVersion": CALCULATION_VERSION,
            "detail": {
                "picks": picks,
                "rows": rows,
                "summary": summary,
                "statusReason": status_reason,
                "sourceFormalSelectionVersion": str(market.get("selectionPolicyVersion") or ""),
                "sourceExecutionProfileVersion": str((market.get("executionProfile") or {}).get("version") or ""),
            },
        }
        self.database.upsert_t1_shadow_strategy_daily_fact(fact)
        return 1


def _canonical_runs(runs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for run in runs:
        if run.get("status") != "done":
            continue
        request = run.get("request") or {}
        market = run.get("market") or {}
        role = str(market.get("runRole") or request.get("runRole") or "")
        mode = str(run.get("selectionMode") or "")
        if bool(request.get("force")) or role in {"research", "legacy", "invalid"}:
            continue
        recommendation_date = str(run.get("recommendationDate") or "")
        if recommendation_date:
            by_date.setdefault(recommendation_date, []).append(run)
    selected = []
    for recommendation_date in sorted(by_date):
        candidates = by_date[recommendation_date]
        technical = [row for row in candidates if _is_technical_failure_mode(str(row.get("selectionMode") or ""))]
        usable = [row for row in candidates if row not in technical]
        usable.sort(key=lambda row: (str(row.get("scanTime") or "") != "14:40", -int(row.get("id") or 0)))
        if usable:
            selected.append(usable[0])
        # Reliability is attempt-based.  Retain scheduled technical failures even
        # when a later same-day rerun succeeds; performance remains day-based.
        selected.extend(sorted(technical, key=lambda row: int(row.get("id") or 0)))
    return selected


def _is_technical_failure_mode(mode: str) -> bool:
    return str(mode or "") in {
        "ai_technical_failure_zero_pick",
        "ai_unavailable_zero_pick",
        "ai_failed",
    }


def _legacy_fact_status(
    run: Dict[str, Any],
    role: str,
    shadow_payload: Dict[str, Any],
    summary: Dict[str, Any],
    picks: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
) -> tuple[str, str]:
    """Classify every scheduled legacy-shadow day without survivorship bias."""
    mode = str(run.get("selectionMode") or "")
    market = run.get("market") or {}
    archive = market.get("legacyShadowArchive") or {}
    archived_roles = {str(item) for item in archive.get("archivedRoles") or []}
    archive_role = "tail_ai" if role == "tail_ai" else "rules"
    if archive.get("status") == "future_generation_stopped" and archive_role in archived_roles:
        return "not_applicable", "该旧影子角色已停止新增；历史只读保留，不能把停止运行解释成技术失败或主动空仓。"
    summary_status = str(summary.get("status") or "").lower()
    summary_count = int(summary.get("count") or len(picks) or len(rows) or 0)
    tradeable_count = int(summary.get("tradeableCount") or 0)
    error = str(shadow_payload.get("error") or "").strip()
    reason = str(shadow_payload.get("reason") or "").strip()
    text = f"{error} {reason}".strip()
    if picks or rows or summary_count > 0:
        if summary_status in {"technical_failure", "failed", "error"}:
            return "technical_failure", text or "影子生成或执行发生技术失败。"
        # Old aggregation could publish an ``ok`` portfolio return after
        # replacing missing stock outcomes with 0%.  That is unknown data, not
        # cash, so it must not enter return/win-rate statistics.
        if tradeable_count < summary_count:
            return "data_insufficient", "影子已经形成持仓，但部分股票缺少完整次日执行结果。"
        if summary_status == "ok" and _number(summary.get("portfolioReturnPct")) is not None:
            return "ok", "影子组合和执行结果完整。"
        return "data_insufficient", text or "影子已经形成持仓，但次日执行结果不完整。"
    if mode == "market_risk_zero_pick":
        return "active_cash", "市场风险门禁明确选择 Top0，当日按现金收益 0% 计入。"
    if role == "tail_ai":
        shadow_status = str(shadow_payload.get("status") or "").lower()
        active_cash_markers = ("返回 Top0", "没有可用 T1 尾盘证据候选")
        # Tail-AI is an independent legacy strategy.  Its explicit normal Top0
        # decision wins over a failure mode belonging to the formal selector.
        if shadow_status in {"ai_zero_pick", "zero_pick", "active_cash"} or any(marker in text for marker in active_cash_markers):
            return "active_cash", text or "AI 正常决定不生成尾盘影子持仓。"
        if shadow_status in {"technical_failure", "failed", "error"} or error:
            return "technical_failure", error or text or "AI 调用或输出失败。"
        if _is_technical_failure_mode(mode):
            return "technical_failure", text or str(run.get("error") or "") or "AI 调用或输出失败。"
    if shadow_payload or summary:
        return "data_insufficient", text or "影子链路已运行，但没有完整的可验证结果。"
    return "not_applicable", "该历史日期尚未启用此影子角色，不能计为主动空仓或技术失败。"


def _policy_fact(
    run: Dict[str, Any],
    role: str,
    policy_version: str,
    treatment: Dict[str, Any],
    control: Dict[str, Any],
    comparison_key: str,
) -> Dict[str, Any]:
    market = run.get("market") or {}
    strategy = market.get("strategyPolicy") or {}
    treatment_count = _summary_holding_count(treatment)
    treatment_return = _complete_portfolio_return(treatment)
    control_return = _complete_portfolio_return(control)
    applicability_status = "applicable"
    applicability_reason = "处理组与同日控制组均有完整的可执行结果。"

    if role == "policy_market_gate" and treatment_count == 0:
        treatment_return = 0.0
        applicability_status = "active_cash"
        applicability_reason = "市场门禁明确将处理组降为 Top0，当日按现金收益 0% 计入。"
    elif role == "policy_risk_gate" and treatment_count == 0:
        gate_audit = strategy.get("negativeGateAudit") or {}
        candidate_count = int(gate_audit.get("candidateCount") or 0)
        blocked_count = int(gate_audit.get("blockedCandidateCount") or 0)
        if candidate_count > 0 and blocked_count >= candidate_count:
            treatment_return = 0.0
            applicability_status = "active_cash"
            applicability_reason = "个股风险门禁明确挡下全部候选，当日按现金收益 0% 计入。"
        else:
            treatment_return = None
            applicability_status = "not_applicable"
            applicability_reason = "当日没有可验证的风险门禁处理组合。"
    elif role == "policy_diversity":
        slot_audit = _policy_slot_audit(role, treatment, control)
        if slot_audit.get("slotReturnPct") is None:
            treatment_return = None
            control_return = None
            applicability_status = "not_applicable"
            applicability_reason = "当日未形成可验证的第二槽，不能把空组合当作 0% 收益。"
    elif role == "policy_rank3":
        slot_audit = _policy_slot_audit(role, treatment, control)
        if slot_audit.get("slotReturnPct") is None:
            treatment_return = None
            control_return = None
            applicability_status = "not_applicable"
            applicability_reason = "当日没有完整的原始 Rank 3 结果，第三槽实验不适用。"
    elif role == "policy_net_expectation" and not bool((strategy.get("netExpectationModel") or {}).get("rankingReady")):
        treatment_return = None
        control_return = None
        applicability_status = "not_applicable"
        applicability_reason = "净期望模型仍在收集样本，尚未生成可验证的处理排序。"
    elif role == "policy_adaptive_exit" and treatment_count == 0:
        treatment_return = None
        control_return = None
        applicability_status = "not_applicable"
        applicability_reason = "当日没有持仓，动态退出实验没有可执行对象。"

    delta = round(treatment_return - control_return, 4) if treatment_return is not None and control_return is not None else None
    tradeable_count = int(treatment.get("tradeableCount")) if treatment.get("tradeableCount") is not None else treatment_count
    if applicability_status == "active_cash" and delta is not None:
        status = "active_cash"
    elif applicability_status == "not_applicable":
        status = "not_applicable"
    elif delta is not None:
        status = "ok"
    elif _summary_has_technical_failure(treatment) or _summary_has_technical_failure(control):
        status = "technical_failure"
        applicability_reason = "处理组或控制组发生技术失败，不能作为策略主动空仓。"
    else:
        status = "data_insufficient"
        applicability_reason = "处理组或同日控制组的执行收益不完整。"
    execution_status_counts = treatment.get("executionStatusCounts") or {}
    adaptive_unexecutable = sum(
        int(value or 0)
        for key, value in execution_status_counts.items()
        if str(key) != "executed"
    ) if role == "policy_adaptive_exit" else 0
    detail = {
        "treatment": treatment,
        "control": control,
        "applicabilityStatus": applicability_status,
        "applicabilityReason": applicability_reason,
    }
    if role == "policy_risk_gate":
        detail["gateAudit"] = strategy.get("negativeGateAudit") or {}
    elif role == "policy_diversity":
        detail["portfolioDiversityAudit"] = strategy.get("portfolioDiversityAudit") or {}
    elif role == "policy_rank3":
        detail["rank3Audit"] = strategy.get("rank3Audit") or {}
    elif role == "policy_net_expectation":
        detail["netExpectationDiagnostics"] = _net_expectation_daily_diagnostics(run)
    elif role == "policy_adaptive_exit":
        detail["adaptiveExitAudit"] = {
            "version": "adaptive_exit_shadow_v1",
            "executionStatusCounts": treatment.get("executionStatusCounts") or {},
            "branchCounts": treatment.get("branchCounts") or {},
            "componentRows": _component_rows(treatment),
        }
    slot_audit = _policy_slot_audit(role, treatment, control)
    if slot_audit:
        detail["slotAudit"] = slot_audit
    benchmark_return = _number((baseline := market.get("baselineComparison") or {}).get("index", {}).get("avgNetReturnPct"))
    excess_return = None
    if role == "policy_rank3" and slot_audit.get("slotReturnPct") is not None and benchmark_return is not None:
        excess_return = round(float(slot_audit["slotReturnPct"]) - benchmark_return, 4)
    return {
        "recommendationDate": run.get("recommendationDate") or "",
        "targetTradeDate": "",
        "canonicalRunId": int(run.get("id") or 0),
        "shadowFamily": "policy_experiment",
        "shadowRole": role,
        "scope": role.removeprefix("policy_"),
        "strategyPolicyVersion": policy_version,
        "outcomeScenario": DEFAULT_OUTCOME_SCENARIO,
        "executionProfileVersion": str((strategy.get("executionProfileVersion") or "execution_profile_v1")),
        "comparisonKey": comparison_key,
        "controlRole": comparison_key.split("_vs_")[0],
        "portfolioSlotCount": 3,
        "holdingCount": treatment_count,
        "cashSlotCount": max(0, 3 - treatment_count),
        "stockOutcomeCount": tradeable_count,
        "missingCount": max(0, treatment_count - tradeable_count),
        "unexecutableCount": adaptive_unexecutable,
        "coveragePct": round(tradeable_count / treatment_count * 100, 2) if treatment_count else None,
        "treatmentNetReturnPct": treatment_return,
        "controlNetReturnPct": control_return,
        "pairedDailyDeltaPct": delta,
        "benchmarkReturnPct": benchmark_return if role in {"policy_rank3", "policy_net_expectation"} else None,
        "excessReturnPct": excess_return,
        "avoidedLossPct": _avoided_loss_pct(treatment_return, control_return),
        "missedProfitPct": _missed_profit_pct(treatment_return, control_return),
        "status": status,
        "generationStatus": "done",
        "sourceType": "forward_policy_experiment",
        "sourceRevision": _run_source_revision(run),
        "calculationVersion": CALCULATION_VERSION,
        "detail": detail,
    }


def _v3_main_fact(
    run: Dict[str, Any],
    strategy: Dict[str, Any],
    baseline: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the promotion-grade V3 fact from the exact execution replay.

    Daily proxy observations are intentionally excluded here.  Only the existing
    open_15m execution replay stored in baselineComparison can make a day usable.
    """
    treatment = baseline.get("v3Main") or {}
    control = baseline.get("legacyOfficialControl") or baseline.get("final") or {}
    decision_status = str(strategy.get("decisionStatus") or "")
    treatment_return = _complete_portfolio_return(treatment)
    if decision_status == "active_top0":
        treatment_return = 0.0
    elif decision_status in {"technical_failure", ""}:
        treatment_return = None
    control_return = _complete_portfolio_return(control)
    if control_return is None and _official_control_is_active_cash(run):
        control_return = 0.0
    delta = (
        round(float(treatment_return) - float(control_return), 4)
        if treatment_return is not None and control_return is not None
        else None
    )
    holding_count = int(strategy.get("holdingCount") or len(strategy.get("picks") or []))
    tradeable_count = int(treatment.get("tradeableCount") or 0)
    target_date = next(
        (
            str(row.get("nextTradeDate") or row.get("targetTradeDate") or "")
            for row in _component_rows(treatment)
            if isinstance(row, dict)
        ),
        "",
    )
    return {
        "recommendationDate": run.get("recommendationDate") or "",
        "targetTradeDate": target_date,
        "canonicalRunId": int(run.get("id") or 0),
        "shadowFamily": "v3_main_experiment",
        "shadowRole": "v3_main",
        "scope": "main_strategy",
        "strategyPolicyVersion": str(strategy.get("pipelineVersion") or T1_V3_PIPELINE_VERSION),
        "outcomeScenario": DEFAULT_OUTCOME_SCENARIO,
        "executionProfileVersion": str(
            strategy.get("executionProfileVersion")
            or ((run.get("market") or {}).get("executionProfile") or {}).get("version")
            or "execution_profile_v1"
        ),
        "comparisonKey": "current_official_vs_v3_main",
        "controlRole": "current_official_same_day",
        "portfolioSlotCount": int(strategy.get("slotLimit") or 2),
        "holdingCount": holding_count,
        "cashSlotCount": int(strategy.get("cashSlotCount") if strategy.get("cashSlotCount") is not None else max(0, 2 - holding_count)),
        "stockOutcomeCount": tradeable_count,
        "missingCount": max(0, holding_count - tradeable_count),
        "unexecutableCount": int(treatment.get("unexecutableCount") or 0),
        "coveragePct": round(tradeable_count / holding_count * 100, 2) if holding_count else None,
        "treatmentNetReturnPct": treatment_return,
        "controlNetReturnPct": control_return,
        "pairedDailyDeltaPct": delta,
        "avoidedLossPct": _avoided_loss_pct(treatment_return, control_return),
        "missedProfitPct": _missed_profit_pct(treatment_return, control_return),
        "status": (
            "technical_failure" if decision_status == "technical_failure"
            else "active_cash" if decision_status == "active_top0" and delta is not None
            else "not_applicable" if decision_status in {"not_ready", "disabled", "not_applicable"}
            else "ok" if delta is not None
            else "data_insufficient"
        ),
        "generationStatus": "done",
        "sourceType": "forward_v3_main_experiment",
        "sourceRevision": _run_source_revision(run),
        "calculationVersion": CALCULATION_VERSION,
        "detail": {
            "decisionStatus": decision_status,
            "zeroPickReasonCode": strategy.get("zeroPickReasonCode") or "",
            "versionBundle": strategy.get("versionBundle") or {},
            "treatment": treatment,
            "control": control,
            "usesProxyReturn": False,
            "outcomeBusinessMeaning": "下一交易日开盘后15分钟卖出、扣除费用和滑点后的组合净收益",
        },
    }


def _v3_admission_blockers(
    metrics: Dict[str, Any],
    rows: List[Dict[str, Any]],
    strategy_version: str,
) -> List[str]:
    blockers: List[str] = []
    if strategy_version != T1_V3_PIPELINE_VERSION:
        blockers.append("冻结报告的 V3 主版本与当前待晋级版本不一致。")
    if int(metrics.get("effectiveDays") or 0) < 120:
        blockers.append("有效交易日少于 120 日。")
    if int(metrics.get("stockOutcomeCount") or 0) < 150:
        blockers.append("有效执行结果少于 150 个。")
    if metrics.get("avgDailyNetReturnPct") is None or float(metrics["avgDailyNetReturnPct"]) <= 0:
        blockers.append("V3 的 open_15m 平均扣费净收益尚未为正。")
    if metrics.get("avgPairedDeltaPct") is None or float(metrics["avgPairedDeltaPct"]) <= 0:
        blockers.append("V3 相对同日当前正式策略的平均配对差尚未为正。")
    if metrics.get("medianDailyNetReturnPct") is None or float(metrics["medianDailyNetReturnPct"]) <= 0:
        blockers.append("V3 收益中位数尚未为正，改善可能依赖少数极端盈利日。")
    treatment_drawdown = metrics.get("maxDrawdownPct")
    control_drawdown = metrics.get("controlMaxDrawdownPct")
    if treatment_drawdown is None or control_drawdown is None:
        blockers.append("V3 或同日正式策略的最大回撤口径不完整。")
    elif float(treatment_drawdown) < float(control_drawdown):
        blockers.append("V3 最大回撤差于同日当前正式策略。")
    effective_days = max(1, int(metrics.get("effectiveDays") or 0))
    if int(metrics.get("unexecutableCount") or 0) / effective_days > 0.05:
        blockers.append("V3 不可执行结果比例高于 5%。")
    technical_failures = sum(1 for row in rows if str(row.get("status") or "") == "technical_failure")
    if technical_failures / max(1, len(rows)) > 0.05:
        blockers.append("V3 技术失败日比例高于 5%。")
    confirmation = [row for row in rows if _fact_is_effective(row)][-60:]
    confirmation_returns = [float(row["treatmentNetReturnPct"]) for row in confirmation]
    confirmation_deltas = [float(row["pairedDailyDeltaPct"]) for row in confirmation if row.get("pairedDailyDeltaPct") is not None]
    if len(confirmation) < 60:
        blockers.append("参数冻结后的确认窗口少于 60 个有效交易日。")
    elif mean(confirmation_returns) <= 0 or not confirmation_deltas or mean(confirmation_deltas) <= 0:
        blockers.append("第 61～120 个有效交易日的独立确认窗口未同时取得正收益和正配对差。")
    if any(bool(((row.get("detail") or {}).get("usesProxyReturn"))) for row in rows):
        blockers.append("报告混入了代理收益，不能用于正式晋级。")
    return blockers


def _net_expectation_daily_diagnostics(run: Dict[str, Any]) -> Dict[str, Any]:
    market = run.get("market") or {}
    strategy = market.get("strategyPolicy") or {}
    baseline = market.get("baselineComparison") or {}
    picks = {
        str(item.get("code") or "").zfill(6): item
        for item in (market.get("baselineGroups") or {}).get("executionCalibration") or []
    }
    weighted = []
    for row in (baseline.get("executionCalibration") or {}).get("componentRows") or []:
        code = str(row.get("code") or "").zfill(6)
        pick = picks.get(code) or {}
        value = _number(row.get("netReturnPct"))
        probability = _number(pick.get("populationInclusionProbability"))
        if value is None or probability is None or probability <= 0 or pick.get("populationEstimationEligible") is not True:
            continue
        weighted.append((value, min(20.0, 1.0 / probability)))
    weight_sum = sum(weight for _, weight in weighted)
    eligible_pool_return = sum(value * weight for value, weight in weighted) / weight_sum if weight_sum else None
    model = strategy.get("netExpectationModel") or {}
    calibration = strategy.get("executionCalibrationAudit") or {}
    return {
        "modelVersion": strategy.get("netExpectationModelVersion") or model.get("version") or "",
        "modelStatus": model.get("status") or "collecting",
        "rankingReady": bool(model.get("rankingReady")),
        "trainingSampleCount": int(model.get("sampleCount") or 0),
        "trainingTradeDays": int(model.get("independentTradeDays") or 0),
        "readyBinCount": int(model.get("readyBinCount") or 0),
        "binCount": int(model.get("binCount") or 0),
        "minimumTotalSamples": int(model.get("minimumTotalSamples") or 150),
        "minimumBinSamples": int(model.get("minimumBinSamples") or 30),
        "calibrationSelectedCount": int(calibration.get("selectedCount") or 0),
        "calibrationCertaintyCount": int(calibration.get("certaintyCount") or 0),
        "calibrationCoveredStrata": int(calibration.get("coveredStratumCount") or 0),
        "calibrationTotalStrata": int(calibration.get("nonEmptyStratumCount") or 0),
        "calibrationCoverageStatus": calibration.get("coverageStatus") or "unknown",
        "eligiblePoolWeightedReturnPct": round(eligible_pool_return, 4) if eligible_pool_return is not None else None,
        "randomBaselineReturnPct": _number((baseline.get("random") or {}).get("portfolioReturnPct")),
        "currentRuleReturnPct": _number((baseline.get("netExpectationControl") or {}).get("portfolioReturnPct")),
        "etfReturnPct": _number((baseline.get("index") or {}).get("avgNetReturnPct")),
    }


def _portfolio_return(summary: Dict[str, Any]) -> Optional[float]:
    if not summary:
        return None
    value = summary.get("portfolioReturnPct")
    if value is None:
        value = (summary.get("portfolio") or {}).get("portfolioReturnPct")
    return _number(value)


def _summary_holding_count(summary: Dict[str, Any]) -> int:
    if not summary:
        return 0
    return int(summary.get("count") or summary.get("componentCount") or 0)


def _summary_has_technical_failure(summary: Dict[str, Any]) -> bool:
    status = str((summary or {}).get("status") or "").lower()
    return status in {"technical_failure", "failed", "error", "ai_failed"}


def _complete_portfolio_return(summary: Dict[str, Any]) -> Optional[float]:
    if not summary:
        return None
    holding_count = _summary_holding_count(summary)
    tradeable_count = int(summary.get("tradeableCount")) if summary.get("tradeableCount") is not None else holding_count
    status = str(summary.get("status") or "").lower()
    if holding_count <= 0 or holding_count > tradeable_count:
        return None
    if status in {"data_insufficient", "not_applicable", "technical_failure", "failed", "error"}:
        return None
    return _portfolio_return(summary)


def _official_control_is_active_cash(run: Dict[str, Any]) -> bool:
    active_cash_modes = {
        "market_risk_zero_pick",
        "data_quality_zero_pick",
        "guardrail_zero_pick",
        "ai_zero_pick",
        "v3_active_top0",
    }
    return (
        int(run.get("finalCount") or 0) <= 0
        and str(run.get("selectionMode") or "") in active_cash_modes
    )


def _avoided_loss_pct(treatment_return: Optional[float], control_return: Optional[float]) -> Optional[float]:
    if treatment_return is None or control_return is None:
        return None
    delta = float(treatment_return) - float(control_return)
    return round(max(delta, 0.0), 4) if float(control_return) < 0 and delta > 0 else 0.0


def _missed_profit_pct(treatment_return: Optional[float], control_return: Optional[float]) -> Optional[float]:
    if treatment_return is None or control_return is None:
        return None
    delta = float(treatment_return) - float(control_return)
    return round(max(-delta, 0.0), 4) if float(control_return) > 0 and delta < 0 else 0.0


def _component_rows(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = summary.get("componentRows")
    if not isinstance(rows, list):
        rows = (summary.get("portfolio") or {}).get("componentRows")
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def _policy_slot_audit(role: str, treatment: Dict[str, Any], control: Dict[str, Any]) -> Dict[str, Any]:
    treatment_rows = _component_rows(treatment)
    control_rows = _component_rows(control)
    if role == "policy_diversity":
        first_return = _number((treatment_rows[0] if len(treatment_rows) > 0 else {}).get("netReturnPct"))
        second_return = _number((treatment_rows[1] if len(treatment_rows) > 1 else {}).get("netReturnPct"))
        return {
            "slot": 2,
            "slotCode": str((treatment_rows[1] if len(treatment_rows) > 1 else {}).get("code") or ""),
            "slotReturnPct": second_return,
            "slotIncrementPct": round(second_return / 3, 4) if second_return is not None else None,
            "firstSlotReturnPct": first_return,
            "sameDayFailure": first_return < 0 and second_return < 0 if first_return is not None and second_return is not None else None,
            "controlSecondSlotCode": str((control_rows[1] if len(control_rows) > 1 else {}).get("code") or ""),
        }
    if role == "policy_rank3":
        third_return = _number((treatment_rows[2] if len(treatment_rows) > 2 else {}).get("netReturnPct"))
        base_returns = [_number(row.get("netReturnPct")) for row in control_rows[:2]]
        base_complete = len(base_returns) == 2 and all(value is not None for value in base_returns)
        base_return = round(mean([float(value) for value in base_returns]), 4) if base_complete else None
        return {
            "slot": 3,
            "slotCode": str((treatment_rows[2] if len(treatment_rows) > 2 else {}).get("code") or ""),
            "slotReturnPct": third_return,
            "slotIncrementPct": round(third_return / 3, 4) if third_return is not None else None,
            "basePortfolioReturnPct": base_return,
            "sameDayFailure": bool(third_return < 0 and all(float(value) < 0 for value in base_returns)) if third_return is not None and base_complete else None,
        }
    return {}


def _aggregate(role: str, version: str, facts: List[Dict[str, Any]], family: str) -> Dict[str, Any]:
    paired_facts = [
        fact for fact in facts
        if _fact_is_effective(fact)
        and fact.get("treatmentNetReturnPct") is not None
        and fact.get("controlNetReturnPct") is not None
        and fact.get("pairedDailyDeltaPct") is not None
    ]
    is_comparative = family in {"policy_experiment", "v3_main_experiment"}
    effective_facts = paired_facts if is_comparative else [fact for fact in facts if _fact_is_effective(fact)]
    returns = [float(fact["treatmentNetReturnPct"]) for fact in effective_facts]
    # Comparative treatment and control curves must contain exactly the same
    # dates.  An independently complete control day cannot be used when the
    # treatment result is missing.
    control_returns = [float(fact["controlNetReturnPct"]) for fact in paired_facts]
    paired = [float(fact["pairedDailyDeltaPct"]) for fact in paired_facts]
    holding_facts = [fact for fact in effective_facts if int(fact.get("holdingCount") or 0) > 0]
    holding_returns = [float(fact["treatmentNetReturnPct"]) for fact in holding_facts]
    positives = [value for value in returns if value > 0]
    negatives = [value for value in returns if value < 0]
    positive_holding_returns = [value for value in holding_returns if value > 0]
    cumulative = _compound(returns)
    nav, max_drawdown = _nav_and_drawdown(returns)
    control_nav, control_max_drawdown = _nav_and_drawdown(control_returns)
    effective_days = len(returns)
    hit_days = len(holding_facts)
    # Coverage must include incomplete holding days.  Filtering to effective
    # returns first made a day with missing outcomes disappear from its own
    # denominator and could display 100% coverage incorrectly.
    coverage_facts = [
        fact for fact in facts
        if int(fact.get("holdingCount") or 0) > 0
        and str(fact.get("status") or "") != "not_applicable"
    ]
    stock_outcome_count = sum(int(fact.get("stockOutcomeCount") or 0) for fact in coverage_facts)
    missing_count = sum(int(fact.get("missingCount") or 0) for fact in coverage_facts)
    coverage_denominator = stock_outcome_count + missing_count
    coverage = round(stock_outcome_count / coverage_denominator * 100, 2) if coverage_denominator else None
    review_status = _review_status(effective_days, coverage, family)
    label, comparison_label = ROLE_META.get(role, (role, ""))
    gate_diagnostics = _aggregate_gate_diagnostics(facts) if role == "policy_risk_gate" else []
    diversity_diagnostics = _aggregate_diversity_diagnostics(facts) if role == "policy_diversity" else {}
    rank3_diagnostics = _aggregate_rank3_diagnostics(facts) if role == "policy_rank3" else {}
    net_expectation_diagnostics = _aggregate_net_expectation_diagnostics(facts) if role == "policy_net_expectation" else {}
    adaptive_exit_diagnostics = _aggregate_adaptive_exit_diagnostics(facts) if role == "policy_adaptive_exit" else {}
    excess_values = [float(fact["excessReturnPct"]) for fact in facts if fact.get("excessReturnPct") is not None]
    return {
        "shadowRole": role,
        "roleLabel": label,
        "comparisonLabel": comparison_label,
        "strategyPolicyVersion": version,
        "calculationVersion": CALCULATION_VERSION,
        "targetTradeDays": len({str(fact.get("recommendationDate") or "") for fact in facts if fact.get("recommendationDate")}),
        "targetAttemptCount": len(facts),
        "effectiveDays": effective_days,
        "hitDays": hit_days,
        "holdingDays": hit_days,
        "stockOutcomeCount": stock_outcome_count,
        "missingCount": missing_count,
        "unexecutableCount": sum(int(fact.get("unexecutableCount") or 0) for fact in effective_facts),
        "coveragePct": coverage,
        "coverageRequiredCount": coverage_denominator,
        "coverageIncomplete": bool(coverage_denominator and (coverage is None or coverage < 95)),
        "avgDailyNetReturnPct": round(mean(returns), 4) if returns else None,
        "medianDailyNetReturnPct": round(median(returns), 4) if returns else None,
        "positiveHoldingDayRate": round(len(positive_holding_returns) / len(holding_returns) * 100, 2) if holding_returns else None,
        "positiveEffectiveDayRate": round(len(positives) / len(returns) * 100, 2) if returns else None,
        "avgGainPct": round(mean(positives), 4) if positives else None,
        "avgLossPct": round(mean(negatives), 4) if negatives else None,
        "profitLossRatio": round(mean(positives) / abs(mean(negatives)), 4) if positives and negatives else None,
        "profitFactor": round(sum(positives) / abs(sum(negatives)), 4) if positives and negatives else None,
        "cumulativeReturnPct": cumulative,
        "maxDrawdownPct": max_drawdown,
        "controlAvgDailyNetReturnPct": round(mean(control_returns), 4) if control_returns else None,
        "controlCumulativeReturnPct": _compound(control_returns),
        "controlMaxDrawdownPct": control_max_drawdown,
        "worst10Pct": _worst_decile(returns),
        "worstDailyReturnPct": round(min(returns), 4) if returns else None,
        "longestLosingStreak": _longest_losing_streak(returns),
        "avgPairedDeltaPct": round(mean(paired), 4) if paired else None,
        "medianPairedDeltaPct": round(median(paired), 4) if paired else None,
        "positivePairedDayRate": round(len([value for value in paired if value > 0]) / len(paired) * 100, 2) if paired else None,
        "cumulativePairedDeltaPct": round(sum(paired), 4) if paired else None,
        "cumulativePositivePairedDeltaPct": round(sum(value for value in paired if value > 0), 4) if paired else None,
        "cumulativeNegativePairedDeltaPct": round(sum(value for value in paired if value < 0), 4) if paired else None,
        "avgExcessReturnPct": round(mean(excess_values), 4) if excess_values else None,
        "avoidedLossPct": round(sum(float(fact.get("avoidedLossPct") or 0) for fact in facts), 4) if paired else None,
        "missedProfitPct": round(sum(float(fact.get("missedProfitPct") or 0) for fact in facts), 4) if paired else None,
        "nav": nav,
        "controlNav": control_nav,
        "reviewStatus": review_status,
        "reviewStatusLabel": _review_status_label(review_status),
        "progress": _review_progress(effective_days, family),
        "firstUsableDate": min((fact.get("recommendationDate") for fact in effective_facts), default=""),
        "lastUsableDate": max((fact.get("recommendationDate") for fact in effective_facts), default=""),
        "activeCashDays": len({fact.get("recommendationDate") for fact in effective_facts if str(fact.get("status") or "") == "active_cash"}),
        "notApplicableDays": len({fact.get("recommendationDate") for fact in facts if str(fact.get("status") or "") == "not_applicable"}),
        "dataInsufficientDays": len({fact.get("recommendationDate") for fact in facts if str(fact.get("status") or "") == "data_insufficient"}),
        "technicalFailureDays": len({fact.get("recommendationDate") for fact in facts if str(fact.get("status") or "") == "technical_failure"}),
        "technicalFailureAttempts": sum(1 for fact in facts if str(fact.get("status") or "") == "technical_failure"),
        "gateDiagnostics": gate_diagnostics,
        "gateHitCount": sum(int(item.get("hitCount") or 0) for item in gate_diagnostics),
        "gateBlockedCandidateCount": sum(
            int(((fact.get("detail") or {}).get("gateAudit") or {}).get("blockedCandidateCount") or 0)
            for fact in facts
        ),
        "diversityDiagnostics": diversity_diagnostics,
        "rank3Diagnostics": rank3_diagnostics,
        "netExpectationDiagnostics": net_expectation_diagnostics,
        "adaptiveExitDiagnostics": adaptive_exit_diagnostics,
    }


def _aggregate_gate_diagnostics(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    order = ("sector", "tail_support", "overnight_risk", "chase_risk", "event_risk")
    result = []
    for gate_key in order:
        rows = [
            gate
            for fact in facts
            for key, gate in ((((fact.get("detail") or {}).get("gateAudit") or {}).get("gates") or {}).items())
            if key == gate_key and isinstance(gate, dict)
        ]
        if not rows:
            continue
        evaluated = sum(int(row.get("evaluatedCount") or 0) for row in rows)
        available = sum(int(row.get("evidenceAvailableCount") or 0) for row in rows)
        result.append(
            {
                "gateKey": gate_key,
                "label": str(rows[-1].get("label") or gate_key),
                "configuredMode": str(rows[-1].get("configuredMode") or "off"),
                "runCount": len(rows),
                "candidateCount": sum(int(row.get("candidateCount") or 0) for row in rows),
                "evaluatedCount": evaluated,
                "hitCount": sum(int(row.get("hitCount") or 0) for row in rows),
                "passCount": sum(int(row.get("passCount") or 0) for row in rows),
                "unavailableCount": sum(int(row.get("unavailableCount") or 0) for row in rows),
                "evidenceCoveragePct": round(available / evaluated * 100, 2) if evaluated else None,
            }
        )
    return result


def _aggregate_diversity_diagnostics(facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    audits = [((fact.get("detail") or {}).get("portfolioDiversityAudit") or {}) for fact in facts]
    slot_audits = [((fact.get("detail") or {}).get("slotAudit") or {}) for fact in facts]
    increments = [_number(item.get("slotIncrementPct")) for item in slot_audits]
    increments = [float(value) for value in increments if value is not None]
    failures = [item.get("sameDayFailure") for item in slot_audits if item.get("sameDayFailure") is not None]
    pairs = [
        (_number(item.get("firstSlotReturnPct")), _number(item.get("slotReturnPct")))
        for item in slot_audits
    ]
    pairs = [(float(first), float(second)) for first, second in pairs if first is not None and second is not None]
    conflicts = {
        key: sum(int((audit.get("conflictCounts") or {}).get(key) or 0) for audit in audits)
        for key in ("sameIndustry", "sameDriverGroup", "industryIdentityUnavailable", "driverIdentityUnavailable")
    }
    return {
        "replacementCount": sum(int(audit.get("replacementCount") or 0) for audit in audits),
        "blockedCandidateCount": sum(int(audit.get("blockedCandidateCount") or 0) for audit in audits),
        "secondSlotOpenDays": sum(1 for audit in audits if audit.get("secondSlotOpen") is True),
        "secondSlotEffectiveDays": len(increments),
        "avgSecondSlotIncrementPct": round(mean(increments), 4) if increments else None,
        "medianSecondSlotIncrementPct": round(median(increments), 4) if increments else None,
        "positiveSecondSlotRate": round(len([value for value in increments if value > 0]) / len(increments) * 100, 2) if increments else None,
        "worstSecondSlotIncrementPct": round(min(increments), 4) if increments else None,
        "sameDayFailureRate": round(len([value for value in failures if value]) / len(failures) * 100, 2) if failures else None,
        "sameDayFailureSampleDays": len(failures),
        "portfolioReturnCorrelation": _correlation(pairs),
        "correlationSampleDays": len(pairs),
        "conflictCounts": conflicts,
    }


def _aggregate_rank3_diagnostics(facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    slot_audits = [((fact.get("detail") or {}).get("slotAudit") or {}) for fact in facts]
    increments = [_number(item.get("slotIncrementPct")) for item in slot_audits]
    increments = [float(value) for value in increments if value is not None]
    slot_returns = [_number(item.get("slotReturnPct")) for item in slot_audits]
    slot_returns = [float(value) for value in slot_returns if value is not None]
    failures = [item.get("sameDayFailure") for item in slot_audits if item.get("sameDayFailure") is not None]
    pairs = [
        (_number(item.get("basePortfolioReturnPct")), _number(item.get("slotReturnPct")))
        for item in slot_audits
    ]
    pairs = [(float(base), float(third)) for base, third in pairs if base is not None and third is not None]
    excess = [float(fact["excessReturnPct"]) for fact in facts if fact.get("excessReturnPct") is not None]
    return {
        "effectiveDays": len(increments),
        "avgIncrementPct": round(mean(increments), 4) if increments else None,
        "medianIncrementPct": round(median(increments), 4) if increments else None,
        "positiveIncrementRate": round(len([value for value in increments if value > 0]) / len(increments) * 100, 2) if increments else None,
        "worstIncrementPct": round(min(increments), 4) if increments else None,
        "avgSlotReturnPct": round(mean(slot_returns), 4) if slot_returns else None,
        "avgEtfExcessPct": round(mean(excess), 4) if excess else None,
        "sameDayFailureRate": round(len([value for value in failures if value]) / len(failures) * 100, 2) if failures else None,
        "sameDayFailureSampleDays": len(failures),
        "baseReturnCorrelation": _correlation(pairs),
        "correlationSampleDays": len(pairs),
    }


def _aggregate_net_expectation_diagnostics(facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [((fact.get("detail") or {}).get("netExpectationDiagnostics") or {}) for fact in facts]
    latest = rows[-1] if rows else {}

    def values(key: str) -> List[float]:
        return [float(value) for row in rows if (value := _number(row.get(key))) is not None]

    pool = values("eligiblePoolWeightedReturnPct")
    random_values = values("randomBaselineReturnPct")
    rule = values("currentRuleReturnPct")
    etf = values("etfReturnPct")
    selected = sum(int(row.get("calibrationSelectedCount") or 0) for row in rows)
    certainty = sum(int(row.get("calibrationCertaintyCount") or 0) for row in rows)
    return {
        "modelVersion": latest.get("modelVersion") or "",
        "modelStatus": latest.get("modelStatus") or "collecting",
        "rankingReady": bool(latest.get("rankingReady")),
        "trainingSampleCount": int(latest.get("trainingSampleCount") or 0),
        "trainingTradeDays": int(latest.get("trainingTradeDays") or 0),
        "readyBinCount": int(latest.get("readyBinCount") or 0),
        "binCount": int(latest.get("binCount") or 0),
        "minimumTotalSamples": int(latest.get("minimumTotalSamples") or 150),
        "minimumBinSamples": int(latest.get("minimumBinSamples") or 30),
        "calibrationSelectedCount": selected,
        "calibrationProbabilityCount": max(0, selected - certainty),
        "budgetLimitedDays": sum(1 for row in rows if row.get("calibrationCoverageStatus") == "budget_limited"),
        "avgEligiblePoolWeightedReturnPct": round(mean(pool), 4) if pool else None,
        "avgRandomBaselineReturnPct": round(mean(random_values), 4) if random_values else None,
        "avgCurrentRuleReturnPct": round(mean(rule), 4) if rule else None,
        "avgEtfReturnPct": round(mean(etf), 4) if etf else None,
    }


def _aggregate_adaptive_exit_diagnostics(facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    audits = [((fact.get("detail") or {}).get("adaptiveExitAudit") or {}) for fact in facts]
    statuses: Dict[str, int] = {}
    branches: Dict[str, int] = {}
    mark_returns = []
    for audit in audits:
        for key, value in (audit.get("executionStatusCounts") or {}).items():
            statuses[str(key)] = statuses.get(str(key), 0) + int(value or 0)
        for key, value in (audit.get("branchCounts") or {}).items():
            branches[str(key)] = branches.get(str(key), 0) + int(value or 0)
        for row in audit.get("componentRows") or []:
            value = _number(row.get("markToMarketReturnPct"))
            if value is not None:
                mark_returns.append(float(value))
    total = sum(statuses.values())
    executed = int(statuses.get("executed") or 0)
    incomplete = total - executed
    return {
        "version": next((str(audit.get("version") or "") for audit in reversed(audits) if audit), "adaptive_exit_shadow_v1"),
        "executionStatusCounts": statuses,
        "branchCounts": branches,
        "holdingOutcomeCount": total,
        "completeExecutionCount": executed,
        "incompleteExecutionCount": incomplete,
        "completeExecutionRate": round(executed / total * 100, 2) if total else None,
        "avgMarkToMarketReturnPct": round(mean(mark_returns), 4) if mark_returns else None,
    }


def _correlation(pairs: List[tuple[float, float]], minimum_samples: int = 5) -> Optional[float]:
    if len(pairs) < minimum_samples:
        return None
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    x_mean = mean(xs)
    y_mean = mean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    x_sum = sum((x - x_mean) ** 2 for x in xs)
    y_sum = sum((y - y_mean) ** 2 for y in ys)
    denominator = math.sqrt(x_sum * y_sum)
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _select_window(facts: List[Dict[str, Any]], window: str) -> List[Dict[str, Any]]:
    normalized = str(window or "all").lower()
    ordered = sorted(
        facts,
        key=lambda fact: (str(fact.get("recommendationDate") or ""), int(fact.get("canonicalRunId") or 0)),
    )
    effective_indexes = [
        index for index, fact in enumerate(ordered)
        if _fact_is_effective(fact)
    ]
    if normalized in {"20d", "recent20"}:
        if len(effective_indexes) <= 20:
            return ordered
        return ordered[effective_indexes[-20] :]
    if normalized in {"discovery60", "60d"}:
        if len(effective_indexes) < 60:
            return ordered
        return ordered[: effective_indexes[59] + 1]
    if normalized in {"confirmation60", "61-120"}:
        if len(effective_indexes) <= 60:
            return []
        start = effective_indexes[59] + 1
        end = effective_indexes[119] + 1 if len(effective_indexes) >= 120 else len(ordered)
        return ordered[start:end]
    return ordered


def _fact_is_effective(fact: Dict[str, Any]) -> bool:
    if str(fact.get("status") or "") not in {"ok", "active_cash"}:
        return False
    if fact.get("treatmentNetReturnPct") is None:
        return False
    if str(fact.get("shadowFamily") or "") in {"policy_experiment", "v3_main_experiment"}:
        return fact.get("controlNetReturnPct") is not None and fact.get("pairedDailyDeltaPct") is not None
    return True


def _review_status(effective_days: int, coverage: Optional[float], family: str) -> str:
    if family in {"legacy_tail_rule", "legacy_tail_ai", "policy_experiment"}:
        return "diagnostic_only"
    if coverage is not None and coverage < 95:
        return "data_insufficient"
    if effective_days < 20:
        return "collecting"
    if effective_days < 60:
        return "engineering_review_due"
    if effective_days < 120:
        return "discovery_review_due" if effective_days == 60 else "confirmation_collecting"
    return "promotion_reviewable"


def _review_progress(days: int, family: str = "") -> Dict[str, Any]:
    if family in {"legacy_tail_rule", "legacy_tail_ai", "policy_experiment"}:
        return {
            "effectiveDays": days,
            "nextTargetDays": days,
            "remainingDays": 0,
            "collecting": False,
            "reason": "历史诊断策略已停止新增，不进入自动晋级。",
        }
    next_target = 20 if days < 20 else 60 if days < 60 else 120 if days < 120 else 120
    return {"effectiveDays": days, "nextTargetDays": next_target, "remainingDays": max(0, next_target - days), "collecting": True}


def _review_status_label(status: str) -> str:
    return {
        "collecting": "收集中",
        "engineering_review_due": "待工程检查",
        "discovery_review_due": "待发现期评审",
        "confirmation_collecting": "确认期收集中",
        "promotion_reviewable": "可提交晋级评审",
        "data_insufficient": "数据不足",
        "diagnostic_only": "角色诊断，不可直接晋级",
    }.get(status, status)


def _compound(values: List[float]) -> Optional[float]:
    if not values:
        return None
    nav = 1.0
    for value in values:
        nav *= 1 + value / 100
    return round((nav - 1) * 100, 4)


def _nav_and_drawdown(values: List[float]) -> tuple[List[float], Optional[float]]:
    nav = 1.0
    peak = 1.0
    drawdown = 0.0
    series = []
    for value in values:
        nav *= 1 + value / 100
        peak = max(peak, nav)
        drawdown = min(drawdown, nav / peak - 1)
        series.append(round(nav, 6))
    return series, round(drawdown * 100, 4) if values else None


def _worst_decile(values: List[float]) -> Optional[float]:
    if not values:
        return None
    count = max(1, math.ceil(len(values) * 0.1))
    return round(mean(sorted(values)[:count]), 4)


def _longest_losing_streak(values: List[float]) -> int:
    longest = current = 0
    for value in values:
        current = current + 1 if value < 0 else 0
        longest = max(longest, current)
    return longest


def _source_watermark(facts: List[Dict[str, Any]]) -> str:
    values = sorted(
        f"{int(fact.get('canonicalRunId') or 0)}:{fact.get('sourceRevision') or fact.get('updatedAt') or ''}"
        for fact in facts
    )
    if not values:
        return ""
    digest = hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _report_source_watermark(facts: List[Dict[str, Any]]) -> str:
    payload = f"{CALCULATION_VERSION}|{_source_watermark(facts)}"
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _run_source_revision(run: Dict[str, Any]) -> str:
    values = [
        str(run.get("updatedAt") or ""),
        str(run.get("latestVerifiedAt") or ""),
        str(run.get("finishedAt") or ""),
    ]
    return max((value for value in values if value), default=f"run:{int(run.get('id') or 0)}")


def _normalize_worker_config(value: Dict[str, Any]) -> Dict[str, Any]:
    raw = {**SHADOW_ANALYTICS_CONFIG, **dict(value or {})}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "pollIntervalSeconds": max(15, min(3600, int(raw.get("pollIntervalSeconds") or 60))),
        "leaseSeconds": max(30, min(900, int(raw.get("leaseSeconds") or 120))),
        "retryBaseSeconds": max(1, min(3600, int(raw.get("retryBaseSeconds") or 30))),
        "maxAttempts": max(1, min(20, int(raw.get("maxAttempts") or 5))),
        "lookbackRunLimit": max(100, min(10000, int(raw.get("lookbackRunLimit") or 5000))),
        "batchSize": max(1, min(500, int(raw.get("batchSize") or 50))),
    }


def _public_fact(fact: Dict[str, Any]) -> Dict[str, Any]:
    result = {key: value for key, value in fact.items() if key != "detail"}
    detail = fact.get("detail") or {}
    slot_audit = detail.get("slotAudit") or {}
    if slot_audit:
        result["slotAudit"] = slot_audit
    if detail.get("statusReason") or detail.get("applicabilityReason"):
        result["statusReason"] = detail.get("statusReason") or detail.get("applicabilityReason")
    return result


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
