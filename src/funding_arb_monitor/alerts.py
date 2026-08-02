from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from typing import Protocol

from .models import Candidate


class AlertDeliveryStore(Protocol):
    def save_alert_delivery(
        self,
        *,
        event_type: str,
        status: str,
        attempts: int,
        detail: str | None = None,
    ) -> None: ...


def render_alert(candidates: Iterable[Candidate]) -> str:
    eligible = [item for item in candidates if item.eligible][:10]
    if not eligible:
        return "Funding monitor: no candidates passed the configured carry, reversal, OI, and volume gates."
    lines = ["Funding monitor: eligible gross carry candidates"]
    for item in eligible:
        lines.append(
            f"- {item.coin}: 7d {item.realized_7d_apr_pct:+.1f}% | "
            f"24h {item.realized_24h_apr_pct:+.1f}% | OI ${item.open_interest_usd / 1e6:.1f}M | "
            f"{item.side}"
        )
    lines.append("Read-only alert. Verify fees, borrow, hedge liquidity, and session risk before action.")
    return "\n".join(lines)


def send_webhook(message: str, webhook_url: str | None = None) -> bool:
    """POST a generic JSON webhook only when explicitly configured."""
    url = webhook_url or os.getenv("FUNDING_ARB_WEBHOOK_URL")
    if not url:
        return False
    request = urllib.request.Request(
        url,
        data=json.dumps({"text": message}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "funding-arb-monitor/0.1"},
    )
    with urllib.request.urlopen(request, timeout=15):
        return True


def send_discord_alert(
    message: str,
    webhook_url: str | None = None,
    *,
    store: AlertDeliveryStore | None = None,
    event_type: str = "discord_alert",
    max_attempts: int = 3,
) -> bool:
    url = webhook_url or os.getenv("FUNDING_ARB_DISCORD_WEBHOOK_URL")
    if not url:
        if store is not None:
            store.save_alert_delivery(
                event_type=event_type,
                status="disabled",
                attempts=0,
                detail="webhook not configured",
            )
        return False
    request = urllib.request.Request(
        url,
        data=json.dumps(
            {"content": message[:2_000], "allowed_mentions": {"parse": []}}
        ).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "funding-arb-monitor/0.1"},
    )
    detail: str | None = None
    attempt_limit = max(1, max_attempts)
    for attempt in range(1, attempt_limit + 1):
        try:
            with urllib.request.urlopen(request, timeout=15):
                if store is not None:
                    store.save_alert_delivery(
                        event_type=event_type,
                        status="delivered",
                        attempts=attempt,
                    )
                return True
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            if exc.code < 500 and exc.code != 429:
                break
        except (urllib.error.URLError, TimeoutError) as exc:
            detail = type(exc).__name__
        if attempt < attempt_limit:
            time.sleep(2 ** (attempt - 1))
    if store is not None:
        store.save_alert_delivery(
            event_type=event_type,
            status="failed",
            attempts=attempt,
            detail=detail,
        )
    return False


def render_shadow_entry(position: dict[str, object], net_apr_pct: float) -> str:
    return (
        "📝 **Shadow paper position opened**\n"
        f"Market: `{position['coin']}`\n"
        f"Hedge: `{position['hedge_venue']} {position['hedge_symbol']}`\n"
        f"Notional: `${float(position['notional_usd']):,.0f}`\n"
        f"Executable 7d net APR: `{net_apr_pct:+.1f}%`\n"
        "Simulation only—no exchange order was placed."
    )


def render_shadow_exit(position: dict[str, object]) -> str:
    return (
        "🏁 **Shadow paper position closed**\n"
        f"Market: `{position['coin']}`\n"
        f"Reason: `{str(position['exit_reason']).replace('_', ' ')}`\n"
        f"Funding P&L: `${float(position['funding_pnl_usd']):+.2f}`\n"
        f"Pair MTM: `${float(position['mark_to_market_pnl_usd']):+.2f}`\n"
        f"Net P&L: `${float(position['net_pnl_usd']):+.2f}`"
    )


def render_cross_perp_transitions(events: list[dict[str, object]]) -> str:
    labels = {
        "became_ready": "READY 3/3",
        "lost_ready": "LOST READINESS",
        "depth_deteriorated": "DEPTH DETERIORATED",
        "economics_deteriorated": "ECONOMICS DETERIORATED",
    }
    lines = ["🔎 **Cross-perpetual state changed**"]
    for event in events[:12]:
        net_apr = event.get("net_apr_7d_pct")
        apr = "n/a" if net_apr is None else f"{float(net_apr):+.1f}%"
        lines.append(
            f"• **{labels.get(str(event['event_type']), str(event['event_type']))}** "
            f"`{event['asset']}/{event['external_venue']}` · "
            f"`{event['direction']}` · net APR `{apr}` · "
            f"streak `{event['streak']}/3`"
        )
    lines.append("Observation and simulation only—no order was placed.")
    return "\n".join(lines)


def render_cross_perp_shadow_entry(position: dict[str, object]) -> str:
    return (
        "🧪 **Cross-perpetual shadow position opened**\n"
        f"Route: `{position['asset']}/{position['external_venue']}`\n"
        f"Direction: `{position['direction']}`\n"
        f"Notional per leg: `${float(position['notional_usd']):,.0f}`\n"
        f"Forecast 7d net: `${float(position['forecast_net_profit_usd']):+.2f}`\n"
        "Second public-data preflight passed. Simulation only—no order was placed."
    )


def render_cross_perp_shadow_exit(position: dict[str, object]) -> str:
    return (
        "🏁 **Cross-perpetual shadow position closed**\n"
        f"Route: `{position['asset']}/{position['external_venue']}`\n"
        f"Reason: `{str(position['exit_reason']).replace('_', ' ')}`\n"
        f"Actual net P&L: `${float(position['actual_net_pnl_usd']):+.2f}`\n"
        f"Forecast error: `${float(position['forecast_error_usd']):+.2f}`"
    )


def render_scan_failure(error: Exception) -> str:
    return f"🚨 **Funding monitor scan failed**\n`{type(error).__name__}: {error}`"


def render_scheduler_failure(job_name: str, detail: str) -> str:
    return f"🚨 **Scheduler job failed**\nJob: `{job_name}`\nDetail: `{detail}`"


def render_liquidity_warning(
    position: dict[str, object], reasons: list[str], streak: int
) -> str:
    return (
        "⚠️ **Shadow position liquidity degraded**\n"
        f"Market: `{position['coin']}`\n"
        f"Observation: `{streak}/3`\n"
        f"Reasons: `{', '.join(reason.replace('_', ' ') for reason in reasons)}`\n"
        "The position remains open unless degradation persists for three consecutive hourly checks."
    )


def render_daily_heartbeat(
    status: dict[str, object], performance: dict[str, object]
) -> str:
    graduation = performance["graduation"]
    return (
        "💓 **Funding monitor daily heartbeat**\n"
        f"Latest scan: `{status.get('status', 'unknown')}` · "
        f"{status.get('candidate_count', 0)} candidates · "
        f"{status.get('eligible_count', 0)} eligible\n"
        f"Paper positions: `{performance['open_positions']} open / "
        f"{performance['completed_trades']} closed`\n"
        f"Open net P&L: `${float(performance['open_net_pnl_usd']):+.2f}`\n"
        f"Graduation evidence: `{graduation['closed_trades']}/"
        f"{graduation['required_closed_trades']} trades · "
        f"{float(graduation['observation_weeks']):.1f}/"
        f"{graduation['required_observation_weeks']} weeks`"
    )
