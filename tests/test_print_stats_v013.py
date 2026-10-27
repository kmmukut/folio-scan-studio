"""v0.13 print statistics: Folio's own log of what it sent to CUPS."""
from __future__ import annotations

import csv
import io
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import app as web, cupsadmin, devices, security, stats, tasks  # noqa: E402
from fake_cups import FakeCups  # noqa: E402

PASSWORD = 'stats-admin-long-password'


# --- The log itself ------------------------------------------------------------

@pytest.fixture
def log(tmp_path):
    stats.init(tmp_path)
    return tmp_path


def at(*args) -> float:
    return datetime(*args).timestamp()


def add(data_dir, when, user='alice', queue='Office', pages=1, **extra):
    return stats.record(data_dir, user=user, server='cups.lan:631', queue=queue, origin='upload',
                        pages=pages, when=when, **extra)


def test_sheets_count_both_sides_and_pages_per_sheet():
    assert stats.sheets_used(5, 1, 1, 'one-sided') == 5
    assert stats.sheets_used(5, 1, 1, 'two-sided-long-edge') == 3
    assert stats.sheets_used(5, 1, 2, 'two-sided-short-edge') == 2  # 3 sides of 2-up pages
    assert stats.sheets_used(5, 3, 2, 'one-sided') == 9
    assert stats.sheets_used(1, 2, 4, 'two-sided-long-edge') == 2  # Each copy starts a new sheet


def test_colour_is_what_was_asked_for_and_what_the_printer_can_do():
    assert stats.color_of('color', True) == 'colour'
    assert stats.color_of('gray', True) == stats.color_of('bw', None) == 'greyscale'
    assert stats.color_of('auto', False) == 'greyscale'  # A mono printer cannot print colour
    assert stats.color_of('auto', True) == 'auto'  # The printer decides


def test_totals_by_person_and_printer(log):
    now = at(2026, 10, 20, 12)
    add(log, at(2026, 10, 3, 9), pages=10, copies=2, sides='two-sided-long-edge', color='colour')
    add(log, at(2026, 10, 4, 9), user='bob', queue='Lab', pages=3, color='greyscale')
    add(log, at(2026, 10, 5, 9), user='folio-guest', pages=1)
    report = stats.summary(log, 'month', now)
    assert report['totals'] == {'jobs': 3, 'pages': 24, 'sheets': 14, 'sheets_saved': 10,
                                'colour_pages': 20, 'greyscale_pages': 3, 'auto_pages': 1, 'cancelled': 0}
    assert [p['user'] for p in report['people']] == ['alice', 'bob', 'folio-guest']  # Most paper first
    assert report['people'][0]['sheets'] == 10
    assert {(p['queue'], p['jobs']) for p in report['printers']} == {('Office', 2), ('Lab', 1)}


def test_periods_and_their_buckets(log):
    stats.save_settings(log, enabled=True, retention_days=0)  # Keep the 2025 job below
    now = at(2026, 10, 20, 12)
    add(log, at(2026, 10, 1, 0, 0, 5), pages=1)    # This month
    add(log, at(2026, 9, 30, 23, 59), pages=2)     # Last month, last minute
    add(log, at(2026, 1, 15), pages=4)             # This year
    add(log, at(2025, 11, 2), pages=8)             # Within 12 months
    add(log, at(2025, 3, 1), pages=16)             # Older
    pages = {period: stats.summary(log, period, now)['totals']['pages'] for period in stats.PERIODS}
    assert pages == {'30d': 3, 'month': 1, 'last-month': 2, 'year': 7, '12m': 15, 'all': 31}
    days = stats.summary(log, '30d', now)
    assert days['unit'] == 'day' and len(days['series']) == 30
    assert days['series'][0]['key'] == '2026-09-21' and days['series'][-1]['key'] == '2026-10-20'
    assert next(b for b in days['series'] if b['key'] == '2026-09-30')['pages'] == 2
    months = stats.summary(log, '12m', now)
    assert months['unit'] == 'month' and [b['key'] for b in months['series']][::11] == ['2025-11', '2026-10']
    assert sum(b['jobs'] for b in months['series']) == 4  # Empty months are listed too
    assert len(stats.summary(log, 'last-month', now)['series']) == 30  # September's days
    assert [b['key'] for b in stats.summary(log, 'all', now)['series']][0] == '2025-03'
    with pytest.raises(ValueError):
        stats.period_range('forever', now)


