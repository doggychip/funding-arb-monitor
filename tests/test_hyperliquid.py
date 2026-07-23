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
