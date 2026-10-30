"""A finished scan's result ("Added 1 page(s)", or its error) belongs to the
document it went into: deleting that document clears it, other people don't
get it, and the page shows it only while that document is open."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app  # noqa: E402

appmod, client, create, scan_and_wait = test_app.appmod, test_app.client, test_app.create, test_app.scan_and_wait
WORKBENCH = (ROOT / 'frontend' / 'js' / 'workbench.js').read_text()


def login(username: str, password: str) -> TestClient:
    person = TestClient(appmod.app)
    result = person.post('/api/auth/login', json={'username': username, 'password': password})
    assert result.status_code == 200, result.text
    person.headers.update({'X-CSRF-Token': result.json()['csrf']})
    return person


def finished(person: TestClient) -> dict:
    for _ in range(100):
        status = person.get('/api/scan').json()
        if not status['active']:
            return status
        time.sleep(.025)
    raise AssertionError('Scan worker did not finish')


def test_deleting_the_document_clears_its_scan_result(client):
    scanned, other = create(client), create(client)
    scan_and_wait(client, scanned, count=1)
    status = client.get('/api/scan').json()
    assert (status['job_id'], status['pages_added'], status['message']) == (scanned, 1, 'Added 1 page(s)')
    assert client.delete(f'/api/jobs/{other}').status_code == 200  # Another document: the result stays
    assert client.get('/api/scan').json()['message'] == 'Added 1 page(s)'
    assert client.delete(f'/api/jobs/{scanned}').status_code == 200
    status = client.get('/api/scan').json()
    assert (status['job_id'], status['pages_added'], status['message'], status['error']) == (None, 0, 'Ready', None)


def test_deleting_the_document_clears_its_scan_error(client, monkeypatch):
    monkeypatch.setattr(appmod, 'run_scanner_process', lambda command, cwd, stop_event: (1, 'Scanner unplugged'))
    job = create(client)
    assert client.post(f'/api/jobs/{job}/scan', json={'source': 'Flatbed', 'mode': 'Color', 'resolution': 150}).status_code == 202
    assert finished(client)['error'] == 'Scanner unplugged'
    client.delete(f'/api/jobs/{job}')
    assert client.get('/api/scan').json()['error'] is None


def test_other_people_dont_get_someone_elses_scan_result(client):
    client.post('/api/admin/users', json={'username': 'alice', 'password': 'alice-long-password', 'role': 'member'})
    alice = login('alice', 'alice-long-password')
    scan_and_wait(client, create(client), count=1)
    status = alice.get('/api/scan').json()
    assert (status['job_id'], status['pages_added'], status['message'], status['error']) == (None, 0, 'Ready', None)
    own = create(alice)  # Her own scan's result is hers, and the admin's to see
    assert alice.post(f'/api/jobs/{own}/scan', json={'source': 'Flatbed', 'mode': 'Color', 'resolution': 150}).status_code == 202
    status = finished(alice)
    assert (status['job_id'], status['pages_added']) == (own, 1)
    assert client.get('/api/scan').json()['job_id'] == own


def test_the_page_shows_a_scan_result_on_its_own_document_only():
    notice = WORKBENCH.split('function renderScanNotice(){', 1)[1].split('\n}', 1)[0]
    own_document = "else if(!status.job_id||status.job_id!==state.job?.id)el.className='notice hidden'"
    assert own_document in notice
    assert notice.index('if(status.active)') < notice.index(own_document) < notice.index('status.error') < notice.index('status.pages_added')
    assert 'state.scan=status;renderScanNotice();' in WORKBENCH.split('function renderScanStatus(status){', 1)[1].split('\n}', 1)[0]
    document = WORKBENCH.split('function renderDocument(){', 1)[1].split('\n}', 1)[0]
    assert 'renderScanNotice();' in document  # Opening, creating or deleting a document redraws it
