"""Folio Scan Studio: secure scanning, non-destructive edits, and optional guest printing."""
from __future__ import annotations

import io
import csv
import hashlib
import math
import asyncio
import hmac
import json
import logging
import os
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from urllib.parse import unquote
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageOps
from backend import security
from backend.processing import transform_page, find_deskew_angle
from backend import printing, devices, diagnostics, tasks, ipp, cupsadmin, cupsimport, features, stats, printserver, airprint
from backend.version import __version__

log = logging.getLogger('folio')

DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
JOBS_DIR = DATA_DIR / 'jobs'
WEB_DIR = Path(__file__).resolve().parent.parent / 'frontend'
DEVICE = os.environ.get('SCANNER_DEVICE', '').strip()  # Blank for a Folio-managed clean installation
RESOLUTIONS = (75, 100, 150, 200, 300)
JOB_ID_RE = re.compile(r'^[a-f0-9]{12}$')
PAGE_ID_RE = re.compile(r'^[a-f0-9]{16}$')
TASK_ID_RE = re.compile(r'^[a-f0-9]{32}$')
SUPPLIES_TTL = 300  # Toner levels change slowly; ask the printer at most every 5 minutes
lock = threading.RLock()
scanner_lock = threading.Lock()
ocr_lock = threading.Lock()
scan_cancel_event = threading.Event()
# Default: 15-minute maximum scan; ask SANE to cancel and allow 30 seconds
# for the WSD backend to release the device before a last-resort hard kill.
SCAN_TIMEOUT_SECONDS = max(30.0, float(os.environ.get('SCAN_TIMEOUT_SECONDS', '900')))
SCAN_GRACE_SECONDS = max(2.0, float(os.environ.get('SCAN_GRACE_SECONDS', '30')))
scan_state = {"active": False, "message": "Ready", "job_id": None, "pages_added": 0,
              "error": None, "cancel_requested": False, "cancelled": False, "updated": 0}
# Full-resolution decodes of 300 DPI scans take ~25-100 MB each. Bound how
# many run at once so a page strip of 40 thumbnails can't exhaust a small host.
RENDER_SLOTS = threading.BoundedSemaphore(max(1, min(8, int(
    os.environ.get('FOLIO_RENDER_CONCURRENCY', '').strip() or min(2, os.cpu_count() or 1)))))
# Bump when preview rendering changes so stale cached JPEGs are not served.
RENDER_VERSION = 1
PREVIEW_SIZES = {  # (downscale before edits, final bound after edits)
    'thumb': ((520, 700), (520, 700)),
    'normal': ((1150, 1500), (1250, 1650)),
    'large': ((2250, 3000), (2350, 3200)),
}
PROXY_SIZE = PREVIEW_SIZES['large'][0]

COOKIE_SECURE = os.environ.get('COOKIE_SECURE', 'false').lower() in ('true','1','yes')
# Multiple CUPS servers, scoped by host+queue.  Legacy env vars are optional
# migration inputs only; new installations select their default in Devices.
# There is deliberately no default server: Folio must not assume any network.
CUPS_SERVERS = devices.parse_cups_servers(os.environ.get(
    'CUPS_SERVERS', os.environ.get('CUPS_SERVER', '')))
CUPS_SERVER = CUPS_SERVERS[0] if CUPS_SERVERS else ''
CUPS_QUEUE = os.environ.get('CUPS_QUEUE', '').strip()
GUEST_MAX_BYTES = 10 * 1024 * 1024
GUEST_MAX_PAGES = 20
# The signed-in upload guard is a memory/disk safety ceiling, NOT the guest quota.
# Authenticated accounts have no 10 MB or 20-page guest restriction.
USER_PRINT_MAX_BYTES = max(64, min(2048, int(os.environ.get('USER_PRINT_MAX_MB', '256')))) * 1024 * 1024
PRINT_MEDIA = {key: info[0] for key,info in printing.MEDIA.items()}
PREVIEW_ROOT: Path | None = None  # Override; defaults to DATA_DIR/print-previews
PREVIEW_TTL = 15 * 60
PRINT_SIDES = {'one-sided', 'two-sided-long-edge', 'two-sided-short-edge'}


def preview_root() -> Path:
    # Resolved on use, so DATA_DIR changes (tests, future config) apply here too.
    return PREVIEW_ROOT or DATA_DIR / 'print-previews'


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_directories()
    stop_background = threading.Event()
    threading.Thread(target=background_upkeep, args=(stop_background,), daemon=True, name='folio-upkeep').start()
    try:
        yield
    finally:
        stop_background.set()
        await asyncio.to_thread(gracefully_stop_on_server_shutdown)


def background_upkeep(stop: threading.Event, first: float = 20, interval: float = 600) -> None:
    """Every few minutes: keep the built-in printers announced on the network,
    and count prints sent straight to CUPS (when that setting is on) before
    CUPS forgets them. The first run waits for the print server to start."""
    wait = first
    while not stop.wait(wait):
        wait = interval
        announce_printers()
        try:
            cupsimport.import_jobs(DATA_DIR, cups_servers())
        except Exception:  # noqa: BLE001 - background upkeep must never take Folio down
            log.warning('Counting prints sent straight to CUPS failed', exc_info=True)


def announce_printers() -> list[str]:
    """Announce the built-in server's shared printers (AirPrint), if the host's
    Avahi folder is mounted; returns the printers announced."""
    try:
        conn = cupsadmin.connection(DATA_DIR, printserver.SERVER, timeout=10) if printserver.enabled() else None
        return airprint.sync(conn)
    except (ipp.IPPError, OSError):
        log.warning('Announcing printers on the network failed', exc_info=True)
        return []


def announce_soon() -> None:
    threading.Thread(target=announce_printers, daemon=True, name='folio-announce').start()


app = FastAPI(title='Folio Scan Studio', version=__version__, docs_url=None, redoc_url=None,
              lifespan=lifespan)
app.mount('/assets', StaticFiles(directory=str(WEB_DIR)), name='assets')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(length=12) -> str:
    return uuid4().hex[:length]


def job_path(job_id: str) -> Path:
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(404, 'Document not found')
    path = JOBS_DIR / job_id
    if not (path / 'job.json').exists():
        raise HTTPException(404, 'Document not found')
    return path


def load_job(job_id: str) -> dict:
    return json.loads((job_path(job_id) / 'job.json').read_text())


def save_job(job: dict) -> None:
    job['updated'] = now()
    # Allow first write when a new document directory has no job.json yet.
    if not JOB_ID_RE.fullmatch(job['id']):
        raise ValueError('Invalid document ID')
    path = JOBS_DIR / job['id'] / 'job.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(job, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def get_page(job: dict, page_id: str) -> dict:
    if not PAGE_ID_RE.fullmatch(page_id):
        raise HTTPException(404, 'Page not found')
    return next((p for p in job['pages'] if p['id'] == page_id), None) or _not_found('Page not found')


def _not_found(detail: str):
    raise HTTPException(404, detail)


def available_device() -> bool:
    try:
        output = subprocess.run(['scanimage', '-L'], capture_output=True, text=True, timeout=15)
        return output.returncode == 0 and DEVICE in output.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


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


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=256)


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


class GuestSettings(BaseModel):
    enabled: bool


class UserCreate(BaseModel):
    username: str
    password: str
    role: Literal['admin','member','printer'] = 'member'


class UserUpdate(BaseModel):
    role: Literal['admin','member','printer'] | None = None
    enabled: bool | None = None
    password: str | None = None


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


def managed_sane_path() -> Path:
    return Path(os.environ.get('MANAGED_SANE_FILE') or DATA_DIR / 'managed-airscan.conf')


def registry() -> dict:
    return devices.get_config(DATA_DIR, DEVICE, CUPS_QUEUE)


def persist_registry(state: dict) -> None:
    devices.save_config(DATA_DIR, state)
    devices.write_managed_sane_config(state, managed_sane_path())
    # SANE sees new or changed endpoints on its next query; don't serve old answers.
    devices.invalidate('sane')


def cups_servers(config: dict | None = None) -> list[str]:
    """Print servers in use: the built-in one, those in CUPS_SERVERS, then any added in Devices."""
    added = (config if config is not None else registry()).get('cups_servers', [])
    builtin = [printserver.SERVER] if printserver.enabled() else []
    return list(dict.fromkeys([*builtin, *CUPS_SERVERS, *added]))


def server_source(server: str) -> str:
    if cupsadmin.is_builtin(server):
        return 'builtin'
    # Servers from CUPS_SERVERS come back on every restart, so only the ones
    # added in Devices can be removed there.
    return 'settings' if server in CUPS_SERVERS else 'folio'


def server_label(server: str) -> str:
    return 'Built-in' if cupsadmin.is_builtin(server) else server


def printer_inventory() -> tuple[dict, list[dict]]:
    state=registry()
    records=devices.all_cups_queues(cups_servers())
    if devices.migrate_legacy_printers(state,records,CUPS_QUEUE):
        persist_registry(state)
    for record in records:
        record['server_label'] = server_label(record['server'])
        if cupsadmin.is_builtin(record['server']):
            record['label'] = record['queue']  # "Office", not "Office (localhost:631)"
    return state,records


def print_default(config: dict, records: list[dict]) -> str | None:
    resolved=devices.resolve_printer(config.get('default_printer'), records)
    return resolved['id'] if resolved else None


def visible_printers(config: dict, records: list[dict], role: str) -> list[dict]:
    return devices.visible_queues(config,records,role,CUPS_QUEUE)


def get_selected_scanner(identifier: str | None) -> tuple[dict, str]:
    """Resolve selected stable device id to current SANE enumeration."""
    cfg = registry()
    identifier = identifier or cfg.get('default_scanner')
    entry = next((e for e in cfg['scanners'] if e['id']==identifier and e.get('enabled',True)),None)
    if entry is None:
        raise HTTPException(404, 'No scanner configured. Go to Devices and add a network scanner.')
    # Legacy scanner continues working even if SANE discovery changes its w0 index.
    if entry['kind']=='existing':
        found=devices.match_scanner(entry,devices.listed_sane_devices())
        return entry,found['sane_name'] if found else entry['hint']
    found=devices.match_scanner(entry, devices.listed_sane_devices())
    if not found:
        raise HTTPException(503, 'Selected scanner is not responding. Check its IP and WSD/eSCL endpoint.')
    return entry, found['sane_name']


