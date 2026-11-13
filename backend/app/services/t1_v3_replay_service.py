from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from backend.app.engines import t1_v3_pipeline


REPLAY_VERSION = "2026-11-06.t1_v3_history_replay.v1"


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _run_role(run: Dict[str, Any]) -> str:
    market = run.get("market") or {}
    request = run.get("request") or {}
    return str(market.get("runRole") or request.get("runRole") or "official")


def _group_metric(market: Dict[str, Any], role: str) -> Optional[float]:
    group = ((market.get("baselineComparison") or {}).get(role) or {})
    return _number(group.get("avgNetReturnPct"))


class T1V3ReplayService:
    """Build immutable research reports without mutating any historical run or outcome."""

    def __init__(self, database: Any):
        self.database = database

    def latest(self) -> Dict[str, Any]:
        report = self.database.latest_t1_v3_replay_report()
        return report or {
            "status": "not_run",
            "message": "尚未生成 V3 历史重播报告。",
            "admissionEligible": False,
            "modifiesHistoricalRuns": False,
        }

    def run(self, date_from: str = "", date_to: str = "") -> Dict[str, Any]:
        period_end = str(date_to or date.today().isoformat())[:10]
        period_start = str(date_from or f"{period_end[:4]}-10-01")[:10]
        if period_start > period_end:
            raise ValueError("开始日期不能晚于结束日期")

        runs = self._canonical_runs(period_start, period_end)
        before_hash = self._historical_rows_hash(runs)
        details = [self._replay_run(run) for run in runs]
        after_hash = self._historical_rows_hash(runs)
        grade_counts = {
            grade: sum(1 for item in details if item.get("evidenceGrade") == grade)
            for grade in ("A", "B", "C")
        }
        exact_rows = [item for item in details if item.get("exactOutcomeAvailable")]
        treatment = [item.get("v3NetReturnPct") for item in exact_rows if item.get("v3NetReturnPct") is not None]
        control = [item.get("controlNetReturnPct") for item in exact_rows if item.get("controlNetReturnPct") is not None]
        deltas = [item.get("pairedDeltaPct") for item in exact_rows if item.get("pairedDeltaPct") is not None]
        report: Dict[str, Any] = {
            "status": "done",
            "replayVersion": REPLAY_VERSION,
            "targetStrategyVersion": t1_v3_pipeline.PIPELINE_VERSION,
            "periodStart": period_start,
            "periodEnd": period_end,
            "runCount": len(details),
            "gradeCounts": grade_counts,
            "exactOutcomeCount": len(exact_rows),
            "metrics": {
                "exactV3AverageNetReturnPct": self._average(treatment),
                "exactControlAverageNetReturnPct": self._average(control),
                "exactAveragePairedDeltaPct": self._average(deltas),
            },
            "businessMeanings": {
                "evidenceGradeA": "该历史 run 保存了 V3 全候选事实、V3 决策链和精确 open_15m（次日开盘15分钟）成交验证，可做精确复核。",
                "evidenceGradeB": "该历史 run 仅保存旧候选池及部分分钟证据，只能检查覆盖缺口，不能还原 V3 最终选股或计算可用于晋级的收益。",
                "evidenceGradeC": "该历史 run 只有正式推荐或结果数据，无法还原筛选过程。",
                "pairedDeltaPct": "同一交易日 V3 组合净收益减去旧正式策略组合净收益，用于排除大盘同涨同跌影响。",
                "exactV3AverageNetReturnPct": "仅对 A 级且已有精确成交结果的 run，计算 V3 组合平均净收益率。",
            },
            "admissionEligible": False,
            "admissionReason": "历史重播只用于工程核验和发现证据缺口，不进入 V3 正式策略晋级门槛；晋级只认前向影子运行的 120 个有效交易日。",
            "modifiesHistoricalRuns": False,
            "historicalRowsUnchanged": before_hash == after_hash,
            "historicalRowsHashBefore": before_hash,
            "historicalRowsHashAfter": after_hash,
            "details": details,
            "createdAt": datetime.now().isoformat(timespec="seconds"),
        }
        report["reportHash"] = _stable_hash(report)
        report_id = self.database.append_t1_v3_replay_report(report)
        report["id"] = report_id
        return report

    def _canonical_runs(self, date_from: str, date_to: str) -> List[Dict[str, Any]]:
        rows = self.database.list_t1_overnight_selection_runs(limit=5000, status="done")
        selected: Dict[str, Dict[str, Any]] = {}
        for run in rows:
            day = str(run.get("recommendationDate") or "")[:10]
            if not day or day < date_from or day > date_to:
                continue
            if _run_role(run) != "official":
                continue
            if day not in selected or int(run.get("id") or 0) > int(selected[day].get("id") or 0):
                selected[day] = run
        return [selected[key] for key in sorted(selected)]

    def _replay_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        run_id = int(run.get("id") or 0)
        market = run.get("market") or {}
        counts = self._evidence_counts(run_id)
        v3 = market.get("v3Strategy") or {}
        exact_v3 = _group_metric(market, "v3Main")
        control_role = "legacyOfficialControl" if (market.get("baselineGroups") or {}).get("legacyOfficialControl") is not None else "final"
        exact_control = _group_metric(market, control_role)
        has_v3_chain = bool(v3 and counts["v3FactCount"] > 0)
        if has_v3_chain:
            grade = "A"
            replay_status = "exact_outcome_ready" if exact_v3 is not None else "exact_chain_waiting_outcome"
            limitation = "V3 决策链完整；若尚无次日成交结果，则只等待验证，不补写历史数据。"
        elif counts["archiveCount"] > 0 and (counts["minuteCount"] > 0 or counts["validationMinuteCount"] > 0):
            grade = "B"
            replay_status = "evidence_gap_review_only"
            limitation = "旧数据缺少 V3 全市场召回事实或确定性排序输入，不能可信地重算 V3 Top2。"
        else:
            grade = "C"
            replay_status = "result_only_unreplayable"
            limitation = "只保留结果或少量候选信息，无法还原当时可见的全市场证据。"
        paired = exact_v3 - exact_control if exact_v3 is not None and exact_control is not None else None
        return {
            "runId": run_id,
            "recommendationDate": run.get("recommendationDate") or "",
            "evidenceGrade": grade,
            "replayStatus": replay_status,
            "limitation": limitation,
            "v3DecisionStatus": v3.get("decisionStatus") or "not_available",
            "v3NetReturnPct": exact_v3,
            "controlRole": control_role,
            "controlNetReturnPct": exact_control,
            "pairedDeltaPct": paired,
            "exactOutcomeAvailable": exact_v3 is not None,
            "evidenceCounts": counts,
            "affectsOfficialRecommendation": False,
        }

    def _evidence_counts(self, run_id: int) -> Dict[str, int]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT
                  (SELECT COUNT(*) FROM t1_overnight_v3_candidate_facts WHERE run_id=?) AS v3_facts,
                  (SELECT COUNT(*) FROM t1_overnight_candidate_archives WHERE run_id=?) AS archives,
                  (SELECT COUNT(*) FROM t1_overnight_minute_snapshots WHERE run_id=?) AS minutes,
                  (SELECT COUNT(*) FROM t1_overnight_validation_minute_snapshots WHERE run_id=?) AS validation_minutes,
                  (SELECT COUNT(*) FROM t1_overnight_selection_outcomes WHERE run_id=?) AS outcomes
                """,
                (run_id, run_id, run_id, run_id, run_id),
            ).fetchone()
        return {
            "v3FactCount": int(row["v3_facts"] or 0),
            "archiveCount": int(row["archives"] or 0),
            "minuteCount": int(row["minutes"] or 0),
            "validationMinuteCount": int(row["validation_minutes"] or 0),
            "outcomeCount": int(row["outcomes"] or 0),
        }

    def _historical_rows_hash(self, runs: List[Dict[str, Any]]) -> str:
        ids = [int(item.get("id") or 0) for item in runs if int(item.get("id") or 0) > 0]
        if not ids:
            return _stable_hash([])
        placeholders = ",".join("?" for _ in ids)
        with self.database.connect() as connection:
            run_rows = connection.execute(
                f"""SELECT id,status,selection_mode,final_picks_json,request_json,market_json,updated_at
                FROM t1_overnight_selection_runs WHERE id IN ({placeholders}) ORDER BY id""",
                ids,
            ).fetchall()
            outcome_rows = connection.execute(
                f"""SELECT run_id,code,outcome_json,verified_at
                FROM t1_overnight_selection_outcomes WHERE run_id IN ({placeholders}) ORDER BY run_id,code""",
                ids,
            ).fetchall()
        return _stable_hash(
            {
                "runs": [dict(row) for row in run_rows],
                "outcomes": [dict(row) for row in outcome_rows],
            }
        )

    @staticmethod
    def _average(values: List[float]) -> Optional[float]:
        return round(sum(values) / len(values), 6) if values else None
