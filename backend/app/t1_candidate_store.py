"""Persistent frozen candidate archives and shared unadjusted daily observations."""
from __future__ import annotations

import json
import hashlib
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime

from backend.app.engines.t1_candidate_allocation import VERSION


REVIEW_SCHEMA_VERSION = "2026-11-07.v1"

TERMINAL_STAGE_LABELS = {
    "hard_filter_rejected": "硬过滤淘汰",
    "pool_not_recalled": "未进入召回500",
    "validation_not_allocated": "未分配验证120",
    "minute15m_technical_failure": "15分钟请求技术失败",
    "minute15m_quality_rejected": "15分钟数据质量不合格",
    "model_observation_only": "模型收集中，仅观察",
    "tail_not_allocated": "未分配尾盘校准",
    "minute1m_technical_failure": "1分钟请求技术失败",
    "tail_negative_rejected": "尾盘证据走弱",
    "event_risk_rejected": "事件风险淘汰",
    "non_positive_expectation": "保守净期望不为正",
    "tradeability_rejected": "推荐日不可交易",
    "shortlist_not_portfolio": "短名单未进入组合",
    "v3_shadow_selected": "V3影子组合入选",
    "legacy_validation_not_allocated": "未分配15分钟验证",
    "legacy_minute15m_technical_failure": "15分钟请求技术失败",
    "legacy_minute15m_quality_rejected": "15分钟数据质量不合格",
    "legacy_setup_rejected": "形态未成立",
    "legacy_ai_batch_rejected": "AI批次未晋级",
    "legacy_tail_rejected": "尾盘资格未通过",
    "legacy_final_not_selected": "AI终选未入选",
    "legacy_guardrail_rejected": "最终护栏淘汰",
    "legacy_official_selected": "正式入选",
    "legacy_stage_unknown": "旧版层级未知",
    "stage_unknown": "层级未知",
}


def _text_reasons(values):
    if isinstance(values, str):
        values = [values]
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def candidate_identity_tags(item):
    tags = []
    for value, label in (
        (item.get("semifinalSelected"), "legacy_semifinal"),
        (item.get("officialSelected"), "official_pick"),
        (item.get("ruleShadowSelected"), "rule_shadow"),
        (item.get("aiShadowSelected"), "ai_shadow"),
        (item.get("terminalStage") == "v3_shadow_selected", "v3_shadow_pick"),
        (item.get("v3CalibrationSample"), "tail_calibration_sample"),
    ):
        if value and label not in tags:
            tags.append(label)
    return tags


def legacy_candidate_review(item):
    """Derive one mutually exclusive terminal stage from immutable legacy facts."""
    reasons = []
    if item.get("officialSelected"):
        stage = "legacy_official_selected"
        reasons = ["当日正式推荐名单已冻结"]
    elif int(item.get("allocationRound") or 0) <= 0:
        stage = "legacy_validation_not_allocated"
        reasons = [item.get("exclusionReason") or "旧版500池内未获得15分钟验证名额"]
    elif str(item.get("minute15mStatus") or "") in {"failed", "unavailable"}:
        stage = "legacy_minute15m_technical_failure"
        reasons = [item.get("minute15mError") or "15分钟数据请求或依赖失败"]
    elif str(item.get("minute15mStatus") or "") == "quality_rejected" or not item.get("qualityEligible"):
        stage = "legacy_minute15m_quality_rejected"
        reasons = _text_reasons((item.get("dataQuality") or {}).get("missing")) or ["15分钟数据未通过质量校验"]
    elif item.get("setupEligible") is False:
        stage = "legacy_setup_rejected"
        reasons = _text_reasons((item.get("setupEvidence") or {}).get("reasons")) or [item.get("exclusionReason") or "未确认可交易形态"]
    elif item.get("setupEligible") is True and not item.get("semifinalSelected"):
        stage = "legacy_ai_batch_rejected"
        reasons = ["形态成立，但未被旧版AI批次选入半决赛"]
    elif item.get("semifinalSelected") and item.get("formalEligible") is False:
        stage = "legacy_tail_rejected"
        reasons = _text_reasons(item.get("formalExclusionReasons")) or ["未通过正式尾盘资格"]
    elif item.get("aiFinalSelected") and not item.get("officialSelected"):
        stage = "legacy_guardrail_rejected"
        reasons = _text_reasons(item.get("guardrailRejectionReasons")) or ["AI终选后被最终护栏拦截"]
    elif item.get("semifinalSelected") and not item.get("officialSelected"):
        stage = "legacy_final_not_selected"
        reasons = ["进入半决赛，但未进入当日正式推荐"]
    else:
        stage = "legacy_stage_unknown"
        reasons = ["旧版字段不足，无法严格还原最终停留层级"]
    return {
        "terminalStage": stage,
        "terminalStageLabel": TERMINAL_STAGE_LABELS[stage],
        "terminalReason": "；".join(_text_reasons(reasons)),
        "terminalRuleVersion": str(item.get("selectionPolicyVersion") or item.get("archiveVersion") or VERSION),
        "identityTags": candidate_identity_tags(item),
        "reviewScope": "legacy_500",
    }


