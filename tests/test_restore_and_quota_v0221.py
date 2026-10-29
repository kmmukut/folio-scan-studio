"""v0.22.1: two restores can't share an upload; a backup must fit on the disk
once unpacked; two prints sent together can't both fit in the pages left;
and the docs say page limits cover printing through Folio only."""
from __future__ import annotations

import collections
import io
import shutil
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import app as web, backup, tasks  # noqa: E402
import test_first_minutes_v016 as v016  # noqa: E402
import test_quotas_v018 as v018  # noqa: E402

installed, office = v016.installed, v018.office  # A Folio in use; one with a printer and a member
Usage = collections.namedtuple('Usage', 'total used free')
MB = 1024 * 1024


def uploads_left() -> list[Path]:
    return [*web.DATA_DIR.glob('.restore-upload*'), *tasks.runner.root.glob('.restore-upload*')]


def titles(admin) -> set[str]:
    return {job['title'] for job in admin.get('/api/jobs').json()}


def test_two_restores_never_share_an_upload(installed):
    _, admin, _ = installed
    first = v016.take_backup(admin)
    admin.post('/api/jobs', json={'title': 'Added later'})
    second = v016.take_backup(admin)
    release = threading.Event()  # Keep the export lane busy, so the first restore waits in the queue
    blocker = tasks.runner.submit('test', 'export', 'user:test', lambda task: release.wait(20))
    try:
        waiting = admin.post('/api/admin/restore', content=first)
        assert waiting.status_code == 202, waiting.text
        task = waiting.json()['task']
        assert task['status'] == 'queued'
        own = tasks.runner.root / task['id'] / 'backup.tar.gz'  # Its upload is in its own folder
        assert own.read_bytes() == first and uploads_left() == []
        refused = admin.post('/api/admin/restore', content=second)
        assert refused.status_code == 429
        assert refused.json()['detail'] == 'Another restore is already waiting or running. Wait for it to finish.'
        assert own.read_bytes() == first and uploads_left() == []
    finally:
        release.set()
    assert v016.follow(admin, task)['status'] == 'done'
    assert 'Added later' not in titles(admin)  # The first backup, not the second, was restored
    assert not own.exists()
    later = admin.post('/api/admin/restore', content=second)  # Once it's done, another may start
    assert later.status_code == 202 and v016.follow(admin, later.json()['task'])['status'] == 'done'
    assert 'Added later' in titles(admin) and uploads_left() == []
    assert blocker.status == 'done'


def big_backup(unpacked: int) -> bytes:
    """A backup that compresses to almost nothing and unpacks to ``unpacked`` bytes."""
    good = {'format': backup.FORMAT, 'folio_version': '0.22.0'}
    archive = v016.make_archive({}, good)
    data = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as source, \
            tarfile.open(fileobj=data, mode='w:gz') as target:
        for member in source.getmembers():
            target.addfile(member, source.extractfile(member) if member.isfile() else None)
        info = tarfile.TarInfo('data/jobs/x/pages/big.png')
        info.size = unpacked
        target.addfile(info, io.BytesIO(bytes(unpacked)))
    return data.getvalue()


def test_a_backup_must_fit_on_the_disk_once_unpacked(installed, monkeypatch):
    _, admin, _ = installed
    archive = big_backup(30 * MB)
    assert len(archive) < MB  # Small to upload
    monkeypatch.setattr(backup, 'ROOM', 5 * MB)
    monkeypatch.setattr(shutil, 'disk_usage', lambda path: Usage(10_000 * MB, 0, 20 * MB))
    before = titles(admin)
    refused = admin.post('/api/admin/restore', content=archive)
    assert refused.status_code == 400
    assert refused.json()['detail'] == ('This backup unpacks to 31 MB, but only 16 MB can be used. '
                                        'Make room on the disk, then try again.')
    assert titles(admin) == before and not (web.DATA_DIR / '.restore-staging').exists() and uploads_left() == []
    # Checked again when the restore runs: the disk may have filled up since
    upload = tasks.runner.root / 'backup.tar.gz'
    upload.write_bytes(archive)
    with pytest.raises(backup.BackupError, match='unpacks to 31 MB'):
        backup.restore(upload, web.DATA_DIR, current_version='0.22.1', conn=None)
    assert titles(admin) == before
    # With room enough, the same backup is fine; a description file that's too big isn't
    assert backup.inspect(upload, '0.22.1', room=100 * MB)['format'] == backup.FORMAT
    monkeypatch.setattr(backup, 'MANIFEST_LIMIT', 10)
    with pytest.raises(backup.BackupError, match='not a Folio backup'):
        backup.inspect(upload, '0.22.1')


def direct_print(client, pages: int):
    return client.post('/api/print', headers={'X-Upload-Filename': 'a.pdf', 'Content-Type': 'application/pdf'},
                       content=v018.pdf(pages))


def test_two_prints_sent_together_cant_both_fit(office, monkeypatch):
    admin, alice, sent = office
    admin.put('/api/admin/quota-default', json={'monthly_pages': 5})
    fake = web.print_with_cups

    def slow_print(path, title, **choices):  # Sending takes a moment, as with a real print server
        time.sleep(.4)
        return fake(path, title, **choices)
    monkeypatch.setattr(web, 'print_with_cups', slow_print)
    answers = []
    sending = [threading.Thread(target=lambda: answers.append(direct_print(alice, 4))) for _ in range(2)]
    for thread in sending:
        thread.start()
    for thread in sending:
        thread.join(20)
    assert sorted(a.status_code for a in answers) == [200, 403]  # 4 + 4 pages, with 5 left
    assert len(sent) == 1 and alice.get('/api/print/quota').json() == {'limit': 5, 'used': 4, 'left': 1}
    # A print the print server refuses isn't counted, and doesn't hold up the next one
    def refused(*args, **kwargs):
        raise HTTPException(502, 'The print server did not accept the job')
    monkeypatch.setattr(web, 'print_with_cups', refused)
    assert direct_print(alice, 1).status_code == 502
    assert alice.get('/api/print/quota').json()['left'] == 1
    monkeypatch.setattr(web, 'print_with_cups', fake)
    assert direct_print(alice, 1).status_code == 200
    assert alice.get('/api/print/quota').json()['left'] == 0


def test_the_docs_say_what_page_limits_cover():
    page = (ROOT / 'frontend' / 'index.html').read_text()
    assert 'Limits apply to printing through Folio; prints sent straight to the print server' in page
    assert "AirPrint and\n  other prints sent straight to the print server aren't limited." in (ROOT / 'README.md').read_text()
    guide = (ROOT / 'docs' / 'DEPLOYMENT.md').read_text()
    assert "Folio's page limits don't apply" in guide and 'leave port 631 unpublished' in guide
