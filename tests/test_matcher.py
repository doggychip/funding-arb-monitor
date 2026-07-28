import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from funding_arb_monitor.matcher import PaperMatcher
from funding_arb_monitor.models import Candidate, MarketSnapshot
from funding_arb_monitor.store import Store
from funding_arb_monitor.venues import HedgeQuote


class FakeVenue:
    name = "coinbase"

    def __init__(self) -> None:
        self.depth_usd = 20_000

    def quote(self, asset: str, notional_usd: float) -> HedgeQuote | None:
        if asset != "TEST":
            return None
        return HedgeQuote(
            venue="coinbase",
            symbol="TEST-USD",
            asset=asset,
            bid=99.9,
            ask=100.1,
            executable_buy_price=100.1,
            executable_sell_price=99.9,
            bid_depth_usd=self.depth_usd,
            ask_depth_usd=self.depth_usd,
            fee_bps=10,
            captured_at_ms=int(time.time() * 1000),
        )


class FakePerpClient:
    def __init__(self, mark_price: float = 105) -> None:
        self.mark_price = mark_price

    def market_snapshot(self, coin: str, dex: str) -> MarketSnapshot:
        return MarketSnapshot(
            dex,
            coin,
            0.0001,
            2_000_000,
            1_000_000,
            self.mark_price,
            datetime.now(timezone.utc),
        )


def seed_candidate(store: Store) -> None:
    now = datetime.now(timezone.utc)
    store.save_snapshots(
        [MarketSnapshot("(main)", "TEST", 0.0001, 2_000_000, 1_000_000, 100, now)]
    )
    store.save_candidates(
        [
            Candidate(
                dex="(main)",
                coin="TEST",
                side="short_perp_long_hedge",
                history_hours=720,
                open_interest_usd=2_000_000,
                day_volume_usd=1_000_000,
                current_apr_pct=87.6,
                realized_apr_pct=200,
                realized_7d_apr_pct=200,
                realized_24h_apr_pct=180,
                estimated_net_7d_apr_pct=170,
                hedge_assessment="24h_crypto_hedge_venue_and_liquidity_review",
                negative_hour_share_pct=0,
                peak_decay_halflife_hours=3,
                eligible=True,
                reasons=(),
                analyzed_at=now,
            )
        ]
    )


def test_matcher_requires_approval_before_opening_position(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    seed_candidate(store)
    matcher = PaperMatcher(
        store,
        venues=[FakeVenue()],  # type: ignore[list-item]
        perp_client=FakePerpClient(),  # type: ignore[arg-type]
    )

    recommendations = matcher.recommend()

    assert len(recommendations) == 1
    assert recommendations[0]["executable_net_apr_pct"] == pytest.approx(163.7142857)
    check = store.latest_paper_match_checks()[0]
    assert check["hedge_venue"] == "coinbase"
    assert check["net_apr_7d_pct"] == pytest.approx(163.7142857)
    assert check["net_apr_14d_pct"] == pytest.approx(179.3571429)
    assert check["net_apr_30d_pct"] == pytest.approx(187.7)
    assert store.open_paper_positions() == []
    recommendation_id = recommendations[0]["id"]

    def approve() -> dict[str, object] | None:
        try:
            return matcher.approve(recommendation_id)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        approvals = list(executor.map(lambda _: approve(), range(2)))

    position = next(item for item in approvals if item is not None)
    assert position["coin"] == "TEST"
    assert position["hedge_venue"] == "coinbase"
    assert position["perp_entry_price"] == 105
    assert position["quantity"] == pytest.approx(1_000 / 105)
    with pytest.raises(ValueError, match="approved"):
        matcher.approve(recommendation_id)
    assert len(store.open_paper_positions()) == 1


def test_approval_rejects_deteriorated_spot_depth(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    seed_candidate(store)
    venue = FakeVenue()
    matcher = PaperMatcher(
        store,
        venues=[venue],  # type: ignore[list-item]
        perp_client=FakePerpClient(),  # type: ignore[arg-type]
    )
    recommendation_id = matcher.recommend()[0]["id"]
    venue.depth_usd = 4_999

    with pytest.raises(ValueError, match="depth"):
        matcher.approve(recommendation_id)

    assert store.open_paper_positions() == []
    assert store.paper_recommendation(recommendation_id)["status"] == "pending"


def test_shadow_workflow_auto_opens_simulated_position(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    seed_candidate(store)
    alerts = []
    monkeypatch.setattr(
        "funding_arb_monitor.matcher.send_discord_alert",
        lambda message, **kwargs: alerts.append(message),
    )
    matcher = PaperMatcher(
        store,
        venues=[FakeVenue()],  # type: ignore[list-item]
        perp_client=FakePerpClient(),  # type: ignore[arg-type]
    )

    result = matcher.shadow()

    assert result["recommendations"] == 1
    assert len(result["opened"]) == 1
    assert result["rejected"] == []
    position = store.open_paper_positions()[0]
    assert position["notes"] == "Auto-opened by the shadow paper scheduler."
    assert "Shadow paper position opened" in alerts[0]


def test_matcher_ignores_an_eligible_market_from_an_older_scan(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    seed_candidate(store)
    store.save_candidates(
        [
            Candidate(
                dex="(main)",
                coin="CURRENT",
                side="short_perp_long_hedge",
                history_hours=168,
                open_interest_usd=2_000_000,
                day_volume_usd=1_000_000,
                current_apr_pct=5,
                realized_apr_pct=5,
                realized_7d_apr_pct=5,
                realized_24h_apr_pct=5,
                estimated_net_7d_apr_pct=-20,
                hedge_assessment="24h_crypto_hedge_venue_and_liquidity_review",
                negative_hour_share_pct=0,
                peak_decay_halflife_hours=None,
                eligible=False,
                reasons=("7d_realized_apr_below_threshold",),
                analyzed_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            )
        ]
    )
    matcher = PaperMatcher(
        store,
        venues=[FakeVenue()],  # type: ignore[list-item]
        perp_client=FakePerpClient(),  # type: ignore[arg-type]
    )

    assert matcher.recommend() == []
