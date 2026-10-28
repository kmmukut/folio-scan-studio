"""Folio v0.9.3: per-CUPS-queue paper source discovery and print validation."""
import subprocess
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pymupdf as fitz
from fastapi.testclient import TestClient
import pytest
from backend import app as web, devices, security


def pdf_bytes():
    with fitz.open() as pdf:
        page=pdf.new_page()
        page.insert_text((55,80),'Folio tray choice test')
        return pdf.tobytes()


def fake_cups(args, timeout=8):
    if args[0] == 'lpoptions':
        queue=args[args.index('-p')+1]
        if queue=='Laser':
            text=('PageSize/Page Size: *Letter A4\n'
                  'InputSlot/Paper Source: *AutoSelect/Automatic selection '
                  'Tray1/Tray 1 Tray2/Tray 2 ManualFeed/Manual feed\n')
        elif queue=='OfficeColor':
            text=('PageSize/Page Size: *Letter A4\n'
                  'media-source/Media source: *auto main alternate manual\n')
        else:
            text='PageSize/Page Size: *Letter A4\n'
        return subprocess.CompletedProcess(args,0,text,'')
    return subprocess.CompletedProcess(args,1,'','Unexpected query')


def test_ipp_and_ppd_paper_source_parsing(monkeypatch):
    monkeypatch.setattr(devices,'cups_run',fake_cups)
    laser=devices.cups_capabilities('cups-a:631','Laser')
    assert laser['source_option']=='InputSlot'
    assert laser['source_default']=='AutoSelect'
    assert [v['value'] for v in laser['paper_sources']]==['AutoSelect','Tray1','Tray2','ManualFeed']
    assert [v['label'] for v in laser['paper_sources']]==['Automatic selection','Tray 1','Tray 2','Manual feed']
    assert laser['paper_sources'][0]['is_default'] is True
    ipp=devices.cups_capabilities('cups-b:631','OfficeColor')
    assert ipp['source_option']=='media-source'
    assert [v['value'] for v in ipp['paper_sources']]==['auto','main','alternate','manual']
    assert ipp['paper_sources'][1]['label']=='Main tray'
    assert devices.cups_capabilities('cups-a:631','NoTrays')['paper_sources']==[]
    # A PPD can expose a field name other than InputSlot.
    monkeypatch.setattr(devices,'cups_run',lambda args,timeout=8:
        subprocess.CompletedProcess(args,0,'MediaPosition/Source: *Auto Source1 Source2\n',''))
    assert devices.cups_capabilities('cups-c:631','Laser')['source_option']=='MediaPosition'


@pytest.fixture
def printing_site(monkeypatch,tmp_path):
    monkeypatch.setattr(web,'DEVICE','')
    monkeypatch.setattr(web,'CUPS_QUEUE','')
    monkeypatch.setattr(web,'CUPS_SERVERS',['cups-a:631','cups-b:631'])
    monkeypatch.setattr(web,'CUPS_SERVER','cups-a:631')
    monkeypatch.setattr(web,'DATA_DIR',tmp_path)
    monkeypatch.setattr(web,'JOBS_DIR',tmp_path/'jobs')
    monkeypatch.setattr(web,'PREVIEW_ROOT',tmp_path/'print-previews')
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD','a-very-long-password-for-testing')
    monkeypatch.setattr(devices,'list_cups_queues',lambda server:
        [{'queue':'Laser','ready':True,'status':'idle'},
         {'queue':'NoTrays','ready':True,'status':'idle'}] if server=='cups-a:631'
        else [{'queue':'OfficeColor','ready':True,'status':'idle'}])
    monkeypatch.setattr(devices,'cups_run',fake_cups)
    monkeypatch.setattr(devices,'listed_sane_devices',lambda:[])
    security._guest_submissions.clear();security._global_submissions.clear()
    sent=[]
    def print_process(args,**kwargs):
        sent.append(args)
        return subprocess.CompletedProcess(args,0,'request id is Laser-2 (1 file(s))','')
    # The actual submission is mocked; CUPS discovery uses cups_run above.
    monkeypatch.setattr(web.subprocess,'run',print_process)
    with TestClient(web.app) as client:
        login=client.post('/api/auth/login',json={'username':'admin','password':'a-very-long-password-for-testing'})
        assert login.status_code==200,login.text
        client.headers['x-csrf-token']=login.json()['csrf']
        yield client,sent,tmp_path


