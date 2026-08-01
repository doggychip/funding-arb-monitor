from datetime import datetime, timezone

from fastapi.testclient import TestClient

from funding_arb_monitor.api import create_app
from funding_arb_monitor.models import Candidate, utc_now
from funding_arb_monitor.store import Store


class _CrossPerpObservation:
    def __init__(
        self,
        *,
        asset: str,
        external_venue: str,
        external_symbol: str,
        direction: str,
        observed_at_ms: int,
        net_apr_7d_pct: float,
        reasons: tuple[str, ...] = (),
    ) -> None:
        self.asset = asset
        self.external_venue = external_venue
        self.external_symbol = external_symbol
        self.direction = direction
        self.observed_at_ms = observed_at_ms
        self.net_apr_7d_pct = net_apr_7d_pct
        self.reasons = reasons

    def as_dict(self) -> dict[str, object]:
        return {
            "observed_at_ms": self.observed_at_ms,
            "hyperliquid_dex": "hyperliquid",
            "asset": self.asset,
            "external_venue": self.external_venue,
            "external_symbol": self.external_symbol,
            "direction": self.direction,
            "qualified": True,
            "qualification_version": 2,
            "net_apr_7d_pct": self.net_apr_7d_pct,
            "reasons": list(self.reasons),
        }


def _save_cross_perp_api_run(
    store: Store, observations: list[_CrossPerpObservation]
) -> None:
    run_id = store.start_cross_perp_run()
    saved = store.save_cross_perp_observations(
        run_id, observations, continuity_window_ms=5_400_000
    )
    store.finish_cross_perp_run(
        run_id,
        status="success",
        venue_status={"binance": "success"},
        match_count=len(observations),
        evaluation_count=len(observations),
        positive_net_count=len(observations),
        ready_count=sum(item["observation_ready"] for item in saved),
    )


def test_cross_perp_api_empty_state(tmp_path) -> None:
    client = TestClient(create_app(str(tmp_path / "test.db")))

    assert client.get("/api/cross-perp/summary").json() == {
        "status": "never_run",
        "venue_status": {},
        "match_count": 0,
        "evaluation_count": 0,
        "positive_net_count": 0,
        "ready_count": 0,
        "max_streak": 0,
        "last_ready_at_ms": None,
        "last_ready_run_id": None,
        "last_ready_count": 0,
        "rejection_counts": {},
    }
    assert client.get("/api/cross-perp/opportunities").json() == []


def test_cross_perp_summary_uses_the_newest_run_rejections_only(tmp_path) -> None:
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.initialize()
    _save_cross_perp_api_run(
        store,
        [
            _CrossPerpObservation(
                asset="ZRO",
                external_venue="binance",
                external_symbol="ZROUSDT",
                direction="short_hyperliquid_long_external",
                observed_at_ms=1_000,
                net_apr_7d_pct=20.0,
                reasons=("stale_quote",),
            )
        ],
    )
    failed_run = store.start_cross_perp_run()
    store.finish_cross_perp_run(
        failed_run,
        status="failed",
        venue_status={"binance": "failed"},
        match_count=0,
        evaluation_count=0,
        positive_net_count=0,
        ready_count=0,
    )
    client = TestClient(create_app(db_path))

    summary = client.get("/api/cross-perp/summary").json()

    assert summary["status"] == "failed"
    assert summary["rejection_counts"] == {}


