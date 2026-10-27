"""Optionally count prints that reached CUPS without Folio: AirPrint from
phones, computers printing to a shared printer, and so on.

Off by default, because Folio's statistics normally cover only what Folio
sent. When an administrator switches it on, each print server's newest job
becomes the starting point, so only later jobs are counted. It needs the
server's administrator login (the built-in server has one), because CUPS
shows a job's owner only to its owner or an administrator. Page counts are
what CUPS reports; some printers report none.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from backend import cupsadmin, devices, ipp, stats

ATTRIBUTES = ['job-id', 'job-state', 'job-originating-user-name', 'job-printer-uri', 'time-at-completed',
              'job-impressions-completed', 'job-media-sheets-completed']
COMPLETED = 9
_lock = threading.Lock()
_last_run = 0.0


def import_server(data_dir: Path, server: str) -> int:
    """Add the server's newly completed jobs that Folio did not send; returns how many."""
    conn = cupsadmin.connection(data_dir, server, admin=True, timeout=10)
    response = conn.request(ipp.GET_JOBS, [
        (ipp.URI, 'printer-uri', conn.printer_uri()), (ipp.KEYWORD, 'which-jobs', 'completed'),
        (ipp.KEYWORD, 'requested-attributes', ATTRIBUTES)], timeout=15)
    jobs = response.all(ipp.JOB)
    first = lambda job, key, default=None: (job.get(key) or [default])[0]
    newest = max((first(job, 'job-id', 0) or 0 for job in jobs), default=0)
    start = stats.watermark(data_dir, server)
    if start is None or newest < start:
        # Just switched on (or the server was reinstalled): count from here.
        stats.set_watermark(data_dir, server, newest)
        return 0
    added = 0
    for job in sorted(jobs, key=lambda job: first(job, 'job-id', 0) or 0):
        job_id = first(job, 'job-id', 0) or 0
        if job_id <= start or first(job, 'job-state') != COMPLETED or stats.has_job(data_dir, server, job_id):
            continue  # Already seen, not printed, or Folio's own job
        uri = first(job, 'job-printer-uri', '') or ''
        queue = uri.rsplit('/printers/', 1)[1] if '/printers/' in uri else ''
        pages = max(0, first(job, 'job-impressions-completed', 0) or 0)
        sheets = max(0, first(job, 'job-media-sheets-completed', 0) or 0)
        stats.record(data_dir, user=str(first(job, 'job-originating-user-name', '') or 'unknown'), server=server,
                     queue=queue or 'unknown', origin='cups', pages=pages or sheets, sides='unknown',
                     sheets=sheets or pages, cups_job=job_id, when=first(job, 'time-at-completed') or None)
        added += 1
    stats.set_watermark(data_dir, server, newest)
    return added


def import_jobs(data_dir: Path, servers: list[str], min_interval: float = 0) -> int:
    """Run an import on every server Folio can administer, if the setting is on.

    ``min_interval`` skips the run when one happened that recently (page loads).
    """
    global _last_run
    if not stats.settings(data_dir)['include_cups_jobs']:
        return 0
    with _lock:
        if time.monotonic() - _last_run < min_interval:
            return 0
        _last_run = time.monotonic()
        added = 0
        for server in servers:
            if devices.cups_backoff(server) or not cupsadmin.admin_status(data_dir, server)['connected']:
                continue
            try:
                added += import_server(data_dir, server)
            except (ipp.IPPError, OSError):
                continue  # Try again next time
        return added
