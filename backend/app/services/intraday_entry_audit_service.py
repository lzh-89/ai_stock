from __future__ import annotations

import http.client
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List

from backend.app.database import Database
from backend.app.services.ai_narrator import _chat_completions_url, _extract_chat_content, _http_error_message
from backend.app.services.stock_analysis_service import StockAnalysisService
from backend.app.settings import Settings


LOGGER = logging.getLogger("ai_stock.intraday_entry_audit")

AUDIT_VERSION = "entry_audit_v1"
AI_TIMEOUT_SECONDS = 60
MAX_NEW_POSITION_PCT = 20.0
MAX_DEFAULT_T1_POSITION_PCT = 10.0

ENTRY_STATUS_LABELS = {
    "block_entry": "禁止进场",
    "wait_confirm": "暂不进场，等待确认",
    "pullback_only": "只允许回踩低吸",
    "small_test": "可小仓试错",
    "watch_priority": "重点观察",
    "t1_only": "仅适合 T+1",
    "manual_review": "需人工确认",
}

ALLOWED_ENTRY_STATUSES = set(ENTRY_STATUS_LABELS)
ALLOWED_POSITION_LEVELS = {"none", "observe", "tiny_test", "small", "light", "hold_only", "reduce"}
EVIDENCE_KEY_LABELS = {
    "aiFullMarket": "背景 AI 全市场",
    "ai_full_market": "AI 全市场",
    "t1": "背景 T+1",
    "morningSectorRadar": "早盘主线雷达",
    "stockAnalysis": "个股分析",
    "modulePair": "审计链路",
    "triggerModule": "触发模块",
    "triggerSignal": "触发信号",
    "stockAnalysisSignal": "个股分析信号",
    "ai_full_market_plus_stock_analysis": "AI 全市场 + 个股分析",
    "t1_plus_stock_analysis": "T+1 + 个股分析",
    "watch_risk": "观察但风险偏高",
    "positive": "正向支持",
}
DISPLAY_TEXT_REPLACEMENTS = {
    "block_entry": "禁止进场",
    "wait_confirm": "暂不进场，等待确认",
    "pullback_only": "只允许回踩低吸",
    "small_test": "可小仓试错",
    "watch_priority": "重点观察",
    "t1_only": "仅适合 T+1",
    "manual_review": "需人工确认",
    "ai_full_market": "AI 全市场",
    "aiFullMarket": "背景 AI 全市场",
    "morningSectorRadar": "早盘主线雷达",
    "stockAnalysis": "个股分析",
    "modulePair": "审计链路",
    "triggerModule": "触发模块",
    "triggerSignal": "触发信号",
    "stockAnalysisSignal": "个股分析信号",
    "ai_full_market_plus_stock_analysis": "AI 全市场 + 个股分析",
    "t1_plus_stock_analysis": "T+1 + 个股分析",
    "watch_risk": "观察但风险偏高",
    "positive": "正向支持",
    "background_only": "仅作背景",
    "matched": "已匹配",
    "missing": "缺失",
    "not_matched": "未匹配",
    "deepseek_success": "DeepSeek 已完成",
    "deepseek_failed": "DeepSeek 未完成",
    "ai_not_configured": "AI 未配置",
    "not_started": "未开始",
    "none": "不参与",
    "observe": "只观察",
    "tiny_test": "极小仓试错",
    "small": "小仓",
    "light": "轻仓",
    "hold_only": "仅持有不加仓",
    "reduce": "减仓",
    "no_new_position": "不新增仓位",
    "new_small_test": "新开小仓试错",
    "AI全市场": "AI 全市场",
    "watch": "观察",
    "avoid": "回避",
    "buy": "买入",
}
FORBIDDEN_TEXT = (
    "必买",
    "一定涨",
    "稳赚",
    "满仓",
    "无风险",
    "确定上涨",
    "保证收益",
    "稳赚不赔",
    "无脑买入",
    "必然上涨",
    "确定性机会",
)

ENTRY_AUDITOR_ROLE = """你是 DeepSeek 当天进场审计主审计者，负责交易前风控审查。

你的任务是只基于输入 JSON 和 constraintCheck，重新审计触发模块与个股分析之间的证据冲突，判断今天是否允许进场，以及在什么条件、什么周期、什么仓位下才允许。

必须遵守：
1. 只能使用输入 JSON，不得编造行情、资金、新闻、公告、业绩或盘口事实。
2. 不得把 AI 全市场、T+1、个股分析、早盘主线雷达任一模块当作绝对真理。
3. AI 全市场入选不是自动可进场；T+1 入选不是自动可隔夜；个股分析 avoid 不是自动禁止进场。
4. 早盘主线雷达只能作为板块证据，不能单独决定买入或否决。
5. 必须解释触发模块与个股分析的冲突来源，包括周期、买点、风险、数据缺失或市场环境。
6. 如果个股分析 decision=avoid 且你仍输出非 block_entry，必须解释为什么仍可观察、低吸或小仓试错，并给出低仓位、触发条件和严格失效条件。
7. T+1 审计不能被包装成中线或长线买点；超过 10% 仓位必须有更强证据、明确次日处理计划和严格失效条件。
8. 新开仓建议不得超过 20%。
9. 禁止输出“必买”“一定涨”“稳赚”“满仓”“无风险”等确定性表达。
10. 必须输出严格 JSON，不要 Markdown，不要 JSON 外文字。"""


