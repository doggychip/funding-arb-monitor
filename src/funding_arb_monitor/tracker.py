from __future__ import annotations

import time
from datetime import datetime

from .alerts import (
    render_liquidity_warning,
    render_shadow_exit,
    send_discord_alert,
)
from .costs import CostAssumptions
from .models import FundingPoint
from .store import Store
from .venues import BinanceSpot, CoinbaseSpot, KrakenSpot, OkxSpot


class PaperPositionTracker:
    def __init__(
        self,
        store: Store,
        *,
        max_hedge_drift_pct: float = 2,
        max_holding_days: int = 7,
        max_snapshot_age_seconds: int = 15 * 60,
        min_day_volume_usd: float = 500_000,
        min_depth_multiple: float = 2,
    ) -> None:
        self.store = store
        self.venues = {
            "okx": OkxSpot(),
            "binance": BinanceSpot(),
            "coinbase": CoinbaseSpot(),
            "kraken": KrakenSpot(),
        }
        self.max_hedge_drift_pct = max_hedge_drift_pct
        self.max_holding_days = max_holding_days
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.min_day_volume_usd = min_day_volume_usd
        self.min_depth_multiple = min_depth_multiple
        self.costs = CostAssumptions()
        self.perp_fee_bps = self.costs.perp_fill_bps

    def update(self) -> dict[str, int]:
        updated = 0
        closed = 0
        for position in self.store.open_paper_positions():
            if not all(
                position.get(field) is not None
                for field in ("perp_entry_price", "hedge_entry_price", "quantity", "hedge_symbol")
            ):
                continue
            venue = self.venues.get(str(position["hedge_venue"]))
            snapshot = self.store.latest_market_snapshot(str(position["coin"]))
            if venue is None or snapshot is None:
                continue
            captured_at = datetime.fromisoformat(str(snapshot["captured_at"])).timestamp()
            if time.time() - captured_at > self.max_snapshot_age_seconds:
                continue
            try:
                quote = venue.quote(
                    str(position["coin"]).split(":", 1)[-1],
                    float(position["notional_usd"]),
                )
            except RuntimeError:
                continue
            if quote is None:
                continue
            perp_price = float(snapshot["mark_price"])
            quantity = float(position["quantity"])
            if position["side"] == "short_perp_long_hedge":
                perp_pnl = (float(position["perp_entry_price"]) - perp_price) * quantity
                hedge_pnl = (quote.executable_sell_price - float(position["hedge_entry_price"])) * quantity
            else:
                perp_pnl = (perp_price - float(position["perp_entry_price"])) * quantity
                hedge_pnl = (float(position["hedge_entry_price"]) - quote.executable_buy_price) * quantity
            drift = abs(perp_price - quote.mid) / ((perp_price + quote.mid) / 2) * 100
            self.store.save_paper_mark(
                int(position["id"]),
                timestamp_ms=quote.captured_at_ms,
                perp_price=perp_price,
                hedge_price=quote.mid,
                perp_pnl_usd=perp_pnl,
                hedge_pnl_usd=hedge_pnl,
                hedge_drift_pct=drift,
            )
            updated += 1
            liquidity_reasons: list[str] = []
            if float(snapshot["day_volume_usd"]) < self.min_day_volume_usd:
                liquidity_reasons.append("perp_day_volume_below_500k")
            required_depth = float(position["notional_usd"]) * self.min_depth_multiple
            if min(quote.bid_depth_usd, quote.ask_depth_usd) < required_depth:
                liquidity_reasons.append("spot_depth_below_2x_notional")
            self.store.save_paper_liquidity_check(
                int(position["id"]),
                timestamp_ms=quote.captured_at_ms,
                day_volume_usd=float(snapshot["day_volume_usd"]),
                bid_depth_usd=quote.bid_depth_usd,
                ask_depth_usd=quote.ask_depth_usd,
                reasons=liquidity_reasons,
            )
            liquidity_streak = self.store.liquidity_degradation_streak(
                int(position["id"])
            )
            if liquidity_streak == 1:
                send_discord_alert(
                    render_liquidity_warning(
                        position, liquidity_reasons, liquidity_streak
                    ),
                    store=self.store,
                    event_type="position_liquidity_warning",
                )

            candidate = self.store.latest_candidate(str(position["coin"]))
            executable_net_apr = None
            if candidate is not None and candidate.get("realized_7d_apr_pct") is not None:
                round_trip_bps = self.perp_fee_bps * 2 + quote.entry_cost_bps + quote.exit_cost_bps
                annualized_cost_pct = (
                    round_trip_bps / 10_000 * 365 / self.costs.holding_days * 100
                )
                executable_net_apr = (
                    float(candidate["realized_7d_apr_pct"])
                    - annualized_cost_pct
                    - self.costs.annual_borrow_pct
                )
            exit_reason = self._exit_reason(
                position,
                drift,
                executable_net_apr,
                liquidity_streak=liquidity_streak,
            )
            if exit_reason:
                exit_cost = float(position["notional_usd"]) * (
                    self.perp_fee_bps + quote.exit_cost_bps
                ) / 10_000
                self.store.close_paper_position(
                    int(position["id"]),
                    reason=exit_reason,
                    exit_cost_usd=exit_cost,
                    executed_at_ms=quote.captured_at_ms,
                    perp_exit_price=perp_price,
                    hedge_exit_price=(
                        quote.executable_sell_price
                        if position["side"] == "short_perp_long_hedge"
                        else quote.executable_buy_price
                    ),
                    exit_quantity=quantity,
                    exit_hedge_spread_bps=quote.spread_bps,
                    exit_bid_depth_usd=quote.bid_depth_usd,
                    exit_ask_depth_usd=quote.ask_depth_usd,
                )
                closed_position = self.store.paper_position(int(position["id"]))
                if closed_position is not None:
                    send_discord_alert(
                        render_shadow_exit(closed_position),
                        store=self.store,
                        event_type="shadow_position_closed",
                    )
                closed += 1
        return {"updated": updated, "closed": closed}

    def _exit_reason(
        self,
        position: dict[str, object],
        drift_pct: float,
        executable_net_apr_pct: float | None,
        *,
        liquidity_streak: int = 0,
    ) -> str | None:
        if drift_pct > self.max_hedge_drift_pct:
            return "hedge_drift_above_2pct"
        if liquidity_streak >= 3:
            return "liquidity_degraded_for_3_hours"
        if int(time.time() * 1000) - int(position["opened_at_ms"]) >= self.max_holding_days * 86_400_000:
            return "maximum_7d_holding_period"
        if executable_net_apr_pct is not None and executable_net_apr_pct <= 0:
            return "estimated_net_carry_non_positive"
        points = self.store.latest_funding_points(
            str(position["coin"]),
            3,
            start_time_ms=int(position["opened_at_ms"]),
        )
        if self._three_consecutive_hours(points):
            rates = [point.funding_rate for point in points]
            if position["side"] == "short_perp_long_hedge" and all(rate <= 0 for rate in rates):
                return "funding_flipped_for_3_hours"
            if position["side"] == "long_perp_short_hedge" and all(rate >= 0 for rate in rates):
                return "funding_flipped_for_3_hours"
        return None

    @staticmethod
    def _three_consecutive_hours(points: list[FundingPoint]) -> bool:
        if len(points) != 3:
            return False
        timestamps = sorted(point.timestamp_ms for point in points)
        return all(
            45 * 60_000 <= later - earlier <= 75 * 60_000
            for earlier, later in zip(timestamps, timestamps[1:])
        )
