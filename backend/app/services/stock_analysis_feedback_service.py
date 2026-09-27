from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.app.database import Database
from backend.app.providers.daily_kline_provider import DailyKlineProvider
from backend.app.services.ai_narrator import _chat_completions_url, _extract_chat_content, _http_error_message
from backend.app.settings import PROJECT_ROOT, Settings


LOGGER = logging.getLogger("ai_stock.stock_analysis_feedback")

DEFAULT_HORIZONS = (3, 5, 7, 10)
SHORT_HORIZONS = (3, 5, 7, 10)
MID_HORIZONS = (20, 40)
LONG_HORIZONS = (60, 120, 240)
V2_HORIZON_BUCKETS = {
    "short": SHORT_HORIZONS,
    "mid": MID_HORIZONS,
    "long": LONG_HORIZONS,
}
DEFAULT_CASE_PROMPT_CONFIG = {
    "mode": "auto",
    "minSamples": 800,
    "minSimilarity": 0.65,
    "minMatchedCases": 5,
    "recentFalseBuyWindow": 200,
    "falseBuyAllowedDegradePoints": 5.0,
    "falseBuyAbsoluteLimitPct": 25.0,
    "success3dReturnPct": 3.0,
    "success10dReturnPct": 6.0,
    "falseBuyReturnPct": -3.0,
    "falseBuyDrawdownPct": -5.0,
    "missed5dReturnPct": 5.0,
    "caseDedupeWindowDays": 5,
}
REVIEW_LOG_PATH = PROJECT_ROOT / "logs" / "stock_analysis_review.log"
STOCK_ANALYSIS_TABLES = {
    "v1": {
        "snapshots": "stock_analysis_snapshots",
        "outcomes": "stock_analysis_outcomes",
        "trade_outcomes": "",
        "review_reports": "stock_analysis_review_reports",
        "review_lessons": "stock_analysis_review_lessons",
        "review_lesson_cases": "stock_analysis_review_lesson_cases",
        "config_key": "stock_case_prompt",
        "analysis_version": "stock_analysis_v1",
        "outcome_version": "direction_v1",
    },
    "v2": {
        "snapshots": "stock_analysis_snapshots_v2",
        "outcomes": "stock_analysis_outcomes_v2",
        "trade_outcomes": "stock_analysis_trade_outcomes_v2",
        "review_reports": "stock_analysis_review_reports_v2",
        "review_lessons": "stock_analysis_review_lessons_v2",
        "review_lesson_cases": "stock_analysis_review_lesson_cases_v2",
        "config_key": "stock_case_prompt_v2",
        "analysis_version": "stock_analysis_v2",
        "outcome_version": "direction_v2",
    },
}


def _normalize_feedback_version(value: Any) -> str:
    text = str(value or "v1").strip().lower()
    if text in {"2", "v2", "stock_analysis_v2", "direction_v2"}:
        return "v2"
    return "v1"