class IntradayEntryAuditService:
    def __init__(self, settings: Settings, database: Database, stock_analysis: StockAnalysisService):
        self.settings = settings
        self.database = database
        self.stock_analysis = stock_analysis

    def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        trade_date = now.split("T", 1)[0]
        code = _normalize_code(payload.get("code"))
        if not code:
            raise ValueError("缺少有效股票代码")
        name = str(payload.get("name") or "").strip()
        trigger_source = _normalize_trigger_source(payload.get("triggerSource", payload.get("trigger_source")))
        trigger_run_id = _parse_int(payload.get("triggerRunId", payload.get("trigger_run_id", 0)), 0)
        trigger_item_id = str(payload.get("triggerItemId", payload.get("trigger_item_id", code)) or code)
        scan_time = str(payload.get("scanTime", payload.get("scan_time", "")) or "")
        risk_mode = str(payload.get("riskMode", payload.get("risk_mode", "balanced")) or "balanced")
        market_source = str(payload.get("marketSource", payload.get("market_source", "auto")) or "auto")
        refresh_stock_analysis = bool(payload.get("refreshStockAnalysis", payload.get("refresh_stock_analysis", True)))
        position = _normalize_position(payload.get("position") or {})

        trigger = self._load_trigger_signal(trigger_source, code, trigger_run_id, trade_date)
        if not name:
            name = str((trigger.get("item") or {}).get("name") or trigger.get("name") or "")

        stock_analysis = self._load_or_generate_stock_analysis(
            code=code,
            risk_mode=risk_mode,
            scan_time=scan_time or "14:36",
            market_source=market_source,
            refresh=refresh_stock_analysis,
            position=position,
        )
        if not name:
            name = str(stock_analysis.get("name") or "")

        morning_sector = self._load_morning_sector_radar(code, trade_date, now)
        background = self._load_background_signals(trigger_source, code, trade_date)
        market = (trigger.get("run") or {}).get("market") or {}
        stock_evidence = (stock_analysis.get("evidence") or {}) if isinstance(stock_analysis.get("evidence"), dict) else {}

        entry_input = {
            "version": AUDIT_VERSION,
            "tradeDate": trade_date,
            "code": code,
            "name": name,
            "trigger": {
                "source": trigger_source,
                "runId": trigger.get("runId") or trigger_run_id or None,
                "itemId": trigger_item_id,
                "scanTime": scan_time or (trigger.get("run") or {}).get("scanTime") or "",
                "signalDate": trigger.get("signalDate") or trade_date,
            },
            "position": position,
            "signals": {
                "triggerModule": trigger,
                "stockAnalysis": stock_analysis,
                "morningSectorRadar": morning_sector,
            },
            "backgroundSignals": background,
            "market": _compact_value(market, max_items=50),
            "intraday": _compact_value(stock_evidence.get("intraday") or {}, max_items=80),
            "daily": _compact_value(stock_evidence.get("daily") or {}, max_items=80),
            "dataQuality": {},
        }
        data_quality = self._build_data_quality(entry_input)
        entry_input["dataQuality"] = data_quality
        constraint_check = self._build_constraint_check(entry_input)

        ai_audit: Dict[str, Any] = {}
        ai_status = "not_started"
        ai_failure_reason = ""
        if self.settings.enable_ai and self.settings.ai_api_key and self.settings.ai_base_url:
            ai_audit, ai_failure_reason = self._audit_with_ai(entry_input, constraint_check)
            ai_status = "deepseek_success" if ai_audit else "deepseek_failed"
        else:
            ai_status = "ai_not_configured"
            ai_failure_reason = "DeepSeek 未配置或 AI 功能未启用，当前未生成 AI 审计结论。"

        final_audit = self._normalize_final_audit(
            ai_audit=ai_audit,
            ai_status=ai_status,
            ai_failure_reason=ai_failure_reason,
            entry_input=entry_input,
            constraint_check=constraint_check,
            generated_at=now,
        )
        snapshot_id = self._save_snapshot(
            now=now,
            trade_date=trade_date,
            code=code,
            name=name,
            trigger_source=trigger_source,
            trigger_run_id=trigger.get("runId") or trigger_run_id or None,
            trigger_item_id=trigger_item_id,
            position=position,
            entry_input=entry_input,
            constraint_check=constraint_check,
            ai_audit=ai_audit,
            final_audit=final_audit,
            ai_status=ai_status,
            ai_failure_reason=ai_failure_reason,
        )
        final_audit["snapshotId"] = snapshot_id
        return final_audit

    def recent(self, code: str = "", limit: int = 20) -> Dict[str, Any]:
        normalized_code = _normalize_code(code)
        limit = max(1, min(100, int(limit or 20)))
        clauses = []
        params: List[Any] = []
        if normalized_code:
            clauses.append("code = ?")
            params.append(normalized_code)
        params.append(limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, code, name, trade_date, audited_at, trigger_source,
                       trigger_run_id, entry_status, position_level, position_pct,
                       position_action, ai_status, fallback_reason, final_audit_json
                FROM intraday_entry_audit_snapshots
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        items = []
        for row in rows:
            audit = _loads(row["final_audit_json"], {})
            display_status = _display_status(str(row["entry_status"] or ""), audit.get("displayStatus"))
            items.append(
                {
                    "snapshotId": int(row["id"]),
                    "code": row["code"],
                    "name": row["name"],
                    "tradeDate": row["trade_date"],
                    "auditedAt": row["audited_at"],
                    "triggerSource": row["trigger_source"],
                    "triggerRunId": row["trigger_run_id"],
                    "entryStatus": row["entry_status"],
                    "displayStatus": display_status,
                    "auditConclusion": _translate_display_text(str(audit.get("auditConclusion") or "")),
                    "positionLevel": row["position_level"],
                    "positionPct": row["position_pct"],
                    "positionAction": row["position_action"],
                    "aiStatus": row["ai_status"],
                    "fallbackReason": row["fallback_reason"],
                }
            )
        return {"code": normalized_code, "limit": limit, "count": len(items), "items": items}

    def history(self, code: str = "", entry_status: str = "", limit: int = 100) -> Dict[str, Any]:
        normalized_code = _normalize_code(code)
        normalized_status = str(entry_status or "").strip()
        limit = max(1, min(500, int(limit or 100)))
        clauses = []
        params: List[Any] = []
        if normalized_code:
            clauses.append("s.code = ?")
            params.append(normalized_code)
        if normalized_status in ALLOWED_ENTRY_STATUSES:
            clauses.append("s.entry_status = ?")
            params.append(normalized_status)
        params.append(limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT s.*
                FROM intraday_entry_audit_snapshots s
                {where}
                ORDER BY s.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            snapshot_ids = [int(row["id"]) for row in rows]
            outcomes_by_snapshot: Dict[int, List[Dict[str, Any]]] = {snapshot_id: [] for snapshot_id in snapshot_ids}
            if snapshot_ids:
                placeholders = ",".join("?" for _ in snapshot_ids)
                outcome_rows = connection.execute(
                    f"""
                    SELECT *
                    FROM intraday_entry_audit_outcomes
                    WHERE snapshot_id IN ({placeholders})
                    ORDER BY snapshot_id DESC, horizon
                    """,
                    snapshot_ids,
                ).fetchall()
                for row in outcome_rows:
                    outcomes_by_snapshot.setdefault(int(row["snapshot_id"]), []).append(_outcome_from_row(row))
                feedback_rows = connection.execute(
                    f"""
                    SELECT *
                    FROM intraday_entry_audit_feedback
                    WHERE snapshot_id IN ({placeholders})
                    """,
                    snapshot_ids,
                ).fetchall()
                feedback_by_snapshot = {int(row["snapshot_id"]): _feedback_from_row(row) for row in feedback_rows}
            else:
                feedback_by_snapshot = {}
        items = []
        for row in rows:
            audit = _loads(row["final_audit_json"], {})
            snapshot_id = int(row["id"])
            items.append(
                {
                    "snapshotId": snapshot_id,
                    "code": row["code"],
                    "name": row["name"],
                    "tradeDate": row["trade_date"],
                    "auditedAt": row["audited_at"],
                    "triggerSource": row["trigger_source"],
                    "triggerRunId": row["trigger_run_id"],
                    "entryStatus": row["entry_status"],
                    "displayStatus": _display_status(str(row["entry_status"] or ""), audit.get("displayStatus")),
                    "auditConclusion": _translate_display_text(str(audit.get("auditConclusion") or "")),
                    "positionLevel": row["position_level"],
                    "positionPct": row["position_pct"],
                    "positionAction": row["position_action"],
                    "aiStatus": row["ai_status"],
                    "dataQuality": audit.get("dataQuality") or {},
                    "outcomes": outcomes_by_snapshot.get(snapshot_id, []),
                    "feedback": feedback_by_snapshot.get(snapshot_id, {}),
                }
            )
        return {"code": normalized_code, "entryStatus": normalized_status, "limit": limit, "count": len(items), "items": items}

    def save_feedback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        snapshot_id = _parse_int(payload.get("snapshotId", payload.get("snapshot_id", 0)), 0)
        if snapshot_id <= 0:
            raise ValueError("缺少有效审计快照 ID")
        user_action = _normalize_feedback_action(payload.get("userAction", payload.get("user_action", "")))
        now = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            snapshot = connection.execute(
                """
                SELECT id, code, name, trade_date
                FROM intraday_entry_audit_snapshots
                WHERE id = ?
                """,
                (snapshot_id,),
            ).fetchone()
            if not snapshot:
                raise ValueError("审计快照不存在")
            connection.execute(
                """
                INSERT INTO intraday_entry_audit_feedback(
                  snapshot_id, code, trade_date, user_action, actual_buy_date,
                  actual_buy_price, actual_buy_pct, actual_sell_date, actual_sell_price,
                  note, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                  user_action=excluded.user_action,
                  actual_buy_date=excluded.actual_buy_date,
                  actual_buy_price=excluded.actual_buy_price,
                  actual_buy_pct=excluded.actual_buy_pct,
                  actual_sell_date=excluded.actual_sell_date,
                  actual_sell_price=excluded.actual_sell_price,
                  note=excluded.note,
                  updated_at=excluded.updated_at
                """,
                (
                    snapshot_id,
                    snapshot["code"],
                    snapshot["trade_date"],
                    user_action,
                    str(payload.get("actualBuyDate", payload.get("actual_buy_date", "")) or "").strip(),
                    _optional_float(payload.get("actualBuyPrice", payload.get("actual_buy_price"))),
                    _optional_float(payload.get("actualBuyPct", payload.get("actual_buy_pct"))),
                    str(payload.get("actualSellDate", payload.get("actual_sell_date", "")) or "").strip(),
                    _optional_float(payload.get("actualSellPrice", payload.get("actual_sell_price"))),
                    str(payload.get("note") or "").strip()[:800],
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM intraday_entry_audit_feedback WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        return {"snapshotId": snapshot_id, "feedback": _feedback_from_row(row)}

    def batch_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        trade_date = datetime.now().date().isoformat()
        trigger_source = _normalize_trigger_source(payload.get("triggerSource", payload.get("trigger_source", "ai_full_market")))
        trigger_run_id = _parse_int(payload.get("triggerRunId", payload.get("trigger_run_id", 0)), 0)
        limit = max(1, min(10, _parse_int(payload.get("limit", 3), 3)))
        selected_only = bool(payload.get("selectedOnly", payload.get("selected_only", True)))
        allow_historical = bool(payload.get("allowHistorical", payload.get("allow_historical", False)))
        if trigger_source == "ai_full_market":
            run = self._load_ai_full_market_run(trigger_run_id, trade_date) if trigger_run_id > 0 else self._load_ai_full_market_run(0, trade_date)
        else:
            run = self._load_t1_run(trigger_run_id, trade_date) if trigger_run_id > 0 else self._load_t1_run(0, trade_date)
        if not run:
            return {
                "tradeDate": trade_date,
                "triggerSource": trigger_source,
                "runId": trigger_run_id or None,
                "doneCount": 0,
                "skipReason": "未找到当天触发模块 run，批量审计已跳过；如需历史候选，请先在对应历史结果卡片上单票审计。",
                "items": [],
            }
        signal_date = str(run.get("recommendationDate") or run.get("tradeDate") or str(run.get("createdAt") or "").split("T", 1)[0] or "")
        if signal_date != trade_date and not allow_historical:
            return {
                "tradeDate": trade_date,
                "triggerSource": trigger_source,
                "runId": run.get("id"),
                "signalDate": signal_date,
                "doneCount": 0,
                "skipReason": f"最近 run 信号日期为 {signal_date}，不是今天 {trade_date}，批量审计已跳过。",
                "items": [],
            }
        candidates = []
        for item in run.get("items") or []:
            if selected_only and not bool(item.get("selected", True)):
                continue
            code = _normalize_code(item.get("code"))
            if code:
                candidates.append(item)
            if len(candidates) >= limit:
                break
        results = []
        for item in candidates:
            try:
                result = self.run(
                    {
                        **payload,
                        "code": item.get("code"),
                        "name": item.get("name"),
                        "triggerSource": trigger_source,
                        "triggerRunId": int(run.get("id") or 0),
                        "triggerItemId": item.get("id") or item.get("code"),
                        "scanTime": payload.get("scanTime") or run.get("scanTime") or "",
                    }
                )
                results.append(
                    {
                        "code": result.get("code"),
                        "name": result.get("name"),
                        "snapshotId": result.get("snapshotId"),
                        "entryStatus": result.get("entryStatus"),
                        "displayStatus": result.get("displayStatus"),
                        "aiStatus": result.get("aiStatus"),
                    }
                )
            except Exception as error:  # noqa: BLE001 - batch should continue with remaining symbols.
                results.append({"code": item.get("code"), "name": item.get("name"), "error": str(error)})
        return {
            "tradeDate": trade_date,
            "triggerSource": trigger_source,
            "runId": run.get("id"),
            "signalDate": signal_date,
            "requestedCount": len(candidates),
            "doneCount": len([item for item in results if item.get("snapshotId")]),
            "items": results,
        }

    def refresh_outcomes(
        self,
        snapshot_id: int = 0,
        limit: int = 100,
        horizons: List[str] | None = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        requested_horizons = _normalize_outcome_horizons(horizons)
        limit = max(1, min(500, int(limit or 100)))
        clauses = []
        params: List[Any] = []
        if snapshot_id > 0:
            clauses.append("id = ?")
            params.append(int(snapshot_id))
        params.append(limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM intraday_entry_audit_snapshots
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        results = []
        for row in rows:
            snapshot = dict(row)
            series = self._daily_series(str(snapshot.get("code") or ""))
            for horizon in requested_horizons:
                outcome = _build_outcome(snapshot, series, horizon)
                results.append(outcome)
                if not dry_run:
                    self._upsert_outcome(outcome)
        done_count = len([item for item in results if item.get("status") == "done"])
        no_data_count = len([item for item in results if item.get("status") == "no_data"])
        pending_count = len([item for item in results if item.get("status") == "pending"])
        return {
            "dryRun": bool(dry_run),
            "snapshotCount": len(rows),
            "rowCount": len(results),
            "doneCount": done_count,
            "noDataCount": no_data_count,
            "pendingCount": pending_count,
            "horizons": requested_horizons,
            "rows": results[:200],
        }

    def _daily_series(self, code: str) -> List[Dict[str, Any]]:
        normalized_code = _normalize_code(code)
        if not normalized_code:
            return []
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, open, high, low, close
                FROM daily_kline
                WHERE code = ?
                ORDER BY trade_date
                """,
                (normalized_code,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _upsert_outcome(self, outcome: Dict[str, Any]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO intraday_entry_audit_outcomes(
                  snapshot_id, code, trade_date, horizon, entry_triggered, entry_date,
                  entry_price, exit_date, exit_price, return_pct, max_drawdown_pct,
                  max_runup_pct, hit_invalid_condition, hit_trigger_condition, status,
                  label, reason, outcome_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id, horizon) DO UPDATE SET
                  code=excluded.code,
                  trade_date=excluded.trade_date,
                  entry_triggered=excluded.entry_triggered,
                  entry_date=excluded.entry_date,
                  entry_price=excluded.entry_price,
                  exit_date=excluded.exit_date,
                  exit_price=excluded.exit_price,
                  return_pct=excluded.return_pct,
                  max_drawdown_pct=excluded.max_drawdown_pct,
                  max_runup_pct=excluded.max_runup_pct,
                  hit_invalid_condition=excluded.hit_invalid_condition,
                  hit_trigger_condition=excluded.hit_trigger_condition,
                  status=excluded.status,
                  label=excluded.label,
                  reason=excluded.reason,
                  outcome_json=excluded.outcome_json,
                  updated_at=excluded.updated_at
                """,
                (
                    int(outcome.get("snapshotId") or 0),
                    str(outcome.get("code") or ""),
                    str(outcome.get("tradeDate") or ""),
                    str(outcome.get("horizon") or ""),
                    1 if outcome.get("entryTriggered") else 0,
                    str(outcome.get("entryDate") or ""),
                    _optional_float(outcome.get("entryPrice")),
                    str(outcome.get("exitDate") or ""),
                    _optional_float(outcome.get("exitPrice")),
                    _optional_float(outcome.get("returnPct")),
                    _optional_float(outcome.get("maxDrawdownPct")),
                    _optional_float(outcome.get("maxRunupPct")),
                    1 if outcome.get("hitInvalidCondition") else 0,
                    1 if outcome.get("hitTriggerCondition") else 0,
                    str(outcome.get("status") or "pending"),
                    str(outcome.get("label") or ""),
                    str(outcome.get("reason") or ""),
                    json.dumps(outcome, ensure_ascii=False),
                    now,
                ),
            )

    def _load_trigger_signal(self, source: str, code: str, run_id: int, trade_date: str) -> Dict[str, Any]:
        if source == "ai_full_market":
            run = self._load_ai_full_market_run(run_id, trade_date)
            item = _find_item((run or {}).get("items") or [], code)
            return _trigger_payload("ai_full_market", run, item, code)
        if source == "t1":
            run = self._load_t1_run(run_id, trade_date)
            item = _find_item((run or {}).get("items") or [], code)
            return _trigger_payload("t1", run, item, code)
        raise ValueError("triggerSource 只能是 ai_full_market 或 t1")

    def _load_background_signals(self, trigger_source: str, code: str, trade_date: str) -> Dict[str, Any]:
        background: Dict[str, Any] = {}
        if trigger_source == "ai_full_market":
            run = self._load_t1_run(0, trade_date)
            background["t1"] = _trigger_payload("t1", run, _find_item((run or {}).get("items") or [], code), code, optional=True)
        else:
            run = self._load_ai_full_market_run(0, trade_date)
            background["aiFullMarket"] = _trigger_payload("ai_full_market", run, _find_item((run or {}).get("items") or [], code), code, optional=True)
        return background

    def _load_ai_full_market_run(self, run_id: int, trade_date: str) -> Dict[str, Any]:
        if run_id > 0:
            run = self.database.load_ai_full_market_selection_run(run_id)
        else:
            with self.database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT *
                    FROM ai_full_market_selection_runs
                    WHERE date(created_at) = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (trade_date,),
                ).fetchone()
            run = self.database._ai_full_market_run_from_row(row) if row else None  # noqa: SLF001
        if not run:
            return {}
        run["items"] = self.database.load_ai_full_market_selection_items(int(run["id"]))
        return run

    def _load_t1_run(self, run_id: int, trade_date: str) -> Dict[str, Any]:
        if run_id > 0:
            run = self.database.load_t1_overnight_selection_run(run_id)
        else:
            with self.database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT *
                    FROM t1_overnight_selection_runs
                    WHERE recommendation_date = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (trade_date,),
                ).fetchone()
            run = self.database._t1_overnight_run_from_row(row) if row else None  # noqa: SLF001
        if not run:
            return {}
        run["items"] = self.database.load_t1_overnight_selection_items(int(run["id"]))
        return run

    def _load_or_generate_stock_analysis(
        self,
        *,
        code: str,
        risk_mode: str,
        scan_time: str,
        market_source: str,
        refresh: bool,
        position: Dict[str, Any],
    ) -> Dict[str, Any]:
        today = datetime.now().date().isoformat()
        snapshot = self._latest_stock_analysis_snapshot(code, today)
        if snapshot:
            snapshot["fresh"] = True
            snapshot["generatedByAudit"] = False
            return snapshot
        if not refresh:
            return {"code": code, "fresh": False, "generatedByAudit": False, "error": "缺少当天个股分析，且本次未允许自动生成。"}
        try:
            result = self.stock_analysis.analyze(
                code=code,
                risk_mode=risk_mode,
                scan_time=scan_time,
                use_ai=True,
                market_source=market_source,
                force_market_context=False,
                position_context=_stock_analysis_position_context(position),
            )
        except Exception as error:  # noqa: BLE001 - audit can still save an insufficient-evidence snapshot.
            LOGGER.warning("entry audit auto stock analysis failed code=%s error=%s", code, error)
            return {"code": code, "fresh": False, "generatedByAudit": True, "error": str(error)}
        return {
            "snapshotId": result.get("snapshotId"),
            "analysisVersion": result.get("analysisVersion"),
            "code": result.get("code") or code,
            "name": result.get("name") or "",
            "analyzedAt": result.get("generatedAt") or "",
            "analysisDate": today,
            "fresh": True,
            "generatedByAudit": True,
            "aiMode": result.get("aiMode") or "",
            "aiProvider": result.get("aiProvider") or "",
            "aiModel": result.get("aiModel") or "",
            "decision": result.get("decision") or {},
            "evidence": _compact_value(result.get("evidence") or {}, max_items=120),
            "ruleFallbackUsed": bool(result.get("ruleFallbackUsed")),
            "fallbackReason": result.get("fallbackReason") or "",
        }

    def _latest_stock_analysis_snapshot(self, code: str, trade_date: str) -> Dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM stock_analysis_snapshots_v2
                WHERE code = ? AND analysis_date = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (code, trade_date),
            ).fetchone()
        if not row:
            return {}
        decision = _loads(row["decision_json"], {})
        evidence = _loads(row["evidence_pack_json"], {})
        return {
            "snapshotId": int(row["id"]),
            "analysisVersion": row["analysis_version"],
            "code": row["code"],
            "name": row["name"],
            "analyzedAt": row["analyzed_at"],
            "analysisDate": row["analysis_date"],
            "aiMode": row["ai_mode"],
            "aiProvider": row["ai_provider"],
            "aiModel": row["ai_model"],
            "decision": decision,
            "evidence": _compact_value(evidence, max_items=120),
            "ruleFallbackUsed": bool(row["rule_fallback_used"]),
            "caseMemoryStatus": _loads(row["case_memory_status_json"], {}),
        }

    def _load_morning_sector_radar(self, code: str, trade_date: str, audited_at: str) -> Dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM morning_sector_radar_runs
                WHERE trade_date = ? AND sector_type = 'all' AND status = 'done'
                ORDER BY
                  CASE scan_time WHEN '10:00' THEN 0 WHEN '09:45' THEN 1 WHEN '10:30' THEN 2 ELSE 3 END,
                  id DESC
                LIMIT 1
                """,
                (trade_date,),
            ).fetchone()
        if not row:
            return {"available": False, "missingReason": "缺少当天早盘主线雷达结果"}
        run = self.database._morning_sector_radar_run_from_row(row)  # noqa: SLF001
        sectors = self.database.load_morning_sector_radar_sectors(int(run["id"]))
        constituents = self.database.load_morning_sector_radar_constituents(int(run["id"]))
        matched = [item for item in constituents if _normalize_code(item.get("code")) == code]
        sector_map = {(item.get("sectorType"), item.get("sectorCode")): item for item in sectors}
        matched_sectors = []
        for item in matched[:8]:
            sector = sector_map.get((item.get("sectorType"), item.get("sectorCode"))) or {}
            matched_sectors.append(
                {
                    "sectorCode": item.get("sectorCode"),
                    "sectorType": item.get("sectorType"),
                    "sectorName": sector.get("sectorName") or "",
                    "finalRank": sector.get("finalRank"),
                    "selected": bool(sector.get("selected")),
                    "activeScore": sector.get("activityScore"),
                    "role": item.get("candidateRole"),
                    "sectorRank": item.get("sectorRank"),
                    "reason": item.get("reason"),
                    "risk": item.get("risk"),
                    "sectorRisks": sector.get("risks") or [],
                    "aiAction": sector.get("aiAction") or "",
                    "aiReason": sector.get("aiReason") or "",
                }
            )
        radar_dt = _combine_dt(trade_date, str(run.get("scanTime") or ""))
        audited_dt = _parse_dt(audited_at)
        stale = bool(radar_dt and audited_dt and (audited_dt - radar_dt).total_seconds() > 4 * 3600)
        return {
            "available": True,
            "runId": run.get("id"),
            "tradeDate": run.get("tradeDate"),
            "scanTime": run.get("scanTime"),
            "stale": stale,
            "matched": bool(matched_sectors),
            "matchedSectors": matched_sectors,
            "market": _compact_value(run.get("market") or {}, max_items=30),
            "dataQualityStatus": run.get("dataQualityStatus") or "",
        }

    def _build_data_quality(self, entry_input: Dict[str, Any]) -> Dict[str, Any]:
        trigger = entry_input.get("signals", {}).get("triggerModule") or {}
        stock = entry_input.get("signals", {}).get("stockAnalysis") or {}
        radar = entry_input.get("signals", {}).get("morningSectorRadar") or {}
        core_missing = []
        optional_missing = []
        if not trigger.get("matched"):
            core_missing.append("核心证据缺失：触发模块结果未匹配该股票")
        if not stock.get("fresh"):
            core_missing.append("核心证据缺失：缺少当天个股分析")
        if not radar.get("available"):
            optional_missing.append("可选板块证据缺失：未找到当天早盘主线雷达结果，不影响主链路审计")
        if not entry_input.get("intraday"):
            core_missing.append("核心证据缺失：缺少当前分时/盘中证据")
        trigger_signal_date = str(entry_input.get("trigger", {}).get("signalDate") or "")
        audit_trade_date = str(entry_input.get("tradeDate") or "")
        date_warning = ""
        if trigger_signal_date and audit_trade_date and trigger_signal_date != audit_trade_date:
            date_warning = f"当前用 {audit_trade_date} 的数据审计 {trigger_signal_date} 的历史候选，不是复盘当日结论。"
        has_current_quote = bool((entry_input.get("intraday") or {}).get("price") or (entry_input.get("intraday") or {}).get("lastPrice"))
        stock_status = "已使用当天已有个股分析"
        if stock.get("generatedByAudit") and stock.get("fresh"):
            stock_status = "本次自动生成了当天个股分析"
        elif not stock.get("fresh") and stock.get("error"):
            stock_status = f"自动生成个股分析失败：{stock.get('error')}"
        elif not stock.get("fresh"):
            stock_status = "缺少当天个股分析"
        sector_status = "未运行"
        if radar.get("available") and radar.get("stale"):
            sector_status = "已过期，仅作背景"
        elif radar.get("available"):
            sector_status = "可用"
        return {
            "stockAnalysisFresh": bool(stock.get("fresh")),
            "stockAnalysisGeneratedByAudit": bool(stock.get("generatedByAudit")),
            "stockAnalysisStatus": stock_status,
            "triggerSignalMatched": bool(trigger.get("matched")),
            "triggerSignalDate": trigger_signal_date,
            "auditTradeDate": audit_trade_date,
            "dateWarning": date_warning,
            "sectorRadarFresh": bool(radar.get("available")) and not bool(radar.get("stale")),
            "sectorRadarStale": bool(radar.get("stale")),
            "sectorRadarStatus": sector_status,
            "hasCurrentQuote": has_current_quote or bool(entry_input.get("intraday")),
            "coreMissingEvidence": _unique_text(core_missing),
            "optionalMissingEvidence": _unique_text(optional_missing),
            "missingEvidence": _unique_text(core_missing + optional_missing),
        }

    def _build_constraint_check(self, entry_input: Dict[str, Any]) -> Dict[str, Any]:
        data_quality = entry_input.get("dataQuality") or {}
        stock_decision = ((entry_input.get("signals", {}).get("stockAnalysis") or {}).get("decision") or {})
        decision = str(stock_decision.get("decision") or "").lower()
        risks = []
        if decision == "avoid":
            risks.append("个股分析为 avoid：这是高权重风险证据，但不作为系统一票否决。")
        if data_quality.get("sectorRadarStale"):
            risks.append("早盘主线雷达距审计时间超过 4 小时，仅可作为过期背景证据。")
        return {
            "version": AUDIT_VERSION,
            "isBusinessVerdict": False,
            "missingEvidence": data_quality.get("missingEvidence") or [],
            "coreMissingEvidence": data_quality.get("coreMissingEvidence") or [],
            "optionalMissingEvidence": data_quality.get("optionalMissingEvidence") or [],
            "riskEvidenceNotes": risks,
            "outputConstraints": {
                "maxNewPositionPct": MAX_NEW_POSITION_PCT,
                "defaultMaxT1PositionPct": MAX_DEFAULT_T1_POSITION_PCT,
                "mustUseOnlyProvidedEvidence": True,
                "mustExplainAvoidIfNotBlock": True,
                "mustExplainMissingEvidence": True,
                "cannotPromiseProfit": True,
            },
            "notes": [
                "系统约束层只检查数据完整性、交易可执行性和输出安全边界，不做买卖裁决。",
                "AI/T+1 入选、个股 avoid、板块强弱都只能作为证据交给 DeepSeek。",
            ],
        }

    def _audit_with_ai(self, entry_input: Dict[str, Any], constraint_check: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {"role": "system", "content": ENTRY_AUDITOR_ROLE},
                {
                    "role": "user",
                    "content": (
                        "请基于 entryAuditInput 与 constraintCheck 输出严格 JSON。"
                        "根字段必须包含 entryStatus、displayStatus、auditConclusion、confidence、positionLevel、positionPct、positionAction、"
                        "allowChasing、allowPullbackEntry、applicableHorizon、triggerConditions、invalidConditions、opportunityEvidence、riskEvidence、"
                        "conflictExplanation、modulePair、backgroundSignalsUsed、missingEvidence、riskWarnings。"
                        "entryStatus 只能是 block_entry/wait_confirm/pullback_only/small_test/watch_priority/t1_only/manual_review。"
                        "positionLevel 只能是 none/observe/tiny_test/small/light/hold_only/reduce。"
                        "请特别说明触发模块与个股分析之间的冲突，且不得因为任一模块单独通过或否决。\n"
                        f"{json.dumps({'entryAuditInput': entry_input, 'constraintCheck': constraint_check}, ensure_ascii=False)}"
                    ),
                },
            ],
            "temperature": 0.12,
            "max_tokens": 4000,
            "stream": False,
        }
        if self.settings.ai_provider in {"deepseek", "openai"}:
            payload["response_format"] = {"type": "json_object"}
        if self.settings.ai_provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        last_error = ""
        for attempt in range(1, 3):
            started = time.perf_counter()
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
                with urllib.request.urlopen(request, timeout=AI_TIMEOUT_SECONDS) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                return {}, _http_error_message(error)
            except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.IncompleteRead, json.JSONDecodeError) as error:
                last_error = f"{self.settings.ai_provider} 进场审计调用失败：{error}"
                LOGGER.warning("entry audit ai failed attempt=%s elapsed_ms=%.0f error=%s", attempt, (time.perf_counter() - started) * 1000, error)
                continue
            audit = _extract_json_object(_extract_chat_content(data))
            if audit:
                return audit, ""
            last_error = f"{self.settings.ai_provider} 返回内容不是有效 JSON"
        return {}, last_error

    def _normalize_final_audit(
        self,
        *,
        ai_audit: Dict[str, Any],
        ai_status: str,
        ai_failure_reason: str,
        entry_input: Dict[str, Any],
        constraint_check: Dict[str, Any],
        generated_at: str,
    ) -> Dict[str, Any]:
        data_quality = entry_input.get("dataQuality") or {}
        missing = list(data_quality.get("missingEvidence") or [])
        normalization_warnings: List[str] = []
        if ai_status != "deepseek_success":
            final = _fallback_audit(ai_failure_reason, missing)
        else:
            final = dict(ai_audit)
        status = str(final.get("entryStatus") or "").strip()
        if status not in ALLOWED_ENTRY_STATUSES:
            normalization_warnings.append("DeepSeek entryStatus 缺失或不在枚举内，已降级为人工确认。")
            status = "manual_review"
        position_level = str(final.get("positionLevel") or "").strip()
        if position_level not in ALLOWED_POSITION_LEVELS:
            normalization_warnings.append("DeepSeek positionLevel 缺失或不在枚举内，已设为 observe。")
            position_level = "observe" if status != "block_entry" else "none"
        position_pct = _optional_float(final.get("positionPct"))
        if position_pct is None:
            position_pct = 0.0
        position_pct = max(0.0, min(MAX_NEW_POSITION_PCT, float(position_pct)))
        if not data_quality.get("stockAnalysisFresh") or not data_quality.get("triggerSignalMatched"):
            position_pct = 0.0
            if status not in {"manual_review", "watch_priority"}:
                normalization_warnings.append("核心证据不足，最终状态已降级为人工确认。")
                status = "manual_review"
        if entry_input.get("trigger", {}).get("source") == "t1" and position_pct > MAX_DEFAULT_T1_POSITION_PCT:
            text_pool = " ".join(_text_array(final.get("opportunityEvidence")) + _text_array(final.get("invalidConditions")) + [str(final.get("positionAction") or ""), str(final.get("auditConclusion") or "")])
            if "次日" not in text_pool or len(_text_array(final.get("invalidConditions"))) < 2:
                position_pct = MAX_DEFAULT_T1_POSITION_PCT
                normalization_warnings.append("T+1 仓位超过 10% 但缺少次日处理计划或严格失效条件，已压回 10%。")
        stock_decision = (((entry_input.get("signals") or {}).get("stockAnalysis") or {}).get("decision") or {})
        if str(stock_decision.get("decision") or "").lower() == "avoid" and status != "block_entry":
            explanation_pool = " ".join(
                _text_array(final.get("riskEvidence"))
                + _text_array(final.get("conflictExplanation"))
                + _text_array(final.get("auditConclusion"))
            )
            has_avoid_explanation = "avoid" in explanation_pool.lower() or "回避" in explanation_pool or "个股分析" in explanation_pool
            if not has_avoid_explanation or len(_text_array(final.get("invalidConditions"))) < 1:
                status = "manual_review"
                position_pct = 0.0
                normalization_warnings.append("个股分析为 avoid 但 DeepSeek 未充分解释非禁止结论，已降级为人工确认。")
        sanitized = _sanitize_forbidden_text(final)
        if sanitized.get("_sanitized"):
            normalization_warnings.append("审计文案含确定性交易措辞，已做安全替换。")
        final = sanitized.get("value") if isinstance(sanitized.get("value"), dict) else final
        final["entryStatus"] = status
        final["displayStatus"] = _display_status(status, final.get("displayStatus"))
        final["positionLevel"] = position_level
        final["positionPct"] = round(position_pct, 2)
        final["positionAction"] = str(final.get("positionAction") or ("no_new_position" if position_pct <= 0 else "new_small_test"))
        final["allowChasing"] = bool(final.get("allowChasing")) and position_pct > 0
        final["allowPullbackEntry"] = bool(final.get("allowPullbackEntry")) and status in {"pullback_only", "small_test", "t1_only"}
        final["applicableHorizon"] = _text_array(final.get("applicableHorizon")) or (["T+1"] if entry_input.get("trigger", {}).get("source") == "t1" else ["intraday"])
        final["triggerConditions"] = _text_array(final.get("triggerConditions"))
        final["invalidConditions"] = _text_array(final.get("invalidConditions"))
        final["opportunityEvidence"] = _text_array(final.get("opportunityEvidence"))
        final["riskEvidence"] = _text_array(final.get("riskEvidence"))
        final["conflictExplanation"] = _translate_display_text(str(final.get("conflictExplanation") or "DeepSeek 未提供明确冲突解释。" if ai_status == "deepseek_success" else "DeepSeek 未完成，当前为证据不足降级。"))
        final["modulePair"] = final.get("modulePair") if isinstance(final.get("modulePair"), dict) else _module_pair(entry_input)
        final["backgroundSignalsUsed"] = _text_array(final.get("backgroundSignalsUsed"))
        if not final["backgroundSignalsUsed"]:
            final["backgroundSignalsUsed"] = _background_signal_summary(entry_input)
        final["missingEvidence"] = _unique_text(_text_array(final.get("missingEvidence")) + missing)
        final["dataQuality"] = data_quality
        final["actionSummary"] = _action_summary(status, final, data_quality)
        final["timeline"] = _timeline(entry_input, generated_at)
        final["chainSummary"] = _chain_summary(entry_input, data_quality)
        final["evidenceDetails"] = _evidence_details(entry_input)
        final["aiStatus"] = ai_status
        final["aiProvider"] = self.settings.ai_provider if ai_status == "deepseek_success" else ""
        final["aiModel"] = self.settings.ai_model if ai_status == "deepseek_success" else ""
        final["fallbackReason"] = "" if ai_status == "deepseek_success" else ai_failure_reason
        final["generatedAt"] = generated_at
        final["code"] = entry_input.get("code")
        final["name"] = entry_input.get("name")
        final["auditConclusion"] = _translate_display_text(str(final.get("auditConclusion") or ("DeepSeek 未完成，当前证据不足，不能生成 AI 进场审计结论。" if ai_status != "deepseek_success" else "审计结论缺失，需人工确认。")))
        final["constraintCheck"] = constraint_check
        final["normalizationWarnings"] = _unique_text(_text_array(final.get("normalizationWarnings")) + normalization_warnings + _schema_warnings(final))
        return final

    def _save_snapshot(
        self,
        *,
        now: str,
        trade_date: str,
        code: str,
        name: str,
        trigger_source: str,
        trigger_run_id: int | None,
        trigger_item_id: str,
        position: Dict[str, Any],
        entry_input: Dict[str, Any],
        constraint_check: Dict[str, Any],
        ai_audit: Dict[str, Any],
        final_audit: Dict[str, Any],
        ai_status: str,
        ai_failure_reason: str,
    ) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO intraday_entry_audit_snapshots(
                  audit_version, code, name, trade_date, audited_at, trigger_source, trigger_run_id,
                  trigger_item_id, position_state, position_json, input_json, constraint_check_json,
                  ai_audit_json, final_audit_json, entry_status, position_level, position_pct,
                  position_action, ai_status, ai_provider, ai_model, ai_failure_reason,
                  fallback_reason, data_quality_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    AUDIT_VERSION,
                    code,
                    name,
                    trade_date,
                    now,
                    trigger_source,
                    trigger_run_id,
                    trigger_item_id,
                    position.get("state") or "none",
                    json.dumps(position, ensure_ascii=False),
                    json.dumps(entry_input, ensure_ascii=False),
                    json.dumps(constraint_check, ensure_ascii=False),
                    json.dumps(ai_audit or {}, ensure_ascii=False),
                    json.dumps(final_audit, ensure_ascii=False),
                    final_audit.get("entryStatus") or "",
                    final_audit.get("positionLevel") or "",
                    _optional_float(final_audit.get("positionPct")),
                    final_audit.get("positionAction") or "",
                    ai_status,
                    final_audit.get("aiProvider") or "",
                    final_audit.get("aiModel") or "",
                    ai_failure_reason or "",
                    final_audit.get("fallbackReason") or "",
                    json.dumps(final_audit.get("dataQuality") or {}, ensure_ascii=False),
                    now,
                ),
            )
        return int(cursor.lastrowid)


def _trigger_payload(source: str, run: Dict[str, Any], item: Dict[str, Any], code: str, optional: bool = False) -> Dict[str, Any]:
    if not run:
        return {"source": source, "matched": False, "optional": optional, "missingReason": "未找到当天触发模块 run"}
    merged = _merge_item(item) if item else {}
    return {
        "source": source,
        "runId": run.get("id"),
        "signalDate": run.get("recommendationDate") or (str(run.get("createdAt") or "").split("T", 1)[0]),
        "scanTime": run.get("scanTime") or "",
        "status": run.get("status") or "",
        "selectionMode": run.get("selectionMode") or "",
        "matched": bool(item),
        "optional": optional,
        "code": code,
        "name": merged.get("name") or item.get("name") if item else "",
        "item": _compact_value(merged, max_items=70),
        "market": _compact_value(run.get("market") or {}, max_items=35),
        "missingReason": "" if item else "触发模块 run 中未匹配该股票",
    }


def _merge_item(item: Dict[str, Any]) -> Dict[str, Any]:
    if not item:
        return {}
    return {**(item.get("item") or {}), **(item.get("pick") or {}), **{key: value for key, value in item.items() if key not in {"item", "pick"}}}


def _find_item(items: List[Dict[str, Any]], code: str) -> Dict[str, Any]:
    for item in items:
        if _normalize_code(item.get("code")) == code:
            return item
    return {}


def _module_pair(entry_input: Dict[str, Any]) -> Dict[str, Any]:
    trigger_source = entry_input.get("trigger", {}).get("source")
    stock_decision = ((entry_input.get("signals", {}).get("stockAnalysis") or {}).get("decision") or {}).get("decision")
    return {
        "type": "ai_full_market_plus_stock_analysis" if trigger_source == "ai_full_market" else "t1_plus_stock_analysis",
        "triggerSignal": "matched" if (entry_input.get("signals", {}).get("triggerModule") or {}).get("matched") else "missing",
        "stockAnalysisSignal": stock_decision or "missing",
        "morningSectorRadar": "matched" if (entry_input.get("signals", {}).get("morningSectorRadar") or {}).get("matched") else "background_only",
    }


def _background_signal_summary(entry_input: Dict[str, Any]) -> List[str]:
    result = []
    background = entry_input.get("backgroundSignals") or {}
    for key, value in background.items():
        if not isinstance(value, dict):
            continue
        label = "背景 T+1" if key == "t1" else "背景 AI 全市场"
        if value.get("matched"):
            result.append(f"{label}：匹配该股票，信号日期 {value.get('signalDate') or '--'}，仅作背景证据")
        else:
            result.append(f"{label}：未匹配该股票，仅说明缺少背景共振，不影响主链路审计")
    radar = (entry_input.get("signals") or {}).get("morningSectorRadar") or {}
    if radar.get("available") and radar.get("matched"):
        sectors = radar.get("matchedSectors") or []
        names = [str(item.get("sectorName") or item.get("sectorCode") or "").strip() for item in sectors if isinstance(item, dict)]
        result.append(f"早盘主线雷达：匹配板块 {('、'.join(names[:3]) if names else '已匹配')}，只作板块背景证据")
    elif radar.get("available"):
        result.append("早盘主线雷达：当天有结果，但该股票未进入雷达前排/跟随样本")
    else:
        result.append("早盘主线雷达：未找到当天结果，属于可选板块证据缺失")
    return result


def _timeline(entry_input: Dict[str, Any], generated_at: str) -> Dict[str, Any]:
    trigger = entry_input.get("trigger") or {}
    stock = (entry_input.get("signals") or {}).get("stockAnalysis") or {}
    radar = (entry_input.get("signals") or {}).get("morningSectorRadar") or {}
    return {
        "auditGeneratedAt": generated_at,
        "auditTradeDate": entry_input.get("tradeDate") or "",
        "triggerSource": trigger.get("source") or "",
        "triggerRunId": trigger.get("runId"),
        "triggerSignalDate": trigger.get("signalDate") or "",
        "triggerScanTime": trigger.get("scanTime") or "",
        "stockAnalysisDate": stock.get("analysisDate") or "",
        "stockAnalyzedAt": stock.get("analyzedAt") or "",
        "stockAnalysisGeneratedByAudit": bool(stock.get("generatedByAudit")),
        "sectorRadarDate": radar.get("tradeDate") or "",
        "sectorRadarTime": radar.get("scanTime") or "",
        "sectorRadarStatus": "缺失" if not radar.get("available") else ("过期" if radar.get("stale") else "可用"),
    }


def _chain_summary(entry_input: Dict[str, Any], data_quality: Dict[str, Any]) -> List[Dict[str, Any]]:
    trigger = (entry_input.get("signals") or {}).get("triggerModule") or {}
    stock = (entry_input.get("signals") or {}).get("stockAnalysis") or {}
    radar = (entry_input.get("signals") or {}).get("morningSectorRadar") or {}
    trigger_source = (entry_input.get("trigger") or {}).get("source") or ""
    result = [
        {
            "label": "触发模块",
            "status": "已匹配" if trigger.get("matched") else "未匹配",
            "detail": f"{_trigger_source_label(trigger_source)} · 信号日期 {trigger.get('signalDate') or '--'} · run {trigger.get('runId') or '--'}",
        },
        {
            "label": "个股分析",
            "status": "当天结果" if stock.get("fresh") else "缺失或生成失败",
            "detail": data_quality.get("stockAnalysisStatus") or "",
        },
        {
            "label": "早盘主线雷达",
            "status": data_quality.get("sectorRadarStatus") or "缺失",
            "detail": "可选板块证据，只作共振/退潮/过热背景，不单独决定买入或否决。",
        },
        {
            "label": "DeepSeek 主审计",
            "status": "参与审计",
            "detail": "基于触发模块与个股分析重新解释冲突、条件、仓位和失效。",
        },
    ]
    if data_quality.get("dateWarning"):
        result.insert(
            0,
            {
                "label": "日期口径",
                "status": "历史信号",
                "detail": data_quality.get("dateWarning"),
            },
        )
    return result


def _evidence_details(entry_input: Dict[str, Any]) -> Dict[str, Any]:
    trigger = (entry_input.get("signals") or {}).get("triggerModule") or {}
    stock = (entry_input.get("signals") or {}).get("stockAnalysis") or {}
    radar = (entry_input.get("signals") or {}).get("morningSectorRadar") or {}
    background = entry_input.get("backgroundSignals") or {}
    decision = stock.get("decision") or {}
    trigger_item = trigger.get("item") or {}
    return {
        "triggerModule": {
            "title": _trigger_source_label(trigger.get("source") or (entry_input.get("trigger") or {}).get("source")),
            "signalDate": trigger.get("signalDate") or "",
            "scanTime": trigger.get("scanTime") or "",
            "matched": bool(trigger.get("matched")),
            "reason": trigger_item.get("reason") or trigger_item.get("aiSelectionReason") or "",
            "risk": trigger_item.get("risk") or trigger_item.get("aiSelectionRisk") or "",
            "entryCondition": trigger_item.get("entryCondition") or trigger_item.get("aiEntryCondition") or trigger_item.get("buyPlan") or "",
            "invalidCondition": trigger_item.get("invalidCondition") or trigger_item.get("aiInvalidCondition") or "",
            "score": trigger_item.get("score") or trigger_item.get("confidence") or "",
        },
        "stockAnalysis": {
            "title": "个股分析",
            "analysisDate": stock.get("analysisDate") or "",
            "analyzedAt": stock.get("analyzedAt") or "",
            "generatedByAudit": bool(stock.get("generatedByAudit")),
            "decision": decision.get("decision") or "",
            "primaryHorizon": decision.get("primaryHorizon") or "",
            "confidence": decision.get("confidence") or "",
            "summary": decision.get("summary") or decision.get("decisionText") or "",
            "risks": _text_array(decision.get("risks") or decision.get("riskWarnings") or []),
            "missingData": _text_array(decision.get("missingData") or []),
        },
        "morningSectorRadar": {
            "title": "早盘主线雷达",
            "available": bool(radar.get("available")),
            "status": "缺失" if not radar.get("available") else ("过期" if radar.get("stale") else "可用"),
            "scanTime": radar.get("scanTime") or "",
            "matched": bool(radar.get("matched")),
            "matchedSectors": radar.get("matchedSectors") or [],
        },
        "backgroundSignals": {
            key: {
                "title": _trigger_source_label(value.get("source") or key),
                "matched": bool(value.get("matched")),
                "signalDate": value.get("signalDate") or "",
                "scanTime": value.get("scanTime") or "",
                "reason": ((value.get("item") or {}).get("reason") or ""),
            }
            for key, value in background.items()
            if isinstance(value, dict)
        },
    }


def _schema_warnings(final: Dict[str, Any]) -> List[str]:
    warnings = []
    required = [
        ("auditConclusion", "审计结论"),
        ("positionLevel", "仓位等级"),
        ("positionAction", "仓位动作"),
        ("applicableHorizon", "适用周期"),
        ("triggerConditions", "进场条件"),
        ("invalidConditions", "失效条件"),
        ("opportunityEvidence", "机会证据"),
        ("riskEvidence", "风险证据"),
        ("conflictExplanation", "冲突解释"),
    ]
    for key, label in required:
        value = final.get(key)
        if value is None or value == "" or value == []:
            warnings.append(f"DeepSeek 输出缺少{label}，已由后端保守补齐或留空展示。")
    if final.get("entryStatus") == "t1_only" and "T+1" not in " ".join(_text_array(final.get("applicableHorizon"))):
        warnings.append("T+1 审计结论未明确 T+1 周期，页面仅按隔夜机会理解。")
    return warnings


def _trigger_source_label(source: Any) -> str:
    value = str(source or "")
    if value == "ai_full_market":
        return "AI 全市场"
    if value == "t1":
        return "T+1"
    return _translate_display_text(value) or "--"


def _action_summary(status: str, final: Dict[str, Any], data_quality: Dict[str, Any]) -> str:
    if data_quality.get("dateWarning"):
        prefix = data_quality.get("dateWarning")
    else:
        prefix = ""
    base = {
        "block_entry": "当前结论是禁止进场，重点执行风险回避，不做新开仓。",
        "wait_confirm": "当前不是买点，先等待触发条件确认。",
        "pullback_only": "当前不允许追高，只能等待回踩低吸条件成立。",
        "small_test": "当前只适合小仓试错，必须严格执行失效条件。",
        "watch_priority": "当前值得重点观察，但还不构成明确进场动作。",
        "t1_only": "当前只按 T+1 隔夜机会处理，不解释成日线持有买点。",
        "manual_review": "当前证据不足或数据冲突，需要人工复核后再决策。",
    }.get(status, "当前需要人工复核。")
    pct = _optional_float(final.get("positionPct"))
    if pct is not None and pct > 0:
        base = f"{base} 建议仓位不超过 {pct:g}%。"
    if prefix:
        return f"{prefix} {base}"
    return base


def _display_status(status: str, raw_display_status: Any = "") -> str:
    display_status = str(raw_display_status or "").strip()
    if not display_status or display_status in ALLOWED_ENTRY_STATUSES:
        return ENTRY_STATUS_LABELS.get(status) or status
    return _translate_display_text(display_status)


def _fallback_audit(reason: str, missing: List[str]) -> Dict[str, Any]:
    return {
        "entryStatus": "manual_review",
        "displayStatus": ENTRY_STATUS_LABELS["manual_review"],
        "auditConclusion": "DeepSeek 未完成，当前未生成 AI 审计结论；核心证据不足时不能包装成可进场判断。",
        "confidence": "low",
        "positionLevel": "observe",
        "positionPct": 0,
        "positionAction": "no_new_position",
        "allowChasing": False,
        "allowPullbackEntry": False,
        "applicableHorizon": ["manual_review"],
        "triggerConditions": [],
        "invalidConditions": ["补齐当天个股分析、触发模块证据和当前行情后重新审计"],
        "opportunityEvidence": [],
        "riskEvidence": [reason or "DeepSeek 未返回有效审计 JSON"],
        "conflictExplanation": "DeepSeek 未完成，当前为证据不足降级。",
        "modulePair": {},
        "backgroundSignalsUsed": [],
        "missingEvidence": missing,
        "riskWarnings": [],
    }


def _normalize_outcome_horizons(horizons: List[str] | None) -> List[str]:
    if not horizons:
        return ["t1_next_day", "1d", "3d", "5d"]
    allowed = {"t1_next_day", "1d", "3d", "5d"}
    result = []
    for item in horizons:
        text = str(item or "").strip()
        if text in allowed and text not in result:
            result.append(text)
    return result or ["t1_next_day", "1d", "3d", "5d"]


def _build_outcome(snapshot: Dict[str, Any], series: List[Dict[str, Any]], horizon: str) -> Dict[str, Any]:
    snapshot_id = int(snapshot.get("id") or 0)
    code = str(snapshot.get("code") or "")
    trade_date = str(snapshot.get("trade_date") or "")
    final_audit = _loads(snapshot.get("final_audit_json"), {})
    entry_status = str(snapshot.get("entry_status") or final_audit.get("entryStatus") or "")
    position_pct = _optional_float(snapshot.get("position_pct"))
    entry_triggered = _entry_expected(entry_status, position_pct)
    base = {
        "snapshotId": snapshot_id,
        "code": code,
        "tradeDate": trade_date,
        "horizon": horizon,
        "entryTriggered": entry_triggered,
        "entryStatus": entry_status,
        "positionPct": position_pct,
        "hitInvalidCondition": False,
        "hitTriggerCondition": entry_triggered,
    }
    if not series:
        return {
            **base,
            "status": "no_data",
            "label": "数据不足",
            "reason": "缺少日 K 数据，暂不能回填审计表现。",
        }
    index = _series_index(series, trade_date)
    if index < 0:
        return {
            **base,
            "status": "no_data",
            "label": "数据不足",
            "reason": f"日 K 中未找到审计交易日 {trade_date}。",
        }
    offset = _horizon_offset(horizon)
    exit_index = index + offset
    if exit_index >= len(series):
        return {
            **base,
            "status": "pending",
            "label": "待回填",
            "reason": f"{horizon} 还缺少后续交易日数据。",
        }
    entry_bar = series[index]
    window = series[index + 1 : exit_index + 1] if offset > 0 else [series[index]]
    exit_bar = series[exit_index]
    entry_price = _optional_float(entry_bar.get("close"))
    exit_price = _optional_float(exit_bar.get("close"))
    if entry_price is None or entry_price <= 0 or exit_price is None:
        return {
            **base,
            "status": "no_data",
            "label": "数据不足",
            "reason": "缺少入场或退出参考价格。",
        }
    highs = [_optional_float(item.get("high")) for item in window]
    lows = [_optional_float(item.get("low")) for item in window]
    highs = [value for value in highs if value is not None]
    lows = [value for value in lows if value is not None]
    return_pct = (exit_price - entry_price) / entry_price * 100
    max_runup_pct = ((max(highs) - entry_price) / entry_price * 100) if highs else return_pct
    max_drawdown_pct = ((min(lows) - entry_price) / entry_price * 100) if lows else return_pct
    label, reason = _outcome_label(entry_status, entry_triggered, return_pct, max_drawdown_pct, max_runup_pct)
    return {
        **base,
        "status": "done",
        "label": label,
        "reason": reason,
        "entryDate": str(entry_bar.get("trade_date") or trade_date),
        "entryPrice": round(entry_price, 4),
        "exitDate": str(exit_bar.get("trade_date") or ""),
        "exitPrice": round(exit_price, 4),
        "returnPct": round(return_pct, 4),
        "maxDrawdownPct": round(max_drawdown_pct, 4),
        "maxRunupPct": round(max_runup_pct, 4),
    }


def _entry_expected(entry_status: str, position_pct: float | None) -> bool:
    if entry_status in {"small_test", "t1_only"}:
        return True
    if entry_status == "pullback_only":
        return bool(position_pct and position_pct > 0)
    return False


def _series_index(series: List[Dict[str, Any]], trade_date: str) -> int:
    for index, item in enumerate(series):
        if str(item.get("trade_date") or item.get("date") or "") == trade_date:
            return index
    return -1


def _horizon_offset(horizon: str) -> int:
    if horizon == "t1_next_day":
        return 1
    if horizon.endswith("d"):
        try:
            return max(1, int(horizon[:-1]))
        except ValueError:
            return 1
    return 1


def _outcome_label(entry_status: str, entry_triggered: bool, return_pct: float, max_drawdown_pct: float, max_runup_pct: float) -> tuple[str, str]:
    if entry_status == "block_entry":
        if return_pct <= -3 or max_drawdown_pct <= -5:
            return "规避有效", "审计禁止进场后，后续表现走弱或出现明显回撤。"
        if return_pct >= 5 or max_runup_pct >= 8:
            return "可能错过", "审计禁止进场后，后续出现明显上涨，需要复查阻断理由。"
        return "规避中性", "审计禁止进场后，后续表现没有明显机会或风险。"
    if not entry_triggered:
        if return_pct >= 5:
            return "观察错过", "审计未给明确进场仓位，但后续出现上涨。"
        return "观察合理", "审计保持观察后，后续没有出现强验证机会。"
    if return_pct >= 5 or max_runup_pct >= 8:
        return "审计有效", "审计允许进场后，后续收益或盘中浮盈验证较好。"
    if return_pct <= -3 or max_drawdown_pct <= -5:
        return "审计失败", "审计允许进场后，后续亏损或回撤较大。"
    return "审计中性", "审计允许进场后，后续表现不强不弱。"


def _outcome_from_row(row: Any) -> Dict[str, Any]:
    payload = _loads(row["outcome_json"], {})
    payload.update(
        {
            "id": int(row["id"]),
            "snapshotId": int(row["snapshot_id"]),
            "code": row["code"],
            "tradeDate": row["trade_date"],
            "horizon": row["horizon"],
            "entryTriggered": bool(row["entry_triggered"]),
            "entryDate": row["entry_date"],
            "entryPrice": row["entry_price"],
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
    )
    return payload


def _feedback_from_row(row: Any) -> Dict[str, Any]:
    if not row:
        return {}
    return {
        "id": int(row["id"]),
        "snapshotId": int(row["snapshot_id"]),
        "code": row["code"],
        "tradeDate": row["trade_date"],
        "userAction": row["user_action"],
        "actualBuyDate": row["actual_buy_date"],
        "actualBuyPrice": row["actual_buy_price"],
        "actualBuyPct": row["actual_buy_pct"],
        "actualSellDate": row["actual_sell_date"],
        "actualSellPrice": row["actual_sell_price"],
        "note": row["note"],
        "updatedAt": row["updated_at"],
    }


def _normalize_feedback_action(value: Any) -> str:
    action = str(value or "").strip()
    allowed = {"", "adopted", "not_adopted", "bought", "abandoned", "watching"}
    if action not in allowed:
        raise ValueError("反馈状态只能是 adopted/not_adopted/bought/abandoned/watching")
    return action


def _stock_analysis_position_context(position: Dict[str, Any]) -> Dict[str, Any]:
    if position.get("state") != "holding":
        return {"positionState": "none", "positionInfo": {}, "canSellToday": False, "scope": "entry_audit", "source": "user_input"}
    info = {
        "costPrice": position.get("costPrice"),
        "positionPct": position.get("positionPct"),
        "holdingDays": position.get("holdingDays"),
        "plannedHorizon": position.get("plannedHorizon"),
    }
    return {"positionState": "existing", "positionInfo": info, "canSellToday": True, "scope": "entry_audit", "source": "user_input"}


def _normalize_position(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = str(raw.get("state") or raw.get("positionState") or "none").strip().lower()
    if state in {"existing", "hold", "held"}:
        state = "holding"
    if state not in {"none", "holding", "planned"}:
        state = "none"
    return {
        "state": state,
        "costPrice": _optional_float(raw.get("costPrice", raw.get("cost_price"))),
        "positionPct": _optional_float(raw.get("positionPct", raw.get("position_pct"))) or 0,
        "holdingDays": int(_optional_float(raw.get("holdingDays", raw.get("holding_days"))) or 0),
        "plannedHorizon": str(raw.get("plannedHorizon", raw.get("planned_horizon", "short")) or "short"),
    }


def _normalize_trigger_source(value: Any) -> str:
    source = str(value or "").strip().lower()
    if source in {"ai", "ai_full", "full_market"}:
        source = "ai_full_market"
    if source in {"t+1", "t1_overnight"}:
        source = "t1"
    if source not in {"ai_full_market", "t1"}:
        raise ValueError("triggerSource 只能是 ai_full_market 或 t1")
    return source


def _extract_json_object(content: str) -> Dict[str, Any]:
    if not content:
        return {}
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s*```$", "", text).strip()
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


def _sanitize_forbidden_text(value: Any) -> Dict[str, Any]:
    changed = False

    def walk(item: Any) -> Any:
        nonlocal changed
        if isinstance(item, str):
            text = item
            for word in FORBIDDEN_TEXT:
                if word in text:
                    changed = True
                    text = text.replace(word, "条件满足后再评估")
            return text
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, dict):
            return {key: walk(child) for key, child in item.items()}
        return item

    return {"value": walk(value), "_sanitized": changed}


def _compact_value(value: Any, max_items: int = 80) -> Any:
    if isinstance(value, list):
        return [_compact_value(item, max_items=max_items) for item in value[:max_items]]
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= max_items:
                result["_truncated"] = True
                break
            if key in {"stocks", "items", "pool", "candidates"} and isinstance(child, list):
                result[key] = [_compact_value(item, max_items=30) for item in child[:30]]
            else:
                result[key] = _compact_value(child, max_items=max_items)
        return result
    return value


def _text_array(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        result: List[str] = []
        for item in value:
            result.extend(_text_array(item))
        return result
    if isinstance(value, dict):
        result = []
        for key, child in value.items():
            label = EVIDENCE_KEY_LABELS.get(str(key), str(key))
            child_text = _text_array(child)
            if child_text:
                result.append(f"{label}：{'；'.join(child_text)}")
            else:
                result.append(label)
        return result
    text = str(value).strip()
    return [_translate_display_text(text)] if text else []


def _translate_display_text(text: str) -> str:
    result = str(text or "")
    if result in DISPLAY_TEXT_REPLACEMENTS:
        return DISPLAY_TEXT_REPLACEMENTS[result]
    for raw, label in sorted(DISPLAY_TEXT_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        result = result.replace(raw, label)
    return result


def _unique_text(items: List[str]) -> List[str]:
    result = []
    seen = set()
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _loads(text: Any, default: Any) -> Any:
    try:
        return json.loads(text or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _normalize_code(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits[-6:].zfill(6) if digits else ""


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace("％", "")
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _combine_dt(trade_date: str, scan_time: str) -> datetime | None:
    if not trade_date or not scan_time:
        return None
    try:
        return datetime.fromisoformat(f"{trade_date}T{scan_time}:00")
    except ValueError:
        return None
