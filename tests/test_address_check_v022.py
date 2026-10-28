"""v0.22: "Check a network address". Folio asks a device what it offers
(IPP, JetDirect, LPD, eSCL, WSD) and fills in the forms; WSD scanners no
longer start from one brand's port and path."""
from __future__ import annotations

import http.server
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import probe  # noqa: E402
from fake_cups import FakeCups  # noqa: E402
import test_builtin_print_server_v015 as v015  # noqa: E402

builtin = v015.builtin  # The built-in print server with a fake CUPS behind it
follow, server_path = v015.follow, v015.server_path

DEVICE_ID = 'urn:uuid:00000000-0000-4000-8000-000000000001'
ENVELOPE = ('<?xml version="1.0" encoding="utf-8"?>{doctype}<soap:Envelope'
            ' xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
            ' xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
            ' xmlns:wsx="http://schemas.xmlsoap.org/ws/2004/09/mex"'
            ' xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof"'
            ' xmlns:wscn="http://schemas.microsoft.com/windows/2006/08/wdp/scan">'
            '<soap:Header><wsa:Action>{action}</wsa:Action></soap:Header><soap:Body>{body}</soap:Body></soap:Envelope>')


class FakeDevice:
    """A network printer-scanner on 127.0.0.1: WS-Discovery over UDP, and
    WSD and eSCL over HTTP. Each test chooses what it answers."""

    def __init__(self):
        self.answers_discovery = True
        self.escl = False
        self.escl_redirect = False
        self.scan_path = '/WDP/SCAN'
        self.reported_host = '192.0.2.99'  # Its scan address names another host; Folio must still ask 127.0.0.1
        self.doctype = False
        self.requests = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.requests.append(('GET', self.path))
                if self.path == '/eSCL/ScannerCapabilities' and fake.escl_redirect:
                    self.send_response(302)
                    self.send_header('Location', '/elsewhere/ScannerCapabilities')
                    self.send_header('Content-Length', '0')
                    return self.end_headers()
                if fake.escl and self.path.endswith('/ScannerCapabilities'):
                    return self.reply(fake.xml('<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"'
                                               ' xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">'
                                               '<pwg:MakeAndModel>EXAMPLE Office 3000</pwg:MakeAndModel></scan:ScannerCapabilities>'))
                self.reply(b'', 404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                fake.requests.append(('POST', self.path))
                if self.path == '/WSD/DEVICE' and b'transfer/Get<' in body:
                    return self.reply(fake.metadata())
                if self.path == fake.scan_path and b'GetScannerElements<' in body:
                    return self.reply(fake.envelope('http://schemas.microsoft.com/windows/2006/08/wdp/scan/GetScannerElementsResponse',
                                                    '<wscn:GetScannerElementsResponse><wscn:ScannerElements><wscn:ElementData>'
                                                    '<wscn:ScannerDescription><wscn:ScannerName>Office scanner</wscn:ScannerName>'
                                                    '</wscn:ScannerDescription></wscn:ElementData></wscn:ScannerElements>'
                                                    '</wscn:GetScannerElementsResponse>'))
                self.reply(b'', 404)

            def reply(self, body, code=200):
                self.send_response(code)
                self.send_header('Content-Type', 'application/soap+xml')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.httpd.server_address[1]
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(('127.0.0.1', 0))
        self.udp.settimeout(.1)
        self.discovery_port = self.udp.getsockname()[1]
        self.running = True
        for target in (self.httpd.serve_forever, self.answer_discovery):
            threading.Thread(target=target, daemon=True).start()

    def xml(self, text: str) -> bytes:
        doctype = '<!DOCTYPE x [<!ENTITY a "a">]>' if self.doctype else ''
        return f'<?xml version="1.0" encoding="utf-8"?>{doctype}{text}'.encode()

    def envelope(self, action: str, body: str) -> bytes:
        return ENVELOPE.format(doctype='<!DOCTYPE x [<!ENTITY a "a">]>' if self.doctype else '',
                               action=action, body=body).encode()

    def metadata(self) -> bytes:
        def hosted(path, kind):
            return (f'<wsdp:Hosted><wsa:EndpointReference><wsa:Address>http://{self.reported_host}:{self.port}{path}'
                    f'</wsa:Address></wsa:EndpointReference><wsdp:Types>{kind}</wsdp:Types></wsdp:Hosted>')
        return self.envelope('http://schemas.xmlsoap.org/ws/2004/09/transfer/GetResponse', (
            '<wsx:Metadata><wsx:MetadataSection Dialect="http://schemas.xmlsoap.org/ws/2006/02/devprof/ThisModel">'
            '<wsdp:ThisModel><wsdp:Manufacturer>EXAMPLE</wsdp:Manufacturer><wsdp:ModelName>Office 3000</wsdp:ModelName>'
            '</wsdp:ThisModel></wsx:MetadataSection>'
            '<wsx:MetadataSection Dialect="http://schemas.xmlsoap.org/ws/2006/02/devprof/ThisDevice"><wsdp:ThisDevice>'
            '<wsdp:FriendlyName>Office (3000)</wsdp:FriendlyName></wsdp:ThisDevice></wsx:MetadataSection>'
            '<wsx:MetadataSection Dialect="http://schemas.xmlsoap.org/ws/2006/02/devprof/Relationship">'
            '<wsdp:Relationship Type="http://schemas.xmlsoap.org/ws/2006/02/devprof/host">'
            f'<wsdp:Host><wsa:EndpointReference><wsa:Address>{DEVICE_ID}</wsa:Address></wsa:EndpointReference>'
            '<wsdp:Types>wsdp:Device</wsdp:Types></wsdp:Host>'
            f'{hosted("/WDP/PRINT", "wprt:PrinterServiceType")}{hosted(self.scan_path, "wscn:ScannerServiceType")}'
            '</wsdp:Relationship></wsx:MetadataSection></wsx:Metadata>'))

    def answer_discovery(self):
        while self.running:
            try:
                data, source = self.udp.recvfrom(65536)
            except OSError:
                continue
            self.requests.append(('UDP', 'Probe' if b'discovery/Probe<' in data else 'other'))
            if self.answers_discovery and b'discovery/Probe<' in data:
                self.udp.sendto(self.envelope('http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches', (
                    f'<wsd:ProbeMatches><wsd:ProbeMatch><wsa:EndpointReference><wsa:Address>{DEVICE_ID}</wsa:Address>'
                    '</wsa:EndpointReference><wsd:Types>wsdp:Device wscn:ScanDeviceType</wsd:Types>'
                    f'<wsd:XAddrs>http://{self.reported_host}:{self.port}/WSD/DEVICE</wsd:XAddrs>'
                    '</wsd:ProbeMatch></wsd:ProbeMatches>')), source)

    def close(self):
        self.running = False
        self.httpd.shutdown()
        self.httpd.server_close()
        self.udp.close()


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Listener:
    """An open port, like JetDirect's, that records what it receives."""

    def __init__(self):
        self.socket = socket.create_server(('127.0.0.1', 0))
        self.port = self.socket.getsockname()[1]
        self.received = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.socket.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(1)
                try:
                    self.received.append(conn.recv(1024))
                except OSError:
                    self.received.append(b'')

    def close(self):
        self.socket.close()


@pytest.fixture
def device(monkeypatch):
    fake = FakeDevice()
    monkeypatch.setattr(probe, 'WSD_DISCOVERY_PORT', fake.discovery_port)
    monkeypatch.setattr(probe, 'WSD_KNOWN', ((fake.port, '/wsd/scan'), (fake.port, '/WDP/SCAN')))
    monkeypatch.setattr(probe, 'ESCL_PORTS', (fake.port,))
    monkeypatch.setattr(probe, 'DISCOVERY_WAIT', .3)
    for name in ('IPP_PORT', 'JETDIRECT_PORT', 'LPD_PORT'):
        monkeypatch.setattr(probe, name, closed_port())
    yield fake
    fake.close()


@pytest.fixture
def printer(monkeypatch):
    """A network printer answering IPP at /ipp/print."""
    fake = FakeCups()
    fake.printers['Office'] = {'state': 3, 'reasons': [], 'make-and-model': 'Samsung M288x Series',
                               'formats': ['application/octet-stream', 'image/urf']}
    fake.paths['/ipp/print'] = 'Office'
    monkeypatch.setattr(probe, 'IPP_PORT', int(fake.server.rsplit(':', 1)[1]))
    yield fake
    fake.close()


# --- Scanners ---------------------------------------------------------------------

def test_a_wsd_scanner_tells_folio_its_scan_address(device):
    found = probe.check('127.0.0.1')
    assert found['scanners'] == [{'protocol': 'WSD', 'port': device.port, 'path': '/WDP/SCAN',
                                  'name': 'Office (3000)', 'model': 'EXAMPLE Office 3000', 'found_by': 'device'}]
    assert found['make_model'] == 'EXAMPLE Office 3000' and found['ip'] == '127.0.0.1'
    # It reported addresses on another host (192.0.2.99); Folio asked only the address it was given.
    assert ('POST', '/WSD/DEVICE') in device.requests
    assert found['printing'] == {'ipp': None, 'jetdirect': None, 'lpd': None}


def test_a_scanner_that_ignores_discovery_is_found_where_its_brand_answers(device):
    device.answers_discovery = False
    found = probe.check('127.0.0.1')
    # /wsd/scan (tried first) doesn't answer here; /WDP/SCAN does, and only that counts.
    assert found['scanners'] == [{'protocol': 'WSD', 'port': device.port, 'path': '/WDP/SCAN',
                                  'name': 'Office scanner', 'model': '', 'found_by': 'known address'}]
    assert ('POST', '/wsd/scan') in device.requests


def test_no_scanner_is_assumed_when_none_answers(device):
    device.answers_discovery, device.scan_path = False, '/somewhere/else'
    assert probe.check('127.0.0.1')['scanners'] == []
    assert probe.wsd_scanner('127.0.0.1') is None


def test_escl_is_offered_first(device):
    device.escl = True
    scanners = probe.check('127.0.0.1')['scanners']
    assert [s['protocol'] for s in scanners] == ['eSCL', 'WSD']
    assert scanners[0] == {'protocol': 'eSCL', 'port': device.port, 'path': '/eSCL', 'name': 'EXAMPLE Office 3000',
                           'found_by': 'device'}


def test_answers_with_a_doctype_or_a_redirect_are_ignored(device):
    device.escl = device.doctype = True  # Entities could make a small answer huge
    assert probe.check('127.0.0.1')['scanners'] == []
    device.doctype, device.escl_redirect = False, True
    assert [s['protocol'] for s in probe.check('127.0.0.1')['scanners']] == ['WSD']
    assert ('GET', '/elsewhere/ScannerCapabilities') not in device.requests


# --- Printers ---------------------------------------------------------------------

def test_a_driverless_printer_is_recommended_as_driverless(device, printer):
    printer.printers['Office']['formats'] = ['application/pdf', 'image/pwg-raster']
    found = probe.check('127.0.0.1')
    port = probe.IPP_PORT
    assert found['printing']['ipp'] == {'port': port, 'path': '/ipp/print', 'formats': ['application/pdf', 'image/pwg-raster'],
                                        'make_model': 'Samsung M288x Series', 'full': True, 'encrypted_only': False}
    assert found['make_model'] == 'Samsung M288x Series'  # The printer's own name comes first
    assert probe.recommend_printer(found, None) == {'connection': 'ipp', 'port': port, 'path': '/ipp/print',
                                                    'driver': 'everywhere'}


def test_jetdirect_is_checked_without_sending_anything(device, printer, monkeypatch):
    jetdirect = Listener()
    try:
        monkeypatch.setattr(probe, 'JETDIRECT_PORT', jetdirect.port)
        found = probe.check('127.0.0.1')
        assert found['printing']['jetdirect'] == jetdirect.port and found['printing']['lpd'] is None
        assert jetdirect.received == [b'']  # Connected, sent nothing, closed: no job
        # This printer offers only AirPrint's raster: a driver over JetDirect, like the Samsung M2885FW needs.
        assert probe.recommend_printer(found, {'ppd': 'samsung.ppd', 'name': 'Samsung'}) == {
            'connection': 'socket', 'port': jetdirect.port, 'path': None, 'driver': 'samsung.ppd'}
    finally:
        jetdirect.close()


def test_recommendations_without_jetdirect():
    ipp_found = {'port': 631, 'path': '/ipp', 'full': False}
    assert probe.recommend_printer({'printing': {'ipp': ipp_found, 'jetdirect': None, 'lpd': 515}}, None) == {
        'connection': 'ipp', 'port': 631, 'path': '/ipp', 'driver': None}
    assert probe.recommend_printer({'printing': {'ipp': None, 'jetdirect': None, 'lpd': 515}}, None) == {
        'connection': 'lpd', 'port': 515, 'path': None, 'driver': None}
    assert probe.recommend_printer({'printing': {'ipp': None, 'jetdirect': None, 'lpd': None}}, None) is None


def test_addresses_are_checked():
    for bad in ('', 'http://192.0.2.1', '192.0.2.1; rm -rf /', '-x', 'a' * 254):
        with pytest.raises(ValueError):
            probe.resolve(bad)
    with pytest.raises(ValueError, match='IPv4'):
        probe.resolve('2001:db8::1')
    assert probe.resolve(' 127.0.0.1 ') == ('127.0.0.1', '127.0.0.1')
    assert probe.resolve('localhost')[1] == '127.0.0.1'


# --- Through the API ----------------------------------------------------------------

def test_the_check_fills_in_a_driver_from_the_print_server(builtin, device, printer, monkeypatch):
    fake, admin = builtin
    jetdirect = Listener()
    try:
        monkeypatch.setattr(probe, 'JETDIRECT_PORT', jetdirect.port)
        answer = admin.post('/api/admin/devices/check-address', json={'address': '127.0.0.1', 'server': fake.server})
        assert answer.status_code == 200, answer.text
        found = answer.json()
        assert found['printing']['suggested']['name'] == 'Samsung M288x Series PXL'
        assert found['printing']['recommended'] == {'connection': 'socket', 'port': jetdirect.port, 'path': None,
                                                    'driver': found['printing']['suggested']['ppd']}
        assert [s['path'] for s in found['scanners']] == ['/WDP/SCAN']
    finally:
        jetdirect.close()
    assert admin.post('/api/admin/devices/check-address', json={'address': 'not an address'}).status_code == 400
    assert admin.post('/api/admin/devices/check-address',
                      json={'address': '127.0.0.1', 'server': 'unknown:631'}).status_code == 404


def test_only_administrators_can_check_addresses(builtin):
    fake, admin = builtin
    admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
    alice = v015.login('alice', 'alice-long-password')
    assert alice.post('/api/admin/devices/check-address', json={'address': '127.0.0.1'}).status_code == 403


def test_a_wsd_scanner_added_without_port_and_path_is_asked_for_them(builtin, device):
    _, admin = builtin
    added = admin.post('/api/admin/scanners', json={'name': 'Office scanner', 'ip': '127.0.0.1', 'protocol': 'WSD'})
    assert added.status_code == 201, added.text
    assert (added.json()['port'], added.json()['path']) == (device.port, '/WDP/SCAN')
    device.answers_discovery, device.scan_path = False, '/somewhere/else'
    refused = admin.post('/api/admin/scanners', json={'name': 'Other', 'ip': '127.0.0.2', 'protocol': 'WSD'})
    assert refused.status_code == 400 and 'No WSD scanner answered at 127.0.0.2' in refused.json()['detail']
    # eSCL has a standard address; nothing to ask.
    escl = admin.post('/api/admin/scanners', json={'name': 'AirScan', 'ip': '192.0.2.50', 'protocol': 'eSCL'})
    assert (escl.json()['port'], escl.json()['path']) == (80, '/eSCL')


def test_another_protocol_does_not_keep_the_old_port_and_path(builtin):
    _, admin = builtin
    added = admin.post('/api/admin/scanners', json={'name': 'Office scanner', 'ip': '192.0.2.50', 'protocol': 'WSD',
                                                     'port': 8018, 'path': '/wsd/scan'}).json()
    changed = admin.patch(f"/api/admin/scanners/{added['id']}", json={'protocol': 'eSCL'})
    assert changed.status_code == 200, changed.text
    scanner = next(s for s in admin.get('/api/admin/devices').json()['scanners'] if s['id'] == added['id'])
    assert (scanner['protocol'], scanner['port'], scanner['path']) == ('eSCL', 80, '/eSCL')


def test_a_typed_printer_address_can_have_a_port_and_path(builtin):
    fake, admin = builtin
    started = admin.post(f'{server_path(fake)}/printers', json={
        'name': 'Upstairs', 'connection': 'ipp', 'address': '192.0.2.40', 'port': 8631, 'path': '/ipp/printer',
        'driver': 'everywhere'})
    assert started.status_code == 202, started.text
    assert follow(admin, started.json()['task'])['status'] == 'done'
    assert fake.printers['Upstairs']['device-uri'] == 'ipp://192.0.2.40:8631/ipp/printer'


# --- The page -------------------------------------------------------------------------

def test_the_forms_no_longer_start_from_one_brands_scanner_address():
    page = (ROOT / 'frontend' / 'index.html').read_text()
    script = (ROOT / 'frontend' / 'js' / 'devices.js').read_text()
    for text in (page, script):
        assert '8018' not in text and '/wsd/scan' not in text
    assert 'id="address-check-form"' in page and 'id="add-printer-port"' in page and 'id="add-printer-path"' in page
    assert "api('/api/admin/devices/check-address'" in script

