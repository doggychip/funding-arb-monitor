from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from funding_arb_monitor.api import create_app
from funding_arb_monitor.cross_perp import CrossPerpObservation
from funding_arb_monitor.cross_perp_paper import (
    CrossPerpPreflightEvidence,
    CrossPerpShadowEngine,
    DelayedLegSample,
    evaluate_execution_truth,
)
from funding_arb_monitor.cross_perp_venues import PerpFundingEvent
from funding_arb_monitor.store import Store


def _observation(
    observed_at_ms: int,
    *,
    qualified: bool = True,
    reasons: tuple[str, ...] = (),
) -> CrossPerpObservation:
    return CrossPerpObservation(
        observed_at_ms=observed_at_ms,
        hyperliquid_dex="hyperliquid",
        asset="SAGA",
        external_venue="binance",
        external_symbol="SAGAUSDT",
        direction="short_hyperliquid_long_external",
        hyperliquid_current_funding_rate=0.001,
        external_current_funding_rate=0.0002,
        hyperliquid_funding_apr_pct=250,
        external_funding_apr_pct=30,
        gross_spread_apr_pct=220,
        net_apr_7d_pct=200,
        expected_funding_usd=42,
        transaction_cost_usd=4,
        net_profit_usd=38,
        funding_cost_ratio=10.5,
        stress_net_profit_usd=37,
        basis_bps=10,
        hyperliquid_mark_price=1.0,
        external_mark_price=1.001,
        hyperliquid_mark_at_ms=observed_at_ms,
        external_mark_at_ms=observed_at_ms,
        hyperliquid_executable_price=0.999,
        external_executable_price=1.002,
        hyperliquid_slippage_bps=5,
        external_slippage_bps=5,
        hyperliquid_depth_usd=5_000,
        external_depth_usd=5_000,
        hyperliquid_fee_bps=4.5,
        external_fee_bps=5,
        hyperliquid_history_coverage=1,
        external_history_coverage=1,
        hyperliquid_funding_at_ms=observed_at_ms,
        external_funding_at_ms=observed_at_ms,
        hyperliquid_quote_at_ms=observed_at_ms,
        external_quote_at_ms=observed_at_ms,
        qualified=qualified,
        reasons=reasons,
    )


def _save_run(store: Store, observation: CrossPerpObservation) -> dict[str, object]:
    run_id = store.start_cross_perp_run()
    saved = store.save_cross_perp_observations(
        run_id, [observation], continuity_window_ms=5_400_000
    )
    store.finish_cross_perp_run(
        run_id,
        status="success",
        venue_status={"binance": "success"},
        match_count=1,
        evaluation_count=1,
        positive_net_count=int(observation.qualified),
        ready_count=sum(bool(item["observation_ready"]) for item in saved),
    )
    return saved[0]


def _ready_route(store: Store) -> dict[str, object]:
    _save_run(store, _observation(1_000))
    _save_run(store, _observation(3_601_000))
    return _save_run(store, _observation(7_201_000))


class FakePreflight:
    def __init__(self, evidence: CrossPerpPreflightEvidence) -> None:
        self.evidence = evidence
        self.routes: list[dict[str, object]] = []

    def refresh(self, route):
        self.routes.append(route)
        return self.evidence


def _evidence(
    observed_at_ms: int,
    *,
    qualified: bool = True,
    reasons: tuple[str, ...] | None = None,
) -> CrossPerpPreflightEvidence:
    failure_reasons = reasons or ("net_carry_non_positive",)
    observation = _observation(
        observed_at_ms,
        qualified=qualified,
        reasons=() if qualified else failure_reasons,
    )
    delayed_samples = tuple(
        DelayedLegSample(delay, 0.999, 1.002, 1.0, True)
        for delay in (100, 250, 500)
    )
    truth = evaluate_execution_truth(
        observation,
        hyperliquid_next_funding_at_ms=observed_at_ms + 3_600_000,
        hyperliquid_funding_interval_ms=3_600_000,
        external_next_funding_at_ms=observed_at_ms + 3_600_000,
        external_funding_interval_ms=8 * 3_600_000,
        delayed_leg_samples=delayed_samples,
    )
    return CrossPerpPreflightEvidence(
        observation=observation,
        hyperliquid_exit_price=1.001,
        external_exit_price=1.0,
        hyperliquid_funding_events=(
            PerpFundingEvent(observed_at_ms, 0.001),
        ),
        external_funding_events=(
            PerpFundingEvent(observed_at_ms, 0.0002),
        ),
        execution_truth=truth,
    )


