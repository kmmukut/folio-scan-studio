"""v0.22: hardware reports. Folio writes a report on a printer or scanner from
what the device and the print server answer, for the administrator to read
and submit on GitHub. Reports are public, so nothing in them may identify a
network or a person."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import app as web, airscan, devices, probe, report  # noqa: E402
import test_builtin_print_server_v015 as v015  # noqa: E402

builtin = v015.builtin  # The built-in print server with a fake CUPS behind it

ADDRESS = '192.0.2.124'
# What the address check finds at a Samsung M2885FW (probe.check's answer)
SAMSUNG = {'address': ADDRESS, 'ip': ADDRESS, 'make_model': 'Samsung M2885FW Series',
           'scanners': [{'protocol': 'WSD', 'port': 8018, 'path': '/wsd/scan', 'name': 'SEC30CDA7A1B2C3',
                         'model': '', 'found_by': 'known address'}],
           'printing': {'ipp': {'port': 631, 'path': '/ipp/print', 'make_model': 'Samsung M2885FW Series',
                                'formats': ['application/octet-stream', 'application/vnd.hp-PCL', 'image/urf'],
                                'full': False, 'encrypted_only': False},
                        'jetdirect': 9100, 'lpd': 515}}
PRIVATE = (ADDRESS, '192.0.2', 'SEC30CDA7A1B2C3', 'printer.lan', 'Folio-net', 'net-', 'ZD7QB8KD')


def private_parts(text: str) -> list[str]:
    return [part for part in PRIVATE if part.lower() in text.lower()]


def test_reports_leave_out_what_identifies_a_network_or_a_person():
    text = report.scrub(
        'ipp://admin:secret@printer.lan:631/ipp/print?uuid=4509a320-00a0-008f-00b6-002507510eca '
        'usb://Samsung/M2880?serial=ZD7QB8KD9C00XYZ, Black Toner S/N:CRUM-15030206561, MFG:EPSON;SN:X3B8123; '
        'MAC a4:d7:3c:87:46:d4 at 192.168.1.234, fe80::1%eth0 or [2001:db8::5], owner jane@example.com, '
        'scanner Folio-net-05a0ef437064 at office-mfp, Kitchen printer stopped',
        hosts=['office-mfp'], names=['Kitchen printer'])
    for private in ('admin', 'secret', 'printer.lan', '4509a320', 'ZD7QB8KD', 'CRUM', 'X3B8123', 'a4:d7',
                    '192.168', 'fe80', '2001:db8', 'jane', 'example.com', '05a0ef437064', 'office-mfp', 'Kitchen'):
        assert private not in text, (private, text)
    assert '<login>@<host>:631/ipp/print' in text and '<MAC address>' in text and '<name> stopped' in text
    # What isn't private stays: versions, models, ports, paths, ordinary words
    kept = report.scrub('Samsung M288x Series PXL, HPLIP 3.22.10, Folio 0.22.0, port 9100, /wsd/scan, serial port, '
                        'a printer named printer', hosts=['localhost'], names=['ab'])
    assert kept == 'Samsung M288x Series PXL, HPLIP 3.22.10, Folio 0.22.0, port 9100, /wsd/scan, serial port, ' \
                   'a printer named printer'


def test_device_addresses_are_described_not_copied():
    assert report.describe_uri(f'socket://{ADDRESS}:9100') == ('JetDirect, port 9100', report.NETWORK)
    assert report.describe_uri('ipps://printer.lan/ipp/print') == ('IPP over TLS, port 631, /ipp/print', report.NETWORK)
    assert report.describe_uri('usb://Samsung/M2880%20Series?serial=ZD7QB8KD') == ('USB (Samsung M2880 Series)',
                                                                                  report.USB)
    assert report.describe_uri('lpd://printer.lan/kitchen') == ('LPD', report.NETWORK)  # Queue names may be personal
    assert report.describe_uri('dnssd://Jane%27s%20Epson._ipp._tcp.local/?uuid=x') == ('IPP, found through Bonjour',
                                                                                        report.NETWORK)
    assert report.uri_host(f'socket://{ADDRESS}:9100') == ADDRESS and report.uri_host('usb://Samsung/M2880') is None


def test_a_printer_report_says_what_the_printer_and_cups_answer(builtin, monkeypatch):
    fake, admin = builtin
    fake.printers['Kitchen'] = {'state': 5, 'reasons': ['paused'], 'make-and-model': 'Samsung M288x Series PXL',
                                'device-uri': f'socket://{ADDRESS}:9100',
                                'message': f'Kitchen: unable to connect to {ADDRESS}'}
    asked = []
    monkeypatch.setattr(probe, 'check', lambda address: asked.append(address) or SAMSUNG)
    printer = devices.printer_id(fake.server, 'Kitchen')
    answer = admin.post('/api/admin/devices/report', json={'printer': printer, 'problem': 'Kitchen printed garbage'})
    assert answer.status_code == 200, answer.text
    result = answer.json()
    text = result['text']
    assert asked == [ADDRESS]  # The queue's own address, asked only for what it offers
    for expected in ('**Device:** Samsung M2885FW Series', 'Print server: Folio’s built-in one',
                     'Connection: JetDirect, port 9100', 'Driver: Samsung M288x Series PXL',
                     'Now: stopped; <name>: unable to connect to <address> (paused)',
                     'The driver offers: monochrome, two-sided',
                     'IPP at port 631, /ipp/print: no PDF or PWG raster, only AirPrint raster',
                     'Formats it accepts: application/octet-stream, application/vnd.hp-PCL, image/urf',
                     'JetDirect: port 9100', 'Scanning: WSD at port 8018, /wsd/scan (found at a known address)',
                     '> <name> printed garbage', '### What happened'):
        assert expected in text, (expected, text.splitlines())
    assert re.search(r'\*\*Folio:\*\* 0\.22\.\d+, (Full|Lite) edition, (amd64|arm64)\n', text)
    assert private_parts(text) == [] and 'Kitchen' not in text and fake.server not in text
    assert result['title'] == '[Hardware] Samsung M2885FW Series'
    assert result['fields'] == {'device': 'Samsung M2885FW Series', 'kind': 'Multifunction (print and scan)',
                                'connection': report.NETWORK, 'setup': 'Driver: Samsung M288x Series PXL, over JetDirect',
                                'host': report.about_folio()}


def test_a_usb_printer_is_reported_without_asking_the_network(builtin, monkeypatch):
    fake, admin = builtin
    fake.printers['Laser'] = {'state': 3, 'reasons': [], 'make-and-model': 'Brother HL-L2350DW series, using brlaser v6',
                              'device-uri': 'usb://Brother/HL-L2350DW%20series?serial=E78123A9N123456'}
    monkeypatch.setattr(probe, 'check', lambda address: pytest.fail('a USB printer has no address to ask'))
    result = admin.post('/api/admin/devices/report', json={'printer': devices.printer_id(fake.server, 'Laser')}).json()
    assert result['fields']['device'] == 'Brother HL-L2350DW series' and result['fields']['connection'] == report.USB
    assert 'Connection: USB (Brother HL-L2350DW series)' in result['text'] and 'E78123A9N' not in result['text']
    assert 'What its address offers' not in result['text']


def test_a_scanner_report_says_what_sane_and_the_scanner_answer(builtin, monkeypatch):
    _, admin = builtin
    monkeypatch.setattr(airscan, 'binary', lambda: None)
    added = admin.post('/api/admin/scanners', json={'name': 'Jane scanner', 'ip': ADDRESS, 'protocol': 'WSD',
                                                     'port': 8018, 'path': '/wsd/scan'}).json()
    sane_name = f'airscan:w0:Folio-{added["id"]} Jane scanner'
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [{'sane_name': sane_name,
                                                                   'description': f'WSD Folio-{added["id"]} Jane scanner'}])
    monkeypatch.setattr(devices, 'scanner_capabilities', lambda name: {
        'sources': ['Flatbed', 'ADF'], 'modes': ['Color', 'Gray'], 'resolutions': [75, 150, 300], 'available': True})
    monkeypatch.setattr(probe, 'check', lambda address: SAMSUNG)
    admin.patch(f"/api/admin/scanners/{added['id']}", json={'airscan': True})
    result = admin.post('/api/admin/devices/report', json={'scanner': added['id']}).json()
    text = result['text']
    for expected in ('**Device:** Samsung M2885FW Series', 'Connection: WSD, port 8018, /wsd/scan (set up by Folio)',
                     'SANE finds it now', 'Options: Flatbed, ADF; Color, Gray; 75–300 dpi',
                     'Shared as AirScan: yes, but AirSane isn’t serving it now', 'IPP at port 631, /ipp/print'):
        assert expected in text, (expected, text.splitlines())
    assert private_parts(text) == [] and 'Jane' not in text and added['id'] not in text
    assert result['fields']['kind'] == 'Multifunction (print and scan)' and result['fields']['setup'] == 'Scanner via WSD'
    # While a scan runs, the scanner isn't asked anything more
    monkeypatch.setitem(web.scan_state, 'active', True)
    monkeypatch.setattr(probe, 'check', lambda address: pytest.fail('asked the scanner during a scan'))
    busy = admin.post('/api/admin/devices/report', json={'scanner': added['id']}).json()['text']
    assert 'Not checked: a scan was running.' in busy


def test_a_scanner_sane_found_by_itself(builtin, monkeypatch):
    _, admin = builtin
    hint = 'epson2:libusb:001:004'
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [{'sane_name': hint,
                                                                   'description': 'Epson PID 1188 flatbed scanner'}])
    monkeypatch.setattr(devices, 'scanner_capabilities', lambda name: {
        'sources': [], 'modes': [], 'resolutions': [], 'available': False, 'error': 'Error during device I/O'})
    monkeypatch.setattr(probe, 'check', lambda address: pytest.fail('a USB scanner has no address to ask'))
    added = admin.post('/api/admin/scanners/attach', json={'name': 'Desk', 'device_name': hint}).json()
    result = admin.post('/api/admin/devices/report', json={'scanner': added['id']}).json()
    assert result['fields'] == {'device': 'Epson PID 1188 flatbed scanner', 'kind': 'Scanner', 'connection': report.USB,
                                'setup': 'SANE backend epson2', 'host': report.about_folio()}
    assert 'SANE’s answer: Error during device I/O' in result['text']


def test_a_report_on_a_device_that_could_not_be_added(builtin, monkeypatch):
    _, admin = builtin
    monkeypatch.setattr(probe, 'check', lambda address: SAMSUNG)
    result = admin.post('/api/admin/devices/report', json={
        'address': ADDRESS, 'name': 'Kitchen_Laser',
        'problem': 'Adding it as a printer (JetDirect, driverless) failed: A printer named Kitchen_Laser already exists'}).json()
    assert '> Adding it as a printer (JetDirect, driverless) failed: A printer named <name> already exists' in result['text']
    assert result['fields']['device'] == 'Samsung M2885FW Series' and private_parts(result['text']) == []

    def unreachable(address):
        raise ValueError('No such address')
    monkeypatch.setattr(probe, 'check', unreachable)
    result = admin.post('/api/admin/devices/report', json={'address': 'printer.lan'}).json()
    assert 'Folio couldn’t reach the address.' in result['text'] and result['title'] == '[Hardware] Printer'
    usb = admin.post('/api/admin/devices/report', json={
        'device_uri': 'usb://HP/LaserJet%201020?serial=ZD7QB8KD', 'problem': 'No driver found'}).json()
    assert usb['fields']['device'] == 'HP LaserJet 1020' and usb['fields']['connection'] == report.USB
    assert private_parts(usb['text']) == []


def test_reports_are_for_administrators_one_device_at_a_time(builtin):
    _, admin = builtin
    assert admin.post('/api/admin/devices/report', json={}).status_code == 400
    assert admin.post('/api/admin/devices/report', json={'scanner': 'x', 'address': ADDRESS}).status_code == 400
    assert admin.post('/api/admin/devices/report', json={'scanner': 'net-000000000000'}).status_code == 404
    admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
    alice = v015.login('alice', 'alice-long-password')
    assert alice.post('/api/admin/devices/report', json={'address': ADDRESS}).status_code == 403


def test_the_report_matches_githubs_form():
    form = (ROOT / '.github' / 'ISSUE_TEMPLATE' / 'hardware_report.yml').read_text()
    ids = re.findall(r'^\s+id: (\w+)$', form, re.M)
    assert {'device', 'kind', 'connection', 'setup', 'host', 'notes'} <= set(ids)
    options = set(re.findall(r'^\s+- (.+)$', form, re.M))
    assert set(report.KINDS.values()) <= options and {report.NETWORK, report.USB, report.IPP_USB} <= options
    script = (ROOT / 'frontend' / 'js' / 'devices.js').read_text()
    assert "HARDWARE_FORM='https://github.com/kmmukut/folio-scan-studio/issues/new'" in script
    assert "template:'hardware_report.yml'" in script


def test_the_page_offers_reports():
    page = (ROOT / 'frontend' / 'index.html').read_text()
    script = (ROOT / 'frontend' / 'js' / 'devices.js').read_text()
    assert 'id="report-dialog"' in page and 'id="report-text"' in page and 'id="report-open"' in page
    assert 'rel="noopener"' in page.split('id="report-open"')[1].split('>')[0]
    for button in ('data-scanner-action="report"', 'data-queue-action="report"', 'data-address-report'):
        assert button in script
    assert "api('/api/admin/devices/report'" in script
    assert 'offerHardwareReport(feedback,' in script  # When adding a printer fails