def v3_candidate_review(item):
    """Derive a terminal stage for every V3 full-market fact, including unallocated facts."""
    tail = item.get("v3TailGate") or {}
    estimate = item.get("netExpectationEstimate") or {}
    stored = str(item.get("terminalStage") or "")
    reasons = []
    if str(item.get("hardFilterStatus") or "eligible") == "rejected":
        stage = "hard_filter_rejected"
        reasons = _text_reasons(item.get("hardFilterReasons")) or ["不满足V3硬过滤要求"]
    elif not item.get("v3RecallRank"):
        stage = "pool_not_recalled"
        reasons = ["硬过滤合格，但未进入当日V3召回500"]
    elif not item.get("v3ValidationRank"):
        stage = "validation_not_allocated"
        reasons = ["已进入V3召回500，但未获得120只验证名额"]
    elif str(item.get("minute15mStatus") or "") in {"failed", "unavailable"}:
        stage = "minute15m_technical_failure"
        reasons = ["15分钟数据请求或依赖失败"]
    elif str(item.get("minute15mStatus") or "") == "quality_rejected":
        stage = "minute15m_quality_rejected"
        reasons = _text_reasons((item.get("v3DataQuality") or {}).get("reasons")) or ["15分钟数据未通过质量校验"]
    elif item.get("dataEligible") is not True:
        stage = "stage_unknown"
        reasons = ["已分配验证，但缺少明确的数据质量终态"]
    elif not estimate.get("rankingEligible"):
        stage = "model_observation_only"
        reasons = ["保守净期望模型样本不足，当前只收集证据"]
    elif not item.get("v3TailRank"):
        stage = "tail_not_allocated"
        reasons = ["未获得最多30只的1分钟尾盘校准名额"]
    elif str(item.get("minute1mStatus") or "") in {"failed", "unavailable"}:
        stage = "minute1m_technical_failure"
        reasons = ["1分钟尾盘数据请求或依赖失败"]
    elif tail.get("tailNegative"):
        stage = "tail_negative_rejected"
        reasons = _text_reasons(tail.get("reasons")) or ["真实尾盘证据走弱"]
    elif item.get("eventRiskHardBlocked"):
        stage = "event_risk_rejected"
        reasons = ["决策截止前高风险事件触发阻断"]
    elif estimate.get("conservativeScorePct") is not None and float(estimate.get("conservativeScorePct") or 0) <= 0:
        stage = "non_positive_expectation"
        reasons = ["保守净期望小于等于0"]
    elif not item.get("formalTradeabilityEligible", True):
        stage = "tradeability_rejected"
        reasons = ["推荐日买入可执行性不合格"]
    elif stored in {"v3_shadow_selected", "shortlist_not_portfolio"}:
        stage = stored
        reasons = ["进入V3影子组合"] if stage == "v3_shadow_selected" else ["进入短名单，但未进入V3影子组合"]
    elif stored == "shortlist_eligible":
        stage = "shortlist_not_portfolio"
        reasons = ["短名单资格成立，但没有形成组合入选记录"]
    else:
        stage = "stage_unknown"
        reasons = ["V3过程字段不足，无法严格还原最终停留层级"]
    result = {
        "terminalStage": stage,
        "terminalStageLabel": TERMINAL_STAGE_LABELS[stage],
        "terminalReason": "；".join(_text_reasons(reasons)),
        "terminalRuleVersion": str(item.get("pipelineVersion") or item.get("v3PipelineVersion") or "2026-11-06.v3"),
        "reviewScope": "v3_full_market",
    }
    result["identityTags"] = candidate_identity_tags({**item, **result})
    return result


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def now_text():
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def transaction(database):
    with closing(database.connect()) as connection:
        with connection:
            yield connection


