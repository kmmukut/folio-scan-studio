"""Email: invitations, password resets and notifications, sent through an SMTP
server the administrator sets up (Devices → Email).

The settings live in access.sqlite3 (Folio's private accounts database), so
the SMTP password never leaves the server and is part of backups like the
other logins. Messages are plain text and go to one recipient each, so no
one sees another person's address.

Most messages are sent by a background thread: a slow mail server never
holds up a page, and asking for a password reset takes as long for an
unknown account as for a real one. The test message is sent while the
administrator waits, so any problem shows at once.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import smtplib
import socket
import ssl
import threading
import time
import urllib.parse
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from backend import security

log = logging.getLogger('folio.mail')

SECURITY = ('starttls', 'ssl', 'none')
DEFAULT_PORTS = {'starttls': 587, 'ssl': 465, 'none': 25}
NOTIFICATIONS = {
    'supplies': 'Toner or ink is running low',
    'attention': 'A printer needs attention (out of paper, a jam, a cover open)',
    'quota': 'Someone has used up their pages for the month (sent to them)',
    'joined': 'Someone accepted an invitation',
}
DEFAULTS = {'host': '', 'port': 587, 'security': 'starttls', 'username': '', 'password': '', 'sender': '',
            'verify_tls': True, 'public_url': '', 'password_reset': True,
            'notify': {kind: True for kind in NOTIFICATIONS}}

_outbox: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_last = {'at': None, 'ok': None, 'error': None}


class MailError(Exception):
    """The mail server could not be reached or refused the message."""


# --- Settings -------------------------------------------------------------------------

def settings(data_dir: Path) -> dict:
    with security.connection(data_dir) as db:
        row = db.execute("SELECT value FROM settings WHERE key='mail'").fetchone()
    try:
        stored = json.loads(row[0]) if row else {}
    except ValueError:
        stored = {}
    merged = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    merged['notify'] = {**DEFAULTS['notify'], **(stored.get('notify') or {})}
    return merged


def clean_public_url(url: str) -> str:
    """Folio's address as people reach it, for links in emails. Links are never
    built from the request, whose Host header anyone can set."""
    url = (url or '').strip().rstrip('/')
    if not url:
        return ''
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.query or parts.fragment or parts.username:
        raise ValueError('Enter Folio’s address as people open it, such as https://folio.example.com')
    return url


def save(data_dir: Path, body: dict) -> dict:
    """Store new settings; an empty password keeps the stored one."""
    current = settings(data_dir)
    host = (body.get('host') or '').strip()
    if host and (len(host) > 253 or any(c in host for c in ' /@:')):
        raise ValueError('Enter the mail server’s name, such as smtp.example.com')
    mode = body.get('security') or 'starttls'
    if mode not in SECURITY:
        raise ValueError('Choose STARTTLS, SSL/TLS or no encryption')
    port = int(body.get('port') or DEFAULT_PORTS[mode])
    if not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    sender = (body.get('sender') or '').strip()
    if host and not sender:
        raise ValueError('Enter the address emails come from')
    if sender:
        sender = security.clean_email(sender)
    password = body.get('password') or ''
    username = (body.get('username') or '').strip()
    if len(username) > 254 or len(password) > 512:
        raise ValueError('The mail server login is too long')
    notify = body.get('notify') or {}
    updated = {'host': host, 'port': port, 'security': mode, 'username': username,
               'password': (password or current['password']) if username else '', 'sender': sender,
               'verify_tls': bool(body.get('verify_tls', True)),
               'public_url': clean_public_url(body.get('public_url', '')),
               'password_reset': bool(body.get('password_reset', True)),
               'notify': {kind: bool(notify.get(kind, True)) for kind in NOTIFICATIONS}}
    with security.connection(data_dir) as db:
        db.execute("INSERT INTO settings (key,value) VALUES ('mail', ?) "
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (json.dumps(updated),))
    return public(data_dir)


def remove(data_dir: Path) -> dict:
    with security.connection(data_dir) as db:
        db.execute("DELETE FROM settings WHERE key='mail'")
    return public(data_dir)


def public(data_dir: Path) -> dict:
    """What browsers may see: never the password."""
    cfg = settings(data_dir)
    return {**{k: v for k, v in cfg.items() if k != 'password'}, 'has_password': bool(cfg['password']),
            'configured': configured(cfg), 'public_url': public_url(data_dir, cfg) or '',
            'public_url_from_env': bool(os.environ.get('FOLIO_PUBLIC_URL')) and not cfg['public_url'],
            'reset_available': reset_available(data_dir, cfg), 'notifications': NOTIFICATIONS, 'last': last_result()}


def configured(cfg: dict) -> bool:
    return bool(cfg['host'] and cfg['sender'])


def public_url(data_dir: Path, cfg: dict | None = None) -> str | None:
    cfg = cfg or settings(data_dir)
    if cfg['public_url']:
        return cfg['public_url']
    try:
        return clean_public_url(os.environ.get('FOLIO_PUBLIC_URL', '')) or None
    except ValueError:
        return None


def reset_available(data_dir: Path, cfg: dict | None = None) -> bool:
    """Whether "Forgot password?" can work: email set up, a public address, and on."""
    cfg = cfg or settings(data_dir)
    return configured(cfg) and cfg['password_reset'] and bool(public_url(data_dir, cfg))


def notification_on(data_dir: Path, kind: str) -> bool:
    cfg = settings(data_dir)
    return configured(cfg) and cfg['notify'].get(kind, False)


def password_link(data_dir: Path, token: str) -> str | None:
    """The link in an invitation or reset email. The token is in the fragment,
    which browsers never send to servers or proxies, so it stays out of logs."""
    base = public_url(data_dir)
    return f'{base}/#password={token}' if base else None


# --- Sending ----------------------------------------------------------------------------

def message(cfg: dict, to: str, subject: str, body: str, *, automatic: bool = True) -> EmailMessage:
    mail = EmailMessage()
    mail['From'] = formataddr(('Folio', cfg['sender']))
    mail['To'] = to
    mail['Subject'] = subject
    mail['Date'] = formatdate(localtime=True)
    mail['Message-ID'] = make_msgid(domain=cfg['sender'].rsplit('@', 1)[1])
    if automatic:
        mail['Auto-Submitted'] = 'auto-generated'  # No out-of-office replies, please
    mail.set_content(body)
    return mail


def _connect(cfg: dict, timeout: float) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if not cfg['verify_tls']:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # The administrator chose to accept a self-signed certificate
    if cfg['security'] == 'ssl':
        server = smtplib.SMTP_SSL(cfg['host'], cfg['port'], timeout=timeout, context=context)
    else:
        server = smtplib.SMTP(cfg['host'], cfg['port'], timeout=timeout)
        if cfg['security'] == 'starttls':
            server.starttls(context=context)
    if cfg['username']:
        server.login(cfg['username'], cfg['password'])
    return server


def send(data_dir: Path, to: str, subject: str, body: str, *, automatic: bool = True, timeout: float = 20) -> None:
    """Send one message now; raises MailError with a reason a person can act on."""
    cfg = settings(data_dir)
    if not configured(cfg):
        raise MailError('Email is not set up. Add a mail server in Devices → Email.')
    try:
        with _connect(cfg, timeout) as server:
            server.send_message(message(cfg, to, subject, body, automatic=automatic))
    except smtplib.SMTPAuthenticationError as exc:
        error = 'The mail server did not accept the username or password'
        raise _failed(error) from exc
    except smtplib.SMTPNotSupportedError as exc:
        raise _failed('The mail server doesn’t offer STARTTLS. Choose SSL/TLS (usually port 465).') from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise _failed(f'The mail server refused the address {to}') from exc
    except smtplib.SMTPSenderRefused as exc:
        raise _failed(f'The mail server refused the sender {cfg["sender"]}: {_reason(exc)}') from exc
    except ssl.SSLCertVerificationError as exc:
        raise _failed('The mail server’s certificate could not be verified. If it is self-signed, '
                      'switch off certificate checking.') from exc
    except ssl.SSLError as exc:
        raise _failed(f'Encryption failed ({exc.reason or exc}). Check the port and encryption setting.') from exc
    except smtplib.SMTPResponseException as exc:
        raise _failed(f'The mail server answered {exc.smtp_code}: {_reason(exc)}') from exc
    except (socket.timeout, TimeoutError) as exc:
        raise _failed(f'The mail server at {cfg["host"]}:{cfg["port"]} did not answer in time') from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise _failed(f'Could not reach the mail server at {cfg["host"]}:{cfg["port"]} ({exc})') from exc
    _last.update(at=time.time(), ok=True, error=None)


def _reason(exc: smtplib.SMTPResponseException) -> str:
    text = exc.smtp_error.decode('utf-8', 'replace') if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
    return text[:200]


def _failed(error: str) -> MailError:
    _last.update(at=time.time(), ok=False, error=error)
    return MailError(error)


def last_result() -> dict:
    """The most recent attempt to send, for Devices and System check."""
    return dict(_last)


def send_later(data_dir: Path, to: str, subject: str, body: str) -> None:
    """Queue a message for the background sender; failures are logged and shown
    in System check, never raised."""
    global _worker
    _outbox.put((Path(data_dir), to, subject, body))
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_deliver, daemon=True, name='folio-mail')
            _worker.start()


def _deliver() -> None:
    while True:
        data_dir, to, subject, body = _outbox.get()
        try:
            send(data_dir, to, subject, body)
        except MailError as exc:
            log.warning('Email to %s not sent: %s', to, exc)
        except Exception:  # noqa: BLE001 - one bad message must not stop the sender
            log.warning('Email to %s not sent', to, exc_info=True)
        finally:
            _outbox.task_done()


def wait_until_sent(timeout: float = 10) -> bool:
    """For tests and shutdown: True once the queue is empty."""
    deadline = time.monotonic() + timeout
    while _outbox.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(.02)
    return not _outbox.unfinished_tasks
