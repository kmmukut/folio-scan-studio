"""Folio's local user accounts, roles, and bounded guest print sessions.

Migration-safe: existing v0.3 users and sessions remain valid on upgrade.
All credentials live in /data/access.sqlite3; passwords are salted PBKDF2 hashes.

Since 0.20 an account can have an email address, and a password can be set
through a one-time link: an invitation (48 hours; the account waits,
"pending", until the person chooses a password) or a password reset (one
hour). Only a hash of each link's token is stored, and using a link ends
every other sign-in of that account.
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
EMAIL_PATTERN = re.compile(r'^[^@\s<>(),;:"\\]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?'
                           r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$')
LINK_SECONDS = {'invite': 48 * 60 * 60, 'reset': 60 * 60}
_login_attempts: dict[str, deque] = defaultdict(deque)
_reset_requests: dict[str, deque] = defaultdict(deque)
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
        if 'monthly_pages' not in columns:  # Print quota: NULL = the default, -1 = no limit
            db.execute('ALTER TABLE users ADD COLUMN monthly_pages INTEGER')
        if 'email' not in columns:
            db.execute('ALTER TABLE users ADD COLUMN email TEXT')
        if 'pending' not in columns:  # Invited, no password chosen yet
            db.execute('ALTER TABLE users ADD COLUMN pending INTEGER NOT NULL DEFAULT 0')
        db.execute('CREATE TABLE IF NOT EXISTS password_links (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, '
                   'purpose TEXT NOT NULL, expires INTEGER NOT NULL)')
        db.execute('DELETE FROM password_links WHERE expires < ?', (int(time.time()),))
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
        row = db.execute('SELECT salt,password_hash,role,enabled,pending FROM users WHERE username=?', (username,)).fetchone()
    salt = row[0] if row else b'\0' * 24
    candidate = hash_password(password, salt)
    if not row or not hmac.compare_digest(candidate, row[1]) or not row[3] or row[4]:
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


def _set_password(db, username: str, password: str) -> None:
    salt = secrets.token_bytes(24)
    db.execute('UPDATE users SET salt=?, password_hash=?, pending=0 WHERE username=?',
               (salt, hash_password(password, salt), username))
    db.execute('DELETE FROM sessions WHERE username=?', (username,))
    db.execute('DELETE FROM password_links WHERE username=?', (username,))


def update_password(data_dir: Path, username: str, old_password: str, new_password: str):
    if not 12 <= len(new_password) <= 256:
        return False
    with connection(data_dir) as db:
        row = db.execute('SELECT salt,password_hash FROM users WHERE username=?', (username,)).fetchone()
        if not row or not hmac.compare_digest(hash_password(old_password, row[0]), row[1]):
            return False
        _set_password(db, username, new_password)
    return True


def list_users(data_dir: Path):
    with connection(data_dir) as db:
        rows = db.execute('''SELECT u.username,u.role,u.enabled,u.created,u.monthly_pages,u.email,u.pending,
                                    (SELECT max(expires) FROM password_links l WHERE l.username=u.username
                                     AND l.purpose='invite' AND l.expires >= ?)
                             FROM users u ORDER BY u.created,u.username''', (int(time.time()),)).fetchall()
    return [{'username': r[0], 'role': r[1], 'enabled': bool(r[2]), 'created': r[3], 'monthly_pages': r[4],
             'email': r[5] or None, 'pending': bool(r[6]), 'invite_expires': r[7]} for r in rows]


# --- Email addresses and password links (0.20) -------------------------------------

def clean_email(text: str | None) -> str | None:
    """A tidy address, or None for none; ValueError if it can't be one."""
    text = (text or '').strip()
    if not text:
        return None
    if len(text) > 254 or not EMAIL_PATTERN.fullmatch(text):
        raise ValueError(f'“{text[:60]}” is not an email address')
    return text


def _check_email_free(db, email: str | None, username: str) -> None:
    if email and db.execute('SELECT 1 FROM users WHERE lower(email)=lower(?) AND username!=?',
                            (email, username)).fetchone():
        raise ValueError('Another account already uses this email address')


