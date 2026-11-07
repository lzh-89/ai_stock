import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from backend.app.database import Database
from backend.app.engines import t1_v3_pipeline as v3_pipeline
from backend.app.services.t1_strategy_deployment_service import (
    StrategyDeploymentConflictError,
    T1StrategyDeploymentService,
)


class T1StrategyDeploymentTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database = Database(Path(self.tempdir.name) / "app.sqlite")
        self.service = T1StrategyDeploymentService(self.database, "legacy-official-v1")

    def _next_weekday(self):
        value = date.today() + timedelta(days=1)
        while value.weekday() >= 5:
            value += timedelta(days=1)
        return value.isoformat()

    def _eligible_report(self):
        return self.database.append_t1_shadow_evaluation_report(
            {
                "shadowFamily": "v3_main_experiment",
                "shadowRole": "v3_main",
                "scope": "main_strategy",
                "strategyPolicyVersion": v3_pipeline.PIPELINE_VERSION,
                "outcomeScenario": "open_15m",
                "executionProfileVersion": "execution-v1",
                "windowType": "120d",
                "windowStartDate": "2026-01-01",
                "windowEndDate": "2026-06-30",
                "effectiveDayCount": 120,
                "hitDayCount": 100,
                "outcomeCount": 180,
                "coveragePct": 99.0,
                "metrics": {"avgDailyNetReturnPct": 0.3, "avgPairedDeltaPct": 0.2, "maxDrawdownPct": -5.0},
                "admission": {"manualPromotionReviewAllowed": True, "blockers": []},
                "reviewStatus": "promotion_reviewable",
                "sourceWatermark": "fixture-watermark",
            }
        )

    def test_initial_state_is_shadow_and_blocked_without_report(self):
        status = self.service.status()
        self.assertEqual(status["lifecycleState"], "shadow_collecting")
        self.assertEqual(status["currentOfficialVersion"], "legacy-official-v1")
        self.assertFalse(status["promotionReviewAllowed"])
        self.assertTrue(status["promotionBlockers"])

    def test_approve_activates_only_for_effective_future_run(self):
        report = self._eligible_report()
        status = self.service.status()
        approved = self.service.approve(
            {
                "deploymentRevision": status["deploymentRevision"],
                "evaluationReportId": report["id"],
                "evaluationReportHash": status["latestEvaluationReport"]["reportHash"],
                "effectiveFromTradeDate": self._next_weekday(),
                "confirmationText": "确认启用V3",
                "reason": "冻结报告通过，批准未来 Run 生效",
                "confirmations": {
                    "readFrozenReport": True,
                    "confirmFixedVersions": True,
                    "futureRunsOnly": True,
                    "understandRollback": True,
                },
            }
        )
        self.assertEqual(approved["lifecycleState"], "scheduled")
        self.assertEqual(approved["currentOfficialVersion"], "legacy-official-v1")
        frozen = self.service.freeze_for_run(self._next_weekday())
        self.assertEqual(frozen["officialVersionForThisRun"], v3_pipeline.PIPELINE_VERSION)
        active = self.service.activate_for_run(88, self._next_weekday())
        self.assertEqual(active["lifecycleState"], "official")
        self.assertEqual(active["firstEffectiveRunId"], 88)
        self.assertEqual(active["currentOfficialVersion"], v3_pipeline.PIPELINE_VERSION)

    def test_stale_revision_cannot_overwrite_review(self):
        initial = self.service.status()
        self.service.review({"decision": "continue_observing", "reason": "继续积累确认窗口"})
        with self.assertRaises(StrategyDeploymentConflictError):
            self.service.cancel({"deploymentRevision": initial["deploymentRevision"], "reason": "stale"})

    def test_report_hash_is_rechecked(self):
        report = self._eligible_report()
        status = self.service.status()
        with self.assertRaises(StrategyDeploymentConflictError):
            self.service.approve(
                {
                    "deploymentRevision": status["deploymentRevision"],
                    "evaluationReportId": report["id"],
                    "evaluationReportHash": "wrong",
                    "effectiveFromTradeDate": self._next_weekday(),
                    "confirmationText": "确认启用V3",
                    "reason": "test",
                    "confirmations": {
                        "readFrozenReport": True,
                        "confirmFixedVersions": True,
                        "futureRunsOnly": True,
                        "understandRollback": True,
                    },
                }
            )

    def test_exchange_calendar_rejects_a_weekday_market_holiday(self):
        first = date.fromisoformat(self._next_weekday())
        second = first + timedelta(days=1)
        while second.weekday() >= 5:
            second += timedelta(days=1)
        self.database.set_app_setting(
            "t1_tracking_exchange_calendar",
            {
                "start": first.isoformat(),
                "end": second.isoformat(),
                "openDates": [second.isoformat()],
                "source": "test_exchange_calendar",
            },
        )
        report = self._eligible_report()
        status = self.service.status()
        self.assertEqual(status["nextAvailableTradeDate"], second.isoformat())
        with self.assertRaises(ValueError):
            self.service.approve(
                {
                    "deploymentRevision": status["deploymentRevision"],
                    "evaluationReportId": report["id"],
                    "evaluationReportHash": status["latestEvaluationReport"]["reportHash"],
                    "effectiveFromTradeDate": first.isoformat(),
                    "confirmationText": "确认启用V3",
                    "reason": "验证休市日拦截",
                    "confirmations": {
                        "readFrozenReport": True,
                        "confirmFixedVersions": True,
                        "futureRunsOnly": True,
                        "understandRollback": True,
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
