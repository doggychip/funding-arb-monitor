# Cloudflare R2 Offsite Backups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Copy every verified scheduled SQLite backup to a private Cloudflare R2 bucket and provide a safe command to download and verify an offsite backup without touching the live database.

**Architecture:** Keep SQLite backup creation and integrity checking in `maintenance.py`, and isolate S3-compatible configuration, upload, and download behavior in a new `r2_backup.py` module. The maintenance CLI conditionally uploads when complete R2 configuration is present and exposes an explicit verified-download command; existing scheduler failure recording handles remote errors without new scheduler logic.

**Tech Stack:** Python 3.11+, SQLite, boto3 S3 client, pytest, Cloudflare R2, Zeabur.

## Global Constraints

- The R2 bucket is private and has a 30-day object-expiration lifecycle rule.
- Credentials exist only in protected Zeabur variables and are never logged or committed.
- Remote backup object keys use `funding-arb-monitor/YYYY/MM/DD/<filename>`.
- A remote upload includes a SHA-256 checksum in object metadata.
- Missing all R2 variables preserves local-only backup behavior; partial configuration is an error.
- Download refuses to overwrite a file and never replaces the configured live database.
- Add `boto3` as the sole new runtime dependency.
- Do not change the SQLite schema or scheduler timing.

---

## File Structure

- Create `src/funding_arb_monitor/r2_backup.py`: R2 environment configuration, client construction, object-key generation, uploads, and verified downloads.
- Modify `src/funding_arb_monitor/cli.py`: connect backup and download commands to the R2 module.
- Modify `pyproject.toml`: declare the bounded boto3 runtime dependency.
- Create `tests/test_r2_backup.py`: unit-test R2 behavior with a fake injected client.
- Modify `tests/test_maintenance.py`: cover CLI integration while preserving existing local backup behavior.
- Modify `README.md`: document R2 variables, retention setup, backup verification, and restore drill.
- Modify `scripts/container-smoke.sh`: confirm local-only backup behavior remains valid in the production image.

### Task 1: R2 Configuration and Verified Upload

**Files:**
- Create: `src/funding_arb_monitor/r2_backup.py`
- Create: `tests/test_r2_backup.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `R2Config(account_id: str, access_key_id: str, secret_access_key: str, bucket: str)`
- Produces: `r2_config_from_env(environ: Mapping[str, str]) -> R2Config | None`
- Produces: `create_r2_client(config: R2Config) -> Any`
- Produces: `remote_object_key(path: Path, now: datetime | None = None) -> str`
- Produces: `upload_backup(path: Path, config: R2Config, client: Any | None = None, now: datetime | None = None) -> str`

- [ ] **Step 1: Write failing configuration and key tests**

```python
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


def test_remote_object_key_uses_utc_date_and_filename(tmp_path) -> None:
    path = tmp_path / "funding_arb-20260728T092000Z.db"
    now = datetime(2026, 7, 28, 9, 20, tzinfo=timezone.utc)
    assert remote_object_key(path, now) == (
        "funding-arb-monitor/2026/07/28/funding_arb-20260728T092000Z.db"
    )
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run: `pytest tests/test_r2_backup.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'funding_arb_monitor.r2_backup'`.

- [ ] **Step 3: Implement strict environment parsing and object keys**

```python
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
```

- [ ] **Step 4: Run the focused configuration tests**

Run: `pytest tests/test_r2_backup.py -v`

Expected: configuration and object-key tests pass.

- [ ] **Step 5: Write the failing verified-upload test**

```python
def test_upload_backup_sends_checksum_metadata(tmp_path) -> None:
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
```

`FakeS3Client.put_object` reads `Body` during the call, stores the bytes under
`BodyBytes`, and stores the remaining keyword arguments in `put_requests`.

- [ ] **Step 6: Run the upload test and verify it fails**

Run: `pytest tests/test_r2_backup.py::test_upload_backup_sends_checksum_metadata -v`