def test_cross_perp_opportunities_are_ranked_and_can_require_ready_streaks(
    tmp_path,
) -> None:
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.initialize()
    zro = {
        "asset": "ZRO",
        "external_venue": "binance",
        "external_symbol": "ZROUSDT",
        "direction": "short_hyperliquid_long_external",
    }
    _save_cross_perp_api_run(
        store,
        [_CrossPerpObservation(observed_at_ms=1_000, net_apr_7d_pct=20.0, **zro)],
    )
    _save_cross_perp_api_run(
        store,
        [
            _CrossPerpObservation(
                observed_at_ms=3_601_000, net_apr_7d_pct=20.0, **zro
            )
        ],
    )
    _save_cross_perp_api_run(
        store,
        [
            _CrossPerpObservation(
                observed_at_ms=7_201_000, net_apr_7d_pct=20.0, **zro
            ),
            _CrossPerpObservation(
                asset="ARB",
                external_venue="binance",
                external_symbol="ARBUSDT",
                direction="long_hyperliquid_short_external",
                observed_at_ms=7_201_000,
                net_apr_7d_pct=30.0,
            ),
        ],
    )
    client = TestClient(create_app(db_path))

    opportunities = client.get("/api/cross-perp/opportunities").json()
    ready = client.get(
        "/api/cross-perp/opportunities?observation_ready_only=true"
    ).json()

    assert [item["asset"] for item in opportunities] == ["ARB", "ZRO"]
    assert [item["asset"] for item in ready] == ["ZRO"]
    assert ready[0]["streak"] == 3
    assert ready[0]["max_streak"] == 3
    assert ready[0]["qualified_scans_24h"] == 3
    assert ready[0]["observed_scans_24h"] == 3
    assert ready[0]["last_ready_at_ms"] == 7_201_000
    summary = client.get("/api/cross-perp/summary").json()
    summary_fields = (
        "max_streak",
        "last_ready_at_ms",
        "last_ready_run_id",
        "last_ready_count",
    )
    assert {key: summary[key] for key in summary_fields} == {
        "max_streak": 3,
        "last_ready_at_ms": 7_201_000,
        "last_ready_run_id": 3,
        "last_ready_count": 1,
    }


def test_cross_perp_history_requires_exact_route_and_allowed_direction(tmp_path) -> None:
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.initialize()
    _save_cross_perp_api_run(
        store,
        [
            _CrossPerpObservation(
                asset="ZRO",
                external_venue="binance",
                external_symbol="ZROUSDT",
                direction="short_hyperliquid_long_external",
                observed_at_ms=1_000,
                net_apr_7d_pct=20.0,
            )
        ],
    )
    client = TestClient(create_app(db_path))
    route = (
        "/api/cross-perp/history?asset=ZRO&external_venue=binance&"
        "direction=short_hyperliquid_long_external"
    )

    assert len(client.get(route).json()) == 1
    assert client.get(route.replace("asset=ZRO", "asset=ARB")).json() == []
    assert client.get(
        route.replace("external_venue=binance", "external_venue=okx")
    ).json() == []
    assert client.get(
        route.replace(
            "short_hyperliquid_long_external", "long_hyperliquid_short_external"
        )
    ).json() == []
    assert client.get(
        route.replace("short_hyperliquid_long_external", "invalid")
    ).status_code == 422
    assert client.get("/api/cross-perp/history").status_code == 422


def test_cross_perp_api_limits_are_bounded(tmp_path) -> None:
    client = TestClient(create_app(str(tmp_path / "test.db")))
    history = (
        "/api/cross-perp/history?asset=ZRO&external_venue=binance&"
        "direction=short_hyperliquid_long_external&limit="
    )

    for url in ("/api/cross-perp/opportunities?limit=", history):
        assert client.get(f"{url}0").status_code == 422
        assert client.get(f"{url}501").status_code == 422


def test_cross_perp_api_read_token_protects_all_routes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUNDING_ARB_READ_TOKEN", "read-secret")
    client = TestClient(create_app(str(tmp_path / "test.db")))
    routes = (
        "/api/cross-perp/summary",
        "/api/cross-perp/opportunities",
        "/api/cross-perp/history?asset=ZRO&external_venue=binance&"
        "direction=short_hyperliquid_long_external",
    )

    for route in routes:
        assert client.get(route).status_code == 401
        assert client.get(
            route, headers={"X-Read-Token": "read-secret"}
        ).status_code == 200


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
    assert 'id="fresh-market-count"' in dashboard.text
    assert 'id="stale-market-count"' in dashboard.text
    assert 'id="no-hedge-count"' in dashboard.text
    assert 'id="paper-progress"' in dashboard.text
    assert 'id="funnel-steps"' in dashboard.text
    assert 'id="cross-perp-status"' in dashboard.text
    assert 'id="cross-perp-counters"' in dashboard.text
    assert 'id="cross-perp-run-age"' in dashboard.text
    assert 'id="cross-perp-max-streak"' in dashboard.text
    assert 'id="cross-perp-last-ready"' in dashboard.text
    assert "Latest run age" in dashboard.text
    assert 'id="cross-perp-rows"' in dashboard.text
    assert 'id="cross-perp-empty"' in dashboard.text
    assert "Observation ready" in dashboard.text
    assert "Actionable cross-perp" not in dashboard.text
    assert 'id="rejection-rows"' in dashboard.text
    assert 'id="strategy-rows"' in dashboard.text
    assert "monitoring current" in dashboard.text
    assert "Funding / costs" in dashboard.text
    assert "Skip; perp execution costs consume carry" in dashboard.text
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


