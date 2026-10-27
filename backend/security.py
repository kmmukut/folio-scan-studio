"""Folio's local user accounts, roles, and bounded guest print sessions.

Migration-safe: existing v0.3 users and sessions remain valid on upgrade.
All credentials live in /data/access.sqlite3; passwords are salted PBKDF2 hashes.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path

COOKIE_NAME = 'folio_session'
GUEST_COOKIE = 'folio_guest'
SESSION_SECONDS = 7 * 24 * 60 * 60
HASH_ITERATIONS = 600_000
ROLES = ('admin', 'member', 'printer')
USERNAME_PATTERN = re.compile(r'^[a-zA-Z][a-zA-Z0-9_.-]{2,31}$')
_login_attempts: dict[str, deque] = defaultdict(deque)
_guest_tokens: dict[str, tuple[str, float]] = {}
_guest_submissions: dict[str, deque] = defaultdict(deque)
_global_submissions = deque()
_auth_lock = threading.RLock()


def db_path(data_dir: Path) -> Path:
    return data_dir / 'access.sqlite3'


@contextmanager
def connection(data_dir: Path):
    db = sqlite3.connect(db_path(data_dir), timeout=10)
    try:
        with db:
            yield db
    finally:
        db.close()


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, HASH_ITERATIONS)


def init(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    with connection(data_dir) as db:
        # Keep the original v0.3 table definition and add non-breaking columns.
        db.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, salt BLOB NOT NULL, password_hash BLOB NOT NULL)')
        columns = {row[1] for row in db.execute('PRAGMA table_info(users)')}
        if 'role' not in columns:
            db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
        if 'enabled' not in columns:
            db.execute('ALTER TABLE users ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')
        if 'created' not in columns:
            db.execute('ALTER TABLE users ADD COLUMN created INTEGER NOT NULL DEFAULT 0')
        db.execute('CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, csrf TEXT NOT NULL, expires INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('guest_print','off')")
        count = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        password = os.environ.get('SCAN_STUDIO_ADMIN_PASSWORD', '')
        # Without a password in the settings, the first administrator is created
        # in the browser instead (create_first_admin).
        if count == 0 and password:
            username = os.environ.get('SCAN_STUDIO_ADMIN_USER', 'admin')
            if not 1 <= len(username) <= 50 or not username.replace('_','').replace('-','').isalnum():
                raise RuntimeError('SCAN_STUDIO_ADMIN_USER must be letters, numbers, _ or -')
            if len(password) < 12:
                raise RuntimeError('Set SCAN_STUDIO_ADMIN_PASSWORD to at least 12 characters before first startup')
            salt = secrets.token_bytes(24)
            db.execute('INSERT INTO users (username,salt,password_hash,role,enabled,created) VALUES (?,?,?,?,?,?)',
                       (username, salt, hash_password(password, salt), 'admin', 1, int(time.time())))
        db.execute('DELETE FROM sessions WHERE expires < ?', (int(time.time()),))


def initial_admin(data_dir: Path) -> str:
    """Legacy scans without owner belong to the original administrator."""
    with connection(data_dir) as db:
        row = db.execute("SELECT username FROM users WHERE role='admin' ORDER BY created ASC, rowid ASC LIMIT 1").fetchone()
        return row[0] if row else os.environ.get('SCAN_STUDIO_ADMIN_USER', 'admin')


def try_login(data_dir: Path, username: str, password: str, ip: str):
    key = ip or 'unknown'
    ts = time.time()
    with _auth_lock:
        attempts = _login_attempts[key]
        while attempts and attempts[0] < ts - 900:
            attempts.popleft()
        if len(attempts) >= 8:
            return None, 'Too many attempts. Try again later.'
        attempts.append(ts)
    with connection(data_dir) as db:
        row = db.execute('SELECT salt,password_hash,role,enabled FROM users WHERE username=?', (username,)).fetchone()
    salt = row[0] if row else b'\0' * 24
    candidate = hash_password(password, salt)
    if not row or not hmac.compare_digest(candidate, row[1]) or not row[3]:
        return None, 'Invalid username or password'
    with _auth_lock:
        _login_attempts[key].clear()
    return start_session(data_dir, username, row[2]), None


def start_session(data_dir: Path, username: str, role: str) -> dict:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expiry = int(time.time() + SESSION_SECONDS)
    with connection(data_dir) as db:
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), username, csrf, expiry))
    return {'token': token, 'csrf': csrf, 'username': username, 'role': role}


def keep_session(data_dir: Path, token: str, username: str, csrf: str) -> bool:
    """After a restore: keep the person restoring signed in, if the restored
    accounts still have them as an enabled administrator."""
    with connection(data_dir) as db:
        row = db.execute('SELECT role,enabled FROM users WHERE username=?', (username,)).fetchone()
        if not row or row[0] != 'admin' or not row[1]:
            return False
        db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(),
                   username, csrf, int(time.time() + SESSION_SECONDS)))
    return True


def setup_required(data_dir: Path) -> bool:
    """True until the first account exists."""
    with connection(data_dir) as db:
        return db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0


def create_first_admin(data_dir: Path, username: str, password: str) -> dict:
    """Create the first administrator from the browser and sign them in. Only
    possible while no account exists; the check and insert are one transaction."""
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError('Username needs 3–32 letters, numbers, dots, underscores or dashes (starting with a letter)')
    if not 12 <= len(password) <= 256:
        raise ValueError('Password must contain 12–256 characters')
    salt = secrets.token_bytes(24)
    db = sqlite3.connect(db_path(data_dir), timeout=10, isolation_level=None)
    try:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
            db.execute('ROLLBACK')
            raise PermissionError('Folio already has an administrator. Sign in instead.')
        db.execute('INSERT INTO users (username,salt,password_hash,role,enabled,created) VALUES (?,?,?,?,?,?)',
                   (username, salt, hash_password(password, salt), 'admin', 1, int(time.time())))
        db.execute('COMMIT')
    finally:
        db.close()
    return start_session(data_dir, username, 'admin')


def session(data_dir: Path, token: str | None):
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connection(data_dir) as db:
        row = db.execute('''SELECT s.username,s.csrf,s.expires,u.role,u.enabled
                            FROM sessions s JOIN users u ON s.username=u.username
                            WHERE s.token_hash=?''', (digest,)).fetchone()
    if row and row[2] > time.time() and row[4]:
        return {'username': row[0], 'csrf': row[1], 'role': row[3]}
    return None


def logout(data_dir: Path, token: str | None):
    if token:
        with connection(data_dir) as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))


def update_password(data_dir: Path, username: str, old_password: str, new_password: str):
    if not 12 <= len(new_password) <= 256:
        return False
    with connection(data_dir) as db:
        row = db.execute('SELECT salt,password_hash FROM users WHERE username=?', (username,)).fetchone()
        if not row or not hmac.compare_digest(hash_password(old_password, row[0]), row[1]):
            return False
        salt = secrets.token_bytes(24)
        db.execute('UPDATE users SET salt=?, password_hash=? WHERE username=?',
                   (salt, hash_password(new_password, salt), username))
        db.execute('DELETE FROM sessions WHERE username=?', (username,))
    return True


def list_users(data_dir: Path):
    with connection(data_dir) as db:
        rows = db.execute('SELECT username,role,enabled,created FROM users ORDER BY created,username').fetchall()
    return [{'username': r[0], 'role': r[1], 'enabled': bool(r[2]), 'created': r[3]} for r in rows]


def add_user(data_dir: Path, username: str, password: str, role: str):
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError('Username needs 3–32 letters, numbers, dots, underscores or dashes (starting with a letter)')
    if not 12 <= len(password) <= 256:
        raise ValueError('Password must contain 12–256 characters')
    if role not in ROLES:
        raise ValueError('Invalid account role')
    salt = secrets.token_bytes(24)
    try:
        with connection(data_dir) as db:
            db.execute('INSERT INTO users (username,salt,password_hash,role,enabled,created) VALUES (?,?,?,?,?,?)',
                       (username, salt, hash_password(password,salt), role, 1, int(time.time())))
    except sqlite3.IntegrityError as exc:
        raise ValueError('Username already exists') from exc
    return list_users(data_dir)


def edit_user(data_dir: Path, username: str, actor: str, *, role=None, enabled=None, password=None):
    with connection(data_dir) as db:
        row = db.execute('SELECT role,enabled FROM users WHERE username=?',(username,)).fetchone()
        if not row:
            raise ValueError('User does not exist')
        if role is not None and role not in ROLES:
            raise ValueError('Invalid role')
        if password is not None and not 12 <= len(password) <= 256:
            raise ValueError('Password must contain 12–256 characters')
        new_role = role if role is not None else row[0]
        new_enabled = bool(enabled) if enabled is not None else bool(row[1])
        if actor == username and (new_role != 'admin' or not new_enabled):
            raise ValueError('You cannot demote or disable your own administrator account')
        if row[0] == 'admin' and row[1] and (new_role != 'admin' or not new_enabled):
            admins = db.execute("SELECT count(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
            if admins <= 1:
                raise ValueError('Cannot disable the last active administrator')
        db.execute('UPDATE users SET role=?,enabled=? WHERE username=?', (new_role,int(new_enabled),username))
        if password is not None:
            salt = secrets.token_bytes(24)
            db.execute('UPDATE users SET salt=?,password_hash=? WHERE username=?', (salt,hash_password(password,salt),username))
        if password is not None or new_role != row[0] or new_enabled != bool(row[1]):
            db.execute('DELETE FROM sessions WHERE username=?',(username,))
    return list_users(data_dir)


def remove_user(data_dir: Path, username: str, actor: str):
    if username == actor:
        raise ValueError('You cannot delete your own administrator account')
    with connection(data_dir) as db:
        row = db.execute('SELECT role,enabled FROM users WHERE username=?',(username,)).fetchone()
        if not row:
            raise ValueError('User does not exist')
        if row[0] == 'admin' and row[1]:
            admins = db.execute("SELECT count(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
            if admins <= 1:
                raise ValueError('Cannot delete the last active administrator')
        db.execute('DELETE FROM sessions WHERE username=?',(username,))
        db.execute('DELETE FROM users WHERE username=?',(username,))
    # Existing scan files remain on disk for administrator recovery.
    return list_users(data_dir)


def guest_enabled(data_dir: Path) -> bool:
    with connection(data_dir) as db:
        row = db.execute("SELECT value FROM settings WHERE key='guest_print'").fetchone()
        return bool(row and row[0] == 'on')


def set_guest(data_dir: Path, enabled: bool):
    with connection(data_dir) as db:
        db.execute("INSERT INTO settings (key,value) VALUES ('guest_print', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('on' if enabled else 'off',))


def guest_csrf() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with _auth_lock:
        for key, (_, expires) in list(_guest_tokens.items()):
            if expires < time.time():
                _guest_tokens.pop(key, None)
        _guest_tokens[hashlib.sha256(token.encode()).hexdigest()] = (csrf, time.time() + 3600)
    return token, csrf


def valid_guest(token: str | None, csrf: str | None) -> bool:
    if not token or not csrf:
        return False
    with _auth_lock:
        record = _guest_tokens.get(hashlib.sha256(token.encode()).hexdigest())
    return bool(record and record[1] > time.time() and hmac.compare_digest(csrf,record[0]))


def guest_rate_allowed(ip: str, *, record: bool=False) -> bool:
    ts = time.time()
    with _auth_lock:
        attempts = _guest_submissions[ip]
        while attempts and attempts[0] < ts - 900:
            attempts.popleft()
        while _global_submissions and _global_submissions[0] < ts - 3600:
            _global_submissions.popleft()
        if len(attempts) >= 3 or len(_global_submissions) >= 20:
            return False
        if record:
            attempts.append(ts)
            _global_submissions.append(ts)
        return True
