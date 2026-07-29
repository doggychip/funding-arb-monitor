import time
from datetime import datetime, timezone

import pytest

from funding_arb_monitor.models import FundingPoint, MarketSnapshot
from funding_arb_monitor.scanner import ScanConfig, Scanner
from funding_arb_monitor.store import Store


class FakeClient:
    def snapshots(self) -> list[MarketSnapshot]:
        return [
            MarketSnapshot(
                dex="(main)",
                coin="TEST",
                funding_rate=0.00003,
                open_interest_usd=2_000_000,
                day_volume_usd=1_000_000,
                mark_price=1,
                captured_at=datetime.now(timezone.utc),
            )
        ]

    def funding_history(
        self,
        coin: str,
        days: int,
        start_time_ms: int | None = None,
    ) -> list[FundingPoint]:
        first_hour = int(time.time() * 1000) - 167 * 3_600_000
        return [FundingPoint(coin, first_hour + hour * 3_600_000, 0.00003) for hour in range(168)]


def test_scanner_marks_consistent_positive_carry_eligible(tmp_path) -> None:
    scanner = Scanner(
        FakeClient(),  # type: ignore[arg-type]
        Store(tmp_path / "test.db"),
        ScanConfig(min_realized_7d_apr_pct=15),
    )
    candidates = scanner.run()

    assert len(candidates) == 1
    assert candidates[0].eligible is True
    assert candidates[0].side == "short_perp_long_hedge"
    assert candidates[0].realized_7d_apr_pct == pytest.approx(26.28)


class FailingClient(FakeClient):
    def funding_history(
        self,
        coin: str,
        days: int,
        start_time_ms: int | None = None,
    ) -> list[FundingPoint]:
        raise RuntimeError("temporary upstream failure")


