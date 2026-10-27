"""v0.20: email through SMTP: invitations, "Forgot your password?", and
notifications. A fake mail server receives the real messages."""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import app as web, backup, devices, mail, notify, security, tasks  # noqa: E402
from fake_cups import FakeCups, self_signed_certificate  # noqa: E402
from fake_smtp import FakeSmtp  # noqa: E402

PASSWORD = 'email-admin-long-password'
PUBLIC = 'https://folio.example.com'


def login(username, password):
    client = TestClient(web.app)
    signed = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert signed.status_code == 200, signed.text
    client.headers['x-csrf-token'] = signed.json()['csrf']
    return client


def link_token(text: str) -> str:
    found = re.search(re.escape(PUBLIC) + r'/#password=([\w-]+)', text)
    assert found, text
    return found.group(1)


@pytest.fixture
def smtp():
    server = FakeSmtp(username='folio@example.com', password='smtp-app-password')
    yield server
    server.close()


@pytest.fixture
def folio(tmp_path, monkeypatch, smtp):
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'JOBS_DIR', tmp_path / 'jobs')
    monkeypatch.setattr(web, 'DEVICE', '')
    monkeypatch.setattr(web, 'CUPS_SERVERS', ['cups.test:631'])
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD', PASSWORD)
    monkeypatch.delenv('FOLIO_PUBLIC_URL', raising=False)
    monkeypatch.setattr(devices, 'listed_sane_devices', lambda: [])
    monkeypatch.setattr(devices, 'list_cups_queues', lambda server: [{'queue': 'Office', 'ready': True, 'status': 'idle'}])
    monkeypatch.setattr(devices, 'cups_capabilities', lambda *a: {
        'known': True, 'media': [], 'duplex': True, 'color': False, 'color_modes': ['auto', 'gray']})
    monkeypatch.setattr(web, 'print_with_cups', lambda path, title, **c: 'request id is Office-1 (1 file(s))')
    monkeypatch.setattr(mail, '_last', {'at': None, 'ok': None, 'error': None})
    security._reset_requests.clear()
    security._login_attempts.clear()
    with TestClient(web.app):
        admin = login('admin', PASSWORD)
        yield admin, smtp
        mail.wait_until_sent()
        tasks.runner.start(tmp_path / 'tasks')


def set_up_mail(admin, smtp, **changes):
    body = {'public_url': PUBLIC, 'host': '127.0.0.1', 'port': smtp.port, 'security': 'none',
            'username': 'folio@example.com', 'password': 'smtp-app-password', 'sender': 'folio@example.com', **changes}
    saved = admin.put('/api/admin/mail', json=body)
    assert saved.status_code == 200, saved.text
    return saved.json()


def delivered(smtp, count, timeout=10):
    assert mail.wait_until_sent(timeout)
    assert len(smtp.messages) == count, [m['message']['Subject'] for m in smtp.messages]
    return smtp.messages


# --- Settings and the test email -------------------------------------------------------

def test_mail_settings_never_show_the_password(folio):
    admin, smtp = folio
    assert admin.get('/api/admin/mail').json()['configured'] is False
    settings = set_up_mail(admin, smtp)
    assert settings['configured'] and settings['has_password'] and 'password' not in settings
    assert 'smtp-app-password' not in admin.get('/api/admin/mail').text
    # Saving without a password keeps the stored one.
    set_up_mail(admin, smtp, password='')
    assert mail.settings(web.DATA_DIR)['password'] == 'smtp-app-password'
    for change, problem in (({'sender': ''}, 'address emails come from'), ({'sender': 'not an address'}, 'not an email'),
                            ({'public_url': 'folio.example.com'}, 'Folio’s address'), ({'host': 'smtp example'}, 'server')):
        refused = admin.put('/api/admin/mail', json={'host': '127.0.0.1', 'sender': 'folio@example.com', **change})
        assert refused.status_code == 400 and problem in refused.json()['detail'], refused.text
    assert admin.delete('/api/admin/mail').json()['configured'] is False


