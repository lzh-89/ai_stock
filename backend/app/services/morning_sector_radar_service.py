from __future__ import annotations

import http.client
import json
import logging
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.app.database import Database
from backend.app.market_context_store import FullMarketContextStore, market_context_meta
from backend.app.providers.sector_provider import SectorProviderError, get_sector_provider
from backend.app.services.ai_narrator import _chat_completions_url, _extract_chat_content, _http_error_message
from backend.app.settings import PROJECT_ROOT, Settings


LOGGER = logging.getLogger("ai_stock.morning_sector_radar")

DEFAULT_SCAN_TIME = "10:00"
DEFAULT_SECTOR_LIMIT = 20
DEFAULT_LEADER_LIMIT = 5
PROMPT_VERSION = "2026-07-16-morning-sector-radar-v1"
CACHE_PATH = PROJECT_ROOT / "data" / "morning_sector_radar_cache.json"
MOCK_CACHE_PATH = PROJECT_ROOT / "data" / "morning_sector_radar_mock_cache.json"
CACHE_VERSION = 1
AI_REVIEW_MAX_TOKENS = 4500
AI_REPAIR_MAX_TOKENS = 4500
RUN_TIMEOUT_SECONDS = 10 * 60


class AIJsonParseError(ValueError):
    pass


class MorningSectorRadarService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.market_context_store = FullMarketContextStore()
        self._lock = threading.Lock()
        self._threads: Dict[int, threading.Thread] = {}

    def start_run(
        self,
        sector_type: str = "all",
        source: str = "auto",
        market_source: str = "auto",
        scan_time: str = DEFAULT_SCAN_TIME,
        use_ai: bool = True,
        force: bool = False,
        sector_limit: int = DEFAULT_SECTOR_LIMIT,
        leader_limit: int = DEFAULT_LEADER_LIMIT,
    ) -> Dict[str, Any]:
        sector_type = _normalize_sector_type(sector_type)
        source = source if source in {"auto", "akshare", "mock"} else "auto"
        market_source = market_source if market_source in {"auto", "sina", "tencent", "akshare"} else "auto"
        scan_time = _normalize_scan_time(scan_time)
        sector_limit = _clamp_int(sector_limit, 5, 50, DEFAULT_SECTOR_LIMIT)
        leader_limit = _clamp_int(leader_limit, 3, 10, DEFAULT_LEADER_LIMIT)
        latest = self.database.latest_morning_sector_radar_run(sector_type=sector_type, scan_time=scan_time, requested_source=source)
        if latest and latest.get("status") in {"pending", "running"}:
            if _run_timed_out(latest):
                self._mark_run_timed_out(int(latest["id"]), latest)
            elif not force:
                self._resume_thread_if_needed(latest)
                return self.get_run(int(latest["id"]))
        run_id = self.database.create_morning_sector_radar_run(
            {
                "tradeDate": date.today().isoformat(),
                "scanTime": scan_time,
                "sectorType": sector_type,
                "requestedSource": source,
                "marketSource": market_source,
                "status": "pending",
                "selectionMode": "",
                "sampleRole": _sample_role(scan_time),
                "request": {
                    "sectorType": sector_type,
                    "source": source,
                    "marketSource": market_source,
                    "scanTime": scan_time,
                    "useAI": use_ai,
                    "sectorLimit": sector_limit,
                    "leaderLimit": leader_limit,
                },
            }
        )
        self._start_thread(run_id)
        return self.get_run(run_id)

    def latest(self, sector_type: str = "all", source: str = "auto", scan_time: str = DEFAULT_SCAN_TIME) -> Dict[str, Any]:
        normalized_source = source if source in {"auto", "akshare", "mock"} else "auto"
        run = self.database.latest_morning_sector_radar_run(
            sector_type=_normalize_sector_type(sector_type),
            scan_time=_normalize_scan_time(scan_time),
            requested_source=normalized_source,
        )
        if not run:
            return {"status": "empty", "sectors": [], "ruleSectors": [], "items": []}
        if normalized_source != "mock" and _is_mock_source(str(run.get("actualSource") or "")):
            return {
                "status": "empty",
                "sectors": [],
                "ruleSectors": [],
                "items": [],
                "warning": "最近一次结果来自 Mock 测试数据，已从真实板块结果中隔离，请重新生成。",
            }
        if run.get("status") in {"pending", "running"} and _run_timed_out(run):
            self._mark_run_timed_out(int(run["id"]), run)
            run = self.database.load_morning_sector_radar_run(int(run["id"])) or run
        self._resume_thread_if_needed(run)
        return self.get_run(int(run["id"]))

    def get_run(self, run_id: int) -> Dict[str, Any]:
        run = self.database.load_morning_sector_radar_run(run_id)
        if not run:
            raise ValueError(f"未找到早盘主线雷达任务 {run_id}")
        if run.get("status") in {"pending", "running"} and _run_timed_out(run):
            self._mark_run_timed_out(run_id, run)
            run = self.database.load_morning_sector_radar_run(run_id) or run
        sectors = self.database.load_morning_sector_radar_sectors(run_id)
        constituents = self.database.load_morning_sector_radar_constituents(run_id)
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in constituents:
            grouped.setdefault(f"{item.get('sectorType')}::{item.get('sectorCode')}", []).append(item)
        leader_limit = int((run.get("request") or {}).get("leaderLimit") or DEFAULT_LEADER_LIMIT)
        for sector in sectors:
            all_constituents = grouped.get(f"{sector.get('sectorType')}::{sector.get('sectorCode')}", [])
            sector["leaders"] = all_constituents[:leader_limit]
            sector["followers"] = all_constituents[leader_limit : leader_limit + 3]
            sector["constituentCount"] = len(all_constituents)
        selected = [item for item in sectors if item.get("selected")]
        selected = sorted(selected, key=lambda item: int(item.get("finalRank") or item.get("aiRank") or item.get("ruleRank") or 999))
        rule_sectors = sorted(sectors, key=lambda item: int(item.get("ruleRank") or 999))
        run["sectors"] = selected
        run["ruleSectors"] = rule_sectors[: int((run.get("request") or {}).get("sectorLimit") or DEFAULT_SECTOR_LIMIT)]
        run["items"] = sectors
        run["progressPct"] = _progress_pct(run)
        run["elapsedSeconds"] = _elapsed_seconds(run)
        return run

    def refresh_outcomes(
        self,
        run_id: int | None = None,
        date_from: str = "",
        date_to: str = "",
        batch_size: int = 200,
        dry_run: bool = False,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        rows = self.database.load_morning_sector_radar_pending_outcomes(
            run_id=run_id,
            date_from=_normalize_date_text(date_from),
            date_to=_normalize_date_text(date_to),
            limit=_clamp_int(batch_size, 1, 1000, 200),
        )
        snapshot_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        warnings: List[str] = []
        results = {"done": 0, "pending": 0, "no_data": 0, "excluded": 0}
        processed = 0
        for row in rows:
            outcome = self._build_outcome_update(row, snapshot_cache, warnings)
            status = str(outcome.get("status") or "pending")
            results[status] = results.get(status, 0) + 1
            processed += 1
            if not dry_run:
                self.database.update_morning_sector_radar_outcome_result(int(row["id"]), outcome)
        pending = self.database.count_morning_sector_radar_outcomes(status="pending", run_id=run_id)
        return {
            "status": "dry_run" if dry_run else "done",
            "runId": run_id or 0,
            "dryRun": dry_run,
            "processedCount": processed,
            "doneCount": results.get("done", 0),
            "pendingInBatchCount": results.get("pending", 0),
            "pendingCount": pending,
            "noDataCount": results.get("no_data", 0),
            "excludedCount": results.get("excluded", 0),
            "warnings": warnings[:8],
            "message": _outcome_refresh_message(processed, results, pending, dry_run),
        }

    def _build_outcome_update(
        self,
        row: Dict[str, Any],
        snapshot_cache: Dict[str, Dict[str, Dict[str, Any]]],
        warnings: List[str],
    ) -> Dict[str, Any]:
        base = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
        detail = {
            **base,
            "backfill": {
                "version": "2026-07-18-sector-outcome-v1",
                "updatedAt": datetime.now().isoformat(timespec="seconds"),
                "method": "sector_change_pct_snapshot_diff",
                "supportsIntradayPath": False,
            },
        }
        excluded_reason = _outcome_excluded_reason(row)
        if excluded_reason:
            detail["backfill"]["excludedReason"] = excluded_reason
            return {"status": "excluded", "label": "excluded", "reason": excluded_reason, "outcome": detail}

        reference = _optional_float_value(row.get("referenceValue"))
        if reference is None:
            reference = _optional_float_value(((base.get("sector") or {}) if isinstance(base.get("sector"), dict) else {}).get("changePct"))
        if reference is None:
            reason = "缺少运行时板块参考值，不能用收盘后数据倒推。"
            detail["backfill"]["noDataReason"] = reason
            return {"status": "no_data", "label": "weak", "reason": reason, "outcome": detail}

        no_data_reason = _outcome_no_data_reason(row)
        if no_data_reason:
            detail["backfill"]["noDataReason"] = no_data_reason
            return {"status": "no_data", "label": "weak", "reason": no_data_reason, "outcome": detail}

        maturity = _outcome_maturity(row)
        if maturity:
            detail["backfill"]["pendingReason"] = maturity
            return {"status": "pending", "label": "", "reason": maturity, "outcome": detail}

        sector_type = str(row.get("sectorType") or "")
        try:
            snapshot = snapshot_cache.get(sector_type)
            if snapshot is None:
                snapshot = self._current_sector_snapshot_by_code(sector_type)
                snapshot_cache[sector_type] = snapshot
        except Exception as error:  # noqa: BLE001
            message = f"{sector_type} 后续板块快照获取失败：{error}"
            if message not in warnings:
                warnings.append(message)
            detail["backfill"]["pendingReason"] = message
            return {"status": "pending", "label": "", "reason": message, "outcome": detail}

        current = snapshot.get(str(row.get("sectorCode") or "")) or snapshot.get(_sector_name_key(row))
        if not current:
            reason = "后续板块快照未匹配到同一板块代码或名称。"
            detail["backfill"]["noDataReason"] = reason
            return {"status": "no_data", "label": "weak", "reason": reason, "outcome": detail}

        close_value = _optional_float_value(current.get("changePct"))
        if close_value is None:
            reason = "后续板块快照缺少涨跌幅字段。"
            detail["backfill"]["noDataReason"] = reason
            return {"status": "no_data", "label": "weak", "reason": reason, "outcome": detail}

        close_return = round(close_value - reference, 4)
        benchmark = 0.0
        excess = round(close_return - benchmark, 4)
        label = "strong" if excess >= 1.0 else "weak" if excess <= -1.0 else "neutral"
        reason = "已按同口径板块快照回填收盘涨跌幅差值；严格分钟路径和次日开盘待后续历史源补齐。"
        detail["backfill"].update(
            {
                "status": "done",
                "referenceValue": reference,
                "closeValue": close_value,
                "closeReturnPct": close_return,
                "benchmarkCloseReturnPct": benchmark,
                "excessCloseReturnPct": excess,
                "benchmarkSource": "proxy_zero_until_index_source_ready",
                "matchedBy": "sector_code" if str(row.get("sectorCode") or "") in snapshot else "sector_name",
                "matchedSector": current,
                "proxyWarning": "当前以板块涨跌幅差值代理收益，不等同严格板块指数点位收益。",
            }
        )
        return {
            "referenceValue": reference,
            "closeValue": close_value,
            "closeReturnPct": close_return,
            "benchmarkCloseReturnPct": benchmark,
            "excessCloseReturnPct": excess,
            "status": "done",
            "label": label,
            "reason": reason,
            "outcome": detail,
        }

    def _current_sector_snapshot_by_code(self, sector_type: str) -> Dict[str, Dict[str, Any]]:
        payload = get_sector_provider("akshare").fetch_sector_snapshot(sector_type if sector_type in {"industry", "concept"} else "all")
        result: Dict[str, Dict[str, Any]] = {}
        for sector in payload.get("sectors") or []:
            if sector_type in {"industry", "concept"} and sector.get("sectorType") != sector_type:
                continue
            compact = {
                "sectorCode": sector.get("sectorCode") or "",
                "sectorName": sector.get("sectorName") or "",
                "sectorType": sector.get("sectorType") or "",
                "changePct": sector.get("changePct"),
                "generatedAt": payload.get("generatedAt"),
                "source": payload.get("source"),
            }
            if compact["sectorCode"]:
                result[str(compact["sectorCode"])] = compact
            name_key = _sector_name_key(compact)
            if name_key:
                result[name_key] = compact
        if not result:
            raise SectorProviderError("后续板块快照为空")
        return result

    def diagnostics(self, window_runs: int = 20) -> Dict[str, Any]:
        return self.database.morning_sector_radar_diagnostics(window_runs=_clamp_int(window_runs, 1, 120, 20))

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

    def _mark_run_timed_out(self, run_id: int, run: Dict[str, Any]) -> None:
        message = f"任务运行超过 {RUN_TIMEOUT_SECONDS // 60} 分钟仍停留在 {run.get('currentStage') or 'unknown'}，系统判定为超时。"
        self.database.update_morning_sector_radar_run(
            run_id,
            {
                "status": "failed",
                "currentStage": "failed",
                "selectionMode": "source_unavailable" if str(run.get("currentStage") or "") == "loading_sectors" else "timeout",
                "ruleFallbackUsed": False,
                "aiStatus": "skipped",
                "dataQualityStatus": "unavailable",
                "error": message,
                "finishedAt": datetime.now().isoformat(timespec="seconds"),
            },
        )

    def _run_worker(self, run_id: int) -> None:
        started_at = datetime.now().isoformat(timespec="seconds")
        self.database.update_morning_sector_radar_run(run_id, {"status": "running", "currentStage": "loading_market", "startedAt": started_at})
        try:
            run = self.database.load_morning_sector_radar_run(run_id)
            if not run:
                raise RuntimeError(f"任务不存在：{run_id}")
            request = run.get("request") or {}
            market_payload = self._load_market_context(str(request.get("marketSource") or "auto"))
            self.database.update_morning_sector_radar_run(run_id, {"market": market_payload.get("meta") or {}, "currentStage": "loading_sectors"})
            provider_payload, stale = self._load_sector_payload(
                source=str(request.get("source") or "auto"),
                sector_type=str(request.get("sectorType") or "all"),
            )
            provider = get_sector_provider("mock" if provider_payload.get("source") == "mock_sector" else "auto")
            self.database.update_morning_sector_radar_run(run_id, {"currentStage": "loading_constituents", "actualSource": provider_payload.get("source", "")})
            sector_limit = int(request.get("sectorLimit") or DEFAULT_SECTOR_LIMIT)
            leader_limit = int(request.get("leaderLimit") or DEFAULT_LEADER_LIMIT)
            scored = self._score_sectors(provider_payload.get("sectors") or [], market_payload.get("market") or {})
            is_mock = _is_mock_source(str(provider_payload.get("source") or ""))
            for sector in scored:
                quality = sector.get("dataQuality") or {}
                if is_mock:
                    quality.update({"status": "mock", "canPromote": False, "source": "mock"})
                elif stale:
                    quality.update({"status": "stale", "canPromote": False})
                else:
                    quality["canPromote"] = quality.get("status") == "ok"
                sector["dataQuality"] = quality
            preliminary = scored[: max(sector_limit, 20)]
            for sector in preliminary:
                try:
                    sector["leaders"] = _score_leaders(provider.fetch_constituents(sector.get("sectorCode") or sector["sectorName"], sector["sectorType"], limit=max(leader_limit, 8)))
                except Exception as error:  # noqa: BLE001
                    sector["leaders"] = []
                    sector.setdefault("warnings", []).append(f"成分股获取失败：{error}")
                _refresh_leader_quality(sector)
            rule_candidates = sorted(preliminary, key=lambda item: item["ruleScore"], reverse=True)[:sector_limit]
            for index, sector in enumerate(rule_candidates, start=1):
                sector["ruleRank"] = index
            self.database.update_morning_sector_radar_run(
                run_id,
                {
                    "currentStage": "ai_reviewing",
                    "sectorCount": len(provider_payload.get("sectors") or []),
                    "ruleCandidateCount": len(rule_candidates),
                    "qualifiedCount": len([item for item in rule_candidates if item.get("ruleStatus") not in {"insufficient", "fading"}]),
                    "overheatedCount": len([item for item in rule_candidates if item.get("ruleStatus") == "overheated"]),
                    "dataQualityStatus": "mock" if is_mock else "stale" if stale else "ok",
                },
            )
            ai_result = self._review_with_ai(rule_candidates, market_payload, request, stale)
            final_sectors = _apply_ai_review(rule_candidates, ai_result)
            selection_mode = ai_result.get("selectionMode") or "rules_fallback"
            rule_fallback = selection_mode != "ai_reviewed"
            self.database.update_morning_sector_radar_run(
                run_id,
                {
                    "currentStage": "saving",
                    "selectionMode": selection_mode,
                    "ruleFallbackUsed": rule_fallback,
                    "aiStatus": ai_result.get("aiStatus", ""),
                    "aiModel": ai_result.get("aiModel", ""),
                    "promptVersion": ai_result.get("promptVersion", PROMPT_VERSION),
                    "aiLatencyMs": int(ai_result.get("aiLatencyMs") or 0),
                    "aiResponse": ai_result.get("raw") or {},
                    "aiError": ai_result.get("error", ""),
                    "aiFinalCount": len([item for item in final_sectors if item.get("selected")]),
                },
            )
            run["selectionMode"] = selection_mode
            self.database.upsert_morning_sector_radar_sectors(run_id, final_sectors)
            self.database.upsert_morning_sector_radar_constituents(run_id, final_sectors)
            self.database.upsert_morning_sector_radar_outcomes(run_id, run, final_sectors)
            self._write_cache(provider_payload, final_sectors)
            self.database.update_morning_sector_radar_run(
                run_id,
                {"status": "done", "currentStage": "done", "finishedAt": datetime.now().isoformat(timespec="seconds")},
            )
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("morning sector radar run failed run_id=%s error=%s", run_id, error)
            self.database.update_morning_sector_radar_run(
                run_id,
                {
                    "status": "failed",
                    "currentStage": "failed",
                    "selectionMode": "source_unavailable",
                    "ruleFallbackUsed": False,
                    "aiStatus": "skipped",
                    "dataQualityStatus": "unavailable",
                    "sourceMeta": _source_failure_meta(error),
                    "error": str(error),
                    "finishedAt": datetime.now().isoformat(timespec="seconds"),
                },
            )

    def _load_market_context(self, market_source: str) -> Dict[str, Any]:
        try:
            context = self.market_context_store.latest_or_refresh(source=market_source, force=False)
            return {"meta": market_context_meta(context), "market": context.get("market") or {}}
        except Exception as error:  # noqa: BLE001
            return {"meta": {"warning": f"全市场上下文不可用：{error}", "source": market_source}, "market": {}}

    def _load_sector_payload(self, source: str, sector_type: str) -> tuple[Dict[str, Any], bool]:
        if source == "mock":
            return get_sector_provider("mock").fetch_sector_snapshot(sector_type), False
        try:
            payload = get_sector_provider("akshare").fetch_sector_snapshot(sector_type)
            payload["requestedSource"] = source
            return payload, False
        except Exception as error:  # noqa: BLE001
            cached = _read_cache()
            if cached:
                cached = dict(cached)
                cached["warnings"] = list(cached.get("warnings") or []) + [f"板块源刷新失败，使用旧缓存：{error}"]
                cached["stale"] = True
                return cached, True
            raise SectorProviderError(f"板块源刷新失败且没有可用真实缓存（{_cache_unavailable_reason()}）：{error}") from error

    def _score_sectors(self, rows: List[Dict[str, Any]], market: Dict[str, Any]) -> List[Dict[str, Any]]:
        turnovers = [float(item.get("turnoverAmount") or 0) for item in rows]
        changes = [float(item.get("changePct") or 0) for item in rows]
        fund_flows = [float(item.get("mainNetInflow") or 0) for item in rows if item.get("fundFlowStatus") == "ok"]
        result = []
        for row in rows:
            up = int(row.get("upCount") or 0)
            down = int(row.get("downCount") or 0)
            total = max(1, up + down + int(row.get("flatCount") or 0))
            breadth_pct = up / total * 100
            price_strength = _score_percentile(float(row.get("changePct") or 0), changes, 15)
            breadth = _clamp(breadth_pct / 100) * 15
            turnover_activity = _score_percentile(float(row.get("turnoverAmount") or 0), turnovers, 15)
            capital_flow = _capital_flow_score(row, fund_flows)
            limit_score = min(10, float(row.get("limitUpCount") or 0) * 2.5)
            leader_quality = 5.0
            persistence = _clamp((float(row.get("fiveDayMainNetInflow") or 0) / 1_000_000_000 + 1) / 2) * 10 if row.get("fiveDayMainNetInflow") else 4.5
            market_alignment = 5 if (market.get("mode") or "") == "aggressive" else 3 if (market.get("mode") or "") == "balanced" else 1
            risk_penalty = _risk_penalty(row, breadth_pct)
            score = price_strength + breadth + turnover_activity + capital_flow + leader_quality + limit_score + persistence + market_alignment - risk_penalty
            status = _rule_status(score, row, breadth_pct, capital_flow, risk_penalty)
            result.append(
                {
                    **row,
                    "ruleRank": 0,
                    "ruleStatus": status,
                    "finalStatus": status,
                    "activityScore": round(max(0, min(100, score)), 2),
                    "ruleScore": round(max(0, min(100, score)), 2),
                    "breadthPct": round(breadth_pct, 2),
                    "turnoverActivity": round(turnover_activity, 2),
                    "dimensions": {
                        "priceStrength": round(price_strength, 2),
                        "breadth": round(breadth, 2),
                        "turnoverActivity": round(turnover_activity, 2),
                        "capitalFlow": round(capital_flow, 2) if capital_flow is not None else None,
                        "leaderQuality": round(leader_quality, 2),
                        "limitUpStructure": round(limit_score, 2),
                        "persistence": round(persistence, 2),
                        "marketAlignment": round(market_alignment, 2),
                        "riskPenalty": round(risk_penalty, 2),
                    },
                    "reasons": _rule_reasons(row, breadth_pct, capital_flow),
                    "risks": _rule_risks(row, breadth_pct, capital_flow, risk_penalty),
                    "dataQuality": {"status": "ok" if row.get("fundFlowStatus") == "ok" else "fund_flow_missing", "missingFields": [] if row.get("fundFlowStatus") == "ok" else ["fundFlow"]},
                    "leaders": [],
                }
            )
        return sorted(result, key=lambda item: item["ruleScore"], reverse=True)

    def _review_with_ai(self, candidates: List[Dict[str, Any]], market_payload: Dict[str, Any], request: Dict[str, Any], stale: bool) -> Dict[str, Any]:
        if not request.get("useAI", True):
            return _fallback_ai_result("rules_fallback", "用户关闭 AI 审查")
        if not (self.settings.enable_ai and self.settings.ai_api_key and self.settings.ai_base_url):
            return _fallback_ai_result("rules_fallback", "DeepSeek 未配置或 AI 未启用")
        evidence = {
            "market": market_payload.get("market") or {},
            "marketMeta": market_payload.get("meta") or {},
            "scanTime": request.get("scanTime"),
            "sampleRole": _sample_role(str(request.get("scanTime") or DEFAULT_SCAN_TIME)),
            "stale": stale,
            "ruleCandidates": [_ai_candidate(item) for item in candidates],
        }
        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {"role": "system", "content": _ai_role_instructions()},
                {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
            ],
            "temperature": 0.15,
            "max_tokens": AI_REVIEW_MAX_TOKENS,
            "stream": False,
        }
        if self.settings.ai_provider in {"deepseek", "openai"}:
            payload["response_format"] = {"type": "json_object"}
        if self.settings.ai_provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        last_error = ""
        for attempt in range(1, 3):
            started = time.perf_counter()
            request_obj = urllib.request.Request(
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
            text = ""
            finish_reason = ""
            try:
                with urllib.request.urlopen(request_obj, timeout=45) as response:
                    data = json.loads(response.read().decode("utf-8"))
                finish_reason = _extract_finish_reason(data)
                text = _extract_chat_content(data)
                try:
                    parsed = _extract_json_object(text)
                except AIJsonParseError as error:
                    last_error = f"{self.settings.ai_provider} 审查失败：{error}"
                    LOGGER.warning(
                        "morning sector ai review invalid_json attempt=%s finish_reason=%s content_len=%s error=%s preview=%s",
                        attempt,
                        finish_reason,
                        len(text),
                        error,
                        _content_preview(text),
                    )
                    if attempt == 1 and text:
                        payload["messages"] = _json_repair_messages(text, str(error))
                        payload["temperature"] = 0
                        payload["max_tokens"] = AI_REPAIR_MAX_TOKENS
                    continue
                validated = _validate_ai_result(parsed, candidates, stale)
                validated.update(
                    {
                        "selectionMode": "ai_reviewed",
                        "aiStatus": "ok",
                        "aiModel": self.settings.ai_model,
                        "promptVersion": PROMPT_VERSION,
                        "aiLatencyMs": int((time.perf_counter() - started) * 1000),
                        "raw": parsed,
                    }
                )
                return validated
            except urllib.error.HTTPError as error:
                last_error = _http_error_message(error)
                break
            except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.IncompleteRead, json.JSONDecodeError, ValueError) as error:
                last_error = f"{self.settings.ai_provider} 审查失败：{error}"
                LOGGER.warning(
                    "morning sector ai review failed attempt=%s finish_reason=%s content_len=%s error=%s preview=%s",
                    attempt,
                    finish_reason,
                    len(text),
                    error,
                    _content_preview(text),
                )
        return _fallback_ai_result("rules_fallback", last_error or "DeepSeek 未返回有效审查结果")

    def _write_cache(self, provider_payload: Dict[str, Any], sectors: List[Dict[str, Any]]) -> None:
        payload = {
            "version": CACHE_VERSION,
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "source": provider_payload.get("source", ""),
            "sectorType": provider_payload.get("sectorType", "all"),
            "warnings": provider_payload.get("warnings") or [],
            "sectors": provider_payload.get("sectors") or [],
            "lastResult": [_compact_sector_cache(item) for item in sectors[:50]],
        }
        path = MOCK_CACHE_PATH if _is_mock_source(str(provider_payload.get("source") or "")) else CACHE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temp.replace(path)


