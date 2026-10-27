"""v0.17: blank pages left out or removed, and sending documents to
Paperless-ngx or a folder."""
from __future__ import annotations

import http.server
import json
import random
import subprocess
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import app as web, destinations, devices, processing, security, tasks  # noqa: E402

PASSWORD = 'destinations-admin-password'


# --- What counts as blank ----------------------------------------------------------

@lru_cache(maxsize=None)
def sheet(kind: str) -> Image.Image:
    """A letter-size page at 300 DPI, as a scanner would deliver it."""
    random.seed(3)
    width, height = 2550, 3300
    page = Image.new('RGB', (width, height), (200, 200, 200) if kind == 'grey' else (246, 243, 232))
    draw = ImageDraw.Draw(page)
    big, small = ImageFont.load_default(size=42), ImageFont.load_default(size=26)
    if kind == 'dust':
        for _ in range(300):
            x, y, r = random.randint(0, width), random.randint(0, height), random.randint(2, 7)
            draw.ellipse((x, y, x + r, y + r), fill=(30, 30, 30))
    elif kind == 'show-through':
        for y in range(300, 3000, 70):
            draw.text((250, y), 'mirrored text from the other side ' * 2, fill=(200, 197, 187), font=big)
        page = page.filter(ImageFilter.GaussianBlur(2))
    elif kind == 'edge-shadow':
        draw.rectangle((0, 0, 60, height), fill=(40, 40, 40))
    elif kind == 'page-number':
        draw.text((width // 2, height - 250), '3', fill=(20, 20, 20), font=big)
    elif kind == 'initials':
        draw.text((2100, 3000), 'K.M.', fill=(40, 40, 40), font=small)
    elif kind == 'thin-signature':
        draw.line([(400 + i * 12, 2500 + (i % 7) * 9) for i in range(40)], fill=(60, 60, 110), width=3)
    elif kind == 'one-small-line':
        draw.text((250, 400), 'Received with thanks, 27/10/2026', fill=(40, 40, 40), font=small)
    elif kind == 'text':
        for y in range(300, 3000, 60):
            draw.text((250, y), 'Lorem ipsum dolor sit amet, consectetur adipiscing elit', fill=(25, 25, 25), font=big)
    return page


@pytest.mark.parametrize('kind', ['empty', 'grey', 'dust', 'show-through', 'edge-shadow', 'page-number'])
def test_pages_with_nothing_written_on_them_are_blank(kind):
    assert processing.is_blank(sheet(kind))


@pytest.mark.parametrize('kind', ['initials', 'thin-signature', 'one-small-line', 'text'])
def test_any_writing_keeps_a_page(kind):
    assert not processing.is_blank(sheet(kind))


# --- Leaving them out while scanning, or removing them later -----------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', 'airscan:w0:Test Scanner')
    monkeypatch.setattr(web, 'CUPS_SERVERS', [])
    monkeypatch.setattr(destinations, 'FOLDER_ROOT', tmp_path / 'destinations')
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    feeder = ['text', 'empty', 'initials', 'dust']  # What the scanner "feeds"

    def fake_scanner(command, cwd, stop_event):
        for number, kind in enumerate(feeder, start=1):
            sheet(kind).resize((1275, 1650)).save(Path(cwd) / f'page-{number:04d}.png')  # 150 DPI
        return 0, None
    monkeypatch.setattr(web, 'run_scanner_process', fake_scanner)
    monkeypatch.setattr(web.subprocess, 'run', lambda command, **kw: subprocess.CompletedProcess(
        command, 0, "device `airscan:w0:Test Scanner' is a Test Scanner", ''))
    web.scan_cancel_event.clear()
    web.scan_state.update(active=False, error=None, pages_added=0, cancel_requested=False)
    security._guest_submissions.clear()
    with TestClient(web.app) as test:
        signed = test.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD}).json()
        test.headers['x-csrf-token'] = signed['csrf']
        yield test
        tasks.runner.start(tmp_path / 'tasks')


def scan(client, job_id, **options):
    started = client.post(f'/api/jobs/{job_id}/scan', json={'source': 'ADF', 'mode': 'Color', 'resolution': 150, **options})
    assert started.status_code == 202, started.text
    for _ in range(200):
        status = client.get('/api/scan').json()
        if not status['active']:
            return status, client.get(f'/api/jobs/{job_id}').json()
        time.sleep(.05)
    pytest.fail('The scan did not finish')


