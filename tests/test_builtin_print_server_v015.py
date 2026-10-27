"""v0.15: the built-in print server, printer supplies, and counting prints sent
straight to CUPS. The last test runs a real CUPS and only runs inside the image."""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import airprint, app as web, cupsadmin, cupsimport, devices, diagnostics, ipp, printserver, security, stats, tasks  # noqa: E402
from fake_cups import FakeCups  # noqa: E402

PASSWORD = 'builtin-admin-long-password'
SECRET = 'generated-for-folio'


# --- Configuration -----------------------------------------------------------------

def block(conf: str, opening: str) -> str:
    return re.search(re.escape(opening) + r'.*?</(Location|Limit)>', conf, re.S).group(0)


def test_printing_is_open_but_administration_stays_inside_the_container():
    conf = printserver.cupsd_conf(port=631)
    assert 'Allow all' in block(conf, '<Location />')
    for opening in ('<Location /admin>', '<Location /admin/conf>', '<Limit CUPS-Add-Modify-Printer'):
        part = block(conf, opening)
        assert 'Require user @SYSTEM' in part and 'Allow 127.0.0.1' in part and 'Allow all' not in part
    # Announcing is airprint.py's job, through the host's Avahi, never CUPS's own.
    assert 'Browsing No' in conf and 'BrowseLocalProtocols none' in conf
    assert 'Port 8631' in printserver.cupsd_conf(port=8631)


def test_prepare_writes_its_configuration_and_keeps_its_password(tmp_path, monkeypatch):
    package = tmp_path / 'package'
    package.mkdir()
    (package / 'raw.types').write_text('application/octet-stream')
    (package / 'cupsd.conf').write_text('the package default')
    (package / 'printers.conf').write_text('not ours to copy')
    monkeypatch.setattr(printserver, 'DEFAULTS', package)
    data = tmp_path / 'data'
    root = printserver.prepare(data, system=False)
    assert root == data / 'cups'
    assert (root / 'cupsd.conf').read_text().startswith('# Written by Folio')
    assert f'ServerRoot {root}' in (root / 'cups-files.conf').read_text()
    assert (root / 'raw.types').exists() and not (root / 'printers.conf').exists()
    assert (root / 'spool').is_dir() and (root / 'ppd').is_dir()
    user, password = printserver.credentials(data)
    assert user == 'folio-cups' and len(password) >= 24
    assert oct(printserver.secret_path(data).stat().st_mode & 0o777) == '0o600'
    (root / 'cupsd.conf').write_text('edited by hand')
    printserver.prepare(data, system=False)
    assert printserver.credentials(data)[1] == password  # Same login after a restart
    assert (root / 'cupsd.conf').read_text().startswith('# Written by Folio')


