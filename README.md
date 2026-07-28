# Funding Arb Monitor

Read-only funding-carry monitoring for Hyperliquid. It discovers all active perps across the
main and builder dexes, stores public funding history in SQLite, computes realized carry, and
emits only candidates that clear explicit liquidity and stability gates.

It does not accept exchange credentials, place orders, manage balances, or execute trades.

## Why monitor first

Current-hour funding APR is a snapshot, not a forecast. The scanner instead records:

- 24-hour, 7-day, and window realized funding APR;
- negative-funding share, which shows how often a short-perp carry reverses;
- peak decay half-life;
- open interest and notional volume;
- an explicit long-perp/short-underlying flag for negative funding, because borrow must be
  independently verified.

Hourly runs fetch only funding records newer than the latest stored point. Public API requests
are throttled and transient disconnects, HTTP 429 responses, and server errors use bounded
exponential backoff. If one market still fails, the rest of the scan completes; cached data for
that market is marked `funding_refresh_failed` and cannot pass the monitoring gates.

Paper matching adds a conservative cost model (10 bps perp fill, each venue's base taker fee,
5% annual financing, and a 7-day hold) and requires an exact same-asset OKX, Binance, Coinbase,
or Kraken spot book with ≥5× notional depth and ≥10% executable net APR. The dashboard also
shows 14- and 30-day net-APR sensitivity, but approval remains gated by the 7-day case. OKX and
Binance prefer USDC books, then USDT; stablecoin basis/depeg risk is not modeled. Equity perps
(`xyz:*`) usually fail exact-spot matching and are not a 24/7 delta-neutral arb when the cash
market is shut. FX, session gaps, and your own market impact are still out of scope.

Paper entry and approval also fetch the public Hyperliquid L2 book and use bid-side VWAP for the
simulated short-perp fill. A missing, stale, or undersized two-sided perp book is rejected. Quote
depth, spread, and capture time are retained with the recommendation. The simulation still cannot
reproduce leg latency, partial fills, exchange outages, or stablecoin basis, so its result is
evidence—not a guarantee of live execution.

## Run locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

# Fetch and persist a scan; prints only candidates and their rejection reasons.
funding-arb-monitor scan --days 30 --min-oi 1000000

# Run the read-only dashboard/API.
funding-arb-monitor serve
```

Open http://127.0.0.1:8080 for the local dashboard. It shows scan status, candidates, trade
recommendations, performance, and P&L/basis timelines for open and closed paper positions. FastAPI
docs remain at http://127.0.0.1:8080/docs.

Useful endpoints: `GET /healthz`, `GET /readyz`, `GET /api/status`, `GET /api/candidates`
(add `?eligible_only=true` to filter or `?current_scan_only=true` for the actionable batch),
`GET /api/opportunities/actionable`,
`GET /api/paper/recommendations`, `GET /api/paper/match-checks`, `GET /api/paper/positions`,
`GET /api/paper/positions/{id}/timeline`, `GET /api/paper/report`,
`GET /api/paper/performance`, `GET /api/alerts/deliveries`, and
`POST /api/paper/recommendations/{id}/approve`.

The candidate API and dashboard retain the newest analysis for every market observed across
rotating scan batches. Each row displays its analysis age. Paper matching and recommendations use
only the current hourly batch, so an older eligible result remains visible but cannot be approved.
Every candidate includes `scan_id`, `analysis_age_seconds`, and `actionable_now`.
`/api/opportunities/actionable` is the machine-consumption endpoint: it returns only eligible rows
attached to the latest successful scan. Historical eligibility must never be treated as an order
signal.

## Paper positions

Paper positions are accounting entries only; they never create exchange orders or use credentials.
The default notional is $1,000, with at most three concurrent positions. The Zeabur scheduler
uses shadow mode to auto-open qualified simulations; manual approval remains available:

```bash
# Match eligible candidates to OKX/Binance/Coinbase/Kraken exact-asset books.
funding-arb-monitor paper recommend

# Or automatically open every qualified result as a simulated position only.
funding-arb-monitor paper shadow

