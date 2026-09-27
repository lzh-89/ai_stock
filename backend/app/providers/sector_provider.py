from __future__ import annotations

import hashlib
import html
import json
import math
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime
from io import StringIO
from typing import Any, Callable, Dict, List
from urllib.parse import quote, urlencode

import requests


THS_CONCEPT_SCAN_TIMEOUT_SECONDS = 35


class SectorProviderError(RuntimeError):
    pass


class SectorProvider:
    source = "sector_provider"

    def fetch_sector_snapshot(self, sector_type: str = "all") -> Dict[str, Any]:
        raise NotImplementedError

    def fetch_stock_sector_profile(self, code: str) -> Dict[str, Any]:
        raise NotImplementedError


class AKShareEastmoneySectorProvider(SectorProvider):
    source = "akshare_eastmoney_sector"

    def fetch_sector_snapshot(self, sector_type: str = "all") -> Dict[str, Any]:
        try:
            import akshare as ak  # type: ignore
        except Exception as error:  # noqa: BLE001
            raise SectorProviderError(f"AKShare 未安装或不可用：{error}") from error

        sector_types = _sector_types(sector_type)
        sectors: List[Dict[str, Any]] = []
        warnings: List[str] = []
        actual_sources = set()
        for current_type in sector_types:
            try:
                rows = _df_to_records(_fetch_board_list(ak, current_type))
            except Exception as error:  # noqa: BLE001
                warnings.append(f"{current_type} 板块列表获取失败：{_clean_error(error)}")
                continue
            rows_have_fund_flow = any(_pick(row, ["主力净流入-净额", "净流入", "净流入（亿元）"]) not in {None, "", "-"} for row in rows)
            fund_rows = {} if rows_have_fund_flow else _fund_flow_by_name(ak, current_type, warnings)
            for row in rows:
                sector = _normalize_sector_row(row, current_type)
                if not sector.get("sectorName"):
                    continue
                if row.get("source"):
                    actual_sources.add(str(row.get("source")))
                fund_flow = fund_rows.get(str(sector["sectorName"]))
                if fund_flow:
                    sector.update(fund_flow)
                    sector["fundFlowStatus"] = "ok"
                elif sector.get("mainNetInflow") is not None:
                    sector["fundFlowStatus"] = "ok"
                else:
                    sector["fundFlowStatus"] = "missing"
                sectors.append(sector)

        if not sectors:
            raise SectorProviderError("未获取到行业/概念板块数据" + (f"：{'；'.join(warnings)}" if warnings else ""))
        actual_source = "ths_sector" if any(item.startswith("ths_") for item in actual_sources) else self.source

        return {
            "source": actual_source,
            "sectorType": sector_type,
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "warnings": warnings,
            "sectors": sectors,
        }

    def fetch_constituents(self, sector_name: str, sector_type: str, limit: int = 12) -> List[Dict[str, Any]]:
        try:
            import akshare as ak  # type: ignore
        except Exception as error:  # noqa: BLE001
            raise SectorProviderError(f"AKShare 未安装或不可用：{error}") from error
        try:
            rows = _fetch_constituents(ak, sector_name, sector_type)
        except Exception as error:  # noqa: BLE001
            raise SectorProviderError(f"{sector_name} 成分股获取失败：{_clean_error(error)}") from error
        result = [_normalize_constituent_row(row) for row in rows]
        result = [item for item in result if item.get("code") and item.get("code") != "000000" and item.get("name")]
        result = sorted(
            result,
            key=lambda item: (
                float(item.get("changePct") or 0),
                float(item.get("turnoverAmount") or 0),
                float(item.get("mainNetInflow") or 0),
            ),
            reverse=True,
        )
        return result[: max(1, limit)]

    def fetch_stock_sector_profile(self, code: str) -> Dict[str, Any]:
        return _with_retry(
            [
                lambda: _fetch_sina_stock_sector_profile(code),
                lambda: _fetch_eastmoney_stock_sector_profile(code),
            ],
            f"{code} 个股行业/概念",
            attempts=1,
        )


