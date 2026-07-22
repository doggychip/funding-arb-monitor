from __future__ import annotations

import os

from fastapi import FastAPI, Query

from .store import Store


def create_app(database_path: str | None = None) -> FastAPI:
    store = Store(database_path or os.getenv("FUNDING_ARB_DB", "data/funding_arb.db"))
    store.initialize()
    app = FastAPI(title="Funding Arb Monitor", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/candidates")
    def candidates(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, object]]:
        return store.latest_candidates(limit)

    return app


app = create_app()
