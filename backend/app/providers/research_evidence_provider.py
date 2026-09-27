from __future__ import annotations

import json
import logging
import html as html_lib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.app.settings import PROJECT_ROOT


LOGGER = logging.getLogger("ai_stock.provider.research_evidence")
RESEARCH_EVIDENCE_CACHE_PATH = PROJECT_ROOT / "data" / "research_evidence_cache.json"
RESEARCH_EVIDENCE_CACHE_VERSION = "v2-news-announcements"
RESEARCH_EVIDENCE_TTL_SECONDS = 60 * 60 * 24
MAX_WORKERS = 5


class ResearchEvidenceProvider:
    source = "eastmoney_research_evidence"

    def __init__(self, ttl_seconds: int = RESEARCH_EVIDENCE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self.last_warning = ""
        self.last_stats: Dict[str, Any] = {}
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._disk_loaded = False

    def fetch_many(self, codes: List[str], force: bool = False) -> Dict[str, Dict[str, Any]]:
        self.last_warning = ""
        normalized = _unique_codes(codes)
        fresh_cache = {} if force else self._read_cache(normalized, fresh_only=True)
        stale_cache = {} if force else self._read_cache(normalized, fresh_only=False)
        missing = [code for code in normalized if code not in fresh_cache]
        fetched: Dict[str, Dict[str, Any]] = {}
        failures: List[str] = []

        if missing:
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(missing))) as executor:
                futures = {executor.submit(self.fetch_one, code): code for code in missing}
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        evidence = future.result()
                    except Exception as error:  # noqa: BLE001 - evidence is best-effort.
                        fallback = stale_cache.get(code)
                        if fallback:
                            fallback = dict(fallback)
                            warnings = list(fallback.get("warnings") or [])
                            warnings.append(f"证据刷新失败，使用过期缓存：{error}")
                            fallback["warnings"] = warnings
                            fallback["stale"] = True
                            fetched[code] = fallback
                        else:
                            failures.append(f"{code}: {error}")
                        LOGGER.warning("research evidence fetch failed code=%s error=%s", code, error)
                    else:
                        fetched[code] = evidence

        fresh_fetched = {code: item for code, item in fetched.items() if not item.get("stale")}
        if fresh_fetched:
            self._write_disk_cache(fresh_fetched)

        result = {**fresh_cache, **fetched}
        if failures:
            self.last_warning = "；".join(failures[:5])
        self.last_stats = {
            "requested": len(normalized),
            "cacheHit": len(fresh_cache),
            "fetched": len(fresh_fetched),
            "stale": len([item for item in fetched.values() if item.get("stale")]),
            "failed": len(failures),
        }
        return result

    def fetch_one(self, code: str) -> Dict[str, Any]:
        code = str(code).strip().zfill(6)
        warnings: List[str] = []
        fundamentals: Dict[str, Any] = {}
        valuation: Dict[str, Any] = {}
        money_flow: Dict[str, Any] = {}
        news: List[Dict[str, str]] = []
        announcements: List[Dict[str, str]] = []

        try:
            quote = _fetch_quote(code)
            fundamentals = _quote_fundamentals(quote)
            valuation = _quote_valuation(quote)
        except Exception as error:  # noqa: BLE001 - keep other evidence categories.
            warnings.append(f"基本面/估值获取失败：{error}")

        try:
            fundamentals = {**_fetch_f10_fundamentals(code), **fundamentals}
        except Exception as error:  # noqa: BLE001 - keep quote fundamentals when possible.
            warnings.append(f"F10 基本面获取失败：{error}")

        if not valuation:
            try:
                valuation = _fetch_baidu_valuation(code)
            except Exception as error:  # noqa: BLE001 - keep other evidence categories.
                warnings.append(f"百度估值获取失败：{error}")

        try:
            money_flow = _fetch_money_flow(code)
        except Exception as error:  # noqa: BLE001 - keep other evidence categories.
            warnings.append(f"资金流获取失败：{error}")

        try:
            news = _fetch_news(code, _news_keywords(code, fundamentals))
        except Exception as error:  # noqa: BLE001 - keep announcement evidence.
            warnings.append(f"新闻获取失败：{error}")

        try:
            announcements = _fetch_announcements(code)
        except Exception as error:  # noqa: BLE001 - keep news evidence.
            warnings.append(f"公告获取失败：{error}")

        catalysts = []
        catalysts.extend(
            {"type": "新闻", "time": item.get("time", ""), "title": item.get("title", ""), "url": item.get("url", "")}
            for item in news[:3]
        )
        catalysts.extend(
            {"type": "公告", "time": item.get("time", ""), "title": item.get("title", ""), "url": item.get("url", "")}
            for item in announcements[:3]
        )
        evidence_status, missing_data = _evidence_status(fundamentals, valuation, money_flow, news, announcements)
        return {
            "code": code,
            "source": self.source,
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
            "fundamentals": fundamentals,
            "valuation": valuation,
            "moneyFlow": money_flow,
            "news": news,
            "announcements": announcements,
            "catalysts": [item for item in catalysts if item.get("title")],
            "evidenceStatus": evidence_status,
            "missingData": missing_data,
            "warnings": warnings,
        }

    def _read_cache(self, codes: List[str], fresh_only: bool) -> Dict[str, Dict[str, Any]]:
        self._load_disk_cache()
        now = time.time()
        result: Dict[str, Dict[str, Any]] = {}
        for code in codes:
            item = self._cache.get(code)
            if not item:
                continue
            if item.get("version") != RESEARCH_EVIDENCE_CACHE_VERSION:
                continue
            updated_at = float(item.get("updatedAtEpoch", 0))
            if fresh_only and now - updated_at > self.ttl_seconds:
                continue
            result[code] = dict(item.get("evidence") or {})
        return result

    def _load_disk_cache(self) -> None:
        if self._disk_loaded:
            return
        self._disk_loaded = True
        if not RESEARCH_EVIDENCE_CACHE_PATH.exists():
            return
        try:
            with RESEARCH_EVIDENCE_CACHE_PATH.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            LOGGER.warning("research evidence cache ignored path=%s error=%s", RESEARCH_EVIDENCE_CACHE_PATH, error)
            self.last_warning = f"研究证据缓存损坏，已跳过本地缓存：{error}"
            return
        items = payload.get("items", {}) if isinstance(payload, dict) else {}
        if not isinstance(items, dict):
            return
        for code, item in items.items():
            if isinstance(item, dict):
                self._cache[str(code).zfill(6)] = item

    def _write_disk_cache(self, fetched: Dict[str, Dict[str, Any]]) -> None:
        payload = _read_disk_payload()
        items = payload.setdefault("items", {})
        now = time.time()
        for code, evidence in fetched.items():
            items[code] = {
                "version": RESEARCH_EVIDENCE_CACHE_VERSION,
                "updatedAt": datetime.now().isoformat(timespec="seconds"),
                "updatedAtEpoch": now,
                "source": evidence.get("source", self.source),
                "evidence": evidence,
            }
            self._cache[code] = items[code]
        payload["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        RESEARCH_EVIDENCE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = RESEARCH_EVIDENCE_CACHE_PATH.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
        temp_path.replace(RESEARCH_EVIDENCE_CACHE_PATH)


def _fetch_quote(code: str) -> Dict[str, Any]:
    requests = _requests()
    fields = ",".join(
        [
            "f43",
            "f57",
            "f58",
            "f84",
            "f85",
            "f116",
            "f117",
            "f127",
            "f162",
            "f164",
            "f167",
            "f173",
            "f189",
        ]
    )
    params = {
        "fltt": "2",
        "invt": "2",
        "fields": fields,
        "secid": _secid(code),
    }
    response = requests.get(
        "https://push2.eastmoney.com/api/qt/stock/get",
        params=params,
        headers=_headers("https://quote.eastmoney.com/"),
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    if not isinstance(data, dict) or not data:
        raise RuntimeError("东方财富行情详情为空")
    return data


def _quote_fundamentals(quote: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        "industry": _clean_text(quote.get("f127")),
        "listedDate": _format_yyyymmdd(quote.get("f189")),
        "totalShares": _as_float(quote.get("f84")),
        "floatShares": _as_float(quote.get("f85")),
        "totalMarketCap": _as_float(quote.get("f116")),
        "floatMarketCap": _as_float(quote.get("f117")),
        "source": "东方财富行情详情",
    }
    return {key: value for key, value in result.items() if value not in {"", None}}


def _quote_valuation(quote: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        "peDynamic": _as_float(quote.get("f162")),
        "peTtm": _as_float(quote.get("f164")),
        "pb": _as_float(quote.get("f167")),
        "ps": _as_float(quote.get("f173")),
        "source": "东方财富行情详情",
    }
    return {key: value for key, value in result.items() if value not in {"", None}}


def _fetch_money_flow(code: str) -> Dict[str, Any]:
    try:
        return _fetch_eastmoney_money_flow(code)
    except Exception as eastmoney_error:  # noqa: BLE001 - Sina is a practical fallback for资金流.
        try:
            result = _fetch_sina_money_flow(code)
            warnings = list(result.get("warnings") or [])
            warnings.append(f"东方财富资金流失败，已回退新浪：{eastmoney_error}")
            result["warnings"] = warnings
            return result
        except Exception as sina_error:  # noqa: BLE001
            raise RuntimeError(f"东方财富资金流失败：{eastmoney_error}；新浪资金流失败：{sina_error}") from sina_error


def _fetch_eastmoney_money_flow(code: str) -> Dict[str, Any]:
    requests = _requests()
    params = {
        "lmt": "0",
        "klt": "101",
        "secid": _secid(code),
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "_": str(int(time.time() * 1000)),
    }
    response = requests.get(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        params=params,
        headers=_headers("https://data.eastmoney.com/"),
        timeout=12,
    )
    response.raise_for_status()
    payload = response.json()
    klines = ((payload.get("data") or {}).get("klines") or []) if isinstance(payload, dict) else []
    rows = [_parse_money_flow_row(item) for item in klines if isinstance(item, str)]
    rows = [item for item in rows if item]
    if not rows:
        raise RuntimeError("东方财富资金流为空")
    latest = rows[-1]
    recent = rows[-5:]
    latest["fiveDayMainNetInflow"] = round(sum(item.get("mainNetInflow", 0) for item in recent), 2)
    latest["fiveDaySmallNetInflow"] = round(sum(item.get("smallNetInflow", 0) for item in recent), 2)
    latest["source"] = "东方财富资金流"
    return latest


def _fetch_sina_money_flow(code: str) -> Dict[str, Any]:
    requests = _requests()
    symbol = ("sh" if code.startswith(("5", "6", "9")) else "sz") + code
    response = requests.get(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs",
        params={"daima": symbol},
        headers=_headers("https://finance.sina.com.cn"),
        timeout=12,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("新浪资金流为空")
    parsed = [_parse_sina_money_flow_row(row) for row in rows[:5] if isinstance(row, dict)]
    parsed = [item for item in parsed if item]
    if not parsed:
        raise RuntimeError("新浪资金流格式异常")
    latest = parsed[0]
    latest["fiveDayMainNetInflow"] = round(sum(item.get("mainNetInflow", 0) for item in parsed), 2)
    latest["source"] = "新浪资金流"
    return latest


def _parse_sina_money_flow_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "date": str(row.get("opendate", "")),
        "close": _as_float(row.get("trade")),
        "change": _ratio_to_percent(row.get("changeratio")),
        "netInflow": _as_float(row.get("netamount")) or 0,
        "netInflowRatio": _ratio_to_percent(row.get("ratioamount")),
        "mainNetInflow": _as_float(row.get("r0_net")) or 0,
        "mainNetInflowRatio": _ratio_to_percent(row.get("r0_ratio")),
        "source": "新浪资金流",
    }


def _fetch_f10_fundamentals(code: str) -> Dict[str, Any]:
    requests = _requests()
    sec_code = _f10_code(code)
    headers = _headers("https://emweb.securities.eastmoney.com/")
    survey_response = requests.get(
        "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax",
        params={"code": sec_code},
        headers=headers,
        timeout=12,
    )
    survey_response.raise_for_status()
    survey_payload = survey_response.json()
    survey_rows = survey_payload.get("jbzl") or []
    survey = survey_rows[0] if survey_rows else {}

    finance_response = requests.get(
        "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew",
        params={"type": "0", "code": sec_code},
        headers=headers,
        timeout=12,
    )
    finance_response.raise_for_status()
    finance_payload = finance_response.json()
    finance_rows = finance_payload.get("data") or []
    finance = finance_rows[0] if finance_rows else {}
    if not survey and not finance:
        raise RuntimeError("东方财富 F10 为空")

    industry = _clean_text(survey.get("INDUSTRYCSRC1")) or _clean_text(survey.get("EM2016"))
    result = {
        "shortName": _clean_text(survey.get("SECURITY_NAME_ABBR")),
        "companyName": _clean_text(survey.get("ORG_NAME")),
        "industry": industry,
        "securityType": _clean_text(survey.get("SECURITY_TYPE")),
        "tradeMarket": _clean_text(survey.get("TRADE_MARKET")),
        "chairman": _clean_text(survey.get("CHAIRMAN")),
        "president": _clean_text(survey.get("PRESIDENT")),
        "reportDate": _clean_text(finance.get("REPORT_DATE_NAME")),
        "revenue": _as_float(finance.get("TOTALOPERATEREVE")),
        "revenueYoY": _as_float(finance.get("TOTALOPERATEREVETZ")),
        "netProfit": _as_float(finance.get("PARENTNETPROFIT")),
        "netProfitYoY": _as_float(finance.get("PARENTNETPROFITTZ")),
        "roe": _as_float(finance.get("ROEJQ")),
        "grossMargin": _as_float(finance.get("XSMLL")),
        "debtRatio": _as_float(finance.get("ZCFZL")),
        "operatingCashFlowPerShare": _as_float(finance.get("MGJYXJJE")),
        "source": "东方财富 F10",
    }
    return {key: value for key, value in result.items() if value not in {"", None}}


def _fetch_baidu_valuation(code: str) -> Dict[str, Any]:
    pe = _fetch_baidu_valuation_item(code, "市盈率(TTM)")
    pb = _fetch_baidu_valuation_item(code, "市净率")
    market_cap = _fetch_baidu_valuation_item(code, "总市值")
    result = {
        "peTtm": pe.get("value"),
        "pb": pb.get("value"),
        "marketCapYi": market_cap.get("value"),
        "valuationDate": pe.get("date") or pb.get("date") or market_cap.get("date"),
        "source": "百度股市通",
    }
    return {key: value for key, value in result.items() if value not in {"", None}}


def _fetch_baidu_valuation_item(code: str, indicator: str) -> Dict[str, Any]:
    requests = _requests()
    params = {
        "openapi": "1",
        "dspName": "iphone",
        "tn": "tangram",
        "client": "app",
        "query": indicator,
        "code": code,
        "word": "",
        "resource_id": "51171",
        "market": "ab",
        "tag": indicator,
        "chart_select": "近一年",
        "industry_select": "",
        "skip_industry": "1",
        "finClientType": "pc",
    }
    response = requests.get("https://gushitong.baidu.com/opendata", params=params, headers=_headers("https://gushitong.baidu.com/"), timeout=12)
    response.raise_for_status()
    payload = response.json()
    rows = (
        payload.get("Result", [{}])[0]
        .get("DisplayData", {})
        .get("resultData", {})
        .get("tplData", {})
        .get("result", {})
        .get("chartInfo", [{}])[0]
        .get("body", [])
    )
    if not rows:
        return {}
    last = rows[-1]
    if isinstance(last, dict):
        return {"date": str(last.get("date", "")), "value": _as_float(last.get("value"))}
    if isinstance(last, list) and len(last) >= 2:
        return {"date": str(last[0]), "value": _as_float(last[1])}
    return {}


def _parse_money_flow_row(item: str) -> Dict[str, Any]:
    parts = item.split(",")
    if len(parts) < 13:
        return {}
    return {
        "date": parts[0],
        "close": _as_float(parts[11]),
        "change": _as_float(parts[12]),
        "mainNetInflow": _as_float(parts[1]) or 0,
        "mainNetInflowRatio": _as_float(parts[6]),
        "superBigNetInflow": _as_float(parts[5]) or 0,
        "superBigNetInflowRatio": _as_float(parts[10]),
        "bigNetInflow": _as_float(parts[4]) or 0,
        "bigNetInflowRatio": _as_float(parts[9]),
        "midNetInflow": _as_float(parts[3]) or 0,
        "midNetInflowRatio": _as_float(parts[8]),
        "smallNetInflow": _as_float(parts[2]) or 0,
        "smallNetInflowRatio": _as_float(parts[7]),
    }


def _fetch_news(code: str, keywords: Optional[List[str]] = None) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    for keyword in keywords or [code]:
        result = _merge_news(result, _fetch_eastmoney_news(keyword))
        if len(result) >= 5:
            return result[:5]
    result = _merge_news(result, _fetch_sina_stock_news(code))
    return result[:5]


def _fetch_eastmoney_news(keyword: str) -> List[Dict[str, str]]:
    requests = _requests()
    callback = f"jQuery{int(time.time() * 1000)}"
    inner_param = {
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": 5,
                "preTag": "<em>",
                "postTag": "</em>",
            }
        },
    }
    response = requests.get(
        "https://search-api-web.eastmoney.com/search/jsonp",
        params={"cb": callback, "param": json.dumps(inner_param, ensure_ascii=False), "_": str(int(time.time() * 1000))},
        headers=_headers("https://so.eastmoney.com/news/s"),
        timeout=12,
    )
    response.raise_for_status()
    payload = _parse_jsonp(response.text)
    rows = ((payload.get("result") or {}).get("cmsArticleWebOld") or []) if isinstance(payload, dict) else []
    news = []
    for row in rows[:5]:
        title = _strip_tags(row.get("title", ""))
        article_code = str(row.get("code", "")).strip()
        url = row.get("url") or (f"http://finance.eastmoney.com/a/{article_code}.html" if article_code else "")
        if title:
            news.append(
                {
                    "title": title,
                    "time": str(row.get("date", "")),
                    "source": _clean_text(row.get("mediaName")) or "东方财富新闻",
                    "url": str(url),
                }
            )
    return news


