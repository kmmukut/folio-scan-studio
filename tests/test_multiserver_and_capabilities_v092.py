"""Folio v0.9.2: real SANE option formatting and multiple CUPS instances."""
from __future__ import annotations
import json
import subprocess
from fastapi.testclient import TestClient
from backend import app as web, devices, security


def test_realistic_indented_sane_options(monkeypatch):
    output='''Output format is not set, using pnm as a default.
  Scanner options:
    --resolution 75|100|150|200|300dpi [300]
        Sets the resolution of the scanned image.
    --mode Color|Gray [Color]
        Selects the scan mode.
    --source Flatbed|ADF [Flatbed]
        Selects the scan source.
'''
    def fake(cmd, **kwargs):
        assert cmd==['scanimage','-d','airscan:w0:Folio-net-ab Samsung M2885FW','--all-options']
        return subprocess.CompletedProcess(cmd,0,output,'')
    monkeypatch.setattr(devices.subprocess,'run',fake)
    got=devices.scanner_capabilities('airscan:w0:Folio-net-ab Samsung M2885FW')
    assert got == {'sources':['Flatbed','ADF'], 'modes':['Color','Gray'],
                   'resolutions':[75,100,150,200,300], 'available':True}


def test_sane_error_remains_diagnostic(monkeypatch):
    monkeypatch.setattr(devices.subprocess,'run',lambda args,**kwargs:
        subprocess.CompletedProcess(args,1,'','WSD scanning service unavailable'))
    got=devices.scanner_capabilities('airscan:w0:Unavailable')
    assert got['available'] is False
    assert 'WSD scanning service' in got['error']


def test_parse_cups_servers_and_reject_bad_addresses():
    assert devices.parse_cups_servers(' cups:631,198.51.100.2:631,cups:631 ')==['cups:631','198.51.100.2:631']
    for bad in ('a:0', '198.51.100.1:65536', 'cups:631;rm', 'https://printer:631', 'cups|queue', 'cups/queue'):
        try:
            devices.parse_cups_servers(bad)
        except ValueError:
            pass
        else:
            raise AssertionError('Accepted invalid CUPS address: '+bad)


def test_independent_servers_duplicate_queue_names_and_permissions(tmp_path,monkeypatch):
    servers=['198.51.100.2:631','203.0.113.70:631']
    monkeypatch.setattr(web,'CUPS_SERVERS',servers)
    monkeypatch.setattr(web,'CUPS_SERVER',servers[0])
    monkeypatch.setattr(web,'CUPS_QUEUE','')
    monkeypatch.setattr(web,'DEVICE','')
    monkeypatch.setattr(web,'DATA_DIR',tmp_path)
    monkeypatch.setattr(web,'JOBS_DIR',tmp_path/'jobs')
    monkeypatch.setattr(web,'PREVIEW_ROOT',tmp_path/'print-previews')
    monkeypatch.setenv('SCAN_STUDIO_ADMIN_PASSWORD','a-secure-testing-password')
    monkeypatch.setattr(devices,'listed_sane_devices',lambda:[])
    monkeypatch.setattr(devices,'list_cups_queues', lambda server:
        [{'queue':'Shared','ready':True,'status':'idle'}] if server==servers[0] else
        [{'queue':'Shared','ready':True,'status':'idle'}, {'queue':'ColorOffice','ready':True,'status':'idle'}])
    monkeypatch.setattr(devices,'cups_capabilities',lambda server,queue:{
        'known':True,'media':['Letter'],'duplex':True,'color':server==servers[1],
        'color_modes':['auto','gray','bw'], 'trays':[], 'resolution':[]})
    security._guest_submissions.clear();security._global_submissions.clear()
    with TestClient(web.app) as client:
        logged=client.post('/api/auth/login',json={'username':'admin','password':'a-secure-testing-password'})
        assert logged.status_code==200,logged.text
        client.headers['x-csrf-token']=logged.json()['csrf']
        available=client.get('/api/admin/devices').json()
        assert available['default_printer'] is None
        assert available['cups_servers']==servers
        same=[r for r in available['printers'] if r['queue']=='Shared']
        assert len(same)==2
        assert same[0]['id']!=same[1]['id']
        assert same[0]['server']==servers[0] and same[1]['server']==servers[1]
        assert client.get('/api/printers').json()['default'] is None
        assert client.get('/api/print/status').json()['ready'] is False
        selected=devices.printer_id(servers[1],'Shared')
        result=client.patch('/api/admin/printers/'+selected,json={'make_default':True,'guest':True})
        assert result.status_code==200,result.text
        assert client.get('/api/printers').json()['default']==selected
        assert client.get('/api/print/status').json()['server']==servers[1]
        client.post('/api/admin/guest',json={'enabled':True})
        guests=client.get('/api/guest/printers').json()['printers']
        assert [r['id'] for r in guests]==[selected]
        # Legacy unscoped printer name is ambiguous and must not pick a server.
        assert client.patch('/api/admin/printers/Shared',json={'guest':True}).status_code==404
        # Actual lp submission targets the selected CUPS host and real queue name.
        commands=[]
        monkeypatch.setattr(web.subprocess,'run',lambda args,**kw:
            (commands.append(args) or subprocess.CompletedProcess(args,0,'request id is Shared-7','')))
        dummy=tmp_path/'print.pdf'; dummy.write_bytes(b'%PDF')
        response=web.print_with_cups(dummy,queue=selected)
        assert 'Shared-7' in response
        assert commands[0][:5]==['lp','-h',servers[1],'-d','Shared']
        assert (tmp_path/'devices.json').exists()
        assert json.loads((tmp_path/'devices.json').read_text())['default_printer']==selected


def test_legacy_queue_default_migrates_to_specific_server():
    registry={'default_printer':'SamsungAirPrint', 'scanners':[],
              'printers':{'SamsungAirPrint':{'enabled':True,'guest':True,'member':True,'printer':True}}}
    records=[{'server':'198.51.100.2:631','queue':'SamsungAirPrint',
              'id':'198.51.100.2:631|SamsungAirPrint','ready':True}]
    assert devices.migrate_legacy_printers(registry,records)
    assert registry['default_printer']==records[0]['id']
    assert registry['printers'][records[0]['id']]['guest'] is True
    assert 'SamsungAirPrint' not in registry['printers']
    # Ambiguous old defaults are not mapped to a random CUPS server.
    duplicate=[*records,{'server':'cups2:631','queue':'SamsungAirPrint',
                         'id':'cups2:631|SamsungAirPrint','ready':True}]
    legacy={'default_printer':'SamsungAirPrint','printers':{'SamsungAirPrint':{'guest':True}}}
    assert devices.migrate_legacy_printers(legacy,duplicate) is False
    assert legacy['default_printer']=='SamsungAirPrint'