def test_blank_pages_can_be_left_out_while_scanning_and_brought_back(client):
    job_id = client.post('/api/jobs', json={'title': 'Letters'}).json()['id']
    status, job = scan(client, job_id, skip_blank=True)
    assert len(job['pages']) == 2 and len(job['deleted_pages']) == 2
    assert 'Added 2 page(s)' in status['message'] and '2 blank pages left out' in status['message']
    kept = [p['id'] for p in job['pages']]
    blank = [d['page']['id'] for d in job['deleted_pages']]
    client.post(f'/api/jobs/{job_id}/undo-delete')
    job = client.post(f'/api/jobs/{job_id}/undo-delete').json()
    assert [p['id'] for p in job['pages']] == [kept[0], blank[0], kept[1], blank[1]]  # Back in scanned order


def test_duplex_batches_keep_their_blank_pages(client):
    job_id = client.post('/api/jobs', json={'title': 'Two-sided'}).json()['id']
    _, job = scan(client, job_id, skip_blank=True, role='backs')
    assert len(job['pages']) == 4 and job['deleted_pages'] == []  # Fronts and backs must stay paired


def test_blank_pages_can_be_removed_later(client):
    job_id = client.post('/api/jobs', json={'title': 'Old scan'}).json()['id']
    _, job = scan(client, job_id)
    assert len(job['pages']) == 4
    ids = [p['id'] for p in job['pages']]
    result = client.post(f'/api/jobs/{job_id}/remove-blank-pages').json()
    assert result['removed'] == [ids[1], ids[3]] and [p['id'] for p in result['job']['pages']] == [ids[0], ids[2]]
    assert client.post(f'/api/jobs/{job_id}/remove-blank-pages').json()['removed'] == []
    client.post(f'/api/jobs/{job_id}/undo-delete')
    job = client.post(f'/api/jobs/{job_id}/undo-delete').json()
    assert [p['id'] for p in job['pages']] == ids


# --- Paperless-ngx --------------------------------------------------------------------

class FakePaperless:
    """Answers the two Paperless API calls Folio makes, and keeps what it receives."""

    def __init__(self, token='paperless-token'):
        self.token, self.received = token, []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorized(self):
                return self.headers.get('Authorization') == f'Token {fake.token}'

            def do_GET(self):
                if not self.authorized():
                    return self.answer(401, {'detail': 'Invalid token.'})
                if self.path.startswith('/api/tags/'):
                    page2 = 'page=2' in self.path
                    results = [{'id': 7, 'name': 'Scanned'}] if page2 else [{'id': 3, 'name': 'Bills'}]
                    host = f'http://{self.headers["Host"]}'
                    return self.answer(200, {'count': 2, 'results': results,
                                             'next': None if page2 else f'{host}/api/tags/?page=2&page_size=100'})
                self.answer(404, {'detail': 'Not found.'})

            def do_POST(self):
                if not self.authorized():
                    return self.answer(401, {'detail': 'Invalid token.'})
                body = self.rfile.read(int(self.headers['Content-Length']))
                fake.received.append({'content_type': self.headers['Content-Type'], 'body': body})
                self.answer(200, '1f8b0c6a-5e7f-4a3b-9c1d-000000000001')

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def paperless():
    fake = FakePaperless()
    yield fake
    fake.close()


def follow(client, task, timeout=60):
    deadline = time.monotonic() + timeout
    while task['status'] not in tasks.FINISHED:
        assert time.monotonic() < deadline, task
        time.sleep(.05)
        task = client.get(f"/api/tasks/{task['id']}").json()
    return task


def a_document(client) -> str:
    job_id = client.post('/api/jobs', json={'title': 'Electricity bill: October'}).json()['id']
    scan(client, job_id)
    return job_id


def test_paperless_is_set_up_with_a_checked_token_and_its_own_tags(client, paperless):
    details = {'kind': 'paperless', 'name': 'Paperless', 'url': paperless.url + '/', 'token': 'wrong'}
    refused = client.post('/api/admin/destinations/check', json=details)
    assert refused.status_code == 400 and 'API token' in refused.json()['detail']
    unreachable = client.post('/api/admin/destinations/check', json={**details, 'url': 'http://127.0.0.1:9'})
    assert unreachable.status_code == 400 and 'Could not reach Paperless' in unreachable.json()['detail']
    checked = client.post('/api/admin/destinations/check', json={**details, 'token': 'paperless-token'}).json()
    assert checked['tags'] == [{'id': 3, 'name': 'Bills'}, {'id': 7, 'name': 'Scanned'}]  # Both pages
    added = client.post('/api/admin/destinations', json={**details, 'token': 'paperless-token', 'tags': [7, 99]})
    assert added.status_code == 201, added.text
    saved = added.json()
    assert saved['url'] == paperless.url and saved['tags'] == [7] and saved['has_token'] is True
    assert 'token' not in saved and 'paperless-token' not in client.get('/api/admin/destinations').text
    assert client.get('/api/destinations').json() == [{'id': saved['id'], 'name': 'Paperless', 'kind': 'paperless'}]


