"""SQLite storage. One small file on disk, no server process needed."""
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("BUZZ_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "buzz.db"))

# All writes go through this lock so balances can never race. With ~20 users
# this costs nothing and keeps the logic simple.
WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_trusted INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    balance INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- status: suggested -> opening -> open -> locked -> resolved
--         (or rejected / cancelled)
CREATE TABLE IF NOT EXISTS markets (
    id INTEGER PRIMARY KEY,
    phrase TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    meeting TEXT NOT NULL DEFAULT '',
    suggested_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'suggested',
    opening_line REAL,
    line REAL,
    actual INTEGER,
    created_at REAL NOT NULL,
    opened_at REAL,
    locked_at REAL,
    resolved_at REAL
);
CREATE TABLE IF NOT EXISTS votes (
    market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (market_id, user_id)
);
CREATE TABLE IF NOT EXISTS guesses (
    market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    guess REAL NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (market_id, user_id)
);
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY,
    market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    side TEXT NOT NULL CHECK (side IN ('over', 'under')),
    stake INTEGER NOT NULL CHECK (stake > 0),
    line REAL NOT NULL,
    created_at REAL NOT NULL,
    result TEXT,          -- win / lose / push / refund
    payout INTEGER
);
CREATE INDEX IF NOT EXISTS bets_market ON bets(market_id);
CREATE INDEX IF NOT EXISTS bets_user ON bets(user_id);
CREATE TABLE IF NOT EXISTS line_history (
    id INTEGER PRIMARY KEY,
    market_id INTEGER NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    line REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS line_history_market ON line_history(market_id, ts);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount INTEGER NOT NULL,
    reason TEXT NOT NULL,
    ts REAL NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "invite_code": "buzzword",
    "starting_balance": "1000",
    # Bigger = line moves less per point bet.
    "liquidity": "2000",
    "max_bet": "500",
}


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")  # gentler on SD cards
    return conn


def init(conn):
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key)


def get_int_setting(conn, key):
    try:
        return int(get_setting(conn, key))
    except (TypeError, ValueError):
        return int(DEFAULT_SETTINGS[key])


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


class transaction:
    """`with transaction(conn):` -> serialized write transaction."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        WRITE_LOCK.acquire()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except Exception:
            WRITE_LOCK.release()
            raise
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            WRITE_LOCK.release()
        return False


def now():
    return time.time()
