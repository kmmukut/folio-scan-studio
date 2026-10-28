"""What a network address offers, so a printer or scanner can be added
without knowing its ports and paths.

Folio asks the device itself and only reads; nothing is printed or scanned.

- Printing: IPP (plain, then encrypted), and whether the JetDirect and LPD
  ports accept a connection (opened and closed again; nothing is sent).
- Scanning: eSCL (AirScan), and WSD. WSD has no standard address, so Folio
  sends a discovery probe straight to the device (multicast discovery
  doesn't cross subnets) and reads its list of services, which names the
  scan address. Some devices don't answer that probe; then the scan
  addresses known brands use are tried, and kept only if a scanner answers.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

from backend import cupsadmin, devices

IPP_PORT = 631
IPP_PATHS = ('/ipp/print', '/ipp', '/ipp/printer')  # The standard one first
JETDIRECT_PORT, LPD_PORT = 9100, 515
ESCL_PORTS = (80, 8080)
WSD_DISCOVERY_PORT = 3702
WSD_KNOWN = ((8018, '/wsd/scan'),  # Samsung
             (80, '/WDP/SCAN'))    # Epson
CONNECT_TIMEOUT, HTTP_TIMEOUT, DISCOVERY_WAIT = 2.5, 6, 1.0
MAX_ANSWER = 512 * 1024

ANONYMOUS = 'http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous'
SOAP = ('<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof"'
        ' xmlns:wscn="http://schemas.microsoft.com/windows/2006/08/wdp/scan">'
        '<soap:Header><wsa:To>{to}</wsa:To><wsa:Action>{action}</wsa:Action>'
        '<wsa:MessageID>urn:uuid:{id}</wsa:MessageID>{reply}</soap:Header>'
        '<soap:Body>{body}</soap:Body></soap:Envelope>')
REPLY_TO = f'<wsa:ReplyTo><wsa:Address>{ANONYMOUS}</wsa:Address></wsa:ReplyTo>'


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # A device's answer must come from the address that was asked


# Straight to the device: no proxy from the environment, no redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)


def resolve(address: str) -> tuple[str, str]:
    """The address as typed, and its IPv4 address."""
    address = address.strip()
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        if not cupsadmin.HOST.fullmatch(address) or len(address) > 253:
            raise ValueError('Enter an IP address or host name') from None
        try:
            ip = ipaddress.ip_address(socket.getaddrinfo(address, None, socket.AF_INET)[0][4][0])
        except (OSError, IndexError):
            raise ValueError(f'{address} can’t be found. Check the name, or use the IP address.') from None
    if ip.version != 4:
        raise ValueError('Use the device’s IPv4 address')
    return address, str(ip)


def port_open(ip: str, port: int) -> bool:
    """Whether the port accepts a connection. Nothing is sent."""
    try:
        with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def _fetch(url: str, body: bytes | None = None) -> bytes | None:
    headers = {'Content-Type': 'application/soap+xml; charset=utf-8'} if body else {}
    try:
        with _OPENER.open(urllib.request.Request(url, data=body, headers=headers), timeout=HTTP_TIMEOUT) as answer:
            data = answer.read(MAX_ANSWER + 1)
    except (OSError, ValueError):
        return None
    return data if len(data) <= MAX_ANSWER else None


def _xml(data: bytes | None) -> ET.Element | None:
    """Parse a device's answer; documents with a DOCTYPE (entities) are refused."""
    if not data or b'<!doctype' in data.lower() or b'<!entity' in data.lower():
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _all(root: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if _local(e.tag) == name]


def _text(root: ET.Element, name: str) -> str:
    found = _all(root, name)
    return (found[0].text or '').strip() if found else ''


def _scan_address(ip: str, url: str) -> tuple[int, str] | None:
    """Port and path of a scan address the device reported. Folio always
    connects to the address it was given, never to another host."""
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme != 'http':
        return None
    try:
        port = parts.port or 80
        devices.scanner_endpoint(ip, port, parts.path)  # Same rules as a typed one
    except ValueError:
        return None
    return port, parts.path


def soap(to: str, action: str, body: str = '', reply: bool = True) -> bytes:
    return SOAP.format(to=escape(to), action=action, id=uuid.uuid4(), reply=REPLY_TO if reply else '',
                       body=body).encode()


# --- Printing ------------------------------------------------------------------

def ipp_printer(ip: str) -> dict | None:
    """The first IPP path that answers, with what the printer accepts."""
    if not port_open(ip, IPP_PORT):
        return None
    for path in IPP_PATHS:
        found = cupsadmin.driverless_support(f'ipp://{ip}:{IPP_PORT}{path}')
        if found is not None:
            return {'port': IPP_PORT, 'path': path, **found}
    return None


