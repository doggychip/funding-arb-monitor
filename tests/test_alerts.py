import json
import urllib.error

from funding_arb_monitor.alerts import send_discord_alert


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_discord_alert_uses_discord_payload(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert send_discord_alert("test alert", "https://discord.example/webhook")
    assert captured == {
        "body": {"content": "test alert", "allowed_mentions": {"parse": []}},
        "timeout": 15,
    }


def test_discord_alert_is_disabled_without_webhook(monkeypatch) -> None:
    monkeypatch.delenv("FUNDING_ARB_DISCORD_WEBHOOK_URL", raising=False)
    assert not send_discord_alert("test alert")


def test_discord_alert_retries_and_records_delivery(monkeypatch) -> None:
    attempts = 0
    deliveries = []

    class FakeStore:
        def save_alert_delivery(self, **delivery):
            deliveries.append(delivery)

    def flaky_urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError("temporary")
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", flaky_urlopen)
    monkeypatch.setattr("funding_arb_monitor.alerts.time.sleep", lambda seconds: None)

    assert send_discord_alert(
        "test alert",
        "https://discord.example/webhook",
        store=FakeStore(),
        event_type="test",
    )
    assert attempts == 2
    assert deliveries == [
        {
            "event_type": "test",
            "status": "delivered",
            "attempts": 2,
        }
    ]
