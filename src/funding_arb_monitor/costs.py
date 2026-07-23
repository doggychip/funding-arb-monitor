from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostAssumptions:
    """Conservative default estimates, editable at paper-position entry."""

    perp_fill_bps: float = 10.0
    hedge_fill_bps: float = 10.0
    annual_borrow_pct: float = 5.0
    holding_days: int = 7

    @property
    def round_trip_cost_pct(self) -> float:
        # Entry + exit for both the perp and hedge leg.
        return (self.perp_fill_bps + self.hedge_fill_bps) * 2 / 10_000

    @property
    def annualized_round_trip_cost_pct(self) -> float:
        return self.round_trip_cost_pct * 365 / self.holding_days * 100

    def estimated_net_annual_apr_pct(self, gross_apr_pct: float) -> float:
        return gross_apr_pct - self.annualized_round_trip_cost_pct - self.annual_borrow_pct


def hedge_assessment(coin: str) -> str:
    if coin.startswith("xyz:"):
        return "reference_market_session_fx_and_hedge_review"
    return "24h_crypto_hedge_venue_and_liquidity_review"