class StockAnalysisFeedbackService:
    def __init__(self, settings: Settings, database: Database, version: str = "v1"):
        self.settings = settings
        self.database = database
        self.daily_kline_provider = DailyKlineProvider(settings.tushare_token)
        self.version = _normalize_feedback_version(version)
        self.tables = STOCK_ANALYSIS_TABLES[self.version]

    def table(self, key: str) -> str:
        return self.tables[key]

    def build_case_memory(self, evidence_pack: Dict[str, Any]) -> Dict[str, Any]:
        stats = self.stats()
        config = self.current_config()
        mode = config["mode"]
        by_horizon: Dict[str, Dict[str, Any]] = {}
        for bucket in ("short", "mid", "long"):
            similar_cases = self.similar_cases(evidence_pack, limit=3, bucket=bucket)
            active_lessons = self.match_active_lessons(evidence_pack, limit=3, bucket=bucket)
            eligible = _prompt_eligible_for_bucket(stats, config, similar_cases, bucket)
            sample_pool = (stats.get("samplePools") or {}).get(bucket, {}) if isinstance(stats.get("samplePools"), dict) else {}
            should_inject = mode == "on" or (mode == "auto" and eligible)
            by_horizon[bucket] = {
                "bucket": bucket,
                "horizons": list(V2_HORIZON_BUCKETS.get(bucket, ())),
                "status": _case_bucket_status(mode, sample_pool, similar_cases, should_inject, config),
                "effectiveSampleCount": int(sample_pool.get("effectiveSampleCount") or 0),
                "matchedCaseCount": len(similar_cases),
                "bestSimilarity": similar_cases[0]["similarity"] if similar_cases else None,
                "injected": should_inject,
                "eligible": eligible,
                "similarCases": similar_cases,
                "reviewMemoryStatus": {
                    "activeLessonCount": len(active_lessons),
                    "injected": should_inject and bool(active_lessons),
                    "lessons": active_lessons if should_inject else [],
                },
            }
        short_memory = by_horizon.get("short", {})
        similar_cases = short_memory.get("similarCases", [])
        short_review_status = short_memory.get("reviewMemoryStatus", {}) if isinstance(short_memory.get("reviewMemoryStatus"), dict) else {}
        eligible = bool(short_memory.get("eligible"))
        should_inject = any(bool(item.get("injected")) for item in by_horizon.values())
        any_pool_ready = any(
            int(item.get("effectiveSampleCount") or 0) >= int(config["minSamples"])
            for item in by_horizon.values()
        )
        any_similar_cases = any(bool(item.get("similarCases")) for item in by_horizon.values())
        if mode == "off":
            status = "disabled"
        elif should_inject:
            status = "injected"
        elif not any_pool_ready:
            status = "collecting"
        elif not any_similar_cases:
            status = "insufficient_match"
        else:
            status = "available"
        return {
            "status": status,
            "version": self.version,
            "analysisVersion": self.tables["analysis_version"],
            "mode": mode,
            "effectiveSampleCount": stats["effectiveSampleCount"],
            "samplePools": stats.get("samplePools", {}),
            "promptSamplePool": stats.get("promptSamplePool", "short"),
            "minSamples": config["minSamples"],
            "matchedCaseCount": len(similar_cases),
            "bestSimilarity": similar_cases[0]["similarity"] if similar_cases else None,
            "injected": should_inject,
            "eligible": eligible,
            "falseBuyGuard": stats.get("falseBuyGuard", {}),
            "caseDedupeWindowDays": config["caseDedupeWindowDays"],
            "similarCases": similar_cases,
            "byHorizon": by_horizon,
            "reviewMemoryStatus": {
                "activeLessonCount": short_review_status.get("activeLessonCount", 0),
                "injected": bool(short_review_status.get("injected")),
                "lessons": short_review_status.get("lessons", []),
            },
        }

    def prompt_payload(self, case_memory: Dict[str, Any]) -> Dict[str, Any]:
        if not case_memory.get("injected"):
            return {}
        by_horizon = case_memory.get("byHorizon") if isinstance(case_memory.get("byHorizon"), dict) else {}
        case_memory_by_horizon: Dict[str, Any] = {}
        review_memory_by_horizon: Dict[str, Any] = {}
        for bucket in ("short", "mid", "long"):
            item = by_horizon.get(bucket) if isinstance(by_horizon.get(bucket), dict) else {}
            if not item.get("injected"):
                continue
            case_memory_by_horizon[bucket] = {
                "effectiveSampleCount": item.get("effectiveSampleCount", 0),
                "matchedCaseCount": item.get("matchedCaseCount", 0),
                "bestSimilarity": item.get("bestSimilarity"),
                "similarCases": item.get("similarCases", [])[:5],
            }
            review_status = item.get("reviewMemoryStatus") if isinstance(item.get("reviewMemoryStatus"), dict) else {}
            review_memory_by_horizon[bucket] = review_status.get("lessons", [])[:3] if review_status.get("injected") else []
        return {
            "caseMemory": {
                "effectiveSampleCount": case_memory.get("effectiveSampleCount", 0),
                "similarCases": case_memory.get("similarCases", [])[:5],
                "constraints": [
                    "历史案例只能校准判断，不能覆盖硬风控。",
                    "如果当前命中硬风控，即使相似历史案例表现较好，也不得直接输出 buy。",
                    "如果案例建议和风控结论冲突，必须输出 caseVsRiskConflict。",
                ],
            },
            "caseMemoryByHorizon": case_memory_by_horizon,
            "reviewMemory": case_memory.get("reviewMemoryStatus", {}).get("lessons", [])[:3],
            "reviewMemoryByHorizon": review_memory_by_horizon,
        }

    def current_config(self) -> Dict[str, Any]:
        return _case_prompt_config(self.settings, self._load_ui_config())

    def update_config(self, values: Dict[str, Any]) -> Dict[str, Any]:
        current = self.current_config()
        updated = dict(current)
        for key in DEFAULT_CASE_PROMPT_CONFIG:
            if key in values:
                updated[key] = values[key]
        updated = _normalize_case_prompt_config(updated)
        now = datetime.now().isoformat(timespec="seconds")
        key = self.tables["config_key"]
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (key, json.dumps(updated, ensure_ascii=False), now),
            )
        LOGGER.info("stock case prompt config updated mode=%s min_samples=%s", updated.get("mode"), updated.get("minSamples"))
        return {"updated": True, "config": updated, "updatedAt": now}

    def _load_ui_config(self) -> Dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute("SELECT value_json FROM app_settings WHERE key = ?", (self.tables["config_key"],)).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row["value_json"] or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def save_snapshot(
        self,
        *,
        code: str,
        name: str,
        risk_mode: str,
        scan_time: str,
        market_source: str,
        ai_mode: str,
        ai_provider: str,
        ai_model: str,
        decision: Dict[str, Any],
        evidence_pack: Dict[str, Any],
        rule_fallback_used: bool,
        case_memory_status: Dict[str, Any],
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        analysis_date = now.split("T", 1)[0]
        snapshot_id = 0
        snapshots_table = self.table("snapshots")
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO {snapshots_table}(
                  code, name, analyzed_at, analysis_date, risk_mode, scan_time, market_source,
                  ai_mode, ai_provider, ai_model, decision, primary_horizon, confidence,
                  evidence_pack_json, decision_json, rule_fallback_used, case_memory_status_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    name,
                    now,
                    analysis_date,
                    risk_mode,
                    scan_time,
                    market_source,
                    ai_mode,
                    ai_provider,
                    ai_model,
                    str(decision.get("decision") or "watch"),
                    str(decision.get("primaryHorizon") or "none"),
                    int(_safe_float(decision.get("confidence")) or 0),
                    json.dumps(evidence_pack, ensure_ascii=False),
                    json.dumps(decision, ensure_ascii=False),
                    1 if rule_fallback_used else 0,
                    json.dumps(case_memory_status, ensure_ascii=False),
                    now,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
        LOGGER.info(
            "stock analysis snapshot saved version=%s id=%s code=%s decision=%s case_status=%s injected=%s",
            self.version,
            snapshot_id,
            code,
            decision.get("decision"),
            case_memory_status.get("status"),
            case_memory_status.get("injected"),
        )
        return snapshot_id

    def update_outcomes(
        self,
        *,
        code: str = "",
        date_from: str = "",
        date_to: str = "",
        horizons: List[int] | None = None,
        limit: int = 500,
        dry_run: bool = False,
        force_history: bool = False,
        mode: str = "backfill",
    ) -> Dict[str, Any]:
        normalized_horizons = _normalize_horizons(horizons)
        normalized_mode = _normalize_outcome_mode(mode)
        batch_size = _normalize_outcome_limit(limit)
        target_snapshot_count = 0
        if normalized_mode == "backfill":
            target_snapshot_count = self._count_snapshots_needing_outcomes(
                code=code,
                date_from=date_from,
                date_to=date_to,
                horizons=normalized_horizons,
            )
            snapshots = self._load_snapshots_needing_outcomes(
                code=code,
                date_from=date_from,
                date_to=date_to,
                horizons=normalized_horizons,
                limit=batch_size,
            )
        else:
            snapshots = self._load_snapshots_for_outcomes(code=code, date_from=date_from, date_to=date_to, limit=batch_size)
            target_snapshot_count = len(snapshots)
        codes = sorted({row["code"] for row in snapshots if row.get("code")})
        history_days = max(240, max(normalized_horizons, default=10) + 80)
        series_by_code = self.daily_kline_provider.fetch_many(codes, days=history_days, force=force_history, cache_only=False)
        updated = 0
        pending = 0
        no_data = 0
        rows: List[Dict[str, Any]] = []
        config = self.current_config()
        for snapshot in snapshots:
            trade = _build_trade(snapshot, series_by_code.get(snapshot["code"], []), normalized_horizons)
            for horizon in normalized_horizons:
                result = trade["results"].get(str(horizon), {})
                bucket = _horizon_bucket(horizon)
                label = _label_for(snapshot.get("decision", "watch"), horizon, trade["results"], config, bucket=bucket)
                row = {
                    "snapshotId": snapshot["id"],
                    "code": snapshot["code"],
                    "name": snapshot.get("name", ""),
                    "recommendationDate": snapshot["analysis_date"],
                    "riskMode": snapshot.get("risk_mode", ""),
                    "decision": snapshot.get("decision", ""),
                    "horizon": horizon,
                    "horizonBucket": bucket,
                    "buyDate": trade.get("buyDate", ""),
                    "buyPrice": trade.get("buyPrice"),
                    "exitDate": result.get("exitDate", ""),
                    "exitPrice": result.get("exitPrice"),
                    "returnPct": result.get("returnPct"),
                    "maxDrawdownPct": result.get("maxDrawdownPct"),
                    "maxRunupPct": result.get("maxRunupPct"),
                    "status": result.get("status", "pending"),
                    "label": label if result.get("status") == "done" else "",
                    "reason": result.get("reason", ""),
                }
                rows.append(row)
                if row["status"] == "done":
                    updated += 1
                elif row["status"] == "pending":
                    pending += 1
                else:
                    no_data += 1
                if not dry_run:
                    self._upsert_outcome(row)
        remaining_snapshot_count = 0
        if normalized_mode == "backfill":
            remaining_snapshot_count = (
                target_snapshot_count
                if dry_run
                else self._count_snapshots_needing_outcomes(
                    code=code,
                    date_from=date_from,
                    date_to=date_to,
                    horizons=normalized_horizons,
                )
            )
        LOGGER.info(
            "stock analysis outcomes updated version=%s mode=%s batch_size=%s snapshots=%s rows=%s done=%s pending=%s no_data=%s remaining=%s dry_run=%s",
            self.version,
            normalized_mode,
            batch_size,
            len(snapshots),
            len(rows),
            updated,
            pending,
            no_data,
            remaining_snapshot_count,
            dry_run,
        )
        return {
            "updated": not dry_run,
            "dryRun": dry_run,
            "version": self.version,
            "outcomeVersion": self.tables["outcome_version"],
            "mode": normalized_mode,
            "batchSize": batch_size,
            "targetSnapshotCount": target_snapshot_count,
            "processedSnapshotCount": len(snapshots),
            "snapshotCount": len(snapshots),
            "rowCount": len(rows),
            "doneCount": updated,
            "pendingCount": pending,
            "noDataCount": no_data,
            "remainingSnapshotCount": remaining_snapshot_count,
            "hasMore": remaining_snapshot_count > 0 if normalized_mode == "backfill" else False,
            "horizons": normalized_horizons,
            "horizonBuckets": sorted({_horizon_bucket(value) for value in normalized_horizons}),
            "historyDaysRequested": history_days,
            "dailyKlineSource": self.daily_kline_provider.last_source,
            "dailyDataDate": self.daily_kline_provider.last_data_date,
            "warning": self.daily_kline_provider.last_warning,
            "rows": rows[:200],
        }

    def stats(self) -> Dict[str, Any]:
        config = self.current_config()
        snapshots_table = self.table("snapshots")
        outcomes_table = self.table("outcomes")
        reports_table = self.table("review_reports")
        lessons_table = self.table("review_lessons")
        effective_sql = _effective_snapshot_filter_sql(snapshots_table)
        short_horizons_sql = ",".join(str(value) for value in SHORT_HORIZONS)
        with self.database.connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) AS count FROM {snapshots_table}").fetchone()["count"]
            deduped_total = connection.execute(
                f"SELECT COUNT(*) AS count FROM {snapshots_table} s WHERE s.id IN ({effective_sql})"
            ).fetchone()["count"]
            bucket_rows = connection.execute(
                f"""
                SELECT o.horizon, o.status, COUNT(*) AS count
                FROM {outcomes_table} o
                JOIN {snapshots_table} s ON s.id = o.snapshot_id
                WHERE s.id IN ({effective_sql})
                GROUP BY o.horizon, o.status
                ORDER BY o.horizon, o.status
                """
            ).fetchall()
            sample_pools = {
                key: _sample_pool_stats(connection, snapshots_table, outcomes_table, effective_sql, key, tuple(horizons))
                for key, horizons in V2_HORIZON_BUCKETS.items()
            }
            buy_total = connection.execute(
                f"""
                SELECT COUNT(DISTINCT s.id) AS count
                FROM {snapshots_table} s
                JOIN {outcomes_table} o ON o.snapshot_id = s.id
                WHERE s.decision = 'buy' AND o.status = 'done'
                  AND o.horizon IN ({short_horizons_sql}) AND s.id IN ({effective_sql})
                """
            ).fetchone()["count"]
            false_buy = connection.execute(
                f"""
                SELECT COUNT(DISTINCT s.id) AS count
                FROM {snapshots_table} s
                JOIN {outcomes_table} o ON o.snapshot_id = s.id
                WHERE s.decision = 'buy' AND o.horizon IN ({short_horizons_sql})
                  AND o.label = 'false_buy' AND s.id IN ({effective_sql})
                """
            ).fetchone()["count"]
            recent_rows = connection.execute(
                f"""
                SELECT s.id, s.decision, MAX(CASE WHEN o.label = 'false_buy' THEN 1 ELSE 0 END) AS is_false_buy
                FROM {snapshots_table} s
                JOIN {outcomes_table} o ON o.snapshot_id = s.id
                WHERE o.status = 'done' AND o.horizon IN ({short_horizons_sql})
                  AND s.id IN ({effective_sql})
                GROUP BY s.id
                ORDER BY s.id DESC
                LIMIT ?
                """,
                (int(config["recentFalseBuyWindow"]),),
            ).fetchall()
            latest_reports = connection.execute(
                f"""
                SELECT id, review_date, period_start, period_end, sample_count, false_buy_count,
                       missed_opportunity_count, report_json, created_at
                FROM {reports_table}
                ORDER BY id DESC
                LIMIT 5
                """,
            ).fetchall()
            lessons = connection.execute(
                f"""
                SELECT id, lesson_key, title, status, pattern_json, prompt_instruction,
                       evidence_json, source_report_id, updated_at
                FROM {lessons_table}
                ORDER BY updated_at DESC
                LIMIT 20
                """
            ).fetchall()
        outcome_buckets = _outcome_bucket_stats(bucket_rows)
        for key, bucket in outcome_buckets.items():
            if key not in sample_pools:
                continue
            sample_pools[key].update(
                {
                    "rowCount": bucket.get("rowCount", 0),
                    "doneOutcomeCount": bucket.get("doneCount", 0),
                    "pendingOutcomeCount": bucket.get("pendingCount", 0),
                    "noDataOutcomeCount": bucket.get("noDataCount", 0),
                }
            )
        short_pool = sample_pools.get("short", {})
        effective = int(short_pool.get("effectiveSampleCount") or 0)
        label_counts = dict(short_pool.get("labelCounts") or {})
        baseline_false_buy_rate = _rate(false_buy, buy_total)
        recent_buy_rows = [row for row in recent_rows if row["decision"] == "buy"]
        recent_false_buy_count = len([row for row in recent_buy_rows if row["is_false_buy"]])
        recent_false_buy_rate = _rate(recent_false_buy_count, len(recent_buy_rows))
        false_buy_guard = {
            "baselineFalseBuyRatePct": baseline_false_buy_rate,
            "recentFalseBuyRatePct": recent_false_buy_rate,
            "recentWindow": int(config["recentFalseBuyWindow"]),
            "allowedDegradePoints": config["falseBuyAllowedDegradePoints"],
            "absoluteLimitPct": config["falseBuyAbsoluteLimitPct"],
            "ok": _false_buy_guard_ok(baseline_false_buy_rate, recent_false_buy_rate, config),
        }
        return {
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "version": self.version,
            "analysisVersion": self.tables["analysis_version"],
            "outcomeVersion": self.tables["outcome_version"],
            "snapshotCount": total,
            "rawSnapshotCount": total,
            "dedupedSnapshotCount": deduped_total,
            "effectiveSampleCount": effective,
            "samplePools": sample_pools,
            "effectiveSampleCountByBucket": {key: pool.get("effectiveSampleCount", 0) for key, pool in sample_pools.items()},
            "labelCounts": label_counts,
            "outcomeBuckets": outcome_buckets,
            "promptOutcomeBucket": "short",
            "promptSamplePool": "short",
            "buySampleCount": buy_total,
            "falseBuyCount": false_buy,
            "falseBuyGuard": false_buy_guard,
            "promptConfig": config,
            "latestReports": [_report_row(row) for row in latest_reports],
            "lessons": [_lesson_row(row) for row in lessons],
        }

    def similar_cases(self, evidence_pack: Dict[str, Any], limit: int = 3, bucket: str = "short") -> List[Dict[str, Any]]:
        target = _features_from_evidence(evidence_pack)
        config = self.current_config()
        snapshots_table = self.table("snapshots")
        outcomes_table = self.table("outcomes")
        effective_sql = _effective_snapshot_filter_sql(snapshots_table)
        horizons = _horizons_for_bucket(bucket)
        placeholders = ",".join("?" for _ in horizons)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT s.id, s.code, s.name, s.analysis_date, s.decision, s.confidence,
                       s.evidence_pack_json, s.decision_json,
                       GROUP_CONCAT(o.horizon || ':' || o.return_pct || ':' || o.label, '|') AS outcome_summary
                FROM {snapshots_table} s
                JOIN {outcomes_table} o ON o.snapshot_id = s.id
                WHERE o.status = 'done' AND o.horizon IN ({placeholders})
                  AND s.id IN ({effective_sql})
                GROUP BY s.id
                ORDER BY s.id DESC
                LIMIT 600
                """,
                list(horizons),
            ).fetchall()
        scored: List[Dict[str, Any]] = []
        current_code = str(evidence_pack.get("snapshot", {}).get("code") or "")
        for row in rows:
            if row["code"] == current_code:
                continue
            try:
                evidence = json.loads(row["evidence_pack_json"])
                decision = json.loads(row["decision_json"])
            except json.JSONDecodeError:
                continue
            features = _features_from_evidence(evidence)
            similarity, similarities, differences = _similarity(target, features)
            if similarity < config["minSimilarity"]:
                continue
            scored.append(
                {
                    "caseId": f"{row['analysis_date']}-{row['code']}-{row['id']}",
                    "snapshotId": row["id"],
                    "code": row["code"],
                    "name": row["name"],
                    "analysisDate": row["analysis_date"],
                    "originalDecision": row["decision"],
                    "confidence": row["confidence"],
                    "horizonBucket": bucket,
                    "label": _dominant_label(str(row["outcome_summary"] or "")),
                    "similarity": round(similarity, 2),
                    "keySimilarities": similarities[:4],
                    "keyDifferences": differences[:3],
                    "outcomes": _outcome_summary(row["outcome_summary"]),
                    "lesson": _case_lesson(row["decision"], _dominant_label(str(row["outcome_summary"] or "")), decision),
                }
            )
        scored.sort(key=lambda item: (item["similarity"], _label_priority(item.get("label", ""))), reverse=True)
        deduped = _dedupe_cases_by_code_window(scored, int(config["caseDedupeWindowDays"]))
        return deduped[: max(0, limit)]

    def match_active_lessons(self, evidence_pack: Dict[str, Any], limit: int = 3, bucket: str = "short") -> List[Dict[str, Any]]:
        features = _features_from_evidence(evidence_pack)
        lessons_table = self.table("review_lessons")
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, lesson_key, title, pattern_json, prompt_instruction, evidence_json
                FROM {lessons_table}
                WHERE status = 'active'
                ORDER BY updated_at DESC
                LIMIT 40
                """
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            try:
                pattern = json.loads(row["pattern_json"] or "{}")
                evidence = json.loads(row["evidence_json"] or "[]")
            except json.JSONDecodeError:
                pattern, evidence = {}, []
            if _lesson_horizon_bucket(pattern, evidence) != bucket:
                continue
            matched, reason = _lesson_matches(features, pattern)
            if not matched:
                continue
            result.append(
                {
                    "lessonKey": row["lesson_key"],
                    "title": row["title"],
                    "promptInstruction": row["prompt_instruction"],
                    "matchReason": reason,
                    "evidence": evidence[:3] if isinstance(evidence, list) else [],
                }
            )
        return result[:limit]

    def review_failures(
        self,
        *,
        date_from: str = "",
        date_to: str = "",
        limit: int = 80,
        save: bool = True,
    ) -> Dict[str, Any]:
        cases = self._failure_cases(date_from=date_from, date_to=date_to, limit=limit)
        report = self._ai_review(cases) if cases else {}
        if not report:
            report = _rule_review_report(cases)
        saved_id = None
        if save:
            saved_id = self._save_review_report(cases, report, date_from=date_from, date_to=date_to)
            self._save_review_lessons(saved_id, report, cases)
        return {
            "saved": save,
            "version": self.version,
            "reportId": saved_id,
            "caseCount": len(cases),
            "cases": cases[:20],
            "report": report,
        }

    def recent_outcomes(self, *, limit: int = 50) -> Dict[str, Any]:
        max_snapshots = max(1, min(200, int(limit or 50)))
        snapshots_table = self.table("snapshots")
        outcomes_table = self.table("outcomes")
        effective_sql = _effective_snapshot_filter_sql(snapshots_table)
        short_horizons_sql = ",".join(str(value) for value in SHORT_HORIZONS)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                WITH recent_snapshots AS (
                  SELECT o.snapshot_id, MAX(o.updated_at) AS last_updated_at
                  FROM {outcomes_table} o
                  JOIN {snapshots_table} s ON s.id = o.snapshot_id
                  WHERE s.id IN ({effective_sql})
                  GROUP BY o.snapshot_id
                  ORDER BY last_updated_at DESC, o.snapshot_id DESC
                  LIMIT ?
                )
                SELECT s.id AS snapshot_id, s.code, s.name, s.analysis_date, s.risk_mode,
                       s.decision, o.buy_date, o.buy_price, o.horizon, o.exit_date,
                       o.exit_price, o.return_pct, o.max_drawdown_pct, o.max_runup_pct,
                       o.status, o.label, o.reason, o.updated_at
                FROM recent_snapshots rs
                JOIN {snapshots_table} s ON s.id = rs.snapshot_id
                JOIN {outcomes_table} o ON o.snapshot_id = rs.snapshot_id
                ORDER BY rs.last_updated_at DESC, s.id DESC, o.horizon ASC
                """,
                (max_snapshots,),
            ).fetchall()
        result_rows = [_outcome_row_from_db(row) for row in rows]
        done = len([row for row in result_rows if row["status"] == "done"])
        pending = len([row for row in result_rows if row["status"] == "pending"])
        no_data = len([row for row in result_rows if row["status"] not in {"done", "pending"}])
        snapshot_count = len({row["snapshotId"] for row in result_rows})
        last_updated_at = max((str(row.get("updatedAt") or "") for row in result_rows), default="")
        return {
            "updated": False,
            "dryRun": False,
            "mode": "recent",
            "source": "stored",
            "version": self.version,
            "outcomeVersion": self.tables["outcome_version"],
            "resultTitle": "最近已补算结果" if self.version == "v1" else "V2 最近方向收益结果",
            "processedSnapshotCount": snapshot_count,
            "snapshotCount": snapshot_count,
            "rowCount": len(result_rows),
            "doneCount": done,
            "pendingCount": pending,
            "noDataCount": no_data,
            "remainingSnapshotCount": 0,
            "hasMore": False,
            "lastUpdatedAt": last_updated_at,
            "rows": result_rows,
        }

    def update_trade_outcomes(
        self,
        *,
        code: str = "",
        date_from: str = "",
        date_to: str = "",
        limit: int = 500,
        dry_run: bool = False,
        force_history: bool = False,
        mode: str = "backfill",
    ) -> Dict[str, Any]:
        self._ensure_trade_outcomes_supported()
        normalized_mode = _normalize_outcome_mode(mode)
        batch_size = _normalize_outcome_limit(limit)
        target_snapshot_count = self._count_snapshots_needing_trade_outcomes(code=code, date_from=date_from, date_to=date_to) if normalized_mode == "backfill" else 0
        snapshots = (
            self._load_snapshots_needing_trade_outcomes(code=code, date_from=date_from, date_to=date_to, limit=batch_size)
            if normalized_mode == "backfill"
            else self._load_snapshots_for_trade_outcomes(code=code, date_from=date_from, date_to=date_to, limit=batch_size)
        )
        if normalized_mode != "backfill":
            target_snapshot_count = len(snapshots)
        codes = sorted({row["code"] for row in snapshots if row.get("code")})
        series_by_code = self.daily_kline_provider.fetch_many(codes, days=260, force=force_history, cache_only=False)
        rows: List[Dict[str, Any]] = []
        done = 0
        pending = 0
        no_trigger = 0
        no_data = 0
        for snapshot in snapshots:
            row = _build_trade_outcome(snapshot, series_by_code.get(snapshot["code"], []))
            rows.append(row)
            if row["status"] == "done":
                done += 1
            elif row["status"] == "pending":
                pending += 1
            elif row["status"] == "no_trigger":
                no_trigger += 1
            else:
                no_data += 1
            if not dry_run:
                self._upsert_trade_outcome(row)
        remaining_snapshot_count = 0
        if normalized_mode == "backfill":
            remaining_snapshot_count = (
                target_snapshot_count
                if dry_run
                else self._count_snapshots_needing_trade_outcomes(code=code, date_from=date_from, date_to=date_to)
            )
        LOGGER.info(
            "stock analysis trade outcomes updated version=%s mode=%s batch_size=%s snapshots=%s rows=%s done=%s pending=%s no_trigger=%s no_data=%s remaining=%s dry_run=%s",
            self.version,
            normalized_mode,
            batch_size,
            len(snapshots),
            len(rows),
            done,
            pending,
            no_trigger,
            no_data,
            remaining_snapshot_count,
            dry_run,
        )
        return {
            "updated": not dry_run,
            "dryRun": dry_run,
            "version": self.version,
            "outcomeVersion": "trade_v2",
            "mode": normalized_mode,
            "batchSize": batch_size,
            "targetSnapshotCount": target_snapshot_count,
            "processedSnapshotCount": len(snapshots),
            "snapshotCount": len(snapshots),
            "rowCount": len(rows),
            "doneCount": done,
            "pendingCount": pending,
            "noTriggerCount": no_trigger,
            "noDataCount": no_data,
            "remainingSnapshotCount": remaining_snapshot_count,
            "hasMore": remaining_snapshot_count > 0 if normalized_mode == "backfill" else False,
            "dailyKlineSource": self.daily_kline_provider.last_source,
            "dailyDataDate": self.daily_kline_provider.last_data_date,
            "warning": self.daily_kline_provider.last_warning,
            "rows": rows[:200],
        }

    def recent_trade_outcomes(self, *, limit: int = 50) -> Dict[str, Any]:
        self._ensure_trade_outcomes_supported()
        max_snapshots = max(1, min(200, int(limit or 50)))
        snapshots_table = self.table("snapshots")
        trade_outcomes_table = self.table("trade_outcomes")
        effective_sql = _effective_snapshot_filter_sql(snapshots_table)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                WITH recent_snapshots AS (
                  SELECT o.snapshot_id, MAX(o.updated_at) AS last_updated_at
                  FROM {trade_outcomes_table} o
                  JOIN {snapshots_table} s ON s.id = o.snapshot_id
                  WHERE s.id IN ({effective_sql})
                  GROUP BY o.snapshot_id
                  ORDER BY last_updated_at DESC, o.snapshot_id DESC
                  LIMIT ?
                )
                SELECT s.id AS snapshot_id, s.code, s.name, s.analysis_date, s.risk_mode,
                       s.decision, o.plan_version, o.horizon_type, o.entry_triggered,
                       o.entry_date, o.entry_price, o.entry_reason, o.exit_date,
                       o.exit_price, o.exit_reason, o.return_pct, o.max_drawdown_pct,
                       o.max_runup_pct, o.status, o.label, o.reason, o.updated_at
                FROM recent_snapshots rs
                JOIN {snapshots_table} s ON s.id = rs.snapshot_id
                JOIN {trade_outcomes_table} o ON o.snapshot_id = rs.snapshot_id
                ORDER BY rs.last_updated_at DESC, s.id DESC, o.horizon_type ASC
                """,
                (max_snapshots,),
            ).fetchall()
        result_rows = [_trade_outcome_row_from_db(row) for row in rows]
        done = len([row for row in result_rows if row["status"] == "done"])
        pending = len([row for row in result_rows if row["status"] == "pending"])
        no_trigger = len([row for row in result_rows if row["status"] == "no_trigger"])
        no_data = len([row for row in result_rows if row["status"] not in {"done", "pending", "no_trigger"}])
        snapshot_count = len({row["snapshotId"] for row in result_rows})
        last_updated_at = max((str(row.get("updatedAt") or "") for row in result_rows), default="")
        return {
            "updated": False,
            "dryRun": False,
            "mode": "recent",
            "source": "stored",
            "version": self.version,
            "outcomeVersion": "trade_v2",
            "resultTitle": "V2 最近策略执行收益",
            "processedSnapshotCount": snapshot_count,
            "snapshotCount": snapshot_count,
            "rowCount": len(result_rows),
            "doneCount": done,
            "pendingCount": pending,
            "noTriggerCount": no_trigger,
            "noDataCount": no_data,
            "remainingSnapshotCount": 0,
            "hasMore": False,
            "lastUpdatedAt": last_updated_at,
            "rows": result_rows,
        }

    def _load_snapshots_for_outcomes(self, *, code: str, date_from: str, date_to: str, limit: int) -> List[Dict[str, Any]]:
        clauses, params = _snapshot_filter_clauses(code=code, date_from=date_from, date_to=date_to)
        params.append(_normalize_outcome_limit(limit))
        snapshots_table = self.table("snapshots")
        effective_sql = _effective_snapshot_filter_sql(snapshots_table)
        sql = f"""
            SELECT id, code, name, analysis_date, analyzed_at, risk_mode, decision
            FROM {snapshots_table}
            WHERE {' AND '.join(clauses)}
              AND id IN ({effective_sql})
            ORDER BY id DESC
            LIMIT ?
        """
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _load_snapshots_needing_outcomes(
        self,
        *,
        code: str,
        date_from: str,
        date_to: str,
        horizons: List[int],
        limit: int,
    ) -> List[Dict[str, Any]]:
        sql, params = _outcome_backfill_query(
            code=code,
            date_from=date_from,
            date_to=date_to,
            horizons=horizons,
            limit=limit,
            snapshots_table=self.table("snapshots"),
            outcomes_table=self.table("outcomes"),
        )
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _count_snapshots_needing_outcomes(self, *, code: str, date_from: str, date_to: str, horizons: List[int]) -> int:
        sql, params = _outcome_backfill_count_query(
            code=code,
            date_from=date_from,
            date_to=date_to,
            horizons=horizons,
            snapshots_table=self.table("snapshots"),
            outcomes_table=self.table("outcomes"),
        )
        with self.database.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return int(row["count"] if row else 0)

    def _load_snapshots_for_trade_outcomes(self, *, code: str, date_from: str, date_to: str, limit: int) -> List[Dict[str, Any]]:
        clauses, params = _snapshot_filter_clauses(code=code, date_from=date_from, date_to=date_to, alias="s")
        params.append(_normalize_outcome_limit(limit))
        snapshots_table = self.table("snapshots")
        effective_sql = _effective_snapshot_filter_sql(snapshots_table)
        sql = f"""
            SELECT s.id, s.code, s.name, s.analysis_date, s.analyzed_at, s.risk_mode,
                   s.decision, s.primary_horizon, s.decision_json
            FROM {snapshots_table} s
            WHERE {' AND '.join(clauses)}
              AND s.decision = 'buy'
              AND s.id IN ({effective_sql})
            ORDER BY s.id DESC
            LIMIT ?
        """
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _load_snapshots_needing_trade_outcomes(self, *, code: str, date_from: str, date_to: str, limit: int) -> List[Dict[str, Any]]:
        sql, params = _trade_outcome_backfill_query(
            code=code,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            snapshots_table=self.table("snapshots"),
            trade_outcomes_table=self.table("trade_outcomes"),
        )
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _count_snapshots_needing_trade_outcomes(self, *, code: str, date_from: str, date_to: str) -> int:
        sql, params = _trade_outcome_backfill_count_query(
            code=code,
            date_from=date_from,
            date_to=date_to,
            snapshots_table=self.table("snapshots"),
            trade_outcomes_table=self.table("trade_outcomes"),
        )
        with self.database.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return int(row["count"] if row else 0)

    def _upsert_outcome(self, row: Dict[str, Any]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        outcomes_table = self.table("outcomes")
        with self.database.connect() as connection:
            connection.execute(
                f"""
                INSERT INTO {outcomes_table}(
                  snapshot_id, code, recommendation_date, buy_date, buy_price, horizon,
                  exit_date, exit_price, return_pct, max_drawdown_pct, max_runup_pct,
                  status, label, reason, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id, horizon) DO UPDATE SET
                  buy_date=excluded.buy_date,
                  buy_price=excluded.buy_price,
                  exit_date=excluded.exit_date,
                  exit_price=excluded.exit_price,
                  return_pct=excluded.return_pct,
                  max_drawdown_pct=excluded.max_drawdown_pct,
                  max_runup_pct=excluded.max_runup_pct,
                  status=excluded.status,
                  label=excluded.label,
                  reason=excluded.reason,
                  updated_at=excluded.updated_at
                """,
                (
                    row["snapshotId"],
                    row["code"],
                    row["recommendationDate"],
                    row.get("buyDate", ""),
                    row.get("buyPrice"),
                    row["horizon"],
                    row.get("exitDate", ""),
                    row.get("exitPrice"),
                    row.get("returnPct"),
                    row.get("maxDrawdownPct"),
                    row.get("maxRunupPct"),
                    row.get("status", "pending"),
                    row.get("label", ""),
                    row.get("reason", ""),
                    now,
                ),
            )

    def _upsert_trade_outcome(self, row: Dict[str, Any]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        trade_outcomes_table = self.table("trade_outcomes")
        with self.database.connect() as connection:
            connection.execute(
                f"""
                INSERT INTO {trade_outcomes_table}(
                  trade_outcome_version, snapshot_id, code, recommendation_date,
                  risk_mode, plan_version, horizon_type, entry_triggered,
                  entry_date, entry_price, entry_reason, exit_date, exit_price,
                  exit_reason, return_pct, max_drawdown_pct, max_runup_pct,
                  status, label, reason, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id, plan_version, horizon_type) DO UPDATE SET
                  entry_triggered=excluded.entry_triggered,
                  entry_date=excluded.entry_date,
                  entry_price=excluded.entry_price,
                  entry_reason=excluded.entry_reason,
                  exit_date=excluded.exit_date,
                  exit_price=excluded.exit_price,
                  exit_reason=excluded.exit_reason,
                  return_pct=excluded.return_pct,
                  max_drawdown_pct=excluded.max_drawdown_pct,
                  max_runup_pct=excluded.max_runup_pct,
                  status=excluded.status,
                  label=excluded.label,
                  reason=excluded.reason,
                  updated_at=excluded.updated_at
                """,
                (
                    "trade_v2",
                    row["snapshotId"],
                    row["code"],
                    row["recommendationDate"],
                    row.get("riskMode", ""),
                    row.get("planVersion", "new_position_plan_v2"),
                    row.get("horizonType", ""),
                    1 if row.get("entryTriggered") else 0,
                    row.get("entryDate", ""),
                    row.get("entryPrice"),
                    row.get("entryReason", ""),
                    row.get("exitDate", ""),
                    row.get("exitPrice"),
                    row.get("exitReason", ""),
                    row.get("returnPct"),
                    row.get("maxDrawdownPct"),
                    row.get("maxRunupPct"),
                    row.get("status", "pending"),
                    row.get("label", ""),
                    row.get("reason", ""),
                    now,
                ),
            )

    def _ensure_trade_outcomes_supported(self) -> None:
        if not self.table("trade_outcomes"):
            raise ValueError("策略执行收益仅支持个股分析 V2。")

    def _failure_cases(self, *, date_from: str, date_to: str, limit: int) -> List[Dict[str, Any]]:
        clauses = ["o.status = 'done'", "o.label IN ('false_buy', 'missed_opportunity')"]
        params: List[Any] = []
        if date_from:
            clauses.append("s.analysis_date >= ?")
            params.append(_normalize_date(date_from))
        if date_to:
            clauses.append("s.analysis_date <= ?")
            params.append(_normalize_date(date_to))
        params.append(max(1, min(300, int(limit or 80))))
        snapshots_table = self.table("snapshots")
        outcomes_table = self.table("outcomes")
        effective_sql = _effective_snapshot_filter_sql(snapshots_table)
        short_horizons_sql = ",".join(str(value) for value in SHORT_HORIZONS)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT s.id, s.code, s.name, s.analysis_date, s.decision, s.confidence,
                       s.evidence_pack_json, o.horizon, o.return_pct, o.max_drawdown_pct,
                       o.max_runup_pct, o.label
                FROM {snapshots_table} s
                JOIN {outcomes_table} o ON o.snapshot_id = s.id
                WHERE {' AND '.join(clauses)}
                  AND o.horizon IN ({short_horizons_sql})
                  AND s.id IN ({effective_sql})
                ORDER BY s.id DESC, o.horizon ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        cases = []
        for row in rows:
            try:
                evidence = json.loads(row["evidence_pack_json"])
            except json.JSONDecodeError:
                evidence = {}
            features = _features_from_evidence(evidence)
            cases.append(
                {
                    "snapshotId": row["id"],
                    "code": row["code"],
                    "name": row["name"],
                    "analysisDate": row["analysis_date"],
                    "decision": row["decision"],
                    "confidence": row["confidence"],
                    "horizon": row["horizon"],
                    "returnPct": row["return_pct"],
                    "maxDrawdownPct": row["max_drawdown_pct"],
                    "maxRunupPct": row["max_runup_pct"],
                    "label": row["label"],
                    "features": features,
                }
            )
        return _dedupe_cases_by_code_window(cases, int(self.current_config()["caseDedupeWindowDays"]))

    def _ai_review(self, cases: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not (self.settings.enable_ai and self.settings.ai_api_key and self.settings.ai_base_url and cases):
            return {}
        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是A股个股分析复盘助手，只根据输入失败案例总结误买和漏判原因，输出严格JSON。",
                },
                {
                    "role": "user",
                    "content": (
                        "请分析这些 false_buy / missed_opportunity 案例，输出 JSON："
                        "summary、falseBuyReasons、missedOpportunityReasons、lessons。"
                        "lessons 每项必须包含 lessonKey、title、pattern、promptInstruction、evidence。"
                        "pattern 必须是 JSON 对象，不能是字符串；只能使用这些字段："
                        "label、decision、riskLabels、dailyStatuses、marketModes、dailyScoreMin、dailyScoreMax、"
                        "intradayScoreMin、turnoverMin、turnoverMax、changeMin、changeMax、volumeRatioMin、"
                        "return20dMin、return20dMax、distanceMa20Min、distanceMa20Max、aboveMa20、aboveMa60、mainNetInflowMin。"
                        "pattern 至少包含两个可匹配条件，避免生成会匹配所有股票的宽泛经验。"
                        "evidence 必须是数组，每项包含 snapshotId、code、name、analysisDate、label、horizon、returnPct、reason。"
                        "promptInstruction 必须是可注入后续个股分析提示词的中文规则，但必须强调确认条件、仓位控制、止损或失效条件；"
                        "不得输出“风险标签不直接否决买入”这类会鼓励无条件追高的规则。"
                        f"\n{json.dumps({'cases': cases[:60]}, ensure_ascii=False)}"
                    ),
                },
            ],
            "temperature": 0.1,
            "max_tokens": 2200,
            "stream": False,
        }
        if self.settings.ai_provider in {"deepseek", "openai"}:
            payload["response_format"] = {"type": "json_object"}
        if self.settings.ai_provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
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
            started = time.perf_counter()
            with urllib.request.urlopen(request, timeout=35) as response:
                data = json.loads(response.read().decode("utf-8"))
            report = _extract_json_object(_extract_chat_content(data))
            LOGGER.info("stock analysis review ai ok cases=%s elapsed_ms=%.0f", len(cases), (time.perf_counter() - started) * 1000)
            return report if isinstance(report, dict) else {}
        except urllib.error.HTTPError as error:
            LOGGER.warning("stock analysis review ai http error=%s", _http_error_message(error))
        except Exception as error:  # noqa: BLE001 - review falls back to deterministic summary.
            LOGGER.warning("stock analysis review ai failed error=%s", error)
        return {}

    def _save_review_report(self, cases: List[Dict[str, Any]], report: Dict[str, Any], *, date_from: str, date_to: str) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        false_buy_count = len([case for case in cases if case.get("label") == "false_buy"])
        missed_count = len([case for case in cases if case.get("label") == "missed_opportunity"])
        reports_table = self.table("review_reports")
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO {reports_table}(
                  review_date, period_start, period_end, sample_count, false_buy_count,
                  missed_opportunity_count, ai_provider, ai_model, report_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    date.today().isoformat(),
                    _normalize_date(date_from),
                    _normalize_date(date_to),
                    len(cases),
                    false_buy_count,
                    missed_count,
                    self.settings.ai_provider,
                    self.settings.ai_model if self.settings.ai_api_key else "",
                    json.dumps(report, ensure_ascii=False),
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def _save_review_lessons(self, report_id: int, report: Dict[str, Any], cases: List[Dict[str, Any]]) -> None:
        lessons = report.get("lessons") if isinstance(report.get("lessons"), list) else []
        now = datetime.now().isoformat(timespec="seconds")
        lessons_table = self.table("review_lessons")
        lesson_cases_table = self.table("review_lesson_cases")
        with self.database.connect() as connection:
            for index, item in enumerate(lessons[:8], start=1):
                if not isinstance(item, dict):
                    continue
                key = _lesson_key(item.get("lessonKey") or item.get("title") or f"lesson_{report_id}_{index}")
                title = str(item.get("title") or key).strip()[:120]
                instruction = str(item.get("promptInstruction") or item.get("suggestion") or "").strip()
                pattern = _normalize_lesson_pattern(
                    item.get("pattern"),
                    lesson_key=key,
                    title=title,
                    instruction=instruction,
                )
                evidence = _normalize_lesson_evidence(item.get("evidence"))
                cursor = connection.execute(
                    f"""
                    INSERT INTO {lessons_table}(
                      lesson_key, title, status, pattern_json, prompt_instruction, evidence_json,
                      source_report_id, created_at, updated_at
                    )
                    VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lesson_key) DO UPDATE SET
                      title=excluded.title,
                      pattern_json=excluded.pattern_json,
                      prompt_instruction=excluded.prompt_instruction,
                      evidence_json=excluded.evidence_json,
                      source_report_id=excluded.source_report_id,
                      updated_at=excluded.updated_at
                    """,
                    (
                        key,
                        title,
                        json.dumps(pattern, ensure_ascii=False),
                        instruction,
                        json.dumps(evidence, ensure_ascii=False),
                        report_id,
                        now,
                        now,
                    ),
                )
                lesson_id = int(cursor.lastrowid or 0)
                if not lesson_id:
                    row = connection.execute(f"SELECT id FROM {lessons_table} WHERE lesson_key = ?", (key,)).fetchone()
                    lesson_id = int(row["id"]) if row else 0
                for case in cases[:20]:
                    if lesson_id and case.get("snapshotId"):
                        connection.execute(
                            f"""
                            INSERT OR REPLACE INTO {lesson_cases_table}(lesson_id, snapshot_id, label, similarity)
                            VALUES (?, ?, ?, ?)
                            """,
                            (lesson_id, case["snapshotId"], case.get("label", ""), 0),
                        )

    def repair_lesson_structures(self, *, dry_run: bool = True, limit: int = 200) -> Dict[str, Any]:
        max_rows = max(1, min(1000, int(limit or 200)))
        lessons_table = self.table("review_lessons")
        reports_table = self.table("review_reports")
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT l.id, l.lesson_key, l.title, l.status, l.pattern_json, l.prompt_instruction,
                       l.evidence_json, l.source_report_id, r.report_json
                FROM {lessons_table} l
                LEFT JOIN {reports_table} r ON r.id = l.source_report_id
                ORDER BY l.id ASC
                LIMIT ?
                """,
                (max_rows,),
            ).fetchall()
            repairs = []
            now = datetime.now().isoformat(timespec="seconds")
            for row in rows:
                current_pattern = _json_dict(row["pattern_json"])
                current_evidence = _json_list(row["evidence_json"])
                report_lesson = _find_report_lesson(row["report_json"], row["lesson_key"], row["title"])
                if report_lesson:
                    raw_pattern = report_lesson.get("pattern", current_pattern)
                    raw_evidence = report_lesson.get("evidence", current_evidence)
                    instruction = str(report_lesson.get("promptInstruction") or row["prompt_instruction"] or "").strip()
                    title = str(report_lesson.get("title") or row["title"] or "").strip()
                else:
                    raw_pattern = current_pattern
                    raw_evidence = current_evidence
                    instruction = str(row["prompt_instruction"] or "").strip()
                    title = str(row["title"] or "").strip()
                repaired_pattern = _normalize_lesson_pattern(
                    raw_pattern,
                    lesson_key=row["lesson_key"],
                    title=title,
                    instruction=instruction,
                )
                repaired_evidence = _normalize_lesson_evidence(raw_evidence)
                current_quality = _lesson_quality(current_pattern, current_evidence, row["prompt_instruction"])
                repaired_quality = _lesson_quality(repaired_pattern, repaired_evidence, instruction)
                changed = current_pattern != repaired_pattern or current_evidence != repaired_evidence
                if not changed:
                    continue
                item = {
                    "id": row["id"],
                    "lessonKey": row["lesson_key"],
                    "title": title or row["title"],
                    "status": row["status"],
                    "before": current_quality,
                    "after": repaired_quality,
                    "pattern": repaired_pattern,
                    "evidenceCount": len(repaired_evidence),
                }
                repairs.append(item)
                if not dry_run:
                    connection.execute(
                        f"""
                        UPDATE {lessons_table}
                        SET pattern_json = ?, evidence_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json.dumps(repaired_pattern, ensure_ascii=False),
                            json.dumps(repaired_evidence, ensure_ascii=False),
                            now,
                            row["id"],
                        ),
                    )
        return {
            "dryRun": dry_run,
            "version": self.version,
            "checkedCount": len(rows),
            "repairCount": len(repairs),
            "updated": 0 if dry_run else len(repairs),
            "repairs": repairs[:50],
        }

    def update_lesson_status(self, lesson_id: int, status: str) -> Dict[str, Any]:
        if status not in {"draft", "active", "paused", "rejected"}:
            raise ValueError("经验状态只能是 draft/active/paused/rejected")
        now = datetime.now().isoformat(timespec="seconds")
        lessons_table = self.table("review_lessons")
        with self.database.connect() as connection:
            current = connection.execute(
                f"""
                SELECT id, lesson_key, title, status, pattern_json, prompt_instruction,
                       evidence_json, source_report_id, updated_at
                FROM {lessons_table}
                WHERE id = ?
                """,
                (lesson_id,),
            ).fetchone()
            if not current:
                raise ValueError(f"没有找到经验 {lesson_id}")
            if status == "active":
                quality = _lesson_quality(
                    _json_dict(current["pattern_json"]),
                    _json_list(current["evidence_json"]),
                    current["prompt_instruction"],
                )
                if not quality["safeToEnable"]:
                    raise ValueError(f"经验结构不完整，不允许启用：{quality['warning']}")
            connection.execute(
                f"UPDATE {lessons_table} SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, lesson_id),
            )
            row = connection.execute(
                f"""
                SELECT id, lesson_key, title, status, pattern_json, prompt_instruction,
                       evidence_json, source_report_id, updated_at
                FROM {lessons_table}
                WHERE id = ?
                """,
                (lesson_id,),
            ).fetchone()
        return _lesson_row(row)


