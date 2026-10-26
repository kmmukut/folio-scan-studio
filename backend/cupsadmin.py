"""Printer administration and print-job management on CUPS servers.

Folio uses a CUPS administrator login, entered once per server in Devices,
to add, remove and pause printer queues. Listing and cancelling one's own
print jobs needs no login: CUPS identifies the owner by the user name Folio
submits jobs under. Stored logins never leave the Folio server.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from backend import devices, ipp

PRINTER_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')  # Same rule as devices.QUEUENAME
PPD_NAME = re.compile(r'^[\w.:/+@()~ -]{1,240}$')
HOST = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*$')
URI_PATH = re.compile(r'^[A-Za-z0-9._~/%-]{0,120}$')
# Manual connections Folio can build from an address; discovered devices may
# use any scheme CUPS itself reported.
MANUAL_CONNECTIONS = {
    'ipp': ('ipp://{address}{port}{path}', '/ipp/print'),
    'socket': ('socket://{address}{port}', ''),
    'lpd': ('lpd://{address}{port}{path}', '/queue'),
}
DRIVERLESS = 'everywhere'
JOB_ATTRIBUTES = ['job-id', 'job-name', 'job-originating-user-name', 'job-state', 'job-state-reasons',
                  'job-printer-state-message', 'time-at-creation', 'time-at-completed', 'job-printer-uri',
                  'job-media-sheets-completed']
ACTIVE_JOB_STATES = {3, 4, 5, 6}
GUEST_USER = 'folio-guest'
DISCOVERY_TTL = 10 * 60
_store_lock = threading.Lock()
_discovered: dict[str, tuple[float, set[str]]] = {}


class NotConnected(ipp.IPPError):
    """No administrator login is stored for this CUPS server."""


# --- Stored administrator logins ------------------------------------------------

def store_path(data_dir: Path) -> Path:
    return Path(data_dir) / 'cups-admin.json'


def _load(data_dir: Path) -> dict:
    try:
        return json.loads(store_path(data_dir).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save(data_dir: Path, logins: dict) -> None:
    path = store_path(data_dir)
    fd, tmp = tempfile.mkstemp(prefix='.cups-admin-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)  # Contains passwords: owner-only, like .env
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(logins, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def admin_status(data_dir: Path, server: str) -> dict:
    login = _load(data_dir).get(server)
    return ({'connected': True, 'username': login['username'], 'encrypted': bool(login.get('tls'))}
            if login else {'connected': False, 'username': None, 'encrypted': False})


def connection(data_dir: Path, server: str, *, admin: bool = False, timeout: float = 15) -> ipp.Connection:
    """A connection to ``server``; with ``admin`` it carries the stored login."""
    login = _load(data_dir).get(server)
    if admin and not login:
        raise NotConnected(f'Connect an administrator login for {server} in Devices first')
    if not login:
        return ipp.Connection(server, timeout=timeout)
    return ipp.Connection(server, login['username'] if admin else None, login['password'] if admin else None,
                          tls=bool(login.get('tls')), pin=login.get('pin'), timeout=timeout)


def connect_admin(data_dir: Path, server: str, username: str, password: str) -> dict:
    """Check the login with a harmless administrator-only request, then store it.

    Servers that only accept passwords over TLS are switched to TLS, and the
    certificate seen now is the one trusted from then on.
    """
    for tls in (False, True):
        conn = ipp.Connection(server, username, password, tls=tls, timeout=15)
        try:
            # Asks CUPS to list USB devices for at most one second: needs admin rights, changes nothing.
            conn.request(ipp.CUPS_GET_DEVICES, [(ipp.INTEGER, 'timeout', 1), (ipp.NAME, 'include-schemes', ['usb'])],
                         resource='/admin/')
        except ipp.EncryptionRequired:
            continue
        with _store_lock:
            logins = _load(data_dir)
            logins[server] = {'username': username, 'password': password, 'tls': tls, 'pin': conn.pin}
            _save(data_dir, logins)
        return admin_status(data_dir, server)
    raise ipp.IPPError(f'CUPS server {server} asked for encryption but the TLS connection failed')


def disconnect_admin(data_dir: Path, server: str) -> None:
    with _store_lock:
        logins = _load(data_dir)
        if logins.pop(server, None) is not None:
            _save(data_dir, logins)


# --- Finding devices and drivers -----------------------------------------------

def connection_kind(uri: str, device_class: str) -> str:
    scheme = uri.split(':', 1)[0].lower()
    if scheme == 'usb' or (scheme in ('ipp', 'ipps') and ('localhost' in uri or '127.0.0.1' in uri)):
        return 'USB'  # Includes IPP-over-USB, which ipp-usb exposes on localhost
    if device_class == 'network' or scheme in ('ipp', 'ipps', 'socket', 'lpd', 'dnssd', 'http', 'https'):
        return 'Network'
    if device_class == 'file':
        return 'Virtual'
    return 'Other'


def discover(conn: ipp.Connection, timeout: int = 8) -> list[dict]:
    """Printers CUPS can see right now: USB, and network printers it can find.

    CUPS also lists bare schemes ("socket", "lpd") as manual-entry templates;
    those are dropped here because Folio offers its own manual entry.
    """
    response = conn.request(ipp.CUPS_GET_DEVICES, [(ipp.INTEGER, 'timeout', timeout)],
                            resource='/admin/', timeout=timeout + 20)
    found = []
    for device in response.all(ipp.PRINTER):
        uri = (device.get('device-uri') or [''])[0]
        if ':' not in uri:
            continue
        device_class = (device.get('device-class') or [''])[0]
        make_model = (device.get('device-make-and-model') or [''])[0]
        found.append({'uri': uri, 'connection': connection_kind(uri, device_class),
                      'name': (device.get('device-info') or [''])[0] or make_model or uri,
                      'make_model': '' if make_model == 'Unknown' else make_model,
                      'device_id': (device.get('device-id') or [''])[0],
                      'driverless': uri.split(':', 1)[0] in ('ipp', 'ipps', 'dnssd', 'driverless')})
    _discovered[conn.server] = (time.monotonic(), {d['uri'] for d in found})
    return found


def was_discovered(server: str, uri: str) -> bool:
    stamp, uris = _discovered.get(server, (0.0, set()))
    return time.monotonic() - stamp < DISCOVERY_TTL and uri in uris


def manual_uri(kind: str, address: str, port: int | None = None, path: str | None = None) -> str:
    """Build a device URI from an address instead of trusting a typed URI."""
    if kind not in MANUAL_CONNECTIONS:
        raise ValueError('Choose IPP, JetDirect or LPD')
    address = address.strip()
    try:
        ip = ipaddress.ip_address(address)
        address = f'[{ip}]' if ip.version == 6 else str(ip)
    except ValueError:
        if not HOST.fullmatch(address) or len(address) > 253:
            raise ValueError('Enter the printer’s IP address or host name') from None
    template, default_path = MANUAL_CONNECTIONS[kind]
    path = (path or default_path).strip()
    if path and not path.startswith('/'):
        path = '/' + path
    if not URI_PATH.fullmatch(path) or '..' in path:
        raise ValueError('The queue path may only contain letters, numbers and / . _ - ~')
    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    port_text = f':{int(port)}' if port else (':9100' if kind == 'socket' else '')
    return template.format(address=address, port=port_text, path=path)


def makes(conn: ipp.Connection) -> list[str]:
    """Manufacturers that have drivers on this server (one CUPS-Get-PPDs call)."""
    def fetch():
        response = conn.request(ipp.CUPS_GET_PPDS, [(ipp.KEYWORD, 'requested-attributes', ['ppd-make'])],
                                authenticate=False, timeout=60)
        return sorted({p['ppd-make'][0] for p in response.all(ipp.PRINTER) if p.get('ppd-make')}, key=str.lower)
    return devices.cached(('cups', 'makes', conn.server), devices.CAPABILITY_TTL, fetch)


def models(conn: ipp.Connection, make: str) -> list[dict]:
    def fetch():
        response = conn.request(ipp.CUPS_GET_PPDS, [
            (ipp.TEXT, 'ppd-make', make),
            (ipp.KEYWORD, 'requested-attributes', ['ppd-name', 'ppd-make-and-model', 'ppd-natural-language'])],
            authenticate=False, timeout=60)
        return [{'ppd': p['ppd-name'][0], 'name': p['ppd-make-and-model'][0],
                 'language': (p.get('ppd-natural-language') or ['en'])[0]}
                for p in response.all(ipp.PRINTER) if p.get('ppd-name') and p.get('ppd-make-and-model')]
    try:
        return devices.cached(('cups', 'models', conn.server, make.lower()), devices.CAPABILITY_TTL, fetch)
    except ipp.IPPError as exc:
        if exc.status == 0x0406:  # not-found: no drivers for this make
            return []
        raise


def _tokens(text: str) -> list[str]:
    return re.findall(r'[a-z0-9]+', text.lower())


def rank_drivers(candidates: list[dict], query: str, limit: int = 40) -> list[dict]:
    """Order drivers by how well they match a model name such as "M2885FW".

    Driver families are often named with an x ("M288x"), so "m2885fw" also
    matches "m288x". English drivers and ones marked "(recommended)" come first
    among equals.
    """
    wanted = [t for t in _tokens(query) if t not in ('series', 'printer')]

    def token_score(token: str, offered: list[str]) -> int:
        best = 0
        for item in offered:
            if item == token:
                return 4
            family = item.rstrip('x')
            if item.endswith('x') and len(family) >= 3 and token.startswith(family):
                best = max(best, 3)
            elif len(token) >= 4 and (item.startswith(token) or token.startswith(item)) and len(item) >= 4:
                best = max(best, 2)
        return best

    # Model numbers ("m2885fw") identify a driver; the maker's name matches them all.
    model_words = [t for t in wanted if any(c.isdigit() for c in t)]
    scored = []
    for driver in candidates:
        offered = _tokens(driver['name'])
        score = sum(token_score(t, offered) for t in wanted)
        if model_words and not any(token_score(t, offered) for t in model_words):
            continue
        if wanted and score == 0:
            continue
        bonus = (2 if 'recommended' in driver['name'].lower() else 0) + (1 if driver['language'] == 'en' else 0)
        scored.append((score, bonus, driver['name'].lower(), driver))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in scored[:limit]]


# --- Queues --------------------------------------------------------------------

def add_printer(conn: ipp.Connection, name: str, device_uri: str, ppd: str,
                info: str = '', location: str = '') -> None:
    """Create (or replace) a queue that accepts jobs at once.

    Queues are always shared: CUPS refuses jobs from other computers for
    unshared queues, and Folio is such a computer.
    """
    if not PRINTER_NAME.fullmatch(name):
        raise ValueError('Printer names use letters, numbers, dot, dash and underscore (no spaces)')
    if ppd != DRIVERLESS and not PPD_NAME.fullmatch(ppd):
        raise ValueError('Unknown driver')
    conn.request(ipp.CUPS_ADD_MODIFY_PRINTER, [(ipp.URI, 'printer-uri', conn.printer_uri(name)),
                                               (ipp.NAME, 'ppd-name', ppd)],
                 resource='/admin/', timeout=120, printer_attributes=[
                     (ipp.URI, 'device-uri', device_uri),
                     (ipp.TEXT, 'printer-info', info[:127] or name),
                     (ipp.TEXT, 'printer-location', location[:127]),
                     (ipp.BOOLEAN, 'printer-is-shared', True),
                     (ipp.BOOLEAN, 'printer-is-accepting-jobs', True),
                     (ipp.ENUM, 'printer-state', 3)])
    devices.invalidate('cups')


def delete_printer(conn: ipp.Connection, name: str) -> None:
    conn.request(ipp.CUPS_DELETE_PRINTER, [(ipp.URI, 'printer-uri', conn.printer_uri(name))], resource='/admin/')
    devices.invalidate('cups')


def set_paused(conn: ipp.Connection, name: str, paused: bool) -> None:
    conn.request(ipp.PAUSE_PRINTER if paused else ipp.RESUME_PRINTER,
                 [(ipp.URI, 'printer-uri', conn.printer_uri(name))], resource='/admin/')
    devices.invalidate('cups')


def printer_state(conn: ipp.Connection, name: str) -> dict:
    response = conn.request(ipp.GET_PRINTER_ATTRIBUTES, [
        (ipp.URI, 'printer-uri', conn.printer_uri(name)),
        (ipp.KEYWORD, 'requested-attributes', ['printer-state', 'printer-state-message', 'printer-state-reasons',
                                               'printer-is-accepting-jobs', 'queued-job-count',
                                               'printer-make-and-model', 'device-uri'])], authenticate=False)
    attrs = response.group(ipp.PRINTER)
    first = lambda key, default=None: (attrs.get(key) or [default])[0]
    reasons = [r for r in attrs.get('printer-state-reasons', []) if r and r != 'none']
    return {'state': ipp.PRINTER_STATES.get(first('printer-state'), 'unknown'),
            'paused': first('printer-state') == 5 and 'paused' in reasons,
            'message': first('printer-state-message', '') or '', 'reasons': reasons,
            'accepting': bool(first('printer-is-accepting-jobs', True)),
            'queued': first('queued-job-count', 0) or 0, 'driver': first('printer-make-and-model', '') or '',
            'device_uri': first('device-uri', '') or ''}


# --- Print jobs ----------------------------------------------------------------

JOB_STATE_LABELS = {'pending': 'Waiting', 'held': 'On hold', 'processing': 'Printing', 'stopped': 'Stopped',
                    'cancelled': 'Cancelled', 'aborted': 'Failed', 'completed': 'Done'}


def _job(server: str, attrs: dict[str, list]) -> dict:
    first = lambda key, default=None: (attrs.get(key) or [default])[0]
    state = ipp.JOB_STATES.get(first('job-state'), 'unknown')
    printer_uri = first('job-printer-uri', '') or ''
    queue = printer_uri.rsplit('/printers/', 1)[-1] if '/printers/' in printer_uri else ''
    return {'server': server, 'id': first('job-id'), 'queue': queue,
            'printer': devices.printer_id(server, queue) if queue and devices.QUEUENAME.fullmatch(queue) else None,
            'title': first('job-name', '') or '', 'user': first('job-originating-user-name', '') or '',
            'state': state, 'label': JOB_STATE_LABELS.get(state, state),
            'active': first('job-state') in ACTIVE_JOB_STATES,
            'reasons': [r for r in attrs.get('job-state-reasons', []) if r and r != 'none'],
            'message': first('job-printer-state-message', '') or '',
            'created': first('time-at-creation'), 'completed': first('time-at-completed'),
            'sheets': first('job-media-sheets-completed')}


def list_jobs(conn: ipp.Connection, *, user: str | None = None, queue: str | None = None,
              which: str = 'not-completed', limit: int = 50) -> list[dict]:
    """Jobs on one server, newest first. With ``user``, only that user's jobs
    (CUPS shows a job's title and owner only to its owner or an admin)."""
    attributes = [(ipp.URI, 'printer-uri', conn.printer_uri(queue)), (ipp.KEYWORD, 'which-jobs', which),
                  (ipp.KEYWORD, 'requested-attributes', JOB_ATTRIBUTES)]
    if user:
        attributes += [(ipp.NAME, 'requesting-user-name', user), (ipp.BOOLEAN, 'my-jobs', True)]
    response = conn.request(ipp.GET_JOBS, attributes, authenticate=bool(conn.username), timeout=10)
    jobs = [_job(conn.server, attrs) for attrs in response.all(ipp.JOB)]
    jobs.sort(key=lambda job: job['id'] or 0, reverse=True)
    return jobs[:limit]


def cancel_job(conn: ipp.Connection, job_id: int, as_user: str) -> None:
    conn.request(ipp.CANCEL_JOB, [(ipp.URI, 'job-uri', conn.job_uri(job_id)),
                                  (ipp.NAME, 'requesting-user-name', as_user)],
                 resource='/jobs/', authenticate=bool(conn.username))


def job_owner(conn: ipp.Connection, job_id: int, as_user: str) -> str | None:
    response = conn.request(ipp.GET_JOB_ATTRIBUTES, [
        (ipp.URI, 'job-uri', conn.job_uri(job_id)), (ipp.NAME, 'requesting-user-name', as_user),
        (ipp.KEYWORD, 'requested-attributes', ['job-originating-user-name'])], authenticate=bool(conn.username))
    return (response.group(ipp.JOB).get('job-originating-user-name') or [None])[0]


def suggested_name(make_model: str, fallback: str = 'Printer') -> str:
    words = re.findall(r'[A-Za-z0-9]+', make_model)[:4]
    name = '_'.join(words) or fallback
    return name[:60] if PRINTER_NAME.fullmatch(name[:60]) else fallback
