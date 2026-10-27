"""Announce the built-in print server's printers on the network (AirPrint/Bonjour).

The host's own Avahi does the announcing: Folio writes one service file per
shared printer into the host's /etc/avahi/services, mounted into the
container, and Avahi advertises it with the host's name and address. Phones
and computers then print to port 631 on the host, which must be published
to the built-in print server. Only that folder is shared with the host;
nothing else is (mounting the host's D-Bus would also work, but would give
the container far more power over the host than announcing needs).

Folio only touches the files it wrote (folio-*.service). The print server's
supervisor, which runs as root, does the writing (the host's folder belongs
to root) and records what it announced in a status file Folio reads.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

from backend import ipp

SERVICES_DIR = Path(os.environ.get('FOLIO_AVAHI_SERVICES', '/etc/avahi/services'))
STATUS_FILE = Path(os.environ.get('FOLIO_RUN_DIR', '/run/folio')) / 'airprint.json'
PREFIX = 'folio-'
ATTRIBUTES = ['printer-name', 'printer-info', 'printer-is-shared', 'printer-make-and-model', 'printer-location',
              'document-format-supported', 'urf-supported', 'color-supported', 'sides-supported', 'printer-uuid',
              'printer-type', 'printer-state']
# Formats announced, in the order AirPrint clients prefer; only those the queue accepts.
FORMATS = ['application/pdf', 'image/urf', 'image/pwg-raster', 'image/jpeg', 'image/png',
           'application/postscript', 'application/octet-stream']


def available() -> bool:
    """Whether the host's Avahi services folder is mounted (and writable)."""
    return SERVICES_DIR.is_dir() and os.access(SERVICES_DIR, os.W_OK)


def port() -> int:
    """The host port that reaches the built-in print server (FOLIO_AIRPRINT_PORT)."""
    try:
        value = int(os.environ.get('FOLIO_AIRPRINT_PORT', '631'))
    except ValueError:
        return 631
    return value if 1 <= value <= 65535 else 631


def txt(key: str, value: str) -> str:
    return f'    <txt-record>{escape(f"{key}={value}"[:255])}</txt-record>'


def service_file(printer: dict, ipp_port: int) -> str:
    """An Avahi service definition announcing one printer the way CUPS itself would."""
    first = lambda key, default='': (printer.get(key) or [default])[0]
    name = first('printer-name')
    formats = set(printer.get('document-format-supported') or [])
    pdl = ','.join(f for f in FORMATS if f in formats) or 'application/pdf'
    uuid = str(first('printer-uuid', '')).removeprefix('urn:uuid:')
    records = [txt('txtvers', '1'), txt('qtotal', '1'), txt('rp', f'printers/{name}'),
               txt('ty', first('printer-make-and-model', 'Printer')), txt('note', first('printer-location', '')),
               txt('product', f"({first('printer-make-and-model', 'Printer')})"), txt('priority', '0'),
               txt('pdl', pdl), txt('Color', 'T' if first('color-supported', False) else 'F'),
               txt('Duplex', 'T' if any(s != 'one-sided' for s in printer.get('sides-supported') or []) else 'F'),
               txt('Transparent', 'T'), txt('Binary', 'T'),
               txt('printer-state', str(first('printer-state', 3))),
               txt('printer-type', hex(int(first('printer-type', 0) or 0)))]
    if printer.get('urf-supported'):
        records.append(txt('URF', ','.join(printer['urf-supported'])))
    if uuid:
        records.append(txt('UUID', uuid))
    label = first('printer-info') or name
    return '\n'.join([
        '<?xml version="1.0" standalone="no"?>',
        '<!DOCTYPE service-group SYSTEM "avahi-service.dtd">',
        '<!-- Written by Folio for its built-in print server; it is replaced or removed automatically. -->',
        '<service-group>',
        f'  <name replace-wildcards="yes">{escape(label[:50])} @ %h</name>',
        '  <service>',
        '    <type>_ipp._tcp</type>',
        '    <subtype>_universal._sub._ipp._tcp</subtype>',
        f'    <port>{ipp_port}</port>',
        *records,
        '  </service>',
        '</service-group>', ''])


def _write(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding='utf-8') == content:
        return  # Unchanged: don't make Avahi re-announce
    fd, tmp = tempfile.mkstemp(prefix='.folio-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content)
        os.chmod(tmp, 0o644)  # Avahi reads it as its own user
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def withdraw() -> None:
    """Remove every announcement Folio made (the print server is stopping)."""
    if SERVICES_DIR.is_dir():
        for path in SERVICES_DIR.glob(f'{PREFIX}*.service'):
            path.unlink(missing_ok=True)


def write_status(*, announced: list[str], error: str | None = None) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps({'mounted': SERVICES_DIR.is_dir(), 'announced': announced, 'error': error,
                               'checked_at': time.time()}), encoding='utf-8')
    tmp.chmod(0o644)  # Folio reads it as an ordinary user
    os.replace(tmp, STATUS_FILE)


def status() -> dict:
    """What the print server last announced; empty when it isn't running."""
    try:
        return json.loads(STATUS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def sync(conn: ipp.Connection | None) -> list[str]:
    """Announce every shared printer on ``conn`` and withdraw the rest; returns
    the printers announced. ``None`` (built-in server off) withdraws all."""
    if not available():
        return []
    printers = []
    if conn is not None:
        try:
            response = conn.request(ipp.CUPS_GET_PRINTERS, [(ipp.KEYWORD, 'requested-attributes', ATTRIBUTES)],
                                    authenticate=False, timeout=10)
            printers = [p for p in response.all(ipp.PRINTER) if (p.get('printer-is-shared') or [False])[0]]
        except ipp.IPPError as exc:
            if exc.status != 0x0406:  # not-found: no printers yet, which is fine
                raise
    wanted = {}
    for printer in printers:
        name = (printer.get('printer-name') or [''])[0]
        if name and all(c.isalnum() or c in '_.-' for c in name):
            wanted[f'{PREFIX}{name}.service'] = service_file(printer, port())
    for path in SERVICES_DIR.glob(f'{PREFIX}*.service'):
        if path.name not in wanted:
            path.unlink(missing_ok=True)
    for filename, content in wanted.items():
        _write(SERVICES_DIR / filename, content)
    return sorted(name.removeprefix(PREFIX).removesuffix('.service') for name in wanted)
