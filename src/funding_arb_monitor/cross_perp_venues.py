from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Protocol

from funding_arb_monitor.venues import PublicJsonClient


@dataclass(frozen=True)
class PerpInstrument:
    venue: str
    asset: str
    symbol: str


@dataclass(frozen=True)
class PerpFundingEvent:
    timestamp_ms: int
    funding_rate: float


@dataclass(frozen=True)
class PerpBookQuote:
    venue: str
    asset: str
    symbol: str
    bid: float
    ask: float
    executable_buy_price: float | None
    executable_sell_price: float | None
    bid_depth_usd: float
    ask_depth_usd: float
    fee_bps: float
    captured_at_ms: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class ExternalPerpMarket:
    instrument: PerpInstrument
    current_funding_rate: float
    mark_price: float
    funding_captured_at_ms: int
    funding_events: tuple[PerpFundingEvent, ...]
    quote: PerpBookQuote


class ExternalPerpVenue(Protocol):
    name: str

    def instruments(self) -> dict[str, PerpInstrument]: ...

    def market(
        self, instrument: PerpInstrument, *, days: int, notional_usd: float
    ) -> ExternalPerpMarket: ...


def _book_vwap(levels: object, notional_usd: float) -> tuple[float | None, float]:
    if not isinstance(levels, list) or not levels:
        raise RuntimeError("invalid perpetual order book response")
    remaining = notional_usd
    filled_quantity = 0.0
    filled_notional = 0.0
    total_depth = 0.0
    try:
        for level in levels:
            if not isinstance(level, list):
                raise TypeError
            price, quantity = float(level[0]), float(level[1])
            if price <= 0 or quantity < 0:
                raise ValueError
            level_notional = price * quantity
            total_depth += level_notional
            take_notional = min(remaining, level_notional)
            filled_notional += take_notional
            filled_quantity += take_notional / price
            remaining -= take_notional
            if remaining <= 1e-9:
                break
    except (IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("invalid perpetual order book response") from exc
    if remaining > 1e-9 or filled_quantity == 0:
        return None, total_depth
    return filled_notional / filled_quantity, total_depth


def _quote_from_book(
    *,
    venue: str,
    asset: str,
    symbol: str,
    fee_bps: float,
    book: object,
    notional_usd: float,
) -> PerpBookQuote:
    if not isinstance(book, dict):
        raise RuntimeError("invalid perpetual order book response")
    try:
        bids, asks = book["bids"], book["asks"]
        bid = float(bids[0][0])
        ask = float(asks[0][0])
        captured_at_ms = int(book.get("T", book.get("ts")))
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("invalid perpetual order book response") from exc
    executable_sell_price, bid_depth_usd = _book_vwap(bids, notional_usd)
    executable_buy_price, ask_depth_usd = _book_vwap(asks, notional_usd)
    return PerpBookQuote(
        venue=venue,
        asset=asset,
        symbol=symbol,
        bid=bid,
        ask=ask,
        executable_buy_price=executable_buy_price,
        executable_sell_price=executable_sell_price,
        bid_depth_usd=bid_depth_usd,
        ask_depth_usd=ask_depth_usd,
        fee_bps=fee_bps,
        captured_at_ms=captured_at_ms,
    )


class BinancePerpVenue:
    name = "binance"
    fee_bps = 5.0
    base_url = "https://fapi.binance.com"

    def __init__(self, get_json: Callable[[str], object] | None = None) -> None:
        self.get_json = get_json or PublicJsonClient().get
        self._exchange_info: object | None = None

    def instruments(self) -> dict[str, PerpInstrument]:
        if self._exchange_info is None:
            self._exchange_info = self.get_json(f"{self.base_url}/fapi/v1/exchangeInfo")
        payload = self._exchange_info
        symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
        return {
            str(symbol["baseAsset"]): PerpInstrument(
                venue=self.name,
                asset=str(symbol["baseAsset"]),
                symbol=str(symbol["symbol"]),
            )
            for symbol in symbols
            if isinstance(symbol, dict)
            and symbol.get("contractType") == "PERPETUAL"
            and symbol.get("status") == "TRADING"
            and symbol.get("quoteAsset") == "USDT"
        }

    def market(
        self, instrument: PerpInstrument, *, days: int, notional_usd: float
    ) -> ExternalPerpMarket:
        symbol_query = urllib.parse.urlencode({"symbol": instrument.symbol})
        start_time_ms = int((time.time() - days * 86_400) * 1000)
        history_query = urllib.parse.urlencode(
            {"symbol": instrument.symbol, "startTime": start_time_ms, "limit": 1000}
        )
        depth_query = urllib.parse.urlencode({"symbol": instrument.symbol, "limit": 500})
        premium = self.get_json(f"{self.base_url}/fapi/v1/premiumIndex?{symbol_query}")
        history = self.get_json(f"{self.base_url}/fapi/v1/fundingRate?{history_query}")
        book = self.get_json(f"{self.base_url}/fapi/v1/depth?{depth_query}")
        try:
            if not isinstance(premium, dict) or not isinstance(history, list):
                raise TypeError
            if not all(isinstance(row, dict) for row in history):
                raise TypeError
            funding_events = tuple(
                PerpFundingEvent(int(row["fundingTime"]), float(row["fundingRate"]))
                for row in history
            )
            market = ExternalPerpMarket(
                instrument=instrument,
                current_funding_rate=float(premium["lastFundingRate"]),
                mark_price=float(premium["markPrice"]),
                funding_captured_at_ms=int(premium["time"]),
                funding_events=funding_events,
                quote=_quote_from_book(
                    venue=self.name,
                    asset=instrument.asset,
                    symbol=instrument.symbol,
                    fee_bps=self.fee_bps,
                    book=book,
                    notional_usd=notional_usd,
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("invalid Binance perpetual market response") from exc
        return market


class OkxPerpVenue:
    name = "okx"
    fee_bps = 5.0
    base_url = "https://www.okx.com/api/v5"

    def __init__(self, get_json: Callable[[str], object] | None = None) -> None:
        self.get_json = get_json or PublicJsonClient().get
        self._instruments: object | None = None

    def instruments(self) -> dict[str, PerpInstrument]:
        if self._instruments is None:
            self._instruments = self.get_json(
                f"{self.base_url}/public/instruments?instType=SWAP"
            )
        payload = self._instruments
        instruments = payload.get("data", []) if isinstance(payload, dict) else []
        matches = [
            instrument
            for instrument in instruments
            if isinstance(instrument, dict)
            and instrument.get("ctType") == "linear"
            and instrument.get("state") == "live"
            and instrument.get("settleCcy") in {"USDT", "USDC"}
        ]
        selected: dict[str, dict[str, object]] = {}
        for instrument in matches:
            underlying = str(instrument.get("uly") or "")
            asset = underlying.split("-", 1)[0]
            if not asset:
                continue
            existing = selected.get(asset)
            if existing is None or (
                instrument.get("settleCcy") == "USDT"
                and existing.get("settleCcy") != "USDT"
            ):
                selected[asset] = instrument
        return {
            asset: PerpInstrument(self.name, asset, str(instrument["instId"]))
            for asset, instrument in selected.items()
        }

    def market(
        self, instrument: PerpInstrument, *, days: int, notional_usd: float
    ) -> ExternalPerpMarket:
        instrument_query = urllib.parse.urlencode({"instId": instrument.symbol})
        funding = self.get_json(f"{self.base_url}/public/funding-rate?{instrument_query}")
        history = self._funding_history(instrument.symbol, days)
        ticker = self.get_json(f"{self.base_url}/market/ticker?{instrument_query}")
        book_query = urllib.parse.urlencode({"instId": instrument.symbol, "sz": 400})
        books = self.get_json(f"{self.base_url}/market/books?{book_query}")
        try:
            if not isinstance(funding, dict) or not isinstance(ticker, dict) or not isinstance(books, dict):
                raise TypeError
            funding_row = funding["data"][0]
            ticker_row = ticker["data"][0]
            book = books["data"][0]
            return ExternalPerpMarket(
                instrument=instrument,
                current_funding_rate=float(funding_row["fundingRate"]),
                mark_price=float(ticker_row["last"]),
                funding_captured_at_ms=int(funding_row["fundingTime"]),
                funding_events=history,
                quote=_quote_from_book(
                    venue=self.name,
                    asset=instrument.asset,
                    symbol=instrument.symbol,
                    fee_bps=self.fee_bps,
                    book=book,
                    notional_usd=notional_usd,
                ),
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("invalid OKX perpetual market response") from exc

    def _funding_history(self, symbol: str, days: int) -> tuple[PerpFundingEvent, ...]:
        window_start_ms = int((time.time() - days * 86_400) * 1000)
        before: int | None = None
        events: list[PerpFundingEvent] = []
        while True:
            query_values: dict[str, object] = {"instId": symbol, "limit": 100}
            if before is not None:
                query_values["before"] = before
            query = urllib.parse.urlencode(query_values)
            payload = self.get_json(f"{self.base_url}/public/funding-rate-history?{query}")
            try:
                if not isinstance(payload, dict) or not isinstance(payload["data"], list):
                    raise TypeError
                rows = payload["data"]
                if not all(isinstance(row, dict) for row in rows):
                    raise TypeError
                page = [
                    PerpFundingEvent(int(row["fundingTime"]), float(row["fundingRate"]))
                    for row in rows
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("invalid OKX funding history response") from exc
            if not page:
                break
            events.extend(event for event in page if event.timestamp_ms >= window_start_ms)
            oldest_timestamp = min(event.timestamp_ms for event in page)
            if oldest_timestamp < window_start_ms:
                break
            before = oldest_timestamp
        return tuple(events)