def test_a_test_email_arrives_and_a_wrong_password_is_explained(folio):
    admin, smtp = folio
    set_up_mail(admin, smtp)
    sent = admin.post('/api/admin/mail/test', json={'to': 'me@example.com'})
    assert sent.status_code == 200, sent.text
    message = delivered(smtp, 1)[0]
    assert message['to'] == ['me@example.com'] and message['message']['From'] == 'Folio <folio@example.com>'
    assert message['message']['Subject'] == 'Folio test email' and 'Email works' in message['message'].get_content()
    set_up_mail(admin, smtp, password='the-wrong-one')
    refused = admin.post('/api/admin/mail/test', json={'to': 'me@example.com'})
    assert refused.status_code == 502 and 'did not accept the username or password' in refused.json()['detail']
    checks = {c['id']: c for c in admin.get('/api/admin/diagnostics').json()['sections']['system']}
    assert checks['email']['status'] == 'warn' and 'The last email failed' in checks['email']['detail']
    smtp.refuse.add('nobody@example.com')
    set_up_mail(admin, smtp)
    refused = admin.post('/api/admin/mail/test', json={'to': 'nobody@example.com'})
    assert refused.status_code == 502 and 'refused the address nobody@example.com' in refused.json()['detail']
    assert admin.post('/api/admin/mail/test', json={}).json()['detail'] == 'Enter an address to send the test to'


@pytest.mark.parametrize('mode', ['starttls', 'ssl'])
def test_encrypted_mail_servers(folio, tmp_path_factory, mode):
    admin, _ = folio
    certificate = self_signed_certificate(tmp_path_factory.mktemp('tls'), 'smtp')
    if not certificate:
        pytest.skip('openssl is not available')
    server = FakeSmtp(username='folio@example.com', password='smtp-app-password', mode=mode, certificate=certificate)
    try:
        # A self-signed certificate is refused unless checking is switched off.
        set_up_mail(admin, server, security=mode)
        refused = admin.post('/api/admin/mail/test', json={'to': 'me@example.com'})
        assert refused.status_code == 502 and 'certificate could not be verified' in refused.json()['detail']
        set_up_mail(admin, server, security=mode, verify_tls=False)
        assert admin.post('/api/admin/mail/test', json={'to': 'me@example.com'}).status_code == 200
        assert len(server.messages) == 1
    finally:
        server.close()


# --- Invitations -------------------------------------------------------------------------

def test_an_invitation_by_email_lets_someone_choose_their_password(folio):
    admin, smtp = folio
    set_up_mail(admin, smtp)
    admin.put('/api/auth/account', json={'email': 'admin@example.com'})
    invited = admin.post('/api/admin/invitations', json={'username': 'alice', 'email': 'alice@example.com',
                                                         'role': 'member'}, headers={'host': 'evil.example'})
    assert invited.status_code == 201, invited.text
    result = invited.json()
    assert result['emailed'] and result['email'] == 'alice@example.com' and result['link'].startswith(PUBLIC + '/#password=')
    message = delivered(smtp, 1)[0]
    assert message['to'] == ['alice@example.com'] and message['message']['Subject'] == 'You are invited to Folio'
    body = message['message'].get_content()
    token = link_token(body)  # The link uses Folio's address, never the request's Host header
    assert 'evil.example' not in body and 'Your username is alice' in body and 'admin has made you an account' in body
    alice_row = next(u for u in result['users'] if u['username'] == 'alice')
    assert alice_row['pending'] and alice_row['invite_expires'] > time.time() + 47 * 3600
    # No password works until she chooses one.
    guest = TestClient(web.app)
    assert guest.post('/api/auth/login', json={'username': 'alice', 'password': 'x' * 12}).status_code == 401
    assert guest.post('/api/auth/link', json={'token': token}).json() == {'username': 'alice', 'purpose': 'invite'}
    assert guest.post('/api/auth/link/password', json={'token': token, 'password': 'short'}).status_code == 422
    chosen = guest.post('/api/auth/link/password', json={'token': token, 'password': 'alice-chose-this-one'})
    assert chosen.status_code == 200 and chosen.json()['username'] == 'alice'
    assert guest.get('/api/auth/status').json()['authenticated']  # Signed in straight away
    # The link works once; her password works from now on.
    assert guest.post('/api/auth/link', json={'token': token}).status_code == 404
    assert guest.post('/api/auth/link/password', json={'token': token, 'password': 'another-password-1'}).status_code == 404
    login('alice', 'alice-chose-this-one')
    joined = delivered(smtp, 2)[1]
    assert joined['to'] == ['admin@example.com'] and joined['message']['Subject'] == 'alice has joined Folio'
    assert not next(u for u in admin.get('/api/admin/users').json() if u['username'] == 'alice')['pending']


