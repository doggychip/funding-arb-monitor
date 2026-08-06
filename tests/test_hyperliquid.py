import io
import urllib.error
from email.message import Message
from unittest.mock import patch

import pytest

from funding_arb_monitor.hyperliquid import HyperliquidClient


def test_post_retries_429_using_retry_after() -> None:
    headers = Message()
    headers["Retry-After"] = "3"
    rate_limited = urllib.error.HTTPError(
        url="https://api.hyperliquid.xyz/info",
        code=429,
        msg="Too Many Requests",
        hdrs=headers,
        fp=io.BytesIO(b"null"),
    )
    sleeps: list[float] = []
    client = HyperliquidClient(
        max_attempts=2,
        request_interval_seconds=0,
        sleep=sleeps.append,
    )

    with patch(
        "funding_arb_monitor.hyperliquid.urllib.request.urlopen",
        side_effect=[rate_limited, io.BytesIO(b'{"ok": true}')],
    ):
        result = client.post({"type": "perpDexs"})

    assert result == {"ok": True}
    assert sleeps == [3.0]


def test_post_does_not_retry_bad_request() -> None:
    bad_request = urllib.error.HTTPError(
        url="https://api.hyperliquid.xyz/info",
        code=400,
        msg="Bad Request",
        hdrs=Message(),
        fp=io.BytesIO(b"invalid"),
    )
    sleeps: list[float] = []
    client = HyperliquidClient(max_attempts=4, request_interval_seconds=0, sleep=sleeps.append)

    with (
        patch(
            "funding_arb_monitor.hyperliquid.urllib.request.urlopen",
            side_effect=bad_request,
        ),
        pytest.raises(RuntimeError, match="HTTP 400"),
    ):
        client.post({"type": "bad"})

    assert sleeps == []


def test_market_snapshot_fetches_only_requested_dex() -> None:
    client = HyperliquidClient()
    response = [
        {"universe": [{"name": "xyz:TEST"}]},
        [
            {
                "markPx": "105",
                "funding": "0.0001",
                "openInterest": "100",
                "dayNtlVlm": "50000",
            }
        ],
    ]

    with patch.object(client, "post", return_value=response) as post:
        snapshot = client.market_snapshot("xyz:TEST", "xyz")

    post.assert_called_once_with({"type": "metaAndAssetCtxs", "dex": "xyz"})
    assert snapshot is not None
    assert snapshot.mark_price == 105
    assert snapshot.open_interest_usd == 10_500


def test_perp_quote_uses_executable_vwap_for_both_sides() -> None:
    client = HyperliquidClient()
    book = {
        "time": 1_785_212_312_933,
        "levels": [
            [{"px": "99", "sz": "6", "n": 2}, {"px": "98", "sz": "10", "n": 3}],
            [{"px": "101", "sz": "5", "n": 2}, {"px": "102", "sz": "10", "n": 3}],
        ],
    }

    with patch.object(client, "post", return_value=book) as post:
        quote = client.perp_quote("TEST", "(main)", 1_000)

    post.assert_called_once_with({"type": "l2Book", "coin": "TEST"})
    assert quote is not None
    assert quote.executable_sell_price == pytest.approx((99 * 6 + 98 * 4.1428571429) / 10.1428571429)
    assert quote.executable_buy_price == pytest.approx((101 * 5 + 102 * 4.8529411765) / 9.8529411765)
    assert quote.bid_depth_usd == pytest.approx(1_574)
    assert quote.ask_depth_usd == pytest.approx(1_525)
    assert quote.captured_at_ms == 1_785_212_312_933


def test_perp_quote_returns_none_when_either_side_cannot_fill_notional() -> None:
    client = HyperliquidClient()
    book = {
        "time": 1,
        "levels": [
            [{"px": "99", "sz": "2", "n": 1}],
            [{"px": "101", "sz": "20", "n": 1}],
        ],
    }

    with patch.object(client, "post", return_value=book):
        quote = client.perp_quote("TEST", "(main)", 1_000)

    assert quote is None


def test_perp_book_quote_preserves_partial_depth_for_cross_perp() -> None:
    client = HyperliquidClient()
    book = {
        "time": 1,
        "levels": [
            [{"px": "99", "sz": "20", "n": 1}],
            [{"px": "101", "sz": "2", "n": 1}],
        ],
    }
    partial_quote = getattr(client, "perp_book_quote", None)
    assert partial_quote is not None, "cross-perp partial-book reader is missing"

    with patch.object(client, "post", return_value=book):
        quote = partial_quote("TEST", "(main)", 1_000)

    assert quote.executable_sell_price == 99.0
    assert quote.executable_buy_price is None
    assert quote.bid_depth_usd == pytest.approx(1_980.0)
    assert quote.ask_depth_usd == pytest.approx(202.0)


def test_perp_book_quote_reports_depth_beyond_the_fill_level() -> None:
    client = HyperliquidClient()
    book = {
        "time": 1,
        "levels": [
            [
                {"px": "100", "sz": "10", "n": 1},
                {"px": "99", "sz": "10", "n": 1},
                {"px": "98", "sz": "20", "n": 1},
            ],
            [
                {"px": "101", "sz": "10", "n": 1},
                {"px": "102", "sz": "10", "n": 1},
                {"px": "103", "sz": "20", "n": 1},
            ],
        ],
    }

    with patch.object(client, "post", return_value=book):
        quote = client.perp_book_quote("TEST", "(main)", 1_000)

    assert quote.bid_depth_usd == pytest.approx(3_950)
    assert quote.ask_depth_usd == pytest.approx(4_090)
