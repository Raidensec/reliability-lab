from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Order:
    order_id: str
    customer: str
    amount_cents: int
    created_at: str


@dataclass(frozen=True)
class DatabaseSummary:
    record_count: int
    records_sha256: str


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_database(path: Path) -> None:
    with closing(connect_database(path)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            apply_migrations(connection)


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
    version = max(versions, default=0)
    if version > 1:
        raise sqlite3.DatabaseError(f"Database schema version {version} is unsupported")
    if version == 0:
        create_initial_schema(connection)


def create_initial_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer TEXT NOT NULL, "
        "amount_cents INTEGER NOT NULL CHECK(amount_cents > 0), created_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (1, utc_now()),
    )


def create_order(path: Path, customer: str, amount_cents: int) -> Order:
    order = Order(str(uuid.uuid4()), customer, amount_cents, utc_now())
    with closing(connect_database(path)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO orders(order_id, customer, amount_cents, created_at) "
                "VALUES (?, ?, ?, ?)",
                (order.order_id, order.customer, order.amount_cents, order.created_at),
            )
    return order


def list_orders(path: Path, limit: int = 50) -> list[Order]:
    with closing(connect_database(path)) as connection:
        rows = connection.execute(
            "SELECT order_id, customer, amount_cents, created_at FROM orders "
            "ORDER BY created_at DESC, order_id LIMIT ?",
            (limit,),
        )
        return [Order(**dict(row)) for row in rows]


def database_ready(path: Path) -> bool:
    with closing(connect_database(path)) as connection:
        migration = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 1"
        ).fetchone()
        if migration is None:
            return False
        connection.execute("SELECT order_id FROM orders LIMIT 1").fetchone()
    return True


def summarize_database(path: Path) -> DatabaseSummary:
    with closing(connect_database(path)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"SQLite integrity check failed: {integrity}")
        rows = connection.execute(
            "SELECT order_id, customer, amount_cents, created_at FROM orders ORDER BY order_id"
        )
        digest = hashlib.sha256()
        count = 0
        for row in rows:
            record = json.dumps(
                dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            digest.update(record.encode("utf-8"))
            digest.update(b"\n")
            count += 1
    return DatabaseSummary(count, digest.hexdigest())


def order_json(order: Order) -> dict[str, str | int]:
    return asdict(order)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
