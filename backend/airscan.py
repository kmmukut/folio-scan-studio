"""Share Folio's scanners with other computers as AirScan (eSCL) scanners.

Macs (Image Capture, Preview), Windows 10 and 11, Android (the Mopria Scan
app) and Linux (sane-airscan) scan from AirScan scanners without a driver.
AirSane (https://github.com/SimulPiscator/AirSane, GPL-3.0), built into the
image, does the AirScan side: it serves scanners that SANE can use over
HTTP, so a WSD-only scanner becomes an AirScan one too. Folio decides which
(each scanner has a "Share as AirScan" switch, off by default), runs AirSane
as its own user, and describes each shared scanner for the announcement.

Announcing works like AirPrint (see airprint.py): Folio writes what to
announce into data/airscan/announce.json, and the print server's supervisor,
which runs as root, turns it into service files in the host's Avahi folder.
Where that isn't possible (the folder isn't mounted, or the computers are
on another network), the Devices page shows the service file to add by hand.

AirScan has no sign-in: anyone on the network who can reach the port can
scan from a shared scanner. AirSane accepts only private addresses.

AirSane runs with --debug=true because its start-up log is the only place
that names each scanner's UUID, which is part of its address. The log
format is pinned along with AirSane's commit (see the Dockerfile) and
checked by a test inside the image.
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from backend import airprint, probe

log = logging.getLogger('folio.airscan')

LISTEN_PORT = 8090  # AirSane's port inside the container
SERVICE_PREFIX = 'folio-airscan@'  # '@' never appears in printer names, so AirPrint's files can't clash
SHAREABLE = ('managed', 'attached')  # Scanners Folio set up (not the read-only legacy configuration)
# AirScan has no sign-in, so only private and local addresses may scan.
ACCESS = ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16',
          '::1/128', 'fc00::/7', 'fe80::/10')
TXT_KEYS = ('txtvers', 'vers', 'pdl', 'ty', 'note', 'uuid', 'rs', 'cs', 'is', 'duplex')
DEVICE_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,40}')
RESTART_DELAYS = (2, 10, 30, 120)
CHECK_EVERY = 15
COLOR_SPACES = (('Grayscale', 'grayscale'), ('RGB', 'color'), ('BlackAndWhite', 'binary'))


def binary() -> str | None:
    return shutil.which('airsaned')


def port() -> int:
    """The host port that reaches AirSane (FOLIO_AIRSCAN_PORT), for the announcement."""
    try:
        value = int(os.environ.get('FOLIO_AIRSCAN_PORT', '8090'))
    except ValueError:
        return 8090
    return value if 1 <= value <= 65535 else 8090


def folder(data_dir: Path) -> Path:
    return Path(data_dir) / 'airscan'


def shared(cfg: dict) -> list[dict]:
    return [e for e in cfg.get('scanners', []) if e.get('airscan') and e.get('enabled', True)
            and (e.get('kind') == 'managed' or (e.get('kind') == 'attached' and e.get('hint')))]


# --- AirSane's configuration ---------------------------------------------------------

def sane_pattern(entry: dict) -> str:
    """The scanner's SANE name, as a regular expression (ECMAScript, AirSane's flavour)."""
    if entry['kind'] == 'managed':  # Folio names them in the sane-airscan configuration (devices.py)
        return f'airscan:[a-z]+[0-9]+:Folio-{entry["id"]} .*'
    return re.sub(r'([\\^$.*+?()\[\]{}|/])', r'\\\1', entry['hint'])  # Attached: the SANE name itself


def ignore_rule(entries: list[dict]) -> str:
    """One line for AirSane's ignore list: every device except the shared ones."""
    return '(?!(?:' + '|'.join(sane_pattern(e) for e in entries) + ')$).*'


