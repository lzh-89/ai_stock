from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Callable, Dict, Iterable, List, Optional

from backend.app.database import Database


LOGGER = logging.getLogger("ai_stock.t1_event_risk")
SOURCE = "eastmoney_announcement"
PROVIDER_VERSION = "t1_event_risk_eastmoney_v1"
CLASSIFIER_VERSION = "t1_event_risk_keywords_v1"
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_CHUNK_SIZE = 40
DEFAULT_CACHE_TTL_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 8
MAX_PAGES_PER_CHUNK = 3

_CATEGORY_RULES = (
    ("regulatory", "监管风险", ("立案", "处罚", "监管措施", "监管函", "问询函", "警示函", "纪律处分")),
    ("reduction", "股东减持", ("减持计划", "拟减持", "减持股份", "集中竞价减持", "大宗交易减持")),
    ("unlock", "限售解禁", ("解除限售", "限售股份上市流通", "限售股上市流通", "解禁")),
    ("earnings", "业绩风险", ("业绩预告", "业绩快报", "业绩预告修正", "预亏", "首亏", "续亏", "扭亏", "业绩下修")),
    ("corporate_action", "公司行为", ("停牌", "复牌", "退市风险", "终止上市", "重大资产重组", "控制权变更", "除权除息")),
)

_CRITICAL_KEYWORDS = ("立案", "退市风险", "终止上市", "重大违法", "暂停上市")
_HIGH_KEYWORDS = (
    "处罚",
    "监管措施",
    "警示函",
    "减持计划",
    "拟减持",
    "预亏",
    "首亏",
    "续亏",
    "业绩下修",
    "解除限售",
    "限售股份上市流通",
    "停牌",
    "重大资产重组终止",
)