def _read_cache() -> Dict[str, Any]:
    if not CACHE_PATH.exists():
        return {}
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    if int(payload.get("version") or 0) != CACHE_VERSION:
        return {}
    if _is_mock_source(str(payload.get("source") or "")):
        LOGGER.warning("ignored mock-polluted morning sector radar cache path=%s", CACHE_PATH)
        return {}
    return payload if isinstance(payload, dict) else {}


def _cache_unavailable_reason() -> str:
    if not CACHE_PATH.exists():
        return "真实缓存不存在"
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return "真实缓存无法读取"
    if not isinstance(payload, dict):
        return "真实缓存格式无效"
    if int(payload.get("version") or 0) != CACHE_VERSION:
        return "真实缓存版本不匹配"
    if _is_mock_source(str(payload.get("source") or "")):
        return "真实缓存文件包含 Mock 数据，已隔离"
    return "真实缓存为空或不可用"


def _source_failure_meta(error: Exception) -> Dict[str, Any]:
    return {
        "status": "unavailable",
        "source": "akshare_eastmoney_sector",
        "cacheStatus": _cache_unavailable_reason(),
        "error": str(error),
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
    }


def _score_leaders(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked = []
    for row in rows:
        change = float(row.get("changePct") or 0)
        turnover = float(row.get("turnoverAmount") or 0)
        flow = float(row.get("mainNetInflow") or 0)
        risk = []
        if change >= 9.8:
            risk.append("涨停或接近涨停，可买性受限")
        if change >= 8:
            risk.append("涨幅较高，追高风险")
        score = change * 4 + _clamp(turnover / 3_000_000_000) * 30 + _clamp((flow + 100_000_000) / 250_000_000) * 20
        ranked.append(
            {
                **row,
                "score": round(score, 2),
                "tradeability": "hard_to_buy" if change >= 9.8 else "chase_risk" if change >= 8 else "normal",
                "risk": "；".join(risk),
                "reason": f"涨跌幅 {change:.2f}%，成交额 {float(turnover or 0) / 100000000:.1f} 亿，主力净流入 {float(flow or 0) / 100000000:.1f} 亿。",
            }
        )
    ranked = sorted(ranked, key=lambda item: item.get("score") or 0, reverse=True)
    for index, item in enumerate(ranked, start=1):
        item["constituentRank"] = index
        if float(item.get("changePct") or 0) >= 8:
            item["candidateRole"] = "overheated"
        elif index == 1:
            item["candidateRole"] = "core_front"
        elif index <= 5:
            item["candidateRole"] = "front_runner"
        else:
            item["candidateRole"] = "back_runner"
    return ranked


def _refresh_leader_quality(sector: Dict[str, Any]) -> None:
    leaders = sector.get("leaders") or []
    if not leaders:
        return
    front = [item for item in leaders[:5] if item.get("candidateRole") in {"core_front", "front_runner", "confirming"}]
    consistency = len([item for item in leaders[:5] if float(item.get("mainNetInflow") or 0) > 0]) / max(1, min(5, len(leaders)))
    quality = min(15, len(front) * 2.2 + consistency * 5 + min(3, len([item for item in leaders[:5] if float(item.get("turnoverAmount") or 0) > 500_000_000])))
    sector["dimensions"]["leaderQuality"] = round(quality, 2)
    sector["dimensions"]["frontRunnerFundConsistency"] = round(consistency, 2)
    sector["activityScore"] = round(min(100, float(sector["activityScore"]) - 5 + quality), 2)
    sector["ruleScore"] = sector["activityScore"]


def _capital_flow_score(row: Dict[str, Any], all_flows: List[float]) -> float:
    if row.get("fundFlowStatus") != "ok":
        return 0.0
    main_flow = float(row.get("mainNetInflow") or 0)
    ratio = float(row.get("mainNetInflowRatio") or 0)
    super_ratio = float(row.get("superLargeNetInflowRatio") or 0)
    return (
        _score_percentile(main_flow, all_flows, 5)
        + _clamp((ratio + 2) / 10) * 4
        + _clamp((super_ratio + 1) / 6) * 2
        + (2 if main_flow > 0 and ratio > 0 else 0)
        + (2 if float(row.get("fiveDayMainNetInflow") or 0) > 0 else 0)
    )


def _risk_penalty(row: Dict[str, Any], breadth_pct: float) -> float:
    penalty = 0.0
    if float(row.get("changePct") or 0) >= 5.5:
        penalty += 5
    if breadth_pct < 45 and float(row.get("changePct") or 0) > 2:
        penalty += 8
    if row.get("fundFlowStatus") != "ok":
        penalty += 3
    if float(row.get("mainNetInflow") or 0) < 0 and float(row.get("changePct") or 0) > 1:
        penalty += 6
    return penalty


def _rule_status(score: float, row: Dict[str, Any], breadth_pct: float, capital_flow: float, risk_penalty: float) -> str:
    if row.get("fundFlowStatus") != "ok" and score < 55:
        return "insufficient"
    if risk_penalty >= 10:
        return "overheated"
    if score >= 76 and capital_flow >= 8:
        return "continuing"
    if score >= 66:
        return "emerging"
    if score >= 54:
        return "diverging" if breadth_pct < 55 else "emerging"
    return "fading"


def _rule_reasons(row: Dict[str, Any], breadth_pct: float, capital_flow: float) -> List[str]:
    reasons = [
        f"板块涨跌幅 {float(row.get('changePct') or 0):.2f}%。",
        f"上涨扩散 {breadth_pct:.1f}%。",
    ]
    if row.get("fundFlowStatus") == "ok":
        reasons.append(f"主力净流入 {float(row.get('mainNetInflow') or 0) / 100000000:.1f} 亿，资金方向分 {capital_flow:.1f}/15。")
    return reasons


def _rule_risks(row: Dict[str, Any], breadth_pct: float, capital_flow: float, risk_penalty: float) -> List[str]:
    risks = []
    if row.get("fundFlowStatus") != "ok":
        risks.append("板块资金流缺失，不能确认资金方向。")
    if float(row.get("mainNetInflow") or 0) < 0 and float(row.get("changePct") or 0) > 1:
        risks.append("价格上涨但主力资金净流出，存在背离。")
    if breadth_pct < 50:
        risks.append("上涨扩散不足，可能由少数股票带动。")
    if risk_penalty >= 8:
        risks.append("存在过热或退潮惩罚。")
    return risks


def _apply_ai_review(candidates: List[Dict[str, Any]], ai_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_code = {item["sectorCode"]: dict(item) for item in candidates}
    if ai_result.get("selectionMode") != "ai_reviewed":
        for index, item in enumerate(candidates[:5], start=1):
            item["aiRank"] = index
            item["finalRank"] = index
            item["selected"] = True
            item["aiAction"] = "rules_fallback"
            item["aiStatus"] = "rules_fallback"
            item["aiReason"] = ai_result.get("error", "规则兜底")
            item["aiConfidence"] = None
        return candidates
    selected_codes = set()
    for review in ai_result.get("sectors") or []:
        code = str(review.get("sectorCode") or "")
        item = by_code.get(code)
        if not item:
            continue
        action = str(review.get("action") or "maintain")
        item["aiAction"] = action
        item["aiStatus"] = "ok"
        item["aiRank"] = int(review.get("aiRank") or 0)
        item["finalRank"] = int(review.get("aiRank") or 0)
        item["finalStatus"] = str(review.get("finalStatus") or item.get("ruleStatus"))
        item["aiConfidence"] = _optional_float(review.get("confidence"))
        item["aiReason"] = str(review.get("reason") or "")
        item["counterEvidence"] = review.get("counterEvidence") or []
        item["invalidConditions"] = review.get("invalidConditions") or []
        item["aiRisks"] = review.get("risks") or []
        item["selected"] = action != "reject" and item["aiRank"] > 0
        if item["selected"]:
            selected_codes.add(code)
    for item in by_code.values():
        item.setdefault("aiAction", "")
        item.setdefault("aiStatus", "not_selected")
        item.setdefault("selected", False)
    return sorted(by_code.values(), key=lambda item: (0 if item.get("selected") else 1, int(item.get("finalRank") or 999), int(item.get("ruleRank") or 999)))


def _validate_ai_result(parsed: Dict[str, Any], candidates: List[Dict[str, Any]], stale: bool) -> Dict[str, Any]:
    allowed = {item["sectorCode"]: item for item in candidates}
    rows = parsed.get("sectors") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        raise ValueError("AI 返回缺少 sectors 数组")
    seen = set()
    valid = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("sectorCode") or "")
        if code not in allowed:
            raise ValueError(f"AI 返回候选池外板块：{code}")
        if code in seen:
            raise ValueError(f"AI 返回重复板块：{code}")
        seen.add(code)
        action = str(row.get("action") or "")
        if action not in {"maintain", "promote", "downgrade", "reject"}:
            raise ValueError(f"AI action 非法：{action}")
        if not str(row.get("reason") or "").strip():
            row = dict(row)
            rule_reason = " ".join(str(item) for item in (allowed[code].get("reasons") or []) if item)
            row["reason"] = f"AI 未给出明确理由，系统补充规则证据：{rule_reason or '规则证据不足，需人工复核。'}"
            invalid = row.get("invalidConditions") if isinstance(row.get("invalidConditions"), list) else []
            row["invalidConditions"] = invalid + ["AI 返回缺少 reason，系统已补充规则证据"]
        quality = allowed[code].get("dataQuality") or {}
        can_promote = not stale and quality.get("status") == "ok" and quality.get("canPromote") is not False
        if action == "promote" and not can_promote:
            row = dict(row)
            row["action"] = "maintain"
            row["reason"] = f"原 AI 试图提升，但该板块数据质量为 {quality.get('status') or 'insufficient'}，系统按约束改为维持；{row.get('reason')}"
            invalid = row.get("invalidConditions") if isinstance(row.get("invalidConditions"), list) else []
            row["invalidConditions"] = invalid + ["数据质量不足或 Mock/旧缓存样本禁止 promote"]
        valid.append(row)
    valid = sorted(valid, key=lambda item: int(item.get("aiRank") or 999))
    return {"sectors": valid, "top0": bool(parsed.get("top0")), "marketConclusion": str(parsed.get("marketConclusion") or "")}


def _ai_candidate(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "sectorCode": item.get("sectorCode"),
        "sectorName": item.get("sectorName"),
        "sectorType": item.get("sectorType"),
        "ruleRank": item.get("ruleRank"),
        "ruleStatus": item.get("ruleStatus"),
        "ruleScore": item.get("ruleScore"),
        "changePct": item.get("changePct"),
        "breadthPct": item.get("breadthPct"),
        "dimensions": item.get("dimensions"),
        "fundFlow": {
            "status": item.get("fundFlowStatus"),
            "mainNetInflow": item.get("mainNetInflow"),
            "mainNetInflowRatio": item.get("mainNetInflowRatio"),
            "superLargeNetInflowRatio": item.get("superLargeNetInflowRatio"),
            "frontRunnerConsistency": (item.get("dimensions") or {}).get("frontRunnerFundConsistency"),
        },
        "leaders": (item.get("leaders") or [])[:5],
        "reasons": item.get("reasons") or [],
        "risks": item.get("risks") or [],
        "dataQuality": item.get("dataQuality") or {},
    }


def _ai_role_instructions() -> str:
    return (
        "你是 A 股早盘主线板块审查员，只能基于输入 JSON 审查规则候选板块。"
        "你可以 maintain/promote/downgrade/reject，允许 Top0，但不能添加候选池外板块。"
        "资金流缺失、旧缓存、Mock 或数据不足时不能 promote。"
        "禁止输出买入指令、必涨、稳赚、满仓、无风险。"
        "每个 sectors 元素都必须包含 sectorCode, action, aiRank, finalStatus, confidence, reason, counterEvidence, risks, invalidConditions。"
        "reason 不能为空；如果没有足够证据入选，也要说明拒绝或降级原因。"
        "sectors 只返回最终观察板块和少量关键 reject/downgrade 样本，不要逐条复述全部候选；建议总数不超过 8 个。"
        "recommendedCount 必须等于 aiRank 大于 0 且 action 不是 reject 的数量；Top0 时 sectors 可以为空。"
        "只输出一个合法 JSON 对象：marketConclusion,recommendedCount,top0,sectors。"
    )


def _extract_json_object(text: str) -> Dict[str, Any]:
    candidates = _json_candidates(text)
    last_error: Exception | None = None
    for candidate in candidates:
        for parsed_candidate in _json_candidate_variants(candidate):
            try:
                value = json.loads(parsed_candidate)
            except json.JSONDecodeError as error:
                last_error = error
                continue
            if isinstance(value, dict):
                return value
    raise AIJsonParseError(f"AI 未返回可解析 JSON 对象：{last_error}") from last_error


def _extract_finish_reason(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return ""


def _json_repair_messages(malformed_json: str, error: str) -> List[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是严格 JSON 修复器。只修复语法，不改变字段含义，不新增候选，不输出解释。"
                "必须只返回一个合法 JSON 对象，字段仍为 marketConclusion,recommendedCount,top0,sectors。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "parseError": error,
                    "malformedJson": str(malformed_json or "")[:18000],
                },
                ensure_ascii=False,
            ),
        },
    ]