def test_an_invitation_without_email_is_a_link_to_pass_on(folio):
    admin, smtp = folio
    result = admin.post('/api/admin/invitations', json={'username': 'kiosk', 'role': 'printer'}).json()
    assert not result['emailed'] and result['link'] is None and result['token']  # The browser adds its own address
    assert TestClient(web.app).post('/api/auth/link', json={'token': result['token']}).json()['purpose'] == 'invite'
    # A new link replaces the old one.
    again = admin.post('/api/admin/users/kiosk/link', json={'send': False}).json()
    assert again['purpose'] == 'invite' and again['token'] != result['token']
    assert TestClient(web.app).post('/api/auth/link', json={'token': result['token']}).status_code == 404
    # Email set up, but no address for Folio: the email would have no link, so it isn't sent.
    set_up_mail(admin, smtp, public_url='')
    admin.patch('/api/admin/users/kiosk', json={'email': 'kiosk@example.com'})
    sent = admin.post('/api/admin/users/kiosk/link', json={'send': True}).json()
    assert not sent['emailed'] and 'Folio’s address isn’t set' in sent['email_error']
    assert smtp.messages == []
    assert admin.post('/api/admin/invitations', json={'username': 'kiosk2', 'email': 'KIOSK@example.com'}).json()[
        'detail'] == 'Another account already uses this email address'
    assert admin.post('/api/admin/invitations', json={'username': 'x', 'email': 'a@b.co'}).status_code == 422


# --- Forgot your password? -----------------------------------------------------------------

def test_forgot_password_emails_a_link_and_reveals_nothing(folio):
    admin, smtp = folio
    guest = TestClient(web.app)
    assert guest.get('/api/auth/status').json()['password_reset'] is False
    assert guest.post('/api/auth/forgot', json={'login': 'admin'}).status_code == 409
    set_up_mail(admin, smtp)
    admin.post('/api/admin/users', json={'username': 'bob', 'password': 'bob-old-long-password', 'role': 'member',
                                         'email': 'Bob@Example.com'})
    assert guest.get('/api/auth/status').json()['password_reset'] is True  # On by default
    unknown = guest.post('/api/auth/forgot', json={'login': 'nobody'})
    known = guest.post('/api/auth/forgot', json={'login': 'bob@example.com'})  # By address, in any case
    assert unknown.status_code == known.status_code == 200 and unknown.json() == known.json()
    message = delivered(smtp, 1)[0]
    assert message['to'] == ['Bob@Example.com'] and message['message']['Subject'] == 'Choose a new Folio password'
    token = link_token(message['message'].get_content())
    bob = login('bob', 'bob-old-long-password')
    assert guest.post('/api/auth/link', json={'token': token}).json() == {'username': 'bob', 'purpose': 'reset'}
    assert guest.post('/api/auth/link/password', json={'token': token, 'password': 'bob-new-long-password'}).status_code == 200
    assert bob.get('/api/auth/status').json()['authenticated'] is False  # Signed out everywhere else
    login('bob', 'bob-new-long-password')
    notice = delivered(smtp, 2)[1]
    assert notice['message']['Subject'] == 'Your Folio password was changed' and 'through a link' in notice['message'].get_content()


def test_administrators_can_reset_by_email_too_and_it_can_be_switched_off(folio):
    admin, smtp = folio
    set_up_mail(admin, smtp)
    admin.put('/api/auth/account', json={'email': 'admin@example.com'})
    guest = TestClient(web.app)
    guest.post('/api/auth/forgot', json={'login': 'admin'})
    assert delivered(smtp, 1)[0]['to'] == ['admin@example.com']
    set_up_mail(admin, smtp, password_reset=False)
    assert guest.get('/api/auth/status').json()['password_reset'] is False
    assert guest.post('/api/auth/forgot', json={'login': 'admin'}).status_code == 409


