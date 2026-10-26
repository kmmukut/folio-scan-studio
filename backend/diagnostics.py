"""Plain-language system checks for administrators.

Each check returns ``{id, label, status, detail}`` where status is one of
``ok``, ``warn``, ``fail`` or ``info``. Checks never change device or CUPS
state, and they report what Folio can observe instead of guessing.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import cupsadmin, devices

LOW_DISK_WARN = 1024 ** 3        # 1 GiB
LOW_DISK_FAIL = 200 * 1024 ** 2  # 200 MiB
LOW_MEMORY_WARN = 256 * 1024 ** 2
STARTED = time.time()


def check(id: str, label: str, status: str, detail: str) -> dict:
    return {'id': id, 'label': label, 'status': status, 'detail': detail}


def human_bytes(value: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.0f} {unit}' if unit in ('B', 'KB') else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} TB'


def storage_problems(data_dir: Path) -> list[str]:
    """Fast checks shared by /api/health: writable data and a readable database."""
    problems = []
    if not os.access(data_dir, os.W_OK):
        problems.append('Data directory is not writable')
    try:
        db = sqlite3.connect(data_dir / 'access.sqlite3', timeout=2)
        try:
            db.execute('SELECT 1').fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        problems.append('Account database cannot be opened')
    return problems


def run_tool(args: list[str], timeout: float = 6) -> tuple[int | None, str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, ''
    except (OSError, subprocess.TimeoutExpired):
        return 1, 'timed out'
    return done.returncode, (done.stdout or done.stderr).strip()


def memory_info() -> tuple[int | None, int | None]:
    """(available bytes, container limit bytes) where the host exposes them."""
    available = limit = None
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                available = int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    for path in ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes'):
        try:
            raw = Path(path).read_text().strip()
            if raw.isdigit() and int(raw) < 1 << 60:
                limit = int(raw)
            break
        except OSError:
            continue
    if limit:
        try:
            used = int(Path('/sys/fs/cgroup/memory.current').read_text().strip())
            available = min(available or limit, max(0, limit - used))
        except (OSError, ValueError):
            pass
    return available, limit


def folder_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def check_storage(data_dir: Path, jobs_dir: Path) -> list[dict]:
    results = []
    problems = storage_problems(data_dir)
    try:
        usage = shutil.disk_usage(data_dir)
        free = usage.free
    except OSError:
        free = None
    if problems:
        results.append(check('storage', 'Data storage', 'fail', '; '.join(problems)))
    elif free is None:
        results.append(check('storage', 'Data storage', 'warn', 'Free space could not be measured'))
    else:
        status = 'fail' if free < LOW_DISK_FAIL else 'warn' if free < LOW_DISK_WARN else 'ok'
        results.append(check('storage', 'Data storage', status,
                             f'{human_bytes(free)} free of {human_bytes(usage.total)} at {data_dir}'))
    documents = [p for p in jobs_dir.glob('*/job.json')] if jobs_dir.is_dir() else []
    library = folder_size(jobs_dir) if jobs_dir.is_dir() else 0
    previews = sum(folder_size(p) for p in jobs_dir.glob('*/cache')) if jobs_dir.is_dir() else 0
    results.append(check('library', 'Document library', 'info',
                         f'{len(documents)} documents, {human_bytes(library)} on disk '
                         f'(of which {human_bytes(previews)} is regenerable preview cache)'))
    return results


def check_memory() -> dict:
    available, limit = memory_info()
    if available is None:
        return check('memory', 'Memory', 'info', 'Not reported by this host')
    status = 'warn' if available < LOW_MEMORY_WARN else 'ok'
    detail = f'{human_bytes(available)} available'
    if limit:
        detail += f' within a {human_bytes(limit)} container limit'
    if status == 'warn':
        detail += '. Large exports, OCR and many previews at once may fail; consider raising the limit.'
    return check('memory', 'Memory', status, detail)


def check_proxy(request, cookie_secure: bool) -> list[dict]:
    """Detect a reverse proxy Folio does not trust, and HTTPS/cookie mismatches."""
    results = []
    client = request.client.host if request.client else 'unknown'
    forwarded = [part.strip() for part in request.headers.get('x-forwarded-for', '').split(',') if part.strip()]
    if forwarded and client not in forwarded:
        results.append(check('proxy', 'Reverse proxy', 'warn',
            f'Requests arrive through a proxy at {client}, but Folio does not trust it, so every '
            'visitor shares that address for sign-in and guest-print limits. Set '
            f'FORWARDED_ALLOW_IPS to the proxy address or its network (for example {client}).'))
    elif forwarded:
        results.append(check('proxy', 'Reverse proxy', 'ok',
                             f'Trusted proxy detected; your browser appears as {client}'))
    else:
        results.append(check('proxy', 'Reverse proxy', 'info',
                             f'Direct connection; your browser appears as {client}'))
    https = request.url.scheme == 'https'
    if https and not cookie_secure:
        results.append(check('cookies', 'Secure cookies', 'warn',
                             'Folio is served over HTTPS but COOKIE_SECURE is false. Set it to true.'))
    elif not https and cookie_secure and forwarded:
        results.append(check('cookies', 'Secure cookies', 'info',
                             'COOKIE_SECURE is on. Folio sees plain HTTP from the proxy, which is '
                             'normal when the proxy terminates HTTPS.'))
    else:
        results.append(check('cookies', 'Secure cookies', 'ok',
                             'Cookie settings match how this page was opened'))
    return results


def probe_cups_server(server: str) -> dict:
    started = time.monotonic()
    code, output = run_tool(['lpstat', '-h', server, '-r'], timeout=6)
    elapsed = (time.monotonic() - started) * 1000
    if code is None:
        return check(f'cups:{server}', f'CUPS {server}', 'fail', 'CUPS client tools are missing from this image')
    if code == 0 and 'is running' in output:
        devices.mark_cups_reachable(server)
        queues = devices.list_cups_queues(server)
        ready = sum(1 for q in queues if q['ready'])
        status = 'ok' if queues else 'warn'
        detail = f'Reachable in {elapsed:.0f} ms; {len(queues)} queues, {ready} ready'
        if not queues:
            detail += '. Add a printer queue in CUPS, then refresh Devices.'
        return check(f'cups:{server}', f'CUPS {server}', status, detail)
    reason = 'did not answer within 6 s' if output == 'timed out' else (output or 'connection refused')[:160]
    return check(f'cups:{server}', f'CUPS {server}', 'fail',
                 f'{reason}. Check the address, that CUPS listens on the network, and port 631.')


def check_printing(cups_servers: list[str], default_printer: str | None, data_dir: Path | None = None) -> list[dict]:
    if not cups_servers:
        return [check('cups', 'Printing', 'warn',
                      'No CUPS server is configured. Set CUPS_SERVERS in the container environment.')]
    with ThreadPoolExecutor(max_workers=min(4, len(cups_servers))) as pool:
        results = list(pool.map(probe_cups_server, cups_servers))
    if data_dir is not None:
        for server in cups_servers:
            login = cupsadmin.admin_status(data_dir, server)
            results.append(check(f'cups-admin:{server}', f'Printer administration on {server}', 'info',
                                 f"Connected as {login['username']}" + (' over TLS' if login['encrypted'] else '')
                                 if login['connected'] else
                                 'Not connected. Connect a CUPS administrator login in Devices to add, '
                                 'remove or pause printers from Folio.'))
    if default_printer:
        server, _, queue = default_printer.partition('|')
        results.append(check('default-printer', 'Default printer', 'info',
                             f'{queue} on {server}' if queue else default_printer))
    else:
        results.append(check('default-printer', 'Default printer', 'warn',
                             'None chosen. Pick one under Devices so Print Desk knows where to send jobs.'))
    return results


def check_scanning(registry: dict) -> list[dict]:
    code, output = run_tool(['scanimage', '--version'])
    if code is None:
        return [check('sane', 'SANE', 'fail', 'scanimage is missing from this image')]
    version = re.search(r'sane-backends\)?\s+([\w.+~-]+)', output)
    results = [check('sane', 'SANE', 'ok', f'sane-backends {version.group(1)}' if version else output[:120])]
    seen = devices.read_sane_devices()
    scanners = [s for s in registry.get('scanners', []) if s.get('enabled', True)]
    if not scanners:
        results.append(check('scanners', 'Scanners', 'warn',
                             f'No scanner registered. SANE currently lists {len(seen)} device(s); '
                             'add or attach one under Devices.'))
    for entry in scanners:
        found = devices.match_scanner(entry, seen)
        where = entry.get('ip') or entry.get('hint') or 'configured device'
        if found:
            results.append(check(f"scanner:{entry['id']}", entry['name'], 'ok',
                                 f"Listed by SANE as {found['sane_name']}"))
        else:
            hint = ('Check that it is powered on, on the same network, and that the protocol, '
                    'port and path are right.' if entry.get('kind') == 'managed' else
                    'Check its connection. A USB scanner must be passed through to the container.')
            results.append(check(f"scanner:{entry['id']}", entry['name'], 'fail',
                                 f'Not found at {where}. {hint}'))
    discovery = os.getenv('FOLIO_AIRSCAN_DISCOVERY', 'off').strip().lower() in ('on', 'true', 'yes', '1')
    results.append(check('discovery', 'Network scanner discovery', 'info',
                         'On: SANE also looks for eSCL/WSD scanners on the local network' if discovery else
                         'Off: only scanners added by address are used (FOLIO_AIRSCAN_DISCOVERY)'))
    return results


def check_ocr() -> dict:
    code, output = run_tool(['ocrmypdf', '--version'])
    if code is None:
        return check('ocr', 'Searchable PDF (OCR)', 'info', 'Not installed in this image; standard PDFs still work')
    return check('ocr', 'Searchable PDF (OCR)', 'ok' if code == 0 else 'warn',
                 f'OCRmyPDF {output.splitlines()[0]}' if code == 0 and output else 'OCRmyPDF did not respond')


def run_checks(*, data_dir: Path, jobs_dir: Path, cups_servers: list[str], registry: dict,
               request, cookie_secure: bool, version: str, default_printer: str | None) -> dict:
    sections = {
        'system': [check('version', 'Folio', 'info',
                         f'Version {version} on Python {platform.python_version()}, '
                         f'up {int((time.time() - STARTED) // 60)} min'),
                   *check_storage(data_dir, jobs_dir), check_memory(),
                   *check_proxy(request, cookie_secure)],
        'printing': check_printing(cups_servers, default_printer, data_dir),
        'scanning': [*check_scanning(registry), check_ocr()],
    }
    statuses = [c['status'] for group in sections.values() for c in group]
    overall = 'fail' if 'fail' in statuses else 'warn' if 'warn' in statuses else 'ok'
    return {'overall': overall, 'sections': sections, 'checked_at': time.time()}