# Requote both legs, recheck gates/depth/net APR, then atomically open the simulated pair.
# The command rejects deteriorated or expired recommendations.
funding-arb-monitor paper approve --id 1

# Accrue public funding into open simulated positions; idempotent per funding hour.
funding-arb-monitor paper accrue

# Refresh hedge marks, liquidity, drift, net-after-exit, and conservative exit flags.
funding-arb-monitor paper update

# Summary of funding P&L, MTM, and costs.
funding-arb-monitor paper report

# Verify Discord delivery from the deployed runtime.
funding-arb-monitor paper alert-test
```

Manual open still exists if you already know the hedge venue:

```bash
funding-arb-monitor paper open \
  --coin XMR \
  --hedge-venue coinbase \
  --notional 1000
```

Monitoring-eligible ≠ tradeable. `paper recommend` often returns `[]` when eligible names have no
exact liquid spot hedge (for example `CASHCAT` or `xyz:PALLADIUM`). Those outcomes are stored as
match checks (`no_exact_spot_market`, thin depth, net APR below threshold) and shown on the dashboard.

Open positions generate a warning after one degraded-liquidity observation. They close only after
three consecutive hourly observations with Hyperliquid 24h volume below $500,000 or public spot-book
depth below 2x position notional. Simulated exits persist executable prices, spread, depth, quantity,
and timestamp; open P&L remains labeled as estimated while closed P&L is labeled simulated-realized.
Database initialization applies these additions idempotently without replacing existing history.

## Alerts

Set `FUNDING_ARB_WEBHOOK_URL` to a webhook that accepts `{"text": "..."}` and add `--alert`:

```bash
export FUNDING_ARB_WEBHOOK_URL="https://example.invalid/webhook"
funding-arb-monitor scan --alert
```

Default alerts are dashboard/API only. No alert is sent when the variable is absent.

For Discord notifications on shadow entries, liquidity warnings, position exits, scheduler failures,
and the daily heartbeat, set a Discord channel webhook URL:

```bash
export FUNDING_ARB_DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

Webhook delivery failures do not interrupt scanning or position tracking. Messages suppress Discord
mentions and never include exchange credentials. Delivery retries and final outcomes are stored in
SQLite and exposed through `GET /api/alerts/deliveries`.

## Scheduling

Hourly host cron (times are local):

```cron
5 * * * * cd /path/to/funding-arb-monitor && /path/to/.venv/bin/funding-arb-monitor scan --days 30 --min-oi 1000000 >> data/scanner.log 2>&1
7 * * * * cd /path/to/funding-arb-monitor && /path/to/.venv/bin/funding-arb-monitor paper shadow >> data/scanner.log 2>&1
10 * * * * cd /path/to/funding-arb-monitor && /path/to/.venv/bin/funding-arb-monitor paper accrue >> data/scanner.log 2>&1
12 * * * * cd /path/to/funding-arb-monitor && /path/to/.venv/bin/funding-arb-monitor paper update >> data/scanner.log 2>&1
15 17 * * * cd /path/to/funding-arb-monitor && /path/to/.venv/bin/funding-arb-monitor paper report >> data/paper-report.log 2>&1
16 17 * * * cd /path/to/funding-arb-monitor && /path/to/.venv/bin/funding-arb-monitor paper heartbeat >> data/scanner.log 2>&1
```

The Docker image runs the API and the same schedule in one process by default. Override
`FUNDING_ARB_SCHEDULER=0` if another scheduler owns these jobs.

```bash
docker build -t funding-arb-monitor .
docker run --rm -p 8080:8080 -v funding-arb-data:/data \
  -e FUNDING_ARB_APPROVAL_TOKEN="$(openssl rand -hex 32)" \
  funding-arb-monitor
```

## Zeabur

Deploy the GitHub repository as a service; Zeabur detects the root `Dockerfile`.

1. Keep the service at one replica so only one scheduler runs.
2. Add a persistent volume mounted at `/data`.
3. Set `FUNDING_ARB_APPROVAL_TOKEN` to a random secret. The dashboard asks for it only when
   approving a paper recommendation; read-only pages remain public. Paper approval is disabled
   when this variable is absent.