def test_cross_perp_dashboard_accessibility_and_shared_token_prompt(tmp_path) -> None:
    client = TestClient(create_app(str(tmp_path / "test.db")))

    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert 'id="cross-perp-heading"' in dashboard.text
    assert 'role="status"' in dashboard.text
    assert 'aria-live="polite"' in dashboard.text
    assert 'aria-atomic="true"' in dashboard.text
    assert 'aria-busy="true"' in dashboard.text
    assert 'aria-labelledby="cross-perp-heading"' in dashboard.text
    assert dashboard.text.count('<th scope="col">') >= 10
    assert 'id="cross-perp-empty" hidden' in dashboard.text
    assert "let readTokenPromptPromise = null;" in dashboard.text
    assert "if (!readTokenPromptPromise)" in dashboard.text
    assert "await promptForReadToken()" in dashboard.text


def test_cross_perp_reload_hides_stale_empty_state_before_fetch(tmp_path) -> None:
    client = TestClient(create_app(str(tmp_path / "test.db")))

    dashboard = client.get("/")
    loader = dashboard.text.split("async function loadCrossPerp()", 1)[1]
    loading_prelude = loader.split("try {", 1)[0]

    assert 'status.textContent = "Loading public cross-perpetual evidence…";' in loading_prelude
    assert 'status.setAttribute("aria-busy", "true");' in loading_prelude
    assert "empty.hidden = true;" in loading_prelude


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


def test_readyz_allows_a_failed_cross_perp_scheduler_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUNDING_ARB_SCHEDULER", "1")
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.initialize()
    run_id = store.start_scan_run()
    store.finish_scan_run(run_id, status="success")
    job_run_id = store.start_scheduled_job("cross-perp", "2026-07-30T18:06")
    store.finish_scheduled_job(job_run_id, exit_code=2, error="venue outage")
    client = TestClient(create_app(db_path))

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
    monitoring = client.get("/api/opportunities/monitoring").json()

    assert historical[0]["coin"] == "OLDER"
    assert historical[0]["scan_id"] == first_run
    assert historical[0]["actionable_now"] is False
    assert historical[0]["monitoring_current"] is False
    assert historical[0]["analysis_age_seconds"] >= 0
    assert actionable == []
    assert monitoring == []


def test_dashboard_labels_candidate_analysis_age(tmp_path) -> None:
    client = TestClient(create_app(str(tmp_path / "test.db")))

    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "Analysis age" in dashboard.text


