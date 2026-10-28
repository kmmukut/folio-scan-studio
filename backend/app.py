"""Folio Scan Studio: secure scanning, non-destructive edits, and optional guest printing."""
from __future__ import annotations

import hashlib
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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from backend import security
from backend.processing import transform_page
from backend import printing, devices, diagnostics, tasks, ipp, cupsadmin, cupsimport, features, stats, printserver, mail, notify
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
SETUP_WINDOW = 15 * 60  # Creating the first administrator in the browser is possible this long after starting
_setup_opened = time.monotonic()
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


def background_upkeep(stop: threading.Event, first: float = 20, interval: float = 300) -> None:
    """Every few minutes, count prints sent straight to CUPS (when that setting
    is on) before CUPS forgets them, and look at the printers for notification
    emails. The first run waits for the print server."""
    wait = first
    while not stop.wait(wait):
        wait = interval
        try:
            cupsimport.import_jobs(DATA_DIR, cups_servers())
        except Exception:  # noqa: BLE001 - background upkeep must never take Folio down
            log.warning('Counting prints sent straight to CUPS failed', exc_info=True)
        try:
            watch_printers()
        except Exception:  # noqa: BLE001
            log.warning('Checking printers for notifications failed', exc_info=True)


def watch_printers() -> int:
    """Read each printer's supplies and state, for the notification emails
    (only when one of those is on and an administrator has an address)."""
    cfg = mail.settings(DATA_DIR)
    if (not mail.configured(cfg) or not (cfg['notify']['supplies'] or cfg['notify']['attention'])
            or not security.admin_emails(DATA_DIR)):
        return 0
    _, records = printer_inventory()
    printers = []
    for record in records:
        if devices.cups_backoff(record['server']):
            continue
        try:
            supplies = printer_supplies(record['server'], record['queue'])
            state = cupsadmin.printer_state(cupsadmin.connection(DATA_DIR, record['server'], timeout=8),
                                            record['queue'])
        except (ipp.IPPError, OSError):
            continue  # Unreachable: nothing is reported, and nothing re-armed
        printers.append({'id': record['id'], 'label': record['label'], 'supplies': supplies,
                         'reasons': state['reasons']})
    return notify.check_printers(DATA_DIR, printers)


app = FastAPI(title='Folio Scan Studio', version=__version__, docs_url=None, redoc_url=None,
              lifespan=lifespan)
app.mount('/assets', StaticFiles(directory=str(WEB_DIR)), name='assets')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    public = ('/api/health', '/api/features', '/api/auth/login', '/api/auth/status', '/api/setup/admin', '/api/guest/status', '/api/guest/print', '/api/guest/inspect', '/api/guest/printers',
              '/api/auth/forgot', '/api/auth/link', '/api/auth/link/password')
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
        if session['role'] == 'printer' and not (path.startswith('/api/auth/') or (path in ('/api/print','/api/print/status','/api/print/quota','/api/print/inspect','/api/print/submit-preview','/api/printers') or path.startswith(('/api/print/preview/', '/api/print/jobs', '/api/tasks/')))):
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


FORGOT_ANSWER = ('If an account has that username or email address, a link to choose a new password is on its '
                 'way to its email. The link works for one hour.')


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
        if not cfg.get('first_print_at'):
            cfg['first_print_at'] = now()
            persist_registry(cfg)
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


def preview_owner(request: Request, guest: bool) -> str:
    cookie_name = security.GUEST_COOKIE if guest else security.COOKIE_NAME
    cookie = request.cookies.get(cookie_name)
    if not cookie:
        raise HTTPException(403, 'Your session has expired')
    return hashlib.sha256(cookie.encode('utf-8')).hexdigest()


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
        check_quota(choices.get('user'), selected * choices.get('copies', 1))
        if task:
            # Last point at which cancelling stops the job; CUPS owns it after this.
            task.update('Sending to the printer…', start + (end - start) * .95)
        outcome = print_with_cups(target, title, **choices)
    log_print(choices, selected, origin, outcome)
    note_quota_reached(choices.get('user'))
    return {**cups_receipt(outcome), 'pages_selected': selected}


def note_quota_reached(username: str | None) -> None:
    """Email a person who has just used their last page of the month. Never stops a print."""
    if not username or username == cupsadmin.GUEST_USER:
        return
    try:
        state = quota_state(username)
        if state['limit'] is not None and state['left'] == 0:
            notify.quota_reached(DATA_DIR, username, state['limit'])
    except Exception:  # noqa: BLE001
        log.warning('Could not check whether %s reached their page limit', username, exc_info=True)