class MockSectorProvider(SectorProvider):
    source = "mock_sector"

    def fetch_sector_snapshot(self, sector_type: str = "all") -> Dict[str, Any]:
        sectors = []
        now = datetime.now().isoformat(timespec="seconds")
        for current_type, names in {
            "industry": ["半导体", "电力设备", "证券", "中药", "汽车零部件"],
            "concept": ["机器人概念", "算力租赁", "低空经济", "液冷服务器", "商业航天"],
        }.items():
            if sector_type not in {"all", current_type}:
                continue
            for index, name in enumerate(names, start=1):
                seed = _stable_seed(f"{current_type}-{name}")
                change = 3.8 - index * 0.35 + seed % 7 * 0.08
                sectors.append(
                    {
                        "sectorCode": f"MOCK{current_type[:1].upper()}{index:03d}",
                        "sectorName": name,
                        "sectorType": current_type,
                        "changePct": round(change, 2),
                        "turnoverAmount": 80_000_000_000 - index * 6_000_000_000,
                        "turnoverRate": round(2.5 + index * 0.25, 2),
                        "upCount": 22 - index,
                        "downCount": 5 + index,
                        "flatCount": 1,
                        "limitUpCount": max(0, 5 - index),
                        "mainNetInflow": 1_200_000_000 - index * 110_000_000,
                        "mainNetInflowRatio": round(7.2 - index * 0.6, 2),
                        "superLargeNetInflow": 420_000_000 - index * 35_000_000,
                        "superLargeNetInflowRatio": round(2.8 - index * 0.16, 2),
                        "fundFlowStatus": "ok",
                        "leaderName": f"{name}龙头",
                        "leaderCode": f"300{index:03d}",
                        "snapshotAt": now,
                    }
                )
        return {
            "source": self.source,
            "sectorType": sector_type,
            "generatedAt": now,
            "warnings": ["Mock 仅用于页面和接口诊断，不可用于真实交易决策。"],
            "sectors": sectors,
        }

    def fetch_constituents(self, sector_name: str, sector_type: str, limit: int = 12) -> List[Dict[str, Any]]:
        rows = []
        for index in range(1, max(1, limit) + 1):
            seed = _stable_seed(f"{sector_name}-{index}")
            rows.append(
                {
                    "code": f"{'30' if sector_type == 'concept' else '60'}{seed % 10000:04d}",
                    "name": f"{sector_name[:4]}{index}",
                    "changePct": round(7.5 - index * 0.48 + (seed % 5) * 0.12, 2),
                    "turnoverAmount": 3_000_000_000 - index * 120_000_000,
                    "turnoverRate": round(3.2 + index * 0.28, 2),
                    "volumeRatio": round(1.3 + index * 0.12, 2),
                    "mainNetInflow": 120_000_000 - index * 7_000_000,
                    "mainNetInflowRatio": round(5.5 - index * 0.28, 2),
                    "limitStatus": "limit_up" if index <= 1 else "normal",
                }
            )
        return rows

    def fetch_stock_sector_profile(self, code: str) -> Dict[str, Any]:
        seed = _stable_seed(code)
        industries = ["半导体", "电力设备", "证券", "中药", "汽车零部件"]
        concepts = ["机器人概念", "算力租赁", "低空经济", "液冷服务器", "商业航天"]
        return {
            "code": str(code or "").zfill(6),
            "name": f"Mock{str(code or '')[-2:]}",
            "industrySector": industries[seed % len(industries)],
            "conceptSectors": [concepts[seed % len(concepts)]],
            "source": self.source,
            "status": "partial",
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
        }


def get_sector_provider(source: str) -> SectorProvider:
    return MockSectorProvider() if source == "mock" else AKShareEastmoneySectorProvider()


def _sector_types(sector_type: str) -> List[str]:
    return ["industry", "concept"] if sector_type not in {"industry", "concept"} else [sector_type]


