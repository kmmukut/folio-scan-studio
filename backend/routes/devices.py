"""Scanners, printers and print servers (including the built-in one).

Route handlers moved out of app.py in 0.21. Shared state and helpers stay in
app.py and are reached as web.NAME, so tests can still swap them there.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend import airprint, airscan, cupsadmin, devices, diagnostics, ipp, printing, printserver, probe, report, tasks
from backend.version import __version__
from backend import app as web

router = APIRouter()


def available_device() -> bool:
    try:
        output = subprocess.run(['scanimage', '-L'], capture_output=True, text=True, timeout=15)
        return output.returncode == 0 and web.DEVICE in output.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


class ScannerConfigBody(BaseModel):
    name: str = Field(min_length=1, max_length=75)
    ip: str
    protocol: Literal['WSD','eSCL'] = 'WSD'
    port: int | None = Field(default=None, ge=1, le=65535)
    path: str | None = None
    enabled: bool = True
    make_default: bool = False


class ScannerAttachBody(BaseModel):
    name: str = Field(min_length=1,max_length=75)
    device_name: str = Field(min_length=1,max_length=250)
    make_default: bool = False


class ScannerUpdateBody(BaseModel):
    airscan: bool | None = None  # Share it with other computers as an AirScan scanner
    name: str | None = Field(default=None, min_length=1, max_length=75)
    ip: str | None = None
    protocol: Literal['WSD','eSCL'] | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    path: str | None = None
    enabled: bool | None = None
    make_default: bool = False


class PrinterSettingsBody(BaseModel):
    enabled: bool | None = None
    guest: bool | None = None
    member: bool | None = None
    printer: bool | None = None
    make_default: bool = False


def server_source(server: str) -> str:
    if cupsadmin.is_builtin(server):
        return 'builtin'
    # Servers from CUPS_SERVERS come back on every restart, so only the ones
    # added in Devices can be removed there.
    return 'settings' if server in web.CUPS_SERVERS else 'folio'


@router.get('/api/admin/devices')
def admin_devices(refresh: bool = False):
    if refresh:
        devices.invalidate()
    cfg, records=web.printer_inventory()
    discovered={q['id']:q for q in records}
    all_keys=list(dict.fromkeys([*discovered,*cfg.get('printers',{})]))
    seen=devices.listed_sane_devices()
    registered={d['sane_name'] for e in cfg['scanners'] if (d:=devices.match_scanner(e,seen))}
    unregistered=[s for s in seen if s['sane_name'] not in registered]
    output=[]
    for key in all_keys:
        if key in discovered:
            record=discovered[key]
        elif '|' in key:
            server, queue = key.split('|',1)
            record={'id':key,'server':server,'queue':queue,'label':f'{queue} ({server})',
                    'ready':False,'status':'Not found in CUPS'}
        else:
            continue
        output.append({**record, 'settings':devices.printer_settings(cfg,key,web.CUPS_QUEUE),
                       'capabilities':devices.cups_capabilities(record['server'],record['queue'])
                       if key in discovered else {}})
    return {'scanners':devices.scanners_state(cfg), 'unregistered':unregistered,
            'default_scanner':cfg['default_scanner'],
            'printers':output, 'default_printer':web.print_default(cfg,records),
            'cups_servers':web.cups_servers()}


def scanner_record(name: str, ip: str, protocol: str, port: int | None, path: str | None,
                   existing_id: str | None = None) -> dict:
    """devices.make_scanner, after asking a WSD scanner for its port and path
    when they're left empty (WSD has no standard ones)."""
    if protocol=='WSD' and not (port and path):
        found=probe.wsd_scanner(devices.scanner_ip(ip))
        if not found:
            raise ValueError(f'No WSD scanner answered at {ip.strip()}. Enter its port and path (in the scanner’s '
                             'network settings), or use Check a network address.')
        port,path=port or found['port'],path or found['path']
    return devices.make_scanner(name,ip,protocol,port,path,existing_id=existing_id)


