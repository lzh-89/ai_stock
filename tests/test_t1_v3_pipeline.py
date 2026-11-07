import unittest
from datetime import datetime, timedelta

from backend.app.engines import t1_v3_pipeline as v3
from backend.app.services.t1_overnight_selection_service import T1OvernightSelectionService


def candidate(index, *, tradable=True, score=1.0):
    code = str(index + 1).zfill(6)
    return {
        "code": code,
        "name": f"股票{index}",
        "amount": 10_000_000 + index * 100_000,
        "change": (index % 12) - 2,
        "relStrength": index % 100,
        "industry": f"行业{index % 12}",
        "industryGroup": f"I{index % 12}",
        "primaryDriver": f"驱动{index % 8}",
        "driverGroup": f"D{index % 4}",
        "tradeability": {"level": "tradable" if tradable else "hard-to-buy"},
        "formalTradeabilityEligible": tradable,
        "preliminaryScore": 1000 - index,
        "preliminaryScoreRank": index + 1,
        "recallConservativeScore": score - index / 1000,
        "recallRankingEligible": True,
        "recallEstimate": {"uncertaintyPenaltyPct": (index % 10) / 100},
        "v3RecallRank": index + 1,
    }


def minute_series(period_minutes=15, count=10, start="2026-10-20T09:30:00", step=0.02):
    moment = datetime.fromisoformat(start)
    result = []
    for index in range(count):
        close = 10 + index * step
        end = moment + timedelta(minutes=period_minutes * (index + 1))
        result.append(
            {
                "time": end.isoformat(timespec="seconds"),
                "bucketEnd": end.isoformat(timespec="seconds"),
                "open": close - 0.01,
                "high": close + 0.03,
                "low": close - 0.03,
                "close": close,
                "volume": 1000 + index * 10,
            }
        )
    return result