def _fetch_board_list(ak: Any, sector_type: str) -> Any:
    if sector_type == "industry":
        return _with_retry(
            [
                lambda: _fetch_board_list_direct("industry"),
                lambda: ak.stock_board_industry_name_em(),
                lambda: _fetch_ths_industry_snapshot(ak),
            ],
            "industry 板块列表",
        )
    return _with_retry(
        [
            lambda: _fetch_board_list_direct("concept"),
            lambda: ak.stock_board_concept_name_em(),
            lambda: _fetch_ths_concept_snapshot(ak),
        ],
        "concept 板块列表",
    )


def _fetch_constituents(ak: Any, sector_code_or_name: str, sector_type: str) -> List[Dict[str, Any]]:
    identifier = str(sector_code_or_name)
    if identifier.startswith("BK"):
        return _with_retry(
            [
                lambda: _fetch_constituents_direct(identifier),
                lambda: ak.stock_board_industry_cons_em(symbol=identifier)
                if sector_type == "industry"
                else ak.stock_board_concept_cons_em(symbol=identifier),
            ],
            f"{identifier} 成分股",
        )
    if identifier.startswith(("THSI", "THSC")):
        return _with_retry([lambda: _fetch_ths_constituents(str(sector_code_or_name), sector_type)], f"{sector_code_or_name} 成分股")
    ths_code = _ths_code_by_name(ak, identifier, sector_type)
    if sector_type == "industry":
        fetchers = [lambda: ak.stock_board_industry_cons_em(symbol=identifier)]
    else:
        fetchers = [lambda: ak.stock_board_concept_cons_em(symbol=identifier)]
    if ths_code:
        fetchers.append(lambda: _fetch_ths_constituents(ths_code, sector_type))
    return _with_retry(fetchers, f"{sector_code_or_name} 成分股")


def _ths_code_by_name(ak: Any, sector_name: str, sector_type: str) -> str:
    try:
        if sector_type == "industry":
            rows = _df_to_records(ak.stock_board_industry_name_ths())
            prefix = "THSI"
        else:
            rows = _df_to_records(ak.stock_board_concept_name_ths())
            prefix = "THSC"
    except Exception:
        return ""
    for row in rows:
        if str(row.get("name") or "") == sector_name:
            return f"{prefix}{row.get('code') or ''}"
    return ""


def _fetch_ths_industry_snapshot(ak: Any) -> List[Dict[str, Any]]:
    rows = _df_to_records(ak.stock_board_industry_summary_ths())
    names = _df_to_records(ak.stock_board_industry_name_ths())
    codes = {str(item.get("name") or ""): str(item.get("code") or "") for item in names}
    result = []
    for row in rows:
        name = str(_pick(row, ["板块"]) or "").strip()
        if not name:
            continue
        result.append(
            {
                "板块代码": f"THSI{codes.get(name) or _mock_code('industry', name)}",
                "板块名称": name,
                "涨跌幅": _pick(row, ["涨跌幅", "涨跌幅(%)"]),
                "成交额": _num(_pick(row, ["总成交额", "总成交额（亿元）"])) * 100000000,
                "成交量": _num(_pick(row, ["总成交量", "总成交量（万手）"])) * 10000,
                "主力净流入-净额": _num(_pick(row, ["净流入", "净流入（亿元）"])) * 100000000,
                "上涨家数": _pick(row, ["上涨家数"]),
                "下跌家数": _pick(row, ["下跌家数"]),
                "领涨股票": _pick(row, ["领涨股"]),
                "领涨股票代码": "",
                "source": "ths_industry_summary",
            }
        )
    if not result:
        raise SectorProviderError("同花顺行业实时表为空")
    return result


