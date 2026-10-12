from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


LOGGER = logging.getLogger("ai_stock.database")
SQLITE_BUSY_TIMEOUT_SECONDS = float(os.getenv("SQLITE_BUSY_TIMEOUT_SECONDS", "30"))
SQLITE_LOCK_RETRY_DELAYS_SECONDS = (0.1, 0.25, 0.5, 1.0, 2.0)


def _is_sqlite_lock_error(error: BaseException) -> bool:
    message = str(error).lower()
    return isinstance(error, sqlite3.OperationalError) and ("database is locked" in message or "database is busy" in message)


class _RetryConnection(sqlite3.Connection):
    """SQLite connection with bounded retries for transient writer contention."""

    def _retry(self, operation: Any, operation_name: str) -> Any:
        for attempt, delay in enumerate((0.0, *SQLITE_LOCK_RETRY_DELAYS_SECONDS), start=1):
            if delay:
                time.sleep(delay)
            try:
                return operation()
            except sqlite3.OperationalError as error:
                if not _is_sqlite_lock_error(error) or attempt >= len(SQLITE_LOCK_RETRY_DELAYS_SECONDS) + 1:
                    raise
                LOGGER.warning(
                    "sqlite lock retry operation=%s attempt=%s delay_ms=%.0f error=%s",
                    operation_name,
                    attempt,
                    delay * 1000,
                    error,
                )
        raise RuntimeError("unreachable sqlite retry state")

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return self._retry(lambda: sqlite3.Connection.execute(self, sql, parameters), "execute")

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        return self._retry(lambda: sqlite3.Connection.executemany(self, sql, parameters), "executemany")

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        return self._retry(lambda: sqlite3.Connection.executescript(self, sql_script), "executescript")

    def commit(self) -> None:
        return self._retry(lambda: sqlite3.Connection.commit(self), "commit")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        from backend.app.t1_candidate_store import init_candidate_schema
        init_candidate_schema(self)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=max(1.0, SQLITE_BUSY_TIMEOUT_SECONDS),
            factory=_RetryConnection,
        )
        connection.row_factory = sqlite3.Row
        # busy_timeout applies to lock waits inside SQLite; the connection wrapper
        # below handles the remaining short-lived contention after that wait.
        connection.execute(f"PRAGMA busy_timeout={int(max(1.0, SQLITE_BUSY_TIMEOUT_SECONDS) * 1000)}")
        # synchronous is connection-scoped, so apply it to every connection as
        # well as during schema initialization.
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _init_schema(self) -> None:
        with self.connect() as connection:
            # Configure WAL once during schema initialization. Do not toggle the
            # journal mode on every connection, which itself can contend on locks.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                  code TEXT PRIMARY KEY,
                  name TEXT NOT NULL DEFAULT '',
                  note TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                  key TEXT PRIMARY KEY,
                  value_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stock_analysis_snapshots (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  code TEXT NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  analyzed_at TEXT NOT NULL,
                  analysis_date TEXT NOT NULL,
                  risk_mode TEXT NOT NULL DEFAULT 'balanced',
                  scan_time TEXT NOT NULL DEFAULT '',
                  market_source TEXT NOT NULL DEFAULT '',
                  ai_mode TEXT NOT NULL DEFAULT 'rules',
                  ai_provider TEXT NOT NULL DEFAULT '',
                  ai_model TEXT NOT NULL DEFAULT '',
                  decision TEXT NOT NULL DEFAULT 'watch',
                  primary_horizon TEXT NOT NULL DEFAULT 'none',
                  confidence INTEGER NOT NULL DEFAULT 0,
                  evidence_pack_json TEXT NOT NULL,
                  decision_json TEXT NOT NULL,
                  rule_fallback_used INTEGER NOT NULL DEFAULT 0,
                  case_memory_status_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_stock_analysis_snapshots_code_date
                  ON stock_analysis_snapshots(code, analysis_date);
                CREATE INDEX IF NOT EXISTS idx_stock_analysis_snapshots_decision_date
                  ON stock_analysis_snapshots(decision, analysis_date);

                CREATE TABLE IF NOT EXISTS stock_analysis_outcomes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  snapshot_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  recommendation_date TEXT NOT NULL,
                  buy_date TEXT NOT NULL DEFAULT '',
                  buy_price REAL,
                  horizon INTEGER NOT NULL,
                  exit_date TEXT NOT NULL DEFAULT '',
                  exit_price REAL,
                  return_pct REAL,
                  max_drawdown_pct REAL,
                  max_runup_pct REAL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  label TEXT NOT NULL DEFAULT '',
                  reason TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL,
                  UNIQUE(snapshot_id, horizon)
                );

                CREATE INDEX IF NOT EXISTS idx_stock_analysis_outcomes_code_date
                  ON stock_analysis_outcomes(code, recommendation_date);
                CREATE INDEX IF NOT EXISTS idx_stock_analysis_outcomes_label
                  ON stock_analysis_outcomes(label);

                CREATE TABLE IF NOT EXISTS stock_analysis_review_reports (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  review_date TEXT NOT NULL,
                  period_start TEXT NOT NULL DEFAULT '',
                  period_end TEXT NOT NULL DEFAULT '',
                  sample_count INTEGER NOT NULL DEFAULT 0,
                  false_buy_count INTEGER NOT NULL DEFAULT 0,
                  missed_opportunity_count INTEGER NOT NULL DEFAULT 0,
                  ai_provider TEXT NOT NULL DEFAULT '',
                  ai_model TEXT NOT NULL DEFAULT '',
                  report_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stock_analysis_review_lessons (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  lesson_key TEXT NOT NULL UNIQUE,
                  title TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'draft',
                  pattern_json TEXT NOT NULL DEFAULT '{}',
                  prompt_instruction TEXT NOT NULL DEFAULT '',
                  evidence_json TEXT NOT NULL DEFAULT '[]',
                  source_report_id INTEGER,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stock_analysis_review_lesson_cases (
                  lesson_id INTEGER NOT NULL,
                  snapshot_id INTEGER NOT NULL,
                  label TEXT NOT NULL DEFAULT '',
                  similarity REAL NOT NULL DEFAULT 0,
                  PRIMARY KEY(lesson_id, snapshot_id)
                );

                CREATE TABLE IF NOT EXISTS stock_analysis_snapshots_v2 (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  analysis_version TEXT NOT NULL DEFAULT 'stock_analysis_v2',
                  code TEXT NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  analyzed_at TEXT NOT NULL,
                  analysis_date TEXT NOT NULL,
                  risk_mode TEXT NOT NULL DEFAULT 'balanced',
                  scan_time TEXT NOT NULL DEFAULT '',
                  market_source TEXT NOT NULL DEFAULT '',
                  ai_mode TEXT NOT NULL DEFAULT 'rules',
                  ai_provider TEXT NOT NULL DEFAULT '',
                  ai_model TEXT NOT NULL DEFAULT '',
                  decision TEXT NOT NULL DEFAULT 'watch',
                  primary_horizon TEXT NOT NULL DEFAULT 'none',
                  confidence INTEGER NOT NULL DEFAULT 0,
                  evidence_pack_json TEXT NOT NULL,
                  decision_json TEXT NOT NULL,
                  rule_fallback_used INTEGER NOT NULL DEFAULT 0,
                  case_memory_status_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_stock_analysis_snapshots_v2_code_date
                  ON stock_analysis_snapshots_v2(code, analysis_date);
                CREATE INDEX IF NOT EXISTS idx_stock_analysis_snapshots_v2_decision_date
                  ON stock_analysis_snapshots_v2(decision, analysis_date);

                CREATE TABLE IF NOT EXISTS stock_analysis_outcomes_v2 (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  outcome_version TEXT NOT NULL DEFAULT 'direction_v2',
                  snapshot_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  recommendation_date TEXT NOT NULL,
                  buy_date TEXT NOT NULL DEFAULT '',
                  buy_price REAL,
                  horizon INTEGER NOT NULL,
                  exit_date TEXT NOT NULL DEFAULT '',
                  exit_price REAL,
                  return_pct REAL,
                  max_drawdown_pct REAL,
                  max_runup_pct REAL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  label TEXT NOT NULL DEFAULT '',
                  reason TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL,
                  UNIQUE(snapshot_id, horizon)
                );

                CREATE INDEX IF NOT EXISTS idx_stock_analysis_outcomes_v2_code_date
                  ON stock_analysis_outcomes_v2(code, recommendation_date);
                CREATE INDEX IF NOT EXISTS idx_stock_analysis_outcomes_v2_label
                  ON stock_analysis_outcomes_v2(label);

                CREATE TABLE IF NOT EXISTS stock_analysis_trade_outcomes_v2 (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  trade_outcome_version TEXT NOT NULL DEFAULT 'trade_v2',
                  snapshot_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  recommendation_date TEXT NOT NULL,
                  risk_mode TEXT NOT NULL DEFAULT '',
                  plan_version TEXT NOT NULL DEFAULT '',
                  horizon_type TEXT NOT NULL DEFAULT '',
                  entry_triggered INTEGER NOT NULL DEFAULT 0,
                  entry_date TEXT NOT NULL DEFAULT '',
                  entry_price REAL,
                  entry_reason TEXT NOT NULL DEFAULT '',
                  exit_date TEXT NOT NULL DEFAULT '',
                  exit_price REAL,
                  exit_reason TEXT NOT NULL DEFAULT '',
                  return_pct REAL,
                  max_drawdown_pct REAL,
                  max_runup_pct REAL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  label TEXT NOT NULL DEFAULT '',
                  reason TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL,
                  UNIQUE(snapshot_id, plan_version, horizon_type)
                );

                CREATE INDEX IF NOT EXISTS idx_stock_analysis_trade_outcomes_v2_code_date
                  ON stock_analysis_trade_outcomes_v2(code, recommendation_date);
                CREATE INDEX IF NOT EXISTS idx_stock_analysis_trade_outcomes_v2_label
                  ON stock_analysis_trade_outcomes_v2(label);

                CREATE TABLE IF NOT EXISTS stock_analysis_review_reports_v2 (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  review_version TEXT NOT NULL DEFAULT 'review_v2',
                  review_date TEXT NOT NULL,
                  period_start TEXT NOT NULL DEFAULT '',
                  period_end TEXT NOT NULL DEFAULT '',
                  sample_count INTEGER NOT NULL DEFAULT 0,
                  false_buy_count INTEGER NOT NULL DEFAULT 0,
                  missed_opportunity_count INTEGER NOT NULL DEFAULT 0,
                  ai_provider TEXT NOT NULL DEFAULT '',
                  ai_model TEXT NOT NULL DEFAULT '',
                  report_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stock_analysis_review_lessons_v2 (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  lesson_version TEXT NOT NULL DEFAULT 'review_lesson_v2',
                  lesson_key TEXT NOT NULL UNIQUE,
                  title TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'draft',
                  pattern_json TEXT NOT NULL DEFAULT '{}',
                  prompt_instruction TEXT NOT NULL DEFAULT '',
                  evidence_json TEXT NOT NULL DEFAULT '[]',
                  source_report_id INTEGER,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stock_analysis_review_lesson_cases_v2 (
                  lesson_id INTEGER NOT NULL,
                  snapshot_id INTEGER NOT NULL,
                  label TEXT NOT NULL DEFAULT '',
                  similarity REAL NOT NULL DEFAULT 0,
                  PRIMARY KEY(lesson_id, snapshot_id)
                );

                CREATE TABLE IF NOT EXISTS daily_kline (
                  code TEXT NOT NULL,
                  trade_date TEXT NOT NULL,
                  open REAL,
                  high REAL,
                  low REAL,
                  close REAL,
                  volume REAL,
                  amount REAL,
                  change REAL,
                  turnover REAL,
                  source TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(code, trade_date)
                );

                CREATE INDEX IF NOT EXISTS idx_daily_kline_trade_date
                  ON daily_kline(trade_date);

                CREATE TABLE IF NOT EXISTS ai_full_market_selection_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  profile TEXT NOT NULL,
                  risk_mode TEXT NOT NULL DEFAULT 'balanced',
                  requested_source TEXT NOT NULL DEFAULT 'auto',
                  actual_source TEXT NOT NULL DEFAULT '',
                  scan_time TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'pending',
                  selection_mode TEXT NOT NULL DEFAULT '',
                  total_count INTEGER NOT NULL DEFAULT 0,
                  hard_filtered_count INTEGER NOT NULL DEFAULT 0,
                  pool_size INTEGER NOT NULL DEFAULT 0,
                  batch_size INTEGER NOT NULL DEFAULT 0,
                  batch_pick INTEGER NOT NULL DEFAULT 0,
                  final_limit INTEGER NOT NULL DEFAULT 10,
                  batch_count INTEGER NOT NULL DEFAULT 0,
                  completed_batch_count INTEGER NOT NULL DEFAULT 0,
                  semifinal_count INTEGER NOT NULL DEFAULT 0,
                  final_count INTEGER NOT NULL DEFAULT 0,
                  current_stage TEXT NOT NULL DEFAULT '',
                  final_picks_json TEXT NOT NULL DEFAULT '[]',
                  batch_summary_json TEXT NOT NULL DEFAULT '[]',
                  request_json TEXT NOT NULL DEFAULT '{}',
                  market_json TEXT NOT NULL DEFAULT '{}',
                  error TEXT NOT NULL DEFAULT '',
                  started_at TEXT NOT NULL DEFAULT '',
                  finished_at TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ai_full_market_selection_runs_lookup
                  ON ai_full_market_selection_runs(profile, risk_mode, requested_source, scan_time, status);

                CREATE TABLE IF NOT EXISTS ai_full_market_selection_items (
                  run_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  rule_rank INTEGER NOT NULL DEFAULT 0,
                  ai_rank INTEGER NOT NULL DEFAULT 0,
                  batch_index INTEGER NOT NULL DEFAULT 0,
                  stage TEXT NOT NULL DEFAULT 'pool',
                  selected INTEGER NOT NULL DEFAULT 0,
                  score REAL,
                  confidence REAL,
                  reason TEXT NOT NULL DEFAULT '',
                  risk TEXT NOT NULL DEFAULT '',
                  focus TEXT NOT NULL DEFAULT '',
                  item_json TEXT NOT NULL DEFAULT '{}',
                  pick_json TEXT NOT NULL DEFAULT '{}',
                  PRIMARY KEY(run_id, code)
                );

                CREATE TABLE IF NOT EXISTS ai_full_market_selection_outcomes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  recommendation_date TEXT NOT NULL DEFAULT '',
                  profile TEXT NOT NULL DEFAULT '',
                  risk_mode TEXT NOT NULL DEFAULT 'balanced',
                  selection_mode TEXT NOT NULL DEFAULT '',
                  stage TEXT NOT NULL DEFAULT '',
                  selected INTEGER NOT NULL DEFAULT 0,
                  candidate_type TEXT NOT NULL DEFAULT '',
                  rule_rank INTEGER NOT NULL DEFAULT 0,
                  ai_rank INTEGER NOT NULL DEFAULT 0,
                  horizon INTEGER NOT NULL DEFAULT 1,
                  buy_price REAL,
                  exit_date TEXT NOT NULL DEFAULT '',
                  exit_price REAL,
                  return_pct REAL,
                  benchmark_return_pct REAL,
                  excess_return_pct REAL,
                  max_drawdown_pct REAL,
                  max_runup_pct REAL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  label TEXT NOT NULL DEFAULT '',
                  reason TEXT NOT NULL DEFAULT '',
                  market_state TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL,
                  outcome_json TEXT NOT NULL DEFAULT '{}',
                  UNIQUE(run_id, code, horizon)
                );

                CREATE INDEX IF NOT EXISTS idx_ai_full_market_selection_outcomes_date
                  ON ai_full_market_selection_outcomes(recommendation_date, horizon, label);
                CREATE INDEX IF NOT EXISTS idx_ai_full_market_selection_outcomes_type
                  ON ai_full_market_selection_outcomes(candidate_type, selected, horizon);

                CREATE TABLE IF NOT EXISTS ai_full_market_selection_optimization_reports (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  report_date TEXT NOT NULL,
                  profile TEXT NOT NULL DEFAULT '',
                  risk_mode TEXT NOT NULL DEFAULT '',
                  sample_count INTEGER NOT NULL DEFAULT 0,
                  selected_count INTEGER NOT NULL DEFAULT 0,
                  rejected_count INTEGER NOT NULL DEFAULT 0,
                  prompt_version TEXT NOT NULL DEFAULT '',
                  config_json TEXT NOT NULL DEFAULT '{}',
                  metrics_json TEXT NOT NULL DEFAULT '{}',
                  suggestion_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_full_market_sector_snapshots (
                  run_id INTEGER NOT NULL,
                  sector_type TEXT NOT NULL,
                  sector_code TEXT NOT NULL,
                  sector_name TEXT NOT NULL DEFAULT '',
                  sector_rank INTEGER NOT NULL DEFAULT 0,
                  sector_state TEXT NOT NULL DEFAULT 'data_insufficient',
                  sector_risk_score REAL,
                  sector_confidence REAL,
                  change_pct REAL,
                  up_ratio_pct REAL,
                  limit_up_count INTEGER NOT NULL DEFAULT 0,
                  turnover_amount REAL,
                  main_net_inflow REAL,
                  fund_flow_status TEXT NOT NULL DEFAULT '',
                  data_quality TEXT NOT NULL DEFAULT '',
                  trade_date TEXT NOT NULL DEFAULT '',
                  as_of_time TEXT NOT NULL DEFAULT '',
                  source TEXT NOT NULL DEFAULT '',
                  snapshot_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(run_id, sector_type, sector_code)
                );

                CREATE INDEX IF NOT EXISTS idx_ai_full_market_sector_snapshots_state
                  ON ai_full_market_sector_snapshots(run_id, sector_state, sector_type);

                CREATE TABLE IF NOT EXISTS ai_full_market_stock_sector_snapshots (
                  run_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  listing_board TEXT NOT NULL DEFAULT '',
                  industry_sector TEXT NOT NULL DEFAULT '',
                  concept_sectors_json TEXT NOT NULL DEFAULT '[]',
                  sector_evidence_status TEXT NOT NULL DEFAULT 'unavailable',
                  primary_sector_state TEXT NOT NULL DEFAULT 'data_insufficient',
                  sector_exception_eligible INTEGER NOT NULL DEFAULT 0,
                  sector_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(run_id, code)
                );

                CREATE INDEX IF NOT EXISTS idx_ai_full_market_stock_sector_snapshots_state
                  ON ai_full_market_stock_sector_snapshots(run_id, primary_sector_state);

                CREATE TABLE IF NOT EXISTS ai_full_market_style_snapshots (
                  run_id INTEGER PRIMARY KEY,
                  market_risk TEXT NOT NULL DEFAULT '',
                  style_regime TEXT NOT NULL DEFAULT '',
                  confidence REAL,
                  data_quality TEXT NOT NULL DEFAULT '',
                  trade_date TEXT NOT NULL DEFAULT '',
                  as_of_time TEXT NOT NULL DEFAULT '',
                  source TEXT NOT NULL DEFAULT '',
                  snapshot_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS morning_sector_radar_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  trade_date TEXT NOT NULL,
                  scan_time TEXT NOT NULL DEFAULT '',
                  sector_type TEXT NOT NULL DEFAULT 'all',
                  requested_source TEXT NOT NULL DEFAULT 'auto',
                  actual_source TEXT NOT NULL DEFAULT '',
                  market_source TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'pending',
                  selection_mode TEXT NOT NULL DEFAULT '',
                  rule_fallback_used INTEGER NOT NULL DEFAULT 0,
                  ai_status TEXT NOT NULL DEFAULT '',
                  ai_model TEXT NOT NULL DEFAULT '',
                  prompt_version TEXT NOT NULL DEFAULT '',
                  ai_latency_ms INTEGER NOT NULL DEFAULT 0,
                  sample_role TEXT NOT NULL DEFAULT '',
                  current_stage TEXT NOT NULL DEFAULT '',
                  sector_count INTEGER NOT NULL DEFAULT 0,
                  rule_candidate_count INTEGER NOT NULL DEFAULT 0,
                  qualified_count INTEGER NOT NULL DEFAULT 0,
                  ai_final_count INTEGER NOT NULL DEFAULT 0,
                  overheated_count INTEGER NOT NULL DEFAULT 0,
                  data_quality_status TEXT NOT NULL DEFAULT '',
                  request_json TEXT NOT NULL DEFAULT '{}',
                  market_json TEXT NOT NULL DEFAULT '{}',
                  source_meta_json TEXT NOT NULL DEFAULT '{}',
                  ai_response_json TEXT NOT NULL DEFAULT '{}',
                  ai_error TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  started_at TEXT NOT NULL DEFAULT '',
                  finished_at TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_morning_sector_radar_runs_lookup
                  ON morning_sector_radar_runs(trade_date, scan_time, sector_type, requested_source, status);

                CREATE TABLE IF NOT EXISTS morning_sector_radar_sectors (
                  run_id INTEGER NOT NULL,
                  sector_code TEXT NOT NULL,
                  sector_name TEXT NOT NULL,
                  sector_type TEXT NOT NULL,
                  rule_rank INTEGER NOT NULL DEFAULT 0,
                  rule_status TEXT NOT NULL DEFAULT 'insufficient',
                  ai_rank INTEGER NOT NULL DEFAULT 0,
                  ai_action TEXT NOT NULL DEFAULT '',
                  ai_status TEXT NOT NULL DEFAULT '',
                  final_rank INTEGER NOT NULL DEFAULT 0,
                  final_status TEXT NOT NULL DEFAULT 'insufficient',
                  selected INTEGER NOT NULL DEFAULT 0,
                  activity_score REAL,
                  ai_confidence REAL,
                  change_pct REAL,
                  breadth_pct REAL,
                  turnover_amount REAL,
                  turnover_activity REAL,
                  main_net_inflow REAL,
                  main_net_inflow_ratio REAL,
                  super_large_net_inflow REAL,
                  super_large_net_inflow_ratio REAL,
                  fund_flow_status TEXT NOT NULL DEFAULT '',
                  limit_up_count INTEGER NOT NULL DEFAULT 0,
                  dimensions_json TEXT NOT NULL DEFAULT '{}',
                  reasons_json TEXT NOT NULL DEFAULT '[]',
                  risks_json TEXT NOT NULL DEFAULT '[]',
                  ai_reason TEXT NOT NULL DEFAULT '',
                  counter_evidence_json TEXT NOT NULL DEFAULT '[]',
                  invalid_conditions_json TEXT NOT NULL DEFAULT '[]',
                  data_quality_json TEXT NOT NULL DEFAULT '{}',
                  snapshot_json TEXT NOT NULL DEFAULT '{}',
                  PRIMARY KEY(run_id, sector_type, sector_code)
                );

                CREATE TABLE IF NOT EXISTS morning_sector_radar_constituents (
                  run_id INTEGER NOT NULL,
                  sector_code TEXT NOT NULL,
                  sector_type TEXT NOT NULL,
                  code TEXT NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  sector_rank INTEGER NOT NULL DEFAULT 0,
                  candidate_role TEXT NOT NULL DEFAULT '',
                  tradeability TEXT NOT NULL DEFAULT '',
                  score REAL,
                  reason TEXT NOT NULL DEFAULT '',
                  risk TEXT NOT NULL DEFAULT '',
                  item_json TEXT NOT NULL DEFAULT '{}',
                  PRIMARY KEY(run_id, sector_type, sector_code, code)
                );

                CREATE TABLE IF NOT EXISTS morning_sector_radar_outcomes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id INTEGER NOT NULL,
                  sector_code TEXT NOT NULL,
                  sector_name TEXT NOT NULL DEFAULT '',
                  sector_type TEXT NOT NULL,
                  trade_date TEXT NOT NULL,
                  scan_time TEXT NOT NULL,
                  sample_role TEXT NOT NULL DEFAULT '',
                  result_type TEXT NOT NULL,
                  rule_rank INTEGER NOT NULL DEFAULT 0,
                  ai_rank INTEGER NOT NULL DEFAULT 0,
                  ai_action TEXT NOT NULL DEFAULT '',
                  selected INTEGER NOT NULL DEFAULT 0,
                  reference_value REAL,
                  close_value REAL,
                  next_open_value REAL,
                  close_return_pct REAL,
                  next_open_return_pct REAL,
                  benchmark_close_return_pct REAL,
                  benchmark_next_open_return_pct REAL,
                  excess_close_return_pct REAL,
                  excess_next_open_return_pct REAL,
                  max_runup_pct REAL,
                  max_drawdown_pct REAL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  label TEXT NOT NULL DEFAULT '',
                  reason TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL,
                  outcome_json TEXT NOT NULL DEFAULT '{}',
                  UNIQUE(run_id, sector_type, sector_code, result_type)
                );

                CREATE TABLE IF NOT EXISTS t1_overnight_selection_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  recommendation_date TEXT NOT NULL,
                  scan_time TEXT NOT NULL DEFAULT '',
                  risk_mode TEXT NOT NULL DEFAULT 'balanced',
                  requested_source TEXT NOT NULL DEFAULT 'auto',
                  actual_source TEXT NOT NULL DEFAULT '',
                  evidence_run_id INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'pending',
                  selection_mode TEXT NOT NULL DEFAULT '',
                  total_count INTEGER NOT NULL DEFAULT 0,
                  hard_filtered_count INTEGER NOT NULL DEFAULT 0,
                  pool_size INTEGER NOT NULL DEFAULT 0,
                  batch_size INTEGER NOT NULL DEFAULT 0,
                  batch_pick INTEGER NOT NULL DEFAULT 0,
                  final_limit INTEGER NOT NULL DEFAULT 3,
                  batch_count INTEGER NOT NULL DEFAULT 0,
                  completed_batch_count INTEGER NOT NULL DEFAULT 0,
                  semifinal_count INTEGER NOT NULL DEFAULT 0,
                  final_count INTEGER NOT NULL DEFAULT 0,
                  current_stage TEXT NOT NULL DEFAULT '',
                  final_picks_json TEXT NOT NULL DEFAULT '[]',
                  batch_summary_json TEXT NOT NULL DEFAULT '[]',
                  request_json TEXT NOT NULL DEFAULT '{}',
                  market_json TEXT NOT NULL DEFAULT '{}',
                  error TEXT NOT NULL DEFAULT '',
                  started_at TEXT NOT NULL DEFAULT '',
                  finished_at TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_t1_overnight_selection_runs_lookup
                  ON t1_overnight_selection_runs(recommendation_date, risk_mode, requested_source, status);

                CREATE TABLE IF NOT EXISTS t1_overnight_evidence_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  recommendation_date TEXT NOT NULL,
                  scan_time TEXT NOT NULL DEFAULT '',
                  requested_source TEXT NOT NULL DEFAULT 'auto',
                  actual_source TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'pending',
                  market_source TEXT NOT NULL DEFAULT '',
                  sector_source TEXT NOT NULL DEFAULT '',
                  minute_source TEXT NOT NULL DEFAULT '',
                  market_snapshot_count INTEGER NOT NULL DEFAULT 0,
                  sector_count INTEGER NOT NULL DEFAULT 0,
                  constituent_count INTEGER NOT NULL DEFAULT 0,
                  stock_map_count INTEGER NOT NULL DEFAULT 0,
                  minute_count INTEGER NOT NULL DEFAULT 0,
                  quality_status TEXT NOT NULL DEFAULT '',
                  missing_json TEXT NOT NULL DEFAULT '[]',
                  request_json TEXT NOT NULL DEFAULT '{}',
                  market_json TEXT NOT NULL DEFAULT '{}',
                  error TEXT NOT NULL DEFAULT '',
                  started_at TEXT NOT NULL DEFAULT '',
                  finished_at TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_t1_overnight_evidence_runs_lookup
                  ON t1_overnight_evidence_runs(recommendation_date, scan_time, requested_source, status);

                CREATE TABLE IF NOT EXISTS t1_overnight_market_snapshots (
                  run_id INTEGER NOT NULL,
                  snapshot_time TEXT NOT NULL DEFAULT '',
                  source TEXT NOT NULL DEFAULT '',
                  snapshot_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(run_id, snapshot_time)
                );

                CREATE TABLE IF NOT EXISTS t1_overnight_sector_snapshots (
                  run_id INTEGER NOT NULL,
                  sector_type TEXT NOT NULL,
                  sector_code TEXT NOT NULL,
                  sector_name TEXT NOT NULL DEFAULT '',
                  sector_rank INTEGER NOT NULL DEFAULT 0,
                  change_pct REAL,
                  turnover_amount REAL,
                  main_net_inflow REAL,
                  fund_flow_status TEXT NOT NULL DEFAULT '',
                  limit_up_count INTEGER NOT NULL DEFAULT 0,
                  up_count INTEGER NOT NULL DEFAULT 0,
                  down_count INTEGER NOT NULL DEFAULT 0,
                  sector_state TEXT NOT NULL DEFAULT 'data_insufficient',
                  sector_risk_score REAL,
                  sector_confidence REAL,
                  trade_date TEXT NOT NULL DEFAULT '',
                  as_of_time TEXT NOT NULL DEFAULT '',
                  snapshot_scope TEXT NOT NULL DEFAULT '',
                  sector_state_version TEXT NOT NULL DEFAULT '',
                  snapshot_json TEXT NOT NULL DEFAULT '{}',
                  PRIMARY KEY(run_id, sector_type, sector_code)
                );

                CREATE TABLE IF NOT EXISTS t1_overnight_sector_constituents (
                  run_id INTEGER NOT NULL,
                  sector_type TEXT NOT NULL,
                  sector_code TEXT NOT NULL,
                  code TEXT NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  stock_rank INTEGER NOT NULL DEFAULT 0,
                  change_pct REAL,
                  turnover_amount REAL,
                  turnover_rate REAL,
                  volume_ratio REAL,
                  main_net_inflow REAL,
                  limit_status TEXT NOT NULL DEFAULT '',
                  constituent_json TEXT NOT NULL DEFAULT '{}',
                  PRIMARY KEY(run_id, sector_type, sector_code, code)
                );

                CREATE INDEX IF NOT EXISTS idx_t1_overnight_sector_constituents_code
                  ON t1_overnight_sector_constituents(run_id, code);

                CREATE TABLE IF NOT EXISTS t1_overnight_stock_sector_map (
                  run_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  listing_board TEXT NOT NULL DEFAULT '',
                  industry_sector TEXT NOT NULL DEFAULT '',
                  concept_sectors_json TEXT NOT NULL DEFAULT '[]',
                  sector_evidence_status TEXT NOT NULL DEFAULT 'unavailable',
                  evidence_json TEXT NOT NULL DEFAULT '{}',
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(run_id, code)
                );

                CREATE TABLE IF NOT EXISTS t1_stock_sector_identity_cache (
                  code TEXT PRIMARY KEY,
                  name TEXT NOT NULL DEFAULT '',
                  industry_sector TEXT NOT NULL DEFAULT '',
                  concept_sectors_json TEXT NOT NULL DEFAULT '[]',
                  region_sector TEXT NOT NULL DEFAULT '',
                  source TEXT NOT NULL DEFAULT '',
                  confidence REAL,
                  status TEXT NOT NULL DEFAULT 'available',
                  last_success_at TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL,
                  raw_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_t1_stock_sector_identity_cache_updated
                  ON t1_stock_sector_identity_cache(updated_at);

                CREATE TABLE IF NOT EXISTS t1_overnight_minute_snapshots (
                  run_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  period TEXT NOT NULL DEFAULT '15m',
                  source TEXT NOT NULL DEFAULT '',
                  point_count INTEGER NOT NULL DEFAULT 0,
                  quality_status TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  series_json TEXT NOT NULL DEFAULT '[]',
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(run_id, code, period)
                );

                CREATE TABLE IF NOT EXISTS t1_overnight_selection_items (
                  run_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  pool_rank INTEGER NOT NULL DEFAULT 0,
                  ai_rank INTEGER NOT NULL DEFAULT 0,
                  batch_index INTEGER NOT NULL DEFAULT 0,
                  stage TEXT NOT NULL DEFAULT 'pool',
                  selected INTEGER NOT NULL DEFAULT 0,
                  candidate_type TEXT NOT NULL DEFAULT '',
                  score REAL,
                  confidence REAL,
                  reason TEXT NOT NULL DEFAULT '',
                  buy_plan TEXT NOT NULL DEFAULT '',
                  sell_plan TEXT NOT NULL DEFAULT '',
                  risk TEXT NOT NULL DEFAULT '',
                  invalid_condition TEXT NOT NULL DEFAULT '',
                  item_json TEXT NOT NULL DEFAULT '{}',
                  pick_json TEXT NOT NULL DEFAULT '{}',
                  PRIMARY KEY(run_id, code)
                );

                CREATE TABLE IF NOT EXISTS t1_overnight_selection_outcomes (
                  run_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  recommendation_date TEXT NOT NULL DEFAULT '',
                  next_trade_date TEXT NOT NULL DEFAULT '',
                  buy_reference_price REAL,
                  next_open REAL,
                  next_high REAL,
                  next_low REAL,
                  next_close REAL,
                  open_return_pct REAL,
                  high_return_pct REAL,
                  close_return_pct REAL,
                  max_drawdown_pct REAL,
                  benchmark_return_pct REAL,
                  excess_return_pct REAL,
                  outcome_label TEXT NOT NULL DEFAULT '',
                  outcome_score REAL,
                  hit_sell_plan INTEGER NOT NULL DEFAULT 0,
                  hit_invalid_condition INTEGER NOT NULL DEFAULT 0,
                  failure_reason TEXT NOT NULL DEFAULT '',
                  verified_at TEXT NOT NULL DEFAULT '',
                  data_source TEXT NOT NULL DEFAULT '',
                  outcome_json TEXT NOT NULL DEFAULT '{}',
                  PRIMARY KEY(run_id, code)
                );

                CREATE INDEX IF NOT EXISTS idx_t1_overnight_selection_outcomes_date
                  ON t1_overnight_selection_outcomes(recommendation_date, next_trade_date);

                CREATE TABLE IF NOT EXISTS t1_overnight_validation_scenarios (
                  run_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  validation_version TEXT NOT NULL DEFAULT 'v2_tradeable',
                  scenario_key TEXT NOT NULL DEFAULT '',
                  scenario_label TEXT NOT NULL DEFAULT '',
                  sell_time TEXT NOT NULL DEFAULT '',
                  sell_price REAL,
                  gross_return_pct REAL,
                  net_return_pct REAL,
                  data_status TEXT NOT NULL DEFAULT '',
                  scenario_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(run_id, code, validation_version, scenario_key)
                );

                CREATE INDEX IF NOT EXISTS idx_t1_overnight_validation_scenarios_lookup
                  ON t1_overnight_validation_scenarios(run_id, validation_version, scenario_key);

                CREATE TABLE IF NOT EXISTS t1_overnight_validation_minute_snapshots (
                  run_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  target_trade_date TEXT NOT NULL,
                  period TEXT NOT NULL DEFAULT '1m',
                  source TEXT NOT NULL DEFAULT '',
                  point_count INTEGER NOT NULL DEFAULT 0,
                  quality_status TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  series_json TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(run_id, code, target_trade_date, period)
                );

                CREATE INDEX IF NOT EXISTS idx_t1_overnight_validation_minute_snapshots_lookup
                  ON t1_overnight_validation_minute_snapshots(run_id, target_trade_date, quality_status);

                CREATE TABLE IF NOT EXISTS t1_overnight_strategy_feedback (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  feedback_date TEXT NOT NULL,
                  window_runs INTEGER NOT NULL DEFAULT 0,
                  verified_outcomes INTEGER NOT NULL DEFAULT 0,
                  success_rate REAL,
                  strong_success_rate REAL,
                  low_open_fail_rate REAL,
                  pullback_rate REAL,
                  avg_high_return_pct REAL,
                  avg_close_return_pct REAL,
                  avg_max_drawdown_pct REAL,
                  best_patterns_json TEXT NOT NULL DEFAULT '[]',
                  worst_patterns_json TEXT NOT NULL DEFAULT '[]',
                  prompt_summary TEXT NOT NULL DEFAULT '',
                  source_run_id INTEGER,
                  trigger_source TEXT NOT NULL DEFAULT '',
                  feedback_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_t1_overnight_strategy_feedback_date
                  ON t1_overnight_strategy_feedback(feedback_date, id);

                CREATE TABLE IF NOT EXISTS t1_overnight_strategy_suggestions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  feedback_id INTEGER NOT NULL DEFAULT 0,
                  pattern_key TEXT NOT NULL DEFAULT '',
                  pattern_label TEXT NOT NULL DEFAULT '',
                  market_state TEXT NOT NULL DEFAULT '',
                  candidate_type TEXT NOT NULL DEFAULT '',
                  risk_profile TEXT NOT NULL DEFAULT '',
                  sample_count INTEGER NOT NULL DEFAULT 0,
                  success_rate REAL,
                  strong_success_rate REAL,
                  low_open_fail_rate REAL,
                  pullback_rate REAL,
                  avg_net_return_pct REAL,
                  avg_max_drawdown_pct REAL,
                  suggestion_type TEXT NOT NULL DEFAULT '',
                  suggestion_action TEXT NOT NULL DEFAULT '',
                  suggestion_reason TEXT NOT NULL DEFAULT '',
                  confidence REAL,
                  status TEXT NOT NULL DEFAULT 'draft',
                  reviewer_note TEXT NOT NULL DEFAULT '',
                  suggestion_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  activated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_t1_overnight_strategy_suggestions_status
                  ON t1_overnight_strategy_suggestions(status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_t1_overnight_strategy_suggestions_feedback
                  ON t1_overnight_strategy_suggestions(feedback_id, id);

                CREATE TABLE IF NOT EXISTS intraday_entry_audit_snapshots (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  audit_version TEXT NOT NULL DEFAULT 'entry_audit_v1',
                  code TEXT NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  trade_date TEXT NOT NULL,
                  audited_at TEXT NOT NULL,
                  trigger_source TEXT NOT NULL DEFAULT '',
                  trigger_run_id INTEGER,
                  trigger_item_id TEXT NOT NULL DEFAULT '',
                  position_state TEXT NOT NULL DEFAULT 'none',
                  position_json TEXT NOT NULL DEFAULT '{}',
                  input_json TEXT NOT NULL,
                  constraint_check_json TEXT NOT NULL DEFAULT '{}',
                  ai_audit_json TEXT NOT NULL DEFAULT '{}',
                  final_audit_json TEXT NOT NULL,
                  entry_status TEXT NOT NULL DEFAULT '',
                  position_level TEXT NOT NULL DEFAULT '',
                  position_pct REAL,
                  position_action TEXT NOT NULL DEFAULT '',
                  ai_status TEXT NOT NULL DEFAULT 'not_started',
                  ai_provider TEXT NOT NULL DEFAULT '',
                  ai_model TEXT NOT NULL DEFAULT '',
                  ai_failure_reason TEXT NOT NULL DEFAULT '',
                  fallback_reason TEXT NOT NULL DEFAULT '',
                  data_quality_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_entry_audit_snapshots_code_date
                  ON intraday_entry_audit_snapshots(code, trade_date);

                CREATE INDEX IF NOT EXISTS idx_entry_audit_snapshots_status_date
                  ON intraday_entry_audit_snapshots(entry_status, trade_date);

                CREATE INDEX IF NOT EXISTS idx_entry_audit_snapshots_trigger
                  ON intraday_entry_audit_snapshots(trigger_source, trigger_run_id);

                CREATE TABLE IF NOT EXISTS intraday_entry_audit_outcomes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  snapshot_id INTEGER NOT NULL,
                  code TEXT NOT NULL,
                  trade_date TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  entry_triggered INTEGER NOT NULL DEFAULT 0,
                  entry_date TEXT NOT NULL DEFAULT '',
                  entry_price REAL,
                  exit_date TEXT NOT NULL DEFAULT '',
                  exit_price REAL,
                  return_pct REAL,
                  max_drawdown_pct REAL,
                  max_runup_pct REAL,
                  hit_invalid_condition INTEGER NOT NULL DEFAULT 0,
                  hit_trigger_condition INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'pending',
                  label TEXT NOT NULL DEFAULT '',
                  reason TEXT NOT NULL DEFAULT '',
                  outcome_json TEXT NOT NULL DEFAULT '{}',
                  updated_at TEXT NOT NULL,
                  UNIQUE(snapshot_id, horizon)
                );

                CREATE INDEX IF NOT EXISTS idx_entry_audit_outcomes_code_date
                  ON intraday_entry_audit_outcomes(code, trade_date);

                CREATE INDEX IF NOT EXISTS idx_entry_audit_outcomes_status
                  ON intraday_entry_audit_outcomes(status, horizon);

                CREATE TABLE IF NOT EXISTS intraday_entry_audit_feedback (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  snapshot_id INTEGER NOT NULL UNIQUE,
                  code TEXT NOT NULL,
                  trade_date TEXT NOT NULL,
                  user_action TEXT NOT NULL DEFAULT '',
                  actual_buy_date TEXT NOT NULL DEFAULT '',
                  actual_buy_price REAL,
                  actual_buy_pct REAL,
                  actual_sell_date TEXT NOT NULL DEFAULT '',
                  actual_sell_price REAL,
                  note TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_entry_audit_feedback_action
                  ON intraday_entry_audit_feedback(user_action, trade_date);
                """
            )
            self._ensure_column(connection, "t1_overnight_selection_runs", "evidence_run_id", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "t1_overnight_sector_snapshots", "sector_state", "TEXT NOT NULL DEFAULT 'data_insufficient'")
            self._ensure_column(connection, "t1_overnight_sector_snapshots", "sector_risk_score", "REAL")
            self._ensure_column(connection, "t1_overnight_sector_snapshots", "sector_confidence", "REAL")
            self._ensure_column(connection, "t1_overnight_sector_snapshots", "trade_date", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "t1_overnight_sector_snapshots", "as_of_time", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "t1_overnight_sector_snapshots", "snapshot_scope", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "t1_overnight_sector_snapshots", "sector_state_version", "TEXT NOT NULL DEFAULT ''")
            for table in ("t1_overnight_selection_runs", "t1_overnight_evidence_runs"):
                self._ensure_column(connection, table, "heartbeat_at", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(connection, table, "lease_expires_at", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(connection, table, "worker_id", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(connection, table, "last_stage_at", "TEXT NOT NULL DEFAULT ''")
                connection.execute(
                    f"""
                    UPDATE {table}
                    SET heartbeat_at = CASE WHEN heartbeat_at = '' THEN updated_at ELSE heartbeat_at END,
                        last_stage_at = CASE WHEN last_stage_at = '' THEN updated_at ELSE last_stage_at END,
                        lease_expires_at = CASE
                          WHEN lease_expires_at = '' AND status IN ('pending', 'running')
                          THEN replace(datetime(updated_at, '+15 minutes'), ' ', 'T')
                          ELSE lease_expires_at
                        END
                    WHERE heartbeat_at = '' OR last_stage_at = '' OR lease_expires_at = ''
                    """
                )

    def _ensure_column(self, connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if column in existing:
            return
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def list_watchlist(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT code, name, note, created_at FROM watchlist ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def add_watchlist(self, code: str, name: str = "", note: str = "") -> Dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO watchlist(code, name, note, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET name=excluded.name, note=excluded.note
                """,
                (code, name, note, now),
            )
        return {"code": code, "name": name, "note": note, "created_at": now}

    def remove_watchlist(self, code: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM watchlist WHERE code = ?", (code,))

    def get_app_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value_json FROM app_settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return default

    def set_app_setting(self, key: str, value: Any) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), now),
            )

    def upsert_daily_kline_series(self, code: str, series: List[Dict[str, Any]], source: str = "") -> int:
        normalized_code = str(code or "").strip().zfill(6)
        if not normalized_code or not series:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for point in series:
            trade_date = _normalize_date(point.get("date"))
            if not trade_date:
                continue
            rows.append(
                (
                    normalized_code,
                    trade_date,
                    _optional_float(point.get("open")),
                    _optional_float(point.get("high")),
                    _optional_float(point.get("low")),
                    _optional_float(point.get("close")),
                    _optional_float(point.get("volume")),
                    _optional_float(point.get("amount")),
                    _optional_float(point.get("change")),
                    _optional_float(point.get("turnover")),
                    source,
                    now,
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO daily_kline(
                  code, trade_date, open, high, low, close, volume, amount, change, turnover, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, trade_date) DO UPDATE SET
                  open=excluded.open,
                  high=excluded.high,
                  low=excluded.low,
                  close=excluded.close,
                  volume=excluded.volume,
                  amount=excluded.amount,
                  change=excluded.change,
                  turnover=excluded.turnover,
                  source=excluded.source,
                  updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def load_daily_kline_series(self, codes: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        normalized = sorted({str(code or "").strip().zfill(6) for code in codes if str(code or "").strip()})
        if not normalized:
            return {}
        result: Dict[str, List[Dict[str, Any]]] = {code: [] for code in normalized}
        with self.connect() as connection:
            for chunk in _chunks(normalized, 500):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT code, trade_date, open, high, low, close, volume, amount, change, turnover
                    FROM daily_kline
                    WHERE code IN ({placeholders})
                    ORDER BY code, trade_date
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    result[row["code"]].append(
                        {
                            "date": row["trade_date"],
                            "open": row["open"],
                            "high": row["high"],
                            "low": row["low"],
                            "close": row["close"],
                            "volume": row["volume"],
                            "amount": row["amount"],
                            "change": row["change"],
                            "turnover": row["turnover"],
                        }
                    )
        return {code: series for code, series in result.items() if series}

    def create_ai_full_market_selection_run(self, run: Dict[str, Any], pool_items: List[Dict[str, Any]]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO ai_full_market_selection_runs(
                  profile, risk_mode, requested_source, actual_source, scan_time, status, selection_mode,
                  total_count, hard_filtered_count, pool_size, batch_size, batch_pick, final_limit,
                  batch_count, request_json, market_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run.get("profile") or ""),
                    str(run.get("riskMode") or "balanced"),
                    str(run.get("requestedSource") or "auto"),
                    str(run.get("actualSource") or ""),
                    str(run.get("scanTime") or ""),
                    str(run.get("status") or "pending"),
                    str(run.get("selectionMode") or ""),
                    int(run.get("totalCount") or 0),
                    int(run.get("hardFilteredCount") or 0),
                    int(run.get("poolSize") or len(pool_items)),
                    int(run.get("batchSize") or 80),
                    int(run.get("batchPick") or 10),
                    int(run.get("finalLimit") or 10),
                    int(run.get("batchCount") or 0),
                    json.dumps(run.get("request") or {}, ensure_ascii=False),
                    json.dumps(run.get("market") or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO ai_full_market_selection_items(
                  run_id, code, name, rule_rank, batch_index, stage, score, item_json
                )
                VALUES (?, ?, ?, ?, ?, 'pool', ?, ?)
                """,
                [
                    (
                        run_id,
                        str(item.get("code") or "").zfill(6),
                        str(item.get("name") or ""),
                        int(item.get("ruleRank") or item.get("rank") or index),
                        int(item.get("batchIndex") or 0),
                        _optional_float(item.get("score")),
                        json.dumps(item, ensure_ascii=False),
                    )
                    for index, item in enumerate(pool_items, start=1)
                    if isinstance(item, dict) and str(item.get("code") or "").strip()
                ],
            )
        return run_id

    def update_ai_full_market_selection_run(self, run_id: int, updates: Dict[str, Any]) -> None:
        allowed = {
            "status": "status",
            "selectionMode": "selection_mode",
            "actualSource": "actual_source",
            "totalCount": "total_count",
            "hardFilteredCount": "hard_filtered_count",
            "poolSize": "pool_size",
            "batchSize": "batch_size",
            "batchPick": "batch_pick",
            "finalLimit": "final_limit",
            "batchCount": "batch_count",
            "completedBatchCount": "completed_batch_count",
            "semifinalCount": "semifinal_count",
            "finalCount": "final_count",
            "currentStage": "current_stage",
            "finalPicks": "final_picks_json",
            "batchSummary": "batch_summary_json",
            "request": "request_json",
            "market": "market_json",
            "error": "error",
            "startedAt": "started_at",
            "finishedAt": "finished_at",
        }
        sets = []
        params: List[Any] = []
        for key, column in allowed.items():
            if key not in updates:
                continue
            value = updates[key]
            if key in {"finalPicks", "batchSummary"}:
                value = json.dumps(value or [], ensure_ascii=False)
            if key in {"request", "market"}:
                value = json.dumps(value or {}, ensure_ascii=False)
            sets.append(f"{column} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(datetime.now().isoformat(timespec="seconds"))
        params.append(run_id)
        with self.connect() as connection:
            connection.execute(f"UPDATE ai_full_market_selection_runs SET {', '.join(sets)} WHERE id = ?", params)

    def upsert_ai_full_market_selection_items(self, run_id: int, items: List[Dict[str, Any]], stage: str) -> int:
        if not items:
            return 0
        rows = []
        for index, item in enumerate(items, start=1):
            code = str(item.get("code") or "").zfill(6)
            if not code:
                continue
            rows.append(
                (
                    run_id,
                    code,
                    str(item.get("name") or ""),
                    int(item.get("ruleRank") or item.get("rank") or 0),
                    int(item.get("aiRank") or item.get("rank") or index),
                    int(item.get("batchIndex") or 0),
                    stage,
                    1 if item.get("selected", stage != "pool") else 0,
                    _optional_float(item.get("score")),
                    _optional_float(item.get("confidence") if item.get("confidence") is not None else item.get("aiSelectionScore")),
                    str(item.get("reason") or item.get("aiSelectionReason") or ""),
                    str(item.get("risk") or item.get("aiSelectionRisk") or ""),
                    str(item.get("focus") or item.get("aiSelectionFocus") or ""),
                    json.dumps(item.get("item") or item, ensure_ascii=False),
                    json.dumps(item, ensure_ascii=False),
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO ai_full_market_selection_items(
                  run_id, code, name, rule_rank, ai_rank, batch_index, stage, selected,
                  score, confidence, reason, risk, focus, item_json, pick_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, code) DO UPDATE SET
                  name=excluded.name,
                  ai_rank=excluded.ai_rank,
                  batch_index=excluded.batch_index,
                  stage=excluded.stage,
                  selected=excluded.selected,
                  score=excluded.score,
                  confidence=excluded.confidence,
                  reason=excluded.reason,
                  risk=excluded.risk,
                  focus=excluded.focus,
                  pick_json=excluded.pick_json
                """,
                rows,
            )
        return len(rows)

    def upsert_ai_full_market_sector_snapshots(self, run_id: int, sectors: List[Dict[str, Any]]) -> int:
        if not sectors:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for index, sector in enumerate(sectors, start=1):
            sector_type = str(sector.get("sectorType") or "").strip()
            sector_code = str(sector.get("sectorCode") or sector.get("sectorName") or "").strip()
            if sector_type not in {"industry", "concept"} or not sector_code:
                continue
            rows.append(
                (
                    int(run_id),
                    sector_type,
                    sector_code,
                    str(sector.get("sectorName") or ""),
                    int(sector.get("rank") or sector.get("ruleRank") or index),
                    str(sector.get("sectorState") or "data_insufficient"),
                    _optional_float(sector.get("sectorRiskScore")),
                    _optional_float(sector.get("sectorConfidence")),
                    _optional_float(sector.get("changePct")),
                    _optional_float(sector.get("upRatioPct")),
                    int(sector.get("limitUpCount") or 0),
                    _optional_float(sector.get("turnoverAmount")),
                    _optional_float(sector.get("mainNetInflow")),
                    str(sector.get("fundFlowStatus") or ""),
                    str((sector.get("dataQuality") or {}).get("status") or sector.get("dataQuality") or ""),
                    str(sector.get("tradeDate") or ""),
                    str(sector.get("asOfTime") or ""),
                    str(sector.get("source") or ""),
                    json.dumps(sector, ensure_ascii=False),
                    now,
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO ai_full_market_sector_snapshots(
                  run_id, sector_type, sector_code, sector_name, sector_rank,
                  sector_state, sector_risk_score, sector_confidence, change_pct,
                  up_ratio_pct, limit_up_count, turnover_amount, main_net_inflow,
                  fund_flow_status, data_quality, trade_date, as_of_time, source,
                  snapshot_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, sector_type, sector_code) DO UPDATE SET
                  sector_name=excluded.sector_name,
                  sector_rank=excluded.sector_rank,
                  sector_state=excluded.sector_state,
                  sector_risk_score=excluded.sector_risk_score,
                  sector_confidence=excluded.sector_confidence,
                  change_pct=excluded.change_pct,
                  up_ratio_pct=excluded.up_ratio_pct,
                  limit_up_count=excluded.limit_up_count,
                  turnover_amount=excluded.turnover_amount,
                  main_net_inflow=excluded.main_net_inflow,
                  fund_flow_status=excluded.fund_flow_status,
                  data_quality=excluded.data_quality,
                  trade_date=excluded.trade_date,
                  as_of_time=excluded.as_of_time,
                  source=excluded.source,
                  snapshot_json=excluded.snapshot_json
                """,
                rows,
            )
        return len(rows)

    def upsert_ai_full_market_stock_sector_snapshots(self, run_id: int, items: List[Dict[str, Any]]) -> int:
        if not items:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for item in items:
            code = str(item.get("code") or "").zfill(6)
            if not code:
                continue
            rows.append(
                (
                    int(run_id),
                    code,
                    str(item.get("name") or ""),
                    str(item.get("listingBoard") or ""),
                    str(item.get("industrySector") or ""),
                    json.dumps(item.get("conceptSectors") or [], ensure_ascii=False),
                    str(item.get("sectorEvidenceStatus") or "unavailable"),
                    str(item.get("sectorState") or "data_insufficient"),
                    1 if item.get("sectorExceptionEligible") else 0,
                    json.dumps(item.get("sectorEvidence") or item, ensure_ascii=False),
                    now,
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO ai_full_market_stock_sector_snapshots(
                  run_id, code, name, listing_board, industry_sector, concept_sectors_json,
                  sector_evidence_status, primary_sector_state, sector_exception_eligible,
                  sector_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, code) DO UPDATE SET
                  name=excluded.name,
                  listing_board=excluded.listing_board,
                  industry_sector=excluded.industry_sector,
                  concept_sectors_json=excluded.concept_sectors_json,
                  sector_evidence_status=excluded.sector_evidence_status,
                  primary_sector_state=excluded.primary_sector_state,
                  sector_exception_eligible=excluded.sector_exception_eligible,
                  sector_json=excluded.sector_json
                """,
                rows,
            )
        return len(rows)

    def upsert_ai_full_market_style_snapshot(self, run_id: int, style: Dict[str, Any]) -> None:
        if not style:
            return
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_full_market_style_snapshots(
                  run_id, market_risk, style_regime, confidence, data_quality,
                  trade_date, as_of_time, source, snapshot_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  market_risk=excluded.market_risk,
                  style_regime=excluded.style_regime,
                  confidence=excluded.confidence,
                  data_quality=excluded.data_quality,
                  trade_date=excluded.trade_date,
                  as_of_time=excluded.as_of_time,
                  source=excluded.source,
                  snapshot_json=excluded.snapshot_json
                """,
                (
                    int(run_id),
                    str(style.get("marketRisk") or ""),
                    str(style.get("styleRegime") or ""),
                    _optional_float(style.get("confidence")),
                    str(style.get("dataQuality") or ""),
                    str(style.get("tradeDate") or ""),
                    str(style.get("asOfTime") or ""),
                    str(style.get("source") or ""),
                    json.dumps(style, ensure_ascii=False),
                    now,
                ),
            )

    def load_ai_full_market_selection_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM ai_full_market_selection_runs WHERE id = ?", (run_id,)).fetchone()
        return self._ai_full_market_run_from_row(row) if row else None

    def latest_ai_full_market_selection_run(
        self,
        profile: str = "daily",
        risk_mode: str = "balanced",
        requested_source: str = "auto",
        scan_time: str = "",
        created_date: str = "",
    ) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            scan_filter = "AND scan_time = ?" if scan_time else ""
            date_filter = "AND substr(created_at, 1, 10) = ?" if created_date else ""
            params: List[Any] = [profile, risk_mode, requested_source]
            if scan_time:
                params.append(scan_time)
            if created_date:
                params.append(created_date)
            row = connection.execute(
                f"""
                SELECT *
                FROM ai_full_market_selection_runs
                WHERE profile = ? AND risk_mode = ? AND requested_source = ?
                {scan_filter}
                {date_filter}
                ORDER BY id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return self._ai_full_market_run_from_row(row) if row else None

    def load_ai_full_market_selection_items(self, run_id: int) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM ai_full_market_selection_items
                WHERE run_id = ?
                ORDER BY selected DESC, ai_rank, rule_rank, code
                """,
                (run_id,),
            ).fetchall()
        return [self._ai_full_market_item_from_row(row) for row in rows]

    def create_morning_sector_radar_run(self, run: Dict[str, Any]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO morning_sector_radar_runs(
                  trade_date, scan_time, sector_type, requested_source, actual_source, market_source,
                  status, selection_mode, rule_fallback_used, ai_status, ai_model, prompt_version,
                  sample_role, current_stage, request_json, market_json, source_meta_json,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run.get("tradeDate") or date.today().isoformat()),
                    str(run.get("scanTime") or ""),
                    str(run.get("sectorType") or "all"),
                    str(run.get("requestedSource") or "auto"),
                    str(run.get("actualSource") or ""),
                    str(run.get("marketSource") or "auto"),
                    str(run.get("status") or "pending"),
                    str(run.get("selectionMode") or ""),
                    1 if run.get("ruleFallbackUsed") else 0,
                    str(run.get("aiStatus") or ""),
                    str(run.get("aiModel") or ""),
                    str(run.get("promptVersion") or ""),
                    str(run.get("sampleRole") or ""),
                    str(run.get("currentStage") or "pending"),
                    json.dumps(run.get("request") or {}, ensure_ascii=False),
                    json.dumps(run.get("market") or {}, ensure_ascii=False),
                    json.dumps(run.get("sourceMeta") or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return int(cursor.lastrowid)

    def update_morning_sector_radar_run(self, run_id: int, updates: Dict[str, Any]) -> None:
        allowed = {
            "status": "status",
            "selectionMode": "selection_mode",
            "ruleFallbackUsed": "rule_fallback_used",
            "actualSource": "actual_source",
            "marketSource": "market_source",
            "aiStatus": "ai_status",
            "aiModel": "ai_model",
            "promptVersion": "prompt_version",
            "aiLatencyMs": "ai_latency_ms",
            "sampleRole": "sample_role",
            "currentStage": "current_stage",
            "sectorCount": "sector_count",
            "ruleCandidateCount": "rule_candidate_count",
            "qualifiedCount": "qualified_count",
            "aiFinalCount": "ai_final_count",
            "overheatedCount": "overheated_count",
            "dataQualityStatus": "data_quality_status",
            "request": "request_json",
            "market": "market_json",
            "sourceMeta": "source_meta_json",
            "aiResponse": "ai_response_json",
            "aiError": "ai_error",
            "error": "error",
            "startedAt": "started_at",
            "finishedAt": "finished_at",
        }
        sets = []
        params: List[Any] = []
        for key, column in allowed.items():
            if key not in updates:
                continue
            value = updates[key]
            if key in {"request", "market", "sourceMeta", "aiResponse"}:
                value = json.dumps(value or {}, ensure_ascii=False)
            if key == "ruleFallbackUsed":
                value = 1 if value else 0
            sets.append(f"{column} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(datetime.now().isoformat(timespec="seconds"))
        params.append(run_id)
        with self.connect() as connection:
            connection.execute(f"UPDATE morning_sector_radar_runs SET {', '.join(sets)} WHERE id = ?", params)

    def latest_morning_sector_radar_run(self, sector_type: str = "all", scan_time: str = "", requested_source: str = "auto") -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM morning_sector_radar_runs
                WHERE sector_type = ? AND scan_time = ? AND requested_source = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (sector_type, scan_time, requested_source),
            ).fetchone()
        return self._morning_sector_radar_run_from_row(row) if row else None

    def load_morning_sector_radar_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM morning_sector_radar_runs WHERE id = ?", (run_id,)).fetchone()
        return self._morning_sector_radar_run_from_row(row) if row else None

    def upsert_morning_sector_radar_sectors(self, run_id: int, sectors: List[Dict[str, Any]]) -> int:
        rows = []
        for sector in sectors:
            rows.append(
                (
                    run_id,
                    str(sector.get("sectorCode") or ""),
                    str(sector.get("sectorName") or ""),
                    str(sector.get("sectorType") or ""),
                    int(sector.get("ruleRank") or 0),
                    str(sector.get("ruleStatus") or "insufficient"),
                    int(sector.get("aiRank") or 0),
                    str(sector.get("aiAction") or ""),
                    str(sector.get("aiStatus") or ""),
                    int(sector.get("finalRank") or 0),
                    str(sector.get("finalStatus") or sector.get("ruleStatus") or "insufficient"),
                    1 if sector.get("selected") else 0,
                    _optional_float(sector.get("activityScore")),
                    _optional_float(sector.get("aiConfidence")),
                    _optional_float(sector.get("changePct")),
                    _optional_float(sector.get("breadthPct")),
                    _optional_float(sector.get("turnoverAmount")),
                    _optional_float(sector.get("turnoverActivity")),
                    _optional_float(sector.get("mainNetInflow")),
                    _optional_float(sector.get("mainNetInflowRatio")),
                    _optional_float(sector.get("superLargeNetInflow")),
                    _optional_float(sector.get("superLargeNetInflowRatio")),
                    str(sector.get("fundFlowStatus") or ""),
                    int(sector.get("limitUpCount") or 0),
                    json.dumps(sector.get("dimensions") or {}, ensure_ascii=False),
                    json.dumps(sector.get("reasons") or [], ensure_ascii=False),
                    json.dumps((sector.get("aiRisks") or sector.get("risks") or []), ensure_ascii=False),
                    str(sector.get("aiReason") or ""),
                    json.dumps(sector.get("counterEvidence") or [], ensure_ascii=False),
                    json.dumps(sector.get("invalidConditions") or [], ensure_ascii=False),
                    json.dumps(sector.get("dataQuality") or {}, ensure_ascii=False),
                    json.dumps(sector, ensure_ascii=False),
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO morning_sector_radar_sectors(
                  run_id, sector_code, sector_name, sector_type, rule_rank, rule_status,
                  ai_rank, ai_action, ai_status, final_rank, final_status, selected,
                  activity_score, ai_confidence, change_pct, breadth_pct, turnover_amount,
                  turnover_activity, main_net_inflow, main_net_inflow_ratio,
                  super_large_net_inflow, super_large_net_inflow_ratio, fund_flow_status,
                  limit_up_count, dimensions_json, reasons_json, risks_json, ai_reason,
                  counter_evidence_json, invalid_conditions_json, data_quality_json, snapshot_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, sector_type, sector_code) DO UPDATE SET
                  sector_name=excluded.sector_name,
                  rule_rank=excluded.rule_rank,
                  rule_status=excluded.rule_status,
                  ai_rank=excluded.ai_rank,
                  ai_action=excluded.ai_action,
                  ai_status=excluded.ai_status,
                  final_rank=excluded.final_rank,
                  final_status=excluded.final_status,
                  selected=excluded.selected,
                  activity_score=excluded.activity_score,
                  ai_confidence=excluded.ai_confidence,
                  change_pct=excluded.change_pct,
                  breadth_pct=excluded.breadth_pct,
                  turnover_amount=excluded.turnover_amount,
                  turnover_activity=excluded.turnover_activity,
                  main_net_inflow=excluded.main_net_inflow,
                  main_net_inflow_ratio=excluded.main_net_inflow_ratio,
                  super_large_net_inflow=excluded.super_large_net_inflow,
                  super_large_net_inflow_ratio=excluded.super_large_net_inflow_ratio,
                  fund_flow_status=excluded.fund_flow_status,
                  limit_up_count=excluded.limit_up_count,
                  dimensions_json=excluded.dimensions_json,
                  reasons_json=excluded.reasons_json,
                  risks_json=excluded.risks_json,
                  ai_reason=excluded.ai_reason,
                  counter_evidence_json=excluded.counter_evidence_json,
                  invalid_conditions_json=excluded.invalid_conditions_json,
                  data_quality_json=excluded.data_quality_json,
                  snapshot_json=excluded.snapshot_json
                """,
                rows,
            )
        return len(rows)

    def upsert_morning_sector_radar_constituents(self, run_id: int, sectors: List[Dict[str, Any]]) -> int:
        rows = []
        for sector in sectors:
            for index, item in enumerate(sector.get("leaders") or [], start=1):
                code = str(item.get("code") or "").strip().zfill(6)
                if not code:
                    continue
                rows.append(
                    (
                        run_id,
                        str(sector.get("sectorCode") or ""),
                        str(sector.get("sectorType") or ""),
                        code,
                        str(item.get("name") or ""),
                        index,
                        str(item.get("candidateRole") or ""),
                        str(item.get("tradeability") or ""),
                        _optional_float(item.get("score")),
                        str(item.get("reason") or ""),
                        str(item.get("risk") or ""),
                        json.dumps(item, ensure_ascii=False),
                    )
                )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO morning_sector_radar_constituents(
                  run_id, sector_code, sector_type, code, name, sector_rank, candidate_role,
                  tradeability, score, reason, risk, item_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, sector_type, sector_code, code) DO UPDATE SET
                  name=excluded.name,
                  sector_rank=excluded.sector_rank,
                  candidate_role=excluded.candidate_role,
                  tradeability=excluded.tradeability,
                  score=excluded.score,
                  reason=excluded.reason,
                  risk=excluded.risk,
                  item_json=excluded.item_json
                """,
                rows,
            )
        return len(rows)

    def upsert_morning_sector_radar_outcomes(self, run_id: int, run: Dict[str, Any], sectors: List[Dict[str, Any]]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for sector in sectors:
            result_types = ["rule_baseline"]
            if sector.get("selected"):
                result_types.append("ai_reviewed" if run.get("selectionMode") == "ai_reviewed" else "rules_fallback")
            elif sector.get("aiAction") == "reject":
                result_types.append("ai_rejected")
            for result_type in result_types:
                rows.append(
                    (
                        run_id,
                        str(sector.get("sectorCode") or ""),
                        str(sector.get("sectorName") or ""),
                        str(sector.get("sectorType") or ""),
                        str(run.get("tradeDate") or date.today().isoformat()),
                        str(run.get("scanTime") or ""),
                        str(run.get("sampleRole") or ""),
                        result_type,
                        int(sector.get("ruleRank") or 0),
                        int(sector.get("aiRank") or 0),
                        str(sector.get("aiAction") or ""),
                        1 if sector.get("selected") else 0,
                        _optional_float(sector.get("changePct")),
                        "pending",
                        now,
                        json.dumps({"sector": sector}, ensure_ascii=False),
                    )
                )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO morning_sector_radar_outcomes(
                  run_id, sector_code, sector_name, sector_type, trade_date, scan_time,
                  sample_role, result_type, rule_rank, ai_rank, ai_action, selected,
                  reference_value, status, updated_at, outcome_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, sector_type, sector_code, result_type) DO UPDATE SET
                  sector_name=excluded.sector_name,
                  rule_rank=excluded.rule_rank,
                  ai_rank=excluded.ai_rank,
                  ai_action=excluded.ai_action,
                  selected=excluded.selected,
                  reference_value=excluded.reference_value,
                  status=excluded.status,
                  updated_at=excluded.updated_at,
                  outcome_json=excluded.outcome_json
                """,
                rows,
            )
        return len(rows)

    def load_morning_sector_radar_sectors(self, run_id: int) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM morning_sector_radar_sectors
                WHERE run_id = ?
                ORDER BY selected DESC, final_rank, ai_rank, rule_rank
                """,
                (run_id,),
            ).fetchall()
        return [self._morning_sector_radar_sector_from_row(row) for row in rows]

    def load_morning_sector_radar_constituents(self, run_id: int) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM morning_sector_radar_constituents
                WHERE run_id = ?
                ORDER BY sector_rank, score DESC, code
                """,
                (run_id,),
            ).fetchall()
        return [self._morning_sector_radar_constituent_from_row(row) for row in rows]

    def count_morning_sector_radar_outcomes(self, status: str = "", run_id: int | None = None) -> int:
        clauses = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS count FROM morning_sector_radar_outcomes {where}", params).fetchone()
        return int(row["count"] or 0) if row else 0

    def load_morning_sector_radar_pending_outcomes(
        self,
        run_id: int | None = None,
        date_from: str = "",
        date_to: str = "",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        clauses = ["o.status = 'pending'"]
        params: List[Any] = []
        if run_id:
            clauses.append("o.run_id = ?")
            params.append(run_id)
        if date_from:
            clauses.append("o.trade_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("o.trade_date <= ?")
            params.append(date_to)
        params.append(max(1, int(limit or 200)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                  o.*,
                  r.status AS run_status,
                  r.selection_mode,
                  r.rule_fallback_used,
                  r.ai_status AS run_ai_status,
                  r.actual_source,
                  r.data_quality_status,
                  r.source_meta_json,
                  r.market_json
                FROM morning_sector_radar_outcomes o
                JOIN morning_sector_radar_runs r ON r.id = o.run_id
                WHERE {' AND '.join(clauses)}
                ORDER BY o.run_id DESC, o.result_type, o.rule_rank, o.id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._morning_sector_radar_outcome_from_row(row) for row in rows]

    def update_morning_sector_radar_outcome_result(self, outcome_id: int, updates: Dict[str, Any]) -> None:
        allowed = {
            "referenceValue": "reference_value",
            "closeValue": "close_value",
            "nextOpenValue": "next_open_value",
            "closeReturnPct": "close_return_pct",
            "nextOpenReturnPct": "next_open_return_pct",
            "benchmarkCloseReturnPct": "benchmark_close_return_pct",
            "benchmarkNextOpenReturnPct": "benchmark_next_open_return_pct",
            "excessCloseReturnPct": "excess_close_return_pct",
            "excessNextOpenReturnPct": "excess_next_open_return_pct",
            "maxRunupPct": "max_runup_pct",
            "maxDrawdownPct": "max_drawdown_pct",
            "status": "status",
            "label": "label",
            "reason": "reason",
            "outcome": "outcome_json",
        }
        sets = []
        params: List[Any] = []
        for key, column in allowed.items():
            if key not in updates:
                continue
            value = updates[key]
            if key == "outcome":
                value = json.dumps(value or {}, ensure_ascii=False)
            sets.append(f"{column} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(datetime.now().isoformat(timespec="seconds"))
        params.append(outcome_id)
        with self.connect() as connection:
            connection.execute(f"UPDATE morning_sector_radar_outcomes SET {', '.join(sets)} WHERE id = ?", params)

    def morning_sector_radar_diagnostics(self, window_runs: int = 20) -> Dict[str, Any]:
        with self.connect() as connection:
            runs = connection.execute(
                """
                SELECT id, trade_date, scan_time, selection_mode, rule_fallback_used, ai_status,
                       actual_source, data_quality_status, sample_role, prompt_version
                FROM morning_sector_radar_runs
                WHERE status = 'done'
                ORDER BY id DESC
                LIMIT ?
                """,
                (window_runs,),
            ).fetchall()
            run_ids = [int(row["id"]) for row in runs]
            outcomes = connection.execute(
                """
                SELECT result_type, status, COUNT(*) AS count,
                       AVG(close_return_pct) AS avg_close_return_pct,
                       AVG(excess_close_return_pct) AS avg_excess_close_return_pct,
                       AVG(next_open_return_pct) AS avg_next_open_return_pct,
                       AVG(excess_next_open_return_pct) AS avg_excess_next_open_return_pct,
                       AVG(max_drawdown_pct) AS avg_max_drawdown_pct,
                       SUM(CASE WHEN close_return_pct > 0 THEN 1 ELSE 0 END) AS positive_close_count
                FROM morning_sector_radar_outcomes
                WHERE run_id IN (
                  SELECT id FROM morning_sector_radar_runs WHERE status = 'done' ORDER BY id DESC LIMIT ?
                )
                GROUP BY result_type, status
                """,
                (window_runs,),
            ).fetchall()
            status_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM morning_sector_radar_outcomes
                WHERE run_id IN (
                  SELECT id FROM morning_sector_radar_runs WHERE status = 'done' ORDER BY id DESC LIMIT ?
                )
                GROUP BY status
                """,
                (window_runs,),
            ).fetchall()
            action_rows = connection.execute(
                """
                SELECT COALESCE(NULLIF(ai_action, ''), 'rules') AS ai_action, status, COUNT(*) AS count,
                       AVG(close_return_pct) AS avg_close_return_pct,
                       AVG(excess_close_return_pct) AS avg_excess_close_return_pct
                FROM morning_sector_radar_outcomes
                WHERE run_id IN (
                  SELECT id FROM morning_sector_radar_runs WHERE status = 'done' ORDER BY id DESC LIMIT ?
                )
                GROUP BY COALESCE(NULLIF(ai_action, ''), 'rules'), status
                """,
                (window_runs,),
            ).fetchall()
            run_breakdown = connection.execute(
                """
                SELECT
                  r.id AS run_id,
                  r.trade_date,
                  r.scan_time,
                  r.selection_mode,
                  r.rule_fallback_used,
                  r.ai_status,
                  r.actual_source,
                  r.data_quality_status,
                  r.sample_role,
                  COUNT(o.id) AS row_count,
                  SUM(CASE WHEN o.status = 'done' THEN 1 ELSE 0 END) AS done_count,
                  SUM(CASE WHEN o.status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                  SUM(CASE WHEN o.status = 'no_data' THEN 1 ELSE 0 END) AS no_data_count,
                  SUM(CASE WHEN o.status = 'excluded' THEN 1 ELSE 0 END) AS excluded_count
                FROM morning_sector_radar_runs r
                LEFT JOIN morning_sector_radar_outcomes o ON o.run_id = r.id
                WHERE r.id IN (
                  SELECT id FROM morning_sector_radar_runs WHERE status = 'done' ORDER BY id DESC LIMIT ?
                )
                GROUP BY r.id
                ORDER BY r.id DESC
                """,
                (window_runs,),
            ).fetchall()
        status_counts = {str(row["status"] or "pending"): int(row["count"] or 0) for row in status_rows}
        row_count = sum(status_counts.values())
        result_type_parts = [f"{_result_type_label(str(row['result_type'] or ''))} {int(row['count'] or 0)}" for row in outcomes]
        return {
            "windowRuns": window_runs,
            "runCount": len(runs),
            "runs": [dict(row) for row in runs],
            "groups": [dict(row) for row in outcomes],
            "aiActionGroups": [dict(row) for row in action_rows],
            "runBreakdown": [dict(row) for row in run_breakdown],
            "rowCount": row_count,
            "doneCount": status_counts.get("done", 0),
            "pendingCount": status_counts.get("pending", 0),
            "noDataCount": status_counts.get("no_data", 0),
            "excludedCount": status_counts.get("excluded", 0),
            "outcomeStatus": {
                "rowCount": row_count,
                "doneCount": status_counts.get("done", 0),
                "pendingCount": status_counts.get("pending", 0),
                "noDataCount": status_counts.get("no_data", 0),
                "excludedCount": status_counts.get("excluded", 0),
            },
            "formulaText": f"{len(runs)} 次任务，共 {row_count} 条样本行" + (f"（{' + '.join(result_type_parts)}）" if result_type_parts else ""),
        }

    def _morning_sector_radar_outcome_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "runId": int(row["run_id"]),
            "sectorCode": row["sector_code"],
            "sectorName": row["sector_name"],
            "sectorType": row["sector_type"],
            "tradeDate": row["trade_date"],
            "scanTime": row["scan_time"],
            "sampleRole": row["sample_role"],
            "resultType": row["result_type"],
            "ruleRank": int(row["rule_rank"] or 0),
            "aiRank": int(row["ai_rank"] or 0),
            "aiAction": row["ai_action"],
            "selected": bool(row["selected"]),
            "referenceValue": row["reference_value"],
            "closeValue": row["close_value"],
            "nextOpenValue": row["next_open_value"],
            "closeReturnPct": row["close_return_pct"],
            "nextOpenReturnPct": row["next_open_return_pct"],
            "benchmarkCloseReturnPct": row["benchmark_close_return_pct"],
            "benchmarkNextOpenReturnPct": row["benchmark_next_open_return_pct"],
            "excessCloseReturnPct": row["excess_close_return_pct"],
            "excessNextOpenReturnPct": row["excess_next_open_return_pct"],
            "maxRunupPct": row["max_runup_pct"],
            "maxDrawdownPct": row["max_drawdown_pct"],
            "status": row["status"],
            "label": row["label"],
            "reason": row["reason"],
            "updatedAt": row["updated_at"],
            "outcome": _json_or_default(row["outcome_json"], {}),
            "runStatus": row["run_status"],
            "selectionMode": row["selection_mode"],
            "ruleFallbackUsed": bool(row["rule_fallback_used"]),
            "runAiStatus": row["run_ai_status"],
            "actualSource": row["actual_source"],
            "dataQualityStatus": row["data_quality_status"],
            "sourceMeta": _json_or_default(row["source_meta_json"], {}),
            "market": _json_or_default(row["market_json"], {}),
        }

    def _ai_full_market_run_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "profile": row["profile"],
            "riskMode": row["risk_mode"],
            "requestedSource": row["requested_source"],
            "actualSource": row["actual_source"],
            "scanTime": row["scan_time"],
            "status": row["status"],
            "selectionMode": row["selection_mode"],
            "totalCount": int(row["total_count"] or 0),
            "hardFilteredCount": int(row["hard_filtered_count"] or 0),
            "poolSize": int(row["pool_size"] or 0),
            "batchSize": int(row["batch_size"] or 0),
            "batchPick": int(row["batch_pick"] or 0),
            "finalLimit": int(row["final_limit"] or 10),
            "batchCount": int(row["batch_count"] or 0),
            "completedBatchCount": int(row["completed_batch_count"] or 0),
            "semifinalCount": int(row["semifinal_count"] or 0),
            "finalCount": int(row["final_count"] or 0),
            "currentStage": row["current_stage"],
            "finalPicks": _json_or_default(row["final_picks_json"], []),
            "batchSummary": _json_or_default(row["batch_summary_json"], []),
            "request": _json_or_default(row["request_json"], {}),
            "market": _json_or_default(row["market_json"], {}),
            "error": row["error"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _ai_full_market_item_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        pick = _json_or_default(row["pick_json"], {})
        return {
            "runId": int(row["run_id"]),
            "code": row["code"],
            "name": row["name"],
            "ruleRank": int(row["rule_rank"] or 0),
            "aiRank": int(row["ai_rank"] or 0),
            "batchIndex": int(row["batch_index"] or 0),
            "stage": row["stage"],
            "selected": bool(row["selected"]),
            "score": row["score"],
            "confidence": row["confidence"],
            "reason": row["reason"],
            "risk": row["risk"],
            "focus": row["focus"],
            "candidateTypeUsed": pick.get("candidateTypeUsed") or pick.get("aiCandidateTypeUsed") or "",
            "whyBetterThanSimilar": pick.get("whyBetterThanSimilar") or pick.get("aiWhyBetterThanSimilar") or "",
            "item": _json_or_default(row["item_json"], {}),
            "pick": pick,
        }

    def _morning_sector_radar_run_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "tradeDate": row["trade_date"],
            "scanTime": row["scan_time"],
            "sectorType": row["sector_type"],
            "requestedSource": row["requested_source"],
            "actualSource": row["actual_source"],
            "marketSource": row["market_source"],
            "status": row["status"],
            "selectionMode": row["selection_mode"],
            "ruleFallbackUsed": bool(row["rule_fallback_used"]),
            "aiStatus": row["ai_status"],
            "aiModel": row["ai_model"],
            "promptVersion": row["prompt_version"],
            "aiLatencyMs": int(row["ai_latency_ms"] or 0),
            "sampleRole": row["sample_role"],
            "currentStage": row["current_stage"],
            "sectorCount": int(row["sector_count"] or 0),
            "ruleCandidateCount": int(row["rule_candidate_count"] or 0),
            "qualifiedCount": int(row["qualified_count"] or 0),
            "aiFinalCount": int(row["ai_final_count"] or 0),
            "overheatedCount": int(row["overheated_count"] or 0),
            "dataQualityStatus": row["data_quality_status"],
            "request": _json_or_default(row["request_json"], {}),
            "market": _json_or_default(row["market_json"], {}),
            "sourceMeta": _json_or_default(row["source_meta_json"], {}),
            "aiResponse": _json_or_default(row["ai_response_json"], {}),
            "aiError": row["ai_error"],
            "error": row["error"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _morning_sector_radar_sector_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        snapshot = _json_or_default(row["snapshot_json"], {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        snapshot.update(
            {
                "runId": int(row["run_id"]),
                "sectorCode": row["sector_code"],
                "sectorName": row["sector_name"],
                "sectorType": row["sector_type"],
                "ruleRank": int(row["rule_rank"] or 0),
                "ruleStatus": row["rule_status"],
                "aiRank": int(row["ai_rank"] or 0),
                "aiAction": row["ai_action"],
                "aiStatus": row["ai_status"],
                "finalRank": int(row["final_rank"] or 0),
                "finalStatus": row["final_status"],
                "selected": bool(row["selected"]),
                "activityScore": row["activity_score"],
                "aiConfidence": row["ai_confidence"],
                "changePct": row["change_pct"],
                "breadthPct": row["breadth_pct"],
                "turnoverAmount": row["turnover_amount"],
                "turnoverActivity": row["turnover_activity"],
                "mainNetInflow": row["main_net_inflow"],
                "mainNetInflowRatio": row["main_net_inflow_ratio"],
                "superLargeNetInflow": row["super_large_net_inflow"],
                "superLargeNetInflowRatio": row["super_large_net_inflow_ratio"],
                "fundFlowStatus": row["fund_flow_status"],
                "limitUpCount": int(row["limit_up_count"] or 0),
                "dimensions": _json_or_default(row["dimensions_json"], {}),
                "reasons": _json_or_default(row["reasons_json"], []),
                "risks": _json_or_default(row["risks_json"], []),
                "aiReason": row["ai_reason"],
                "counterEvidence": _json_or_default(row["counter_evidence_json"], []),
                "invalidConditions": _json_or_default(row["invalid_conditions_json"], []),
                "dataQuality": _json_or_default(row["data_quality_json"], {}),
            }
        )
        return snapshot

    def _morning_sector_radar_constituent_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = _json_or_default(row["item_json"], {})
        if not isinstance(item, dict):
            item = {}
        item.update(
            {
                "runId": int(row["run_id"]),
                "sectorCode": row["sector_code"],
                "sectorType": row["sector_type"],
                "code": row["code"],
                "name": row["name"],
                "sectorRank": int(row["sector_rank"] or 0),
                "candidateRole": row["candidate_role"],
                "tradeability": row["tradeability"],
                "score": row["score"],
                "reason": row["reason"],
                "risk": row["risk"],
            }
        )
        return item

    def create_t1_overnight_evidence_run(self, run: Dict[str, Any]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        heartbeat_at = str(run.get("heartbeatAt") or now)
        lease_expires_at = str(run.get("leaseExpiresAt") or _lease_expiry(now))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO t1_overnight_evidence_runs(
                  recommendation_date, scan_time, requested_source, actual_source, status,
                  market_source, sector_source, minute_source, quality_status,
                  missing_json, request_json, market_json, error, started_at,
                  heartbeat_at, lease_expires_at, worker_id, last_stage_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run.get("recommendationDate") or date.today().isoformat()),
                    str(run.get("scanTime") or ""),
                    str(run.get("requestedSource") or "auto"),
                    str(run.get("actualSource") or ""),
                    str(run.get("status") or "pending"),
                    str(run.get("marketSource") or ""),
                    str(run.get("sectorSource") or ""),
                    str(run.get("minuteSource") or ""),
                    str(run.get("qualityStatus") or ""),
                    json.dumps(run.get("missing") or [], ensure_ascii=False),
                    json.dumps(run.get("request") or {}, ensure_ascii=False),
                    json.dumps(run.get("market") or {}, ensure_ascii=False),
                    str(run.get("error") or ""),
                    str(run.get("startedAt") or now),
                    heartbeat_at,
                    lease_expires_at,
                    str(run.get("workerId") or ""),
                    str(run.get("lastStageAt") or now),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def update_t1_overnight_evidence_run(self, run_id: int, updates: Dict[str, Any]) -> None:
        allowed = {
            "status": "status",
            "actualSource": "actual_source",
            "marketSource": "market_source",
            "sectorSource": "sector_source",
            "minuteSource": "minute_source",
            "marketSnapshotCount": "market_snapshot_count",
            "sectorCount": "sector_count",
            "constituentCount": "constituent_count",
            "stockMapCount": "stock_map_count",
            "minuteCount": "minute_count",
            "qualityStatus": "quality_status",
            "missing": "missing_json",
            "request": "request_json",
            "market": "market_json",
            "error": "error",
            "startedAt": "started_at",
            "finishedAt": "finished_at",
            "heartbeatAt": "heartbeat_at",
            "leaseExpiresAt": "lease_expires_at",
            "workerId": "worker_id",
            "lastStageAt": "last_stage_at",
        }
        sets = []
        params: List[Any] = []
        for key, column in allowed.items():
            if key not in updates:
                continue
            value = updates[key]
            if key in {"missing"}:
                value = json.dumps(value or [], ensure_ascii=False)
            if key in {"request", "market"}:
                value = json.dumps(value or {}, ensure_ascii=False)
            sets.append(f"{column} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        now = datetime.now().isoformat(timespec="seconds")
        params.append(now)
        if "heartbeatAt" not in updates:
            sets.append("heartbeat_at = ?")
            params.append(now)
        if "lastStageAt" not in updates:
            sets.append("last_stage_at = ?")
            params.append(now)
        if "leaseExpiresAt" not in updates:
            sets.append("lease_expires_at = ?")
            params.append(_lease_expiry(now))
        params.append(run_id)
        with self.connect() as connection:
            connection.execute(f"UPDATE t1_overnight_evidence_runs SET {', '.join(sets)} WHERE id = ?", params)

    def latest_t1_overnight_evidence_run(
        self,
        recommendation_date: str,
        scan_time: str,
        requested_source: str = "auto",
        status: str = "done",
    ) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM t1_overnight_evidence_runs
                WHERE recommendation_date = ?
                  AND scan_time = ?
                  AND requested_source = ?
                  AND status = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (recommendation_date, scan_time, requested_source, status),
            ).fetchone()
        return self._t1_overnight_evidence_run_from_row(row) if row else None

    def latest_reusable_t1_overnight_evidence_run(
        self,
        recommendation_date: str,
        scan_time: str,
        requested_source: str = "auto",
        qualities: tuple[str, ...] = ("A", "B"),
    ) -> Optional[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in qualities)
        params = [recommendation_date, scan_time, requested_source, "done", *qualities]
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT *
                FROM t1_overnight_evidence_runs
                WHERE recommendation_date = ?
                  AND scan_time = ?
                  AND requested_source = ?
                  AND status = ?
                  AND quality_status IN ({placeholders})
                ORDER BY
                  CASE quality_status
                    WHEN 'A' THEN 1
                    WHEN 'B' THEN 2
                    WHEN 'C' THEN 3
                    ELSE 4
                  END,
                  id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return self._t1_overnight_evidence_run_from_row(row) if row else None

    def load_t1_overnight_evidence_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM t1_overnight_evidence_runs WHERE id = ?", (run_id,)).fetchone()
        return self._t1_overnight_evidence_run_from_row(row) if row else None

    def recover_stale_t1_runs(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Close expired T+1 leases so no run can remain pending/running forever."""
        now_dt = now or datetime.now()
        now_text = now_dt.isoformat(timespec="seconds")
        recovered_selection: List[Dict[str, Any]] = []
        recovered_evidence: List[Dict[str, Any]] = []
        with self.connect() as connection:
            evidence_rows = connection.execute(
                """
                SELECT id, status, market_snapshot_count, sector_count, stock_map_count, minute_count,
                       (SELECT COUNT(*) FROM t1_overnight_market_snapshots item WHERE item.run_id = run.id) AS market_rows,
                       (SELECT COUNT(*) FROM t1_overnight_sector_snapshots item WHERE item.run_id = run.id) AS sector_rows,
                       (SELECT COUNT(*) FROM t1_overnight_stock_sector_map item WHERE item.run_id = run.id) AS stock_map_rows,
                       (SELECT COUNT(*) FROM t1_overnight_minute_snapshots item WHERE item.run_id = run.id) AS minute_rows
                FROM t1_overnight_evidence_runs run
                WHERE status IN ('pending', 'running')
                  AND lease_expires_at != ''
                  AND datetime(lease_expires_at) < datetime(?)
                ORDER BY id
                """,
                (now_text,),
            ).fetchall()
            for row in evidence_rows:
                has_evidence = any(
                    int(row[column] or 0) > 0
                    for column in (
                        "market_snapshot_count",
                        "sector_count",
                        "stock_map_count",
                        "minute_count",
                        "market_rows",
                        "sector_rows",
                        "stock_map_rows",
                        "minute_rows",
                    )
                )
                status = "partial" if has_evidence else "failed_timeout"
                error = "T+1 Evidence 租约过期，watchdog 已自动收口。"
                cursor = connection.execute(
                    """
                    UPDATE t1_overnight_evidence_runs
                    SET status = ?, quality_status = CASE WHEN ? = 'partial' THEN 'C' ELSE quality_status END,
                        error = CASE WHEN error = '' THEN ? ELSE error || '；' || ? END,
                        finished_at = ?, heartbeat_at = ?, last_stage_at = ?, lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND status IN ('pending', 'running')
                    """,
                    (status, status, error, error, now_text, now_text, now_text, now_text, now_text, int(row["id"])),
                )
                if cursor.rowcount:
                    recovered_evidence.append({"id": int(row["id"]), "status": status})

            selection_rows = connection.execute(
                """
                SELECT id, status, pool_size, semifinal_count, final_count
                FROM t1_overnight_selection_runs
                WHERE status IN ('pending', 'running')
                  AND lease_expires_at != ''
                  AND datetime(lease_expires_at) < datetime(?)
                ORDER BY id
                """,
                (now_text,),
            ).fetchall()
            for row in selection_rows:
                has_evidence = any(
                    int(row[column] or 0) > 0 for column in ("pool_size", "semifinal_count", "final_count")
                )
                status = "partial" if has_evidence else "failed_timeout"
                error = "T+1 选股租约过期，watchdog 已自动收口。"
                cursor = connection.execute(
                    """
                    UPDATE t1_overnight_selection_runs
                    SET status = ?, selection_mode = CASE WHEN selection_mode = '' THEN 'watchdog_timeout' ELSE selection_mode END,
                        current_stage = 'watchdog_timeout',
                        error = CASE WHEN error = '' THEN ? ELSE error || '；' || ? END,
                        finished_at = ?, heartbeat_at = ?, last_stage_at = ?, lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND status IN ('pending', 'running')
                    """,
                    (status, error, error, now_text, now_text, now_text, now_text, now_text, int(row["id"])),
                )
                if cursor.rowcount:
                    recovered_selection.append({"id": int(row["id"]), "status": status})
        if recovered_selection or recovered_evidence:
            LOGGER.warning(
                "t1 watchdog recovered selection=%s evidence=%s",
                len(recovered_selection),
                len(recovered_evidence),
            )
        return {"selection": recovered_selection, "evidence": recovered_evidence}

    def upsert_t1_overnight_market_snapshots(self, run_id: int, snapshots: List[Dict[str, Any]]) -> int:
        if not snapshots:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for item in snapshots:
            rows.append(
                (
                    run_id,
                    str(item.get("snapshotTime") or item.get("scanTime") or ""),
                    str(item.get("source") or ""),
                    json.dumps(item, ensure_ascii=False),
                    now,
                )
            )
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO t1_overnight_market_snapshots(run_id, snapshot_time, source, snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, snapshot_time) DO UPDATE SET
                  source=excluded.source,
                  snapshot_json=excluded.snapshot_json,
                  created_at=excluded.created_at
                """,
                rows,
            )
        return len(rows)

    def upsert_t1_overnight_sector_snapshots(self, run_id: int, sectors: List[Dict[str, Any]]) -> int:
        rows = []
        for index, sector in enumerate(sectors, start=1):
            sector_code = str(sector.get("sectorCode") or sector.get("sectorName") or "").strip()
            sector_type = str(sector.get("sectorType") or "").strip()
            if not sector_code or not sector_type:
                continue
            rows.append(
                (
                    run_id,
                    sector_type,
                    sector_code,
                    str(sector.get("sectorName") or ""),
                    int(sector.get("rank") or sector.get("ruleRank") or index),
                    _optional_float(sector.get("changePct")),
                    _optional_float(sector.get("turnoverAmount")),
                    _optional_float(sector.get("mainNetInflow")),
                    str(sector.get("fundFlowStatus") or ""),
                    int(sector.get("limitUpCount") or 0),
                    int(sector.get("upCount") or 0),
                    int(sector.get("downCount") or 0),
                    str(sector.get("sectorStateShadow") or sector.get("sectorState") or "data_insufficient"),
                    _optional_float(sector.get("sectorRiskScore")),
                    _optional_float(sector.get("sectorStateConfidence") or sector.get("sectorConfidence")),
                    str(sector.get("tradeDate") or ""),
                    str(sector.get("scanTime") or sector.get("asOfTime") or ""),
                    str(sector.get("snapshotScope") or ""),
                    str(sector.get("sectorStateVersion") or ""),
                    json.dumps(sector, ensure_ascii=False),
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO t1_overnight_sector_snapshots(
                  run_id, sector_type, sector_code, sector_name, sector_rank,
                  change_pct, turnover_amount, main_net_inflow, fund_flow_status,
                  limit_up_count, up_count, down_count, sector_state, sector_risk_score,
                  sector_confidence, trade_date, as_of_time, snapshot_scope,
                  sector_state_version, snapshot_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, sector_type, sector_code) DO UPDATE SET
                  sector_name=excluded.sector_name,
                  sector_rank=excluded.sector_rank,
                  change_pct=excluded.change_pct,
                  turnover_amount=excluded.turnover_amount,
                  main_net_inflow=excluded.main_net_inflow,
                  fund_flow_status=excluded.fund_flow_status,
                  limit_up_count=excluded.limit_up_count,
                  up_count=excluded.up_count,
                  down_count=excluded.down_count,
                  sector_state=excluded.sector_state,
                  sector_risk_score=excluded.sector_risk_score,
                  sector_confidence=excluded.sector_confidence,
                  trade_date=excluded.trade_date,
                  as_of_time=excluded.as_of_time,
                  snapshot_scope=excluded.snapshot_scope,
                  sector_state_version=excluded.sector_state_version,
                  snapshot_json=excluded.snapshot_json
                """,
                rows,
            )
        return len(rows)

    def upsert_t1_overnight_sector_constituents(self, run_id: int, constituents: List[Dict[str, Any]]) -> int:
        rows = []
        for item in constituents:
            code = str(item.get("code") or "").zfill(6)
            sector_code = str(item.get("sectorCode") or "").strip()
            sector_type = str(item.get("sectorType") or "").strip()
            if not code or not sector_code or not sector_type:
                continue
            rows.append(
                (
                    run_id,
                    sector_type,
                    sector_code,
                    code,
                    str(item.get("name") or ""),
                    int(item.get("stockRank") or item.get("sectorRank") or 0),
                    _optional_float(item.get("changePct")),
                    _optional_float(item.get("turnoverAmount")),
                    _optional_float(item.get("turnoverRate")),
                    _optional_float(item.get("volumeRatio")),
                    _optional_float(item.get("mainNetInflow")),
                    str(item.get("limitStatus") or ""),
                    json.dumps(item, ensure_ascii=False),
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO t1_overnight_sector_constituents(
                  run_id, sector_type, sector_code, code, name, stock_rank,
                  change_pct, turnover_amount, turnover_rate, volume_ratio,
                  main_net_inflow, limit_status, constituent_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, sector_type, sector_code, code) DO UPDATE SET
                  name=excluded.name,
                  stock_rank=excluded.stock_rank,
                  change_pct=excluded.change_pct,
                  turnover_amount=excluded.turnover_amount,
                  turnover_rate=excluded.turnover_rate,
                  volume_ratio=excluded.volume_ratio,
                  main_net_inflow=excluded.main_net_inflow,
                  limit_status=excluded.limit_status,
                  constituent_json=excluded.constituent_json
                """,
                rows,
            )
        return len(rows)

    def upsert_t1_overnight_stock_sector_maps(self, run_id: int, items: List[Dict[str, Any]]) -> int:
        if not items:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for item in items:
            code = str(item.get("code") or "").zfill(6)
            if not code:
                continue
            rows.append(
                (
                    run_id,
                    code,
                    str(item.get("name") or ""),
                    str(item.get("listingBoard") or ""),
                    str(item.get("industrySector") or ""),
                    json.dumps(item.get("conceptSectors") or [], ensure_ascii=False),
                    str(item.get("sectorEvidenceStatus") or "unavailable"),
                    json.dumps(item.get("sectorEvidence") or item, ensure_ascii=False),
                    now,
                )
            )
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO t1_overnight_stock_sector_map(
                  run_id, code, name, listing_board, industry_sector, concept_sectors_json,
                  sector_evidence_status, evidence_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, code) DO UPDATE SET
                  name=excluded.name,
                  listing_board=excluded.listing_board,
                  industry_sector=excluded.industry_sector,
                  concept_sectors_json=excluded.concept_sectors_json,
                  sector_evidence_status=excluded.sector_evidence_status,
                  evidence_json=excluded.evidence_json,
                  updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def load_t1_overnight_stock_sector_maps(self, run_id: int, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        normalized = [str(code or "").zfill(6) for code in codes if str(code or "").strip()]
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM t1_overnight_stock_sector_map
                WHERE run_id = ? AND code IN ({placeholders})
                """,
                [run_id, *normalized],
            ).fetchall()
        return {str(row["code"]): self._t1_overnight_stock_sector_map_from_row(row) for row in rows}

    def load_t1_stock_sector_identity_cache(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        normalized = [str(code or "").strip().zfill(6) for code in codes if str(code or "").strip()]
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM t1_stock_sector_identity_cache
                WHERE code IN ({placeholders})
                """,
                normalized,
            ).fetchall()
        return {str(row["code"]): self._t1_stock_sector_identity_cache_from_row(row) for row in rows}

    def upsert_t1_stock_sector_identity_cache(self, items: List[Dict[str, Any]]) -> int:
        if not items:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for item in items:
            code = str(item.get("code") or "").strip().zfill(6)
            if not code or code == "000000":
                continue
            concepts = item.get("conceptSectors") or []
            if isinstance(concepts, str):
                concepts = [concepts]
            rows.append(
                (
                    code,
                    str(item.get("name") or ""),
                    str(item.get("industrySector") or ""),
                    json.dumps(concepts or [], ensure_ascii=False),
                    str(item.get("regionSector") or ""),
                    str(item.get("source") or ""),
                    _optional_float(item.get("confidence")),
                    str(item.get("status") or "available"),
                    str(item.get("lastSuccessAt") or item.get("generatedAt") or now),
                    now,
                    json.dumps(item.get("raw") or item, ensure_ascii=False),
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO t1_stock_sector_identity_cache(
                  code, name, industry_sector, concept_sectors_json, region_sector,
                  source, confidence, status, last_success_at, updated_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                  name=excluded.name,
                  industry_sector=excluded.industry_sector,
                  concept_sectors_json=excluded.concept_sectors_json,
                  region_sector=excluded.region_sector,
                  source=excluded.source,
                  confidence=excluded.confidence,
                  status=excluded.status,
                  last_success_at=excluded.last_success_at,
                  updated_at=excluded.updated_at,
                  raw_json=excluded.raw_json
                """,
                rows,
            )
        return len(rows)

    def upsert_t1_overnight_minute_snapshots(self, run_id: int, period: str, items: List[Dict[str, Any]]) -> int:
        if not items:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for item in items:
            code = str(item.get("code") or "").zfill(6)
            if not code:
                continue
            series = item.get("series") or []
            rows.append(
                (
                    run_id,
                    code,
                    str(item.get("period") or period or "15m"),
                    str(item.get("source") or ""),
                    int(item.get("pointCount") or len(series)),
                    str(item.get("qualityStatus") or ("available" if series else "unavailable")),
                    str(item.get("error") or ""),
                    json.dumps(series, ensure_ascii=False),
                    now,
                )
            )
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO t1_overnight_minute_snapshots(
                  run_id, code, period, source, point_count, quality_status, error, series_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, code, period) DO UPDATE SET
                  source=excluded.source,
                  point_count=excluded.point_count,
                  quality_status=excluded.quality_status,
                  error=excluded.error,
                  series_json=excluded.series_json,
                  updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def load_t1_overnight_minute_snapshots(self, run_id: int, codes: List[str], period: str = "15m") -> Dict[str, Dict[str, Any]]:
        normalized = [str(code or "").zfill(6) for code in codes if str(code or "").strip()]
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM t1_overnight_minute_snapshots
                WHERE run_id = ? AND period = ? AND code IN ({placeholders})
                """,
                [run_id, period, *normalized],
            ).fetchall()
        return {str(row["code"]): self._t1_overnight_minute_snapshot_from_row(row) for row in rows}

    def load_latest_t1_overnight_minute_snapshots(
        self,
        recommendation_date: str,
        codes: List[str],
        period: str = "15m",
        exclude_run_id: int = 0,
    ) -> Dict[str, Dict[str, Any]]:
        normalized = [str(code or "").zfill(6) for code in codes if str(code or "").strip()]
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        params: List[Any] = [recommendation_date, period, *normalized]
        exclude_clause = ""
        if exclude_run_id:
            exclude_clause = "AND m.run_id != ?"
            params.append(int(exclude_run_id))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT m.*
                FROM t1_overnight_minute_snapshots m
                JOIN t1_overnight_evidence_runs r ON r.id = m.run_id
                WHERE r.recommendation_date = ?
                  AND m.period = ?
                  AND m.code IN ({placeholders})
                  AND m.quality_status = 'available'
                  AND m.point_count > 0
                  {exclude_clause}
                ORDER BY m.code ASC, m.updated_at DESC, m.run_id DESC
                """,
                params,
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            code = str(row["code"])
            if code in result:
                continue
            item = self._t1_overnight_minute_snapshot_from_row(row)
            if item.get("series"):
                result[code] = item
        return result

    def load_latest_t1_overnight_validation_minute_snapshots_any_run(
        self,
        target_trade_date: str,
        codes: List[str],
        period: str = "",
        exclude_run_id: int = 0,
    ) -> Dict[str, Dict[str, Any]]:
        normalized = [str(code or "").zfill(6) for code in codes if str(code or "").strip()]
        if not normalized or not target_trade_date:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        period_clause = ""
        params: List[Any] = [str(target_trade_date), *normalized]
        if period:
            period_clause = "AND period = ?"
            params.append(str(period))
        exclude_clause = ""
        if exclude_run_id:
            exclude_clause = "AND run_id != ?"
            params.append(int(exclude_run_id))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM t1_overnight_validation_minute_snapshots
                WHERE target_trade_date = ?
                  AND code IN ({placeholders})
                  AND quality_status IN ('available', 'partial')
                  AND point_count > 0
                  {period_clause}
                  {exclude_clause}
                ORDER BY code ASC,
                  CASE quality_status WHEN 'available' THEN 0 WHEN 'partial' THEN 1 ELSE 9 END,
                  CASE period WHEN '1m' THEN 0 WHEN '5m' THEN 1 WHEN '15m' THEN 2 ELSE 9 END,
                  updated_at DESC,
                  run_id DESC
                """,
                params,
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            code = str(row["code"] or "").zfill(6)
            if code in result:
                continue
            item = self._t1_overnight_validation_minute_snapshot_from_row(row)
            if item.get("series"):
                result[code] = item
        return result

    def create_t1_overnight_selection_run(self, run: Dict[str, Any], pool_items: List[Dict[str, Any]]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        heartbeat_at = str(run.get("heartbeatAt") or now)
        lease_expires_at = str(run.get("leaseExpiresAt") or _lease_expiry(now))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO t1_overnight_selection_runs(
                  recommendation_date, scan_time, risk_mode, requested_source, actual_source, evidence_run_id, status, selection_mode,
                  total_count, hard_filtered_count, pool_size, batch_size, batch_pick, final_limit,
                  batch_count, request_json, market_json, heartbeat_at, lease_expires_at, worker_id,
                  last_stage_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run.get("recommendationDate") or date.today().isoformat()),
                    str(run.get("scanTime") or ""),
                    str(run.get("riskMode") or "balanced"),
                    str(run.get("requestedSource") or "auto"),
                    str(run.get("actualSource") or ""),
                    int(run.get("evidenceRunId") or 0),
                    str(run.get("status") or "pending"),
                    str(run.get("selectionMode") or ""),
                    int(run.get("totalCount") or 0),
                    int(run.get("hardFilteredCount") or 0),
                    int(run.get("poolSize") or len(pool_items)),
                    int(run.get("batchSize") or 60),
                    int(run.get("batchPick") or 6),
                    int(run.get("finalLimit") or 3),
                    int(run.get("batchCount") or 0),
                    json.dumps(run.get("request") or {}, ensure_ascii=False),
                    json.dumps(run.get("market") or {}, ensure_ascii=False),
                    heartbeat_at,
                    lease_expires_at,
                    str(run.get("workerId") or ""),
                    str(run.get("lastStageAt") or now),
                    now,
                    now,
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO t1_overnight_selection_items(
                  run_id, code, name, pool_rank, batch_index, stage, candidate_type, score, item_json
                )
                VALUES (?, ?, ?, ?, ?, 'pool', ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        str(item.get("code") or "").zfill(6),
                        str(item.get("name") or ""),
                        int(item.get("poolRank") or item.get("rank") or index),
                        int(item.get("batchIndex") or 0),
                        str(item.get("candidateType") or ""),
                        _optional_float(item.get("t1Score", item.get("score"))),
                        json.dumps(item, ensure_ascii=False),
                    )
                    for index, item in enumerate(pool_items, start=1)
                    if isinstance(item, dict) and str(item.get("code") or "").strip()
                ],
            )
        return run_id

    def update_t1_overnight_selection_run(self, run_id: int, updates: Dict[str, Any]) -> None:
        allowed = {
            "status": "status",
            "selectionMode": "selection_mode",
            "actualSource": "actual_source",
            "evidenceRunId": "evidence_run_id",
            "totalCount": "total_count",
            "hardFilteredCount": "hard_filtered_count",
            "poolSize": "pool_size",
            "batchSize": "batch_size",
            "batchPick": "batch_pick",
            "finalLimit": "final_limit",
            "batchCount": "batch_count",
            "completedBatchCount": "completed_batch_count",
            "semifinalCount": "semifinal_count",
            "finalCount": "final_count",
            "currentStage": "current_stage",
            "finalPicks": "final_picks_json",
            "batchSummary": "batch_summary_json",
            "request": "request_json",
            "market": "market_json",
            "error": "error",
            "startedAt": "started_at",
            "finishedAt": "finished_at",
            "heartbeatAt": "heartbeat_at",
            "leaseExpiresAt": "lease_expires_at",
            "workerId": "worker_id",
            "lastStageAt": "last_stage_at",
        }
        sets = []
        params: List[Any] = []
        for key, column in allowed.items():
            if key not in updates:
                continue
            value = updates[key]
            if key in {"finalPicks", "batchSummary"}:
                value = json.dumps(value or [], ensure_ascii=False)
            if key in {"request", "market"}:
                value = json.dumps(value or {}, ensure_ascii=False)
            sets.append(f"{column} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        now = datetime.now().isoformat(timespec="seconds")
        params.append(now)
        if "heartbeatAt" not in updates:
            sets.append("heartbeat_at = ?")
            params.append(now)
        if "lastStageAt" not in updates:
            sets.append("last_stage_at = ?")
            params.append(now)
        if "leaseExpiresAt" not in updates:
            sets.append("lease_expires_at = ?")
            params.append(_lease_expiry(now))
        params.append(run_id)
        with self.connect() as connection:
            connection.execute(f"UPDATE t1_overnight_selection_runs SET {', '.join(sets)} WHERE id = ?", params)

    def upsert_t1_overnight_selection_items(self, run_id: int, items: List[Dict[str, Any]], stage: str) -> int:
        if not items:
            return 0
        rows = []
        for index, item in enumerate(items, start=1):
            code = str(item.get("code") or "").zfill(6)
            if not code:
                continue
            rows.append(
                (
                    run_id,
                    code,
                    str(item.get("name") or ""),
                    int(item.get("poolRank") or item.get("rank") or 0),
                    int(item.get("aiRank") or item.get("rank") or index),
                    int(item.get("batchIndex") or 0),
                    stage,
                    1 if item.get("selected", stage != "pool") else 0,
                    str(item.get("candidateType") or ""),
                    _optional_float(item.get("t1Score", item.get("score"))),
                    _optional_float(item.get("confidence")),
                    str(item.get("reason") or ""),
                    str(item.get("buyPlan") or ""),
                    str(item.get("sellPlan") or ""),
                    str(item.get("risk") or ""),
                    str(item.get("invalidCondition") or ""),
                    json.dumps(item.get("item") or item, ensure_ascii=False),
                    json.dumps(item, ensure_ascii=False),
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO t1_overnight_selection_items(
                  run_id, code, name, pool_rank, ai_rank, batch_index, stage, selected,
                  candidate_type, score, confidence, reason, buy_plan, sell_plan, risk,
                  invalid_condition, item_json, pick_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, code) DO UPDATE SET
                  name=excluded.name,
                  pool_rank=excluded.pool_rank,
                  ai_rank=excluded.ai_rank,
                  batch_index=excluded.batch_index,
                  stage=excluded.stage,
                  selected=excluded.selected,
                  candidate_type=excluded.candidate_type,
                  score=excluded.score,
                  confidence=excluded.confidence,
                  reason=excluded.reason,
                  buy_plan=excluded.buy_plan,
                  sell_plan=excluded.sell_plan,
                  risk=excluded.risk,
                  invalid_condition=excluded.invalid_condition,
                  item_json=excluded.item_json,
                  pick_json=excluded.pick_json
                """,
                rows,
            )
        return len(rows)

    def load_t1_overnight_selection_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM t1_overnight_selection_runs WHERE id = ?", (run_id,)).fetchone()
        return self._t1_overnight_run_from_row(row) if row else None

    def latest_t1_overnight_selection_run(
        self,
        risk_mode: str = "balanced",
        requested_source: str = "auto",
    ) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM t1_overnight_selection_runs
                WHERE risk_mode = ? AND requested_source = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (risk_mode, requested_source),
            ).fetchone()
        return self._t1_overnight_run_from_row(row) if row else None

    def list_t1_overnight_selection_runs(self, limit: int = 60, status: str = "") -> List[Dict[str, Any]]:
        params: List[Any] = []
        status_clause = ""
        normalized_status = str(status or "").strip()
        if normalized_status:
            status_clause = "WHERE run.status = ?"
            params.append(normalized_status)
        params.append(max(1, int(limit or 60)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                  run.*,
                  COUNT(outcome.code) AS outcome_count,
                  SUM(CASE WHEN outcome.outcome_label != '' AND outcome.outcome_label != '数据不足' THEN 1 ELSE 0 END) AS verified_outcome_count,
                  SUM(CASE WHEN outcome.outcome_label IN ('强成功', '成功溢价', '弱成功') THEN 1 ELSE 0 END) AS success_count,
                  SUM(CASE WHEN outcome.outcome_label = '强成功' THEN 1 ELSE 0 END) AS strong_success_count,
                  AVG(json_extract(outcome.outcome_json, '$.tradeable.primaryScenario.netReturnPct')) AS avg_primary_net_return_pct,
                  MAX(outcome.verified_at) AS latest_verified_at
                FROM t1_overnight_selection_runs run
                LEFT JOIN t1_overnight_selection_outcomes outcome ON outcome.run_id = run.id
                {status_clause}
                GROUP BY run.id
                ORDER BY run.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._t1_overnight_run_summary_from_row(row) for row in rows]

    def load_t1_overnight_selection_items(self, run_id: int) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM t1_overnight_selection_items
                WHERE run_id = ?
                ORDER BY selected DESC, ai_rank, pool_rank, code
                """,
                (run_id,),
            ).fetchall()
        return [self._t1_overnight_item_from_row(row) for row in rows]

    def upsert_t1_overnight_selection_outcomes(self, run_id: int, outcomes: List[Dict[str, Any]]) -> int:
        if not outcomes:
            return 0
        rows = []
        for outcome in outcomes:
            code = str(outcome.get("code") or "").zfill(6)
            if not code:
                continue
            rows.append(
                (
                    run_id,
                    code,
                    str(outcome.get("name") or ""),
                    str(outcome.get("recommendationDate") or ""),
                    str(outcome.get("nextTradeDate") or ""),
                    _optional_float(outcome.get("buyReferencePrice")),
                    _optional_float(outcome.get("nextOpen")),
                    _optional_float(outcome.get("nextHigh")),
                    _optional_float(outcome.get("nextLow")),
                    _optional_float(outcome.get("nextClose")),
                    _optional_float(outcome.get("openReturnPct")),
                    _optional_float(outcome.get("highReturnPct")),
                    _optional_float(outcome.get("closeReturnPct")),
                    _optional_float(outcome.get("maxDrawdownPct")),
                    _optional_float(outcome.get("benchmarkReturnPct")),
                    _optional_float(outcome.get("excessReturnPct")),
                    str(outcome.get("outcomeLabel") or ""),
                    _optional_float(outcome.get("outcomeScore")),
                    1 if outcome.get("hitSellPlan") else 0,
                    1 if outcome.get("hitInvalidCondition") else 0,
                    str(outcome.get("failureReason") or ""),
                    str(outcome.get("verifiedAt") or datetime.now().isoformat(timespec="seconds")),
                    str(outcome.get("dataSource") or ""),
                    json.dumps(outcome, ensure_ascii=False),
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO t1_overnight_selection_outcomes(
                  run_id, code, name, recommendation_date, next_trade_date,
                  buy_reference_price, next_open, next_high, next_low, next_close,
                  open_return_pct, high_return_pct, close_return_pct, max_drawdown_pct,
                  benchmark_return_pct, excess_return_pct, outcome_label, outcome_score,
                  hit_sell_plan, hit_invalid_condition, failure_reason, verified_at,
                  data_source, outcome_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, code) DO UPDATE SET
                  name=excluded.name,
                  recommendation_date=excluded.recommendation_date,
                  next_trade_date=excluded.next_trade_date,
                  buy_reference_price=excluded.buy_reference_price,
                  next_open=excluded.next_open,
                  next_high=excluded.next_high,
                  next_low=excluded.next_low,
                  next_close=excluded.next_close,
                  open_return_pct=excluded.open_return_pct,
                  high_return_pct=excluded.high_return_pct,
                  close_return_pct=excluded.close_return_pct,
                  max_drawdown_pct=excluded.max_drawdown_pct,
                  benchmark_return_pct=excluded.benchmark_return_pct,
                  excess_return_pct=excluded.excess_return_pct,
                  outcome_label=excluded.outcome_label,
                  outcome_score=excluded.outcome_score,
                  hit_sell_plan=excluded.hit_sell_plan,
                  hit_invalid_condition=excluded.hit_invalid_condition,
                  failure_reason=excluded.failure_reason,
                  verified_at=excluded.verified_at,
                  data_source=excluded.data_source,
                  outcome_json=excluded.outcome_json
                """,
                rows,
            )
        return len(rows)

    def load_t1_overnight_selection_outcomes(self, run_id: int) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM t1_overnight_selection_outcomes
                WHERE run_id = ?
                ORDER BY outcome_score DESC, high_return_pct DESC, code
                """,
                (run_id,),
            ).fetchall()
        return [self._t1_overnight_outcome_from_row(row) for row in rows]

    def upsert_t1_overnight_validation_scenarios(self, run_id: int, scenarios: List[Dict[str, Any]]) -> int:
        if not scenarios:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for scenario in scenarios:
            code = str(scenario.get("code") or "").zfill(6)
            scenario_key = str(scenario.get("scenarioKey") or "")
            validation_version = str(scenario.get("validationVersion") or "v2_tradeable")
            if not code or not scenario_key:
                continue
            rows.append(
                (
                    run_id,
                    code,
                    validation_version,
                    scenario_key,
                    str(scenario.get("scenarioLabel") or ""),
                    str(scenario.get("sellTime") or ""),
                    _optional_float(scenario.get("sellPrice")),
                    _optional_float(scenario.get("grossReturnPct")),
                    _optional_float(scenario.get("netReturnPct")),
                    str(scenario.get("dataStatus") or ""),
                    json.dumps(scenario, ensure_ascii=False),
                    now,
                    now,
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO t1_overnight_validation_scenarios(
                  run_id, code, validation_version, scenario_key, scenario_label,
                  sell_time, sell_price, gross_return_pct, net_return_pct, data_status,
                  scenario_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, code, validation_version, scenario_key) DO UPDATE SET
                  scenario_label=excluded.scenario_label,
                  sell_time=excluded.sell_time,
                  sell_price=excluded.sell_price,
                  gross_return_pct=excluded.gross_return_pct,
                  net_return_pct=excluded.net_return_pct,
                  data_status=excluded.data_status,
                  scenario_json=excluded.scenario_json,
                  updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def load_t1_overnight_validation_scenarios(self, run_id: int) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM t1_overnight_validation_scenarios
                WHERE run_id = ?
                ORDER BY code, validation_version, scenario_key
                """,
                (run_id,),
            ).fetchall()
        return [self._t1_overnight_validation_scenario_from_row(row) for row in rows]

    def upsert_t1_overnight_validation_minute_snapshots(
        self,
        run_id: int,
        target_trade_date: str,
        period: str,
        items: List[Dict[str, Any]],
    ) -> int:
        if not items:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for item in items:
            code = str(item.get("code") or "").zfill(6)
            if not code:
                continue
            series = item.get("series") or []
            rows.append(
                (
                    int(run_id),
                    code,
                    str(item.get("targetTradeDate") or target_trade_date or ""),
                    str(item.get("period") or period or "1m"),
                    str(item.get("source") or ""),
                    int(item.get("pointCount") or len(series)),
                    str(item.get("qualityStatus") or ("available" if series else "unavailable")),
                    str(item.get("error") or ""),
                    json.dumps(series, ensure_ascii=False),
                    now,
                    now,
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO t1_overnight_validation_minute_snapshots(
                  run_id, code, target_trade_date, period, source, point_count,
                  quality_status, error, series_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, code, target_trade_date, period) DO UPDATE SET
                  source=excluded.source,
                  point_count=excluded.point_count,
                  quality_status=excluded.quality_status,
                  error=excluded.error,
                  series_json=excluded.series_json,
                  updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def load_t1_overnight_validation_minute_snapshots(
        self,
        run_id: int,
        codes: List[str],
        target_trade_date: str,
        period: str = "",
    ) -> Dict[str, Dict[str, Any]]:
        normalized = [str(code or "").zfill(6) for code in codes if str(code or "").strip()]
        if not normalized or not target_trade_date:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        period_clause = ""
        params: List[Any] = [int(run_id), str(target_trade_date), *normalized]
        if period:
            period_clause = "AND period = ?"
            params.append(str(period))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM t1_overnight_validation_minute_snapshots
                WHERE run_id = ?
                  AND target_trade_date = ?
                  AND code IN ({placeholders})
                  {period_clause}
                ORDER BY code ASC,
                  CASE quality_status WHEN 'available' THEN 0 WHEN 'partial' THEN 1 ELSE 9 END,
                  CASE period WHEN '1m' THEN 0 WHEN '5m' THEN 1 WHEN '15m' THEN 2 ELSE 9 END,
                  updated_at DESC
                """,
                params,
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            code = str(row["code"] or "").zfill(6)
            if code not in result:
                result[code] = self._t1_overnight_validation_minute_snapshot_from_row(row)
        return result

    def count_t1_overnight_completed_runs(self) -> int:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT request_json, market_json, scan_time
                FROM t1_overnight_selection_runs
                WHERE status = 'done'
                  AND final_count > 0
                  AND actual_source != 'mock'
                  AND actual_source NOT LIKE 'cache:%'
                """
            ).fetchall()
        return len(
            [
                row
                for row in rows
                if not _t1_history_display_only(
                    _json_or_default(row["market_json"], {}),
                    _json_or_default(row["request_json"], {}),
                )
                and _t1_run_role_from_json(_json_or_default(row["market_json"], {}), _json_or_default(row["request_json"], {}), str(row["scan_time"] or "")) == "official"
            ]
        )

    def count_t1_overnight_official_trade_dates(self) -> int:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT recommendation_date, request_json, market_json, scan_time
                FROM t1_overnight_selection_runs
                WHERE status = 'done'
                  AND actual_source != 'mock'
                  AND actual_source NOT LIKE 'cache:%'
                """
            ).fetchall()
        dates = set()
        for row in rows:
            market = _json_or_default(row["market_json"], {})
            request = _json_or_default(row["request_json"], {})
            if _t1_history_display_only(market, request):
                continue
            if _t1_run_role_from_json(market, request, str(row["scan_time"] or "")) != "official":
                continue
            recommendation_date = str(row["recommendation_date"] or "").strip()
            if recommendation_date:
                dates.add(recommendation_date[:10])
        return len(dates)

    def count_t1_overnight_verified_outcomes(self) -> int:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT outcome.outcome_json, run.request_json, run.market_json, run.scan_time
                FROM t1_overnight_selection_outcomes outcome
                JOIN t1_overnight_selection_runs run ON run.id = outcome.run_id
                WHERE outcome.outcome_label != ''
                  AND outcome.outcome_label != '数据不足'
                  AND run.actual_source != 'mock'
                  AND run.actual_source NOT LIKE 'cache:%'
                """
            ).fetchall()
        count = 0
        for row in rows:
            if _t1_history_display_only(_json_or_default(row["market_json"], {}), _json_or_default(row["request_json"], {})):
                continue
            if _t1_run_role_from_json(_json_or_default(row["market_json"], {}), _json_or_default(row["request_json"], {}), str(row["scan_time"] or "")) != "official":
                continue
            if _t1_outcome_history_display_only(_json_or_default(row["outcome_json"], {})):
                continue
            count += 1
        return count

    def count_t1_overnight_tradeable_outcomes(self) -> int:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT outcome.outcome_json, run.request_json, run.market_json, run.scan_time
                FROM t1_overnight_selection_outcomes outcome
                JOIN t1_overnight_selection_runs run ON run.id = outcome.run_id
                WHERE outcome.outcome_label != ''
                  AND outcome.outcome_label != '数据不足'
                  AND run.actual_source != 'mock'
                  AND run.actual_source NOT LIKE 'cache:%'
                """
            ).fetchall()
        count = 0
        for row in rows:
            if _t1_history_display_only(_json_or_default(row["market_json"], {}), _json_or_default(row["request_json"], {})):
                continue
            if _t1_run_role_from_json(_json_or_default(row["market_json"], {}), _json_or_default(row["request_json"], {}), str(row["scan_time"] or "")) != "official":
                continue
            payload = _json_or_default(row["outcome_json"], {})
            if _t1_outcome_history_display_only(payload):
                continue
            quality = payload.get("dataQuality") or {}
            primary = ((payload.get("tradeable") or {}).get("primaryScenario") or {})
            if (
                payload.get("validationVersion") == "v2_tradeable"
                and payload.get("validationStatus") == "final"
                and quality.get("grade") in {"A", "B"}
                and primary.get("scenarioKey") == "open_15m"
                and primary.get("dataStatus") == "ok"
                and primary.get("netReturnPct") is not None
                and payload.get("benchmarkReturnPct") is not None
            ):
                count += 1
        return count

    def load_recent_t1_overnight_outcomes(self, window_runs: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                  outcome.*,
                  run.actual_source AS run_actual_source,
                  run.selection_mode AS run_selection_mode,
                  run.market_json AS run_market_json,
                  run.scan_time AS run_scan_time,
                  run.final_count AS run_final_count,
                  run.request_json AS run_request_json
                FROM t1_overnight_selection_outcomes outcome
                JOIN t1_overnight_selection_runs run ON run.id = outcome.run_id
                WHERE run.status = 'done'
                  AND run.actual_source != 'mock'
                  AND run.actual_source NOT LIKE 'cache:%'
                  AND run.final_count > 0
                  AND outcome.outcome_label != ''
                ORDER BY outcome.recommendation_date DESC, outcome.run_id ASC, outcome.code
                LIMIT 1000
                """
            ).fetchall()
        grouped: Dict[str, Dict[int, List[sqlite3.Row]]] = {}
        run_meta: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            recommendation_date = str(row["recommendation_date"] or "")
            run_id = int(row["run_id"])
            market = _json_or_default(row["run_market_json"], {})
            request = _json_or_default(row["run_request_json"], {})
            if _t1_history_display_only(market, request):
                continue
            if _t1_outcome_history_display_only(_json_or_default(row["outcome_json"], {})):
                continue
            grouped.setdefault(recommendation_date, {}).setdefault(run_id, []).append(row)
            run_meta[run_id] = {
                "scanTime": row["run_scan_time"],
                "selectionMode": row["run_selection_mode"],
                "finalCount": int(row["run_final_count"] or 0),
                "market": _json_or_default(row["run_market_json"], {}),
                "request": _json_or_default(row["run_request_json"], {}),
            }
        selected_rows: List[sqlite3.Row] = []
        for recommendation_date in sorted(grouped.keys(), reverse=True):
            if len({row["recommendation_date"] for row in selected_rows}) >= max(1, int(window_runs or 20)):
                break
            run_ids = sorted(grouped[recommendation_date].keys())
            official = _pick_official_t1_run_id(run_ids, run_meta)
            if official:
                selected_rows.extend(grouped[recommendation_date].get(official, []))
        return [self._t1_overnight_outcome_from_row(row) for row in selected_rows]

    def load_recent_t1_overnight_raw_run_outcomes(self, window_runs: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                  outcome.*,
                  run.actual_source AS run_actual_source,
                  run.selection_mode AS run_selection_mode,
                  run.market_json AS run_market_json
                FROM t1_overnight_selection_outcomes outcome
                JOIN (
                  SELECT id, actual_source, selection_mode, market_json
                  FROM t1_overnight_selection_runs
                  WHERE status = 'done'
                    AND actual_source != 'mock'
                    AND actual_source NOT LIKE 'cache:%'
                  ORDER BY id DESC
                  LIMIT ?
                ) run ON run.id = outcome.run_id
                WHERE outcome.outcome_label != ''
                ORDER BY outcome.recommendation_date DESC, outcome.run_id DESC, outcome.code
                """,
                (max(1, int(window_runs or 20)),),
            ).fetchall()
        return [self._t1_overnight_outcome_from_row(row) for row in rows]

    def insert_t1_overnight_strategy_feedback(self, feedback: Dict[str, Any]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO t1_overnight_strategy_feedback(
                  feedback_date, window_runs, verified_outcomes, success_rate, strong_success_rate,
                  low_open_fail_rate, pullback_rate, avg_high_return_pct, avg_close_return_pct,
                  avg_max_drawdown_pct, best_patterns_json, worst_patterns_json, prompt_summary,
                  source_run_id, trigger_source, feedback_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(feedback.get("feedbackDate") or date.today().isoformat()),
                    int(feedback.get("windowRuns") or 0),
                    int(feedback.get("verifiedOutcomes") or 0),
                    _optional_float(feedback.get("successRate")),
                    _optional_float(feedback.get("strongSuccessRate")),
                    _optional_float(feedback.get("lowOpenFailRate")),
                    _optional_float(feedback.get("pullbackRate")),
                    _optional_float(feedback.get("avgHighReturnPct")),
                    _optional_float(feedback.get("avgCloseReturnPct")),
                    _optional_float(feedback.get("avgMaxDrawdownPct")),
                    json.dumps(feedback.get("bestPatterns") or [], ensure_ascii=False),
                    json.dumps(feedback.get("worstPatterns") or [], ensure_ascii=False),
                    str(feedback.get("promptSummary") or ""),
                    int(feedback.get("sourceRunId") or 0),
                    str(feedback.get("triggerSource") or ""),
                    json.dumps(feedback, ensure_ascii=False),
                    now,
                ),
            )
        return int(cursor.lastrowid)

    def upsert_t1_overnight_strategy_suggestions(self, feedback_id: int, suggestions: List[Dict[str, Any]]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        changed = 0
        with self.connect() as connection:
            for suggestion in suggestions:
                pattern_key = str(suggestion.get("patternKey") or suggestion.get("pattern") or "").strip()
                action = str(suggestion.get("suggestionAction") or suggestion.get("action") or "").strip()
                if not pattern_key or not action:
                    continue
                existing = connection.execute(
                    """
                    SELECT id, status, reviewer_note, activated_at
                    FROM t1_overnight_strategy_suggestions
                    WHERE pattern_key = ?
                      AND suggestion_action = ?
                      AND status IN ('draft', 'observing', 'active')
                    ORDER BY
                      CASE status WHEN 'active' THEN 0 WHEN 'observing' THEN 1 ELSE 2 END,
                      id DESC
                    LIMIT 1
                    """,
                    (pattern_key, action),
                ).fetchone()
                payload = json.dumps(suggestion, ensure_ascii=False)
                feedback_value = int(feedback_id or 0)
                pattern_label = str(suggestion.get("patternLabel") or suggestion.get("pattern") or pattern_key)
                market_state = str(suggestion.get("marketState") or "")
                candidate_type = str(suggestion.get("candidateType") or "")
                risk_profile = str(suggestion.get("riskProfile") or "")
                sample_count = int(suggestion.get("sampleCount") or suggestion.get("count") or 0)
                success_rate = _optional_float(suggestion.get("successRate"))
                strong_success_rate = _optional_float(suggestion.get("strongSuccessRate"))
                low_open_fail_rate = _optional_float(suggestion.get("lowOpenFailRate"))
                pullback_rate = _optional_float(suggestion.get("pullbackRate"))
                avg_net_return = _optional_float(suggestion.get("avgNetReturnPct"))
                avg_drawdown = _optional_float(suggestion.get("avgMaxDrawdownPct"))
                suggestion_type = str(suggestion.get("suggestionType") or "")
                reason = str(suggestion.get("suggestionReason") or suggestion.get("reason") or "")
                confidence = _optional_float(suggestion.get("confidence"))
                if existing:
                    connection.execute(
                        """
                        UPDATE t1_overnight_strategy_suggestions
                        SET feedback_id = ?,
                            pattern_label = ?,
                            market_state = ?,
                            candidate_type = ?,
                            risk_profile = ?,
                            sample_count = ?,
                            success_rate = ?,
                            strong_success_rate = ?,
                            low_open_fail_rate = ?,
                            pullback_rate = ?,
                            avg_net_return_pct = ?,
                            avg_max_drawdown_pct = ?,
                            suggestion_type = ?,
                            suggestion_reason = ?,
                            confidence = ?,
                            suggestion_json = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            feedback_value,
                            pattern_label,
                            market_state,
                            candidate_type,
                            risk_profile,
                            sample_count,
                            success_rate,
                            strong_success_rate,
                            low_open_fail_rate,
                            pullback_rate,
                            avg_net_return,
                            avg_drawdown,
                            suggestion_type,
                            reason,
                            confidence,
                            payload,
                            now,
                            int(existing["id"]),
                        ),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO t1_overnight_strategy_suggestions(
                          feedback_id, pattern_key, pattern_label, market_state, candidate_type,
                          risk_profile, sample_count, success_rate, strong_success_rate,
                          low_open_fail_rate, pullback_rate, avg_net_return_pct,
                          avg_max_drawdown_pct, suggestion_type, suggestion_action,
                          suggestion_reason, confidence, status, reviewer_note,
                          suggestion_json, created_at, updated_at, activated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', '', ?, ?, ?, '')
                        """,
                        (
                            feedback_value,
                            pattern_key,
                            pattern_label,
                            market_state,
                            candidate_type,
                            risk_profile,
                            sample_count,
                            success_rate,
                            strong_success_rate,
                            low_open_fail_rate,
                            pullback_rate,
                            avg_net_return,
                            avg_drawdown,
                            suggestion_type,
                            action,
                            reason,
                            confidence,
                            payload,
                            now,
                            now,
                        ),
                    )
                changed += 1
        return changed

    def list_t1_overnight_strategy_suggestions(self, limit: int = 50, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        allowed_statuses = [str(status) for status in (statuses or []) if str(status)]
        params: List[Any] = []
        status_filter = ""
        if allowed_statuses:
            placeholders = ",".join("?" for _ in allowed_statuses)
            status_filter = f"WHERE status IN ({placeholders})"
            params.extend(allowed_statuses)
        params.append(max(1, int(limit or 50)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM t1_overnight_strategy_suggestions
                {status_filter}
                ORDER BY
                  CASE status
                    WHEN 'active' THEN 0
                    WHEN 'draft' THEN 1
                    WHEN 'observing' THEN 2
                    WHEN 'rejected' THEN 3
                    WHEN 'revoked' THEN 4
                    ELSE 5
                  END,
                  updated_at DESC,
                  id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._t1_overnight_strategy_suggestion_from_row(row) for row in rows]

    def update_t1_overnight_strategy_suggestion_status(self, suggestion_id: int, status: str, reviewer_note: str = "") -> Optional[Dict[str, Any]]:
        normalized = str(status or "").strip()
        if normalized not in {"draft", "observing", "active", "rejected", "revoked"}:
            raise ValueError(f"unsupported suggestion status: {status}")
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM t1_overnight_strategy_suggestions WHERE id = ?",
                (int(suggestion_id),),
            ).fetchone()
            if not row:
                return None
            activated_at = row["activated_at"] or ""
            if normalized == "active" and not activated_at:
                activated_at = now
            if normalized in {"draft", "observing", "rejected", "revoked"} and row["status"] != "active":
                activated_at = row["activated_at"] or ""
            connection.execute(
                """
                UPDATE t1_overnight_strategy_suggestions
                SET status = ?,
                    reviewer_note = ?,
                    updated_at = ?,
                    activated_at = ?
                WHERE id = ?
                """,
                (normalized, str(reviewer_note or ""), now, activated_at, int(suggestion_id)),
            )
            updated = connection.execute(
                "SELECT * FROM t1_overnight_strategy_suggestions WHERE id = ?",
                (int(suggestion_id),),
            ).fetchone()
        return self._t1_overnight_strategy_suggestion_from_row(updated) if updated else None

    def latest_t1_overnight_strategy_feedback(self) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM t1_overnight_strategy_feedback
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._t1_overnight_strategy_feedback_from_row(row) if row else None

    def find_t1_overnight_runs_without_outcomes(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT run.*
                FROM t1_overnight_selection_runs run
                WHERE run.status = 'done'
                  AND run.final_count > 0
                  AND run.recommendation_date < ?
                  AND run.actual_source != 'mock'
                  AND run.actual_source NOT LIKE 'cache:%'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM t1_overnight_selection_outcomes outcome
                    WHERE outcome.run_id = run.id
                      AND outcome.outcome_label != ''
                      AND outcome.outcome_label != '数据不足'
                  )
                ORDER BY run.id DESC
                LIMIT ?
                """,
                (date.today().isoformat(), max(1, int(limit or 20))),
            ).fetchall()
        return [self._t1_overnight_run_from_row(row) for row in rows]

    def count_t1_overnight_zero_pick_runs_without_outcomes(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM t1_overnight_selection_runs run
                WHERE run.status = 'done'
                  AND run.final_count = 0
                  AND run.selection_mode IN ('market_risk_zero_pick', 'data_quality_zero_pick', 'guardrail_zero_pick')
                  AND run.recommendation_date < ?
                  AND run.actual_source != 'mock'
                  AND run.actual_source NOT LIKE 'cache:%'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM t1_overnight_selection_outcomes outcome
                    WHERE outcome.run_id = run.id
                      AND outcome.outcome_label != ''
                  )
                """,
                (date.today().isoformat(),),
            ).fetchone()
        return int(row["count"] or 0) if row else 0

    def _t1_overnight_run_summary_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        run = self._t1_overnight_run_from_row(row)
        return {
            "id": run["id"],
            "recommendationDate": run["recommendationDate"],
            "scanTime": run["scanTime"],
            "status": run["status"],
            "selectionMode": run["selectionMode"],
            "actualSource": run["actualSource"],
            "requestedSource": run["requestedSource"],
            "finalCount": run["finalCount"],
            "poolSize": run["poolSize"],
            "semifinalCount": run["semifinalCount"],
            "error": run["error"],
            "finishedAt": run["finishedAt"],
            "createdAt": run["createdAt"],
            "updatedAt": run["updatedAt"],
            "market": run["market"],
            "request": run["request"],
            "finalPicks": run["finalPicks"],
            "outcomeCount": int(row["outcome_count"] or 0),
            "verifiedOutcomeCount": int(row["verified_outcome_count"] or 0),
            "successCount": int(row["success_count"] or 0),
            "strongSuccessCount": int(row["strong_success_count"] or 0),
            "avgPrimaryNetReturnPct": _optional_float(row["avg_primary_net_return_pct"]),
            "latestVerifiedAt": row["latest_verified_at"] or "",
        }

    def _t1_overnight_run_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "recommendationDate": row["recommendation_date"],
            "scanTime": row["scan_time"],
            "riskMode": row["risk_mode"],
            "requestedSource": row["requested_source"],
            "actualSource": row["actual_source"],
            "evidenceRunId": int(row["evidence_run_id"] or 0) if _row_has(row, "evidence_run_id") else 0,
            "status": row["status"],
            "selectionMode": row["selection_mode"],
            "totalCount": int(row["total_count"] or 0),
            "hardFilteredCount": int(row["hard_filtered_count"] or 0),
            "poolSize": int(row["pool_size"] or 0),
            "batchSize": int(row["batch_size"] or 0),
            "batchPick": int(row["batch_pick"] or 0),
            "finalLimit": int(row["final_limit"] or 3),
            "batchCount": int(row["batch_count"] or 0),
            "completedBatchCount": int(row["completed_batch_count"] or 0),
            "semifinalCount": int(row["semifinal_count"] or 0),
            "finalCount": int(row["final_count"] or 0),
            "currentStage": row["current_stage"],
            "finalPicks": _json_or_default(row["final_picks_json"], []),
            "batchSummary": _json_or_default(row["batch_summary_json"], []),
            "request": _json_or_default(row["request_json"], {}),
            "market": _json_or_default(row["market_json"], {}),
            "error": row["error"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "heartbeatAt": row["heartbeat_at"],
            "leaseExpiresAt": row["lease_expires_at"],
            "workerId": row["worker_id"],
            "lastStageAt": row["last_stage_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _t1_overnight_evidence_run_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "recommendationDate": row["recommendation_date"],
            "scanTime": row["scan_time"],
            "requestedSource": row["requested_source"],
            "actualSource": row["actual_source"],
            "status": row["status"],
            "marketSource": row["market_source"],
            "sectorSource": row["sector_source"],
            "minuteSource": row["minute_source"],
            "marketSnapshotCount": int(row["market_snapshot_count"] or 0),
            "sectorCount": int(row["sector_count"] or 0),
            "constituentCount": int(row["constituent_count"] or 0),
            "stockMapCount": int(row["stock_map_count"] or 0),
            "minuteCount": int(row["minute_count"] or 0),
            "qualityStatus": row["quality_status"],
            "missing": _json_or_default(row["missing_json"], []),
            "request": _json_or_default(row["request_json"], {}),
            "market": _json_or_default(row["market_json"], {}),
            "error": row["error"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "heartbeatAt": row["heartbeat_at"],
            "leaseExpiresAt": row["lease_expires_at"],
            "workerId": row["worker_id"],
            "lastStageAt": row["last_stage_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _t1_overnight_stock_sector_map_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        evidence = _json_or_default(row["evidence_json"], {})
        evidence.setdefault("status", row["sector_evidence_status"])
        evidence.setdefault("sourceRunId", int(row["run_id"]))
        return {
            "runId": int(row["run_id"]),
            "code": row["code"],
            "name": row["name"],
            "listingBoard": row["listing_board"],
            "industrySector": row["industry_sector"],
            "conceptSectors": _json_or_default(row["concept_sectors_json"], []),
            "sectorEvidenceStatus": row["sector_evidence_status"],
            "sectorEvidence": evidence,
            "updatedAt": row["updated_at"],
        }

    def _t1_stock_sector_identity_cache_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "code": row["code"],
            "name": row["name"],
            "industrySector": row["industry_sector"],
            "conceptSectors": _json_or_default(row["concept_sectors_json"], []),
            "regionSector": row["region_sector"],
            "source": row["source"],
            "confidence": row["confidence"],
            "status": row["status"],
            "lastSuccessAt": row["last_success_at"],
            "updatedAt": row["updated_at"],
            "raw": _json_or_default(row["raw_json"], {}),
        }

    def _t1_overnight_minute_snapshot_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "runId": int(row["run_id"]),
            "code": row["code"],
            "period": row["period"],
            "source": row["source"],
            "pointCount": int(row["point_count"] or 0),
            "qualityStatus": row["quality_status"],
            "error": row["error"],
            "series": _json_or_default(row["series_json"], []),
            "updatedAt": row["updated_at"],
        }

    def _t1_overnight_validation_minute_snapshot_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "runId": int(row["run_id"]),
            "code": row["code"],
            "targetTradeDate": row["target_trade_date"],
            "period": row["period"],
            "source": row["source"],
            "pointCount": int(row["point_count"] or 0),
            "qualityStatus": row["quality_status"],
            "error": row["error"],
            "series": _json_or_default(row["series_json"], []),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _t1_overnight_item_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        stage = row["stage"]
        return {
            "runId": int(row["run_id"]),
            "code": row["code"],
            "name": row["name"],
            "poolRank": int(row["pool_rank"] or 0),
            "aiRank": int(row["ai_rank"] or 0),
            "batchIndex": int(row["batch_index"] or 0),
            "stage": stage,
            "selected": bool(row["selected"]) and stage != "pool",
            "candidateType": row["candidate_type"],
            "score": row["score"],
            "confidence": row["confidence"],
            "reason": row["reason"],
            "buyPlan": row["buy_plan"],
            "sellPlan": row["sell_plan"],
            "risk": row["risk"],
            "invalidCondition": row["invalid_condition"],
            "item": _json_or_default(row["item_json"], {}),
            "pick": _json_or_default(row["pick_json"], {}),
        }

    def _t1_overnight_outcome_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        payload = _json_or_default(row["outcome_json"], {})
        if _row_has(row, "run_actual_source"):
            payload.setdefault("actualSource", row["run_actual_source"])
        if _row_has(row, "run_selection_mode"):
            payload.setdefault("selectionMode", row["run_selection_mode"])
        if _row_has(row, "run_market_json"):
            market = _json_or_default(row["run_market_json"], {})
            payload.setdefault("sourceStatus", (market.get("sourceStatus") or {}) if isinstance(market, dict) else {})
        return {
            "runId": int(row["run_id"]),
            "code": row["code"],
            "name": row["name"],
            "recommendationDate": row["recommendation_date"],
            "nextTradeDate": row["next_trade_date"],
            "buyReferencePrice": row["buy_reference_price"],
            "nextOpen": row["next_open"],
            "nextHigh": row["next_high"],
            "nextLow": row["next_low"],
            "nextClose": row["next_close"],
            "openReturnPct": row["open_return_pct"],
            "highReturnPct": row["high_return_pct"],
            "closeReturnPct": row["close_return_pct"],
            "maxDrawdownPct": row["max_drawdown_pct"],
            "benchmarkReturnPct": row["benchmark_return_pct"],
            "excessReturnPct": row["excess_return_pct"],
            "outcomeLabel": row["outcome_label"],
            "outcomeScore": row["outcome_score"],
            "hitSellPlan": bool(row["hit_sell_plan"]),
            "hitInvalidCondition": bool(row["hit_invalid_condition"]),
            "failureReason": row["failure_reason"],
            "verifiedAt": row["verified_at"],
            "dataSource": row["data_source"],
            "outcome": payload,
        }

    def _t1_overnight_validation_scenario_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        payload = _json_or_default(row["scenario_json"], {})
        payload.update(
            {
                "runId": int(row["run_id"]),
                "code": row["code"],
                "validationVersion": row["validation_version"],
                "scenarioKey": row["scenario_key"],
                "scenarioLabel": row["scenario_label"],
                "sellTime": row["sell_time"],
                "sellPrice": row["sell_price"],
                "grossReturnPct": row["gross_return_pct"],
                "netReturnPct": row["net_return_pct"],
                "dataStatus": row["data_status"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
        )
        return payload

    def _t1_overnight_strategy_feedback_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        payload = _json_or_default(row["feedback_json"], {})
        payload.update(
            {
                "id": int(row["id"]),
                "feedbackDate": row["feedback_date"],
                "windowRuns": int(row["window_runs"] or 0),
                "verifiedOutcomes": int(row["verified_outcomes"] or 0),
                "successRate": row["success_rate"],
                "strongSuccessRate": row["strong_success_rate"],
                "lowOpenFailRate": row["low_open_fail_rate"],
                "pullbackRate": row["pullback_rate"],
                "avgHighReturnPct": row["avg_high_return_pct"],
                "avgCloseReturnPct": row["avg_close_return_pct"],
                "avgMaxDrawdownPct": row["avg_max_drawdown_pct"],
                "bestPatterns": _json_or_default(row["best_patterns_json"], []),
                "worstPatterns": _json_or_default(row["worst_patterns_json"], []),
                "promptSummary": row["prompt_summary"],
                "sourceRunId": int(row["source_run_id"] or 0),
                "triggerSource": row["trigger_source"],
                "createdAt": row["created_at"],
            }
        )
        return payload

    def _t1_overnight_strategy_suggestion_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        payload = _json_or_default(row["suggestion_json"], {})
        payload.update(
            {
                "id": int(row["id"]),
                "feedbackId": int(row["feedback_id"] or 0),
                "patternKey": row["pattern_key"],
                "patternLabel": row["pattern_label"],
                "marketState": row["market_state"],
                "candidateType": row["candidate_type"],
                "riskProfile": row["risk_profile"],
                "sampleCount": int(row["sample_count"] or 0),
                "successRate": row["success_rate"],
                "strongSuccessRate": row["strong_success_rate"],
                "lowOpenFailRate": row["low_open_fail_rate"],
                "pullbackRate": row["pullback_rate"],
                "avgNetReturnPct": row["avg_net_return_pct"],
                "avgMaxDrawdownPct": row["avg_max_drawdown_pct"],
                "suggestionType": row["suggestion_type"],
                "suggestionAction": row["suggestion_action"],
                "suggestionReason": row["suggestion_reason"],
                "confidence": row["confidence"],
                "status": row["status"],
                "reviewerNote": row["reviewer_note"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "activatedAt": row["activated_at"],
            }
        )
        return payload


def _chunks(values: List[str], size: int) -> List[List[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _normalize_date(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    text = text.replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if len(text) >= 10:
        return text[:10]
    return ""


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _result_type_label(value: str) -> str:
    return {
        "rule_baseline": "规则基线",
        "ai_reviewed": "AI 审查",
        "rules_fallback": "规则兜底",
        "ai_rejected": "AI 排除",
    }.get(value, value or "未知")


def _json_or_default(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, json.JSONDecodeError):
        return default


def _pick_official_t1_run_id(run_ids: List[int], run_meta: Dict[int, Dict[str, Any]]) -> int:
    def rank(run_id: int) -> tuple[int, int]:
        meta = run_meta.get(run_id) or {}
        market = meta.get("market") if isinstance(meta.get("market"), dict) else {}
        request = meta.get("request") if isinstance(meta.get("request"), dict) else {}
        if _t1_history_display_only(market, request):
            return (99, run_id)
        role = str((market.get("runRole") or request.get("runRole") or "")).strip()
        scan_time = str(meta.get("scanTime") or request.get("scanTime") or "")
        selection_mode = str(meta.get("selectionMode") or "")
        force = bool(request.get("force"))
        final_count = int(meta.get("finalCount") or 0)
        if role == "official":
            return (0, run_id)
        if final_count > 0 and not force and "14:30" <= scan_time <= "14:50" and selection_mode not in {"rules_fallback"}:
            return (1, run_id)
        if final_count > 0 and not force and "14:30" <= scan_time <= "14:50":
            return (2, run_id)
        if final_count > 0:
            return (3, run_id)
        return (9, run_id)

    ranked = sorted(run_ids, key=rank)
    return ranked[0] if ranked else 0


def _t1_history_display_only(market: Dict[str, Any], request: Dict[str, Any]) -> bool:
    treatment = {}
    if isinstance(market, dict):
        treatment = market.get("historicalTreatment") or {}
    if not treatment and isinstance(request, dict):
        treatment = request.get("historicalTreatment") or {}
    if not isinstance(treatment, dict):
        return False
    return bool(treatment.get("displayOnly") or treatment.get("learningEligible") is False and treatment.get("status") in {"legacy_display_only", "semantic_legacy_display_only", "zero_pick_cash_day"})


def _t1_outcome_history_display_only(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    treatment = payload.get("historicalTreatment") or {}
    if not isinstance(treatment, dict):
        return False
    return bool(treatment.get("displayOnly") or treatment.get("learningEligible") is False)


def _t1_run_role_from_json(market: Dict[str, Any], request: Dict[str, Any], scan_time: str) -> str:
    role = str((market.get("runRole") if isinstance(market, dict) else "") or (request.get("runRole") if isinstance(request, dict) else "") or "").strip()
    if role in {"official", "research", "legacy", "invalid"}:
        return role
    if isinstance(request, dict) and bool(request.get("force")):
        return "research"
    time_text = str(scan_time or (request.get("scanTime") if isinstance(request, dict) else "") or "")
    if "14:30" <= time_text <= "14:50":
        return "official"
    return "research"


def _row_has(row: sqlite3.Row, key: str) -> bool:
    try:
        return key in row.keys()
    except AttributeError:
        return False


def _lease_expiry(now_text: str, seconds: Optional[float] = None) -> str:
    lease_seconds = max(30.0, float(seconds or os.getenv("T1_RUN_LEASE_SECONDS", "900")))
    try:
        current = datetime.fromisoformat(now_text)
    except ValueError:
        current = datetime.now()
    return (current + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