def test_reset_links_expire_are_rate_limited_and_are_stored_only_as_hashes(folio, monkeypatch):
    admin, smtp = folio
    set_up_mail(admin, smtp)
    admin.put('/api/auth/account', json={'email': 'admin@example.com'})
    guest = TestClient(web.app)
    assert all(guest.post('/api/auth/forgot', json={'login': 'admin'}).status_code == 200 for _ in range(3))
    assert guest.post('/api/auth/forgot', json={'login': 'admin'}).status_code == 429
    token = link_token(delivered(smtp, 3)[2]['message'].get_content())
    database = (web.DATA_DIR / 'access.sqlite3').read_bytes()
    assert token.encode() not in database
    later = time.time() + 61 * 60
    monkeypatch.setattr(security.time, 'time', lambda: later)
    assert guest.post('/api/auth/link', json={'token': token}).status_code == 404  # One hour only


def test_backups_leave_out_unused_links(folio, tmp_path):
    admin, _ = folio
    admin.post('/api/admin/invitations', json={'username': 'carol'})
    copy = tmp_path / 'copy.sqlite3'
    backup._sqlite_copy(web.DATA_DIR / 'access.sqlite3', copy, drop_sessions=True)
    with sqlite3.connect(copy) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'password_links' not in tables and 'users' in tables


# --- Your account -------------------------------------------------------------------------------

def test_everyone_manages_their_own_address_and_password(folio):
    admin, smtp = folio
    set_up_mail(admin, smtp)
    admin.post('/api/admin/users', json={'username': 'dana', 'password': 'dana-long-password', 'role': 'printer'})
    dana = login('dana', 'dana-long-password')
    assert dana.get('/api/auth/account').json() == {'username': 'dana', 'role': 'printer', 'email': None,
                                                    'email_ready': True}
    assert dana.put('/api/auth/account', json={'email': 'not-an-address'}).status_code == 400
    assert dana.put('/api/auth/account', json={'email': 'dana@example.com'}).json()['email'] == 'dana@example.com'
    assert dana.post('/api/auth/password', json={'current_password': 'dana-long-password',
                                                 'new_password': 'dana-newer-password'}).status_code == 200
    assert delivered(smtp, 1)[0]['message']['Subject'] == 'Your Folio password was changed'
    admin.patch('/api/admin/users/dana', json={'password': 'set-by-the-admin'})
    assert 'by an administrator' in delivered(smtp, 2)[1]['message'].get_content()
    admin.patch('/api/admin/users/dana', json={'email': ''})
    assert security.account(web.DATA_DIR, 'dana')['email'] is None


# --- Notifications -------------------------------------------------------------------------------

def printer(**changes):
    return {'id': 'cups.test:631|Office', 'label': 'Office', 'supplies': [], 'reasons': [], **changes}


TONER_LOW = [{'name': 'Black Toner', 'level': 6, 'low': True}]


def test_printer_problems_are_emailed_once_until_they_clear(folio):
    admin, smtp = folio
    data = web.DATA_DIR
    assert notify.check_printers(data, [printer(supplies=TONER_LOW)]) == 0  # Email not set up: nothing kept either
    set_up_mail(admin, smtp)
    assert notify.check_printers(data, [printer(supplies=TONER_LOW)]) == 0  # No administrator address yet
    admin.put('/api/auth/account', json={'email': 'admin@example.com'})
    assert notify.check_printers(data, [printer(supplies=TONER_LOW)]) == 1
    low = delivered(smtp, 1)[0]
    assert low['message']['Subject'] == 'Toner or ink is running low on Office'
    assert 'Black Toner: 6 % left' in low['message'].get_content()
    assert notify.check_printers(data, [printer(supplies=TONER_LOW)]) == 0  # Already reported
    assert notify.check_printers(data, []) == 0  # Printer didn't answer: nothing re-armed
    assert notify.check_printers(data, [printer(supplies=TONER_LOW)]) == 0
    assert notify.check_printers(data, [printer()]) == 0  # New toner…
    assert notify.check_printers(data, [printer(supplies=TONER_LOW)]) == 1  # …running low again later
    reasons = ['media-jam-error', 'offline-report', 'media-empty-warning']
    assert notify.check_printers(data, [printer(reasons=reasons, supplies=TONER_LOW)]) == 1
    jam = delivered(smtp, 3)[2]
    assert jam['message']['Subject'] == 'Office needs attention'
    assert 'Paper jam' in jam['message'].get_content() and 'Out of paper' in jam['message'].get_content()
    assert 'ffline' not in jam['message'].get_content()  # Printers asleep at night aren't news
    set_up_mail(admin, smtp, notify={'supplies': False, 'attention': True, 'quota': True, 'joined': True})
    assert notify.check_printers(data, [printer(supplies=[{'name': 'Cyan', 'level': 2, 'low': True}])]) == 0


