"""Hardware reports: how a printer or scanner works with Folio, for its compatibility list.

docs/HARDWARE.md lists the devices people have tried, including the ones that
didn't work. Folio writes a report from what the device and the print server
answer right now, which says more than people would type. The administrator
reads it, may change anything, and submits it on GitHub; Folio sends nothing.

Reports are public, so they leave out what identifies a network or a person:
the names given to printers and scanners in Folio, addresses, host names,
serial numbers, UUIDs, and Folio's scanner IDs (made from addresses). Device
addresses are described (connection, port, path), never copied. Text from
elsewhere (messages, models) is scrubbed of anything that looks like one of
those, and the finished report is scrubbed once more.
"""
from __future__ import annotations

import ipaddress
import platform
import re
import urllib.parse

from backend import features
from backend.version import __version__

# The answers offered by .github/ISSUE_TEMPLATE/hardware_report.yml
KINDS = {'printer': 'Printer', 'scanner': 'Scanner', 'both': 'Multifunction (print and scan)'}
NETWORK, USB, IPP_USB = 'Network (Ethernet or Wi-Fi)', 'USB', 'IPP-over-USB'
CONNECTIONS = {'ipp': 'IPP', 'ipps': 'IPP over TLS', 'http': 'IPP', 'https': 'IPP over TLS',
               'socket': 'JetDirect', 'lpd': 'LPD', 'usb': 'USB', 'ippusb': 'IPP-over-USB',
               'dnssd': 'IPP, found through Bonjour', 'smb': 'Windows printer sharing (SMB)'}
NETWORK_SCHEMES = ('ipp', 'ipps', 'http', 'https', 'socket', 'lpd')

_SCRUB = (
    (re.compile(r'(?<=//)[^/@\s]+@'), '<login>@'),  # ipp://user:password@host
    (re.compile(r'(?<![\w:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![\w:-])'), '<MAC address>'),
    (re.compile(r'\b[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\b'), '<UUID>'),
    (re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])'), '<address>'),
    (re.compile(r'\[?(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%[\w.-]+)?(?![\w:])\]?'), '<address>'),
    (re.compile(r'\b(serial|uuid)=[^&\s;,)]+', re.I), r'\1=<removed>'),
    (re.compile(r'\b(S/?N|SERN|serial(?:[ -]?number)?)(\s*[:=#]\s*|\s+)(?=[\w-]*\d)[A-Za-z0-9][\w-]{3,}', re.I),
     r'\1\2<serial>'),
    (re.compile(r'\b[\w-]+(?:\.[\w-]+)*\.(?:local|lan|home|internal|localdomain|home\.arpa|fritz\.box)\b', re.I),
     '<host>'),
    (re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+'), '<email>'),
    (re.compile(r'\b(?:net|sane)-[0-9a-f]{12}\b'), '<scanner ID>'),
)


def scrub(text: object, *, hosts: tuple | list = (), names: tuple | list = ()) -> str:
    """Text without anything that identifies a network, a device or a person.
    ``hosts`` (addresses, host names) and ``names`` (given in Folio) are taken
    out where they appear as whole words; then everything that looks like one."""
    text = ' '.join(str(text or '').split())
    for value, placeholder in [*((h, '<host>') for h in hosts), *((n, '<name>') for n in names)]:
        value = str(value or '').strip().strip('[]')
        if len(value) >= 3 and value.lower() != 'localhost' and not _is_address(value):  # Addresses: below
            text = re.sub(rf'(?<![\w.-]){re.escape(value)}(?![\w-])', placeholder, text, flags=re.I)
    for pattern, replacement in _SCRUB:
        text = pattern.sub(replacement, text)
    return text


def _is_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value.split('%')[0])
        return True
    except ValueError:
        return False


def uri_host(uri: str | None) -> str | None:
    """The host a device URI points at, if Folio can ask it over the network."""
    try:
        parts = urllib.parse.urlsplit(uri or '')
        return parts.hostname if parts.scheme in NETWORK_SCHEMES else None
    except ValueError:
        return None


