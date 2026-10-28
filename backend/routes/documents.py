"""Documents on the workbench: scanning, pages, editing, exporting and sending.

Route handlers moved out of app.py in 0.21. Shared state and helpers stay in
app.py and are reached as web.NAME, so tests can still swap them there.
"""
from __future__ import annotations

import io
import hashlib
import math
import asyncio
import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from PIL import Image, ImageOps

from backend import destinations, devices, features, security, tasks
from backend.processing import transform_page, find_deskew_angle, is_blank
from backend import app as web

router = APIRouter()


def new_id(length=12) -> str:
    return uuid4().hex[:length]


def get_page(job: dict, page_id: str) -> dict:
    if not web.PAGE_ID_RE.fullmatch(page_id):
        raise HTTPException(404, 'Page not found')
    return next((p for p in job['pages'] if p['id'] == page_id), None) or _not_found('Page not found')


def _not_found(detail: str):
    raise HTTPException(404, detail)


class NewDocument(BaseModel):
    title: str = Field(default='Untitled document', max_length=100)


class RenameDocument(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ScanRequest(BaseModel):
    # No Samsung-specific hardcoding: additional scanners can advertise other values.
    scanner_id: str | None = Field(default=None, max_length=50)
    source: str = Field(default='ADF', min_length=1, max_length=40)
    mode: str = Field(default='Color', min_length=1, max_length=40)
    resolution: int = Field(default=300, ge=50, le=4800)
    # None = keep scanning until feeder reports empty (backend-dependent).
    count: int | None = Field(default=None, ge=1, le=100)
    role: Literal['normal', 'fronts', 'backs'] = 'normal'
    # Put blank pages straight into the deleted pages (Undo brings them back).
    # Only for regular batches: duplex fronts and backs must stay paired.
    skip_blank: bool = False


class PagePatch(BaseModel):
    rotation: Literal[0, 90, 180, 270] | None = None
    export_mode: Literal['Original', 'Gray'] | None = None
    deskew: float | None = Field(default=None, ge=-12, le=12)
    crop: list[float] | None = None
    reset_crop: bool = False
    scale: Literal[50, 75, 100, 125, 150, 200] | None = None
    brightness: int | None = Field(default=None, ge=-80, le=80)
    contrast: int | None = Field(default=None, ge=-80, le=80)
    auto_levels: bool | None = None


class PageOrder(BaseModel):
    ids: list[str]


class InterleaveRequest(BaseModel):
    front_batch_id: str
    back_batch_id: str
    reverse_backs: bool = True


def ensure_scanner_options(req: ScanRequest, sane_name: str, entry: dict) -> None:
    for value in (req.mode,req.source):
        if not re.fullmatch(r'[\w .+/-]{1,40}', value):
            raise HTTPException(400,'Invalid scanner option')
    # Even with a stale browser tab, reject unsupported hardware settings.
    # Keep the existing known-good scanner fast and backward compatible.
    # New managed scanners are strictly validated from their SANE options.
    caps=(dict(sources=['ADF','Flatbed'],modes=['Color','Gray'],resolutions=list(web.RESOLUTIONS),available=True)
          if entry['kind']=='existing' else devices.scanner_capabilities(sane_name))
    if caps['available']:
        if req.source not in caps['sources'] or req.mode not in caps['modes'] or req.resolution not in caps['resolutions']:
            raise HTTPException(400,'Selected scanning settings are not supported by this scanner')
    elif entry['kind']!='existing':
        raise HTTPException(503, 'Cannot read scanner capabilities; try Test scanner in Device Manager')
    elif req.source not in ('ADF','Flatbed') or req.mode not in ('Color','Gray') or req.resolution not in web.RESOLUTIONS:
        raise HTTPException(400, 'Unsupported option for the existing scanner')


@router.get('/api/scan')
def scan_status(request: Request):
    with web.lock:
        status = dict(web.scan_state)
    if request.state.role != 'admin' and status.get('job_id'):
        try:
            job = web.load_job(status['job_id'])
            if (job.get('owner') or security.initial_admin(web.DATA_DIR)) != request.state.username:
                status['job_id'] = None
                status['message'] = 'Scanner busy' if status['active'] else 'Ready'
                status['error'] = None
        except HTTPException:
            status['job_id'] = None
    return status


@router.get('/api/jobs')
def list_jobs(request: Request):
    result = []
    for path in web.JOBS_DIR.glob('*/job.json'):
        try:
            j = json.loads(path.read_text())
            owner = (j.get('owner') or security.initial_admin(web.DATA_DIR))
            if request.state.role != 'admin' and owner != request.state.username:
                continue
            result.append({'id': j['id'], 'title': j['title'], 'updated': j['updated'],
                           'pages': len(j['pages']), 'owner': owner})
        except (KeyError, json.JSONDecodeError, OSError):
            continue
    result.sort(key=lambda j: j['updated'], reverse=True)
    return result[:60]


@router.post('/api/jobs', status_code=201)
def create_job(body: NewDocument, request: Request):
    web.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = new_id()
    path = web.JOBS_DIR / job_id
    path.mkdir()
    (path / 'pages').mkdir()
    (path / 'thumbs').mkdir()
    job = {'id': job_id, 'title': body.title.strip() or 'Untitled document',
           'owner': request.state.username, 'created': web.now(),
           'updated': web.now(), 'pages': [], 'deleted_pages': [], 'batches': []}
    web.save_job(job)
    return job


@router.get('/api/jobs/{job_id}')
def read_job(job_id: str):
    with web.lock:
        return web.load_job(job_id)


@router.patch('/api/jobs/{job_id}')
def rename_job(job_id: str, body: RenameDocument):
    with web.lock:
        j = web.load_job(job_id)
        j['title'] = body.title.strip()
        web.save_job(j)
        return j


@router.delete('/api/jobs/{job_id}', status_code=200)
def delete_document(job_id: str):
    with web.lock:
        path = web.job_path(job_id)
        if web.scan_state['active'] and web.scan_state['job_id'] == job_id:
            raise HTTPException(409, 'This document is being scanned. Cancel or finish the scan first.')
        # Validated ID, a directory created inside JOBS_DIR by this app.
        # Delete source images, thumbnails, batches, metadata, and edit history.
        shutil.rmtree(path)
        return {'ok': True, 'deleted': job_id}


def execute_scan(job_id: str, req: ScanRequest, sane_device: str) -> None:
    # The scanner lock is acquired by the route before starting the thread.
    try:
        with tempfile.TemporaryDirectory(prefix='scan-studio-') as scratch_dir:
            scratch = Path(scratch_dir)
            batch_id = new_id()
            command = ['scanimage', '-d', sane_device, '--source', req.source,
                       '--mode', req.mode, '--resolution', str(req.resolution), '--format=png']
            if 'ADF' in req.source.upper():
                command.append('--batch=page-%04d.png')
                if req.count:
                    command.extend(['--batch-count', str(req.count)])
            else:
                command.extend(['-o', 'page-0001.png'])

            # When cancellation or a feeder error occurs, preserve every fully
            # written page in the scratch directory before returning status.
            if web.scan_cancel_event.is_set():
                problem = 'Cancelled before scanning started'
            else:
                _, problem = web.run_scanner_process(command, scratch, web.scan_cancel_event)
            cancelled = web.scan_cancel_event.is_set() or (problem is not None and 'timed out' in problem.lower())

            files = sorted(scratch.glob('page-*.png'))
            imported, left_out = [], 0
            skip_blank = req.skip_blank and req.role == 'normal'
            with web.lock:
                job = web.load_job(job_id)
                path = web.job_path(job_id)
                for f in files:
                    page_id = new_id(16)
                    output = path / 'pages' / f'{page_id}.png'
                    try:
                        with Image.open(f) as im:
                            im.load()
                            thumb = ImageOps.contain(im.convert('RGB'), (340, 440))
                            thumb.save(path / 'thumbs' / f'{page_id}.jpg', 'JPEG', quality=78)
                            blank = skip_blank and is_blank(im)
                        shutil.copyfile(f, output)
                    except (OSError, ValueError):
                        continue
                    page = {'id': page_id, 'file': f'{page_id}.png', 'rotation': 0,
                            'export_mode': 'Original', 'crop': None, 'scale': 100, 'deskew': 0.0, 'scanned_mode': req.mode,
                            'dpi': req.resolution, 'batch_id': batch_id, 'created': web.now()}
                    imported.append(page_id)
                    if blank:  # Kept, but as a deleted page that Undo restores to its place
                        job['deleted_pages'].append({'page': page, 'index': len(job['pages'])})
                        left_out += 1
                        continue
                    job['pages'].append(page)
                if imported:
                    job['batches'].append({'id': batch_id, 'created': web.now(),
                                           'label': f"{req.source} · {req.mode} · {len(imported)} page(s)",
                                           'role': req.role, 'pages': imported, 'mode': req.mode,
                                           'source': req.source, 'resolution': req.resolution,
                                           'scanner_id':req.scanner_id or web.registry()['default_scanner']})
                    web.save_job(job)

                if problem and not cancelled:
                    # The device may have gone away; re-probe instead of trusting the cache.
                    devices.invalidate('sane')
                added = len(imported) - left_out
                message = f'Added {added} page(s)' if imported else 'No scanned pages received'
                if left_out:
                    message += f" · {left_out} blank {'page' if left_out == 1 else 'pages'} left out (Undo brings them back)"
                if problem:
                    message += f' · {problem}' if cancelled else ' · Feeder stopped; completed pages were kept'
                web.scan_state.update(active=False, message=message, pages_added=len(imported),
                                  cancelled=cancelled, cancel_requested=False,
                                  error=None if imported else (problem or message),
                                  updated=time.time())
    except Exception as exc:
        with web.lock:
            web.scan_state.update(active=False, message='Scan failed', error=str(exc),
                              cancel_requested=False, updated=time.time())
    finally:
        web.scan_cancel_event.clear()
        web.scanner_lock.release()


@router.post('/api/scan/cancel', status_code=202)
def cancel_scan(request: Request):
    with web.lock:
        if not web.scan_state['active']:
            raise HTTPException(409, 'No scan is currently running')
        if request.state.role != 'admin':
            active = web.load_job(web.scan_state['job_id'])
            if (active.get('owner') or security.initial_admin(web.DATA_DIR)) != request.state.username:
                raise HTTPException(403, 'Only the owner can cancel this scan')
        web.scan_cancel_event.set()
        web.scan_state.update(cancel_requested=True, message='Cancellation requested…',
                          updated=time.time())
        return dict(web.scan_state)


@router.post('/api/jobs/{job_id}/scan', status_code=202)
def start_scan(job_id: str, request: ScanRequest):
    web.job_path(job_id)
    entry, sane_device = web.get_selected_scanner(request.scanner_id)
    ensure_scanner_options(request,sane_device,entry)
    if not web.scanner_lock.acquire(blocking=False):
        raise HTTPException(409, 'Another scan is already running')
    web.scan_cancel_event.clear()
    with web.lock:
        web.scan_state.update(active=True, message='Scanning…', pages_added=0,
                          job_id=job_id, scanner_id=entry['id'], error=None, cancelled=False,
                          cancel_requested=False, updated=time.time())
    try:
        thread = threading.Thread(target=execute_scan, args=(job_id, request, sane_device), daemon=True)
        thread.start()
    except Exception:
        web.scanner_lock.release()
        raise
    return dict(web.scan_state)


@router.post('/api/jobs/{job_id}/reorder')
def reorder_pages(job_id: str, request: PageOrder):
    with web.lock:
        j = web.load_job(job_id)
        ids = [p['id'] for p in j['pages']]
        if len(ids) != len(request.ids) or set(ids) != set(request.ids):
            raise HTTPException(400, 'Page IDs must exactly match current document')
        by_id = {p['id']: p for p in j['pages']}
        j['pages'] = [by_id[i] for i in request.ids]
        web.save_job(j)
        return j


@router.patch('/api/jobs/{job_id}/pages/{page_id}')
def edit_page(job_id: str, page_id: str, request: PagePatch):
    with web.lock:
        j = web.load_job(job_id)
        p = get_page(j, page_id)
        if request.rotation is not None:
            # The crop is stored in coordinates of the right-angle-rotated
            # original, not the deskewed preview. Rotate that rectangle when
            # the user rotates an already cropped page so the SAME source
            # pixels remain selected.
            delta = (request.rotation - int(p.get('rotation', 0))) % 360
            if p.get('crop') and request.crop is None and not request.reset_crop and delta:
                x, y, w, h = p['crop']
                if delta == 90:
                    adjusted = [1 - y - h, x, h, w]
                elif delta == 180:
                    adjusted = [1 - x - w, 1 - y - h, w, h]
                elif delta == 270:
                    adjusted = [y, 1 - x - w, h, w]
                else:
                    adjusted = [x, y, w, h]
                p['crop'] = [round(max(0.0, min(1.0, v)), 5) for v in adjusted]
            p['rotation'] = request.rotation
        if request.export_mode is not None:
            p['export_mode'] = request.export_mode
        if request.deskew is not None:
            p['deskew'] = round(request.deskew, 2)
        if request.scale is not None:
            p['scale'] = request.scale
        if request.brightness is not None:
            p['brightness'] = request.brightness
        if request.contrast is not None:
            p['contrast'] = request.contrast
        if request.auto_levels is not None:
            p['auto_levels'] = request.auto_levels
        if request.reset_crop:
            p['crop'] = None
        elif request.crop is not None:
            if len(request.crop) != 4 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in request.crop):
                raise HTTPException(400, 'Crop must be normalized x, y, width, height')
            x,y,w,h = request.crop
            if w < .03 or h < .03 or x+w > 1.000001 or y+h > 1.000001:
                raise HTTPException(400, 'Crop rectangle is outside the page or too small')
            p['crop'] = [round(v, 5) for v in request.crop]
        p['edited_at'] = web.now()
        web.save_job(j)
        return j


