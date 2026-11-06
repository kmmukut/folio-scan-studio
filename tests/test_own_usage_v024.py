"""v0.24: members and print-only accounts see their own printing, and the
next person signing in on a page never sees what the previous one saw."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import app as web, stats  # noqa: E402
import test_print_stats_v013 as v013  # noqa: E402

log, api, at, add, login, upload_and_print = v013.log, v013.api, v013.at, v013.add, v013.login, v013.upload_and_print
FRONTEND = ROOT / 'frontend'
AUTH, CORE, STATS = ((FRONTEND / 'js' / f'{name}.js').read_text() for name in ('auth', 'core', 'stats'))
PAGE, STYLE = (FRONTEND / 'index.html').read_text(), (FRONTEND / 'style.css').read_text()


def test_a_persons_own_numbers_leave_out_everyone_else(log):
    now = at(2026, 10, 20, 12)
    add(log, at(2026, 3, 2, 9), user='alice', pages=4)
    add(log, at(2026, 10, 3, 9), user='alice', pages=2, copies=3)
    add(log, at(2026, 1, 5, 9), user='bob', pages=50)
    # A job sent straight to CUPS under the same name isn't known to be hers
    stats.record(log, user='alice', server='cups.lan:631', queue='Office', origin='cups', pages=9,
                 when=at(2026, 10, 4, 9))
    mine = stats.summary(log, 'all', now, user='alice')
    assert (mine['totals']['jobs'], mine['totals']['pages']) == (2, 10)
    assert [p['user'] for p in mine['people']] == ['alice']
    assert mine['series'][0]['key'] == '2026-03'  # "All time" starts at her first job, not bob's
    assert stats.summary(log, 'all', now)['totals']['pages'] == 69  # Everyone's is unchanged
    assert stats.summary(log, 'all', now, user='carol')['totals']['jobs'] == 0


def test_members_and_print_only_accounts_see_only_their_own_printing(api):
    _, admin = api
    for name, role in (('alice', 'member'), ('printer1', 'printer')):
        assert admin.post('/api/admin/users', json={'username': name, 'password': f'{name}-long-password',
                                                    'role': role}).status_code == 201
    alice, kiosk = login('alice', 'alice-long-password'), login('printer1', 'printer1-long-password')
    upload_and_print(admin, pages=7)
    upload_and_print(alice, pages=3, **{'X-Print-Copies': '2'})
    upload_and_print(kiosk, pages=1)
    admin.put('/api/admin/users/alice/quota', json={'monthly_pages': 10})

    report = alice.get('/api/print/usage?period=month').json()
    assert (report['totals']['jobs'], report['totals']['pages']) == (1, 6)
    assert [p['queue'] for p in report['printers']] == ['Office']
    assert report['quota'] == {'limit': 10, 'used': 6, 'left': 4}
    assert report['enabled'] is True and report['retention_days'] == 365
    # Nobody else's numbers or names, and not the record-keeping settings
    assert not {'people', 'settings', 'guest_user'} & report.keys()
    assert not any(key.startswith('direct_') for key in report['totals'])
    assert 'admin' not in str(report) and 'printer1' not in str(report)

    printed = kiosk.get('/api/print/usage').json()  # Print-only accounts may ask too
    assert printed['totals']['pages'] == 1 and printed['quota']['limit'] is None
    assert admin.get('/api/print/usage').json()['totals']['pages'] == 7
    assert admin.get('/api/admin/stats').json()['totals']['pages'] == 14

    for person in (alice, kiosk):
        assert person.get('/api/admin/stats').status_code == 403
        assert person.get('/api/print/usage?period=forever').status_code == 400
    assert TestClient(web.app).get('/api/print/usage').status_code == 401
    admin.put('/api/admin/stats/settings', json={'enabled': False, 'retention_days': 0})
    report = alice.get('/api/print/usage').json()
    assert report['enabled'] is False and report['retention_days'] == 0


def test_everyone_signed_in_has_a_statistics_page():
    assert AUTH.count("$('#stats-tab').classList.remove('hidden')") == 2  # Members and print-only accounts
    assert "#stats-tab" not in re.search(r'\.printer-only \.rail [^{]*\{display:none', STYLE).group(0)
    assert "if(state.role==='printer'){showPrint=!showStats;" in CORE
    assert "async function openStats(){workspace(false,false,false,true);" in STATS
    assert "own?report.enabled:report.settings.enabled" in STATS
    # What only administrators see is marked, and hidden on someone's own page
    for marked in ('id="stats-export"', '<span class="overline">BY PERSON</span>', 'stats-settings'):
        tag = PAGE[PAGE.rindex('<', 0, PAGE.index(marked)):PAGE.index('>', PAGE.index(marked))]
        assert 'stats-admin-only' in tag, marked
    assert '.stats-own .stats-admin-only,.stats-desk:not(.stats-own) .stats-own-only{display:none!important}' in STYLE
    assert 'id="stats-quota"' in PAGE and 'id="stats-own-kept"' in PAGE


def test_the_next_person_never_sees_the_previous_ones_page():
    sign_out = AUTH[AUTH.index('async function signOut(){'):]
    assert 'location.reload()' in sign_out[:sign_out.index('\n}')]
    show_login = AUTH[AUTH.index('function showLogin(){'):]
    show_login = show_login[:show_login.index('\n}')]
    assert "$('#setup-checklist').classList.add('hidden')" in show_login
    assert show_login.index('state.lastUser=state.user') < show_login.index('state.user=null')
    # After a session runs out, someone else signing in on that page gets a fresh one
    assert "if(state.lastUser&&state.lastUser!==state.user){location.reload();return;}" in AUTH
    # Changing the password signs out through showLogin, so the name is still known there
    assert "$('#account-dialog').close();showLogin();" in AUTH
