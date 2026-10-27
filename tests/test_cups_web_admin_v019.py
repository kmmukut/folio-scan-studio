"""v0.19: CUPS's own web administration, managed from Folio, and a check
before adding a printer as driverless."""
from __future__ import annotations

import grp
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import app as web, cupsadmin, devices, ipp, printserver, security, tasks  # noqa: E402
from fake_cups import FakeCups  # noqa: E402

PASSWORD = 'web-admin-test-long-password'
SECRET = 'generated-for-folio'
LOGIN = 'a-long-cups-password'


def admin_blocks(conf: str) -> list[str]:
    parts = conf.split('<Location /admin')[1:] + conf.split('<Limit CUPS-Add-Modify-Printer')[1:]
    return [part.split('</')[0] for part in parts]


def test_cups_administration_from_other_computers_needs_https():
    for part in admin_blocks(printserver.cupsd_conf()):
        assert 'Allow all' not in part and 'Allow 127.0.0.1' in part
    for part in admin_blocks(printserver.cupsd_conf(remote_admin=True)):
        assert 'Allow all' in part and 'Encryption Required' in part and 'Require user @SYSTEM' in part


def fake_hash(password: str) -> str:
    return '$y$' + hashlib.sha256(password.encode()).hexdigest()


class Accounts:
    """The container's user list, for tests that don't run as root."""

    def __init__(self, monkeypatch):
        self.users: dict[str, str] = {'root': '*', 'www-data': '*'}
        self.commands: list[tuple] = []
        monkeypatch.setattr(printserver, '_run', self.run)
        monkeypatch.setattr(printserver, '_account_exists', lambda name: name in self.users)
        monkeypatch.setattr(printserver, '_shadow_hash', lambda name: self.users[name])
        monkeypatch.setattr(printserver.subprocess, 'run', self.userdel)

    def run(self, *command, stdin=None):
        self.commands.append(command)
        if command[0] == 'useradd':
            self.users[command[-1]] = '!'
        elif command[0] == 'chpasswd':
            name, _, password = stdin.strip().partition(':')
            self.users[name] = fake_hash(password)
        elif command[0] == 'usermod':
            self.users[command[-1]] = command[2]

    def userdel(self, command, **kwargs):
        assert command[0] == 'userdel', command
        self.users.pop(command[1], None)
        return subprocess.CompletedProcess(command, 0)


@pytest.fixture
def supervisor_files(tmp_path, monkeypatch):
    monkeypatch.setattr(printserver, 'STATUS_FILE', tmp_path / 'run' / 'print-server.json')
    printserver.config_dir(tmp_path).mkdir()
    return tmp_path


def ask(data, request):
    folder = printserver.requests_dir(data)
    folder.mkdir(exist_ok=True)
    (folder / 'web-admin.json').write_text(json.dumps(request))
    assert printserver.process_requests(data)  # cupsd must reload
    assert not (folder / 'web-admin.json').exists()  # The password is gone at once
    return printserver.status()['web_admin']


def test_a_login_is_created_kept_as_a_hash_and_removed(supervisor_files, monkeypatch):
    data, accounts = supervisor_files, Accounts(monkeypatch)
    state = ask(data, {'enabled': True, 'username': 'printadmin', 'password': LOGIN})
    assert state['enabled'] and state['username'] == 'printadmin' and state['error'] is None
    assert ('useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', '--groups', 'lpadmin',
            'printadmin') in accounts.commands
    stored = printserver.web_admin_path(data)
    assert json.loads(stored.read_text()) == {'username': 'printadmin', 'hash': fake_hash(LOGIN)}
    assert LOGIN not in stored.read_text() and stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert 'Encryption Required' in (data / 'cups' / 'cupsd.conf').read_text()
    # A new name replaces the old account.
    ask(data, {'enabled': True, 'username': 'office-admin', 'password': 'another-long-password'})
    assert 'printadmin' not in accounts.users and 'office-admin' in accounts.users
    # After a restart the container has forgotten the account; the hash brings it back.
    del accounts.users['office-admin']
    printserver.prepare(data, system=False)  # (The root-only part is tested in the image.)
    assert printserver.web_admin(data)['username'] == 'office-admin'
    state = ask(data, {'enabled': False})
    assert not state['enabled'] and 'office-admin' not in accounts.users and not stored.exists()
    assert 'Encryption Required' not in (data / 'cups' / 'cupsd.conf').read_text()