def test_retention_switching_off_and_clearing(log):
    now = time.time()
    add(log, now - 400 * 86400)
    add(log, now - 100 * 86400)
    assert stats.summary(log, 'all', now)['totals']['jobs'] == 1  # Past the 365-day default
    stats.save_settings(log, enabled=True, retention_days=90)
    assert stats.summary(log, 'all', now)['totals']['jobs'] == 0
    with pytest.raises(ValueError):
        stats.save_settings(log, enabled=True, retention_days=12)
    stats.save_settings(log, enabled=False, retention_days=0)
    assert add(log, now) is False and stats.summary(log, 'all', now)['totals']['jobs'] == 0
    stats.save_settings(log, enabled=True, retention_days=0)
    add(log, now - 5000 * 86400)
    add(log, now)
    assert stats.summary(log, 'all', now)['totals']['jobs'] == 2  # 0 keeps everything
    assert stats.clear(log) == 2


def test_cancelled_jobs_and_removed_accounts(log):
    now = time.time()
    add(log, now - 60, cups_job=11, pages=4)
    add(log, now - 30, cups_job=12, pages=2, user='bob')
    stats.mark_cancelled(log, 'cups.lan:631', 11)
    stats.mark_cancelled(log, 'other:631', 12)  # Same number, different server: not this job
    stats.forget_user(log, 'bob')
    report = stats.summary(log, 'all', now)
    assert report['totals']['pages'] == 2 and report['totals']['cancelled'] == 1
    assert [p['user'] for p in report['people']] == [stats.REMOVED_ACCOUNT]


def test_the_job_number_is_read_from_lp():
    assert web.cups_job_number('request id is Office-123 (1 file(s))') == 123
    assert web.cups_job_number('request id is Lab-2-15 (1 file(s))') == 15
    assert web.cups_job_number('request id is Lab-2-15') == 15
    assert web.cups_job_number('accepted') is None


# --- Through the API -----------------------------------------------------------

def login(username, password):
    client = TestClient(web.app)
    signed = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert signed.status_code == 200, signed.text
    client.headers['x-csrf-token'] = signed.json()['csrf']
    return client


@pytest.fixture
def api(tmp_path, monkeypatch):
    fake = FakeCups()
    fake.printers['Office'] = {'state': 3, 'reasons': []}
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setattr(web, 'CUPS_SERVERS', [fake.server])
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    monkeypatch.setattr(devices, 'list_cups_queues', lambda server: [{'queue': 'Office', 'ready': True, 'status': 'idle'}])
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    monkeypatch.setattr(devices, 'cups_capabilities', lambda *a: {
        'known': True, 'media': [], 'duplex': True, 'color': False, 'color_modes': ['auto', 'gray']})

    def fake_print(path, title, **choices):
        # CUPS keeps the job under the Folio user, as lp -U does.
        job = fake.add_job('Office', choices.get('user') or 'anonymous', title)
        return f'request id is Office-{job} (1 file(s))'
    monkeypatch.setattr(web, 'print_with_cups', fake_print)
    security._guest_submissions.clear()
    security._global_submissions.clear()
    with TestClient(web.app):
        admin = login('admin', PASSWORD)
        assert admin.patch(f"/api/admin/printers/{quote(f'{fake.server}|Office', safe='')}",
                           json={'make_default': True, 'guest': True}).status_code == 200
        yield fake, admin
        tasks.runner.start(tmp_path / 'tasks')
    fake.close()


def pdf(pages: int) -> bytes:
    document = fitz.open()
    for _ in range(pages):
        document.new_page()
    return document.tobytes()


def upload_and_print(client, pages=3, background=False, **headers):
    token = client.post('/api/print/inspect', headers={'X-Upload-Filename': 'a.pdf'}, content=pdf(pages)).json()['token']
    response = client.post(f'/api/print/submit-preview{"?background=true" if background else ""}',
                           json={'token': token}, headers=headers)
    assert response.status_code in (200, 202), response.text
    return response.json()


