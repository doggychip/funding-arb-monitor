# Cross-Perpetual Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build read-only hourly monitoring of funding-rate spreads between Hyperliquid and exact Binance or OKX perpetual matches, with executable cost estimates and three-scan observation qualification.

**Architecture:** Add public venue adapters in `cross_perp_venues.py` and keep funding normalization, executable carry evaluation, and orchestration in `cross_perp.py`. Persist immutable run and observation history through additive SQLite tables, then expose the latest results through protected APIs and a dashboard section without changing existing spot-perp or paper-trading semantics.

**Tech Stack:** Python 3.11+, standard-library `urllib`, SQLite via `sqlite3`, FastAPI, vanilla HTML/CSS/JavaScript, pytest.

## Global Constraints

- Use only public, unauthenticated exchange endpoints; never accept exchange credentials or place orders.
- Monitor Binance and OKX exact underlying matches against Hyperliquid.
- Evaluate both `short_hyperliquid_long_external` and `long_hyperliquid_short_external`.
- Use a fixed `$1,000` observation notional and seven-day realized funding history.
- Require sufficient history, fresh data, complete visible depth, absolute basis at most `100` bps, and positive seven-day net carry.
- Include entry and exit taker fees plus measured entry slippage and an equal exit-slippage allowance.
- Require three consecutive qualifying successful hourly runs for `observation_ready`.
- Preserve every evaluated or unavailable route with explicit reasons.
- Cross-perp degradation must not make `/readyz` fail.
- Do not alter existing candidate eligibility, paper positions, or automatic paper workflows.
- Do not add dependencies.

## File Structure

- Create `src/funding_arb_monitor/cross_perp_venues.py`: public Binance and OKX perpetual discovery, funding, mark-price, and order-book adapters.
- Create `src/funding_arb_monitor/cross_perp.py`: domain models, funding normalization, cost evaluation, qualification, and multi-venue monitor orchestration.
- Modify `src/funding_arb_monitor/store.py`: additive run/observation tables, streak persistence, summaries, ranking, and history queries.
- Modify `src/funding_arb_monitor/cli.py`: add the read-only `cross-perp` command.
- Modify `src/funding_arb_monitor/scheduler.py`: schedule cross-perp monitoring hourly after the main scan.
- Modify `src/funding_arb_monitor/api.py`: expose three protected GET endpoints.
- Modify `src/funding_arb_monitor/static/index.html`: show cross-perp status and ranked observations.
- Modify `README.md`: document the command, qualification semantics, and APIs.
- Create `tests/test_cross_perp_venues.py`: venue payload parsing, pagination, and depth behavior.
- Create `tests/test_cross_perp.py`: normalization, evaluation, qualification, and failure isolation.
- Modify `tests/test_scheduler.py`, `tests/test_api.py`: integration coverage.

---

### Task 1: Public Binance and OKX Perpetual Clients

**Files:**
- Create: `src/funding_arb_monitor/cross_perp_venues.py`
- Create: `tests/test_cross_perp_venues.py`

**Interfaces:**
- Produces: `PerpInstrument`, `PerpFundingEvent`, `PerpBookQuote`, `ExternalPerpMarket`, `ExternalPerpVenue`, `BinancePerpVenue`, and `OkxPerpVenue`.
- Produces: `ExternalPerpVenue.instruments() -> dict[str, PerpInstrument]`.
- Produces: `ExternalPerpVenue.market(instrument: PerpInstrument, *, days: int, notional_usd: float) -> ExternalPerpMarket`.
- Consumes: `PublicJsonClient` from `funding_arb_monitor.venues`.

- [ ] **Step 1: Write failing instrument-discovery tests**