def describe_uri(uri: str | None) -> tuple[str, str]:
    """How a device URI connects, without the address: (description, form's connection answer)."""
    try:
        parts = urllib.parse.urlsplit(uri or '')
        port = parts.port
    except ValueError:
        return 'unknown', ''
    scheme = parts.scheme.lower()
    label = CONNECTIONS.get(scheme, scheme or 'unknown')
    if scheme == 'usb':
        model = ' '.join(urllib.parse.unquote(p) for p in (parts.netloc, parts.path.strip('/')) if p)
        return f'USB ({scrub(model)})' if model else 'USB', USB
    if scheme == 'ippusb':
        return label, IPP_USB
    if scheme in ('ipp', 'ipps', 'http', 'https'):
        return f'{label}, port {port or 631}, {scrub(parts.path) or "/"}', NETWORK
    if scheme == 'socket':
        return f'{label}, port {port or 9100}', NETWORK
    if scheme in ('lpd', 'dnssd', 'smb'):
        return label, NETWORK  # An LPD queue's or Bonjour service's name may be personal
    return label, ''


PROCESSORS = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}  # As Docker names them


def about_folio() -> str:
    machine = platform.machine()
    return f'{__version__}, {features.edition().capitalize()} edition, {PROCESSORS.get(machine.lower(), machine or "unknown")}'


def formats_verdict(formats: list[str]) -> str:
    if 'application/pdf' in formats or 'image/pwg-raster' in formats:
        return 'accepts PDF or PWG raster, so driverless printing (IPP Everywhere) should work'
    if 'image/urf' in formats:
        return 'no PDF or PWG raster, only AirPrint raster: driverless printing may garble pages'
    return 'no PDF or PWG raster: driverless printing won’t work, a driver is needed'


def offers_lines(found: dict | None) -> list[str]:
    """What the address check found, in a few lines (probe.check's answer)."""
    if not found:
        return ['Folio couldn’t reach the address.']
    lines = []
    printing, ipp = found.get('printing') or {}, (found.get('printing') or {}).get('ipp')
    if ipp:
        tls = ' (encrypted connections only)' if ipp.get('encrypted_only') else ''
        lines.append(f'IPP at port {ipp["port"]}, {ipp["path"]}{tls}: {formats_verdict(ipp.get("formats") or [])}')
        lines.append(f'Formats it accepts: {", ".join(ipp.get("formats") or []) or "none listed"}')
    else:
        lines.append('IPP: no answer')
    lines.append(f'JetDirect: {"port " + str(printing["jetdirect"]) if printing.get("jetdirect") else "no"}')
    lines.append(f'LPD: {"port " + str(printing["lpd"]) if printing.get("lpd") else "no"}')
    scanners = found.get('scanners') or []
    for scanner in scanners:
        how = ' (found at a known address)' if scanner.get('found_by') == 'known address' else ''
        lines.append(f'Scanning: {scanner["protocol"]} at port {scanner["port"]}, {scanner["path"]}{how}')
    if not scanners:
        lines.append('Scanning: neither eSCL nor WSD answered')
    return lines


def offers_kind(found: dict | None, base: str) -> str:
    """'both' when the address also offers the other job (a multifunction device)."""
    if not found:
        return base
    printing = found.get('printing') or {}
    prints = bool(printing.get('ipp') or printing.get('jetdirect') or printing.get('lpd'))
    scans = bool(found.get('scanners'))
    return 'both' if prints and scans else ('scanner' if scans and base != 'printer' else base)


def reported_model(found: dict | None) -> str:
    """The make and model as the device reports them: IPP's or eSCL's, else WSD's
    manufacturer and model (never WSD's friendly name, which owners may set)."""
    if not found:
        return ''
    ipp = (found.get('printing') or {}).get('ipp') or {}
    escl = next((s for s in found.get('scanners') or [] if s.get('protocol') == 'eSCL'), {})
    wsd = next((s for s in found.get('scanners') or [] if s.get('protocol') == 'WSD'), {})
    return ipp.get('make_model') or escl.get('name') or wsd.get('model') or ''


