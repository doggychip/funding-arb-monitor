import pytest

from funding_arb_monitor.venues import BinanceSpot, CoinbaseSpot, KrakenSpot, OkxSpot


def test_coinbase_exact_asset_quote_uses_depth() -> None:
    def get_json(url: str) -> object:
        if url.endswith("/products"):
            return [
                {
                    "id": "TEST-USD",
                    "base_currency": "TEST",
                    "quote_currency": "USD",
                    "status": "online",
                    "trading_disabled": False,
                }
            ]
        return {"bids": [["99", "100", 1]], "asks": [["101", "100", 1]]}

    quote = CoinbaseSpot(get_json).quote("TEST", 1_000)

    assert quote is not None
    assert quote.symbol == "TEST-USD"
    assert quote.executable_buy_price == pytest.approx(101)
    assert quote.executable_sell_price == pytest.approx(99)
    assert quote.bid_depth_usd == pytest.approx(9_900)


def test_kraken_exact_asset_quote_uses_wsname() -> None:
    def get_json(url: str) -> object:
        if url.endswith("/AssetPairs"):
            return {
                "result": {
                    "TESTUSD": {
                        "altname": "TESTUSD",
                        "wsname": "TEST/USD",
                        "status": "online",
                    }
                }
            }
        return {"result": {"TESTUSD": {"bids": [["99", "100", 0]], "asks": [["101", "100", 0]]}}}

    quote = KrakenSpot(get_json).quote("TEST", 1_000)

    assert quote is not None
    assert quote.symbol == "TESTUSD"
    assert quote.fee_bps == 40


def test_okx_prefers_exact_asset_usdc_book() -> None:
    def get_json(url: str) -> object:
        if "public/instruments" in url:
            return {
                "data": [
                    {
                        "instId": "TEST-USDT",
                        "baseCcy": "TEST",
                        "quoteCcy": "USDT",
                        "state": "live",
                    },
                    {
                        "instId": "TEST-USDC",
                        "baseCcy": "TEST",
                        "quoteCcy": "USDC",
                        "state": "live",
                    },
                ]
            }
        assert "instId=TEST-USDC" in url
        return {"data": [{"bids": [["99", "100"]], "asks": [["101", "100"]]}]}

    quote = OkxSpot(get_json).quote("TEST", 1_000)

    assert quote is not None
    assert quote.venue == "okx"
    assert quote.symbol == "TEST-USDC"
    assert quote.fee_bps == 10


def test_binance_exact_asset_quote_uses_depth() -> None:
    def get_json(url: str) -> object:
        if url.endswith("/exchangeInfo"):
            return {
                "symbols": [
                    {
                        "symbol": "TESTUSDT",
                        "baseAsset": "TEST",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "isSpotTradingAllowed": True,
                    }
                ]
            }
        assert "symbol=TESTUSDT" in url
        return {"bids": [["99", "100"]], "asks": [["101", "100"]]}

    quote = BinanceSpot(get_json).quote("TEST", 1_000)

    assert quote is not None
    assert quote.venue == "binance"
    assert quote.symbol == "TESTUSDT"
    assert quote.fee_bps == 10
