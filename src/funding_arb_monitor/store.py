from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .costs import CostAssumptions
from .models import Candidate, FundingPoint, MarketSnapshot


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
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
                CREATE TABLE IF NOT EXISTS paper_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    coin TEXT NOT NULL,
                    hedge_venue TEXT NOT NULL,
                    side TEXT NOT NULL,
                    notional_usd REAL NOT NULL,
                    entry_cost_usd REAL NOT NULL,
                    hedge_assessment TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    opened_at_ms INTEGER NOT NULL,
                    closed_at_ms INTEGER
                );
                CREATE TABLE IF NOT EXISTS paper_accruals (
                    position_id INTEGER NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    funding_pnl_usd REAL NOT NULL,
                    PRIMARY KEY (position_id, timestamp_ms),
                    FOREIGN KEY (position_id) REFERENCES paper_positions(id)
                );
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at_ms INTEGER NOT NULL,
                    completed_at_ms INTEGER,
                    status TEXT NOT NULL,
                    snapshot_count INTEGER NOT NULL DEFAULT 0,
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    eligible_count INTEGER NOT NULL DEFAULT 0,
                    failed_market_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS cross_perp_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at_ms INTEGER NOT NULL,
                    completed_at_ms INTEGER,
                    status TEXT NOT NULL,
                    venue_status_json TEXT NOT NULL DEFAULT '{}',
                    match_count INTEGER NOT NULL DEFAULT 0,
                    evaluation_count INTEGER NOT NULL DEFAULT 0,
                    positive_net_count INTEGER NOT NULL DEFAULT 0,
                    ready_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS cross_perp_observations (
                    run_id INTEGER NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    hyperliquid_dex TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    external_venue TEXT NOT NULL,
                    external_symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    qualified INTEGER NOT NULL,
                    streak INTEGER NOT NULL,
                    observation_ready INTEGER NOT NULL,
                    net_apr_7d_pct REAL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (
                        run_id, hyperliquid_dex, asset, external_venue,
                        external_symbol, direction
                    ),
                    FOREIGN KEY (run_id) REFERENCES cross_perp_runs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_cross_perp_latest
                ON cross_perp_observations(run_id, observation_ready, net_apr_7d_pct);
                CREATE TABLE IF NOT EXISTS cross_perp_entry_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checked_at_ms INTEGER NOT NULL,
                    source_run_id INTEGER NOT NULL,
                    hyperliquid_dex TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    external_venue TEXT NOT NULL,
                    external_symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (source_run_id) REFERENCES cross_perp_runs(id)
                );
                CREATE TABLE IF NOT EXISTS cross_perp_paper_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_check_id INTEGER NOT NULL,
                    source_run_id INTEGER NOT NULL,
                    opened_at_ms INTEGER NOT NULL,
                    closed_at_ms INTEGER,
                    hyperliquid_dex TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    external_venue TEXT NOT NULL,
                    external_symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    notional_usd REAL NOT NULL,
                    hyperliquid_entry_price REAL NOT NULL,
                    external_entry_price REAL NOT NULL,
                    hyperliquid_quantity REAL NOT NULL,
                    external_quantity REAL NOT NULL,
                    entry_fee_usd REAL NOT NULL,
                    estimated_exit_fee_usd REAL NOT NULL,
                    forecast_funding_usd REAL NOT NULL,
                    forecast_transaction_cost_usd REAL NOT NULL,
                    forecast_net_profit_usd REAL NOT NULL,
                    forecast_net_apr_pct REAL NOT NULL,
                    forecast_basis_bps REAL NOT NULL,
                    exit_reason TEXT,
                    hyperliquid_exit_price REAL,
                    external_exit_price REAL,
                    exit_fee_usd REAL,
                    FOREIGN KEY (entry_check_id) REFERENCES cross_perp_entry_checks(id),
                    FOREIGN KEY (source_run_id) REFERENCES cross_perp_runs(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_cross_perp_paper_open_route
                ON cross_perp_paper_positions(
                    hyperliquid_dex, asset, external_venue, external_symbol, direction
                ) WHERE closed_at_ms IS NULL;
                CREATE TABLE IF NOT EXISTS cross_perp_paper_marks (
                    position_id INTEGER NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    hyperliquid_exit_price REAL NOT NULL,
                    external_exit_price REAL NOT NULL,
                    hyperliquid_pnl_usd REAL NOT NULL,
                    external_pnl_usd REAL NOT NULL,
                    basis_bps REAL NOT NULL,
                    net_apr_7d_pct REAL,
                    qualified INTEGER NOT NULL,
                    reasons_json TEXT NOT NULL,
                    PRIMARY KEY (position_id, timestamp_ms),
                    FOREIGN KEY (position_id) REFERENCES cross_perp_paper_positions(id)
                );
                CREATE TABLE IF NOT EXISTS cross_perp_paper_accruals (
                    position_id INTEGER NOT NULL,
                    leg TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    funding_rate REAL NOT NULL,
                    funding_pnl_usd REAL NOT NULL,
                    PRIMARY KEY (position_id, leg, timestamp_ms),
                    FOREIGN KEY (position_id) REFERENCES cross_perp_paper_positions(id)
                );
                CREATE TABLE IF NOT EXISTS cross_perp_funding_forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_check_id INTEGER NOT NULL,
                    source_run_id INTEGER NOT NULL,
                    predicted_at_ms INTEGER NOT NULL,
                    hyperliquid_dex TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    external_venue TEXT NOT NULL,
                    external_symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    leg TEXT NOT NULL,
                    settlement_at_ms INTEGER NOT NULL,
                    predicted_rate REAL NOT NULL,
                    direction_sign INTEGER NOT NULL,
                    notional_usd REAL NOT NULL,
                    predicted_pnl_usd REAL NOT NULL,
                    actual_settlement_at_ms INTEGER,
                    actual_rate REAL,
                    actual_pnl_usd REAL,
                    reconciled_at_ms INTEGER,
                    UNIQUE (entry_check_id, leg, settlement_at_ms),
                    FOREIGN KEY (entry_check_id) REFERENCES cross_perp_entry_checks(id),
                    FOREIGN KEY (source_run_id) REFERENCES cross_perp_runs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_cross_perp_forecast_pending
                ON cross_perp_funding_forecasts(
                    hyperliquid_dex, asset, external_venue, external_symbol,
                    direction, leg, settlement_at_ms, reconciled_at_ms
                );
                CREATE TABLE IF NOT EXISTS paper_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    coin TEXT NOT NULL,
                    candidate_analyzed_at TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    hedge_symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    notional_usd REAL NOT NULL,
                    quantity REAL NOT NULL,
                    perp_entry_price REAL NOT NULL,
                    hedge_entry_price REAL NOT NULL,
                    gross_apr_pct REAL NOT NULL,
                    executable_net_apr_pct REAL NOT NULL,
                    hedge_fee_bps REAL NOT NULL,
                    hedge_spread_bps REAL NOT NULL,
                    bid_depth_usd REAL NOT NULL,
                    ask_depth_usd REAL NOT NULL,
                    entry_cost_usd REAL NOT NULL,
                    estimated_exit_cost_usd REAL NOT NULL,
                    UNIQUE(candidate_analyzed_at, coin, venue)
                );
                CREATE TABLE IF NOT EXISTS paper_position_marks (
                    position_id INTEGER NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    perp_price REAL NOT NULL,
                    hedge_price REAL NOT NULL,
                    perp_pnl_usd REAL NOT NULL,
                    hedge_pnl_usd REAL NOT NULL,
                    hedge_drift_pct REAL NOT NULL,
                    PRIMARY KEY (position_id, timestamp_ms),
                    FOREIGN KEY (position_id) REFERENCES paper_positions(id)
                );
                CREATE TABLE IF NOT EXISTS paper_match_checks (
                    candidate_analyzed_at TEXT NOT NULL,
                    coin TEXT NOT NULL,
                    checked_at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    PRIMARY KEY (candidate_analyzed_at, coin)
                );
                CREATE TABLE IF NOT EXISTS paper_liquidity_checks (
                    position_id INTEGER NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    day_volume_usd REAL NOT NULL,
                    bid_depth_usd REAL NOT NULL,
                    ask_depth_usd REAL NOT NULL,
                    degraded INTEGER NOT NULL,
                    reasons TEXT NOT NULL,
                    PRIMARY KEY (position_id, timestamp_ms),
                    FOREIGN KEY (position_id) REFERENCES paper_positions(id)
                );
                CREATE TABLE IF NOT EXISTS alert_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    attempted_at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS scheduled_job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    scheduled_slot TEXT NOT NULL,
                    started_at_ms INTEGER NOT NULL,
                    completed_at_ms INTEGER,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    error TEXT
                );
                """
            )
            self._ensure_column(connection, "paper_positions", "recommendation_id", "INTEGER")
            self._ensure_column(connection, "paper_positions", "perp_entry_price", "REAL")
            self._ensure_column(connection, "paper_positions", "hedge_entry_price", "REAL")
            self._ensure_column(connection, "paper_positions", "quantity", "REAL")
            self._ensure_column(connection, "paper_positions", "hedge_symbol", "TEXT")
            self._ensure_column(connection, "paper_positions", "hedge_fee_bps", "REAL")
            self._ensure_column(connection, "paper_positions", "hedge_spread_bps", "REAL")
            self._ensure_column(
                connection, "paper_positions", "estimated_exit_cost_usd", "REAL NOT NULL DEFAULT 0"
            )
            self._ensure_column(connection, "paper_positions", "exit_cost_usd", "REAL")
            self._ensure_column(connection, "paper_positions", "exit_reason", "TEXT")
            self._ensure_column(connection, "paper_positions", "perp_exit_price", "REAL")
            self._ensure_column(connection, "paper_positions", "hedge_exit_price", "REAL")
            self._ensure_column(connection, "paper_positions", "exit_quantity", "REAL")
            self._ensure_column(connection, "paper_positions", "exit_hedge_spread_bps", "REAL")
            self._ensure_column(connection, "paper_positions", "exit_bid_depth_usd", "REAL")
            self._ensure_column(connection, "paper_positions", "exit_ask_depth_usd", "REAL")
            self._ensure_column(connection, "paper_positions", "exit_executed_at_ms", "INTEGER")
            self._ensure_column(connection, "paper_match_checks", "hedge_venue", "TEXT")
            self._ensure_column(connection, "paper_match_checks", "hedge_symbol", "TEXT")
            self._ensure_column(connection, "paper_match_checks", "net_apr_7d_pct", "REAL")
            self._ensure_column(connection, "paper_match_checks", "net_apr_14d_pct", "REAL")
            self._ensure_column(connection, "paper_match_checks", "net_apr_30d_pct", "REAL")
            self._ensure_column(connection, "candidates", "scan_run_id", "INTEGER")
            self._ensure_column(connection, "paper_recommendations", "perp_bid_depth_usd", "REAL")
            self._ensure_column(connection, "paper_recommendations", "perp_ask_depth_usd", "REAL")
            self._ensure_column(connection, "paper_recommendations", "perp_spread_bps", "REAL")
            self._ensure_column(connection, "paper_recommendations", "perp_quote_at_ms", "INTEGER")
            self._ensure_column(
                connection,
                "cross_perp_paper_positions",
                "forward_funding_usd_24h",
                "REAL",
            )
            self._ensure_column(
                connection,
                "cross_perp_paper_positions",
                "forward_net_profit_usd_24h",
                "REAL",
            )
            self._ensure_column(
                connection,
                "cross_perp_paper_positions",
                "committed_capital_usd",
                "REAL",
            )
            self._ensure_column(
                connection,
                "cross_perp_paper_positions",
                "readiness_level",
                "TEXT",
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def latest_funding_timestamp(self, coin: str) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(timestamp_ms) AS timestamp_ms FROM funding_points WHERE coin = ?",
                (coin,),
            ).fetchone()
        return int(row["timestamp_ms"]) if row and row["timestamp_ms"] is not None else None

    def latest_funding_timestamps(self, coins: list[str]) -> dict[str, int]:
        if not coins:
            return {}
        placeholders = ", ".join("?" for _ in coins)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT coin, MAX(timestamp_ms) AS timestamp_ms
                FROM funding_points
                WHERE coin IN ({placeholders})
                GROUP BY coin
                """,
                coins,
            ).fetchall()
        return {str(row["coin"]): int(row["timestamp_ms"]) for row in rows}

    def funding_history(self, coin: str, start_time_ms: int) -> list[FundingPoint]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT coin, timestamp_ms, funding_rate
                FROM funding_points
                WHERE coin = ? AND timestamp_ms >= ?
                ORDER BY timestamp_ms
                """,
                (coin, start_time_ms),
            ).fetchall()
        return [
            FundingPoint(
                coin=row["coin"],
                timestamp_ms=row["timestamp_ms"],
                funding_rate=row["funding_rate"],
            )
            for row in rows
        ]

    def save_candidates(
        self, candidates: list[Candidate], *, scan_run_id: int | None = None
    ) -> None:
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO candidates (
                    analyzed_at, dex, coin, payload_json, scan_run_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.analyzed_at.isoformat(),
                        item.dex,
                        item.coin,
                        json.dumps(item.as_dict(), separators=(",", ":")),
                        scan_run_id,
                    )
                    for item in candidates
                ],
            )

    def start_scan_run(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO scan_runs (started_at_ms, status) VALUES (?, 'running')",
                (int(time.time() * 1000),),
            )
        return int(cursor.lastrowid)

    def finish_scan_run(
        self,
        run_id: int,
        *,
        status: str,
        snapshot_count: int = 0,
        candidate_count: int = 0,
        eligible_count: int = 0,
        failed_market_count: int = 0,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE scan_runs
                SET completed_at_ms = ?, status = ?, snapshot_count = ?,
                    candidate_count = ?, eligible_count = ?, failed_market_count = ?, error = ?
                WHERE id = ?
                """,
                (
                    int(time.time() * 1000),
                    status,
                    snapshot_count,
                    candidate_count,
                    eligible_count,
                    failed_market_count,
                    error[:500] if error else None,
                    run_id,
                ),
            )

    def latest_scan_run(self) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def latest_successful_scan_run(self) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM scan_runs
                WHERE status = 'success'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def start_cross_perp_run(self) -> int:
        started_at_ms = int(time.time() * 1000)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE cross_perp_runs
                SET completed_at_ms = ?, status = 'failed',
                    error = 'abandoned by newer cross-perp run'
                WHERE status = 'running'
                """,
                (started_at_ms,),
            )
            cursor = connection.execute(
                "INSERT INTO cross_perp_runs (started_at_ms, status) VALUES (?, 'running')",
                (started_at_ms,),
            )
        return int(cursor.lastrowid)

    def save_cross_perp_observations(
        self, run_id: int, observations: list[object], *, continuity_window_ms: int
    ) -> list[dict[str, object]]:
        saved: list[dict[str, object]] = []
        with self.connect() as connection:
            previous_run = connection.execute(
                """
                SELECT id, status FROM cross_perp_runs
                WHERE id < ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            for observation in observations:
                payload = observation.as_dict()
                qualified = bool(payload["qualified"])
                streak = 0
                if qualified:
                    previous_observation = None
                    if previous_run and previous_run["status"] == "success":
                        previous_observation = connection.execute(
                            """
                            SELECT observed_at_ms, qualified, streak,
                                COALESCE(
                                    json_extract(payload_json, '$.qualification_version'),
                                    1
                                ) AS qualification_version
                            FROM cross_perp_observations
                            WHERE run_id = ?
                                AND hyperliquid_dex = ?
                                AND asset = ?
                                AND external_venue = ?
                                AND external_symbol = ?
                                AND direction = ?
                            """,
                            (
                                previous_run["id"],
                                payload["hyperliquid_dex"],
                                payload["asset"],
                                payload["external_venue"],
                                payload["external_symbol"],
                                payload["direction"],
                            ),
                        ).fetchone()
                    if (
                        previous_observation
                        and previous_observation["qualified"]
                        and int(previous_observation["qualification_version"])
                        == int(payload.get("qualification_version", 1))
                        and 0
                        <= int(payload["observed_at_ms"])
                        - int(previous_observation["observed_at_ms"])
                        <= continuity_window_ms
                    ):
                        streak = int(previous_observation["streak"]) + 1
                    else:
                        streak = 1
                observation_ready = streak >= 3
                payload["streak"] = streak
                payload["observation_ready"] = observation_ready
                connection.execute(
                    """
                    INSERT INTO cross_perp_observations (
                        run_id, observed_at_ms, hyperliquid_dex, asset,
                        external_venue, external_symbol, direction, qualified,
                        streak, observation_ready, net_apr_7d_pct, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        payload["observed_at_ms"],
                        payload["hyperliquid_dex"],
                        payload["asset"],
                        payload["external_venue"],
                        payload["external_symbol"],
                        payload["direction"],
                        int(qualified),
                        streak,
                        int(observation_ready),
                        payload["net_apr_7d_pct"],
                        json.dumps(payload, separators=(",", ":")),
                    ),
                )
                saved.append({"run_id": run_id, **payload})
        return saved

    def finish_cross_perp_run(
        self,
        run_id: int,
        *,
        status: str,
        venue_status: dict[str, object],
        match_count: int,
        evaluation_count: int,
        positive_net_count: int,
        ready_count: int,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE cross_perp_runs
                SET completed_at_ms = ?, status = ?, venue_status_json = ?,
                    match_count = ?, evaluation_count = ?, positive_net_count = ?,
                    ready_count = ?, error = ?
                WHERE id = ?
                """,
                (
                    int(time.time() * 1000),
                    status,
                    json.dumps(venue_status, separators=(",", ":")),
                    match_count,
                    evaluation_count,
                    positive_net_count,
                    ready_count,
                    error[:500] if error else None,
                    run_id,
                ),
            )

    def cross_perp_summary(self) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM cross_perp_runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return {
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
            observation_rows = connection.execute(
                """
                SELECT payload_json FROM cross_perp_observations
                WHERE run_id = ?
                """,
                (row["id"],),
            ).fetchall()
            current_stats = connection.execute(
                """
                SELECT COALESCE(MAX(streak), 0) AS max_streak,
                    MAX(COALESCE(
                        json_extract(payload_json, '$.qualification_version'), 1
                    )) AS qualification_version
                FROM cross_perp_observations
                WHERE run_id = ?
                """,
                (row["id"],),
            ).fetchone()
            qualification_version = current_stats["qualification_version"]
            last_ready = None
            if qualification_version is not None:
                last_ready = connection.execute(
                    """
                    SELECT o.run_id, MAX(o.observed_at_ms) AS observed_at_ms
                    FROM cross_perp_observations o
                    JOIN cross_perp_runs r ON r.id = o.run_id
                    WHERE o.observation_ready = 1 AND r.status = 'success'
                        AND COALESCE(
                            json_extract(
                                o.payload_json, '$.qualification_version'
                            ), 1
                        ) = ?
                    GROUP BY o.run_id
                    ORDER BY o.run_id DESC
                    LIMIT 1
                    """,
                    (qualification_version,),
                ).fetchone()
            last_ready_count = 0
            if last_ready:
                last_ready_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM cross_perp_observations
                        WHERE run_id = ? AND observation_ready = 1
                            AND COALESCE(json_extract(
                                payload_json, '$.qualification_version'
                            ), 1) = ?
                        """,
                        (last_ready["run_id"], qualification_version),
                    ).fetchone()[0]
                )
        result = dict(row)
        result["venue_status"] = json.loads(str(result.pop("venue_status_json")))
        result["max_streak"] = int(current_stats["max_streak"])
        result["last_ready_at_ms"] = (
            int(last_ready["observed_at_ms"]) if last_ready else None
        )
        result["last_ready_run_id"] = int(last_ready["run_id"]) if last_ready else None
        result["last_ready_count"] = last_ready_count
        rejection_counts: dict[str, int] = {}
        for observation in observation_rows:
            for reason in json.loads(observation["payload_json"]).get("reasons", []):
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        result["rejection_counts"] = dict(
            sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0]))
        )
        return result

    def latest_cross_perp_observations(
        self, limit: int = 100, *, observation_ready_only: bool = False
    ) -> list[dict[str, object]]:
        ready_clause = (
            "AND current.observation_ready = 1" if observation_ready_only else ""
        )
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                WITH latest_run AS (
                    SELECT id FROM cross_perp_runs
                    WHERE status = 'success' AND completed_at_ms IS NOT NULL
                        AND id = (SELECT MAX(id) FROM cross_perp_runs)
                ), latest_time AS (
                    SELECT MAX(observed_at_ms) AS observed_at_ms
                    FROM cross_perp_observations
                    WHERE run_id = (SELECT id FROM latest_run)
                ), route_stats AS (
                    SELECT o.hyperliquid_dex, o.asset, o.external_venue,
                        o.external_symbol, o.direction,
                        COALESCE(json_extract(
                            o.payload_json, '$.qualification_version'
                        ), 1) AS qualification_version,
                        MAX(o.streak) AS max_streak,
                        MAX(CASE WHEN o.observation_ready = 1
                            THEN o.observed_at_ms END) AS last_ready_at_ms,
                        SUM(CASE WHEN o.observed_at_ms >= lt.observed_at_ms - 86400000
                            AND o.qualified = 1 THEN 1 ELSE 0 END)
                            AS qualified_scans_24h,
                        SUM(CASE WHEN o.observed_at_ms >= lt.observed_at_ms - 86400000
                            THEN 1 ELSE 0 END) AS observed_scans_24h
                    FROM cross_perp_observations o
                    JOIN cross_perp_runs r ON r.id = o.run_id AND r.status = 'success'
                    CROSS JOIN latest_time lt
                    GROUP BY o.hyperliquid_dex, o.asset, o.external_venue,
                        o.external_symbol, o.direction, qualification_version
                )
                SELECT current.*, stats.max_streak, stats.last_ready_at_ms,
                    stats.qualified_scans_24h, stats.observed_scans_24h
                FROM cross_perp_observations current
                LEFT JOIN route_stats stats
                    ON stats.hyperliquid_dex = current.hyperliquid_dex
                    AND stats.asset = current.asset
                    AND stats.external_venue = current.external_venue
                    AND stats.external_symbol = current.external_symbol
                    AND stats.direction = current.direction
                    AND stats.qualification_version = COALESCE(json_extract(
                        current.payload_json, '$.qualification_version'
                    ), 1)
                WHERE current.run_id = (SELECT id FROM latest_run)
                {ready_clause}
                ORDER BY current.net_apr_7d_pct IS NULL,
                    current.net_apr_7d_pct DESC, current.rowid
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._cross_perp_row(row) for row in rows]

    def cross_perp_history(
        self, asset: str, external_venue: str, direction: str, limit: int = 100
    ) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM cross_perp_observations
                WHERE asset = ? AND external_venue = ? AND direction = ?
                ORDER BY observed_at_ms DESC, run_id DESC
                LIMIT ?
                """,
                (asset, external_venue, direction, limit),
            ).fetchall()
        return [self._cross_perp_row(row) for row in rows]

    def cross_perp_transitions(self, run_id: int) -> list[dict[str, object]]:
        """Return state changes for one completed run without mutating route state."""
        with self.connect() as connection:
            previous_run = connection.execute(
                """
                SELECT id FROM cross_perp_runs
                WHERE id < ? AND status = 'success'
                ORDER BY id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            current_rows = connection.execute(
                "SELECT * FROM cross_perp_observations WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            previous_rows = (
                connection.execute(
                    "SELECT * FROM cross_perp_observations WHERE run_id = ?",
                    (previous_run["id"],),
                ).fetchall()
                if previous_run
                else []
            )

        def key(row: sqlite3.Row) -> tuple[object, ...]:
            return (
                row["hyperliquid_dex"],
                row["asset"],
                row["external_venue"],
                row["external_symbol"],
                row["direction"],
            )

        previous = {key(row): row for row in previous_rows}
        current = {key(row): row for row in current_rows}
        events: list[dict[str, object]] = []
        economic_reasons = {
            "net_carry_non_positive",
            "net_profit_below_minimum",
            "cost_buffer_too_thin",
            "stress_net_negative",
        }
        for route, row in current.items():
            prior = previous.get(route)
            current_payload = json.loads(row["payload_json"])
            previous_payload = json.loads(prior["payload_json"]) if prior else {}
            current_reasons = set(current_payload.get("reasons", []))
            previous_reasons = set(previous_payload.get("reasons", []))
            event_types: list[str] = []
            if bool(row["observation_ready"]) and not bool(
                prior and prior["observation_ready"]
            ):
                event_types.append("became_ready")
            if prior and bool(prior["observation_ready"]) and not bool(
                row["observation_ready"]
            ):
                event_types.append("lost_ready")
            if (
                prior is not None
                and "insufficient_depth" in current_reasons
                and "insufficient_depth" not in previous_reasons
            ):
                event_types.append("depth_deteriorated")
            if prior and (current_reasons & economic_reasons) - previous_reasons:
                event_types.append("economics_deteriorated")
            for event_type in event_types:
                events.append(
                    {
                        "event_type": event_type,
                        "run_id": run_id,
                        "asset": row["asset"],
                        "external_venue": row["external_venue"],
                        "external_symbol": row["external_symbol"],
                        "direction": row["direction"],
                        "streak": int(row["streak"]),
                        "net_apr_7d_pct": row["net_apr_7d_pct"],
                        "reasons": sorted(current_reasons),
                    }
                )
        for route, prior in previous.items():
            if route in current or not bool(prior["observation_ready"]):
                continue
            events.append(
                {
                    "event_type": "lost_ready",
                    "run_id": run_id,
                    "asset": prior["asset"],
                    "external_venue": prior["external_venue"],
                    "external_symbol": prior["external_symbol"],
                    "direction": prior["direction"],
                    "streak": 0,
                    "net_apr_7d_pct": None,
                    "reasons": ["route_missing"],
                }
            )
        return events

    def save_cross_perp_entry_check(
        self,
        *,
        source_run_id: int,
        route: dict[str, object],
        status: str,
        reasons: list[str],
        payload: dict[str, object],
        checked_at_ms: int,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO cross_perp_entry_checks (
                    checked_at_ms, source_run_id, hyperliquid_dex, asset,
                    external_venue, external_symbol, direction, status,
                    reasons_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checked_at_ms,
                    source_run_id,
                    route["hyperliquid_dex"],
                    route["asset"],
                    route["external_venue"],
                    route["external_symbol"],
                    route["direction"],
                    status,
                    json.dumps(reasons, separators=(",", ":")),
                    json.dumps(payload, separators=(",", ":")),
                ),
            )
        return int(cursor.lastrowid)

    def latest_cross_perp_entry_checks(
        self, limit: int = 100
    ) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM cross_perp_entry_checks
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["reasons"] = json.loads(str(item.pop("reasons_json")))
            item["evidence"] = json.loads(str(item.pop("payload_json")))
            output.append(item)
        return output

    def update_cross_perp_entry_check(
        self,
        check_id: int,
        *,
        status: str,
        reasons: list[str],
        payload: dict[str, object],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE cross_perp_entry_checks
                SET status = ?, reasons_json = ?, payload_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(reasons, separators=(",", ":")),
                    json.dumps(payload, separators=(",", ":")),
                    check_id,
                ),
            )

    def cross_perp_execution_truth_streak(
        self, route: dict[str, object]
    ) -> int:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT source_run_id, payload_json
                FROM cross_perp_entry_checks
                WHERE hyperliquid_dex = ? AND asset = ?
                    AND external_venue = ? AND external_symbol = ?
                    AND direction = ?
                ORDER BY id DESC
                """,
                (
                    route["hyperliquid_dex"],
                    route["asset"],
                    route["external_venue"],
                    route["external_symbol"],
                    route["direction"],
                ),
            ).fetchall()
        streak = 0
        seen_runs: set[int] = set()
        for row in rows:
            source_run_id = int(row["source_run_id"])
            if source_run_id in seen_runs:
                continue
            seen_runs.add(source_run_id)
            payload = json.loads(str(row["payload_json"]))
            if not bool(payload.get("execution_truth_passed")):
                break
            streak += 1
        return streak

    def save_cross_perp_funding_forecasts(
        self,
        *,
        entry_check_id: int,
        source_run_id: int,
        route: dict[str, object],
        predicted_at_ms: int,
        payments: list[dict[str, object]],
    ) -> int:
        with self.connect() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT OR IGNORE INTO cross_perp_funding_forecasts (
                    entry_check_id, source_run_id, predicted_at_ms,
                    hyperliquid_dex, asset, external_venue, external_symbol,
                    direction, leg, settlement_at_ms, predicted_rate,
                    direction_sign, notional_usd, predicted_pnl_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        entry_check_id,
                        source_run_id,
                        predicted_at_ms,
                        route["hyperliquid_dex"],
                        route["asset"],
                        route["external_venue"],
                        route["external_symbol"],
                        route["direction"],
                        payment["leg"],
                        payment["settlement_at_ms"],
                        payment["predicted_rate"],
                        payment["direction_sign"],
                        payment["notional_usd"],
                        payment["predicted_pnl_usd"],
                    )
                    for payment in payments
                ],
            )
            return connection.total_changes - before

    def reconcile_cross_perp_funding_forecasts(
        self,
        route: dict[str, object],
        actual_events: list[tuple[str, int, float]],
        *,
        reconciled_at_ms: int,
        tolerance_ms: int = 5 * 60_000,
    ) -> int:
        events_by_leg: dict[str, list[tuple[int, float]]] = {}
        for leg, timestamp_ms, rate in actual_events:
            events_by_leg.setdefault(leg, []).append((timestamp_ms, rate))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, leg, settlement_at_ms, direction_sign, notional_usd
                FROM cross_perp_funding_forecasts
                WHERE hyperliquid_dex = ? AND asset = ?
                    AND external_venue = ? AND external_symbol = ?
                    AND direction = ? AND reconciled_at_ms IS NULL
                """,
                (
                    route["hyperliquid_dex"],
                    route["asset"],
                    route["external_venue"],
                    route["external_symbol"],
                    route["direction"],
                ),
            ).fetchall()
            updates: list[tuple[int, float, float, int, int]] = []
            for row in rows:
                candidates = events_by_leg.get(str(row["leg"]), [])
                if not candidates:
                    continue
                actual_timestamp, actual_rate = min(
                    candidates,
                    key=lambda event: abs(event[0] - int(row["settlement_at_ms"])),
                )
                if abs(actual_timestamp - int(row["settlement_at_ms"])) > tolerance_ms:
                    continue
                actual_pnl = (
                    float(row["notional_usd"])
                    * actual_rate
                    * int(row["direction_sign"])
                )
                updates.append(
                    (
                        actual_timestamp,
                        actual_rate,
                        actual_pnl,
                        reconciled_at_ms,
                        int(row["id"]),
                    )
                )
            connection.executemany(
                """
                UPDATE cross_perp_funding_forecasts
                SET actual_settlement_at_ms = ?, actual_rate = ?,
                    actual_pnl_usd = ?, reconciled_at_ms = ?
                WHERE id = ? AND reconciled_at_ms IS NULL
                """,
                updates,
            )
        return len(updates)

    def cross_perp_funding_forecast_accuracy(self) -> dict[str, object]:
        with self.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM cross_perp_funding_forecasts
                    ORDER BY predicted_at_ms DESC, id DESC
                    """
                ).fetchall()
            ]
        reconciled = [row for row in rows if row["reconciled_at_ms"] is not None]
        now_ms = int(time.time() * 1_000)

        def metrics(items: list[dict[str, object]]) -> dict[str, object]:
            predicted = sum(float(item["predicted_pnl_usd"]) for item in items)
            actual = sum(float(item["actual_pnl_usd"]) for item in items)
            directional = [
                item
                for item in items
                if abs(float(item["predicted_pnl_usd"])) > 1e-12
            ]
            sign_correct = sum(
                1
                for item in directional
                if float(item["predicted_pnl_usd"])
                * float(item["actual_pnl_usd"])
                > 0
            )
            return {
                "reconciled_predictions": len(items),
                "reconciled_directional_predictions": len(directional),
                "sign_accuracy_pct": (
                    sign_correct / len(directional) * 100 if directional else None
                ),
                "predicted_pnl_usd": predicted,
                "actual_pnl_usd": actual,
                "capture_ratio_pct": (
                    actual / predicted * 100 if abs(predicted) > 1e-9 else None
                ),
                "mean_absolute_error_usd": (
                    sum(
                        abs(
                            float(item["actual_pnl_usd"])
                            - float(item["predicted_pnl_usd"])
                        )
                        for item in items
                    )
                    / len(items)
                    if items
                    else None
                ),
            }

        grouped: dict[tuple[object, ...], list[dict[str, object]]] = {}
        for row in reconciled:
            key = (
                row["asset"],
                row["external_venue"],
                row["direction"],
                row["leg"],
            )
            grouped.setdefault(key, []).append(row)
        return {
            "total_predictions": len(rows),
            "pending_predictions": len(rows) - len(reconciled),
            "overdue_predictions": sum(
                1
                for row in rows
                if row["reconciled_at_ms"] is None
                and int(row["settlement_at_ms"]) + 5 * 60_000 < now_ms
            ),
            **metrics(reconciled),
            "by_route": [
                {
                    "asset": key[0],
                    "external_venue": key[1],
                    "direction": key[2],
                    "leg": key[3],
                    **metrics(items),
                }
                for key, items in sorted(grouped.items())
            ],
        }

    def open_cross_perp_paper_position(
        self,
        *,
        entry_check_id: int,
        source_run_id: int,
        evidence: dict[str, object],
        opened_at_ms: int,
    ) -> int:
        notional = float(evidence["notional_usd"])
        hyperliquid_price = float(evidence["hyperliquid_executable_price"])
        external_price = float(evidence["external_executable_price"])
        entry_fee = notional * (
            float(evidence["hyperliquid_fee_bps"])
            + float(evidence["external_fee_bps"])
        ) / 10_000
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO cross_perp_paper_positions (
                    entry_check_id, source_run_id, opened_at_ms,
                    hyperliquid_dex, asset, external_venue, external_symbol,
                    direction, notional_usd, hyperliquid_entry_price,
                    external_entry_price, hyperliquid_quantity,
                    external_quantity, entry_fee_usd, estimated_exit_fee_usd,
                    forecast_funding_usd, forecast_transaction_cost_usd,
                    forecast_net_profit_usd, forecast_net_apr_pct,
                    forecast_basis_bps, forward_funding_usd_24h,
                    forward_net_profit_usd_24h, committed_capital_usd,
                    readiness_level
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_check_id,
                    source_run_id,
                    opened_at_ms,
                    evidence["hyperliquid_dex"],
                    evidence["asset"],
                    evidence["external_venue"],
                    evidence["external_symbol"],
                    evidence["direction"],
                    notional,
                    hyperliquid_price,
                    external_price,
                    notional / hyperliquid_price,
                    notional / external_price,
                    entry_fee,
                    entry_fee,
                    evidence["expected_funding_usd"],
                    evidence["transaction_cost_usd"],
                    evidence["net_profit_usd"],
                    evidence["net_apr_7d_pct"],
                    evidence["basis_bps"],
                    evidence.get("forward_funding_usd_24h"),
                    evidence.get("forward_net_profit_usd_24h"),
                    evidence.get("committed_capital_usd"),
                    evidence.get("readiness_level"),
                ),
            )
        return int(cursor.lastrowid)

    def cross_perp_paper_positions(
        self, *, include_closed: bool = True
    ) -> list[dict[str, object]]:
        where = "" if include_closed else "WHERE p.closed_at_ms IS NULL"
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT p.*,
                    COALESCE((SELECT SUM(a.funding_pnl_usd)
                        FROM cross_perp_paper_accruals a
                        WHERE a.position_id = p.id), 0) AS funding_pnl_usd,
                    COALESCE((SELECT m.hyperliquid_pnl_usd + m.external_pnl_usd
                        FROM cross_perp_paper_marks m
                        WHERE m.position_id = p.id
                        ORDER BY m.timestamp_ms DESC LIMIT 1), 0) AS pair_mtm_usd
                FROM cross_perp_paper_positions p
                {where}
                ORDER BY p.opened_at_ms DESC
                """
            ).fetchall()
        return [self._cross_perp_paper_row(row) for row in rows]

    def open_cross_perp_paper_positions(self) -> list[dict[str, object]]:
        return self.cross_perp_paper_positions(include_closed=False)

    def save_cross_perp_paper_accruals(
        self,
        position_id: int,
        entries: list[tuple[str, int, float, float]],
    ) -> None:
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO cross_perp_paper_accruals (
                    position_id, leg, timestamp_ms, funding_rate, funding_pnl_usd
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (position_id, leg, timestamp_ms, rate, pnl)
                    for leg, timestamp_ms, rate, pnl in entries
                ],
            )

    def save_cross_perp_paper_mark(
        self,
        position_id: int,
        *,
        timestamp_ms: int,
        hyperliquid_exit_price: float,
        external_exit_price: float,
        hyperliquid_pnl_usd: float,
        external_pnl_usd: float,
        basis_bps: float,
        net_apr_7d_pct: float | None,
        qualified: bool,
        reasons: list[str],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO cross_perp_paper_marks VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    position_id,
                    timestamp_ms,
                    hyperliquid_exit_price,
                    external_exit_price,
                    hyperliquid_pnl_usd,
                    external_pnl_usd,
                    basis_bps,
                    net_apr_7d_pct,
                    int(qualified),
                    json.dumps(reasons, separators=(",", ":")),
                ),
            )

    def cross_perp_paper_failure_streak(self, position_id: int) -> int:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT qualified FROM cross_perp_paper_marks
                WHERE position_id = ? ORDER BY timestamp_ms DESC
                """,
                (position_id,),
            ).fetchall()
        streak = 0
        for row in rows:
            if bool(row["qualified"]):
                break
            streak += 1
        return streak

    def cross_perp_route_cooldown_until(
        self,
        route: dict[str, object],
        cooldown_ms: int,
    ) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT closed_at_ms FROM cross_perp_paper_positions
                WHERE hyperliquid_dex = ? AND asset = ?
                    AND external_venue = ? AND external_symbol = ?
                    AND direction = ? AND closed_at_ms IS NOT NULL
                ORDER BY closed_at_ms DESC LIMIT 1
                """,
                (
                    route["hyperliquid_dex"],
                    route["asset"],
                    route["external_venue"],
                    route["external_symbol"],
                    route["direction"],
                ),
            ).fetchone()
        if row is None:
            return None
        return int(row["closed_at_ms"]) + cooldown_ms

    def close_cross_perp_paper_position(
        self,
        position_id: int,
        *,
        closed_at_ms: int,
        reason: str,
        hyperliquid_exit_price: float,
        external_exit_price: float,
        exit_fee_usd: float,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE cross_perp_paper_positions
                SET closed_at_ms = ?, exit_reason = ?,
                    hyperliquid_exit_price = ?, external_exit_price = ?,
                    exit_fee_usd = ?
                WHERE id = ? AND closed_at_ms IS NULL
                """,
                (
                    closed_at_ms,
                    reason,
                    hyperliquid_exit_price,
                    external_exit_price,
                    exit_fee_usd,
                    position_id,
                ),
            )

    def cross_perp_paper_timeline(self, position_id: int) -> dict[str, object] | None:
        positions = [
            item
            for item in self.cross_perp_paper_positions()
            if int(item["id"]) == position_id
        ]
        if not positions:
            return None
        with self.connect() as connection:
            marks = connection.execute(
                """
                SELECT * FROM cross_perp_paper_marks
                WHERE position_id = ? ORDER BY timestamp_ms
                """,
                (position_id,),
            ).fetchall()
            accruals = connection.execute(
                """
                SELECT * FROM cross_perp_paper_accruals
                WHERE position_id = ? ORDER BY timestamp_ms, leg
                """,
                (position_id,),
            ).fetchall()
        return {
            "position": positions[0],
            "marks": [
                {
                    **dict(row),
                    "qualified": bool(row["qualified"]),
                    "reasons": json.loads(row["reasons_json"]),
                }
                for row in marks
            ],
            "funding_accruals": [dict(row) for row in accruals],
        }

    def cross_perp_paper_attribution(self) -> dict[str, object]:
        positions = self.cross_perp_paper_positions()
        closed = [item for item in positions if item["closed_at_ms"] is not None]
        return {
            "open_positions": len(positions) - len(closed),
            "closed_positions": len(closed),
            "realized_net_pnl_usd": sum(
                float(item["actual_net_pnl_usd"]) for item in closed
            ),
            "forecast_net_profit_usd": sum(
                float(item["forecast_net_profit_usd"]) for item in closed
            ),
            "forecast_error_usd": sum(
                float(item["forecast_error_usd"]) for item in closed
            ),
            "funding_variance_usd": sum(
                float(item["funding_variance_usd"]) for item in closed
            ),
            "basis_mtm_usd": sum(float(item["pair_mtm_usd"]) for item in closed),
            "cost_variance_usd": sum(
                float(item["cost_variance_usd"]) for item in closed
            ),
            "expected_funding_to_date_usd": sum(
                float(item["expected_funding_to_date_usd"]) for item in positions
            ),
            "funding_variance_to_date_usd": sum(
                float(item["funding_variance_to_date_usd"]) for item in positions
            ),
            "forecast_error_to_date_usd": sum(
                float(item["forecast_error_to_date_usd"]) for item in positions
            ),
            "positions": positions,
        }

    def cross_perp_execution_truth_summary(self) -> dict[str, object]:
        min_closed_positions = 20
        min_profitable_positions = 12
        min_reconciled_forecasts = 30
        min_forecast_sign_accuracy_pct = 60.0
        monitor = self.cross_perp_summary()
        checks = [
            item
            for item in self.latest_cross_perp_entry_checks(500)
            if item["source_run_id"] == monitor.get("id")
        ]
        latest_by_route: dict[tuple[object, ...], dict[str, object]] = {}
        for check in checks:
            route = (
                check["hyperliquid_dex"],
                check["asset"],
                check["external_venue"],
                check["external_symbol"],
                check["direction"],
            )
            latest_by_route.setdefault(route, check)
        latest = list(latest_by_route.values())
        paper_executable = [
            item
            for item in latest
            if item["status"] == "passed"
            and bool(item["evidence"].get("paper_executable"))
        ]
        closed = [
            item
            for item in self.cross_perp_paper_positions()
            if item["closed_at_ms"] is not None
        ]
        positive_closed = [
            item for item in closed if float(item["actual_net_pnl_usd"]) > 0
        ]
        realized_net = sum(float(item["actual_net_pnl_usd"]) for item in closed)
        forecast_accuracy = self.cross_perp_funding_forecast_accuracy()
        reconciled_forecasts = int(
            forecast_accuracy["reconciled_directional_predictions"]
        )
        sign_accuracy = forecast_accuracy["sign_accuracy_pct"]
        live_review_eligible = bool(
            len(closed) >= min_closed_positions
            and len(positive_closed) >= min_profitable_positions
            and realized_net > 0
            and reconciled_forecasts >= min_reconciled_forecasts
            and sign_accuracy is not None
            and float(sign_accuracy) >= min_forecast_sign_accuracy_pct
        )
        live_review_reasons: list[str] = []
        if len(closed) < min_closed_positions:
            live_review_reasons.append("fewer_than_20_closed_paper_positions")
        if len(positive_closed) < min_profitable_positions:
            live_review_reasons.append("fewer_than_12_profitable_paper_positions")
        if realized_net <= 0:
            live_review_reasons.append("aggregate_paper_pnl_non_positive")
        if reconciled_forecasts < min_reconciled_forecasts:
            live_review_reasons.append(
                "fewer_than_30_reconciled_directional_funding_forecasts"
            )
        if sign_accuracy is None or float(sign_accuracy) < min_forecast_sign_accuracy_pct:
            live_review_reasons.append("funding_forecast_sign_accuracy_below_60_pct")
        return {
            "monitoring_ready": int(monitor.get("ready_count", 0)),
            "preflight_checked": len(latest),
            "paper_executable": len(paper_executable),
            "live_review_eligible": live_review_eligible,
            "live_review_reasons": live_review_reasons,
            "closed_paper_positions": len(closed),
            "profitable_closed_positions": len(positive_closed),
            "funding_forecast_accuracy": forecast_accuracy,
            "requirements": {
                "observation_scans": 3,
                "execution_truth_scans": 3,
                "depth_multiple": 3,
                "forward_horizon_hours": 24,
                "delayed_leg_ms": [100, 250, 500],
                "closed_paper_positions_for_live_review": min_closed_positions,
                "positive_paper_positions_for_live_review": min_profitable_positions,
                "reconciled_directional_funding_forecasts_for_live_review": (
                    min_reconciled_forecasts
                ),
                "minimum_forecast_sign_accuracy_pct": min_forecast_sign_accuracy_pct,
            },
            "latest_checks": latest,
        }

    def start_scheduled_job(self, name: str, scheduled_slot: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scheduled_job_runs (
                    name, scheduled_slot, started_at_ms, status
                ) VALUES (?, ?, ?, 'running')
                """,
                (name, scheduled_slot, int(time.time() * 1000)),
            )
        return int(cursor.lastrowid)

    def finish_scheduled_job(
        self, job_run_id: int, *, exit_code: int, error: str | None = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE scheduled_job_runs
                SET completed_at_ms = ?, status = ?, exit_code = ?, error = ?
                WHERE id = ?
                """,
                (
                    int(time.time() * 1000),
                    "success" if exit_code == 0 else "failed",
                    exit_code,
                    error[:500] if error else None,
                    job_run_id,
                ),
            )

    def scheduled_job_runs(self, limit: int = 100) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM scheduled_job_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def scheduler_health(self, *, now_ms: int | None = None) -> dict[str, object]:
        current_ms = now_ms or int(time.time() * 1000)
        critical_jobs = {
            "scan",
            "shadow",
            "accrue",
            "update",
            "report",
            "heartbeat",
            "backup",
        }
        max_age_ms = {
            "scan": 2 * 3_600_000,
            "shadow": 2 * 3_600_000,
            "accrue": 2 * 3_600_000,
            "update": 2 * 3_600_000,
            "report": 26 * 3_600_000,
            "heartbeat": 26 * 3_600_000,
            "backup": 26 * 3_600_000,
        }
        with self.connect() as connection:
            rows = connection.execute(
                """
                WITH latest AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY name ORDER BY id DESC
                    ) AS recency_rank
                    FROM scheduled_job_runs
                )
                SELECT * FROM latest
                WHERE recency_rank = 1
                ORDER BY name
                """
            ).fetchall()
        unhealthy: list[str] = []
        latest: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            latest.append(item)
            name = str(row["name"])
            if name not in critical_jobs:
                continue
            if row["status"] != "success":
                unhealthy.append(f"{name}:{row['status']}")
                continue
            completed_at_ms = row["completed_at_ms"]
            if (
                completed_at_ms is not None
                and name in max_age_ms
                and current_ms - int(completed_at_ms) > max_age_ms[name]
            ):
                unhealthy.append(f"{name}:overdue")
        return {
            "healthy": not unhealthy,
            "unhealthy_jobs": unhealthy,
            "latest_jobs": latest,
        }

    def unhealthy_scheduled_jobs(self) -> list[str]:
        return list(self.scheduler_health()["unhealthy_jobs"])

    def database_health(self) -> dict[str, object]:
        minimum_free_bytes = int(
            os.getenv("FUNDING_ARB_MIN_FREE_BYTES", str(10 * 1024 * 1024))
        )
        try:
            with self.connect() as connection:
                integrity = str(
                    connection.execute("PRAGMA quick_check").fetchone()[0]
                )
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            free_bytes = shutil.disk_usage(self.path.parent).free
        except (OSError, sqlite3.Error) as exc:
            return {
                "healthy": False,
                "integrity": "unavailable",
                "writable": False,
                "free_bytes": 0,
                "error": str(exc)[:200],
            }
        return {
            "healthy": integrity == "ok" and free_bytes >= minimum_free_bytes,
            "integrity": integrity,
            "writable": True,
            "free_bytes": free_bytes,
        }

    def latest_market_snapshot(self, coin: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM market_snapshots
                WHERE coin = ?
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (coin,),
            ).fetchone()
        return dict(row) if row else None

    def save_paper_recommendation(self, recommendation: dict[str, object]) -> int | None:
        fields = (
            "created_at_ms",
            "expires_at_ms",
            "status",
            "coin",
            "candidate_analyzed_at",
            "venue",
            "hedge_symbol",
            "side",
            "notional_usd",
            "quantity",
            "perp_entry_price",
            "perp_bid_depth_usd",
            "perp_ask_depth_usd",
            "perp_spread_bps",
            "perp_quote_at_ms",
            "hedge_entry_price",
            "gross_apr_pct",
            "executable_net_apr_pct",
            "hedge_fee_bps",
            "hedge_spread_bps",
            "bid_depth_usd",
            "ask_depth_usd",
            "entry_cost_usd",
            "estimated_exit_cost_usd",
        )
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT OR IGNORE INTO paper_recommendations ({", ".join(fields)})
                VALUES ({", ".join("?" for _ in fields)})
                """,
                tuple(recommendation[field] for field in fields),
            )
        return int(cursor.lastrowid) if cursor.rowcount else None

    def paper_recommendations(self, status: str | None = None) -> list[dict[str, object]]:
        now_ms = int(time.time() * 1000)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE paper_recommendations
                SET status = 'expired'
                WHERE status = 'pending' AND expires_at_ms < ?
                """,
                (now_ms,),
            )
            if status:
                rows = connection.execute(
                    """
                    SELECT * FROM paper_recommendations
                    WHERE status = ?
                    ORDER BY created_at_ms DESC
                    """,
                    (status,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM paper_recommendations ORDER BY created_at_ms DESC LIMIT 100"
                ).fetchall()
        return [dict(row) for row in rows]

    def paper_recommendation(self, recommendation_id: int) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM paper_recommendations WHERE id = ?",
                (recommendation_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_recommendation_status(self, recommendation_id: int, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE paper_recommendations SET status = ? WHERE id = ?",
                (status, recommendation_id),
            )

    def approve_paper_recommendation(
        self,
        recommendation_id: int,
        *,
        max_open_positions: int,
        execution: dict[str, object],
        approval_mode: str = "manual",
    ) -> int:
        now_ms = int(time.time() * 1000)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recommendation = connection.execute(
                "SELECT * FROM paper_recommendations WHERE id = ?",
                (recommendation_id,),
            ).fetchone()
            if recommendation is None:
                raise ValueError("recommendation not found")
            if recommendation["status"] != "pending":
                raise ValueError(f"recommendation is {recommendation['status']}")
            if int(recommendation["expires_at_ms"]) < now_ms:
                connection.execute(
                    "UPDATE paper_recommendations SET status = 'expired' WHERE id = ?",
                    (recommendation_id,),
                )
                connection.commit()
                raise ValueError("recommendation expired; generate a fresh quote")
            connection.execute(
                """
                UPDATE paper_recommendations
                SET venue = ?, hedge_symbol = ?, quantity = ?,
                    perp_entry_price = ?, perp_bid_depth_usd = ?,
                    perp_ask_depth_usd = ?, perp_spread_bps = ?,
                    perp_quote_at_ms = ?, hedge_entry_price = ?,
                    gross_apr_pct = ?, executable_net_apr_pct = ?,
                    hedge_fee_bps = ?, hedge_spread_bps = ?,
                    bid_depth_usd = ?, ask_depth_usd = ?,
                    entry_cost_usd = ?, estimated_exit_cost_usd = ?
                WHERE id = ?
                """,
                (
                    execution["venue"],
                    execution["hedge_symbol"],
                    execution["quantity"],
                    execution["perp_entry_price"],
                    execution["perp_bid_depth_usd"],
                    execution["perp_ask_depth_usd"],
                    execution["perp_spread_bps"],
                    execution["perp_quote_at_ms"],
                    execution["hedge_entry_price"],
                    execution["gross_apr_pct"],
                    execution["executable_net_apr_pct"],
                    execution["hedge_fee_bps"],
                    execution["hedge_spread_bps"],
                    execution["bid_depth_usd"],
                    execution["ask_depth_usd"],
                    execution["entry_cost_usd"],
                    execution["estimated_exit_cost_usd"],
                    recommendation_id,
                ),
            )
            recommendation = connection.execute(
                "SELECT * FROM paper_recommendations WHERE id = ?",
                (recommendation_id,),
            ).fetchone()
            open_count = connection.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE closed_at_ms IS NULL"
            ).fetchone()[0]
            if open_count >= max_open_positions:
                raise ValueError("maximum open paper positions reached")
            coin = str(recommendation["coin"])
            if connection.execute(
                """
                SELECT 1 FROM paper_positions
                WHERE coin = ? AND closed_at_ms IS NULL
                """,
                (coin,),
            ).fetchone():
                raise ValueError(f"an open paper position already exists for {coin}")
            cursor = connection.execute(
                """
                INSERT INTO paper_positions (
                    coin, hedge_venue, side, notional_usd, entry_cost_usd,
                    hedge_assessment, notes, opened_at_ms, recommendation_id,
                    perp_entry_price, hedge_entry_price, quantity, hedge_symbol,
                    hedge_fee_bps, hedge_spread_bps, estimated_exit_cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    coin,
                    recommendation["venue"],
                    recommendation["side"],
                    recommendation["notional_usd"],
                    recommendation["entry_cost_usd"],
                    "exact_spot_market_matched_from_public_order_book",
                    (
                        "Auto-opened by the shadow paper scheduler."
                        if approval_mode == "shadow_auto"
                        else "Opened from an approval-gated public-order-book recommendation."
                    ),
                    now_ms,
                    recommendation_id,
                    recommendation["perp_entry_price"],
                    recommendation["hedge_entry_price"],
                    recommendation["quantity"],
                    recommendation["hedge_symbol"],
                    recommendation["hedge_fee_bps"],
                    recommendation["hedge_spread_bps"],
                    recommendation["estimated_exit_cost_usd"],
                ),
            )
            connection.execute(
                "UPDATE paper_recommendations SET status = 'approved' WHERE id = ?",
                (recommendation_id,),
            )
            return int(cursor.lastrowid)

    def save_paper_match_check(
        self,
        *,
        candidate_analyzed_at: str,
        coin: str,
        status: str,
        detail: str,
        hedge_venue: str | None = None,
        hedge_symbol: str | None = None,
        net_apr_7d_pct: float | None = None,
        net_apr_14d_pct: float | None = None,
        net_apr_30d_pct: float | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_match_checks (
                    candidate_analyzed_at, coin, checked_at_ms, status, detail,
                    hedge_venue, hedge_symbol, net_apr_7d_pct,
                    net_apr_14d_pct, net_apr_30d_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_analyzed_at, coin) DO UPDATE SET
                    checked_at_ms = excluded.checked_at_ms,
                    status = excluded.status,
                    detail = excluded.detail,
                    hedge_venue = excluded.hedge_venue,
                    hedge_symbol = excluded.hedge_symbol,
                    net_apr_7d_pct = excluded.net_apr_7d_pct,
                    net_apr_14d_pct = excluded.net_apr_14d_pct,
                    net_apr_30d_pct = excluded.net_apr_30d_pct
                """,
                (
                    candidate_analyzed_at,
                    coin,
                    int(time.time() * 1000),
                    status,
                    detail,
                    hedge_venue,
                    hedge_symbol,
                    net_apr_7d_pct,
                    net_apr_14d_pct,
                    net_apr_30d_pct,
                ),
            )

    def latest_paper_match_checks(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM paper_match_checks
                WHERE candidate_analyzed_at = (
                    SELECT MAX(candidate_analyzed_at) FROM paper_match_checks
                )
                ORDER BY coin
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_candidates(
        self, limit: int = 100, *, eligible_only: bool = False
    ) -> list[dict[str, object]]:
        eligible_clause = (
            "AND json_extract(payload_json, '$.eligible') = 1"
            if eligible_only
            else ""
        )
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                WITH ranked AS (
                    SELECT payload_json, scan_run_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY dex, coin
                            ORDER BY analyzed_at DESC
                        ) AS recency_rank
                    FROM candidates
                )
                SELECT payload_json, scan_run_id
                FROM ranked
                WHERE recency_rank = 1
                {eligible_clause}
                ORDER BY json_extract(payload_json, '$.realized_7d_apr_pct') DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return self._candidate_rows(rows)

    def latest_scan_candidates(
        self, limit: int = 100, *, eligible_only: bool = False
    ) -> list[dict[str, object]]:
        eligible_clause = (
            "AND json_extract(payload_json, '$.eligible') = 1"
            if eligible_only
            else ""
        )
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json, scan_run_id
                FROM candidates
                WHERE analyzed_at = (SELECT MAX(analyzed_at) FROM candidates)
                {eligible_clause}
                ORDER BY json_extract(payload_json, '$.realized_7d_apr_pct') DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return self._candidate_rows(rows)

    def monitoring_candidates(self, limit: int = 100) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, scan_run_id
                FROM candidates
                WHERE scan_run_id = (
                    SELECT id FROM scan_runs
                    WHERE status = 'success'
                    ORDER BY id DESC
                    LIMIT 1
                )
                AND json_extract(payload_json, '$.eligible') = 1
                ORDER BY json_extract(payload_json, '$.realized_7d_apr_pct') DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return self._candidate_rows(rows)

    def execution_ready_candidates(self, limit: int = 100) -> list[dict[str, object]]:
        candidates = self.execution_ranked_candidates(limit=500)
        ready = [
            candidate
            for candidate in candidates
            if candidate["execution_status"] == "pending_approval"
        ][:limit]
        for candidate in ready:
            candidate["actionable_now"] = True
        return ready

    def execution_ranked_candidates(self, limit: int = 100) -> list[dict[str, object]]:
        latest_run = self.latest_successful_scan_run()
        if latest_run is None:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.payload_json, c.scan_run_id,
                    m.status AS execution_status,
                    m.detail AS execution_detail,
                    m.hedge_venue,
                    m.hedge_symbol,
                    m.net_apr_7d_pct AS executable_net_apr_pct,
                    m.net_apr_14d_pct,
                    m.net_apr_30d_pct
                FROM candidates c
                LEFT JOIN paper_match_checks m
                    ON m.candidate_analyzed_at = c.analyzed_at
                    AND m.coin = c.coin
                WHERE c.scan_run_id = ?
                    AND json_extract(c.payload_json, '$.eligible') = 1
                ORDER BY m.net_apr_7d_pct IS NULL,
                    m.net_apr_7d_pct DESC,
                    json_extract(c.payload_json, '$.realized_7d_apr_pct') DESC
                LIMIT ?
                """,
                (latest_run["id"], limit),
            ).fetchall()
        candidates = self._candidate_rows(rows)
        for candidate, row in zip(candidates, rows):
            candidate.update(
                {
                    "execution_status": row["execution_status"] or "not_checked",
                    "execution_detail": row["execution_detail"],
                    "hedge_venue": row["hedge_venue"],
                    "hedge_symbol": row["hedge_symbol"],
                    "executable_net_apr_pct": row["executable_net_apr_pct"],
                    "net_apr_14d_pct": row["net_apr_14d_pct"],
                    "net_apr_30d_pct": row["net_apr_30d_pct"],
                }
            )
        return candidates

    def execution_funnel(self) -> dict[str, object]:
        latest_run = self.latest_successful_scan_run()
        if latest_run is None:
            return {
                "scan_id": None,
                "discovered": 0,
                "analyzed": 0,
                "monitoring_eligible": 0,
                "spot_matched": 0,
                "spot_depth_sufficient": 0,
                "profitable_after_costs": 0,
                "perp_executable": 0,
                "paper_opened": 0,
            }
        ranked = self.execution_ranked_candidates(limit=500)
        spot_matched = sum(
            item["hedge_venue"] is not None
            or item["execution_status"] == "spot_depth_below_5x_notional"
            for item in ranked
        )
        spot_depth_sufficient = sum(
            item["hedge_venue"] is not None for item in ranked
        )
        profitable_after_costs = sum(
            item["executable_net_apr_pct"] is not None
            and float(item["executable_net_apr_pct"]) >= 10
            for item in ranked
        )
        perp_executable = sum(
            item["execution_status"] == "pending_approval" for item in ranked
        )
        with self.connect() as connection:
            paper_opened = connection.execute(
                """
                SELECT COUNT(DISTINCT p.id)
                FROM paper_positions p
                JOIN paper_recommendations r ON r.id = p.recommendation_id
                JOIN candidates c
                    ON c.analyzed_at = r.candidate_analyzed_at
                    AND c.coin = r.coin
                WHERE c.scan_run_id = ?
                """,
                (latest_run["id"],),
            ).fetchone()[0]
        return {
            "scan_id": latest_run["id"],
            "discovered": latest_run["snapshot_count"],
            "analyzed": latest_run["candidate_count"],
            "monitoring_eligible": latest_run["eligible_count"],
            "spot_matched": spot_matched,
            "spot_depth_sufficient": spot_depth_sufficient,
            "profitable_after_costs": profitable_after_costs,
            "perp_executable": perp_executable,
            "paper_opened": paper_opened,
        }

    def rejection_analytics(self, days: int = 30) -> dict[str, object]:
        cutoff_ms = int((time.time() - days * 86_400) * 1000)
        cutoff_iso = datetime.fromtimestamp(
            cutoff_ms / 1000, timezone.utc
        ).isoformat()
        with self.connect() as connection:
            monitoring_rows = connection.execute(
                """
                SELECT reason.value AS reason, COUNT(*) AS count
                FROM candidates c, json_each(c.payload_json, '$.reasons') reason
                WHERE c.analyzed_at >= ?
                GROUP BY reason.value
                ORDER BY count DESC, reason.value
                """,
                (cutoff_iso,),
            ).fetchall()
            execution_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM paper_match_checks
                WHERE checked_at_ms >= ?
                    AND status NOT IN ('pending_approval', 'already_open')
                GROUP BY status
                ORDER BY count DESC, status
                """,
                (cutoff_ms,),
            ).fetchall()
            monitoring_daily = connection.execute(
                """
                SELECT substr(c.analyzed_at, 1, 10) AS day, COUNT(*) AS count
                FROM candidates c, json_each(c.payload_json, '$.reasons') reason
                WHERE c.analyzed_at >= ?
                GROUP BY day
                """,
                (cutoff_iso,),
            ).fetchall()
            execution_daily = connection.execute(
                """
                SELECT date(checked_at_ms / 1000, 'unixepoch') AS day,
                    COUNT(*) AS count
                FROM paper_match_checks
                WHERE checked_at_ms >= ?
                    AND status NOT IN ('pending_approval', 'already_open')
                GROUP BY day
                """,
                (cutoff_ms,),
            ).fetchall()
        daily_by_date: dict[str, dict[str, object]] = {}
        for row in monitoring_daily:
            daily_by_date[str(row["day"])] = {
                "date": str(row["day"]),
                "monitoring_rejections": int(row["count"]),
                "execution_checks": 0,
            }
        for row in execution_daily:
            day = str(row["day"])
            daily_by_date.setdefault(
                day,
                {
                    "date": day,
                    "monitoring_rejections": 0,
                    "execution_checks": 0,
                },
            )["execution_checks"] = int(row["count"])
        return {
            "window_days": days,
            "monitoring_reasons": {
                str(row["reason"]): int(row["count"]) for row in monitoring_rows
            },
            "execution_statuses": {
                str(row["status"]): int(row["count"]) for row in execution_rows
            },
            "daily": [
                daily_by_date[day] for day in sorted(daily_by_date, reverse=True)
            ],
        }

    def _candidate_rows(
        self, rows: list[sqlite3.Row]
    ) -> list[dict[str, object]]:
        latest_successful_run = self.latest_successful_scan_run()
        latest_successful_id = (
            int(latest_successful_run["id"]) if latest_successful_run else None
        )
        now = time.time()
        output: list[dict[str, object]] = []
        for row in rows:
            candidate = json.loads(row["payload_json"])
            scan_id = row["scan_run_id"]
            analyzed_at = datetime.fromisoformat(str(candidate["analyzed_at"])).timestamp()
            candidate["scan_id"] = scan_id
            candidate["analysis_age_seconds"] = max(0, int(now - analyzed_at))
            candidate["monitoring_current"] = bool(
                candidate.get("eligible")
                and scan_id is not None
                and int(scan_id) == latest_successful_id
            )
            candidate["actionable_now"] = False
            output.append(candidate)
        return output

    @staticmethod
    def _cross_perp_row(row: sqlite3.Row) -> dict[str, object]:
        result = json.loads(row["payload_json"])
        indexed_columns = (
            "run_id",
            "observed_at_ms",
            "hyperliquid_dex",
            "asset",
            "external_venue",
            "external_symbol",
            "direction",
            "streak",
            "net_apr_7d_pct",
        )
        result.update({column: row[column] for column in indexed_columns})
        result["qualification_version"] = int(
            result.get("qualification_version", 1)
        )
        result["qualified"] = bool(row["qualified"])
        result["observation_ready"] = bool(row["observation_ready"])
        for column in (
            "max_streak",
            "last_ready_at_ms",
            "qualified_scans_24h",
            "observed_scans_24h",
        ):
            if column in row.keys():
                result[column] = row[column]
        result["observation_age_seconds"] = max(
            0, int(time.time() - int(row["observed_at_ms"]) / 1000)
        )
        return result

    @staticmethod
    def _cross_perp_paper_row(row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        funding_pnl = float(result["funding_pnl_usd"])
        pair_mtm = float(result["pair_mtm_usd"])
        actual_cost = float(result["entry_fee_usd"]) + float(
            result["exit_fee_usd"]
            if result["exit_fee_usd"] is not None
            else result["estimated_exit_fee_usd"]
        )
        actual_net = funding_pnl + pair_mtm - actual_cost
        forecast_funding = float(result["forecast_funding_usd"])
        forecast_cost = float(result["forecast_transaction_cost_usd"])
        forecast_net = float(result["forecast_net_profit_usd"])
        closed = result["closed_at_ms"] is not None
        forward_funding = result.get("forward_funding_usd_24h")
        forecast_horizon_ms = 24 * 3_600_000 if forward_funding is not None else 7 * 86_400_000
        end_ms = int(result["closed_at_ms"] or time.time() * 1_000)
        elapsed_fraction = min(
            max(end_ms - int(result["opened_at_ms"]), 0) / forecast_horizon_ms,
            1.0,
        )
        expected_funding_to_date = float(
            forward_funding if forward_funding is not None else forecast_funding
        ) * elapsed_fraction
        expected_net_to_date = expected_funding_to_date - forecast_cost
        result.update(
            {
                "actual_cost_usd": actual_cost,
                "actual_net_pnl_usd": actual_net,
                "funding_variance_usd": (
                    funding_pnl - forecast_funding if closed else None
                ),
                "cost_variance_usd": forecast_cost - actual_cost if closed else None,
                "forecast_error_usd": actual_net - forecast_net if closed else None,
                "expected_funding_to_date_usd": expected_funding_to_date,
                "expected_net_to_date_usd": expected_net_to_date,
                "funding_variance_to_date_usd": funding_pnl
                - expected_funding_to_date,
                "forecast_error_to_date_usd": actual_net - expected_net_to_date,
            }
        )
        return result

    def latest_candidate(self, coin: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM candidates
                WHERE coin = ?
                ORDER BY analyzed_at DESC
                LIMIT 1
                """,
                (coin,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def latest_funding_points(
        self, coin: str, limit: int = 3, *, start_time_ms: int | None = None
    ) -> list[FundingPoint]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT coin, timestamp_ms, funding_rate FROM funding_points
                WHERE coin = ? AND (? IS NULL OR timestamp_ms >= ?)
                ORDER BY timestamp_ms DESC
                LIMIT ?
                """,
                (coin, start_time_ms, start_time_ms, limit),
            ).fetchall()
        return [
            FundingPoint(
                coin=str(row["coin"]),
                timestamp_ms=int(row["timestamp_ms"]),
                funding_rate=float(row["funding_rate"]),
            )
            for row in rows
        ]

    def open_paper_position(
        self,
        *,
        coin: str,
        hedge_venue: str,
        side: str,
        notional_usd: float,
        entry_cost_usd: float,
        hedge_assessment: str,
        notes: str,
        recommendation_id: int | None = None,
        perp_entry_price: float | None = None,
        hedge_entry_price: float | None = None,
        quantity: float | None = None,
        hedge_symbol: str | None = None,
        hedge_fee_bps: float | None = None,
        hedge_spread_bps: float | None = None,
        estimated_exit_cost_usd: float = 0,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO paper_positions (
                    coin, hedge_venue, side, notional_usd, entry_cost_usd,
                    hedge_assessment, notes, opened_at_ms, recommendation_id,
                    perp_entry_price, hedge_entry_price, quantity, hedge_symbol,
                    hedge_fee_bps, hedge_spread_bps, estimated_exit_cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    coin,
                    hedge_venue,
                    side,
                    notional_usd,
                    entry_cost_usd,
                    hedge_assessment,
                    notes,
                    int(time.time() * 1000),
                    recommendation_id,
                    perp_entry_price,
                    hedge_entry_price,
                    quantity,
                    hedge_symbol,
                    hedge_fee_bps,
                    hedge_spread_bps,
                    estimated_exit_cost_usd,
                ),
            )
        return int(cursor.lastrowid)

    def paper_position(self, position_id: int) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT p.*,
                    COALESCE((
                        SELECT SUM(a.funding_pnl_usd) FROM paper_accruals a
                        WHERE a.position_id = p.id
                    ), 0) AS funding_pnl_usd,
                    COALESCE((
                        SELECT m.perp_pnl_usd + m.hedge_pnl_usd
                        FROM paper_position_marks m
                        WHERE m.position_id = p.id
                        ORDER BY m.timestamp_ms DESC LIMIT 1
                    ), 0) AS mark_to_market_pnl_usd,
                    (
                        SELECT m.hedge_drift_pct FROM paper_position_marks m
                        WHERE m.position_id = p.id
                        ORDER BY m.timestamp_ms DESC LIMIT 1
                    ) AS hedge_drift_pct,
                    (
                        SELECT l.degraded FROM paper_liquidity_checks l
                        WHERE l.position_id = p.id
                        ORDER BY l.timestamp_ms DESC LIMIT 1
                    ) AS liquidity_degraded,
                    (
                        SELECT l.reasons FROM paper_liquidity_checks l
                        WHERE l.position_id = p.id
                        ORDER BY l.timestamp_ms DESC LIMIT 1
                    ) AS liquidity_reasons
                FROM paper_positions p
                WHERE p.id = ?
                """,
                (position_id,),
            ).fetchone()
        return self._paper_row(row) if row else None

    def paper_positions(self, *, include_closed: bool = True) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT p.*,
                    COALESCE((
                        SELECT SUM(a.funding_pnl_usd) FROM paper_accruals a
                        WHERE a.position_id = p.id
                    ), 0) AS funding_pnl_usd,
                    COALESCE((
                        SELECT m.perp_pnl_usd + m.hedge_pnl_usd
                        FROM paper_position_marks m
                        WHERE m.position_id = p.id
                        ORDER BY m.timestamp_ms DESC LIMIT 1
                    ), 0) AS mark_to_market_pnl_usd,
                    (
                        SELECT m.hedge_drift_pct FROM paper_position_marks m
                        WHERE m.position_id = p.id
                        ORDER BY m.timestamp_ms DESC LIMIT 1
                    ) AS hedge_drift_pct,
                    (
                        SELECT l.degraded FROM paper_liquidity_checks l
                        WHERE l.position_id = p.id
                        ORDER BY l.timestamp_ms DESC LIMIT 1
                    ) AS liquidity_degraded,
                    (
                        SELECT l.reasons FROM paper_liquidity_checks l
                        WHERE l.position_id = p.id
                        ORDER BY l.timestamp_ms DESC LIMIT 1
                    ) AS liquidity_reasons
                FROM paper_positions p
                {"" if include_closed else "WHERE p.closed_at_ms IS NULL"}
                ORDER BY p.opened_at_ms DESC
                """
            ).fetchall()
        return [self._paper_row(row) for row in rows]

    def open_paper_positions(self) -> list[dict[str, object]]:
        return self.paper_positions(include_closed=False)

    def has_open_paper_position(self, coin: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM paper_positions
                WHERE coin = ? AND closed_at_ms IS NULL
                LIMIT 1
                """,
                (coin,),
            ).fetchone()
        return row is not None

    def save_paper_accruals(self, position_id: int, entries: list[tuple[int, float]]) -> None:
        with self.connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO paper_accruals VALUES (?, ?, ?)",
                [(position_id, timestamp, pnl) for timestamp, pnl in entries],
            )

    def latest_paper_accrual_timestamp(self, position_id: int) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(timestamp_ms) AS timestamp_ms FROM paper_accruals WHERE position_id = ?",
                (position_id,),
            ).fetchone()
        return int(row["timestamp_ms"]) if row and row["timestamp_ms"] is not None else None

    def save_paper_mark(
        self,
        position_id: int,
        *,
        timestamp_ms: int,
        perp_price: float,
        hedge_price: float,
        perp_pnl_usd: float,
        hedge_pnl_usd: float,
        hedge_drift_pct: float,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO paper_position_marks
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    timestamp_ms,
                    perp_price,
                    hedge_price,
                    perp_pnl_usd,
                    hedge_pnl_usd,
                    hedge_drift_pct,
                ),
            )

    def save_paper_liquidity_check(
        self,
        position_id: int,
        *,
        timestamp_ms: int,
        day_volume_usd: float,
        bid_depth_usd: float,
        ask_depth_usd: float,
        reasons: list[str],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO paper_liquidity_checks
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    timestamp_ms,
                    day_volume_usd,
                    bid_depth_usd,
                    ask_depth_usd,
                    int(bool(reasons)),
                    ",".join(reasons),
                ),
            )

    def liquidity_degradation_streak(self, position_id: int, limit: int = 3) -> int:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp_ms, degraded
                FROM paper_liquidity_checks
                WHERE position_id = ?
                ORDER BY timestamp_ms DESC
                LIMIT ?
                """,
                (position_id, limit),
            ).fetchall()
        streak = 0
        previous_timestamp: int | None = None
        for row in rows:
            timestamp = int(row["timestamp_ms"])
            if not bool(row["degraded"]):
                break
            if (
                previous_timestamp is not None
                and not 45 * 60_000 <= previous_timestamp - timestamp <= 75 * 60_000
            ):
                break
            streak += 1
            previous_timestamp = timestamp
        return streak

    def close_paper_position(
        self,
        position_id: int,
        *,
        reason: str,
        exit_cost_usd: float,
        executed_at_ms: int | None = None,
        perp_exit_price: float | None = None,
        hedge_exit_price: float | None = None,
        exit_quantity: float | None = None,
        exit_hedge_spread_bps: float | None = None,
        exit_bid_depth_usd: float | None = None,
        exit_ask_depth_usd: float | None = None,
    ) -> None:
        closed_at_ms = executed_at_ms or int(time.time() * 1000)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE paper_positions
                SET closed_at_ms = ?, exit_reason = ?, exit_cost_usd = ?,
                    perp_exit_price = ?, hedge_exit_price = ?, exit_quantity = ?,
                    exit_hedge_spread_bps = ?, exit_bid_depth_usd = ?,
                    exit_ask_depth_usd = ?, exit_executed_at_ms = ?
                WHERE id = ? AND closed_at_ms IS NULL
                """,
                (
                    closed_at_ms,
                    reason,
                    exit_cost_usd,
                    perp_exit_price,
                    hedge_exit_price,
                    exit_quantity,
                    exit_hedge_spread_bps,
                    exit_bid_depth_usd,
                    exit_ask_depth_usd,
                    closed_at_ms,
                    position_id,
                ),
            )

    def save_alert_delivery(
        self,
        *,
        event_type: str,
        status: str,
        attempts: int,
        detail: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO alert_deliveries (
                    event_type, attempted_at_ms, status, attempts, detail
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    int(time.time() * 1000),
                    status,
                    attempts,
                    detail[:500] if detail else None,
                ),
            )

    def alert_deliveries(self, limit: int = 50) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, attempted_at_ms, status, attempts, detail
                FROM alert_deliveries
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def paper_position_timeline(self, position_id: int) -> dict[str, object] | None:
        position = self.paper_position(position_id)
        if position is None:
            return None
        with self.connect() as connection:
            marks = connection.execute(
                """
                SELECT timestamp_ms, perp_price, hedge_price, perp_pnl_usd,
                    hedge_pnl_usd, hedge_drift_pct
                FROM paper_position_marks
                WHERE position_id = ?
                ORDER BY timestamp_ms
                """,
                (position_id,),
            ).fetchall()
            accruals = connection.execute(
                """
                SELECT timestamp_ms, funding_pnl_usd
                FROM paper_accruals
                WHERE position_id = ?
                ORDER BY timestamp_ms
                """,
                (position_id,),
            ).fetchall()

        marks_by_time = {int(row["timestamp_ms"]): row for row in marks}
        funding_by_time = {
            int(row["timestamp_ms"]): float(row["funding_pnl_usd"]) for row in accruals
        }
        timestamps = {
            int(position["opened_at_ms"]),
            *marks_by_time,
            *funding_by_time,
        }
        if position["closed_at_ms"] is not None:
            timestamps.add(int(position["closed_at_ms"]))

        cumulative_funding = 0.0
        pair_mtm = 0.0
        basis_pct: float | None = None
        drift_pct: float | None = None
        points: list[dict[str, object]] = []
        for timestamp_ms in sorted(timestamps):
            cumulative_funding += funding_by_time.get(timestamp_ms, 0)
            mark = marks_by_time.get(timestamp_ms)
            if mark is not None:
                pair_mtm = float(mark["perp_pnl_usd"]) + float(mark["hedge_pnl_usd"])
                midpoint = (float(mark["perp_price"]) + float(mark["hedge_price"])) / 2
                basis_pct = (
                    (float(mark["perp_price"]) - float(mark["hedge_price"]))
                    / midpoint
                    * 100
                    if midpoint
                    else None
                )
                drift_pct = float(mark["hedge_drift_pct"])
            end_ms = min(
                timestamp_ms,
                int(position["closed_at_ms"] or timestamp_ms),
            )
            financing_cost = (
                float(position["notional_usd"])
                * CostAssumptions().annual_borrow_pct
                / 100
                * max(0, end_ms - int(position["opened_at_ms"]))
                / (365 * 86_400_000)
            )
            exit_cost = (
                float(position["exit_cost_usd"])
                if position["closed_at_ms"] is not None
                and timestamp_ms >= int(position["closed_at_ms"])
                and position["exit_cost_usd"] is not None
                else float(position["estimated_exit_cost_usd"] or 0)
            )
            points.append(
                {
                    "timestamp_ms": timestamp_ms,
                    "funding_pnl_usd": cumulative_funding,
                    "pair_mtm_usd": pair_mtm,
                    "financing_cost_usd": financing_cost,
                    "net_pnl_usd": (
                        cumulative_funding
                        + pair_mtm
                        - float(position["entry_cost_usd"])
                        - exit_cost
                        - financing_cost
                    ),
                    "basis_pct": basis_pct,
                    "hedge_drift_pct": drift_pct,
                }
            )
        return {"position": position, "points": points}

    def paper_summary(self) -> dict[str, object]:
        positions = self.paper_positions()
        open_positions = [item for item in positions if item["closed_at_ms"] is None]
        return {
            "open_positions": len(open_positions),
            "closed_positions": len(positions) - len(open_positions),
            "notional_usd": sum(float(item["notional_usd"]) for item in open_positions),
            "funding_pnl_usd": sum(float(item["funding_pnl_usd"]) for item in positions),
            "mark_to_market_pnl_usd": sum(
                float(item["mark_to_market_pnl_usd"]) for item in positions
            ),
            "estimated_entry_cost_usd": sum(float(item["entry_cost_usd"]) for item in positions),
            "financing_cost_usd": sum(float(item["financing_cost_usd"]) for item in positions),
            "net_pnl_usd": sum(float(item["net_pnl_usd"]) for item in positions),
        }

    def paper_performance(self) -> dict[str, object]:
        positions = self.paper_positions()
        closed = sorted(
            (item for item in positions if item["closed_at_ms"] is not None),
            key=lambda item: int(item["closed_at_ms"]),
        )
        open_positions = [item for item in positions if item["closed_at_ms"] is None]
        wins = sum(float(item["net_pnl_usd"]) > 0 for item in closed)
        cumulative_pnl = 0.0
        peak_pnl = 0.0
        max_drawdown = 0.0
        for item in closed:
            cumulative_pnl += float(item["net_pnl_usd"])
            peak_pnl = max(peak_pnl, cumulative_pnl)
            max_drawdown = max(max_drawdown, peak_pnl - cumulative_pnl)
        exit_reasons: dict[str, int] = {}
        for item in closed:
            reason = str(item.get("exit_reason") or "unspecified")
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        holding_hours = [
            (int(item["closed_at_ms"]) - int(item["opened_at_ms"])) / 3_600_000
            for item in closed
        ]
        now_ms = int(time.time() * 1000)
        observation_weeks = (
            (
                max(int(item["closed_at_ms"] or now_ms) for item in positions)
                - min(int(item["opened_at_ms"]) for item in positions)
            )
            / (7 * 86_400_000)
            if positions
            else 0
        )
        graduation = {
            "required_closed_trades": 30,
            "required_observation_weeks": 4,
            "closed_trades": len(closed),
            "observation_weeks": observation_weeks,
            "trade_progress_pct": min(len(closed) / 30 * 100, 100),
            "time_progress_pct": min(observation_weeks / 4 * 100, 100),
            "eligible_for_live_review": len(closed) >= 30 and observation_weeks >= 4,
        }
        return {
            "completed_trades": len(closed),
            "winning_trades": wins,
            "win_rate_pct": wins / len(closed) * 100 if closed else None,
            "realized_net_pnl_usd": sum(float(item["net_pnl_usd"]) for item in closed),
            "average_net_pnl_usd": (
                sum(float(item["net_pnl_usd"]) for item in closed) / len(closed)
                if closed
                else None
            ),
            "max_drawdown_usd": max_drawdown,
            "average_holding_hours": (
                sum(holding_hours) / len(holding_hours) if holding_hours else None
            ),
            "open_positions": len(open_positions),
            "open_net_pnl_usd": sum(
                float(item["net_pnl_usd"]) for item in open_positions
            ),
            "exit_reasons": exit_reasons,
            "graduation": graduation,
        }

    def paper_strategy_analytics(self) -> dict[str, object]:
        positions = {
            int(item["id"]): item
            for item in self.paper_positions()
            if item["closed_at_ms"] is not None
        }
        if not positions:
            return {
                "total_closed_trades": 0,
                "by_coin": [],
                "by_venue": [],
                "by_holding_period": [],
                "by_entry_net_apr": [],
                "by_market_regime": [],
                "by_exit_reason": [],
            }
        placeholders = ", ".join("?" for _ in positions)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT p.id, r.gross_apr_pct, r.executable_net_apr_pct
                FROM paper_positions p
                LEFT JOIN paper_recommendations r ON r.id = p.recommendation_id
                WHERE p.id IN ({placeholders})
                """,
                list(positions),
            ).fetchall()
        entry_by_position = {int(row["id"]): dict(row) for row in rows}
        evidence: list[dict[str, object]] = []
        for position_id, position in positions.items():
            entry = entry_by_position.get(position_id, {})
            holding_hours = (
                int(position["closed_at_ms"]) - int(position["opened_at_ms"])
            ) / 3_600_000
            net_apr = entry.get("executable_net_apr_pct")
            gross_apr = entry.get("gross_apr_pct")
            evidence.append(
                {
                    **position,
                    "holding_hours": holding_hours,
                    "entry_net_apr_bucket": self._entry_net_apr_bucket(net_apr),
                    "market_regime": self._market_regime(gross_apr),
                    "holding_period": self._holding_period(holding_hours),
                }
            )

        def summarize(field: str) -> list[dict[str, object]]:
            groups: dict[str, list[dict[str, object]]] = {}
            for item in evidence:
                name = str(item.get(field) or "unknown")
                groups.setdefault(name, []).append(item)
            output: list[dict[str, object]] = []
            for name, items in groups.items():
                pnl = sum(float(item["net_pnl_usd"]) for item in items)
                wins = sum(float(item["net_pnl_usd"]) > 0 for item in items)
                output.append(
                    {
                        "name": name,
                        "trades": len(items),
                        "wins": wins,
                        "win_rate_pct": wins / len(items) * 100,
                        "net_pnl_usd": pnl,
                        "average_net_pnl_usd": pnl / len(items),
                        "average_holding_hours": sum(
                            float(item["holding_hours"]) for item in items
                        )
                        / len(items),
                    }
                )
            return sorted(
                output,
                key=lambda item: (-float(item["net_pnl_usd"]), str(item["name"])),
            )

        return {
            "total_closed_trades": len(evidence),
            "by_coin": summarize("coin"),
            "by_venue": summarize("hedge_venue"),
            "by_holding_period": summarize("holding_period"),
            "by_entry_net_apr": summarize("entry_net_apr_bucket"),
            "by_market_regime": summarize("market_regime"),
            "by_exit_reason": summarize("exit_reason"),
        }

    @staticmethod
    def _holding_period(hours: float) -> str:
        if hours < 24:
            return "under_24h"
        if hours < 72:
            return "1_to_3d"
        if hours < 168:
            return "3_to_7d"
        return "7d_plus"

    @staticmethod
    def _entry_net_apr_bucket(value: object) -> str:
        if value is None:
            return "unknown"
        apr = float(value)
        if apr < 10:
            return "under_10pct"
        if apr < 25:
            return "10_to_25pct"
        if apr < 50:
            return "25_to_50pct"
        return "50pct_plus"

    @staticmethod
    def _market_regime(value: object) -> str:
        if value is None:
            return "unknown"
        apr = float(value)
        if apr < 30:
            return "moderate_carry"
        if apr < 60:
            return "high_carry"
        return "extreme_carry"

    @staticmethod
    def _paper_row(row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        end_ms = result.get("closed_at_ms") or int(time.time() * 1000)
        elapsed_years = max(0, end_ms - result["opened_at_ms"]) / (365 * 86_400_000)
        result["financing_cost_usd"] = (
            result["notional_usd"]
            * CostAssumptions().annual_borrow_pct
            / 100
            * elapsed_years
        )
        exit_cost = (
            result["exit_cost_usd"]
            if result.get("closed_at_ms") and result.get("exit_cost_usd") is not None
            else result.get("estimated_exit_cost_usd", 0)
        )
        result["net_pnl_usd"] = (
            result["funding_pnl_usd"]
            + result.get("mark_to_market_pnl_usd", 0)
            - result["entry_cost_usd"]
            - exit_cost
            - result["financing_cost_usd"]
        )
        result["pnl_status"] = (
            "simulated_realized" if result.get("closed_at_ms") else "estimated_after_exit"
        )
        result["exit_execution_complete"] = bool(
            result.get("closed_at_ms")
            and result.get("perp_exit_price") is not None
            and result.get("hedge_exit_price") is not None
            and result.get("exit_executed_at_ms") is not None
        )
        return result
