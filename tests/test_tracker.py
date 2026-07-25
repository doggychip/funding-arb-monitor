import time
from datetime import datetime, timedelta, timezone

from funding_arb_monitor.models import FundingPoint, MarketSnapshot
from funding_arb_monitor.store import Store
from funding_arb_monitor.tracker import PaperPositionTracker
from funding_arb_monitor.venues import HedgeQuote


class FakeVenue:
    def quote(self, asset: str, notional_usd: float) -> HedgeQuote:
        return HedgeQuote(
            venue="coinbase",
            symbol="TEST-USD",
            asset=asset,
            bid=99.9,
            ask=100.1,
            executable_buy_price=100.1,
            executable_sell_price=99.9,
            bid_depth_usd=20_000,
            ask_depth_usd=20_000,
            fee_bps=10,
            captured_at_ms=int(time.time() * 1000),
        )


def test_tracker_closes_after_three_funding_flips(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    alerts = []
    monkeypatch.setattr(
        "funding_arb_monitor.tracker.send_discord_alert",
        lambda message: alerts.append(message),
    )
    now = datetime.now(timezone.utc)
    store.save_snapshots(
        [MarketSnapshot("(main)", "TEST", -0.0001, 2_000_000, 1_000_000, 100, now)]
    )
    position_id = store.open_paper_position(
        coin="TEST",
        hedge_venue="coinbase",
        side="short_perp_long_hedge",
        notional_usd=1_000,
        entry_cost_usd=2,
        hedge_assessment="exact_spot_market_matched_from_public_order_book",
        notes="test",
        perp_entry_price=100,
        hedge_entry_price=100.1,
        quantity=10,
        hedge_symbol="TEST-USD",
        hedge_fee_bps=10,
        hedge_spread_bps=20,
        estimated_exit_cost_usd=2,
    )
    timestamp = int(time.time() * 1000)
    store.save_funding(
        [FundingPoint("TEST", timestamp + index * 3_600_000, -0.0001) for index in range(3)]
    )
    tracker = PaperPositionTracker(store)
    tracker.venues = {"coinbase": FakeVenue()}  # type: ignore[dict-item]

    result = tracker.update()

    assert result == {"updated": 1, "closed": 1}
    position = store.paper_position(position_id)
    assert position is not None
    assert position["exit_reason"] == "funding_flipped_for_3_hours"
    assert "Shadow paper position closed" in alerts[0]


def test_tracker_ignores_funding_flips_before_position_opened(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    now = datetime.now(timezone.utc)
    store.save_snapshots(
        [MarketSnapshot("(main)", "TEST", -0.0001, 2_000_000, 1_000_000, 100, now)]
    )
    position_id = store.open_paper_position(
        coin="TEST",
        hedge_venue="coinbase",
        side="short_perp_long_hedge",
        notional_usd=1_000,
        entry_cost_usd=2,
        hedge_assessment="exact_spot_market_matched_from_public_order_book",
        notes="test",
        perp_entry_price=100,
        hedge_entry_price=100.1,
        quantity=10,
        hedge_symbol="TEST-USD",
        hedge_fee_bps=10,
        hedge_spread_bps=20,
        estimated_exit_cost_usd=2,
    )
    opened_at_ms = int(store.paper_position(position_id)["opened_at_ms"])
    store.save_funding(
        [
            FundingPoint("TEST", opened_at_ms - index * 3_600_000 - 1, -0.0001)
            for index in range(1, 4)
        ]
    )
    tracker = PaperPositionTracker(store)
    tracker.venues = {"coinbase": FakeVenue()}  # type: ignore[dict-item]

    assert tracker.update() == {"updated": 1, "closed": 0}
    assert store.paper_position(position_id)["closed_at_ms"] is None


def test_tracker_skips_stale_perp_snapshot(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    store.save_snapshots(
        [
            MarketSnapshot(
                "(main)",
                "TEST",
                0.0001,
                2_000_000,
                1_000_000,
                100,
                datetime.now(timezone.utc) - timedelta(hours=1),
            )
        ]
    )
    store.open_paper_position(
        coin="TEST",
        hedge_venue="coinbase",
        side="short_perp_long_hedge",
        notional_usd=1_000,
        entry_cost_usd=2,
        hedge_assessment="exact_spot_market_matched_from_public_order_book",
        notes="test",
        perp_entry_price=100,
        hedge_entry_price=100.1,
        quantity=10,
        hedge_symbol="TEST-USD",
        hedge_fee_bps=10,
        hedge_spread_bps=20,
        estimated_exit_cost_usd=2,
    )
    tracker = PaperPositionTracker(store)
    tracker.venues = {"coinbase": FakeVenue()}  # type: ignore[dict-item]

    assert tracker.update() == {"updated": 0, "closed": 0}
