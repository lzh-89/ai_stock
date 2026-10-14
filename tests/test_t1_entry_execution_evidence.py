import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from backend.app.database import Database
from backend.app.services.t1_entry_execution_evidence_service import (
    ENTRY_EXECUTION_EVIDENCE_VERSION,
    T1EntryExecutionEvidenceService,
)


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FakeMinuteProvider:
    def __init__(self):
        self.source = "fake_minute"
        self.last_sources_by_code = {}
        self.rows = {}
        self.calls = 0

    def fetch_many(self, codes, period="1m", require_dated=False):
        self.calls += 1
        self.last_sources_by_code = {code: self.source for code in codes}
        return {code: list(self.rows.get(code) or []) for code in codes}


class FakeAnalysis:
    def __init__(self, provider):
        self.kline_provider = provider


class T1EntryExecutionEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "app.sqlite")
        self.clock = Clock(datetime.fromisoformat("2026-10-14T14:41:10"))
        self.provider = FakeMinuteProvider()
        self.service = T1EntryExecutionEvidenceService(
            FakeAnalysis(self.provider), self.database, clock=self.clock
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self, role="official", force=False):
        market = {
            "runRole": role,
            "decisionTiming": {
                "signalPublishedAt": "2026-10-14T14:41:00",
                "signalTime": "14:41",
            },
        }
        run_id = self.database.create_t1_overnight_selection_run(
            {
                "recommendationDate": "2026-10-14",
                "scanTime": "14:40",
                "requestedSource": "auto",
                "status": "done",
                "selectionMode": "ai_t1_overnight",
                "finalCount": 1,
                "finalPicks": [{"code": "000001", "name": "测试股"}],
                "request": {"runRole": role, "force": force, "poolSize": 500},
                "market": market,
            },
            [],
        )
        self.database.update_t1_overnight_selection_run(
            run_id,
            {
                "status": "done",
                "selectionMode": "ai_t1_overnight",
                "finalCount": 1,
                "finalPicks": [{"code": "000001", "name": "测试股"}],
                "market": market,
            },
        )
        return run_id

    def test_freezes_first_post_publish_minute_and_ignores_pre_signal_row(self):
        run_id = self._run()
        jobs = self.service.register_today_jobs(self.clock())
        self.assertEqual([item["runId"] for item in jobs], [run_id])
        self.provider.rows["000001"] = [
            {"time": "2026-10-14 14:40", "open": 9.9, "close": 10.0, "volume": 1000},
            {"time": "2026-10-14 14:42", "open": 10.1, "close": 10.2, "volume": 1200},
        ]
        self.clock.value = datetime.fromisoformat("2026-10-14T14:43:10")
        result = self.service.run_due(now=self.clock())
        self.assertEqual(result["processed"][0]["status"], "done")
        snapshot = self.database.load_t1_entry_execution_snapshots(run_id)["000001"]
        self.assertTrue(snapshot["decisionEligible"])
        self.assertEqual(snapshot["entryTime"], "14:42")
        self.assertEqual(snapshot["price"], 10.1)
        self.assertEqual(len(snapshot["series"]), 1)

    def test_missing_minute_retries_then_succeeds_before_deadline(self):
        run_id = self._run()
        self.service.register_today_jobs(self.clock())
        self.clock.value = datetime.fromisoformat("2026-10-14T14:43:10")
        first = self.service.run_due(now=self.clock())
        self.assertEqual(first["processed"][0]["status"], "retry_wait")
        self.assertEqual(self.database.load_t1_entry_execution_snapshots(run_id), {})

        self.provider.rows["000001"] = [
            {"time": "2026-10-14 14:44", "open": 10.3, "close": 10.4, "volume": 800}
        ]
        self.clock.value = datetime.fromisoformat("2026-10-14T14:44:00")
        second = self.service.run_due(now=self.clock())
        self.assertEqual(second["processed"][0]["status"], "done")
        self.assertTrue(self.database.load_t1_entry_execution_snapshots(run_id)["000001"]["decisionEligible"])

    def test_deadline_persists_auditable_unavailable_result(self):
        run_id = self._run()
        self.service.register_today_jobs(self.clock())
        self.clock.value = datetime.fromisoformat("2026-10-14T14:56:31")
        result = self.service.run_due(now=self.clock())
        self.assertEqual(result["processed"][0]["status"], "partial")
        snapshot = self.database.load_t1_entry_execution_snapshots(run_id)["000001"]
        self.assertFalse(snapshot["decisionEligible"])
        self.assertEqual(snapshot["status"], "unavailable")

    def test_research_and_forced_runs_do_not_register(self):
        self._run(role="research", force=True)
        self.assertEqual(self.service.register_today_jobs(self.clock()), [])

    def test_snapshot_is_append_only(self):
        run_id = self._run()
        self.service.register_today_jobs(self.clock())
        self.provider.rows["000001"] = [
            {"time": "2026-10-14 14:42", "open": 10.1, "close": 10.2, "volume": 1200}
        ]
        self.clock.value = datetime.fromisoformat("2026-10-14T14:43:10")
        self.service.run_due(now=self.clock())
        snapshot = self.database.load_t1_entry_execution_snapshots(run_id)["000001"]
        self.assertEqual(snapshot["evidenceVersion"], ENTRY_EXECUTION_EVIDENCE_VERSION)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE t1_overnight_entry_execution_snapshots SET status='unavailable' WHERE id=?",
                    (snapshot["snapshotId"],),
                )


if __name__ == "__main__":
    unittest.main()