def _content_preview(text: str, limit: int = 600) -> str:
    preview = str(text or "").replace("\n", "\\n")
    return preview[:limit]


def _json_candidates(text: str) -> List[str]:
    stripped = str(text or "").strip()
    if not stripped:
        return []
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return []
    return [stripped[start : end + 1]]


def _json_candidate_variants(text: str) -> List[str]:
    variants = [text.strip()]
    repaired = _repair_json_text(text)
    if repaired not in variants:
        variants.append(repaired)
    return variants


def _repair_json_text(text: str) -> str:
    repaired = _repair_json_lines(str(text or "").strip())
    repaired = re.sub(r"\s*,\s*([}\]])", r"\1", repaired)
    repaired = re.sub(
        r'((?:true|false|null|"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[}\]]))(\s*)(?="[^"]+"\s*:)',
        r"\1,\2",
        repaired,
    )
    repaired = re.sub(r'([}\]])(\s*)(?=[{\[])', r"\1,\2", repaired)
    repaired = re.sub(
        r'((?:true|false|null|"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[}\]]))(\s+)(?=(?:"(?:[^"\\]|\\.)*"|-?\d|true|false|null|[{\[]))',
        r"\1,\2",
        repaired,
    )
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    return repaired


def _repair_json_lines(text: str) -> str:
    lines = str(text or "").splitlines()
    if len(lines) <= 1:
        return text
    result: List[str] = []
    for index, line in enumerate(lines):
        current = line.rstrip()
        result.append(current)
        next_line = _next_non_empty_line(lines, index + 1)
        if _line_needs_comma(current.strip(), next_line.strip() if next_line else ""):
            result[-1] = f"{current},"
    return "\n".join(result)


