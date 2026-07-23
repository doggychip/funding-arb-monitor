from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .costs import CostAssumptions, hedge_assessment
from .models import FundingPoint
from .store import Store


@dataclass(frozen=True)
class PaperOpenRequest:
    coin: str
    hedge_venue: str
    notional_usd: float = 1_000
    side: str = "short_perp_long_hedge"
    notes: str = ""


class PaperLedger:
    """Paper only: positions are accounting records, never exchange orders."""

    def __init__(self, store: Store, costs: CostAssumptions | None = None) -> None:
        self.store = store
        self.costs = costs or CostAssumptions()

    def open(self, request: PaperOpenRequest) -> dict[str, object]:
        if request.notional_usd <= 0:
            raise ValueError("notional must be positive")
        if request.side not in {"short_perp_long_hedge", "long_perp_short_hedge"}:
            raise ValueError("unsupported paper side")
        position_id = self.store.open_paper_position(
            coin=request.coin,
            hedge_venue=request.hedge_venue,
            side=request.side,
            notional_usd=request.notional_usd,
            entry_cost_usd=request.notional_usd * self.costs.round_trip_cost_pct / 2,
            hedge_assessment=hedge_assessment(request.coin),
            notes=request.notes,
        )
        return self.store.paper_position(position_id)

    def accrue(self, position_id: int, funding: list[FundingPoint]) -> float:
        position = self.store.paper_position(position_id)
        if position is None:
            raise ValueError(f"paper position {position_id} not found")
        sign = 1 if position["side"] == "short_perp_long_hedge" else -1
        funding_pnl = [
            (point.timestamp_ms, position["notional_usd"] * point.funding_rate * sign)
            for point in funding
            if point.timestamp_ms >= position["opened_at_ms"]
        ]
        self.store.save_paper_accruals(position_id, funding_pnl)
        return sum(amount for _, amount in funding_pnl)

    def accrue_open_positions(self, funding_by_coin: dict[str, list[FundingPoint]]) -> int:
        count = 0
        for position in self.store.open_paper_positions():
            self.accrue(position["id"], funding_by_coin.get(position["coin"], []))
            count += 1
        return count