@router.post('/api/admin/scanners',status_code=201)
def register_scanner(body: ScannerConfigBody):
    cfg=web.registry()
    try:
        record=scanner_record(body.name,body.ip,body.protocol,body.port,body.path)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    if any((e['id']==record['id'] or
            (e.get('kind')=='managed' and e.get('endpoint')==record['endpoint']))
           for e in cfg['scanners']):
        raise HTTPException(409,'Scanner endpoint is already configured')
    record['enabled']=body.enabled
    if body.make_default and not record['enabled']:
        raise HTTPException(400,'A disabled scanner cannot be the default')
    cfg['scanners'].append(record)
    if record['enabled'] and (body.make_default or not cfg.get('default_scanner')):
        cfg['default_scanner']=record['id']
    web.persist_registry(cfg)
    return record


@router.post('/api/admin/scanners/attach',status_code=201)
def attach_detected_scanner(body: ScannerAttachBody):
    """Attach only a scanner name actually listed by SANE, not arbitrary shell input."""
    cfg=web.registry()
    discovered=devices.listed_sane_devices()
    if not any(x['sane_name']==body.device_name for x in discovered):
        raise HTTPException(404,'Device no longer appears in SANE; refresh and try again')
    if any((match:=devices.match_scanner(e,discovered)) and match['sane_name']==body.device_name
           for e in cfg['scanners']):
        raise HTTPException(409,'Scanner is already registered')
    if not devices.DEVICE_NAME.fullmatch(body.name.strip()):
        raise HTTPException(400,'Use letters, numbers, spaces and punctuation in scanner name')
    identity='sane-'+hashlib.sha256(body.device_name.encode()).hexdigest()[:12]
    record={'id':identity,'name':body.name.strip(),'kind':'attached',
            'hint':body.device_name,'enabled':True}
    cfg['scanners'].append(record)
    if body.make_default or not cfg.get('default_scanner'):
        cfg['default_scanner']=identity
    web.persist_registry(cfg)
    return record


@router.patch('/api/admin/scanners/{scanner_id}')
def modify_scanner(scanner_id: str, body: ScannerUpdateBody):
    cfg=web.registry()
    original=next((e for e in cfg['scanners'] if e['id']==scanner_id),None)
    if not original:
        raise HTTPException(404,'Scanner not found')
    if web.scan_state['active'] and scanner_id in (web.scan_state.get('scanner_id'),cfg['default_scanner']):
        raise HTTPException(409,'Wait for the current scan to finish')
    if original['kind']=='attached':
        if body.ip is not None:
            try:
                original.update(scanner_record(body.name or original['name'],body.ip,
                    body.protocol or 'WSD',body.port,body.path,existing_id=original['id']))
            except ValueError as exc:
                raise HTTPException(400,str(exc)) from exc
        elif any(v is not None for v in (body.port,body.path,body.protocol)):
            raise HTTPException(400,'Enter an IP address to move this scanner to Folio-managed WSD/eSCL')
        elif body.name:
            original['name']=body.name[:75]
    else:
        if original['kind']=='existing':
            # Promote the original device into a Folio-managed configuration if the
            # administrator changes its endpoint. Leave the RO legacy mount intact.
            if body.ip is not None:
                try:
                    original.update(scanner_record(body.name or original['name'],body.ip,
                        body.protocol or 'WSD',body.port,body.path,existing_id=original['id']))
                except ValueError as exc:
                    raise HTTPException(400,str(exc)) from exc
            elif any(x is not None for x in (body.port,body.path,body.protocol)):
                raise HTTPException(400,'Enter the scanner IP to manage this endpoint in Folio')
            elif body.name:
                original['name']=body.name[:75]
        else:
            protocol=body.protocol or original['protocol']
            same=protocol==original['protocol']  # Another protocol doesn't keep the old port and path
            try:
                new=scanner_record(body.name or original['name'],body.ip or original['ip'],protocol,
                                   body.port or (original['port'] if same else None),
                                   body.path or (original['path'] if same else None),existing_id=original['id'])
            except ValueError as exc:
                raise HTTPException(400,str(exc)) from exc
            original.update(new)
    # Updating a scanner keeps its stable Folio ID; still reject a duplicate
    # endpoint if another registered scanner already uses the new IP/port/path.
    if original.get('kind')=='managed' and any(
        e['id']!=scanner_id and e.get('kind')=='managed' and
        e.get('endpoint')==original['endpoint'] for e in cfg['scanners']):
        raise HTTPException(409,'Another scanner already uses this endpoint')
    if body.enabled is not None:
        original['enabled']=body.enabled
    if body.airscan is not None:
        if body.airscan and original.get('kind') not in airscan.SHAREABLE:
            raise HTTPException(400,'Only scanners added in Folio can be shared as AirScan')
        original['airscan']=body.airscan
    if body.make_default:
        if not original.get('enabled',True):
            raise HTTPException(400,'A disabled scanner cannot be the default')
        cfg['default_scanner']=scanner_id
    elif cfg.get('default_scanner') == scanner_id and not original.get('enabled',True):
        cfg['default_scanner']=next((s['id'] for s in cfg['scanners']
                                     if s['id']!=scanner_id and s.get('enabled',True)),None)
    elif not cfg.get('default_scanner') and original.get('enabled',True):
        cfg['default_scanner']=scanner_id
    web.persist_registry(cfg)
    return original