def finish(*, kind: str, model: str, connection: str, setup: str, sections: list[tuple[str, list[str]]],
           problem: str | None, hosts: list, names: list) -> dict:
    """The report's text, and the short answers for GitHub's form."""
    model = scrub(model, hosts=hosts)
    lines = ['## Hardware report written by Folio', '',
             f'- **Device:** {model or "unknown (it didn’t report its model)"}',
             f'- **Folio:** {about_folio()}']
    for title, items in sections:
        if items:
            lines += ['', f'### {title}', *(f'- {item}' for item in items)]
    if problem and problem.strip():
        lines += ['', '### What Folio reported', f'> {scrub(problem, hosts=hosts, names=names)}']
    lines += ['', '### What happened', '(Say what worked, what didn’t, and anything you had to do.)', '']
    text = '\n'.join(scrub(line) if line.strip() else '' for line in lines)  # Once more, in case
    return {'title': f'[Hardware] {model or KINDS[kind]}', 'text': text,
            'fields': {'device': model, 'kind': KINDS[kind], 'connection': connection,
                       'setup': scrub(setup, hosts=hosts), 'host': about_folio()}}


def printer(*, state: dict, capabilities: dict, driverless: dict | None, offers: dict | None,
            builtin: bool, problem: str | None, hosts: list, names: list) -> dict:
    """A report on a printer set up in CUPS."""
    connection, form_connection = describe_uri(state.get('device_uri'))
    driver = scrub(state.get('driver') or '', hosts=hosts)
    lines = [f'Print server: {"Folio’s built-in one" if builtin else "another CUPS server"}',
             f'Connection: {connection}', f'Driver: {driver or "unknown"}']
    if state.get('error'):
        lines.append(f'The print server didn’t answer: {scrub(state["error"], hosts=hosts, names=names)}')
    else:
        now = state.get('state', 'unknown') + ('' if state.get('accepting', True) else ', not accepting jobs')
        message = scrub(state.get('message') or '', hosts=hosts, names=names)
        reasons = ', '.join(state.get('reasons') or [])
        lines.append(f'Now: {now}' + (f'; {message}' if message else '') + (f' ({reasons})' if reasons else ''))
    if capabilities.get('known'):
        options = ['colour' if capabilities.get('color') else 'monochrome' if capabilities.get('color') is False else None,
                   'two-sided' if capabilities.get('duplex') else 'one-sided' if capabilities.get('duplex') is False else None,
                   f'{len(capabilities.get("media") or [])} paper sizes' if capabilities.get('media') else None,
                   f'{len(capabilities.get("trays") or [])} trays' if capabilities.get('trays') else None]
        lines.append('The driver offers: ' + ', '.join(o for o in options if o))
    if driverless:
        lines.append(f'Asked over IPP, the printer {formats_verdict(driverless.get("formats") or [])}')
    model = (driverless or {}).get('make_model') or reported_model(offers)
    if not model and form_connection == USB:  # usb://Make/Model, else the driver's name
        model = connection.removeprefix('USB (').removesuffix(')') if connection != 'USB' else driver
    sections = [('Printing', lines)]
    if uri_host(state.get('device_uri')):
        sections.append(('What its address offers (checked just now)', offers_lines(offers)))
    setup = 'Driverless (IPP Everywhere)' if 'driverless' in driver.lower() or 'everywhere' in driver.lower() \
        else f'Driver: {driver}' if driver else ''
    setup += f', over {connection.split(",")[0]}' if setup else connection.split(',')[0]
    return finish(kind=offers_kind(offers, 'printer'), model=model, connection=form_connection, setup=setup,
                  sections=sections, problem=problem, hosts=hosts, names=names)


