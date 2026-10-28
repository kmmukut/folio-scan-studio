"""The Print desk, guest printing and print jobs.

Route handlers moved out of app.py in 0.21. Shared state and helpers stay in
app.py and are reached as web.NAME, so tests can still swap them there.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import time
import secrets
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend import cupsadmin, devices, ipp, printing, security, stats, tasks
from backend import app as web

router = APIRouter()


@router.get('/api/print/quota')
def my_print_quota(request: Request):
    return web.quota_state(request.state.username)


@router.get('/api/guest/status')
def guest_status(response: Response):
    token, csrf = security.guest_csrf()
    response.set_cookie(security.GUEST_COOKIE, token, httponly=True, secure=web.COOKIE_SECURE,
                        samesite='strict', max_age=3600, path='/')
    return {'enabled':security.guest_enabled(web.DATA_DIR), 'csrf':csrf,
            'max_megabytes':10, 'max_pages':web.GUEST_MAX_PAGES,
            'formats':['PDF','JPEG','PNG']}


def validate_print_file(path: Path, name: str, guest: bool) -> dict:
    try:
        return printing.inspect_file(path, name, guest)
    except printing.PrintFileError as exc:
        message = str(exc)
        raise HTTPException(413 if '20 pages' in message else 400, message) from exc


async def spool_upload(request: Request, path: Path, authenticated: bool) -> str:
    """Stream request body to disk with independent user and guest size ceilings."""
    limit = web.USER_PRINT_MAX_BYTES if authenticated else web.GUEST_MAX_BYTES
    try:
        size_hint = int(request.headers.get('content-length','0') or '0')
    except ValueError as exc:
        raise HTTPException(400, 'Invalid upload size') from exc
    if size_hint > limit:
        raise HTTPException(413, f'File exceeds the {limit//1048576} MB upload limit')
    name = unquote(request.headers.get('x-upload-filename','')).strip()[:180]
    if not name.lower().endswith(('.pdf','.png','.jpg','.jpeg')):
        raise HTTPException(400, 'Only PDF, JPEG and PNG files are allowed')
    total=0
    with path.open('wb') as target:
        async for chunk in request.stream():
            total+=len(chunk)
            if total>limit:
                raise HTTPException(413, f'File exceeds the {limit//1048576} MB upload limit')
            target.write(chunk)
    if not total:
        raise HTTPException(400, 'Choose a file to print')
    return name


def guest_authorize(request: Request) -> None:
    if not security.guest_enabled(web.DATA_DIR):
        raise HTTPException(403, 'Guest printing is disabled')
    if not security.valid_guest(request.cookies.get(security.GUEST_COOKIE),
                                request.headers.get('x-guest-csrf')):
        raise HTTPException(403, 'Guest session expired. Reopen the guest print page.')


def preview_folder(token: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9_-]{32}',token):
        raise HTTPException(404, 'Preview not found')
    return web.preview_root() / token


def trim_preview_budget(owner: str):
    # Bound the space taken by file previews even if visitors repeatedly upload
    # and never print. Two previews per session, at most 20 globally / 768 MB.
    previews=[]
    for directory in web.preview_root().iterdir():
        if not directory.is_dir():
            continue
        try:
            meta=json.loads((directory/'meta.json').read_text())
            previews.append((float(meta['created']),directory,meta.get('owner'),
                             (directory/'source').stat().st_size))
        except (OSError,ValueError,KeyError,json.JSONDecodeError):
            shutil.rmtree(directory,ignore_errors=True)
    previews.sort(key=lambda x:x[0],reverse=True)
    owned=0;total=0
    for index,(_,directory,who,size) in enumerate(previews):
        if who==owner:owned+=1
        total+=size
        if (who==owner and owned>=3) or index>=19 or total>768*1024*1024:
            shutil.rmtree(directory,ignore_errors=True)


def preview_record(request: Request, token: str, guest: bool) -> tuple[Path, dict]:
    if guest and not security.guest_enabled(web.DATA_DIR):
        raise HTTPException(403, 'Guest printing is disabled')
    directory=preview_folder(token)
    try:
        meta=json.loads((directory/'meta.json').read_text())
        if meta['owner']!=web.preview_owner(request,guest) or meta['guest']!=guest:
            raise HTTPException(404,'Preview not found')
        if time.time()-meta['created']>web.PREVIEW_TTL:
            raise HTTPException(410,'Preview expired. Choose the file again.')
        source=directory/'source'
        if not source.is_file():
            raise HTTPException(404,'Preview not found')
        return source,meta
    except HTTPException:
        raise
    except (OSError,ValueError,KeyError,json.JSONDecodeError):
        raise HTTPException(404, 'Preview not found')


async def inspect_upload(request: Request, guest: bool):
    if guest:
        guest_authorize(request)
    web.clear_old_previews()
    trim_preview_budget(web.preview_owner(request,guest))
    token=secrets.token_urlsafe(24)
    directory=preview_folder(token)
    directory.mkdir(mode=0o700)
    try:
        source=directory/'source'
        name=await spool_upload(request,source,not guest)
        info=validate_print_file(source,name,guest)
        record={'owner':web.preview_owner(request,guest),'guest':guest,
                'name':name,'created':time.time(),**info}
        (directory/'meta.json').write_text(json.dumps(record))
        return {'token':token,'name':name,'expires_in':web.PREVIEW_TTL,**info}
    except Exception:
        shutil.rmtree(directory,ignore_errors=True)
        raise


@router.post('/api/print/inspect')
async def inspect_user_upload(request: Request):
    return await inspect_upload(request,False)


@router.post('/api/guest/inspect')
async def inspect_guest_upload(request: Request):
    return await inspect_upload(request,True)


@router.get('/api/print/preview/{token}/page/{number}')
def preview_user_page(token: str, number: int, request: Request):
    source,record=preview_record(request,token,False)
    try:
        image=printing.render_preview(source,record['kind'],number)
    except printing.PrintFileError as exc:
        raise HTTPException(404,str(exc)) from exc
    return Response(content=image,media_type='image/jpeg',headers={'Cache-Control':'no-store'})


@router.get('/api/guest/preview/{token}/page/{number}')
def preview_guest_page(token: str, number: int, request: Request):
    source,record=preview_record(request,token,True)
    try:
        image=printing.render_preview(source,record['kind'],number)
    except printing.PrintFileError as exc:
        raise HTTPException(404,str(exc)) from exc
    return Response(content=image,media_type='image/jpeg',headers={'Cache-Control':'no-store'})


class PreviewPrintRequest(BaseModel):
    token: str


async def submit_preview(request: Request, body: PreviewPrintRequest, guest: bool, background: bool = False):
    if guest:
        guest_authorize(request)
    source,record=preview_record(request,body.token,guest)
    choices=web.print_choices(request,not guest)
    # Read metadata again if contents were replaced unexpectedly on disk.
    validate_print_file(source,record['name'],guest)
    if guest:
        ip=request.client.host if request.client else 'unknown'
        if not security.guest_rate_allowed(ip,record=True):
            raise HTTPException(429,'Guest print limit reached. Try again later.')
    if not background:
        return await asyncio.to_thread(web.run_print, source, record['kind'], record['name'], choices)
    # The preview may expire or be replaced while the job waits, so the job
    # gets its own copy of the file.
    return web.task_response(tasks.runner.submit(
        'print', 'print', web.task_owner(request, guest),
        lambda task: web.run_print(task.directory / 'source', record['kind'], record['name'], choices, task),
        prepare=lambda folder: shutil.copyfile(source, folder / 'source'), limit=web.job_limit(request, guest)))


@router.post('/api/print/submit-preview')
async def submit_user_preview(request: Request, body: PreviewPrintRequest, background: bool = False):
    return await submit_preview(request,body,False,background)


@router.post('/api/guest/submit-preview')
async def submit_guest_preview(request: Request, body: PreviewPrintRequest, background: bool = False):
    return await submit_preview(request,body,True,background)


@router.get('/api/printers')
def printers_available(request: Request, refresh: bool = False):
    if refresh:
        devices.invalidate('cups')
    cfg, records=web.printer_inventory()
    queues=web.visible_printers(cfg,records,request.state.role)
    return {'default':web.print_default(cfg,records),
            'printers':[{**q,'capabilities':devices.cups_capabilities(q['server'],q['queue'])} for q in queues]}


@router.get('/api/guest/printers')
def printers_for_guests():
    if not security.guest_enabled(web.DATA_DIR):
        return {'default':None,'printers':[]}
    cfg, records=web.printer_inventory()
    queues=web.visible_printers(cfg,records,'guest')
    return {'default':web.print_default(cfg,records),
            'printers':[{**q,'capabilities':devices.cups_capabilities(q['server'],q['queue'])} for q in queues]}


@router.get('/api/print/status')
def print_status(request: Request, queue: str | None = None):
    web._check_cups_config()
    cfg, records=web.printer_inventory()
    permitted=web.visible_printers(cfg,records,request.state.role)
    chosen=devices.resolve_printer(queue or cfg.get('default_printer'),records)
    if chosen and chosen['id'] in {q['id'] for q in permitted}:
        caps=devices.cups_capabilities(chosen['server'],chosen['queue'])
        known=list(caps.get('media_map',{}) or printing.match_media_choices(caps['media']))
        return {'ready':chosen['ready'],'queue':chosen['queue'],'server':chosen['server'],
                'server_label':web.server_label(chosen['server']),
                'message':'CUPS queue is accepting jobs' if chosen['ready'] else chosen['status'],
                'media':known,'capabilities':caps,
                'paper_labels':{k:v[1] for k,v in printing.MEDIA.items()},
                'supplies_low':[x for x in web.quiet_supplies(chosen['server'], chosen['queue']) if x['low']]}
    if not records:
        return {'ready':False,'queue':queue or '', 'message':'No printers found. Check your print servers under Devices.',
                'available_queues':[]}
    return {'ready':False,'queue':queue or '',
            'message':'Choose a default printer under Devices, or select a permitted printer',
            'available_queues':[p['label'] for p in permitted]}


class CancelJobBody(BaseModel):
    server: str = Field(min_length=1, max_length=120)
    job_id: int = Field(ge=1)


@router.post('/api/admin/jobs/cancel')
def cups_admin_cancel_job(body: CancelJobBody, request: Request):
    conn = web.cups_call(cupsadmin.connection, web.DATA_DIR, web.configured_server(body.server), admin=True)
    web.cups_call(cupsadmin.cancel_job, conn, body.job_id, request.state.username)
    stats.mark_cancelled(web.DATA_DIR, body.server, body.job_id)
    return {'cancelled': body.job_id}


@router.get('/api/print/jobs')
def my_print_jobs(request: Request):
    """The signed-in person's recent print jobs on every CUPS server."""
    user = request.state.username

    def server_jobs(server: str) -> list[dict]:
        conn = cupsadmin.connection(web.DATA_DIR, server, timeout=6)
        active = cupsadmin.list_jobs(conn, user=user, which='not-completed', limit=20)
        return active + cupsadmin.list_jobs(conn, user=user, which='completed', limit=10)
    jobs, unavailable = [], []
    servers = web.cups_servers()
    reachable = [s for s in servers if not devices.cups_backoff(s)]
    unavailable += [s for s in servers if s not in reachable]
    if reachable:
        with ThreadPoolExecutor(max_workers=min(4, len(reachable))) as pool:
            futures = {server: pool.submit(server_jobs, server) for server in reachable}
        for server, future in futures.items():
            try:
                jobs += future.result()
            except ipp.IPPError:
                devices.mark_cups_down(server)
                unavailable.append(server)
    jobs.sort(key=lambda job: (not job['active'], -(job['created'] or 0)))
    return {'jobs': jobs[:20], 'unavailable': unavailable}


