from __future__ import annotations

import http.client
import copy
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.app.database import Database
from backend.app.services.ai_narrator import _chat_completions_url, _extract_chat_content, _http_error_message
from backend.app.settings import Settings


LOGGER = logging.getLogger("ai_stock.cross_module_validation")

DEFAULT_LIMIT = 600
ALIGNMENT_WINDOW_DAYS = 7
REVIEW_CASE_LIMIT = 28
REVIEW_TIMEOUT_SECONDS = 70
REVIEW_MAX_TOKENS = 2800


class CrossModuleValidationService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    def summary(self, *, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        return self._build_response(limit=limit, include_cases=False)

    def conflicts(self, *, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        data = self._build_response(limit=limit, include_cases=True, case_limit=None)
        cases = [item for item in data["cases"] if item.get("patternGroup") == "conflict"]
        return {
            "generatedAt": data["generatedAt"],
            "scope": data["scope"],
            "rowCount": len(cases),
            "cases": cases[: max(1, min(300, int(limit or DEFAULT_LIMIT)))],
            "conflictStats": data["conflictStats"],
        }

    def confluence(self, *, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        data = self._build_response(limit=limit, include_cases=True, case_limit=None)
        cases = [item for item in data["cases"] if item.get("patternGroup") == "confluence"]
        return {
            "generatedAt": data["generatedAt"],
            "scope": data["scope"],
            "rowCount": len(cases),
            "cases": cases[: max(1, min(300, int(limit or DEFAULT_LIMIT)))],
            "confluenceStats": data["confluenceStats"],
        }

    def module_matrix(self, *, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        data = self._build_response(limit=limit, include_cases=False)
        return {
            "generatedAt": data["generatedAt"],
            "scope": data["scope"],
            "moduleMatrix": data["moduleMatrix"],
            "marketStateStats": data["marketStateStats"],
        }

    def refresh(self, *, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        data = self._build_response(limit=limit, include_cases=True)
        data["refreshed"] = True
        data["refreshMode"] = "read_existing_outcomes"
        return data

    def review(self, *, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        data = self._build_response(limit=limit, include_cases=True, case_limit=None)
        audit_pack = _audit_pack(data)
        review = self._review_with_ai(audit_pack)
        if not review:
            review = _fallback_review(audit_pack, ai_available=self._ai_available())
        return {
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "source": "deepseek" if review.get("aiMode") == "deepseek" else review.get("aiMode", "rules"),
            "scope": data["scope"],
            "auditPack": audit_pack,
            "review": review,
        }

    def _build_response(self, *, limit: int, include_cases: bool, case_limit: Optional[int] = None) -> Dict[str, Any]:
        max_rows = max(50, min(3000, int(limit or DEFAULT_LIMIT)))
        events = self._aligned_events(limit=max_rows)
        cases = [_classify_event(event) for event in events]
        cases.sort(key=lambda item: (item.get("tradeDate") or "", item.get("code") or ""), reverse=True)
        summary = _summary(cases)
        conflict_stats = _pattern_stats(cases, group="conflict")
        confluence_stats = _pattern_stats(cases, group="confluence")
        generated_at = datetime.now().isoformat(timespec="seconds")
        return {
            "generatedAt": generated_at,
            "version": "cross_module_validation_v1",
            "scope": {
                "limit": max_rows,
                "sampleCount": len(cases),
                "dateFrom": _date_range(cases)[0],
                "dateTo": _date_range(cases)[1],
                "source": "existing_module_outcomes",
                "writePolicy": "read_only_no_new_table",
                "aiReviewMode": "manual",
                "alignmentPolicy": f"exact_same_day_first_nearest_stock_analysis_within_{ALIGNMENT_WINDOW_DAYS}d",
            },
            "summary": summary,
            "moduleMatrix": _module_matrix(cases),
            "conflictStats": conflict_stats,
            "confluenceStats": confluence_stats,
            "marketStateStats": _market_state_stats(cases),
            "outcomeBasisStats": _outcome_basis_stats(cases),
            "ruleSuggestions": _rule_suggestions(cases, conflict_stats, confluence_stats),
            "warnings": _warnings(cases),
            "cases": cases[: (case_limit if case_limit is not None else len(cases))] if include_cases else [],
        }

    def _aligned_events(self, *, limit: int) -> List[Dict[str, Any]]:
        events: Dict[str, Dict[str, Any]] = {}
        for row in self._ai_full_market_rows(limit=limit * 3):
            key = _event_key(row.get("recommendationDate"), row.get("code"))
            if not key:
                continue
            event = events.setdefault(key, _base_event(row.get("recommendationDate"), row.get("code"), row.get("name")))
            event["aiFullMarket"] = _merge_ai_signal(event.get("aiFullMarket"), row)
            _fill_market_state(event, row.get("marketState") or row.get("market_state") or _market_state_from_json(row.get("runMarket")))
        for row in self._t1_rows(limit=limit):
            key = _event_key(row.get("recommendationDate"), row.get("code"))
            if not key:
                continue
            event = events.setdefault(key, _base_event(row.get("recommendationDate"), row.get("code"), row.get("name")))
            event["t1"] = _merge_t1_signal(event.get("t1"), row)
            _fill_market_state(event, _market_state_from_json(row.get("runMarket")))
        for row in self._stock_rows(limit=limit * 3):
            key = _event_key(row.get("analysisDate"), row.get("code"))
            if not key:
                continue
            event = events.setdefault(key, _base_event(row.get("analysisDate"), row.get("code"), row.get("name")))
            event["stockAnalysis"] = _merge_stock_signal(event.get("stockAnalysis"), row)
            _fill_market_state(event, _market_state_from_json(row.get("evidencePack")))
        for row in self._stock_trade_rows(limit=limit):
            key = _event_key(row.get("recommendationDate"), row.get("code"))
            if not key or key not in events:
                continue
            signal = events[key].setdefault("stockAnalysis", {"present": True, "outcomes": {}, "tradeOutcomes": []})
            signal.setdefault("tradeOutcomes", []).append(_stock_trade_outcome(row))
        _attach_nearest_stock_analysis(events)
        return list(events.values())

    def _ai_full_market_rows(self, *, limit: int) -> List[Dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                  o.run_id, o.code, o.name, o.recommendation_date, o.profile,
                  o.risk_mode, o.selection_mode, o.selected, o.candidate_type,
                  o.rule_rank, o.ai_rank, o.horizon, o.return_pct,
                  o.excess_return_pct, o.max_drawdown_pct, o.max_runup_pct,
                  o.status, o.label, o.reason, o.market_state,
                  o.outcome_json, i.reason AS item_reason, i.risk AS item_risk,
                  i.focus AS item_focus, i.pick_json, i.item_json,
                  r.market_json, r.created_at, r.finished_at
                FROM ai_full_market_selection_outcomes o
                LEFT JOIN ai_full_market_selection_items i ON i.run_id = o.run_id AND i.code = o.code
                LEFT JOIN ai_full_market_selection_runs r ON r.id = o.run_id
                WHERE o.horizon IN (1, 3, 5)
                ORDER BY o.recommendation_date DESC, o.run_id DESC, o.selected DESC, o.horizon DESC, o.ai_rank ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [_ai_row(row) for row in rows]

    def _t1_rows(self, *, limit: int) -> List[Dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                  o.run_id, o.code, o.name, o.recommendation_date, o.next_trade_date,
                  o.open_return_pct, o.high_return_pct, o.close_return_pct,
                  o.max_drawdown_pct, o.excess_return_pct, o.outcome_label,
                  o.outcome_score, o.hit_sell_plan, o.hit_invalid_condition,
                  o.failure_reason, o.verified_at, o.data_source, o.outcome_json,
                  i.selected, i.candidate_type, i.score, i.confidence,
                  i.reason AS item_reason, i.buy_plan, i.sell_plan, i.risk AS item_risk,
                  i.invalid_condition, i.pick_json, i.item_json,
                  r.market_json, r.selection_mode, r.scan_time
                FROM t1_overnight_selection_outcomes o
                LEFT JOIN t1_overnight_selection_items i ON i.run_id = o.run_id AND i.code = o.code
                LEFT JOIN t1_overnight_selection_runs r ON r.id = o.run_id
                ORDER BY o.recommendation_date DESC, o.run_id DESC, COALESCE(i.selected, 0) DESC, o.outcome_score DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [_t1_row(row) for row in rows]

    def _stock_rows(self, *, limit: int) -> List[Dict[str, Any]]:
        max_rows = max(1, int(limit))
        query = """
            SELECT
              s.id AS snapshot_id, s.code, s.name, s.analysis_date, s.analyzed_at,
              s.risk_mode, s.scan_time, s.market_source, s.ai_mode,
              s.ai_provider, s.ai_model, s.decision, s.primary_horizon,
              s.confidence, s.rule_fallback_used, s.evidence_pack_json,
              s.decision_json, o.horizon, o.buy_date, o.buy_price,
              o.exit_date, o.exit_price, o.return_pct, o.max_drawdown_pct,
              o.max_runup_pct, o.status, o.label, o.reason, o.updated_at
            FROM {snapshots_table} s
            LEFT JOIN {outcomes_table} o ON o.snapshot_id = s.id
            ORDER BY s.analysis_date DESC, s.id DESC, o.horizon ASC
            LIMIT ?
        """
        rows = []
        with self.database.connect() as connection:
            rows.extend(
                connection.execute(
                    query.format(snapshots_table="stock_analysis_snapshots_v2", outcomes_table="stock_analysis_outcomes_v2"),
                    (max_rows,),
                ).fetchall()
            )
            rows.extend(
                connection.execute(
                    query.format(snapshots_table="stock_analysis_snapshots", outcomes_table="stock_analysis_outcomes"),
                    (max_rows,),
                ).fetchall()
            )
        normalized = [_stock_row(row) for row in rows]
        normalized.sort(key=lambda item: (str(item.get("analysisDate") or ""), int(item.get("snapshotId") or 0)), reverse=True)
        return normalized[:max_rows]

    def _stock_trade_rows(self, *, limit: int) -> List[Dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                  s.id AS snapshot_id, s.code, s.name, s.analysis_date,
                  o.recommendation_date, o.risk_mode, o.plan_version,
                  o.horizon_type, o.entry_triggered, o.entry_date, o.entry_price,
                  o.exit_date, o.exit_price, o.return_pct, o.max_drawdown_pct,
                  o.max_runup_pct, o.status, o.label, o.reason, o.updated_at
                FROM stock_analysis_trade_outcomes_v2 o
                JOIN stock_analysis_snapshots_v2 s ON s.id = o.snapshot_id
                ORDER BY o.recommendation_date DESC, o.updated_at DESC, o.snapshot_id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [_stock_trade_row(row) for row in rows]

    def _ai_available(self) -> bool:
        return bool(self.settings.enable_ai and self.settings.ai_api_key and self.settings.ai_base_url)

    def _review_with_ai(self, audit_pack: Dict[str, Any]) -> Dict[str, Any]:
        if not self._ai_available():
            return {}
        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是 A 股交易系统跨模块复盘审查员。你只审计输入中的三个功能结果："
                        "AI 全市场、T+1、个股分析，以及它们的后验表现。"
                        "不得编造股票、行情、新闻或未提供的数据。不得改标签、不得改配置、不得自动调参。"
                        "你要指出冲突来自周期目标不一致、数据不足、机会发现过激、买点审查过严、"
                        "隔夜策略独立有效或市场状态影响等哪类原因。输出严格 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "请基于这个跨模块审计包输出 JSON，根字段必须包含："
                        "reviewConclusion、confidence、keyFindings、optimizationDrafts、missingEvidence、riskWarnings。"
                        "keyFindings 每项包含 pattern、finding、evidence、affectedModules、risk。"
                        "optimizationDrafts 每项包含 targetModule、actionType、suggestion、requiresUserConfirmation、blockedAutoApplyReason。"
                        "只允许提出待确认建议，不允许写成已经生效。"
                        f"\n{json.dumps(audit_pack, ensure_ascii=False)}"
                    ),
                },
            ],
            "temperature": 0.1,
            "max_tokens": REVIEW_MAX_TOKENS,
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
            with urllib.request.urlopen(request, timeout=REVIEW_TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = _extract_chat_content(data)
            review = _extract_json_object(content)
            if isinstance(review, dict) and review:
                review["aiMode"] = self.settings.ai_provider
                review["aiModel"] = self.settings.ai_model
                review["elapsedMs"] = int((time.perf_counter() - started) * 1000)
                return review
        except urllib.error.HTTPError as error:
            LOGGER.warning("cross module review ai http error=%s", _http_error_message(error))
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
            LOGGER.warning("cross module review ai failed error=%s", error)
        return {}


def _base_event(trade_date: Any, code: Any, name: Any) -> Dict[str, Any]:
    return {
        "tradeDate": str(trade_date or "")[:10],
        "code": str(code or "").zfill(6),
        "name": str(name or ""),
        "marketState": "unknown",
        "aiFullMarket": {"present": False},
        "t1": {"present": False},
        "stockAnalysis": {"present": False},
    }


def _attach_nearest_stock_analysis(events: Dict[str, Dict[str, Any]]) -> None:
    stock_events_by_code: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for event in events.values():
        stock = event.get("stockAnalysis") or {}
        if not stock.get("present"):
            continue
        stock.setdefault(
            "alignment",
            {
                "mode": "exact_same_day",
                "analysisDate": event.get("tradeDate"),
                "signalDate": event.get("tradeDate"),
                "dateGapDays": 0,
                "comparable": True,
            },
        )
        stock_events_by_code.setdefault(str(event.get("code") or ""), []).append((str(event.get("tradeDate") or ""), event))

    for event in events.values():
        stock = event.get("stockAnalysis") or {}
        if stock.get("present"):
            continue
        if not (event.get("aiFullMarket", {}).get("present") or event.get("t1", {}).get("present")):
            continue
        match = _nearest_stock_event(event, stock_events_by_code.get(str(event.get("code") or ""), []))
        if not match:
            continue
        analysis_date, stock_event = match
        source_signal = stock_event.get("stockAnalysis") or {}
        signal = copy.deepcopy(source_signal)
        gap_days = _date_gap_days(event.get("tradeDate"), analysis_date)
        signal["alignment"] = {
            "mode": "nearby_stock_analysis",
            "analysisDate": analysis_date,
            "signalDate": event.get("tradeDate"),
            "dateGapDays": gap_days,
            "comparable": gap_days == 0,
        }
        event["stockAnalysis"] = signal
        if not event.get("name") and stock_event.get("name"):
            event["name"] = stock_event.get("name")


def _nearest_stock_event(event: Dict[str, Any], candidates: List[Tuple[str, Dict[str, Any]]]) -> Optional[Tuple[str, Dict[str, Any]]]:
    trade_date = str(event.get("tradeDate") or "")
    ranked = []
    for analysis_date, stock_event in candidates:
        gap = _date_gap_days(trade_date, analysis_date)
        if gap is None or abs(gap) > ALIGNMENT_WINDOW_DAYS:
            continue
        # Prefer same-day, then earlier analysis to avoid hindsight, then closest future context.
        ranked.append((abs(gap), 0 if gap <= 0 else 1, -_date_ordinal(analysis_date), analysis_date, stock_event))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][3], ranked[0][4]


def _date_gap_days(left: Any, right: Any) -> Optional[int]:
    try:
        left_date = datetime.fromisoformat(str(left or "")[:10]).date()
        right_date = datetime.fromisoformat(str(right or "")[:10]).date()
    except ValueError:
        return None
    return (right_date - left_date).days


def _date_ordinal(value: Any) -> int:
    try:
        return datetime.fromisoformat(str(value or "")[:10]).date().toordinal()
    except ValueError:
        return 0


def _event_key(trade_date: Any, code: Any) -> str:
    date_text = str(trade_date or "")[:10]
    code_text = str(code or "").strip().zfill(6)
    return f"{date_text}:{code_text}" if date_text and code_text else ""


def _fill_market_state(event: Dict[str, Any], value: Any) -> None:
    current = str(event.get("marketState") or "unknown")
    text = str(value or "").strip()
    if current in {"", "unknown"} and text:
        event["marketState"] = text


def _merge_ai_signal(current: Optional[Dict[str, Any]], row: Dict[str, Any]) -> Dict[str, Any]:
    signal = current if isinstance(current, dict) and current.get("present") else {
        "present": True,
        "runId": row.get("runId"),
        "selected": bool(row.get("selected")),
        "candidateType": row.get("candidateType") or "",
        "score": row.get("score"),
        "confidence": row.get("confidence"),
        "aiRank": row.get("aiRank"),
        "reason": row.get("itemReason") or row.get("reason") or "",
        "risk": row.get("itemRisk") or "",
        "focus": row.get("itemFocus") or "",
        "entryCondition": _pick_text(row.get("pick"), "entryCondition", "buyTrigger"),
        "invalidCondition": _pick_text(row.get("pick"), "invalidCondition", "stopLoss"),
        "outcomes": {},
    }
    if bool(row.get("selected")):
        signal["selected"] = True
    horizon = str(row.get("horizon") or "")
    if horizon:
        signal.setdefault("outcomes", {})[horizon] = _common_outcome(row)
    return signal


def _merge_t1_signal(current: Optional[Dict[str, Any]], row: Dict[str, Any]) -> Dict[str, Any]:
    selected = bool(row.get("selected"))
    signal = current if isinstance(current, dict) and current.get("present") else {
        "present": True,
        "runId": row.get("runId"),
        "selected": selected,
        "candidateType": row.get("candidateType") or "",
        "score": row.get("score"),
        "confidence": row.get("confidence"),
        "reason": row.get("itemReason") or "",
        "buyPlan": row.get("buyPlan") or "",
        "sellPlan": row.get("sellPlan") or "",
        "risk": row.get("itemRisk") or "",
        "invalidCondition": row.get("invalidCondition") or "",
        "dataSource": row.get("dataSource") or "",
        "outcome": _t1_outcome(row),
    }
    if selected:
        signal["selected"] = True
    return signal


def _merge_stock_signal(current: Optional[Dict[str, Any]], row: Dict[str, Any]) -> Dict[str, Any]:
    if not (isinstance(current, dict) and current.get("present")) or int(row.get("snapshotId") or 0) > int(current.get("snapshotId") or 0):
        decision_json = row.get("decisionJson") if isinstance(row.get("decisionJson"), dict) else {}
        battle = decision_json.get("battle") if isinstance(decision_json.get("battle"), dict) else {}
        signal = {
            "present": True,
            "snapshotId": row.get("snapshotId"),
            "decision": row.get("decision") or "watch",
            "primaryHorizon": row.get("primaryHorizon") or "none",
            "confidence": row.get("confidence"),
            "tradeabilityScore": row.get("confidence"),
            "riskScore": battle.get("bearScore"),
            "aiMode": row.get("aiMode") or "",
            "ruleFallbackUsed": bool(row.get("ruleFallbackUsed")),
            "reason": _decision_reason(decision_json),
            "outcomes": {},
            "tradeOutcomes": [],
        }
    else:
        signal = current
    horizon = str(row.get("horizon") or "")
    if horizon:
        signal.setdefault("outcomes", {})[horizon] = _common_outcome(row)
    return signal


def _classify_event(event: Dict[str, Any]) -> Dict[str, Any]:
    ai = event.get("aiFullMarket") or {}
    t1 = event.get("t1") or {}
    stock = event.get("stockAnalysis") or {}
    stock_alignment = stock.get("alignment") if isinstance(stock.get("alignment"), dict) else {}
    stock_alignment_mode = str(stock_alignment.get("mode") or "")
    strict_stock_signal = bool(stock.get("present") and stock_alignment_mode != "nearby_stock_analysis")
    nearby_stock_context = bool(stock.get("present") and stock_alignment_mode == "nearby_stock_analysis")
    ai_pos = bool(ai.get("present") and ai.get("selected"))
    t1_pos = bool(t1.get("present") and t1.get("selected"))
    stock_decision = str(stock.get("decision") or "")
    stock_present = bool(stock.get("present"))
    stock_supportive = strict_stock_signal and _stock_supportive(stock)
    stock_low = strict_stock_signal and _stock_low(stock)
    stock_buy = strict_stock_signal and stock_decision == "buy"
    pattern = "single_module_or_observation"
    group = "observation"
    if nearby_stock_context:
        pattern, group = "nearby_stock_context", "observation"
    elif _all_disagree(ai_pos, t1_pos, stock_supportive, stock_low, ai, t1, stock):
        pattern, group = "all_disagree", "conflict"
    elif ai_pos and stock_low:
        pattern, group = "ai_selected_stock_low", "conflict"
    elif t1_pos and stock_low:
        pattern, group = "t1_selected_stock_low", "conflict"
    elif stock_buy and not ai_pos and not t1_pos:
        pattern, group = "stock_buy_not_selected", "conflict"
    elif ai.get("present") and t1.get("present") and ai_pos != t1_pos:
        pattern, group = "ai_t1_disagree", "conflict"
    elif ai_pos and t1_pos and stock_supportive:
        pattern, group = "three_module_positive", "confluence"
    elif ai_pos and stock_supportive:
        pattern, group = "ai_and_stock_positive", "confluence"
    elif t1_pos and stock_supportive:
        pattern, group = "t1_and_stock_positive", "confluence"
    outcome = _primary_outcome(event)
    diagnosis = _diagnosis(event, pattern, group, outcome, strict_stock_signal=strict_stock_signal, nearby_stock_context=nearby_stock_context)
    result = dict(event)
    result["pattern"] = pattern
    result["patternGroup"] = group
    result["patternLabel"] = _pattern_label(pattern)
    result["primaryOutcome"] = outcome
    result["diagnosis"] = diagnosis
    result["quality"] = diagnosis.get("comparability") or {}
    result["modulePresence"] = {
        "aiFullMarket": bool(ai.get("present")),
        "t1": bool(t1.get("present")),
        "stockAnalysis": stock_present,
    }
    result["alignment"] = {
        "stockAnalysis": stock.get("alignment") if isinstance(stock.get("alignment"), dict) else {},
    }
    return result


def _all_disagree(ai_pos: bool, t1_pos: bool, stock_supportive: bool, stock_low: bool, ai: Dict[str, Any], t1: Dict[str, Any], stock: Dict[str, Any]) -> bool:
    if not (ai.get("present") and t1.get("present") and stock.get("present")):
        return False
    if not (stock_supportive or stock_low):
        return False
    votes = [ai_pos, t1_pos, stock_supportive]
    return len({bool(item) for item in votes}) > 1 and ai_pos != t1_pos


def _diagnosis(
    event: Dict[str, Any],
    pattern: str,
    group: str,
    outcome: Dict[str, Any],
    *,
    strict_stock_signal: bool,
    nearby_stock_context: bool,
) -> Dict[str, Any]:
    label = _unified_label(event, pattern, group, outcome, strict_stock_signal=strict_stock_signal, nearby_stock_context=nearby_stock_context)
    comparability = _comparability(event, outcome, strict_stock_signal=strict_stock_signal, nearby_stock_context=nearby_stock_context)
    return {
        "pattern": pattern,
        "label": label,
        "labelText": _unified_label_text(label),
        "summary": _diagnosis_summary(pattern, label, comparability),
        "whyClassified": _why_classified(event, pattern),
        "comparability": comparability,
    }


def _comparability(event: Dict[str, Any], outcome: Dict[str, Any], *, strict_stock_signal: bool, nearby_stock_context: bool) -> Dict[str, Any]:
    alignment = (event.get("stockAnalysis") or {}).get("alignment")
    alignment = alignment if isinstance(alignment, dict) else {}
    has_done_outcome = bool(outcome and outcome.get("status") == "done")
    has_outcome = bool(outcome)
    uses_mock = _uses_mock_data(event, outcome)
    can_use_strict = has_done_outcome and not nearby_stock_context and not uses_mock
    can_optimize = can_use_strict and strict_stock_signal
    reason = "同日同股且后验已完成，可进入严格统计。" if can_optimize else "只能观察，不能直接用于优化。"
    if nearby_stock_context:
        reason = "个股分析为 7 日内近邻上下文，不是当日信号。"
    elif uses_mock:
        reason = "样本包含 mock/模拟数据。"
    elif not has_outcome:
        reason = "缺少可用后验结果。"
    elif not has_done_outcome:
        reason = "后验样本未到期或状态未完成。"
    elif not strict_stock_signal and (event.get("aiFullMarket", {}).get("present") or event.get("t1", {}).get("present")):
        reason = "缺少同日个股分析，只能做模块自身观察。"
    return {
        "alignmentMode": alignment.get("mode") or ("exact_same_day" if strict_stock_signal else "none"),
        "stockAnalysisDate": alignment.get("analysisDate") or event.get("tradeDate"),
        "dateGapDays": alignment.get("dateGapDays"),
        "hasDoneOutcome": has_done_outcome,
        "outcomeSource": outcome.get("source") or "",
        "outcomeHorizon": outcome.get("horizon") or "",
        "outcomeBasis": outcome.get("basis") or _outcome_basis(outcome.get("source"), outcome.get("horizon")),
        "canUseForStrictStats": can_use_strict,
        "canUseForOptimization": can_optimize,
        "reason": reason,
    }


def _uses_mock_data(event: Dict[str, Any], outcome: Dict[str, Any]) -> bool:
    t1 = event.get("t1") if isinstance(event.get("t1"), dict) else {}
    texts = [
        t1.get("dataSource"),
        outcome.get("dataSource") if isinstance(outcome, dict) else "",
        outcome.get("reason") if isinstance(outcome, dict) else "",
    ]
    return any("mock" in str(item or "").lower() or "模拟" in str(item or "") for item in texts)


def _unified_label(
    event: Dict[str, Any],
    pattern: str,
    group: str,
    outcome: Dict[str, Any],
    *,
    strict_stock_signal: bool,
    nearby_stock_context: bool,
) -> str:
    if nearby_stock_context or not outcome or outcome.get("status") != "done":
        return "not_comparable"
    if _uses_mock_data(event, outcome):
        return "not_comparable"
    return_pct = _safe_float(outcome.get("returnPct"))
    drawdown_pct = _safe_float(outcome.get("maxDrawdownPct"))
    if return_pct is None:
        return "not_comparable"
    if group == "conflict":
        if return_pct > 1.0:
            return "missed_opportunity" if pattern in {"stock_buy_not_selected", "ai_t1_disagree", "all_disagree"} else "timing_wrong"
        if return_pct > 0:
            return "weak_positive"
        return "false_positive" if pattern != "stock_buy_not_selected" else "correct_reject"
    if group == "confluence":
        if return_pct > 2.0 and (drawdown_pct is None or drawdown_pct >= -6):
            return "true_positive"
        if return_pct > 0 and drawdown_pct is not None and drawdown_pct < -8:
            return "lucky_positive"
        if return_pct > 0:
            return "weak_positive"
        return "false_positive"
    if strict_stock_signal and return_pct <= 0 and not (event.get("aiFullMarket", {}).get("selected") or event.get("t1", {}).get("selected")):
        return "correct_reject"
    if return_pct > 2.0:
        return "missed_opportunity"
    return "not_comparable"


def _unified_label_text(value: str) -> str:
    return {
        "true_positive": "有效正例",
        "weak_positive": "弱正例",
        "timing_wrong": "方向可能对但买点/周期错",
        "lucky_positive": "风险偏高的正收益",
        "false_positive": "误选/失败",
        "correct_reject": "正确回避",
        "missed_opportunity": "漏选机会",
        "not_comparable": "不可直接比较",
    }.get(value, value or "--")


def _diagnosis_summary(pattern: str, label: str, comparability: Dict[str, Any]) -> str:
    if label == "not_comparable":
        return comparability.get("reason") or "样本口径不足，不能下结论。"
    return {
        "ai_selected_stock_low": "AI 全市场给出机会，但同日个股分析未给买入，需要比较后验是否支持机会发现还是风控审查。",
        "t1_selected_stock_low": "T+1 隔夜信号与个股分析日线/买点审查冲突，需要分开看次日交易收益和日线风险。",
        "stock_buy_not_selected": "个股分析给出买入但候选模块未选，需要检查是否是独立机会或忽略市场/板块弱势。",
        "all_disagree": "三模块方向不一致，优先检查数据源、周期和目标函数是否混用。",
        "ai_t1_disagree": "AI 全市场与 T+1 对同日同股判断不同，应分别看 1-5 日机会和次日交易口径。",
        "three_module_positive": "三模块同时正向，但仍需用胜率、收益和回撤验证，不等于自动买入。",
        "ai_and_stock_positive": "AI 全市场机会与个股分析买点/风控形成共振，重点看 1-5 日是否更可靠。",
        "t1_and_stock_positive": "T+1 隔夜机会与个股分析风险可控形成共振，重点看次日可交易收益。",
    }.get(pattern, "单模块或观察样本，只适合补充理解。")


def _why_classified(event: Dict[str, Any], pattern: str) -> str:
    ai = event.get("aiFullMarket") or {}
    t1 = event.get("t1") or {}
    stock = event.get("stockAnalysis") or {}
    parts = []
    if ai.get("present"):
        parts.append(f"AI全市场{'选中' if ai.get('selected') else '未选'}")
    if t1.get("present"):
        parts.append(f"T+1{'选中' if t1.get('selected') else '未选'}")
    if stock.get("present"):
        align = stock.get("alignment") if isinstance(stock.get("alignment"), dict) else {}
        align_text = "同日" if align.get("mode") == "exact_same_day" else "近邻"
        parts.append(f"个股分析{align_text}{stock.get('decision') or 'watch'}")
    return "；".join(parts) or _pattern_label(pattern)


def _stock_supportive(stock: Dict[str, Any]) -> bool:
    if not stock.get("present"):
        return False
    decision = str(stock.get("decision") or "")
    if decision == "buy":
        return True
    if decision != "watch":
        return False
    confidence = _safe_float(stock.get("confidence"))
    risk_score = _safe_float(stock.get("riskScore"))
    return (confidence is not None and confidence >= 65) and (risk_score is None or risk_score <= 60)


def _stock_low(stock: Dict[str, Any]) -> bool:
    if not stock.get("present"):
        return False
    decision = str(stock.get("decision") or "")
    if decision == "avoid":
        return True
    if decision == "watch":
        return not _stock_supportive(stock)
    return False


def _primary_outcome(event: Dict[str, Any]) -> Dict[str, Any]:
    ai = event.get("aiFullMarket") or {}
    stock = event.get("stockAnalysis") or {}
    t1 = event.get("t1") or {}
    ai_outcomes = ai.get("outcomes") if isinstance(ai.get("outcomes"), dict) else {}
    stock_outcomes = stock.get("outcomes") if isinstance(stock.get("outcomes"), dict) else {}
    for bucket in (ai_outcomes, stock_outcomes):
        for horizon in ("5", "3", "1", "10"):
            if isinstance(bucket.get(horizon), dict) and bucket[horizon].get("status") == "done":
                source = "aiFullMarket" if bucket is ai_outcomes else "stockAnalysis"
                return {"source": source, "horizon": horizon, "basis": _outcome_basis(source, horizon), **bucket[horizon]}
    if isinstance(t1.get("outcome"), dict):
        return {"source": "t1", "horizon": "next_day", "basis": _outcome_basis("t1", "next_day"), **t1["outcome"]}
    return {}


def _summary(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    done_count = len([item for item in cases if item.get("primaryOutcome", {}).get("status") == "done"])
    no_outcome_count = len([item for item in cases if not item.get("primaryOutcome")])
    data_insufficient_count = len([item for item in cases if not item.get("primaryOutcome") or item.get("diagnosis", {}).get("label") == "not_comparable"])
    not_comparable_count = len([item for item in cases if item.get("diagnosis", {}).get("label") == "not_comparable"])
    pending_count = len([item for item in cases if item.get("primaryOutcome") and item.get("primaryOutcome", {}).get("status") not in {"", "done"}])
    nearby_count = len([item for item in cases if item.get("alignment", {}).get("stockAnalysis", {}).get("mode") == "nearby_stock_analysis"])
    strict_same_day_count = len([item for item in cases if item.get("alignment", {}).get("stockAnalysis", {}).get("mode") == "exact_same_day"])
    return {
        "eventCount": len(cases),
        "effectiveTradeDayCount": len({item.get("tradeDate") for item in cases if item.get("tradeDate")}),
        "conflictCount": len([item for item in cases if item.get("patternGroup") == "conflict"]),
        "confluenceCount": len([item for item in cases if item.get("patternGroup") == "confluence"]),
        "aiSignalCount": len([item for item in cases if item.get("aiFullMarket", {}).get("present")]),
        "t1SignalCount": len([item for item in cases if item.get("t1", {}).get("present")]),
        "stockSignalCount": len([item for item in cases if item.get("stockAnalysis", {}).get("present")]),
        "strictSameDayStockCount": strict_same_day_count,
        "nearbyStockContextCount": nearby_count,
        "doneOutcomeCount": done_count,
        "pendingOutcomeCount": pending_count,
        "noOutcomeCount": no_outcome_count,
        "notComparableCount": not_comparable_count,
        "dataInsufficientCount": data_insufficient_count,
        "optimizationReadyCount": len([item for item in cases if item.get("quality", {}).get("canUseForOptimization")]),
        "doneRatePct": _rate(done_count, len(cases)),
        "avgReturnPct": _avg([item.get("primaryOutcome", {}).get("returnPct") for item in cases]),
        "avgDrawdownPct": _avg([item.get("primaryOutcome", {}).get("maxDrawdownPct") for item in cases]),
    }


def _pattern_stats(cases: List[Dict[str, Any]], *, group: str) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in cases:
        if item.get("patternGroup") == group:
            grouped.setdefault(str(item.get("pattern") or ""), []).append(item)
    return [_stats_row(key, values) for key, values in sorted(grouped.items())]


def _module_matrix(cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    specs = [
        ("ai_vs_stock", lambda item: item.get("aiFullMarket", {}).get("present") and item.get("stockAnalysis", {}).get("present")),
        ("t1_vs_stock", lambda item: item.get("t1", {}).get("present") and item.get("stockAnalysis", {}).get("present")),
        ("ai_vs_t1", lambda item: item.get("aiFullMarket", {}).get("present") and item.get("t1", {}).get("present")),
        ("three_modules", lambda item: all((item.get("modulePresence") or {}).values())),
    ]
    return [_stats_row(key, [item for item in cases if predicate(item)]) for key, predicate in specs]


def _market_state_stats(cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in cases:
        grouped.setdefault(str(item.get("marketState") or "unknown"), []).append(item)
    return [_stats_row(key, values) for key, values in sorted(grouped.items())]


def _stats_row(key: str, values: List[Dict[str, Any]]) -> Dict[str, Any]:
    done = [item for item in values if item.get("primaryOutcome", {}).get("status") == "done"]
    positives = [item for item in done if _safe_float(item.get("primaryOutcome", {}).get("returnPct")) is not None and float(item["primaryOutcome"]["returnPct"]) > 0]
    comparable = [item for item in values if item.get("quality", {}).get("canUseForStrictStats")]
    nearby = [item for item in values if item.get("alignment", {}).get("stockAnalysis", {}).get("mode") == "nearby_stock_analysis"]
    return {
        "key": key,
        "label": _pattern_label(key),
        "sampleCount": len(values),
        "doneCount": len(done),
        "doneRatePct": _rate(len(done), len(values)),
        "comparableCount": len(comparable),
        "nearbyContextCount": len(nearby),
        "notComparableCount": len([item for item in values if item.get("diagnosis", {}).get("label") == "not_comparable"]),
        "qualityNote": _quality_note(values),
        "metricNote": _metric_note(done),
        "positiveCount": len(positives),
        "positiveRatePct": _rate(len(positives), len(done)),
        "avgReturnPct": _avg([item.get("primaryOutcome", {}).get("returnPct") for item in done]),
        "avgExcessReturnPct": _avg([item.get("primaryOutcome", {}).get("excessReturnPct") for item in done]),
        "avgDrawdownPct": _avg([item.get("primaryOutcome", {}).get("maxDrawdownPct") for item in done]),
    }


def _outcome_basis_stats(cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in cases:
        outcome = item.get("primaryOutcome") or {}
        key = str(outcome.get("basis") or _outcome_basis(outcome.get("source"), outcome.get("horizon")) or "no_done_outcome")
        grouped.setdefault(key, []).append(item)
    return [_stats_row(key, values) for key, values in sorted(grouped.items())]


def _quality_note(values: List[Dict[str, Any]]) -> str:
    if not values:
        return "暂无样本。"
    comparable_count = len([item for item in values if item.get("quality", {}).get("canUseForStrictStats")])
    nearby_count = len([item for item in values if item.get("alignment", {}).get("stockAnalysis", {}).get("mode") == "nearby_stock_analysis"])
    if nearby_count:
        return f"严格统计 {comparable_count} 条；近邻上下文 {nearby_count} 条不用于调参。"
    return f"严格统计 {comparable_count} 条。"


def _metric_note(done: List[Dict[str, Any]]) -> str:
    bases = sorted({str(item.get("primaryOutcome", {}).get("basis") or "") for item in done if item.get("primaryOutcome", {}).get("basis")})
    if not bases:
        return "暂无完成后验。"
    return "；".join(bases[:3])


def _audit_pack(data: Dict[str, Any]) -> Dict[str, Any]:
    cases = data.get("cases") or []
    conflicts = [item for item in cases if item.get("patternGroup") == "conflict"][:REVIEW_CASE_LIMIT // 2]
    confluences = [item for item in cases if item.get("patternGroup") == "confluence"][:REVIEW_CASE_LIMIT // 2]
    return {
        "scope": {**(data.get("scope") or {}), "dataQualityWarnings": data.get("warnings") or []},
        "summary": data.get("summary") or {},
        "metrics": {
            "conflictStats": data.get("conflictStats") or [],
            "confluenceStats": data.get("confluenceStats") or [],
            "marketStateStats": data.get("marketStateStats") or [],
            "moduleMatrix": data.get("moduleMatrix") or [],
            "outcomeBasisStats": data.get("outcomeBasisStats") or [],
        },
        "moduleRuns": _module_runs(cases),
        "ruleSuggestions": data.get("ruleSuggestions") or [],
        "representativeCases": [_case_for_prompt(item) for item in (conflicts + confluences)],
        "constraints": {
            "cannotChangeLabels": True,
            "cannotWriteConfig": True,
            "cannotOverrideModuleResults": True,
            "mustAuditOriginalModuleOutputs": True,
        },
    }


def _case_for_prompt(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tradeDate": item.get("tradeDate"),
        "code": item.get("code"),
        "name": item.get("name"),
        "marketState": item.get("marketState"),
        "pattern": item.get("pattern"),
        "patternLabel": item.get("patternLabel"),
        "aiFullMarket": _compact_signal(item.get("aiFullMarket")),
        "t1": _compact_signal(item.get("t1")),
        "stockAnalysis": _compact_signal(item.get("stockAnalysis")),
        "alignment": item.get("alignment") or {},
        "primaryOutcome": item.get("primaryOutcome") or {},
        "diagnosis": item.get("diagnosis") or {},
    }


def _compact_signal(signal: Any) -> Dict[str, Any]:
    if not isinstance(signal, dict) or not signal.get("present"):
        return {"present": False}
    keep = [
        "present",
        "runId",
        "snapshotId",
        "selected",
        "decision",
        "candidateType",
        "score",
        "confidence",
        "tradeabilityScore",
        "riskScore",
        "reason",
        "risk",
        "entryCondition",
        "invalidCondition",
        "buyPlan",
        "sellPlan",
        "alignment",
    ]
    return {key: signal.get(key) for key in keep if key in signal}


def _module_runs(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    ai_run_ids = sorted({item.get("aiFullMarket", {}).get("runId") for item in cases if item.get("aiFullMarket", {}).get("runId")})
    t1_run_ids = sorted({item.get("t1", {}).get("runId") for item in cases if item.get("t1", {}).get("runId")})
    stock_snapshot_ids = sorted({item.get("stockAnalysis", {}).get("snapshotId") for item in cases if item.get("stockAnalysis", {}).get("snapshotId")})
    return {
        "aiFullMarket": {"runIds": ai_run_ids[-20:], "selectedCount": len([item for item in cases if item.get("aiFullMarket", {}).get("selected")])},
        "t1": {"runIds": t1_run_ids[-20:], "selectedCount": len([item for item in cases if item.get("t1", {}).get("selected")])},
        "stockAnalysis": {
            "snapshotIds": stock_snapshot_ids[-30:],
            "decisionCounts": _count_by(cases, lambda item: item.get("stockAnalysis", {}).get("decision") if item.get("stockAnalysis", {}).get("present") else ""),
        },
    }


def _count_by(cases: List[Dict[str, Any]], key_fn: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in cases:
        key = str(key_fn(item) or "")
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def _warnings(cases: List[Dict[str, Any]]) -> List[str]:
    warnings = []
    if not cases:
        warnings.append("暂无可对齐的跨模块复盘样本。")
    if len(cases) < 20:
        warnings.append("样本数偏少，DeepSeek 审查只能作为解释，不能作为调参依据。")
    if any(item.get("marketState") in {"", "unknown"} for item in cases):
        warnings.append("部分样本缺少统一市场状态，市场分层统计需谨慎参考。")
    not_comparable_count = len([item for item in cases if item.get("diagnosis", {}).get("label") == "not_comparable"])
    if not_comparable_count:
        warnings.append(f"有 {not_comparable_count} 条样本未到期、缺后验或口径不可比，已单独标记为不可直接比较。")
    nearby_count = len([item for item in cases if item.get("alignment", {}).get("stockAnalysis", {}).get("mode") == "nearby_stock_analysis"])
    if nearby_count:
        warnings.append(f"有 {nearby_count} 条样本使用近邻个股分析对齐，只适合做复盘参考，不能视为严格同日信号。")
    return warnings


def _rule_suggestions(cases: List[Dict[str, Any]], conflict_stats: List[Dict[str, Any]], confluence_stats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    suggestions: List[Dict[str, Any]] = []
    exact_cross_count = len([item for item in cases if item.get("alignment", {}).get("stockAnalysis", {}).get("mode") == "exact_same_day"])
    nearby_count = len([item for item in cases if item.get("alignment", {}).get("stockAnalysis", {}).get("mode") == "nearby_stock_analysis"])
    if nearby_count and exact_cross_count < 20:
        suggestions.append(
            {
                "source": "rule_stats",
                "targetModule": "stock_analysis",
                "actionType": "data_collection",
                "evidence": f"严格同日跨模块样本 {exact_cross_count} 条，近邻对齐样本 {nearby_count} 条。",
                "suggestion": "优先在 AI 全市场和 T+1 候选点击个股分析，积累同日同股样本后再判断谁更准。",
            }
        )
    for stat in conflict_stats:
        key = stat.get("key")
        done = int(stat.get("doneCount") or 0)
        avg_return = _safe_float(stat.get("avgReturnPct"))
        if key == "ai_selected_stock_low" and done >= 5 and avg_return is not None:
            target = "stock_analysis" if avg_return > 0 else "ai_full_market"
            suggestion = "检查个股分析是否把 watch 过度解释为看空。" if avg_return > 0 else "检查 AI 全市场是否追高或忽略回撤风险。"
            suggestions.append(_rule_suggestion(target, "threshold_review", stat, suggestion))
        if key == "t1_selected_stock_low" and done >= 5 and avg_return is not None:
            target = "stock_analysis" if avg_return > 0 else "t1"
            suggestion = "隔夜策略可能有独立有效性，应把日线低分和次日交易机会分开展示。" if avg_return > 0 else "加强 T+1 对日线风险、低开和冲高回落的惩罚。"
            suggestions.append(_rule_suggestion(target, "strategy_boundary_review", stat, suggestion))
    for stat in confluence_stats:
        done = int(stat.get("doneCount") or 0)
        avg_return = _safe_float(stat.get("avgReturnPct"))
        if done >= 5 and avg_return is not None and avg_return < 0:
            suggestions.append(_rule_suggestion("cross_module", "risk_review", stat, "共振样本后验为负时，不应把多模块一致直接展示成高置信买入。"))
    if not suggestions:
        suggestions.append(
            {
                "source": "rule_stats",
                "targetModule": "cross_module",
                "actionType": "sample_building",
                "evidence": "当前冲突/共振完成样本不足。",
                "suggestion": "先积累同日个股分析和后验结果，暂不根据跨模块统计调整任何原模块。"
            }
        )
    return suggestions[:8]


def _rule_suggestion(target: str, action_type: str, stat: Dict[str, Any], suggestion: str) -> Dict[str, Any]:
    return {
        "source": "rule_stats",
        "targetModule": target,
        "actionType": action_type,
        "evidence": f"{stat.get('label')}: 样本 {stat.get('sampleCount')}，完成 {stat.get('doneCount')}，正收益率 {_format_pct_for_text(stat.get('positiveRatePct'))}，平均收益 {_format_pct_for_text(stat.get('avgReturnPct'))}。",
        "suggestion": suggestion,
    }


def _date_range(cases: List[Dict[str, Any]]) -> Tuple[str, str]:
    dates = sorted([str(item.get("tradeDate") or "") for item in cases if item.get("tradeDate")])
    return (dates[0], dates[-1]) if dates else ("", "")


def _format_pct_for_text(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "--"
    return f"{number:+.2f}%"


def _fallback_review(audit_pack: Dict[str, Any], *, ai_available: bool) -> Dict[str, Any]:
    summary = audit_pack.get("summary") or {}
    conflict_count = int(summary.get("conflictCount") or 0)
    confluence_count = int(summary.get("confluenceCount") or 0)
    return {
        "aiMode": "ai_unavailable" if not ai_available else "rules_fallback",
        "reviewConclusion": f"已完成跨模块结果对齐：冲突样本 {conflict_count}，共振样本 {confluence_count}。DeepSeek 未完成时仅展示客观统计，不生成强结论。",
        "confidence": "low",
        "keyFindings": [],
        "optimizationDrafts": [],
        "missingEvidence": ["DeepSeek 审查未完成或未配置。"],
        "riskWarnings": ["当前结果不能自动改配置，也不能替代三个模块的原始复盘。"],
    }


def _ai_row(row: Any) -> Dict[str, Any]:
    outcome = _json(row["outcome_json"], {})
    pick = _json(row["pick_json"], {})
    item = _json(row["item_json"], {})
    return {
        "runId": int(row["run_id"] or 0),
        "code": row["code"],
        "name": row["name"],
        "recommendationDate": row["recommendation_date"],
        "profile": row["profile"],
        "selected": bool(row["selected"]),
        "candidateType": row["candidate_type"],
        "score": item.get("score"),
        "confidence": item.get("confidence") or pick.get("confidence"),
        "ruleRank": row["rule_rank"],
        "aiRank": row["ai_rank"],
        "horizon": row["horizon"],
        "returnPct": row["return_pct"],
        "excessReturnPct": row["excess_return_pct"],
        "maxDrawdownPct": row["max_drawdown_pct"],
        "maxRunupPct": row["max_runup_pct"],
        "status": row["status"],
        "label": row["label"],
        "reason": row["reason"],
        "marketState": row["market_state"],
        "itemReason": row["item_reason"],
        "itemRisk": row["item_risk"],
        "itemFocus": row["item_focus"],
        "pick": pick,
        "outcome": outcome,
        "runMarket": _json(row["market_json"], {}),
    }


def _t1_row(row: Any) -> Dict[str, Any]:
    return {
        "runId": int(row["run_id"] or 0),
        "code": row["code"],
        "name": row["name"],
        "recommendationDate": row["recommendation_date"],
        "nextTradeDate": row["next_trade_date"],
        "selected": bool(row["selected"]),
        "candidateType": row["candidate_type"],
        "score": row["score"],
        "confidence": row["confidence"],
        "openReturnPct": row["open_return_pct"],
        "highReturnPct": row["high_return_pct"],
        "closeReturnPct": row["close_return_pct"],
        "maxDrawdownPct": row["max_drawdown_pct"],
        "excessReturnPct": row["excess_return_pct"],
        "label": row["outcome_label"],
        "outcomeScore": row["outcome_score"],
        "hitSellPlan": bool(row["hit_sell_plan"]),
        "hitInvalidCondition": bool(row["hit_invalid_condition"]),
        "failureReason": row["failure_reason"],
        "dataSource": row["data_source"],
        "outcome": _json(row["outcome_json"], {}),
        "itemReason": row["item_reason"],
        "buyPlan": row["buy_plan"],
        "sellPlan": row["sell_plan"],
        "itemRisk": row["item_risk"],
        "invalidCondition": row["invalid_condition"],
        "pick": _json(row["pick_json"], {}),
        "runMarket": _json(row["market_json"], {}),
    }


def _stock_row(row: Any) -> Dict[str, Any]:
    return {
        "snapshotId": int(row["snapshot_id"] or 0),
        "code": row["code"],
        "name": row["name"],
        "analysisDate": row["analysis_date"],
        "riskMode": row["risk_mode"],
        "aiMode": row["ai_mode"],
        "decision": row["decision"],
        "primaryHorizon": row["primary_horizon"],
        "confidence": row["confidence"],
        "ruleFallbackUsed": bool(row["rule_fallback_used"]),
        "evidencePack": _json(row["evidence_pack_json"], {}),
        "decisionJson": _json(row["decision_json"], {}),
        "horizon": row["horizon"],
        "returnPct": row["return_pct"],
        "maxDrawdownPct": row["max_drawdown_pct"],
        "maxRunupPct": row["max_runup_pct"],
        "status": row["status"],
        "label": row["label"],
        "reason": row["reason"],
    }


def _stock_trade_row(row: Any) -> Dict[str, Any]:
    return {
        "snapshotId": int(row["snapshot_id"] or 0),
        "code": row["code"],
        "name": row["name"],
        "recommendationDate": row["recommendation_date"],
        "planVersion": row["plan_version"],
        "horizonType": row["horizon_type"],
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
    }


def _common_outcome(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": row.get("status") or "",
        "label": row.get("label") or "",
        "returnPct": row.get("returnPct"),
        "excessReturnPct": row.get("excessReturnPct"),
        "maxDrawdownPct": row.get("maxDrawdownPct"),
        "maxRunupPct": row.get("maxRunupPct"),
        "reason": row.get("reason") or "",
    }


def _t1_outcome(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "done" if row.get("nextTradeDate") else "pending",
        "label": row.get("label") or "",
        "returnPct": row.get("closeReturnPct"),
        "openReturnPct": row.get("openReturnPct"),
        "highReturnPct": row.get("highReturnPct"),
        "closeReturnPct": row.get("closeReturnPct"),
        "excessReturnPct": row.get("excessReturnPct"),
        "maxDrawdownPct": row.get("maxDrawdownPct"),
        "hitSellPlan": row.get("hitSellPlan"),
        "hitInvalidCondition": row.get("hitInvalidCondition"),
        "dataSource": row.get("dataSource") or "",
        "reason": row.get("failureReason") or "",
    }


def _stock_trade_outcome(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "planVersion": row.get("planVersion"),
        "horizonType": row.get("horizonType"),
        "entryTriggered": row.get("entryTriggered"),
        "status": row.get("status"),
        "label": row.get("label"),
        "returnPct": row.get("returnPct"),
        "maxDrawdownPct": row.get("maxDrawdownPct"),
        "maxRunupPct": row.get("maxRunupPct"),
        "reason": row.get("reason"),
    }


def _market_state_from_json(value: Any) -> str:
    data = value if isinstance(value, dict) else {}
    market = data.get("market") if isinstance(data.get("market"), dict) else data
    for key in ("marketState", "market_state", "marketMode", "mode", "state"):
        if market.get(key):
            return str(market.get(key))
    snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
    if snapshot.get("marketMode"):
        return str(snapshot.get("marketMode"))
    return ""


def _pick_text(pick: Any, *keys: str) -> str:
    if not isinstance(pick, dict):
        return ""
    for key in keys:
        if pick.get(key):
            return str(pick.get(key))
    return ""


def _decision_reason(decision: Dict[str, Any]) -> str:
    if decision.get("decisionText"):
        return str(decision.get("decisionText"))
    summary = decision.get("summary")
    if isinstance(summary, list) and summary:
        return str(summary[0])
    return ""


def _pattern_label(value: str) -> str:
    return {
        "ai_selected_stock_low": "AI选中但个股分析未买入",
        "t1_selected_stock_low": "T+1选中但个股分析未买入",
        "stock_buy_not_selected": "个股分析买入但候选模块未选",
        "all_disagree": "三模块方向不一致",
        "ai_t1_disagree": "AI全市场与T+1不一致",
        "nearby_stock_context": "近邻个股分析上下文",
        "three_module_positive": "三模块共振",
        "ai_and_stock_positive": "AI全市场与个股分析共振",
        "t1_and_stock_positive": "T+1与个股分析共振",
        "ai_vs_stock": "AI全市场 vs 个股分析",
        "t1_vs_stock": "T+1 vs 个股分析",
        "ai_vs_t1": "AI全市场 vs T+1",
        "three_modules": "三模块同时出现",
        "single_module_or_observation": "单模块/观察样本",
    }.get(value, value or "--")


def _outcome_basis(source: Any, horizon: Any) -> str:
    source_text = str(source or "")
    horizon_text = str(horizon or "")
    if source_text == "t1":
        return "T+1次日收盘收益"
    if source_text == "aiFullMarket":
        return f"AI全市场{horizon_text}日收益"
    if source_text == "stockAnalysis":
        return f"个股分析{horizon_text}日方向收益"
    return ""


def _avg(values: List[Any]) -> Optional[float]:
    nums = [float(value) for value in values if _safe_float(value) is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100, 1)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if parsed is not None else default


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
