"""Persistence layer.

Speaks to SQLite (default, zero-config) or PostgreSQL (when DATABASE_URL is set).
Every statement is written once with `?` placeholders and rewritten for Postgres,
so both dialects take exactly the same code path.

Correctness under concurrency comes from the database itself:
  * `ux_confirmed_slot` -- a partial UNIQUE index on bookings(slot_id) WHERE
    status='confirmed'. The database, not application code, guarantees that two
    simultaneous requests can never both confirm the same slot.
  * `rate_attempts` insert guarded by a counting subquery -- an atomic
    "insert only if fewer than 5 attempts in the window" statement.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

SQLITE_PATH = os.getenv("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "data", "clinic.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

DDL_SQLITE = [
    """CREATE TABLE IF NOT EXISTS slots (
        id          TEXT PRIMARY KEY,
        provider    TEXT    NOT NULL,
        start_at    TEXT    NOT NULL,
        start_ms    INTEGER NOT NULL,
        end_at      TEXT    NOT NULL,
        end_ms      INTEGER NOT NULL,
        ord         INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS bookings (
        seq            INTEGER PRIMARY KEY AUTOINCREMENT,
        id             TEXT    NOT NULL UNIQUE,
        slot_id        TEXT    NOT NULL,
        name           TEXT    NOT NULL,
        email          TEXT    NOT NULL,
        status         TEXT    NOT NULL,
        created_at     TEXT    NOT NULL,
        created_ms     INTEGER NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_confirmed_slot
        ON bookings (slot_id) WHERE status = 'confirmed'""",
    "CREATE INDEX IF NOT EXISTS ix_bookings_email ON bookings (email, created_ms, seq)",
    """CREATE TABLE IF NOT EXISTS idem (
        key          TEXT PRIMARY KEY,
        body_hash    TEXT    NOT NULL,
        status_code  INTEGER,
        response     TEXT,
        created_ms   INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS rate_attempts (
        seq        INTEGER PRIMARY KEY AUTOINCREMENT,
        email      TEXT    NOT NULL,
        at_ms      INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_rate ON rate_attempts (email, at_ms)",
]

DDL_POSTGRES = [
    """CREATE TABLE IF NOT EXISTS slots (
        id          TEXT PRIMARY KEY,
        provider    TEXT   NOT NULL,
        start_at    TEXT   NOT NULL,
        start_ms    BIGINT NOT NULL,
        end_at      TEXT   NOT NULL,
        end_ms      BIGINT NOT NULL,
        ord         BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS bookings (
        seq            BIGSERIAL PRIMARY KEY,
        id             TEXT   NOT NULL UNIQUE,
        slot_id        TEXT   NOT NULL,
        name           TEXT   NOT NULL,
        email          TEXT   NOT NULL,
        status         TEXT   NOT NULL,
        created_at     TEXT   NOT NULL,
        created_ms     BIGINT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_confirmed_slot
        ON bookings (slot_id) WHERE status = 'confirmed'""",
    "CREATE INDEX IF NOT EXISTS ix_bookings_email ON bookings (email, created_ms, seq)",
    """CREATE TABLE IF NOT EXISTS idem (
        key          TEXT PRIMARY KEY,
        body_hash    TEXT    NOT NULL,
        status_code  INTEGER,
        response     TEXT,
        created_ms   BIGINT  NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS rate_attempts (
        seq      BIGSERIAL PRIMARY KEY,
        email    TEXT   NOT NULL,
        at_ms    BIGINT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_rate ON rate_attempts (email, at_ms)",
]


class UniqueViolation(Exception):
    """Raised when an INSERT breaks a unique constraint."""


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, database_url: str | None = None, sqlite_path: str | None = None):
        url = (database_url if database_url is not None else DATABASE_URL).strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        self.pg = url.startswith("postgresql://") or url.startswith("postgres://")
        self.url = url
        self.sqlite_path = sqlite_path or SQLITE_PATH
        self.init_schema()

    # ---------------------------------------------------------------- plumbing
    @contextmanager
    def conn(self):
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row

            c = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        else:
            parent = os.path.dirname(os.path.abspath(self.sqlite_path))
            os.makedirs(parent, exist_ok=True)
            c = sqlite3.connect(self.sqlite_path, isolation_level=None, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=15000")
            c.execute("PRAGMA synchronous=NORMAL")
        try:
            yield c
        finally:
            c.close()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    def all(self, cur, sql: str, params=()):
        cur.execute(self._sql(sql), params)
        return [dict(r) for r in cur.fetchall()]

    def one(self, cur, sql: str, params=()):
        cur.execute(self._sql(sql), params)
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def write(self, cur, sql: str, params=()):
        """Execute a write; translate unique-constraint failures into UniqueViolation."""
        try:
            cur.execute(self._sql(sql), params)
            return cur
        except Exception as exc:  # noqa: BLE001 - dialect specific error types
            name = type(exc).__name__.lower()
            text = str(exc).lower()
            if "uniqueviolation" in name or "integrityerror" in name or "unique" in text or "duplicate key" in text:
                raise UniqueViolation(str(exc)) from exc
            raise

    def scalar(self, cur, sql: str, params=()):
        cur.execute(self._sql(sql), params)
        row = cur.fetchone()
        if row is None:
            return None
        return list(dict(row).values())[0] if not isinstance(row, tuple) else row[0]

    # ------------------------------------------------------------------- setup
    def init_schema(self):
        with self.conn() as c:
            cur = c.cursor()
            for stmt in (DDL_POSTGRES if self.pg else DDL_SQLITE):
                cur.execute(stmt)

    def reset(self, slots: list[dict]):
        """Delete every booking and replace all slots (past slots included)."""
        with self.conn() as c:
            cur = c.cursor()
            for table in ("bookings", "slots", "idem", "rate_attempts"):
                cur.execute(f"DELETE FROM {table}")
            for i, s in enumerate(slots):
                self.write(
                    cur,
                    "INSERT INTO slots (id, provider, start_at, start_ms, end_at, end_ms, ord)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], s["provider"], s["start"], s["start_ms"], s["end"], s["end_ms"], i),
                )

    # ------------------------------------------------------------------- slots
    def list_slots(self, provider: str | None = None) -> list[dict]:
        with self.conn() as c:
            cur = c.cursor()
            if provider is None:
                rows = self.all(cur, "SELECT * FROM slots ORDER BY start_ms ASC, ord ASC")
            else:
                rows = self.all(cur, "SELECT * FROM slots WHERE provider = ? ORDER BY start_ms ASC, ord ASC", (provider,))
            booked = {
                r["slot_id"]
                for r in self.all(cur, "SELECT slot_id FROM bookings WHERE status = 'confirmed'")
            }
        for r in rows:
            r["available"] = r["id"] not in booked
        return rows

    def seed_if_empty(self, slots: list[dict]) -> int:
        """Populate demo slots on a brand new database, so the web page has content."""
        with self.conn() as c:
            cur = c.cursor()
            if int(self.scalar(cur, "SELECT COUNT(*) FROM slots") or 0) > 0:
                return 0
            for i, s in enumerate(slots):
                self.write(
                    cur,
                    "INSERT INTO slots (id, provider, start_at, start_ms, end_at, end_ms, ord)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], s["provider"], s["start"], s["start_ms"], s["end"], s["end_ms"], i),
                )
            return len(slots)

    def get_slot(self, slot_id: str) -> dict | None:
        with self.conn() as c:
            return self.one(c.cursor(), "SELECT * FROM slots WHERE id = ?", (slot_id,))

    # ---------------------------------------------------------------- bookings
    def get_booking(self, booking_id: str) -> dict | None:
        with self.conn() as c:
            return self.one(c.cursor(), "SELECT * FROM bookings WHERE id = ?", (booking_id,))

    def create_booking(self, booking: dict) -> None:
        """Insert a confirmed booking. Raises UniqueViolation if the slot is taken."""
        with self.conn() as c:
            self.write(
                c.cursor(),
                "INSERT INTO bookings (id, slot_id, name, email, status, created_at, created_ms)"
                " VALUES (?, ?, ?, ?, 'confirmed', ?, ?)",
                (
                    booking["id"],
                    booking["slot_id"],
                    booking["name"],
                    booking["email"],
                    booking["created_at"],
                    booking["created_ms"],
                ),
            )

    def cancel_booking(self, booking_id: str) -> str:
        """Returns 'ok' | 'missing' | 'already'."""
        with self.conn() as c:
            cur = c.cursor()
            cur.execute(self._sql("SELECT status FROM bookings WHERE id = ?"), (booking_id,))
            row = cur.fetchone()
            if row is None:
                return "missing"
            status = dict(row)["status"] if not isinstance(row, tuple) else row[0]
            if status != "confirmed":
                return "already"
            self.write(cur, "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status = 'confirmed'", (booking_id,))
            return "ok"

    def list_bookings(self, email: str, limit: int, cursor: tuple[int, int] | None) -> list[dict]:
        """One page, oldest first, plus one extra row to detect whether more exist."""
        sql = "SELECT * FROM bookings WHERE email = ?"
        params: list = [email]
        if cursor is not None:
            sql += " AND (created_ms > ? OR (created_ms = ? AND seq > ?))"
            params += [cursor[0], cursor[0], cursor[1]]
        sql += " ORDER BY created_ms ASC, seq ASC LIMIT ?"
        params.append(limit + 1)
        with self.conn() as c:
            return self.all(c.cursor(), sql, tuple(params))

    # ------------------------------------------------------------- idempotency
    def idem_get(self, key: str) -> dict | None:
        with self.conn() as c:
            return self.one(c.cursor(), "SELECT * FROM idem WHERE key = ?", (key,))

    def idem_claim(self, key: str, body_hash: str) -> bool:
        """Try to create the pending row. False when another request already holds the key."""
        try:
            with self.conn() as c:
                self.write(
                    c.cursor(),
                    "INSERT INTO idem (key, body_hash, status_code, response, created_ms) VALUES (?, ?, NULL, NULL, ?)",
                    (key, body_hash, now_ms()),
                )
            return True
        except UniqueViolation:
            return False

    def idem_finish(self, key: str, status_code: int, response: str) -> None:
        with self.conn() as c:
            self.write(
                c.cursor(),
                "UPDATE idem SET status_code = ?, response = ? WHERE key = ?",
                (status_code, response, key),
            )

    def idem_release(self, key: str) -> None:
        """Drop a pending claim so a failed attempt can be retried cleanly."""
        with self.conn() as c:
            self.write(c.cursor(), "DELETE FROM idem WHERE key = ? AND response IS NULL", (key,))

    # -------------------------------------------------------------- rate limit
    def rate_take(self, email: str, limit: int, window_ms: int) -> tuple[bool, int]:
        """Atomically record one attempt for `email` if fewer than `limit` exist in the window.

        Returns (allowed, retry_after_seconds).
        """
        now = now_ms()
        start = now - window_ms
        with self.conn() as c:
            cur = c.cursor()
            # cheap housekeeping: the table only ever needs to hold one window
            cur.execute(self._sql("DELETE FROM rate_attempts WHERE at_ms <= ?"), (start,))
            cur.execute(
                self._sql(
                    "INSERT INTO rate_attempts (email, at_ms) "
                    "SELECT ?, ? WHERE ("
                    "  SELECT COUNT(*) FROM rate_attempts WHERE email = ? AND at_ms > ?"
                    ") < ?"
                ),
                (email, now, email, start, limit),
            )
            inserted = cur.rowcount
            if inserted and inserted > 0:
                return True, 0
            oldest = self.scalar(
                cur,
                "SELECT MIN(at_ms) FROM rate_attempts WHERE email = ? AND at_ms > ?",
                (email, start),
            )
            if oldest is None:
                return True, 0
            remaining_ms = (int(oldest) + window_ms) - now
            return False, max(1, -(-remaining_ms // 1000))
