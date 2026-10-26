"""Hardware-free API contract tests with simulated sane-airscan batch output."""
import importlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
appmod = importlib.import_module('backend.app')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(appmod, 'DEVICE', 'airscan:w0:Samsung M2885FW')
    monkeypatch.setattr(appmod, 'CUPS_QUEUE', 'SamsungAirPrint')
    from backend import devices
    monkeypatch.setattr(devices,'list_cups_queues', lambda server:[
        {'queue':'SamsungAirPrint','ready':True,'status':'idle'}])
    monkeypatch.setattr(devices,'cups_capabilities', lambda *a: {
        'known':False,'media':[],'duplex':None,'color':None,'color_modes':['auto','color','gray','bw']})
    monkeypatch.setattr(appmod, 'DATA_DIR', tmp_path)
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', 'a-test-password-that-is-long-enough')

    from backend import security
    security._guest_submissions.clear()
    security._global_submissions.clear()
    appmod.scan_cancel_event.clear()
    appmod.scan_state.update(active=False, error=None, pages_added=0, cancel_requested=False)

    def fake_detect(command, **kwargs):
        assert command == ['scanimage', '-L']
        return subprocess.CompletedProcess(command, 0, "device `airscan:w0:Samsung M2885FW' is a WSD Samsung M2885FW", '')

    def fake_scanimage(command, cwd, stop_event):
        assert command and command[0] == 'scanimage'
        source = command[command.index('--source') + 1]
        count = int(command[command.index('--batch-count') + 1]) if '--batch-count' in command else 2
        if source == 'Flatbed':
            count = 1
        for n in range(1, count + 1):
            im = Image.new('RGB', (120, 160), (255, 210 - n*5, 160))
            im.save(Path(cwd) / f'page-{n:04d}.png')
        return 0, None

    monkeypatch.setattr(appmod.subprocess, 'run', fake_detect)
    monkeypatch.setattr(appmod, 'run_scanner_process', fake_scanimage)
    with TestClient(appmod.app) as test:
        # No document or scanner access before logging in.
        assert test.get('/api/jobs').status_code == 401
        assert test.get('/api/scanner').status_code == 401
        result = test.post('/api/auth/login', json={'username':'admin','password':'a-test-password-that-is-long-enough'})
        assert result.status_code == 200, result.text
        csrf = result.json()['csrf']
        test.headers.update({'X-CSRF-Token':csrf})
        yield test


def create(client):
    response = client.post('/api/jobs', json={'title': 'Test scan'})
    assert response.status_code == 201, response.text
    return response.json()['id']


def scan_and_wait(client, job_id, mode='Color', count=2, role='normal'):
    response = client.post(f'/api/jobs/{job_id}/scan', json={'source':'ADF', 'mode':mode, 'resolution':150, 'count':count, 'role':role})
    assert response.status_code == 202, response.text
    for _ in range(100):
        status = client.get('/api/scan').json()
        if not status['active']:
            assert status['error'] is None, status
            break
        time.sleep(.025)
    else:
        pytest.fail('Scan worker did not finish')
    return client.get(f'/api/jobs/{job_id}').json()


def test_create_scan_reorder_and_undo(client):
    assert client.get('/').status_code == 200
    assert client.get('/api/scanner').json()['connected'] is True
    jid = create(client)
    job = scan_and_wait(client, jid)
    assert len(job['pages']) == 2
    assert len(job['batches']) == 1
    first, second = (p['id'] for p in job['pages'])
    preview = client.get(f'/api/jobs/{jid}/pages/{first}/image?thumb=true')
    assert preview.status_code == 200 and preview.headers['content-type'] == 'image/jpeg'
    edit = client.patch(f'/api/jobs/{jid}/pages/{first}',json={'rotation':90, 'export_mode':'Gray'})
    assert edit.json()['pages'][0]['rotation'] == 90
    order = client.post(f'/api/jobs/{jid}/reorder',json={'ids':[second, first]})
    assert order.json()['pages'][0]['id'] == second
    deleted = client.delete(f'/api/jobs/{jid}/pages/{second}')
    assert len(deleted.json()['pages']) == 1
    restored = client.post(f'/api/jobs/{jid}/undo-delete')
    assert len(restored.json()['pages']) == 2
    assert [p['id'] for p in restored.json()['pages']] == [second, first]  # restored at deleted index
    duplicate = client.post(f'/api/jobs/{jid}/pages/{first}/duplicate')
    assert len(duplicate.json()['pages']) == 3
    assert len({p['id'] for p in duplicate.json()['pages']}) == 3