def init_candidate_schema(database):
    with transaction(database) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS t1_overnight_candidate_archives (
          run_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
          preliminary_rank INTEGER NOT NULL, primary_group TEXT NOT NULL DEFAULT '',
          allocation_round INTEGER NOT NULL DEFAULT 0,
          minute_15m_status TEXT NOT NULL DEFAULT 'not_planned',
          minute_1m_status TEXT NOT NULL DEFAULT 'not_planned',
          quality_eligible INTEGER NOT NULL DEFAULT 0, verified_rank INTEGER,
          ai_batch_index INTEGER, semifinal_selected INTEGER NOT NULL DEFAULT 0,
          official_selected INTEGER NOT NULL DEFAULT 0, rule_shadow_selected INTEGER NOT NULL DEFAULT 0,
          ai_shadow_selected INTEGER NOT NULL DEFAULT 0,
          initial_json TEXT NOT NULL, state_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(run_id, code));
        CREATE INDEX IF NOT EXISTS idx_t1_archive_rank ON t1_overnight_candidate_archives(run_id, preliminary_rank);
        CREATE INDEX IF NOT EXISTS idx_t1_archive_group ON t1_overnight_candidate_archives(run_id, primary_group);
        CREATE TABLE IF NOT EXISTS t1_overnight_candidate_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, code TEXT NOT NULL,
          stage TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_t1_candidate_events ON t1_overnight_candidate_events(run_id,code,id);
        CREATE TABLE IF NOT EXISTS t1_market_daily_bars (
          trade_date TEXT NOT NULL, code TEXT NOT NULL, price_basis TEXT NOT NULL,
          payload_json TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(trade_date,code,price_basis));
        CREATE TABLE IF NOT EXISTS t1_overnight_next_day_tracking_jobs (
          run_id INTEGER PRIMARY KEY, target_trade_date TEXT NOT NULL, status TEXT NOT NULL,
          expected_count INTEGER NOT NULL, resolved_count INTEGER NOT NULL DEFAULT 0,
          attempt_count INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT NOT NULL DEFAULT '',
          lease_expires_at TEXT NOT NULL DEFAULT '', worker_id TEXT NOT NULL DEFAULT '',
          calendar_json TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_t1_tracking_due ON t1_overnight_next_day_tracking_jobs(status,target_trade_date,next_retry_at);
        CREATE TABLE IF NOT EXISTS t1_market_daily_collection_leases (
          trade_date TEXT PRIMARY KEY, worker_id TEXT NOT NULL, lease_expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS t1_overnight_candidate_next_day_metrics (
          run_id INTEGER NOT NULL, code TEXT NOT NULL, target_trade_date TEXT NOT NULL,
          status TEXT NOT NULL, open_proxy REAL, close_proxy REAL,
          payload_json TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(run_id,code,target_trade_date));
        CREATE TABLE IF NOT EXISTS t1_overnight_v3_candidate_facts (
          run_id INTEGER NOT NULL, code TEXT NOT NULL, recommendation_date TEXT NOT NULL,
          hard_filter_status TEXT NOT NULL DEFAULT 'eligible',
          formal_tradeability_eligible INTEGER NOT NULL DEFAULT 0,
          initial_json TEXT NOT NULL, state_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(run_id, code));
        CREATE INDEX IF NOT EXISTS idx_t1_v3_facts_date
          ON t1_overnight_v3_candidate_facts(recommendation_date, run_id);
        CREATE INDEX IF NOT EXISTS idx_t1_v3_facts_stage
          ON t1_overnight_v3_candidate_facts(run_id, hard_filter_status, formal_tradeability_eligible);
        CREATE TABLE IF NOT EXISTS t1_overnight_candidate_review_facts (
          run_id INTEGER NOT NULL, code TEXT NOT NULL, review_schema_version TEXT NOT NULL,
          source_scope TEXT NOT NULL, terminal_stage TEXT NOT NULL,
          leader_business_conclusion TEXT NOT NULL DEFAULT '',
          leader_tags_json TEXT NOT NULL DEFAULT '[]', payload_json TEXT NOT NULL,
          input_hash TEXT NOT NULL, computed_at TEXT NOT NULL,
          PRIMARY KEY(run_id, code, review_schema_version));
        CREATE INDEX IF NOT EXISTS idx_t1_review_facts_stage
          ON t1_overnight_candidate_review_facts(run_id, review_schema_version, terminal_stage);
        """)


class CandidateStore:
    def __init__(self, database):
        self.database = database

    def freeze(self, run_id, candidates, snapshot_version=""):
        stamp = now_text()
        with transaction(self.database) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM t1_overnight_candidate_archives WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
                return
            conn.executemany("""INSERT OR IGNORE INTO t1_overnight_candidate_archives
              (run_id,code,name,preliminary_rank,initial_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
              [(run_id, x["code"], x["name"], x["preliminaryScoreRank"],
                encode(dict(x, archiveVersion=VERSION, snapshotVersion=snapshot_version,
                            frozenScanPrice=x.get("price"))), stamp, stamp) for x in candidates])

    def freeze_v3_facts(self, run_id, recommendation_date, candidates):
        """Persist one lightweight full-market fact per code without duplicating minute evidence."""
        stamp = now_text()
        rows = []
        seen = set()
        for raw in candidates or []:
            code = str(raw.get("code") or "").zfill(6)
            if not code or code in seen:
                continue
            seen.add(code)
            item = dict(raw)
            item["code"] = code
            item.setdefault("frozenScanPrice", item.get("price"))
            rows.append(
                (
                    int(run_id), code, str(recommendation_date or "")[:10],
                    str(item.get("hardFilterStatus") or "eligible"),
                    int(bool(item.get("formalTradeabilityEligible"))),
                    encode(item), stamp, stamp,
                )
            )
        if not rows:
            return 0
        with transaction(self.database) as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO t1_overnight_v3_candidate_facts(
                  run_id,code,recommendation_date,hard_filter_status,
                  formal_tradeability_eligible,initial_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    def update_v3_facts(self, run_id, changes, stage="v3_update"):
        stamp = now_text()
        with transaction(self.database) as conn:
            for change in changes or []:
                code = str(change.get("code") or "").zfill(6)
                row = conn.execute(
                    "SELECT state_json FROM t1_overnight_v3_candidate_facts WHERE run_id=? AND code=?",
                    (int(run_id), code),
                ).fetchone()
                if not row:
                    continue
                state = json.loads(row[0] or "{}")
                derived = {key: value for key, value in change.items() if key != "code"}
                state.update(derived)
                fields = {"state_json": encode(state), "updated_at": stamp}
                if "hardFilterStatus" in change:
                    fields["hard_filter_status"] = str(change.get("hardFilterStatus") or "eligible")
                if "formalTradeabilityEligible" in change:
                    fields["formal_tradeability_eligible"] = int(bool(change.get("formalTradeabilityEligible")))
                conn.execute(
                    "UPDATE t1_overnight_v3_candidate_facts SET "
                    + ",".join(f"{key}=?" for key in fields)
                    + " WHERE run_id=? AND code=?",
                    (*fields.values(), int(run_id), code),
                )
                conn.execute(
                    """INSERT INTO t1_overnight_candidate_events
                    (run_id,code,stage,status,payload_json,created_at) VALUES(?,?,?,?,?,?)""",
                    (int(run_id), code, stage, str(change.get("terminalStage") or stage), encode(derived), stamp),
                )

    def v3_facts(self, run_id, include_rejected=True):
        clauses = ["run_id=?"]
        params = [int(run_id)]
        if not include_rejected:
            clauses.append("hard_filter_status='eligible'")
        with transaction(self.database) as conn:
            rows = conn.execute(
                "SELECT * FROM t1_overnight_v3_candidate_facts WHERE "
                + " AND ".join(clauses)
                + " ORDER BY code",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["initial_json"])
            item.update(json.loads(row["state_json"] or "{}"))
            # The legacy SQLite column is boolean and cannot represent "not recorded".
            # Preserve absence from the frozen JSON so review classification can show
            # tradeability/risk unknown instead of silently treating it as not buyable.
            tradeability_recorded = "formalTradeabilityEligible" in item
            item.update(
                hardFilterStatus=row["hard_filter_status"],
                recommendationDate=row["recommendation_date"],
            )
            if tradeability_recorded:
                item["formalTradeabilityEligible"] = bool(row["formal_tradeability_eligible"])
            result.append(item)
        return result

    def v3_recall_training_samples(self, before_date):
        """Load only prior-date proxy outcomes; current/future dates cannot enter fitting."""
        with transaction(self.database) as conn:
            rows = conn.execute(
                """SELECT f.recommendation_date,f.initial_json,f.state_json,m.payload_json
                FROM t1_overnight_v3_candidate_facts f
                JOIN t1_overnight_candidate_next_day_metrics m
                  ON m.run_id=f.run_id AND m.code=f.code
                WHERE f.recommendation_date<? AND f.hard_filter_status='eligible'
                ORDER BY f.recommendation_date,f.run_id,f.code""",
                (str(before_date or "")[:10],),
            ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["initial_json"])
            item.update(json.loads(row["state_json"] or "{}"))
            metric = json.loads(row["payload_json"] or "{}")
            if not metric.get("aggregateEligible") or metric.get("openProxyReturnPct") is None:
                continue
            result.append(
                {
                    "recommendationDate": row["recommendation_date"],
                    "recallStratum": item.get("recallStratum"),
                    "openProxyReturnPct": metric.get("openProxyReturnPct"),
                    "inclusionProbability": item.get("recallInclusionProbability") or 1.0,
                }
            )
        return result

    def replace_review_facts(self, run_id, facts, version=REVIEW_SCHEMA_VERSION):
        """Replace one rebuildable review version without touching immutable Run facts."""
        stamp = now_text()
        rows = []
        for raw in facts or []:
            item = dict(raw)
            code = str(item.get("code") or "").zfill(6)
            if not code:
                continue
            payload = encode(item)
            rows.append((
                int(run_id), code, str(version), str(item.get("reviewScope") or "unknown"),
                str(item.get("terminalStage") or "stage_unknown"),
                str(item.get("leaderBusinessConclusion") or ""),
                encode(item.get("leaderPerformanceTags") or []), payload,
                hashlib.sha256(payload.encode("utf-8")).hexdigest(), stamp,
            ))
        with transaction(self.database) as conn:
            conn.execute(
                "DELETE FROM t1_overnight_candidate_review_facts WHERE run_id=? AND review_schema_version=?",
                (int(run_id), str(version)),
            )
            if rows:
                conn.executemany(
                    """INSERT INTO t1_overnight_candidate_review_facts(
                      run_id,code,review_schema_version,source_scope,terminal_stage,
                      leader_business_conclusion,leader_tags_json,payload_json,input_hash,computed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    rows,
                )
        return len(rows)

    def review_facts(self, run_id, version=REVIEW_SCHEMA_VERSION):
        with transaction(self.database) as conn:
            rows = conn.execute(
                """SELECT payload_json,input_hash,computed_at
                   FROM t1_overnight_candidate_review_facts
                   WHERE run_id=? AND review_schema_version=? ORDER BY code""",
                (int(run_id), str(version)),
            ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["payload_json"] or "{}")
            item.update(reviewInputHash=row["input_hash"], reviewComputedAt=row["computed_at"])
            result.append(item)
        return result

    def update(self, run_id, changes, stage):
        """Update derived state only; initial_json is immutable even on a resumed Run."""
        columns = {"primaryGroup": "primary_group", "allocationRound": "allocation_round",
            "minute15mStatus": "minute_15m_status", "minute1mStatus": "minute_1m_status",
            "qualityEligible": "quality_eligible", "verifiedRank": "verified_rank",
            "aiBatchIndex": "ai_batch_index", "semifinalSelected": "semifinal_selected",
            "officialSelected": "official_selected", "ruleShadowSelected": "rule_shadow_selected",
            "aiShadowSelected": "ai_shadow_selected"}
        stamp = now_text()
        with transaction(self.database) as conn:
            for change in changes:
                code = change["code"]
                old = conn.execute("SELECT state_json FROM t1_overnight_candidate_archives WHERE run_id=? AND code=?",
                                   (run_id, code)).fetchone()
                if not old:
                    continue
                state = json.loads(old[0])
                # Initial prices, ranks and quote evidence are never rewritten by stage updates.
                frozen_keys = {"rawStock", "price", "frozenScanPrice", "preliminaryScore", "preliminaryScoreRank",
                    "recallRank", "cheapFeatures", "riskFlags", "candidateTypes", "priceBasis", "archiveVersion",
                    "name", "snapshotVersion", "quoteAsOfTime", "quoteTradeDate", "industry", "primaryCandidateType"}
                derived = {k: v for k, v in change.items() if k not in frozen_keys}
                state.update(derived)
                fields = {columns[k]: v for k, v in change.items() if k in columns}
                fields.update(state_json=encode(state), updated_at=stamp)
                conn.execute("UPDATE t1_overnight_candidate_archives SET " + ",".join(f"{k}=?" for k in fields)
                             + " WHERE run_id=? AND code=?", (*fields.values(), run_id, code))
                conn.execute("""INSERT INTO t1_overnight_candidate_events
                    (run_id,code,stage,status,payload_json,created_at) VALUES(?,?,?,?,?,?)""",
                    (run_id, code, stage, change.get("minute15mStatus") or change.get("minute1mStatus") or stage,
                     encode(derived), stamp))

    @staticmethod
    def decode(row):
        item = json.loads(row["initial_json"])
        item.update(json.loads(row["state_json"]))
        item.update(primaryGroup=row["primary_group"], allocationRound=row["allocation_round"],
            minute15mStatus=row["minute_15m_status"], minute1mStatus=row["minute_1m_status"],
            qualityEligible=bool(row["quality_eligible"]), verifiedRank=row["verified_rank"],
            aiBatchIndex=row["ai_batch_index"], semifinalSelected=bool(row["semifinal_selected"]),
            officialSelected=bool(row["official_selected"]), ruleShadowSelected=bool(row["rule_shadow_selected"]),
            aiShadowSelected=bool(row["ai_shadow_selected"]), lastUpdatedAt=row["updated_at"])
        if "metric_json" in row.keys() and row["metric_json"]:
            item["nextDay"] = json.loads(row["metric_json"])
        item.update(legacy_candidate_review(item))
        return item

    def all(self, run_id):
        with transaction(self.database) as conn:
            rows = conn.execute("SELECT * FROM t1_overnight_candidate_archives WHERE run_id=? ORDER BY preliminary_rank", (run_id,)).fetchall()
        return [self.decode(x) for x in rows]

    def summary(self, run_id):
        items = self.all(run_id)
        rounds = []
        for number in (1, 2):
            selected = [x for x in items if x["allocationRound"] == number]
            rounds.append({"round": number, "planned": len(selected),
                "requested": sum(bool(x.get("minute15mRequested")) for x in selected),
                "available": sum(x.get("minute15mAvailable", False) for x in selected),
                "eligible": sum(x["qualityEligible"] for x in selected),
                "setupEligible": sum(bool(x.get("setupEligible")) for x in selected),
                "formalEligible": sum(bool(x.get("formalEligible")) for x in selected),
                "failed": sum(x["minute15mStatus"] == "failed" for x in selected)})
        return {"runId": run_id, "legacy": not bool(items), "legacyNotRecorded": not bool(items),
            "reviewSchemaVersion": REVIEW_SCHEMA_VERSION, "reviewScope": "legacy_500" if items else "unknown",
            "archiveVersion": items[0]["archiveVersion"] if items else "", "total": len(items),
            "selectionPolicyVersion": next((x.get("selectionPolicyVersion") for x in items if x.get("selectionPolicyVersion")), "legacy"),
            "setupStatusCounts": dict(Counter(x.get("setupStatus", "legacy") for x in items)),
            "formalExclusionCounts": dict(Counter(reason for x in items for reason in x.get("formalExclusionReasons") or [])),
            "rounds": rounds, "groups": dict(Counter(x["primaryGroup"] or "not_requested" for x in items)),
            "minute15m": dict(Counter(x["minute15mStatus"] for x in items)),
            "minute1m": dict(Counter(x["minute1mStatus"] for x in items)),
            "terminalStageCounts": dict(Counter(x["terminalStage"] for x in items)),
            "identityTagCounts": dict(Counter(tag for x in items for tag in x.get("identityTags") or [])),
            "lastUpdatedAt": max((x["lastUpdatedAt"] for x in items), default="")}

    def page(self, run_id, page=1, page_size=50, query="", group="", round_number=0,
             stage="", identity="", sort="preliminary_rank"):
        """Page legacy archives after deriving business stages; the archive is capped at 500 rows."""
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        items = self.all(run_id)
        job = self.job(run_id)
        metrics = {}
        if job:
            with transaction(self.database) as conn:
                rows = conn.execute(
                    """SELECT code,payload_json FROM t1_overnight_candidate_next_day_metrics
                       WHERE run_id=? AND target_trade_date=?""",
                    (int(run_id), job["target_trade_date"]),
                ).fetchall()
            metrics = {row["code"]: json.loads(row["payload_json"] or "{}") for row in rows}
        for item in items:
            if item["code"] in metrics:
                item["nextDay"] = metrics[item["code"]]
        text_query = str(query or "")[:80].lower().strip()
        if text_query:
            items = [item for item in items if text_query in str(item.get("code") or "").lower()
                     or text_query in str(item.get("name") or "").lower()]
        if group:
            items = [item for item in items if item.get("primaryGroup") == group]
        if int(round_number or 0) > 0:
            items = [item for item in items if int(item.get("allocationRound") or 0) == int(round_number)]
        if stage:
            items = [item for item in items if item.get("terminalStage") == stage]
        if identity:
            items = [item for item in items if identity in (item.get("identityTags") or [])]
        def sort_key(item):
            if sort == "verified_rank":
                value = item.get("verifiedRank")
                return (value is None, value or 999999, int(item.get("preliminaryScoreRank") or 999999))
            if sort in {"open_proxy", "close_proxy"}:
                field = "openProxyReturnPct" if sort == "open_proxy" else "closeProxyReturnPct"
                value = (item.get("nextDay") or {}).get(field)
                return (value is None, -(float(value) if value is not None else -999999),
                        int(item.get("preliminaryScoreRank") or 999999))
            return (False, int(item.get("preliminaryScoreRank") or 999999), 0)
        items.sort(key=sort_key)
        total = len(items)
        selected = items[(page - 1) * page_size: page * page_size]
        for item in selected:
            for key in ("rawStock", "cheapFeatures", "dataQuality", "series", "recallReasons"):
                item.pop(key, None)
        return {"runId": run_id, "legacy": not bool(self.all(run_id)),
                "reviewSchemaVersion": REVIEW_SCHEMA_VERSION, "reviewScope": "legacy_500",
                "archiveVersion": VERSION, "page": page, "pageSize": page_size, "total": total,
                "lastUpdatedAt": max((x.get("lastUpdatedAt") or "" for x in selected), default=""), "items": selected}

    def detail(self, run_id, code):
        with transaction(self.database) as conn:
            row = conn.execute("""SELECT a.*,m.payload_json AS metric_json FROM t1_overnight_candidate_archives a
                LEFT JOIN t1_overnight_next_day_tracking_jobs j ON j.run_id=a.run_id
                LEFT JOIN t1_overnight_candidate_next_day_metrics m ON m.run_id=a.run_id AND m.code=a.code
                AND m.target_trade_date=j.target_trade_date WHERE a.run_id=? AND a.code=?""", (run_id, code)).fetchone()
            events = conn.execute("SELECT * FROM t1_overnight_candidate_events WHERE run_id=? AND code=? ORDER BY id", (run_id, code)).fetchall()
        if not row:
            raise ValueError("该 Run 未记录此候选档案")
        candidate = self.decode(row)
        review = next((item for item in self.review_facts(run_id, REVIEW_SCHEMA_VERSION)
                       if item.get("code") == candidate.get("code")), None)
        if review:
            candidate.update({key: value for key, value in review.items()
                              if key not in {"name"} or value})
            candidate["reviewSchemaVersion"] = REVIEW_SCHEMA_VERSION
        return {"candidate": candidate, "events": [dict(stage=x["stage"], status=x["status"],
            createdAt=x["created_at"], payload=json.loads(x["payload_json"])) for x in events]}

    def sync_decisions(self, run_id):
        run = self.database.load_t1_overnight_selection_run(run_id)
        if not run:
            return
        market = run.get("market") or {}
        official = {x["code"] for x in run.get("finalPicks") or []}
        rule = {x["code"] for x in (market.get("shadowTop3") or {}).get("picks") or []}
        ai = {x["code"] for x in (market.get("shadowDeepSeekTailTop3") or {}).get("picks") or []}
        self.update(run_id, [dict(code=x["code"], officialSelected=x["code"] in official,
            ruleShadowSelected=x["code"] in rule, aiShadowSelected=x["code"] in ai) for x in self.all(run_id)], "decision")

    def bars(self, trade_date):
        with transaction(self.database) as conn:
            rows = conn.execute("SELECT code,payload_json FROM t1_market_daily_bars WHERE trade_date=? AND price_basis='unadjusted'", (trade_date,)).fetchall()
        return {x["code"]: json.loads(x["payload_json"]) for x in rows}

    def save_bars(self, trade_date, bars):
        from backend.app.providers.t1_daily_observation_provider import valid_bar
        # A successful observation is never silently overwritten by a retry.
        with transaction(self.database) as conn:
            conn.executemany("INSERT OR IGNORE INTO t1_market_daily_bars VALUES(?,?,?,?,?)",
                [(trade_date, code, "unadjusted", encode(bar), now_text()) for code, bar in bars.items()
                 if valid_bar(bar, trade_date) and bar.get("code", code) == code])

    def job(self, run_id):
        with transaction(self.database) as conn:
            row = conn.execute("SELECT * FROM t1_overnight_next_day_tracking_jobs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def save_metrics(self, run_id, trade_date, metrics, worker_id):
        stamp = now_text()
        with transaction(self.database) as conn:
            owner = conn.execute("SELECT worker_id FROM t1_overnight_next_day_tracking_jobs WHERE run_id=?", (run_id,)).fetchone()
            if not owner or owner[0] != worker_id:
                return False
            conn.executemany("""INSERT INTO t1_overnight_candidate_next_day_metrics VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,code,target_trade_date) DO UPDATE SET status=excluded.status,
                open_proxy=excluded.open_proxy,close_proxy=excluded.close_proxy,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                [(run_id, x["code"], trade_date, x["status"], x.get("openProxyReturnPct"),
                    x.get("closeProxyReturnPct"), encode(x), stamp) for x in metrics])
        return True