def ensure_scanner_options(req: ScanRequest, sane_name: str, entry: dict) -> None:
    for value in (req.mode,req.source):
        if not re.fullmatch(r'[\w .+/-]{1,40}', value):
            raise HTTPException(400,'Invalid scanner option')
    # Even with a stale browser tab, reject unsupported hardware settings.
    # Keep the existing known-good scanner fast and backward compatible.
    # New managed scanners are strictly validated from their SANE options.
    caps=(dict(sources=['ADF','Flatbed'],modes=['Color','Gray'],resolutions=list(RESOLUTIONS),available=True)
          if entry['kind']=='existing' else devices.scanner_capabilities(sane_name))
    if caps['available']:
        if req.source not in caps['sources'] or req.mode not in caps['modes'] or req.resolution not in caps['resolutions']:
            raise HTTPException(400,'Selected scanning settings are not supported by this scanner')
    elif entry['kind']!='existing':
        raise HTTPException(503, 'Cannot read scanner capabilities; try Test scanner in Device Manager')
    elif req.source not in ('ADF','Flatbed') or req.mode not in ('Color','Gray') or req.resolution not in RESOLUTIONS:
        raise HTTPException(400, 'Unsupported option for the existing scanner')


def choose_print_queue(request: Request, authenticated: bool) -> str:
    cfg, records=printer_inventory()
    role=request.state.role if authenticated else 'guest'
    visible=visible_printers(cfg,records,role)
    requested=request.headers.get('x-print-queue','').strip()
    if requested:
        try:
            devices.validate_printer_reference(requested)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc
    target=devices.resolve_printer(requested or cfg.get('default_printer'),records)
    if not target and not requested and not cfg.get('default_printer') and len(visible)==1:
        target=visible[0]
    if not target:
        if requested:
            raise HTTPException(404,'Unknown printer or ambiguous queue name. Select a printer from Print Desk.')
        raise HTTPException(400,'Select a printer, or set a default in Devices.')
    if target['id'] not in {p['id'] for p in visible}:
        raise HTTPException(403,'Printing to this queue is not permitted')
    return target['id']