class T1EventRiskProvider:
    def __init__(
        self,
        database: Database,
        fetch_json: Optional[Callable[[str, int], Dict[str, Any]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ):
        self.database = database
        self.fetch_json = fetch_json or _fetch_json
        self.clock = clock or datetime.now
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))

    def evaluate(
        self,
        candidates: Iterable[Dict[str, Any]],
        *,
        run_id: int,
        recommendation_date: str,
        signal_published_at: str,
        force: bool = False,
    ) -> Dict[str, Any]:
        codes = sorted(
            {
                _normalize_code(candidate.get("code"))
                for candidate in candidates
                if _normalize_code(candidate.get("code"))
            }
        )
        cutoff = _parse_datetime(signal_published_at) or self.clock()
        if cutoff > self.clock():
            cutoff = self.clock()
        cutoff_at = cutoff.isoformat(timespec="seconds")
        not_before = (self.clock() - timedelta(seconds=self.cache_ttl_seconds)).isoformat(timespec="seconds")
        cached = None if force or not codes else self.database.latest_reusable_t1_event_risk_snapshot(
            recommendation_date,
            SOURCE,
            codes,
            not_before,
        )
        if cached:
            events = list(cached.get("events") or [])
            covered_codes = list(cached.get("coveredCodes") or [])
            errors = list(cached.get("errors") or [])
            status = str(cached.get("status") or "unavailable")
            reused_from = int(cached.get("id") or 0)
        else:
            fetched = self._fetch_codes(codes, recommendation_date, cutoff.date().isoformat())
            events = fetched["events"]
            covered_codes = fetched["coveredCodes"]
            errors = fetched["errors"]
            status = "available" if len(covered_codes) == len(codes) else "partial" if covered_codes else "unavailable"
            reused_from = 0

        normalized_events = _normalize_events(events, cutoff)
        exact_count = len([event for event in normalized_events if event.get("timestampStatus") == "exact"])
        timestamp_audit_passed = exact_count == len(normalized_events)
        coverage_pct = round(len(covered_codes) / len(codes) * 100, 2) if codes else 100.0
        payload_hash = hashlib.sha256(
            json.dumps(
                {
                    "codes": codes,
                    "coveredCodes": sorted(covered_codes),
                    "events": normalized_events,
                    "cutoff": cutoff_at,
                    "version": PROVIDER_VERSION,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        snapshot = self.database.append_t1_event_risk_snapshot(
            {
                "runId": run_id,
                "recommendationDate": recommendation_date,
                "signalPublishedAt": signal_published_at,
                "evidenceCutoffAt": cutoff_at,
                "source": SOURCE,
                "status": status,
                "requestedCodes": codes,
                "coveredCodes": covered_codes,
                "coveragePct": coverage_pct,
                "exactTimestampCount": exact_count,
                "timestampAuditPassed": timestamp_audit_passed,
                "events": normalized_events,
                "errors": errors,
                "payloadHash": payload_hash,
                "reusedFromSnapshotId": reused_from,
                "createdAt": self.clock().isoformat(timespec="seconds"),
            }
        )
        assessments = _assess_codes(codes, set(covered_codes), normalized_events, cutoff)
        readiness = self.status()
        return {
            "version": PROVIDER_VERSION,
            "classifierVersion": CLASSIFIER_VERSION,
            "snapshotId": snapshot["id"],
            "source": SOURCE,
            "status": status,
            "recommendationDate": recommendation_date,
            "signalPublishedAt": signal_published_at,
            "evidenceCutoffAt": cutoff_at,
            "requestedCount": len(codes),
            "coveredCount": len(covered_codes),
            "coveragePct": coverage_pct,
            "eventCount": len(normalized_events),
            "exactTimestampCount": exact_count,
            "timestampAuditPassed": timestamp_audit_passed,
            "reusedFromSnapshotId": reused_from,
            "errors": errors,
            "assessments": assessments,
            "eventGateReady": bool(readiness.get("eventGateReady")),
            "readiness": readiness,
        }

    def status(self) -> Dict[str, Any]:
        snapshots = self.database.list_t1_event_risk_snapshots(limit=500)
        snapshot_by_run: Dict[int, Dict[str, Any]] = {}
        for snapshot in snapshots:
            run_id = int(snapshot.get("runId") or 0)
            if run_id > 0 and run_id not in snapshot_by_run:
                snapshot_by_run[run_id] = snapshot

        runs = self.database.list_t1_overnight_selection_runs(limit=5000, status="done")
        by_date: Dict[str, List[Dict[str, Any]]] = {}
        for run in runs:
            request = run.get("request") or {}
            market = run.get("market") or {}
            role = str(market.get("runRole") or request.get("runRole") or "")
            mode = str(run.get("selectionMode") or "")
            if bool(request.get("force")) or role in {"research", "legacy", "invalid"}:
                continue
            if mode in {"rules_fallback", "ai_technical_failure_zero_pick", "ai_unavailable_zero_pick", "ai_failed"}:
                continue
            trade_date = str(run.get("recommendationDate") or "")
            if trade_date:
                by_date.setdefault(trade_date, []).append(run)

        audited = []
        for trade_date in sorted(by_date):
            candidates = sorted(
                by_date[trade_date],
                key=lambda row: (str(row.get("scanTime") or "") != "14:40", -int(row.get("id") or 0)),
            )
            run = candidates[0]
            snapshot = snapshot_by_run.get(int(run.get("id") or 0))
            passed = bool(
                snapshot
                and snapshot.get("status") == "available"
                and float(snapshot.get("coveragePct") or 0) >= 95
                and snapshot.get("timestampAuditPassed") is True
            )
            audited.append(
                {
                    "recommendationDate": trade_date,
                    "runId": int(run.get("id") or 0),
                    "snapshotId": int((snapshot or {}).get("id") or 0),
                    "coveragePct": (snapshot or {}).get("coveragePct"),
                    "timestampAuditPassed": bool((snapshot or {}).get("timestampAuditPassed")),
                    "passed": passed,
                }
            )

        consecutive = 0
        for day in reversed(audited):
            if not day["passed"]:
                break
            consecutive += 1
        latest = snapshots[0] if snapshots else {}
        return {
            "providerVersion": PROVIDER_VERSION,
            "classifierVersion": CLASSIFIER_VERSION,
            "source": SOURCE,
            "sourceLabel": "东方财富公告",
            "eventGateReady": consecutive >= 10,
            "requiredConsecutiveOfficialDays": 10,
            "consecutivePassedOfficialDays": consecutive,
            "latestSnapshot": _snapshot_summary(latest),
            "recentOfficialAudits": audited[-10:],
            "status": "ready" if consecutive >= 10 else "collecting" if snapshots else "not_started",
        }

    def _fetch_codes(self, codes: List[str], begin_date: str, end_date: str) -> Dict[str, Any]:
        chunks = [codes[index:index + DEFAULT_CHUNK_SIZE] for index in range(0, len(codes), DEFAULT_CHUNK_SIZE)]
        events: List[Dict[str, Any]] = []
        covered: set[str] = set()
        errors: List[str] = []
        with ThreadPoolExecutor(max_workers=min(3, max(1, len(chunks)))) as executor:
            futures = {
                executor.submit(self._fetch_chunk_with_retry, chunk, begin_date, end_date): chunk
                for chunk in chunks
            }
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    rows = future.result()
                except Exception as error:  # noqa: BLE001 - partial coverage is a valid degraded state.
                    errors.append(f"{chunk[0]}-{chunk[-1]}: {error}")
                    continue
                covered.update(chunk)
                events.extend(rows)
        return {
            "events": _deduplicate_events(events),
            "coveredCodes": sorted(covered),
            "errors": errors,
        }

    def _fetch_chunk_with_retry(self, codes: List[str], begin_date: str, end_date: str) -> List[Dict[str, Any]]:
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                return self._fetch_chunk(codes, begin_date, end_date)
            except Exception as error:  # noqa: BLE001 - one bounded retry is part of the provider contract.
                last_error = error
                if attempt == 0:
                    time.sleep(0.2)
        raise RuntimeError(str(last_error or "event provider request failed"))

    def _fetch_chunk(self, codes: List[str], begin_date: str, end_date: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for page in range(1, MAX_PAGES_PER_CHUNK + 1):
            params = urllib.parse.urlencode(
                {
                    "sr": "-1",
                    "page_size": "100",
                    "page_index": str(page),
                    "ann_type": "A",
                    "client_source": "web",
                    "stock_list": ",".join(codes),
                    "begin_time": begin_date,
                    "end_time": end_date,
                    "f_node": "0",
                    "s_node": "0",
                }
            )
            payload = self.fetch_json(f"https://np-anotice-stock.eastmoney.com/api/security/ann?{params}", DEFAULT_TIMEOUT_SECONDS)
            page_rows = _extract_records(payload)
            rows.extend(_normalize_source_record(record, codes) for record in page_rows)
            if len(page_rows) < 100:
                break
        return [item for group in rows for item in group]


def classify_event(title: str) -> Dict[str, Any]:
    normalized = re.sub(r"\s+", "", str(title or ""))
    category = "other"
    label = "其他公告"
    matched: List[str] = []
    for rule_category, rule_label, keywords in _CATEGORY_RULES:
        hits = [keyword for keyword in keywords if keyword in normalized]
        if hits:
            category = rule_category
            label = rule_label
            matched = hits
            break
    if any(keyword in normalized for keyword in _CRITICAL_KEYWORDS):
        severity = "critical"
    elif any(keyword in normalized for keyword in _HIGH_KEYWORDS):
        severity = "high"
    elif category != "other":
        severity = "medium"
    else:
        severity = "info"
    return {
        "category": category,
        "categoryLabel": label,
        "severity": severity,
        "matchedKeywords": matched,
        "classifierVersion": CLASSIFIER_VERSION,
    }


def _fetch_json(url: str, timeout: int) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://data.eastmoney.com/",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _extract_records(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else {}
    rows = (data or {}).get("list") if isinstance(data, dict) else []
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise RuntimeError("event provider returned an invalid list")
    return [row for row in rows if isinstance(row, dict)]


def _normalize_source_record(record: Dict[str, Any], requested_codes: List[str]) -> List[Dict[str, Any]]:
    title = str(record.get("title") or record.get("TITLE") or record.get("announcementTitle") or "").strip()
    source_event_id = str(record.get("art_code") or record.get("ART_CODE") or record.get("id") or "").strip()
    raw_timestamp = str(
        record.get("display_time")
        or record.get("DISPLAY_TIME")
        or record.get("publish_time")
        or record.get("PUBLISH_TIME")
        or record.get("notice_date")
        or record.get("NOTICE_DATE")
        or record.get("eutime")
        or record.get("EUTIME")
        or ""
    ).strip()
    record_codes = _record_codes(record)
    wanted = set(requested_codes)
    classification = classify_event(title)
    result = []
    for code in record_codes:
        if code not in wanted:
            continue
        identity = source_event_id or hashlib.sha256(f"{code}|{title}|{raw_timestamp}".encode("utf-8")).hexdigest()
        result.append(
            {
                "source": SOURCE,
                "sourceEventId": identity,
                "code": code,
                "title": title,
                "rawPublishedAt": raw_timestamp,
                **classification,
            }
        )
    return result


def _record_codes(record: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in ("codes", "CODES", "stock_list", "stockList"):
        raw = record.get(key)
        if isinstance(raw, list):
            for item in raw:
                values.append(item.get("stock_code") or item.get("code") if isinstance(item, dict) else item)
        elif raw:
            values.extend(str(raw).split(","))
    for key in ("stock_code", "SECURITY_CODE", "code"):
        if record.get(key):
            values.append(record.get(key))
    return sorted({_normalize_code(value) for value in values if _normalize_code(value)})


def _normalize_events(events: List[Dict[str, Any]], cutoff: datetime) -> List[Dict[str, Any]]:
    normalized = []
    for raw in events:
        event = dict(raw)
        published_at = _parse_datetime(event.get("rawPublishedAt") or event.get("publishedAt"))
        exact = bool(published_at and _has_exact_time(event.get("rawPublishedAt") or event.get("publishedAt")))
        event["publishedAt"] = published_at.isoformat(timespec="seconds") if published_at else ""
        event["timestampStatus"] = "exact" if exact else "date_only" if published_at else "unavailable"
        event["withinCutoff"] = bool(exact and published_at and published_at <= cutoff)
        normalized.append(event)
    normalized.sort(key=lambda item: (str(item.get("code") or ""), str(item.get("publishedAt") or ""), str(item.get("sourceEventId") or "")))
    return normalized


def _assess_codes(
    codes: List[str],
    covered_codes: set[str],
    events: List[Dict[str, Any]],
    cutoff: datetime,
) -> Dict[str, Dict[str, Any]]:
    by_code: Dict[str, List[Dict[str, Any]]] = {code: [] for code in codes}
    for event in events:
        code = _normalize_code(event.get("code"))
        if code in by_code:
            by_code[code].append(event)
    assessments: Dict[str, Dict[str, Any]] = {}
    severity_rank = {"info": 0, "medium": 1, "high": 2, "critical": 3}
    for code in codes:
        rows = by_code.get(code) or []
        exact_rows = [row for row in rows if row.get("timestampStatus") == "exact" and row.get("withinCutoff")]
        uncertain_rows = [
            row for row in rows
            if row.get("category") != "other" and row.get("timestampStatus") != "exact"
        ]
        risk_rows = [row for row in exact_rows if row.get("severity") in {"high", "critical"}]
        if code not in covered_codes:
            status = "unavailable"
        elif uncertain_rows:
            status = "partial"
        elif risk_rows:
            status = "risk"
        else:
            status = "clear"
        max_severity = max(
            [str(row.get("severity") or "info") for row in risk_rows],
            key=lambda value: severity_rank.get(value, 0),
            default="none",
        )
        assessments[code] = {
            "eventRiskStatus": status,
            "evidenceStatus": "available" if status in {"clear", "risk"} else "unavailable",
            "providerVersion": PROVIDER_VERSION,
            "classifierVersion": CLASSIFIER_VERSION,
            "source": SOURCE,
            "evidenceCutoffAt": cutoff.isoformat(timespec="seconds"),
            "triggered": bool(risk_rows),
            "highestSeverity": max_severity,
            "hitCategories": sorted({str(row.get("category") or "") for row in risk_rows if row.get("category")}),
            "events": exact_rows,
            "uncertainEvents": uncertain_rows,
            "reason": (
                "；".join(str(row.get("title") or "") for row in risk_rows[:3])
                if risk_rows
                else "存在发布时间不完整的潜在风险公告"
                if uncertain_rows
                else "事件数据源未覆盖该候选"
                if code not in covered_codes
                else "证据截止时间前未发现高风险事件"
            ),
        }
    return assessments


def _deduplicate_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for event in events:
        key = str(event.get("sourceEventId") or "")
        code = _normalize_code(event.get("code"))
        identity = f"{code}:{key}" if key else hashlib.sha256(json.dumps(event, sort_keys=True).encode("utf-8")).hexdigest()
        result[identity] = event
    return list(result.values())


def _normalize_code(value: Any) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _has_exact_time(value: Any) -> bool:
    text = str(value or "").strip()
    match = re.search(r"[T\s](\d{2}):(\d{2})(?::(\d{2}))?", text)
    if not match:
        return False
    return any(int(group or 0) != 0 for group in match.groups())


def _snapshot_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not snapshot:
        return {}
    return {
        key: snapshot.get(key)
        for key in (
            "id",
            "runId",
            "recommendationDate",
            "evidenceCutoffAt",
            "source",
            "status",
            "requestedCount",
            "coveredCount",
            "coveragePct",
            "eventCount",
            "timestampAuditPassed",
            "createdAt",
        )
    }