def _fetch_ths_concept_snapshot(ak: Any) -> List[Dict[str, Any]]:
    names = _df_to_records(ak.stock_board_concept_name_ths())
    targets = [(str(item.get("name") or "").strip(), str(item.get("code") or "").strip()) for item in names]
    targets = [(name, code) for name, code in targets if name and code]
    if not targets:
        raise SectorProviderError("同花顺概念名称表为空")
    result: List[Dict[str, Any]] = []
    errors: List[str] = []
    executor = ThreadPoolExecutor(max_workers=8)
    futures = {executor.submit(_fetch_ths_concept_info_by_name, ak, name, code): name for name, code in targets}
    timed_out = False
    try:
        for future in as_completed(futures, timeout=THS_CONCEPT_SCAN_TIMEOUT_SECONDS):
            try:
                row = future.result()
            except Exception as error:  # noqa: BLE001
                if len(errors) < 5:
                    errors.append(f"{futures[future]}：{_clean_error(error)}")
                continue
            if row:
                result.append(row)
    except TimeoutError:
        timed_out = True
        errors.append(f"同花顺概念详情扫描超过 {THS_CONCEPT_SCAN_TIMEOUT_SECONDS} 秒，已停止等待剩余请求")
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    if not result:
        raise SectorProviderError("同花顺概念详情扫描为空" + (f"：{'；'.join(errors)}" if errors else ""))
    if timed_out and len(result) < 80:
        raise SectorProviderError(f"同花顺概念详情扫描超时且样本不足：成功 {len(result)} 个；{'；'.join(errors[:5])}")
    if timed_out:
        result.append({"板块名称": "", "sourceWarning": f"同花顺概念详情扫描超时，成功 {len(result)} 个，部分概念缺失"})
    return result


def _fetch_ths_concept_info_by_name(ak: Any, name: str, code: str) -> Dict[str, Any]:
    rows = _df_to_records(ak.stock_board_concept_info_ths(symbol=name))
    values = {str(row.get("项目") or ""): row.get("值") for row in rows}
    up_count, down_count = _split_counts(values.get("涨跌家数"))
    return {
        "板块代码": f"THSC{code}",
        "板块名称": name,
        "涨跌幅": values.get("板块涨幅"),
        "成交额": _num(values.get("成交额(亿)")) * 100000000,
        "成交量": _num(values.get("成交量(万手)")) * 10000,
        "主力净流入-净额": _num(values.get("资金净流入(亿)")) * 100000000,
        "上涨家数": up_count,
        "下跌家数": down_count,
        "source": "ths_concept_info",
    }


def _fetch_ths_constituents(sector_code: str, sector_type: str) -> List[Dict[str, Any]]:
    path = "thshy" if sector_code.startswith("THSI") or sector_type == "industry" else "gn"
    code = sector_code[4:]
    headers = _ths_headers()
    headers.update({"Host": "q.10jqka.com.cn", "Referer": f"http://q.10jqka.com.cn/{path}/"})
    response = requests.get(
        f"http://q.10jqka.com.cn/{path}/detail/code/{code}/",
        headers=headers,
        timeout=15,
    )
    response.raise_for_status()
    tables = _read_html_tables(response.text)
    if not tables:
        raise SectorProviderError("同花顺详情页没有成分股表")
    rows = _df_to_records(tables[0])
    return [
        {
            "代码": item.get("代码"),
            "名称": item.get("名称"),
            "涨跌幅": _pick(item, ["涨跌幅(%)", "涨跌幅"]),
            "成交额": item.get("成交额"),
            "换手率": _pick(item, ["换手(%)", "换手率"]),
            "量比": item.get("量比"),
        }
        for item in rows
    ]


def _fetch_board_list_direct(sector_type: str) -> List[Dict[str, Any]]:
    base_url = (
        "https://17.push2.eastmoney.com/api/qt/clist/get"
        if sector_type == "industry"
        else "https://79.push2.eastmoney.com/api/qt/clist/get"
    )
    params = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3" if sector_type == "industry" else "f12",
        "fs": "m:90 t:2" if sector_type == "industry" else "m:90 t:3",
        "fields": "f3,f6,f8,f12,f14,f62,f104,f105,f128,f140",
    }
    rows = [
        row
        for row in _eastmoney_clist(params, base_url=base_url, fallback_urls=["https://push2.eastmoney.com/api/qt/clist/get"])
        if str(row.get("f12") or "").startswith("BK")
    ]
    result = [
        {
            "板块代码": row.get("f12"),
            "板块名称": row.get("f14"),
            "涨跌幅": row.get("f3"),
            "成交额": row.get("f6"),
            "换手率": row.get("f8"),
            "主力净流入-净额": row.get("f62"),
            "上涨家数": row.get("f104"),
            "下跌家数": row.get("f105"),
            "领涨股票": row.get("f128"),
            "领涨股票代码": row.get("f140"),
            "source": "eastmoney_industry_board" if sector_type == "industry" else "eastmoney_concept_board",
        }
        for row in rows
    ]
    if not result:
        raise SectorProviderError("东财直连板块列表返回空数据")
    return result


