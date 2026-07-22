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

The tool still does not model fees, slippage, borrow, FX, underlying market-session gaps, or
your own market impact. Equity perps are not a 24/7 delta-neutral arbitrage when the stock
exchange is shut.

## Run locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

# Fetch and persist a scan; prints only candidates and their rejection reasons.
funding-arb-monitor scan --days 30 --min-oi 1000000

# Run the read-only API, then visit http://127.0.0.1:8080/docs.
funding-arb-monitor serve
```

The API exposes `GET /healthz` and `GET /api/candidates`.

## Alerts

Set `FUNDING_ARB_WEBHOOK_URL` to a webhook that accepts `{"text": "..."}` and add `--alert`:

```bash
export FUNDING_ARB_WEBHOOK_URL="https://example.invalid/webhook"
funding-arb-monitor scan --alert
```

No alert is sent when the variable is absent.

## Scheduling

Run an hourly scan on the host:

```cron
5 * * * * cd /path/to/funding-arb-monitor && /path/to/.venv/bin/funding-arb-monitor scan --days 30 --alert >> data/scanner.log 2>&1
```

Or use the one-shot Compose scanner:

```bash
docker compose --profile scan run --rm scanner
docker compose up --build api
```

## Graduation criteria for paper trading

Do not add exchange keys until the monitor has retained enough scans to validate its gates.
The next phase should be a separate paper-execution service that reconciles both legs, models
fees/borrow/slippage, enforces hedge and liquidation limits, and has a kill switch. Live
execution should remain a separate, opt-in project boundary.