@router.get('/api/admin/airscan')
def airscan_status():
    """Which scanners are shared as AirScan, whether they're announced, and the
    service file for announcing one by hand."""
    return airscan.sharing.status()


@router.delete('/api/admin/scanners/{scanner_id}')
def unregister_scanner(scanner_id: str):
    cfg=web.registry()
    if web.scan_state['active'] and scanner_id==web.scan_state.get('scanner_id'):
        raise HTTPException(409,'Wait for the current scan to finish')
    legacy_entry = next((e for e in cfg['scanners'] if e['id']==scanner_id),None)
    if scanner_id=='original' and legacy_entry and legacy_entry['kind']=='existing':
        raise HTTPException(400,'Edit the legacy scanner and enter its IP before removing it')
    before=len(cfg['scanners'])
    cfg['scanners']=[e for e in cfg['scanners'] if e['id']!=scanner_id]
    if len(cfg['scanners'])==before:
        raise HTTPException(404,'Scanner not found')
    if cfg.get('default_scanner') == scanner_id:
        cfg['default_scanner']=next((e['id'] for e in cfg['scanners'] if e.get('enabled',True)),None)
    web.persist_registry(cfg)
    return {'deleted':True}


@router.get('/api/admin/scanners/{scanner_id}/test')
def test_scanner(scanner_id: str):
    # A test is an explicit request to talk to the device, never a cached answer.
    devices.invalidate('sane')
    entry,name=web.get_selected_scanner(scanner_id)
    return {'scanner':scanner_id, 'name':entry['name'], 'device':name,
            'capabilities':devices.scanner_capabilities(name)}


@router.patch('/api/admin/printers/{queue}')
def configure_printer(queue: str, body: PrinterSettingsBody):
    try:
        devices.validate_printer_reference(queue)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    cfg, records=web.printer_inventory()
    target=devices.resolve_printer(queue,records)
    if not target:
        raise HTTPException(404,'Add the printer in CUPS first, then refresh Devices')
    key=target['id']
    flags=devices.printer_settings(cfg,key,web.CUPS_QUEUE)
    for field in ('enabled','guest','member','printer'):
        value=getattr(body,field)
        if value is not None:
            flags[field]=value
    if body.make_default:
        if not flags['enabled']:
            raise HTTPException(400,'Disabled printer cannot be default')
        cfg['default_printer']=key
    elif web.print_default(cfg,records)==key and not flags['enabled']:
        raise HTTPException(400,'Choose another default printer before disabling this queue')
    cfg.setdefault('printers',{})[key]=flags
    web.persist_registry(cfg)
    return {'id':key,'queue':target['queue'], 'server':target['server'],
            'settings':flags,'default':cfg['default_printer']==key}