def set_email(data_dir: Path, username: str, email: str | None) -> None:
    email = clean_email(email)
    with connection(data_dir) as db:
        _check_email_free(db, email, username)
        if not db.execute('UPDATE users SET email=? WHERE username=?', (email, username)).rowcount:
            raise ValueError('User does not exist')


def account(data_dir: Path, username: str) -> dict | None:
    with connection(data_dir) as db:
        row = db.execute('SELECT username,role,enabled,email,pending FROM users WHERE username=?', (username,)).fetchone()
    return {'username': row[0], 'role': row[1], 'enabled': bool(row[2]), 'email': row[3] or None,
            'pending': bool(row[4])} if row else None


def admin_emails(data_dir: Path) -> list[str]:
    """Where notifications for administrators go."""
    with connection(data_dir) as db:
        rows = db.execute("SELECT email FROM users WHERE role='admin' AND enabled=1 AND pending=0 "
                          "AND email IS NOT NULL AND email!='' ORDER BY created").fetchall()
    return [r[0] for r in rows]


def create_link(data_dir: Path, username: str, purpose: str) -> str:
    """A new one-time link for this account (replacing older ones for the same purpose)."""
    token = secrets.token_urlsafe(32)
    with connection(data_dir) as db:
        if not db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
            raise ValueError('User does not exist')
        db.execute('DELETE FROM password_links WHERE username=? AND purpose=?', (username, purpose))
        db.execute('INSERT INTO password_links VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(),
                   username, purpose, int(time.time() + LINK_SECONDS[purpose])))
    return token


def invite_user(data_dir: Path, username: str, email: str | None, role: str) -> str:
    """An account that waits for its person to choose a password; returns the invitation token."""
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError('Username needs 3–32 letters, numbers, dots, underscores or dashes (starting with a letter)')
    if role not in ROLES:
        raise ValueError('Invalid account role')
    email = clean_email(email)
    try:
        with connection(data_dir) as db:
            _check_email_free(db, email, username)
            # A random hash no password can match, until the invitation is used.
            db.execute('INSERT INTO users (username,salt,password_hash,role,enabled,created,email,pending) '
                       'VALUES (?,?,?,?,?,?,?,1)', (username, secrets.token_bytes(24), secrets.token_bytes(32), role, 1,
                                                    int(time.time()), email))
    except sqlite3.IntegrityError as exc:
        raise ValueError('Username already exists') from exc
    return create_link(data_dir, username, 'invite')


