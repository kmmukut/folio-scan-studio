"""v0.22: scanners shared as AirScan. Folio runs AirSane for the scanners
switched on in Devices, and the print server's supervisor announces them."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import airprint, airscan, printserver  # noqa: E402
import test_builtin_print_server_v015 as v015  # noqa: E402

builtin = v015.builtin  # The built-in print server with a fake CUPS behind it

SAMSUNG = {'id': 'net-05a0ef437064', 'name': 'Samsung scanner', 'kind': 'managed', 'protocol': 'WSD',
           'ip': '192.0.2.124', 'port': 8018, 'path': '/wsd/scan', 'enabled': True, 'airscan': True}
# What AirSane printed for the maintainer's Samsung and for SANE's test scanners.
STARTUP = """git commit: 08ff4d7 (branch HEAD, rev 333)
start time is 2075751.00
enumerating  devices...
found: airscan:w0:Folio-net-05a0ef437064 Samsung scanner (WSD Folio-net-05a0ef437064 Samsung scanner)
stable unique name: airscan:WSD Folio-net-05a0ef437064 Samsung scanner:1
uuid: 251334b1-202e-5bc1-9723-9ea958538fc3
sane_open(airscan:w0:Folio-net-05a0ef437064 Samsung scanner) -> 0x7f0d040f74d0
found: test:0 (Noname frontend-tester)
stable unique name: test:Noname frontend-tester:1
uuid: f45d13b9-c4f7-5d19-9cd5-63f71ac4b129
error: failed to open device
found: test:1 (Noname frontend-tester)
ignoring test:1
startup took 3.62 secconds
listening on 127.0.0.1:8090
listening on 172.16.4.2:8090
"""
# Trimmed from what AirSane served for the Samsung.
CAPABILITIES = """<?xml version='1.0' encoding='UTF-8'?>
<scan:ScannerCapabilities xmlns:pwg='http://www.pwg.org/schemas/2010/12/sm' xmlns:scan='http://schemas.hp.com/imaging/escl/2011/05/03'>
<pwg:Version>2.0</pwg:Version><pwg:MakeAndModel>{model}</pwg:MakeAndModel><scan:UUID>{uuid}</scan:UUID>
<scan:Platen><scan:PlatenInputCaps><scan:SettingProfiles><scan:SettingProfile name='0'><scan:ColorModes>
<scan:ColorMode>Grayscale8</scan:ColorMode><scan:ColorMode>RGB24</scan:ColorMode></scan:ColorModes>
<scan:DocumentFormats><pwg:DocumentFormat>application/pdf</pwg:DocumentFormat><pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
<pwg:DocumentFormat>image/png</pwg:DocumentFormat></scan:DocumentFormats></scan:SettingProfile></scan:SettingProfiles></scan:PlatenInputCaps></scan:Platen>
<scan:Adf><scan:AdfSimplexInputCaps></scan:AdfSimplexInputCaps></scan:Adf>
</scan:ScannerCapabilities>"""

# A stand-in for airsaned: prints its start-up log, serves ScannerCapabilities,
# starts over on SIGHUP (counting them) and exits on SIGTERM.
FAKE_AIRSANED = r'''
import http.server, json, os, signal, sys, threading
sys.stderr.reconfigure(line_buffering=True)
args = dict(a[2:].split('=', 1) for a in sys.argv[1:] if a.startswith('--') and '=' in a)
port, state = int(args['listen-port']), os.environ['FAKE_AIRSANE']
with open(state + '.args', 'w') as f:
    json.dump(sys.argv[1:], f)
devices = []
def startup():
    global devices
    devices = json.load(open(state + '.devices'))
    print('start time is 1.00', file=sys.stderr)
    for name, model, uuid in devices:
        print(f'found: {name} ({model})', file=sys.stderr)
        print(f'uuid: {uuid}', file=sys.stderr)
    print(f'listening on 127.0.0.1:{port}', file=sys.stderr)
CAPS = %r
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        for i, (name, model, uuid) in enumerate(devices):
            if self.path == ('/eSCL' if i == 0 else '/' + uuid) + '/ScannerCapabilities':
                body = CAPS.format(model=model, uuid=uuid).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
                return self.wfile.write(body)
        self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers()
server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
def hup(*a):
    with open(state + '.hups', 'a') as f:
        f.write('x')
    threading.Thread(target=startup).start()
signal.signal(signal.SIGHUP, hup)
signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
startup()
server.serve_forever()
''' % CAPABILITIES


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_for(condition, timeout=15):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, 'timed out'
        time.sleep(.05)


# --- AirSane's configuration ---------------------------------------------------------

def test_airsane_ignores_every_device_but_the_shared_ones():
    attached = {'id': 'sane-1', 'kind': 'attached', 'hint': 'pixma:04A9176D_(1).x'}
    rule = re.compile(airscan.ignore_rule([SAMSUNG, attached]))  # Lookahead works alike in Python and ECMAScript
    for shared in ('airscan:w0:Folio-net-05a0ef437064 Samsung scanner', 'airscan:e12:Folio-net-05a0ef437064 Samsung scanner',
                   'pixma:04A9176D_(1).x'):
        assert not rule.fullmatch(shared), shared
    for other in ('airscan:w1:Folio-net-000000000000 Other', 'airscan:w0:Folio-net-05a0ef4370645 Samsung scanner',
                  'pixma:04A9176D_(1)Xx', 'test:0', 'airscan:e0:Kyocera (found on the network)'):
        assert rule.fullmatch(other), other


def test_the_configuration_and_when_airsane_must_reload(tmp_path):
    sane_conf = tmp_path / 'managed-airscan.conf'
    sane_conf.write_text('"Folio-net-05a0ef437064 Samsung scanner" = http://192.0.2.124:8018/wsd/scan, WSD\n')
    first = airscan.write_config(tmp_path, [SAMSUNG], sane_conf)
    base = tmp_path / 'airscan'
    assert oct(base.stat().st_mode & 0o777) == '0o700'
    access = (base / 'access.conf').read_text()
    assert 'allow 192.168.0.0/16' in access and 'allow 10.0.0.0/8' in access and '0.0.0.0/0' not in access
    assert (base / 'ignore.conf').read_text().splitlines()[1] == airscan.ignore_rule([SAMSUNG])
    assert airscan.write_config(tmp_path, [SAMSUNG], sane_conf) == first
    sane_conf.write_text(sane_conf.read_text().replace('192.0.2.124', '192.0.2.125'))  # Moved
    assert airscan.write_config(tmp_path, [SAMSUNG], sane_conf) != first
    command = airscan.command(tmp_path)
    assert {'--mdns-announce=false', '--web-interface=false', '--reset-option=false', '--debug=true',
            f'--ignore-list={base / "ignore.conf"}', f'--access-file={base / "access.conf"}'} <= set(command)


def test_only_scanners_folio_set_up_can_be_shared():
    cfg = {'scanners': [SAMSUNG, {**SAMSUNG, 'id': 'net-1', 'airscan': False},
                        {**SAMSUNG, 'id': 'net-2', 'enabled': False},
                        {'id': 'original', 'kind': 'existing', 'airscan': True},
                        {'id': 'sane-1', 'kind': 'attached', 'hint': 'test:0', 'airscan': True}]}
    assert [e['id'] for e in airscan.shared(cfg)] == ['net-05a0ef437064', 'sane-1']


def test_the_startup_log_names_each_shared_scanner():
    log = airscan.StartupLog()
    ready = [log.feed(line) for line in STARTUP.splitlines()]
    assert ready.count(True) == 1 and log.generation == 1
    assert log.uuid_for(SAMSUNG) == '251334b1-202e-5bc1-9723-9ea958538fc3'
    assert log.uuid_for({'kind': 'attached', 'hint': 'test:0'}) is None  # It couldn't be opened
    assert log.uuid_for({'kind': 'attached', 'hint': 'test:1'}) is None  # Ignored
    assert log.uuid_for({**SAMSUNG, 'id': 'net-000000000000'}) is None
    # Lines about scans later on change nothing; a reload starts over.
    log.feed('found: airscan:w0:Folio-net-000000000000 X (WSD X)')
    log.feed('uuid: 00000000-0000-0000-0000-000000000000')
    assert log.uuid_for({**SAMSUNG, 'id': 'net-000000000000'}) is None
    log.feed('start time is 5.00')
    log.feed('listening on 127.0.0.1:8090')
    assert log.generation == 2 and log.devices == {}


# --- Announcing -----------------------------------------------------------------------

def test_the_announcement_says_what_airsane_serves(tmp_path, monkeypatch):
    monkeypatch.setattr(airscan, 'LISTEN_PORT', free_port())
    (tmp_path / 'states.devices').write_text(json.dumps([['airscan:w0:Folio-net-05a0ef437064 Samsung scanner',
                                                          'WSD Samsung', '251334b1-202e-5bc1-9723-9ea958538fc3']]))
    fake = tmp_path / 'airsaned'
    fake.write_text(f'#!{sys.executable}\n{FAKE_AIRSANED}')
    fake.chmod(0o755)
    process = subprocess.Popen([str(fake), f'--listen-port={airscan.LISTEN_PORT}'], stderr=subprocess.DEVNULL,
                               env={**os.environ, 'FAKE_AIRSANE': str(tmp_path / 'states')})
    try:
        wait_for(lambda: airscan.capabilities('/eSCL') is not None)
        caps = airscan.capabilities('/eSCL')
    finally:
        process.terminate()
        process.wait()
    assert caps == {'uuid': '251334b1-202e-5bc1-9723-9ea958538fc3', 'make_model': 'WSD Samsung',
                    'formats': ['application/pdf', 'image/jpeg', 'image/png'], 'color_spaces': ['grayscale', 'color'],
                    'platen': True, 'adf': True, 'duplex': False}
    txt = airscan.txt_records('Samsung scanner', '/eSCL', caps)
    assert txt == {'txtvers': '1', 'vers': '2.0', 'pdl': 'application/pdf,image/jpeg,image/png', 'ty': 'Samsung scanner',
                   'note': 'Folio', 'uuid': '251334b1-202e-5bc1-9723-9ea958538fc3', 'rs': 'eSCL',
                   'cs': 'grayscale,color', 'is': 'platen,adf', 'duplex': 'F'}
    text = airscan.service_file({'id': SAMSUNG['id'], 'name': 'Samsung <scanner>', 'txt': txt}, 8090, airscan.BY_HAND)
    service = ET.fromstring(text.split('\n', 2)[2])  # After the XML and DOCTYPE lines
    assert service.find('name').text == 'Samsung <scanner> @ %h'
    assert service.find('service/type').text == '_uscan._tcp' and service.find('service/port').text == '8090'
    assert 'rs=eSCL' in [r.text for r in service.findall('service/txt-record')]


def test_each_scanner_announced_by_hand_gets_its_own_file_name():
    names = airscan.by_hand_names([
        {'id': 'net-05a0ef437064', 'name': 'Samsung M2885FW'},
        {'id': 'net-c708101a48f6', 'name': 'EPSON Scanner'},
        {'id': 'net-aaaaaaaaaaaa', 'name': 'Büro-Scanner (2. Stock)'},
        {'id': 'net-bbbbbbbbbbbb', 'name': '★★★'},
        {'id': 'net-cccccccccccc', 'name': 'Office'},
        {'id': 'net-dddddddddddd', 'name': 'office!'}])
    assert names == {
        'net-05a0ef437064': 'folio-scanner-samsung-m2885fw.service',
        'net-c708101a48f6': 'folio-scanner-epson-scanner.service',
        'net-aaaaaaaaaaaa': 'folio-scanner-buro-scanner-2-stock.service',
        'net-bbbbbbbbbbbb': 'folio-scanner-net-bbbbbbbbbbbb.service',  # Nothing usable in the name
        'net-cccccccccccc': 'folio-scanner-office-net-cccccccccccc.service',  # Same name: neither replaces the other
        'net-dddddddddddd': 'folio-scanner-office-net-dddddddddddd.service'}
    long_name = airscan.by_hand_names([{'id': 'net-eeeeeeeeeeee', 'name': 'A' * 80}])['net-eeeeeeeeeeee']
    assert long_name == f'folio-scanner-{"a" * 40}.service'
    # Never a name Folio itself writes and removes in a mounted Avahi folder
    assert not any(name.startswith(airscan.SERVICE_PREFIX) for name in names.values())


def test_each_installation_keeps_its_own_machine_id(tmp_path):
    data, target = tmp_path / 'data', tmp_path / 'etc-machine-id'
    target.write_text('a560302b73144f339b5b57cf796f7db5\n')  # The image's own
    assert airscan.keep_machine_id(data, target)
    saved = (data / 'machine-id').read_text().strip()
    assert airscan.MACHINE_ID.fullmatch(saved) and target.read_text() == saved + '\n'
    # The next start, or a new image with another ID of its own: the installation's stays
    target.write_text('ffffffffffffffffffffffffffffffff\n')
    assert airscan.keep_machine_id(data, target)
    assert target.read_text() == saved + '\n' and (data / 'machine-id').read_text() == saved + '\n'
    assert airscan.keep_machine_id(tmp_path / 'other', tmp_path / 'other-id')
    assert (tmp_path / 'other' / 'machine-id').read_text() != saved + '\n'  # Another installation, another ID
    # docker-entrypoint.sh does it as root, before Folio starts
    script = (ROOT / 'docker-entrypoint.sh').read_text()
    assert script.index('python -m backend.airscan') < script.index('exec setpriv')


def test_the_machine_id_file_is_neither_followed_nor_waited_on(tmp_path):
    data, target, saved = tmp_path / 'data', tmp_path / 'machine-id', tmp_path / 'data' / 'machine-id'
    data.mkdir()
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.write_text('0123456789abcdef0123456789abcdef\n')
    saved.symlink_to(elsewhere)  # Folio may change its data folder; root doesn't follow links there
    assert airscan.keep_machine_id(data, target)
    assert elsewhere.read_text() == '0123456789abcdef0123456789abcdef\n' and not saved.is_symlink()
    assert target.read_text() == saved.read_text() != elsewhere.read_text()
    saved.unlink()
    os.mkfifo(saved)  # Nor wait on a pipe
    worker = threading.Thread(target=airscan.keep_machine_id, args=(data, target), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and saved.is_file() and airscan.MACHINE_ID.fullmatch(saved.read_text().strip())
    saved.write_text('not an id\n')
    assert airscan.keep_machine_id(data, target) and airscan.MACHINE_ID.fullmatch(saved.read_text().strip())
    # When it can't be kept, Folio still starts
    saved.unlink()
    saved.mkdir()
    assert airscan.keep_machine_id(data, target) is False
    assert airscan.keep_machine_id(tmp_path / 'more', tmp_path / 'missing' / 'machine-id') is False


def test_the_supervisor_trusts_nothing_in_folios_file(tmp_path):
    base = tmp_path / 'airscan'
    base.mkdir()
    good = {'id': 'net-05a0ef437064', 'name': 'Samsung\x07 scanner', 'txt': {'rs': 'eSCL', 'uuid': 'u-1', 'ty': 'x\ny',
                                                                             'evil': 'x', 'pdl': ['not', 'text']}}
    (base / 'announce.json').write_text(json.dumps([
        good, {**good, 'id': '../../etc/x'}, {**good, 'id': 'A'}, {**good, 'txt': {'uuid': 'u'}}, 'nonsense', {}]))
    assert airscan.read_announcements(tmp_path) == [
        {'id': 'net-05a0ef437064', 'name': 'Samsung scanner', 'txt': {'rs': 'eSCL', 'uuid': 'u-1', 'ty': 'xy'}}]
    (base / 'announce.json').unlink()
    (tmp_path / 'elsewhere.json').write_text(json.dumps([good]))
    (base / 'announce.json').symlink_to(tmp_path / 'elsewhere.json')
    assert airscan.read_announcements(tmp_path) == []  # A link to another file is not followed
    (base / 'announce.json').unlink()
    (base / 'announce.json').write_text(' ' * 70000)
    assert airscan.read_announcements(tmp_path) == []
    (base / 'announce.json').unlink()
    (base / 'announce.json').mkdir()  # Nor a folder, nor a pipe to wait on
    assert airscan.read_announcements(tmp_path) == []
    (base / 'announce.json').rmdir()
    os.mkfifo(base / 'announce.json')
    worker = threading.Thread(target=airscan.read_announcements, args=(tmp_path,), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()


def test_the_supervisor_announces_shared_scanners_beside_airprint(tmp_path, monkeypatch):
    services = tmp_path / 'services'
    services.mkdir()
    monkeypatch.setattr(airprint, 'SERVICES_DIR', services)
    monkeypatch.setattr(airprint, 'STATUS_FILE', tmp_path / 'run' / 'airprint.json')  # AirScan's sits beside it
    monkeypatch.setenv('FOLIO_AIRSCAN_PORT', '18090')
    (services / 'folio-Office.service').write_text('printer')
    (services / 'folio-airscan@net-gone.service').write_text('a scanner no longer shared')
    (services / 'other.service').write_text('not Folio’s')
    data = tmp_path / 'data'
    (data / 'airscan').mkdir(parents=True)
    (data / 'airscan' / 'announce.json').write_text(json.dumps([
        {'id': 'net-05a0ef437064', 'name': 'Samsung scanner', 'txt': {'rs': 'eSCL', 'uuid': 'u-1'}}]))
    printserver.announce(data, server='127.0.0.1:9')  # No CUPS there: AirPrint fails, AirScan still works
    assert sorted(p.name for p in services.iterdir()) == [
        'folio-Office.service', 'folio-airscan@net-05a0ef437064.service', 'other.service']
    written = (services / 'folio-airscan@net-05a0ef437064.service').read_text()
    assert '<port>18090</port>' in written and '<txt-record>rs=eSCL</txt-record>' in written
    assert oct((services / 'folio-airscan@net-05a0ef437064.service').stat().st_mode & 0o777) == '0o644'
    assert airscan.announcement_status()['announced'] == ['net-05a0ef437064']
    # AirPrint's own clean-up leaves the scanners alone; stopping withdraws everything of Folio's.
    airprint.sync(None)
    assert sorted(p.name for p in services.iterdir()) == ['folio-airscan@net-05a0ef437064.service', 'other.service']
    airprint.withdraw()
    assert [p.name for p in services.iterdir()] == ['other.service']


# --- Keeping AirSane running ------------------------------------------------------------

@pytest.fixture
def fake_airsane(tmp_path, monkeypatch):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'airsaned'
    fake.write_text(f'#!{sys.executable}\n{FAKE_AIRSANED}')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
    monkeypatch.setenv('FAKE_AIRSANE', str(tmp_path / 'state'))
    monkeypatch.setattr(airscan, 'LISTEN_PORT', free_port())
    monkeypatch.setattr(airprint, 'STATUS_FILE', tmp_path / 'run' / 'airprint.json')
    return tmp_path / 'state'


def test_airsane_runs_while_a_scanner_is_shared(tmp_path, fake_airsane):
    data = tmp_path / 'data'
    data.mkdir()
    sane_conf = tmp_path / 'managed-airscan.conf'
    sane_conf.write_text('one\n')
    second = {**SAMSUNG, 'id': 'net-aaaaaaaaaaaa', 'name': 'Office scanner'}
    cfg = {'scanners': [dict(SAMSUNG), second]}  # Copies: the test changes them
    Path(f'{fake_airsane}.devices').write_text(json.dumps([
        ['airscan:w0:Folio-net-05a0ef437064 Samsung scanner', 'WSD Samsung', 'uuid-samsung'],
        ['airscan:w1:Folio-net-aaaaaaaaaaaa Office scanner', 'WSD Office', 'uuid-office']]))
    sharing = airscan.Sharing()
    sharing.start(lambda: (data, cfg, sane_conf))
    try:
        wait_for(lambda: sharing.state.get('ready'))
        announced = json.loads((data / 'airscan' / 'announce.json').read_text())
        assert [(i['id'], i['txt']['rs'], i['txt']['uuid']) for i in announced] == [
            ('net-05a0ef437064', 'eSCL', 'uuid-samsung'), ('net-aaaaaaaaaaaa', 'uuid-office', 'uuid-office')]
        status = sharing.status()
        assert status['running'] and status['port'] == 8090 and not status['announcing']
        assert [s['path'] for s in status['scanners']] == ['/eSCL', '/uuid-office']
        assert '<txt-record>rs=uuid-office</txt-record>' in status['scanners'][1]['service_file']
        assert [s['service_name'] for s in status['scanners']] == [
            'folio-scanner-samsung-scanner.service', 'folio-scanner-office-scanner.service']
        args = json.loads(Path(f'{fake_airsane}.args').read_text())
        assert f'--listen-port={airscan.LISTEN_PORT}' in args and '--mdns-announce=false' in args
        # A renamed scanner: AirSane reloads (no restart) and the announcement follows.
        cfg['scanners'][1] = {**second, 'name': 'Upstairs'}
        sane_conf.write_text('two\n')
        Path(f'{fake_airsane}.devices').write_text(json.dumps([
            ['airscan:w0:Folio-net-05a0ef437064 Samsung scanner', 'WSD Samsung', 'uuid-samsung'],
            ['airscan:w1:Folio-net-aaaaaaaaaaaa Upstairs', 'WSD Upstairs', 'uuid-upstairs']]))
        sharing.poke()
        wait_for(lambda: Path(f'{fake_airsane}.hups').exists() and sharing.state.get('ready')
                 and [s['name'] for s in sharing.state['scanners']] == ['Samsung scanner', 'Upstairs'])
        assert Path(f'{fake_airsane}.hups').read_text() == 'x'
        assert json.loads((data / 'airscan' / 'announce.json').read_text())[1]['txt']['uuid'] == 'uuid-upstairs'
        # Nothing shared any more: AirSane stops and the announcements go.
        for entry in cfg['scanners']:
            entry['airscan'] = False
        process = sharing._process
        sharing.poke()
        wait_for(lambda: process.poll() is not None and not sharing.state['running'])
        assert json.loads((data / 'airscan' / 'announce.json').read_text()) == []
    finally:
        sharing.stop()


def test_a_scanner_airsane_cannot_open_is_reported(tmp_path, fake_airsane):
    data = tmp_path / 'data'
    data.mkdir()
    Path(f'{fake_airsane}.devices').write_text('[]')  # AirSane found nothing it could use
    sharing = airscan.Sharing()
    sharing.start(lambda: (data, {'scanners': [SAMSUNG]}, tmp_path / 'none.conf'))
    try:
        wait_for(lambda: sharing.state.get('ready'))
        assert sharing.status()['scanners'] == [{
            'id': SAMSUNG['id'], 'name': 'Samsung scanner', 'path': None, 'uuid': None, 'announced': False,
            'service_file': None, 'service_name': None,
            'problem': 'AirSane couldn’t open this scanner. Check that it’s switched on, '
                                             'then press Refresh devices.'}]
        assert airscan.read_announcements(data) == []  # Nothing to announce
    finally:
        sharing.stop()


# --- Through the API ---------------------------------------------------------------------

def test_an_administrator_switches_sharing_per_scanner(builtin, monkeypatch):
    monkeypatch.setattr(airscan, 'binary', lambda: None)  # The real AirSane is tested separately
    fake, admin = builtin
    added = admin.post('/api/admin/scanners', json={'name': 'Samsung scanner', 'ip': '192.0.2.124', 'protocol': 'WSD',
                                                     'port': 8018, 'path': '/wsd/scan'}).json()
    assert not added.get('airscan')
    assert admin.patch(f"/api/admin/scanners/{added['id']}", json={'airscan': True}).status_code == 200
    scanners = admin.get('/api/admin/devices').json()['scanners']
    assert next(s for s in scanners if s['id'] == added['id'])['airscan'] is True
    status = admin.get('/api/admin/airscan').json()
    assert status == {'available': False, 'running': False, 'ready': False, 'error': None, 'port': 8090,
                      'announcing': False, 'scanners': []}
    assert admin.patch(f"/api/admin/scanners/{added['id']}", json={'airscan': False}).status_code == 200
    admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
    alice = v015.login('alice', 'alice-long-password')
    assert alice.get('/api/admin/airscan').status_code == 403
    assert alice.patch(f"/api/admin/scanners/{added['id']}", json={'airscan': True}).status_code == 403


def test_the_page_offers_sharing():
    page = (ROOT / 'frontend' / 'index.html').read_text()
    script = (ROOT / 'frontend' / 'js' / 'devices.js').read_text()
    assert 'id="airscan-panel"' in page
    assert "api('/api/admin/airscan')" in script and 'data-scanner-action="airscan"' in script
    # Each scanner's own file name, never one shared name that a second scanner would overwrite
    assert '/etc/avahi/services/${html(s.service_name)}' in script and 'folio-scanner.service' not in script
    assert 'If you share or stop sharing another scanner, or rename this one, copy the file again' in script


# --- The real AirSane, inside the image -------------------------------------------------------

needs_airsane = pytest.mark.skipif(not shutil.which('airsaned') or not Path('/etc/sane.d/test.conf').exists(),
                                   reason='AirSane and SANE’s test scanner are in the image only')


@needs_airsane
def test_the_image_has_the_pinned_airsane():
    pinned = re.search(r'ARG AIRSANE_COMMIT=([0-9a-f]{40})', (ROOT / 'Dockerfile').read_text()).group(1)
    assert f'commit {pinned}' in Path('/usr/share/doc/airsane/README.Folio').read_text()
    assert 'GNU GENERAL PUBLIC LICENSE' in Path('/usr/share/doc/airsane/copyright').read_text()


@needs_airsane
def test_a_shared_scanner_really_scans_through_airsane(tmp_path, monkeypatch):
    sane = tmp_path / 'sane'
    sane.mkdir()
    (sane / 'dll.conf').write_text('test\n')  # SANE's test scanners: test:0 and test:1
    shutil.copy('/etc/sane.d/test.conf', sane / 'test.conf')
    monkeypatch.setenv('SANE_CONFIG_DIR', str(sane))
    monkeypatch.setattr(airscan, 'LISTEN_PORT', free_port())
    monkeypatch.setattr(airprint, 'STATUS_FILE', tmp_path / 'run' / 'airprint.json')
    data = tmp_path / 'data'
    data.mkdir()
    cfg = {'scanners': [{'id': 'sane-test', 'name': 'Test scanner', 'kind': 'attached', 'hint': 'test:0',
                         'enabled': True, 'airscan': True}]}
    sharing = airscan.Sharing()
    sharing.start(lambda: (data, cfg, tmp_path / 'none.conf'))
    try:
        wait_for(lambda: sharing.state.get('ready'), timeout=60)
        (item,) = json.loads((data / 'airscan' / 'announce.json').read_text())
        assert item['txt']['rs'] == 'eSCL' and 'platen' in item['txt']['is'].split(',')
        assert 'image/png' in item['txt']['pdl']
        # test:1 is not shared: AirSane left it out.
        assert any(line.startswith('ignoring test:1') for line in sharing._log.lines)
        base = f'http://127.0.0.1:{airscan.LISTEN_PORT}/{item["txt"]["rs"]}'
        settings = ('<?xml version="1.0" encoding="UTF-8"?><scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"'
                    ' xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"><pwg:Version>2.0</pwg:Version>'
                    '<pwg:InputSource>Platen</pwg:InputSource><scan:ColorMode>RGB24</scan:ColorMode>'
                    '<scan:XResolution>75</scan:XResolution><scan:YResolution>75</scan:YResolution>'
                    '<pwg:DocumentFormat>image/png</pwg:DocumentFormat></scan:ScanSettings>')
        started = urllib.request.urlopen(urllib.request.Request(f'{base}/ScanJobs', data=settings.encode(),
                                                                headers={'Content-Type': 'text/xml'}), timeout=30)
        assert started.status == 201
        job = re.sub(r'^https?://[^/]+', '', started.headers['Location'])
        page = urllib.request.urlopen(f'http://127.0.0.1:{airscan.LISTEN_PORT}{job}/NextDocument', timeout=60)
        assert page.headers['Content-Type'] == 'image/png' and page.read(8) == b'\x89PNG\r\n\x1a\n'
    finally:
        sharing.stop()
