from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, List

from backend.app.database import Database
from backend.app.engines.filter_engine import in_confirmed_universe
from backend.app.providers.akshare_provider import AKShareProvider
from backend.app.providers.baostock_provider import BaostockProvider
from backend.app.providers.daily_kline_provider import DailyKlineProvider
from backend.app.providers.minute_kline_provider import MinuteKlineProvider
from backend.app.providers.mock_provider import MockMarketProvider
from backend.app.providers.sina_provider import SinaProvider
from backend.app.providers.tencent_provider import TencentProvider
from backend.app.providers.tushare_provider import TushareProvider
from backend.app.schema import RawStock
from backend.app.market_context_store import FullMarketContextStore, market_context_meta
from backend.app.settings import Settings
from backend.app.universe_store import (
    DEFAULT_DYNAMIC_UNIVERSE_SIZE,
    build_dynamic_universe,
    is_universe_fresh,
    save_dynamic_universe,
    universe_response,
)


LOGGER = logging.getLogger("ai_stock.analysis")


class AnalysisService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.mock_provider = MockMarketProvider()
        self.akshare_provider = AKShareProvider()
        self.sina_provider = SinaProvider()
        self.tencent_provider = TencentProvider()
        self.kline_provider = MinuteKlineProvider()
        self.tushare_provider = TushareProvider(settings.tushare_token)
        self.baostock_provider = BaostockProvider()
        self.daily_kline_provider = DailyKlineProvider(settings.tushare_token)
        self.full_market_context_store = FullMarketContextStore(
            tencent_provider=self.tencent_provider,
            sina_provider=self.sina_provider,
            akshare_provider=self.akshare_provider,
        )

    def _save_daily_kline_to_database(self, series_by_code: Dict[str, List[Dict[str, Any]]], source: str) -> int:
        saved_rows = 0
        for code, series in series_by_code.items():
            saved_rows += self.database.upsert_daily_kline_series(code, series, source=source)
        if series_by_code:
            LOGGER.info(
                "daily kline db saved codes=%s rows=%s source=%s",
                len(series_by_code),
                saved_rows,
                source,
            )
        return saved_rows

    def refresh_default_universe(
        self,
        source: str = "auto",
        max_size: int = DEFAULT_DYNAMIC_UNIVERSE_SIZE,
        force: bool = True,
    ) -> Dict[str, Any]:
        if not force and is_universe_fresh():
            response = universe_response()
            response["updated"] = False
            response["providerWarning"] = ""
            return response

        warnings = []
        for provider in self._universe_provider_order(source):
            started = time.perf_counter()
            try:
                LOGGER.info("default universe refresh start source=%s", provider.source)
                stocks = provider.fetch_snapshot()
                items = build_dynamic_universe(
                    stocks,
                    provider.source,
                    max_size=max_size,
                    include_bse=self.settings.include_bse,
                    include_star_market=self.settings.include_star_market,
                )
                response = save_dynamic_universe(items, provider.source, len(stocks))
                response["updated"] = True
                response["providerWarning"] = "；".join(warnings)
                LOGGER.info(
                    "default universe refresh done source=%s snapshot=%s count=%s elapsed_ms=%.0f",
                    provider.source,
                    len(stocks),
                    response.get("count", 0),
                    (time.perf_counter() - started) * 1000,
                )
                return response
            except Exception as error:  # noqa: BLE001 - keep the last cached universe available.
                LOGGER.warning(
                    "default universe refresh failed source=%s elapsed_ms=%.0f error=%s",
                    provider.source,
                    (time.perf_counter() - started) * 1000,
                    error,
                )
                warnings.append(f"{provider.source} 更新失败：{error}")

        response = universe_response()
        response["updated"] = False
        response["providerWarning"] = "；".join(warnings) if warnings else "没有可用的全市场源可更新重点池"
        return response

    def _apply_scope(self, stocks: List[RawStock], scope: str) -> List[RawStock]:
        if scope != "watchlist":
            return stocks
        watch_codes = {item["code"] for item in self.database.list_watchlist()}
        if not watch_codes:
            return stocks
        return [stock for stock in stocks if stock.code in watch_codes]

    def _load_market_data(self, source: str = "auto") -> tuple[Any, Dict[str, Any], List[RawStock], str]:
        source = source or "auto"
        warnings = []
        providers = self._provider_order(source)
        LOGGER.info("provider order requested=%s order=%s", source, ",".join(provider.source for provider in providers))
        for provider in providers:
            started = time.perf_counter()
            try:
                LOGGER.info("provider fetch start source=%s", provider.source)
                if provider.source == "mock":
                    raw_stocks = provider.fetch_snapshot()
                    market = provider.fetch_market()
                    warning = "；".join(warnings + (["已回退模拟数据"] if warnings else []))
                    LOGGER.info(
                        "provider fetch ok source=%s raw=%s elapsed_ms=%.0f warning=%s",
                        provider.source,
                        len(raw_stocks),
                        (time.perf_counter() - started) * 1000,
                        warning,
                    )
                    return provider, market, raw_stocks, warning
                raw_stocks = provider.fetch_snapshot()
                market_stocks = self._confirmed_universe(raw_stocks)
                market = provider.fetch_market(market_stocks)
                warning = "；".join(warnings + ([f"已使用 {provider.source} 数据源"] if warnings else []))
                LOGGER.info(
                    "provider fetch ok source=%s raw=%s confirmed=%s elapsed_ms=%.0f warning=%s",
                    provider.source,
                    len(raw_stocks),
                    len(market_stocks),
                    (time.perf_counter() - started) * 1000,
                    warning,
                )
                return provider, market, raw_stocks, warning
            except Exception as error:  # noqa: BLE001 - provider failures should not break local UI.
                LOGGER.warning(
                    "provider fetch failed source=%s elapsed_ms=%.0f error=%s",
                    provider.source,
                    (time.perf_counter() - started) * 1000,
                    error,
                )
                warnings.append(f"{provider.source} 获取失败：{error}")

        started = time.perf_counter()
        market = self.mock_provider.fetch_market()
        raw_stocks = self.mock_provider.fetch_snapshot()
        LOGGER.warning(
            "provider fallback mock raw=%s elapsed_ms=%.0f warning=%s",
            len(raw_stocks),
            (time.perf_counter() - started) * 1000,
            "；".join(warnings + ["已回退模拟数据"]),
        )
        return self.mock_provider, market, raw_stocks, "；".join(warnings + ["已回退模拟数据"])

    def _load_daily_market_data(self, source: str = "auto") -> tuple[Any, Dict[str, Any], List[RawStock], str]:
        requested_source = source or "auto"
        if requested_source == "mock":
            return self._load_market_data("mock")

        context = self.full_market_context_store.latest_or_refresh(source=requested_source, force=False)
        stocks = context.get("stocks") or []
        if not stocks:
            raise RuntimeError("全市场上下文为空")

        market = dict(context.get("market") or {})
        meta = market_context_meta(context)
        warning = str(context.get("warning") or "")
        market.update(
            {
                "context": meta,
                "contextScope": "full_market",
                "contextLabel": "全市场缓存",
                "contextRows": len(stocks),
                "contextSource": meta.get("source", ""),
                "contextGeneratedAt": meta.get("generatedAt", ""),
                "contextCacheHit": meta.get("cacheHit", False),
                "contextStale": meta.get("stale", False),
                "contextWarning": meta.get("warning", ""),
                "snapshotMode": "full_market_context",
            }
        )
        provider = SimpleNamespace(source=meta.get("source") or requested_source, last_meta={"mode": "full_market_context", "rows": len(stocks)})
        return provider, market, stocks, warning

    def _attach_kline_series(self, stocks: List[RawStock]) -> int:
        if not stocks:
            return 0
        series_by_code = self.kline_provider.fetch_many([stock.code for stock in stocks], period=self.settings.kline_period)
        attached = 0
        for stock in stocks:
            series = series_by_code.get(stock.code)
            if not series:
                continue
            stock.kline_series = series
            source_for_code = getattr(self.kline_provider, "source_for_code", None)
            stock.kline_source = source_for_code(stock.code) if callable(source_for_code) else self.kline_provider.source
            stock.kline_period = self.settings.kline_period
            attached += 1
        return attached

    def _provider_order(self, source: str) -> List[Any]:
        registry = {
            "akshare": self.akshare_provider,
            "sina": self.sina_provider,
            "tencent": self.tencent_provider,
            "tushare": self.tushare_provider,
            "baostock": self.baostock_provider,
            "mock": self.mock_provider,
        }
        if source in registry and source != "auto":
            if source == "mock":
                return [self.mock_provider]
            ordered = [
                registry[source],
                self.tencent_provider,
                self.sina_provider,
                self.akshare_provider,
                self.tushare_provider,
                self.baostock_provider,
                self.mock_provider,
            ]
            deduped = []
            seen = set()
            for provider in ordered:
                if provider.source in seen:
                    continue
                seen.add(provider.source)
                deduped.append(provider)
            return deduped
        return [
            self.sina_provider,
            self.tencent_provider,
            self.akshare_provider,
            self.tushare_provider,
            self.baostock_provider,
            self.mock_provider,
        ]

    def _universe_provider_order(self, source: str) -> List[Any]:
        registry = {
            "sina": self.sina_provider,
            "akshare": self.akshare_provider,
        }
        if source in registry:
            return [registry[source]]
        return [self.sina_provider, self.akshare_provider]

    def _confirmed_universe(self, stocks: List[RawStock]) -> List[RawStock]:
        return [
            stock
            for stock in stocks
            if in_confirmed_universe(
                stock.code,
                include_bse=self.settings.include_bse,
                include_star_market=self.settings.include_star_market,
            )
        ]
