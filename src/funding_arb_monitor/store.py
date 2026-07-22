from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Candidate, FundingPoint, MarketSnapshot


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    captured_at TEXT NOT NULL,
                    dex TEXT NOT NULL,
                    coin TEXT NOT NULL,
                    funding_rate REAL NOT NULL,
                    open_interest_usd REAL NOT NULL,
                    day_volume_usd REAL NOT NULL,
                    mark_price REAL NOT NULL,
                    PRIMARY KEY (captured_at, dex, coin)
                );
                CREATE TABLE IF NOT EXISTS funding_points (
                    coin TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    funding_rate REAL NOT NULL,
                    PRIMARY KEY (coin, timestamp_ms)
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    analyzed_at TEXT NOT NULL,
                    dex TEXT NOT NULL,
                    coin TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (analyzed_at, dex, coin)
                );
                """
            )

    def save_snapshots(self, snapshots: list[MarketSnapshot]) -> None:
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO market_snapshots
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.captured_at.isoformat(),
                        item.dex,
                        item.coin,
                        item.funding_rate,
                        item.open_interest_usd,
                        item.day_volume_usd,
                        item.mark_price,
                    )
                    for item in snapshots
                ],
            )

    def save_funding(self, points: list[FundingPoint]) -> None:
        with self.connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO funding_points VALUES (?, ?, ?)",
                [(item.coin, item.timestamp_ms, item.funding_rate) for item in points],
            )

    def save_candidates(self, candidates: list[Candidate]) -> None:
        with self.connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO candidates VALUES (?, ?, ?, ?)",
                [
                    (
                        item.analyzed_at.isoformat(),
                        item.dex,
                        item.coin,
                        json.dumps(item.as_dict(), separators=(",", ":")),
                    )
                    for item in candidates
                ],
            )

    def latest_candidates(self, limit: int = 100) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM candidates
                WHERE analyzed_at = (SELECT MAX(analyzed_at) FROM candidates)
                ORDER BY json_extract(payload_json, '$.realized_7d_apr_pct') DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]