@router.post('/api/jobs/{job_id}/pages/{page_id}/restore-original')
def restore_original_scan(job_id: str, page_id: str):
    """Discard all page editing metadata and return to immutable scan pixels.

    The original image remains saved in pages/<filename> throughout editing.
    This operation does not rescan, delete, or change that source file.
    """
    with web.lock:
        job = web.load_job(job_id)
        page = get_page(job, page_id)
        page.update(web.ORIGINAL_PAGE_EDIT_VALUES)
        page['edited_at'] = web.now()
        web.save_job(job)
        return job


@router.post('/api/jobs/{job_id}/pages/{page_id}/auto-straighten')
def auto_straighten(job_id: str, page_id: str):
    with web.lock:
        j = web.load_job(job_id)
        p = get_page(j, page_id)
        # Estimate against the CURRENTLY SELECTED CONTENT, before fine deskew.
        # Pixels cut away by cropping must not influence auto-straighten.
        base = web.load_page_image(job_id, {**p, 'deskew': 0, 'scale': 100,
                                        'brightness': 0, 'contrast': 0,
                                        'auto_levels': False, 'export_mode': 'Original'})
        angle, evidence = find_deskew_angle(base)
        # An estimate is NEVER applied automatically. Client previews the
        # suggested correction and requires explicit approval.
        return {'angle': angle, 'line_count': evidence, 'page_id': page_id,
                'message': 'No reliable skew found; page left unchanged' if not angle else f'Suggested {angle:+.1f}° correction (not yet applied)'}


