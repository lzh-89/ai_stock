from __future__ import annotations

import math
from typing import Dict, List


def generate_series(seed: int, base_price: float, bias: str) -> List[Dict[str, float]]:
    points: List[Dict[str, float]] = []
    close = base_price * (0.985 + seed * 0.001)
    trend = 0.0024 if bias == "triggered" else 0.0012 if bias == "pullback" else 0.0017
    for index in range(48):
        wave = math.sin((index + seed) / 4) * 0.005 + math.cos((index + seed) / 9) * 0.003
        drift = trend * index / 48
        open_price = close
        close = base_price * (0.982 + drift + wave + index * 0.00038)
        if bias == "pullback" and 22 < index < 32:
            close *= 0.992
        if bias == "triggered" and index > 14:
            close *= 1.005
        high = max(open_price, close) * (1 + 0.004 + ((seed + index) % 4) * 0.0007)
        low = min(open_price, close) * (1 - 0.004 - ((seed + index) % 3) * 0.0008)
        volume = 80 + index * 2 + abs(math.sin((index + seed) / 3)) * 70 + (55 if bias == "triggered" and index > 14 else 0)
        points.append({"open": open_price, "high": high, "low": low, "close": close, "volume": volume})
    return points


def ema(values: List[float], period: int) -> List[float]:
    factor = 2 / (period + 1)
    result: List[float] = []
    for index, value in enumerate(values):
        result.append(value if index == 0 else value * factor + result[index - 1] * (1 - factor))
    return result


def macd(series: List[Dict[str, float]]) -> List[Dict[str, float]]:
    closes = [item["close"] for item in series]
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    dif = [value - ema26[index] for index, value in enumerate(ema12)]
    dea = ema(dif, 9)
    return [{"dif": value, "dea": dea[index], "hist": (value - dea[index]) * 2} for index, value in enumerate(dif)]


def vwap(series: List[Dict[str, float]]) -> List[float]:
    amount = 0.0
    volume = 0.0
    values: List[float] = []
    for item in series:
        typical = (item["high"] + item["low"] + item["close"]) / 3
        amount += typical * item["volume"]
        volume += item["volume"]
        values.append(amount / volume if volume else typical)
    return values


def moving_average(series: List[Dict[str, float]], period: int) -> List[float]:
    result: List[float] = []
    for index, _ in enumerate(series):
        start = max(0, index - period + 1)
        window = series[start : index + 1]
        result.append(sum(item["close"] for item in window) / len(window))
    return result