Expected: FAIL because `upload_backup` is not defined.

- [ ] **Step 7: Implement client construction and verified upload**

```python
def create_r2_client(config: R2Config) -> Any:
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
```

Add `"boto3>=1.34,<2"` to `[project].dependencies`.

- [ ] **Step 8: Run R2 unit tests**

Run: `pytest tests/test_r2_backup.py -v`

Expected: all configuration, key, upload, and propagated-client-error tests pass.

- [ ] **Step 9: Commit the R2 upload unit**

```bash
git add pyproject.toml src/funding_arb_monitor/r2_backup.py tests/test_r2_backup.py
git commit -m "feat: upload verified backups to R2"
```

### Task 2: Safe Verified Download

**Files:**
- Modify: `src/funding_arb_monitor/r2_backup.py`
- Modify: `tests/test_r2_backup.py`

**Interfaces:**
- Consumes: `R2Config`, `create_r2_client`, and `sha256_file` from Task 1.
- Produces: `download_backup(key: str, destination: Path, config: R2Config, client: Any | None = None) -> Path`

- [ ] **Step 1: Write failing download safety tests**

```python
def test_download_backup_verifies_checksum_and_sqlite(tmp_path) -> None:
    source = Store(tmp_path / "source.db")
    source.initialize()
    payload = source.path.read_bytes()
    checksum = hashlib.sha256(payload).hexdigest()
    client = FakeS3Client(
        objects={
            "funding-arb-monitor/2026/07/28/source.db": {
                "Body": io.BytesIO(payload),
                "Metadata": {"sha256": checksum},
            }
        }
    )
    destination = tmp_path / "restore.db"

    result = download_backup(KEY, destination, CONFIG, client=client)

    assert result == destination
    assert integrity_check(destination) == "ok"


def test_download_backup_refuses_existing_destination(tmp_path) -> None:
    destination = tmp_path / "restore.db"
    destination.write_text("keep me")
    with pytest.raises(FileExistsError):
        download_backup(KEY, destination, CONFIG, client=FakeS3Client())
    assert destination.read_text() == "keep me"
```

Also add separate tests for missing or mismatched checksum metadata and invalid
SQLite content. Each test asserts that neither the destination nor its
`.part` file remains after failure.

- [ ] **Step 2: Run the download tests and verify they fail**

Run: `pytest tests/test_r2_backup.py -k download -v`

Expected: FAIL because `download_backup` is not defined.

- [ ] **Step 3: Implement download-to-temporary, checksum, and integrity checks**

```python
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
    try:
        response = s3.get_object(Bucket=config.bucket, Key=key)
        expected = response.get("Metadata", {}).get("sha256")
        if not expected:
            raise RuntimeError("remote backup has no sha256 metadata")
        with partial.open("xb") as target:
            shutil.copyfileobj(response["Body"], target)
        if not hmac.compare_digest(sha256_file(partial), expected):
            raise RuntimeError("remote backup checksum mismatch")
        if integrity_check(partial) != "ok":
            raise RuntimeError("downloaded backup integrity check failed")
        partial.replace(destination)
        return destination
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
```

Import `integrity_check` from `maintenance.py`. Close the response body in a
`finally` block when it exposes `close()`.

- [ ] **Step 4: Run all R2 tests**

Run: `pytest tests/test_r2_backup.py -v`

Expected: all upload and download tests pass.

- [ ] **Step 5: Commit verified download**

```bash
git add src/funding_arb_monitor/r2_backup.py tests/test_r2_backup.py
git commit -m "feat: download and verify R2 backups"
```

### Task 3: Maintenance CLI Integration

**Files:**
- Modify: `src/funding_arb_monitor/cli.py`
- Modify: `tests/test_maintenance.py`

**Interfaces:**
- Consumes: `r2_config_from_env`, `upload_backup`, and `download_backup`.
- Produces: `maintenance backup` output lines `backup=<local path>` and, when configured, `remote_backup=<object key>`.
- Produces: `maintenance download --key KEY --destination PATH` output line `download=<verified local path>`.