def _next_non_empty_line(lines: List[str], start: int) -> str:
    for line in lines[start:]:
        if line.strip():
            return line
    return ""


def _line_needs_comma(current: str, next_line: str) -> bool:
    if not current or not next_line:
        return False
    if current.endswith((",", "{", "[", ":", "(")):
        return False
    if next_line[0] in "}])":
        return False
    return _looks_like_json_value_end(current) and _looks_like_json_value_start(next_line)


def _looks_like_json_value_end(line: str) -> bool:
    return bool(re.search(r'(?:true|false|null|"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[}\]])$', line))


def _looks_like_json_value_start(line: str) -> bool:
    return bool(re.match(r'(?:"[^"]+"\s*:|"(?:[^"\\]|\\.)*"|-?\d|true|false|null|[{\[])', line))


def _fallback_ai_result(mode: str, error: str) -> Dict[str, Any]:
    return {"selectionMode": mode, "aiStatus": "failed", "error": error, "promptVersion": PROMPT_VERSION}


def _is_mock_source(source: str) -> bool:
    return "mock" in str(source or "").lower()


def _progress_pct(run: Dict[str, Any]) -> int:
    stage = str(run.get("currentStage") or "")
    order = ["pending", "loading_market", "loading_sectors", "loading_constituents", "scoring", "ai_reviewing", "saving", "done"]
    if run.get("status") == "failed":
        return 100
    if stage == "done":
        return 100
    try:
        return int(order.index(stage) / max(1, len(order) - 1) * 100)
    except ValueError:
        return 20 if run.get("status") == "running" else 0