def follow(client, task, timeout=15):
    deadline = time.monotonic() + timeout
    while task['status'] not in tasks.FINISHED:
        assert time.monotonic() < deadline, task
        time.sleep(.02)
        task = client.get(f"/api/tasks/{task['id']}").json()
    return task


def document(client, pages=2) -> str:
    job = client.post('/api/jobs', json={'title': 'Private title'}).json()
    for n in range(pages):
        page_id = f'{n:016x}'
        Image.new('RGB', (425, 550), (250, 250, 245)).save(web.JOBS_DIR / job['id'] / 'pages' / f'{page_id}.png')
        job['pages'].append({'id': page_id, 'file': f'{page_id}.png', 'rotation': 0, 'export_mode': 'Original',
                             'crop': None, 'scale': 100, 'deskew': 0.0, 'scanned_mode': 'Color', 'dpi': 50,
                             'batch_id': 'b' * 12, 'created': web.now()})
    web.save_job(job)
    return job['id']


def test_every_way_of_printing_is_counted(api):
    fake, admin = api
    upload_and_print(admin, pages=3, **{'X-Print-Copies': '2', 'X-Print-Sides': 'two-sided-long-edge'})
    follow(admin, upload_and_print(admin, pages=1, background=True)['task'])
    job_id = document(admin, pages=2)
    assert admin.post(f'/api/jobs/{job_id}/print', headers={'X-Print-Pages': '2'}).status_code == 200
    assert admin.post(f"/api/admin/printers/{quote(f'{fake.server}|Office', safe='')}/test-page").status_code == 200
    report = admin.get('/api/admin/stats?period=30d').json()
    # 3 pages x 2 copies on 4 sheets; 1 page; page 2 of the document; the test page.
    totals = report['totals']
    assert (totals['jobs'], totals['pages'], totals['sheets'], totals['sheets_saved']) == (4, 9, 7, 2)
    assert totals['greyscale_pages'] == 9  # The printer reports no colour
    assert report['printers'] == [{**report['printers'][0], 'server': fake.server, 'queue': 'Office'}]
    exported = admin.get('/api/admin/stats/export?period=all')
    assert exported.status_code == 200 and exported.headers['content-type'].startswith('text/csv')
    assert 'attachment; filename="folio-print-log-all-' in exported.headers['content-disposition']
    rows = list(csv.DictReader(io.StringIO(exported.text)))
    assert [r['origin'] for r in rows] == ['upload', 'upload', 'document', 'test-page']
    assert [r['cups_job'] for r in rows] == ['1', '2', '3', '4']
    assert 'Private title' not in exported.text and 'a.pdf' not in exported.text  # Titles are never stored


def test_guests_are_counted_together(api):
    _, admin = api
    admin.post('/api/admin/guest', json={'enabled': True})
    guest = TestClient(web.app)
    csrf = guest.get('/api/guest/status').json()['csrf']
    token = guest.post('/api/guest/inspect', headers={'X-Upload-Filename': 'g.pdf', 'X-Guest-CSRF': csrf},
                       content=pdf(2)).json()['token']
    assert guest.post('/api/guest/submit-preview', json={'token': token}, headers={'X-Guest-CSRF': csrf}).status_code == 200
    report = admin.get('/api/admin/stats').json()
    assert report['guest_user'] == cupsadmin.GUEST_USER
    assert [(p['user'], p['pages']) for p in report['people']] == [(cupsadmin.GUEST_USER, 2)]


def test_a_job_cancelled_in_folio_no_longer_counts(api):
    fake, admin = api
    upload_and_print(admin, pages=5)
    upload_and_print(admin, pages=1)
    assert admin.post('/api/print/jobs/cancel', json={'server': fake.server, 'job_id': 1}).status_code == 200
    totals = admin.get('/api/admin/stats').json()['totals']
    assert (totals['jobs'], totals['pages'], totals['cancelled']) == (1, 1, 1)
    # An administrator cancelling someone's job (with the CUPS login connected) counts the same.
    assert admin.put(f'/api/admin/cups/servers/{quote(fake.server)}/login',
                     json={'username': 'cupsadmin', 'password': 'cups-secret'}).status_code == 200
    assert admin.post('/api/admin/jobs/cancel', json={'server': fake.server, 'job_id': 2}).status_code == 200
    totals = admin.get('/api/admin/stats').json()['totals']
    assert (totals['jobs'], totals['cancelled']) == (0, 2)


