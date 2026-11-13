from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import threading
import uuid
from dataclasses import fields
from datetime import datetime, date
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
FULL_MARKET_CONTEXT_SNAPSHOT_DIR = PROJECT_ROOT / "data" / "full_market_context_snapshots"
FULL_MARKET_CONTEXT_CACHE_VERSION = 2
FULL_MARKET_CONTEXT_TTL_SECONDS = int(os.getenv("FULL_MARKET_CONTEXT_TTL_SECONDS", str(60 * 60)))
FULL_MARKET_CONTEXT_MIN_ROWS = int(os.getenv("FULL_MARKET_CONTEXT_MIN_ROWS", "1200"))
FULL_MARKET_CONTEXT_EXPECTED_ROWS = int(os.getenv("FULL_MARKET_CONTEXT_EXPECTED_ROWS", "5000"))
FULL_MARKET_DECISION_MIN_ROWS = int(os.getenv("FULL_MARKET_DECISION_MIN_ROWS", "4500"))
FULL_MARKET_DECISION_MIN_COVERAGE_PCT = float(os.getenv("FULL_MARKET_DECISION_MIN_COVERAGE_PCT", "90"))
FULL_MARKET_DECISION_MIN_CRITICAL_PCT = float(os.getenv("FULL_MARKET_DECISION_MIN_CRITICAL_PCT", "98"))
FULL_MARKET_DECISION_MAX_AGE_SECONDS = int(os.getenv("FULL_MARKET_DECISION_MAX_AGE_SECONDS", "1800"))
FULL_MARKET_CONTEXT_SNAPSHOT_RETAIN_DAYS = int(os.getenv("FULL_MARKET_CONTEXT_SNAPSHOT_RETAIN_DAYS", "10"))
TENCENT_FULL_MARKET_MAX_WORKERS = int(os.getenv("TENCENT_FULL_MARKET_MAX_WORKERS", "8"))
TENCENT_FULL_MARKET_BATCH_SIZE = int(os.getenv("TENCENT_FULL_MARKET_BATCH_SIZE", "60"))
_PUBLISH_LOCK = threading.Lock()


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
        self._reference_universe_cache: tuple[set[str], str] | None = None

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

    def prefetch(self, source: str = "auto", force: bool = False) -> Dict[str, Any]:
        """Refresh and atomically publish a full-market snapshot for T+1."""
        cached = self._read_cache()
        if cached and not force and _is_same_day_ready_snapshot(cached):
            return _runtime_payload(cached, cache_hit=True, stale=False)
        return self.refresh(source=source)

    def latest_for_scan(self, trade_date: str, scan_time: str, source: str = "auto") -> Dict[str, Any]:
        """Select a published snapshot whose data predates the formal scan."""
        cutoff = f"{trade_date}T{scan_time}:59"
        snapshots = [self._read_cache()]
        for path in FULL_MARKET_CONTEXT_SNAPSHOT_DIR.glob(f"{trade_date}-*.json"):
            snapshots.append(self._read_payload(path))
        reference_codes = {
            str(stock.get("code") or "").strip().zfill(6)
            for item in snapshots
            if item
            and str(item.get("source") or "") in {"sina", "tencent", "akshare", "eastmoney"}
            and str(item.get("asOfTime") or "") <= cutoff
            and str(item.get("capturedAt") or "") <= cutoff
            for stock in (item.get("stocks") or [])
            if isinstance(stock, dict) and str(stock.get("code") or "").strip()
        }
        reference_version = ""
        if len(reference_codes) >= FULL_MARKET_DECISION_MIN_ROWS:
            digest = hashlib.sha256("|".join(sorted(reference_codes)).encode("utf-8")).hexdigest()[:12]
            reference_version = f"same-day-snapshot-union-{trade_date}-{digest}"
        candidates = []
        for item in snapshots:
            if not item or str(item.get("tradeDate") or "") != trade_date:
                continue
            if str(item.get("asOfTime") or "") > cutoff or str(item.get("capturedAt") or "") > cutoff:
                continue
            if source != "auto" and str(item.get("source") or "") != source:
                continue
            quality = self.decision_quality(
                item,
                trade_date=trade_date,
                scan_time=scan_time,
                reference_codes=reference_codes,
                reference_version=reference_version,
            )
            if quality.get("snapshotDecisionUsable"):
                item = dict(item)
                item["decisionQuality"] = quality
                item["snapshotDecisionUsable"] = True
                candidates.append(item)
        if not candidates:
            return {}
        chosen = max(candidates, key=lambda item: (str(item.get("asOfTime") or ""), item.get("status") == "ready"))
        payload = _runtime_payload(chosen, cache_hit=True, stale=_is_stale(chosen))
        payload["stale"] = payload["stale"] or _age_seconds(payload.get("generatedAt") or "") > FULL_MARKET_CONTEXT_TTL_SECONDS
        return payload

    def decision_quality(
        self,
        payload: Dict[str, Any],
        trade_date: str,
        scan_time: str,
        reference_codes: set[str] | None = None,
        reference_version: str = "",
    ) -> Dict[str, Any]:
        stocks = payload.get("stocks") or []
        runtime_stocks = [item if isinstance(item, RawStock) else _stock_from_dict(item) for item in stocks]
        runtime_stocks = [item for item in runtime_stocks if item is not None]
        if reference_codes is None:
            reference_codes, reference_version = self._reference_universe_codes()
        return _snapshot_decision_quality(
            stocks=runtime_stocks,
            source=str(payload.get("source") or ""),
            trade_date=str(payload.get("tradeDate") or ""),
            captured_at=str(payload.get("capturedAt") or payload.get("asOfTime") or payload.get("generatedAt") or ""),
            requested_trade_date=trade_date,
            scan_time=scan_time,
            reference_codes=reference_codes,
            reference_version=reference_version,
        )

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
                market = self._fetch_market_by_provider(provider_name, stocks)
                provider_meta = self._provider_meta(provider_name)
                payload = self._write_cache(
                    stocks=stocks,
                    market=market,
                    requested_source=requested_source,
                    actual_source=provider_name,
                    warning="；".join(warnings),
                    provider_meta=provider_meta,
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

    def _provider_meta(self, provider_name: str) -> Dict[str, Any]:
        provider = {
            "sina": self.sina_provider,
            "tencent": self.tencent_provider,
            "akshare": self.akshare_provider,
        }.get(provider_name)
        return dict(getattr(provider, "last_meta", {}) or {})

    def _fetch_market_by_provider(self, provider_name: str, stocks: List[RawStock]) -> Dict[str, Any]:
        provider = {
            "sina": self.sina_provider,
            "tencent": self.tencent_provider,
            "akshare": self.akshare_provider,
        }.get(provider_name)
        if provider is None:
            raise RuntimeError(f"不支持的全市场行情源：{provider_name}")
        market = provider.fetch_market(stocks)
        if not isinstance(market, dict):
            raise RuntimeError(f"{provider_name} 市场环境返回格式无效")
        return market

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
            code_items = self._full_market_code_items_from_history()
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

    def _full_market_code_items_from_history(self) -> List[Dict[str, str]]:
        best: List[Dict[str, str]] = []
        for path in sorted(FULL_MARKET_CONTEXT_SNAPSHOT_DIR.glob("*.json"), reverse=True)[:30]:
            payload = self._read_payload(path)
            rows = payload.get("stocks") or []
            if len(rows) <= len(best):
                continue
            seen = set()
            candidate = []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code") or "").strip().zfill(6)
                if not code or code in seen:
                    continue
                seen.add(code)
                candidate.append({"code": code, "name": str(item.get("name") or code)})
            if len(candidate) > len(best):
                best = candidate
        return best

    def _reference_universe_codes(self) -> tuple[set[str], str]:
        if self._reference_universe_cache is not None:
            return self._reference_universe_cache
        recent_paths = sorted(FULL_MARKET_CONTEXT_SNAPSHOT_DIR.glob("*.json"), reverse=True)[:30]
        for path in [None, *recent_paths]:
            payload = self._read_cache() if path is None else self._read_payload(path)
            if not payload or str(payload.get("source") or "") not in {"sina", "tencent", "akshare", "eastmoney"}:
                continue
            codes = {
                str(item.get("code") or "").strip().zfill(6)
                for item in payload.get("stocks") or []
                if isinstance(item, dict) and str(item.get("code") or "").strip()
            }
            if len(codes) < FULL_MARKET_DECISION_MIN_ROWS:
                continue
            digest = hashlib.sha256("|".join(sorted(codes)).encode("utf-8")).hexdigest()[:12]
            version = str(payload.get("snapshotVersion") or "history")
            self._reference_universe_cache = (codes, f"snapshot-universe-{version}-{digest}")
            return self._reference_universe_cache
        self._reference_universe_cache = (set(), "")
        return self._reference_universe_cache

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
        return self._read_payload(FULL_MARKET_CONTEXT_CACHE_PATH)

    def _read_payload(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        if int(payload.get("version") or 0) not in {1, FULL_MARKET_CONTEXT_CACHE_VERSION}:
            return {}
        return payload

    def _write_cache(
        self,
        stocks: List[RawStock],
        market: Dict[str, Any],
        requested_source: str,
        actual_source: str,
        warning: str = "",
        provider_meta: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        generated_at = datetime.now().isoformat(timespec="seconds")
        provider_meta = dict(provider_meta or {})
        failed_pages = int(provider_meta.get("pagesFailed") or 0)
        # A received/cache code list is not a certified market universe.
        universe = {str(c).zfill(6) for c in provider_meta.get("universeCodes") or []}
        certified = bool(universe and provider_meta.get("universeVersion") and provider_meta.get("universeAuthoritative"))
        received = {s.code for s in stocks}
        expected_rows = len(universe) if certified else None
        coverage_pct = round(len(received & universe) / len(universe) * 100, 2) if certified else None
        status = "ready" if certified and coverage_pct >= 90 and not failed_pages else "partial"
        reference_codes, reference_version = self._reference_universe_codes()
        if not reference_codes and certified:
            reference_codes = universe
            reference_version = str(provider_meta.get("universeVersion") or "")
        decision_quality = _snapshot_decision_quality(
            stocks=stocks,
            source=actual_source,
            trade_date=date.today().isoformat(),
            captured_at=generated_at,
            requested_trade_date=date.today().isoformat(),
            scan_time="14:40",
            reference_codes=reference_codes,
            reference_version=reference_version,
        )
        payload = {
            "version": FULL_MARKET_CONTEXT_CACHE_VERSION,
            "snapshotVersion": f"{date.today().isoformat()}-{uuid.uuid4().hex[:12]}",
            "status": status,
            "tradeDate": date.today().isoformat(),
            "asOfTime": generated_at,
            "capturedAt": generated_at,
            "publishedAt": generated_at,
            "generatedAt": generated_at,
            "requestedSource": requested_source,
            "source": actual_source,
            "ttlSeconds": FULL_MARKET_CONTEXT_TTL_SECONDS,
            "count": len(stocks),
            "coverage": {
                "rows": len(stocks),
                "expectedRows": expected_rows,
                "coveragePct": coverage_pct,
                "status": "verified" if certified and not failed_pages else "partial" if certified else "unknown",
                "version": "2026-10-16.universe.v1",
                "universeVersion": provider_meta.get("universeVersion") if certified else None,
                "reason": "以权威代码全集核验；失败页单独记录" if certified else "未取得带版本的权威代码全集，不能证明全市场覆盖率",
                "pagesOk": int(provider_meta.get("pagesOk") or 0),
                "pagesFailed": failed_pages,
            },
            "failedPages": provider_meta.get("failures") or [],
            "snapshotDecisionUsable": bool(decision_quality.get("snapshotDecisionUsable")),
            "decisionQuality": decision_quality,
            "warning": warning,
            "market": market,
            "stocks": [stock.to_dict() for stock in stocks],
        }
        with _PUBLISH_LOCK:
            FULL_MARKET_CONTEXT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            FULL_MARKET_CONTEXT_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            snapshot_path = FULL_MARKET_CONTEXT_SNAPSHOT_DIR / f"{payload['snapshotVersion']}.json"
            for path in (snapshot_path, FULL_MARKET_CONTEXT_CACHE_PATH):
                tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
                with tmp_path.open("w", encoding="utf-8") as file:
                    json.dump(payload, file, ensure_ascii=False, indent=2)
                    file.write("\n")
                tmp_path.replace(path)
            if len(received) >= FULL_MARKET_DECISION_MIN_ROWS:
                next_reference = set(reference_codes) | set(received)
                digest = hashlib.sha256("|".join(sorted(next_reference)).encode("utf-8")).hexdigest()[:12]
                self._reference_universe_cache = (
                    next_reference,
                    f"snapshot-universe-{payload['snapshotVersion']}-{digest}",
                )
            self._prune_snapshot_history()
        return _runtime_payload(payload, cache_hit=False, stale=False)

    def _prune_snapshot_history(self) -> None:
        """Keep the published snapshot directory bounded for long-running services."""
        cutoff = date.today().toordinal() - max(1, FULL_MARKET_CONTEXT_SNAPSHOT_RETAIN_DAYS)
        for path in FULL_MARKET_CONTEXT_SNAPSHOT_DIR.glob("*.json"):
            try:
                snapshot_date = date.fromisoformat(path.name[:10])
            except ValueError:
                continue
            if snapshot_date.toordinal() < cutoff:
                try:
                    path.unlink()
                except OSError:
                    LOGGER.warning("unable to prune market snapshot path=%s", path)


def market_context_public_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": payload.get("source", ""),
        "requestedSource": payload.get("requestedSource", ""),
        "snapshotVersion": payload.get("snapshotVersion", ""),
        "status": payload.get("status", ""),
        "tradeDate": payload.get("tradeDate", ""),
        "asOfTime": payload.get("asOfTime", payload.get("generatedAt", "")),
        "capturedAt": payload.get("capturedAt", payload.get("generatedAt", "")),
        "generatedAt": payload.get("generatedAt", ""),
        "count": payload.get("count", 0),
        "coverage": payload.get("coverage") or {},
        "failedPages": payload.get("failedPages") or [],
        "snapshotDecisionUsable": bool(payload.get("snapshotDecisionUsable")),
        "decisionQuality": payload.get("decisionQuality") or {},
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
        "snapshotVersion": payload.get("snapshotVersion", ""),
        "status": payload.get("status", ""),
        "tradeDate": payload.get("tradeDate", ""),
        "asOfTime": payload.get("asOfTime", payload.get("generatedAt", "")),
        "capturedAt": payload.get("capturedAt", payload.get("generatedAt", "")),
        "generatedAt": payload.get("generatedAt", ""),
        "count": payload.get("count", 0),
        "coverage": payload.get("coverage") or {},
        "failedPages": payload.get("failedPages") or [],
        "snapshotDecisionUsable": bool(payload.get("snapshotDecisionUsable")),
        "decisionQuality": payload.get("decisionQuality") or {},
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
    coverage = dict(payload.get("coverage") or {})
    if coverage.get("version") != "2026-10-16.universe.v1":
        coverage.update(coveragePct=None, expectedRows=None, status="unknown",
                        version="2026-10-16.universe.v1",
                        reason="旧缓存没有带版本的权威股票全集，覆盖率未知")
    return {
        "source": payload.get("source", ""),
        "requestedSource": payload.get("requestedSource", ""),
        "snapshotVersion": payload.get("snapshotVersion", ""),
        "status": payload.get("status", ""),
        "tradeDate": payload.get("tradeDate", ""),
        "asOfTime": payload.get("asOfTime", generated_at),
        "capturedAt": payload.get("capturedAt", generated_at),
        "generatedAt": generated_at,
        "count": len(stocks) or int(payload.get("count") or 0),
        "ttlSeconds": int(payload.get("ttlSeconds") or FULL_MARKET_CONTEXT_TTL_SECONDS),
        "coverage": coverage,
        "failedPages": payload.get("failedPages") or [],
        "snapshotDecisionUsable": bool(payload.get("snapshotDecisionUsable")),
        "decisionQuality": payload.get("decisionQuality") or {},
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


def _is_same_day_ready_snapshot(payload: Dict[str, Any]) -> bool:
    if str(payload.get("tradeDate") or "") != date.today().isoformat():
        return False
    if str(payload.get("status") or "") != "ready":
        return False
    return not _is_stale(payload)


def _snapshot_decision_quality(
    stocks: List[RawStock],
    source: str,
    trade_date: str,
    captured_at: str,
    requested_trade_date: str,
    scan_time: str,
    reference_codes: set[str],
    reference_version: str,
) -> Dict[str, Any]:
    unique = {
        str(stock.code or "").strip().zfill(6): stock
        for stock in stocks
        if str(stock.code or "").strip()
    }
    received_codes = set(unique)
    critical_valid = 0
    for stock in unique.values():
        try:
            values = (float(stock.price), float(stock.change), float(stock.amount or 0))
            if values[0] > 0 and all(math.isfinite(value) for value in values) and values[2] >= 0:
                critical_valid += 1
        except (TypeError, ValueError):
            continue
    critical_pct = round(critical_valid / max(1, len(unique)) * 100, 2)
    coverage_pct = (
        round(len(received_codes & reference_codes) / len(reference_codes) * 100, 2)
        if reference_codes else None
    )
    captured = None
    cutoff_start = None
    cutoff_end = None
    try:
        captured = datetime.fromisoformat(captured_at)
        cutoff_start = datetime.fromisoformat(f"{requested_trade_date}T{scan_time}:00")
        cutoff_end = datetime.fromisoformat(f"{requested_trade_date}T{scan_time}:59")
    except (TypeError, ValueError):
        pass
    freshness_seconds = (
        max(0, int((cutoff_start - captured).total_seconds()))
        if captured is not None and cutoff_start is not None else None
    )
    reasons: List[str] = []
    if str(source or "") not in {"sina", "tencent", "akshare", "eastmoney"}:
        reasons.append("行情源不是允许用于正式判断的真实行情源")
    if trade_date != requested_trade_date:
        reasons.append("快照交易日不是当前 Run 的推荐日")
    if captured is None or cutoff_end is None or captured > cutoff_end:
        reasons.append("快照时间晚于决策截止时间或时间格式无效")
    if freshness_seconds is None or freshness_seconds > FULL_MARKET_DECISION_MAX_AGE_SECONDS:
        reasons.append("快照距决策时点超过 30 分钟")
    if len(unique) < FULL_MARKET_DECISION_MIN_ROWS:
        reasons.append(f"有效股票数少于 {FULL_MARKET_DECISION_MIN_ROWS} 只")
    if not reference_codes:
        reasons.append("缺少带版本的本地参考股票全集")
    elif coverage_pct is None or coverage_pct < FULL_MARKET_DECISION_MIN_COVERAGE_PCT:
        reasons.append(f"参考股票覆盖率低于 {FULL_MARKET_DECISION_MIN_COVERAGE_PCT:g}%")
    if critical_pct < FULL_MARKET_DECISION_MIN_CRITICAL_PCT:
        reasons.append(f"关键行情字段有效率低于 {FULL_MARKET_DECISION_MIN_CRITICAL_PCT:g}%")
    return {
        "version": "v1_t1_snapshot_decision_quality",
        "snapshotDecisionUsable": not reasons,
        "storageStatusIndependent": True,
        "source": source,
        "tradeDate": trade_date,
        "capturedAt": captured_at,
        "scanTime": scan_time,
        "rowCount": len(unique),
        "minimumRowCount": FULL_MARKET_DECISION_MIN_ROWS,
        "referenceUniverseVersion": reference_version,
        "referenceUniverseCount": len(reference_codes),
        "referenceCoveragePct": coverage_pct,
        "minimumReferenceCoveragePct": FULL_MARKET_DECISION_MIN_COVERAGE_PCT,
        "criticalFieldValidPct": critical_pct,
        "minimumCriticalFieldValidPct": FULL_MARKET_DECISION_MIN_CRITICAL_PCT,
        "freshnessSecondsAtCutoff": freshness_seconds,
        "maximumFreshnessSeconds": FULL_MARKET_DECISION_MAX_AGE_SECONDS,
        "candidateCoveragePct": 100.0 if not reasons else None,
        "reasons": reasons,
    }


def _age_seconds(value: str) -> int:
    if not value:
        return -1
    try:
        generated = datetime.fromisoformat(value)
    except ValueError:
        return -1
    return max(0, int((datetime.now() - generated).total_seconds()))