```python
from funding_arb_monitor.cross_perp_venues import BinancePerpVenue, OkxPerpVenue


def test_binance_lists_only_live_linear_perpetuals() -> None:
    payload = {
        "symbols": [
            {
                "symbol": "ZROUSDT",
                "baseAsset": "ZRO",
                "quoteAsset": "USDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
            },
            {
                "symbol": "ZROUSDT_260925",
                "baseAsset": "ZRO",
                "quoteAsset": "USDT",
                "contractType": "CURRENT_QUARTER",
                "status": "TRADING",
            },
        ]
    }
    venue = BinancePerpVenue(get_json=lambda _: payload)
    assert venue.instruments()["ZRO"].symbol == "ZROUSDT"


def test_okx_prefers_live_usdt_linear_swap() -> None:
    payload = {
        "data": [
            {
                "instId": "ZRO-USDC-SWAP",
                "uly": "ZRO-USDC",
                "settleCcy": "USDC",
                "ctType": "linear",
                "state": "live",
            },
            {
                "instId": "ZRO-USDT-SWAP",
                "uly": "ZRO-USDT",
                "settleCcy": "USDT",
                "ctType": "linear",
                "state": "live",
            },
        ]
    }
    venue = OkxPerpVenue(get_json=lambda _: payload)
    assert venue.instruments()["ZRO"].symbol == "ZRO-USDT-SWAP"
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run: `.venv/bin/pytest tests/test_cross_perp_venues.py -v`

Expected: collection fails with `ModuleNotFoundError: funding_arb_monitor.cross_perp_venues`.

- [ ] **Step 3: Add immutable public-venue models and instrument discovery**

```python
@dataclass(frozen=True)
class PerpInstrument:
    venue: str
    asset: str
    symbol: str


@dataclass(frozen=True)
class PerpFundingEvent:
    timestamp_ms: int
    funding_rate: float


@dataclass(frozen=True)
class PerpBookQuote:
    venue: str
    asset: str
    symbol: str
    bid: float
    ask: float
    executable_buy_price: float | None
    executable_sell_price: float | None
    bid_depth_usd: float
    ask_depth_usd: float
    fee_bps: float
    captured_at_ms: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class ExternalPerpMarket:
    instrument: PerpInstrument
    current_funding_rate: float
    mark_price: float
    funding_captured_at_ms: int
    funding_events: tuple[PerpFundingEvent, ...]
    quote: PerpBookQuote


class ExternalPerpVenue(Protocol):
    name: str

    def instruments(self) -> dict[str, PerpInstrument]: ...

    def market(
        self, instrument: PerpInstrument, *, days: int, notional_usd: float
    ) -> ExternalPerpMarket: ...
```

Implement Binance discovery from `/fapi/v1/exchangeInfo`, accepting only
`PERPETUAL`, `TRADING`, `USDT` linear contracts. Implement OKX discovery from
`/api/v5/public/instruments?instType=SWAP`, accepting only `linear`, `live`,
`USDT` or `USDC` settled swaps and preferring USDT when both exist.

- [ ] **Step 4: Run discovery tests**

Run: `.venv/bin/pytest tests/test_cross_perp_venues.py -v`

Expected: both discovery tests pass.

- [ ] **Step 5: Write failing market-data tests**

Add deterministic URL-dispatch fakes that return:

```python
binance_responses = {
    "/fapi/v1/premiumIndex": {
        "symbol": "ZROUSDT",
        "markPrice": "2.00",
        "lastFundingRate": "0.0001",
        "time": 1_000_000,
    },
    "/fapi/v1/fundingRate": [
        {"fundingTime": 100, "fundingRate": "0.0001"},
        {"fundingTime": 200, "fundingRate": "-0.0002"},
    ],
    "/fapi/v1/depth": {
        "T": 1_000_000,
        "bids": [["1.99", "600"], ["1.98", "100"]],
        "asks": [["2.01", "600"], ["2.02", "100"]],
    },
}
```

Assert:

```python
market = venue.market(instrument, days=7, notional_usd=1_000)
assert market.current_funding_rate == 0.0001
assert market.mark_price == 2.0
assert [point.funding_rate for point in market.funding_events] == [0.0001, -0.0002]
assert market.quote.executable_buy_price == 2.01
assert market.quote.executable_sell_price == 1.99
assert market.quote.fee_bps == 5.0
```

Add equivalent OKX fixtures for `/public/funding-rate`,
`/public/funding-rate-history`, `/market/ticker`, and `/market/books`. Verify
that each OKX history page uses the preceding page's oldest timestamp as the
`before` value until the seven-day boundary or an empty page.

- [ ] **Step 6: Run the market-data tests and verify failure**

Run: `.venv/bin/pytest tests/test_cross_perp_venues.py -v`

Expected: failures because `market()` is not implemented.

- [ ] **Step 7: Implement market data, pagination, and executable depth**

Use `urllib.parse.urlencode` for all query strings. Normalize both exchange
funding rates unchanged so positive means longs pay shorts. Walk bids and asks
with the same notional-VWAP algorithm used by the existing spot venue code.
Raise `RuntimeError` for malformed payloads. Return the quote and full visible
depth even when one side cannot fill `$1,000`; set that side's executable price
to `None` so the evaluator can record `insufficient_depth` rather than
mislabeling a thin book as a venue outage.

Set public conservative taker-fee assumptions as:

```python
class BinancePerpVenue:
    name = "binance"
    fee_bps = 5.0


