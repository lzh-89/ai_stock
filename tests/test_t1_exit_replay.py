import unittest

from backend.app.engines.t1_exit_replay import replay_adaptive_exit, simulate_entry_execution
from backend.app.services.t1_overnight_selection_service import _adaptive_exit_enabled_for_run


PROFILE = {
    "version": "execution_profile_v2_participation",
    "slotNotionalCny": 33333.33,
    "orderLatencySeconds": 5,
    "maxParticipationRate": 0.01,
    "minimumCommissionCny": 5.0,
    "commissionPct": 0.025,
    "stampTaxPct": 0.05,
    "transferFeePct": 0.0,
    "buySlippagePct": 0.1,
    "sellSlippagePct": 0.1,
    "boardLot": 100,
}


def bar(day, time_text, close, low=None, volume=1_000_000):
    return {
        "time": f"{day}T{time_text}:00",
        "timeLabel": "end",
        "open": close,
        "high": close,
        "low": close if low is None else low,
        "close": close,
        "volume": volume,
        "amount": close * volume,
    }


class T1ExitReplayTest(unittest.TestCase):
    def _entry(self):
        return simulate_entry_execution(
            series=[bar("2026-10-13", "14:41", 100), bar("2026-10-13", "14:42", 100)],
            recommendation_date="2026-10-13",
            signal_published_at="2026-10-13T14:40:00",
            reference_price=100,
            profile=PROFILE,
        )

    def test_entry_uses_first_full_bar_after_latency_and_real_fees(self):
        result = self._entry()
        self.assertEqual(result["status"], "executed_from_minute")
        self.assertEqual(result["fills"][0]["barEnd"], "2026-10-13T14:42:00")
        self.assertGreater(result["price"], 100)
        self.assertEqual(result["commissionCny"], 7.5075)

    def test_entry_applies_minimum_commission_for_small_notional(self):
        profile = {**PROFILE, "slotNotionalCny": 19_000}
        result = simulate_entry_execution(
            series=[bar("2026-10-13", "14:41", 100), bar("2026-10-13", "14:42", 100)],
            recommendation_date="2026-10-13",
            signal_published_at="2026-10-13T14:40:00",
            reference_price=100,
            profile=profile,
        )
        self.assertEqual(result["filledQuantity"], 100)
        self.assertEqual(result["commissionCny"], 5.0)

    def test_participation_normalizes_lot_volume_to_shares(self):
        rows = []
        for minute in (41, 42, 43, 44):
            row = bar("2026-10-13", f"14:{minute:02d}", 100, volume=100)
            row["volumeUnit"] = "lot"
            row["amount"] = 100 * 100 * 100
            rows.append(row)
        result = simulate_entry_execution(
            series=rows,
            recommendation_date="2026-10-13",
            signal_published_at="2026-10-13T14:40:00",
            reference_price=100,
            profile=PROFILE,
        )
        self.assertEqual(result["status"], "executed_from_minute")
        self.assertEqual(result["filledQuantity"], 300)
        self.assertEqual({item["priceSource"] for item in result["fills"]}, {"minute_vwap"})

    def test_strong_open_splits_exit_without_future_branch_selection(self):
        entry = self._entry()
        series = [bar("2026-10-14", f"09:{minute:02d}", 103 + (minute - 31) * 0.1) for minute in range(31, 49)]
        result = replay_adaptive_exit(
            code="000001",
            industry_group="IND_BANK",
            next_trade_date="2026-10-14",
            minute_series=series,
            entry_execution=entry,
            opening_evidence={},
            profile=PROFILE,
        )
        self.assertEqual(result["branch"], "strong_open_split_exit")
        self.assertEqual(len(result["legs"]), 2)
        self.assertEqual(result["executionStatus"], "executed")
        self.assertIsNotNone(result["netReturnPct"])

    def test_0935_breakdown_excludes_decision_bar_from_predecision_low(self):
        entry = self._entry()
        series = [
            bar("2026-10-14", "09:31", 100, low=99.5),
            bar("2026-10-14", "09:32", 100, low=99.2),
            bar("2026-10-14", "09:33", 100, low=99.0),
            bar("2026-10-14", "09:34", 100, low=99.1),
            bar("2026-10-14", "09:35", 98.5, low=98.0),
            bar("2026-10-14", "09:36", 200),
            bar("2026-10-14", "09:37", 98.0),
        ]
        result = replay_adaptive_exit(
            code="000001",
            industry_group="IND_BANK",
            next_trade_date="2026-10-14",
            minute_series=series,
            entry_execution=entry,
            opening_evidence={},
            profile=PROFILE,
        )
        self.assertEqual(result["branch"], "breakdown_exit")
        self.assertEqual(result["decisionReason"], "09:35 跌破此前低点且低于 VWAP")

    def test_low_open_requires_on_time_opening_evidence(self):
        entry = self._entry()
        series = [bar("2026-10-14", f"09:{minute:02d}", 98) for minute in range(31, 49)]
        unavailable = replay_adaptive_exit(
            code="000001", industry_group="IND_BANK", next_trade_date="2026-10-14",
            minute_series=series, entry_execution=entry, opening_evidence={}, profile=PROFILE,
        )
        self.assertEqual(unavailable["branch"], "fixed_0945_exit")
        evidence = {
            "decisionEligible": True,
            "snapshotPublishedAt": "2026-10-14T09:31:40",
            "marketCoveragePct": 95,
            "downRatioPct": 65,
            "mainIndexStatus": "unavailable",
        }
        triggered = replay_adaptive_exit(
            code="000001", industry_group="IND_BANK", next_trade_date="2026-10-14",
            minute_series=series, entry_execution=entry, opening_evidence=evidence, profile=PROFILE,
        )
        self.assertEqual(triggered["branch"], "opening_risk_exit")

    def test_unknown_minute_semantics_never_produces_learning_return(self):
        entry = self._entry()
        unknown = [{"time": "2026-10-14T09:31:00", "close": 101, "volume": 10000, "amount": 1010000}]
        result = replay_adaptive_exit(
            code="000001", industry_group="", next_trade_date="2026-10-14",
            minute_series=unknown, entry_execution=entry, opening_evidence={}, profile=PROFILE,
        )
        self.assertEqual(result["dataStatus"], "minute_semantics_unknown")
        self.assertIsNone(result["netReturnPct"])

    def test_adaptive_exit_only_enables_for_v5_with_execution_profile_v2(self):
        self.assertFalse(_adaptive_exit_enabled_for_run({"market": {}}))
        self.assertFalse(
            _adaptive_exit_enabled_for_run(
                {
                    "market": {
                        "strategyPolicy": {
                            "strategyPolicyVersion": "2026-10-13收益优化.shadow.v4",
                            "executionProfileVersion": "execution_profile_v2_participation",
                        }
                    }
                }
            )
        )
        self.assertFalse(
            _adaptive_exit_enabled_for_run(
                {
                    "market": {
                        "strategyPolicy": {
                            "strategyPolicyVersion": "2026-10-13收益优化.shadow.v5",
                            "executionProfileVersion": "execution_profile_v1",
                        }
                    }
                }
            )
        )
        self.assertTrue(
            _adaptive_exit_enabled_for_run(
                {
                    "market": {
                        "strategyPolicy": {
                            "strategyPolicyVersion": "2026-10-13收益优化.shadow.v5",
                        },
                        "executionProfile": {"version": "execution_profile_v2_participation"},
                    }
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
