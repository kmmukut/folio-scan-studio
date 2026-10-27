"""Folio's own print log, for usage statistics.

One row per job Folio hands to CUPS: who, which printer, when, and how many
pages and sheets. It lives in its own file (data/print-log.sqlite3), apart
from accounts, so it can be switched off or deleted without touching
anything else. Document titles and contents are never stored. Jobs printed
from other computers straight to CUPS are not counted; Folio only reports
what it sent itself.

Times are grouped by the container's time zone (the TZ setting).
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

DB_NAME = 'print-log.sqlite3'
DEFAULT_RETENTION_DAYS = 365
RETENTION_CHOICES = (90, 365, 730, 1825, 0)  # 0 keeps everything
PERIODS = {'30d': 'Last 30 days', 'month': 'This month', 'last-month': 'Last month',
           'year': 'This year', '12m': 'Last 12 months', 'all': 'All time'}
ORIGINS = ('document', 'upload', 'test-page')
COLORS = ('colour', 'greyscale', 'auto')
REMOVED_ACCOUNT = ''  # What a deleted account's name becomes


def db_path(data_dir: Path) -> Path:
    return data_dir / DB_NAME


@contextmanager
def connection(data_dir: Path):
    db = sqlite3.connect(db_path(data_dir), timeout=10)
    try:
        with db:
            yield db
    finally:
        db.close()


def init(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with connection(data_dir) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS print_log (
            id INTEGER PRIMARY KEY,
            printed_at INTEGER NOT NULL,
            user TEXT NOT NULL,
            server TEXT NOT NULL,
            queue TEXT NOT NULL,
            origin TEXT NOT NULL,
            pages INTEGER NOT NULL,      -- document pages in one copy
            copies INTEGER NOT NULL,
            nup INTEGER NOT NULL,
            sides TEXT NOT NULL,
            sheets INTEGER NOT NULL,     -- paper used by all copies
            color TEXT NOT NULL,
            media TEXT NOT NULL,
            cups_job INTEGER,
            status TEXT NOT NULL DEFAULT 'sent')''')
        db.execute('CREATE INDEX IF NOT EXISTS print_log_time ON print_log (printed_at)')
        db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('PRAGMA user_version=1')


def settings(data_dir: Path) -> dict:
    with connection(data_dir) as db:
        values = dict(db.execute('SELECT key, value FROM settings'))
    return {'enabled': values.get('enabled', 'on') == 'on',
            'retention_days': int(values.get('retention_days', DEFAULT_RETENTION_DAYS))}


def save_settings(data_dir: Path, *, enabled: bool, retention_days: int) -> dict:
    if retention_days not in RETENTION_CHOICES:
        raise ValueError('Choose how long to keep statistics from the list')
    with connection(data_dir) as db:
        for key, value in (('enabled', 'on' if enabled else 'off'), ('retention_days', str(retention_days))):
            db.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
    prune(data_dir)
    return settings(data_dir)


def sheets_used(pages: int, copies: int, nup: int, sides: str) -> int:
    """Sheets of paper for a job: pages are placed ``nup`` to a side, on one or both sides."""
    faces = -(-max(pages, 0) // max(nup, 1))
    per_copy = faces if sides == 'one-sided' else -(-faces // 2)
    return per_copy * max(copies, 1)


def color_of(color_mode: str, printer_has_color: bool | None) -> str:
    """What was asked for. On a colour printer "Automatic" is the printer's call."""
    if color_mode in ('gray', 'bw') or printer_has_color is False:
        return 'greyscale'
    return 'colour' if color_mode == 'color' else 'auto'


def record(data_dir: Path, *, user: str, server: str, queue: str, origin: str, pages: int,
           copies: int = 1, nup: int = 1, sides: str = 'one-sided', color: str = 'auto',
           media: str = 'default', cups_job: int | None = None, when: float | None = None) -> bool:
    """Add one job to the log; returns False when statistics are switched off."""
    if not settings(data_dir)['enabled']:
        return False
    with connection(data_dir) as db:
        db.execute('INSERT INTO print_log (printed_at, user, server, queue, origin, pages, copies, nup, '
                   'sides, sheets, color, media, cups_job) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (int(when or time.time()), user, server, queue, origin, pages, copies, nup, sides,
                    sheets_used(pages, copies, nup, sides), color, media, cups_job))
    prune(data_dir)
    return True


def mark_cancelled(data_dir: Path, server: str, cups_job: int) -> None:
    """A job cancelled in Folio before it finished no longer counts as printed."""
    with connection(data_dir) as db:
        db.execute("UPDATE print_log SET status='cancelled' WHERE server=? AND cups_job=?", (server, cups_job))


def forget_user(data_dir: Path, username: str) -> None:
    """Deleting an account removes its name; its pages still count for the printer."""
    with connection(data_dir) as db:
        db.execute('UPDATE print_log SET user=? WHERE user=?', (REMOVED_ACCOUNT, username))


def prune(data_dir: Path, now: float | None = None) -> None:
    days = settings(data_dir)['retention_days']
    if days:
        with connection(data_dir) as db:
            db.execute('DELETE FROM print_log WHERE printed_at < ?', (int((now or time.time()) - days * 86400),))


def clear(data_dir: Path) -> int:
    with connection(data_dir) as db:
        return db.execute('DELETE FROM print_log').rowcount