@app.middleware('http')
async def guard_private_api(request: Request, call_next):
    path = request.url.path
    # No account or documents are revealed on public endpoints.
    public = ('/api/health', '/api/features', '/api/auth/login', '/api/auth/status', '/api/guest/status', '/api/guest/print', '/api/guest/inspect', '/api/guest/printers')
    if request.method in ('POST','PUT','PATCH','DELETE'):
        # Fail closed for cross-site browser requests, including login and guest print.
        if request.headers.get('sec-fetch-site', '').lower() == 'cross-site':
            return JSONResponse({'detail':'Cross-site request rejected'}, status_code=403)
    if (path.startswith('/api/') and path not in public and not path.startswith(('/api/guest/preview/', '/api/guest/tasks/'))
            and path != '/api/guest/submit-preview'):
        session = security.session(DATA_DIR, request.cookies.get(security.COOKIE_NAME))
        if not session:
            return JSONResponse({'detail':'Please sign in'}, status_code=401)
        if request.method not in ('GET','HEAD','OPTIONS'):
            if not hmac.compare_digest(request.headers.get('x-csrf-token',''), session['csrf']):
                return JSONResponse({'detail':'CSRF token missing or invalid'}, status_code=403)
        request.state.username = session['username']
        request.state.role = session['role']
        if path.startswith('/api/admin/') and session['role'] != 'admin':
            return JSONResponse({'detail':'Administrator privileges required'}, status_code=403)
        # Print-only accounts have access only to account management for
        # themselves and the authenticated printing API, never scan files.
        if session['role'] == 'printer' and not (path.startswith('/api/auth/') or (path in ('/api/print','/api/print/status','/api/print/inspect','/api/print/submit-preview','/api/printers') or path.startswith(('/api/print/preview/', '/api/print/jobs', '/api/tasks/')))):
            return JSONResponse({'detail':'Printing-only account'}, status_code=403)
        # Every job endpoint (including thumbnails, exports and mutations)
        # is protected: members access only documents they created.
        if path.startswith('/api/jobs/') and session['role'] != 'admin':
            job_id = path.split('/')[3]
            if not JOB_ID_RE.fullmatch(job_id):
                return JSONResponse({'detail':'Document not found'}, status_code=404)
            try:
                job = json.loads((JOBS_DIR / job_id / 'job.json').read_text())
            except (OSError, ValueError, json.JSONDecodeError):
                return JSONResponse({'detail':'Document not found'}, status_code=404)
            if (job.get('owner') or security.initial_admin(DATA_DIR)) != session['username']:
                return JSONResponse({'detail':'Document not found'}, status_code=404)
    response = await call_next(request)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault('Content-Security-Policy',
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
    if path.startswith('/api/'):
        response.headers.setdefault('Cache-Control', 'no-store')
    elif path.startswith('/assets/'):
        # A fingerprinted URL never changes content, so it can be kept; any
        # other asset request must be checked with the server first.
        response.headers.setdefault('Cache-Control', 'public, max-age=31536000, immutable'
                                    if request.query_params.get('v') else 'no-cache')
    return response


@app.get('/api/auth/status')
def auth_status(request: Request):
    session = security.session(DATA_DIR, request.cookies.get(security.COOKIE_NAME))
    return {'authenticated':bool(session), 'username':session['username'] if session else None,
            'role':session['role'] if session else None,
            'csrf':session['csrf'] if session else None,
            'guest_print':security.guest_enabled(DATA_DIR)}


@app.post('/api/auth/login')
def login(body: LoginBody, request: Request, response: Response):
    record, error = security.try_login(DATA_DIR, body.username, body.password,
                                        request.client.host if request.client else 'unknown')
    if error:
        raise HTTPException(429 if error.startswith('Too many') else 401, error)
    response.set_cookie(security.COOKIE_NAME, record['token'], httponly=True,
                        secure=COOKIE_SECURE, samesite='strict', max_age=security.SESSION_SECONDS, path='/')
    return {'authenticated':True, 'username':record['username'], 'role':record['role'], 'csrf':record['csrf']}


@app.post('/api/auth/logout')
def logout(request: Request, response: Response):
    security.logout(DATA_DIR, request.cookies.get(security.COOKIE_NAME))
    response.delete_cookie(security.COOKIE_NAME, path='/')
    return {'ok':True}


@app.post('/api/auth/password')
def change_password(body: PasswordBody, request: Request, response: Response):
    if not security.update_password(DATA_DIR, request.state.username, body.current_password, body.new_password):
        raise HTTPException(400, 'Current password incorrect, or new password is shorter than 12 characters')
    response.delete_cookie(security.COOKIE_NAME, path='/')
    return {'ok':True, 'reauthenticate':True}


@app.post('/api/admin/guest')
def change_guest(body: GuestSettings):
    security.set_guest(DATA_DIR, body.enabled)
    return {'enabled':security.guest_enabled(DATA_DIR)}


@app.get('/api/admin/users')
def users():
    return security.list_users(DATA_DIR)


@app.post('/api/admin/users', status_code=201)
def add_user(body: UserCreate):
    try:
        return security.add_user(DATA_DIR, body.username, body.password, body.role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch('/api/admin/users/{username}')
def update_user(username: str, body: UserUpdate, request: Request):
    try:
        return security.edit_user(DATA_DIR, username, request.state.username,
                                  role=body.role, enabled=body.enabled, password=body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete('/api/admin/users/{username}')
def delete_user(username: str, request: Request):
    try:
        result = security.remove_user(DATA_DIR, username, request.state.username)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Do not allow a later account with the same username to inherit scans
    # owned by someone who was deleted. Transfer them to the acting admin.
    with lock:
        for path in JOBS_DIR.glob('*/job.json'):
            try:
                j = json.loads(path.read_text())
                if j.get('owner') == username:
                    j['owner'] = request.state.username
                    save_job(j)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
    stats.forget_user(DATA_DIR, username)
    return result


@app.get('/api/guest/status')
def guest_status(response: Response):
    token, csrf = security.guest_csrf()
    response.set_cookie(security.GUEST_COOKIE, token, httponly=True, secure=COOKIE_SECURE,
                        samesite='strict', max_age=3600, path='/')
    return {'enabled':security.guest_enabled(DATA_DIR), 'csrf':csrf,
            'max_megabytes':10, 'max_pages':GUEST_MAX_PAGES,
            'formats':['PDF','JPEG','PNG']}


def _check_cups_config() -> None:
    if not cups_servers():
        raise HTTPException(503, 'No print server yet. Add one under Devices → Print servers.')


def print_choices(request: Request, authenticated: bool) -> dict:
    """Explicit, allowlisted CUPS options. Never accept arbitrary lp flags."""
    try:
        copies = int(request.headers.get('x-print-copies', '1'))
        nup = int(request.headers.get('x-print-nup', '1'))
    except ValueError as exc:
        raise HTTPException(400, 'Copies and pages per sheet must be numbers') from exc
    media = request.headers.get('x-print-media', 'default').lower()
    sides = request.headers.get('x-print-sides', 'one-sided').lower()
    orientation = request.headers.get('x-print-orientation', 'auto').lower()
    scale = request.headers.get('x-print-scale','fit').lower()
    pages = request.headers.get('x-print-pages','all').strip()
    collate = request.headers.get('x-print-collate','true').lower() == 'true'
    border = request.headers.get('x-print-border','none').lower()
    quality = request.headers.get('x-print-quality','default').lower()
    color_mode = request.headers.get('x-print-color','auto').lower()
    paper_source = request.headers.get('x-print-source','default').strip()
    queue = choose_print_queue(request, authenticated)
    if color_mode not in ('auto','color','gray','bw'):
        raise HTTPException(400, 'Unsupported print color mode')
    records=devices.all_cups_queues(cups_servers())
    target=devices.resolve_printer(queue,records)
    caps=(devices.cups_capabilities(target['server'],target['queue'])
          if target else {'color':None,'duplex':None,'media':[]})
    if color_mode=='color' and caps['color'] is False:
        raise HTTPException(400, 'The selected printer does not support color printing')
    if sides!='one-sided' and caps['duplex'] is False:
        raise HTTPException(400,'Duplex is not supported by the selected printer')
    if paper_source!='default' and paper_source not in {x['value'] for x in caps.get('paper_sources', [])}:
        raise HTTPException(400,'Paper source not supported by the selected printer. Choose Printer default or refresh printer capabilities.')
    if media not in PRINT_MEDIA or sides not in PRINT_SIDES:
        raise HTTPException(400, 'Unsupported paper size or duplex setting')
    if media!='default' and caps['media'] and media not in printing.match_media_choices(caps['media']):
        raise HTTPException(400,'Paper size not advertised by this CUPS printer queue. Check its driver settings.')
    if media in printing.ENVELOPE_MEDIA and sides!='one-sided':
        raise HTTPException(400,'Envelopes require one-sided printing. Choose One-sided.')
    if border not in ('none','single','double') or quality not in ('default','draft','normal','best'):
        raise HTTPException(400, 'Unsupported border or print quality')
    if orientation not in printing.ORIENTATION or nup not in printing.NUP:
        raise HTTPException(400, 'Unsupported orientation or pages-per-sheet setting')
    if scale not in ('fit','actual'):
        if not scale.isdigit() or not 50 <= int(scale) <= 200:
            raise HTTPException(400, 'Scaling must be Fit, Actual, or 50-200 percent')
    if len(pages)>500 or (pages.lower() not in ('all','*') and
                          not re.fullmatch(r'[0-9,\-\s]+',pages)):
        raise HTTPException(400, 'Use a page range such as 1,3-5,8')
    if not 1 <= copies <= (50 if authenticated else 1):
        raise HTTPException(400, 'Guests may print one copy; signed-in users may select up to 50')
    return {'copies':copies, 'media':media, 'sides':sides, 'orientation':orientation,
            'nup':nup, 'scale':scale, 'pages':pages or 'all', 'collate':collate,
            'border':border, 'quality':quality, 'queue':queue, 'color_mode':color_mode,
            'paper_source':paper_source,
            # CUPS records this as the job's owner, so people can follow and
            # cancel their own jobs (and CUPS page logs show who printed).
            'user':request.state.username if authenticated else cupsadmin.GUEST_USER}


def print_with_cups(file_path: Path, title: str = 'Folio print', *,
                    copies: int = 1, media: str = 'default', sides: str = 'one-sided',
                    orientation: str = 'auto', nup: int = 1, scale: str = 'fit',
                    collate: bool = True, border: str = 'none', quality: str = 'default',
                    queue: str | None = None, color_mode: str = 'auto',
                    paper_source: str = 'default', user: str | None = None) -> str:
    _check_cups_config()
    cfg,records=printer_inventory()
    resolved=devices.resolve_printer(queue or cfg.get('default_printer'),records)
    if not resolved:
        raise HTTPException(404, 'Printer no longer available in configured CUPS servers')
    server=resolved['server']
    queue_name=resolved['queue']
    command = ['lp', '-h', server, '-d', queue_name, *(['-U', user] if user else []),
               '-n', str(copies), '-o', 'job-sheets=none',
               '-o', f'sides={sides}', '-o', f'number-up={nup}',
               '-o', f'collate={str(collate).lower()}']
    if border != 'none' and nup > 1:
        command += ['-o', f'page-border={border}']
    if quality != 'default':
        command += ['-o', f"print-quality={dict(draft=3,normal=4,best=5)[quality]}"]
    if PRINT_MEDIA[media]:
        caps=devices.cups_capabilities(server,queue_name)
        actual=caps.get('media_map',{}).get(media)
        if not actual and caps.get('media'):
            actual=printing.match_media_choices(caps['media']).get(media)
        if caps.get('media') and not actual:
            raise HTTPException(400,'This paper size is no longer offered by CUPS for the selected printer')
        # For a legacy PPD, Env10 must be submitted as PageSize=Env10, not
        # as a guessed media=COM10; the latter may be rejected or ignored.
        if actual and printing.normalized_media_name(actual)!=printing.normalized_media_name(PRINT_MEDIA[media]):
            size_option=caps.get('media_option') or 'PageSize'
            if size_option.lower() not in ('pagesize','media'):
                raise HTTPException(400,'Unrecognized media size option from CUPS')
            command += ['-o', f'{size_option}={actual}']
        else:
            command += ['-o', f'media={PRINT_MEDIA[media]}']
        if media in printing.ENVELOPE_MEDIA and caps.get('envelope_type_option') and caps.get('envelope_type_value'):
            command += ['-o', f"{caps['envelope_type_option']}={caps['envelope_type_value']}"]
    if paper_source != 'default':
        # Revalidate against the chosen CUPS server/queue, not a browser option
        # name. Never send an arbitrary -o option supplied by the client.
        caps=devices.cups_capabilities(server,queue_name)
        if paper_source not in {s['value'] for s in caps['paper_sources']} or not caps['source_option']:
            raise HTTPException(400, 'Paper source is no longer available on this printer')
        command += ['-o', f"{caps['source_option']}={paper_source}"]
    if printing.ORIENTATION[orientation] is not None:
        command += ['-o', f'orientation-requested={printing.ORIENTATION[orientation]}']
    if color_mode in ('gray','bw'):
        command += ['-o','print-color-mode=monochrome']
    elif color_mode=='color':
        command += ['-o','print-color-mode=color']
    if scale == 'fit':
        command += ['-o','fit-to-page']
    command += ['-t', title[:90], '--', str(file_path)]
    try:
        outcome = subprocess.run(command, text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(502, f'Cannot contact CUPS server {server}. Check connectivity and TCP port 631') from exc
    if outcome.returncode != 0:
        detail = (outcome.stderr or outcome.stdout or 'Unknown CUPS error').strip()[-240:]
        raise HTTPException(502, f'CUPS on {server} rejected this print job: {detail}. Check the queue and its permissions.')
    return outcome.stdout.strip()[:240]


def cups_job_number(outcome: str) -> int | None:
    """The job number from lp's "request id is Queue-123 (1 file(s))"."""
    found = re.search(r'request id is\s+\S*-(\d+)(?:\s|$)', outcome, flags=re.I)
    return int(found.group(1)) if found else None


def log_print(choices: dict, pages: int, origin: str, outcome: str) -> None:
    """Add a job CUPS accepted to Folio's print statistics. Never stops a print."""
    try:
        cfg, records = printer_inventory()
        target = devices.resolve_printer(choices.get('queue') or cfg.get('default_printer'), records)
        if not target:
            return
        caps = devices.cups_capabilities(target['server'], target['queue'])
        stats.record(DATA_DIR, user=choices.get('user') or '', server=target['server'], queue=target['queue'],
                     origin=origin, pages=pages, copies=choices.get('copies', 1), nup=choices.get('nup', 1),
                     sides=choices.get('sides', 'one-sided'),
                     color=stats.color_of(choices.get('color_mode', 'auto'), caps.get('color')),
                     media=choices.get('media', 'default'), cups_job=cups_job_number(outcome))
    except Exception:  # noqa: BLE001 - statistics must never fail a print
        log.warning('Could not add a print job to the statistics', exc_info=True)


def cups_receipt(outcome: str) -> dict:
    found = re.search(r'request id is\s+(\S+)', outcome, flags=re.I)
    return {'ok':True, 'message':'Accepted by CUPS (printing may still be pending)',
            'job_id':found.group(1) if found else None,
            'cups_response':outcome}


def validate_print_file(path: Path, name: str, guest: bool) -> dict:
    try:
        return printing.inspect_file(path, name, guest)
    except printing.PrintFileError as exc:
        message = str(exc)
        raise HTTPException(413 if '20 pages' in message else 400, message) from exc


async def spool_upload(request: Request, path: Path, authenticated: bool) -> str:
    """Stream request body to disk with independent user and guest size ceilings."""
    limit = USER_PRINT_MAX_BYTES if authenticated else GUEST_MAX_BYTES
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
    if not security.guest_enabled(DATA_DIR):
        raise HTTPException(403, 'Guest printing is disabled')
    if not security.valid_guest(request.cookies.get(security.GUEST_COOKIE),
                                request.headers.get('x-guest-csrf')):
        raise HTTPException(403, 'Guest session expired. Reopen the guest print page.')


def preview_owner(request: Request, guest: bool) -> str:
    cookie_name = security.GUEST_COOKIE if guest else security.COOKIE_NAME
    cookie = request.cookies.get(cookie_name)
    if not cookie:
        raise HTTPException(403, 'Your session has expired')
    return hashlib.sha256(cookie.encode('utf-8')).hexdigest()


def preview_folder(token: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9_-]{32}',token):
        raise HTTPException(404, 'Preview not found')
    return preview_root() / token


def trim_preview_budget(owner: str):
    # Bound the space taken by file previews even if visitors repeatedly upload
    # and never print. Two previews per session, at most 20 globally / 768 MB.
    previews=[]
    for directory in preview_root().iterdir():
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
    if guest and not security.guest_enabled(DATA_DIR):
        raise HTTPException(403, 'Guest printing is disabled')
    directory=preview_folder(token)
    try:
        meta=json.loads((directory/'meta.json').read_text())
        if meta['owner']!=preview_owner(request,guest) or meta['guest']!=guest:
            raise HTTPException(404,'Preview not found')
        if time.time()-meta['created']>PREVIEW_TTL:
            raise HTTPException(410,'Preview expired. Choose the file again.')
        source=directory/'source'
        if not source.is_file():
            raise HTTPException(404,'Preview not found')
        return source,meta
    except HTTPException:
        raise
    except (OSError,ValueError,KeyError,json.JSONDecodeError):
        raise HTTPException(404, 'Preview not found')


def clear_old_previews():
    """Bound the temporary disk footprint. Preview files aren't scan library data."""
    root=preview_root()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        try:
            meta=json.loads((directory/'meta.json').read_text())
            expired=time.time()-meta['created']>PREVIEW_TTL
        except (OSError,KeyError,ValueError,json.JSONDecodeError):
            expired=True
        if expired:
            shutil.rmtree(directory,ignore_errors=True)


async def inspect_upload(request: Request, guest: bool):
    if guest:
        guest_authorize(request)
    clear_old_previews()
    trim_preview_budget(preview_owner(request,guest))
    token=secrets.token_urlsafe(24)
    directory=preview_folder(token)
    directory.mkdir(mode=0o700)
    try:
        source=directory/'source'
        name=await spool_upload(request,source,not guest)
        info=validate_print_file(source,name,guest)
        record={'owner':preview_owner(request,guest),'guest':guest,
                'name':name,'created':time.time(),**info}
        (directory/'meta.json').write_text(json.dumps(record))
        return {'token':token,'name':name,'expires_in':PREVIEW_TTL,**info}
    except Exception:
        shutil.rmtree(directory,ignore_errors=True)
        raise


@app.post('/api/print/inspect')
async def inspect_user_upload(request: Request):
    return await inspect_upload(request,False)


@app.post('/api/guest/inspect')
async def inspect_guest_upload(request: Request):
    return await inspect_upload(request,True)


@app.get('/api/print/preview/{token}/page/{number}')
def preview_user_page(token: str, number: int, request: Request):
    source,record=preview_record(request,token,False)
    try:
        image=printing.render_preview(source,record['kind'],number)
    except printing.PrintFileError as exc:
        raise HTTPException(404,str(exc)) from exc
    return Response(content=image,media_type='image/jpeg',headers={'Cache-Control':'no-store'})


@app.get('/api/guest/preview/{token}/page/{number}')
def preview_guest_page(token: str, number: int, request: Request):
    source,record=preview_record(request,token,True)
    try:
        image=printing.render_preview(source,record['kind'],number)
    except printing.PrintFileError as exc:
        raise HTTPException(404,str(exc)) from exc
    return Response(content=image,media_type='image/jpeg',headers={'Cache-Control':'no-store'})


class PreviewPrintRequest(BaseModel):
    token: str


def run_print(source: Path, kind: str, title: str, choices: dict,
              task: tasks.Task | None = None, progress: tuple[float, float] = (0.0, 1.0),
              origin: str = 'upload') -> dict:
    """Select pages, apply scaling/colour, and submit to CUPS (shared by all flows).

    ``progress`` is the slice of the task's progress bar this step may use;
    ``origin`` ('document' or 'upload') is recorded in the print statistics.
    """
    choices = dict(choices)
    pages = choices.pop('pages')
    start, end = progress

    def page_progress(done: int, total: int) -> None:
        if task:
            task.update(f'Converting page {done + 1} of {total}…', start + (end - start) * .9 * done / max(total, 1))
    with tempfile.TemporaryDirectory(prefix='folio-print-') as tmp:
        target = Path(tmp) / 'selected.pdf'
        if task:
            task.update('Preparing your pages…', start)
        try:
            selected = printing.prepare_pdf(source, kind, target, pages=pages, media=choices['media'],
                                            scale=choices['scale'], color_mode=choices['color_mode'],
                                            progress=page_progress)
        except printing.PrintFileError as exc:
            raise HTTPException(400, str(exc)) from exc
        if task:
            # Last point at which cancelling stops the job; CUPS owns it after this.
            task.update('Sending to the printer…', start + (end - start) * .95)
        outcome = print_with_cups(target, title, **choices)
    log_print(choices, selected, origin, outcome)
    return {**cups_receipt(outcome), 'pages_selected': selected}


def task_owner(request: Request, guest: bool = False) -> str:
    return f'guest:{preview_owner(request, True)}' if guest else f'user:{request.state.username}'


def task_response(task: tasks.Task) -> JSONResponse:
    return JSONResponse({'task': task.public(tasks.runner.position(task))}, status_code=202)


async def submit_preview(request: Request, body: PreviewPrintRequest, guest: bool, background: bool = False):
    if guest:
        guest_authorize(request)
    source,record=preview_record(request,body.token,guest)
    choices=print_choices(request,not guest)
    # Read metadata again if contents were replaced unexpectedly on disk.
    validate_print_file(source,record['name'],guest)
    if guest:
        ip=request.client.host if request.client else 'unknown'
        if not security.guest_rate_allowed(ip,record=True):
            raise HTTPException(429,'Guest print limit reached. Try again later.')
    if not background:
        return await asyncio.to_thread(run_print, source, record['kind'], record['name'], choices)
    # The preview may expire or be replaced while the job waits, so the job
    # gets its own copy of the file.
    return task_response(tasks.runner.submit(
        'print', 'print', task_owner(request, guest),
        lambda task: run_print(task.directory / 'source', record['kind'], record['name'], choices, task),
        prepare=lambda folder: shutil.copyfile(source, folder / 'source')))


@app.post('/api/print/submit-preview')
async def submit_user_preview(request: Request, body: PreviewPrintRequest, background: bool = False):
    return await submit_preview(request,body,False,background)


@app.post('/api/guest/submit-preview')
async def submit_guest_preview(request: Request, body: PreviewPrintRequest, background: bool = False):
    return await submit_preview(request,body,True,background)


@app.get('/api/printers')
def printers_available(request: Request, refresh: bool = False):
    if refresh:
        devices.invalidate('cups')
    cfg, records=printer_inventory()
    queues=visible_printers(cfg,records,request.state.role)
    return {'default':print_default(cfg,records),
            'printers':[{**q,'capabilities':devices.cups_capabilities(q['server'],q['queue'])} for q in queues]}


@app.get('/api/guest/printers')
def printers_for_guests():
    if not security.guest_enabled(DATA_DIR):
        return {'default':None,'printers':[]}
    cfg, records=printer_inventory()
    queues=visible_printers(cfg,records,'guest')
    return {'default':print_default(cfg,records),
            'printers':[{**q,'capabilities':devices.cups_capabilities(q['server'],q['queue'])} for q in queues]}


@app.get('/api/print/status')
def print_status(request: Request, queue: str | None = None):
    _check_cups_config()
    cfg, records=printer_inventory()
    permitted=visible_printers(cfg,records,request.state.role)
    chosen=devices.resolve_printer(queue or cfg.get('default_printer'),records)
    if chosen and chosen['id'] in {q['id'] for q in permitted}:
        caps=devices.cups_capabilities(chosen['server'],chosen['queue'])
        known=list(caps.get('media_map',{}) or printing.match_media_choices(caps['media']))
        return {'ready':chosen['ready'],'queue':chosen['queue'],'server':chosen['server'],
                'server_label':server_label(chosen['server']),
                'message':'CUPS queue is accepting jobs' if chosen['ready'] else chosen['status'],
                'media':known,'capabilities':caps,
                'paper_labels':{k:v[1] for k,v in printing.MEDIA.items()},
                'supplies_low':[x for x in quiet_supplies(chosen['server'], chosen['queue']) if x['low']]}
    if not records:
        return {'ready':False,'queue':queue or '', 'message':'No printers found. Check your print servers under Devices.',
                'available_queues':[]}
    return {'ready':False,'queue':queue or '',
            'message':'Choose a default printer under Devices, or select a permitted printer',
            'available_queues':[p['label'] for p in permitted]}


@app.get('/api/admin/devices')
def admin_devices(refresh: bool = False):
    if refresh:
        devices.invalidate()
    cfg, records=printer_inventory()
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
        output.append({**record, 'settings':devices.printer_settings(cfg,key,CUPS_QUEUE),
                       'capabilities':devices.cups_capabilities(record['server'],record['queue'])
                       if key in discovered else {}})
    return {'scanners':devices.scanners_state(cfg), 'unregistered':unregistered,
            'default_scanner':cfg['default_scanner'],
            'printers':output, 'default_printer':print_default(cfg,records),
            'cups_servers':cups_servers()}


@app.post('/api/admin/scanners',status_code=201)
def register_scanner(body: ScannerConfigBody):
    cfg=registry()
    try:
        record=devices.make_scanner(body.name,body.ip,body.protocol,body.port,body.path)
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
    persist_registry(cfg)
    return record


@app.post('/api/admin/scanners/attach',status_code=201)
def attach_detected_scanner(body: ScannerAttachBody):
    """Attach only a scanner name actually listed by SANE, not arbitrary shell input."""
    cfg=registry()
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
    persist_registry(cfg)
    return record


@app.patch('/api/admin/scanners/{scanner_id}')
def modify_scanner(scanner_id: str, body: ScannerUpdateBody):
    cfg=registry()
    original=next((e for e in cfg['scanners'] if e['id']==scanner_id),None)
    if not original:
        raise HTTPException(404,'Scanner not found')
    if scan_state['active'] and scanner_id in (scan_state.get('scanner_id'),cfg['default_scanner']):
        raise HTTPException(409,'Wait for the current scan to finish')
    if original['kind']=='attached':
        if body.ip is not None:
            try:
                original.update(devices.make_scanner(body.name or original['name'],body.ip,
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
                    original.update(devices.make_scanner(body.name or original['name'],body.ip,
                        body.protocol or 'WSD',body.port,body.path,existing_id=original['id']))
                except ValueError as exc:
                    raise HTTPException(400,str(exc)) from exc
            elif any(x is not None for x in (body.port,body.path,body.protocol)):
                raise HTTPException(400,'Enter the scanner IP to manage this endpoint in Folio')
            elif body.name:
                original['name']=body.name[:75]
        else:
            try:
                new=devices.make_scanner(body.name or original['name'],body.ip or original['ip'],
                                         body.protocol or original['protocol'],body.port or original['port'],
                                         body.path or original['path'],existing_id=original['id'])
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
    if body.make_default:
        if not original.get('enabled',True):
            raise HTTPException(400,'A disabled scanner cannot be the default')
        cfg['default_scanner']=scanner_id
    elif cfg.get('default_scanner') == scanner_id and not original.get('enabled',True):
        cfg['default_scanner']=next((s['id'] for s in cfg['scanners']
                                     if s['id']!=scanner_id and s.get('enabled',True)),None)
    elif not cfg.get('default_scanner') and original.get('enabled',True):
        cfg['default_scanner']=scanner_id
    persist_registry(cfg)
    return original


@app.delete('/api/admin/scanners/{scanner_id}')
def unregister_scanner(scanner_id: str):
    cfg=registry()
    if scan_state['active'] and scanner_id==scan_state.get('scanner_id'):
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
    persist_registry(cfg)
    return {'deleted':True}


@app.get('/api/admin/scanners/{scanner_id}/test')
def test_scanner(scanner_id: str):
    # A test is an explicit request to talk to the device, never a cached answer.
    devices.invalidate('sane')
    entry,name=get_selected_scanner(scanner_id)
    return {'scanner':scanner_id, 'name':entry['name'], 'device':name,
            'capabilities':devices.scanner_capabilities(name)}


@app.patch('/api/admin/printers/{queue}')
def configure_printer(queue: str, body: PrinterSettingsBody):
    try:
        devices.validate_printer_reference(queue)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    cfg, records=printer_inventory()
    target=devices.resolve_printer(queue,records)
    if not target:
        raise HTTPException(404,'Add the printer in CUPS first, then refresh Devices')
    key=target['id']
    flags=devices.printer_settings(cfg,key,CUPS_QUEUE)
    for field in ('enabled','guest','member','printer'):
        value=getattr(body,field)
        if value is not None:
            flags[field]=value
    if body.make_default:
        if not flags['enabled']:
            raise HTTPException(400,'Disabled printer cannot be default')
        cfg['default_printer']=key
    elif print_default(cfg,records)==key and not flags['enabled']:
        raise HTTPException(400,'Choose another default printer before disabling this queue')
    cfg.setdefault('printers',{})[key]=flags
    persist_registry(cfg)
    return {'id':key,'queue':target['queue'], 'server':target['server'],
            'settings':flags,'default':cfg['default_printer']==key}


# --- Printer administration and print jobs (0.11) --------------------------------

def configured_server(server: str) -> str:
    if server not in cups_servers():
        raise HTTPException(404, 'Unknown print server. Add it under Devices → Print servers first.')
    return server


def cups_call(fn, *args, **kwargs):
    """Run a CUPS request, turning its failures into clear HTTP answers."""
    try:
        return fn(*args, **kwargs)
    except cupsadmin.NotConnected as exc:
        raise HTTPException(409, str(exc)) from exc
    except ipp.AuthRequired as exc:
        raise HTTPException(403, str(exc)) from exc
    except ipp.IPPError as exc:
        if str(exc).startswith('Cannot reach'):
            devices.mark_cups_down(getattr(args[0], 'server', '') if args else '')
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def known_printer(printer: str) -> dict:
    try:
        devices.validate_printer_reference(printer)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    target = devices.resolve_printer(printer, devices.all_cups_queues(cups_servers()))
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


class CancelJobBody(BaseModel):
    server: str = Field(min_length=1, max_length=120)
    job_id: int = Field(ge=1)


class PrintServerBody(BaseModel):
    server: str = Field(min_length=1, max_length=120)


@app.get('/api/admin/cups/servers')
def list_print_servers():
    return [{'server': server, 'admin': cupsadmin.admin_status(DATA_DIR, server),
             'reachable': not devices.cups_backoff(server), 'source': server_source(server),
             'announcing': airprint.available() if cupsadmin.is_builtin(server) else None}
            for server in cups_servers()]


@app.post('/api/admin/cups/servers', status_code=201)
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
    cfg = registry()
    current = cups_servers(cfg)
    if server in current:
        raise HTTPException(409, f'{server} is already a print server')
    if len(current) >= 16:
        raise HTTPException(400, 'Folio supports up to 16 print servers')
    probe = diagnostics.probe_cups_server(server)
    if probe['status'] == 'fail':
        raise HTTPException(400, f'Folio could not reach CUPS at {server}: {probe["detail"]}')
    cfg.setdefault('cups_servers', []).append(server)
    persist_registry(cfg)
    devices.invalidate('cups')
    return {'server': server, 'detail': probe['detail']}


@app.delete('/api/admin/cups/servers/{server}')
def remove_print_server(server: str):
    if cupsadmin.is_builtin(server):
        raise HTTPException(400, 'The built-in print server is part of Folio. Set FOLIO_BUILTIN_CUPS=off to turn it off.')
    if server in CUPS_SERVERS:
        raise HTTPException(400, 'This server is set in CUPS_SERVERS. Remove it there and restart Folio.')
    cfg = registry()
    if server not in cfg.get('cups_servers', []):
        raise HTTPException(404, 'Unknown print server')
    cfg['cups_servers'].remove(server)
    # Its queues disappear from Folio; forget their settings and the stored login.
    cfg['printers'] = {key: value for key, value in cfg.get('printers', {}).items()
                       if not key.startswith(f'{server}|')}
    if (cfg.get('default_printer') or '').startswith(f'{server}|'):
        cfg['default_printer'] = None
    persist_registry(cfg)
    cupsadmin.disconnect_admin(DATA_DIR, server)
    devices.invalidate('cups')
    return {'removed': server}


@app.put('/api/admin/cups/servers/{server}/login')
def cups_connect(server: str, body: CupsLoginBody):
    configured_server(server)
    if cupsadmin.is_builtin(server):
        raise HTTPException(400, 'The built-in print server is connected automatically.')
    return cups_call(cupsadmin.connect_admin, DATA_DIR, server, body.username, body.password)


@app.delete('/api/admin/cups/servers/{server}/login')
def cups_disconnect(server: str):
    configured_server(server)
    if cupsadmin.is_builtin(server):
        raise HTTPException(400, 'The built-in print server is connected automatically.')
    cupsadmin.disconnect_admin(DATA_DIR, server)
    return cupsadmin.admin_status(DATA_DIR, server)


@app.get('/api/admin/cups/servers/{server}/devices')
def cups_devices(server: str):
    conn = cups_call(cupsadmin.connection, DATA_DIR, configured_server(server), admin=True)
    return {'devices': cups_call(cupsadmin.discover, conn)}


@app.get('/api/admin/cups/servers/{server}/makes')
def cups_makes(server: str):
    conn = cupsadmin.connection(DATA_DIR, configured_server(server))
    return {'makes': cups_call(cupsadmin.makes, conn)}


@app.get('/api/admin/cups/servers/{server}/drivers')
def cups_drivers(server: str, make: str, model: str = ''):
    if not 1 <= len(make) <= 80 or len(model) > 120:
        raise HTTPException(400, 'Choose a manufacturer')
    conn = cupsadmin.connection(DATA_DIR, configured_server(server))
    drivers = cups_call(cupsadmin.models, conn, make)
    return {'drivers': cupsadmin.rank_drivers(drivers, model, limit=60), 'total': len(drivers)}


@app.post('/api/admin/cups/servers/{server}/printers', status_code=202)
def cups_add_printer(server: str, body: AddPrinterBody, request: Request):
    conn = cups_call(cupsadmin.connection, DATA_DIR, configured_server(server), admin=True)
    if body.device_uri:
        if not cupsadmin.was_discovered(server, body.device_uri):
            raise HTTPException(400, 'That printer is no longer in the list of detected devices. Press Find printers again.')
        device_uri = body.device_uri
    elif body.connection and body.address:
        device_uri = cups_call(cupsadmin.manual_uri, body.connection, body.address, body.port, body.path)
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
        cups_call(cupsadmin.add_printer, conn, body.name, device_uri, body.driver, body.description, body.location)
        cfg = registry()
        cfg.setdefault('printers', {})[key] = {'enabled': True, 'guest': body.guest, 'member': True, 'printer': True}
        if body.make_default or not cfg.get('default_printer'):
            cfg['default_printer'] = key
        persist_registry(cfg)
        task.update('Checking the new printer…', .8)
        if cupsadmin.is_builtin(server):
            announce_printers()
        return {'id': key, 'queue': body.name, 'server': server, 'state': cups_call(cupsadmin.printer_state, conn, body.name)}
    return task_response(tasks.runner.submit('printer-setup', 'print', task_owner(request), work))


@app.delete('/api/admin/printers/{printer}')
def cups_remove_printer(printer: str):
    target = known_printer(printer)
    conn = cups_call(cupsadmin.connection, DATA_DIR, target['server'], admin=True)
    cups_call(cupsadmin.delete_printer, conn, target['queue'])
    cfg = registry()
    cfg.get('printers', {}).pop(target['id'], None)
    if cfg.get('default_printer') == target['id']:
        cfg['default_printer'] = None
    persist_registry(cfg)
    if cupsadmin.is_builtin(target['server']):
        announce_soon()
    return {'deleted': target['id']}


@app.get('/api/admin/printers/{printer}/state')
def cups_printer_state(printer: str):
    target = known_printer(printer)
    return cups_call(cupsadmin.printer_state, cupsadmin.connection(DATA_DIR, target['server']), target['queue'])


@app.post('/api/admin/printers/{printer}/pause')
def cups_pause_printer(printer: str, body: PausePrinterBody):
    target = known_printer(printer)
    conn = cups_call(cupsadmin.connection, DATA_DIR, target['server'], admin=True)
    cups_call(cupsadmin.set_paused, conn, target['queue'], body.paused)
    return cups_call(cupsadmin.printer_state, conn, target['queue'])


@app.post('/api/admin/printers/{printer}/test-page')
def cups_test_page(printer: str, request: Request):
    target = known_printer(printer)
    with tempfile.TemporaryDirectory(prefix='folio-test-page-') as tmp:
        page = Path(tmp) / 'test-page.pdf'
        printing.test_page(page, printer=target['queue'], server=target['server'],
                           user=request.state.username, version=__version__)
        outcome = print_with_cups(page, 'Folio test page', queue=target['id'], user=request.state.username)
    log_print({'queue': target['id'], 'user': request.state.username}, 1, 'test-page', outcome)
    return cups_receipt(outcome)


def printer_supplies(server: str, queue: str) -> list[dict]:
    conn = cupsadmin.connection(DATA_DIR, server, timeout=8)
    return devices.cached(('cups', 'supplies', server, queue), SUPPLIES_TTL,
                          lambda: cupsadmin.printer_supplies(conn, queue))


def quiet_supplies(server: str, queue: str) -> list[dict]:
    """Supplies for a status line: an unreachable printer just has none."""
    if devices.cups_backoff(server):
        return []
    try:
        return printer_supplies(server, queue)
    except (ipp.IPPError, OSError):
        return []


@app.get('/api/admin/printers/{printer}/supplies')
def cups_printer_supplies(printer: str):
    target = known_printer(printer)
    return {'supplies': cups_call(printer_supplies, target['server'], target['queue'])}


@app.get('/api/admin/printers/{printer}/jobs')
def cups_printer_jobs(printer: str, which: Literal['not-completed', 'completed'] = 'not-completed'):
    """Everyone's jobs on one printer; titles and owners need the admin login."""
    target = known_printer(printer)
    admin = cupsadmin.admin_status(DATA_DIR, target['server'])['connected']
    conn = cupsadmin.connection(DATA_DIR, target['server'], admin=admin)
    return {'jobs': cups_call(cupsadmin.list_jobs, conn, queue=target['queue'], which=which, limit=30),
            'details_hidden': not admin}


@app.post('/api/admin/jobs/cancel')
def cups_admin_cancel_job(body: CancelJobBody, request: Request):
    conn = cups_call(cupsadmin.connection, DATA_DIR, configured_server(body.server), admin=True)
    cups_call(cupsadmin.cancel_job, conn, body.job_id, request.state.username)
    stats.mark_cancelled(DATA_DIR, body.server, body.job_id)
    return {'cancelled': body.job_id}


class StatsSettingsBody(BaseModel):
    enabled: bool
    retention_days: int
    include_cups_jobs: bool | None = None


def stats_period(period: str) -> str:
    if period not in stats.PERIODS:
        raise HTTPException(400, 'Unknown period')
    return period


@app.get('/api/admin/stats')
def print_statistics(period: str = '12m'):
    """What Folio has printed, by person and printer (plus jobs sent straight to CUPS, if chosen)."""
    cupsimport.import_jobs(DATA_DIR, cups_servers(), min_interval=60)
    return {**stats.summary(DATA_DIR, stats_period(period)), 'guest_user': cupsadmin.GUEST_USER}


@app.get('/api/admin/stats/export')
def export_print_statistics(period: str = 'all'):
    period = stats_period(period)  # Before streaming starts; it also names the file

    def cell(value):
        # Spreadsheets run cells that start with these as formulas.
        text = '' if value is None else str(value)
        return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text

    def lines():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(stats.CSV_COLUMNS)
        for row in stats.export_rows(DATA_DIR, period):
            writer.writerow([cell(value) for value in row])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
        yield buffer.getvalue()
    filename = f'folio-print-log-{period}-{datetime.now().strftime("%Y-%m-%d")}.csv'
    return StreamingResponse(lines(), media_type='text/csv; charset=utf-8',
                             headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.put('/api/admin/stats/settings')
def save_statistics_settings(body: StatsSettingsBody):
    try:
        return stats.save_settings(DATA_DIR, enabled=body.enabled, retention_days=body.retention_days,
                                   include_cups_jobs=body.include_cups_jobs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete('/api/admin/stats')
def clear_print_statistics():
    return {'deleted': stats.clear(DATA_DIR)}


@app.get('/api/print/jobs')
def my_print_jobs(request: Request):
    """The signed-in person's recent print jobs on every CUPS server."""
    user = request.state.username

    def server_jobs(server: str) -> list[dict]:
        conn = cupsadmin.connection(DATA_DIR, server, timeout=6)
        active = cupsadmin.list_jobs(conn, user=user, which='not-completed', limit=20)
        return active + cupsadmin.list_jobs(conn, user=user, which='completed', limit=10)
    jobs, unavailable = [], []
    servers = cups_servers()
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


@app.post('/api/print/jobs/cancel')
def cancel_my_print_job(body: CancelJobBody, request: Request):
    user = request.state.username
    conn = cupsadmin.connection(DATA_DIR, configured_server(body.server))
    # Folio checks ownership itself before acting, whichever login it uses.
    if cups_call(cupsadmin.job_owner, conn, body.job_id, user) != user:
        raise HTTPException(404, 'Print job not found')
    try:
        cupsadmin.cancel_job(conn, body.job_id, user)
    except ipp.AuthRequired:
        if not cupsadmin.admin_status(DATA_DIR, body.server)['connected']:
            raise HTTPException(403, 'This CUPS server only lets administrators cancel jobs. '
                                     'Ask an administrator to connect its login in Devices.')
        cups_call(cupsadmin.cancel_job, cupsadmin.connection(DATA_DIR, body.server, admin=True), body.job_id, user)
    except ipp.IPPError as exc:
        raise HTTPException(502, str(exc)) from exc
    stats.mark_cancelled(DATA_DIR, body.server, body.job_id)
    return {'cancelled': body.job_id}


@app.get('/api/scanners')
def scanners_available():
    cfg=registry()
    return {'default':cfg['default_scanner'],
            'scanners':[e for e in devices.scanners_state(cfg) if e.get('enabled',True)]}


@app.post('/api/guest/print')
async def print_guest_file(request: Request):
    return await receive_print_file(request, authenticated=False)


@app.post('/api/print')
async def print_authenticated_file(request: Request):
    return await receive_print_file(request, authenticated=True)


async def receive_print_file(request: Request, *, authenticated: bool):
    """Legacy direct-upload endpoint retained for existing clients/tests."""
    if not authenticated:
        guest_authorize(request)
    choices=print_choices(request,authenticated)
    with tempfile.TemporaryDirectory(prefix='folio-print-') as tmp:
        incoming=Path(tmp)/'source'
        name=await spool_upload(request,incoming,authenticated)
        info=validate_print_file(incoming,name,not authenticated)
        if not authenticated:
            ip=request.client.host if request.client else 'unknown'
            if not security.guest_rate_allowed(ip,record=True):
                raise HTTPException(429,'Guest print limit reached. Try again later.')
        return await asyncio.to_thread(run_print, incoming, info['kind'], name, choices)


def asset_version() -> str:
    """Fingerprint of the page's script and styles.

    The page references them as /assets/app.js?v=<fingerprint>, so after an
    upgrade browsers and caching proxies (Nginx Proxy Manager's "Cache Assets"
    keeps them 30 minutes regardless of headers) fetch the new files at once.
    """
    digest = hashlib.sha256()
    for name in ('app.js', 'style.css'):
        digest.update((WEB_DIR / name).read_bytes())
    return digest.hexdigest()[:12]


@app.get('/', response_class=HTMLResponse)
def homepage():
    version = asset_version()
    page = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
    for asset in ('/assets/app.js', '/assets/style.css'):
        page = page.replace(f'"{asset}"', f'"{asset}?v={version}"')
    # The page itself is always revalidated; it is what points at the current assets.
    return HTMLResponse(page, headers={'Cache-Control': 'no-cache'})


@app.get('/api/health')
def health():
    """Cheap liveness check for Docker/Portainer; reveals no configuration."""
    problems = diagnostics.storage_problems(DATA_DIR)
    if problems:
        return JSONResponse({'ok': False, 'version': __version__, 'detail': problems}, status_code=503)
    return {'ok': True, 'version': __version__}


@app.get('/api/features')
def available_features():
    return {'version': __version__, **features.summary(),
            'builtin_print_server': printserver.SERVER if printserver.enabled() else None}


@app.exception_handler(features.Unavailable)
def feature_unavailable(request: Request, exc: features.Unavailable):
    return JSONResponse({'detail': str(exc), 'feature': exc.feature}, status_code=409)


@app.get('/api/admin/diagnostics')
def admin_diagnostics(request: Request):
    cfg = registry()
    servers = cups_servers(cfg)
    present = [(q['server'], q['queue']) for q in devices.all_cups_queues(servers) if q.get('queue')]
    return diagnostics.run_checks(
        data_dir=DATA_DIR, jobs_dir=JOBS_DIR, cups_servers=servers, registry=cfg,
        request=request, cookie_secure=COOKIE_SECURE, version=__version__,
        default_printer=cfg.get('default_printer'), printers=present[:20], supplies_for=quiet_supplies)


@app.get('/api/scanner')
def scanner():
    cfg=registry()
    entry=next((e for e in cfg['scanners'] if e['id']==cfg.get('default_scanner')),None)
    # Compatible response for v0.8 clients; v0.9 uses /api/scanners.
    if entry and entry['kind']=='existing':
        return {'connected': available_device(), 'name':entry['name'],
                'sources':['ADF','Flatbed'], 'modes':['Color','Gray'],
                'resolutions':list(RESOLUTIONS), 'device':DEVICE}
    matching=next((e for e in devices.scanners_state(cfg) if e['id']==cfg['default_scanner']),None)
    return {'connected':bool(matching and matching['connected']),
            'name':matching['name'] if matching else 'Scanner',
            'protocol':(matching.get('protocol') or 'SANE') if matching else None,
            **(matching['capabilities'] if matching else {}),
            'device':matching['sane_name'] if matching else None}


@app.get('/api/scan')
def scan_status(request: Request):
    with lock:
        status = dict(scan_state)
    if request.state.role != 'admin' and status.get('job_id'):
        try:
            job = load_job(status['job_id'])
            if (job.get('owner') or security.initial_admin(DATA_DIR)) != request.state.username:
                status['job_id'] = None
                status['message'] = 'Scanner busy' if status['active'] else 'Ready'
                status['error'] = None
        except HTTPException:
            status['job_id'] = None
    return status


@app.get('/api/jobs')
def list_jobs(request: Request):
    result = []
    for path in JOBS_DIR.glob('*/job.json'):
        try:
            j = json.loads(path.read_text())
            owner = (j.get('owner') or security.initial_admin(DATA_DIR))
            if request.state.role != 'admin' and owner != request.state.username:
                continue
            result.append({'id': j['id'], 'title': j['title'], 'updated': j['updated'],
                           'pages': len(j['pages']), 'owner': owner})
        except (KeyError, json.JSONDecodeError, OSError):
            continue
    result.sort(key=lambda j: j['updated'], reverse=True)
    return result[:60]


@app.post('/api/jobs', status_code=201)
def create_job(body: NewDocument, request: Request):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = new_id()
    path = JOBS_DIR / job_id
    path.mkdir()
    (path / 'pages').mkdir()
    (path / 'thumbs').mkdir()
    job = {'id': job_id, 'title': body.title.strip() or 'Untitled document',
           'owner': request.state.username, 'created': now(),
           'updated': now(), 'pages': [], 'deleted_pages': [], 'batches': []}
    save_job(job)
    return job


@app.get('/api/jobs/{job_id}')
def read_job(job_id: str):
    with lock:
        return load_job(job_id)


@app.patch('/api/jobs/{job_id}')
def rename_job(job_id: str, body: RenameDocument):
    with lock:
        j = load_job(job_id)
        j['title'] = body.title.strip()
        save_job(j)
        return j


@app.delete('/api/jobs/{job_id}', status_code=200)
def delete_document(job_id: str):
    with lock:
        path = job_path(job_id)
        if scan_state['active'] and scan_state['job_id'] == job_id:
            raise HTTPException(409, 'This document is being scanned. Cancel or finish the scan first.')
        # Validated ID, a directory created inside JOBS_DIR by this app.
        # Delete source images, thumbnails, batches, metadata, and edit history.
        shutil.rmtree(path)
        return {'ok': True, 'deleted': job_id}


def run_scanner_process(command: list[str], scratch: Path, stop_event: threading.Event) -> tuple[int | None, str | None]:
    """Run scanimage with graceful SANE cancellation and a last-resort kill.

    A single SIGINT asks scanimage to invoke sane_cancel; a second SIGINT
    would abort it immediately, so we never send one. SIGKILL is a fallback
    after the grace period, not a WSD/firmware-level cancellation guarantee.
    Stderr goes to a file to avoid blocked pipes during long ADF batches.
    """
    log_path = scratch / 'scanimage.stderr'
    deadline = time.monotonic() + SCAN_TIMEOUT_SECONDS
    reason = None
    stop_requested_at = None
    forced = False
    with log_path.open('w', encoding='utf-8') as log:
        try:
            proc = subprocess.Popen(command, cwd=scratch,
                                    stdout=subprocess.DEVNULL, stderr=log,
                                    start_new_session=True)
        except OSError as exc:
            return None, str(exc)
        try:
            while proc.poll() is None:
                current_time = time.monotonic()
                if stop_requested_at is None:
                    if stop_event.is_set():
                        reason = 'Cancelled by user'
                    elif current_time >= deadline:
                        reason = f'Scan timed out after {int(SCAN_TIMEOUT_SECONDS)} seconds'
                    if reason is not None:
                        stop_requested_at = current_time
                        with lock:
                            scan_state.update(message='Requesting graceful scanner cancellation…',
                                              cancel_requested=True, updated=time.time())
                        try:
                            os.killpg(proc.pid, signal.SIGINT)
                        except ProcessLookupError:
                            pass
                elif current_time - stop_requested_at >= SCAN_GRACE_SECONDS:
                    if proc.poll() is None:
                        forced = True
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    break
                time.sleep(0.2)
            # Do not proceed until the local scanimage process has exited.
            proc.wait(timeout=10)
        except Exception:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            raise

    if reason:
        if forced:
            reason += '; scanimage did not exit gracefully and was force-stopped'
        return proc.returncode, reason
    if proc.returncode != 0:
        try:
            error = log_path.read_text(encoding='utf-8', errors='replace').strip()[-800:]
        except OSError:
            error = ''
        return proc.returncode, error or f'scanimage exited {proc.returncode}'
    return proc.returncode, None


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
            if scan_cancel_event.is_set():
                problem = 'Cancelled before scanning started'
            else:
                _, problem = run_scanner_process(command, scratch, scan_cancel_event)
            cancelled = scan_cancel_event.is_set() or (problem is not None and 'timed out' in problem.lower())

            files = sorted(scratch.glob('page-*.png'))
            imported = []
            with lock:
                job = load_job(job_id)
                path = job_path(job_id)
                for f in files:
                    page_id = new_id(16)
                    output = path / 'pages' / f'{page_id}.png'
                    try:
                        with Image.open(f) as im:
                            im.load()
                            thumb = ImageOps.contain(im.convert('RGB'), (340, 440))
                            thumb.save(path / 'thumbs' / f'{page_id}.jpg', 'JPEG', quality=78)
                        shutil.copyfile(f, output)
                    except (OSError, ValueError):
                        continue
                    page = {'id': page_id, 'file': f'{page_id}.png', 'rotation': 0,
                            'export_mode': 'Original', 'crop': None, 'scale': 100, 'deskew': 0.0, 'scanned_mode': req.mode,
                            'dpi': req.resolution, 'batch_id': batch_id, 'created': now()}
                    job['pages'].append(page)
                    imported.append(page_id)
                if imported:
                    job['batches'].append({'id': batch_id, 'created': now(),
                                           'label': f"{req.source} · {req.mode} · {len(imported)} page(s)",
                                           'role': req.role, 'pages': imported, 'mode': req.mode,
                                           'source': req.source, 'resolution': req.resolution,
                                           'scanner_id':req.scanner_id or registry()['default_scanner']})
                    save_job(job)

                if problem and not cancelled:
                    # The device may have gone away; re-probe instead of trusting the cache.
                    devices.invalidate('sane')
                message = f'Added {len(imported)} page(s)' if imported else 'No scanned pages received'
                if problem:
                    message += f' · {problem}' if cancelled else ' · Feeder stopped; completed pages were kept'
                scan_state.update(active=False, message=message, pages_added=len(imported),
                                  cancelled=cancelled, cancel_requested=False,
                                  error=None if imported else (problem or message),
                                  updated=time.time())
    except Exception as exc:
        with lock:
            scan_state.update(active=False, message='Scan failed', error=str(exc),
                              cancel_requested=False, updated=time.time())
    finally:
        scan_cancel_event.clear()
        scanner_lock.release()


@app.post('/api/scan/cancel', status_code=202)
def cancel_scan(request: Request):
    with lock:
        if not scan_state['active']:
            raise HTTPException(409, 'No scan is currently running')
        if request.state.role != 'admin':
            active = load_job(scan_state['job_id'])
            if (active.get('owner') or security.initial_admin(DATA_DIR)) != request.state.username:
                raise HTTPException(403, 'Only the owner can cancel this scan')
        scan_cancel_event.set()
        scan_state.update(cancel_requested=True, message='Cancellation requested…',
                          updated=time.time())
        return dict(scan_state)


@app.post('/api/jobs/{job_id}/scan', status_code=202)
def start_scan(job_id: str, request: ScanRequest):
    job_path(job_id)
    entry, sane_device = get_selected_scanner(request.scanner_id)
    ensure_scanner_options(request,sane_device,entry)
    if not scanner_lock.acquire(blocking=False):
        raise HTTPException(409, 'Another scan is already running')
    scan_cancel_event.clear()
    with lock:
        scan_state.update(active=True, message='Scanning…', pages_added=0,
                          job_id=job_id, scanner_id=entry['id'], error=None, cancelled=False,
                          cancel_requested=False, updated=time.time())
    try:
        thread = threading.Thread(target=execute_scan, args=(job_id, request, sane_device), daemon=True)
        thread.start()
    except Exception:
        scanner_lock.release()
        raise
    return dict(scan_state)


@app.post('/api/jobs/{job_id}/reorder')
def reorder_pages(job_id: str, request: PageOrder):
    with lock:
        j = load_job(job_id)
        ids = [p['id'] for p in j['pages']]
        if len(ids) != len(request.ids) or set(ids) != set(request.ids):
            raise HTTPException(400, 'Page IDs must exactly match current document')
        by_id = {p['id']: p for p in j['pages']}
        j['pages'] = [by_id[i] for i in request.ids]
        save_job(j)
        return j


@app.patch('/api/jobs/{job_id}/pages/{page_id}')
def edit_page(job_id: str, page_id: str, request: PagePatch):
    with lock:
        j = load_job(job_id)
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
        p['edited_at'] = now()
        save_job(j)
        return j


# Full restoration restores the raw scanned page, not just UI zoom or guides.
# Keep original page files untouched: adjustments are metadata only.
ORIGINAL_PAGE_EDIT_VALUES = {
    'rotation': 0,
    'deskew': 0.0,
    'crop': None,
    'scale': 100,
    'brightness': 0,
    'contrast': 0,
    'auto_levels': False,
    'export_mode': 'Original',
}


@app.post('/api/jobs/{job_id}/pages/{page_id}/restore-original')
def restore_original_scan(job_id: str, page_id: str):
    """Discard all page editing metadata and return to immutable scan pixels.

    The original image remains saved in pages/<filename> throughout editing.
    This operation does not rescan, delete, or change that source file.
    """
    with lock:
        job = load_job(job_id)
        page = get_page(job, page_id)
        page.update(ORIGINAL_PAGE_EDIT_VALUES)
        page['edited_at'] = now()
        save_job(job)
        return job


@app.post('/api/jobs/{job_id}/pages/{page_id}/auto-straighten')
def auto_straighten(job_id: str, page_id: str):
    with lock:
        j = load_job(job_id)
        p = get_page(j, page_id)
        # Estimate against the CURRENTLY SELECTED CONTENT, before fine deskew.
        # Pixels cut away by cropping must not influence auto-straighten.
        base = load_page_image(job_id, {**p, 'deskew': 0, 'scale': 100,
                                        'brightness': 0, 'contrast': 0,
                                        'auto_levels': False, 'export_mode': 'Original'})
        angle, evidence = find_deskew_angle(base)
        # An estimate is NEVER applied automatically. Client previews the
        # suggested correction and requires explicit approval.
        return {'angle': angle, 'line_count': evidence, 'page_id': page_id,
                'message': 'No reliable skew found; page left unchanged' if not angle else f'Suggested {angle:+.1f}° correction (not yet applied)'}


@app.delete('/api/jobs/{job_id}/pages/{page_id}')
def delete_page(job_id: str, page_id: str):
    with lock:
        j = load_job(job_id)
        p = get_page(j, page_id)
        j['deleted_pages'].append({'page': p, 'index': j['pages'].index(p)})
        j['pages'] = [x for x in j['pages'] if x['id'] != page_id]
        save_job(j)
        return j


@app.post('/api/jobs/{job_id}/undo-delete')
def undo_delete(job_id: str):
    with lock:
        j = load_job(job_id)
        if not j['deleted_pages']:
            raise HTTPException(400, 'Nothing to restore')
        last = j['deleted_pages'].pop()
        j['pages'].insert(min(len(j['pages']), last['index']), last['page'])
        save_job(j)
        return j


@app.post('/api/jobs/{job_id}/pages/{page_id}/duplicate')
def duplicate_page(job_id: str, page_id: str):
    with lock:
        j = load_job(job_id)
        p = get_page(j, page_id)
        clone = {**p, 'id': new_id(16), 'created': now()}
        # File and thumbnail point to same immutable original.
        j['pages'].insert(j['pages'].index(p) + 1, clone)
        save_job(j)
        return j


@app.post('/api/jobs/{job_id}/interleave')
def interleave(job_id: str, request: InterleaveRequest):
    with lock:
        j = load_job(job_id)
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
        save_job(j)
        return j


def load_page_image(job_id: str, page: dict, *, apply_edit=True, uncropped=False) -> Image.Image:
    file = job_path(job_id) / 'pages' / page['file']
    with RENDER_SLOTS:
        image = Image.open(file)
        image.load()
    if apply_edit:
        image = transform_page(image, page, uncropped=uncropped)
    return image


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
    cache = job_path(job_id) / 'cache'
    proxy = cache / f"{Path(page['file']).stem}.proxy.jpg"
    if not proxy.is_file():
        original = load_page_image(job_id, page, apply_edit=False)
        # Resizing a page that is only slightly larger than the proxy costs more
        # than it saves (A4 at 300 DPI is 2480x3508); keep its pixels and let
        # each preview size downscale from the JPEG instead.
        if original.width > PROXY_SIZE[0] * 1.25 or original.height > PROXY_SIZE[1] * 1.25:
            original.thumbnail(PROXY_SIZE, Image.Resampling.LANCZOS)
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
    before, after = PREVIEW_SIZES[size_name]
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
    key = hashlib.sha256(json.dumps([RENDER_VERSION, variant, state], sort_keys=True)
                         .encode()).hexdigest()[:16]
    cache = job_path(job_id) / 'cache'
    stem = Path(page['file']).stem
    target = cache / f'{stem}.{variant}.{key}.jpg'
    try:
        return target.read_bytes()
    except FileNotFoundError:
        pass
    proxy = page_proxy(job_id, page)  # Takes its own render slot when building
    with RENDER_SLOTS:
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


@app.get('/api/jobs/{job_id}/pages/{page_id}/image')
def page_image(job_id: str, page_id: str, thumb: bool = False, uncropped: bool = False, large: bool = False):
    with lock:
        j = load_job(job_id)
        # Allow media of a recently removed page until browser refresh.
        p = next((p for p in j['pages'] if p['id'] == page_id), None)
        if p is None:
            raise HTTPException(404, 'Page not found')
    data = render_preview_jpeg(job_id, p, preview_variant(thumb, large, uncropped))
    # Scans are private documents: keep them out of shared browser caches.
    return Response(content=data, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


def stop_process_group(proc: subprocess.Popen, grace: float = 10.0) -> None:
    """Stop a process started with start_new_session=True and everything it spawned."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # Wait without reaping: until the leader is reaped, its group ID cannot be
    # reused by an unrelated process, so the SIGKILL below is always safe.
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                break
        except ChildProcessError:
            break
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # Workers that ignored SIGTERM or outlived the leader
    except ProcessLookupError:
        pass
    proc.wait()


def ocr_pdf(source: Path, target: Path, page_count: int, cancel: threading.Event | None = None) -> None:
    """Run OCRmyPDF only when requested; never modify original page scans.

    A limited OCR worker count protects small Proxmox VMs against resource spikes.
    ``cancel`` stops OCRmyPDF early when a background export is cancelled.
    """
    command = ['ocrmypdf', '--skip-text', '--output-type', 'pdf', '--optimize', '0',
               '--jobs', '2', '--language', 'eng', '--quiet', str(source), str(target)]
    deadline = time.monotonic() + min(1800, max(180, page_count * 90))
    try:
        # Output is discarded: never expose internal paths or OCR engine messages.
        # Its own process group, so stopping it also stops the Ghostscript and
        # Tesseract workers it starts instead of leaving them running.
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except FileNotFoundError as exc:
        raise HTTPException(503, 'OCR engine is not installed in this image') from exc
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                raise tasks.Cancelled()
            if time.monotonic() > deadline:
                raise HTTPException(504, 'OCR timed out. Try the standard PDF export')
            time.sleep(0.25)
    finally:
        if proc.poll() is None:
            stop_process_group(proc)
    if proc.returncode != 0 or not target.is_file():
        raise HTTPException(502, 'OCR processing failed. Standard PDF export is still available')


def build_job_pdf(job_id: str, j: dict, target: Path, quality: str = 'balanced', ocr: bool = False,
                  task: tasks.Task | None = None, progress: tuple[float, float] = (0.0, 1.0)) -> None:
    """Write the document's edited pages, at full resolution, to ``target``."""
    import img2pdf
    q = {'small': 62, 'balanced': 82, 'best': 93}[quality]
    start, end = progress
    # OCR usually takes far longer than assembling the pages.
    assembled = start + (end - start) * (.3 if ocr else 1)
    total = len(j['pages'])
    with tempfile.TemporaryDirectory(prefix='scan-studio-pdf-') as tmp:
        temp_images = []
        for index, page in enumerate(j['pages']):
            if task:
                task.update(f'Preparing page {index + 1} of {total}…', start + (assembled - start) * index / total)
            im = load_page_image(job_id, page)
            if im.mode not in ('RGB', 'L'):
                im = im.convert('RGB')
            p = Path(tmp) / f'{index:04d}.jpg'
            im.save(p, 'JPEG', quality=q, optimize=True,
                    dpi=(page['dpi'], page['dpi']))
            temp_images.append(str(p))
        plain = Path(tmp) / 'plain.pdf' if ocr else target
        with plain.open('wb') as output:
            img2pdf.convert(temp_images, outputstream=output)
        if ocr:
            # A background export waits its turn; a direct download fails fast.
            if not ocr_lock.acquire(blocking=task is not None):
                raise HTTPException(429, 'Another searchable PDF export is in progress')
            try:
                if task:
                    task.update('Recognizing text (OCR). This can take a few minutes…', assembled)
                    ocr_pdf(plain, target, total, cancel=task.cancel_event)
                else:
                    ocr_pdf(plain, target, total)
            finally:
                ocr_lock.release()


def make_job_pdf(job_id: str, j: dict, quality: str = 'balanced', ocr: bool = False) -> bytes:
    with tempfile.TemporaryDirectory(prefix='scan-studio-pdf-') as tmp:
        target = Path(tmp) / 'document.pdf'
        build_job_pdf(job_id, j, target, quality, ocr)
        return target.read_bytes()


def export_filename(j: dict, ocr: bool) -> str:
    safe_name = re.sub(r'[^a-zA-Z0-9._ -]', '', j['title']).strip() or 'Scanned-document'
    return safe_name[:80] + ('-searchable' if ocr else '') + '.pdf'


@app.get('/api/jobs/{job_id}/export')
def export_pdf(job_id: str, quality: Literal['small', 'balanced', 'best'] = 'balanced',
               ocr: bool = False):
    """Direct download, kept for scripts. The web app uses background exports."""
    if ocr:
        features.require('ocr')
    with lock:
        j = load_job(job_id)
    if not j['pages']:
        raise HTTPException(400, 'There are no pages to export')
    pdf_bytes = make_job_pdf(job_id, j, quality, ocr)
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type='application/pdf',
                             headers={'Content-Disposition': f'attachment; filename="{export_filename(j, ocr)}"',
                                      'Cache-Control': 'no-store'})


class ExportRequest(BaseModel):
    quality: Literal['small', 'balanced', 'best'] = 'balanced'
    ocr: bool = False


@app.post('/api/jobs/{job_id}/exports', status_code=202)
def start_export(job_id: str, body: ExportRequest, request: Request):
    """Build a PDF in the background; poll /api/tasks/{id}, then download its file."""
    if body.ocr:
        features.require('ocr')
    with lock:
        j = load_job(job_id)  # Snapshot: later edits don't change this export
    if not j['pages']:
        raise HTTPException(400, 'There are no pages to export')

    def work(task: tasks.Task) -> dict:
        target = task.directory / 'export.pdf'
        build_job_pdf(job_id, j, target, body.quality, body.ocr, task)
        task.file, task.filename = target, export_filename(j, body.ocr)
        return {'pages': len(j['pages']), 'bytes': target.stat().st_size}
    return task_response(tasks.runner.submit('export', 'export', task_owner(request), work))


@app.post('/api/jobs/{job_id}/print')
async def print_folio_document(job_id: str, request: Request, background: bool = False):
    """Print an existing owned document without uploading it back from the browser."""
    choices = print_choices(request, True)
    with lock:
        j = load_job(job_id)
    if not j['pages']:
        raise HTTPException(400, 'Scan at least one page before printing')
    if not background:
        pdf_bytes = await asyncio.to_thread(make_job_pdf, job_id, j, 'balanced', False)
        with tempfile.TemporaryDirectory(prefix='folio-print-document-') as tmp:
            filepath = Path(tmp) / 'document.pdf'
            filepath.write_bytes(pdf_bytes)
            return await asyncio.to_thread(run_print, filepath, 'pdf', j['title'], choices, origin='document')

    def work(task: tasks.Task) -> dict:
        document = task.directory / 'document.pdf'
        build_job_pdf(job_id, j, document, 'balanced', False, task, progress=(0.0, .6))
        return run_print(document, 'pdf', j['title'], choices, task, progress=(.6, 1.0), origin='document')
    return task_response(tasks.runner.submit('print', 'print', task_owner(request), work))


def owned_task(task_id: str, owner: str) -> tasks.Task:
    task = tasks.runner.get(task_id, owner) if TASK_ID_RE.fullmatch(task_id) else None
    if not task:
        raise HTTPException(404, 'This job is no longer available. Folio may have restarted; please try again.')
    return task


@app.get('/api/tasks/{task_id}')
def task_status(task_id: str, request: Request):
    task = owned_task(task_id, task_owner(request))
    return task.public(tasks.runner.position(task))


@app.get('/api/tasks/{task_id}/file')
def task_file(task_id: str, request: Request):
    task = owned_task(task_id, task_owner(request))
    if task.status != 'done' or not task.file or not task.file.is_file():
        raise HTTPException(409, 'This file is not ready yet')
    return FileResponse(task.file, media_type='application/pdf', filename=task.filename,
                        headers={'Cache-Control': 'no-store'})


@app.delete('/api/tasks/{task_id}')
def cancel_task(task_id: str, request: Request):
    task = owned_task(task_id, task_owner(request))
    tasks.runner.cancel(task)
    return task.public()


@app.get('/api/guest/tasks/{task_id}')
def guest_task_status(task_id: str, request: Request):
    task = owned_task(task_id, task_owner(request, guest=True))
    return task.public(tasks.runner.position(task))


PRIVATE_FILES = ('access.sqlite3', 'print-log.sqlite3', 'devices.json', 'cups-admin.json', 'managed-airscan.conf')


def keep_private() -> None:
    """Folio's files are for Folio alone. Installations before 0.15 created some
    readable by every user; the print server's helpers run as another user."""
    for name in PRIVATE_FILES:
        for path in (DATA_DIR / name, DATA_DIR / f'{name}-wal', DATA_DIR / f'{name}-shm'):
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                continue
            except OSError:
                log.warning('Could not make %s private', path)
    try:
        JOBS_DIR.chmod(0o700)
    except OSError:
        log.warning('Could not make %s private', JOBS_DIR)


def ensure_directories():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tasks.runner.start(DATA_DIR / 'tasks')
    clear_old_previews()
    security.init(DATA_DIR)
    stats.init(DATA_DIR)
    keep_private()
    cfg=registry()
    devices.write_managed_sane_config(cfg,managed_sane_path())
    # Existing v0.3 job.json files have no owner. Claim them for the original
    # administrator so adding member accounts doesn't expose old scan archives.
    original_admin = security.initial_admin(DATA_DIR)
    for path in JOBS_DIR.glob('*/job.json'):
        try:
            with lock:
                j = json.loads(path.read_text())
                if 'owner' not in j:
                    j['owner'] = original_admin
                    save_job(j)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue


def gracefully_stop_on_server_shutdown():
    # Docker stop / redeploy should give scanimage a chance to tell WSD to
    # release the device, rather than killing the container mid-scan.
    with lock:
        active = scan_state['active']
    if active:
        scan_cancel_event.set()
        if scanner_lock.acquire(timeout=SCAN_GRACE_SECONDS + 5):
            scanner_lock.release()
