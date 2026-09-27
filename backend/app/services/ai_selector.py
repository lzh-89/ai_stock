from __future__ import annotations

import json
import re
from typing import Any, Dict, List


def _extract_picks(content: str) -> List[Dict[str, Any]]:
    if not content:
        return []
    text = content.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return _extract_pick_fragments(text)
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return _extract_pick_fragments(text)
    if isinstance(data, list):
        picks = data
    elif isinstance(data, dict):
        picks = _find_pick_list(data)
    else:
        return []
    valid = [item for item in picks if isinstance(item, dict)]
    return valid or _extract_pick_fragments(text)


def _find_pick_list(data: Dict[str, Any]) -> List[Any]:
    for key in (
        "picks",
        "top20",
        "top_20",
        "top10",
        "top_10",
        "stocks",
        "selected",
        "selectedStocks",
        "selection",
        "recommendations",
        "candidates",
        "候选",
        "结果",
        "推荐",
        "选股结果",
    ):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            dict_values = list(value.values())
            if dict_values and all(isinstance(item, dict) for item in dict_values):
                return dict_values
            nested = _find_pick_list(value)
            if nested:
                return nested
    dict_values = list(data.values())
    if dict_values and all(isinstance(item, dict) for item in dict_values):
        return dict_values
    return []


def _extract_pick_fragments(text: str) -> List[Dict[str, Any]]:
    picks: List[Dict[str, Any]] = []
    for match in re.finditer(r"\{[^{}]*\"code\"\s*:\s*\"?\d{6}\"?[^{}]*\}", text, flags=re.S):
        fragment = match.group(0)
        code = _extract_jsonish_field(fragment, "code")
        if not code:
            continue
        picks.append(
            {
                "code": code,
                "rank": _extract_jsonish_field(fragment, "rank") or len(picks) + 1,
                "reason": _extract_jsonish_field(fragment, "reason"),
                "risk": _extract_jsonish_field(fragment, "risk"),
                "focus": _extract_jsonish_field(fragment, "focus"),
            }
        )
    return picks


def _extract_jsonish_field(fragment: str, field: str) -> str:
    pattern = rf'"{re.escape(field)}"\s*:\s*(?:"([^"]*)"|([0-9.]+))'
    match = re.search(pattern, fragment)
    if not match:
        return ""
    return (match.group(1) or match.group(2) or "").strip()
