"""Notification emails (each kind can be switched off in Devices → Email).

* Toner or ink running low, and a printer needing attention (out of paper, a
  jam, a cover open): to administrators, from a check every few minutes.
* Someone used up their pages for the month: to that person.
* Someone accepted an invitation: to administrators.
* A password was changed: always, to the account's owner, so a change they
  didn't make doesn't go unnoticed.

Administrators receive them at the address on their own account. A
condition is reported once, when it appears; it is reported again only after
it has cleared and comes back. What has been reported is kept in
access.sqlite3 (table ``notices``), so a restart doesn't repeat anything.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from backend import mail, security

# Printer states worth a person's trip to the printer. CUPS adds -error,
# -warning or -report to these; "offline" is left out on purpose (printers
# that sleep or are switched off at night would report it every evening).
ATTENTION = {'media-empty': 'Out of paper', 'media-needed': 'Needs paper', 'media-jam': 'Paper jam',
             'door-open': 'A door is open', 'cover-open': 'A cover is open', 'toner-empty': 'Out of toner',
             'marker-supply-empty': 'Out of toner or ink', 'input-tray-missing': 'A paper tray is missing',
             'output-area-full': 'The output tray is full'}


def _init(db) -> None:
    db.execute('CREATE TABLE IF NOT EXISTS notices (key TEXT PRIMARY KEY, at INTEGER NOT NULL)')


def _seen(data_dir: Path, prefix: str) -> set[str]:
    with security.connection(data_dir) as db:
        _init(db)
        return {row[0] for row in db.execute('SELECT key FROM notices WHERE key LIKE ?', (prefix + '%',))}


def _remember(data_dir: Path, keys: set[str]) -> None:
    with security.connection(data_dir) as db:
        _init(db)
        db.executemany('INSERT OR IGNORE INTO notices VALUES (?,?)', [(key, int(time.time())) for key in keys])


def _forget(data_dir: Path, keys: set[str]) -> None:
    with security.connection(data_dir) as db:
        _init(db)
        db.executemany('DELETE FROM notices WHERE key=?', [(key,) for key in keys])


def _signature(data_dir: Path) -> str:
    url = mail.public_url(data_dir)
    return f'\n\n— Folio{f" at {url}" if url else ""}\n(Administrators can choose which emails Folio sends in Devices → Email.)\n'


def _to_admins(data_dir: Path, subject: str, body: str) -> int:
    recipients = security.admin_emails(data_dir)
    for address in recipients:
        mail.send_later(data_dir, address, subject, body + _signature(data_dir))
    return len(recipients)


def attention_reasons(reasons: list[str]) -> list[str]:
    """The printer states in ATTENTION, without CUPS's -error/-warning/-report endings."""
    found = []
    for reason in reasons:
        base = reason
        for ending in ('-error', '-warning', '-report'):
            base = base.removesuffix(ending)
        if base in ATTENTION and base not in found:
            found.append(base)
    return found


def check_printers(data_dir: Path, printers: list[dict]) -> int:
    """``printers``: what was just read from each printer that answered, as
    {'id', 'label', 'supplies': [...], 'reasons': [...]}. Emails administrators
    about what is new and re-arms what has cleared; returns emails queued."""
    sent = 0
    # Keys: kind, printer id and what it is about, separated by tabs (printer ids hold colons).
    for kind, current in (
            ('supplies', {f"supplies\t{p['id']}\t{s['name']}": (p, f"{s['name']}: {s['level']} % left")
                          for p in printers for s in p.get('supplies', []) if s.get('low')}),
            ('attention', {f"attention\t{p['id']}\t{r}": (p, ATTENTION[r])
                           for p in printers for r in attention_reasons(p.get('reasons', []))})):
        checked = {p['id'] for p in printers}
        seen = _seen(data_dir, f'{kind}\t')
        cleared = {key for key in seen if key.split('\t')[1] in checked and key not in current}
        _forget(data_dir, cleared)  # Reported again if it comes back
        new = {key: value for key, value in current.items() if key not in seen}
        if not new or not mail.notification_on(data_dir, kind) or not security.admin_emails(data_dir):
            continue  # Not remembered either: reported once email works
        lines, labels = [], []
        for printer, text in new.values():
            lines.append(f"• {printer['label']}: {text}")
            if printer['label'] not in labels:
                labels.append(printer['label'])
        if kind == 'supplies':
            subject = f"Toner or ink is running low on {', '.join(labels)}"
            body = 'Time to order a replacement:\n\n' + '\n'.join(lines)
        else:
            subject = f"{', '.join(labels)} needs attention"
            body = 'A printer reports a problem that someone has to fix at the printer:\n\n' + '\n'.join(lines)
            body += '\n\nPrint jobs wait until it is fixed.'
        sent += _to_admins(data_dir, subject, body)
        _remember(data_dir, set(new))
    return sent


