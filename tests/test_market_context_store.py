import tempfile
import unittest
import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

from backend.app import market_context_store as module
from backend.app.market_context_store import FullMarketContextStore
from backend.app.schema import RawStock


class _Provider:
    source = "sina"
    last_meta = {"pagesOk": 1, "pagesFailed": 0}

    def __init__(self, stocks):
        self.stocks = stocks

    def fetch_snapshot(self):
        return self.stocks

    def fetch_market(self, stocks=None):
        return {
            "indices": [{"name": "上证指数", "code": "000001", "change": -0.5}],
            "equalWeightedIndices": [{"name": "全市场等权", "change": -1.0}],
            "breadth": {"up": 1, "down": 2},
            "marketIndexStatus": {"available": True, "source": "sina"},
        }


class MarketContextStoreTest(unittest.TestCase):
    def test_auto_provider_order_uses_three_independent_real_sources(self):
        store = FullMarketContextStore(
            sina_provider=_Provider([]), akshare_provider=_Provider([]), tencent_provider=_Provider([])
        )
        self.assertEqual(store._provider_order("auto"), ["sina", "tencent", "akshare"])

    def test_decision_quality_is_independent_from_storage_status(self):
        stocks = [
            RawStock(f"{index:06d}", f"测试{index}", "沪市主板", "", 10, 1, 1, 1, 1, 1, 0, 50, 50, amount=100)
            for index in range(1, 4)
        ]
        with patch.object(module, "FULL_MARKET_DECISION_MIN_ROWS", 3):
            quality = module._snapshot_decision_quality(
                stocks=stocks,
                source="sina",
                trade_date="2026-11-12",
                captured_at="2026-11-12T14:20:00",
                requested_trade_date="2026-11-12",
                scan_time="14:40",
                reference_codes={stock.code for stock in stocks},
                reference_version="fixture-v1",
            )
        self.assertTrue(quality["snapshotDecisionUsable"])
        self.assertTrue(quality["storageStatusIndependent"])
        self.assertEqual(quality["referenceCoveragePct"], 100.0)
        self.assertEqual(quality["criticalFieldValidPct"], 100.0)

    def test_tencent_uses_local_snapshot_universe_without_calling_sina(self):
        stocks = [
            RawStock("000001", "甲", "沪市主板", "", 10, 1, 1, 1, 1, 1, 0, 50, 50),
            RawStock("000002", "乙", "沪市主板", "", 11, 1, 1, 1, 1, 1, 0, 50, 50),
        ]

        class Tencent(_Provider):
            def fetch_codes_parallel(self, codes, max_workers=1, batch_size=1):
                self.requested_codes = list(codes)
                return stocks

        class Sina(_Provider):
            def fetch_snapshot(self):
                raise AssertionError("腾讯备用源不得再次依赖新浪拉代码清单")

        tencent = Tencent(stocks)
        sina = Sina([])
        with tempfile.TemporaryDirectory() as tempdir, patch.object(
            module, "FULL_MARKET_CONTEXT_CACHE_PATH", Path(tempdir) / "cache.json"
        ), patch.object(module, "FULL_MARKET_CONTEXT_SNAPSHOT_DIR", Path(tempdir) / "snapshots"), patch.object(
            module, "FULL_MARKET_CONTEXT_MIN_ROWS", 2
        ):
            snapshot_dir = Path(tempdir) / "snapshots"
            snapshot_dir.mkdir()
            (snapshot_dir / f"{date.today().isoformat()}-fixture.json").write_text(
                json.dumps({
                    "version": module.FULL_MARKET_CONTEXT_CACHE_VERSION,
                    "snapshotVersion": f"{date.today().isoformat()}-fixture",
                    "source": "sina",
                    "stocks": [stock.to_dict() for stock in stocks],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            store = FullMarketContextStore(sina_provider=sina, akshare_provider=sina, tencent_provider=tencent)
            result = store._fetch_tencent_full_market()
        self.assertEqual([stock.code for stock in result], ["000001", "000002"])
        self.assertEqual(tencent.requested_codes, ["000001", "000002"])

    def test_latest_for_scan_uses_pre_cutoff_partial_snapshot_and_rejects_later_snapshot(self):
        stocks = [
            RawStock("000001", "甲", "沪市主板", "", 10, 1, 1, 1, 1, 1, 0, 50, 50, amount=100),
            RawStock("000002", "乙", "沪市主板", "", 11, 1, 1, 1, 1, 1, 0, 50, 50, amount=100),
        ]
        trade_date = date.today().isoformat()
        with tempfile.TemporaryDirectory() as tempdir, patch.object(
            module, "FULL_MARKET_CONTEXT_CACHE_PATH", Path(tempdir) / "cache.json"
        ), patch.object(module, "FULL_MARKET_CONTEXT_SNAPSHOT_DIR", Path(tempdir) / "snapshots"), patch.object(
            module, "FULL_MARKET_DECISION_MIN_ROWS", 2
        ):
            snapshot_dir = Path(tempdir) / "snapshots"
            snapshot_dir.mkdir()
            for label, captured in (("before", f"{trade_date}T14:39:00"), ("after", f"{trade_date}T14:41:00")):
                (snapshot_dir / f"{trade_date}-{label}.json").write_text(
                    json.dumps({
                        "version": module.FULL_MARKET_CONTEXT_CACHE_VERSION,
                        "snapshotVersion": f"{trade_date}-{label}",
                        "status": "partial",
                        "tradeDate": trade_date,
                        "source": "sina",
                        "asOfTime": captured,
                        "capturedAt": captured,
                        "generatedAt": captured,
                        "stocks": [stock.to_dict() for stock in stocks],
                    }, ensure_ascii=False),
                    encoding="utf-8",
                )
            chosen = FullMarketContextStore().latest_for_scan(trade_date, "14:40", "auto")
        self.assertEqual(chosen["snapshotVersion"], f"{trade_date}-before")
        self.assertEqual(chosen["status"], "partial")
        self.assertTrue(chosen["snapshotDecisionUsable"])

    def test_refresh_preserves_provider_indices_in_cache(self):
        stock = RawStock("000001", "测试", "沪市主板", "", 10, -1, 1, 1, 1, 1, 0, 50, 50)
        provider = _Provider([stock])
        with tempfile.TemporaryDirectory() as tempdir, patch.object(module, "FULL_MARKET_CONTEXT_CACHE_PATH", Path(tempdir) / "cache.json"), patch.object(
            module, "FULL_MARKET_CONTEXT_SNAPSHOT_DIR", Path(tempdir) / "snapshots"
        ), patch.object(module, "FULL_MARKET_CONTEXT_MIN_ROWS", 1), patch.object(
            module, "FULL_MARKET_DECISION_MIN_ROWS", 1
        ), patch.object(module, "FULL_MARKET_CONTEXT_EXPECTED_ROWS", 1):
            store = FullMarketContextStore(sina_provider=provider, akshare_provider=provider, tencent_provider=provider)
            payload = store.refresh("sina")
            self.assertEqual(payload["market"]["indices"][0]["name"], "上证指数")
            self.assertTrue(payload["market"]["marketIndexStatus"]["available"])
            self.assertEqual(payload["market"]["breadth"], {"up": 1, "down": 2})
            self.assertEqual(store._reference_universe_cache[0], {"000001"})


if __name__ == "__main__":
    unittest.main()
