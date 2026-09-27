import unittest
from datetime import datetime

from backend.app.services import daily_kline_cache_service as svc


class DailyKlineCacheSchedulerTest(unittest.TestCase):
    def test_due_slots_returns_recent_unprocessed_slot(self):
        state = svc._normalize_state({})
        due = svc._due_slots(datetime(2026, 9, 16, 16, 5), state["config"], state)
        self.assertEqual(due, ["16:00"])

    def test_due_slots_allows_catchup_within_one_hour(self):
        state = svc._normalize_state({})
        due = svc._due_slots(datetime(2026, 9, 16, 12, 30), state["config"], state)
        self.assertEqual(due, ["12:00"])

    def test_due_slots_excludes_recorded_slot(self):
        state = svc._normalize_state({"days": {"2026-09-16": {"12:00": {"status": "completed"}}}})
        due = svc._due_slots(datetime(2026, 9, 16, 16, 5), state["config"], state)
        self.assertEqual(due, ["16:00"])

    def test_config_normalizes_invalid_values(self):
        config = svc._normalize_config(
            {
                "scheduleTimes": ["16:00", "bad", "16:00"],
                "pollIntervalSeconds": 1,
                "maxCatchupMinutes": 999,
                "days": 5,
                "recentRuns": 0,
            }
        )
        self.assertEqual(config["scheduleTimes"], ["16:00"])
        self.assertEqual(config["pollIntervalSeconds"], 30)
        self.assertEqual(config["maxCatchupMinutes"], 360)
        self.assertEqual(config["days"], 40)
        self.assertEqual(config["recentRuns"], 1)


if __name__ == "__main__":
    unittest.main()