def test_duplex_and_mixed_modes(client):
    jid = create(client)
    front = scan_and_wait(client,jid,'Color',2,'fronts')
    back = scan_and_wait(client,jid,'Gray',2,'backs')
    assert len(back['pages']) == 4
    assert back['pages'][2]['scanned_mode'] == 'Gray'
    ordered = client.post(f'/api/jobs/{jid}/interleave',json={
        'front_batch_id': front['batches'][0]['id'],
        'back_batch_id': back['batches'][1]['id'],
        'reverse_backs':True,
    })
    assert ordered.status_code == 200, ordered.text
    ids=[p['id'] for p in ordered.json()['pages']]
    assert ids == [back['pages'][0]['id'], back['pages'][3]['id'],back['pages'][1]['id'],back['pages'][2]['id']]
    bad = client.post(f'/api/jobs/{jid}/interleave',json={
        'front_batch_id':front['batches'][0]['id'],
        'back_batch_id':front['batches'][0]['id'], 'reverse_backs':True,
    })
    assert bad.status_code==400
    wrong_order = client.post(f'/api/jobs/{jid}/reorder',json={'ids':[ids[0]]})
    assert wrong_order.status_code == 400


def test_export_pdf(client):
    pytest.importorskip('img2pdf')
    jid=create(client)
    scan_and_wait(client,jid)
    resp=client.get(f'/api/jobs/{jid}/export?quality=balanced')
    assert resp.status_code==200,resp.text
    assert resp.headers['content-type']=='application/pdf'
    assert resp.content.startswith(b'%PDF-')


def test_feeder_error_preserves_finished_pages(client, monkeypatch):
    def partial_scan(command, cwd, stop_event):
        Image.new('RGB', (60, 80), '#ffffff').save(Path(cwd) / 'page-0001.png')
        return 1, 'Document feeder is empty'
    monkeypatch.setattr(appmod, 'run_scanner_process', partial_scan)
    jid = create(client)
    resp = client.post(f'/api/jobs/{jid}/scan', json={
        'source':'ADF', 'mode':'Color', 'resolution':150, 'count':2,
    })
    assert resp.status_code==202
    for _ in range(100):
        if not client.get('/api/scan').json()['active']:
            break
        time.sleep(.025)
    status = client.get('/api/scan').json()
    assert status['pages_added'] == 1
    assert status['error'] is None
    assert len(client.get(f'/api/jobs/{jid}').json()['pages']) == 1


def test_cancel_endpoint_preserves_completed_pages(client, monkeypatch):
    scan_started = threading.Event()

    def waiting_scan(command, cwd, stop_event):
        Image.new('RGB', (60, 80), '#ffffff').save(Path(cwd) / 'page-0001.png')
        scan_started.set()
        assert stop_event.wait(timeout=2), 'Cancellation was not sent'
        return -2, 'Cancelled by user'

    monkeypatch.setattr(appmod, 'run_scanner_process', waiting_scan)
    jid = create(client)
    response = client.post(f'/api/jobs/{jid}/scan', json={
        'source': 'ADF', 'mode': 'Color', 'resolution': 150, 'count': 4,
    })
    assert response.status_code == 202
    assert scan_started.wait(timeout=2)
    response = client.post('/api/scan/cancel')
    assert response.status_code == 202
    assert response.json()['cancel_requested'] is True
    for _ in range(100):
        if not client.get('/api/scan').json()['active']:
            break
        time.sleep(.025)
    status = client.get('/api/scan').json()
    assert status['active'] is False
    assert status['cancelled'] is True
    assert status['pages_added'] == 1
    assert len(client.get(f'/api/jobs/{jid}').json()['pages']) == 1
    assert client.post('/api/scan/cancel').status_code == 409


def test_graceful_sigint_to_scanimage(tmp_path, monkeypatch):
    """A fake scanimage-like child should receive exactly one SIGINT."""
    script = tmp_path / 'fake-scanner.py'
    script.write_text('''import pathlib, signal, sys, time
marker = pathlib.Path(sys.argv[1])
def cancel(signum, frame):
    marker.with_suffix('.signal').write_text(str(signum))
    sys.exit(130)
signal.signal(signal.SIGINT, cancel)
marker.touch()
while True:
    time.sleep(.05)
''')
    marker = tmp_path / 'ready'
    stopped = threading.Event()
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(appmod.run_scanner_process(
        [sys.executable, str(script), str(marker)], tmp_path, stopped)))
    thread.start()
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(.025)
    assert marker.exists(), 'Child process never started'
    stopped.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), 'Graceful cancellation hung'
    assert 'Cancelled by user' in outcome[0][1]
    assert marker.with_suffix('.signal').read_text() == '2'


def test_unresponsive_process_gets_force_stopped(tmp_path, monkeypatch):
    """A child that ignores SIGINT must be killed after the grace period."""
    script = tmp_path / 'fake-hung-scanner.py'
    script.write_text('''import pathlib, signal, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).touch()
while True:
    time.sleep(.05)
''')
    marker = tmp_path / 'ready'
    monkeypatch.setattr(appmod, 'SCAN_GRACE_SECONDS', 0.25)
    stopped = threading.Event()
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(appmod.run_scanner_process(
        [sys.executable, str(script), str(marker)], tmp_path, stopped)))
    thread.start()
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(.025)
    assert marker.exists(), 'Child process never started'
    stopped.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), 'Force-stop fallback hung'
    assert 'force-stopped' in outcome[0][1]


