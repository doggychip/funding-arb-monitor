import sys
from dataclasses import replace
from datetime import datetime, timezone

import pytest

import funding_arb_monitor.cli as cli
from funding_arb_monitor.cross_perp import (
    CrossPerpConfig,
    CrossPerpMonitor,
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
from funding_arb_monitor.models import MarketSnapshot, PerpQuote
from funding_arb_monitor.store import Store


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


def qualifying_observation(*, observed_at_ms: int):
    hyperliquid, external = _markets()
    return replace(
        evaluate_direction(
            hyperliquid,
            external,
            "short_hyperliquid_long_external",
            CrossPerpConfig(),
            NOW_MS,
        ),
        observed_at_ms=observed_at_ms,
    )


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


def test_cross_perp_observations_round_trip(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    run_id = store.start_cross_perp_run()
    saved = store.save_cross_perp_observations(
        run_id,
        [qualifying_observation(observed_at_ms=1_000)],
        continuity_window_ms=5_400_000,
    )
    store.finish_cross_perp_run(
        run_id,
        status="success",
        venue_status={"binance": "success", "okx": "success"},
        match_count=1,
        evaluation_count=1,
        positive_net_count=1,
        ready_count=0,
    )

    assert saved[0]["streak"] == 1
    assert saved[0]["observation_ready"] is False
    assert store.latest_cross_perp_observations()[0]["asset"] == "ZRO"


def _save_cross_perp_run(
    store: Store,
    observations: list,
    *,
    status: str = "success",
    venue_status: dict[str, str] | None = None,
):
    run_id = store.start_cross_perp_run()
    saved = store.save_cross_perp_observations(
        run_id, observations, continuity_window_ms=5_400_000
    )
    store.finish_cross_perp_run(
        run_id,
        status=status,
        venue_status=venue_status or {"binance": status},
        match_count=len(observations),
        evaluation_count=len(observations),
        positive_net_count=sum(item.qualified for item in observations),
        ready_count=sum(item["observation_ready"] for item in saved),
    )
    return saved


def test_cross_perp_qualification_requires_three_consecutive_successful_runs(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()

    saved = [
        _save_cross_perp_run(
            store, [qualifying_observation(observed_at_ms=1_000)]
        )[0],
        _save_cross_perp_run(
            store, [qualifying_observation(observed_at_ms=3_601_000)]
        )[0],
        _save_cross_perp_run(
            store, [qualifying_observation(observed_at_ms=7_201_000)]
        )[0],
    ]

    assert [item["streak"] for item in saved] == [1, 2, 3]
    assert [item["observation_ready"] for item in saved] == [False, False, True]
    assert store.cross_perp_history(
        "ZRO", "binance", "short_hyperliquid_long_external"
    )[0]["streak"] == 3


@pytest.mark.parametrize(
    ("previous", "next"),
    [
        (
            replace(
                qualifying_observation(observed_at_ms=1_000),
                qualified=False,
                reasons=("net_carry_non_positive",),
            ),
            qualifying_observation(observed_at_ms=3_601_000),
        ),
        (
            qualifying_observation(observed_at_ms=1_000),
            qualifying_observation(observed_at_ms=5_401_001),
        ),
        (
            replace(qualifying_observation(observed_at_ms=1_000), direction="long_hyperliquid_short_external"),
            qualifying_observation(observed_at_ms=3_601_000),
        ),
        (
            replace(qualifying_observation(observed_at_ms=1_000), external_venue="okx"),
            qualifying_observation(observed_at_ms=3_601_000),
        ),
    ],
    ids=["non_qualifying", "time_gap", "direction_change", "venue_change"],
)
def test_cross_perp_qualification_resets_when_previous_route_cannot_continue(
    tmp_path, previous, next
) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    _save_cross_perp_run(store, [previous])

    saved = _save_cross_perp_run(store, [next])

    assert saved[0]["streak"] == 1
    assert saved[0]["observation_ready"] is False


def test_cross_perp_qualification_resets_when_previous_successful_run_omits_route(
    tmp_path,
) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    _save_cross_perp_run(store, [qualifying_observation(observed_at_ms=1_000)])
    _save_cross_perp_run(store, [])

    saved = _save_cross_perp_run(
        store, [qualifying_observation(observed_at_ms=7_201_000)]
    )

    assert saved[0]["streak"] == 1


def test_cross_perp_qualification_resets_after_a_failed_run(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    _save_cross_perp_run(store, [qualifying_observation(observed_at_ms=1_000)])
    _save_cross_perp_run(store, [], status="failed")

    saved = _save_cross_perp_run(
        store, [qualifying_observation(observed_at_ms=7_201_000)]
    )

    assert saved[0]["streak"] == 1


def test_latest_cross_perp_observations_rank_ready_routes_and_hide_stale_runs(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    first = qualifying_observation(observed_at_ms=1_000)
    second = replace(
        qualifying_observation(observed_at_ms=1_000),
        external_symbol="ZROUSDC",
        net_apr_7d_pct=40.0,
    )
    third = replace(
        qualifying_observation(observed_at_ms=1_000),
        asset="ETH",
        external_symbol="ETHUSDT",
        net_apr_7d_pct=None,
    )
    non_ready = replace(
        qualifying_observation(observed_at_ms=7_201_000),
        asset="APT",
        external_symbol="APTUSDT",
        net_apr_7d_pct=30.0,
    )
    _save_cross_perp_run(store, [first, second, third])
    _save_cross_perp_run(
        store,
        [
            qualifying_observation(observed_at_ms=3_601_000),
            replace(second, observed_at_ms=3_601_000),
            replace(third, observed_at_ms=3_601_000),
        ],
    )
    _save_cross_perp_run(
        store,
        [
            qualifying_observation(observed_at_ms=7_201_000),
            replace(second, observed_at_ms=7_201_000),
            replace(third, observed_at_ms=7_201_000),
            non_ready,
        ],
    )

    latest = store.latest_cross_perp_observations()
    assert [item["net_apr_7d_pct"] for item in latest] == [
        40.0,
        30.0,
        qualifying_observation(observed_at_ms=1_000).net_apr_7d_pct,
        None,
    ]
    assert [item["external_symbol"] for item in store.latest_cross_perp_observations(
        observation_ready_only=True
    )] == ["ZROUSDC", "ZROUSDT", "ETHUSDT"]
    assert all(item["observation_age_seconds"] >= 0 for item in latest)

    _save_cross_perp_run(store, [], status="failed")
    assert store.latest_cross_perp_observations() == []


def test_cross_perp_summary_decodes_venue_status_and_rejections(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    _save_cross_perp_run(
        store,
        [
            replace(
                qualifying_observation(observed_at_ms=1_000),
                qualified=False,
                reasons=("stale_quote", "insufficient_depth"),
            )
        ],
        venue_status={"binance": "success", "okx": "failed"},
    )

    summary = store.cross_perp_summary()

    assert summary["status"] == "success"
    assert summary["venue_status"] == {"binance": "success", "okx": "failed"}
    assert summary["rejection_counts"] == {"insufficient_depth": 1, "stale_quote": 1}


class FakeHyperliquid:
    def __init__(self, assets: tuple[str, ...] = ("ZRO",)) -> None:
        self.assets = assets
        self.history_calls: list[str] = []
        self.quote_calls: list[tuple[str, str]] = []

    def snapshots(self) -> list[MarketSnapshot]:
        return [
            MarketSnapshot(
                dex="(main)",
                coin=asset,
                funding_rate=0.0001,
                open_interest_usd=1_000_000.0,
                day_volume_usd=1_000_000.0,
                mark_price=100.0,
                captured_at=datetime.fromtimestamp(NOW_MS / 1_000, tz=timezone.utc),
            )
            for asset in self.assets
        ]

    def funding_history(self, coin: str, days: int):
        assert days == 7
        self.history_calls.append(coin)
        return _funding_events(30.0)

    def perp_quote(self, coin: str, dex: str, notional_usd: float) -> PerpQuote:
        assert notional_usd == 1_000.0
        self.quote_calls.append((coin, dex))
        return PerpQuote(
            coin=coin,
            dex=dex,
            bid=99.99,
            ask=100.01,
            executable_sell_price=99.95,
            executable_buy_price=100.05,
            bid_depth_usd=2_000.0,
            ask_depth_usd=2_000.0,
            captured_at_ms=NOW_MS - 1_000,
        )


class FakeExternalVenue:
    def __init__(
        self,
        name: str,
        assets: tuple[str, ...],
        *,
        catalogue_error: str | None = None,
        market_errors: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.assets = assets
        self.catalogue_error = catalogue_error
        self.market_errors = market_errors

    def instruments(self) -> dict[str, PerpInstrument]:
        if self.catalogue_error:
            raise RuntimeError(self.catalogue_error)
        return {
            asset: PerpInstrument(self.name, asset, f"{asset}USDT")
            for asset in self.assets
        }

    def market(
        self, instrument: PerpInstrument, *, days: int, notional_usd: float
    ) -> ExternalPerpMarket:
        assert days == 7
        assert notional_usd == 1_000.0
        if instrument.asset in self.market_errors:
            raise RuntimeError(f"{instrument.asset} unavailable")
        return ExternalPerpMarket(
            instrument=instrument,
            current_funding_rate=0.0001,
            mark_price=100.0,
            funding_captured_at_ms=NOW_MS - 1_000,
            funding_events=_funding_events(10.0),
            quote=_quote(
                venue=self.name,
                executable_buy_price=100.04,
                executable_sell_price=99.96,
            ),
        )


def test_monitor_persists_successful_venue_when_another_catalogue_fails(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    hyperliquid = FakeHyperliquid()

    result = CrossPerpMonitor(
        hyperliquid=hyperliquid,
        venues=[
            FakeExternalVenue("binance", ("ZRO",)),
            FakeExternalVenue("okx", ("ZRO",), catalogue_error="unavailable"),
        ],
        store=store,
        now_ms=lambda: NOW_MS,
    ).run()

    assert result["status"] == "success"
    assert result["venue_status"] == {
        "binance": "success",
        "okx": "failed: unavailable",
    }
    assert result["match_count"] == 1
    assert result["evaluation_count"] == 2
    assert {
        row["direction"] for row in store.latest_cross_perp_observations()
    } == {
        "short_hyperliquid_long_external",
        "long_hyperliquid_short_external",
    }
    assert hyperliquid.history_calls == ["ZRO"]
    assert hyperliquid.quote_calls == [("ZRO", "(main)")]


def test_monitor_fails_when_all_external_catalogues_fail_without_copying_stale_rows(
    tmp_path,
) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    _save_cross_perp_run(store, [qualifying_observation(observed_at_ms=NOW_MS - 1)])

    with pytest.raises(RuntimeError, match="no external perpetual catalogues"):
        CrossPerpMonitor(
            hyperliquid=FakeHyperliquid(),
            venues=[
                FakeExternalVenue("binance", (), catalogue_error="down"),
                FakeExternalVenue("okx", (), catalogue_error="down"),
            ],
            store=store,
            now_ms=lambda: NOW_MS,
        ).run()

    assert store.cross_perp_summary()["status"] == "failed"
    assert store.latest_cross_perp_observations() == []


def test_monitor_fails_when_hyperliquid_funding_history_is_unavailable(tmp_path) -> None:
    class HistoryFailureHyperliquid(FakeHyperliquid):
        def funding_history(self, coin: str, days: int):
            raise RuntimeError("funding history unavailable")

    store = Store(tmp_path / "test.db")
    store.initialize()
    _save_cross_perp_run(store, [qualifying_observation(observed_at_ms=NOW_MS - 1)])

    with pytest.raises(RuntimeError, match="Hyperliquid market data unavailable"):
        CrossPerpMonitor(
            hyperliquid=HistoryFailureHyperliquid(),
            venues=[FakeExternalVenue("binance", ("ZRO",))],
            store=store,
            now_ms=lambda: NOW_MS,
        ).run()

    assert store.cross_perp_summary()["status"] == "failed"
    assert store.latest_cross_perp_observations() == []


def test_monitor_fails_when_hyperliquid_order_book_is_unavailable(tmp_path) -> None:
    class BookFailureHyperliquid(FakeHyperliquid):
        def perp_quote(
            self, coin: str, dex: str, notional_usd: float
        ) -> PerpQuote:
            raise RuntimeError("order book unavailable")

    store = Store(tmp_path / "test.db")
    store.initialize()
    _save_cross_perp_run(store, [qualifying_observation(observed_at_ms=NOW_MS - 1)])

    with pytest.raises(RuntimeError, match="Hyperliquid market data unavailable"):
        CrossPerpMonitor(
            hyperliquid=BookFailureHyperliquid(),
            venues=[FakeExternalVenue("binance", ("ZRO",))],
            store=store,
            now_ms=lambda: NOW_MS,
        ).run()

    assert store.cross_perp_summary()["status"] == "failed"
    assert store.latest_cross_perp_observations() == []


def test_monitor_persists_market_failures_and_continues_other_assets(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()

    result = CrossPerpMonitor(
        hyperliquid=FakeHyperliquid(("ZRO", "APT")),
        venues=[FakeExternalVenue("binance", ("ZRO", "APT"), market_errors=("ZRO",))],
        store=store,
        now_ms=lambda: NOW_MS,
    ).run()

    rows = store.latest_cross_perp_observations()
    unavailable = [row for row in rows if row["asset"] == "ZRO"]
    assert result["evaluation_count"] == 4
    assert len(unavailable) == 2
    assert {row["direction"] for row in unavailable} == {
        "short_hyperliquid_long_external",
        "long_hyperliquid_short_external",
    }
    assert {tuple(row["reasons"]) for row in unavailable} == {("venue_unavailable",)}
    assert {row["asset"] for row in rows} == {"ZRO", "APT"}


def test_cli_parser_accepts_cross_perp() -> None:
    args = cli.parser().parse_args(["--db", "test.db", "cross-perp"])

    assert args.command == "cross-perp"


def test_cli_cross_perp_prints_summary(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli.CrossPerpMonitor,
        "run",
        lambda _: {"status": "success", "match_count": 1},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["funding-arb-monitor", "--db", str(tmp_path / "test.db"), "cross-perp"],
    )

    cli.main()

    assert '"status": "success"' in capsys.readouterr().out
