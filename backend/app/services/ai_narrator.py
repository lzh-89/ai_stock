from __future__ import annotations

import http.client
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from backend.app.settings import Settings


LOGGER = logging.getLogger("ai_stock.ai")


ROLE_INSTRUCTIONS = """你是一个规则锁定的 A 股盘中分析员。

你的任务是解释系统已经计算出的候选股票、规则检查、风险和时间窗。

必须遵守：
1. 只能基于输入数据和规则结果分析，不能编造行情、新闻、公告。
2. 不能推荐候选池之外的股票。
3. 不能把观察窗口写成确定性买卖指令。
4. 必须说明买入条件、卖出条件、失效条件和风险。
5. 涨停、跌停、接近涨停时必须说明可买性或可卖性限制。
6. 禁止使用“必涨”“稳赚”“满仓”“无风险”等确定性表达。

输出 3 到 5 句中文，语气像专业盘中分析员，结论必须谨慎。"""


class AINarrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_error = ""
        self.last_mode = "rules"

    def explain(self, candidate: Dict[str, Any], market: Dict[str, Any], use_ai: bool = True) -> List[str]:
        self.last_error = ""
        self.last_mode = "rules"
        if use_ai and self.settings.enable_ai and self.settings.ai_api_key and self.settings.ai_base_url:
            text, error = self._explain_with_chat_api(candidate, market)
            if text:
                self.last_mode = self.settings.ai_provider
                return [part.strip() for part in text.split("\n") if part.strip()]
            self.last_error = error or f"{self.settings.ai_provider} 未返回有效解释"
        return self._rule_narrative(candidate)

    def _rule_narrative(self, item: Dict[str, Any]) -> List[str]:
        trade = item.get("tradeability", {})
        risk_flags = item.get("riskFlags") or []
        return [
            f"技术面 {item['dimensions']['technical']}/100：当前定义为{item['style']}，价格相对 VWAP 和 15 分钟 MACD 是主要确认条件。",
            f"量能 {item['dimensions']['volume']}/100：量比 {item['volumeRatio']:.1f}，成交额放大 {item['amountFactor']:.2f}x，分数只代表观察优先级。",
            f"板块 {item['dimensions']['sector']}/100：{item['sector']} 排名第 {item['sectorRank']}，需要继续看板块是否保持前五。",
            f"可买卖性：{trade.get('reason', '未触发特殊限制')}",
            f"风险：{('、'.join(risk_flags) if risk_flags else '暂无强风险标签')}；跌破 VWAP 或早盘低点时规则降级。",
        ]

    def _explain_with_chat_api(self, candidate: Dict[str, Any], market: Dict[str, Any]) -> tuple[str, str]:
        user_payload = {
            "market": market,
            "candidate": _compact_candidate(candidate),
        }
        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {"role": "system", "content": ROLE_INSTRUCTIONS},
                {
                    "role": "user",
                    "content": (
                        "请只基于以下规则引擎 JSON 输出盘中解释。"
                        "每句独立成行，不要编号，不要输出 Markdown。\n"
                        f"{json.dumps(user_payload, ensure_ascii=False)}"
                    ),
                },
            ],
            "temperature": 0.2,
            "max_tokens": 650,
            "stream": False,
        }
        if self.settings.ai_provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        last_error = ""
        code = candidate.get("code", "")
        for attempt in range(1, 3):
            started = time.perf_counter()
            LOGGER.info(
                "ai call start provider=%s model=%s code=%s attempt=%s",
                self.settings.ai_provider,
                self.settings.ai_model,
                code,
                attempt,
            )
            request = urllib.request.Request(
                _chat_completions_url(self.settings.ai_base_url),
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.settings.ai_api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                LOGGER.warning(
                    "ai call http_error provider=%s code=%s attempt=%s elapsed_ms=%.0f status=%s",
                    self.settings.ai_provider,
                    code,
                    attempt,
                    (time.perf_counter() - started) * 1000,
                    error.code,
                )
                return "", _http_error_message(error)
            except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.IncompleteRead, json.JSONDecodeError) as error:
                last_error = f"{self.settings.ai_provider} 调用失败：{error}"
                LOGGER.warning(
                    "ai call failed provider=%s code=%s attempt=%s elapsed_ms=%.0f error=%s",
                    self.settings.ai_provider,
                    code,
                    attempt,
                    (time.perf_counter() - started) * 1000,
                    error,
                )
                continue
            LOGGER.info(
                "ai call ok provider=%s code=%s attempt=%s elapsed_ms=%.0f",
                self.settings.ai_provider,
                code,
                attempt,
                (time.perf_counter() - started) * 1000,
            )
            return _extract_chat_content(data), ""
        return "", last_error


def _chat_completions_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


def _extract_chat_content(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        return str(message.get("content", "")).strip()
    return str(data.get("output_text", "")).strip()


def _http_error_message(error: urllib.error.HTTPError) -> str:
    try:
        raw = error.read().decode("utf-8")
    except Exception:  # noqa: BLE001 - error bodies are best effort only.
        raw = ""
    if not raw:
        return f"AI 接口返回 HTTP {error.code}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return f"AI 接口返回 HTTP {error.code}: {raw[:160]}"
    message = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else data.get("message")
    return f"AI 接口返回 HTTP {error.code}: {message or raw[:160]}"


def _compact_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "rank",
        "code",
        "name",
        "sector",
        "theme",
        "score",
        "style",
        "status",
        "change",
        "turnover",
        "volumeRatio",
        "amountFactor",
        "sectorRank",
        "riskFlags",
        "tradeability",
        "dimensions",
        "checks",
        "buyWindow",
        "buyTrigger",
        "sellWindow",
        "sellTrigger",
        "invalidCondition",
    ]
    return {key: candidate.get(key) for key in keys}