def test_execution_truth_uses_forward_rates_and_requires_three_times_depth() -> None:
    observation = _observation(1_000)
    samples = tuple(
        DelayedLegSample(delay, 0.999, 1.002, 1.0, True)
        for delay in (100, 250, 500)
    )

    truth = evaluate_execution_truth(
        observation,
        hyperliquid_next_funding_at_ms=3_601_000,
        hyperliquid_funding_interval_ms=3_600_000,
        external_next_funding_at_ms=3_601_000,
        external_funding_interval_ms=8 * 3_600_000,
        delayed_leg_samples=samples,
    )

    assert truth.hyperliquid_settlements == 24
    assert truth.external_settlements == 3
    assert truth.forward_funding_usd == pytest.approx(23.4)
    assert truth.paper_executable is True
    assert truth.depth_multiple == 5
    assert truth.return_on_capital_24h_pct is not None

    shallow = evaluate_execution_truth(
        replace(
            observation,
            hyperliquid_depth_usd=2_999,
            external_depth_usd=10_000,
        ),
        hyperliquid_next_funding_at_ms=3_601_000,
        hyperliquid_funding_interval_ms=3_600_000,
        external_next_funding_at_ms=3_601_000,
        external_funding_interval_ms=8 * 3_600_000,
        delayed_leg_samples=samples,
    )
    assert shallow.paper_executable is False
    assert "depth_below_3x_notional" in shallow.reasons


def test_execution_truth_fails_delayed_leg_stress() -> None:
    observation = _observation(1_000)
    truth = evaluate_execution_truth(
        observation,
        hyperliquid_next_funding_at_ms=3_601_000,
        hyperliquid_funding_interval_ms=3_600_000,
        external_next_funding_at_ms=3_601_000,
        external_funding_interval_ms=8 * 3_600_000,
        delayed_leg_samples=(
            DelayedLegSample(100, 0.995, 1.002, 40.0, False),
        ),
    )

    assert truth.paper_executable is False
    assert "delayed_leg_stress_failed" in truth.reasons


