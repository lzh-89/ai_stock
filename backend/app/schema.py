from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class Catalyst:
    type: str
    time: str
    title: str
    url: str = ""


@dataclass
class RawStock:
    code: str
    name: str
    sector: str
    theme: str
    price: float
    change: float
    turnover: float
    volume_ratio: float
    amount_factor: float
    sector_rank: int
    sector_limit_ups: int
    rel_strength: float
    news_score: float
    risk_flags: List[str] = field(default_factory=list)
    catalysts: List[Catalyst] = field(default_factory=list)
    status_bias: str = "watch"
    series_seed: int = 1
    suspended: bool = False
    st: bool = False
    limit_status: str = "normal"
    kline_series: List[Dict[str, float]] = field(default_factory=list)
    kline_source: str = ""
    kline_period: str = ""
    amount: float = 0.0
    open: float | None = None
    high: float | None = None
    low: float | None = None
    pre_close: float | None = None
    trade_date: str = ""
    quote_time: str = ""
    price_basis: str = "unadjusted"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["catalysts"] = [asdict(item) for item in self.catalysts]
        return data
