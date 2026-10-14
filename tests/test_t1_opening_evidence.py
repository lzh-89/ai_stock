import tempfile
import unittest
import sqlite3
from datetime import datetime
from pathlib import Path

from backend.app.database import Database
from backend.app.schema import RawStock
from backend.app.services.t1_opening_evidence_service import (
    OPENING_PRECHECK,
    OPENING_SNAPSHOT,
    T1OpeningEvidenceService,
)


def stock(code, change, quote_time):
    return RawStock(
        code=code,
        name=code,
        sector="主板",
        theme="test",
        price=10,
        change=change,
        turnover=1,
        volume_ratio=1,
        amount_factor=1,
        sector_rank=1,
        sector_limit_ups=0,
        rel_strength=50,
        news_score=50,
        amount=1_000_000,
        open=10,
        trade_date=quote_time[:10],
        quote_time=quote_time,
    )


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FakeMarketStore:
    def __init__(self, clock, complete_indices=True):
        self.clock = clock
        self.complete_indices = complete_indices
        self.refresh_count = 0
        self.tencent_provider = type("Tencent", (), {"fetch_indices": lambda self: []})()

    def latest_cached(self):
        return {"count": 3, "coverage": {"expectedRows": 3}}

    def refresh(self, source="tencent"):
        self.refresh_count += 1
        target = self.clock().date().isoformat()
        quote_time = f"{target}T09:31:10"
        codes = ["sh000001", "sz399001", "sz399006", "sh000852"]
        if not self.complete_indices:
            codes = codes[:3]
        return {
            "source": source,
            "coverage": {"expectedRows": 3},
            "stocks": [
                stock("000001", -1.0, quote_time),
                stock("000002", -2.0, quote_time),
                stock("000003", 1.0, quote_time),
            ],
            "market": {
                "indices": [
                    {
                        "code": code,
                        "changePct": -0.2 - index * 0.2,
                        "tradeDate": target,
                        "quoteTime": quote_time,
                    }
                    for index, code in enumerate(codes)
                ]
            },
        }


class FakeAnalysis:
    def __init__(self, store):
        self.full_market_context_store = store


class FakeSectorProvider:
    def fetch_constituents(self, sector_name, sector_type, limit=12):
        return [{"code": "000001"}, {"code": "000002"}]


class T1OpeningEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "app.sqlite")
        self.clock = Clock(datetime.fromisoformat("2026-10-13T09:29:10"))
        self.store = FakeMarketStore(self.clock)
        self.service = T1OpeningEvidenceService(
            FakeAnalysis(self.store), self.database, sector_provider=FakeSectorProvider(), clock=self.clock
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self, force=False, role="official"):
        run_id = self.database.create_t1_overnight_selection_run(
            {
                "recommendationDate": "2026-10-12",
                "scanTime": "14:40",
                "requestedSource": "auto",
                "status": "done",
                "selectionMode": "ai_t1_overnight",
                "poolSize": 500,
                "finalCount": 1,
                "finalPicks": [{"code": "000001", "industrySector": "银行"}],
                "request": {"runRole": role, "poolSize": 500, "force": force},
                "market": {
                    "runRole": role,
                    "strategyPolicy": {"strategyPolicyVersion": "2026-10-13收益优化.shadow.v5"},
                },
            },
            [],
        )
        self.database.update_t1_overnight_selection_run(
            run_id,
            {
                "status": "done",
                "selectionMode": "ai_t1_overnight",
                "finalCount": 1,
                "finalPicks": [{"code": "000001", "industrySector": "银行"}],
                "market": {
                    "runRole": role,
                    "strategyPolicy": {"strategyPolicyVersion": "2026-10-13收益优化.shadow.v5"},
                },
            },
        )
        return run_id

    def test_only_latest_canonical_official_run_is_registered(self):
        first = self._run()
        latest = self._run()
        self._run(force=True, role="research")
        registered = self.service.register_upcoming_jobs(self.clock())
        self.assertEqual({item["runId"] for item in registered}, {latest})
        self.assertNotIn(first, {item["runId"] for item in registered})
        self.assertEqual({item["evidenceType"] for item in registered}, {OPENING_PRECHECK, OPENING_SNAPSHOT})

    def test_precheck_and_snapshot_are_idempotent_and_decision_eligible(self):
        run_id = self._run()
        self.clock.value = datetime.fromisoformat("2026-10-12T15:00:00")
        self.service.register_upcoming_jobs(self.clock())
        self.clock.value = datetime.fromisoformat("2026-10-13T09:29:10")
        first = self.service.run_due(now=self.clock())
        self.assertEqual(first["processed"][0]["status"], "done")
        precheck = self.database.load_t1_opening_evidence(run_id, OPENING_PRECHECK)
        self.assertEqual(precheck["sectorConstituentsByIndustry"]["银行"]["expectedCount"], 2)

        self.clock.value = datetime.fromisoformat("2026-10-13T09:31:20")
        result = self.service.run_due(now=self.clock())
        self.assertEqual(result["processed"][0]["status"], "done")
        snapshot = self.database.load_t1_opening_evidence(run_id, OPENING_SNAPSHOT)
        self.assertTrue(snapshot["decisionEligible"])
        self.assertEqual(snapshot["marketCoveragePct"], 100.0)
        self.assertEqual(snapshot["mainIndexStatus"], "available")
        self.assertEqual(snapshot["sectorReturnsByIndustry"]["银行"]["coveragePct"], 100.0)
        self.assertEqual(self.store.refresh_count, 1)

        repeated = self.service.run_due(now=self.clock())
        self.assertEqual(repeated["processed"], [])
        self.assertEqual(self.store.refresh_count, 1)

    def test_incomplete_indices_do_not_disable_independent_market_condition(self):
        self._run()
        self.clock.value = datetime.fromisoformat("2026-10-12T15:00:00")
        self.service.register_upcoming_jobs(self.clock())
        self.clock.value = datetime.fromisoformat("2026-10-13T09:29:10")
        self.service.run_due(now=self.clock())
        self.store.complete_indices = False
        self.clock.value = datetime.fromisoformat("2026-10-13T09:31:20")
        self.service.run_due(now=self.clock())
        snapshot = self.database.load_t1_opening_evidence(1, OPENING_SNAPSHOT)
        self.assertTrue(snapshot["decisionEligible"])
        self.assertEqual(snapshot["mainIndexStatus"], "unavailable")
        self.assertEqual(snapshot["marketBreadthStatus"], "available")

    def test_missed_deadline_never_calls_provider_or_backfills_decision(self):
        run_id = self._run()
        self.clock.value = datetime.fromisoformat("2026-10-13T09:32:00")
        result = self.service.run_due(now=self.clock())
        self.assertEqual({item["status"] for item in result["processed"]}, {"missed_deadline"})
        snapshot = self.database.load_t1_opening_evidence(run_id, OPENING_SNAPSHOT)
        self.assertFalse(snapshot["decisionEligible"])
        self.assertTrue(snapshot["lateBackfill"])
        self.assertEqual(snapshot["status"], "missed_deadline")
        self.assertEqual(self.store.refresh_count, 0)

    def test_published_snapshot_is_database_append_only(self):
        run_id = self._run()
        self.clock.value = datetime.fromisoformat("2026-10-13T09:32:00")
        self.service.run_due(now=self.clock())
        snapshot = self.database.load_t1_opening_evidence(run_id, OPENING_SNAPSHOT)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE t1_overnight_opening_evidence_snapshots SET status = 'available' WHERE id = ?",
                    (snapshot["snapshotId"],),
                )


if __name__ == "__main__":
    unittest.main()
