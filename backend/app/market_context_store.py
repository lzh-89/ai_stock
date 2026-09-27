from __future__ import annotations

import json
import logging
import os
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from backend.app.providers.akshare_provider import AKShareProvider
from backend.app.providers.provider_common import market_from_stocks
from backend.app.providers.sina_provider import SinaProvider
from backend.app.providers.tencent_provider import TencentProvider
from backend.app.schema import Catalyst, RawStock


LOGGER = logging.getLogger("ai_stock.market_context")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FULL_MARKET_CONTEXT_CACHE_PATH = PROJECT_ROOT / "data" / "full_market_context_cache.json"
FULL_MARKET_CONTEXT_CACHE_VERSION = 1
FULL_MARKET_CONTEXT_TTL_SECONDS = int(os.getenv("FULL_MARKET_CONTEXT_TTL_SECONDS", str(60 * 60)))
FULL_MARKET_CONTEXT_MIN_ROWS = int(os.getenv("FULL_MARKET_CONTEXT_MIN_ROWS", "1200"))
TENCENT_FULL_MARKET_MAX_WORKERS = int(os.getenv("TENCENT_FULL_MARKET_MAX_WORKERS", "8"))
TENCENT_FULL_MARKET_BATCH_SIZE = int(os.getenv("TENCENT_FULL_MARKET_BATCH_SIZE", "60"))


class FullMarketContextStore:
    def __init__(
        self,
        tencent_provider: TencentProvider | None = None,
        sina_provider: SinaProvider | None = None,
        akshare_provider: AKShareProvider | None = None,
    ) -> None:
        self.tencent_provider = tencent_provider or TencentProvider()
        self.sina_provider = sina_provider or SinaProvider()
        self.akshare_provider = akshare_provider or AKShareProvider()

    def latest_or_refresh(self, source: str = "auto", force: bool = False) -> Dict[str, Any]:
        cached = self._read_cache()
        if cached and not force and not _is_stale(cached):
            return _runtime_payload(cached, cache_hit=True, stale=False)
        try:
            return self.refresh(source=source or "auto")
        except Exception as error:  # noqa: BLE001 - stale full-market context is better than losing stock analysis.
            if cached:
                LOGGER.warning("full market context refresh failed, using stale cache error=%s", error)
                payload = _runtime_payload(cached, cache_hit=True, stale=True)
                payload["warning"] = f"全市场刷新失败，已使用旧缓存：{error}"
                return payload
            raise

    def latest_cached(self) -> Dict[str, Any]:
        cached = self._read_cache()
        if not cached:
            return {}
        return _runtime_payload(cached, cache_hit=True, stale=_is_stale(cached))

    def refresh(self, source: str = "auto") -> Dict[str, Any]:
        requested_source = source or "auto"
        warnings: List[str] = []
        for provider_name in self._provider_order(requested_source):
            started = datetime.now()
            try:
                LOGGER.info("full market context refresh start requested=%s provider=%s", requested_source, provider_name)
                stocks = self._fetch_by_provider(provider_name)
                if len(stocks) < FULL_MARKET_CONTEXT_MIN_ROWS:
                    raise RuntimeError(f"全市场快照样本过少：{len(stocks)}")
                payload = self._write_cache(
                    stocks=stocks,
                    requested_source=requested_source,
                    actual_source=provider_name,
                    warning="；".join(warnings),
                )
                elapsed_ms = (datetime.now() - started).total_seconds() * 1000
                LOGGER.info(
                    "full market context refresh done requested=%s provider=%s rows=%s elapsed_ms=%.0f",
                    requested_source,
                    provider_name,
                    len(stocks),
                    elapsed_ms,
                )
                return payload
            except Exception as error:  # noqa: BLE001 - try next full-market source.
                elapsed_ms = (datetime.now() - started).total_seconds() * 1000
                LOGGER.warning(
                    "full market context refresh failed requested=%s provider=%s elapsed_ms=%.0f error=%s",
                    requested_source,
                    provider_name,
                    elapsed_ms,
                    error,
                )
                warnings.append(f"{provider_name} 全市场刷新失败：{error}")
        raise RuntimeError("；".join(warnings) if warnings else "没有可用的全市场行情源")

    def public_latest(self, source: str = "auto", force: bool = False) -> Dict[str, Any]:
        return market_context_public_response(self.latest_or_refresh(source=source, force=force))

    def _fetch_by_provider(self, provider_name: str) -> List[RawStock]:
        if provider_name == "sina":
            return self.sina_provider.fetch_snapshot()
        if provider_name == "akshare":
            return self.akshare_provider.fetch_snapshot()
        if provider_name == "tencent":
            return self._fetch_tencent_full_market()
        raise RuntimeError(f"不支持的全市场行情源：{provider_name}")

    def _fetch_tencent_full_market(self) -> List[RawStock]:
        code_items = self._full_market_code_items_from_cache()
        if len(code_items) < FULL_MARKET_CONTEXT_MIN_ROWS:
            LOGGER.info("tencent full market seed codes from sina because cached_codes=%s", len(code_items))
            seed_stocks = self.sina_provider.fetch_snapshot()
            code_items = [{"code": stock.code, "name": stock.name} for stock in seed_stocks]
        codes = [item["code"] for item in code_items]
        if len(codes) < FULL_MARKET_CONTEXT_MIN_ROWS:
            raise RuntimeError(f"腾讯全市场缺少股票代码清单：{len(codes)}")
        return self.tencent_provider.fetch_codes_parallel(
            codes,
            max_workers=TENCENT_FULL_MARKET_MAX_WORKERS,
            batch_size=TENCENT_FULL_MARKET_BATCH_SIZE,
        )

    def _full_market_code_items_from_cache(self) -> List[Dict[str, str]]:
        cached = self._read_cache()
        result = []
        seen = set()
        for item in cached.get("stocks") or []:
            code = str(item.get("code", "")).strip().zfill(6)
            name = str(item.get("name", "")).strip()
            if not code or code in seen:
                continue
            seen.add(code)
            result.append({"code": code, "name": name or code})
        return result

    def _provider_order(self, requested_source: str) -> List[str]:
        registry = {"sina", "tencent", "akshare"}
        if requested_source in registry:
            ordered = [requested_source, "sina", "tencent", "akshare"]
        else:
            ordered = ["sina", "tencent", "akshare"]
        result = []
        seen = set()
        for item in ordered:
            if item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result

    def _read_cache(self) -> Dict[str, Any]:
        if not FULL_MARKET_CONTEXT_CACHE_PATH.exists():
            return {}
        try:
            with FULL_MARKET_CONTEXT_CACHE_PATH.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        if int(payload.get("version") or 0) != FULL_MARKET_CONTEXT_CACHE_VERSION:
            return {}
        return payload

    def _write_cache(self, stocks: List[RawStock], requested_source: str, actual_source: str, warning: str = "") -> Dict[str, Any]:
        generated_at = datetime.now().isoformat(timespec="seconds")
        market = market_from_stocks(stocks)
        payload = {
            "version": FULL_MARKET_CONTEXT_CACHE_VERSION,
            "generatedAt": generated_at,
            "requestedSource": requested_source,
            "source": actual_source,
            "ttlSeconds": FULL_MARKET_CONTEXT_TTL_SECONDS,
            "count": len(stocks),
            "warning": warning,
            "market": market,
            "stocks": [stock.to_dict() for stock in stocks],
        }
        FULL_MARKET_CONTEXT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = FULL_MARKET_CONTEXT_CACHE_PATH.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
        tmp_path.replace(FULL_MARKET_CONTEXT_CACHE_PATH)
        return _runtime_payload(payload, cache_hit=False, stale=False)