- [ ] **Step 1: Write failing CLI parser and local-only behavior tests**

```python
def test_maintenance_download_requires_key_and_destination() -> None:
    args = parser().parse_args(
        [
            "maintenance",
            "download",
            "--key",
            "funding-arb-monitor/2026/07/28/snapshot.db",
            "--destination",
            "/tmp/snapshot.db",
        ]
    )
    assert args.maintenance_command == "download"
    assert args.key.endswith("snapshot.db")


def test_local_backup_does_not_upload_without_r2_configuration(
    tmp_path, monkeypatch, capsys
) -> None:
    source = Store(tmp_path / "source.db")
    source.initialize()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "funding-arb-monitor",
            "--db",
            str(source.path),
            "maintenance",
            "backup",
            "--destination",
            str(tmp_path / "backups"),
        ],
    )
    monkeypatch.setattr("funding_arb_monitor.cli.os.environ", {})
    main()
    assert capsys.readouterr().out.startswith("backup=")
```

- [ ] **Step 2: Run the CLI tests and verify parser failure**

Run: `pytest tests/test_maintenance.py -k "download or local_backup" -v`

Expected: download parser test fails because the subcommand does not exist.

- [ ] **Step 3: Add the download parser and conditional backup upload**

```python
download = maintenance_commands.add_parser(
    "download", help="download and verify an R2 SQLite backup"
)
download.add_argument("--key", required=True)
download.add_argument("--destination", required=True)
```

In the maintenance branch:

```python
config = r2_config_from_env(os.environ)
if args.maintenance_command == "download":
    if config is None:
        raise SystemExit("R2 configuration is required for download")
    restored = download_backup(args.key, Path(args.destination), config)
    print(f"download={restored}")
    return

backup_path = backup_database(store.path, destination)
print(f"backup={backup_path}")
if config is not None:
    print(f"remote_backup={upload_backup(backup_path, config)}")
```

- [ ] **Step 4: Write and run configured-upload and download CLI tests**

Monkeypatch `funding_arb_monitor.cli.upload_backup` to return a known key and
`funding_arb_monitor.cli.download_backup` to create and return a known path.
Assert exact output and arguments:

```python
assert output == (
    f"backup={backup_path}\n"
    "remote_backup=funding-arb-monitor/2026/07/28/snapshot.db\n"
)
assert download_output == f"download={destination}\n"
```

Run: `pytest tests/test_maintenance.py -v`

Expected: all maintenance CLI tests pass.

- [ ] **Step 5: Verify scheduler failure behavior needs no code change**

Run: `pytest tests/test_scheduler.py::test_failed_job_sends_discord_alert -v`

Expected: PASS, demonstrating that a nonzero maintenance subprocess result is
recorded and alerted by the existing generic scheduler path.

- [ ] **Step 6: Commit CLI integration**

```bash
git add src/funding_arb_monitor/cli.py tests/test_maintenance.py
git commit -m "feat: connect maintenance commands to R2"
```

### Task 4: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `scripts/container-smoke.sh`

**Interfaces:**
- Consumes: the CLI and environment variables from Tasks 1-3.
- Produces: operator instructions for Cloudflare R2 setup, Zeabur variables, manual backup verification, and safe restore drills.

- [ ] **Step 1: Update the runbook**

Document these exact protected Zeabur variables:

```text
FUNDING_ARB_R2_ACCOUNT_ID
FUNDING_ARB_R2_ACCESS_KEY_ID
FUNDING_ARB_R2_SECRET_ACCESS_KEY
FUNDING_ARB_R2_BUCKET
```

Document a private bucket, an object read/write token scoped only to that
bucket, and a 30-day object-expiration lifecycle rule. Add:

```bash
funding-arb-monitor maintenance backup
funding-arb-monitor maintenance download \
  --key funding-arb-monitor/YYYY/MM/DD/funding_arb-YYYYMMDDTHHMMSSZ.db \
  --destination /data/restore-drills/funding_arb-YYYYMMDDTHHMMSSZ.db
funding-arb-monitor \
  --db /data/restore-drills/funding_arb-YYYYMMDDTHHMMSSZ.db \
  maintenance check
```

Retain the warning that the service must be stopped before explicitly
replacing `/data/funding_arb.db`.

- [ ] **Step 2: Keep the container smoke test explicitly local-only**

Run the existing backup command with the four R2 variables unset:

```sh
backup_output="$(
  docker exec \
    -e FUNDING_ARB_R2_ACCOUNT_ID= \
    -e FUNDING_ARB_R2_ACCESS_KEY_ID= \
    -e FUNDING_ARB_R2_SECRET_ACCESS_KEY= \
    -e FUNDING_ARB_R2_BUCKET= \
    "$container_name" funding-arb-monitor maintenance backup
)"
```

This confirms the production image contains boto3 while local-only fallback
continues to work without network access.

- [ ] **Step 3: Run formatting-independent checks and the full suite**

Run:

```bash
git diff --check
pytest -q
```

Expected: no whitespace errors and all tests pass.

- [ ] **Step 4: Run the production container smoke test**

Run: `./scripts/container-smoke.sh`

Expected: image builds, runs non-root, reports healthy, and restores a valid
local backup.

- [ ] **Step 5: Commit documentation and smoke coverage**

```bash
git add README.md scripts/container-smoke.sh
git commit -m "docs: add R2 backup and restore runbook"
```

### Task 5: Cloudflare and Zeabur Production Setup

**Files:**
- No repository changes.

**Interfaces:**
- Consumes: a Cloudflare R2 bucket, bucket-scoped credentials, the deployed CLI, and protected Zeabur variables.
- Produces: one verified R2 object and one verified restore-drill database outside the live database path.

- [ ] **Step 1: Create the private R2 bucket**

In Cloudflare, create a bucket named `funding-arb-monitor-backups`. Do not
enable public access. Add a lifecycle rule that expires objects after 30 days.

- [ ] **Step 2: Create least-privilege credentials**

Create an R2 API token scoped to object read and write for only
`funding-arb-monitor-backups`. Record its account ID, access-key ID, and secret
once in the password manager or protected deployment-variable flow. Do not
paste secrets into chat, shell history, repository files, or deployment logs.

- [ ] **Step 3: Configure protected Zeabur variables**

Set the four variables from the runbook on the existing service. Confirm only
the variable names—not their values—when reviewing configuration.

- [ ] **Step 4: Push and deploy the verified commit**

Push `main`, wait for the Zeabur deployment to reach `RUNNING`, and verify:

```bash
curl -fsS https://funding-arb-monitor.zeabur.app/healthz
curl -fsS https://funding-arb-monitor.zeabur.app/readyz
```

Expected: health is `ok` and readiness is `ready`.

- [ ] **Step 5: Trigger and confirm one remote backup**

Run `funding-arb-monitor maintenance backup` inside the service. Confirm output
contains one `backup=` line and one `remote_backup=` line. In Cloudflare,
confirm that exact object key exists and is private.

- [ ] **Step 6: Perform a non-destructive restore drill**

Run:

```bash
R2_RESTORE_KEY='funding-arb-monitor/2026/07/28/funding_arb-20260728T172000Z.db'
funding-arb-monitor maintenance download \
  --key "$R2_RESTORE_KEY" \
  --destination /data/restore-drills/r2-verification.db
funding-arb-monitor \
  --db /data/restore-drills/r2-verification.db \
  maintenance check
```

Expected: the download command reports the destination and the integrity check
prints `integrity=ok`. Do not copy it over the live database.

- [ ] **Step 7: Record final evidence**

Record the deployed commit, Zeabur deployment status, health/readiness results,
remote object key, lifecycle retention duration, and restore-drill integrity
result. Never record credential values.
