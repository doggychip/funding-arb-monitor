from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterable

from .models import Candidate


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


def send_discord_alert(message: str, webhook_url: str | None = None) -> bool:
    url = webhook_url or os.getenv("FUNDING_ARB_DISCORD_WEBHOOK_URL")
    if not url:
        return False
    request = urllib.request.Request(
        url,
        data=json.dumps(
            {"content": message[:2_000], "allowed_mentions": {"parse": []}}
        ).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "funding-arb-monitor/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            return True
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
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


def render_scan_failure(error: Exception) -> str:
    return f"🚨 **Funding monitor scan failed**\n`{type(error).__name__}: {error}`"
