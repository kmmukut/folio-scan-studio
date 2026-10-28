"""v0.20.1: Folio can be installed as an app (a progressive web app)."""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import app as web  # noqa: E402

client = TestClient(web.app)


def test_the_manifest_describes_an_installable_app():
    answer = client.get('/manifest.webmanifest')  # No sign-in needed: browsers fetch it on their own
    assert answer.status_code == 200 and answer.headers['content-type'] == 'application/manifest+json'
    assert answer.headers['cache-control'] == 'no-cache'
    manifest = json.loads(answer.text)
    assert manifest['short_name'] == 'Folio' and manifest['display'] == 'standalone'
    assert manifest['start_url'] == manifest['scope'] == manifest['id'] == '/'
    # Chrome needs a 192 and a 512 px icon; phones that crop icons need a "maskable" one.
    icons = {(icon['sizes'], icon['purpose']): icon['src'] for icon in manifest['icons']}
    assert {('192x192', 'any'), ('512x512', 'any'), ('512x512', 'maskable')} <= set(icons)
    for (sizes, _), src in icons.items():
        served = client.get(src)
        assert served.status_code == 200 and served.headers['content-type'] == 'image/png'
        assert '%dx%d' % Image.open(io.BytesIO(served.content)).size == sizes


def test_the_page_links_the_manifest_and_icons():
    page = client.get('/').text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    for href in re.findall(r'<link rel="(?:icon|apple-touch-icon)"[^>]*href="([^"]+)"', page):
        assert client.get(href).status_code == 200
    assert 'apple-touch-icon' in page and '<meta name="apple-mobile-web-app-title" content="Folio">' in page
    assert Image.open(io.BytesIO(client.get('/assets/icons/apple-touch-icon.png').content)).size == (180, 180)


def test_the_service_worker_keeps_no_copies():
    answer = client.get('/sw.js')
    assert answer.status_code == 200 and answer.headers['content-type'].startswith('text/javascript')
    assert answer.headers['cache-control'] == 'no-cache'  # Upgrades reach it at once
    script = answer.text
    # Nothing stored: no Cache Storage, no IndexedDB. Only page loads that fail get the "can't be reached" page.
    assert 'caches' not in script and 'indexedDB' not in script
    assert "event.request.mode !== 'navigate'" in script and 'can’t be reached' in script
    app_js = (ROOT / 'frontend' / 'js' / 'main.js').read_text()
    assert "window.isSecureContext)navigator.serviceWorker.register('/sw.js')" in app_js
