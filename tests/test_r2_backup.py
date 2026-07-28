from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from funding_arb_monitor.r2_backup import (
    R2Config,
    remote_object_key,
    r2_config_from_env,
    upload_backup,
)


class FakeS3Client:
    def __init__(self) -> None:
        self.put_requests: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        body = kwargs.pop("Body")
        kwargs["BodyBytes"] = body.read()
        self.put_requests.append(kwargs)


def test_r2_config_is_optional_only_when_all_values_are_absent() -> None:
    assert r2_config_from_env({}) is None

    with pytest.raises(ValueError, match="FUNDING_ARB_R2_SECRET_ACCESS_KEY"):
        r2_config_from_env(
            {
                "FUNDING_ARB_R2_ACCOUNT_ID": "account",
                "FUNDING_ARB_R2_ACCESS_KEY_ID": "access",
                "FUNDING_ARB_R2_BUCKET": "backups",
            }
        )


def test_remote_object_key_uses_utc_date_and_filename(tmp_path: Path) -> None:
    path = tmp_path / "funding_arb-20260728T092000Z.db"
    now = datetime(2026, 7, 28, 9, 20, tzinfo=timezone.utc)

    assert remote_object_key(path, now) == (
        "funding-arb-monitor/2026/07/28/funding_arb-20260728T092000Z.db"
    )


def test_upload_backup_sends_checksum_metadata(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.db"
    path.write_bytes(b"verified backup")
    client = FakeS3Client()
    config = R2Config("account", "access", "secret", "backups")

    key = upload_backup(
        path,
        config,
        client=client,
        now=datetime(2026, 7, 28, tzinfo=timezone.utc),
    )

    request = client.put_requests[0]
    assert key == "funding-arb-monitor/2026/07/28/snapshot.db"
    assert request["Bucket"] == "backups"
    assert request["Key"] == key
    assert request["Metadata"]["sha256"] == hashlib.sha256(
        b"verified backup"
    ).hexdigest()
    assert request["BodyBytes"] == b"verified backup"


def test_upload_backup_propagates_client_error(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.db"
    path.write_bytes(b"verified backup")
    config = R2Config("account", "access", "secret", "backups")

    class FailingClient:
        def put_object(self, **kwargs: Any) -> None:
            raise RuntimeError("R2 unavailable")

    with pytest.raises(RuntimeError, match="R2 unavailable"):
        upload_backup(path, config, client=FailingClient())
