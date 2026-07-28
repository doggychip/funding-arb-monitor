# Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the read-only funding-arbitrage monitor operationally unambiguous, durable, access-controlled when configured, execution-realistic in paper mode, continuously verified, and safely deployable from `main`.

**Architecture:** Keep the existing single-container FastAPI, scheduler, and SQLite design. Add explicit actionable-candidate contracts, executable Hyperliquid order-book quotes for paper fills, persisted scheduler health, SQLite backup/integrity primitives, optional read authentication, and CI/container smoke tests without adding runtime dependencies or live-order capabilities.

**Tech Stack:** Python 3.11+, FastAPI, SQLite WAL, pytest, Docker, GitHub Actions, Zeabur.

## Global Constraints

- Remain read-only toward exchanges: no credentials, balances, or live-order code.
- Add no runtime dependency.
- Preserve existing SQLite data through additive initialization only.
- Keep one Zeabur replica and the existing `/data` volume.
- Keep public `/`, `/healthz`, and a sanitized `/readyz`; protect operational APIs only when `FUNDING_ARB_READ_TOKEN` is configured.
- Use tests first for every production behavior change.

---

### Task 1: Unambiguous actionable opportunities

**Files:**
- Modify: `src/funding_arb_monitor/api.py`
- Modify: `src/funding_arb_monitor/store.py`
- Modify: `src/funding_arb_monitor/static/index.html`
- Modify: `tests/test_api.py`

**Interfaces:**
- Produces: `GET /api/opportunities/actionable`
- Produces: candidate fields `scan_id`, `analysis_age_seconds`, and `actionable_now`
- Preserves: `GET /api/candidates` as the historical/latest-per-market view

- [ ] Write tests proving an older eligible candidate is visible historically but absent from `/api/opportunities/actionable`.
- [ ] Run the focused tests and verify they fail because the route and fields do not exist.
- [ ] Persist candidate-to-scan identity and compute `actionable_now` only for candidates from the latest successful scan.
- [ ] Add the dedicated route and display the distinction in the dashboard.
- [ ] Run focused and full tests.

### Task 2: Executable two-leg paper quotes

**Files:**
- Modify: `src/funding_arb_monitor/hyperliquid.py`
- Modify: `src/funding_arb_monitor/models.py`
- Modify: `src/funding_arb_monitor/matcher.py`
- Modify: `src/funding_arb_monitor/store.py`
- Modify: `tests/test_hyperliquid.py`
- Modify: `tests/test_matcher.py`
- Modify: `tests/test_paper.py`

**Interfaces:**
- Produces: `HyperliquidClient.perp_quote(coin, dex, notional_usd) -> PerpQuote | None`
- Produces: volume-weighted entry/exit prices, depth, spread, timestamp, and fillability persisted with recommendations and positions
- Rejects: insufficient perp depth, stale quotes, or adverse two-leg drift

- [ ] Write tests with literal order-book fixtures for VWAP and insufficient depth.
- [ ] Run focused tests and verify failure because `perp_quote` is absent.
- [ ] Implement minimal L2 parsing and executable VWAP without new dependencies.
- [ ] Write matcher tests proving mark-only entries and stale/insufficient perp books are rejected.
- [ ] Run focused tests and verify failure.
- [ ] Persist and use the executable perp price, depth, spread, and quote time; add a conservative legging buffer and configurable notional sensitivity to match-check output.
- [ ] Run focused and full tests.

### Task 3: Scheduler-aware readiness