def test_non_destructive_crop_resize_and_reset(client):
    jid = create(client)
    scan_and_wait(client,jid,count=1)
    job = client.get(f'/api/jobs/{jid}').json()
    p = job['pages'][0]
    original = client.get(f'/api/jobs/{jid}/pages/{p["id"]}/image')
    from io import BytesIO
    original_size = Image.open(BytesIO(original.content)).size
    edited = client.patch(f'/api/jobs/{jid}/pages/{p["id"]}', json={
        'crop':[0.1,0.2,0.5,0.5], 'scale':150, 'deskew':1.5,
    })
    assert edited.status_code == 200, edited.text
    assert edited.json()['pages'][0]['crop'] == [0.1,0.2,0.5,0.5]
    image = client.get(f'/api/jobs/{jid}/pages/{p["id"]}/image')
    edited_size=Image.open(BytesIO(image.content)).size
    assert edited_size[0] < original_size[0]
    assert edited_size[1] < original_size[1]
    bad=client.patch(f'/api/jobs/{jid}/pages/{p["id"]}',json={'crop':[.8,.7,.5,.5]})
    assert bad.status_code == 400
    reset=client.patch(f'/api/jobs/{jid}/pages/{p["id"]}',json={
        'crop': None, 'reset_crop':True, 'scale':100, 'deskew':0,
    })
    assert reset.status_code==200
    assert reset.json()['pages'][0]['crop'] is None
    assert reset.json()['pages'][0]['scale'] == 100
    assert reset.json()['pages'][0]['deskew'] == 0
    # Immutability: file bytes haven't changed.
    src=appmod.JOBS_DIR/jid/'pages'/p['file']
    with Image.open(src) as im:
        assert im.size == (120,160)


@pytest.mark.full_only
def test_auto_straighten_and_old_document_defaults(client, monkeypatch):
    from PIL import ImageDraw
    from backend.processing import find_deskew_angle
    page=Image.new('RGB',(900,1200),'white')
    draw=ImageDraw.Draw(page)
    for i in range(15):
        y=130+i*50
        draw.rectangle((115,y,760,y+5), fill='black')
    tilted=page.rotate(5,expand=True,fillcolor='white')
    correction, evidence=find_deskew_angle(tilted)
    assert evidence >= 3, (correction,evidence)
    assert abs(correction + 5)<2, (correction,evidence)
    jid=create(client)
    j=scan_and_wait(client,jid,count=1)
    first=j['pages'][0]
    # Simulate v0.2 metadata without new keys.
    with appmod.lock:
        j=appmod.load_job(jid)
        for key in ('crop','deskew','scale'):
            j['pages'][0].pop(key,None)
        appmod.save_job(j)
    resp=client.get(f'/api/jobs/{jid}/pages/{first["id"]}/image')
    assert resp.status_code==200
    auto=client.post(f'/api/jobs/{jid}/pages/{first["id"]}/auto-straighten')
    assert auto.status_code == 200
    assert 'angle' in auto.json()


def test_login_csrf_session_protection_and_password_change(client):
    from fastapi.testclient import TestClient
    assert client.get('/api/auth/status').json()['authenticated'] is True
    token=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/jobs',json={'title':'unauthorized mutation'}).status_code == 403
    client.headers['X-CSRF-Token']=token
    assert client.post('/api/jobs',json={'title':'allowed mutation'}).status_code == 201
    change=client.post('/api/auth/password',json={
        'current_password':'a-test-password-that-is-long-enough',
        'new_password':'a-different-test-password-for-login'
    })
    assert change.status_code==200
    assert client.get('/api/jobs').status_code==401
    # Restore original password for the next test (each uses own tmp database).
    again=client.post('/api/auth/login',json={'username':'admin','password':'a-different-test-password-for-login'})
    assert again.status_code==200
    client.headers['X-CSRF-Token']=again.json()['csrf']
    assert client.get('/api/jobs').status_code==200


def test_guest_print_is_disabled_by_default_and_restricts_scanner(client, monkeypatch):
    from pypdf import PdfWriter
    from io import BytesIO
    import os
    jid=create(client)
    scan_and_wait(client,jid,count=1)
    # Logged-out guest can never read scanned page even with a known ID.
    guest = TestClient(appmod.app)
    assert guest.get('/api/jobs').status_code==401
    assert guest.get(f'/api/jobs/{jid}').status_code==401
    assert guest.post('/api/scan/cancel').status_code==401
    public=guest.get('/api/guest/status')
    assert public.json()['enabled'] is False
    pdf_writer=PdfWriter();pdf_writer.add_blank_page(width=612,height=792)
    buffer=BytesIO();pdf_writer.write(buffer)
    content=buffer.getvalue()
    options={'headers':{'X-Guest-CSRF':public.json()['csrf'],
                        'X-Upload-Filename':'sample.pdf','Content-Type':'application/pdf'},'content':content}
    assert guest.post('/api/guest/print',**options).status_code==403
    enabled=client.post('/api/admin/guest',json={'enabled':True})
    assert enabled.status_code==200
    public=guest.get('/api/guest/status')
    assert public.json()['enabled'] is True
    options['headers']['X-Guest-CSRF']=public.json()['csrf']
    logged=[]
    def mock_cups(file_path,title,**choices):
        logged.append((file_path.read_bytes(),title));return 'request id SamsungAirPrint-4'
    monkeypatch.setattr(appmod,'print_with_cups',mock_cups)
    # Missing csrf must be rejected.
    bad_headers={**options['headers'],'X-Guest-CSRF':'wrong-token'}
    assert guest.post('/api/guest/print',headers=bad_headers,content=content).status_code==403
    sent=guest.post('/api/guest/print',**options)
    assert sent.status_code==200, sent.text
    assert logged and logged[0][0].startswith(b'%PDF-')
    assert guest.get('/api/scanner').status_code==401
    assert guest.post('/api/jobs',json={}).status_code==401
    assert guest.post('/api/guest/print',headers={**options['headers'],'X-Upload-Filename':'script.sh'},content=b'echo bad').status_code == 400
    # Guest setting persists and can be turned off again.
    disabled=client.post('/api/admin/guest',json={'enabled':False})
    assert disabled.status_code==200
    assert guest.post('/api/guest/print',**options).status_code==403