def _fetch_sina_stock_news(code: str) -> List[Dict[str, str]]:
    requests = _requests()
    symbol = ("sh" if code.startswith(("5", "6", "9")) else "sz") + code
    response = requests.get(
        f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{symbol}.phtml",
        headers=_headers("https://finance.sina.com.cn/"),
        timeout=12,
    )
    response.raise_for_status()
    text = response.text
    start = text.find('class="datelist"')
    if start == -1:
        return []
    block = text[start : start + 12000]
    pattern = re.compile(
        r"(?P<time>20\d{2}-\d{2}-\d{2}&nbsp;\d{2}:\d{2}).{0,80}?"
        r"<a[^>]+href=['\"](?P<url>[^'\"]+)['\"][^>]*>(?P<title>.*?)</a>",
        re.S,
    )
    news = []
    for match in pattern.finditer(block):
        title = _strip_tags(html_lib.unescape(match.group("title")))
        url = html_lib.unescape(match.group("url"))
        time_text = html_lib.unescape(match.group("time")).replace("\xa0", " ").strip()
        if title and url:
            news.append({"title": title, "time": time_text, "source": "新浪财经", "url": url})
    return news


def _merge_news(existing: List[Dict[str, str]], incoming: List[Dict[str, str]]) -> List[Dict[str, str]]:
    result = list(existing)
    seen = {(item.get("title", ""), item.get("url", "")) for item in result}
    for item in incoming:
        key = (item.get("title", ""), item.get("url", ""))
        if not item.get("title") or key in seen:
            continue
        result.append(item)
        seen.add(key)
    return result


