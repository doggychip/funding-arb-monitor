from __future__ import annotations

import os
import threading
from hmac import compare_digest
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse

from .matcher import PaperMatcher
from .scheduler import run_scheduler
from .store import Store


def create_app(database_path: str | None = None) -> FastAPI:
    resolved_database_path = database_path or os.getenv("FUNDING_ARB_DB", "data/funding_arb.db")
    approval_token = os.getenv("FUNDING_ARB_APPROVAL_TOKEN")
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

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/candidates")
    def candidates(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, object]]:
        return store.latest_candidates(limit)

    @app.get("/api/status")
    def status() -> dict[str, object]:
        return store.latest_scan_run() or {"status": "never_run"}

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
        if approval_token:
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

    @app.get("/api/alerts/deliveries")
    def alert_deliveries(
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict[str, object]]:
        return store.alert_deliveries(limit)

    return app


app = create_app()