def test_guest_page_limit(client,monkeypatch):
    from pypdf import PdfWriter
    from io import BytesIO
    client.post('/api/admin/guest',json={'enabled':True})
    guest=TestClient(appmod.app)
    public=guest.get('/api/guest/status')
    writer=PdfWriter()
    for _ in range(21):writer.add_blank_page(width=612,height=792)
    data=BytesIO();writer.write(data)
    resp=guest.post('/api/guest/print', headers={
        'X-Guest-CSRF':public.json()['csrf'], 'X-Upload-Filename':'21pages.pdf',
        'Content-Type':'application/pdf'},content=data.getvalue())
    assert resp.status_code == 413


def test_new_user_accounts_roles_and_migration(client, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import security
    from io import BytesIO
    from pypdf import PdfWriter
    # An administrator can create accounts, and the server restricts by role.
    create_member = client.post('/api/admin/users', json={
        'username':'alex', 'password':'member-password-that-is-long', 'role':'member'})
    assert create_member.status_code == 201, create_member.text
    assert any(u['username']=='alex' and u['role']=='member' for u in create_member.json())
    admin_job = create(client)
    scan_and_wait(client,admin_job,count=1)
    member=TestClient(appmod.app)
    signed=member.post('/api/auth/login',json={
        'username':'alex','password':'member-password-that-is-long'})
    assert signed.status_code==200 and signed.json()['role']=='member'
    member.headers['X-CSRF-Token']=signed.json()['csrf']
    assert member.get('/api/jobs').json() == []
    assert member.get(f'/api/jobs/{admin_job}').status_code == 404
    assert member.get(f'/api/jobs/{admin_job}/pages/{client.get(f"/api/jobs/{admin_job}").json()["pages"][0]["id"]}/image').status_code==404
    assert member.post('/api/admin/guest',json={'enabled':True}).status_code == 403
    assert member.get('/api/admin/users').status_code == 403
    own=member.post('/api/jobs',json={'title':'Private draft'})
    assert own.status_code==201
    assert own.json()['owner']=='alex'
    assert len(member.get('/api/jobs').json())==1
    assert len(client.get('/api/jobs').json())==2  # admin can see all
    assert member.delete(f'/api/jobs/{admin_job}').status_code == 404
    # Print-only account has no scanner or document access, even if guest printing off.
    made = client.post('/api/admin/users',json={
        'username':'visitor1','password':'printing-password-is-long','role':'printer'})
    assert made.status_code == 201
    printer=TestClient(appmod.app)
    logged=printer.post('/api/auth/login',json={'username':'visitor1','password':'printing-password-is-long'})
    assert logged.status_code==200 and logged.json()['role']=='printer'
    printer.headers['X-CSRF-Token']=logged.json()['csrf']
    assert printer.get('/api/jobs').status_code==403
    assert printer.get('/api/scanner').status_code==403
    assert printer.get('/api/admin/users').status_code==403
    assert printer.get('/api/auth/status').json()['authenticated'] is True
    # Printer-only can print with authenticated endpoint; mock external CUPS.
    writer=PdfWriter();writer.add_blank_page(width=400,height=500)
    buf=BytesIO();writer.write(buf)
    submitted=[]
    monkeypatch.setattr(appmod,'print_with_cups',lambda path,title,**choices: submitted.append(path.read_bytes()) or 'request id queued')
    print_job=printer.post('/api/print',headers={'X-Upload-Filename':'file.pdf','Content-Type':'application/pdf'},content=buf.getvalue())
    assert print_job.status_code==200,print_job.text
    assert submitted[0].startswith(b'%PDF-')
    # Admin disables account -> immediate session invalidation.
    disabled=client.patch('/api/admin/users/alex',json={'enabled':False})
    assert disabled.status_code==200
    assert member.get('/api/jobs').status_code==401
    assert member.post('/api/auth/login',json={'username':'alex','password':'member-password-that-is-long'}).status_code==401
    assert client.delete('/api/admin/users/admin').status_code==400
    assert client.delete('/api/admin/users/alex').status_code==200
    # Scans from a removed account are transferred to the administrator,
    # preventing username reuse from revealing someone else's documents.
    assert client.get(f'/api/jobs/{own.json()["id"]}').json()['owner']=='admin'
    assert client.post('/api/admin/users',json={
        'username':'alex','password':'a-new-person-strong-password','role':'member'}).status_code==201
    newmember=TestClient(appmod.app)
    relog=newmember.post('/api/auth/login',json={'username':'alex','password':'a-new-person-strong-password'})
    assert relog.status_code==200
    assert newmember.get(f'/api/jobs/{own.json()["id"]}').status_code==404


def test_document_deletion_rejects_active_job(client):
    jid=create(client)
    scan_and_wait(client,jid,count=2)
    directory=appmod.JOBS_DIR/jid
    assert directory.is_dir()
    appmod.scan_state.update(active=True,job_id=jid)
    refused=client.delete(f'/api/jobs/{jid}')
    assert refused.status_code==409
    assert directory.exists()
    appmod.scan_state.update(active=False,job_id=None)
    deleted=client.delete(f'/api/jobs/{jid}')
    assert deleted.status_code==200 and deleted.json()['deleted']==jid
    assert not directory.exists()
    assert client.get(f'/api/jobs/{jid}').status_code==404


@pytest.mark.full_only
def test_preview_is_bounded_and_auto_straighten_proposes_only(client):
    jid=create(client)
    job=scan_and_wait(client,jid,count=1)
    page=job['pages'][0]
    from io import BytesIO
    baseline=Image.open(BytesIO(client.get(f'/api/jobs/{jid}/pages/{page["id"]}/image').content)).size
    changed=client.patch(f'/api/jobs/{jid}/pages/{page["id"]}',json={'scale':200,'deskew':2})
    assert changed.status_code==200
    updated=Image.open(BytesIO(client.get(f'/api/jobs/{jid}/pages/{page["id"]}/image').content))
    assert updated.width<=1250 and updated.height<=1650
    # Scaling up export pixels must not cause large preview downloads.
    assert max(updated.size) <= max(baseline)+10  # rotation may expand a few pixels
    suggestion=client.post(f'/api/jobs/{jid}/pages/{page["id"]}/auto-straighten')
    assert suggestion.status_code==200
    stored=client.get(f'/api/jobs/{jid}').json()['pages'][0]
    assert stored['deskew']==2  # never silently overwrite manually set angle


def test_existing_v03_sqlite_schema_migrates_in_place(tmp_path, monkeypatch):
    import sqlite3
    from backend import security
    data=tmp_path/'original'
    data.mkdir()
    salt=b'_'*24
    digest=security.hash_password('an-original-password-that-is-valid',salt)
    conn=sqlite3.connect(data/'access.sqlite3')
    conn.execute('CREATE TABLE users (username TEXT PRIMARY KEY, salt BLOB NOT NULL, password_hash BLOB NOT NULL)')
    conn.execute('INSERT INTO users VALUES (?,?,?)',('admin',salt,digest))
    conn.commit();conn.close()
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD','should-not-replace-existing')
    security.init(data)
    assert security.list_users(data)[0]['role']=='admin'
    assert security.try_login(data,'admin','an-original-password-that-is-valid','test')[0]['role']=='admin'
    security.init(data)
    assert len(security.list_users(data))==1


@pytest.mark.full_only
def test_deskew_abstains_without_confident_text():
    from backend.processing import find_deskew_angle
    from PIL import ImageDraw
    blank=Image.new('RGB',(1000,1400),'white')
    assert find_deskew_angle(blank)[0]==0
    sparse=blank.copy();draw=ImageDraw.Draw(sparse)
    draw.line((200,100,500,400),fill='black',width=15)
    draw.rectangle((40,50,960,1350),outline='black',width=4)
    assert find_deskew_angle(sparse)[0]==0


def test_brightness_contrast_auto_levels_persist_without_touching_original(client):
    from io import BytesIO
    jid=create(client)
    job=scan_and_wait(client,jid,count=1)
    page=job['pages'][0]
    source=appmod.JOBS_DIR/jid/'pages'/page['file']
    before=source.read_bytes()
    edits={'brightness':20,'contrast':25,'auto_levels':True}
    saved=client.patch(f'/api/jobs/{jid}/pages/{page["id"]}',json=edits)
    assert saved.status_code==200,saved.text
    updated=saved.json()['pages'][0]
    assert {k:updated[k] for k in edits}==edits
    result=client.get(f'/api/jobs/{jid}/pages/{page["id"]}/image')
    assert result.status_code==200
    with Image.open(BytesIO(result.content)) as im:
        assert im.width>0 and im.height>0
    assert source.read_bytes()==before
    assert client.patch(f'/api/jobs/{jid}/pages/{page["id"]}',json={'brightness':81}).status_code==422
    assert client.patch(f'/api/jobs/{jid}/pages/{page["id"]}',json={'contrast':-81}).status_code==422
    reset=client.patch(f'/api/jobs/{jid}/pages/{page["id"]}',json={
        'brightness':0,'contrast':0,'auto_levels':False})
    assert reset.status_code==200
    assert reset.json()['pages'][0]['auto_levels'] is False


def test_tone_pipeline_changes_image_values():
    # Pillow only: tone editing is a core feature, available in the Lite edition too.
    from PIL import Image, ImageStat
    from backend.processing import transform_page
    base=Image.linear_gradient('L').rotate(90).resize((170,100)).point(lambda v: 40+v*170//256)
    stat=lambda im: ImageStat.Stat(im)
    lighter=transform_page(base.copy(), {'brightness':30})
    darker=transform_page(base.copy(), {'brightness':-30})
    assert stat(lighter).mean[0]>stat(base).mean[0]>stat(darker).mean[0]
    punchy=transform_page(base.copy(), {'contrast':40})
    assert stat(punchy).stddev[0]>stat(base).stddev[0]
    auto=transform_page(base.copy(),{'auto_levels':True})
    spread=lambda im: im.getextrema()[1]-im.getextrema()[0]
    assert spread(auto) >= spread(base) and spread(base) > 100


def test_searchable_export_option_calls_ocr_only_when_requested(client,monkeypatch,ocr_available):
    import io
    import sys
    import types
    from pypdf import PdfWriter
    jid=create(client)
    scan_and_wait(client,jid,count=1)
    pdf=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=612,height=792);writer.write(pdf)
    data=pdf.getvalue()
    # Mimic img2pdf in environments without the optional PDF encoder
    # (Folio streams its output to a file via outputstream=).
    monkeypatch.setitem(sys.modules,'img2pdf',types.SimpleNamespace(
        convert=lambda images, outputstream=None: outputstream.write(data) if outputstream else data))
    calls=[]
    def fake_ocr(source,target,page_count):
        calls.append(page_count)
        target.write_bytes(source.read_bytes())
    monkeypatch.setattr(appmod,'ocr_pdf',fake_ocr)
    standard=client.get(f'/api/jobs/{jid}/export?ocr=false')
    assert standard.status_code==200 and calls==[]
    searchable=client.get(f'/api/jobs/{jid}/export?ocr=true')
    assert searchable.status_code==200 and calls==[1]
    assert searchable.content.startswith(b'%PDF-')
    assert '-searchable.pdf' in searchable.headers['content-disposition']


def test_reset_view_restores_original_scanned_pixels_and_metadata(client):
    """Both Reset View buttons use this server operation; no rescan occurs."""
    import hashlib
    jid = create(client)
    job = scan_and_wait(client, jid, count=2)
    first, second = job['pages']
    endpoint = f'/api/jobs/{jid}/pages/{first["id"]}'
    preview_original = client.get(endpoint+'/image').content
    original_file = appmod.JOBS_DIR / jid / 'pages' / first['file']
    file_hash = hashlib.sha256(original_file.read_bytes()).hexdigest()

    edited = client.patch(endpoint, json={
        'rotation': 90, 'deskew': 2.25, 'crop': [0.1, 0.1, 0.75, 0.75],
        'scale': 125, 'brightness': 28, 'contrast': -20,
        'auto_levels': True, 'export_mode': 'Gray',
    })
    assert edited.status_code == 200, edited.text
    assert client.get(endpoint+'/image').content != preview_original
    assert hashlib.sha256(original_file.read_bytes()).hexdigest() == file_hash

    result = client.post(endpoint+'/restore-original')
    assert result.status_code == 200, result.text
    restored = result.json()['pages'][0]
    assert restored['id'] == first['id']
    assert restored['rotation'] == 0
    assert restored['deskew'] == 0
    assert restored['crop'] is None
    assert restored['scale'] == 100
    assert restored['brightness'] == 0
    assert restored['contrast'] == 0
    assert restored['auto_levels'] is False
    assert restored['export_mode'] == 'Original'
    assert result.json()['pages'][1]['id'] == second['id']
    assert client.get(endpoint+'/image').content == preview_original
    assert hashlib.sha256(original_file.read_bytes()).hexdigest() == file_hash
    # Safe and idempotent even when the page was already at its original state.
    assert client.post(endpoint+'/restore-original').status_code == 200
    # The original metadata and source image remain available after reset.
    assert client.get(endpoint+'/image').content == preview_original


def test_signed_in_print_has_no_guest_size_or_page_quota(client, monkeypatch):
    """Authenticated printing is not accidentally routed through guest limits."""
    from io import BytesIO
    from pypdf import PdfWriter, PdfReader
    from backend import security
    writer=PdfWriter()
    for _ in range(26):
        writer.add_blank_page(width=612,height=792)
    pdf=BytesIO();writer.write(pdf)
    sent=[]
    def mock_cups(path, title, **options):
        sent.append((path.read_bytes(), title, options))
        return 'request id is SamsungAirPrint-105 (1 file(s))'
    monkeypatch.setattr(appmod,'print_with_cups',mock_cups)
    monkeypatch.setattr(appmod,'GUEST_MAX_BYTES', 500)  # lower guest cap only
    monkeypatch.setattr(security,'guest_rate_allowed',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('Guest quota consulted for user')))
    resp=client.post('/api/print', headers={'X-Upload-Filename':'Long Document.pdf',
        'X-Print-Copies':'3', 'X-Print-Media':'letter', 'X-Print-Sides':'two-sided-long-edge'},content=pdf.getvalue())
    assert resp.status_code==200,resp.text
    assert resp.json()['job_id']=='SamsungAirPrint-105'
    assert len(PdfReader(BytesIO(sent[0][0])).pages)==26
    assert all(sent[0][2][k] == v for k,v in {'copies':3, 'media':'letter', 'sides':'two-sided-long-edge'}.items())
    # Signed-in files can exceed the anonymous upload cap.
    assert len(pdf.getvalue())>500
    invalid=client.post('/api/print',headers={'X-Upload-Filename':'x.pdf', 'X-Print-Copies':'999'}, content=pdf.getvalue())
    assert invalid.status_code==400