def market_context_public_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": payload.get("source", ""),
        "requestedSource": payload.get("requestedSource", ""),
        "generatedAt": payload.get("generatedAt", ""),
        "count": payload.get("count", 0),
        "ttlSeconds": payload.get("ttlSeconds", FULL_MARKET_CONTEXT_TTL_SECONDS),
        "ageSeconds": payload.get("ageSeconds", 0),
        "cacheHit": bool(payload.get("cacheHit")),
        "stale": bool(payload.get("stale")),
        "cachePath": str(FULL_MARKET_CONTEXT_CACHE_PATH),
        "warning": payload.get("warning", ""),
        "market": payload.get("market") or {},
    }


def market_context_meta(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "scope": "full_market",
        "source": payload.get("source", ""),
        "requestedSource": payload.get("requestedSource", ""),
        "generatedAt": payload.get("generatedAt", ""),
        "count": payload.get("count", 0),
        "ttlSeconds": payload.get("ttlSeconds", FULL_MARKET_CONTEXT_TTL_SECONDS),
        "ageSeconds": payload.get("ageSeconds", 0),
        "cacheHit": bool(payload.get("cacheHit")),
        "stale": bool(payload.get("stale")),
        "warning": payload.get("warning", ""),
    }


def _runtime_payload(payload: Dict[str, Any], cache_hit: bool, stale: bool) -> Dict[str, Any]:
    stocks = [_stock_from_dict(item) for item in payload.get("stocks") or []]
    stocks = [stock for stock in stocks if stock is not None]
    generated_at = str(payload.get("generatedAt") or "")
    age_seconds = _age_seconds(generated_at)
    market = payload.get("market") if isinstance(payload.get("market"), dict) else {}
    if not market and stocks:
        market = market_from_stocks(stocks)
    return {
        "source": payload.get("source", ""),
        "requestedSource": payload.get("requestedSource", ""),
        "generatedAt": generated_at,
        "count": len(stocks) or int(payload.get("count") or 0),
        "ttlSeconds": int(payload.get("ttlSeconds") or FULL_MARKET_CONTEXT_TTL_SECONDS),
        "ageSeconds": age_seconds,
        "cacheHit": cache_hit,
        "stale": stale,
        "warning": payload.get("warning", ""),
        "market": market,
        "stocks": stocks,
    }


def _stock_from_dict(item: Any) -> RawStock | None:
    if not isinstance(item, dict):
        return None
    valid_fields = {field.name for field in fields(RawStock)}
    kwargs = {key: value for key, value in item.items() if key in valid_fields and key != "catalysts"}
    catalysts = []
    for catalyst in item.get("catalysts") or []:
        if not isinstance(catalyst, dict):
            continue
        catalysts.append(
            Catalyst(
                str(catalyst.get("type", "")),
                str(catalyst.get("time", "")),
                str(catalyst.get("title", "")),
                str(catalyst.get("url", "")),
            )
        )
    kwargs["catalysts"] = catalysts
    try:
        return RawStock(**kwargs)
    except TypeError:
        return None


def _is_stale(payload: Dict[str, Any]) -> bool:
    generated_at = str(payload.get("generatedAt") or "")
    age_seconds = _age_seconds(generated_at)
    ttl_seconds = int(payload.get("ttlSeconds") or FULL_MARKET_CONTEXT_TTL_SECONDS)
    return age_seconds < 0 or age_seconds >= ttl_seconds


def _age_seconds(value: str) -> int:
    if not value:
        return -1
    try:
        generated = datetime.fromisoformat(value)
    except ValueError:
        return -1
    return max(0, int((datetime.now() - generated).total_seconds()))
