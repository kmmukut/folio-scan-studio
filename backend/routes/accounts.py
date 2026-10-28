"""Signing in, accounts, invitations, password links, page quotas and job limits.

Route handlers moved out of app.py in 0.21. Shared state and helpers stay in
app.py and are reached as web.NAME, so tests can still swap them there.
"""
from __future__ import annotations

import json
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend import mail, notify, security, stats
from backend import app as web

router = APIRouter()


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
    email: str | None = Field(default=None, max_length=254)


class UserUpdate(BaseModel):
    role: Literal['admin','member','printer'] | None = None
    enabled: bool | None = None
    password: str | None = None
    email: str | None = Field(default=None, max_length=254)  # Sent as null or '' to remove it


class InviteBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    role: Literal['admin','member','printer'] = 'member'


class LinkRequest(BaseModel):
    send: bool = True


class ForgotBody(BaseModel):
    login: str = Field(min_length=1, max_length=254)


class LinkBody(BaseModel):
    token: str = Field(min_length=10, max_length=100)


class LinkPasswordBody(LinkBody):
    password: str = Field(min_length=12, max_length=256)


class AccountBody(BaseModel):
    email: str | None = Field(default=None, max_length=254)


@router.get('/api/auth/status')
def auth_status(request: Request):
    session = security.session(web.DATA_DIR, request.cookies.get(security.COOKIE_NAME))
    return {'authenticated':bool(session), 'username':session['username'] if session else None,
            'role':session['role'] if session else None,
            'csrf':session['csrf'] if session else None,
            'guest_print':security.guest_enabled(web.DATA_DIR),
            'password_reset':mail.reset_available(web.DATA_DIR),
            **setup_state()}


def setup_state() -> dict:
    """Whether the first administrator still has to be created, and whether that
    is still possible (for SETUP_WINDOW after Folio started, so a forgotten
    installation can't be claimed by whoever finds it later)."""
    required = security.setup_required(web.DATA_DIR)
    return {'setup_required': required,
            'setup_open': required and time.monotonic() - web._setup_opened < web.SETUP_WINDOW}


@router.post('/api/setup/admin')
def create_first_administrator(body: LoginBody, response: Response):
    state = setup_state()
    if not state['setup_required']:
        raise HTTPException(409, 'Folio already has an administrator. Sign in instead.')
    if not state['setup_open']:
        raise HTTPException(403, 'For safety, the first administrator can only be created in the first '
                                 f'{web.SETUP_WINDOW // 60} minutes after Folio starts. Restart Folio and try again.')
    try:
        record = security.create_first_admin(web.DATA_DIR, body.username.strip(), body.password)
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    response.set_cookie(security.COOKIE_NAME, record['token'], httponly=True,
                        secure=web.COOKIE_SECURE, samesite='strict', max_age=security.SESSION_SECONDS, path='/')
    return {'authenticated':True, 'username':record['username'], 'role':record['role'], 'csrf':record['csrf']}


@router.post('/api/auth/login')
def login(body: LoginBody, request: Request, response: Response):
    record, error = security.try_login(web.DATA_DIR, body.username, body.password,
                                        request.client.host if request.client else 'unknown')
    if error:
        raise HTTPException(429 if error.startswith('Too many') else 401, error)
    response.set_cookie(security.COOKIE_NAME, record['token'], httponly=True,
                        secure=web.COOKIE_SECURE, samesite='strict', max_age=security.SESSION_SECONDS, path='/')
    return {'authenticated':True, 'username':record['username'], 'role':record['role'], 'csrf':record['csrf']}


@router.post('/api/auth/logout')
def logout(request: Request, response: Response):
    security.logout(web.DATA_DIR, request.cookies.get(security.COOKIE_NAME))
    response.delete_cookie(security.COOKIE_NAME, path='/')
    return {'ok':True}


@router.post('/api/auth/password')
def change_password(body: PasswordBody, request: Request, response: Response):
    if not security.update_password(web.DATA_DIR, request.state.username, body.current_password, body.new_password):
        raise HTTPException(400, 'Current password incorrect, or new password is shorter than 12 characters')
    notify.password_changed(web.DATA_DIR, request.state.username, 'self')
    response.delete_cookie(security.COOKIE_NAME, path='/')
    return {'ok':True, 'reauthenticate':True}


