"""Regression: input trays are visible and work from uploaded-file print flow."""
import subprocess
from pathlib import Path
from backend import devices


def test_inputslot_without_ppd_nickname(monkeypatch):
    # Older queues need not show InputSlot/Paper Source; a bare InputSlot
    # is valid and should not be skipped.
    monkeypatch.setattr(devices,'cups_run',lambda *a,**k: subprocess.CompletedProcess(
        a[0],0,'InputSlot: *AutoSelect Tray1 Tray2 ManualFeed\nPageSize: *Letter A4\n',''))
    caps=devices.cups_capabilities('cups-host:631','GenericLaser')
    assert caps['source_option']=='InputSlot'
    assert [opt['value'] for opt in caps['paper_sources']]==[
        'AutoSelect','Tray1','Tray2','ManualFeed']


def test_guest_and_member_drop_zones_have_visible_source_controls():
    page=(Path(__file__).resolve().parents[1]/'frontend/index.html').read_text()
    js='\n'.join(p.read_text() for p in sorted((Path(__file__).resolve().parents[1]/'frontend/js').glob('*.js')))
    for key, chosen in [('guest','chosen-file'),('member','member-chosen-file')]:
        first=page.index('id="'+chosen+'"')
        tray=page.index('id="'+key+'-source"')
        preview=page.index('id="'+key+'-file-preview"')
        assert first < tray < preview  # not beneath a long PDF preview
        assert page.count('id="'+key+'-source"')==1
        assert 'id="'+key+'-source-refresh"' in page
        assert 'id="'+key+'-source-hint"' in page
    assert "'X-Print-Source':$(`#${prefix}-source`).value" in js
    assert 'No individual paper trays reported by CUPS' in js
