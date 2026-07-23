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