def known_printer(printer: str) -> dict:
    try:
        devices.validate_printer_reference(printer)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    target = devices.resolve_printer(printer, devices.all_cups_queues(web.cups_servers()))
    if not target:
        raise HTTPException(404, 'Printer not found in CUPS. Refresh Devices.')
    return target


class CupsLoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class AddPrinterBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    device_uri: str | None = Field(default=None, max_length=512)  # A detected device...
    connection: Literal['ipp', 'socket', 'lpd'] | None = None      # ...or an address
    address: str | None = Field(default=None, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    path: str | None = Field(default=None, max_length=120)
    driver: str = Field(min_length=1, max_length=240)              # 'everywhere' or a CUPS driver name
    location: str = Field(default='', max_length=127)
    description: str = Field(default='', max_length=127)
    make_default: bool = False
    guest: bool = False


class PausePrinterBody(BaseModel):
    paused: bool


class PrintServerBody(BaseModel):
    server: str = Field(min_length=1, max_length=120)


@router.get('/api/admin/cups/servers')
def list_print_servers():
    return [{'server': server, 'admin': cupsadmin.admin_status(web.DATA_DIR, server),
             'reachable': not devices.cups_backoff(server), 'source': server_source(server),
             'announcing': bool(airprint.status().get('mounted')) if cupsadmin.is_builtin(server) else None,
             'web_admin': web_admin_status() if cupsadmin.is_builtin(server) else None}
            for server in web.cups_servers()]


@router.post('/api/admin/cups/servers', status_code=201)
def add_print_server(body: PrintServerBody):
    text = body.server.strip()
    if text.lower().startswith(('http://', 'https://', 'ipp://')):
        text = text.split('://', 1)[1].split('/', 1)[0]
    try:
        server = devices.parse_cups_servers(text)[0]
    except (ValueError, IndexError) as exc:
        raise HTTPException(400, 'Enter the print server’s address, such as 192.168.1.10 or cups:631') from exc
    if ':' not in server:
        server += ':631'  # One spelling per server keeps printer IDs stable
    cfg = web.registry()
    current = web.cups_servers(cfg)
    if server in current:
        raise HTTPException(409, f'{server} is already a print server')
    if len(current) >= 16:
        raise HTTPException(400, 'Folio supports up to 16 print servers')
    probe = diagnostics.probe_cups_server(server)
    if probe['status'] == 'fail':
        raise HTTPException(400, f'Folio could not reach CUPS at {server}: {probe["detail"]}')
    cfg.setdefault('cups_servers', []).append(server)
    web.persist_registry(cfg)
    devices.invalidate('cups')
    return {'server': server, 'detail': probe['detail']}


@router.delete('/api/admin/cups/servers/{server}')
def remove_print_server(server: str):
    if cupsadmin.is_builtin(server):
        raise HTTPException(400, 'The built-in print server is part of Folio. Set FOLIO_BUILTIN_CUPS=off to turn it off.')
    if server in web.CUPS_SERVERS:
        raise HTTPException(400, 'This server is set in CUPS_SERVERS. Remove it there and restart Folio.')
    cfg = web.registry()
    if server not in cfg.get('cups_servers', []):
        raise HTTPException(404, 'Unknown print server')
    cfg['cups_servers'].remove(server)
    # Its queues disappear from Folio; forget their settings and the stored login.
    cfg['printers'] = {key: value for key, value in cfg.get('printers', {}).items()
                       if not key.startswith(f'{server}|')}
    if (cfg.get('default_printer') or '').startswith(f'{server}|'):
        cfg['default_printer'] = None
    web.persist_registry(cfg)
    cupsadmin.disconnect_admin(web.DATA_DIR, server)
    devices.invalidate('cups')
    return {'removed': server}


@router.put('/api/admin/cups/servers/{server}/login')
def cups_connect(server: str, body: CupsLoginBody):
    web.configured_server(server)
    if cupsadmin.is_builtin(server):
        raise HTTPException(400, 'The built-in print server is connected automatically.')
    return web.cups_call(cupsadmin.connect_admin, web.DATA_DIR, server, body.username, body.password)


@router.delete('/api/admin/cups/servers/{server}/login')
def cups_disconnect(server: str):
    web.configured_server(server)
    if cupsadmin.is_builtin(server):
        raise HTTPException(400, 'The built-in print server is connected automatically.')
    cupsadmin.disconnect_admin(web.DATA_DIR, server)
    return cupsadmin.admin_status(web.DATA_DIR, server)


@router.get('/api/admin/cups/servers/{server}/devices')
def cups_devices(server: str):
    conn = web.cups_call(cupsadmin.connection, web.DATA_DIR, web.configured_server(server), admin=True)
    return {'devices': web.cups_call(cupsadmin.discover, conn)}


@router.get('/api/admin/cups/servers/{server}/makes')
def cups_makes(server: str):
    conn = cupsadmin.connection(web.DATA_DIR, web.configured_server(server))
    return {'makes': web.cups_call(cupsadmin.makes, conn)}


@router.get('/api/admin/cups/servers/{server}/drivers')
def cups_drivers(server: str, make: str, model: str = ''):
    if not 1 <= len(make) <= 80 or len(model) > 120:
        raise HTTPException(400, 'Choose a manufacturer')
    conn = cupsadmin.connection(web.DATA_DIR, web.configured_server(server))
    drivers = web.cups_call(cupsadmin.models, conn, make)
    return {'drivers': cupsadmin.rank_drivers(drivers, model, limit=60), 'total': len(drivers)}


class DriverlessCheckBody(BaseModel):
    device_uri: str | None = Field(default=None, max_length=512)
    connection: Literal['ipp', 'socket', 'lpd'] | None = None
    address: str | None = Field(default=None, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    path: str | None = Field(default=None, max_length=120)


@router.post('/api/admin/cups/servers/{server}/driverless-check')
def check_driverless(server: str, body: DriverlessCheckBody):
    """Before adding a printer as driverless: will it really work, and if not,
    which of this server's drivers fits it best?"""
    web.configured_server(server)
    if body.device_uri:
        uri = body.device_uri
    elif body.address:
        uri = web.cups_call(cupsadmin.manual_uri, body.connection or 'ipp', body.address, body.port, body.path)
    else:
        raise HTTPException(400, 'Give the printer’s address')
    support = cupsadmin.driverless_support(uri)
    if support is None:
        return {'checked': False}
    suggested = suggest_driver(server, support['make_model']) if not support['full'] else None
    return {'checked': True, **support, 'suggested': suggested}


def suggest_driver(server: str, make_model: str) -> dict | None:
    """This print server's best driver for a printer model, if it has one."""
    if not make_model:
        return None
    try:
        candidates = cupsadmin.models(cupsadmin.connection(web.DATA_DIR, server), make_model.split()[0])
    except ipp.IPPError:
        return None
    ranked = cupsadmin.rank_drivers(candidates, make_model, limit=1)
    return ranked[0] if ranked else None


class AddressCheckBody(BaseModel):
    address: str = Field(min_length=1, max_length=253)
    server: str | None = Field(default=None, max_length=300)  # For a driver suggestion


@router.post('/api/admin/devices/check-address')
def check_address(body: AddressCheckBody):
    """What a printer's or scanner's address offers, to fill in the forms.
    Only asks the device; nothing is printed or scanned."""
    try:
        found = probe.check(body.address)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    printing = found['printing']
    needs_driver = not (printing['ipp'] or {}).get('full') and (printing['ipp'] or printing['jetdirect'] or printing['lpd'])
    suggested = suggest_driver(web.configured_server(body.server), found['make_model']) if body.server and needs_driver else None
    printing['suggested'] = suggested
    printing['recommended'] = probe.recommend_printer(found, suggested)
    return found


class ReportBody(BaseModel):
    printer: str | None = Field(default=None, max_length=300)
    scanner: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, min_length=1, max_length=253)  # Not added (yet)
    device_uri: str | None = Field(default=None, min_length=1, max_length=512)  # A printer found by searching
    problem: str | None = Field(default=None, max_length=1000)  # What Folio said when it failed
    name: str | None = Field(default=None, max_length=100)  # The name typed for it: left out of the report


def address_offers(host: str | None) -> dict | None:
    """What an address offers (the address check), for a report; None if it can't be asked."""
    if not host:
        return None
    try:
        return probe.check(host)
    except ValueError:
        return None


@router.post('/api/admin/devices/report')
def hardware_report(body: ReportBody):
    """A hardware report for Folio's compatibility list, from what the device and
    the print server answer now (see report.py). Folio sends it nowhere: the
    administrator reads it and submits it on GitHub."""
    if sum(x is not None for x in (body.printer, body.scanner, body.address, body.device_uri)) != 1:
        raise HTTPException(400, 'Choose one printer, scanner or address')
    known = [*web.cups_servers(), socket.gethostname()]
    if body.printer:
        target = known_printer(body.printer)
        try:
            state = cupsadmin.printer_state(cupsadmin.connection(web.DATA_DIR, target['server']), target['queue'])
        except ipp.IPPError as exc:
            state = {'error': str(exc)}
        host = report.uri_host(state.get('device_uri'))
        with ThreadPoolExecutor(2) as pool:  # Each may wait several seconds for an answer
            driverless = pool.submit(cupsadmin.driverless_support, state.get('device_uri') or '')
            offers = pool.submit(address_offers, host)
        return report.printer(state=state, capabilities=devices.cups_capabilities(target['server'], target['queue']),
                              driverless=driverless.result(), offers=offers.result(),
                              builtin=cupsadmin.is_builtin(target['server']), problem=body.problem,
                              hosts=[*known, host], names=[target['queue'], body.name])
    if body.scanner:
        cfg = web.registry()
        entry = next((e for e in cfg['scanners'] if e['id'] == body.scanner), None)
        if not entry:
            raise HTTPException(404, 'Scanner not found')
        busy = bool(web.scan_state['active'])  # Then nothing extra is asked of the scanner
        if not busy:
            devices.invalidate('sane')
        state = devices.scanners_state({**cfg, 'scanners': [entry]})[0]
        host = entry.get('ip') if entry.get('kind') == 'managed' else None
        shared = next((s for s in airscan.sharing.status()['scanners'] if s['id'] == entry['id']), {}) \
            if entry.get('airscan') else None
        return report.scanner(entry=entry, state=state, offers=None if busy else address_offers(host), shared=shared,
                              busy=busy, problem=body.problem, hosts=[*known, host],
                              names=[entry.get('name'), body.name])
    host = body.address.strip() if body.address else report.uri_host(body.device_uri)
    return report.address(offers=address_offers(host), device_uri=body.device_uri, problem=body.problem,
                          hosts=[*known, host], names=[body.name])


class WebAdminBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=256)


