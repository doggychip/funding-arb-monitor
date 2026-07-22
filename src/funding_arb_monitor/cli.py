from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .alerts import render_alert, send_webhook
from .api import create_app
from .hyperliquid import HyperliquidClient
from .scanner import ScanConfig, Scanner
from .store import Store


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Read-only Hyperliquid funding-carry monitor")
    command.add_argument("--db", default=os.getenv("FUNDING_ARB_DB", "data/funding_arb.db"))
    subcommands = command.add_subparsers(dest="command", required=True)

    scan = subcommands.add_parser("scan", help="fetch public data, persist it, and evaluate candidates")
    scan.add_argument("--days", type=int, default=30)
    scan.add_argument("--min-oi", type=float, default=1_000_000)
    scan.add_argument("--max-history-fetches", type=int, default=40)
    scan.add_argument("--min-7d-apr", type=float, default=15)
    scan.add_argument("--max-negative-share", type=float, default=25)
    scan.add_argument("--min-day-volume", type=float, default=500_000)
    scan.add_argument("--alert", action="store_true", help="send generic webhook alert if configured")
    scan.add_argument("--json", action="store_true", help="print full JSON instead of a summary")

    serve = subcommands.add_parser("serve", help="serve the read-only candidates API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    return command


def main() -> None:
    args = parser().parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(create_app(args.db), host=args.host, port=args.port)
        return

    config = ScanConfig(
        days=args.days,
        min_open_interest_usd=args.min_oi,
        max_history_fetches=args.max_history_fetches,
        min_realized_7d_apr_pct=args.min_7d_apr,
        max_negative_hour_share_pct=args.max_negative_share,
        min_day_volume_usd=args.min_day_volume,
    )
    results = Scanner(HyperliquidClient(), Store(Path(args.db)), config).run()
    if args.alert:
        delivered = send_webhook(render_alert(results))
        print(f"alert_delivered={delivered}")
    if args.json:
        print(json.dumps([item.as_dict() for item in results], indent=2))
        return

    print(f"analyzed={len(results)} eligible={sum(item.eligible for item in results)}")
    for item in results:
        status = "ELIGIBLE" if item.eligible else ",".join(item.reasons)
        seven_day = item.realized_7d_apr_pct
        print(
            f"{item.coin:16} 7d={seven_day if seven_day is not None else float('nan'):+6.1f}% "
            f"OI=${item.open_interest_usd / 1e6:7.1f}M {status}"
        )


if __name__ == "__main__":
    main()
