from __future__ import annotations
import io
import json
import subprocess
import time
import sys
from pathlib import Path
import fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend import printing,app as appmod,security


def pdf_data(pages=3):
    with fitz.open() as doc:
        for n in range(pages):
            page=doc.new_page(width=612,height=792)
            page.insert_text((60,75),f'DOCUMENT PAGE {n+1}',fontsize=22)
        return doc.tobytes()

@pytest.fixture
def qa(tmp_path,monkeypatch):
    monkeypatch.setattr(appmod,'DEVICE','airscan:w0:Samsung M2885FW')
    monkeypatch.setattr(appmod,'CUPS_QUEUE','SamsungAirPrint')
    from backend import devices
    monkeypatch.setattr(devices,'list_cups_queues', lambda server:[
        {'queue':'SamsungAirPrint','ready':True,'status':'idle'}])
    monkeypatch.setattr(devices,'cups_capabilities', lambda *a: {
        'known':False,'media':[],'duplex':None,'color':None,'color_modes':['auto','color','gray','bw']})
    monkeypatch.setattr(appmod,'DATA_DIR',tmp_path)
    monkeypatch.setattr(appmod,'JOBS_DIR',tmp_path/'jobs')
    monkeypatch.setattr(appmod,'PREVIEW_ROOT',tmp_path/'print-previews')
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD','qa-admin-long-password')
    security._guest_submissions.clear(); security._global_submissions.clear()
    with TestClient(appmod.app) as client:
        login=client.post('/api/auth/login',json={'username':'admin','password':'qa-admin-long-password'})
        assert login.status_code==200
        client.headers['X-CSRF-Token']=login.json()['csrf']
        yield client,monkeypatch,tmp_path


def test_pdf_inspect_preview_range_and_nup(qa):
    client,monkeypatch,tmp=qa
    seen=[]
    def cups(path,title,**choices):
        with fitz.open(path) as doc:
            seen.append((doc.page_count,[doc[n].get_text() for n in range(doc.page_count)],choices))
        return 'request id is SamsungAirPrint-9'
    monkeypatch.setattr(appmod,'print_with_cups',cups)
    response=client.post('/api/print/inspect',headers={'X-Upload-Filename':'sample.pdf'},content=pdf_data(7))
    assert response.status_code==200,response.text
    data=response.json();assert data['pages']==7 and data['kind']=='pdf'
    jpeg=client.get(f'/api/print/preview/{data["token"]}/page/4')
    assert jpeg.status_code==200 and jpeg.content[:3]==b'\xff\xd8\xff'
    r=client.post('/api/print/submit-preview',json={'token':data['token']},headers={
        'X-Print-Pages':'2,4-5', 'X-Print-Nup':'4','X-Print-Sides':'two-sided-long-edge',
        'X-Print-Media':'a5', 'X-Print-Orientation':'landscape','X-Print-Scale':'90',
        'X-Print-Quality':'best','X-Print-Border':'single','X-Print-Copies':'2'})
    assert r.status_code==200,r.text
    assert r.json()['pages_selected']==3
    assert seen[0][0]==3
    assert ['DOCUMENT PAGE 2','DOCUMENT PAGE 4','DOCUMENT PAGE 5']==[s.strip() for s in seen[0][1]]
    options=seen[0][2]
    assert (options['nup'],options['media'],options['orientation'],options['scale'],options['quality'])==(4,'a5','landscape','90','best')
    assert client.post('/api/print/submit-preview',json={'token':data['token']},headers={'X-Print-Pages':'8'}).status_code==400