class OkxPerpVenue:
    name = "okx"
    fee_bps = 5.0
```

- [ ] **Step 8: Run the venue test file**

Run: `.venv/bin/pytest tests/test_cross_perp_venues.py -v`

Expected: all tests pass.

- [ ] **Step 9: Commit the public venue clients**

```bash
git add src/funding_arb_monitor/cross_perp_venues.py tests/test_cross_perp_venues.py
git commit -m "feat: add public cross-perp venue clients"
```

### Task 2: Funding Normalization and Executable Carry Evaluation

**Files:**
- Create: `src/funding_arb_monitor/cross_perp.py`
- Create: `tests/test_cross_perp.py`

**Interfaces:**
- Consumes: `PerpBookQuote`, `PerpFundingEvent`, `ExternalPerpMarket`, and `ExternalPerpVenue`.
- Produces: `CrossPerpConfig`, `HyperliquidPerpMarket`, `CrossPerpObservation`.
- Produces: `realized_funding_apr(events, *, window_days) -> tuple[float | None, float, int | None]`.
- Produces: `evaluate_direction(hyperliquid, external, direction, config, now_ms) -> CrossPerpObservation`.
- Direction values are exactly `short_hyperliquid_long_external` and `long_hyperliquid_short_external`.

- [ ] **Step 1: Write failing funding-normalization tests**

```python
from funding_arb_monitor.cross_perp import realized_funding_apr
from funding_arb_monitor.cross_perp_venues import PerpFundingEvent


def test_realized_funding_apr_normalizes_eight_hour_events() -> None:
    interval_ms = 8 * 3_600_000
    events = tuple(
        PerpFundingEvent(index * interval_ms, 0.0001)
        for index in range(21)
    )
    apr, coverage, observed_interval_ms = realized_funding_apr(
        events, window_days=7
    )
    assert apr == pytest.approx(10.95)
    assert coverage == pytest.approx(1.0)
    assert observed_interval_ms == interval_ms


def test_realized_funding_apr_rejects_single_event() -> None:
    assert realized_funding_apr(
        (PerpFundingEvent(1, 0.0001),), window_days=7
    ) == (None, 0.0, None)
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.venv/bin/pytest tests/test_cross_perp.py -k realized_funding -v`

Expected: collection fails because `funding_arb_monitor.cross_perp` is absent.

- [ ] **Step 3: Implement normalization and domain models**

Use the median gap between sorted unique timestamps as the observed funding
interval. Calculate expected events as `window_ms / interval_ms`, coverage as
`min(len(events) / expected_events, 1.0)`, and annualized APR as
`mean(funding_rate) * YEAR_MS / interval_ms * 100`.

Define:

```python
@dataclass(frozen=True)
class CrossPerpConfig:
    notional_usd: float = 1_000.0
    history_days: int = 7
    min_history_coverage: float = 0.8
    max_basis_bps: float = 100.0
    max_quote_age_ms: int = 60_000
    continuity_window_ms: int = 90 * 60_000
    hyperliquid_fee_bps: float = 4.5


@dataclass(frozen=True)
class HyperliquidPerpMarket:
    dex: str
    asset: str
    current_funding_rate: float
    mark_price: float
    funding_captured_at_ms: int
    funding_events: tuple[PerpFundingEvent, ...]
    quote: PerpBookQuote


@dataclass(frozen=True)
class CrossPerpObservation:
    observed_at_ms: int
    hyperliquid_dex: str
    asset: str
    external_venue: str
    external_symbol: str
    direction: str
    hyperliquid_current_funding_rate: float | None
    external_current_funding_rate: float | None
    hyperliquid_funding_apr_pct: float | None
    external_funding_apr_pct: float | None
    gross_spread_apr_pct: float | None
    net_apr_7d_pct: float | None
    expected_funding_usd: float | None
    transaction_cost_usd: float | None
    basis_bps: float | None
    hyperliquid_mark_price: float | None
    external_mark_price: float | None
    hyperliquid_executable_price: float | None
    external_executable_price: float | None
    hyperliquid_slippage_bps: float | None
    external_slippage_bps: float | None
    hyperliquid_depth_usd: float
    external_depth_usd: float
    hyperliquid_fee_bps: float
    external_fee_bps: float
    hyperliquid_history_coverage: float
    external_history_coverage: float
    hyperliquid_funding_at_ms: int | None
    external_funding_at_ms: int | None
    hyperliquid_quote_at_ms: int | None
    external_quote_at_ms: int | None
    qualified: bool
    reasons: tuple[str, ...]
    streak: int = 0
    observation_ready: bool = False

    def as_dict(self) -> dict[str, object]: ...
