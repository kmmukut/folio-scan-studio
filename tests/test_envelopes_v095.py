"""Regression tests for envelope size aliases, dimensions, print submission."""
import subprocess
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import devices, printing, app as web, security
from fastapi.testclient import TestClient
import pymupdf as fitz


def legacy_samsung_options(args, timeout=8):
    if args[0]=='lpoptions':
        return subprocess.CompletedProcess(args,0,
            'PageSize/Page Size: *Letter/US Letter A4/A4 Env10/No. 10 Envelope '
            'EnvDL/DL Envelope EnvC5/C5 Envelope EnvC6/C6 Envelope '
            'EnvMonarch/Monarch Envelope\n'
            'InputSlot/Paper Source: *Tray1/Tray 1 MultiTray/Multi Tray\n'
            'MediaType/Paper Type: *Plain/Plain Envelope/Envelope Thick/Thick\n', '')
    return subprocess.CompletedProcess(args,1,'','Bad query')


def test_envelope_aliases_and_dimensions(monkeypatch):
    monkeypatch.setattr(devices,'cups_run',legacy_samsung_options)
    caps=devices.cups_capabilities('cups-a:631','Samsung')
    assert caps['media_map']['com10']=='Env10'
    assert caps['media_map']['dl']=='EnvDL'
    assert caps['media_map']['c5']=='EnvC5'
    assert caps['media_map']['c6']=='EnvC6'
    assert caps['media_map']['monarch']=='EnvMonarch'
    assert caps['media_option']=='PageSize'
    assert caps['envelope_type_value']=='Envelope'
    assert '105 × 241 mm' in printing.MEDIA['com10'][1]
    assert '4⅛ × 9½ in' in printing.MEDIA['com10'][1]
    assert printing.match_media_choices(['na_number-10_4.125x9.5in']) == {'com10':'na_number-10_4.125x9.5in'}
    assert devices.paper_source_label('MultiTray') == 'Multipurpose / manual feed'


def test_unknown_envelope_remains_unavailable(monkeypatch):
    def only_plain(args, timeout=8):
        return subprocess.CompletedProcess(args,0,'PageSize/Page Size: *Letter A4\nInputSlot/Paper Source: *Tray1 MultiTray\n','')
    monkeypatch.setattr(devices,'cups_run',only_plain)
    caps=devices.cups_capabilities('cups-a:631','Samsung')
    assert 'com10' not in caps['media_map']
    assert caps['paper_sources'][1]['value']=='MultiTray'


def test_legacy_envelope_submits_exact_ppd_size_and_manual_tray(monkeypatch,tmp_path):
    monkeypatch.setattr(web,'CUPS_SERVERS',['cups-a:631'])
    monkeypatch.setattr(web,'CUPS_SERVER','cups-a:631')
    monkeypatch.setattr(web,'CUPS_QUEUE','')
    monkeypatch.setattr(web,'DATA_DIR',tmp_path)
    monkeypatch.setattr(web,'JOBS_DIR',tmp_path/'jobs')
    monkeypatch.setattr(web,'PREVIEW_ROOT',tmp_path/'print-previews')
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD','password-of-16-characters')
    monkeypatch.setattr(devices,'list_cups_queues',lambda server:[{'queue':'Samsung','ready':True,'status':'idle'}])
    monkeypatch.setattr(devices,'cups_run',legacy_samsung_options)
    sent=[]
    def lp_call(args,**kwargs):
        sent.append(args)
        return subprocess.CompletedProcess(args,0,'request id is Samsung-1 (1 file(s))','')
    monkeypatch.setattr(web.subprocess,'run',lp_call)
    security._guest_submissions.clear();security._global_submissions.clear()
    with TestClient(web.app) as client:
        auth=client.post('/api/auth/login',json={'username':'admin','password':'password-of-16-characters'})
        assert auth.status_code==200, auth.text
        client.headers['x-csrf-token']=auth.json()['csrf']
        printers=client.get('/api/printers').json()['printers']
        assert printers[0]['capabilities']['media_map']['com10']=='Env10'
        with fitz.open() as pdf:
            pdf.new_page(); raw=pdf.tobytes()
        inspection=client.post('/api/print/inspect',headers={'x-upload-filename':'envelopes.pdf'},content=raw)
        assert inspection.status_code==200,inspection.text
        payload={'token':inspection.json()['token']}
        headers={'x-print-queue':'cups-a:631|Samsung','x-print-source':'MultiTray',
                 'x-print-media':'com10','x-print-sides':'one-sided'}
        result=client.post('/api/print/submit-preview',json=payload,headers=headers)
        assert result.status_code==200, result.text
        command=next(command for command in sent if command[0]=='lp')
        assert 'PageSize=Env10' in command
        assert 'InputSlot=MultiTray' in command
        assert 'MediaType=Envelope' in command
        assert 'media=COM10' not in command
        rejected=client.post('/api/print/submit-preview',json=payload,
                             headers={**headers,'x-print-sides':'two-sided-long-edge'})
        assert rejected.status_code==400
        assert 'one-sided' in rejected.text.lower()


def test_paper_size_labels_across_print_flows():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    html=(root/'frontend/index.html').read_text()
    js='\n'.join(p.read_text() for p in sorted((root/'frontend/js').glob('*.js')))
    for name in ('member','guest','saved'):
        assert f'id="{name}-media"' in html
        assert f'id="{name}-media-hint"' in html
    assert html.count('#10 envelope · 4⅛ × 9½ in (105 × 241 mm)')==3
    assert "caps.media_map||{}" in js
    assert 'updatePaperSizeHint(prefix)' in js
