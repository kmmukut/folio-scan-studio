"""Shared test setup: isolate all state from the host and from other tests."""
import os
import tempfile

# Must run before backend modules are imported: they read these at import time.
# Never let a test touch a real /data volume.
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='folio-test-data-'))
# Folio ships with no default CUPS server; API tests stub one placeholder server.
os.environ.setdefault('CUPS_SERVERS', 'cups.test:631')
# Behaviour tests stub CUPS/SANE and change their answers mid-test; caching
# is exercised explicitly in test_foundations_v096.py.
os.environ.setdefault('FOLIO_DEVICE_CACHE', 'off')

import pytest  # noqa: E402


def pytest_collection_modifyitems(config, items):
    from backend import features
    if features.available('auto_straighten'):
        return
    skip = pytest.mark.skip(reason='Lite edition: OpenCV/NumPy not installed')
    for item in items:
        if 'full_only' in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def ocr_available(monkeypatch):
    """For tests that replace OCRmyPDF with a stand-in: report OCR as installed."""
    from backend import features
    detected = {**features.detect(), 'ocr': True}
    monkeypatch.setattr(features, 'detect', lambda: detected)


@pytest.fixture(autouse=True)
def _reset_device_cache():
    from backend import devices
    devices.invalidate()
    yield
    devices.invalidate()
