from __future__ import annotations

import time
from collections.abc import Iterable

from .alerts import render_shadow_entry, send_discord_alert
from .costs import CostAssumptions
from .hyperliquid import HyperliquidClient
from .models import PerpQuote
from .store import Store
from .venues import (
    BinanceSpot,
    CoinbaseSpot,
    HedgeQuote,
    KrakenSpot,
    OkxSpot,
    SpotVenue,
)


class PaperMatcher:
    def __init__(
        self,
        store: Store,
        venues: Iterable[SpotVenue] | None = None,
        perp_client: HyperliquidClient | None = None,
        *,
        notional_usd: float = 1_000,
        min_depth_multiple: float = 5,
        min_net_apr_pct: float = 10,
        max_open_positions: int = 3,
        max_snapshot_age_seconds: int = 15 * 60,
    ) -> None:
        self.store = store
        self.venues = list(
            venues or [OkxSpot(), BinanceSpot(), CoinbaseSpot(), KrakenSpot()]
        )
        self.perp_client = perp_client or HyperliquidClient()
        self.notional_usd = notional_usd
        self.min_depth_multiple = min_depth_multiple
        self.min_net_apr_pct = min_net_apr_pct
        self.max_open_positions = max_open_positions
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.costs = CostAssumptions()
        self.perp_fee_bps = self.costs.perp_fill_bps

    def recommend(self) -> list[dict[str, object]]:
        created: list[dict[str, object]] = []
        for candidate in self.store.latest_scan_candidates():
            if not candidate.get("eligible") or candidate.get("side") != "short_perp_long_hedge":
                continue
            coin = str(candidate["coin"])
            analyzed_at = str(candidate["analyzed_at"])
            if self.store.has_open_paper_position(coin):
                self._record_check(analyzed_at, coin, "already_open", "paper position already open")
                continue
            quote, match_status = self._best_quote(self._asset(coin))
            if quote is None:
                self._record_check(analyzed_at, coin, match_status, match_status.replace("_", " "))
                continue
            gross_apr = float(candidate["realized_7d_apr_pct"])
            net_apr = self._net_apr(gross_apr, quote)
            if net_apr < self.min_net_apr_pct:
                self._record_check(
                    analyzed_at,
                    coin,
                    "net_carry_below_threshold",
                    f"executable net APR {net_apr:.1f}% < {self.min_net_apr_pct:.1f}%",
                    quote=quote,
                    gross_apr=gross_apr,
                )
                continue
            candidate_dex = str(candidate["dex"])
            try:
                perp_quote = self.perp_client.perp_quote(
                    coin, candidate_dex, self.notional_usd
                )
            except RuntimeError:
                perp_quote = None
            if perp_quote is None:
                self._record_check(
                    analyzed_at,
                    coin,
                    "insufficient_perp_depth",
                    "no executable two-sided perp quote",
                    quote=quote,
                    gross_apr=gross_apr,
                )
                continue
            if (
                int(time.time() * 1000) - perp_quote.captured_at_ms
                > self.max_snapshot_age_seconds * 1000
            ):
                self._record_check(
                    analyzed_at,
                    coin,
                    "stale_perp_quote",
                    f"perp quote is older than {self.max_snapshot_age_seconds // 60} minutes",
                    quote=quote,
                    gross_apr=gross_apr,
                )
                continue
            now_ms = int(time.time() * 1000)
            recommendation = {
                "created_at_ms": now_ms,
                "expires_at_ms": now_ms + 3_600_000,
                "status": "pending",
                "coin": candidate["coin"],
                "candidate_analyzed_at": candidate["analyzed_at"],
                "side": candidate["side"],
                **self._execution(gross_apr, quote, perp_quote),
            }
            recommendation_id = self.store.save_paper_recommendation(recommendation)
            if recommendation_id is not None:
                recommendation["id"] = recommendation_id
                created.append(recommendation)
            self._record_check(
                analyzed_at,
                coin,
                "pending_approval",
                f"{quote.venue} {quote.symbol}; executable net APR {net_apr:.1f}%",
                quote=quote,
                gross_apr=gross_apr,
            )
        return created

    def shadow(self) -> dict[str, object]:
        opened: list[dict[str, object]] = []
        rejected: list[dict[str, object]] = []
        recommendations = self.recommend()
        for recommendation in recommendations:
            recommendation_id = int(recommendation["id"])
            try:
                position = self.approve(recommendation_id, approval_mode="shadow_auto")
                opened.append(position)
                send_discord_alert(
                    render_shadow_entry(
                        position, float(recommendation["executable_net_apr_pct"])
                    ),
                    store=self.store,
                    event_type="shadow_position_opened",
                )
            except ValueError as exc:
                rejected.append({"id": recommendation_id, "reason": str(exc)})
        return {
            "recommendations": len(recommendations),
            "opened": opened,
            "rejected": rejected,
        }

    def approve(
        self, recommendation_id: int, *, approval_mode: str = "manual"
    ) -> dict[str, object]:
        recommendation = self.store.paper_recommendation(recommendation_id)
        if recommendation is None:
            raise ValueError("recommendation not found")
        if recommendation["status"] != "pending":
            raise ValueError(f"recommendation is {recommendation['status']}")
        if int(recommendation["expires_at_ms"]) < int(time.time() * 1000):
            self.store.set_recommendation_status(recommendation_id, "expired")
            raise ValueError("recommendation expired; generate a fresh quote")
        coin = str(recommendation["coin"])
        candidate = self.store.latest_candidate(coin)
        if (
            candidate is None
            or not candidate.get("eligible")
            or candidate.get("side") != "short_perp_long_hedge"
        ):
            raise ValueError("candidate no longer passes monitoring gates")
        venue = next(
            (item for item in self.venues if item.name == recommendation["venue"]),
            None,
        )
        if venue is None:
            raise ValueError("recommended spot venue is unavailable")
        try:
            quote = venue.quote(self._asset(coin), self.notional_usd)
        except RuntimeError as exc:
            raise ValueError(f"spot requote failed: {exc}") from exc
        if quote is None or quote.symbol != recommendation["hedge_symbol"]:
            raise ValueError("exact spot market is no longer available")
        required_depth = self.notional_usd * self.min_depth_multiple
        if quote.bid_depth_usd < required_depth or quote.ask_depth_usd < required_depth:
            raise ValueError("spot depth is now below 5x notional")
        gross_apr = float(candidate["realized_7d_apr_pct"])
        net_apr = self._net_apr(gross_apr, quote)
        if net_apr < self.min_net_apr_pct:
            raise ValueError(
                f"executable net APR is now {net_apr:.1f}% < {self.min_net_apr_pct:.1f}%"
            )
        try:
            perp_quote = self.perp_client.perp_quote(
                coin, str(candidate["dex"]), self.notional_usd
            )
        except RuntimeError as exc:
            raise ValueError(f"perp requote failed: {exc}") from exc
        if perp_quote is None:
            raise ValueError("perp depth is insufficient for the requested notional")
        if (
            int(time.time() * 1000) - perp_quote.captured_at_ms
            > self.max_snapshot_age_seconds * 1000
        ):
            raise ValueError("perp quote is stale")
        execution = self._execution(gross_apr, quote, perp_quote)
        position_id = self.store.approve_paper_recommendation(
            recommendation_id,
            max_open_positions=self.max_open_positions,
            execution=execution,
            approval_mode=approval_mode,
        )
        position = self.store.paper_position(position_id)
        if position is None:
            raise RuntimeError("approved paper position was not persisted")
        return position

    def _net_apr(
        self, gross_apr: float, quote: HedgeQuote, holding_days: int | None = None
    ) -> float:
        total_round_trip_bps = (
            self.perp_fee_bps * 2 + quote.entry_cost_bps + quote.exit_cost_bps
        )
        annualized_cost_pct = (
            total_round_trip_bps
            / 10_000
            * 365
            / (holding_days or self.costs.holding_days)
            * 100
        )
        return gross_apr - annualized_cost_pct - self.costs.annual_borrow_pct

    def _execution(
        self, gross_apr: float, quote: HedgeQuote, perp_quote: PerpQuote
    ) -> dict[str, object]:
        perp_price = perp_quote.executable_sell_price
        return {
            "venue": quote.venue,
            "hedge_symbol": quote.symbol,
            "notional_usd": self.notional_usd,
            "quantity": self.notional_usd / perp_price,
            "perp_entry_price": perp_price,
            "perp_bid_depth_usd": perp_quote.bid_depth_usd,
            "perp_ask_depth_usd": perp_quote.ask_depth_usd,
            "perp_spread_bps": perp_quote.spread_bps,
            "perp_quote_at_ms": perp_quote.captured_at_ms,
            "hedge_entry_price": quote.executable_buy_price,
            "gross_apr_pct": gross_apr,
            "executable_net_apr_pct": self._net_apr(gross_apr, quote),
            "hedge_fee_bps": quote.fee_bps,
            "hedge_spread_bps": quote.spread_bps,
            "bid_depth_usd": quote.bid_depth_usd,
            "ask_depth_usd": quote.ask_depth_usd,
            "entry_cost_usd": self.notional_usd
            * (self.perp_fee_bps + quote.entry_cost_bps)
            / 10_000,
            "estimated_exit_cost_usd": self.notional_usd
            * (self.perp_fee_bps + quote.exit_cost_bps)
            / 10_000,
        }

    def _best_quote(self, asset: str) -> tuple[HedgeQuote | None, str]:
        quotes: list[HedgeQuote] = []
        required_depth = self.notional_usd * self.min_depth_multiple
        quote_found = False
        venue_errors = 0
        for venue in self.venues:
            try:
                quote = venue.quote(asset, self.notional_usd)
            except RuntimeError:
                venue_errors += 1
                continue
            if quote is not None:
                quote_found = True
                if (
                    quote.bid_depth_usd >= required_depth
                    and quote.ask_depth_usd >= required_depth
                ):
                    quotes.append(quote)
        if quotes:
            return (
                min(quotes, key=lambda quote: quote.entry_cost_bps + quote.exit_cost_bps),
                "matched",
            )
        if quote_found:
            return None, "spot_depth_below_5x_notional"
        if venue_errors == len(self.venues):
            return None, "all_spot_venues_unavailable"
        return None, "no_exact_spot_market"

    def _record_check(
        self,
        analyzed_at: str,
        coin: str,
        status: str,
        detail: str,
        *,
        quote: HedgeQuote | None = None,
        gross_apr: float | None = None,
    ) -> None:
        self.store.save_paper_match_check(
            candidate_analyzed_at=analyzed_at,
            coin=coin,
            status=status,
            detail=detail,
            hedge_venue=quote.venue if quote else None,
            hedge_symbol=quote.symbol if quote else None,
            net_apr_7d_pct=(
                self._net_apr(gross_apr, quote, 7)
                if quote is not None and gross_apr is not None
                else None
            ),
            net_apr_14d_pct=(
                self._net_apr(gross_apr, quote, 14)
                if quote is not None and gross_apr is not None
                else None
            ),
            net_apr_30d_pct=(
                self._net_apr(gross_apr, quote, 30)
                if quote is not None and gross_apr is not None
                else None
            ),
        )

    @staticmethod
    def _asset(coin: str) -> str:
        return coin.split(":", 1)[-1]