def _elapsed_seconds(run: Dict[str, Any]) -> int:
    started = str(run.get("startedAt") or run.get("createdAt") or "")
    finished = str(run.get("finishedAt") or "")
    try:
        start_dt = datetime.fromisoformat(started)
        end_dt = datetime.fromisoformat(finished) if finished else datetime.now()
        return max(0, int((end_dt - start_dt).total_seconds()))
    except ValueError:
        return 0


def _run_timed_out(run: Dict[str, Any]) -> bool:
    if run.get("status") not in {"pending", "running"}:
        return False
    started = str(run.get("startedAt") or run.get("createdAt") or "")
    updated = str(run.get("updatedAt") or "")
    try:
        start_dt = datetime.fromisoformat(started)
    except ValueError:
        return False
    try:
        update_dt = datetime.fromisoformat(updated) if updated else start_dt
    except ValueError:
        update_dt = start_dt
    now = datetime.now()
    return (now - start_dt).total_seconds() > RUN_TIMEOUT_SECONDS or (now - update_dt).total_seconds() > RUN_TIMEOUT_SECONDS


def _sample_role(scan_time: str) -> str:
    now = datetime.now()
    try:
        hour, minute = [int(part) for part in scan_time.split(":", 1)]
    except ValueError:
        return "standard"
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    diff = (now - target).total_seconds() / 60
    if -2 <= diff <= 5:
        return "standard"
    return "early" if diff < -2 else "late"


