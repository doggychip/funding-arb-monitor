# Cross-Perpetual Monitoring Design

## Goal

Add read-only monitoring for funding-rate arbitrage between Hyperliquid
perpetuals and matching Binance or OKX perpetuals. The monitor must evaluate
both hedge directions, retain rejected observations for analysis, and require
three consecutive qualifying hourly scans before labeling a route
`observation_ready`.

This feature does not place orders, use exchange credentials, or automatically
open paper positions.

## Scope

Version one will:

- Match exact underlying assets between Hyperliquid and Binance or OKX.
- Evaluate both short-Hyperliquid/long-external and
  long-Hyperliquid/short-external directions.
- Use seven-day realized funding history for the primary carry estimate.
- Price a fixed $1,000 observation notional from visible order-book depth.
- Include round-trip taker fees and measured slippage in the net-carry result.
- Store every evaluation and its rejection reasons.
- Expose protected read APIs and a dashboard section.

Version one will not:

- Use authenticated exchange APIs.
- Place live or paper orders.
- Match aliases or bridged/wrapped assets.
- Optimize collateral transfers, leverage, liquidation distance, or margin
  allocation.
- Treat a positive funding spread as executable trading readiness.

## Chosen Architecture

Use a dedicated cross-perpetual observation engine rather than extending the
existing spot-perpetual candidate pipeline. Spot borrow assumptions, hedge
semantics, rejection reasons, and paper-position workflows remain unchanged.

An hourly cross-perpetual command will run after the main market scan and will:

1. Fetch Hyperliquid perpetual markets, normalized funding history, and
   executable order-book quotes.
2. Fetch the same public data for Binance and OKX perpetual markets.
3. Build exact underlying matches.
4. Evaluate both hedge directions for each match.
5. Persist the result and its qualification streak.
6. Publish summary and ranked views through the existing API and dashboard.

Each external venue is isolated. A Binance failure does not prevent OKX
observations, and cross-perpetual degradation does not invalidate the primary
Hyperliquid scan.

## Funding Normalization and Direction

All venue clients normalize a funding rate so that a positive value means
longs pay shorts. Historical events retain their source timestamps, and
coverage is assessed using the venue's observed funding interval.

For a route with normalized annualized funding rates `hyperliquid_apr` and
`external_apr`:

- Short Hyperliquid / long external gross spread:
  `hyperliquid_apr - external_apr`.
- Long Hyperliquid / short external gross spread:
  `external_apr - hyperliquid_apr`.

Both directions are evaluated and stored even when their gross or net carry is
non-positive.

## Executable Cost Model

The observation notional is $1,000 per leg.

For each direction, the engine walks the relevant side of both public order
books and records executable entry prices, visible depth, and slippage from the
midpoint. A route fails depth qualification if either leg cannot fill the full
notional.

Seven-day expected funding dollars are:

`notional * gross_spread_apr_pct / 100 * 7 / 365`

Estimated transaction cost includes:

- Entry and exit taker fees on both venues.
- Measured entry slippage on both legs.
- An equal slippage allowance for the eventual exit.

Seven-day net APR is the annualized result after subtracting those transaction
costs. Basis is displayed and gated separately because convergence is not
guaranteed; it is not silently counted as profit.

Venue fee assumptions are explicit constants covered by tests and included in
API output so the estimate can be audited. Changing them later must not alter
historical observations.

## Qualification Rules

An observation qualifies for the current run only when:

- The underlying asset is an exact match.
- Seven-day funding history has sufficient interval coverage.
- Funding, mark-price, and order-book data are fresh.
- Both $1,000 legs are fully executable from visible depth.
- Absolute entry basis is at most 1%.
- Seven-day net carry is positive after modeled costs.

Otherwise, the observation stores one or more explicit reasons, including:

- `insufficient_history`
- `stale_funding`
- `stale_quote`
- `insufficient_depth`
- `basis_too_wide`
- `net_carry_non_positive`
- `venue_unavailable`