def test_guest_restrictions_remain_enforced_even_when_signed_in_preview(client, monkeypatch):
    from io import BytesIO
    from pypdf import PdfWriter
    client.post('/api/admin/guest',json={'enabled':True})
    guest=TestClient(appmod.app)
    token=guest.get('/api/guest/status').json()['csrf']
    writer=PdfWriter();writer.add_blank_page(width=612,height=792)
    data=BytesIO();writer.write(data)
    headers={'X-Upload-Filename':'test.pdf','X-Guest-CSRF':token}
    assert guest.post('/api/guest/print',headers={**headers,'X-Print-Copies':'2'},content=data.getvalue()).status_code==400
    # Guest print settings may choose paper/duplex; only copy/file/page quotas differ.
    monkeypatch.setattr(appmod,'print_with_cups',lambda *a,**k: 'request id is HomePrinter-1')
    assert guest.post('/api/guest/print',headers={**headers,'X-Print-Media':'letter'},content=data.getvalue()).status_code==200
    monkeypatch.setattr(appmod, 'GUEST_MAX_BYTES', 100)
    assert guest.post('/api/guest/print',headers=headers,content=data.getvalue()).status_code==413


def test_print_status_shows_wrong_queue_names(client, monkeypatch):
    def fake_lpstat(cmd, **kwargs):
        assert cmd[:3]==['lpstat','-h',appmod.CUPS_SERVER]
        if cmd[3:] == ['-p',appmod.CUPS_QUEUE]:
            return subprocess.CompletedProcess(cmd,1,'','Unknown destination')
        if cmd[3:] == ['-a',appmod.CUPS_QUEUE]:
            return subprocess.CompletedProcess(cmd,1,'','Unknown destination')
        return subprocess.CompletedProcess(cmd,0,'printer HomePrinter is idle. enabled since now\n','')
    monkeypatch.setattr(appmod.subprocess, 'run', fake_lpstat)
    from backend import devices
    monkeypatch.setattr(devices,'list_cups_queues',lambda server:[
        {'queue':'HomePrinter','ready':True,'status':'idle'}])
    r=client.get('/api/print/status')
    assert r.status_code==200
    assert r.json()['ready'] is False
    assert any('HomePrinter' in x for x in r.json()['available_queues'])


