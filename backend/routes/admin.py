"""Statistics, System check, backups, destinations, email settings and the setup checklist.

Route handlers moved out of app.py in 0.21. Shared state and helpers stay in
app.py and are reached as web.NAME, so tests can still swap them there.
"""
from __future__ import annotations

import io
import csv
import shutil
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend import airprint, backup, cupsadmin, cupsimport, destinations, devices, diagnostics, features, ipp, mail, printserver, security, stats, tasks
from backend.version import __version__
from backend import app as web

router = APIRouter()


class MailBody(BaseModel):
    host: str = Field(default='', max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    security: Literal['starttls', 'ssl', 'none'] = 'starttls'
    username: str = Field(default='', max_length=254)
    password: str = Field(default='', max_length=512)  # Empty keeps the stored one
    sender: str = Field(default='', max_length=254)
    verify_tls: bool = True
    public_url: str = Field(default='', max_length=300)
    password_reset: bool = True
    notify: dict[str, bool] = Field(default_factory=dict)


class MailTestBody(BaseModel):
    to: str | None = Field(default=None, max_length=254)


@router.get('/api/admin/mail')
def mail_settings(request: Request):
    return {**mail.public(web.DATA_DIR), 'my_email': security.account(web.DATA_DIR, request.state.username)['email'],
            'admins_with_email': len(security.admin_emails(web.DATA_DIR))}


@router.put('/api/admin/mail')
def save_mail_settings(body: MailBody, request: Request):
    try:
        mail.save(web.DATA_DIR, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return mail_settings(request)


@router.delete('/api/admin/mail')
def remove_mail_settings(request: Request):
    mail.remove(web.DATA_DIR)
    return mail_settings(request)


@router.post('/api/admin/mail/test')
def send_test_mail(body: MailTestBody, request: Request):
    try:
        to = security.clean_email(body.to) or security.account(web.DATA_DIR, request.state.username)['email']
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not to:
        raise HTTPException(400, 'Enter an address to send the test to')
    try:
        mail.send(web.DATA_DIR, to, 'Folio test email',
                  f'This is a test from Folio, sent by {request.state.username}. Email works: invitations, '
                  'password resets and notifications can be sent.', automatic=False)
    except mail.MailError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {'sent_to': to}


class StatsSettingsBody(BaseModel):
    enabled: bool
    retention_days: int
    include_cups_jobs: bool | None = None


def stats_period(period: str) -> str:
    if period not in stats.PERIODS:
        raise HTTPException(400, 'Unknown period')
    return period


def builtin_admin() -> ipp.Connection | None:
    if not printserver.enabled():
        return None
    return cupsadmin.connection(web.DATA_DIR, printserver.SERVER, admin=True, timeout=15)


@router.post('/api/admin/backup', status_code=202)
def start_backup(request: Request):
    """Pack everything Folio keeps into one file, as a background job."""
    def work(task: tasks.Task) -> dict:
        task.update('Collecting the built-in printers…', .02)
        try:
            printers = backup.builtin_printers(builtin_admin()) if printserver.enabled() else []
        except (ipp.IPPError, OSError) as exc:
            raise HTTPException(503, f'The built-in print server did not answer ({exc}). Try again in a minute.') from exc
        name = f"folio-backup-{datetime.now().strftime('%Y-%m-%d')}.tar.gz"
        target = task.directory / name
        manifest = backup.create(web.DATA_DIR, target, version=__version__, edition=features.edition(),
                                 printers=printers, progress=lambda done, total: task.update(
                                     f'Packing documents ({done} of {total} files)…', .05 + .9 * done / max(total, 1)))
        task.file, task.filename = target, name
        cfg = web.registry()
        cfg['last_backup_at'] = web.now()
        web.persist_registry(cfg)
        return {**manifest, 'bytes': target.stat().st_size}
    return web.task_response(tasks.runner.submit('backup', 'export', web.task_owner(request), work, limit=web.job_limit(request)))


@router.post('/api/admin/restore', status_code=202)
async def start_restore(request: Request):
    """Upload a backup, check it, then replace Folio's data with it in the background."""
    upload = web.DATA_DIR / '.restore-upload.tar.gz'
    free = shutil.disk_usage(web.DATA_DIR).free
    total = 0
    with upload.open('wb') as target:
        async for chunk in request.stream():
            total += len(chunk)
            if total > free // 2:
                target.close()
                upload.unlink(missing_ok=True)
                raise HTTPException(413, 'Not enough free space to restore this backup')
            target.write(chunk)
    try:
        manifest = backup.inspect(upload, __version__)
    except backup.BackupError as exc:
        upload.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    # Keep the person restoring signed in, if their account is in the backup.
    token = request.cookies.get(security.COOKIE_NAME)
    current = security.session(web.DATA_DIR, token)

    def work(task: tasks.Task) -> dict:
        def swapped() -> None:
            task.update('Starting up with the restored data…', .9)
            web.JOBS_DIR.mkdir(parents=True, exist_ok=True)  # A backup without documents has no jobs folder
            security.init(web.DATA_DIR)
            if current and token:
                security.keep_session(web.DATA_DIR, token, current['username'], current['csrf'])
            stats.init(web.DATA_DIR)
            web.keep_private()
            devices.invalidate()
            devices.write_managed_sane_config(web.registry(), web.managed_sane_path())
        try:
            return backup.restore(upload, web.DATA_DIR, current_version=__version__, conn=builtin_admin(),
                                  on_swapped=swapped, progress=lambda done, total: task.update(
                                      f'Restoring ({done} of {total} files)…', .05 + .8 * done / max(total, 1)))
        finally:
            upload.unlink(missing_ok=True)
    task = tasks.runner.submit('restore', 'export', web.task_owner(request), work, limit=web.job_limit(request))
    return JSONResponse({'task': task.public(tasks.runner.position(task)), 'manifest': manifest}, status_code=202)


class ChecklistBody(BaseModel):
    dismissed: bool


@router.get('/api/admin/checklist')
def setup_checklist():
    """The first steps after installing, ticked off by what is actually set up."""
    cfg, records = web.printer_inventory()
    steps = [
        {'id': 'printer', 'title': 'Add a printer', 'done': bool(records)},
        {'id': 'print', 'title': 'Print a test page', 'done': bool(cfg.get('first_print_at'))},
        {'id': 'scanner', 'title': 'Add a scanner', 'done': bool(cfg.get('scanners'))},
        {'id': 'people', 'title': 'Invite the people who will use Folio', 'optional': True,
         'done': len(security.list_users(web.DATA_DIR)) > 1},
        {'id': 'airprint', 'title': 'Let phones print to your printers', 'optional': True,
         'done': bool(airprint.status().get('announced'))},
        {'id': 'backup', 'title': 'Download a backup', 'optional': True, 'done': bool(cfg.get('last_backup_at'))},
        {'id': 'email', 'title': 'Set up email for invitations, password resets and notifications',
         'optional': True, 'done': mail.configured(mail.settings(web.DATA_DIR))},
    ]
    return {'steps': steps, 'dismissed': bool(cfg.get('checklist_dismissed')), 'last_backup_at': cfg.get('last_backup_at'),
            'complete': all(step['done'] for step in steps if not step.get('optional'))}


@router.put('/api/admin/checklist')
def save_setup_checklist(body: ChecklistBody):
    cfg = web.registry()
    cfg['checklist_dismissed'] = body.dismissed
    web.persist_registry(cfg)
    return setup_checklist()


@router.get('/api/admin/stats')
def print_statistics(period: str = '12m'):
    """What Folio has printed, by person and printer (plus jobs sent straight to CUPS, if chosen)."""
    cupsimport.import_jobs(web.DATA_DIR, web.cups_servers(), min_interval=60)
    return {**stats.summary(web.DATA_DIR, stats_period(period)), 'guest_user': cupsadmin.GUEST_USER}


@router.get('/api/admin/stats/export')
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
        for row in stats.export_rows(web.DATA_DIR, period):
            writer.writerow([cell(value) for value in row])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
        yield buffer.getvalue()
    filename = f'folio-print-log-{period}-{datetime.now().strftime("%Y-%m-%d")}.csv'
    return StreamingResponse(lines(), media_type='text/csv; charset=utf-8',
                             headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@router.put('/api/admin/stats/settings')
def save_statistics_settings(body: StatsSettingsBody):
    try:
        return stats.save_settings(web.DATA_DIR, enabled=body.enabled, retention_days=body.retention_days,
                                   include_cups_jobs=body.include_cups_jobs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete('/api/admin/stats')
def clear_print_statistics():
    return {'deleted': stats.clear(web.DATA_DIR)}


@router.get('/api/admin/diagnostics')
def admin_diagnostics(request: Request):
    cfg = web.registry()
    servers = web.cups_servers(cfg)
    present = [(q['server'], q['queue']) for q in devices.all_cups_queues(servers) if q.get('queue')]
    return diagnostics.run_checks(
        data_dir=web.DATA_DIR, jobs_dir=web.JOBS_DIR, cups_servers=servers, registry=cfg,
        request=request, cookie_secure=web.COOKIE_SECURE, version=__version__,
        default_printer=cfg.get('default_printer'), printers=present[:20], supplies_for=web.quiet_supplies,
        extra=quota_checks(), system_extra=mail_checks())


def mail_checks() -> list[dict]:
    cfg = mail.settings(web.DATA_DIR)
    if not mail.configured(cfg):
        return [diagnostics.check('email', 'Email', 'info', 'Not set up. With email, people can be invited, reset '
                                  'a forgotten password and receive notifications: Devices → Email.')]
    checks = []
    last = mail.last_result()
    encryption = {'starttls': 'STARTTLS', 'ssl': 'SSL/TLS', 'none': 'no encryption'}[cfg['security']]
    if last['ok'] is False:
        checks.append(diagnostics.check('email', 'Email', 'warn', f"The last email failed: {last['error']}"))
    else:
        checks.append(diagnostics.check('email', 'Email', 'ok', f"Sends through {cfg['host']}:{cfg['port']} "
                                        f"({encryption}){', last email sent fine' if last['ok'] else ''}."))
    if not mail.public_url(web.DATA_DIR, cfg):
        checks.append(diagnostics.check('public-url', 'Links in emails', 'warn', 'Folio’s address isn’t set, so '
                                        'invitations and password resets can’t be emailed. Set it in Devices → Email.'))
    if any(cfg['notify'].values()) and not security.admin_emails(web.DATA_DIR):
        checks.append(diagnostics.check('admin-email', 'Notifications', 'warn', 'No administrator has an email '
                                        'address, so notifications have nowhere to go. Add yours under Your account.'))
    return checks


def quota_checks() -> list[dict]:
    """Quotas are counted from the print log, so they need statistics kept."""
    limited = security.quota_default(web.DATA_DIR) is not None or any(
        u['monthly_pages'] is not None and u['monthly_pages'] >= 0
        for u in security.list_users(web.DATA_DIR) if u['role'] != 'admin')
    if not limited:
        return []
    if not stats.settings(web.DATA_DIR)['enabled']:
        return [diagnostics.check('quotas', 'Print quotas', 'warn', 'Monthly page limits are set, but print '
                                  'statistics are switched off, so pages aren’t counted and nobody is limited. '
                                  'Turn statistics on under Statistics → What Folio keeps.')]
    return [diagnostics.check('quotas', 'Print quotas', 'ok', 'Monthly page limits are counted from the print log.')]


class DestinationBody(BaseModel):
    kind: Literal['paperless', 'folder']
    name: str = Field(min_length=1, max_length=40)
    url: str | None = Field(default=None, max_length=300)
    token: str | None = Field(default=None, max_length=200)
    verify_tls: bool = True
    folder: str | None = Field(default=None, max_length=120)
    tags: list[int] = Field(default_factory=list, max_length=50)


def destination_call(fn, *args):
    try:
        return fn(*args)
    except destinations.DestinationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/api/admin/destinations')
def admin_destinations():
    return [destinations.public(d) for d in web.configured_destinations()]


@router.post('/api/admin/destinations/check')
def check_destination(body: DestinationBody):
    """Try the details without saving; for Paperless, also lists its tags to choose from."""
    destination, tags = destination_call(destinations.prepare, body.model_dump())
    return {'ok': True, 'tags': tags}


@router.post('/api/admin/destinations', status_code=201)
def add_destination(body: DestinationBody):
    cfg = web.registry()
    if len(cfg.get('destinations', [])) >= 20:
        raise HTTPException(400, 'Folio supports up to 20 destinations')
    destination, _ = destination_call(destinations.prepare, body.model_dump())
    cfg.setdefault('destinations', []).append(destination)
    web.persist_registry(cfg)
    return destinations.public(destination)


@router.delete('/api/admin/destinations/{destination_id}')
def remove_destination(destination_id: str):
    cfg = web.registry()
    kept = [d for d in cfg.get('destinations', []) if d['id'] != destination_id]
    if len(kept) == len(cfg.get('destinations', [])):
        raise HTTPException(404, 'Unknown destination')
    cfg['destinations'] = kept
    web.persist_registry(cfg)
    return {'removed': destination_id}
