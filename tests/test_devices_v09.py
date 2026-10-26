"""Folio v0.9 device migration, safety and color conversion regressions."""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
import fitz
from fastapi.testclient import TestClient
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import app as web, devices, printing, security


def fake_cups(args, timeout=8):
    if args[0]=='lpstat' and '-p' in args:
        return subprocess.CompletedProcess(args,0,
            'printer SamsungAirPrint is idle. enabled since Mon\n'
            'printer ColorDesk is idle. enabled since Tue\n','')
    if args[0]=='lpstat' and '-a' in args:
        return subprocess.CompletedProcess(args,0,
            'SamsungAirPrint accepting requests since Mon\nColorDesk accepting requests since Tue\n','')
    if args[0]=='lpoptions':
        queue=args[args.index('-p')+1]
        if queue=='ColorDesk':
            txt='PageSize/Page Size: Letter *A4 Legal\nColorModel/Color Mode: *RGB Gray\nDuplex/2-sided: *None DuplexNoTumble\nInputSlot/Paper Source: *Auto Tray1 Tray2\n'
        else:
            txt='PageSize/Page Size: *Letter A4 Legal\nColorModel/Color Mode: *Gray\nDuplex/2-sided: *None DuplexNoTumble\n'
        return subprocess.CompletedProcess(args,0,txt,'')
    return subprocess.CompletedProcess(args,1,'','Unrecognized CUPS command')


def sane_state():
    rows=[{'sane_name':'airscan:w3:Samsung M2885FW', 'description':'WSD Samsung ip=192.0.2.124'}]
    for e in current_config.get('scanners',[]):
        if e['kind']=='managed' and e['enabled']:
            rows.append({'sane_name':f"airscan:w5:Folio-{e['id']} {e['name']}", 'description':f"{e['protocol']} scanner ip={e['ip']}"})
    return rows

current_config={}

@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'DEVICE', 'airscan:w0:Samsung M2885FW')
    monkeypatch.setattr(web, 'CUPS_QUEUE', 'SamsungAirPrint')
    monkeypatch.setattr(web,'DATA_DIR',tmp_path)
    monkeypatch.setattr(web,'JOBS_DIR',tmp_path/'jobs')
    monkeypatch.setattr(web,'PREVIEW_ROOT',tmp_path/'print-previews')
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD','a-long-testing-password-123')
    monkeypatch.setattr(devices,'cups_run',fake_cups)
    def listed():
        current_config.clear();current_config.update(web.registry())
        return sane_state()
    monkeypatch.setattr(devices,'listed_sane_devices',listed)
    monkeypatch.setattr(devices,'scanner_capabilities',lambda name: {'available':True,'sources':['ADF','Flatbed'], 'modes':['Color','Gray'],'resolutions':[150,300]})
    security._guest_submissions.clear();security._global_submissions.clear()
    with TestClient(web.app) as client:
        status=client.post('/api/auth/login',json={'username':'admin','password':'a-long-testing-password-123'})
        assert status.status_code==200,status.text
        client.headers['x-csrf-token']=status.json()['csrf']
        yield client,tmp_path


def test_import_and_live_cups_queues(ui):
    client,tmp=ui
    config=client.get('/api/admin/devices').json()
    assert config['default_scanner']=='original'
    assert config['default_printer']==devices.printer_id(web.CUPS_SERVER,web.CUPS_QUEUE)
    assert {p['queue'] for p in config['printers']}=={'SamsungAirPrint','ColorDesk'}
    assert next(p for p in config['printers'] if p['queue']=='SamsungAirPrint')['capabilities']['color'] is False
    assert next(p for p in config['printers'] if p['queue']=='ColorDesk')['capabilities']['color'] is True
    assert config['scanners'][0]['connected'] is True
    assert (tmp/'devices.json').exists()
    assert (tmp/'managed-airscan.conf').exists()
    assert client.get('/api/printers').json()['printers'][0]['queue']=='SamsungAirPrint'


