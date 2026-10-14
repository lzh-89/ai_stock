import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from backend.app.services import t1_overnight_selection_service as svc
from backend.app.database import Database
from backend.app.providers import minute_kline_provider as minute_provider_module
from backend.app.providers.minute_kline_provider import MinuteKlineProvider


class T1OvernightSemanticsTest(unittest.TestCase):
    def _prompt_payload(self, prompt):
        marker = "客观复盘只描述事实；只有 confirmedStrategyConstraints 是用户已确认的策略约束。\n"
        return json.loads(prompt.split(marker, 1)[1])

    def test_weekend_trade_date_blocks_official(self):
        status = svc._t1_trade_date_status("2026-08-02", "14:40")
        self.assertEqual(status["status"], "non_trading_weekend")
        self.assertEqual(status["eligibility"], "official_blocked")
        self.assertTrue(svc._t1_trade_date_blocks_official(status))

    def test_weekday_trade_date_allows_official(self):
        status = svc._t1_trade_date_status("2026-08-03", "14:40")
        self.assertEqual(status["eligibility"], "official_allowed")
        self.assertFalse(svc._t1_trade_date_blocks_official(status))

    def test_non_trading_run_is_diagnostic_even_with_tradeable_result(self):
        sample = svc._review_sample_meta(
            run={"recommendationDate": "2026-08-02", "scanTime": "14:40", "status": "done"},
            outcome_status="verified",
            run_role="research",
            tradeable_count=1,
            outcome_count=3,
            learning_eligible_count=0,
        )
        display = svc._review_display_meta({}, sample, "verified", "research")
        self.assertEqual(sample["sampleClass"], "diagnostic")
        self.assertEqual(display["reviewStatus"], "diagnostic")
        self.assertEqual(display["reviewStatusLabel"], "仅排查")

    def test_partial_tradeable_run_is_not_learning(self):
        sample = svc._review_sample_meta(
            run={"recommendationDate": "2026-08-03", "scanTime": "14:40", "status": "done"},
            outcome_status="verified",
            run_role="official",
            tradeable_count=1,
            outcome_count=3,
            learning_eligible_count=0,
        )
        display = svc._review_display_meta({}, sample, "verified", "official")
        self.assertEqual(sample["sampleClass"], "partial")
        self.assertEqual(display["reviewStatus"], "partial")
        self.assertEqual(display["reviewStatusLabel"], "部分缺数据")

    def test_data_insufficient_official_run_is_partial_not_diagnostic(self):
        sample = svc._review_sample_meta(
            run={"recommendationDate": "2026-08-03", "scanTime": "14:40", "status": "done"},
            outcome_status="data_insufficient",
            run_role="official",
            tradeable_count=0,
            outcome_count=3,
            learning_eligible_count=0,
        )
        display = svc._review_display_meta({}, sample, "data_insufficient", "official")
        self.assertEqual(sample["sampleClass"], "data_insufficient")
        self.assertEqual(display["reviewStatus"], "partial")
        self.assertEqual(display["reviewStatusLabel"], "部分缺数据")

    def test_review_usage_explanation_translates_research_fallback_reasons(self):
        sample = {"sampleClass": "usable", "sampleLabel": "可看结果", "sampleReason": "研究结果不进入优化。"}
        display = {"reviewStatus": "reviewable", "reviewStatusLabel": "可看结果", "reviewStatusReason": "研究结果不进入优化。"}
        ai_decision = svc._review_ai_decision_meta({"selectionMode": "rules_fallback"})
        learning_scope = svc._review_learning_scope_meta(display, ai_decision, 0)
        explanation = svc._review_usage_explanation(
            run={
                "recommendationDate": "2026-08-04",
                "scanTime": "14:40",
                "selectionMode": "rules_fallback",
                "finalCount": 3,
                "request": {"runRole": "research", "force": True},
            },
            outcomes=[
                {
                    "code": "000981",
                    "outcome": {
                        "learningEligible": False,
                        "exclusionReasons": ["验证源为缓存", "同口径基准不可用"],
                    },
                }
            ],
            tradeable_summary={"tradeableCount": 3},
            sample=sample,
            display=display,
            ai_decision=ai_decision,
            learning_scope=learning_scope,
            outcome_status="verified",
            run_role="research",
            learning_eligible_count=0,
        )
        text = "；".join(explanation["reasons"])
        self.assertFalse(explanation["canOptimize"])
        self.assertIn("手动强制重跑", text)
        self.assertIn("规则兜底", text)
        self.assertIn("缓存数据", text)
        self.assertIn("市场基准", text)

    def test_review_usage_explanation_names_missing_post_publish_entry_evidence(self):
        reasons = svc._review_outcome_exclusion_reasons(
            [
                {
                    "outcome": {
                        "learningEligible": False,
                        "exclusionReasons": [
                            "没有推荐日信号发布后的分钟成交，只能用冻结价近似，不能作为最高等级成交证据。"
                        ],
                        "tradeable": {"primaryScenario": {"dataStatus": "ok"}},
                    }
                }
            ]
        )
        self.assertTrue(any("正式推荐发布后的分钟成交证据" in reason for reason in reasons))

    def test_post_publish_minute_produces_strict_entry_execution(self):
        result = svc._simulated_entry_execution(
            run={
                "recommendationDate": "2026-10-14",
                "scanTime": "14:40",
                "market": {"decisionTiming": {"signalPublishedAt": "2026-10-14T14:41:00"}},
            },
            buy_reference=10.0,
            recommendation_minute_series=[
                {"time": "2026-10-14 14:42", "open": 10.1, "close": 10.2, "volume": 5000}
            ],
        )
        self.assertEqual(result["status"], "executed_from_minute")
        self.assertTrue(result["executable"])

    def test_legacy_recommendation_snapshot_cannot_become_strict_entry_evidence(self):
        outcome = svc._build_outcome(
            run={
                "id": 94,
                "recommendationDate": "2026-10-12",
                "scanTime": "14:40",
                "market": {"decisionTiming": {"signalPublishedAt": "2026-10-12T14:40:12"}},
            },
            pick={"code": "000001", "name": "测试股", "price": 10.0, "dataQuality": {"grade": "A"}},
            series=[
                {"date": "2026-10-12", "open": 9.8, "high": 10.1, "low": 9.7, "close": 10.0},
                {"date": "2026-10-13", "open": 10.1, "high": 10.3, "low": 9.9, "close": 10.2},
            ],
            minute_series=[
                {"time": "2026-10-13 09:45", "open": 10.1, "high": 10.2, "low": 10.0, "close": 10.15}
            ],
            data_source="daily_test",
            minute_source="minute_test",
            verified_at="2026-10-13T15:10:00",
            recommendation_minute_series=[
                {"time": "2026-10-12 14:40", "open": 9.98, "close": 10.0, "volume": 5000}
            ],
            entry_execution_minute_series=[],
        )
        self.assertEqual(outcome["entryExecution"]["status"], "reference_price_without_minute")
        self.assertFalse(outcome["learningEligible"])

    def test_dedicated_post_publish_snapshot_can_become_strict_entry_evidence(self):
        post_publish = [
            {"time": "2026-10-12 14:42", "open": 10.02, "close": 10.03, "volume": 5000}
        ]
        outcome = svc._build_outcome(
            run={
                "id": 95,
                "recommendationDate": "2026-10-12",
                "scanTime": "14:40",
                "market": {"decisionTiming": {"signalPublishedAt": "2026-10-12T14:41:00"}},
            },
            pick={"code": "000001", "name": "测试股", "price": 10.0, "dataQuality": {"grade": "A"}},
            series=[
                {"date": "2026-10-12", "open": 9.8, "high": 10.1, "low": 9.7, "close": 10.0},
                {"date": "2026-10-13", "open": 10.1, "high": 10.3, "low": 9.9, "close": 10.2},
            ],
            minute_series=[
                {"time": "2026-10-13 09:45", "open": 10.1, "high": 10.2, "low": 10.0, "close": 10.15}
            ],
            data_source="daily_test",
            minute_source="minute_test",
            verified_at="2026-10-13T15:10:00",
            recommendation_minute_series=post_publish,
            entry_execution_minute_series=post_publish,
        )
        self.assertEqual(outcome["entryExecution"]["status"], "executed_from_minute")
        self.assertTrue(outcome["learningEligible"])

    def test_learnable_ai_semifinal_rules_final_keeps_ai_scope_separate(self):
        sample = {
            "sampleClass": "learning",
            "sampleLabel": "可用于优化",
            "sampleReason": "这是正式交易日推荐，次日 15 分钟价格完整，结果可以用于后续优化策略。",
        }
        display = svc._review_display_meta({"selectionMode": "ai_semifinal_rules_final"}, sample, "verified", "official")
        ai_decision = svc._review_ai_decision_meta({"selectionMode": "ai_semifinal_rules_final"})
        learning_scope = svc._review_learning_scope_meta(display, ai_decision, 3)
        explanation = svc._review_usage_explanation(
            run={
                "recommendationDate": "2026-08-06",
                "scanTime": "14:40",
                "selectionMode": "ai_semifinal_rules_final",
                "finalCount": 3,
            },
            outcomes=[],
            tradeable_summary={"tradeableCount": 3},
            sample=sample,
            display=display,
            ai_decision=ai_decision,
            learning_scope=learning_scope,
            outcome_status="verified",
            run_role="official",
            learning_eligible_count=3,
        )
        text = "；".join(explanation["reasons"])
        self.assertTrue(explanation["canOptimize"])
        self.assertEqual(ai_decision["status"], "ai_semifinal_rules_final")
        self.assertEqual(learning_scope["scope"], "candidate_rules_only")
        self.assertIn("不作为 DeepSeek 终选成功样本", text)

    def test_sector_state_shadow_retreat_is_generic(self):
        payload = svc._classify_t1_sector_state_shadow(
            {
                "sectorType": "industry",
                "sectorName": "测试行业",
                "changePct": -2.8,
                "upCount": 2,
                "downCount": 8,
                "limitDownCount": 1,
                "sampleCount": 10,
                "snapshotScope": "full_market",
                "coverageStatus": "available",
            },
            {},
        )
        self.assertEqual(payload["state"], "retreat")
        self.assertGreater(payload["riskScore"], 70)

    def test_ai_validation_rejects_sector_state_without_s2(self):
        candidate = {
            "code": "600000",
            "industrySector": "银行",
            "conceptSectors": [],
            "sectorEvidenceStatus": "partial",
            "sectorStateShadow": "data_insufficient",
            "sectorEvidence": {
                "evidenceLayers": {
                    "identity": {"status": "available"},
                    "sectorSnapshot": {"status": "unavailable"},
                    "constituentRank": {"status": "unavailable"},
                }
            },
            "evidenceRefs": [{"id": "Q1"}, {"id": "M1"}, {"id": "S1"}, {"id": "S2"}, {"id": "R1"}],
        }
        validation = svc._validate_ai_picks_evidence(
            [
                {
                    "code": "600000",
                    "rank": 1,
                    "reason": "银行行业退潮后出现修复，适合观察。",
                    "supportingEvidenceRefs": ["Q1"],
                    "counterEvidenceRefs": [],
                }
            ],
            [candidate],
            "batch",
        )
        self.assertFalse(validation["valid"])
        self.assertTrue(any("S2" in error or "状态证据不足" in error for error in validation["errors"]))

    def test_structured_ai_selection_slip_passes_without_free_text(self):
        candidate = {
            "code": "600000",
            "industrySector": "银行",
            "conceptSectors": ["高股息"],
            "sectorEvidenceStatus": "partial",
            "sectorEvidence": {
                "evidenceLayers": {
                    "identity": {"status": "available"},
                    "sectorSnapshot": {"status": "unavailable"},
                    "constituentRank": {"status": "unavailable"},
                }
            },
            "evidenceRefs": [
                {"id": "Q1", "status": "available", "summary": "涨跌幅 1.2%，量比 1.3。"},
                {"id": "M1", "status": "available", "summary": "市场震荡。"},
                {"id": "S1", "status": "partial", "summary": "仅行业身份可用。"},
                {"id": "S2", "status": "unavailable", "summary": "板块状态证据不足。"},
                {"id": "R1", "status": "estimated", "summary": "隔夜风险 32。"},
            ],
        }
        validation = svc._validate_ai_picks_evidence(
            [
                {
                    "code": "600000",
                    "rank": 1,
                    "supportingEvidenceRefs": ["Q1", "M1"],
                    "counterEvidenceRefs": ["R1"],
                    "decisionTags": ["premium_potential", "low_overnight_risk"],
                    "confidence": 62,
                }
            ],
            [candidate],
            "final",
        )
        self.assertTrue(validation["valid"], validation["errors"])

    def test_structured_ai_selection_slip_rejects_tail_support_without_t1(self):
        candidate = {
            "code": "600000",
            "industrySector": "银行",
            "conceptSectors": [],
            "sectorEvidence": {"evidenceLayers": {}},
            "evidenceRefs": [{"id": "Q1", "status": "available"}, {"id": "M1", "status": "available"}, {"id": "R1", "status": "estimated"}],
        }
        validation = svc._validate_ai_picks_evidence(
            [
                {
                    "code": "600000",
                    "rank": 1,
                    "supportingEvidenceRefs": ["Q1", "M1"],
                    "counterEvidenceRefs": ["R1"],
                    "decisionTags": ["tail_support"],
                    "confidence": 60,
                }
            ],
            [candidate],
            "final",
        )
        self.assertFalse(validation["valid"])
        self.assertTrue(any("T1" in error and "tail_support" in error for error in validation["errors"]))

    def test_structured_ai_selection_slip_allows_tail_support_with_t1(self):
        candidate = {
            "code": "600000",
            "industrySector": "银行",
            "conceptSectors": [],
            "sectorEvidence": {"evidenceLayers": {}},
            "evidenceRefs": [
                {"id": "Q1", "status": "available"},
                {"id": "M1", "status": "available"},
                {"id": "T1", "status": "available"},
                {"id": "R1", "status": "estimated"},
            ],
        }
        validation = svc._validate_ai_picks_evidence(
            [
                {
                    "code": "600000",
                    "rank": 1,
                    "supportingEvidenceRefs": ["Q1", "T1"],
                    "counterEvidenceRefs": ["R1"],
                    "decisionTags": ["tail_support"],
                    "confidence": 60,
                }
            ],
            [candidate],
            "final",
        )
        self.assertTrue(validation["valid"], validation["errors"])

    def test_structured_ai_selection_slip_rejects_unknown_evidence_ref(self):
        candidate = {
            "code": "600000",
            "industrySector": "银行",
            "conceptSectors": [],
            "sectorEvidence": {"evidenceLayers": {}},
            "evidenceRefs": [{"id": "Q1"}, {"id": "M1"}, {"id": "R1"}],
        }
        validation = svc._validate_ai_picks_evidence(
            [
                {
                    "code": "600000",
                    "rank": 1,
                    "supportingEvidenceRefs": ["Q1", "S9"],
                    "counterEvidenceRefs": [],
                    "decisionTags": ["tail_support"],
                    "confidence": 60,
                }
            ],
            [candidate],
            "final",
        )
        self.assertFalse(validation["valid"])
        self.assertTrue(any("不存在的证据ID" in error for error in validation["errors"]))

    def test_empty_ai_picks_is_valid_zero_pick(self):
        validation = svc._validate_ai_picks_evidence([], [], "final")
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["status"], "zero_pick")
        self.assertTrue(validation["zeroPick"])

    def test_structured_ai_selection_slip_rejects_sector_state_tag_without_available_s2(self):
        candidate = {
            "code": "600000",
            "industrySector": "银行",
            "conceptSectors": [],
            "sectorStateShadow": "data_insufficient",
            "sectorEvidence": {
                "evidenceLayers": {
                    "identity": {"status": "available"},
                    "sectorSnapshot": {"status": "unavailable"},
                    "constituentRank": {"status": "unavailable"},
                }
            },
            "evidenceRefs": [
                {"id": "Q1", "status": "available"},
                {"id": "M1", "status": "available"},
                {"id": "S2", "status": "unavailable"},
                {"id": "R1", "status": "estimated"},
            ],
        }
        validation = svc._validate_ai_picks_evidence(
            [
                {
                    "code": "600000",
                    "rank": 1,
                    "supportingEvidenceRefs": ["Q1", "M1"],
                    "counterEvidenceRefs": ["R1"],
                    "decisionTags": ["tail_support", "sector_state_supported"],
                    "confidence": 60,
                }
            ],
            [candidate],
            "final",
        )
        self.assertFalse(validation["valid"])
        self.assertTrue(any("sector_state_supported" in error or "S2" in error for error in validation["errors"]))

    def test_apply_picks_generates_display_text_from_evidence(self):
        candidate = {
            "code": "600000",
            "name": "测试银行",
            "industrySector": "银行",
            "conceptSectors": ["高股息"],
            "sectorEvidenceStatus": "partial",
            "sectorEvidence": {
                "evidenceLayers": {
                    "identity": {"status": "available"},
                    "sectorSnapshot": {"status": "unavailable"},
                    "constituentRank": {"status": "unavailable"},
                }
            },
            "evidenceRefs": [
                {"id": "Q1", "status": "available", "summary": "实时快照可用。"},
                {"id": "M1", "status": "available", "summary": "市场震荡。"},
                {"id": "R1", "status": "estimated", "summary": "隔夜风险 32。"},
            ],
            "t1Dimensions": {"overnightRisk": 32, "overnightRiskStatus": "estimated"},
            "tradeability": {"level": "normal"},
        }
        selected = svc._apply_picks(
            [candidate],
            [
                {
                    "code": "600000",
                    "rank": 1,
                    "supportingEvidenceRefs": ["Q1", "M1"],
                    "counterEvidenceRefs": ["R1"],
                    "decisionTags": ["premium_potential"],
                    "confidence": 62,
                }
            ],
            1,
            0,
        )
        self.assertEqual(len(selected), 1)
        pick = selected[0]
        self.assertEqual(pick["aiOutputProtocol"], "structured_selection_slip")
        self.assertIn("AI最终选择", pick["reason"])
        self.assertIn("价格和成交量", pick["reason"])
        self.assertIn("今天尾盘信号确认后再买", pick["buyPlan"])
        self.assertIn("下个交易日主要赚开盘溢价", pick["sellPlan"])
        self.assertIn("震荡分化", pick["marketFit"])
        self.assertEqual(pick["decisionTags"], ["premium_potential"])

    def test_overnight_risk_status_is_partial_when_only_late_session_metric_missing(self):
        sector_layers = {
            "identity": {"status": "available"},
            "sectorSnapshot": {"status": "available"},
            "constituentRank": {"status": "unavailable"},
        }
        status = svc._overnight_risk_status(
            state="mild_up",
            metrics={
                "downRatioPct": 51.4,
                "limitDown": 5,
                "nearLimitDown": 0,
                "mainIndexChangePct": 0.93,
                "averageIndexChangePct": 1.34,
                "turnoverRatio5d": 1.08,
            },
            missing_market=["14:30后指数变化"],
            sector_status="partial",
            sector_layers=sector_layers,
        )
        self.assertEqual(status, "partial")

    def test_overnight_risk_status_unknown_only_when_core_evidence_missing(self):
        status = svc._overnight_risk_status(
            state="unknown",
            metrics={},
            missing_market=["市场状态", "14:30后指数变化"],
            sector_status="unavailable",
            sector_layers={
                "identity": {"status": "unavailable"},
                "sectorSnapshot": {"status": "unavailable"},
                "constituentRank": {"status": "unavailable"},
            },
        )
        self.assertEqual(status, "unknown")

    def test_final_guardrails_keep_zero_pick_policy(self):
        picks = [
            {
                "code": "600000",
                "dataQuality": {"eligibleForRealTop": True},
                "riskProfile": {"key": "normal"},
            }
        ]
        guarded = svc._apply_final_guardrails(
            picks,
            {"recommendationPolicy": {"maxPickCount": 0}, "marketContext": {"state": "mixed"}},
            3,
        )
        self.assertEqual(guarded, [])

    def test_rules_fallback_still_filtered_by_guardrails(self):
        candidates = [
            {
                "code": "600000",
                "t1Score": 99,
                "dataQuality": {"eligibleForRealTop": True},
                "riskProfile": {"key": "avoid_overnight"},
            },
            {
                "code": "000001",
                "t1Score": 60,
                "dataQuality": {"eligibleForRealTop": True},
                "riskProfile": {"key": "normal"},
            },
        ]
        fallback = svc._rules_fallback(candidates, 3)
        guarded = svc._apply_final_guardrails(
            fallback,
            {"recommendationPolicy": {"maxPickCount": 3}, "marketContext": {"state": "mixed"}},
            3,
        )
        self.assertEqual([item["code"] for item in guarded], ["000001"])

    def test_tail_support_v2_marks_data_insufficient_for_sparse_tail(self):
        tail = svc._tail_support_shadow(
            {"relStrength": 80},
            [
                {"time": "2026-08-10 14:30:00", "open": 10, "close": 10.1, "volume": 100},
                {"time": "2026-08-10 14:31:00", "open": 10.1, "close": 10.2, "volume": 100},
            ],
            "14:40",
        )
        self.assertEqual(tail["version"], "v2_tail_support_shadow")
        self.assertEqual(tail["status"], "data_insufficient")

    def test_tail_support_v2_detects_late_dump(self):
        rows = []
        price = 10.0
        for minute in range(30, 41):
            price += 0.02 if minute < 36 else -0.08
            rows.append(
                {
                    "time": f"2026-08-10 14:{minute:02d}:00",
                    "open": price + 0.02,
                    "close": price,
                    "high": price + 0.04,
                    "low": price - 0.03,
                    "volume": 200 if minute >= 36 else 80,
                }
            )
        tail = svc._tail_support_shadow({"relStrength": 55, "change": 4}, rows, "14:40")
        self.assertEqual(tail["status"], "late_dump")
        self.assertGreaterEqual(tail["longestSellPressureCount"], 3)

    def test_tail_shadow_prompt_evidence_adds_t1_only_when_available(self):
        candidate = {
            "code": "600000",
            "evidenceRefs": [{"id": "Q1", "status": "available"}],
            "tailSupportShadow": {"status": "strong_support", "label": "尾盘承接强", "score": 72, "tailChangePct": 0.8},
            "gapDownRiskShadow": {"status": "low", "label": "低开风险低", "score": 25},
        }
        enriched = svc._with_tail_shadow_prompt_evidence(candidate)
        refs = {item["id"]: item for item in enriched["evidenceRefs"]}
        self.assertEqual(refs["T1"]["status"], "available")
        self.assertIn("tailSupportPackV2", enriched)

    def test_exit_timing_stats_reads_outcome_tradeable_scenarios(self):
        stats = svc._exit_timing_stats(
            [
                {
                    "code": "600000",
                    "excessReturnPct": 0.8,
                    "tradeable": {
                        "primaryScenario": {"scenarioKey": "open_15m", "dataStatus": "ok", "netReturnPct": 1.2},
                        "scenarios": [
                            {"scenarioKey": "open", "dataStatus": "ok", "netReturnPct": 0.4},
                            {"scenarioKey": "open_15m", "dataStatus": "ok", "netReturnPct": 1.2},
                        ],
                    },
                }
            ]
        )
        open_15m = next(item for item in stats if item["scenarioKey"] == "open_15m")
        self.assertEqual(open_15m["sampleCount"], 1)
        self.assertEqual(open_15m["avgNetReturnPct"], 1.2)

    def test_cautious_sector_text_is_not_unsupported_label(self):
        candidate = {
            "listingBoard": "沪市主板",
            "industrySector": "银行",
            "conceptSectors": [],
        }
        labels = svc._unsupported_sector_labels("无法确认今日板块强度，不构成正向行业证据。", candidate)
        self.assertEqual(labels, [])

    def test_stage2_auto_waits_for_samples(self):
        gate = svc._stage_gate("auto", 30, 30, official_days=12, tradeable_outcomes=18)
        self.assertTrue(gate["requested"])
        self.assertFalse(gate["effective"])
        self.assertIn("等待样本", gate["reason"])

    def test_stage2_auto_becomes_effective_after_gate(self):
        gate = svc._stage_gate("auto", 30, 30, official_days=30, tradeable_outcomes=35)
        self.assertTrue(gate["effective"])
        self.assertTrue(gate["productionEligible"])
        self.assertFalse(gate["experimental"])

    def test_experimental_stage_forces_research(self):
        controls = {
            "sectorRetreatDowngrade": {
                "effectiveEnabled": True,
                "experimental": True,
            }
        }
        self.assertTrue(svc._stage_controls_force_research(controls))

    def test_retreat_downgrade_preserves_exception_candidate(self):
        ordinary = {
            "sectorStateShadow": "retreat",
            "sectorExceptionEligible": False,
            "stockRankInIndustry": 12,
            "relStrength": 70,
            "amountFactor": 1.0,
            "tradeability": {"level": "normal"},
        }
        exception = {**ordinary, "sectorExceptionEligible": True}
        self.assertTrue(svc._sector_state_downgrade_applies(ordinary))
        self.assertFalse(svc._sector_state_downgrade_applies(exception))

    def test_batch_prompt_uses_layered_candidate_payload(self):
        candidate = {
            "code": "600000",
            "name": "测试银行",
            "listingBoard": "沪市主板",
            "industrySector": "银行",
            "conceptSectors": ["高股息"],
            "marketBoardLimitUpCount": 28,
            "marketBoardRank": 1,
            "sectorRank": 1,
            "sectorLimitUps": 28,
            "sectorEvidenceStatus": "partial",
            "sectorEvidence": {
                "status": "partial",
                "allowedClaims": ["所属行业/概念身份", "不得写正向板块强度", "不得写行业前排/个股排名"],
                "evidenceLayers": {
                    "identity": {"status": "available"},
                    "sectorSnapshot": {"status": "unavailable"},
                    "constituentRank": {"status": "unavailable"},
                },
                "matchedSectors": [],
                "sectorSnapshotSourceKind": "unknown",
                "sectorSnapshotScope": "unknown",
            },
            "evidenceRefs": [{"id": "Q1"}, {"id": "M1"}, {"id": "S1"}, {"id": "S2"}, {"id": "R1"}],
            "candidateType": "relative_strength_leader",
            "candidateTypeLabel": "个股相对强势",
            "candidateTypes": ["relative_strength_leader"],
            "poolRank": 1,
            "score": 70,
            "t1Score": 68,
            "relStrength": 88,
            "change": 1.2,
            "turnover": 120000000,
            "volumeRatio": 1.3,
            "amountFactor": 1.2,
            "t1Dimensions": {
                "fundStrength": 58,
                "sectorStrength": 34,
                "intradayStructure": 62,
                "premiumPotentialScore": 55,
                "premiumPotentialLabel": "次日溢价潜力评分，非概率",
                "overnightRisk": 32,
                "overnightRiskStatus": "estimated",
                "overnightRiskBreakdown": [],
            },
            "tradeability": {"level": "normal"},
            "dataQuality": {
                "grade": "B",
                "label": "真实但部分缺失",
                "quoteQuality": "A",
                "marketQuality": "A",
                "eventQuality": "C",
                "decisionEligibility": "partial",
                "eligibleForRealTop": True,
                "unavailableEvidence": ["sector"],
            },
        }
        prompt = svc._user_prompt(
            [candidate],
            {"stageControls": {"sectorStatePrompt": {"effectiveEnabled": False}, "styleRegimePrompt": {"effectiveEnabled": False}}},
            1,
            "batch",
        )
        payload = self._prompt_payload(prompt)
        packed = payload["candidates"][0]
        self.assertEqual(payload["promptContract"]["candidatePayload"], "batch_layered_minimal")
        self.assertIn("candidateCore", packed)
        self.assertIn("scorePack", packed)
        self.assertIn("sectorEvidencePack", packed)
        self.assertIn("boardProxy", packed)
        self.assertNotIn("sectorEvidence", packed)
        self.assertNotIn("sectorRank", packed)
        self.assertNotIn("sectorLimitUps", packed)
        self.assertEqual(packed["code"], "600000")
        self.assertEqual(packed["candidateCore"]["code"], "600000")
        self.assertEqual(packed["sectorEvidencePack"]["industrySector"], "银行")
        self.assertIn("不得写正向板块强度", packed["sectorEvidencePack"]["allowedClaims"])
        self.assertEqual(packed["boardProxy"]["semanticLevel"], "listing_board_proxy")
        self.assertNotIn("sectorState", packed["sectorEvidencePack"])
        refs = [item["id"] for item in packed["sectorEvidencePack"]["evidenceRefs"]]
        self.assertNotIn("S2", refs)

    def test_minute_rows_reuse_same_day_snapshots_and_retry_missing(self):
        class FakeMinuteProvider:
            source = "fake_minute"

            def __init__(self):
                self.calls = 0
                self.last_meta = {}
                self.last_warning = ""
                self.last_sources_by_code = {}

            def fetch_many(self, codes, period="15m"):
                self.calls += 1
                normalized = [str(code).zfill(6) for code in codes]
                result = {}
                for code in normalized:
                    if self.calls == 1 and code == "000002":
                        continue
                    result[code] = [
                        {"time": "09:30", "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0, "amount": 1.0}
                    ]
                self.last_sources_by_code = {code: self.source for code in result}
                self.last_warning = "first-pass-warning" if self.calls == 1 else ""
                self.last_meta = {
                    "source": self.source,
                    "requested": len(normalized),
                    "ok": len(result),
                    "failed": len(normalized) - len(result),
                    "period": period,
                    "elapsedMs": 1,
                    "warning": self.last_warning,
                }
                return result

            def source_for_code(self, code):
                return self.last_sources_by_code.get(str(code).zfill(6), self.source)

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "t1.sqlite")
            run_id = db.create_t1_overnight_evidence_run(
                {
                    "recommendationDate": "2026-08-05",
                    "scanTime": "14:40",
                    "requestedSource": "sina",
                    "actualSource": "sina",
                    "status": "done",
                    "marketSource": "sina",
                    "sectorSource": "sina",
                    "minuteSource": "fake_minute",
                    "qualityStatus": "B",
                    "missing": [],
                    "request": {"version": "v4_layered_sector_state_shadow"},
                    "market": {},
                    "error": "",
                    "startedAt": "2026-08-05T14:40:00",
                }
            )
            db.upsert_t1_overnight_minute_snapshots(
                run_id,
                "15m",
                [
                    {
                        "code": "600000",
                        "period": "15m",
                        "source": "reuse:tencent_minute",
                        "qualityStatus": "available",
                        "error": "",
                        "series": [{"time": "09:30", "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0, "amount": 1.0}],
                    }
                ],
            )
            provider = FakeMinuteProvider()
            rows = svc._fetch_t1_minute_rows_with_retries(
                db,
                provider,
                ["600000", "000001", "000002"],
                "15m",
                "2026-08-05",
                exclude_run_id=0,
            )
            self.assertEqual(set(rows.keys()), {"600000", "000001", "000002"})
            self.assertEqual(rows["600000"]["source"], "reuse:tencent_minute")
            self.assertEqual(rows["000002"]["source"], "fake_minute")
            self.assertGreaterEqual(provider.calls, 2)
            self.assertEqual(provider.last_meta["reusedCount"], 1)
            self.assertEqual(provider.last_meta["ok"], 3)
            self.assertEqual(provider.last_meta["failed"], 0)
            db.upsert_t1_overnight_minute_snapshots(run_id, "15m", list(rows.values()))
            latest = db.load_latest_t1_overnight_minute_snapshots("2026-08-05", ["600000", "000001", "000002"], "15m")
            self.assertEqual(set(latest.keys()), {"600000", "000001", "000002"})

    def test_minute_provider_require_dated_skips_undated_source(self):
        class UndatedProvider:
            source = "undated_minute"
            last_meta = {}
            last_warning = ""

            def fetch_many(self, codes, period="1m"):
                self.last_meta = {"source": self.source, "requested": len(codes), "ok": len(codes), "failed": 0}
                return {
                    str(code).zfill(6): [
                        {"time": "09:45", "open": 1.0, "close": 1.1, "high": 1.1, "low": 1.0, "volume": 1.0, "amount": 1.0}
                    ]
                    for code in codes
                }

        class DatedProvider:
            source = "dated_minute"
            last_meta = {}
            last_warning = ""

            def fetch_many(self, codes, period="1m"):
                self.last_meta = {"source": self.source, "requested": len(codes), "ok": len(codes), "failed": 0}
                return {
                    str(code).zfill(6): [
                        {"time": "2026-08-05 09:45:00", "open": 1.0, "close": 1.2, "high": 1.2, "low": 1.0, "volume": 1.0, "amount": 1.0}
                    ]
                    for code in codes
                }

        provider = MinuteKlineProvider()
        provider.providers = [UndatedProvider(), DatedProvider()]
        rows = provider.fetch_many(["600000"], period="1m", require_dated=True)
        self.assertEqual(rows["600000"][0]["time"], "2026-08-05 09:45:00")
        self.assertEqual(provider.source_for_code("600000"), "dated_minute")

    def test_minute_provider_target_date_continues_to_historical_source(self):
        class CurrentDayProvider:
            source = "current_day"
            last_meta = {}
            last_warning = ""

            def fetch_many(self, codes, period="1m"):
                return {
                    str(code).zfill(6): [{"time": "2026-08-06 15:00:00", "close": 1.1}]
                    for code in codes
                }

        class HistoricalProvider:
            source = "historical"
            last_meta = {}
            last_warning = ""

            def fetch_many(self, codes, period="1m"):
                return {
                    str(code).zfill(6): [{"time": "2026-08-05 15:00:00", "close": 1.2}]
                    for code in codes
                }

        provider = MinuteKlineProvider()
        provider.providers = [CurrentDayProvider(), HistoricalProvider()]
        rows = provider.fetch_many(
            ["600000"], period="1m", require_dated=True, target_trade_date="2026-08-05"
        )
        self.assertEqual(rows["600000"][0]["time"], "2026-08-05 15:00:00")
        self.assertEqual(provider.source_for_code("600000"), "historical")

    def test_t1_verification_filters_undated_minute_rows(self):
        rows = svc._dated_minute_series_by_code(
            {
                "600000": [{"time": "09:45", "open": 1.0, "close": 1.1}],
                "000001": [{"time": "2026-08-05 09:45:00", "open": 1.0, "close": 1.2}],
            }
        )
        self.assertNotIn("600000", rows)
        self.assertIn("000001", rows)

    def test_validation_snapshot_anchors_same_day_frozen_undated_rows(self):
        snapshot = {
            "period": "1m",
            "createdAt": "2026-08-06T18:01:35",
            "series": [{"open": 8.0, "close": 8.1} for _ in range(20)],
        }
        rows = svc._minute_series_from_validation_snapshot(snapshot, "2026-08-06")
        self.assertEqual(rows[15]["time"], "2026-08-06 09:45:00")
        self.assertTrue(rows[15]["_timeInferredFromSameDaySnapshot"])
        stale_rows = svc._minute_series_from_validation_snapshot({**snapshot, "createdAt": "2026-08-07T10:00:00"}, "2026-08-06")
        self.assertEqual(stale_rows, [])

    def test_eastmoney_1m_limit_covers_historical_validation_day(self):
        self.assertGreaterEqual(minute_provider_module._eastmoney_limit("1m"), 1000)
        self.assertGreater(minute_provider_module._eastmoney_limit("1m"), minute_provider_module._eastmoney_limit("15m"))

    def test_next_weekday_date_skips_weekend(self):
        self.assertEqual(svc._next_weekday_date("2026-08-07"), "2026-08-10")
        self.assertEqual(svc._next_weekday_date("2026-08-04"), "2026-08-05")

    def test_t1_snapshot_minutes_are_anchored_to_target_trade_date(self):
        series = svc._minute_series_from_t1_snapshot(
            {
                "period": "15m",
                "series": [
                    {"open": 10.0, "close": 10.5},
                    {"open": 10.5, "close": 10.8},
                ],
            },
            "2026-08-05",
        )
        self.assertEqual(series[0]["time"], "2026-08-05 09:45:00")
        self.assertEqual(series[1]["time"], "2026-08-05 10:00:00")
        open_5m, _, open_5m_status = svc._minute_close_at_or_after(series, "09:35")
        open_15m, open_15m_time, open_15m_status = svc._minute_close_at_or_after(series, "09:45")
        self.assertIsNone(open_5m)
        self.assertEqual(open_5m_status, "target_unavailable")
        self.assertEqual(open_15m, 10.5)
        self.assertEqual(open_15m_time, "09:45")
        self.assertEqual(open_15m_status, "time_exact_or_after")

    def test_validation_minute_snapshots_can_be_persisted_by_run_and_target_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "t1.sqlite")
            stored = db.upsert_t1_overnight_validation_minute_snapshots(
                53,
                "2026-08-05",
                "1m",
                [
                    {
                        "code": "301232",
                        "targetTradeDate": "2026-08-05",
                        "period": "1m",
                        "source": "eastmoney_minute",
                        "qualityStatus": "available",
                        "series": [
                            {"time": "2026-08-05 09:30:00", "open": 10.0, "close": 10.1},
                            {"time": "2026-08-05 09:45:00", "open": 10.1, "close": 10.4},
                        ],
                    }
                ],
            )
            self.assertEqual(stored, 1)
            rows = db.load_t1_overnight_validation_minute_snapshots(53, ["301232"], "2026-08-05")
            self.assertEqual(rows["301232"]["source"], "eastmoney_minute")
            self.assertEqual(rows["301232"]["period"], "1m")
            self.assertEqual(rows["301232"]["pointCount"], 2)

    def test_outcome_uses_recommendation_minute_snapshot_when_daily_reference_missing(self):
        outcome = svc._build_outcome(
            run={"id": 55, "recommendationDate": "2026-08-06", "scanTime": "14:40", "market": {}},
            pick={"code": "300590", "name": "移为通信", "price": 11.06, "dataQuality": {"grade": "A"}},
            series=[
                {"date": "2026-08-05", "open": 10.63, "high": 10.94, "low": 10.59, "close": 10.87},
                {"date": "2026-08-07", "open": 10.94, "high": 11.2, "low": 10.8, "close": 11.1},
            ],
            minute_series=[{"time": "2026-08-07 09:45:00", "open": 10.9, "high": 11.0, "low": 10.8, "close": 10.95}],
            data_source="baostock_daily",
            minute_source="tencent_minute",
            verified_at="2026-08-07T10:00:00",
            recommendation_minute_series=[
                {"time": "2026-08-06 09:45:00", "open": 10.73, "high": 10.95, "low": 10.73, "close": 10.95},
                {"time": "2026-08-06 14:45:00", "open": 11.02, "high": 11.28, "low": 11.02, "close": 11.07},
            ],
        )
        consistency = outcome["priceConsistency"]
        self.assertTrue(consistency["ok"])
        self.assertTrue(consistency["verified"])
        self.assertEqual(consistency["referenceSource"], "recommendation_minute_snapshot")
        self.assertEqual(consistency["referenceDate"], "2026-08-06")
        self.assertNotEqual(outcome["outcomeLabel"], "价格异常")

    def test_outcome_marks_reference_unavailable_without_price_anomaly(self):
        outcome = svc._build_outcome(
            run={"id": 55, "recommendationDate": "2026-08-06", "scanTime": "14:40", "market": {}},
            pick={"code": "300590", "name": "移为通信", "price": 11.06, "dataQuality": {"grade": "A"}},
            series=[
                {"date": "2026-08-05", "open": 10.63, "high": 10.94, "low": 10.59, "close": 10.87},
                {"date": "2026-08-07", "open": 10.94, "high": 11.2, "low": 10.8, "close": 11.1},
            ],
            minute_series=[{"time": "2026-08-07 09:45:00", "open": 10.9, "high": 11.0, "low": 10.8, "close": 10.95}],
            data_source="baostock_daily",
            minute_source="tencent_minute",
            verified_at="2026-08-07T10:00:00",
            recommendation_minute_series=[],
        )
        consistency = outcome["priceConsistency"]
        self.assertTrue(consistency["ok"])
        self.assertFalse(consistency["verified"])
        self.assertEqual(consistency["status"], "reference_unavailable")
        self.assertEqual(outcome["dataQuality"]["grade"], "C")
        self.assertFalse(outcome["learningEligible"])
        self.assertNotEqual(outcome["outcomeLabel"], "价格异常")

    def test_outcome_keeps_price_anomaly_when_daily_reference_really_mismatches(self):
        outcome = svc._build_outcome(
            run={"id": 55, "recommendationDate": "2026-08-06", "scanTime": "14:40", "market": {}},
            pick={"code": "300590", "name": "移为通信", "price": 13.0, "dataQuality": {"grade": "A"}},
            series=[
                {"date": "2026-08-06", "open": 10.73, "high": 11.28, "low": 10.72, "close": 11.1},
                {"date": "2026-08-07", "open": 10.94, "high": 11.2, "low": 10.8, "close": 11.1},
            ],
            minute_series=[{"time": "2026-08-07 09:45:00", "open": 10.9, "high": 11.0, "low": 10.8, "close": 10.95}],
            data_source="baostock_daily",
            minute_source="tencent_minute",
            verified_at="2026-08-07T10:00:00",
            recommendation_minute_series=[],
        )
        self.assertFalse(outcome["priceConsistency"]["ok"])
        self.assertEqual(outcome["outcomeLabel"], "价格异常")
        self.assertEqual(outcome["validationStatus"], "invalid")

    def test_validation_backfill_targets_only_missing_primary_evidence(self):
        snapshots = {
            "301232": {
                "qualityStatus": "available",
                "series": [{"time": "2026-08-05 09:45:00", "open": 10.0, "close": 10.4}],
            }
        }
        outcomes = [
            {
                "code": "301232",
                "dataSource": "eastmoney_daily",
                "outcome": {
                    "tradeable": {
                        "primaryScenario": {
                            "scenarioKey": "open_15m",
                            "dataStatus": "ok",
                            "netReturnPct": 3.8,
                        }
                    }
                },
            },
            {
                "code": "603920",
                "dataSource": "eastmoney_daily",
                "outcome": {
                    "tradeable": {
                        "primaryScenario": {
                            "scenarioKey": "open_15m",
                            "dataStatus": "minute_missing",
                            "netReturnPct": None,
                        }
                    }
                },
            },
        ]
        need, skipped = svc._validation_backfill_target_codes(
            ["301232", "603920"],
            snapshots,
            outcomes,
            "2026-08-05",
            force=False,
        )
        self.assertEqual(need, ["603920"])
        self.assertEqual(skipped, ["301232"])

    def test_validation_backfill_retries_undated_snapshot_when_primary_missing(self):
        snapshots = {
            "600909": {
                "qualityStatus": "partial",
                "series": [{"open": 8.0, "close": 8.1, "high": 8.1, "low": 8.0}],
            }
        }
        outcomes = [
            {
                "code": "600909",
                "outcome": {
                    "tradeable": {
                        "primaryScenario": {
                            "scenarioKey": "open_15m",
                            "dataStatus": "minute_missing",
                            "netReturnPct": None,
                        }
                    }
                },
            }
        ]
        need, skipped = svc._validation_backfill_target_codes(
            ["600909"],
            snapshots,
            outcomes,
            "2026-08-06",
            force=False,
        )
        self.assertEqual(need, ["600909"])
        self.assertEqual(skipped, [])

    def test_validation_backfill_force_includes_existing_strict_evidence(self):
        snapshots = {
            "301232": {
                "qualityStatus": "available",
                "series": [{"time": "2026-08-05 09:45:00", "open": 10.0, "close": 10.4}],
            }
        }
        need, skipped = svc._validation_backfill_target_codes(
            ["301232"],
            snapshots,
            [],
            "2026-08-05",
            force=True,
        )
        self.assertEqual(need, ["301232"])
        self.assertEqual(skipped, [])

    def test_validation_backfill_requires_close_for_final_freeze(self):
        morning_snapshot = {
            "qualityStatus": "partial",
            "series": [
                {"time": "2026-08-05 09:45:00", "close": 10.4},
                {"time": "2026-08-05 11:04:00", "close": 10.2},
            ],
        }
        need, skipped = svc._validation_backfill_target_codes(
            ["301232"],
            {"301232": morning_snapshot},
            [],
            "2026-08-05",
            required_through="15:00",
        )
        self.assertEqual(need, ["301232"])
        self.assertEqual(skipped, [])
        morning_snapshot["series"].append({"time": "2026-08-05 15:00:00", "close": 10.5})
        need, skipped = svc._validation_backfill_target_codes(
            ["301232"],
            {"301232": morning_snapshot},
            [],
            "2026-08-05",
            required_through="15:00",
        )
        self.assertEqual(need, [])
        self.assertEqual(skipped, ["301232"])

    def test_validation_minute_quality_only_marks_full_day_available(self):
        morning = [{"time": "2026-08-05 11:04:00", "close": 10.2}]
        complete = morning + [{"time": "2026-08-05 15:00:00", "close": 10.5}]
        self.assertEqual(svc._validation_minute_quality(morning, "2026-08-05"), "partial")
        self.assertEqual(svc._validation_minute_quality(complete, "2026-08-05"), "available")

    def test_validation_backfill_regrades_legacy_morning_snapshot(self):
        series = [
            {"time": "2026-08-05 09:30:00", "close": 10.0},
            {"time": "2026-08-05 10:05:00", "close": 10.2},
        ]
        rows = svc._regraded_validation_snapshot_rows(
            {
                "301232": {
                    "period": "1m",
                    "source": "tencent_minute",
                    "qualityStatus": "available",
                    "series": series,
                }
            },
            "2026-08-05",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qualityStatus"], "partial")
        self.assertEqual(rows[0]["series"], series)

    def test_validation_backfill_message_separates_pick_and_benchmark_failures(self):
        items = [
            {"code": "000539", "role": "pick", "status": "failed", "error": "1m/15m 均未返回"},
            {"code": "510300", "role": "benchmark", "status": "failed", "error": "1m/15m 均未返回"},
            {"code": "000001", "role": "pick", "status": "skipped"},
        ]
        role_summary = svc._validation_backfill_role_summary(items)
        message = svc._validation_backfill_message(
            run={"recommendationDate": "2026-08-13", "scanTime": "14:40", "request": {"runRole": "official"}},
            target_trade_date="2026-08-14",
            requested=2,
            stored=0,
            failed_items=[item for item in items if item["status"] == "failed"],
            skipped_count=1,
            role_summary=role_summary,
        )
        self.assertIn("最终推荐：成功 0 只、已有 1 只、失败 1 只", message["message"])
        self.assertIn("市场基准：成功 0 只、已有 0 只、失败 1 只", message["message"])
        self.assertIn("最终推荐缺少次日 09:45 卖出验证价：000539", message["reason"])
        self.assertIn("市场基准缺少同口径对照价：510300", message["reason"])
        self.assertFalse(message["recoverable"])

    def test_validation_backfill_message_is_recoverable_when_only_benchmark_fails(self):
        items = [
            {"code": "000539", "role": "pick", "status": "skipped"},
            {"code": "510300", "role": "benchmark", "status": "failed", "error": "未返回"},
        ]
        message = svc._validation_backfill_message(
            run={"recommendationDate": "2026-08-13", "scanTime": "14:40", "request": {"runRole": "official"}},
            target_trade_date="2026-08-14",
            requested=1,
            stored=0,
            failed_items=[items[1]],
            skipped_count=1,
            role_summary=svc._validation_backfill_role_summary(items),
        )
        self.assertIn("最终推荐：成功 0 只、已有 1 只、失败 0 只", message["message"])
        self.assertIn("市场基准缺少同口径对照价：510300", message["reason"])
        self.assertTrue(message["recoverable"])

    def test_load_latest_validation_minute_snapshots_can_reuse_other_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "app.sqlite")
            database.upsert_t1_overnight_validation_minute_snapshots(
                62,
                "2026-08-12",
                "1m",
                [
                    {
                        "code": "002048",
                        "targetTradeDate": "2026-08-12",
                        "period": "1m",
                        "source": "tencent_minute",
                        "qualityStatus": "available",
                        "series": [
                            {"time": "2026-08-12 09:31:00", "close": 10.0},
                            {"time": "2026-08-12 09:45:00", "close": 10.2},
                        ],
                    }
                ],
            )
            reused = database.load_latest_t1_overnight_validation_minute_snapshots_any_run(
                "2026-08-12",
                ["002048"],
                exclude_run_id=61,
            )
        self.assertIn("002048", reused)
        self.assertEqual(reused["002048"]["runId"], 62)
        self.assertEqual(reused["002048"]["source"], "tencent_minute")

    def test_validation_auto_freeze_due_slots_skip_processed_slots(self):
        state = svc._normalize_validation_auto_freeze_state(
            {
                "days": {
                    "2026-08-21": {
                        "09:46": {"status": "completed"},
                    }
                }
            }
        )
        slots = svc._validation_auto_freeze_due_slots(
            datetime.fromisoformat("2026-08-21T10:06:00"),
            state["config"],
            state,
        )
        self.assertEqual(slots, ["10:05"])

    def test_validation_auto_freeze_adds_close_retries_to_legacy_config(self):
        config = svc._normalize_validation_auto_freeze_config(
            {"scheduleTimes": ["09:46", "10:05", "15:10"]}
        )
        self.assertEqual(config["finalizeTimes"], ["15:10", "15:20", "15:40"])
        self.assertEqual(config["scheduleTimes"], ["09:46", "10:05", "15:10", "15:20", "15:40"])

    def test_validation_auto_freeze_next_weekday_handles_weekend(self):
        self.assertEqual(svc._next_weekday_date("2026-08-07"), "2026-08-10")
        self.assertEqual(svc._previous_weekday_date("2026-08-10"), "2026-08-07")

    def test_auto_run_config_defaults_to_production_schedule(self):
        config = svc._normalize_auto_run_config({})
        self.assertTrue(config["enabled"])
        self.assertEqual(config["scheduleTimes"], ["14:40"])
        self.assertEqual(config["latestTriggerTime"], "14:50")
        self.assertEqual(config["poolSize"], 500)
        self.assertFalse(config["force"])

    def test_auto_run_due_slot_detects_1440_once(self):
        state = svc._normalize_auto_run_state({})
        slots = svc._validation_auto_freeze_due_slots(
            datetime.fromisoformat("2026-08-24T14:41:00"),
            state["config"],
            state,
        )
        self.assertEqual(slots, ["14:40"])
        state["days"] = {"2026-08-24": {"14:40": {"status": "started"}}}
        slots = svc._validation_auto_freeze_due_slots(
            datetime.fromisoformat("2026-08-24T14:42:00"),
            state["config"],
            state,
        )
        self.assertEqual(slots, [])


if __name__ == "__main__":
    unittest.main()
