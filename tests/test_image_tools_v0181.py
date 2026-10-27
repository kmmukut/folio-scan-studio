"""Checks of the tools inside the Folio image itself (skipped elsewhere).

0.15 added HP's printer drivers, whose SANE plug-in aborted every scanner
listing in a container; tests that simulate scanimage could not notice.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('scanimage'), reason='runs inside the Folio image, where SANE is installed')
def test_listing_scanners_does_not_crash():
    listing = subprocess.run(['scanimage', '-L'], capture_output=True, text=True, timeout=60)
    assert listing.returncode == 0, listing.stderr[-500:]