def test_add_edit_scanner_and_dont_touch_legacy_mount(ui):
    client,tmp=ui
    add=client.post('/api/admin/scanners',json={'name':'Office Epson','ip':'10.20.30.40','protocol':'eSCL',
                                                   'port':80,'path':'/eSCL','make_default':True})
    assert add.status_code==201,add.text
    ident=add.json()['id']
    txt=(tmp/'managed-airscan.conf').read_text()
    assert 'http://10.20.30.40:80/eSCL, eSCL' in txt
    status=client.get(f'/api/admin/scanners/{ident}/test')
    assert status.status_code==200,status.text
    assert status.json()['capabilities']['resolutions']==[150,300]
    update=client.patch(f'/api/admin/scanners/{ident}',json={'ip':'10.20.30.41','port':8080})
    assert update.status_code==200,update.text
    assert '10.20.30.41:8080' in (tmp/'managed-airscan.conf').read_text()
    assert '10.20.30.40' not in (tmp/'managed-airscan.conf').read_text()
    # The default scanner can now be removed: Folio selects another enabled
    # device automatically, rather than forcing users to change defaults first.
    assert client.delete(f'/api/admin/scanners/{ident}').status_code==200
    assert client.get('/api/scanners').json()['default']=='original'
    assert 'Office Epson' not in (tmp/'managed-airscan.conf').read_text()


def test_existing_scanner_ip_can_migrate_to_managed(ui):
    client,tmp=ui
    result=client.patch('/api/admin/scanners/original',json={'ip':'192.0.2.220',
                           'name':'Samsung replacement IP','port':8018,'path':'/wsd/scan','protocol':'WSD'})
    assert result.status_code==200,result.text
    assert result.json()['kind']=='managed'
    assert '192.0.2.220' in (tmp/'managed-airscan.conf').read_text()
    scan=client.get('/api/scanners').json()
    assert scan['scanners'][0]['connected']
    assert scan['scanners'][0]['name']=='Samsung replacement IP'


def test_unsafe_endpoints_rejected(ui):
    client,tmp=ui
    for path in ('/wsd/../scan','//evil','/wsd/scan;rm -rf /'):
        r=client.post('/api/admin/scanners',json={'name':'unsafe','ip':'192.168.1.9',
                                                   'protocol':'WSD','path':path})
        assert r.status_code==400,r.text
    r=client.post('/api/admin/scanners',json={'name':'test','ip':'evil-server','protocol':'WSD'})
    assert r.status_code==400
    r=client.patch('/api/admin/printers/SamsungAirPrint;whoami',json={'guest':True})
    assert r.status_code==400


def test_queue_permissions_and_default(ui):
    client,tmp=ui
    saved=client.patch('/api/admin/printers/ColorDesk',json={'make_default':True,'guest':False,'member':True})
    assert saved.status_code==200,saved.text
    assert client.get('/api/printers').json()['default']==devices.printer_id(web.CUPS_SERVER,'ColorDesk')
    assert {p['queue'] for p in client.get('/api/printers').json()['printers']}=={'SamsungAirPrint','ColorDesk'}
    client.post('/api/admin/guest',json={'enabled':True})
    assert {p['queue'] for p in client.get('/api/guest/printers').json()['printers']}=={'SamsungAirPrint'}
    assert client.patch('/api/admin/printers/SamsungAirPrint',json={'enabled':False}).status_code==200
    assert 'SamsungAirPrint' not in [q['queue'] for q in client.get('/api/guest/printers').json()['printers']]
    assert client.patch('/api/admin/printers/ColorDesk',json={'guest':True}).status_code==200
    assert [p['queue'] for p in client.get('/api/guest/printers').json()['printers']]==['ColorDesk']
    assert client.patch('/api/admin/printers/ColorDesk',json={'enabled':False}).status_code==400
    # Sessions remain role-enforced; members cannot administer printers.
    client.post('/api/admin/users',json={'username':'alex','password':'a-member-password-123','role':'member'})
    with TestClient(web.app) as member:
        logged=member.post('/api/auth/login',json={'username':'alex','password':'a-member-password-123'})
        member.headers['x-csrf-token']=logged.json()['csrf']
        assert member.get('/api/admin/devices').status_code==403
        assert member.patch('/api/admin/printers/ColorDesk',json={'guest':True}).status_code==403


