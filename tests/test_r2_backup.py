from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from funding_arb_monitor.r2_backup import (
    R2Config,
    create_r2_client,
    download_backup,
    remote_object_key,
    r2_config_from_env,
    upload_backup,
)
from funding_arb_monitor.maintenance import integrity_check
from funding_arb_monitor.store import Store


KEY = "funding-arb-monitor/2026/07/28/source.db"
CONFIG = R2Config("account", "access", "secret", "backups")


class FakeS3Client:
    def __init__(self, objects: dict[str, dict[str, Any]] | None = None) -> None:
        self.put_requests: list[dict[str, Any]] = []
        self.objects = objects or {}

    def put_object(self, **kwargs: Any) -> None:
        body = kwargs.pop("Body")
        kwargs["BodyBytes"] = body.read()
        self.put_requests.append(kwargs)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        return self.objects[Key]


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


def test_create_r2_client_uses_cloudflare_endpoint_and_credentials(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    client = object()

    def create_client(service: str, **kwargs: str) -> object:
        calls.append((service, kwargs))
        return client

    monkeypatch.setattr("boto3.client", create_client)

    result = create_r2_client(CONFIG)

    assert result is client
    assert calls == [
        (
            "s3",
            {
                "endpoint_url": "https://account.r2.cloudflarestorage.com",
                "aws_access_key_id": "access",
                "aws_secret_access_key": "secret",
                "region_name": "auto",
            },
        )
    ]


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


def test_download_backup_verifies_checksum_and_sqlite(tmp_path: Path) -> None:
    source = Store(tmp_path / "source.db")
    source.initialize()
    payload = source.path.read_bytes()
    checksum = hashlib.sha256(payload).hexdigest()
    client = FakeS3Client(
        objects={
            KEY: {
                "Body": io.BytesIO(payload),
                "Metadata": {"sha256": checksum},
            }
        }
    )
    destination = tmp_path / "restore.db"

    result = download_backup(KEY, destination, CONFIG, client=client)

    assert result == destination
    assert integrity_check(destination) == "ok"


def test_download_backup_refuses_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "restore.db"
    destination.write_text("keep me")

    with pytest.raises(FileExistsError):
        download_backup(KEY, destination, CONFIG, client=FakeS3Client())

    assert destination.read_text() == "keep me"


def test_download_backup_does_not_overwrite_destination_created_during_download(
    tmp_path: Path,
) -> None:
    source = Store(tmp_path / "source.db")
    source.initialize()
    payload = source.path.read_bytes()
    destination = tmp_path / "restore.db"

    class DestinationAppearingBody(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            destination.write_bytes(b"live database")
            return super().read(size)

    client = FakeS3Client(
        objects={
            KEY: {
                "Body": DestinationAppearingBody(payload),
                "Metadata": {"sha256": hashlib.sha256(payload).hexdigest()},
            }
        }
    )

    with pytest.raises(FileExistsError):
        download_backup(KEY, destination, CONFIG, client=client)

    assert destination.read_bytes() == b"live database"
    assert not destination.with_name("restore.db.part").exists()


def test_download_backup_removes_partial_when_checksum_metadata_missing(tmp_path: Path) -> None:
    destination = tmp_path / "restore.db"
    body = io.BytesIO(b"payload")
    client = FakeS3Client(objects={KEY: {"Body": body}})

    with pytest.raises(RuntimeError, match="no sha256 metadata"):
        download_backup(KEY, destination, CONFIG, client=client)

    assert not destination.exists()
    assert not destination.with_name("restore.db.part").exists()
    assert body.closed


def test_download_backup_removes_partial_when_checksum_mismatches(tmp_path: Path) -> None:
    destination = tmp_path / "restore.db"
    client = FakeS3Client(
        objects={
            KEY: {
                "Body": io.BytesIO(b"payload"),
                "Metadata": {"sha256": "a" * 64},
            }
        }
    )

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        download_backup(KEY, destination, CONFIG, client=client)

    assert not destination.exists()
    assert not destination.with_name("restore.db.part").exists()


def test_download_backup_removes_partial_when_sqlite_is_invalid(tmp_path: Path) -> None:
    payload = b"not a sqlite database"
    destination = tmp_path / "restore.db"
    client = FakeS3Client(
        objects={
            KEY: {
                "Body": io.BytesIO(payload),
                "Metadata": {"sha256": hashlib.sha256(payload).hexdigest()},
            }
        }
    )

    with pytest.raises(RuntimeError, match="integrity check failed"):
        download_backup(KEY, destination, CONFIG, client=client)

    assert not destination.exists()
    assert not destination.with_name("restore.db.part").exists()
