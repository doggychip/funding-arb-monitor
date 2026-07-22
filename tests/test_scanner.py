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

    def funding_history(self, coin: str, days: int) -> list[FundingPoint]:
        return [FundingPoint(coin, hour, 0.00003) for hour in range(168)]


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
