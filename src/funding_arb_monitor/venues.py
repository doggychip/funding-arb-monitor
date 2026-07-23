from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class HedgeQuote:
    venue: str
    symbol: str
    asset: str
    bid: float
    ask: float
    executable_buy_price: float
    executable_sell_price: float
    bid_depth_usd: float
    ask_depth_usd: float
    fee_bps: float
    captured_at_ms: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000

    @property
    def entry_cost_bps(self) -> float:
        slippage_bps = (self.executable_buy_price / self.mid - 1) * 10_000
        return self.fee_bps + max(slippage_bps, self.spread_bps / 2)

    @property
    def exit_cost_bps(self) -> float:
        slippage_bps = (1 - self.executable_sell_price / self.mid) * 10_000
        return self.fee_bps + max(slippage_bps, self.spread_bps / 2)


class PublicJsonClient:
    def __init__(self, timeout: int = 15, attempts: int = 3) -> None:
        self.timeout = timeout
        self.attempts = attempts

    def get(self, url: str) -> object:
        request = urllib.request.Request(url, headers={"User-Agent": "funding-arb-monitor/0.1"})
        for attempt in range(self.attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if (exc.code != 429 and exc.code < 500) or attempt == self.attempts - 1:
                    raise RuntimeError(f"public venue HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == self.attempts - 1:
                    raise RuntimeError(f"public venue request failed: {getattr(exc, 'reason', exc)}") from exc
            time.sleep(2**attempt)
        raise RuntimeError("public venue request failed after retries")


def _depth_usd(levels: list[list[object]]) -> float:
    return sum(float(level[0]) * float(level[1]) for level in levels)


def _average_price(levels: list[list[object]], notional_usd: float) -> float | None:
    remaining = notional_usd
    quantity = 0.0
    value = 0.0
    for level in levels:
        price, size = float(level[0]), float(level[1])
        take_value = min(price * size, remaining)
        quantity += take_value / price
        value += take_value
        remaining -= take_value
        if remaining <= 1e-9:
            return value / quantity
    return None


class CoinbaseSpot:
    name = "coinbase"
    fee_bps = 60.0
    base_url = "https://api.exchange.coinbase.com"

    def __init__(self, get_json: Callable[[str], object] | None = None) -> None:
        self.get_json = get_json or PublicJsonClient().get
        self._products: object | None = None

    def quote(self, asset: str, notional_usd: float) -> HedgeQuote | None:
        if self._products is None:
            self._products = self.get_json(f"{self.base_url}/products")
        products = self._products
        if not isinstance(products, list):
            return None
        matches = [
            product
            for product in products
            if isinstance(product, dict)
            and product.get("base_currency") == asset
            and product.get("quote_currency") in {"USD", "USDC"}
            and product.get("status") == "online"
            and not product.get("trading_disabled", False)
        ]
        if not matches:
            return None
        product = sorted(matches, key=lambda item: item["quote_currency"] != "USD")[0]
        symbol = str(product["id"])
        book = self.get_json(f"{self.base_url}/products/{urllib.parse.quote(symbol)}/book?level=2")
        return self._quote_from_book(asset, symbol, book, notional_usd)

    def _quote_from_book(
        self, asset: str, symbol: str, book: object, notional_usd: float
    ) -> HedgeQuote | None:
        if not isinstance(book, dict):
            return None
        bids, asks = book.get("bids", []), book.get("asks", [])
        if not bids or not asks:
            return None
        buy = _average_price(asks, notional_usd)
        sell = _average_price(bids, notional_usd)
        if buy is None or sell is None:
            return None
        return HedgeQuote(
            venue=self.name,
            symbol=symbol,
            asset=asset,
            bid=float(bids[0][0]),
            ask=float(asks[0][0]),
            executable_buy_price=buy,
            executable_sell_price=sell,
            bid_depth_usd=_depth_usd(bids),
            ask_depth_usd=_depth_usd(asks),
            fee_bps=self.fee_bps,
            captured_at_ms=int(time.time() * 1000),
        )


class KrakenSpot:
    name = "kraken"
    fee_bps = 40.0
    base_url = "https://api.kraken.com/0/public"

    def __init__(self, get_json: Callable[[str], object] | None = None) -> None:
        self.get_json = get_json or PublicJsonClient().get
        self._pairs: object | None = None

    def quote(self, asset: str, notional_usd: float) -> HedgeQuote | None:
        if self._pairs is None:
            self._pairs = self.get_json(f"{self.base_url}/AssetPairs")
        payload = self._pairs
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        matches: list[tuple[str, dict[str, object]]] = []
        for pair_id, pair in result.items():
            if not isinstance(pair, dict) or pair.get("status") != "online":
                continue
            wsname = str(pair.get("wsname") or "")
            if "/" not in wsname:
                continue
            base, quote = wsname.split("/", 1)
            if base == asset and quote in {"USD", "USDC"}:
                matches.append((pair_id, pair))
        if not matches:
            return None
        pair_id, pair = sorted(matches, key=lambda item: str(item[1]["wsname"]).endswith("/USDC"))[0]
        symbol = str(pair.get("altname") or pair_id)
        depth_url = f"{self.base_url}/Depth?{urllib.parse.urlencode({'pair': symbol, 'count': 50})}"
        depth_payload = self.get_json(depth_url)
        depth_result = depth_payload.get("result", {}) if isinstance(depth_payload, dict) else {}
        book = next(iter(depth_result.values()), None)
        return self._quote_from_book(asset, symbol, book, notional_usd)

    def _quote_from_book(
        self, asset: str, symbol: str, book: object, notional_usd: float
    ) -> HedgeQuote | None:
        if not isinstance(book, dict):
            return None
        bids, asks = book.get("bids", []), book.get("asks", [])
        if not bids or not asks:
            return None
        buy = _average_price(asks, notional_usd)
        sell = _average_price(bids, notional_usd)
        if buy is None or sell is None:
            return None
        return HedgeQuote(
            venue=self.name,
            symbol=symbol,
            asset=asset,
            bid=float(bids[0][0]),
            ask=float(asks[0][0]),
            executable_buy_price=buy,
            executable_sell_price=sell,
            bid_depth_usd=_depth_usd(bids),
            ask_depth_usd=_depth_usd(asks),
            fee_bps=self.fee_bps,
            captured_at_ms=int(time.time() * 1000),
        )
