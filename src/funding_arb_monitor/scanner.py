from __future__ import annotations

from dataclasses import dataclass

from .analytics import (
    negative_hour_share_pct,
    peak_decay_halflife_hours,
    realized_apr_pct,
    rolling_apr_pct,
)
from .hyperliquid import HyperliquidClient
from .models import Candidate, MarketSnapshot, utc_now
from .store import Store


@dataclass(frozen=True)
class ScanConfig:
    days: int = 30
    min_open_interest_usd: float = 1_000_000
    max_history_fetches: int = 40
    min_realized_7d_apr_pct: float = 15
    max_negative_hour_share_pct: float = 25
    min_day_volume_usd: float = 500_000


class Scanner:
    def __init__(self, client: HyperliquidClient, store: Store, config: ScanConfig) -> None:
        self.client = client
        self.store = store
        self.config = config

    def select_history_candidates(self, snapshots: list[MarketSnapshot]) -> list[MarketSnapshot]:
        liquid = [
            snapshot
            for snapshot in snapshots
            if snapshot.open_interest_usd >= self.config.min_open_interest_usd
        ]
        return sorted(liquid, key=lambda item: abs(item.current_apr_pct), reverse=True)[
            : self.config.max_history_fetches
        ]

    def run(self) -> list[Candidate]:
        self.store.initialize()
        snapshots = self.client.snapshots()
        self.store.save_snapshots(snapshots)
        analyzed_at = utc_now()
        output: list[Candidate] = []

        for snapshot in self.select_history_candidates(snapshots):
            history = self.client.funding_history(snapshot.coin, self.config.days)
            self.store.save_funding(history)
            rates = [(point.timestamp_ms, point.funding_rate) for point in history]
            realized = realized_apr_pct(rates)
            seven_day = rolling_apr_pct(rates, 168)
            one_day = rolling_apr_pct(rates, 24)
            negative = negative_hour_share_pct(rates)
            if realized is None or negative is None:
                continue

            reasons: list[str] = []
            if seven_day is None:
                reasons.append("insufficient_7d_history")
            elif abs(seven_day) < self.config.min_realized_7d_apr_pct:
                reasons.append("7d_realized_apr_below_threshold")
            if negative > self.config.max_negative_hour_share_pct:
                reasons.append("funding_reverses_too_often")
            if snapshot.day_volume_usd < self.config.min_day_volume_usd:
                reasons.append("day_volume_below_threshold")

            # A positive rate is mechanically hedgeable as short perp / long underlying.
            # Negative funding needs long perp / short underlying and must be assessed
            # separately for borrow availability; it cannot be auto-approved.
            side = "short_perp_long_hedge" if (seven_day or 0) > 0 else "long_perp_short_hedge"
            if side == "long_perp_short_hedge":
                reasons.append("requires_underlying_short_borrow_review")

            output.append(
                Candidate(
                    dex=snapshot.dex,
                    coin=snapshot.coin,
                    side=side,
                    history_hours=len(rates),
                    open_interest_usd=snapshot.open_interest_usd,
                    day_volume_usd=snapshot.day_volume_usd,
                    current_apr_pct=snapshot.current_apr_pct,
                    realized_apr_pct=realized,
                    realized_7d_apr_pct=seven_day,
                    realized_24h_apr_pct=one_day,
                    negative_hour_share_pct=negative,
                    peak_decay_halflife_hours=peak_decay_halflife_hours(rates),
                    eligible=not reasons,
                    reasons=tuple(reasons),
                    analyzed_at=analyzed_at,
                )
            )

        output.sort(key=lambda item: abs(item.realized_7d_apr_pct or 0), reverse=True)
        self.store.save_candidates(output)
        return output
