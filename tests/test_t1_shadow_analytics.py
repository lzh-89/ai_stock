import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend.app.database import Database
from backend.app.services.t1_shadow_analytics_service import (
    T1ShadowAnalyticsService,
    _select_window,
    _v3_main_fact,
)
from backend.app.services import t1_overnight_selection_service as selection


class T1ShadowAnalyticsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "app.sqlite")
        self.service = T1ShadowAnalyticsService(self.database)

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self, recommendation_date, market, mode="ai_t1_overnight", role="official", force=False, rebuild=True):
        run_id = self.database.create_t1_overnight_selection_run(
            {
                "recommendationDate": recommendation_date,
                "scanTime": "14:40",
                "requestedSource": "auto",
                "status": "done",
                "selectionMode": mode,
                "finalCount": 1,
                "request": {"runRole": role, "poolSize": 500, "force": force},
                "market": {"runRole": role, **market},
            },
            [],
        )
        self.database.update_t1_overnight_selection_run(
            run_id,
            {"status": "done", "selectionMode": mode, "finalCount": 1, "market": {"runRole": role, **market}},
        )
        if rebuild:
            self.service.rebuild()
        return run_id

    def _candidate(self, code, rank, industry_code, candidate_types):
        industry_name = f"行业{industry_code}" if industry_code else ""
        matched = []
        if industry_code:
            matched.append({"sectorType": "industry", "sectorCode": industry_code, "sectorName": industry_name})
        return {
            "code": code,
            "aiRank": rank,
            "t1Score": 90 - rank,
            "industrySector": industry_name,
            "candidateTypes": candidate_types,
            "sectorEvidence": {
                "evidenceLayers": {"identity": {"status": "available" if industry_code else "unavailable"}},
                "matchedSectors": matched,
            },
            "riskProfile": {"key": "normal"},
            "t1Dimensions": {"overnightRisk": 20},
            "change": 2,
        }

    def _gates_off(self):
        return {
            **selection.DEFAULT_STAGE_CONFIG,
            "sectorNegativeGateMode": "off",
            "tailNegativeGateMode": "off",
            "overnightRiskGateMode": "off",
            "chaseRiskGateMode": "off",
            "eventRiskGateMode": "off",
        }

    def test_legacy_families_are_aggregated_separately(self):
        self._run(
            "2026-10-08",
            {
                "shadowTop3": {"picks": [{"code": "000001"}]},
                "shadowDeepSeekTailTop3": {"picks": [{"code": "000002"}]},
                "shadowComparison": {
                    "outcomeComparison": {
                        "shadow": {"status": "ok", "count": 1, "tradeableCount": 1, "portfolioReturnPct": 1.2, "slotCount": 3, "cashSlots": 2},
                        "rows": [{"code": "000001", "dataStatus": "ok"}],
                        "deepSeekTail": {"status": "ok", "count": 1, "tradeableCount": 1, "portfolioReturnPct": -0.6, "slotCount": 3, "cashSlots": 2},
                        "deepSeekTailRows": [{"code": "000002", "dataStatus": "ok"}],
                    }
                },
            },
        )
        rule = self.service.summary(shadow_family="legacy_tail_rule", shadow_role="tail_rule")
        ai = self.service.summary(shadow_family="legacy_tail_ai", shadow_role="tail_ai")
        self.assertEqual(rule["roles"][0]["cumulativeReturnPct"], 1.2)
        self.assertEqual(ai["roles"][0]["cumulativeReturnPct"], -0.6)
        self.assertIsNone(rule["roles"][0]["avgPairedDeltaPct"])
        self.assertIsNone(rule["roles"][0]["avoidedLossPct"])
        self.assertIsNone(rule["roles"][0]["missedProfitPct"])
        self.assertNotIn("detail", rule["facts"][0])
        self.assertEqual(rule["comparabilityStatus"], "role_and_version_isolated")

    def test_research_rerun_does_not_duplicate_canonical_day(self):
        market = {
            "shadowTop3": {"picks": [{"code": "000001"}]},
            "shadowComparison": {"outcomeComparison": {"shadow": {"status": "ok", "count": 1, "tradeableCount": 1, "portfolioReturnPct": 1.0}}},
        }
        self._run("2026-10-08", market)
        self._run("2026-10-08", market, role="research", force=True)
        result = self.service.summary(shadow_family="legacy_tail_rule", shadow_role="tail_rule")
        self.assertEqual(result["migratedRunCount"], 1)

    def test_policy_experiment_keeps_treatment_and_control_pair(self):
        self._run(
            "2026-10-09",
            {
                "strategyPolicy": {
                    "strategyPolicyVersion": "policy-v1",
                    "executionProfileVersion": "execution-v1",
                    "negativeGateAudit": {
                        "blockedCandidateCount": 1,
                        "gates": {
                            "sector": {
                                "label": "板块状态",
                                "configuredMode": "shadow",
                                "candidateCount": 2,
                                "evaluatedCount": 2,
                                "hitCount": 1,
                                "passCount": 1,
                                "unavailableCount": 0,
                                "evidenceAvailableCount": 2,
                            }
                        },
                    },
                },
                "baselineComparison": {
                    "selectionPolicyControl": {"count": 2, "tradeableCount": 2, "portfolioReturnPct": -1.0},
                    "marketGateTreatment": {"count": 0, "tradeableCount": 0, "portfolioReturnPct": 0.0},
                    "riskGateControl": {"count": 2, "tradeableCount": 2, "portfolioReturnPct": -1.0},
                    "riskGateTreatment": {"count": 1, "tradeableCount": 1, "portfolioReturnPct": 0.0},
                },
            },
        )
        result = self.service.summary(
            shadow_family="policy_experiment",
            shadow_role="policy_market_gate",
            strategy_policy_version="policy-v1",
        )
        report = result["roles"][0]
        self.assertEqual(report["avgPairedDeltaPct"], 1.0)
        self.assertEqual(report["avoidedLossPct"], 1.0)
        self.assertEqual(report["missedProfitPct"], 0.0)
        risk_result = self.service.summary(
            shadow_family="policy_experiment",
            shadow_role="policy_risk_gate",
            strategy_policy_version="policy-v1",
        )
        risk_report = risk_result["roles"][0]
        self.assertEqual(risk_report["gateBlockedCandidateCount"], 1)
        self.assertEqual(risk_report["gateDiagnostics"][0]["gateKey"], "sector")
        self.assertEqual(risk_report["gateDiagnostics"][0]["hitCount"], 1)
        self.assertEqual(risk_report["gateDiagnostics"][0]["evidenceCoveragePct"], 100.0)

    def test_v3_main_uses_exact_open15_replay_and_same_day_official_control(self):
        self._run(
            "2026-10-10",
            {
                "v3Strategy": {
                    "pipelineVersion": "2026-11-06.t1_v3_shadow.v1",
                    "decisionStatus": "selected",
                    "holdingCount": 1,
                    "cashSlotCount": 1,
                    "slotLimit": 2,
                    "picks": [{"code": "000003"}],
                    "versionBundle": {"pipelineVersion": "2026-11-06.t1_v3_shadow.v1"},
                },
                "baselineComparison": {
                    "v3Main": {
                        "count": 1,
                        "tradeableCount": 1,
                        "portfolioReturnPct": 0.6,
                        "componentRows": [{"code": "000003", "nextTradeDate": "2026-10-12", "netReturnPct": 0.6}],
                    },
                    "final": {
                        "count": 1,
                        "tradeableCount": 1,
                        "portfolioReturnPct": -0.2,
                        "componentRows": [{"code": "000001", "nextTradeDate": "2026-10-12", "netReturnPct": -0.2}],
                    },
                },
            },
        )
        result = self.service.summary(
            shadow_family="v3_main_experiment",
            shadow_role="v3_main",
            strategy_policy_version="2026-11-06.t1_v3_shadow.v1",
        )
        report = result["roles"][0]
        self.assertEqual(report["avgDailyNetReturnPct"], 0.6)
        self.assertEqual(report["avgPairedDeltaPct"], 0.8)
        facts = self.database.list_t1_shadow_strategy_daily_facts(
            shadow_family="v3_main_experiment", shadow_role="v3_main"
        )
        self.assertFalse(facts[0]["detail"]["usesProxyReturn"])
        self.assertIn("开盘后15分钟", facts[0]["detail"]["outcomeBusinessMeaning"])

    def test_strategy_policy_shadow_does_not_change_official_selection(self):
        market = {
            "marketContext": {"state": "heavy_down", "metrics": {"downRatioPct": 79.0, "averageEqualWeightedChangePct": -1.4}},
            "baselineGroups": {},
        }
        ranking = [
            {"code": "000001", "aiRank": 1, "industrySector": "银行", "candidateTypes": ["late_money_inflow"], "tailSupportShadow": {"status": "strong_support"}},
            {"code": "000002", "aiRank": 2, "industrySector": "医药", "candidateTypes": ["vwap_support"], "tailSupportShadow": {"status": "strong_support"}},
        ]
        selection._attach_strategy_policy_shadow(market, ranking, selection.DEFAULT_STAGE_CONFIG)
        self.assertFalse(market["strategyPolicy"]["affectsOfficialRecommendation"])
        self.assertEqual(market["strategyPolicy"]["regimePhase"], "risk_off")
        self.assertEqual(market["baselineGroups"]["marketGateTreatment"], [])
        self.assertEqual([item["code"] for item in ranking], ["000001", "000002"])

    def test_negative_gate_modes_are_isolated_and_audited(self):
        market = {"marketContext": {"state": "mixed", "metrics": {}}}
        ranking = [
            {
                "code": "000001",
                "aiRank": 1,
                "recommendationDate": "2026-10-13",
                "scanTime": "14:40",
                "sectorStateShadow": "overheated",
                "sectorStateVersion": "sector-v1",
                "sectorEvidenceStatus": "available",
                "sectorEvidence": {"status": "available", "tradeDate": "2026-10-13", "scanTime": "14:40"},
                "tailSupportShadow": {
                    "status": "strong_support",
                    "version": selection.T1_TAIL_SUPPORT_SHADOW_VERSION,
                    "classifierVersion": selection.T1_TAIL_SUPPORT_SHADOW_VERSION,
                    "pointCount": 11,
                    "period": "1m",
                    "tradeDate": "2026-10-13",
                    "minuteUsableAsOfTime": "14:40",
                    "sourceQualityStatus": "available",
                },
                "riskProfile": {"key": "normal"},
                "t1Dimensions": {"overnightRisk": 25},
                "change": 3,
            }
        ]
        config = {**selection.DEFAULT_STAGE_CONFIG, "sectorNegativeGateMode": "off"}
        selection._attach_strategy_policy_shadow(market, ranking, config)
        self.assertEqual([item["code"] for item in market["baselineGroups"]["riskGateTreatment"]], ["000001"])
        sector = market["strategyPolicy"]["negativeGateAudit"]["gates"]["sector"]
        self.assertEqual(sector["configuredMode"], "off")
        self.assertEqual(sector["hitCount"], 0)

    def test_negative_gate_records_each_trigger_without_changing_official_ranking(self):
        market = {"marketContext": {"state": "mixed", "metrics": {}}}
        ranking = [
            {
                "code": "000001",
                "aiRank": 1,
                "recommendationDate": "2026-10-13",
                "scanTime": "14:40",
                "sectorStateShadow": "overheated",
                "sectorStateVersion": "sector-v1",
                "sectorEvidenceStatus": "available",
                "sectorEvidence": {"status": "available", "tradeDate": "2026-10-13", "scanTime": "14:40"},
                "tailSupportShadow": {
                    "status": "late_dump",
                    "version": selection.T1_TAIL_SUPPORT_SHADOW_VERSION,
                    "classifierVersion": selection.T1_TAIL_SUPPORT_SHADOW_VERSION,
                    "pointCount": 11,
                    "period": "1m",
                    "tradeDate": "2026-10-13",
                    "minuteUsableAsOfTime": "14:40",
                    "sourceQualityStatus": "available",
                },
                "riskProfile": {"key": "aggressive"},
                "t1Dimensions": {"overnightRisk": 52},
                "change": 8,
            }
        ]
        original_codes = [item["code"] for item in ranking]
        selection._attach_strategy_policy_shadow(market, ranking, selection.DEFAULT_STAGE_CONFIG)
        audit = market["strategyPolicy"]["negativeGateAudit"]
        self.assertEqual(audit["blockedCandidateCount"], 1)
        self.assertEqual(audit["gates"]["sector"]["hitCount"], 1)
        self.assertEqual(audit["gates"]["tail_support"]["hitCount"], 1)
        self.assertEqual(audit["gates"]["overnight_risk"]["hitCount"], 1)
        self.assertEqual(audit["gates"]["chase_risk"]["hitCount"], 1)
        self.assertEqual(market["baselineGroups"]["riskGateTreatment"], [])
        self.assertEqual([item["code"] for item in ranking], original_codes)
        self.assertFalse(market["strategyPolicy"]["affectsOfficialRecommendation"])

    def test_diversity_scans_full_risk_sequence_and_replaces_second_slot(self):
        market = {"marketContext": {"state": "trend_up", "metrics": {}}}
        ranking = [
            self._candidate("000003", 3, "IND_MED", ["vwap_support"]),
            self._candidate("000002", 2, "IND_CHIP", ["late_money_inflow"]),
            self._candidate("000001", 1, "IND_BANK", ["late_money_inflow"]),
        ]
        original_order = [item["code"] for item in ranking]
        selection._attach_strategy_policy_shadow(market, ranking, self._gates_off())
        groups = market["baselineGroups"]
        strategy = market["strategyPolicy"]
        self.assertEqual([item["code"] for item in groups["riskGateTreatmentSequence"]], ["000001", "000002", "000003"])
        self.assertEqual([item["code"] for item in groups["riskGateTreatment"]], ["000001", "000002"])
        self.assertEqual([item["code"] for item in groups["portfolioDiversityTreatment"]], ["000001", "000003"])
        self.assertEqual([item["code"] for item in strategy["portfolioDiversityBlockedCandidates"]], ["000002"])
        self.assertEqual(strategy["portfolioDiversityBlockedCandidates"][0]["portfolioConflicts"][0]["rules"], ["same_driver_group"])
        self.assertEqual(strategy["portfolioDiversityAudit"]["replacements"][0], {"slot": 2, "removedCode": "000002", "addedCode": "000003"})
        self.assertEqual([item["code"] for item in ranking], original_order)
        self.assertFalse(strategy["affectsOfficialRecommendation"])

    def test_missing_industry_or_unknown_driver_cannot_open_second_slot(self):
        for second in (
            self._candidate("000002", 2, "", ["vwap_support"]),
            self._candidate("000002", 2, "IND_MED", ["unmapped_signal"]),
        ):
            market = {"marketContext": {"state": "trend_up", "metrics": {}}}
            ranking = [self._candidate("000001", 1, "IND_BANK", ["late_money_inflow"]), second]
            selection._attach_strategy_policy_shadow(market, ranking, self._gates_off())
            self.assertEqual([item["code"] for item in market["baselineGroups"]["portfolioDiversityTreatment"]], ["000001"])
            self.assertFalse(market["strategyPolicy"]["portfolioDiversityAudit"]["secondSlotOpen"])

    def test_rank3_is_exact_frozen_third_and_never_force_filled(self):
        market = {"marketContext": {"state": "trend_up", "metrics": {}}}
        two = [
            self._candidate("000001", 1, "IND_BANK", ["late_money_inflow"]),
            self._candidate("000002", 2, "IND_MED", ["vwap_support"]),
        ]
        selection._attach_strategy_policy_shadow(market, two, self._gates_off())
        self.assertEqual(market["baselineGroups"]["rank3"], [])
        self.assertEqual(market["baselineGroups"]["rank3CashControl"], [])
        self.assertFalse(market["strategyPolicy"]["rank3Audit"]["available"])
        three = two + [self._candidate("000003", 3, "IND_TECH", ["relative_strength_leader"])]
        market = {"marketContext": {"state": "trend_up", "metrics": {}}}
        selection._attach_strategy_policy_shadow(market, three, self._gates_off())
        self.assertEqual([item["code"] for item in market["baselineGroups"]["rank3CashControl"]], ["000001", "000002"])
        self.assertEqual([item["code"] for item in market["baselineGroups"]["rank3"]], ["000001", "000002", "000003"])
        self.assertEqual(market["strategyPolicy"]["rank3Audit"]["rank3Code"], "000003")
        self.assertFalse(market["strategyPolicy"]["rank3Audit"]["forcedFill"])

    def test_net_expectation_freezes_calibration_without_changing_official_groups(self):
        candidates = []
        for index in range(1, 21):
            item = self._candidate(str(index).zfill(6), index, f"IND_{index}", ["relative_strength_leader"])
            item["name"] = f"测试{index}"
            item["preliminaryScoreRank"] = index
            candidates.append(item)
        store = selection.CandidateStore(self.database)
        store.freeze(94, candidates, "snapshot-v1")
        market = {"marketContext": {"state": "mixed", "metrics": {}}, "baselineGroups": {}}
        selection._attach_strategy_policy_shadow(market, candidates[:3], self._gates_off())
        official_before = [item["code"] for item in market["baselineGroups"]["selectionPolicyTreatment"]]
        selection._attach_net_expectation_shadow(
            self.database,
            store,
            market,
            candidates,
            run_id=94,
            recommendation_date="2026-10-13",
        )
        self.assertEqual(
            [item["code"] for item in market["baselineGroups"]["selectionPolicyTreatment"]],
            official_before,
        )
        calibration_count = len(market["baselineGroups"]["executionCalibration"])
        self.assertLessEqual(calibration_count, 10)
        self.assertGreaterEqual(calibration_count, 3)
        self.assertEqual(market["baselineGroups"]["netExpectationTreatment"], [])
        self.assertFalse(market["strategyPolicy"]["netExpectationModel"]["rankingReady"])
        self.assertFalse(market["strategyPolicy"]["netExpectationAffectsOfficialRecommendation"])
        archive = store.all(94)
        self.assertEqual(sum(item.get("sampleSelected") is True for item in archive), calibration_count)

    def test_existing_hard_guardrail_is_common_to_risk_control_and_treatment(self):
        market = {"marketContext": {"state": "trend_up", "metrics": {}}}
        blocked = self._candidate("000001", 1, "IND_BANK", ["late_money_inflow"])
        blocked["riskProfile"] = {"key": "avoid_overnight"}
        allowed = self._candidate("000002", 2, "IND_MED", ["vwap_support"])
        selection._attach_strategy_policy_shadow(market, [blocked, allowed], self._gates_off())
        groups = market["baselineGroups"]
        self.assertEqual([item["code"] for item in groups["riskGateControl"]], ["000002"])
        self.assertEqual([item["code"] for item in groups["riskGateTreatment"]], ["000002"])
        self.assertEqual(market["strategyPolicy"]["existingGuardrailBlockedCandidates"][0]["code"], "000001")
        self.assertEqual(market["strategyPolicy"]["negativeGateAudit"]["blockedCandidateCount"], 0)

    def test_diversity_and_rank3_slot_diagnostics_are_aggregated_separately(self):
        def summary(codes, returns):
            rows = [{"code": code, "netReturnPct": value} for code, value in zip(codes, returns)]
            return {
                "count": len(codes),
                "tradeableCount": len(returns),
                "portfolioReturnPct": round(sum(returns) / 3, 4),
                "componentRows": rows,
            }

        self._run(
            "2026-10-10",
            {
                "strategyPolicy": {
                    "strategyPolicyVersion": "policy-v3",
                    "executionProfileVersion": "execution-v1",
                    "portfolioDiversityAudit": {
                        "replacementCount": 1,
                        "blockedCandidateCount": 1,
                        "secondSlotOpen": True,
                        "conflictCounts": {"sameDriverGroup": 1},
                    },
                    "rank3Audit": {"available": True, "rank3Code": "000003"},
                },
                "baselineComparison": {
                    "riskGateTreatment": summary(["000001", "000002"], [1.0, -1.0]),
                    "portfolioDiversityTreatment": summary(["000001", "000003"], [1.0, 2.0]),
                    "rank3CashControl": summary(["000001", "000002"], [1.0, -1.0]),
                    "rank3": summary(["000001", "000002", "000003"], [1.0, -1.0, 3.0]),
                    "index": {"avgNetReturnPct": 0.5},
                },
            },
        )
        diversity = self.service.summary(shadow_family="policy_experiment", shadow_role="policy_diversity", strategy_policy_version="policy-v3")["roles"][0]
        self.assertEqual(diversity["diversityDiagnostics"]["replacementCount"], 1)
        self.assertEqual(diversity["diversityDiagnostics"]["avgSecondSlotIncrementPct"], 0.6667)
        self.assertIsNone(diversity["diversityDiagnostics"]["portfolioReturnCorrelation"])
        rank3 = self.service.summary(shadow_family="policy_experiment", shadow_role="policy_rank3", strategy_policy_version="policy-v3")["roles"][0]
        self.assertEqual(rank3["rank3Diagnostics"]["avgIncrementPct"], 1.0)
        self.assertEqual(rank3["rank3Diagnostics"]["avgEtfExcessPct"], 2.5)
        self.assertEqual(rank3["rank3Diagnostics"]["sameDayFailureRate"], 0.0)

    def test_net_expectation_stays_collecting_and_reports_calibration_baselines(self):
        self._run(
            "2026-10-13",
            {
                "strategyPolicy": {
                    "strategyPolicyVersion": "policy-v4",
                    "executionProfileVersion": "execution-v1",
                    "calibrationSamplerVersion": "sampler-v1",
                    "netExpectationModelVersion": "model-v1",
                    "netExpectationModel": {
                        "status": "collecting",
                        "rankingReady": False,
                        "sampleCount": 12,
                        "independentTradeDays": 2,
                        "readyBinCount": 0,
                        "binCount": 4,
                        "minimumTotalSamples": 150,
                        "minimumBinSamples": 30,
                    },
                    "executionCalibrationAudit": {
                        "selectedCount": 10,
                        "certaintyCount": 2,
                        "coveredStratumCount": 5,
                        "nonEmptyStratumCount": 8,
                        "coverageStatus": "budget_limited",
                    },
                },
                "baselineGroups": {
                    "executionCalibration": [
                        {
                            "code": "000001",
                            "populationEstimationEligible": True,
                            "populationInclusionProbability": 0.5,
                        }
                    ]
                },
                "baselineComparison": {
                    "netExpectationControl": {"count": 1, "tradeableCount": 1, "portfolioReturnPct": -0.3},
                    "netExpectationTreatment": {"count": 0, "tradeableCount": 0, "portfolioReturnPct": 0.0},
                    "executionCalibration": {"componentRows": [{"code": "000001", "netReturnPct": 0.6}]},
                    "random": {"portfolioReturnPct": -0.1},
                    "index": {"avgNetReturnPct": 0.2},
                },
            },
        )
        report = self.service.summary(
            shadow_family="policy_experiment",
            shadow_role="policy_net_expectation",
            strategy_policy_version="policy-v4",
        )["roles"][0]
        self.assertEqual(report["effectiveDays"], 0)
        detail = report["netExpectationDiagnostics"]
        self.assertFalse(detail["rankingReady"])
        self.assertEqual(detail["trainingSampleCount"], 12)
        self.assertEqual(detail["calibrationProbabilityCount"], 8)
        self.assertEqual(detail["avgEligiblePoolWeightedReturnPct"], 0.6)
        self.assertEqual(detail["budgetLimitedDays"], 1)

    def test_adaptive_exit_keeps_incomplete_execution_in_diagnostics_not_returns(self):
        self._run(
            "2026-10-10",
            {
                "strategyPolicy": {
                    "strategyPolicyVersion": "policy-v5",
                    "executionProfileVersion": "execution-v2",
                },
                "baselineComparison": {
                    "selectionPolicyTreatment": {
                        "count": 2,
                        "tradeableCount": 2,
                        "portfolioReturnPct": -0.2,
                    },
                    "selectionPolicyTreatmentAdaptiveExit": {
                        "count": 2,
                        "componentCount": 2,
                        "tradeableCount": 1,
                        "portfolioReturnPct": None,
                        "executionStatusCounts": {"executed": 1, "partial_execution": 1},
                        "branchCounts": {"opening_risk_exit": 1, "fixed_0945_exit": 1},
                        "componentRows": [
                            {"code": "000001", "netReturnPct": 0.3, "markToMarketReturnPct": 0.3},
                            {"code": "000002", "netReturnPct": None, "markToMarketReturnPct": -1.2},
                        ],
                    },
                },
            },
        )
        report = self.service.summary(
            shadow_family="policy_experiment",
            shadow_role="policy_adaptive_exit",
            strategy_policy_version="policy-v5",
        )["roles"][0]
        self.assertEqual(report["effectiveDays"], 0)
        self.assertIsNone(report["avgPairedDeltaPct"])
        diagnostics = report["adaptiveExitDiagnostics"]
        self.assertEqual(diagnostics["holdingOutcomeCount"], 2)
        self.assertEqual(diagnostics["completeExecutionRate"], 50.0)
        self.assertEqual(diagnostics["incompleteExecutionCount"], 1)
        self.assertEqual(diagnostics["branchCounts"]["opening_risk_exit"], 1)
        self.assertAlmostEqual(diagnostics["avgMarkToMarketReturnPct"], -0.45)
        self.assertEqual(report["gateDiagnostics"], [])
        self.assertEqual(report["rank3Diagnostics"], {})
        self.assertEqual(report["netExpectationDiagnostics"], {})

    def test_adaptive_exit_complete_day_pairs_only_with_fixed_open15m(self):
        self._run(
            "2026-10-11",
            {
                "strategyPolicy": {
                    "strategyPolicyVersion": "policy-v5",
                    "executionProfileVersion": "execution-v2",
                },
                "baselineComparison": {
                    "selectionPolicyTreatment": {
                        "count": 2,
                        "tradeableCount": 2,
                        "portfolioReturnPct": -0.2,
                    },
                    "selectionPolicyTreatmentAdaptiveExit": {
                        "count": 2,
                        "componentCount": 2,
                        "tradeableCount": 2,
                        "portfolioReturnPct": 0.3,
                        "executionStatusCounts": {"executed": 2},
                        "branchCounts": {"breakdown_exit": 1, "strong_open_split_exit": 1},
                        "componentRows": [
                            {"code": "000001", "netReturnPct": 0.4, "markToMarketReturnPct": 0.4},
                            {"code": "000002", "netReturnPct": 0.5, "markToMarketReturnPct": 0.5},
                        ],
                    },
                },
            },
        )
        report = self.service.summary(
            shadow_family="policy_experiment",
            shadow_role="policy_adaptive_exit",
            strategy_policy_version="policy-v5",
        )["roles"][0]
        self.assertEqual(report["effectiveDays"], 1)
        self.assertEqual(report["avgPairedDeltaPct"], 0.5)
        self.assertEqual(report["adaptiveExitDiagnostics"]["completeExecutionRate"], 100.0)

    def test_summary_is_read_only_and_worker_builds_fact_once(self):
        run_id = self._run(
            "2026-10-13",
            {
                "shadowTop3": {"picks": [{"code": "000001"}]},
                "shadowComparison": {
                    "outcomeComparison": {
                        "shadow": {
                            "status": "ok", "count": 1, "tradeableCount": 1,
                            "portfolioReturnPct": 0.6, "slotCount": 3, "cashSlots": 2,
                        }
                    }
                },
            },
            rebuild=False,
        )
        before = self.service.summary(shadow_family="legacy_tail_rule", shadow_role="tail_rule")
        self.assertEqual(before["roles"][0]["effectiveDays"], 0)
        first = self.service.run_due(trigger_source="test")
        self.assertEqual(first["processed"][0]["runId"], run_id)
        self.assertEqual(first["processed"][0]["status"], "done")
        after = self.service.summary(shadow_family="legacy_tail_rule", shadow_role="tail_rule")
        self.assertEqual(after["roles"][0]["effectiveDays"], 1)
        second = self.service.run_due(trigger_source="test")
        self.assertEqual(second["processed"], [])
        self.assertEqual(len(self.database.list_t1_shadow_strategy_daily_facts(
            shadow_family="legacy_tail_rule", shadow_role="tail_rule"
        )), 1)

    def test_sql_lease_and_retry_recover_without_duplicate_fact(self):
        class Clock:
            def __init__(self):
                self.value = datetime.fromisoformat("2026-10-13T16:00:00")

            def __call__(self):
                return self.value

        clock = Clock()
        service = T1ShadowAnalyticsService(self.database, clock=clock)
        self._run(
            "2026-10-13",
            {
                "shadowTop3": {"picks": [{"code": "000001"}]},
                "shadowComparison": {
                    "outcomeComparison": {
                        "shadow": {"status": "ok", "count": 1, "tradeableCount": 1, "portfolioReturnPct": 0.4}
                    }
                },
            },
            rebuild=False,
        )
        self.database.set_app_setting(
            "t1_shadow_analytics_worker_state",
            {"config": {"retryBaseSeconds": 1, "maxAttempts": 3}},
        )
        with patch.object(service, "_rebuild_run", side_effect=RuntimeError("temporary aggregate failure")):
            failed = service.run_due(trigger_source="test")
        self.assertEqual(failed["processed"][0]["status"], "retry_wait")
        job = self.database.list_t1_shadow_aggregate_jobs()[0]
        self.assertEqual(job["attemptCount"], 1)
        self.assertIsNone(
            self.database.acquire_t1_shadow_aggregate_job(
                job["id"], "other-worker", clock().isoformat(timespec="seconds"),
                (clock() + timedelta(seconds=30)).isoformat(timespec="seconds"),
            )
        )
        clock.value += timedelta(seconds=2)
        recovered = service.run_due(trigger_source="test")
        self.assertEqual(recovered["processed"][0]["status"], "done")
        self.assertEqual(len(self.database.list_t1_shadow_strategy_daily_facts(
            shadow_family="legacy_tail_rule", shadow_role="tail_rule"
        )), 1)

    def test_fixed_reports_append_revision_and_cannot_be_overwritten(self):
        for index in range(20):
            self.database.upsert_t1_shadow_strategy_daily_fact(
                {
                    "recommendationDate": f"2026-09-{index + 1:02d}",
                    "canonicalRunId": index + 1,
                    "shadowFamily": "policy_experiment",
                    "shadowRole": "policy_market_gate",
                    "scope": "market_gate",
                    "strategyPolicyVersion": "policy-report-v1",
                    "outcomeScenario": "open_15m",
                    "executionProfileVersion": "execution-v1",
                    "comparisonKey": "selection_policy_vs_market_gate",
                    "holdingCount": 1,
                    "stockOutcomeCount": 1,
                    "cashSlotCount": 2,
                    "coveragePct": 100,
                    "treatmentNetReturnPct": 0.1,
                    "controlNetReturnPct": -0.1,
                    "pairedDailyDeltaPct": 0.2,
                    "status": "ok",
                    "sourceRevision": "revision-1",
                    "calculationVersion": "test",
                }
            )
        self.service.freeze_due_reports()
        reports = self.database.list_t1_shadow_evaluation_reports(
            shadow_family="policy_experiment", shadow_role="policy_market_gate",
            strategy_policy_version="policy-report-v1", window_type="20d",
        )
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["reportRevision"], 1)
        self.assertEqual(reports[0]["metrics"]["calculationVersion"], "t1_shadow_analytics_v6")
        self.assertFalse(reports[0]["admission"]["automaticPromotion"])

        revised = self.database.list_t1_shadow_strategy_daily_facts(
            shadow_family="policy_experiment", shadow_role="policy_market_gate",
            strategy_policy_version="policy-report-v1",
        )[0]
        revised.update({"sourceRevision": "revision-2", "treatmentNetReturnPct": 0.2, "detail": {}})
        self.database.upsert_t1_shadow_strategy_daily_fact(revised)
        self.service.freeze_due_reports()
        reports = self.database.list_t1_shadow_evaluation_reports(
            shadow_family="policy_experiment", shadow_role="policy_market_gate",
            strategy_policy_version="policy-report-v1", window_type="20d",
        )
        self.assertEqual([item["reportRevision"] for item in reports], [2, 1])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE t1_overnight_shadow_evaluation_reports SET review_status='changed' WHERE id=?",
                    (reports[0]["id"],),
                )

    def test_execution_profiles_are_never_mixed_in_default_summary(self):
        for index, execution_version in enumerate(("execution-v1", "execution-v2"), start=1):
            self.database.upsert_t1_shadow_strategy_daily_fact(
                {
                    "recommendationDate": f"2026-10-{index:02d}",
                    "canonicalRunId": index,
                    "shadowFamily": "policy_experiment",
                    "shadowRole": "policy_market_gate",
                    "scope": "market_gate",
                    "strategyPolicyVersion": "policy-shared",
                    "outcomeScenario": "open_15m",
                    "executionProfileVersion": execution_version,
                    "comparisonKey": "selection_policy_vs_market_gate",
                    "holdingCount": 1,
                    "stockOutcomeCount": 1,
                    "coveragePct": 100,
                    "treatmentNetReturnPct": float(index),
                    "controlNetReturnPct": 0,
                    "pairedDailyDeltaPct": float(index),
                    "status": "ok",
                    "sourceRevision": f"revision-{index}",
                }
            )
        result = self.service.summary(
            shadow_family="policy_experiment",
            shadow_role="policy_market_gate",
            strategy_policy_version="policy-shared",
        )
        self.assertEqual(len(result["roles"]), 2)
        self.assertEqual(
            {item["executionProfileVersion"]: item["effectiveDays"] for item in result["roles"]},
            {"execution-v1": 1, "execution-v2": 1},
        )

    def test_recent_window_counts_effective_days_and_keeps_missing_days(self):
        facts = [
            {
                "id": index,
                "recommendationDate": f"2026-09-{index:02d}",
                "canonicalRunId": index,
                "treatmentNetReturnPct": None if index in {2, 7, 12, 17, 22} else 0.1,
            }
            for index in range(1, 26)
        ]
        selected = _select_window(facts, "20d")
        self.assertEqual(len([item for item in selected if item["treatmentNetReturnPct"] is not None]), 20)
        self.assertEqual(len(selected), 25)

    def test_confirmation_window_does_not_fall_back_to_all_facts(self):
        for index in range(1, 11):
            self.database.upsert_t1_shadow_strategy_daily_fact(
                {
                    "recommendationDate": f"2026-10-{index:02d}",
                    "canonicalRunId": index,
                    "shadowFamily": "policy_experiment",
                    "shadowRole": "policy_market_gate",
                    "scope": "market_gate",
                    "strategyPolicyVersion": "policy-window-v1",
                    "outcomeScenario": "open_15m",
                    "executionProfileVersion": "execution-v1",
                    "comparisonKey": "selection_policy_vs_market_gate",
                    "holdingCount": 1,
                    "stockOutcomeCount": 1,
                    "coveragePct": 100,
                    "treatmentNetReturnPct": 0.1,
                    "controlNetReturnPct": 0,
                    "pairedDailyDeltaPct": 0.1,
                    "status": "ok",
                    "sourceRevision": f"revision-{index}",
                }
            )
        result = self.service.summary(
            shadow_family="policy_experiment",
            shadow_role="policy_market_gate",
            strategy_policy_version="policy-window-v1",
            window="confirmation60",
        )
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["roles"][0]["effectiveDays"], 0)

    def test_empty_policy_experiments_are_not_reported_as_zero_return(self):
        self._run(
            "2026-10-14",
            {
                "strategyPolicy": {
                    "strategyPolicyVersion": "policy-empty-v1",
                    "executionProfileVersion": "execution-v1",
                    "portfolioDiversityAudit": {"secondSlotOpen": False},
                },
                "baselineComparison": {
                    "selectionPolicyTreatmentAdaptiveExit": {
                        "status": "ok", "count": 0, "tradeableCount": 0, "portfolioReturnPct": 0,
                    },
                    "selectionPolicyTreatment": {
                        "status": "data_insufficient", "count": 0, "tradeableCount": 0, "portfolioReturnPct": 0,
                    },
                    "portfolioDiversityTreatment": {
                        "status": "ok", "count": 0, "tradeableCount": 0, "portfolioReturnPct": 0,
                    },
                    "portfolioDiversityControl": {
                        "status": "data_insufficient", "count": 0, "tradeableCount": 0, "portfolioReturnPct": 0,
                    },
                },
            },
        )
        for role in ("policy_adaptive_exit", "policy_diversity"):
            result = self.service.summary(
                shadow_family="policy_experiment",
                shadow_role=role,
                strategy_policy_version="policy-empty-v1",
            )
            self.assertEqual(result["roles"][0]["effectiveDays"], 0)
            self.assertEqual(result["facts"][0]["status"], "not_applicable")
            self.assertIsNone(result["facts"][0]["treatmentNetReturnPct"])

    def test_comparative_control_curve_uses_only_same_day_pairs(self):
        rows = [
            (1, 1.0, 0.0, 1.0, "ok"),
            (2, None, 10.0, None, "data_insufficient"),
        ]
        for run_id, treatment, control, delta, status in rows:
            self.database.upsert_t1_shadow_strategy_daily_fact(
                {
                    "recommendationDate": f"2026-10-{run_id:02d}",
                    "canonicalRunId": run_id,
                    "shadowFamily": "policy_experiment",
                    "shadowRole": "policy_market_gate",
                    "scope": "market_gate",
                    "strategyPolicyVersion": "policy-paired-v1",
                    "outcomeScenario": "open_15m",
                    "executionProfileVersion": "execution-v1",
                    "comparisonKey": "selection_policy_vs_market_gate",
                    "holdingCount": 1,
                    "stockOutcomeCount": 1,
                    "coveragePct": 100,
                    "treatmentNetReturnPct": treatment,
                    "controlNetReturnPct": control,
                    "pairedDailyDeltaPct": delta,
                    "status": status,
                    "sourceRevision": f"revision-{run_id}",
                }
            )
        report = self.service.summary(
            shadow_family="policy_experiment",
            shadow_role="policy_market_gate",
            strategy_policy_version="policy-paired-v1",
        )["roles"][0]
        self.assertEqual(report["effectiveDays"], 1)
        self.assertEqual(report["controlAvgDailyNetReturnPct"], 0.0)
        self.assertEqual(report["avgPairedDeltaPct"], 1.0)

    def test_holding_win_rate_excludes_active_cash_days(self):
        rows = [
            (1, 0, 0.0, -1.0, 1.0, "active_cash"),
            (2, 1, 1.0, 0.0, 1.0, "ok"),
        ]
        for run_id, holdings, treatment, control, delta, status in rows:
            self.database.upsert_t1_shadow_strategy_daily_fact(
                {
                    "recommendationDate": f"2026-10-{run_id:02d}",
                    "canonicalRunId": run_id,
                    "shadowFamily": "policy_experiment",
                    "shadowRole": "policy_market_gate",
                    "scope": "market_gate",
                    "strategyPolicyVersion": "policy-cash-v1",
                    "outcomeScenario": "open_15m",
                    "executionProfileVersion": "execution-v1",
                    "comparisonKey": "selection_policy_vs_market_gate",
                    "holdingCount": holdings,
                    "stockOutcomeCount": holdings,
                    "coveragePct": 100 if holdings else None,
                    "treatmentNetReturnPct": treatment,
                    "controlNetReturnPct": control,
                    "pairedDailyDeltaPct": delta,
                    "status": status,
                    "sourceRevision": f"revision-{run_id}",
                }
            )
        report = self.service.summary(
            shadow_family="policy_experiment",
            shadow_role="policy_market_gate",
            strategy_policy_version="policy-cash-v1",
        )["roles"][0]
        self.assertEqual(report["positiveHoldingDayRate"], 100.0)
        self.assertEqual(report["positiveEffectiveDayRate"], 50.0)
        self.assertEqual(report["activeCashDays"], 1)

    def test_v3_missing_control_is_not_assumed_to_be_official_cash(self):
        strategy = {
            "pipelineVersion": "2026-11-06.t1_v3_shadow.v1",
            "decisionStatus": "active_top0",
            "holdingCount": 0,
            "cashSlotCount": 2,
        }
        baseline = {"v3Main": {}, "final": {}}
        missing_control = _v3_main_fact(
            {"id": 1, "recommendationDate": "2026-10-15", "selectionMode": "ai_t1_overnight", "finalCount": 1},
            strategy,
            baseline,
        )
        self.assertIsNone(missing_control["controlNetReturnPct"])
        self.assertEqual(missing_control["status"], "data_insufficient")
        active_cash_control = _v3_main_fact(
            {"id": 2, "recommendationDate": "2026-10-16", "selectionMode": "market_risk_zero_pick", "finalCount": 0},
            strategy,
            baseline,
        )
        self.assertEqual(active_cash_control["controlNetReturnPct"], 0.0)
        self.assertEqual(active_cash_control["status"], "active_cash")

    def test_empty_v3_summary_uses_v3_version_identity(self):
        result = self.service.summary(shadow_family="v3_main_experiment", shadow_role="v3_main")
        self.assertEqual(result["roles"][0]["strategyPolicyVersion"], "2026-11-06.t1_v3_shadow.v1")

    def test_empty_adaptive_scenario_is_not_applicable_at_source(self):
        result = selection._scenario_baseline_comparison([], "adaptive_exit_v1")
        self.assertEqual(result["status"], "not_applicable")
        self.assertIsNone(result["portfolioReturnPct"])

    def test_coverage_includes_incomplete_holding_days(self):
        for run_id, status, outcomes, missing, value in (
            (1, "ok", 1, 0, 0.5),
            (2, "data_insufficient", 0, 1, None),
        ):
            self.database.upsert_t1_shadow_strategy_daily_fact({
                "recommendationDate": f"2026-10-{run_id:02d}",
                "canonicalRunId": run_id,
                "shadowFamily": "legacy_tail_rule",
                "shadowRole": "tail_rule",
                "scope": "role_diagnostic",
                "strategyPolicyVersion": "coverage-v1",
                "outcomeScenario": "open_15m",
                "executionProfileVersion": "execution-v1",
                "comparisonKey": "tail_rule_descriptive",
                "holdingCount": 1,
                "stockOutcomeCount": outcomes,
                "missingCount": missing,
                "treatmentNetReturnPct": value,
                "status": status,
                "sourceRevision": f"r-{run_id}",
            })
        report = self.service.summary(
            shadow_family="legacy_tail_rule",
            shadow_role="tail_rule",
            strategy_policy_version="coverage-v1",
        )["roles"][0]
        self.assertEqual(report["effectiveDays"], 1)
        self.assertEqual(report["stockOutcomeCount"], 1)
        self.assertEqual(report["missingCount"], 1)
        self.assertEqual(report["coveragePct"], 50.0)

    def test_legacy_tail_identity_is_not_split_by_formal_policy_version(self):
        for day, policy in (("2026-10-01", "formal-v1"), ("2026-10-02", "formal-v2")):
            self._run(day, {
                "selectionPolicyVersion": policy,
                "shadowTop3": {"picks": [{"code": "000001"}]},
                "shadowComparison": {"outcomeComparison": {"shadow": {
                    "status": "ok", "count": 1, "tradeableCount": 1, "portfolioReturnPct": 0.2,
                }}},
            }, rebuild=False)
        self.service.rebuild()
        result = self.service.summary(shadow_family="legacy_tail_rule", shadow_role="tail_rule")
        self.assertEqual(len(result["roles"]), 1)
        self.assertEqual(result["roles"][0]["targetTradeDays"], 2)
        self.assertEqual(result["roles"][0]["strategyPolicyVersion"], "legacy_shadow_open15m_v1")

    def test_legacy_ai_keeps_active_cash_technical_failure_and_not_applicable_days(self):
        self._run("2026-10-01", {
            "shadowDeepSeekTailTop3": {"picks": [], "reason": "DeepSeek 尾盘影子返回 Top0"},
        }, rebuild=False)
        self._run("2026-10-02", {
            "shadowDeepSeekTailTop3": {"picks": [], "error": "AI 上下文超过预算"},
        }, mode="ai_technical_failure_zero_pick", rebuild=False)
        self._run("2026-10-03", {}, rebuild=False)
        self._run("2026-10-04", {
            "legacyShadowArchive": {"status": "future_generation_stopped", "archivedRoles": ["tail_ai", "rules"]},
            "shadowDeepSeekTailTop3": {"picks": [], "reason": "V3 上线后不再生成独立尾盘 Top3"},
        }, mode="ai_technical_failure_zero_pick", rebuild=False)
        self.service.rebuild()
        result = self.service.summary(shadow_family="legacy_tail_ai", shadow_role="tail_ai")
        statuses = [fact["status"] for fact in result["facts"]]
        self.assertEqual(statuses, ["active_cash", "technical_failure", "not_applicable", "not_applicable"])
        report = result["roles"][0]
        self.assertEqual(report["activeCashDays"], 1)
        self.assertEqual(report["technicalFailureDays"], 1)
        self.assertEqual(report["notApplicableDays"], 2)
        self.assertEqual(report["effectiveDays"], 1)

    def test_legacy_incomplete_outcomes_are_not_filled_with_fake_cash_returns(self):
        self._run("2026-10-05", {
            "shadowDeepSeekTailTop3": {
                "picks": [{"code": "000001"}, {"code": "000002"}, {"code": "000003"}],
            },
            "shadowComparison": {"outcomeComparison": {
                "deepSeekTail": {
                    "status": "ok",
                    "count": 3,
                    "tradeableCount": 1,
                    "portfolioReturnPct": 0.5,
                    "slotCount": 3,
                    "cashSlots": 2,
                },
                "deepSeekTailRows": [
                    {"code": "000001", "dataStatus": "ok"},
                    {"code": "000002", "dataStatus": "minute_missing"},
                    {"code": "000003", "dataStatus": "minute_missing"},
                ],
            }},
        })
        result = self.service.summary(shadow_family="legacy_tail_ai", shadow_role="tail_ai")
        fact = result["facts"][0]
        report = result["roles"][0]
        self.assertEqual(fact["status"], "data_insufficient")
        self.assertIsNone(fact["treatmentNetReturnPct"])
        self.assertEqual(fact["holdingCount"], 3)
        self.assertEqual(fact["cashSlotCount"], 0)
        self.assertEqual(fact["missingCount"], 2)
        self.assertEqual(report["effectiveDays"], 0)
        self.assertEqual(report["coveragePct"], 33.33)

    def test_legacy_ai_explicit_top0_is_not_overridden_by_formal_selector_failure(self):
        self._run("2026-10-06", {
            "shadowDeepSeekTailTop3": {
                "status": "ai_zero_pick",
                "picks": [],
                "reason": "DeepSeek 尾盘影子返回 Top0，表示它认为没有足够合格的尾盘影子候选。",
            },
        }, mode="ai_technical_failure_zero_pick")
        result = self.service.summary(shadow_family="legacy_tail_ai", shadow_role="tail_ai")
        fact = result["facts"][0]
        self.assertEqual(fact["status"], "active_cash")
        self.assertEqual(fact["treatmentNetReturnPct"], 0.0)

    def test_missed_profit_requires_profitable_control(self):
        market = {
            "strategyPolicy": {"strategyPolicyVersion": "policy-v1", "rank3Audit": {"available": True}},
            "baselineComparison": {
                "rank3": {"count": 3, "tradeableCount": 3, "portfolioReturnPct": -1.3,
                          "componentRows": [{"netReturnPct": -1}, {"netReturnPct": -1}, {"netReturnPct": -1.9}]},
                "rank3CashControl": {"count": 2, "tradeableCount": 2, "portfolioReturnPct": -1.0,
                                     "componentRows": [{"netReturnPct": -1}, {"netReturnPct": -1}]},
            },
        }
        self._run("2026-10-04", market)
        row = self.database.list_t1_shadow_strategy_daily_facts(
            shadow_family="policy_experiment", shadow_role="policy_rank3"
        )[0]
        self.assertEqual(row["missedProfitPct"], 0.0)
        self.assertEqual(row["avoidedLossPct"], 0.0)

    def test_technical_failure_zero_pick_is_research(self):
        run = {
            "status": "done",
            "selectionMode": "ai_technical_failure_zero_pick",
            "request": {"runRole": "official", "poolSize": 500},
            "market": {"runRole": "official"},
            "finalCount": 0,
            "finalPicks": [],
        }
        self.assertEqual(selection._run_role(run), "research")
        self.assertTrue(selection._is_zero_pick_run(run))


if __name__ == "__main__":
    unittest.main()