4. Set `FUNDING_ARB_READ_TOKEN` to protect operational `GET /api/*` responses. The dashboard asks
   for the token and stores it in browser session storage. `/`, `/healthz`, and sanitized
   `/readyz` remain public.
5. Set `FUNDING_ARB_DISCORD_WEBHOOK_URL` to enable operational paper-trading alerts.
6. Configure the following protected variables for offsite backups:

   ```text
   FUNDING_ARB_R2_ACCOUNT_ID
   FUNDING_ARB_R2_ACCESS_KEY_ID
   FUNDING_ARB_R2_SECRET_ACCESS_KEY
   FUNDING_ARB_R2_BUCKET
   ```

   Create a private Cloudflare R2 bucket, then create an object read/write token scoped only to
   that bucket. Set a 30-day object-expiration lifecycle rule on the bucket.
7. Keep `FUNDING_ARB_TIMEZONE=Asia/Hong_Kong` (the Docker default), or override it explicitly.
8. Keep the Git trigger on `main`.
9. Generate a Zeabur domain after `/healthz` passes. Monitor `/readyz` separately; it returns
   unavailable until a successful scan has completed within the last two hours and the latest
   persisted scheduler outcomes are healthy.

The container honors Zeabur's `PORT` variable. Deleting or detaching the `/data` volume deletes
the SQLite scan and paper-trading history.

### Additive database migration

Back up the `/data` volume before deployment. On startup, `Store.initialize()` creates the
`paper_liquidity_checks` and `alert_deliveries` tables and adds nullable exit-execution columns to
`paper_positions`. Existing rows and IDs are preserved. Verify `/healthz`, the current paper
position, and `/api/alerts/deliveries` after deployment. Rolling back the application image is safe
because the previous version ignores these additive tables and columns.

### Integrity, backup, and restore

SQLite connections enable WAL, foreign keys, and a five-second busy timeout. The embedded
scheduler creates a verified online backup each day under `/data/backups`; override the directory
with `FUNDING_ARB_BACKUP_DIR`. With all four R2 variables configured, each verified backup is also
uploaded to R2. Local backups do not survive deletion of the Zeabur volume.

```bash
funding-arb-monitor maintenance check
funding-arb-monitor maintenance backup

# Restore drill: download and verify to a path outside the live database.
funding-arb-monitor maintenance download \
  --key funding-arb-monitor/YYYY/MM/DD/funding_arb-YYYYMMDDTHHMMSSZ.db \
  --destination /data/restore-drills/funding_arb-YYYYMMDDTHHMMSSZ.db
funding-arb-monitor \
  --db /data/restore-drills/funding_arb-YYYYMMDDTHHMMSSZ.db \
  maintenance check

# Only after a successful drill: stop the service, preserve the current DB, then replace it.
cp /data/funding_arb.db /data/funding_arb.pre-restore.db
cp /data/restore-drills/funding_arb-YYYYMMDDTHHMMSSZ.db /data/funding_arb.db
```

The service must be stopped before explicitly replacing `/data/funding_arb.db`; never overwrite the
live database while the API or scheduler is running.

## Continuous verification

GitHub Actions runs the full suite on Python 3.11 and 3.12. The container smoke test builds the
image, verifies it runs as non-root, starts it with a persistent volume, checks HTTP security
headers, runs SQLite integrity and backup commands, and validates the restored backup:

```bash
./scripts/container-smoke.sh
```

## Graduation criteria for live trading

Do not add exchange keys until match checks show repeatable exact-asset hedges with positive
executable net APR after costs, and paper positions survive conservative exits (net ≤ 0, funding
flip, drift > 2%, three-hour liquidity deterioration, or 7-day hold). The dashboard requires at
least 30 closed simulations and four observation weeks before marking the evidence eligible for a
live-trading review. That status is evidence for review, not permission to trade. Live execution
should remain a separate, opt-in project boundary.
