from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from backend.app.settings import PROJECT_ROOT


RETAIL_HOLDER_CACHE_PATH = PROJECT_ROOT / "data" / "retail_holder_cache.json"
RETAIL_HOLDER_TOP_JSON_PATH = PROJECT_ROOT / "data" / "retail_holder_top100.json"
RETAIL_HOLDER_TOP_CSV_PATH = PROJECT_ROOT / "data" / "retail_holder_top100.csv"
RETAIL_HOLDER_TTL = timedelta(hours=24)


def latest_retail_holders(limit: int = 100, query: str = "", force: bool = False) -> Dict[str, Any]:
    cached = _read_cache()
    warning = ""
    if force or not cached.get("items"):
        try:
            cached = refresh_retail_holder_cache()
        except Exception as error:  # noqa: BLE001 - stale cache is better than no page data.
            if not cached.get("items"):
                raise
            warning = f"股东户数刷新失败，已使用缓存：{error}"

    stale = _is_stale(cached)
    if stale and not warning:
        warning = "股东户数缓存已超过 24 小时，可点击更新获取最新披露数据。"
    return _response_from_cache(cached, limit=limit, query=query, stale=stale, warning=warning)


def refresh_retail_holder_cache(symbol: str = "最新") -> Dict[str, Any]:
    rows = _fetch_eastmoney_holder_rows(symbol=symbol)
    items = sorted(_records(rows), key=lambda item: item.get("holderCount", 0), reverse=True)
    if not items:
        raise RuntimeError("股东户数接口未返回有效数据")
    for index, item in enumerate(items, start=1):
        item["rank"] = index

    payload = {
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "source": "eastmoney.datacenter.holdernum",
        "upstream": "东方财富-数据中心-特色数据-股东户数",
        "metric": "股东户数-本次",
        "businessMeaning": "用股东户数作为散户数量的近似代理；该数据来自上市公司披露，不是实时散户人数。",
        "symbol": symbol,
        "totalRows": len(items),
        "items": items,
    }
    _write_json(RETAIL_HOLDER_CACHE_PATH, payload)
    export_retail_holder_top(payload, limit=100)
    return payload


def export_retail_holder_top(payload: Dict[str, Any], limit: int = 100) -> None:
    items = payload.get("items", [])[:limit]
    top_payload = {
        **{key: value for key, value in payload.items() if key != "items"},
        "count": len(items),
        "items": items,
    }
    _write_json(RETAIL_HOLDER_TOP_JSON_PATH, top_payload)
    _write_csv(RETAIL_HOLDER_TOP_CSV_PATH, items)


def _response_from_cache(payload: Dict[str, Any], limit: int, query: str, stale: bool, warning: str) -> Dict[str, Any]:
    items = payload.get("items", [])
    if not isinstance(items, list):
        items = []
    filtered = _filter_items(items, query)
    limited = filtered[: max(1, min(limit, 500))]
    return {
        **{key: value for key, value in payload.items() if key != "items"},
        "cacheHit": True,
        "stale": stale,
        "warning": warning,
        "query": query,
        "limit": limit,
        "matchedRows": len(filtered),
        "count": len(limited),
        "items": limited,
    }


