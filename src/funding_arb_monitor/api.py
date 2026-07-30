from __future__ import annotations

import os
import threading
import time
from hmac import compare_digest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from .matcher import PaperMatcher
from .scheduler import run_scheduler
from .store import Store


CrossPerpDirection = Literal[
    "short_hyperliquid_long_external",
    "long_hyperliquid_short_external",
]


def create_app(database_path: str | None = None) -> FastAPI:
    resolved_database_path = database_path or os.getenv("FUNDING_ARB_DB", "data/funding_arb.db")
    approval_token = os.getenv("FUNDING_ARB_APPROVAL_TOKEN")
    read_token = os.getenv("FUNDING_ARB_READ_TOKEN")
    store = Store(resolved_database_path)
    store.initialize()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop = threading.Event()
        thread: threading.Thread | None = None
        if os.getenv("FUNDING_ARB_SCHEDULER") == "1":
            thread = threading.Thread(
                target=run_scheduler,
                args=(stop, str(resolved_database_path)),
                daemon=True,
                name="funding-arb-scheduler",
            )
            thread.start()
        yield
        stop.set()
        if thread is not None:
            thread.join(timeout=10)

    app = FastAPI(title="Funding Arb Monitor", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def security(request: Request, call_next):
        if (
            read_token
            and request.method == "GET"
            and request.url.path.startswith("/api/")
            and not compare_digest(
                request.headers.get("X-Read-Token", ""), read_token
            )
        ):
            response = JSONResponse(
                status_code=401, content={"detail": "read token required"}
            )
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, object]:
        max_scan_age_seconds = int(
            os.getenv("FUNDING_ARB_MAX_SCAN_AGE_SECONDS", "7200")
        )
        database_health = store.database_health()
        if not database_health["healthy"]:
            raise HTTPException(
                status_code=503, detail="database storage is unhealthy"
            )
        try:
            run = store.latest_scan_run()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database is unavailable") from exc
        if (
            run is None
            or run["status"] != "success"
            or run["completed_at_ms"] is None
            or int(time.time() * 1000) - int(run["completed_at_ms"])
            > max_scan_age_seconds * 1000
        ):
            raise HTTPException(
                status_code=503, detail="no recent successful scan"
            )
        if (
            os.getenv("FUNDING_ARB_SCHEDULER") == "1"
            and store.unhealthy_scheduled_jobs()
        ):
            raise HTTPException(
                status_code=503, detail="scheduled jobs are unhealthy"
            )
        return {
            "status": "ready",
            "last_scan_completed_at_ms": run["completed_at_ms"],
        }

    @app.get("/api/candidates")
    def candidates(
        limit: int = Query(default=100, ge=1, le=500),
        eligible_only: bool = False,
        current_scan_only: bool = False,
    ) -> list[dict[str, object]]:
        if current_scan_only:
            return store.latest_scan_candidates(limit, eligible_only=eligible_only)
        return store.latest_candidates(limit, eligible_only=eligible_only)

    @app.get("/api/opportunities/actionable")
    def actionable_opportunities(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, object]]:
        return store.actionable_candidates(limit)

    @app.get("/api/opportunities/ranked")
    def ranked_opportunities(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, object]]:
        return store.execution_ranked_candidates(limit)

    @app.get("/api/opportunities/funnel")
    def opportunity_funnel() -> dict[str, object]:
        return store.execution_funnel()

    @app.get("/api/analytics/rejections")
    def rejection_analytics(
        days: int = Query(default=30, ge=1, le=365),
    ) -> dict[str, object]:
        return store.rejection_analytics(days)

    @app.get("/api/cross-perp/summary")
    def cross_perp_summary() -> dict[str, object]:
        return store.cross_perp_summary()

    @app.get("/api/cross-perp/opportunities")
    def cross_perp_opportunities(
        limit: int = Query(default=100, ge=1, le=500),
        observation_ready_only: bool = False,
    ) -> list[dict[str, object]]:
        return store.latest_cross_perp_observations(
            limit, observation_ready_only=observation_ready_only
        )

    @app.get("/api/cross-perp/history")
    def cross_perp_history(
        asset: str,
        external_venue: str,
        direction: CrossPerpDirection,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, object]]:
        return store.cross_perp_history(asset, external_venue, direction, limit)

    @app.get("/api/status")
    def status() -> dict[str, object]:
        result = store.latest_scan_run() or {"status": "never_run"}
        return {
            **result,
            "database": store.database_health(),
            "scheduler": store.scheduler_health(),
        }

    @app.get("/api/paper/positions")
    def paper_positions(include_closed: bool = False) -> list[dict[str, object]]:
        return store.paper_positions(include_closed=include_closed)

    @app.get("/api/paper/positions/{position_id}/timeline")
    def paper_position_timeline(position_id: int) -> dict[str, object]:
        timeline = store.paper_position_timeline(position_id)
        if timeline is None:
            raise HTTPException(status_code=404, detail="paper position not found")
        return timeline

    @app.get("/api/paper/recommendations")
    def paper_recommendations() -> list[dict[str, object]]:
        return store.paper_recommendations()

    @app.get("/api/paper/match-checks")
    def paper_match_checks() -> list[dict[str, object]]:
        return store.latest_paper_match_checks()

    @app.post("/api/paper/recommendations/{recommendation_id}/approve")
    def approve_paper_recommendation(
        recommendation_id: int,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        if not approval_token:
            raise HTTPException(status_code=503, detail="paper approval is disabled")
        supplied_token = (
            authorization.removeprefix("Bearer ")
            if authorization and authorization.startswith("Bearer ")
            else ""
        )
        if not compare_digest(supplied_token, approval_token):
            raise HTTPException(status_code=401, detail="approval token required")
        try:
            return PaperMatcher(store).approve(recommendation_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/paper/report")
    def paper_report() -> dict[str, object]:
        return store.paper_summary()

    @app.get("/api/paper/performance")
    def paper_performance() -> dict[str, object]:
        return store.paper_performance()

    @app.get("/api/paper/strategy-analytics")
    def paper_strategy_analytics() -> dict[str, object]:
        return store.paper_strategy_analytics()

    @app.get("/api/alerts/deliveries")
    def alert_deliveries(
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict[str, object]]:
        return store.alert_deliveries(limit)

    return app


app = create_app()
