import copy
import io
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from backend.app.engines import t1_selection_policy as policy
from backend.app.engines.t1_candidate_allocation import clip_minute_series, select_tail_codes
from backend.app.market_context_store import _runtime_payload
from backend.app.providers.tencent_kline_provider import _aggregate_series
from backend.app.services import t1_overnight_selection_service as svc


def candidate(code="000001", falling=False):
    closes = [10, 9.99, 10, 10.01, 10, 10.02, 10.04, 10.05, 10.08, 10.10]
    if falling:
        closes = [10.5 - i * .04 for i in range(10)]
    times = ["09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30", "13:15", "13:30"]
    rows = [dict(time=f"2026-10-16T{t}:00", close=p, open=p, high=p+.005, low=p-.005,
                 volume=1000, timeLabel="end", bucketComplete=True) for t, p in zip(times, closes)]
    c = policy.annotate(dict(code=code, name="测试", series=rows, klineSource="tencent_minute",
        amount=300_000_000, relStrength=85, change=2, dataEligible=True,
        quoteTradeDate="2026-10-16", t1Dimensions={"overnightRisk": 30}))
    c["dataQuality"] = {"eligibleForRealTop": True}
    c["evidenceRefs"] = [{"id": "P1", "status": "available", "source": "tencent_minute",
        "asOfTime": "2026-10-16T13:30:00", "scanCutoff": "2026-10-16T14:40:00"}]
    return c


def tail(**changes):
    return dict(dict(pointCount=11, status="weak_support", actualLastTime="2026-10-16T14:40:00",
        recent5MinuteCount=5, recent5VolumePointCount=5,
        source="tencent_minute", tradeDate="2026-10-16", vwapDeviationPct=.2,
        change5mPct=.1, sellPressureVolumeRatio=40, longestSellPressureCount=1,
        tailMaxDrawdownPct=-.1, tailChangePct=.2, recoveredVwap=False), **changes)


def pick(code="000001", **changes):
    return dict(dict(code=code, supportingEvidenceRefs=["P1"], counterEvidenceRefs=[],
                     decisionTags=["data_quality_ok"], confidence=75), **changes)


