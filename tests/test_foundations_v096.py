"""v0.9.6 foundations: preview cache, device cache/backoff, diagnostics, no hardcoded network."""
from __future__ import annotations

import io
import ipaddress
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import app as web, devices, diagnostics, security  # noqa: E402
from backend.version import __version__  # noqa: E402

PASSWORD = 'test-password-12345678'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    monkeypatch.setattr(devices, 'list_cups_queues', lambda server: [])
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    security._guest_submissions.clear()
    security._global_submissions.clear()
    with TestClient(web.app) as test:
        signed = test.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD})
        assert signed.status_code == 200, signed.text
        test.headers['x-csrf-token'] = signed.json()['csrf']
        yield test


def document_with_page(client, size=(2600, 3400)) -> tuple[str, dict]:
    """A document holding one 'scanned' page larger than the preview proxy."""
    job = client.post('/api/jobs', json={'title': 'Cache test'}).json()
    image = Image.new('RGB', size, (250, 248, 240))
    for y in range(200, size[1] - 200, 90):  # Text-like rows give edits something to change
        image.paste((30, 40, 35), (180, y, size[0] - 180, y + 24))
    page_id = 'a' * 16
    image.save(web.JOBS_DIR / job['id'] / 'pages' / f'{page_id}.png')
    page = {'id': page_id, 'file': f'{page_id}.png', 'rotation': 0, 'export_mode': 'Original',
            'crop': None, 'scale': 100, 'deskew': 0.0, 'scanned_mode': 'Color', 'dpi': 300,
            'batch_id': 'b' * 12, 'created': web.now()}
    job['pages'].append(page)
    web.save_job(job)
    return job['id'], page


def image_url(job_id, page_id, **flags):
    query = '&'.join(f'{k}={str(v).lower()}' for k, v in flags.items())
    return f'/api/jobs/{job_id}/pages/{page_id}/image?{query}'


def count_original_decodes(monkeypatch) -> list:
    decodes = []
    original = web.load_page_image

    def spy(job_id, page, **kwargs):
        decodes.append(page['file'])
        return original(job_id, page, **kwargs)
    monkeypatch.setattr(web, 'load_page_image', spy)
    return decodes


def test_previews_decode_the_original_scan_once(client, monkeypatch):
    job_id, page = document_with_page(client)
    decodes = count_original_decodes(monkeypatch)
    first = client.get(image_url(job_id, page['id'], thumb=True))
    assert first.status_code == 200 and first.headers['content-type'] == 'image/jpeg'
    assert len(decodes) == 1  # Builds the proxy
    for flags in ({'thumb': True}, {}, {'large': True}, {'large': True, 'uncropped': True}):
        assert client.get(image_url(job_id, page['id'], **flags)).status_code == 200
    assert len(decodes) == 1  # Every other size comes from the proxy
    # Privacy: scans are never stored in shared/browser caches.
    assert first.headers['cache-control'] == 'no-store'


def test_editing_renders_a_new_preview_and_prunes_the_old_one(client, monkeypatch):
    job_id, page = document_with_page(client)
    before = client.get(image_url(job_id, page['id'])).content
    decodes = count_original_decodes(monkeypatch)
    edited = client.patch(f'/api/jobs/{job_id}/pages/{page["id"]}', json={'brightness': 40})
    assert edited.status_code == 200
    after = client.get(image_url(job_id, page['id'])).content
    assert after != before
    assert decodes == []  # Edits re-render from the proxy, not from the PNG
    cache = web.JOBS_DIR / job_id / 'cache'
    assert len(list(cache.glob('*.normal.*.jpg'))) == 1
    # Undoing the edit gives back the same pixels as the first render.
    client.patch(f'/api/jobs/{job_id}/pages/{page["id"]}', json={'brightness': 0})
    assert client.get(image_url(job_id, page['id'])).content == before


def test_proxy_preview_matches_a_render_from_the_original(client):
    job_id, page = document_with_page(client)
    edits = {'rotation': 90, 'crop': [0.1, 0.1, 0.7, 0.6], 'deskew': 1.5, 'contrast': 20}
    client.patch(f'/api/jobs/{job_id}/pages/{page["id"]}', json=edits)
    page = client.get(f'/api/jobs/{job_id}').json()['pages'][0]
    served = Image.open(io.BytesIO(client.get(image_url(job_id, page['id'])).content)).convert('RGB')
    # The v0.9.5 pipeline: downscale the original, then apply the same edits.
    with Image.open(web.JOBS_DIR / job_id / 'pages' / page['file']) as original:
        reference = original.convert('RGB')
    reference.thumbnail(web.PREVIEW_SIZES['normal'][0], Image.Resampling.LANCZOS)
    reference = web.transform_page(reference, {**page, 'scale': 100})
    reference.thumbnail(web.PREVIEW_SIZES['normal'][1], Image.Resampling.LANCZOS)
    assert abs(served.width - reference.width) <= 2 and abs(served.height - reference.height) <= 2
    difference = ImageChops.difference(served.resize(reference.size), reference.convert('RGB'))
    assert max(ImageStat.Stat(difference).mean) < 4  # Out of 255: visually identical


