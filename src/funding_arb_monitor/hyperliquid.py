from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime

from .models import FundingPoint, MarketSnapshot


class HyperliquidClient:
    """Public, read-only Hyperliquid info API client. Never accepts credentials."""

    def __init__(self, endpoint: str = "https://api.hyperliquid.xyz/info", timeout: int = 20) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    def post(self, payload: dict[str, object]) -> object:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "funding-arb-monitor/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Hyperliquid HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Hyperliquid request failed: {exc.reason}") from exc

    def dex_names(self) -> list[str]:
        data = self.post({"type": "perpDexs"})
        return [
            item["name"]
            for item in data
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ] if isinstance(data, list) else []

    def snapshots(self) -> list[MarketSnapshot]:
        captured_at = datetime.now().astimezone()
        output: list[MarketSnapshot] = []
        for dex in ["", *self.dex_names()]:
            payload: dict[str, object] = {"type": "metaAndAssetCtxs"}
            if dex:
                payload["dex"] = dex
            try:
                data = self.post(payload)
                universe, contexts = data[0].get("universe", []), data[1]
            except (IndexError, TypeError, AttributeError, RuntimeError):
                continue
            for asset, context in zip(universe, contexts):
                if asset.get("isDelisted"):
                    continue
                try:
                    mark = float(context["markPx"])
                    output.append(
                        MarketSnapshot(
                            dex=dex or "(main)",
                            coin=asset["name"],
                            funding_rate=float(context.get("funding") or 0),
                            open_interest_usd=float(context.get("openInterest") or 0) * mark,
                            day_volume_usd=float(context.get("dayNtlVlm") or 0),
                            mark_price=mark,
                            captured_at=captured_at,
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        return output

    def funding_history(self, coin: str, days: int) -> list[FundingPoint]:
        start_ms = int((time.time() - days * 86_400) * 1000)
        points: dict[int, FundingPoint] = {}
        while True:
            data = self.post({"type": "fundingHistory", "coin": coin, "startTime": start_ms})
            if not isinstance(data, list) or not data:
                break
            for row in data:
                try:
                    point = FundingPoint(coin, int(row["time"]), float(row["fundingRate"]))
                    points[point.timestamp_ms] = point
                except (KeyError, TypeError, ValueError):
                    continue
            if len(data) < 500:
                break
            next_start = max(point.timestamp_ms for point in points.values()) + 1
            if next_start <= start_ms:
                break
            start_ms = next_start
        return sorted(points.values(), key=lambda item: item.timestamp_ms)
