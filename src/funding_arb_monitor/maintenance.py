from __future__ import annotations

import sqlite3
from pathlib import Path


def integrity_check(database_path: str | Path) -> str:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "no result"


def backup_database(
    database_path: str | Path, destination: str | Path
) -> Path:
    source_path = Path(database_path)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(source_path) as source,
        sqlite3.connect(destination_path) as target,
    ):
        source.backup(target)
    if integrity_check(destination_path) != "ok":
        destination_path.unlink(missing_ok=True)
        raise RuntimeError("backup integrity check failed")
    return destination_path