def test_a_document_is_sent_to_paperless(client, paperless):
    destination = client.post('/api/admin/destinations', json={
        'kind': 'paperless', 'name': 'Paperless', 'url': paperless.url, 'token': 'paperless-token', 'tags': [3]}).json()
    job_id = a_document(client)
    started = client.post(f'/api/jobs/{job_id}/send', json={'destination': destination['id']})
    assert started.status_code == 202, started.text
    task = follow(client, started.json()['task'])
    assert task['status'] == 'done', task
    assert task['result']['destination'] == 'Paperless'
    upload = paperless.received[-1]
    assert upload['content_type'].startswith('multipart/form-data; boundary=')
    assert b'name="title"\r\n\r\nElectricity bill: October' in upload['body']
    assert b'name="tags"\r\n\r\n3' in upload['body']
    assert b'filename="Electricity bill: October.pdf"' in upload['body'] and b'%PDF' in upload['body']
    sent = client.get(f'/api/jobs/{job_id}').json()['sent']
    assert sent[-1]['destination'] == 'Paperless'


def test_a_paperless_problem_is_explained(client, paperless):
    destination = client.post('/api/admin/destinations', json={
        'kind': 'paperless', 'name': 'Paperless', 'url': paperless.url, 'token': 'paperless-token'}).json()
    paperless.token = 'rotated'  # Someone renewed the token in Paperless
    job_id = a_document(client)
    task = follow(client, client.post(f'/api/jobs/{job_id}/send', json={'destination': destination['id']}).json()['task'])
    assert task['status'] == 'failed' and 'did not accept the API token' in task['error']


# --- Folders ----------------------------------------------------------------------------

def test_a_document_is_saved_to_a_mounted_folder(client):
    nas = destinations.FOLDER_ROOT / 'nas'
    nas.mkdir(parents=True)
    assert client.post('/api/admin/destinations', json={'kind': 'folder', 'name': 'NAS', 'folder': '../etc'}).status_code == 400
    missing = client.post('/api/admin/destinations', json={'kind': 'folder', 'name': 'NAS', 'folder': 'missing'})
    assert missing.status_code == 400 and 'Mount a host folder there' in missing.json()['detail']
    destination = client.post('/api/admin/destinations', json={'kind': 'folder', 'name': 'NAS', 'folder': 'nas'}).json()
    job_id = a_document(client)
    for _ in range(2):
        task = follow(client, client.post(f'/api/jobs/{job_id}/send', json={'destination': destination['id']}).json()['task'])
        assert task['status'] == 'done', task
    names = sorted(p.name for p in nas.iterdir())
    today = time.strftime('%Y-%m-%d')
    assert names == [f'{today} Electricity bill October (2).pdf', f'{today} Electricity bill October.pdf']
    assert (nas / names[1]).read_bytes().startswith(b'%PDF')  # Complete; no hidden part-files left behind
    assert 'Saved as' in task['result']['detail']


def test_destinations_are_for_people_with_documents(client):
    client.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
    client.post('/api/admin/users', json={'username': 'kiosk', 'password': 'kiosk-long-password', 'role': 'printer'})
    (destinations.FOLDER_ROOT / 'nas').mkdir(parents=True)
    destination = client.post('/api/admin/destinations', json={'kind': 'folder', 'name': 'NAS', 'folder': 'nas'}).json()
    job_id = a_document(client)

    def person(name, password):
        other = TestClient(web.app)
        other.headers['x-csrf-token'] = other.post('/api/auth/login', json={'username': name, 'password': password}).json()['csrf']
        return other
    alice, kiosk = person('alice', 'alice-long-password'), person('kiosk', 'kiosk-long-password')
    assert [d['name'] for d in alice.get('/api/destinations').json()] == ['NAS']
    assert alice.post('/api/admin/destinations', json={'kind': 'folder', 'name': 'X', 'folder': 'nas'}).status_code == 403
    assert alice.post(f'/api/jobs/{job_id}/send', json={'destination': destination['id']}).status_code == 404  # Not hers
    assert kiosk.get('/api/destinations').status_code == 403
    assert client.delete(f"/api/admin/destinations/{destination['id']}").status_code == 200
    assert client.post(f'/api/jobs/{job_id}/send', json={'destination': destination['id']}).status_code == 404
