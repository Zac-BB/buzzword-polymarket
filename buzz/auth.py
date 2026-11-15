"""Passwords, accounts and sessions."""
import hashlib
import hmac
import re
import secrets
import threading
import time

from . import db

PBKDF2_ITERATIONS = 120_000  # ~0.2s on a Pi 4; fine for 20 people
SESSION_DAYS = 30
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,24}$")


class AuthError(ValueError):
    pass


class Throttle:
    """Blocks a key (a username or IP address) for a while after too many
    failures, so short handed-out passwords can't be guessed by brute force.
    Kept in memory: a restart clears it, which is fine for this purpose."""

    def __init__(self, limit, window=15 * 60):
        self.limit, self.window = limit, window
        self.fails = {}
        self.lock = threading.Lock()

    def _recent(self, key, now):
        times = [t for t in self.fails.get(key, []) if now - t < self.window]
        if times:
            self.fails[key] = times
        else:
            self.fails.pop(key, None)
        return times

    def check(self, *keys):
        now = time.time()
        with self.lock:
            for key in keys:
                times = self._recent(key, now)
                if len(times) >= self.limit:
                    wait = int(self.window - (now - times[0])) // 60 + 1
                    raise AuthError(f"Too many failed attempts. Try again in {wait} minute(s).")

    def fail(self, *keys):
        now = time.time()
        with self.lock:
            if len(self.fails) > 5000:  # keep memory bounded under a flood
                self.fails.clear()
            for key in keys:
                self.fails.setdefault(key, []).append(now)

    def clear(self, key):
        with self.lock:
            self.fails.pop(key, None)


USER_THROTTLE = Throttle(limit=5)    # wrong passwords per username
IP_THROTTLE = Throttle(limit=20)     # wrong passwords / invite codes per address


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def check_password(password, stored):
    try:
        _, iters, salt, hexdigest = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), hexdigest)


def validate_new_account(username, display_name, password):
    if not USERNAME_RE.match(username or ""):
        raise AuthError("Username must be 2-24 characters: letters, numbers, _ . -")
    if not display_name or len(display_name) > 40:
        raise AuthError("Name is required (max 40 characters).")
    if not password or len(password) < 4:
        raise AuthError("Password must be at least 4 characters.")


def create_user(conn, username, display_name, password, is_admin=False, is_trusted=False):
    """Must be called inside a transaction."""
    username = (username or "").strip()
    display_name = (display_name or "").strip()
    validate_new_account(username, display_name, password)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise AuthError("That username is taken.")
    balance = db.get_int_setting(conn, "starting_balance")
    cur = conn.execute(
        "INSERT INTO users(username, display_name, pw_hash, is_admin, is_trusted, balance, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (username, display_name, hash_password(password), int(is_admin), int(is_trusted), balance, db.now()),
    )
    uid = cur.lastrowid
    conn.execute("INSERT INTO ledger(user_id, amount, reason, ts) VALUES (?, ?, ?, ?)",
                 (uid, balance, "Starting balance", db.now()))
    return uid


def authenticate(conn, username, password, ip=""):
    ukey = "u:" + (username or "").strip().lower()
    ikey = "ip:" + ip
    USER_THROTTLE.check(ukey)
    IP_THROTTLE.check(ikey)
    row = conn.execute("SELECT * FROM users WHERE username = ?", ((username or "").strip(),)).fetchone()
    if not row or not check_password(password or "", row["pw_hash"]):
        USER_THROTTLE.fail(ukey)
        IP_THROTTLE.fail(ikey)
        raise AuthError("Wrong username or password.")
    USER_THROTTLE.clear(ukey)
    if row["disabled"]:
        raise AuthError("This account is disabled.")
    return row


def set_password(conn, user_id, password):
    if not password or len(password) < 4:
        raise AuthError("Password must be at least 4 characters.")
    conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(password), user_id))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def start_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions(token, user_id, csrf, expires_at) VALUES (?, ?, ?, ?)",
                 (token, user_id, secrets.token_urlsafe(16), db.now() + SESSION_DAYS * 86400))
    return token


def load_session(conn, token):
    """Returns (user_row, csrf) or (None, None)."""
    if not token:
        return None, None
    row = conn.execute(
        "SELECT u.*, s.csrf AS _csrf FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ? AND u.disabled = 0",
        (token, db.now()),
    ).fetchone()
    if not row:
        return None, None
    return row, row["_csrf"]


def end_session(conn, token):
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