def recommend_printer(found: dict, suggested: dict | None) -> dict | None:
    """How to add the printer: driverless when it prints that way, otherwise
    with a driver over JetDirect (the most direct), IPP or LPD."""
    printing = found['printing']
    ipp_found = printing['ipp']
    if ipp_found and ipp_found['full']:
        return {'connection': 'ipp', 'port': ipp_found['port'], 'path': ipp_found['path'], 'driver': 'everywhere'}
    driver = suggested['ppd'] if suggested else None
    if printing['jetdirect']:
        return {'connection': 'socket', 'port': printing['jetdirect'], 'path': None, 'driver': driver}
    if ipp_found:
        return {'connection': 'ipp', 'port': ipp_found['port'], 'path': ipp_found['path'], 'driver': driver}
    if printing['lpd']:
        return {'connection': 'lpd', 'port': printing['lpd'], 'path': None, 'driver': driver}
    return None


# --- Scanning ------------------------------------------------------------------

def escl_scanner(ip: str) -> dict | None:
    for port in ESCL_PORTS:
        if not port_open(ip, port):
            continue
        root = _xml(_fetch(f'http://{ip}:{port}/eSCL/ScannerCapabilities'))
        if root is not None and _local(root.tag) == 'ScannerCapabilities':
            return {'protocol': 'eSCL', 'port': port, 'path': '/eSCL', 'name': _text(root, 'MakeAndModel'),
                    'found_by': 'device'}
    return None


def wsd_discover(ip: str) -> dict | None:
    """Ask the device itself (WS-Discovery, then WS-Transfer Get) for its scan address."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(DISCOVERY_WAIT)
        probe = soap('urn:schemas-xmlsoap-org:ws:2005:04:discovery',
                     'http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe',
                     '<wsd:Probe><wsd:Types>wsdp:Device</wsd:Types></wsd:Probe>', reply=False)
        match = None
        for _ in range(3):  # UDP: a probe or its answer may get lost
            try:
                udp.sendto(probe, (ip, WSD_DISCOVERY_PORT))
                data, source = udp.recvfrom(65536)
            except OSError:
                continue
            answer = _xml(data) if source[0] == ip else None
            if answer is not None and (match := next(iter(_all(answer, 'ProbeMatch')), None)) is not None:
                break
    if match is None:
        return None
    device_id = _text(match, 'Address')
    for xaddr in _text(match, 'XAddrs').split():
        target = _scan_address(ip, xaddr)
        if not target or not device_id:
            continue
        port, path = target
        metadata = _xml(_fetch(f'http://{ip}:{port}{path}',
                               soap(device_id, 'http://schemas.xmlsoap.org/ws/2004/09/transfer/Get')))
        if metadata is None:
            continue
        model = ' '.join(filter(None, (_text(metadata, 'Manufacturer'), _text(metadata, 'ModelName'))))
        for hosted in _all(metadata, 'Hosted'):
            if 'ScannerServiceType' not in _text(hosted, 'Types'):
                continue
            scan = _scan_address(ip, _text(hosted, 'Address'))
            if scan:
                return {'protocol': 'WSD', 'port': scan[0], 'path': scan[1],
                        'name': _text(metadata, 'FriendlyName') or model, 'model': model, 'found_by': 'device'}
    return None


def wsd_scanner_answers(ip: str, port: int, path: str) -> str | None:
    """The scanner's name if a WSD scanner answers at this address, else None."""
    url = f'http://{ip}:{port}{path}'
    root = _xml(_fetch(url, soap(url, 'http://schemas.microsoft.com/windows/2006/08/wdp/scan/GetScannerElements',
                                 '<wscn:GetScannerElementsRequest><wscn:RequestedElements>'
                                 '<wscn:Name>wscn:ScannerDescription</wscn:Name>'
                                 '</wscn:RequestedElements></wscn:GetScannerElementsRequest>')))
    if root is None or not _all(root, 'GetScannerElementsResponse'):
        return None
    return _text(root, 'ScannerName')


def wsd_scanner(ip: str) -> dict | None:
    found = wsd_discover(ip)
    if found:
        return found
    for port, path in WSD_KNOWN:
        if port_open(ip, port) and (name := wsd_scanner_answers(ip, port, path)) is not None:
            return {'protocol': 'WSD', 'port': port, 'path': path, 'name': name, 'model': '',
                    'found_by': 'known address'}
    return None


# --- Everything at once ----------------------------------------------------------

def check(address: str) -> dict:
    typed, ip = resolve(address)
    with ThreadPoolExecutor(5) as pool:
        ipp_found = pool.submit(ipp_printer, ip)
        escl = pool.submit(escl_scanner, ip)
        wsd = pool.submit(wsd_scanner, ip)
        jetdirect = pool.submit(port_open, ip, JETDIRECT_PORT)
        lpd = pool.submit(port_open, ip, LPD_PORT)
    printer, escl, wsd = ipp_found.result(), escl.result(), wsd.result()
    make_model = (printer or {}).get('make_model') or (wsd or {}).get('model') or (escl or {}).get('name') or ''
    return {'address': typed, 'ip': ip, 'make_model': make_model,
            'scanners': [s for s in (escl, wsd) if s],  # eSCL first: sane-airscan handles it best
            'printing': {'ipp': printer,
                         'jetdirect': JETDIRECT_PORT if jetdirect.result() else None,
                         'lpd': LPD_PORT if lpd.result() else None}}
