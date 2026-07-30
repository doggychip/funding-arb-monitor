import time
import urllib.parse

import pytest

from funding_arb_monitor.cross_perp_venues import (
    BinancePerpVenue,
    OkxPerpVenue,
    PerpInstrument,
)


def test_binance_lists_only_live_linear_perpetuals() -> None:
    payload = {
        "symbols": [
            {
                "symbol": "ZROUSDT",
                "baseAsset": "ZRO",
                "quoteAsset": "USDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
            },
            {
                "symbol": "ZROUSDT_260925",
                "baseAsset": "ZRO",
                "quoteAsset": "USDT",
                "contractType": "CURRENT_QUARTER",
                "status": "TRADING",
            },
        ]
    }
    venue = BinancePerpVenue(get_json=lambda _: payload)
    assert venue.instruments()["ZRO"].symbol == "ZROUSDT"


def test_okx_prefers_live_usdt_linear_swap() -> None:
    payload = {
        "data": [
            {
                "instId": "ZRO-USDC-SWAP",
                "uly": "ZRO-USDC",
                "settleCcy": "USDC",
                "ctType": "linear",
                "state": "live",
            },
            {
                "instId": "ZRO-USDT-SWAP",
                "uly": "ZRO-USDT",
                "settleCcy": "USDT",
                "ctType": "linear",
                "state": "live",
            },
        ]
    }
    venue = OkxPerpVenue(get_json=lambda _: payload)
    assert venue.instruments()["ZRO"].symbol == "ZRO-USDT-SWAP"


def test_binance_market_uses_funding_history_and_executable_depth() -> None:
    responses = {
        "/fapi/v1/premiumIndex": {
            "symbol": "ZROUSDT",
            "markPrice": "2.00",
            "lastFundingRate": "0.0001",
            "time": 1_000_000,
        },
        "/fapi/v1/fundingRate": [
            {"fundingTime": 100, "fundingRate": "0.0001"},
            {"fundingTime": 200, "fundingRate": "-0.0002"},
        ],
        "/fapi/v1/depth": {
            "T": 1_000_000,
            "bids": [["1.99", "600"], ["1.98", "100"]],
            "asks": [["2.01", "600"], ["2.02", "100"]],
        },
    }

    def get_json(url: str) -> object:
        return responses[urllib.parse.urlsplit(url).path]

    venue = BinancePerpVenue(get_json=get_json)
    instrument = PerpInstrument("binance", "ZRO", "ZROUSDT")

    market = venue.market(instrument, days=7, notional_usd=1_000)

    assert market.current_funding_rate == 0.0001
    assert market.mark_price == 2.0
    assert market.mark_captured_at_ms == 1_000_000
    assert [point.funding_rate for point in market.funding_events] == [0.0001, -0.0002]
    assert market.quote.executable_buy_price == 2.01
    assert market.quote.executable_sell_price == 1.99
    assert market.quote.bid_depth_usd == pytest.approx(1_392.0)
    assert market.quote.ask_depth_usd == pytest.approx(1_408.0)
    assert market.quote.fee_bps == 5.0


def test_okx_market_paginates_history_with_oldest_timestamp_as_before() -> None:
    history_requests: list[dict[str, list[str]]] = []
    market_data_requests: list[str] = []
    now_ms = int(time.time() * 1000)
    first_timestamp = now_ms - 1_000
    second_timestamp = now_ms - 2_000
    third_timestamp = now_ms - 3_000

    def get_json(url: str) -> object:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/v5/public/funding-rate":
            return {"data": [{"fundingRate": "0.0001", "fundingTime": str(now_ms)}]}
        if parsed.path == "/api/v5/public/funding-rate-history":
            history_requests.append(query)
            before = query.get("before", [None])[0]
            if before is None:
                return {
                    "data": [
                        {"fundingRate": "0.0001", "fundingTime": str(first_timestamp)},
                        {"fundingRate": "-0.0002", "fundingTime": str(second_timestamp)},
                    ]
                }
            if before == str(second_timestamp):
                return {
                    "data": [
                        {"fundingRate": "0.0003", "fundingTime": str(third_timestamp)}
                    ]
                }
            if before == str(third_timestamp):
                return {"data": []}
        if parsed.path == "/api/v5/public/mark-price":
            market_data_requests.append(parsed.path)
            return {
                "data": [
                    {
                        "instId": "ZRO-USDT-SWAP",
                        "instType": "SWAP",
                        "markPx": "2.00",
                        "ts": str(now_ms),
                    }
                ]
            }
        if parsed.path == "/api/v5/market/ticker":
            market_data_requests.append(parsed.path)
            return {"data": [{"last": "9.99", "ts": str(now_ms)}]}
        if parsed.path == "/api/v5/market/books":
            return {
                "data": [
                    {
                        "ts": str(now_ms),
                        "bids": [["1.99", "600", "0", "1"]],
                        "asks": [["2.01", "600", "0", "1"]],
                    }
                ]
            }
        raise AssertionError(f"unexpected URL: {url}")

    venue = OkxPerpVenue(get_json=get_json)
    instrument = PerpInstrument("okx", "ZRO", "ZRO-USDT-SWAP")

    market = venue.market(instrument, days=7, notional_usd=1_000)

    assert market.mark_price == 2.0
    assert market.mark_captured_at_ms == now_ms
    assert [point.funding_rate for point in market.funding_events] == [0.0001, -0.0002, 0.0003]
    assert market.quote.executable_buy_price == 2.01
    assert market.quote.executable_sell_price == 1.99
    assert market.quote.fee_bps == 5.0
    assert [request.get("before", [None])[0] for request in history_requests] == [
        None,
        str(second_timestamp),
        str(third_timestamp),
    ]
    assert market_data_requests == ["/api/v5/public/mark-price"]