def _filter_items(items: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    keyword = str(query or "").strip().lower()
    if not keyword:
        return items
    return [
        item
        for item in items
        if keyword in str(item.get("code", "")).lower() or keyword in str(item.get("name", "")).lower()
    ]


def _read_cache() -> Dict[str, Any]:
    if not RETAIL_HOLDER_CACHE_PATH.exists():
        return {}
    try:
        with RETAIL_HOLDER_CACHE_PATH.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_stale(payload: Dict[str, Any]) -> bool:
    updated_at = _parse_datetime(payload.get("updatedAt"))
    if not updated_at:
        return True
    return datetime.now() - updated_at > RETAIL_HOLDER_TTL


def _records(dataframe: Any) -> List[Dict[str, Any]]:
    rows = dataframe.to_dict("records") if hasattr(dataframe, "to_dict") else dataframe
    if not isinstance(rows, list):
        return []
    result: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(_pick(row, "代码", "SECURITY_CODE")).strip().zfill(6)
        name = str(_pick(row, "名称", "SECURITY_NAME_ABBR")).strip()
        holder_count = _num(_pick(row, "股东户数-本次", "HOLDER_NUM"))
        if not code or not name or holder_count <= 0:
            continue
        previous_holder_count = _num(_pick(row, "股东户数-上次", "PRE_HOLDER_NUM"))
        result.append(
            {
                "code": code,
                "name": name,
                "holderCount": int(holder_count),
                "previousHolderCount": int(previous_holder_count) if previous_holder_count else 0,
                "holderChange": int(_num(_pick(row, "股东户数-增减", "HOLDER_NUM_CHANGE"))),
                "holderChangeRatio": round(_num(_pick(row, "股东户数-增减比例", "HOLDER_NUM_RATIO")), 4),
                "statDate": _date_text(_pick(row, "股东户数统计截止日-本次", "END_DATE")),
                "previousStatDate": _date_text(_pick(row, "股东户数统计截止日-上次", "PRE_END_DATE")),
                "noticeDate": _date_text(_pick(row, "公告日期", "HOLD_NOTICE_DATE")),
                "latestPrice": round(_num(_pick(row, "最新价", "f2")), 3),
                "changePct": round(_num(_pick(row, "涨跌幅", "f3")), 3),
                "intervalChangePct": round(_num(_pick(row, "区间涨跌幅", "INTERVAL_CHRATE")), 3),
                "avgHoldingMarketValue": round(_num(_pick(row, "户均持股市值", "AVG_MARKET_CAP")), 2),
                "avgHoldingShares": round(_num(_pick(row, "户均持股数量", "AVG_HOLD_NUM")), 2),
                "totalMarketCap": round(_num(_pick(row, "总市值", "TOTAL_MARKET_CAP")), 2),
                "totalShares": round(_num(_pick(row, "总股本", "TOTAL_A_SHARES")), 2),
            }
        )
    return result


def _fetch_eastmoney_holder_rows(symbol: str = "最新") -> List[Dict[str, Any]]:
    try:
        import requests  # type: ignore
    except ModuleNotFoundError as error:
        raise RuntimeError("requests 未安装，无法获取股东户数") from error

    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    page_size = 1000
    params = {
        "sortColumns": "HOLD_NOTICE_DATE,SECURITY_CODE",
        "sortTypes": "-1,-1",
        "pageSize": str(page_size),
        "pageNumber": "1",
        "reportName": "RPT_HOLDERNUMLATEST" if symbol == "最新" else "RPT_HOLDERNUM_DET",
        "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,END_DATE,INTERVAL_CHRATE,AVG_MARKET_CAP,"
        "AVG_HOLD_NUM,TOTAL_MARKET_CAP,TOTAL_A_SHARES,HOLD_NOTICE_DATE,HOLDER_NUM,"
        "PRE_HOLDER_NUM,HOLDER_NUM_CHANGE,HOLDER_NUM_RATIO,END_DATE,PRE_END_DATE",
        "quoteColumns": "f2,f3",
        "source": "WEB",
        "client": "WEB",
    }
    if symbol != "最新":
        params["filter"] = f"(END_DATE='{symbol[:4]}-{symbol[4:6]}-{symbol[6:]}')"

    first = _request_eastmoney_page(requests, url, params, 1)
    result = first.get("result") or {}
    pages = int(result.get("pages") or 1)
    rows = list(result.get("data") or [])
    for page in range(2, pages + 1):
        data_json = _request_eastmoney_page(requests, url, params, page)
        rows.extend((data_json.get("result") or {}).get("data") or [])
    return rows


def _request_eastmoney_page(requests: Any, url: str, params: Dict[str, str], page: int) -> Dict[str, Any]:
    request_params = {**params, "pageNumber": str(page)}
    last_error: Exception | None = None
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://data.eastmoney.com/gdhs/",
    }
    for attempt in range(1, 4):
        try:
            response = requests.get(url, params=request_params, headers=headers, timeout=15)
            response.raise_for_status()
            data_json = response.json()
            if not data_json.get("result"):
                raise RuntimeError(f"东方财富股东户数第 {page} 页无 result")
            return data_json
        except Exception as error:  # noqa: BLE001 - provider requests are retried.
            last_error = error
            if attempt < 3:
                time.sleep(0.8 * attempt)
    raise RuntimeError(f"东方财富股东户数第 {page} 页请求失败：{last_error}")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temp_path.replace(path)


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "rank",
        "code",
        "name",
        "holderCount",
        "previousHolderCount",
        "holderChange",
        "holderChangeRatio",
        "statDate",
        "previousStatDate",
        "noticeDate",
        "latestPrice",
        "changePct",
        "intervalChangePct",
        "avgHoldingMarketValue",
        "avgHoldingShares",
        "totalMarketCap",
        "totalShares",
    ]
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow({"rank": index, **row})
    temp_path.replace(path)


def _num(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _date_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)[:10]


def _pick(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return ""


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
