"""v0.10 background jobs: exports, OCR and printing outlive no proxy timeout."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pymupdf as fitz
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import app as web, devices, security, tasks  # noqa: E402

PASSWORD = 'jobs-admin-long-password'


# --- Task runner ---------------------------------------------------------------

def wait_for(task: tasks.Task, timeout: float = 10) -> tasks.Task:
    deadline = time.monotonic() + timeout
    while task.status not in tasks.FINISHED:
        assert time.monotonic() < deadline, f'Task stuck in {task.status}: {task.message}'
        time.sleep(.01)
    return task


def blocker(release: threading.Event):
    """Work that occupies a lane until released (or the runner restarts)."""
    def work(task):
        while not release.wait(.02):
            task.check_cancelled()
    return work


@pytest.fixture
def runner(tmp_path):
    instance = tasks.TaskRunner()
    instance.start(tmp_path / 'tasks')
    yield instance
    instance.start(tmp_path / 'tasks')  # Cancels anything a failed test left running


def test_runner_reports_result_and_progress(runner):
    def work(task):
        task.update('Halfway', .5)
        return {'answer': 42}
    task = wait_for(runner.submit('export', 'export', 'user:a', work))
    assert (task.status, task.result, task.progress) == ('done', {'answer': 42}, 1.0)
    assert task.public()['message'] == 'Done'


def test_tasks_are_private_to_their_owner(runner):
    task = runner.submit('export', 'export', 'user:alice', lambda t: None)
    assert runner.get(task.id, 'user:alice') is task
    assert runner.get(task.id, 'user:bob') is None
    assert runner.get(task.id, 'guest:alice') is None


def test_cancelling_a_queued_task_means_it_never_runs(runner):
    release, ran = threading.Event(), []
    first = runner.submit('export', 'export', 'user:a', blocker(release))
    second = runner.submit('export', 'export', 'user:a', lambda t: ran.append(True))
    assert runner.position(second) == 1
    assert 'Waiting for 1 other job' in second.public(runner.position(second))['message']
    runner.cancel(second)
    assert second.status == 'cancelled' and not second.directory.exists()
    release.set()
    wait_for(first)
    time.sleep(.1)
    assert ran == []


def test_cancelling_a_running_task_stops_it_at_its_next_update(runner):
    started = threading.Event()

    def work(task):
        started.set()
        while True:
            task.update('Working', .1)
            time.sleep(.01)
    task = runner.submit('export', 'export', 'user:a', work)
    assert started.wait(5)
    runner.cancel(task)
    assert wait_for(task).status == 'cancelled'


def test_failure_messages_are_helpful_without_leaking_internals(runner):
    def readable(task):
        raise HTTPException(502, 'CUPS on cups:631 rejected this print job')

    def internal(task):
        raise RuntimeError('/data/jobs/secret-path exploded')
    assert wait_for(runner.submit('print', 'print', 'u', readable)).error == 'CUPS on cups:631 rejected this print job'
    hidden = wait_for(runner.submit('print', 'print', 'u', internal))
    assert hidden.status == 'failed' and 'secret-path' not in hidden.error


def test_a_long_export_does_not_delay_printing(runner):
    release = threading.Event()
    try:
        export = runner.submit('export', 'export', 'u', blocker(release))
        printed = wait_for(runner.submit('print', 'print', 'u', lambda t: 'printed'), timeout=5)
        assert printed.result == 'printed' and export.status == 'running'
    finally:
        release.set()


def test_prepare_runs_before_the_task_can_start(runner):
    def prepare(folder):
        time.sleep(.1)  # A worker that started early would not find the file
        (folder / 'source').write_text('input')
    task = wait_for(runner.submit('print', 'print', 'u', lambda t: (t.directory / 'source').read_text(),
                                  prepare=prepare))
    assert task.result == 'input'


def test_finished_tasks_and_files_expire(runner):
    task = wait_for(runner.submit('export', 'export', 'u', lambda t: (t.directory / 'out.pdf').write_text('x')))
    runner.prune(now=task.finished + tasks.RETENTION_SECONDS + 1)
    assert runner.get(task.id, 'u') is None and not task.directory.exists()


def test_restart_forgets_tasks_and_removes_their_files(runner, tmp_path):
    task = wait_for(runner.submit('export', 'export', 'u', lambda t: (t.directory / 'out.pdf').write_text('x')))
    runner.start(tmp_path / 'tasks')
    assert runner.get(task.id, 'u') is None and not task.directory.exists()


def test_ocr_cancellation_stops_ocr_and_every_worker_it_started(tmp_path, monkeypatch):
    """OCRmyPDF runs Ghostscript and Tesseract as children; cancelling must stop them too."""
    real_popen = subprocess.Popen
    worker_pid = tmp_path / 'worker.pid'

    def fake_popen(command, **kwargs):
        assert command[0] == 'ocrmypdf' and kwargs.get('start_new_session')
        # A leader that starts a worker which ignores SIGTERM, like a busy OCR engine.
        script = f"sh -c 'trap \"\" TERM; sleep 30' & echo $! > {worker_pid}; wait"
        return real_popen(['sh', '-c', script], **kwargs)
    monkeypatch.setattr(web.subprocess, 'Popen', fake_popen)
    cancel = threading.Event()
    threading.Timer(.4, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(tasks.Cancelled):
        web.ocr_pdf(tmp_path / 'in.pdf', tmp_path / 'out.pdf', 1, cancel=cancel)
    assert time.monotonic() - started < 15
    worker = int(worker_pid.read_text())
    for _ in range(50):  # The worker is reaped by init once killed
        status = Path(f'/proc/{worker}/status')
        if not status.exists() or 'State:\tZ' in status.read_text():
            break
        time.sleep(.1)
    else:
        pytest.fail('An OCR worker process was left running after cancellation')


# --- HTTP API ------------------------------------------------------------------

def pdf_data(pages=3):
    with fitz.open() as doc:
        for n in range(pages):
            doc.new_page(width=612, height=792).insert_text((60, 75), f'DOCUMENT PAGE {n + 1}', fontsize=22)
        return doc.tobytes()


def login(username, password):
    client = TestClient(web.app)
    signed = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert signed.status_code == 200, signed.text
    client.headers['x-csrf-token'] = signed.json()['csrf']
    return client


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    monkeypatch.setattr(devices, 'list_cups_queues', lambda server: [{'queue': 'Office', 'ready': True, 'status': 'idle'}])
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    monkeypatch.setattr(devices, 'cups_capabilities', lambda *a: {
        'known': False, 'media': [], 'duplex': None, 'color': None, 'color_modes': ['auto', 'color', 'gray', 'bw']})
    printed = []

    def fake_cups(path, title, **choices):
        with fitz.open(path) as doc:
            printed.append({'pages': doc.page_count, 'title': title, **choices})
        return 'request id is Office-7 (1 file(s))'
    monkeypatch.setattr(web, 'print_with_cups', fake_cups)
    security._guest_submissions.clear()
    security._global_submissions.clear()
    with TestClient(web.app):  # Runs startup: data folders and the task runner
        admin = login('admin', PASSWORD)
        configured = admin.patch('/api/admin/printers/Office', json={'make_default': True, 'guest': True})
        assert configured.status_code == 200, configured.text
        yield admin, printed
        tasks.runner.start(tmp_path / 'tasks')


def follow(client, task, base='/api/tasks', timeout=15):
    deadline = time.monotonic() + timeout
    while task['status'] not in tasks.FINISHED:
        assert time.monotonic() < deadline, task
        time.sleep(.02)
        response = client.get(f"{base}/{task['id']}")
        assert response.status_code == 200, response.text
        task = response.json()
    return task


def document(client, pages=2) -> str:
    job = client.post('/api/jobs', json={'title': 'Quarterly report'}).json()
    for n in range(pages):
        page_id = f'{n:016x}'
        Image.new('RGB', (850, 1100), (250, 250, 245)).save(web.JOBS_DIR / job['id'] / 'pages' / f'{page_id}.png')
        job['pages'].append({'id': page_id, 'file': f'{page_id}.png', 'rotation': 0, 'export_mode': 'Original',
                             'crop': None, 'scale': 100, 'deskew': 0.0, 'scanned_mode': 'Color', 'dpi': 100,
                             'batch_id': 'b' * 12, 'created': web.now()})
    web.save_job(job)
    return job['id']


def test_background_export_produces_a_downloadable_pdf(api):
    admin, _ = api
    job_id = document(admin, pages=3)
    started = admin.post(f'/api/jobs/{job_id}/exports', json={'quality': 'small'})
    assert started.status_code == 202, started.text
    task = follow(admin, started.json()['task'])
    assert task['status'] == 'done' and task['has_file'] and task['result']['pages'] == 3
    download = admin.get(f"/api/tasks/{task['id']}/file")
    assert download.status_code == 200 and download.headers['cache-control'] == 'no-store'
    assert "filename*=utf-8''Quarterly%20report.pdf" in download.headers['content-disposition']
    with fitz.open(stream=download.content, filetype='pdf') as pdf:
        assert pdf.page_count == 3


def test_export_of_an_empty_document_is_refused_immediately(api):
    admin, _ = api
    job = admin.post('/api/jobs', json={'title': 'Empty'}).json()
    assert admin.post(f"/api/jobs/{job['id']}/exports", json={}).status_code == 400


def test_other_accounts_cannot_see_or_download_a_task(api):
    admin, _ = api
    admin.post('/api/admin/users', json={'username': 'member1', 'password': 'member-long-password', 'role': 'member'})
    task = follow(admin, admin.post(f'/api/jobs/{document(admin)}/exports', json={}).json()['task'])
    member = login('member1', 'member-long-password')
    assert member.get(f"/api/tasks/{task['id']}").status_code == 404
    assert member.get(f"/api/tasks/{task['id']}/file").status_code == 404
    assert member.delete(f"/api/tasks/{task['id']}").status_code == 404
    assert TestClient(web.app).get(f"/api/tasks/{task['id']}").status_code == 401


def test_searchable_export_can_be_cancelled_during_ocr(api, monkeypatch):
    admin, _ = api
    in_ocr = threading.Event()

    def slow_ocr(source, target, page_count, cancel=None):
        in_ocr.set()
        while not cancel.wait(.02):
            pass
        raise tasks.Cancelled()
    monkeypatch.setattr(web, 'ocr_pdf', slow_ocr)
    task = admin.post(f'/api/jobs/{document(admin)}/exports', json={'ocr': True}).json()['task']
    assert in_ocr.wait(10)
    assert 'Recognizing text' in admin.get(f"/api/tasks/{task['id']}").json()['message']
    assert admin.delete(f"/api/tasks/{task['id']}").status_code == 200
    finished = follow(admin, task)
    assert finished['status'] == 'cancelled' and not finished['has_file']
    assert admin.get(f"/api/tasks/{task['id']}/file").status_code == 409


def inspect(client, pages=3, guest_csrf=None):
    headers = {'X-Upload-Filename': 'handout.pdf'}
    if guest_csrf:
        headers['X-Guest-CSRF'] = guest_csrf
    response = client.post('/api/guest/inspect' if guest_csrf else '/api/print/inspect',
                           headers=headers, content=pdf_data(pages))
    assert response.status_code == 200, response.text
    return response.json()['token']


def test_background_print_of_an_upload_returns_the_cups_receipt(api):
    admin, printed = api
    token = inspect(admin, pages=5)
    started = admin.post('/api/print/submit-preview?background=true', json={'token': token},
                         headers={'X-Print-Pages': '2-4', 'X-Print-Color': 'gray'})
    assert started.status_code == 202, started.text
    task = follow(admin, started.json()['task'])
    assert task['status'] == 'done', task
    assert task['result']['job_id'] == 'Office-7' and task['result']['pages_selected'] == 3
    assert printed[-1]['pages'] == 3 and printed[-1]['color_mode'] == 'gray'


def test_invalid_print_settings_are_still_rejected_before_queueing(api):
    admin, printed = api
    token = inspect(admin, pages=2)
    bad = admin.post('/api/print/submit-preview?background=true', json={'token': token},
                     headers={'X-Print-Media': 'not-a-size'})
    assert bad.status_code == 400 and printed == []


def test_queued_print_keeps_its_own_copy_of_the_upload(api):
    admin, printed = api
    release = threading.Event()
    hold = tasks.runner.submit('print', 'print', 'test', blocker(release))
    try:
        token = inspect(admin, pages=2)
        task = admin.post('/api/print/submit-preview?background=true', json={'token': token}).json()['task']
        assert task['status'] == 'queued' and 'Waiting for 1 other job' in task['message']
        # The preview expires or is replaced while the job waits in line.
        for folder in (web.DATA_DIR / 'print-previews').iterdir():
            for item in folder.iterdir():
                item.unlink()
    finally:
        release.set()
    wait_for(hold)
    assert follow(admin, task)['status'] == 'done' and printed[-1]['pages'] == 2


def test_background_print_of_a_saved_document(api):
    admin, printed = api
    job_id = document(admin, pages=2)
    started = admin.post(f'/api/jobs/{job_id}/print?background=true', headers={'X-Print-Pages': '2'})
    assert started.status_code == 202, started.text
    task = follow(admin, started.json()['task'])
    assert task['status'] == 'done' and task['result']['pages_selected'] == 1
    assert printed[-1]['pages'] == 1 and printed[-1]['title'] == 'Quarterly report'


def test_cups_rejection_is_reported_on_the_task(api, monkeypatch):
    admin, _ = api

    def rejecting(path, title, **choices):
        raise HTTPException(502, 'CUPS on cups.test:631 rejected this print job: paper jam')
    monkeypatch.setattr(web, 'print_with_cups', rejecting)
    task = admin.post('/api/print/submit-preview?background=true', json={'token': inspect(admin)}).json()['task']
    finished = follow(admin, task)
    assert finished['status'] == 'failed' and 'paper jam' in finished['error']


def test_guest_background_print_is_private_to_that_guest(api):
    admin, printed = api
    admin.post('/api/admin/guest', json={'enabled': True})
    guest = TestClient(web.app)
    csrf = guest.get('/api/guest/status').json()['csrf']
    token = inspect(guest, pages=2, guest_csrf=csrf)
    started = guest.post('/api/guest/submit-preview?background=true', json={'token': token},
                         headers={'X-Guest-CSRF': csrf})
    assert started.status_code == 202, started.text
    task = follow(guest, started.json()['task'], base='/api/guest/tasks')
    assert task['status'] == 'done' and printed[-1]['pages'] == 2
    other = TestClient(web.app)
    other.get('/api/guest/status')
    assert other.get(f"/api/guest/tasks/{task['id']}").status_code == 404
    assert admin.get(f"/api/tasks/{task['id']}").status_code == 404
    # The guest rate limit still counts background submissions.
    assert len(security._global_submissions) == 1


def test_print_only_accounts_can_follow_their_print_jobs(api):
    admin, printed = api
    admin.post('/api/admin/users', json={'username': 'frontdesk', 'password': 'printer-long-password', 'role': 'printer'})
    desk = login('frontdesk', 'printer-long-password')
    task = desk.post('/api/print/submit-preview?background=true', json={'token': inspect(desk)}).json()['task']
    assert follow(desk, task)['status'] == 'done'
    assert desk.post(f'/api/jobs/{document(admin)}/exports', json={}).status_code == 403


def test_tasks_from_before_a_restart_report_that_clearly(api):
    admin, _ = api
    task = follow(admin, admin.post(f'/api/jobs/{document(admin)}/exports', json={}).json()['task'])
    web.ensure_directories()  # What startup does after a container restart
    gone = admin.get(f"/api/tasks/{task['id']}")
    assert gone.status_code == 404 and 'restarted' in gone.json()['detail']