def _write(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding='utf-8') == content:
        return
    fd, tmp = tempfile.mkstemp(prefix='.folio-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_config(data_dir: Path, entries: list[dict], sane_conf: Path) -> str:
    """Write AirSane's files; returns a fingerprint that changes when AirSane must reload."""
    base = folder(data_dir)
    base.mkdir(mode=0o700, exist_ok=True)
    files = {
        'ignore.conf': '# Written by Folio: AirSane shares only the scanners switched on in Devices.\n'
                       + ignore_rule(entries) + '\n',
        'access.conf': '# Written by Folio: AirScan has no sign-in, so only private addresses may scan.\n'
                       + ''.join(f'allow {network}\n' for network in ACCESS),
        'options.conf': '# Written by Folio.\n',
    }
    for name, content in files.items():
        _write(base / name, content)
    try:
        scanners = sane_conf.read_text(encoding='utf-8')  # A changed endpoint or name needs a reload too
    except OSError:
        scanners = ''
    return hashlib.sha256(json.dumps([files, scanners]).encode()).hexdigest()


def command(data_dir: Path) -> list[str]:
    base = folder(data_dir)
    return [binary() or 'airsaned', f'--listen-port={LISTEN_PORT}', '--mdns-announce=false',
            '--web-interface=false', '--disclose-version=false', '--reset-option=false',
            '--hotplug=false', '--network-hotplug=false', '--compatible-path=true', '--random-paths=false',
            '--local-scanners-only=false', '--debug=true', f'--ignore-list={base / "ignore.conf"}',
            f'--access-file={base / "access.conf"}', f'--options-file={base / "options.conf"}']


class StartupLog:
    """Reads AirSane's log: which devices it shares, under which UUID."""

    def __init__(self):
        self.lines = collections.deque(maxlen=60)
        self.devices: dict[str, str] = {}  # "<SANE name> (<make and model>)" -> UUID, once listening
        self.generation = 0                # Counts each time AirSane is ready
        self._pending: dict[str, str] | None = {}  # None between start-ups
        self._current: str | None = None

    def feed(self, line: str) -> bool:
        """Take one line; True when AirSane has just become ready."""
        line = line.rstrip('\n')
        self.lines.append(line)
        if line.startswith('start time is'):
            self._pending, self._current = {}, None
        elif self._pending is None:
            return False  # Lines about scans, not start-up
        elif line.startswith('found: '):
            self._current = line[len('found: '):]
        elif line.startswith(('ignoring ', 'error: ')):
            if self._current:
                self._pending.pop(self._current, None)
            self._current = None
        elif line.startswith('uuid: ') and self._current:
            self._pending[self._current] = line[len('uuid: '):].strip()
        elif line.startswith('listening on'):
            self.devices, self._pending = self._pending, None
            self.generation += 1
            return True
        return False

    def uuid_for(self, entry: dict) -> str | None:
        """The UUID AirSane gave this scanner."""
        for found, uuid in self.devices.items():
            if entry['kind'] == 'managed':
                if re.match(rf'airscan:[a-z]+[0-9]+:Folio-{re.escape(entry["id"])} ', found):
                    return uuid
            elif found.startswith(entry['hint'] + ' ('):
                return uuid
        return None


# --- What to announce ------------------------------------------------------------------

def capabilities(path: str) -> dict | None:
    """What AirSane serves at this path (only asked on this computer)."""
    url = f'http://127.0.0.1:{LISTEN_PORT}{path}/ScannerCapabilities'
    try:  # Straight to AirSane: no proxy, no redirects
        with probe._OPENER.open(urllib.request.Request(url), timeout=10) as answer:
            root = probe._xml(answer.read(probe.MAX_ANSWER))
    except OSError:
        return None
    if root is None:
        return None
    formats = list(dict.fromkeys(e.text.strip() for e in probe._all(root, 'DocumentFormat') if e.text))
    modes = {e.text.strip() for e in probe._all(root, 'ColorMode') if e.text}
    return {'uuid': probe._text(root, 'UUID'), 'make_model': probe._text(root, 'MakeAndModel'), 'formats': formats,
            'color_spaces': [name for prefix, name in COLOR_SPACES if any(m.startswith(prefix) for m in modes)],
            'platen': bool(probe._all(root, 'Platen')), 'adf': bool(probe._all(root, 'Adf')),
            'duplex': bool(probe._all(root, 'AdfDuplexInputCaps'))}


def txt_records(name: str, path: str, caps: dict) -> dict:
    """The announcement's details, as AirSane itself would announce them."""
    return {'txtvers': '1', 'vers': '2.0', 'pdl': ','.join(caps['formats']), 'ty': name, 'note': 'Folio',
            'uuid': caps['uuid'], 'rs': path.lstrip('/'), 'cs': ','.join(caps['color_spaces']),
            'is': ','.join(source for source in ('platen', 'adf') if caps[source]),
            'duplex': 'T' if caps['duplex'] else 'F'}


def clean(value: object, limit: int) -> str:
    return ''.join(c for c in str(value) if c.isprintable())[:limit]


def valid_announcement(item: object) -> dict | None:
    """One announcement from Folio's file, or None. The supervisor (root) trusts nothing in it."""
    if not isinstance(item, dict) or not isinstance(item.get('txt'), dict):
        return None
    if not isinstance(item.get('id'), str) or not DEVICE_ID.fullmatch(item['id']):
        return None
    name = clean(item.get('name', ''), 50).strip()
    txt = {key: clean(value, 200) for key, value in item['txt'].items() if key in TXT_KEYS and isinstance(value, str)}
    if not name or not txt.get('rs') or not txt.get('uuid'):
        return None
    return {'id': item['id'], 'name': name, 'txt': txt}


def service_file(item: dict, airscan_port: int, note: str) -> str:
    """An Avahi service definition announcing one shared scanner."""
    records = [f'    <txt-record>{escape(f"{key}={item["txt"][key]}"[:255])}</txt-record>'
               for key in TXT_KEYS if key in item['txt']]
    return '\n'.join([
        '<?xml version="1.0" standalone="no"?>',
        '<!DOCTYPE service-group SYSTEM "avahi-service.dtd">',
        f'<!-- {escape(note)} -->',
        '<service-group>',
        f'  <name replace-wildcards="yes">{escape(item["name"])} @ %h</name>',
        '  <service>',
        '    <type>_uscan._tcp</type>',
        f'    <port>{int(airscan_port)}</port>',
        *records,
        '  </service>',
        '</service-group>', ''])


AUTOMATIC = 'Written by Folio for a scanner shared as AirScan; it is replaced or removed automatically.'
BY_HAND = ('Folio: a scanner shared as AirScan. Put this file in /etc/avahi/services on a computer that '
           'runs Avahi on the network of the Macs and phones, and forward the port to Folio if needed.')


# --- In the print server's supervisor (root) ---------------------------------------------

def read_announcements(data_dir: Path) -> list[dict]:
    path = folder(data_dir) / 'announce.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return []
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return []
        data = handle.read(65537)
    if len(data) > 65536:
        return []
    try:
        items = json.loads(data)
    except ValueError:
        return []
    found = [valid_announcement(item) for item in items[:32]] if isinstance(items, list) else []
    return [item for item in found if item]


def sync_announcements(data_dir: Path) -> list[str]:
    """Turn Folio's announce.json into Avahi service files; returns the scanners announced."""
    if not airprint.available():
        return []
    wanted = {f'{SERVICE_PREFIX}{item["id"]}.service': service_file(item, port(), AUTOMATIC)
              for item in read_announcements(data_dir)}
    for path in airprint.SERVICES_DIR.glob(f'{SERVICE_PREFIX}*.service'):
        if path.name not in wanted:
            path.unlink(missing_ok=True)
    for filename, content in wanted.items():
        airprint._write(airprint.SERVICES_DIR / filename, content)
    return sorted(name.removeprefix(SERVICE_PREFIX).removesuffix('.service') for name in wanted)


def status_file() -> Path:
    return airprint.STATUS_FILE.with_name('airscan.json')  # Beside AirPrint's


def write_status(*, announced: list[str], error: str | None = None) -> None:
    path = status_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'mounted': airprint.SERVICES_DIR.is_dir(), 'announced': announced, 'error': error,
                               'checked_at': time.time()}), encoding='utf-8')
    tmp.chmod(0o644)  # Folio reads it as an ordinary user
    os.replace(tmp, path)