def _snapshot_filter_clauses(*, code: str, date_from: str, date_to: str, alias: str = "") -> tuple[List[str], List[Any]]:
    prefix = f"{alias}." if alias else ""
    clauses = ["1=1"]
    params: List[Any] = []
    normalized_code = _normalize_code(code)
    normalized_from = _normalize_date(date_from)
    normalized_to = _normalize_date(date_to)
    if normalized_code:
        clauses.append(f"{prefix}code = ?")
        params.append(normalized_code)
    if normalized_from:
        clauses.append(f"{prefix}analysis_date >= ?")
        params.append(normalized_from)
    if normalized_to:
        clauses.append(f"{prefix}analysis_date <= ?")
        params.append(normalized_to)
    return clauses, params


def _outcome_row_from_db(row: Any) -> Dict[str, Any]:
    return {
        "snapshotId": row["snapshot_id"],
        "code": row["code"],
        "name": row["name"],
        "recommendationDate": row["analysis_date"],
        "riskMode": row["risk_mode"],
        "decision": row["decision"],
        "horizon": row["horizon"],
        "horizonBucket": _horizon_bucket(row["horizon"]),
        "buyDate": row["buy_date"],
        "buyPrice": row["buy_price"],
        "exitDate": row["exit_date"],
        "exitPrice": row["exit_price"],
        "returnPct": row["return_pct"],
        "maxDrawdownPct": row["max_drawdown_pct"],
        "maxRunupPct": row["max_runup_pct"],
        "status": row["status"],
        "label": row["label"],
        "reason": row["reason"],
        "updatedAt": row["updated_at"],
    }