@router.post('/api/print/jobs/cancel')
def cancel_my_print_job(body: CancelJobBody, request: Request):
    user = request.state.username
    conn = cupsadmin.connection(web.DATA_DIR, web.configured_server(body.server))
    # Folio checks ownership itself before acting, whichever login it uses.
    if web.cups_call(cupsadmin.job_owner, conn, body.job_id, user) != user:
        raise HTTPException(404, 'Print job not found')
    try:
        cupsadmin.cancel_job(conn, body.job_id, user)
    except ipp.AuthRequired:
        if not cupsadmin.admin_status(web.DATA_DIR, body.server)['connected']:
            raise HTTPException(403, 'This CUPS server only lets administrators cancel jobs. '
                                     'Ask an administrator to connect its login in Devices.')
        web.cups_call(cupsadmin.cancel_job, cupsadmin.connection(web.DATA_DIR, body.server, admin=True), body.job_id, user)
    except ipp.IPPError as exc:
        raise HTTPException(502, str(exc)) from exc
    stats.mark_cancelled(web.DATA_DIR, body.server, body.job_id)
    return {'cancelled': body.job_id}


@router.post('/api/guest/print')
async def print_guest_file(request: Request):
    return await receive_print_file(request, authenticated=False)


@router.post('/api/print')
async def print_authenticated_file(request: Request):
    return await receive_print_file(request, authenticated=True)


async def receive_print_file(request: Request, *, authenticated: bool):
    """Legacy direct-upload endpoint retained for existing clients/tests."""
    if not authenticated:
        guest_authorize(request)
    choices=web.print_choices(request,authenticated)
    with tempfile.TemporaryDirectory(prefix='folio-print-') as tmp:
        incoming=Path(tmp)/'source'
        name=await spool_upload(request,incoming,authenticated)
        info=validate_print_file(incoming,name,not authenticated)
        if not authenticated:
            ip=request.client.host if request.client else 'unknown'
            if not security.guest_rate_allowed(ip,record=True):
                raise HTTPException(429,'Guest print limit reached. Try again later.')
        return await asyncio.to_thread(web.run_print, incoming, info['kind'], name, choices)


@router.get('/api/guest/tasks/{task_id}')
def guest_task_status(task_id: str, request: Request):
    task = web.owned_task(task_id, web.task_owner(request, guest=True))
    return task.public(tasks.runner.position(task))
