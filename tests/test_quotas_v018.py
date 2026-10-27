"""v0.18 print quotas: pages per person per month, counted from the print log."""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import app as web, cupsadmin, devices, diagnostics, security, stats, tasks  # noqa: E402

PASSWORD = 'quota-admin-long-password'


def login(username, password):
    client = TestClient(web.app)
    signed = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert signed.status_code == 200, signed.text
    client.headers['x-csrf-token'] = signed.json()['csrf']
    return client


@pytest.fixture
def office(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setattr(web, 'CUPS_SERVERS', ['cups.test:631'])
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    monkeypatch.setattr(devices, 'list_cups_queues', lambda server: [{'queue': 'Office', 'ready': True, 'status': 'idle'}])
    monkeypatch.setattr(devices, 'cups_capabilities', lambda *a: {
        'known': True, 'media': [], 'duplex': True, 'color': False, 'color_modes': ['auto', 'gray']})
    sent = []

    def fake_print(path, title, **choices):
        sent.append(choices)
        return f'request id is Office-{len(sent)} (1 file(s))'
    monkeypatch.setattr(web, 'print_with_cups', fake_print)
    security._guest_submissions.clear()
    security._global_submissions.clear()
    with TestClient(web.app):
        admin = login('admin', PASSWORD)
        admin.patch('/api/admin/printers/cups.test%3A631%7COffice', json={'make_default': True, 'guest': True})
        admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
        yield admin, login('alice', 'alice-long-password'), sent
        tasks.runner.start(tmp_path / 'tasks')


def pdf(pages: int) -> bytes:
    document = fitz.open()
    for _ in range(pages):
        document.new_page()
    return document.tobytes()


def print_pages(client, pages, copies=1, background=False):
    token = client.post('/api/print/inspect', headers={'X-Upload-Filename': 'a.pdf'}, content=pdf(pages)).json()['token']
    return client.post(f'/api/print/submit-preview{"?background=true" if background else ""}', json={'token': token},
                       headers={'X-Print-Copies': str(copies)})


def test_without_limits_everyone_prints_freely(office):
    admin, alice, sent = office
    assert print_pages(alice, 3).status_code == 200
    assert alice.get('/api/print/quota').json() == {'limit': None, 'used': 3, 'left': None}


def test_a_monthly_limit_stops_a_print_before_it_reaches_the_printer(office):
    admin, alice, sent = office
    print_pages(alice, 3)
    assert admin.put('/api/admin/quota-default', json={'monthly_pages': 5}).json()['monthly_pages'] == 5
    assert alice.get('/api/print/quota').json() == {'limit': 5, 'used': 3, 'left': 2}
    refused = print_pages(alice, 2, copies=2)  # 4 pages, 2 left
    assert refused.status_code == 403
    assert refused.json()['detail'] == ('This would print 4 pages, but you have 2 of your 5 pages left this month. '
                                       'Ask an administrator for more.')
    assert len(sent) == 1  # Nothing reached CUPS
    assert print_pages(alice, 2).status_code == 200
    assert alice.get('/api/print/quota').json()['left'] == 0
    assert print_pages(alice, 1).status_code == 403
    # Administrators have no limit, and guests keep their own rules.
    assert print_pages(admin, 9).status_code == 200
    admin.post('/api/admin/guest', json={'enabled': True})
    guest = TestClient(web.app)
    csrf = guest.get('/api/guest/status').json()['csrf']
    token = guest.post('/api/guest/inspect', headers={'X-Upload-Filename': 'g.pdf', 'X-Guest-CSRF': csrf},
                       content=pdf(1)).json()['token']
    assert guest.post('/api/guest/submit-preview', json={'token': token}, headers={'X-Guest-CSRF': csrf}).status_code == 200
    assert sent[-1]['user'] == cupsadmin.GUEST_USER


def test_each_person_can_have_their_own_limit(office):
    admin, alice, _ = office
    admin.put('/api/admin/quota-default', json={'monthly_pages': 1})
    assert admin.put('/api/admin/users/alice/quota', json={'monthly_pages': -1}).json()['limit'] is None
    assert print_pages(alice, 4).status_code == 200  # No limit for her
    assert admin.put('/api/admin/users/alice/quota', json={'monthly_pages': 10}).json() == \
        {'limit': 10, 'used': 4, 'left': 6}
    people = {u['username']: u for u in admin.get('/api/admin/users').json()}
    assert people['alice']['monthly_pages'] == 10 and people['alice']['quota']['used'] == 4
    assert people['admin']['quota']['limit'] is None
    assert admin.put('/api/admin/users/alice/quota', json={'monthly_pages': None}).json()['limit'] == 1  # The default
    assert admin.put('/api/admin/users/alice/quota', json={'monthly_pages': -2}).status_code == 422
    assert admin.put('/api/admin/users/nobody/quota', json={'monthly_pages': 3}).status_code == 400


def test_a_background_print_over_the_limit_explains_why(office):
    admin, alice, _ = office
    admin.put('/api/admin/quota-default', json={'monthly_pages': 2})
    task = print_pages(alice, 3, background=True).json()['task']
    deadline = time.monotonic() + 15
    while task['status'] not in tasks.FINISHED:
        assert time.monotonic() < deadline
        time.sleep(.05)
        task = alice.get(f"/api/tasks/{task['id']}").json()
    assert task['status'] == 'failed' and 'you have 2 of your 2 pages left' in task['error']


def test_only_this_months_folio_prints_count(office):
    admin, alice, _ = office
    now = datetime.now()
    last_month = datetime(now.year - (now.month == 1), (now.month - 2) % 12 + 1, 15).timestamp()
    stats.record(web.DATA_DIR, user='alice', server='cups.test:631', queue='Office', origin='upload', pages=50,
                 when=last_month)
    stats.record(web.DATA_DIR, user='alice', server='cups.test:631', queue='Office', origin='cups', pages=7)
    stats.record(web.DATA_DIR, user='alice', server='cups.test:631', queue='Office', origin='upload', pages=2)
    assert stats.pages_this_month(web.DATA_DIR, 'alice') == 2


def test_quotas_need_statistics_and_only_administrators_set_them(office, monkeypatch):
    admin, alice, _ = office
    assert alice.put('/api/admin/quota-default', json={'monthly_pages': 1}).status_code == 403
    assert alice.put('/api/admin/users/alice/quota', json={'monthly_pages': -1}).status_code == 403
    admin.put('/api/admin/quota-default', json={'monthly_pages': 100})
    admin.put('/api/admin/stats/settings', json={'enabled': False, 'retention_days': 365})
    assert admin.get('/api/admin/quota-default').json() == {'monthly_pages': 100, 'statistics_enabled': False}
    monkeypatch.setattr(diagnostics, 'probe_cups_server', lambda server: diagnostics.check(
        f'cups:{server}', f'CUPS {server}', 'ok', 'Reachable in 1 ms; 1 printer, 1 ready'))
    checks = {c['id']: c for c in admin.get('/api/admin/diagnostics').json()['sections']['printing']}
    assert checks['quotas']['status'] == 'warn' and 'statistics are switched off' in checks['quotas']['detail']
    assert admin.put('/api/admin/quota-default', json={'monthly_pages': -1}).json()['monthly_pages'] is None
    checks = {c['id']: c for c in admin.get('/api/admin/diagnostics').json()['sections']['printing']}
    assert 'quotas' not in checks  # No limits, nothing to say
    admin.post('/api/admin/users', json={'username': 'kiosk', 'password': 'kiosk-long-password', 'role': 'printer'})
    assert login('kiosk', 'kiosk-long-password').get('/api/print/quota').status_code == 200