def test_market_preserves_visible_depth_when_execution_is_insufficient() -> None:
    responses = {
        "/fapi/v1/premiumIndex": {
            "markPrice": "2.00",
            "lastFundingRate": "0.0001",
            "time": 1_000_000,
        },
        "/fapi/v1/fundingRate": [],
        "/fapi/v1/depth": {
            "T": 1_000_000,
            "bids": [["1.99", "100"]],
            "asks": [["2.01", "100"]],
        },
    }
    venue = BinancePerpVenue(
        get_json=lambda url: responses[urllib.parse.urlsplit(url).path]
    )

    market = venue.market(
        PerpInstrument("binance", "ZRO", "ZROUSDT"), days=7, notional_usd=1_000
    )

    assert market.quote.executable_buy_price is None
    assert market.quote.executable_sell_price is None
    assert market.quote.bid_depth_usd == pytest.approx(199.0)
    assert market.quote.ask_depth_usd == pytest.approx(201.0)


@pytest.mark.parametrize(
    "venue",
    [
        BinancePerpVenue(get_json=lambda _: []),
        OkxPerpVenue(get_json=lambda _: {"data": {}}),
    ],
)
def test_discovery_rejects_malformed_top_level_payloads(venue) -> None:
    with pytest.raises(RuntimeError, match="instrument response"):
        venue.instruments()


@pytest.mark.parametrize(
    "venue",
    [
        BinancePerpVenue(
            get_json=lambda _: {
                "symbols": [
                    {
                        "symbol": "ZROUSDT",
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                    }
                ]
            }
        ),
        OkxPerpVenue(
            get_json=lambda _: {
                "data": [
                    {
                        "ctType": "linear",
                        "state": "live",
                        "settleCcy": "USDT",
                    }
                ]
            }
        ),
    ],
)
def test_discovery_rejects_malformed_matching_records(venue) -> None:
    with pytest.raises(RuntimeError, match="instrument response"):
        venue.instruments()


@pytest.mark.parametrize(
    "venue",
    [
        BinancePerpVenue(
            get_json=lambda _: {
                "symbols": [
                    {
                        "symbol": "ZROUSDT",
                        "baseAsset": None,
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                    }
                ]
            }
        ),
        BinancePerpVenue(
            get_json=lambda _: {
                "symbols": [
                    {
                        "symbol": None,
                        "baseAsset": "ZRO",
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                    }
                ]
            }
        ),
        OkxPerpVenue(
            get_json=lambda _: {
                "data": [
                    {
                        "instId": None,
                        "uly": "ZRO-USDT",
                        "settleCcy": "USDT",
                        "ctType": "linear",
                        "state": "live",
                    }
                ]
            }
        ),
        OkxPerpVenue(
            get_json=lambda _: {
                "data": [
                    {
                        "instId": "ZRO-USDT-SWAP",
                        "uly": None,
                        "settleCcy": "USDT",
                        "ctType": "linear",
                        "state": "live",
                    }
                ]
            }
        ),
    ],
)
def test_discovery_rejects_null_required_matching_fields(venue) -> None:
    with pytest.raises(RuntimeError, match="instrument response"):
        venue.instruments()
