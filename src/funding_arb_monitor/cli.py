from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .alerts import (
    render_alert,
    render_daily_heartbeat,
    send_discord_alert,
    send_webhook,
)
from .api import create_app
from .hyperliquid import HyperliquidClient
from .matcher import PaperMatcher
from .paper import PaperLedger, PaperOpenRequest
from .scanner import ScanConfig, Scanner
from .store import Store
from .tracker import PaperPositionTracker


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

    paper = subcommands.add_parser("paper", help="manage simulated paired positions; never places orders")
    paper_commands = paper.add_subparsers(dest="paper_command", required=True)
    open_position = paper_commands.add_parser("open", help="record a simulated paired hedge")
    open_position.add_argument("--coin", required=True)
    open_position.add_argument("--hedge-venue", required=True)
    open_position.add_argument("--notional", type=float, default=1_000)
    open_position.add_argument(
        "--side",
        choices=["short_perp_long_hedge", "long_perp_short_hedge"],
        default="short_perp_long_hedge",
    )
    open_position.add_argument("--notes", default="")
    paper_commands.add_parser("accrue", help="fetch public funding and accrue it to open paper positions")
    paper_commands.add_parser(
        "recommend", help="match eligible candidates to public spot books for approval"
    )
    paper_commands.add_parser(
        "shadow", help="auto-open qualified recommendations as simulated positions only"
    )
    approve = paper_commands.add_parser("approve", help="approve one unexpired paper recommendation")
    approve.add_argument("--id", type=int, required=True)
    paper_commands.add_parser("update", help="mark paired positions and apply conservative exits")
    paper_commands.add_parser("report", help="print paper-position performance")
    paper_commands.add_parser("heartbeat", help="send the daily Discord status heartbeat")
    paper_commands.add_parser("alert-test", help="send a Discord configuration test")
    return command


def main() -> None:
    args = parser().parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(create_app(args.db), host=args.host, port=args.port)
        return

    store = Store(Path(args.db))
    store.initialize()
    if args.command == "paper":
        ledger = PaperLedger(store)
        if args.paper_command == "open":
            position = ledger.open(
                PaperOpenRequest(
                    coin=args.coin,
                    hedge_venue=args.hedge_venue,
                    notional_usd=args.notional,
                    side=args.side,
                    notes=args.notes,
                )
            )
            print(json.dumps(position, indent=2))
            return
        if args.paper_command == "accrue":
            positions = store.open_paper_positions()
            client = HyperliquidClient()
            start_by_coin: dict[str, int] = {}
            for position in positions:
                latest = store.latest_paper_accrual_timestamp(position["id"])
                start = (latest + 1) if latest is not None else position["opened_at_ms"]
                coin = str(position["coin"])
                start_by_coin[coin] = min(start_by_coin.get(coin, start), start)
            history = {
                coin: client.funding_history(coin, 30, start_time_ms=start)
                for coin, start in start_by_coin.items()
            }
            print(f"accrued_positions={ledger.accrue_open_positions(history)}")
            return
        if args.paper_command == "recommend":
            recommendations = PaperMatcher(store).recommend()
            print(json.dumps(recommendations, indent=2))
            return
        if args.paper_command == "shadow":
            print(json.dumps(PaperMatcher(store).shadow(), indent=2))
            return
        if args.paper_command == "approve":
            print(json.dumps(PaperMatcher(store).approve(args.id), indent=2))
            return
        if args.paper_command == "update":
            print(json.dumps(PaperPositionTracker(store).update(), indent=2))
            return
        if args.paper_command == "heartbeat":
            delivered = send_discord_alert(
                render_daily_heartbeat(
                    store.latest_scan_run() or {"status": "never_run"},
                    store.paper_performance(),
                ),
                store=store,
                event_type="daily_heartbeat",
            )
            print(f"discord_heartbeat_delivered={delivered}")
            if not delivered:
                raise SystemExit(1)
            return
        if args.paper_command == "alert-test":
            delivered = send_discord_alert(
                "✅ **Funding monitor Discord test succeeded**\n"
                "Zeabur can deliver operational alerts to this channel.",
                store=store,
                event_type="configuration_test",
            )
            print(f"discord_test_delivered={delivered}")
            if not delivered:
                raise SystemExit(1)
            return
        print(json.dumps({"summary": store.paper_summary(), "positions": store.paper_positions()}, indent=2))
        return

    config = ScanConfig(
        days=args.days,
        min_open_interest_usd=args.min_oi,
        max_history_fetches=args.max_history_fetches,
        min_realized_7d_apr_pct=args.min_7d_apr,
        max_negative_hour_share_pct=args.max_negative_share,
        min_day_volume_usd=args.min_day_volume,
    )
    results = Scanner(HyperliquidClient(), store, config).run()
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
