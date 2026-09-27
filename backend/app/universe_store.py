from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from backend.app.engines.filter_engine import in_confirmed_universe
from backend.app.providers.provider_common import DEFAULT_UNIVERSE, board_name
from backend.app.schema import RawStock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UNIVERSE_CACHE_PATH = PROJECT_ROOT / "data" / "default_universe.json"
DEFAULT_DYNAMIC_UNIVERSE_SIZE = int(os.getenv("DEFAULT_UNIVERSE_SIZE", "80"))
MIN_CACHED_UNIVERSE_SIZE = 10


def load_universe_items() -> List[Tuple[str, str]]:
    payload = load_cached_universe()
    items = _valid_items(payload.get("items", []) if payload else [])
    if len(items) >= MIN_CACHED_UNIVERSE_SIZE:
        return [(item["code"], item["name"]) for item in items]
    return list(DEFAULT_UNIVERSE)


def universe_response() -> Dict[str, Any]:
    payload = load_cached_universe()
    items = _valid_items(payload.get("items", []) if payload else [])
    if len(items) >= MIN_CACHED_UNIVERSE_SIZE:
        generated_at = str(payload.get("generatedAt", ""))
        generated_date = generated_at[:10] if generated_at else ""
        today = datetime.now().date().isoformat()
        return {
            "name": payload.get("name", "动态重点池"),
            "type": payload.get("type", "dynamic"),
            "source": payload.get("source", ""),
            "generatedAt": generated_at,
            "stale": generated_date != today,
            "cachePath": str(UNIVERSE_CACHE_PATH),
            "count": len(items),
            "description": payload.get(
                "description",
                "腾讯数据源默认拉取的动态重点池，由全市场快照每日更新，更新失败时继续使用上次结果。",
            ),
            "method": payload.get("method", _dynamic_method(payload)),
            "items": items,
        }
    return fallback_universe_response()


def fallback_universe_response() -> Dict[str, Any]:
    return {
        "name": "内置重点池",
        "type": "built_in",
        "source": "static",
        "generatedAt": "",
        "stale": True,
        "cachePath": str(UNIVERSE_CACHE_PATH),
        "count": len(DEFAULT_UNIVERSE),
        "description": "当前没有可用的动态重点池缓存，腾讯数据源会临时使用代码内置样本。",
        "method": [
            "从 backend/app/providers/provider_common.py 的 DEFAULT_UNIVERSE 读取。",
            "执行 scripts/update_default_universe.py 或点击页面重点池更新后，会写入 data/default_universe.json。",
            "腾讯数据源优先读取动态缓存；缓存不可用时才回退这份静态池。",
        ],
        "items": [
            {"code": code, "name": name, "board": board_name(code), "source": "built_in"}
            for code, name in DEFAULT_UNIVERSE
        ],
    }


def load_cached_universe() -> Dict[str, Any]:
    if not UNIVERSE_CACHE_PATH.exists():
        return {}
    try:
        with UNIVERSE_CACHE_PATH.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def is_universe_fresh() -> bool:
    payload = load_cached_universe()
    generated_at = str(payload.get("generatedAt", ""))
    return bool(generated_at and generated_at[:10] == datetime.now().date().isoformat())


