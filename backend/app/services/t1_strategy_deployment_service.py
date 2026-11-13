"""Auditable user-controlled deployment state for the T+1 V3 strategy."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from backend.app.engines import t1_v3_pipeline as v3_pipeline
from backend.app.database import Database


STRATEGY_DEPLOYMENT_STATE_KEY = "t1_overnight_strategy_deployment_state"
CONFIRMATION_TEXT = "确认启用V3"
OFFLINE_MARKET_CLOSURES = {
    "2026": (
        ("2026-01-01", "2026-01-03"),
        ("2026-02-15", "2026-02-23"),
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
        ("2026-10-01", "2026-10-07"),
    ),
}


class StrategyDeploymentConflictError(RuntimeError):
    pass


class T1StrategyDeploymentService:
    def __init__(self, database: Database, current_official_version: str):
        self.database = database
        self.legacy_version = str(current_official_version)

    def status(self) -> Dict[str, Any]:
        state = self._state()
        report = self._latest_v3_report()
        report_view = self._report_view(report)
        blockers = list(((report or {}).get("admission") or {}).get("blockers") or [])
        if not report:
            blockers = ["尚未生成 V3 的 120 日冻结评审报告。"]
        elif str(report.get("strategyPolicyVersion") or "") != v3_pipeline.PIPELINE_VERSION:
            blockers.append("冻结评审报告版本与当前 V3 目标版本不一致。")
        review_allowed = bool(
            report
            and ((report.get("admission") or {}).get("manualPromotionReviewAllowed"))
            and not blockers
        )
        display_state = dict(state)
        if review_allowed and str(state.get("lifecycleState") or "") == "shadow_collecting":
            display_state["lifecycleState"] = "review_ready"
        return {
            **display_state,
            "stateLabel": _state_label(str(display_state.get("lifecycleState") or "")),
            "targetVersionBundle": _version_bundle(),
            "latestEvaluationReport": report_view,
            "promotionReviewAllowed": review_allowed,
            "promotionBlockers": blockers,
            "confirmationTextRequired": CONFIRMATION_TEXT,
            "nextAvailableTradeDate": _next_trade_date(self.database, date.today()).isoformat(),
            "events": self.database.list_t1_strategy_deployment_events(limit=50),
            "businessMeaning": {
                "currentOfficialVersion": "当前真正产生正式推荐的策略版本",
                "targetStrategyVersion": "准备从影子切换为正式的 V3 固定版本",
                "effectiveFromTradeDate": "批准后开始影响新建 Run 的计划交易日",
                "firstEffectiveRunId": "首个实际使用新正式策略的批次编号",
                "deploymentRevision": "防止重复点击或多个页面相互覆盖的修订号",
            },
        }

    def review(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"continue_observing", "reject"}:
            raise ValueError("评审决定必须是继续观察或拒绝本次晋级。")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise ValueError("请填写继续观察或拒绝的原因。")
        state = self._state()
        if str(state.get("lifecycleState") or "") == "official":
            raise ValueError("V3 已正式生效；如需停止，请使用正式回滚流程。")
        next_state = {
            **state,
            "lifecycleState": "shadow_collecting",
            "reason": reason,
            "lastReviewDecision": decision,
            "lastReviewAt": _now(),
            "nextReviewDate": str(payload.get("nextReviewDate") or ""),
            "deploymentRevision": int(state.get("deploymentRevision") or 0) + 1,
        }
        self._save(state, next_state, decision, payload, reason)
        return self.status()

    def approve(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self._state()
        expected_revision = int(payload.get("deploymentRevision") or 0)
        if expected_revision != int(state.get("deploymentRevision") or 0):
            raise StrategyDeploymentConflictError("部署状态已被其他操作更新，请刷新页面后重新确认。")
        if str(state.get("lifecycleState") or "") in {"scheduled", "official"}:
            raise ValueError("当前已经存在等待生效或已正式生效的 V3 部署。")
        if str(payload.get("confirmationText") or "").strip() != CONFIRMATION_TEXT:
            raise ValueError(f"请输入“{CONFIRMATION_TEXT}”完成最终确认。")
        confirmations = payload.get("confirmations") or {}
        required = ("readFrozenReport", "confirmFixedVersions", "futureRunsOnly", "understandRollback")
        if not all(confirmations.get(key) is True for key in required):
            raise ValueError("请逐项确认冻结报告、固定版本、只影响未来 Run 和回滚目标。")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise ValueError("请填写批准原因。")
        report = self._report_by_id(int(payload.get("evaluationReportId") or 0))
        if not report:
            raise ValueError("未找到指定的冻结评审报告。")
        report_hash = _report_hash(report)
        if str(payload.get("evaluationReportHash") or "") != report_hash:
            raise StrategyDeploymentConflictError("冻结评审报告已变化，请重新查看后再批准。")
        admission = report.get("admission") or {}
        if not admission.get("manualPromotionReviewAllowed") or admission.get("blockers"):
            raise ValueError("冻结评审报告仍有阻塞项，不能批准转正式。")
        if str(report.get("windowType") or "") != "120d" or int(report.get("effectiveDayCount") or 0) < 120:
            raise ValueError("只有满足条件的 120 日冻结报告可以用于晋级。")
        if str(report.get("strategyPolicyVersion") or "") != v3_pipeline.PIPELINE_VERSION:
            raise ValueError("报告策略版本与当前 V3 目标版本不一致。")
        effective_date = str(payload.get("effectiveFromTradeDate") or "")[:10]
        try:
            effective = date.fromisoformat(effective_date)
        except ValueError as error:
            raise ValueError("计划生效交易日格式不正确。") from error
        minimum_effective = _next_trade_date(self.database, date.today())
        if effective < minimum_effective or not _is_trade_date(self.database, effective):
            raise ValueError(f"计划生效日必须是可交易日，且不得早于 {minimum_effective.isoformat()}。")
        next_state = {
            **state,
            "lifecycleState": "scheduled",
            "targetStrategyVersion": v3_pipeline.PIPELINE_VERSION,
            "versionBundle": _version_bundle(),
            "evaluationReportId": int(report["id"]),
            "evaluationReportHash": report_hash,
            "approvedBy": str(payload.get("actor") or "local_user"),
            "approvedAt": _now(),
            "effectiveFromTradeDate": effective_date,
            "firstEffectiveRunId": 0,
            "previousDeploymentId": int(state.get("deploymentId") or 0),
            "fallbackStrategyVersion": self.legacy_version,
            "transitionKind": "promotion",
            "reason": reason,
            "deploymentRevision": int(state.get("deploymentRevision") or 0) + 1,
            "deploymentId": int(state.get("deploymentId") or 0) + 1,
        }
        self._save(state, next_state, "approve_scheduled", payload, reason)
        return self.status()

    def cancel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self._checked_state(payload)
        if str(state.get("lifecycleState") or "") != "scheduled" or int(state.get("firstEffectiveRunId") or 0):
            raise ValueError("只有尚未产生首个生效 Run 的待生效切换可以撤销。")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise ValueError("请填写撤销原因。")
        next_state = {
            **state,
            "lifecycleState": "shadow_collecting",
            "effectiveFromTradeDate": "",
            "approvedAt": "",
            "approvedBy": "",
            "transitionKind": "",
            "reason": reason,
            "deploymentRevision": int(state.get("deploymentRevision") or 0) + 1,
        }
        self._save(state, next_state, "cancel_scheduled", payload, reason)
        return self.status()

    def rollback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self._checked_state(payload)
        if str(state.get("currentOfficialVersion") or "") != v3_pipeline.PIPELINE_VERSION:
            raise ValueError("当前正式策略不是 V3，无需执行 V3 回滚。")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise ValueError("请填写回滚原因。")
        if payload.get("confirmHistoricalRunsUnchanged") is not True:
            raise ValueError("请确认历史 Run 不会被修改。")
        minimum_effective = _next_trade_date(self.database, date.today())
        effective_date = str(payload.get("effectiveFromTradeDate") or minimum_effective.isoformat())[:10]
        try:
            effective = date.fromisoformat(effective_date)
        except ValueError as error:
            raise ValueError("回滚生效日格式不正确。") from error
        if effective < minimum_effective or not _is_trade_date(self.database, effective):
            raise ValueError(f"回滚必须从 {minimum_effective.isoformat()} 或之后的可交易日新 Run 开始生效。")
        next_state = {
            **state,
            "lifecycleState": "scheduled",
            "targetStrategyVersion": self.legacy_version,
            "effectiveFromTradeDate": effective_date,
            "firstEffectiveRunId": 0,
            "approvedBy": str(payload.get("actor") or "local_user"),
            "approvedAt": _now(),
            "fallbackStrategyVersion": self.legacy_version,
            "transitionKind": "rollback",
            "reason": reason,
            "deploymentRevision": int(state.get("deploymentRevision") or 0) + 1,
            "deploymentId": int(state.get("deploymentId") or 0) + 1,
        }
        self._save(state, next_state, "rollback_scheduled", payload, reason)
        return self.status()

    def freeze_for_run(self, recommendation_date: str) -> Dict[str, Any]:
        state = self._state()
        scheduled = (
            str(state.get("lifecycleState") or "") == "scheduled"
            and str(state.get("effectiveFromTradeDate") or "")
            and str(recommendation_date or "") >= str(state.get("effectiveFromTradeDate") or "")
        )
        official_version = str(state.get("targetStrategyVersion") or "") if scheduled else str(state.get("currentOfficialVersion") or "")
        return {
            **state,
            "effectiveForThisRun": scheduled or official_version == v3_pipeline.PIPELINE_VERSION,
            "officialVersionForThisRun": official_version or self.legacy_version,
            "frozenAt": _now(),
        }

    def activate_for_run(self, run_id: int, recommendation_date: str) -> Dict[str, Any]:
        state = self._state()
        if not (
            str(state.get("lifecycleState") or "") == "scheduled"
            and str(recommendation_date or "") >= str(state.get("effectiveFromTradeDate") or "")
        ):
            return state
        target = str(state.get("targetStrategyVersion") or self.legacy_version)
        rolled_back = str(state.get("transitionKind") or "") == "rollback"
        next_state = {
            **state,
            "lifecycleState": "rolled_back" if rolled_back else "official",
            "currentOfficialVersion": target,
            "firstEffectiveRunId": int(run_id),
            "activatedAt": _now(),
            "deploymentRevision": int(state.get("deploymentRevision") or 0) + 1,
        }
        self._save(state, next_state, "rollback_effective" if rolled_back else "promotion_effective", {}, state.get("reason") or "")
        return next_state

    def _state(self) -> Dict[str, Any]:
        raw = dict(self.database.get_app_setting(STRATEGY_DEPLOYMENT_STATE_KEY, {}) or {})
        return {
            "deploymentId": int(raw.get("deploymentId") or 0),
            "lifecycleState": str(raw.get("lifecycleState") or "shadow_collecting"),
            "currentOfficialVersion": str(raw.get("currentOfficialVersion") or self.legacy_version),
            "targetStrategyVersion": str(raw.get("targetStrategyVersion") or v3_pipeline.PIPELINE_VERSION),
            "versionBundle": raw.get("versionBundle") or _version_bundle(),
            "evaluationReportId": int(raw.get("evaluationReportId") or 0),
            "evaluationReportHash": str(raw.get("evaluationReportHash") or ""),
            "approvedBy": str(raw.get("approvedBy") or ""),
            "approvedAt": str(raw.get("approvedAt") or ""),
            "effectiveFromTradeDate": str(raw.get("effectiveFromTradeDate") or ""),
            "firstEffectiveRunId": int(raw.get("firstEffectiveRunId") or 0),
            "previousDeploymentId": int(raw.get("previousDeploymentId") or 0),
            "fallbackStrategyVersion": str(raw.get("fallbackStrategyVersion") or self.legacy_version),
            "deploymentRevision": int(raw.get("deploymentRevision") or 0),
            "transitionKind": str(raw.get("transitionKind") or ""),
            "reason": str(raw.get("reason") or ""),
            **{key: value for key, value in raw.items() if key not in {
                "deploymentId", "lifecycleState", "currentOfficialVersion", "targetStrategyVersion",
                "versionBundle", "evaluationReportId", "evaluationReportHash", "approvedBy", "approvedAt",
                "effectiveFromTradeDate", "firstEffectiveRunId", "previousDeploymentId",
                "fallbackStrategyVersion", "deploymentRevision", "transitionKind", "reason",
            }},
        }

    def _checked_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self._state()
        if int(payload.get("deploymentRevision") or 0) != int(state.get("deploymentRevision") or 0):
            raise StrategyDeploymentConflictError("部署状态已更新，请刷新页面后重试。")
        return state

    def _save(
        self,
        previous: Dict[str, Any],
        next_state: Dict[str, Any],
        event_type: str,
        payload: Dict[str, Any],
        reason: str,
    ) -> None:
        ok = self.database.save_t1_strategy_deployment_transition(
            STRATEGY_DEPLOYMENT_STATE_KEY,
            int(previous.get("deploymentRevision") or 0),
            next_state,
            {
                "eventType": event_type,
                "previousState": previous.get("lifecycleState") or "",
                "nextState": next_state.get("lifecycleState") or "",
                "actor": str(payload.get("actor") or "local_user"),
                "reason": reason,
                "detail": {
                    "evaluationReportId": int(next_state.get("evaluationReportId") or 0),
                    "evaluationReportHash": str(next_state.get("evaluationReportHash") or ""),
                    "versionBundle": next_state.get("versionBundle") or {},
                },
            },
        )
        if not ok:
            raise StrategyDeploymentConflictError("部署状态已被其他操作更新，请刷新页面后重试。")

    def _latest_v3_report(self) -> Optional[Dict[str, Any]]:
        reports = self.database.list_t1_shadow_evaluation_reports(
            shadow_family="v3_main_experiment",
            shadow_role="v3_main",
            strategy_policy_version=v3_pipeline.PIPELINE_VERSION,
            outcome_scenario="open_15m",
            window_type="120d",
        )
        return max(reports, key=lambda item: (int(item.get("reportRevision") or 0), int(item.get("id") or 0)), default=None)

    def _report_by_id(self, report_id: int) -> Optional[Dict[str, Any]]:
        return next(
            (item for item in self.database.list_t1_shadow_evaluation_reports(outcome_scenario="") if int(item.get("id") or 0) == int(report_id)),
            None,
        )

    def _report_view(self, report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not report:
            return None
        return {**report, "reportHash": _report_hash(report), "immutable": True}


def _version_bundle() -> Dict[str, str]:
    return {
        "pipelineVersion": v3_pipeline.PIPELINE_VERSION,
        "recallModelVersion": v3_pipeline.RECALL_MODEL_VERSION,
        "validationAllocationVersion": v3_pipeline.VALIDATION_ALLOCATION_VERSION,
        "structureFeatureVersion": v3_pipeline.STRUCTURE_FEATURE_VERSION,
        "tailGateVersion": v3_pipeline.TAIL_GATE_VERSION,
        "portfolioPolicyVersion": v3_pipeline.PORTFOLIO_POLICY_VERSION,
    }


def _report_hash(report: Dict[str, Any]) -> str:
    payload = {key: value for key, value in report.items() if key not in {"created", "reportHash"}}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _is_trade_date(database: Database, value: date) -> bool:
    if value.weekday() >= 5:
        return False
    calendar = database.get_app_setting("t1_tracking_exchange_calendar", {}) or {}
    start = str(calendar.get("start") or "")[:10]
    end = str(calendar.get("end") or "")[:10]
    text = value.isoformat()
    if start and end and start <= text <= end:
        open_dates = {str(item or "")[:10] for item in calendar.get("openDates") or []}
        return text in open_dates
    for start_text, end_text in OFFLINE_MARKET_CLOSURES.get(str(value.year), ()):
        if start_text <= text <= end_text:
            return False
    return True


def _next_trade_date(database: Database, value: date) -> date:
    candidate = value + timedelta(days=1)
    for _ in range(370):
        if _is_trade_date(database, candidate):
            return candidate
        candidate += timedelta(days=1)
    return _next_weekday(value)


def _state_label(state: str) -> str:
    return {
        "shadow_collecting": "影子验证中",
        "review_ready": "等待人工评审",
        "scheduled": "已批准，等待生效",
        "official": "V3 正式生效",
        "rolled_back": "已退回上一正式策略",
    }.get(state, state)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