def test_scanner_uses_cached_history_but_marks_refresh_failure(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    first_hour = int(time.time() * 1000) - 167 * 3_600_000
    store.save_funding(
        [FundingPoint("TEST", first_hour + hour * 3_600_000, 0.00003) for hour in range(168)]
    )

    candidates = Scanner(
        FailingClient(),  # type: ignore[arg-type]
        store,
        ScanConfig(min_realized_7d_apr_pct=15),
    ).run()

    assert len(candidates) == 1
    assert candidates[0].eligible is False
    assert "funding_refresh_failed" in candidates[0].reasons


def test_scanner_alerts_on_full_scan_failure(tmp_path, monkeypatch) -> None:
    alerts = []
    monkeypatch.setattr(
        "funding_arb_monitor.scanner.send_discord_alert",
        lambda message, **kwargs: alerts.append(message),
    )
    client = FakeClient()
    monkeypatch.setattr(client, "snapshots", lambda: [])

    with pytest.raises(RuntimeError, match="no live markets"):
        Scanner(
            client,  # type: ignore[arg-type]
            Store(tmp_path / "test.db"),
            ScanConfig(),
        ).run()

    assert "Funding monitor scan failed" in alerts[0]


def test_history_selection_keeps_a_current_leader_and_rotates_an_unseen_market(
    tmp_path,
) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    store.save_funding([FundingPoint("HOT", 1_000, 0.001)])
    captured_at = datetime.now(timezone.utc)
    snapshots = [
        MarketSnapshot(
            dex="(main)",
            coin="HOT",
            funding_rate=0.001,
            open_interest_usd=2_000_000,
            day_volume_usd=1_000_000,
            mark_price=1,
            captured_at=captured_at,
        ),
        MarketSnapshot(
            dex="(main)",
            coin="UNSEEN_A",
            funding_rate=0.00001,
            open_interest_usd=2_000_000,
            day_volume_usd=1_000_000,
            mark_price=1,
            captured_at=captured_at,
        ),
        MarketSnapshot(
            dex="(main)",
            coin="UNSEEN_B",
            funding_rate=0.000005,
            open_interest_usd=2_000_000,
            day_volume_usd=1_000_000,
            mark_price=1,
            captured_at=captured_at,
        ),
    ]
    scanner = Scanner(
        FakeClient(),  # type: ignore[arg-type]
        store,
        ScanConfig(max_history_fetches=2),
    )

    assert [item.coin for item in scanner.select_history_candidates(snapshots)] == [
        "HOT",
        "UNSEEN_A",
    ]


def test_history_selection_prioritizes_a_hedgeable_leader(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    captured_at = datetime.now(timezone.utc)
    snapshots = [
        MarketSnapshot(
            dex="(main)",
            coin="UNHEDGEABLE",
            funding_rate=0.002,
            open_interest_usd=2_000_000,
            day_volume_usd=1_000_000,
            mark_price=1,
            captured_at=captured_at,
        ),
        MarketSnapshot(
            dex="(main)",
            coin="HEDGEABLE",
            funding_rate=0.001,
            open_interest_usd=2_000_000,
            day_volume_usd=1_000_000,
            mark_price=1,
            captured_at=captured_at,
        ),
    ]
    scanner = Scanner(
        FakeClient(),  # type: ignore[arg-type]
        store,
        ScanConfig(max_history_fetches=1),
        hedgeable_assets_provider=lambda: {"HEDGEABLE"},
    )

    assert [item.coin for item in scanner.select_history_candidates(snapshots)] == [
        "HEDGEABLE"
    ]


def test_history_selection_matches_namespaced_perp_to_exact_spot_asset(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    captured_at = datetime.now(timezone.utc)
    snapshots = [
        MarketSnapshot(
            dex="(main)",
            coin="UNHEDGEABLE",
            funding_rate=0.002,
            open_interest_usd=2_000_000,
            day_volume_usd=1_000_000,
            mark_price=1,
            captured_at=captured_at,
        ),
        MarketSnapshot(
            dex="xyz",
            coin="xyz:HEDGEABLE",
            funding_rate=0.001,
            open_interest_usd=2_000_000,
            day_volume_usd=1_000_000,
            mark_price=1,
            captured_at=captured_at,
        ),
    ]
    scanner = Scanner(
        FakeClient(),  # type: ignore[arg-type]
        store,
        ScanConfig(max_history_fetches=1),
        hedgeable_assets_provider=lambda: {"HEDGEABLE"},
    )

    assert [item.coin for item in scanner.select_history_candidates(snapshots)] == [
        "xyz:HEDGEABLE"
    ]


def test_history_selection_falls_back_when_spot_catalogues_fail(tmp_path) -> None:
    def unavailable_catalogues() -> set[str]:
        raise RuntimeError("spot venues unavailable")

    captured_at = datetime.now(timezone.utc)
    snapshots = [
        MarketSnapshot(
            dex="(main)",
            coin="HOT",
            funding_rate=0.002,
            open_interest_usd=2_000_000,
            day_volume_usd=1_000_000,
            mark_price=1,
            captured_at=captured_at,
        ),
        MarketSnapshot(
            dex="(main)",
            coin="COOL",
            funding_rate=0.001,
            open_interest_usd=2_000_000,
            day_volume_usd=1_000_000,
            mark_price=1,
            captured_at=captured_at,
        ),
    ]
    store = Store(tmp_path / "test.db")
    store.initialize()
    scanner = Scanner(
        FakeClient(),  # type: ignore[arg-type]
        store,
        ScanConfig(max_history_fetches=1),
        hedgeable_assets_provider=unavailable_catalogues,
    )

    assert [item.coin for item in scanner.select_history_candidates(snapshots)] == ["HOT"]


def test_default_history_budget_refreshes_eighty_liquid_markets(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    captured_at = datetime.now(timezone.utc)
    snapshots = [
        MarketSnapshot(
            dex="(main)",
            coin=f"COIN_{index:03}",
            funding_rate=index / 1_000_000,
            open_interest_usd=2_000_000,
            day_volume_usd=1_000_000,
            mark_price=1,
            captured_at=captured_at,
        )
        for index in range(100)
    ]
    scanner = Scanner(
        FakeClient(),  # type: ignore[arg-type]
        store,
        ScanConfig(),
    )

    assert len(scanner.select_history_candidates(snapshots)) == 80