def _trade_outcome_row_from_db(row: Any) -> Dict[str, Any]:
    return {
        "snapshotId": row["snapshot_id"],
        "code": row["code"],
        "name": row["name"],
        "recommendationDate": row["analysis_date"],
        "riskMode": row["risk_mode"],
        "decision": row["decision"],
        "planVersion": row["plan_version"],
        "horizonType": row["horizon_type"],
        "entryTriggered": bool(row["entry_triggered"]),
        "entryDate": row["entry_date"],
        "entryPrice": row["entry_price"],
        "entryReason": row["entry_reason"],
        "exitDate": row["exit_date"],
        "exitPrice": row["exit_price"],
        "exitReason": row["exit_reason"],
        "returnPct": row["return_pct"],
        "maxDrawdownPct": row["max_drawdown_pct"],
        "maxRunupPct": row["max_runup_pct"],
        "status": row["status"],
        "label": row["label"],
        "reason": row["reason"],
        "updatedAt": row["updated_at"],
    }


def _trade_outcome_backfill_query(
    *,
    code: str,
    date_from: str,
    date_to: str,
    limit: int,
    snapshots_table: str,
    trade_outcomes_table: str,
) -> tuple[str, List[Any]]:
    base_sql, params = _trade_outcome_backfill_base_sql(
        code=code,
        date_from=date_from,
        date_to=date_to,
        snapshots_table=snapshots_table,
        trade_outcomes_table=trade_outcomes_table,
    )
    sql = f"""
        {base_sql}
        SELECT id, code, name, analysis_date, analyzed_at, risk_mode,
               decision, primary_horizon, decision_json
        FROM backfill_candidates
        WHERE outcome_count = 0 OR has_pending_outcome = 1
        ORDER BY CASE WHEN outcome_count = 0 THEN 0 ELSE 1 END,
                 COALESCE(last_outcome_updated_at, '') ASC,
                 analysis_date ASC, id ASC
        LIMIT ?
    """
    return sql, params + [_normalize_outcome_limit(limit)]


