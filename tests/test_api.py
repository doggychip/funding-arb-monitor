from fastapi.testclient import TestClient

from funding_arb_monitor.api import create_app
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
    performance = client.get("/api/paper/performance").json()
    assert performance["completed_trades"] == 0
    assert performance["win_rate_pct"] is None


def test_approval_token_protects_public_mutation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUNDING_ARB_APPROVAL_TOKEN", "secret")
    client = TestClient(create_app(str(tmp_path / "test.db")))
    url = "/api/paper/recommendations/1/approve"

    assert client.post(url).status_code == 401
    response = client.post(url, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 409
    assert response.json()["detail"] == "recommendation not found"