# --- Reporting ---------------------------------------------------------------

def _month_start(moment: datetime, months_back: int = 0) -> datetime:
    index = moment.year * 12 + moment.month - 1 - months_back
    return moment.replace(year=index // 12, month=index % 12 + 1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)


def period_range(period: str, now: float | None = None, oldest: float | None = None) -> tuple[datetime, datetime]:
    """Start and end (local time) of a named period. The end is exclusive."""
    current = datetime.fromtimestamp(now or time.time())
    end = current + timedelta(seconds=1)
    if period == '30d':
        start = (current - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'month':
        start = _month_start(current)
    elif period == 'last-month':
        start, end = _month_start(current, 1), _month_start(current)
    elif period == 'year':
        start = _month_start(current, current.month - 1)
    elif period == '12m':
        start = _month_start(current, 11)
    elif period == 'all':
        start = _month_start(datetime.fromtimestamp(oldest) if oldest else current)
    else:
        raise ValueError('Unknown period')
    return start, end


def _buckets(start: datetime, end: datetime) -> tuple[str, list[str]]:
    """Days for short periods, months for long ones, every one listed (empty ones too)."""
    last = end - timedelta(seconds=1)
    if (last - start).days < 62:
        count = (last.date() - start.date()).days + 1
        return 'day', [(start + timedelta(days=n)).strftime('%Y-%m-%d') for n in range(count)]
    first, final = start.year * 12 + start.month - 1, last.year * 12 + last.month - 1
    return 'month', [f'{index // 12:04d}-{index % 12 + 1:02d}' for index in range(first, final + 1)]


def _totals(rows: list[tuple]) -> dict:
    jobs = pages = sheets = 0
    by_color = dict.fromkeys(COLORS, 0)
    for pages_one, copies, job_sheets, color in rows:
        jobs += 1
        pages += pages_one * copies
        sheets += job_sheets
        by_color[color] = by_color.get(color, 0) + pages_one * copies
    return {'jobs': jobs, 'pages': pages, 'sheets': sheets, 'sheets_saved': pages - sheets,
            'colour_pages': by_color['colour'], 'greyscale_pages': by_color['greyscale'],
            'auto_pages': by_color['auto']}


def summary(data_dir: Path, period: str = '12m', now: float | None = None) -> dict:
    prune(data_dir, now)
    with connection(data_dir) as db:
        oldest = db.execute('SELECT MIN(printed_at) FROM print_log').fetchone()[0]
        start, end = period_range(period, now, oldest)
        rows = db.execute('SELECT printed_at, user, server, queue, pages, copies, sheets, color, status '
                          'FROM print_log WHERE printed_at >= ? AND printed_at < ? ORDER BY printed_at',
                          (int(start.timestamp()), int(end.timestamp()))).fetchall()
    sent = [r for r in rows if r[8] == 'sent']
    unit, keys = _buckets(start, end)
    fmt = '%Y-%m-%d' if unit == 'day' else '%Y-%m'
    series = {key: [] for key in keys}
    people: dict[str, list] = {}
    printers: dict[tuple[str, str], list] = {}
    for row in sent:
        values = (row[4], row[5], row[6], row[7])
        series.setdefault(datetime.fromtimestamp(row[0]).strftime(fmt), []).append(values)
        people.setdefault(row[1], []).append(values)
        printers.setdefault((row[2], row[3]), []).append(values)

    def ranked(groups: dict, describe) -> list[dict]:
        items = [{**describe(key), **_totals(values)} for key, values in groups.items()]
        return sorted(items, key=lambda item: (-item['sheets'], -item['pages']))
    return {
        'period': period, 'label': PERIODS[period],
        'start': start.isoformat(timespec='seconds'), 'end': end.isoformat(timespec='seconds'),
        'totals': {**_totals([(r[4], r[5], r[6], r[7]) for r in sent]),
                   'cancelled': len(rows) - len(sent)},
        'unit': unit,
        'series': [{'key': key, **_totals(values)} for key, values in sorted(series.items())],
        'people': ranked(people, lambda user: {'user': user}),
        'printers': ranked(printers, lambda key: {'server': key[0], 'queue': key[1]}),
        'oldest': datetime.fromtimestamp(oldest).isoformat(timespec='seconds') if oldest else None,
        'settings': settings(data_dir),
    }


CSV_COLUMNS = ('printed_at', 'user', 'printer', 'server', 'origin', 'pages', 'copies', 'pages_per_sheet',
               'sides', 'sheets', 'color', 'paper', 'cups_job', 'status')


def export_rows(data_dir: Path, period: str = 'all', now: float | None = None):
    """Rows for a spreadsheet, oldest first."""
    with connection(data_dir) as db:
        oldest = db.execute('SELECT MIN(printed_at) FROM print_log').fetchone()[0]
        start, end = period_range(period, now, oldest)
        rows = db.execute('SELECT printed_at, user, queue, server, origin, pages, copies, nup, sides, sheets, '
                          'color, media, cups_job, status FROM print_log WHERE printed_at >= ? AND printed_at < ? '
                          'ORDER BY printed_at', (int(start.timestamp()), int(end.timestamp()))).fetchall()
    for row in rows:
        yield (datetime.fromtimestamp(row[0]).isoformat(timespec='seconds'), *row[1:])
