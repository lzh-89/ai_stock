from __future__ import annotations

import http.client
import json
import logging
import math
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.app.database import Database
from backend.app.engines.daily_scoring_engine import score_daily_stock
from backend.app.engines.filter_engine import hard_filter_reason
from backend.app.engines.timing_engine import WINDOWS
from backend.app.providers.sector_provider import get_sector_provider
from backend.app.services.ai_narrator import _chat_completions_url, _extract_chat_content, _http_error_message
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.ai_selector import _extract_picks
from backend.app.settings import Settings


LOGGER = logging.getLogger("ai_stock.ai_full_market")

DEFAULT_POOL_SIZE = 500
DEFAULT_BATCH_SIZE = 80
DEFAULT_BATCH_PICK = 10
DEFAULT_FINAL_LIMIT = 10
AI_FULL_MARKET_TIMEOUT_SECONDS = 90
AI_FULL_MARKET_MAX_TOKENS = 3500
AI_FULL_MARKET_MAX_ATTEMPTS = 4
AI_FULL_MARKET_RETRY_BASE_SECONDS = 0.8
AI_FULL_MARKET_PROMPT_VERSION = "2026-08-02-daily-1-7-v1"
AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION = "2026-08-02-sector-style-v1"
AI_FULL_MARKET_AI_VALIDATION_VERSION = "2026-08-02-ai-evidence-v1"
AI_FULL_MARKET_SELECTION_CONFIG_KEY = "ai_full_market_selection_config_v1"
AI_FULL_MARKET_STANDARD_SCAN_TIME = "14:36"
AI_FULL_MARKET_STANDARD_END_TIME = "15:00"
AI_FULL_MARKET_OUTCOME_HORIZONS = [1, 3, 5, 7]
AI_FULL_MARKET_PRIMARY_HORIZON = 7
AI_FULL_MARKET_SECTOR_PROFILE_TIMEOUT_SECONDS = 35
AI_FULL_MARKET_SECTOR_CONSTITUENT_TIMEOUT_SECONDS = 45
AI_FULL_MARKET_DAILY_PREFILTER_MIN = 320
AI_FULL_MARKET_DAILY_PREFILTER_MAX = 1200
AI_FULL_MARKET_DAILY_KLINE_TIMEOUT_SECONDS = 75
CANDIDATE_TYPES = [
    "strong_trend",
    "sector_leader",
    "volume_breakout",
    "pullback_support",
    "low_reversal",
    "oversold_repair",
    "high_attention_high_risk",
    "rule_disliked_but_active",
]
SECTOR_STATES = [
    "rotation_in",
    "strong",
    "overheated",
    "neutral",
    "weakening",
    "distribution",
    "retreat",
    "oversold_repair",
    "data_insufficient",
]
SEMIFINAL_MIN_TYPES = {
    "pullback_support": 4,
    "low_reversal": 5,
    "oversold_repair": 3,
    "rule_disliked_but_active": 3,
}
STRATEGY_BUCKET_DEFS = [
    ("strong_trend", "强趋势 Top5", "趋势延续、板块前排、放量确认", ("strong_trend", "sector_leader")),
    ("low_start", "低位启动 Top5", "低位放量、拐点启动、首次转强", ("low_reversal", "volume_breakout")),
    ("pullback_support", "回踩承接 Top5", "趋势内回踩、缩量企稳、承接有效", ("pullback_support",)),
    ("oversold_repair", "超跌修复 Top5", "急跌后企稳、反弹修复、风险收益比改善", ("oversold_repair",)),
    ("high_attention_high_risk", "高风险人气观察榜", "人气强但波动大，只做观察和条件跟踪", ("high_attention_high_risk", "rule_disliked_but_active")),
]
DEFAULT_AI_FULL_MARKET_SELECTION_CONFIG = {
    "featureId": "ai_full_market_selection",
    "version": "v1",
    "mode": "auto",
    "promptVersion": AI_FULL_MARKET_PROMPT_VERSION,
    "minCompletedRuns": 12,
    "minValidatedOutcomes": 60,
    "minSelectedOutcomes": 20,
    "minImprovementPct": 0.5,
    "sectorEvidenceEnabled": True,
    "sectorShadowEnabled": True,
    "sectorPromptEvidenceEnabled": True,
    "sectorExposureAdjustEnabled": False,
    "styleRegimeEnabled": False,
    "maxSectorProfileStocks": 500,
    "maxSectorConstituentSectors": 20,
    "typeExposureScale": {candidate_type: 1.0 for candidate_type in CANDIDATE_TYPES},
    "sectorStateExposureScale": {sector_state: 1.0 for sector_state in SECTOR_STATES},
    "semifinalMinTypeCounts": dict(SEMIFINAL_MIN_TYPES),
}