@router.delete('/api/jobs/{job_id}/pages/{page_id}')
def delete_page(job_id: str, page_id: str):
    with web.lock:
        j = web.load_job(job_id)
        p = get_page(j, page_id)
        j['deleted_pages'].append({'page': p, 'index': j['pages'].index(p)})
        j['pages'] = [x for x in j['pages'] if x['id'] != page_id]
        web.save_job(j)
        return j


@router.post('/api/jobs/{job_id}/remove-blank-pages')
def remove_blank_pages(job_id: str):
    """Move pages with nothing written on them to the deleted pages, last one
    first, so Undo restores each to its place."""
    with web.lock:
        j = web.load_job(job_id)
        pages = list(j['pages'])
    blank = set()
    for page in pages:
        try:
            proxy = page_proxy(job_id, page)  # Built before taking a render slot, like previews
            with web.RENDER_SLOTS, Image.open(proxy) as image:
                if is_blank(image):
                    blank.add(page['id'])
        except (OSError, ValueError):
            continue  # An unreadable page is never treated as blank
    with web.lock:
        j = web.load_job(job_id)
        removed = []
        for index in range(len(j['pages']) - 1, -1, -1):
            page = j['pages'][index]
            if page['id'] in blank:
                j['deleted_pages'].append({'page': page, 'index': index})
                removed.append(page['id'])
        j['pages'] = [p for p in j['pages'] if p['id'] not in blank]
        if removed:
            web.save_job(j)
        return {'removed': removed[::-1], 'job': j}