def test_print_with_cups_uses_configured_queue_and_rejects_failed_submission(monkeypatch,tmp_path):
    from backend import devices
    monkeypatch.setattr(appmod,'CUPS_QUEUE','SamsungAirPrint')
    monkeypatch.setattr(devices,'list_cups_queues', lambda server:[{'queue':'SamsungAirPrint','ready':True,'status':'idle'}])
    path=tmp_path/'document.pdf';path.write_bytes(b'%PDF- sample')
    commands=[]
    def accepted(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd,0,'request id is HomePrinter-44 (1 file(s))\n','')
    monkeypatch.setattr(appmod.subprocess, 'run', accepted)
    result=appmod.print_with_cups(path,'A safe title',copies=2,media='a4',sides='two-sided-long-edge',queue='SamsungAirPrint')
    assert 'HomePrinter-44' in result
    # Printer-specific size aliases may require an lpoptions query first.
    cmd=next(item for item in commands if item[0]=='lp')
    assert cmd[:4]==['lp','-h',appmod.CUPS_SERVER,'-d']
    assert cmd[4]==appmod.CUPS_QUEUE
    assert cmd[cmd.index('-n')+1]=='2'
    assert 'media=A4' in cmd and 'sides=two-sided-long-edge' in cmd
    def refused(cmd,**kwargs):
        return subprocess.CompletedProcess(cmd,1,'','Unable to print: printer not shared')
    monkeypatch.setattr(appmod.subprocess, 'run', refused)
    with pytest.raises(appmod.HTTPException) as exc:
        appmod.print_with_cups(path,queue='SamsungAirPrint')
    assert exc.value.status_code==502
    assert 'printer not shared' in exc.value.detail