def test_statistics_never_stop_a_print(api, monkeypatch):
    _, admin = api

    def broken(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(stats, 'record', broken)
    receipt = upload_and_print(admin, pages=1)
    assert receipt['ok'] and receipt['job_id'] == 'Office-1'


def test_only_administrators_see_or_change_statistics(api):
    _, admin = api
    admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
    admin.post('/api/admin/users', json={'username': 'printer1', 'password': 'printer-long-password', 'role': 'printer'})
    for name, password in (('alice', 'alice-long-password'), ('printer1', 'printer-long-password')):
        other = login(name, password)
        assert other.get('/api/admin/stats').status_code == 403
        assert other.get('/api/admin/stats/export').status_code == 403
        assert other.put('/api/admin/stats/settings', json={'enabled': False, 'retention_days': 0}).status_code == 403
        assert other.delete('/api/admin/stats').status_code == 403
    assert TestClient(web.app).get('/api/admin/stats').status_code == 401


def test_settings_clearing_and_deleted_accounts(api):
    _, admin = api
    admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
    upload_and_print(login('alice', 'alice-long-password'), pages=2)
    assert admin.delete('/api/admin/users/alice').status_code == 200
    report = admin.get('/api/admin/stats').json()
    assert [(p['user'], p['pages']) for p in report['people']] == [(stats.REMOVED_ACCOUNT, 2)]
    assert report['settings'] == {'enabled': True, 'retention_days': 365}
    assert admin.put('/api/admin/stats/settings', json={'enabled': True, 'retention_days': 7}).status_code == 400
    assert admin.put('/api/admin/stats/settings', json={'enabled': False, 'retention_days': 730}).json() == \
        {'enabled': False, 'retention_days': 730}
    upload_and_print(admin, pages=4)
    assert admin.get('/api/admin/stats').json()['totals']['pages'] == 2  # Not recorded while off
    assert admin.delete('/api/admin/stats').json() == {'deleted': 1}
    assert admin.get('/api/admin/stats?period=all').json()['totals']['jobs'] == 0
    assert admin.get('/api/admin/stats?period=forever').status_code == 400
    assert admin.get('/api/admin/stats/export?period=x%22%0d%0aSet-Cookie:a=b').status_code == 400


def test_spreadsheet_cells_cannot_become_formulas(api):
    _, admin = api
    stats.record(web.DATA_DIR, user='alice', server='cups:631', queue='=HYPERLINK("x")', origin='upload', pages=1)
    rows = list(csv.reader(io.StringIO(admin.get('/api/admin/stats/export').text)))
    assert rows[0] == list(stats.CSV_COLUMNS)
    assert rows[1][2] == '\'=HYPERLINK("x")'


def test_months_follow_the_container_time_zone(log, monkeypatch):
    def zone(name):
        monkeypatch.setenv('TZ', name)
        time.tzset()
    try:
        zone('UTC')
        add(log, at(2026, 10, 31, 23, 30))
        later = at(2026, 11, 2)
        assert [b['key'] for b in stats.summary(log, 'year', later)['series'] if b['jobs']] == ['2026-10']
        zone('Asia/Tokyo')  # UTC+9: the same moment is already November
        assert [b['key'] for b in stats.summary(log, 'year', later)['series'] if b['jobs']] == ['2026-11']
    finally:
        monkeypatch.undo()
        time.tzset()


def test_diagnostics_report_the_print_log(log):
    from backend import diagnostics
    add(log, time.time())
    assert diagnostics.check_print_log(log)['detail'].startswith('1 jobs on record, kept for 365 days')
    stats.save_settings(log, enabled=False, retention_days=0)
    assert diagnostics.check_print_log(log)['detail'] == 'Switched off; 1 jobs on record'
