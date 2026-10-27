"""Where finished documents go: Paperless-ngx, or a folder.

Administrators set destinations up in Devices; anyone working on documents
can then send a finished PDF to one from the workbench, as a background job.

* **Paperless-ngx**: uploaded through its API (``/api/documents/post_document/``)
  with an API token, a title and optional tags. Paperless then reads the
  text, files and tags it as usual.
* **A folder**: any host folder mounted into the container under
  /destinations (a NAS share, Paperless's consume folder…). The file is
  written under a hidden name and then renamed, so folder watchers never
  pick up a half-written PDF.

API tokens stay on the Folio server; they are never sent to browsers.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FOLDER_ROOT = Path(os.environ.get('FOLIO_DESTINATIONS', '/destinations'))
KINDS = {'paperless': 'Paperless-ngx', 'folder': 'Folder'}
NAME = re.compile(r'^[\w .&()-]{1,40}$')


class DestinationError(ValueError):
    """The destination is set up wrongly or did not accept the document."""


def public(destination: dict) -> dict:
    """What browsers may see: never the token."""
    return {'id': destination['id'], 'name': destination['name'], 'kind': destination['kind'],
            'kind_label': KINDS[destination['kind']], 'url': destination.get('url'),
            'folder': destination.get('folder'), 'tags': destination.get('tags', []),
            'verify_tls': destination.get('verify_tls', True), 'has_token': bool(destination.get('token'))}


# --- Paperless-ngx ----------------------------------------------------------------

def clean_url(url: str) -> str:
    url = (url or '').strip().rstrip('/')
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise DestinationError('Enter Paperless’s address, such as http://192.168.1.20:8000')
    return url.removesuffix('/api')


def _call(url: str, token: str, verify: bool, path: str, *, body: bytes | None = None,
          content_type: str | None = None, timeout: float = 30):
    headers = {'Authorization': f'Token {token}', 'Accept': 'application/json'}
    if content_type:
        headers['Content-Type'] = content_type
    request = urllib.request.Request(url + path, data=body, headers=headers, method='POST' if body is not None else 'GET')
    context = None
    if url.startswith('https://') and not verify:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # The administrator chose to accept a self-signed certificate
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return json.loads(response.read() or b'null')
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode('utf-8', 'replace')
        if exc.code in (401, 403):
            raise DestinationError('Paperless did not accept the API token') from exc
        raise DestinationError(f'Paperless answered {exc.code}: {detail}') from exc
    except ssl.SSLError as exc:
        raise DestinationError('Paperless’s certificate could not be verified. If it is self-signed, '
                               'switch off certificate checking for this destination.') from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, 'reason', exc)
        raise DestinationError(f'Could not reach Paperless at {url} ({reason})') from exc


def paperless_tags(url: str, token: str, verify: bool = True) -> list[dict]:
    """Paperless's tags, for choosing defaults; also proves the address and token work."""
    tags, path = [], '/api/tags/?page_size=100&ordering=name'
    for _ in range(50):  # Pages of 100; enough for any household or office
        page = _call(url, token, verify, path, timeout=15)
        tags += [{'id': t['id'], 'name': t['name']} for t in page.get('results', [])]
        if not page.get('next'):
            break
        path = urllib.parse.urlsplit(page['next'])
        path = f'{path.path}?{path.query}'
    return tags


def _multipart(fields: list[tuple[str, str]], file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f'folio-{secrets.token_hex(12)}'
    parts = []
    for name, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    safe = filename.replace('"', "'")
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{safe}"\r\n'
                 f'Content-Type: application/pdf\r\n\r\n'.encode() + content + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


def send_paperless(destination: dict, pdf: Path, title: str, tags: list[int]) -> str:
    """Upload; returns Paperless's processing task id."""
    fields = [('title', title[:128])] + [('tags', str(int(tag))) for tag in tags]
    body, content_type = _multipart(fields, 'document', f'{title[:100] or "Folio document"}.pdf', pdf.read_bytes())
    task = _call(destination['url'], destination['token'], destination.get('verify_tls', True),
                 '/api/documents/post_document/', body=body, content_type=content_type, timeout=120)
    return str(task or '')


# --- Folders ------------------------------------------------------------------------

def folder_path(folder: str) -> Path:
    """The folder inside /destinations that a destination writes to, checked."""
    name = (folder or '').strip().strip('/')
    target = (FOLDER_ROOT / name).resolve()
    root = FOLDER_ROOT.resolve()
    if not name or (target != root and root not in target.parents):
        raise DestinationError('Choose a folder inside /destinations, e.g. “nas” for /destinations/nas')
    if not target.is_dir():
        raise DestinationError(f'/destinations/{name} does not exist. Mount a host folder there in compose.yaml.')
    if not os.access(target, os.W_OK):
        raise DestinationError(f'Folio may not write to /destinations/{name}. On the host, give the folder to '
                               'the user Folio runs as (PUID).')
    return target


def file_name(title: str) -> str:
    cleaned = re.sub(r'\s+', ' ', re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', title)).strip(' .')[:120] or 'Folio document'
    return f"{time.strftime('%Y-%m-%d')} {cleaned}.pdf"


def send_folder(destination: dict, pdf: Path, title: str) -> Path:
    target_dir = folder_path(destination['folder'])
    base = file_name(title)
    target, number = target_dir / base, 2
    while target.exists():
        target = target_dir / f'{base[:-4]} ({number}).pdf'
        number += 1
    fd, temporary = tempfile.mkstemp(prefix='.folio-', suffix='.part', dir=target_dir)
    try:
        with os.fdopen(fd, 'wb') as out, pdf.open('rb') as source:
            while chunk := source.read(1 << 20):
                out.write(chunk)
        os.chmod(temporary, 0o644)  # Readable by whatever picks it up (a NAS, Paperless)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


# --- Setting up -----------------------------------------------------------------------

def prepare(body: dict, existing: dict | None = None) -> tuple[dict, list[dict]]:
    """Check a new destination; returns it (with an id) and, for Paperless, its tags."""
    kind = body.get('kind')
    if kind not in KINDS:
        raise DestinationError('Choose Paperless-ngx or a folder')
    name = (body.get('name') or '').strip()
    if not NAME.fullmatch(name):
        raise DestinationError('Give it a short name (letters, numbers, spaces)')
    destination = {'id': (existing or {}).get('id') or secrets.token_hex(6), 'kind': kind, 'name': name}
    if kind == 'paperless':
        token = (body.get('token') or '').strip() or (existing or {}).get('token', '')
        if not token:
            raise DestinationError('Enter a Paperless API token (in Paperless: your profile → API token)')
        destination.update(url=clean_url(body.get('url', '')), token=token, verify_tls=bool(body.get('verify_tls', True)))
        tags = paperless_tags(destination['url'], token, destination['verify_tls'])
        known = {t['id'] for t in tags}
        destination['tags'] = [int(t) for t in body.get('tags') or [] if int(t) in known]
        return destination, tags
    folder_path(body.get('folder', ''))
    destination['folder'] = (body.get('folder') or '').strip().strip('/')
    return destination, []