def test_print_saved_folio_document_is_per_user_and_real_pdf(client,monkeypatch):
    from io import BytesIO
    from pypdf import PdfReader
    import sys, types
    jid=create(client);scan_and_wait(client,jid, count=2)
    try:
        import img2pdf
    except ImportError:
        from pypdf import PdfWriter
        def fake_convert(images):
            pdf=PdfWriter()
            for _ in images:pdf.add_blank_page(width=600,height=800)
            buf=BytesIO();pdf.write(buf);return buf.getvalue()
        monkeypatch.setitem(sys.modules,'img2pdf',types.SimpleNamespace(convert=fake_convert))
    receipts=[]
    def mock_cups(path,title,**kwargs):
        receipts.append((path.read_bytes(),title,kwargs))
        return 'request id is SamsungAirPrint-7'
    monkeypatch.setattr(appmod,'print_with_cups',mock_cups)
    r=client.post(f'/api/jobs/{jid}/print',headers={'X-Print-Copies':'2'})
    assert r.status_code==200,r.text
    assert r.json()['job_id']=='SamsungAirPrint-7'
    assert len(PdfReader(BytesIO(receipts[0][0])).pages)==2
    assert receipts[0][2]['copies']==2
    client.post('/api/admin/users',json={
        'username':'outside','password':'outside-account-password','role':'member'})
    member=TestClient(appmod.app)
    session=member.post('/api/auth/login',json={'username':'outside','password':'outside-account-password'}).json()
    member.headers['X-CSRF-Token']=session['csrf']
    assert member.post(f'/api/jobs/{jid}/print').status_code==404
    monkeypatch.setattr(appmod.subprocess, 'run', lambda cmd,**kwargs: subprocess.CompletedProcess(cmd,0,'printer SamsungAirPrint is idle and accepting requests since now',''))
    assert member.get('/api/print/status').status_code==200