@pytest.mark.parametrize('request_body, problem', [
    ({'enabled': True, 'username': 'root', 'password': LOGIN}, 'Choose another username'),
    ({'enabled': True, 'username': 'folio-cups', 'password': LOGIN}, 'Choose another username'),
    ({'enabled': True, 'username': 'Admin', 'password': LOGIN}, 'Choose another username'),
    ({'enabled': True, 'username': 'www-data', 'password': LOGIN}, 'system account'),
    ({'enabled': True, 'username': 'printadmin', 'password': 'short'}, '12–256 characters'),
    ({'enabled': True, 'username': 'printadmin', 'password': 'with:a-colon-inside'}, 'no colons'),
    (['not', 'a', 'request'], 'Unreadable request'),
])
def test_bad_requests_change_nothing(supervisor_files, monkeypatch, request_body, problem):
    data, accounts = supervisor_files, Accounts(monkeypatch)
    state = ask(data, request_body)
    assert problem in state['error'] and not state['enabled']
    assert accounts.users == {'root': '*', 'www-data': '*'} and not printserver.web_admin_path(data).exists()


def test_the_supervisor_never_follows_a_link_or_trusts_a_tampered_login(supervisor_files, monkeypatch):
    data, accounts = supervisor_files, Accounts(monkeypatch)
    elsewhere = data / 'elsewhere.json'
    elsewhere.write_text(json.dumps({'enabled': True, 'username': 'sneaky', 'password': LOGIN}))
    printserver.requests_dir(data).mkdir()
    (printserver.requests_dir(data) / 'web-admin.json').symlink_to(elsewhere)
    assert printserver.process_requests(data)
    assert printserver.status()['web_admin']['error'] == 'Unreadable request' and 'sneaky' not in accounts.users
    assert not os.path.lexists(printserver.requests_dir(data) / 'web-admin.json') and elsewhere.exists()
    for login in ({'username': 'root', 'hash': '$y$x'}, {'username': 'printadmin', 'hash': 'x:0:0'}, ['x'], {}):
        printserver.web_admin_path(data).write_text(json.dumps(login))
        assert printserver.web_admin(data) is None


def test_the_data_folder_stays_usable_by_folios_group(tmp_path):
    tmp_path.chmod(0o1770)
    printserver.allow_passing_through(tmp_path)
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o1771  # Others: pass through only


# --- Through Folio ------------------------------------------------------------------

def login(username, password):
    client = TestClient(web.app)
    signed = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert signed.status_code == 200, signed.text
    client.headers['x-csrf-token'] = signed.json()['csrf']
    return client


@pytest.fixture
def folio(tmp_path, monkeypatch):
    fake = FakeCups(username=printserver.ADMIN_USER, password=SECRET)
    monkeypatch.setattr(printserver, 'SERVER', fake.server)
    monkeypatch.setattr(printserver, 'STATUS_FILE', tmp_path / 'run' / 'print-server.json')
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setattr(web, 'CUPS_SERVERS', [])
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    monkeypatch.setenv('FOLIO_BUILTIN_CUPS', 'on')
    (tmp_path / 'cups').mkdir()
    printserver.secret_path(tmp_path).write_text(SECRET)
    monkeypatch.setattr(devices, 'list_cups_queues', lambda server: [])
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    security._guest_submissions.clear()
    with TestClient(web.app):
        yield fake, login('admin', PASSWORD)
        tasks.runner.start(tmp_path / 'tasks')
    fake.close()


@pytest.fixture
def running_supervisor(folio, monkeypatch):
    accounts, stop = Accounts(monkeypatch), threading.Event()

    def loop():
        while not stop.is_set():
            printserver.process_requests(web.DATA_DIR)
            time.sleep(.05)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    yield accounts
    stop.set()
    thread.join()


def test_administrators_turn_cups_administration_on_and_off(folio, running_supervisor):
    fake, admin = folio
    assert admin.get('/api/admin/cups/builtin/web-admin').json()['enabled'] is False
    turned_on = admin.put('/api/admin/cups/builtin/web-admin', json={'username': 'printadmin', 'password': LOGIN})
    assert turned_on.status_code == 200, turned_on.text
    assert turned_on.json() == {'enabled': True, 'username': 'printadmin', 'error': None, 'pending': False, 'port': 631}
    builtin = next(s for s in admin.get('/api/admin/cups/servers').json() if s['source'] == 'builtin')
    assert builtin['web_admin']['username'] == 'printadmin'
    assert LOGIN not in json.dumps(builtin)
    refused = admin.put('/api/admin/cups/builtin/web-admin', json={'username': 'root', 'password': LOGIN})
    assert refused.status_code == 400 and 'Choose another username' in refused.json()['detail']
    assert admin.delete('/api/admin/cups/builtin/web-admin').json()['enabled'] is False
    assert 'printadmin' not in running_supervisor.users
    assert admin.put('/api/admin/cups/builtin/web-admin', json={'username': 'ab', 'password': LOGIN}).status_code == 422
    admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
    alice = login('alice', 'alice-long-password')
    assert alice.get('/api/admin/cups/builtin/web-admin').status_code == 403
    assert alice.put('/api/admin/cups/builtin/web-admin', json={'username': 'alice', 'password': LOGIN}).status_code == 403