def announcement_status() -> dict:
    try:
        return json.loads(status_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


# --- In Folio: keeping AirSane running ------------------------------------------------------

class Sharing:
    """Runs AirSane while at least one scanner is shared, reloads it when that changes,
    and writes the announcements once it is ready."""

    def __init__(self):
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._log = StartupLog()
        self._fingerprint: str | None = None
        self._announced_generation = -1
        self._failures = 0
        self._next_start = 0.0
        self._context: Callable[[], tuple[Path, dict, Path]] | None = None
        self.state: dict = {'running': False, 'ready': False, 'error': None, 'scanners': []}

    def start(self, context: Callable[[], tuple[Path, dict, Path]]) -> None:
        """``context`` returns (data folder, scanner registry, sane-airscan configuration file)."""
        with self._lock:
            self._context = context
            if self._thread and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True, name='folio-airscan')
            self._thread.start()

    def poke(self) -> None:
        """Something changed (a scanner was shared, renamed, moved): look again now."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(10)
        self._terminate()

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001 - sharing must never take Folio down
                log.warning('AirScan sharing failed', exc_info=True)
            self._wake.wait(CHECK_EVERY)
            self._wake.clear()

    def reconcile(self) -> None:
        if not self._context:
            return
        data_dir, cfg, sane_conf = self._context()
        entries = shared(cfg)
        if not entries or not binary():
            self._terminate()
            self._failures = 0
            self._publish(data_dir, [])
            self.state = {'running': False, 'ready': False, 'error': None, 'scanners': []}
            return
        fingerprint = write_config(data_dir, entries, sane_conf)
        process = self._process
        if process is None or process.poll() is not None:
            if process is not None:
                self._failures += 1
                self.state['error'] = 'AirSane stopped: ' + ' / '.join(list(self._log.lines)[-3:])[:300]
                self._process = None
            if time.monotonic() < self._next_start:
                return
            self._launch(data_dir)
        elif fingerprint != self._fingerprint:
            self._log = StartupLog()  # Reloading: its UUIDs are read again
            self._announced_generation = -1
            process.send_signal(signal.SIGHUP)
            self.state['ready'] = False
        self._fingerprint = fingerprint
        if self._log.generation != self._announced_generation and self._log.generation:
            self._announce(data_dir, entries)

    def _launch(self, data_dir: Path) -> None:
        self._log = StartupLog()
        self._announced_generation = -1
        self._process = subprocess.Popen(command(data_dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, text=True, errors='replace')
        self._next_start = time.monotonic() + RESTART_DELAYS[min(self._failures, len(RESTART_DELAYS) - 1)]
        threading.Thread(target=self._read, args=(self._process, self._log), daemon=True,
                         name='folio-airscan-log').start()
        self.state.update(running=True, ready=False)

    def _read(self, process: subprocess.Popen, startup: StartupLog) -> None:
        for line in process.stderr:
            if startup is not self._log and line.startswith('start time is'):
                startup = self._log  # Reloaded: the keeper started a fresh log
            if startup.feed(line):
                self._wake.set()

    def _announce(self, data_dir: Path, entries: list[dict]) -> None:
        generation = self._log.generation
        first = capabilities('/eSCL')
        scanners, items = [], []
        for entry in entries:
            uuid = self._log.uuid_for(entry)
            path = None
            if uuid:
                path = '/eSCL' if first and first['uuid'] == uuid else f'/{uuid}'
            caps = capabilities(path) if path else None
            if caps and caps['uuid'] != uuid:
                caps = None
            name = clean(entry.get('name', ''), 50).strip() or 'Scanner'
            if caps:
                items.append({'id': entry['id'], 'name': name, 'txt': txt_records(name, path, caps)})
            scanners.append({'id': entry['id'], 'name': name, 'path': path if caps else None,
                             'uuid': uuid if caps else None, 'problem': None if caps else
                             'AirSane couldn’t open this scanner. Check that it’s switched on, then press Refresh devices.'})
        self._publish(data_dir, items)
        self._failures = 0
        self._announced_generation = generation
        self.state = {'running': True, 'ready': True, 'error': None, 'scanners': scanners, 'items': items}

    def _publish(self, data_dir: Path, items: list[dict]) -> None:
        base = folder(data_dir)
        if not items and not (base / 'announce.json').exists():
            return
        base.mkdir(mode=0o700, exist_ok=True)
        # Readable by the supervisor (root) anyway; no secrets in it.
        _write(base / 'announce.json', json.dumps(items, indent=1))

    def _terminate(self) -> None:
        process, self._process = self._process, None
        self._fingerprint = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)

    def status(self) -> dict:
        """For the Devices page: what is shared, and how to announce it by hand."""
        announced = announcement_status()
        recent = time.time() - announced.get('checked_at', 0) < 120
        items = {item['id']: item for item in self.state.get('items', [])}
        scanners = []
        for scanner in self.state.get('scanners', []):
            item = items.get(scanner['id'])
            scanners.append({**scanner, 'announced': recent and scanner['id'] in announced.get('announced', []),
                             'service_file': service_file(item, port(), BY_HAND) if item else None})
        return {'available': bool(binary()), 'running': self.state.get('running', False),
                'ready': self.state.get('ready', False), 'error': self.state.get('error'), 'port': port(),
                'announcing': bool(recent and announced.get('mounted')), 'scanners': scanners}


sharing = Sharing()