def web_admin_status() -> dict:
    state = printserver.status().get('web_admin') or {'enabled': False, 'username': None, 'error': None}
    return {'enabled': bool(state.get('enabled')), 'username': state.get('username'), 'error': state.get('error'),
            'pending': (printserver.requests_dir(web.DATA_DIR) / 'web-admin.json').exists(), 'port': airprint.port()}


def request_web_admin(request: dict) -> dict:
    """Hand a request to the print server's supervisor (it runs as root and
    owns the accounts) and wait briefly for its answer."""
    if not printserver.enabled():
        raise HTTPException(409, 'The built-in print server is switched off (FOLIO_BUILTIN_CUPS)')
    folder = printserver.requests_dir(web.DATA_DIR)
    folder.mkdir(mode=0o700, exist_ok=True)
    before = (printserver.status().get('web_admin') or {}).get('changed_at', 0)
    fd, temporary = tempfile.mkstemp(prefix='.request-', dir=folder)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:  # mkstemp: readable by Folio (and root) only
        json.dump(request, handle)
    request_file = folder / 'web-admin.json'
    os.replace(temporary, request_file)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = printserver.status().get('web_admin') or {}
        if state.get('changed_at', 0) > before and not request_file.exists():
            if state.get('error'):
                raise HTTPException(400, state['error'])
            return web_admin_status()
        time.sleep(.2)
    try:  # Not picked up: don't leave a password lying around
        request_file.unlink()
    except FileNotFoundError:
        return web_admin_status()  # Picked up just now
    raise HTTPException(503, 'The built-in print server did not answer. See Diagnostics, then try again.')