class AIFullMarketSelectionService:
    def __init__(self, settings: Settings, analysis_service: AnalysisService, database: Database):
        self.settings = settings
        self.analysis_service = analysis_service
        self.database = database
        self._lock = threading.Lock()
        self._threads: Dict[int, threading.Thread] = {}

    def start_run(
        self,
        profile: str = "daily",
        risk_mode: str = "balanced",
        source: str = "auto",
        scan_time: str = "14:36",
        force: bool = False,
        pool_size: int = DEFAULT_POOL_SIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_pick: int = DEFAULT_BATCH_PICK,
        final_limit: int = DEFAULT_FINAL_LIMIT,
    ) -> Dict[str, Any]:
        profile = _normalize_profile(profile)
        risk_mode = risk_mode if risk_mode in {"balanced", "aggressive", "defensive"} else "balanced"
        scan_time = scan_time if scan_time in WINDOWS else "14:36"
        pool_size = _clamp_int(pool_size, 50, 800, DEFAULT_POOL_SIZE)
        batch_size = _clamp_int(batch_size, 20, 120, DEFAULT_BATCH_SIZE)
        batch_pick = _clamp_int(batch_pick, 3, 20, DEFAULT_BATCH_PICK)
        final_limit = _clamp_int(final_limit, 1, 20, DEFAULT_FINAL_LIMIT)
        selection_config = self.current_config()
        prompt_version = _prompt_version(selection_config)
        run_sample_role = _run_sample_role(profile, scan_time, datetime.now())
        today = datetime.now().date().isoformat()

        latest = self.database.latest_ai_full_market_selection_run(
            profile=profile,
            risk_mode=risk_mode,
            requested_source=source,
            scan_time=scan_time,
            created_date=today,
        )
        if latest and latest.get("status") in {"pending", "running"} and not force:
            self._resume_thread_if_needed(latest)
            return self.get_run(int(latest["id"]))
        if (
            latest
            and latest.get("status") == "done"
            and latest.get("selectionMode") == "ai_full_market"
            and not force
            and _should_reuse_latest_run(latest, run_sample_role)
        ):
            self._resume_thread_if_needed(latest)
            return self.get_run(int(latest["id"]))

        market = {
            "scanTime": scan_time,
            "promptVersion": prompt_version,
            "selectionConfigVersion": selection_config.get("version", "v1"),
            "buildPoolProgress": {
                "stage": "queued",
                "message": "等待构建全市场日线候选池。",
                "accuracyMode": "full_hard_filtered_market",
            },
        }
        market["runSampleRole"] = run_sample_role
        market["runSampleRoleLabel"] = _run_sample_role_label(run_sample_role)
        market["standardScanTime"] = AI_FULL_MARKET_STANDARD_SCAN_TIME
        market["standardEndTime"] = AI_FULL_MARKET_STANDARD_END_TIME
        market["selectionConfig"] = _selection_config_public_snapshot(selection_config)
        market["learningMeta"] = _selection_learning_meta(selection_config)
        run_id = self.database.create_ai_full_market_selection_run(
            {
                "profile": profile,
                "riskMode": risk_mode,
                "requestedSource": source,
                "actualSource": "",
                "scanTime": scan_time,
                "status": "pending",
                "selectionMode": "",
                "totalCount": 0,
                "hardFilteredCount": 0,
                "poolSize": 0,
                "batchSize": batch_size,
                "batchPick": batch_pick,
                "finalLimit": final_limit,
                "batchCount": 0,
                "request": {
                    "profile": profile,
                    "riskMode": risk_mode,
                    "source": source,
                    "scanTime": scan_time,
                    "poolSize": pool_size,
                    "batchSize": batch_size,
                    "batchPick": batch_pick,
                    "finalLimit": final_limit,
                    "promptVersion": prompt_version,
                    "runSampleRole": run_sample_role,
                    "standardScanTime": AI_FULL_MARKET_STANDARD_SCAN_TIME,
                    "selectionConfig": selection_config,
                },
                "market": market,
            },
            [],
        )
        self._start_thread(run_id)
        return self.get_run(run_id)

    def _save_sector_context_snapshots(self, run_id: int, market: Dict[str, Any], pool: List[Dict[str, Any]]) -> None:
        sector_context = market.get("sectorContext") or {}
        try:
            self.database.upsert_ai_full_market_sector_snapshots(run_id, sector_context.get("sectors") or [])
            self.database.upsert_ai_full_market_stock_sector_snapshots(run_id, pool)
            self.database.upsert_ai_full_market_style_snapshot(run_id, sector_context.get("style") or {})
        except Exception as error:  # noqa: BLE001 - snapshots must not block the selection run.
            LOGGER.warning("ai full market sector snapshot save failed run=%s error=%s", run_id, error)

    def _build_sector_context(
        self,
        *,
        candidates: List[Dict[str, Any]],
        source: str,
        scan_time: str,
        trade_date: str,
        selection_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not selection_config.get("sectorEvidenceEnabled", True):
            return _empty_sector_context(trade_date=trade_date, scan_time=scan_time, reason="板块证据开关关闭。")
        provider_source = "mock" if source == "mock" else "auto"
        warnings: List[str] = []
        provider = get_sector_provider(provider_source)
        try:
            payload = provider.fetch_sector_snapshot("all")
        except Exception as error:  # noqa: BLE001 - sector evidence degrades explicitly.
            LOGGER.warning("ai full market sector snapshot failed error=%s", error)
            warnings.append(f"实时板块快照采集失败：{error}")
            cached = _load_cached_ai_full_market_sector_context(self.database, trade_date, scan_time)
            if cached.get("sectors"):
                warnings.extend(cached.get("warnings") or [])
                return _build_sector_context_from_ranked_sectors(
                    provider=provider,
                    candidates=candidates,
                    sectors=cached.get("sectors") or [],
                    trade_date=trade_date,
                    scan_time=scan_time,
                    sector_source=str(cached.get("source") or "cached_ai_full_market_sector"),
                    warnings=warnings,
                    selection_config=selection_config,
                    data_quality=str(cached.get("dataQuality") or "stale"),
                )
            identity_context = _build_identity_only_sector_context(
                provider=provider,
                candidates=candidates,
                trade_date=trade_date,
                scan_time=scan_time,
                source="stock_sector_profile",
                warnings=warnings,
                selection_config=selection_config,
            )
            if identity_context:
                return identity_context
            return _empty_sector_context(trade_date=trade_date, scan_time=scan_time, reason=f"板块证据采集失败：{error}")
        try:
            sector_source = str(payload.get("source") or getattr(provider, "source", ""))
            warnings.extend([str(item) for item in payload.get("warnings") or [] if str(item or "").strip()])
            sectors = _rank_ai_full_market_sector_snapshots(
                payload.get("sectors") or [],
                trade_date=trade_date,
                scan_time=scan_time,
                source=sector_source,
            )
            return _build_sector_context_from_ranked_sectors(
                provider=provider,
                candidates=candidates,
                sectors=sectors,
                trade_date=trade_date,
                scan_time=scan_time,
                sector_source=sector_source,
                warnings=warnings,
                selection_config=selection_config,
                data_quality="available" if sectors else "unavailable",
            )
        except Exception as error:  # noqa: BLE001 - sector evidence degrades explicitly.
            LOGGER.warning("ai full market sector context failed error=%s", error)
            return _empty_sector_context(trade_date=trade_date, scan_time=scan_time, reason=f"板块证据采集失败：{error}")

    def current_config(self) -> Dict[str, Any]:
        value = self.database.get_app_setting(AI_FULL_MARKET_SELECTION_CONFIG_KEY, {})
        return _normalize_selection_config(value)

    def update_config(self, values: Dict[str, Any]) -> Dict[str, Any]:
        updated = _merge_selection_config(self.current_config(), values)
        self.database.set_app_setting(AI_FULL_MARKET_SELECTION_CONFIG_KEY, updated)
        return {"updated": True, "featureId": "ai_full_market_selection", "config": updated}

    def refresh_outcomes(
        self,
        *,
        run_id: int | None = None,
        profile: str = "",
        risk_mode: str = "",
        date_from: str = "",
        date_to: str = "",
        horizons: Optional[List[int]] = None,
        limit: int = 120,
        dry_run: bool = False,
        force_history: bool = False,
    ) -> Dict[str, Any]:
        normalized_horizons = _normalize_outcome_horizons(horizons)
        runs = self._load_outcome_runs(
            run_id=run_id,
            profile=profile,
            risk_mode=risk_mode,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
        benchmark_series = self.analysis_service.daily_kline_provider.fetch_many(["510300"], days=260, force=force_history, cache_only=False)
        benchmark = benchmark_series.get("510300", [])
        rows: List[Dict[str, Any]] = []
        selected_count = 0
        rejected_count = 0
        done_count = 0
        pending_count = 0
        no_data_count = 0
        for run in runs:
            items = self.database.load_ai_full_market_selection_items(int(run["id"]))
            codes = sorted({str(item.get("code") or "").zfill(6) for item in items if item.get("code")})
            series_by_code = self.analysis_service.daily_kline_provider.fetch_many(codes, days=260, force=force_history, cache_only=False)
            market_state = _classify_market_state(run.get("market") or {})
            recommendation_date = _recommendation_date_from_run(run)
            outcome_rows = _build_ai_full_market_outcome_rows(
                run,
                items,
                series_by_code,
                recommendation_date,
                normalized_horizons,
                benchmark,
                market_state,
            )
            rows.extend(outcome_rows)
            for row in outcome_rows:
                if row["selected"]:
                    selected_count += 1
                else:
                    rejected_count += 1
                if row["status"] == "done":
                    done_count += 1
                elif row["status"] == "pending":
                    pending_count += 1
                else:
                    no_data_count += 1
            if not dry_run:
                self._upsert_ai_full_market_outcomes(outcome_rows)
        report = {
            "version": "v1",
            "runCount": len(runs),
            "rowCount": len(rows),
            "selectedCount": selected_count,
            "rejectedCount": rejected_count,
            "doneCount": done_count,
            "pendingCount": pending_count,
            "noDataCount": no_data_count,
            "horizons": normalized_horizons,
            "dryRun": dry_run,
        }
        if not dry_run and rows:
            self._save_optimization_report(report, rows, runs)
        return report

    def recent_outcomes(self, *, limit: int = 50, run_id: int | None = None, selected_only: bool = False) -> Dict[str, Any]:
        clauses = []
        params: List[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            params.append(int(run_id))
        else:
            clauses.append("run_id IN (SELECT id FROM ai_full_market_selection_runs WHERE profile = 'daily' AND json_extract(market_json, '$.promptVersion') = ?)")
            params.append(AI_FULL_MARKET_PROMPT_VERSION)
        if selected_only:
            clauses.append("selected = 1")
        params.append(max(1, min(200, int(limit or 50))))
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM ai_full_market_selection_outcomes
                {where_sql}
                ORDER BY updated_at DESC, run_id DESC, selected DESC, ai_rank ASC, horizon ASC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        items = [dict(row) for row in rows]
        return {
            "version": "v1",
            "rowCount": len(items),
            "runId": run_id or 0,
            "rows": items,
        }

    def feedback_stats(self, *, current_run_id: int | None = None) -> Dict[str, Any]:
        config = self.current_config()
        effective_run_ids = self._effective_learning_run_ids()
        with self.database.connect() as connection:
            rows = []
            if effective_run_ids:
                placeholders = ",".join("?" for _ in effective_run_ids)
                rows = connection.execute(
                    f"""
                    SELECT outcome.*
                    FROM ai_full_market_selection_outcomes outcome
                    WHERE outcome.run_id IN ({placeholders})
                    ORDER BY outcome.recommendation_date DESC, outcome.run_id DESC, outcome.horizon ASC, outcome.id DESC
                    LIMIT 5000
                    """,
                    tuple(effective_run_ids),
                ).fetchall()
            current_rows = []
            if current_run_id:
                current_rows = connection.execute(
                    """
                    SELECT *
                    FROM ai_full_market_selection_outcomes
                    WHERE run_id = ?
                    ORDER BY horizon ASC, selected DESC, ai_rank ASC, rule_rank ASC, id DESC
                    """,
                    (int(current_run_id),),
                ).fetchall()
        items = [dict(row) for row in rows]
        current_items = [dict(row) for row in current_rows]
        stats = _summarize_ai_full_market_outcomes(items)
        stats["effectiveRunIds"] = effective_run_ids
        stats["effectiveTradingDayCount"] = len(effective_run_ids)
        report = _build_ai_full_market_optimization_report(stats, config)
        return {
            "version": "v1",
            "config": config,
            "stats": stats,
            "currentRunId": current_run_id or 0,
            "currentRunStats": _summarize_ai_full_market_outcomes(current_items) if current_items else {},
            "report": report,
        }

    def outcomes_review(self, *, limit: int = 1000) -> Dict[str, Any]:
        max_rows = max(50, min(5000, int(limit or 1000)))
        effective_run_ids = self._effective_learning_run_ids()
        with self.database.connect() as connection:
            rows = []
            if effective_run_ids:
                placeholders = ",".join("?" for _ in effective_run_ids)
                rows = connection.execute(
                    f"""
                    SELECT
                      outcome.*,
                      run.created_at AS run_created_at,
                      run.finished_at AS run_finished_at,
                      json_extract(run.market_json, '$.promptVersion') AS prompt_version,
                      json_extract(run.market_json, '$.runSampleRole') AS run_sample_role
                    FROM ai_full_market_selection_outcomes outcome
                    JOIN ai_full_market_selection_runs run ON run.id = outcome.run_id
                    WHERE outcome.status = 'done'
                      AND outcome.run_id IN ({placeholders})
                    ORDER BY outcome.recommendation_date DESC, outcome.run_id DESC, outcome.horizon ASC, outcome.ai_rank ASC, outcome.rule_rank ASC
                    LIMIT ?
                    """,
                    tuple(effective_run_ids) + (max_rows,),
                ).fetchall()
        items = [dict(row) for row in rows]
        review = _build_ai_full_market_review(items)
        review["effectiveRunIds"] = effective_run_ids
        review["effectiveTradingDayCount"] = len(effective_run_ids)
        feedback = self.feedback_stats()
        review["config"] = feedback.get("config") or {}
        review["report"] = feedback.get("report") or {}
        review["learningMeta"] = _selection_learning_meta(review["config"], review["report"])
        return review

    def optimize_from_history(self, *, apply: bool = True) -> Dict[str, Any]:
        stats = self.feedback_stats()
        suggestion = _suggest_ai_full_market_config(stats["stats"], stats["config"])
        current = stats["config"]
        updated = current
        applied = False
        if apply and suggestion.get("allowApply"):
            updated = _merge_selection_config(current, suggestion.get("updates") or {})
            self.database.set_app_setting(AI_FULL_MARKET_SELECTION_CONFIG_KEY, updated)
            applied = True
        report = {
            "version": "v1",
            "currentConfig": current,
            "suggestion": suggestion,
            "applied": applied,
            "updatedConfig": updated,
            "stats": stats["stats"],
        }
        report["learningMeta"] = _selection_learning_meta(updated, {"suggestion": suggestion, "eligible": suggestion.get("allowApply", False), "reason": suggestion.get("reason", "")})
        if applied or suggestion:
            self._save_optimization_report(report, [], [])
        return report

    def _load_outcome_runs(
        self,
        *,
        run_id: int | None,
        profile: str,
        risk_mode: str,
        date_from: str,
        date_to: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        clauses = ["status = 'done'"]
        params: List[Any] = []
        if run_id:
            clauses.append("id = ?")
            params.append(int(run_id))
        else:
            clauses.append("json_extract(market_json, '$.promptVersion') = ?")
            params.append(AI_FULL_MARKET_PROMPT_VERSION)
            clauses.append("profile = 'daily'")
        if profile:
            clauses.append("profile = ?")
            params.append(_normalize_profile(profile))
        if risk_mode:
            clauses.append("risk_mode = ?")
            params.append(str(risk_mode))
        if date_from:
            clauses.append("substr(created_at, 1, 10) >= ?")
            params.append(str(date_from)[:10])
        if date_to:
            clauses.append("substr(created_at, 1, 10) <= ?")
            params.append(str(date_to)[:10])
        params.append(max(1, min(500, int(limit or 120))))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM ai_full_market_selection_runs
                WHERE {' AND '.join(clauses)}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self.database._ai_full_market_run_from_row(row) for row in rows]

    def _upsert_ai_full_market_outcomes(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        payload = []
        for row in rows:
            payload.append(
                (
                    int(row.get("runId") or 0),
                    str(row.get("code") or "").zfill(6),
                    str(row.get("name") or ""),
                    str(row.get("recommendationDate") or ""),
                    str(row.get("profile") or ""),
                    str(row.get("riskMode") or ""),
                    str(row.get("selectionMode") or ""),
                    str(row.get("stage") or ""),
                    1 if row.get("selected") else 0,
                    str(row.get("candidateType") or ""),
                    int(row.get("ruleRank") or 0),
                    int(row.get("aiRank") or 0),
                    int(row.get("horizon") or 0),
                    _safe_number(row.get("buyPrice")),
                    str(row.get("exitDate") or ""),
                    _safe_number(row.get("exitPrice")),
                    _safe_number(row.get("returnPct")),
                    _safe_number(row.get("benchmarkReturnPct")),
                    _safe_number(row.get("excessReturnPct")),
                    _safe_number(row.get("maxDrawdownPct")),
                    _safe_number(row.get("maxRunupPct")),
                    str(row.get("status") or "pending"),
                    str(row.get("label") or ""),
                    str(row.get("reason") or ""),
                    str(row.get("marketState") or ""),
                    now,
                    json.dumps(row, ensure_ascii=False),
                )
            )
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO ai_full_market_selection_outcomes(
                  run_id, code, name, recommendation_date, profile, risk_mode,
                  selection_mode, stage, selected, candidate_type, rule_rank, ai_rank,
                  horizon, buy_price, exit_date, exit_price, return_pct,
                  benchmark_return_pct, excess_return_pct, max_drawdown_pct,
                  max_runup_pct, status, label, reason, market_state, updated_at,
                  outcome_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, code, horizon) DO UPDATE SET
                  name=excluded.name,
                  recommendation_date=excluded.recommendation_date,
                  profile=excluded.profile,
                  risk_mode=excluded.risk_mode,
                  selection_mode=excluded.selection_mode,
                  stage=excluded.stage,
                  selected=excluded.selected,
                  candidate_type=excluded.candidate_type,
                  rule_rank=excluded.rule_rank,
                  ai_rank=excluded.ai_rank,
                  buy_price=excluded.buy_price,
                  exit_date=excluded.exit_date,
                  exit_price=excluded.exit_price,
                  return_pct=excluded.return_pct,
                  benchmark_return_pct=excluded.benchmark_return_pct,
                  excess_return_pct=excluded.excess_return_pct,
                  max_drawdown_pct=excluded.max_drawdown_pct,
                  max_runup_pct=excluded.max_runup_pct,
                  status=excluded.status,
                  label=excluded.label,
                  reason=excluded.reason,
                  market_state=excluded.market_state,
                  updated_at=excluded.updated_at,
                  outcome_json=excluded.outcome_json
                """,
                payload,
            )
        return len(payload)

    def _save_optimization_report(self, report: Dict[str, Any], rows: List[Dict[str, Any]], runs: List[Dict[str, Any]]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        config = self.current_config()
        selected_count = len([row for row in rows if row.get("selected")])
        rejected_count = len([row for row in rows if not row.get("selected")])
        profiles = sorted({str(run.get("profile") or "") for run in runs if run.get("profile")})
        risk_modes = sorted({str(run.get("riskMode") or "") for run in runs if run.get("riskMode")})
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_full_market_selection_optimization_reports(
                  report_date, profile, risk_mode, sample_count, selected_count,
                  rejected_count, prompt_version, config_json, metrics_json,
                  suggestion_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now[:10],
                    ",".join(profiles),
                    ",".join(risk_modes),
                    int(report.get("rowCount") or report.get("sampleCount") or len(rows)),
                    selected_count,
                    rejected_count,
                    _prompt_version(config),
                    json.dumps(config, ensure_ascii=False),
                    json.dumps(report.get("stats") or report, ensure_ascii=False),
                    json.dumps(report.get("suggestion") or {}, ensure_ascii=False),
                    now,
                ),
            )

    def get_run(self, run_id: int) -> Dict[str, Any]:
        run = self.database.load_ai_full_market_selection_run(run_id)
        if not run:
            raise ValueError(f"未找到 AI 全市场选股任务 {run_id}")
        self._resume_thread_if_needed(run)
        items = self.database.load_ai_full_market_selection_items(run_id)
        run["items"] = items
        _attach_run_diagnostics(run, items)
        self._attach_daily_run_context(run)
        run["progressPct"] = _progress_pct(run)
        run["elapsedSeconds"] = _elapsed_seconds(run)
        return run

    def latest(
        self,
        profile: str = "daily",
        risk_mode: str = "balanced",
        source: str = "auto",
        scan_time: str = "14:36",
    ) -> Dict[str, Any]:
        profile = _normalize_profile(profile)
        risk_mode = risk_mode if risk_mode in {"balanced", "aggressive", "defensive"} else "balanced"
        source = source if source in {"auto", "sina", "tencent", "akshare", "mock"} else "auto"
        scan_time = scan_time if scan_time in WINDOWS else "14:36"
        today = datetime.now().date().isoformat()
        run = self.database.latest_ai_full_market_selection_run(
            profile=profile,
            risk_mode=risk_mode,
            requested_source=source,
            scan_time=scan_time,
            created_date=today,
        )
        if not run:
            return {
                "status": "empty",
                "profile": profile,
                "riskMode": risk_mode,
                "requestedSource": source,
                "scanTime": scan_time,
                "items": [],
                "standardScanTime": AI_FULL_MARKET_STANDARD_SCAN_TIME,
                "displayDate": today,
                "displayScope": "today",
            }
        return self.get_run(int(run["id"]))

    def _attach_daily_run_context(self, run: Dict[str, Any]) -> None:
        created_date = str(run.get("createdAt") or "")[:10]
        if not created_date:
            return
        profile = str(run.get("profile") or "")
        scan_time = str(run.get("scanTime") or "")
        prompt_version = str(run.get("promptVersion") or AI_FULL_MARKET_PROMPT_VERSION)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, profile, risk_mode, scan_time, status, selection_mode,
                       created_at, finished_at, market_json
                FROM ai_full_market_selection_runs
                WHERE substr(created_at, 1, 10) = ?
                  AND profile = ?
                  AND scan_time = ?
                  AND json_extract(market_json, '$.promptVersion') = ?
                ORDER BY id ASC
                """,
                (created_date, profile, scan_time, prompt_version),
            ).fetchall()
        daily_runs = [_run_sample_summary(dict(row)) for row in rows]
        effective = _effective_learning_run_from_summaries(daily_runs)
        role = _run_sample_role_from_run(run)
        run["runSampleRole"] = role
        run["runSampleRoleLabel"] = _run_sample_role_label(role)
        run["standardScanTime"] = AI_FULL_MARKET_STANDARD_SCAN_TIME
        run["standardEndTime"] = AI_FULL_MARKET_STANDARD_END_TIME
        run["dailyRunCount"] = len(daily_runs)
        run["effectiveLearningRun"] = effective or {}
        run["isEffectiveLearningRun"] = bool(effective and int(effective.get("id") or 0) == int(run.get("id") or 0))
        run["dailyRunSummary"] = daily_runs[-6:]
        learning_meta = dict(run.get("learningMeta") or {})
        learning_meta.update(
            {
                "runSampleRole": role,
                "runSampleRoleLabel": run["runSampleRoleLabel"],
                "dailyRunCount": run["dailyRunCount"],
                "effectiveLearningRun": run["effectiveLearningRun"],
                "isEffectiveLearningRun": run["isEffectiveLearningRun"],
            }
        )
        run["learningMeta"] = learning_meta

    def _effective_learning_run_ids(self) -> List[int]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, profile, risk_mode, scan_time, status, selection_mode,
                       created_at, finished_at, market_json
                FROM ai_full_market_selection_runs
                WHERE status = 'done'
                  AND profile = 'daily'
                  AND selection_mode = 'ai_full_market'
                  AND json_extract(market_json, '$.promptVersion') = ?
                ORDER BY created_at ASC, id ASC
                """,
                (AI_FULL_MARKET_PROMPT_VERSION,),
            ).fetchall()
        summaries = [_run_sample_summary(dict(row)) for row in rows]
        grouped: Dict[tuple[str, str, str, str], List[Dict[str, Any]]] = {}
        for summary in summaries:
            if summary.get("runSampleRole") not in {"standard", "late_standard"}:
                continue
            key = (
                str(summary.get("createdDate") or ""),
                str(summary.get("profile") or ""),
                str(summary.get("scanTime") or ""),
                str(summary.get("promptVersion") or ""),
            )
            grouped.setdefault(key, []).append(summary)
        effective_ids = []
        for group in grouped.values():
            effective = _effective_learning_run_from_summaries(group)
            if effective:
                effective_ids.append(int(effective["id"]))
        return sorted(effective_ids)

    def _build_pool(
        self,
        profile: str,
        risk_mode: str,
        source: str,
        scan_time: str,
        pool_size: int,
        selection_config: Dict[str, Any],
        progress_callback: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if profile == "daily":
            _emit_pool_progress(progress_callback, "market", 5, "正在获取全市场行情快照。")
            provider, market, raw_stocks, provider_warning = self.analysis_service._load_daily_market_data(source)
            filtered = self.analysis_service._apply_scope(self.analysis_service._confirmed_universe(raw_stocks), "all")
            hard_filtered = []
            for stock in filtered:
                allowed, _ = hard_filter_reason(
                    stock,
                    include_bse=self.settings.include_bse,
                    include_star_market=self.settings.include_star_market,
                )
                if allowed:
                    hard_filtered.append(stock)
            stock_pool = hard_filtered
            prefilter_meta = {
                "enabled": False,
                "reason": "准确性优先：硬过滤后的全市场股票全部进入日线评分，不使用行情快照预筛。",
                "rawCount": len(hard_filtered),
                "selectedCount": len(hard_filtered),
                "targetCount": len(hard_filtered),
            }
            stock_codes = [stock.code for stock in stock_pool]
            series_by_code: Dict[str, List[Dict[str, Any]]] = {}
            _emit_pool_progress(progress_callback, "local_daily_kline", 10, f"正在读取本地日线缓存：{len(stock_codes)} 只。")
            fallback_meta = _local_daily_kline_fallback(
                self.database,
                stock_codes,
                series_by_code,
                requested_days=180,
            )
            target_daily_date = _resolve_daily_target_date(market, fallback_meta.get("latestDate"))
            stale_codes = _daily_kline_missing_or_stale_codes(stock_codes, series_by_code, target_daily_date)
            fetched_count = 0
            if stale_codes:
                _emit_pool_progress(
                    progress_callback,
                    "fetch_daily_kline",
                    12,
                    f"正在补抓最新日线：{len(stale_codes)} 只，优先使用 baostock/Tushare 真实源。",
                    {"targetDate": target_daily_date, "missingOrStaleCount": len(stale_codes)},
                )
                fetched_series: Dict[str, List[Dict[str, Any]]] = {}
                chunks = list(_chunks(stale_codes, 300))
                for chunk_index, code_chunk in enumerate(chunks, start=1):
                    pct = 12 + round((chunk_index - 1) / max(1, len(chunks)) * 28)
                    _emit_pool_progress(
                        progress_callback,
                        "fetch_daily_kline",
                        pct,
                        f"正在补抓最新日线：第 {chunk_index}/{len(chunks)} 批，{len(code_chunk)} 只。",
                        {"targetDate": target_daily_date, "chunkIndex": chunk_index, "chunkCount": len(chunks), "fetchedCount": fetched_count},
                    )
                    chunk_series = self.analysis_service.daily_kline_provider.fetch_many(
                        code_chunk,
                        days=180,
                        force=True,
                        cache_only=False,
                        fetcher_timeout_seconds=90,
                        preferred_sources=["eastmoney", "tushare"],
                    )
                    if chunk_series:
                        fetched_series.update(chunk_series)
                        series_by_code.update(chunk_series)
                        fetched_count = len(fetched_series)
                        daily_source = self.analysis_service.daily_kline_provider.last_source or "daily_kline"
                        self.analysis_service._save_daily_kline_to_database(chunk_series, daily_source)
                        target_daily_date = _max_date(
                            target_daily_date,
                            self.analysis_service.daily_kline_provider.last_data_date,
                            _latest_series_date_from_map(chunk_series),
                        )
                    _emit_pool_progress(
                        progress_callback,
                        "fetch_daily_kline",
                        12 + round(chunk_index / max(1, len(chunks)) * 28),
                        f"日线补抓进度：{chunk_index}/{len(chunks)} 批，已获取 {fetched_count} 只。",
                        {"targetDate": target_daily_date, "chunkIndex": chunk_index, "chunkCount": len(chunks), "fetchedCount": fetched_count},
                    )
                daily_source = self.analysis_service.daily_kline_provider.last_source or "daily_kline"
            else:
                daily_source = "local_daily_kline"
            _emit_pool_progress(progress_callback, "score_daily", 44, "正在用最新日线计算候选特征和策略桶。")
            coverage_meta = _daily_kline_coverage_meta(
                stock_codes,
                series_by_code,
                target_daily_date,
                fallback_meta,
                fetched_count,
                self.analysis_service.daily_kline_provider.last_warning,
            )
            candidates = []
            for stock in stock_pool:
                series = series_by_code.get(stock.code)
                if not _daily_kline_series_is_fresh(series, target_daily_date):
                    continue
                candidate = score_daily_stock(stock, series, risk_mode=risk_mode, kline_source=daily_source)
                candidates.append(_annotate_candidate(candidate, profile="daily"))
            _emit_pool_progress(progress_callback, "layer_pool", 52, f"正在压缩候选池：有效日线候选 {len(candidates)} 只。")
            base_ranked = _layered_candidate_pool(candidates, pool_size, profile="daily", selection_config=selection_config)
            _emit_pool_progress(progress_callback, "sector_context", 58, f"正在获取板块/风格证据：候选 {len(base_ranked)} 只。")
            sector_context = self._build_sector_context(
                candidates=base_ranked,
                source=source,
                scan_time=scan_time,
                trade_date=target_daily_date or datetime.now().date().isoformat(),
                selection_config=selection_config,
            )
            _emit_pool_progress(progress_callback, "finalize_pool", 68, "正在合并板块证据并生成 AI 候选池。")
            enhanced_ranked = _apply_sector_context_to_candidates(base_ranked, sector_context, selection_config)
            shadow_ranked = _layered_candidate_pool(
                [_apply_sector_exposure_adjustment(item, selection_config) for item in enhanced_ranked],
                pool_size,
                profile="daily",
                selection_config=selection_config,
            )
            ranked = shadow_ranked if selection_config.get("sectorExposureAdjustEnabled") else enhanced_ranked
            market["providerWarning"] = provider_warning
            market["dailyPrefilter"] = prefilter_meta
            market["dailyKlineFallback"] = coverage_meta
            market["dailyKlineSource"] = daily_source
            market["dailyDataDate"] = target_daily_date
            market["scanTime"] = scan_time
            market["candidateTypeCounts"] = _candidate_type_counts(ranked)
            market["poolTypeCounts"] = market["candidateTypeCounts"]
            market.update(_build_market_environment_payload(market, ranked))
            market["sectorContext"] = _compact_sector_context(sector_context)
            market["sectorEvidenceMeta"] = sector_context.get("meta") or {}
            market["sectorRiskSummary"] = sector_context.get("summary") or {}
            market["styleRegime"] = sector_context.get("style") or {}
            market["shadowPoolSummary"] = _shadow_pool_summary(base_ranked, shadow_ranked, selection_config)
            market["promptVersion"] = _prompt_version(selection_config)
            market["selectionConfigVersion"] = selection_config.get("version", "v1")
            return {
                "pool": [_pool_item(item, index) for index, item in enumerate(ranked, start=1)],
                "totalCount": len(raw_stocks),
                "hardFilteredCount": len(hard_filtered),
                "actualSource": provider.source,
                "market": market,
            }

        raise ValueError("AI 全市场选股当前只支持日线 1-7 个交易日候选发现。")

    def _start_thread(self, run_id: int) -> None:
        with self._lock:
            thread = self._threads.get(run_id)
            if thread and thread.is_alive():
                return
            thread = threading.Thread(target=self._run_worker, args=(run_id,), daemon=True)
            self._threads[run_id] = thread
            thread.start()

    def _resume_thread_if_needed(self, run: Dict[str, Any]) -> None:
        if run.get("status") in {"pending", "running"}:
            self._start_thread(int(run["id"]))

    def _run_worker(self, run_id: int) -> None:
        started_at = datetime.now().isoformat(timespec="seconds")
        self.database.update_ai_full_market_selection_run(
            run_id,
            {"status": "running", "startedAt": started_at, "currentStage": "batch"},
        )
        try:
            run = self.database.load_ai_full_market_selection_run(run_id)
            if not run:
                return
            request = run.get("request") or {}
            selection_config = _normalize_selection_config(request.get("selectionConfig") or self.current_config())
            pool_rows = self.database.load_ai_full_market_selection_items(run_id)
            if not pool_rows:
                def pool_progress(stage: str, pct: int, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
                    latest_run = self.database.load_ai_full_market_selection_run(run_id) or run
                    self.database.update_ai_full_market_selection_run(
                        run_id,
                        {
                            "currentStage": f"building_pool:{stage}",
                            "market": _merge_market_progress(latest_run.get("market") or {}, stage, message, pct, extra),
                        },
                    )

                self.database.update_ai_full_market_selection_run(
                    run_id,
                    {
                        "currentStage": "building_pool",
                        "market": _merge_market_progress(
                            run.get("market") or {},
                            "building_pool",
                            "正在构建全市场日线候选池并补齐最新日线数据。",
                            3,
                        ),
                    },
                )
                batch = self._build_pool(
                    str(run.get("profile") or request.get("profile") or "daily"),
                    str(run.get("riskMode") or request.get("riskMode") or "balanced"),
                    str(run.get("requestedSource") or request.get("source") or "auto"),
                    str(run.get("scanTime") or request.get("scanTime") or "14:36"),
                    int(request.get("poolSize") or DEFAULT_POOL_SIZE),
                    selection_config,
                    progress_callback=pool_progress,
                )
                pool = batch.get("pool") or []
                if not pool:
                    raise ValueError(batch.get("error") or "没有可供 AI 全市场选股的大候选池。")
                market = dict(batch.get("market") or {})
                market["runSampleRole"] = (run.get("market") or {}).get("runSampleRole") or request.get("runSampleRole") or ""
                market["runSampleRoleLabel"] = _run_sample_role_label(str(market.get("runSampleRole") or ""))
                market["standardScanTime"] = AI_FULL_MARKET_STANDARD_SCAN_TIME
                market["standardEndTime"] = AI_FULL_MARKET_STANDARD_END_TIME
                market["selectionConfig"] = _selection_config_public_snapshot(selection_config)
                market["learningMeta"] = _selection_learning_meta(selection_config)
                market["buildPoolProgress"] = {
                    "stage": "done",
                    "message": "全市场日线候选池构建完成。",
                    "accuracyMode": "full_hard_filtered_market",
                }
                batch_count = math.ceil(len(pool) / int(run.get("batchSize") or DEFAULT_BATCH_SIZE))
                self.database.upsert_ai_full_market_selection_items(run_id, pool, stage="pool")
                self._save_sector_context_snapshots(run_id, market, pool)
                self.database.update_ai_full_market_selection_run(
                    run_id,
                    {
                        "actualSource": batch.get("actualSource", ""),
                        "totalCount": batch.get("totalCount", 0),
                        "hardFilteredCount": batch.get("hardFilteredCount", 0),
                        "poolSize": len(pool),
                        "batchCount": batch_count,
                        "market": market,
                        "currentStage": "batch",
                    },
                )
                run = self.database.load_ai_full_market_selection_run(run_id) or run
            pool = [item.get("item") or {} for item in self.database.load_ai_full_market_selection_items(run_id)]
            profile = str(run.get("profile") or "daily")
            final_limit = int(run.get("finalLimit") or DEFAULT_FINAL_LIMIT)
            batch_size = int(run.get("batchSize") or DEFAULT_BATCH_SIZE)
            batch_pick = int(run.get("batchPick") or DEFAULT_BATCH_PICK)
            market = run.get("market") or {}
            market["promptVersion"] = _prompt_version(selection_config)
            market["selectionConfigVersion"] = selection_config.get("version", "v1")
            market["poolTypeCounts"] = market.get("poolTypeCounts") or _candidate_type_counts(pool)
            LOGGER.info(
                "ai full select start profile=%s raw=%s hard_filtered=%s pool=%s batch_size=%s",
                profile,
                run.get("totalCount"),
                run.get("hardFilteredCount"),
                len(pool),
                batch_size,
            )
            if not self._ai_available():
                fallback = _rules_fallback(pool, final_limit)
                rejected = _rejected_candidates(pool, fallback, stage="rules_fallback")
                market["finalTypeCounts"] = _selected_type_counts_by_used_type(fallback)
                market["strategyBuckets"] = _build_strategy_buckets(pool, [], fallback, rejected, profile)
                market["fallbackReason"] = "AI 未配置，已使用规则兜底。"
                market["aiCallSummary"] = _ai_call_summary([], {}, _prompt_version(selection_config))
                market["rejectedStats"] = _rejected_stats(pool, fallback, rejected)
                self.database.upsert_ai_full_market_selection_items(run_id, fallback, stage="final")
                self.database.upsert_ai_full_market_selection_items(run_id, rejected, stage="rules_fallback")
                self.database.update_ai_full_market_selection_run(
                    run_id,
                    {
                        "status": "done",
                        "selectionMode": "rules_fallback",
                        "finalPicks": fallback,
                        "finalCount": len(fallback),
                        "market": market,
                        "error": "AI 未配置，已使用规则兜底。",
                        "finishedAt": datetime.now().isoformat(timespec="seconds"),
                    },
                )
                return

            batch_summary = []
            semifinal: List[Dict[str, Any]] = []
            chunks = _chunks(pool, batch_size)
            for batch_index, chunk in enumerate(chunks, start=1):
                current_batch_pick = _batch_pick_for_size(len(chunk), batch_size, batch_pick)
                self.database.update_ai_full_market_selection_run(
                    run_id,
                    {"currentStage": f"batch:{batch_index}/{len(chunks)}"},
                )
                ai_started = time.perf_counter()
                picks, error = self._select_with_ai(
                    candidates=chunk,
                    market=market,
                    limit=current_batch_pick,
                    profile=profile,
                    stage="batch",
                    batch_index=batch_index,
                    selection_config=selection_config,
                )
                elapsed_ms = int((time.perf_counter() - ai_started) * 1000)
                selected, pick_stats = _apply_picks(chunk, picks, limit=current_batch_pick, batch_index=batch_index)
                if not selected and error:
                    LOGGER.warning("ai full select batch empty profile=%s index=%s error=%s", profile, batch_index, error)
                    selected = _rules_fallback(
                        chunk,
                        current_batch_pick,
                        reason="AI 批次调用中断，按规则分补位进入半决赛。",
                        risk="该批次候选未经过 AI 初筛比较，终选时需要更严格验证。",
                        focus="等待终选阶段结合其他批次候选统一比较。",
                        selection_source="batch-rules-fallback",
                        batch_index=batch_index,
                    )
                    pick_stats["rulesFallbackPickCount"] = len(selected)
                rejected = _rejected_candidates(chunk, selected, stage="batch_rejected", batch_index=batch_index)
                semifinal.extend(selected)
                batch_summary.append(
                    {
                        "batchIndex": batch_index,
                        "candidateCount": len(chunk),
                        "targetPickCount": current_batch_pick,
                        "pickCount": len(selected),
                        "rawPickCount": len(picks),
                        "elapsedMs": elapsed_ms,
                        "typeCounts": _candidate_type_counts(selected),
                        **pick_stats,
                        "rejectedCount": len(rejected),
                        "error": error,
                    }
                )
                market["semifinalTypeCounts"] = _candidate_type_counts(_unique_by_code(semifinal))
                market["aiCallSummary"] = _ai_call_summary(batch_summary, {}, _prompt_version(selection_config))
                self.database.upsert_ai_full_market_selection_items(run_id, selected, stage="semifinal")
                self.database.upsert_ai_full_market_selection_items(run_id, rejected, stage="batch_rejected")
                self.database.update_ai_full_market_selection_run(
                    run_id,
                    {
                        "completedBatchCount": batch_index,
                        "semifinalCount": len(_unique_by_code(semifinal)),
                        "batchSummary": batch_summary,
                        "market": market,
                    },
                )
                LOGGER.info(
                    "ai full select batch done profile=%s index=%s candidates=%s picks=%s",
                    profile,
                    batch_index,
                    len(chunk),
                    len(selected),
                )

            semifinal = _unique_by_code(semifinal)
            supplements, supplement_summary = _semifinal_supplements(pool, semifinal, profile, selection_config)
            if supplements:
                semifinal = _unique_by_code(semifinal + supplements)
                self.database.upsert_ai_full_market_selection_items(run_id, supplements, stage="semifinal")
            market["semifinalSupplement"] = supplement_summary
            market["semifinalTypeCounts"] = _candidate_type_counts(semifinal)
            self.database.update_ai_full_market_selection_run(
                run_id,
                {"currentStage": "final", "semifinalCount": len(semifinal), "market": market},
            )
            final_started = time.perf_counter()
            final_picks, final_error = self._select_with_ai(
                candidates=semifinal,
                market=market,
                limit=final_limit,
                profile=profile,
                stage="final",
                batch_index=0,
                selection_config=selection_config,
            )
            final_elapsed_ms = int((time.perf_counter() - final_started) * 1000)
            final_selected, final_stats = _apply_picks(semifinal, final_picks, limit=final_limit, batch_index=0)
            if final_selected:
                final_selected, rebalance_summary = _rebalance_final_selection(final_selected, semifinal, final_limit, profile)
                final_stats["rebalanceReplacementCount"] = len(rebalance_summary.get("replacements") or [])
                if rebalance_summary.get("replacements"):
                    market["finalRebalance"] = rebalance_summary
            final_rejected = _rejected_candidates(semifinal, final_selected, stage="final_rejected")
            final_summary = {
                "candidateCount": len(semifinal),
                "targetPickCount": final_limit,
                "pickCount": len(final_selected),
                "rawPickCount": len(final_picks),
                "elapsedMs": final_elapsed_ms,
                "typeCounts": _selected_type_counts_by_used_type(final_selected),
                **final_stats,
                "rejectedCount": len(final_rejected),
                "error": final_error,
            }
            selection_mode = "ai_full_market"
            error = final_error
            if not final_selected:
                fallback_pool = semifinal if semifinal else pool
                final_selected = _rules_fallback(
                    fallback_pool,
                    final_limit,
                    reason="AI 终选调用中断，已从半决赛候选中按规则分兜底。",
                    risk="该结果不是完整 AI 终选，买入前必须人工复核板块持续性、位置和触发条件。",
                    focus="优先等待重新运行 AI 成功；若盘中使用，只把它作为观察名单。",
                    selection_source="final-rules-fallback",
                )
                final_rejected = _rejected_candidates(fallback_pool, final_selected, stage="rules_fallback")
                selection_mode = "rules_fallback"
                error = f"AI 终选连接不稳定，已使用规则兜底完成。原始错误：{final_error}" if final_error else "AI 未返回有效终选结果，已使用规则兜底。"
                market["fallbackReason"] = error
            market["finalTypeCounts"] = _selected_type_counts_by_used_type(final_selected)
            market["finalCallSummary"] = final_summary
            market["aiCallSummary"] = _ai_call_summary(batch_summary, final_summary, _prompt_version(selection_config))
            market["rejectedStats"] = _rejected_stats(pool, final_selected, final_rejected)
            market["strategyBuckets"] = _build_strategy_buckets(pool, semifinal, final_selected, final_rejected, profile)
            self.database.upsert_ai_full_market_selection_items(run_id, final_rejected, stage="final_rejected")
            self.database.upsert_ai_full_market_selection_items(run_id, final_selected, stage="final")
            self.database.update_ai_full_market_selection_run(
                run_id,
                {
                    "status": "done",
                    "selectionMode": selection_mode,
                    "currentStage": "",
                    "finalPicks": final_selected,
                    "finalCount": len(final_selected),
                    "market": market,
                    "error": error,
                    "finishedAt": datetime.now().isoformat(timespec="seconds"),
                },
            )
            LOGGER.info(
                "ai full select saved profile=%s selection_mode=%s top=%s",
                profile,
                selection_mode,
                len(final_selected),
            )
        except Exception as error:  # noqa: BLE001 - task state must remain visible.
            self.database.update_ai_full_market_selection_run(
                run_id,
                {
                    "status": "failed",
                    "selectionMode": "ai_failed",
                    "error": str(error),
                    "finishedAt": datetime.now().isoformat(timespec="seconds"),
                },
            )
            LOGGER.exception("ai full select run failed id=%s error=%s", run_id, error)

    def _ai_available(self) -> bool:
        return bool(self.settings.enable_ai and self.settings.ai_api_key and self.settings.ai_base_url)

    def _select_with_ai(
        self,
        candidates: List[Dict[str, Any]],
        market: Dict[str, Any],
        limit: int,
        profile: str,
        stage: str,
        batch_index: int,
        selection_config: Optional[Dict[str, Any]] = None,
    ) -> tuple[List[Dict[str, Any]], str]:
        if not candidates:
            return [], ""
        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {"role": "system", "content": _role_prompt(profile, stage)},
                {
                    "role": "user",
                    "content": _user_prompt(candidates, market, limit, profile, stage, selection_config),
                },
            ],
            "temperature": 0.12,
            "max_tokens": AI_FULL_MARKET_MAX_TOKENS,
            "stream": False,
        }
        if self.settings.ai_provider in {"deepseek", "openai"}:
            payload["response_format"] = {"type": "json_object"}
        if self.settings.ai_provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}

        last_error = ""
        for attempt in range(1, AI_FULL_MARKET_MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            LOGGER.info(
                "ai full select call start provider=%s model=%s profile=%s stage=%s batch=%s attempt=%s candidates=%s limit=%s",
                self.settings.ai_provider,
                self.settings.ai_model,
                profile,
                stage,
                batch_index,
                attempt,
                len(candidates),
                limit,
            )
            request = urllib.request.Request(
                _chat_completions_url(self.settings.ai_base_url),
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.settings.ai_api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=AI_FULL_MARKET_TIMEOUT_SECONDS) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                last_error = _http_error_message(error)
                if error.code not in {408, 409, 425, 429, 500, 502, 503, 504}:
                    return [], last_error
                LOGGER.warning(
                    "ai full select http retryable profile=%s stage=%s batch=%s attempt=%s elapsed_ms=%.0f status=%s error=%s",
                    profile,
                    stage,
                    batch_index,
                    attempt,
                    (time.perf_counter() - started) * 1000,
                    error.code,
                    last_error,
                )
                if attempt < AI_FULL_MARKET_MAX_ATTEMPTS:
                    time.sleep(_ai_retry_delay(attempt))
                continue
            except (
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
                ssl.SSLError,
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                OSError,
                json.JSONDecodeError,
            ) as error:
                last_error = f"{self.settings.ai_provider} AI全市场选股调用失败：{_compact_error(error)}"
                LOGGER.warning(
                    "ai full select failed profile=%s stage=%s batch=%s attempt=%s elapsed_ms=%.0f error=%s",
                    profile,
                    stage,
                    batch_index,
                    attempt,
                    (time.perf_counter() - started) * 1000,
                    error,
                )
                if attempt < AI_FULL_MARKET_MAX_ATTEMPTS:
                    time.sleep(_ai_retry_delay(attempt))
                continue
            content = _extract_chat_content(data)
            picks = _extract_picks(content)
            LOGGER.info(
                "ai full select call ok profile=%s stage=%s batch=%s attempt=%s elapsed_ms=%.0f picks=%s content_len=%s",
                profile,
                stage,
                batch_index,
                attempt,
                (time.perf_counter() - started) * 1000,
                len(picks),
                len(content),
            )
            if picks:
                validation = _validate_ai_full_market_picks_evidence(picks, candidates, stage)
                if validation.get("valid"):
                    return _attach_ai_full_market_validation_to_picks(picks, validation), ""
                filtered_picks = _filter_ai_picks_with_validation_errors(picks, validation)
                if filtered_picks and len(filtered_picks) < len(picks):
                    filtered_validation = _validate_ai_full_market_picks_evidence(filtered_picks, candidates, stage)
                    if filtered_validation.get("valid"):
                        LOGGER.warning(
                            "ai full select evidence validation filtered invalid picks profile=%s stage=%s batch=%s original=%s kept=%s errors=%s",
                            profile,
                            stage,
                            batch_index,
                            len(picks),
                            len(filtered_picks),
                            validation.get("errors"),
                        )
                        return _attach_ai_full_market_validation_to_picks(filtered_picks, filtered_validation), ""
                last_error = "AI 证据校验失败：" + "；".join(validation.get("errors") or [])[:360]
                LOGGER.warning(
                    "ai full select evidence validation failed profile=%s stage=%s batch=%s errors=%s",
                    profile,
                    stage,
                    batch_index,
                    validation.get("errors"),
                )
                if attempt < AI_FULL_MARKET_MAX_ATTEMPTS:
                    time.sleep(_ai_retry_delay(attempt))
                    continue
                return [], last_error
            LOGGER.warning(
                "ai full select invalid_json profile=%s stage=%s batch=%s preview=%s",
                profile,
                stage,
                batch_index,
                content[:500].replace("\n", " "),
            )
            last_error = f"{self.settings.ai_provider} 返回内容不是有效选股 JSON"
            if attempt < AI_FULL_MARKET_MAX_ATTEMPTS:
                time.sleep(_ai_retry_delay(attempt))
        return [], last_error


def _empty_sector_context(trade_date: str, scan_time: str, reason: str) -> Dict[str, Any]:
    return {
        "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
        "meta": {
            "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
            "source": "",
            "tradeDate": trade_date,
            "asOfTime": scan_time,
            "sectorCount": 0,
            "stockMapCount": 0,
            "coveragePct": 0,
            "warnings": [reason],
            "dataQuality": "unavailable",
        },
        "sectors": [],
        "stockMaps": {},
        "style": {
            "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
            "marketRisk": "unknown",
            "styleRegime": "data_insufficient",
            "riskGroups": [],
            "favoredGroups": [],
            "riskSectors": [],
            "favoredSectors": [],
            "confidence": 0,
            "evidenceRefs": [reason],
            "tradeDate": trade_date,
            "asOfTime": scan_time,
            "dataQuality": "unavailable",
        },
        "summary": {"riskSectors": [], "favoredSectors": [], "dataQuality": "unavailable", "message": reason},
    }


def _build_sector_context_from_ranked_sectors(
    *,
    provider: Any,
    candidates: List[Dict[str, Any]],
    sectors: List[Dict[str, Any]],
    trade_date: str,
    scan_time: str,
    sector_source: str,
    warnings: List[str],
    selection_config: Dict[str, Any],
    data_quality: str,
) -> Dict[str, Any]:
    stock_profiles = _fetch_ai_full_market_stock_sector_profiles(
        provider=provider,
        candidates=candidates[: int(selection_config.get("maxSectorProfileStocks") or 500)],
        trade_date=trade_date,
        scan_time=scan_time,
        source=sector_source,
    )
    constituents = _fetch_ai_full_market_sector_constituents(
        provider=provider,
        sectors=_sector_constituent_targets(sectors, stock_profiles, int(selection_config.get("maxSectorConstituentSectors") or 20)),
        limit=30,
    )
    sectors = _attach_sector_constituent_counts(sectors, constituents)
    sector_by_name = {(str(item.get("sectorType") or ""), str(item.get("sectorName") or "")): item for item in sectors}
    stock_maps = _build_ai_full_market_stock_sector_maps(candidates, stock_profiles, sector_by_name, constituents, trade_date, scan_time, sector_source)
    style = _build_style_regime_snapshot(sectors, trade_date, scan_time, sector_source)
    if data_quality != "available":
        style["dataQuality"] = data_quality
        style["evidenceRefs"] = _unique_strings(list(style.get("evidenceRefs") or []) + ["板块强弱来自历史快照兜底，只能作为弱证据。"])
    summary = _sector_context_summary(sectors, stock_maps)
    summary["dataQuality"] = data_quality
    if data_quality != "available":
        summary["message"] = f"{summary.get('message') or ''} 当前板块强弱来自历史快照兜底，只能作为弱证据。".strip()
    return {
        "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
        "meta": {
            "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
            "source": sector_source,
            "tradeDate": trade_date,
            "asOfTime": scan_time,
            "sectorCount": len(sectors),
            "stockMapCount": len(stock_maps),
            "coveragePct": _rate(len([item for item in stock_maps.values() if item.get("sectorEvidenceStatus") == "available"]), len(candidates)),
            "warnings": warnings[:8],
            "dataQuality": data_quality if sectors else "unavailable",
        },
        "sectors": sectors,
        "stockMaps": stock_maps,
        "style": style,
        "summary": summary,
    }


def _build_identity_only_sector_context(
    *,
    provider: Any,
    candidates: List[Dict[str, Any]],
    trade_date: str,
    scan_time: str,
    source: str,
    warnings: List[str],
    selection_config: Dict[str, Any],
) -> Dict[str, Any]:
    stock_profiles = _fetch_ai_full_market_stock_sector_profiles(
        provider=provider,
        candidates=candidates[: int(selection_config.get("maxSectorProfileStocks") or 500)],
        trade_date=trade_date,
        scan_time=scan_time,
        source=source,
    )
    if not stock_profiles:
        return {}
    stock_maps = _build_ai_full_market_stock_sector_maps(candidates, stock_profiles, {}, [], trade_date, scan_time, source)
    message = "实时板块强弱不可用，仅获取到个股行业/概念身份；DeepSeek 不能据此输出板块强弱、主线前排或资金共振。"
    return {
        "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
        "meta": {
            "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
            "source": source,
            "tradeDate": trade_date,
            "asOfTime": scan_time,
            "sectorCount": 0,
            "stockMapCount": len(stock_maps),
            "coveragePct": _rate(len([item for item in stock_maps.values() if item.get("sectorEvidenceStatus") in {"partial", "available"}]), len(candidates)),
            "warnings": _unique_strings([*warnings, message])[:8],
            "dataQuality": "identity_only",
        },
        "sectors": [],
        "stockMaps": stock_maps,
        "style": {
            "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
            "marketRisk": "unknown",
            "styleRegime": "data_insufficient",
            "riskGroups": [],
            "favoredGroups": [],
            "riskSectors": [],
            "favoredSectors": [],
            "confidence": 0,
            "evidenceRefs": [message],
            "tradeDate": trade_date,
            "asOfTime": scan_time,
            "source": source,
            "dataQuality": "identity_only",
        },
        "summary": {"riskSectors": [], "favoredSectors": [], "dataQuality": "identity_only", "message": message},
    }


def _load_cached_ai_full_market_sector_context(database: Database, trade_date: str, scan_time: str) -> Dict[str, Any]:
    try:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, trade_date, as_of_time, source
                FROM ai_full_market_sector_snapshots
                WHERE data_quality IN ('available', 'partial')
                ORDER BY trade_date DESC, run_id DESC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return {}
            rows = connection.execute(
                """
                SELECT snapshot_json
                FROM ai_full_market_sector_snapshots
                WHERE run_id = ?
                """,
                (int(row["run_id"]),),
            ).fetchall()
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("ai full market cached sector snapshot load failed error=%s", error)
        return {}
    sectors = [_json_or_empty(item["snapshot_json"]) for item in rows]
    sectors = [item for item in sectors if item.get("sectorName")]
    if not sectors:
        return {}
    cached_date = str(row["trade_date"] or "")
    data_quality = "stale" if cached_date and cached_date != str(trade_date or "") else "available"
    return {
        "sectors": sectors,
        "source": f"{row['source'] or 'sector_snapshot'}_cache",
        "dataQuality": data_quality,
        "warnings": [f"实时板块源不可用，使用历史板块快照 run={row['run_id']}，快照日期 {cached_date or '--'} {row['as_of_time'] or ''}。"],
    }


def _rank_ai_full_market_sector_snapshots(sectors: List[Dict[str, Any]], trade_date: str, scan_time: str, source: str) -> List[Dict[str, Any]]:
    clean = []
    for item in sectors:
        if not isinstance(item, dict):
            continue
        sector_type = str(item.get("sectorType") or "").strip()
        sector_name = str(item.get("sectorName") or "").strip()
        sector_code = str(item.get("sectorCode") or sector_name).strip()
        if sector_type not in {"industry", "concept"} or not sector_name:
            continue
        row = dict(item)
        row["sectorType"] = sector_type
        row["sectorName"] = sector_name
        row["sectorCode"] = sector_code
        row["source"] = source
        row["tradeDate"] = trade_date
        row["asOfTime"] = scan_time
        clean.append(row)
    ranked = sorted(
        clean,
        key=lambda row: (
            0 if row.get("sectorType") == "industry" else 1,
            -float(row.get("changePct") or 0),
            -float(row.get("turnoverAmount") or 0),
            -float(row.get("mainNetInflow") or 0),
        ),
    )
    per_type_rank: Dict[str, int] = {}
    result = []
    for item in ranked:
        sector_type = str(item.get("sectorType") or "")
        per_type_rank[sector_type] = per_type_rank.get(sector_type, 0) + 1
        row = dict(item)
        row["rank"] = per_type_rank[sector_type]
        row["ruleRank"] = per_type_rank[sector_type]
        row.update(_classify_sector_state(row))
        result.append(row)
    return result


def _classify_sector_state(sector: Dict[str, Any]) -> Dict[str, Any]:
    change = _safe_number(sector.get("changePct"))
    up_count = int(sector.get("upCount") or 0)
    down_count = int(sector.get("downCount") or 0)
    total = up_count + down_count + int(sector.get("flatCount") or 0)
    up_ratio = up_count / total if total > 0 else None
    limit_up = int(sector.get("limitUpCount") or 0)
    rank = int(sector.get("rank") or sector.get("ruleRank") or 999)
    flow = _safe_number(sector.get("mainNetInflow"))
    flow_ratio = _safe_number(sector.get("mainNetInflowRatio"))
    fund_ok = str(sector.get("fundFlowStatus") or "") == "ok"
    reasons: List[str] = []
    counter: List[str] = []

    if change is None:
        return {
            "sectorState": "data_insufficient",
            "sectorRiskScore": 60,
            "sectorConfidence": 20,
            "upRatioPct": None,
            "flowInterpretation": "unknown",
            "dataQuality": {"status": "unavailable", "missingFields": ["changePct"]},
            "positiveEvidenceRefs": [],
            "counterEvidenceRefs": ["S2: 缺少板块涨跌幅，不能形成板块强弱结论。"],
        }

    if up_ratio is not None:
        reasons.append(f"S1: 成分扩散 {round(up_ratio * 100, 1)}%。")
    if fund_ok:
        reasons.append(f"S2: 主力净流入 {round(float(flow or 0) / 100000000, 2)} 亿，净占比 {flow_ratio if flow_ratio is not None else '--'}%。")
    else:
        counter.append("S2: 主力资金流缺失或非可靠口径，只能作弱证据。")
    reasons.append(f"S3: 板块涨跌幅 {round(change, 2)}%，同类排名 {rank}，涨停 {limit_up}。")

    flow_positive = fund_ok and (flow or 0) > 0 and (flow_ratio is None or flow_ratio > 0)
    flow_negative = fund_ok and ((flow or 0) < 0 or (flow_ratio is not None and flow_ratio < 0))
    broad_strong = up_ratio is not None and up_ratio >= 0.62
    broad_weak = up_ratio is not None and up_ratio <= 0.38
    active_inflow = bool(flow_positive and change > 0 and (broad_strong or limit_up > 0))
    passive_defensive = bool(change >= 0 and not active_inflow and (flow_negative or not broad_strong))

    if broad_weak and change <= -2.0 and flow_negative:
        state = "retreat"
        risk = 88
        confidence = 82
        counter.append("S4: 板块普跌且资金流出，局部退潮风险高。")
    elif change <= -1.2 and (flow_negative or broad_weak):
        state = "retreat"
        risk = 80
        confidence = 72
        counter.append("S4: 板块跌幅和扩散偏弱，普通趋势/追涨候选应作为反方证据。")
    elif change > 0.5 and flow_negative and not broad_strong:
        state = "distribution"
        risk = 76
        confidence = 68
        counter.append("S4: 价格上涨但资金或扩散不确认，可能是被动抗跌或派发。")
    elif change >= 4.5 or (change >= 3.2 and limit_up >= 4):
        state = "overheated"
        risk = 70
        confidence = 74
        counter.append("S4: 板块强但拥挤，追高和兑现风险上升。")
    elif active_inflow and rank <= 15 and 0.4 <= change <= 3.5:
        state = "rotation_in"
        risk = 36
        confidence = 76
        reasons.append("S4: 涨幅、扩散和资金方向共同改善，偏主动轮入。")
    elif change >= 1.5 and broad_strong and (flow_positive or limit_up >= 1):
        state = "strong"
        risk = 42
        confidence = 74
        reasons.append("S4: 板块趋势、扩散和前排强度较好。")
    elif change > 0 and broad_weak:
        state = "oversold_repair"
        risk = 58
        confidence = 58
        counter.append("S4: 反弹但扩散不足，只能按修复看，不能写成重回主升。")
    elif change < -0.4 or flow_negative:
        state = "weakening"
        risk = 64
        confidence = 60
        counter.append("S4: 板块边际转弱，需要降低普通趋势延续证据权重。")
    else:
        state = "neutral"
        risk = 50
        confidence = 54

    return {
        "sectorState": state,
        "sectorRiskScore": risk,
        "sectorConfidence": confidence,
        "upRatioPct": round(up_ratio * 100, 1) if up_ratio is not None else None,
        "flowInterpretation": "active_inflow" if active_inflow else "passive_defensive" if passive_defensive else "outflow" if flow_negative else "unknown",
        "dataQuality": {"status": "available" if total > 0 else "partial", "missingFields": [] if fund_ok else ["fundFlow"]},
        "positiveEvidenceRefs": reasons[:6],
        "counterEvidenceRefs": counter[:6],
    }


def _fetch_ai_full_market_stock_sector_profiles(
    provider: Any,
    candidates: List[Dict[str, Any]],
    trade_date: str,
    scan_time: str,
    source: str,
) -> Dict[str, Dict[str, Any]]:
    fetcher = getattr(provider, "fetch_stock_sector_profile", None)
    if not callable(fetcher) or not candidates:
        return {}
    rows: Dict[str, Dict[str, Any]] = {}

    def fetch_one(candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        code = _normalize_code(candidate.get("code"))
        if not code:
            return None
        profile = fetcher(code)
        industry = str(profile.get("industrySector") or "").strip()
        concepts = _unique_strings(profile.get("conceptSectors") or [])
        if not industry and not concepts:
            return None
        return {
            "code": code,
            "name": candidate.get("name") or profile.get("name") or "",
            "industrySector": industry,
            "conceptSectors": concepts,
            "source": profile.get("source") or source,
            "tradeDate": trade_date,
            "asOfTime": scan_time,
        }

    executor = ThreadPoolExecutor(max_workers=min(16, max(1, len(candidates))))
    futures = {executor.submit(fetch_one, candidate): candidate for candidate in candidates}
    try:
        for future in as_completed(futures, timeout=AI_FULL_MARKET_SECTOR_PROFILE_TIMEOUT_SECONDS):
            candidate = futures[future]
            try:
                row = future.result()
            except Exception as error:  # noqa: BLE001
                LOGGER.debug("ai full market stock sector profile failed code=%s error=%s", candidate.get("code"), error)
                continue
            if row and row.get("code"):
                rows[str(row["code"])] = row
    except FutureTimeoutError:
        LOGGER.warning("ai full market stock sector profile timed out candidates=%s", len(candidates))
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    return rows


def _sector_constituent_targets(sectors: List[Dict[str, Any]], profiles: Dict[str, Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    sector_by_name = {(str(item.get("sectorType") or ""), str(item.get("sectorName") or "")): item for item in sectors}
    names: List[tuple[str, str]] = []
    for profile in profiles.values():
        if profile.get("industrySector"):
            names.append(("industry", str(profile.get("industrySector"))))
        for concept in profile.get("conceptSectors") or []:
            names.append(("concept", str(concept)))
    matched = [sector_by_name[key] for key in _unique_tuples(names) if key in sector_by_name]
    if len(matched) < limit:
        matched += [item for item in sectors if item not in matched and int(item.get("rank") or 999) <= 12]
    return matched[: max(0, limit)]


def _fetch_ai_full_market_sector_constituents(provider: Any, sectors: List[Dict[str, Any]], limit: int = 30) -> List[Dict[str, Any]]:
    fetcher = getattr(provider, "fetch_constituents", None)
    if not callable(fetcher) or not sectors:
        return []
    rows: List[Dict[str, Any]] = []

    def fetch_one(sector: Dict[str, Any]) -> List[Dict[str, Any]]:
        sector_type = str(sector.get("sectorType") or "")
        sector_code = str(sector.get("sectorCode") or sector.get("sectorName") or "")
        sector_name = str(sector.get("sectorName") or "")
        result = []
        for index, item in enumerate(fetcher(sector_code or sector_name, sector_type, limit=limit), start=1):
            code = _normalize_code(item.get("code"))
            if not code:
                continue
            result.append(
                {
                    **dict(item),
                    "code": code,
                    "sectorType": sector_type,
                    "sectorCode": sector_code,
                    "sectorName": sector_name,
                    "sectorRank": int(sector.get("rank") or 0),
                    "stockRank": index,
                    "sectorState": sector.get("sectorState"),
                }
            )
        return result

    executor = ThreadPoolExecutor(max_workers=min(8, max(1, len(sectors))))
    futures = {executor.submit(fetch_one, sector): sector for sector in sectors}
    try:
        for future in as_completed(futures, timeout=AI_FULL_MARKET_SECTOR_CONSTITUENT_TIMEOUT_SECONDS):
            try:
                rows.extend(future.result())
            except Exception as error:  # noqa: BLE001
                sector = futures[future]
                LOGGER.debug("ai full market sector constituent failed sector=%s error=%s", sector.get("sectorName"), error)
    except FutureTimeoutError:
        LOGGER.warning("ai full market sector constituents timed out sectors=%s", len(sectors))
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    return rows


def _attach_sector_constituent_counts(sectors: List[Dict[str, Any]], constituents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple[str, str], Dict[str, int]] = {}
    for item in constituents:
        key = (str(item.get("sectorType") or ""), str(item.get("sectorCode") or ""))
        bucket = grouped.setdefault(key, {"limitUpCount": 0, "nearLimitUpCount": 0, "constituentSampleCount": 0})
        bucket["constituentSampleCount"] += 1
        change = _safe_number(item.get("changePct")) or 0
        status = str(item.get("limitStatus") or "")
        if status in {"limit_up", "one_word_limit_up"} or change >= 9.8:
            bucket["limitUpCount"] += 1
        elif status == "near_limit_up" or change >= 8:
            bucket["nearLimitUpCount"] += 1
    result = []
    for sector in sectors:
        row = dict(sector)
        bucket = grouped.get((str(row.get("sectorType") or ""), str(row.get("sectorCode") or ""))) or {}
        if bucket:
            row["limitUpCount"] = max(int(row.get("limitUpCount") or 0), int(bucket.get("limitUpCount") or 0))
            row["nearLimitUpCount"] = int(bucket.get("nearLimitUpCount") or 0)
            row["constituentSampleCount"] = int(bucket.get("constituentSampleCount") or 0)
            row.update(_classify_sector_state(row))
        result.append(row)
    return result


def _build_ai_full_market_stock_sector_maps(
    candidates: List[Dict[str, Any]],
    profiles: Dict[str, Dict[str, Any]],
    sector_by_name: Dict[tuple[str, str], Dict[str, Any]],
    constituents: List[Dict[str, Any]],
    trade_date: str,
    scan_time: str,
    source: str,
) -> Dict[str, Dict[str, Any]]:
    constituent_by_code: Dict[str, List[Dict[str, Any]]] = {}
    for item in constituents:
        constituent_by_code.setdefault(_normalize_code(item.get("code")), []).append(item)
    result: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        code = _normalize_code(candidate.get("code"))
        if not code:
            continue
        legacy = _sector_evidence(candidate)
        profile = profiles.get(code) or {}
        industry = str(profile.get("industrySector") or legacy.get("industrySector") or "").strip()
        concepts = _unique_strings([*(profile.get("conceptSectors") or []), *(legacy.get("conceptSectors") or [])])
        matched: List[Dict[str, Any]] = []
        if industry and ("industry", industry) in sector_by_name:
            matched.append(_stock_sector_match(sector_by_name[("industry", industry)], constituent_by_code.get(code, [])))
        for concept in concepts:
            sector = sector_by_name.get(("concept", concept))
            if sector:
                matched.append(_stock_sector_match(sector, constituent_by_code.get(code, [])))
        status = "available" if matched else "partial" if industry or concepts else str(legacy.get("sectorEvidenceStatus") or "unavailable")
        primary = _primary_sector_match(matched)
        reasons = []
        if status == "available":
            reasons.append("AI 全市场独立行业/概念快照已匹配，板块状态可作为正反证据。")
        elif status == "partial":
            reasons.append("已识别行业/概念身份，但缺少同日板块强度或成分股覆盖，不能写成板块前排。")
        else:
            reasons.append("缺少真实行业/概念证据；上市板块只作为交易所板块。")
        evidence = {
            "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
            "status": status,
            "semanticLevel": "ai_full_market_sector_snapshot" if matched else "stock_sector_profile" if status == "partial" else "listing_board_proxy",
            "source": source,
            "tradeDate": trade_date,
            "asOfTime": scan_time,
            "listingBoardProxy": status != "available",
            "listingBoardIsIndustry": False,
            "matchedSectors": matched,
            "reasons": _unique_strings(reasons + list((legacy.get("sectorEvidence") or {}).get("reasons") or [])),
        }
        state = str(primary.get("sectorState") or "data_insufficient")
        result[code] = {
            "code": code,
            "name": candidate.get("name") or profile.get("name") or "",
            "listingBoard": legacy.get("listingBoard") or "",
            "industrySector": industry,
            "conceptSectors": concepts,
            "sectorEvidenceStatus": status,
            "sectorEvidence": evidence,
            "sectorState": state,
            "sectorRiskScore": primary.get("sectorRiskScore"),
            "sectorConfidence": primary.get("sectorConfidence"),
            "sectorFlowInterpretation": primary.get("flowInterpretation") or "",
            "industryRank": int(primary.get("rank") or 0) if primary.get("sectorType") == "industry" else 0,
            "industryLimitUpCount": int(primary.get("limitUpCount") or 0) if primary.get("sectorType") == "industry" else 0,
            "stockRankInIndustry": int(primary.get("stockRank") or 0) if primary.get("sectorType") == "industry" else 0,
            "conceptLimitUpCount": max([int(item.get("limitUpCount") or 0) for item in matched if item.get("sectorType") == "concept"] or [0]),
            "conceptLimitUpCounts": [
                {"sectorName": item.get("sectorName"), "limitUpCount": int(item.get("limitUpCount") or 0), "rank": int(item.get("rank") or 0)}
                for item in matched
                if item.get("sectorType") == "concept"
            ],
            "sectorPositiveEvidenceRefs": _unique_strings([ref for item in matched for ref in item.get("positiveEvidenceRefs") or []])[:6],
            "sectorCounterEvidenceRefs": _unique_strings([ref for item in matched for ref in item.get("counterEvidenceRefs") or []])[:6],
            "sectorMissingEvidence": [] if status == "available" else ["缺少完整同日板块强度/成分股覆盖证据。"],
            "sectorExceptionEligible": _sector_exception_eligible(candidate, primary, state),
        }
    return result


def _stock_sector_match(sector: Dict[str, Any], constituent_matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    row = dict(sector)
    matching_constituents = [
        item for item in constituent_matches if item.get("sectorType") == sector.get("sectorType") and item.get("sectorCode") == sector.get("sectorCode")
    ]
    if matching_constituents:
        row["stockRank"] = int(matching_constituents[0].get("stockRank") or 0)
    return row


def _primary_sector_match(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not matches:
        return {}
    industries = [item for item in matches if item.get("sectorType") == "industry"]
    if industries:
        return sorted(industries, key=lambda item: int(item.get("rank") or 999))[0]
    return sorted(matches, key=lambda item: (int(item.get("rank") or 999), -float(item.get("sectorConfidence") or 0)))[0]


def _sector_exception_eligible(candidate: Dict[str, Any], primary: Dict[str, Any], state: str) -> bool:
    if state not in {"retreat", "distribution", "weakening"}:
        return False
    candidate_types = set(candidate.get("candidateTypes") or [candidate.get("candidateType")])
    stock_rank = int(primary.get("stockRank") or 0)
    rel_strength = float(candidate.get("relStrength") or 0)
    amount_factor = float(candidate.get("amountFactor") or 0)
    return bool(
        stock_rank in {1, 2, 3}
        or rel_strength >= 84
        or amount_factor >= 1.8
        or candidate_types.intersection({"low_reversal", "oversold_repair", "rule_disliked_but_active"})
    )


def _apply_sector_context_to_candidates(
    candidates: List[Dict[str, Any]],
    sector_context: Dict[str, Any],
    selection_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    if not selection_config.get("sectorEvidenceEnabled", True):
        return candidates
    maps = sector_context.get("stockMaps") or {}
    style = sector_context.get("style") or {}
    result = []
    for candidate in candidates:
        code = _normalize_code(candidate.get("code"))
        sector_map = maps.get(code) or {}
        row = dict(candidate)
        row.update({key: value for key, value in sector_map.items() if key not in {"code", "name"}})
        row["styleRegimeFit"] = _style_regime_fit(row, style)
        if selection_config.get("styleRegimeEnabled"):
            row["styleRegimeAdjustment"] = _style_regime_adjustment(row["styleRegimeFit"])
            row["score"] = round(float(row.get("score") or 0) + float(row["styleRegimeAdjustment"] or 0), 2)
        row["evidenceRefs"] = _candidate_evidence_refs(row)
        result.append(row)
    return result


def _apply_sector_exposure_adjustment(candidate: Dict[str, Any], selection_config: Dict[str, Any]) -> Dict[str, Any]:
    if not selection_config.get("sectorExposureAdjustEnabled"):
        return dict(candidate)
    row = dict(candidate)
    state = str(row.get("sectorState") or "")
    candidate_types = set(row.get("candidateTypes") or [row.get("candidateType")])
    score = float(row.get("score") or 0)
    sector_scale = float((selection_config.get("sectorStateExposureScale") or {}).get(state, 1.0) or 1.0)
    adjustment = 0.0
    if state in {"retreat", "distribution"} and candidate_types.intersection({"strong_trend", "sector_leader", "volume_breakout", "high_attention_high_risk"}):
        adjustment -= 8.0 if not row.get("sectorExceptionEligible") else 3.0
    if state == "weakening" and candidate_types.intersection({"strong_trend", "volume_breakout"}):
        adjustment -= 4.0
    if state in {"rotation_in", "strong"} and candidate_types.intersection({"strong_trend", "sector_leader", "volume_breakout", "pullback_support", "low_reversal"}):
        adjustment += 4.0
    if state == "overheated" and candidate_types.intersection({"high_attention_high_risk", "volume_breakout"}):
        adjustment -= 3.0
    if abs(sector_scale - 1.0) >= 0.001:
        adjustment += (sector_scale - 1.0) * 8.0
    row["sectorExposureAdjustment"] = round(adjustment, 2)
    row["score"] = round(score + adjustment, 2)
    return row


def _quote_prefilter_daily_stock_pool(stocks: List[Any], pool_size: int) -> tuple[List[Any], Dict[str, Any]]:
    total = len(stocks)
    target = min(
        total,
        max(
            AI_FULL_MARKET_DAILY_PREFILTER_MIN,
            min(AI_FULL_MARKET_DAILY_PREFILTER_MAX, int(max(pool_size, 50) * 5)),
        ),
    )
    if total <= target:
        return stocks, {
            "enabled": False,
            "reason": "硬过滤后股票数未超过预筛上限，直接补日线。",
            "rawCount": total,
            "selectedCount": total,
            "targetCount": target,
        }

    selected: List[Any] = []
    seen: set[str] = set()

    def add_bucket(items: List[Any], limit: int) -> int:
        added = 0
        for stock in items:
            code = str(getattr(stock, "code", "") or "").zfill(6)
            if not code or code in seen:
                continue
            selected.append(stock)
            seen.add(code)
            added += 1
            if len(selected) >= target or added >= limit:
                break
        return added

    ranked = sorted(stocks, key=_quote_prefilter_score, reverse=True)
    bucket_counts = {
        "strong": add_bucket([item for item in ranked if 0 <= float(getattr(item, "change", 0) or 0) <= 8.8], max(80, int(target * 0.32))),
        "frontline": add_bucket([item for item in ranked if int(getattr(item, "sector_rank", 999) or 999) <= 5], max(50, int(target * 0.18))),
        "active_volume": add_bucket([item for item in ranked if float(getattr(item, "amount_factor", 0) or 0) >= 1.15 or float(getattr(item, "volume_ratio", 0) or 0) >= 1.25], max(70, int(target * 0.2))),
        "repair": add_bucket([item for item in ranked if -5.5 <= float(getattr(item, "change", 0) or 0) <= 2.5], max(60, int(target * 0.16))),
        "high_attention": add_bucket([item for item in ranked if str(getattr(item, "limit_status", "") or "") in {"near_limit_up", "limit_up"} or float(getattr(item, "change", 0) or 0) >= 7.0], max(35, int(target * 0.08))),
    }
    if len(selected) < target:
        bucket_counts["fill"] = add_bucket(ranked, target - len(selected))
    else:
        bucket_counts["fill"] = 0

    return selected, {
        "enabled": True,
        "method": "quote_layered_prefilter_v1",
        "rawCount": total,
        "selectedCount": len(selected),
        "targetCount": target,
        "reducedCount": total - len(selected),
        "bucketCounts": bucket_counts,
        "note": "仅 AI 全市场真实源冷缓存使用的宽预筛；正式候选仍需日线评分、板块证据和 DeepSeek 比较。",
    }


def _quote_prefilter_score(stock: Any) -> float:
    change = float(getattr(stock, "change", 0) or 0)
    amount_factor = float(getattr(stock, "amount_factor", 0) or 0)
    volume_ratio = float(getattr(stock, "volume_ratio", 0) or 0)
    rel_strength = float(getattr(stock, "rel_strength", 0) or 0)
    news_score = float(getattr(stock, "news_score", 0) or 0)
    sector_rank = int(getattr(stock, "sector_rank", 999) or 999)
    sector_limit_ups = int(getattr(stock, "sector_limit_ups", 0) or 0)
    limit_status = str(getattr(stock, "limit_status", "") or "")
    risk_count = len(getattr(stock, "risk_flags", []) or [])
    score = 0.0
    score += rel_strength * 0.34
    score += min(amount_factor, 4.0) * 11
    score += min(volume_ratio, 5.0) * 4
    score += news_score * 0.08
    score += max(0, 12 - sector_rank) * 1.8 if sector_rank > 0 else 0
    score += min(sector_limit_ups, 8) * 1.8
    if -3.5 <= change <= 7.8:
        score += 12
    elif 7.8 < change <= 9.6:
        score += 4
    elif -7.5 <= change < -3.5:
        score += 3
    else:
        score -= 8
    if limit_status in {"near_limit_up", "limit_up"}:
        score += 6
    score -= min(risk_count, 4) * 2.5
    return round(score, 4)


def _local_daily_kline_fallback(
    database: Database,
    codes: List[str],
    series_by_code: Dict[str, List[Dict[str, Any]]],
    requested_days: int,
) -> Dict[str, Any]:
    normalized = [str(code or "").strip().zfill(6) for code in codes if str(code or "").strip()]
    missing = [code for code in normalized if len(series_by_code.get(code) or []) < 40]
    if not missing:
        return {
            "enabled": False,
            "reason": "现场日线源已满足候选池评分。",
            "requestedCount": len(normalized),
            "missingBefore": 0,
            "addedCount": 0,
        }
    local = database.load_daily_kline_series(missing)
    added = 0
    latest_date = ""
    for code, series in local.items():
        clean = _sorted_kline_series(series)[-requested_days:]
        if len(clean) < 40:
            continue
        series_by_code[code] = clean
        added += 1
        if clean and str(clean[-1].get("date") or "") > latest_date:
            latest_date = str(clean[-1].get("date") or "")
    return {
        "enabled": True,
        "source": "database.daily_kline",
        "requestedCount": len(normalized),
        "missingBefore": len(missing),
        "addedCount": added,
        "missingAfter": max(0, len(missing) - added),
        "latestDate": latest_date,
        "dataQuality": "local_snapshot" if added else "unavailable",
        "reason": "现场日线源缺失或超时，已使用本地日线库快照补齐。" if added else "现场日线源缺失或超时，本地日线库也无可用快照。",
    }


def _resolve_daily_target_date(market: Dict[str, Any], fallback_latest_date: Any = "") -> str:
    candidates = [
        market.get("dailyDataDate"),
        fallback_latest_date,
    ]
    for key in ("tradeDate", "dataDate", "snapshotDate"):
        candidates.append(market.get(key))
    return _max_date(*candidates)


def _max_date(*values: Any) -> str:
    result = ""
    for value in values:
        text = str(value or "").strip()[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text) and text > result:
            result = text
    return result


def _latest_series_date(series: Optional[List[Dict[str, Any]]]) -> str:
    if not series:
        return ""
    latest = ""
    for point in series:
        date_text = str((point or {}).get("date") or "").strip()[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", date_text) and date_text > latest:
            latest = date_text
    return latest


def _latest_series_date_from_map(series_by_code: Dict[str, List[Dict[str, Any]]]) -> str:
    return _max_date(*(_latest_series_date(series) for series in (series_by_code or {}).values()))


def _daily_kline_series_is_fresh(series: Optional[List[Dict[str, Any]]], target_date: str) -> bool:
    if len(series or []) < 40:
        return False
    if not target_date:
        return True
    return _latest_series_date(series) >= target_date


def _daily_kline_missing_or_stale_codes(
    codes: List[str],
    series_by_code: Dict[str, List[Dict[str, Any]]],
    target_date: str,
) -> List[str]:
    result = []
    for code in codes:
        normalized = str(code or "").strip().zfill(6)
        if not normalized:
            continue
        if not _daily_kline_series_is_fresh(series_by_code.get(normalized), target_date):
            result.append(normalized)
    return result


def _daily_kline_coverage_meta(
    codes: List[str],
    series_by_code: Dict[str, List[Dict[str, Any]]],
    target_date: str,
    local_meta: Dict[str, Any],
    fetched_count: int,
    provider_warning: str = "",
) -> Dict[str, Any]:
    normalized = [str(code or "").strip().zfill(6) for code in codes if str(code or "").strip()]
    latest_by_code = {code: _latest_series_date(series_by_code.get(code)) for code in normalized}
    enough_count = sum(1 for code in normalized if len(series_by_code.get(code) or []) >= 40)
    fresh_count = sum(1 for code in normalized if _daily_kline_series_is_fresh(series_by_code.get(code), target_date))
    stale_count = sum(1 for code in normalized if len(series_by_code.get(code) or []) >= 40 and not _daily_kline_series_is_fresh(series_by_code.get(code), target_date))
    missing_count = max(0, len(normalized) - enough_count)
    coverage_pct = round(fresh_count / len(normalized) * 100, 2) if normalized else 0.0
    data_quality = "available" if fresh_count == len(normalized) else "partial" if fresh_count else "unavailable"
    reason = "日线数据已按目标交易日完成覆盖。"
    if data_quality == "partial":
        reason = "部分股票日线未能更新到目标交易日，已从正式候选评分中剔除。"
    elif data_quality == "unavailable":
        reason = "日线数据未能更新到目标交易日，本次不应生成正式推荐。"
    return {
        "enabled": True,
        "source": "database.daily_kline+external_daily_kline" if fetched_count else "database.daily_kline",
        "requestedCount": len(normalized),
        "targetDate": target_date,
        "latestDate": _max_date(*latest_by_code.values()),
        "localAddedCount": int(local_meta.get("addedCount") or 0),
        "externalFetchedCount": int(fetched_count or 0),
        "freshCount": fresh_count,
        "staleCount": stale_count,
        "missingCount": missing_count,
        "coveragePct": coverage_pct,
        "dataQuality": data_quality,
        "providerWarning": provider_warning,
        "reason": reason,
    }


def _emit_pool_progress(
    progress_callback: Optional[Any],
    stage: str,
    pct: int,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    if callable(progress_callback):
        progress_callback(stage, pct, message, extra or {})


def _merge_market_progress(
    market: Dict[str, Any],
    stage: str,
    message: str,
    progress_pct: int = 3,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    updated = dict(market or {})
    updated["buildPoolProgress"] = {
        **dict(updated.get("buildPoolProgress") or {}),
        "stage": stage,
        "message": message,
        "progressPct": max(0, min(95, int(progress_pct or 0))),
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        **(extra or {}),
    }
    return updated


def _candidate_evidence_refs(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    metrics = candidate.get("dailyMetrics") or {}
    refs = [
        {"id": "Q1", "type": "price_volume", "text": f"20日收益 {metrics.get('return20d', '--')}%，距MA20 {metrics.get('distanceMa20', '--')}%，5日量能 {metrics.get('amountRatio5d', '--')}。"},
        {"id": "Q2", "type": "relative_strength", "text": f"相对强度 {candidate.get('relStrength', '--')}，成交额因子 {candidate.get('amountFactor', '--')}，候选类型 {candidate.get('candidateTypeLabel', '--')}。"},
        {"id": "R1", "type": "risk", "text": f"风险标签：{'、'.join(candidate.get('riskFlags') or []) or '无明显规则风险'}。"},
    ]
    status = str(candidate.get("sectorEvidenceStatus") or "")
    if status == "available":
        seen_ids = {str(item.get("id") or "") for item in refs}
        sector_ref_count = 0
        for raw_text in [*(candidate.get("sectorPositiveEvidenceRefs") or []), *(candidate.get("sectorCounterEvidenceRefs") or [])]:
            text = str(raw_text or "").strip()
            if not text:
                continue
            match = re.match(r"^(S\d+)\s*[:：]\s*(.+)$", text)
            ref_id = match.group(1) if match else f"S{sector_ref_count + 1}"
            ref_text = match.group(2).strip() if match else text
            if ref_id in seen_ids:
                refs.append({"id": f"{ref_id}_{sector_ref_count + 1}", "type": "sector_context", "text": ref_text})
            else:
                refs.append({"id": ref_id, "type": "sector_context", "text": ref_text})
                seen_ids.add(ref_id)
            sector_ref_count += 1
        if sector_ref_count == 0:
            refs.append({"id": "S1", "type": "sector_context", "text": "板块证据存在，但需结合个股验证。"})
    else:
        refs.append({"id": "S0", "type": "sector_quality", "text": f"板块证据 {status or 'unavailable'}，不能输出行业龙头、主线前排或资金共振结论。"})
    return refs


def _style_regime_fit(candidate: Dict[str, Any], style: Dict[str, Any]) -> Dict[str, Any]:
    if not style or style.get("dataQuality") == "unavailable":
        return {"status": "data_insufficient", "matchedGroups": [], "message": "风格证据不足。"}
    sector_names = _unique_strings([candidate.get("industrySector"), *(candidate.get("conceptSectors") or [])])
    candidate_groups = _style_groups_for_sectors([{"sectorName": name} for name in sector_names])
    favored = set(style.get("favoredGroups") or [])
    risk = set(style.get("riskGroups") or [])
    matched_favored = sorted(set(candidate_groups).intersection(favored))
    matched_risk = sorted(set(candidate_groups).intersection(risk))
    if matched_favored and not matched_risk:
        status = "favored"
        message = "候选所属风格与当前优势风格一致。"
    elif matched_risk and not matched_favored:
        status = "risk"
        message = "候选所属风格落在当前风险风格组。"
    elif matched_favored and matched_risk:
        status = "mixed"
        message = "候选同时命中优势与风险风格，需回到个股证据比较。"
    else:
        status = "neutral"
        message = "候选未明显命中当前风格轮动摘要。"
    return {
        "status": status,
        "candidateGroups": candidate_groups,
        "matchedFavoredGroups": matched_favored,
        "matchedRiskGroups": matched_risk,
        "styleRegime": style.get("styleRegime") or "",
        "marketRisk": style.get("marketRisk") or "",
        "message": message,
    }


def _style_regime_adjustment(fit: Dict[str, Any]) -> float:
    status = str(fit.get("status") or "")
    if status == "favored":
        return 2.0
    if status == "risk":
        return -2.5
    if status == "mixed":
        return -0.5
    return 0.0


def _build_style_regime_snapshot(sectors: List[Dict[str, Any]], trade_date: str, scan_time: str, source: str) -> Dict[str, Any]:
    available = [item for item in sectors if item.get("sectorState") != "data_insufficient"]
    risk = [item for item in available if item.get("sectorState") in {"retreat", "distribution", "weakening"}]
    favored = [item for item in available if item.get("sectorState") in {"rotation_in", "strong"}]
    overheated = [item for item in available if item.get("sectorState") == "overheated"]
    risk_groups = _style_groups_for_sectors(risk + overheated)
    favored_groups = _style_groups_for_sectors(favored)
    if not available:
        market_risk = "unknown"
        regime = "data_insufficient"
        confidence = 0
    elif len(risk) >= max(4, len(available) * 0.22):
        market_risk = "high" if len(risk) >= max(8, len(available) * 0.35) else "medium"
        regime = "sector_retreat" if not favored else "sector_rotation"
        confidence = 66
    elif favored:
        market_risk = "medium" if overheated else "low"
        regime = "rotation_in" if risk else "sector_strength"
        confidence = 64
    else:
        market_risk = "medium"
        regime = "neutral"
        confidence = 52
    return {
        "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
        "marketRisk": market_risk,
        "styleRegime": regime,
        "riskGroups": risk_groups,
        "favoredGroups": favored_groups,
        "riskSectors": [_sector_brief(item) for item in sorted(risk + overheated, key=lambda row: float(row.get("sectorRiskScore") or 0), reverse=True)[:8]],
        "favoredSectors": [_sector_brief(item) for item in sorted(favored, key=lambda row: (float(row.get("sectorConfidence") or 0), -int(row.get("rank") or 999)), reverse=True)[:8]],
        "confidence": confidence,
        "evidenceRefs": ["风格状态由行业/概念 sectorState 聚合得出；未验证前只作为摘要或 Prompt 证据。"],
        "tradeDate": trade_date,
        "asOfTime": scan_time,
        "source": source,
        "dataQuality": "available" if available else "unavailable",
    }


def _style_groups_for_sectors(sectors: List[Dict[str, Any]]) -> List[str]:
    groups = []
    for sector in sectors:
        text = str(sector.get("sectorName") or "")
        if any(word in text for word in ("半导体", "芯片", "软件", "人工智能", "算力", "通信", "电子", "机器人")):
            groups.append("growth")
        if any(word in text for word in ("银行", "保险", "证券", "地产", "基建", "煤炭", "石油", "钢铁")):
            groups.append("value")
        if any(word in text for word in ("食品", "饮料", "医药", "中药", "医疗", "家电", "消费")):
            groups.append("consumer_healthcare")
        if any(word in text for word in ("电力", "公用", "高速", "高股息", "银行", "煤炭")):
            groups.append("defensive")
        if any(word in text for word in ("新能源", "锂电", "光伏", "风电", "电池", "汽车")):
            groups.append("new_energy")
        if any(word in text for word in ("军工", "航天", "船舶", "卫星")):
            groups.append("military")
    return _unique_strings(groups)


def _sector_brief(sector: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "sectorType": sector.get("sectorType"),
        "sectorCode": sector.get("sectorCode"),
        "sectorName": sector.get("sectorName"),
        "sectorState": sector.get("sectorState"),
        "rank": sector.get("rank"),
        "changePct": sector.get("changePct"),
        "upRatioPct": sector.get("upRatioPct"),
        "limitUpCount": sector.get("limitUpCount"),
        "turnoverAmount": sector.get("turnoverAmount"),
        "mainNetInflow": sector.get("mainNetInflow"),
        "fundFlowStatus": sector.get("fundFlowStatus"),
        "flowInterpretation": sector.get("flowInterpretation"),
        "sectorRiskScore": sector.get("sectorRiskScore"),
        "sectorConfidence": sector.get("sectorConfidence"),
        "dataQuality": sector.get("dataQuality") or {},
        "tradeDate": sector.get("tradeDate") or "",
        "asOfTime": sector.get("asOfTime") or "",
        "source": sector.get("source") or "",
    }


def _sector_context_summary(sectors: List[Dict[str, Any]], stock_maps: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    risk = [_sector_brief(item) for item in sectors if item.get("sectorState") in {"retreat", "distribution", "weakening"}][:8]
    favored = [_sector_brief(item) for item in sectors if item.get("sectorState") in {"rotation_in", "strong"}][:8]
    state_counts: Dict[str, int] = {}
    for item in sectors:
        state = str(item.get("sectorState") or "data_insufficient")
        state_counts[state] = state_counts.get(state, 0) + 1
    coverage = _rate(len([item for item in stock_maps.values() if item.get("sectorEvidenceStatus") == "available"]), len(stock_maps))
    return {
        "riskSectors": risk,
        "favoredSectors": favored,
        "sectorStateCounts": state_counts,
        "stockCoveragePct": coverage,
        "dataQuality": "available" if sectors else "unavailable",
        "message": f"行业/概念覆盖 {coverage if coverage is not None else 0}%，风险板块 {len(risk)}，优势板块 {len(favored)}。",
    }


def _compact_sector_context(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": context.get("version") or AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
        "meta": context.get("meta") or {},
        "summary": context.get("summary") or {},
        "style": context.get("style") or {},
        "sectors": [_sector_brief(item) for item in (context.get("sectors") or [])[:40]],
    }


def _shadow_pool_summary(base: List[Dict[str, Any]], shadow: List[Dict[str, Any]], selection_config: Dict[str, Any]) -> Dict[str, Any]:
    base_codes = [_normalize_code(item.get("code")) for item in base[:50]]
    shadow_codes = [_normalize_code(item.get("code")) for item in shadow[:50]]
    return {
        "enabled": bool(selection_config.get("sectorShadowEnabled", True)),
        "formalUsesSectorExposure": bool(selection_config.get("sectorExposureAdjustEnabled")),
        "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
        "baseTop50ChangedCount": len([code for code in shadow_codes[:50] if code and code not in set(base_codes[:50])]),
        "baseTypeCounts": _candidate_type_counts(base),
        "shadowTypeCounts": _candidate_type_counts(shadow),
    }


def _unique_tuples(values: List[tuple[str, str]]) -> List[tuple[str, str]]:
    result = []
    seen = set()
    for item in values:
        if not item[0] or not item[1] or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


def _validate_ai_full_market_picks_evidence(picks: List[Dict[str, Any]], candidates: List[Dict[str, Any]], stage: str) -> Dict[str, Any]:
    errors: List[str] = []
    by_code = {_normalize_code(item.get("code")): item for item in candidates if _normalize_code(item.get("code"))}
    if not isinstance(picks, list) or not picks:
        return {"version": AI_FULL_MARKET_AI_VALIDATION_VERSION, "valid": False, "status": "failed", "errors": ["AI 未返回 picks。"]}
    required_fields = [
        "strategyHypothesis",
        "supportingEvidenceRefs",
        "counterEvidenceRefs",
        "missingEvidence",
        "entryCondition",
        "invalidCondition",
        "marketFit",
        "sectorFit",
        "confidence",
        "confidenceReason",
    ]
    for index, pick in enumerate(picks, start=1):
        if not isinstance(pick, dict):
            errors.append(f"第 {index} 项不是对象。")
            continue
        code = _normalize_code(pick.get("code"))
        if not code or code not in by_code:
            errors.append(f"第 {index} 项 code={code or '--'} 不在候选池。")
            continue
        candidate = by_code[code]
        if stage == "final":
            for field in required_fields:
                value = pick.get(field)
                if value is None or value == "" or (field == "supportingEvidenceRefs" and isinstance(value, list) and not value):
                    errors.append(f"{code} 缺少必填字段 {field}。")
        allowed_refs = {str(item.get("id") or "") for item in candidate.get("evidenceRefs") or [] if item.get("id")}
        support_refs = _normalize_evidence_ref_values(pick.get("supportingEvidenceRefs"))
        counter_refs = _normalize_evidence_ref_values(pick.get("counterEvidenceRefs"))
        for ref in support_refs + counter_refs:
            if re.fullmatch(r"[A-Z]\d+", ref) and ref not in allowed_refs:
                errors.append(f"{code} 引用了不存在的证据ID {ref}。")
        sector_status = str(candidate.get("sectorEvidenceStatus") or "unavailable")
        sector_state = str(candidate.get("sectorState") or "data_insufficient")
        text = _ai_full_market_pick_text(pick)
        if sector_status != "available" and _has_positive_sector_claim(text):
            errors.append(f"{code} 板块证据为 {sector_status}，但输出了正向行业/概念强度结论。")
        if sector_state in {"retreat", "distribution", "weakening"} and _ordinary_trend_candidate(candidate):
            if not bool(pick.get("sectorExceptionUsed") or pick.get("sector_exception_used")):
                errors.append(f"{code} 来自 {sector_state} 板块且属于普通趋势/追涨候选，必须标记 sectorExceptionUsed。")
            if not str(pick.get("sectorExceptionReason") or pick.get("sector_exception_reason") or "").strip():
                errors.append(f"{code} 风险板块例外入选缺少 sectorExceptionReason。")
        unsupported = _unsupported_ai_full_market_sector_labels(text, candidate)
        if unsupported and sector_status != "available":
            errors.append(f"{code} 引用了输入中不存在或不可用的行业/概念名称：{','.join(unsupported[:4])}。")
    errors = _unique_strings(errors)
    return {
        "version": AI_FULL_MARKET_AI_VALIDATION_VERSION,
        "valid": not errors,
        "status": "passed" if not errors else "failed",
        "errors": errors[:20],
    }


def _attach_ai_full_market_validation_to_picks(picks: List[Dict[str, Any]], validation: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            **dict(pick),
            "aiEvidenceValidation": {
                "version": validation.get("version") or AI_FULL_MARKET_AI_VALIDATION_VERSION,
                "status": "passed" if validation.get("valid") else "failed",
                "errors": validation.get("errors") or [],
            },
        }
        for pick in picks
        if isinstance(pick, dict)
    ]


def _filter_ai_picks_with_validation_errors(picks: List[Dict[str, Any]], validation: Dict[str, Any]) -> List[Dict[str, Any]]:
    bad_codes = {
        match.group(1)
        for error in validation.get("errors") or []
        for match in [re.match(r"^(\d{6})\s", str(error or ""))]
        if match
    }
    if not bad_codes:
        return picks
    return [pick for pick in picks if _normalize_code(pick.get("code")) not in bad_codes]


def _normalize_evidence_ref_values(value: Any) -> List[str]:
    if isinstance(value, str):
        values = [item.strip() for item in re.split(r"[,，、\s;；]+", value) if item.strip()]
    elif isinstance(value, list):
        values = [str(item.get("id") if isinstance(item, dict) else item).strip() for item in value]
    else:
        values = []
    return _unique_strings(values)


def _ai_full_market_pick_text(pick: Dict[str, Any]) -> str:
    fields = [
        "reason",
        "strategyHypothesis",
        "risk",
        "focus",
        "entryCondition",
        "invalidCondition",
        "marketFit",
        "sectorFit",
        "sectorExceptionReason",
        "confidenceReason",
        "whyBetterThanSimilar",
    ]
    return "。".join(str(pick.get(field) or "") for field in fields)


def _has_positive_sector_claim(text: str) -> bool:
    pattern = r"(板块|行业|概念).{0,18}(涨停|强度|排名|前排|主线|共振|持续|领涨|发酵|回流|资金|轮入)"
    for sentence in re.split(r"[。；;，,\n]+", str(text or "")):
        for match in re.finditer(pattern, sentence):
            window = sentence[max(0, match.start() - 12) : min(len(sentence), match.end() + 12)]
            if any(word in window for word in ("不可用", "不能", "缺少", "缺乏", "缺失", "未能", "无法", "不足", "不完整", "不支持", "不依赖", "不作为", "仅")):
                continue
            return True
    return False


def _ordinary_trend_candidate(candidate: Dict[str, Any]) -> bool:
    types = set(candidate.get("candidateTypes") or [candidate.get("candidateType")])
    if types.intersection({"low_reversal", "oversold_repair", "rule_disliked_but_active"}):
        return False
    if candidate.get("sectorExceptionEligible"):
        return False
    return bool(types.intersection({"strong_trend", "sector_leader", "volume_breakout", "high_attention_high_risk"}))


def _unsupported_ai_full_market_sector_labels(text: str, candidate: Dict[str, Any]) -> List[str]:
    allowed = {
        str(candidate.get("listingBoard") or ""),
        str(candidate.get("name") or ""),
        str(candidate.get("sector") or ""),
        str(candidate.get("theme") or ""),
        str(candidate.get("industrySector") or ""),
        *[str(item or "") for item in candidate.get("conceptSectors") or []],
    }
    allowed_aliases = _sector_label_aliases(allowed)
    labels = []
    for match in re.finditer(r"([\u4e00-\u9fa5A-Za-z0-9]{2,12})(?:板块|行业|概念)", str(text or "")):
        label = _clean_ai_sector_label(match.group(1))
        window = text[max(0, match.start() - 12) : min(len(text), match.end() + 12)]
        if not label or label in {"市场", "行业", "概念", "板块", "上市", "真实", "所属", "该股", "个股", "股票", "股"}:
            continue
        if str(candidate.get("name") or "") and str(candidate.get("name") or "") in label:
            continue
        if _is_cautious_or_generic_sector_label(label, window):
            continue
        aliases = _sector_label_aliases(_split_ai_sector_label(label))
        if aliases and aliases.issubset(allowed_aliases):
            continue
        labels.append(label)
    return _unique_strings(labels)


def _ai_full_market_sector_number_errors(text: str, candidate: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    industry_count = int(candidate.get("industryLimitUpCount") or 0)
    concept_counts = [int(item.get("limitUpCount") or 0) for item in candidate.get("conceptLimitUpCounts") or []]
    for match in re.finditer(r"(\d+)\s*只\s*涨停", str(text or "")):
        count = int(match.group(1))
        allowed = {industry_count, *concept_counts}
        allowed = {value for value in allowed if value}
        if allowed and count not in allowed:
            errors.append(f"行业/概念涨停数 {count} 与输入不一致。")
    ranks = {
        int(candidate.get("industryRank") or 0),
        int(candidate.get("sectorRank") or 0),
        *[int(item.get("rank") or 0) for item in candidate.get("conceptLimitUpCounts") or []],
        int(candidate.get("stockRankInIndustry") or 0),
    }
    ranks = {value for value in ranks if value}
    for match in re.finditer(r"(?:板块|行业|概念|同类|个股板块内|成分股).{0,8}(?:排名|排第|第)\s*(\d+)", str(text or "")):
        rank = int(match.group(1))
        window = text[max(0, match.start() - 18) : min(len(text), match.end() + 18)]
        if ranks and rank not in ranks:
            errors.append(f"行业/概念/个股排名 {rank} 与输入不一致。")
    return _unique_strings(errors)


def _clean_ai_sector_label(label: str) -> str:
    text = str(label or "").strip("该个所属真实上市，。；;、 ")
    for prefix in ("行业", "概念", "板块", "所属", "作为", "相比其他", "优于其他", "适合", "受益于", "益于", "以及", "及", "且", "但", "虽", "若"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    for suffix in ("作为", "属于", "是", "为"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip("，。；;、 ")


def _split_ai_sector_label(label: str) -> List[str]:
    parts = [item.strip() for item in re.split(r"(?:以及|及|和|与|、|/|,|，)", str(label or "")) if item.strip()]
    return parts or [str(label or "").strip()]


def _sector_label_aliases(labels: Any) -> set[str]:
    values = [labels] if isinstance(labels, str) else [str(item or "") for item in labels or []]
    aliases: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", "", str(value or "").strip())
        if not text:
            continue
        candidates = {text}
        for suffix in ("概念", "板块", "行业"):
            if text.endswith(suffix):
                candidates.add(text[: -len(suffix)])
        aliases.update(item for item in candidates if item)
    return aliases


def _is_cautious_or_generic_sector_label(label: str, window: str) -> bool:
    cautious_words = ["无法", "不能", "未能", "缺少", "缺乏", "不足", "不构成", "不作为", "不可用", "不支持", "未确认", "仅"]
    generic_words = ["于", "若", "今日", "资金", "强度", "证据", "确认", "覆盖", "成分股", "数据", "正向", "负面", "涨幅", "支撑", "退潮", "轮入", "风险", "适合", "作为", "属于", "相比其他", "其他", "优于其他", "个股与", "个股", "股票", "市场", "防御性", "日线风险但", "7日有望随", "若市场转弱或", "若市场情绪转弱或", "若大盘转弱或"]
    technical_words = ["MA", "macd", "MACD", "KDJ", "量", "放量", "缩量", "突破", "跌破", "回踩", "企稳", "冲高", "回落", "均线", "前高", "低点", "趋势", "修复", "承接", "入场", "失效"]
    return any(word in window for word in cautious_words) or label in generic_words or any(word in label for word in technical_words)


def _normalize_profile(value: str) -> str:
    return "daily"


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _run_date(value: Any) -> str:
    parsed = _parse_datetime(value)
    return parsed.date().isoformat() if parsed else str(value or "")[:10]


def _run_sample_role(profile: str, scan_time: str, created_at: Any = None) -> str:
    return "standard"


def _run_sample_role_label(role: str) -> str:
    return {
        "temporary_observation": "临时观察",
        "standard": "标准推荐",
        "late_standard": "后补标准",
    }.get(str(role or ""), "未标记")


def _run_sample_role_from_run(run: Dict[str, Any]) -> str:
    market = run.get("market") or {}
    request = run.get("request") or {}
    role = str(market.get("runSampleRole") or request.get("runSampleRole") or "").strip()
    if role:
        return role
    return _run_sample_role(str(run.get("profile") or ""), str(run.get("scanTime") or ""), run.get("createdAt") or run.get("created_at"))


def _run_sample_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    market = _json_or_empty(row.get("market_json"))
    prompt_version = str(market.get("promptVersion") or AI_FULL_MARKET_PROMPT_VERSION)
    run = {
        "id": int(row.get("id") or 0),
        "profile": row.get("profile") or "",
        "riskMode": row.get("risk_mode") or "",
        "scanTime": row.get("scan_time") or "",
        "status": row.get("status") or "",
        "selectionMode": row.get("selection_mode") or "",
        "createdAt": row.get("created_at") or "",
        "finishedAt": row.get("finished_at") or "",
        "market": market,
    }
    role = _run_sample_role_from_run(run)
    return {
        "id": run["id"],
        "profile": run["profile"],
        "riskMode": run["riskMode"],
        "scanTime": run["scanTime"],
        "status": run["status"],
        "selectionMode": run["selectionMode"],
        "createdAt": run["createdAt"],
        "finishedAt": run["finishedAt"],
        "createdDate": _run_date(run["createdAt"]),
        "promptVersion": prompt_version,
        "runSampleRole": role,
        "runSampleRoleLabel": _run_sample_role_label(role),
    }


def _effective_learning_run_from_summaries(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    eligible = [
        row
        for row in rows
        if row.get("status") == "done"
        and row.get("selectionMode") == "ai_full_market"
        and row.get("runSampleRole") in {"standard", "late_standard"}
    ]
    if not eligible:
        return {}
    return sorted(eligible, key=lambda row: (str(row.get("createdAt") or ""), int(row.get("id") or 0)))[-1]


def _should_reuse_latest_run(latest: Dict[str, Any], desired_role: str) -> bool:
    latest_date = _run_date(latest.get("createdAt"))
    if latest_date != datetime.now().date().isoformat():
        return False
    latest_role = _run_sample_role_from_run(latest)
    if latest_role == "temporary_observation" and desired_role in {"standard", "late_standard"}:
        return False
    return True


def _json_or_empty(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _prompt_version(selection_config: Optional[Dict[str, Any]] = None) -> str:
    if isinstance(selection_config, dict):
        version = str(selection_config.get("promptVersion") or "").strip()
        if version:
            return version
    return AI_FULL_MARKET_PROMPT_VERSION


def _normalize_selection_config(value: Any) -> Dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_AI_FULL_MARKET_SELECTION_CONFIG, ensure_ascii=False))
    if not isinstance(value, dict):
        return config
    for key in ("featureId", "version", "mode", "promptVersion"):
        if key in value and value.get(key) is not None:
            config[key] = value.get(key)
    if str(config.get("promptVersion") or "") != AI_FULL_MARKET_PROMPT_VERSION:
        config["promptVersion"] = AI_FULL_MARKET_PROMPT_VERSION
    for key in ("minCompletedRuns", "minValidatedOutcomes", "minSelectedOutcomes"):
        if key in value:
            try:
                config[key] = max(0, int(value.get(key)))
            except (TypeError, ValueError):
                pass
    if "minImprovementPct" in value:
        try:
            config["minImprovementPct"] = float(value.get("minImprovementPct"))
        except (TypeError, ValueError):
            pass
    for key in ("sectorEvidenceEnabled", "sectorShadowEnabled", "sectorPromptEvidenceEnabled", "sectorExposureAdjustEnabled", "styleRegimeEnabled"):
        if key in value:
            config[key] = bool(value.get(key))
    for key, default, minimum, maximum in (
        ("maxSectorProfileStocks", 500, 50, 800),
        ("maxSectorConstituentSectors", 20, 0, 60),
    ):
        if key in value:
            try:
                config[key] = max(minimum, min(maximum, int(value.get(key))))
            except (TypeError, ValueError):
                config[key] = default
    type_scale = dict(config.get("typeExposureScale") or {})
    if isinstance(value.get("typeExposureScale"), dict):
        for candidate_type, scale in value.get("typeExposureScale", {}).items():
            try:
                type_scale[str(candidate_type)] = float(scale)
            except (TypeError, ValueError):
                continue
    config["typeExposureScale"] = {candidate_type: max(0.5, min(1.8, float(type_scale.get(candidate_type, 1.0) or 1.0))) for candidate_type in CANDIDATE_TYPES}
    sector_scale = dict(config.get("sectorStateExposureScale") or {})
    if isinstance(value.get("sectorStateExposureScale"), dict):
        for sector_state, scale in value.get("sectorStateExposureScale", {}).items():
            try:
                sector_scale[str(sector_state)] = float(scale)
            except (TypeError, ValueError):
                continue
    config["sectorStateExposureScale"] = {sector_state: max(0.5, min(1.8, float(sector_scale.get(sector_state, 1.0) or 1.0))) for sector_state in SECTOR_STATES}
    min_types = dict(config.get("semifinalMinTypeCounts") or {})
    if isinstance(value.get("semifinalMinTypeCounts"), dict):
        for candidate_type, minimum in value.get("semifinalMinTypeCounts", {}).items():
            try:
                min_types[str(candidate_type)] = max(0, int(minimum))
            except (TypeError, ValueError):
                continue
    config["semifinalMinTypeCounts"] = {candidate_type: int(min_types.get(candidate_type, SEMIFINAL_MIN_TYPES.get(candidate_type, 0))) for candidate_type in SEMIFINAL_MIN_TYPES}
    return config


def _merge_selection_config(current: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = _normalize_selection_config(current)
    if not isinstance(updates, dict):
        return merged
    for key in (
        "featureId",
        "version",
        "mode",
        "promptVersion",
        "minCompletedRuns",
        "minValidatedOutcomes",
        "minSelectedOutcomes",
        "minImprovementPct",
        "sectorEvidenceEnabled",
        "sectorShadowEnabled",
        "sectorPromptEvidenceEnabled",
        "sectorExposureAdjustEnabled",
        "styleRegimeEnabled",
        "maxSectorProfileStocks",
        "maxSectorConstituentSectors",
    ):
        if key in updates:
            merged[key] = updates[key]
    if isinstance(updates.get("typeExposureScale"), dict):
        scales = dict(merged.get("typeExposureScale") or {})
        for candidate_type, value in updates["typeExposureScale"].items():
            try:
                scales[str(candidate_type)] = float(value)
            except (TypeError, ValueError):
                continue
        merged["typeExposureScale"] = scales
    if isinstance(updates.get("sectorStateExposureScale"), dict):
        scales = dict(merged.get("sectorStateExposureScale") or {})
        for sector_state, value in updates["sectorStateExposureScale"].items():
            try:
                scales[str(sector_state)] = float(value)
            except (TypeError, ValueError):
                continue
        merged["sectorStateExposureScale"] = scales
    if isinstance(updates.get("semifinalMinTypeCounts"), dict):
        counts = dict(merged.get("semifinalMinTypeCounts") or {})
        for candidate_type, value in updates["semifinalMinTypeCounts"].items():
            try:
                counts[str(candidate_type)] = int(value)
            except (TypeError, ValueError):
                continue
        merged["semifinalMinTypeCounts"] = counts
    return _normalize_selection_config(merged)


def _selection_config_public_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    normalized = _normalize_selection_config(config)
    return {
        "featureId": normalized.get("featureId"),
        "version": normalized.get("version"),
        "mode": normalized.get("mode"),
        "promptVersion": normalized.get("promptVersion"),
        "minCompletedRuns": int(normalized.get("minCompletedRuns") or 0),
        "minValidatedOutcomes": int(normalized.get("minValidatedOutcomes") or 0),
        "minSelectedOutcomes": int(normalized.get("minSelectedOutcomes") or 0),
        "minImprovementPct": float(normalized.get("minImprovementPct") or 0),
        "sectorEvidenceEnabled": bool(normalized.get("sectorEvidenceEnabled")),
        "sectorShadowEnabled": bool(normalized.get("sectorShadowEnabled")),
        "sectorPromptEvidenceEnabled": bool(normalized.get("sectorPromptEvidenceEnabled")),
        "sectorExposureAdjustEnabled": bool(normalized.get("sectorExposureAdjustEnabled")),
        "styleRegimeEnabled": bool(normalized.get("styleRegimeEnabled")),
        "maxSectorProfileStocks": int(normalized.get("maxSectorProfileStocks") or 0),
        "maxSectorConstituentSectors": int(normalized.get("maxSectorConstituentSectors") or 0),
        "typeExposureScale": dict(normalized.get("typeExposureScale") or {}),
        "sectorStateExposureScale": dict(normalized.get("sectorStateExposureScale") or {}),
        "semifinalMinTypeCounts": dict(normalized.get("semifinalMinTypeCounts") or {}),
    }


def _selection_learning_meta(config: Dict[str, Any], report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized = _normalize_selection_config(config)
    scale_map = dict(normalized.get("typeExposureScale") or {})
    sector_scale_map = dict(normalized.get("sectorStateExposureScale") or {})
    learned_types = [
        {
            "candidateType": candidate_type,
            "label": _candidate_type_label(candidate_type),
            "scale": round(float(scale_map.get(candidate_type, 1.0) or 1.0), 3),
        }
        for candidate_type in CANDIDATE_TYPES
        if abs(float(scale_map.get(candidate_type, 1.0) or 1.0) - 1.0) >= 0.001
    ]
    learned_sector_states = [
        {
            "sectorState": sector_state,
            "label": _sector_state_label(sector_state),
            "scale": round(float(sector_scale_map.get(sector_state, 1.0) or 1.0), 3),
        }
        for sector_state in SECTOR_STATES
        if abs(float(sector_scale_map.get(sector_state, 1.0) or 1.0) - 1.0) >= 0.001
    ]
    suggestion = (report or {}).get("suggestion") or {}
    sample_status = suggestion.get("sampleStatus") or {}
    thresholds = suggestion.get("thresholds") or {
        "minCompletedRuns": int(normalized.get("minCompletedRuns") or 0),
        "minValidatedOutcomes": int(normalized.get("minValidatedOutcomes") or 0),
        "minSelectedOutcomes": int(normalized.get("minSelectedOutcomes") or 0),
        "minImprovementPct": float(normalized.get("minImprovementPct") or 0),
    }
    return {
        "method": "统计规则",
        "aiParticipates": False,
        "aiRole": "AI 负责当次候选比较、否决和 Top10 理由；参数学习由历史表现统计规则完成，不自动改 Prompt。",
        "configKey": AI_FULL_MARKET_SELECTION_CONFIG_KEY,
        "promptVersion": normalized.get("promptVersion"),
        "configVersion": normalized.get("version"),
        "hasLearnedParams": bool(learned_types or learned_sector_states),
        "learnedTypes": learned_types,
        "learnedSectorStates": learned_sector_states,
        "thresholds": thresholds,
        "sampleStatus": sample_status,
        "eligible": bool((report or {}).get("eligible") or suggestion.get("allowApply")),
        "reason": (report or {}).get("reason") or suggestion.get("reason") or "",
    }


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _multi_entry_pool(candidates: List[Dict[str, Any]], limit: int, profile: str) -> List[Dict[str, Any]]:
    buckets: List[List[Dict[str, Any]]] = []
    buckets.append(sorted(candidates, key=lambda item: float(item.get("score") or 0), reverse=True))
    buckets.append(sorted(candidates, key=lambda item: float(item.get("relStrength") or 0), reverse=True))
    buckets.append(sorted(candidates, key=lambda item: float(item.get("amountFactor") or 0), reverse=True))
    buckets.append(sorted(candidates, key=lambda item: -int(item.get("sectorRank") or 99)))
    if profile == "daily":
        buckets.append(sorted(candidates, key=lambda item: float((item.get("dailyMetrics") or {}).get("amountRatio5d") or 0), reverse=True))
        buckets.append([item for item in candidates if (item.get("dailyMetrics") or {}).get("breakout20d")])
        buckets.append([item for item in candidates if (item.get("dailyMetrics") or {}).get("pullbackMa20")])
    else:
        buckets.append([item for item in candidates if item.get("status") in {"buy-triggered", "pullback"}])
        buckets.append([item for item in candidates if item.get("style") in {"强势突破", "回踩低吸"}])
    selected: List[Dict[str, Any]] = []
    seen = set()
    cursor = 0
    while len(selected) < limit and any(cursor < len(bucket) for bucket in buckets):
        for bucket in buckets:
            if cursor >= len(bucket):
                continue
            item = bucket[cursor]
            code = item.get("code")
            if code and code not in seen:
                selected.append(item)
                seen.add(code)
                if len(selected) >= limit:
                    break
        cursor += 1
    return selected[:limit]


def _layered_candidate_pool(
    candidates: List[Dict[str, Any]],
    limit: int,
    profile: str,
    selection_config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    buckets: List[tuple[str, List[Dict[str, Any]]]] = []
    for candidate_type in CANDIDATE_TYPES:
        typed = [
            item
            for item in candidates
            if candidate_type in (item.get("candidateTypes") or [item.get("candidateType")])
        ]
        buckets.append((candidate_type, sorted(typed, key=lambda item: _layer_rank_key(item, candidate_type, profile), reverse=True)))
    buckets.append(("all", sorted(candidates, key=lambda item: _ai_pool_key(item, profile), reverse=True)))

    selected: List[Dict[str, Any]] = []
    seen = set()
    exposure_targets = _type_exposure_targets(limit, profile, selection_config)
    for candidate_type, bucket in buckets:
        target = exposure_targets.get(candidate_type, 0)
        if target <= 0:
            continue
        for item in bucket:
            code = item.get("code")
            if code and code not in seen:
                selected.append(item)
                seen.add(code)
                if len(selected) >= limit or _selected_type_count(selected, candidate_type) >= target:
                    break
            if len(selected) >= limit:
                break
        if len(selected) >= limit:
            return selected[:limit]

    cursor = 0
    while len(selected) < limit and any(cursor < len(bucket) for _, bucket in buckets):
        for _, bucket in buckets:
            if cursor >= len(bucket):
                continue
            item = bucket[cursor]
            code = item.get("code")
            if code and code not in seen:
                selected.append(item)
                seen.add(code)
                if len(selected) >= limit:
                    break
        cursor += 1
    return selected[:limit]


def _type_exposure_targets(limit: int, profile: str, selection_config: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    if limit <= 0:
        return {}
    scale_map = dict((selection_config or {}).get("typeExposureScale") or {})
    base = {
        "strong_trend": 0.12,
        "sector_leader": 0.12,
        "volume_breakout": 0.12,
        "pullback_support": 0.10,
        "low_reversal": 0.08,
        "oversold_repair": 0.08,
        "high_attention_high_risk": 0.06,
        "rule_disliked_but_active": 0.08,
    }
    targets = {}
    for candidate_type, ratio in base.items():
        scale = float(scale_map.get(candidate_type, 1.0) or 1.0)
        targets[candidate_type] = max(2, int(limit * ratio * max(0.5, min(1.8, scale))))
    return targets


def _selected_type_count(selected: List[Dict[str, Any]], candidate_type: str) -> int:
    return sum(1 for item in selected if candidate_type in (item.get("candidateTypes") or [item.get("candidateType")]))


def _build_market_environment_payload(market: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    breadth = market.get("breadth") or {}
    up = int(breadth.get("up") or 0)
    down = int(breadth.get("down") or 0)
    total = max(1, up + down)
    up_ratio = up / total if total else 0
    limit_up = int(market.get("limitUp") or 0)
    limit_down = int(market.get("limitDown") or 0)
    near_limit = len([item for item in candidates if str(item.get("limitStatus") or "") == "near_limit_up"])
    hard_limit = len([item for item in candidates if str(item.get("limitStatus") or "") in {"limit_up", "one_word_limit_up"}])
    broken_proxy_base = max(1, near_limit + hard_limit)
    broken_proxy_rate = round(near_limit / broken_proxy_base * 100, 1)
    mainlines = _derive_mainlines(candidates)
    leading_mainlines = [item for item in mainlines if item.get("strengthLevel") in {"strong", "active"}]

    if up_ratio >= 0.68:
        breadth_label = "普涨"
    elif up_ratio <= 0.35:
        breadth_label = "普跌"
    elif 0.45 <= up_ratio <= 0.58:
        breadth_label = "分化"
    else:
        breadth_label = "弱分化"

    if limit_down >= max(25, limit_up * 1.2) or up_ratio <= 0.32:
        emotion_phase = "退潮期"
    elif limit_up >= 50 and up_ratio >= 0.62 and broken_proxy_rate <= 45:
        emotion_phase = "情绪上升期"
    elif up_ratio >= 0.55 and limit_up >= 25:
        emotion_phase = "修复期"
    else:
        emotion_phase = "混沌分化期"

    if emotion_phase == "情绪上升期":
        index_state = "放量突破/强修复代理"
    elif emotion_phase == "修复期":
        index_state = "缩量反弹/修复代理"
    elif emotion_phase == "退潮期":
        index_state = "破位退潮代理"
    else:
        index_state = "分化震荡代理"

    environment = {
        "breadthLabel": breadth_label,
        "up": up,
        "down": down,
        "upRatioPct": round(up_ratio * 100, 1),
        "limitUp": limit_up,
        "limitDown": limit_down,
        "nearLimitUp": near_limit,
        "brokenLimitRateProxyPct": broken_proxy_rate,
        "emotionPhase": emotion_phase,
        "indexState": index_state,
        "mainlineStatus": "主线持续" if leading_mainlines else "主线不清",
        "mainlineCount": len(leading_mainlines),
        "dataQuality": _market_data_quality(market, mainlines),
        "summary": f"{breadth_label} · {emotion_phase} · {index_state}",
    }
    return {"environment": environment, "mainlines": mainlines[:8]}


def _derive_mainlines(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        label = _mainline_label(item)
        if not label:
            continue
        bucket = groups.setdefault(
            label,
            {"name": label, "count": 0, "change": [], "amountFactor": [], "limitUpCount": 0, "nearLimitCount": 0, "frontCount": 0, "leaders": []},
        )
        bucket["count"] += 1
        change = _safe_number(item.get("change"))
        if change is not None:
            bucket["change"].append(change)
        amount_factor = _safe_number(item.get("amountFactor"))
        if amount_factor is not None:
            bucket["amountFactor"].append(amount_factor)
        limit_status = str(item.get("limitStatus") or "")
        if limit_status in {"limit_up", "one_word_limit_up"}:
            bucket["limitUpCount"] += 1
        if limit_status == "near_limit_up":
            bucket["nearLimitCount"] += 1
        if int(item.get("sectorRank") or 99) <= 3:
            bucket["frontCount"] += 1
        bucket["leaders"].append(item)

    result = []
    for bucket in groups.values():
        avg_change = _average([float(value) for value in bucket.pop("change", [])])
        avg_amount = _average([float(value) for value in bucket.pop("amountFactor", [])])
        leaders = sorted(bucket.pop("leaders", []), key=lambda item: _ai_pool_key(item, "daily"), reverse=True)[:3]
        strength = (
            float(avg_change or 0) * 2
            + int(bucket.get("limitUpCount") or 0) * 8
            + int(bucket.get("nearLimitCount") or 0) * 3
            + float(avg_amount or 0) * 4
            + int(bucket.get("frontCount") or 0) * 2
            + min(10, int(bucket.get("count") or 0))
        )
        level = "strong" if strength >= 32 else "active" if strength >= 18 else "watch"
        result.append(
            {
                **bucket,
                "avgChange": avg_change,
                "avgAmountFactor": avg_amount,
                "strengthScore": round(strength, 2),
                "strengthLevel": level,
                "leaders": [
                    {
                        "code": leader.get("code"),
                        "name": leader.get("name"),
                        "change": leader.get("change"),
                        "score": leader.get("score"),
                        "sectorRank": leader.get("sectorRank"),
                    }
                    for leader in leaders
                ],
            }
        )
    return sorted(result, key=lambda item: float(item.get("strengthScore") or 0), reverse=True)


def _mainline_label(item: Dict[str, Any]) -> str:
    sector = str(item.get("sector") or "").strip()
    theme = str(item.get("theme") or "").strip()
    provider_theme = any(keyword in theme for keyword in ("实时行情", "日线行情", "AKShare", "新浪", "腾讯", "Tushare", "BaoStock"))
    if theme and not provider_theme and theme != sector:
        return theme[:18]
    if sector in _broad_listing_boards():
        return ""
    return sector[:18] if sector else ""


def _market_data_quality(market: Dict[str, Any], mainlines: List[Dict[str, Any]]) -> Dict[str, Any]:
    hot = [str(item) for item in (market.get("hotSectors") or []) if str(item).strip()]
    broad_names = _broad_listing_boards()
    board_only = bool(hot) and all(item in broad_names for item in hot[: min(3, len(hot))])
    return {
        "level": "board_proxy" if board_only else "sector_proxy",
        "message": "当前行情源只提供市场板块/交易板代理，主线持续性为候选池推断。" if board_only else "主线为行情源板块与候选池量价聚合推断。",
        "rawHotSectors": hot[:10],
        "derivedMainlineCount": len(mainlines),
    }


def _build_strategy_buckets(
    pool: List[Dict[str, Any]],
    semifinal: List[Dict[str, Any]],
    final_selected: List[Dict[str, Any]],
    rejected: List[Dict[str, Any]],
    profile: str,
) -> List[Dict[str, Any]]:
    candidates = _merge_strategy_candidates(pool, semifinal, final_selected, rejected)
    buckets = []
    for key, title, subtitle, types in STRATEGY_BUCKET_DEFS:
        matched = [item for item in candidates if _matches_strategy_bucket(item, key, types)]
        if key == "high_attention_high_risk":
            non_selected = [item for item in matched if not item.get("selected")]
            matched = non_selected or matched
        ranked = sorted(matched, key=lambda item: _strategy_bucket_score(item, key, profile), reverse=True)[:5]
        buckets.append(
            {
                "key": key,
                "title": title,
                "subtitle": subtitle,
                "items": [_strategy_bucket_row(item, key) for item in ranked],
            }
        )
    return buckets


def _merge_strategy_candidates(
    pool: List[Dict[str, Any]],
    semifinal: List[Dict[str, Any]],
    final_selected: List[Dict[str, Any]],
    rejected: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for stage, items in (("pool", pool), ("semifinal", semifinal), ("final_rejected", rejected), ("final", final_selected)):
        for item in items:
            code = str(item.get("code") or "").zfill(6)
            if not code:
                continue
            previous = merged.get(code, {})
            row = {**previous, **item}
            row["code"] = code
            row["stage"] = stage
            row["selected"] = stage == "final" or bool(item.get("selected") and stage == "final")
            row["candidateTypes"] = _candidate_types_for_item(row)
            merged[code] = row
    return list(merged.values())


def _candidate_types_for_item(item: Dict[str, Any]) -> List[str]:
    return _unique_strings(
        [
            *(item.get("candidateTypes") or []),
            item.get("candidateType"),
            item.get("candidateTypeUsed"),
            item.get("aiCandidateTypeUsed"),
        ]
    )


def _matches_strategy_bucket(item: Dict[str, Any], key: str, types: tuple[str, ...]) -> bool:
    item_types = _candidate_types_for_item(item)
    if key == "low_start":
        return "low_reversal" in item_types or _low_start_like(item)
    return any(candidate_type in item_types for candidate_type in types)


def _low_start_like(item: Dict[str, Any]) -> bool:
    metrics = item.get("dailyMetrics") or {}
    change = _safe_number(item.get("change"))
    return_20d = _safe_number(metrics.get("return20d"))
    distance_ma20 = _safe_number(metrics.get("distanceMa20"))
    amount_factor = _safe_number(metrics.get("amountRatio5d")) or _safe_number(item.get("amountFactor")) or _safe_number(item.get("volumeRatio")) or 0
    rel_strength = _safe_number(item.get("relStrength")) or 0
    low_position = (
        (distance_ma20 is not None and -10 <= distance_ma20 <= 8)
        or (return_20d is not None and return_20d <= 12)
        or (change is not None and -2 <= change <= 5)
    )
    return bool(low_position and amount_factor >= 1.05 and rel_strength >= 52)


def _strategy_bucket_score(item: Dict[str, Any], key: str, profile: str) -> float:
    selected_bonus = 10000 - int(item.get("aiRank") or 99) * 120 if item.get("selected") else 0
    stage_bonus = {"final": 5000, "semifinal": 2500, "final_rejected": 1600, "batch_rejected": 900, "pool": 0}.get(str(item.get("stage") or ""), 0)
    base = _layer_rank_key(item, _primary_bucket_type(item, key), profile)
    if key == "high_attention_high_risk":
        base += float(item.get("change") or 0) * 2 + float(item.get("amountFactor") or 0) * 8
    if key == "low_start":
        base += 300 if _low_start_like(item) else 0
    return selected_bonus + stage_bonus + base - min(9999, int(item.get("ruleRank") or 9999)) / 20


def _primary_bucket_type(item: Dict[str, Any], key: str) -> str:
    types = _candidate_types_for_item(item)
    for bucket_key, _, _, bucket_types in STRATEGY_BUCKET_DEFS:
        if bucket_key == key:
            for candidate_type in bucket_types:
                if candidate_type in types:
                    return candidate_type
            return bucket_types[0]
    return types[0] if types else "other"


def _strategy_bucket_row(item: Dict[str, Any], bucket_key: str) -> Dict[str, Any]:
    bucket_candidate_type = _primary_bucket_type(item, bucket_key)
    return {
        "bucketKey": bucket_key,
        "code": item.get("code"),
        "name": item.get("name"),
        "ruleRank": item.get("ruleRank") or item.get("rank") or 0,
        "aiRank": item.get("aiRank") or 0,
        "stage": item.get("stage") or "pool",
        "selected": bool(item.get("selected")),
        "score": item.get("score"),
        "confidence": item.get("confidence") or item.get("aiSelectionScore"),
        "candidateType": item.get("candidateType"),
        "candidateTypeUsed": bucket_candidate_type,
        "aiCandidateTypeUsed": item.get("candidateTypeUsed") or item.get("aiCandidateTypeUsed") or "",
        "candidateTypes": _candidate_types_for_item(item),
        "sector": item.get("sector"),
        "theme": item.get("theme"),
        "industrySector": item.get("industrySector"),
        "conceptSectors": item.get("conceptSectors") or [],
        "sectorEvidenceStatus": item.get("sectorEvidenceStatus"),
        "sectorState": item.get("sectorState"),
        "sectorRiskScore": item.get("sectorRiskScore"),
        "sectorExceptionEligible": bool(item.get("sectorExceptionEligible")),
        "sectorExceptionUsed": bool(item.get("sectorExceptionUsed")),
        "sectorExceptionReason": item.get("sectorExceptionReason") or item.get("aiSectorExceptionReason") or "",
        "change": item.get("change"),
        "volumeRatio": item.get("volumeRatio"),
        "amountFactor": item.get("amountFactor"),
        "sectorRank": item.get("sectorRank"),
        "sectorLimitUps": item.get("sectorLimitUps"),
        "riskFlags": item.get("riskFlags") or [],
        "tradeability": item.get("tradeability") or {},
        "dailyMetrics": item.get("dailyMetrics") or {},
        "reason": item.get("reason") or item.get("aiSelectionReason") or _bucket_reason(item, bucket_key),
        "risk": item.get("risk") or item.get("aiSelectionRisk") or _bucket_risk(item, bucket_key),
        "focus": item.get("focus") or item.get("aiSelectionFocus") or _bucket_focus(item, bucket_key),
        "entryCondition": item.get("entryCondition") or item.get("buyTrigger") or _bucket_entry_condition(item, bucket_key),
        "invalidCondition": item.get("invalidCondition") or _bucket_invalid_condition(item, bucket_key),
    }


def _bucket_reason(item: Dict[str, Any], bucket_key: str) -> str:
    label = _candidate_type_label(_primary_bucket_type(item, bucket_key))
    return f"{label}候选，分数 {round(float(item.get('score') or 0), 1)}，板块排名 {item.get('sectorRank') or '--'}，量能因子 {item.get('amountFactor') or '--'}。"


def _bucket_risk(item: Dict[str, Any], bucket_key: str) -> str:
    if bucket_key == "high_attention_high_risk":
        return "人气和波动都高，只适合观察触发条件，不适合无条件追高。"
    return "需要确认板块持续、量价不走弱，若跌破关键承接位应放弃。"


def _bucket_focus(item: Dict[str, Any], bucket_key: str) -> str:
    return {
        "strong_trend": "观察能否继续放量并保持板块前排。",
        "low_start": "观察首次放量后能否站稳关键均线或 VWAP。",
        "pullback_support": "观察回踩是否缩量、承接后能否重新转强。",
        "oversold_repair": "观察修复是否有持续量能，避免弱反抽。",
        "high_attention_high_risk": "观察是否炸板、冲高回落或板块退潮。",
    }.get(bucket_key, "观察量价和板块是否继续共振。")


def _bucket_entry_condition(item: Dict[str, Any], bucket_key: str) -> str:
    return {
        "strong_trend": "放量站稳日内均价线或突破早盘高点后再考虑。",
        "low_start": "低位放量后不回落，站稳 VWAP/MA20 才考虑。",
        "pullback_support": "回踩 VWAP/关键均线不破并重新放量转强。",
        "oversold_repair": "止跌后放量站回短期均线，不能只看急跌反弹。",
        "high_attention_high_risk": "只观察，不追高；必须等分歧后仍能承接。",
    }.get(bucket_key, "满足量价转强和板块持续后再考虑。")


def _bucket_invalid_condition(item: Dict[str, Any], bucket_key: str) -> str:
    return {
        "strong_trend": "跌破 VWAP/早盘突破位或板块前排退潮。",
        "low_start": "放量后冲高回落、跌回启动位或量能快速萎缩。",
        "pullback_support": "回踩跌破关键均线且无资金承接。",
        "oversold_repair": "继续破位或反弹无量，修复逻辑失效。",
        "high_attention_high_risk": "炸板、冲高回落、跌破 VWAP 或板块退潮。",
    }.get(bucket_key, "跌破关键承接位或板块退潮。")


def _rebalance_final_selection(
    selected: List[Dict[str, Any]],
    semifinal: List[Dict[str, Any]],
    limit: int,
    profile: str,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    current = _unique_by_code(selected)[:limit]
    replacements = []
    required_buckets = [
        ("low_start", ("low_reversal", "volume_breakout")),
        ("pullback_support", ("pullback_support",)),
        ("oversold_repair", ("oversold_repair",)),
    ]
    for bucket_key, types in required_buckets:
        if any(_matches_strategy_bucket(item, bucket_key, types) for item in current):
            continue
        alternatives = [
            item
            for item in semifinal
            if item.get("code") not in {row.get("code") for row in current}
            and _matches_strategy_bucket(item, bucket_key, types)
            and "high_attention_high_risk" not in _candidate_types_for_item(item)
        ]
        if not alternatives:
            continue
        alternative = sorted(alternatives, key=lambda item: _strategy_bucket_score(item, bucket_key, profile), reverse=True)[0]
        replace_index = _replaceable_final_index(current)
        if replace_index < 0:
            continue
        removed = current[replace_index]
        row = dict(alternative)
        row["selected"] = True
        row["selectionSource"] = "ai-full-market-rebalanced"
        row["candidateTypeUsed"] = _primary_bucket_type(row, bucket_key)
        row["reason"] = row.get("reason") or f"终选风格再平衡：补入{_candidate_type_label(row['candidateTypeUsed'])}代表候选，避免 Top10 过度集中。"
        row["risk"] = row.get("risk") or _bucket_risk(row, bucket_key)
        row["focus"] = row.get("focus") or _bucket_focus(row, bucket_key)
        row["entryCondition"] = row.get("entryCondition") or _bucket_entry_condition(row, bucket_key)
        row["invalidCondition"] = row.get("invalidCondition") or _bucket_invalid_condition(row, bucket_key)
        row["whyBetterThanSimilar"] = row.get("whyBetterThanSimilar") or "在同类型候选中量价、板块或位置证据更完整，用于补足最终观察组合的风格覆盖。"
        row["confidence"] = row.get("confidence") if row.get("confidence") is not None else min(78, max(55, float(row.get("score") or 0)))
        current[replace_index] = row
        replacements.append({"bucket": bucket_key, "added": row.get("code"), "removed": removed.get("code")})
    current_codes = {item.get("code") for item in current}
    for index, item in list(enumerate(current)):
        used_type = str(item.get("candidateTypeUsed") or item.get("aiCandidateTypeUsed") or item.get("candidateType") or "")
        if used_type not in {"high_attention_high_risk", "rule_disliked_but_active"}:
            continue
        alternatives = [
            candidate
            for candidate in semifinal
            if candidate.get("code") not in current_codes
            and not {"high_attention_high_risk", "rule_disliked_but_active"}.intersection(set(_candidate_types_for_item(candidate)))
        ]
        if not alternatives:
            continue
        alternative = sorted(alternatives, key=lambda row: _strategy_bucket_score(row, "strong_trend", profile), reverse=True)[0]
        row = dict(alternative)
        row["selected"] = True
        row["selectionSource"] = "ai-full-market-risk-guardrail"
        row["candidateTypeUsed"] = row.get("candidateType") or _primary_bucket_type(row, "strong_trend")
        row["reason"] = row.get("reason") or "终选风险护栏：高风险人气票移入观察榜，本名额保留给可操作性更好的候选。"
        row["risk"] = row.get("risk") or _bucket_risk(row, "strong_trend")
        row["focus"] = row.get("focus") or _bucket_focus(row, "strong_trend")
        row["entryCondition"] = row.get("entryCondition") or _bucket_entry_condition(row, "strong_trend")
        row["invalidCondition"] = row.get("invalidCondition") or _bucket_invalid_condition(row, "strong_trend")
        row["whyBetterThanSimilar"] = row.get("whyBetterThanSimilar") or "高风险人气票只保留在观察榜，最终 Top10 优先选择触发条件更清楚、回撤约束更明确的候选。"
        row["confidence"] = row.get("confidence") if row.get("confidence") is not None else min(78, max(55, float(row.get("score") or 0)))
        current[index] = row
        current_codes.discard(item.get("code"))
        current_codes.add(row.get("code"))
        replacements.append({"bucket": "risk_guardrail", "added": row.get("code"), "removed": item.get("code")})
    for rank, item in enumerate(current[:limit], start=1):
        item["aiRank"] = rank
    return current[:limit], {"enabled": True, "replacements": replacements}


def _replaceable_final_index(items: List[Dict[str, Any]]) -> int:
    if not items:
        return -1
    type_counts = _selected_type_counts_by_used_type(items)
    over_types = {candidate_type for candidate_type, count in type_counts.items() if count >= max(4, math.ceil(len(items) * 0.45))}
    candidates = []
    for index, item in enumerate(items):
        candidate_type = str(item.get("candidateTypeUsed") or item.get("candidateType") or "")
        penalty = 1000 if candidate_type in over_types or "strong_trend" in _candidate_types_for_item(item) else 0
        confidence = float(item.get("confidence") or item.get("aiSelectionScore") or item.get("score") or 0)
        candidates.append((penalty - confidence - int(item.get("aiRank") or 0), index))
    candidates.sort(reverse=True)
    return candidates[0][1]


def _annotate_candidate(candidate: Dict[str, Any], profile: str) -> Dict[str, Any]:
    item = dict(candidate)
    candidate_types = _candidate_types(item, profile)
    item["candidateTypes"] = candidate_types
    item["candidateType"] = candidate_types[0] if candidate_types else "other"
    item["candidateTypeLabel"] = _candidate_type_label(item["candidateType"])
    return item


def _candidate_types(candidate: Dict[str, Any], profile: str) -> List[str]:
    return _daily_candidate_types(candidate)


def _daily_candidate_types(candidate: Dict[str, Any]) -> List[str]:
    metrics = candidate.get("dailyMetrics") or {}
    trend = candidate.get("dailyTrend") or {}
    risk_flags = candidate.get("riskFlags") or []
    score = float(candidate.get("score") or 0)
    change = float(candidate.get("change") or 0)
    rel_strength = float(candidate.get("relStrength") or 0)
    amount_factor = float(candidate.get("amountFactor") or 0)
    sector_rank = int(candidate.get("sectorRank") or 99)
    return_5d = float(metrics.get("return5d") or 0)
    return_20d = float(metrics.get("return20d") or 0)
    distance_ma20 = float(metrics.get("distanceMa20") or 0)
    amount_ratio_5d = float(metrics.get("amountRatio5d") or 0)
    types: List[str] = []

    if trend.get("aboveMa20") and trend.get("aboveMa60") and trend.get("longTrendOk") and score >= 60:
        types.append("strong_trend")
    if sector_rank <= 3 or rel_strength >= 82:
        types.append("sector_leader")
    if metrics.get("breakout20d") or (amount_ratio_5d >= 1.35 and return_5d > 0):
        types.append("volume_breakout")
    if metrics.get("pullbackMa20") or (-3 <= distance_ma20 <= 5 and trend.get("aboveMa60")):
        types.append("pullback_support")
    if -8 <= distance_ma20 <= 4 and return_5d > 0 and amount_ratio_5d >= 1.05 and score < 65:
        types.append("low_reversal")
    if return_20d <= -8 and return_5d >= 0 and amount_ratio_5d >= 1.0:
        types.append("oversold_repair")
    if change >= 8 or amount_factor >= 1.8 or any("追高" in str(flag) or "高位" in str(flag) for flag in risk_flags):
        types.append("high_attention_high_risk")
    if candidate.get("status") == "daily-risk" or score < 55:
        if amount_ratio_5d >= 1.15 or rel_strength >= 75 or sector_rank <= 5 or change >= 3:
            types.append("rule_disliked_but_active")

    return types or ["other"]


def _layer_rank_key(candidate: Dict[str, Any], candidate_type: str, profile: str) -> float:
    base = _ai_pool_key(candidate, profile)
    metrics = candidate.get("dailyMetrics") or {}
    if candidate_type == "volume_breakout":
        return base + float(candidate.get("amountFactor") or 0) * 8 + float(metrics.get("amountRatio5d") or 0) * 6
    if candidate_type == "pullback_support":
        return base + (8 if profile == "daily" and metrics.get("pullbackMa20") else 0)
    if candidate_type == "low_reversal":
        return base - float(candidate.get("score") or 0) * 0.25 + float(candidate.get("relStrength") or 0) * 0.2
    if candidate_type == "oversold_repair":
        return base - abs(float(metrics.get("return20d") or 0)) * 0.15
    if candidate_type == "rule_disliked_but_active":
        return base - float(candidate.get("score") or 0) * 0.15 + float(candidate.get("amountFactor") or 0) * 10
    return base


def _ai_pool_key(candidate: Dict[str, Any], profile: str) -> float:
    dimensions = candidate.get("dimensions") or {}
    risk_penalty = float(dimensions.get("riskPenalty") or 0)
    sector_rank = int(candidate.get("sectorRank") or 99)
    key = (
        float(candidate.get("score") or 0)
        + float(candidate.get("relStrength") or 0) * 0.25
        + float(candidate.get("amountFactor") or 0) * 8
        + float(candidate.get("volumeRatio") or 0) * 3
        + max(0, 8 - sector_rank) * 4
        - risk_penalty * 0.7
    )
    if profile == "daily":
        metrics = candidate.get("dailyMetrics") or {}
        key += float(metrics.get("amountRatio5d") or 0) * 5
        key += 6 if metrics.get("breakout20d") else 0
        key += 5 if metrics.get("pullbackMa20") else 0
    else:
        key += 8 if candidate.get("style") in {"强势突破", "回踩低吸"} else 0
        key += 5 if candidate.get("status") in {"buy-triggered", "pullback"} else 0
    return key


def _candidate_type_counts(candidates: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for candidate in candidates:
        for candidate_type in candidate.get("candidateTypes") or [candidate.get("candidateType") or "other"]:
            counts[candidate_type] = counts.get(candidate_type, 0) + 1
    return counts


def _selected_type_counts_by_used_type(candidates: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for candidate in candidates:
        candidate_type = str(
            candidate.get("candidateTypeUsed")
            or candidate.get("aiCandidateTypeUsed")
            or candidate.get("candidateType")
            or "other"
        )
        counts[candidate_type] = counts.get(candidate_type, 0) + 1
    return counts


def _candidate_type_label(candidate_type: str) -> str:
    return {
        "strong_trend": "强趋势延续",
        "sector_leader": "板块前排",
        "volume_breakout": "放量启动",
        "pullback_support": "回踩承接",
        "low_reversal": "低位启动",
        "oversold_repair": "超跌修复",
        "high_attention_high_risk": "高人气高风险",
        "rule_disliked_but_active": "规则不看好但异动明显",
        "other": "其他候选",
    }.get(candidate_type, candidate_type or "其他候选")


def _sector_state_label(sector_state: str) -> str:
    return {
        "rotation_in": "主动轮入",
        "strong": "强势",
        "overheated": "过热",
        "neutral": "中性",
        "weakening": "转弱",
        "distribution": "派发",
        "retreat": "退潮",
        "oversold_repair": "超跌修复",
        "data_insufficient": "板块不足",
    }.get(sector_state, sector_state or "未知")


def _pool_item(candidate: Dict[str, Any], rule_rank: int) -> Dict[str, Any]:
    item = dict(candidate)
    item["ruleRank"] = rule_rank
    item["rank"] = rule_rank
    for key in ("series", "macdSeries", "vwapSeries", "ma5", "ma10", "ma20", "ma60"):
        item.pop(key, None)
    return item


def _chunks(values: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _batch_pick_for_size(candidate_count: int, batch_size: int, batch_pick: int) -> int:
    if candidate_count <= 0:
        return 0
    if batch_size <= 0:
        return min(candidate_count, batch_pick)
    scaled = int(candidate_count * batch_pick / batch_size + 0.5)
    return min(candidate_count, batch_pick, max(2, scaled))


def _unique_by_code(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        code = item.get("code")
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(item)
    return result


def _unique_strings(values: List[Any]) -> List[str]:
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


def _normalize_text_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return _unique_strings(value)[:8]
    text = str(value or "").strip()
    return [text] if text else []


def _apply_picks(
    candidates: List[Dict[str, Any]],
    picks: List[Dict[str, Any]],
    limit: int,
    batch_index: int,
) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
    by_code = {item.get("code"): item for item in candidates}
    selected = []
    seen = set()
    stats = {
        "invalidPickCount": 0,
        "outOfPoolPickCount": 0,
        "duplicatePickCount": 0,
    }
    for rank, pick in enumerate(sorted(picks, key=lambda item: _safe_rank(item.get("rank"))), start=1):
        code = _normalize_code(pick.get("code"))
        if not code:
            stats["invalidPickCount"] += 1
            continue
        if code in seen:
            stats["duplicatePickCount"] += 1
            continue
        if code not in by_code:
            stats["outOfPoolPickCount"] += 1
            continue
        base = dict(by_code[code])
        base["aiRank"] = rank
        base["batchIndex"] = batch_index
        base["selected"] = True
        base["reason"] = str(pick.get("reason") or pick.get("入选理由") or "")
        base["risk"] = str(pick.get("risk") or pick.get("风险") or "")
        base["focus"] = str(pick.get("focus") or pick.get("确认点") or "")
        base["entryCondition"] = str(pick.get("entryCondition") or pick.get("entry_condition") or pick.get("入手条件") or pick.get("buyCondition") or "")
        base["invalidCondition"] = str(pick.get("invalidCondition") or pick.get("invalid_condition") or pick.get("失效条件") or base.get("invalidCondition") or "")
        base["candidateTypeUsed"] = str(pick.get("candidateTypeUsed") or pick.get("candidate_type_used") or base.get("candidateType") or "")
        base["strategyHypothesis"] = str(pick.get("strategyHypothesis") or pick.get("strategy_hypothesis") or "")
        base["supportingEvidenceRefs"] = _normalize_text_list(pick.get("supportingEvidenceRefs") or pick.get("supporting_evidence_refs"))
        base["counterEvidenceRefs"] = _normalize_text_list(pick.get("counterEvidenceRefs") or pick.get("counter_evidence_refs"))
        base["missingEvidence"] = _normalize_text_list(pick.get("missingEvidence") or pick.get("missing_evidence"))
        base["marketFit"] = str(pick.get("marketFit") or pick.get("market_fit") or "")
        base["sectorFit"] = str(pick.get("sectorFit") or pick.get("sector_fit") or "")
        base["sectorExceptionUsed"] = bool(pick.get("sectorExceptionUsed") or pick.get("sector_exception_used"))
        base["sectorExceptionReason"] = str(pick.get("sectorExceptionReason") or pick.get("sector_exception_reason") or "")
        base["whyBetterThanSimilar"] = str(pick.get("whyBetterThanSimilar") or pick.get("why_better_than_similar") or "")
        base["confidence"] = _normalize_confidence(pick.get("confidence") if pick.get("confidence") is not None else pick.get("score"))
        base["confidenceReason"] = str(pick.get("confidenceReason") or pick.get("confidence_reason") or "")
        base["aiSelected"] = True
        base["selectionSource"] = "ai-full-market"
        base["aiSelectionReason"] = base["reason"]
        base["aiSelectionRisk"] = base["risk"]
        base["aiSelectionFocus"] = base["focus"]
        base["aiCandidateTypeUsed"] = base["candidateTypeUsed"]
        base["aiWhyBetterThanSimilar"] = base["whyBetterThanSimilar"]
        base["aiConfidenceReason"] = base["confidenceReason"]
        base["aiSectorExceptionReason"] = base["sectorExceptionReason"]
        base["aiEvidenceValidation"] = pick.get("aiEvidenceValidation") or {}
        if base["confidence"] is not None:
            base["aiSelectionScore"] = base["confidence"]
        selected.append(base)
        seen.add(code)
        if len(selected) >= limit:
            break
    return selected, stats


def _rejected_candidates(
    candidates: List[Dict[str, Any]],
    selected: List[Dict[str, Any]],
    stage: str,
    batch_index: int = 0,
) -> List[Dict[str, Any]]:
    selected_codes = {str(item.get("code") or "") for item in selected if item.get("code")}
    rejected: List[Dict[str, Any]] = []
    for item in candidates:
        code = str(item.get("code") or "")
        if not code or code in selected_codes:
            continue
        row = dict(item)
        row["batchIndex"] = batch_index
        row["selected"] = False
        row["aiSelected"] = False
        row["selectionSource"] = stage
        row["reason"] = row.get("reason") or _rejected_reason(row, stage)
        row["risk"] = row.get("risk") or _rejected_risk(row, stage)
        row["focus"] = row.get("focus") or _rejected_focus(row, stage)
        rejected.append(row)
    return rejected


def _rejected_reason(item: Dict[str, Any], stage: str) -> str:
    candidate_type = item.get("candidateType") or "other"
    label = _candidate_type_label(str(candidate_type))
    if stage == "final_rejected":
        return f"已进入终选比较，但未进入 Top10：{label} 的风险收益比不及入选项。"
    return f"批次未入选：{label} 暂未进入半决赛。"


def _rejected_risk(item: Dict[str, Any], stage: str) -> str:
    if stage == "final_rejected":
        return "已进入终选比较但未入选，说明同类里确定性或位置优势不够。"
    return "未进入批次半决赛，后续可观察是否出现更强量价或板块证据。"


def _rejected_focus(item: Dict[str, Any], stage: str) -> str:
    if stage == "final_rejected":
        return "观察后续是否重新站上关键位、放量或板块继续加强。"
    return "等待更明确的量价共振或板块联动再复核。"


def _rejected_stats(
    candidates: List[Dict[str, Any]],
    selected: List[Dict[str, Any]],
    rejected: List[Dict[str, Any]],
) -> Dict[str, Any]:
    selected_codes = {str(item.get("code") or "").zfill(6) for item in selected if item.get("code")}
    total_rejected = max(0, len([item for item in candidates if str(item.get("code") or "").zfill(6) not in selected_codes]))
    return {
        "candidateCount": len(candidates),
        "selectedCount": len(selected),
        "rejectedCount": len(rejected),
        "totalRejectedCount": total_rejected,
        "finalRejectedCount": len(rejected),
        "batchRejectedCount": max(0, total_rejected - len(rejected)),
        "selectedTypeCounts": _candidate_type_counts(selected),
        "rejectedTypeCounts": _candidate_type_counts(rejected),
    }


def _rules_fallback(
    candidates: List[Dict[str, Any]],
    limit: int,
    reason: str = "AI 全市场选股失败，按规则分兜底。",
    risk: str = "该结果不是 AI 全市场选择。",
    focus: str = "等待 AI 全市场任务成功后再确认。",
    selection_source: str = "rules-fallback",
    batch_index: int = 0,
) -> List[Dict[str, Any]]:
    ranked = sorted(candidates, key=lambda item: float(item.get("score") or 0), reverse=True)
    result = []
    for rank, item in enumerate(ranked[:limit], start=1):
        row = dict(item)
        row["aiRank"] = rank
        row["batchIndex"] = batch_index
        row["selected"] = True
        row["selectionSource"] = selection_source
        row["aiSelected"] = False
        row["candidateTypeUsed"] = row.get("candidateType") or ""
        row["whyBetterThanSimilar"] = "规则兜底结果未经过 AI 同类比较。"
        row["reason"] = reason
        row["risk"] = risk
        row["focus"] = focus
        result.append(row)
    return result


def _ai_retry_delay(attempt: int) -> float:
    return min(5.0, AI_FULL_MARKET_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))


def _compact_error(error: Exception) -> str:
    text = str(error).strip()
    if not text:
        text = error.__class__.__name__
    return text[:240]


def _semifinal_supplements(
    pool: List[Dict[str, Any]],
    semifinal: List[Dict[str, Any]],
    profile: str,
    selection_config: Optional[Dict[str, Any]] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    existing_codes = {item.get("code") for item in semifinal if item.get("code")}
    supplements: List[Dict[str, Any]] = []
    added_by_type: Dict[str, int] = {}
    before_counts = _candidate_type_counts(semifinal)
    min_type_counts = dict((selection_config or {}).get("semifinalMinTypeCounts") or SEMIFINAL_MIN_TYPES)
    for candidate_type, minimum in min_type_counts.items():
        current = int(before_counts.get(candidate_type) or 0) + int(added_by_type.get(candidate_type) or 0)
        need = max(0, minimum - current)
        if need <= 0:
            continue
        typed = [
            item
            for item in pool
            if item.get("code") not in existing_codes
            and candidate_type in (item.get("candidateTypes") or [item.get("candidateType")])
        ]
        typed = sorted(typed, key=lambda item: _layer_rank_key(item, candidate_type, profile), reverse=True)
        for item in typed[:need]:
            row = dict(item)
            row["batchIndex"] = 0
            row["selected"] = True
            row["aiSelected"] = False
            row["selectionSource"] = "semifinal-supplement"
            row["candidateTypeUsed"] = candidate_type
            row["reason"] = f"半决赛类型补位：{_candidate_type_label(candidate_type)} 代表候选，供终选统一比较。"
            row["risk"] = "补位只扩大终选比较范围，不代表 AI 已最终推荐。"
            row["focus"] = "等待终选阶段与其他候选横向比较。"
            supplements.append(row)
            existing_codes.add(row.get("code"))
            added_by_type[candidate_type] = added_by_type.get(candidate_type, 0) + 1
    return supplements, {
        "enabled": True,
        "addedCount": len(supplements),
        "addedByType": added_by_type,
        "beforeTypeCounts": before_counts,
        "afterTypeCounts": _candidate_type_counts(_unique_by_code(semifinal + supplements)),
    }


def _ai_call_summary(
    batch_summary: List[Dict[str, Any]],
    final_summary: Dict[str, Any],
    prompt_version: str = AI_FULL_MARKET_PROMPT_VERSION,
) -> Dict[str, Any]:
    calls = list(batch_summary)
    if final_summary:
        calls.append(final_summary)
    elapsed_values = [int(item.get("elapsedMs") or 0) for item in calls if int(item.get("elapsedMs") or 0) > 0]
    return {
        "promptVersion": prompt_version,
        "batchCallCount": len(batch_summary),
        "finalCallCount": 1 if final_summary else 0,
        "failedCallCount": sum(1 for item in calls if item.get("error")),
        "emptyPickCount": sum(1 for item in calls if int(item.get("pickCount") or 0) == 0),
        "invalidPickCount": sum(int(item.get("invalidPickCount") or 0) for item in calls),
        "outOfPoolPickCount": sum(int(item.get("outOfPoolPickCount") or 0) for item in calls),
        "duplicatePickCount": sum(int(item.get("duplicatePickCount") or 0) for item in calls),
        "avgElapsedMs": round(sum(elapsed_values) / len(elapsed_values)) if elapsed_values else 0,
    }


def _attach_run_diagnostics(run: Dict[str, Any], items: List[Dict[str, Any]]) -> None:
    market = run.get("market") or {}
    request = run.get("request") or {}
    selection_config = _normalize_selection_config(market.get("selectionConfig") or request.get("selectionConfig") or {})
    pool = [(item.get("item") or item) for item in items]
    final_rows = [_merge_item_payload(item) for item in items if item.get("stage") == "final"]
    rejected_rows = [_merge_item_payload(item) for item in items if item.get("stage") != "final"]
    if not market.get("environment"):
        market.update(_build_market_environment_payload(market, pool))
    market["strategyBuckets"] = _build_strategy_buckets(pool, [], final_rows, rejected_rows, run.get("profile") or "daily")
    run["market"] = _compact_market(market)
    run["marketEnvironment"] = market.get("environment") or {}
    run["mainlines"] = market.get("mainlines") or []
    run["strategyBuckets"] = market.get("strategyBuckets") or []
    run["promptVersion"] = market.get("promptVersion") or AI_FULL_MARKET_PROMPT_VERSION
    run["selectionConfig"] = _selection_config_public_snapshot(selection_config)
    run["learningMeta"] = market.get("learningMeta") or _selection_learning_meta(selection_config)
    run["poolTypeCounts"] = market.get("poolTypeCounts") or market.get("candidateTypeCounts") or _items_type_counts(items, None)
    run["semifinalTypeCounts"] = market.get("semifinalTypeCounts") or _items_type_counts(items, {"semifinal", "final"})
    run["finalTypeCounts"] = _items_used_type_counts(items, {"final"}) or market.get("finalTypeCounts") or _items_type_counts(items, {"final"})
    run["aiCallSummary"] = market.get("aiCallSummary") or _ai_call_summary(run.get("batchSummary") or [], market.get("finalCallSummary") or {})
    run["semifinalSupplement"] = market.get("semifinalSupplement") or {"enabled": False, "addedCount": 0, "addedByType": {}}
    run["actualScanTime"] = run.get("scanTime") or market.get("scanTime") or ""


def _merge_item_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    payload = item.get("item") or {}
    pick = item.get("pick") or {}
    return {**payload, **pick, **{key: value for key, value in item.items() if key not in {"item", "pick"}}}


def _items_type_counts(items: List[Dict[str, Any]], stages: Optional[set[str]]) -> Dict[str, int]:
    values = []
    for item in items:
        if stages is not None and item.get("stage") not in stages:
            continue
        payload = item.get("item") or item
        values.append(payload)
    return _candidate_type_counts(values)


def _items_used_type_counts(items: List[Dict[str, Any]], stages: Optional[set[str]]) -> Dict[str, int]:
    values = []
    for item in items:
        if stages is not None and item.get("stage") not in stages:
            continue
        values.append(
            {
                "candidateTypeUsed": item.get("candidateTypeUsed")
                or (item.get("pick") or {}).get("candidateTypeUsed")
                or (item.get("pick") or {}).get("aiCandidateTypeUsed")
                or (item.get("item") or {}).get("candidateType")
            }
        )
    return _selected_type_counts_by_used_type(values)


def _prompt_payload(
    candidates: List[Dict[str, Any]],
    market: Dict[str, Any],
    limit: int,
    profile: str,
    stage: str,
    selection_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    config = _normalize_selection_config(selection_config or {})
    market_payload = _compact_market(market)
    if not config.get("styleRegimeEnabled"):
        market_payload.pop("styleRegime", None)
    return {
        "market": market_payload,
        "task": {
            "profile": profile,
            "stage": stage,
            "limit": limit,
            "selectionMode": "ai_full_market",
            "promptVersion": _prompt_version(selection_config),
            "targetHorizon": "1-7 个交易日",
            "dataGrain": "daily",
            "instruction": f"从 candidates 中选择最值得进入未来 1-7 个交易日重点观察名单的股票，最多 Top{limit}，证据不足可以少于 {limit}。规则分、daily-risk、invalid 只是证据，不是绝对淘汰条件。",
        },
        "candidates": [_compact_candidate_for_prompt(item, profile, selection_config) for item in candidates],
    }


def _compact_candidate_for_prompt(candidate: Dict[str, Any], profile: str, selection_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    data = _compact_candidate(candidate, profile)
    config = _normalize_selection_config(selection_config or {})
    if not config.get("styleRegimeEnabled"):
        data.pop("styleRegimeFit", None)
        data.pop("styleRegimeAdjustment", None)
    if config.get("sectorPromptEvidenceEnabled", True):
        return data
    for key in (
        "sectorState",
        "sectorRiskScore",
        "sectorConfidence",
        "sectorFlowInterpretation",
        "industryRank",
        "industryLimitUpCount",
        "stockRankInIndustry",
        "conceptLimitUpCount",
        "conceptLimitUpCounts",
        "sectorPositiveEvidenceRefs",
        "sectorCounterEvidenceRefs",
        "sectorExceptionEligible",
        "sectorExposureAdjustment",
    ):
        data.pop(key, None)
    data["sectorMissingEvidence"] = _unique_strings(list(data.get("sectorMissingEvidence") or []) + ["板块强弱证据开关关闭，本次 Prompt 不使用 sectorState。"])
    data["evidenceRefs"] = [item for item in data.get("evidenceRefs") or [] if str(item.get("id") or "") not in {"S1", "S2"}]
    return data


def _user_prompt(
    candidates: List[Dict[str, Any]],
    market: Dict[str, Any],
    limit: int,
    profile: str,
    stage: str,
    selection_config: Optional[Dict[str, Any]] = None,
) -> str:
    if stage == "final":
        instruction = _final_prompt(limit)
    else:
        instruction = _daily_batch_prompt(limit)
    return (
        f"{instruction}\n\n"
        "下面是结构化输入。请只根据 JSON 中的 market、task、candidates 判断。\n"
        f"{json.dumps(_prompt_payload(candidates, market, limit, profile, stage, selection_config), ensure_ascii=False)}"
    )


def _compact_market(market: Dict[str, Any]) -> Dict[str, Any]:
    breadth = market.get("breadth") or {}
    return {
        "breadth": {"up": breadth.get("up", 0), "down": breadth.get("down", 0)},
        "limitUp": market.get("limitUp", 0),
        "limitDown": market.get("limitDown", 0),
        "hotSectors": (market.get("hotSectors") or [])[:10],
        "mode": market.get("mode", ""),
        "dailyDataDate": market.get("dailyDataDate", ""),
        "scanTime": market.get("scanTime", ""),
        "candidateTypeCounts": market.get("candidateTypeCounts") or {},
        "poolTypeCounts": market.get("poolTypeCounts") or market.get("candidateTypeCounts") or {},
        "semifinalTypeCounts": market.get("semifinalTypeCounts") or {},
        "semifinalSupplement": market.get("semifinalSupplement") or {},
        "environment": market.get("environment") or {},
        "mainlines": market.get("mainlines") or [],
        "sectorEvidenceMeta": market.get("sectorEvidenceMeta") or {},
        "sectorRiskSummary": market.get("sectorRiskSummary") or {},
        "sectorContext": market.get("sectorContext") or {},
        "styleRegime": market.get("styleRegime") or {},
        "shadowPoolSummary": market.get("shadowPoolSummary") or {},
        "buildPoolProgress": market.get("buildPoolProgress") or {},
        "strategyBuckets": market.get("strategyBuckets") or [],
        "finalRebalance": market.get("finalRebalance") or {},
        "rejectedStats": market.get("rejectedStats") or {},
        "promptVersion": market.get("promptVersion") or AI_FULL_MARKET_PROMPT_VERSION,
        "selectionConfigVersion": market.get("selectionConfigVersion") or "",
        "selectionConfig": market.get("selectionConfig") or {},
        "learningMeta": market.get("learningMeta") or {},
        "runSampleRole": market.get("runSampleRole") or "",
        "runSampleRoleLabel": market.get("runSampleRoleLabel") or "",
        "standardScanTime": market.get("standardScanTime") or AI_FULL_MARKET_STANDARD_SCAN_TIME,
        "standardEndTime": market.get("standardEndTime") or AI_FULL_MARKET_STANDARD_END_TIME,
    }


def _broad_listing_boards() -> set[str]:
    return {"创业板", "深市主板", "沪市主板", "科创板", "北交所", "沪深主板", "主板"}


def _sector_evidence(candidate: Dict[str, Any]) -> Dict[str, Any]:
    existing = candidate.get("sectorEvidence") if isinstance(candidate.get("sectorEvidence"), dict) else {}
    if existing:
        return {
            "listingBoard": candidate.get("listingBoard") or "",
            "industrySector": candidate.get("industrySector") or "",
            "conceptSectors": candidate.get("conceptSectors") or [],
            "sectorEvidenceStatus": candidate.get("sectorEvidenceStatus") or existing.get("status") or "unavailable",
            "sectorEvidenceNote": "；".join(existing.get("reasons") or [])[:220],
            "sectorEvidence": existing,
        }
    sector = str(candidate.get("sector") or "").strip()
    theme = str(candidate.get("theme") or "").strip()
    provider_theme = any(keyword in theme for keyword in ("实时行情", "日线行情", "AKShare", "新浪", "腾讯", "Tushare", "BaoStock"))
    boards = _broad_listing_boards()
    listing_board = sector if sector in boards else ""
    industry_sector = "" if sector in boards else sector
    concept_sectors = [] if provider_theme or not theme or theme == sector else [theme]
    if industry_sector or concept_sectors:
        status = "available"
        note = "行业或概念字段可作为弱证据，仍需结合量价和市场环境验证。"
    elif listing_board:
        status = "proxy"
        note = "当前只有上市板块代理，不能据此断言行业龙头、概念主线或板块前排。"
    else:
        status = "unavailable"
        note = "缺少真实行业/概念板块证据。"
    return {
        "listingBoard": listing_board,
        "industrySector": industry_sector,
        "conceptSectors": concept_sectors,
        "sectorEvidenceStatus": status,
        "sectorEvidenceNote": note,
        "sectorEvidence": {
            "version": AI_FULL_MARKET_SECTOR_EVIDENCE_VERSION,
            "status": status,
            "semanticLevel": "legacy_provider_sector" if status == "available" else "listing_board_proxy",
            "listingBoardProxy": status != "available",
            "listingBoardIsIndustry": False,
            "matchedSectors": [],
            "reasons": [note],
        },
    }


def _compact_candidate(candidate: Dict[str, Any], profile: str) -> Dict[str, Any]:
    checks = candidate.get("checks") or []
    sector_evidence = _sector_evidence(candidate)
    data = {
        "code": candidate.get("code"),
        "name": candidate.get("name"),
        "listingBoard": sector_evidence.get("listingBoard"),
        "industrySector": sector_evidence.get("industrySector"),
        "conceptSectors": sector_evidence.get("conceptSectors"),
        "sectorEvidenceStatus": sector_evidence.get("sectorEvidenceStatus"),
        "sectorEvidenceNote": sector_evidence.get("sectorEvidenceNote"),
        "sectorState": candidate.get("sectorState") or "data_insufficient",
        "sectorRiskScore": candidate.get("sectorRiskScore"),
        "sectorConfidence": candidate.get("sectorConfidence"),
        "sectorFlowInterpretation": candidate.get("sectorFlowInterpretation") or "",
        "industryRank": candidate.get("industryRank") or 0,
        "industryLimitUpCount": candidate.get("industryLimitUpCount") or 0,
        "stockRankInIndustry": candidate.get("stockRankInIndustry") or 0,
        "conceptLimitUpCount": candidate.get("conceptLimitUpCount") or 0,
        "conceptLimitUpCounts": candidate.get("conceptLimitUpCounts") or [],
        "sectorPositiveEvidenceRefs": candidate.get("sectorPositiveEvidenceRefs") or [],
        "sectorCounterEvidenceRefs": candidate.get("sectorCounterEvidenceRefs") or [],
        "sectorMissingEvidence": candidate.get("sectorMissingEvidence") or [],
        "sectorExceptionEligible": bool(candidate.get("sectorExceptionEligible")),
        "sectorExposureAdjustment": candidate.get("sectorExposureAdjustment"),
        "styleRegimeFit": candidate.get("styleRegimeFit") or {},
        "styleRegimeAdjustment": candidate.get("styleRegimeAdjustment"),
        "evidenceRefs": candidate.get("evidenceRefs") or _candidate_evidence_refs(candidate),
        "sector": candidate.get("sector"),
        "theme": candidate.get("theme"),
        "ruleRank": candidate.get("ruleRank"),
        "candidateType": candidate.get("candidateType"),
        "candidateTypeLabel": candidate.get("candidateTypeLabel"),
        "candidateTypes": candidate.get("candidateTypes"),
        "score": candidate.get("score"),
        "status": candidate.get("status"),
        "style": candidate.get("style"),
        "change": candidate.get("change"),
        "turnover": candidate.get("turnover"),
        "volumeRatio": candidate.get("volumeRatio"),
        "amountFactor": candidate.get("amountFactor"),
        "relStrength": candidate.get("relStrength"),
        "sectorRank": candidate.get("sectorRank"),
        "riskFlags": candidate.get("riskFlags"),
        "tradeability": candidate.get("tradeability") or {},
        "dimensions": candidate.get("dimensions") or {},
        "passed": candidate.get("passed"),
        "checks": {
            "passed": [item.get("label") for item in checks if item.get("ok")],
            "failed": [item.get("label") for item in checks if not item.get("ok")],
        },
    }
    if profile == "daily":
        data["dailyMetrics"] = candidate.get("dailyMetrics") or {}
        data["dailyTrend"] = candidate.get("dailyTrend") or {}
        data["missingData"] = candidate.get("missingData") or []
    else:
        data["klineSource"] = candidate.get("klineSource")
        data["lastVwap"] = candidate.get("lastVwap")
        data["morningHigh"] = candidate.get("morningHigh")
        data["morningLow"] = candidate.get("morningLow")
        data["buyTrigger"] = candidate.get("buyTrigger")
        data["invalidCondition"] = candidate.get("invalidCondition")
        data["overnight"] = candidate.get("overnight")
    return data


def _role_prompt(profile: str, stage: str) -> str:
    return (
        "你是一个规则约束下的 A 股全市场选股 AI。"
        "AI 全市场选股只使用日线数据，目标是未来 1-7 个交易日的短线候选发现，不是立即买入信号，也不是中线选股。"
        "你的任务不是简单复述规则分数，也不是只选择规则分最高的股票，而是在系统完成基础硬过滤后，从候选池中比较不同类型股票，选出最值得进入重点观察名单的标的。"
        "系统已经完成硬过滤：剔除 ST、退市风险、停牌、当前配置不支持的市场板块、极低流动性股票，以及数据明显异常或无法识别的股票。"
        "硬过滤之外，所有规则分、趋势状态、风险标签、daily-risk、invalid、均线状态、量价状态，都只是分析证据，不是绝对淘汰条件。"
        "你必须比较强趋势延续、板块前排、放量启动、回踩承接、低位反转、超跌修复、高人气高风险、规则不看好但异动明显等候选类型。"
        "你不能只按 score、ruleRank、status 排名，必须结合趋势结构、量价关系、板块强度、相对强弱、流动性、风险标签、位置高低、可验证买点或观察点、当前市场环境。"
        "如果 sectorEvidenceStatus 是 proxy、stale 或 unavailable，禁止把 listingBoard 或行情源代理字段解释成行业龙头、概念主线、板块前排；只能写成数据缺失或代理证据。"
        "sectorState 由系统按同日行业/概念快照计算，你只能引用输入给出的状态和证据，不能自行猜测行业退潮、轮入、资金共振或板块前排。"
        "distribution/retreat/weakening 中的普通趋势延续、追涨突破和后排跟风是反方证据；若仍入选必须标记 sectorExceptionUsed=true 并说明例外理由。"
        "rotation_in/strong 只代表板块证据较好，不等于可以买；overheated 必须审查追高和兑现风险。"
        "如果选择规则分不高、daily-risk 或 invalid 的股票，reason 必须说明它的反转、异动、题材、资金或结构改善证据。"
        "每个入选项必须给出 candidateTypeUsed，并说明 whyBetterThanSimilar，解释它为什么强于同类候选或为什么值得占用一个名额。supportingEvidenceRefs/counterEvidenceRefs 只能引用该股票 candidate.evidenceRefs 数组里真实存在的 id，例如 Q1/Q2/R1/S0/S1/S2/S3/S4；不要引用未出现在 evidenceRefs 里的编号。证据不足时可以少选，不得为了凑满 Top10 降低标准。"
        "输出严格 JSON，根字段 picks；每项包含 code、rank、candidateTypeUsed、strategyHypothesis、supportingEvidenceRefs、counterEvidenceRefs、missingEvidence、marketFit、sectorFit、sectorExceptionUsed、sectorExceptionReason、reason、risk、focus、entryCondition、invalidCondition、confidence、confidenceReason、whyBetterThanSimilar。"
        "禁止输出候选池外股票，禁止 Markdown，禁止必涨、稳赚、满仓、无风险等确定性表达。"
        f"当前阶段：{stage}；当前 profile：{profile}。"
    )


def _daily_batch_prompt(limit: int) -> str:
    return (
        "当前是日线 AI 全市场选股的批次初筛阶段。\n"
        "这些 candidates 来自全市场硬过滤后的分层抽样，不是规则最终推荐名单。\n"
        f"请从本批 candidates 中选择最多 {limit} 只，进入半决赛池；证据不足可以少于 {limit}。\n"
        "选择目标：选择未来 1-7 个交易日内最值得跟踪的日线趋势、回踩、启动或修复候选。入选仅代表候选发现，不代表当日立即买入。\n"
        "你必须做横向比较，不要只选 score 最高的股票。\n"
        "请兼顾强趋势延续、板块前排、放量启动、回踩承接、低位反转、超跌修复、高人气高风险、规则不看好但异动明显。\n"
        "daily-risk、规则低分、均线破位、MACD 弱，只是风险证据，不是绝对淘汰条件。\n"
        "sectorEvidenceStatus 为 proxy、stale 或 unavailable 时，禁止输出行业龙头、概念主线、板块前排等结论；必须在 missingEvidence 中说明板块证据不足。\n"
        "如果你选择这类股票，必须说明它为什么仍值得进入半决赛。\n"
        "reason 必须引用具体证据，例如 MA20/MA60、return20d、distanceMa20、amountRatio5d、sectorRank、riskFlags，不能只写强趋势或量价配合。\n"
        "如果放弃低位反转、超跌修复或规则不看好但异动明显，请在 rejectedSummary 中概括原因。\n"
        "输出 JSON 格式：{\"picks\":[{\"code\":\"股票代码，必须来自 candidates\",\"rank\":1,\"candidateTypeUsed\":\"实际采用的候选类型\",\"strategyHypothesis\":\"1-7日候选假设\",\"supportingEvidenceRefs\":[\"Q1\",\"S1\"],\"counterEvidenceRefs\":[\"R1\",\"S2\"],\"missingEvidence\":[\"缺失证据\"],\"marketFit\":\"与市场环境是否匹配\",\"sectorFit\":\"板块证据质量和匹配度\",\"sectorExceptionUsed\":false,\"sectorExceptionReason\":\"若来自退潮/派发/转弱板块仍入选，说明为什么不是普通后排反抽\",\"reason\":\"入选理由，说明为什么它在本批中更值得进入半决赛\",\"risk\":\"主要风险，必须具体\",\"focus\":\"后续确认点，例如站回 MA20、放量突破、回踩承接、板块继续走强等\",\"entryCondition\":\"什么条件满足后才可以入手\",\"invalidCondition\":\"什么条件出现则今天不能买或观察失效\",\"confidence\":0,\"confidenceReason\":\"置信度来源和不足\",\"whyBetterThanSimilar\":\"为什么比同类候选更值得入选\"}],\"rejectedSummary\":[{\"candidateType\":\"被放弃类型\",\"reason\":\"放弃原因\",\"count\":0}]}\n"
        f"要求：picks 最多 {limit} 个；candidateTypeUsed 必须来自该股票 candidateTypes 或能解释为其主风格；code 必须完全来自 candidates；rank 从 1 开始；confidence 表示相对信心，不表示收益承诺；不要输出 candidates 外的股票；不要输出 Markdown。"
    )


def _final_prompt(limit: int) -> str:
    return (
        "当前是 AI 全市场选股的最终 Top10 阶段。\n"
        "你会收到 semifinal candidates。这些股票来自多个批次的 AI 初筛结果，覆盖强趋势、反转、低位启动、板块前排、超跌修复、高风险高弹性等不同类型。\n"
        f"请从 semifinal candidates 中最终选择最多 {limit} 只，作为本次 AI 全市场未来 1-7 个交易日重点观察名单；证据不足可以少于 {limit}。\n"
        "最终选择要求：不要简单按 score 或 ruleRank 排名；必须比较不同股票之间的确定性、弹性、风险收益比和可验证买点；Top10 中可以包含不同风格，但不能全是同一种类型；"
        "对于高位强趋势股票，要重点说明追高风险和失效条件；对于低位反转、daily-risk 或 invalid 股票，要说明为什么风险可接受，以及需要什么确认；"
        "sectorEvidenceStatus 为 proxy、stale 或 unavailable 时，禁止把上市板块或行情源代理字段解释成行业龙头、概念主线或板块前排；应在 missingEvidence 中写明板块证据不足；"
        "如果某只股票只是规则分高但缺乏买点，不应优先选择；如果某只股票规则分不高但异动证据强，可以选择，但必须说明证据；"
        "如果市场环境偏弱，应该降低追高票权重，提高承接、低位修复、防守型候选权重；如果市场环境偏强，可以提高强趋势、板块前排、放量突破候选权重。"
        "Top10 不能无解释地全部集中在单一风格；如果最终仍集中在强趋势或板块前排，必须在 portfolioComment 说明其他类型证据为什么不足。\n"
        "输出 JSON 格式：{\"picks\":[{\"code\":\"股票代码，必须来自 candidates\",\"rank\":1,\"candidateTypeUsed\":\"实际采用的候选类型\",\"strategyHypothesis\":\"1-7日候选假设\",\"supportingEvidenceRefs\":[\"Q1\",\"S1\"],\"counterEvidenceRefs\":[\"R1\",\"S2\"],\"missingEvidence\":[\"缺失证据\"],\"marketFit\":\"与市场环境是否匹配\",\"sectorFit\":\"板块证据质量和匹配度\",\"sectorExceptionUsed\":false,\"sectorExceptionReason\":\"若来自退潮/派发/转弱板块仍入选，说明为什么不是普通后排反抽\",\"reason\":\"最终入选理由，说明为什么它比其他 semifinal 股票更值得进入 Top10\",\"risk\":\"主要风险，必须具体\",\"focus\":\"后续确认点或观察条件\",\"entryCondition\":\"什么条件满足后才可以入手\",\"invalidCondition\":\"什么条件出现则今天不能买或观察失效\",\"confidence\":0,\"confidenceReason\":\"置信度来源和不足\",\"whyBetterThanSimilar\":\"为什么比同类候选更值得入选\"}],\"portfolioComment\":\"说明最终组合的风格结构和取舍\",\"rejectedSummary\":[{\"candidateType\":\"被放弃类型\",\"reason\":\"放弃原因\",\"count\":0}]}\n"
        f"要求：picks 最多 {limit} 个；candidateTypeUsed 必须来自该股票 candidateTypes 或能解释为其主风格；code 必须完全来自 candidates；rank 从 1 开始；confidence 表示相对信心，不表示收益承诺；不要输出 candidates 外的股票；不要输出 Markdown。"
    )


def _normalize_outcome_horizons(values: Optional[List[int]]) -> List[int]:
    allowed = set(AI_FULL_MARKET_OUTCOME_HORIZONS)
    if not values:
        return list(AI_FULL_MARKET_OUTCOME_HORIZONS)
    result = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number in allowed and number not in result:
            result.append(number)
    return result or list(AI_FULL_MARKET_OUTCOME_HORIZONS)


def _recommendation_date_from_run(run: Dict[str, Any]) -> str:
    for key in ("finishedAt", "startedAt", "createdAt"):
        text = str(run.get(key) or "")
        if len(text) >= 10:
            return text[:10]
    return datetime.now().date().isoformat()


def _classify_market_state(market: Dict[str, Any]) -> str:
    breadth = market.get("breadth") or {}
    up = int(breadth.get("up") or 0)
    down = int(breadth.get("down") or 0)
    total = max(1, up + down)
    up_ratio = up / total
    limit_up = int(market.get("limitUp") or 0)
    limit_down = int(market.get("limitDown") or 0)
    if total <= 1:
        return "unknown"
    if up_ratio >= 0.68 and limit_up >= max(35, limit_down * 1.8):
        return "trend_up"
    if up_ratio <= 0.35 or limit_down >= max(30, limit_up * 1.2):
        return "retreat"
    if 0.45 <= up_ratio <= 0.58:
        return "mixed"
    return "mild_up" if up_ratio > 0.58 else "mild_down"


def _build_ai_full_market_outcome_rows(
    run: Dict[str, Any],
    items: List[Dict[str, Any]],
    series_by_code: Dict[str, List[Dict[str, Any]]],
    recommendation_date: str,
    horizons: List[int],
    benchmark: List[Dict[str, Any]],
    market_state: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in items:
        code = str(item.get("code") or "").zfill(6)
        if not code:
            continue
        payload = item.get("item") if isinstance(item.get("item"), dict) else item
        series = _sorted_kline_series(series_by_code.get(code, []))
        anchor_index = _anchor_index(series, recommendation_date)
        reference_price = _item_reference_price(payload) or _series_close(series, anchor_index)
        for horizon in horizons:
            row = _build_single_outcome(
                run=run,
                item=item,
                payload=payload,
                series=series,
                benchmark=benchmark,
                anchor_index=anchor_index,
                reference_price=reference_price,
                recommendation_date=recommendation_date,
                horizon=horizon,
                market_state=market_state,
            )
            rows.append(row)
    return rows


def _build_single_outcome(
    *,
    run: Dict[str, Any],
    item: Dict[str, Any],
    payload: Dict[str, Any],
    series: List[Dict[str, Any]],
    benchmark: List[Dict[str, Any]],
    anchor_index: int,
    reference_price: Optional[float],
    recommendation_date: str,
    horizon: int,
    market_state: str,
) -> Dict[str, Any]:
    selected = str(item.get("stage") or "") == "final"
    base = {
        "runId": int(run.get("id") or 0),
        "code": str(item.get("code") or "").zfill(6),
        "name": item.get("name") or payload.get("name") or "",
        "recommendationDate": recommendation_date,
        "profile": run.get("profile") or "",
        "riskMode": run.get("riskMode") or "",
        "selectionMode": run.get("selectionMode") or "",
        "stage": item.get("stage") or "",
        "selected": selected,
        "candidateType": item.get("candidateTypeUsed") or payload.get("candidateType") or "",
        "ruleRank": int(item.get("ruleRank") or payload.get("ruleRank") or 0),
        "aiRank": int(item.get("aiRank") or 0),
        "horizon": horizon,
        "buyPrice": reference_price,
        "marketState": market_state,
        "sectorState": payload.get("sectorState") or "data_insufficient",
        "sectorEvidenceStatus": payload.get("sectorEvidenceStatus") or "",
        "industrySector": payload.get("industrySector") or "",
        "conceptSectors": payload.get("conceptSectors") or [],
        "sectorExceptionEligible": bool(payload.get("sectorExceptionEligible")),
        "sectorExceptionUsed": bool(payload.get("sectorExceptionUsed") or (item.get("pick") or {}).get("sectorExceptionUsed")),
        "sectorFlowInterpretation": payload.get("sectorFlowInterpretation") or "",
        "styleRegime": ((run.get("market") or {}).get("styleRegime") or {}).get("styleRegime") or "",
        "marketRisk": ((run.get("market") or {}).get("styleRegime") or {}).get("marketRisk") or "",
    }
    if anchor_index < 0 or not reference_price or reference_price <= 0:
        return {**base, "status": "no_data", "label": "", "reason": "缺少推荐日价格或 K 线锚点。"}
    exit_index = anchor_index + horizon
    if exit_index >= len(series):
        return {**base, "status": "pending", "label": "", "reason": "尚未产生完整未来交易日数据。"}
    exit_point = series[exit_index]
    exit_price = _safe_number(exit_point.get("close"))
    if not exit_price:
        return {**base, "status": "no_data", "label": "", "reason": "缺少未来收盘价。"}
    window = series[anchor_index + 1 : exit_index + 1]
    max_high = max((_safe_number(point.get("high")) or exit_price for point in window), default=exit_price)
    min_low = min((_safe_number(point.get("low")) or exit_price for point in window), default=exit_price)
    return_pct = round((exit_price - reference_price) / reference_price * 100, 2)
    max_runup_pct = round((max_high - reference_price) / reference_price * 100, 2)
    max_drawdown_pct = round((min_low - reference_price) / reference_price * 100, 2)
    benchmark_return = _benchmark_return(benchmark, recommendation_date, horizon)
    excess_return = round(return_pct - benchmark_return, 2) if benchmark_return is not None else None
    discovery_triggered = _discovery_triggered(selected, payload, window, reference_price, max_runup_pct, return_pct)
    trade_verification = _trade_entry_verification(payload)
    entry_triggered = bool(trade_verification.get("entryTriggered"))
    invalid_triggered = max_drawdown_pct <= -4.0 or any((_safe_number(point.get("close")) or reference_price) <= reference_price * 0.965 for point in window)
    stop_triggered = max_drawdown_pct <= -5.5
    chase_failure = selected and max_runup_pct >= 2.5 and return_pct < 0
    trigger_point = _first_trigger_point(window, reference_price) if discovery_triggered else {}
    label, reason = _outcome_label(selected, return_pct, excess_return, max_drawdown_pct, max_runup_pct, discovery_triggered, invalid_triggered)
    return {
        **base,
        "exitDate": str(exit_point.get("date") or ""),
        "exitPrice": exit_price,
        "returnPct": return_pct,
        "benchmarkReturnPct": benchmark_return,
        "excessReturnPct": excess_return,
        "maxDrawdownPct": max_drawdown_pct,
        "maxRunupPct": max_runup_pct,
        "discoveryTriggered": discovery_triggered,
        "entryTriggered": entry_triggered,
        "invalidTriggered": invalid_triggered,
        "triggerDate": str(trigger_point.get("date") or ""),
        "triggerPrice": _safe_number(trigger_point.get("high")) or _safe_number(trigger_point.get("close")),
        "entryMethod": trade_verification.get("entryMethod") or _entry_method(payload),
        "tradeValidationStatus": trade_verification.get("status"),
        "tradeLabel": trade_verification.get("label"),
        "tradeReason": trade_verification.get("reason"),
        "candidateReturnPct": return_pct,
        "candidateExcessReturnPct": excess_return,
        "stopTriggered": stop_triggered,
        "chaseFailure": chase_failure,
        "status": "done",
        "label": label,
        "reason": reason,
    }


def _sorted_kline_series(series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    values = [item for item in series if isinstance(item, dict) and item.get("date")]
    return sorted(values, key=lambda item: str(item.get("date") or ""))


def _anchor_index(series: List[Dict[str, Any]], recommendation_date: str) -> int:
    result = -1
    for index, point in enumerate(series):
        if str(point.get("date") or "") <= recommendation_date:
            result = index
        else:
            break
    return result


def _series_close(series: List[Dict[str, Any]], index: int) -> Optional[float]:
    if index < 0 or index >= len(series):
        return None
    return _safe_number(series[index].get("close"))


def _item_reference_price(item: Dict[str, Any]) -> Optional[float]:
    last = item.get("last") if isinstance(item.get("last"), dict) else {}
    return _safe_number(item.get("price")) or _safe_number(last.get("close")) or _safe_number(item.get("close"))


def _benchmark_return(benchmark: List[Dict[str, Any]], recommendation_date: str, horizon: int) -> Optional[float]:
    series = _sorted_kline_series(benchmark)
    anchor = _anchor_index(series, recommendation_date)
    start = _series_close(series, anchor)
    end = _series_close(series, anchor + horizon)
    if not start or not end:
        return None
    return round((end - start) / start * 100, 2)


def _discovery_triggered(
    selected: bool,
    payload: Dict[str, Any],
    window: List[Dict[str, Any]],
    reference_price: float,
    max_runup_pct: float,
    return_pct: float,
) -> bool:
    if not window:
        return False
    if not selected:
        return max_runup_pct >= 3.5 or return_pct >= 3.0
    return max_runup_pct >= 1.0 or return_pct > 0


def _trade_entry_verification(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = str(payload.get("entryCondition") or payload.get("buyTrigger") or "").strip()
    if not text:
        return {
            "status": "trade_unverifiable",
            "label": "entry_condition_missing",
            "entryTriggered": False,
            "entryMethod": "",
            "reason": "缺少结构化进场条件，不能验证真实可执行收益。",
        }
    return {
        "status": "trade_unverifiable",
        "label": "entry_condition_text_unparsed",
        "entryTriggered": False,
        "entryMethod": text[:120],
        "reason": "进场条件是自然语言文本，当前版本不做启发式触发猜测，需进场审计或个股分析确认。",
    }


def _first_trigger_point(window: List[Dict[str, Any]], reference_price: float) -> Dict[str, Any]:
    for point in window:
        high = _safe_number(point.get("high")) or reference_price
        close = _safe_number(point.get("close")) or reference_price
        if high >= reference_price * 1.01 or close > reference_price:
            return point
    return window[0] if window else {}


def _entry_method(payload: Dict[str, Any]) -> str:
    text = str(payload.get("entryCondition") or payload.get("buyTrigger") or "").strip()
    return text[:120] if text else "价格触发代理：未来窗口内出现 1% 以上上冲或收盘转强。"


def _outcome_label(
    selected: bool,
    return_pct: float,
    excess_pct: Optional[float],
    max_drawdown_pct: float,
    max_runup_pct: float,
    entry_triggered: bool,
    invalid_triggered: bool,
) -> tuple[str, str]:
    score = excess_pct if excess_pct is not None else return_pct
    if selected:
        if invalid_triggered and return_pct < 0:
            return "false_positive", "候选入选后触发失效/止损代理且方向走弱，属于候选发现错买样本。"
        if score >= 2.0 and max_drawdown_pct > -5:
            return "true_positive", "入选后相对表现较好，且回撤可控。该标签衡量候选发现，不代表真实成交收益。"
        if return_pct >= 0:
            return "weak_positive", "方向没有明显错误，但收益弹性不足。该标签衡量候选发现，不代表真实成交收益。"
        return "false_positive", "入选后亏损或跑输明显，需要复盘候选发现错买原因。"
    if entry_triggered and (score >= 3.0 or max_runup_pct >= 5.0):
        return "missed_opportunity", "未入选样本后续触发走强代理，需要复盘漏判原因。"
    if return_pct <= -2.0:
        return "correct_reject", "未入选后表现偏弱，拒绝判断基本有效。"
    return "neutral_reject", "未入选后表现中性，暂不构成明显错判。"


def _summarize_ai_full_market_outcomes(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    done = [item for item in items if item.get("status") == "done"]
    pending = [item for item in items if item.get("status") == "pending"]
    no_data = [item for item in items if item.get("status") == "no_data"]
    selected_rows = [item for item in items if int(item.get("selected") or 0) == 1]
    rejected_rows = [item for item in items if int(item.get("selected") or 0) == 0]
    selected_done_rows = [item for item in selected_rows if item.get("status") == "done"]
    selected_pending_rows = [item for item in selected_rows if item.get("status") == "pending"]
    selected_no_data_rows = [item for item in selected_rows if item.get("status") == "no_data"]
    primary_all = [item for item in items if int(item.get("horizon") or 0) == AI_FULL_MARKET_PRIMARY_HORIZON]
    primary_done = [item for item in done if int(item.get("horizon") or 0) == AI_FULL_MARKET_PRIMARY_HORIZON]
    horizon5_all = [item for item in items if int(item.get("horizon") or 0) == 5]
    horizon5 = [item for item in done if int(item.get("horizon") or 0) == 5]
    horizon_counts = {
        str(horizon): len([item for item in done if int(item.get("horizon") or 0) == horizon])
        for horizon in AI_FULL_MARKET_OUTCOME_HORIZONS
    }
    selected = [item for item in primary_done if int(item.get("selected") or 0) == 1]
    rejected = [item for item in primary_done if int(item.get("selected") or 0) == 0]
    validated_run_count = len({int(item.get("run_id") or item.get("runId") or 0) for item in selected if int(item.get("run_id") or item.get("runId") or 0) > 0})
    sample_selected = [item for item in primary_all if int(item.get("selected") or 0) == 1]
    sample_rejected = [item for item in primary_all if int(item.get("selected") or 0) == 0]
    by_type: Dict[str, Dict[str, Any]] = {}
    for row in primary_done:
        candidate_type = str(row.get("candidate_type") or "other")
        bucket = by_type.setdefault(candidate_type, {"candidateType": candidate_type, "sampleCount": 0, "selectedCount": 0, "rejectedCount": 0, "selectedReturnPct": [], "selectedExcessPct": [], "missedCount": 0, "falsePositiveCount": 0, "notTriggeredCount": 0})
        bucket["sampleCount"] += 1
        if int(row.get("selected") or 0) == 1:
            bucket["selectedCount"] += 1
            if row.get("return_pct") is not None:
                bucket["selectedReturnPct"].append(float(row["return_pct"]))
            if row.get("excess_return_pct") is not None:
                bucket["selectedExcessPct"].append(float(row["excess_return_pct"]))
            if row.get("label") == "false_positive":
                bucket["falsePositiveCount"] += 1
            if row.get("label") == "not_triggered":
                bucket["notTriggeredCount"] += 1
        else:
            bucket["rejectedCount"] += 1
            if row.get("label") == "missed_opportunity":
                bucket["missedCount"] += 1
    for bucket in by_type.values():
        bucket["selectedAvgReturnPct"] = _average(bucket.pop("selectedReturnPct", []))
        bucket["selectedAvgExcessPct"] = _average(bucket.pop("selectedExcessPct", []))
        bucket["missedRatePct"] = _rate(bucket.get("missedCount", 0), bucket.get("rejectedCount", 0))
        bucket["falsePositiveRatePct"] = _rate(bucket.get("falsePositiveCount", 0), bucket.get("selectedCount", 0))
    by_sector_state = _summarize_outcomes_by_json_field(primary_done, "sectorState")
    by_style_regime = _summarize_outcomes_by_json_field(primary_done, "styleRegime")
    trade_unverifiable = [
        item
        for item in selected
        if str(_json_or_empty(item.get("outcome_json")).get("tradeValidationStatus") or "") == "trade_unverifiable"
    ]
    return {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "rowCount": len(items),
        "doneCount": len(done),
        "pendingCount": len(pending),
        "noDataCount": len(no_data),
        "selectedRowCount": len(selected_rows),
        "selectedDoneRowCount": len(selected_done_rows),
        "selectedPendingRowCount": len(selected_pending_rows),
        "selectedNoDataRowCount": len(selected_no_data_rows),
        "rejectedRowCount": len(rejected_rows),
        "primaryHorizon": AI_FULL_MARKET_PRIMARY_HORIZON,
        "horizonCounts": horizon_counts,
        "allHorizon5Count": len(horizon5_all),
        "horizon5Count": len(horizon5),
        "allHorizon7Count": len(primary_all),
        "horizon7Count": len(primary_done),
        "validatedRunCount": validated_run_count,
        "completedRunCount": validated_run_count,
        "sampleSelectedCount": len(sample_selected),
        "sampleRejectedCount": len(sample_rejected),
        "selectedCount": len(selected),
        "rejectedCount": len(rejected),
        "selectedAvgReturnPct": _average([float(item["return_pct"]) for item in selected if item.get("return_pct") is not None]),
        "selectedAvgExcessPct": _average([float(item["excess_return_pct"]) for item in selected if item.get("excess_return_pct") is not None]),
        "missedOpportunityCount": len([item for item in rejected if item.get("label") == "missed_opportunity"]),
        "falsePositiveCount": len([item for item in selected if item.get("label") == "false_positive"]),
        "notTriggeredCount": len([item for item in selected if item.get("label") == "not_triggered"]),
        "tradeUnverifiableCount": len(trade_unverifiable),
        "tradeVerifiedCount": max(0, len(selected) - len(trade_unverifiable)),
        "candidateDiscoveryNote": f"主评价使用 T+{AI_FULL_MARKET_PRIMARY_HORIZON} 候选发现表现，不等于真实可执行收益。",
        "tradePerformanceNote": "自然语言进场条件暂不做启发式触发猜测；可执行交易表现需进场审计或结构化条件验证。",
        "byCandidateType": sorted(by_type.values(), key=lambda item: item.get("sampleCount", 0), reverse=True),
        "bySectorState": by_sector_state,
        "byStyleRegime": by_style_regime,
        "sectorExceptionStats": _sector_exception_stats(primary_done),
    }


def _build_ai_full_market_optimization_report(stats: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    suggestion = _suggest_ai_full_market_config(stats, config)
    report = {
        "featureId": "ai_full_market_selection",
        "configKey": AI_FULL_MARKET_SELECTION_CONFIG_KEY,
        "eligible": suggestion.get("allowApply", False),
        "reason": suggestion.get("reason", ""),
        "suggestion": suggestion,
    }
    report["learningMeta"] = _selection_learning_meta(config, report)
    return report


def _summarize_outcomes_by_json_field(items: List[Dict[str, Any]], field: str) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for item in items:
        outcome = _json_or_empty(item.get("outcome_json"))
        key = str(outcome.get(field) or item.get(field) or "unknown")
        bucket = buckets.setdefault(
            key,
            {
                "key": key,
                "sampleCount": 0,
                "selectedCount": 0,
                "rejectedCount": 0,
                "selectedReturnPct": [],
                "selectedExcessPct": [],
                "falsePositiveCount": 0,
                "missedOpportunityCount": 0,
                "maxDrawdownPct": [],
                "maxRunupPct": [],
            },
        )
        bucket["sampleCount"] += 1
        if item.get("max_drawdown_pct") is not None:
            bucket["maxDrawdownPct"].append(float(item["max_drawdown_pct"]))
        if item.get("max_runup_pct") is not None:
            bucket["maxRunupPct"].append(float(item["max_runup_pct"]))
        if int(item.get("selected") or 0) == 1:
            bucket["selectedCount"] += 1
            if item.get("return_pct") is not None:
                bucket["selectedReturnPct"].append(float(item["return_pct"]))
            if item.get("excess_return_pct") is not None:
                bucket["selectedExcessPct"].append(float(item["excess_return_pct"]))
            if item.get("label") == "false_positive":
                bucket["falsePositiveCount"] += 1
        else:
            bucket["rejectedCount"] += 1
            if item.get("label") == "missed_opportunity":
                bucket["missedOpportunityCount"] += 1
    result = []
    for bucket in buckets.values():
        selected_returns = bucket.pop("selectedReturnPct", [])
        selected_excess = bucket.pop("selectedExcessPct", [])
        drawdowns = bucket.pop("maxDrawdownPct", [])
        runups = bucket.pop("maxRunupPct", [])
        bucket["selectedAvgReturnPct"] = _average(selected_returns)
        bucket["selectedAvgExcessPct"] = _average(selected_excess)
        bucket["avgMaxDrawdownPct"] = _average(drawdowns)
        bucket["avgMaxRunupPct"] = _average(runups)
        bucket["falsePositiveRatePct"] = _rate(bucket.get("falsePositiveCount", 0), bucket.get("selectedCount", 0))
        bucket["missedRatePct"] = _rate(bucket.get("missedOpportunityCount", 0), bucket.get("rejectedCount", 0))
        result.append(bucket)
    return sorted(result, key=lambda row: row.get("sampleCount", 0), reverse=True)


def _sector_exception_stats(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    selected = []
    for item in items:
        if int(item.get("selected") or 0) != 1:
            continue
        outcome = _json_or_empty(item.get("outcome_json"))
        if outcome.get("sectorExceptionUsed") or outcome.get("sectorExceptionEligible"):
            selected.append(item)
    return {
        "sampleCount": len(selected),
        "avgReturnPct": _average([float(item["return_pct"]) for item in selected if item.get("return_pct") is not None]),
        "avgExcessPct": _average([float(item["excess_return_pct"]) for item in selected if item.get("excess_return_pct") is not None]),
        "falsePositiveCount": len([item for item in selected if item.get("label") == "false_positive"]),
        "falsePositiveRatePct": _rate(len([item for item in selected if item.get("label") == "false_positive"]), len(selected)),
    }


def _build_ai_full_market_review(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    primary = [item for item in items if int(item.get("horizon") or 0) == AI_FULL_MARKET_PRIMARY_HORIZON]
    horizon5 = [item for item in items if int(item.get("horizon") or 0) == 5]
    selected_primary = [item for item in primary if int(item.get("selected") or 0) == 1]
    rejected_primary = [item for item in primary if int(item.get("selected") or 0) == 0]
    selected_h5 = [item for item in horizon5 if int(item.get("selected") or 0) == 1]
    rejected_h5 = [item for item in horizon5 if int(item.get("selected") or 0) == 0]
    by_type: Dict[str, Dict[str, Any]] = {}
    for item in primary:
        candidate_type = str(item.get("candidate_type") or "other")
        bucket = by_type.setdefault(
            candidate_type,
            {
                "candidateType": candidate_type,
                "sampleCount": 0,
                "selectedCount": 0,
                "rejectedCount": 0,
                "selectedReturnPct": [],
                "selectedExcessPct": [],
                "falsePositiveCount": 0,
                "missedOpportunityCount": 0,
                "notTriggeredCount": 0,
            },
        )
        bucket["sampleCount"] += 1
        if int(item.get("selected") or 0) == 1:
            bucket["selectedCount"] += 1
            if item.get("return_pct") is not None:
                bucket["selectedReturnPct"].append(float(item["return_pct"]))
            if item.get("excess_return_pct") is not None:
                bucket["selectedExcessPct"].append(float(item["excess_return_pct"]))
            if item.get("label") == "false_positive":
                bucket["falsePositiveCount"] += 1
            if item.get("label") == "not_triggered":
                bucket["notTriggeredCount"] += 1
        else:
            bucket["rejectedCount"] += 1
            if item.get("label") == "missed_opportunity":
                bucket["missedOpportunityCount"] += 1
    for bucket in by_type.values():
        bucket["selectedAvgReturnPct"] = _average(bucket.pop("selectedReturnPct", []))
        bucket["selectedAvgExcessPct"] = _average(bucket.pop("selectedExcessPct", []))
        bucket["falsePositiveRatePct"] = _rate(bucket.get("falsePositiveCount", 0), bucket.get("selectedCount", 0))
        bucket["missedRatePct"] = _rate(bucket.get("missedOpportunityCount", 0), bucket.get("rejectedCount", 0))
    by_sector_state = _summarize_outcomes_by_json_field(primary, "sectorState")
    by_style_regime = _summarize_outcomes_by_json_field(primary, "styleRegime")

    by_run: Dict[int, Dict[str, Any]] = {}
    for item in items:
        run_id = int(item.get("run_id") or 0)
        bucket = by_run.setdefault(
            run_id,
            {
                "runId": run_id,
                "recommendationDate": item.get("recommendation_date") or "",
                "finishedAt": item.get("run_finished_at") or "",
                "horizons": {},
                "selectedReturnPct": [],
                "selectedExcessPct": [],
            },
        )
        horizon = str(int(item.get("horizon") or 0))
        horizon_bucket = bucket["horizons"].setdefault(horizon, {"doneCount": 0, "selectedCount": 0, "selectedAvgReturnPct": []})
        horizon_bucket["doneCount"] += 1
        if int(item.get("selected") or 0) == 1:
            horizon_bucket["selectedCount"] += 1
            if item.get("return_pct") is not None:
                horizon_bucket["selectedAvgReturnPct"].append(float(item["return_pct"]))
                if int(item.get("horizon") or 0) == AI_FULL_MARKET_PRIMARY_HORIZON:
                    bucket["selectedReturnPct"].append(float(item["return_pct"]))
            if item.get("excess_return_pct") is not None:
                if int(item.get("horizon") or 0) == AI_FULL_MARKET_PRIMARY_HORIZON:
                    bucket["selectedExcessPct"].append(float(item["excess_return_pct"]))
    run_rows = []
    for bucket in by_run.values():
        for horizon_bucket in bucket["horizons"].values():
            horizon_bucket["selectedAvgReturnPct"] = _average(horizon_bucket.pop("selectedAvgReturnPct", []))
        bucket["selectedAvgReturnPct"] = _average(bucket.pop("selectedReturnPct", []))
        bucket["selectedAvgExcessPct"] = _average(bucket.pop("selectedExcessPct", []))
        run_rows.append(bucket)

    return {
        "version": "v1",
        "promptVersion": AI_FULL_MARKET_PROMPT_VERSION,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "rowCount": len(items),
        "runCount": len(by_run),
        "primaryHorizon": AI_FULL_MARKET_PRIMARY_HORIZON,
        "horizon5Count": len(horizon5),
        "horizon7Count": len(primary),
        "selectedCount": len(selected_primary),
        "rejectedCount": len(rejected_primary),
        "selectedAvgReturnPct": _average([float(item["return_pct"]) for item in selected_primary if item.get("return_pct") is not None]),
        "selectedAvgExcessPct": _average([float(item["excess_return_pct"]) for item in selected_primary if item.get("excess_return_pct") is not None]),
        "falsePositiveCount": len([item for item in selected_primary if item.get("label") == "false_positive"]),
        "notTriggeredCount": len([item for item in selected_primary if item.get("label") == "not_triggered"]),
        "missedOpportunityCount": len([item for item in rejected_primary if item.get("label") == "missed_opportunity"]),
        "horizon5SelectedCount": len(selected_h5),
        "horizon5RejectedCount": len(rejected_h5),
        "candidateDiscoveryNote": f"主评价使用 T+{AI_FULL_MARKET_PRIMARY_HORIZON} 候选发现表现，不等于真实可执行收益。",
        "tradePerformanceNote": "自然语言进场条件暂不做启发式触发猜测；可执行交易表现需进场审计或结构化条件验证。",
        "byCandidateType": sorted(by_type.values(), key=lambda item: item.get("sampleCount", 0), reverse=True),
        "bySectorState": by_sector_state,
        "byStyleRegime": by_style_regime,
        "sectorExceptionStats": _sector_exception_stats(primary),
        "byRun": sorted(run_rows, key=lambda item: int(item.get("runId") or 0), reverse=True),
        "rows": items[:200],
    }


def _suggest_ai_full_market_config(stats: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    min_runs = int(config.get("minCompletedRuns") or 12)
    min_outcomes = int(config.get("minValidatedOutcomes") or 60)
    min_selected = int(config.get("minSelectedOutcomes") or 20)
    thresholds = {
        "minCompletedRuns": min_runs,
        "minValidatedOutcomes": min_outcomes,
        "minSelectedOutcomes": min_selected,
        "minImprovementPct": float(config.get("minImprovementPct") or 0.5),
    }
    sample_status = {
        "effectiveCandidateRuns": int(stats.get("effectiveTradingDayCount") or 0),
        "completedRuns": int(stats.get("validatedRunCount") or stats.get("completedRunCount") or 0),
        "validatedOutcomes": int(stats.get("doneCount") or 0),
        "selectedOutcomes": int(stats.get("selectedCount") or 0),
    }
    missing = []
    if sample_status["completedRuns"] < min_runs:
        missing.append(f"完成7日验证Run {sample_status['completedRuns']}/{min_runs}")
    if sample_status["validatedOutcomes"] < min_outcomes:
        missing.append(f"完成验证行 {sample_status['validatedOutcomes']}/{min_outcomes}")
    if sample_status["selectedOutcomes"] < min_selected:
        missing.append(f"Top10 7日样本 {sample_status['selectedOutcomes']}/{min_selected}")
    if missing:
        return {
            "allowApply": False,
            "reason": f"样本不足，继续收集 AI 全市场历史结果：{'；'.join(missing)}。",
            "updates": {},
            "thresholds": thresholds,
            "sampleStatus": sample_status,
            "parameterChanges": [],
            "evaluatedTypes": [],
            "aiParticipates": False,
            "method": "统计规则",
        }
    baseline = float(stats.get("selectedAvgExcessPct") or 0)
    min_improvement = thresholds["minImprovementPct"]
    current_scale = dict(config.get("typeExposureScale") or {})
    current_sector_scale = dict(config.get("sectorStateExposureScale") or {})
    updates: Dict[str, float] = {}
    sector_updates: Dict[str, float] = {}
    reasons = []
    parameter_changes = []
    evaluated_types = []
    for bucket in stats.get("byCandidateType") or []:
        candidate_type = str(bucket.get("candidateType") or "")
        selected_count = int(bucket.get("selectedCount") or 0)
        if candidate_type not in CANDIDATE_TYPES or selected_count < 5:
            continue
        avg_excess = bucket.get("selectedAvgExcessPct")
        if avg_excess is None:
            continue
        delta = float(avg_excess) - baseline
        old_scale = float(current_scale.get(candidate_type, 1.0) or 1.0)
        new_scale = old_scale
        action = "keep"
        reason = f"{_candidate_type_label(candidate_type)} 与整体基线差异未超过 {min_improvement}pct，保持权重。"
        if delta >= min_improvement:
            new_scale = min(1.8, old_scale * 1.08)
            action = "increase"
            reason = f"{_candidate_type_label(candidate_type)} 优于整体 Top10 基线 {round(delta, 2)}pct，增加候选暴露。"
            reasons.append(reason)
        elif delta <= -min_improvement:
            new_scale = max(0.5, old_scale * 0.92)
            action = "decrease"
            reason = f"{_candidate_type_label(candidate_type)} 低于整体 Top10 基线 {round(abs(delta), 2)}pct，降低候选暴露。"
            reasons.append(reason)
        if abs(new_scale - old_scale) >= 0.01:
            updates[candidate_type] = round(new_scale, 3)
        row = {
            "candidateType": candidate_type,
            "label": _candidate_type_label(candidate_type),
            "selectedCount": selected_count,
            "selectedAvgExcessPct": round(float(avg_excess), 2),
            "baselineExcessPct": round(baseline, 2),
            "deltaPct": round(delta, 2),
            "oldScale": round(old_scale, 3),
            "newScale": round(new_scale, 3),
            "action": action,
            "reason": reason,
        }
        evaluated_types.append(row)
        if action != "keep":
            parameter_changes.append(row)
    evaluated_sector_states = []
    for bucket in stats.get("bySectorState") or []:
        sector_state = str(bucket.get("key") or "")
        selected_count = int(bucket.get("selectedCount") or 0)
        if sector_state not in SECTOR_STATES or selected_count < 5:
            continue
        avg_excess = bucket.get("selectedAvgExcessPct")
        if avg_excess is None:
            continue
        delta = float(avg_excess) - baseline
        old_scale = float(current_sector_scale.get(sector_state, 1.0) or 1.0)
        new_scale = old_scale
        action = "keep"
        reason = f"{_sector_state_label(sector_state)} 板块状态与整体基线差异未超过 {min_improvement}pct，保持权重。"
        if delta >= min_improvement:
            new_scale = min(1.8, old_scale * 1.05)
            action = "increase"
            reason = f"{_sector_state_label(sector_state)} 状态优于整体 Top10 基线 {round(delta, 2)}pct，建议小幅增加暴露。"
            reasons.append(reason)
        elif delta <= -min_improvement:
            new_scale = max(0.5, old_scale * 0.95)
            action = "decrease"
            reason = f"{_sector_state_label(sector_state)} 状态低于整体 Top10 基线 {round(abs(delta), 2)}pct，建议小幅降低暴露。"
            reasons.append(reason)
        row = {
            "parameterType": "sectorStateExposureScale",
            "sectorState": sector_state,
            "label": _sector_state_label(sector_state),
            "selectedCount": selected_count,
            "selectedAvgExcessPct": round(float(avg_excess), 2),
            "baselineExcessPct": round(baseline, 2),
            "deltaPct": round(delta, 2),
            "oldScale": round(old_scale, 3),
            "newScale": round(new_scale, 3),
            "action": action,
            "reason": reason + (" 当前只有 sectorExposureAdjustEnabled=true 后才影响正式候选池。" if action != "keep" else ""),
        }
        evaluated_sector_states.append(row)
        if abs(new_scale - old_scale) >= 0.01:
            sector_updates[sector_state] = round(new_scale, 3)
            parameter_changes.append(row)
    update_payload: Dict[str, Any] = {}
    if updates:
        update_payload["typeExposureScale"] = updates
    if sector_updates:
        update_payload["sectorStateExposureScale"] = sector_updates
    return {
        "allowApply": bool(update_payload),
        "reason": "；".join(reasons) if reasons else "没有发现足够稳定的软参数调整机会。",
        "updates": update_payload,
        "thresholds": thresholds,
        "sampleStatus": sample_status,
        "parameterChanges": parameter_changes,
        "evaluatedTypes": evaluated_types,
        "evaluatedSectorStates": evaluated_sector_states,
        "aiParticipates": False,
        "method": "统计规则",
    }


def _average(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


def _rate(part: int, total: int) -> Optional[float]:
    return round(part / total * 100, 2) if total else None


def _safe_rank(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 999


def _safe_number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_confidence(value: Any) -> Optional[float]:
    number = _safe_number(value)
    if number is None:
        return None
    if 0 <= number <= 1:
        return round(number * 100, 1)
    if 1 < number <= 10:
        return round(number * 10, 1)
    return round(max(0, min(100, number)), 1)


def _normalize_code(value: Any) -> str:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def _progress_pct(run: Dict[str, Any]) -> int:
    batch_count = int(run.get("batchCount") or 0)
    completed = int(run.get("completedBatchCount") or 0)
    if run.get("status") == "done":
        return 100
    if str(run.get("currentStage") or "").startswith("building_pool"):
        market_progress = ((run.get("market") or {}).get("buildPoolProgress") or {})
        return max(3, min(95, int(market_progress.get("progressPct") or 3)))
    if batch_count <= 0:
        return 0
    stage_bonus = 0.1 if run.get("currentStage") == "final" else 0
    return min(99, round(((completed / batch_count) * 0.9 + stage_bonus) * 100))


def _elapsed_seconds(run: Dict[str, Any]) -> int:
    started = _parse_time(run.get("startedAt")) or _parse_time(run.get("createdAt"))
    finished = _parse_time(run.get("finishedAt"))
    if not started:
        return 0
    end = finished or datetime.now()
    return max(0, int((end - started).total_seconds()))


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
