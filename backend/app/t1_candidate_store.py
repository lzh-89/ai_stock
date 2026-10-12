"""Persistent frozen candidate archives and shared unadjusted daily observations."""
from __future__ import annotations

import json
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime

from backend.app.engines.t1_candidate_allocation import VERSION


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
                "failed": sum(x["minute15mStatus"] == "failed" for x in selected)})
        return {"runId": run_id, "legacy": not bool(items), "legacyNotRecorded": not bool(items),
            "archiveVersion": items[0]["archiveVersion"] if items else "", "total": len(items),
            "rounds": rounds, "groups": dict(Counter(x["primaryGroup"] or "not_requested" for x in items)),
            "minute15m": dict(Counter(x["minute15mStatus"] for x in items)),
            "minute1m": dict(Counter(x["minute1mStatus"] for x in items)),
            "lastUpdatedAt": max((x["lastUpdatedAt"] for x in items), default="")}

    def page(self, run_id, page=1, page_size=50, query="", group="", stage="", sort="preliminary_rank"):
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        where, params = ["a.run_id=?"], [run_id]
        if query:
            where.append("(a.code LIKE ? OR a.name LIKE ?)")
            params.extend([f"%{query[:80]}%"] * 2)
        if group:
            where.append("a.primary_group=?")
            params.append(group)
        stages = {"not_requested": "a.allocation_round=0", "request_failed": "a.minute_15m_status='failed'",
            "quality_rejected": "a.minute_15m_status='quality_rejected'", "semifinal": "a.semifinal_selected=1",
            "official": "a.official_selected=1", "shadow": "(a.rule_shadow_selected=1 OR a.ai_shadow_selected=1)"}
        if stage in stages:
            where.append(stages[stage])
        order = {"preliminary_rank": "a.preliminary_rank", "verified_rank": "a.verified_rank IS NULL,a.verified_rank",
            "open_proxy": "m.open_proxy IS NULL,m.open_proxy DESC,a.preliminary_rank",
            "close_proxy": "m.close_proxy IS NULL,m.close_proxy DESC,a.preliminary_rank"}.get(sort, "a.preliminary_rank")
        condition = " AND ".join(where)
        with transaction(self.database) as conn:
            recorded = conn.execute("SELECT count(*) FROM t1_overnight_candidate_archives WHERE run_id=?", (run_id,)).fetchone()[0]
            total = conn.execute("SELECT count(*) FROM t1_overnight_candidate_archives a WHERE " + condition, params).fetchone()[0]
            rows = conn.execute("""SELECT a.*, m.payload_json AS metric_json FROM t1_overnight_candidate_archives a
                LEFT JOIN t1_overnight_next_day_tracking_jobs j ON j.run_id=a.run_id
                LEFT JOIN t1_overnight_candidate_next_day_metrics m ON m.run_id=a.run_id AND m.code=a.code
                AND m.target_trade_date=j.target_trade_date WHERE """ + condition + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size)).fetchall()
        items = [self.decode(x) for x in rows]
        for item in items:
            for key in ("rawStock", "cheapFeatures", "dataQuality", "series", "recallReasons"):
                item.pop(key, None)
        return {"runId": run_id, "legacy": not bool(recorded),
                "archiveVersion": VERSION, "page": page, "pageSize": page_size, "total": total,
                "lastUpdatedAt": max((x["updated_at"] for x in rows), default=""), "items": items}

    def detail(self, run_id, code):
        with transaction(self.database) as conn:
            row = conn.execute("""SELECT a.*,m.payload_json AS metric_json FROM t1_overnight_candidate_archives a
                LEFT JOIN t1_overnight_next_day_tracking_jobs j ON j.run_id=a.run_id
                LEFT JOIN t1_overnight_candidate_next_day_metrics m ON m.run_id=a.run_id AND m.code=a.code
                AND m.target_trade_date=j.target_trade_date WHERE a.run_id=? AND a.code=?""", (run_id, code)).fetchone()
            events = conn.execute("SELECT * FROM t1_overnight_candidate_events WHERE run_id=? AND code=? ORDER BY id", (run_id, code)).fetchall()
        if not row:
            raise ValueError("该 Run 未记录此候选档案")
        return {"candidate": self.decode(row), "events": [dict(stage=x["stage"], status=x["status"],
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