A route identity is the combination of asset, external venue, external symbol,
and direction. Its streak increases only when the immediately preceding
successful hourly cross-perpetual run contains the same qualifying identity
and the elapsed time is within the allowed hourly continuity window. A failed
run, missing route, non-qualifying result, or excessive time gap resets the
next qualifying observation to a streak of one.

The route becomes `observation_ready` at a streak of three. All earlier
qualifying observations remain visible as monitoring evidence.

## Persistence

The SQLite initialization adds two independent tables.

### `cross_perp_runs`

Stores:

- Run ID and start/completion timestamps.
- Status and error detail.
- Venue success/failure coverage.
- Match, evaluation, positive-net, and observation-ready counts.

### `cross_perp_observations`

Stores one immutable row per run and route identity, including:

- Asset, venues, symbols, and direction.
- Source timestamps and funding-history coverage.
- Hyperliquid and external realized funding APRs.
- Gross funding-spread APR.
- Midpoints, executable entry prices, visible depth, and slippage.
- Basis, fee assumptions, expected funding dollars, transaction cost, and
  seven-day net APR.
- Qualification status, reasons, streak, and `observation_ready`.

These are additive tables created through the project's existing idempotent
SQLite initialization pattern. No existing table or paper-trade meaning is
changed.

## Scheduler and Failure Behavior

The scheduler invokes a separate cross-perpetual CLI command hourly after the
main scan. The command has its own run lifecycle and exits non-zero only for a
total run failure. Partial venue failures are recorded as degraded coverage
while successful venues are still persisted.

The latest cross-perpetual status is shown through its own summary. Initially,
cross-perpetual failure will not make `/readyz` fail because the primary monitor
must remain available during a Binance or OKX outage.

No stale observation is promoted as current. APIs identify the latest
completed cross-perpetual run and expose its data age.

## API

All new endpoints inherit the existing `X-Read-Token` protection.

### `GET /api/cross-perp/summary`

Returns the latest run, venue coverage, freshness, funnel counts, rejection
counts, and number of observation-ready routes.

### `GET /api/cross-perp/opportunities`

Returns observations from the latest completed run ranked by seven-day net APR.
It supports a bounded result limit and an `observation_ready_only` filter.

### `GET /api/cross-perp/history`

Returns recent observations for one asset, external venue, and direction so
the qualification streak and resets are auditable.

## Dashboard

The existing dashboard gains a cross-perpetual section with:

- Evaluated-route, positive-after-cost, and observation-ready counters.
- Venue coverage and latest-run age.
- A ranked table showing asset, route, direction, gross spread APR, seven-day
  net APR, basis, executable depth, streak, status, and reasons.
- Clear degraded or unavailable venue messaging.

The section must not label routes as actionable trades. The strongest state is
`observation_ready`.

## Testing

Tests will be written before production code and will cover:

- Funding-sign and interval normalization for all three venues.
- Both hedge-direction calculations.
- Fee, slippage, funding-dollar, and net-APR arithmetic.
- History coverage, freshness, depth, basis, and net-carry rejection.
- Streak progression to three and resets after gaps or failed qualification.
- Exact-symbol matching and unsupported markets.
- Partial and total venue failures.
- SQLite persistence and latest/history queries.
- API authentication, ranking, filters, and empty/degraded states.
- Scheduler command integration without changing primary readiness semantics.
- Dashboard rendering of the new API fields.

The complete existing test suite must continue to pass.

## Deployment Verification

After implementation:

1. Run the focused cross-perpetual tests and the full test suite.
2. Commit and push the implementation.
3. Confirm the watched Zeabur service deploys the exact pushed commit.
4. Verify `/healthz` and `/readyz`.
5. Verify all three protected cross-perpetual endpoints.
6. Confirm a fresh run contains Binance and/or OKX observations, explicit
   rejection reasons, and correct streak behavior.
7. Confirm the dashboard distinguishes monitoring evidence from
   `observation_ready` routes and does not imply live execution.
