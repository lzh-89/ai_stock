import tempfile
import unittest
from pathlib import Path

from backend.app.database import Database
from backend.app.engines import t1_v3_pipeline
from backend.app.services.t1_v3_replay_service import T1V3ReplayService
from backend.app.t1_candidate_store import CandidateStore


class T1V3ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database = Database(Path(self.tempdir.name) / "app.sqlite")
        self.service = T1V3ReplayService(self.database)
        self.store = CandidateStore(self.database)

    def _run(self, day, market=None, pool=None):
        return self.database.create_t1_overnight_selection_run(
            {
                "recommendationDate": day,
                "status": "done",
                "selectionMode": "ai_t1_overnight",
                "request": {"runRole": "official"},
                "market": market or {"runRole": "official"},
            },
            pool or [],
        )

    def test_replay_grades_evidence_and_never_changes_historical_run(self):
        run_a = self._run(
            "2026-10-06",
            {
                "runRole": "official",
                "v3Strategy": {
                    "pipelineVersion": t1_v3_pipeline.PIPELINE_VERSION,
                    "decisionStatus": "selected",
                },
                "baselineGroups": {"v3Main": [{"code": "000001"}]},
                "baselineComparison": {
                    "v3Main": {"avgNetReturnPct": 1.2},
                    "final": {"avgNetReturnPct": 0.4},
                },
            },
        )
        self.store.freeze_v3_facts(
            run_a,
            "2026-10-06",
            [{"code": "000001", "name": "甲", "hardFilterStatus": "eligible"}],
        )

        run_b = self._run("2026-10-07", pool=[{"code": "000002", "name": "乙"}])
        self.store.freeze(run_b, [{"code": "000002", "name": "乙", "preliminaryScoreRank": 1}])
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO t1_overnight_minute_snapshots(
                  run_id,code,period,source,point_count,quality_status,error,series_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (run_b, "000002", "15m", "fixture", 1, "available", "", "[]", "2026-10-07T14:40:00"),
            )

        run_c = self._run("2026-10-08")
        self.database.upsert_t1_overnight_selection_outcomes(
            run_c,
            [{"code": "000003", "recommendationDate": "2026-10-08", "outcomeLabel": "弱成功"}],
        )
        before = self.database.load_t1_overnight_selection_run(run_a)

        report = self.service.run("2026-10-01", "2026-10-31")

        self.assertEqual(report["gradeCounts"], {"A": 1, "B": 1, "C": 1})
        self.assertEqual(report["exactOutcomeCount"], 1)
        self.assertAlmostEqual(report["metrics"]["exactAveragePairedDeltaPct"], 0.8)
        self.assertFalse(report["admissionEligible"])
        self.assertFalse(report["modifiesHistoricalRuns"])
        self.assertTrue(report["historicalRowsUnchanged"])
        self.assertEqual(before, self.database.load_t1_overnight_selection_run(run_a))
        self.assertEqual(self.service.latest()["id"], report["id"])

    def test_invalid_period_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.run("2026-11-01", "2026-10-01")

    def test_new_role_membership_does_not_duplicate_full_candidate_json(self):
        run_id = self._run("2026-10-09")
        candidate = {
            "code": "000009",
            "name": "存储样本",
            "aiRank": 1,
            "reason": "完整入选理由",
            "buyPlan": "完整买入计划",
            "largeEvidence": "证据" * 500,
        }
        self.database.upsert_t1_overnight_selection_items(run_id, [candidate], stage="final")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT item_json,pick_json FROM t1_overnight_selection_items WHERE run_id=? AND code=?",
                (run_id, "000009"),
            ).fetchone()
        self.assertIn("largeEvidence", row["item_json"])
        self.assertNotIn("largeEvidence", row["pick_json"])
        self.assertIn("t1_role_reference_v1", row["pick_json"])
        loaded = self.database.load_t1_overnight_selection_items(run_id)[0]
        self.assertEqual(loaded["pick"]["reason"], "完整入选理由")
        self.assertEqual(loaded["pick"]["largeEvidence"], candidate["largeEvidence"])


if __name__ == "__main__":
    unittest.main()
