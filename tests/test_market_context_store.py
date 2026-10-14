import tempfile
import unittest
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
    def test_refresh_preserves_provider_indices_in_cache(self):
        stock = RawStock("000001", "测试", "沪市主板", "", 10, -1, 1, 1, 1, 1, 0, 50, 50)
        provider = _Provider([stock])
        with tempfile.TemporaryDirectory() as tempdir, patch.object(module, "FULL_MARKET_CONTEXT_CACHE_PATH", Path(tempdir) / "cache.json"), patch.object(
            module, "FULL_MARKET_CONTEXT_SNAPSHOT_DIR", Path(tempdir) / "snapshots"
        ), patch.object(module, "FULL_MARKET_CONTEXT_MIN_ROWS", 1), patch.object(module, "FULL_MARKET_CONTEXT_EXPECTED_ROWS", 1):
            store = FullMarketContextStore(sina_provider=provider, akshare_provider=provider, tencent_provider=provider)
            payload = store.refresh("sina")
            self.assertEqual(payload["market"]["indices"][0]["name"], "上证指数")
            self.assertTrue(payload["market"]["marketIndexStatus"]["available"])
            self.assertEqual(payload["market"]["breadth"], {"up": 1, "down": 2})


if __name__ == "__main__":
    unittest.main()
