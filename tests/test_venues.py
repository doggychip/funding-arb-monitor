import pytest

from funding_arb_monitor.venues import CoinbaseSpot, KrakenSpot


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