class PolicyTest(unittest.TestCase):
    def test_final_decision_cards_keep_nine_and_thirty_candidates_inside_safe_budget(self):
        rows = []
        for index in range(30):
            item = policy.formal_tail(candidate(f"{index + 1:06d}"), tail(), "14:40")
            item["supportingEvidenceRefs"] = ["P1", "T1"]
            item["counterEvidenceRefs"] = ["R1"]
            item["evidenceRefs"] = list(item.get("evidenceRefs") or []) + [
                {"id": "T1", "status": "available", "summary": "真实尾盘承接证据"}
            ]
            rows.append(item)
        cards = [svc._compact_candidate_for_prompt(item, "final", True) for item in rows]
        self.assertTrue(all(card["cardVersion"] == svc.T1_FINAL_DECISION_CARD_VERSION for card in cards))
        self.assertTrue(all(card["cardCharacters"] <= svc.T1_FINAL_DECISION_CARD_MAX_CHARACTERS for card in cards))
        self.assertEqual(cards[0]["priorDecisionEvidence"]["supportingEvidenceRefs"], ["P1", "T1"])
        self.assertEqual(cards[0]["priorDecisionEvidence"]["counterEvidenceRefs"], ["R1"])
        for count in (9, 12, 30):
            prompt = svc._user_prompt(rows[:count], {"selectionPolicyVersion": policy.VERSION}, 3, "final", {})
            self.assertLess(len(prompt), svc.T1_AI_SAFE_REQUEST_CHARACTERS, count)

    def test_mock_and_unavailable_sources_never_become_official_market_decisions(self):
        self.assertFalse(svc._t1_market_decision_usable("mock", {"mockStatus": {"used": True}}))
        self.assertFalse(svc._t1_market_decision_usable("unavailable", {}))
        self.assertFalse(svc._t1_market_decision_usable(
            "sina", {"marketSnapshotFreeze": {"snapshotDecisionUsable": False}}
        ))
        self.assertTrue(svc._t1_market_decision_usable(
            "cache:tencent", {"marketSnapshotFreeze": {"snapshotDecisionUsable": True}}
        ))

    def test_proxy_scores_and_board_do_not_manufacture_independent_evidence(self):
        c = candidate()
        other = policy.annotate(dict(c, amountFactor=99, volumeRatio=99, sectorRank=1,
                                     sectorLimitUps=100, t1Dimensions={"overnightRisk": 30}))
        self.assertEqual(c["t1Score"], other["t1Score"])
        self.assertIsNone(other["scoreComponents"]["industry"])
        self.assertIsNone(other["t1Dimensions"]["tomorrowPremium"])
        self.assertNotIn("late_money_inflow", other["candidateTypes"])

    def test_data_available_is_not_a_confirmed_setup(self):
        c = candidate(falling=True)
        self.assertTrue(c["dataEligible"])
        self.assertFalse(c["setupEligible"])
        self.assertFalse(policy.formal_tail(c, tail(), "14:40")["formalEligible"])

    def test_prior_reference_and_independent_detectors(self):
        c = candidate()
        setup = c["setupEvidence"]
        self.assertLess(setup["referenceHigh"], c["series"][-1]["close"])
        self.assertIn("breakout_hold", setup["confirmedTypes"])
        strengths = [setup["detectors"][k]["evidenceStrength"] for k in setup["confirmedTypes"]]
        self.assertEqual(strengths, sorted(strengths, reverse=True))
        self.assertLess(setup["morningHigh"], c["series"][-1]["close"])

    def test_missing_volume_simulation_and_incomplete_bars_are_insufficient(self):
        for mode in ("volume", "simulated", "incomplete", "undated"):
            c = candidate()
            if mode == "simulated":
                c["klineSource"] = "simulated"
            for r in c["series"]:
                if mode == "volume": r["volume"] = 0
                if mode == "incomplete": r["bucketComplete"] = False
                if mode == "undated": r["time"] = r["time"][11:16]
            self.assertEqual(policy.structure(c)["status"], "insufficient", mode)

    def test_formal_tail_requires_fresh_same_day_real_evidence(self):
        self.assertTrue(policy.formal_tail(candidate(), tail(), "14:40")["formalEligible"])
        for delta in ({"actualLastTime": "2026-10-16T14:35:00"},
                      {"actualLastTime": "2026-10-15T14:40:00"},
                      {"actualLastTime": "2026-10-16T14:41:00"},
                      {"source": "simulated"}, {"pointCount": 0}, {"vwapDeviationPct": None},
                      {"recent5MinuteCount": 4}, {"recent5VolumePointCount": 4}):
            self.assertFalse(policy.formal_tail(candidate(), tail(**delta), "14:40")["formalEligible"], delta)

    def test_weak_tail_cannot_be_bypassed_by_rule_or_ai_selection(self):
        c = policy.formal_tail(candidate(), tail(change5mPct=-.7, sellPressureVolumeRatio=70), "14:40")
        market = {"selectionPolicyVersion": policy.VERSION}
        self.assertFalse(c["formalEligible"])
        self.assertEqual(svc._apply_final_guardrails([c], market, 3), [])
        c["formalEligible"] = True
        market["marketContext"] = {"state": "panic"}
        self.assertEqual(svc._apply_final_guardrails([c], market, 3), [])

    def test_final_ai_needs_t1_and_confirmed_qualification(self):
        c = policy.formal_tail(candidate(), tail(), "14:40")
        self.assertFalse(svc._validate_ai_picks_evidence([pick()], [c], "final")["valid"])
        self.assertTrue(svc._validate_ai_picks_evidence([pick(supportingEvidenceRefs=["P1", "T1"])], [c], "final")["valid"])

    def test_time_aggregation_missing_opening_lunch_and_scan_cutoff(self):
        def minute(t):
            return dict(time=f"2026-10-16T{t}:00", open=10, high=10, low=10, close=10, volume=1)
        rows = [minute("09:30")] + [minute(f"09:{m:02}") for m in range(31, 61) if m != 37 and m < 60]
        rows += [minute("10:00"), minute("13:00"), minute("13:01"), minute("14:39")]
        result = _aggregate_series(rows, 15)
        self.assertEqual(result[0]["time"], "2026-10-16 09:45")
        self.assertFalse(result[0]["bucketComplete"])
        self.assertTrue(result[1]["bucketComplete"])
        self.assertEqual(result[2]["bucketStart"], "2026-10-16 13:00")
        self.assertEqual([x["time"] for x in clip_minute_series(result, "2026-10-16", "14:40", "15m")],
                         ["2026-10-16 10:00"])

    def test_all_semifinal_candidates_are_prioritized(self):
        pool = [dict(candidate(), code=f"{i:06}", primaryGroup="core", verifiedRank=i) for i in range(45)]
        semifinal = pool[-22:]
        selected = select_tail_codes(pool, semifinal, 30)
        self.assertTrue({x["code"] for x in semifinal}.issubset(selected))
        self.assertLessEqual(len(selected), 30)

    def test_s2_disabled_removes_nested_hints_and_tag_permissions(self):
        c = candidate()
        c.update(sectorStateShadow="strong", sectorEvidence={"nested": {"sectorStateLabel": "强势"}},
            evidenceRefs=c["evidenceRefs"] + [{"id": "S2", "status": "available"}])
        c["extra"] = {"refs": [{"id": "S2"}], "tags": ["sector_state_supported"]}
        clean = svc._without_shadow_sector_prompt_fields(c)
        self.assertNotIn("S2", json.dumps(clean))
        self.assertNotIn("sectorState", json.dumps(clean))
        prompt = svc._user_prompt([c], {"selectionPolicyVersion": policy.VERSION,
            "stageControls": {"sectorStatePrompt": {"effectiveEnabled": False}}}, 3, "batch")
        self.assertNotIn('"sector_state_supported"', prompt)
        self.assertNotIn('"series"', prompt)
        self.assertFalse(svc._validate_ai_picks_evidence([pick(decisionTags=["sector_state_supported"])], [clean], "batch")["valid"])

    def test_legacy_coverage_is_unknown_without_modifying_input(self):
        frozen = {"coverage": {"rows": 5031, "expectedRows": 5031, "coveragePct": 100}, "stocks": []}
        runtime = _runtime_payload(frozen, True, False)
        self.assertIsNone(runtime["coverage"]["coveragePct"])
        self.assertEqual(frozen["coverage"]["coveragePct"], 100)

    def test_versions_do_not_mix_optimization_or_feedback(self):
        rows = [dict(selectionPolicyVersion=v, reviewStatus="learnable", learningEligibleCount=1,
            exitTimingStats=[dict(scenarioKey=svc.PRIMARY_TRADEABLE_SCENARIO_KEY, netReturnsPct=[value])])
            for v, value in (("legacy", 20), (policy.VERSION, -1))]
        result = svc._optimization_review_stats(rows)
        self.assertEqual(result["primary"]["avgNetReturnPct"], -1)
        self.assertEqual(len(result["versionGroups"]), 2)
        feedback = svc._build_strategy_feedback([{"outcomeLabel": "弱成功"}], 20, 98, "test", policy.VERSION)
        self.assertEqual(feedback["verifiedOutcomes"], 0)