def test_execution_funnel_and_ranking_use_latest_match_checks(tmp_path) -> None:
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.initialize()
    analyzed_at = datetime.now(timezone.utc)
    run_id = store.start_scan_run()
    common = {
        "dex": "(main)",
        "side": "short_perp_long_hedge",
        "history_hours": 168,
        "open_interest_usd": 2_000_000,
        "day_volume_usd": 1_000_000,
        "current_apr_pct": 20.0,
        "realized_apr_pct": 20.0,
        "realized_24h_apr_pct": 20.0,
        "estimated_net_7d_apr_pct": 10.0,
        "hedge_assessment": "review",
        "negative_hour_share_pct": 0.0,
        "peak_decay_halflife_hours": None,
    }
    store.save_candidates(
        [
            Candidate(
                coin="PROFIT",
                realized_7d_apr_pct=40.0,
                eligible=True,
                reasons=(),
                analyzed_at=analyzed_at,
                **common,
            ),
            Candidate(
                coin="LOSS",
                realized_7d_apr_pct=35.0,
                eligible=True,
                reasons=(),
                analyzed_at=analyzed_at,
                **common,
            ),
            Candidate(
                coin="REJECTED",
                realized_7d_apr_pct=5.0,
                eligible=False,
                reasons=("7d_realized_apr_below_threshold",),
                analyzed_at=analyzed_at,
                **common,
            ),
        ],
        scan_run_id=run_id,
    )
    store.finish_scan_run(
        run_id,
        status="success",
        snapshot_count=10,
        candidate_count=3,
        eligible_count=2,
    )
    store.save_paper_match_check(
        candidate_analyzed_at=analyzed_at.isoformat(),
        coin="PROFIT",
        status="pending_approval",
        detail="executable",
        hedge_venue="binance",
        hedge_symbol="PROFITUSDC",
        net_apr_7d_pct=20,
    )
    store.save_paper_match_check(
        candidate_analyzed_at=analyzed_at.isoformat(),
        coin="LOSS",
        status="net_carry_below_threshold",
        detail="not profitable",
        hedge_venue="okx",
        hedge_symbol="LOSS-USDC",
        net_apr_7d_pct=-5,
    )
    client = TestClient(create_app(db_path))

    funnel = client.get("/api/opportunities/funnel").json()
    ranked = client.get("/api/opportunities/ranked").json()
    monitoring = client.get("/api/opportunities/monitoring").json()
    actionable = client.get("/api/opportunities/actionable").json()

    assert funnel == {
        "scan_id": run_id,
        "discovered": 10,
        "analyzed": 3,
        "monitoring_eligible": 2,
        "spot_matched": 2,
        "spot_depth_sufficient": 2,
        "profitable_after_costs": 1,
        "perp_executable": 1,
        "paper_opened": 0,
    }
    assert [item["coin"] for item in ranked] == ["PROFIT", "LOSS"]
    assert ranked[0]["executable_net_apr_pct"] == 20
    assert ranked[1]["executable_net_apr_pct"] == -5
    assert ranked[1]["execution_status"] == "net_carry_below_threshold"
    assert [item["coin"] for item in monitoring] == ["PROFIT", "LOSS"]
    assert all(item["monitoring_current"] for item in monitoring)
    assert all(item["actionable_now"] is False for item in monitoring)
    assert [item["coin"] for item in actionable] == ["PROFIT"]
    assert actionable[0]["execution_status"] == "pending_approval"
    assert actionable[0]["actionable_now"] is True


def test_rejection_analytics_counts_monitoring_and_execution_history(tmp_path) -> None:
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.initialize()
    analyzed_at = datetime.now(timezone.utc)
    common = {
        "dex": "(main)",
        "side": "short_perp_long_hedge",
        "history_hours": 168,
        "open_interest_usd": 2_000_000,
        "day_volume_usd": 1_000_000,
        "current_apr_pct": 5.0,
        "realized_apr_pct": 5.0,
        "realized_7d_apr_pct": 5.0,
        "realized_24h_apr_pct": 5.0,
        "estimated_net_7d_apr_pct": -10.0,
        "hedge_assessment": "review",
        "negative_hour_share_pct": 0.0,
        "peak_decay_halflife_hours": None,
        "eligible": False,
        "analyzed_at": analyzed_at,
    }
    store.save_candidates(
        [
            Candidate(
                coin="LOW",
                reasons=("7d_realized_apr_below_threshold",),
                **common,
            ),
            Candidate(
                coin="THIN",
                reasons=("day_volume_below_threshold",),
                **common,
            ),
        ]
    )
    store.save_paper_match_check(
        candidate_analyzed_at=analyzed_at.isoformat(),
        coin="NOHEDGE",
        status="no_exact_spot_market",
        detail="no exact spot market",
    )
    store.save_paper_match_check(
        candidate_analyzed_at=analyzed_at.isoformat(),
        coin="TOOCOSTLY",
        status="net_carry_below_threshold",
        detail="costs exceed carry",
        hedge_venue="binance",
        hedge_symbol="TOOCOSTLYUSDC",
        net_apr_7d_pct=-2,
    )
    store.save_paper_match_check(
        candidate_analyzed_at=analyzed_at.isoformat(),
        coin="PASSED",
        status="pending_approval",
        detail="execution checks passed",
        hedge_venue="binance",
        hedge_symbol="PASSEDUSDC",
        net_apr_7d_pct=20,
    )
    client = TestClient(create_app(db_path))

    analytics = client.get("/api/analytics/rejections?days=30").json()

    assert analytics["monitoring_reasons"] == {
        "7d_realized_apr_below_threshold": 1,
        "day_volume_below_threshold": 1,
    }
    assert analytics["execution_statuses"] == {
        "net_carry_below_threshold": 1,
        "no_exact_spot_market": 1,
    }
    assert analytics["daily"][0]["monitoring_rejections"] == 2
    assert analytics["daily"][0]["execution_checks"] == 2
