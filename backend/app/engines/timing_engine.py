from __future__ import annotations

from typing import Any, Dict


WINDOWS = {
    "09:50": {
        "label": "09:45-10:15",
        "stage": "第一确认",
        "buy": "强势突破早盘高点并站稳 VWAP，且 15 分钟 MACD 继续走强",
        "sell": "10:00-11:00",
        "sellTrigger": "买入后 15-30 分钟跌回 VWAP 或早盘低点",
    },
    "10:42": {
        "label": "10:30-11:00",
        "stage": "回踩确认",
        "buy": "回踩 15 分钟均线/VWAP 不破且缩量，板块仍在前五",
        "sell": "13:30-14:20",
        "sellTrigger": "午后冲高但 MACD 红柱收缩时分批止盈",
    },
    "13:18": {
        "label": "13:00-13:30",
        "stage": "午后回流",
        "buy": "板块热度回升，股价重新站上 VWAP，量能重新放大",
        "sell": "13:30-14:20",
        "sellTrigger": "第二波拉升量能不足或跌破午后均价",
    },
    "14:36": {
        "label": "14:30-14:50",
        "stage": "尾盘决策",
        "buy": "仅允许强趋势股小仓位确认，不追高",
        "sell": "14:30-14:50",
        "sellTrigger": "未能维持日内强度或板块跌出前五则降低隔夜仓位",
    },
}


def attach_plan(candidate: Dict[str, Any], scan_time: str = "10:42") -> Dict[str, Any]:
    window = WINDOWS.get(scan_time, WINDOWS["10:42"])
    tradeability = candidate.get("tradeability", {})
    candidate["buyWindow"] = window["label"]
    candidate["buyTrigger"] = f"{window['buy']}；{tradeability.get('reason', '按规则继续观察')}"
    candidate["sellWindow"] = window["sell"]
    candidate["sellTrigger"] = window["sellTrigger"]
    candidate["invalidCondition"] = f"跌破 VWAP {candidate['lastVwap']:.2f} 或早盘低点 {candidate['morningLow']:.2f}"
    candidate["overnight"] = "conditional" if candidate["score"] >= 84 and candidate["sectorRank"] <= 3 and candidate["dimensions"]["riskPenalty"] <= 8 else "review-at-close"
    return candidate