@router.get('/api/admin/cups/builtin/web-admin')
def get_web_admin():
    return web_admin_status()


@router.put('/api/admin/cups/builtin/web-admin')
def set_web_admin(body: WebAdminBody):
    """Turn on CUPS's own web administration with this login (or change it)."""
    return request_web_admin({'enabled': True, 'username': body.username.strip(), 'password': body.password})


@router.delete('/api/admin/cups/builtin/web-admin')
def remove_web_admin():
    return request_web_admin({'enabled': False})


@router.post('/api/admin/cups/servers/{server}/printers', status_code=202)
def cups_add_printer(server: str, body: AddPrinterBody, request: Request):
    conn = web.cups_call(cupsadmin.connection, web.DATA_DIR, web.configured_server(server), admin=True)
    if body.device_uri:
        if not cupsadmin.was_discovered(server, body.device_uri):
            raise HTTPException(400, 'That printer is no longer in the list of detected devices. Press Find printers again.')
        device_uri = body.device_uri
    elif body.connection and body.address:
        device_uri = web.cups_call(cupsadmin.manual_uri, body.connection, body.address, body.port, body.path)
    else:
        raise HTTPException(400, 'Choose a detected printer or enter its network address')
    if not cupsadmin.PRINTER_NAME.fullmatch(body.name):
        raise HTTPException(400, 'Printer names use letters, numbers, dot, dash and underscore (no spaces)')
    devices.invalidate('cups')
    if any(q['queue'].lower() == body.name.lower() for q in devices.all_cups_queues([server])):
        # CUPS would silently replace an existing queue of that name.
        raise HTTPException(409, f'A printer named {body.name} already exists on {server}. Choose another name.')
    key = devices.printer_id(server, body.name)

    def work(task: tasks.Task) -> dict:
        task.update('Setting up the printer in CUPS…', .2)
        web.cups_call(cupsadmin.add_printer, conn, body.name, device_uri, body.driver, body.description, body.location)
        cfg = web.registry()
        cfg.setdefault('printers', {})[key] = {'enabled': True, 'guest': body.guest, 'member': True, 'printer': True}
        if body.make_default or not cfg.get('default_printer'):
            cfg['default_printer'] = key
        web.persist_registry(cfg)
        task.update('Checking the new printer…', .8)
        return {'id': key, 'queue': body.name, 'server': server, 'state': web.cups_call(cupsadmin.printer_state, conn, body.name)}
    return web.task_response(tasks.runner.submit('printer-setup', 'print', web.task_owner(request), work, limit=web.job_limit(request)))


