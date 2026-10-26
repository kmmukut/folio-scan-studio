"""v0.12 Lite and Full editions from one codebase."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import app as web, devices, diagnostics, features, security  # noqa: E402
from backend.version import __version__  # noqa: E402

PASSWORD = 'editions-admin-password'
LITE = {'ocr': False, 'auto_straighten': False}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    monkeypatch.setattr(devices, 'list_cups_queues', lambda server: [])
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    security._guest_submissions.clear()
    with TestClient(web.app) as test:
        signed = test.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD})
        test.headers['x-csrf-token'] = signed.json()['csrf']
        yield test


def lite(monkeypatch):
    monkeypatch.setattr(features, 'detect', lambda: dict(LITE))
    monkeypatch.setenv('FOLIO_EDITION', 'lite')


def document(client) -> tuple[str, str]:
    from PIL import Image
    job = client.post('/api/jobs', json={'title': 'Edition test'}).json()
    Image.new('RGB', (600, 800), 'white').save(web.JOBS_DIR / job['id'] / 'pages' / f'{"a" * 16}.png')
    job['pages'].append({'id': 'a' * 16, 'file': f'{"a" * 16}.png', 'rotation': 0, 'export_mode': 'Original',
                         'crop': None, 'scale': 100, 'deskew': 0.0, 'scanned_mode': 'Color', 'dpi': 100,
                         'batch_id': 'b' * 12, 'created': web.now()})
    web.save_job(job)
    return job['id'], 'a' * 16


def test_features_are_public_and_describe_the_edition(client, monkeypatch):
    anonymous = TestClient(web.app).get('/api/features')
    assert anonymous.status_code == 200
    body = anonymous.json()
    assert body['version'] == __version__ and set(body['features']) == {'ocr', 'auto_straighten'}
    lite(monkeypatch)
    assert TestClient(web.app).get('/api/features').json()['edition'] == 'lite'


def test_edition_is_inferred_when_not_declared(monkeypatch):
    monkeypatch.delenv('FOLIO_EDITION', raising=False)
    monkeypatch.setattr(features, 'detect', lambda: dict(LITE))
    assert features.edition() == 'lite'
    monkeypatch.setattr(features, 'detect', lambda: {'ocr': True, 'auto_straighten': True})
    assert features.edition() == 'full'


def test_lite_refuses_searchable_pdfs_clearly_but_exports_normally(client, monkeypatch):
    lite(monkeypatch)
    job_id, _ = document(client)
    for response in (client.get(f'/api/jobs/{job_id}/export?ocr=true'),
                     client.post(f'/api/jobs/{job_id}/exports', json={'ocr': True})):
        assert response.status_code == 409
        assert 'Lite edition' in response.json()['detail'] and response.json()['feature'] == 'ocr'
    plain = client.get(f'/api/jobs/{job_id}/export')
    assert plain.status_code == 200 and plain.content.startswith(b'%PDF-')


def test_lite_refuses_auto_straighten_but_manual_straightening_works(client, monkeypatch):
    lite(monkeypatch)
    job_id, page_id = document(client)
    refused = client.post(f'/api/jobs/{job_id}/pages/{page_id}/auto-straighten')
    assert refused.status_code == 409 and refused.json()['feature'] == 'auto_straighten'
    edited = client.patch(f'/api/jobs/{job_id}/pages/{page_id}', json={'deskew': 2.5, 'brightness': 10})
    assert edited.status_code == 200
    assert client.get(f'/api/jobs/{job_id}/pages/{page_id}/image').status_code == 200


def test_system_check_names_the_edition(client, monkeypatch):
    lite(monkeypatch)
    monkeypatch.setattr(diagnostics, 'run_tool', lambda args, timeout=6: (None, ''))
    report = client.get('/api/admin/diagnostics').json()
    by_id = {c['id']: c for group in report['sections'].values() for c in group}
    assert 'Lite edition' in by_id['version']['detail']
    assert 'Full edition adds it' in by_id['ocr']['detail']


def test_core_runs_without_opencv_or_numpy():
    """Lite installs neither; importing Folio and editing pages must not need them."""
    script = '''
import sys
sys.modules["cv2"] = None; sys.modules["numpy"] = None   # Any import of them now fails
from backend import app, features, processing
from PIL import Image
assert features.detect() == {"ocr": features.detect()["ocr"], "auto_straighten": False}
page = processing.transform_page(Image.new("RGB", (400, 300), "white"),
                                 {"rotation": 90, "deskew": 1.5, "crop": [.1, .1, .8, .8], "contrast": 20})
try:
    processing.find_deskew_angle(page)
except features.Unavailable as exc:
    print("unavailable:", exc.feature)
'''
    done = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith('unavailable: auto_straighten')


def test_build_files_offer_both_editions_from_one_version():
    dockerfile = (ROOT / 'Dockerfile').read_text()
    compose = (ROOT / 'compose.yaml').read_text()
    assert re.search(r'ARG FOLIO_VERSION=([\d.]+)', dockerfile).group(1) == __version__
    assert 'ARG FOLIO_EDITION=full' in dockerfile
    assert f'folio-scan-studio:{__version__}-${{FOLIO_EDITION:-full}}' in compose
    # Lite must not pull in the Full-only Python packages.
    core = (ROOT / 'requirements.txt').read_text().lower()
    assert 'opencv' not in core and 'numpy' not in core
    assert 'opencv-python-headless' in (ROOT / 'requirements-full.txt').read_text()


def test_page_assets_are_fingerprinted_so_upgrades_are_never_served_stale(client, monkeypatch, tmp_path):
    page = client.get('/')
    assert page.headers['cache-control'] == 'no-cache'
    version = web.asset_version()
    assert f'/assets/app.js?v={version}' in page.text and f'/assets/style.css?v={version}' in page.text
    assert client.get(f'/assets/app.js?v={version}').headers['cache-control'] == 'public, max-age=31536000, immutable'
    assert client.get('/assets/app.js').headers['cache-control'] == 'no-cache'
    # Any change to the script or styles produces a new address.
    import shutil
    copy = tmp_path / 'frontend'
    shutil.copytree(web.WEB_DIR, copy)
    monkeypatch.setattr(web, 'WEB_DIR', copy)
    assert web.asset_version() == version
    (copy / 'app.js').write_text((copy / 'app.js').read_text() + '\n// changed\n')
    assert web.asset_version() != version
