"""Read-only deterministic policy replay; never writes DB or calls a provider/AI.

Historical row-count 15m buckets are not valid input to new structure detectors.
Only archived real 1m series can regenerate clock-aligned 15m evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.engines import t1_selection_policy as policy
from backend.app.engines.filter_engine import hard_filter_reason
from backend.app.engines.t1_candidate_allocation import (
    VERSION as ALLOCATION_VERSION, allocate_round, build_preliminary_pool, clip_minute_series, preliminary_candidate,
)
from backend.app.providers.tencent_kline_provider import _aggregate_series
from backend.app.services import t1_overnight_selection_service as svc


def distribution(values):
    values = [float(v) for v in values if v is not None]
    return dict(count=len(values), minimum=min(values) if values else None,
        maximum=max(values) if values else None, mean=round(mean(values), 3) if values else None,
        median=round(median(values), 3) if values else None,
        atLeast95=sum(v >= 95 for v in values))


def replay(database, run_id, snapshot_dir):
    with sqlite3.connect(f"{Path(database).resolve().as_uri()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        run_row = conn.execute("SELECT * FROM t1_overnight_selection_runs WHERE id=?", (run_id,)).fetchone()
        if run_row is None:
            raise ValueError(f"Run {run_id} not found")
        run = dict(run_row)
        archive = [dict(r) for r in conn.execute("SELECT * FROM t1_overnight_candidate_archives WHERE run_id=? ORDER BY code", (run_id,))]
        minutes = [dict(r) for r in conn.execute("SELECT * FROM t1_overnight_minute_snapshots WHERE run_id=? ORDER BY code,period", (run["evidence_run_id"],))]
        items = [dict(r) for r in conn.execute("SELECT * FROM t1_overnight_selection_items WHERE run_id=? ORDER BY code,stage", (run_id,))]
    market = json.loads(run["market_json"])
    request = json.loads(run["request_json"])
    frozen = {r["code"]: json.loads(r["initial_json"]) for r in archive}
    state = {r["code"]: json.loads(r["state_json"]) for r in archive}
    old_pool = {r["code"]: json.loads(r["item_json"]) for r in items if r["stage"] == "pool"}
    snapshot_version = (market.get("marketSnapshotFreeze") or {}).get("snapshotVersion")
    snapshot_path = Path(snapshot_dir) / f"{snapshot_version}.json"
    snapshot = json.loads(snapshot_path.read_text()) if snapshot_path.is_file() else None
    source_digest = hashlib.sha256(json.dumps(dict(run=run, archive=archive, minutes=minutes,
        items=items, snapshot=snapshot), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    old_120 = {c for c, s in state.items() if s.get("minute15mRequested")}
    one_minute = {r["code"]: r for r in minutes if r["period"] == "1m" and r["point_count"] > 0}
    replayed = []
    for code, row in sorted(one_minute.items()):
        if code not in frozen:
            continue
        initial = frozen[code]
        minute_series = clip_minute_series(json.loads(row["series_json"]), run["recommendation_date"], run["scan_time"], "1m")
        regenerated = clip_minute_series(_aggregate_series(minute_series, 15), run["recommendation_date"], run["scan_time"], "15m")
        base = dict(old_pool.get(code) or initial, series=regenerated, klineSource=row["source"],
            amount=initial["amount"], dataEligible=bool(state[code].get("qualityEligible")),
            quoteTradeDate=run["recommendation_date"])
        candidate = policy.annotate(base)
        facts = svc._tail_support_shadow(candidate, minute_series, run["scan_time"])
        facts.update(actualLastTime=(minute_series[-1] if minute_series else {}).get("time"),
                     source=row["source"], tradeDate=run["recommendation_date"])
        policy.formal_tail(candidate, facts, run["scan_time"])
        replayed.append(dict(code=code, oldScore=old_pool.get(code, {}).get("t1Score"),
            newScore=candidate["t1Score"], complete15mCount=len(regenerated),
            setupStatus=candidate["setupStatus"], types=candidate["candidateTypes"],
            formalEligible=candidate["formalEligible"], reasons=candidate["formalExclusionReasons"],
            tail=candidate["tailSupportPackV2"]))
    by_code = {r["code"]: r for r in replayed}
    preliminary_report = {"status": "snapshot_missing"}
    if snapshot:
        eligible = []
        for raw in snapshot["stocks"]:
            stock = svc._stock_from_dict(raw)
            allowed, _ = hard_filter_reason(stock, include_bse=False, include_star_market=False)
            if allowed and stock.limit_status not in {"down", "limit_down", "one_word_limit_down"}:
                eligible.append(stock)
        # Identity is only used if frozen in this Run, never pulled from today's cache.
        identity = {c: {"industry": r.get("industry") or ""} for c, r in frozen.items()}
        new_pool = build_preliminary_pool([preliminary_candidate(s, identity.get(s.code)) for s in eligible])
        seed = request.get("allocationSeed") or f"{run['recommendation_date']}:{ALLOCATION_VERSION}:{snapshot_version}"
        round_one = allocate_round(new_pool, 1, seed)
        feedback = {r["code"]: dict(qualityEligible=state.get(r["code"], {}).get("qualityEligible", False),
            setupEligible=r["setupStatus"] == "confirmed", confirmedTypes=r["types"], verifiedScore=r["newScore"])
            for r in replayed}
        round_two = allocate_round(new_pool, 2, seed, round_one, feedback)
        new_120 = {r["code"] for r in round_one + round_two}
        preliminary_report = dict(status="quote_recall_replayed_allocation_conditional",
            allocationSeed=seed, received=len(snapshot["stocks"]), hardFiltered=len(eligible), newRecallCount=len(new_pool),
            recallOverlap=len({r["code"] for r in new_pool} & set(frozen)),
            roundCounts=[len(round_one), len(round_two)], groupCounts=dict(Counter(r["primaryGroup"] for r in round_one+round_two)),
            validationOverlap=len(new_120 & old_120), newRecallCodes=[r["code"] for r in new_pool],
            conditionalValidationCodes=sorted(new_120),
            limitation="全源报价可回放；全集中未冻结的行业身份未知。第二轮缺少多数新分配股票当时的正确分钟证据，名单为条件对照，不能视为完整新Run结果。")
    old_validated = [old_pool[c] for c in old_120 if c in old_pool]
    new_quote_scores = [policy.quote_score(svc._stock_from_dict(frozen[c]["rawStock"]))[0] for c in old_120]
    official = json.loads(run["final_picks_json"])
    return dict(runId=run_id, recommendationDate=run["recommendation_date"], scanTime=run["scan_time"],
        policyVersion=policy.VERSION, allocationVersion=ALLOCATION_VERSION, parameters=policy.PARAMETERS,
        inputSha256=source_digest, snapshotVersion=snapshot_version, databaseMode="read_only", aiCalls=0,
        sourceCoverageStatus="unknown_authoritative_universe_missing", originalCoverage=(market.get("cacheStatus") or {}).get("coverage"),
        ablations={"semantics": {"oldFund": distribution((c.get("t1Dimensions") or {}).get("fundStrength") for c in old_validated),
            "newLiquidity": distribution(policy.clamp((math.log10(max(1, frozen[c]["amount"]))-6)*22) for c in old_120),
            "newQuoteScore": distribution(new_quote_scores), "meaning": "不同分项仅比较饱和与分布，不作为收益或同量纲提升"},
            "recall": preliminary_report, "correctedStructureAndTail": {
                "replayedCount": len(replayed), "originalValidatedCount": len(old_120),
                "unreplayableValidatedCount": len(old_120 - set(by_code)),
                "setupConfirmedCount": sum(r["setupStatus"] == "confirmed" for r in replayed),
                "formalEligibleCount": sum(r["formalEligible"] for r in replayed),
                "primaryTypeCounts": dict(Counter(r["types"][0] for r in replayed)), "rows": replayed}},
        originalOfficialAudit=[dict(code=p["code"], replay=by_code.get(p["code"]),
            status="replayed" if p["code"] in by_code else "unreplayable_no_1m") for p in official],
        limitations=["旧120只的15m为按行数聚合，不能充当修正后的正确15m证据；仅30只真实1m可重建。",
            "未重新调用AI，未生成或写回新的正式推荐，未修改历史事实。",
            "没有同版本次日严格执行样本；不报告收益改善，不根据未来数据调参。"],
        profitabilityStatus="pending_forward_validation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="data/app.sqlite")
    parser.add_argument("--run-id", type=int, default=98)
    parser.add_argument("--snapshot-dir", default="data/full_market_context_snapshots")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.database, args.run_id, args.snapshot_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"runId": result["runId"], "inputSha256": result["inputSha256"],
        "replay": {k: v for k, v in result["ablations"]["correctedStructureAndTail"].items() if k != "rows"},
        "output": str(args.output)}, ensure_ascii=False))
