"""v0.21: a password link works exactly once, and each person has a limit on
background jobs (exports, prints, sends) waiting or running at once."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import app as web, devices, security, tasks  # noqa: E402


def test_a_link_works_once_even_when_used_twice_at_the_same_moment(tmp_path):
    security.init(tmp_path)
    security.add_user(tmp_path, 'alice', 'alice-old-password', 'member', 'alice@example.com')
    token = security.create_link(tmp_path, 'alice', 'reset')
    start, results = threading.Barrier(2), {}

    def use(password):
        start.wait()  # Both requests arrive together
        try:
            security.use_link(tmp_path, token, password)
            results[password] = 'used'
        except PermissionError:
            results[password] = 'refused'
    threads = [threading.Thread(target=use, args=(password,)) for password in ('first-new-password', 'second-new-password')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results.values()) == ['refused', 'used']
    winner = next(password for password, outcome in results.items() if outcome == 'used')
    loser = next(password for password, outcome in results.items() if outcome == 'refused')
    assert security.try_login(tmp_path, 'alice', winner, 'test-a')[0]
    assert security.try_login(tmp_path, 'alice', loser, 'test-b')[1] == 'Invalid username or password'


def test_a_link_for_a_disabled_or_removed_account_does_nothing(tmp_path):
    security.init(tmp_path)
    security.add_user(tmp_path, 'bob', 'bob-old-long-password', 'member')
    security.add_user(tmp_path, 'root-admin', 'admin-long-password', 'admin')
    token = security.create_link(tmp_path, 'bob', 'reset')
    security.edit_user(tmp_path, 'bob', 'root-admin', enabled=False)
    with pytest.raises(PermissionError):
        security.use_link(tmp_path, token, 'bob-new-long-password')
    with pytest.raises(PermissionError):
        security.use_link(tmp_path, 'x' * 101, 'bob-new-long-password')
    with pytest.raises(ValueError):
        security.use_link(tmp_path, token, 'short')


# --- Jobs at once, per person -------------------------------------------------------

PASSWORD = 'jobs-admin-long-password'


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
    printer_free = threading.Event()  # The stand-in printer holds every job until this is set

    def slow_print(path, title, **choices):
        printer_free.wait(20)
        return 'request id is Office-1 (1 file(s))'
    monkeypatch.setattr(web, 'print_with_cups', slow_print)
    security._guest_submissions.clear()
    security._global_submissions.clear()
    with TestClient(web.app):
        admin = login('admin', PASSWORD)
        admin.patch('/api/admin/printers/cups.test%3A631%7COffice', json={'make_default': True})
        admin.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
        yield admin, login('alice', 'alice-long-password'), printer_free
        printer_free.set()
        tasks.runner.start(tmp_path / 'tasks')


def background_print(client):
    document = fitz.open()
    document.new_page()
    token = client.post('/api/print/inspect', headers={'X-Upload-Filename': 'a.pdf'}, content=document.tobytes()).json()['token']
    return client.post('/api/print/submit-preview?background=true', json={'token': token})


def test_each_person_has_a_limit_on_jobs_at_once(office):
    admin, alice, printer_free = office
    assert admin.get('/api/admin/job-limit-default').json() == {'job_limit': 5}
    assert admin.put('/api/admin/job-limit-default', json={'job_limit': 2}).json() == {'job_limit': 2}
    assert [background_print(alice).status_code for _ in range(2)] == [202, 202]
    refused = background_print(alice)
    assert refused.status_code == 429
    assert refused.json()['detail'] == ('You already have 2 jobs waiting or running (your limit is 2). '
                                        'Wait for one to finish, or cancel one.')
    # Administrators have no limit of their own.
    assert all(background_print(admin).status_code == 202 for _ in range(3))
    # Her own number, then no limit at all.
    assert admin.put('/api/admin/users/alice/job-limit', json={'job_limit': 3}).json() == {'limit': 3}
    assert background_print(alice).status_code == 202
    assert background_print(alice).status_code == 429
    assert admin.put('/api/admin/users/alice/job-limit', json={'job_limit': -1}).json() == {'limit': None}
    assert background_print(alice).status_code == 202
    people = {p['username']: p for p in admin.get('/api/admin/users').json()}
    assert people['alice']['job_limit'] == -1 and people['alice']['jobs'] == {'limit': None}
    assert people['admin']['jobs'] == {'limit': None}
    # Back to the default; once her jobs are done she can print again.
    assert admin.put('/api/admin/users/alice/job-limit', json={'job_limit': None}).json() == {'limit': 2}
    printer_free.set()
    deadline = time.monotonic() + 20
    while background_print(alice).status_code != 202:
        assert time.monotonic() < deadline
        time.sleep(.1)


def test_job_limits_are_checked_and_only_administrators_set_them(office):
    admin, alice, _ = office
    assert admin.put('/api/admin/job-limit-default', json={'job_limit': 0}).status_code == 400
    assert admin.put('/api/admin/job-limit-default', json={'job_limit': None}).status_code == 400
    assert admin.put('/api/admin/users/alice/job-limit', json={'job_limit': 0}).status_code == 400
    assert admin.put('/api/admin/users/alice/job-limit', json={'job_limit': -2}).status_code == 422
    assert admin.put('/api/admin/users/alice/job-limit', json={'job_limit': 101}).status_code == 422
    assert admin.put('/api/admin/users/nobody/job-limit', json={'job_limit': 3}).status_code == 400
    assert alice.put('/api/admin/job-limit-default', json={'job_limit': 50}).status_code == 403
    assert alice.put('/api/admin/users/alice/job-limit', json={'job_limit': -1}).status_code == 403


@pytest.fixture
def runner(tmp_path):
    runner = tasks.TaskRunner()
    runner.start(tmp_path / 'tasks')
    return runner


def test_two_requests_cannot_both_take_the_last_place(runner):
    hold, start, outcomes = threading.Event(), threading.Barrier(2), []

    def submit():
        start.wait()
        try:
            runner.submit('export', 'export', 'user:alice', lambda task: hold.wait(10),
                          prepare=lambda folder: time.sleep(.3), limit=1)
            outcomes.append('accepted')
        except tasks.Busy:
            outcomes.append('busy')
    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    hold.set()
    assert sorted(outcomes) == ['accepted', 'busy']


def test_everyone_together_has_a_ceiling_and_a_failed_start_frees_its_place(runner, monkeypatch):
    monkeypatch.setattr(tasks, 'MAX_ACTIVE', 3)
    hold = threading.Event()
    for n in range(3):
        runner.submit('print', 'print', f'user:person{n}', lambda task: hold.wait(10))
    with pytest.raises(tasks.Busy, match='busy with many jobs'):
        runner.submit('print', 'print', 'user:another', lambda task: None)
    hold.set()
    deadline = time.monotonic() + 10
    while any(t.status not in tasks.FINISHED for t in runner._tasks.values()):
        assert time.monotonic() < deadline
        time.sleep(.02)

    def broken(folder):
        raise OSError('disk full')
    with pytest.raises(OSError):
        runner.submit('export', 'export', 'user:alice', lambda task: None, prepare=broken, limit=1)
    runner.submit('export', 'export', 'user:alice', lambda task: None, limit=1)  # Its place was freed


def test_one_busy_person_cannot_push_out_someone_elses_finished_download(runner):
    def finished(owner):
        task = runner.submit('export', 'export', owner, lambda task: None)
        deadline = time.monotonic() + 10
        while task.status not in tasks.FINISHED:
            assert time.monotonic() < deadline
            time.sleep(.01)
        return task
    bobs = finished('user:bob')
    for _ in range(tasks.MAX_FINISHED_PER_OWNER + 5):
        finished('user:alice')
    runner.prune()
    owners = [t.owner for t in runner._tasks.values()]
    assert owners.count('user:alice') == tasks.MAX_FINISHED_PER_OWNER
    assert runner.get(bobs.id, 'user:bob') is bobs and bobs.directory.exists()
