import pytest

from funding_arb_monitor.analytics import (
    negative_hour_share_pct,
    peak_decay_halflife_hours,
    realized_apr_pct,
    rolling_apr_pct,
)


def test_realized_and_rolling_apr() -> None:
    rates = [(hour * 3_600_000, 0.00001) for hour in range(168)]
    assert realized_apr_pct(rates) == pytest.approx(8.76)
    assert rolling_apr_pct(rates, 168) == pytest.approx(8.76)
    assert rolling_apr_pct(rates, 169) is None


def test_rolling_apr_rejects_a_window_with_missing_hours() -> None:
    rates = [(hour * 3_600_000, 0.00001) for hour in range(168)]
    rates[-1] = (168 * 3_600_000, 0.00001)

    assert rolling_apr_pct(rates, 168) is None


def test_negative_share_and_peak_half_life() -> None:
    rates = [
        (0, 0.0001),
        (3_600_000, 0.00008),
        (7_200_000, 0.00005),
        (10_800_000, -0.00001),
    ]
    assert negative_hour_share_pct(rates) == 25
    assert peak_decay_halflife_hours(rates) == 2