def test_explicit_queue_and_color_checks(ui):
    client,tmp=ui
    request={'x-print-queue':'SamsungAirPrint','x-print-color':'color'}
    preview=client.post('/api/print/inspect',headers={'X-Upload-Filename':'page.pdf'},content=one_page())
    assert preview.status_code==200,preview.text
    token=preview.json()['token']
    bad=client.post('/api/print/submit-preview',json={'token':token},headers=request)
    assert bad.status_code==400 and 'does not support color' in bad.text
    saved=[]
    def capture(path,title,**kwargs):
        saved.append(kwargs)
        return 'request id is ColorDesk-1'
    from pytest import MonkeyPatch
    with MonkeyPatch.context() as mp:
        mp.setattr(web,'print_with_cups',capture)
        success=client.post('/api/print/submit-preview',json={'token':token},headers={'x-print-queue':'ColorDesk','x-print-color':'color','x-print-media':'a4'})
    assert success.status_code==200,success.text
    assert saved[0]['queue']==devices.printer_id(web.CUPS_SERVER,'ColorDesk') and saved[0]['color_mode']=='color'
    assert client.post('/api/print/submit-preview',json={'token':token},headers={'x-print-queue':'NeverConfigured'}).status_code==404
    assert client.post('/api/print/submit-preview',json={'token':token},headers={'x-print-queue':'ColorDesk','x-print-media':'a6'}).status_code==400


def one_page():
    with fitz.open() as doc:
        doc.new_page().insert_text((70,100),'Hello from Folio')
        return doc.tobytes()


def test_gray_and_black_white_real_pdf_conversion(tmp_path):
    source=tmp_path/'source.pdf'
    source.write_bytes(one_page())
    for mode in ('gray','bw'):
        target=tmp_path/f'{mode}.pdf'
        assert printing.prepare_pdf(source,'pdf',target,color_mode=mode)==1
        with fitz.open(target) as doc:
            page=doc[0]
            assert page.rect.width==595 or page.rect.width>200
            assert doc.page_count==1
            imgs=page.get_images(full=True)
            assert imgs
            if mode=='bw':
                pix=fitz.Pixmap(doc, imgs[0][0])
                assert pix.n==1, '1-bit source should load as a single monochrome channel'


def test_attach_existing_sane_scanner_and_reject_duplicates(ui, monkeypatch):
    client,tmp = ui
    original_listed = devices.listed_sane_devices
    def extra_sane():
        return [*original_listed(),
                {'sane_name':'airscan:e1:Office Scanner',
                 'description':'eSCL Office Scanner ip=10.12.20.30'}]
    monkeypatch.setattr(devices,'listed_sane_devices',extra_sane)
    first=client.get('/api/admin/devices')
    assert any(s['sane_name']=='airscan:e1:Office Scanner'
               for s in first.json()['unregistered'])
    added=client.post('/api/admin/scanners/attach',json={
        'name':'Office Scanner','device_name':'airscan:e1:Office Scanner',
        'make_default':True})
    assert added.status_code==201,added.text
    attached=added.json()
    assert attached['kind']=='attached'
    assert client.get('/api/scanners').json()['default']==attached['id']
    assert client.get('/api/admin/scanners/'+attached['id']+'/test').status_code==200
    again=client.post('/api/admin/scanners/attach',json={
        'name':'Office Scanner','device_name':'airscan:e1:Office Scanner'})
    assert again.status_code==409
    assert not any(s['sane_name']=='airscan:e1:Office Scanner'
                   for s in client.get('/api/admin/devices').json()['unregistered'])