def quota_state(username: str) -> dict:
    limit = security.quota_for(DATA_DIR, username)
    used = stats.pages_this_month(DATA_DIR, username)
    return {'limit': limit, 'used': used, 'left': None if limit is None else max(0, limit - used)}


def check_quota(username: str | None, pages: int) -> None:
    """Refuse a print that would go over the person's monthly pages."""
    if not username or username == cupsadmin.GUEST_USER:
        return  # Guests have their own limits
    state = quota_state(username)
    if state['limit'] is not None and pages > state['left']:
        raise HTTPException(403, f"This would print {pages} {'page' if pages == 1 else 'pages'}, but you have "
                                 f"{state['left']} of your {state['limit']} pages left this month. "
                                 'Ask an administrator for more.')


def task_owner(request: Request, guest: bool = False) -> str:
    return f'guest:{preview_owner(request, True)}' if guest else f'user:{request.state.username}'


GUEST_JOB_LIMIT = 2  # Guests: a print or two at a time (they are rate-limited as well)


def job_limit(request: Request, guest: bool = False) -> int | None:
    """How many background jobs this person may have waiting or running at once."""
    return GUEST_JOB_LIMIT if guest else security.job_limit_for(DATA_DIR, request.state.username)


def task_response(task: tasks.Task) -> JSONResponse:
    return JSONResponse({'task': task.public(tasks.runner.position(task))}, status_code=202)


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


PAGE_ASSET = re.compile(r'"(/assets/(?:js/[\w-]+\.js|style\.css))"')


def asset_version() -> str:
    """Fingerprint of the page's scripts and styles.

    The page references them as /assets/js/core.js?v=<fingerprint> and so on,
    so after an upgrade browsers and caching proxies (Nginx Proxy Manager's
    "Cache Assets" keeps them 30 minutes regardless of headers) fetch the new
    files at once.
    """
    digest = hashlib.sha256()
    for path in [*sorted((WEB_DIR / 'js').glob('*.js')), WEB_DIR / 'style.css']:
        digest.update(path.name.encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()[:12]


@app.get('/', response_class=HTMLResponse)
def homepage():
    version = asset_version()
    page = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
    page = PAGE_ASSET.sub(lambda m: f'"{m.group(1)}?v={version}"', page)
    # The page itself is always revalidated; it is what points at the current assets.
    return HTMLResponse(page, headers={'Cache-Control': 'no-cache'})


@app.get('/manifest.webmanifest')
def web_app_manifest():
    """What lets phones and computers install Folio as an app."""
    return FileResponse(WEB_DIR / 'manifest.webmanifest', media_type='application/manifest+json',
                        headers={'Cache-Control': 'no-cache'})


@app.get('/sw.js')
def service_worker():
    """Served from the site's root: a service worker only looks after pages in
    its own folder and below. Always revalidated, so upgrades reach it."""
    return FileResponse(WEB_DIR / 'sw.js', media_type='text/javascript', headers={'Cache-Control': 'no-cache'})


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


@app.exception_handler(tasks.Busy)
def too_many_jobs(request: Request, exc: tasks.Busy):
    return JSONResponse({'detail': str(exc)}, status_code=429)


@app.exception_handler(features.Unavailable)
def feature_unavailable(request: Request, exc: features.Unavailable):
    return JSONResponse({'detail': str(exc), 'feature': exc.feature}, status_code=409)


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


def load_page_image(job_id: str, page: dict, *, apply_edit=True, uncropped=False) -> Image.Image:
    file = job_path(job_id) / 'pages' / page['file']
    with RENDER_SLOTS:
        image = Image.open(file)
        image.load()
    if apply_edit:
        image = transform_page(image, page, uncropped=uncropped)
    return image


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


def configured_destinations() -> list[dict]:
    return registry().get('destinations', [])


def owned_task(task_id: str, owner: str) -> tasks.Task:
    task = tasks.runner.get(task_id, owner) if TASK_ID_RE.fullmatch(task_id) else None
    if not task:
        raise HTTPException(404, 'This job is no longer available. Folio may have restarted; please try again.')
    return task


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
    global _setup_opened
    _setup_opened = time.monotonic()
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


# The route handlers live in backend/routes, by area. They are added last because
# they use what is defined above (as web.NAME).
from backend.routes import ROUTERS  # noqa: E402

for _router in ROUTERS:
    app.include_router(_router)
