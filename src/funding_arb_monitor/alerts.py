from __future__ import annotations

import json
import os
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