@router.delete('/api/admin/printers/{printer}')
def cups_remove_printer(printer: str):
    target = known_printer(printer)
    conn = web.cups_call(cupsadmin.connection, web.DATA_DIR, target['server'], admin=True)
    web.cups_call(cupsadmin.delete_printer, conn, target['queue'])
    cfg = web.registry()
    cfg.get('printers', {}).pop(target['id'], None)
    if cfg.get('default_printer') == target['id']:
        cfg['default_printer'] = None
    web.persist_registry(cfg)
    return {'deleted': target['id']}


@router.get('/api/admin/printers/{printer}/state')
def cups_printer_state(printer: str):
    target = known_printer(printer)
    return web.cups_call(cupsadmin.printer_state, cupsadmin.connection(web.DATA_DIR, target['server']), target['queue'])


@router.post('/api/admin/printers/{printer}/pause')
def cups_pause_printer(printer: str, body: PausePrinterBody):
    target = known_printer(printer)
    conn = web.cups_call(cupsadmin.connection, web.DATA_DIR, target['server'], admin=True)
    web.cups_call(cupsadmin.set_paused, conn, target['queue'], body.paused)
    return web.cups_call(cupsadmin.printer_state, conn, target['queue'])


@router.post('/api/admin/printers/{printer}/test-page')
def cups_test_page(printer: str, request: Request):
    target = known_printer(printer)
    with tempfile.TemporaryDirectory(prefix='folio-test-page-') as tmp:
        page = Path(tmp) / 'test-page.pdf'
        printing.test_page(page, printer=target['queue'], server=target['server'],
                           user=request.state.username, version=__version__)
        outcome = web.print_with_cups(page, 'Folio test page', queue=target['id'], user=request.state.username)
    web.log_print({'queue': target['id'], 'user': request.state.username}, 1, 'test-page', outcome)
    return web.cups_receipt(outcome)


