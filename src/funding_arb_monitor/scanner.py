from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from .analytics import (
    negative_hour_share_pct,
    peak_decay_halflife_hours,
    realized_apr_pct,
    rolling_apr_pct,
)
from .alerts import render_scan_failure, send_discord_alert
from .hyperliquid import HyperliquidClient
from .costs import CostAssumptions, hedge_assessment
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
    min_estimated_net_7d_apr_pct: float = 0


class Scanner:
    def __init__(self, client: HyperliquidClient, store: Store, config: ScanConfig) -> None:
        self.client = client
        self.store = store
        self.config = config
        self.costs = CostAssumptions()

    def select_history_candidates(self, snapshots: list[MarketSnapshot]) -> list[MarketSnapshot]:
        liquid = [
            snapshot
            for snapshot in snapshots
            if snapshot.open_interest_usd >= self.config.min_open_interest_usd
        ]
        latest_by_coin = self.store.latest_funding_timestamps(
            [snapshot.coin for snapshot in liquid]
        )
        priority_count = min(
            len(liquid), max(1, self.config.max_history_fetches // 2)
        )
        priority = sorted(
            liquid, key=lambda item: abs(item.current_apr_pct), reverse=True
        )[:priority_count]
        priority_coins = {snapshot.coin for snapshot in priority}
        rotation = sorted(
            [snapshot for snapshot in liquid if snapshot.coin not in priority_coins],
            key=lambda item: (
                item.coin in latest_by_coin,
                latest_by_coin.get(item.coin, 0),
                -abs(item.current_apr_pct),
            ),
        )
        return (priority + rotation)[: self.config.max_history_fetches]

    def run(self) -> list[Candidate]:
        self.store.initialize()
        run_id = self.store.start_scan_run()
        try:
            return self._run(run_id)
        except Exception as exc:
            self.store.finish_scan_run(run_id, status="failed", error=str(exc))
            send_discord_alert(
                render_scan_failure(exc),
                store=self.store,
                event_type="scan_failed",
            )
            raise

    def _run(self, run_id: int) -> list[Candidate]:
        snapshots = self.client.snapshots()
        if not snapshots:
            raise RuntimeError("snapshot discovery returned no live markets")
        self.store.save_snapshots(snapshots)
        analyzed_at = utc_now()
        output: list[Candidate] = []
        failed_market_count = 0

        for snapshot in self.select_history_candidates(snapshots):
            window_start_ms = int((time.time() - self.config.days * 86_400) * 1000)
            latest_timestamp = self.store.latest_funding_timestamp(snapshot.coin)
            incremental_start = max(window_start_ms, (latest_timestamp + 1) if latest_timestamp else 0)
            refresh_failed = False
            try:
                new_history = self.client.funding_history(
                    snapshot.coin,
                    self.config.days,
                    start_time_ms=incremental_start,
                )
                self.store.save_funding(new_history)
            except RuntimeError as exc:
                refresh_failed = True
                failed_market_count += 1
                print(f"{snapshot.coin}: funding refresh failed: {exc}", file=sys.stderr)

            history = self.store.funding_history(snapshot.coin, window_start_ms)
            rates = [(point.timestamp_ms, point.funding_rate) for point in history]
            realized = realized_apr_pct(rates)
            seven_day = rolling_apr_pct(rates, 168)
            one_day = rolling_apr_pct(rates, 24)
            negative = negative_hour_share_pct(rates)
            if realized is None or negative is None:
                continue

            reasons: list[str] = []
            if refresh_failed:
                reasons.append("funding_refresh_failed")
            if history and history[-1].timestamp_ms < int((time.time() - 3 * 3_600) * 1000):
                reasons.append("funding_history_stale")
            estimated_net = (
                self.costs.estimated_net_annual_apr_pct(seven_day)
                if seven_day is not None
                else None
            )
            if seven_day is None:
                reasons.append("insufficient_7d_history")
            elif abs(seven_day) < self.config.min_realized_7d_apr_pct:
                reasons.append("7d_realized_apr_below_threshold")
            elif estimated_net is not None and estimated_net < self.config.min_estimated_net_7d_apr_pct:
                reasons.append("estimated_net_carry_below_cost_threshold")
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
                    estimated_net_7d_apr_pct=estimated_net,
                    hedge_assessment=hedge_assessment(snapshot.coin),
                    negative_hour_share_pct=negative,
                    peak_decay_halflife_hours=peak_decay_halflife_hours(rates),
                    eligible=not reasons,
                    reasons=tuple(reasons),
                    analyzed_at=analyzed_at,
                )
            )

        output.sort(key=lambda item: abs(item.realized_7d_apr_pct or 0), reverse=True)
        self.store.save_candidates(output)
        self.store.finish_scan_run(
            run_id,
            status="partial" if failed_market_count else "success",
            snapshot_count=len(snapshots),
            candidate_count=len(output),
            eligible_count=sum(candidate.eligible for candidate in output),
            failed_market_count=failed_market_count,
        )
        return output
