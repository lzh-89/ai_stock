import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from backend.app.database import Database


class DatabaseReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "reliability.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_connections_use_wal_busy_timeout_and_normal_synchronous(self):
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 1)
            self.assertGreaterEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 1000)

    def test_stale_runs_are_closed_and_evidence_error_is_preserved(self):
        evidence_id = self.database.create_t1_overnight_evidence_run(
            {
                "recommendationDate": "2026-10-07",
                "scanTime": "14:40",
                "status": "running",
                "error": "provider timeout",
                "leaseExpiresAt": "2026-10-07T14:00:00",
            }
        )
        selection_id = self.database.create_t1_overnight_selection_run(
            {
                "recommendationDate": "2026-10-07",
                "scanTime": "14:40",
                "status": "running",
                "leaseExpiresAt": "2026-10-07T14:00:00",
            },
            [],
        )

        recovered = self.database.recover_stale_t1_runs(datetime.fromisoformat("2026-10-07T14:01:00"))

        self.assertEqual(recovered["evidence"], [{"id": evidence_id, "status": "failed_timeout"}])
        self.assertEqual(recovered["selection"], [{"id": selection_id, "status": "failed_timeout"}])
        evidence = self.database.load_t1_overnight_evidence_run(evidence_id)
        selection = self.database.load_t1_overnight_selection_run(selection_id)
        self.assertEqual(evidence["status"], "failed_timeout")
        self.assertIn("provider timeout", evidence["error"])
        self.assertIn("watchdog", evidence["error"])
        self.assertEqual(selection["status"], "failed_timeout")
        self.assertEqual(selection["currentStage"], "watchdog_timeout")

    def test_updates_refresh_heartbeat_and_lease(self):
        run_id = self.database.create_t1_overnight_selection_run(
            {"recommendationDate": "2026-10-07", "status": "pending"}, []
        )
        before = self.database.load_t1_overnight_selection_run(run_id)
        self.database.update_t1_overnight_selection_run(run_id, {"currentStage": "loading_market"})
        after = self.database.load_t1_overnight_selection_run(run_id)

        self.assertGreaterEqual(after["heartbeatAt"], before["heartbeatAt"])
        self.assertGreaterEqual(
            datetime.fromisoformat(after["leaseExpiresAt"]),
            datetime.fromisoformat(after["heartbeatAt"]) + timedelta(seconds=890),
        )
        self.assertEqual(after["currentStage"], "loading_market")

    def test_stale_evidence_with_persisted_rows_becomes_partial(self):
        evidence_id = self.database.create_t1_overnight_evidence_run(
            {
                "recommendationDate": "2026-10-07",
                "scanTime": "14:40",
                "status": "running",
                "leaseExpiresAt": "2026-10-07T14:00:00",
            }
        )
        self.database.upsert_t1_overnight_market_snapshots(
            evidence_id,
            [{"snapshotTime": "14:40", "source": "test", "market": {"count": 1}}],
        )

        recovered = self.database.recover_stale_t1_runs(datetime.fromisoformat("2026-10-07T14:01:00"))

        self.assertEqual(recovered["evidence"], [{"id": evidence_id, "status": "partial"}])
        self.assertEqual(self.database.load_t1_overnight_evidence_run(evidence_id)["status"], "partial")


if __name__ == "__main__":
    unittest.main()
