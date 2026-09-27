from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.database import Database
from backend.app.market_context_store import FULL_MARKET_CONTEXT_CACHE_PATH
from backend.app.services.stock_analysis_feedback_service import StockAnalysisFeedbackService
from backend.app.services.stock_analysis_service import StockAnalysisService
from backend.app.settings import load_settings


LOGGER = logging.getLogger("ai_stock.collect_stock_analysis_v2")
RISK_MODES = ("balanced", "aggressive", "defensive")
SCAN_TIMES = ("09:50", "10:42", "13:18", "14:36")
SAMPLE_BUCKETS = ("momentum", "neutral", "weak")
DEFAULT_TARGET_PER_BUCKET = "momentum:15,neutral:15,weak:10"


def main() -> int:
    parser = argparse.ArgumentParser(description="Manually collect V2 stock analysis samples")
    parser.add_argument("--codes", default="", help="Comma separated 6-digit stock codes. Overrides cache selection.")
    parser.add_argument("--codes-file", default="", help="Optional text file, one stock code per line.")
    parser.add_argument("--limit", type=int, default=20, help="Max stocks selected from full-market cache when --codes is empty.")
    parser.add_argument(
        "--strategy",
        choices=["stratified", "activity"],
        default="stratified",
        help="Candidate selection strategy. stratified builds momentum/neutral/weak sample pools.",
    )
    parser.add_argument(
        "--target-per-bucket",
        default=DEFAULT_TARGET_PER_BUCKET,
        help="Target sample structure for --strategy stratified, e.g. momentum:15,neutral:15,weak:10.",
    )
    parser.add_argument(
        "--skip-today-existing",
        action="store_true",
        help="Skip stocks already analyzed today in the same risk mode.",
    )
    parser.add_argument("--risk-mode", choices=[*RISK_MODES, "all"], default="balanced")
    parser.add_argument("--scan-time", choices=SCAN_TIMES, default="10:42")
    parser.add_argument("--market-source", default="auto")
    parser.add_argument("--force-market-context", action="store_true", help="Refresh full-market context before the first analysis.")
    parser.add_argument("--no-ai", action="store_true", help="Collect rules-only V2 samples without calling DeepSeek.")
    parser.add_argument("--dry-run", action="store_true", help="Only show selected candidates; do not analyze or write DB rows.")
    parser.add_argument("--sleep", type=float, default=0.5, help="Seconds to sleep between analyses.")
    parser.add_argument("--refresh-outcomes", action="store_true", help="After analysis, backfill V2 direction outcomes.")
    parser.add_argument("--refresh-trade-outcomes", action="store_true", help="After analysis, backfill V2 strategy trade outcomes.")
    parser.add_argument("--horizons", default="3,5,7,10", help="Outcome horizons used with --refresh-outcomes.")
    parser.add_argument("--outcome-limit", type=int, default=500)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    risk_modes = list(RISK_MODES) if args.risk_mode == "all" else [args.risk_mode]
    settings = None
    database = None
    existing_by_risk: Dict[str, set[str]] = {}
    if args.skip_today_existing:
        settings = load_settings()
        database = Database(settings.database_path)
        existing_by_risk = _today_existing_codes(database, risk_modes)

    candidates, selection_report = _load_candidates(
        args,
        excluded_codes=_selection_exclude_codes(args, risk_modes, existing_by_risk),
    )
    if args.dry_run:
        print(
            json.dumps(
                {
                    "dryRun": True,
                    "version": "v2",
                    "strategy": args.strategy,
                    "riskModes": risk_modes,
                    "skipTodayExisting": bool(args.skip_today_existing),
                    "todayExistingByRisk": {risk: len(codes) for risk, codes in existing_by_risk.items()},
                    "wouldSkipTodayExisting": _preview_today_existing_skips(candidates, risk_modes, existing_by_risk),
                    "selectionReport": selection_report,
                    "candidateCount": len(candidates),
                    "candidates": candidates,
                    "note": "dry-run only previews candidates and does not write stock_analysis_*_v2 tables.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if settings is None:
        settings = load_settings()
    if database is None:
        database = Database(settings.database_path)
    analysis_service = StockAnalysisService(settings, database, feedback_version="v2")
    feedback_service = StockAnalysisFeedbackService(settings, database, version="v2")

    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    skipped_existing: List[Dict[str, Any]] = []
    force_next = bool(args.force_market_context)
    for risk_mode in risk_modes:
        for item in candidates:
            code = item["code"]
            if args.skip_today_existing and code in existing_by_risk.get(risk_mode, set()):
                skipped_existing.append(
                    {
                        "code": code,
                        "name": item.get("name", ""),
                        "bucket": item.get("bucket", ""),
                        "riskMode": risk_mode,
                        "reason": "already_analyzed_today",
                    }
                )
                continue
            started = time.perf_counter()
            try:
                payload = analysis_service.analyze(
                    code=code,
                    risk_mode=risk_mode,
                    scan_time=args.scan_time,
                    use_ai=not args.no_ai,
                    market_source=args.market_source,
                    force_market_context=force_next,
                )
                force_next = False
                decision = payload.get("decision") or {}
                results.append(
                    {
                        "code": payload.get("code", code),
                        "name": payload.get("name", item.get("name", "")),
                        "bucket": item.get("bucket", ""),
                        "riskMode": risk_mode,
                        "scanTime": args.scan_time,
                        "snapshotId": payload.get("snapshotId"),
                        "analysisVersion": payload.get("analysisVersion"),
                        "aiMode": payload.get("aiMode"),
                        "ruleFallbackUsed": payload.get("ruleFallbackUsed"),
                        "decision": decision.get("decision"),
                        "primaryHorizon": decision.get("primaryHorizon"),
                        "confidence": decision.get("confidence"),
                        "caseStatus": (payload.get("caseMemoryStatus") or {}).get("status"),
                        "elapsedMs": round((time.perf_counter() - started) * 1000),
                    }
                )
                LOGGER.info("collected v2 stock analysis code=%s risk=%s snapshot_id=%s", code, risk_mode, payload.get("snapshotId"))
            except Exception as error:  # noqa: BLE001 - keep batch collection going.
                force_next = False
                LOGGER.warning("collect v2 stock analysis failed code=%s risk=%s error=%s", code, risk_mode, error)
                errors.append(
                    {
                        "code": code,
                        "name": item.get("name", ""),
                        "bucket": item.get("bucket", ""),
                        "riskMode": risk_mode,
                        "error": str(error),
                    }
                )
            if args.sleep > 0:
                time.sleep(args.sleep)

    outcome_result: Dict[str, Any] = {}
    if args.refresh_outcomes:
        outcome_result = feedback_service.update_outcomes(
            horizons=_parse_horizons(args.horizons),
            limit=args.outcome_limit,
            mode="backfill",
            dry_run=False,
        )
    trade_outcome_result: Dict[str, Any] = {}
    if args.refresh_trade_outcomes:
        trade_outcome_result = feedback_service.update_trade_outcomes(
            limit=args.outcome_limit,
            mode="backfill",
            dry_run=False,
        )

    print(
        json.dumps(
            {
                "dryRun": False,
                "version": "v2",
                "analysisVersion": "stock_analysis_v2",
                "strategy": args.strategy,
                "skipTodayExisting": bool(args.skip_today_existing),
                "selectionReport": selection_report,
                "candidateCount": len(candidates),
                "riskModes": risk_modes,
                "analyzedCount": len(results),
                "errorCount": len(errors),
                "skippedTodayExistingCount": len(skipped_existing),
                "skippedTodayExisting": skipped_existing,
                "snapshotCount": sum(1 for item in results if item.get("snapshotId")),
                "aiSuccessCount": sum(1 for item in results if item.get("aiMode") not in {"", None, "rules"} and not item.get("ruleFallbackUsed")),
                "ruleFallbackCount": sum(1 for item in results if item.get("ruleFallbackUsed")),
                "resultByBucket": _count_by(results, "bucket"),
                "errorByBucket": _count_by(errors, "bucket"),
                "results": results,
                "errors": errors,
                "outcomes": outcome_result,
                "tradeOutcomes": trade_outcome_result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not errors else 1


def _load_candidates(args: argparse.Namespace, *, excluded_codes: set[str] | None = None) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    manual = _manual_codes(args.codes) + _file_codes(args.codes_file)
    if manual:
        candidates = _dedupe_candidates(manual)
        selected, skipped = _exclude_existing_from_selection(candidates, excluded_codes or set())
        selected = selected[: max(1, args.limit)]
        return selected, {
            "source": "manual",
            "strategy": "manual",
            "selectedByBucket": _count_by(selected, "bucket"),
            "skippedTodayExistingFromSelection": len(skipped),
            "skippedTodayExistingCodes": skipped[:50],
        }
    return _cache_candidates(
        limit=args.limit,
        strategy=args.strategy,
        target_per_bucket=args.target_per_bucket,
        excluded_codes=excluded_codes or set(),
    )


def _manual_codes(value: str) -> List[Dict[str, str]]:
    result = []
    for part in str(value or "").replace("，", ",").split(","):
        code = _normalize_code(part)
        if code:
            result.append({"code": code, "name": "", "bucket": "manual"})
    return result


def _file_codes(path_value: str) -> List[Dict[str, str]]:
    if not path_value:
        return []
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.replace(",", " ").split()
        code = _normalize_code(parts[0] if parts else "")
        if code:
            rows.append({"code": code, "name": parts[1] if len(parts) > 1 else "", "bucket": "manual"})
    return rows


def _cache_candidates(
    *,
    limit: int,
    strategy: str,
    target_per_bucket: str,
    excluded_codes: set[str],
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not FULL_MARKET_CONTEXT_CACHE_PATH.exists():
        raise RuntimeError(f"没有找到全市场缓存：{FULL_MARKET_CONTEXT_CACHE_PATH}")
    payload = json.loads(FULL_MARKET_CONTEXT_CACHE_PATH.read_text(encoding="utf-8"))
    stocks = payload.get("stocks") if isinstance(payload, dict) else []
    if not isinstance(stocks, list) or not stocks:
        raise RuntimeError("全市场缓存没有 stocks 列表，无法自动选择 V2 采集样本")
    if strategy == "activity":
        return _activity_candidates(stocks, limit=limit, excluded_codes=excluded_codes)
    return _stratified_candidates(stocks, limit=limit, target_per_bucket=target_per_bucket, excluded_codes=excluded_codes)


def _activity_candidates(
    stocks: List[Any],
    *,
    limit: int,
    excluded_codes: set[str],
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    skipped_existing: List[Dict[str, Any]] = []
    for item in stocks:
        row = _eligible_cache_row(item)
        if row is None:
            continue
        if row["code"] in excluded_codes:
            skipped_existing.append({"code": row["code"], "name": row.get("name", ""), "bucket": "activity"})
            continue
        amount_factor = row.get("amountFactor") or 0
        volume_ratio = row.get("volumeRatio") or 0
        rel_strength = row.get("relStrength") or 0
        turnover = row.get("turnover") or 0
        if amount_factor < 1.0 and volume_ratio < 1.0:
            continue
        row["bucket"] = "activity"
        row["score"] = round(amount_factor * 35 + volume_ratio * 12 + rel_strength * 0.4 + min(turnover, 20), 3)
        filtered.append(row)
    filtered.sort(key=lambda row: row["score"], reverse=True)
    selected = _dedupe_candidates(filtered)[: max(1, limit)]
    return selected, {
        "source": "full_market_context_cache",
        "strategy": "activity",
        "availableByBucket": {"activity": len(filtered)},
        "selectedByBucket": _count_by(selected, "bucket"),
        "skippedTodayExistingFromSelection": len(skipped_existing),
        "skippedTodayExistingCodes": skipped_existing[:50],
    }


def _stratified_candidates(
    stocks: List[Any],
    *,
    limit: int,
    target_per_bucket: str,
    excluded_codes: set[str],
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    raw_targets = _parse_bucket_targets(target_per_bucket)
    effective_targets = _limit_bucket_targets(raw_targets, max(1, limit))
    buckets: Dict[str, List[Dict[str, Any]]] = {bucket: [] for bucket in SAMPLE_BUCKETS}
    skipped_existing: List[Dict[str, Any]] = []
    for item in stocks:
        row = _eligible_cache_row(item)
        if row is None:
            continue
        bucket = _classify_bucket(row)
        row["bucket"] = bucket
        row["score"] = round(_bucket_score(row, bucket), 3)
        if row["code"] in excluded_codes:
            skipped_existing.append({"code": row["code"], "name": row.get("name", ""), "bucket": bucket})
            continue
        buckets[bucket].append(row)
    for rows in buckets.values():
        rows.sort(key=lambda row: row["score"], reverse=True)

    selected: List[Dict[str, Any]] = []
    selected_keys: set[str] = set()
    for bucket in SAMPLE_BUCKETS:
        for row in buckets[bucket][: effective_targets.get(bucket, 0)]:
            if row["code"] in selected_keys:
                continue
            selected.append(row)
            selected_keys.add(row["code"])

    if len(selected) < max(1, limit):
        leftovers: List[Dict[str, Any]] = []
        for bucket in SAMPLE_BUCKETS:
            leftovers.extend(row for row in buckets[bucket] if row["code"] not in selected_keys)
        leftovers.sort(key=lambda row: row["score"], reverse=True)
        for row in leftovers:
            if len(selected) >= max(1, limit):
                break
            selected.append(row)
            selected_keys.add(row["code"])

    selected = _dedupe_candidates(selected)[: max(1, limit)]
    return selected, {
        "source": "full_market_context_cache",
        "strategy": "stratified",
        "requestedTargetPerBucket": raw_targets,
        "effectiveTargetPerBucket": effective_targets,
        "availableByBucket": {bucket: len(rows) for bucket, rows in buckets.items()},
        "selectedByBucket": _count_by(selected, "bucket"),
        "skippedTodayExistingFromSelection": len(skipped_existing),
        "skippedTodayExistingCodes": skipped_existing[:50],
    }


def _eligible_cache_row(item: Any) -> Dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    code = _normalize_code(item.get("code"))
    if not code or not _supported_a_share_code(code):
        return None
    if item.get("suspended") or item.get("st"):
        return None
    change = _num(item.get("change"))
    if change is not None and (change > 9.7 or change < -7):
        return None
    if str(item.get("limit_status") or "") in {"one_word_limit_up", "limit_down"}:
        return None
    return {
        "code": code,
        "name": str(item.get("name") or ""),
        "change": _num(item.get("change")) or 0,
        "amountFactor": _num(item.get("amount_factor")) or 0,
        "volumeRatio": _num(item.get("volume_ratio")) or 0,
        "relStrength": _num(item.get("rel_strength")) if _num(item.get("rel_strength")) is not None else 50,
        "turnover": _num(item.get("turnover")) or 0,
        "limitStatus": str(item.get("limit_status") or ""),
    }


def _classify_bucket(row: Dict[str, Any]) -> str:
    change = float(row.get("change") or 0)
    amount_factor = float(row.get("amountFactor") or 0)
    volume_ratio = float(row.get("volumeRatio") or 0)
    rel_strength = float(row.get("relStrength") or 50)
    if change >= 1.2 and (amount_factor >= 1.1 or volume_ratio >= 1.15 or rel_strength >= 60):
        return "momentum"
    if change <= -1.0 or rel_strength <= 40 or (amount_factor <= 0.75 and volume_ratio <= 0.85):
        return "weak"
    return "neutral"


def _bucket_score(row: Dict[str, Any], bucket: str) -> float:
    change = float(row.get("change") or 0)
    amount_factor = float(row.get("amountFactor") or 0)
    volume_ratio = float(row.get("volumeRatio") or 0)
    rel_strength = float(row.get("relStrength") or 50)
    turnover = float(row.get("turnover") or 0)
    if bucket == "momentum":
        return change * 6 + amount_factor * 30 + volume_ratio * 12 + rel_strength * 0.5 + min(turnover, 20) * 0.5
    if bucket == "weak":
        return abs(min(change, 0)) * 8 + (100 - rel_strength) * 0.5 + max(1 - amount_factor, 0) * 20 + max(1 - volume_ratio, 0) * 12 + min(turnover, 20) * 0.2
    return 100 - abs(change) * 10 + min(amount_factor, 2) * 15 + min(volume_ratio, 2) * 8 + (100 - abs(rel_strength - 50)) * 0.3


def _dedupe_candidates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        code = _normalize_code(row.get("code"))
        if not code or code in seen:
            continue
        seen.add(code)
        cleaned = dict(row)
        cleaned["code"] = code
        cleaned["name"] = str(row.get("name") or "")
        result.append(cleaned)
    return result


def _exclude_existing_from_selection(rows: List[Dict[str, Any]], excluded_codes: set[str]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not excluded_codes:
        return rows, []
    selected = []
    skipped = []
    for row in rows:
        if row.get("code") in excluded_codes:
            skipped.append({"code": row.get("code"), "name": row.get("name", ""), "bucket": row.get("bucket", "")})
            continue
        selected.append(row)
    return selected, skipped


def _parse_bucket_targets(value: str) -> Dict[str, int]:
    aliases = {"strong": "momentum", "momentum": "momentum", "neutral": "neutral", "weak": "weak", "risk": "weak"}
    result: Dict[str, int] = {}
    for part in str(value or "").replace("，", ",").split(","):
        if ":" not in part:
            continue
        key, raw_number = part.split(":", 1)
        bucket = aliases.get(key.strip().lower())
        if not bucket:
            continue
        try:
            number = int(raw_number.strip())
        except ValueError:
            continue
        if number > 0:
            result[bucket] = number
    if not result:
        return {"momentum": 15, "neutral": 15, "weak": 10}
    for bucket in SAMPLE_BUCKETS:
        result.setdefault(bucket, 0)
    return result


def _limit_bucket_targets(targets: Dict[str, int], limit: int) -> Dict[str, int]:
    limit = max(1, limit)
    total = sum(max(0, targets.get(bucket, 0)) for bucket in SAMPLE_BUCKETS)
    if total <= 0:
        return {"momentum": limit, "neutral": 0, "weak": 0}
    if total <= limit:
        return {bucket: max(0, targets.get(bucket, 0)) for bucket in SAMPLE_BUCKETS}
    scaled: Dict[str, int] = {}
    remainders = []
    for bucket in SAMPLE_BUCKETS:
        exact = max(0, targets.get(bucket, 0)) * limit / total
        scaled[bucket] = int(exact)
        remainders.append((exact - scaled[bucket], bucket))
    remaining = limit - sum(scaled.values())
    for _, bucket in sorted(remainders, reverse=True):
        if remaining <= 0:
            break
        scaled[bucket] += 1
        remaining -= 1
    return scaled


def _selection_exclude_codes(
    args: argparse.Namespace,
    risk_modes: List[str],
    existing_by_risk: Dict[str, set[str]],
) -> set[str]:
    if not args.skip_today_existing or len(risk_modes) != 1:
        return set()
    return set(existing_by_risk.get(risk_modes[0], set()))


def _today_existing_codes(database: Database, risk_modes: List[str]) -> Dict[str, set[str]]:
    today = datetime.now().date().isoformat()
    result: Dict[str, set[str]] = {risk_mode: set() for risk_mode in risk_modes}
    if not risk_modes:
        return result
    placeholders = ",".join("?" for _ in risk_modes)
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT code, risk_mode
            FROM stock_analysis_snapshots_v2
            WHERE analysis_date = ? AND risk_mode IN ({placeholders})
            GROUP BY code, risk_mode
            """,
            [today, *risk_modes],
        ).fetchall()
    for row in rows:
        result.setdefault(str(row["risk_mode"]), set()).add(str(row["code"]))
    return result


def _preview_today_existing_skips(
    candidates: List[Dict[str, Any]],
    risk_modes: List[str],
    existing_by_risk: Dict[str, set[str]],
) -> Dict[str, Any]:
    skipped = []
    for risk_mode in risk_modes:
        existing = existing_by_risk.get(risk_mode, set())
        for item in candidates:
            if item.get("code") in existing:
                skipped.append(
                    {
                        "code": item.get("code"),
                        "name": item.get("name", ""),
                        "bucket": item.get("bucket", ""),
                        "riskMode": risk_mode,
                        "reason": "already_analyzed_today",
                    }
                )
    return {"count": len(skipped), "items": skipped[:50]}


def _count_by(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _normalize_code(value: Any) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(text) >= 6:
        return text[-6:]
    return text.zfill(6) if text else ""


def _supported_a_share_code(code: str) -> bool:
    return code.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605"))


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_horizons(value: str) -> List[int]:
    result = []
    for part in str(value or "").split(","):
        try:
            number = int(part.strip())
        except ValueError:
            continue
        if number > 0:
            result.append(number)
    return result or [3, 5, 7, 10]


if __name__ == "__main__":
    sys.exit(main())
