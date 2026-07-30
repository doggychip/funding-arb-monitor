from dataclasses import replace

import pytest

from funding_arb_monitor.cross_perp import (
    CrossPerpConfig,
    HyperliquidPerpMarket,
    evaluate_direction,
    realized_funding_apr,
)
from funding_arb_monitor.cross_perp_venues import (
    ExternalPerpMarket,
    PerpBookQuote,
    PerpFundingEvent,
    PerpInstrument,
)


def test_realized_funding_apr_normalizes_eight_hour_events() -> None:
    interval_ms = 8 * 3_600_000
    events = tuple(
        PerpFundingEvent(index * interval_ms, 0.0001) for index in range(21)
    )

    apr, coverage, observed_interval_ms = realized_funding_apr(events, window_days=7)

    assert apr == pytest.approx(10.95)
    assert coverage == pytest.approx(1.0)
    assert observed_interval_ms == interval_ms


def test_realized_funding_apr_rejects_single_event() -> None:
    assert realized_funding_apr(
        (PerpFundingEvent(1, 0.0001),), window_days=7
    ) == (None, 0.0, None)


NOW_MS = 1_750_000_000_000
INTERVAL_MS = 8 * 3_600_000


def _funding_events(
    apr_pct: float, *, latest_at_ms: int = NOW_MS - 1_000
) -> tuple[PerpFundingEvent, ...]:
    rate = apr_pct / (365 * 3 * 100)
    return tuple(
        PerpFundingEvent(latest_at_ms - (20 - index) * INTERVAL_MS, rate)
        for index in range(21)
    )


def _quote(
    *,
    venue: str,
    executable_buy_price: float | None,
    executable_sell_price: float | None,
    bid_depth_usd: float = 2_000.0,
    ask_depth_usd: float = 2_000.0,
    captured_at_ms: int = NOW_MS - 1_000,
) -> PerpBookQuote:
    return PerpBookQuote(
        venue=venue,
        asset="ZRO",
        symbol="ZROUSDT",
        bid=99.99,
        ask=100.01,
        executable_buy_price=executable_buy_price,
        executable_sell_price=executable_sell_price,
        bid_depth_usd=bid_depth_usd,
        ask_depth_usd=ask_depth_usd,
        fee_bps=5.0 if venue == "binance" else 4.5,
        captured_at_ms=captured_at_ms,
    )


def _markets() -> tuple[HyperliquidPerpMarket, ExternalPerpMarket]:
    hyperliquid = HyperliquidPerpMarket(
        dex="hyperliquid",
        asset="ZRO",
        current_funding_rate=0.0001,
        mark_price=100.0,
        funding_captured_at_ms=NOW_MS - 1_000,
        funding_events=_funding_events(30.0),
        quote=_quote(
            venue="hyperliquid",
            executable_buy_price=100.05,
            executable_sell_price=99.95,
        ),
    )
    external = ExternalPerpMarket(
        instrument=PerpInstrument("binance", "ZRO", "ZROUSDT"),
        current_funding_rate=0.0001,
        mark_price=100.0,
        funding_captured_at_ms=NOW_MS - 1_000,
        funding_events=_funding_events(10.0),
        quote=_quote(
            venue="binance",
            executable_buy_price=100.04,
            executable_sell_price=99.96,
        ),
    )
    return hyperliquid, external


def test_evaluate_direction_calculates_both_executable_carry_routes() -> None:
    hyperliquid, external = _markets()
    config = CrossPerpConfig()

    short_hl = evaluate_direction(
        hyperliquid,
        external,
        "short_hyperliquid_long_external",
        config,
        NOW_MS,
    )
    long_hl = evaluate_direction(
        hyperliquid,
        external,
        "long_hyperliquid_short_external",
        config,
        NOW_MS,
    )

    assert short_hl.gross_spread_apr_pct == pytest.approx(20.0)
    assert long_hl.gross_spread_apr_pct == pytest.approx(-20.0)
    assert short_hl.transaction_cost_usd == pytest.approx(
        1_000 * (2 * (4.5 + 5.0 + 5.0 + 4.0)) / 10_000
    )
    assert short_hl.net_apr_7d_pct < short_hl.gross_spread_apr_pct
    assert long_hl.reasons == ("net_carry_non_positive",)