@pytest.mark.parametrize('value, on', [(None, True), ('on', True), ('OFF', False), ('false', False), ('0', False)])
def test_the_built_in_server_can_be_switched_off(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv('FOLIO_BUILTIN_CUPS', raising=False)
    else:
        monkeypatch.setenv('FOLIO_BUILTIN_CUPS', value)
    assert printserver.enabled() is on


# --- Announcing printers (AirPrint) through the host's Avahi ------------------------

class Listing:
    """Stands in for a CUPS connection that lists printers."""
    def __init__(self, *printers):
        self.printers = printers

    def request(self, operation, *args, **kwargs):
        assert operation == ipp.CUPS_GET_PRINTERS
        return ipp.Response(0, 1, [(ipp.OPERATION, {}), *[(ipp.PRINTER, p) for p in self.printers]])


def queue(name, info, shared=True, color=False, sides=('one-sided',)):
    return {'printer-name': [name], 'printer-info': [info], 'printer-is-shared': [shared],
            'printer-make-and-model': ['Samsung M288x Series'], 'printer-location': ['Hall'],
            'document-format-supported': ['application/octet-stream', 'image/urf', 'application/vnd.cups-raster',
                                          'image/jpeg', 'application/pdf'],
            'urf-supported': ['V1.4', 'CP1', 'W8', 'RS300'], 'color-supported': [color], 'sides-supported': list(sides),
            'printer-uuid': ['urn:uuid:722de7ad-1def-3c8f-6edc-bccf35df038e'], 'printer-state': [3],
            'printer-type': [8400972]}


def test_shared_printers_are_announced_through_avahi_service_files(tmp_path, monkeypatch):
    monkeypatch.setattr(airprint, 'SERVICES_DIR', tmp_path)
    (tmp_path / 'folio-Gone.service').write_text('an old announcement')
    (tmp_path / 'ssh.service').write_text('the host’s own')
    conn = Listing(queue('Office', 'Office & Co', sides=('one-sided', 'two-sided-long-edge')),
                   queue('Private', 'Not shared', shared=False))
    assert airprint.sync(conn) == ['Office']
    assert sorted(p.name for p in tmp_path.iterdir()) == ['folio-Office.service', 'ssh.service']
    text = (tmp_path / 'folio-Office.service').read_text()
    for expected in ('<name replace-wildcards="yes">Office &amp; Co @ %h</name>', '<type>_ipp._tcp</type>',
                     '<subtype>_universal._sub._ipp._tcp</subtype>', '<port>631</port>', 'rp=printers/Office',
                     'pdl=application/pdf,image/urf,image/jpeg,application/octet-stream', 'URF=V1.4,CP1,W8,RS300',
                     'Color=F', 'Duplex=T', 'UUID=722de7ad-1def-3c8f-6edc-bccf35df038e', 'note=Hall'):
        assert expected in text, expected
    monkeypatch.setenv('FOLIO_AIRPRINT_PORT', '6310')  # Published on another host port
    airprint.sync(conn)
    assert '<port>6310</port>' in (tmp_path / 'folio-Office.service').read_text()
    os.utime(tmp_path / 'folio-Office.service', ns=(1, 1))
    airprint.sync(conn)
    assert (tmp_path / 'folio-Office.service').stat().st_mtime_ns == 1  # Unchanged: left alone, no re-announcement
    assert airprint.sync(None) == [] and sorted(p.name for p in tmp_path.iterdir()) == ['ssh.service']


def test_nothing_is_announced_without_the_avahi_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(airprint, 'SERVICES_DIR', tmp_path / 'not-mounted')
    assert airprint.available() is False and airprint.sync(Listing(queue('Office', 'Office'))) == []


# --- Through Folio, with a fake CUPS standing in for the built-in one ------------

def login(username, password):
    client = TestClient(web.app)
    signed = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert signed.status_code == 200, signed.text
    client.headers['x-csrf-token'] = signed.json()['csrf']
    return client


def follow(client, task, timeout=10):
    deadline = time.monotonic() + timeout
    while task['status'] not in tasks.FINISHED:
        assert time.monotonic() < deadline, task
        time.sleep(.02)
        task = client.get(f"/api/tasks/{task['id']}").json()
    return task


@pytest.fixture
def builtin(tmp_path, monkeypatch):
    fake = FakeCups(username=printserver.ADMIN_USER, password=SECRET)
    fake.printers['Office'] = {'state': 3, 'reasons': [], 'markers': [
        ('Black Toner S/N:CRUM-15030206561', 'toner', 8, '#000000', -1),
        ('Imaging Unit S/N:CRUM-14061259080', 'opc', 57, 'none', -1),
        ('Cyan Ink', 'ink', 15, '#00FFFF', 20),
        ('Waste Toner Box', 'waste-toner', -2, 'none', -1)]}
    monkeypatch.setenv('FOLIO_BUILTIN_CUPS', 'on')
    monkeypatch.setattr(printserver, 'SERVER', fake.server)
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setattr(web, 'CUPS_SERVERS', [])
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    (tmp_path / 'cups').mkdir()
    printserver.secret_path(tmp_path).write_text(SECRET)
    monkeypatch.setattr(devices, 'list_cups_queues', lambda server: [
        {'queue': name, 'ready': entry['state'] != 5, 'status': 'idle'} for name, entry in fake.printers.items()])
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    monkeypatch.setattr(devices, 'cups_capabilities', lambda *a: {
        'known': True, 'media': [], 'duplex': True, 'color': False, 'color_modes': ['auto', 'gray']})

    def fake_print(path, title, **choices):
        return f"request id is Office-{fake.add_job('Office', choices.get('user') or 'anonymous', title)} (1 file(s))"
    monkeypatch.setattr(web, 'print_with_cups', fake_print)
    monkeypatch.setattr(cupsimport, '_last_run', 0.0)
    security._guest_submissions.clear()
    security._global_submissions.clear()
    with TestClient(web.app):
        admin = login('admin', PASSWORD)
        yield fake, admin
        tasks.runner.start(tmp_path / 'tasks')
    fake.close()


def server_path(fake):
    return f'/api/admin/cups/servers/{quote(fake.server, safe="")}'


def test_the_built_in_server_comes_first_and_is_connected(builtin, monkeypatch):
    fake, admin = builtin
    monkeypatch.setattr(web, 'CUPS_SERVERS', ['other-cups:631'])
    servers = admin.get('/api/admin/cups/servers').json()
    assert [s['server'] for s in servers] == [fake.server, 'other-cups:631']
    assert servers[0]['source'] == 'builtin' and servers[0]['admin']['connected'] is True
    assert servers[0]['admin']['username'] == printserver.ADMIN_USER
    assert isinstance(servers[0]['announcing'], bool) and servers[1]['announcing'] is None
    assert admin.delete(server_path(fake)).status_code == 400
    assert admin.put(f'{server_path(fake)}/login', json={'username': 'x', 'password': 'y'}).status_code == 400
    assert admin.delete(f'{server_path(fake)}/login').status_code == 400
    monkeypatch.setenv('FOLIO_BUILTIN_CUPS', 'off')
    assert [s['server'] for s in admin.get('/api/admin/cups/servers').json()] == ['other-cups:631']


def test_printers_are_added_to_it_without_a_login_step(builtin, tmp_path_factory, monkeypatch):
    fake, admin = builtin
    avahi = tmp_path_factory.mktemp('avahi-services')
    monkeypatch.setattr(airprint, 'SERVICES_DIR', avahi)
    assert admin.get('/api/admin/cups/servers').json()[0]['announcing'] is True
    started = admin.post(f'{server_path(fake)}/printers', json={
        'name': 'Hallway', 'connection': 'socket', 'address': '192.0.2.5', 'driver': 'everywhere'})
    assert started.status_code == 202, started.text
    assert follow(admin, started.json()['task'])['status'] == 'done'
    assert fake.printers['Hallway']['device-uri'] == 'socket://192.0.2.5:9100'
    assert sorted(p.name for p in avahi.iterdir()) == ['folio-Hallway.service', 'folio-Office.service']
    assert admin.delete(f"/api/admin/printers/{quote(f'{fake.server}|Hallway', safe='')}").status_code == 200
    for _ in range(50):  # Withdrawn in the background
        if not (avahi / 'folio-Hallway.service').exists():
            break
        time.sleep(.05)
    assert sorted(p.name for p in avahi.iterdir()) == ['folio-Office.service']
    # The fake CUPS only accepts administration with Folio's generated login.
    assert admin.get(f'{server_path(fake)}/devices').status_code == 200


def test_supplies_are_read_cleaned_up_and_flagged(builtin, monkeypatch):
    fake, admin = builtin
    key = quote(f'{fake.server}|Office', safe='')
    supplies = admin.get(f'/api/admin/printers/{key}/supplies').json()['supplies']
    assert [(s['name'], s['level'], s['low']) for s in supplies] == [
        ('Black Toner', 8, True), ('Cyan Ink', 15, True),       # Consumables first
        ('Imaging Unit', 57, False), ('Waste Toner Box', None, False)]
    assert supplies[0]['color'] == '#000000' and supplies[2]['color'] is None
    # Everyone printing sees what is running low on their printer.
    admin.patch(f'/api/admin/printers/{key}', json={'make_default': True})
    admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
    status = login('alice', 'alice-long-password').get('/api/print/status').json()
    assert [s['name'] for s in status['supplies_low']] == ['Black Toner', 'Cyan Ink']
    # And System check says so.
    monkeypatch.setattr(diagnostics, 'probe_cups_server', lambda server: diagnostics.check(
        f'cups:{server}', f'CUPS {server}', 'ok', 'Reachable in 1 ms; 1 printer, 1 ready'))
    printing = admin.get('/api/admin/diagnostics').json()['sections']['printing']
    by_id = {c['id']: c for c in printing}
    assert by_id[f'cups:{fake.server}']['label'] == 'Built-in print server'
    assert 'airprint' in by_id and f'cups-admin:{fake.server}' not in by_id
    assert by_id['supplies']['status'] == 'warn'
    assert 'Black Toner on Office (8 %)' in by_id['supplies']['detail']


def test_a_printer_without_supply_levels_says_nothing(builtin):
    fake, admin = builtin
    fake.printers['Office']['markers'] = []
    key = quote(f'{fake.server}|Office', safe='')
    assert admin.get(f'/api/admin/printers/{key}/supplies').json() == {'supplies': []}


def test_prints_sent_straight_to_cups_are_counted_only_when_chosen(builtin):
    fake, admin = builtin
    fake.add_job('Office', 'mobile', 'Before switching on', state=9, pages=5, sheets=5)
    assert stats.settings(web.DATA_DIR)['include_cups_jobs'] is False
    assert cupsimport.import_jobs(web.DATA_DIR, web.cups_servers()) == 0  # Off by default
    saved = admin.put('/api/admin/stats/settings', json={'enabled': True, 'retention_days': 365,
                                                         'include_cups_jobs': True}).json()
    assert saved['include_cups_jobs'] is True
    assert admin.get('/api/admin/stats').json()['totals']['jobs'] == 0  # Earlier jobs are the starting point
    fake.add_job('Office', 'mobile', 'Boarding pass', state=9, pages=4, sheets=2)
    fake.add_job('Office', 'laptop', 'Still printing', state=5, pages=1, sheets=1)
    token = admin.post('/api/print/inspect', headers={'X-Upload-Filename': 'a.pdf'},
                       content=_pdf(2)).json()['token']
    folio_job = int(admin.post('/api/print/submit-preview', json={'token': token}).json()['job_id'].rsplit('-', 1)[1])
    fake.jobs[folio_job]['state'] = 9  # Folio's own job finishes too
    assert cupsimport.import_jobs(web.DATA_DIR, web.cups_servers()) == 1
    assert cupsimport.import_jobs(web.DATA_DIR, web.cups_servers()) == 0  # Nothing twice
    report = admin.get('/api/admin/stats').json()
    totals = report['totals']
    assert (totals['jobs'], totals['direct_jobs'], totals['direct_pages']) == (2, 1, 4)
    assert totals['pages'] == 6 and totals['sheets'] == 2 + 2  # 4 pages on 2 sheets, plus Folio's 2 one-sided
    assert {p['user'] for p in report['people']} == {'admin', 'mobile'}
    rows = admin.get('/api/admin/stats/export').text.splitlines()
    assert any(',mobile,Office,' in row and ',cups,' in row for row in rows)
    admin.put('/api/admin/stats/settings', json={'enabled': True, 'retention_days': 365, 'include_cups_jobs': False})
    fake.add_job('Office', 'mobile', 'After switching off', state=9, pages=1, sheets=1)
    assert cupsimport.import_jobs(web.DATA_DIR, web.cups_servers()) == 0


def _pdf(pages: int) -> bytes:
    document = fitz.open()
    for _ in range(pages):
        document.new_page()
    return document.tobytes()


# --- A real CUPS (runs inside the image, where cupsd exists and we are root) ------

def cups_report(server: str) -> str:
    """What CUPS says about its jobs, for a failing assertion."""
    jobs = subprocess.run(['lpstat', '-h', server, '-W', 'all', '-o', '-l'], capture_output=True, text=True)
    log = Path('/var/log/cups/error_log')
    return f"jobs:\n{jobs.stdout}{jobs.stderr}\nerror_log:\n{log.read_text()[-3000:] if log.exists() else '(none)'}"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@pytest.mark.skipif(not shutil.which('cupsd') or not hasattr(os, 'geteuid') or os.geteuid() != 0,
                    reason='needs cupsd and root: runs inside the Folio image')
def test_a_real_print_through_the_built_in_server(monkeypatch):
    # Like a real data folder: in a place other users may pass through (pytest's
    # own temporary folders are closed to them, which a real /data never is).
    tmp_path = Path(tempfile.mkdtemp(prefix='folio-real-cups-', dir='/var/tmp'))
    received, sink = bytearray(), socket.socket()
    sink.bind(('127.0.0.1', 0))
    sink.listen(4)

    def collect():  # A stand-in JetDirect printer: keeps whatever it is sent
        while True:
            try:
                conn, _ = sink.accept()
            except OSError:
                return
            with conn:
                while chunk := conn.recv(65536):
                    received.extend(chunk)
    threading.Thread(target=collect, daemon=True).start()

    port = free_port()
    root = printserver.prepare(tmp_path, port=port)
    assert tmp_path.stat().st_mode & 0o077 == 0o011  # Passable, not readable, for other users
    cupsd = subprocess.Popen(printserver.cupsd_command(root))
    server = f'127.0.0.1:{port}'
    try:
        for _ in range(100):
            if subprocess.run(['lpstat', '-h', server, '-r'], capture_output=True).returncode == 0:
                break
            time.sleep(.2)
        monkeypatch.setenv('FOLIO_BUILTIN_CUPS', 'on')
        monkeypatch.setattr(printserver, 'SERVER', server)
        monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
        monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
        monkeypatch.setattr(web, 'DEVICE', '')
        monkeypatch.setattr(web, 'CUPS_SERVERS', [])
        monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
        monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
        monkeypatch.setattr(cupsimport, '_last_run', 0.0)
        avahi = tmp_path / 'avahi-services'
        avahi.mkdir()
        monkeypatch.setattr(airprint, 'SERVICES_DIR', avahi)
        security._guest_submissions.clear()
        with TestClient(web.app):
            admin = login('admin', PASSWORD)
            started = admin.post(f'/api/admin/cups/servers/{quote(server, safe="")}/printers', json={
                'name': 'Real_Test', 'connection': 'socket', 'address': '127.0.0.1',
                'port': sink.getsockname()[1], 'driver': 'drv:///sample.drv/generic.ppd'})
            assert started.status_code == 202, started.text
            assert follow(admin, started.json()['task'], timeout=60)['status'] == 'done'
            announced = (avahi / 'folio-Real_Test.service').read_text()
            assert 'rp=printers/Real_Test' in announced and 'URF=' in announced and 'pdl=application/pdf' in announced
            key = quote(f'{server}|Real_Test', safe='')
            assert admin.patch(f'/api/admin/printers/{key}', json={'make_default': True}).status_code == 200
            token = admin.post('/api/print/inspect', headers={'X-Upload-Filename': 'real.pdf'},
                               content=_pdf(1)).json()['token']
            receipt = admin.post('/api/print/submit-preview', json={'token': token})
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()['job_id'].startswith('Real_Test-')
            for _ in range(150):
                if b'%%EOF' in received:
                    break
                time.sleep(.2)
            # CUPS filtered it into PostScript for the printer.
            assert received.startswith(b'%!PS') and b'%%EOF' in received, cups_report(server)
            assert admin.get('/api/admin/stats').json()['totals']['pages'] == 1
            assert admin.get(f'/api/admin/printers/{key}/supplies').json() == {'supplies': []}
            # A job from "a phone": counted once the setting is on.
            admin.put('/api/admin/stats/settings', json={'enabled': True, 'retention_days': 365,
                                                         'include_cups_jobs': True})
            assert cupsimport.import_jobs(tmp_path, [server]) == 0  # The starting point
            document = tmp_path / 'phone.pdf'
            document.write_bytes(_pdf(1))
            subprocess.run(['lp', '-h', server, '-d', 'Real_Test', '-U', 'phone-user', str(document)], check=True,
                           capture_output=True)
            conn = cupsadmin.connection(tmp_path, server, admin=True)
            for _ in range(150):
                if any(job['user'] == 'phone-user' for job in cupsadmin.list_jobs(conn, which='completed')):
                    break
                time.sleep(.2)
            assert cupsimport.import_jobs(tmp_path, [server]) == 1
            people = {p['user']: p for p in admin.get('/api/admin/stats').json()['people']}
            assert people['phone-user']['pages'] == 1
    finally:
        cupsd.terminate()
        cupsd.wait(timeout=20)
        sink.close()
        tasks.runner.start(tmp_path / 'tasks')
        devices.invalidate()
        shutil.rmtree(tmp_path, ignore_errors=True)