```

- [ ] **Step 4: Run normalization tests**

Run: `.venv/bin/pytest tests/test_cross_perp.py -k realized_funding -v`

Expected: all selected tests pass.

- [ ] **Step 5: Write failing direction and transaction-cost tests**

Construct Hyperliquid and external markets with complete seven-day histories,
fresh books, `100.0` midpoints, Hyperliquid sell/buy prices of `99.95/100.05`,
external prices of `99.96/100.04`, and funding APRs of `30%` and `10%`.

Assert:

```python
short_hl = evaluate_direction(
    hyperliquid,
    external,
    "short_hyperliquid_long_external",
    config,
    now_ms,
)
long_hl = evaluate_direction(
    hyperliquid,
    external,
    "long_hyperliquid_short_external",
    config,
    now_ms,
)
assert short_hl.gross_spread_apr_pct == pytest.approx(20.0)
assert long_hl.gross_spread_apr_pct == pytest.approx(-20.0)
assert short_hl.transaction_cost_usd == pytest.approx(
    1_000 * (2 * (4.5 + 5.0 + 5.0 + 4.0)) / 10_000
)
assert short_hl.net_apr_7d_pct < short_hl.gross_spread_apr_pct
assert long_hl.reasons == ("net_carry_non_positive",)
```

Add individual tests for:

- `insufficient_history` below `0.8` coverage.
- `stale_funding` when the latest funding event exceeds twice its observed
  interval.
- `stale_quote` after `60_000` ms.
- `insufficient_depth` when either direction's required book side is below
  `$1,000`.
- `basis_too_wide` above `100` bps.
- Multiple reasons retained in deterministic rule order.

- [ ] **Step 6: Run evaluation tests and verify failure**

Run: `.venv/bin/pytest tests/test_cross_perp.py -k 'direction or insufficient or stale or basis' -v`

Expected: failures because `evaluate_direction()` is missing.

- [ ] **Step 7: Implement minimal direction evaluation**

For each route:

```python
gross_apr_pct = (
    hyperliquid_apr - external_apr
    if direction == "short_hyperliquid_long_external"
    else external_apr - hyperliquid_apr
)
expected_funding_usd = (
    config.notional_usd * gross_apr_pct / 100
    * config.history_days / 365
)
transaction_cost_bps = 2 * (
    config.hyperliquid_fee_bps
    + external.quote.fee_bps
    + hyperliquid_entry_slippage_bps
    + external_entry_slippage_bps
)
transaction_cost_usd = config.notional_usd * transaction_cost_bps / 10_000
net_apr_7d_pct = (
    (expected_funding_usd - transaction_cost_usd)
    / config.notional_usd
    * 365 / config.history_days * 100
)
```

Use the sell book for a short leg and the buy book for a long leg. A missing
executable price on either required side produces `insufficient_depth`. Compute
basis from mark prices as
`(external.mark_price / hyperliquid.mark_price - 1) * 10_000`, gate its
absolute value, and retain its sign in output. Do not count basis as expected
profit.

- [ ] **Step 8: Run all pure evaluator tests**

Run: `.venv/bin/pytest tests/test_cross_perp.py -v`

Expected: all current tests pass.

- [ ] **Step 9: Commit the evaluator**

```bash
git add src/funding_arb_monitor/cross_perp.py tests/test_cross_perp.py
git commit -m "feat: evaluate executable cross-perp carry"
```

### Task 3: SQLite Run History and Three-Scan Qualification

**Files:**
- Modify: `src/funding_arb_monitor/store.py`
- Modify: `tests/test_cross_perp.py`

**Interfaces:**
- Consumes: `CrossPerpObservation.as_dict()`.
- Produces: `Store.start_cross_perp_run() -> int`.
- Produces: `Store.save_cross_perp_observations(run_id, observations, *, continuity_window_ms) -> list[dict[str, object]]`.
- Produces: `Store.finish_cross_perp_run(run_id, *, status, venue_status, match_count, evaluation_count, positive_net_count, ready_count, error=None) -> None`.
- Produces: `Store.cross_perp_summary() -> dict[str, object]`.
- Produces: `Store.latest_cross_perp_observations(limit=100, observation_ready_only=False) -> list[dict[str, object]]`.
- Produces: `Store.cross_perp_history(asset, external_venue, direction, limit=100) -> list[dict[str, object]]`.

- [ ] **Step 1: Write failing schema and round-trip tests**

```python
def test_cross_perp_observations_round_trip(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    run_id = store.start_cross_perp_run()
    saved = store.save_cross_perp_observations(
        run_id,
        [qualifying_observation(observed_at_ms=1_000)],
        continuity_window_ms=5_400_000,
    )
    store.finish_cross_perp_run(
        run_id,
        status="success",
        venue_status={"binance": "success", "okx": "success"},
        match_count=1,
        evaluation_count=1,
        positive_net_count=1,
        ready_count=0,
    )
    assert saved[0]["streak"] == 1
    assert saved[0]["observation_ready"] is False
    assert store.latest_cross_perp_observations()[0]["asset"] == "ZRO"
```

- [ ] **Step 2: Run the persistence test and verify failure**

Run: `.venv/bin/pytest tests/test_cross_perp.py -k round_trip -v`

Expected: `AttributeError` for `start_cross_perp_run`.

- [ ] **Step 3: Add idempotent tables and basic queries**

Add to `Store.initialize()`:

```sql
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
```

Decode `payload_json`, then overlay authoritative indexed columns and `run_id`
when returning rows. Latest-opportunity queries must select the newest
completed run regardless of status; if that run failed without observations,
return an empty list instead of presenting an older successful run as current.
The summary returns the newest run of any status.

- [ ] **Step 4: Run the round-trip test**

Run: `.venv/bin/pytest tests/test_cross_perp.py -k round_trip -v`

Expected: pass.

- [ ] **Step 5: Write failing streak and query tests**

Create three successful runs 60 minutes apart with the same qualifying
identity. Assert streaks `[1, 2, 3]` and ready flags `[False, False, True]`.
Then assert all of these reset the next qualifying streak to one:

- The preceding successful run contains the route but it is non-qualifying.
- The preceding successful run omits the route.
- The gap exceeds `5_400_000` ms.
- The venue or direction changes.

Assert latest ranking sorts non-null `net_apr_7d_pct` descending, ready-only
filtering excludes streaks below three, history is newest first, and summary
decodes `venue_status_json`.

- [ ] **Step 6: Run streak tests and verify failure**

Run: `.venv/bin/pytest tests/test_cross_perp.py -k 'streak or latest_cross or summary' -v`

Expected: streak remains one or query methods are missing.

- [ ] **Step 7: Implement streak calculation and read queries**

Before inserting a qualifying observation, look only at the immediately
previous `cross_perp_runs.status = 'success'` run. Continue the streak only
when that run has the exact same route identity, `qualified = 1`, and the new
observation timestamp minus the previous observation timestamp is within
`continuity_window_ms`. Set `observation_ready = streak >= 3`.

When an observation is non-qualifying, store `streak = 0` and
`observation_ready = 0`.

Include `observation_age_seconds` in returned latest rows, calculated from
`observed_at_ms` at query time and clamped to zero.

- [ ] **Step 8: Run evaluator and persistence tests**

Run: `.venv/bin/pytest tests/test_cross_perp.py -v`

Expected: all tests pass.

- [ ] **Step 9: Commit persistence**

```bash
git add src/funding_arb_monitor/store.py tests/test_cross_perp.py
git commit -m "feat: persist cross-perp qualification history"
```

### Task 4: Monitor Orchestration and Read-Only CLI

**Files:**
- Modify: `src/funding_arb_monitor/cross_perp.py`
- Modify: `src/funding_arb_monitor/cli.py`
- Modify: `tests/test_cross_perp.py`

**Interfaces:**
- Consumes: `HyperliquidClient.snapshots()`, `.funding_history()`, and `.perp_quote()`.
- Consumes: `ExternalPerpVenue.instruments()` and `.market()`.
- Consumes: all cross-perp `Store` methods from Task 3.
- Produces: `CrossPerpMonitor(hyperliquid, venues, store, config=CrossPerpConfig(), now_ms=...)`.
- Produces: `CrossPerpMonitor.run() -> dict[str, object]`.
- Produces CLI command:
  `funding-arb-monitor --db data/funding_arb.db cross-perp`.

- [ ] **Step 1: Write failing successful and partial-failure monitor tests**

Use fakes with one Hyperliquid `ZRO` snapshot and exact Binance/OKX
instruments. Assert a run:

```python
result = CrossPerpMonitor(
    hyperliquid=fake_hyperliquid,
    venues=[working_binance, failing_okx],
    store=store,
    now_ms=lambda: NOW_MS,
).run()

assert result["status"] == "success"
assert result["venue_status"] == {
    "binance": "success",
    "okx": "failed: unavailable",
}
assert result["match_count"] == 1
assert result["evaluation_count"] == 2
assert {
    row["direction"]
    for row in store.latest_cross_perp_observations()
} == {
    "short_hyperliquid_long_external",
    "long_hyperliquid_short_external",
}
```

Add a test where both external venue catalogues fail. Assert the run is stored
as `failed`, `CrossPerpMonitor.run()` raises `RuntimeError`, and no stale
observation is copied into the failed run.

Add a per-market failure test that stores two rejected direction rows with
`venue_unavailable` and continues other assets.

- [ ] **Step 2: Run monitor tests and verify failure**

Run: `.venv/bin/pytest tests/test_cross_perp.py -k monitor -v`

Expected: `CrossPerpMonitor` is missing.

- [ ] **Step 3: Implement orchestration**

The monitor must:

1. Start a cross-perp run before network work.
2. Fetch Hyperliquid snapshots and retain exact asset names.
3. Load each external venue catalogue independently.
4. Intersect exact assets.
5. Fetch one Hyperliquid market and one external market per match.
6. Evaluate both directions.
7. Persist streak-adjusted observations.
8. Finish the run with counts from persisted output.

Reuse each Hyperliquid asset's history and book across both external venues.
Convert the existing `PerpQuote` into `PerpBookQuote` with fee `4.5` bps.
An external venue catalogue failure is partial if another venue succeeds.
A Hyperliquid failure or zero successful external catalogues is a total run
failure.

- [ ] **Step 4: Run monitor tests**

Run: `.venv/bin/pytest tests/test_cross_perp.py -k monitor -v`

Expected: pass.

- [ ] **Step 5: Write failing CLI parser and dispatch tests**

```python
def test_parser_accepts_cross_perp() -> None:
    args = parser().parse_args(["--db", "test.db", "cross-perp"])
    assert args.command == "cross-perp"
```

Monkeypatch `CrossPerpMonitor.run()` to return a deterministic summary and
assert `main()` prints JSON containing `"status": "success"`.

- [ ] **Step 6: Run CLI tests and verify failure**

Run: `.venv/bin/pytest tests/test_cross_perp.py -k cli -v`

Expected: argparse rejects `cross-perp`.

- [ ] **Step 7: Add the CLI command**

Add:

```python
subcommands.add_parser(
    "cross-perp",
    help="monitor public Hyperliquid versus Binance and OKX perpetual carry",
)
```

Dispatch it before the existing scan-only configuration:

```python
if args.command == "cross-perp":
    result = CrossPerpMonitor(
        HyperliquidClient(),
        [BinancePerpVenue(), OkxPerpVenue()],
        store,
    ).run()
    print(json.dumps(result, indent=2))
    return
```

- [ ] **Step 8: Run cross-perp tests**

Run: `.venv/bin/pytest tests/test_cross_perp.py tests/test_cross_perp_venues.py -v`

Expected: all pass.

- [ ] **Step 9: Commit orchestration and CLI**

```bash
git add src/funding_arb_monitor/cross_perp.py src/funding_arb_monitor/cli.py tests/test_cross_perp.py
git commit -m "feat: run cross-perp monitoring"
```

### Task 5: Hourly Scheduling Without Primary Readiness Impact

**Files:**
- Modify: `src/funding_arb_monitor/scheduler.py`
- Modify: `src/funding_arb_monitor/store.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Produces scheduled job `ScheduledJob("cross-perp", 6, ("cross-perp",))`.
- Preserves `/readyz` behavior by treating `cross-perp` as an informational,
  non-critical scheduled job.

- [ ] **Step 1: Write failing scheduler tests**

```python
def test_cross_perp_runs_after_hourly_scan() -> None:
    jobs = due_jobs(datetime(2026, 7, 30, 18, 6), {})
    assert [job.name for job in jobs] == ["cross-perp"]
    assert jobs[0].command == ("cross-perp",)


def test_failed_cross_perp_job_is_visible_but_not_critical(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    run_id = store.start_scheduled_job("cross-perp", "2026-07-30T18:06")
    store.finish_scheduled_job(run_id, exit_code=2, error="venue outage")
    health = store.scheduler_health()
    latest = next(row for row in health["latest_jobs"] if row["name"] == "cross-perp")
    assert latest["status"] == "failed"
    assert health["unhealthy_jobs"] == []
```

In `tests/test_api.py`, insert a successful normal scan and a failed
`cross-perp` scheduled job, then assert `/readyz` remains `200`.

- [ ] **Step 2: Run scheduler/readiness tests and verify failure**

Run: `.venv/bin/pytest tests/test_scheduler.py tests/test_api.py -k 'cross_perp or readyz' -v`

Expected: no scheduled cross-perp job and failed job makes readiness unhealthy.

- [ ] **Step 3: Add the job and criticality rule**

Add the cross-perp job at minute six. In `Store.scheduler_health()`, keep all
latest jobs in output but append failures/overdue states to `unhealthy` only
for the existing critical jobs:

```python
critical_jobs = {
    "scan", "shadow", "accrue", "update",
    "report", "heartbeat", "backup",
}
```

Do not add `cross-perp` to `critical_jobs` or the hourly overdue map.

- [ ] **Step 4: Run scheduler and API readiness tests**

Run: `.venv/bin/pytest tests/test_scheduler.py tests/test_api.py -k 'cross_perp or readyz' -v`

Expected: all selected tests pass.

- [ ] **Step 5: Commit scheduler integration**

```bash
git add src/funding_arb_monitor/scheduler.py src/funding_arb_monitor/store.py tests/test_scheduler.py tests/test_api.py
git commit -m "feat: schedule non-critical cross-perp scans"
```

### Task 6: Protected Cross-Perp APIs

**Files:**
- Modify: `src/funding_arb_monitor/api.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `Store.cross_perp_summary()`.
- Consumes: `Store.latest_cross_perp_observations()`.
- Consumes: `Store.cross_perp_history()`.
- Produces: `GET /api/cross-perp/summary`.
- Produces: `GET /api/cross-perp/opportunities?limit=100&observation_ready_only=false`.
- Produces: `GET /api/cross-perp/history?asset=ZRO&external_venue=binance&direction=short_hyperliquid_long_external&limit=100`.

- [ ] **Step 1: Write failing empty-state, ranking, filtering, and validation tests**

```python
def test_cross_perp_api_empty_state(tmp_path) -> None:
    client = TestClient(create_app(str(tmp_path / "test.db")))
    assert client.get("/api/cross-perp/summary").json() == {
        "status": "never_run",
        "venue_status": {},
        "match_count": 0,
        "evaluation_count": 0,
        "positive_net_count": 0,
        "ready_count": 0,
        "rejection_counts": {},
    }
    assert client.get("/api/cross-perp/opportunities").json() == []
```

Persist mixed ready/non-ready observations, then assert:

- Opportunities are ordered by net APR descending.
- `observation_ready_only=true` returns only streak-three routes.
- History requires the exact asset, venue, and allowed direction.
- Limit validation enforces `1..500`.
- With `FUNDING_ARB_READ_TOKEN`, all three endpoints return `401` without
  `X-Read-Token` and `200` with it.

- [ ] **Step 2: Run API tests and verify 404 failures**

Run: `.venv/bin/pytest tests/test_api.py -k cross_perp -v`

Expected: all new requests return `404`.

- [ ] **Step 3: Add the three GET routes**

Use FastAPI `Query` bounds. Direction must be a `Literal` of the two supported
values, or explicitly validated against the two constants. Return store
results directly; do not introduce mutation routes.

The summary store query must aggregate rejection reasons from the latest
successful run's decoded observation payloads into deterministic descending
count order.

- [ ] **Step 4: Run API tests**

Run: `.venv/bin/pytest tests/test_api.py -k cross_perp -v`

Expected: all pass.

- [ ] **Step 5: Commit APIs**

```bash
git add src/funding_arb_monitor/api.py src/funding_arb_monitor/store.py tests/test_api.py
git commit -m "feat: expose cross-perp monitoring APIs"
```

### Task 7: Dashboard, Documentation, and End-to-End Verification

**Files:**
- Modify: `src/funding_arb_monitor/static/index.html`
- Modify: `tests/test_api.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `/api/cross-perp/summary` and `/api/cross-perp/opportunities`.
- Produces dashboard IDs: `cross-perp-status`, `cross-perp-counters`,
  `cross-perp-rows`, and `cross-perp-empty`.

- [ ] **Step 1: Write a failing dashboard structure test**

Extend `test_dashboard_and_api_are_available`:

```python
assert 'id="cross-perp-status"' in dashboard.text
assert 'id="cross-perp-counters"' in dashboard.text
assert 'id="cross-perp-rows"' in dashboard.text
assert 'id="cross-perp-empty"' in dashboard.text
assert "Observation ready" in dashboard.text
assert "Actionable cross-perp" not in dashboard.text
```

- [ ] **Step 2: Run the dashboard test and verify failure**

Run: `.venv/bin/pytest tests/test_api.py::test_dashboard_and_api_are_available -v`

Expected: fails on the first missing cross-perp element.

- [ ] **Step 3: Add the dashboard section**

Add a section after the existing execution funnel. Its counters are:

- Routes evaluated
- Positive after costs
- Observation ready
- Venue coverage

The table columns are:

- Asset
- Route
- Direction
- Gross 7d APR
- Net 7d APR
- Basis
- Executable depth
- Streak
- Status/reasons
- Data age

Add `loadCrossPerp()` using the existing `apiFetch()` helper. Fetch summary and
opportunities independently from the current spot-perp evidence calls so a
cross-perp outage does not prevent the rest of the dashboard from rendering.
Use `escapeHtml()` for text and existing numeric formatting patterns. Label a
streak-three route `Observation ready`; label other qualified routes
`Monitoring (N/3)`; otherwise render joined rejection reasons.

- [ ] **Step 4: Run dashboard and full API tests**

Run: `.venv/bin/pytest tests/test_api.py -v`

Expected: all pass.

- [ ] **Step 5: Update README with exact operational semantics**

Document:

```bash
funding-arb-monitor --db data/funding_arb.db cross-perp
```

List the three protected endpoints, `$1,000` notional, Binance/OKX scope, both
directions, positive-after-cost rules, three consecutive scans, and the
read-only/non-actionable boundary. State that cross-perp degradation is visible
but non-critical to `/readyz`.

- [ ] **Step 6: Run formatting and complete automated verification**

Run:

```bash
git diff --check
.venv/bin/pytest tests/test_cross_perp_venues.py tests/test_cross_perp.py -v
.venv/bin/pytest -q
```

Expected: no whitespace errors, all focused tests pass, and the entire existing
suite passes.

- [ ] **Step 7: Perform a public-API smoke run locally**

Run:

```bash
.venv/bin/funding-arb-monitor --db data/cross-perp-smoke.db cross-perp
.venv/bin/funding-arb-monitor --db data/cross-perp-smoke.db serve --host 127.0.0.1 --port 8081
```

In another shell, request:

```bash
curl -fsS http://127.0.0.1:8081/healthz
curl -fsS http://127.0.0.1:8081/api/cross-perp/summary
curl -fsS 'http://127.0.0.1:8081/api/cross-perp/opportunities?limit=5'
```

Expected: health is `{"status":"ok"}`, summary reports a completed run with
venue coverage, and opportunities contain explicit status/reasons. Stop the
local server afterward. The smoke database is not committed.

- [ ] **Step 8: Commit dashboard and documentation**

```bash
git add src/funding_arb_monitor/static/index.html tests/test_api.py README.md
git commit -m "feat: show cross-perp monitoring evidence"
```

- [ ] **Step 9: Push and verify Zeabur deployment**

Run:

```bash
git status --short --branch
git log -1 --oneline
git push origin main
```

Verify that Zeabur reports the exact pushed commit as running. Then use the
Keychain-backed read token without printing it to request:

```bash
FUNDING_MONITOR_READ_TOKEN="$(
  security find-generic-password \
    -s funding-arb-monitor-read-token -a "$USER" -w
)"
curl -fsS https://funding-arb-monitor.zeabur.app/healthz
curl -fsS https://funding-arb-monitor.zeabur.app/readyz
curl -fsS -H "X-Read-Token: ${FUNDING_MONITOR_READ_TOKEN}" \
  https://funding-arb-monitor.zeabur.app/api/cross-perp/summary
curl -fsS -H "X-Read-Token: ${FUNDING_MONITOR_READ_TOKEN}" \
  'https://funding-arb-monitor.zeabur.app/api/cross-perp/opportunities?limit=5'
unset FUNDING_MONITOR_READ_TOKEN
```

Expected: both health endpoints return `200`; summary has a recent successful
or explicitly degraded cross-perp run; observations show Binance and/or OKX,
both direction semantics, net carry, and streaks; the live dashboard renders
the new section. Never include the read-token value in terminal output or the
handoff.