def sane_connection(sane_name: str) -> str:
    """Network or USB, from a SANE device name (empty when it doesn't say)."""
    lowered = sane_name.lower()
    if 'libusb' in lowered or '/usb/' in lowered or lowered.startswith('usb'):
        return USB
    if lowered.startswith(('airscan:', 'escl:')) or '/net/' in lowered or ':net:' in lowered or scrub(sane_name) != sane_name:
        return NETWORK
    return ''


def scanner(*, entry: dict, state: dict, offers: dict | None, shared: dict | None, busy: bool,
            problem: str | None, hosts: list, names: list) -> dict:
    """A report on a scanner in Folio: set up by Folio (WSD or eSCL), or one SANE found."""
    caps = state.get('capabilities') or {}
    lines = []
    if entry.get('kind') == 'managed':
        connection = f'{entry["protocol"]}, port {entry["port"]}, {scrub(entry["path"])}'
        form_connection, setup = NETWORK, f'Scanner via {entry["protocol"]}'
        lines.append(f'Connection: {connection} (set up by Folio)')
    else:
        sane_name = state.get('sane_name') or entry.get('hint') or ''
        backend = sane_name.split(':', 1)[0] if ':' in sane_name else ''
        form_connection, setup = sane_connection(sane_name), f'SANE backend {backend}' if backend else ''
        lines.append(f'Connection: {"SANE backend " + backend if backend else "unknown"}'
                     + (f', {form_connection}' if form_connection else ''))
    # For scanners Folio set up, SANE's description is only Folio's name for them
    description = scrub(state.get('description') or '', hosts=hosts, names=names) if entry.get('kind') != 'managed' else ''
    if description:
        lines.append(f'SANE sees it as: {description}')
    lines.append('SANE finds it now' if state.get('connected') else 'SANE doesn’t find it now')
    if caps.get('sources') or caps.get('modes') or caps.get('resolutions'):
        dpi = caps.get('resolutions') or []
        lines.append('Options: ' + '; '.join(filter(None, (
            ', '.join(caps.get('sources') or []), ', '.join(caps.get('modes') or []),
            f'{min(dpi)}–{max(dpi)} dpi' if dpi else ''))))
    if caps.get('error'):
        lines.append(f'SANE’s answer: {scrub(caps["error"], hosts=hosts, names=names)}')
    if shared is not None:  # airscan.Sharing.status()'s entry for it, {} while AirSane isn't serving
        lines.append('Shared as AirScan: ' + (
            f'yes, but {scrub(shared["problem"], hosts=hosts, names=names)}' if shared.get('problem')
            else 'yes, and AirSane serves it' if shared.get('path') else 'yes, but AirSane isn’t serving it now'))
    sections = [('Scanning', lines)]
    if entry.get('kind') == 'managed' or form_connection == NETWORK:
        sections.append(('What its address offers (checked just now)',
                         ['Not checked: a scan was running.'] if busy else offers_lines(offers)))
    model = reported_model(offers) or description
    return finish(kind=offers_kind(offers, 'scanner'), model=model, connection=form_connection, setup=setup,
                  sections=sections, problem=problem, hosts=hosts, names=names)


def address(*, offers: dict | None, device_uri: str | None, problem: str | None,
            hosts: list, names: list) -> dict:
    """A report on a device that couldn't be added: what its address offers."""
    connection, form_connection = describe_uri(device_uri) if device_uri else ('', NETWORK)
    sections = [('Trying to add it', [f'Connection: {connection}'] if connection else [])]
    if not device_uri or uri_host(device_uri):
        sections.append(('What its address offers (checked just now)', offers_lines(offers)))
    model = reported_model(offers)
    if not model and form_connection == USB:
        model = connection.removeprefix('USB (').removesuffix(')')
    kind = offers_kind(offers, 'printer' if device_uri else 'scanner' if offers and offers.get('scanners') else 'printer')
    return finish(kind=kind, model=model, connection=form_connection, setup=connection.split(',')[0],
                  sections=sections, problem=problem, hosts=hosts, names=names)
