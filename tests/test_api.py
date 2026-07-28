from fastapi.testclient import TestClient

from funding_arb_monitor.api import create_app
from funding_arb_monitor.models import Candidate, utc_now
from funding_arb_monitor.store import Store


def test_dashboard_and_api_are_available(tmp_path) -> None:
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.initialize()
    store.save_paper_match_check(
        candidate_analyzed_at="2026-07-23T00:00:00+00:00",
        coin="CASHCAT",
        status="no_exact_spot_market",
        detail="no exact spot market",
    )
    client = TestClient(create_app(db_path))

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Funding Arb Monitor" in dashboard.text
    assert "Trade recommendations &amp; proposed actions" in dashboard.text
    assert "Monitor only; wait for an exact spot listing" in dashboard.text
    assert client.get("/api/candidates").json() == []
    assert client.get("/api/status").json()["status"] == "never_run"
    assert client.get("/api/paper/recommendations").json() == []
    checks = client.get("/api/paper/match-checks").json()
    assert len(checks) == 1
    assert checks[0]["coin"] == "CASHCAT"
    assert checks[0]["status"] == "no_exact_spot_market"
    assert checks[0]["detail"] == "no exact spot market"
    assert client.get("/api/paper/positions").json() == []
    assert client.get("/api/paper/positions/999/timeline").status_code == 404
    performance = client.get("/api/paper/performance").json()
    assert performance["completed_trades"] == 0
    assert performance["win_rate_pct"] is None
    assert performance["graduation"]["eligible_for_live_review"] is False
    assert client.get("/api/alerts/deliveries").json() == []


def test_approval_token_protects_public_mutation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUNDING_ARB_APPROVAL_TOKEN", "secret")
    client = TestClient(create_app(str(tmp_path / "test.db")))
    url = "/api/paper/recommendations/1/approve"

    assert client.post(url).status_code == 401
    response = client.post(url, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 409
    assert response.json()["detail"] == "recommendation not found"


def test_approval_is_disabled_when_token_is_not_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FUNDING_ARB_APPROVAL_TOKEN", raising=False)
    client = TestClient(create_app(str(tmp_path / "test.db")))

    response = client.post("/api/paper/recommendations/1/approve")

    assert response.status_code == 503
    assert response.json()["detail"] == "paper approval is disabled"


def test_readyz_requires_a_recent_successful_scan(tmp_path) -> None:
    db_path = str(tmp_path / "test.db")
    client = TestClient(create_app(db_path))
    assert client.get("/readyz").status_code == 503

    store = Store(db_path)
    run_id = store.start_scan_run()
    store.finish_scan_run(run_id, status="success")

    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_candidates_can_be_filtered_to_eligible_only(tmp_path) -> None:
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.initialize()
    common = {
        "dex": "(main)",
        "side": "short_perp_long_hedge",
        "history_hours": 168,
        "open_interest_usd": 2_000_000,
        "day_volume_usd": 1_000_000,
        "current_apr_pct": 20.0,
        "realized_apr_pct": 20.0,
        "realized_7d_apr_pct": 20.0,
        "realized_24h_apr_pct": 20.0,
        "estimated_net_7d_apr_pct": 10.0,
        "hedge_assessment": "review",
        "negative_hour_share_pct": 0.0,
        "peak_decay_halflife_hours": None,
        "analyzed_at": utc_now(),
    }
    store.save_candidates(
        [
            Candidate(coin="PASS", eligible=True, reasons=(), **common),
            Candidate(coin="FAIL", eligible=False, reasons=("rejected",), **common),
        ]
    )
    client = TestClient(create_app(db_path))

    response = client.get("/api/candidates?eligible_only=true")

    assert response.status_code == 200
    assert [candidate["coin"] for candidate in response.json()] == ["PASS"]
