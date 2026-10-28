"""v0.20.2: the image's main Python libraries stay on their updated versions,
and pip, needed only to build the image, isn't left in it."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pinned(text: str, name: str) -> tuple[int, ...]:
    found = re.search(rf'^{re.escape(name)}==([\d.]+)', text, re.M)
    assert found, f'{name} is not pinned'
    return tuple(int(part) for part in found.group(1).split('.'))


def test_the_main_libraries_stay_on_their_updated_versions():
    requirements = (ROOT / 'requirements.txt').read_text()
    assert pinned(requirements, 'pillow') >= (13, 0, 0)
    assert pinned(requirements, 'pymupdf') >= (2, 0)
    assert pinned((ROOT / 'requirements-full.txt').read_text(), 'opencv-python-headless') >= (5, 0, 0)


def test_pip_builds_the_image_but_does_not_ship_in_it():
    dockerfile = (ROOT / 'Dockerfile').read_text()
    version = re.search(r'pip install --no-cache-dir pip==([\d.]+)', dockerfile)
    assert version and tuple(map(int, version.group(1).split('.'))) >= (26, 2, 0)
    # A current pip installs the libraries, then is removed (the start-up check confirms it's gone).
    assert dockerfile.index('pip==') < dockerfile.index('-r "$(') < dockerfile.index('pip uninstall --yes --quiet pip')
    assert 'python -m ensurepip' in (ROOT / '.github' / 'scripts' / 'test-image.sh').read_text()