def build_dynamic_universe(
    stocks: Iterable[RawStock],
    source: str,
    max_size: int = DEFAULT_DYNAMIC_UNIVERSE_SIZE,
    include_bse: bool = False,
    include_star_market: bool = False,
) -> List[Dict[str, Any]]:
    candidates = []
    seen = set()
    for stock in stocks:
        code = str(stock.code).strip().zfill(6)
        if code in seen:
            continue
        seen.add(code)
        if not _eligible_for_focus_pool(stock, include_bse=include_bse, include_star_market=include_star_market):
            continue
        score = _focus_score(stock)
        board = stock.sector or board_name(code)
        candidates.append(
            (
                score,
                {
                    "code": code,
                    "name": stock.name,
                    "board": board,
                    "source": source,
                    "score": round(score, 2),
                    "reason": _focus_reason(stock),
                    "change": round(stock.change, 2),
                    "turnover": round(stock.turnover, 2),
                    "volumeRatio": round(stock.volume_ratio, 2),
                    "amountFactor": round(stock.amount_factor, 2),
                    "relativeStrength": round(stock.rel_strength, 2),
                },
            )
        )

    ranked = sorted(candidates, key=lambda item: item[0], reverse=True)
    selected: List[Dict[str, Any]] = []
    board_counts: Dict[str, int] = {}
    max_per_board = max(12, max_size // 3)

    for _score, item in ranked:
        if len(selected) >= max_size:
            break
        board = item["board"]
        if board_counts.get(board, 0) >= max_per_board:
            continue
        selected.append(item)
        board_counts[board] = board_counts.get(board, 0) + 1

    if len(selected) < max_size:
        selected_codes = {item["code"] for item in selected}
        for _score, item in ranked:
            if len(selected) >= max_size:
                break
            if item["code"] in selected_codes:
                continue
            selected.append(item)
            selected_codes.add(item["code"])

    return selected


def save_dynamic_universe(items: List[Dict[str, Any]], source: str, snapshot_rows: int) -> Dict[str, Any]:
    if len(items) < MIN_CACHED_UNIVERSE_SIZE:
        raise ValueError(f"动态重点池样本过少：{len(items)}")
    now = datetime.now().isoformat(timespec="seconds")
    payload = {
        "name": "动态重点池",
        "type": "dynamic",
        "source": source,
        "generatedAt": now,
        "snapshotRows": snapshot_rows,
        "count": len(items),
        "description": "腾讯数据源默认拉取的动态重点池，由全市场快照每日更新，更新失败时继续使用上次结果。",
        "method": [
            f"基于 {source} 本轮全市场/样本快照生成，原始快照 {snapshot_rows} 只。",
            "过滤 ST、停牌、非当前市场范围和明显低流动性股票。",
            "按成交额强度、相对强度、量能、换手、板块热度和极端涨跌惩罚综合排序。",
            "设置板块数量上限，避免重点池被单一市场方向完全占满。",
        ],
        "items": items,
    }
    UNIVERSE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = UNIVERSE_CACHE_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
    tmp_path.replace(UNIVERSE_CACHE_PATH)
    return universe_response()


def _valid_items(items: Any) -> List[Dict[str, Any]]:
    valid = []
    if not isinstance(items, list):
        return valid
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip().zfill(6)
        name = str(item.get("name", "")).strip()
        if not code or not name:
            continue
        valid.append({**item, "code": code, "name": name, "board": item.get("board") or board_name(code)})
    return valid


def _dynamic_method(payload: Dict[str, Any]) -> List[str]:
    source = payload.get("source", "全市场快照")
    snapshot_rows = payload.get("snapshotRows", 0)
    return [
        f"基于 {source} 快照生成，原始快照 {snapshot_rows} 只。",
        "腾讯数据源优先读取 data/default_universe.json。",
        "缓存过期不会自动失效；更新失败时继续使用上次重点池。",
    ]


def _eligible_for_focus_pool(stock: RawStock, include_bse: bool, include_star_market: bool) -> bool:
    if not in_confirmed_universe(stock.code, include_bse=include_bse, include_star_market=include_star_market):
        return False
    if stock.suspended or stock.st or "ST" in stock.name.upper():
        return False
    if stock.price <= 0:
        return False
    if stock.amount_factor < 0.75 and stock.volume_ratio < 1.0:
        return False
    if stock.turnover <= 0 and stock.amount_factor < 1.0:
        return False
    return True


def _focus_score(stock: RawStock) -> float:
    amount_score = _clamp((stock.amount_factor - 0.5) / 2.7) * 30
    strength_score = _clamp(stock.rel_strength / 100) * 25
    volume_score = _clamp(stock.volume_ratio / 3.5) * 15
    turnover_score = _clamp(stock.turnover / 10) * 10
    board_score = _clamp((7 - stock.sector_rank) / 6) * 10
    board_limit_score = min(max(stock.sector_limit_ups, 0), 5) * 2
    score = amount_score + strength_score + volume_score + turnover_score + board_score + board_limit_score

    if stock.change <= -6:
        score -= 15
    elif stock.change <= -2:
        score -= 5
    elif 0 < stock.change <= 6:
        score += 5
    elif stock.change >= 9.5:
        score -= 18
    elif stock.change >= 8:
        score -= 8

    if stock.limit_status in {"limit_down", "one_word_limit_up"}:
        score -= 20
    if stock.risk_flags:
        score -= min(12, len(stock.risk_flags) * 3)
    return score


def _focus_reason(stock: RawStock) -> str:
    parts = [
        f"成交额强度 {stock.amount_factor:.2f}x",
        f"相对强度 {stock.rel_strength:.0f}",
        f"量能 {stock.volume_ratio:.2f}",
        f"换手 {stock.turnover:.2f}%",
        f"涨跌幅 {stock.change:.2f}%",
    ]
    if stock.sector_rank:
        parts.append(f"样本板块第 {stock.sector_rank}")
    return "，".join(parts)


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    if math.isnan(value) or math.isinf(value):
        return lower
    return max(lower, min(upper, value))
