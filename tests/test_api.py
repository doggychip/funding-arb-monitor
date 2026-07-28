from datetime import datetime, timezone

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
    status = client.get("/api/status").json()
    assert status["status"] == "never_run"
    assert status["database"]["healthy"] is True
    assert status["scheduler"]["healthy"] is True
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


def test_optional_read_token_protects_operational_gets_only(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FUNDING_ARB_READ_TOKEN", "read-secret")
    client = TestClient(create_app(str(tmp_path / "test.db")))

    assert client.get("/").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
    assert client.get("/api/status").status_code == 401
    response = client.get(
        "/api/status", headers={"X-Read-Token": "read-secret"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "never_run"


def test_security_and_api_cache_headers_are_set(tmp_path) -> None:
    client = TestClient(create_app(str(tmp_path / "test.db")))

    dashboard = client.get("/")
    api = client.get("/api/status")

    assert dashboard.headers["x-content-type-options"] == "nosniff"
    assert dashboard.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in dashboard.headers["content-security-policy"]
    assert api.headers["cache-control"] == "no-store"


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


def test_readyz_rejects_a_failed_critical_scheduler_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUNDING_ARB_SCHEDULER", "1")
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.initialize()
    run_id = store.start_scan_run()
    store.finish_scan_run(run_id, status="success")
    job_run_id = store.start_scheduled_job("update", "2026-07-28T12:12")
    store.finish_scheduled_job(job_run_id, exit_code=2, error="failed")
    client = TestClient(create_app(db_path))

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["detail"] == "scheduled jobs are unhealthy"


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


def test_candidates_return_the_newest_analysis_for_every_market(tmp_path) -> None:
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
        "eligible": False,
        "reasons": ("rejected",),
    }
    first_scan = datetime(2026, 7, 28, 1, tzinfo=timezone.utc)
    second_scan = datetime(2026, 7, 28, 2, tzinfo=timezone.utc)
    store.save_candidates(
        [
            Candidate(coin="ROTATED", analyzed_at=first_scan, **common),
            Candidate(coin="UPDATED", analyzed_at=first_scan, **common),
        ]
    )
    store.save_candidates(
        [
            Candidate(
                coin="UPDATED",
                analyzed_at=second_scan,
                current_apr_pct=30.0,
                **{key: value for key, value in common.items() if key != "current_apr_pct"},
            )
        ]
    )
    client = TestClient(create_app(db_path))

    all_markets = client.get("/api/candidates?limit=500").json()
    current_batch = client.get(
        "/api/candidates?limit=500&current_scan_only=true"
    ).json()

    assert {candidate["coin"] for candidate in all_markets} == {
        "ROTATED",
        "UPDATED",
    }
    updated = next(candidate for candidate in all_markets if candidate["coin"] == "UPDATED")
    assert updated["current_apr_pct"] == 30.0
    assert [candidate["coin"] for candidate in current_batch] == ["UPDATED"]


def test_actionable_opportunities_exclude_older_eligible_candidates(tmp_path) -> None:
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
        "reasons": (),
    }
    first_run = store.start_scan_run()
    store.save_candidates(
        [
            Candidate(
                coin="OLDER",
                analyzed_at=datetime(2026, 7, 28, 1, tzinfo=timezone.utc),
                eligible=True,
                **common,
            )
        ],
        scan_run_id=first_run,
    )
    store.finish_scan_run(first_run, status="success", eligible_count=1)
    second_run = store.start_scan_run()
    store.save_candidates(
        [
            Candidate(
                coin="CURRENT",
                analyzed_at=datetime(2026, 7, 28, 2, tzinfo=timezone.utc),
                eligible=False,
                reasons=("rejected",),
                **{key: value for key, value in common.items() if key != "reasons"},
            )
        ],
        scan_run_id=second_run,
    )
    store.finish_scan_run(second_run, status="success", eligible_count=0)
    client = TestClient(create_app(db_path))

    historical = client.get("/api/candidates?eligible_only=true").json()
    actionable = client.get("/api/opportunities/actionable").json()

    assert historical[0]["coin"] == "OLDER"
    assert historical[0]["scan_id"] == first_run
    assert historical[0]["actionable_now"] is False
    assert historical[0]["analysis_age_seconds"] >= 0
    assert actionable == []


def test_dashboard_labels_candidate_analysis_age(tmp_path) -> None:
    client = TestClient(create_app(str(tmp_path / "test.db")))

    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "Analysis age" in dashboard.text