@router.get('/api/auth/account')
def my_account(request: Request):
    account = security.account(web.DATA_DIR, request.state.username)
    return {'username': account['username'], 'role': account['role'], 'email': account['email'],
            'email_ready': mail.configured(mail.settings(web.DATA_DIR))}


@router.put('/api/auth/account')
def update_my_account(body: AccountBody, request: Request):
    try:
        security.set_email(web.DATA_DIR, request.state.username, body.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return my_account(request)


@router.post('/api/auth/forgot')
def forgot_password(body: ForgotBody, request: Request):
    """Email a reset link. The answer is the same whether or not the account
    exists, and sending happens in the background, so it takes as long either way."""
    if not mail.reset_available(web.DATA_DIR):
        raise HTTPException(409, 'Password reset by email isn’t set up here. Ask an administrator.')
    if not security.reset_rate_allowed(request.client.host if request.client else 'unknown', body.login):
        raise HTTPException(429, 'Too many requests. Try again in a few minutes.')
    target = security.reset_target(web.DATA_DIR, body.login)
    if target:
        token = security.create_link(web.DATA_DIR, target['username'], 'reset')
        notify.reset(web.DATA_DIR, target['username'], target['email'], mail.password_link(web.DATA_DIR, token))
    else:
        security.link_decoy(web.DATA_DIR)
    return {'detail': web.FORGOT_ANSWER}


@router.post('/api/auth/link')
def password_link_account(body: LinkBody):
    """Who an invitation or reset link is for, to greet them on the page."""
    found = security.link_account(web.DATA_DIR, body.token)
    if not found:
        raise HTTPException(404, 'This link has expired or was already used. Ask for a new one.')
    return {'username': found['username'], 'purpose': found['purpose']}


@router.post('/api/auth/link/password')
def set_password_by_link(body: LinkPasswordBody, response: Response):
    try:
        record = security.use_link(web.DATA_DIR, body.token, body.password)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        if record['purpose'] == 'invite':
            notify.joined(web.DATA_DIR, record['username'], record['role'])
        else:
            notify.password_changed(web.DATA_DIR, record['username'], 'reset')
    except Exception:  # noqa: BLE001 - an email must never undo a password change
        web.log.warning('Could not queue an email about %s', record['username'], exc_info=True)
    response.set_cookie(security.COOKIE_NAME, record['token'], httponly=True,
                        secure=web.COOKIE_SECURE, samesite='strict', max_age=security.SESSION_SECONDS, path='/')
    return {'authenticated':True, 'username':record['username'], 'role':record['role'], 'csrf':record['csrf']}


@router.post('/api/admin/guest')
def change_guest(body: GuestSettings):
    security.set_guest(web.DATA_DIR, body.enabled)
    return {'enabled':security.guest_enabled(web.DATA_DIR)}


@router.get('/api/admin/users')
def users():
    return [{**user, 'quota': web.quota_state(user['username']),
             'jobs': {'limit': security.job_limit_for(web.DATA_DIR, user['username'])}}
            for user in security.list_users(web.DATA_DIR)]


class QuotaBody(BaseModel):
    monthly_pages: int | None = Field(default=None, ge=-1, le=100000)


@router.put('/api/admin/users/{username}/quota')
def set_user_quota(username: str, body: QuotaBody):
    """``null``: follow the default; -1: no limit; otherwise pages a month."""
    try:
        security.set_quota(web.DATA_DIR, username, body.monthly_pages)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return web.quota_state(username)


@router.get('/api/admin/quota-default')
def quota_default():
    return {'monthly_pages': security.quota_default(web.DATA_DIR), 'statistics_enabled': stats.settings(web.DATA_DIR)['enabled']}


@router.put('/api/admin/quota-default')
def set_quota_default(body: QuotaBody):
    if body.monthly_pages == -1:
        body.monthly_pages = None
    try:
        security.set_quota_default(web.DATA_DIR, body.monthly_pages)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return quota_default()


class JobLimitBody(BaseModel):
    job_limit: int | None = Field(default=None, ge=-1, le=security.JOB_LIMIT_MAX)


@router.put('/api/admin/users/{username}/job-limit')
def set_user_job_limit(username: str, body: JobLimitBody):
    """``null``: follow the default; -1: no limit; otherwise jobs at once."""
    try:
        security.set_job_limit(web.DATA_DIR, username, body.job_limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'limit': security.job_limit_for(web.DATA_DIR, username)}


@router.get('/api/admin/job-limit-default')
def job_limit_default():
    return {'job_limit': security.job_limit_default(web.DATA_DIR)}


@router.put('/api/admin/job-limit-default')
def set_job_limit_default(body: JobLimitBody):
    try:
        security.set_job_limit_default(web.DATA_DIR, body.job_limit if body.job_limit is not None else 0)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return job_limit_default()


@router.post('/api/admin/users', status_code=201)
def add_user(body: UserCreate):
    try:
        return security.add_user(web.DATA_DIR, body.username, body.password, body.role, body.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch('/api/admin/users/{username}')
def update_user(username: str, body: UserUpdate, request: Request):
    try:
        result = security.edit_user(web.DATA_DIR, username, request.state.username,
                                    role=body.role, enabled=body.enabled, password=body.password,
                                    email=body.email if 'email' in body.model_fields_set else False)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if body.password is not None:
        notify.password_changed(web.DATA_DIR, username, 'admin')
    return result


def deliver_link(username: str, token: str, purpose: str, inviter: str, send: bool) -> dict:
    """Email a new invitation or reset link if possible; the link is returned
    either way, for the administrator to pass on themselves."""
    account = security.account(web.DATA_DIR, username)
    link = mail.password_link(web.DATA_DIR, token)
    result = {'token': token, 'link': link, 'purpose': purpose, 'email': account['email'], 'emailed': False,
              'email_error': None}
    if send and account['email'] and mail.configured(mail.settings(web.DATA_DIR)):
        if not link:
            result['email_error'] = 'Folio’s address isn’t set in Devices → Email, so the email would have no link.'
            return result
        try:
            if purpose == 'invite':
                notify.invitation(web.DATA_DIR, username, account['email'], link, inviter)
            else:
                notify.reset(web.DATA_DIR, username, account['email'], link, wait=True)
            result['emailed'] = True
        except mail.MailError as exc:
            result['email_error'] = str(exc)
    return result


@router.post('/api/admin/invitations', status_code=201)
def invite_user(body: InviteBody, request: Request):
    """An account whose person chooses their own password, through a link
    emailed to them or passed on by the administrator."""
    try:
        token = security.invite_user(web.DATA_DIR, body.username.strip(), body.email, body.role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**deliver_link(body.username.strip(), token, 'invite', request.state.username, True), 'users': users()}


@router.post('/api/admin/users/{username}/link')
def new_password_link(username: str, body: LinkRequest, request: Request):
    """A fresh invitation (for someone who hasn't chosen a password yet) or password reset link."""
    account = security.account(web.DATA_DIR, username)
    if not account:
        raise HTTPException(404, 'User does not exist')
    purpose = 'invite' if account['pending'] else 'reset'
    token = security.create_link(web.DATA_DIR, username, purpose)
    return deliver_link(username, token, purpose, request.state.username, body.send)


@router.delete('/api/admin/users/{username}')
def delete_user(username: str, request: Request):
    try:
        result = security.remove_user(web.DATA_DIR, username, request.state.username)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Do not allow a later account with the same username to inherit scans
    # owned by someone who was deleted. Transfer them to the acting admin.
    with web.lock:
        for path in web.JOBS_DIR.glob('*/job.json'):
            try:
                j = json.loads(path.read_text())
                if j.get('owner') == username:
                    j['owner'] = request.state.username
                    web.save_job(j)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
    stats.forget_user(web.DATA_DIR, username)
    return result