def test_shadow_engine_requires_ready_route_and_second_preflight(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    ready = _ready_route(store)
    now_ms = 7_202_000
    preflight = FakePreflight(_evidence(now_ms))

    result = CrossPerpShadowEngine(
        store, preflight, now_ms=lambda: now_ms
    ).run()

    assert ready["observation_ready"] is True
    assert result == {
        "ready_routes": 1,
        "checks_passed": 1,
        "opened": 1,
        "updated": 1,
        "closed": 0,
    }
    assert len(preflight.routes) == 1
    assert store.latest_cross_perp_entry_checks()[0]["status"] == "passed"
    position = store.open_cross_perp_paper_positions()[0]
    assert position["asset"] == "SAGA"
    assert position["forecast_net_profit_usd"] == 38


def test_shadow_engine_fails_closed_when_preflight_is_unavailable(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    _ready_route(store)

    class BrokenPreflight:
        def refresh(self, route):
            raise RuntimeError("public book unavailable")

    result = CrossPerpShadowEngine(
        store, BrokenPreflight(), now_ms=lambda: 7_202_000
    ).run()

    assert result["checks_passed"] == 0
    assert result["opened"] == 0
    assert store.open_cross_perp_paper_positions() == []
    check = store.latest_cross_perp_entry_checks()[0]
    assert check["status"] == "failed"
    assert check["reasons"] == ["preflight_unavailable"]


def test_shadow_engine_closes_and_attributes_forecast_error(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    _ready_route(store)
    opened_at_ms = 7_202_000
    CrossPerpShadowEngine(
        store,
        FakePreflight(_evidence(opened_at_ms)),
        now_ms=lambda: opened_at_ms,
    ).run()

    result = None
    for index in range(3):
        lost_at_ms = 10_801_000 + index * 3_600_000
        _save_run(
            store,
            replace(
                _observation(lost_at_ms),
                qualified=False,
                reasons=("net_carry_non_positive",),
            ),
        )
        result = CrossPerpShadowEngine(
            store,
            FakePreflight(_evidence(lost_at_ms, qualified=False)),
            now_ms=lambda timestamp=lost_at_ms: timestamp,
        ).run()
        assert result["closed"] == int(index == 2)

    assert result is not None
    assert result["closed"] == 1
    position = store.cross_perp_paper_positions()[0]
    assert position["exit_reason"] == "economic_deterioration_3_scans"
    assert position["funding_pnl_usd"] == 2.4
    assert position["actual_net_pnl_usd"] != position["forecast_net_profit_usd"]
    assert position["forecast_error_usd"] == (
        position["actual_net_pnl_usd"] - position["forecast_net_profit_usd"]
    )
    attribution = store.cross_perp_paper_attribution()
    assert attribution["closed_positions"] == 1
    assert attribution["funding_variance_usd"] == 2.4 - 42


def test_shadow_engine_exits_immediately_for_hard_risk(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    _ready_route(store)
    opened_at_ms = 7_202_000
    CrossPerpShadowEngine(
        store,
        FakePreflight(_evidence(opened_at_ms)),
        now_ms=lambda: opened_at_ms,
    ).run()
    stale_at_ms = 10_801_000
    _save_run(
        store,
        replace(
            _observation(stale_at_ms),
            qualified=False,
            reasons=("stale_mark",),
        ),
    )

    result = CrossPerpShadowEngine(
        store,
        FakePreflight(
            _evidence(stale_at_ms, qualified=False, reasons=("stale_mark",))
        ),
        now_ms=lambda: stale_at_ms,
    ).run()

    assert result["closed"] == 1
    assert store.cross_perp_paper_positions()[0]["exit_reason"] == (
        "hard_risk_stale_mark"
    )


def test_shadow_engine_enforces_route_reentry_cooldown(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    _ready_route(store)
    opened_at_ms = 7_202_000
    CrossPerpShadowEngine(
        store,
        FakePreflight(_evidence(opened_at_ms)),
        now_ms=lambda: opened_at_ms,
    ).run()
    position = store.open_cross_perp_paper_positions()[0]
    closed_at_ms = opened_at_ms + 1_000
    store.close_cross_perp_paper_position(
        int(position["id"]),
        closed_at_ms=closed_at_ms,
        reason="test_close",
        hyperliquid_exit_price=1.001,
        external_exit_price=1.0,
        exit_fee_usd=0.95,
    )

    result = CrossPerpShadowEngine(
        store,
        FakePreflight(_evidence(closed_at_ms + 1_000)),
        now_ms=lambda: closed_at_ms + 1_000,
    ).run()

    assert result["opened"] == 0
    check = store.latest_cross_perp_entry_checks()[0]
    assert check["status"] == "failed"
    assert "route_cooldown" in check["reasons"]


def test_cross_perp_transitions_are_emitted_only_on_state_change(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    _ready_route(store)
    ready_run_id = int(store.cross_perp_summary()["id"])

    assert [
        item["event_type"] for item in store.cross_perp_transitions(ready_run_id)
    ] == ["became_ready"]

    _save_run(
        store,
        replace(
            _observation(10_801_000),
            qualified=False,
            reasons=("cost_buffer_too_thin",),
        ),
    )
    lost_run_id = int(store.cross_perp_summary()["id"])
    event_types = {
        item["event_type"] for item in store.cross_perp_transitions(lost_run_id)
    }
    assert event_types == {"lost_ready", "economics_deteriorated"}


def test_cross_perp_paper_api_exposes_checks_positions_and_attribution(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    _ready_route(store)
    CrossPerpShadowEngine(
        store,
        FakePreflight(_evidence(7_202_000)),
        now_ms=lambda: 7_202_000,
    ).run()
    client = TestClient(create_app(str(store.path)))

    assert client.get("/api/cross-perp/preflight").json()[0]["status"] == "passed"
    truth = client.get("/api/cross-perp/execution-truth").json()
    assert truth["paper_executable"] == 1
    assert truth["live_review_eligible"] is False
    positions = client.get("/api/cross-perp/paper/positions").json()
    assert positions[0]["asset"] == "SAGA"
    position_id = positions[0]["id"]
    assert client.get(
        f"/api/cross-perp/paper/positions/{position_id}/timeline"
    ).status_code == 200
    assert client.get("/api/cross-perp/paper/attribution").json()[
        "open_positions"
    ] == 1
    dashboard = client.get("/").text
    assert 'id="cross-perp-paper-heading"' in dashboard
    assert 'id="cross-perp-paper-rows"' in dashboard
    assert 'id="cross-perp-executable"' in dashboard
    assert "loadCrossPerpPaper();" in dashboard