def quota_reached(data_dir: Path, username: str, limit: int) -> bool:
    """Tell a person, once a month, that they have used all their pages."""
    month = datetime.now().strftime('%Y-%m')
    key = f'quota:{username}:{month}'
    owner = security.account(data_dir, username)
    if not owner or not owner['email'] or not mail.notification_on(data_dir, 'quota') or key in _seen(data_dir, key):
        return False
    month_name = datetime.now().strftime('%B')
    mail.send_later(data_dir, owner['email'], 'You have used your printing pages for this month',
                    f"Hello {username},\n\nYou have printed all {limit} of your pages for {month_name}. Folio won’t "
                    'print more for you until next month, unless an administrator raises your limit.'
                    + _signature(data_dir))
    _remember(data_dir, {key})
    return True


def joined(data_dir: Path, username: str, role: str) -> int:
    if not mail.notification_on(data_dir, 'joined'):
        return 0
    kinds = {'admin': 'an administrator', 'member': 'a member', 'printer': 'a print-only account'}
    return _to_admins(data_dir, f'{username} has joined Folio',
                      f'{username} accepted the invitation, chose a password and signed in, as {kinds.get(role, role)}.')


def password_changed(data_dir: Path, username: str, how: str) -> bool:
    """Always sent when email is set up: a change the owner didn't make must not go unnoticed."""
    owner = security.account(data_dir, username)
    if not owner or not owner['email'] or not mail.configured(mail.settings(data_dir)):
        return False
    reasons = {'reset': 'through a link sent to this address', 'self': 'by you, while signed in',
               'admin': 'by an administrator'}
    mail.send_later(data_dir, owner['email'], 'Your Folio password was changed',
                    f'Hello {username},\n\nThe password of your Folio account was just changed {reasons[how]}. '
                    'Everywhere you were signed in, you have been signed out.\n\nIf you didn’t expect this, '
                    'tell an administrator straight away.' + _signature(data_dir))
    return True


# --- Account emails (sent whenever email is set up) -----------------------------------

def invitation(data_dir: Path, username: str, address: str, link: str, inviter: str) -> None:
    """Sent while the administrator waits, so a problem shows at once (MailError)."""
    mail.send(data_dir, address, 'You are invited to Folio',
              f'Hello {username},\n\n{inviter} has made you an account on Folio, the scanning and printing desk. '
              f'To start, choose your password here:\n\n{link}\n\nYour username is {username}. The link works '
              'once, within 48 hours; after that, ask for a new one.' + _signature(data_dir), automatic=False)


def reset(data_dir: Path, username: str, address: str, link: str, *, wait: bool = False) -> None:
    subject = 'Choose a new Folio password'
    body = (f'Hello {username},\n\nSomeone (hopefully you) asked to reset the password of your Folio account. '
            f'To choose a new one, open this link:\n\n{link}\n\nIt works once, within one hour. If you didn’t '
            'ask for this, ignore this email; your password stays as it is.' + _signature(data_dir))
    if wait:
        mail.send(data_dir, address, subject, body, automatic=False)
    else:
        mail.send_later(data_dir, address, subject, body)
