from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from funding_arb_monitor.maintenance import integrity_check


@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


ENV_FIELDS = {
    "FUNDING_ARB_R2_ACCOUNT_ID": "account_id",
    "FUNDING_ARB_R2_ACCESS_KEY_ID": "access_key_id",
    "FUNDING_ARB_R2_SECRET_ACCESS_KEY": "secret_access_key",
    "FUNDING_ARB_R2_BUCKET": "bucket",
}


def r2_config_from_env(environ: Mapping[str, str]) -> R2Config | None:
    values = {name: environ.get(name, "").strip() for name in ENV_FIELDS}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(f"incomplete R2 configuration; missing: {', '.join(missing)}")
    return R2Config(**{field: values[name] for name, field in ENV_FIELDS.items()})


def remote_object_key(path: Path, now: datetime | None = None) -> str:
    timestamp = now or datetime.now(timezone.utc)
    return f"funding-arb-monitor/{timestamp:%Y/%m/%d}/{path.name}"


def create_r2_client(config: R2Config) -> Any:
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name="auto",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_backup(
    path: Path,
    config: R2Config,
    client: Any | None = None,
    now: datetime | None = None,
) -> str:
    object_key = remote_object_key(path, now)
    s3 = client or create_r2_client(config)
    with path.open("rb") as source:
        s3.put_object(
            Bucket=config.bucket,
            Key=object_key,
            Body=source,
            Metadata={"sha256": sha256_file(path)},
        )
    return object_key


def download_backup(
    key: str,
    destination: Path,
    config: R2Config,
    client: Any | None = None,
) -> Path:
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.part")
    if partial.exists():
        raise FileExistsError(partial)

    s3 = client or create_r2_client(config)
    body: Any | None = None
    try:
        response = s3.get_object(Bucket=config.bucket, Key=key)
        body = response["Body"]
        expected = response.get("Metadata", {}).get("sha256")
        if not expected:
            raise RuntimeError("remote backup has no sha256 metadata")
        with partial.open("xb") as target:
            shutil.copyfileobj(body, target)
        if not hmac.compare_digest(sha256_file(partial), expected):
            raise RuntimeError("remote backup checksum mismatch")
        try:
            is_valid = integrity_check(partial) == "ok"
        except sqlite3.DatabaseError:
            is_valid = False
        if not is_valid:
            raise RuntimeError("downloaded backup integrity check failed")
        os.link(partial, destination)
        partial.unlink()
        return destination
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        if body is not None and hasattr(body, "close"):
            body.close()
