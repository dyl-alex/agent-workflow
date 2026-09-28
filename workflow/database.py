"""SQLite connection and schema initialization helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 1


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize(database_path: Path, schema_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(database_path)
    try:
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_version")]
        if versions != [SCHEMA_VERSION]:
            raise RuntimeError(
                f"Unsupported schema version(s): {versions}; expected [{SCHEMA_VERSION}]"
            )
    finally:
        connection.close()