def _trade_outcome_backfill_count_query(
    *,
    code: str,
    date_from: str,
    date_to: str,
    snapshots_table: str,
    trade_outcomes_table: str,
) -> tuple[str, List[Any]]:
    base_sql, params = _trade_outcome_backfill_base_sql(
        code=code,
        date_from=date_from,
        date_to=date_to,
        snapshots_table=snapshots_table,
        trade_outcomes_table=trade_outcomes_table,
    )
    sql = f"""
        {base_sql}
        SELECT COUNT(*) AS count
        FROM backfill_candidates
        WHERE outcome_count = 0 OR has_pending_outcome = 1
    """
    return sql, params


def _trade_outcome_backfill_base_sql(
    *,
    code: str,
    date_from: str,
    date_to: str,
    snapshots_table: str,
    trade_outcomes_table: str,
) -> tuple[str, List[Any]]:
    clauses, filter_params = _snapshot_filter_clauses(code=code, date_from=date_from, date_to=date_to, alias="s")
    effective_sql = _effective_snapshot_filter_sql(snapshots_table)
    sql = f"""
        WITH effective_snapshots AS (
          SELECT s.id, s.code, s.name, s.analysis_date, s.analyzed_at, s.risk_mode,
                 s.decision, s.primary_horizon, s.decision_json
          FROM {snapshots_table} s
          WHERE {' AND '.join(clauses)}
            AND s.decision = 'buy'
            AND s.id IN ({effective_sql})
        ),
        backfill_candidates AS (
          SELECT es.*,
                 COUNT(o.id) AS outcome_count,
                 MAX(CASE WHEN o.status = 'pending' THEN 1 ELSE 0 END) AS has_pending_outcome,
                 MAX(o.updated_at) AS last_outcome_updated_at
          FROM effective_snapshots es
          LEFT JOIN {trade_outcomes_table} o ON o.snapshot_id = es.id
          GROUP BY es.id
        )
    """
    return sql, filter_params


def _outcome_backfill_query(
    *,
    code: str,
    date_from: str,
    date_to: str,
    horizons: List[int],
    limit: int,
    snapshots_table: str,
    outcomes_table: str,
) -> tuple[str, List[Any]]:
    base_sql, params, expected_count = _outcome_backfill_base_sql(
        code=code,
        date_from=date_from,
        date_to=date_to,
        horizons=horizons,
        snapshots_table=snapshots_table,
        outcomes_table=outcomes_table,
    )
    sql = f"""
        {base_sql}
        SELECT id, code, name, analysis_date, analyzed_at, risk_mode, decision
        FROM backfill_candidates
        WHERE outcome_count < ? OR has_pending_outcome = 1
        ORDER BY CASE WHEN outcome_count < ? THEN 0 ELSE 1 END,
                 COALESCE(last_outcome_updated_at, '') ASC,
                 analysis_date ASC, id ASC
        LIMIT ?
    """
    return sql, params + [expected_count, expected_count, _normalize_outcome_limit(limit)]


def _outcome_backfill_count_query(
    *,
    code: str,
    date_from: str,
    date_to: str,
    horizons: List[int],
    snapshots_table: str,
    outcomes_table: str,
) -> tuple[str, List[Any]]:
    base_sql, params, expected_count = _outcome_backfill_base_sql(
        code=code,
        date_from=date_from,
        date_to=date_to,
        horizons=horizons,
        snapshots_table=snapshots_table,
        outcomes_table=outcomes_table,
    )
    sql = f"""
        {base_sql}
        SELECT COUNT(*) AS count
        FROM backfill_candidates
        WHERE outcome_count < ? OR has_pending_outcome = 1
    """
    return sql, params + [expected_count]


def _outcome_backfill_base_sql(
    *,
    code: str,
    date_from: str,
    date_to: str,
    horizons: List[int],
    snapshots_table: str,
    outcomes_table: str,
) -> tuple[str, List[Any], int]:
    normalized_horizons = _normalize_horizons(horizons)
    placeholders = ",".join(["?"] * len(normalized_horizons))
    clauses, filter_params = _snapshot_filter_clauses(code=code, date_from=date_from, date_to=date_to, alias="s")
    effective_sql = _effective_snapshot_filter_sql(snapshots_table)
    sql = f"""
        WITH effective_snapshots AS (
          SELECT s.id, s.code, s.name, s.analysis_date, s.analyzed_at, s.risk_mode, s.decision
          FROM {snapshots_table} s
          WHERE {' AND '.join(clauses)}
            AND s.id IN ({effective_sql})
        ),
        backfill_candidates AS (
          SELECT es.*,
                 (
                   SELECT COUNT(DISTINCT o.horizon)
                   FROM {outcomes_table} o
                   WHERE o.snapshot_id = es.id
                     AND o.horizon IN ({placeholders})
                 ) AS outcome_count,
                 EXISTS (
                   SELECT 1
                   FROM {outcomes_table} o
                   WHERE o.snapshot_id = es.id
                     AND o.horizon IN ({placeholders})
                     AND o.status = 'pending'
                 ) AS has_pending_outcome,
                 (
                   SELECT MAX(o.updated_at)
                   FROM {outcomes_table} o
                   WHERE o.snapshot_id = es.id
                     AND o.horizon IN ({placeholders})
                 ) AS last_outcome_updated_at
          FROM effective_snapshots es
        )
    """
    params = filter_params + list(normalized_horizons) + list(normalized_horizons) + list(normalized_horizons)
    return sql, params, len(normalized_horizons)


def _build_trade_outcome(snapshot: Dict[str, Any], series: List[Dict[str, Any]]) -> Dict[str, Any]:
    plan_info = _extract_executable_trade_plan(snapshot)
    row = _empty_trade_outcome_row(snapshot, plan_info)
    if not plan_info.get("valid"):
        row.update(
            {
                "status": "no_data",
                "label": plan_info.get("label") or "plan_unverifiable",
                "reason": plan_info.get("reason") or "交易计划缺少可验证字段。",
            }
        )
        return row

    points = _normalize_series(series)
    recommendation_date = str(snapshot.get("analysis_date") or "")
    entry_index = _next_trading_index(points, recommendation_date)
    if entry_index is None:
        row.update({"status": "pending", "reason": "没有推荐日之后的下一交易日 K 线"})
        return row

    plan = plan_info["plan"]
    buy_low = _positive_number(plan.get("buyPriceLow"))
    buy_high = _positive_number(plan.get("buyPriceHigh"))
    stop_loss = _positive_number(plan.get("stopLossPrice"))
    take_profit = _positive_number(plan.get("takeProfitPrice"))
    if not buy_low or not buy_high or not stop_loss:
        row.update({"status": "no_data", "label": "plan_unverifiable", "reason": "买入区间或止损价缺失。"})
        return row
    if buy_low > buy_high:
        buy_low, buy_high = buy_high, buy_low

    entry_point = points[entry_index]
    entry = _entry_from_buy_range(entry_point, buy_low, buy_high)
    if not entry.get("triggered"):
        row.update(
            {
                "status": "no_trigger",
                "label": "no_trigger",
                "entryDate": str(entry_point.get("date") or ""),
                "reason": f"下一交易日未触达买入区间 {buy_low:.4g}-{buy_high:.4g}。",
            }
        )
        return row

    entry_price = _positive_number(entry.get("price"))
    if not entry_price:
        row.update({"status": "no_data", "label": "plan_unverifiable", "reason": "入场价格缺失。"})
        return row

    row.update(
        {
            "entryTriggered": True,
            "entryDate": str(entry_point.get("date") or ""),
            "entryPrice": round(entry_price, 4),
            "entryReason": str(entry.get("reason") or ""),
        }
    )

    earliest_exit_index = entry_index + 1
    timeout_days = _trade_timeout_days(plan_info.get("horizonType"))
    timeout_index = entry_index + timeout_days - 1
    if earliest_exit_index >= len(points):
        row.update({"status": "pending", "reason": "已触发入场，但尚未到 T+1 可卖日。"})
        return row
    if timeout_index >= len(points):
        available_days = max(0, len(points) - entry_index)
        row.update({"status": "pending", "reason": f"已触发入场，但尚未走满 {timeout_days} 个交易日，当前可用 {available_days} 个交易日。"})
        return row

    exit_result = _exit_trade(points, entry_index, timeout_index, entry_price, stop_loss, take_profit)
    if exit_result.get("status") != "done":
        row.update({"status": exit_result.get("status", "pending"), "reason": exit_result.get("reason", "")})
        return row

    exit_index = int(exit_result["exitIndex"])
    holding_points = points[entry_index : exit_index + 1]
    min_low = min((_positive_number(point.get("low")) or entry_price) for point in holding_points)
    max_high = max((_positive_number(point.get("high")) or entry_price) for point in holding_points)
    exit_price = _positive_number(exit_result.get("exitPrice"))
    row.update(
        {
            "exitDate": str(points[exit_index].get("date") or ""),
            "exitPrice": round(exit_price, 4) if exit_price else None,
            "exitReason": str(exit_result.get("exitReason") or ""),
            "returnPct": round((exit_price / entry_price - 1) * 100, 2) if exit_price else None,
            "maxDrawdownPct": round((min_low / entry_price - 1) * 100, 2),
            "maxRunupPct": round((max_high / entry_price - 1) * 100, 2),
            "status": "done",
            "reason": str(exit_result.get("reason") or ""),
        }
    )
    row["label"] = "plan_success" if (_safe_float(row.get("returnPct")) or 0) > 0 else "plan_failed"
    return row