class AIRepairTest(unittest.TestCase):
    def run_ai(self, contents):
        service = object.__new__(svc.T1OvernightSelectionService)
        service.settings = SimpleNamespace(ai_provider="deepseek", ai_model="fixture", ai_base_url="https://example.test/v1", ai_api_key="fixture")
        service._cached_ai_picks = lambda _: None
        service._save_ai_picks_cache = lambda *args: None
        service.latest_strategy_feedback = lambda: {}
        def response(value):
            if isinstance(value, Exception): raise value
            content = value if isinstance(value, str) else json.dumps(value)
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())
        market = {"selectionPolicyVersion": policy.VERSION}
        with patch.object(svc.urllib.request, "urlopen", side_effect=[response(x) for x in contents]) as call:
            result = service._select_with_ai([candidate(), candidate("000002"), candidate("000003")], market, 3, "batch", 1)
        return result, market, call

    def test_partial_repair_preserves_valid_and_restricts_original_codes(self):
        (picks, error), market, call = self.run_ai([
            {"picks": [pick(), pick("000002", supportingEvidenceRefs=["MISSING"])]},
            {"picks": [pick("000003")]}, {"picks": [pick("000002")]}])
        self.assertEqual([p["code"] for p in picks], ["000001", "000002"])
        self.assertEqual(error, "")
        self.assertEqual(call.call_count, 3)

    def test_parse_retry_can_accept_an_original_input_candidate(self):
        (picks, error), market, call = self.run_ai(["not json", {"picks": [pick()]}])
        self.assertEqual([p["code"] for p in picks], ["000001"])
        self.assertEqual(error, "")

    def test_malformed_array_is_not_zero_pick_and_retry_is_bounded(self):
        (picks, error), market, call = self.run_ai([{"picks": [None]}] * 3)
        self.assertEqual(picks, [])
        self.assertTrue(error)
        self.assertEqual(market["aiEvidenceValidationStatus"], "parse_failure")
        self.assertEqual(call.call_count, 3)

    def test_legitimate_empty_pick_is_distinct(self):
        (picks, error), market, call = self.run_ai([{"picks": []}])
        self.assertEqual((picks, error), ([], ""))
        self.assertEqual(market["aiEvidenceValidationStatus"], "zero_pick")
        self.assertEqual(call.call_count, 1)

    def test_over_budget_final_uses_audited_tournament_without_dropping_input_candidates(self):
        service = object.__new__(svc.T1OvernightSelectionService)
        service.settings = SimpleNamespace(ai_provider="deepseek", ai_model="fixture", ai_base_url="https://example.test/v1", ai_api_key="fixture")
        service._cached_ai_picks = lambda _: None
        service._save_ai_picks_cache = lambda *args: None
        service.latest_strategy_feedback = lambda: {}
        candidates = []
        for index in range(12):
            item = policy.formal_tail(candidate(f"{index + 1:06d}"), tail(), "14:40")
            item["evidenceRefs"] = list(item.get("evidenceRefs") or []) + [
                {"id": "T1", "status": "available", "summary": "真实尾盘承接证据"}
            ]
            candidates.append(item)
        sent_sizes = []

        def response(request, timeout=0):
            payload = json.loads(request.data.decode("utf-8"))
            user_prompt = payload["messages"][1]["content"]
            sent_sizes.append(len(user_prompt) + len(payload["messages"][0]["content"]))
            marker = "客观复盘只描述事实；只有 confirmedStrategyConstraints 是用户已确认的策略约束。\n"
            prompt_payload = json.loads(user_prompt.split(marker, 1)[1])
            code = prompt_payload["candidates"][0]["code"]
            body = {"picks": [pick(code, supportingEvidenceRefs=["P1", "T1"])]}
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(body)}}]}).encode())

        market = {"selectionPolicyVersion": policy.VERSION}
        with patch.object(svc, "T1_AI_SAFE_REQUEST_CHARACTERS", 20_000), patch.object(
            svc.urllib.request, "urlopen", side_effect=response
        ):
            picks, error = service._select_with_ai(candidates, market, 3, "final", 0)
        self.assertEqual(error, "")
        self.assertEqual(len(picks), 1)
        self.assertTrue(all(size <= 20_000 for size in sent_sizes))
        audit = market["finalSelectionAudit"]
        self.assertEqual(audit["mode"], "budgeted_tournament")
        audited_codes = {code for group in audit["groups"] for code in group["candidateCodes"] if group["depth"] == 0}
        self.assertEqual(audited_codes, {item["code"] for item in candidates})


if __name__ == "__main__":
    unittest.main()
