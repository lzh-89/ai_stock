import unittest
import tempfile
from pathlib import Path

from backend.app.services import t1_overnight_selection_service as svc
from backend.app.database import Database


def day(index, value, excess=None, status="ok", recovery=None, risk_off=False):
    return {
        "runId": index,
        "recommendationDate": f"2026-09-{index:02d}",
        "status": status,
        "holdingCount": 2 if status != "cash" else 0,
        "portfolioReturnPct": value,
        "benchmarkReturnPct": 0.0,
        "excessReturnPct": value if excess is None else excess,
        "recoveryHoldingCount": 2 if recovery is not None else 0,
        "recoveryReturnPct": recovery,
        "recoveryExcessReturnPct": recovery,
        "marketRiskOff": risk_off,
    }


class T1RiskPolicyTest(unittest.TestCase):
    def test_three_negative_days_only_enter_watch(self):
        state = svc._default_risk_policy_state()
        result = svc._evaluate_risk_policy(state, [day(1, -0.4), day(2, -0.3), day(3, -0.2)])
        self.assertEqual(result["state"], "performance_watch")
        self.assertIn("连续三个", result["reason"])

    def test_single_day_hard_loss_enters_shadow_only(self):
        state = svc._default_risk_policy_state()
        result = svc._evaluate_risk_policy(state, [day(1, -5.2, -4.5)])
        self.assertEqual(result["state"], "performance_shadow_only")
        self.assertIn("-5%", result["reason"])

    def test_incomplete_official_holding_enters_data_hold(self):
        state = svc._default_risk_policy_state()
        result = svc._evaluate_risk_policy(state, [day(1, None, status="data_incomplete")])
        self.assertEqual(result["state"], "data_hold")

    def test_epoch_start_excludes_old_losses(self):
        state = svc._default_risk_policy_state()
        state["monitoringEpoch"] = 2
        state["monitoringFromTradeDate"] = "2026-09-04"
        result = svc._evaluate_risk_policy(
            state,
            [day(1, -6.0), day(2, -1.0), day(3, -1.0), day(4, 0.5)],
        )
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["evaluation"]["effectiveHoldingDays"], 1)

    def test_shadow_mode_never_blocks_official_recommendation(self):
        market = {"strategyPolicy": {"selectionPolicyTreatmentPortfolio": [{"code": "600000"}]}}
        state = svc._default_risk_policy_state()
        state["state"] = "performance_shadow_only"
        svc._attach_risk_policy_state_snapshot(market, state, {"riskPolicyMode": "shadow"})
        effect = market["strategyPolicy"]["strategyRiskStateEffect"]
        self.assertFalse(effect["blocksOfficialRecommendation"])
        self.assertEqual(market["baselineGroups"]["recoveryShadow"][0]["code"], "600000")

    def test_enforced_mode_blocks_only_hard_states(self):
        market = {"strategyPolicy": {"selectionPolicyTreatmentPortfolio": []}}
        state = svc._default_risk_policy_state()
        state["state"] = "performance_watch"
        svc._attach_risk_policy_state_snapshot(market, state, {"riskPolicyMode": "enforced"})
        self.assertFalse(market["strategyPolicy"]["strategyRiskStateEffect"]["blocksOfficialRecommendation"])
        state["state"] = "performance_shadow_only"
        svc._attach_risk_policy_state_snapshot(market, state, {"riskPolicyMode": "enforced"})
        self.assertTrue(market["strategyPolicy"]["strategyRiskStateEffect"]["blocksOfficialRecommendation"])

    def test_event_gate_enforced_downgrades_until_provider_ready(self):
        item = {
            "code": "600000",
            "recommendationDate": "2026-10-13",
            "scanTime": "14:40",
            "eventRiskStatus": "risk",
            "eventRiskEvidence": {
                "eventRiskStatus": "risk",
                "triggered": True,
                "eventGateReady": False,
                "providerVersion": "test_v1",
                "reason": "股东拟减持",
            },
        }
        result = svc._t1_negative_gate_evaluation(
            item,
            "trend_up",
            "normal",
            {
                "sectorNegativeGateMode": "off",
                "tailNegativeGateMode": "off",
                "overnightRiskGateMode": "off",
                "chaseRiskGateMode": "off",
                "eventRiskGateMode": "enforced",
            },
        )
        event_gate = next(gate for gate in result["gates"] if gate["gateKey"] == "event_risk")
        self.assertEqual(event_gate["configuredMode"], "shadow")
        self.assertTrue(event_gate["triggered"])
        self.assertFalse(result["affectsOfficialRecommendation"])

    def test_recovery_requires_five_positive_complete_days(self):
        state = svc._default_risk_policy_state()
        state.update(
            {
                "state": "performance_shadow_only",
                "triggeredAt": "2026-09-01T15:30:00",
                "reason": "test",
            }
        )
        days = [day(index, 0.0, recovery=0.4) for index in range(2, 7)]
        result = svc._evaluate_risk_policy(state, days)
        self.assertTrue(result["recoveryProgress"]["eligible"])

    def test_risk_policy_revision_conflict_does_not_overwrite_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "app.sqlite")
            state = svc._default_risk_policy_state()
            created = database.save_t1_risk_policy_transition(
                svc.RISK_POLICY_STATE_KEY,
                0,
                state,
                {"eventType": "initialized", "nextState": "active"},
            )
            self.assertTrue(created)
            conflicting = {**state, "revision": 2, "state": "performance_shadow_only"}
            saved = database.save_t1_risk_policy_transition(
                svc.RISK_POLICY_STATE_KEY,
                0,
                conflicting,
                {"eventType": "manual_pause", "previousState": "active", "nextState": "performance_shadow_only"},
            )
            self.assertFalse(saved)
            self.assertEqual(database.get_app_setting(svc.RISK_POLICY_STATE_KEY)["state"], "active")


if __name__ == "__main__":
    unittest.main()