def test_without_an_answer_the_password_is_not_left_behind(folio, monkeypatch):
    fake, admin = folio
    real_time = time.monotonic
    start = real_time()
    monkeypatch.setattr(web.time, 'monotonic', lambda: real_time() + (60 if real_time() - start > .3 else 0))
    answer = admin.put('/api/admin/cups/builtin/web-admin', json={'username': 'printadmin', 'password': LOGIN})
    assert answer.status_code == 503 and 'did not answer' in answer.json()['detail']
    assert not (printserver.requests_dir(web.DATA_DIR) / 'web-admin.json').exists()
    monkeypatch.setenv('FOLIO_BUILTIN_CUPS', 'off')
    assert admin.delete('/api/admin/cups/builtin/web-admin').status_code == 409


def test_a_printer_that_lacks_pdf_and_pwg_raster_gets_a_driver_suggested(folio):
    fake, admin = folio
    fake.printers['samsung'] = {'state': 3, 'reasons': [], 'make-and-model': 'Samsung M288x Series',
                                'formats': ['application/octet-stream', 'application/vnd.hp-PCLXL', 'image/urf']}
    fake.printers['modern'] = {'state': 3, 'reasons': [], 'make-and-model': 'Brother HL-L2350DW',
                               'formats': ['application/pdf', 'image/pwg-raster', 'image/urf']}
    check = f'/api/admin/cups/servers/{quote(fake.server, safe="")}/driverless-check'
    host, port = fake.server.split(':')
    answer = admin.post(check, json={'device_uri': f'ipp://{host}:{port}/printers/samsung'}).json()
    assert answer['checked'] and not answer['full'] and answer['make_model'] == 'Samsung M288x Series'
    assert 'image/urf' in answer['formats'] and 'M288x' in answer['suggested']['name']
    answer = admin.post(check, json={'device_uri': f'ipp://{host}:{port}/printers/modern'}).json()
    assert answer['full'] and answer['suggested'] is None
    # A printer that doesn't answer, or isn't IPP, can't be checked; CUPS will try.
    assert admin.post(check, json={'device_uri': 'socket://192.0.2.1:9100'}).json() == {'checked': False}
    assert admin.post(check, json={'connection': 'ipp', 'address': 'bad host!'}).status_code == 400
    assert cupsadmin.driverless_support(f'ipp://{host}:{port}/printers/missing') is None


def test_the_driver_named_like_the_printer_wins_a_tie():
    candidates = [{'ppd': 'lj5', 'name': 'Samsung M288x Foomatic/lj5gray', 'language': 'en'},
                  {'ppd': 'pxlmono', 'name': 'Samsung M288x Foomatic/pxlmono', 'language': 'en'},
                  {'ppd': 'pxl', 'name': 'Samsung M288x Series PXL', 'language': 'en'}]
    # What the printer reports (the driverless check) or its model (the driver picker).
    for query in ('Samsung M288x Series', 'M288x  Series'):
        assert cupsadmin.rank_drivers(candidates, query)[0]['ppd'] == 'pxl'
    assert [d['ppd'] for d in cupsadmin.rank_drivers(candidates, 'M288x')] == ['lj5', 'pxlmono', 'pxl']


# --- A real CUPS (runs inside the image, where cupsd exists and we are root) ------