def _normalize_sector_type(value: str) -> str:
    return value if value in {"industry", "concept", "all"} else "all"


def _normalize_scan_time(value: str) -> str:
    text = str(value or DEFAULT_SCAN_TIME).strip()
    return text if len(text) == 5 and text[2] == ":" else DEFAULT_SCAN_TIME


def _normalize_date_text(value: Any) -> str:
    text = str(value or "").strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    text = text.replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10] if len(text) >= 10 else ""


def _clamp_int(value: Any, lower: int, upper: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(upper, number))


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    if math.isnan(value) or math.isinf(value):
        return lower
    return max(lower, min(upper, value))


def _score_percentile(value: float, values: List[float], max_score: float) -> float:
    usable = sorted(item for item in values if isinstance(item, (int, float)))
    if not usable:
        return 0.0
    below = len([item for item in usable if item <= value])
    return below / len(usable) * max_score


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_float_value(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _sector_name_key(row: Dict[str, Any]) -> str:
    return f"{row.get('sectorType') or ''}::{row.get('sectorName') or ''}"


def _outcome_excluded_reason(row: Dict[str, Any]) -> str:
    if str(row.get("runStatus") or "") != "done":
        return "关联任务未完成，不进入收益回填。"
    if _is_mock_source(str(row.get("actualSource") or "")) or str(row.get("dataQualityStatus") or "") == "mock":
        return "Mock 测试样本只做诊断，不进入正式收益统计。"
    if str(row.get("dataQualityStatus") or "") == "stale":
        return "旧缓存样本只做诊断，不进入正式收益统计。"
    if str(row.get("dataQualityStatus") or "") not in {"", "ok"}:
        return f"数据质量为 {row.get('dataQualityStatus')}，不进入正式收益统计。"
    if str(row.get("sampleRole") or "") != "standard":
        return f"{row.get('sampleRole') or 'unknown'} 时点样本只做诊断，不进入正式收益统计。"
    return ""


def _outcome_no_data_reason(row: Dict[str, Any]) -> str:
    trade_date = _parse_iso_date(row.get("tradeDate"))
    if not trade_date:
        return "交易日期无效，无法回填。"
    if trade_date.weekday() >= 5:
        return "样本交易日为周末，不能作为标准 A 股交易日样本。"
    today = date.today()
    if today > trade_date:
        return "第一阶段尚未接入板块历史快照源，错过当日收盘回填窗口的样本暂标记为缺数据。"
    return ""


def _outcome_maturity(row: Dict[str, Any]) -> str:
    trade_date = _parse_iso_date(row.get("tradeDate"))
    if not trade_date:
        return ""
    today = date.today()
    now = datetime.now()
    if today < trade_date:
        return "尚未到样本交易日。"
    if today == trade_date and (now.hour, now.minute) < (15, 0):
        return "尚未到当日收盘，等待后续行情。"
    return ""


def _parse_iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(_normalize_date_text(value))
    except ValueError:
        return None


def _outcome_refresh_message(processed: int, results: Dict[str, int], pending: int, dry_run: bool) -> str:
    prefix = "回填预演完成" if dry_run else "回填完成"
    return (
        f"{prefix}：本批处理 {processed} 条样本行，"
        f"{results.get('done', 0)} 条完成，"
        f"{results.get('pending', 0)} 条等待，"
        f"{results.get('no_data', 0)} 条缺数据，"
        f"{results.get('excluded', 0)} 条排除；"
        f"当前剩余 pending {pending} 条。"
    )


def _compact_sector_cache(item: Dict[str, Any]) -> Dict[str, Any]:
    return {key: item.get(key) for key in ["sectorCode", "sectorName", "sectorType", "ruleRank", "aiRank", "finalRank", "selected", "activityScore"]}
