from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .costs import CostAssumptions
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
            self._ensure_column(connection, "paper_match_checks", "hedge_venue", "TEXT")
            self._ensure_column(connection, "paper_match_checks", "hedge_symbol", "TEXT")
            self._ensure_column(connection, "paper_match_checks", "net_apr_7d_pct", "REAL")
            self._ensure_column(connection, "paper_match_checks", "net_apr_14d_pct", "REAL")
            self._ensure_column(connection, "paper_match_checks", "net_apr_30d_pct", "REAL")

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
                    perp_entry_price = ?, hedge_entry_price = ?,
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
                    ) AS hedge_drift_pct
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
                    ) AS hedge_drift_pct
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

    def close_paper_position(self, position_id: int, *, reason: str, exit_cost_usd: float) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE paper_positions
                SET closed_at_ms = ?, exit_reason = ?, exit_cost_usd = ?
                WHERE id = ? AND closed_at_ms IS NULL
                """,
                (int(time.time() * 1000), reason, exit_cost_usd, position_id),
            )

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
        }

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
        return result
