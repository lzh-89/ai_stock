import unittest

from backend.app.engines.t1_experiment_sampling import (
    MIN_BIN_SAMPLES,
    MIN_TOTAL_SAMPLES,
    fit_conservative_net_expectation,
    prepare_execution_calibration,
    score_net_expectation_candidate,
)


class T1ExperimentSamplingTest(unittest.TestCase):
    def _candidate(self, index, driver="relative_strength", score=75, risk=40):
        return {
            "code": str(index).zfill(6),
            "primaryDriver": driver,
            "t1Score": score,
            "t1Dimensions": {"overnightRisk": risk},
        }

    def test_sampling_is_stable_bounded_and_records_unsampled_probabilities(self):
        candidates = [self._candidate(index, driver=f"driver_{index % 6}") for index in range(1, 41)]
        roles = {"000001": ["frozen_decision"], "000002": ["risk_gate_blocked"]}
        first = prepare_execution_calibration(
            candidates,
            roles,
            run_id=94,
            recommendation_date="2026-10-13",
            strategy_policy_version="policy-v4",
            base_market_state="mixed",
        )
        second = prepare_execution_calibration(
            reversed(candidates),
            roles,
            run_id=94,
            recommendation_date="2026-10-13",
            strategy_policy_version="policy-v4",
            base_market_state="mixed",
        )
        self.assertEqual(first["selectedCount"], 10)
        self.assertEqual(
            [item["code"] for item in first["selectedCandidates"]],
            [item["code"] for item in second["selectedCandidates"]],
        )
        updates = {item["code"]: item for item in first["candidateUpdates"]}
        self.assertTrue(updates["000001"]["certaintyUnit"])
        self.assertEqual(updates["000001"]["populationInclusionProbability"], 1.0)
        self.assertEqual(len(updates), 40)
        self.assertTrue(all("samplingStatus" in item for item in updates.values()))
        self.assertTrue(all(item["stratumSampleCount"] <= 2 for item in updates.values() if not item["certaintyUnit"]))

    def test_sampler_never_uses_next_day_fields(self):
        candidates = [self._candidate(index) for index in range(1, 21)]
        clean = prepare_execution_calibration(
            candidates, {}, run_id=95, recommendation_date="2026-10-14",
            strategy_policy_version="policy-v4", base_market_state="mixed",
        )
        contaminated = prepare_execution_calibration(
            [dict(item, nextDay={"openReturnPct": 99 - index}) for index, item in enumerate(candidates)],
            {}, run_id=95, recommendation_date="2026-10-14",
            strategy_policy_version="policy-v4", base_market_state="mixed",
        )
        self.assertEqual(
            [item["code"] for item in clean["selectedCandidates"]],
            [item["code"] for item in contaminated["selectedCandidates"]],
        )

    def test_model_stays_observation_only_until_total_and_bin_thresholds(self):
        candidate = self._candidate(1)
        small = [
            {
                "recommendationDate": f"2026-09-{(index % 20) + 1:02d}",
                "stratum": "mixed|relative_strength|70_79|lt48",
                "netReturnPct": 0.4 if index % 2 else -0.2,
                "populationInclusionProbability": 0.5,
            }
            for index in range(MIN_BIN_SAMPLES)
        ]
        model = fit_conservative_net_expectation(small)
        self.assertFalse(model["rankingReady"])
        self.assertFalse(score_net_expectation_candidate(candidate, model, "mixed")["rankingEligible"])

        enough = [dict(small[index % len(small)], code=str(index)) for index in range(MIN_TOTAL_SAMPLES)]
        ready = fit_conservative_net_expectation(enough)
        score = score_net_expectation_candidate(candidate, ready, "mixed")
        self.assertTrue(ready["rankingReady"])
        self.assertTrue(score["rankingEligible"])
        self.assertIsNotNone(score["conservativeScorePct"])


if __name__ == "__main__":
    unittest.main()