def _fetch_constituents_direct(sector_code: str) -> List[Dict[str, Any]]:
    params = {
        "pn": "1",
        "pz": "200",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": f"b:{sector_code} f:!50",
        "fields": "f2,f3,f6,f8,f10,f12,f14,f62,f184",
    }
    rows = _eastmoney_clist(
        params,
        base_url="https://29.push2.eastmoney.com/api/qt/clist/get",
        fallback_urls=["https://push2.eastmoney.com/api/qt/clist/get"],
    )
    return [
        {
            "代码": row.get("f12"),
            "名称": row.get("f14"),
            "涨跌幅": row.get("f3"),
            "成交额": row.get("f6"),
            "换手率": row.get("f8"),
            "量比": row.get("f10"),
            "主力净流入-净额": row.get("f62"),
            "主力净流入-净占比": row.get("f184"),
        }
        for row in rows
    ]


def _eastmoney_clist(
    params: Dict[str, Any],
    base_url: str = "https://push2.eastmoney.com/api/qt/clist/get",
    fallback_urls: List[str] | None = None,
) -> List[Dict[str, Any]]:
    urls = _unique_urls([base_url] + list(fallback_urls or []))
    errors: List[str] = []

    def request_page(url: str, page: int) -> tuple[List[Dict[str, Any]], int]:
        page_params = dict(params)
        page_params["pn"] = str(page)
        payload: Dict[str, Any] | None = None
        for attempt in range(1, 4):
            try:
                response = requests.get(
                    url,
                    params=page_params,
                    timeout=15,
                    headers={
                        "Accept": "application/json,text/plain,*/*",
                        "Referer": "https://quote.eastmoney.com/",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                break
            except Exception as error:  # noqa: BLE001 - Eastmoney occasionally closes board-list pages.
                if attempt >= 3:
                    try:
                        payload = _eastmoney_json_via_curl(url, page_params)
                    except Exception as curl_error:  # noqa: BLE001
                        raise SectorProviderError(f"requests/curl 均失败：{_clean_error(error)}；{_clean_error(curl_error)}") from curl_error
                time.sleep(0.35 * attempt)
        if payload is None:
            raise SectorProviderError("东财直连返回空响应")
        data = (payload or {}).get("data") or {}
        rows = data.get("diff") or []
        if not isinstance(rows, list):
            return [], int(_num(data.get("total")) or 0)
        return [dict(row) for row in rows if isinstance(row, dict)], int(_num(data.get("total")) or 0)

    for url in urls:
        try:
            rows, total = request_page(url, int(_num(params.get("pn")) or 1))
            if rows and total > len(rows):
                page_size = max(1, len(rows))
                max_pages = min(10, math.ceil(total / page_size))
                for page in range(2, max_pages + 1):
                    page_rows, _ = request_page(url, page)
                    if not page_rows:
                        break
                    rows.extend(page_rows)
                    if len(rows) >= total:
                        break
            if rows:
                return rows
            errors.append(f"{url} 返回空数据")
        except Exception as error:  # noqa: BLE001
            errors.append(f"{url}：{_clean_error(error)}")
    raise SectorProviderError("东财直连返回空数据" + (f"：{'；'.join(errors)}" if errors else ""))


def _fetch_sina_stock_sector_profile(code: str) -> Dict[str, Any]:
    normalized = str(code or "").strip().zfill(6)
    if not normalized or normalized == "000000":
        raise SectorProviderError("股票代码为空")
    url = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpOtherInfo/stockid/{normalized}/menu_num/2.phtml"
    response = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    text = response.content.decode("gb18030", errors="ignore")
    industry = _extract_sina_industry(text)
    concepts = _extract_sina_concepts(text)
    if not industry and not concepts:
        raise SectorProviderError(f"{normalized} 新浪行业/概念为空")
    return {
        "code": normalized,
        "name": _extract_sina_stock_name(text),
        "industrySector": industry,
        "conceptSectors": concepts[:12],
        "regionSector": "",
        "mainNetInflow": None,
        "source": "sina_stock_profile",
        "status": "partial",
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
    }


def _extract_sina_industry(text: str) -> str:
    marker = "所属行业板块"
    start = text.rfind(marker)
    if start < 0:
        return ""
    concept_start = text.find("所属概念板块", start + len(marker))
    section = text[start:concept_start] if concept_start > start else text[start : start + 2000]
    cells = _extract_html_cells(section)
    for cell in cells:
        if cell in {"所属行业板块", "同行业个股", "点击查看"} or cell.startswith("备注"):
            continue
        return cell
    return ""


def _extract_sina_concepts(text: str) -> List[str]:
    marker = "所属概念板块"
    start = text.rfind(marker)
    if start < 0:
        return []
    table_end = text.find("</table>", start)
    section = text[start:table_end] if table_end > start else text[start : start + 4000]
    concepts = []
    for cell in _extract_html_cells(section):
        if cell in {"所属概念板块", "概念板块", "同概念个股", "点击查看"}:
            continue
        if cell.startswith("备注"):
            continue
        concepts.append(cell)
    return _unique_texts(concepts)


def _extract_sina_stock_name(text: str) -> str:
    match = re.search(r"<div class=\"r-title\".*?<a[^>]*>([^<]+)</a>", text, re.S)
    if not match:
        return ""
    value = _clean_html_text(match.group(1))
    return re.sub(r"\(\d{6}\).*", "", value).strip()


def _extract_html_cells(section: str) -> List[str]:
    values = []
    for match in re.finditer(r"<td[^>]*>(.*?)</td>|<th[^>]*>(.*?)</th>", section, re.S | re.I):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        value = _clean_html_text(raw)
        if value:
            values.append(value)
    return values


def _clean_html_text(value: Any) -> str:
    text = re.sub(r"<.*?>", "", str(value or ""), flags=re.S)
    text = html.unescape(text)
    text = re.sub(r"\s+", "", text)
    return text.strip()


def _unique_texts(values: List[Any]) -> List[str]:
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _fetch_eastmoney_stock_sector_profile(code: str) -> Dict[str, Any]:
    normalized = str(code or "").strip().zfill(6)
    if not normalized or normalized == "000000":
        raise SectorProviderError("股票代码为空")
    params = {
        "secid": _eastmoney_stock_secid(normalized),
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fields": "f57,f58,f100,f102,f103,f127,f128,f129",
    }
    payload = _eastmoney_stock_get(params)
    data = (payload or {}).get("data") or {}
    industry = _clean_sector_name(data.get("f127"))
    concepts = [_clean_sector_name(item) for item in str(data.get("f129") or "").replace("，", ",").split(",")]
    concepts = [item for item in concepts if item]
    if not industry and not concepts:
        raise SectorProviderError(f"{normalized} 东财行业/概念为空")
    return {
        "code": normalized,
        "name": str(data.get("f58") or ""),
        "industrySector": industry,
        "conceptSectors": concepts[:12],
        "regionSector": _clean_sector_name(data.get("f128")),
        "mainNetInflow": _optional_num(data.get("f103")),
        "source": "eastmoney_stock_profile",
        "status": "partial",
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
    }


def _eastmoney_stock_get(params: Dict[str, Any]) -> Dict[str, Any]:
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    for attempt in range(1, 4):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=12,
                headers={
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": "https://quote.eastmoney.com/",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            response.raise_for_status()
            return response.json()
        except Exception as error:  # noqa: BLE001 - keep stock profile fallback resilient.
            if attempt >= 3:
                try:
                    return _eastmoney_json_via_curl(url, params)
                except Exception as curl_error:  # noqa: BLE001
                    raise SectorProviderError(f"东财个股行业/概念请求失败：{_clean_error(error)}；{_clean_error(curl_error)}") from curl_error
            time.sleep(0.25 * attempt)
    raise SectorProviderError("东财个股行业/概念请求失败")


def _eastmoney_stock_secid(code: str) -> str:
    normalized = str(code or "").strip().zfill(6)
    market = "1" if normalized.startswith(("5", "6", "9")) else "0"
    return f"{market}.{normalized}"


def _clean_sector_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text == "-":
        return ""
    return text


def _eastmoney_json_via_curl(base_url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{base_url}?{urlencode(params, quote_via=quote)}"
    completed = subprocess.run(
        [
            "curl",
            "-sS",
            "--max-time",
            "15",
            url,
            "-H",
            "Accept: application/json,text/plain,*/*",
            "-H",
            "Referer: https://quote.eastmoney.com/",
            "-H",
            "User-Agent: Mozilla/5.0",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=18,
    )
    if completed.returncode != 0:
        raise SectorProviderError(f"curl 返回 {completed.returncode}: {(completed.stderr or completed.stdout)[:200]}")
    text = (completed.stdout or "").strip()
    if not text:
        raise SectorProviderError("curl 返回空数据")
    return json.loads(text)


def _unique_urls(urls: List[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def _fund_flow_by_name(ak: Any, sector_type: str, warnings: List[str]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    sector_arg = "行业资金流" if sector_type == "industry" else "概念资金流"
    for indicator in ["今日", "5日", "10日"]:
        try:
            rows = _df_to_records(ak.stock_sector_fund_flow_rank(indicator=indicator, sector_type=sector_arg))
        except Exception as error:  # noqa: BLE001
            if indicator == "今日":
                warnings.append(f"{sector_type} 今日资金流获取失败：{_clean_error(error)}")
            continue
        for row in rows:
            name = _pick(row, ["名称", "板块名称", "行业", "概念"])
            if not name:
                continue
            target = result.setdefault(str(name), {})
            if indicator == "今日":
                target.update(_normalize_fund_flow_row(row))
            elif indicator == "5日":
                target["fiveDayMainNetInflow"] = _num(_pick(row, ["主力净流入-净额", "主力净流入净额", "今日主力净流入净额"]))
            elif indicator == "10日":
                target["tenDayMainNetInflow"] = _num(_pick(row, ["主力净流入-净额", "主力净流入净额", "今日主力净流入净额"]))
    return result


def _normalize_sector_row(row: Dict[str, Any], sector_type: str) -> Dict[str, Any]:
    name = str(_pick(row, ["板块名称", "名称", "行业名称", "概念名称"]) or "").strip()
    code = str(_pick(row, ["板块代码", "代码"]) or _mock_code(sector_type, name)).strip()
    up_count = _num(_pick(row, ["上涨家数", "上涨数"]))
    down_count = _num(_pick(row, ["下跌家数", "下跌数"]))
    return {
        "sectorCode": code,
        "sectorName": name,
        "sectorType": sector_type,
        "changePct": _num(_pick(row, ["涨跌幅", "涨跌幅%"])),
        "turnoverAmount": _num(_pick(row, ["成交额", "成交金额"])),
        "turnoverRate": _num(_pick(row, ["换手率"])),
        "upCount": int(up_count or 0),
        "downCount": int(down_count or 0),
        "flatCount": int(_num(_pick(row, ["平盘家数", "平盘数"])) or 0),
        "limitUpCount": int(_num(_pick(row, ["涨停家数", "涨停数"])) or 0),
        "mainNetInflow": _optional_num(_pick(row, ["主力净流入-净额", "主力净流入净额", "今日主力净流入净额", "净流入", "净流入（亿元）"])),
        "leaderName": str(_pick(row, ["领涨股票", "领涨股"]) or ""),
        "leaderCode": str(_pick(row, ["领涨股票代码", "领涨股代码"]) or ""),
        "snapshotAt": datetime.now().isoformat(timespec="seconds"),
    }


def _normalize_fund_flow_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "mainNetInflow": _num(_pick(row, ["主力净流入-净额", "主力净流入净额", "今日主力净流入净额"])),
        "mainNetInflowRatio": _num(_pick(row, ["主力净流入-净占比", "主力净占比", "今日主力净流入净占比"])),
        "superLargeNetInflow": _num(_pick(row, ["超大单净流入-净额", "超大单净流入净额"])),
        "superLargeNetInflowRatio": _num(_pick(row, ["超大单净流入-净占比", "超大单净占比"])),
    }


def _normalize_constituent_row(row: Dict[str, Any]) -> Dict[str, Any]:
    code = str(_pick(row, ["代码", "股票代码"]) or "").strip().zfill(6)
    change = _num(_pick(row, ["涨跌幅", "涨跌幅%"]))
    return {
        "code": code,
        "name": str(_pick(row, ["名称", "股票名称"]) or "").strip(),
        "changePct": change,
        "turnoverAmount": _num(_pick(row, ["成交额", "成交金额"])),
        "turnoverRate": _num(_pick(row, ["换手率"])),
        "volumeRatio": _num(_pick(row, ["量比"])),
        "mainNetInflow": _num(_pick(row, ["主力净流入", "主力净流入-净额", "主力净流入净额"])),
        "mainNetInflowRatio": _num(_pick(row, ["主力净占比", "主力净流入-净占比"])),
        "limitStatus": "limit_up" if change >= 9.8 else "near_limit_up" if change >= 8 else "normal",
    }


def _df_to_records(frame: Any) -> List[Dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return [dict(item) for item in frame.to_dict(orient="records")]
    if isinstance(frame, list):
        return [dict(item) for item in frame if isinstance(item, dict)]
    return []


def _read_html_tables(text: str) -> List[Any]:
    try:
        import pandas as pd  # type: ignore
    except Exception as error:  # noqa: BLE001
        raise SectorProviderError(f"pandas 不可用：{error}") from error
    return list(pd.read_html(StringIO(text)))


def _pick(row: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in {"", None, "-"}:
            return row[key]
    return None


def _num(value: Any) -> float:
    text = str(value or "").replace(",", "").replace("%", "").strip()
    if not text or text == "-":
        return 0.0
    multiplier = 1.0
    if text.endswith("亿"):
        multiplier = 100000000.0
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 10000.0
        text = text[:-1]
    try:
        number = float(text) * multiplier
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return number


def _optional_num(value: Any) -> float | None:
    if value in {"", None, "-"}:
        return None
    return _num(value)


def _split_counts(value: Any) -> tuple[int, int]:
    text = str(value or "").strip()
    if "/" not in text:
        return 0, 0
    left, right = text.split("/", 1)
    return int(_num(left)), int(_num(right))


def _ths_headers() -> Dict[str, str]:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        from py_mini_racer import MiniRacer  # type: ignore
        import akshare.stock_feature.stock_board_concept_ths as ths  # type: ignore

        js_code = MiniRacer()
        js_code.eval(ths._get_file_content_ths("ths.js"))
        headers["Cookie"] = f"v={js_code.call('v')}"
    except Exception:
        pass
    return headers


def _mock_code(sector_type: str, name: str) -> str:
    prefix = "IND" if sector_type == "industry" else "CON"
    return f"{prefix}{_stable_seed(name) % 100000:05d}"


def _stable_seed(value: str) -> int:
    return int(hashlib.sha1(value.encode("utf-8")).hexdigest()[:8], 16)


def _with_retry(fetchers: List[Callable[[], Any]], label: str, attempts: int = 3) -> Any:
    last_error: Exception | None = None
    errors: List[str] = []
    for fetcher in fetchers:
        for attempt in range(1, attempts + 1):
            try:
                return fetcher()
            except Exception as error:  # noqa: BLE001
                last_error = error
                if attempt >= attempts:
                    errors.append(_clean_error(error))
                    break
                time.sleep(0.4 * attempt)
    detail = "；".join(_unique_texts(errors[-4:])) if errors else _clean_error(last_error)
    raise SectorProviderError(f"{label}连续请求失败：{detail}") from last_error


def _clean_error(error: Any) -> str:
    text = str(error or "").strip()
    if not text:
        return error.__class__.__name__ if error else "unknown error"
    return text[:300]