class T1V3PipelineTest(unittest.TestCase):
    def test_recall_model_requires_independent_days_and_samples(self):
        samples = []
        for day in range(60):
            for index in range(50):
                samples.append(
                    {
                        "recommendationDate": f"2026-{1 + day // 28:02d}-{1 + day % 28:02d}",
                        "recallStratum": "mixed|0_2|70_84|q3|tradable",
                        "openProxyReturnPct": 0.2 if index % 2 else -0.1,
                        "inclusionProbability": 0.5,
                    }
                )
        model = v3.build_recall_model(samples)
        self.assertTrue(model["ready"])
        self.assertEqual(model["sampleCount"], 3000)
        estimate = model["bins"]["mixed|0_2|70_84|q3|tradable"]
        self.assertTrue(estimate["rankingEligible"])
        self.assertIsNotNone(estimate["recallConservativeScorePct"])

    def test_collecting_recall_preserves_existing_pool_order(self):
        rows = [candidate(index) for index in range(20)]
        fallback = [rows[5]["code"], rows[2]["code"], rows[9]["code"]]
        result = v3.build_recall_pool(
            rows,
            {"version": "test", "ready": False, "status": "collecting", "bins": {}},
            "mixed",
            "seed",
            fallback_codes=fallback,
            limit=5,
        )
        self.assertEqual([row["code"] for row in result["pool"][:3]], fallback)
        self.assertEqual(result["modelStatus"], "collecting")

    def test_validation_allocation_is_unique_and_budgeted(self):
        rows = [candidate(index, tradable=index < 115) for index in range(130)]
        result = v3.allocate_validation(rows, "run-1")
        codes = [row["code"] for row in result["candidates"]]
        self.assertEqual(len(codes), 120)
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(sum(result["roleCounts"].values()), 120)
        self.assertGreaterEqual(result["roleCounts"].get("core", 0), 70)

    def test_structure_features_use_only_complete_cutoff_series(self):
        series = minute_series()
        quality = v3.validate_15m_data(
            {"quoteTradeDate": "2026-10-20"},
            series,
            recommendation_date="2026-10-20",
            decision_cutoff="2026-10-20T14:40:00",
            source="provider",
            quality_status="available",
        )
        self.assertTrue(quality["dataEligible"])
        features = v3.calculate_structure_features(quality["series"], relative_strength=80, change=2)
        self.assertEqual(features["status"], "available")
        self.assertIn("vwapEstimate", features)
        self.assertIn("structureScore", features)

    def test_tail_gate_separates_missing_data_from_active_top0(self):
        result = v3.evaluate_tail_gate(
            [],
            recommendation_date="2026-10-20",
            decision_cutoff="2026-10-20T14:40:00",
            source="",
        )
        self.assertTrue(result["tailNegative"])
        self.assertIn("1分钟数据来源不合格", result["reasons"])

    def test_collecting_net_model_still_allocates_calibration_sample(self):
        rows = []
        for index in range(80):
            row = candidate(index)
            row.update(dataEligible=True, netExpectationEstimate={"rankingEligible": False})
            rows.append(row)
        result = v3.allocate_tail_pool(rows, "collecting-run", limit=30)
        self.assertEqual(result["mode"], "collection_calibration")
        self.assertEqual(result["selectedCount"], 30)
        self.assertTrue(all(item["v3CalibrationSample"] for item in result["candidates"]))
        self.assertTrue(all(item["v3CalibrationInclusionProbability"] > 0 for item in result["candidates"]))

    def test_portfolio_uses_positive_expectation_and_market_slot_limit(self):
        rows = []
        for index in range(4):
            row = candidate(index)
            row.update(
                dataEligible=True,
                formalTradeabilityEligible=True,
                minute15mStatus="available",
                minute1mStatus="available",
                v3ValidationRank=index + 1,
                v3TailRank=index + 1,
                v3TailGate={"tailNegative": False},
                netExpectationEstimate={
                    "rankingEligible": True,
                    "conservativeScorePct": 0.5 - index * 0.05,
                    "uncertaintyPenaltyPct": 0.02,
                },
            )
            rows.append(row)
        result = v3.finalize_shadow_portfolio(rows, "trend_up")
        self.assertEqual(result["holdingCount"], 2)
        self.assertEqual(result["cashSlotCount"], 1)
        self.assertFalse(result["affectsOfficialRecommendation"])

    def test_shadow_result_never_changes_legacy_official_picks(self):
        service = object.__new__(T1OvernightSelectionService)
        legacy = [{"code": "000001"}]
        market = {"v3Strategy": {"decisionStatus": "selected", "picks": [{"code": "000002"}]}}
        picks, mode, _ = service._apply_deployed_v3_result(
            request={"strategyDeploymentSnapshot": {"officialVersionForThisRun": "legacy"}},
            market=market,
            legacy_picks=legacy,
            legacy_selection_mode="ai_t1_overnight",
            legacy_error="",
        )
        self.assertEqual(picks, legacy)
        self.assertEqual(mode, "ai_t1_overnight")

    def test_deployed_v3_technical_failure_falls_back_instead_of_active_top0(self):
        service = object.__new__(T1OvernightSelectionService)
        legacy = [{"code": "000001"}]
        market = {"v3Strategy": {"decisionStatus": "technical_failure", "picks": []}, "baselineGroups": {}}
        picks, mode, _ = service._apply_deployed_v3_result(
            request={"strategyDeploymentSnapshot": {"officialVersionForThisRun": v3.PIPELINE_VERSION}},
            market=market,
            legacy_picks=legacy,
            legacy_selection_mode="ai_t1_overnight",
            legacy_error="",
        )
        self.assertEqual(picks, legacy)
        self.assertEqual(mode, "ai_t1_overnight")
        self.assertTrue(market["strategyDeploymentRun"]["technicalDegraded"])
        self.assertEqual(market["baselineGroups"]["legacyOfficialControl"], legacy)

    def test_deployed_v3_active_top0_is_preserved(self):
        service = object.__new__(T1OvernightSelectionService)
        market = {"v3Strategy": {"decisionStatus": "active_top0", "picks": []}, "baselineGroups": {}}
        picks, mode, reason = service._apply_deployed_v3_result(
            request={"strategyDeploymentSnapshot": {"officialVersionForThisRun": v3.PIPELINE_VERSION}},
            market=market,
            legacy_picks=[{"code": "000001"}],
            legacy_selection_mode="ai_t1_overnight",
            legacy_error="",
        )
        self.assertEqual(picks, [])
        self.assertEqual(mode, "v3_active_top0")
        self.assertIn("主动空仓", reason)


if __name__ == "__main__":
    unittest.main()
