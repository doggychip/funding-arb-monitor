import sqlite3
import subprocess
import sys

from funding_arb_monitor.maintenance import backup_database, integrity_check
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
