from __future__ import annotations

import argparse
import json
import logging
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlparse

from backend.app.database import Database
from backend.app.retail_holder_store import latest_retail_holders, refresh_retail_holder_cache
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.ai_full_market_selection_service import AIFullMarketSelectionService
from backend.app.services.cross_module_validation_service import CrossModuleValidationService
from backend.app.services.daily_kline_cache_service import DailyKlineCacheService
from backend.app.services.morning_sector_radar_service import MorningSectorRadarService
from backend.app.services.intraday_entry_audit_service import IntradayEntryAuditService
from backend.app.services.stock_analysis_feedback_service import StockAnalysisFeedbackService
from backend.app.services.stock_analysis_service import StockAnalysisService
from backend.app.services.t1_overnight_selection_service import T1OvernightSelectionService
from backend.app.services.watchlist_service import WatchlistService
from backend.app.settings import PROJECT_ROOT, load_settings
from backend.app.universe_store import universe_response


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("ai_stock.server")

SETTINGS = load_settings()
DATABASE = Database(SETTINGS.database_path)
ANALYSIS = AnalysisService(SETTINGS, DATABASE)
STOCK_FEEDBACK = StockAnalysisFeedbackService(SETTINGS, DATABASE)
STOCK_ANALYSIS = StockAnalysisService(SETTINGS, DATABASE)
STOCK_FEEDBACK_V2 = StockAnalysisFeedbackService(SETTINGS, DATABASE, version="v2")
STOCK_ANALYSIS_V2 = StockAnalysisService(SETTINGS, DATABASE, feedback_version="v2")
AI_FULL_MARKET_SELECTION = AIFullMarketSelectionService(SETTINGS, ANALYSIS, DATABASE)
T1_OVERNIGHT_SELECTION = T1OvernightSelectionService(SETTINGS, ANALYSIS, DATABASE)
T1_OVERNIGHT_SELECTION.start_watchdog()
DAILY_KLINE_CACHE = DailyKlineCacheService(AnalysisService(SETTINGS, DATABASE), DATABASE)
MORNING_SECTOR_RADAR = MorningSectorRadarService(SETTINGS, DATABASE)
CROSS_MODULE_VALIDATION = CrossModuleValidationService(SETTINGS, DATABASE)
INTRADAY_ENTRY_AUDIT = IntradayEntryAuditService(SETTINGS, DATABASE, STOCK_ANALYSIS_V2)
WATCHLIST = WatchlistService(DATABASE)


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json(
                {
                    "ok": True,
                    "database": str(SETTINGS.database_path),
                    "ai": SETTINGS.ai_mode(),
                    "aiProvider": SETTINGS.ai_provider,
                    "aiModel": SETTINGS.ai_model if SETTINGS.ai_api_key else "",
                }
            )
            return
        if parsed.path == "/api/daily-kline-cache/scheduler":
            self._json(DAILY_KLINE_CACHE.status())
            return
        if parsed.path == "/api/cross-module-validation/summary":
            query = parse_qs(parsed.query)
            limit = _parse_int(query.get("limit", ["600"])[0], 600)
            self._json(CROSS_MODULE_VALIDATION.summary(limit=limit))
            return
        if parsed.path == "/api/cross-module-validation/conflicts":
            query = parse_qs(parsed.query)
            limit = _parse_int(query.get("limit", ["600"])[0], 600)
            self._json(CROSS_MODULE_VALIDATION.conflicts(limit=limit))
            return
        if parsed.path == "/api/cross-module-validation/confluence":
            query = parse_qs(parsed.query)
            limit = _parse_int(query.get("limit", ["600"])[0], 600)
            self._json(CROSS_MODULE_VALIDATION.confluence(limit=limit))
            return
        if parsed.path == "/api/cross-module-validation/module-matrix":
            query = parse_qs(parsed.query)
            limit = _parse_int(query.get("limit", ["600"])[0], 600)
            self._json(CROSS_MODULE_VALIDATION.module_matrix(limit=limit))
            return
        if parsed.path == "/api/intraday-entry-audit/recent":
            query = parse_qs(parsed.query)
            code = query.get("code", [""])[0]
            limit = _parse_int(query.get("limit", ["20"])[0], 20)
            try:
                self._json(INTRADAY_ENTRY_AUDIT.recent(code=code, limit=limit))
            except Exception as error:  # noqa: BLE001 - keep API readable.
                LOGGER.warning("request intraday entry audit recent failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/intraday-entry-audit/history":
            query = parse_qs(parsed.query)
            code = query.get("code", [""])[0]
            entry_status = query.get("entryStatus", query.get("entry_status", [""]))[0]
            limit = _parse_int(query.get("limit", ["100"])[0], 100)
            try:
                self._json(INTRADAY_ENTRY_AUDIT.history(code=code, entry_status=entry_status, limit=limit))
            except Exception as error:  # noqa: BLE001 - keep API readable.
                LOGGER.warning("request intraday entry audit history failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path.startswith("/api/ai-full-market-selection/runs/"):
            run_id = _parse_int(parsed.path.rsplit("/", 1)[-1], 0)
            if run_id <= 0:
                self._json({"error": "invalid run id"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                self._json(AI_FULL_MARKET_SELECTION.get_run(run_id))
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/ai-full-market-selection/latest":
            query = parse_qs(parsed.query)
            profile = query.get("profile", query.get("type", ["daily"]))[0]
            risk_mode = query.get("riskMode", query.get("risk_mode", ["balanced"]))[0]
            source = query.get("source", ["auto"])[0]
            scan_time = query.get("scanTime", query.get("scan_time", ["14:36"]))[0]
            self._json(AI_FULL_MARKET_SELECTION.latest(profile=profile, risk_mode=risk_mode, source=source, scan_time=scan_time))
            return
        if parsed.path == "/api/ai-full-market-selection/feedback-config":
            self._json({"config": AI_FULL_MARKET_SELECTION.current_config()})
            return
        if parsed.path == "/api/ai-full-market-selection/feedback-stats":
            query = parse_qs(parsed.query)
            run_id = _parse_int(query.get("runId", query.get("run_id", ["0"]))[0], 0)
            self._json(AI_FULL_MARKET_SELECTION.feedback_stats(current_run_id=run_id if run_id > 0 else None))
            return
        if parsed.path == "/api/ai-full-market-selection/outcomes/recent":
            query = parse_qs(parsed.query)
            limit = _parse_int(query.get("limit", ["50"])[0], 50)
            run_id = _parse_int(query.get("runId", query.get("run_id", ["0"]))[0], 0)
            selected_only = str(query.get("selectedOnly", query.get("selected_only", ["0"]))[0]).lower() in {"1", "true", "yes"}
            self._json(AI_FULL_MARKET_SELECTION.recent_outcomes(limit=limit, run_id=run_id if run_id > 0 else None, selected_only=selected_only))
            return
        if parsed.path == "/api/ai-full-market-selection/outcomes/review":
            query = parse_qs(parsed.query)
            limit = _parse_int(query.get("limit", ["1000"])[0], 1000)
            self._json(AI_FULL_MARKET_SELECTION.outcomes_review(limit=limit))
            return
        if parsed.path.startswith("/api/morning-sector-radar/runs/"):
            run_id = _parse_int(parsed.path.rsplit("/", 1)[-1], 0)
            if run_id <= 0:
                self._json({"error": "invalid run id"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                self._json(MORNING_SECTOR_RADAR.get_run(run_id))
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/morning-sector-radar/latest":
            query = parse_qs(parsed.query)
            sector_type = query.get("sectorType", query.get("sector_type", ["all"]))[0]
            source = query.get("source", ["auto"])[0]
            scan_time = query.get("scanTime", query.get("scan_time", ["10:00"]))[0]
            self._json(MORNING_SECTOR_RADAR.latest(sector_type=sector_type, source=source, scan_time=scan_time))
            return
        if parsed.path == "/api/morning-sector-radar/diagnostics":
            query = parse_qs(parsed.query)
            window_runs = _parse_int(query.get("windowRuns", query.get("window_runs", ["20"]))[0], 20)
            self._json(MORNING_SECTOR_RADAR.diagnostics(window_runs=window_runs))
            return
        if parsed.path == "/api/t1-overnight-selection/critic-config":
            self._json({"config": T1_OVERNIGHT_SELECTION.critic_config(), "status": T1_OVERNIGHT_SELECTION.critic_status()})
            return
        if parsed.path == "/api/t1-overnight-selection/execution-config":
            self._json({"config": T1_OVERNIGHT_SELECTION.execution_config()})
            return
        if parsed.path == "/api/t1-overnight-selection/stage-config":
            self._json({"config": T1_OVERNIGHT_SELECTION.stage_config(), "status": T1_OVERNIGHT_SELECTION.stage_status()})
            return
        if parsed.path == "/api/t1-overnight-selection/auto-run":
            self._json(T1_OVERNIGHT_SELECTION.auto_run_status())
            return
        if parsed.path == "/api/t1-overnight-selection/prefetch":
            self._json(T1_OVERNIGHT_SELECTION.market_prefetch_status())
            return
        if parsed.path == "/api/t1-overnight-selection/watchdog":
            self._json(T1_OVERNIGHT_SELECTION.watchdog_status(recover=False))
            return
        if parsed.path == "/api/t1-overnight-selection/validation-auto-freeze":
            self._json(T1_OVERNIGHT_SELECTION.validation_auto_freeze_status())
            return
        if parsed.path == "/api/t1-overnight-selection/strategy-feedback":
            self._json({"feedback": T1_OVERNIGHT_SELECTION.latest_strategy_feedback(), "criticStatus": T1_OVERNIGHT_SELECTION.critic_status()})
            return
        if parsed.path == "/api/t1-overnight-selection/review":
            query = parse_qs(parsed.query)
            limit = _parse_int(query.get("limit", ["60"])[0], 60)
            run_id = _parse_int(query.get("runId", query.get("run_id", ["0"]))[0], 0)
            status = query.get("status", [""])[0]
            review_status = query.get("reviewStatus", query.get("review_status", [""]))[0]
            self._json(T1_OVERNIGHT_SELECTION.review(limit=limit, run_id=run_id, status=status, review_status=review_status))
            return
        if parsed.path.startswith("/api/t1-overnight-selection/runs/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) >= 5 and parts[4] in {"archive-summary", "candidates", "candidate", "next-day-summary"}:
                run_id = _parse_int(parts[3], 0)
                query = parse_qs(parsed.query)
                try:
                    if run_id <= 0 or not DATABASE.load_t1_overnight_selection_run(run_id):
                        raise ValueError("未找到 T+1 Run")
                    store = T1_OVERNIGHT_SELECTION.candidate_store
                    if parts[4] == "archive-summary":
                        data = store.summary(run_id)
                    elif parts[4] == "next-day-summary":
                        data = T1_OVERNIGHT_SELECTION.candidate_tracking.summary(run_id)
                    elif parts[4] == "candidate" and len(parts) == 6:
                        data = store.detail(run_id, parts[5])
                    elif parts[4] == "candidates":
                        data = store.page(run_id, page=_parse_int(query.get("page", ["1"])[0], 1),
                            page_size=_parse_int(query.get("pageSize", ["50"])[0], 50),
                            query=query.get("query", [""])[0], group=query.get("group", [""])[0],
                            stage=query.get("stage", [""])[0], sort=query.get("sort", ["preliminary_rank"])[0])
                    else:
                        raise ValueError("无效档案路径")
                    self._json(data)
                except ValueError as error:
                    self._json({"error": str(error)}, status=HTTPStatus.NOT_FOUND)
                except Exception as error:
                    LOGGER.exception("candidate archive query failed run=%s", run_id)
                    self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
                return
        if parsed.path.startswith("/api/t1-overnight-selection/runs/") and parsed.path.endswith("/outcomes"):
            parts = parsed.path.strip("/").split("/")
            run_id = _parse_int(parts[-2] if len(parts) >= 2 else "", 0)
            if run_id <= 0:
                self._json({"error": "invalid run id"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                self._json(T1_OVERNIGHT_SELECTION.get_outcomes(run_id))
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.NOT_FOUND)
            return
        if parsed.path.startswith("/api/t1-overnight-selection/runs/"):
            run_id = _parse_int(parsed.path.rsplit("/", 1)[-1], 0)
            if run_id <= 0:
                self._json({"error": "invalid run id"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                self._json(T1_OVERNIGHT_SELECTION.get_run(run_id))
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/t1-overnight-selection/latest":
            query = parse_qs(parsed.query)
            risk_mode = query.get("riskMode", query.get("risk_mode", ["balanced"]))[0]
            source = query.get("source", ["auto"])[0]
            self._json(T1_OVERNIGHT_SELECTION.latest(risk_mode=risk_mode, source=source))
            return
        if parsed.path == "/api/stock/search":
            query = parse_qs(parsed.query)
            keyword = query.get("query", query.get("q", [""]))[0]
            limit = _parse_int(query.get("limit", ["10"])[0], 10)
            started = time.perf_counter()
            data = STOCK_ANALYSIS.search(keyword, limit=limit)
            LOGGER.info(
                "request stock search done query=%s count=%s elapsed_ms=%.0f",
                keyword,
                data.get("count"),
                (time.perf_counter() - started) * 1000,
            )
            self._json(data)
            return
        if parsed.path == "/api/stock/case-memory/stats":
            query = parse_qs(parsed.query)
            feedback = _stock_feedback_for_version(query.get("version", query.get("analysisVersion", ["v1"]))[0])
            started = time.perf_counter()
            data = feedback.stats()
            LOGGER.info(
                "request stock case stats done version=%s samples=%s elapsed_ms=%.0f",
                data.get("version"),
                data.get("effectiveSampleCount"),
                (time.perf_counter() - started) * 1000,
            )
            self._json(data)
            return
        if parsed.path == "/api/stock/case-memory/config":
            query = parse_qs(parsed.query)
            feedback = _stock_feedback_for_version(query.get("version", query.get("analysisVersion", ["v1"]))[0])
            self._json({"version": feedback.version, "config": feedback.current_config()})
            return
        if parsed.path == "/api/stock/outcomes/recent":
            query = parse_qs(parsed.query)
            limit = _parse_int(query.get("limit", ["50"])[0], 50)
            feedback = _stock_feedback_for_version(query.get("version", query.get("analysisVersion", ["v1"]))[0])
            started = time.perf_counter()
            data = feedback.recent_outcomes(limit=limit)
            LOGGER.info(
                "request stock recent outcomes done version=%s snapshots=%s rows=%s elapsed_ms=%.0f",
                data.get("version"),
                data.get("snapshotCount"),
                data.get("rowCount"),
                (time.perf_counter() - started) * 1000,
            )
            self._json(data)
            return
        if parsed.path == "/api/stock/trade-outcomes/recent":
            query = parse_qs(parsed.query)
            limit = _parse_int(query.get("limit", ["50"])[0], 50)
            feedback = _stock_feedback_for_version(query.get("version", query.get("analysisVersion", ["v2"]))[0])
            started = time.perf_counter()
            try:
                data = feedback.recent_trade_outcomes(limit=limit)
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            LOGGER.info(
                "request stock recent trade outcomes done version=%s snapshots=%s rows=%s elapsed_ms=%.0f",
                data.get("version"),
                data.get("snapshotCount"),
                data.get("rowCount"),
                (time.perf_counter() - started) * 1000,
            )
            self._json(data)
            return
        if parsed.path == "/api/market-context/latest":
            query = parse_qs(parsed.query)
            source = query.get("source", ["auto"])[0]
            started = time.perf_counter()
            try:
                data = STOCK_ANALYSIS.market_context(source=source, force=False)
            except Exception as error:  # noqa: BLE001 - keep API failure readable.
                LOGGER.warning("request market context latest failed source=%s elapsed_ms=%.0f error=%s", source, (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request market context latest done source=%s actual_source=%s rows=%s cache_hit=%s elapsed_ms=%.0f",
                    source,
                    data.get("source"),
                    data.get("count"),
                    data.get("cacheHit"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/watchlist":
            self._json({"items": WATCHLIST.list()})
            return
        if parsed.path == "/api/universe/default":
            self._json(universe_response())
            return
        if parsed.path == "/api/retail-holders/latest":
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["100"])[0])
            except (TypeError, ValueError):
                limit = 100
            keyword = query.get("query", [""])[0]
            started = time.perf_counter()
            LOGGER.info("request retail holders latest start limit=%s query=%s", limit, keyword)
            try:
                data = latest_retail_holders(limit=limit, query=keyword)
            except Exception as error:  # noqa: BLE001 - API should return readable errors.
                LOGGER.warning("request retail holders latest failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request retail holders latest done count=%s matched=%s elapsed_ms=%.0f",
                    data.get("count"),
                    data.get("matchedRows"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/":
            self.path = "/index.html"
            parsed = urlparse(self.path)
        self._disable_html_cache(parsed.path)
        return super().do_GET()

    def end_headers(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path.endswith(".html"):
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def _disable_html_cache(self, path: str) -> None:
        if path != "/" and not path.endswith(".html"):
            return
        for header in ("If-Modified-Since", "If-None-Match"):
            if header in self.headers:
                del self.headers[header]

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/t1-overnight-selection/runs/") and parsed.path.endswith("/backfill-next-day"):
            parts = parsed.path.strip("/").split("/")
            try:
                self._read_json()
                self._json(T1_OVERNIGHT_SELECTION.candidate_tracking.retry(_parse_int(parts[3], 0)))
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:
                LOGGER.exception("candidate next-day repair failed")
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/universe/default/refresh":
            payload = self._read_json()
            source = payload.get("source", "auto")
            try:
                max_size = int(payload.get("maxSize", payload.get("max_size", 80)))
            except (TypeError, ValueError):
                max_size = 80
            started = time.perf_counter()
            LOGGER.info("request default universe refresh start source=%s max_size=%s", source, max_size)
            data = ANALYSIS.refresh_default_universe(source=source, max_size=max_size, force=True)
            LOGGER.info(
                "request default universe refresh done source=%s updated=%s count=%s elapsed_ms=%.0f",
                source,
                data.get("updated"),
                data.get("count"),
                (time.perf_counter() - started) * 1000,
            )
            self._json(data)
            return
        if parsed.path == "/api/retail-holders/refresh":
            payload = self._read_json()
            try:
                limit = int(payload.get("limit", 100))
            except (TypeError, ValueError):
                limit = 100
            keyword = str(payload.get("query", ""))
            symbol = str(payload.get("symbol", "最新") or "最新")
            started = time.perf_counter()
            LOGGER.info("request retail holders refresh start symbol=%s limit=%s query=%s", symbol, limit, keyword)
            try:
                refresh_retail_holder_cache(symbol=symbol)
                data = latest_retail_holders(limit=limit, query=keyword)
            except Exception as error:  # noqa: BLE001 - API should return readable errors.
                LOGGER.warning("request retail holders refresh failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request retail holders refresh done count=%s matched=%s elapsed_ms=%.0f",
                    data.get("count"),
                    data.get("matchedRows"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/stock/analyze":
            payload = self._read_json()
            started = time.perf_counter()
            query_text = str(payload.get("query") or payload.get("code") or "")
            stock_analysis = _stock_analysis_for_version(payload.get("version", payload.get("analysisVersion", "v1")))
            risk_mode = payload.get("riskMode", payload.get("risk_mode", "balanced"))
            scan_time = payload.get("scanTime", payload.get("scan_time", "10:42"))
            use_ai = bool(payload.get("useAI", True))
            market_source = str(payload.get("marketSource", payload.get("market_source", "auto")) or "auto")
            force_market_context = bool(payload.get("forceMarketContext", payload.get("force_market_context", False)))
            try:
                position_context = _parse_stock_analysis_position(payload)
                LOGGER.info(
                    "request stock analyze start version=%s query=%s risk_mode=%s scan_time=%s use_ai=%s market_source=%s force_market_context=%s position_state=%s",
                    stock_analysis.feedback_version,
                    query_text,
                    risk_mode,
                    scan_time,
                    use_ai,
                    market_source,
                    force_market_context,
                    position_context.get("positionState"),
                )
                data = stock_analysis.analyze(
                    code=query_text,
                    risk_mode=risk_mode,
                    scan_time=scan_time,
                    use_ai=use_ai,
                    market_source=market_source,
                    force_market_context=force_market_context,
                    position_context=position_context,
                )
            except ValueError as error:
                LOGGER.warning("request stock analyze bad_request query=%s elapsed_ms=%.0f error=%s", query_text, (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep API failure readable.
                LOGGER.warning("request stock analyze failed query=%s elapsed_ms=%.0f error=%s", query_text, (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request stock analyze done code=%s ai_mode=%s decision=%s elapsed_ms=%.0f",
                    data.get("code"),
                    data.get("aiMode"),
                    data.get("decision", {}).get("decision"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/ai-full-market-selection/run":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = AI_FULL_MARKET_SELECTION.start_run(
                    profile=str(payload.get("profile", payload.get("type", "daily"))),
                    risk_mode=str(payload.get("riskMode", payload.get("risk_mode", "balanced"))),
                    source=str(payload.get("source", "auto")),
                    scan_time=str(payload.get("scanTime", payload.get("scan_time", "14:36"))),
                    force=bool(payload.get("force", False)),
                    pool_size=_parse_int(payload.get("poolSize", payload.get("pool_size", 500)), 500),
                    batch_size=_parse_int(payload.get("batchSize", payload.get("batch_size", 80)), 80),
                    batch_pick=_parse_int(payload.get("batchPick", payload.get("batch_pick", 10)), 10),
                    final_limit=_parse_int(payload.get("finalLimit", payload.get("final_limit", 10)), 10),
                )
            except ValueError as error:
                LOGGER.warning("request ai full market bad_request elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep API failure readable.
                LOGGER.warning("request ai full market failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request ai full market started run=%s profile=%s risk_mode=%s elapsed_ms=%.0f",
                    data.get("id"),
                    data.get("profile"),
                    data.get("riskMode"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/ai-full-market-selection/outcomes/refresh":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                horizons = payload.get("horizons", [1, 3, 5, 7])
                if not isinstance(horizons, list):
                    horizons = [1, 3, 5, 7]
                data = AI_FULL_MARKET_SELECTION.refresh_outcomes(
                    run_id=_parse_int(payload.get("runId", payload.get("run_id", 0)), 0) or None,
                    profile=str(payload.get("profile", "")),
                    risk_mode=str(payload.get("riskMode", payload.get("risk_mode", ""))),
                    date_from=str(payload.get("dateFrom", payload.get("date_from", ""))),
                    date_to=str(payload.get("dateTo", payload.get("date_to", ""))),
                    horizons=horizons,
                    limit=_parse_int(payload.get("limit", 120), 120),
                    dry_run=bool(payload.get("dryRun", payload.get("dry_run", False))),
                    force_history=bool(payload.get("forceHistory", payload.get("force_history", False))),
                )
            except ValueError as error:
                LOGGER.warning("request ai full market outcomes refresh bad_request elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep API failure readable.
                LOGGER.warning("request ai full market outcomes refresh failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                self._json(data)
            return
        if parsed.path == "/api/ai-full-market-selection/feedback-config":
            payload = self._read_json()
            try:
                self._json(AI_FULL_MARKET_SELECTION.update_config(payload))
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep manual config API readable.
                LOGGER.warning("request ai full market feedback config failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/ai-full-market-selection/optimization/run":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = AI_FULL_MARKET_SELECTION.optimize_from_history(apply=bool(payload.get("apply", True)))
            except Exception as error:  # noqa: BLE001 - keep API failure readable.
                LOGGER.warning("request ai full market optimization failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                self._json(data)
            return
        if parsed.path == "/api/morning-sector-radar/run":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = MORNING_SECTOR_RADAR.start_run(
                    sector_type=str(payload.get("sectorType", payload.get("sector_type", "all"))),
                    source=str(payload.get("source", "auto")),
                    market_source=str(payload.get("marketSource", payload.get("market_source", "auto"))),
                    scan_time=str(payload.get("scanTime", payload.get("scan_time", "10:00"))),
                    use_ai=bool(payload.get("useAI", payload.get("use_ai", True))),
                    force=bool(payload.get("force", False)),
                    sector_limit=_parse_int(payload.get("sectorLimit", payload.get("sector_limit", 20)), 20),
                    leader_limit=_parse_int(payload.get("leaderLimit", payload.get("leader_limit", 5)), 5),
                )
            except ValueError as error:
                LOGGER.warning("request morning sector radar bad_request elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep API failure readable.
                LOGGER.warning("request morning sector radar failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request morning sector radar started run=%s sector_type=%s source=%s elapsed_ms=%.0f",
                    data.get("id"),
                    data.get("sectorType"),
                    data.get("requestedSource"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/morning-sector-radar/outcomes/refresh":
            payload = self._read_json()
            try:
                self._json(
                    MORNING_SECTOR_RADAR.refresh_outcomes(
                        run_id=_parse_int(payload.get("runId", payload.get("run_id", 0)), 0) or None,
                        date_from=str(payload.get("dateFrom", payload.get("date_from", ""))),
                        date_to=str(payload.get("dateTo", payload.get("date_to", ""))),
                        batch_size=_parse_int(payload.get("batchSize", payload.get("batch_size", 200)), 200),
                        dry_run=bool(payload.get("dryRun", payload.get("dry_run", False))),
                    )
                )
            except Exception as error:  # noqa: BLE001 - keep manual endpoint readable.
                LOGGER.warning("request morning sector outcomes refresh failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/t1-overnight-selection/run":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = T1_OVERNIGHT_SELECTION.start_run(
                    risk_mode=str(payload.get("riskMode", payload.get("risk_mode", "balanced"))),
                    source=str(payload.get("source", "auto")),
                    scan_time=str(payload.get("scanTime", payload.get("scan_time", "14:40"))),
                    force=bool(payload.get("force", False)),
                    pool_size=_parse_int(payload.get("poolSize", payload.get("pool_size", 500)), 500),
                    batch_size=_parse_int(payload.get("batchSize", payload.get("batch_size", 80)), 80),
                    batch_pick=_parse_int(payload.get("batchPick", payload.get("batch_pick", 8)), 8),
                    final_limit=_parse_int(payload.get("finalLimit", payload.get("final_limit", 3)), 3),
                    trigger=str(payload.get("trigger") or "manual"),
                    auto_schedule=bool(payload.get("autoSchedule", payload.get("auto_schedule", False))),
                )
            except ValueError as error:
                LOGGER.warning("request t1 overnight bad_request elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep API failure readable.
                LOGGER.warning("request t1 overnight failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request t1 overnight started run=%s risk_mode=%s elapsed_ms=%.0f",
                    data.get("id"),
                    data.get("riskMode"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/t1-overnight-selection/evidence/preheat":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = T1_OVERNIGHT_SELECTION.preheat_evidence(
                    source=str(payload.get("source", "auto")),
                    scan_time=str(payload.get("scanTime", payload.get("scan_time", "14:40"))),
                    pool_size=_parse_int(payload.get("poolSize", payload.get("pool_size", 500)), 500),
                )
            except ValueError as error:
                LOGGER.warning("request t1 overnight evidence preheat bad_request elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep manual preheat readable.
                LOGGER.warning("request t1 overnight evidence preheat failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request t1 overnight evidence preheat done evidence=%s pool=%s elapsed_ms=%.0f",
                    (data.get("evidence") or {}).get("evidenceRunId"),
                    data.get("poolCount"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/t1-overnight-selection/critic-config":
            payload = self._read_json()
            try:
                self._json(T1_OVERNIGHT_SELECTION.update_critic_config(payload))
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep manual config API readable.
                LOGGER.warning("request t1 overnight critic config failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/t1-overnight-selection/execution-config":
            payload = self._read_json()
            try:
                self._json(T1_OVERNIGHT_SELECTION.update_execution_config(payload))
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep manual config API readable.
                LOGGER.warning("request t1 overnight execution config failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/t1-overnight-selection/stage-config":
            payload = self._read_json()
            try:
                self._json(T1_OVERNIGHT_SELECTION.update_stage_config(payload))
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep manual stage config API readable.
                LOGGER.warning("request t1 overnight stage config failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path.startswith("/api/t1-overnight-selection/runs/") and parsed.path.endswith("/verify"):
            parts = parsed.path.strip("/").split("/")
            run_id = _parse_int(parts[-2] if len(parts) >= 2 else "", 0)
            if run_id <= 0:
                self._json({"error": "invalid run id"}, status=HTTPStatus.BAD_REQUEST)
                return
            started = time.perf_counter()
            try:
                data = T1_OVERNIGHT_SELECTION.verify_run(run_id, trigger_source="manual_verify")
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep validation failure readable.
                LOGGER.warning("request t1 overnight verify failed run=%s elapsed_ms=%.0f error=%s", run_id, (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info("request t1 overnight verify done run=%s status=%s elapsed_ms=%.0f", run_id, data.get("status"), (time.perf_counter() - started) * 1000)
                self._json(data)
            return
        if parsed.path.startswith("/api/t1-overnight-selection/runs/") and parsed.path.endswith("/backfill-validation-minutes"):
            parts = parsed.path.strip("/").split("/")
            run_id = _parse_int(parts[-2] if len(parts) >= 2 else "", 0)
            if run_id <= 0:
                self._json({"error": "invalid run id"}, status=HTTPStatus.BAD_REQUEST)
                return
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = T1_OVERNIGHT_SELECTION.backfill_validation_minutes(
                    run_id,
                    scope=str(payload.get("scope") or "final"),
                    force=bool(payload.get("force")),
                    period=str(payload.get("period") or "1m"),
                )
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep validation failure readable.
                LOGGER.warning("request t1 overnight validation minute backfill failed run=%s elapsed_ms=%.0f error=%s", run_id, (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info("request t1 overnight validation minute backfill done run=%s status=%s stored=%s elapsed_ms=%.0f", run_id, data.get("status"), data.get("stored"), (time.perf_counter() - started) * 1000)
                self._json(data)
            return
        if parsed.path == "/api/t1-overnight-selection/verify-pending":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = T1_OVERNIGHT_SELECTION.verify_pending_runs(limit=_parse_int(payload.get("limit", 20), 20))
            except Exception as error:  # noqa: BLE001 - keep manual batch API readable.
                LOGGER.warning("request t1 overnight verify pending failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info("request t1 overnight verify pending done count=%s elapsed_ms=%.0f", data.get("count"), (time.perf_counter() - started) * 1000)
                self._json(data)
            return
        if parsed.path == "/api/t1-overnight-selection/validation-auto-freeze/run":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = T1_OVERNIGHT_SELECTION.run_validation_auto_freeze_due(
                    trigger_source=str(payload.get("triggerSource") or "manual_api"),
                    slot=str(payload.get("slot") or ""),
                    force=bool(payload.get("force")),
                )
            except Exception as error:  # noqa: BLE001 - keep manual scheduler test readable.
                LOGGER.warning("request t1 validation auto-freeze run failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info("request t1 validation auto-freeze run done status=%s elapsed_ms=%.0f", data.get("lastStatus"), (time.perf_counter() - started) * 1000)
                self._json(data)
            return
        if parsed.path == "/api/t1-overnight-selection/auto-run/run":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = T1_OVERNIGHT_SELECTION.run_auto_run_due(
                    trigger_source=str(payload.get("triggerSource") or "manual_api"),
                    slot=str(payload.get("slot") or ""),
                    force=bool(payload.get("force")),
                )
            except Exception as error:  # noqa: BLE001 - keep manual scheduler test readable.
                LOGGER.warning("request t1 auto-run failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info("request t1 auto-run done status=%s elapsed_ms=%.0f", data.get("lastStatus"), (time.perf_counter() - started) * 1000)
                self._json(data)
            return
        if parsed.path == "/api/t1-overnight-selection/prefetch/run":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = T1_OVERNIGHT_SELECTION.run_market_prefetch_due(
                    trigger_source=str(payload.get("triggerSource") or "manual_api"),
                    slot=str(payload.get("slot") or ""),
                    force=bool(payload.get("force", True)),
                )
            except Exception as error:  # noqa: BLE001 - keep manual prefetch API readable.
                LOGGER.warning("request t1 market prefetch failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                self._json(data)
            return
        if parsed.path == "/api/t1-overnight-selection/strategy-feedback/refresh":
            payload = self._read_json()
            try:
                source_run_id = _parse_int(payload.get("runId", payload.get("sourceRunId", 0)), 0)
                self._json({"feedback": T1_OVERNIGHT_SELECTION.refresh_strategy_feedback(source_run_id=source_run_id, trigger_source="manual_refresh")})
            except Exception as error:  # noqa: BLE001 - keep manual feedback API readable.
                LOGGER.warning("request t1 overnight strategy feedback refresh failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/t1-overnight-selection/history-dry-run":
            try:
                self._json(T1_OVERNIGHT_SELECTION.history_dry_run())
            except Exception as error:  # noqa: BLE001 - keep dry-run readable.
                LOGGER.warning("request t1 overnight history dry-run failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/t1-overnight-selection/history-soft-cleanup":
            try:
                self._json(T1_OVERNIGHT_SELECTION.history_soft_cleanup())
            except Exception as error:  # noqa: BLE001 - keep soft cleanup readable.
                LOGGER.warning("request t1 overnight history soft cleanup failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/intraday-entry-audit/run":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = INTRADAY_ENTRY_AUDIT.run(payload)
            except ValueError as error:
                LOGGER.warning("request intraday entry audit bad_request elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep API failure readable.
                LOGGER.warning("request intraday entry audit failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request intraday entry audit done code=%s status=%s ai_status=%s elapsed_ms=%.0f",
                    data.get("code"),
                    data.get("entryStatus"),
                    data.get("aiStatus"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/intraday-entry-audit/batch-run":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = INTRADAY_ENTRY_AUDIT.batch_run(payload)
            except ValueError as error:
                LOGGER.warning("request intraday entry audit batch bad_request elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep API failure readable.
                LOGGER.warning("request intraday entry audit batch failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                self._json(data)
            return
        if parsed.path == "/api/intraday-entry-audit/feedback":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                data = INTRADAY_ENTRY_AUDIT.save_feedback(payload)
            except ValueError as error:
                LOGGER.warning("request intraday entry audit feedback bad_request elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep API failure readable.
                LOGGER.warning("request intraday entry audit feedback failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                self._json(data)
            return
        if parsed.path == "/api/intraday-entry-audit/outcomes/refresh":
            payload = self._read_json()
            started = time.perf_counter()
            try:
                horizons = payload.get("horizons", ["t1_next_day", "1d", "3d", "5d"])
                if isinstance(horizons, str):
                    horizons = [item.strip() for item in horizons.split(",") if item.strip()]
                if not isinstance(horizons, list):
                    horizons = ["t1_next_day", "1d", "3d", "5d"]
                data = INTRADAY_ENTRY_AUDIT.refresh_outcomes(
                    snapshot_id=_parse_int(payload.get("snapshotId", payload.get("snapshot_id", 0)), 0),
                    limit=_parse_int(payload.get("limit", 100), 100),
                    horizons=horizons,
                    dry_run=bool(payload.get("dryRun", payload.get("dry_run", False))),
                )
            except Exception as error:  # noqa: BLE001 - keep API readable.
                LOGGER.warning("request intraday entry audit outcomes refresh failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                self._json(data)
            return
        if parsed.path == "/api/t1-overnight-selection/strategy-suggestions/review":
            payload = self._read_json()
            try:
                suggestion_id = _parse_int(payload.get("id", payload.get("suggestionId", 0)), 0)
                if suggestion_id <= 0:
                    self._json({"error": "invalid suggestion id"}, status=HTTPStatus.BAD_REQUEST)
                    return
                data = T1_OVERNIGHT_SELECTION.review_strategy_suggestion(
                    suggestion_id,
                    str(payload.get("status") or ""),
                    str(payload.get("reviewerNote") or ""),
                )
                self._json(data)
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep manual suggestion review readable.
                LOGGER.warning("request t1 overnight strategy suggestion review failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/t1-overnight-selection/run-role":
            payload = self._read_json()
            try:
                run_id = _parse_int(payload.get("id", payload.get("runId", 0)), 0)
                if run_id <= 0:
                    self._json({"error": "invalid run id"}, status=HTTPStatus.BAD_REQUEST)
                    return
                data = T1_OVERNIGHT_SELECTION.update_run_role(
                    run_id,
                    str(payload.get("role", payload.get("runRole", ""))),
                    str(payload.get("reviewerNote") or ""),
                )
                self._json(data)
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep manual role update readable.
                LOGGER.warning("request t1 overnight run role update failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/stock/outcomes/refresh":
            payload = self._read_json()
            started = time.perf_counter()
            horizons = _parse_horizons(str(payload.get("horizons", "3,5,7,10")))
            feedback = _stock_feedback_for_version(payload.get("version", payload.get("analysisVersion", "v1")))
            try:
                batch_size = _parse_int(payload.get("batchSize", payload.get("batch_size", payload.get("limit", 500))), 500)
                data = feedback.update_outcomes(
                    code=str(payload.get("code", "")),
                    date_from=str(payload.get("from", payload.get("dateFrom", ""))),
                    date_to=str(payload.get("to", payload.get("dateTo", ""))),
                    horizons=horizons,
                    limit=batch_size,
                    dry_run=bool(payload.get("dryRun", payload.get("dry_run", False))),
                    force_history=bool(payload.get("forceHistory", payload.get("force_history", False))),
                    mode=str(payload.get("mode", "backfill")),
                )
            except Exception as error:  # noqa: BLE001 - keep manual endpoint readable.
                LOGGER.warning("request stock outcomes refresh failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request stock outcomes refresh done version=%s mode=%s rows=%s done=%s remaining=%s elapsed_ms=%.0f",
                    data.get("version"),
                    data.get("mode"),
                    data.get("rowCount"),
                    data.get("doneCount"),
                    data.get("remainingSnapshotCount"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/stock/trade-outcomes/refresh":
            payload = self._read_json()
            started = time.perf_counter()
            feedback = _stock_feedback_for_version(payload.get("version", payload.get("analysisVersion", "v2")))
            try:
                batch_size = _parse_int(payload.get("batchSize", payload.get("batch_size", payload.get("limit", 500))), 500)
                data = feedback.update_trade_outcomes(
                    code=str(payload.get("code", "")),
                    date_from=str(payload.get("from", payload.get("dateFrom", ""))),
                    date_to=str(payload.get("to", payload.get("dateTo", ""))),
                    limit=batch_size,
                    dry_run=bool(payload.get("dryRun", payload.get("dry_run", False))),
                    force_history=bool(payload.get("forceHistory", payload.get("force_history", False))),
                    mode=str(payload.get("mode", "backfill")),
                )
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep manual endpoint readable.
                LOGGER.warning("request stock trade outcomes refresh failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request stock trade outcomes refresh done version=%s mode=%s rows=%s done=%s remaining=%s elapsed_ms=%.0f",
                    data.get("version"),
                    data.get("mode"),
                    data.get("rowCount"),
                    data.get("doneCount"),
                    data.get("remainingSnapshotCount"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/stock/case-memory/config":
            payload = self._read_json()
            feedback = _stock_feedback_for_version(payload.get("version", payload.get("analysisVersion", "v1")))
            try:
                data = feedback.update_config(payload)
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001 - keep API readable.
                LOGGER.warning("request stock case config update failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                self._json(data)
            return
        if parsed.path == "/api/stock/review/generate":
            payload = self._read_json()
            started = time.perf_counter()
            feedback = _stock_feedback_for_version(payload.get("version", payload.get("analysisVersion", "v1")))
            try:
                data = feedback.review_failures(
                    date_from=str(payload.get("from", payload.get("dateFrom", ""))),
                    date_to=str(payload.get("to", payload.get("dateTo", ""))),
                    limit=_parse_int(str(payload.get("limit", 80)), 80),
                    save=not bool(payload.get("dryRun", payload.get("dry_run", False))),
                )
            except Exception as error:  # noqa: BLE001 - keep manual endpoint readable.
                LOGGER.warning("request stock review generate failed elapsed_ms=%.0f error=%s", (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request stock review generate done version=%s cases=%s report_id=%s elapsed_ms=%.0f",
                    data.get("version"),
                    data.get("caseCount"),
                    data.get("reportId"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/stock/review/lesson-status":
            payload = self._read_json()
            feedback = _stock_feedback_for_version(payload.get("version", payload.get("analysisVersion", "v1")))
            try:
                lesson = feedback.update_lesson_status(
                    lesson_id=int(payload.get("id", payload.get("lessonId", 0))),
                    status=str(payload.get("status", "")),
                )
            except (TypeError, ValueError) as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._json({"updated": True, "lesson": lesson})
            return
        if parsed.path == "/api/cross-module-validation/refresh":
            payload = self._read_json()
            try:
                limit = _parse_int(payload.get("limit", payload.get("sampleLimit", 600)), 600)
                self._json(CROSS_MODULE_VALIDATION.refresh(limit=limit))
            except Exception as error:  # noqa: BLE001 - keep API readable.
                LOGGER.warning("request cross module validation refresh failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/cross-module-validation/review":
            payload = self._read_json()
            try:
                limit = _parse_int(payload.get("limit", payload.get("sampleLimit", 600)), 600)
                self._json(CROSS_MODULE_VALIDATION.review(limit=limit))
            except Exception as error:  # noqa: BLE001 - keep API readable.
                LOGGER.warning("request cross module validation review failed error=%s", error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/market-context/refresh":
            payload = self._read_json()
            source = str(payload.get("source", "auto") or "auto")
            started = time.perf_counter()
            LOGGER.info("request market context refresh start source=%s", source)
            try:
                data = STOCK_ANALYSIS.market_context(source=source, force=True)
            except Exception as error:  # noqa: BLE001 - API should return readable errors.
                LOGGER.warning("request market context refresh failed source=%s elapsed_ms=%.0f error=%s", source, (time.perf_counter() - started) * 1000, error)
                self._json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            else:
                LOGGER.info(
                    "request market context refresh done source=%s actual_source=%s rows=%s elapsed_ms=%.0f",
                    source,
                    data.get("source"),
                    data.get("count"),
                    (time.perf_counter() - started) * 1000,
                )
                self._json(data)
            return
        if parsed.path == "/api/watchlist":
            payload = self._read_json()
            try:
                item = WATCHLIST.add(payload.get("code", ""), payload.get("name", ""), payload.get("note", ""))
            except ValueError as error:
                self._json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._json(item, status=HTTPStatus.CREATED)
            return
        self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        prefix = "/api/watchlist/"
        if parsed.path.startswith(prefix):
            WATCHLIST.remove(unquote(parsed.path[len(prefix) :]))
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def _json(self, data: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("http %s", format % args)


def _parse_horizons(value: Any) -> list[int]:
    result = []
    for item in str(value or "").split(","):
        try:
            horizon = int(item.strip())
        except ValueError:
            continue
        if horizon > 0:
            result.append(horizon)
    return result or [3, 5, 7, 10]


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stock_feedback_for_version(value: Any) -> StockAnalysisFeedbackService:
    return STOCK_FEEDBACK_V2 if _is_stock_v2(value) else STOCK_FEEDBACK


def _parse_stock_analysis_position(payload: Dict[str, Any]) -> Dict[str, Any]:
    state = str(payload.get("positionState", payload.get("position_state", "none")) or "none").strip().lower()
    if state not in {"none", "existing"}:
        state = "none"
    if state != "existing":
        return {
            "positionState": "none",
            "positionInfo": {},
            "canSellToday": False,
            "scope": "single_analysis_request",
            "source": "user_input",
        }

    raw_info = payload.get("positionInfo", payload.get("position_info", {}))
    info = raw_info if isinstance(raw_info, dict) else {}
    cost_price = _parse_optional_float(info.get("costPrice", info.get("cost_price")))
    position_pct = _parse_optional_float(info.get("positionPct", info.get("position_pct")))
    holding_days = _parse_optional_float(info.get("holdingDays", info.get("holding_days")))
    planned_horizon = str(info.get("plannedHorizon", info.get("planned_horizon", "")) or "").strip().lower()
    errors = []
    if cost_price is None or cost_price <= 0:
        errors.append("成本价必须大于 0")
    if position_pct is None or position_pct <= 0 or position_pct > 100:
        errors.append("仓位比例必须在 0-100% 之间")
    if holding_days is None or holding_days < 0 or int(holding_days) != holding_days:
        errors.append("持有天数必须是大于等于 0 的整数")
    if planned_horizon and planned_horizon not in {"short", "mid", "long"}:
        errors.append("计划周期只能是 short/mid/long")
    if errors:
        raise ValueError("已有持仓信息无效：" + "；".join(errors))

    position_info: Dict[str, Any] = {
        "costPrice": round(float(cost_price), 4),
        "positionPct": round(float(position_pct), 2),
        "holdingDays": int(holding_days),
    }
    if planned_horizon:
        position_info["plannedHorizon"] = planned_horizon
    return {
        "positionState": "existing",
        "positionInfo": position_info,
        "canSellToday": True,
        "scope": "single_analysis_request",
        "source": "user_input",
    }


def _parse_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace("％", "")
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _stock_analysis_for_version(value: Any) -> StockAnalysisService:
    return STOCK_ANALYSIS_V2 if _is_stock_v2(value) else STOCK_ANALYSIS


def _is_stock_v2(value: Any) -> bool:
    return str(value or "").strip().lower() in {"2", "v2", "stock_analysis_v2", "direction_v2"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local A-share radar server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()

    T1_OVERNIGHT_SELECTION.start_auto_run_scheduler()
    T1_OVERNIGHT_SELECTION.start_market_prefetch_scheduler()
    T1_OVERNIGHT_SELECTION.start_validation_auto_freeze_scheduler()
    T1_OVERNIGHT_SELECTION.candidate_tracking.start_scheduler()
    DAILY_KLINE_CACHE.start_scheduler()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    LOGGER.info("Serving A-share radar at http://%s:%s", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
