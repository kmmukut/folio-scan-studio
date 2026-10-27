"""v0.16 the first minutes: the administrator created in the browser, the
getting-started checklist, and backup and restore."""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import app as web, devices, printserver, security, stats, tasks  # noqa: E402
from fake_cups import FakeCups  # noqa: E402

PASSWORD = 'first-admin-long-password'


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A new installation started without SCAN_STUDIO_ADMIN_PASSWORD."""
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setattr(web, 'CUPS_SERVERS', [])
    monkeypatch.delenv('SCAN_STUDIO_ADMIN_PASSWORD', raising=False)
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    with TestClient(web.app) as client:
        yield client


def test_without_a_password_setting_the_first_visitor_creates_the_administrator(fresh):
    status = fresh.get('/api/auth/status').json()
    assert status['authenticated'] is False and status['setup_required'] is True and status['setup_open'] is True
    assert fresh.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD}).status_code == 401
    created = fresh.post('/api/setup/admin', json={'username': 'maria', 'password': PASSWORD})
    assert created.status_code == 200, created.text
    assert created.json()['role'] == 'admin'
    status = fresh.get('/api/auth/status').json()  # Signed in at once
    assert status['authenticated'] is True and status['username'] == 'maria' and status['setup_required'] is False
    fresh.headers['x-csrf-token'] = status['csrf']
    assert fresh.get('/api/admin/users').status_code == 200
    # Only once: afterwards it is an ordinary sign-in.
    stranger = TestClient(web.app)
    assert stranger.post('/api/setup/admin', json={'username': 'mallory', 'password': PASSWORD}).status_code == 409
    assert stranger.post('/api/auth/login', json={'username': 'maria', 'password': PASSWORD}).status_code == 200


def test_bad_details_are_refused(fresh):
    assert fresh.post('/api/setup/admin', json={'username': 'maria', 'password': 'too-short'}).status_code == 400
    assert fresh.post('/api/setup/admin', json={'username': '9lives', 'password': PASSWORD}).status_code == 400
    assert fresh.get('/api/auth/status').json()['setup_required'] is True


def test_the_chance_to_create_it_closes_after_fifteen_minutes(fresh, monkeypatch):
    monkeypatch.setattr(web, '_setup_opened', time.monotonic() - web.SETUP_WINDOW - 1)
    assert fresh.get('/api/auth/status').json()['setup_open'] is False
    refused = fresh.post('/api/setup/admin', json={'username': 'maria', 'password': PASSWORD})
    assert refused.status_code == 403 and 'Restart Folio' in refused.json()['detail']


def test_two_visitors_at_once_get_one_administrator(fresh):
    results = []

    def attempt(name):
        try:
            security.create_first_admin(web.DATA_DIR, name, PASSWORD)
            results.append(name)
        except PermissionError:
            results.append(None)
    threads = [threading.Thread(target=attempt, args=(f'admin{n}',)) for n in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for r in results if r) == 1 and len(security.list_users(web.DATA_DIR)) == 1


def test_the_password_setting_still_creates_the_administrator(tmp_path, monkeypatch):
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    security.init(tmp_path)
    assert [u['username'] for u in security.list_users(tmp_path)] == ['admin']
    assert security.setup_required(tmp_path) is False


def test_the_checklist_follows_what_is_set_up(fresh, monkeypatch):
    status = fresh.post('/api/setup/admin', json={'username': 'maria', 'password': PASSWORD}).json()
    fresh.headers['x-csrf-token'] = status['csrf']
    steps = {s['id']: s for s in fresh.get('/api/admin/checklist').json()['steps']}
    assert not any(steps[s]['done'] for s in ('printer', 'print', 'scanner', 'people', 'airprint', 'backup'))
    assert steps['people']['optional'] and not steps['printer'].get('optional')
    fresh.post('/api/admin/users', json={'username': 'alex', 'password': 'alex-long-password', 'role': 'member'})
    monkeypatch.setattr(web, 'printer_inventory', lambda: (web.registry(), [{'id': 'x|Office', 'queue': 'Office'}]))
    checklist = fresh.get('/api/admin/checklist').json()
    steps = {s['id']: s for s in checklist['steps']}
    assert steps['printer']['done'] and steps['people']['done'] and not checklist['complete']
    hidden = fresh.put('/api/admin/checklist', json={'dismissed': True}).json()
    assert hidden['dismissed'] is True and fresh.get('/api/admin/checklist').json()['dismissed'] is True


# --- Backup and restore ----------------------------------------------------------


def follow(client, task, timeout=30):
    deadline = time.monotonic() + timeout
    while task['status'] not in tasks.FINISHED:
        assert time.monotonic() < deadline, task
        time.sleep(.05)
        task = client.get(f"/api/tasks/{task['id']}").json()
    return task


def login(username, password):
    client = TestClient(web.app)
    signed = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert signed.status_code == 200, signed.text
    client.headers['x-csrf-token'] = signed.json()['csrf']
    return client


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A Folio in use: an admin, a member, a document, statistics, built-in printers."""
    fake = FakeCups(username=printserver.ADMIN_USER, password='folio-generated')
    fake.printers['Office'] = {'state': 3, 'reasons': [], 'device-uri': 'socket://192.0.2.20:9100',
                               'printer-info': 'Office laser', 'ppd-file': b'*PPD-Adobe: "4.3"\n*ModelName: "Office"\n'}
    monkeypatch.setenv('FOLIO_BUILTIN_CUPS', 'on')
    monkeypatch.setattr(printserver, 'SERVER', fake.server)
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setattr(web, 'CUPS_SERVERS', [])
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    (tmp_path / 'cups').mkdir()
    printserver.secret_path(tmp_path).write_text('folio-generated')
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    monkeypatch.setattr(devices, 'list_cups_queues', lambda server: [
        {'queue': name, 'ready': True, 'status': 'idle'} for name in fake.printers])
    with TestClient(web.app):
        admin = login('admin', PASSWORD)
        admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
        job = admin.post('/api/jobs', json={'title': 'Tax letter'}).json()
        Image.new('RGB', (40, 50), 'white').save(web.JOBS_DIR / job['id'] / 'pages' / 'p1.png')
        (web.JOBS_DIR / job['id'] / 'cache').mkdir()
        (web.JOBS_DIR / job['id'] / 'cache' / 'preview.jpg').write_bytes(b'regenerable')
        stats.record(tmp_path, user='alice', server=fake.server, queue='Office', origin='upload', pages=3)
        yield fake, admin, job
        tasks.runner.start(tmp_path / 'tasks')
    fake.close()