def test_the_background_check_reads_real_printer_state(folio, monkeypatch):
    admin, smtp = folio
    fake = FakeCups()
    try:
        fake.printers['Hall'] = {'state': 3, 'reasons': ['media-empty-error'],
                                 'markers': [('Black Toner S/N:CRUM-1', 'toner', 4, '#000000', 10)]}
        monkeypatch.setattr(web, 'CUPS_SERVERS', [fake.server])
        monkeypatch.setattr(devices, 'list_cups_queues', lambda server: [{'queue': 'Hall', 'ready': True, 'status': 'idle'}])
        set_up_mail(admin, smtp)
        admin.put('/api/auth/account', json={'email': 'admin@example.com'})
        assert web.watch_printers() == 2
        subjects = sorted(m['message']['Subject'] for m in delivered(smtp, 2))
        label = f'Hall ({fake.server})'  # Printers on other print servers say where they are
        assert subjects == [f'{label} needs attention', f'Toner or ink is running low on {label}']
        assert web.watch_printers() == 0
    finally:
        fake.close()


def pdf(pages: int) -> bytes:
    document = fitz.open()
    for _ in range(pages):
        document.new_page()
    return document.tobytes()


def test_people_hear_once_when_their_pages_are_used_up(folio):
    admin, smtp = folio
    set_up_mail(admin, smtp)
    admin.patch('/api/admin/printers/' + quote('cups.test:631|Office', safe=''), json={'make_default': True})
    admin.post('/api/admin/users', json={'username': 'erin', 'password': 'erin-long-password', 'role': 'member',
                                         'email': 'erin@example.com'})
    admin.put('/api/admin/quota-default', json={'monthly_pages': 3})
    erin = login('erin', 'erin-long-password')

    def print_pages(pages):
        token = erin.post('/api/print/inspect', headers={'X-Upload-Filename': 'a.pdf'}, content=pdf(pages)).json()['token']
        return erin.post('/api/print/submit-preview', json={'token': token})
    assert print_pages(2).status_code == 200
    assert mail.wait_until_sent() and smtp.messages == []
    assert print_pages(1).status_code == 200
    message = delivered(smtp, 1)[0]
    assert message['to'] == ['erin@example.com'] and 'all 3 of your pages' in message['message'].get_content()
    admin.put('/api/admin/quota-default', json={'monthly_pages': 4})
    assert print_pages(1).status_code == 200
    delivered(smtp, 1)  # Once a month


def test_the_checklist_and_system_check_mention_email(folio):
    admin, smtp = folio
    steps = {s['id']: s for s in admin.get('/api/admin/checklist').json()['steps']}
    assert steps['email']['optional'] and not steps['email']['done']
    checks = {c['id']: c for c in admin.get('/api/admin/diagnostics').json()['sections']['system']}
    assert checks['email']['status'] == 'info'
    set_up_mail(admin, smtp, public_url='')
    checks = {c['id']: c for c in admin.get('/api/admin/diagnostics').json()['sections']['system']}
    assert checks['email']['status'] == 'ok' and checks['public-url']['status'] == 'warn'
    assert checks['admin-email']['status'] == 'warn'
    assert {s['id']: s for s in admin.get('/api/admin/checklist').json()['steps']}['email']['done']


def test_only_administrators_see_mail_settings_and_strangers_cannot_post_across_sites(folio):
    admin, smtp = folio
    admin.post('/api/admin/users', json={'username': 'fred', 'password': 'fred-long-password', 'role': 'member'})
    fred = login('fred', 'fred-long-password')
    assert fred.get('/api/admin/mail').status_code == 403
    assert fred.post('/api/admin/invitations', json={'username': 'mallory'}).status_code == 403
    set_up_mail(admin, smtp)
    assert TestClient(web.app).post('/api/auth/forgot', json={'login': 'admin'},
                                    headers={'sec-fetch-site': 'cross-site'}).status_code == 403