def test_crop_remains_clipped_after_straightening_and_right_angle_rotation(client):
    """A fine rotation must never expose pixels excluded by an earlier crop."""
    from PIL import ImageChops, ImageDraw
    from backend import features
    from backend.processing import transform_page

    def shows_discarded_red(image):  # Pillow only, so this also runs on the Lite edition
        red, green, _ = image.convert('RGB').split()
        both = ImageChops.multiply(red.point(lambda v: 255 if v > 180 else 0),
                                   green.point(lambda v: 255 if v < 80 else 0))
        return both.getbbox() is not None
    jid = create(client)
    job = scan_and_wait(client, jid, count=1)
    page = job['pages'][0]
    path = appmod.JOBS_DIR / jid / 'pages' / page['file']
    original = Image.new('RGB', (600, 800), (210, 14, 28))  # red discarded region
    d = ImageDraw.Draw(original)
    d.rectangle((105, 155, 500, 655), fill=(22, 118, 76))  # green desired content
    original.save(path)
    url=f'/api/jobs/{jid}/pages/{page["id"]}'
    response=client.patch(url,json={'crop':[.2,.25,.6,.5]})
    assert response.status_code==200
    cropped=transform_page(original,response.json()['pages'][0]).convert('RGB')
    # Crop excludes all red; it must stay excluded at every deskew angle.
    assert not shows_discarded_red(cropped)
    for angle in (-8, 3, 11.75):
        rotated=client.patch(url,json={'deskew':angle})
        assert rotated.status_code==200
        image=transform_page(original,rotated.json()['pages'][0]).convert('RGB')
        assert not shows_discarded_red(image), f'Restored discarded content at {angle}°'
    # Auto-straighten should inspect the cropped content, not the discarded region.
    suggestion=client.post(url+'/auto-straighten')
    assert suggestion.status_code==(200 if features.available('auto_straighten') else 409)
    # Rotating 90 degrees should preserve the selected pixels and reproject crop.
    page_before=client.get(f'/api/jobs/{jid}').json()['pages'][0]
    rotated=client.patch(url,json={'rotation':90})
    assert rotated.status_code==200
    page_after=rotated.json()['pages'][0]
    x,y,w,h=page_before['crop']
    assert page_after['crop']==pytest.approx([1-y-h,x,h,w],abs=1e-5)
    rotated_img=transform_page(original,page_after).convert('RGB')
    assert not shows_discarded_red(rotated_img)


def test_crop_base_preview_is_unskewed_and_uncropped(client):
    """The crop editor and the crop operation must share one coordinate space."""
    from io import BytesIO
    jid=create(client)
    page=scan_and_wait(client,jid,count=1)['pages'][0]
    url=f'/api/jobs/{jid}/pages/{page["id"]}'
    client.patch(url,json={'deskew':7.5,'crop':[.1,.15,.7,.6]})
    whole=Image.open(BytesIO(client.get(url+'/image?uncropped=true&large=true').content))
    assert whole.size==(120,160), 'Crop base must show original orientation without the deskew expansion'
    cropped=Image.open(BytesIO(client.get(url+'/image').content))
    assert cropped.width<whole.width and cropped.height<whole.height