def take_backup(admin) -> bytes:
    started = admin.post('/api/admin/backup')
    assert started.status_code == 202, started.text
    task = follow(admin, started.json()['task'])
    assert task['status'] == 'done', task
    download = admin.get(f"/api/tasks/{task['id']}/file")
    assert download.status_code == 200 and download.headers['content-type'] == 'application/gzip'
    assert 'folio-backup-' in download.headers['content-disposition']
    return download.content


def test_a_backup_holds_everything_and_restoring_brings_it_back(installed):
    fake, admin, job = installed
    archive = take_backup(admin)
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile('folio-backup.json').read())
        tar.extract('data/access.sqlite3', web.DATA_DIR / 'peek', filter='data')
    assert manifest['folio_version'] == web.__version__ and manifest['documents'] == 1
    assert manifest['printers'] == ['Office']
    assert f"data/jobs/{job['id']}/pages/p1.png" in names and 'printers/Office.ppd' in names
    assert not any('/cache/' in name for name in names)  # Previews are regenerated
    peek = sqlite3.connect(web.DATA_DIR / 'peek' / 'data' / 'access.sqlite3')
    assert peek.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0  # No sign-ins in a backup
    peek.close()
    assert {s['id']: s for s in admin.get('/api/admin/checklist').json()['steps']}['backup']['done']

    # Things go wrong: a person, a document and a printer are lost.
    admin.delete('/api/admin/users/alice')
    admin.delete(f"/api/jobs/{job['id']}")
    del fake.printers['Office']
    restored = admin.post('/api/admin/restore', content=archive)
    assert restored.status_code == 202, restored.text
    assert restored.json()['manifest']['documents'] == 1
    result = follow(admin, restored.json()['task'])
    assert result['status'] == 'done', result
    assert result['result']['printers_restored'] == ['Office'] and result['result']['printers_failed'] == {}
    assert fake.printers['Office']['ppd-file'].startswith(b'*PPD-Adobe')  # Recreated with its own driver
    assert fake.printers['Office']['device-uri'] == 'socket://192.0.2.20:9100'
    assert admin.get('/api/auth/status').json()['authenticated'] is True  # Still signed in
    assert 'alice' in {u['username'] for u in admin.get('/api/admin/users').json()}
    assert admin.get(f"/api/jobs/{job['id']}").json()['title'] == 'Tax letter'
    assert admin.get('/api/admin/stats').json()['totals']['pages'] == 3
    assert (web.DATA_DIR / '.before-restore' / 'access.sqlite3').exists()  # The replaced data is kept aside
    assert login('alice', 'alice-long-password')  # The restored account works


def make_archive(members: dict[str, bytes], manifest: dict | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        items = dict(members)
        if manifest is not None:
            items = {'folio-backup.json': json.dumps(manifest).encode(), **items}
        for name, content in items.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_only_real_compatible_backups_are_restored(installed):
    _, admin, job = installed
    good = {'format': 1, 'folio_version': '0.9.6', 'edition': 'full', 'documents': 0, 'printers': []}
    cases = {
        'not a backup at all': b'just some text',
        'no manifest': make_archive({'data/devices.json': b'{}'}),
        'from a newer Folio': make_archive({}, {**good, 'folio_version': '9.0.0'}),
        'reaches outside': make_archive({'data/../../etc/evil': b'x'}, good),
        'unknown part': make_archive({'secrets/key': b'x'}, good),
    }
    for label, archive in cases.items():
        refused = admin.post('/api/admin/restore', content=archive)
        assert refused.status_code == 400, (label, refused.text)
    assert admin.get(f"/api/jobs/{job['id']}").status_code == 200  # Nothing was touched
    assert not (web.DATA_DIR / '.restore-upload.tar.gz').exists()


def test_only_administrators_back_up_or_restore(installed):
    _, admin, _ = installed
    alice = login('alice', 'alice-long-password')
    assert alice.post('/api/admin/backup').status_code == 403
    assert alice.post('/api/admin/restore', content=b'x').status_code == 403
    assert TestClient(web.app).post('/api/admin/backup').status_code in (401, 403)


def test_a_print_server_without_printers_backs_up_fine(installed):
    fake, admin, _ = installed
    fake.printers.clear()  # CUPS answers "No destinations added" then
    with tarfile.open(fileobj=io.BytesIO(take_backup(admin)), mode='r:gz') as tar:
        manifest = json.loads(tar.extractfile('folio-backup.json').read())
    assert manifest['printers'] == [] and manifest['documents'] == 1


def test_restoring_a_backup_without_documents_leaves_a_working_library(installed):
    _, admin, job = installed
    admin.delete(f"/api/jobs/{job['id']}")
    empty = take_backup(admin)
    restored = admin.post('/api/admin/restore', content=empty)
    assert follow(admin, restored.json()['task'])['status'] == 'done'
    assert web.JOBS_DIR.is_dir()
    assert admin.post('/api/jobs', json={'title': 'After the restore'}).status_code == 201