def _extract_executable_trade_plan(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    try:
        decision = json.loads(snapshot.get("decision_json") or "{}")
    except json.JSONDecodeError:
        decision = {}
    if not isinstance(decision, dict):
        decision = {}
    validation = decision.get("tradePlanValidation") if isinstance(decision.get("tradePlanValidation"), dict) else {}
    if snapshot.get("decision") != "buy" or validation.get("valid") is not True:
        return {
            "valid": False,
            "planVersion": "new_position_plan_v2",
            "horizonType": str(snapshot.get("primary_horizon") or "none"),
            "label": "plan_unverifiable",
            "reason": "仅对 decision=buy 且 tradePlanValidation.valid=true 的样本做策略回测。",
        }
    plan = decision.get("newPositionPlan") if isinstance(decision.get("newPositionPlan"), dict) else {}
    plan_version = str(plan.get("planVersion") or "new_position_plan_v2")
    if not plan:
        plan = decision.get("tradePlan") if isinstance(decision.get("tradePlan"), dict) else {}
        plan_version = "legacy_tradePlan"
    horizon = str(plan.get("horizon") or snapshot.get("primary_horizon") or "short")
    if horizon not in {"short", "mid", "long"}:
        horizon = "short"
    has_prices = _positive_number(plan.get("buyPriceLow")) and _positive_number(plan.get("buyPriceHigh")) and _positive_number(plan.get("stopLossPrice"))
    if not has_prices:
        return {
            "valid": False,
            "planVersion": plan_version,
            "horizonType": horizon,
            "label": "legacy_plan_unverifiable" if plan_version == "legacy_tradePlan" else "plan_unverifiable",
            "reason": "交易计划缺少买入区间或止损价，无法执行策略回测。",
        }
    return {"valid": True, "plan": plan, "planVersion": plan_version, "horizonType": horizon}


def _empty_trade_outcome_row(snapshot: Dict[str, Any], plan_info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "snapshotId": snapshot["id"],
        "code": snapshot["code"],
        "name": snapshot.get("name", ""),
        "recommendationDate": snapshot["analysis_date"],
        "riskMode": snapshot.get("risk_mode", ""),
        "decision": snapshot.get("decision", ""),
        "planVersion": plan_info.get("planVersion") or "new_position_plan_v2",
        "horizonType": plan_info.get("horizonType") or snapshot.get("primary_horizon") or "short",
        "entryTriggered": False,
        "entryDate": "",
        "entryPrice": None,
        "entryReason": "",
        "exitDate": "",
        "exitPrice": None,
        "exitReason": "",
        "returnPct": None,
        "maxDrawdownPct": None,
        "maxRunupPct": None,
        "status": "pending",
        "label": "",
        "reason": "",
    }


def _entry_from_buy_range(point: Dict[str, Any], buy_low: float, buy_high: float) -> Dict[str, Any]:
    open_price = _positive_number(point.get("open"))
    high_price = _positive_number(point.get("high"))
    low_price = _positive_number(point.get("low"))
    if not high_price or not low_price:
        return {"triggered": False, "reason": "下一交易日高低价缺失"}
    if open_price and buy_low <= open_price <= buy_high:
        return {"triggered": True, "price": open_price, "reason": "下一交易日开盘价落在 AI 买入区间内。"}
    if high_price < buy_low or low_price > buy_high:
        return {"triggered": False, "reason": "下一交易日未触达 AI 买入区间。"}
    if open_price and open_price > buy_high and low_price <= buy_high:
        return {"triggered": True, "price": buy_high, "reason": "下一交易日回落触达买入区间上沿，按区间上沿模拟入场。"}
    if open_price and open_price < buy_low and high_price >= buy_low:
        return {"triggered": True, "price": buy_low, "reason": "下一交易日上穿买入区间下沿，按区间下沿模拟入场。"}
    midpoint = (buy_low + buy_high) / 2
    if low_price <= midpoint <= high_price:
        return {"triggered": True, "price": midpoint, "reason": "下一交易日盘中触达买入区间，按区间中值模拟入场。"}
    return {"triggered": True, "price": buy_low, "reason": "下一交易日触达买入区间边缘，按区间下沿模拟入场。"}


def _exit_trade(
    points: List[Dict[str, Any]],
    entry_index: int,
    timeout_index: int,
    entry_price: float,
    stop_loss: float,
    take_profit: Optional[float],
) -> Dict[str, Any]:
    for index in range(entry_index + 1, timeout_index + 1):
        point = points[index]
        open_price = _positive_number(point.get("open"))
        high_price = _positive_number(point.get("high"))
        low_price = _positive_number(point.get("low"))
        close_price = _positive_number(point.get("close"))
        if not high_price or not low_price:
            return {"status": "no_data", "reason": "持仓期高低价缺失"}
        if stop_loss and low_price <= stop_loss:
            exit_price = min(open_price or stop_loss, stop_loss)
            return {
                "status": "done",
                "exitIndex": index,
                "exitPrice": exit_price,
                "exitReason": "stop_loss",
                "reason": "T+1 后触发止损，按更保守的开盘价/止损价退出。",
            }
        if take_profit and high_price >= take_profit:
            exit_price = take_profit if not open_price or open_price < take_profit else take_profit
            return {
                "status": "done",
                "exitIndex": index,
                "exitPrice": exit_price,
                "exitReason": "take_profit",
                "reason": "T+1 后触发止盈，按止盈价退出。",
            }
        if index == timeout_index:
            if not close_price:
                return {"status": "no_data", "reason": "周期结束日收盘价缺失"}
            return {
                "status": "done",
                "exitIndex": index,
                "exitPrice": close_price,
                "exitReason": "timeout",
                "reason": "未触发止损止盈，按计划周期结束日收盘退出。",
            }
    return {"status": "pending", "reason": "尚未满足退出条件"}


def _trade_timeout_days(horizon_type: Any) -> int:
    return {"short": 5, "mid": 20, "long": 60}.get(str(horizon_type or "short"), 5)


def _build_trade(snapshot: Dict[str, Any], series: List[Dict[str, Any]], horizons: List[int]) -> Dict[str, Any]:
    points = _normalize_series(series)
    recommendation_date = str(snapshot.get("analysis_date") or "")
    buy_index = _next_trading_index(points, recommendation_date)
    trade = {"buyDate": "", "buyPrice": None, "results": {}}
    if buy_index is None:
        for horizon in horizons:
            trade["results"][str(horizon)] = {"status": "pending", "reason": "没有推荐日之后的下一交易日 K 线"}
        return trade
    buy_point = points[buy_index]
    buy_price = _positive_number(buy_point.get("open")) or _positive_number(buy_point.get("close"))
    trade["buyDate"] = buy_point.get("date", "")
    trade["buyPrice"] = buy_price
    if not buy_price:
        for horizon in horizons:
            trade["results"][str(horizon)] = {"status": "no_data", "reason": "买入价缺失"}
        return trade
    for horizon in horizons:
        exit_index = buy_index + horizon - 1
        if exit_index >= len(points):
            trade["results"][str(horizon)] = {
                "status": "pending",
                "reason": f"尚未走满 {horizon} 个交易日",
                "availableTradingDays": max(0, len(points) - buy_index),
            }
            continue
        exit_point = points[exit_index]
        exit_price = _positive_number(exit_point.get("close"))
        if not exit_price:
            trade["results"][str(horizon)] = {"status": "no_data", "reason": "卖出价缺失"}
            continue
        holding_points = points[buy_index : exit_index + 1]
        min_low = min((_positive_number(point.get("low")) or buy_price) for point in holding_points)
        max_high = max((_positive_number(point.get("high")) or buy_price) for point in holding_points)
        trade["results"][str(horizon)] = {
            "status": "done",
            "exitDate": exit_point.get("date", ""),
            "exitPrice": round(exit_price, 4),
            "returnPct": round((exit_price / buy_price - 1) * 100, 2),
            "maxDrawdownPct": round((min_low / buy_price - 1) * 100, 2),
            "maxRunupPct": round((max_high / buy_price - 1) * 100, 2),
        }
    return trade


def _effective_snapshot_filter_sql(table_name: str = "stock_analysis_snapshots") -> str:
    return f"""
        SELECT MAX(effective_s.id)
        FROM {table_name} effective_s
        GROUP BY effective_s.code, effective_s.analysis_date, effective_s.risk_mode
    """


def _dedupe_cases_by_code_window(cases: List[Dict[str, Any]], window_days: int) -> List[Dict[str, Any]]:
    if window_days <= 0:
        return cases
    selected: List[Dict[str, Any]] = []
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for case in cases:
        by_code.setdefault(str(case.get("code") or ""), []).append(case)
    for _, rows in by_code.items():
        ordered = sorted(rows, key=_case_rank_key, reverse=True)
        kept_for_code: List[Dict[str, Any]] = []
        for item in ordered:
            item_date = _parse_date(item.get("analysisDate") or item.get("analysis_date"))
            if item_date and any(_dates_close(item_date, _parse_date(kept.get("analysisDate") or kept.get("analysis_date")), window_days) for kept in kept_for_code):
                continue
            kept_for_code.append(item)
        selected.extend(kept_for_code)
    return sorted(selected, key=_case_rank_key, reverse=True)


def _case_rank_key(item: Dict[str, Any]) -> tuple[float, str]:
    label_score = _label_priority(str(item.get("label") or ""))
    magnitude = _case_return_magnitude(item)
    date_text = str(item.get("analysisDate") or item.get("analysis_date") or "")
    similarity = _safe_float(item.get("similarity")) or 0.0
    return (float(label_score) * 1000 + magnitude * 10 + similarity, date_text)


def _case_return_magnitude(item: Dict[str, Any]) -> float:
    values: List[float] = []
    for key in ("returnPct", "maxDrawdownPct", "maxRunupPct"):
        number = _safe_float(item.get(key))
        if number is not None:
            values.append(abs(number))
    outcomes = item.get("outcomes")
    if isinstance(outcomes, dict):
        for key, value in outcomes.items():
            if "ReturnPct" in str(key):
                number = _safe_float(value)
                if number is not None:
                    values.append(abs(number))
    return max(values) if values else 0.0


def _parse_date(value: Any) -> Optional[date]:
    text = _normalize_date(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _dates_close(left: date, right: Optional[date], window_days: int) -> bool:
    if not right:
        return False
    return abs((left - right).days) < window_days


def _label_for(
    decision: str,
    horizon: int,
    results: Dict[str, Any],
    config: Dict[str, Any],
    *,
    bucket: str = "short",
) -> str:
    result = results.get(str(horizon), {})
    if result.get("status") != "done":
        return ""
    ret = _safe_float(result.get("returnPct")) or 0
    drawdown = _safe_float(result.get("maxDrawdownPct")) or 0
    if bucket in {"mid", "long"}:
        return _longer_horizon_label(decision, bucket, ret, drawdown)
    five_day = results.get("5", {})
    five_ret = _safe_float(five_day.get("returnPct")) if five_day.get("status") == "done" else None
    ten_day = results.get("10", {})
    ten_ret = _safe_float(ten_day.get("returnPct")) if ten_day.get("status") == "done" else None
    if decision == "buy":
        if ret <= config["falseBuyReturnPct"] or drawdown <= config["falseBuyDrawdownPct"]:
            return "false_buy"
        if horizon == 3 and ret >= config["success3dReturnPct"]:
            return "correct_buy"
        if horizon == 10 and ret >= config["success10dReturnPct"]:
            return "correct_buy"
        return "neutral_buy"
    if five_ret is not None and five_ret >= config["missed5dReturnPct"]:
        return "missed_opportunity"
    if ten_ret is not None and ten_ret >= config["success10dReturnPct"]:
        return "early_watch" if decision == "watch" else "missed_opportunity"
    if ret < 0 or drawdown <= config["falseBuyDrawdownPct"]:
        return "correct_avoid" if decision == "avoid" else "risk_saved"
    return "neutral_watch"


def _longer_horizon_label(decision: str, bucket: str, return_pct: float, drawdown_pct: float) -> str:
    positive_threshold = 5.0 if bucket == "mid" else 10.0
    negative_threshold = -6.0 if bucket == "mid" else -12.0
    drawdown_threshold = -10.0 if bucket == "mid" else -20.0
    prefix = f"{bucket}_direction"
    if decision == "buy":
        if return_pct <= negative_threshold or drawdown_pct <= drawdown_threshold:
            return f"{prefix}_negative"
        if return_pct >= positive_threshold:
            return f"{prefix}_positive"
        return f"{prefix}_neutral"
    if return_pct >= positive_threshold:
        return f"{prefix}_missed"
    if return_pct <= 0 or drawdown_pct <= drawdown_threshold:
        return f"{prefix}_protected"
    return f"{prefix}_neutral"


def _horizon_bucket(horizon: Any) -> str:
    try:
        value = int(horizon)
    except (TypeError, ValueError):
        return "other"
    if value in SHORT_HORIZONS:
        return "short"
    if value in MID_HORIZONS:
        return "mid"
    if value in LONG_HORIZONS:
        return "long"
    return "other"


def _horizons_for_bucket(bucket: str) -> tuple[int, ...]:
    value = str(bucket or "short").strip().lower()
    if value in V2_HORIZON_BUCKETS:
        return tuple(V2_HORIZON_BUCKETS[value])
    return SHORT_HORIZONS


def _case_bucket_status(
    mode: str,
    sample_pool: Dict[str, Any],
    similar_cases: List[Dict[str, Any]],
    injected: bool,
    config: Dict[str, Any],
) -> str:
    if mode == "off":
        return "disabled"
    if int(sample_pool.get("effectiveSampleCount") or 0) < int(config["minSamples"]):
        return "collecting"
    if not similar_cases:
        return "insufficient_match"
    if injected:
        return "injected"
    return "available"


def _sample_pool_stats(
    connection: Any,
    snapshots_table: str,
    outcomes_table: str,
    effective_sql: str,
    bucket: str,
    horizons: tuple[int, ...],
) -> Dict[str, Any]:
    placeholders = ",".join("?" for _ in horizons)
    if not placeholders:
        return {
            "key": bucket,
            "horizons": [],
            "effectiveSampleCount": 0,
            "labelCounts": {},
            "buySampleCount": 0,
            "falseBuyCount": 0,
            "missedOpportunityCount": 0,
            "positiveCount": 0,
            "negativeCount": 0,
            "protectedCount": 0,
        }
    horizon_params = list(horizons)
    effective = connection.execute(
        f"""
        SELECT COUNT(DISTINCT o.snapshot_id) AS count
        FROM {outcomes_table} o
        JOIN {snapshots_table} s ON s.id = o.snapshot_id
        WHERE o.status = 'done' AND o.horizon IN ({placeholders}) AND s.id IN ({effective_sql})
        """,
        horizon_params,
    ).fetchone()["count"]
    labels = connection.execute(
        f"""
        SELECT o.label, COUNT(*) AS count
        FROM {outcomes_table} o
        JOIN {snapshots_table} s ON s.id = o.snapshot_id
        WHERE o.status = 'done' AND o.horizon IN ({placeholders})
          AND o.label != '' AND s.id IN ({effective_sql})
        GROUP BY o.label
        """,
        horizon_params,
    ).fetchall()
    buy_total = connection.execute(
        f"""
        SELECT COUNT(DISTINCT s.id) AS count
        FROM {snapshots_table} s
        JOIN {outcomes_table} o ON o.snapshot_id = s.id
        WHERE s.decision = 'buy' AND o.status = 'done'
          AND o.horizon IN ({placeholders}) AND s.id IN ({effective_sql})
        """,
        horizon_params,
    ).fetchone()["count"]
    label_counts = {row["label"]: row["count"] for row in labels}
    positive_count = sum(int(label_counts.get(label, 0)) for label in ("correct_buy", f"{bucket}_direction_positive"))
    negative_count = sum(int(label_counts.get(label, 0)) for label in ("false_buy", f"{bucket}_direction_negative"))
    missed_count = sum(int(label_counts.get(label, 0)) for label in ("missed_opportunity", f"{bucket}_direction_missed"))
    protected_count = sum(int(label_counts.get(label, 0)) for label in ("risk_saved", "correct_avoid", f"{bucket}_direction_protected"))
    return {
        "key": bucket,
        "horizons": list(horizons),
        "effectiveSampleCount": int(effective or 0),
        "labelCounts": label_counts,
        "buySampleCount": int(buy_total or 0),
        "falseBuyCount": int(label_counts.get("false_buy", 0)),
        "missedOpportunityCount": missed_count,
        "positiveCount": positive_count,
        "negativeCount": negative_count,
        "protectedCount": protected_count,
        "rowCount": 0,
        "doneOutcomeCount": 0,
        "pendingOutcomeCount": 0,
        "noDataOutcomeCount": 0,
    }


def _outcome_bucket_stats(rows: List[Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        key: {
            "horizons": list(horizons),
            "rowCount": 0,
            "doneCount": 0,
            "pendingCount": 0,
            "noDataCount": 0,
        }
        for key, horizons in V2_HORIZON_BUCKETS.items()
    }
    for row in rows:
        bucket = _horizon_bucket(row["horizon"])
        if bucket not in result:
            continue
        count = int(row["count"] or 0)
        result[bucket]["rowCount"] += count
        if row["status"] == "done":
            result[bucket]["doneCount"] += count
        elif row["status"] == "pending":
            result[bucket]["pendingCount"] += count
        else:
            result[bucket]["noDataCount"] += count
    return result


def _normalize_horizons(horizons: List[int] | None) -> List[int]:
    values = horizons or list(DEFAULT_HORIZONS)
    result = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result.append(number)
    return sorted(set(result)) or list(DEFAULT_HORIZONS)


def _normalize_outcome_limit(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 500
    return max(1, min(5000, number))


def _normalize_outcome_mode(value: Any) -> str:
    text = str(value or "backfill").strip().lower()
    if text in {"backfill", "missing", "pending"}:
        return "backfill"
    if text in {"range", "recent", "latest", "force"}:
        return text
    return "backfill"


def _normalize_series(series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    points = []
    for point in series:
        normalized_date = _normalize_date(point.get("date"))
        if not normalized_date:
            continue
        item = dict(point)
        item["date"] = normalized_date
        points.append(item)
    return sorted(points, key=lambda item: item["date"])


def _next_trading_index(points: List[Dict[str, Any]], recommendation_date: str) -> Optional[int]:
    for index, point in enumerate(points):
        if str(point.get("date", "")) > recommendation_date:
            return index
    return None


def _features_from_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = evidence.get("snapshot") or {}
    intraday = evidence.get("intraday") or {}
    daily = evidence.get("daily") or {}
    metrics = daily.get("dailyMetrics") or {}
    trend = daily.get("dailyTrend") or {}
    market = evidence.get("market") or {}
    sector = evidence.get("sectorHeat") or {}
    money_flow = evidence.get("moneyFlow") or {}
    valuation = evidence.get("valuation") or {}
    risks = list(snapshot.get("riskFlags") or []) + list(daily.get("riskFlags") or [])
    return {
        "code": snapshot.get("code", ""),
        "sector": snapshot.get("sector", ""),
        "theme": snapshot.get("theme", ""),
        "change": _safe_float(snapshot.get("change")),
        "turnover": _safe_float(snapshot.get("turnover")),
        "volumeRatio": _safe_float(snapshot.get("volumeRatio")),
        "intradayScore": _safe_float(intraday.get("score")),
        "intradayStatus": intraday.get("status", ""),
        "dailyScore": _safe_float(daily.get("score")),
        "dailyStatus": daily.get("status", ""),
        "aboveMa20": bool(trend.get("aboveMa20")),
        "aboveMa60": bool(trend.get("aboveMa60")),
        "return20d": _safe_float(metrics.get("return20d")),
        "distanceMa20": _safe_float(metrics.get("distanceMa20")),
        "marketMode": market.get("mode", ""),
        "sectorRank": int(_safe_float(sector.get("currentRank")) or 0),
        "mainNetInflow": _safe_float(money_flow.get("mainNetInflow")),
        "peTtm": _safe_float(valuation.get("peTtm")),
        "riskFlags": sorted({str(item) for item in risks if str(item or "").strip()}),
    }


def _similarity(target: Dict[str, Any], other: Dict[str, Any]) -> tuple[float, List[str], List[str]]:
    score = 0.0
    total = 0.0
    similarities: List[str] = []
    differences: List[str] = []

    def add(weight: float, ok: bool, same: str, diff: str = "") -> None:
        nonlocal score, total
        total += weight
        if ok:
            score += weight
            if same:
                similarities.append(same)
        elif diff:
            differences.append(diff)

    add(12, target.get("sector") == other.get("sector") and bool(target.get("sector")), f"同属 {target.get('sector')} 板块", "板块不同")
    add(8, target.get("marketMode") == other.get("marketMode"), f"市场模式同为 {target.get('marketMode')}", "市场模式不同")
    add(12, _bucket(target.get("dailyScore"), [55, 70, 82]) == _bucket(other.get("dailyScore"), [55, 70, 82]), "日线分数区间接近", "日线强弱区间不同")
    add(10, target.get("aboveMa20") == other.get("aboveMa20"), "MA20 站上状态一致", "MA20 状态不同")
    add(10, target.get("aboveMa60") == other.get("aboveMa60"), "MA60 站上状态一致", "MA60 状态不同")
    add(10, _bucket(target.get("intradayScore"), [60, 70, 78]) == _bucket(other.get("intradayScore"), [60, 70, 78]), "15m 分数区间接近", "15m 分数区间不同")
    add(8, _bucket(target.get("volumeRatio"), [1.2, 2.0, 3.0]) == _bucket(other.get("volumeRatio"), [1.2, 2.0, 3.0]), "量比区间接近", "量比差异较大")
    add(8, _bucket(target.get("return20d"), [-5, 5, 15]) == _bucket(other.get("return20d"), [-5, 5, 15]), "20日涨跌幅区间接近", "20日位置不同")
    add(7, _bucket(target.get("distanceMa20"), [-3, 0, 8]) == _bucket(other.get("distanceMa20"), [-3, 0, 8]), "距 MA20 位置接近", "距 MA20 位置不同")
    target_risks = set(target.get("riskFlags") or [])
    other_risks = set(other.get("riskFlags") or [])
    risk_overlap = bool(target_risks & other_risks) or (not target_risks and not other_risks)
    add(8, risk_overlap, "风险标签结构接近", "风险标签不同")
    add(7, _same_flow_side(target.get("mainNetInflow"), other.get("mainNetInflow")), "主力资金方向一致", "主力资金方向不同")
    return (score / total if total else 0.0), similarities, differences


def _case_prompt_config(settings: Settings, ui_config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    configured = settings.stock_case_prompt if isinstance(settings.stock_case_prompt, dict) else {}
    result = dict(DEFAULT_CASE_PROMPT_CONFIG)
    result.update({key: value for key, value in configured.items() if key in result})
    result["mode"] = os.getenv("STOCK_CASE_PROMPT_MODE", str(result["mode"])).strip().lower()
    result["minSamples"] = _env_int("STOCK_CASE_PROMPT_MIN_SAMPLES", int(result["minSamples"]))
    result["minSimilarity"] = _env_float("STOCK_CASE_PROMPT_MIN_SIMILARITY", float(result["minSimilarity"]))
    result["minMatchedCases"] = _env_int("STOCK_CASE_PROMPT_MIN_MATCHED_CASES", int(result["minMatchedCases"]))
    result["recentFalseBuyWindow"] = _env_int("STOCK_CASE_RECENT_FALSE_BUY_WINDOW", int(result["recentFalseBuyWindow"]))
    result["falseBuyAllowedDegradePoints"] = _env_float(
        "STOCK_CASE_FALSE_BUY_ALLOWED_DEGRADE_POINTS",
        float(result["falseBuyAllowedDegradePoints"]),
    )
    result["falseBuyAbsoluteLimitPct"] = _env_float("STOCK_CASE_FALSE_BUY_ABSOLUTE_LIMIT_PCT", float(result["falseBuyAbsoluteLimitPct"]))
    result["success3dReturnPct"] = _env_float("STOCK_CASE_SUCCESS_3D_RETURN_PCT", float(result["success3dReturnPct"]))
    result["success10dReturnPct"] = _env_float("STOCK_CASE_SUCCESS_10D_RETURN_PCT", float(result["success10dReturnPct"]))
    result["falseBuyReturnPct"] = _env_float("STOCK_CASE_FALSE_BUY_RETURN_PCT", float(result["falseBuyReturnPct"]))
    result["falseBuyDrawdownPct"] = _env_float("STOCK_CASE_FALSE_BUY_DRAWDOWN_PCT", float(result["falseBuyDrawdownPct"]))
    result["missed5dReturnPct"] = _env_float("STOCK_CASE_MISSED_5D_RETURN_PCT", float(result["missed5dReturnPct"]))
    result["caseDedupeWindowDays"] = _env_int("STOCK_CASE_DEDUPE_WINDOW_DAYS", int(result["caseDedupeWindowDays"]))
    if isinstance(ui_config, dict):
        result.update({key: value for key, value in ui_config.items() if key in result})
    return _normalize_case_prompt_config(result)


def _normalize_case_prompt_config(config: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(DEFAULT_CASE_PROMPT_CONFIG)
    result.update({key: value for key, value in config.items() if key in result})
    result["mode"] = str(result.get("mode") or "auto").strip().lower()
    if result["mode"] not in {"off", "preview", "auto", "on"}:
        result["mode"] = "auto"
    for key in ("minSamples", "minMatchedCases", "recentFalseBuyWindow", "caseDedupeWindowDays"):
        try:
            result[key] = max(0, int(result[key]))
        except (TypeError, ValueError):
            result[key] = int(DEFAULT_CASE_PROMPT_CONFIG[key])
    for key in (
        "minSimilarity",
        "falseBuyAllowedDegradePoints",
        "falseBuyAbsoluteLimitPct",
        "success3dReturnPct",
        "success10dReturnPct",
        "falseBuyReturnPct",
        "falseBuyDrawdownPct",
        "missed5dReturnPct",
    ):
        try:
            result[key] = float(result[key])
        except (TypeError, ValueError):
            result[key] = float(DEFAULT_CASE_PROMPT_CONFIG[key])
    result["minSimilarity"] = max(0.0, min(1.0, result["minSimilarity"]))
    return result


def _prompt_eligible(stats: Dict[str, Any], config: Dict[str, Any], similar_cases: List[Dict[str, Any]]) -> bool:
    prompt_pool_key = str(stats.get("promptSamplePool") or stats.get("promptOutcomeBucket") or "short")
    return _prompt_eligible_for_bucket(stats, config, similar_cases, prompt_pool_key)


def _prompt_eligible_for_bucket(stats: Dict[str, Any], config: Dict[str, Any], similar_cases: List[Dict[str, Any]], bucket: str) -> bool:
    prompt_pool_key = str(bucket or "short")
    prompt_pool = (stats.get("samplePools") or {}).get(prompt_pool_key, {}) if isinstance(stats.get("samplePools"), dict) else {}
    effective_count = (
        int(prompt_pool.get("effectiveSampleCount") or 0)
        if prompt_pool
        else int(stats.get("effectiveSampleCount", 0) or 0)
    )
    return (
        effective_count >= config["minSamples"]
        and len(similar_cases) >= config["minMatchedCases"]
        and stats.get("falseBuyGuard", {}).get("ok", False)
    )


def _false_buy_guard_ok(baseline: Optional[float], recent: Optional[float], config: Dict[str, Any]) -> bool:
    if recent is None:
        return True
    if recent > config["falseBuyAbsoluteLimitPct"]:
        return False
    if baseline is None:
        return True
    return recent <= baseline + config["falseBuyAllowedDegradePoints"]


def _rate(count: int, total: int) -> Optional[float]:
    if not total:
        return None
    return round(count / total * 100, 2)


def _report_row(row: Any) -> Dict[str, Any]:
    try:
        report = json.loads(row["report_json"] or "{}")
    except json.JSONDecodeError:
        report = {}
    return {
        "id": row["id"],
        "reviewDate": row["review_date"],
        "periodStart": row["period_start"],
        "periodEnd": row["period_end"],
        "sampleCount": row["sample_count"],
        "falseBuyCount": row["false_buy_count"],
        "missedOpportunityCount": row["missed_opportunity_count"],
        "summary": report.get("summary", ""),
        "createdAt": row["created_at"],
    }


def _lesson_row(row: Any) -> Dict[str, Any]:
    pattern = _json_dict(row["pattern_json"])
    evidence = _json_list(row["evidence_json"])
    quality = _lesson_quality(pattern, evidence, row["prompt_instruction"])
    return {
        "id": row["id"],
        "lesson_key": row["lesson_key"],
        "lessonKey": row["lesson_key"],
        "title": row["title"],
        "status": row["status"],
        "pattern": pattern,
        "patternSummary": _pattern_summary(pattern),
        "prompt_instruction": row["prompt_instruction"],
        "promptInstruction": row["prompt_instruction"],
        "evidenceCount": len(evidence),
        "sourceReportId": row["source_report_id"],
        "quality": quality,
        "updated_at": row["updated_at"],
        "updatedAt": row["updated_at"],
    }


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _find_report_lesson(report_json: Any, lesson_key: str, title: str) -> Dict[str, Any]:
    try:
        report = json.loads(report_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    lessons = report.get("lessons") if isinstance(report.get("lessons"), list) else []
    normalized_key = _lesson_key(lesson_key)
    normalized_title = str(title or "").strip()
    for item in lessons:
        if not isinstance(item, dict):
            continue
        item_key = _lesson_key(item.get("lessonKey") or item.get("title") or "")
        item_title = str(item.get("title") or "").strip()
        if item_key == normalized_key or (normalized_title and item_title == normalized_title):
            return item
    return {}


def _normalize_lesson_pattern(raw_pattern: Any, *, lesson_key: str, title: str, instruction: str) -> Dict[str, Any]:
    pattern: Dict[str, Any] = {}
    if isinstance(raw_pattern, dict):
        pattern.update(_filter_lesson_pattern(raw_pattern))
        raw_text = " ".join([str(title or ""), str(instruction or ""), json.dumps(raw_pattern, ensure_ascii=False)])
    else:
        raw_text = " ".join([str(title or ""), str(raw_pattern or ""), str(instruction or "")])
    inferred = _infer_lesson_pattern_from_text(raw_text, lesson_key=lesson_key)
    for key, value in inferred.items():
        pattern.setdefault(key, value)
    if "label" not in pattern:
        if "false" in str(lesson_key).lower() or "误买" in raw_text:
            pattern["label"] = "false_buy"
        elif "missed" in str(lesson_key).lower() or "漏判" in raw_text or "错失" in raw_text:
            pattern["label"] = "missed_opportunity"
    return _filter_lesson_pattern(pattern)


def _filter_lesson_pattern(pattern: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key in ("label", "decision"):
        value = str(pattern.get(key) or "").strip()
        if value:
            result[key] = value
    for key in ("riskLabels", "dailyStatuses", "marketModes"):
        values = _as_text_list(pattern.get(key))
        if values:
            result[key] = values[:8]
    for key in ("aboveMa20", "aboveMa60"):
        if key in pattern:
            result[key] = bool(pattern.get(key))
    for key in (
        "dailyScoreMin",
        "dailyScoreMax",
        "intradayScoreMin",
        "turnoverMin",
        "turnoverMax",
        "changeMin",
        "changeMax",
        "volumeRatioMin",
        "return20dMin",
        "return20dMax",
        "distanceMa20Min",
        "distanceMa20Max",
        "mainNetInflowMin",
    ):
        number = _safe_float(pattern.get(key))
        if number is not None:
            result[key] = number
    return result


def _infer_lesson_pattern_from_text(text: str, *, lesson_key: str) -> Dict[str, Any]:
    compact = str(text or "")
    lower = compact.lower()
    result: Dict[str, Any] = {}
    if "defensive" in lower or "防御" in compact:
        result["marketModes"] = ["defensive"]
    if "daily-risk" in lower or "日线风险" in compact:
        result["dailyStatuses"] = ["daily-risk"]
    risk_labels = []
    for word in ("远离MA20", "连续大涨", "高位", "高位震荡", "放量滞涨", "追高"):
        if word in compact:
            risk_labels.append(word)
    if risk_labels:
        result["riskLabels"] = sorted(set(risk_labels))
    if "换手" in compact:
        result["turnoverMin"] = _threshold_from_text(compact, default=10.0)
    if "涨幅" in compact or "追高" in compact or "接近涨停" in compact:
        result["changeMin"] = _threshold_from_text(compact, default=7.0)
    if "放量" in compact or "量价" in compact:
        result["volumeRatioMin"] = 1.2
    if "主力净流入" in compact or "资金活跃" in compact:
        result["mainNetInflowMin"] = 0.0
    if "站上ma20" in lower:
        result["aboveMa20"] = True
    if "站上ma60" in lower:
        result["aboveMa60"] = True
    if "跌破ma60" in lower or "均线空头" in compact:
        result.setdefault("dailyStatuses", ["daily-risk"])
    if not result and ("missed" in str(lesson_key).lower() or "漏判" in compact):
        result["dailyScoreMin"] = 70.0
    return result


def _threshold_from_text(text: str, *, default: float) -> float:
    numbers = [_safe_float(match) for match in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*%", text)]
    values = [number for number in numbers if number is not None]
    return float(values[0]) if values else default


def _normalize_lesson_evidence(raw_evidence: Any) -> List[Dict[str, Any]]:
    items: List[Any]
    if isinstance(raw_evidence, list):
        items = raw_evidence
    elif raw_evidence:
        items = [raw_evidence]
    else:
        items = []
    result: List[Dict[str, Any]] = []
    for item in items[:8]:
        if isinstance(item, dict):
            normalized = {
                "snapshotId": item.get("snapshotId"),
                "code": item.get("code", ""),
                "name": item.get("name", ""),
                "analysisDate": item.get("analysisDate", ""),
                "label": item.get("label", ""),
                "horizon": item.get("horizon"),
                "returnPct": item.get("returnPct"),
                "reason": item.get("reason", item.get("text", "")),
            }
            result.append({key: value for key, value in normalized.items() if value not in (None, "")})
        else:
            text = str(item or "").strip()
            if text:
                result.append({"text": text})
    return result


def _lesson_quality(pattern: Dict[str, Any], evidence: List[Any], instruction: Any) -> Dict[str, Any]:
    condition_count = _pattern_condition_count(pattern)
    evidence_count = len(evidence)
    has_instruction = bool(str(instruction or "").strip())
    if condition_count >= 2 and evidence_count > 0 and has_instruction:
        status = "complete"
        warning = "结构完整，可谨慎启用"
    elif condition_count > 0 and has_instruction:
        status = "partial"
        warning = "匹配条件或案例证据偏少，建议补充后再启用"
    elif has_instruction:
        status = "generic"
        warning = "缺少结构化匹配条件，启用后会影响范围过宽"
    else:
        status = "empty"
        warning = "缺少可注入规则"
    return {
        "status": status,
        "safeToEnable": status == "complete",
        "conditionCount": condition_count,
        "evidenceCount": evidence_count,
        "warning": warning,
    }


def _pattern_condition_count(pattern: Dict[str, Any]) -> int:
    count = 0
    for key in ("riskLabels", "dailyStatuses", "marketModes"):
        if _as_text_list(pattern.get(key)):
            count += 1
    for key in (
        "aboveMa20",
        "aboveMa60",
        "dailyScoreMin",
        "dailyScoreMax",
        "intradayScoreMin",
        "turnoverMin",
        "turnoverMax",
        "changeMin",
        "changeMax",
        "volumeRatioMin",
        "return20dMin",
        "return20dMax",
        "distanceMa20Min",
        "distanceMa20Max",
        "mainNetInflowMin",
    ):
        if key in pattern:
            count += 1
    return count


def _pattern_summary(pattern: Dict[str, Any]) -> str:
    parts = []
    if pattern.get("riskLabels"):
        parts.append("风险标签:" + "/".join(_as_text_list(pattern.get("riskLabels"))[:3]))
    if pattern.get("dailyStatuses"):
        parts.append("日线:" + "/".join(_as_text_list(pattern.get("dailyStatuses"))[:3]))
    if pattern.get("marketModes"):
        parts.append("市场:" + "/".join(_as_text_list(pattern.get("marketModes"))[:3]))
    for key, label in (
        ("turnoverMin", "换手>="),
        ("changeMin", "涨幅>="),
        ("volumeRatioMin", "量比>="),
        ("dailyScoreMin", "日线分>="),
        ("intradayScoreMin", "日内分>="),
    ):
        if key in pattern:
            parts.append(f"{label}{pattern[key]}")
    return "；".join(parts[:5]) or "缺少结构化匹配条件"


def _as_text_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _rule_review_report(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    false_buy = [case for case in cases if case.get("label") == "false_buy"]
    missed = [case for case in cases if case.get("label") == "missed_opportunity"]
    lessons = []
    if false_buy:
        lessons.append(
            {
                "lessonKey": "false_buy_high_risk_guard",
                "title": "误买案例风险约束",
                "pattern": {"riskLabels": ["高位", "放量", "破位"]},
                "promptInstruction": "遇到高位放量、破位或资金明显流出时，即使短线分数较高，也必须降低买入结论并说明确认条件。",
                "evidence": [f"{case.get('code')} {case.get('returnPct')}%" for case in false_buy[:5]],
            }
        )
    if missed:
        lessons.append(
            {
                "lessonKey": "missed_strong_daily_watch",
                "title": "强日线漏判观察",
                "pattern": {"dailyScoreMin": 70, "aboveMa20": True, "aboveMa60": True},
                "promptInstruction": "遇到日线强、站上 MA20/MA60 且市场环境不弱的 watch/avoid 案例，应检查是否属于漏判风险，并给出可上调到 watch/buy 的触发条件。",
                "evidence": [f"{case.get('code')} {case.get('returnPct')}%" for case in missed[:5]],
            }
        )
    return {
        "summary": f"本次复盘 {len(cases)} 个失败/漏判样本，其中误买 {len(false_buy)} 个、漏判 {len(missed)} 个。",
        "falseBuyReasons": ["误买通常来自风险标签、回撤和资金方向没有被充分惩罚。"] if false_buy else [],
        "missedOpportunityReasons": ["漏判通常来自日线强势但短线确认不足时过度保守。"] if missed else [],
        "lessons": lessons,
    }


def _lesson_matches(features: Dict[str, Any], pattern: Dict[str, Any]) -> tuple[bool, str]:
    if not pattern:
        return True, "通用复盘经验"
    daily_min = _safe_float(pattern.get("dailyScoreMin"))
    if daily_min is not None and (_safe_float(features.get("dailyScore")) or 0) < daily_min:
        return False, ""
    daily_max = _safe_float(pattern.get("dailyScoreMax"))
    if daily_max is not None and (_safe_float(features.get("dailyScore")) or 0) > daily_max:
        return False, ""
    intraday_min = _safe_float(pattern.get("intradayScoreMin"))
    if intraday_min is not None and (_safe_float(features.get("intradayScore")) or 0) < intraday_min:
        return False, ""
    for feature_key, min_key, max_key in (
        ("turnover", "turnoverMin", "turnoverMax"),
        ("change", "changeMin", "changeMax"),
        ("return20d", "return20dMin", "return20dMax"),
        ("distanceMa20", "distanceMa20Min", "distanceMa20Max"),
    ):
        value = _safe_float(features.get(feature_key))
        min_value = _safe_float(pattern.get(min_key))
        max_value = _safe_float(pattern.get(max_key))
        if min_value is not None and (value is None or value < min_value):
            return False, ""
        if max_value is not None and (value is None or value > max_value):
            return False, ""
    volume_ratio_min = _safe_float(pattern.get("volumeRatioMin"))
    if volume_ratio_min is not None and ((_safe_float(features.get("volumeRatio")) or 0) < volume_ratio_min):
        return False, ""
    main_inflow_min = _safe_float(pattern.get("mainNetInflowMin"))
    if main_inflow_min is not None and ((_safe_float(features.get("mainNetInflow")) or 0) < main_inflow_min):
        return False, ""
    for key in ("aboveMa20", "aboveMa60"):
        if key in pattern and bool(features.get(key)) != bool(pattern.get(key)):
            return False, ""
    risk_labels = pattern.get("riskLabels") if isinstance(pattern.get("riskLabels"), list) else []
    if risk_labels:
        risks = " ".join(features.get("riskFlags") or [])
        if not any(str(label) in risks for label in risk_labels):
            return False, ""
    daily_statuses = _as_text_list(pattern.get("dailyStatuses"))
    if daily_statuses and str(features.get("dailyStatus") or "") not in daily_statuses:
        return False, ""
    market_modes = _as_text_list(pattern.get("marketModes"))
    if market_modes and str(features.get("marketMode") or "") not in market_modes:
        return False, ""
    return True, "当前证据与复盘经验形态匹配"


def _lesson_horizon_bucket(pattern: Dict[str, Any], evidence: List[Any]) -> str:
    labels = []
    for value in _as_text_list(pattern.get("label")):
        labels.append(value)
    for item in evidence if isinstance(evidence, list) else []:
        if isinstance(item, dict):
            labels.extend(_as_text_list(item.get("label")))
            if item.get("horizon") is not None:
                bucket = _horizon_bucket(item.get("horizon"))
                if bucket in {"short", "mid", "long"}:
                    return bucket
        elif isinstance(item, str):
            labels.append(item)
    text = " ".join(str(label or "") for label in labels)
    if "long_direction_" in text:
        return "long"
    if "mid_direction_" in text:
        return "mid"
    return "short"


def _outcome_summary(text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for part in str(text or "").split("|"):
        fields = part.split(":")
        if len(fields) < 3:
            continue
        horizon, ret, label = fields[:3]
        result[f"{horizon}dReturnPct"] = _safe_float(ret)
        if label and label not in result.get("labels", []):
            result.setdefault("labels", []).append(label)
    return result


def _dominant_label(text: str) -> str:
    labels = []
    for part in str(text or "").split("|"):
        fields = part.split(":")
        if len(fields) >= 3 and fields[2]:
            labels.append(fields[2])
    for label in ("false_buy", "missed_opportunity", "correct_buy", "correct_avoid", "early_watch", "risk_saved"):
        if label in labels:
            return label
    return labels[0] if labels else ""


def _case_lesson(decision: str, label: str, ai_decision: Dict[str, Any]) -> str:
    if label == "missed_opportunity":
        return "类似形态历史上出现过漏判，若当前日线和市场环境配合，应检查是否需要提高到 watch 或给出明确买入触发条件。"
    if label == "false_buy":
        return "类似形态历史上出现过误买，若当前也存在回撤、破位或资金风险，应维持谨慎。"
    if label == "correct_buy":
        return "类似形态历史买入后表现较好，可作为正向校准，但仍需满足当前入场和风控条件。"
    return str(ai_decision.get("decisionText") or f"原判断 {decision}，后续表现用于校准。")


def _label_priority(label: str) -> int:
    return {
        "missed_opportunity": 6,
        "false_buy": 5,
        "correct_buy": 4,
        "correct_avoid": 3,
        "early_watch": 2,
        "risk_saved": 1,
    }.get(label, 0)


def _bucket(value: Any, cuts: List[float]) -> int:
    number = _safe_float(value)
    if number is None:
        return -1
    for index, cut in enumerate(cuts):
        if number < cut:
            return index
    return len(cuts)


def _same_flow_side(left: Any, right: Any) -> bool:
    a = _safe_float(left)
    b = _safe_float(right)
    if a is None or b is None:
        return a is None and b is None
    return (a >= 0 and b >= 0) or (a < 0 and b < 0)


def _positive_number(value: Any) -> Optional[float]:
    number = _safe_float(value)
    return number if number is not None and number > 0 else None


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _normalize_date(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip().replace("/", "-")
    if "T" in text:
        text = text.split("T", 1)[0]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if len(text) >= 10:
        return text[:10]
    return ""


def _normalize_code(value: Any) -> str:
    match = re.search(r"\d{6}", str(value or ""))
    return match.group(0) if match else ""


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _lesson_key(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    return text[:80] or f"lesson_{int(time.time())}"


def _extract_json_object(content: str) -> Dict[str, Any]:
    text = str(content or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def append_review_log(message: str) -> None:
    REVIEW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REVIEW_LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