def test_exported_pdf_still_uses_full_resolution_original(client):
    job_id, page = document_with_page(client)
    client.get(image_url(job_id, page['id'], large=True))  # Proxy now exists
    pdf = client.get(f'/api/jobs/{job_id}/export')
    assert pdf.status_code == 200
    with fitz.open(stream=pdf.content, filetype='pdf') as doc:
        info = doc[0].get_images(full=True)[0]
        assert (info[2], info[3]) == (2600, 3400)


def test_single_render_slot_does_not_deadlock(client, monkeypatch):
    monkeypatch.setattr(web, 'RENDER_SLOTS', threading.BoundedSemaphore(1))
    job_id, page = document_with_page(client)
    result = {}
    worker = threading.Thread(target=lambda: result.update(
        response=client.get(image_url(job_id, page['id'], thumb=True))), daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert not worker.is_alive(), 'Preview rendering deadlocked with one render slot'
    assert result['response'].status_code == 200


def test_device_cache_answers_repeated_queries_until_invalidated(monkeypatch):
    monkeypatch.setattr(devices, 'DEVICE_CACHE', True)
    calls = []
    monkeypatch.setattr(devices, 'list_cups_queues',
                        lambda server: calls.append(server) or [{'queue': 'Office', 'ready': True, 'status': 'idle'}])
    servers = ['cups-a:631', 'cups-b:631']
    first = devices.all_cups_queues(servers)
    second = devices.all_cups_queues(servers)
    assert first == second and [r['id'] for r in first] == ['cups-a:631|Office', 'cups-b:631|Office']
    assert sorted(calls) == servers
    devices.invalidate('cups')
    devices.all_cups_queues(servers)
    assert len(calls) == 4


def test_failed_probes_expire_sooner_than_good_ones(monkeypatch):
    monkeypatch.setattr(devices, 'DEVICE_CACHE', True)
    clock = [1000.0]
    monkeypatch.setattr(devices.time, 'monotonic', lambda: clock[0])
    answers = iter([{'available': False}, {'available': True}, {'available': False}])
    monkeypatch.setattr(devices, 'read_scanner_capabilities', lambda device, timeout: next(answers))
    assert devices.scanner_capabilities('test:dev')['available'] is False
    clock[0] += devices.FAILED_PROBE_TTL + 1
    assert devices.scanner_capabilities('test:dev')['available'] is True
    clock[0] += devices.FAILED_PROBE_TTL + 1  # A good answer is kept much longer
    assert devices.scanner_capabilities('test:dev')['available'] is True


def test_unreachable_cups_server_is_skipped_during_backoff(monkeypatch):
    attempts = []

    def hanging(args, **kwargs):
        attempts.append(args)
        raise subprocess.TimeoutExpired(args, kwargs.get('timeout', 8))
    monkeypatch.setattr(devices.subprocess, 'run', hanging)
    first = devices.cups_run(['lpstat', '-h', 'offline:631', '-p'])
    second = devices.cups_run(['lpstat', '-h', 'offline:631', '-a'])
    assert first.returncode == 1 and second.returncode == 1
    assert len(attempts) == 1  # The second query did not wait for another timeout
    assert 'not responding' in second.stderr
    assert devices.cups_backoff('offline:631') > 0
    devices.mark_cups_reachable('offline:631')
    devices.cups_run(['lpstat', '-h', 'offline:631', '-p'])
    assert len(attempts) == 2


def test_refresh_parameter_clears_cached_printer_answers(client, monkeypatch):
    monkeypatch.setattr(devices, 'DEVICE_CACHE', True)
    calls = []
    monkeypatch.setattr(devices, 'list_cups_queues', lambda server: calls.append(server) or [])
    client.get('/api/printers')
    client.get('/api/printers')
    assert len(calls) == 1
    client.get('/api/printers?refresh=true')
    assert len(calls) == 2
    client.get('/api/admin/devices?refresh=true')
    assert len(calls) == 3


def test_no_default_cups_server_without_configuration():
    env = {k: v for k, v in os.environ.items() if k not in ('CUPS_SERVERS', 'CUPS_SERVER')}
    output = subprocess.run([sys.executable, '-c', 'from backend import app; print(app.CUPS_SERVERS)'],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert output.returncode == 0, output.stderr
    assert output.stdout.strip() == '[]'


def test_no_development_network_addresses_in_shipped_files():
    """Examples use loopback, documentation ranges or obvious placeholders, never a real network."""
    allowed = [ipaddress.ip_network(n) for n in ('127.0.0.0/8', '0.0.0.0/32', '192.0.2.0/24', '198.51.100.0/24',
                                                 '203.0.113.0/24', '192.168.1.0/24', '172.19.0.0/16')]
    shipped = [*ROOT.glob('backend/*.py'), *ROOT.glob('frontend/*'), ROOT / 'compose.yaml',
               ROOT / '.env.example', ROOT / 'Dockerfile']
    for path in shipped:
        for found in re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b', path.read_text(encoding='utf-8')):
            network = ipaddress.ip_network(found, strict=False)
            assert any(network.subnet_of(ok) for ok in allowed), f'{found} in {path.name} looks like a real network'


def test_version_is_consistent_across_release_files():
    html = (ROOT / 'frontend' / 'index.html').read_text()
    compose = (ROOT / 'compose.yaml').read_text()
    assert re.search(r'rail-license">[^<]*<span>([\d.]+)</span>', html).group(1) == __version__
    minor = '.'.join(__version__.split('.')[:2])
    assert f'folio-scan-studio:${{FOLIO_VERSION:-{minor}}}-' in compose
    assert f'\nFOLIO_VERSION={minor}\n' in (ROOT / '.env.example').read_text()
    assert f'ARG FOLIO_VERSION={__version__}\n' in (ROOT / 'Dockerfile').read_text()
    assert web.app.version == __version__


def test_health_is_public_cheap_and_reports_storage_failures(client, monkeypatch):
    anonymous = TestClient(web.app)
    healthy = anonymous.get('/api/health')
    assert healthy.status_code == 200
    assert healthy.json() == {'ok': True, 'version': __version__}
    monkeypatch.setattr(diagnostics, 'storage_problems', lambda data_dir: ['Data directory is not writable'])
    broken = anonymous.get('/api/health')
    assert broken.status_code == 503 and broken.json()['ok'] is False


def fake_tools(monkeypatch):
    outputs = {'lpstat': (0, 'scheduler is running'),
               'scanimage': (0, 'scanimage (sane-backends) 1.1.1-debian; backend version 1.1.1'),
               'ocrmypdf': (None, '')}
    monkeypatch.setattr(diagnostics, 'run_tool', lambda args, timeout=6: outputs[args[0]])
    monkeypatch.setattr(devices, 'read_sane_devices', lambda timeout=14: [])


def test_diagnostics_requires_an_administrator(client):
    assert TestClient(web.app).get('/api/admin/diagnostics').status_code == 401


def test_diagnostics_reports_printing_scanning_and_storage(client, monkeypatch):
    fake_tools(monkeypatch)
    monkeypatch.setattr(web, 'CUPS_SERVERS', ['cups-a:631'])
    monkeypatch.setattr(devices, 'list_cups_queues',
                        lambda server: [{'queue': 'Office', 'ready': True, 'status': 'idle'}])
    report = client.get('/api/admin/diagnostics').json()
    by_id = {c['id']: c for group in report['sections'].values() for c in group}
    assert by_id['cups:cups-a:631']['status'] == 'ok' and '1 printer, 1 ready' in by_id['cups:cups-a:631']['detail']
    assert by_id['default-printer']['status'] == 'warn'
    assert by_id['sane']['detail'] == 'sane-backends 1.1.1-debian'
    assert by_id['scanners']['status'] == 'warn'  # None registered yet
    assert by_id['ocr']['status'] == 'info'
    assert by_id['storage']['status'] in ('ok', 'warn')
    assert report['overall'] == 'warn'


def test_diagnostics_flags_an_untrusted_reverse_proxy(client, monkeypatch):
    fake_tools(monkeypatch)
    proxied = client.get('/api/admin/diagnostics', headers={'X-Forwarded-For': '10.0.0.40'}).json()
    proxy = next(c for c in proxied['sections']['system'] if c['id'] == 'proxy')
    assert proxy['status'] == 'warn' and 'FORWARDED_ALLOW_IPS' in proxy['detail']
    direct = client.get('/api/admin/diagnostics').json()
    assert next(c for c in direct['sections']['system'] if c['id'] == 'proxy')['status'] == 'info'


def test_diagnostics_without_cups_servers_explains_the_setting(client, monkeypatch):
    fake_tools(monkeypatch)
    monkeypatch.setattr(web, 'CUPS_SERVERS', [])
    report = client.get('/api/admin/diagnostics').json()
    printing = report['sections']['printing']
    assert printing[0]['status'] == 'warn' and 'Print servers' in printing[0]['detail']