@router.post('/api/jobs/{job_id}/undo-delete')
def undo_delete(job_id: str):
    with web.lock:
        j = web.load_job(job_id)
        if not j['deleted_pages']:
            raise HTTPException(400, 'Nothing to restore')
        last = j['deleted_pages'].pop()
        j['pages'].insert(min(len(j['pages']), last['index']), last['page'])
        web.save_job(j)
        return j


@router.post('/api/jobs/{job_id}/pages/{page_id}/duplicate')
def duplicate_page(job_id: str, page_id: str):
    with web.lock:
        j = web.load_job(job_id)
        p = get_page(j, page_id)
        clone = {**p, 'id': new_id(16), 'created': web.now()}
        # File and thumbnail point to same immutable original.
        j['pages'].insert(j['pages'].index(p) + 1, clone)
        web.save_job(j)
        return j


@router.post('/api/jobs/{job_id}/interleave')
def interleave(job_id: str, request: InterleaveRequest):
    with web.lock:
        j = web.load_job(job_id)
        front = next((b for b in j['batches'] if b['id'] == request.front_batch_id), None)
        back = next((b for b in j['batches'] if b['id'] == request.back_batch_id), None)
        if not front or not back or front['id'] == back['id']:
            raise HTTPException(400, 'Select two different existing batches')
        pages = j['pages']
        current_ids = {p['id'] for p in pages}
        fronts = [p for p in front['pages'] if p in current_ids]
        backs = [p for p in back['pages'] if p in current_ids]
        if not fronts or len(fronts) != len(backs):
            raise HTTPException(400, 'Front and back batches must contain the same number of active pages')
        if request.reverse_backs:
            backs.reverse()
        mixed = [i for pair in zip(fronts, backs) for i in pair]
        ids = [p['id'] for p in pages]
        insertion_pos = min(ids.index(i) for i in mixed)
        remainder = [i for i in ids if i not in set(mixed)]
        insertion_pos = sum(1 for i in ids[:insertion_pos] if i in remainder)
        reordered = remainder[:insertion_pos] + mixed + remainder[insertion_pos:]
        by_id = {p['id']: p for p in pages}
        j['pages'] = [by_id[i] for i in reordered]
        web.save_job(j)
        return j


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f'.{path.name}.{uuid4().hex[:8]}.tmp')
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def page_proxy(job_id: str, page: dict) -> Path:
    """A reduced JPEG copy of the immutable scan, used only for on-screen previews.

    Decoding a 300 DPI PNG for every thumbnail and every slider change is the
    most expensive thing Folio does on a small host. The proxy is built once
    per scanned file; exports and printing always use the original pixels.
    """
    cache = web.job_path(job_id) / 'cache'
    proxy = cache / f"{Path(page['file']).stem}.proxy.jpg"
    if not proxy.is_file():
        original = web.load_page_image(job_id, page, apply_edit=False)
        # Resizing a page that is only slightly larger than the proxy costs more
        # than it saves (A4 at 300 DPI is 2480x3508); keep its pixels and let
        # each preview size downscale from the JPEG instead.
        if original.width > web.PROXY_SIZE[0] * 1.25 or original.height > web.PROXY_SIZE[1] * 1.25:
            original.thumbnail(web.PROXY_SIZE, Image.Resampling.LANCZOS)
        if original.mode not in ('RGB', 'L'):
            original = original.convert('L' if original.mode in ('1', 'LA', 'I', 'I;16') else 'RGB')
        buffer = io.BytesIO()
        original.save(buffer, 'JPEG', quality=90)
        cache.mkdir(exist_ok=True)
        write_atomic(proxy, buffer.getvalue())
    return proxy