def test_guest_inspect_limit_and_cookie_isolation(qa):
    client,monkeypatch,tmp=qa
    client.post('/api/admin/guest',json={'enabled':True})
    guest=TestClient(appmod.app)
    csrf=guest.get('/api/guest/status').json()['csrf']
    bad=guest.post('/api/guest/inspect',headers={'X-Guest-CSRF':csrf,'X-Upload-Filename':'long.pdf'},content=pdf_data(21))
    assert bad.status_code==413,bad.text
    good=guest.post('/api/guest/inspect',headers={'X-Guest-CSRF':csrf,'X-Upload-Filename':'ok.pdf'},content=pdf_data(2))
    assert good.status_code==200,good.text
    token=good.json()['token']
    other=TestClient(appmod.app)
    other.get('/api/guest/status')
    assert other.get(f'/api/guest/preview/{token}/page/1').status_code==404
    assert client.get(f'/api/print/preview/{token}/page/1').status_code==404
    assert guest.get(f'/api/guest/preview/{token}/page/2').status_code==200
    # Guest cannot print more than one copy, even with a valid preview token.
    assert guest.post('/api/guest/submit-preview',headers={'X-Guest-CSRF':csrf,'X-Print-Copies':'2'},json={'token':token}).status_code==400


def test_tolerant_pdf_and_precise_password_error(qa):
    client,_,_=qa
    valid=pdf_data(1)
    # MuPDF handles a short prefix before the PDF header; old exact first-byte
    # validation rejected such documents even though viewers open them.
    resp=client.post('/api/print/inspect',headers={'X-Upload-Filename':'prefixed.pdf'},content=b'\n\n'+valid)
    assert resp.status_code==200,resp.text
    bad=client.post('/api/print/inspect',headers={'X-Upload-Filename':'broken.pdf'},content=b'%PDF-not actually a pdf')
    assert bad.status_code==400
    assert 'PDF could not be read' in bad.json()['detail']
    with fitz.open() as doc:
        doc.new_page()
        encrypted=doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,owner_pw='abc',user_pw='secret')
    locked=client.post('/api/print/inspect',headers={'X-Upload-Filename':'locked.pdf'},content=encrypted)
    assert locked.status_code==400 and 'password' in locked.json()['detail'].lower()


def test_preview_image_and_expiration(qa,monkeypatch):
    client,monkeypatch,tmp=qa
    stream=io.BytesIO();Image.new('RGB',(480,640),'#fff').save(stream,'PNG')
    resp=client.post('/api/print/inspect',headers={'X-Upload-Filename':'photo.png'},content=stream.getvalue())
    assert resp.status_code==200
    token=resp.json()['token']
    assert resp.json()['pages']==1
    assert client.get(f'/api/print/preview/{token}/page/1').content.startswith(b'\xff\xd8\xff')
    assert client.get(f'/api/print/preview/{token}/page/2').status_code==404
    meta=tmp/'print-previews'/token/'meta.json'
    stored=json.loads(meta.read_text());stored['created']=time.time()-1200;meta.write_text(json.dumps(stored))
    assert client.get(f'/api/print/preview/{token}/page/1').status_code==410


def test_print_file_builds_safe_lp_options(qa,monkeypatch):
    client,monkeypatch,tmp=qa
    commands=[]
    def fake_run(command,**kw):
        commands.append(command)
        return subprocess.CompletedProcess(command,0,'request id is Printer-12 (1 file(s))','')
    monkeypatch.setattr(appmod.subprocess,'run',fake_run)
    src=tmp/'d.pdf';src.write_bytes(pdf_data(2))
    output=appmod.print_with_cups(src,'Print test',copies=2,media='letter',
         sides='two-sided-short-edge',nup=4,orientation='landscape',scale='fit',
         border='double',quality='normal',collate=True)
    assert 'Printer-12' in output
    cmd=commands[-1]
    assert 'media=Letter' in cmd and 'number-up=4' in cmd and 'page-border=double' in cmd
    assert 'orientation-requested=4' in cmd and 'print-quality=4' in cmd
    assert 'sides=two-sided-short-edge' in cmd and 'fit-to-page' in cmd
    assert 'collate=true' in cmd
    assert all(flag not in cmd for flag in ('-P','-k'))


def test_range_validations():
    assert printing.page_numbers('3,1-2,2',4)==[0,1,2]
    for spec in ['0','1,','3-1','2-5','1;rm -rf /','-1','1, 2-999']:
        with pytest.raises(printing.PrintFileError):printing.page_numbers(spec,4)
