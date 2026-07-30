from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone


HOURS_PER_YEAR = 365 * 24


@dataclass(frozen=True)
class MarketSnapshot:
    dex: str
    coin: str
    funding_rate: float
    open_interest_usd: float
    day_volume_usd: float
    mark_price: float
    captured_at: datetime

    @property
    def current_apr_pct(self) -> float:
        return self.funding_rate * HOURS_PER_YEAR * 100


@dataclass(frozen=True)
class FundingPoint:
    coin: str
    timestamp_ms: int
    funding_rate: float


@dataclass(frozen=True)
class PerpQuote:
    coin: str
    dex: str
    bid: float
    ask: float
    executable_sell_price: float | None
    executable_buy_price: float | None
    bid_depth_usd: float
    ask_depth_usd: float
    captured_at_ms: int

    @property
    def spread_bps(self) -> float:
        midpoint = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / midpoint * 10_000


@dataclass(frozen=True)
class Candidate:
    dex: str
    coin: str
    side: str
    history_hours: int
    open_interest_usd: float
    day_volume_usd: float
    current_apr_pct: float
    realized_apr_pct: float
    realized_7d_apr_pct: float | None
    realized_24h_apr_pct: float | None
    estimated_net_7d_apr_pct: float | None
    hedge_assessment: str
    negative_hour_share_pct: float
    peak_decay_halflife_hours: int | None
    eligible: bool
    reasons: tuple[str, ...]
    analyzed_at: datetime

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["analyzed_at"] = self.analyzed_at.isoformat()
        result["reasons"] = list(self.reasons)
        return result


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
