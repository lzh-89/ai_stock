from __future__ import annotations

import http.client
import hashlib
import json
import logging
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import copy
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from datetime import date, datetime, timedelta
from statistics import median
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from backend.app.database import Database
from backend.app.engines.filter_engine import hard_filter_reason
from backend.app.engines.scoring_engine import score_stock
from backend.app.engines.timing_engine import attach_plan
from backend.app.market_context_store import market_context_meta
from backend.app.providers.sector_provider import get_sector_provider
from backend.app.schema import RawStock
from backend.app.services.ai_narrator import _chat_completions_url, _extract_chat_content, _http_error_message
from backend.app.services.ai_selector import _extract_picks
from backend.app.services.analysis_service import AnalysisService
from backend.app.settings import Settings


LOGGER = logging.getLogger("ai_stock.t1_overnight")

DEFAULT_POOL_SIZE = 500
DEFAULT_BATCH_SIZE = 80
DEFAULT_BATCH_PICK = 8
DEFAULT_FINAL_LIMIT = 3
PORTFOLIO_SLOT_COUNT = 3
DEFAULT_SCAN_TIME = "14:40"
T1_TIMEOUT_SECONDS = 90
T1_MAX_TOKENS = 3500
CRITIC_CONFIG_KEY = "t1_overnight_critic_config"
EXECUTION_CONFIG_KEY = "t1_overnight_execution_config"
STAGE_CONFIG_KEY = "t1_overnight_stage_config"
MARKET_TURNOVER_HISTORY_KEY = "t1_overnight_market_turnover_history"
AI_RESULT_CACHE_KEY = "t1_overnight_ai_result_cache"
HISTORY_SOFT_CLEANUP_SETTING_KEY = "t1_overnight_history_soft_cleanup"
VALIDATION_AUTO_FREEZE_STATE_KEY = "t1_overnight_validation_auto_freeze_state"
AUTO_RUN_STATE_KEY = "t1_overnight_auto_run_state"
MARKET_PREFETCH_STATE_KEY = "t1_overnight_market_prefetch_state"
TRADEABLE_VALIDATION_VERSION = "v2_tradeable"
THEORETICAL_VALIDATION_VERSION = "v1_theoretical"
HISTORY_SOFT_CLEANUP_VERSION = "v2_semantic_evidence_isolation"
ZERO_PICK_SELECTION_MODES = {"market_risk_zero_pick", "data_quality_zero_pick", "guardrail_zero_pick"}
PRIMARY_TRADEABLE_SCENARIO_KEY = "open_15m"
SUCCESS_NET_RETURN_THRESHOLD_PCT = 0.2
STRONG_SUCCESS_NET_RETURN_THRESHOLD_PCT = 3.0
LOW_OPEN_FAIL_THRESHOLD_PCT = -1.5
INVALID_DRAWDOWN_THRESHOLD_PCT = -3.0
PRICE_MISMATCH_WARNING_PCT = 0.5
PRICE_MISMATCH_INVALID_PCT = 1.0
PRICE_MISMATCH_SEVERE_PCT = 5.0
CORPORATE_ACTION_PRE_CLOSE_MISMATCH_PCT = 1.0
CORPORATE_ACTION_EXTREME_GAP_PCT = 20.0
OFFICIAL_SCAN_START = "14:30"
OFFICIAL_SCAN_END = "14:50"
DAILY_FINAL_AFTER_TIME = "15:05"
AI_RESULT_CACHE_LIMIT = 80
AI_CALL_AUDIT_LIMIT = 24
BASELINE_MARKET_SAMPLE_LIMIT = 60
RANDOM_BASELINE_ITERATIONS = 30
AI_EVIDENCE_VALIDATION_VERSION = "v4_structured_selection_slip"
T1_EVIDENCE_VERSION = "v4_layered_sector_state_shadow"
T1_TRADE_DATE_GUARD_VERSION = "v1_t1_trade_date_guard"
T1_SECTOR_STATE_SHADOW_VERSION = "v1_t1_sector_state_shadow"
T1_STYLE_REGIME_SHADOW_VERSION = "v1_t1_style_regime_shadow"
T1_EXECUTION_CONTRACT_VERSION = "v1_signal_published_execution"
T1_TAIL_SUPPORT_SHADOW_VERSION = "v2_tail_support_shadow"
T1_GAP_DOWN_RISK_SHADOW_VERSION = "v1_gap_down_risk_shadow"
T1_RULE_TAIL_SHADOW_VERSION = "v1_rule_tail_shadow_top3"
T1_DEEPSEEK_TAIL_SHADOW_VERSION = "v1_deepseek_tail_shadow_top3"
T1_TAIL_MINUTE_PERIOD = "1m"
T1_OFFICIAL_MIN_POOL_SIZE = 300
T1_PRODUCTION_POOL_SIZE = 500
T1_STANDARD_POOL_SIZE = 300
T1_QUICK_POOL_SIZE = 80
T1_REUSABLE_EVIDENCE_QUALITIES = ("A", "B", "C")
T1_REUSABLE_LIMITED_MIN_MINUTE_RATIO = 0.75
T1_REUSABLE_LIMITED_MIN_IDENTITY_RATIO = 0.55
T1_FULL_MARKET_FREEZE_REUSE_SECONDS = int(os.getenv("T1_FULL_MARKET_FREEZE_REUSE_SECONDS", str(30 * 60)))
T1_EVIDENCE_INDUSTRY_CONSTITUENT_LIMIT = 800
T1_EVIDENCE_CONCEPT_CONSTITUENT_LIMIT = 120
T1_EVIDENCE_MAX_INDUSTRY_SECTORS = 999
T1_EVIDENCE_MAX_CONCEPT_SECTORS = 120
T1_EVIDENCE_MAX_CONSTITUENT_TARGET_SECTORS = int(os.getenv("T1_EVIDENCE_MAX_CONSTITUENT_TARGET_SECTORS", "48"))
T1_INTERNAL_SECTOR_SOURCE = "t1_internal_candidate_aggregate"
T1_INTERNAL_MIN_SECTOR_SAMPLE_COUNT = int(os.getenv("T1_INTERNAL_MIN_SECTOR_SAMPLE_COUNT", "3"))
T1_INTERNAL_SECTOR_STRENGTH_CAP = int(os.getenv("T1_INTERNAL_SECTOR_STRENGTH_CAP", "68"))
T1_EVIDENCE_CONSTITUENT_TIMEOUT_SECONDS = int(os.getenv("T1_EVIDENCE_CONSTITUENT_TIMEOUT_SECONDS", str(7 * 60)))
T1_EVIDENCE_STOCK_PROFILE_TIMEOUT_SECONDS = int(os.getenv("T1_EVIDENCE_STOCK_PROFILE_TIMEOUT_SECONDS", str(2 * 60)))
T1_RUN_LEASE_SECONDS = max(60, int(os.getenv("T1_RUN_LEASE_SECONDS", str(15 * 60))))
T1_WATCHDOG_INTERVAL_SECONDS = max(15, int(os.getenv("T1_WATCHDOG_INTERVAL_SECONDS", "30")))
T1_STOCK_IDENTITY_CACHE_TTL_DAYS = int(os.getenv("T1_STOCK_IDENTITY_CACHE_TTL_DAYS", "30"))
T1_STOCK_PROFILE_MAX_WORKERS = int(os.getenv("T1_STOCK_PROFILE_MAX_WORKERS", "4"))
T1_MINUTE_FETCH_CHUNK_SIZE = int(os.getenv("T1_MINUTE_FETCH_CHUNK_SIZE", "80"))
T1_MINUTE_FETCH_ATTEMPTS = int(os.getenv("T1_MINUTE_FETCH_ATTEMPTS", "2"))
T1_MINUTE_FETCH_CHUNK_SLEEP_SECONDS = float(os.getenv("T1_MINUTE_FETCH_CHUNK_SLEEP_SECONDS", "0.15"))
T1_MINUTE_FETCH_RETRY_SLEEP_SECONDS = float(os.getenv("T1_MINUTE_FETCH_RETRY_SLEEP_SECONDS", "0.8"))
T1_MINUTE_MAX_CODES = max(1, int(os.getenv("T1_MINUTE_MAX_CODES", "120")))
T1_TAIL_MINUTE_MAX_CODES = max(1, int(os.getenv("T1_TAIL_MINUTE_MAX_CODES", "30")))
T1_ENABLE_THS_CONSTITUENTS = str(os.getenv("T1_ENABLE_THS_CONSTITUENTS", "0")).lower() in {"1", "true", "yes"}

AI_SELECTION_OUTPUT_PROTOCOL = "structured_selection_slip"
AI_STRUCTURED_TEXT_FIELDS = (
    "reason",
    "strategyHypothesis",
    "buyPlan",
    "sellPlan",
    "risk",
    "entryCondition",
    "invalidCondition",
    "marketFit",
    "sectorFit",
    "confidenceReason",
)
AI_DECISION_TAG_LABELS = {
    "tail_support": "尾盘承接",
    "vwap_support": "VWAP 承接",
    "volume_confirmed": "量能确认",
    "premium_potential": "次日溢价潜力",
    "low_overnight_risk": "隔夜风险相对可控",
    "market_fit": "市场环境适配",
    "sector_identity_available": "行业/概念身份可用",
    "sector_snapshot_available": "同日板块快照可用",
    "sector_state_supported": "引用系统板块状态",
    "counter_risk_accepted": "已接受反方风险",
    "data_quality_ok": "数据质量满足终选",
    "liquidity_ok": "流动性可交易",
    "avoid_chase": "避免追高",
}
def _env_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


T1_IDENTITY_LIVE_FETCH_ENABLED = _env_bool(os.getenv("T1_IDENTITY_LIVE_FETCH_ENABLED"), False)
T1_TRADING_DAY_GUARD_ENV = os.getenv("T1_TRADING_DAY_GUARD_ENABLED")
T1_SECTOR_STATE_SHADOW_ENV = os.getenv("T1_SECTOR_STATE_SHADOW_ENABLED")
T1_SECTOR_STATE_PROMPT_ENV = os.getenv("T1_SECTOR_STATE_PROMPT_ENABLED")
T1_SECTOR_RETREAT_DOWNGRADE_ENV = os.getenv("T1_SECTOR_RETREAT_DOWNGRADE_ENABLED")
T1_STYLE_REGIME_SHADOW_ENV = os.getenv("T1_STYLE_REGIME_SHADOW_ENABLED")
T1_STYLE_REGIME_PROMPT_ENV = os.getenv("T1_STYLE_REGIME_PROMPT_ENABLED")
T1_TRADING_DAY_GUARD_ENABLED = _env_bool(T1_TRADING_DAY_GUARD_ENV, True)
T1_SECTOR_STATE_SHADOW_ENABLED = _env_bool(T1_SECTOR_STATE_SHADOW_ENV, True)
T1_SECTOR_STATE_PROMPT_ENABLED = _env_bool(T1_SECTOR_STATE_PROMPT_ENV, False)
T1_SECTOR_RETREAT_DOWNGRADE_ENABLED = _env_bool(T1_SECTOR_RETREAT_DOWNGRADE_ENV, False)
T1_STYLE_REGIME_SHADOW_ENABLED = _env_bool(T1_STYLE_REGIME_SHADOW_ENV, True)
T1_STYLE_REGIME_PROMPT_ENABLED = _env_bool(T1_STYLE_REGIME_PROMPT_ENV, False)
MINUTE_UNDATED_NEXT_POINT_MAX_CALENDAR_DAYS = 4
LISTING_BOARD_NAMES = {"沪市主板", "深市主板", "创业板", "科创板", "北交所", "B股", "其他"}
T1_SECTOR_STATES = {
    "rotation_in",
    "strong",
    "overheated",
    "neutral",
    "weakening",
    "distribution",
    "retreat",
    "oversold_repair",
    "data_insufficient",
}
T1_SECTOR_STATE_LABELS = {
    "rotation_in": "轮入观察",
    "strong": "强势延续",
    "overheated": "高热兑现",
    "neutral": "中性分化",
    "weakening": "转弱",
    "distribution": "派发分歧",
    "retreat": "退潮",
    "oversold_repair": "超跌修复",
    "data_insufficient": "证据不足",
}
TRADEABLE_SCENARIO_DEFINITIONS = [
    {"key": "open", "label": "次日开盘", "source": "open", "time": "09:30"},
    {"key": "open_5m", "label": "开盘后5分钟", "source": "minute", "time": "09:35"},
    {"key": "open_15m", "label": "开盘后15分钟", "source": "minute", "time": "09:45"},
    {"key": "open_30m", "label": "开盘后30分钟", "source": "minute", "time": "10:00"},
    {"key": "ten_thirty", "label": "10:30", "source": "minute", "time": "10:30"},
    {"key": "midday_close", "label": "上午收盘", "source": "minute", "time": "11:30"},
    {"key": "afternoon_open", "label": "下午开盘", "source": "minute", "time": "13:00"},
    {"key": "one_thirty", "label": "13:30", "source": "minute", "time": "13:30"},
    {"key": "two_pm", "label": "14点", "source": "minute", "time": "14:00"},
    {"key": "two_thirty", "label": "14:30", "source": "minute", "time": "14:30"},
    {"key": "close", "label": "次日收盘", "source": "close", "time": "15:00"},
]
TRADEABLE_SCENARIO_KEYS = {item["key"] for item in TRADEABLE_SCENARIO_DEFINITIONS}
MINUTE_SCENARIO_KEYS = {item["key"] for item in TRADEABLE_SCENARIO_DEFINITIONS if item["source"] == "minute"}
LEGACY_PRIMARY_SCENARIO_TEXT = "open_15m 优先，缺失时依次使用 open_5m/open_30m/open/close"
INDEX_BENCHMARKS = [
    {"code": "510300", "name": "沪深300ETF"},
    {"code": "512100", "name": "中证1000ETF"},
    {"code": "159915", "name": "创业板ETF"},
]
DEFAULT_EXECUTION_PROFILE = {
    "buySlippagePct": 0.10,
    "sellSlippagePct": 0.10,
    "commissionPct": 0.025,
    "stampTaxPct": 0.05,
    "transferFeePct": 0.0,
    "buyTimes": ["14:30", "14:40", "14:50"],
    "sellTimes": ["open", "open_5m", "open_15m", "open_30m", "ten_thirty", "midday_close", "afternoon_open", "one_thirty", "two_pm", "two_thirty", "close"],
}
DEFAULT_CRITIC_CONFIG = {
    "mode": "auto",
    "minCompletedRuns": 10,
    "minVerifiedOutcomes": 20,
    "maxReplacements": 1,
    "historyWindowRuns": 20,
    "requireOutcomes": True,
    "allowReducePickCount": True,
    "allowZeroPick": True,
    "requireMarketState": True,
    "minTradeableValidationSamples": 20,
}
DEFAULT_STAGE_CONFIG = {
    "sectorStateShadowEnabled": True,
    "sectorRetreatDowngradeMode": "off",
    "styleRegimeShadowEnabled": True,
    "styleRegimePromptMode": "off",
    "minOfficialTradeDaysForStage2": 30,
    "minTradeableOutcomesForStage2": 30,
    "minOfficialTradeDaysForStage3": 60,
    "minTradeableOutcomesForStage3": 60,
}
DEFAULT_VALIDATION_AUTO_FREEZE_CONFIG = {
    "enabled": True,
    "scheduleTimes": ["09:46", "10:05", "15:10"],
    "pollIntervalSeconds": 60,
    "scope": "all",
    "period": "1m",
    "lookbackRunLimit": 500,
    "retainDays": 10,
}
DEFAULT_AUTO_RUN_CONFIG = {
    "enabled": True,
    "scheduleTimes": [DEFAULT_SCAN_TIME],
    "latestTriggerTime": OFFICIAL_SCAN_END,
    "pollIntervalSeconds": 60,
    "source": "auto",
    "scanTime": DEFAULT_SCAN_TIME,
    "poolSize": DEFAULT_POOL_SIZE,
    "batchSize": DEFAULT_BATCH_SIZE,
    "batchPick": DEFAULT_BATCH_PICK,
    "finalLimit": DEFAULT_FINAL_LIMIT,
    "force": False,
    "retainDays": 10,
}
DEFAULT_MARKET_PREFETCH_CONFIG = {
    "enabled": True,
    "scheduleTimes": ["14:20", "14:25", "14:30", "14:35", "14:39", "14:40"],
    "pollIntervalSeconds": 45,
    "source": "auto",
    "retainDays": 10,
}


class T1OvernightSelectionService:
    def __init__(self, settings: Settings, analysis_service: AnalysisService, database: Database):
        self.settings = settings
        self.analysis_service = analysis_service
        self.database = database
        self._lock = threading.Lock()
        self._threads: Dict[int, threading.Thread] = {}
        self._validation_auto_freeze_thread: Optional[threading.Thread] = None
        self._validation_auto_freeze_lock = threading.Lock()
        self._auto_run_thread: Optional[threading.Thread] = None
        self._auto_run_lock = threading.Lock()
        self._market_prefetch_thread: Optional[threading.Thread] = None
        self._market_prefetch_lock = threading.Lock()
        self._identity_backfill_lock = threading.Lock()
        self._identity_backfill_codes: set[str] = set()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()

    def start_watchdog(self) -> Dict[str, Any]:
        with self._lock:
            if self._watchdog_thread and self._watchdog_thread.is_alive():
                return {"alive": True, "workerId": self.worker_id}
            self._watchdog_stop.clear()
            self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="t1-watchdog", daemon=True)
            self._watchdog_thread.start()
        return {"alive": True, "workerId": self.worker_id}

    def watchdog_status(self, recover: bool = False) -> Dict[str, Any]:
        result = self.database.recover_stale_t1_runs() if recover else {"selection": [], "evidence": []}
        return {
            "alive": bool(self._watchdog_thread and self._watchdog_thread.is_alive()),
            "workerId": self.worker_id,
            "intervalSeconds": T1_WATCHDOG_INTERVAL_SECONDS,
            "leaseSeconds": T1_RUN_LEASE_SECONDS,
            "recovered": result,
        }

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(T1_WATCHDOG_INTERVAL_SECONDS):
            try:
                self.database.recover_stale_t1_runs()
            except Exception as error:  # noqa: BLE001 - watchdog must keep running.
                LOGGER.warning("t1 watchdog failed error=%s", error)

    def market_prefetch_status(self) -> Dict[str, Any]:
        state = _normalize_market_prefetch_state(self.database.get_app_setting(MARKET_PREFETCH_STATE_KEY, {}))
        state["schedulerAlive"] = bool(self._market_prefetch_thread and self._market_prefetch_thread.is_alive())
        state["nextSlots"] = _market_prefetch_due_slots(
            datetime.now(), state.get("config") or DEFAULT_MARKET_PREFETCH_CONFIG, state, include_future=True
        )
        return state

    def start_market_prefetch_scheduler(self) -> Dict[str, Any]:
        state = _normalize_market_prefetch_state(self.database.get_app_setting(MARKET_PREFETCH_STATE_KEY, {}))
        config = state.get("config") or DEFAULT_MARKET_PREFETCH_CONFIG
        if not bool(config.get("enabled")):
            state["schedulerAlive"] = False
            return state
        with self._market_prefetch_lock:
            if self._market_prefetch_thread and self._market_prefetch_thread.is_alive():
                state["schedulerAlive"] = True
                return state
            self._market_prefetch_thread = threading.Thread(
                target=self._market_prefetch_loop,
                name="t1-market-prefetch",
                daemon=True,
            )
            self._market_prefetch_thread.start()
        state["schedulerAlive"] = True
        return state

    def run_market_prefetch_due(
        self,
        trigger_source: str = "manual",
        now: Optional[datetime] = None,
        slot: str = "",
        force: bool = False,
    ) -> Dict[str, Any]:
        now_dt = now or datetime.now()
        with self._market_prefetch_lock:
            state = _normalize_market_prefetch_state(self.database.get_app_setting(MARKET_PREFETCH_STATE_KEY, {}))
            config = state.get("config") or DEFAULT_MARKET_PREFETCH_CONFIG
            checked_at = now_dt.isoformat(timespec="seconds")
            state["lastCheckAt"] = checked_at
            if not bool(config.get("enabled")) and not force:
                state["lastStatus"] = "disabled"
                self.database.set_app_setting(MARKET_PREFETCH_STATE_KEY, state)
                return {**state, "processed": []}
            today = now_dt.date().isoformat()
            if now_dt.weekday() >= 5 and not force:
                state["lastStatus"] = "non_trading_day_skipped"
                state["lastMessage"] = "今天是周末，全市场 T+1 预取跳过。"
                self.database.set_app_setting(MARKET_PREFETCH_STATE_KEY, state)
                return {**state, "processed": []}
            slots = [_normalize_scan_time(slot)] if slot else _market_prefetch_due_slots(now_dt, config, state)
            processed: List[Dict[str, Any]] = []
            for due_slot in slots:
                try:
                    snapshot = self.analysis_service.full_market_context_store.prefetch(
                        source=str(config.get("source") or "auto"), force=True
                    )
                    meta = market_context_meta(snapshot)
                    event = {
                        "status": "done",
                        "slot": due_slot,
                        "tradeDate": today,
                        "triggerSource": trigger_source,
                        "checkedAt": checked_at,
                        "snapshotVersion": meta.get("snapshotVersion") or "",
                        "source": meta.get("source") or "",
                        "snapshotStatus": meta.get("status") or "",
                        "coverage": meta.get("coverage") or {},
                        "failedPages": meta.get("failedPages") or [],
                        "message": f"{today} {due_slot} 全市场快照预取完成。",
                    }
                except Exception as error:  # noqa: BLE001 - scheduler must remain alive.
                    LOGGER.warning("t1 market prefetch failed slot=%s error=%s", due_slot, error)
                    event = {
                        "status": "failed",
                        "slot": due_slot,
                        "tradeDate": today,
                        "triggerSource": trigger_source,
                        "checkedAt": checked_at,
                        "message": str(error),
                    }
                processed.append(event)
                _record_market_prefetch_event(state, today, due_slot, event, config)
            state["lastStatus"] = processed[-1].get("status") if processed else "waiting"
            state["lastMessage"] = processed[-1].get("message", "") if processed else "未到全市场预取时间。"
            self.database.set_app_setting(MARKET_PREFETCH_STATE_KEY, state)
            return {**state, "processed": processed}

    def _market_prefetch_loop(self) -> None:
        while True:
            try:
                state = _normalize_market_prefetch_state(self.database.get_app_setting(MARKET_PREFETCH_STATE_KEY, {}))
                config = state.get("config") or DEFAULT_MARKET_PREFETCH_CONFIG
                if bool(config.get("enabled")):
                    self.run_market_prefetch_due(trigger_source="scheduler")
                interval = _clamp_int(config.get("pollIntervalSeconds"), 15, 300, 45)
            except Exception as error:  # noqa: BLE001 - scheduler must not kill the server.
                LOGGER.warning("t1 market prefetch scheduler tick failed error=%s", error)
                interval = 45
            time.sleep(interval)

    def _queue_t1_identity_backfill(self, stocks: List[RawStock], source: str = "auto") -> int:
        """Refresh missing identity rows off the formal T+1 critical path."""
        candidates = []
        with self._identity_backfill_lock:
            for stock in stocks:
                code = _normalize_code(stock.code)
                if code and code not in self._identity_backfill_codes:
                    self._identity_backfill_codes.add(code)
                    candidates.append(stock)
        if not candidates:
            return 0

        def worker() -> None:
            try:
                provider = get_sector_provider("mock" if source == "mock" else "auto")
                fetcher = getattr(provider, "fetch_stock_sector_profile", None)
                if not callable(fetcher):
                    return
                rows: List[Dict[str, Any]] = []
                for stock in candidates:
                    try:
                        profile = fetcher(_normalize_code(stock.code))
                        mapped = _profile_map_from_identity(
                            profile,
                            stock,
                            evidence_run_id=0,
                            recommendation_date=date.today().isoformat(),
                            scan_time=DEFAULT_SCAN_TIME,
                            identity_status="available",
                        )
                        if mapped:
                            rows.extend(_identity_cache_rows_from_profile_maps([mapped]))
                    except Exception as error:  # noqa: BLE001 - one missing identity must not stop the queue.
                        LOGGER.warning("t1 async identity backfill failed code=%s error=%s", stock.code, error)
                if rows:
                    self.database.upsert_t1_stock_sector_identity_cache(rows)
            except Exception as error:  # noqa: BLE001 - background refresh is best effort.
                LOGGER.warning("t1 async identity backfill failed error=%s", error)
            finally:
                with self._identity_backfill_lock:
                    for stock in candidates:
                        self._identity_backfill_codes.discard(_normalize_code(stock.code))

        threading.Thread(target=worker, name="t1-identity-backfill", daemon=True).start()
        return len(candidates)

    def _start_run_heartbeat(self, run_id: int) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()
        interval = max(5.0, min(30.0, T1_RUN_LEASE_SECONDS / 3))

        def refresh() -> None:
            while not stop.wait(interval):
                now = datetime.now()
                heartbeat = now.isoformat(timespec="seconds")
                lease = (now + timedelta(seconds=T1_RUN_LEASE_SECONDS)).isoformat(timespec="seconds")
                try:
                    run = self.database.load_t1_overnight_selection_run(run_id)
                    if not run or run.get("status") not in {"pending", "running"}:
                        return
                    self.database.update_t1_overnight_selection_run(
                        run_id,
                        {"heartbeatAt": heartbeat, "leaseExpiresAt": lease},
                    )
                    evidence_id = int(run.get("evidenceRunId") or 0)
                    if evidence_id > 0:
                        evidence = self.database.load_t1_overnight_evidence_run(evidence_id)
                        if evidence and evidence.get("status") in {"pending", "running"}:
                            self.database.update_t1_overnight_evidence_run(
                                evidence_id,
                                {"heartbeatAt": heartbeat, "leaseExpiresAt": lease},
                            )
                except Exception as error:  # noqa: BLE001 - watchdog handles a missed beat.
                    LOGGER.warning("t1 heartbeat refresh failed id=%s error=%s", run_id, error)

        thread = threading.Thread(target=refresh, name=f"t1-heartbeat-{run_id}", daemon=True)
        thread.start()
        return stop, thread

    def start_run(
        self,
        risk_mode: str = "balanced",
        source: str = "auto",
        scan_time: str = DEFAULT_SCAN_TIME,
        force: bool = False,
        pool_size: int = DEFAULT_POOL_SIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_pick: int = DEFAULT_BATCH_PICK,
        final_limit: int = DEFAULT_FINAL_LIMIT,
        trigger: str = "manual",
        auto_schedule: bool = False,
    ) -> Dict[str, Any]:
        risk_mode = "balanced"
        source = source if source in {"auto", "sina", "akshare", "mock"} else "auto"
        scan_time = _normalize_scan_time(scan_time)
        pool_size = _clamp_int(pool_size, 80, 600, DEFAULT_POOL_SIZE)
        batch_size = _clamp_int(batch_size, 20, 100, DEFAULT_BATCH_SIZE)
        batch_pick = _clamp_int(batch_pick, 2, 12, DEFAULT_BATCH_PICK)
        final_limit = _clamp_int(final_limit, 1, 5, DEFAULT_FINAL_LIMIT)
        run_profile = _run_profile_for_pool(pool_size)
        recommendation_date = date.today().isoformat()
        stage_config = self.stage_config()
        stage_status = self.stage_status(stage_config)
        trade_date_status = _t1_trade_date_status(recommendation_date, scan_time, actual_source=source)
        run_role = _initial_run_role(scan_time, force, pool_size)
        if _t1_trade_date_blocks_official(trade_date_status):
            run_role = "invalid"
        elif str(trade_date_status.get("eligibility") or "") == "research_only":
            run_role = "research"
        elif _stage_controls_force_research(stage_status):
            run_role = "research"

        latest = self.database.latest_t1_overnight_selection_run(risk_mode=risk_mode, requested_source=source)
        if (
            latest
            and latest.get("status") in {"pending", "running", "done"}
            and str(latest.get("recommendationDate") or "") == recommendation_date
            and not force
            and _same_run_request(
                latest,
                scan_time=scan_time,
                pool_size=pool_size,
                batch_size=batch_size,
                batch_pick=batch_pick,
                final_limit=final_limit,
                stage_config=stage_config,
            )
        ):
            self._resume_thread_if_needed(latest)
            run = self.get_run(int(latest["id"]))
            run["reusedExisting"] = True
            return run

        run_id = self.database.create_t1_overnight_selection_run(
            {
                "recommendationDate": recommendation_date,
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
                    "riskMode": risk_mode,
                    "source": source,
                    "scanTime": scan_time,
                    "poolSize": pool_size,
                    "batchSize": batch_size,
                    "batchPick": batch_pick,
                    "finalLimit": final_limit,
                    "trigger": str(trigger or "manual"),
                    "autoSchedule": bool(auto_schedule),
                    "force": bool(force),
                    "runProfile": run_profile,
                    "runRole": run_role,
                    "tradeDateStatus": trade_date_status,
                    "stageConfig": stage_config,
                    "stageControls": stage_status,
                },
                "workerId": self.worker_id,
                "leaseExpiresAt": (datetime.now() + timedelta(seconds=T1_RUN_LEASE_SECONDS)).isoformat(timespec="seconds"),
                "market": {
                    "scanTime": scan_time,
                    "recommendedFinalTime": DEFAULT_SCAN_TIME,
                    "triggerMode": str(trigger or "manual"),
                    "timeHint": _time_hint(scan_time),
                    "ai": _ai_meta(self.settings),
                    "runProfile": run_profile,
                    "runRole": run_role,
                    "tradeDateStatus": trade_date_status,
                    "recommendationPolicy": _trade_date_block_policy(trade_date_status)
                    if _t1_trade_date_blocks_official(trade_date_status)
                    else {},
                    "stageControls": stage_status,
                },
            },
            [],
        )
        if _t1_trade_date_blocks_official(trade_date_status):
            reason = _t1_trade_date_block_reason(trade_date_status)
            self.database.update_t1_overnight_selection_run(
                run_id,
                {
                    "status": "done",
                    "selectionMode": "invalid_non_trading_day",
                    "currentStage": "done",
                    "finalPicks": [],
                    "finalCount": 0,
                    "error": reason,
                    "finishedAt": datetime.now().isoformat(timespec="seconds"),
                },
            )
            run = self.get_run(run_id)
            run["reusedExisting"] = False
            return run
        self._start_thread(run_id)
        run = self.get_run(run_id)
        run["reusedExisting"] = False
        return run

    def get_run(self, run_id: int) -> Dict[str, Any]:
        run = self.database.load_t1_overnight_selection_run(run_id)
        if not run:
            raise ValueError(f"未找到 T+1 超短线筛选任务 {run_id}")
        items = self.database.load_t1_overnight_selection_items(run_id)
        outcomes = self.database.load_t1_overnight_selection_outcomes(run_id)
        scenarios = self.database.load_t1_overnight_validation_scenarios(run_id)
        outcomes = _attach_validation_scenarios(outcomes, scenarios)
        run["items"] = items
        run["outcomes"] = outcomes
        run["validationScenarios"] = scenarios
        run["outcomeStatus"] = _outcome_status(run, outcomes)
        run["outcomeSummary"] = _outcome_summary(outcomes)
        run["tradeableSummary"] = _tradeable_summary(outcomes)
        run["baselineComparison"] = (run.get("market") or {}).get("baselineComparison") or {}
        run["runRole"] = _run_role(run)
        evidence_run_id = int(run.get("evidenceRunId") or (run.get("market") or {}).get("evidenceRunId") or 0)
        run["evidenceRun"] = self.database.load_t1_overnight_evidence_run(evidence_run_id) if evidence_run_id > 0 else None
        run["criticStatus"] = self.critic_status()
        run["stageStatus"] = self.stage_status()
        run["strategyFeedback"] = self.latest_strategy_feedback()
        run["progressPct"] = _progress_pct(run)
        run["elapsedSeconds"] = _elapsed_seconds(run)
        run["timeHint"] = _time_hint(str(run.get("scanTime") or ""))
        return run

    def latest(self, risk_mode: str = "balanced", source: str = "auto") -> Dict[str, Any]:
        risk_mode = "balanced"
        source = source if source in {"auto", "sina", "akshare", "mock"} else "auto"
        run = self.database.latest_t1_overnight_selection_run(risk_mode=risk_mode, requested_source=source)
        if not run:
            return {"status": "empty", "riskMode": risk_mode, "requestedSource": source, "items": []}
        return self.get_run(int(run["id"]))

    def preheat_evidence(
        self,
        source: str = "auto",
        scan_time: str = DEFAULT_SCAN_TIME,
        pool_size: int = DEFAULT_POOL_SIZE,
    ) -> Dict[str, Any]:
        source = source if source in {"auto", "sina", "akshare", "mock"} else "auto"
        scan_time = _normalize_scan_time(scan_time)
        pool_size = _clamp_int(pool_size, T1_OFFICIAL_MIN_POOL_SIZE, 600, DEFAULT_POOL_SIZE)
        started = time.perf_counter()
        batch = self._build_pool(
            "balanced",
            source,
            scan_time,
            pool_size,
            force=False,
        )
        market = batch.get("market") or {}
        evidence = market.get("t1Evidence") or {}
        return {
            "status": "done",
            "source": source,
            "actualSource": batch.get("actualSource") or "",
            "scanTime": scan_time,
            "poolSize": int(pool_size),
            "poolCount": len(batch.get("pool") or []),
            "selectionPoolCount": len(batch.get("selectionPool") or []),
            "reusedExisting": bool(evidence.get("reusedExisting")),
            "reuseReason": str(evidence.get("reuseReason") or ""),
            "evidence": evidence,
            "dataQualityStats": market.get("dataQualityStats") or {},
            "minuteKlineStatus": market.get("minuteKlineStatus") or {},
            "warnings": _unique_strings(
                [
                    str(batch.get("error") or ""),
                    str(market.get("providerWarning") or ""),
                    *list(evidence.get("missing") or []),
                ]
            ),
            "elapsedSeconds": round(time.perf_counter() - started, 1),
        }

    def get_outcomes(self, run_id: int) -> Dict[str, Any]:
        run = self.database.load_t1_overnight_selection_run(run_id)
        if not run:
            raise ValueError(f"未找到 T+1 超短线筛选任务 {run_id}")
        outcomes = self.database.load_t1_overnight_selection_outcomes(run_id)
        scenarios = self.database.load_t1_overnight_validation_scenarios(run_id)
        outcomes = _attach_validation_scenarios(outcomes, scenarios)
        return {
            "runId": run_id,
            "status": _outcome_status(run, outcomes),
            "items": outcomes,
            "scenarios": scenarios,
            "summary": _outcome_summary(outcomes),
            "tradeableSummary": _tradeable_summary(outcomes),
            "baselineComparison": (run.get("market") or {}).get("baselineComparison") or {},
            "strategyFeedback": self.latest_strategy_feedback(),
        }

    def validation_auto_freeze_status(self) -> Dict[str, Any]:
        state = _normalize_validation_auto_freeze_state(
            self.database.get_app_setting(VALIDATION_AUTO_FREEZE_STATE_KEY, {})
        )
        state["schedulerAlive"] = bool(self._validation_auto_freeze_thread and self._validation_auto_freeze_thread.is_alive())
        state["nextSlots"] = _validation_auto_freeze_due_slots(
            datetime.now(),
            state.get("config") or DEFAULT_VALIDATION_AUTO_FREEZE_CONFIG,
            state,
            include_future=True,
        )
        return state

    def auto_run_status(self) -> Dict[str, Any]:
        state = _normalize_auto_run_state(self.database.get_app_setting(AUTO_RUN_STATE_KEY, {}))
        state["schedulerAlive"] = bool(self._auto_run_thread and self._auto_run_thread.is_alive())
        state["nextSlots"] = _validation_auto_freeze_due_slots(
            datetime.now(),
            state.get("config") or DEFAULT_AUTO_RUN_CONFIG,
            state,
            include_future=True,
        )
        return state

    def start_auto_run_scheduler(self) -> Dict[str, Any]:
        state = _normalize_auto_run_state(self.database.get_app_setting(AUTO_RUN_STATE_KEY, {}))
        config = state.get("config") or DEFAULT_AUTO_RUN_CONFIG
        if not bool(config.get("enabled")):
            state["schedulerAlive"] = False
            return state
        with self._auto_run_lock:
            if self._auto_run_thread and self._auto_run_thread.is_alive():
                state["schedulerAlive"] = True
                return state
            thread = threading.Thread(target=self._auto_run_loop, name="t1-auto-run", daemon=True)
            self._auto_run_thread = thread
            thread.start()
        state["schedulerAlive"] = True
        return state

    def run_auto_run_due(
        self,
        trigger_source: str = "manual",
        now: Optional[datetime] = None,
        slot: str = "",
        force: bool = False,
    ) -> Dict[str, Any]:
        now_dt = now or datetime.now()
        with self._auto_run_lock:
            state = _normalize_auto_run_state(self.database.get_app_setting(AUTO_RUN_STATE_KEY, {}))
            config = state.get("config") or DEFAULT_AUTO_RUN_CONFIG
            checked_at = now_dt.isoformat(timespec="seconds")
            state["lastCheckAt"] = checked_at
            if not bool(config.get("enabled")) and not force:
                state["lastStatus"] = "disabled"
                state["lastMessage"] = "T+1 自动生成推荐未开启。"
                self.database.set_app_setting(AUTO_RUN_STATE_KEY, state)
                return {**state, "processed": []}
            today = now_dt.date().isoformat()
            if now_dt.weekday() >= 5 and not force:
                event = _auto_run_skip_event(today, "weekend", checked_at, trigger_source, "今天是周末，自动生成 T+1 推荐跳过。")
                _record_validation_auto_freeze_event(state, today, "weekend", event, config)
                state["lastStatus"] = event["status"]
                state["lastMessage"] = event["message"]
                self.database.set_app_setting(AUTO_RUN_STATE_KEY, state)
                return {**state, "processed": [event]}

            slots = [_normalize_scan_time(slot)] if slot else _validation_auto_freeze_due_slots(now_dt, config, state)
            slots = [item for item in slots if item]
            if not slots:
                state["lastStatus"] = "waiting"
                state["lastMessage"] = "未到 T+1 自动生成时间，或今天的时间点已经处理过。"
                self.database.set_app_setting(AUTO_RUN_STATE_KEY, state)
                return {**state, "processed": []}

            processed = []
            for due_slot in slots:
                latest_trigger_time = str(config.get("latestTriggerTime") or OFFICIAL_SCAN_END)
                existing_event = self._auto_run_existing_event(today, due_slot, trigger_source, config, checked_at)
                if existing_event:
                    event = existing_event
                elif not force and now_dt.strftime("%H:%M") > latest_trigger_time:
                    event = _auto_run_skip_event(
                        today,
                        due_slot,
                        checked_at,
                        trigger_source,
                        f"已超过自动生成最晚时间 {latest_trigger_time}，为避免买入不可执行，今天不补跑正式 T+1 推荐。",
                        status="missed",
                    )
                else:
                    event = self._run_auto_run_slot(today, due_slot, trigger_source, config, checked_at)
                processed.append(event)
                _record_validation_auto_freeze_event(state, today, due_slot, event, config)

            last_event = processed[-1] if processed else {}
            state["lastStatus"] = last_event.get("status") or "waiting"
            state["lastMessage"] = last_event.get("message") or state.get("lastMessage") or ""
            state["lastRunAt"] = checked_at if processed else state.get("lastRunAt") or ""
            self.database.set_app_setting(AUTO_RUN_STATE_KEY, state)
            return {**state, "processed": processed}

    def _auto_run_loop(self) -> None:
        while True:
            try:
                state = _normalize_auto_run_state(self.database.get_app_setting(AUTO_RUN_STATE_KEY, {}))
                config = state.get("config") or DEFAULT_AUTO_RUN_CONFIG
                if bool(config.get("enabled")):
                    self.run_auto_run_due(trigger_source="scheduler")
                interval = _clamp_int(config.get("pollIntervalSeconds"), 30, 600, 60)
            except Exception as error:  # noqa: BLE001 - scheduler must not kill the server.
                LOGGER.warning("t1 auto-run scheduler tick failed error=%s", error)
                interval = 60
            time.sleep(interval)

    def _run_auto_run_slot(
        self,
        trade_date: str,
        slot: str,
        trigger_source: str,
        config: Dict[str, Any],
        checked_at: str,
    ) -> Dict[str, Any]:
        try:
            result = self.start_run(
                risk_mode="balanced",
                source=str(config.get("source") or "auto"),
                scan_time=str(config.get("scanTime") or slot or DEFAULT_SCAN_TIME),
                force=bool(config.get("force")),
                pool_size=int(config.get("poolSize") or DEFAULT_POOL_SIZE),
                batch_size=int(config.get("batchSize") or DEFAULT_BATCH_SIZE),
                batch_pick=int(config.get("batchPick") or DEFAULT_BATCH_PICK),
                final_limit=int(config.get("finalLimit") or DEFAULT_FINAL_LIMIT),
                trigger="auto_schedule",
                auto_schedule=True,
            )
        except Exception as error:  # noqa: BLE001 - record readable scheduler failure.
            LOGGER.warning("t1 auto-run failed slot=%s error=%s", slot, error)
            return {
                "status": "failed",
                "slot": slot,
                "tradeDate": trade_date,
                "triggerSource": trigger_source,
                "checkedAt": checked_at,
                "message": str(error),
            }
        run_id = int(result.get("id") or 0)
        reused = bool(result.get("reusedExisting"))
        status = "reused" if reused else "started"
        return {
            "status": status,
            "slot": slot,
            "tradeDate": trade_date,
            "triggerSource": trigger_source,
            "checkedAt": checked_at,
            "runId": run_id,
            "runStatus": result.get("status") or "",
            "reusedExisting": reused,
            "message": f"{trade_date} {slot} 已{'复用已有' if reused else '自动创建'} T+1 推荐 run {run_id}。",
        }

    def start_validation_auto_freeze_scheduler(self) -> Dict[str, Any]:
        state = _normalize_validation_auto_freeze_state(
            self.database.get_app_setting(VALIDATION_AUTO_FREEZE_STATE_KEY, {})
        )
        config = state.get("config") or DEFAULT_VALIDATION_AUTO_FREEZE_CONFIG
        if not bool(config.get("enabled")):
            state["schedulerAlive"] = False
            return state
        with self._validation_auto_freeze_lock:
            if self._validation_auto_freeze_thread and self._validation_auto_freeze_thread.is_alive():
                state["schedulerAlive"] = True
                return state
            thread = threading.Thread(target=self._validation_auto_freeze_loop, name="t1-validation-auto-freeze", daemon=True)
            self._validation_auto_freeze_thread = thread
            thread.start()
        state["schedulerAlive"] = True
        return state

    def run_validation_auto_freeze_due(
        self,
        trigger_source: str = "manual",
        now: Optional[datetime] = None,
        slot: str = "",
        force: bool = False,
    ) -> Dict[str, Any]:
        now_dt = now or datetime.now()
        with self._validation_auto_freeze_lock:
            state = _normalize_validation_auto_freeze_state(
                self.database.get_app_setting(VALIDATION_AUTO_FREEZE_STATE_KEY, {})
            )
            config = state.get("config") or DEFAULT_VALIDATION_AUTO_FREEZE_CONFIG
            checked_at = now_dt.isoformat(timespec="seconds")
            state["lastCheckAt"] = checked_at
            if not bool(config.get("enabled")) and not force:
                state["lastStatus"] = "disabled"
                self.database.set_app_setting(VALIDATION_AUTO_FREEZE_STATE_KEY, state)
                return {**state, "processed": []}

            today = now_dt.date().isoformat()
            if now_dt.weekday() >= 5 and not force:
                state["lastStatus"] = "non_trading_day_skipped"
                state["lastMessage"] = "今天是周末，自动冻结跳过。"
                self.database.set_app_setting(VALIDATION_AUTO_FREEZE_STATE_KEY, state)
                return {**state, "processed": []}

            slots = [_normalize_scan_time(slot)] if slot else _validation_auto_freeze_due_slots(now_dt, config, state)
            slots = [item for item in slots if item]
            if not slots:
                state["lastStatus"] = "waiting"
                state["lastMessage"] = "未到自动冻结时间，或今天的时间点已经处理过。"
                self.database.set_app_setting(VALIDATION_AUTO_FREEZE_STATE_KEY, state)
                return {**state, "processed": []}

            processed = []
            for due_slot in slots:
                event = self._run_validation_auto_freeze_slot(
                    target_trade_date=today,
                    slot=due_slot,
                    trigger_source=trigger_source,
                    config=config,
                    checked_at=checked_at,
                )
                processed.append(event)
                _record_validation_auto_freeze_event(state, today, due_slot, event, config)

            last_event = processed[-1] if processed else {}
            state["lastStatus"] = last_event.get("status") or "waiting"
            state["lastMessage"] = last_event.get("message") or state.get("lastMessage") or ""
            state["lastRunAt"] = checked_at
            self.database.set_app_setting(VALIDATION_AUTO_FREEZE_STATE_KEY, state)
            return {**state, "processed": processed}

    def _validation_auto_freeze_loop(self) -> None:
        while True:
            try:
                state = _normalize_validation_auto_freeze_state(
                    self.database.get_app_setting(VALIDATION_AUTO_FREEZE_STATE_KEY, {})
                )
                config = state.get("config") or DEFAULT_VALIDATION_AUTO_FREEZE_CONFIG
                if bool(config.get("enabled")):
                    self.run_validation_auto_freeze_due(trigger_source="scheduler")
                interval = _clamp_int(config.get("pollIntervalSeconds"), 30, 600, 60)
            except Exception as error:  # noqa: BLE001 - scheduler must not kill the server.
                LOGGER.warning("t1 validation auto-freeze scheduler tick failed error=%s", error)
                interval = 60
            time.sleep(interval)

    def _run_validation_auto_freeze_slot(
        self,
        target_trade_date: str,
        slot: str,
        trigger_source: str,
        config: Dict[str, Any],
        checked_at: str,
    ) -> Dict[str, Any]:
        runs = self._validation_auto_freeze_target_runs(
            target_trade_date,
            limit=int(config.get("lookbackRunLimit") or DEFAULT_VALIDATION_AUTO_FREEZE_CONFIG["lookbackRunLimit"]),
        )
        if not runs:
            return {
                "status": "no_runs",
                "slot": slot,
                "targetTradeDate": target_trade_date,
                "triggerSource": trigger_source,
                "checkedAt": checked_at,
                "runCount": 0,
                "message": f"{target_trade_date} 没有需要自动冻结验证分钟线的 T+1 run。",
                "results": [],
            }

        scope = str(config.get("scope") or "all")
        period = str(config.get("period") or "1m")
        results = []
        for run in runs:
            run_id = int(run.get("id") or 0)
            try:
                result = self.backfill_validation_minutes(run_id, scope=scope, force=False, period=period)
            except Exception as error:  # noqa: BLE001 - keep other runs moving.
                LOGGER.warning("t1 validation auto-freeze run failed run=%s slot=%s error=%s", run_id, slot, error)
                results.append(
                    {
                        "runId": run_id,
                        "status": "failed",
                        "message": str(error),
                        "roleSummary": {},
                    }
                )
            else:
                failed = int(result.get("failed") or 0)
                recoverable = bool(result.get("recoverable", failed <= 0))
                results.append(
                    {
                        "runId": run_id,
                        "status": "ok" if failed <= 0 else ("partial" if recoverable else "failed"),
                        "stored": int(result.get("stored") or 0),
                        "skipped": int(result.get("skipped") or 0),
                        "failed": failed,
                        "message": result.get("message") or "",
                        "roleSummary": result.get("roleSummary") or {},
                    }
                )

        failed_results = [item for item in results if item.get("status") == "failed"]
        partial_results = [item for item in results if item.get("status") == "partial"]
        status = "completed"
        if failed_results:
            status = "failed"
        elif partial_results:
            status = "partial"
        return {
            "status": status,
            "slot": slot,
            "targetTradeDate": target_trade_date,
            "triggerSource": trigger_source,
            "checkedAt": checked_at,
            "runCount": len(runs),
            "message": f"{target_trade_date} {slot} 自动冻结完成：处理 {len(runs)} 个 run，失败 {len(failed_results)} 个，部分完成 {len(partial_results)} 个。",
            "results": results,
        }

    def _validation_auto_freeze_target_runs(self, target_trade_date: str, limit: int = 500) -> List[Dict[str, Any]]:
        runs = self.database.list_t1_overnight_selection_runs(limit=limit, status="done")
        result = []
        for run in runs:
            recommendation_date = str(run.get("recommendationDate") or "")
            if not recommendation_date or _next_weekday_date(recommendation_date) != target_trade_date:
                continue
            if int(run.get("finalCount") or 0) <= 0:
                continue
            run_role = _run_role(run)
            if run_role in {"invalid", "legacy"}:
                continue
            selection_mode = str(run.get("selectionMode") or "")
            if selection_mode in {"invalid_non_trading_day"}:
                continue
            result.append(run)
        return sorted(result, key=lambda item: int(item.get("id") or 0))

    def _auto_run_existing_event(
        self,
        trade_date: str,
        slot: str,
        trigger_source: str,
        config: Dict[str, Any],
        checked_at: str,
    ) -> Dict[str, Any]:
        latest = self.database.latest_t1_overnight_selection_run(
            risk_mode="balanced",
            requested_source=str(config.get("source") or "auto"),
        )
        if not latest or str(latest.get("recommendationDate") or "") != trade_date:
            return {}
        if not _same_run_request(
            latest,
            scan_time=str(config.get("scanTime") or slot or DEFAULT_SCAN_TIME),
            pool_size=int(config.get("poolSize") or DEFAULT_POOL_SIZE),
            batch_size=int(config.get("batchSize") or DEFAULT_BATCH_SIZE),
            batch_pick=int(config.get("batchPick") or DEFAULT_BATCH_PICK),
            final_limit=int(config.get("finalLimit") or DEFAULT_FINAL_LIMIT),
            stage_config=self.stage_config(),
        ):
            return {}
        run_id = int(latest.get("id") or 0)
        self._resume_thread_if_needed(latest)
        return {
            "status": "reused",
            "slot": slot,
            "tradeDate": trade_date,
            "triggerSource": trigger_source,
            "checkedAt": checked_at,
            "runId": run_id,
            "runStatus": latest.get("status") or "",
            "reusedExisting": True,
            "message": f"{trade_date} {slot} 已存在同参数 T+1 推荐 run {run_id}，自动任务不重复生成。",
        }

    def review(self, limit: int = 60, run_id: int = 0, status: str = "", review_status: str = "") -> Dict[str, Any]:
        stats_limit = max(500, int(limit or 60))
        stats_runs = self.database.list_t1_overnight_selection_runs(limit=stats_limit, status=status)
        stats_summaries = [self._review_run_summary(run) for run in stats_runs]
        runs = stats_runs[: max(1, int(limit or 60))]
        summaries = stats_summaries[: len(runs)]
        normalized_review_status = str(review_status or "").strip()
        if normalized_review_status:
            summaries = [run for run in summaries if run.get("reviewStatus") == normalized_review_status]
        selected_run_id = int(run_id or 0)
        if selected_run_id <= 0:
            selected_run_id = _default_review_run_id(summaries)
        selected_run = self.database.load_t1_overnight_selection_run(selected_run_id) if selected_run_id > 0 else None
        selected = self._review_run_detail(selected_run) if selected_run else None
        if selected and normalized_review_status and selected.get("reviewStatus") != normalized_review_status:
            selected = None
            selected_run_id = _default_review_run_id(summaries)
            selected_run = self.database.load_t1_overnight_selection_run(selected_run_id) if selected_run_id > 0 else None
            selected = self._review_run_detail(selected_run) if selected_run else None
        return {
            "runs": [_public_review_summary(run) for run in summaries],
            "selectedRun": selected,
            "selectedRunId": selected_run_id,
            "count": len(summaries),
            "reviewStats": _review_stats(stats_summaries),
            "optimizationStats": _optimization_review_stats(stats_summaries),
            "validationAutoFreeze": self.validation_auto_freeze_status(),
            "reviewStatus": normalized_review_status,
        }

    def _review_run_summary(self, run: Dict[str, Any]) -> Dict[str, Any]:
        outcomes = self.database.load_t1_overnight_selection_outcomes(int(run.get("id") or 0))
        scenarios = self.database.load_t1_overnight_validation_scenarios(int(run.get("id") or 0))
        outcomes = _attach_validation_scenarios(outcomes, scenarios)
        summary = _outcome_summary(outcomes)
        tradeable_summary = _tradeable_summary(outcomes)
        outcome_status = _outcome_status(run, outcomes)
        run_role = _run_role(run)
        learning_eligible_count = len([item for item in outcomes if bool(_outcome_payload(item).get("learningEligible"))])
        sample = _review_sample_meta(
            run=run,
            outcome_status=outcome_status,
            run_role=run_role,
            tradeable_count=int(tradeable_summary.get("tradeableCount") or 0),
            outcome_count=int(run.get("outcomeCount") or len(outcomes) or 0),
            learning_eligible_count=learning_eligible_count,
            primary_entry_not_executable_count=int(tradeable_summary.get("primaryEntryNotExecutableCount") or 0),
            primary_minute_missing_count=int(tradeable_summary.get("primaryMinuteMissingCount") or 0),
        )
        display = _review_display_meta(run, sample, outcome_status, run_role)
        ai_decision = _review_ai_decision_meta(run)
        learning_scope = _review_learning_scope_meta(display, ai_decision, learning_eligible_count)
        return {
            "id": int(run.get("id") or 0),
            "recommendationDate": run.get("recommendationDate") or "",
            "scanTime": run.get("scanTime") or "",
            "status": run.get("status") or "",
            "outcomeStatus": outcome_status,
            "runRole": run_role,
            "sampleClass": sample["sampleClass"],
            "sampleLabel": sample["sampleLabel"],
            "sampleReason": sample["sampleReason"],
            "reviewStatus": display["reviewStatus"],
            "reviewStatusLabel": display["reviewStatusLabel"],
            "reviewStatusReason": display["reviewStatusReason"],
            "reviewStatusTone": display["reviewStatusTone"],
            "aiDecisionStatus": ai_decision["status"],
            "aiDecisionLabel": ai_decision["label"],
            "aiDecisionReason": ai_decision["reason"],
            "aiDecisionDetail": ai_decision.get("detail", ""),
            "aiDecisionTone": ai_decision["tone"],
            "learningScope": learning_scope["scope"],
            "learningScopeLabel": learning_scope["label"],
            "learningScopeReason": learning_scope["reason"],
            "selectionMode": run.get("selectionMode") or "",
            "actualSource": run.get("actualSource") or "",
            "requestedSource": run.get("requestedSource") or "",
            "poolSize": int(run.get("poolSize") or 0),
            "finalCount": int(run.get("finalCount") or 0),
            "outcomeCount": int(run.get("outcomeCount") or len(outcomes) or 0),
            "verifiedOutcomeCount": int(run.get("verifiedOutcomeCount") or summary.get("verifiedCount") or 0),
            "successCount": int(summary.get("successCount") or run.get("successCount") or 0),
            "strongSuccessCount": int(run.get("strongSuccessCount") or 0),
            "avgPrimaryNetReturnPct": tradeable_summary.get("avgNetReturnPct", run.get("avgPrimaryNetReturnPct")),
            "tradeableCount": int(tradeable_summary.get("tradeableCount") or 0),
            "learningEligibleCount": learning_eligible_count,
            "exitTimingStats": _exit_timing_stats(outcomes),
            "minuteMissingCount": int(tradeable_summary.get("minuteMissingCount") or 0),
            "finalPicks": _review_pick_names(run.get("finalPicks") or []),
            "marketLabel": ((run.get("market") or {}).get("marketContext") or {}).get("label") or "",
            "error": run.get("error") or "",
            "latestVerifiedAt": run.get("latestVerifiedAt") or "",
            "finishedAt": run.get("finishedAt") or "",
        }

    def _review_run_detail(self, run: Dict[str, Any]) -> Dict[str, Any]:
        run_id = int(run.get("id") or 0)
        outcomes = self.database.load_t1_overnight_selection_outcomes(run_id)
        scenarios = self.database.load_t1_overnight_validation_scenarios(run_id)
        outcomes = _attach_validation_scenarios(outcomes, scenarios)
        outcome_status = _outcome_status(run, outcomes)
        run_role = _run_role(run)
        tradeable_summary = _tradeable_summary(outcomes)
        learning_eligible_count = len([item for item in outcomes if bool(_outcome_payload(item).get("learningEligible"))])
        sample = _review_sample_meta(
            run=run,
            outcome_status=outcome_status,
            run_role=run_role,
            tradeable_count=int(tradeable_summary.get("tradeableCount") or 0),
            outcome_count=len(outcomes),
            learning_eligible_count=learning_eligible_count,
            primary_entry_not_executable_count=int(tradeable_summary.get("primaryEntryNotExecutableCount") or 0),
            primary_minute_missing_count=int(tradeable_summary.get("primaryMinuteMissingCount") or 0),
        )
        display = _review_display_meta(run, sample, outcome_status, run_role)
        ai_decision = _review_ai_decision_meta(run)
        learning_scope = _review_learning_scope_meta(display, ai_decision, learning_eligible_count)
        usage_explanation = _review_usage_explanation(
            run=run,
            outcomes=outcomes,
            tradeable_summary=tradeable_summary,
            sample=sample,
            display=display,
            ai_decision=ai_decision,
            learning_scope=learning_scope,
            outcome_status=outcome_status,
            run_role=run_role,
            learning_eligible_count=learning_eligible_count,
        )
        market = run.get("market") or {}
        baseline_comparison = _review_baseline_comparison(market.get("baselineComparison") or {})
        compact_market = {
            "marketContext": market.get("marketContext") or {},
            "recommendationPolicy": market.get("recommendationPolicy") or {},
            "baselineComparison": baseline_comparison,
        }
        return {
            "id": run_id,
            "recommendationDate": run.get("recommendationDate") or "",
            "scanTime": run.get("scanTime") or "",
            "status": run.get("status") or "",
            "outcomeStatus": outcome_status,
            "runRole": run_role,
            "sampleClass": sample["sampleClass"],
            "sampleLabel": sample["sampleLabel"],
            "sampleReason": sample["sampleReason"],
            "reviewStatus": display["reviewStatus"],
            "reviewStatusLabel": display["reviewStatusLabel"],
            "reviewStatusReason": display["reviewStatusReason"],
            "reviewStatusTone": display["reviewStatusTone"],
            "aiDecisionStatus": ai_decision["status"],
            "aiDecisionLabel": ai_decision["label"],
            "aiDecisionReason": ai_decision["reason"],
            "aiDecisionDetail": ai_decision.get("detail", ""),
            "aiDecisionTone": ai_decision["tone"],
            "learningScope": learning_scope["scope"],
            "learningScopeLabel": learning_scope["label"],
            "learningScopeReason": learning_scope["reason"],
            "usageExplanation": usage_explanation,
            "selectionMode": run.get("selectionMode") or "",
            "actualSource": run.get("actualSource") or "",
            "requestedSource": run.get("requestedSource") or "",
            "poolSize": int(run.get("poolSize") or 0),
            "finalCount": int(run.get("finalCount") or 0),
            "finalPicks": _review_pick_names(run.get("finalPicks") or []),
            "outcomes": _review_outcomes(outcomes),
            "outcomeSummary": _outcome_summary(outcomes),
            "tradeableSummary": tradeable_summary,
            "learningEligibleCount": learning_eligible_count,
            "baselineComparison": baseline_comparison,
            "market": compact_market,
            "error": run.get("error") or "",
            "finishedAt": run.get("finishedAt") or "",
        }

    def verify_run(self, run_id: int, trigger_source: str = "manual_verify") -> Dict[str, Any]:
        run = self.database.load_t1_overnight_selection_run(run_id)
        if not run:
            raise ValueError(f"未找到 T+1 超短线筛选任务 {run_id}")
        if run.get("status") != "done":
            raise ValueError("只有已完成的 T+1 run 可以做次日验证。")
        if _is_zero_pick_run(run):
            return _zero_pick_verification_result(run, self.latest_strategy_feedback())
        if str(run.get("recommendationDate") or "") >= date.today().isoformat():
            outcomes = self.database.load_t1_overnight_selection_outcomes(run_id)
            scenarios = self.database.load_t1_overnight_validation_scenarios(run_id)
            outcomes = _attach_validation_scenarios(outcomes, scenarios)
            return {
                "runId": run_id,
                "status": _outcome_status(run, outcomes),
                "items": outcomes,
                "scenarios": scenarios,
                "summary": _outcome_summary(outcomes),
                "tradeableSummary": _tradeable_summary(outcomes),
                "strategyFeedback": self.latest_strategy_feedback(),
            }
        picks = run.get("finalPicks") or []
        if not picks:
            picks = [
                item.get("pick") or item
                for item in self.database.load_t1_overnight_selection_items(run_id)
                if item.get("selected") and item.get("stage") == "final"
            ]
        if not picks:
            return _zero_pick_verification_result(run, self.latest_strategy_feedback())

        baseline_groups = (run.get("market") or {}).get("baselineGroups") or {}
        baseline_pick_groups = {
            key: [item for item in value if isinstance(item, dict)]
            for key, value in baseline_groups.items()
            if isinstance(value, list)
        }
        deepseek_tail_shadow = ((run.get("market") or {}).get("shadowDeepSeekTailTop3") or {}).get("picks") or []
        if isinstance(deepseek_tail_shadow, list) and deepseek_tail_shadow:
            baseline_pick_groups["deepSeekTailShadow"] = [item for item in deepseek_tail_shadow if isinstance(item, dict)]
        all_verification_picks = _unique_by_code(
            list(picks) + [item for group in baseline_pick_groups.values() for item in group]
        )
        codes = [str(item.get("code") or "").zfill(6) for item in all_verification_picks if str(item.get("code") or "").strip()]
        benchmark_codes = [item["code"] for item in INDEX_BENCHMARKS]
        fetch_codes = _unique_codes(codes + benchmark_codes)
        existing_outcomes = self.database.load_t1_overnight_selection_outcomes(run_id)
        existing_scenarios = self.database.load_t1_overnight_validation_scenarios(run_id)
        existing_outcomes = _attach_validation_scenarios(existing_outcomes, existing_scenarios)
        series_by_code = self.analysis_service.daily_kline_provider.fetch_many(fetch_codes, days=80, force=False)
        daily_source = self.analysis_service.daily_kline_provider.last_source or "daily_kline"
        db_series_by_code = self.database.load_daily_kline_series(fetch_codes)
        db_cache_used = False
        for code in fetch_codes:
            live_series = series_by_code.get(code) or []
            cached_series = db_series_by_code.get(code) or []
            if cached_series and (not live_series or not _next_trade_point(live_series, str(run.get("recommendationDate") or ""))):
                series_by_code[code] = cached_series
                db_cache_used = True
        recommendation_date = str(run.get("recommendationDate") or "")
        missing_recommendation_daily_codes = [
            code
            for code in fetch_codes
            if recommendation_date and not _trade_point_on_date(series_by_code.get(code, []), recommendation_date)
        ]
        if missing_recommendation_daily_codes:
            try:
                refreshed_series = self.analysis_service.daily_kline_provider.fetch_many(
                    missing_recommendation_daily_codes,
                    days=10,
                    force=True,
                    preferred_sources=["baostock", "eastmoney", "tencent", "tushare"],
                )
            except Exception as error:  # noqa: BLE001 - recommendation day OHLC has minute fallback.
                LOGGER.warning(
                    "t1 overnight recommendation daily refresh failed run=%s codes=%s error=%s",
                    run_id,
                    len(missing_recommendation_daily_codes),
                    error,
                )
                refreshed_series = {}
            if refreshed_series:
                refresh_source = self.analysis_service.daily_kline_provider.last_source or daily_source
                self.analysis_service._save_daily_kline_to_database(refreshed_series, refresh_source)
                for code, refreshed in refreshed_series.items():
                    normalized = str(code or "").zfill(6)
                    if _trade_point_on_date(refreshed or [], recommendation_date):
                        series_by_code[normalized] = refreshed
                if refresh_source and daily_source in {"", "daily_kline", "cache_daily", "daily_kline_db_cache", "mixed_daily_db_cache"}:
                    daily_source = refresh_source
        if db_cache_used and not daily_source:
            daily_source = "daily_kline_db_cache"
        elif db_cache_used and daily_source == "daily_kline":
            daily_source = "mixed_daily_db_cache"
        self.analysis_service._save_daily_kline_to_database(series_by_code, daily_source)
        minute_by_code: Dict[str, List[Dict[str, Any]]] = {}
        recommendation_minute_by_code: Dict[str, List[Dict[str, Any]]] = {}
        verification_codes = _unique_codes(codes + benchmark_codes)
        evidence_run_id = int(run.get("evidenceRunId") or (run.get("market") or {}).get("evidenceRunId") or 0)
        if evidence_run_id > 0 and recommendation_date:
            recommendation_snapshots = self.database.load_t1_overnight_minute_snapshots(
                evidence_run_id,
                verification_codes,
                period=self.settings.kline_period,
            )
            for code, snapshot in recommendation_snapshots.items():
                if str(snapshot.get("qualityStatus") or "") != "available":
                    continue
                series = _minute_series_from_t1_snapshot(snapshot, recommendation_date)
                if series:
                    recommendation_minute_by_code[str(code or "").zfill(6)] = series
        target_trade_date = _next_weekday_date(recommendation_date)
        minute_source_by_code: Dict[str, str] = {}
        if target_trade_date:
            validation_snapshots = self.database.load_t1_overnight_validation_minute_snapshots(
                run_id,
                verification_codes,
                target_trade_date=target_trade_date,
            )
            for code, snapshot in validation_snapshots.items():
                if str(snapshot.get("qualityStatus") or "") not in {"available", "partial"}:
                    continue
                series = _minute_series_from_validation_snapshot(snapshot, target_trade_date)
                if not series:
                    continue
                normalized = str(code or "").zfill(6)
                minute_by_code[normalized] = series
                minute_source_by_code[normalized] = str(snapshot.get("source") or "t1_validation_minute_snapshot")
            reusable_validation_snapshots = self.database.load_latest_t1_overnight_validation_minute_snapshots_any_run(
                target_trade_date,
                [code for code in verification_codes if code not in minute_by_code],
                exclude_run_id=run_id,
            )
            reusable_rows = []
            for code, snapshot in reusable_validation_snapshots.items():
                if str(snapshot.get("qualityStatus") or "") not in {"available", "partial"}:
                    continue
                series = _minute_series_from_validation_snapshot(snapshot, target_trade_date)
                if not series:
                    continue
                normalized = str(code or "").zfill(6)
                minute_by_code[normalized] = series
                source = f"reused_validation_snapshot:run_{int(snapshot.get('runId') or 0)}:{snapshot.get('source') or ''}"
                minute_source_by_code[normalized] = source
                reusable_rows.append(
                    {
                        "code": normalized,
                        "targetTradeDate": target_trade_date,
                        "period": str(snapshot.get("period") or "1m"),
                        "source": source,
                        "qualityStatus": _validation_minute_quality(series, target_trade_date),
                        "error": "",
                        "series": series,
                    }
                )
            if reusable_rows:
                rows_by_period: Dict[str, List[Dict[str, Any]]] = {}
                for row in reusable_rows:
                    rows_by_period.setdefault(str(row.get("period") or "1m"), []).append(row)
                for row_period, period_rows in rows_by_period.items():
                    self.database.upsert_t1_overnight_validation_minute_snapshots(run_id, target_trade_date, row_period, period_rows)
        minute_source = "t1_validation_minute_snapshot" if minute_by_code else ""
        require_dated_minutes = not _allow_undated_t1_verification_minutes(recommendation_date)
        minute_fetch_codes = [code for code in verification_codes if code not in minute_by_code]
        fetched_period_by_code: Dict[str, str] = {}
        for fetch_period in ("1m", "15m"):
            period_fetch_codes = [code for code in minute_fetch_codes if code not in minute_by_code]
            if not period_fetch_codes:
                break
            try:
                fetched_minute_by_code = self.analysis_service.kline_provider.fetch_many(
                    period_fetch_codes,
                    period=fetch_period,
                    require_dated=require_dated_minutes,
                )
            except TypeError:
                fetched_minute_by_code = self.analysis_service.kline_provider.fetch_many(period_fetch_codes, period=fetch_period)
                if require_dated_minutes:
                    fetched_minute_by_code = _dated_minute_series_by_code(fetched_minute_by_code)
            except Exception as error:  # noqa: BLE001 - minute data is optional for historical verification.
                LOGGER.warning("t1 overnight minute verification data unavailable run=%s period=%s error=%s", run_id, fetch_period, error)
                continue
            if require_dated_minutes:
                fetched_minute_by_code = _dated_minute_series_by_code(fetched_minute_by_code)
            for code, series in fetched_minute_by_code.items():
                normalized = str(code or "").zfill(6)
                target_series = _minute_series_for_trade_date(series or [], target_trade_date) if target_trade_date else list(series or [])
                if not target_series:
                    continue
                minute_by_code[normalized] = target_series
                fetched_period_by_code[normalized] = fetch_period
                source = _minute_provider_source_for_code(self.analysis_service.kline_provider, normalized)
                minute_source_by_code[normalized] = source
            if fetched_minute_by_code and not minute_source:
                minute_source = self.analysis_service.kline_provider.source
        verified_at = datetime.now().isoformat(timespec="seconds")
        if target_trade_date and minute_fetch_codes:
            persist_rows = []
            for code in minute_fetch_codes:
                normalized = str(code or "").zfill(6)
                series = minute_by_code.get(normalized) or []
                if not series or not _minute_series_for_trade_date(series, target_trade_date):
                    continue
                persist_rows.append(
                    {
                        "code": normalized,
                        "targetTradeDate": target_trade_date,
                        "period": fetched_period_by_code.get(normalized) or "1m",
                        "source": minute_source_by_code.get(normalized) or self.analysis_service.kline_provider.source,
                        "qualityStatus": _validation_minute_quality(series, target_trade_date),
                        "error": "",
                        "series": series,
                    }
                )
            if persist_rows:
                self.database.upsert_t1_overnight_validation_minute_snapshots(run_id, target_trade_date, "1m", persist_rows)
        if target_trade_date:
            snapshot_minutes = self.database.load_latest_t1_overnight_minute_snapshots(
                target_trade_date,
                verification_codes,
                period=self.settings.kline_period,
            )
            for code, snapshot in snapshot_minutes.items():
                normalized = str(code or "").zfill(6)
                if minute_by_code.get(normalized):
                    continue
                series = _minute_series_from_t1_snapshot(snapshot, target_trade_date)
                if series:
                    minute_by_code[normalized] = series
                    source = str(snapshot.get("source") or "t1_target_day_frozen_snapshot")
                    minute_source_by_code[normalized] = source
                    if not minute_source:
                        minute_source = source
                    self.database.upsert_t1_overnight_validation_minute_snapshots(
                        run_id,
                        target_trade_date,
                        str(snapshot.get("period") or self.settings.kline_period or "15m"),
                        [
                            {
                                "code": normalized,
                                "targetTradeDate": target_trade_date,
                                "period": str(snapshot.get("period") or self.settings.kline_period or "15m"),
                                "source": f"t1_target_day_frozen_snapshot:{source}",
                                "qualityStatus": _validation_minute_quality(series, target_trade_date),
                                "error": "",
                                "series": series,
                            }
                        ],
                    )
        if minute_source_by_code and hasattr(self.analysis_service.kline_provider, "last_sources_by_code"):
            merged_sources = dict(getattr(self.analysis_service.kline_provider, "last_sources_by_code", {}) or {})
            merged_sources.update(minute_source_by_code)
            self.analysis_service.kline_provider.last_sources_by_code = merged_sources
        fresh_verification_available = any(
            _next_trade_point(series_by_code.get(code, []), recommendation_date)
            or _minute_derived_next_trade_point(minute_by_code.get(code, []), recommendation_date, verified_at)
            for code in codes
        )
        if not fresh_verification_available and existing_outcomes:
            return {
                "runId": run_id,
                "status": _outcome_status(run, existing_outcomes),
                "items": existing_outcomes,
                "scenarios": existing_scenarios,
                "summary": _outcome_summary(existing_outcomes),
                "tradeableSummary": _tradeable_summary(existing_outcomes),
                "strategyFeedback": self.latest_strategy_feedback(),
                "warning": "行情源与本地日线缓存均未提供可验证的次日数据，已保留原复盘结果，未覆盖写库。",
            }
        execution_profile = self.execution_config()
        fresh_verification_by_code = {
            code: bool(
                _next_trade_point(series_by_code.get(code, []), recommendation_date)
                or _minute_derived_next_trade_point(minute_by_code.get(code, []), recommendation_date, verified_at)
            )
            for code in codes
        }
        existing_payload_by_code = {
            str(item.get("code") or "").zfill(6): dict(item.get("outcome") or item)
            for item in existing_outcomes
            if str(item.get("code") or "").strip()
        }
        outcomes = []
        for pick in picks:
            code = str(pick.get("code") or "").zfill(6)
            outcome = _build_outcome(
                run=run,
                pick=pick,
                series=series_by_code.get(code, []),
                minute_series=minute_by_code.get(code, []),
                data_source=daily_source,
                minute_source=minute_source_by_code.get(code) or _minute_source_for_pick(self.analysis_service.kline_provider, pick, minute_source),
                verified_at=verified_at,
                execution_profile=execution_profile,
                recommendation_minute_series=recommendation_minute_by_code.get(code, []),
            )
            if not fresh_verification_by_code.get(code) and existing_payload_by_code.get(code):
                outcome = existing_payload_by_code[code]
            outcomes.append(outcome)
        baseline_comparison = {}
        baseline_outcomes_by_group: Dict[str, List[Dict[str, Any]]] = {}
        for group_name, group_picks in baseline_pick_groups.items():
            group_outcomes = [
                _build_outcome(
                    run=run,
                    pick=pick,
                    series=series_by_code.get(str(pick.get("code") or "").zfill(6), []),
                    minute_series=minute_by_code.get(str(pick.get("code") or "").zfill(6), []),
                    data_source=daily_source,
                    minute_source=minute_source_by_code.get(str(pick.get("code") or "").zfill(6)) or _minute_source_for_pick(self.analysis_service.kline_provider, pick, minute_source),
                    verified_at=verified_at,
                    execution_profile=execution_profile,
                    recommendation_minute_series=recommendation_minute_by_code.get(str(pick.get("code") or "").zfill(6), []),
                )
                for pick in group_picks
            ]
            baseline_outcomes_by_group[group_name] = group_outcomes
            baseline_comparison[group_name] = _baseline_comparison(group_outcomes, slot_count=PORTFOLIO_SLOT_COUNT)
        if "final" not in baseline_comparison:
            baseline_comparison["final"] = _baseline_comparison(outcomes, slot_count=PORTFOLIO_SLOT_COUNT)
        if baseline_outcomes_by_group.get("market"):
            baseline_comparison["market"] = _baseline_comparison(baseline_outcomes_by_group["market"], slot_count=max(1, len(baseline_outcomes_by_group["market"])))
            baseline_comparison["market"]["reason"] = baseline_comparison["market"].get("reason") or "全池等权基线：固定 open_15m 等权平均，样本来自可交易候选池分层抽样。"
            baseline_comparison["random"] = _random_portfolio_baseline_comparison(
                baseline_outcomes_by_group["market"],
                seed=str(run.get("recommendationDate") or run_id),
                slot_count=PORTFOLIO_SLOT_COUNT,
                iterations=RANDOM_BASELINE_ITERATIONS,
            )
        baseline_comparison["index"] = _index_benchmark_comparison(run, series_by_code, minute_by_code, self.analysis_service.kline_provider, minute_source, daily_source, execution_profile, verified_at)
        baseline_comparison["portfolio"] = _portfolio_comparison(outcomes, slot_count=PORTFOLIO_SLOT_COUNT)
        baseline_comparison["criticPair"] = _critic_pair_comparison(
            baseline_comparison.get("aiPreCritic") or {},
            baseline_comparison.get("final") or {},
            run.get("market") or {},
        )
        shadow_outcomes = baseline_outcomes_by_group.get("tailSupportShadow") or []
        deepseek_tail_outcomes = baseline_outcomes_by_group.get("deepSeekTailShadow") or []
        shadow_comparison = _shadow_outcome_comparison(
            final_outcomes=outcomes,
            shadow_outcomes=shadow_outcomes,
            shadow_picks=baseline_pick_groups.get("tailSupportShadow") or [],
            deepseek_tail_outcomes=deepseek_tail_outcomes,
            deepseek_tail_picks=baseline_pick_groups.get("deepSeekTailShadow") or [],
        )
        benchmark_return = _safe_number((baseline_comparison.get("index") or {}).get("avgNetReturnPct"))
        if benchmark_return is not None:
            for outcome in outcomes:
                primary_net = _primary_net_return(outcome)
                outcome["benchmarkReturnPct"] = benchmark_return
                outcome["excessReturnPct"] = round(primary_net - benchmark_return, 2) if primary_net is not None else None
                outcome["benchmark"] = {
                    "source": "index_etf_avg",
                    "scenarioKey": PRIMARY_TRADEABLE_SCENARIO_KEY,
                    "returnPct": benchmark_return,
                    "excessReturnPct": outcome["excessReturnPct"],
                }
        else:
            for outcome in outcomes:
                reasons = list(outcome.get("exclusionReasons") or [])
                if "同口径基准不可用" not in reasons:
                    reasons.append("同口径基准不可用")
                outcome["learningEligible"] = False
                outcome["isTradeableMetric"] = False
                outcome["exclusionReasons"] = reasons
        updated_market = dict(run.get("market") or {})
        updated_market["baselineComparison"] = baseline_comparison
        if shadow_comparison:
            existing_shadow = dict(updated_market.get("shadowComparison") or {})
            existing_shadow["outcomeComparison"] = shadow_comparison
            updated_market["shadowComparison"] = existing_shadow
        critic_meta = dict(updated_market.get("critic") or {})
        if baseline_comparison.get("criticPair"):
            critic_meta["pairedReview"] = baseline_comparison["criticPair"]
            updated_market["critic"] = critic_meta
        self.database.update_t1_overnight_selection_run(run_id, {"market": updated_market})
        self.database.upsert_t1_overnight_selection_outcomes(run_id, outcomes)
        self.database.upsert_t1_overnight_validation_scenarios(run_id, _flatten_validation_scenarios(outcomes))
        outcomes = self.database.load_t1_overnight_selection_outcomes(run_id)
        scenarios = self.database.load_t1_overnight_validation_scenarios(run_id)
        outcomes = _attach_validation_scenarios(outcomes, scenarios)
        feedback = self.refresh_strategy_feedback(source_run_id=run_id, trigger_source=trigger_source)
        return {
            "runId": run_id,
            "status": _outcome_status(run, outcomes),
            "items": outcomes,
            "scenarios": scenarios,
            "summary": _outcome_summary(outcomes),
            "tradeableSummary": _tradeable_summary(outcomes),
            "baselineComparison": baseline_comparison,
            "strategyFeedback": feedback,
        }

    def backfill_validation_minutes(
        self,
        run_id: int,
        scope: str = "final",
        force: bool = False,
        period: str = "1m",
    ) -> Dict[str, Any]:
        run = self.database.load_t1_overnight_selection_run(run_id)
        if not run:
            raise ValueError(f"未找到 T+1 超短线筛选任务 {run_id}")
        if run.get("status") != "done":
            raise ValueError("只有已完成的 T+1 run 可以补拉验证分钟线。")
        if _is_zero_pick_run(run):
            return {
                "runId": run_id,
                "status": "zero_pick",
                "targetTradeDate": "",
                "scope": "final",
                "requested": 0,
                "stored": 0,
                "failed": 0,
                "skipped": 0,
                "items": [],
                "verification": _zero_pick_verification_result(run, self.latest_strategy_feedback()),
                "message": "该 run 是 Top0 空仓决策，没有需要补拉的个股验证分钟线。",
            }
        recommendation_date = str(run.get("recommendationDate") or "")
        if not recommendation_date or recommendation_date >= date.today().isoformat():
            raise ValueError("推荐日之后尚无可验证交易日，暂不能补拉。")
        target_trade_date = _next_weekday_date(recommendation_date)
        if not target_trade_date:
            raise ValueError("无法从推荐日推导目标验证日。")

        final_picks = _verification_picks_for_scope(run, self.database, "final")
        final_codes = _unique_codes([str(item.get("code") or "") for item in final_picks])
        picks = _verification_picks_for_scope(run, self.database, scope)
        codes = _unique_codes([str(item.get("code") or "") for item in picks])
        benchmark_codes = _unique_codes([str(item.get("code") or "") for item in INDEX_BENCHMARKS])
        verification_codes = _unique_codes(codes + benchmark_codes)
        if not codes:
            verification = self.verify_run(run_id, trigger_source="backfill_validation_minutes_empty")
            return {
                "runId": run_id,
                "status": verification.get("status") or "empty",
                "targetTradeDate": target_trade_date,
                "scope": "final",
                "requested": 0,
                "stored": 0,
                "failed": 0,
                "skipped": 0,
                "items": [],
                "verification": verification,
                "message": "该 run 没有最终推荐股，不需要补拉。",
            }

        existing_snapshots = self.database.load_t1_overnight_validation_minute_snapshots(
            run_id,
            verification_codes,
            target_trade_date=target_trade_date,
        )
        reusable_snapshots = self.database.load_latest_t1_overnight_validation_minute_snapshots_any_run(
            target_trade_date,
            verification_codes,
            exclude_run_id=run_id,
        )
        reusable_rows: List[Dict[str, Any]] = []
        for code, snapshot in reusable_snapshots.items():
            normalized = str(code or "").zfill(6)
            if normalized in existing_snapshots and _validation_snapshot_supports_primary(existing_snapshots.get(normalized) or {}, target_trade_date):
                continue
            series = _minute_series_from_validation_snapshot(snapshot, target_trade_date)
            if not series:
                continue
            reusable_rows.append(
                {
                    "code": normalized,
                    "targetTradeDate": target_trade_date,
                    "period": str(snapshot.get("period") or "1m"),
                    "source": f"reused_validation_snapshot:run_{int(snapshot.get('runId') or 0)}:{snapshot.get('source') or ''}",
                    "qualityStatus": _validation_minute_quality(series, target_trade_date),
                    "error": "",
                    "series": series,
                }
            )
        if reusable_rows:
            reusable_by_period: Dict[str, List[Dict[str, Any]]] = {}
            for row in reusable_rows:
                reusable_by_period.setdefault(str(row.get("period") or "1m"), []).append(row)
            for row_period, period_rows in reusable_by_period.items():
                self.database.upsert_t1_overnight_validation_minute_snapshots(run_id, target_trade_date, row_period, period_rows)
            existing_snapshots = self.database.load_t1_overnight_validation_minute_snapshots(
                run_id,
                verification_codes,
                target_trade_date=target_trade_date,
            )
        outcomes = self.database.load_t1_overnight_selection_outcomes(run_id)
        scenarios = self.database.load_t1_overnight_validation_scenarios(run_id)
        outcomes = _attach_validation_scenarios(outcomes, scenarios)
        need_codes, skipped_codes = _validation_backfill_target_codes(
            codes=verification_codes,
            snapshots=existing_snapshots,
            outcomes=outcomes,
            target_trade_date=target_trade_date,
            force=force,
        )
        rows: List[Dict[str, Any]] = []
        failed_items: List[Dict[str, Any]] = []
        source_by_code: Dict[str, str] = {}
        normalized_period = _normalize_validation_minute_period(period)
        if need_codes:
            fetch_periods = _unique_strings([normalized_period, "15m" if normalized_period != "15m" else "1m"])
            fetch_errors: Dict[str, List[str]] = {code: [] for code in need_codes}
            remaining_fetch_codes = list(need_codes)
            for fetch_period in fetch_periods:
                if not remaining_fetch_codes:
                    break
                try:
                    fetched = self.analysis_service.kline_provider.fetch_many(remaining_fetch_codes, period=fetch_period, require_dated=True)
                except TypeError:
                    fetched = self.analysis_service.kline_provider.fetch_many(remaining_fetch_codes, period=fetch_period)
                    fetched = _dated_minute_series_by_code(fetched)
                except Exception as error:  # noqa: BLE001 - keep alternate periods and fallback moving.
                    LOGGER.warning("t1 validation minute backfill failed run=%s period=%s error=%s", run_id, fetch_period, error)
                    for code in remaining_fetch_codes:
                        fetch_errors.setdefault(code, []).append(f"{fetch_period}: {error}")
                    continue
                fetched = _dated_minute_series_by_code(fetched)
                stored_this_period: set[str] = set()
                for code in remaining_fetch_codes:
                    normalized = str(code or "").zfill(6)
                    series = _minute_series_for_trade_date(fetched.get(normalized) or [], target_trade_date)
                    if not series:
                        fetch_errors.setdefault(normalized, []).append(f"{fetch_period}: 行情源未返回 {target_trade_date} 的分钟线")
                        continue
                    source = _minute_provider_source_for_code(self.analysis_service.kline_provider, normalized)
                    source_by_code[normalized] = source
                    rows.append(
                        {
                            "code": normalized,
                            "targetTradeDate": target_trade_date,
                            "period": fetch_period,
                            "source": source,
                            "qualityStatus": _validation_minute_quality(series, target_trade_date),
                            "error": "",
                            "series": series,
                        }
                    )
                    stored_this_period.add(normalized)
                remaining_fetch_codes = [code for code in remaining_fetch_codes if str(code or "").zfill(6) not in stored_this_period]

            stored_codes_after_fetch = {str(row.get("code") or "").zfill(6) for row in rows}
            missing_after_fetch = [code for code in need_codes if code not in stored_codes_after_fetch]
            if missing_after_fetch:
                frozen_rows = self.database.load_latest_t1_overnight_minute_snapshots(
                    target_trade_date,
                    missing_after_fetch,
                    period=self.settings.kline_period,
                )
                for code, snapshot in frozen_rows.items():
                    normalized = str(code or "").zfill(6)
                    if normalized in stored_codes_after_fetch:
                        continue
                    series = _minute_series_from_t1_snapshot(snapshot, target_trade_date)
                    if not series:
                        continue
                    source = str(snapshot.get("source") or "t1_target_day_frozen_snapshot")
                    source_by_code[normalized] = source
                    rows.append(
                        {
                            "code": normalized,
                            "targetTradeDate": target_trade_date,
                            "period": str(snapshot.get("period") or self.settings.kline_period or "15m"),
                            "source": f"t1_target_day_frozen_snapshot:{source}",
                            "qualityStatus": _validation_minute_quality(series, target_trade_date),
                            "error": "",
                            "series": series,
                        }
                    )
                stored_codes_after_frozen = {str(row.get("code") or "").zfill(6) for row in rows}
                failed_items = [item for item in failed_items if item.get("code") not in stored_codes_after_frozen]
                for code in missing_after_fetch:
                    normalized = str(code or "").zfill(6)
                    if normalized not in stored_codes_after_frozen and not any(item.get("code") == normalized for item in failed_items):
                        errors = fetch_errors.get(normalized) or []
                        detail = "；".join(errors[-2:]) if errors else "行情源未返回目标验证日分钟线"
                        failed_items.append({"code": normalized, "status": "failed", "error": f"{detail}；未找到目标验证日冻结快照或其他 run 可复用快照"})

        stored = 0
        rows_by_period: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            rows_by_period.setdefault(str(row.get("period") or normalized_period), []).append(row)
        for row_period, period_rows in rows_by_period.items():
            stored += self.database.upsert_t1_overnight_validation_minute_snapshots(run_id, target_trade_date, row_period, period_rows)
        verification = self.verify_run(run_id, trigger_source="backfill_validation_minutes")
        stored_codes = {str(row.get("code") or "").zfill(6) for row in rows}
        code_role_by_code = {
            code: ("pick" if code in final_codes else "shadow")
            for code in codes
        }
        code_role_by_code.update({code: "benchmark" for code in benchmark_codes})
        items = [
            {
                "code": code,
                "role": code_role_by_code.get(code) or "pick",
                "roleLabel": _validation_backfill_role_label(code_role_by_code.get(code) or "pick"),
                "status": "stored" if code in stored_codes else "skipped",
                "source": source_by_code.get(code) or (existing_snapshots.get(code) or {}).get("source") or "",
                "reason": "已写入目标验证日分钟线" if code in stored_codes else "已有可用 open_15m 验证证据",
            }
            for code in verification_codes
            if code in stored_codes or code in skipped_codes
        ]
        items.extend(
            {
                **item,
                "role": code_role_by_code.get(str(item.get("code") or "").zfill(6)) or "pick",
                "roleLabel": _validation_backfill_role_label(code_role_by_code.get(str(item.get("code") or "").zfill(6)) or "pick"),
            }
            for item in failed_items
        )
        role_summary = _validation_backfill_role_summary(items)
        failed_message_items = [item for item in items if str(item.get("status") or "") == "failed"]
        message = _validation_backfill_message(
            run=run,
            target_trade_date=target_trade_date,
            requested=len(need_codes),
            stored=stored,
            failed_items=failed_message_items,
            skipped_count=len(skipped_codes),
            role_summary=role_summary,
        )
        return {
            "runId": run_id,
            "status": verification.get("status") or "done",
            "targetTradeDate": target_trade_date,
            "scope": "final",
            "requested": len(need_codes),
            "stored": stored,
            "failed": len(failed_items),
            "skipped": len(skipped_codes),
            "items": items,
            "roleSummary": role_summary,
            "verification": verification,
            "message": message.get("message"),
            "reason": message.get("reason"),
            "recoverable": message.get("recoverable"),
        }

    def verify_pending_runs(self, limit: int = 20) -> Dict[str, Any]:
        runs = self.database.find_t1_overnight_runs_without_outcomes(limit=limit)
        results = []
        for run in runs:
            try:
                result = self.verify_run(int(run["id"]), trigger_source="auto_verify")
            except Exception as error:  # noqa: BLE001 - keep batch verification moving.
                LOGGER.warning("t1 overnight auto verify failed run=%s error=%s", run.get("id"), error)
                results.append({"runId": run.get("id"), "status": "failed", "error": str(error)})
            else:
                results.append({"runId": run.get("id"), "status": result.get("status"), "summary": result.get("summary")})
        skipped_zero_pick = self.database.count_t1_overnight_zero_pick_runs_without_outcomes()
        return {
            "count": len(results),
            "skippedZeroPick": skipped_zero_pick,
            "results": results,
            "strategyFeedback": self.latest_strategy_feedback(),
        }

    def history_dry_run(self) -> Dict[str, Any]:
        return self._history_soft_cleanup_plan(include_already_cleaned=False)

    def history_soft_cleanup(self) -> Dict[str, Any]:
        plan = self._history_soft_cleanup_plan(include_already_cleaned=False)
        run_ids = [int(item.get("id") or 0) for item in plan.get("runPlans", []) if int(item.get("id") or 0) > 0]
        applied_at = datetime.now().isoformat(timespec="seconds")
        cutoff_date = date.today().isoformat()
        if not run_ids:
            plan.update(
                {
                    "dryRun": False,
                    "applied": True,
                    "appliedAt": applied_at,
                    "message": "没有需要软清理的旧 T+1 历史数据。",
                    "feedback": self.latest_strategy_feedback(),
                    "criticStatus": self.critic_status(),
                }
            )
            return plan

        with self.database.connect() as connection:
            for run_id in run_ids:
                row = connection.execute(
                    """
                    SELECT id, recommendation_date, selection_mode, final_count, request_json, market_json, final_picks_json
                    FROM t1_overnight_selection_runs
                    WHERE id = ?
                    """,
                    (run_id,),
                ).fetchone()
                if not row:
                    continue
                request = _json_dict(row["request_json"])
                market = _json_dict(row["market_json"])
                run_snapshot = {
                    "id": int(row["id"]),
                    "recommendationDate": row["recommendation_date"],
                    "selectionMode": row["selection_mode"],
                    "finalCount": int(row["final_count"] or 0),
                    "request": request,
                    "market": market,
                    "finalPicks": _json_list(row["final_picks_json"]),
                }
                semantic_issue = _semantic_polluted_run(run_snapshot)
                status = (
                    "zero_pick_cash_day"
                    if int(row["final_count"] or 0) <= 0 and str(row["selection_mode"] or "") in ZERO_PICK_SELECTION_MODES
                    else "semantic_legacy_display_only"
                    if semantic_issue
                    else "legacy_display_only"
                )
                treatment_reasons = [
                    "旧验证口径历史 run，仅保留展示和问题排查。",
                    "不进入严格复盘、DeepSeek 学习或 Critic 样本。",
                ]
                if semantic_issue:
                    treatment_reasons.append("该 run 存在上市板块代理被当作行业/概念证据的语义污染风险。")
                treatment = _historical_treatment(
                    status=status,
                    applied_at=applied_at,
                    cutoff_date=cutoff_date,
                    reasons=treatment_reasons,
                    extra={
                        "originalRunRole": str((market.get("runRole") or request.get("runRole") or "")),
                        "originalSelectionMode": str(row["selection_mode"] or ""),
                        "semanticEvidencePolluted": semantic_issue,
                    },
                )
                request["runRole"] = "legacy"
                market["runRole"] = "legacy"
                request["historicalTreatment"] = treatment
                market["historicalTreatment"] = treatment
                connection.execute(
                    """
                    UPDATE t1_overnight_selection_runs
                    SET request_json = ?,
                        market_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json.dumps(request, ensure_ascii=False), json.dumps(market, ensure_ascii=False), applied_at, run_id),
                )

                outcome_rows = connection.execute(
                    """
                    SELECT *
                    FROM t1_overnight_selection_outcomes
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchall()
                for outcome_row in outcome_rows:
                    payload = _json_dict(outcome_row["outcome_json"])
                    original_label = str(outcome_row["outcome_label"] or payload.get("outcomeLabel") or "")
                    original_failure = str(outcome_row["failure_reason"] or payload.get("failureReason") or "")
                    item = self.database._t1_overnight_outcome_from_row(outcome_row)
                    suspected_price = _suspected_historical_price_mismatch(item)
                    reasons = _learning_exclusion_reasons_for_loaded_outcome(item)
                    if suspected_price:
                        status = "invalid_quarantined"
                        label = "价格异常"
                        failure = "历史样本疑似价格口径异常，已隔离为仅展示。"
                        reasons = ["疑似价格口径异常"] + [reason for reason in reasons if reason != "疑似价格口径异常"]
                    else:
                        status = "legacy_display_only"
                        label = "历史参考"
                        failure = "旧验证口径历史样本，仅展示，不进入严格复盘或学习。"
                    treatment = _historical_treatment(
                        status=status,
                        applied_at=applied_at,
                        cutoff_date=cutoff_date,
                        reasons=reasons,
                        extra={
                            "originalOutcomeLabel": original_label,
                            "originalFailureReason": original_failure,
                            "originalValidationStatus": str(payload.get("validationStatus") or ""),
                        },
                    )
                    payload["historicalTreatment"] = treatment
                    payload["displayOnly"] = True
                    payload["learningEligible"] = False
                    payload["isTradeableMetric"] = False
                    payload["validationStatus"] = "invalid" if suspected_price else "legacy"
                    payload["outcomeLabel"] = label
                    payload["failureReason"] = failure
                    payload["exclusionReasons"] = _unique_strings(["历史旧口径，仅展示不学习"] + reasons)
                    connection.execute(
                        """
                        UPDATE t1_overnight_selection_outcomes
                        SET outcome_label = ?,
                            outcome_score = 0,
                            hit_sell_plan = 0,
                            hit_invalid_condition = ?,
                            failure_reason = ?,
                            outcome_json = ?
                        WHERE run_id = ? AND code = ?
                        """,
                        (
                            label,
                            1 if suspected_price else 0,
                            failure,
                            json.dumps(payload, ensure_ascii=False),
                            run_id,
                            str(outcome_row["code"] or "").zfill(6),
                        ),
                    )

                scenario_rows = connection.execute(
                    """
                    SELECT *
                    FROM t1_overnight_validation_scenarios
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchall()
                for scenario_row in scenario_rows:
                    payload = _json_dict(scenario_row["scenario_json"])
                    treatment = _historical_treatment(
                        status="legacy_display_only",
                        applied_at=applied_at,
                        cutoff_date=cutoff_date,
                        reasons=["旧验证口径场景，仅保留历史展示。"],
                        extra={
                            "originalDataStatus": str(scenario_row["data_status"] or payload.get("dataStatus") or ""),
                            "originalValidationStatus": str(payload.get("validationStatus") or ""),
                        },
                    )
                    payload["historicalTreatment"] = treatment
                    payload["displayOnly"] = True
                    payload["validationStatus"] = "legacy"
                    connection.execute(
                        """
                        UPDATE t1_overnight_validation_scenarios
                        SET scenario_json = ?,
                            updated_at = ?
                        WHERE run_id = ?
                          AND code = ?
                          AND validation_version = ?
                          AND scenario_key = ?
                        """,
                        (
                            json.dumps(payload, ensure_ascii=False),
                            applied_at,
                            run_id,
                            str(scenario_row["code"] or "").zfill(6),
                            str(scenario_row["validation_version"] or ""),
                            str(scenario_row["scenario_key"] or ""),
                        ),
                    )

            note = f"历史软清理 {applied_at}：旧口径建议不再影响 DeepSeek。"
            connection.execute(
                """
                UPDATE t1_overnight_strategy_suggestions
                SET status = CASE WHEN status = 'active' THEN 'revoked' ELSE 'rejected' END,
                    reviewer_note = CASE WHEN reviewer_note = '' THEN ? ELSE reviewer_note || '；' || ? END,
                    updated_at = ?
                WHERE status IN ('active', 'draft', 'observing')
                """,
                (note, note, applied_at),
            )
            feedback_rows = connection.execute(
                """
                SELECT id, feedback_json
                FROM t1_overnight_strategy_feedback
                WHERE verified_outcomes > 0
                """
            ).fetchall()
            for row in feedback_rows:
                payload = _json_dict(row["feedback_json"])
                payload["historicalTreatment"] = _historical_treatment(
                    status="legacy_feedback_display_only",
                    applied_at=applied_at,
                    cutoff_date=cutoff_date,
                    reasons=["旧口径策略复盘记录，仅保留审计，不作为后续 promptSummary。"],
                )
                connection.execute(
                    """
                    UPDATE t1_overnight_strategy_feedback
                    SET feedback_json = ?
                    WHERE id = ?
                    """,
                    (json.dumps(payload, ensure_ascii=False), int(row["id"])),
                )

        self.database.set_app_setting(
            HISTORY_SOFT_CLEANUP_SETTING_KEY,
            {
                "version": HISTORY_SOFT_CLEANUP_VERSION,
                "appliedAt": applied_at,
                "cutoffDate": cutoff_date,
                "runIds": run_ids,
                "planActions": plan.get("actions") or {},
            },
        )
        feedback = self.refresh_strategy_feedback(source_run_id=0, trigger_source="history_soft_cleanup")
        result = self._history_soft_cleanup_plan(include_already_cleaned=False)
        result.update(
            {
                "dryRun": False,
                "applied": True,
                "appliedAt": applied_at,
                "message": "历史软清理已完成：旧数据保留展示，但不再进入严格复盘、DeepSeek 学习或 Critic 样本。",
                "appliedRuns": len(run_ids),
                "feedback": feedback,
                "criticStatus": self.critic_status(),
            }
        )
        return result

    def _history_soft_cleanup_plan(self, include_already_cleaned: bool = False) -> Dict[str, Any]:
        with self.database.connect() as connection:
            run_rows = connection.execute(
                """
                SELECT id, recommendation_date, scan_time, selection_mode, final_count, actual_source, request_json, market_json, final_picks_json
                FROM t1_overnight_selection_runs
                WHERE status = 'done'
                  AND recommendation_date <= ?
                ORDER BY recommendation_date DESC, id DESC
                """,
                (date.today().isoformat(),),
            ).fetchall()
        runs = [
            {
                "id": int(row["id"]),
                "recommendationDate": row["recommendation_date"],
                "scanTime": row["scan_time"],
                "selectionMode": row["selection_mode"],
                "finalCount": int(row["final_count"] or 0),
                "actualSource": row["actual_source"],
                "request": _json_dict(row["request_json"]),
                "market": _json_dict(row["market_json"]),
                "finalPicks": _json_list(row["final_picks_json"]),
            }
            for row in run_rows
        ]
        today_text = date.today().isoformat()
        eligible_runs = [
            run
            for run in runs
            if str(run.get("recommendationDate") or "") < today_text or _semantic_polluted_run(run)
        ]
        already_cleaned = [run for run in eligible_runs if _historical_treatment_applied(run.get("market") or {}, run.get("request") or {})]
        pending_runs = eligible_runs if include_already_cleaned else [run for run in eligible_runs if run not in already_cleaned]
        outcomes: List[Dict[str, Any]] = []
        for run in pending_runs:
            run_outcomes = self.database.load_t1_overnight_selection_outcomes(int(run["id"]))
            if not run_outcomes:
                continue
            scenarios = self.database.load_t1_overnight_validation_scenarios(int(run["id"]))
            outcomes.extend(_attach_validation_scenarios(run_outcomes, scenarios))

        action_counts: Dict[str, int] = {
            "keepDisplayOnly": 0,
            "markLegacy": 0,
            "markInvalidOrQuarantine": 0,
            "primaryUnavailable": 0,
            "learningEligible": 0,
            "excludeFromLearning": 0,
            "semanticEvidencePolluted": 0,
        }
        samples = []
        for item in outcomes:
            payload = _outcome_payload(item)
            primary = ((payload.get("tradeable") or {}).get("primaryScenario") or {})
            reasons = _learning_exclusion_reasons_for_loaded_outcome(item)
            suspected_price = _suspected_historical_price_mismatch(item)
            if primary.get("scenarioKey") != PRIMARY_TRADEABLE_SCENARIO_KEY or primary.get("dataStatus") != "ok" or primary.get("netReturnPct") is None:
                action_counts["primaryUnavailable"] += 1
            if payload.get("validationVersion") != TRADEABLE_VALIDATION_VERSION:
                action_counts["markLegacy"] += 1
            if suspected_price:
                action_counts["markInvalidOrQuarantine"] += 1
            if _is_learning_eligible_outcome(item) and not suspected_price:
                action_counts["learningEligible"] += 1
            else:
                action_counts["excludeFromLearning"] += 1
            action_counts["keepDisplayOnly"] += 1
            if suspected_price or item.get("code") == "603629" or len(samples) < 8:
                samples.append(
                    {
                        "runId": item.get("runId"),
                        "date": item.get("recommendationDate"),
                        "code": item.get("code"),
                        "name": item.get("name"),
                        "currentLabel": item.get("outcomeLabel"),
                        "primaryScenario": primary.get("scenarioKey") or "",
                        "primaryStatus": primary.get("dataStatus") or "",
                        "primaryNetReturnPct": primary.get("netReturnPct"),
                        "openReturnPct": item.get("openReturnPct"),
                        "closeReturnPct": item.get("closeReturnPct"),
                        "suspectedPriceMismatch": suspected_price,
                        "wouldEnterLearning": _is_learning_eligible_outcome(item) and not suspected_price,
                        "exclusionReasons": ["疑似价格口径异常"] if suspected_price else reasons,
                    }
                )
        for run in pending_runs:
            if _semantic_polluted_run(run):
                action_counts["semanticEvidencePolluted"] += 1
        zero_pick_runs = [run for run in runs if int(run.get("finalCount") or 0) <= 0 and run.get("selectionMode") in ZERO_PICK_SELECTION_MODES]
        pending_zero_pick_runs = [run for run in pending_runs if int(run.get("finalCount") or 0) <= 0 and run.get("selectionMode") in ZERO_PICK_SELECTION_MODES]
        return {
            "dryRun": True,
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "message": "这是历史数据处理预演，没有写入数据库。",
            "runs": len(pending_runs),
            "allHistoricalRuns": len(eligible_runs),
            "alreadySoftCleanedRuns": len(already_cleaned),
            "zeroPickRuns": len(pending_zero_pick_runs),
            "allZeroPickRuns": len(zero_pick_runs),
            "outcomes": len(outcomes),
            "actions": action_counts,
            "primaryScenarioKey": PRIMARY_TRADEABLE_SCENARIO_KEY,
            "primaryFallbackAllowed": False,
            "priceMismatchThresholdPct": PRICE_MISMATCH_INVALID_PCT,
            "samples": samples[:20],
            "runPlans": [
                {
                    "id": run.get("id"),
                    "recommendationDate": run.get("recommendationDate"),
                    "selectionMode": run.get("selectionMode"),
                    "finalCount": run.get("finalCount"),
                    "semanticEvidencePolluted": _semantic_polluted_run(run),
                    "treatment": "zero_pick_cash_day"
                    if int(run.get("finalCount") or 0) <= 0 and run.get("selectionMode") in ZERO_PICK_SELECTION_MODES
                    else "semantic_legacy_display_only"
                    if _semantic_polluted_run(run)
                    else "legacy_display_only",
                }
                for run in pending_runs
            ],
        }

    def critic_config(self) -> Dict[str, Any]:
        return _normalize_critic_config(self.database.get_app_setting(CRITIC_CONFIG_KEY, DEFAULT_CRITIC_CONFIG))

    def execution_config(self) -> Dict[str, Any]:
        return _normalize_execution_config(self.database.get_app_setting(EXECUTION_CONFIG_KEY, DEFAULT_EXECUTION_PROFILE))

    def stage_config(self) -> Dict[str, Any]:
        return _normalize_stage_config(self.database.get_app_setting(STAGE_CONFIG_KEY, DEFAULT_STAGE_CONFIG))

    def stage_status(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = _normalize_stage_config(config or self.stage_config())
        official_runs = self.database.count_t1_overnight_official_trade_dates()
        tradeable_outcomes = self.database.count_t1_overnight_tradeable_outcomes()
        return _t1_stage_controls_snapshot(config, official_runs=official_runs, tradeable_outcomes=tradeable_outcomes)

    def update_execution_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        config = _normalize_execution_config({**self.execution_config(), **(payload or {})})
        self.database.set_app_setting(EXECUTION_CONFIG_KEY, config)
        return {"config": config}

    def update_stage_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        config = _normalize_stage_config({**self.stage_config(), **(payload or {})})
        self.database.set_app_setting(STAGE_CONFIG_KEY, config)
        return {"config": config, "status": self.stage_status(config)}

    def update_critic_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        config = _normalize_critic_config({**self.critic_config(), **(payload or {})})
        self.database.set_app_setting(CRITIC_CONFIG_KEY, config)
        return {"config": config, "status": self.critic_status(config)}

    def critic_status(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = _normalize_critic_config(config or self.critic_config())
        completed_runs = self.database.count_t1_overnight_completed_runs()
        verified_outcomes = self.database.count_t1_overnight_verified_outcomes()
        tradeable_outcomes = self.database.count_t1_overnight_tradeable_outcomes()
        feedback = self.database.latest_t1_overnight_strategy_feedback()
        enabled, reason = _critic_enabled_reason(config, completed_runs, verified_outcomes, tradeable_outcomes)
        return {
            "mode": config["mode"],
            "enabled": enabled,
            "reason": reason,
            "completedRuns": completed_runs,
            "minCompletedRuns": config["minCompletedRuns"],
            "verifiedOutcomes": verified_outcomes,
            "minVerifiedOutcomes": config["minVerifiedOutcomes"],
            "tradeableOutcomes": tradeable_outcomes,
            "maxReplacements": config["maxReplacements"],
            "historyWindowRuns": config["historyWindowRuns"],
            "requireOutcomes": config["requireOutcomes"],
            "allowReducePickCount": config["allowReducePickCount"],
            "allowZeroPick": config["allowZeroPick"],
            "requireMarketState": config["requireMarketState"],
            "minTradeableValidationSamples": config["minTradeableValidationSamples"],
            "hasFeedback": bool(feedback and feedback.get("promptSummary")),
        }

    def latest_strategy_feedback(self) -> Dict[str, Any]:
        feedback = self.database.latest_t1_overnight_strategy_feedback() or _empty_strategy_feedback()
        if _strategy_feedback_display_only(feedback):
            feedback = _empty_strategy_feedback()
            feedback["historicalFeedbackSuppressed"] = True
        return self._attach_strategy_suggestions(feedback)

    def refresh_strategy_feedback(self, source_run_id: int = 0, trigger_source: str = "manual_refresh") -> Dict[str, Any]:
        config = self.critic_config()
        window_runs = int(config.get("historyWindowRuns") or 20)
        previous = self.database.latest_t1_overnight_strategy_feedback()
        recent_outcomes = [
            outcome
            for outcome in self.database.load_recent_t1_overnight_outcomes(window_runs)
            if outcome.get("outcomeLabel") != "数据不足"
        ]
        outcomes = [outcome for outcome in recent_outcomes if _is_learning_eligible_outcome(outcome)]
        feedback = _build_strategy_feedback(
            outcomes,
            window_runs=window_runs,
            source_run_id=source_run_id,
            trigger_source=trigger_source,
        )
        feedback["sampleAudit"] = _strategy_sample_audit(recent_outcomes, outcomes)
        feedback["portfolio"] = self._portfolio_feedback(window_runs)
        feedback["promptSummary"] = _feedback_prompt_summary(feedback)
        reused = bool(
            trigger_source == "manual_refresh"
            and previous
            and int(previous.get("sourceRunId") or 0) == int(source_run_id or 0)
            and _same_strategy_feedback(previous, feedback)
        )
        if reused:
            feedback_id = int(previous.get("id") or 0)
            feedback["id"] = feedback_id
            feedback["createdAt"] = previous.get("createdAt", "")
        else:
            feedback_id = self.database.insert_t1_overnight_strategy_feedback(feedback)
            feedback["id"] = feedback_id
        suggestions = _build_strategy_suggestions(feedback)
        suggestion_count = self.database.upsert_t1_overnight_strategy_suggestions(feedback_id, suggestions)
        feedback = self._attach_strategy_suggestions(feedback)
        feedback["refreshMeta"] = {
            "reusedExistingFeedback": reused,
            "hasNewSamples": not previous or int(previous.get("verifiedOutcomes") or 0) != int(feedback.get("verifiedOutcomes") or 0),
            "metricsChanged": not previous or not _same_strategy_feedback(previous, feedback),
            "suggestionsGenerated": suggestion_count,
        }
        return feedback

    def review_strategy_suggestion(self, suggestion_id: int, status: str, reviewer_note: str = "") -> Dict[str, Any]:
        suggestion = self.database.update_t1_overnight_strategy_suggestion_status(suggestion_id, status, reviewer_note)
        if not suggestion:
            raise ValueError(f"未找到策略建议 {suggestion_id}")
        return {"suggestion": suggestion, "feedback": self.latest_strategy_feedback()}

    def update_run_role(self, run_id: int, role: str, reviewer_note: str = "") -> Dict[str, Any]:
        normalized = str(role or "").strip()
        if normalized not in {"official", "research"}:
            raise ValueError("runRole 只支持 official 或 research")
        run = self.database.load_t1_overnight_selection_run(run_id)
        if not run:
            raise ValueError(f"未找到 T+1 run {run_id}")
        market = dict(run.get("market") or {})
        request = dict(run.get("request") or {})
        if _historical_treatment_applied(market, request):
            raise ValueError("旧口径历史 run 已软清理为仅展示，不能重新设为 official。")
        if normalized == "official":
            requested_pool_size = _run_requested_pool_size(run)
            if requested_pool_size < T1_OFFICIAL_MIN_POOL_SIZE:
                raise ValueError(f"候选池 {requested_pool_size} 小于正式样本最低 {T1_OFFICIAL_MIN_POOL_SIZE}，只能作为研究 run。")
            signal_guard = _run_signal_window_guard(run)
            if signal_guard.get("officialEligible") is False:
                raise ValueError(str(signal_guard.get("reason") or "信号发布时间超过尾盘可执行窗口，不能设为正式 run。"))
            if _requires_legacy_isolation(run):
                raise ValueError("该 run 缺少新版 T+1 独立证据或存在板块语义污染风险，只能保留为历史/研究样本，不能设为 official。")
        updated_at = datetime.now().isoformat(timespec="seconds")
        if normalized == "official":
            with self.database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT id, request_json, market_json
                    FROM t1_overnight_selection_runs
                    WHERE recommendation_date = ?
                      AND id != ?
                    """,
                    (str(run.get("recommendationDate") or ""), int(run_id)),
                ).fetchall()
                for row in rows:
                    other_request = _json_dict(row["request_json"])
                    other_market = _json_dict(row["market_json"])
                    if _historical_treatment_applied(other_market, other_request):
                        continue
                    other_request["runRole"] = "research"
                    other_market["runRole"] = "research"
                    other_market["runRoleUpdatedAt"] = updated_at
                    other_market["runRoleReviewerNote"] = "同日已有新的 official run，自动改为 research。"
                    connection.execute(
                        """
                        UPDATE t1_overnight_selection_runs
                        SET request_json = ?,
                            market_json = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (json.dumps(other_request, ensure_ascii=False), json.dumps(other_market, ensure_ascii=False), updated_at, int(row["id"])),
                    )
        request["runRole"] = normalized
        market["runRole"] = normalized
        market["runRoleUpdatedAt"] = updated_at
        market["runRoleReviewerNote"] = str(reviewer_note or ("用户手动设为正式 run。" if normalized == "official" else "用户手动改为研究 run。"))
        self.database.update_t1_overnight_selection_run(run_id, {"request": request, "market": market})
        feedback = self.refresh_strategy_feedback(source_run_id=run_id, trigger_source="manual_run_role")
        return {"run": self.get_run(run_id), "feedback": feedback, "criticStatus": self.critic_status()}

    def _portfolio_feedback(self, window_runs: int) -> Dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM t1_overnight_selection_runs
                WHERE status = 'done'
                ORDER BY recommendation_date DESC, id DESC
                LIMIT 1000
                """
            ).fetchall()
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            run = self.database._t1_overnight_run_from_row(row)
            if _historical_treatment_applied(run.get("market") or {}, run.get("request") or {}):
                continue
            if _run_role(run) != "official":
                continue
            grouped.setdefault(str(run.get("recommendationDate") or ""), []).append(run)
        selected_runs: List[Dict[str, Any]] = []
        for trade_date in sorted(grouped.keys(), reverse=True):
            if len(selected_runs) >= max(1, int(window_runs or 20)):
                break
            selected_runs.append(sorted(grouped[trade_date], key=lambda item: int(item.get("id") or 0))[0])

        days = []
        for run in selected_runs:
            if _is_zero_pick_run(run):
                days.append(
                    {
                        "runId": run.get("id"),
                        "recommendationDate": run.get("recommendationDate"),
                        "status": "zero_pick",
                        "portfolioReturnPct": 0.0,
                        "benchmarkReturnPct": None,
                        "excessReturnPct": None,
                        "cashSlots": PORTFOLIO_SLOT_COUNT,
                        "reason": _zero_pick_reason(run),
                    }
                )
                continue
            outcomes = self.database.load_t1_overnight_selection_outcomes(int(run.get("id") or 0))
            scenarios = self.database.load_t1_overnight_validation_scenarios(int(run.get("id") or 0))
            outcomes = _attach_validation_scenarios(outcomes, scenarios)
            usable = [
                item
                for item in outcomes
                if not _t1_outcome_display_only(item)
                and (_outcome_payload(item).get("validationStatus") == "final")
            ]
            portfolio = _portfolio_comparison(usable, slot_count=PORTFOLIO_SLOT_COUNT)
            portfolio_return = _safe_number(portfolio.get("portfolioReturnPct"))
            benchmark_return = _safe_number(((run.get("market") or {}).get("baselineComparison") or {}).get("index", {}).get("avgNetReturnPct"))
            if benchmark_return is None:
                benchmark_return = _avg([item.get("benchmarkReturnPct") for item in usable])
            days.append(
                {
                    "runId": run.get("id"),
                    "recommendationDate": run.get("recommendationDate"),
                    "status": "ok" if portfolio_return is not None else "unavailable",
                    "portfolioReturnPct": portfolio_return,
                    "benchmarkReturnPct": benchmark_return,
                    "excessReturnPct": round(portfolio_return - benchmark_return, 2) if portfolio_return is not None and benchmark_return is not None else None,
                    "cashSlots": portfolio.get("cashSlots"),
                    "tradeableCount": portfolio.get("tradeableCount"),
                }
            )
        valid_returns = [day.get("portfolioReturnPct") for day in days if day.get("portfolioReturnPct") is not None]
        benchmark_returns = [day.get("benchmarkReturnPct") for day in days if day.get("benchmarkReturnPct") is not None]
        excess_returns = [day.get("excessReturnPct") for day in days if day.get("excessReturnPct") is not None]
        return {
            "windowRuns": window_runs,
            "officialDays": len(days),
            "verifiedDays": len(valid_returns),
            "zeroPickDays": len([day for day in days if day.get("status") == "zero_pick"]),
            "avgPortfolioReturnPct": _avg(valid_returns),
            "avgBenchmarkReturnPct": _avg(benchmark_returns),
            "avgExcessReturnPct": _avg(excess_returns),
            "cumulativePortfolioReturnPct": _compound_return_pct(valid_returns),
            "days": days[: max(1, int(window_runs or 20))],
            "slotCount": PORTFOLIO_SLOT_COUNT,
            "basis": "official_trade_dates",
        }

    def _attach_strategy_suggestions(self, feedback: Dict[str, Any]) -> Dict[str, Any]:
        all_suggestions = self.database.list_t1_overnight_strategy_suggestions(limit=120)
        feedback_id = int((feedback or {}).get("id") or 0)
        current = [item for item in all_suggestions if int(item.get("feedbackId") or 0) == feedback_id]
        active = [item for item in all_suggestions if item.get("status") == "active"]
        pending = [
            item
            for item in all_suggestions
            if item.get("status") in {"draft", "observing"}
            and (not feedback_id or int(item.get("feedbackId") or 0) == feedback_id)
        ]
        suggestions = _unique_suggestions(current + pending + active)
        archived = [
            item
            for item in all_suggestions
            if item.get("status") in {"rejected", "revoked"}
            and int(item.get("feedbackId") or 0) != feedback_id
        ][:20]
        feedback = dict(feedback or {})
        if _contains_unconfirmed_strategy_instruction(str(feedback.get("promptSummary") or "")):
            feedback["promptSummary"] = _feedback_prompt_summary(feedback)
        feedback["suggestions"] = suggestions
        feedback["pendingSuggestions"] = pending
        feedback["activeConstraints"] = _confirmed_strategy_constraints(active)
        feedback["archivedSuggestions"] = archived
        return feedback

    def _build_pool(
        self,
        risk_mode: str,
        source: str,
        scan_time: str,
        pool_size: int,
        force: bool = False,
        stage_callback: Optional[Any] = None,
        recommendation_date: str = "",
        stage_controls: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        recommendation_date = str(recommendation_date or date.today().isoformat())
        stage_controls = stage_controls or self.stage_status()
        provider, market, raw_stocks, provider_warning = self._load_market_data_for_t1(source, scan_time=scan_time)
        market = _enrich_market_turnover_context(self.database, market, provider.source)
        market_context = _classify_market_context(market)
        trade_date_status = _t1_trade_date_status(
            recommendation_date,
            scan_time,
            actual_source=str(provider.source or ""),
            market=market,
        )
        market_context["tradeDateStatus"] = trade_date_status
        market_context["stageControls"] = stage_controls
        market["tradeDateStatus"] = trade_date_status
        market["stageControls"] = stage_controls
        if stage_callback:
            stage_callback()
        filtered = self.analysis_service._apply_scope(raw_stocks, "all")
        hard_filtered: List[RawStock] = []
        for stock in filtered:
            allowed, _ = hard_filter_reason(
                stock,
                include_bse=self.settings.include_bse,
                include_star_market=self.settings.include_star_market,
            )
            if not allowed:
                continue
            if stock.limit_status == "down":
                continue
            hard_filtered.append(stock)
        preliminary: List[tuple[RawStock, Dict[str, Any]]] = []
        for stock in hard_filtered:
            candidate = attach_plan(score_stock(stock, risk_mode=risk_mode), scan_time=scan_time)
            preliminary.append(
                (
                    stock,
                    _annotate_t1_candidate(
                        candidate,
                        scan_time=scan_time,
                        market_context=market_context,
                        sector_evidence=None,
                    ),
                )
            )

        preliminary_candidates = _layered_t1_pool([candidate for _, candidate in preliminary], pool_size)
        preliminary_by_code = {candidate.get("code"): candidate for candidate in preliminary_candidates}
        preliminary = [(stock, preliminary_by_code[stock.code]) for stock, _ in preliminary if stock.code in preliminary_by_code]
        evidence_stocks = [stock for stock, _ in preliminary]
        minute_layer_codes = _unique_codes([stock.code for stock in evidence_stocks[:T1_MINUTE_MAX_CODES]])
        tail_minute_layer_codes = minute_layer_codes[:T1_TAIL_MINUTE_MAX_CODES]
        if str(self.settings.kline_period or "").lower() == T1_TAIL_MINUTE_PERIOD:
            minute_layer_codes = tail_minute_layer_codes
        evidence = self._prepare_t1_evidence_run(
            requested_source=source,
            actual_source=provider.source,
            scan_time=scan_time,
            market=market,
            stocks=evidence_stocks,
            force=force,
            recommendation_date=recommendation_date,
            stage_controls=stage_controls,
            minute_codes=minute_layer_codes,
            tail_minute_codes=tail_minute_layer_codes,
        )
        evidence_run = evidence.get("run") or {}
        evidence_run_id = int(evidence_run.get("id") or 0)
        sector_evidence_by_code = evidence.get("sectorEvidenceByCode") or {}
        minute_by_code = evidence.get("minuteByCode") or {}
        tail_minute_by_code = evidence.get("tailMinuteByCode") or {}
        attached_kline_count = 0
        for stock, _ in preliminary:
            minute = minute_by_code.get(_normalize_code(stock.code)) or {}
            series = minute.get("series") or []
            if not series:
                continue
            stock.kline_series = series
            stock.kline_source = str(minute.get("source") or "t1_evidence_minute")
            stock.kline_period = str(minute.get("period") or self.settings.kline_period)
            attached_kline_count += 1

        rescored = []
        for stock, original in preliminary:
            base = attach_plan(score_stock(stock, risk_mode=risk_mode), scan_time=scan_time) if stock.kline_series else original
            annotated = _annotate_t1_candidate(
                base,
                scan_time=scan_time,
                market_context=market_context,
                sector_evidence=sector_evidence_by_code.get(_normalize_code(stock.code)),
            )
            enriched = _apply_data_quality(annotated, provider.source, market)
            code = _normalize_code(stock.code)
            minute = tail_minute_by_code.get(code) or {}
            if not minute and str(self.settings.kline_period or "").lower() == T1_TAIL_MINUTE_PERIOD:
                minute = minute_by_code.get(code) or {}
            enriched.update(
                _p1_shadow_features(
                    candidate=enriched,
                    minute_series=minute.get("series") or [],
                    requested_scan_time=scan_time,
                    market_context=market_context,
                )
            )
            rescored.append(enriched)
        ranked = _layered_t1_pool(rescored, pool_size)
        selection_ranked = [item for item in ranked if (item.get("dataQuality") or {}).get("eligibleForRealTop")]
        data_quality_stats = _data_quality_stats(ranked)

        market["providerWarning"] = provider_warning
        market["scanTime"] = scan_time
        market["runProfile"] = _run_profile_for_pool(pool_size)
        market["recommendedFinalTime"] = DEFAULT_SCAN_TIME
        market["triggerMode"] = "manual"
        market["timeHint"] = _time_hint(scan_time)
        market["candidateTypeCounts"] = _candidate_type_counts(ranked)
        market["dataQualityStats"] = data_quality_stats
        market["minuteKlineStatus"] = _minute_kline_status(
            provider=getattr(self.analysis_service, "kline_provider", None),
            requested=len(minute_layer_codes),
            attached=attached_kline_count,
            period=self.settings.kline_period,
        )
        market["marketContext"] = market_context
        market["tradeDateStatus"] = trade_date_status
        market["aiParticipation"] = _ai_participation_mode(
            ai_available=self._ai_available(),
            data_quality_stats=data_quality_stats,
            evidence_run=evidence_run,
            market_context=market_context,
        )
        market["recommendationPolicy"] = _recommendation_policy(
            market_context=market_context,
            data_quality_stats=data_quality_stats,
            requested_limit=DEFAULT_FINAL_LIMIT,
        )
        market["ai"] = _ai_meta(self.settings)
        market["sourceStatus"] = _source_status(
            requested_source=source,
            actual_source=provider.source,
            market=market,
            warning=provider_warning,
        )
        market["stageControls"] = stage_controls
        t1_evidence_meta = _compact_t1_evidence_meta(evidence_run, evidence.get("missing") or [])
        t1_evidence_meta["stageControls"] = stage_controls
        t1_evidence_meta["reusedExisting"] = bool(evidence.get("reused"))
        t1_evidence_meta["reuseReason"] = str(evidence.get("reuseReason") or "")
        market["t1Evidence"] = t1_evidence_meta
        market["asOfContract"] = _t1_as_of_contract(
            requested_scan_time=scan_time,
            evidence_run=evidence_run,
            market=market,
        )
        market["evidenceRunId"] = evidence_run_id
        if evidence.get("warning"):
            market["providerWarning"] = _join_warning(market.get("providerWarning", ""), str(evidence.get("warning") or ""))
        return {
            "pool": [_pool_item(item, index) for index, item in enumerate(ranked, start=1)],
            "selectionPool": [_pool_item(item, index) for index, item in enumerate(selection_ranked, start=1)],
            "totalCount": len(raw_stocks),
            "hardFilteredCount": len(hard_filtered),
            "actualSource": provider.source,
            "evidenceRunId": evidence_run_id,
            "market": market,
        }

    def _load_market_data_for_t1(self, source: str, scan_time: str = DEFAULT_SCAN_TIME) -> tuple[Any, Dict[str, Any], List[RawStock], str]:
        if source == "mock":
            provider, market, raw_stocks, provider_warning = self.analysis_service._load_market_data("mock")
            market = dict(market or {})
            provider_warning = _join_warning(provider_warning, "当前使用 Mock 模拟数据，不可用于真实 T+1 推荐。")
            market["providerWarning"] = provider_warning
            market["cacheStatus"] = {"used": False}
            market["mockStatus"] = {"used": True, "reason": "用户手动选择 Mock 数据源"}
            return provider, market, raw_stocks, provider_warning

        context_store = self.analysis_service.full_market_context_store
        frozen = context_store.latest_for_scan(date.today().isoformat(), scan_time, source=source)
        if not frozen:
            frozen = context_store.latest_cached()
        frozen_stocks = frozen.get("stocks") or []
        frozen_meta = market_context_meta(frozen) if frozen else {}
        if _usable_t1_full_market_freeze(frozen, source, scan_time=scan_time):
            market = dict(frozen.get("market") or {})
            generated_at = str(frozen_meta.get("generatedAt") or "")
            actual = str(frozen_meta.get("source") or "unknown")
            warning = _join_warning(
                str(frozen.get("warning") or ""),
                f"已复用同日 T+1 全市场冻结快照，生成时间 {generated_at or '--'}，避免连续重跑候选池跳动。",
            )
            market["providerWarning"] = warning
            market["cacheStatus"] = {
                "used": False,
                "t1Frozen": True,
                "generatedAt": generated_at,
                "ageSeconds": int(frozen_meta.get("ageSeconds") or 0),
                "count": len(frozen_stocks),
                "source": actual,
                "scanTime": scan_time,
                "snapshotVersion": frozen_meta.get("snapshotVersion") or "",
                "status": frozen_meta.get("status") or "",
                "tradeDate": frozen_meta.get("tradeDate") or "",
                "asOfTime": frozen_meta.get("asOfTime") or generated_at,
                "coverage": frozen_meta.get("coverage") or {},
                "failedPages": frozen_meta.get("failedPages") or [],
            }
            market["marketSnapshotFreeze"] = {
                "enabled": True,
                "reused": True,
                "source": actual,
                "generatedAt": generated_at,
                "snapshotVersion": frozen_meta.get("snapshotVersion") or "",
                "status": frozen_meta.get("status") or "",
                "tradeDate": frozen_meta.get("tradeDate") or "",
                "asOfTime": frozen_meta.get("asOfTime") or generated_at,
                "coverage": frozen_meta.get("coverage") or {},
                "failedPages": frozen_meta.get("failedPages") or [],
                "ageSeconds": int(frozen_meta.get("ageSeconds") or 0),
                "reuseWindowSeconds": T1_FULL_MARKET_FREEZE_REUSE_SECONDS,
                "count": len(frozen_stocks),
            }
            provider = SimpleNamespace(source=actual)
            return provider, market, frozen_stocks, warning

        refresh_sources = [source] if source in {"sina", "akshare"} else ["sina", "akshare"]
        refresh_errors: List[str] = []
        for refresh_source in refresh_sources:
            try:
                provider = self.analysis_service.sina_provider if refresh_source == "sina" else self.analysis_service.akshare_provider
                stocks = provider.fetch_snapshot()
                if len(stocks) < 1200:
                    raise RuntimeError(f"全市场快照样本过少：{len(stocks)}")
                market_stocks = self.analysis_service._confirmed_universe(stocks)
                market = dict(provider.fetch_market(market_stocks) or {})
                generated_at = datetime.now().isoformat(timespec="seconds")
                cache_meta: Dict[str, Any] = {}
                try:
                    context = self.analysis_service.full_market_context_store._write_cache(
                        stocks=stocks,
                        requested_source=refresh_source,
                        actual_source=refresh_source,
                        warning="",
                        provider_meta=dict(getattr(provider, "last_meta", {}) or {}),
                    )
                    cache_meta = market_context_meta(context)
                    generated_at = str(cache_meta.get("generatedAt") or generated_at)
                except Exception as cache_error:  # noqa: BLE001 - cache write must not break live T+1 selection.
                    LOGGER.warning("t1 full market cache write failed source=%s error=%s", refresh_source, cache_error)
                warning = _join_warning(
                    f"已使用 {refresh_source} 真实全市场行情，生成时间 {generated_at or '--'}。",
                )
                market["providerWarning"] = warning
                market["cacheStatus"] = {
                    "used": False,
                    "generatedAt": generated_at,
                    "count": len(stocks),
                    "snapshotVersion": cache_meta.get("snapshotVersion") or "",
                    "status": cache_meta.get("status") or "",
                    "coverage": cache_meta.get("coverage") or {},
                    "failedPages": cache_meta.get("failedPages") or [],
                }
                market["marketSnapshotFreeze"] = {
                    "enabled": True,
                    "reused": False,
                    "source": refresh_source,
                    "generatedAt": generated_at,
                    "snapshotVersion": cache_meta.get("snapshotVersion") or "",
                    "status": cache_meta.get("status") or "",
                    "tradeDate": cache_meta.get("tradeDate") or date.today().isoformat(),
                    "asOfTime": cache_meta.get("asOfTime") or generated_at,
                    "coverage": cache_meta.get("coverage") or {},
                    "failedPages": cache_meta.get("failedPages") or [],
                    "reuseWindowSeconds": T1_FULL_MARKET_FREEZE_REUSE_SECONDS,
                    "count": len(stocks),
                }
                provider = SimpleNamespace(source=refresh_source)
                return provider, market, stocks, warning
            except Exception as error:  # noqa: BLE001 - try next real full-market source before cache/mock.
                LOGGER.warning("t1 full market refresh failed source=%s error=%s", refresh_source, error)
                refresh_errors.append(f"{refresh_source} 行情源失败：{error}")

        cached = self.analysis_service.full_market_context_store.latest_cached()
        cached_stocks = cached.get("stocks") or []
        try:
            if len(cached_stocks) >= 1200 and _usable_t1_full_market_freeze(cached, source, scan_time=scan_time, allow_stale=True):
                meta = market_context_meta(cached)
                market = dict(cached.get("market") or {})
                warning = _join_warning(
                    str(cached.get("warning") or ""),
                    *refresh_errors,
                    f"已回退全市场缓存，生成时间 {meta.get('generatedAt') or '--'}，缓存{'已过期' if meta.get('stale') else '未过期'}。",
                )
                market["providerWarning"] = warning
                market["cacheStatus"] = {
                    "used": True,
                    "generatedAt": meta.get("generatedAt", ""),
                    "stale": bool(meta.get("stale")),
                    "ageSeconds": int(meta.get("ageSeconds") or 0),
                    "count": len(cached_stocks),
                    "fallbackReason": "；".join(refresh_errors),
                    "snapshotVersion": meta.get("snapshotVersion") or "",
                    "status": meta.get("status") or "",
                    "tradeDate": meta.get("tradeDate") or "",
                    "asOfTime": meta.get("asOfTime") or meta.get("generatedAt") or "",
                    "coverage": meta.get("coverage") or {},
                    "failedPages": meta.get("failedPages") or [],
                }
                provider = SimpleNamespace(source=f"cache:{meta.get('source') or 'unknown'}")
                market["marketSnapshotFreeze"] = {
                    "enabled": True,
                    "reused": True,
                    "source": meta.get("source") or "unknown",
                    "generatedAt": meta.get("generatedAt") or "",
                    "snapshotVersion": meta.get("snapshotVersion") or "",
                    "status": meta.get("status") or "",
                    "tradeDate": meta.get("tradeDate") or "",
                    "asOfTime": meta.get("asOfTime") or meta.get("generatedAt") or "",
                    "coverage": meta.get("coverage") or {},
                    "failedPages": meta.get("failedPages") or [],
                    "ageSeconds": int(meta.get("ageSeconds") or 0),
                    "reuseWindowSeconds": T1_FULL_MARKET_FREEZE_REUSE_SECONDS,
                    "count": len(cached_stocks),
                }
                return provider, market, cached_stocks, warning
            provider, market, raw_stocks, provider_warning = self.analysis_service._load_market_data("mock")
            market = dict(market or {})
            market["providerWarning"] = _join_warning(
                provider_warning,
                *refresh_errors,
                "真实行情源失败且无可用全市场缓存，已回退 Mock。",
                "当前使用 Mock 模拟数据，不可用于真实 T+1 推荐。",
            )
            market["cacheStatus"] = {"used": False, "fallbackReason": "；".join(refresh_errors)}
            market["mockStatus"] = {"used": True, "reason": "；".join(refresh_errors)}
            return provider, market, raw_stocks, market["providerWarning"]
        except Exception as error:  # noqa: BLE001 - keep T+1 task visible even if fallback metadata is broken.
            provider, market, raw_stocks, provider_warning = self.analysis_service._load_market_data("mock")
            market = dict(market or {})
            market["providerWarning"] = _join_warning(
                provider_warning,
                *refresh_errors,
                f"缓存回退失败，已回退 Mock：{error}",
                "当前使用 Mock 模拟数据，不可用于真实 T+1 推荐。",
            )
            market["cacheStatus"] = {"used": False, "fallbackReason": str(error)}
            market["mockStatus"] = {"used": True, "reason": str(error)}
            return provider, market, raw_stocks, market["providerWarning"]

    def _prepare_t1_evidence_run(
        self,
        requested_source: str,
        actual_source: str,
        scan_time: str,
        market: Dict[str, Any],
        stocks: List[RawStock],
        force: bool = False,
        recommendation_date: str = "",
        stage_controls: Optional[Dict[str, Any]] = None,
        minute_codes: Optional[List[str]] = None,
        tail_minute_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        recommendation_date = str(recommendation_date or date.today().isoformat())
        stage_controls = stage_controls or market.get("stageControls") or self.stage_status()
        market["stageControls"] = stage_controls
        codes = _unique_codes([stock.code for stock in stocks])
        code_set = set(codes)
        minute_codes = [code for code in _unique_codes(minute_codes or codes) if code in code_set]
        tail_minute_codes = [code for code in _unique_codes(tail_minute_codes or minute_codes) if code in set(minute_codes)]
        minute_candidate_count = len(minute_codes)
        tail_minute_candidate_count = len(tail_minute_codes)
        existing = self.database.latest_reusable_t1_overnight_evidence_run(
            recommendation_date=recommendation_date,
            scan_time=scan_time,
            requested_source=requested_source,
            qualities=T1_REUSABLE_EVIDENCE_QUALITIES,
        )
        if existing:
            existing_request = existing.get("request") or {}
            sector_maps = self.database.load_t1_overnight_stock_sector_maps(int(existing["id"]), codes)
            minute_maps = self.database.load_t1_overnight_minute_snapshots(int(existing["id"]), minute_codes, period=self.settings.kline_period)
            tail_minute_maps = self.database.load_t1_overnight_minute_snapshots(int(existing["id"]), tail_minute_codes, period=T1_TAIL_MINUTE_PERIOD)
            requires_sector_state = _stage_control_enabled(
                market.get("stageControls") or {},
                "sectorStateShadow",
                T1_SECTOR_STATE_SHADOW_ENABLED,
            )
            if (
                existing_request.get("version") == T1_EVIDENCE_VERSION
                and existing_request.get("evidenceCoverage")
                and len(sector_maps) >= len(codes)
                and len(minute_maps) >= len(minute_codes)
                and len(tail_minute_maps) >= len(tail_minute_codes)
                and _t1_evidence_run_is_reusable(existing, len(codes), requested_minute_count=minute_candidate_count)
                and (not requires_sector_state or _t1_sector_maps_have_state_field(sector_maps))
            ):
                return {
                    "run": existing,
                    "sectorEvidenceByCode": {code: item.get("sectorEvidence") or {} for code, item in sector_maps.items()},
                    "minuteByCode": minute_maps,
                    "tailMinuteByCode": tail_minute_maps,
                    "missing": existing.get("missing") or [],
                    "warning": "已复用同日同时间段 T+1 冻结证据；强制重跑只重算推荐，不重抓已合格证据。",
                    "reused": True,
                    "reuseReason": _t1_evidence_reuse_reason(existing),
                }
        now = datetime.now().isoformat(timespec="seconds")
        evidence_run_id = self.database.create_t1_overnight_evidence_run(
            {
                "recommendationDate": recommendation_date,
                "scanTime": scan_time,
                "requestedSource": requested_source,
                "actualSource": actual_source,
                "status": "running",
                "marketSource": actual_source,
                "request": {
                    "version": T1_EVIDENCE_VERSION,
                    "requestedSource": requested_source,
                    "actualSource": actual_source,
                    "scanTime": scan_time,
                    "candidateCount": len(codes),
                    "minuteCandidateCount": minute_candidate_count,
                    "tailMinuteCandidateCount": tail_minute_candidate_count,
                    "minuteCodes": minute_codes,
                    "tailMinuteCodes": tail_minute_codes,
                    "force": bool(force),
                },
                "market": market,
                "startedAt": now,
                "workerId": self.worker_id,
                "leaseExpiresAt": (datetime.now() + timedelta(seconds=T1_RUN_LEASE_SECONDS)).isoformat(timespec="seconds"),
            }
        )
        missing: List[str] = []
        warnings: List[str] = []
        identity_cache_stats = {"hit": 0, "fresh": 0, "stale": 0, "fetchRequested": 0, "fetchSucceeded": 0}
        cached_profile_maps: List[Dict[str, Any]] = []
        stocks_requiring_profile_fetch: List[RawStock] = []
        try:
            identity_cache = self.database.load_t1_stock_sector_identity_cache(codes)
            cached_profile_maps = _profile_maps_from_identity_cache(
                stocks,
                identity_cache,
                evidence_run_id=evidence_run_id,
                recommendation_date=recommendation_date,
                scan_time=scan_time,
            )
            identity_cache_stats["hit"] = len(cached_profile_maps)
            identity_cache_stats["fresh"] = len(
                [
                    item
                    for item in cached_profile_maps
                    if _t1_layer_status(((item.get("sectorEvidence") or {}).get("evidenceLayers") or {}), "identity") == "available"
                ]
            )
            identity_cache_stats["stale"] = len(cached_profile_maps) - identity_cache_stats["fresh"]
            stocks_requiring_profile_fetch = _stocks_requiring_identity_fetch(stocks, identity_cache)
            identity_cache_stats["fetchRequested"] = len(stocks_requiring_profile_fetch)
        except Exception as error:  # noqa: BLE001 - cache miss must not block evidence collection.
            LOGGER.warning("t1 stock sector identity cache load failed run=%s error=%s", evidence_run_id, error)
            stocks_requiring_profile_fetch = stocks
        self.database.upsert_t1_overnight_market_snapshots(
            evidence_run_id,
            [
                {
                    "snapshotTime": scan_time,
                    "source": actual_source,
                    "capturedAt": now,
                    "market": market,
                }
            ],
        )

        sector_source = ""
        sectors: List[Dict[str, Any]] = []
        constituents: List[Dict[str, Any]] = []
        stock_maps: List[Dict[str, Any]] = []
        profile_maps: List[Dict[str, Any]] = []
        internal_sectors: List[Dict[str, Any]] = []
        sector_provider = None
        try:
            sector_provider = get_sector_provider("mock" if requested_source == "mock" or actual_source == "mock" else "auto")
            if T1_IDENTITY_LIVE_FETCH_ENABLED:
                profile_maps = _fetch_t1_stock_sector_profile_maps(
                    provider=sector_provider,
                    stocks=stocks_requiring_profile_fetch,
                    evidence_run_id=evidence_run_id,
                    recommendation_date=recommendation_date,
                    scan_time=scan_time,
                )
            else:
                queued = self._queue_t1_identity_backfill(stocks_requiring_profile_fetch, source=actual_source)
                if queued:
                    warnings.append(f"{queued} 只股票行业/概念身份缺失，已进入异步补拉队列")
            identity_cache_stats["fetchSucceeded"] = len(profile_maps)
            cache_rows = _identity_cache_rows_from_profile_maps(profile_maps)
            if cache_rows:
                self.database.upsert_t1_stock_sector_identity_cache(cache_rows)
            profile_maps = _merge_profile_map_sources(cached_profile_maps, profile_maps)
            internal_sectors = _build_t1_internal_sector_snapshots(
                stocks=stocks,
                profile_maps=profile_maps,
                recommendation_date=recommendation_date,
                scan_time=scan_time,
            )
            sector_payload = sector_provider.fetch_sector_snapshot("all")
            external_source = str(sector_payload.get("source") or getattr(sector_provider, "source", ""))
            external_sectors = _tag_t1_external_sector_snapshots(
                _rank_t1_sector_snapshots(sector_payload.get("sectors") or []),
                external_source,
            )
            targets = _t1_sector_constituent_targets_for_profiles(external_sectors, profile_maps)
            constituents = _fetch_t1_sector_constituents(sector_provider, targets)
            external_sectors = _apply_t1_sector_constituent_counts(external_sectors, constituents)
            sectors = _merge_t1_sector_snapshot_sources(internal_sectors, external_sectors)
            market_context_for_state = _classify_market_context(market)
            market_context_for_state["stageControls"] = stage_controls
            sectors = _with_t1_sector_state_shadow(sectors, market_context_for_state)
            market["sectorStateShadowSummary"] = _build_t1_sector_state_summary(sectors, stage_controls)
            market["styleRegimeShadow"] = _build_t1_style_regime_shadow(sectors, market_context_for_state, stage_controls)
            sector_source = _t1_sector_sources_label(external_source, internal_sectors)
            stock_maps = _build_t1_stock_sector_maps(
                stocks=stocks,
                sectors=sectors,
                constituents=constituents,
                evidence_run_id=evidence_run_id,
                recommendation_date=recommendation_date,
                scan_time=scan_time,
                source=sector_source,
            )
            if profile_maps:
                stock_maps = _merge_t1_stock_sector_profile_maps(
                    stock_maps,
                    profile_maps,
                    sectors=sectors,
                    evidence_run_id=evidence_run_id,
                    recommendation_date=recommendation_date,
                    scan_time=scan_time,
                    source=sector_source,
                )
            self.database.upsert_t1_overnight_sector_snapshots(evidence_run_id, sectors)
            self.database.upsert_t1_overnight_sector_constituents(evidence_run_id, constituents)
            self.database.upsert_t1_overnight_stock_sector_maps(evidence_run_id, stock_maps)
            if sector_payload.get("warnings"):
                warnings.extend([str(item) for item in sector_payload.get("warnings") or [] if str(item or "").strip()])
            coverage = _t1_sector_evidence_coverage(stock_maps)
            identity_count = int(((coverage.get("identity") or {}).get("available")) or 0)
            snapshot_count = int(((coverage.get("sectorSnapshot") or {}).get("available")) or 0)
            rank_count = int(((coverage.get("constituentRank") or {}).get("available")) or 0)
            if identity_count < len(stock_maps):
                missing.append(f"T+1 逐股行业/概念身份覆盖 {identity_count}/{len(stock_maps)}；未覆盖候选不得使用板块理由")
            if snapshot_count < len(stock_maps):
                missing.append(f"T+1 同日板块强度覆盖 {snapshot_count}/{len(stock_maps)}；未覆盖候选不能写板块强势、涨停数或资金共振")
            if rank_count < len(stock_maps):
                missing.append(f"T+1 成分股排名覆盖 {rank_count}/{len(stock_maps)}；未覆盖候选不能写行业前排")
        except Exception as error:  # noqa: BLE001 - sector evidence can be missing, but must be explicit.
            message = f"T+1 行业/概念证据采集失败：{error}"
            LOGGER.warning("t1 evidence sector collection failed run=%s error=%s", evidence_run_id, error)
            missing.append(message)
            if not profile_maps:
                if T1_IDENTITY_LIVE_FETCH_ENABLED:
                    profile_maps = _fetch_t1_stock_sector_profile_maps(
                        provider=sector_provider,
                        stocks=stocks_requiring_profile_fetch,
                        evidence_run_id=evidence_run_id,
                        recommendation_date=recommendation_date,
                        scan_time=scan_time,
                        base_reason=message,
                    )
                else:
                    queued = self._queue_t1_identity_backfill(stocks_requiring_profile_fetch, source=actual_source)
                    if queued:
                        missing.append(f"{queued} 只股票行业/概念身份缺失，已进入异步补拉队列")
                identity_cache_stats["fetchSucceeded"] = len(profile_maps)
                cache_rows = _identity_cache_rows_from_profile_maps(profile_maps)
                if cache_rows:
                    self.database.upsert_t1_stock_sector_identity_cache(cache_rows)
                profile_maps = _merge_profile_map_sources(cached_profile_maps, profile_maps)
            if profile_maps:
                internal_sectors = _build_t1_internal_sector_snapshots(
                    stocks=stocks,
                    profile_maps=profile_maps,
                    recommendation_date=recommendation_date,
                    scan_time=scan_time,
                )
                if internal_sectors:
                    sectors = internal_sectors
                    market_context_for_state = _classify_market_context(market)
                    market_context_for_state["stageControls"] = stage_controls
                    sectors = _with_t1_sector_state_shadow(sectors, market_context_for_state)
                    market["sectorStateShadowSummary"] = _build_t1_sector_state_summary(sectors, stage_controls)
                    market["styleRegimeShadow"] = _build_t1_style_regime_shadow(sectors, market_context_for_state, stage_controls)
                    sector_source = T1_INTERNAL_SECTOR_SOURCE
                    stock_maps = _build_t1_stock_sector_maps(
                        stocks=stocks,
                        sectors=sectors,
                        constituents=[],
                        evidence_run_id=evidence_run_id,
                        recommendation_date=recommendation_date,
                        scan_time=scan_time,
                        source=sector_source,
                    )
                    stock_maps = _merge_t1_stock_sector_profile_maps(
                        stock_maps,
                        profile_maps,
                        sectors=sectors,
                        evidence_run_id=evidence_run_id,
                        recommendation_date=recommendation_date,
                        scan_time=scan_time,
                        source=sector_source,
                    )
                    missing.append("外部板块快照不可用，已改用 T+1 候选池聚合板块强度；不含外部板块资金流，也不代表全市场板块统计")
                else:
                    sector_source = "stock_sector_identity"
                    stock_maps = profile_maps
                    missing.append("板块快照/成分股失败，已用逐股行业/概念身份作为降级证据；不能支持今日板块强度结论")
            else:
                stock_maps = _unavailable_t1_stock_sector_maps(
                    stocks=stocks,
                    evidence_run_id=evidence_run_id,
                    recommendation_date=recommendation_date,
                    scan_time=scan_time,
                    reason=message,
                )
            self.database.upsert_t1_overnight_sector_snapshots(evidence_run_id, sectors)
            self.database.upsert_t1_overnight_sector_constituents(evidence_run_id, constituents)
            self.database.upsert_t1_overnight_stock_sector_maps(evidence_run_id, stock_maps)
            coverage = _t1_sector_evidence_coverage(stock_maps)
            identity_count = int(((coverage.get("identity") or {}).get("available")) or 0)
            snapshot_count = int(((coverage.get("sectorSnapshot") or {}).get("available")) or 0)
            rank_count = int(((coverage.get("constituentRank") or {}).get("available")) or 0)
            if identity_count < len(stock_maps):
                missing.append(f"T+1 逐股行业/概念身份覆盖 {identity_count}/{len(stock_maps)}；未覆盖候选不得使用板块理由")
            if snapshot_count < len(stock_maps):
                missing.append(f"T+1 同日板块强度覆盖 {snapshot_count}/{len(stock_maps)}；未覆盖候选不能写板块强势、涨停数或资金共振")
            if rank_count < len(stock_maps):
                missing.append(f"T+1 成分股排名覆盖 {rank_count}/{len(stock_maps)}；未覆盖候选不能写行业前排")

        minute_source = ""
        minute_rows: List[Dict[str, Any]] = []
        minute_code_set = set(minute_codes)
        tail_minute_code_set = set(tail_minute_codes)
        if actual_source == "mock" or requested_source == "mock":
            missing.append("Mock 数据不采集真实分钟线")
            minute_rows = [
                {
                    "code": code,
                    "period": self.settings.kline_period,
                    "source": "mock",
                    "qualityStatus": "research_only" if code not in minute_code_set else "unavailable",
                    "error": "未进入 15m 候选层级" if code not in minute_code_set else "Mock 数据不采集真实分钟线",
                    "series": [],
                }
                for code in codes
            ]
        else:
            try:
                minute_rows_by_code = _fetch_t1_minute_rows_with_retries(
                    self.database,
                    self.analysis_service.kline_provider,
                    codes=minute_codes,
                    period=self.settings.kline_period,
                    recommendation_date=recommendation_date,
                    exclude_run_id=evidence_run_id,
                )
                minute_source = getattr(self.analysis_service.kline_provider, "source", "multi_minute")
                provider_warning = str(getattr(self.analysis_service.kline_provider, "last_warning", "") or "")
                if provider_warning:
                    warnings.append(provider_warning)
                for code in codes:
                    if code not in minute_code_set:
                        minute_rows.append(
                            {
                                "code": code,
                                "period": self.settings.kline_period,
                                "source": "",
                                "qualityStatus": "research_only",
                                "error": "未进入 15m 候选层级，未请求分钟线",
                                "series": [],
                                "pointCount": 0,
                            }
                        )
                        continue
                    row = dict(minute_rows_by_code.get(code) or {})
                    if row:
                        row["code"] = code
                        row["period"] = str(row.get("period") or self.settings.kline_period)
                        row["source"] = str(row.get("source") or minute_source)
                        row["qualityStatus"] = str(row.get("qualityStatus") or ("available" if row.get("series") else "unavailable"))
                        row["error"] = str(row.get("error") or "")
                        row["pointCount"] = int(row.get("pointCount") or len(row.get("series") or []))
                    else:
                        row = {
                            "code": code,
                            "period": self.settings.kline_period,
                            "source": "",
                            "qualityStatus": "unavailable",
                            "error": provider_warning or "真实分钟线未取到",
                            "series": [],
                            "pointCount": 0,
                        }
                    minute_rows.append(row)
                minute_count = len([item for item in minute_rows if item.get("series")])
                if minute_count < len(minute_codes):
                    missing.append(f"真实 15m 分钟线缺失 {len(minute_codes) - minute_count}/{len(minute_codes)}")
            except Exception as error:  # noqa: BLE001
                message = f"T+1 分钟线采集失败：{error}"
                LOGGER.warning("t1 evidence minute collection failed run=%s error=%s", evidence_run_id, error)
                missing.append(message)
                minute_rows = [
                    {
                        "code": code,
                        "period": self.settings.kline_period,
                        "source": "",
                        "qualityStatus": "research_only" if code not in minute_code_set else "unavailable",
                        "error": "未进入 15m 候选层级" if code not in minute_code_set else message,
                        "series": [],
                        "pointCount": 0,
                    }
                    for code in codes
                ]
        self.database.upsert_t1_overnight_minute_snapshots(evidence_run_id, self.settings.kline_period, minute_rows)

        tail_minute_rows = minute_rows if self.settings.kline_period == T1_TAIL_MINUTE_PERIOD else []
        if self.settings.kline_period != T1_TAIL_MINUTE_PERIOD and actual_source != "mock" and requested_source != "mock":
            try:
                tail_rows_by_code = _fetch_t1_minute_rows_with_retries(
                    self.database,
                    self.analysis_service.kline_provider,
                    codes=tail_minute_codes,
                    period=T1_TAIL_MINUTE_PERIOD,
                    recommendation_date=recommendation_date,
                    exclude_run_id=evidence_run_id,
                )
                tail_minute_rows = []
                for code in codes:
                    if code not in tail_minute_code_set:
                        tail_minute_rows.append(
                            {
                                "code": code,
                                "period": T1_TAIL_MINUTE_PERIOD,
                                "source": "",
                                "qualityStatus": "research_only",
                                "error": "未进入 1m 尾盘候选层级，未请求分钟线",
                                "series": [],
                                "pointCount": 0,
                            }
                        )
                        continue
                    row = dict(tail_rows_by_code.get(code) or {})
                    if row:
                        row["code"] = code
                        row["period"] = T1_TAIL_MINUTE_PERIOD
                        row["source"] = str(row.get("source") or minute_source or "multi_minute")
                        row["qualityStatus"] = str(row.get("qualityStatus") or ("available" if row.get("series") else "unavailable"))
                        row["error"] = str(row.get("error") or "")
                        row["pointCount"] = int(row.get("pointCount") or len(row.get("series") or []))
                    else:
                        row = {
                            "code": code,
                            "period": T1_TAIL_MINUTE_PERIOD,
                            "source": "",
                            "qualityStatus": "unavailable",
                            "error": "1m 尾盘分钟线未取到",
                            "series": [],
                            "pointCount": 0,
                        }
                    tail_minute_rows.append(row)
                tail_count = len([item for item in tail_minute_rows if item.get("series")])
                if tail_count < len(tail_minute_codes):
                    missing.append(f"T+1 尾盘 1m 证据缺失 {len(tail_minute_codes) - tail_count}/{len(tail_minute_codes)}；缺失候选只做尾盘影子数据不足")
            except Exception as error:  # noqa: BLE001
                message = f"T+1 尾盘 1m 证据采集失败：{error}"
                LOGGER.warning("t1 evidence tail minute collection failed run=%s error=%s", evidence_run_id, error)
                missing.append(message)
                tail_minute_rows = [
                    {
                        "code": code,
                        "period": T1_TAIL_MINUTE_PERIOD,
                        "source": "",
                        "qualityStatus": "research_only" if code not in tail_minute_code_set else "unavailable",
                        "error": "未进入 1m 尾盘候选层级" if code not in tail_minute_code_set else message,
                        "series": [],
                        "pointCount": 0,
                    }
                    for code in codes
                ]
            self.database.upsert_t1_overnight_minute_snapshots(evidence_run_id, T1_TAIL_MINUTE_PERIOD, tail_minute_rows)
        elif self.settings.kline_period == T1_TAIL_MINUTE_PERIOD:
            self.database.upsert_t1_overnight_minute_snapshots(evidence_run_id, T1_TAIL_MINUTE_PERIOD, tail_minute_rows)

        minute_count = len([item for item in minute_rows if item.get("series")])
        tail_minute_count = len([item for item in tail_minute_rows if item.get("series")])
        coverage = _t1_sector_evidence_coverage(stock_maps)
        market.setdefault("sectorStateShadowSummary", _build_t1_sector_state_summary(sectors, stage_controls))
        market.setdefault("styleRegimeShadow", _build_t1_style_regime_shadow(sectors, _classify_market_context(market), stage_controls))
        identity_count = int(((coverage.get("identity") or {}).get("available")) or 0)
        snapshot_count = int(((coverage.get("sectorSnapshot") or {}).get("available")) or 0)
        rank_count = int(((coverage.get("constituentRank") or {}).get("available")) or 0)
        quality_status = _t1_evidence_quality_status(
            candidate_count=len(codes),
            identity_count=identity_count,
            sector_snapshot_count=snapshot_count,
            constituent_rank_count=rank_count,
            minute_count=minute_count,
            missing=missing,
        )
        request_meta = {
            "version": T1_EVIDENCE_VERSION,
            "requestedSource": requested_source,
            "actualSource": actual_source,
            "scanTime": scan_time,
            "candidateCount": len(codes),
            "minuteCandidateCount": minute_candidate_count,
            "tailMinuteCandidateCount": tail_minute_candidate_count,
            "minuteCodes": minute_codes,
            "tailMinuteCodes": tail_minute_codes,
            "force": bool(force),
            "evidenceCoverage": coverage,
            "identityCache": identity_cache_stats,
            "tailMinutePeriod": T1_TAIL_MINUTE_PERIOD,
            "tailMinuteCount": tail_minute_count,
        }
        finished_at = datetime.now().isoformat(timespec="seconds")
        self.database.update_t1_overnight_evidence_run(
            evidence_run_id,
            {
                "status": "done",
                "actualSource": actual_source,
                "marketSource": actual_source,
                "sectorSource": sector_source,
                "minuteSource": minute_source,
                "marketSnapshotCount": 1,
                "sectorCount": len(sectors),
                "constituentCount": len(constituents),
                "stockMapCount": len(stock_maps),
                "minuteCount": minute_count,
                "qualityStatus": quality_status,
                "missing": _unique_strings(missing),
                "request": request_meta,
                "market": market,
                "error": "；".join(_unique_strings(missing)) if missing else "",
                "finishedAt": finished_at,
            },
        )
        run = self.database.load_t1_overnight_evidence_run(evidence_run_id) or {"id": evidence_run_id}
        sector_maps = self.database.load_t1_overnight_stock_sector_maps(evidence_run_id, codes)
        minute_maps = self.database.load_t1_overnight_minute_snapshots(evidence_run_id, codes, period=self.settings.kline_period)
        tail_minute_maps = self.database.load_t1_overnight_minute_snapshots(evidence_run_id, codes, period=T1_TAIL_MINUTE_PERIOD)
        return {
            "run": run,
            "sectorEvidenceByCode": {code: item.get("sectorEvidence") or {} for code, item in sector_maps.items()},
            "minuteByCode": minute_maps,
            "tailMinuteByCode": tail_minute_maps,
            "missing": _unique_strings(missing),
            "warning": "；".join(_unique_strings(warnings)),
            "reused": False,
            "reuseReason": "",
        }

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
        try:
            self.database.update_t1_overnight_selection_run(
                run_id,
                {
                    "status": "running",
                    "startedAt": datetime.now().isoformat(timespec="seconds"),
                    "currentStage": "loading_market",
                    "workerId": self.worker_id,
                    "leaseExpiresAt": (datetime.now() + timedelta(seconds=T1_RUN_LEASE_SECONDS)).isoformat(timespec="seconds"),
                },
            )
        except Exception as error:  # noqa: BLE001 - watchdog will close the pending lease.
            LOGGER.error("t1 overnight run could not claim lease id=%s error=%s", run_id, error)
            return
        heartbeat_stop, heartbeat_thread = self._start_run_heartbeat(run_id)
        try:
            run = self.database.load_t1_overnight_selection_run(run_id)
            if not run:
                return
            final_limit = int(run.get("finalLimit") or DEFAULT_FINAL_LIMIT)
            batch_size = int(run.get("batchSize") or DEFAULT_BATCH_SIZE)
            batch_pick = int(run.get("batchPick") or DEFAULT_BATCH_PICK)
            risk_mode = str(run.get("riskMode") or "balanced")
            source = str(run.get("requestedSource") or "auto")
            scan_time = str(run.get("scanTime") or DEFAULT_SCAN_TIME)
            recommendation_date = str(run.get("recommendationDate") or date.today().isoformat())
            request = run.get("request") or {}
            requested_pool_size = _clamp_int(request.get("poolSize"), 80, 600, DEFAULT_POOL_SIZE)
            stage_config = _normalize_stage_config(request.get("stageConfig") or self.stage_config())
            stage_controls = request.get("stageControls") or self.stage_status(stage_config)

            batch = self._build_pool(
                risk_mode,
                source,
                scan_time,
                requested_pool_size,
                force=bool(request.get("force")),
                stage_callback=lambda: self.database.update_t1_overnight_selection_run(run_id, {"currentStage": "building_pool"}),
                recommendation_date=recommendation_date,
                stage_controls=stage_controls,
            )
            pool = batch["pool"]
            selection_pool = batch.get("selectionPool") or []
            if not pool:
                raise ValueError(batch.get("error") or "没有可供 T+1 超短线筛选的候选池。")
            market = batch.get("market") or {}
            market["recommendationPolicy"] = _recommendation_policy(
                market_context=market.get("marketContext") or {},
                data_quality_stats=market.get("dataQualityStats") or {},
                requested_limit=final_limit,
            )
            policy = market.get("recommendationPolicy") or {}
            policy_max_pick = policy.get("maxPickCount")
            effective_final_limit = min(final_limit, max(0, int(policy_max_pick if policy_max_pick is not None else final_limit)))
            market["baselineGroups"] = {
                "rules": _baseline_rule_picks(selection_pool, effective_final_limit),
                "momentum": _baseline_simple_change_picks(selection_pool, effective_final_limit),
                "random": _baseline_deterministic_random_picks(selection_pool, effective_final_limit, str(run.get("recommendationDate") or "")),
                "market": _baseline_market_pool_picks(selection_pool, BASELINE_MARKET_SAMPLE_LIMIT),
                "tailSupportShadow": _tail_support_shadow_top_picks(selection_pool, effective_final_limit),
                "aiPreCritic": [],
                "final": [],
            }
            market["shadowTop3"] = {
                "version": T1_RULE_TAIL_SHADOW_VERSION,
                "stage": "shadow",
                "label": "规则尾盘影子Top3",
                "description": "系统假设更重视尾盘承接时的规则对照，不影响正式 Top3。",
                "picks": [dict(item) for item in market["baselineGroups"].get("tailSupportShadow", [])],
            }
            batch_count = math.ceil(len(selection_pool) / batch_size) if selection_pool else 0
            self.database.upsert_t1_overnight_selection_items(run_id, pool, stage="pool")
            self.database.update_t1_overnight_selection_run(
                run_id,
                {
                    "actualSource": batch.get("actualSource", ""),
                    "evidenceRunId": int(batch.get("evidenceRunId") or 0),
                    "totalCount": batch.get("totalCount", 0),
                    "hardFilteredCount": batch.get("hardFilteredCount", 0),
                    "poolSize": len(pool),
                    "batchCount": batch_count,
                    "market": market,
                    "currentStage": "building_pool",
                },
            )
            LOGGER.info(
                "t1 overnight select start raw=%s hard_filtered=%s pool=%s batch_size=%s scan_time=%s",
                batch.get("totalCount"),
                batch.get("hardFilteredCount"),
                len(pool),
                batch_size,
                scan_time,
            )
            if effective_final_limit <= 0:
                reason = str(policy.get("reason") or "市场环境或数据质量不适合隔夜，当前推荐 0 只。")
                _stamp_decision_timing(market)
                self.database.update_t1_overnight_selection_run(
                    run_id,
                    {
                        "status": "done",
                        "selectionMode": "market_risk_zero_pick",
                        "currentStage": "done",
                        "finalPicks": [],
                        "finalCount": 0,
                        "market": market,
                        "error": reason,
                        "finishedAt": datetime.now().isoformat(timespec="seconds"),
                    },
                )
                return
            if not selection_pool:
                reason = "没有达到真实可交易标准的分时数据候选，当前只保留研究候选，不生成真实 Top3。"
                _stamp_decision_timing(market)
                self.database.update_t1_overnight_selection_run(
                    run_id,
                    {
                        "status": "done",
                        "selectionMode": "data_quality_zero_pick",
                        "currentStage": "done",
                        "finalPicks": [],
                        "finalCount": 0,
                        "market": market,
                        "error": reason,
                        "finishedAt": datetime.now().isoformat(timespec="seconds"),
                    },
                )
                return
            if not self._ai_available():
                fallback = _rules_fallback(selection_pool, effective_final_limit)
                fallback = _apply_final_guardrails(fallback, market, effective_final_limit)
                market["baselineGroups"]["final"] = [dict(item) for item in fallback]
                market["shadowComparison"] = _shadow_top3_comparison(fallback, market["baselineGroups"].get("tailSupportShadow") or [])
                _stamp_decision_timing(market)
                self.database.upsert_t1_overnight_selection_items(run_id, fallback, stage="final")
                self.database.update_t1_overnight_selection_run(
                    run_id,
                    {
                        "status": "done",
                        "selectionMode": "rules_fallback",
                        "currentStage": "done",
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
            chunks = _chunks(selection_pool, batch_size)
            for batch_index, chunk in enumerate(chunks, start=1):
                current_batch_pick = _batch_pick_for_size(len(chunk), batch_size, batch_pick)
                self.database.update_t1_overnight_selection_run(
                    run_id,
                    {"currentStage": f"batch:{batch_index}/{len(chunks)}"},
                )
                picks, error = self._select_with_ai(chunk, market, current_batch_pick, stage="batch", batch_index=batch_index)
                selected = _apply_picks(chunk, picks, limit=current_batch_pick, batch_index=batch_index)
                if not selected and error:
                    LOGGER.warning("t1 overnight batch empty index=%s error=%s", batch_index, error)
                semifinal.extend(selected)
                batch_summary.append(
                    {
                        "batchIndex": batch_index,
                        "candidateCount": len(chunk),
                        "targetPickCount": current_batch_pick,
                        "pickCount": len(selected),
                        "error": error,
                        "aiZeroPick": bool(not selected and not error and ((market.get("lastAiZeroPick") or {}).get("stage") == "batch")),
                    }
                )
                self.database.upsert_t1_overnight_selection_items(run_id, selected, stage="semifinal")
                self.database.update_t1_overnight_selection_run(
                    run_id,
                    {
                        "completedBatchCount": batch_index,
                        "semifinalCount": len(_unique_by_code(semifinal)),
                        "batchSummary": batch_summary,
                    },
                )

            semifinal = _unique_by_code(semifinal)
            self.database.update_t1_overnight_selection_run(run_id, {"currentStage": "final", "semifinalCount": len(semifinal)})
            final_picks, final_error = self._select_with_ai(semifinal, market, effective_final_limit, stage="final", batch_index=0)
            final_ai_zero_pick = bool(not final_picks and not final_error and ((market.get("lastAiZeroPick") or {}).get("stage") == "final"))
            final_selected = _apply_picks(semifinal, final_picks, limit=effective_final_limit, batch_index=0)
            selection_mode = "ai_t1_overnight"
            error = final_error
            if not final_selected:
                final_selected = _ai_semifinal_rules_final(semifinal, effective_final_limit, final_error)
                if final_selected:
                    selection_mode = "ai_semifinal_rules_final"
                    error = (
                        "DeepSeek 终选返回 Top0；已保留 AI 批次初筛候选，并在 AI 半决赛池内按本地 T+1 分给出观察结果。"
                        if final_ai_zero_pick
                        else ((final_error + "；") if final_error else "") + "DeepSeek 终选未通过证据校验，已保留 AI 批次初筛候选，并在 AI 半决赛池内按本地 T+1 分终选。"
                    )
                else:
                    final_selected = _rules_fallback(selection_pool, effective_final_limit)
                    selection_mode = "rules_fallback"
                    error = (
                        "DeepSeek 终选返回 Top0，且 AI 半决赛池没有可保留候选；已使用规则兜底观察。"
                        if final_ai_zero_pick
                        else final_error or _summarize_batch_errors(batch_summary) or "AI 未返回有效 T+1 终选结果，已使用规则兜底。"
                    )
            market["baselineGroups"]["aiPreCritic"] = [dict(item) for item in final_selected]
            critic_result = _critic_skipped_result(self.critic_status(), reason="规则兜底结果不执行 Critic。")
            if selection_mode == "ai_t1_overnight":
                self.database.update_t1_overnight_selection_run(run_id, {"currentStage": "critic"})
                critic_result = self._apply_critic_if_enabled(
                    picks=final_selected,
                    semifinal=semifinal,
                    market=market,
                    final_limit=effective_final_limit,
                )
                final_selected = critic_result.get("picks") or final_selected
            elif selection_mode == "ai_semifinal_rules_final":
                critic_result = _critic_skipped_result(
                    self.critic_status(),
                    reason="DeepSeek 终选返回 Top0，当前为 AI 初筛后规则观察，不执行 Critic。"
                    if final_ai_zero_pick
                    else "DeepSeek 终选未通过证据校验，当前为 AI 初筛后规则终选，不执行 Critic。",
                )
            final_selected = _apply_final_guardrails(final_selected, market, effective_final_limit)
            if not final_selected:
                selection_mode = "guardrail_zero_pick"
                error = error or (market.get("recommendationPolicy") or {}).get("reason") or "最终候选未通过市场/数据质量/隔夜风险守门规则，推荐 0 只。"
            tail_shadow_ai = self._select_tail_shadow_with_ai(semifinal or selection_pool, market, effective_final_limit)
            market["shadowDeepSeekTailTop3"] = tail_shadow_ai
            market["baselineGroups"]["sector"] = _baseline_sector_peer_picks(selection_pool, final_selected, effective_final_limit)
            market["baselineGroups"]["final"] = [dict(item) for item in final_selected]
            market["shadowComparison"] = _shadow_top3_comparison(
                final_selected,
                market["baselineGroups"].get("tailSupportShadow") or [],
                deepseek_tail_picks=tail_shadow_ai.get("picks") or [],
            )
            market["criticStatus"] = critic_result.get("status") or self.critic_status()
            critic_meta = {key: value for key, value in critic_result.items() if key != "picks"}
            critic_meta["prePicks"] = [dict(item) for item in market["baselineGroups"].get("aiPreCritic", [])]
            critic_meta["postPicks"] = [dict(item) for item in final_selected]
            critic_meta["changedPicks"] = _pick_codes(critic_meta.get("prePicks") or []) != _pick_codes(final_selected)
            market["critic"] = critic_meta
            _stamp_decision_timing(market)
            self.database.upsert_t1_overnight_selection_items(run_id, final_selected, stage="final")
            self.database.update_t1_overnight_selection_run(
                run_id,
                {
                    "status": "done",
                    "selectionMode": selection_mode,
                    "currentStage": "done",
                    "finalPicks": final_selected,
                    "finalCount": len(final_selected),
                    "market": market,
                    "error": error,
                    "finishedAt": datetime.now().isoformat(timespec="seconds"),
                },
            )
            LOGGER.info("t1 overnight select saved selection_mode=%s top=%s", selection_mode, len(final_selected))
        except Exception as error:  # noqa: BLE001
            try:
                self.database.update_t1_overnight_selection_run(
                    run_id,
                    {
                        "status": "failed",
                        "selectionMode": "ai_failed",
                        "currentStage": "failed",
                        "error": str(error),
                        "finishedAt": datetime.now().isoformat(timespec="seconds"),
                    },
                )
            except Exception as state_error:  # noqa: BLE001 - leave recovery to watchdog.
                LOGGER.error("t1 overnight failure state could not be saved id=%s error=%s", run_id, state_error)
            LOGGER.exception("t1 overnight run failed id=%s error=%s", run_id, error)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1.0)

    def _ai_available(self) -> bool:
        return bool(self.settings.enable_ai and self.settings.ai_api_key and self.settings.ai_base_url)

    def _cached_ai_picks(self, fingerprint: str) -> Optional[List[Dict[str, Any]]]:
        cache = self.database.get_app_setting(AI_RESULT_CACHE_KEY, {"items": {}})
        items = cache.get("items") if isinstance(cache, dict) else {}
        item = items.get(fingerprint) if isinstance(items, dict) else None
        picks = (item or {}).get("picks")
        return picks if isinstance(picks, list) else None

    def _save_ai_picks_cache(self, fingerprint: str, picks: List[Dict[str, Any]], meta: Dict[str, Any]) -> None:
        cache = self.database.get_app_setting(AI_RESULT_CACHE_KEY, {"items": {}})
        items = cache.get("items") if isinstance(cache, dict) else {}
        if not isinstance(items, dict):
            items = {}
        items[fingerprint] = {
            "fingerprint": fingerprint,
            "picks": picks,
            "meta": meta,
            "createdAt": datetime.now().isoformat(timespec="seconds"),
        }
        if len(items) > AI_RESULT_CACHE_LIMIT:
            ordered = sorted(items.items(), key=lambda row: str((row[1] or {}).get("createdAt") or ""))
            items = dict(ordered[-AI_RESULT_CACHE_LIMIT:])
        self.database.set_app_setting(AI_RESULT_CACHE_KEY, {"items": items})

    def _apply_critic_if_enabled(
        self,
        picks: List[Dict[str, Any]],
        semifinal: List[Dict[str, Any]],
        market: Dict[str, Any],
        final_limit: int,
    ) -> Dict[str, Any]:
        status = self.critic_status()
        rule_issues = _rule_critic_issues(picks)
        result = {
            "picks": picks,
            "status": status,
            "enabled": bool(status.get("enabled")),
            "applied": False,
            "issues": rule_issues,
            "replacements": [],
            "reason": status.get("reason", ""),
        }
        if not status.get("enabled"):
            return result
        if status.get("mode") == "rule_only":
            result["applied"] = True
            result["reason"] = "仅规则审查，未调用 DeepSeek Critic。"
            return result
        if int(status.get("maxReplacements") or 0) <= 0:
            result["applied"] = True
            result["reason"] = "已执行规则审查；当前配置最多替换 0 只，不调用 DeepSeek 替换。"
            return result
        if not self._ai_available():
            result["reason"] = "AI 未配置，跳过 DeepSeek Critic。"
            return result

        feedback = self.latest_strategy_feedback()
        critic, error = self._critic_with_ai(
            picks=picks,
            semifinal=semifinal,
            market=market,
            rule_issues=rule_issues,
            feedback=feedback,
            max_replacements=int(status.get("maxReplacements") or 1),
        )
        if error:
            result["reason"] = error
            return result
        updated, replacements = _apply_critic_replacements(
            picks=picks,
            semifinal=semifinal,
            critic=critic,
            max_replacements=int(status.get("maxReplacements") or 1),
            final_limit=final_limit,
        )
        drop_codes = {
            _normalize_code(code)
            for code in (critic.get("dropCodes") or [])
            if _normalize_code(code)
        }
        if drop_codes and status.get("allowReducePickCount", True):
            updated = [item for item in updated if _normalize_code(item.get("code")) not in drop_codes]
        result.update(
            {
                "picks": updated,
                "applied": True,
                "passed": bool(critic.get("passed")) if isinstance(critic, dict) else False,
                "issues": critic.get("issues") or rule_issues,
                "replacements": replacements,
                "droppedCodes": sorted(drop_codes),
                "reason": "" if replacements or drop_codes else "Critic 未要求替换。",
            }
        )
        return result

    def _select_tail_shadow_with_ai(
        self,
        candidates: List[Dict[str, Any]],
        market: Dict[str, Any],
        limit: int,
    ) -> Dict[str, Any]:
        if not self._ai_available():
            return {
                "version": T1_DEEPSEEK_TAIL_SHADOW_VERSION,
                "stage": "shadow",
                "enabled": False,
                "picks": [],
                "error": "AI 未配置，跳过 DeepSeek 尾盘影子终选。",
            }
        shadow_candidates = [_with_tail_shadow_prompt_evidence(item) for item in candidates or []]
        shadow_candidates = [
            item
            for item in shadow_candidates
            if any(str(ref.get("id") or "") == "T1" and str(ref.get("status") or "") == "available" for ref in item.get("evidenceRefs") or [])
        ]
        if not shadow_candidates or limit <= 0:
            return {
                "version": T1_DEEPSEEK_TAIL_SHADOW_VERSION,
                "stage": "shadow",
                "enabled": True,
                "picks": [],
                "error": "没有可用 T1 尾盘证据候选，跳过 DeepSeek 尾盘影子终选。",
            }
        shadow_market = copy.deepcopy(market or {})
        shadow_market["shadowSelectionMode"] = "deepseek_tail_shadow"
        picks, error = self._select_with_ai(shadow_candidates, shadow_market, limit, stage="final", batch_index=-901)
        ai_zero_pick = bool(not picks and not error and ((shadow_market.get("lastAiZeroPick") or {}).get("stage") == "final"))
        selected = _apply_picks(shadow_candidates, picks, limit=limit, batch_index=-901)
        selected = _apply_final_guardrails(selected, market, limit)
        for rank, item in enumerate(selected, start=1):
            item["aiRank"] = rank
            item["rank"] = rank
            item["shadowRank"] = rank
            item["selectionSource"] = "deepseek-tail-shadow"
            item["reason"] = "DeepSeek 尾盘影子终选：额外读取 T1 尾盘承接证据，只做复盘对照，不影响正式 Top3。"
        return {
            "version": T1_DEEPSEEK_TAIL_SHADOW_VERSION,
            "stage": "shadow",
            "enabled": True,
            "affectsOfficialTop3": False,
            "picks": selected,
            "status": "ai_zero_pick" if ai_zero_pick else ("ok" if selected else "failed" if error else "empty"),
            "label": "AI 尾盘影子选择不选" if ai_zero_pick else "DeepSeek 尾盘影子终选",
            "reason": "DeepSeek 尾盘影子返回 Top0，表示它认为没有足够合格的尾盘影子候选。" if ai_zero_pick else "",
            "error": "" if ai_zero_pick else error,
            "aiCallAudits": shadow_market.get("aiCallAudits") or [],
            "aiEvidenceValidationStatus": shadow_market.get("aiEvidenceValidationStatus") or "",
            "aiEvidenceValidationErrors": shadow_market.get("aiEvidenceValidationErrors") or [],
        }

    def _select_with_ai(
        self,
        candidates: List[Dict[str, Any]],
        market: Dict[str, Any],
        limit: int,
        stage: str,
        batch_index: int,
    ) -> tuple[List[Dict[str, Any]], str]:
        if not candidates or limit <= 0:
            return [], ""
        system_content = _role_prompt(stage)
        feedback = self.latest_strategy_feedback() if stage == "final" else {}
        base_user_content = _user_prompt(
            candidates,
            market,
            limit,
            stage,
            feedback,
        )
        fingerprint = _ai_input_fingerprint(
            provider=self.settings.ai_provider,
            model=self.settings.ai_model,
            stage=stage,
            limit=limit,
            system_prompt=system_content,
            user_prompt=base_user_content,
        )
        cached_picks = self._cached_ai_picks(fingerprint)
        if cached_picks is not None:
            cached_validation = _validate_ai_picks_evidence(cached_picks, candidates, stage)
            if not cached_validation.get("valid"):
                partial_cached_validation = _partial_ai_pick_validation(cached_picks, candidates, stage)
                if partial_cached_validation.get("valid"):
                    cached_validation = partial_cached_validation
                    cached_picks = partial_cached_validation.get("picks") or []
            if not cached_validation.get("valid"):
                _record_ai_call_audit(
                    market,
                    {
                        "stage": stage,
                        "batchIndex": batch_index,
                        "provider": self.settings.ai_provider,
                        "model": self.settings.ai_model,
                        "temperature": 0,
                        "limit": limit,
                        "candidateCount": len(candidates),
                        "candidateCodes": [_normalize_code(item.get("code")) for item in candidates[:80]],
                        "fingerprint": fingerprint,
                        "cacheHit": True,
                        "validResponse": False,
                        "evidenceValidation": cached_validation,
                        "error": "缓存 AI 结果未通过当前证据校验，已忽略缓存。",
                        "prompt": base_user_content,
                    },
                )
                cached_picks = None
        if cached_picks is not None:
            _record_ai_call_audit(
                market,
                {
                    "stage": stage,
                    "batchIndex": batch_index,
                    "provider": self.settings.ai_provider,
                    "model": self.settings.ai_model,
                    "temperature": 0,
                    "limit": limit,
                    "candidateCount": len(candidates),
                    "candidateCodes": [_normalize_code(item.get("code")) for item in candidates[:80]],
                    "fingerprint": fingerprint,
                    "cacheHit": True,
                    "validResponse": True,
                    "pickCount": len(cached_picks),
                    "acceptedPickCount": len(cached_picks),
                    "evidenceValidation": cached_validation,
                    "prompt": base_user_content,
                },
            )
            market["aiEvidenceValidationStatus"] = cached_validation.get("status") or "passed"
            market["aiEvidenceValidationErrors"] = []
            if cached_validation.get("errors"):
                market["aiEvidenceValidationErrors"] = cached_validation.get("errors") or []
            if cached_validation.get("status") == "zero_pick":
                market["lastAiZeroPick"] = _ai_zero_pick_meta(stage, batch_index, len(candidates), limit, cached=True)
            return _attach_ai_validation_to_picks(cached_picks, cached_validation), ""
        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": base_user_content},
            ],
            "temperature": 0,
            "max_tokens": T1_MAX_TOKENS,
            "stream": False,
        }
        if self.settings.ai_provider in {"deepseek", "openai"}:
            payload["response_format"] = {"type": "json_object"}
        if self.settings.ai_provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}

        last_error = ""
        validation_feedback = ""
        for attempt in range(1, 3):
            user_content = base_user_content
            current_fingerprint = fingerprint
            if validation_feedback:
                user_content = (
                    f"{base_user_content}\n\n"
                    "上一次输出未通过后端证据校验，请修正后只输出 JSON。"
                    "只输出 picks，每个 pick 只包含 code、rank、supportingEvidenceRefs、counterEvidenceRefs、decisionTags、confidence。"
                    "不要输出 reason/strategyHypothesis/buyPlan/sellPlan/risk/entryCondition/invalidCondition/marketFit/sectorFit/confidenceReason。"
                    "如果不能确认板块状态，就删除 sector_state_supported 标签；如果引用板块状态，supportingEvidenceRefs 必须包含可用的 S2。"
                    "不要写板块资金、主力净流入、资金共振、行业前排等自然语言结论。"
                    f"校验错误：{validation_feedback}"
                )
                current_fingerprint = _ai_input_fingerprint(
                    provider=self.settings.ai_provider,
                    model=self.settings.ai_model,
                    stage=stage,
                    limit=limit,
                    system_prompt=system_content,
                    user_prompt=user_content,
                )
                payload["messages"][1]["content"] = user_content
            started = time.perf_counter()
            LOGGER.info(
                "t1 overnight call start provider=%s model=%s stage=%s batch=%s attempt=%s candidates=%s limit=%s",
                self.settings.ai_provider,
                self.settings.ai_model,
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
                with urllib.request.urlopen(request, timeout=T1_TIMEOUT_SECONDS) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                _record_ai_call_audit(
                    market,
                    {
                        "stage": stage,
                        "batchIndex": batch_index,
                        "attempt": attempt,
                        "provider": self.settings.ai_provider,
                        "model": self.settings.ai_model,
                        "temperature": 0,
                        "limit": limit,
                        "candidateCount": len(candidates),
                        "candidateCodes": [_normalize_code(item.get("code")) for item in candidates[:80]],
                        "fingerprint": current_fingerprint,
                        "cacheHit": False,
                        "validResponse": False,
                        "error": _http_error_message(error),
                        "prompt": user_content,
                    },
                )
                return [], _http_error_message(error)
            except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.IncompleteRead, json.JSONDecodeError) as error:
                last_error = f"{self.settings.ai_provider} T+1 超短线筛选调用失败：{error}"
                LOGGER.warning(
                    "t1 overnight call failed stage=%s batch=%s attempt=%s elapsed_ms=%.0f error=%s",
                    stage,
                    batch_index,
                    attempt,
                    (time.perf_counter() - started) * 1000,
                    error,
                )
                _record_ai_call_audit(
                    market,
                    {
                        "stage": stage,
                        "batchIndex": batch_index,
                        "attempt": attempt,
                        "provider": self.settings.ai_provider,
                        "model": self.settings.ai_model,
                        "temperature": 0,
                        "limit": limit,
                        "candidateCount": len(candidates),
                        "candidateCodes": [_normalize_code(item.get("code")) for item in candidates[:80]],
                        "fingerprint": current_fingerprint,
                        "cacheHit": False,
                        "validResponse": False,
                        "error": last_error,
                        "elapsedMs": round((time.perf_counter() - started) * 1000),
                        "prompt": user_content,
                    },
                )
                continue
            content = _extract_chat_content(data)
            picks = _extract_picks(content)
            validation = _validate_ai_picks_evidence(picks, candidates, stage)
            effective_picks = picks
            effective_validation = validation
            if picks and not validation.get("valid"):
                partial_validation = _partial_ai_pick_validation(picks, candidates, stage)
                if partial_validation.get("valid"):
                    effective_picks = partial_validation.get("picks") or []
                    effective_validation = partial_validation
            LOGGER.info(
                "t1 overnight call ok stage=%s batch=%s attempt=%s elapsed_ms=%.0f picks=%s accepted=%s content_len=%s evidence_valid=%s validation_status=%s",
                stage,
                batch_index,
                attempt,
                (time.perf_counter() - started) * 1000,
                len(picks),
                len(effective_picks),
                len(content),
                effective_validation.get("valid"),
                effective_validation.get("status"),
            )
            _record_ai_call_audit(
                market,
                {
                    "stage": stage,
                    "batchIndex": batch_index,
                    "attempt": attempt,
                    "provider": self.settings.ai_provider,
                    "model": self.settings.ai_model,
                    "temperature": 0,
                    "limit": limit,
                    "candidateCount": len(candidates),
                    "candidateCodes": [_normalize_code(item.get("code")) for item in candidates[:80]],
                    "fingerprint": current_fingerprint,
                    "cacheHit": False,
                    "validResponse": bool(effective_picks) and bool(effective_validation.get("valid")),
                    "pickCount": len(picks),
                    "acceptedPickCount": len(effective_picks),
                    "evidenceValidation": effective_validation,
                    "originalEvidenceValidation": validation if effective_validation is not validation else {},
                    "elapsedMs": round((time.perf_counter() - started) * 1000),
                    "prompt": user_content,
                    "rawResponse": content,
                },
            )
            if effective_validation.get("valid") and effective_validation.get("status") == "zero_pick":
                market["aiEvidenceValidationStatus"] = "zero_pick"
                market["aiEvidenceValidationErrors"] = []
                market["lastAiZeroPick"] = _ai_zero_pick_meta(stage, batch_index, len(candidates), limit, cached=False)
                self._save_ai_picks_cache(
                    current_fingerprint,
                    [],
                    {
                        "stage": stage,
                        "batchIndex": batch_index,
                        "provider": self.settings.ai_provider,
                        "model": self.settings.ai_model,
                        "temperature": 0,
                        "candidateCount": len(candidates),
                        "evidenceValidationVersion": AI_EVIDENCE_VALIDATION_VERSION,
                        "zeroPick": True,
                    },
                )
                return [], ""
            if effective_picks and effective_validation.get("valid"):
                clean_picks = _attach_ai_validation_to_picks(effective_picks, effective_validation)
                market["aiEvidenceValidationStatus"] = effective_validation.get("status") or "passed"
                market["aiEvidenceValidationErrors"] = effective_validation.get("errors") or []
                self._save_ai_picks_cache(
                    current_fingerprint,
                    clean_picks,
                    {
                        "stage": stage,
                        "batchIndex": batch_index,
                        "provider": self.settings.ai_provider,
                        "model": self.settings.ai_model,
                        "temperature": 0,
                        "candidateCount": len(candidates),
                        "evidenceValidationVersion": AI_EVIDENCE_VALIDATION_VERSION,
                    },
                )
                return clean_picks, ""
            if picks:
                last_error = "AI 输出证据校验失败：" + "；".join((validation.get("errors") or [])[:5])
                validation_feedback = last_error
                market["aiEvidenceValidationStatus"] = "retrying" if attempt == 1 else "failed"
                market["aiEvidenceValidationErrors"] = validation.get("errors") or []
                LOGGER.warning(
                    "t1 overnight evidence validation failed stage=%s batch=%s attempt=%s errors=%s",
                    stage,
                    batch_index,
                    attempt,
                    last_error,
                )
                continue
            LOGGER.warning("t1 overnight invalid_json stage=%s batch=%s preview=%s", stage, batch_index, content[:500].replace("\n", " "))
            last_error = f"{self.settings.ai_provider} 返回内容不是有效 T+1 选股 JSON"
        if last_error.startswith("AI 输出证据校验失败"):
            market["aiEvidenceValidationStatus"] = "failed"
        return [], last_error

    def _critic_with_ai(
        self,
        picks: List[Dict[str, Any]],
        semifinal: List[Dict[str, Any]],
        market: Dict[str, Any],
        rule_issues: List[Dict[str, Any]],
        feedback: Dict[str, Any],
        max_replacements: int,
    ) -> tuple[Dict[str, Any], str]:
        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {"role": "system", "content": _critic_role_prompt()},
                {
                    "role": "user",
                    "content": _critic_user_prompt(
                        picks=picks,
                        semifinal=semifinal,
                        market=market,
                        rule_issues=rule_issues,
                        feedback=feedback,
                        max_replacements=max_replacements,
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 2200,
            "stream": False,
        }
        if self.settings.ai_provider in {"deepseek", "openai"}:
            payload["response_format"] = {"type": "json_object"}
        if self.settings.ai_provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}

        request = urllib.request.Request(
            _chat_completions_url(self.settings.ai_base_url),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.ai_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Connection": "close",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=T1_TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return {}, _http_error_message(error)
        except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.IncompleteRead, json.JSONDecodeError) as error:
            return {}, f"{self.settings.ai_provider} Critic 调用失败：{error}"
        content = _extract_chat_content(data)
        critic = _extract_json_object(content)
        if not critic:
            return {}, f"{self.settings.ai_provider} Critic 返回内容不是有效 JSON"
        return critic, ""


def _rank_t1_sector_snapshots(sectors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    clean = []
    for item in sectors:
        if not isinstance(item, dict):
            continue
        sector_type = str(item.get("sectorType") or "").strip()
        sector_code = str(item.get("sectorCode") or item.get("sectorName") or "").strip()
        sector_name = str(item.get("sectorName") or "").strip()
        if sector_type not in {"industry", "concept"} or not sector_code or not sector_name:
            continue
        clean.append(dict(item))
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
        result.append(row)
    return result


def _t1_sector_constituent_targets(sectors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    industries = [item for item in sectors if item.get("sectorType") == "industry"][:T1_EVIDENCE_MAX_INDUSTRY_SECTORS]
    concepts = [item for item in sectors if item.get("sectorType") == "concept"][:T1_EVIDENCE_MAX_CONCEPT_SECTORS]
    return industries + concepts


def _t1_sector_constituent_targets_for_profiles(sectors: List[Dict[str, Any]], profile_maps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not sectors or not profile_maps or T1_EVIDENCE_MAX_CONSTITUENT_TARGET_SECTORS <= 0:
        return []
    wanted: Counter[tuple[str, str]] = Counter()
    for profile in profile_maps:
        industry = _sector_name_key(profile.get("industrySector"))
        if industry:
            wanted[("industry", industry)] += 1
        for concept in profile.get("conceptSectors") or []:
            concept_key = _sector_name_key(concept)
            if concept_key:
                wanted[("concept", concept_key)] += 1
    if not wanted:
        return []
    candidates = []
    for sector in sectors:
        key = (str(sector.get("sectorType") or ""), _sector_name_key(sector.get("sectorName")))
        matched_count = wanted.get(key, 0)
        if matched_count <= 0:
            continue
        row = dict(sector)
        row["candidateMatchCount"] = matched_count
        candidates.append(row)
    candidates = sorted(
        candidates,
        key=lambda row: (
            -int(row.get("candidateMatchCount") or 0),
            int(row.get("rank") or row.get("ruleRank") or 9999),
            -float(row.get("changePct") or 0),
            -float(row.get("turnoverAmount") or 0),
        ),
    )
    return candidates[:T1_EVIDENCE_MAX_CONSTITUENT_TARGET_SECTORS]


def _fetch_t1_sector_constituents(provider: Any, sectors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not sectors:
        return []
    if not T1_ENABLE_THS_CONSTITUENTS:
        original_count = len(sectors)
        sectors = [
            sector
            for sector in sectors
            if not str(sector.get("sectorCode") or "").startswith(("THSI", "THSC"))
        ]
        skipped = original_count - len(sectors)
        if skipped:
            LOGGER.info("t1 evidence skipped ths constituent fetch sectors=%s", skipped)
        if not sectors:
            return []
    rows: List[Dict[str, Any]] = []

    def fetch_one(sector: Dict[str, Any]) -> List[Dict[str, Any]]:
        sector_code = str(sector.get("sectorCode") or "")
        sector_name = str(sector.get("sectorName") or "")
        sector_type = str(sector.get("sectorType") or "")
        identifiers = _unique_strings([sector_code, sector_name])
        raw_items: List[Dict[str, Any]] = []
        last_error: Optional[Exception] = None
        for identifier in identifiers:
            try:
                raw_items = provider.fetch_constituents(
                    identifier,
                    sector_type,
                    limit=_t1_constituent_limit_for_sector(sector_type),
                )
                if raw_items:
                    break
            except Exception as error:  # noqa: BLE001 - retry the sector name when provider codes are unstable.
                last_error = error
                continue
        if not raw_items and last_error:
            raise last_error
        result = []
        for index, item in enumerate(raw_items, start=1):
            code = _normalize_code(item.get("code"))
            if not code:
                continue
            result.append(
                {
                    **dict(item),
                    "code": code,
                    "sectorCode": sector_code or sector_name,
                    "sectorName": sector_name,
                    "sectorType": sector_type,
                    "sectorRank": int(sector.get("rank") or sector.get("ruleRank") or 0),
                    "stockRank": index,
                    "sectorChangePct": _safe_number(sector.get("changePct")),
                    "sectorMainNetInflow": _safe_number(sector.get("mainNetInflow")),
                    "fundFlowStatus": sector.get("fundFlowStatus") or "",
                }
            )
        return result

    executor = ThreadPoolExecutor(max_workers=min(8, max(1, len(sectors))))
    futures = {executor.submit(fetch_one, sector): sector for sector in sectors}
    try:
        for future in as_completed(futures, timeout=T1_EVIDENCE_CONSTITUENT_TIMEOUT_SECONDS):
            sector = futures[future]
            try:
                rows.extend(future.result())
            except Exception as error:  # noqa: BLE001 - keep other sectors available.
                LOGGER.warning(
                    "t1 evidence constituent fetch failed sector=%s type=%s error=%s",
                    sector.get("sectorName"),
                    sector.get("sectorType"),
                    error,
                )
    except FutureTimeoutError:
        LOGGER.warning("t1 evidence constituent fetch timed out sectors=%s", len(sectors))
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    return rows


def _t1_constituent_limit_for_sector(sector_type: str) -> int:
    if str(sector_type or "") == "industry":
        return T1_EVIDENCE_INDUSTRY_CONSTITUENT_LIMIT
    return T1_EVIDENCE_CONCEPT_CONSTITUENT_LIMIT


def _apply_t1_sector_constituent_counts(sectors: List[Dict[str, Any]], constituents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[tuple[str, str], Dict[str, int]] = {}
    for item in constituents:
        key = (str(item.get("sectorType") or ""), str(item.get("sectorCode") or ""))
        bucket = counts.setdefault(key, {"limitUp": 0, "nearLimitUp": 0, "constituents": 0})
        bucket["constituents"] += 1
        status = str(item.get("limitStatus") or "")
        change = _safe_number(item.get("changePct")) or 0
        if status in {"limit_up", "one_word_limit_up"} or change >= 9.8:
            bucket["limitUp"] += 1
        elif status == "near_limit_up" or change >= 8:
            bucket["nearLimitUp"] += 1
    result = []
    for sector in sectors:
        key = (str(sector.get("sectorType") or ""), str(sector.get("sectorCode") or ""))
        bucket = counts.get(key) or {}
        row = dict(sector)
        if bucket:
            row["limitUpCount"] = int(bucket.get("limitUp") or row.get("limitUpCount") or 0)
            row["nearLimitUpCount"] = int(bucket.get("nearLimitUp") or 0)
            row["constituentSampleCount"] = int(bucket.get("constituents") or 0)
        return_status = row.get("fundFlowStatus") or ("ok" if row.get("mainNetInflow") is not None else "missing")
        row["fundFlowStatus"] = return_status
        result.append(row)
    return result


def _with_t1_sector_state_shadow(sectors: List[Dict[str, Any]], market_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    controls = (market_context or {}).get("stageControls") or {}
    if not _stage_control_enabled(controls, "sectorStateShadow", T1_SECTOR_STATE_SHADOW_ENABLED):
        return [dict(item) for item in sectors]
    result = []
    for sector in sectors:
        if not isinstance(sector, dict):
            continue
        row = dict(sector)
        shadow = _classify_t1_sector_state_shadow(row, market_context or {})
        row.update(
            {
                "sectorStateShadow": shadow["state"],
                "sectorStateLabel": shadow["label"],
                "sectorStateConfidence": shadow["confidence"],
                "sectorRiskScore": shadow["riskScore"],
                "sectorFlowInterpretation": shadow["flowInterpretation"],
                "sectorStateEvidenceRefs": shadow["evidenceRefs"],
                "sectorStateCounterRefs": shadow["counterEvidenceRefs"],
                "sectorStateVersion": T1_SECTOR_STATE_SHADOW_VERSION,
                "sectorStateStage": "shadow",
                "sectorStateReasons": shadow["reasons"],
            }
        )
        # Keep a read-only alias for downstream consumers; stage metadata marks it as shadow.
        row.setdefault("sectorState", shadow["state"])
        row.setdefault("sectorConfidence", shadow["confidence"])
        result.append(row)
    return result


def _classify_t1_sector_state_shadow(sector: Dict[str, Any], market_context: Dict[str, Any]) -> Dict[str, Any]:
    sector_type = str(sector.get("sectorType") or "")
    scope = str(sector.get("snapshotScope") or ("candidate_pool" if _sector_source_is_internal(sector.get("source")) else "full_market"))
    source = str(sector.get("source") or "")
    change = _safe_number(sector.get("changePct")) or 0.0
    up_count = int(sector.get("upCount") or 0)
    down_count = int(sector.get("downCount") or 0)
    flat_count = int(sector.get("flatCount") or 0)
    sample_count = _t1_sector_sample_count(sector)
    if sample_count <= 0 and scope != "candidate_pool" and _safe_number(sector.get("changePct")) is not None:
        sample_count = 12
    limit_up = int(sector.get("limitUpCount") or 0)
    near_limit_up = int(sector.get("nearLimitUpCount") or 0)
    limit_down = int(sector.get("limitDownCount") or 0)
    total_count = max(sample_count, up_count + down_count + flat_count, 1)
    up_ratio = up_count / total_count * 100 if total_count else 0.0
    down_ratio = down_count / total_count * 100 if total_count else 0.0
    limit_up_ratio = limit_up / total_count * 100 if total_count else 0.0
    coverage_status = str(sector.get("coverageStatus") or "available")
    rank = int(sector.get("rank") or sector.get("ruleRank") or sector.get("finalRank") or 0)
    fund_flow_status = str(sector.get("fundFlowStatus") or "")
    main_net_inflow = _safe_number(sector.get("mainNetInflow"))
    reasons: List[str] = []

    min_sample = T1_INTERNAL_MIN_SECTOR_SAMPLE_COUNT if scope == "candidate_pool" else 5
    if sector_type not in {"industry", "concept"} or sample_count < min_sample or coverage_status not in {"available", ""}:
        state = "data_insufficient"
        risk = 48.0 if sample_count else 55.0
        confidence = 0.18 if not sample_count else 0.28
        reasons.append(f"板块样本覆盖不足：样本 {sample_count}，范围 {scope or 'unknown'}。")
        return _t1_sector_state_payload(state, risk, confidence, sector, reasons)

    if change <= -2.5 and (down_ratio >= 60 or limit_down > 0):
        state = "retreat"
        risk = 78 + min(14, abs(change) * 2.2) + min(8, limit_down * 1.5)
        reasons.append(f"板块明显下跌且扩散：涨跌幅 {change:.2f}%，下跌占比 {down_ratio:.1f}%。")
    elif change <= -1.2 and down_ratio >= 55:
        state = "distribution"
        risk = 68 + min(10, abs(change) * 2.0)
        reasons.append(f"板块转入派发分歧：涨跌幅 {change:.2f}%，下跌占比 {down_ratio:.1f}%。")
    elif change < -0.2 or down_ratio >= 58:
        state = "weakening"
        risk = 58 + min(10, down_ratio * 0.08)
        reasons.append(f"板块转弱：涨跌幅 {change:.2f}%，下跌占比 {down_ratio:.1f}%。")
    elif change >= 5.0 or (limit_up_ratio >= 12 and change >= 3.0):
        state = "overheated"
        risk = 64 + min(14, change * 1.4) + min(8, limit_up_ratio * 0.4)
        reasons.append(f"板块热度偏高，次日兑现风险上升：涨跌幅 {change:.2f}%，涨停占比 {limit_up_ratio:.1f}%。")
    elif change >= 2.0 and up_ratio >= 60 and (limit_up > 0 or rank <= 12 or near_limit_up > 0):
        state = "strong"
        risk = 36
        reasons.append(f"板块强势延续：涨跌幅 {change:.2f}%，上涨占比 {up_ratio:.1f}%。")
    elif change >= 0.8 and up_ratio >= 55:
        state = "rotation_in"
        risk = 32
        reasons.append(f"板块出现轮入迹象：涨跌幅 {change:.2f}%，上涨占比 {up_ratio:.1f}%。")
    elif 0.1 <= change <= 1.4 and up_ratio >= 52 and limit_up <= max(1, int(total_count * 0.03)):
        state = "oversold_repair"
        risk = 42
        reasons.append(f"板块温和修复但强度未扩散：涨跌幅 {change:.2f}%，上涨占比 {up_ratio:.1f}%。")
    else:
        state = "neutral"
        risk = 45
        reasons.append(f"板块中性分化：涨跌幅 {change:.2f}%，上涨占比 {up_ratio:.1f}%。")

    if scope == "candidate_pool":
        risk += 5
        reasons.append("该状态只来自 T+1 候选池聚合，不能代表全市场行业状态。")
    elif scope == "mixed":
        risk += 2
        reasons.append("该状态混合外部快照与候选池聚合，需按证据范围使用。")
    if fund_flow_status not in {"ok", "available"}:
        reasons.append("主力资金流缺失或不可用，资金方向不单独参与状态定性。")
    elif main_net_inflow is not None:
        reasons.append("主力资金流为行情商推算值，仅作为辅助证据。")

    confidence = _t1_sector_state_confidence(
        state=state,
        scope=scope,
        sample_count=sample_count,
        coverage_status=coverage_status,
        has_fund_flow=fund_flow_status in {"ok", "available"} and main_net_inflow is not None,
    )
    return _t1_sector_state_payload(state, risk, confidence, sector, reasons)


def _t1_sector_state_payload(
    state: str,
    risk: float,
    confidence: float,
    sector: Dict[str, Any],
    reasons: List[str],
) -> Dict[str, Any]:
    flow = _t1_sector_flow_interpretation(sector)
    refs = ["S1"]
    counter_refs = ["S2"] if state in {"weakening", "distribution", "retreat", "overheated", "data_insufficient"} else []
    return {
        "state": state if state in T1_SECTOR_STATES else "data_insufficient",
        "label": T1_SECTOR_STATE_LABELS.get(state, state or "证据不足"),
        "riskScore": round(max(0, min(100, risk)), 1),
        "confidence": round(max(0, min(1, confidence)), 2),
        "flowInterpretation": flow,
        "evidenceRefs": refs,
        "counterEvidenceRefs": counter_refs,
        "reasons": _unique_strings(reasons + ([flow] if flow else [])),
    }


def _t1_sector_sample_count(sector: Dict[str, Any]) -> int:
    values = [
        int(sector.get("sampleCount") or 0),
        int(sector.get("constituentSampleCount") or 0),
        int(sector.get("upCount") or 0) + int(sector.get("downCount") or 0) + int(sector.get("flatCount") or 0),
    ]
    return max(values or [0])


def _t1_sector_state_confidence(
    state: str,
    scope: str,
    sample_count: int,
    coverage_status: str,
    has_fund_flow: bool,
) -> float:
    if state == "data_insufficient":
        return 0.2
    base = 0.54 if scope == "candidate_pool" else 0.72 if scope == "full_market" else 0.64
    if sample_count >= 30:
        base += 0.08
    elif sample_count < 8:
        base -= 0.08
    if coverage_status not in {"available", ""}:
        base -= 0.12
    if has_fund_flow:
        base += 0.04
    return base


def _t1_sector_flow_interpretation(sector: Dict[str, Any]) -> str:
    status = str(sector.get("fundFlowStatus") or "")
    flow = _safe_number(sector.get("mainNetInflow"))
    if status not in {"ok", "available"} or flow is None:
        return "资金流证据不可用，不能据此判断主力共振。"
    if flow > 0:
        return "行情商资金流为正，仅作辅助，不能单独决定板块状态。"
    if flow < 0:
        return "行情商资金流为负，仅作辅助，不能单独决定板块状态。"
    return "行情商资金流接近中性，仅作辅助。"


def _build_t1_sector_state_summary(sectors: List[Dict[str, Any]], stage_controls: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not _stage_control_enabled(stage_controls or {}, "sectorStateShadow", T1_SECTOR_STATE_SHADOW_ENABLED):
        return {"enabled": False, "version": T1_SECTOR_STATE_SHADOW_VERSION}
    counts: Dict[str, int] = {}
    by_type: Dict[str, Dict[str, int]] = {}
    highlights: List[Dict[str, Any]] = []
    for sector in sectors:
        state = str(sector.get("sectorStateShadow") or sector.get("sectorState") or "data_insufficient")
        counts[state] = counts.get(state, 0) + 1
        sector_type = str(sector.get("sectorType") or "unknown")
        type_bucket = by_type.setdefault(sector_type, {})
        type_bucket[state] = type_bucket.get(state, 0) + 1
        if state in {"rotation_in", "strong", "overheated", "distribution", "retreat"}:
            highlights.append(
                {
                    "sectorName": sector.get("sectorName") or "",
                    "sectorType": sector_type,
                    "sectorState": state,
                    "sectorStateLabel": T1_SECTOR_STATE_LABELS.get(state, state),
                    "riskScore": sector.get("sectorRiskScore"),
                    "confidence": sector.get("sectorStateConfidence"),
                    "scope": sector.get("snapshotScope") or "",
                    "changePct": sector.get("changePct"),
                    "sampleCount": _t1_sector_sample_count(sector),
                }
            )
    highlights = sorted(
        highlights,
        key=lambda row: (
            -float(row.get("riskScore") or 0) if row.get("sectorState") in {"overheated", "distribution", "retreat"} else 0,
            -float(row.get("changePct") or 0),
            str(row.get("sectorName") or ""),
        ),
    )[:12]
    return {
        "enabled": True,
        "stage": "shadow",
        "version": T1_SECTOR_STATE_SHADOW_VERSION,
        "counts": counts,
        "byType": by_type,
        "highlights": highlights,
        "note": "板块状态影子：展示和落库；只有在阶段2满足样本门槛并开启后，才会对普通后排有限降权。",
    }


def _build_t1_style_regime_shadow(
    sectors: List[Dict[str, Any]],
    market_context: Dict[str, Any],
    stage_controls: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not _stage_control_enabled(stage_controls or {}, "styleRegimeShadow", T1_STYLE_REGIME_SHADOW_ENABLED):
        return {"enabled": False, "version": T1_STYLE_REGIME_SHADOW_VERSION}
    industries = [item for item in sectors if str(item.get("sectorType") or "") == "industry"]
    if not industries:
        return {
            "enabled": True,
            "stage": "shadow",
            "version": T1_STYLE_REGIME_SHADOW_VERSION,
            "state": "data_insufficient",
            "label": "风格证据不足",
            "confidence": 0.15,
            "reasons": ["缺少行业快照，无法聚合风格轮动。"],
        }
    counts = Counter(str(item.get("sectorStateShadow") or item.get("sectorState") or "data_insufficient") for item in industries)
    retreat_count = counts.get("retreat", 0) + counts.get("distribution", 0) + counts.get("weakening", 0)
    strong_count = counts.get("rotation_in", 0) + counts.get("strong", 0)
    overheated_count = counts.get("overheated", 0)
    total = max(1, len(industries))
    if retreat_count / total >= 0.45:
        state = "broad_retreat"
        label = "行业退潮扩散"
    elif overheated_count / total >= 0.18:
        state = "overheated_realization"
        label = "高热兑现风险"
    elif strong_count / total >= 0.35 and retreat_count / total < 0.30:
        state = "rotation_or_strength"
        label = "轮动或强势扩散"
    else:
        state = "mixed_rotation"
        label = "分化轮动"
    favored = [
        _compact_sector_regime_item(item)
        for item in sorted(industries, key=lambda row: (str(row.get("sectorStateShadow") or "") not in {"rotation_in", "strong"}, -float(row.get("changePct") or 0)))[:8]
        if str(item.get("sectorStateShadow") or item.get("sectorState") or "") in {"rotation_in", "strong"}
    ]
    risk_groups = [
        _compact_sector_regime_item(item)
        for item in sorted(industries, key=lambda row: (str(row.get("sectorStateShadow") or "") not in {"retreat", "distribution", "overheated"}, -float(row.get("sectorRiskScore") or 0)))[:8]
        if str(item.get("sectorStateShadow") or item.get("sectorState") or "") in {"retreat", "distribution", "overheated"}
    ]
    return {
        "enabled": True,
        "stage": "shadow",
        "version": T1_STYLE_REGIME_SHADOW_VERSION,
        "state": state,
        "label": label,
        "confidence": round(min(0.75, 0.35 + min(0.3, total / 120)), 2),
        "counts": dict(counts),
        "marketState": market_context.get("state") or "",
        "favoredGroups": favored,
        "riskGroups": risk_groups,
        "reasons": [
            f"行业状态统计：强/轮入 {strong_count}，转弱/派发/退潮 {retreat_count}，过热 {overheated_count}，共 {total} 个行业。",
            "阶段3影子聚合：未证明独立增益前不进入正式判断。",
        ],
    }


def _compact_sector_regime_item(sector: Dict[str, Any]) -> Dict[str, Any]:
    state = str(sector.get("sectorStateShadow") or sector.get("sectorState") or "data_insufficient")
    return {
        "sectorName": sector.get("sectorName") or "",
        "sectorState": state,
        "sectorStateLabel": T1_SECTOR_STATE_LABELS.get(state, state),
        "riskScore": sector.get("sectorRiskScore"),
        "changePct": sector.get("changePct"),
        "confidence": sector.get("sectorStateConfidence"),
        "scope": sector.get("snapshotScope") or "",
    }


def _sector_name_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def _sector_snapshot_by_type_name(sectors: List[Dict[str, Any]]) -> Dict[tuple[str, str], Dict[str, Any]]:
    result: Dict[tuple[str, str], Dict[str, Any]] = {}
    for sector in sectors:
        sector_type = str(sector.get("sectorType") or "")
        name = _sector_name_key(sector.get("sectorName"))
        if sector_type and name:
            result[(sector_type, name)] = sector
    return result


def _t1_internal_sector_code(sector_type: str, sector_name: str) -> str:
    prefix = "T1I" if sector_type == "industry" else "T1C"
    digest = hashlib.sha1(f"{sector_type}:{_sector_name_key(sector_name)}".encode("utf-8")).hexdigest()[:12].upper()
    return f"{prefix}{digest}"


def _build_t1_internal_sector_snapshots(
    stocks: List[RawStock],
    profile_maps: List[Dict[str, Any]],
    recommendation_date: str,
    scan_time: str,
) -> List[Dict[str, Any]]:
    stocks_by_code = {_normalize_code(stock.code): stock for stock in stocks if _normalize_code(stock.code)}
    grouped: Dict[tuple[str, str], List[RawStock]] = {}
    for profile in profile_maps:
        code = _normalize_code(profile.get("code"))
        stock = stocks_by_code.get(code)
        evidence = profile.get("sectorEvidence") or {}
        layers = _sector_layers_from_evidence(
            evidence,
            str(profile.get("industrySector") or ""),
            list(profile.get("conceptSectors") or []),
        )
        if not stock or not _t1_layer_available(layers, "identity"):
            continue
        identities: List[tuple[str, str]] = []
        industry = str(profile.get("industrySector") or "").strip()
        if industry:
            identities.append(("industry", industry))
        identities.extend(
            ("concept", str(concept).strip())
            for concept in profile.get("conceptSectors") or []
            if str(concept or "").strip()
        )
        seen = set()
        for sector_type, sector_name in identities:
            key = (sector_type, _sector_name_key(sector_name))
            if not key[1] or key in seen:
                continue
            seen.add(key)
            grouped.setdefault(key, []).append(stock)

    snapshots: List[Dict[str, Any]] = []
    snapshot_at = datetime.now().isoformat(timespec="seconds")
    for (sector_type, sector_key), members in grouped.items():
        if not members:
            continue
        changes = [float(stock.change or 0) for stock in members]
        amounts = [max(0.0, float(stock.amount or 0)) for stock in members]
        turnovers = [max(0.0, float(stock.turnover or 0)) for stock in members]
        up_count = len([stock for stock in members if float(stock.change or 0) > 0])
        down_count = len([stock for stock in members if float(stock.change or 0) < 0])
        limit_up_count = len([stock for stock in members if stock.limit_status in {"limit_up", "one_word_limit_up"}])
        near_limit_up_count = len([stock for stock in members if stock.limit_status == "near_limit_up" or 8 <= float(stock.change or 0) < 9.8])
        limit_down_count = len([stock for stock in members if stock.limit_status == "limit_down"])
        leader = max(members, key=lambda stock: (float(stock.change or 0), float(stock.amount or 0), float(stock.turnover or 0)))
        sample_count = len(members)
        coverage_status = "available" if sample_count >= T1_INTERNAL_MIN_SECTOR_SAMPLE_COUNT else "partial"
        snapshots.append(
            {
                "sectorCode": _t1_internal_sector_code(sector_type, sector_key),
                "sectorName": sector_key,
                "sectorType": sector_type,
                "changePct": round(float(median(changes)), 2),
                "turnoverAmount": round(sum(amounts), 2),
                "turnoverRate": round(sum(turnovers) / sample_count, 2) if sample_count else 0.0,
                "upCount": up_count,
                "downCount": down_count,
                "flatCount": max(0, sample_count - up_count - down_count),
                "limitUpCount": limit_up_count,
                "nearLimitUpCount": near_limit_up_count,
                "limitDownCount": limit_down_count,
                "mainNetInflow": None,
                "fundFlowStatus": "unavailable",
                "leaderName": leader.name,
                "leaderCode": _normalize_code(leader.code),
                "sampleCount": sample_count,
                "coverageStatus": coverage_status,
                "snapshotScope": "candidate_pool",
                "snapshotAt": snapshot_at,
                "tradeDate": recommendation_date,
                "scanTime": scan_time,
                "source": T1_INTERNAL_SECTOR_SOURCE,
            }
        )

    per_type_rank: Dict[str, int] = {}
    ranked = sorted(
        snapshots,
        key=lambda row: (
            0 if row.get("coverageStatus") == "available" else 1,
            0 if row.get("sectorType") == "industry" else 1,
            -int(row.get("limitUpCount") or 0),
            -float(row.get("changePct") or 0),
            -float(row.get("turnoverAmount") or 0),
            -int(row.get("sampleCount") or 0),
            str(row.get("sectorName") or ""),
        ),
    )
    for row in ranked:
        sector_type = str(row.get("sectorType") or "")
        per_type_rank[sector_type] = per_type_rank.get(sector_type, 0) + 1
        row["rank"] = per_type_rank[sector_type]
        row["ruleRank"] = per_type_rank[sector_type]
        row["finalRank"] = per_type_rank[sector_type]
    return ranked


def _merge_t1_sector_snapshot_sources(
    internal_sectors: List[Dict[str, Any]],
    external_sectors: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    external_names = {
        (str(row.get("sectorType") or ""), _sector_name_key(row.get("sectorName")))
        for row in external_sectors
        if str(row.get("sectorType") or "") and _sector_name_key(row.get("sectorName"))
    }
    internal_only = [
        row
        for row in internal_sectors
        if (str(row.get("sectorType") or ""), _sector_name_key(row.get("sectorName"))) not in external_names
    ]
    return internal_only + list(external_sectors)


def _tag_t1_external_sector_snapshots(sectors: List[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    tagged = []
    for sector in sectors:
        row = dict(sector)
        row["source"] = row.get("source") or source or "external_sector_snapshot"
        row["snapshotScope"] = row.get("snapshotScope") or "full_market"
        row["coverageStatus"] = row.get("coverageStatus") or "available"
        if row.get("fundFlowStatus") is None:
            row["fundFlowStatus"] = "ok" if row.get("mainNetInflow") is not None else "missing"
        tagged.append(row)
    return tagged


def _t1_sector_sources_label(external_source: str, internal_sectors: List[Dict[str, Any]]) -> str:
    sources = _unique_strings([external_source, T1_INTERNAL_SECTOR_SOURCE if internal_sectors else ""])
    return "+".join(sources)


def _split_sector_sources(source: Any) -> List[str]:
    if isinstance(source, list):
        values = [str(item or "") for item in source]
    else:
        values = re.split(r"\+", str(source or ""))
    return _unique_strings([item.strip() for item in values if item.strip()])


def _sector_source_is_internal(source: Any) -> bool:
    return any(T1_INTERNAL_SECTOR_SOURCE == item or T1_INTERNAL_SECTOR_SOURCE in item for item in _split_sector_sources(source))


def _sector_snapshot_match_is_usable(match: Dict[str, Any]) -> bool:
    return str(match.get("coverageStatus") or "available") == "available"


def _available_sector_snapshot_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        item
        for item in matches
        if str(item.get("sectorType") or "") in {"industry", "concept"}
        and str(item.get("fundFlowStatus") or "") != "identity_only"
        and bool(item.get("sectorCode"))
        and _sector_snapshot_match_is_usable(item)
    ]


def _sector_snapshot_sources(matches: List[Dict[str, Any]], fallback_source: str = "") -> List[str]:
    sources = _unique_strings(
        source
        for item in _available_sector_snapshot_matches(matches)
        for source in _split_sector_sources(item.get("source") or "")
    )
    return sources or _split_sector_sources(fallback_source)


def _sector_snapshot_source_kind(matches: List[Dict[str, Any]], fallback_source: str = "") -> str:
    sources = [
        source
        for value in _sector_snapshot_sources(matches, fallback_source)
        for source in _split_sector_sources(value)
    ]
    internal = [source for source in sources if _sector_source_is_internal(source)]
    external = [source for source in sources if source and not _sector_source_is_internal(source)]
    if internal and external:
        return "mixed"
    if internal:
        return "candidate_pool_aggregate"
    if external:
        return "external_snapshot"
    return "unknown"


def _candidate_sector_snapshot_matches(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence = candidate.get("sectorEvidence") or {}
    layers = _sector_layers_from_evidence(
        evidence,
        str(candidate.get("industrySector") or ""),
        list(candidate.get("conceptSectors") or []),
    )
    if not _t1_layer_available(layers, "sectorSnapshot"):
        return []
    return _available_sector_snapshot_matches(list(evidence.get("matchedSectors") or []))


def _candidate_sector_snapshot_kind(candidate: Dict[str, Any]) -> str:
    evidence = candidate.get("sectorEvidence") or {}
    return _sector_snapshot_source_kind(
        _candidate_sector_snapshot_matches(candidate),
        str((evidence.get("evidenceLayers") or {}).get("sectorSnapshot", {}).get("source") or evidence.get("source") or ""),
    )


def _sector_match_from_snapshot(
    sector: Dict[str, Any],
    evidence_run_id: int,
    recommendation_date: str,
    scan_time: str,
    source: str,
    status: str = "snapshot_only",
    stock_rank: int = 0,
) -> Dict[str, Any]:
    return {
        "sectorCode": sector.get("sectorCode") or sector.get("sectorName") or "",
        "sectorType": sector.get("sectorType") or "",
        "sectorName": sector.get("sectorName") or "",
        "status": status,
        "tradeDate": recommendation_date,
        "scanTime": scan_time,
        "finalRank": int(sector.get("rank") or sector.get("ruleRank") or sector.get("finalRank") or 0),
        "industryRank": int(sector.get("rank") or sector.get("ruleRank") or sector.get("finalRank") or 0),
        "stockRank": int(stock_rank or 0),
        "limitUpCount": int(sector.get("limitUpCount") or 0),
        "activityScore": _safe_number(sector.get("activityScore") or sector.get("changePct")),
        "changePct": _safe_number(sector.get("changePct")),
        "mainNetInflow": _safe_number(sector.get("mainNetInflow")),
        "fundFlowStatus": sector.get("fundFlowStatus") or "",
        "snapshotScope": sector.get("snapshotScope") or "full_market",
        "sampleCount": int(sector.get("sampleCount") or sector.get("constituentSampleCount") or 0),
        "coverageStatus": sector.get("coverageStatus") or "available",
        "sectorStateShadow": sector.get("sectorStateShadow") or sector.get("sectorState") or "data_insufficient",
        "sectorStateLabel": sector.get("sectorStateLabel") or T1_SECTOR_STATE_LABELS.get(str(sector.get("sectorStateShadow") or sector.get("sectorState") or ""), ""),
        "sectorRiskScore": _safe_number(sector.get("sectorRiskScore")),
        "sectorStateConfidence": _safe_number(sector.get("sectorStateConfidence") or sector.get("sectorConfidence")),
        "sectorFlowInterpretation": sector.get("sectorFlowInterpretation") or "",
        "sectorStateVersion": sector.get("sectorStateVersion") or "",
        "sectorStateStage": sector.get("sectorStateStage") or "",
        "sectorStateReasons": list(sector.get("sectorStateReasons") or []),
        "sourceRunId": evidence_run_id,
        "source": sector.get("source") or source or "t1_overnight_evidence",
    }


def _t1_evidence_layers(
    identity_status: str = "unavailable",
    identity_source: str = "",
    sector_snapshot_status: str = "unavailable",
    sector_snapshot_source: str = "",
    constituent_rank_status: str = "unavailable",
    constituent_rank_source: str = "",
) -> Dict[str, Dict[str, Any]]:
    return {
        "identity": {"status": identity_status or "unavailable", "source": identity_source or ""},
        "sectorSnapshot": {"status": sector_snapshot_status or "unavailable", "source": sector_snapshot_source or ""},
        "constituentRank": {"status": constituent_rank_status or "unavailable", "source": constituent_rank_source or ""},
    }


def _t1_layer_status(layers: Dict[str, Any], key: str) -> str:
    layer = layers.get(key) if isinstance(layers, dict) else {}
    if isinstance(layer, dict):
        return str(layer.get("status") or "unavailable")
    return "unavailable"


def _t1_layer_available(layers: Dict[str, Any], key: str) -> bool:
    return _t1_layer_status(layers, key) == "available"


def _sector_layers_from_evidence(evidence: Optional[Dict[str, Any]], industry: str = "", concepts: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    payload = evidence or {}
    layers = payload.get("evidenceLayers")
    if isinstance(layers, dict) and layers:
        return {
            "identity": dict(layers.get("identity") or {"status": "unavailable", "source": ""}),
            "sectorSnapshot": dict(layers.get("sectorSnapshot") or {"status": "unavailable", "source": ""}),
            "constituentRank": dict(layers.get("constituentRank") or {"status": "unavailable", "source": ""}),
        }
    status = str(payload.get("status") or "unavailable")
    has_identity = bool(industry or concepts or payload.get("matchedSectors"))
    source = str(payload.get("source") or "")
    if status == "available":
        return _t1_evidence_layers("available", source, "available", source, "available", source)
    if status == "partial" and has_identity:
        return _t1_evidence_layers("available", source, "unavailable", "", "unavailable", "")
    if status == "stale" and has_identity:
        return _t1_evidence_layers("stale", source, "stale", source, "unavailable", "")
    return _t1_evidence_layers()


def _compat_sector_status_from_layers(layers: Dict[str, Any]) -> str:
    if _t1_layer_available(layers, "constituentRank"):
        return "available"
    if _t1_layer_available(layers, "identity") or _t1_layer_available(layers, "sectorSnapshot"):
        return "partial"
    if any(_t1_layer_status(layers, key) == "stale" for key in ("identity", "sectorSnapshot", "constituentRank")):
        return "stale"
    return "unavailable"


def _sector_allowed_claims(
    layers: Dict[str, Any],
    industry: str,
    concepts: List[str],
    source: str = "",
    matched_sectors: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    claims = ["上市板块仅作交易所分类"]
    if _t1_layer_available(layers, "identity") and (industry or concepts):
        claims.append("所属行业/概念身份")
    if _t1_layer_available(layers, "sectorSnapshot"):
        source_kind = _sector_snapshot_source_kind(matched_sectors or [], source)
        if source_kind == "candidate_pool_aggregate":
            claims.append("T+1候选池聚合板块强度/涨跌/候选池涨停数")
            claims.append("不得写全市场行业涨停数或外部资金流")
        else:
            claims.append("同日板块强度/涨跌/涨停数/资金")
            if source_kind == "mixed":
                claims.append("候选池聚合部分不得写全市场行业涨停数或外部资金流")
    if _t1_layer_available(layers, "constituentRank"):
        claims.append("行业/概念内个股排名")
    if not _t1_layer_available(layers, "sectorSnapshot"):
        claims.append("不得写正向板块强度")
    if not _t1_layer_available(layers, "constituentRank"):
        claims.append("不得写行业前排/个股排名")
    return _unique_strings(claims)


def _sector_evidence_payload(
    *,
    status: str,
    semantic_level: str,
    source: str,
    evidence_run_id: int,
    recommendation_date: str,
    scan_time: str,
    listing_board_proxy: bool,
    matched_sectors: List[Dict[str, Any]],
    layers: Dict[str, Dict[str, Any]],
    allowed_claims: List[str],
    reasons: List[str],
    region_sector: str = "",
) -> Dict[str, Any]:
    snapshot_layer_available = _t1_layer_available(layers, "sectorSnapshot")
    snapshot_matches = _available_sector_snapshot_matches(matched_sectors) if snapshot_layer_available else []
    snapshot_sources = _sector_snapshot_sources(snapshot_matches, source) if snapshot_layer_available else []
    snapshot_kind = _sector_snapshot_source_kind(snapshot_matches, source) if snapshot_layer_available else "unknown"
    internal_snapshot_count = sum(1 for item in snapshot_matches if _sector_source_is_internal(item.get("source")))
    payload = {
        "version": T1_EVIDENCE_VERSION,
        "status": status,
        "semanticLevel": semantic_level,
        "source": source or "t1_overnight_evidence",
        "sourceRunId": evidence_run_id,
        "tradeDate": recommendation_date,
        "scanTime": scan_time,
        "listingBoardProxy": bool(listing_board_proxy),
        "listingBoardIsIndustry": False,
        "matchedSectors": _dedupe_sector_matches(matched_sectors),
        "evidenceLayers": layers,
        "allowedClaims": allowed_claims,
        "sectorSnapshotSources": snapshot_sources,
        "sectorSnapshotSourceKind": snapshot_kind,
        "sectorSnapshotScope": (
            "candidate_pool"
            if snapshot_kind == "candidate_pool_aggregate"
            else "mixed"
            if snapshot_kind == "mixed"
            else "full_market"
            if snapshot_kind == "external_snapshot"
            else "unknown"
        ),
        "internalSectorSnapshotCount": internal_snapshot_count,
        "regionSector": region_sector,
        "reasons": _unique_strings(reasons),
    }
    return payload


def _merge_profile_map_sources(cached_profile_maps: List[Dict[str, Any]], fetched_profile_maps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_code = {_normalize_code(item.get("code")): item for item in cached_profile_maps if _normalize_code(item.get("code"))}
    for item in fetched_profile_maps:
        code = _normalize_code(item.get("code"))
        if code:
            by_code[code] = item
    return list(by_code.values())


def _t1_sector_evidence_coverage(stock_maps: List[Dict[str, Any]]) -> Dict[str, Any]:
    coverage = {
        "total": len(stock_maps),
        "identity": {"available": 0, "stale": 0, "unavailable": 0},
        "sectorSnapshot": {"available": 0, "stale": 0, "unavailable": 0},
        "constituentRank": {"available": 0, "stale": 0, "unavailable": 0},
    }
    for item in stock_maps:
        evidence = item.get("sectorEvidence") or {}
        layers = _sector_layers_from_evidence(evidence, str(item.get("industrySector") or ""), list(item.get("conceptSectors") or []))
        for key in ("identity", "sectorSnapshot", "constituentRank"):
            status = _t1_layer_status(layers, key)
            bucket = coverage[key]
            if status == "available":
                bucket["available"] += 1
            elif status == "stale":
                bucket["stale"] += 1
            else:
                bucket["unavailable"] += 1
    return coverage


def _build_t1_stock_sector_maps(
    stocks: List[RawStock],
    sectors: List[Dict[str, Any]],
    constituents: List[Dict[str, Any]],
    evidence_run_id: int,
    recommendation_date: str,
    scan_time: str,
    source: str,
) -> List[Dict[str, Any]]:
    sector_by_key = {
        (str(item.get("sectorType") or ""), str(item.get("sectorCode") or "")): item
        for item in sectors
    }
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for constituent in constituents:
        code = _normalize_code(constituent.get("code"))
        if not code:
            continue
        sector = sector_by_key.get((str(constituent.get("sectorType") or ""), str(constituent.get("sectorCode") or ""))) or {}
        if not sector:
            sector = {
                "sectorCode": constituent.get("sectorCode") or "",
                "sectorName": constituent.get("sectorName") or "",
                "sectorType": constituent.get("sectorType") or "",
                "rank": constituent.get("sectorRank") or 0,
            }
        match = _sector_match_from_snapshot(
            sector,
            evidence_run_id=evidence_run_id,
            recommendation_date=recommendation_date,
            scan_time=scan_time,
            source=source,
            status="available",
            stock_rank=int(constituent.get("stockRank") or 0),
        )
        grouped.setdefault(code, []).append(match)

    result = []
    has_sector_universe = bool(sectors)
    for stock in stocks:
        code = _normalize_code(stock.code)
        matches = _dedupe_sector_matches(grouped.get(code, []))
        industry_matches = [item for item in matches if item.get("sectorType") == "industry"]
        concept_matches = [item for item in matches if item.get("sectorType") == "concept"]
        identity_available = bool(matches)
        snapshot_available = bool(matches)
        rank_available = any(int(item.get("stockRank") or 0) > 0 for item in matches)
        layers = _t1_evidence_layers(
            identity_status="available" if identity_available else "unavailable",
            identity_source=source,
            sector_snapshot_status="available" if snapshot_available else "unavailable",
            sector_snapshot_source=source,
            constituent_rank_status="available" if rank_available else "unavailable",
            constituent_rank_source=source,
        )
        industry = str((industry_matches[0] or {}).get("sectorName") or "") if industry_matches else ""
        concepts = _unique_strings([item.get("sectorName") for item in concept_matches])
        status = _compat_sector_status_from_layers(layers)
        reasons = (
            ["T+1 独立行业/概念快照已匹配候选股。"]
            if matches
            else ["T+1 独立行业/概念快照已采集，但尚未确认该候选所属行业/概念；不能作为该股正向板块证据。"]
            if has_sector_universe
            else ["T+1 独立行业/概念快照不可用。"]
        )
        allowed_claims = _sector_allowed_claims(layers, industry, concepts, source, matches)
        result.append(
            {
                "code": code,
                "name": stock.name,
                "listingBoard": _listing_board_from_candidate({"code": code, "sector": stock.sector}),
                "industrySector": industry,
                "conceptSectors": concepts,
                "sectorEvidenceStatus": status,
                "sectorEvidence": _sector_evidence_payload(
                    status=status,
                    semantic_level="t1_independent_sector_snapshot" if matches else "t1_sector_identity_unknown" if has_sector_universe else "listing_board_proxy",
                    source=source or "t1_overnight_evidence",
                    evidence_run_id=evidence_run_id,
                    recommendation_date=recommendation_date,
                    scan_time=scan_time,
                    listing_board_proxy=not bool(matches),
                    matched_sectors=matches,
                    layers=layers,
                    allowed_claims=allowed_claims,
                    reasons=reasons,
                ),
            }
        )
    return result


def _profile_map_from_identity(
    profile: Dict[str, Any],
    stock: RawStock,
    evidence_run_id: int,
    recommendation_date: str,
    scan_time: str,
    identity_status: str = "available",
    base_reason: str = "",
) -> Optional[Dict[str, Any]]:
    code = _normalize_code(stock.code or profile.get("code"))
    if not code:
        return None
    industry = str(profile.get("industrySector") or "").strip()
    concepts = _unique_strings(profile.get("conceptSectors") or [])
    if not industry and not concepts:
        return None
    source = str(profile.get("source") or "eastmoney_stock_profile")
    matched = []
    if industry:
        matched.append(
            {
                "sectorCode": "",
                "sectorType": "industry",
                "sectorName": industry,
                "status": identity_status,
                "tradeDate": recommendation_date,
                "scanTime": scan_time,
                "finalRank": 0,
                "industryRank": 0,
                "stockRank": 0,
                "limitUpCount": 0,
                "activityScore": None,
                "changePct": None,
                "mainNetInflow": profile.get("mainNetInflow"),
                "fundFlowStatus": "identity_only",
                "sourceRunId": evidence_run_id,
                "source": source,
            }
        )
    for concept in concepts:
        matched.append(
            {
                "sectorCode": "",
                "sectorType": "concept",
                "sectorName": concept,
                "status": identity_status,
                "tradeDate": recommendation_date,
                "scanTime": scan_time,
                "finalRank": 0,
                "industryRank": 0,
                "stockRank": 0,
                "limitUpCount": 0,
                "activityScore": None,
                "changePct": None,
                "mainNetInflow": None,
                "fundFlowStatus": "identity_only",
                "sourceRunId": evidence_run_id,
                "source": source,
            }
        )
    layers = _t1_evidence_layers(identity_status=identity_status, identity_source=source)
    status = _compat_sector_status_from_layers(layers)
    reasons = [
        "逐股资料已识别真实行业/概念身份。",
        "仅身份层可用时，不能支持今日板块强度、资金共振或行业前排结论。",
    ]
    if identity_status == "stale":
        reasons.append("逐股行业/概念身份缓存已过期，已作为降级证据使用。")
    if base_reason:
        reasons.append(base_reason)
    return {
        "code": code,
        "name": stock.name or profile.get("name") or "",
        "listingBoard": _listing_board_from_candidate({"code": code, "sector": stock.sector}),
        "industrySector": industry,
        "conceptSectors": concepts,
        "sectorEvidenceStatus": status,
        "sectorEvidence": _sector_evidence_payload(
            status=status,
            semantic_level="t1_stock_sector_profile",
            source=source,
            evidence_run_id=evidence_run_id,
            recommendation_date=recommendation_date,
            scan_time=scan_time,
            listing_board_proxy=False,
            matched_sectors=matched,
            layers=layers,
            allowed_claims=_sector_allowed_claims(layers, industry, concepts, source, matched),
            reasons=reasons,
            region_sector=str(profile.get("regionSector") or ""),
        ),
    }


def _t1_identity_cache_is_fresh(row: Dict[str, Any]) -> bool:
    if not row:
        return False
    timestamp = str(row.get("lastSuccessAt") or row.get("updatedAt") or "")
    if not timestamp:
        return False
    try:
        updated = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return (datetime.now() - updated).days <= T1_STOCK_IDENTITY_CACHE_TTL_DAYS


def _profile_maps_from_identity_cache(
    stocks: List[RawStock],
    cached: Dict[str, Dict[str, Any]],
    evidence_run_id: int,
    recommendation_date: str,
    scan_time: str,
) -> List[Dict[str, Any]]:
    rows = []
    for stock in stocks:
        code = _normalize_code(stock.code)
        row = cached.get(code) or {}
        if not row:
            continue
        identity_status = "available" if _t1_identity_cache_is_fresh(row) else "stale"
        profile = {
            **row,
            "source": row.get("source") or "t1_identity_cache",
        }
        mapped = _profile_map_from_identity(
            profile,
            stock,
            evidence_run_id=evidence_run_id,
            recommendation_date=recommendation_date,
            scan_time=scan_time,
            identity_status=identity_status,
        )
        if mapped:
            rows.append(mapped)
    return rows


def _stocks_requiring_identity_fetch(stocks: List[RawStock], cached: Dict[str, Dict[str, Any]]) -> List[RawStock]:
    result = []
    for stock in stocks:
        code = _normalize_code(stock.code)
        row = cached.get(code) or {}
        if not row:
            result.append(stock)
            continue
        has_identity = bool(row.get("industrySector") or row.get("conceptSectors"))
        if not has_identity or not _t1_identity_cache_is_fresh(row):
            result.append(stock)
    return result


def _identity_cache_rows_from_profile_maps(profile_maps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for item in profile_maps:
        evidence = item.get("sectorEvidence") or {}
        layers = evidence.get("evidenceLayers") or {}
        if _t1_layer_status(layers, "identity") != "available":
            continue
        rows.append(
            {
                "code": item.get("code"),
                "name": item.get("name"),
                "industrySector": item.get("industrySector"),
                "conceptSectors": item.get("conceptSectors") or [],
                "regionSector": evidence.get("regionSector") or "",
                "source": evidence.get("source") or "",
                "confidence": 0.86,
                "status": "available",
                "lastSuccessAt": datetime.now().isoformat(timespec="seconds"),
                "raw": {
                    "code": item.get("code"),
                    "name": item.get("name"),
                    "industrySector": item.get("industrySector"),
                    "conceptSectors": item.get("conceptSectors") or [],
                    "regionSector": evidence.get("regionSector") or "",
                    "source": evidence.get("source") or "",
                },
            }
        )
    return rows


def _fetch_t1_stock_sector_profile_maps(
    provider: Any,
    stocks: List[RawStock],
    evidence_run_id: int,
    recommendation_date: str,
    scan_time: str,
    base_reason: str = "",
) -> List[Dict[str, Any]]:
    fetcher = getattr(provider, "fetch_stock_sector_profile", None)
    if not callable(fetcher) or not stocks:
        return []
    rows: List[Dict[str, Any]] = []

    def fetch_one(stock: RawStock) -> Optional[Dict[str, Any]]:
        code = _normalize_code(stock.code)
        if not code:
            return None
        profile = fetcher(code)
        return _profile_map_from_identity(
            profile,
            stock,
            evidence_run_id=evidence_run_id,
            recommendation_date=recommendation_date,
            scan_time=scan_time,
            identity_status="available",
            base_reason=base_reason,
        )

    max_workers = max(1, min(max(1, T1_STOCK_PROFILE_MAX_WORKERS), len(stocks)))
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(fetch_one, stock): stock for stock in stocks}
    try:
        for future in as_completed(futures, timeout=T1_EVIDENCE_STOCK_PROFILE_TIMEOUT_SECONDS):
            stock = futures[future]
            try:
                row = future.result()
            except Exception as error:  # noqa: BLE001
                LOGGER.warning("t1 stock sector profile fetch failed code=%s error=%s", stock.code, error)
                continue
            if row:
                rows.append(row)
    except FutureTimeoutError:
        LOGGER.warning("t1 stock sector profile fetch timed out stocks=%s", len(stocks))
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    return rows


def _attach_sector_snapshot_to_profile_map(
    profile_map: Dict[str, Any],
    sectors: List[Dict[str, Any]],
    evidence_run_id: int,
    recommendation_date: str,
    scan_time: str,
    source: str,
) -> Dict[str, Any]:
    if not sectors:
        return profile_map
    evidence = dict(profile_map.get("sectorEvidence") or {})
    layers = dict(evidence.get("evidenceLayers") or {})
    if not _t1_layer_available(layers, "identity"):
        return profile_map
    sector_by_name = _sector_snapshot_by_type_name(sectors)
    matches = []
    snapshot_matches = 0
    usable_snapshot_matches: List[Dict[str, Any]] = []
    for match in evidence.get("matchedSectors") or []:
        sector_type = str(match.get("sectorType") or "")
        sector_name = _sector_name_key(match.get("sectorName"))
        sector = sector_by_name.get((sector_type, sector_name))
        if sector:
            snapshot_match = _sector_match_from_snapshot(
                sector,
                evidence_run_id=evidence_run_id,
                recommendation_date=recommendation_date,
                scan_time=scan_time,
                source=str(sector.get("source") or source or evidence.get("source") or ""),
                status="snapshot_only",
                stock_rank=0,
            )
            matches.append(snapshot_match)
            if _sector_snapshot_match_is_usable(snapshot_match):
                snapshot_matches += 1
                usable_snapshot_matches.append(snapshot_match)
        else:
            matches.append(match)
    if not snapshot_matches:
        return profile_map
    layers = _t1_evidence_layers(
        identity_status=_t1_layer_status(layers, "identity"),
        identity_source=str((layers.get("identity") or {}).get("source") or evidence.get("source") or ""),
        sector_snapshot_status="available",
        sector_snapshot_source="+".join(_sector_snapshot_sources(usable_snapshot_matches, source or evidence.get("source") or "")),
        constituent_rank_status=_t1_layer_status(layers, "constituentRank"),
        constituent_rank_source=str((layers.get("constituentRank") or {}).get("source") or ""),
    )
    status = _compat_sector_status_from_layers(layers)
    updated = dict(profile_map)
    updated["sectorEvidenceStatus"] = status
    evidence["status"] = status
    evidence["semanticLevel"] = "t1_identity_plus_sector_snapshot"
    evidence["matchedSectors"] = _dedupe_sector_matches(matches)
    evidence["evidenceLayers"] = layers
    evidence["allowedClaims"] = _sector_allowed_claims(
        layers,
        str(updated.get("industrySector") or ""),
        list(updated.get("conceptSectors") or []),
        str(layers.get("sectorSnapshot", {}).get("source") or source or ""),
        usable_snapshot_matches,
    )
    snapshot_kind = _sector_snapshot_source_kind(usable_snapshot_matches, source or evidence.get("source") or "")
    evidence["sectorSnapshotSources"] = _sector_snapshot_sources(usable_snapshot_matches, source or evidence.get("source") or "")
    evidence["sectorSnapshotSourceKind"] = snapshot_kind
    evidence["sectorSnapshotScope"] = "candidate_pool" if snapshot_kind == "candidate_pool_aggregate" else "mixed" if snapshot_kind == "mixed" else "full_market"
    evidence["internalSectorSnapshotCount"] = sum(1 for item in usable_snapshot_matches if _sector_source_is_internal(item.get("source")))
    evidence["reasons"] = _unique_strings(
        list(evidence.get("reasons") or [])
        + [
            "逐股行业/概念身份已与 T+1 同日板块快照按名称匹配；可引用同日板块强度，但不能引用个股行业内排名。"
            if snapshot_kind != "candidate_pool_aggregate"
            else "已匹配 T+1 候选池聚合板块证据；仅反映本次候选池样本，不含外部资金流，也不代表全市场板块统计。"
        ]
    )
    updated["sectorEvidence"] = evidence
    return updated


def _merge_t1_stock_sector_profile_maps(
    base_maps: List[Dict[str, Any]],
    profile_maps: List[Dict[str, Any]],
    sectors: List[Dict[str, Any]],
    evidence_run_id: int,
    recommendation_date: str,
    scan_time: str,
    source: str,
) -> List[Dict[str, Any]]:
    if not profile_maps:
        return base_maps
    profiles = {
        _normalize_code(item.get("code")): _attach_sector_snapshot_to_profile_map(
            item,
            sectors=sectors,
            evidence_run_id=evidence_run_id,
            recommendation_date=recommendation_date,
            scan_time=scan_time,
            source=source,
        )
        for item in profile_maps
        if _normalize_code(item.get("code"))
    }
    result = []
    seen = set()
    for item in base_maps:
        code = _normalize_code(item.get("code"))
        seen.add(code)
        if code not in profiles:
            result.append(item)
            continue
        profile = profiles[code]
        base_evidence = dict(item.get("sectorEvidence") or {})
        profile_evidence = dict(profile.get("sectorEvidence") or {})
        base_layers = base_evidence.get("evidenceLayers") or {}
        profile_layers = profile_evidence.get("evidenceLayers") or {}
        identity_status = "available" if _t1_layer_available(base_layers, "identity") or _t1_layer_available(profile_layers, "identity") else _t1_layer_status(profile_layers, "identity")
        snapshot_status = "available" if _t1_layer_available(base_layers, "sectorSnapshot") or _t1_layer_available(profile_layers, "sectorSnapshot") else "unavailable"
        rank_status = "available" if _t1_layer_available(base_layers, "constituentRank") else "unavailable"
        merged_layers = _t1_evidence_layers(
            identity_status=identity_status,
            identity_source=str(((profile_layers.get("identity") or {}) if isinstance(profile_layers, dict) else {}).get("source") or ((base_layers.get("identity") or {}) if isinstance(base_layers, dict) else {}).get("source") or ""),
            sector_snapshot_status=snapshot_status,
            sector_snapshot_source=str(((base_layers.get("sectorSnapshot") or {}) if isinstance(base_layers, dict) else {}).get("source") or ((profile_layers.get("sectorSnapshot") or {}) if isinstance(profile_layers, dict) else {}).get("source") or source or ""),
            constituent_rank_status=rank_status,
            constituent_rank_source=str(((base_layers.get("constituentRank") or {}) if isinstance(base_layers, dict) else {}).get("source") or ""),
        )
        industry = str(item.get("industrySector") or profile.get("industrySector") or "")
        concepts = _unique_strings(list(item.get("conceptSectors") or []) + list(profile.get("conceptSectors") or []))
        status = _compat_sector_status_from_layers(merged_layers)
        matches = _dedupe_sector_matches(list(base_evidence.get("matchedSectors") or []) + list(profile_evidence.get("matchedSectors") or []))
        merged = {
            **dict(profile),
            "listingBoard": item.get("listingBoard") or profile.get("listingBoard") or "",
            "industrySector": industry,
            "conceptSectors": concepts,
            "sectorEvidenceStatus": status,
        }
        merged["sectorEvidence"] = _sector_evidence_payload(
            status=status,
            semantic_level="t1_constituent_rank_plus_identity" if rank_status == "available" else profile_evidence.get("semanticLevel") or base_evidence.get("semanticLevel") or "t1_stock_sector_profile",
            source=_unique_strings([base_evidence.get("source"), profile_evidence.get("source"), source])[0] if _unique_strings([base_evidence.get("source"), profile_evidence.get("source"), source]) else source,
            evidence_run_id=evidence_run_id,
            recommendation_date=recommendation_date,
            scan_time=scan_time,
            listing_board_proxy=False,
            matched_sectors=matches,
            layers=merged_layers,
            allowed_claims=_sector_allowed_claims(merged_layers, industry, concepts, str(merged_layers.get("sectorSnapshot", {}).get("source") or source or ""), matches),
            reasons=_unique_strings(list(profile_evidence.get("reasons") or []) + list(base_evidence.get("reasons") or [])),
            region_sector=str(profile_evidence.get("regionSector") or base_evidence.get("regionSector") or ""),
        )
        result.append(merged)
    for code, profile in profiles.items():
        if code not in seen:
            result.append(profile)
    return result


def _unavailable_t1_stock_sector_maps(
    stocks: List[RawStock],
    evidence_run_id: int,
    recommendation_date: str,
    scan_time: str,
    reason: str,
) -> List[Dict[str, Any]]:
    result = []
    for stock in stocks:
        code = _normalize_code(stock.code)
        layers = _t1_evidence_layers()
        status = _compat_sector_status_from_layers(layers)
        result.append(
            {
                "code": code,
                "name": stock.name,
                "listingBoard": _listing_board_from_candidate({"code": code, "sector": stock.sector}),
                "industrySector": "",
                "conceptSectors": [],
                "sectorEvidenceStatus": status,
                "sectorEvidence": _sector_evidence_payload(
                    status=status,
                    semantic_level="listing_board_proxy",
                    source="t1_overnight_evidence",
                    evidence_run_id=evidence_run_id,
                    recommendation_date=recommendation_date,
                    scan_time=scan_time,
                    listing_board_proxy=True,
                    matched_sectors=[],
                    layers=layers,
                    allowed_claims=_sector_allowed_claims(layers, "", []),
                    reasons=[reason],
                ),
            }
        )
    return result


def _t1_evidence_quality_status(
    candidate_count: int,
    identity_count: int,
    sector_snapshot_count: int,
    constituent_rank_count: int,
    minute_count: int,
    missing: List[str],
) -> str:
    if candidate_count <= 0:
        return "empty"
    minute_ratio = minute_count / max(1, candidate_count)
    identity_ratio = identity_count / max(1, candidate_count)
    snapshot_ratio = sector_snapshot_count / max(1, candidate_count)
    rank_ratio = constituent_rank_count / max(1, candidate_count)
    if minute_ratio >= 0.9 and identity_ratio >= 0.9 and snapshot_ratio >= 0.55 and rank_ratio >= 0.35:
        return "A"
    if minute_ratio >= 0.75 and identity_ratio >= 0.55 and snapshot_ratio >= 0.25:
        return "B"
    if minute_ratio >= 0.4:
        return "C"
    return "D"


def _t1_evidence_run_is_reusable(
    run: Dict[str, Any],
    requested_count: int,
    requested_minute_count: Optional[int] = None,
) -> bool:
    quality = str(run.get("qualityStatus") or "").upper()
    if quality in {"A", "B"}:
        return True
    if quality != "C":
        return False
    request = run.get("request") or {}
    coverage = request.get("evidenceCoverage") or {}
    candidate_count = max(1, int(request.get("candidateCount") or requested_count or 0))
    minute_candidate_count = max(1, int(requested_minute_count or request.get("minuteCandidateCount") or candidate_count))
    minute_ratio = int(run.get("minuteCount") or 0) / minute_candidate_count
    identity_ratio = int(((coverage.get("identity") or {}).get("available")) or 0) / candidate_count
    return (
        minute_ratio >= T1_REUSABLE_LIMITED_MIN_MINUTE_RATIO
        and identity_ratio >= T1_REUSABLE_LIMITED_MIN_IDENTITY_RATIO
    )


def _t1_sector_maps_have_state_field(sector_maps: Dict[str, Dict[str, Any]]) -> bool:
    if not sector_maps:
        return False
    return all(
        "sectorStateShadow" in item
        or "sectorStateShadow" in (item.get("sectorEvidence") or {})
        for item in sector_maps.values()
        if isinstance(item, dict)
    )


def _t1_evidence_reuse_reason(run: Dict[str, Any]) -> str:
    quality = str(run.get("qualityStatus") or "").upper()
    if quality in {"A", "B"}:
        return "完整/可用冻结证据已满足正式筛选复用条件。"
    if quality == "C":
        return "分钟线与逐股行业/概念身份可支撑有限 AI 参与，复用以避免连续重跑证据抖动。"
    return "同日同时间段冻结证据已复用。"


def _compact_t1_evidence_meta(run: Dict[str, Any], missing: List[str]) -> Dict[str, Any]:
    if not run:
        return {"status": "unavailable", "missing": missing or ["T+1 独立证据 run 不可用"]}
    request = run.get("request") or {}
    market = run.get("market") or {}
    return {
        "version": T1_EVIDENCE_VERSION,
        "evidenceRunId": int(run.get("id") or 0),
        "status": run.get("status") or "",
        "qualityStatus": run.get("qualityStatus") or "",
        "recommendationDate": run.get("recommendationDate") or "",
        "scanTime": run.get("scanTime") or "",
        "actualSource": run.get("actualSource") or "",
        "marketSource": run.get("marketSource") or "",
        "sectorSource": run.get("sectorSource") or "",
        "sectorSourceLabel": _sector_source_label(run.get("sectorSource") or ""),
        "sectorSnapshotMode": _sector_snapshot_mode_from_source(run.get("sectorSource") or ""),
        "minuteSource": run.get("minuteSource") or "",
        "sectorCount": int(run.get("sectorCount") or 0),
        "constituentCount": int(run.get("constituentCount") or 0),
        "stockMapCount": int(run.get("stockMapCount") or 0),
        "minuteCount": int(run.get("minuteCount") or 0),
        "minuteCandidateCount": int(request.get("minuteCandidateCount") or 0),
        "tailMinuteCandidateCount": int(request.get("tailMinuteCandidateCount") or 0),
        "evidenceCoverage": request.get("evidenceCoverage") or {},
        "identityCache": request.get("identityCache") or {},
        "tradeDateStatus": market.get("tradeDateStatus") or {},
        "sectorStateShadowSummary": market.get("sectorStateShadowSummary") or {},
        "styleRegimeShadow": market.get("styleRegimeShadow") or {},
        "stageControls": market.get("stageControls") or _t1_stage_controls_snapshot(),
        "missing": _unique_strings(list(missing or []) + list(run.get("missing") or [])),
        "note": "T+1 使用自己的冻结证据，不再混用早盘雷达 Top 板块成分股。",
    }


def _sector_source_label(source: Any) -> str:
    text = str(source or "")
    if _sector_snapshot_mode_from_source(text) == "mixed":
        return "外部快照+自建聚合"
    if _sector_source_is_internal(text):
        return "自建候选池聚合"
    if text:
        return "外部板块快照"
    return "未获取板块快照"


def _sector_snapshot_mode_from_source(source: Any) -> str:
    text = str(source or "")
    if _sector_source_is_internal(text) and "+" in text:
        return "mixed"
    if _sector_source_is_internal(text):
        return "candidate_pool"
    if text:
        return "external"
    return "unavailable"


def _usable_t1_full_market_freeze(
    payload: Dict[str, Any],
    requested_source: str,
    scan_time: str = DEFAULT_SCAN_TIME,
    allow_stale: bool = False,
) -> bool:
    if not payload:
        return False
    stocks = payload.get("stocks") or []
    if len(stocks) < 1200:
        return False
    meta = market_context_meta(payload)
    if not allow_stale and meta.get("stale"):
        return False
    trade_date = str(meta.get("tradeDate") or "")
    if trade_date != date.today().isoformat():
        return False
    if str(meta.get("status") or "") != "ready":
        return False
    coverage = meta.get("coverage") or {}
    if int(coverage.get("rows") or len(stocks)) < 1200:
        return False
    as_of = str(meta.get("asOfTime") or meta.get("generatedAt") or "")
    if not _snapshot_not_later_than_scan(as_of, trade_date, scan_time):
        return False
    if int(meta.get("ageSeconds") or 0) > T1_FULL_MARKET_FREEZE_REUSE_SECONDS:
        if not allow_stale:
            return False
    requested = str(requested_source or "auto")
    actual = str(meta.get("source") or "")
    return requested == "auto" or requested == actual


def _snapshot_not_later_than_scan(as_of: str, trade_date: str, scan_time: str) -> bool:
    if not as_of:
        return False
    try:
        captured = datetime.fromisoformat(as_of)
        cutoff = datetime.fromisoformat(f"{trade_date}T{_normalize_scan_time(scan_time)}:59")
    except ValueError:
        return False
    return captured <= cutoff


def _ai_participation_mode(
    ai_available: bool,
    data_quality_stats: Dict[str, Any],
    evidence_run: Dict[str, Any],
    market_context: Dict[str, Any],
) -> Dict[str, Any]:
    if not ai_available:
        return {
            "mode": "ai_unavailable",
            "label": "AI 不可用",
            "canCallFinalAI": False,
            "reason": "AI 未配置或接口不可用，系统只能使用规则兜底。",
            "allowedClaims": [],
        }
    total = int(data_quality_stats.get("total") or 0)
    eligible = int(data_quality_stats.get("eligibleForRealTop") or 0)
    sector_counts = data_quality_stats.get("sectorEvidenceCounts") or {}
    layer_counts = data_quality_stats.get("sectorLayerCounts") or {}
    identity_counts = layer_counts.get("identity") or {}
    snapshot_counts = layer_counts.get("sectorSnapshot") or {}
    rank_counts = layer_counts.get("constituentRank") or {}
    identity_available = int(identity_counts.get("available") or 0)
    snapshot_available = int(snapshot_counts.get("available") or 0)
    rank_available = int(rank_counts.get("available") or 0)
    available_sector = int(sector_counts.get("available") or 0)
    partial_sector = int(sector_counts.get("partial") or 0)
    market_complete = bool((market_context.get("indicatorCompleteness") or {}).get("complete"))
    sector_source_kind = _sector_snapshot_mode_from_source(evidence_run.get("sectorSource") or "")
    if total <= 0 or eligible <= 0:
        return {
            "mode": "research_ai",
            "label": "AI 研究模式",
            "canCallFinalAI": False,
            "reason": "没有达到真实可交易标准的分钟线候选，AI 不生成真实 Top。",
            "allowedClaims": ["只能做研究解释", "不得生成真实 Top3"],
        }
    if sector_source_kind == "candidate_pool" and snapshot_available > 0:
        return {
            "mode": "limited_ai_partial_sector",
            "label": "AI 有限参与",
            "canCallFinalAI": True,
            "reason": "真实分钟线、市场和逐股行业/概念身份可用；板块强度来自 T+1 自建候选池聚合，只能引用候选池内强弱，不能写全市场行业统计或外部资金流。",
            "allowedClaims": ["个股量价", "市场状态", "候选池聚合板块强弱", "所属行业/概念身份", "隔夜风险"],
        }
    if snapshot_available >= max(3, int(eligible * 0.4)) and market_complete:
        return {
            "mode": "full_ai",
            "label": "AI 完整参与",
            "canCallFinalAI": True,
            "reason": f"真实分钟线、市场和同日板块强度证据可用；其中 {rank_available} 只有成分股排名可引用行业内位置。",
            "allowedClaims": ["个股量价", "市场状态", "同日板块强度", "有证据候选的行业/概念排名", "隔夜风险"],
        }
    if snapshot_available > 0:
        return {
            "mode": "limited_ai_partial_sector",
            "label": "AI 有限参与",
            "canCallFinalAI": True,
            "reason": "部分候选有同日行业/概念强度证据，AI 只能对对应候选使用板块理由。",
            "allowedClaims": ["个股量价", "市场状态", "有证据候选的同日板块事实", "隔夜风险"],
        }
    if identity_available > 0 or partial_sector > 0 or available_sector > 0:
        return {
            "mode": "limited_ai_partial_sector",
            "label": "AI 有限参与",
            "canCallFinalAI": True,
            "reason": "逐股行业/概念身份可用，但缺少板块强度、涨停数和排名；AI 只能引用所属身份和缺失风险。",
            "allowedClaims": ["个股量价", "市场状态", "所属行业/概念身份", "隔夜风险", "板块强度缺失说明"],
            "missing": list(evidence_run.get("missing") or []),
        }
    return {
        "mode": "limited_ai_no_sector",
        "label": "AI 无板块有限参与",
        "canCallFinalAI": True,
        "reason": "真实分钟线候选可用，但行业/概念证据缺失；AI 只能比较量价、市场和风险，不得写板块强势。",
        "allowedClaims": ["个股量价", "市场状态", "隔夜风险", "板块证据缺失说明"],
        "missing": list(evidence_run.get("missing") or []),
    }


def _annotate_t1_candidate(
    candidate: Dict[str, Any],
    scan_time: str,
    market_context: Optional[Dict[str, Any]] = None,
    sector_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    item = dict(candidate)
    dimensions = item.get("dimensions") or {}
    tradeability = item.get("tradeability") or {}
    change = float(item.get("change") or 0)
    amount_factor = float(item.get("amountFactor") or 0)
    volume_ratio = float(item.get("volumeRatio") or 0)
    rel_strength = float(item.get("relStrength") or 0)
    risk_penalty = float(dimensions.get("riskPenalty") or 0)
    score = float(item.get("score") or 0)
    semantics = _sector_semantics(item, sector_evidence)
    market_context = market_context or {}
    item.update(semantics)

    fund_strength = min(96, amount_factor * 24 + volume_ratio * 13 + max(0, change) * 1.8 + min(12, float(item.get("turnover") or 0) * 0.9))
    sector_strength = _sector_strength_score(item, rel_strength)
    intraday_structure = min(100, score * 0.62 + (10 if item.get("status") in {"buy-triggered", "pullback"} else 0))
    overnight_risk, overnight_risk_status, overnight_risk_breakdown = _overnight_risk_score(
        item=item,
        market_context=market_context,
        tradeability=tradeability,
        risk_penalty=risk_penalty,
        change=change,
        amount_factor=amount_factor,
    )
    sector_state_risk_shadow = _sector_state_risk_shadow_adjustment(item)
    stage_controls = market_context.get("stageControls") or {}
    sector_downgrade_enabled = _stage_control_enabled(stage_controls, "sectorRetreatDowngrade", T1_SECTOR_RETREAT_DOWNGRADE_ENABLED)
    sector_state_downgrade = (
        sector_state_risk_shadow
        if sector_downgrade_enabled and _sector_state_downgrade_applies(item)
        else 0.0
    )
    if sector_state_downgrade:
        overnight_risk_breakdown = list(overnight_risk_breakdown) + [
            {
                "label": "板块退潮有限降权",
                "impact": round(sector_state_downgrade, 1),
                "evidence": f"sectorState={item.get('sectorStateShadow') or 'data_insufficient'}",
            }
        ]
    scored_overnight_risk = min(100, overnight_risk + sector_state_downgrade)
    premium_potential = _premium_potential_score(
        rel_strength=rel_strength,
        change=change,
        amount_factor=amount_factor,
        intraday_structure=intraday_structure,
        sector_strength=sector_strength,
        overnight_risk=scored_overnight_risk,
        market_context=market_context,
        sector_status=_premium_sector_status(item),
    )
    t1_score = (
        fund_strength * 0.30
        + sector_strength * 0.25
        + intraday_structure * 0.20
        + premium_potential * 0.15
        - scored_overnight_risk * 0.10
    )
    if sector_state_downgrade:
        t1_score -= min(4.0, sector_state_downgrade * 0.18)
    if scan_time < "14:30":
        t1_score -= 4
    item["t1Score"] = round(max(0, min(100, t1_score)), 1)
    item["t1Dimensions"] = {
        "fundStrength": round(fund_strength, 1),
        "sectorStrength": round(sector_strength, 1),
        "intradayStructure": round(intraday_structure, 1),
        "premiumPotentialScore": round(premium_potential, 1),
        "premiumPotentialLabel": "次日溢价潜力评分，非概率",
        "tomorrowPremium": round(premium_potential, 1),
        "tomorrowPremiumDeprecated": True,
        "baseOvernightRisk": round(overnight_risk, 1),
        "overnightRisk": round(scored_overnight_risk, 1),
        "overnightRiskStatus": overnight_risk_status,
        "overnightRiskBreakdown": overnight_risk_breakdown,
        "sectorEvidenceStatus": item.get("sectorEvidenceStatus") or "unavailable",
        "sectorStateShadow": item.get("sectorStateShadow") or "data_insufficient",
        "sectorStateLabel": item.get("sectorStateLabel") or T1_SECTOR_STATE_LABELS.get(str(item.get("sectorStateShadow") or ""), "证据不足"),
        "sectorStateConfidence": item.get("sectorStateConfidence"),
        "sectorRiskScore": item.get("sectorRiskScore"),
        "sectorStateRiskShadow": sector_state_risk_shadow,
        "sectorStateDowngrade": round(sector_state_downgrade, 1),
        "overnightRiskWithSectorShadow": round(max(0, min(100, overnight_risk + sector_state_risk_shadow)), 1),
        "sectorStateStage": item.get("sectorStateStage") or "shadow",
        "sectorStateAffectsScore": bool(sector_state_downgrade),
        "sectorStateDowngradeMode": ((stage_controls.get("sectorRetreatDowngrade") or {}).get("mode") or "off"),
    }
    item["evidenceRefs"] = _candidate_evidence_refs(item, market_context)
    risk_profile = _risk_profile(item)
    item["riskProfile"] = risk_profile
    item["riskProfileKey"] = risk_profile["key"]
    item["riskProfileLabel"] = risk_profile["label"]
    candidate_types = _candidate_types(item)
    item["candidateTypes"] = candidate_types
    item["candidateType"] = candidate_types[0]
    item["candidateTypeLabel"] = _candidate_type_label(item["candidateType"])
    item["scanTime"] = scan_time
    return item


def _t1_as_of_contract(requested_scan_time: str, evidence_run: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    requested = str(requested_scan_time or DEFAULT_SCAN_TIME)
    evidence_updated = _time_from_iso_text(str((evidence_run or {}).get("updatedAt") or (evidence_run or {}).get("finishedAt") or ""))
    market_updated = _time_from_iso_text(str((market or {}).get("updatedAt") or ""))
    fallback_time = evidence_updated or market_updated or requested
    market_time = market_updated or fallback_time
    quote_time = fallback_time
    minute_time = fallback_time
    sector_time = evidence_updated or fallback_time
    identity_time = evidence_updated or fallback_time
    return {
        "version": T1_EXECUTION_CONTRACT_VERSION,
        "requestedScanTime": requested,
        "marketDataAsOfTime": market_time,
        "quoteDataAsOfTime": quote_time,
        "minuteDataAsOfTime": minute_time,
        "sectorDataAsOfTime": sector_time,
        "identityDataAsOfTime": identity_time,
        "marketUsableAsOfTime": _min_intraday_time(requested, market_time),
        "quoteUsableAsOfTime": _min_intraday_time(requested, quote_time),
        "minuteUsableAsOfTime": _min_intraday_time(requested, minute_time),
        "sectorUsableAsOfTime": _min_intraday_time(requested, sector_time),
        "identityUsableAsOfTime": _min_intraday_time(requested, identity_time),
        "globalEarliestAsOfTime": _earliest_intraday_time([market_time, quote_time, minute_time, sector_time, identity_time]),
        "globalEarliestOnlyForQualityHint": True,
        "reason": "每类证据独立使用自己的可用截止时间，避免板块数据滞后时粗暴截断个股分钟线。",
    }


def _stamp_decision_timing(market: Dict[str, Any]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    timing = dict((market or {}).get("decisionTiming") or {})
    timing.setdefault("version", T1_EXECUTION_CONTRACT_VERSION)
    timing.setdefault("decisionReadyAt", now)
    timing.setdefault("signalPublishedAt", now)
    timing["signalTime"] = _time_from_iso_text(str(timing.get("signalPublishedAt") or now))
    timing["description"] = "模拟买入只能使用信号发布时间之后的第一个可成交价格。"
    market["decisionTiming"] = timing
    _apply_official_signal_window_guard(market)


def _apply_official_signal_window_guard(market: Dict[str, Any]) -> None:
    timing = dict((market or {}).get("decisionTiming") or {})
    signal_time = _time_from_iso_text(str(timing.get("signalPublishedAt") or timing.get("signalTime") or ""))
    guard = {
        "version": T1_EXECUTION_CONTRACT_VERSION,
        "officialWindow": f"{OFFICIAL_SCAN_START}-{OFFICIAL_SCAN_END}",
        "signalTime": signal_time,
        "officialEligible": not bool(signal_time and signal_time > OFFICIAL_SCAN_END),
        "reason": "",
    }
    if signal_time and signal_time > OFFICIAL_SCAN_END:
        guard["reason"] = f"信号发布时间 {signal_time} 已超过 {OFFICIAL_SCAN_END}，用户无法按 T+1 尾盘计划买入；该 run 只能作为研究/排查，不进入正式优化样本。"
        if str((market or {}).get("runRole") or "") == "official":
            market["runRole"] = "research"
            market["runRoleReviewerNote"] = guard["reason"]
            market["runRoleUpdatedAt"] = datetime.now().isoformat(timespec="seconds")
    market["officialSignalWindowGuard"] = guard


def _run_signal_window_guard(run: Dict[str, Any]) -> Dict[str, Any]:
    market = run.get("market") or {}
    guard = market.get("officialSignalWindowGuard") or {}
    if isinstance(guard, dict) and guard:
        return guard
    timing = market.get("decisionTiming") or {}
    signal_time = _time_from_iso_text(str(timing.get("signalPublishedAt") or timing.get("signalTime") or ""))
    if not signal_time:
        return {"officialEligible": True, "signalTime": "", "reason": ""}
    if signal_time > OFFICIAL_SCAN_END:
        return {
            "version": T1_EXECUTION_CONTRACT_VERSION,
            "officialWindow": f"{OFFICIAL_SCAN_START}-{OFFICIAL_SCAN_END}",
            "signalTime": signal_time,
            "officialEligible": False,
            "reason": f"信号发布时间 {signal_time} 已超过 {OFFICIAL_SCAN_END}，不能作为真实可交易的正式样本。",
        }
    return {"officialEligible": True, "signalTime": signal_time, "reason": ""}


def _time_from_iso_text(text: str) -> str:
    text = str(text or "")
    if len(text) >= 16 and text[10] in {"T", " "}:
        return text[11:16]
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return ""


def _min_intraday_time(left: str, right: str) -> str:
    left = _time_from_iso_text(left) or str(left or "")
    right = _time_from_iso_text(right) or str(right or "")
    if left and right:
        return min(left[:5], right[:5])
    return (left or right)[:5]


def _earliest_intraday_time(values: List[str]) -> str:
    times = [_time_from_iso_text(value) or str(value or "")[:5] for value in values if str(value or "").strip()]
    times = [value for value in times if len(value) >= 5 and value[2] == ":"]
    return min(times) if times else ""


def _p1_shadow_features(
    candidate: Dict[str, Any],
    minute_series: List[Dict[str, Any]],
    requested_scan_time: str,
    market_context: Dict[str, Any],
) -> Dict[str, Any]:
    minute_usable = _min_intraday_time(requested_scan_time, _latest_minute_time(minute_series) or requested_scan_time)
    tail = _tail_support_shadow(candidate, minute_series, minute_usable)
    gap = _gap_down_risk_shadow(candidate, market_context, tail)
    shadow_score = _shadow_t1_score(candidate, tail, gap)
    return {
        "tailSupportShadow": tail,
        "gapDownRiskShadow": gap,
        "shadowT1Score": shadow_score,
        "shadowVersions": {
            "tailSupport": T1_TAIL_SUPPORT_SHADOW_VERSION,
            "gapDownRisk": T1_GAP_DOWN_RISK_SHADOW_VERSION,
            "stage": "shadow",
            "affectsOfficialTop3": False,
        },
    }


def _latest_minute_time(series: List[Dict[str, Any]]) -> str:
    times = [_minute_time(item) for item in series or [] if _minute_time(item)]
    return max(times) if times else ""


def _tail_support_shadow(candidate: Dict[str, Any], minute_series: List[Dict[str, Any]], minute_usable_time: str) -> Dict[str, Any]:
    rows = [item for item in minute_series or [] if (_minute_time(item) or "") <= minute_usable_time]
    rows = sorted(rows, key=lambda item: _minute_time(item) or "")
    tail_rows = [item for item in rows if "14:30" <= (_minute_time(item) or "") <= minute_usable_time]
    if len(tail_rows) < 5:
        return {
            "version": T1_TAIL_SUPPORT_SHADOW_VERSION,
            "stage": "shadow",
            "status": "data_insufficient",
            "label": "尾盘证据不足",
            "score": 50.0,
            "minuteUsableAsOfTime": minute_usable_time,
            "pointCount": len(tail_rows),
            "reasons": ["14:30 后 1m 分钟线不足，暂不能判断尾盘承接。"],
        }
    first_close = _safe_number((tail_rows[0] or {}).get("close") or (tail_rows[0] or {}).get("open"))
    last_close = _safe_number((tail_rows[-1] or {}).get("close"))
    tail_change = _pct_return(last_close or 0, first_close or 0)

    closes = [_safe_number(item.get("close") or item.get("open")) for item in tail_rows]
    closes = [value for value in closes if value and value > 0]
    peak = closes[0] if closes else 0
    max_drawdown = 0.0
    for close in closes[1:]:
        if close > peak:
            peak = close
        drawdown = _pct_return(close, peak) or 0
        if drawdown < max_drawdown:
            max_drawdown = drawdown

    def window_change(count: int) -> Optional[float]:
        window = tail_rows[-count:]
        if len(window) < 2:
            return None
        start = _safe_number(window[0].get("close") or window[0].get("open"))
        end = _safe_number(window[-1].get("close") or window[-1].get("open"))
        return _pct_return(end or 0, start or 0)

    change_3m = window_change(3)
    change_5m = window_change(5)
    change_10m = window_change(10)

    sell_pressure = 0
    longest_sell_pressure = 0
    current_sell_pressure = 0
    down_volume = 0.0
    total_volume = 0.0
    for prev, current in zip(tail_rows, tail_rows[1:]):
        prev_close = _safe_number((prev or {}).get("close"))
        current_close = _safe_number((current or {}).get("close"))
        volume = _safe_number((current or {}).get("volume")) or 0.0
        total_volume += max(0.0, volume)
        if prev_close and current_close and current_close < prev_close:
            sell_pressure += 1
            current_sell_pressure += 1
            down_volume += max(0.0, volume)
        else:
            current_sell_pressure = 0
        longest_sell_pressure = max(longest_sell_pressure, current_sell_pressure)
    sell_pressure_volume_ratio = round(down_volume / total_volume * 100, 1) if total_volume > 0 else None

    volume_values = [_safe_number(item.get("volume")) for item in tail_rows]
    volume_values = [value for value in volume_values if value is not None]
    recent_volume = sum(volume_values[-3:]) / 3 if len(volume_values) >= 3 else 0
    prior_volume = sum(volume_values[:-3]) / max(1, len(volume_values[:-3])) if len(volume_values) > 3 else 0
    volume_rising = bool(prior_volume and recent_volume > prior_volume * 1.25)

    vwap_price = _tail_vwap_price(tail_rows)
    vwap_deviation = _pct_return(last_close or 0, vwap_price or 0)
    below_vwap_indices = [
        index
        for index, item in enumerate(tail_rows)
        if vwap_price and (_safe_number(item.get("close") or item.get("open")) or 0) < vwap_price
    ]
    recovered_vwap = bool(below_vwap_indices and last_close and vwap_price and last_close >= vwap_price)
    vwap_recovery_minutes = len(tail_rows) - 1 - max(below_vwap_indices) if recovered_vwap else None

    lows = [_safe_number(item.get("low") or item.get("close")) for item in tail_rows]
    highs = [_safe_number(item.get("high") or item.get("close")) for item in tail_rows]
    lows = [value for value in lows if value and value > 0]
    highs = [value for value in highs if value and value > 0]
    range_position = None
    if lows and highs and last_close and max(highs) > min(lows):
        range_position = round((last_close - min(lows)) / (max(highs) - min(lows)) * 100, 1)

    score = 50.0
    reasons = []
    if tail_change is not None:
        score += max(-16, min(16, tail_change * 4.5))
        reasons.append(f"14:30后涨跌 {tail_change:.2f}%")
    if vwap_deviation is not None:
        if vwap_deviation >= 0.25:
            score += min(8, vwap_deviation * 3.0)
            reasons.append(f"尾盘价在均价线上方 {vwap_deviation:.2f}%")
        elif vwap_deviation <= -0.25:
            score -= min(12, abs(vwap_deviation) * 4.0)
            reasons.append(f"尾盘价低于均价线 {vwap_deviation:.2f}%")
    if recovered_vwap:
        score += 7
        reasons.append("跌破均价线后已收回")
    if max_drawdown < -1.2:
        score -= min(16, abs(max_drawdown) * 4)
        reasons.append(f"尾盘时序回撤 {max_drawdown:.2f}%")
    if longest_sell_pressure >= 3 or (change_5m is not None and change_5m <= -0.8):
        score -= 16
        reasons.append("最近几分钟卖压连续")
    elif sell_pressure >= 3:
        score -= 8
        reasons.append("尾盘卖压偏多")
    if sell_pressure_volume_ratio is not None and sell_pressure_volume_ratio >= 58:
        score -= min(10, (sell_pressure_volume_ratio - 50) * 0.35)
        reasons.append(f"下跌成交占比 {sell_pressure_volume_ratio:.1f}%")
    if volume_rising and (change_5m or tail_change or 0) <= 0.25:
        score -= 10
        reasons.append("放量但价格承接不足")
    elif volume_rising and (change_5m or tail_change or 0) > 0.6:
        score += 6
        reasons.append("放量上行")
    if range_position is not None and range_position >= 72:
        score += 5
        reasons.append("尾盘收在区间上沿")
    elif range_position is not None and range_position <= 35:
        score -= 6
        reasons.append("尾盘收在区间偏低位置")
    change = float(candidate.get("change") or 0)
    rel_strength = float(candidate.get("relStrength") or 0)
    if change > 7 and (tail_change or 0) <= 0:
        score -= 8
        reasons.append("高位尾盘未继续承接")
    if rel_strength >= 70 and (tail_change or 0) >= 0:
        score += 5
        reasons.append("相对强度仍占优")
    score = round(max(0, min(100, score)), 1)
    if longest_sell_pressure >= 3 and (change_5m or 0) <= -0.5:
        status, label = "late_dump", "尾盘走弱"
    elif (tail_change or 0) > 1.0 and volume_rising and (vwap_deviation or 0) < 0.2:
        status, label = "late_pull_without_support", "尾盘拉升待验证"
    elif score >= 68:
        status, label = "strong_support", "尾盘承接强"
    elif score >= 54:
        status, label = "weak_support", "尾盘承接一般"
    elif score <= 38:
        status, label = "late_dump", "尾盘走弱"
    else:
        status, label = "weak_support", "尾盘承接偏弱"
    return {
        "version": T1_TAIL_SUPPORT_SHADOW_VERSION,
        "stage": "shadow",
        "status": status,
        "label": label,
        "score": score,
        "minuteUsableAsOfTime": minute_usable_time,
        "pointCount": len(tail_rows),
        "tailChangePct": tail_change,
        "tailPullbackPct": max_drawdown,
        "tailMaxDrawdownPct": max_drawdown,
        "change3mPct": change_3m,
        "change5mPct": change_5m,
        "change10mPct": change_10m,
        "vwap": vwap_price,
        "vwapDeviationPct": vwap_deviation,
        "recoveredVwap": recovered_vwap,
        "vwapRecoveryMinutes": vwap_recovery_minutes,
        "consecutiveSellPressureCount": sell_pressure,
        "longestSellPressureCount": longest_sell_pressure,
        "sellPressureVolumeRatio": sell_pressure_volume_ratio,
        "volumeRising": bool(volume_rising),
        "tailRangePositionPct": range_position,
        "reasons": _unique_strings(reasons),
    }


def _tail_vwap_price(rows: List[Dict[str, Any]]) -> Optional[float]:
    amount_sum = 0.0
    volume_sum = 0.0
    weighted_price_sum = 0.0
    weighted_volume_sum = 0.0
    for item in rows or []:
        close = _safe_number(item.get("close") or item.get("open"))
        volume = _safe_number(item.get("volume")) or 0.0
        amount = _safe_number(item.get("amount")) or 0.0
        if amount > 0 and volume > 0:
            amount_sum += amount
            volume_sum += volume
        if close and close > 0 and volume > 0:
            weighted_price_sum += close * volume
            weighted_volume_sum += volume
    amount_vwap = amount_sum / volume_sum if amount_sum > 0 and volume_sum > 0 else None
    fallback_vwap = weighted_price_sum / weighted_volume_sum if weighted_price_sum > 0 and weighted_volume_sum > 0 else None
    last_close = _safe_number((rows[-1] if rows else {}).get("close"))
    if amount_vwap and last_close and 0.5 <= amount_vwap / last_close <= 1.5:
        return round(amount_vwap, 4)
    if fallback_vwap:
        return round(fallback_vwap, 4)
    return None


def _with_tail_shadow_prompt_evidence(candidate: Dict[str, Any]) -> Dict[str, Any]:
    item = copy.deepcopy(candidate or {})
    tail = dict(item.get("tailSupportShadow") or {})
    gap = dict(item.get("gapDownRiskShadow") or {})
    refs = [dict(ref) for ref in item.get("evidenceRefs") or [] if isinstance(ref, dict)]
    refs = [ref for ref in refs if str(ref.get("id") or "") not in {"T1", "G1"}]
    tail_status = str(tail.get("status") or "")
    if tail and tail_status != "data_insufficient":
        refs.append(
            {
                "id": "T1",
                "type": "tail_support_shadow",
                "status": "available",
                "summary": (
                    f"尾盘承接影子 {tail.get('label') or tail_status}，分数 {tail.get('score')}；"
                    f"14:30后 {tail.get('tailChangePct')}%，相对均价线 {tail.get('vwapDeviationPct')}%，"
                    f"最近5分钟 {tail.get('change5mPct')}%，连续卖压 {tail.get('longestSellPressureCount')}。"
                    "这是影子证据，只用于 DeepSeek 尾盘影子终选，不影响正式 Top3。"
                ),
            }
        )
    if gap:
        refs.append(
            {
                "id": "G1",
                "type": "gap_down_risk_shadow",
                "status": "available" if gap.get("status") else "partial",
                "summary": f"低开风险影子 {gap.get('label') or gap.get('status') or '--'}，分数 {gap.get('score')}；目标是观察次日低开超过 -1.5% 的风险。",
            }
        )
    item["evidenceRefs"] = refs
    item["tailSupportPackV2"] = {
        "refId": "T1",
        "version": tail.get("version") or T1_TAIL_SUPPORT_SHADOW_VERSION,
        "stage": "shadow",
        "status": tail.get("status") or "data_insufficient",
        "label": tail.get("label") or "尾盘证据不足",
        "score": tail.get("score"),
        "minuteUsableAsOfTime": tail.get("minuteUsableAsOfTime"),
        "pointCount": tail.get("pointCount"),
        "tailChangePct": tail.get("tailChangePct"),
        "vwapDeviationPct": tail.get("vwapDeviationPct"),
        "recoveredVwap": tail.get("recoveredVwap"),
        "tailMaxDrawdownPct": tail.get("tailMaxDrawdownPct"),
        "change3mPct": tail.get("change3mPct"),
        "change5mPct": tail.get("change5mPct"),
        "change10mPct": tail.get("change10mPct"),
        "longestSellPressureCount": tail.get("longestSellPressureCount"),
        "sellPressureVolumeRatio": tail.get("sellPressureVolumeRatio"),
        "volumeRising": tail.get("volumeRising"),
        "tailRangePositionPct": tail.get("tailRangePositionPct"),
        "reasons": tail.get("reasons") or [],
        "allowedUse": "只能用于影子终选对照；若使用 tail_support，supportingEvidenceRefs 必须包含 T1。",
    }
    item["gapDownRiskPack"] = {
        "refId": "G1",
        "version": gap.get("version") or T1_GAP_DOWN_RISK_SHADOW_VERSION,
        "stage": "shadow",
        "status": gap.get("status") or "",
        "label": gap.get("label") or "",
        "score": gap.get("score"),
        "target": gap.get("target") or "tradeOpenReturnPct <= -1.5%",
        "reasons": gap.get("reasons") or [],
    }
    return item


def _gap_down_risk_shadow(candidate: Dict[str, Any], market_context: Dict[str, Any], tail: Dict[str, Any]) -> Dict[str, Any]:
    dimensions = candidate.get("t1Dimensions") or {}
    overnight = float(dimensions.get("overnightRisk") or 0)
    change = float(candidate.get("change") or 0)
    market_state = str((market_context or {}).get("state") or "")
    sector_state = str(candidate.get("sectorStateShadow") or "data_insufficient")
    score = overnight * 0.45
    reasons = [f"现有隔夜风险 {overnight:.1f}"]
    if market_state in {"heavy_down", "panic"}:
        score += 18
        reasons.append("市场弱势/恐慌")
    elif market_state in {"mild_down", "mixed"}:
        score += 8
        reasons.append("市场偏弱或分化")
    if sector_state in {"distribution", "retreat", "weakening"}:
        score += 12
        reasons.append("所属板块影子状态偏退潮")
    if str((tail or {}).get("status") or "") in {"late_dump", "late_pull_without_support"}:
        score += 12
        reasons.append("尾盘承接影子偏弱")
    if change >= 8:
        score += 8
        reasons.append("当日涨幅较高，次日兑现压力更大")
    score = round(max(0, min(100, score)), 1)
    if score >= 72:
        status, label = "high", "低开风险高"
    elif score >= 50:
        status, label = "medium", "低开风险中"
    else:
        status, label = "low", "低开风险低"
    return {
        "version": T1_GAP_DOWN_RISK_SHADOW_VERSION,
        "stage": "shadow",
        "status": status,
        "label": label,
        "score": score,
        "target": "tradeOpenReturnPct <= -1.5%",
        "marketGapPctOnlyForStructure": True,
        "reasons": _unique_strings(reasons),
    }


def _shadow_t1_score(candidate: Dict[str, Any], tail: Dict[str, Any], gap: Dict[str, Any]) -> float:
    base = float(candidate.get("t1Score") or candidate.get("score") or 0)
    tail_score = float((tail or {}).get("score") or 50)
    gap_score = float((gap or {}).get("score") or 50)
    adjusted = base + (tail_score - 50) * 0.16 - max(0, gap_score - 45) * 0.10
    return round(max(0, min(100, adjusted)), 1)


def _tail_support_shadow_top_picks(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    eligible = [item for item in candidates or [] if (item.get("dataQuality") or {}).get("eligibleForRealTop", True)]
    ranked = sorted(eligible, key=lambda item: float(item.get("shadowT1Score") or item.get("t1Score") or item.get("score") or 0), reverse=True)
    result = []
    for rank, item in enumerate(ranked[: max(0, int(limit or 0))], start=1):
        row = dict(item)
        row["aiRank"] = rank
        row["rank"] = rank
        row["selected"] = True
        row["selectionSource"] = "tail-support-shadow"
        row["reason"] = "影子对照：在原 T+1 分基础上观察尾盘承接和低开风险，不影响正式 Top3。"
        row["shadowRank"] = rank
        result.append(row)
    return result


def _shadow_top3_comparison(
    final_picks: List[Dict[str, Any]],
    shadow_picks: List[Dict[str, Any]],
    deepseek_tail_picks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    final_codes = _pick_codes(final_picks)
    shadow_codes = _pick_codes(shadow_picks)
    deepseek_tail_codes = _pick_codes(deepseek_tail_picks or [])
    overlap = [code for code in final_codes if code in shadow_codes]
    deepseek_overlap = [code for code in final_codes if code in deepseek_tail_codes]
    return {
        "version": T1_TAIL_SUPPORT_SHADOW_VERSION,
        "stage": "shadow",
        "label": "正式Top3 vs 尾盘影子Top3",
        "affectsOfficialTop3": False,
        "finalCodes": final_codes,
        "shadowCodes": shadow_codes,
        "deepSeekTailCodes": deepseek_tail_codes,
        "overlapCodes": overlap,
        "deepSeekTailOverlapCodes": deepseek_overlap,
        "changedCount": max(len(final_codes), len(shadow_codes)) - len(overlap),
        "deepSeekTailChangedCount": max(len(final_codes), len(deepseek_tail_codes)) - len(deepseek_overlap),
        "reason": "影子 Top3 用来观察尾盘承接、低开风险和 DeepSeek 尾盘证据是否能改善结果，当前不参与正式选股。",
    }


def _sector_strength_score(candidate: Dict[str, Any], rel_strength: float) -> float:
    status = str(candidate.get("sectorEvidenceStatus") or "unavailable")
    evidence = candidate.get("sectorEvidence") or {}
    layers = _sector_layers_from_evidence(evidence, str(candidate.get("industrySector") or ""), list(candidate.get("conceptSectors") or []))
    if _t1_layer_available(layers, "sectorSnapshot"):
        rank = int(candidate.get("industryRank") or 0)
        limit_ups = int(candidate.get("industryLimitUpCount") or 0)
        concept_limit_ups = int(candidate.get("conceptLimitUpCount") or 0)
        matches = _candidate_sector_snapshot_matches(candidate)
        source_kind = _candidate_sector_snapshot_kind(candidate)
        activity = max([float(item.get("activityScore") or 0) for item in matches] or [0])
        flow_score = max(
            [
                8 if float(item.get("mainNetInflow") or 0) > 0 and item.get("fundFlowStatus") == "ok" else 0
                for item in matches
            ]
            or [0]
        )
        if source_kind == "candidate_pool_aggregate":
            sample_counts = [int(item.get("sampleCount") or 0) for item in matches if int(item.get("sampleCount") or 0) > 0]
            sample_bonus = min(10, max(sample_counts or [0]) * 1.6)
            rank_score = 0
            flow_score = 0
            score = 20 + sample_bonus + min(18, limit_ups * 3.2) + min(10, concept_limit_ups * 2.0) + min(14, activity * 0.16)
            return round(min(T1_INTERNAL_SECTOR_STRENGTH_CAP, max(0, score)), 1)
        rank_score = max(0, 30 - max(0, rank - 1) * 4) if rank else 8
        score = 22 + rank_score + min(20, limit_ups * 4) + min(12, concept_limit_ups * 2.5) + min(16, activity * 0.18) + flow_score
        if source_kind == "mixed":
            score = min(score, max(T1_INTERNAL_SECTOR_STRENGTH_CAP + 4, 72))
        if not _t1_layer_available(layers, "constituentRank"):
            score = min(score, 74)
        return round(min(94, max(0, score)), 1)
    if _t1_layer_available(layers, "identity"):
        score = 24 + rel_strength * 0.10
        return round(min(52, max(0, score)), 1)
    if status == "stale":
        score = 12 + rel_strength * 0.08
        return round(min(42, max(0, score)), 1)
    score = 8 + rel_strength * 0.06
    return round(min(34, max(0, score)), 1)


def _premium_sector_status(candidate: Dict[str, Any]) -> str:
    layers = _sector_layers_from_evidence(candidate.get("sectorEvidence") or {}, str(candidate.get("industrySector") or ""), list(candidate.get("conceptSectors") or []))
    if _t1_layer_available(layers, "sectorSnapshot"):
        if _candidate_sector_snapshot_kind(candidate) == "candidate_pool_aggregate":
            return "candidate_pool_snapshot"
        return "snapshot"
    if _t1_layer_available(layers, "identity"):
        return "identity"
    return str(candidate.get("sectorEvidenceStatus") or "unavailable")


def _sector_state_risk_shadow_adjustment(candidate: Dict[str, Any]) -> float:
    state = str(candidate.get("sectorStateShadow") or "data_insufficient")
    confidence = _safe_number(candidate.get("sectorStateConfidence"))
    confidence_factor = max(0.35, min(1.0, confidence if confidence is not None else 0.35))
    base = {
        "retreat": 22,
        "distribution": 16,
        "weakening": 10,
        "overheated": 12,
        "data_insufficient": 8,
        "oversold_repair": 4,
        "neutral": 0,
        "rotation_in": -3,
        "strong": -2,
    }.get(state, 6)
    return round(base * confidence_factor, 1)


def _sector_state_downgrade_applies(candidate: Dict[str, Any]) -> bool:
    state = str(candidate.get("sectorStateShadow") or "data_insufficient")
    if state not in {"retreat", "distribution", "overheated", "weakening"}:
        return False
    if bool(candidate.get("sectorExceptionEligible")):
        return False
    industry_rank = int(candidate.get("stockRankInIndustry") or 99)
    rel_strength = float(candidate.get("relStrength") or 0)
    amount_factor = float(candidate.get("amountFactor") or 0)
    tradeability = candidate.get("tradeability") or {}
    if industry_rank <= 3 and rel_strength >= 86:
        return False
    if rel_strength >= 92 and amount_factor >= 1.35 and tradeability.get("level") != "hard-to-buy":
        return False
    return True


def _overnight_risk_score(
    item: Dict[str, Any],
    market_context: Dict[str, Any],
    tradeability: Dict[str, Any],
    risk_penalty: float,
    change: float,
    amount_factor: float,
) -> tuple[float, str, List[Dict[str, Any]]]:
    risk = risk_penalty * 2.4 + max(0, change - 6) * 4.2
    breakdown: List[Dict[str, Any]] = []

    def add(label: str, impact: float, evidence: str = "") -> None:
        nonlocal risk
        if impact <= 0:
            return
        risk += impact
        breakdown.append({"label": label, "impact": round(impact, 1), "evidence": evidence})

    level = str(tradeability.get("level") or "")
    if level in {"chase-risk", "hard-to-buy"}:
        add("买入追高或排队风险", 16 if level == "chase-risk" else 22, level)
    elif level in {"not-buyable", "hard-to-sell"}:
        add("可交易性不足", 28, level)

    state = str(market_context.get("state") or "unknown")
    metrics = market_context.get("metrics") or {}
    if state == "panic":
        add("市场恐慌退潮", 42, "marketContext=panic")
    elif state == "heavy_down":
        add("市场放量大跌", 28, "marketContext=heavy_down")
    elif state == "mild_down":
        add("市场偏弱", 14, "marketContext=mild_down")
    elif state == "euphoric":
        add("情绪高潮次日兑现风险", 10, "marketContext=euphoric")
    elif state == "unknown":
        add("市场环境证据不足", 18, "marketContext=unknown")

    down_ratio = _safe_number(metrics.get("downRatioPct"))
    if down_ratio is not None and down_ratio >= 70:
        add("下跌家数扩散", min(16, (down_ratio - 60) * 0.55), f"{down_ratio:.1f}%")
    limit_down = int(metrics.get("limitDown") or 0)
    near_limit_down = int(metrics.get("nearLimitDown") or 0)
    if limit_down >= 40:
        add("跌停扩散", min(18, limit_down * 0.15), str(limit_down))
    if near_limit_down >= 80:
        add("接近跌停扩散", min(12, near_limit_down * 0.04), str(near_limit_down))
    late_session = _safe_number(metrics.get("lateSessionChangePct"))
    if late_session is not None and late_session <= -0.35:
        add("14:30 后市场继续走弱", min(12, abs(late_session) * 8), f"{late_session:.2f}%")

    sector_status = str(item.get("sectorEvidenceStatus") or "unavailable")
    sector_layers = _sector_layers_from_evidence(item.get("sectorEvidence") or {}, str(item.get("industrySector") or ""), list(item.get("conceptSectors") or []))
    if not _t1_layer_available(sector_layers, "identity"):
        add("真实行业/概念身份缺失", 12, "sector.identity=unavailable")
    elif not _t1_layer_available(sector_layers, "sectorSnapshot"):
        add("同日板块强度证据缺失", 7, "sector.snapshot=unavailable")
    elif not _t1_layer_available(sector_layers, "constituentRank"):
        add("成分股排名证据缺失", 3, "sector.rank=unavailable")
    if _t1_layer_available(sector_layers, "sectorSnapshot"):
        source_kind = _candidate_sector_snapshot_kind(item)
        if source_kind == "candidate_pool_aggregate":
            add("板块证据仅来自候选池聚合", 6, "sector.scope=candidate_pool")
        elif source_kind == "mixed":
            add("部分板块证据来自候选池聚合", 3, "sector.scope=mixed")
        if _t1_layer_available(sector_layers, "constituentRank") and int(item.get("industryLimitUpCount") or 0) <= 0 and int(item.get("conceptLimitUpCount") or 0) <= 0:
            add("真实行业/概念涨停扩散不足", 4, "limitUpCount=0")

    if change >= 7.5 and amount_factor >= 1.6:
        add("高位放量隔夜兑现", 10, f"change={change:.2f}, amountFactor={amount_factor:.2f}")
    elif change >= 6:
        add("涨幅偏高次日低开风险", 6, f"change={change:.2f}")

    missing_market = list((market_context.get("indicatorCompleteness") or {}).get("missing") or [])
    if missing_market:
        add("市场指标不完整", min(12, 4 + len(missing_market) * 2), "；".join(missing_market[:3]))

    status = _overnight_risk_status(
        state=state,
        metrics=metrics,
        missing_market=missing_market,
        sector_status=sector_status,
        sector_layers=sector_layers,
    )
    return round(max(0, min(100, risk)), 1), status, breakdown[:10]


def _overnight_risk_status(
    state: str,
    metrics: Dict[str, Any],
    missing_market: List[str],
    sector_status: str,
    sector_layers: Dict[str, Any],
) -> str:
    market_state_known = str(state or "unknown") != "unknown"
    market_has_core_metrics = any(
        _safe_number(metrics.get(key)) is not None
        for key in (
            "downRatioPct",
            "limitDown",
            "nearLimitDown",
            "mainIndexChangePct",
            "averageIndexChangePct",
            "turnoverRatio5d",
        )
    )
    sector_identity_ok = _t1_layer_available(sector_layers, "identity")
    sector_snapshot_ok = _t1_layer_available(sector_layers, "sectorSnapshot")
    sector_missing = str(sector_status or "") in {"unavailable", "stale"} or not sector_identity_ok

    if not market_state_known and not market_has_core_metrics and sector_missing:
        return "unknown"
    if missing_market or not sector_snapshot_ok or str(sector_status or "") in {"unavailable", "stale"}:
        return "partial"
    return "estimated"


def _overnight_risk_status_label(status: Any) -> str:
    return {
        "estimated": "完整估算",
        "partial": "部分估算",
        "unknown": "证据不足",
    }.get(str(status or ""), str(status or "证据不足"))


def _premium_potential_score(
    rel_strength: float,
    change: float,
    amount_factor: float,
    intraday_structure: float,
    sector_strength: float,
    overnight_risk: float,
    market_context: Dict[str, Any],
    sector_status: str,
) -> float:
    score = (
        rel_strength * 0.24
        + max(0, 8 - abs(change - 3.8)) * 4.0
        + min(18, amount_factor * 6.5)
        + intraday_structure * 0.16
        + sector_strength * 0.14
        - overnight_risk * 0.18
    )
    state = str(market_context.get("state") or "unknown")
    score += {
        "trend_up": 6,
        "mild_up": 4,
        "mixed": 0,
        "mild_down": -8,
        "heavy_down": -18,
        "panic": -35,
        "euphoric": -6,
        "unknown": -10,
    }.get(state, -4)
    score += {
        "available": 4,
        "snapshot": 3,
        "candidate_pool_snapshot": 1,
        "identity": -4,
        "partial": -6,
        "stale": -8,
        "unavailable": -12,
    }.get(sector_status, -8)
    return round(max(0, min(96, score)), 1)


def _candidate_evidence_refs(candidate: Dict[str, Any], market_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    refs = [
        {
            "id": "Q1",
            "type": "quote",
            "status": "available",
            "summary": f"实时快照：涨跌幅 {candidate.get('change')}%，量比 {candidate.get('volumeRatio')}，成交额强度 {candidate.get('amountFactor')}。",
        },
        {
            "id": "M1",
            "type": "market",
            "status": "available" if (market_context.get("indicatorCompleteness") or {}).get("complete") else "partial",
            "summary": f"市场状态 {market_context.get('label') or market_context.get('state') or '--'}；{';'.join(market_context.get('reasons') or [])}",
        },
        {
            "id": "B1",
            "type": "listing_board",
            "status": "available",
            "summary": f"上市板块 {candidate.get('listingBoard') or '--'}，涨停数 {candidate.get('marketBoardLimitUpCount') or 0}；这不是行业/概念证据。",
        },
    ]
    sector_status = str(candidate.get("sectorEvidenceStatus") or "unavailable")
    refs.append(
        {
            "id": "S1",
            "type": "sector",
            "status": sector_status,
            "summary": _sector_evidence_summary(candidate),
        }
    )
    refs.append(
        {
            "id": "S2",
            "type": "sector_state_shadow",
            "status": "available" if candidate.get("sectorStateShadow") and candidate.get("sectorStateShadow") != "data_insufficient" else "unavailable",
            "summary": _sector_state_evidence_summary(candidate),
        }
    )
    refs.append(
        {
            "id": "R1",
            "type": "risk",
            "status": (candidate.get("t1Dimensions") or {}).get("overnightRiskStatus") or "estimated",
            "summary": f"隔夜风险 {(candidate.get('t1Dimensions') or {}).get('overnightRisk')}，风险项：{';'.join(item.get('label', '') for item in ((candidate.get('t1Dimensions') or {}).get('overnightRiskBreakdown') or [])[:4])}",
        }
    )
    return refs


def _sector_evidence_summary(candidate: Dict[str, Any]) -> str:
    status = str(candidate.get("sectorEvidenceStatus") or "unavailable")
    industry = str(candidate.get("industrySector") or "")
    concepts = ", ".join(candidate.get("conceptSectors") or [])
    evidence = candidate.get("sectorEvidence") or {}
    layers = _sector_layers_from_evidence(evidence, industry, list(candidate.get("conceptSectors") or []))
    allowed = "；".join(evidence.get("allowedClaims") or [])
    snapshot_kind = _candidate_sector_snapshot_kind(candidate)
    snapshot_matches = _candidate_sector_snapshot_matches(candidate)
    industry_sample_count = int(candidate.get("industrySectorSampleCount") or 0)
    max_sample_count = max([int(item.get("sampleCount") or 0) for item in snapshot_matches] or [industry_sample_count or 0])
    if _t1_layer_available(layers, "sectorSnapshot") and snapshot_kind == "candidate_pool_aggregate":
        return (
            f"T+1 自建候选池聚合板块证据：行业 {industry or '--'}，概念 {concepts or '--'}，"
            f"候选池内样本 {max_sample_count or '--'}，候选池内行业涨停 {candidate.get('industryLimitUpCount') or 0}，"
            f"聚合排序参考 {candidate.get('industryRank') or '--'}。不含外部资金流，不代表全市场行业统计；允许：{allowed}"
        )
    if _t1_layer_available(layers, "sectorSnapshot") and snapshot_kind == "mixed":
        return (
            f"同日板块证据混合外部快照与候选池聚合：行业 {industry or '--'}，概念 {concepts or '--'}，"
            f"可用样本 {max_sample_count or '--'}，涨停数需按 allowedClaims 区分；允许：{allowed}"
        )
    if _t1_layer_available(layers, "constituentRank"):
        return (
            f"同日行业/概念证据完整：行业 {industry or '--'}，概念 {concepts or '--'}，"
            f"行业涨停 {candidate.get('industryLimitUpCount') or 0}，行业排名 {candidate.get('industryRank') or '--'}，"
            f"行业内个股排名 {candidate.get('stockRankInIndustry') or '--'}。允许：{allowed}"
        )
    if _t1_layer_available(layers, "sectorSnapshot"):
        return (
            f"同日板块强度可用但成分股排名缺失：行业 {industry or '--'}，概念 {concepts or '--'}，"
            f"行业涨停 {candidate.get('industryLimitUpCount') or 0}，行业排名 {candidate.get('industryRank') or '--'}；"
            f"不能说行业前排。允许：{allowed}"
        )
    if _t1_layer_available(layers, "identity"):
        return f"仅行业/概念身份可用：行业 {industry or '--'}，概念 {concepts or '--'}；不能当作今日板块强度或排名。允许：{allowed}"
    if status == "stale":
        return f"过期板块证据：行业 {industry or '--'}，来源日期 {evidence.get('tradeDate') or '--'}；不能支持今日板块强度结论。"
    return "行业/概念证据不可用；只能使用上市板块、个股分时和市场环境做有限分析。"


def _sector_state_evidence_summary(candidate: Dict[str, Any]) -> str:
    state = str(candidate.get("sectorStateShadow") or "data_insufficient")
    label = str(candidate.get("sectorStateLabel") or T1_SECTOR_STATE_LABELS.get(state, "证据不足"))
    confidence = _safe_number(candidate.get("sectorStateConfidence"))
    risk_score = _safe_number(candidate.get("sectorRiskScore"))
    source = str(candidate.get("primarySectorStateSource") or candidate.get("industrySector") or "")
    scope = str(candidate.get("sectorSnapshotScope") or (candidate.get("sectorEvidence") or {}).get("sectorSnapshotScope") or "")
    reasons = [str(item) for item in candidate.get("sectorStateReasons") or (candidate.get("sectorEvidence") or {}).get("sectorStateReasons") or [] if str(item or "").strip()]
    if state == "data_insufficient":
        return "板块状态影子证据不足；阶段1只展示和落库，不影响本次正式 Top3。"
    return (
        f"板块状态影子：{source or '--'} 为 {label}，风险 {risk_score if risk_score is not None else '--'}，"
        f"置信度 {confidence if confidence is not None else '--'}，范围 {scope or '--'}。"
        f"阶段1只展示和落库，不影响本次正式 Top3。"
        + (f"依据：{'；'.join(reasons[:2])}" if reasons else "")
    )


def _apply_data_quality(candidate: Dict[str, Any], snapshot_source: str, market: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(candidate)
    source_status = market.get("sourceStatus") or {}
    cache_status = market.get("cacheStatus") or {}
    mock_status = market.get("mockStatus") or {}
    market_context = market.get("marketContext") or _classify_market_context(market)
    sector_quality = _sector_quality_from_semantics(item)
    kline_source = str(item.get("klineSource") or "simulated")
    kline_period = str(item.get("klinePeriod") or "")
    series_len = len(item.get("series") or [])
    reasons: List[str] = []
    grade = "A"
    core_eligible = True
    quote_quality = "A"

    if snapshot_source == "mock" or mock_status.get("used"):
        grade = "D"
        quote_quality = "D"
        core_eligible = False
        reasons.append("全市场快照为 Mock 模拟数据")
    elif (str(snapshot_source or "").startswith("cache:") or cache_status.get("used") or source_status.get("isCache")) and not cache_status.get("t1Frozen"):
        grade = "C"
        quote_quality = "C"
        core_eligible = False
        reasons.append("全市场快照来自缓存")

    if kline_source == "simulated":
        grade = "D"
        quote_quality = _worse_data_quality_grade(quote_quality, "D")
        core_eligible = False
        reasons.append("关键分时/K线为模拟回退")
    elif series_len < 8:
        grade = "B" if grade == "A" else grade
        quote_quality = _worse_data_quality_grade(quote_quality, "B")
        reasons.append("真实分时点数偏少")

    if not item.get("price") or float(item.get("price") or 0) <= 0:
        grade = "E"
        quote_quality = "E"
        core_eligible = False
        reasons.append("价格异常")

    market_quality = _market_quality_grade(market_context, snapshot_source, cache_status, mock_status)
    trade_date_status = market_context.get("tradeDateStatus") or market.get("tradeDateStatus") or {}
    if _t1_trade_date_blocks_official(trade_date_status):
        grade = _worse_data_quality_grade(grade, "D")
        market_quality = _worse_data_quality_grade(market_quality, "D")
        core_eligible = False
        reasons.append(_t1_trade_date_block_reason(trade_date_status))
    event_quality = _event_quality_grade(item)
    unavailable_evidence = []
    if sector_quality.get("status") in {"unavailable", "stale"}:
        unavailable_evidence.append("sector")
    if market_quality in {"C", "D", "E"}:
        unavailable_evidence.append("market")
    if event_quality in {"C", "D"}:
        unavailable_evidence.append("event")
    if sector_quality.get("grade") in {"C", "D"} and grade == "A":
        grade = "B"
    if market_quality in {"D", "E"}:
        grade = _worse_data_quality_grade(grade, market_quality)
    elif market_quality == "C" and grade == "A":
        grade = "B"
    if grade == "A" and event_quality in {"C", "D"}:
        grade = "B"

    if not core_eligible:
        decision_eligibility = "research_only"
    elif sector_quality.get("grade") in {"A", "B"} and market_quality in {"A", "B"}:
        decision_eligibility = "strict"
    elif market_context.get("state") in {"panic", "heavy_down"} and sector_quality.get("status") in {"unavailable", "stale"}:
        decision_eligibility = "restricted"
    else:
        decision_eligibility = "partial"

    item["dataQuality"] = {
        "grade": grade,
        "label": _data_quality_label(grade),
        "quoteQuality": quote_quality,
        "sectorQuality": sector_quality,
        "marketQuality": market_quality,
        "eventQuality": event_quality,
        "decisionEligibility": decision_eligibility,
        "unavailableEvidence": _unique_strings(unavailable_evidence),
        "eligibleForRealTop": core_eligible and grade in {"A", "B"},
        "snapshotSource": snapshot_source or "",
        "klineSource": kline_source,
        "klinePeriod": kline_period,
        "seriesPoints": series_len,
        "isMock": snapshot_source == "mock" or bool(mock_status.get("used")),
        "isCache": str(snapshot_source or "").startswith("cache:") or bool(cache_status.get("used")),
        "isSimulated": kline_source == "simulated",
        "reasons": _unique_strings(reasons + list(sector_quality.get("reasons") or [])),
    }
    if grade in {"C", "D", "E"}:
        item["researchOnly"] = True
    return item


def _market_quality_grade(market_context: Dict[str, Any], snapshot_source: str, cache_status: Dict[str, Any], mock_status: Dict[str, Any]) -> str:
    if snapshot_source == "mock" or mock_status.get("used"):
        return "D"
    if str(snapshot_source or "").startswith("cache:") or cache_status.get("used"):
        return "C"
    completeness = market_context.get("indicatorCompleteness") or {}
    missing = list(completeness.get("missing") or [])
    if not missing:
        return "A"
    if len(missing) <= 2:
        return "B"
    return "C"


def _event_quality_grade(candidate: Dict[str, Any]) -> str:
    catalysts = candidate.get("catalysts") or []
    if not catalysts:
        return "C"
    real_events = [
        item
        for item in catalysts
        if isinstance(item, dict) and str(item.get("type") or "") not in {"行情", "模拟"}
    ]
    return "A" if real_events else "B"


def _data_quality_label(grade: str) -> str:
    return {
        "A": "真实完整",
        "B": "真实但部分缺失",
        "C": "缓存/延迟",
        "D": "含模拟关键数据",
        "E": "数据异常",
    }.get(grade, grade or "--")


def _data_quality_stats(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    source_counts: Dict[str, int] = {}
    sector_counts: Dict[str, int] = {}
    sector_source_counts: Dict[str, int] = {}
    sector_state_counts: Dict[str, int] = {}
    sector_layer_counts: Dict[str, Dict[str, int]] = {
        "identity": {},
        "sectorSnapshot": {},
        "constituentRank": {},
    }
    decision_counts: Dict[str, int] = {}
    eligible = 0
    simulated = 0
    for candidate in candidates:
        quality = candidate.get("dataQuality") or {}
        grade = str(quality.get("grade") or "unknown")
        source = str(quality.get("klineSource") or candidate.get("klineSource") or "unknown")
        sector_status = str(candidate.get("sectorEvidenceStatus") or (quality.get("sectorQuality") or {}).get("status") or "unknown")
        sector_source_kind = str(candidate.get("sectorSnapshotSourceKind") or (quality.get("sectorQuality") or {}).get("sectorSnapshotSourceKind") or "unknown")
        sector_state = str(candidate.get("sectorStateShadow") or (quality.get("sectorQuality") or {}).get("sectorStateShadow") or "data_insufficient")
        decision = str(quality.get("decisionEligibility") or "unknown")
        layers = ((quality.get("sectorQuality") or {}).get("layers") or (candidate.get("sectorEvidence") or {}).get("evidenceLayers") or {})
        counts[grade] = counts.get(grade, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1
        sector_counts[sector_status] = sector_counts.get(sector_status, 0) + 1
        sector_source_counts[sector_source_kind] = sector_source_counts.get(sector_source_kind, 0) + 1
        sector_state_counts[sector_state] = sector_state_counts.get(sector_state, 0) + 1
        for layer_key in ("identity", "sectorSnapshot", "constituentRank"):
            layer_status = _t1_layer_status(layers, layer_key)
            bucket = sector_layer_counts.setdefault(layer_key, {})
            bucket[layer_status] = bucket.get(layer_status, 0) + 1
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        if quality.get("eligibleForRealTop"):
            eligible += 1
        if quality.get("isSimulated"):
            simulated += 1
    return {
        "total": len(candidates),
        "eligibleForRealTop": eligible,
        "simulatedKeyData": simulated,
        "counts": counts,
        "klineSourceCounts": source_counts,
        "sectorEvidenceCounts": sector_counts,
        "sectorSourceKindCounts": sector_source_counts,
        "sectorStateShadowCounts": sector_state_counts,
        "sectorLayerCounts": sector_layer_counts,
        "decisionEligibilityCounts": decision_counts,
        "strictSample": eligible,
    }


def _minute_kline_status(provider: Any, requested: int, attached: int, period: str) -> Dict[str, Any]:
    meta = dict(getattr(provider, "last_meta", {}) or {})
    source_counts = dict(meta.get("sourceCounts") or {})
    providers = meta.get("providers") if isinstance(meta.get("providers"), list) else []
    warning = str(meta.get("warning") or getattr(provider, "last_warning", "") or "")
    ok = int(meta.get("ok") or attached or 0)
    failed = max(0, int(meta.get("failed") or requested - ok))
    return {
        "source": meta.get("source") or getattr(provider, "source", ""),
        "period": meta.get("period") or period,
        "requested": int(meta.get("requested") or requested or 0),
        "ok": ok,
        "attached": int(attached or 0),
        "failed": failed,
        "sourceCounts": source_counts,
        "providers": providers,
        "warning": warning,
        "reusedCount": int(meta.get("reusedCount") or 0),
        "missingCount": int(meta.get("missingCount") or failed),
        "healthy": ok > 0,
    }


def _fetch_t1_minute_rows_with_retries(
    database: Database,
    provider: Any,
    codes: List[str],
    period: str,
    recommendation_date: str,
    exclude_run_id: int = 0,
) -> Dict[str, Dict[str, Any]]:
    unique_codes = _unique_codes(codes)
    if not unique_codes:
        if hasattr(provider, "last_meta"):
            provider.last_meta = {
                "source": getattr(provider, "source", ""),
                "requested": 0,
                "ok": 0,
                "failed": 0,
                "period": period,
                "elapsedMs": 0,
                "sourceCounts": {},
                "providers": [],
                "warning": "",
                "reusedCount": 0,
            }
        if hasattr(provider, "last_warning"):
            provider.last_warning = ""
        if hasattr(provider, "last_sources_by_code"):
            provider.last_sources_by_code = {}
        return {}

    reused_rows = database.load_latest_t1_overnight_minute_snapshots(
        recommendation_date=recommendation_date,
        codes=unique_codes,
        period=period,
        exclude_run_id=exclude_run_id,
    )
    results: Dict[str, Dict[str, Any]] = {}
    source_by_code: Dict[str, str] = {}
    provider_meta: List[Dict[str, Any]] = []
    warnings: List[str] = []
    reused_count = 0

    for code in unique_codes:
        reused = reused_rows.get(code)
        if not reused:
            continue
        row = {
            "code": code,
            "period": str(reused.get("period") or period or "15m"),
            "source": str(reused.get("source") or "t1_overnight_minute_cache"),
            "qualityStatus": str(reused.get("qualityStatus") or "available"),
            "error": str(reused.get("error") or ""),
            "series": reused.get("series") or [],
            "pointCount": int(reused.get("pointCount") or len(reused.get("series") or [])),
        }
        results[code] = row
        source_by_code[code] = row["source"]
        reused_count += 1
    if reused_count:
        provider_meta.append(
            {
                "source": "t1_overnight_minute_cache",
                "requested": reused_count,
                "ok": reused_count,
                "failed": 0,
                "period": period,
                "elapsedMs": 0,
                "warning": f"复用同日 T+1 分钟线快照 {reused_count} 只",
                "reused": True,
            }
        )

    missing = [code for code in unique_codes if code not in results]
    started = time.perf_counter()
    for attempt in range(1, max(1, T1_MINUTE_FETCH_ATTEMPTS) + 1):
        if not missing:
            break
        attempt_missing: List[str] = []
        for chunk in _code_chunks(missing, max(1, T1_MINUTE_FETCH_CHUNK_SIZE)):
            chunk_started = time.perf_counter()
            try:
                batch = provider.fetch_many(chunk, period=period)
            except Exception as error:  # noqa: BLE001 - best effort multi-source retry.
                warning = f"{getattr(provider, 'source', 'minute')} 批次分钟线失败：{error}"
                provider_meta.append(
                    {
                        "source": getattr(provider, "source", "minute"),
                        "requested": len(chunk),
                        "ok": 0,
                        "failed": len(chunk),
                        "period": period,
                        "elapsedMs": round((time.perf_counter() - chunk_started) * 1000),
                        "warning": warning,
                    }
                )
                warnings.append(warning)
                attempt_missing.extend(chunk)
                continue

            accepted = 0
            source_for_code = getattr(provider, "source_for_code", None)
            for code, series in batch.items():
                normalized = str(code or "").strip().zfill(6)
                if not normalized or not series or normalized in results:
                    continue
                source = source_for_code(normalized) if callable(source_for_code) else getattr(provider, "source", "minute")
                row = {
                    "code": normalized,
                    "period": period,
                    "source": str(source or getattr(provider, "source", "minute")),
                    "qualityStatus": "available",
                    "error": "",
                    "series": series,
                    "pointCount": len(series),
                }
                results[normalized] = row
                source_by_code[normalized] = row["source"]
                accepted += 1

            chunk_missing = [code for code in chunk if code not in results]
            attempt_missing.extend(chunk_missing)
            meta = dict(getattr(provider, "last_meta", {}) or {})
            provider_meta.append(
                {
                    **meta,
                    "source": meta.get("source") or getattr(provider, "source", "minute"),
                    "requested": meta.get("requested", len(chunk)),
                    "ok": meta.get("ok", accepted),
                    "accepted": accepted,
                    "missingAfter": len(chunk_missing),
                    "period": period,
                    "elapsedMs": meta.get("elapsedMs", round((time.perf_counter() - chunk_started) * 1000)),
                    "warning": meta.get("warning") or getattr(provider, "last_warning", ""),
                    "attempt": attempt,
                }
            )
            if chunk_missing:
                LOGGER.warning(
                    "t1 minute chunk missing attempt=%s chunk=%s missing=%s source=%s",
                    attempt,
                    len(chunk),
                    len(chunk_missing),
                    getattr(provider, "source", "minute"),
                )
                if T1_MINUTE_FETCH_CHUNK_SLEEP_SECONDS > 0:
                    time.sleep(T1_MINUTE_FETCH_CHUNK_SLEEP_SECONDS)
        missing = _unique_codes(attempt_missing)
        if missing and attempt < T1_MINUTE_FETCH_ATTEMPTS and T1_MINUTE_FETCH_RETRY_SLEEP_SECONDS > 0:
            time.sleep(T1_MINUTE_FETCH_RETRY_SLEEP_SECONDS)

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    missing_count = len([code for code in unique_codes if code not in results])
    source_counts: Dict[str, int] = {}
    for source in source_by_code.values():
        source_counts[source] = source_counts.get(source, 0) + 1
    warning = ""
    if missing_count:
        warning = f"{missing_count} 只候选未取到真实分钟线"
    provider_warnings = [str(item.get("warning") or "") for item in provider_meta if item.get("warning")]
    if provider_warnings:
        warning = "；".join([item for item in [warning, *provider_warnings[:3]] if item])
    if not results:
        warning = warning or "所有分钟线源均未返回有效数据"

    if hasattr(provider, "last_sources_by_code"):
        provider.last_sources_by_code = source_by_code
    if hasattr(provider, "last_warning"):
        provider.last_warning = warning
    if hasattr(provider, "last_meta"):
        provider.last_meta = {
            "source": getattr(provider, "source", "multi_minute"),
            "requested": len(unique_codes),
            "ok": len(results),
            "failed": missing_count,
            "period": period,
            "elapsedMs": elapsed_ms,
            "sourceCounts": source_counts,
            "providers": provider_meta,
            "warning": warning,
            "reusedCount": reused_count,
            "missingCount": missing_count,
        }
    return results


def _load_t1_sector_evidence_map(
    database: Database,
    codes: List[str],
    recommendation_date: str,
    scan_time: str,
) -> Dict[str, Dict[str, Any]]:
    normalized_codes = {_normalize_code(code) for code in codes if _normalize_code(code)}
    if not normalized_codes:
        return {}
    try:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, trade_date, scan_time, actual_source, data_quality_status
                FROM morning_sector_radar_runs
                WHERE status = 'done'
                  AND sector_type = 'all'
                  AND trade_date <= ?
                ORDER BY
                  CASE WHEN trade_date = ? THEN 0 ELSE 1 END,
                  trade_date DESC,
                  scan_time DESC,
                  id DESC
                LIMIT 8
                """,
                (recommendation_date, recommendation_date),
            ).fetchall()
    except Exception as error:  # noqa: BLE001 - sector evidence is useful, not mandatory.
        LOGGER.warning("t1 sector evidence query failed error=%s", error)
        return {}

    evidence_by_code: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        run_id = int(row["id"] or 0)
        if not run_id:
            continue
        try:
            sectors = database.load_morning_sector_radar_sectors(run_id)
            constituents = database.load_morning_sector_radar_constituents(run_id)
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("t1 sector evidence load failed run=%s error=%s", run_id, error)
            continue
        sector_map = {(item.get("sectorType"), item.get("sectorCode")): item for item in sectors}
        run_trade_date = str(row["trade_date"] or "")
        run_scan_time = str(row["scan_time"] or "")
        status = _sector_snapshot_status(run_trade_date, run_scan_time, recommendation_date, scan_time)
        for constituent in constituents:
            code = _normalize_code(constituent.get("code"))
            if code not in normalized_codes:
                continue
            current = evidence_by_code.get(code)
            if current and _sector_evidence_priority(current.get("status")) > _sector_evidence_priority(status):
                continue
            if (
                current
                and _sector_evidence_priority(current.get("status")) == _sector_evidence_priority(status)
                and int(current.get("sourceRunId") or 0) != run_id
            ):
                continue
            sector = sector_map.get((constituent.get("sectorType"), constituent.get("sectorCode"))) or {}
            evidence_by_code[code] = _merge_sector_evidence(
                existing=current,
                constituent=constituent,
                sector=sector,
                run={
                    "id": run_id,
                    "tradeDate": run_trade_date,
                    "scanTime": run_scan_time,
                    "actualSource": str(row["actual_source"] or ""),
                    "dataQualityStatus": str(row["data_quality_status"] or ""),
                },
                status=status,
            )
    return evidence_by_code


def _sector_snapshot_status(run_trade_date: str, run_scan_time: str, recommendation_date: str, scan_time: str) -> str:
    if run_trade_date != recommendation_date:
        return "stale"
    if run_scan_time and scan_time and run_scan_time > scan_time:
        return "partial"
    if run_scan_time >= "14:00":
        return "available"
    return "partial"


def _sector_evidence_priority(status: Any) -> int:
    return {"available": 4, "partial": 3, "stale": 2, "unavailable": 1}.get(str(status or ""), 0)


def _merge_sector_evidence(
    existing: Optional[Dict[str, Any]],
    constituent: Dict[str, Any],
    sector: Dict[str, Any],
    run: Dict[str, Any],
    status: str,
) -> Dict[str, Any]:
    payload = dict(existing or {})
    matched = list(payload.get("matchedSectors") or [])
    sector_type = str(constituent.get("sectorType") or sector.get("sectorType") or "")
    sector_name = str(sector.get("sectorName") or constituent.get("sectorName") or "").strip()
    if sector_name:
        matched.append(
            {
                "sectorCode": constituent.get("sectorCode") or sector.get("sectorCode") or "",
                "sectorType": sector_type,
                "sectorName": sector_name,
                "status": status,
                "tradeDate": run.get("tradeDate") or "",
                "scanTime": run.get("scanTime") or "",
                "finalRank": int(sector.get("finalRank") or sector.get("ruleRank") or 0),
                "industryRank": int(sector.get("finalRank") or sector.get("ruleRank") or 0),
                "stockRank": int(constituent.get("sectorRank") or 0),
                "limitUpCount": int(sector.get("limitUpCount") or 0),
                "activityScore": _safe_number(sector.get("activityScore")),
                "changePct": _safe_number(sector.get("changePct")),
                "mainNetInflow": _safe_number(sector.get("mainNetInflow")),
                "fundFlowStatus": sector.get("fundFlowStatus") or "",
                "sourceRunId": run.get("id"),
                "source": run.get("actualSource") or "morning_sector_radar",
            }
        )
    payload.update(
        {
            "status": status,
            "source": run.get("actualSource") or "morning_sector_radar",
            "sourceRunId": run.get("id"),
            "tradeDate": run.get("tradeDate") or "",
            "scanTime": run.get("scanTime") or "",
            "dataQualityStatus": run.get("dataQualityStatus") or "",
            "matchedSectors": _dedupe_sector_matches(matched),
        }
    )
    return payload


def _dedupe_sector_matches(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        key = (item.get("sectorType"), item.get("sectorCode") or item.get("sectorName"))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return sorted(
        result,
        key=lambda item: (
            0 if item.get("sectorType") == "industry" else 1,
            int(item.get("finalRank") or 999),
            -float(item.get("activityScore") or 0),
        ),
    )


def _listing_board_from_candidate(candidate: Dict[str, Any]) -> str:
    sector = str(candidate.get("sector") or "").strip()
    code = _normalize_code(candidate.get("code"))
    if sector in LISTING_BOARD_NAMES:
        return sector
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("8", "4", "920")):
        return "北交所"
    if code.startswith("6"):
        return "沪市主板"
    if code.startswith(("0", "001", "002", "003")):
        return "深市主板"
    return sector or "其他"


def _primary_sector_state_from_matches(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    candidates = [item for item in matches if str(item.get("sectorType") or "") == "industry"]
    if not candidates:
        candidates = [item for item in matches if str(item.get("sectorType") or "") == "concept"]
    if not candidates:
        return {
            "sectorStateShadow": "data_insufficient",
            "sectorStateLabel": T1_SECTOR_STATE_LABELS["data_insufficient"],
            "sectorStateConfidence": 0.0,
            "sectorRiskScore": 55.0,
            "sectorFlowInterpretation": "缺少行业/概念状态证据。",
            "sectorStateVersion": T1_SECTOR_STATE_SHADOW_VERSION,
            "sectorStateStage": "shadow",
            "sectorStateReasons": ["未匹配到可用行业/概念快照。"],
            "primarySectorStateSource": "",
            "sectorExceptionEligible": False,
        }
    def sort_key(item: Dict[str, Any]) -> tuple[int, float, int]:
        return (
            0 if str(item.get("sectorType") or "") == "industry" else 1,
            -float(item.get("sectorStateConfidence") or 0),
            int(item.get("finalRank") or item.get("industryRank") or 9999),
        )

    match = sorted(candidates, key=sort_key)[0]
    state = str(match.get("sectorStateShadow") or "data_insufficient")
    risk_score = _safe_number(match.get("sectorRiskScore"))
    confidence = _safe_number(match.get("sectorStateConfidence"))
    return {
        "sectorStateShadow": state if state in T1_SECTOR_STATES else "data_insufficient",
        "sectorStateLabel": match.get("sectorStateLabel") or T1_SECTOR_STATE_LABELS.get(state, state),
        "sectorStateConfidence": round(confidence if confidence is not None else 0.0, 2),
        "sectorRiskScore": round(risk_score if risk_score is not None else 55.0, 1),
        "sectorFlowInterpretation": match.get("sectorFlowInterpretation") or "",
        "sectorStateVersion": match.get("sectorStateVersion") or T1_SECTOR_STATE_SHADOW_VERSION,
        "sectorStateStage": match.get("sectorStateStage") or "shadow",
        "sectorStateReasons": list(match.get("sectorStateReasons") or []),
        "primarySectorStateSource": match.get("sectorName") or "",
        "sectorExceptionEligible": state in {"retreat", "distribution", "weakening", "overheated"}
        and float(confidence if confidence is not None else 0) >= 0.45,
    }


def _sector_semantics(candidate: Dict[str, Any], sector_evidence: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    legacy_sector = str(candidate.get("sector") or "").strip()
    listing_board = _listing_board_from_candidate(candidate)
    board_proxy = legacy_sector in LISTING_BOARD_NAMES or legacy_sector == listing_board
    matches = list((sector_evidence or {}).get("matchedSectors") or [])
    industry_matches = [item for item in matches if item.get("sectorType") == "industry"]
    concept_matches = [item for item in matches if item.get("sectorType") == "concept"]
    industry = str((industry_matches[0] or {}).get("sectorName") or "").strip() if industry_matches else ""
    concepts = _unique_strings([item.get("sectorName") for item in concept_matches])
    layers = _sector_layers_from_evidence(sector_evidence, industry, concepts)
    status = _compat_sector_status_from_layers(layers)
    identity_available = _t1_layer_available(layers, "identity")
    snapshot_available = _t1_layer_available(layers, "sectorSnapshot")
    rank_available = _t1_layer_available(layers, "constituentRank")
    snapshot_matches = _available_sector_snapshot_matches(matches) if snapshot_available else []
    snapshot_kind = _sector_snapshot_source_kind(
        snapshot_matches,
        str(((sector_evidence or {}).get("evidenceLayers") or {}).get("sectorSnapshot", {}).get("source") or (sector_evidence or {}).get("source") or ""),
    )
    if snapshot_available and (industry or concepts) and snapshot_kind == "candidate_pool_aggregate":
        semantic_level = "t1_candidate_pool_sector_aggregate"
    elif snapshot_available and (industry or concepts) and snapshot_kind == "mixed":
        semantic_level = "t1_mixed_sector_snapshot"
    else:
        semantic_level = "real_sector_snapshot" if snapshot_available and (industry or concepts) else "stock_sector_identity" if identity_available and (industry or concepts) else "listing_board_proxy"
    reasons: List[str] = []
    evidence_reasons = [str(item) for item in (sector_evidence or {}).get("reasons") or [] if str(item or "").strip()]
    if rank_available:
        reasons.append("T+1 推荐日行业/概念身份、同日板块强度和成分股排名均可用。")
    elif snapshot_available:
        if snapshot_kind == "candidate_pool_aggregate":
            reasons.append("T+1 推荐日行业/概念身份和候选池聚合板块强度可用；它只反映本次候选池样本，不代表全市场行业统计或外部资金流。")
        else:
            reasons.append("T+1 推荐日行业/概念身份和同日板块强度可用，但成分股排名不可用。")
    elif identity_available:
        reasons.append("T+1 逐股行业/概念身份可用，但同日板块强度和成分股排名不可用。")
    elif status == "stale":
        reasons.append(f"行业/概念快照来自 {(sector_evidence or {}).get('tradeDate') or '--'}，不可作为推荐日板块强度证据。")
    else:
        reasons.append("未找到推荐日行业/概念快照；上市板块只代表交易所板块。")
    reasons.extend(evidence_reasons)

    if legacy_sector and not board_proxy and not industry:
        industry = legacy_sector
        semantic_level = "legacy_provider_sector_unverified"
        layers = _t1_evidence_layers("stale", "legacy_candidate_sector")
        status = _compat_sector_status_from_layers(layers)
        identity_available = False
        snapshot_available = False
        rank_available = False
        reasons.append("候选源提供了非上市板块名称，但缺少同日板块快照验证。")

    snapshot_industry_matches = [item for item in snapshot_matches if item.get("sectorType") == "industry"]
    snapshot_concept_matches = [item for item in snapshot_matches if item.get("sectorType") == "concept"]
    industry_limit_up = int((snapshot_industry_matches[0] or {}).get("limitUpCount") or 0) if snapshot_industry_matches else 0
    industry_rank = int((snapshot_industry_matches[0] or {}).get("finalRank") or 0) if snapshot_industry_matches else 0
    stock_rank = int((snapshot_industry_matches[0] or {}).get("stockRank") or 0) if snapshot_industry_matches else 0
    concept_limit_counts = [
        {
            "sectorName": item.get("sectorName"),
            "limitUpCount": int(item.get("limitUpCount") or 0),
            "rank": int(item.get("finalRank") or 0),
            "sampleCount": int(item.get("sampleCount") or 0),
            "snapshotScope": item.get("snapshotScope") or "full_market",
            "source": item.get("source") or "",
        }
        for item in snapshot_concept_matches
    ]
    allowed_claims = _sector_allowed_claims(
        layers,
        industry,
        concepts,
        str((layers.get("sectorSnapshot") or {}).get("source") or (sector_evidence or {}).get("source") or ""),
        snapshot_matches,
    )
    snapshot_scope = (
        "candidate_pool"
        if snapshot_kind == "candidate_pool_aggregate"
        else "mixed"
        if snapshot_kind == "mixed"
        else "full_market"
        if snapshot_kind == "external_snapshot"
        else "unknown"
    )
    sector_state = _primary_sector_state_from_matches(snapshot_matches if snapshot_available else [])
    return {
        "legacySector": legacy_sector,
        "listingBoard": listing_board,
        "industrySector": industry,
        "conceptSectors": concepts,
        "marketBoardLimitUpCount": int(candidate.get("sectorLimitUps") or 0) if board_proxy else 0,
        "marketBoardRank": int(candidate.get("sectorRank") or 0) if board_proxy else 0,
        "industryLimitUpCount": industry_limit_up if snapshot_available else 0,
        "conceptLimitUpCount": max([int(item.get("limitUpCount") or 0) for item in concept_matches] or [0]) if snapshot_available else 0,
        "conceptLimitUpCounts": concept_limit_counts if snapshot_available else [],
        "industryRank": industry_rank if snapshot_available else 0,
        "stockRankInIndustry": stock_rank if rank_available else 0,
        "industrySectorSampleCount": int((snapshot_industry_matches[0] or {}).get("sampleCount") or 0) if snapshot_industry_matches else 0,
        "sectorSnapshotSourceKind": snapshot_kind,
        "sectorSnapshotScope": snapshot_scope,
        "sectorEvidenceStatus": status,
        "sectorStateShadow": sector_state["sectorStateShadow"],
        "sectorStateLabel": sector_state["sectorStateLabel"],
        "sectorStateConfidence": sector_state["sectorStateConfidence"],
        "sectorRiskScore": sector_state["sectorRiskScore"],
        "sectorFlowInterpretation": sector_state["sectorFlowInterpretation"],
        "sectorStateVersion": sector_state["sectorStateVersion"],
        "sectorStateStage": sector_state["sectorStateStage"],
        "sectorStateReasons": sector_state["sectorStateReasons"],
        "primarySectorStateSource": sector_state["primarySectorStateSource"],
        "sectorExceptionEligible": sector_state["sectorExceptionEligible"],
        "sectorEvidence": {
            "version": (sector_evidence or {}).get("version") or "v1_sector_semantics",
            "status": status,
            "semanticLevel": (sector_evidence or {}).get("semanticLevel") or semantic_level,
            "source": (sector_evidence or {}).get("source") or "",
            "sourceRunId": (sector_evidence or {}).get("sourceRunId") or 0,
            "tradeDate": (sector_evidence or {}).get("tradeDate") or "",
            "scanTime": (sector_evidence or {}).get("scanTime") or "",
            "listingBoardProxy": board_proxy,
            "listingBoardIsIndustry": False,
            "matchedSectors": matches,
            "evidenceLayers": layers,
            "allowedClaims": allowed_claims,
            "sectorSnapshotSources": _sector_snapshot_sources(snapshot_matches, str((sector_evidence or {}).get("source") or "")),
            "sectorSnapshotSourceKind": snapshot_kind,
            "sectorSnapshotScope": snapshot_scope,
            "internalSectorSnapshotCount": sum(1 for item in snapshot_matches if _sector_source_is_internal(item.get("source"))),
            "sectorStateShadow": sector_state["sectorStateShadow"],
            "sectorStateLabel": sector_state["sectorStateLabel"],
            "sectorStateConfidence": sector_state["sectorStateConfidence"],
            "sectorRiskScore": sector_state["sectorRiskScore"],
            "sectorFlowInterpretation": sector_state["sectorFlowInterpretation"],
            "sectorStateVersion": sector_state["sectorStateVersion"],
            "sectorStateStage": sector_state["sectorStateStage"],
            "sectorStateReasons": sector_state["sectorStateReasons"],
            "sectorExceptionEligible": sector_state["sectorExceptionEligible"],
            "reasons": _unique_strings(reasons),
        },
    }


def _sector_quality_from_semantics(semantics: Dict[str, Any]) -> Dict[str, Any]:
    status = str(semantics.get("sectorEvidenceStatus") or "unavailable")
    evidence = semantics.get("sectorEvidence") or {}
    layers = _sector_layers_from_evidence(evidence, str(semantics.get("industrySector") or ""), list(semantics.get("conceptSectors") or []))
    identity_available = _t1_layer_available(layers, "identity")
    snapshot_available = _t1_layer_available(layers, "sectorSnapshot")
    rank_available = _t1_layer_available(layers, "constituentRank")
    snapshot_kind = str(evidence.get("sectorSnapshotSourceKind") or semantics.get("sectorSnapshotSourceKind") or "")
    if identity_available and snapshot_available and rank_available:
        grade = "A"
        label = "身份/强度/排名均可用"
    elif identity_available and snapshot_available:
        grade = "B"
        label = "身份和自建聚合强度可用" if snapshot_kind == "candidate_pool_aggregate" else "身份和板块强度可用"
    elif identity_available:
        grade = "C"
        label = "仅行业/概念身份可用"
    elif status == "stale":
        grade = "C"
        label = "板块身份或快照过期"
    else:
        grade = "D"
        label = "板块证据不可用"
    return {
        "grade": grade,
        "status": status,
        "label": label,
        "layers": layers,
        "allowedClaims": list(evidence.get("allowedClaims") or []),
        "reasons": list(evidence.get("reasons") or []),
        "sectorSnapshotSourceKind": snapshot_kind,
        "sectorSnapshotScope": evidence.get("sectorSnapshotScope") or semantics.get("sectorSnapshotScope") or "",
        "sectorStateShadow": semantics.get("sectorStateShadow") or evidence.get("sectorStateShadow") or "data_insufficient",
        "sectorStateLabel": semantics.get("sectorStateLabel") or evidence.get("sectorStateLabel") or T1_SECTOR_STATE_LABELS["data_insufficient"],
        "sectorStateConfidence": semantics.get("sectorStateConfidence") or evidence.get("sectorStateConfidence"),
        "sectorRiskScore": semantics.get("sectorRiskScore") or evidence.get("sectorRiskScore"),
        "sectorStateStage": semantics.get("sectorStateStage") or evidence.get("sectorStateStage") or "shadow",
    }


def _minute_source_for_pick(provider: Any, pick: Dict[str, Any], fallback: str) -> str:
    source_for_code = getattr(provider, "source_for_code", None)
    code = str(pick.get("code") or "").zfill(6)
    if callable(source_for_code):
        source = source_for_code(code)
        if source:
            return source
    return fallback or getattr(provider, "source", "")


def _minute_provider_source_for_code(provider: Any, code: str) -> str:
    source_for_code = getattr(provider, "source_for_code", None)
    normalized = str(code or "").zfill(6)
    if callable(source_for_code):
        source = source_for_code(normalized)
        if source:
            return str(source)
    return str(getattr(provider, "source", "") or "minute")


def _normalize_validation_minute_period(period: str) -> str:
    text = str(period or "1m").strip().lower()
    if text.endswith("min"):
        text = f"{text[:-3]}m"
    if text in {"1m", "5m", "15m", "30m", "60m"}:
        return text
    return "1m"


def _validation_minute_quality(series: List[Dict[str, Any]], target_trade_date: str) -> str:
    dated = _minute_series_for_trade_date(series or [], target_trade_date)
    if not dated:
        return "unavailable"
    times = {_minute_time(item) for item in dated}
    if any(time >= "09:45" for time in times if time):
        return "available"
    return "partial"


def _verification_picks_for_scope(run: Dict[str, Any], database: Database, scope: str = "final") -> List[Dict[str, Any]]:
    normalized_scope = str(scope or "final").strip().lower()
    picks = list(run.get("finalPicks") or [])
    if not picks:
        picks = [
            item.get("pick") or item
            for item in database.load_t1_overnight_selection_items(int(run.get("id") or 0))
            if item.get("selected") and item.get("stage") == "final"
        ]
    if normalized_scope != "all":
        return picks
    baseline_groups = (run.get("market") or {}).get("baselineGroups") or {}
    baseline_picks = [
        item
        for value in baseline_groups.values()
        if isinstance(value, list)
        for item in value
        if isinstance(item, dict)
    ]
    return _unique_by_code(list(picks) + baseline_picks)


def _validation_backfill_target_codes(
    codes: List[str],
    snapshots: Dict[str, Dict[str, Any]],
    outcomes: List[Dict[str, Any]],
    target_trade_date: str,
    force: bool = False,
) -> tuple[List[str], List[str]]:
    if force:
        return _unique_codes(codes), []
    outcome_by_code = {
        str(item.get("code") or "").zfill(6): item
        for item in outcomes
        if str(item.get("code") or "").strip()
    }
    need: List[str] = []
    skipped: List[str] = []
    for code in _unique_codes(codes):
        snapshot = snapshots.get(code) or {}
        outcome = outcome_by_code.get(code) or {}
        if _validation_snapshot_supports_primary(snapshot, target_trade_date):
            skipped.append(code)
            continue
        need.append(code)
    return need, skipped


def _validation_snapshot_supports_primary(snapshot: Dict[str, Any], target_trade_date: str) -> bool:
    if not snapshot or str(snapshot.get("qualityStatus") or "") not in {"available", "partial"}:
        return False
    series = _minute_series_from_validation_snapshot(snapshot, target_trade_date)
    if not series:
        return False
    times = [_minute_time(item) for item in series]
    return any(time >= "09:45" for time in times if time)


def _outcome_has_strict_primary(outcome: Dict[str, Any]) -> bool:
    primary = ((outcome.get("outcome") or outcome).get("tradeable") or {}).get("primaryScenario") or {}
    data_source = str((outcome.get("outcome") or outcome).get("dataSource") or "")
    if primary.get("scenarioKey") != PRIMARY_TRADEABLE_SCENARIO_KEY:
        return False
    if primary.get("dataStatus") != "ok" or primary.get("netReturnPct") is None:
        return False
    if any(token in data_source for token in ["mock", "simulated"]):
        return False
    return True


def _validation_backfill_role_summary(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    summary = {
        "pick": {"stored": 0, "skipped": 0, "failed": 0, "total": 0},
        "shadow": {"stored": 0, "skipped": 0, "failed": 0, "total": 0},
        "benchmark": {"stored": 0, "skipped": 0, "failed": 0, "total": 0},
    }
    for item in items or []:
        role_text = str(item.get("role") or "")
        role = role_text if role_text in summary else "pick"
        status = str(item.get("status") or "")
        summary[role]["total"] += 1
        if status in {"stored", "skipped", "failed"}:
            summary[role][status] += 1
    return summary


def _validation_backfill_role_label(role: str) -> str:
    return {
        "pick": "最终推荐",
        "shadow": "影子对照",
        "benchmark": "市场基准",
    }.get(str(role or ""), "最终推荐")


def _validation_backfill_message(
    run: Dict[str, Any],
    target_trade_date: str,
    requested: int,
    stored: int,
    failed_items: List[Dict[str, Any]],
    skipped_count: int,
    role_summary: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict[str, Any]:
    failed_count = len(failed_items or [])
    role_summary = role_summary or {}
    pick_summary = role_summary.get("pick") or {}
    shadow_summary = role_summary.get("shadow") or {}
    benchmark_summary = role_summary.get("benchmark") or {}
    pick_failed = int(pick_summary.get("failed") or 0)
    pick_stored = int(pick_summary.get("stored") or 0)
    pick_skipped = int(pick_summary.get("skipped") or 0)
    shadow_failed = int(shadow_summary.get("failed") or 0)
    shadow_stored = int(shadow_summary.get("stored") or 0)
    shadow_skipped = int(shadow_summary.get("skipped") or 0)
    benchmark_failed = int(benchmark_summary.get("failed") or 0)
    benchmark_stored = int(benchmark_summary.get("stored") or 0)
    benchmark_skipped = int(benchmark_summary.get("skipped") or 0)
    role_text = ""
    if role_summary:
        role_text = (
            f"最终推荐：成功 {pick_stored} 只、已有 {pick_skipped} 只、失败 {pick_failed} 只；"
            f"影子对照：成功 {shadow_stored} 只、已有 {shadow_skipped} 只、失败 {shadow_failed} 只；"
            f"市场基准：成功 {benchmark_stored} 只、已有 {benchmark_skipped} 只、失败 {benchmark_failed} 只。"
        )
    if requested <= 0:
        return {
            "message": f"当前 run 已有可用验证分钟线，跳过 {skipped_count} 只；已重新验证。{role_text}",
            "reason": "",
            "recoverable": True,
        }
    if failed_count <= 0:
        return {
            "message": f"已补拉目标验证日分钟线 {stored} 只，跳过 {skipped_count} 只；已重新验证并更新标签。{role_text}",
            "reason": "",
            "recoverable": True,
        }
    failed_pick_codes = _unique_codes([
        str(item.get("code") or "")
        for item in failed_items
        if str(item.get("role") or "") in {"", "pick"}
    ])[:8]
    failed_shadow_codes = _unique_codes([
        str(item.get("code") or "")
        for item in failed_items
        if str(item.get("role") or "") == "shadow"
    ])[:8]
    failed_benchmark_codes = _unique_codes([
        str(item.get("code") or "")
        for item in failed_items
        if str(item.get("role") or "") == "benchmark"
    ])[:8]
    failed_codes = _unique_codes([*failed_pick_codes, *failed_shadow_codes, *failed_benchmark_codes])[:8]
    run_role = str((run.get("request") or {}).get("runRole") or "")
    recommendation_date = str(run.get("recommendationDate") or "")
    trade_date_status = _t1_trade_date_status(recommendation_date, str(run.get("scanTime") or ""))
    parts = [
        f"{','.join(failed_codes)} 未取到 {target_trade_date} 的目标日分钟线",
        "本地也没有可复用的目标日冻结快照",
    ]
    if failed_pick_codes:
        parts.append(f"最终推荐缺少次日 09:45 卖出验证价：{','.join(failed_pick_codes)}")
    if failed_shadow_codes:
        parts.append(f"影子对照缺少次日 09:45 验证价：{','.join(failed_shadow_codes)}")
    if failed_benchmark_codes:
        parts.append(f"市场基准缺少同口径对照价：{','.join(failed_benchmark_codes)}")
    if run_role and run_role != "official":
        parts.append(f"该 run 类型为 {run_role}，不是正式 official 样本")
    if str(trade_date_status.get("status") or "").startswith("non_trading"):
        parts.append(f"推荐日 {recommendation_date} 不是交易日")
    reason = "；".join(parts)
    if stored > 0:
        message = f"部分补拉完成：成功 {stored} 只，失败 {failed_count} 只，跳过 {skipped_count} 只。{role_text}失败原因：{reason}。"
    else:
        message = f"无法补拉目标验证日分钟线：失败 {failed_count} 只，跳过 {skipped_count} 只。{role_text}原因：{reason}。"
    return {
        "message": message,
        "reason": reason,
        "recoverable": pick_failed <= 0,
    }


def _enrich_market_turnover_context(database: Database, market: Dict[str, Any], source: str) -> Dict[str, Any]:
    enriched = dict(market or {})
    turnover_amount = _safe_number(enriched.get("turnoverAmount"))
    today = date.today().isoformat()
    history = database.get_app_setting(MARKET_TURNOVER_HISTORY_KEY, [])
    if not isinstance(history, list):
        history = []
    normalized_history = []
    for row in history:
        if not isinstance(row, dict):
            continue
        record_date = str(row.get("date") or "")
        amount = _safe_number(row.get("turnoverAmount"))
        if record_date and amount and amount > 0:
            normalized_history.append({"date": record_date, "turnoverAmount": amount, "source": str(row.get("source") or "")})
    previous = [row for row in normalized_history if row["date"] < today]
    previous = sorted(previous, key=lambda row: row["date"], reverse=True)[:5]
    if turnover_amount and turnover_amount > 0 and previous:
        average_5d = sum(row["turnoverAmount"] for row in previous) / len(previous)
        if average_5d > 0:
            enriched["turnoverAvg5d"] = round(average_5d, 2)
            enriched["turnoverRatio5d"] = round(turnover_amount / average_5d, 3)
            enriched["turnoverHistory"] = {"sampleDays": len(previous), "source": "local_t1_history"}
    elif turnover_amount and turnover_amount > 0:
        enriched["turnoverHistory"] = {"sampleDays": 0, "source": "local_t1_history", "warning": "缺少近5日成交额历史"}

    if turnover_amount and turnover_amount > 0 and source not in {"mock"} and not str(source or "").startswith("cache:"):
        rows = [row for row in normalized_history if row["date"] != today]
        rows.append({"date": today, "turnoverAmount": turnover_amount, "source": source})
        rows = sorted(rows, key=lambda row: row["date"], reverse=True)[:30]
        database.set_app_setting(MARKET_TURNOVER_HISTORY_KEY, rows)
    return enriched


def _market_change_items(raw_items: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not isinstance(raw_items, list):
        return items
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        change = _safe_number(item.get("change"))
        if change is None:
            continue
        items.append({**item, "name": str(item.get("name") or ""), "change": change})
    return items


def _legacy_equal_weighted_indices(items: List[Dict[str, Any]]) -> bool:
    names = {str(item.get("name") or "") for item in items}
    return bool(names) and names.issubset({"沪深主板", "创业板"})


def _growth_or_broad_small_index(item: Dict[str, Any]) -> bool:
    name = str(item.get("name") or "")
    code = str(item.get("code") or "")
    return "创业" in name or "中证1000" in name or "1000" in code


def _classify_market_context(market: Dict[str, Any]) -> Dict[str, Any]:
    breadth = market.get("breadth") or {}
    up = int(breadth.get("up") or 0)
    down = int(breadth.get("down") or 0)
    total = max(1, up + down)
    down_ratio = down / total * 100
    limit_up = int(market.get("limitUp") or 0)
    limit_down = int(market.get("limitDown") or 0)
    st_limit_down = int(market.get("stLimitDown") or 0)
    near_limit_down = int(market.get("nearLimitDown") or 0)
    total_limit_down = int(market.get("totalLimitDown") or (limit_down + st_limit_down))
    index_items = _market_change_items(market.get("indices") or [])
    equal_weighted_items = _market_change_items(market.get("equalWeightedIndices") or [])
    legacy_equal_weighted = False
    if not equal_weighted_items and _legacy_equal_weighted_indices(index_items):
        legacy_equal_weighted = True
        equal_weighted_items = [
            {"name": "主板等权" if item.get("name") == "沪深主板" else "创业板等权", "change": item.get("change")}
            for item in index_items
        ]
        index_items = []
    index_changes = [float(item["change"]) for item in index_items]
    equal_weighted_changes = [float(item["change"]) for item in equal_weighted_items]
    main_index_change = min(index_changes) if index_changes else None
    average_index_change = sum(index_changes) / len(index_changes) if index_changes else None
    weakest_equal_weighted_change = min(equal_weighted_changes) if equal_weighted_changes else None
    average_equal_weighted_change = sum(equal_weighted_changes) / len(equal_weighted_changes) if equal_weighted_changes else None
    trend_reference_change = average_index_change if average_index_change is not None else (average_equal_weighted_change or 0.0)
    turnover_amount = _safe_number(market.get("turnoverAmount"))
    turnover_ratio_5d = _safe_number(market.get("turnoverRatio5d"))
    late_session_change = _safe_number(market.get("lateSessionChange"))
    mode = str(market.get("mode") or "")
    missing_metrics = []
    if not index_changes:
        missing_metrics.append("真实指数涨跌幅")
    if not equal_weighted_changes:
        missing_metrics.append("个股等权涨跌幅")
    if market.get("turnoverAmount") is None:
        missing_metrics.append("全市场成交额")
    if market.get("turnoverRatio5d") is None:
        missing_metrics.append("成交额近5日对比")
    if market.get("lateSessionChange") is None:
        missing_metrics.append("14:30后指数变化")
    market_index_status = market.get("marketIndexStatus") or {}
    if market_index_status.get("warning") and not index_changes:
        missing_metrics.append(str(market_index_status.get("warning")))
    limit_down_summary = {
        "natural": limit_down,
        "st": st_limit_down,
        "near": near_limit_down,
        "total": total_limit_down,
    }

    system_index_panic = main_index_change is not None and main_index_change <= -2.5
    growth_index_panic = any(_growth_or_broad_small_index(item) and float(item["change"]) <= -3.0 for item in index_items)
    natural_limit_down_panic = limit_down >= 80
    st_limit_down_panic = st_limit_down >= 60 and natural_limit_down_panic
    near_limit_down_panic = near_limit_down >= 180 and down_ratio >= 70
    small_loss_panic = weakest_equal_weighted_change is not None and weakest_equal_weighted_change <= -3.0 and down_ratio >= 70
    turnover_selloff = turnover_ratio_5d is not None and turnover_ratio_5d >= 1.2 and (
        (average_index_change is not None and average_index_change <= -1.2)
        or (weakest_equal_weighted_change is not None and weakest_equal_weighted_change <= -2.0 and down_ratio >= 70)
    )
    turnover_heavy = turnover_ratio_5d is not None and turnover_ratio_5d >= 1.1 and (
        (average_index_change is not None and average_index_change < 0)
        or (average_equal_weighted_change is not None and average_equal_weighted_change <= -0.8 and down_ratio >= 60)
    )

    panic_checks = [
        system_index_panic,
        growth_index_panic,
        down_ratio >= 80,
        natural_limit_down_panic,
        st_limit_down_panic,
        near_limit_down_panic,
        limit_up <= max(5, limit_down * 0.25) and limit_down >= 20,
        small_loss_panic,
        turnover_selloff,
        late_session_change is not None and late_session_change <= -0.7,
        mode == "defensive" and down_ratio >= 78 and limit_down >= 40,
    ]
    heavy_checks = [
        main_index_change is not None and main_index_change <= -1.5,
        down_ratio >= 70,
        limit_down >= 40,
        st_limit_down >= 30,
        near_limit_down >= 80,
        turnover_heavy,
        late_session_change is not None and late_session_change <= -0.35,
        mode == "defensive",
        limit_down > limit_up * 1.5 and limit_down >= 20,
        weakest_equal_weighted_change is not None and weakest_equal_weighted_change <= -1.5 and down_ratio >= 60,
    ]
    panic_reason_parts = []
    if system_index_panic or growth_index_panic:
        panic_reason_parts.append("真实指数明显下跌")
    if down_ratio >= 80:
        panic_reason_parts.append(f"下跌占比 {down_ratio:.1f}%")
    if limit_down >= 80:
        panic_reason_parts.append(f"自然跌停 {limit_down} 只")
    if st_limit_down >= 40:
        panic_reason_parts.append(f"ST 跌停 {st_limit_down} 只")
    if near_limit_down >= 80:
        panic_reason_parts.append(f"接近跌停 {near_limit_down} 只")
    if small_loss_panic:
        panic_reason_parts.append("小票亏钱效应深跌")
    if turnover_selloff:
        panic_reason_parts.append("成交额放大且抛压明显")
    if late_session_change is not None and late_session_change <= -0.7:
        panic_reason_parts.append("尾盘继续跳水")
    heavy_reason_parts = []
    if main_index_change is not None and main_index_change <= -1.5:
        heavy_reason_parts.append("真实指数偏弱")
    if down_ratio >= 70:
        heavy_reason_parts.append(f"下跌占比 {down_ratio:.1f}%")
    if limit_down >= 40:
        heavy_reason_parts.append(f"自然跌停 {limit_down} 只")
    if st_limit_down >= 20:
        heavy_reason_parts.append(f"ST 跌停 {st_limit_down} 只")
    if weakest_equal_weighted_change is not None and weakest_equal_weighted_change <= -1.5 and down_ratio >= 60:
        heavy_reason_parts.append("小票亏钱效应扩散")
    if near_limit_down >= 80:
        heavy_reason_parts.append(f"接近跌停 {near_limit_down} 只")
    if turnover_heavy:
        heavy_reason_parts.append("成交额放大且承接偏弱")
    if late_session_change is not None and late_session_change <= -0.35:
        heavy_reason_parts.append("尾盘修复不足")

    reasons: List[str] = []
    state = "mixed"
    risk_level = "medium"
    strategy_mode = "均衡"
    max_pick_count = DEFAULT_FINAL_LIMIT
    suggested_position = "20%-30%"

    if sum(1 for value in panic_checks if value) >= 3 or (main_index_change is not None and main_index_change <= -3.5) or limit_down >= 120:
        state = "panic"
        risk_level = "extreme"
        strategy_mode = "空仓观察"
        max_pick_count = 0
        suggested_position = "0%"
        reasons.append("；".join(panic_reason_parts) or "市场进入恐慌退潮阈值")
    elif sum(1 for value in heavy_checks if value) >= 2:
        state = "heavy_down"
        risk_level = "high"
        strategy_mode = "防守观察"
        max_pick_count = 1
        suggested_position = "0%-10%"
        reasons.append("；".join(heavy_reason_parts) or "市场广度或小票亏钱效应明显恶化")
    elif trend_reference_change < -0.4 or down_ratio >= 60:
        state = "mild_down"
        risk_level = "elevated"
        strategy_mode = "谨慎"
        max_pick_count = 2
        suggested_position = "0%-15%"
        reasons.append("市场偏弱，需提高过滤标准")
    elif trend_reference_change >= 1.6 and up > down * 1.8 and limit_up >= 70:
        state = "euphoric"
        risk_level = "medium_high"
        strategy_mode = "防兑现"
        max_pick_count = DEFAULT_FINAL_LIMIT
        suggested_position = "10%-25%"
        reasons.append("市场情绪高潮，需防次日兑现")
    elif trend_reference_change >= 0.8 and up > down * 1.3:
        state = "trend_up"
        risk_level = "medium"
        strategy_mode = "正常"
        reasons.append("市场趋势偏强")
    elif trend_reference_change >= 0.2:
        state = "mild_up"
        risk_level = "medium"
        strategy_mode = "正常"
        reasons.append("市场温和上涨")
    else:
        reasons.append("市场震荡分化")

    if not index_changes and total <= 1:
        state = "unknown"
        risk_level = "unknown"
        strategy_mode = "谨慎"
        max_pick_count = 2
        suggested_position = "0%-10%"
        reasons = ["市场环境数据不足"]

    return {
        "state": state,
        "label": _market_state_label(state),
        "riskLevel": risk_level,
        "strategyMode": strategy_mode,
        "maxPickCount": max_pick_count,
        "suggestedPosition": suggested_position,
        "reasons": reasons,
        "metrics": {
            "up": up,
            "down": down,
            "downRatioPct": round(down_ratio, 1),
            "limitUp": limit_up,
            "limitDown": limit_down,
            "stLimitDown": st_limit_down,
            "nearLimitDown": near_limit_down,
            "totalLimitDown": total_limit_down,
            "limitDownStats": limit_down_summary,
            "mainIndexChangePct": round(main_index_change, 2) if main_index_change is not None else None,
            "averageIndexChangePct": round(average_index_change, 2) if average_index_change is not None else None,
            "weakestEqualWeightedChangePct": round(weakest_equal_weighted_change, 2) if weakest_equal_weighted_change is not None else None,
            "averageEqualWeightedChangePct": round(average_equal_weighted_change, 2) if average_equal_weighted_change is not None else None,
            "lossEffectChangePct": round(weakest_equal_weighted_change, 2) if weakest_equal_weighted_change is not None else None,
            "realIndexCount": len(index_changes),
            "equalWeightedCount": len(equal_weighted_changes),
            "turnoverAmount": round(turnover_amount, 2) if turnover_amount is not None else None,
            "turnoverAmountYi": round(turnover_amount / 100000000, 1) if turnover_amount else None,
            "turnoverRatio5d": round(turnover_ratio_5d, 3) if turnover_ratio_5d is not None else None,
            "lateSessionChangePct": round(late_session_change, 2) if late_session_change is not None else None,
            "marketMode": mode,
            "legacyEqualWeightedIndices": legacy_equal_weighted,
        },
        "thresholds": {
            "panic": "任意3条：真实主要指数<=-2.5%，创业板指/中证1000<=-3%，下跌占比>=80%，自然跌停>=80，ST 跌停显著增加，接近跌停明显扩散，尾盘继续跳水。",
            "heavyDown": "任意2-3条：真实主要指数<=-1.5%，下跌占比>=70%，自然跌停>=40，ST 跌停或接近跌停明显增加，成交额放大或尾盘无修复。",
        },
        "indicatorCompleteness": {
            "complete": not missing_metrics,
            "missing": missing_metrics,
            "warning": "；".join(missing_metrics) if missing_metrics else "",
        },
    }


def _market_state_label(state: str) -> str:
    return {
        "trend_up": "趋势上涨",
        "mild_up": "温和上涨",
        "mixed": "震荡分化",
        "euphoric": "情绪高潮",
        "mild_down": "温和下跌",
        "heavy_down": "放量大跌",
        "panic": "恐慌退潮",
        "unknown": "数据不足",
    }.get(state, state or "--")


def _recommendation_policy(market_context: Dict[str, Any], data_quality_stats: Dict[str, Any], requested_limit: int) -> Dict[str, Any]:
    market_max = int(market_context.get("maxPickCount") if market_context.get("maxPickCount") is not None else requested_limit)
    eligible = int(data_quality_stats.get("eligibleForRealTop") or 0)
    max_pick_count = max(0, min(requested_limit, market_max, eligible))
    reasons = list(market_context.get("reasons") or [])
    trade_date_status = market_context.get("tradeDateStatus") or {}
    if _t1_trade_date_blocks_official(trade_date_status):
        max_pick_count = 0
        reasons.append(_t1_trade_date_block_reason(trade_date_status))
    if eligible <= 0:
        max_pick_count = 0
        reasons.append("没有达到真实可交易标准的分时候选")
    elif max_pick_count < requested_limit:
        reasons.append(f"按市场/数据质量限制，最多推荐 {max_pick_count} 只")
    return {
        "requestedLimit": requested_limit,
        "maxPickCount": max_pick_count,
        "allowZeroPick": True,
        "allowReducePickCount": True,
        "reason": "；".join(reasons),
        "tradeDateGuardBlocked": _t1_trade_date_blocks_official(trade_date_status),
    }


def _candidate_types(candidate: Dict[str, Any]) -> List[str]:
    dimensions = candidate.get("t1Dimensions") or {}
    tradeability = candidate.get("tradeability") or {}
    change = float(candidate.get("change") or 0)
    amount_factor = float(candidate.get("amountFactor") or 0)
    volume_ratio = float(candidate.get("volumeRatio") or 0)
    rel_strength = float(candidate.get("relStrength") or 0)
    sector_status = str(candidate.get("sectorEvidenceStatus") or "unavailable")
    industry_rank = int(candidate.get("industryRank") or 99)
    stock_rank = int(candidate.get("stockRankInIndustry") or 99)
    score = float(candidate.get("score") or 0)
    types: List[str] = []
    if dimensions.get("fundStrength", 0) >= 58 and change > 0:
        types.append("late_money_inflow")
    if sector_status == "available" and (industry_rank <= 5 or stock_rank <= 3):
        types.append("sector_front_runner")
    elif rel_strength >= 88:
        types.append("relative_strength_leader")
    if candidate.get("status") == "pullback" or candidate.get("style") == "回踩低吸":
        types.append("vwap_support")
    if candidate.get("status") == "buy-triggered" or candidate.get("style") == "强势突破":
        types.append("breakout_hold")
    if change >= 7 and tradeability.get("level") != "hard-to-buy":
        types.append("limit_up_candidate")
    if amount_factor >= 1.55 or volume_ratio >= 1.6:
        types.append("high_volume_attention")
    if 0 <= change <= 4 and amount_factor >= 1.15 and score < 68:
        types.append("low_reversal_intraday")
    if dimensions.get("overnightRisk", 0) >= 45 and amount_factor >= 1.4:
        types.append("risk_high_but_active")
    return types or ["high_volume_attention"]


def _candidate_type_label(candidate_type: str) -> str:
    return {
        "late_money_inflow": "尾盘资金回流",
        "sector_front_runner": "真实板块前排",
        "relative_strength_leader": "个股相对强势",
        "vwap_support": "VWAP承接",
        "breakout_hold": "突破维持",
        "limit_up_candidate": "涨停候选",
        "high_volume_attention": "放量高人气",
        "low_reversal_intraday": "日内低位反转",
        "risk_high_but_active": "高风险但资金活跃",
    }.get(candidate_type, candidate_type or "其他")


def _risk_profile(candidate: Dict[str, Any]) -> Dict[str, Any]:
    dimensions = candidate.get("t1Dimensions") or {}
    tradeability = candidate.get("tradeability") or {}
    change = float(candidate.get("change") or 0)
    t1_score = float(candidate.get("t1Score") or candidate.get("score") or 0)
    overnight_risk = float(dimensions.get("overnightRisk") or 0)
    fund_strength = float(dimensions.get("fundStrength") or 0)
    sector_strength = float(dimensions.get("sectorStrength") or 0)
    premium = float(dimensions.get("premiumPotentialScore") or dimensions.get("tomorrowPremium") or 0)
    risk_status = str(dimensions.get("overnightRiskStatus") or "")
    risk_flags = candidate.get("riskFlags") or []
    if isinstance(risk_flags, str):
        risk_flags = [risk_flags]

    if (
        overnight_risk >= 68
        or tradeability.get("level") == "hard-to-buy"
        or "高位加速" in risk_flags
        or (change >= 8.5 and fund_strength < 52)
        or (risk_status == "unknown" and overnight_risk >= 65)
    ):
        return {
            "key": "avoid_overnight",
            "label": "不建议隔夜",
            "tone": "bad",
            "summary": "隔夜风险高于明日溢价证据，除非尾盘重新放量承接，否则不适合纳入最终 Top3。",
        }
    if overnight_risk >= 48 or change >= 6.5 or tradeability.get("level") == "chase-risk":
        return {
            "key": "aggressive",
            "label": "进攻型",
            "tone": "warn",
            "summary": "资金进攻性强，但对尾盘承接和次日竞价要求高，适合小仓位快进快出观察。",
        }
    if t1_score >= 66 and fund_strength >= 52 and sector_strength >= 45 and premium >= 45 and overnight_risk <= 42:
        return {
            "key": "balanced",
            "label": "均衡型",
            "tone": "good",
            "summary": "资金、板块和隔夜风险相对均衡，是 T+1 最优先比较的类型。",
        }
    return {
        "key": "defensive_watch",
        "label": "防守观察型",
        "tone": "neutral",
        "summary": "风险相对可控，但进攻证据不足，适合等尾盘确认或作为备选观察。",
    }


def _ensure_risk_profile(candidate: Dict[str, Any]) -> Dict[str, Any]:
    profile = candidate.get("riskProfile") if isinstance(candidate.get("riskProfile"), dict) else _risk_profile(candidate)
    return {
        "riskProfile": profile,
        "riskProfileKey": profile.get("key", ""),
        "riskProfileLabel": profile.get("label", ""),
    }


def _layered_t1_pool(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    candidate_types = [
        "late_money_inflow",
        "sector_front_runner",
        "relative_strength_leader",
        "vwap_support",
        "breakout_hold",
        "limit_up_candidate",
        "high_volume_attention",
        "low_reversal_intraday",
        "risk_high_but_active",
    ]
    buckets: List[List[Dict[str, Any]]] = []
    for candidate_type in candidate_types:
        typed = [item for item in candidates if candidate_type in (item.get("candidateTypes") or [])]
        buckets.append(sorted(typed, key=lambda item: _type_rank_key(item, candidate_type), reverse=True))
    buckets.append(sorted(candidates, key=_t1_pool_key, reverse=True))
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


def _type_rank_key(candidate: Dict[str, Any], candidate_type: str) -> float:
    dimensions = candidate.get("t1Dimensions") or {}
    base = _t1_pool_key(candidate)
    if candidate_type == "late_money_inflow":
        return base + float(dimensions.get("fundStrength") or 0) * 0.25
    if candidate_type == "sector_front_runner":
        return base + float(dimensions.get("sectorStrength") or 0) * 0.25
    if candidate_type == "relative_strength_leader":
        return base + float(candidate.get("relStrength") or 0) * 0.18
    if candidate_type == "vwap_support":
        return base + float(dimensions.get("intradayStructure") or 0) * 0.18
    if candidate_type == "risk_high_but_active":
        return base - float(dimensions.get("overnightRisk") or 0) * 0.2
    return base


def _t1_pool_key(candidate: Dict[str, Any]) -> float:
    dimensions = candidate.get("t1Dimensions") or {}
    return (
        float(candidate.get("t1Score") or 0)
        + float(candidate.get("relStrength") or 0) * 0.12
        + float(candidate.get("amountFactor") or 0) * 4
        + float(dimensions.get("sectorStrength") or 0) * 0.12
        - float(dimensions.get("overnightRisk") or 0) * 0.12
    )


def _pool_item(candidate: Dict[str, Any], pool_rank: int) -> Dict[str, Any]:
    item = dict(candidate)
    item["poolRank"] = pool_rank
    item["rank"] = pool_rank
    for key in ("series", "macdSeries", "vwapSeries", "ma5", "ma10", "ma20", "ma60"):
        item.pop(key, None)
    return item


def _compact_market(market: Dict[str, Any]) -> Dict[str, Any]:
    breadth = market.get("breadth") or {}
    return {
        "indices": market.get("indices") or [],
        "equalWeightedIndices": market.get("equalWeightedIndices") or [],
        "breadth": {"up": breadth.get("up", 0), "down": breadth.get("down", 0)},
        "limitUp": market.get("limitUp", 0),
        "limitDown": market.get("limitDown", 0),
        "stLimitDown": market.get("stLimitDown", 0),
        "nearLimitDown": market.get("nearLimitDown", 0),
        "totalLimitDown": market.get("totalLimitDown", 0),
        "limitDownStats": market.get("limitDownStats") or {},
        "turnoverAmount": market.get("turnoverAmount"),
        "turnoverAvg5d": market.get("turnoverAvg5d"),
        "turnoverRatio5d": market.get("turnoverRatio5d"),
        "lateSessionChange": market.get("lateSessionChange"),
        "hotSectors": (market.get("hotSectors") or [])[:10],
        "mode": market.get("mode", ""),
        "scanTime": market.get("scanTime", ""),
        "recommendedFinalTime": market.get("recommendedFinalTime", DEFAULT_SCAN_TIME),
        "triggerMode": "manual",
        "timeHint": market.get("timeHint", ""),
        "candidateTypeCounts": market.get("candidateTypeCounts") or {},
        "dataQualityStats": market.get("dataQualityStats") or {},
        "marketContext": market.get("marketContext") or {},
        "tradeDateStatus": market.get("tradeDateStatus") or {},
        "stageControls": market.get("stageControls") or _t1_stage_controls_snapshot(),
        "sectorStateShadowSummary": market.get("sectorStateShadowSummary") or {},
        "styleRegimeShadow": market.get("styleRegimeShadow") or {},
        "recommendationPolicy": market.get("recommendationPolicy") or {},
        "sourceStatus": market.get("sourceStatus") or {},
        "t1Evidence": market.get("t1Evidence") or {},
        "aiParticipation": market.get("aiParticipation") or {},
        "marketIndexStatus": market.get("marketIndexStatus") or {},
        "cacheStatus": market.get("cacheStatus") or {},
        "providerWarning": market.get("providerWarning", ""),
        "aiEvidenceValidationStatus": market.get("aiEvidenceValidationStatus", ""),
        "aiEvidenceValidationErrors": market.get("aiEvidenceValidationErrors") or [],
    }


def _prompt_contract(stage: str) -> Dict[str, Any]:
    if stage == "batch":
        return {
            "version": "v2_batch_structured_selection_slip",
            "candidatePayload": "batch_layered_minimal",
            "outputProtocol": AI_SELECTION_OUTPUT_PROTOCOL,
            "purpose": "批次初筛只做半决赛入围，不生成最终 Top3 结论。",
            "layers": [
                "candidateCore=股票身份和候选类型",
                "scorePack=启发式评分，不是概率或收益承诺",
                "tradeabilityPack=尾盘买入可行性和分时承接",
                "qualityPack=数据可用性和真实 Top 资格",
                "boardProxy=上市板块代理，只能描述交易板块/市场广度",
                "sectorEvidencePack=真实行业/概念证据和 allowedClaims",
            ],
            "sectorRule": "板块结论只能来自 sectorEvidencePack.allowedClaims 和 evidenceRefs；缺失层只能写入 missingEvidence 或风险。",
            "boardRule": "boardProxy.listingBoard 不是行业或概念，marketBoardLimitUpCount 不是行业涨停数。",
            "internalAggregateRule": "candidate_pool_aggregate 只能写 T+1 候选池内样本强弱，不能写全市场行业统计、主力净流入或资金共振。",
            "outputRule": "只输出 code/rank/supportingEvidenceRefs/counterEvidenceRefs/decisionTags/confidence；不要输出 reason/sectorFit/marketFit 等自然语言事实。",
            "allowedDecisionTags": sorted(AI_DECISION_TAG_LABELS.keys()),
        }
    return {
        "version": "v2_final_structured_selection_slip",
        "candidatePayload": "final_full_candidate",
        "outputProtocol": AI_SELECTION_OUTPUT_PROTOCOL,
        "purpose": "终选阶段在半决赛池内比较最终 Top0-Top3。",
        "sectorRule": "仍必须遵守 evidenceRefs、sectorEvidence.evidenceLayers 和 allowedClaims。",
        "scoreRule": "premiumPotentialScore 是次日溢价潜力评分，非概率。",
        "outputRule": "只输出 code/rank/supportingEvidenceRefs/counterEvidenceRefs/decisionTags/confidence；后端根据证据生成页面理由。",
        "allowedDecisionTags": sorted(AI_DECISION_TAG_LABELS.keys()),
    }


def _compact_candidate_for_prompt(candidate: Dict[str, Any], stage: str, include_sector_state: bool) -> Dict[str, Any]:
    if stage == "batch":
        return _compact_batch_candidate(candidate, include_sector_state=include_sector_state)
    item = _compact_candidate(candidate)
    if not include_sector_state:
        return _without_shadow_sector_prompt_fields(item)
    return item


def _compact_batch_candidate(candidate: Dict[str, Any], include_sector_state: bool) -> Dict[str, Any]:
    dimensions = candidate.get("t1Dimensions") or {}
    evidence = candidate.get("sectorEvidence") or {}
    industry = str(candidate.get("industrySector") or "")
    concepts = [str(item).strip() for item in candidate.get("conceptSectors") or [] if str(item or "").strip()]
    layers = _sector_layers_from_evidence(evidence, industry, concepts)
    data_quality = candidate.get("dataQuality") or {}
    sector_quality = data_quality.get("sectorQuality") or _sector_quality_from_semantics(candidate)
    evidence_refs = list(candidate.get("evidenceRefs") or [])
    if not include_sector_state:
        evidence_refs = [
            ref
            for ref in evidence_refs
            if not isinstance(ref, dict) or str(ref.get("id") or "") != "S2"
        ]
    sector_pack: Dict[str, Any] = {
        "industrySector": industry,
        "conceptSectors": concepts,
        "sectorEvidenceStatus": candidate.get("sectorEvidenceStatus") or evidence.get("status") or "unavailable",
        "summary": _sector_evidence_summary(candidate),
        "allowedClaims": list(evidence.get("allowedClaims") or []),
        "evidenceLayers": layers,
        "sectorSnapshot": {
            "sourceKind": candidate.get("sectorSnapshotSourceKind") or evidence.get("sectorSnapshotSourceKind") or "unknown",
            "scope": candidate.get("sectorSnapshotScope") or evidence.get("sectorSnapshotScope") or "unknown",
            "industryLimitUpCount": candidate.get("industryLimitUpCount"),
            "conceptLimitUpCount": candidate.get("conceptLimitUpCount"),
            "conceptLimitUpCounts": candidate.get("conceptLimitUpCounts") or [],
            "industryRank": candidate.get("industryRank"),
            "industrySectorSampleCount": candidate.get("industrySectorSampleCount"),
            "matchedSectors": _compact_sector_matches_for_prompt(evidence.get("matchedSectors") or []),
        },
        "constituentRank": {
            "enabled": _t1_layer_available(layers, "constituentRank"),
            "stockRankInIndustry": candidate.get("stockRankInIndustry") if _t1_layer_available(layers, "constituentRank") else 0,
        },
        "evidenceRefs": evidence_refs,
    }
    if include_sector_state:
        sector_pack["sectorState"] = {
            "refId": "S2",
            "state": candidate.get("sectorStateShadow") or "data_insufficient",
            "label": candidate.get("sectorStateLabel") or T1_SECTOR_STATE_LABELS.get(str(candidate.get("sectorStateShadow") or ""), "证据不足"),
            "confidence": candidate.get("sectorStateConfidence"),
            "riskScore": candidate.get("sectorRiskScore"),
            "flowInterpretation": candidate.get("sectorFlowInterpretation") or "",
            "reasons": candidate.get("sectorStateReasons") or [],
            "exceptionEligible": bool(candidate.get("sectorExceptionEligible")),
        }
    return {
        "code": candidate.get("code"),
        "name": candidate.get("name"),
        "candidateCore": {
            "code": candidate.get("code"),
            "name": candidate.get("name"),
            "poolRank": candidate.get("poolRank"),
            "candidateType": candidate.get("candidateType"),
            "candidateTypeLabel": candidate.get("candidateTypeLabel"),
            "candidateTypes": candidate.get("candidateTypes") or [],
            "status": candidate.get("status"),
            "style": candidate.get("style"),
            "theme": candidate.get("theme"),
        },
        "scorePack": {
            "scoreMeaning": "所有 score/strength/premium 字段均为启发式评分，不是概率或收益承诺。",
            "t1Score": candidate.get("t1Score"),
            "ruleScore": candidate.get("score"),
            "relStrength": candidate.get("relStrength"),
            "change": candidate.get("change"),
            "turnover": candidate.get("turnover"),
            "volumeRatio": candidate.get("volumeRatio"),
            "amountFactor": candidate.get("amountFactor"),
            "fundStrength": dimensions.get("fundStrength"),
            "sectorStrength": dimensions.get("sectorStrength"),
            "intradayStructure": dimensions.get("intradayStructure"),
            "premiumPotentialScore": dimensions.get("premiumPotentialScore") or dimensions.get("tomorrowPremium"),
            "premiumPotentialLabel": dimensions.get("premiumPotentialLabel") or "次日溢价潜力评分，非概率",
        },
        "riskPack": {
            "riskProfile": candidate.get("riskProfile") or {},
            "riskProfileLabel": candidate.get("riskProfileLabel", ""),
            "overnightRisk": dimensions.get("overnightRisk"),
            "overnightRiskStatus": dimensions.get("overnightRiskStatus"),
            "overnightRiskBreakdown": (dimensions.get("overnightRiskBreakdown") or [])[:5],
            "riskFlags": candidate.get("riskFlags") or [],
            "invalidCondition": candidate.get("invalidCondition"),
        },
        "tradeabilityPack": {
            "tradeability": candidate.get("tradeability") or {},
            "lastVwap": candidate.get("lastVwap"),
            "morningHigh": candidate.get("morningHigh"),
            "morningLow": candidate.get("morningLow"),
            "buyWindow": candidate.get("buyWindow"),
            "sellWindow": candidate.get("sellWindow"),
        },
        "qualityPack": {
            "grade": data_quality.get("grade"),
            "label": data_quality.get("label"),
            "quoteQuality": data_quality.get("quoteQuality"),
            "sectorQuality": sector_quality,
            "marketQuality": data_quality.get("marketQuality"),
            "eventQuality": data_quality.get("eventQuality"),
            "decisionEligibility": data_quality.get("decisionEligibility"),
            "eligibleForRealTop": data_quality.get("eligibleForRealTop"),
            "unavailableEvidence": data_quality.get("unavailableEvidence") or [],
            "reasons": (data_quality.get("reasons") or [])[:6],
        },
        "boardProxy": {
            "listingBoard": candidate.get("listingBoard"),
            "marketBoardLimitUpCount": candidate.get("marketBoardLimitUpCount"),
            "marketBoardRank": candidate.get("marketBoardRank"),
            "semanticLevel": "listing_board_proxy",
            "allowedUse": "只用于上市交易板块、交易制度或市场广度；禁止当作行业/概念强度、行业涨停数、行业资金或行业排名。",
        },
        "sectorEvidencePack": sector_pack,
    }


def _compact_sector_matches_for_prompt(matches: List[Dict[str, Any]], limit: int = 4) -> List[Dict[str, Any]]:
    result = []
    for item in matches[:limit]:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        row = {
            "sectorType": item.get("sectorType"),
            "sectorName": item.get("sectorName"),
            "source": source,
            "rank": item.get("rank") or item.get("ruleRank"),
            "changePct": item.get("changePct"),
            "limitUpCount": item.get("limitUpCount"),
            "sampleCount": item.get("sampleCount") or item.get("constituentSampleCount"),
            "coverageStatus": item.get("coverageStatus"),
        }
        if not _sector_source_is_internal(source):
            row["mainNetInflow"] = item.get("mainNetInflow")
            row["fundFlowStatus"] = item.get("fundFlowStatus")
        result.append(row)
    return result


def _compact_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "code": candidate.get("code"),
        "name": candidate.get("name"),
        "sector": candidate.get("sector"),
        "legacySector": candidate.get("legacySector"),
        "listingBoard": candidate.get("listingBoard"),
        "industrySector": candidate.get("industrySector"),
        "conceptSectors": candidate.get("conceptSectors") or [],
        "marketBoardLimitUpCount": candidate.get("marketBoardLimitUpCount"),
        "marketBoardRank": candidate.get("marketBoardRank"),
        "industryLimitUpCount": candidate.get("industryLimitUpCount"),
        "conceptLimitUpCount": candidate.get("conceptLimitUpCount"),
        "conceptLimitUpCounts": candidate.get("conceptLimitUpCounts") or [],
        "industryRank": candidate.get("industryRank"),
        "stockRankInIndustry": candidate.get("stockRankInIndustry"),
        "sectorEvidenceStatus": candidate.get("sectorEvidenceStatus"),
        "sectorEvidence": candidate.get("sectorEvidence") or {},
        "sectorEvidenceSummary": _sector_evidence_summary(candidate),
        "sectorSnapshotSourceKind": candidate.get("sectorSnapshotSourceKind"),
        "sectorSnapshotScope": candidate.get("sectorSnapshotScope"),
        "industrySectorSampleCount": candidate.get("industrySectorSampleCount"),
        "sectorStateShadow": candidate.get("sectorStateShadow") or "data_insufficient",
        "sectorStateLabel": candidate.get("sectorStateLabel") or T1_SECTOR_STATE_LABELS.get(str(candidate.get("sectorStateShadow") or ""), "证据不足"),
        "sectorStateConfidence": candidate.get("sectorStateConfidence"),
        "sectorRiskScore": candidate.get("sectorRiskScore"),
        "sectorFlowInterpretation": candidate.get("sectorFlowInterpretation") or "",
        "sectorStateVersion": candidate.get("sectorStateVersion") or "",
        "sectorStateStage": candidate.get("sectorStateStage") or "shadow",
        "sectorStateReasons": candidate.get("sectorStateReasons") or [],
        "primarySectorStateSource": candidate.get("primarySectorStateSource") or "",
        "sectorExceptionEligible": bool(candidate.get("sectorExceptionEligible")),
        "evidenceRefs": candidate.get("evidenceRefs") or [],
        "theme": candidate.get("theme"),
        "poolRank": candidate.get("poolRank"),
        "candidateType": candidate.get("candidateType"),
        "candidateTypeLabel": candidate.get("candidateTypeLabel"),
        "candidateTypes": candidate.get("candidateTypes"),
        "riskProfile": candidate.get("riskProfile") or {},
        "riskProfileLabel": candidate.get("riskProfileLabel", ""),
        "t1Score": candidate.get("t1Score"),
        "t1Dimensions": candidate.get("t1Dimensions"),
        "ruleScore": candidate.get("score"),
        "status": candidate.get("status"),
        "style": candidate.get("style"),
        "change": candidate.get("change"),
        "turnover": candidate.get("turnover"),
        "volumeRatio": candidate.get("volumeRatio"),
        "amountFactor": candidate.get("amountFactor"),
        "relStrength": candidate.get("relStrength"),
        "sectorRank": candidate.get("sectorRank"),
        "sectorLimitUps": candidate.get("sectorLimitUps"),
        "legacyFieldWarning": "sector/sectorRank/sectorLimitUps 是兼容旧字段；若 legacySector 是沪市主板/创业板/科创板等上市板块，不得当作行业或概念证据。",
        "riskFlags": candidate.get("riskFlags"),
        "tradeability": candidate.get("tradeability") or {},
        "dataQuality": candidate.get("dataQuality") or {},
        "lastVwap": candidate.get("lastVwap"),
        "morningHigh": candidate.get("morningHigh"),
        "morningLow": candidate.get("morningLow"),
        "buyWindow": candidate.get("buyWindow"),
        "sellWindow": candidate.get("sellWindow"),
        "invalidCondition": candidate.get("invalidCondition"),
        "tailSupportPack": candidate.get("tailSupportPackV2") or {},
        "gapDownRiskPack": candidate.get("gapDownRiskPack") or {},
    }


def _without_shadow_sector_prompt_fields(candidate: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(candidate)
    for key in (
        "sectorStateShadow",
        "sectorStateLabel",
        "sectorStateConfidence",
        "sectorRiskScore",
        "sectorFlowInterpretation",
        "sectorStateVersion",
        "sectorStateStage",
        "sectorStateReasons",
        "primarySectorStateSource",
        "sectorExceptionEligible",
    ):
        item.pop(key, None)
    evidence = dict(item.get("sectorEvidence") or {})
    for key in (
        "sectorStateShadow",
        "sectorStateLabel",
        "sectorStateConfidence",
        "sectorRiskScore",
        "sectorFlowInterpretation",
        "sectorStateVersion",
        "sectorStateStage",
        "sectorStateReasons",
        "sectorExceptionEligible",
    ):
        evidence.pop(key, None)
    item["sectorEvidence"] = evidence
    item["evidenceRefs"] = [
        ref
        for ref in item.get("evidenceRefs") or []
        if not isinstance(ref, dict) or str(ref.get("id") or "") != "S2"
    ]
    dimensions = dict(item.get("t1Dimensions") or {})
    for key in (
        "sectorStateShadow",
        "sectorStateLabel",
        "sectorStateConfidence",
        "sectorRiskScore",
        "sectorStateRiskShadow",
        "overnightRiskWithSectorShadow",
        "sectorStateStage",
        "sectorStateAffectsScore",
    ):
        dimensions.pop(key, None)
    item["t1Dimensions"] = dimensions
    return item


def _user_prompt(
    candidates: List[Dict[str, Any]],
    market: Dict[str, Any],
    limit: int,
    stage: str,
    feedback: Optional[Dict[str, Any]] = None,
) -> str:
    instruction = _final_prompt(limit) if stage == "final" else _batch_prompt(limit)
    feedback = feedback or {}
    use_feedback = stage == "final"
    compact_market = _compact_market(market)
    stage_controls = compact_market.get("stageControls") or {}
    sector_state_prompt_enabled = _stage_control_enabled(stage_controls, "sectorStatePrompt", T1_SECTOR_STATE_PROMPT_ENABLED)
    style_regime_prompt_enabled = _stage_control_enabled(stage_controls, "styleRegimePrompt", T1_STYLE_REGIME_PROMPT_ENABLED)
    compact_candidates = [
        _compact_candidate_for_prompt(item, stage=stage, include_sector_state=sector_state_prompt_enabled)
        for item in candidates
    ]
    if not sector_state_prompt_enabled:
        compact_market.pop("sectorStateShadowSummary", None)
    if not style_regime_prompt_enabled:
        compact_market.pop("styleRegimeShadow", None)
    payload = {
        "market": compact_market,
        "task": {
            "selectionMode": "ai_t1_overnight",
            "stage": stage,
            "limit": limit,
            "triggerMode": "manual",
            "goal": "今天买入，下个交易日卖出，最终只推荐最适合 T+1 隔夜超短线的股票。",
            "allowFewerThanLimit": True,
            "allowZeroPick": True,
        },
        "promptContract": _prompt_contract(stage),
        "objectiveStrategyFeedback": _objective_strategy_feedback(feedback) if use_feedback else {"enabled": False},
        "confirmedStrategyConstraints": (feedback.get("activeConstraints") or []) if use_feedback else [],
        "candidates": compact_candidates,
    }
    return (
        f"{instruction}\n\n"
        "下面是结构化输入。请只根据 JSON 中的 market、task、objectiveStrategyFeedback、confirmedStrategyConstraints、candidates 判断。"
        "客观复盘只描述事实；只有 confirmedStrategyConstraints 是用户已确认的策略约束。\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _ai_input_fingerprint(
    provider: str,
    model: str,
    stage: str,
    limit: int,
    system_prompt: str,
    user_prompt: str,
) -> str:
    payload = {
        "provider": provider,
        "model": model,
        "stage": stage,
        "limit": limit,
        "systemPrompt": system_prompt,
        "userPrompt": user_prompt,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _record_ai_call_audit(market: Dict[str, Any], entry: Dict[str, Any]) -> None:
    audits = market.get("aiCallAudits")
    if not isinstance(audits, list):
        audits = []
    clean = dict(entry)
    clean["createdAt"] = datetime.now().isoformat(timespec="seconds")
    if clean.get("prompt"):
        prompt = str(clean["prompt"])
        clean["promptLength"] = len(prompt)
        clean["promptHash"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        clean["promptTruncated"] = len(prompt) > 120000
        clean["prompt"] = prompt[:120000]
    if clean.get("rawResponse"):
        raw = str(clean["rawResponse"])
        clean["rawResponseLength"] = len(raw)
        clean["rawResponseHash"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        clean["rawResponseTruncated"] = len(raw) > 60000
        clean["rawResponse"] = raw[:60000]
    audits.append(clean)
    market["aiCallAudits"] = audits[-AI_CALL_AUDIT_LIMIT:]


def _ai_zero_pick_meta(stage: str, batch_index: int, candidate_count: int, limit: int, cached: bool = False) -> Dict[str, Any]:
    return {
        "version": AI_EVIDENCE_VALIDATION_VERSION,
        "status": "zero_pick",
        "stage": stage,
        "batchIndex": batch_index,
        "candidateCount": candidate_count,
        "limit": limit,
        "cached": bool(cached),
        "reason": "DeepSeek 返回 picks=[]，表示本轮没有选出符合条件的候选。",
    }


def _objective_strategy_feedback(feedback: Dict[str, Any]) -> Dict[str, Any]:
    verified = int(feedback.get("verifiedOutcomes") or 0)
    return {
        "enabled": verified > 0,
        "verifiedOutcomes": verified,
        "windowRuns": int(feedback.get("windowRuns") or 0),
        "successRate": feedback.get("successRate"),
        "strongSuccessRate": feedback.get("strongSuccessRate"),
        "lowOpenFailRate": feedback.get("lowOpenFailRate"),
        "pullbackRate": feedback.get("pullbackRate"),
        "avgNetReturnPct": feedback.get("avgNetReturnPct"),
        "avgMaxDrawdownPct": feedback.get("avgMaxDrawdownPct"),
        "summary": _feedback_prompt_summary(feedback),
        "bestPatterns": _compact_patterns(feedback.get("bestPatterns") or []),
        "worstPatterns": _compact_patterns(feedback.get("worstPatterns") or []),
    }


def _compact_patterns(patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "pattern": item.get("pattern"),
            "count": item.get("count"),
            "successRate": item.get("successRate"),
            "strongSuccessRate": item.get("strongSuccessRate"),
            "avgNetReturnPct": item.get("avgNetReturnPct"),
            "avgMaxDrawdownPct": item.get("avgMaxDrawdownPct"),
        }
        for item in patterns[:5]
    ]


def _role_prompt(stage: str) -> str:
    return (
        "你是一个规则约束下的 A 股超短线 T+1 选股 AI。"
        "你的任务是从系统提供的候选池中选择适合“今天买入、下个交易日卖出”的隔夜超短线标的。"
        "系统已经完成硬过滤，包括 ST、退市风险、停牌、极低流动性、明显数据异常和不支持市场范围。"
        "你不能选择候选池外股票，不能输出确定性收益表达。"
        "你必须重点比较真实证据支持下的资金强度、行业/概念证据、个股相对地位、分时结构、次日溢价潜力和隔夜风险。"
        "不要只按 t1Score、ruleScore 或涨幅排序，必须横向比较不同 candidateType。"
        "riskProfile 是系统规则画像，用于解释进攻性和隔夜风险；你可以参考它，但不能把高风险说成低风险。"
        "dataQuality 是硬约束，不能把含模拟关键数据或仅缓存观察的数据说成真实可交易信号。"
        "aiParticipation.mode 表示你能参与到什么程度；limited_ai_no_sector 时你仍可比较个股量价和市场风险，但不得写任何正向行业/概念结论。"
        "listingBoard 只是上市板块，例如沪市主板、创业板、科创板；它不是行业或概念。"
        "legacy sector/sectorRank/sectorLimitUps 仅兼容旧字段，不能据此写出行业涨停、行业排名、板块资金共振。"
        "sectorEvidence.evidenceLayers 是板块证据权限：identity=available 才能说所属行业/概念身份；sectorSnapshot=available 才能说今日板块强度、涨跌幅、涨停数或资金；constituentRank=available 才能说行业前排、行业内排名或个股相对地位。"
        "sectorStateShadow 若出现在输入中，是系统阶段1影子计算结果；你只能引用该状态，不能自行计算或猜测行业退潮、轮入、派发、过热。"
        "distribution/retreat/weakening/overheated 是反方或风险证据；若仍入选，必须说明为什么不是普通后排跟风或退潮反抽。"
        "如果 sectorEvidence.sectorSnapshotSourceKind=candidate_pool_aggregate 或 sectorSnapshotScope=candidate_pool，只能说“T+1候选池聚合强弱/候选池内样本/候选池内涨停”，禁止写全市场行业涨停数、外部资金流、主力净流入、资金共振。"
        "当 sectorEvidence.sectorSnapshotSourceKind=mixed 时，只有外部快照对应的行业/概念才可写外部板块事实；自建候选池聚合部分仍禁止资金、主力、净流入、共振。"
        "如果 constituentRank 不是 available，禁止写行业前排、板块前排、排名、龙头、核心标的、相对地位。"
        "行业/概念名称必须逐字复制 candidates 中的 industrySector 或 conceptSectors，禁止新增简称、泛称或带“等”的概念名，例如不能把“航天军工”改写为“军工等”。"
        "sectorEvidence.allowedClaims 是最终可用结论清单，必须遵守；如果某层 unavailable/stale，只能把它写入 missingEvidence 或风险，不能写成正向理由。"
        "batch 阶段 candidates 已按 candidateCore、scorePack、tradeabilityPack、qualityPack、boardProxy、sectorEvidencePack 拆层；先看 sectorEvidencePack.allowedClaims，再看 sectorEvidencePack.summary，最后才看分数。"
        "premiumPotentialScore 是次日溢价潜力评分，不是上涨概率，禁止写成“概率”或“99%”。"
        "每个入选项必须引用 supportingEvidenceRefs；引用值只能来自候选的 evidenceRefs.id。"
        "tail_support 只能在候选存在可用 T1 尾盘承接证据且 supportingEvidenceRefs 包含 T1 时使用；没有 T1 时禁止使用该标签。"
        "宁可少选，也不要用不被证据支持的话术补理由；后端会剔除不合规候选。"
        "marketContext 是总开关，恐慌退潮或放量大跌时允许少选甚至不选。"
        "输出严格 JSON，根字段 picks；每项只包含 code、rank、supportingEvidenceRefs、counterEvidenceRefs、decisionTags、confidence。"
        "不要输出 reason、strategyHypothesis、buyPlan、sellPlan、risk、entryCondition、invalidCondition、marketFit、sectorFit、confidenceReason；这些页面文字由后端根据冻结证据生成。"
        f"decisionTags 只能来自：{','.join(sorted(AI_DECISION_TAG_LABELS.keys()))}。"
        "禁止 Markdown，禁止必涨、稳赚、满仓、无风险等确定性表达。"
        f"当前阶段：{stage}。"
    )


def _batch_prompt(limit: int) -> str:
    return (
        "当前是超短线 T+1 推荐的批次初筛阶段。\n"
        f"请从 candidates 中选择最多 {limit} 只进入半决赛池。\n"
        "选择目标：今天接近尾盘仍有资金承接、板块不退潮、明天有冲高或溢价预期，适合今天买入、下个交易日卖出的候选。\n"
        "不能只按规则分或涨幅排序。必须比较尾盘资金回流、VWAP 承接、真实行业/概念证据、个股相对地位、成交额和量比、是否追高、次日溢价潜力、隔夜风险。\n"
        "板块适配只通过 evidenceRefs 和 decisionTags 表达；不要写自然语言板块事实，不要使用资金共振、主力净流入、行业前排、龙头等未经证据允许的词。\n"
        "如果没有足够证据，不要为了凑数量强行选择。\n"
        "输出 JSON：{\"picks\":[{\"code\":\"股票代码，必须来自 candidates\",\"rank\":1,\"supportingEvidenceRefs\":[\"Q1\",\"M1\"],\"counterEvidenceRefs\":[\"R1\"],\"decisionTags\":[\"premium_potential\",\"low_overnight_risk\"],\"confidence\":62}]}\n"
        f"要求：picks 最多 {limit} 个；code 必须完全来自 candidates；confidence 是相对信心，不是收益承诺。"
    )


def _final_prompt(limit: int) -> str:
    return (
        "当前是超短线 T+1 Top3 终选阶段。\n"
        f"请从 semifinal candidates 中选择最多 {limit} 只，作为今天买入、下个交易日卖出的最终推荐。\n"
        "最终选择要求：必须优先考虑尾盘资金态度、真实行业/概念持续性和个股辨识度；不要只选涨幅最高；不要选择尾盘跳水、炸板回落、放量滞涨且没有承接证据的股票。"
        "如果选择高位股，必须用 counterEvidenceRefs 和 decisionTags 表达你已接受哪些风险；如果选择非涨停股，必须引用支持其次日溢价潜力的证据 ID。\n"
        "不要输出自然语言 marketFit/sectorFit/reason；后端会按你引用的证据 ID 生成页面说明。\n"
        "如果市场环境或候选质量不足，可以少于上限；不要为了凑满数量选择不适合隔夜的股票。\n"
        "输出 JSON：{\"picks\":[{\"code\":\"股票代码，必须来自 candidates\",\"rank\":1,\"supportingEvidenceRefs\":[\"Q1\",\"M1\",\"R1\"],\"counterEvidenceRefs\":[\"S2\"],\"decisionTags\":[\"premium_potential\",\"counter_risk_accepted\"],\"confidence\":62}]}\n"
        f"要求：picks 最多 {limit} 个；code 必须完全来自 candidates；confidence 是相对信心，不是收益承诺。"
    )


def _normalize_decision_tags(value: Any) -> List[str]:
    if isinstance(value, str):
        raw = [item.strip() for item in re.split(r"[,，、\s]+", value) if item.strip()]
    elif isinstance(value, list):
        raw = [str(item).strip() for item in value if str(item or "").strip()]
    else:
        raw = []
    return _unique_strings([item for item in raw if item in AI_DECISION_TAG_LABELS])


def _ignored_decision_tags(value: Any) -> List[str]:
    if isinstance(value, str):
        raw = [item.strip() for item in re.split(r"[,，、\s]+", value) if item.strip()]
    elif isinstance(value, list):
        raw = [str(item).strip() for item in value if str(item or "").strip()]
    else:
        raw = []
    return _unique_strings([item for item in raw if item and item not in AI_DECISION_TAG_LABELS])


def _candidate_evidence_by_id(candidate: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(item.get("id") or ""): item
        for item in candidate.get("evidenceRefs") or []
        if isinstance(item, dict) and item.get("id")
    }


def _evidence_summaries(candidate: Dict[str, Any], refs: List[str]) -> List[str]:
    evidence = _candidate_evidence_by_id(candidate)
    summaries = []
    for ref in refs:
        item = evidence.get(ref)
        if not item:
            continue
        summary = str(item.get("summary") or "").strip()
        status = str(item.get("status") or "").strip()
        prefix = f"{ref}"
        if status:
            prefix += f"({status})"
        summaries.append(f"{prefix}: {summary}" if summary else prefix)
    return summaries


def _plain_missing_evidence(candidate: Dict[str, Any], support_refs: List[str], counter_refs: List[str]) -> List[str]:
    missing = []
    evidence = _candidate_evidence_by_id(candidate)
    for ref in support_refs + counter_refs:
        item = evidence.get(ref)
        status = str((item or {}).get("status") or "")
        if status == "partial":
            if ref == "M1":
                missing.append("市场数据不完整，市场判断只能作为参考。")
            elif ref == "R1":
                missing.append("隔夜风险里有估算项，不能当作确定结论。")
            elif ref in {"S1", "S2"}:
                missing.append("板块数据不完整，不能过度依赖板块强度。")
            else:
                missing.append(f"{_plain_ref_labels([ref])}不完整，只能参考。")
        elif status == "stale":
            missing.append(f"{_plain_ref_labels([ref])}不是最新数据，只能参考。")
        elif status == "unavailable":
            missing.append(f"{_plain_ref_labels([ref])}缺失，不能作为主要依据。")
    if "S1" not in support_refs and "S2" not in support_refs:
        missing.append("板块不是这次推荐的主要加分项。")
    return _unique_strings(missing)


def _plain_market_fit(candidate: Dict[str, Any], support_refs: List[str], counter_refs: List[str]) -> str:
    evidence = _candidate_evidence_by_id(candidate).get("M1") or {}
    summary = str(evidence.get("summary") or "")
    status = str(evidence.get("status") or "")
    if "趋势上涨" in summary or "趋势偏强" in summary:
        base = "大盘环境偏强，对隔夜溢价有帮助。"
    elif "下跌" in summary or "偏弱" in summary or "恐慌" in summary:
        base = "大盘环境偏弱，要降低预期，不能恋战。"
    elif "震荡" in summary or "分化" in summary:
        base = "大盘处于震荡分化，适合轻仓观察，不适合追高。"
    else:
        base = "市场环境不是主要加分项，需要结合尾盘走势再确认。"
    if status == "partial":
        base += " 但市场指标不完整，这条只能作为参考。"
    return base


def _plain_sector_fit(candidate: Dict[str, Any], support_refs: List[str], counter_refs: List[str]) -> str:
    industry = str(candidate.get("industrySector") or "").strip()
    concepts = [str(item) for item in candidate.get("conceptSectors") or [] if str(item).strip()]
    status = str(candidate.get("sectorEvidenceStatus") or "")
    sector_state = str(candidate.get("sectorStateShadow") or "")
    pieces = []
    if industry:
        pieces.append(f"所属行业：{industry}")
    if concepts:
        pieces.append(f"相关概念：{'、'.join(concepts[:4])}")
    if sector_state in {"retreat", "distribution", "weakening"}:
        pieces.append("板块有转弱迹象，不能只看个股强。")
    elif sector_state in {"strong", "rotation_in", "oversold_repair"}:
        pieces.append("板块状态对它有一定支持。")
    else:
        pieces.append("板块不是唯一依据，重点仍看尾盘承接和次日溢价。")
    if status in {"partial", "stale", "unavailable"}:
        pieces.append("板块证据不完整，不能直接理解成板块龙头或全行业很强。")
    return "；".join(pieces)


def _plain_overnight_risk(candidate: Dict[str, Any], counter_refs: List[str]) -> str:
    dimensions = candidate.get("t1Dimensions") or {}
    risk_value = dimensions.get("overnightRisk")
    status = str(dimensions.get("overnightRiskStatus") or "")
    try:
        value = float(risk_value)
    except (TypeError, ValueError):
        value = None
    if value is None:
        text = "隔夜风险暂时无法完整判断。"
    elif value < 25:
        text = f"隔夜风险偏低，当前风险分 {value:.1f}。"
    elif value < 50:
        text = f"隔夜风险中等，当前风险分 {value:.1f}。"
    else:
        text = f"隔夜风险偏高，当前风险分 {value:.1f}。"
    if status in {"partial", "unknown"}:
        text += " 其中有部分数据是估算或不完整，明天开盘不强就要快点处理。"
    elif counter_refs:
        text += " 系统已把风险提醒纳入比较。"
    return text


def _short_refs(refs: List[str]) -> str:
    return "/".join(refs) if refs else "未引用"


def _plain_ref_labels(refs: List[str]) -> str:
    labels = {
        "Q1": "价格和成交量",
        "M1": "市场环境",
        "S1": "行业/概念证据",
        "S2": "板块状态",
        "R1": "隔夜风险",
        "T1": "尾盘承接",
        "G1": "低开风险",
    }
    values = [labels.get(str(ref), str(ref)) for ref in refs or []]
    return "、".join(_unique_strings(values)) if values else "暂无明确证据"


def _tag_labels(tags: List[str]) -> List[str]:
    return [AI_DECISION_TAG_LABELS[tag] for tag in tags if tag in AI_DECISION_TAG_LABELS]


def _t1_tail_buy_plan(candidate: Dict[str, Any]) -> str:
    scan_time = str(candidate.get("scanTime") or DEFAULT_SCAN_TIME)
    return f"今天尾盘信号确认后再买，参考 {scan_time} 附近价格；如果信号出来后快速拉高、跌破 VWAP 或放量跳水，就放弃。"


def _t1_next_day_sell_plan() -> str:
    return "下个交易日主要赚开盘溢价：开盘先观察，09:35 到 09:45 有冲高优先兑现；低开后继续走弱就尽快降低仓位。"


def _plain_t1_reason(stage: str, support_refs: List[str], counter_refs: List[str], tags: List[str], confidence: Optional[float]) -> str:
    support = _plain_ref_labels(support_refs)
    counters = _plain_ref_labels(counter_refs)
    tag_labels = _tag_labels(tags)
    tag_text = "、".join(tag_labels[:4]) if tag_labels else "尾盘承接和次日溢价"
    prefix = "AI最终选择" if stage == "final" else "AI初步筛选"
    confidence_text = f"，信心 {confidence:.0f}/100" if confidence is not None else ""
    return f"{prefix}这只股票，是因为它在 {support} 上更占优，同时已检查 {counters}。核心看点：{tag_text}{confidence_text}。"


def _ai_selection_slip_missing_evidence(candidate: Dict[str, Any], support_refs: List[str], counter_refs: List[str]) -> List[str]:
    return _plain_missing_evidence(candidate, support_refs, counter_refs)


def _hydrate_ai_selection_slip(candidate: Dict[str, Any], pick: Dict[str, Any], stage: str) -> Dict[str, Any]:
    support_refs = _normalize_evidence_refs(pick.get("supportingEvidenceRefs") or pick.get("supporting_evidence_refs"))
    counter_refs = _normalize_evidence_refs(pick.get("counterEvidenceRefs") or pick.get("counter_evidence_refs"))
    tags = _normalize_decision_tags(pick.get("decisionTags") or pick.get("decision_tags"))
    ignored_tags = _ignored_decision_tags(pick.get("decisionTags") or pick.get("decision_tags"))
    confidence = _normalize_confidence(pick.get("confidence") if pick.get("confidence") is not None else pick.get("score"))
    tag_text = "、".join(_tag_labels(tags)) or "未给出标签"
    dimensions = candidate.get("t1Dimensions") or {}
    missing = _normalize_string_list(pick.get("missingEvidence") or pick.get("missing_evidence"))
    missing.extend(_ai_selection_slip_missing_evidence(candidate, support_refs, counter_refs))
    plain_reason = _plain_t1_reason(stage, support_refs, counter_refs, tags, confidence)
    generated = {
        "aiOutputProtocol": AI_SELECTION_OUTPUT_PROTOCOL,
        "decisionTags": tags,
        "ignoredDecisionTags": ignored_tags,
        "supportingEvidenceRefs": support_refs,
        "counterEvidenceRefs": counter_refs,
        "missingEvidence": _unique_strings(missing),
        "confidence": confidence,
        "reason": plain_reason,
        "strategyHypothesis": (
            "这不是追当天最高涨幅，而是看今天尾盘买入后，明天开盘附近有没有可兑现的溢价。"
        ),
        "buyPlan": _t1_tail_buy_plan(candidate),
        "sellPlan": _t1_next_day_sell_plan(),
        "risk": _plain_overnight_risk(candidate, counter_refs),
        "entryCondition": "只在今天尾盘信号仍成立时买：价格不能明显跌破均价线，不能放量跳水，盘口要能成交。",
        "invalidCondition": str(candidate.get("invalidCondition") or "") or "尾盘放量跳水、跌破 VWAP、板块证据转弱或市场风险继续扩散。",
        "marketFit": _plain_market_fit(candidate, support_refs, counter_refs),
        "sectorFit": _plain_sector_fit(candidate, support_refs, counter_refs),
        "confidenceReason": (
            f"AI 信心 {confidence if confidence is not None else '--'}/100；"
            f"主要看 {_plain_ref_labels(support_refs)}，同时注意 {_plain_ref_labels(counter_refs)}。"
        ),
    }
    return generated


def _apply_picks(candidates: List[Dict[str, Any]], picks: List[Dict[str, Any]], limit: int, batch_index: int) -> List[Dict[str, Any]]:
    by_code = {item.get("code"): item for item in candidates}
    selected = []
    seen = set()
    for rank, pick in enumerate(sorted(picks, key=lambda item: _safe_rank(item.get("rank"))), start=1):
        code = _normalize_code(pick.get("code"))
        if not code or code in seen or code not in by_code:
            continue
        base = dict(by_code[code])
        base["aiRank"] = rank
        base["batchIndex"] = batch_index
        base["selected"] = True
        base.update(_hydrate_ai_selection_slip(base, pick, "final" if batch_index <= 0 else "batch"))
        base["aiEvidenceValidation"] = pick.get("aiEvidenceValidation") or {"version": AI_EVIDENCE_VALIDATION_VERSION, "status": "unknown", "errors": []}
        base["selectionSource"] = "ai-t1-overnight"
        base.update(_ensure_risk_profile(base))
        selected.append(base)
        seen.add(code)
        if len(selected) >= limit:
            break
    return selected


def _rules_fallback(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    eligible = [item for item in candidates if (item.get("dataQuality") or {}).get("eligibleForRealTop", True)]
    ranked = sorted(eligible, key=lambda item: float(item.get("t1Score") or item.get("score") or 0), reverse=True)
    result = []
    for rank, item in enumerate(ranked[:limit], start=1):
        row = dict(item)
        row["aiRank"] = rank
        row["selected"] = True
        row["selectionSource"] = "rules-fallback"
        row["reason"] = "AI T+1 超短线筛选失败，按本地 T+1 综合分兜底。"
        row["buyPlan"] = "仅作为规则兜底观察，需人工确认尾盘承接和板块强度。"
        row["sellPlan"] = "次日如冲高优先分批止盈；如低开走弱或跌破昨日收盘，降低预期。"
        row["risk"] = "该结果不是 AI T+1 终选。"
        row["strategyHypothesis"] = "规则兜底观察，不作为 DeepSeek 证据校验通过的 AI 推荐。"
        row["supportingEvidenceRefs"] = ["Q1", "M1"]
        row["counterEvidenceRefs"] = ["R1"] if (row.get("t1Dimensions") or {}).get("overnightRisk", 0) else []
        row["missingEvidence"] = ["AI 终选失败或未通过证据校验"]
        row["entryCondition"] = "人工确认尾盘承接、价格未跌破 VWAP 后再考虑。"
        row["marketFit"] = "按本地规则排序，市场适配需人工复核。"
        row["sectorFit"] = _sector_evidence_summary(row)
        row["confidenceReason"] = "规则兜底无 AI 证据校验置信度。"
        row["aiEvidenceValidation"] = {"version": AI_EVIDENCE_VALIDATION_VERSION, "status": "not_ai_fallback", "errors": []}
        row["invalidCondition"] = "尾盘跌破 VWAP、板块退潮、放量滞涨或明显跳水。"
        row.update(_ensure_risk_profile(row))
        result.append(row)
    return result


def _ai_semifinal_rules_final(candidates: List[Dict[str, Any]], limit: int, final_error: str = "") -> List[Dict[str, Any]]:
    eligible = [
        item
        for item in candidates
        if (item.get("dataQuality") or {}).get("eligibleForRealTop", True)
        and str((item.get("aiEvidenceValidation") or {}).get("status") or "") in {"passed", "partial_passed"}
    ]
    ranked = sorted(eligible, key=lambda item: float(item.get("t1Score") or item.get("score") or 0), reverse=True)
    result = []
    for rank, item in enumerate(ranked[:limit], start=1):
        row = dict(item)
        row["aiRank"] = rank
        row["rank"] = rank
        row["selected"] = True
        row["selectionSource"] = "ai-semifinal-rules-final"
        original_reason = str(row.get("reason") or "")
        row["reason"] = (
            "DeepSeek 批次初筛已通过证据校验；终选输出未通过校验，当前在 AI 半决赛池内按本地 T+1 综合分终选。"
            + (f" 原初筛理由：{original_reason}" if original_reason else "")
        )
        row["strategyHypothesis"] = row.get("strategyHypothesis") or "AI 初筛候选保留，规则终选用于避免不合规 AI 终选理由进入结果。"
        row["confidenceReason"] = row.get("confidenceReason") or "终选理由来自本地规则复核，AI 只贡献初筛候选池。"
        missing = list(row.get("missingEvidence") or [])
        if final_error:
            missing.append("DeepSeek 终选证据校验失败，已剔除不合规终选理由。")
        row["missingEvidence"] = _unique_strings(missing)
        row.setdefault("supportingEvidenceRefs", ["Q1", "M1"])
        row.setdefault("counterEvidenceRefs", ["R1"] if (row.get("t1Dimensions") or {}).get("overnightRisk", 0) else [])
        row.setdefault("entryCondition", "尾盘承接不破 VWAP，且市场未继续跳水。")
        row.setdefault("invalidCondition", "尾盘跌破 VWAP、放量滞涨、板块证据转弱或市场快速下行。")
        row.setdefault("marketFit", "来自 AI 初筛池，市场适配仍按本地风险规则复核。")
        row.setdefault("sectorFit", _sector_evidence_summary(row))
        validation = dict(row.get("aiEvidenceValidation") or {})
        validation["status"] = validation.get("status") or "passed"
        validation["finalStage"] = "rules_final_after_ai_semifinal"
        validation["finalStageError"] = final_error[:500] if final_error else ""
        row["aiEvidenceValidation"] = validation
        row.update(_ensure_risk_profile(row))
        result.append(row)
    return result


def _baseline_rule_picks(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    ranked = sorted(
        [item for item in candidates if (item.get("dataQuality") or {}).get("eligibleForRealTop", True)],
        key=_t1_pool_key,
        reverse=True,
    )
    result = []
    for rank, item in enumerate(ranked[:limit], start=1):
        row = dict(item)
        row["aiRank"] = rank
        row["rank"] = rank
        row["selected"] = True
        row["selectionSource"] = "baseline-rules"
        row["reason"] = "规则基线：仅按本地 T+1 综合分排序，不调用 AI 或 Critic。"
        result.append(row)
    return result


def _baseline_simple_change_picks(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    ranked = sorted(
        [item for item in candidates if (item.get("dataQuality") or {}).get("eligibleForRealTop", True)],
        key=lambda item: (float(item.get("change") or 0), float(item.get("amountFactor") or 0)),
        reverse=True,
    )
    return _baseline_pick_rows(ranked, limit, "baseline-momentum", "简单涨幅基线：按当日涨幅和成交额强度排序。")


def _baseline_deterministic_random_picks(candidates: List[Dict[str, Any]], limit: int, seed: str) -> List[Dict[str, Any]]:
    eligible = [item for item in candidates if (item.get("dataQuality") or {}).get("eligibleForRealTop", True)]
    ranked = sorted(eligible, key=lambda item: _stable_random_key(str(item.get("code") or ""), seed))
    return _baseline_pick_rows(ranked, limit, "baseline-random", "随机基线：用推荐日期和代码做稳定随机对照。")


def _baseline_market_pool_picks(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    eligible = [
        item
        for item in candidates
        if (item.get("dataQuality") or {}).get("eligibleForRealTop", True)
    ]
    if not eligible or limit <= 0:
        return []
    ranked = sorted(eligible, key=_t1_pool_key, reverse=True)
    if len(ranked) <= limit:
        selected = ranked
    else:
        top_n = max(1, limit // 3)
        mid_n = max(1, limit // 3)
        tail_n = max(1, limit - top_n - mid_n)
        middle_start = max(0, len(ranked) // 2 - mid_n // 2)
        selected = _unique_by_code(
            ranked[:top_n]
            + ranked[middle_start : middle_start + mid_n]
            + ranked[-tail_n:]
        )[:limit]
    return _baseline_pick_rows(
        selected,
        len(selected),
        "baseline-market-pool",
        "全池等权基线：从可交易候选池按高分/中位/低位分层取样，固定 15m 等权对照。",
    )


def _baseline_sector_peer_picks(candidates: List[Dict[str, Any]], final_picks: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    if not candidates or not final_picks or limit <= 0:
        return []
    selected_codes = {_normalize_code(item.get("code")) for item in final_picks}
    peer_keys = {_sector_peer_key(item) for item in final_picks}
    peer_keys = {item for item in peer_keys if item[1]}
    if not peer_keys:
        return []
    peers = [
        item
        for item in candidates
        if _normalize_code(item.get("code")) not in selected_codes
        and _sector_peer_key(item) in peer_keys
        and (item.get("dataQuality") or {}).get("eligibleForRealTop", True)
    ]
    ranked = sorted(peers, key=_t1_pool_key, reverse=True)
    levels = {key for key, _ in peer_keys}
    reason = "同行业/同概念基线：从最终推荐的真实行业或概念内选择规则分靠前、数据质量可交易的候选。"
    if levels == {"listingBoard"}:
        reason = "同上市板块观察基线：缺少真实行业/概念证据，仅按上市板块退化对照，不代表同一行业。"
    return _baseline_pick_rows(ranked, max(1, min(limit, len(final_picks) or limit)), "baseline-sector", reason)


def _sector_peer_key(item: Dict[str, Any]) -> tuple[str, str]:
    payload = item.get("item") if isinstance(item.get("item"), dict) else item
    industry = str(payload.get("industrySector") or "").strip()
    concepts = [str(value).strip() for value in payload.get("conceptSectors") or [] if str(value).strip()]
    layers = _sector_layers_from_evidence(payload.get("sectorEvidence") or {}, industry, concepts)
    if _t1_layer_available(layers, "identity") and industry:
        return ("industry", industry)
    if _t1_layer_available(layers, "identity") and concepts:
        return ("concept", concepts[0])
    return ("listingBoard", str(payload.get("listingBoard") or payload.get("sector") or "").strip())


def _baseline_pick_rows(candidates: List[Dict[str, Any]], limit: int, source: str, reason: str) -> List[Dict[str, Any]]:
    result = []
    for rank, item in enumerate(candidates[:limit], start=1):
        row = dict(item)
        row["aiRank"] = rank
        row["rank"] = rank
        row["selected"] = True
        row["selectionSource"] = source
        row["reason"] = reason
        result.append(row)
    return result


def _unique_codes(codes: List[str]) -> List[str]:
    result = []
    seen = set()
    for code in codes:
        normalized = str(code or "").strip().zfill(6)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _unique_strings(values: List[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _unique_suggestions(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        suggestion_id = int(item.get("id") or 0)
        key = suggestion_id or str(item.get("patternKey") or item.get("patternLabel") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _historical_treatment(
    status: str,
    applied_at: str,
    cutoff_date: str,
    reasons: List[str],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "version": HISTORY_SOFT_CLEANUP_VERSION,
        "status": status,
        "displayOnly": True,
        "learningEligible": False,
        "appliedAt": applied_at,
        "cutoffDate": cutoff_date,
        "reasons": _unique_strings(reasons),
    }
    if extra:
        payload.update(extra)
    return payload


def _historical_treatment_applied(market: Dict[str, Any], request: Dict[str, Any]) -> bool:
    treatment = {}
    if isinstance(market, dict):
        treatment = market.get("historicalTreatment") or {}
    if not treatment and isinstance(request, dict):
        treatment = request.get("historicalTreatment") or {}
    return isinstance(treatment, dict) and bool(
        treatment.get("displayOnly")
        or treatment.get("learningEligible") is False
        and str(treatment.get("status") or "") in {"legacy_display_only", "semantic_legacy_display_only", "zero_pick_cash_day", "invalid_quarantined"}
    )


def _stable_random_key(code: str, seed: str) -> int:
    text = f"{seed}:{code}"
    value = 0
    for char in text:
        value = (value * 131 + ord(char)) % 1000000007
    return value


def _apply_final_guardrails(picks: List[Dict[str, Any]], market: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    policy = market.get("recommendationPolicy") or {}
    market_context = market.get("marketContext") or {}
    max_count = min(limit, max(0, int(policy.get("maxPickCount") if policy.get("maxPickCount") is not None else limit)))
    if max_count <= 0:
        return []
    if market_context.get("state") == "panic":
        return []
    guarded = []
    for item in picks:
        quality = item.get("dataQuality") or {}
        if quality and not quality.get("eligibleForRealTop"):
            continue
        validation = item.get("aiEvidenceValidation") or {}
        if validation.get("status") == "failed":
            continue
        profile = item.get("riskProfile") or {}
        if profile.get("key") == "avoid_overnight":
            continue
        guarded.append(item)
        if len(guarded) >= max_count:
            break
    for index, item in enumerate(guarded, start=1):
        item["aiRank"] = index
        item["rank"] = index
    return guarded


def _chunks(values: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _code_chunks(values: List[str], size: int) -> List[List[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _batch_pick_for_size(candidate_count: int, batch_size: int, batch_pick: int) -> int:
    if candidate_count <= 0:
        return 0
    if batch_size <= 0:
        return min(candidate_count, batch_pick)
    scaled = int(candidate_count * batch_pick / batch_size + 0.5)
    return min(candidate_count, batch_pick, max(1, scaled))


def _unique_by_code(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        code = _normalize_code(item.get("code"))
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(item)
    return result


def _pick_codes(items: List[Dict[str, Any]]) -> List[str]:
    return [_normalize_code(item.get("code")) for item in items if _normalize_code(item.get("code"))]


def _candidate_type_counts(candidates: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for candidate in candidates:
        for candidate_type in candidate.get("candidateTypes") or [candidate.get("candidateType") or "other"]:
            counts[candidate_type] = counts.get(candidate_type, 0) + 1
    return counts


def _ai_meta(settings: Settings) -> Dict[str, Any]:
    configured = bool(settings.enable_ai and settings.ai_api_key and settings.ai_base_url)
    return {
        "configured": configured,
        "provider": settings.ai_provider if configured else "",
        "model": settings.ai_model if configured else "",
        "fallback": "" if configured else "AI 未配置时会直接使用规则兜底。",
    }


def _source_status(requested_source: str, actual_source: str, market: Dict[str, Any], warning: str) -> Dict[str, Any]:
    cache_status = market.get("cacheStatus") or {}
    mock_status = market.get("mockStatus") or {}
    actual = actual_source or ""
    return {
        "requested": requested_source or "auto",
        "actual": actual,
        "label": _source_label(actual),
        "isCache": bool(cache_status.get("used") or actual.startswith("cache:")),
        "isT1Frozen": bool(cache_status.get("t1Frozen") or (market.get("marketSnapshotFreeze") or {}).get("reused")),
        "isMock": bool(mock_status.get("used") or actual == "mock"),
        "cacheGeneratedAt": cache_status.get("generatedAt", ""),
        "freezeGeneratedAt": (market.get("marketSnapshotFreeze") or {}).get("generatedAt", ""),
        "freezeAgeSeconds": int((market.get("marketSnapshotFreeze") or {}).get("ageSeconds") or cache_status.get("ageSeconds") or 0),
        "cacheStale": bool(cache_status.get("stale")),
        "fallbackReason": cache_status.get("fallbackReason") or mock_status.get("reason") or "",
        "warning": warning or market.get("providerWarning", ""),
    }


def _source_label(source: str) -> str:
    if source.startswith("cache:"):
        return f"全市场缓存({source.split(':', 1)[1] or 'unknown'})"
    return {
        "auto": "自动",
        "sina": "新浪",
        "akshare": "AKShare/东财",
        "mock": "Mock 模拟数据",
    }.get(source, source or "--")


def _summarize_batch_errors(batch_summary: List[Dict[str, Any]]) -> str:
    errors = [str(batch.get("error") or "").strip() for batch in batch_summary if str(batch.get("error") or "").strip()]
    if not errors:
        return ""
    unique_errors = []
    for error in errors:
        if error not in unique_errors:
            unique_errors.append(error)
    return "AI 批次初筛未返回有效结果，已使用规则兜底：" + "；".join(unique_errors[:3])


def _normalize_critic_config(config: Dict[str, Any]) -> Dict[str, Any]:
    source = {**DEFAULT_CRITIC_CONFIG, **(config or {})}
    mode = str(source.get("mode") or "auto")
    if mode not in {"off", "rule_only", "auto", "always"}:
        mode = "auto"
    return {
        "mode": mode,
        "minCompletedRuns": _clamp_int(source.get("minCompletedRuns"), 0, 200, 10),
        "minVerifiedOutcomes": _clamp_int(source.get("minVerifiedOutcomes"), 0, 600, 20),
        "maxReplacements": _clamp_int(source.get("maxReplacements"), 0, 2, 1),
        "historyWindowRuns": _clamp_int(source.get("historyWindowRuns"), 5, 120, 20),
        "requireOutcomes": bool(source.get("requireOutcomes", True)),
        "allowReducePickCount": bool(source.get("allowReducePickCount", True)),
        "allowZeroPick": bool(source.get("allowZeroPick", True)),
        "requireMarketState": bool(source.get("requireMarketState", True)),
        "minTradeableValidationSamples": _clamp_int(source.get("minTradeableValidationSamples"), 0, 600, 20),
    }


def _normalize_execution_config(config: Dict[str, Any]) -> Dict[str, Any]:
    source = {**DEFAULT_EXECUTION_PROFILE, **(config or {})}
    buy_times = _normalize_time_list(source.get("buyTimes"), DEFAULT_EXECUTION_PROFILE["buyTimes"])
    sell_times = [
        item
        for item in _normalize_time_list(source.get("sellTimes"), DEFAULT_EXECUTION_PROFILE["sellTimes"], allow_keys=True)
        if item in TRADEABLE_SCENARIO_KEYS
    ]
    return {
        "buySlippagePct": _clamp_float(source.get("buySlippagePct"), 0, 2, DEFAULT_EXECUTION_PROFILE["buySlippagePct"]),
        "sellSlippagePct": _clamp_float(source.get("sellSlippagePct"), 0, 2, DEFAULT_EXECUTION_PROFILE["sellSlippagePct"]),
        "commissionPct": _clamp_float(source.get("commissionPct"), 0, 1, DEFAULT_EXECUTION_PROFILE["commissionPct"]),
        "stampTaxPct": _clamp_float(source.get("stampTaxPct"), 0, 1, DEFAULT_EXECUTION_PROFILE["stampTaxPct"]),
        "transferFeePct": _clamp_float(source.get("transferFeePct"), 0, 0.5, DEFAULT_EXECUTION_PROFILE["transferFeePct"]),
        "buyTimes": buy_times or list(DEFAULT_EXECUTION_PROFILE["buyTimes"]),
        "sellTimes": sell_times or list(DEFAULT_EXECUTION_PROFILE["sellTimes"]),
    }


def _normalize_stage_config(config: Dict[str, Any]) -> Dict[str, Any]:
    source = {**DEFAULT_STAGE_CONFIG, **(config or {})}
    return {
        "sectorStateShadowEnabled": bool(source.get("sectorStateShadowEnabled", True)),
        "sectorRetreatDowngradeMode": _normalize_stage_mode(source.get("sectorRetreatDowngradeMode"), "off"),
        "styleRegimeShadowEnabled": bool(source.get("styleRegimeShadowEnabled", True)),
        "styleRegimePromptMode": _normalize_stage_mode(source.get("styleRegimePromptMode"), "off"),
        "minOfficialTradeDaysForStage2": _clamp_int(source.get("minOfficialTradeDaysForStage2"), 1, 250, 30),
        "minTradeableOutcomesForStage2": _clamp_int(source.get("minTradeableOutcomesForStage2"), 1, 1000, 30),
        "minOfficialTradeDaysForStage3": _clamp_int(source.get("minOfficialTradeDaysForStage3"), 1, 250, 60),
        "minTradeableOutcomesForStage3": _clamp_int(source.get("minTradeableOutcomesForStage3"), 1, 1000, 60),
    }


def _normalize_stage_mode(value: Any, fallback: str = "off") -> str:
    mode = str(value or fallback).strip().lower()
    return mode if mode in {"off", "auto", "experimental"} else fallback


def _stage_gate(mode: str, min_days: int, min_outcomes: int, official_days: int, tradeable_outcomes: int) -> Dict[str, Any]:
    normalized = _normalize_stage_mode(mode)
    if normalized == "off":
        return {
            "requested": False,
            "effective": False,
            "productionEligible": False,
            "experimental": False,
            "reason": "页面配置为关闭。",
        }
    if normalized == "experimental":
        return {
            "requested": True,
            "effective": True,
            "productionEligible": False,
            "experimental": True,
            "reason": "实验模式已开启；相关 run 会标记为 research，不进入 official 严格学习。",
        }
    missing = []
    if official_days < min_days:
        missing.append(f"official 交易日 {official_days}/{min_days}")
    if tradeable_outcomes < min_outcomes:
        missing.append(f"open_15m 有效样本 {tradeable_outcomes}/{min_outcomes}")
    if missing:
        return {
            "requested": True,
            "effective": False,
            "productionEligible": False,
            "experimental": False,
            "reason": "等待样本：" + "，".join(missing),
        }
    return {
        "requested": True,
        "effective": True,
        "productionEligible": True,
        "experimental": False,
        "reason": "已达到页面配置的样本门槛，可进入正式判断。",
    }


def _stage_control_enabled(controls: Dict[str, Any], key: str, fallback: bool = False) -> bool:
    row = controls.get(key) if isinstance(controls, dict) else None
    if isinstance(row, dict) and "effectiveEnabled" in row:
        return bool(row.get("effectiveEnabled"))
    if isinstance(row, dict) and "enabled" in row:
        return bool(row.get("enabled"))
    return bool(fallback)


def _stage_controls_force_research(controls: Dict[str, Any]) -> bool:
    return any(
        bool((controls.get(key) or {}).get("experimental"))
        and bool((controls.get(key) or {}).get("effectiveEnabled"))
        for key in ("sectorRetreatDowngrade", "styleRegimePrompt")
    )


def _normalize_time_list(value: Any, fallback: List[str], allow_keys: bool = False) -> List[str]:
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        values = [str(item).strip() for item in value]
    else:
        values = list(fallback)
    result = []
    for item in values:
        if not item:
            continue
        if allow_keys and item in TRADEABLE_SCENARIO_KEYS:
            result.append(item)
            continue
        if len(item) == 5 and item[2] == ":" and item[:2].isdigit() and item[3:].isdigit():
            result.append(item)
    return result


def _clamp_float(value: Any, min_value: float, max_value: float, fallback: float) -> float:
    number = _safe_number(value)
    if number is None:
        return fallback
    return round(max(min_value, min(max_value, number)), 4)


def _critic_enabled_reason(config: Dict[str, Any], completed_runs: int, verified_outcomes: int, tradeable_outcomes: int = 0) -> tuple[bool, str]:
    mode = config.get("mode")
    if mode == "off":
        return False, "已关闭"
    if mode == "always":
        if int(config.get("maxReplacements") or 0) <= 0:
            return True, "始终启用审查，不执行替换"
        return True, "始终启用"
    if mode == "rule_only":
        return True, "仅规则审查"
    missing = []
    if completed_runs < int(config.get("minCompletedRuns") or 0):
        missing.append(f"推荐次数 {completed_runs}/{config.get('minCompletedRuns')}")
    if config.get("requireOutcomes") and verified_outcomes < int(config.get("minVerifiedOutcomes") or 0):
        missing.append(f"验证样本 {verified_outcomes}/{config.get('minVerifiedOutcomes')}")
    if config.get("requireOutcomes") and tradeable_outcomes < int(config.get("minTradeableValidationSamples") or 0):
        missing.append(f"专业验证样本 {tradeable_outcomes}/{config.get('minTradeableValidationSamples')}")
    if missing:
        return False, "等待样本：" + "，".join(missing)
    if int(config.get("maxReplacements") or 0) <= 0:
        return True, "已达到自动启用条件，仅审查不替换"
    return True, "已达到自动启用条件"


def _critic_skipped_result(status: Dict[str, Any], reason: str = "") -> Dict[str, Any]:
    return {
        "picks": [],
        "status": status,
        "enabled": False,
        "applied": False,
        "issues": [],
        "replacements": [],
        "reason": reason or status.get("reason", ""),
    }


def _rule_critic_issues(picks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    issues = []
    for pick in picks:
        profile = pick.get("riskProfile") or {}
        dimensions = pick.get("t1Dimensions") or {}
        code = str(pick.get("code") or "")
        if profile.get("key") == "avoid_overnight":
            issues.append({"code": code, "severity": "high", "reason": "风险画像为不建议隔夜。"})
        if float(dimensions.get("overnightRisk") or 0) >= 65:
            issues.append({"code": code, "severity": "high", "reason": "隔夜风险分过高。"})
        if pick.get("candidateType") == "risk_high_but_active":
            issues.append({"code": code, "severity": "medium", "reason": "候选类型为高风险但资金活跃，需要二次确认。"})
        quality = pick.get("dataQuality") or {}
        if quality and not quality.get("eligibleForRealTop"):
            issues.append({"code": code, "severity": "high", "reason": f"数据质量为 {_data_quality_label(str(quality.get('grade') or ''))}，不应进入真实 Top3。"})
        validation = pick.get("aiEvidenceValidation") or {}
        if validation.get("status") == "failed":
            issues.append({"code": code, "severity": "high", "reason": "AI 输出未通过证据事实校验。"})
    return issues


def _critic_role_prompt() -> str:
    return (
        "你是 A 股超短线 T+1 推荐的审查员。"
        "你只能审查当前 Top3 是否存在明显隔夜风险，并且最多按系统允许数量替换。"
        "替换候选必须来自 semifinalCandidates，不能新增池外股票。"
        "硬过滤和 riskProfile 高风险提示优先于主观偏好。"
        "listingBoard 不是行业/概念；必须按 sectorEvidence.evidenceLayers 使用证据：identity 支持所属身份，sectorSnapshot 支持今日强度/涨停数/资金，constituentRank 支持行业前排或个股排名。"
        "如果 sectorEvidence.sectorSnapshotSourceKind=candidate_pool_aggregate，只能审查候选池聚合强弱，不能把它当成全市场板块统计、外部资金流或资金共振。"
        "premiumPotentialScore 是评分不是概率。"
        "禁止输出 Markdown，必须输出严格 JSON。"
    )


def _critic_user_prompt(
    picks: List[Dict[str, Any]],
    semifinal: List[Dict[str, Any]],
    market: Dict[str, Any],
    rule_issues: List[Dict[str, Any]],
    feedback: Dict[str, Any],
    max_replacements: int,
) -> str:
    selected_codes = {item.get("code") for item in picks}
    alternatives = [item for item in semifinal if item.get("code") not in selected_codes]
    compact_market = _compact_market(market)
    current_top = [_compact_candidate(item) for item in picks]
    semifinal_compact = [_compact_candidate(item) for item in alternatives[:30]]
    stage_controls = compact_market.get("stageControls") or {}
    if not _stage_control_enabled(stage_controls, "sectorStatePrompt", T1_SECTOR_STATE_PROMPT_ENABLED):
        compact_market.pop("sectorStateShadowSummary", None)
        current_top = [_without_shadow_sector_prompt_fields(item) for item in current_top]
        semifinal_compact = [_without_shadow_sector_prompt_fields(item) for item in semifinal_compact]
    if not _stage_control_enabled(stage_controls, "styleRegimePrompt", T1_STYLE_REGIME_PROMPT_ENABLED):
        compact_market.pop("styleRegimeShadow", None)
    payload = {
        "task": {
            "maxReplacements": max_replacements,
            "allowReducePickCount": True,
            "allowZeroPick": True,
            "instruction": "判断 currentTop3 是否需要替换或减少数量；如替换，removeCode 必须来自 currentTop3，addCode 必须来自 semifinalCandidates。恐慌/高风险市场可以返回 dropCodes。",
        },
        "market": compact_market,
        "objectiveStrategyFeedback": _objective_strategy_feedback(feedback),
        "confirmedStrategyConstraints": feedback.get("activeConstraints") or [],
        "ruleIssues": rule_issues,
        "currentTop3": current_top,
        "semifinalCandidates": semifinal_compact,
    }
    return (
        f"请最多替换 {max_replacements} 只。若没有高价值替换，返回 passed=true 和空 replacements；如果市场或隔夜风险不适合，可以返回 dropCodes，但不能新增候选池外股票。\n"
        "客观复盘只描述历史事实；只能把 confirmedStrategyConstraints 作为用户已确认的策略约束。\n"
        "输出格式：{\"passed\":true,\"issues\":[],\"replacements\":[]}\n"
        "或：{\"passed\":false,\"issues\":[{\"code\":\"000001\",\"severity\":\"high\",\"reason\":\"...\"}],"
        "\"replacements\":[{\"removeCode\":\"000001\",\"addCode\":\"000002\",\"reason\":\"...\"}],\"dropCodes\":[]}\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _validate_ai_picks_evidence(picks: List[Dict[str, Any]], candidates: List[Dict[str, Any]], stage: str) -> Dict[str, Any]:
    errors: List[str] = []
    by_code = {_normalize_code(item.get("code")): item for item in candidates if _normalize_code(item.get("code"))}
    if not isinstance(picks, list):
        return {
            "version": AI_EVIDENCE_VALIDATION_VERSION,
            "valid": False,
            "status": "failed",
            "errors": ["AI 未返回 picks 数组。"],
        }
    if not picks:
        return {
            "version": AI_EVIDENCE_VALIDATION_VERSION,
            "valid": True,
            "status": "zero_pick",
            "errors": [],
            "acceptedPickCount": 0,
            "droppedPickCount": 0,
            "zeroPick": True,
        }
    for index, pick in enumerate(picks, start=1):
        code = _normalize_code((pick or {}).get("code"))
        if not code or code not in by_code:
            errors.append(f"第 {index} 项 code={code or '--'} 不在候选池。")
            continue
        candidate = by_code[code]
        evidence_refs = {str(item.get("id") or ""): item for item in candidate.get("evidenceRefs") or [] if item.get("id")}
        support_refs = _normalize_evidence_refs(pick.get("supportingEvidenceRefs"))
        counter_refs = _normalize_evidence_refs(pick.get("counterEvidenceRefs"))
        confidence = _normalize_confidence(pick.get("confidence") if pick.get("confidence") is not None else pick.get("score"))
        if not support_refs:
            errors.append(f"{code} 缺少 supportingEvidenceRefs。")
        if confidence is None:
            errors.append(f"{code} 缺少 confidence。")
        missing_refs = [ref for ref in support_refs + counter_refs if ref not in evidence_refs]
        if missing_refs:
            errors.append(f"{code} 引用了不存在的证据ID：{','.join(missing_refs[:5])}。")
        decision_tags = _normalize_decision_tags(pick.get("decisionTags") or pick.get("decision_tags"))
        sector_status = str(candidate.get("sectorEvidenceStatus") or "unavailable")
        layers = _sector_layers_from_evidence(
            candidate.get("sectorEvidence") or {},
            str(candidate.get("industrySector") or ""),
            list(candidate.get("conceptSectors") or []),
        )
        identity_ok = _t1_layer_available(layers, "identity")
        snapshot_ok = _t1_layer_available(layers, "sectorSnapshot")
        rank_ok = _t1_layer_available(layers, "constituentRank")
        if "S1" in support_refs and not (identity_ok or snapshot_ok or rank_ok):
            errors.append(f"{code} 的板块证据三层均不可用，不能把 S1 作为正向支撑证据。")
        s2_status = str((evidence_refs.get("S2") or {}).get("status") or "")
        if "S2" in support_refs and s2_status != "available":
            errors.append(f"{code} 的 S2 板块状态证据不可用，不能作为正向支撑证据。")
        if "sector_state_supported" in decision_tags and "S2" not in support_refs:
            errors.append(f"{code} 使用 sector_state_supported 标签但未把 S2 放入 supportingEvidenceRefs。")
        if "sector_state_supported" in decision_tags and s2_status != "available":
            errors.append(f"{code} 使用 sector_state_supported 标签时 S2 必须可用。")
        if "sector_snapshot_available" in decision_tags and "S1" not in support_refs:
            errors.append(f"{code} 使用 sector_snapshot_available 标签但未把 S1 放入 supportingEvidenceRefs。")
        if "sector_snapshot_available" in decision_tags and not snapshot_ok:
            errors.append(f"{code} 使用 sector_snapshot_available 标签但同日板块快照不可用。")
        t1_status = str((evidence_refs.get("T1") or {}).get("status") or "")
        if "tail_support" in decision_tags and ("T1" not in support_refs or t1_status != "available"):
            errors.append(f"{code} 使用 tail_support 标签时必须引用可用的 T1 尾盘承接证据。")
        text = _ai_pick_text(pick)
        if _has_probability_wording(text):
            errors.append(f"{code} 把溢价潜力评分写成了概率。")
        if _has_positive_sector_claim(text) and not snapshot_ok:
            errors.append(f"{code} 在同日板块强度证据不可用时写出了正向行业/概念强度结论。")
        if _has_sector_rank_claim(text) and not rank_ok:
            errors.append(f"{code} 在成分股排名证据不可用时写出了行业前排或排名结论。")
        internal_claim_errors = _unsupported_internal_sector_claim_errors(text, candidate)
        errors.extend([f"{code} {error}" for error in internal_claim_errors])
        sector_state_errors = _sector_state_claim_errors(text, candidate, support_refs + counter_refs)
        errors.extend([f"{code} {error}" for error in sector_state_errors])
        board_count = int(candidate.get("marketBoardLimitUpCount") or candidate.get("sectorLimitUps") or 0)
        if board_count > 0 and not snapshot_ok and _mentions_limitup_count_as_sector(text, board_count):
            errors.append(f"{code} 把上市板块涨停数 {board_count} 误用为行业/概念涨停数。")
        number_errors = _sector_number_mismatch_errors(text, candidate)
        errors.extend([f"{code} {error}" for error in number_errors])
        unsupported_labels = _unsupported_sector_labels(text, candidate)
        if unsupported_labels:
            errors.append(f"{code} 引用了输入中不存在或不可用的行业/概念名称：{','.join(unsupported_labels[:4])}。")
    errors = _unique_strings(errors)
    return {
        "version": AI_EVIDENCE_VALIDATION_VERSION,
        "valid": not errors,
        "status": "passed" if not errors else "failed",
        "errors": errors[:20],
    }


def _partial_ai_pick_validation(picks: List[Dict[str, Any]], candidates: List[Dict[str, Any]], stage: str) -> Dict[str, Any]:
    valid_picks: List[Dict[str, Any]] = []
    invalid_errors: List[str] = []
    seen = set()
    for pick in picks if isinstance(picks, list) else []:
        if not isinstance(pick, dict):
            invalid_errors.append("AI 返回了非对象 pick，已剔除。")
            continue
        code = _normalize_code(pick.get("code"))
        if not code:
            invalid_errors.append("AI 返回了缺少 code 的 pick，已剔除。")
            continue
        if code in seen:
            invalid_errors.append(f"{code} 重复出现，已剔除重复项。")
            continue
        single_validation = _validate_ai_picks_evidence([pick], candidates, stage)
        if single_validation.get("valid"):
            row = dict(pick)
            row["rank"] = len(valid_picks) + 1
            valid_picks.append(row)
            seen.add(code)
        else:
            invalid_errors.extend(single_validation.get("errors") or [])
    if not valid_picks:
        return {
            "version": AI_EVIDENCE_VALIDATION_VERSION,
            "valid": False,
            "status": "failed",
            "picks": [],
            "errors": _unique_strings(invalid_errors)[:20],
            "droppedPickCount": len(picks or []),
            "acceptedPickCount": 0,
        }
    final_validation = _validate_ai_picks_evidence(valid_picks, candidates, stage)
    if not final_validation.get("valid"):
        return {
            "version": AI_EVIDENCE_VALIDATION_VERSION,
            "valid": False,
            "status": "failed",
            "picks": [],
            "errors": _unique_strings(list(invalid_errors) + list(final_validation.get("errors") or []))[:20],
            "droppedPickCount": max(0, len(picks or []) - len(valid_picks)),
            "acceptedPickCount": len(valid_picks),
        }
    dropped = max(0, len(picks or []) - len(valid_picks))
    return {
        "version": AI_EVIDENCE_VALIDATION_VERSION,
        "valid": True,
        "status": "partial_passed" if dropped else "passed",
        "picks": valid_picks,
        "errors": _unique_strings(invalid_errors)[:20],
        "droppedPickCount": dropped,
        "acceptedPickCount": len(valid_picks),
    }


def _attach_ai_validation_to_picks(picks: List[Dict[str, Any]], validation: Dict[str, Any]) -> List[Dict[str, Any]]:
    status = validation.get("status") or ("passed" if validation.get("valid") else "failed")
    return [
        {
            **dict(pick),
            "aiEvidenceValidation": {
                "version": validation.get("version") or AI_EVIDENCE_VALIDATION_VERSION,
                "status": status if validation.get("valid") else "failed",
                "errors": validation.get("errors") or [],
                "droppedPickCount": int(validation.get("droppedPickCount") or 0),
                "acceptedPickCount": int(validation.get("acceptedPickCount") or len(picks or [])),
            },
        }
        for pick in picks
        if isinstance(pick, dict)
    ]


def _normalize_evidence_refs(value: Any) -> List[str]:
    if isinstance(value, str):
        raw_values = [item.strip() for item in re.split(r"[,，、\s]+", value) if item.strip()]
    elif isinstance(value, list):
        raw_values = [str(item.get("id") if isinstance(item, dict) else item).strip() for item in value]
    else:
        raw_values = []
    return _unique_strings([item for item in raw_values if item])


def _normalize_string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        values = [item.strip() for item in re.split(r"[；;，,\n]+", value) if item.strip()]
    elif isinstance(value, list):
        values = [str(item).strip() for item in value if str(item or "").strip()]
    else:
        values = []
    return _unique_strings(values)


def _ai_pick_text(pick: Dict[str, Any]) -> str:
    fields = [
        "reason",
        "strategyHypothesis",
        "buyPlan",
        "sellPlan",
        "risk",
        "entryCondition",
        "invalidCondition",
        "marketFit",
        "sectorFit",
        "confidenceReason",
    ]
    return "。".join(str(pick.get(field) or "") for field in fields)


def _has_probability_wording(text: str) -> bool:
    if "概率" not in text and "胜率" not in text:
        return False
    if any(phrase in text for phrase in ("不是概率", "非概率", "不是上涨概率", "不代表概率", "不等于概率")):
        return False
    if "溢价" in text or "明日" in text or "次日" in text or re.search(r"\d{2,3}(?:\.\d+)?\s*%", text):
        return True
    return bool(re.search(r"(概率|胜率).{0,8}(大|高|强|[0-9])", text))


def _has_positive_sector_claim(text: str) -> bool:
    pattern = r"(板块|行业|概念).{0,18}(涨停|强度|排名|前排|主线|共振|持续|领涨|发酵|回流|资金)"
    for sentence in re.split(r"[。；;，,\n]+", str(text or "")):
        for match in re.finditer(pattern, sentence):
            window = sentence[max(0, match.start() - 10) : min(len(sentence), match.end() + 10)]
            if any(word in window for word in ("不可用", "不能", "缺少", "缺乏", "缺失", "未能", "无法", "不足", "不完整", "不支持", "不依赖", "不作为", "不写", "仅")):
                continue
            return True
    return False


def _has_sector_rank_claim(text: str) -> bool:
    pattern = r"(板块|行业|概念).{0,18}(排名|前排|排第|第\s*\d+|相对地位|龙头|核心标的)"
    for sentence in re.split(r"[。；;，,\n]+", str(text or "")):
        for match in re.finditer(pattern, sentence):
            window = sentence[max(0, match.start() - 10) : min(len(sentence), match.end() + 10)]
            if any(word in window for word in ("不可用", "不能", "缺少", "缺乏", "缺失", "未能", "无法", "不足", "不完整", "不支持", "不依赖", "不作为", "不写", "仅")):
                continue
            return True
    return False


def _unsupported_internal_sector_claim_errors(text: str, candidate: Dict[str, Any]) -> List[str]:
    source_kind = _candidate_sector_snapshot_kind(candidate)
    if source_kind not in {"candidate_pool_aggregate", "mixed"}:
        return []
    internal_labels = _sector_label_aliases(
        [
            item.get("sectorName")
            for item in _candidate_sector_snapshot_matches(candidate)
            if _sector_source_is_internal(item.get("source"))
        ]
    )
    errors: List[str] = []
    for sentence in re.split(r"[。；;\n]+", str(text or "")):
        clean = sentence.strip()
        if not clean:
            continue
        if any(word in clean for word in ("不含外部资金流", "不能写", "不得写", "没有外部资金", "缺少外部资金", "不代表全市场")):
            continue
        has_sector_word = bool(re.search(r"(板块|行业|概念)", clean))
        has_internal_scope_word = any(word in clean for word in ("候选池", "自建", "聚合", "样本内", "池内"))
        if not has_sector_word and not has_internal_scope_word:
            continue
        if source_kind == "mixed":
            mentions_internal_scope = has_internal_scope_word or any(label and label in clean for label in internal_labels)
            if not mentions_internal_scope:
                continue
        if re.search(r"(资金|净流入|主力|共振)", clean):
            errors.append("自建候选池聚合证据不支持板块资金、主力净流入或资金共振结论。")
        if re.search(r"(全市场|全行业|全板块)", clean) and "候选池" not in clean:
            errors.append("自建候选池聚合证据不能写成全市场行业/概念统计。")
        if re.search(r"(板块|行业|概念).{0,20}\d+\s*只\s*涨停|\d+\s*只\s*涨停.{0,20}(板块|行业|概念)", clean):
            if not any(word in clean for word in ("候选池", "本次候选", "样本内", "池内")):
                errors.append("自建候选池聚合涨停数必须明确写成候选池内/样本内，不能写成全市场行业涨停数。")
    return _unique_strings(errors)


def _sector_state_claim_errors(text: str, candidate: Dict[str, Any], evidence_refs: List[str]) -> List[str]:
    errors: List[str] = []
    state = str(candidate.get("sectorStateShadow") or (candidate.get("sectorEvidence") or {}).get("sectorStateShadow") or "data_insufficient")
    label = T1_SECTOR_STATE_LABELS.get(state, "")
    state_words = {
        "rotation_in": ["轮入", "回流", "流入"],
        "strong": ["强势", "强度延续", "延续"],
        "overheated": ["过热", "高热", "高潮", "兑现"],
        "weakening": ["转弱", "走弱"],
        "distribution": ["派发", "分歧"],
        "retreat": ["退潮", "退坡"],
        "oversold_repair": ["超跌修复", "修复"],
    }
    sentences = [item.strip() for item in re.split(r"[。；;\n]+", str(text or "")) if item.strip()]
    for sentence in sentences:
        if not re.search(r"(板块|行业|概念)", sentence):
            continue
        if any(word in sentence for word in ("不能确认", "无法确认", "缺少", "缺失", "不可用", "不支持", "不得", "不能")):
            continue
        mentioned_states = [
            key
            for key, words in state_words.items()
            if any(word in sentence for word in words)
        ]
        if not mentioned_states:
            continue
        if "S2" not in evidence_refs:
            errors.append("写出了板块状态结论但未引用 S2 板块状态影子证据。")
        if state == "data_insufficient":
            errors.append("板块状态证据不足时不能写行业/概念退潮、轮入、派发、过热等状态结论。")
            continue
        if state not in mentioned_states:
            errors.append(f"写出的板块状态与输入不一致：输入为 {label or state}。")
    return _unique_strings(errors)


def _mentions_limitup_count_as_sector(text: str, count: int) -> bool:
    pattern = rf"{count}\s*只\s*涨停"
    if not re.search(pattern, text):
        return False
    return bool(re.search(r"(板块|行业|概念).{0,20}" + pattern + r"|" + pattern + r".{0,20}(板块|行业|概念)", text))


def _sector_number_mismatch_errors(text: str, candidate: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    industry_count = int(candidate.get("industryLimitUpCount") or 0)
    concept_counts = [int(item.get("limitUpCount") or 0) for item in candidate.get("conceptLimitUpCounts") or []]
    board_count = int(candidate.get("marketBoardLimitUpCount") or 0)
    listing_board = str(candidate.get("listingBoard") or "")
    for match in re.finditer(r"(\d+)\s*只\s*涨停", text):
        count = int(match.group(1))
        window = text[max(0, match.start() - 18) : min(len(text), match.end() + 18)]
        if "上市板块" in window or (listing_board and listing_board in window):
            allowed = {board_count}
            label = "上市板块涨停数"
        else:
            allowed = {industry_count, *concept_counts}
            label = "行业/概念涨停数"
        allowed = {value for value in allowed if value is not None}
        if count not in allowed:
            errors.append(f"{label} {count} 与输入不一致。")

    industry_rank = int(candidate.get("industryRank") or 0)
    concept_ranks = [int(item.get("rank") or 0) for item in candidate.get("conceptLimitUpCounts") or []]
    board_rank = int(candidate.get("marketBoardRank") or 0)
    stock_rank = int(candidate.get("stockRankInIndustry") or 0)
    listing_board = str(candidate.get("listingBoard") or "")
    for match in re.finditer(r"(?:排名|排第|第)\s*(\d+)", text):
        rank = int(match.group(1))
        window = text[max(0, match.start() - 18) : min(len(text), match.end() + 18)]
        prefix = text[max(0, match.start() - 12) : match.start()]
        if re.search(r"(个股|行业中|板块内|概念内).{0,8}$", prefix) and stock_rank:
            allowed = {stock_rank}
            label = "个股行业内排名"
        elif "上市板块" in window or (listing_board and listing_board in window):
            allowed = {board_rank}
            label = "上市板块排名"
        else:
            allowed = {industry_rank, *concept_ranks}
            label = "行业/概念排名"
        allowed = {value for value in allowed if value}
        if allowed and rank not in allowed:
            errors.append(f"{label} {rank} 与输入不一致。")
    return _unique_strings(errors)


def _unsupported_sector_labels(text: str, candidate: Dict[str, Any]) -> List[str]:
    allowed = {
        str(candidate.get("listingBoard") or ""),
        str(candidate.get("industrySector") or ""),
        *[str(item or "") for item in candidate.get("conceptSectors") or []],
    }
    allowed = {item for item in allowed if item}
    normalized_allowed = _sector_label_aliases(allowed)
    labels = []
    for match in re.finditer(r"([\u4e00-\u9fa5A-Za-z0-9]{2,12})(?:板块|行业|概念)", text):
        window = text[max(0, match.start() - 12) : min(len(text), match.end() + 12)]
        sentence_start = max(text.rfind("。", 0, match.start()), text.rfind("；", 0, match.start()), text.rfind("\n", 0, match.start()))
        sentence_end_candidates = [index for index in (text.find("。", match.end()), text.find("；", match.end()), text.find("\n", match.end())) if index >= 0]
        sentence_end = min(sentence_end_candidates) if sentence_end_candidates else len(text)
        sentence = text[sentence_start + 1 : sentence_end]
        label = _clean_ai_sector_label(match.group(1))
        if not label or label in {"市场", "行业", "概念", "板块", "上市", "真实", "所属"}:
            continue
        if _is_cautious_or_generic_sector_label(label, window) or _is_cautious_or_generic_sector_label(label, sentence):
            continue
        label_aliases = _sector_label_aliases(_split_ai_sector_label(label))
        if label_aliases and label_aliases.issubset(normalized_allowed):
            continue
        labels.append(label)
    return _unique_strings(labels)


def _clean_ai_sector_label(label: str) -> str:
    text = str(label or "").strip("该个所属真实上市，。；;、 ")
    for prefix in ("行业", "概念", "板块", "所属"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.strip("，。；;、 ")


def _split_ai_sector_label(label: str) -> List[str]:
    parts = [item.strip() for item in re.split(r"(?:以及|及|和|与|、|/|,|，)", str(label or "")) if item.strip()]
    return parts or [str(label or "").strip()]


def _sector_label_aliases(labels: Any) -> set[str]:
    if isinstance(labels, str):
        values = [labels]
    else:
        values = [str(item or "") for item in labels or []]
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
    generic_words = [
        "今日",
        "资金",
        "强度",
        "证据",
        "确认",
        "覆盖",
        "成分股",
        "数据",
        "尾盘",
        "正向",
        "负面",
        "涨幅",
        "温和",
        "支撑",
        "限制",
        "缺失",
        "股作为",
        "股在",
        "无法确认",
        "不构成",
        "强但",
        "高但",
        "仅身份",
        "候选池",
        "自建",
        "聚合",
        "样本内",
        "身份与",
        "主要来自",
        "部分",
        "热门",
        "证据不足",
        "板块证据",
        "行业证据",
        "概念证据",
        "资金强度高但",
        "无法确认今日",
        "不能确认今日",
        "未确认今日",
        "仅能",
        "只能",
    ]
    if any(word in label or word in window for word in cautious_words):
        return True
    if any(word in label for word in generic_words):
        return True
    if label.startswith(("但", "且", "若", "如", "因", "缺", "无")):
        return True
    if label.endswith(("但", "且", "或")):
        return True
    return False


def _extract_json_object(content: str) -> Dict[str, Any]:
    text = str(content or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def _apply_critic_replacements(
    picks: List[Dict[str, Any]],
    semifinal: List[Dict[str, Any]],
    critic: Dict[str, Any],
    max_replacements: int,
    final_limit: int,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if max_replacements <= 0:
        return picks, []
    by_code = {str(item.get("code") or ""): item for item in semifinal}
    current = [dict(item) for item in picks]
    current_codes = {str(item.get("code") or "") for item in current}
    replacements = []
    for replacement in critic.get("replacements") or []:
        if len(replacements) >= max_replacements:
            break
        remove_code = _normalize_code(replacement.get("removeCode") or replacement.get("remove_code"))
        add_code = _normalize_code(replacement.get("addCode") or replacement.get("add_code"))
        if not remove_code or not add_code or remove_code not in current_codes or add_code not in by_code or add_code in current_codes:
            continue
        next_item = dict(by_code[add_code])
        removed_rank = 0
        updated = []
        for item in current:
            if str(item.get("code") or "") == remove_code:
                removed_rank = int(item.get("aiRank") or item.get("rank") or len(updated) + 1)
                continue
            updated.append(item)
        next_item["aiRank"] = removed_rank or len(updated) + 1
        next_item["selected"] = True
        next_item["selectionSource"] = "critic-replacement"
        next_item["criticReplacement"] = {
            "removeCode": remove_code,
            "addCode": add_code,
            "reason": str(replacement.get("reason") or ""),
        }
        next_item.setdefault("reason", str(replacement.get("reason") or "Critic 替换入选。"))
        next_item.update(_ensure_risk_profile(next_item))
        updated.append(next_item)
        current = sorted(updated, key=lambda item: int(item.get("aiRank") or 999))[:final_limit]
        current_codes = {str(item.get("code") or "") for item in current}
        replacements.append({"removeCode": remove_code, "addCode": add_code, "reason": str(replacement.get("reason") or "")})
    for index, item in enumerate(current, start=1):
        item["aiRank"] = index
        item["rank"] = index
    return current[:final_limit], replacements


def _simulated_entry_execution(
    run: Dict[str, Any],
    buy_reference: Optional[float],
    recommendation_minute_series: List[Dict[str, Any]],
) -> Dict[str, Any]:
    market = run.get("market") or {}
    timing = market.get("decisionTiming") or {}
    signal_published_at = str(timing.get("signalPublishedAt") or "")
    signal_time = _time_from_iso_text(signal_published_at) or str(run.get("scanTime") or DEFAULT_SCAN_TIME)
    recommendation_date = str(run.get("recommendationDate") or "")
    dated = _minute_series_for_trade_date(recommendation_minute_series or [], recommendation_date)
    if not dated and recommendation_minute_series:
        dated = list(recommendation_minute_series or [])
    for item in sorted(dated, key=lambda row: _minute_time(row) or ""):
        item_time = _minute_time(item)
        if item_time and item_time < signal_time:
            continue
        price = _safe_number(item.get("close") or item.get("open"))
        volume = _safe_number(item.get("volume"))
        if price and price > 0 and (volume is None or volume > 0):
            return {
                "version": T1_EXECUTION_CONTRACT_VERSION,
                "executable": True,
                "status": "executed_from_minute",
                "label": "信号发布后可成交",
                "signalPublishedAt": signal_published_at,
                "signalTime": signal_time,
                "entryTime": item_time,
                "price": price,
                "buyReferencePrice": buy_reference,
                "source": "recommendation_minute_snapshot",
            }
    if buy_reference and signal_time <= "14:50":
        return {
            "version": T1_EXECUTION_CONTRACT_VERSION,
            "executable": True,
            "status": "reference_price_without_minute",
            "label": "缺分钟成交，用冻结价近似",
            "signalPublishedAt": signal_published_at,
            "signalTime": signal_time,
            "entryTime": str(run.get("scanTime") or signal_time),
            "price": buy_reference,
            "buyReferencePrice": buy_reference,
            "source": "buy_reference_fallback",
            "qualityWarning": "没有推荐日信号发布后的分钟成交，只能用冻结价近似，不能作为最高等级成交证据。",
        }
    return {
        "version": T1_EXECUTION_CONTRACT_VERSION,
        "executable": False,
        "status": "entry_not_executable",
        "label": "买入不可确认",
        "signalPublishedAt": signal_published_at,
        "signalTime": signal_time,
        "entryTime": "",
        "price": None,
        "buyReferencePrice": buy_reference,
        "source": "",
        "reason": "信号发布后没有可用分钟成交，或发布时间已不满足尾盘执行窗口。",
    }


def _mark_entry_not_executable_scenarios(scenarios: List[Dict[str, Any]], entry_execution: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    reason = str(entry_execution.get("reason") or "买入不可执行，不能计算真实交易收益。")
    for scenario in scenarios or []:
        row = dict(scenario)
        row["grossReturnPct"] = None
        row["netReturnPct"] = None
        row["dataStatus"] = "entry_not_executable"
        row["validationStatus"] = "unavailable"
        row["status"] = "unavailable"
        row["executionStatus"] = "entry_not_executable"
        row["reason"] = reason
        result.append(row)
    return result


def _recommendation_quality_eligible(
    run: Dict[str, Any],
    validation_status: str,
    price_consistency: Dict[str, Any],
    corporate_action: Dict[str, Any],
) -> bool:
    if _run_role(run) != "official":
        return False
    if str(validation_status or "") in {"legacy", "invalid"}:
        return False
    if not price_consistency.get("ok", True):
        return False
    if corporate_action.get("suspected"):
        return False
    if not str(run.get("recommendationDate") or ""):
        return False
    market = run.get("market") or {}
    timing = market.get("decisionTiming") or {}
    contract = market.get("asOfContract") or {}
    return bool(
        str(run.get("scanTime") or contract.get("requestedScanTime") or "")
        and str(timing.get("signalPublishedAt") or "")
    )


def _build_outcome(
    run: Dict[str, Any],
    pick: Dict[str, Any],
    series: List[Dict[str, Any]],
    minute_series: List[Dict[str, Any]],
    data_source: str,
    minute_source: str,
    verified_at: str,
    execution_profile: Optional[Dict[str, Any]] = None,
    recommendation_minute_series: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    execution_profile = _normalize_execution_config(execution_profile or DEFAULT_EXECUTION_PROFILE)
    code = str(pick.get("code") or "").zfill(6)
    recommendation_date = str(run.get("recommendationDate") or "")
    buy_reference = _safe_number(pick.get("price") or (pick.get("item") or {}).get("price"))
    recommendation_reference = _recommendation_price_reference_point(
        series=series,
        recommendation_date=recommendation_date,
        recommendation_minute_series=recommendation_minute_series or [],
    )
    next_point = _next_trade_point(series, recommendation_date)
    minute_derived_next_point = False
    if not next_point:
        next_point = _minute_derived_next_trade_point(minute_series, recommendation_date, verified_at)
        minute_derived_next_point = bool(next_point)
    next_trade_date = str((next_point or {}).get("date", ""))
    daily_status = _daily_validation_status(next_trade_date, verified_at)
    if not buy_reference or buy_reference <= 0:
        buy_reference = _safe_number((recommendation_reference or {}).get("close"))
    entry_execution = _simulated_entry_execution(
        run=run,
        buy_reference=buy_reference,
        recommendation_minute_series=recommendation_minute_series or [],
    )
    simulated_entry_price = _safe_number(entry_execution.get("price")) if entry_execution.get("executable") else None
    trade_base_price = simulated_entry_price or buy_reference
    effective_data_source = (
        f"minute_intraday:{minute_source or 'minute'}"
        if minute_derived_next_point
        else data_source
    )
    price_consistency = _price_consistency(
        buy_reference=buy_reference,
        previous_point=recommendation_reference,
        data_source=effective_data_source,
    )
    corporate_action = _corporate_action_signal(recommendation_reference, next_point, buy_reference, price_consistency)
    base = {
        "runId": run.get("id"),
        "code": code,
        "name": pick.get("name", ""),
        "aiRank": int(pick.get("aiRank") or pick.get("rank") or 999),
        "poolRank": int(pick.get("poolRank") or 0),
        "sector": str(pick.get("sector") or (pick.get("item") or {}).get("sector") or ""),
        "listingBoard": str(pick.get("listingBoard") or (pick.get("item") or {}).get("listingBoard") or ""),
        "industrySector": str(pick.get("industrySector") or (pick.get("item") or {}).get("industrySector") or ""),
        "conceptSectors": pick.get("conceptSectors") or (pick.get("item") or {}).get("conceptSectors") or [],
        "sectorEvidenceStatus": str(pick.get("sectorEvidenceStatus") or (pick.get("item") or {}).get("sectorEvidenceStatus") or ""),
        "sectorEvidence": pick.get("sectorEvidence") or (pick.get("item") or {}).get("sectorEvidence") or {},
        "selectionSource": str(pick.get("selectionSource") or ""),
        "recommendationDate": recommendation_date,
        "nextTradeDate": next_trade_date,
        "buyReferencePrice": buy_reference,
        "simulatedEntryPrice": simulated_entry_price,
        "entryExecution": entry_execution,
        "verifiedAt": verified_at,
        "dataSource": effective_data_source,
        "minuteSource": minute_source,
        "validationVersion": TRADEABLE_VALIDATION_VERSION,
        "executionProfile": dict(execution_profile),
        "riskProfile": pick.get("riskProfile") or {},
        "candidateType": pick.get("candidateType", ""),
        "candidateTypeLabel": pick.get("candidateTypeLabel", ""),
        "dataQuality": pick.get("dataQuality") or {},
        "marketContext": (run.get("market") or {}).get("marketContext") or {},
        "priceConsistency": price_consistency,
        "corporateAction": corporate_action,
        "validationStatus": "pending",
        "validationTiming": {
            "nextTradeDate": next_trade_date,
            "dailyOhlcStatus": daily_status,
            "dailyFinalAfter": DAILY_FINAL_AFTER_TIME,
            "dailyOhlcSource": "minute_intraday" if minute_derived_next_point else "daily_kline",
            "minuteDerivedDailyOhlc": minute_derived_next_point,
        },
        "learningEligible": False,
        "exclusionReasons": [],
    }
    if not next_point or not buy_reference or buy_reference <= 0:
        base["dataQuality"] = _outcome_data_quality(
            pick_quality=pick.get("dataQuality") or {},
            next_point=next_point,
            buy_reference=buy_reference,
            data_source=effective_data_source,
            scenarios=[],
            price_consistency=price_consistency,
            primary={},
        )
        base.update(
            {
                "outcomeLabel": "数据不足",
                "outcomeScore": 0,
                "failureReason": "缺少次日行情或买入参考价。",
                "isTradeableMetric": False,
                "validationStatus": "unavailable",
                "learningEligible": False,
                "recommendationQualityEligible": _recommendation_quality_eligible(run, "unavailable", price_consistency, corporate_action),
                "tradeReturnEligible": False,
                "exclusionReasons": ["缺少次日行情或买入参考价"],
                "theoretical": {},
                "tradeable": {"scenarios": [], "primaryScenario": {}, "entryExecution": entry_execution, "simulatedEntryPrice": simulated_entry_price},
            }
        )
        return base
    next_open = _safe_number(next_point.get("open")) or 0
    next_high = _safe_number(next_point.get("high")) or 0
    next_low = _safe_number(next_point.get("low")) or 0
    next_close = _safe_number(next_point.get("close")) or 0
    open_return = _pct_return(next_open, trade_base_price or 0)
    high_return = _pct_return(next_high, trade_base_price or 0)
    close_return = _pct_return(next_close, trade_base_price or 0)
    max_drawdown = _pct_return(next_low, trade_base_price or 0)
    market_gap_pct = _pct_return(next_open, _safe_number((recommendation_reference or {}).get("close")) or buy_reference or 0)
    scenarios = _build_tradeable_scenarios(
        code=code,
        buy_reference=trade_base_price or buy_reference,
        next_point=next_point,
        minute_series=minute_series,
        minute_source=minute_source,
        execution_profile=execution_profile,
        verified_at=verified_at,
    )
    if not entry_execution.get("executable"):
        scenarios = _mark_entry_not_executable_scenarios(scenarios, entry_execution)
    rule_execution = _rule_execution_result(code, scenarios, open_return, high_return, close_return, daily_status)
    stored_scenarios = list(scenarios)
    if rule_execution.get("scenarioKey"):
        stored_scenarios.append(rule_execution)
    primary = _primary_tradeable_scenario(scenarios)
    outcome_data_source = (
        f"minute_intraday:{minute_source or 'minute'}"
        if primary.get("scenarioKey") == PRIMARY_TRADEABLE_SCENARIO_KEY
        and primary.get("dataStatus") == "ok"
        and primary.get("netReturnPct") is not None
        else effective_data_source
    )
    base["dataSource"] = outcome_data_source
    data_quality = _outcome_data_quality(
        pick_quality=pick.get("dataQuality") or {},
        next_point=next_point,
        buy_reference=buy_reference,
        data_source=outcome_data_source,
        scenarios=stored_scenarios,
        price_consistency=price_consistency,
        primary=primary,
    )
    if corporate_action.get("suspected"):
        data_quality = dict(data_quality)
        reasons = list(data_quality.get("reasons") or [])
        reasons.append(corporate_action.get("reason") or "疑似公司行为或复权口径断点")
        data_quality.update(
            {
                "grade": "E",
                "label": _data_quality_label("E"),
                "eligibleForRealTop": False,
                "corporateActionSuspected": True,
                "reasons": _unique_strings(reasons),
            }
        )
    label, score, failure, hit_sell, hit_invalid = _classify_tradeable_outcome(
        primary.get("netReturnPct"),
        open_return,
        high_return,
        close_return,
        max_drawdown,
    )
    trade_quality = _trade_quality_flags(open_return, high_return, close_return, max_drawdown)
    failure_tags = _failure_tags(primary.get("netReturnPct"), open_return, high_return, close_return, max_drawdown)
    failure_tags.extend(_context_failure_tags(primary.get("netReturnPct"), pick, data_quality, price_consistency, corporate_action, run.get("market") or {}))
    failure_tags = _unique_strings(failure_tags)
    validation_status = _validation_status_for_outcome(primary, data_quality, price_consistency, daily_status)
    learning_eligible, exclusion_reasons = _learning_eligibility(
        primary=primary,
        data_quality=data_quality,
        data_source=outcome_data_source,
        price_consistency=price_consistency,
        validation_status=validation_status,
    )
    if str(entry_execution.get("status") or "") != "executed_from_minute":
        learning_eligible = False
        exclusion_reasons = _unique_strings(
            list(exclusion_reasons or [])
            + [entry_execution.get("qualityWarning") or entry_execution.get("reason") or "模拟买入成交证据不足"]
        )
    trade_quality["failureTags"] = failure_tags
    if not price_consistency.get("ok"):
        label = "价格异常"
        score = 0
        failure = price_consistency.get("reason") or "推荐价与验证价口径不一致。"
        hit_sell = False
        hit_invalid = True
    if corporate_action.get("suspected"):
        label = "价格异常"
        score = 0
        failure = corporate_action.get("reason") or "疑似公司行为或复权口径断点，已隔离。"
        hit_sell = False
        hit_invalid = True
        learning_eligible = False
        exclusion_reasons = _unique_strings(list(exclusion_reasons or []) + [failure])
    base.update(
        {
            "nextOpen": next_open,
            "nextHigh": next_high,
            "nextLow": next_low,
            "nextClose": next_close,
            "openReturnPct": open_return,
            "tradeOpenReturnPct": open_return,
            "marketGapPct": market_gap_pct,
            "highReturnPct": high_return,
            "closeReturnPct": close_return,
            "maxDrawdownPct": max_drawdown,
            "benchmarkReturnPct": None,
            "excessReturnPct": None,
            "dataQuality": data_quality,
            "outcomeLabel": label,
            "outcomeScore": score,
            "hitSellPlan": hit_sell,
            "hitInvalidCondition": hit_invalid,
            "failureReason": failure,
            "isTradeableMetric": learning_eligible,
            "validationStatus": validation_status,
            "learningEligible": learning_eligible,
            "recommendationQualityEligible": _recommendation_quality_eligible(run, validation_status, price_consistency, corporate_action),
            "tradeReturnEligible": learning_eligible,
            "exclusionReasons": exclusion_reasons,
            "failureTags": failure_tags,
            "theoretical": {
                "validationVersion": THEORETICAL_VALIDATION_VERSION,
                "description": "盘中机会参考，不作为主复盘指标。",
                "validationStatus": daily_status,
                "dailyFinalAfter": DAILY_FINAL_AFTER_TIME,
                "openReturnPct": open_return,
                "highReturnPct": high_return,
                "lowReturnPct": max_drawdown,
                "closeReturnPct": close_return,
            },
            "tradeable": {
                "validationVersion": TRADEABLE_VALIDATION_VERSION,
                "validationTiming": {
                    "nextTradeDate": next_trade_date,
                    "dailyOhlcStatus": daily_status,
                    "dailyFinalAfter": DAILY_FINAL_AFTER_TIME,
                },
                "buyScenarios": _build_buy_scenarios(run, buy_reference, execution_profile),
                "entryExecution": entry_execution,
                "simulatedEntryPrice": simulated_entry_price,
                "primaryScenario": primary,
                "scenarios": stored_scenarios,
                "quality": trade_quality,
                "ruleExecution": rule_execution,
                "costModel": _roundtrip_cost_model(execution_profile),
                "primaryScenarioKey": PRIMARY_TRADEABLE_SCENARIO_KEY,
                "primaryFallbackAllowed": False,
            },
        }
    )
    return base


def _outcome_data_quality(
    pick_quality: Dict[str, Any],
    next_point: Optional[Dict[str, Any]],
    buy_reference: Optional[float],
    data_source: str,
    scenarios: List[Dict[str, Any]],
    price_consistency: Optional[Dict[str, Any]] = None,
    primary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    existing = dict(pick_quality or {})
    reasons = list(existing.get("reasons") or [])
    data_source_text = str(data_source or "")
    price_consistency = price_consistency or {}
    primary = primary or {}
    minute_derived = bool((next_point or {}).get("minuteDerived"))
    minute_rows = [row for row in scenarios if row.get("scenarioKey") in MINUTE_SCENARIO_KEYS]
    minute_missing = bool(minute_rows) and any(row.get("dataStatus") != "ok" for row in minute_rows)
    primary_missing = (primary.get("scenarioKey") == PRIMARY_TRADEABLE_SCENARIO_KEY and primary.get("dataStatus") != "ok") or primary.get("netReturnPct") is None

    if not next_point or not buy_reference or buy_reference <= 0:
        derived_grade = "E"
        reasons.append("缺少次日行情或买入参考价")
    elif price_consistency and not price_consistency.get("ok", True):
        derived_grade = "E"
        reasons.append(price_consistency.get("reason") or "推荐价与验证价偏差过大")
    elif price_consistency.get("status") == "reference_unavailable":
        derived_grade = "C"
        reasons.append(price_consistency.get("reason") or "推荐日价格校验证据不足")
    elif "mock" in data_source_text or "simulated" in data_source_text:
        derived_grade = "D"
        reasons.append("验证行情含模拟数据")
    elif "cache" in data_source_text:
        derived_grade = "C"
        reasons.append("验证行情来自缓存")
    elif primary_missing:
        derived_grade = "B"
        reasons.append("固定主 KPI open_15m 缺失")
    elif minute_missing:
        derived_grade = "B"
        reasons.append("验证日线真实，但部分分钟卖出场景缺失")
    else:
        derived_grade = "A"

    if minute_derived:
        reasons.append("次日日线未更新，已使用真实分钟线生成临时验证行情")

    existing_grade = str(existing.get("grade") or "")
    grade = _worse_data_quality_grade(existing_grade, derived_grade)
    clean_reasons = []
    for reason in reasons:
        if reason and reason not in clean_reasons:
            clean_reasons.append(reason)
    existing.update(
        {
            "grade": grade,
            "label": _data_quality_label(grade),
            "eligibleForRealTop": grade in {"A", "B"},
            "validationDataSource": data_source_text,
            "validationMinuteComplete": bool(minute_rows) and not minute_missing,
            "validationMinuteMissing": minute_missing,
            "primaryScenarioKey": PRIMARY_TRADEABLE_SCENARIO_KEY,
            "primaryScenarioAvailable": not primary_missing,
            "validationScenarioCount": len(scenarios),
            "minuteDerivedDailyOhlc": minute_derived,
            "minuteDerivedPointCount": int((next_point or {}).get("minutePointCount") or 0),
            "priceConsistency": price_consistency,
            "reasons": clean_reasons,
        }
    )
    return existing


def _trade_point_on_date(series: List[Dict[str, Any]], trade_date: str) -> Dict[str, Any]:
    if not trade_date:
        return {}
    for point in series or []:
        if str((point or {}).get("date") or "") == trade_date:
            return dict(point or {})
    return {}


def _recommendation_price_reference_point(
    series: List[Dict[str, Any]],
    recommendation_date: str,
    recommendation_minute_series: List[Dict[str, Any]],
) -> Dict[str, Any]:
    daily_point = _trade_point_on_date(series, recommendation_date)
    if daily_point:
        daily_point.setdefault("referenceSource", "recommendation_daily_ohlc")
        daily_point.setdefault("referenceStatus", "verified")
        return daily_point
    minute_point = _minute_ohlc_reference_point(recommendation_minute_series, recommendation_date)
    if minute_point:
        return minute_point
    return {
        "date": recommendation_date,
        "referenceSource": "unavailable",
        "referenceStatus": "unverified",
        "referenceMissing": True,
    }


def _minute_ohlc_reference_point(series: List[Dict[str, Any]], trade_date: str) -> Dict[str, Any]:
    dated = _minute_series_for_trade_date(series or [], trade_date)
    if not dated:
        return {}
    opens = [_safe_number(item.get("open")) for item in dated]
    highs = [_safe_number(item.get("high")) for item in dated]
    lows = [_safe_number(item.get("low")) for item in dated]
    closes = [_safe_number(item.get("close")) for item in dated]
    opens = [item for item in opens if item and item > 0]
    highs = [item for item in highs if item and item > 0]
    lows = [item for item in lows if item and item > 0]
    closes = [item for item in closes if item and item > 0]
    if not highs or not lows or not closes:
        return {}
    return {
        "date": trade_date,
        "open": opens[0] if opens else closes[0],
        "high": max(highs),
        "low": min(lows),
        "close": closes[-1],
        "referenceSource": "recommendation_minute_snapshot",
        "referenceStatus": "verified",
        "minutePointCount": len(dated),
    }


def _price_consistency(
    buy_reference: Optional[float],
    previous_point: Dict[str, Any],
    data_source: str,
) -> Dict[str, Any]:
    reference_source = str((previous_point or {}).get("referenceSource") or "recommendation_daily_ohlc")
    reference_status = str((previous_point or {}).get("referenceStatus") or "")
    reference_missing = bool((previous_point or {}).get("referenceMissing"))
    previous_close = _safe_number((previous_point or {}).get("close"))
    previous_high = _safe_number((previous_point or {}).get("high"))
    previous_low = _safe_number((previous_point or {}).get("low"))
    if reference_missing or (not previous_close and not previous_high and not previous_low):
        return {
            "ok": True,
            "verified": False,
            "status": "reference_unavailable",
            "buyReferencePrice": buy_reference,
            "referenceClose": previous_close,
            "referenceHigh": previous_high,
            "referenceLow": previous_low,
            "referenceDate": str((previous_point or {}).get("date") or ""),
            "referenceSource": reference_source,
            "diffPct": None,
            "rangeDiffPct": None,
            "rangeOk": None,
            "warningThresholdPct": PRICE_MISMATCH_WARNING_PCT,
            "thresholdPct": PRICE_MISMATCH_INVALID_PCT,
            "severeThresholdPct": PRICE_MISMATCH_SEVERE_PCT,
            "dataSource": data_source,
            "priceAdjustment": "unadjusted_expected",
            "reason": "推荐日价格校验证据不足：未取到推荐日日线，也没有可用的推荐时分钟快照。收益可展示，但不进入严格优化。",
        }
    if not buy_reference or buy_reference <= 0 or not previous_close or previous_close <= 0:
        return {
            "ok": False,
            "verified": False,
            "status": "missing_reference",
            "buyReferencePrice": buy_reference,
            "referenceClose": previous_close,
            "referenceHigh": previous_high,
            "referenceLow": previous_low,
            "referenceDate": str((previous_point or {}).get("date") or ""),
            "referenceSource": reference_source,
            "diffPct": None,
            "thresholdPct": PRICE_MISMATCH_INVALID_PCT,
            "reason": "缺少推荐价或推荐日日线参考价，无法确认价格口径。",
        }
    diff_pct = round(abs(float(buy_reference) - previous_close) / float(buy_reference) * 100, 2)
    range_ok = False
    range_diff_pct = None
    if previous_high and previous_low and previous_high > 0 and previous_low > 0:
        tolerance = PRICE_MISMATCH_INVALID_PCT / 100
        lower = previous_low * (1 - tolerance)
        upper = previous_high * (1 + tolerance)
        range_ok = lower <= float(buy_reference) <= upper
        if not range_ok:
            if float(buy_reference) > upper:
                range_diff_pct = round((float(buy_reference) / previous_high - 1) * 100, 2)
            elif float(buy_reference) < lower:
                range_diff_pct = round((previous_low / float(buy_reference) - 1) * 100, 2)
    status = "ok"
    ok = True
    reason = ""
    if previous_high and previous_low and not range_ok and (range_diff_pct or 0) > PRICE_MISMATCH_SEVERE_PCT:
        status = "severe_mismatch"
        ok = False
        reason = f"推荐价超出验证源推荐日 OHLC 区间 {range_diff_pct:.2f}%，疑似复权口径或脏数据。"
    elif previous_high and previous_low and not range_ok and (range_diff_pct or 0) > PRICE_MISMATCH_INVALID_PCT:
        status = "mismatch"
        ok = False
        reason = f"推荐价超出验证源推荐日 OHLC 区间 {range_diff_pct:.2f}%，超过 {PRICE_MISMATCH_INVALID_PCT:.1f}% 阈值。"
    elif not previous_high and diff_pct > PRICE_MISMATCH_SEVERE_PCT:
        status = "severe_mismatch"
        ok = False
        reason = f"推荐价与验证源推荐日收盘价偏差 {diff_pct:.2f}%，疑似复权口径或脏数据。"
    elif not previous_high and diff_pct > PRICE_MISMATCH_INVALID_PCT:
        status = "mismatch"
        ok = False
        reason = f"推荐价与验证源推荐日收盘价偏差 {diff_pct:.2f}%，超过 {PRICE_MISMATCH_INVALID_PCT:.1f}% 阈值。"
    elif diff_pct > PRICE_MISMATCH_WARNING_PCT:
        status = "warning"
        reason = f"推荐价与验证源推荐日价格偏差 {diff_pct:.2f}%，已标记观察。"
    return {
        "ok": ok,
        "verified": ok,
        "status": status,
        "buyReferencePrice": buy_reference,
        "referenceClose": previous_close,
        "referenceHigh": previous_high,
        "referenceLow": previous_low,
        "referenceDate": str((previous_point or {}).get("date") or ""),
        "referenceSource": reference_source,
        "referenceStatus": reference_status,
        "diffPct": diff_pct,
        "rangeDiffPct": range_diff_pct,
        "rangeOk": range_ok,
        "warningThresholdPct": PRICE_MISMATCH_WARNING_PCT,
        "thresholdPct": PRICE_MISMATCH_INVALID_PCT,
        "severeThresholdPct": PRICE_MISMATCH_SEVERE_PCT,
        "dataSource": data_source,
        "priceAdjustment": "unadjusted_expected",
        "reason": reason,
    }


def _corporate_action_signal(
    previous_point: Dict[str, Any],
    next_point: Dict[str, Any],
    buy_reference: Optional[float],
    price_consistency: Dict[str, Any],
) -> Dict[str, Any]:
    previous_close = _safe_number((previous_point or {}).get("close"))
    next_pre_close = _safe_number((next_point or {}).get("preClose") or (next_point or {}).get("preclose") or (next_point or {}).get("pre_close"))
    next_open = _safe_number((next_point or {}).get("open"))
    next_high = _safe_number((next_point or {}).get("high"))
    next_low = _safe_number((next_point or {}).get("low"))
    if previous_close and next_pre_close and previous_close > 0 and next_pre_close > 0:
        diff_pct = round(abs(next_pre_close / previous_close - 1) * 100, 2)
        if diff_pct > CORPORATE_ACTION_PRE_CLOSE_MISMATCH_PCT:
            return {
                "suspected": True,
                "status": "pre_close_mismatch",
                "previousClose": previous_close,
                "nextPreClose": next_pre_close,
                "diffPct": diff_pct,
                "thresholdPct": CORPORATE_ACTION_PRE_CLOSE_MISMATCH_PCT,
                "reason": f"次日前收与推荐日收盘偏差 {diff_pct:.2f}%，疑似除权除息、公司行为或复权口径断点。",
            }
    if previous_close and next_open and next_high and next_low and previous_close > 0:
        gap_pct = round((next_open / previous_close - 1) * 100, 2)
        whole_range_detached = next_high < previous_close * 0.85 or next_low > previous_close * 1.15
        if abs(gap_pct) >= CORPORATE_ACTION_EXTREME_GAP_PCT and whole_range_detached:
            return {
                "suspected": True,
                "status": "extreme_gap",
                "previousClose": previous_close,
                "nextOpen": next_open,
                "gapPct": gap_pct,
                "thresholdPct": CORPORATE_ACTION_EXTREME_GAP_PCT,
                "reason": f"次日价格区间与推荐日收盘出现 {gap_pct:.2f}% 极端断点，疑似公司行为或复权口径异常。",
            }
    if price_consistency.get("status") == "severe_mismatch":
        return {
            "suspected": True,
            "status": "reference_severe_mismatch",
            "diffPct": price_consistency.get("rangeDiffPct") or price_consistency.get("diffPct"),
            "reason": "推荐价与推荐日未复权价格严重不一致，按疑似公司行为/复权污染隔离。",
        }
    return {"suspected": False, "status": "ok", "reason": ""}


def _context_failure_tags(
    primary_net_return: Optional[float],
    pick: Dict[str, Any],
    data_quality: Dict[str, Any],
    price_consistency: Dict[str, Any],
    corporate_action: Dict[str, Any],
    market: Dict[str, Any],
) -> List[str]:
    tags: List[str] = []
    if corporate_action.get("suspected"):
        tags.append("corporate_action")
    if price_consistency.get("ok") is False:
        tags.append("price_mismatch")
    if (data_quality.get("grade") or "") in {"C", "D", "E"}:
        tags.append("data_quality")
    tradeability = pick.get("tradeability") or (pick.get("item") or {}).get("tradeability") or {}
    if str(tradeability.get("level") or "") in {"hard-to-buy", "chase-risk"}:
        tags.append("liquidity")
    market_context = market.get("marketContext") or {}
    if primary_net_return is not None and primary_net_return < 0 and market_context.get("state") in {"panic", "heavy_down", "mild_down"}:
        tags.append("market_drag")
    sector_status = str(pick.get("sectorEvidenceStatus") or (pick.get("item") or {}).get("sectorEvidenceStatus") or "")
    sector_rank = int(pick.get("industryRank") or (pick.get("item") or {}).get("industryRank") or 0)
    sector_limitups = int(pick.get("industryLimitUpCount") or (pick.get("item") or {}).get("industryLimitUpCount") or 0)
    if primary_net_return is not None and primary_net_return < 0 and (sector_status in {"unavailable", "stale"} or sector_rank >= 8 or sector_limitups <= 0):
        tags.append("sector_fade")
    return tags


def _validation_status_for_outcome(
    primary: Dict[str, Any],
    data_quality: Dict[str, Any],
    price_consistency: Dict[str, Any],
    daily_status: str = "final",
) -> str:
    if not price_consistency.get("ok", True):
        return "invalid"
    if data_quality.get("grade") in {"D", "E"}:
        return "invalid"
    if primary.get("dataStatus") == "ok" and primary.get("netReturnPct") is not None:
        if daily_status == "final":
            return str(primary.get("validationStatus") or "final")
        return daily_status or "provisional"
    return "unavailable"


def _learning_eligibility(
    primary: Dict[str, Any],
    data_quality: Dict[str, Any],
    data_source: str,
    price_consistency: Dict[str, Any],
    validation_status: str,
) -> tuple[bool, List[str]]:
    reasons: List[str] = []
    source_text = str(data_source or "")
    if validation_status != "final":
        reasons.append(f"验证状态为 {validation_status}")
    if primary.get("scenarioKey") != PRIMARY_TRADEABLE_SCENARIO_KEY or primary.get("dataStatus") != "ok" or primary.get("netReturnPct") is None:
        reasons.append("固定 15m 主 KPI 不可用")
    if data_quality.get("grade") not in {"A", "B"}:
        reasons.append(f"数据质量 {_data_quality_label(str(data_quality.get('grade') or ''))} 不进入严格学习")
    if price_consistency.get("status") == "reference_unavailable":
        reasons.append("推荐日买入价未完成校验")
    if "mock" in source_text or "simulated" in source_text:
        reasons.append("验证源含模拟数据")
    if "cache" in source_text:
        reasons.append("验证源为缓存")
    if not price_consistency.get("ok", True):
        reasons.append(price_consistency.get("reason") or "价格口径不一致")
    clean = []
    for reason in reasons:
        if reason and reason not in clean:
            clean.append(reason)
    return not clean, clean


def _worse_data_quality_grade(left: str, right: str) -> str:
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
    left = left if left in order else ""
    right = right if right in order else ""
    if not left:
        return right or "E"
    if not right:
        return left
    return left if order[left] >= order[right] else right


def _build_buy_scenarios(run: Dict[str, Any], buy_reference: float, execution_profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    scan_time = str(run.get("scanTime") or "")
    rows = []
    for buy_time in execution_profile["buyTimes"]:
        available = buy_time == scan_time and buy_reference > 0
        rows.append(
            {
                "buyTime": buy_time,
                "buyPrice": buy_reference if available else None,
                "dataStatus": "ok" if available else "snapshot_missing",
                "source": "run_snapshot" if available else "",
            }
        )
    if scan_time and scan_time not in execution_profile["buyTimes"]:
        rows.append(
            {
                "buyTime": scan_time,
                "buyPrice": buy_reference,
                "dataStatus": "ok",
                "source": "run_snapshot",
            }
        )
    return rows


def _build_tradeable_scenarios(
    code: str,
    buy_reference: float,
    next_point: Dict[str, Any],
    minute_series: List[Dict[str, Any]],
    minute_source: str,
    execution_profile: Dict[str, Any],
    verified_at: str = "",
) -> List[Dict[str, Any]]:
    next_trade_date = str(next_point.get("date") or "")
    dated_minute_series = _minute_series_for_trade_date(minute_series, next_trade_date)
    minute_available = bool(dated_minute_series)
    daily_source = "minute_intraday" if next_point.get("minuteDerived") else "daily_ohlc"
    daily_precision = "minute_derived_ohlc" if next_point.get("minuteDerived") else "daily_ohlc"
    scenarios = []
    enabled_sell_times = set(execution_profile.get("sellTimes") or DEFAULT_EXECUTION_PROFILE["sellTimes"])
    for definition in TRADEABLE_SCENARIO_DEFINITIONS:
        key = definition["key"]
        label = definition["label"]
        source_type = definition["source"]
        target_time = definition["time"]
        if key not in enabled_sell_times:
            continue
        sell_price = None
        data_status = "ok"
        time_precision = daily_precision
        sell_time = label
        if source_type == "open":
            sell_price = _safe_number(next_point.get("open"))
        elif source_type == "close":
            sell_price = _safe_number(next_point.get("close"))
        elif not minute_available:
            data_status = "minute_missing"
            time_precision = "missing"
        else:
            sell_price, matched_time, time_precision = _minute_close_at_or_after(dated_minute_series, target_time)
            sell_time = matched_time or label
            if sell_price is None:
                data_status = "minute_missing"
                time_precision = "missing"
        validation_status = _scenario_validation_status(
            trade_date=next_trade_date,
            target_time=target_time,
            source_type=source_type,
            data_status=data_status,
            verified_at=verified_at,
        )
        gross = _pct_return(sell_price or 0, buy_reference) if sell_price else None
        net = _net_return_pct(gross, execution_profile) if gross is not None else None
        scenarios.append(
            {
                "code": code,
                "validationVersion": TRADEABLE_VALIDATION_VERSION,
                "scenarioKey": key,
                "scenarioLabel": label,
                "sellTime": sell_time,
                "sellPrice": sell_price,
                "grossReturnPct": gross,
                "netReturnPct": net,
                "dataStatus": data_status,
                "validationStatus": validation_status,
                "status": validation_status,
                "dataSource": minute_source if source_type == "minute" and minute_available else daily_source,
                "targetTime": target_time,
                "timePrecision": time_precision,
                "costModel": _roundtrip_cost_model(execution_profile),
            }
        )
    return scenarios


def _minute_series_for_trade_date(series: List[Dict[str, Any]], trade_date: str) -> List[Dict[str, Any]]:
    if not series:
        return []
    dated = [item for item in series if _minute_date(item) == trade_date]
    if dated:
        return sorted(dated, key=lambda item: _minute_time(item) or "")
    if trade_date == date.today().isoformat() and not any(_minute_date(item) for item in series):
        return list(series)
    return []


def _minute_series_from_validation_snapshot(snapshot: Dict[str, Any], trade_date: str) -> List[Dict[str, Any]]:
    series = list((snapshot or {}).get("series") or [])
    if not series or not trade_date:
        return []
    dated = _minute_series_for_trade_date(series, trade_date)
    if dated:
        return dated
    if any(_minute_date(item) for item in series):
        return []
    created_at = str((snapshot or {}).get("createdAt") or "")
    if created_at[:10] != trade_date:
        return []
    period = str((snapshot or {}).get("period") or "1m")
    bucket_minutes = _period_minutes_for_t1(period)
    explicit_times = [_minute_time(item) for item in series]
    fallback_times = _minute_bucket_times(period, len(series))
    annotated = []
    for index, item in enumerate(series):
        row = dict(item or {})
        item_time = explicit_times[index] if index < len(explicit_times) else ""
        if not item_time and index < len(fallback_times):
            item_time = fallback_times[index]
            row["_timeInferredFromSameDaySnapshot"] = True
        if item_time:
            row["time"] = f"{trade_date} {item_time}:00"
            row["_bucketPeriodMinutes"] = bucket_minutes
            row["_validationSnapshotAnchored"] = True
            annotated.append(row)
    return annotated


def _dated_minute_series_by_code(minute_by_code: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    dated: Dict[str, List[Dict[str, Any]]] = {}
    for code, series in (minute_by_code or {}).items():
        rows = [item for item in (series or []) if _minute_date(item)]
        if rows:
            dated[str(code or "").zfill(6)] = rows
    return dated


def _minute_series_from_t1_snapshot(snapshot: Dict[str, Any], trade_date: str) -> List[Dict[str, Any]]:
    series = list((snapshot or {}).get("series") or [])
    if not series or not trade_date:
        return []
    if any(_minute_date(item) for item in series):
        return [item for item in series if _minute_date(item) == trade_date]
    period = str((snapshot or {}).get("period") or "15m")
    bucket_minutes = _period_minutes_for_t1(period)
    times = _minute_bucket_times(period, len(series))
    annotated = []
    for index, item in enumerate(series):
        row = dict(item or {})
        bucket_time = times[index] if index < len(times) else ""
        if bucket_time:
            row["time"] = f"{trade_date} {bucket_time}:00"
            row["_bucketPeriodMinutes"] = bucket_minutes
        annotated.append(row)
    return annotated


def _minute_bucket_times(period: str, count: int) -> List[str]:
    minutes = _period_minutes_for_t1(period)
    if minutes <= 1:
        return _minute_intraday_times(count)
    raw = _minute_intraday_bucket_end_times()
    result = []
    for item in raw:
        result.append(item)
        if len(result) >= count:
            break
    return result


def _minute_intraday_bucket_end_times() -> List[str]:
    return [
        "09:45",
        "10:00",
        "10:15",
        "10:30",
        "10:45",
        "11:00",
        "11:15",
        "11:30",
        "13:15",
        "13:30",
        "13:45",
        "14:00",
        "14:15",
        "14:30",
        "14:45",
        "15:00",
    ]


def _minute_intraday_times(count: int) -> List[str]:
    result = []
    for hour, start, end in [(9, 30, 60), (10, 0, 60), (11, 0, 30), (13, 0, 60), (14, 0, 60), (15, 0, 1)]:
        for minute in range(start, end):
            if hour == 15 and minute > 0:
                continue
            result.append(f"{hour:02d}:{minute:02d}")
            if len(result) >= count:
                return result
    return result[:count]


def _period_minutes_for_t1(period: str) -> int:
    text = str(period or "15m").strip().lower()
    if text.endswith("m"):
        try:
            return max(1, int(text[:-1]))
        except ValueError:
            return 15
    return 15


def _allow_undated_t1_verification_minutes(recommendation_date: str) -> bool:
    expected = _next_weekday_date(recommendation_date)
    return bool(expected and expected == date.today().isoformat())


def _next_weekday_date(text: str) -> str:
    try:
        current = date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return ""
    for _ in range(7):
        current = current.fromordinal(current.toordinal() + 1)
        if current.weekday() < 5:
            return current.isoformat()
    return ""


def _previous_weekday_date(text: str) -> str:
    try:
        current = date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return ""
    for _ in range(7):
        current = current - timedelta(days=1)
        if current.weekday() < 5:
            return current.isoformat()
    return ""


def _normalize_validation_auto_freeze_config(config: Any) -> Dict[str, Any]:
    raw = dict(config or {}) if isinstance(config, dict) else {}
    normalized = dict(DEFAULT_VALIDATION_AUTO_FREEZE_CONFIG)
    normalized.update({key: value for key, value in raw.items() if key in normalized})
    schedule_times = raw.get("scheduleTimes", normalized.get("scheduleTimes"))
    if not isinstance(schedule_times, list):
        schedule_times = DEFAULT_VALIDATION_AUTO_FREEZE_CONFIG["scheduleTimes"]
    normalized["scheduleTimes"] = _unique_strings([
        _normalize_scan_time(item)
        for item in schedule_times
        if str(item or "").strip()
    ]) or list(DEFAULT_VALIDATION_AUTO_FREEZE_CONFIG["scheduleTimes"])
    normalized["enabled"] = bool(normalized.get("enabled"))
    normalized["pollIntervalSeconds"] = _clamp_int(normalized.get("pollIntervalSeconds"), 30, 600, 60)
    normalized["scope"] = str(normalized.get("scope") or "all") if str(normalized.get("scope") or "").lower() in {"final", "all"} else "all"
    normalized["period"] = _normalize_validation_minute_period(str(normalized.get("period") or "1m"))
    normalized["lookbackRunLimit"] = _clamp_int(normalized.get("lookbackRunLimit"), 50, 2000, 500)
    normalized["retainDays"] = _clamp_int(normalized.get("retainDays"), 3, 60, 10)
    return normalized


def _normalize_validation_auto_freeze_state(state: Any) -> Dict[str, Any]:
    raw = dict(state or {}) if isinstance(state, dict) else {}
    config = _normalize_validation_auto_freeze_config(raw.get("config") or DEFAULT_VALIDATION_AUTO_FREEZE_CONFIG)
    days = raw.get("days") if isinstance(raw.get("days"), dict) else {}
    return {
        "version": "v1_auto_validation_minute_freeze",
        "config": config,
        "days": days,
        "lastCheckAt": str(raw.get("lastCheckAt") or ""),
        "lastRunAt": str(raw.get("lastRunAt") or ""),
        "lastStatus": str(raw.get("lastStatus") or "unknown"),
        "lastMessage": str(raw.get("lastMessage") or ""),
        "lastEvent": raw.get("lastEvent") if isinstance(raw.get("lastEvent"), dict) else {},
    }


def _normalize_auto_run_config(config: Any) -> Dict[str, Any]:
    raw = dict(config or {}) if isinstance(config, dict) else {}
    normalized = dict(DEFAULT_AUTO_RUN_CONFIG)
    normalized.update({key: value for key, value in raw.items() if key in normalized})
    schedule_times = raw.get("scheduleTimes", normalized.get("scheduleTimes"))
    if not isinstance(schedule_times, list):
        schedule_times = DEFAULT_AUTO_RUN_CONFIG["scheduleTimes"]
    normalized["scheduleTimes"] = _unique_strings([
        _normalize_scan_time(item)
        for item in schedule_times
        if str(item or "").strip()
    ]) or list(DEFAULT_AUTO_RUN_CONFIG["scheduleTimes"])
    normalized["enabled"] = bool(normalized.get("enabled"))
    normalized["latestTriggerTime"] = _normalize_scan_time(normalized.get("latestTriggerTime") or OFFICIAL_SCAN_END)
    normalized["pollIntervalSeconds"] = _clamp_int(normalized.get("pollIntervalSeconds"), 30, 600, 60)
    normalized["source"] = str(normalized.get("source") or "auto") if str(normalized.get("source") or "") in {"auto", "sina", "akshare", "mock"} else "auto"
    normalized["scanTime"] = _normalize_scan_time(normalized.get("scanTime") or DEFAULT_SCAN_TIME)
    normalized["poolSize"] = _clamp_int(normalized.get("poolSize"), T1_OFFICIAL_MIN_POOL_SIZE, 600, DEFAULT_POOL_SIZE)
    normalized["batchSize"] = _clamp_int(normalized.get("batchSize"), 20, 100, DEFAULT_BATCH_SIZE)
    normalized["batchPick"] = _clamp_int(normalized.get("batchPick"), 2, 12, DEFAULT_BATCH_PICK)
    normalized["finalLimit"] = _clamp_int(normalized.get("finalLimit"), 1, 5, DEFAULT_FINAL_LIMIT)
    normalized["force"] = bool(normalized.get("force"))
    normalized["retainDays"] = _clamp_int(normalized.get("retainDays"), 3, 60, 10)
    return normalized


def _normalize_auto_run_state(state: Any) -> Dict[str, Any]:
    raw = dict(state or {}) if isinstance(state, dict) else {}
    config = _normalize_auto_run_config(raw.get("config") or DEFAULT_AUTO_RUN_CONFIG)
    days = raw.get("days") if isinstance(raw.get("days"), dict) else {}
    return {
        "version": "v1_t1_auto_run",
        "config": config,
        "days": days,
        "lastCheckAt": str(raw.get("lastCheckAt") or ""),
        "lastRunAt": str(raw.get("lastRunAt") or ""),
        "lastStatus": str(raw.get("lastStatus") or "unknown"),
        "lastMessage": str(raw.get("lastMessage") or ""),
        "lastEvent": raw.get("lastEvent") if isinstance(raw.get("lastEvent"), dict) else {},
    }


def _normalize_market_prefetch_config(config: Any) -> Dict[str, Any]:
    raw = dict(config or {}) if isinstance(config, dict) else {}
    normalized = dict(DEFAULT_MARKET_PREFETCH_CONFIG)
    normalized.update({key: value for key, value in raw.items() if key in normalized})
    schedule_times = raw.get("scheduleTimes", normalized.get("scheduleTimes"))
    if not isinstance(schedule_times, list):
        schedule_times = DEFAULT_MARKET_PREFETCH_CONFIG["scheduleTimes"]
    normalized["scheduleTimes"] = _unique_strings(
        [_normalize_scan_time(item) for item in schedule_times if str(item or "").strip()]
    ) or list(DEFAULT_MARKET_PREFETCH_CONFIG["scheduleTimes"])
    normalized["enabled"] = bool(normalized.get("enabled"))
    normalized["pollIntervalSeconds"] = _clamp_int(normalized.get("pollIntervalSeconds"), 15, 300, 45)
    normalized["source"] = str(normalized.get("source") or "auto") if str(normalized.get("source") or "") in {"auto", "sina", "tencent", "akshare"} else "auto"
    normalized["retainDays"] = _clamp_int(normalized.get("retainDays"), 3, 60, 10)
    return normalized


def _normalize_market_prefetch_state(state: Any) -> Dict[str, Any]:
    raw = dict(state or {}) if isinstance(state, dict) else {}
    config = _normalize_market_prefetch_config(raw.get("config") or DEFAULT_MARKET_PREFETCH_CONFIG)
    days = raw.get("days") if isinstance(raw.get("days"), dict) else {}
    return {
        "version": "v1_t1_market_prefetch",
        "config": config,
        "days": days,
        "lastCheckAt": str(raw.get("lastCheckAt") or ""),
        "lastStatus": str(raw.get("lastStatus") or "unknown"),
        "lastMessage": str(raw.get("lastMessage") or ""),
        "lastEvent": raw.get("lastEvent") if isinstance(raw.get("lastEvent"), dict) else {},
    }


def _market_prefetch_due_slots(
    now_dt: datetime,
    config: Dict[str, Any],
    state: Dict[str, Any],
    include_future: bool = False,
) -> List[str]:
    today = now_dt.date().isoformat()
    current_time = now_dt.strftime("%H:%M")
    day_state = (state.get("days") or {}).get(today) or {}
    slots = []
    for slot in config.get("scheduleTimes") or []:
        normalized = _normalize_scan_time(slot)
        if not normalized or normalized in day_state:
            continue
        if include_future and normalized >= current_time:
            slots.append(normalized)
        elif not include_future and normalized <= current_time:
            slots.append(normalized)
    return slots


def _record_market_prefetch_event(
    state: Dict[str, Any],
    trade_date: str,
    slot: str,
    event: Dict[str, Any],
    config: Dict[str, Any],
) -> None:
    days = state.setdefault("days", {})
    day_state = days.setdefault(trade_date, {})
    day_state[slot] = event
    state["lastEvent"] = event
    retain_days = int(config.get("retainDays") or DEFAULT_MARKET_PREFETCH_CONFIG["retainDays"])
    cutoff = (date.today() - timedelta(days=retain_days)).isoformat()
    for day_key in list(days.keys()):
        if str(day_key) < cutoff:
            days.pop(day_key, None)


def _auto_run_skip_event(
    trade_date: str,
    slot: str,
    checked_at: str,
    trigger_source: str,
    message: str,
    status: str = "skipped",
) -> Dict[str, Any]:
    return {
        "status": status,
        "slot": slot,
        "tradeDate": trade_date,
        "triggerSource": trigger_source,
        "checkedAt": checked_at,
        "message": message,
    }


def _validation_auto_freeze_due_slots(
    now_dt: datetime,
    config: Dict[str, Any],
    state: Dict[str, Any],
    include_future: bool = False,
) -> List[str]:
    today = now_dt.date().isoformat()
    current_time = now_dt.strftime("%H:%M")
    day_state = (state.get("days") or {}).get(today) or {}
    slots = []
    for slot in config.get("scheduleTimes") or []:
        normalized = _normalize_scan_time(slot)
        if not normalized:
            continue
        if include_future:
            if normalized >= current_time and normalized not in day_state:
                slots.append(normalized)
            continue
        if normalized <= current_time and normalized not in day_state:
            slots.append(normalized)
    return slots


def _record_validation_auto_freeze_event(
    state: Dict[str, Any],
    trade_date: str,
    slot: str,
    event: Dict[str, Any],
    config: Dict[str, Any],
) -> None:
    days = state.setdefault("days", {})
    day_state = days.setdefault(trade_date, {})
    day_state[slot] = event
    state["lastEvent"] = event
    retain_days = int(config.get("retainDays") or DEFAULT_VALIDATION_AUTO_FREEZE_CONFIG["retainDays"])
    cutoff = (date.today() - timedelta(days=retain_days)).isoformat()
    for day_key in list(days.keys()):
        if str(day_key) < cutoff:
            days.pop(day_key, None)


def _minute_derived_next_trade_point(
    minute_series: List[Dict[str, Any]],
    recommendation_date: str,
    verified_at: str = "",
) -> Dict[str, Any]:
    trade_date = _minute_next_trade_date(minute_series, recommendation_date, verified_at)
    if not trade_date:
        return {}
    dated_series = _minute_series_for_trade_date(minute_series, trade_date)
    if not dated_series:
        return {}
    open_price = _safe_number(dated_series[0].get("open") or dated_series[0].get("close"))
    close_price = _safe_number(dated_series[-1].get("close"))
    highs = [_safe_number(item.get("high") or item.get("close") or item.get("open")) for item in dated_series]
    lows = [_safe_number(item.get("low") or item.get("close") or item.get("open")) for item in dated_series]
    highs = [value for value in highs if value is not None and value > 0]
    lows = [value for value in lows if value is not None and value > 0]
    if not open_price or not close_price or not highs or not lows:
        return {}
    return {
        "date": trade_date,
        "open": open_price,
        "high": max(highs),
        "low": min(lows),
        "close": close_price,
        "volume": sum(float(_safe_number(item.get("volume")) or 0) for item in dated_series),
        "amount": sum(float(_safe_number(item.get("amount")) or 0) for item in dated_series),
        "change": 0.0,
        "turnover": 0.0,
        "minuteDerived": True,
        "minutePointCount": len(dated_series),
        "source": "minute_intraday",
        "complete": _verified_time_text(verified_at) >= DAILY_FINAL_AFTER_TIME,
    }


def _minute_next_trade_date(
    minute_series: List[Dict[str, Any]],
    recommendation_date: str,
    verified_at: str = "",
) -> str:
    if not minute_series or not recommendation_date:
        return ""
    dated = sorted({
        _minute_date(item)
        for item in minute_series
        if _minute_date(item) and _minute_date(item) > recommendation_date
    })
    if dated:
        return dated[0]
    if any(_minute_date(item) for item in minute_series):
        return ""
    today = date.today()
    try:
        recommendation = date.fromisoformat(str(recommendation_date)[:10])
    except ValueError:
        return ""
    if today <= recommendation:
        return ""
    if (today - recommendation).days > MINUTE_UNDATED_NEXT_POINT_MAX_CALENDAR_DAYS:
        return ""
    if _verified_time_text(verified_at) < "09:30":
        return ""
    return today.isoformat()


def _minute_date(item: Dict[str, Any]) -> str:
    text = str(item.get("time") or item.get("datetime") or item.get("date") or "")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def _minute_time(item: Dict[str, Any]) -> str:
    text = str(item.get("time") or item.get("datetime") or item.get("date") or "")
    if len(text) >= 16 and text[10] in {" ", "T"}:
        return text[11:16]
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return ""


def _minute_close_at_or_after(series: List[Dict[str, Any]], target_time: str) -> tuple[Optional[float], str, str]:
    if not series:
        return None, "", "missing"
    timed = [(item, _minute_time(item)) for item in series]
    timed = [(item, item_time) for item, item_time in timed if item_time]
    if timed:
        for item, item_time in sorted(timed, key=lambda row: row[1]):
            if item_time >= target_time:
                bucket_minutes = int((item or {}).get("_bucketPeriodMinutes") or 1)
                if bucket_minutes > 1 and item_time != target_time:
                    continue
                return _safe_number((item or {}).get("close")), item_time, "time_exact_or_after"
        return None, "", "target_unavailable"
    fallback_index = _minute_index_for_target(target_time)
    item = series[max(0, min(len(series) - 1, fallback_index))]
    return _safe_number((item or {}).get("close")), target_time, "index_fallback"


def _minute_index_for_target(target_time: str) -> int:
    mapping = {
        "09:30": 0,
        "09:35": 5,
        "09:45": 15,
        "10:00": 30,
        "11:30": 120,
        "13:00": 121,
        "14:00": 181,
        "15:00": 240,
    }
    return mapping.get(target_time, 0)


def _scenario_validation_status(
    trade_date: str,
    target_time: str,
    source_type: str,
    data_status: str,
    verified_at: str = "",
) -> str:
    if data_status != "ok":
        return "unavailable"
    if not trade_date:
        return "unavailable"
    today = date.today().isoformat()
    if trade_date < today:
        return "final"
    if trade_date > today:
        return "pending"
    current_time = _verified_time_text(verified_at)
    final_after = DAILY_FINAL_AFTER_TIME if source_type == "close" else target_time
    return "final" if current_time >= final_after else "provisional"


def _daily_validation_status(trade_date: str, verified_at: str = "") -> str:
    if not trade_date:
        return "unavailable"
    today = date.today().isoformat()
    if trade_date < today:
        return "final"
    if trade_date > today:
        return "pending"
    return "final" if _verified_time_text(verified_at) >= DAILY_FINAL_AFTER_TIME else "provisional"


def _verified_time_text(verified_at: str = "") -> str:
    text = str(verified_at or "")
    if len(text) >= 16 and text[10] in {"T", " "}:
        return text[11:16]
    return datetime.now().strftime("%H:%M")


def _roundtrip_cost_model(execution_profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    profile = _normalize_execution_config(execution_profile or DEFAULT_EXECUTION_PROFILE)
    return {
        "buySlippagePct": profile["buySlippagePct"],
        "sellSlippagePct": profile["sellSlippagePct"],
        "commissionPct": profile["commissionPct"],
        "stampTaxPct": profile["stampTaxPct"],
        "transferFeePct": profile["transferFeePct"],
        "roundtripCostPct": _roundtrip_cost_pct(profile),
    }


def _roundtrip_cost_pct(execution_profile: Optional[Dict[str, Any]] = None) -> float:
    profile = _normalize_execution_config(execution_profile or DEFAULT_EXECUTION_PROFILE)
    return round(
        float(profile["buySlippagePct"])
        + float(profile["sellSlippagePct"])
        + float(profile["commissionPct"]) * 2
        + float(profile["stampTaxPct"])
        + float(profile["transferFeePct"]) * 2,
        4,
    )


def _net_return_pct(gross_return_pct: Optional[float], execution_profile: Optional[Dict[str, Any]] = None) -> Optional[float]:
    if gross_return_pct is None:
        return None
    return round(float(gross_return_pct) - _roundtrip_cost_pct(execution_profile), 2)


def _primary_tradeable_scenario(scenarios: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_key = {item.get("scenarioKey"): item for item in scenarios}
    item = by_key.get(PRIMARY_TRADEABLE_SCENARIO_KEY) or {}
    if item.get("dataStatus") == "ok" and item.get("netReturnPct") is not None:
        return item
    if item:
        primary = dict(item)
        primary["primaryUnavailable"] = True
        primary["reason"] = primary.get("reason") or "固定主 KPI open_15m 缺失，不使用其他时间点 fallback。"
        return primary
    return {
        "scenarioKey": PRIMARY_TRADEABLE_SCENARIO_KEY,
        "scenarioLabel": "开盘后15分钟",
        "dataStatus": "unavailable",
        "primaryUnavailable": True,
        "reason": "未生成 open_15m 场景。",
    }


def _trade_quality_flags(
    open_return: Optional[float],
    high_return: Optional[float],
    close_return: Optional[float],
    max_drawdown: Optional[float],
) -> Dict[str, Any]:
    open_pct = open_return if open_return is not None else 0
    high = high_return if high_return is not None else 0
    close = close_return if close_return is not None else 0
    drawdown = max_drawdown if max_drawdown is not None else 0
    return {
        "highOpenFade": open_pct >= 1.5 and close < open_pct - 1.5,
        "lowOpenRebound": open_pct <= -1 and high > 0,
        "intradayPullback": high >= 2 and close < high - 2,
        "failureTags": _failure_tags(None, open_return, high_return, close_return, max_drawdown),
        "maxDrawdownPct": drawdown,
        "highReturnPct": high,
        "closeReturnPct": close,
    }


def _rule_execution_result(
    code: str,
    scenarios: List[Dict[str, Any]],
    open_return: Optional[float],
    high_return: Optional[float],
    close_return: Optional[float],
    daily_status: str = "final",
) -> Dict[str, Any]:
    by_key = {item.get("scenarioKey"): item for item in scenarios}
    minute_ready = all((by_key.get(key) or {}).get("dataStatus") == "ok" for key in ["open_5m", "open_15m"])
    base = {
        "code": code,
        "validationVersion": TRADEABLE_VALIDATION_VERSION,
        "scenarioKey": "rule_exit",
        "scenarioLabel": "规则卖出",
        "sellTime": "规则卖出",
        "sellPrice": None,
        "grossReturnPct": None,
        "netReturnPct": None,
        "dataStatus": "minute_missing",
        "status": "minute_missing",
        "validationStatus": "unavailable",
        "label": "规则卖出待分钟线",
        "reason": "缺少次日 5/15 分钟数据，暂不模拟规则卖出。",
        "sourceScenarioKey": "",
    }
    if not minute_ready:
        return base
    open_pct = open_return if open_return is not None else 0
    high = high_return if high_return is not None else 0
    close = close_return if close_return is not None else 0
    if open_pct <= -2:
        scenario = by_key.get("open_5m") or by_key.get("open")
        reason = "低开超过阈值，开盘后快速止损。"
    elif open_pct >= 2 and close < open_pct - 1.5:
        scenario = by_key.get("open_5m") or by_key.get("open")
        reason = "高开后走弱，开盘后 5 分钟先兑现。"
    elif high >= 5 and close < high - 2:
        scenario = by_key.get("open_15m") or by_key.get("open_5m")
        reason = "盘中冲高后回落，按止盈纪律处理。"
    elif (by_key.get("open_15m") or {}).get("netReturnPct") is not None and float((by_key.get("open_15m") or {}).get("netReturnPct") or 0) <= -1:
        scenario = by_key.get("open_15m") or by_key.get("open_5m")
        reason = "开盘后 15 分钟仍弱，按时间止损处理。"
    else:
        scenario = by_key.get("open_15m") or by_key.get("open_30m")
        reason = "按开盘后 15 分钟确认执行。"
    source = scenario or {}
    base.update({
        "status": "ok",
        "label": "规则卖出估算",
        "sellPrice": source.get("sellPrice"),
        "grossReturnPct": source.get("grossReturnPct"),
        "netReturnPct": source.get("netReturnPct"),
        "dataStatus": source.get("dataStatus") or "ok",
        "validationStatus": daily_status if daily_status in {"pending", "provisional", "final"} else source.get("validationStatus") or "final",
        "sourceScenarioKey": source.get("scenarioKey", ""),
        "sourceScenarioLabel": source.get("scenarioLabel", ""),
        "reason": reason,
    })
    return base


def _classify_tradeable_outcome(
    primary_net_return: Optional[float],
    open_return: Optional[float],
    high_return: Optional[float],
    close_return: Optional[float],
    max_drawdown: Optional[float],
) -> tuple[str, float, str, bool, bool]:
    high = high_return if high_return is not None else -999
    close = close_return if close_return is not None else -999
    open_pct = open_return if open_return is not None else 0
    drawdown = max_drawdown if max_drawdown is not None else 0
    net = primary_net_return
    if net is None:
        return "数据不足", 0, "缺少可交易卖出场景。", False, False
    if drawdown <= INVALID_DRAWDOWN_THRESHOLD_PCT and net < 0:
        return "触发失效", 20, "次日最低跌幅达到失效阈值。", False, True
    if open_pct <= LOW_OPEN_FAIL_THRESHOLD_PCT and high < 1:
        return "低开失败", 15, "低开且盘中没有有效修复。", False, False
    if high >= 2 and close < 0.5 and net <= 0.5:
        return "冲高回落", 55, "盘中有溢价但按可交易口径收益较弱。", True, False
    if net >= STRONG_SUCCESS_NET_RETURN_THRESHOLD_PCT:
        return "强成功", 95, "", True, False
    if net >= 1:
        return "成功溢价", 80, "", True, False
    if net >= SUCCESS_NET_RETURN_THRESHOLD_PCT:
        return "弱成功", 62, "", False, False
    return "无效", 35, "扣除成本后的可交易收益不足。", False, False


def _failure_tags(
    primary_net_return: Optional[float],
    open_return: Optional[float],
    high_return: Optional[float],
    close_return: Optional[float],
    max_drawdown: Optional[float],
) -> List[str]:
    tags: List[str] = []
    open_pct = open_return if open_return is not None else 0
    high = high_return if high_return is not None else 0
    close = close_return if close_return is not None else 0
    drawdown = max_drawdown if max_drawdown is not None else 0
    net = primary_net_return
    if net is None:
        tags.append("primary_unavailable")
    if open_pct <= LOW_OPEN_FAIL_THRESHOLD_PCT and (net is None or net < 0):
        tags.append("low_open")
    if drawdown <= INVALID_DRAWDOWN_THRESHOLD_PCT:
        tags.append("drawdown_invalid")
    if high >= 2 and close < high - 2:
        tags.append("intraday_pullback")
    if high >= 2 and net is not None and net <= 0.5:
        tags.append("premium_not_captured")
    if close < 0 and net is not None and net < 0:
        tags.append("close_weak")
    return tags


def _flatten_validation_scenarios(outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for outcome in outcomes:
        tradeable = outcome.get("tradeable") or {}
        for scenario in tradeable.get("scenarios") or []:
            row = dict(scenario)
            row["code"] = outcome.get("code")
            row["runId"] = outcome.get("runId")
            rows.append(row)
    return rows


def _attach_validation_scenarios(outcomes: List[Dict[str, Any]], scenarios: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for scenario in scenarios:
        by_code.setdefault(str(scenario.get("code") or "").zfill(6), []).append(scenario)
    result = []
    for outcome in outcomes:
        item = dict(outcome)
        payload = dict(item.get("outcome") or {})
        scenario_rows = by_code.get(str(item.get("code") or "").zfill(6), [])
        if scenario_rows:
            tradeable = dict(payload.get("tradeable") or {})
            tradeable["scenarios"] = scenario_rows
            tradeable["primaryScenario"] = _primary_tradeable_scenario(scenario_rows)
            payload["tradeable"] = tradeable
            item["outcome"] = payload
        result.append(item)
    return result


def _next_trade_point(series: List[Dict[str, Any]], recommendation_date: str) -> Dict[str, Any]:
    points = sorted((point for point in series if str(point.get("date") or "")), key=lambda point: str(point.get("date")))
    for point in points:
        if str(point.get("date")) > recommendation_date:
            return point
    return {}


def _previous_trade_point(series: List[Dict[str, Any]], recommendation_date: str) -> Dict[str, Any]:
    points = sorted((point for point in series if str(point.get("date") or "") <= recommendation_date), key=lambda point: str(point.get("date")))
    return points[-1] if points else {}


def _pct_return(value: float, base: float) -> Optional[float]:
    if not value or not base:
        return None
    return round((value / base - 1) * 100, 2)


def _classify_outcome(
    open_return: Optional[float],
    high_return: Optional[float],
    close_return: Optional[float],
    max_drawdown: Optional[float],
) -> tuple[str, float, str, bool, bool]:
    high = high_return if high_return is not None else -999
    close = close_return if close_return is not None else -999
    open_pct = open_return if open_return is not None else 0
    drawdown = max_drawdown if max_drawdown is not None else 0
    if drawdown <= -3:
        return "触发失效", 20, "次日最低跌幅达到失效阈值。", False, True
    if open_pct <= -1.5 and high < 1:
        return "低开失败", 15, "低开且盘中没有有效修复。", False, False
    if high >= 2 and close < 0.5:
        return "冲高回落", 55, "盘中有溢价但收盘回落。", True, False
    if high >= 4:
        return "强成功", 95, "", True, False
    if high >= 2:
        return "成功溢价", 80, "", True, False
    if high >= 1:
        return "弱成功", 62, "", False, False
    return "无效", 35, "次日最高涨幅不足 1%。", False, False


def _outcome_status(run: Dict[str, Any], outcomes: List[Dict[str, Any]]) -> str:
    if outcomes:
        labels = [item.get("outcomeLabel") for item in outcomes]
        statuses = [_outcome_payload(item).get("validationStatus") or item.get("validationStatus") for item in outcomes]
        if any(status == "invalid" for status in statuses):
            return "invalid"
        if statuses and all(status == "legacy" for status in statuses):
            return "legacy"
        if any(status == "provisional" for status in statuses):
            return "provisional"
        if any(status == "final" for status in statuses):
            return "verified"
        return "verified" if any(label != "数据不足" for label in labels) else "data_insufficient"
    if run.get("status") != "done":
        return "not_ready"
    if _is_zero_pick_run(run):
        return "zero_pick"
    if str(run.get("recommendationDate") or "") >= date.today().isoformat():
        return "pending_next_trade_day"
    return "verifiable"


def _is_zero_pick_run(run: Dict[str, Any]) -> bool:
    if run.get("status") != "done":
        return False
    selection_mode = str(run.get("selectionMode") or "")
    if selection_mode in ZERO_PICK_SELECTION_MODES:
        return True
    final_picks = run.get("finalPicks") or []
    return int(run.get("finalCount") or 0) <= 0 and not final_picks


def _semantic_polluted_run(run: Dict[str, Any]) -> bool:
    picks = run.get("finalPicks") or []
    if not isinstance(picks, list) or not picks:
        return False
    for pick in picks:
        if not isinstance(pick, dict):
            continue
        legacy_sector = str(pick.get("legacySector") or pick.get("sector") or "").strip()
        sector_status = str(pick.get("sectorEvidenceStatus") or "")
        dimensions = pick.get("t1Dimensions") or {}
        old_semantics = legacy_sector in LISTING_BOARD_NAMES and not sector_status
        unavailable_semantics = sector_status in {"unavailable", "stale"} and legacy_sector in LISTING_BOARD_NAMES
        text = _ai_pick_text(pick)
        if (old_semantics or unavailable_semantics) and (_has_positive_sector_claim(text) or _has_probability_wording(text)):
            return True
        if old_semantics and float(dimensions.get("sectorStrength") or 0) >= 95:
            return True
    return False


def _requires_legacy_isolation(run: Dict[str, Any]) -> bool:
    if run.get("status") != "done":
        return False
    if _semantic_polluted_run(run):
        return True
    recommendation_date = str(run.get("recommendationDate") or "")
    if not recommendation_date or recommendation_date >= date.today().isoformat():
        return False
    market = run.get("market") or {}
    evidence_run_id = int(run.get("evidenceRunId") or market.get("evidenceRunId") or 0)
    evidence_meta = market.get("t1Evidence") or {}
    evidence_version = str(evidence_meta.get("version") or "")
    return evidence_run_id <= 0 or evidence_version != T1_EVIDENCE_VERSION


def _run_role(run: Dict[str, Any]) -> str:
    market = run.get("market") or {}
    request = run.get("request") or {}
    if _historical_treatment_applied(market, request):
        return "legacy"
    role = str(market.get("runRole") or request.get("runRole") or "").strip()
    if role in {"official", "research", "legacy", "invalid"}:
        if role == "official" and _run_requested_pool_size(run) < T1_OFFICIAL_MIN_POOL_SIZE:
            return "research"
        if role == "official" and _run_signal_window_guard(run).get("officialEligible") is False:
            return "research"
        return role
    if bool(request.get("force")):
        return "research"
    scan_time = str(run.get("scanTime") or request.get("scanTime") or "")
    if (
        OFFICIAL_SCAN_START <= scan_time <= OFFICIAL_SCAN_END
        and _run_requested_pool_size(run) >= T1_OFFICIAL_MIN_POOL_SIZE
        and _run_signal_window_guard(run).get("officialEligible") is not False
    ):
        return "official"
    return "research"


def _run_requested_pool_size(run: Dict[str, Any]) -> int:
    request = run.get("request") or {}
    value = request.get("poolSize")
    if value is None:
        value = run.get("requestedPoolSize") or run.get("poolSize")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _zero_pick_reason(run: Dict[str, Any]) -> str:
    market = run.get("market") or {}
    policy = market.get("recommendationPolicy") or {}
    context = market.get("marketContext") or {}
    return (
        str(policy.get("reason") or "").strip()
        or str(run.get("error") or "").strip()
        or (f"市场状态为{context.get('label')}，当前按风控推荐 0 只。" if context.get("label") else "")
        or "当前 run 为恐慌/数据质量/守门规则触发的空仓决策，没有最终 Top3。"
    )


def _zero_pick_verification_result(run: Dict[str, Any], strategy_feedback: Dict[str, Any]) -> Dict[str, Any]:
    reason = _zero_pick_reason(run)
    summary = {"count": 0, "verifiedCount": 0, "successCount": 0, "zeroPick": True}
    tradeable_summary = {
        "count": 0,
        "tradeableCount": 0,
        "ruleTradeableCount": 0,
        "successCount": 0,
        "zeroPick": True,
        "reason": reason,
        "validationVersion": TRADEABLE_VALIDATION_VERSION,
    }
    baseline = (run.get("market") or {}).get("baselineComparison") or {}
    if not baseline:
        baseline = {
            "portfolio": {
                "status": "ok",
                "slotCount": PORTFOLIO_SLOT_COUNT,
                "componentCount": 0,
                "tradeableCount": 0,
                "cashSlots": PORTFOLIO_SLOT_COUNT,
                "componentReturnsPct": [],
                "portfolioReturnPct": 0.0,
                "avgTradeableReturnPct": None,
                "reason": "Top0 空仓日：三个槽位全部为现金，组合收益 0%。",
            },
            "final": {
                "status": "ok",
                "count": 0,
                "tradeableCount": 0,
                "avgNetReturnPct": None,
                "portfolioReturnPct": 0.0,
                "slotCount": PORTFOLIO_SLOT_COUNT,
                "cashSlots": PORTFOLIO_SLOT_COUNT,
                "successRate": None,
                "reason": "Top0 空仓日，进入组合现金曲线，不进入个股成功率。",
            },
        }
    return {
        "runId": int(run.get("id") or 0),
        "status": "zero_pick",
        "reason": reason,
        "items": [],
        "scenarios": [],
        "summary": summary,
        "tradeableSummary": tradeable_summary,
        "baselineComparison": baseline,
        "strategyFeedback": strategy_feedback,
    }


def _default_review_run_id(runs: List[Dict[str, Any]]) -> int:
    for run in runs:
        if run.get("sampleClass") in {"learning", "usable", "partial"}:
            return int(run.get("id") or 0)
    return int((runs[0] or {}).get("id") or 0) if runs else 0


def _review_sample_meta(
    run: Dict[str, Any],
    outcome_status: str,
    run_role: str,
    tradeable_count: int,
    outcome_count: int,
    learning_eligible_count: int,
    primary_entry_not_executable_count: int = 0,
    primary_minute_missing_count: int = 0,
) -> Dict[str, str]:
    recommendation_date = str(run.get("recommendationDate") or "")
    trade_date_status = _t1_trade_date_status(recommendation_date, str(run.get("scanTime") or ""))
    if str(trade_date_status.get("status") or "").startswith("non_trading"):
        return {
            "sampleClass": "diagnostic",
            "sampleLabel": "仅排查",
            "sampleReason": f"推荐日 {recommendation_date} 不是交易日，A股不开盘。这个 run 只能用来排查系统问题，不能算正式复盘，也不能用于优化策略。",
        }
    signal_guard = _run_signal_window_guard(run)
    if (
        signal_guard.get("officialEligible") is False
        and run.get("status") == "done"
        and int(run.get("finalCount") or 0) > 0
    ):
        return {
            "sampleClass": "entry_not_executable",
            "sampleLabel": "买入不可执行",
            "sampleReason": str(signal_guard.get("reason") or "信号发布时间超过尾盘可执行窗口，不能作为真实交易样本。"),
        }
    if outcome_status == "pending_next_trade_day":
        return {"sampleClass": "waiting", "sampleLabel": "等待数据", "sampleReason": "还没到次日开盘后 15 分钟，暂时不能验证。"}
    if outcome_status == "zero_pick":
        return {"sampleClass": "zero_pick", "sampleLabel": "空仓", "sampleReason": "当天系统选择不推荐股票，只看组合里的空仓表现，不算个股成功率。"}
    if run_role == "invalid" or outcome_status == "invalid":
        return {"sampleClass": "invalid", "sampleLabel": "仅排查", "sampleReason": "价格、行情源或数据口径有异常，只能排查问题，不能拿来复盘或优化。"}
    if run_role == "legacy" or outcome_status == "legacy":
        return {"sampleClass": "legacy", "sampleLabel": "仅排查", "sampleReason": "这是旧规则产生的历史记录，只保留排查，不拿来复盘或优化。"}
    if outcome_count > 0 and primary_entry_not_executable_count >= outcome_count:
        reason = "信号发布时间超过尾盘可执行窗口，或信号发布后没有可确认成交；不能按真实 T+1 交易计算收益。"
        if signal_guard.get("officialEligible") is False:
            reason = str(signal_guard.get("reason") or reason)
        return {"sampleClass": "entry_not_executable", "sampleLabel": "买入不可执行", "sampleReason": reason}
    if outcome_status == "data_insufficient" or (outcome_count > 0 and tradeable_count <= 0):
        if primary_minute_missing_count > 0:
            return {"sampleClass": "data_insufficient", "sampleLabel": "缺少卖出价", "sampleReason": "缺少次日开盘后 15 分钟价格，不能判断这次推荐的主结果。可以尝试补拉验证分钟线。"}
        return {"sampleClass": "data_insufficient", "sampleLabel": "部分缺数据", "sampleReason": "没有拿到次日开盘后 15 分钟价格，不能判断这次推荐是否成功。"}
    if outcome_count > 0 and 0 < tradeable_count < outcome_count:
        return {
            "sampleClass": "partial",
            "sampleLabel": "部分缺数据",
            "sampleReason": f"只有 {tradeable_count}/{outcome_count} 只股票拿到次日开盘后 15 分钟价格，只能看局部结果，不能用于优化策略。",
        }
    if tradeable_count > 0 and outcome_status in {"verified", "provisional"}:
        if run_role == "official" and learning_eligible_count > 0:
            return {"sampleClass": "learning", "sampleLabel": "可用于优化", "sampleReason": "这是正式交易日推荐，次日 15 分钟价格完整，结果可以用于后续优化策略。"}
        if run_role == "official":
            return {"sampleClass": "usable", "sampleLabel": "可看结果", "sampleReason": "已经拿到次日 15 分钟结果，可以看表现；但还缺最终确认或对照数据，暂时不能用于优化。"}
        return {"sampleClass": "usable", "sampleLabel": "可看结果", "sampleReason": "这是研究/手动重跑结果，可以看表现，但不会用于优化策略。"}
    if outcome_status == "verifiable":
        return {"sampleClass": "verifiable", "sampleLabel": "等待数据", "sampleReason": "推荐日已过，但还没写入次日 15 分钟结果，可以点击刷新验证。"}
    if run.get("status") != "done":
        return {"sampleClass": "not_ready", "sampleLabel": "等待数据", "sampleReason": "这次推荐还没跑完，暂时不能看结果。"}
    return {"sampleClass": "other", "sampleLabel": "仅排查", "sampleReason": "这条记录不符合正常复盘口径，只保留排查。"}


def _review_display_meta(run: Dict[str, Any], sample: Dict[str, str], outcome_status: str, run_role: str) -> Dict[str, str]:
    sample_class = str(sample.get("sampleClass") or "")
    sample_reason = str(sample.get("sampleReason") or "")
    selection_mode = str(run.get("selectionMode") or "")
    if sample_class == "learning":
        return {
            "reviewStatus": "learnable",
            "reviewStatusLabel": "可用于优化",
            "reviewStatusReason": sample_reason or "结果完整，可以用于后续优化策略。",
            "reviewStatusTone": "good",
        }
    if sample_class == "partial":
        return {
            "reviewStatus": "partial",
            "reviewStatusLabel": "部分缺数据",
            "reviewStatusReason": sample_reason or "只有部分股票拿到次日 15 分钟结果，只能参考，不能用于优化。",
            "reviewStatusTone": "warn",
        }
    if sample_class == "entry_not_executable":
        return {
            "reviewStatus": "diagnostic",
            "reviewStatusLabel": "买入不可执行",
            "reviewStatusReason": sample_reason or "信号发布后已经无法按尾盘计划买入，这条记录不能作为真实交易样本。",
            "reviewStatusTone": "bad",
        }
    if sample_class == "data_insufficient":
        return {
            "reviewStatus": "partial",
            "reviewStatusLabel": "缺少卖出价" if sample.get("sampleLabel") == "缺少卖出价" else "部分缺数据",
            "reviewStatusReason": sample_reason or "没有拿到次日 15 分钟价格，不能判断这次推荐是否成功。",
            "reviewStatusTone": "warn",
        }
    if sample_class == "usable":
        if run_role == "official":
            reason = sample_reason or "可以看结果，但暂时不能用于优化。"
        else:
            reason = sample_reason or "研究/手动重跑结果可以参考，但不会用于优化。"
        return {
            "reviewStatus": "reviewable",
            "reviewStatusLabel": "可看结果",
            "reviewStatusReason": reason,
            "reviewStatusTone": "neutral",
        }
    if sample_class == "zero_pick":
        return {
            "reviewStatus": "reviewable",
            "reviewStatusLabel": "可看结果",
            "reviewStatusReason": sample_reason or "当天选择空仓，可以看组合表现，不算个股成功率。",
            "reviewStatusTone": "neutral",
        }
    if sample_class in {"waiting", "verifiable", "not_ready"}:
        if sample_class == "verifiable":
            reason = "推荐日已过，但还没有写入次日 open_15m 验证；点击刷新该 run 验证。"
        else:
            reason = sample_reason or "等待 run 完成或等待次日 open_15m 验证。"
        return {
            "reviewStatus": "waiting",
            "reviewStatusLabel": "等待数据",
            "reviewStatusReason": reason,
            "reviewStatusTone": "warn",
        }
    reason = sample_reason or "这条记录不适合拿来看结果或优化策略，只保留排查。"
    if selection_mode == "invalid_non_trading_day":
        reason = run.get("error") or reason
    return {
        "reviewStatus": "diagnostic",
        "reviewStatusLabel": "仅排查",
        "reviewStatusReason": reason,
        "reviewStatusTone": "bad",
    }


def _compact_review_error(error: str, limit: int = 3) -> str:
    text = str(error or "").strip()
    if not text:
        return ""
    parts = [part.strip() for part in re.split(r"[；;]\s*", text) if part.strip()]
    if not parts:
        return text[:180] + ("..." if len(text) > 180 else "")
    shown = parts[:limit]
    suffix = f"；另有 {len(parts) - limit} 条。" if len(parts) > limit else ""
    return "；".join(shown) + suffix


def _review_ai_decision_meta(run: Dict[str, Any]) -> Dict[str, str]:
    selection_mode = str(run.get("selectionMode") or "")
    error = str(run.get("error") or "").strip()
    compact_error = _compact_review_error(error)
    if selection_mode == "ai_t1_overnight":
        return {
            "status": "ai_final_passed",
            "label": "AI终选通过",
            "reason": "DeepSeek 参与并通过最终 Top 选择，后端证据校验已通过。",
            "detail": "",
            "tone": "good",
        }
    if selection_mode == "ai_semifinal_rules_final":
        reason = "DeepSeek 通过了批次初筛，但最终 Top 选择没有通过证据校验；最终排序由本地 T+1 规则完成。"
        if compact_error:
            reason = f"{reason} 主要原因：{compact_error}"
        return {
            "status": "ai_semifinal_rules_final",
            "label": "AI初筛通过，终选未通过",
            "reason": reason,
            "detail": error,
            "tone": "warn",
        }
    if selection_mode == "rules_fallback":
        reason = "DeepSeek 没有产出可采纳的最终结果，本次最终推荐来自规则兜底。"
        if compact_error:
            reason = f"{reason} 主要原因：{compact_error}"
        return {
            "status": "rules_fallback",
            "label": "规则兜底",
            "reason": reason,
            "detail": error,
            "tone": "warn",
        }
    if selection_mode == "ai_failed":
        reason = "DeepSeek 调用或输出失败，本次不能视为 AI 推荐。"
        if compact_error:
            reason = f"{reason} 主要原因：{compact_error}"
        return {
            "status": "ai_failed",
            "label": "AI失败",
            "reason": reason,
            "detail": error,
            "tone": "bad",
        }
    if selection_mode in {"market_risk_zero_pick", "data_quality_zero_pick", "guardrail_zero_pick"}:
        labels = {
            "market_risk_zero_pick": "风控空仓",
            "data_quality_zero_pick": "数据空仓",
            "guardrail_zero_pick": "守门空仓",
        }
        return {
            "status": selection_mode,
            "label": labels.get(selection_mode, "系统空仓"),
            "reason": error or "系统按市场风险、数据质量或守门规则选择 Top0。",
            "detail": error,
            "tone": "neutral",
        }
    if selection_mode == "invalid_non_trading_day":
        return {
            "status": "invalid_non_trading_day",
            "label": "非交易日",
            "reason": error or "推荐日不是 A 股交易日，这条记录只用于排查。",
            "detail": error,
            "tone": "bad",
        }
    return {
        "status": selection_mode or "unknown",
        "label": selection_mode or "未知来源",
        "reason": error or "当前记录没有明确的 AI 终选状态。",
        "detail": error,
        "tone": "neutral",
    }


def _review_learning_scope_meta(
    display: Dict[str, str],
    ai_decision: Dict[str, str],
    learning_eligible_count: int,
) -> Dict[str, str]:
    can_optimize = str(display.get("reviewStatus") or "") == "learnable" and learning_eligible_count > 0
    if not can_optimize:
        return {
            "scope": "none",
            "label": "不进入优化",
            "reason": "这条记录暂时不满足策略优化样本条件。",
        }
    ai_status = str(ai_decision.get("status") or "")
    if ai_status == "ai_final_passed":
        return {
            "scope": "full_ai_strategy",
            "label": "完整策略优化",
            "reason": "数据完整，并且 DeepSeek 终选通过，可同时用于候选池、规则和 AI 终选表现分析。",
        }
    if ai_status == "ai_semifinal_rules_final":
        return {
            "scope": "candidate_rules_only",
            "label": "规则/候选池优化",
            "reason": "数据完整，可用于分析候选池和本地终选规则；但不作为 DeepSeek 终选成功样本。",
        }
    if ai_status == "rules_fallback":
        return {
            "scope": "rules_fallback_only",
            "label": "规则兜底优化",
            "reason": "数据完整，可用于分析规则兜底表现；但不作为 DeepSeek 推荐成功样本。",
        }
    return {
        "scope": "data_only",
        "label": "数据表现优化",
        "reason": "数据完整，可用于收益和风险统计；AI 决策来源需要单独看。",
    }


def _review_usage_explanation(
    run: Dict[str, Any],
    outcomes: List[Dict[str, Any]],
    tradeable_summary: Dict[str, Any],
    sample: Dict[str, str],
    display: Dict[str, str],
    ai_decision: Dict[str, str],
    learning_scope: Dict[str, str],
    outcome_status: str,
    run_role: str,
    learning_eligible_count: int,
) -> Dict[str, Any]:
    status_label = str(display.get("reviewStatusLabel") or sample.get("sampleLabel") or "")
    can_optimize = str(display.get("reviewStatus") or "") == "learnable" and learning_eligible_count > 0
    final_count = int(run.get("finalCount") or len(outcomes) or 0)
    outcome_count = int(len(outcomes) or final_count or 0)
    tradeable_count = int(tradeable_summary.get("tradeableCount") or 0)
    reasons: List[str] = []
    next_steps: List[str] = []
    recommendation_date = str(run.get("recommendationDate") or "")
    trade_date_status = _t1_trade_date_status(recommendation_date, str(run.get("scanTime") or ""))
    selection_mode = str(run.get("selectionMode") or "")

    if can_optimize:
        reasons.extend(
            [
                "这是正常交易日尾盘产生的正式推荐。",
                "最终推荐股都有次日开盘后 15 分钟结果。",
                str(learning_scope.get("reason") or "这条记录可以进入后续策略优化。"),
                str(ai_decision.get("reason") or ""),
            ]
        )
        next_steps.append("无需补数据，后续策略统计会自动使用这条记录。")
    else:
        if str(trade_date_status.get("status") or "").startswith("non_trading"):
            reasons.append(f"推荐日 {recommendation_date} 不是交易日，A股不开盘。")
        if run_role == "research":
            if bool((run.get("request") or {}).get("force")):
                reasons.append("这是手动强制重跑结果，不是当天正式推荐。")
            else:
                reasons.append("这是研究记录，不是当天正式推荐。")
        elif run_role == "legacy":
            reasons.append("这是旧规则产生的历史记录，只保留排查。")
        elif run_role == "invalid":
            reasons.append("这条记录本身被标记为异常，不能用于优化。")
        if selection_mode == "rules_fallback":
            reasons.append("最终结果来自规则兜底，不是 DeepSeek 有效终选。")
        elif selection_mode == "ai_semifinal_rules_final":
            reasons.append("DeepSeek 只参与了初筛，最终排序仍由本地规则完成。")
        elif selection_mode == "ai_failed":
            reasons.append("DeepSeek 没有产出可用结果。")
        signal_guard = _run_signal_window_guard(run)
        if signal_guard.get("officialEligible") is False:
            reasons.append(str(signal_guard.get("reason") or "信号发布时间超过 14:50，不能作为真实可交易样本。"))
        primary_entry_blocked = int(tradeable_summary.get("primaryEntryNotExecutableCount") or 0)
        primary_minute_missing = int(tradeable_summary.get("primaryMinuteMissingCount") or 0)
        if str(sample.get("sampleClass") or "") == "entry_not_executable":
            pass
        elif outcome_count > 0 and primary_entry_blocked >= outcome_count:
            reasons.append("最终推荐股在真实交易口径下买入不可执行，所以不计算 open_15m 交易收益。")
        elif outcome_count > 0 and tradeable_count <= 0 and primary_minute_missing > 0:
            reasons.append("缺少最终推荐股的次日开盘后 15 分钟卖出价。")
        elif outcome_count > 0 and tradeable_count <= 0:
            reasons.append("没有拿到最终推荐股的次日开盘后 15 分钟价格。")
        elif outcome_count > 0 and tradeable_count < outcome_count:
            reasons.append(f"只有 {tradeable_count}/{outcome_count} 只股票拿到次日开盘后 15 分钟价格。")
        if outcome_status == "provisional":
            reasons.append("当前验证还是临时结果，需要等最终确认。")
        for reason in _review_outcome_exclusion_reasons(outcomes):
            reasons.append(reason)
        if not reasons:
            reasons.append(str(display.get("reviewStatusReason") or sample.get("sampleReason") or "暂时不满足用于优化的条件。"))
        if str(display.get("reviewStatus") or "") == "partial":
            next_steps.append("可以点击“补拉验证分钟线”；如果仍补不到，就只能作为缺数据记录。")
        elif str(sample.get("sampleClass") or "") == "entry_not_executable":
            next_steps.append("这类记录不能靠补拉修复；需要在 14:30-14:50 内完成推荐发布，明天重新跑正式样本。")
        elif str(display.get("reviewStatus") or "") == "waiting":
            next_steps.append("可以点击“刷新该 run 验证”，或等次日 15 分钟数据回来。")
        elif str(display.get("reviewStatus") or "") == "reviewable":
            next_steps.append("可以看这次表现，但不要把它当成策略优化样本。")
        else:
            next_steps.append("无需反复刷新，这条记录主要用于排查系统问题。")

    reasons = _unique_strings([reason for reason in reasons if reason])
    next_steps = _unique_strings([step for step in next_steps if step])
    return {
        "title": "为什么不能用于优化？" if not can_optimize else "为什么可以用于优化？",
        "summary": f"当前用途：{status_label or '--'}。" + ("可以进入后续策略优化。" if can_optimize else "不会进入后续策略优化。"),
        "canOptimize": can_optimize,
        "reasons": reasons,
        "nextSteps": next_steps,
    }


def _review_outcome_exclusion_reasons(outcomes: List[Dict[str, Any]]) -> List[str]:
    translated: List[str] = []
    raw_reasons: List[str] = []
    primary_statuses: List[str] = []
    for outcome in outcomes or []:
        payload = _outcome_payload(outcome)
        raw_reasons.extend([str(reason) for reason in payload.get("exclusionReasons") or [] if str(reason)])
        primary = ((payload.get("tradeable") or {}).get("primaryScenario") or {})
        primary_statuses.append(str(primary.get("dataStatus") or ""))
    text = "；".join(raw_reasons)
    all_entry_not_executable = bool(primary_statuses and all(status == "entry_not_executable" for status in primary_statuses))
    if all_entry_not_executable:
        translated.append("信号发布后无法确认买入成交，不能计算真实交易收益。")
    if not all_entry_not_executable and ("固定 15m 主 KPI 不可用" in text or "open_15m" in text):
        translated.append("缺少次日开盘后 15 分钟价格，不能判断主结果。")
    if "验证源为缓存" in text or "缓存" in text:
        translated.append("验证用了缓存数据，还不够严格。")
    if "同口径基准不可用" in text:
        translated.append("缺少同口径市场基准，不能判断是否跑赢市场。")
    if "验证状态为 provisional" in text:
        translated.append("验证还不是最终状态。")
    if "推荐日买入价未完成校验" in text or "推荐日价格校验证据不足" in text:
        translated.append("买入价来自推荐时冻结价，收益可以看；但推荐日价格校验证据不足，暂不用于优化。")
    if "价格口径" in text or "价格异常" in text:
        translated.append("价格口径有异常，不能用于优化。")
    if "历史旧口径" in text:
        translated.append("这是旧口径历史记录，只保留排查。")
    return _unique_strings(translated)


def _scenario_definition_label(key: str) -> str:
    for definition in TRADEABLE_SCENARIO_DEFINITIONS:
        if definition.get("key") == key:
            return str(definition.get("label") or key)
    return key


def _exit_timing_stats(outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for definition in TRADEABLE_SCENARIO_DEFINITIONS:
        key = str(definition.get("key") or "")
        values: List[float] = []
        missing = 0
        benchmark_ready = 0
        benchmark_win = 0
        for item in outcomes or []:
            scenario = _outcome_scenario_by_key(item, key)
            net = _safe_number(scenario.get("netReturnPct"))
            if scenario.get("dataStatus") != "ok" or net is None:
                missing += 1
                continue
            values.append(float(net))
            excess = _safe_number(item.get("excessReturnPct"))
            if excess is not None:
                benchmark_ready += 1
                if excess > 0:
                    benchmark_win += 1
        rows.append(
            {
                "scenarioKey": key,
                "scenarioLabel": str(definition.get("label") or key),
                "sellTime": str(definition.get("time") or ""),
                "sampleCount": len(values),
                "missingCount": missing,
                "avgNetReturnPct": _avg(values),
                "medianNetReturnPct": round(float(median(values)), 2) if values else None,
                "successRate": _rate([value > 0 for value in values]) if values else None,
                "strongSuccessRate": _rate([value >= 2 for value in values]) if values else None,
                "benchmarkWinRate": round(benchmark_win * 100 / benchmark_ready, 1) if benchmark_ready else None,
                "netReturnsPct": [round(value, 2) for value in values],
                "isPrimary": key == PRIMARY_TRADEABLE_SCENARIO_KEY,
            }
        )
    return rows


def _outcome_scenario_by_key(item: Dict[str, Any], key: str) -> Dict[str, Any]:
    tradeable = (_outcome_payload(item).get("tradeable") or {})
    scenarios = tradeable.get("scenarios") or []
    for scenario in scenarios:
        if scenario.get("scenarioKey") == key:
            return scenario
    primary = tradeable.get("primaryScenario") or {}
    if primary.get("scenarioKey") == key:
        return primary
    return {}


def _optimization_review_stats(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    learning = [run for run in runs if run.get("reviewStatus") == "learnable"]
    timing_rows: List[Dict[str, Any]] = []
    for definition in TRADEABLE_SCENARIO_DEFINITIONS:
        key = str(definition.get("key") or "")
        values: List[float] = []
        missing = 0
        benchmark_values: List[float] = []
        for run in learning:
            timing = {item.get("scenarioKey"): item for item in run.get("exitTimingStats") or []}
            row = timing.get(key) or {}
            missing += int(row.get("missingCount") or 0)
            row_values = [_safe_number(value) for value in row.get("netReturnsPct") or []]
            values.extend([float(value) for value in row_values if value is not None])
            win_rate = _safe_number(row.get("benchmarkWinRate"))
            if win_rate is not None:
                benchmark_values.append(float(win_rate))
        timing_rows.append(
            {
                "scenarioKey": key,
                "scenarioLabel": str(definition.get("label") or key),
                "sellTime": str(definition.get("time") or ""),
                "sampleCount": len(values),
                "missingCount": missing,
                "avgNetReturnPct": _avg(values),
                "medianNetReturnPct": round(float(median(values)), 2) if values else None,
                "successRate": _rate([value > 0 for value in values]) if values else None,
                "strongSuccessRate": _rate([value >= 2 for value in values]) if values else None,
                "benchmarkWinRate": _avg(benchmark_values),
                "netReturnsPct": [round(value, 2) for value in values],
                "isPrimary": key == PRIMARY_TRADEABLE_SCENARIO_KEY,
            }
        )
    primary = next((item for item in timing_rows if item.get("scenarioKey") == PRIMARY_TRADEABLE_SCENARIO_KEY), {})
    return {
        "title": "可用于优化样本统计",
        "scope": "learnable",
        "scopeLabel": "可用于优化",
        "runCount": len(learning),
        "outcomeCount": sum(int(run.get("learningEligibleCount") or 0) for run in learning),
        "primaryScenarioKey": PRIMARY_TRADEABLE_SCENARIO_KEY,
        "primaryScenarioLabel": _scenario_definition_label(PRIMARY_TRADEABLE_SCENARIO_KEY),
        "primary": _public_timing_stat(primary),
        "exitTimingStats": [_public_timing_stat(row) for row in timing_rows],
        "note": "顶部统计只使用“可用于优化”的样本；下方筛选条件只影响历史 run 列表，不改变这组统计。",
        "metricNotes": {
            "avgNetReturnPct": "平均净收益：按推荐冻结价买入、对应时点卖出的平均结果，已扣除页面配置的滑点/交易成本。",
            "medianNetReturnPct": "中位数净收益：把收益从低到高排队后取中间值，能减少单个极端样本的影响。",
            "successRate": "成功率：该卖出时点净收益大于 0 的比例。",
            "strongSuccessRate": "强成功率：该卖出时点净收益达到 2% 及以上的比例。",
            "benchmarkWinRate": "跑赢基准比例：有同口径基准时，收益高于市场基准的比例。",
            "missingCount": "缺数据数：该时点没有拿到可用价格的样本数。",
        },
    }


def _public_timing_stat(row: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(row or {})
    result.pop("netReturnsPct", None)
    return result


def _public_review_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(run or {})
    result["exitTimingStats"] = [_public_timing_stat(row) for row in result.get("exitTimingStats") or []]
    return result


def _review_stats(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    learning = [run for run in runs if run.get("reviewStatus") == "learnable"]
    reviewable = [run for run in runs if run.get("reviewStatus") == "reviewable"]
    partial = [run for run in runs if run.get("reviewStatus") == "partial"]
    waiting = [run for run in runs if run.get("reviewStatus") == "waiting"]
    diagnostic = [run for run in runs if run.get("reviewStatus") in {"diagnostic", "unusable"}]
    tradeable_count = sum(int(run.get("tradeableCount") or 0) for run in learning)
    success_count = sum(int(run.get("successCount") or 0) for run in learning)
    strong_success_count = sum(int(run.get("strongSuccessCount") or 0) for run in learning)
    learning_eligible_count = sum(int(run.get("learningEligibleCount") or 0) for run in learning)
    weighted_return_sum = 0.0
    for run in learning:
        avg_net = _safe_number(run.get("avgPrimaryNetReturnPct"))
        count = int(run.get("tradeableCount") or 0)
        if avg_net is not None and count > 0:
            weighted_return_sum += avg_net * count
    return {
        "loadedRuns": len(runs),
        "displayReviewableRuns": len(reviewable),
        "displayLearnableRuns": len(learning),
        "displayPartialRuns": len(partial),
        "displayWaitingRuns": len(waiting),
        "displayDiagnosticRuns": len(diagnostic),
        "displayUnusableRuns": len(diagnostic),
        "usableRuns": len(learning),
        "officialUsableRuns": len([run for run in learning if run.get("runRole") == "official"]),
        "researchUsableRuns": len([run for run in learning if run.get("runRole") == "research"]),
        "learningRuns": len(learning),
        "tradeableOutcomes": tradeable_count,
        "learningEligibleOutcomes": learning_eligible_count,
        "avgNetReturnPct": round(weighted_return_sum / tradeable_count, 2) if tradeable_count else None,
        "successRate": round(success_count * 100 / tradeable_count, 1) if tradeable_count else None,
        "strongSuccessRate": round(strong_success_count * 100 / tradeable_count, 1) if tradeable_count else None,
        "successCount": success_count,
        "strongSuccessCount": strong_success_count,
        "problemRuns": len(partial) + len(diagnostic),
        "dataInsufficientRuns": len([run for run in runs if run.get("sampleClass") == "data_insufficient"]),
        "legacyRuns": len([run for run in runs if run.get("sampleClass") == "legacy"]),
        "invalidRuns": len([run for run in runs if run.get("sampleClass") == "invalid"]),
        "diagnosticRuns": len(diagnostic),
        "partialRuns": len(partial),
        "waitingRuns": len([run for run in runs if run.get("sampleClass") == "waiting"]),
        "zeroPickRuns": len([run for run in runs if run.get("sampleClass") == "zero_pick"]),
        "note": "页面按能不能使用来分：可用于优化、可看结果、部分缺数据、等待数据、仅排查。周末或异常 run 即使查到部分价格，也只进入仅排查。",
    }


def _review_pick_names(picks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for pick in picks:
        if not isinstance(pick, dict):
            continue
        code = str(pick.get("code") or "").zfill(6)
        if not code.strip("0"):
            code = str(pick.get("code") or "")
        rows.append(
            {
                "code": code,
                "name": str(pick.get("name") or ""),
                "aiRank": pick.get("aiRank") or pick.get("rank"),
                "poolRank": pick.get("poolRank"),
            }
        )
    return rows


def _review_outcomes(outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for item in outcomes:
        payload = item.get("outcome") or {}
        tradeable = payload.get("tradeable") or {}
        rows.append(
            {
                "runId": int(item.get("runId") or 0),
                "code": item.get("code") or "",
                "name": item.get("name") or "",
                "recommendationDate": item.get("recommendationDate") or "",
                "nextTradeDate": item.get("nextTradeDate") or "",
                "buyReferencePrice": item.get("buyReferencePrice"),
                "nextOpen": item.get("nextOpen"),
                "nextHigh": item.get("nextHigh"),
                "nextLow": item.get("nextLow"),
                "nextClose": item.get("nextClose"),
                "openReturnPct": item.get("openReturnPct"),
                "highReturnPct": item.get("highReturnPct"),
                "closeReturnPct": item.get("closeReturnPct"),
                "maxDrawdownPct": item.get("maxDrawdownPct"),
                "benchmarkReturnPct": item.get("benchmarkReturnPct"),
                "excessReturnPct": item.get("excessReturnPct"),
                "outcomeLabel": item.get("outcomeLabel") or "",
                "outcomeScore": item.get("outcomeScore"),
                "failureReason": item.get("failureReason") or "",
                "verifiedAt": item.get("verifiedAt") or "",
                "dataSource": item.get("dataSource") or "",
                "outcome": {
                    "validationStatus": payload.get("validationStatus") or "",
                    "learningEligible": bool(payload.get("learningEligible")),
                    "exclusionReasons": payload.get("exclusionReasons") or [],
                    "failureTags": payload.get("failureTags") or [],
                    "priceConsistency": _review_price_consistency(payload.get("priceConsistency") or {}),
                    "riskProfile": payload.get("riskProfile") or {},
                    "tradeable": {
                        "primaryScenario": _review_scenario(tradeable.get("primaryScenario") or {}),
                        "scenarios": [_review_scenario(scenario) for scenario in tradeable.get("scenarios") or []],
                        "ruleExecution": _review_scenario(tradeable.get("ruleExecution") or {}),
                        "quality": tradeable.get("quality") or {},
                    },
                },
            }
        )
    return rows


def _review_price_consistency(price_consistency: Dict[str, Any]) -> Dict[str, Any]:
    keys = {
        "ok",
        "verified",
        "status",
        "referenceSource",
        "referenceDate",
        "referenceHigh",
        "referenceLow",
        "reason",
    }
    return {key: price_consistency.get(key) for key in keys if key in price_consistency}


def _review_scenario(scenario: Dict[str, Any]) -> Dict[str, Any]:
    keys = {
        "scenarioKey",
        "scenarioLabel",
        "sellTime",
        "sellPrice",
        "grossReturnPct",
        "netReturnPct",
        "dataStatus",
        "validationStatus",
        "status",
        "reason",
    }
    return {key: scenario.get(key) for key in keys if key in scenario}


def _review_baseline_comparison(baseline: Dict[str, Any]) -> Dict[str, Any]:
    compact: Dict[str, Any] = {}
    for key in ("portfolio", "final", "index"):
        source = baseline.get(key) or {}
        compact[key] = {
            field: source.get(field)
            for field in (
                "status",
                "count",
                "tradeableCount",
                "avgNetReturnPct",
                "avgBenchmarkReturnPct",
                "portfolioReturnPct",
                "successRate",
                "slotCount",
                "cashSlots",
                "reason",
            )
            if field in source
        }
    critic_pair = baseline.get("criticPair") or {}
    compact["criticPair"] = {
        field: critic_pair.get(field)
        for field in ("status", "criticApplied", "changedPicks", "replacementCount", "droppedCount", "prePortfolioReturnPct", "postPortfolioReturnPct", "deltaPct", "reason")
        if field in critic_pair
    }
    return compact


def _outcome_summary(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [item for item in outcomes if item.get("outcomeLabel") != "数据不足"]
    if not outcomes:
        return {"count": 0}
    return {
        "count": len(outcomes),
        "verifiedCount": len(valid),
        "successCount": len([item for item in valid if item.get("outcomeLabel") in {"强成功", "成功溢价", "弱成功"}]),
        "avgHighReturnPct": _avg([item.get("highReturnPct") for item in valid]),
        "avgCloseReturnPct": _avg([item.get("closeReturnPct") for item in valid]),
        "avgMaxDrawdownPct": _avg([item.get("maxDrawdownPct") for item in valid]),
    }


def _tradeable_summary(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not outcomes:
        return {"count": 0}
    tradeable_rows = []
    rule_rows = []
    minute_missing_count = 0
    minute_missing_scenario_count = 0
    primary_minute_missing_count = 0
    primary_entry_not_executable_count = 0
    for item in outcomes:
        tradeable = (item.get("outcome") or {}).get("tradeable") or {}
        primary = tradeable.get("primaryScenario") or {}
        primary_status = str(primary.get("dataStatus") or "")
        if primary_status == "minute_missing":
            primary_minute_missing_count += 1
        if primary_status == "entry_not_executable":
            primary_entry_not_executable_count += 1
        net = primary.get("netReturnPct")
        missing_minute_scenarios = [
            scenario
            for scenario in tradeable.get("scenarios") or []
            if scenario.get("scenarioKey") in MINUTE_SCENARIO_KEYS
            and scenario.get("dataStatus") not in {"ok", "entry_not_executable"}
        ]
        if missing_minute_scenarios:
            minute_missing_count += 1
            minute_missing_scenario_count += len(missing_minute_scenarios)
        rule = tradeable.get("ruleExecution") or {}
        rule_net = rule.get("netReturnPct")
        if rule_net is not None:
            rule_rows.append(rule_net)
        if net is None:
            continue
        tradeable_rows.append({"netReturnPct": net, "label": item.get("outcomeLabel"), "primary": primary})
    return {
        "count": len(outcomes),
        "tradeableCount": len(tradeable_rows),
        "minuteMissingCount": minute_missing_count,
        "minuteMissingScenarioCount": minute_missing_scenario_count,
        "primaryMinuteMissingCount": primary_minute_missing_count,
        "primaryEntryNotExecutableCount": primary_entry_not_executable_count,
        "avgNetReturnPct": _avg([item.get("netReturnPct") for item in tradeable_rows]),
        "ruleTradeableCount": len(rule_rows),
        "avgRuleNetReturnPct": _avg(rule_rows),
        "successCount": len([item for item in tradeable_rows if item.get("label") in {"强成功", "成功溢价", "弱成功"}]),
        "primaryScenario": "固定 open_15m；缺失时主 KPI unavailable，不 fallback。",
        "primaryScenarioKey": PRIMARY_TRADEABLE_SCENARIO_KEY,
        "primaryFallbackAllowed": False,
        "validationVersion": TRADEABLE_VALIDATION_VERSION,
    }


def _baseline_comparison(outcomes: List[Dict[str, Any]], slot_count: int = PORTFOLIO_SLOT_COUNT) -> Dict[str, Any]:
    returns = [_primary_net_return(item) for item in outcomes]
    returns = [value for value in returns if value is not None]
    recommendation_quality = [_outcome_payload(item).get("recommendationQualityEligible") for item in outcomes]
    recommendation_quality_count = len([item for item in recommendation_quality if item is True])
    invalid_recommendations = [
        item for item in outcomes
        if _outcome_payload(item).get("recommendationQualityEligible") is True
        and _outcome_payload(item).get("tradeReturnEligible") is not True
    ]
    labels = [
        _outcome_payload(item).get("outcomeLabel") or item.get("outcomeLabel")
        for item in outcomes
        if (_outcome_payload(item).get("outcomeLabel") or item.get("outcomeLabel")) != "数据不足"
    ]
    portfolio = _portfolio_comparison(outcomes, slot_count=slot_count)
    return {
        "count": len(outcomes),
        "tradeableCount": len(returns),
        "recommendationQualityCount": recommendation_quality_count,
        "invalidRecommendationCount": len(invalid_recommendations),
        "invalidRecommendationRate": _rate([_outcome_payload(item).get("tradeReturnEligible") is not True for item in outcomes if _outcome_payload(item).get("recommendationQualityEligible") is True]),
        "avgNetReturnPct": _avg(returns),
        "portfolioReturnPct": portfolio.get("portfolioReturnPct"),
        "slotCount": portfolio.get("slotCount"),
        "cashSlots": portfolio.get("cashSlots"),
        "successRate": _rate([label in {"强成功", "成功溢价", "弱成功"} for label in labels]),
        "labels": labels,
        "status": "ok" if returns else "data_insufficient",
        "portfolio": portfolio,
    }


def _shadow_outcome_comparison(
    final_outcomes: List[Dict[str, Any]],
    shadow_outcomes: List[Dict[str, Any]],
    shadow_picks: List[Dict[str, Any]],
    deepseek_tail_outcomes: Optional[List[Dict[str, Any]]] = None,
    deepseek_tail_picks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    deepseek_tail_outcomes = deepseek_tail_outcomes or []
    deepseek_tail_picks = deepseek_tail_picks or []
    if not shadow_outcomes and not deepseek_tail_outcomes:
        return {}
    final_codes = _unique_codes([str(item.get("code") or "") for item in final_outcomes])
    shadow_codes = _unique_codes([str(item.get("code") or "") for item in shadow_outcomes])
    final_portfolio = _baseline_comparison(final_outcomes, slot_count=PORTFOLIO_SLOT_COUNT)
    shadow_portfolio = _baseline_comparison(shadow_outcomes, slot_count=PORTFOLIO_SLOT_COUNT)
    final_return = _safe_number(final_portfolio.get("portfolioReturnPct"))
    shadow_return = _safe_number(shadow_portfolio.get("portfolioReturnPct"))
    rows = []
    shadow_pick_by_code = {
        str(item.get("code") or "").zfill(6): item
        for item in shadow_picks
        if str(item.get("code") or "").strip()
    }
    for outcome in sorted(
        shadow_outcomes,
        key=lambda item: int(
            (shadow_pick_by_code.get(str(item.get("code") or "").zfill(6), {}) or {}).get("shadowRank")
            or item.get("aiRank")
            or item.get("rank")
            or 999
        ),
    ):
        code = str(outcome.get("code") or "").zfill(6)
        pick = shadow_pick_by_code.get(code) or {}
        tradeable = outcome.get("tradeable") or {}
        scenarios = list(tradeable.get("scenarios") or [])
        primary = _primary_tradeable_scenario(scenarios)
        rows.append(
            {
                "code": code,
                "name": outcome.get("name") or pick.get("name") or "",
                "shadowRank": int(pick.get("shadowRank") or outcome.get("aiRank") or outcome.get("rank") or 0),
                "inFormalTop3": code in final_codes,
                "selectionRole": "重合" if code in final_codes else "影子替代",
                "primaryNetReturnPct": primary.get("netReturnPct"),
                "openReturnPct": (_scenario_by_key(scenarios, "open") or {}).get("netReturnPct"),
                "open5mReturnPct": (_scenario_by_key(scenarios, "open_5m") or {}).get("netReturnPct"),
                "open15mReturnPct": (_scenario_by_key(scenarios, PRIMARY_TRADEABLE_SCENARIO_KEY) or primary or {}).get("netReturnPct"),
                "open30mReturnPct": (_scenario_by_key(scenarios, "open_30m") or {}).get("netReturnPct"),
                "outcomeLabel": outcome.get("outcomeLabel") or "",
                "failureReason": outcome.get("failureReason") or "",
                "dataStatus": primary.get("dataStatus") or "",
                "validationStatus": outcome.get("validationStatus") or primary.get("validationStatus") or "",
            }
        )
    deepseek_tail_portfolio = _baseline_comparison(deepseek_tail_outcomes, slot_count=PORTFOLIO_SLOT_COUNT) if deepseek_tail_outcomes else {}
    deepseek_tail_return = _safe_number(deepseek_tail_portfolio.get("portfolioReturnPct"))
    deepseek_tail_codes = _unique_codes([str(item.get("code") or "") for item in deepseek_tail_outcomes])
    deepseek_tail_rows = _shadow_outcome_rows(
        shadow_outcomes=deepseek_tail_outcomes,
        shadow_picks=deepseek_tail_picks,
        final_codes=final_codes,
        role_label="AI尾盘影子",
    )
    return {
        "version": "v1_shadow_open15m_comparison",
        "stage": "shadow",
        "affectsOfficialTop3": False,
        "primaryScenarioKey": PRIMARY_TRADEABLE_SCENARIO_KEY,
        "finalCodes": final_codes,
        "shadowCodes": shadow_codes,
        "deepSeekTailCodes": deepseek_tail_codes,
        "overlapCodes": [code for code in final_codes if code in shadow_codes],
        "deepSeekTailOverlapCodes": [code for code in final_codes if code in deepseek_tail_codes],
        "changedCount": max(len(final_codes), len(shadow_codes)) - len([code for code in final_codes if code in shadow_codes]),
        "deepSeekTailChangedCount": max(len(final_codes), len(deepseek_tail_codes)) - len([code for code in final_codes if code in deepseek_tail_codes]),
        "final": final_portfolio,
        "shadow": shadow_portfolio,
        "deepSeekTail": deepseek_tail_portfolio,
        "deltaPortfolioReturnPct": round(shadow_return - final_return, 2) if final_return is not None and shadow_return is not None else None,
        "deepSeekTailDeltaPortfolioReturnPct": round(deepseek_tail_return - final_return, 2) if final_return is not None and deepseek_tail_return is not None else None,
        "rows": rows,
        "deepSeekTailRows": deepseek_tail_rows,
        "reason": "影子 Top3 只做观察验证，不进入正式复盘学习；用于比较尾盘承接/低开风险影子指标是否有增量。",
    }


def _shadow_outcome_rows(
    shadow_outcomes: List[Dict[str, Any]],
    shadow_picks: List[Dict[str, Any]],
    final_codes: List[str],
    role_label: str,
) -> List[Dict[str, Any]]:
    rows = []
    shadow_pick_by_code = {
        str(item.get("code") or "").zfill(6): item
        for item in shadow_picks
        if str(item.get("code") or "").strip()
    }
    for outcome in sorted(
        shadow_outcomes or [],
        key=lambda item: int(
            (shadow_pick_by_code.get(str(item.get("code") or "").zfill(6), {}) or {}).get("shadowRank")
            or item.get("aiRank")
            or item.get("rank")
            or 999
        ),
    ):
        code = str(outcome.get("code") or "").zfill(6)
        pick = shadow_pick_by_code.get(code) or {}
        tradeable = outcome.get("tradeable") or {}
        scenarios = list(tradeable.get("scenarios") or [])
        primary = _primary_tradeable_scenario(scenarios)
        rows.append(
            {
                "code": code,
                "name": outcome.get("name") or pick.get("name") or "",
                "shadowRank": int(pick.get("shadowRank") or outcome.get("aiRank") or outcome.get("rank") or 0),
                "inFormalTop3": code in final_codes,
                "selectionRole": "重合" if code in final_codes else role_label,
                "primaryNetReturnPct": primary.get("netReturnPct"),
                "openReturnPct": (_scenario_by_key(scenarios, "open") or {}).get("netReturnPct"),
                "open5mReturnPct": (_scenario_by_key(scenarios, "open_5m") or {}).get("netReturnPct"),
                "open15mReturnPct": (_scenario_by_key(scenarios, PRIMARY_TRADEABLE_SCENARIO_KEY) or primary or {}).get("netReturnPct"),
                "open30mReturnPct": (_scenario_by_key(scenarios, "open_30m") or {}).get("netReturnPct"),
                "outcomeLabel": outcome.get("outcomeLabel") or "",
                "failureReason": outcome.get("failureReason") or "",
                "dataStatus": primary.get("dataStatus") or "",
                "validationStatus": outcome.get("validationStatus") or primary.get("validationStatus") or "",
            }
        )
    return rows


def _scenario_by_key(scenarios: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    for item in scenarios or []:
        if item.get("scenarioKey") == key:
            return item
    return {}


def _portfolio_comparison(outcomes: List[Dict[str, Any]], slot_count: int = PORTFOLIO_SLOT_COUNT) -> Dict[str, Any]:
    slot_count = max(1, int(slot_count or PORTFOLIO_SLOT_COUNT))
    ordered = sorted(
        list(outcomes or []),
        key=lambda item: int(item.get("aiRank") or (item.get("outcome") or {}).get("aiRank") or 999),
    )
    component_returns = [_primary_net_return(item) for item in ordered[:slot_count]]
    tradeable_returns = [value for value in component_returns if value is not None]
    cash_slots = max(0, slot_count - len(tradeable_returns))
    locked_items = [
        _outcome_payload(item).get("tradeable", {}).get("entryExecution", {})
        for item in ordered[:slot_count]
        if _outcome_payload(item).get("tradeReturnEligible") is not True
        and _outcome_payload(item).get("recommendationQualityEligible") is True
    ]
    locked_capital_pct = round(len(locked_items) / slot_count * 100, 2) if slot_count else 0.0
    portfolio_return = round(sum(tradeable_returns) / slot_count, 2) if slot_count else None
    return {
        "slotCount": slot_count,
        "componentCount": len(component_returns),
        "tradeableCount": len(tradeable_returns),
        "cashSlots": cash_slots,
        "lockedCapitalPct": locked_capital_pct,
        "componentReturnsPct": component_returns,
        "portfolioReturnPct": portfolio_return,
        "avgTradeableReturnPct": _avg(tradeable_returns),
        "status": "ok" if tradeable_returns or cash_slots == slot_count else "data_insufficient",
    }


def _random_portfolio_baseline_comparison(
    outcomes: List[Dict[str, Any]],
    seed: str,
    slot_count: int = PORTFOLIO_SLOT_COUNT,
    iterations: int = RANDOM_BASELINE_ITERATIONS,
) -> Dict[str, Any]:
    eligible = [item for item in outcomes if _primary_net_return(item) is not None]
    if not eligible:
        return {
            "count": len(outcomes),
            "tradeableCount": 0,
            "avgNetReturnPct": None,
            "portfolioReturnPct": None,
            "successRate": None,
            "status": "data_insufficient",
            "reason": "随机基准缺少固定 open_15m 可交易样本。",
        }
    slot_count = max(1, int(slot_count or PORTFOLIO_SLOT_COUNT))
    iterations = max(1, int(iterations or RANDOM_BASELINE_ITERATIONS))
    portfolio_returns = []
    for index in range(iterations):
        ranked = sorted(
            eligible,
            key=lambda item: _stable_random_key(str(item.get("code") or ""), f"{seed}:{index}"),
        )
        portfolio = _portfolio_comparison(ranked[:slot_count], slot_count=slot_count)
        if portfolio.get("portfolioReturnPct") is not None:
            portfolio_returns.append(float(portfolio["portfolioReturnPct"]))
    return {
        "count": len(outcomes),
        "tradeableCount": len(eligible),
        "iterations": iterations,
        "slotCount": slot_count,
        "avgNetReturnPct": _avg([_primary_net_return(item) for item in eligible]),
        "portfolioReturnPct": _avg(portfolio_returns),
        "successRate": _rate([value > 0 for value in portfolio_returns]),
        "minPortfolioReturnPct": min(portfolio_returns) if portfolio_returns else None,
        "maxPortfolioReturnPct": max(portfolio_returns) if portfolio_returns else None,
        "status": "ok" if portfolio_returns else "data_insufficient",
        "reason": f"随机基准：从全池样本稳定随机抽 Top{slot_count}，重复 {iterations} 次取组合均值。",
    }


def _critic_pair_comparison(pre: Dict[str, Any], post: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    critic = market.get("critic") or {}
    pre_return = _safe_number((pre.get("portfolio") or {}).get("portfolioReturnPct") if isinstance(pre.get("portfolio"), dict) else pre.get("portfolioReturnPct"))
    post_return = _safe_number((post.get("portfolio") or {}).get("portfolioReturnPct") if isinstance(post.get("portfolio"), dict) else post.get("portfolioReturnPct"))
    changed = bool((critic.get("replacements") or []) or (critic.get("droppedCodes") or []))
    if pre_return is None or post_return is None:
        status = "data_insufficient"
        delta = None
        reason = "Critic 前后组合缺少固定 open_15m 可交易收益，暂不能判断是否提升。"
    else:
        delta = round(post_return - pre_return, 2)
        status = "ok"
        if changed:
            reason = f"Critic 改变了最终结果，组合收益差 {delta:.2f}%。"
        else:
            reason = "Critic 已审查但未改变最终结果，前后组合收益一致。"
    return {
        "status": status,
        "criticApplied": bool(critic.get("applied")),
        "changedPicks": changed,
        "replacementCount": len(critic.get("replacements") or []),
        "droppedCount": len(critic.get("droppedCodes") or []),
        "prePortfolioReturnPct": pre_return,
        "postPortfolioReturnPct": post_return,
        "deltaPct": delta,
        "reason": reason,
    }


def _index_benchmark_comparison(
    run: Dict[str, Any],
    series_by_code: Dict[str, List[Dict[str, Any]]],
    minute_by_code: Dict[str, List[Dict[str, Any]]],
    minute_provider: Any,
    minute_source: str,
    daily_source: str,
    execution_profile: Dict[str, Any],
    verified_at: str = "",
) -> Dict[str, Any]:
    recommendation_date = str(run.get("recommendationDate") or "")
    rows = []
    minute_derived_count = 0
    for benchmark in INDEX_BENCHMARKS:
        code = benchmark["code"]
        series = series_by_code.get(code) or []
        previous = _previous_trade_point(series, recommendation_date)
        next_point = _next_trade_point(series, recommendation_date)
        if not next_point:
            next_point = _minute_derived_next_trade_point(minute_by_code.get(code, []), recommendation_date, verified_at)
            if next_point:
                minute_derived_count += 1
        buy_reference = _safe_number((previous or {}).get("close"))
        if not previous or not next_point or not buy_reference:
            continue
        scenarios = _build_tradeable_scenarios(
            code=code,
            buy_reference=buy_reference,
            next_point=next_point,
            minute_series=minute_by_code.get(code, []),
            minute_source=_minute_source_for_pick(minute_provider, {"code": code}, minute_source),
            execution_profile=execution_profile,
            verified_at=verified_at,
        )
        primary = _primary_tradeable_scenario(scenarios)
        open_return = _pct_return(_safe_number(next_point.get("open")) or 0, buy_reference)
        close_return = _pct_return(_safe_number(next_point.get("close")) or 0, buy_reference)
        rows.append(
            {
                "code": code,
                "name": benchmark["name"],
                "buyReferencePrice": buy_reference,
                "nextTradeDate": next_point.get("date", ""),
                "openReturnPct": open_return,
                "closeReturnPct": close_return,
                "netReturnPct": primary.get("netReturnPct"),
                "primaryScenario": primary,
                "scenarios": scenarios,
            }
        )
    returns = [row["netReturnPct"] for row in rows if row.get("netReturnPct") is not None]
    if not returns:
        return {
            "status": "unavailable",
            "reason": "未取得 510300/512100/159915 指数 ETF 的 open_15m 同口径数据，暂不能计算指数基准。",
            "benchmarks": rows,
        }
    return {
        "status": "ok",
        "count": len(rows),
        "tradeableCount": len(returns),
        "avgNetReturnPct": _avg(returns),
        "successRate": _rate([value > 0 for value in returns]),
        "avgOpenReturnPct": _avg([row.get("openReturnPct") for row in rows]),
        "avgCloseReturnPct": _avg([row.get("closeReturnPct") for row in rows]),
        "benchmarks": rows,
        "scenarioKey": PRIMARY_TRADEABLE_SCENARIO_KEY,
        "dataSource": f"minute_intraday:{minute_source or 'minute'}" if minute_derived_count else daily_source,
        "minuteDerivedCount": minute_derived_count,
        "reason": "指数 ETF 基准：510300、512100 与 159915 固定 open_15m，扣同一交易成本模型。",
    }


def _build_strategy_feedback(outcomes: List[Dict[str, Any]], window_runs: int, source_run_id: int, trigger_source: str) -> Dict[str, Any]:
    valid = [item for item in outcomes if item.get("outcomeLabel") != "数据不足"]
    labels = [item.get("outcomeLabel") for item in valid]
    success_labels = {"强成功", "成功溢价", "弱成功"}
    net_returns = [_primary_net_return(item) for item in valid]
    tag_sets = [_outcome_failure_tags(item) for item in valid]
    feedback = {
        "feedbackDate": date.today().isoformat(),
        "windowRuns": window_runs,
        "verifiedOutcomes": len(valid),
        "successRate": _rate([label in success_labels for label in labels]),
        "strongSuccessRate": _rate([label == "强成功" for label in labels]),
        "lowOpenFailRate": _rate(["low_open" in tags for tags in tag_sets]),
        "pullbackRate": _rate(["intraday_pullback" in tags or "premium_not_captured" in tags for tags in tag_sets]),
        "drawdownInvalidRate": _rate(["drawdown_invalid" in tags for tags in tag_sets]),
        "avgHighReturnPct": _avg([item.get("highReturnPct") for item in valid]),
        "avgCloseReturnPct": _avg([item.get("closeReturnPct") for item in valid]),
        "avgMaxDrawdownPct": _avg([item.get("maxDrawdownPct") for item in valid]),
        "avgNetReturnPct": _avg(net_returns),
        "validationVersion": TRADEABLE_VALIDATION_VERSION,
        "sourceRunId": source_run_id,
        "triggerSource": trigger_source,
    }
    patterns = _pattern_stats(valid)
    best = sorted(patterns, key=lambda item: (item.get("successRate") or 0, item.get("avgHighReturnPct") or 0), reverse=True)[:5]
    worst = sorted(patterns, key=lambda item: (item.get("lowOpenFailRate") or 0, -(item.get("successRate") or 0)), reverse=True)[:5]
    feedback["bestPatterns"] = best
    feedback["worstPatterns"] = worst
    feedback["promptSummary"] = _feedback_prompt_summary(feedback)
    return feedback


def _is_learning_eligible_outcome(item: Dict[str, Any]) -> bool:
    payload = _outcome_payload(item)
    if payload.get("validationVersion") != TRADEABLE_VALIDATION_VERSION:
        return False
    if payload.get("validationStatus") != "final":
        return False
    source_text = str(item.get("actualSource") or payload.get("actualSource") or item.get("dataSource") or payload.get("dataSource") or "")
    if "mock" in source_text or "cache" in source_text:
        return False
    quality = payload.get("dataQuality") or {}
    if quality.get("grade") not in {"A", "B"}:
        return False
    price = payload.get("priceConsistency") or {}
    if price and price.get("ok") is False:
        return False
    primary = (payload.get("tradeable") or {}).get("primaryScenario") or {}
    if primary.get("scenarioKey") != PRIMARY_TRADEABLE_SCENARIO_KEY:
        return False
    if primary.get("dataStatus") != "ok" or primary.get("netReturnPct") is None:
        return False
    if item.get("benchmarkReturnPct") is None and payload.get("benchmarkReturnPct") is None:
        return False
    if payload.get("learningEligible") is False and payload.get("exclusionReasons"):
        return False
    return True


def _strategy_sample_audit(all_outcomes: List[Dict[str, Any]], eligible: List[Dict[str, Any]]) -> Dict[str, Any]:
    eligible_ids = {(item.get("runId"), item.get("code")) for item in eligible}
    reasons: Dict[str, int] = {}
    trade_dates = {str(item.get("recommendationDate") or "") for item in all_outcomes if item.get("recommendationDate")}
    eligible_dates = {str(item.get("recommendationDate") or "") for item in eligible if item.get("recommendationDate")}
    for item in all_outcomes:
        if (item.get("runId"), item.get("code")) in eligible_ids:
            continue
        for reason in _learning_exclusion_reasons_for_loaded_outcome(item):
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "candidateOutcomes": len(all_outcomes),
        "eligibleOutcomes": len(eligible),
        "excludedOutcomes": max(0, len(all_outcomes) - len(eligible)),
        "officialTradeDates": len(trade_dates),
        "eligibleTradeDates": len(eligible_dates),
        "exclusionReasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reasons.items(), key=lambda row: (-row[1], row[0]))
        ],
        "primaryScenarioKey": PRIMARY_TRADEABLE_SCENARIO_KEY,
        "primaryFallbackAllowed": False,
        "windowBasis": "official_trade_dates",
    }


def _learning_exclusion_reasons_for_loaded_outcome(item: Dict[str, Any]) -> List[str]:
    payload = _outcome_payload(item)
    explicit = [str(reason) for reason in (payload.get("exclusionReasons") or []) if str(reason)]
    if explicit:
        return explicit
    reasons: List[str] = []
    if payload.get("validationVersion") != TRADEABLE_VALIDATION_VERSION:
        reasons.append("验证版本不是 v2_tradeable")
    if payload.get("validationStatus") and payload.get("validationStatus") != "final":
        reasons.append(f"验证状态 {payload.get('validationStatus')}")
    source_text = str(item.get("actualSource") or payload.get("actualSource") or item.get("dataSource") or payload.get("dataSource") or "")
    if "mock" in source_text:
        reasons.append("Mock 数据")
    if "cache" in source_text:
        reasons.append("缓存数据")
    quality = payload.get("dataQuality") or {}
    if quality.get("grade") not in {"A", "B"}:
        reasons.append(f"数据质量 {_data_quality_label(str(quality.get('grade') or ''))}")
    price = payload.get("priceConsistency") or {}
    if price and price.get("ok") is False:
        reasons.append("价格口径异常")
    primary = (payload.get("tradeable") or {}).get("primaryScenario") or {}
    if primary.get("scenarioKey") != PRIMARY_TRADEABLE_SCENARIO_KEY or primary.get("dataStatus") != "ok" or primary.get("netReturnPct") is None:
        reasons.append("固定 15m 主 KPI 不可用")
    if item.get("benchmarkReturnPct") is None and payload.get("benchmarkReturnPct") is None:
        reasons.append("同口径基准不可用")
    return reasons or ["不满足严格学习条件"]


def _suspected_historical_price_mismatch(item: Dict[str, Any]) -> bool:
    payload = _outcome_payload(item)
    price = payload.get("priceConsistency") or {}
    if price and price.get("ok") is False:
        return True
    if str(item.get("code") or "") == "603629" and abs(float(item.get("openReturnPct") or 0)) >= 20:
        return True
    returns = [
        _safe_number(item.get("openReturnPct")),
        _safe_number(item.get("highReturnPct")),
        _safe_number(item.get("closeReturnPct")),
    ]
    return any(value is not None and abs(value) >= 20 for value in returns)


def _pattern_stats(outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for item in outcomes:
        outcome = item.get("outcome") or {}
        risk_profile = outcome.get("riskProfile") or {}
        risk_label = risk_profile.get("label") or ""
        candidate_label = outcome.get("candidateTypeLabel") or outcome.get("candidateType") or ""
        market_context = outcome.get("marketContext") or {}
        market_label = market_context.get("label") or market_context.get("state") or ""
        quality = outcome.get("dataQuality") or {}
        quality_label = quality.get("label") or _data_quality_label(str(quality.get("grade") or "")) or ""
        for key in [
            risk_label,
            candidate_label,
            market_label,
            quality_label,
            f"{candidate_label}+{risk_label}" if candidate_label and risk_label else "",
            f"{market_label}+{candidate_label}" if market_label and candidate_label else "",
        ]:
            if key:
                buckets.setdefault(key, []).append(item)
    result = []
    for key, values in buckets.items():
        if len(values) < 2:
            continue
        labels = [item.get("outcomeLabel") for item in values]
        tag_sets = [_outcome_failure_tags(item) for item in values]
        result.append(
            {
                "pattern": key,
                "count": len(values),
                "successRate": _rate([label in {"强成功", "成功溢价", "弱成功"} for label in labels]),
                "strongSuccessRate": _rate([label == "强成功" for label in labels]),
                "lowOpenFailRate": _rate(["low_open" in tags for tags in tag_sets]),
                "pullbackRate": _rate(["intraday_pullback" in tags or "premium_not_captured" in tags for tags in tag_sets]),
                "drawdownInvalidRate": _rate(["drawdown_invalid" in tags for tags in tag_sets]),
                "avgHighReturnPct": _avg([item.get("highReturnPct") for item in values]),
                "avgNetReturnPct": _avg([_primary_net_return(item) for item in values]),
                "avgMaxDrawdownPct": _avg([item.get("maxDrawdownPct") for item in values]),
            }
        )
    return result


def _outcome_failure_tags(item: Dict[str, Any]) -> List[str]:
    payload = _outcome_payload(item)
    tags = payload.get("failureTags")
    if isinstance(tags, list):
        return [str(tag) for tag in tags if str(tag)]
    tradeable = payload.get("tradeable") or {}
    quality = tradeable.get("quality") or {}
    tags = quality.get("failureTags")
    if isinstance(tags, list):
        return [str(tag) for tag in tags if str(tag)]
    return _failure_tags(
        _primary_net_return(item),
        item.get("openReturnPct") if item.get("openReturnPct") is not None else payload.get("openReturnPct"),
        item.get("highReturnPct") if item.get("highReturnPct") is not None else payload.get("highReturnPct"),
        item.get("closeReturnPct") if item.get("closeReturnPct") is not None else payload.get("closeReturnPct"),
        item.get("maxDrawdownPct") if item.get("maxDrawdownPct") is not None else payload.get("maxDrawdownPct"),
    )


def _primary_net_return(item: Dict[str, Any]) -> Optional[float]:
    tradeable = (_outcome_payload(item).get("tradeable") or {})
    primary = tradeable.get("primaryScenario") or {}
    return _safe_number(primary.get("netReturnPct"))


def _outcome_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    payload = item.get("outcome")
    return payload if isinstance(payload, dict) else item


def _t1_outcome_display_only(item: Dict[str, Any]) -> bool:
    payload = _outcome_payload(item)
    treatment = payload.get("historicalTreatment") or {}
    return bool(payload.get("displayOnly") or (isinstance(treatment, dict) and treatment.get("displayOnly")))


def _compound_return_pct(values: List[Any]) -> Optional[float]:
    numbers = [_safe_number(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    if not numbers:
        return None
    total = 1.0
    for value in numbers:
        total *= 1 + float(value) / 100
    return round((total - 1) * 100, 2)


def _feedback_prompt_summary(feedback: Dict[str, Any]) -> str:
    if int(feedback.get("verifiedOutcomes") or 0) <= 0:
        return "暂无足够 T+1 次日验证样本，DeepSeek 不应假装已学习历史规律。"
    best = feedback.get("bestPatterns") or []
    worst = feedback.get("worstPatterns") or []
    audit = feedback.get("sampleAudit") or {}
    basis = "official 交易日" if audit.get("windowBasis") == "official_trade_dates" else "T+1 推荐"
    parts = [
        f"最近 {feedback.get('windowRuns')} 个{basis}窗口中，严格学习样本 {feedback.get('verifiedOutcomes')} 只。",
        f"固定 open_15m 成功率 {format(float(feedback.get('successRate') or 0), '.1f')}%，强成功率 {format(float(feedback.get('strongSuccessRate') or 0), '.1f')}%，平均净收益 {format(float(feedback.get('avgNetReturnPct') or 0), '.2f')}%，平均最大回撤 {format(float(feedback.get('avgMaxDrawdownPct') or 0), '.2f')}%。",
    ]
    if audit:
        parts.append(
            f"候选复盘样本 {audit.get('candidateOutcomes', 0)} 只，排除 {audit.get('excludedOutcomes', 0)} 只；主 KPI 缺失不 fallback。"
        )
    portfolio = feedback.get("portfolio") or {}
    if portfolio.get("verifiedDays"):
        parts.append(
            f"组合口径：official 日 {portfolio.get('officialDays', 0)} 个，已验证 {portfolio.get('verifiedDays', 0)} 个，Top0 空仓 {portfolio.get('zeroPickDays', 0)} 个，平均组合收益 {format(float(portfolio.get('avgPortfolioReturnPct') or 0), '.2f')}%，平均超额 {format(float(portfolio.get('avgExcessReturnPct') or 0), '.2f')}%。"
        )
    if best:
        parts.append("表现较好：" + "；".join(f"{item['pattern']} 成功率 {item['successRate']:.1f}%" for item in best[:2]))
    if worst:
        parts.append("表现较差：" + "；".join(f"{item['pattern']} 低开失败率 {item['lowOpenFailRate']:.1f}%" for item in worst[:2]))
    parts.append("系统可根据这些统计生成待确认策略建议；未经用户确认的建议不影响 DeepSeek 终选或 Critic 自检。")
    return "\n".join(parts)


def _contains_unconfirmed_strategy_instruction(summary: str) -> bool:
    text = str(summary or "")
    return any(pattern in text for pattern in ["必须降低优先级", "不得入选", "不应优先入选", "必须同时满足"])


def _same_strategy_feedback(previous: Dict[str, Any], current: Dict[str, Any]) -> bool:
    keys = [
        "windowRuns",
        "verifiedOutcomes",
        "successRate",
        "strongSuccessRate",
        "lowOpenFailRate",
        "pullbackRate",
        "drawdownInvalidRate",
        "avgHighReturnPct",
        "avgCloseReturnPct",
        "avgMaxDrawdownPct",
        "avgNetReturnPct",
        "promptSummary",
    ]
    for key in keys:
        if previous.get(key) != current.get(key):
            return False
    return (
        (previous.get("bestPatterns") or []) == (current.get("bestPatterns") or [])
        and (previous.get("worstPatterns") or []) == (current.get("worstPatterns") or [])
        and (previous.get("portfolio") or {}) == (current.get("portfolio") or {})
    )


def _strategy_feedback_display_only(feedback: Dict[str, Any]) -> bool:
    treatment = (feedback or {}).get("historicalTreatment") or {}
    return isinstance(treatment, dict) and bool(treatment.get("displayOnly") or treatment.get("learningEligible") is False)


def _build_strategy_suggestions(feedback: Dict[str, Any]) -> List[Dict[str, Any]]:
    if int(feedback.get("verifiedOutcomes") or 0) <= 0:
        return []
    patterns: Dict[str, Dict[str, Any]] = {}
    for item in (feedback.get("bestPatterns") or []) + (feedback.get("worstPatterns") or []):
        key = str(item.get("pattern") or "")
        if key:
            patterns[key] = item
    baseline_success = float(feedback.get("successRate") or 0)
    suggestions = []
    for pattern, item in patterns.items():
        count = int(item.get("count") or 0)
        success_rate = float(item.get("successRate") or 0)
        avg_net = _safe_number(item.get("avgNetReturnPct")) or 0
        avg_drawdown = _safe_number(item.get("avgMaxDrawdownPct")) or 0
        low_open = float(item.get("lowOpenFailRate") or 0)
        pullback = float(item.get("pullbackRate") or 0)
        drawdown_invalid = float(item.get("drawdownInvalidRate") or 0)
        if count < 3:
            suggestions.append(
                _strategy_suggestion(
                    pattern=pattern,
                    item=item,
                    action="observe",
                    suggestion_type="sample_observation",
                    reason=f"{pattern} 当前只有 {count} 个样本，建议继续观察，不影响后续推荐。",
                    confidence=35,
                )
            )
            continue
        if low_open >= 20 or pullback >= 20 or drawdown_invalid >= 20 or avg_drawdown <= -4 or avg_net <= -1 or success_rate <= max(0, baseline_success - 10):
            suggestions.append(
                _strategy_suggestion(
                    pattern=pattern,
                    item=item,
                    action="downweight",
                    suggestion_type="risk_control",
                    reason=f"{pattern} 历史样本 {count} 个，成功率 {success_rate:.1f}%，平均净收益 {avg_net:.2f}%，平均回撤 {avg_drawdown:.2f}%，建议待确认降权。",
                    confidence=min(90, 45 + count * 4),
                )
            )
            continue
        if success_rate >= baseline_success + 15 and avg_net >= 0:
            suggestions.append(
                _strategy_suggestion(
                    pattern=pattern,
                    item=item,
                    action="upweight",
                    suggestion_type="opportunity",
                    reason=f"{pattern} 历史样本 {count} 个，成功率 {success_rate:.1f}%，高于整体 {baseline_success:.1f}%，建议待确认加权。",
                    confidence=min(88, 42 + count * 4),
                )
            )
    priority = {"downweight": 0, "upweight": 1, "observe": 2}
    return sorted(
        suggestions,
        key=lambda item: (priority.get(item.get("suggestionAction"), 9), -(item.get("confidence") or 0), -int(item.get("sampleCount") or 0)),
    )[:10]


def _strategy_suggestion(pattern: str, item: Dict[str, Any], action: str, suggestion_type: str, reason: str, confidence: float) -> Dict[str, Any]:
    return {
        "patternKey": _strategy_pattern_key(pattern),
        "patternLabel": pattern,
        "sampleCount": int(item.get("count") or 0),
        "successRate": item.get("successRate"),
        "strongSuccessRate": item.get("strongSuccessRate"),
        "lowOpenFailRate": item.get("lowOpenFailRate"),
        "pullbackRate": item.get("pullbackRate"),
        "drawdownInvalidRate": item.get("drawdownInvalidRate"),
        "avgNetReturnPct": item.get("avgNetReturnPct"),
        "avgMaxDrawdownPct": item.get("avgMaxDrawdownPct"),
        "suggestionType": suggestion_type,
        "suggestionAction": action,
        "suggestionReason": reason,
        "confidence": round(float(confidence), 1),
    }


def _strategy_pattern_key(pattern: str) -> str:
    return str(pattern or "").strip().lower().replace(" ", "_")


def _confirmed_strategy_constraints(active_suggestions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    constraints = []
    for item in active_suggestions[:20]:
        constraints.append(
            {
                "id": item.get("id"),
                "action": item.get("suggestionAction"),
                "pattern": item.get("patternLabel") or item.get("patternKey"),
                "sampleCount": item.get("sampleCount"),
                "successRate": item.get("successRate"),
                "avgNetReturnPct": item.get("avgNetReturnPct"),
                "avgMaxDrawdownPct": item.get("avgMaxDrawdownPct"),
                "reason": item.get("reviewerNote") or item.get("suggestionReason"),
                "activatedAt": item.get("activatedAt"),
            }
        )
    return constraints


def _empty_strategy_feedback() -> Dict[str, Any]:
    return {
        "feedbackDate": "",
        "windowRuns": 0,
        "verifiedOutcomes": 0,
        "successRate": 0,
        "strongSuccessRate": 0,
        "lowOpenFailRate": 0,
        "pullbackRate": 0,
        "drawdownInvalidRate": 0,
        "avgHighReturnPct": 0,
        "avgCloseReturnPct": 0,
        "avgMaxDrawdownPct": 0,
        "avgNetReturnPct": 0,
        "sampleAudit": {
            "candidateOutcomes": 0,
            "eligibleOutcomes": 0,
            "excludedOutcomes": 0,
            "officialTradeDates": 0,
            "eligibleTradeDates": 0,
            "exclusionReasons": [],
            "primaryScenarioKey": PRIMARY_TRADEABLE_SCENARIO_KEY,
            "primaryFallbackAllowed": False,
            "windowBasis": "official_trade_dates",
        },
        "portfolio": {
            "windowRuns": 0,
            "officialDays": 0,
            "verifiedDays": 0,
            "zeroPickDays": 0,
            "avgPortfolioReturnPct": None,
            "avgBenchmarkReturnPct": None,
            "avgExcessReturnPct": None,
            "cumulativePortfolioReturnPct": None,
            "days": [],
            "slotCount": PORTFOLIO_SLOT_COUNT,
            "basis": "official_trade_dates",
        },
        "bestPatterns": [],
        "worstPatterns": [],
        "promptSummary": "暂无策略复盘样本。",
        "suggestions": [],
        "pendingSuggestions": [],
        "activeConstraints": [],
    }


def _avg(values: List[Any]) -> Optional[float]:
    numbers = [_safe_number(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 2)


def _rate(values: List[bool]) -> float:
    if not values:
        return 0.0
    return round(len([value for value in values if value]) / len(values) * 100, 1)


def _same_run_request(
    run: Dict[str, Any],
    scan_time: str,
    pool_size: int,
    batch_size: int,
    batch_pick: int,
    final_limit: int,
    stage_config: Optional[Dict[str, Any]] = None,
) -> bool:
    if str(run.get("recommendationDate") or "") != date.today().isoformat():
        return False
    request = run.get("request") or {}
    return (
        str(run.get("scanTime") or request.get("scanTime") or "") == scan_time
        and int(request.get("poolSize") or run.get("poolSize") or 0) == pool_size
        and int(run.get("batchSize") or request.get("batchSize") or 0) == batch_size
        and int(run.get("batchPick") or request.get("batchPick") or 0) == batch_pick
        and int(run.get("finalLimit") or request.get("finalLimit") or 0) == final_limit
        and _normalize_stage_config(request.get("stageConfig") or DEFAULT_STAGE_CONFIG)
        == _normalize_stage_config(stage_config or DEFAULT_STAGE_CONFIG)
    )


def _t1_stage_controls_snapshot(
    config: Optional[Dict[str, Any]] = None,
    official_runs: int = 0,
    tradeable_outcomes: int = 0,
) -> Dict[str, Any]:
    config = _normalize_stage_config(config or DEFAULT_STAGE_CONFIG)
    shadow_enabled = (
        _env_bool(T1_SECTOR_STATE_SHADOW_ENV, config["sectorStateShadowEnabled"])
        if T1_SECTOR_STATE_SHADOW_ENV is not None
        else config["sectorStateShadowEnabled"]
    )
    style_shadow_enabled = (
        _env_bool(T1_STYLE_REGIME_SHADOW_ENV, config["styleRegimeShadowEnabled"])
        if T1_STYLE_REGIME_SHADOW_ENV is not None
        else config["styleRegimeShadowEnabled"]
    )
    stage2 = _stage_gate(
        config["sectorRetreatDowngradeMode"],
        config["minOfficialTradeDaysForStage2"],
        config["minTradeableOutcomesForStage2"],
        official_runs,
        tradeable_outcomes,
    )
    stage3 = _stage_gate(
        config["styleRegimePromptMode"],
        config["minOfficialTradeDaysForStage3"],
        config["minTradeableOutcomesForStage3"],
        official_runs,
        tradeable_outcomes,
    )
    if T1_SECTOR_RETREAT_DOWNGRADE_ENV is not None:
        stage2["effective"] = _env_bool(T1_SECTOR_RETREAT_DOWNGRADE_ENV, False)
        stage2["productionEligible"] = stage2["effective"]
        stage2["experimental"] = False
        stage2["reason"] = "由环境变量覆盖页面配置。"
    if T1_STYLE_REGIME_PROMPT_ENV is not None:
        stage3["effective"] = _env_bool(T1_STYLE_REGIME_PROMPT_ENV, False)
        stage3["productionEligible"] = stage3["effective"]
        stage3["experimental"] = False
        stage3["reason"] = "由环境变量覆盖页面配置。"
    sector_prompt_enabled = (
        _env_bool(T1_SECTOR_STATE_PROMPT_ENV, False)
        if T1_SECTOR_STATE_PROMPT_ENV is not None
        else bool(stage2["effective"] and shadow_enabled)
    )
    return {
        "config": config,
        "sampleStatus": {
            "officialTradeDays": official_runs,
            "tradeableOutcomes": tradeable_outcomes,
        },
        "tradingDayGuard": {
            "enabled": T1_TRADING_DAY_GUARD_ENABLED,
            "version": T1_TRADE_DATE_GUARD_VERSION,
            "effect": "非交易日或数据日期不一致时禁止生成正式 T+1 TopN。",
        },
        "sectorStateShadow": {
            "enabled": shadow_enabled,
            "effectiveEnabled": shadow_enabled,
            "version": T1_SECTOR_STATE_SHADOW_VERSION,
            "effect": "板块状态影子只展示和落库；正式降权由阶段2单独控制。",
        },
        "sectorStatePrompt": {
            "enabled": sector_prompt_enabled,
            "effectiveEnabled": sector_prompt_enabled,
            "version": T1_SECTOR_STATE_SHADOW_VERSION,
            "effect": "阶段2生效时，允许 DeepSeek 引用 S2 板块状态证据。",
        },
        "sectorRetreatDowngrade": {
            "enabled": bool(stage2["effective"]),
            "effectiveEnabled": bool(stage2["effective"] and shadow_enabled),
            "mode": config["sectorRetreatDowngradeMode"],
            "requestedMode": config["sectorRetreatDowngradeMode"],
            "requested": bool(stage2["requested"]),
            "productionEligible": bool(stage2["productionEligible"] and shadow_enabled),
            "experimental": bool(stage2["experimental"]),
            "officialGatePassed": bool(stage2["productionEligible"]),
            "version": T1_SECTOR_STATE_SHADOW_VERSION,
            "effect": "阶段2：对退潮、派发、高热兑现板块中的普通后排有限降权，保留逆势核心例外。",
            "reason": stage2["reason"],
        },
        "styleRegimeShadow": {
            "enabled": style_shadow_enabled,
            "effectiveEnabled": style_shadow_enabled,
            "version": T1_STYLE_REGIME_SHADOW_VERSION,
            "effect": "阶段3影子聚合，只用于观察，不直接改变正式 Top3。",
        },
        "styleRegimePrompt": {
            "enabled": bool(stage3["effective"]),
            "effectiveEnabled": bool(stage3["effective"] and style_shadow_enabled),
            "mode": config["styleRegimePromptMode"],
            "requestedMode": config["styleRegimePromptMode"],
            "requested": bool(stage3["requested"]),
            "productionEligible": bool(stage3["productionEligible"] and style_shadow_enabled),
            "experimental": bool(stage3["experimental"]),
            "officialGatePassed": bool(stage3["productionEligible"]),
            "version": T1_STYLE_REGIME_SHADOW_VERSION,
            "effect": "阶段3：允许 DeepSeek 在最终比较中参考风格轮动影子，不替代原始证据。",
            "reason": stage3["reason"],
        },
    }
    return {
        "tradingDayGuard": {
            "enabled": T1_TRADING_DAY_GUARD_ENABLED,
            "version": T1_TRADE_DATE_GUARD_VERSION,
            "effect": "非交易日或数据日期不一致时禁止生成正式 T+1 TopN。",
        },
        "sectorStateShadow": {
            "enabled": T1_SECTOR_STATE_SHADOW_ENABLED,
            "version": T1_SECTOR_STATE_SHADOW_VERSION,
            "effect": "阶段1影子计算，只展示和落库，不直接改变正式 Top3。",
        },
        "sectorStatePrompt": {
            "enabled": T1_SECTOR_STATE_PROMPT_ENABLED,
            "version": T1_SECTOR_STATE_SHADOW_VERSION,
            "effect": "关闭时 DeepSeek Prompt 不包含板块状态影子字段。",
        },
        "sectorRetreatDowngrade": {
            "enabled": T1_SECTOR_RETREAT_DOWNGRADE_ENABLED,
            "version": T1_SECTOR_STATE_SHADOW_VERSION,
            "effect": "阶段2开关，默认关闭；开启后才允许对退潮普通后排有限降权。",
        },
        "styleRegimeShadow": {
            "enabled": T1_STYLE_REGIME_SHADOW_ENABLED,
            "version": T1_STYLE_REGIME_SHADOW_VERSION,
            "effect": "阶段3影子聚合，只用于观察，不直接改变正式 Top3。",
        },
        "styleRegimePrompt": {
            "enabled": T1_STYLE_REGIME_PROMPT_ENABLED,
            "version": T1_STYLE_REGIME_SHADOW_VERSION,
            "effect": "关闭时 DeepSeek Prompt 不包含风格轮动影子字段。",
        },
    }


def _t1_trade_date_status(
    recommendation_date: str,
    scan_time: str,
    actual_source: str = "",
    market: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    rec_text = str(recommendation_date or "")[:10]
    market = market or {}
    reasons: List[str] = []
    status = "assumed_trading_day"
    eligibility = "official_allowed"
    is_trading_day = True
    try:
        rec_date = date.fromisoformat(rec_text)
    except ValueError:
        rec_date = None
        status = "invalid_date"
        eligibility = "official_blocked"
        is_trading_day = False
        reasons.append("推荐日期格式无效，不能生成正式 T+1 推荐。")

    if rec_date and rec_date.weekday() >= 5:
        status = "non_trading_weekend"
        eligibility = "official_blocked"
        is_trading_day = False
        reasons.append("推荐日期是周末，A 股不开市，禁止生成正式 T+1 推荐。")

    source_text = str(actual_source or "")
    mock_used = source_text == "mock" or bool((market.get("mockStatus") or {}).get("used"))
    if mock_used and eligibility != "official_blocked":
        status = "mock_research_only"
        eligibility = "research_only"
        reasons.append("当前使用 Mock 数据，只能研究演示，不能作为正式 T+1 推荐。")

    data_as_of_date = _t1_market_data_as_of_date(market)
    if data_as_of_date and rec_text and data_as_of_date != rec_text:
        status = "data_date_mismatch"
        eligibility = "official_blocked"
        is_trading_day = False
        reasons.append(f"行情快照日期 {data_as_of_date} 与推荐日期 {rec_text} 不一致，禁止生成正式推荐。")

    cache_status = market.get("cacheStatus") or {}
    if (cache_status.get("stale") or source_text.startswith("cache:")) and data_as_of_date and data_as_of_date != rec_text:
        status = "stale_cache_date_mismatch"
        eligibility = "official_blocked"
        is_trading_day = False
        reasons.append("全市场缓存不是推荐日数据。")

    if not reasons and rec_date:
        reasons.append("按工作日与行情快照时间进行交易日校验；节假日以行情日期不匹配兜底。")

    return {
        "version": T1_TRADE_DATE_GUARD_VERSION,
        "recommendationDate": rec_text,
        "recommendationTradeDate": rec_text,
        "dataAsOfDate": data_as_of_date or rec_text,
        "asOfTime": scan_time or "",
        "isTradingDay": bool(is_trading_day),
        "status": status,
        "eligibility": eligibility,
        "source": "weekday_calendar+snapshot_date",
        "guardEnabled": T1_TRADING_DAY_GUARD_ENABLED,
        "reasons": _unique_strings(reasons),
    }


def _t1_market_data_as_of_date(market: Dict[str, Any]) -> str:
    for source in (
        market.get("dataAsOfDate"),
        market.get("tradeDate"),
        (market.get("marketSnapshotFreeze") or {}).get("generatedAt"),
        (market.get("cacheStatus") or {}).get("generatedAt"),
        market.get("contextGeneratedAt"),
    ):
        text = str(source or "")
        if len(text) >= 10 and re.match(r"\d{4}-\d{2}-\d{2}", text[:10]):
            return text[:10]
    return ""


def _t1_trade_date_blocks_official(status: Dict[str, Any]) -> bool:
    if not T1_TRADING_DAY_GUARD_ENABLED:
        return False
    return str(status.get("eligibility") or "") == "official_blocked" or bool(status.get("isTradingDay") is False)


def _t1_trade_date_block_reason(status: Dict[str, Any]) -> str:
    reasons = [str(item) for item in status.get("reasons") or [] if str(item or "").strip()]
    return "；".join(reasons) or "当前不是可确认交易日，不生成正式 T+1 推荐。"


def _trade_date_block_policy(status: Dict[str, Any]) -> Dict[str, Any]:
    reason = _t1_trade_date_block_reason(status)
    return {
        "requestedLimit": DEFAULT_FINAL_LIMIT,
        "maxPickCount": 0,
        "allowZeroPick": True,
        "allowReducePickCount": True,
        "reason": reason,
        "tradeDateGuardBlocked": True,
    }


def _initial_run_role(scan_time: str, force: bool, pool_size: int) -> str:
    if force or int(pool_size or 0) < T1_OFFICIAL_MIN_POOL_SIZE:
        return "research"
    if OFFICIAL_SCAN_START <= scan_time <= OFFICIAL_SCAN_END:
        return "official"
    return "research"


def _run_profile_for_pool(pool_size: int) -> Dict[str, Any]:
    size = int(pool_size or DEFAULT_POOL_SIZE)
    if size >= T1_PRODUCTION_POOL_SIZE:
        return {
            "key": "production_500",
            "label": "生产模式",
            "poolSize": size,
            "officialEligible": True,
            "description": "全市场候选池优先，适合作为正式 T+1 run。",
        }
    if size >= T1_STANDARD_POOL_SIZE:
        return {
            "key": "standard_300",
            "label": "标准模式",
            "poolSize": size,
            "officialEligible": True,
            "description": "候选覆盖中等，满足正式样本最低要求。",
        }
    return {
        "key": "quick_research",
        "label": "快速研究",
        "poolSize": size,
        "officialEligible": False,
        "description": f"候选池低于 {T1_OFFICIAL_MIN_POOL_SIZE}，只用于调试/研究，不进入严格学习。",
    }


def _normalize_scan_time(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return DEFAULT_SCAN_TIME


def _time_hint(scan_time: str) -> str:
    if "09:30" <= scan_time <= "14:29":
        return "当前为预筛结果，不是最终 T+1 推荐。"
    if "14:30" <= scan_time <= "14:50":
        return "当前时间适合做 T+1 最终确认。"
    if scan_time > "14:50":
        return "当前为尾盘后结果，需注意数据源是否已更新。"
    return "建议在 14:30-14:50 手动点击做最终确认。"


def _join_warning(*messages: Any) -> str:
    result = []
    for message in messages:
        text = str(message or "").strip()
        if text and text not in result:
            result.append(text)
    return "；".join(result)


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


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


def _json_dict(value: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> List[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _progress_pct(run: Dict[str, Any]) -> int:
    batch_count = int(run.get("batchCount") or 0)
    completed = int(run.get("completedBatchCount") or 0)
    if run.get("status") == "done":
        return 100
    if run.get("status") == "failed":
        return 100
    stage = str(run.get("currentStage") or "")
    if stage == "pending" or run.get("status") == "pending":
        return 3
    if stage == "loading_market":
        return 12
    if stage == "building_pool":
        return 24
    if batch_count <= 0:
        return 0
    if stage == "final":
        return min(99, 88 + max(0, min(6, completed)))
    if stage == "critic":
        return 96
    return min(99, round(30 + (completed / batch_count) * 58))


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