@router.get('/api/admin/printers/{printer}/supplies')
def cups_printer_supplies(printer: str):
    target = known_printer(printer)
    return {'supplies': web.cups_call(web.printer_supplies, target['server'], target['queue'])}


@router.get('/api/admin/printers/{printer}/jobs')
def cups_printer_jobs(printer: str, which: Literal['not-completed', 'completed'] = 'not-completed'):
    """Everyone's jobs on one printer; titles and owners need the admin login."""
    target = known_printer(printer)
    admin = cupsadmin.admin_status(web.DATA_DIR, target['server'])['connected']
    conn = cupsadmin.connection(web.DATA_DIR, target['server'], admin=admin)
    return {'jobs': web.cups_call(cupsadmin.list_jobs, conn, queue=target['queue'], which=which, limit=30),
            'details_hidden': not admin}


@router.get('/api/scanners')
def scanners_available():
    cfg=web.registry()
    return {'default':cfg['default_scanner'],
            'scanners':[e for e in devices.scanners_state(cfg) if e.get('enabled',True)]}


@router.get('/api/scanner')
def scanner():
    cfg=web.registry()
    entry=next((e for e in cfg['scanners'] if e['id']==cfg.get('default_scanner')),None)
    # Compatible response for v0.8 clients; v0.9 uses /api/scanners.
    if entry and entry['kind']=='existing':
        return {'connected': available_device(), 'name':entry['name'],
                'sources':['ADF','Flatbed'], 'modes':['Color','Gray'],
                'resolutions':list(web.RESOLUTIONS), 'device':web.DEVICE}
    matching=next((e for e in devices.scanners_state(cfg) if e['id']==cfg['default_scanner']),None)
    return {'connected':bool(matching and matching['connected']),
            'name':matching['name'] if matching else 'Scanner',
            'protocol':(matching.get('protocol') or 'SANE') if matching else None,
            **(matching['capabilities'] if matching else {}),
            'device':matching['sane_name'] if matching else None}
