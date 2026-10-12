import copy
import json
import random
import tempfile
import threading
import time
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app.database import Database
from backend.app.engines.t1_candidate_allocation import (
    allocate_round, build_preliminary_pool, clip_minute_series, preliminary_candidate, select_tail_codes,
)
from backend.app.providers.t1_daily_observation_provider import DailyObservationProvider, TradingCalendar, valid_bar, quote_bar
from backend.app.schema import RawStock
from backend.app.services.t1_candidate_tracking_service import CandidateTrackingService, candidate_metric
from backend.app.services import t1_overnight_selection_service as selection
from backend.app.settings import Settings
from backend.app.t1_candidate_store import CandidateStore


def stock(i):
    return RawStock(code=f"{i:06d}", name=f"股票{i}", sector="主板", theme="", price=10,
        change=i % 8, turnover=2, volume_ratio=1.1 + (i % 3) * .4, amount_factor=1.3,
        sector_rank=2, sector_limit_ups=0, rel_strength=50 + i % 50, news_score=0,
        amount=200_000_000 + i * 1000, trade_date="2026-10-09", quote_time="2026-10-09T14:40:00")


def pool(count=500):
    return build_preliminary_pool([preliminary_candidate(stock(i), {"industry": f"行业{i % 15}"})
        for i in range(1, count + 1)], count)


def bar(code, target="2026-10-12"):
    return dict(code=code, tradeDate=target, priceBasis="unadjusted", open=10.2, high=10.8,
        low=9.8, close=10.5, preClose=10, source="fixture", corporateActionStatus="unknown")


def minute_rows(codes):
    return {c: dict(code=c, period="15m", source="fixture_minute", series=[
        dict(time=f"2026-10-09T{10 + j // 4:02d}:{j % 4 * 15:02d}:00", timeLabel="end",
             open=10, high=10.2, low=9.9, close=10 + j / 100, volume=10000, amount=100000)
        for j in range(16)]) for c in codes}