def test_evaluate_direction_rejects_insufficient_history() -> None:
    hyperliquid, external = _markets()
    hyperliquid = replace(
        hyperliquid, funding_events=(PerpFundingEvent(NOW_MS - 1_000, 0.0001),)
    )

    observation = evaluate_direction(
        hyperliquid,
        external,
        "short_hyperliquid_long_external",
        CrossPerpConfig(),
        NOW_MS,
    )

    assert observation.reasons == ("insufficient_history",)


def test_evaluate_direction_rejects_stale_funding() -> None:
    hyperliquid, external = _markets()
    hyperliquid = replace(
        hyperliquid,
        funding_events=_funding_events(30.0, latest_at_ms=NOW_MS - 2 * INTERVAL_MS - 1),
    )

    observation = evaluate_direction(
        hyperliquid,
        external,
        "short_hyperliquid_long_external",
        CrossPerpConfig(),
        NOW_MS,
    )

    assert observation.reasons == ("stale_funding",)


def test_evaluate_direction_rejects_stale_quote() -> None:
    hyperliquid, external = _markets()
    external = replace(
        external,
        quote=replace(external.quote, captured_at_ms=NOW_MS - 60_001),
    )

    observation = evaluate_direction(
        hyperliquid,
        external,
        "short_hyperliquid_long_external",
        CrossPerpConfig(),
        NOW_MS,
    )

    assert observation.reasons == ("stale_quote",)


def test_evaluate_direction_rejects_missing_required_book_side() -> None:
    hyperliquid, external = _markets()
    external = replace(
        external,
        quote=replace(
            external.quote,
            ask_depth_usd=999.0,
        ),
    )

    observation = evaluate_direction(
        hyperliquid,
        external,
        "short_hyperliquid_long_external",
        CrossPerpConfig(),
        NOW_MS,
    )

    assert observation.reasons == ("insufficient_depth",)


def test_evaluate_direction_rejects_wide_basis() -> None:
    hyperliquid, external = _markets()
    external = replace(
        external,
        mark_price=101.01,
        quote=replace(
            external.quote,
            executable_buy_price=101.050404,
            executable_sell_price=100.969596,
        ),
    )

    observation = evaluate_direction(
        hyperliquid,
        external,
        "short_hyperliquid_long_external",
        CrossPerpConfig(),
        NOW_MS,
    )

    assert observation.basis_bps == pytest.approx(101.0)
    assert observation.reasons == ("basis_too_wide",)


def test_evaluate_direction_keeps_multiple_reasons_in_rule_order() -> None:
    hyperliquid, external = _markets()
    hyperliquid = replace(
        hyperliquid,
        funding_events=_funding_events(
            30.0, latest_at_ms=NOW_MS - 2 * INTERVAL_MS - 1
        ),
        quote=replace(
            hyperliquid.quote,
            executable_sell_price=None,
            bid_depth_usd=999.0,
            captured_at_ms=NOW_MS - 60_001,
        ),
    )
    external = replace(
        external,
        mark_price=101.01,
        funding_events=(PerpFundingEvent(NOW_MS - 1_000, 0.0001),),
        quote=replace(
            external.quote,
            executable_buy_price=101.050404,
            executable_sell_price=100.969596,
        ),
    )

    observation = evaluate_direction(
        hyperliquid,
        external,
        "short_hyperliquid_long_external",
        CrossPerpConfig(),
        NOW_MS,
    )

    assert observation.reasons == (
        "insufficient_history",
        "stale_funding",
        "stale_quote",
        "insufficient_depth",
        "basis_too_wide",
    )
