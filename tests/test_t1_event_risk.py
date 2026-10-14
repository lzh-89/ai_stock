import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from backend.app.database import Database
from backend.app.providers.t1_event_risk_provider import T1EventRiskProvider, classify_event


class T1EventRiskProviderTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "app.sqlite")

    def tearDown(self):
        self.tempdir.cleanup()

    def _provider(self, rows):
        calls = []

        def fetch_json(url, timeout):
            calls.append((url, timeout))
            return {"data": {"list": rows}}

        provider = T1EventRiskProvider(
            self.database,
            fetch_json=fetch_json,
            clock=lambda: datetime(2026, 10, 13, 14, 40),
        )
        return provider, calls

    def test_exact_published_high_risk_event_triggers_before_cutoff(self):
        provider, _ = self._provider(
            [
                {
                    "art_code": "AN001",
                    "title": "关于股东拟减持股份的预披露公告",
                    "display_time": "2026-10-13 13:20:00",
                    "codes": [{"stock_code": "600000"}],
                }
            ]
        )
        result = provider.evaluate(
            [{"code": "600000"}],
            run_id=1,
            recommendation_date="2026-10-13",
            signal_published_at="2026-10-13T14:40:00",
        )
        evidence = result["assessments"]["600000"]
        self.assertEqual(evidence["eventRiskStatus"], "risk")
        self.assertTrue(evidence["triggered"])
        self.assertEqual(evidence["hitCategories"], ["reduction"])

    def test_date_only_risk_event_is_partial_not_clear(self):
        provider, _ = self._provider(
            [
                {
                    "art_code": "AN002",
                    "title": "关于收到监管问询函的公告",
                    "notice_date": "2026-10-13 00:00:00",
                    "codes": [{"stock_code": "000001"}],
                }
            ]
        )
        result = provider.evaluate(
            [{"code": "000001"}],
            run_id=2,
            recommendation_date="2026-10-13",
            signal_published_at="2026-10-13T14:40:00",
        )
        evidence = result["assessments"]["000001"]
        self.assertEqual(evidence["eventRiskStatus"], "partial")
        self.assertFalse(result["timestampAuditPassed"])

    def test_event_after_cutoff_does_not_trigger(self):
        provider, _ = self._provider(
            [
                {
                    "art_code": "AN003",
                    "title": "关于收到立案告知书的公告",
                    "display_time": "2026-10-13 15:10:00",
                    "codes": [{"stock_code": "300001"}],
                }
            ]
        )
        result = provider.evaluate(
            [{"code": "300001"}],
            run_id=3,
            recommendation_date="2026-10-13",
            signal_published_at="2026-10-13T14:40:00",
        )
        evidence = result["assessments"]["300001"]
        self.assertEqual(evidence["eventRiskStatus"], "clear")
        self.assertFalse(evidence["triggered"])

    def test_successful_snapshot_is_reused_within_ttl(self):
        provider, calls = self._provider([])
        kwargs = {
            "recommendation_date": "2026-10-13",
            "signal_published_at": "2026-10-13T14:40:00",
        }
        provider.evaluate([{"code": "600000"}], run_id=4, **kwargs)
        second = provider.evaluate([{"code": "600000"}], run_id=5, **kwargs)
        self.assertEqual(len(calls), 1)
        self.assertGreater(second["reusedFromSnapshotId"], 0)

    def test_classifier_keeps_non_risk_notice_informational(self):
        result = classify_event("关于召开2026年第一次临时股东大会的通知")
        self.assertEqual(result["category"], "other")
        self.assertEqual(result["severity"], "info")


if __name__ == "__main__":
    unittest.main()
