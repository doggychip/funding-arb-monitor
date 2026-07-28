import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import funding_arb_monitor.cli as cli
from funding_arb_monitor.cli import main, parser
from funding_arb_monitor.maintenance import backup_database, integrity_check
from funding_arb_monitor.r2_backup import R2Config
from funding_arb_monitor.store import Store


def test_store_connections_enable_sqlite_safety_pragmas(tmp_path) -> None:
    store = Store(tmp_path / "source.db")
    store.initialize()

    with store.connect() as connection:
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert foreign_keys == 1
    assert busy_timeout >= 5_000
    assert journal_mode == "wal"
    assert store.database_health()["healthy"] is True
    assert store.database_health()["integrity"] == "ok"


def test_online_backup_restores_a_valid_database(tmp_path) -> None:
    source = Store(tmp_path / "source.db")
    source.initialize()
    run_id = source.start_scan_run()
    source.finish_scan_run(run_id, status="success")
    destination = tmp_path / "backups" / "snapshot.db"

    result = backup_database(source.path, destination)

    assert result == destination
    assert integrity_check(destination) == "ok"
    restored = Store(destination)
    assert restored.latest_scan_run()["status"] == "success"
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_maintenance_cli_checks_and_backs_up_database(tmp_path) -> None:
    source = Store(tmp_path / "source.db")
    source.initialize()
    destination = tmp_path / "backups"

    checked = subprocess.run(
        [
            sys.executable,
            "-m",
            "funding_arb_monitor.cli",
            "--db",
            str(source.path),
            "maintenance",
            "check",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    backed_up = subprocess.run(
        [
            sys.executable,
            "-m",
            "funding_arb_monitor.cli",
            "--db",
            str(source.path),
            "maintenance",
            "backup",
            "--destination",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert checked.returncode == 0
    assert checked.stdout.strip() == "integrity=ok"
    assert backed_up.returncode == 0
    backup_path = destination / backed_up.stdout.strip().removeprefix("backup=")
    assert backup_path.exists()
    assert integrity_check(backup_path) == "ok"


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


def test_configured_backup_uploads_verified_local_backup(
    tmp_path, monkeypatch, capsys
) -> None:
    source = Store(tmp_path / "source.db")
    source.initialize()
    destination = tmp_path / "backups"
    uploaded: list[tuple[Path, R2Config]] = []

    def upload(path: Path, config: R2Config) -> str:
        uploaded.append((path, config))
        return "funding-arb-monitor/2026/07/28/snapshot.db"

    monkeypatch.setattr(cli, "upload_backup", upload, raising=False)
    monkeypatch.setattr(
        cli.os,
        "environ",
        {
            "FUNDING_ARB_R2_ACCOUNT_ID": "account",
            "FUNDING_ARB_R2_ACCESS_KEY_ID": "access",
            "FUNDING_ARB_R2_SECRET_ACCESS_KEY": "secret",
            "FUNDING_ARB_R2_BUCKET": "backups",
        },
    )
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
            str(destination),
        ],
    )

    main()

    backup_path = uploaded[0][0]
    assert capsys.readouterr().out == (
        f"backup={backup_path}\n"
        "remote_backup=funding-arb-monitor/2026/07/28/snapshot.db\n"
    )
    assert uploaded == [(backup_path, R2Config("account", "access", "secret", "backups"))]
    assert integrity_check(backup_path) == "ok"


def test_maintenance_download_prints_verified_destination(tmp_path, monkeypatch, capsys) -> None:
    destination = tmp_path / "snapshot.db"
    downloaded: list[tuple[str, Path, R2Config]] = []

    def download(key: str, path: Path, config: R2Config) -> Path:
        downloaded.append((key, path, config))
        path.write_bytes(b"verified backup")
        return path

    monkeypatch.setattr(cli, "download_backup", download, raising=False)
    monkeypatch.setattr(
        cli.os,
        "environ",
        {
            "FUNDING_ARB_R2_ACCOUNT_ID": "account",
            "FUNDING_ARB_R2_ACCESS_KEY_ID": "access",
            "FUNDING_ARB_R2_SECRET_ACCESS_KEY": "secret",
            "FUNDING_ARB_R2_BUCKET": "backups",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "funding-arb-monitor",
            "maintenance",
            "download",
            "--key",
            "funding-arb-monitor/2026/07/28/snapshot.db",
            "--destination",
            str(destination),
        ],
    )

    main()

    assert capsys.readouterr().out == f"download={destination}\n"
    assert downloaded == [
        (
            "funding-arb-monitor/2026/07/28/snapshot.db",
            destination,
            R2Config("account", "access", "secret", "backups"),
        )
    ]


def test_maintenance_download_refuses_absent_live_database_path(
    tmp_path, monkeypatch
) -> None:
    live_database = tmp_path / "data" / "funding_arb.db"
    downloaded: list[Path] = []

    def download(key: str, path: Path, config: R2Config) -> Path:
        downloaded.append(path)
        return path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "download_backup", download)
    monkeypatch.setattr(
        cli.os,
        "environ",
        {
            "FUNDING_ARB_R2_ACCOUNT_ID": "account",
            "FUNDING_ARB_R2_ACCESS_KEY_ID": "access",
            "FUNDING_ARB_R2_SECRET_ACCESS_KEY": "secret",
            "FUNDING_ARB_R2_BUCKET": "backups",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "funding-arb-monitor",
            "--db",
            "data/funding_arb.db",
            "maintenance",
            "download",
            "--key",
            "funding-arb-monitor/2026/07/28/snapshot.db",
            "--destination",
            str(live_database),
        ],
    )

    assert not live_database.exists()
    with pytest.raises(SystemExit, match="live database"):
        main()

    assert downloaded == []


def test_partial_r2_configuration_exits_nonzero_without_leaking_credentials(
    tmp_path,
) -> None:
    access_key = "partial-access-key-value"
    environment = os.environ.copy()
    environment.update(
        {
            "FUNDING_ARB_R2_ACCOUNT_ID": "account",
            "FUNDING_ARB_R2_ACCESS_KEY_ID": access_key,
            "FUNDING_ARB_R2_BUCKET": "backups",
        }
    )
    environment.pop("FUNDING_ARB_R2_SECRET_ACCESS_KEY", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "funding_arb_monitor.cli",
            "--db",
            str(tmp_path / "source.db"),
            "maintenance",
            "backup",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0
    assert access_key not in result.stdout + result.stderr


def test_upload_client_failure_exits_nonzero_without_leaking_credentials(
    tmp_path,
) -> None:
    source = Store(tmp_path / "source.db")
    source.initialize()
    fake_module_dir = tmp_path / "fake-module"
    fake_module_dir.mkdir()
    (fake_module_dir / "boto3.py").write_text(
        "class FailingClient:\n"
        "    def put_object(self, **kwargs):\n"
        "        raise RuntimeError('R2 unavailable')\n"
        "\n"
        "def client(*args, **kwargs):\n"
        "    return FailingClient()\n"
    )
    access_key = "upload-access-key-value"
    secret_key = "upload-secret-key-value"
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    [str(fake_module_dir), environment.get("PYTHONPATH", "")],
                )
            ),
            "FUNDING_ARB_R2_ACCOUNT_ID": "account",
            "FUNDING_ARB_R2_ACCESS_KEY_ID": access_key,
            "FUNDING_ARB_R2_SECRET_ACCESS_KEY": secret_key,
            "FUNDING_ARB_R2_BUCKET": "backups",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "funding_arb_monitor.cli",
            "--db",
            str(source.path),
            "maintenance",
            "backup",
            "--destination",
            str(tmp_path / "backups"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0
    assert "R2 unavailable" in result.stderr
    assert access_key not in result.stdout + result.stderr
    assert secret_key not in result.stdout + result.stderr
