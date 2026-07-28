from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import Message
from typing import Callable

from .models import FundingPoint, MarketSnapshot, PerpQuote


class HyperliquidClient:
    """Public, read-only Hyperliquid info API client. Never accepts credentials."""

    def __init__(
        self,
        endpoint: str = "https://api.hyperliquid.xyz/info",
        timeout: int = 20,
        max_attempts: int = 4,
        request_interval_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.request_interval_seconds = request_interval_seconds
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request_at: float | None = None

    def post(self, payload: dict[str, object]) -> object:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "funding-arb-monitor/0.1"},
        )
        for attempt in range(self.max_attempts):
            self._throttle()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:300]
                if exc.code != 429 and exc.code < 500:
                    raise RuntimeError(f"Hyperliquid HTTP {exc.code}: {detail}") from exc
                if attempt == self.max_attempts - 1:
                    raise RuntimeError(f"Hyperliquid HTTP {exc.code}: {detail}") from exc
                self.sleep(self._retry_delay(attempt, exc.headers))
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == self.max_attempts - 1:
                    reason = getattr(exc, "reason", exc)
                    raise RuntimeError(f"Hyperliquid request failed: {reason}") from exc
                self.sleep(self._retry_delay(attempt))
        raise RuntimeError("Hyperliquid request failed after retries")

    def _throttle(self) -> None:
        now = self.monotonic()
        if self._last_request_at is not None:
            remaining = self.request_interval_seconds - (now - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
                now = self.monotonic()
        self._last_request_at = now

    @staticmethod
    def _retry_delay(attempt: int, headers: Message | None = None) -> float:
        if headers is not None:
            retry_after = headers.get("Retry-After")
            if retry_after:
                try:
                    return max(float(retry_after), 1.0)
                except ValueError:
                    pass
        return float(2**attempt)

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

    def market_snapshot(self, coin: str, dex: str) -> MarketSnapshot | None:
        payload: dict[str, object] = {"type": "metaAndAssetCtxs"}
        if dex != "(main)":
            payload["dex"] = dex
        data = self.post(payload)
        try:
            universe, contexts = data[0].get("universe", []), data[1]
        except (IndexError, TypeError, AttributeError) as exc:
            raise RuntimeError("invalid Hyperliquid market snapshot response") from exc
        captured_at = datetime.now().astimezone()
        for asset, context in zip(universe, contexts):
            if (
                not isinstance(asset, dict)
                or not isinstance(context, dict)
                or asset.get("name") != coin
                or asset.get("isDelisted")
            ):
                continue
            try:
                mark = float(context["markPx"])
                return MarketSnapshot(
                    dex=dex,
                    coin=coin,
                    funding_rate=float(context.get("funding") or 0),
                    open_interest_usd=float(context.get("openInterest") or 0) * mark,
                    day_volume_usd=float(context.get("dayNtlVlm") or 0),
                    mark_price=mark,
                    captured_at=captured_at,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("invalid Hyperliquid market fields") from exc
        return None

    def perp_quote(
        self, coin: str, dex: str, notional_usd: float
    ) -> PerpQuote | None:
        payload: dict[str, object] = {"type": "l2Book", "coin": coin}
        if dex != "(main)":
            payload["dex"] = dex
        data = self.post(payload)
        try:
            levels = data["levels"]
            bids, asks = levels[0], levels[1]
            bid = float(bids[0]["px"])
            ask = float(asks[0]["px"])
            sell_price, bid_depth = self._book_vwap(bids, notional_usd)
            buy_price, ask_depth = self._book_vwap(asks, notional_usd)
            captured_at_ms = int(data["time"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("invalid Hyperliquid order book response") from exc
        if sell_price is None or buy_price is None:
            return None
        return PerpQuote(
            coin=coin,
            dex=dex,
            bid=bid,
            ask=ask,
            executable_sell_price=sell_price,
            executable_buy_price=buy_price,
            bid_depth_usd=bid_depth,
            ask_depth_usd=ask_depth,
            captured_at_ms=captured_at_ms,
        )

    @staticmethod
    def _book_vwap(
        levels: list[dict[str, object]], notional_usd: float
    ) -> tuple[float | None, float]:
        remaining = notional_usd
        filled_quantity = 0.0
        filled_notional = 0.0
        total_depth = 0.0
        for level in levels:
            price = float(level["px"])
            quantity = float(level["sz"])
            level_notional = price * quantity
            total_depth += level_notional
            take_notional = min(remaining, level_notional)
            filled_notional += take_notional
            filled_quantity += take_notional / price
            remaining -= take_notional
            if remaining <= 1e-9:
                break
        if remaining > 1e-9 or filled_quantity == 0:
            return None, total_depth
        return filled_notional / filled_quantity, total_depth

    def funding_history(
        self,
        coin: str,
        days: int,
        start_time_ms: int | None = None,
    ) -> list[FundingPoint]:
        window_start_ms = int((time.time() - days * 86_400) * 1000)
        start_ms = max(start_time_ms or window_start_ms, window_start_ms)
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
