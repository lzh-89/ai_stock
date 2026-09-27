from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from backend.app.database import Database
from backend.app.services.analysis_service import AnalysisService
from backend.app.settings import PROJECT_ROOT


LOGGER = logging.getLogger("ai_stock.daily_kline_cache")

DAILY_KLINE_CACHE_STATE_KEY = "daily_kline_cache_scheduler_state"
INDEX_CODES = {"510300", "512100", "159915"}
DEFAULT_DAILY_KLINE_CACHE_CONFIG = {
    "enabled": True,
    "scheduleTimes": ["12:00", "16:00"],
    "pollIntervalSeconds": 60,
    "maxCatchupMinutes": 60,
    "days": 80,
    "recentRuns": 30,
    "retainDays": 10,
}


class DailyKlineCacheService:
    def __init__(self, analysis: AnalysisService, database: Database):
        self.analysis = analysis
        self.database = database
        self._scheduler_thread: Optional[threading.Thread] = None
        self._scheduler_lock = threading.Lock()

    def status(self) -> Dict[str, Any]:
        state = _normalize_state(self.database.get_app_setting(DAILY_KLINE_CACHE_STATE_KEY, {}))
        state["schedulerAlive"] = bool(self._scheduler_thread and self._scheduler_thread.is_alive())
        state["nextSlots"] = _due_slots(datetime.now(), state["config"], state, include_future=True)
        return state

    def start_scheduler(self) -> Dict[str, Any]:
        state = _normalize_state(self.database.get_app_setting(DAILY_KLINE_CACHE_STATE_KEY, {}))
        if not state["config"]["enabled"]:
            state["schedulerAlive"] = False
            return state
        with self._scheduler_lock:
            if self._scheduler_thread and self._scheduler_thread.is_alive():
                state["schedulerAlive"] = True
                return state
            thread = threading.Thread(target=self._scheduler_loop, name="daily-kline-cache", daemon=True)
            self._scheduler_thread = thread
            thread.start()
        state["schedulerAlive"] = True
        return state

    def run_due(
        self,
        trigger_source: str = "manual",
        now: Optional[datetime] = None,
        slot: str = "",
        force: bool = False,
    ) -> Dict[str, Any]:
        now_dt = now or datetime.now()
        with self._scheduler_lock:
            state = _normalize_state(self.database.get_app_setting(DAILY_KLINE_CACHE_STATE_KEY, {}))
            config = state["config"]
            checked_at = now_dt.isoformat(timespec="seconds")
            state["lastCheckAt"] = checked_at
            if not config["enabled"] and not force:
                state["lastStatus"] = "disabled"
                state["lastMessage"] = "日线缓存自动刷新未开启。"
                self.database.set_app_setting(DAILY_KLINE_CACHE_STATE_KEY, state)
                return {**state, "processed": []}

            today = now_dt.date().isoformat()
            if now_dt.weekday() >= 5 and not force:
                state["lastStatus"] = "non_trading_day_skipped"
                state["lastMessage"] = "今天是周末，日线缓存自动刷新跳过。"
                self.database.set_app_setting(DAILY_KLINE_CACHE_STATE_KEY, state)
                return {**state, "processed": []}

            slots = [_normalize_time(slot)] if slot else _due_slots(now_dt, config, state)
            slots = [item for item in slots if item]
            if not slots:
                state["lastStatus"] = "waiting"
                state["lastMessage"] = "未到日线缓存刷新时间，或今天的时间点已经处理过。"
                self.database.set_app_setting(DAILY_KLINE_CACHE_STATE_KEY, state)
                return {**state, "processed": []}

            processed = []
            # If the server was down across multiple slots, one refresh at the latest slot
            # provides the freshest data without downloading the same universe twice.
            for superseded_slot in slots[:-1]:
                event = {
                    "status": "superseded",
                    "slot": superseded_slot,
                    "tradeDate": today,
                    "triggerSource": trigger_source,
                    "checkedAt": checked_at,
                    "message": f"由较晚的 {slots[-1]} 刷新合并处理。",
                }
                processed.append(event)
                _record_event(state, today, superseded_slot, event, config)

            due_slot = slots[-1]
            event = self._run_slot(today, due_slot, trigger_source, checked_at, config, now_dt)
            processed.append(event)
            _record_event(state, today, due_slot, event, config)
            state["lastStatus"] = event["status"]
            state["lastMessage"] = event["message"]
            state["lastRunAt"] = checked_at
            self.database.set_app_setting(DAILY_KLINE_CACHE_STATE_KEY, state)
            return {**state, "processed": processed}

    def refresh(
        self,
        days: int = 80,
        recent_runs: int = 30,
        now: Optional[datetime] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        now_dt = now or datetime.now()
        if now_dt.weekday() >= 5 and not force:
            return {"updated": False, "skipped": True, "reason": "weekend"}

        codes = tracked_codes(self.database, max(1, recent_runs))
        if not codes:
            return {"updated": False, "reason": "no tracked codes"}
        series_by_code = self.analysis.daily_kline_provider.fetch_many(
            sorted(codes),
            days=max(40, days),
            force=True,
        )
        source = self.analysis.daily_kline_provider.last_source or "daily_kline"
        saved_rows = self.analysis._save_daily_kline_to_database(series_by_code, source)
        return {
            "updated": bool(series_by_code),
            "requested": len(codes),
            "fetched": len(series_by_code),
            "missing": len(codes - set(series_by_code)),
            "savedRows": saved_rows,
            "source": source,
            "dataDate": self.analysis.daily_kline_provider.last_data_date,
            "warning": self.analysis.daily_kline_provider.last_warning,
        }

    def _run_slot(
        self,
        trade_date: str,
        slot: str,
        trigger_source: str,
        checked_at: str,
        config: Dict[str, Any],
        now_dt: datetime,
    ) -> Dict[str, Any]:
        try:
            result = self.refresh(
                days=int(config["days"]),
                recent_runs=int(config["recentRuns"]),
                now=now_dt,
            )
        except Exception as error:  # noqa: BLE001 - scheduler failures must be recorded.
            LOGGER.exception("daily kline cache refresh failed slot=%s", slot)
            return {
                "status": "failed",
                "slot": slot,
                "tradeDate": trade_date,
                "triggerSource": trigger_source,
                "checkedAt": checked_at,
                "message": str(error),
            }
        missing = int(result.get("missing") or 0)
        status = "completed" if result.get("updated") and missing == 0 else "partial"
        if not result.get("updated"):
            status = "failed"
        return {
            "status": status,
            "slot": slot,
            "tradeDate": trade_date,
            "triggerSource": trigger_source,
            "checkedAt": checked_at,
            "message": (
                f"日线缓存刷新完成：请求 {result.get('requested', 0)}，"
                f"成功 {result.get('fetched', 0)}，缺失 {missing}。"
            ),
            "result": result,
        }

    def _scheduler_loop(self) -> None:
        while True:
            try:
                state = _normalize_state(self.database.get_app_setting(DAILY_KLINE_CACHE_STATE_KEY, {}))
                config = state["config"]
                if config["enabled"]:
                    self.run_due(trigger_source="scheduler")
                interval = int(config["pollIntervalSeconds"])
            except Exception as error:  # noqa: BLE001 - scheduler must not kill the server.
                LOGGER.warning("daily kline cache scheduler tick failed error=%s", error)
                interval = 60
            time.sleep(interval)


def tracked_codes(database: Database, recent_runs: int = 30) -> set[str]:
    return _tracked_codes(database, max(1, recent_runs))


def _tracked_codes(database: Database, recent_runs: int) -> set[str]:
    codes = set(INDEX_CODES)
    _add_codes_from_cache(codes, PROJECT_ROOT / "data" / "daily_kline_cache.json")
    _add_codes_from_universe(codes, PROJECT_ROOT / "data" / "default_universe.json")
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT final_picks_json
            FROM t1_overnight_selection_runs
            WHERE status = 'done'
            ORDER BY id DESC
            LIMIT ?
            """,
            (recent_runs,),
        ).fetchall()
        outcome_rows = connection.execute(
            "SELECT DISTINCT code FROM t1_overnight_selection_outcomes"
        ).fetchall()
    for row in outcome_rows:
        _add_code(codes, row["code"])
    for row in rows:
        for item in _json_list(row["final_picks_json"]):
            _add_code(codes, item.get("code"))
    return codes


def _normalize_config(config: Any) -> Dict[str, Any]:
    raw = dict(config or {}) if isinstance(config, dict) else {}
    normalized = dict(DEFAULT_DAILY_KLINE_CACHE_CONFIG)
    normalized.update({key: value for key, value in raw.items() if key in normalized})
    schedule_times = raw.get("scheduleTimes", normalized["scheduleTimes"])
    if not isinstance(schedule_times, list):
        schedule_times = DEFAULT_DAILY_KLINE_CACHE_CONFIG["scheduleTimes"]
    normalized["scheduleTimes"] = list(dict.fromkeys(filter(None, (_normalize_time(item) for item in schedule_times))))
    if not normalized["scheduleTimes"]:
        normalized["scheduleTimes"] = list(DEFAULT_DAILY_KLINE_CACHE_CONFIG["scheduleTimes"])
    normalized["enabled"] = bool(normalized["enabled"])
    normalized["pollIntervalSeconds"] = _clamp_int(normalized["pollIntervalSeconds"], 30, 600, 60)
    normalized["maxCatchupMinutes"] = _clamp_int(normalized["maxCatchupMinutes"], 0, 360, 60)
    normalized["days"] = _clamp_int(normalized["days"], 40, 500, 80)
    normalized["recentRuns"] = _clamp_int(normalized["recentRuns"], 1, 500, 30)
    normalized["retainDays"] = _clamp_int(normalized["retainDays"], 3, 60, 10)
    return normalized


def _normalize_state(state: Any) -> Dict[str, Any]:
    raw = dict(state or {}) if isinstance(state, dict) else {}
    return {
        "version": "v1_daily_kline_cache_scheduler",
        "config": _normalize_config(raw.get("config")),
        "days": raw.get("days") if isinstance(raw.get("days"), dict) else {},
        "lastCheckAt": str(raw.get("lastCheckAt") or ""),
        "lastRunAt": str(raw.get("lastRunAt") or ""),
        "lastStatus": str(raw.get("lastStatus") or "unknown"),
        "lastMessage": str(raw.get("lastMessage") or ""),
        "lastEvent": raw.get("lastEvent") if isinstance(raw.get("lastEvent"), dict) else {},
    }


def _due_slots(now_dt: datetime, config: Dict[str, Any], state: Dict[str, Any], include_future: bool = False) -> list[str]:
    today = now_dt.date().isoformat()
    current_time = now_dt.strftime("%H:%M")
    day_state = (state.get("days") or {}).get(today) or {}
    slots = []
    for slot in config.get("scheduleTimes") or []:
        normalized = _normalize_time(slot)
        if not normalized or normalized in day_state:
            continue
        if include_future and normalized >= current_time:
            slots.append(normalized)
        elif not include_future and normalized <= current_time:
            slot_time = datetime.strptime(normalized, "%H:%M").time()
            slot_dt = datetime.combine(now_dt.date(), slot_time)
            age_minutes = (now_dt - slot_dt).total_seconds() / 60
            if age_minutes <= int(config.get("maxCatchupMinutes") or 0):
                slots.append(normalized)
    return slots


def _record_event(state: Dict[str, Any], trade_date: str, slot: str, event: Dict[str, Any], config: Dict[str, Any]) -> None:
    days = state.setdefault("days", {})
    days.setdefault(trade_date, {})[slot] = event
    state["lastEvent"] = event
    cutoff = (date.today() - timedelta(days=int(config["retainDays"]))).isoformat()
    for day_key in list(days):
        if str(day_key) < cutoff:
            days.pop(day_key, None)


def _normalize_time(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        return ""
    return parsed.strftime("%H:%M")


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _add_code(codes: set[str], value: Any) -> None:
    text = str(value or "").strip()
    if text.isdigit() and len(text) <= 6:
        codes.add(text.zfill(6))


def _add_codes_from_cache(codes: set[str], path: Path) -> None:
    payload = _read_json(path)
    items = payload.get("items") or {}
    if isinstance(items, dict):
        for code in items:
            _add_code(codes, code)


def _add_codes_from_universe(codes: set[str], path: Path) -> None:
    payload = _read_json(path)
    for item in payload.get("items") or []:
        if isinstance(item, dict):
            _add_code(codes, item.get("code"))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
