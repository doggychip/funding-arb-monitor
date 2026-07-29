import pytest
import time

from funding_arb_monitor.models import FundingPoint
from funding_arb_monitor.paper import PaperLedger, PaperOpenRequest
from funding_arb_monitor.store import Store


def test_paper_position_accrues_funding_once(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    ledger = PaperLedger(store)
    position = ledger.open(PaperOpenRequest(coin="BTC", hedge_venue="spot-test"))

    ledger.accrue(
        position["id"],
        [
            FundingPoint("BTC", position["opened_at_ms"], 0.0001),
            FundingPoint("BTC", position["opened_at_ms"] + 3_600_000, 0.0002),
        ],
    )
    ledger.accrue(
        position["id"],
        [FundingPoint("BTC", position["opened_at_ms"], 0.0001)],
    )

    updated = store.paper_position(position["id"])
    assert updated["funding_pnl_usd"] == pytest.approx(0.3)
    assert updated["entry_cost_usd"] == pytest.approx(2.0)
    assert updated["net_pnl_usd"] == pytest.approx(-1.7)


def test_closed_position_remains_in_report_with_financing_cost(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    position = PaperLedger(store).open(
        PaperOpenRequest(coin="BTC", hedge_venue="spot-test")
    )
    one_year_ago_ms = int(time.time() * 1000) - 365 * 86_400_000
    with store.connect() as connection:
        connection.execute(
            "UPDATE paper_positions SET opened_at_ms = ? WHERE id = ?",
            (one_year_ago_ms, position["id"]),
        )
    store.close_paper_position(position["id"], reason="test_exit", exit_cost_usd=2)

    summary = store.paper_summary()

    assert summary["open_positions"] == 0
    assert summary["closed_positions"] == 1
    assert summary["financing_cost_usd"] == pytest.approx(50, abs=0.01)
    assert len(store.paper_positions()) == 1


def test_paper_performance_summarizes_closed_trades(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    position_ids = [
        store.open_paper_position(
            coin=coin,
            hedge_venue="test",
            side="short_perp_long_hedge",
            notional_usd=1_000,
            entry_cost_usd=0,
            hedge_assessment="test",
            notes="test",
        )
        for coin in ("WIN", "LOSS")
    ]
    now_ms = int(time.time() * 1000)
    store.save_paper_accruals(position_ids[0], [(now_ms, 10)])
    store.save_paper_accruals(position_ids[1], [(now_ms, -5)])
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE paper_positions
            SET opened_at_ms = ?, closed_at_ms = ?, exit_reason = 'maximum_7d_holding_period',
                exit_cost_usd = 0
            WHERE id = ?
            """,
            (now_ms - 4 * 3_600_000, now_ms - 3 * 3_600_000, position_ids[0]),
        )
        connection.execute(
            """
            UPDATE paper_positions
            SET opened_at_ms = ?, closed_at_ms = ?, exit_reason = 'funding_flipped_for_3_hours',
                exit_cost_usd = 0
            WHERE id = ?
            """,
            (now_ms - 2 * 3_600_000, now_ms - 3_600_000, position_ids[1]),
        )

    performance = store.paper_performance()

    assert performance["completed_trades"] == 2
    assert performance["winning_trades"] == 1
    assert performance["win_rate_pct"] == 50
    assert performance["realized_net_pnl_usd"] == pytest.approx(5, abs=0.02)
    assert performance["max_drawdown_usd"] == pytest.approx(5, abs=0.02)
    assert performance["average_holding_hours"] == 1
    assert performance["graduation"]["closed_trades"] == 2
    assert performance["graduation"]["required_closed_trades"] == 30
    assert performance["graduation"]["eligible_for_live_review"] is False
    assert performance["exit_reasons"] == {
        "maximum_7d_holding_period": 1,
        "funding_flipped_for_3_hours": 1,
    }


def test_paper_strategy_analytics_breaks_down_closed_trade_evidence(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    now_ms = int(time.time() * 1000)
    recommendations = [
        {
            "created_at_ms": now_ms - 3 * 86_400_000,
            "expires_at_ms": now_ms + 3_600_000,
            "status": "approved",
            "coin": "BTC",
            "candidate_analyzed_at": "2026-07-26T00:00:00+00:00",
            "venue": "binance",
            "hedge_symbol": "BTCUSDC",
            "side": "short_perp_long_hedge",
            "notional_usd": 1_000,
            "quantity": 1,
            "perp_entry_price": 100,
            "perp_bid_depth_usd": 20_000,
            "perp_ask_depth_usd": 20_000,
            "perp_spread_bps": 2,
            "perp_quote_at_ms": now_ms,
            "hedge_entry_price": 100,
            "gross_apr_pct": 40,
            "executable_net_apr_pct": 20,
            "hedge_fee_bps": 10,
            "hedge_spread_bps": 2,
            "bid_depth_usd": 20_000,
            "ask_depth_usd": 20_000,
            "entry_cost_usd": 0,
            "estimated_exit_cost_usd": 0,
        },
        {
            "created_at_ms": now_ms - 2 * 86_400_000,
            "expires_at_ms": now_ms + 3_600_000,
            "status": "approved",
            "coin": "ETH",
            "candidate_analyzed_at": "2026-07-27T00:00:00+00:00",
            "venue": "okx",
            "hedge_symbol": "ETH-USDC",
            "side": "short_perp_long_hedge",
            "notional_usd": 1_000,
            "quantity": 1,
            "perp_entry_price": 100,
            "perp_bid_depth_usd": 20_000,
            "perp_ask_depth_usd": 20_000,
            "perp_spread_bps": 2,
            "perp_quote_at_ms": now_ms,
            "hedge_entry_price": 100,
            "gross_apr_pct": 80,
            "executable_net_apr_pct": 50,
            "hedge_fee_bps": 10,
            "hedge_spread_bps": 2,
            "bid_depth_usd": 20_000,
            "ask_depth_usd": 20_000,
            "entry_cost_usd": 0,
            "estimated_exit_cost_usd": 0,
        },
    ]
    recommendation_ids = [
        store.save_paper_recommendation(item) for item in recommendations
    ]
    position_ids = [
        store.open_paper_position(
            coin=coin,
            hedge_venue=venue,
            side="short_perp_long_hedge",
            notional_usd=1_000,
            entry_cost_usd=0,
            hedge_assessment="test",
            notes="test",
            recommendation_id=recommendation_id,
        )
        for coin, venue, recommendation_id in (
            ("BTC", "binance", recommendation_ids[0]),
            ("ETH", "okx", recommendation_ids[1]),
        )
    ]
    store.save_paper_accruals(position_ids[0], [(now_ms, 10)])
    store.save_paper_accruals(position_ids[1], [(now_ms, -5)])
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE paper_positions
            SET opened_at_ms = ?, closed_at_ms = ?, exit_reason = ?, exit_cost_usd = 0
            WHERE id = ?
            """,
            (now_ms - 12 * 3_600_000, now_ms, "funding_flipped_for_3_hours", position_ids[0]),
        )
        connection.execute(
            """
            UPDATE paper_positions
            SET opened_at_ms = ?, closed_at_ms = ?, exit_reason = ?, exit_cost_usd = 0
            WHERE id = ?
            """,
            (now_ms - 48 * 3_600_000, now_ms, "maximum_7d_holding_period", position_ids[1]),
        )

    analytics = store.paper_strategy_analytics()

    assert analytics["total_closed_trades"] == 2
    assert analytics["by_coin"][0]["name"] == "BTC"
    assert analytics["by_coin"][0]["net_pnl_usd"] == pytest.approx(10, abs=0.1)
    assert {item["name"] for item in analytics["by_venue"]} == {"binance", "okx"}
    assert {item["name"] for item in analytics["by_holding_period"]} == {
        "under_24h",
        "1_to_3d",
    }
    assert {item["name"] for item in analytics["by_entry_net_apr"]} == {
        "10_to_25pct",
        "50pct_plus",
    }
    assert {item["name"] for item in analytics["by_market_regime"]} == {
        "high_carry",
        "extreme_carry",
    }
    assert {item["name"] for item in analytics["by_exit_reason"]} == {
        "funding_flipped_for_3_hours",
        "maximum_7d_holding_period",
    }


def test_paper_position_timeline_combines_funding_and_marks(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    position = PaperLedger(store).open(
        PaperOpenRequest(coin="BTC", hedge_venue="spot-test")
    )
    timestamp_ms = int(position["opened_at_ms"]) + 3_600_000
    store.save_paper_accruals(int(position["id"]), [(timestamp_ms, 1)])
    store.save_paper_mark(
        int(position["id"]),
        timestamp_ms=timestamp_ms,
        perp_price=101,
        hedge_price=100,
        perp_pnl_usd=10,
        hedge_pnl_usd=-5,
        hedge_drift_pct=0.995,
    )

    timeline = store.paper_position_timeline(int(position["id"]))

    assert timeline is not None
    assert timeline["position"]["coin"] == "BTC"
    assert len(timeline["points"]) == 2
    latest = timeline["points"][-1]
    assert latest["funding_pnl_usd"] == 1
    assert latest["pair_mtm_usd"] == 5
    assert latest["basis_pct"] == pytest.approx(100 / 100.5)
    assert latest["net_pnl_usd"] == pytest.approx(4, abs=0.01)