def _fetch_announcements(code: str) -> List[Dict[str, str]]:
    requests = _requests()
    params = {
        "sr": "-1",
        "page_size": "5",
        "page_index": "1",
        "ann_type": "A",
        "client_source": "web",
        "f_node": "0",
        "s_node": "0",
        "stock_list": code,
    }
    response = requests.get(
        "https://np-anotice-stock.eastmoney.com/api/security/ann",
        params=params,
        headers=_headers(f"https://data.eastmoney.com/notices/stock/{code}.html"),
        timeout=12,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    rows = data.get("list") or data.get("announcements") or []
    announcements = []
    for row in rows[:5]:
        title = _clean_text(row.get("title"))
        art_code = str(row.get("art_code") or row.get("artCode") or "").strip()
        notice_date = str(row.get("notice_date") or row.get("noticeDate") or "")
        url = f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html" if art_code else ""
        if title:
            announcements.append({"title": title, "time": notice_date[:19], "source": "东方财富公告", "url": url})
    return announcements


def _evidence_status(
    fundamentals: Dict[str, Any],
    valuation: Dict[str, Any],
    money_flow: Dict[str, Any],
    news: List[Dict[str, str]],
    announcements: List[Dict[str, str]],
) -> tuple[Dict[str, str], List[str]]:
    status = {
        "fundamental": "已接入" if fundamentals.get("industry") or fundamentals.get("totalMarketCap") else "待接入",
        "valuation": "已接入" if valuation.get("peTtm") is not None or valuation.get("pb") is not None else "待接入",
        "moneyFlow": "已接入" if money_flow.get("date") else "待接入",
        "news": "已接入" if news or announcements else "待接入",
    }
    missing_map = {
        "fundamental": "基本面未接入",
        "valuation": "估值未接入",
        "moneyFlow": "资金流未接入",
        "news": "真实新闻公告未接入",
    }
    missing = [label for key, label in missing_map.items() if status.get(key) != "已接入"]
    return status, missing


def _read_disk_payload() -> Dict[str, Any]:
    if not RESEARCH_EVIDENCE_CACHE_PATH.exists():
        return {"version": 1, "items": {}}
    try:
        with RESEARCH_EVIDENCE_CACHE_PATH.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        LOGGER.warning("research evidence cache payload reset path=%s error=%s", RESEARCH_EVIDENCE_CACHE_PATH, error)
        return {"version": 1, "items": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "items": {}}
    payload.setdefault("items", {})
    return payload


def _requests() -> Any:
    try:
        import requests  # type: ignore
    except ModuleNotFoundError as error:  # pragma: no cover - local dependency guard.
        raise RuntimeError("requests 未安装，无法获取投研证据") from error
    return requests


def _headers(referer: str) -> Dict[str, str]:
    return {
        "Accept": "*/*",
        "Referer": referer,
        "User-Agent": "Mozilla/5.0",
    }


def _secid(code: str) -> str:
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    return f"{market}.{code}"


def _f10_code(code: str) -> str:
    suffix = "SH" if code.startswith(("5", "6", "9")) else "SZ"
    return f"{suffix}{code}"


def _unique_codes(codes: List[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for code in codes:
        normalized = str(code).strip().zfill(6)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _news_keywords(code: str, fundamentals: Dict[str, Any]) -> List[str]:
    keywords = [code]
    for key in ("shortName", "companyName"):
        value = _clean_text(fundamentals.get(key))
        if value and value not in keywords:
            keywords.append(value)
    return keywords


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _ratio_to_percent(value: Any) -> float | None:
    number = _as_float(value)
    if number is None:
        return None
    return round(number * 100, 4) if abs(number) <= 1 else number


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in {"-", "None", "nan"} else text


def _strip_tags(value: Any) -> str:
    return re.sub(r"<[^>]+>", "", _clean_text(value)).replace("\u3000", " ").strip()


def _parse_jsonp(text: str) -> Dict[str, Any]:
    start = text.find("(")
    end = text.rfind(")")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("东方财富新闻 JSONP 格式异常")
    return json.loads(text[start + 1 : end])


def _format_yyyymmdd(value: Any) -> str:
    text = _clean_text(value)
    if len(text) != 8 or not text.isdigit():
        return text
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"
