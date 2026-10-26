"""v0.11 printer administration and print jobs, against a fake CUPS that speaks IPP."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import app as web, cupsadmin, devices, ipp, security, tasks  # noqa: E402
from fake_cups import FakeCups, self_signed_certificate  # noqa: E402

PASSWORD = 'admin-long-password-0110'
REAL_PRINT_WITH_CUPS = web.print_with_cups  # Saved before fixtures replace it


# --- IPP encoding ----------------------------------------------------------------

def test_ipp_round_trip_including_collections_and_languages():
    body = ipp.encode(ipp.GET_JOBS, [ipp.operation_attributes(
        (ipp.INTEGER, 'limit', 5), (ipp.BOOLEAN, 'my-jobs', True), (ipp.KEYWORD, 'requested-attributes', ['a', 'b']))],
        request_id=42)
    decoded = ipp.decode(body)
    assert decoded.request_id == 42
    attrs = decoded.group(ipp.OPERATION)
    assert attrs['limit'] == [5] and attrs['my-jobs'] == [True] and attrs['requested-attributes'] == ['a', 'b']
    # A response with a collection and a text-with-language value, built by hand.
    def attr(tag, name, raw):
        return bytes([tag]) + len(name).to_bytes(2, 'big') + name.encode() + len(raw).to_bytes(2, 'big') + raw
    data = (b'\x01\x01\x00\x00\x00\x00\x00\x07' + b'\x04'
            + attr(ipp.BEGIN_COLLECTION, 'media-col', b'') + attr(ipp.MEMBER_NAME, '', b'media-size')
            + attr(ipp.BEGIN_COLLECTION, '', b'') + attr(ipp.MEMBER_NAME, '', b'x-dimension')
            + attr(ipp.INTEGER, '', (21000).to_bytes(4, 'big')) + attr(ipp.END_COLLECTION, '', b'')
            + attr(ipp.END_COLLECTION, '', b'')
            + attr(ipp.TEXT_LANG, 'printer-info', b'\x00\x02en\x00\x05Hello') + attr(0x13, 'printer-location', b'')
            + b'\x03')
    printer = ipp.decode(data).group(ipp.PRINTER)
    assert printer['media-col'] == [{'media-size': [{'x-dimension': [21000]}]}]
    assert printer['printer-info'] == ['Hello'] and printer['printer-location'] == [None]


def test_malformed_ipp_is_rejected_cleanly():
    with pytest.raises(ipp.IPPError):
        ipp.decode(b'\x01\x01')
    with pytest.raises(ipp.IPPError):
        ipp.decode(b'\x01\x01\x00\x00\x00\x00\x00\x01\x04\x41\x00\x40abc')


# --- Building device URIs and choosing drivers -----------------------------------------

def test_manual_addresses_become_safe_device_uris():
    assert cupsadmin.manual_uri('ipp', '192.168.1.40') == 'ipp://192.168.1.40/ipp/print'
    assert cupsadmin.manual_uri('socket', 'printer.lan') == 'socket://printer.lan:9100'
    assert cupsadmin.manual_uri('lpd', '10.0.0.9', path='lp1') == 'lpd://10.0.0.9/lp1'
    assert cupsadmin.manual_uri('ipp', 'fe80::1', port=631) == 'ipp://[fe80::1]:631/ipp/print'
    for kind, address, path in (('ipp', 'evil host', None), ('ipp', 'a@b', None), ('socket', 'x;rm', None),
                                ('ipp', '10.0.0.1', '/../../etc'), ('ipp', '10.0.0.1', '/a b'), ('file', '/etc', None)):
        with pytest.raises(ValueError):
            cupsadmin.manual_uri(kind, address, path=path)


def test_driver_ranking_matches_model_families():
    candidates = [{'ppd': 'a', 'name': 'Samsung C48x Series PS', 'language': 'en'},
                  {'ppd': 'b', 'name': 'Samsung M288x Series PXL', 'language': 'en'},
                  {'ppd': 'c', 'name': 'Samsung M288x Foomatic/pxlmono (recommended)', 'language': 'en'},
                  {'ppd': 'd', 'name': 'Samsung M2020 Series', 'language': 'en'}]
    ranked = [d['ppd'] for d in cupsadmin.rank_drivers(candidates, 'Samsung M2885FW')]
    assert ranked[:2] == ['c', 'b']  # The family "M288x" covers the M2885FW; recommended first
    assert 'a' not in ranked
    assert len(cupsadmin.rank_drivers(candidates, '')) == 4


def test_printer_names_never_contain_unsafe_characters():
    assert cupsadmin.suggested_name('Samsung M2020 Series (USB)') == 'Samsung_M2020_Series_USB'
    assert cupsadmin.suggested_name('***') == 'Printer'


# --- Stored logins -------------------------------------------------------------------

def test_admin_login_is_verified_stored_privately_and_never_echoed(tmp_path):
    fake = FakeCups()
    try:
        with pytest.raises(ipp.AuthRequired):
            cupsadmin.connect_admin(tmp_path, fake.server, 'cupsadmin', 'wrong')
        assert not cupsadmin.store_path(tmp_path).exists()
        status = cupsadmin.connect_admin(tmp_path, fake.server, 'cupsadmin', 'cups-secret')
        assert status == {'connected': True, 'username': 'cupsadmin', 'encrypted': False}
        assert oct(cupsadmin.store_path(tmp_path).stat().st_mode & 0o777) == '0o600'
        assert 'cups-secret' not in str(cupsadmin.admin_status(tmp_path, fake.server))
        cupsadmin.disconnect_admin(tmp_path, fake.server)
        assert cupsadmin.admin_status(tmp_path, fake.server)['connected'] is False
    finally:
        fake.close()


def test_servers_that_require_encryption_are_used_over_tls_with_a_pinned_certificate(tmp_path):
    first = self_signed_certificate(tmp_path, 'first')
    if not first:
        pytest.skip('openssl is not available')
    fake = FakeCups(require_tls=True, certificate=first)
    try:
        status = cupsadmin.connect_admin(tmp_path, fake.server, 'cupsadmin', 'cups-secret')
        assert status['encrypted'] is True
        conn = cupsadmin.connection(tmp_path, fake.server, admin=True)
        assert len(cupsadmin.discover(conn)) == 1
    finally:
        fake.close()
    # Same address, different certificate: refuse before sending the password.
    other = FakeCups(require_tls=True, certificate=self_signed_certificate(tmp_path, 'second'))
    try:
        logins = cupsadmin._load(tmp_path)
        logins[other.server] = logins.pop(fake.server)
        cupsadmin._save(tmp_path, logins)
        with pytest.raises(ipp.CertificateChanged):
            cupsadmin.discover(cupsadmin.connection(tmp_path, other.server, admin=True))
        assert other.requests == []  # Nothing, including the login, reached the impostor
    finally:
        other.close()


# --- HTTP API ------------------------------------------------------------------------

def login(username, password):
    client = TestClient(web.app)
    signed = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert signed.status_code == 200, signed.text
    client.headers['x-csrf-token'] = signed.json()['csrf']
    return client


@pytest.fixture
def cups(tmp_path, monkeypatch):
    fake = FakeCups()
    fake.printers['Existing'] = {'state': 3, 'reasons': [], 'ppd-name': 'everywhere', 'device-uri': 'ipp://10.0.0.5/ipp/print'}
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setattr(web, 'CUPS_SERVERS', [fake.server])
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    # Queue listing still comes from lpstat; mirror the fake server's queues.
    monkeypatch.setattr(devices, 'list_cups_queues', lambda server: [
        {'queue': name, 'ready': entry['state'] != 5, 'status': 'idle'} for name, entry in fake.printers.items()])
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    monkeypatch.setattr(devices, 'cups_capabilities', lambda *a: {
        'known': False, 'media': [], 'duplex': None, 'color': None, 'color_modes': ['auto', 'color', 'gray', 'bw']})
    printed = []

    def fake_print(path, title, **choices):
        with fitz.open(path) as doc:
            printed.append({'title': title, 'text': doc[0].get_text(), **choices})
        return 'request id is Existing-9 (1 file(s))'
    monkeypatch.setattr(web, 'print_with_cups', fake_print)
    security._guest_submissions.clear()
    security._global_submissions.clear()
    with TestClient(web.app):
        admin = login('admin', PASSWORD)
        yield fake, admin, printed
        tasks.runner.start(tmp_path / 'tasks')
    fake.close()


def server_path(fake):
    return f'/api/admin/cups/servers/{quote(fake.server)}'


def connect(fake, admin):
    response = admin.put(f'{server_path(fake)}/login', json={'username': 'cupsadmin', 'password': 'cups-secret'})
    assert response.status_code == 200, response.text
    return response.json()


def follow(client, task, timeout=10):
    deadline = time.monotonic() + timeout
    while task['status'] not in tasks.FINISHED:
        assert time.monotonic() < deadline, task
        time.sleep(.02)
        task = client.get(f"/api/tasks/{task['id']}").json()
    return task


def test_connecting_reports_status_without_the_password(cups):
    fake, admin, _ = cups
    assert admin.put(f'{server_path(fake)}/login', json={'username': 'cupsadmin', 'password': 'nope'}).status_code == 403
    status = connect(fake, admin)
    listing = admin.get('/api/admin/cups/servers').json()
    assert status['connected'] and listing[0]['admin']['username'] == 'cupsadmin'
    assert 'cups-secret' not in str(listing)
    assert admin.get('/api/admin/cups/servers/not-configured:631/devices').status_code == 404


def test_only_administrators_can_manage_printers(cups):
    fake, admin, _ = cups
    admin.post('/api/admin/users', json={'username': 'member1', 'password': 'member-long-password', 'role': 'member'})
    member = login('member1', 'member-long-password')
    assert member.get('/api/admin/cups/servers').status_code == 403
    assert member.put(f'{server_path(fake)}/login', json={'username': 'x', 'password': 'y'}).status_code == 403
    assert member.post(f'{server_path(fake)}/printers', json={}).status_code == 403


def test_discovery_requires_a_login_and_lists_real_devices_only(cups):
    fake, admin, _ = cups
    assert admin.get(f'{server_path(fake)}/devices').status_code == 409
    connect(fake, admin)
    found = admin.get(f'{server_path(fake)}/devices').json()['devices']
    assert [d['uri'] for d in found] == ['usb://Samsung/M2020%20Series?serial=Z7']  # Bare "socket" template dropped
    assert found[0]['connection'] == 'USB' and found[0]['make_model'] == 'Samsung M2020 Series'


def test_driver_lists_come_from_cups_and_are_ranked(cups):
    fake, admin, _ = cups
    assert admin.get(f'{server_path(fake)}/makes').json()['makes'] == ['Generic', 'Samsung']
    drivers = admin.get(f'{server_path(fake)}/drivers', params={'make': 'Samsung', 'model': 'M2885FW'}).json()
    assert drivers['total'] == 3
    assert drivers['drivers'][0]['name'] == 'Samsung M288x Foomatic/pxlmono (recommended)'
    assert admin.get(f'{server_path(fake)}/drivers', params={'make': 'Nobody'}).json()['drivers'] == []


def test_adding_a_network_printer_by_address(cups):
    fake, admin, _ = cups
    connect(fake, admin)
    started = admin.post(f'{server_path(fake)}/printers', json={
        'name': 'Hallway', 'connection': 'ipp', 'address': '192.0.2.5', 'driver': 'everywhere',
        'location': 'Upstairs', 'make_default': True, 'guest': True})
    assert started.status_code == 202, started.text
    task = follow(admin, started.json()['task'])
    assert task['status'] == 'done', task
    queue = fake.printers['Hallway']
    assert queue['device-uri'] == 'ipp://192.0.2.5/ipp/print' and queue['ppd-name'] == 'everywhere'
    assert queue['printer-is-shared'] is True and queue['printer-location'] == 'Upstairs'
    key = f'{fake.server}|Hallway'
    config = web.registry()
    assert config['default_printer'] == key and config['printers'][key]['guest'] is True


def test_adding_a_detected_printer_with_a_chosen_driver(cups):
    fake, admin, _ = cups
    connect(fake, admin)
    uri = 'usb://Samsung/M2020%20Series?serial=Z7'
    body = {'name': 'Study', 'device_uri': uri, 'driver': 'foomatic:Samsung-M288x-pxlmono.ppd'}
    assert admin.post(f'{server_path(fake)}/printers', json=body).status_code == 400  # Not discovered yet
    admin.get(f'{server_path(fake)}/devices')
    task = follow(admin, admin.post(f'{server_path(fake)}/printers', json=body).json()['task'])
    assert task['status'] == 'done' and fake.printers['Study']['device-uri'] == uri


def test_existing_queues_are_never_overwritten(cups):
    fake, admin, _ = cups
    connect(fake, admin)
    clash = admin.post(f'{server_path(fake)}/printers', json={
        'name': 'existing', 'connection': 'socket', 'address': '192.0.2.9', 'driver': 'everywhere'})
    assert clash.status_code == 409 and fake.printers['Existing']['device-uri'] == 'ipp://10.0.0.5/ipp/print'


def test_invalid_printer_details_are_rejected(cups):
    fake, admin, _ = cups
    connect(fake, admin)
    for body in ({'name': 'Two words', 'connection': 'ipp', 'address': '192.0.2.5', 'driver': 'everywhere'},
                 {'name': 'Ok', 'connection': 'ipp', 'address': 'bad host', 'driver': 'everywhere'},
                 {'name': 'Ok', 'driver': 'everywhere'},
                 {'name': 'Ok', 'device_uri': 'file:///etc/passwd', 'driver': 'everywhere'}):
        assert admin.post(f'{server_path(fake)}/printers', json=body).status_code == 400, body
    assert set(fake.printers) == {'Existing'}


def test_pause_resume_and_remove_a_printer(cups):
    fake, admin, _ = cups
    connect(fake, admin)
    key = quote(f'{fake.server}|Existing', safe='')
    admin.patch(f'/api/admin/printers/{key}', json={'make_default': True})
    paused = admin.post(f'/api/admin/printers/{key}/pause', json={'paused': True}).json()
    assert paused['paused'] is True and paused['state'] == 'stopped'
    assert admin.post(f'/api/admin/printers/{key}/pause', json={'paused': False}).json()['paused'] is False
    assert admin.delete(f'/api/admin/printers/{key}').status_code == 200
    assert 'Existing' not in fake.printers and web.registry()['default_printer'] is None


def test_test_page_prints_through_the_normal_print_path(cups):
    fake, admin, printed = cups
    key = quote(f'{fake.server}|Existing', safe='')
    receipt = admin.post(f'/api/admin/printers/{key}/test-page').json()
    assert receipt['job_id'] == 'Existing-9'
    assert printed[-1]['title'] == 'Folio test page' and 'Folio test page' in printed[-1]['text']
    assert printed[-1]['user'] == 'admin'


def test_print_jobs_are_submitted_under_the_folio_user(cups, monkeypatch, tmp_path):
    fake, admin, printed = cups
    document = fitz.open(); document.new_page()
    token = admin.post('/api/print/inspect', headers={'X-Upload-Filename': 'a.pdf'}, content=document.tobytes()).json()['token']
    assert admin.post('/api/print/submit-preview', json={'token': token}).status_code == 200
    assert printed[-1]['user'] == 'admin'
    # The real print_with_cups turns that into lp's -U option.
    commands = []
    monkeypatch.setattr(web.subprocess, 'run', lambda command, **kw: commands.append(command) or
                        subprocess.CompletedProcess(command, 0, 'request id is Existing-1', ''))
    source = tmp_path / 'p.pdf'
    document.save(source)
    REAL_PRINT_WITH_CUPS(source, 'Report', queue=f'{fake.server}|Existing', user='member1')
    assert commands[-1][commands[-1].index('-U') + 1] == 'member1'
    REAL_PRINT_WITH_CUPS(source, 'Report', queue=f'{fake.server}|Existing')
    assert '-U' not in commands[-1]  # Direct callers without a user keep the old behaviour


def test_guest_jobs_use_a_shared_guest_identity(cups):
    fake, admin, printed = cups
    admin.patch(f"/api/admin/printers/{quote(f'{fake.server}|Existing', safe='')}", json={'guest': True, 'make_default': True})
    admin.post('/api/admin/guest', json={'enabled': True})
    guest = TestClient(web.app)
    csrf = guest.get('/api/guest/status').json()['csrf']
    stream = fitz.open(); stream.new_page()
    token = guest.post('/api/guest/inspect', headers={'X-Upload-Filename': 'g.pdf', 'X-Guest-CSRF': csrf},
                       content=stream.tobytes()).json()['token']
    assert guest.post('/api/guest/submit-preview', json={'token': token}, headers={'X-Guest-CSRF': csrf}).status_code == 200
    assert printed[-1]['user'] == cupsadmin.GUEST_USER


def test_people_see_and_cancel_only_their_own_jobs(cups):
    fake, admin, _ = cups
    admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
    alice = login('alice', 'alice-long-password')
    mine = fake.add_job('Existing', 'alice', 'Tax return')
    theirs = fake.add_job('Existing', 'bob', 'Bob private')
    fake.add_job('Existing', 'alice', 'Old letter', state=9)
    listing = alice.get('/api/print/jobs').json()
    assert [(j['title'], j['label']) for j in listing['jobs']] == [('Tax return', 'Waiting'), ('Old letter', 'Done')]
    assert 'Bob private' not in str(listing)
    assert alice.post('/api/print/jobs/cancel', json={'server': fake.server, 'job_id': theirs}).status_code == 404
    assert fake.jobs[theirs]['state'] == 3
    assert alice.post('/api/print/jobs/cancel', json={'server': fake.server, 'job_id': mine}).status_code == 200
    assert fake.jobs[mine]['state'] == 7


def test_cancelling_falls_back_to_the_admin_login_when_cups_requires_it(cups):
    fake, admin, _ = cups
    fake.cancel_needs_auth = True
    admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
    alice = login('alice', 'alice-long-password')
    job = fake.add_job('Existing', 'alice', 'Stuck job')
    refused = alice.post('/api/print/jobs/cancel', json={'server': fake.server, 'job_id': job})
    assert refused.status_code == 403 and 'administrator' in refused.json()['detail']
    connect(fake, admin)
    assert alice.post('/api/print/jobs/cancel', json={'server': fake.server, 'job_id': job}).status_code == 200
    assert fake.jobs[job]['state'] == 7


def test_admins_see_everyones_jobs_on_a_printer(cups):
    fake, admin, _ = cups
    fake.add_job('Existing', 'alice', 'Tax return')
    fake.add_job('Existing', 'bob', 'Poster')
    key = quote(f'{fake.server}|Existing', safe='')
    hidden = admin.get(f'/api/admin/printers/{key}/jobs').json()
    assert hidden['details_hidden'] is True and {j['title'] for j in hidden['jobs']} == {''}
    connect(fake, admin)
    shown = admin.get(f'/api/admin/printers/{key}/jobs').json()
    assert {j['user'] for j in shown['jobs']} == {'alice', 'bob'}
    job = shown['jobs'][0]
    assert admin.post('/api/admin/jobs/cancel', json={'server': fake.server, 'job_id': job['id']}).status_code == 200


def test_an_unreachable_server_does_not_hold_up_the_job_list(cups, monkeypatch):
    fake, admin, _ = cups
    monkeypatch.setattr(web, 'CUPS_SERVERS', [fake.server, '127.0.0.1:9'])  # Nothing listens on port 9
    fake.add_job('Existing', 'admin', 'Report')
    started = time.monotonic()
    listing = admin.get('/api/print/jobs').json()
    assert time.monotonic() - started < 8
    assert listing['unavailable'] == ['127.0.0.1:9'] and listing['jobs'][0]['title'] == 'Report'
    assert devices.cups_backoff('127.0.0.1:9') > 0


def test_printer_only_accounts_can_follow_their_jobs(cups):
    fake, admin, _ = cups
    admin.post('/api/admin/users', json={'username': 'frontdesk', 'password': 'printer-long-password', 'role': 'printer'})
    desk = login('frontdesk', 'printer-long-password')
    fake.add_job('Existing', 'frontdesk', 'Badge')
    assert desk.get('/api/print/jobs').json()['jobs'][0]['title'] == 'Badge'
    assert desk.get('/api/admin/cups/servers').status_code == 403