def link_account(data_dir: Path, token: str | None) -> dict | None:
    """The account a link is for, if the link is still valid."""
    if not token or len(token) > 100:
        return None
    with connection(data_dir) as db:
        row = db.execute('''SELECT l.username,l.purpose,u.role,u.enabled FROM password_links l
                            JOIN users u ON u.username=l.username WHERE l.token_hash=? AND l.expires>=?''',
                         (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
    if not row or not row[3]:
        return None
    return {'username': row[0], 'purpose': row[1], 'role': row[2]}


def use_link(data_dir: Path, token: str, password: str) -> dict:
    """Set the password through a link and sign in; the link (and every other
    sign-in of the account) stops working."""
    if not 12 <= len(password) <= 256:
        raise ValueError('Password must contain 12–256 characters')
    found = link_account(data_dir, token)
    if not found:
        raise PermissionError('This link has expired or was already used. Ask for a new one.')
    with connection(data_dir) as db:
        _set_password(db, found['username'], password)
    return {**start_session(data_dir, found['username'], found['role']), 'purpose': found['purpose']}


def reset_target(data_dir: Path, login: str) -> dict | None:
    """The account a "forgot password" request is for: by username or email
    address, enabled, with an address to send to."""
    login = (login or '').strip()
    if not login or len(login) > 254:
        return None
    with connection(data_dir) as db:
        row = db.execute('SELECT username,email FROM users WHERE (username=? OR lower(email)=lower(?)) '
                         "AND enabled=1 AND email IS NOT NULL AND email!='' ORDER BY username=? DESC LIMIT 1",
                         (login, login, login)).fetchone()
    return {'username': row[0], 'email': row[1]} if row else None


def link_decoy(data_dir: Path) -> None:
    """For an unknown account: a database write like create_link's, so the
    answer takes about as long as for a real one."""
    with connection(data_dir) as db:
        db.execute('DELETE FROM password_links WHERE token_hash=?', (secrets.token_hex(32),))


def reset_rate_allowed(ip: str, login: str) -> bool:
    """At most 5 requests per address and 3 per account in 15 minutes, 30 an hour in all."""
    now_ = time.time()
    with _auth_lock:
        keys = [(f'ip:{ip}', 5, 900), (f'login:{login.strip().lower()}', 3, 900), ('all', 30, 3600)]
        for key, limit, window in keys:
            attempts = _reset_requests[key]
            while attempts and attempts[0] < now_ - window:
                attempts.popleft()
            if len(attempts) >= limit:
                return False
        for key, _, _ in keys:
            _reset_requests[key].append(now_)
    return True


# --- Print quotas ---------------------------------------------------------------
# Pages per calendar month. Administrators have none; guests have their own
# limits. Each account follows the default unless it has its own number (or
# -1 for no limit).

def quota_default(data_dir: Path) -> int | None:
    with connection(data_dir) as db:
        row = db.execute("SELECT value FROM settings WHERE key='default_monthly_pages'").fetchone()
    return int(row[0]) if row and row[0] != '' else None


def set_quota_default(data_dir: Path, pages: int | None) -> None:
    if pages is not None and not 0 <= pages <= 100000:
        raise ValueError('Choose between 0 and 100,000 pages a month, or no limit')
    with connection(data_dir) as db:
        db.execute("INSERT INTO settings (key,value) VALUES ('default_monthly_pages', ?) "
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('' if pages is None else str(pages),))


def set_quota(data_dir: Path, username: str, pages: int | None) -> None:
    """``None``: follow the default; -1: no limit; otherwise pages a month."""
    if pages is not None and not -1 <= pages <= 100000:
        raise ValueError('Choose between 0 and 100,000 pages a month, no limit, or the default')
    with connection(data_dir) as db:
        if not db.execute('UPDATE users SET monthly_pages=? WHERE username=?', (pages, username)).rowcount:
            raise ValueError('User does not exist')


def quota_for(data_dir: Path, username: str) -> int | None:
    """The pages this person may print a month; ``None`` means no limit."""
    with connection(data_dir) as db:
        row = db.execute('SELECT role,monthly_pages FROM users WHERE username=?', (username,)).fetchone()
    if not row or row[0] == 'admin' or row[1] == -1:
        return None
    return row[1] if row[1] is not None else quota_default(data_dir)


def add_user(data_dir: Path, username: str, password: str, role: str, email: str | None = None):
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError('Username needs 3–32 letters, numbers, dots, underscores or dashes (starting with a letter)')
    if not 12 <= len(password) <= 256:
        raise ValueError('Password must contain 12–256 characters')
    if role not in ROLES:
        raise ValueError('Invalid account role')
    email = clean_email(email)
    salt = secrets.token_bytes(24)
    try:
        with connection(data_dir) as db:
            _check_email_free(db, email, username)
            db.execute('INSERT INTO users (username,salt,password_hash,role,enabled,created,email) VALUES (?,?,?,?,?,?,?)',
                       (username, salt, hash_password(password,salt), role, 1, int(time.time()), email))
    except sqlite3.IntegrityError as exc:
        raise ValueError('Username already exists') from exc
    return list_users(data_dir)


def edit_user(data_dir: Path, username: str, actor: str, *, role=None, enabled=None, password=None, email=False):
    """``email``: False leaves it; None or '' removes it."""
    if email is not False:
        email = clean_email(email)
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
        if email is not False:
            _check_email_free(db, email, username)
            db.execute('UPDATE users SET email=? WHERE username=?', (email, username))
        if password is not None:
            _set_password(db, username, password)
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
        db.execute('DELETE FROM password_links WHERE username=?',(username,))
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
