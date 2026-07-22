from __future__ import annotations

from collections.abc import Sequence

from .models import HOURS_PER_YEAR

RateSeries = Sequence[tuple[int, float]]


def realized_apr_pct(rates: RateSeries) -> float | None:
    if len(rates) < 2:
        return None
    return sum(rate for _, rate in rates) * HOURS_PER_YEAR / len(rates) * 100


def rolling_apr_pct(rates: RateSeries, hours: int) -> float | None:
    if len(rates) < hours:
        return None
    return sum(rate for _, rate in rates[-hours:]) * HOURS_PER_YEAR / hours * 100


def negative_hour_share_pct(rates: RateSeries) -> float | None:
    if not rates:
        return None
    return sum(rate < 0 for _, rate in rates) * 100 / len(rates)


def peak_decay_halflife_hours(rates: RateSeries) -> int | None:
    """Hours from the highest positive rate until it first drops to half that rate."""
    if not rates:
        return None
    peak_index = max(range(len(rates)), key=lambda index: rates[index][1])
    peak = rates[peak_index][1]
    if peak <= 0:
        return None
    for index in range(peak_index + 1, len(rates)):
        if rates[index][1] <= peak / 2:
            return index - peak_index
    return None