**Files:**
- Modify: `src/funding_arb_monitor/scheduler.py`
- Modify: `src/funding_arb_monitor/store.py`
- Modify: `src/funding_arb_monitor/api.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Produces: persisted `scheduled_job_runs`
- Produces: `Store.start_scheduled_job`, `Store.finish_scheduled_job`, and `Store.scheduler_health`
- Extends: `/readyz` to require writable storage, database integrity, recent scan, and non-overdue critical jobs after their first expected slot

- [ ] Write scheduler tests proving start/completion/exit/duration persistence.
- [ ] Run and verify failure because job-run persistence is absent.
- [ ] Add the table and minimal store methods, then record all job outcomes.
- [ ] Write readiness tests for failed/overdue jobs and sanitized public errors.
- [ ] Run and verify failure.
- [ ] Implement scheduler-aware readiness and expose detailed state only through the protected status endpoint.
- [ ] Run focused and full tests.

### Task 4: SQLite durability and backup

**Files:**
- Create: `src/funding_arb_monitor/maintenance.py`
- Modify: `src/funding_arb_monitor/store.py`
- Modify: `src/funding_arb_monitor/cli.py`
- Modify: `src/funding_arb_monitor/scheduler.py`
- Create: `tests/test_maintenance.py`
- Modify: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `funding-arb-monitor maintenance check`
- Produces: `funding-arb-monitor maintenance backup --destination PATH`
- Configures: foreign keys, busy timeout, WAL, and bounded WAL checkpointing
- Produces: daily local online backup job; optional external copy command remains deployment-specific and documented

- [ ] Write real-SQLite tests for foreign keys, busy timeout, integrity check, online backup, and restoration.
- [ ] Run and verify failure because maintenance commands are absent.
- [ ] Implement SQLite connection pragmas and backup/check functions using the standard library.
- [ ] Add CLI and daily scheduler entry.
- [ ] Run focused and full tests, including opening the restored database.

### Task 5: Optional operational API authentication

**Files:**
- Modify: `src/funding_arb_monitor/api.py`
- Modify: `src/funding_arb_monitor/static/index.html`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `FUNDING_ARB_READ_TOKEN`
- Keeps public: `/`, `/healthz`, `/readyz`
- Protects when configured: `/api/status`, candidates/opportunities, paper data, and alert deliveries
- Produces: security headers and `Cache-Control: no-store` for API responses

- [ ] Write tests proving public probes remain public, operational APIs require the read token only when configured, and responses carry security/cache headers.
- [ ] Run and verify failure.
- [ ] Add a reusable FastAPI dependency and response middleware using constant-time token comparison.
- [ ] Update dashboard fetches to retain the read token in session storage without embedding it in URLs.
- [ ] Run focused and full tests.

### Task 6: CI, container, migration, and restore smoke checks

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `scripts/container-smoke.sh`
- Modify: `Dockerfile`
- Modify: `.dockerignore`
- Modify: `README.md`

**Interfaces:**
- CI runs: unit tests on Python 3.11 and 3.12, Docker build, container health smoke, schema-upgrade smoke, backup/restore smoke
- Container runs as a non-root user while retaining write access to `/data`

- [ ] Create an executable smoke script that starts the image with a temporary volume, waits for health, validates security headers, runs maintenance check/backup, and restores the backup.
- [ ] Run the script against the current image and confirm its non-root assertion fails.
- [ ] Update Dockerfile ownership/user configuration and add `.dockerignore`.
- [ ] Add GitHub Actions workflow calling the same commands.
- [ ] Run unit and container smoke checks locally.

### Task 7: Deployment alignment and documentation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Zeabur watches `main`
- Documents: current versus historical API contracts, read token, readiness semantics, backup retention/restore drill, and evidence gate

- [ ] Update documentation with exact configuration and recovery commands.
- [ ] Review Zeabur’s current service branch and change it to `main` using supported tooling or the dashboard.
- [ ] Verify the service reports `main` before deployment.

### Task 8: Final verification, merge, push, and live deployment

**Files:**
- Review all changed files

**Interfaces:**
- Produces: passing full suite, successful Docker smoke, clean diff, pushed `main`, running Zeabur deployment, and verified public/private endpoint behavior

- [ ] Run the full pytest suite from a clean process.
- [ ] Build and run the container smoke suite.
- [ ] Run migration and restore verification against a copy of the existing local database.
- [ ] Review `git diff --check`, status, and changed-file scope.
- [ ] Commit the feature branch and merge it into `main` without discarding unrelated files.
- [ ] Push `main`, deploy to Zeabur, wait for the exact deployment to reach `RUNNING`, then verify `/healthz`, `/readyz`, actionable opportunities, scheduler health, security headers, and persistence.