def preview_variant(thumb: bool, large: bool, uncropped: bool) -> str:
    return ('thumb' if thumb else 'large' if large else 'normal') + ('-uncropped' if uncropped else '')


def render_preview_jpeg(job_id: str, page: dict, variant: str) -> bytes:
    """Render (or reuse) the preview JPEG for one page state and size."""
    size_name, _, crop_mode = variant.partition('-')
    before, after = web.PREVIEW_SIZES[size_name]
    # Preview never renders at full-resolution plus 200% scaling. That can
    # consume hundreds of MB of RAM and cause the browser image to vanish.
    # Scale is an export pixel-density setting, not a visual zoom control.
    preview_page = {**page, 'scale': 100}
    if crop_mode == 'uncropped':
        # The crop editor displays the right-angle-oriented ORIGINAL, without
        # fine deskew/tone adjustments, so normalized crop edges remain stable.
        preview_page.update(crop=None, deskew=0.0, brightness=0, contrast=0,
                            auto_levels=False, export_mode='Original')
    # The key covers every input that changes pixels, so an edit (or undoing it)
    # selects a different cached file, and duplicates of a page share one.
    state = {k: preview_page.get(k) for k in ('file', 'rotation', 'crop', 'deskew', 'brightness',
                                               'contrast', 'auto_levels', 'export_mode')}
    key = hashlib.sha256(json.dumps([web.RENDER_VERSION, variant, state], sort_keys=True)
                         .encode()).hexdigest()[:16]
    cache = web.job_path(job_id) / 'cache'
    stem = Path(page['file']).stem
    target = cache / f'{stem}.{variant}.{key}.jpg'
    try:
        return target.read_bytes()
    except FileNotFoundError:
        pass
    proxy = page_proxy(job_id, page)  # Takes its own render slot when building
    with web.RENDER_SLOTS:
        with Image.open(proxy) as source:
            source.draft(source.mode, before)  # Fast JPEG DCT downscale when possible
            image = source.copy()
        image.thumbnail(before, Image.Resampling.LANCZOS)
        # The normal preview and exported PDF use the same transformation order.
        image = transform_page(image, preview_page)
        image.thumbnail(after, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.convert('RGB').save(output, 'JPEG', quality=82)
    data = output.getvalue()
    # Keep only the newest render per page variant; older edit states are
    # cheap to regenerate from the proxy if the user returns to them.
    for stale in cache.glob(f'{stem}.{variant}.*.jpg'):
        stale.unlink(missing_ok=True)
    write_atomic(target, data)
    return data


@router.get('/api/jobs/{job_id}/pages/{page_id}/image')
def page_image(job_id: str, page_id: str, thumb: bool = False, uncropped: bool = False, large: bool = False):
    with web.lock:
        j = web.load_job(job_id)
        # Allow media of a recently removed page until browser refresh.
        p = next((p for p in j['pages'] if p['id'] == page_id), None)
        if p is None:
            raise HTTPException(404, 'Page not found')
    data = render_preview_jpeg(job_id, p, preview_variant(thumb, large, uncropped))
    # Scans are private documents: keep them out of shared browser caches.
    return Response(content=data, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


def export_filename(j: dict, ocr: bool) -> str:
    safe_name = re.sub(r'[^a-zA-Z0-9._ -]', '', j['title']).strip() or 'Scanned-document'
    return safe_name[:80] + ('-searchable' if ocr else '') + '.pdf'


@router.get('/api/jobs/{job_id}/export')
def export_pdf(job_id: str, quality: Literal['small', 'balanced', 'best'] = 'balanced',
               ocr: bool = False):
    """Direct download, kept for scripts. The web app uses background exports."""
    if ocr:
        features.require('ocr')
    with web.lock:
        j = web.load_job(job_id)
    if not j['pages']:
        raise HTTPException(400, 'There are no pages to export')
    pdf_bytes = web.make_job_pdf(job_id, j, quality, ocr)
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type='application/pdf',
                             headers={'Content-Disposition': f'attachment; filename="{export_filename(j, ocr)}"',
                                      'Cache-Control': 'no-store'})


class ExportRequest(BaseModel):
    quality: Literal['small', 'balanced', 'best'] = 'balanced'
    ocr: bool = False


@router.post('/api/jobs/{job_id}/exports', status_code=202)
def start_export(job_id: str, body: ExportRequest, request: Request):
    """Build a PDF in the background; poll /api/tasks/{id}, then download its file."""
    if body.ocr:
        features.require('ocr')
    with web.lock:
        j = web.load_job(job_id)  # Snapshot: later edits don't change this export
    if not j['pages']:
        raise HTTPException(400, 'There are no pages to export')

    def work(task: tasks.Task) -> dict:
        target = task.directory / 'export.pdf'
        web.build_job_pdf(job_id, j, target, body.quality, body.ocr, task)
        task.file, task.filename = target, export_filename(j, body.ocr)
        return {'pages': len(j['pages']), 'bytes': target.stat().st_size}
    return web.task_response(tasks.runner.submit('export', 'export', web.task_owner(request), work, limit=web.job_limit(request)))


class SendBody(BaseModel):
    destination: str = Field(pattern=r'^[a-f0-9]{12}$')
    title: str | None = Field(default=None, max_length=128)
    ocr: bool = False


@router.get('/api/destinations')
def list_destinations():
    """Where documents can be sent, for the workbench (no settings, no tokens)."""
    return [{key: d[key] for key in ('id', 'name', 'kind')} for d in web.configured_destinations()]


@router.post('/api/jobs/{job_id}/send', status_code=202)
def send_document(job_id: str, body: SendBody, request: Request):
    """Build the PDF and deliver it to a destination, in the background."""
    destination = next((d for d in web.configured_destinations() if d['id'] == body.destination), None)
    if not destination:
        raise HTTPException(404, 'That destination no longer exists')
    if body.ocr:
        features.require('ocr')
    with web.lock:
        j = web.load_job(job_id)  # Snapshot: later edits don't change what is sent
    if not j['pages']:
        raise HTTPException(400, 'There are no pages to send')
    title = (body.title or j['title']).strip() or 'Folio document'

    def work(task: tasks.Task) -> dict:
        document = task.directory / 'document.pdf'
        web.build_job_pdf(job_id, j, document, 'balanced', body.ocr, task, progress=(0.0, .8))
        task.update(f"Sending to {destination['name']}…", .85)
        try:
            if destination['kind'] == 'paperless':
                destinations.send_paperless(destination, document, title, destination.get('tags', []))
                detail = 'Paperless is reading and filing it now'
            else:
                detail = f"Saved as {destinations.send_folder(destination, document, title).name}"
        except destinations.DestinationError as exc:
            raise HTTPException(502, str(exc)) from exc  # Shown to the person sending
        with web.lock:
            current = web.load_job(job_id)
            current.setdefault('sent', []).append({'destination': destination['name'], 'at': web.now()})
            web.save_job(current)
        return {'destination': destination['name'], 'detail': detail}
    return web.task_response(tasks.runner.submit('send', 'export', web.task_owner(request), work, limit=web.job_limit(request)))


@router.post('/api/jobs/{job_id}/print')
async def print_folio_document(job_id: str, request: Request, background: bool = False):
    """Print an existing owned document without uploading it back from the browser."""
    choices = web.print_choices(request, True)
    with web.lock:
        j = web.load_job(job_id)
    if not j['pages']:
        raise HTTPException(400, 'Scan at least one page before printing')
    if not background:
        pdf_bytes = await asyncio.to_thread(web.make_job_pdf, job_id, j, 'balanced', False)
        with tempfile.TemporaryDirectory(prefix='folio-print-document-') as tmp:
            filepath = Path(tmp) / 'document.pdf'
            filepath.write_bytes(pdf_bytes)
            return await asyncio.to_thread(web.run_print, filepath, 'pdf', j['title'], choices, origin='document')

    def work(task: tasks.Task) -> dict:
        document = task.directory / 'document.pdf'
        web.build_job_pdf(job_id, j, document, 'balanced', False, task, progress=(0.0, .6))
        return web.run_print(document, 'pdf', j['title'], choices, task, progress=(.6, 1.0), origin='document')
    return web.task_response(tasks.runner.submit('print', 'print', web.task_owner(request), work, limit=web.job_limit(request)))
