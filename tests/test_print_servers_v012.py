"""Print servers can be added and removed in Devices, alongside CUPS_SERVERS."""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import app as web, cupsadmin, devices, diagnostics, security  # noqa: E402

PASSWORD = 'servers-admin-password'


@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setattr(web, 'CUPS_SERVERS', ['settings-cups:631'])
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    seen = []
    monkeypatch.setattr(devices, 'list_cups_queues',
                        lambda server: seen.append(server) or [{'queue': 'Office', 'ready': True, 'status': 'idle'}])
    monkeypatch.setattr(devices, 'cups_capabilities', lambda *a: {'known': False, 'media': [], 'duplex': None,
                                                                  'color': None, 'color_modes': ['auto']})
    reachable = {'office-pc:631', '192.168.1.10:631', 'cups.lan:8631'}
    monkeypatch.setattr(diagnostics, 'probe_cups_server', lambda server: diagnostics.check(
        f'cups:{server}', server, 'ok' if server in reachable else 'fail',
        'Reachable in 3 ms; 1 queues, 1 ready' if server in reachable else 'did not answer within 6 s'))
    security._guest_submissions.clear()
    with TestClient(web.app) as client:
        signed = client.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['x-csrf-token'] = signed.json()['csrf']
        yield client, seen


def listing(client):
    return {s['server']: s for s in client.get('/api/admin/cups/servers').json()}


def test_add_a_print_server_and_use_it(admin):
    client, seen = admin
    added = client.post('/api/admin/cups/servers', json={'server': '192.168.1.10'})
    assert added.status_code == 201, added.text
    assert added.json()['server'] == '192.168.1.10:631'  # Default port spelled out
    servers = listing(client)
    assert servers['settings-cups:631']['source'] == 'settings'
    assert servers['192.168.1.10:631']['source'] == 'folio'
    printers = client.get('/api/admin/devices?refresh=true').json()['printers']
    assert {p['server'] for p in printers} == {'settings-cups:631', '192.168.1.10:631'}
    assert '192.168.1.10:631' in seen
    # Other endpoints accept it as a configured server.
    assert client.get(f"/api/admin/cups/servers/{quote('192.168.1.10:631')}/devices").status_code == 409  # No login yet


def test_addresses_are_cleaned_up_and_validated(admin):
    client, _ = admin
    assert client.post('/api/admin/cups/servers', json={'server': 'http://cups.lan:8631/admin'}).json()['server'] == 'cups.lan:8631'
    for bad in ('not a host', 'a;rm -rf', '../etc', ''):
        assert client.post('/api/admin/cups/servers', json={'server': bad}).status_code in (400, 422), bad
    assert client.post('/api/admin/cups/servers', json={'server': 'settings-cups:631'}).status_code == 409
    assert client.post('/api/admin/cups/servers', json={'server': 'cups.lan:8631'}).status_code == 409


def test_unreachable_servers_are_not_added(admin):
    client, _ = admin
    refused = client.post('/api/admin/cups/servers', json={'server': '10.9.9.9'})
    assert refused.status_code == 400 and 'could not reach CUPS' in refused.json()['detail']
    assert '10.9.9.9:631' not in listing(client)


def test_removing_a_server_forgets_its_printers_and_login(admin, monkeypatch):
    client, _ = admin
    client.post('/api/admin/cups/servers', json={'server': 'office-pc'})
    key = 'office-pc:631|Office'
    client.patch(f"/api/admin/printers/{quote(key, safe='')}", json={'make_default': True, 'guest': True})
    logins = {'office-pc:631': {'username': 'u', 'password': 'p', 'tls': False, 'pin': None}}
    cupsadmin._save(web.DATA_DIR, logins)
    assert client.delete(f"/api/admin/cups/servers/{quote('office-pc:631')}").status_code == 200
    config = web.registry()
    assert 'office-pc:631' not in config['cups_servers'] and key not in config['printers']
    assert config['default_printer'] is None
    assert cupsadmin.admin_status(web.DATA_DIR, 'office-pc:631')['connected'] is False
    assert 'office-pc:631' not in listing(client)


def test_servers_from_settings_cannot_be_removed_here(admin):
    client, _ = admin
    refused = client.delete(f"/api/admin/cups/servers/{quote('settings-cups:631')}")
    assert refused.status_code == 400 and 'CUPS_SERVERS' in refused.json()['detail']
    assert client.delete('/api/admin/cups/servers/unknown:631').status_code == 404


def test_folio_works_with_no_cups_servers_in_settings(admin, monkeypatch):
    client, _ = admin
    monkeypatch.setattr(web, 'CUPS_SERVERS', [])
    assert listing(client) == {}
    client.post('/api/admin/cups/servers', json={'server': 'office-pc'})
    assert list(listing(client)) == ['office-pc:631']
    assert client.get('/api/printers').json()['printers'][0]['server'] == 'office-pc:631'


def test_only_administrators_manage_print_servers(admin):
    client, _ = admin
    client.post('/api/admin/users', json={'username': 'member1', 'password': 'member-long-password', 'role': 'member'})
    member = TestClient(web.app)
    member.headers['x-csrf-token'] = member.post('/api/auth/login', json={
        'username': 'member1', 'password': 'member-long-password'}).json()['csrf']
    assert member.post('/api/admin/cups/servers', json={'server': 'office-pc'}).status_code == 403
    assert member.delete(f"/api/admin/cups/servers/{quote('settings-cups:631')}").status_code == 403