def test_upload_print_sources_and_queue_switch(printing_site):
    client,sent,_=printing_site
    discovered=client.get('/api/printers').json()['printers']
    ppd=next(p for p in discovered if p['queue']=='Laser')
    ipp=next(p for p in discovered if p['queue']=='OfficeColor')
    assert ppd['capabilities']['source_option']=='InputSlot'
    assert ipp['capabilities']['source_option']=='media-source'
    token=client.post('/api/print/inspect',headers={'x-upload-filename':'test.pdf'},content=pdf_bytes()).json()['token']
    body={'token':token}
    okay=client.post('/api/print/submit-preview',json=body,
                     headers={'x-print-queue':ppd['id'],'x-print-source':'Tray2','x-print-media':'letter'})
    assert okay.status_code==200,okay.text
    assert ['-o','InputSlot=Tray2'] == sent[-1][sent[-1].index('InputSlot=Tray2')-1:sent[-1].index('InputSlot=Tray2')+1]
    assert 'media=Letter' in sent[-1]
    # Distinct IPP options are sent using that server's attribute name.
    okay=client.post('/api/print/submit-preview',json=body,
                     headers={'x-print-queue':ipp['id'],'x-print-source':'alternate'})
    assert okay.status_code==200,okay.text
    assert sent[-1][:5]==['lp','-h','cups-b:631','-d','OfficeColor']
    assert 'media-source=alternate' in sent[-1]
    previous=len(sent)
    bad=client.post('/api/print/submit-preview',json=body,
                    headers={'x-print-queue':ipp['id'],'x-print-source':'Tray2'})
    assert bad.status_code==400 and 'Paper source' in bad.text
    bad=client.post('/api/print/submit-preview',json=body,
                    headers={'x-print-queue':'cups-a:631|NoTrays','x-print-source':'Tray1'})
    assert bad.status_code==400 and 'Paper source' in bad.text
    bad=client.post('/api/print/submit-preview',json=body,
                    headers={'x-print-queue':ppd['id'],'x-print-source':'Tray2;touch /tmp/hacked'})
    assert bad.status_code==400
    assert len(sent)==previous
    # Printer default doesn't create a tray option and works on all queues.
    okay=client.post('/api/print/submit-preview',json=body,
                     headers={'x-print-queue':'cups-a:631|NoTrays'})
    assert okay.status_code==200
    assert not any(t.startswith(('InputSlot=','media-source=')) for t in sent[-1])


def test_guest_and_saved_document_print_selected_tray(printing_site,monkeypatch):
    client,sent,tmp_path=printing_site
    queue='cups-a:631|Laser'
    allow=client.patch('/api/admin/printers/'+queue,json={'guest':True,'make_default':True})
    assert allow.status_code==200,allow.text
    assert client.post('/api/admin/guest',json={'enabled':True}).status_code==200
    # Guest has its own authorization cookie and its own source choices.
    with TestClient(web.app) as guest:
        csrf=guest.get('/api/guest/status').json()['csrf']
        printers=guest.get('/api/guest/printers').json()['printers']
        assert printers[0]['capabilities']['paper_sources'][2]['value']=='Tray2'
        response=guest.post('/api/guest/inspect',headers={'x-guest-csrf':csrf,'x-upload-filename':'guest.pdf'},content=pdf_bytes())
        assert response.status_code==200,response.text
        printed=guest.post('/api/guest/submit-preview',json={'token':response.json()['token']},
                           headers={'x-guest-csrf':csrf,'x-print-queue':queue,'x-print-source':'ManualFeed'})
        assert printed.status_code==200,printed.text
        assert 'InputSlot=ManualFeed' in sent[-1]
    # Saved Folio documents take the same print_choices and lp path.
    monkeypatch.setattr(web,'load_job',lambda _: {'pages':[{'id':'a'}], 'title':'Saved document'})
    monkeypatch.setattr(web,'make_job_pdf',lambda *a,**kw: pdf_bytes())
    printed=client.post('/api/jobs/000000000000/print',headers={'x-print-queue':queue,'x-print-source':'Tray1'})
    assert printed.status_code==200,printed.text
    assert 'InputSlot=Tray1' in sent[-1]


def test_ui_has_tray_controls_for_all_print_flows():
    root=Path(__file__).resolve().parents[1]
    html=(root/'frontend/index.html').read_text()
    js='\n'.join(p.read_text() for p in sorted((root/'frontend/js').glob('*.js')))
    for prefix in ('guest','member','saved'):
        assert f'id="{prefix}-source"' in html
    assert "'X-Print-Source':$(`#${prefix}-source`).value" in js
    assert 'caps.paper_sources||[]' in js