class AllocationTest(unittest.TestCase):
    def test_shuffle_quotas_unique_and_exploration_bands(self):
        original = pool()
        shuffled = copy.deepcopy(original)
        random.Random(42).shuffle(shuffled)
        # Rebuilding ranks from shuffled provider input must produce the same recall.
        self.assertEqual(build_preliminary_pool(shuffled), original)
        first = allocate_round(original, 1, "frozen")
        second = allocate_round(original, 2, "frozen", first, minute_rows([x["code"] for x in first]))
        alternate = allocate_round(shuffled, 1, "frozen")
        self.assertEqual([(x["code"], x["primaryGroup"]) for x in first],
                         [(x["code"], x["primaryGroup"]) for x in alternate])
        self.assertEqual((len(first), len(second)), (80, 40))
        self.assertEqual(len({x["code"] for x in first + second}), 120)
        self.assertEqual(Counter(x["primaryGroup"] for x in first + second),
                         dict(core=55, single=20, boundary=20, coverage=15, explore=10))
        exploration = [x for x in first + second if x["primaryGroup"] == "explore"]
        self.assertEqual([sum(lo <= x["preliminaryScoreRank"] <= hi for x in exploration)
                          for lo, hi in ((121, 200), (201, 300), (301, 400), (401, 500))], [4, 3, 2, 1])

    def test_small_pool_shortage_and_budget(self):
        candidates = pool(35)
        result = allocate_round(candidates, 1, "s", budget=25)
        self.assertEqual(len(result), 25)
        self.assertEqual(len({x["code"] for x in result}), 25)
        self.assertTrue(any(x["relaxationReason"] for x in result))
        self.assertEqual(allocate_round(candidates, 2, "s", result, budget=25), [])

    def test_clip_requires_date_and_complete_bucket(self):
        points = [dict(time="2026-10-09T14:30:00", timeLabel="end"),
                  dict(time="2026-10-09T14:30:00"),
                  dict(time="2026-10-09T14:39:00", timeLabel="end", bucketComplete=False),
                  dict(time="14:30"), dict(time="2026-10-09T14:45:00", timeLabel="end"),
                  dict(time="2026-10-08T14:30:00", timeLabel="end")]
        self.assertEqual(clip_minute_series(points, "2026-10-09", "14:40", "15m"), points[:1])

    def test_tail_null_dimensions_and_budget(self):
        candidates = [dict(x, t1Score=70, verifiedRank=None, t1Dimensions={"intradayStructure": None}) for x in pool(50)]
        codes = select_tail_codes(candidates, candidates[:8])
        self.assertLessEqual(len(codes), 30)
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(select_tail_codes(candidates, [], 0), [])

    def test_round_two_responds_to_real_gate_feedback(self):
        candidates = pool()
        first = allocate_round(candidates, 1, "feedback")
        failed = first[0]["industry"]
        good = next(x["industry"] for x in first if x["industry"] != failed)
        evidence = {x["code"]: dict(series=[{}], qualityEligible=x["industry"] != failed,
            verifiedScore=95 if x["industry"] == good else 30, scoreDisagreement=40 if x["industry"] == good else 0) for x in first}
        second = allocate_round(candidates, 2, "feedback", first, evidence)
        self.assertEqual(len(second), 40)
        self.assertTrue(all(x["industry"] != failed for x in second if x["primaryGroup"] == "core"))
        self.assertTrue(any("real_score_gate_feedback" in x["allocationReason"] for x in second))

    def test_proxy_percent_missing_basis_exclusions(self):
        c = dict(code="000001", frozenScanPrice=10, priceBasis="unadjusted")
        m = candidate_metric(c, bar(c["code"]), "2026-10-12")
        self.assertEqual((m["openProxyReturnPct"], m["closeProxyReturnPct"]), (2, 5))
        self.assertIsNone(m["overnightGapPct"])
        self.assertIsNone(candidate_metric(c, None, "2026-10-12")["closeProxyReturnPct"])
        self.assertFalse(candidate_metric(c, dict(bar(c["code"]), corporateActionStatus="ex_right"), "2026-10-12")["aggregateEligible"])
        self.assertFalse(valid_bar(dict(bar(c["code"]), priceBasis="qfq"), "2026-10-12"))
        self.assertFalse(valid_bar(bar(c["code"], "2026-10-13"), "2026-10-12"))
        self.assertFalse(valid_bar(dict(bar(c["code"]), close="10.5"), "2026-10-12"))
        self.assertEqual(candidate_metric(c, dict(bar(c["code"]), tradingStatus="suspended", businessStatusConfirmed=True), "2026-10-12")["status"], "business_resolved")

    def test_quote_close_timestamp_must_match_target(self):
        s = stock(1)
        s.open, s.high, s.low = 10, 10.2, 9.8
        self.assertIsNone(quote_bar(s, "2026-10-09"))
        s.quote_time = "2026-10-09T15:01:00"
        self.assertIsNotNone(quote_bar(s, "2026-10-09"))
        self.assertIsNone(quote_bar(s, "2026-10-12"))

    def test_daily_provider_history_budget_and_source_telemetry(self):
        codes = [f"{i:06d}" for i in range(1, 61)]
        calls = []
        def history(code, target, source):
            calls.append((code, source))
            return None if source == "eastmoney" else bar(code, target)
        with patch("backend.app.market_context_store.FullMarketContextStore") as context, \
             patch("backend.app.providers.t1_daily_observation_provider.fetch_history", side_effect=history):
            context.return_value.latest_cached.return_value = {}
            rows, meta = DailyObservationProvider().fetch("2026-01-02", codes, time.monotonic() + 120)
        self.assertEqual(len(rows), 50)
        self.assertEqual(len(calls), 100)
        self.assertEqual(meta["requestCount"], 100)
        self.assertEqual(meta["retryCount"], 50)
        self.assertEqual(meta["failuresBySource"], {"eastmoney_daily": 50})
        self.assertEqual(meta["missingCount"], 10)
        self.assertFalse(meta["deadlineReached"])


class ArchiveTrackingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "app.sqlite")
        self.store = CandidateStore(self.db)
        self.db.set_app_setting("t1_tracking_exchange_calendar", dict(start="2026-10-09", end="2026-11-01",
            openDates=["2026-10-09", "2026-10-12", "2026-10-13"], source="test_exchange", version="fixture"))

    def run_archive(self, candidates, status="done"):
        run_id = self.db.create_t1_overnight_selection_run(dict(recommendationDate="2026-10-09", status=status, scanTime="14:40"), [])
        self.store.freeze(run_id, candidates)
        return run_id

    def test_immutable_archive_pagination_filter_and_legacy(self):
        candidates = pool(60)
        run_id = self.run_archive(candidates)
        changed = dict(candidates[0], price=999, preliminaryScoreRank=999)
        self.store.freeze(run_id, [changed])
        self.store.freeze(run_id, pool(61))
        self.assertEqual(self.store.summary(run_id)["total"], 60)
        self.store.update(run_id, [dict(code=changed["code"], price=999, preliminaryScoreRank=999, primaryGroup="core", allocationRound=1)], "planned")
        detail = self.store.detail(run_id, changed["code"])
        self.assertEqual(detail["candidate"]["price"], 10)
        self.assertEqual(detail["candidate"]["preliminaryScoreRank"], 1)
        self.assertEqual(len(self.store.page(run_id)["items"]), 50)
        self.assertEqual(len(self.store.page(run_id, page=2)["items"]), 10)
        self.assertEqual(self.store.page(run_id, group="core")["total"], 1)
        self.assertEqual(self.store.page(run_id, query=changed["code"])["total"], 1)
        self.assertNotIn("rawStock", self.store.page(run_id)["items"][0])
        self.assertTrue(self.store.summary(999)["legacyNotRecorded"])

    def test_shared_daily_fetch_for_two_runs_and_missing_only_retry(self):
        candidates = pool(100)
        a = self.run_archive(candidates)
        b = self.run_archive(candidates[:30], status="failed")
        missing = candidates[-1]["code"]
        calls = []
        def fetch(target, codes, deadline):
            calls.append(codes)
            return {c: bar(c, target) for c in codes if c != missing or len(calls) > 1}, {}
        tracking = CandidateTrackingService(self.db, provider=SimpleNamespace(fetch=fetch))
        tracking.tick(datetime(2026, 10, 12, 15, 10))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 100)
        self.assertEqual(self.store.job(a)["status"], "retry_wait")
        self.assertEqual(self.store.job(b)["status"], "complete")
        summary = tracking.summary(a)
        self.assertEqual(summary["coveragePct"], 99)
        self.assertFalse(summary["coverageIncomplete"])
        self.assertEqual(summary["groups"][0]["missing"], 1)
        tracking.tick(datetime(2026, 10, 12, 15, 20))
        self.assertEqual(calls[1], [missing])
        self.assertEqual(self.store.job(a)["status"], "complete")
        tracking.tick(datetime(2026, 10, 12, 15, 40))
        self.assertEqual(len(calls), 2)

    def test_requested_failure_remains_in_120_group(self):
        candidates = pool(15)
        run_id = self.run_archive(candidates)
        self.store.update(run_id, [dict(code=candidates[0]["code"], allocationRound=1,
            minute15mRequested=True, minute15mStatus="failed")], "15m_result")
        tracking = CandidateTrackingService(self.db, provider=SimpleNamespace(fetch=lambda *a: ({}, {})))
        tracking.tick(datetime(2026, 10, 12, 15, 10))
        groups = {x["group"]: x for x in tracking.summary(run_id)["groups"]}
        self.assertEqual(groups["requested120"]["total"], 1)
        self.assertEqual(groups["available15m"]["total"], 0)

    def test_provisional_date_does_not_skip_and_expires(self):
        self.db.set_app_setting("t1_tracking_exchange_calendar", {})
        run_id = self.run_archive(pool(5))
        tracking = CandidateTrackingService(self.db, provider=SimpleNamespace(fetch=lambda *a: ({}, {})))
        tracking.tick(datetime(2026, 10, 13, 15, 10))
        self.assertEqual(self.store.job(run_id)["target_trade_date"], "2026-10-12")
        self.assertEqual(self.store.job(run_id)["status"], "waiting_trade_date_confirmation")
        tracking.tick(datetime(2026, 10, 22, 15, 10))
        self.assertEqual(self.store.job(run_id)["status"], "terminal_missing")
        self.assertEqual(self.store.job(run_id)["target_trade_date"], "2026-10-12")

    def test_authoritative_calendar_revision_records_holiday(self):
        self.db.set_app_setting("t1_tracking_exchange_calendar", {})
        run_id = self.run_archive(pool(5))
        tracking = CandidateTrackingService(self.db, provider=SimpleNamespace(fetch=lambda *a: ({}, {})))
        tracking.ensure_job(run_id)
        self.db.set_app_setting("t1_tracking_exchange_calendar", dict(start="2026-10-09", end="2026-11-01",
            openDates=["2026-10-13"], source="exchange", version="v2"))
        tracking.tick(datetime(2026, 10, 12, 15, 10))
        job = self.store.job(run_id)
        self.assertEqual(job["target_trade_date"], "2026-10-13")
        self.assertEqual(json.loads(job["calendar_json"])["revision"]["oldTarget"], "2026-10-12")

    def test_lease_prevents_other_worker_and_same_worker_overlap(self):
        self.run_archive(pool(5))
        tracking = CandidateTrackingService(self.db, provider=Mock(fetch=Mock(return_value=({}, {}))))
        tracking._tick_lock.acquire()
        tracking.tick(datetime(2026, 10, 12, 15, 10))
        tracking._tick_lock.release()
        tracking.provider.fetch.assert_not_called()
        with self.db.connect() as conn:
            conn.execute("INSERT INTO t1_market_daily_collection_leases VALUES(?,?,?)",
                         ("2026-10-12", "other", "2026-10-12T16:00:00"))
        tracking.tick(datetime(2026, 10, 12, 15, 10))
        tracking.provider.fetch.assert_not_called()
        tracking.tick(datetime(2026, 10, 12, 16, 10))
        tracking.provider.fetch.assert_called_once()

    def test_updated_authoritative_calendar_revises_pending_job(self):
        run_id = self.run_archive(pool(5))
        tracking = CandidateTrackingService(self.db, provider=SimpleNamespace(fetch=lambda *args: ({}, {})))
        tracking.ensure_job(run_id)
        self.db.set_app_setting("t1_tracking_exchange_calendar", dict(start="2026-10-09", end="2026-11-01",
            openDates=["2026-10-13"], source="exchange", version="corrected"))
        tracking.tick(datetime(2026, 10, 12, 15, 10))
        job = self.store.job(run_id)
        self.assertEqual(job["target_trade_date"], "2026-10-13")
        self.assertEqual(json.loads(job["calendar_json"])["version"], "corrected")

    def test_all_suspended_quotes_cannot_confirm_provisional_date_forever(self):
        self.db.set_app_setting("t1_tracking_exchange_calendar", {})
        run_id = self.run_archive(pool(5))
        def fetch(target, codes, deadline):
            return {code: dict(bar(code, target), tradingStatus="suspended", businessStatusConfirmed=True)
                    for code in codes}, {}
        tracking = CandidateTrackingService(self.db, provider=SimpleNamespace(fetch=fetch))
        tracking.tick(datetime(2026, 10, 12, 15, 10))
        self.assertEqual(self.store.job(run_id)["status"], "waiting_trade_date_confirmation")
        tracking.tick(datetime(2026, 10, 22, 15, 10))
        self.assertEqual(self.store.job(run_id)["status"], "terminal_missing")

    def test_missing_rotation_prevents_first_fifty_starvation(self):
        run_id = self.run_archive(pool(120))
        calls = []
        def fetch(target, codes, deadline):
            calls.append(codes[:50])
            return {}, {}
        tracking = CandidateTrackingService(self.db, provider=SimpleNamespace(fetch=fetch))
        tracking.tick(datetime(2026,10,12,15,10))
        tracking.tick(datetime(2026,10,12,15,20))
        self.assertEqual(len(calls[0]), 50)
        self.assertFalse(set(calls[0]) & set(calls[1]))
        self.assertEqual(self.store.job(run_id)["resolved_count"], 0)

    def test_trading_day_expiry_counts_calendar_holidays(self):
        self.db.set_app_setting("t1_tracking_exchange_calendar", dict(start="2026-10-09", end="2026-11-01",
            openDates=["2026-10-12", "2026-10-20", "2026-10-21"], source="test", version="holiday"))
        run_id = self.run_archive(pool(5))
        tracking = CandidateTrackingService(self.db, provider=SimpleNamespace(fetch=lambda *args: ({}, {})))
        tracking.tick(datetime(2026,10,22,15,10))
        self.assertNotEqual(self.store.job(run_id)["status"], "terminal_missing")

    def test_worker_resume_keeps_completed_batch_and_frozen_ai_membership(self):
        settings = Settings((), False, False, 15, "15m", self.db.path, "deepseek", "fixture", "fixture", "fixture", True, "", {})
        service = selection.T1OvernightSelectionService(settings, SimpleNamespace(), self.db)
        candidates = [dict(x, t1Score=75, poolRank=i, rank=i, t1Dimensions={}, dataQuality={"eligibleForRealTop":True}) for i,x in enumerate(pool(120),1)]
        run_id = self.run_archive(candidates, "running")
        pick = dict(candidates[0], batchIndex=1, aiRank=1, selected=True)
        self.db.upsert_t1_overnight_selection_items(run_id, candidates, "pool")
        self.db.upsert_t1_overnight_selection_items(run_id, [pick], "semifinal")
        self.db.update_t1_overnight_selection_run(run_id, dict(batchSize=80, batchPick=8, finalLimit=3,
            completedBatchCount=1, batchSummary=[dict(batchIndex=1,candidateCount=80,pickCount=1,picks=[pick])],
            request=dict(poolSize=500,poolCandidateCodes=[x["code"] for x in candidates],selectionCandidateCodes=[x["code"] for x in candidates]),
            market=dict(marketContext={},dataQualityStats={},validationAllocation={"version":"frozen"}), evidenceRunId=1))
        service._load_market_data_for_t1 = Mock(side_effect=AssertionError("resume must use checkpoint"))
        calls=[]
        def ai(chunk, market, limit, stage, batch_index):
            calls.append((stage,batch_index,[x["code"] for x in chunk]))
            return ([dict(code=chunk[0]["code"],confidence=70)] if chunk else []), ""
        service._select_with_ai = ai
        service._attach_dynamic_tail = Mock()
        service._select_tail_shadow_with_ai = Mock(return_value={"picks":[]})
        service._apply_critic_if_enabled = Mock(side_effect=lambda **kwargs: dict(picks=kwargs["picks"], status={}))
        with patch.object(selection,"_recommendation_policy",return_value={"maxPickCount":3}), patch.object(selection,"_apply_final_guardrails",side_effect=lambda picks,*a:picks):
            service._run_worker(run_id)
        run = self.db.load_t1_overnight_selection_run(run_id)
        self.assertEqual(run["status"], "done", run.get("error"))
        self.assertEqual([(s,i) for s,i,_ in calls], [("batch",2),("final",0)])
        self.assertEqual(calls[0][2], [x["code"] for x in candidates[80:]])
        self.assertIn(pick["code"], calls[1][2])
        self.assertEqual(len(run["batchSummary"]), 2)
        self.assertTrue(self.store.detail(run_id, run["finalPicks"][0]["code"])["candidate"]["officialSelected"])

    def test_successful_bar_is_not_overwritten_or_poisoned(self):
        self.store.save_bars("2026-10-12", {"000001": bar("000001")})
        self.store.save_bars("2026-10-12", {"000001": dict(bar("000001"), close=10.7),
            "000002": dict(bar("000002"), close=-1)})
        self.assertEqual(self.store.bars("2026-10-12")["000001"]["close"], 10.5)
        self.assertNotIn("000002", self.store.bars("2026-10-12"))

    def test_build_pool_integration_two_rounds_and_frozen_resume(self):
        settings = Settings((), False, False, 15, "1m", self.db.path, "deepseek", "", "", "", False, "", {})
        analysis = SimpleNamespace(_apply_scope=lambda stocks, scope: stocks, kline_provider=SimpleNamespace())
        service = selection.T1OvernightSelectionService(settings, analysis, self.db)
        self.assertEqual(service.settings.kline_period, "15m")
        run_id = self.run_archive([], "running")
        market = dict(tradeDate="2026-10-09", asOfTime="2026-10-09T14:40:00", snapshotVersion="frozen-1",
            breadth={"up": 3000, "down": 2000}, indices=[])
        service._load_market_data_for_t1 = Mock(return_value=(SimpleNamespace(source="fixture"), market,
            [stock(i) for i in range(1, 501)], ""))
        requested = []
        def prepare(**kwargs):
            self.assertEqual(kwargs["tail_minute_codes"], [])
            requested.append(kwargs["minute_codes"])
            evidence_id = self.db.create_t1_overnight_evidence_run(dict(recommendationDate="2026-10-09",
                scanTime="14:40", status="done", market=market))
            return dict(run={"id": evidence_id}, minuteByCode=minute_rows(kwargs["minute_codes"]))
        service._prepare_t1_evidence_run = prepare
        with patch.object(selection, "_fetch_t1_minute_rows_with_retries", side_effect=lambda *args, **kwargs: minute_rows(args[2])):
            result = service._build_pool("balanced", "auto", "14:40", 500, recommendation_date="2026-10-09", selection_run_id=run_id)
            archive = self.store.all(run_id)
            self.assertEqual(len(archive), 500)
            self.assertEqual(len(result["pool"]), 500)
            self.assertEqual(sum(x["minute15mRequested"] for x in archive), 120)
            self.assertEqual(sum(x["verifiedRank"] is not None for x in archive), 120)
            self.assertEqual([x["planned"] for x in result["market"]["validationAllocation"]["rounds"]], [80, 40])
            initial = [(x["code"], x["preliminaryScoreRank"], x["frozenScanPrice"], x["primaryGroup"]) for x in archive]
            service._load_market_data_for_t1.side_effect = AssertionError("resume must not load live market")
            resumed = service._build_pool("balanced", "auto", "14:40", 500, recommendation_date="2026-10-09", selection_run_id=run_id)
            self.assertEqual(initial, [(x["code"], x["preliminaryScoreRank"], x["frozenScanPrice"], x["primaryGroup"]) for x in self.store.all(run_id)])
            self.assertEqual(resumed["totalCount"], 500)
            self.assertNotIn("rawStock", result["pool"][0])

    def test_failed_first_request_retains_archive_and_planned_round(self):
        settings = Settings((), False, False, 15, "15m", self.db.path, "deepseek", "", "", "", False, "", {})
        service = selection.T1OvernightSelectionService(settings, SimpleNamespace(_apply_scope=lambda s, _: s), self.db)
        run_id = self.run_archive([], "running")
        service._load_market_data_for_t1 = Mock(return_value=(SimpleNamespace(source="fixture"), {}, [stock(i) for i in range(1, 501)], ""))
        service._prepare_t1_evidence_run = Mock(side_effect=RuntimeError("provider unavailable"))
        with self.assertRaises(RuntimeError):
            service._build_pool("balanced", "auto", "14:40", 500, recommendation_date="2026-10-09", selection_run_id=run_id)
        self.assertEqual(self.store.summary(run_id)["total"], 500)
        self.assertEqual(self.store.summary(run_id)["rounds"][0]["requested"], 80)


if __name__ == "__main__":
    unittest.main()