def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def outside_address() -> str | None:
    """This container's own network address: connections from it are not local."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(('192.0.2.1', 9))
        except OSError:
            return None
        address = probe.getsockname()[0]
    return None if address.startswith('127.') else address


def wait_for(server: str) -> None:
    for _ in range(100):  # (lpstat -r exits 0 either way; only its words tell)
        answer = subprocess.run(['lpstat', '-h', server, '-r'], capture_output=True, text=True).stdout
        if answer.strip() == 'scheduler is running':
            return
        time.sleep(.2)
    raise AssertionError(f'cupsd at {server} did not answer')


def add_queue(conn: ipp.Connection, name: str, resource: str = '/admin/') -> None:
    conn.request(ipp.CUPS_ADD_MODIFY_PRINTER, [(ipp.URI, 'printer-uri', conn.printer_uri(name)),
                                               (ipp.NAME, 'ppd-name', 'drv:///sample.drv/generic.ppd')],
                 resource=resource, timeout=60, printer_attributes=[(ipp.URI, 'device-uri', 'socket://127.0.0.1:9')])


@pytest.mark.skipif(not shutil.which('cupsd') or not hasattr(os, 'geteuid') or os.geteuid() != 0,
                    reason='needs cupsd and root: runs inside the Folio image')
def test_cups_administration_with_a_real_cups(monkeypatch):
    address = outside_address()
    if not address:
        pytest.skip('no network address besides loopback')
    data = Path(tempfile.mkdtemp(prefix='folio-web-admin-', dir='/var/tmp'))
    monkeypatch.setattr(printserver, 'STATUS_FILE', data / 'run' / 'print-server.json')
    # Left behind by someone else (the print server was off): taken back at start.
    (data / 'cups').mkdir()
    (data / 'cups' / 'planted.conf').write_text('ServerBin /tmp')
    os.chown(data / 'cups', 1000, 1000)
    os.chown(data / 'cups' / 'planted.conf', 1000, 1000)
    (data / 'cups' / 'printers.conf').symlink_to('/etc/shadow')
    port = free_port()
    root = printserver.prepare(data, port=port)
    planted = (root / 'planted.conf').stat()
    assert root.stat().st_uid == 0 and (planted.st_uid, planted.st_gid) == (0, grp.getgrnam('lp').gr_gid)
    assert not os.path.lexists(root / 'printers.conf')
    cupsd = subprocess.Popen(printserver.cupsd_command(root))
    local, remote = f'127.0.0.1:{port}', f'{address}:{port}'
    folio_login = printserver.credentials(data)
    try:
        wait_for(local)
        # Off: other computers may not administer at all, even with Folio's login.
        with pytest.raises(ipp.AuthRequired):
            add_queue(ipp.Connection(remote, *folio_login), 'Remote_Test')
        printserver.requests_dir(data).mkdir()
        (printserver.requests_dir(data) / 'web-admin.json').write_text(
            json.dumps({'enabled': True, 'username': 'printadmin', 'password': LOGIN}))
        assert printserver.process_requests(data, port=port)
        assert printserver.status()['web_admin']['error'] is None
        cupsd.send_signal(signal.SIGHUP)
        time.sleep(1)
        wait_for(local)
        # On: plain HTTP is refused before any password is checked, whichever way it's sent…
        for resource in ('/admin/', '/'):
            with pytest.raises(ipp.EncryptionRequired):
                add_queue(ipp.Connection(remote, 'printadmin', LOGIN), 'Remote_Test', resource)
        # …over HTTPS the login works, and only the right password.
        with pytest.raises(ipp.AuthRequired):
            add_queue(ipp.Connection(remote, 'printadmin', 'the-wrong-password', tls=True), 'Remote_Test')
        add_queue(ipp.Connection(remote, 'printadmin', LOGIN, tls=True), 'Remote_Test')
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        page = http.client.HTTPSConnection(address, port, context=context, timeout=15)
        page.request('GET', '/admin', headers={'Authorization': 'Basic ' + __import__('base64').b64encode(
            f'printadmin:{LOGIN}'.encode()).decode()})
        assert page.getresponse().status == 200
        page.close()
        # Folio itself still administers locally, without TLS.
        add_queue(ipp.Connection(local, *folio_login), 'Local_Test')
        # A restart forgets the container's accounts; the stored hash restores the login.
        subprocess.run(['userdel', 'printadmin'], check=True)
        printserver.prepare(data, port=port)
        add_queue(ipp.Connection(remote, 'printadmin', LOGIN, tls=True), 'Remote_Test')
        # Off again: the account is gone and other computers are refused.
        (printserver.requests_dir(data) / 'web-admin.json').write_text(json.dumps({'enabled': False}))
        assert printserver.process_requests(data, port=port)
        cupsd.send_signal(signal.SIGHUP)
        time.sleep(1)
        wait_for(local)
        assert subprocess.run(['id', 'printadmin'], capture_output=True).returncode != 0
        with pytest.raises(ipp.AuthRequired):
            add_queue(ipp.Connection(remote, *folio_login, tls=True), 'Remote_Test')
    finally:
        cupsd.terminate()
        cupsd.wait(timeout=20)
        subprocess.run(['userdel', 'printadmin'], capture_output=True)
        shutil.rmtree(data, ignore_errors=True)
