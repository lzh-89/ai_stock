from __future__ import annotations

from datetime import date
from typing import Any, Dict, List


def build_daily_review(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    top = candidates[:3]
    risk_names = [item["name"] for item in candidates if item.get("dimensions", {}).get("riskPenalty", 0) >= 12]
    triggered = [item["name"] for item in candidates if item.get("status") == "buy-triggered"]
    pullbacks = [item["name"] for item in candidates if item.get("status") == "pullback"]
    return {
        "date": date.today().isoformat(),
        "summary": f"今日候选池共 {len(candidates)} 只，强势突破 {len(triggered)} 只，回踩低吸 {len(pullbacks)} 只。",
        "topCandidates": [{"code": item["code"], "name": item["name"], "score": item["score"], "style": item["style"]} for item in top],
        "riskFocus": risk_names,
        "lessons": [
            "优先复盘是否真正满足 15 分钟 VWAP 和 MACD 条件。",
            "涨幅超过 8% 的候选只做降权观察，不用追高逻辑替代规则。",
            "接近涨停或跌停的股票重点记录可买卖性，而不是只看分数。",
        ],
    }

